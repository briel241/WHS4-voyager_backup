"""Stage 8 C 실패 경계: 방사 전 **명시적 거부**가 불명확(UNKNOWN)으로 승격되지 않는다.

실물 C(`diag-20260914-025521`)에서 PoC action 하나가 `TX_UNKNOWN`으로 남고 세션이
`SESSION_INHIBIT`로 종료했다. 보존 evidence의 `command_log`는 그 op을
`status=COMPLETED / call_outcome=OK / reason=None`으로 적었는데, 이는 TEST adapter가 op이
정상 반환하면 **항상** 쓰는 봉투 값이고(`adapter.py`), 장치의 거부 판정은 `result` 안에만
있었다. 그 결과 두 가지가 겹쳤다.

1. `commit_group`이 거부를 평범한 `RuntimeError`로 올려, "commit 진입 후 모든 Exception은
   OUTCOME_UNKNOWN"이라는 방사 계층 규칙에 걸렸다 — 방사 0이 확정인데도 불명확이 됐다.
2. 거부 사유가 evidence 어디에도 남지 않아 사후에 원인을 가릴 수 없었다.

여기서는 장치 없이(RF-off) 그 경계만 직접 구동한다. 규칙을 넓히지 않는다: `accepted` 판정을
읽을 수 없는 응답은 여전히 불명확으로 남아야 한다.
"""

from __future__ import annotations

import unittest

from apps.stage8_launcher_seams import LauncherRadioSink
from vdeter.stage7.testing.controller import Stage7ScenarioController
from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium.radiation import BroadcastCommitRefused, ChildPath

_STATIONS = {"MASS-A": 111_111_111, "ROC": 222_222_222}


class _FakeClient:
    """`LauncherDeviceClient` 자리에 들어가는 최소 대역. 판정하지 않고 준비된 응답만 돌려준다."""

    def __init__(self, result) -> None:
        self._result = result
        self.calls: list = []
        self.invalidated = 0

    def call(self, command_name: str, body: dict):
        self.calls.append((command_name, body))
        return self._result

    def invalidate(self) -> None:
        self.invalidated += 1


def _children(rx_station="MASS-A"):
    samples = (0j,) * 8
    return tuple(
        ChildPath(child_id=f"p:{cable}", parent_id="p", transfer_id="t", cable=cable,
                  tx_station="MASS-B", tx_lane=lane, rx_station=rx_station, rx_lane=1,
                  channel=PhysicalChannel.UPPER, payload_digest="d" * 64,
                  waveform_sha256="w" * 64, scheduled_sample=0,
                  scheduled_hw_timestamp_ns=5_000_000_000, samples=samples,
                  wire_packet_digest="a" * 64)
        for cable, lane in (("C4", 0), ("C6", 1))
    )


def _sink(result):
    client = _FakeClient(result)
    sink = LauncherRadioSink(client, channel_name_of=lambda channel: channel.value,
                             station_id_by_endpoint=_STATIONS)
    return sink, client


def _commit(sink, rx_station="MASS-A"):
    admission = sink.prepare_group(group_id="p", children=_children(rx_station))
    sink.commit_group(admission)


class LauncherCommitRefusalTest(unittest.TestCase):
    """실물 C 조건 재현: 권위 `schedule_tx`가 거부를 **회신**한 경우."""

    def test_schedule_tx_rejection_is_a_refusal_not_an_unknown(self):
        # 실물 C에서 일어날 수 있었던 두 거부 중 하나. 방사 전 거부이므로 불명확이 아니다.
        sink, client = _sink({"accepted": False, "burst_id": "b",
                              "reason": "SCHEDULE_TX_REJECTED", "detail": "LEAD_TOO_SHORT"})
        with self.assertRaises(BroadcastCommitRefused) as ctx:
            _commit(sink)
        self.assertIn("SCHEDULE_TX_REJECTED", str(ctx.exception))
        self.assertIn("LEAD_TOO_SHORT", str(ctx.exception))
        self.assertEqual(client.invalidated, 1)      # 거부 뒤에도 장치 상태 캐시는 버린다
        self.assertEqual(sink.receipt_by_group, {})  # 영수증을 만들지 않는다

    def test_route_bind_rejection_is_a_refusal_not_an_unknown(self):
        # 다른 하나. `radio.schedule_tx` **이전**에 막힌 경우로, 역시 방사 0이 확정이다.
        sink, client = _sink({"accepted": False, "burst_id": "b",
                              "reason": "ROUTE_BIND_REJECTED", "detail": "no fanout binding"})
        with self.assertRaises(BroadcastCommitRefused):
            _commit(sink)
        self.assertEqual(client.invalidated, 1)

    def test_unreadable_acceptance_verdict_stays_unknown(self):
        # 인접 negative: `accepted`를 읽을 수 없으면 방사 여부를 모른다 -> 강등하지 않는다.
        for result in ({"burst_id": "b"}, {"accepted": None}, "not-a-mapping"):
            with self.subTest(result=result):
                sink, client = _sink(result)
                with self.assertRaises(RuntimeError) as ctx:
                    _commit(sink)
                self.assertNotIsInstance(ctx.exception, BroadcastCommitRefused)
                self.assertEqual(client.invalidated, 1)

    def test_acceptance_still_commits_and_records_the_receipt(self):
        # 소비자 직접 영향: 정상 수락 경로가 그대로 동작해야 한다.
        sink, client = _sink({"accepted": True, "burst_id": "b",
                              "transmit_at_utc_ns": 1_700_000_000_000_000_000,
                              "reason": None, "detail": None})
        _commit(sink)
        self.assertEqual(client.invalidated, 1)
        receipt = sink.receipt_by_group["p"]
        self.assertEqual(receipt["transmit_at_utc_ns"], 1_700_000_000_000_000_000)
        self.assertEqual(receipt["wire_packet_digest"], "a" * 64)
        self.assertEqual(client.calls[0][0], "poc_schedule_group_tx")


