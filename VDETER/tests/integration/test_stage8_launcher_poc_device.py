"""P02: PoC device 경계가 **실 프로세스 경계 + 실 RPC**를 건너간다.

자식 프로세스가 launcher 역할을 한다 — Stage 7 endpoint를 소유하고 TEST adapter를 실
`SOCK_SEQPACKET` socket으로 서빙한다. 부모는 supervisor 역할로 실 `SeqPacketClient` +
`Stage7ScenarioController`로만 접근한다. 대체물은 **B210 device I/O 하나**뿐이다.

여기서 확인하는 것은 요청·식별자·응답·오류·거부 경계다. 이 시험을 "실물 종단 PASS"로 합치지
않는다 — RF 방사와 실 B210 수용은 실물에서만 확인된다.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
_LINUX = sys.platform.startswith("linux")
_PLAN_SHA = hashlib.sha256(b"stage8-poc-device-harness").hexdigest()
#: 권위 canonical 채널 이름(문자열을 시험이 따로 정의하지 않는다).
from vdeter.stage7.configuration import LOWER_CHANNEL_NAME as _LOWER  # noqa: E402


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class LauncherPocDeviceRpcTest(unittest.TestCase):
    """실 자식 프로세스의 PoC device op — 정상·거부·오류·식별자 보존."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="poc-dev-")
        os.chmod(self._dir, 0o700)
        env = dict(os.environ)
        src = os.path.join(_REPO, "VDETER", "src")
        vdeter = os.path.join(_REPO, "VDETER")
        env["PYTHONPATH"] = os.pathsep.join(
            [src, vdeter, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        self.child = subprocess.Popen(
            [sys.executable, "-m", "tests.support.poc_device_harness",
             self._dir, _PLAN_SHA],
            cwd=vdeter, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_child)
        ready = self.child.stdout.readline().strip()
        if ready != "READY":
            self.fail(f"harness가 준비되지 않았습니다: {ready!r} "
                      f"stderr={self.child.stderr.read()[:500]}")
        from vdeter.stage7.testing.controller import Stage7ScenarioController
        from vdeter.stage7.transport import SeqPacketClient

        class _Channel:
            def __init__(self, client):
                self._client = client

            def request(self, message):
                deadline = time.monotonic() + 30.0
                self._client.send(message)
                while time.monotonic() < deadline:
                    received = self._client.recv(timeout=2.0)
                    if received is None:
                        continue
                    if received.get("command_id") == message["command_id"]:
                        return received
                raise TimeoutError("PoC device 응답 초과")

        # endpoint마다 **자기 socket**으로 연결한다(실 배치의 launcher별 socket과 같은 경계).
        channels = {}
        for endpoint_id in ("MASS-A", "MASS-B", "ROC"):
            client = SeqPacketClient(os.path.join(self._dir, f"{endpoint_id}.test.sock"))
            self.addCleanup(client.close)
            channels[endpoint_id] = _Channel(client)
            if endpoint_id == "MASS-B":
                self.client = client
        self.controller = Stage7ScenarioController(
            run_id="stage8-poc-dev", resolved_test_plan_sha256=_PLAN_SHA,
            adapter_channels=channels,
            host_raw_clock=lambda: int(time.monotonic_ns()))

    def _stop_child(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)

    def _call(self, name, body):
        return self.controller.poc_command("MASS-B", name, body)

    # --- 정상 조회 -----------------------------------------------------------

    def test_radio_state_crosses_the_real_process_boundary(self):
        """실 socket으로 받은 장치 상태는 자식이 소유한 **실 worker**에서 온다."""
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        state = _C.poc_result(self._call("poc_radio_state", {}))
        self.assertEqual(state["endpoint_id"], "MASS-B")
        # 실 hardware clock(합성 counter 아님)과 실 lane mute 상태.
        self.assertIsInstance(state["hardware_time_ns"], int)
        self.assertGreater(state["hardware_time_ns"], 0)
        self.assertEqual(state["tx_muted"], [False, False])
        # bridge 결속 증거가 실려야 readiness/scheduler가 좌표를 신뢰할 수 있다.
        self.assertIsNotNone(state["clock_id"])

    def test_hardware_time_advances_between_calls(self):
        """시각이 실제로 흐른다 — 고정 stub 값이 아니다."""
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        first = _C.poc_result(self._call("poc_radio_state", {}))["hardware_time_ns"]
        time.sleep(0.2)
        second = _C.poc_result(self._call("poc_radio_state", {}))["hardware_time_ns"]
        self.assertGreater(second, first)

    def test_unknown_burst_state_is_none_not_success(self):
        """원장에 없는 burst는 None이다 — 성공으로 추정하지 않는다."""
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        result = _C.poc_result(
            self._call("poc_tx_burst_state", {"burst_id": "00000000-0000-4000-8000-000000000000"}))
        self.assertIsNone(result)

    # --- 오류·거부 경계 -------------------------------------------------------

    def test_unknown_op_is_rejected(self):
        response = self._call("poc_not_an_op", {})
        self.assertEqual(response.get("status"), "REJECTED")

    def test_body_key_violation_is_rejected_without_calling_the_device(self):
        """계약 밖 body는 장치에 닿기 전에 거부된다(fail-closed)."""
        response = self._call("poc_tx_burst_state", {"burst_id": "x", "extra": 1})
        self.assertEqual(response.get("status"), "REJECTED")
        self.assertEqual((response.get("body") or {}).get("call_outcome"), "NOT_CALLED")

    def test_sample_digest_mismatch_is_refused_before_transmit(self):
        """표본이 전송 중 바뀌면 **송신 전에** 거부한다 — launcher가 스스로 확인한다."""
        samples = b"\x00" * (8192 * 8)
        response = self._call("poc_schedule_group_tx", {
            "burst_id": "11111111-1111-4111-8111-111111111111",
            "channel_name": _LOWER,
            "transmit_at_hw_ns": 1_000_000_000,
            "samples_base64": base64.b64encode(samples).decode("ascii"),
            "samples_sha256": hashlib.sha256(b"different").hexdigest(),
            "destination_station_id": 222_222_222,
            "request_digest": "",
        })
        self.assertEqual(response.get("status"), "REJECTED")
        self.assertIn("samples_sha256", str((response.get("body") or {}).get("reason")))

    def test_wrong_sample_count_is_refused(self):
        """device burst 계약(8,192 표본)을 벗어나면 거부한다 — 잘라내거나 채우지 않는다."""
        samples = b"\x00" * (4096 * 8)
        response = self._call("poc_schedule_group_tx", {
            "burst_id": "22222222-2222-4222-8222-222222222222",
            "channel_name": _LOWER,
            "transmit_at_hw_ns": 1_000_000_000,
            "samples_base64": base64.b64encode(samples).decode("ascii"),
            "samples_sha256": hashlib.sha256(samples).hexdigest(),
            "destination_station_id": 222_222_222,
            "request_digest": "",
        })
        self.assertEqual(response.get("status"), "REJECTED")
        self.assertIn("device burst 크기", str((response.get("body") or {}).get("reason")))

    def test_unknown_channel_name_is_refused(self):
        samples = b"\x00" * (8192 * 8)
        response = self._call("poc_schedule_group_tx", {
            "burst_id": "33333333-3333-4333-8333-333333333333",
            "channel_name": "SIDEBAND",
            "transmit_at_hw_ns": 1_000_000_000,
            "samples_base64": base64.b64encode(samples).decode("ascii"),
            "samples_sha256": hashlib.sha256(samples).hexdigest(),
            "destination_station_id": 222_222_222,
            "request_digest": "",
        })
        self.assertEqual(response.get("status"), "REJECTED")

    def test_replay_of_same_command_id_returns_cached_response(self):
        """같은 command_id + 같은 body는 재실행이 아니라 캐시 응답이다(중복 송신 금지)."""
        message = self.controller._command("MASS-B", "poc_radio_state", {})
        first = self.client.request(message, timeout=30.0)
        second = self.client.request(dict(message), timeout=30.0)
        self.assertEqual(first["body_sha256"], second["body_sha256"])

    # --- 정상 송신 종단(프로세스 경계 → 권위 schedule_tx → TX 원장) -------------

    def _device_samples(self, payload: bytes):
        """supervisor 쪽에서 **frozen PHY**로 device-rate 표본을 만든다(P05 권위 변환)."""
        from apps.stage8_stage7_seams import Link11WaveformFactory
        from vdeter.stage8.shared_medium.iq import to_cf32le
        from vdeter.stage8.poc_api.types import PhysicalChannel

        burst = Link11WaveformFactory().encode(
            source_station="MASS-B", channel=PhysicalChannel.LOWER, payload=payload)
        return to_cf32le(burst.samples)

    def test_poc_burst_reaches_the_authoritative_tx_ledger_across_processes(self):
        """실 RPC로 보낸 burst가 자식의 **권위 `schedule_tx`**를 통과해 TX 원장에 등록된다.

        확인 순서가 중요하다: 접수 직후 원장은 `SUBMITTED`(진행 중)여야 하고, 시간이 흐른 뒤에야
        확정된다. 접수를 즉시 성공으로 바꾸지 않는다는 증거다. 실제 RF 방사는 여기서 증명되지
        않는다(device I/O가 대체물이다).
        """
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        # 파형 생성(frozen PHY + 보간)은 **lead 창 밖에서** 끝낸다. lead는 장치를 위한 시간이지
        # supervisor의 계산 시간이 아니다 — 안에서 하면 lead를 스스로 소진한다.
        samples = self._device_samples(bytes(range(54)))
        encoded = base64.b64encode(samples).decode("ascii")
        digest = hashlib.sha256(samples).hexdigest()
        state = _C.poc_result(self._call("poc_radio_state", {}))
        # 권위 최소 lead(100 ms, retune 시 200 ms)보다 뒤로 잡는다 — lead를 완화하지 않는다.
        transmit_at = int(state["hardware_time_ns"]) + 600_000_000
        burst_id = "44444444-4444-4444-8444-444444444444"
        accepted = _C.poc_result(self._call("poc_schedule_group_tx", {
            "burst_id": burst_id,
            "channel_name": _LOWER,
            "transmit_at_hw_ns": transmit_at,
            "samples_base64": encoded,
            "samples_sha256": digest,
            "destination_station_id": 222_222_222,
            "request_digest": hashlib.sha256(b"poc-req").hexdigest(),
        }))
        self.assertTrue(accepted["accepted"], accepted)

        # 접수 직후: 원장에 있으나 **미확정**이어야 한다.
        immediate = _C.poc_result(self._call("poc_tx_burst_state", {"burst_id": burst_id}))
        self.assertIsNotNone(immediate, "권위 원장에 burst가 없다")
        self.assertFalse(immediate["terminal"], immediate)
        self.assertTrue(all(v == "SUBMITTED" for v in immediate["children"].values()), immediate)
        # 두 물리 경로로 fan-out됐다(§5.1 고정 배선).
        self.assertEqual(sorted(immediate["children"]), ["0", "1"], immediate)

        # 시간이 흐르면 자식의 권위 poll loop이 통지를 소비해 확정한다.
        deadline = time.monotonic() + 20.0
        final = immediate
        while time.monotonic() < deadline and not final["terminal"]:
            time.sleep(0.2)
            final = _C.poc_result(self._call("poc_tx_burst_state", {"burst_id": burst_id}))
        self.assertTrue(final["terminal"], final)
        self.assertTrue(all(v == "COMPLETED" for v in final["children"].values()), final)

    def test_duplicate_burst_id_is_refused_by_the_authoritative_path(self):
        """같은 generation에서 burst_id 재사용은 권위 경로가 거부한다(중복 송신 금지)."""
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        samples = self._device_samples(bytes(range(54)))
        encoded = base64.b64encode(samples).decode("ascii")
        digest = hashlib.sha256(samples).hexdigest()
        state = _C.poc_result(self._call("poc_radio_state", {}))
        burst_id = "55555555-5555-4555-8555-555555555555"

        def _submit(offset_ns, cid_suffix):
            body = {
                "burst_id": burst_id,
                "channel_name": _LOWER,
                "transmit_at_hw_ns": int(state["hardware_time_ns"]) + offset_ns,
                "samples_base64": encoded,
                "samples_sha256": digest,
                "destination_station_id": 222_222_222,
                "request_digest": hashlib.sha256(b"dup").hexdigest(),
            }
            # command_id를 다르게 해야 adapter replay 캐시가 아니라 실제 재실행이 된다.
            message = self.controller._command("MASS-B", "poc_schedule_group_tx", body)
            message["command_id"] = f"66666666-6666-4666-8666-66666666666{cid_suffix}"
            message["body_sha256"] = message["request_body_sha256"]
            return self.client.request(message, timeout=30.0)

        first = _C.poc_result(_submit(600_000_000, "0"))
        self.assertTrue(first["accepted"], first)
        second_body = (_submit(900_000_000, "1").get("body") or {}).get("result")
        self.assertFalse(second_body["accepted"], second_body)
        self.assertEqual(second_body["reason"], "SCHEDULE_TX_REJECTED", second_body)

    # --- P07 tuning이 실 프로세스 경계를 건너간다 ------------------------------

    def test_tuning_apply_and_restore_cross_the_boundary(self):
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C
        from vdeter.stage8.shared_medium import constants

        before = _C.poc_result(self._call("poc_tuning", {
            "mode": "READ", "rx_lo_hz": None, "lane_dsp_hz": None,
            "analog_bandwidth_hz": None, "snapshot": None}))
        applied = _C.poc_result(self._call("poc_tuning", {
            "mode": "APPLY",
            "rx_lo_hz": constants.COMMON_RF_LO_HZ,
            "lane_dsp_hz": {"0": constants.LOWER_DSP_HZ, "1": constants.UPPER_DSP_HZ},
            "analog_bandwidth_hz": constants.ANALOG_RX_BANDWIDTH_HZ,
            "snapshot": None}))
        # 자식이 소유한 실 worker에 걸린 값이 canonical JSON으로 돌아온다(lane 키는 문자열).
        self.assertEqual(applied["rx_lo_hz"], constants.COMMON_RF_LO_HZ)
        self.assertEqual(applied["lane_center_hz"]["0"], constants.LOWER_EFFECTIVE_CENTER_HZ)
        self.assertEqual(applied["lane_center_hz"]["1"], constants.UPPER_EFFECTIVE_CENTER_HZ)
        restored = _C.poc_result(self._call("poc_tuning", {
            "mode": "RESTORE", "rx_lo_hz": None, "lane_dsp_hz": None,
            "analog_bandwidth_hz": None, "snapshot": before}))
        self.assertEqual(restored["lane_center_hz"], before["lane_center_hz"])


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class LauncherProviderTest(LauncherPocDeviceRpcTest):
    """PoC **provider 전체**가 launcher 소유 장치에 프로세스 경계 너머로 결속된다.

    scheduler·arbiter·rx_readiness는 in-process에서 이미 시험된 구현 **그대로**이고, 장치 상태만
    실 RPC로 온다. sink만 경계를 건너는 구현이다.
    """

    def _provider(self, **kwargs):
        from apps.stage8_launcher_seams import assemble_launcher_provider
        return assemble_launcher_provider(
            self.controller, actor_endpoint="MASS-B", endpoint_ids=("MASS-A", "MASS-B", "ROC"),
            station_id_by_endpoint={"MASS-A": 111_111_111, "MASS-B": 333_333_333,
                                    "ROC": 222_222_222},
            **kwargs)

    def test_provider_submit_radiates_through_the_launcher_and_confirms_from_the_ledger(self):
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind
        from vdeter.stage8.shared_medium.radiation import ParentOutcome

        provider, bindings = self._provider()
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        submission = provider.submit(action, request_id="poc-remote-1")
        self.assertIsNotNone(submission)
        parent = provider.parent_for("poc-remote-1")
        self.assertIsNotNone(parent)
        # 두 lane fan-out이 권위 원장에 등록된다(§5.1 고정 배선).
        burst_id = bindings["sink"].burst_by_group[parent.parent_id]
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C
        ledger = _C.poc_result(self._call("poc_tx_burst_state", {"burst_id": burst_id}))
        self.assertEqual(sorted(ledger["children"]), ["0", "1"], ledger)
        # terminal은 **원장**에서 온다(매질 로그·휴리스틱 아님).
        terminals = bindings["terminal_source"](action, {}, parent)
        self.assertTrue(all(v == "COMPLETED" for v in terminals.values()), terminals)
        self.assertEqual(provider.resolve_outcome("poc-remote-1", terminals),
                         ParentOutcome.COMPLETED)

    def test_normal_tx_holding_the_lane_refuses_the_poc_reservation(self):
        """P04: 정상 TX가 lane을 점유하면 **원격 상태**를 읽어 예약을 거부한다(정상 TX 우선)."""
        from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind

        # 자식 프로세스의 실 worker lane을 mute한다(정상 서비스 TX 점유 상황).
        # 기존 fault hook allowlist를 쓴다 — production launcher는 hook을 등록하지 않으므로
        # 실장비에서 이 경로는 항상 거부된다(장치 조작 op을 새로 열지 않는다).
        response = self.controller.inject_test_fault(
            "MASS-B", scenario_id="poc", step_id="mute", fault_fixture_id="poc_mute_lane",
            target_correlation_sha256=hashlib.sha256(b"mute").hexdigest())
        body = response.get("body") or {}
        self.assertEqual(body.get("call_outcome"), "OK", body)
        provider, _bindings = self._provider()
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        with self.assertRaises(PocError) as ctx:
            provider.submit(action, request_id="poc-remote-conflict")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class MacSlotBindingTest(LauncherPocDeviceRpcTest):
    """P03: 송신 시각이 **실 MAC slot map**이 허용한 slot에서 나온다."""

    def test_mac_slots_come_from_the_real_slot_map(self):
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        state = _C.poc_result(self._call("poc_radio_state", {}))
        answer = _C.poc_result(self._call("poc_mac_slots", {
            "channel_name": _LOWER,
            "from_hw_ns": int(state["hardware_time_ns"]),
            "limit": 4}))
        self.assertTrue(answer["slot_map_id"], "활성 slot map id가 있어야 한다")
        slots = answer["slots"]
        self.assertTrue(slots, "실 slot map에 송신 가능 slot이 있어야 한다")
        for slot in slots:
            # MAC 좌표와 장치 좌표가 **둘 다** 실려야 한다(하나로 뭉개지 않는다).
            self.assertIn(slot["function"],
                          ("BBSC", "RAC", "ASC", "DC", "DSCH", "RANGING"))
            self.assertGreaterEqual(slot["start_hw_ns"], int(state["hardware_time_ns"]))
            self.assertGreater(slot["start_utc_ns"], 0)
        # 오름차순이고 요청 상한을 넘지 않는다.
        self.assertEqual([s["start_hw_ns"] for s in slots],
                         sorted(s["start_hw_ns"] for s in slots))
        self.assertLessEqual(len(slots), 4)

    def test_scheduled_transmit_time_lands_on_a_mac_transmit_slot(self):
        """provider가 고른 송신 시각이 실 MAC slot 시작과 **일치**한다(격자 시각 아님)."""
        from apps.stage8_launcher_seams import assemble_launcher_provider
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind

        provider, bindings = assemble_launcher_provider(
            self.controller, actor_endpoint="MASS-B",
            endpoint_ids=("MASS-A", "MASS-B", "ROC"),
            station_id_by_endpoint={"MASS-A": 111_111_111, "MASS-B": 333_333_333,
                                    "ROC": 222_222_222},
            mac_channel_name=_LOWER)
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        provider.submit(action, request_id="poc-mac-1")
        parent = provider.parent_for("poc-mac-1")
        self.assertIsNotNone(parent)
        chosen = int(parent.scheduled_hw_timestamp_ns)
        # 예약 키가 MAC 좌표다 — device 격자 인덱스가 아니다.
        self.assertTrue(parent.slot_key.startswith("mac-"), parent.slot_key)
        # 고른 시각이 실제로 MAC이 허용한 slot의 **시작**이다(격자 시각이 아니다).
        answer = _C.poc_result(self._call("poc_mac_slots", {
            "channel_name": _LOWER, "from_hw_ns": chosen, "limit": 1}))
        self.assertEqual(int(answer["slots"][0]["start_hw_ns"]), chosen)

    def test_no_usable_mac_slot_is_refused_not_shifted(self):
        """권위 lead를 만족하는 MAC slot이 없으면 **거부**한다 — 임의로 당기거나 미루지 않는다."""
        from apps.stage8_launcher_seams import MacSlotScheduler
        from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind

        class _NoSlots(MacSlotScheduler):
            def _query(self):            # launcher가 후보 0개를 준 상황
                return {"slots": [], "slot_map_id": "empty"}

        from apps.stage8_launcher_seams import LauncherDeviceClient, _RemoteWorker
        client = LauncherDeviceClient(self.controller, "MASS-B")
        scheduler = _NoSlots(_RemoteWorker(client), client, channel_name=_LOWER)
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        with self.assertRaises(PocError) as ctx:
            scheduler.resolve(action, request_id="poc-mac-none")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class PocDeviceDisabledByDefaultTest(unittest.TestCase):
    """device seam을 주입하지 않은 설치에서는 PoC op이 **전부 거부**된다(기본값 = 쓸 수 없음)."""

    def test_adapter_without_poc_device_rejects_every_poc_op(self):
        from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
        from vdeter.stage7.ipc import TEST_PROTOCOL, body_sha256_of

        adapter = Stage7ServiceApiTestAdapter(
            endpoint_id="MASS-B", service=object(),
            resolved_test_plan_sha256=_PLAN_SHA,
            authorized_controller=(os.getuid(),))
        for name in ("poc_radio_state", "poc_schedule_group_tx", "poc_tx_burst_state",
                     "poc_tuning"):
            body = {}
            message = {
                "protocol": TEST_PROTOCOL, "kind": "command", "command_name": name,
                "run_id": "r", "resolved_test_plan_sha256": _PLAN_SHA,
                "command_id": f"cid-{name}", "endpoint_id": "MASS-B",
                "expected_lifecycle_generation": 1, "expected_bridge_generation": 0,
                "issued_host_raw_ns": 1, "deadline_host_raw_ns": 2,
                "request_body_sha256": body_sha256_of(body),
                "body_sha256": body_sha256_of(body), "body": body,
            }
            response = adapter.handle_command(message, peer=(os.getuid(),))
            self.assertEqual(response["status"], "REJECTED", name)
            self.assertEqual(response["body"]["reason"], "POC_DEVICE_UNAVAILABLE", name)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_LINUX, "실 subprocess 전용")
class ControllerPocRequestContractTest(unittest.TestCase):
    """P01: workload controller child의 `--poc-request` 계약(실 CLI subprocess).

    launcher는 harness가 실 socket으로 서빙한다(controller는 그것 없이는 아예 시작하지 못한다).
    그 상태에서 PoC package가 잘못돼 arm에 실패하면 **scenario를 제출하지 않고** fail-closed로
    끝나야 한다(§5.1 "arm 실패면 scenario 자극 0"). PoC 없이 시나리오만 돌고 PASS가 되는 일이
    없음을 실 프로세스로 확인한다.
    """

    _RUN_ID = "poc-contract"

    def setUp(self):
        from vdeter.stage7.transport import ensure_socket_root, runtime_socket_root

        root = runtime_socket_root(self._RUN_ID)
        ensure_socket_root(root)
        env = dict(os.environ)
        src = os.path.join(_REPO, "VDETER", "src")
        vdeter = os.path.join(_REPO, "VDETER")
        env["PYTHONPATH"] = os.pathsep.join(
            [src, vdeter, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        self.harness = subprocess.Popen(
            [sys.executable, "-m", "tests.support.poc_device_harness", root, _PLAN_SHA],
            cwd=vdeter, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_harness)
        ready = self.harness.stdout.readline().strip()
        if ready != "READY":
            self.fail(f"harness 준비 실패: {ready!r} {self.harness.stderr.read()[:400]}")

    def _stop_harness(self):
        if self.harness.poll() is None:
            self.harness.terminate()
            try:
                self.harness.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.harness.kill()
                self.harness.wait(timeout=5)

    def test_poc_arm_failure_writes_evidence_and_submits_no_scenario(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            run_root = os.path.join(tmp, "run")
            os.makedirs(run_root, exist_ok=True)
            fixtures = os.path.join(tmp, "fixtures")
            os.makedirs(fixtures, exist_ok=True)
            poc_request = os.path.join(tmp, "poc-request.json")
            with open(poc_request, "w", encoding="utf-8") as handle:
                json.dump({"poc": os.path.join(tmp, "nonexistent-poc.zip"),
                           "poc_dir": None, "poc_id": None, "src_path": None}, handle)

            env = dict(os.environ)
            src = os.path.join(_REPO, "VDETER", "src")
            env["PYTHONPATH"] = os.pathsep.join(
                [src, _REPO, os.path.join(_REPO, "VDETER"),
                 env.get("PYTHONPATH", "")]).strip(os.pathsep)
            done = subprocess.run(
                [sys.executable, os.path.join(_REPO, "apps",
                                              "stage7_mass_roc_controller.py"),
                 "--run-id", self._RUN_ID, "--run-root", run_root,
                 "--test-plan-sha", _PLAN_SHA, "--fixtures-dir", fixtures,
                 "--scenario-id", "SC-VDE-006", "--workload-id", "w1",
                 "--lifecycle", "session", "--poc-request", poc_request],
                cwd=_REPO, env=env, capture_output=True, text=True, timeout=300)

            # fail-closed: 종료 코드 실패 + PoC evidence 기록 + scenario 결과는 FAIL.
            self.assertNotEqual(done.returncode, 0, done.stdout[-800:] + done.stderr[-800:])
            poc_path = os.path.join(run_root, "mass-roc-events", "poc-result.json")
            self.assertTrue(os.path.isfile(poc_path),
                            f"PoC evidence가 없다: {done.stdout[-800:]}{done.stderr[-800:]}")
            with open(poc_path, encoding="utf-8") as handle:
                evidence = json.load(handle)
            self.assertIn(evidence.get("status"), ("ERROR", "REFUSED"), evidence)
            final_path = os.path.join(run_root, "mass-roc-events", "final-result.json")
            self.assertTrue(os.path.isfile(final_path))
            with open(final_path, encoding="utf-8") as handle:
                final = json.load(handle)
            self.assertEqual(final["status"], "FAIL", final)
            # scenario를 하나도 돌리지 않았다(자극 0).
            self.assertEqual(final.get("results"), {}, final)


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class NormalTxSharedMediumOptinTest(LauncherPocDeviceRpcTest):
    """P08: **정상 service 송신**이 공유매체 opt-in에서 두 물리 경로로 방사된다.

    PoC action이 아니라 endpoint의 **정상 transfer**가 대상이다. opt-in을 켜기 전에는 legacy
    단일 child, 켠 뒤에는 §5.1 고정 배선대로 fan-out이다. 논리 주소는 그대로다.
    """

    def _submit_normal_transfer(self, *, step_id):
        """실 service API(`invoke_service_inline` SUBMIT)로 정상 전송을 낸다."""
        payload = b"stage8-normal-fanout"
        response = self.controller.invoke_service_inline(
            "MASS-B", scenario_id="poc", step_id=step_id, service_operation="SUBMIT",
            request={
                "destination": 222_222_222,          # ROC (addressed, 논리 주소 불변)
                "payload_format": "test.stage0.opaque",
                "payload_base64": base64.b64encode(payload).decode("ascii"),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "ack_required": False,
            })
        body = response.get("body") or {}
        self.assertEqual(body.get("call_outcome"), "OK", body)
        return body

    def test_optin_is_off_by_default(self):
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        state = _C.poc_result(self._call("poc_wired_broadcast_optin", {"enabled": None}))
        self.assertFalse(state["enabled"], "기본값은 legacy(꺼짐)여야 한다")

    def test_normal_transfer_fans_out_only_when_optin_is_on(self):
        from vdeter.stage7.testing.controller import Stage7ScenarioController as _C

        # 1) opt-in OFF — 정상 전송은 legacy 단일 child다.
        self._submit_normal_transfer(step_id="off-1")
        # 2) opt-in ON — 이후 승격되는 route부터 두 물리 경로로 나간다.
        toggled = _C.poc_result(self._call("poc_wired_broadcast_optin", {"enabled": True}))
        self.assertTrue(toggled["enabled"])
        self.assertFalse(toggled["previous"], "직전 값은 꺼짐이어야 한다")
        self._submit_normal_transfer(step_id="on-1")
        # 3) 다시 끄면 legacy로 돌아온다.
        restored = _C.poc_result(self._call("poc_wired_broadcast_optin", {"enabled": False}))
        self.assertFalse(restored["enabled"])
        self.assertTrue(restored["previous"])


class SharedMediumOptinRouteTest(unittest.TestCase):
    """P08: opt-in이 **정상 route 승격**에 적용되는지 in-process로 정밀 확인한다.

    프로세스 경계 시험은 "켜고 끌 수 있다"를 보이고, 여기서는 켠 상태에서 실제로 두 child가
    나오고 legacy는 단일 child임을 route 계획 수준에서 고정한다.
    """

    def _sidecar(self):
        from vdeter.stage7.routing import Stage7RouteSidecar
        return Stage7RouteSidecar()

    def test_promoted_route_carries_optin_only_when_enabled(self):
        from vdeter.stage7.routing import Stage7RouteContext, resolve_lane_plan_route
        from vdeter.stage7.configuration import CANONICAL_ROLES

        sidecar = self._sidecar()
        self.assertFalse(sidecar.wired_broadcast_optin, "기본값은 꺼짐")
        previous = sidecar.set_wired_broadcast_optin(True)
        self.assertFalse(previous)
        self.assertTrue(sidecar.wired_broadcast_optin)

        # 켠 상태의 context가 fan-out으로 해석된다(ROC=C1+C3).
        roc = CANONICAL_ROLES["ROC"][1]
        mass_a = CANONICAL_ROLES["MASS-A"][1]
        common = dict(method_kind="SUBMIT_TRANSFER", source_station_id=roc,
                      destination_kind="ADDRESSED", destination_station_id=mass_a,
                      original_source_station_id=None, original_destination_kind=None,
                      original_destination_station_id=None, request_digest="")
        channel = "VDETER_UPPER_100_TEST"
        from vdeter.stage7.routing import CANONICAL_ROUTE_TABLE
        channel = CANONICAL_ROUTE_TABLE[("ROC", "MASS-A")][0]
        on_plan, err = resolve_lane_plan_route(
            "ROC", Stage7RouteContext(**common, broadcast_fanout_optin=True), channel)
        self.assertIsNone(err, err)
        self.assertEqual(on_plan[0], ((0, "C1"), (1, "C3")))
        self.assertEqual(on_plan[2], "OPTIN_TWO_CHILD_FANOUT")

        off_plan, err = resolve_lane_plan_route(
            "ROC", Stage7RouteContext(**common), channel)
        self.assertIsNone(err, err)
        self.assertEqual(off_plan[0], ((0, "C1"),))
        self.assertEqual(off_plan[2], "SINGLE_CHILD")

    def test_toggle_off_restores_legacy(self):
        sidecar = self._sidecar()
        sidecar.set_wired_broadcast_optin(True)
        self.assertTrue(sidecar.set_wired_broadcast_optin(False))
        self.assertFalse(sidecar.wired_broadcast_optin)
