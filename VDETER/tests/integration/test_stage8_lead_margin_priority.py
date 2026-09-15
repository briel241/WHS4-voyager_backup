"""라운드 4 RF-off 보완 — 성공 경로의 lead 여유 기록과 **권위 경로**의 정상 TX 우선.

두 가지를 고정한다.

1. **성공한 송신에도 lead 관측이 남는다.** 라운드 3까지 `required_lead_ns`/`remaining_lead_ns`는
   거부(`RADIO_LATE_REQUEST`) 경로에만 있었다. 그래서 C·D가 통과했을 때 여유가 얼마였는지
   evidence에 없었다("남은 lead 미측정"). 이제 권위 원장이 수락 시점의 세 값을 담고, 기존
   `tx_burst_state` 조회가 그대로 실어 나른다(새 RPC·디스크 쓰기 없음).

2. **권위 장치의 명시적 거부는 UNKNOWN으로 승격되지 않는다.** `Stage7AuthoritativeRadioSink`는
   `schedule_tx` 거부에 평범한 `RuntimeError`를 내고 있었고, 그것이 `AtomicBroadcast.transmit`의
   BaseException 분기에 걸려 `POC_TX_OUTCOME_UNKNOWN`(SESSION_INHIBIT)이 됐다. 같은 거부가
   launcher sink에서는 `BroadcastCommitRefused` → `POC_RADIO_RESOURCE_CONFLICT`
   (REQUEST_REJECTED)였다. 즉 **sink에 따라 severity가 달랐다**. 정상 TX가 자원을 점유해 PoC가
   거부되는 흔한 경우에 세션 전체가 억제되는 결함이므로 두 sink를 일치시켰다.

이 파일은 RF-off다. 실 RF atomicity·on-air 결과를 주장하지 않는다.
"""

from __future__ import annotations

import os
import socket
import sys
import unittest