class _FakeChannel:
    """TEST adapter 응답 봉투를 그대로 흉내 낸다(op 정상 반환 시 call_outcome=OK, reason=None)."""

    def __init__(self, result) -> None:
        self._result = result

    def request(self, message: dict) -> dict:
        return {"status": "COMPLETED",
                "body": {"poc_operation": message["command_name"],
                         "call_outcome": "OK", "reason": None, "result": self._result}}


class CommandLogRefusalEvidenceTest(unittest.TestCase):
    """거부 사유가 evidence(`command_log`)에 남는다 — 봉투만 보면 성공과 구별되지 않는다."""

    def _controller(self, result):
        return Stage7ScenarioController(
            run_id="run-1", resolved_test_plan_sha256="0" * 64,
            adapter_channels={"MASS-B": _FakeChannel(result)},
            host_raw_clock=lambda: 1_000_000_000,
        )

    def test_device_refusal_is_recorded_with_its_reason(self):
        controller = self._controller(
            {"accepted": False, "burst_id": "b",
             "reason": "SCHEDULE_TX_REJECTED", "detail": "LEAD_TOO_SHORT"})
        controller.poc_command("MASS-B", "poc_schedule_group_tx", {})
        entry = controller.command_log[-1]
        # 봉투는 성공한 op과 완전히 같다 — 이것이 실물 C에서 원인을 가릴 수 없었던 이유다.
        self.assertEqual(entry["call_outcome"], "OK")
        self.assertIsNone(entry["reason"])
        # 장치가 스스로 내린 거부 판정과 사유는 남는다.
        self.assertIs(entry["device_accepted"], False)
        self.assertEqual(entry["device_reason"], "SCHEDULE_TX_REJECTED")
        self.assertEqual(entry["device_detail"], "LEAD_TOO_SHORT")

    def test_acceptance_is_recorded_without_a_refusal_reason(self):
        controller = self._controller(
            {"accepted": True, "burst_id": "b", "reason": None, "detail": None})
        controller.poc_command("MASS-B", "poc_schedule_group_tx", {})
        entry = controller.command_log[-1]
        self.assertIs(entry["device_accepted"], True)
        self.assertNotIn("device_reason", entry)   # 거부가 아닌데 사유를 적지 않는다
        self.assertNotIn("device_detail", entry)

    def test_results_without_an_acceptance_verdict_add_no_device_keys(self):
        # 판정을 담지 않는 op(radio_state 등)의 결과 전체를 복사하지 않는다.
        controller = self._controller({"hardware_time_ns": 42, "tx_muted": False})
        controller.poc_command("MASS-B", "poc_radio_state", {})
        entry = controller.command_log[-1]
        self.assertNotIn("device_accepted", entry)
        self.assertNotIn("device_reason", entry)
        self.assertEqual(entry["command"], "poc_radio_state")


class _FakeRunner:
    def __init__(self) -> None:
        self.delivered: list = []

    def deliver(self, step) -> None:
        self.delivered.append(step)


class _FakeConnection:
    def __init__(self, error=None) -> None:
        self.infrastructure_error = error


class ObservationDeliveryCountTest(unittest.TestCase):
    """전달 수는 **실제로 전달된 수**여야 한다 — 건네준 수가 아니다.

    실물 C의 trace는 `poc_observations_delivered count=16`을 적었지만 engine이 event로 낸
    관측은 1건뿐이었다(`events_emitted=2`, `delivery_matrix` 1행). infrastructure 실패가 선
    뒤의 관측은 전달되지 않는데 호출자가 무조건 세고 있었기 때문이다.
    """

    def _workload(self, error=None):
        from vdeter.stage8.integration.entry import PocWorkload

        runner = _FakeRunner()
        return PocWorkload(runner, _FakeConnection(error), None, None, ()), runner

    def test_successful_delivery_reports_true(self):
        from vdeter.stage8.testing.fakes import make_observation

        workload, runner = self._workload()
        self.assertIs(workload.deliver_observation(make_observation()), True)
        self.assertEqual(len(runner.delivered), 1)
        self.assertEqual(runner.delivered[0].kind, "observation")

    def test_after_infrastructure_failure_nothing_is_delivered_or_counted(self):
        from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
        from vdeter.stage8.testing.fakes import make_observation

        failure = PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN, "commit unconfirmed")
        workload, runner = self._workload(failure)
        self.assertIs(workload.deliver_observation(make_observation()), False)
        self.assertEqual(runner.delivered, [])   # 전달하지 않았다 — 세어서도 안 된다


if __name__ == "__main__":
    unittest.main()