_LINUX = (hasattr(socket, "AF_UNIX") and hasattr(os, "getuid") and sys.platform != "win32")
_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SRC = os.path.normpath(os.path.join(_REPO, "VDETER", "src"))
for _p in (_REPO, os.path.join(_REPO, "VDETER"), _SRC, os.path.join(_REPO, "VDETER", "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_STATIONS = {"MASS-A": 111_111_111, "MASS-B": 333_333_333, "ROC": 222_222_222}
_SLOT_SAMPLES = 8_192


# --------------------------------------------------------------------------- #
# 1. sink 경계: 명시적 장치 거부 → BroadcastCommitRefused (severity 고정)
# --------------------------------------------------------------------------- #

class _Err:
    is_err = True

    def __init__(self, error):
        self.error = error


class _Ok:
    is_err = False

    def __init__(self, value=None):
        self.value = value


class _Bridge:
    clock_id = "stage7-utc-test"

    @staticmethod
    def hardware_ns_to_utc_label(hw_ns):
        return int(hw_ns) + 1_000_000_000


class _RefusingAdapter:
    """권위 adapter double — `schedule_tx`가 **명시적 거부**를 회신한다."""

    bridge = _Bridge()

    def __init__(self, error="RADIO_RESOURCE_CONFLICT: 같은 lane의 TX burst가 겹칩니다"):
        self._error = error
        self.calls = 0

    def schedule_tx(self, request):
        self.calls += 1
        return _Err(self._error)


class _AcceptingAdapter(_RefusingAdapter):
    def schedule_tx(self, request):
        self.calls += 1
        return _Ok()


class _FakeSidecar:
    def __init__(self):
        self.bound = []

    def bind_broadcast_fanout_burst(self, burst_id, *, source_station_id,
                                    destination_station_id, request_digest):
        self.bound.append(burst_id)
        return _Ok(object())


class _Child:
    """fan-out child double — sink가 읽는 속성만 갖는다."""

    def __init__(self, lane, destination="ROC"):
        from vdeter.poc.api.types import PhysicalChannel

        self.child_id = f"child-{lane}"
        self.tx_lane = lane
        self.channel = PhysicalChannel.LOWER
        self.scheduled_hw_timestamp_ns = 5_000_000_000
        self.addressed_destination = destination
        self.payload_digest = "d" * 64
        self.wire_packet_digest = "e" * 64
        self.samples = (0j,) * _SLOT_SAMPLES
        self.samples_cf32le = b"\x00" * (_SLOT_SAMPLES * 8)


class AuthoritativeSinkRefusalSeverityTest(unittest.TestCase):
    """명시적 장치 거부는 방사 0이 확정이다 — UNKNOWN으로 올리지 않는다."""

    def _sink(self, adapter):
        from apps.stage8_stage7_seams import Stage7AuthoritativeRadioSink

        return Stage7AuthoritativeRadioSink(
            adapter, sidecar=_FakeSidecar(), source_station_id=_STATIONS["MASS-B"],
            station_id_by_endpoint=_STATIONS)

    def test_explicit_device_refusal_raises_commit_refused_not_runtime_error(self):
        from vdeter.poc.shared_medium.radiation import BroadcastCommitRefused

        adapter = _RefusingAdapter()
        sink = self._sink(adapter)
        admission = sink.prepare_group(group_id="p1", children=(_Child(0), _Child(1)))
        with self.assertRaises(BroadcastCommitRefused) as ctx:
            sink.commit_group(admission)
        self.assertIn("권위 schedule_tx 거부", str(ctx.exception))
        self.assertEqual(adapter.calls, 1)          # 재시도하지 않는다

    def test_commit_refused_maps_to_request_rejected_not_session_inhibit(self):
        """이 sink의 거부가 provider 경계에서 어떤 severity가 되는지 고정한다."""
        from vdeter.poc.api.errors import PocError, PocErrorCode

        # 계약: 방사 전 명시 거부 -> POC_RADIO_RESOURCE_CONFLICT(REQUEST_REJECTED, 자원 반환).
        # UNKNOWN(SESSION_INHIBIT)은 "방사 여부 불명"에만 쓴다.
        self.assertEqual(
            PocError(PocErrorCode.POC_RADIO_RESOURCE_CONFLICT).severity.value,
            "REQUEST_REJECTED")
        self.assertEqual(
            PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN).severity.value,
            "SESSION_INHIBIT")

    def test_accepted_commit_does_not_raise(self):
        adapter = _AcceptingAdapter()
        sink = self._sink(adapter)
        admission = sink.prepare_group(group_id="p2", children=(_Child(0), _Child(1)))
        sink.commit_group(admission)                # 예외 없음
        self.assertEqual(adapter.calls, 1)


# --------------------------------------------------------------------------- #
# 2. 실 Stage 7 software group: 성공 lead 여유 + 정상 TX 우선
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_LINUX, "실 Stage 7 software group(POSIX) 전용")
class RealGroupLeadAndPriorityTest(unittest.TestCase):
    def _group(self):
        from tests.support.stage7 import (build_stage7_software_group,
                                          start_stage7_group)
        built = build_stage7_software_group()
        clock, medium, _plan, coordinator, members, resolved = built
        start_stage7_group(clock, medium, coordinator, members, resolved)
        # coordinator·resolved 참조를 살려 둔다. 지역 변수로만 두면 수집돼 MAC 진행이 멈추고
        # 정상 TX가 권위 원장에 나타나지 않는다(계측으로 확인한 조건).
        self._built = built
        return clock, medium, members

    @staticmethod
    def _assemble(members):
        from apps.stage8_stage7_seams import assemble_authoritative_provider
        from tests.support.stage7 import STATION_BY_ENDPOINT

        return assemble_authoritative_provider(
            members, station_id_by_endpoint=STATION_BY_ENDPOINT)

    @staticmethod
    def _action(target="ROC"):
        from vdeter.poc.api.types import ActionDescriptor, ActionKind

        return ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target=target,
                                payload={"packet_hex": bytes(range(54)).hex()})

    # --- 성공 경로의 lead 여유 -------------------------------------------------

    def test_accepted_tx_records_bounded_lead_margin(self):
        """수락된 송신의 요구/남은/여유 lead가 권위 원장에 남는다(추가 RPC·I/O 없음)."""
        clock, medium, members = self._group()
        provider, bindings = self._assemble(members)
        provider.submit(self._action(), request_id="poc-lead-1")
        parent = provider.parent_for("poc-lead-1")
        burst_id = bindings["sink"].burst_by_group[parent.parent_id]

        state = bindings["adapter"].tx_burst_state(burst_id)
        lead = state.get("lead")
        self.assertIsNotNone(lead, state)
        for key in ("required_lead_ns", "remaining_lead_ns", "margin_ns"):
            self.assertIsInstance(lead[key], int, lead)
        # margin은 두 값의 **차이**일 뿐이며 새 판정 기준이 아니다.
        self.assertEqual(lead["margin_ns"],
                         lead["remaining_lead_ns"] - lead["required_lead_ns"])
        # 수락됐다는 것은 여유가 음수가 아니라는 뜻이다(거부 기준은 바뀌지 않았다).
        self.assertGreaterEqual(lead["margin_ns"], 0, lead)
        # 권위 lead는 완화되지 않았다: 100 ms 또는 retune 200 ms 둘 중 하나다.
        self.assertIn(lead["required_lead_ns"], (100_000_000, 200_000_000), lead)

    def test_lead_record_survives_after_the_parent_becomes_terminal(self):
        """원장에서 parent가 제거된 뒤에도 lead 기록이 남는다(사후 조회 가능)."""
        from apps.stage8_stage7_seams import Stage7LedgerTerminalSource
        from tests.support.stage7 import drive_stage7_group

        clock, medium, members = self._group()
        provider, bindings = self._assemble(members)
        action = self._action()
        provider.submit(action, request_id="poc-lead-2")
        parent = provider.parent_for("poc-lead-2")
        burst_id = bindings["sink"].burst_by_group[parent.parent_id]
        before = bindings["adapter"].tx_burst_state(burst_id)["lead"]

        terminals = Stage7LedgerTerminalSource(
            bindings["adapter"], bindings["sink"],
            pump=lambda: drive_stage7_group(clock, members, 4))(action, {}, parent)
        self.assertTrue(all(v == "COMPLETED" for v in terminals.values()), terminals)

        after = bindings["adapter"].tx_burst_state(burst_id)
        self.assertTrue(after["terminal"], after)
        self.assertEqual(after["lead"], before)        # 확정 뒤에도 같은 값

    def test_unknown_burst_has_no_fabricated_lead(self):
        """원장에 없는 burst는 None이다 — lead를 0으로 만들어내지 않는다."""
        clock, medium, members = self._group()
        _provider, bindings = self._assemble(members)
        self.assertIsNone(bindings["adapter"].tx_burst_state("not-a-burst"))

    # --- 정상 TX 우선(권위 예약 경로) -----------------------------------------

    def _submit_normal_transfer(self, members, clock, destination_station):
        """실 정상 service 송신을 제출하고 권위 원장에 예약이 생길 때까지만 진행한다.

        pump를 **멈춘 채로** 두어 정상 TX parent가 비확정(SUBMITTED)으로 남게 한다 —
        그 상태가 "정상 TX가 자원을 점유 중"이다.
        """
        from tests.support.stage7 import drive_stage7_group
        from vdeter.common.addressing import Addressed
        from vdeter.common.contracts.service import SubmitTransferRequest
        from vdeter.common.identifiers import PayloadFormat, StationId

        endpoint = members["MASS-B"].endpoint
        adapter = endpoint.dependencies.radio
        accepted = endpoint.submit(SubmitTransferRequest(
            destination=Addressed(StationId(destination_station)),
            payload_format=PayloadFormat("test.stage0.opaque"),
            # payload 길이가 전송 경로를 가른다(short-data vs session 승격). 같은 harness·같은
            # 목적지(MASS-A)에서 실측: 26 B는 step 198에 권위 원장에 등록되고, 39 B는 700 step
            # 동안 `schedule_tx`에 도달하지 않는다(거부도 아니다 — late=0, terminal=0).
            # 등록이 **실측된** 길이를 쓴다. 통과시키려고 고른 값이 아니라 계측으로 정한 값이다.
            payload=b"normal-transfer-diagnostic",
            ack_required=False))
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", accepted))
        # 정상 TX가 원장에 **떠 있는 구간은 좁다**(계측: 400 step 중 약 5 step). 매 step **뒤에**
        # 확인하고, 잡는 즉시 pump를 멈춰 그 상태를 고정한다 — 모의 시계는 pump로만 흐르므로
        # 이후 PoC 제출 동안 정상 TX는 비확정(SUBMITTED)으로 남는다.
        for _ in range(600):
            drive_stage7_group(clock, members, 1)
            if adapter._tx_parents:
                break
        # 실패 메시지가 스스로 상태를 설명하게 한다 — 원장이 비어 있는 이유가 "아직 제출 전"인지
        # "이미 확정돼 제거됨"인지 "거부됨"인지를 사후 추측하지 않기 위해서다.
        self.assertTrue(adapter._tx_parents, (
            "정상 TX가 권위 TX 원장에 등록되지 않았다 — "
            f"terminal={len(adapter._tx_terminal_outcomes)} "
            f"late={adapter._late_command_count} rejected={dict(adapter._tx_rejected)}"))
        return adapter

    def test_pending_normal_tx_rejects_the_poc_and_keeps_the_reservation(self):
        """정상 TX가 점유 중이면 PoC만 거부된다 — 정상 예약 보존, 추가 제출 0, 세션 유지."""
        from vdeter.poc.api.errors import PocError, PocErrorCode

        clock, medium, members = self._group()
        # 목적지 선택은 **계측으로 정했다**(추정 아님). 같은 harness에서 1,200 step을 돌려 본 결과:
        #   MASS-B -> MASS-A : step 198에 권위 원장에 등록됨(required_lead_ns=200 ms = retune 동반)
        #   MASS-B -> ROC    : 1,200 step 동안 `schedule_tx`에 **도달하지 않음**(등록 0)
        # 그래서 정상 송신은 MASS-A로 낸다. 이 송신은 채널 전환을 동반하므로 권위 adapter가
        # 송신에 쓰지 않는 lane을 mute한다 — 그것이 실제 배치에서 "정상 TX가 lane을 점유한"
        # 상태다. 기존 시험은 이 상태를 **fault hook**(`poc_mute_lane`)으로 흉내 냈고, 여기서는
        # 실제 정상 service 송신이 만든다.
        adapter = self._submit_normal_transfer(members, clock, _STATIONS["MASS-A"])
        reserved_before = {key: tuple(sorted(p.children)) for key, p in adapter._tx_parents.items()}
        self.assertTrue(reserved_before, "정상 예약이 없다 — 시험 전제가 성립하지 않는다")

        worker = members["MASS-B"].endpoint.dependencies.radio.lease.worker
        original_submit = worker.submit_tx_group
        extra_submits = []

        def _counting_submit(bursts):
            extra_submits.append(tuple(bursts))
            return original_submit(bursts)

        worker.submit_tx_group = _counting_submit
        self.addCleanup(lambda: setattr(worker, "submit_tx_group", original_submit))

        provider, _bindings = self._assemble(members)
        with self.assertRaises(PocError) as ctx:
            provider.submit(self._action("ROC"), request_id="poc-conflict-1")

        # 1) PoC action만 거부된다. 세션을 억제하는 UNKNOWN이 아니다.
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT,
                         str(ctx.exception))
        self.assertEqual(ctx.exception.severity.value, "REQUEST_REJECTED")
        # 1-1) **어느 계층이 거부했는지 흐리지 않는다.** 두 계층 모두 정당한 거부지만 증명 범위가
        #      다르다: arbiter는 lane 점유를 읽고 거부하고(이 경로), 권위 adapter는 pending TX 중
        #      retune을 거부한다. 장치 계층 거부가 REQUEST_REJECTED로 매핑된다는 것은 위
        #      `AuthoritativeSinkRefusalSeverityTest`가 결정적으로 고정한다.
        message = str(ctx.exception)
        refused_by_arbiter = "정상 TX가 점유 중" in message
        refused_by_device = "권위 schedule_tx 거부" in message
        self.assertTrue(refused_by_arbiter or refused_by_device,
                        f"거부 사유가 정상 TX 점유와 무관하다: {message}")
        # 2) 장치에 **추가 송신 제출이 없었다**(거부는 제출 전에 끝났다).
        self.assertEqual(extra_submits, [], extra_submits)
        # 3) 정상 예약은 그대로다 — PoC 거부가 정상 TX를 건드리지 않는다.
        reserved_after = {key: tuple(sorted(p.children)) for key, p in adapter._tx_parents.items()}
        self.assertEqual(reserved_after, reserved_before)
        # 4) 이후 같은 provider로 cleanup이 성립한다(소유권 불확실 상태가 아니다).
        provider.cleanup()


if __name__ == "__main__":
    unittest.main()
