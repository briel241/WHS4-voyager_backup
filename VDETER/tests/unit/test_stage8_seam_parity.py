"""양 경로 provider seam parity 계약시험(2-2, RF-off·pure).

launcher 경로(controller `LauncherRadioSink` → §16.2 RPC → launcher `PocDeviceSeam.schedule_group_tx`)와
RF-off/direct 경로(`AuthoritativeRadioSink` → adapter 직접 호출)가 **같은** 검증·요청 변환·결과 분류를
내는지 실제 두 경로의 sink/adapter로 비교한다. 의도적으로 다른 계약(route 결속 시점·거부 신호의 형태·
폴링 방식)은 그 차이 자체를 단언한다. 프로세스 경계는 in-process로 잇는다: `client.call` → seam op, op 예외는
실 TEST adapter 봉투처럼 `PocDeviceCallError`. 공통 helper만 시험하지 않고 두 production sink를 통해 검증한다.
플랫폼 무관(실 socket·subprocess 없음).
"""

from __future__ import annotations

import base64
import hashlib
import unittest
from types import SimpleNamespace as NS

from apps.poc_device_seams_launcher import LauncherLedgerTerminalSource, LauncherRadioSink
from apps.poc_device_seams_inprocess import (
    Link11WaveformFactory,
    AuthoritativeRadioSink,
    LedgerTerminalSource,
    child_terminals_from_ledger,
    channel_name,
)
from vdeter.common.errors import Details
from vdeter.common.identifiers import BurstId
from vdeter.common.identifiers import PhysicalChannel as Stage7Channel
from vdeter.common.results import Err, Ok
from vdeter.b210_system.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME
from vdeter.b210_system.control_plane.controller import PocDeviceCallError
from vdeter.b210_system.control_plane.poc_device import PocDeviceUnavailable, PocDeviceSeam
from vdeter.poc.integration.device_boundary import (
    SoftwareGroupTx,
    GroupTxRadioSink,
    logical_destination_station_id,
)
from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.types import (
    TARGET_TO_PHYSICAL_CHANNEL,
    ActionDescriptor,
    ActionKind,
    PhysicalChannel,
)
from vdeter.poc.shared_medium.iq import to_cf32le
from vdeter.poc.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider
from vdeter.poc.shared_medium.radiation import (
    BroadcastCommitRefused,
    ChildPath,
    ChildTerminal,
    ParentOutcome,
    ReadinessDecision,
)

STATIONS = {"MASS-A": 111_111_111, "ROC": 222_222_222, "MASS-B": 333_333_333}
EXPECTED_CHANNEL_NAME = {"MASS-A": UPPER_CHANNEL_NAME, "ROC": LOWER_CHANNEL_NAME}
_UTC_OFFSET_NS = 1_700_000_000_000_000_000
_HW_NS = 5_000_000_000


# ------------------------------------------------------------------ doubles (사실만 나른다)

class _Bridge:
    """host time bridge 대역: 잠겨 있으면 hw→UTC label을 고정 offset으로 돌려준다."""

    def __init__(self, *, locked=True, clock_id="bridge-utc-7"):
        self._locked = locked
        self.clock_id = clock_id
        self.generation = 5

    def hardware_ns_to_utc_label(self, hardware_ns):
        return None if not self._locked else int(hardware_ns) + _UTC_OFFSET_NS


class _DeviceError:
    """권위 adapter가 Result로 돌려주는 구조화 오류의 최소 표면(code·details)."""

    def __init__(self, code, **details):
        self.code = code
        self.details = Details.of(**details)

    def __str__(self):
        return f"{self.code} {self.details.as_dict()}"


class _Sidecar:
    def __init__(self, *, refuse=False):
        self.bound = []
        self._refuse = refuse

    def bind_broadcast_fanout_burst(self, burst_id, *, source_station_id, destination_station_id,
                                    request_digest, **extra):
        self.bound.append({"burst_id": str(burst_id), "source_station_id": int(source_station_id),
                           "destination_station_id": int(destination_station_id),
                           "wire_packet_digest": extra.get("wire_packet_digest")})
        if self._refuse:
            return Err(error=_DeviceError("LANE_ROUTE_INVALID", expected_channel="lower"))
        return Ok(None)


class _Adapter:
    """권위 radio adapter 대역: schedule_tx 요청을 기록하고 mode에 따라 수락/거부/예외/인터럽트."""

    def __init__(self, bridge, sidecar, *, mode="accept"):
        self.bridge = bridge
        self._sidecar = sidecar
        self.mode = mode
        self.requests = []
        self.ledger = {}

    def schedule_tx(self, request):
        self.requests.append(request)
        if self.mode == "accept":
            return Ok(None)
        if self.mode == "refuse":
            return Err(error=_DeviceError("RADIO_LATE_REQUEST", required_lead_ns=200_000_000,
                                          remaining_lead_ns=12_345))
        if self.mode == "raise":
            raise RuntimeError("uhd stream error")
        if self.mode == "interrupt":
            raise KeyboardInterrupt("operator interrupt during commit")
        raise AssertionError(self.mode)

    def tx_burst_state(self, burst_id):
        return self.ledger.get(str(burst_id))


class _InProcessLauncherClient:
    """`LauncherDeviceClient` 자리: RPC 대신 launcher seam op을 같은 프로세스에서 부른다.

    실 TEST adapter 봉투 규칙을 따른다 — op이 예외를 내면 `call_outcome=ERR`이고 supervisor 쪽
    `poc_result`가 `PocDeviceCallError`를 올린다. `interrupt_on`은 supervisor 쪽 대기 중 인터럽트.
    """

    def __init__(self, seam, *, interrupt_on=()):
        self._seam = seam
        self._interrupt_on = tuple(interrupt_on)
        self.endpoint_id = seam.endpoint_id
        self.collision_session_id = None
        self.calls = []
        self.results = []
        self.invalidated = 0

    def call(self, command_name, body):
        self.calls.append((command_name, dict(body)))
        if command_name in self._interrupt_on:
            raise KeyboardInterrupt("operator interrupt while awaiting device op")
        try:
            if command_name == "poc_schedule_group_tx":
                result = self._seam.schedule_group_tx(
                    burst_id=body["burst_id"], channel_name=body["channel_name"],
                    transmit_at_hw_ns=body["transmit_at_hw_ns"],
                    samples=base64.b64decode(body["samples_base64"]),
                    destination_station_id=body["destination_station_id"],
                    request_digest=body["request_digest"],
                    wire_packet_digest=body.get("wire_packet_digest"),
                    collision_session_id=body.get("collision_session_id"))
            elif command_name == "poc_tx_burst_state":
                result = self._seam.tx_burst_state(body["burst_id"])
            else:
                raise AssertionError(f"unexpected op {command_name}")
        except AssertionError:
            raise
        except Exception as error:  # noqa: BLE001 - 봉투 ERR → PocDeviceCallError(실 poc_result와 같다)
            raise PocDeviceCallError(
                f"{command_name}: status=COMPLETED outcome=ERR reason={type(error).__name__}: {error}"
            ) from error
        self.results.append(result)
        return result

    def invalidate(self):
        self.invalidated += 1


class _Arbiter:
    def __init__(self, *, refuse=False):
        self.reserved, self.released, self._refuse = [], [], refuse

    def reserve(self, *, resource_key, owner):
        if self._refuse:
            return False
        self.reserved.append(resource_key)
        return True

    def release(self, *, resource_key, owner):
        self.released.append(resource_key)


class _ReadyRx:
    def window_ready(self, request):
        return ReadinessDecision(ready=True, reason="READY")


# ------------------------------------------------------------------ builders

def _children(target, *, hw_ns=_HW_NS, same_lane=False):
    """실제 MASS-B fan-out: 첫 child는 C4(→ROC), 둘째가 C6(→MASS-A). 논리 target과 첫 child 물리 수신자가 다르다."""
    channel = TARGET_TO_PHYSICAL_CHANNEL.get(target, PhysicalChannel.UPPER)
    samples = (0j,) * 8192
    cf32 = to_cf32le(samples)
    paths = (("C4", 0, "ROC"), ("C6", 0 if same_lane else 1, "MASS-A"))
    return tuple(
        ChildPath(child_id=f"p:{cable}", parent_id="p", transfer_id="t", cable=cable,
                  tx_station="MASS-B", tx_lane=lane, rx_station=rx, rx_lane=1,
                  channel=channel, payload_digest="d" * 64, waveform_sha256="w" * 64,
                  scheduled_sample=0, scheduled_hw_timestamp_ns=hw_ns, samples=samples,
                  wire_packet_digest="a" * 64, addressed_destination=target, samples_cf32le=cf32)
        for cable, lane, rx in paths)


def _rf_off_path(*, mode="accept", locked=True, route_refuse=False):
    sidecar = _Sidecar(refuse=route_refuse)
    adapter = _Adapter(_Bridge(locked=locked), sidecar, mode=mode)
    sink = AuthoritativeRadioSink(adapter, sidecar=sidecar,
                                        source_station_id=STATIONS["MASS-B"],
                                        station_id_by_endpoint=STATIONS)
    return NS(sink=sink, adapter=adapter, sidecar=sidecar, client=None)


def _launcher_path(*, mode="accept", locked=True, route_refuse=False, interrupt_on=()):
    sidecar = _Sidecar(refuse=route_refuse)
    adapter = _Adapter(_Bridge(locked=locked), sidecar, mode=mode)
    endpoint = NS(dependencies=NS(radio=adapter), stage4=NS(collision_gate=None))
    seam = PocDeviceSeam(endpoint_id="MASS-B", endpoint=endpoint,
                               station_id_by_endpoint=STATIONS)
    client = _InProcessLauncherClient(seam, interrupt_on=interrupt_on)
    sink = LauncherRadioSink(client, station_id_by_endpoint=STATIONS)
    return NS(sink=sink, adapter=adapter, sidecar=sidecar, client=client, seam=seam)


def _commit(path, children):
    admission = path.sink.prepare_group(group_id="p", children=children)
    path.sink.commit_group(admission)
    return admission


def _request_fields(request):
    return {
        "channel": request.channel,
        "transmit_at": request.transmit_at,
        "sample_format": request.samples.sample_format,
        "sample_rate_hz": request.samples.sample_rate_hz,
        "sample_count": request.samples.sample_count,
        "first_sample_at": request.samples.first_sample_at,
        "data_sha256": hashlib.sha256(request.samples.data).hexdigest(),
    }


def _replay(target, packet_hex="deadbeefcafe"):
    return ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target=target,
                            payload={"packet_hex": packet_hex})


def _provider(path, *, arbiter=None):
    return SharedMediumActionProvider.for_production(
        radio_sink=path.sink, rx_readiness=_ReadyRx(), scheduler=NextSafeSlotScheduler(),
        waveform_factory=Link11WaveformFactory(), arbiter=arbiter or _Arbiter())


# ------------------------------------------------------------------ checkers (부정 대조가 이들을 시험한다)

def _assert_bound_logical_destination(case, bound_destination_station_id, target):
    """장치에 실린 목적지는 논리 target의 station id여야 한다(물리 수신자 아님, D1·D2)."""
    case.assertEqual(bound_destination_station_id, STATIONS[target],
                     f"bound destination is not the logical target {target}")


def _assert_pre_tx_refusal(case, exception):
    """방사 전 확정 거부는 `BroadcastCommitRefused`여야 한다(평범한 예외면 UNKNOWN으로 승격된다)."""
    case.assertIsInstance(exception, BroadcastCommitRefused,
                          "a device pre-fire refusal must not be a plain exception (→ OUTCOME_UNKNOWN)")


def _assert_refusal_details_preserved(case, wire_result, **expected):
    """launcher wire 결과는 구조화 details를 잃지 않아야 한다(command_log까지 그대로 간다)."""
    case.assertIs(wire_result.get("accepted"), False)
    case.assertEqual(wire_result.get("error_details"), expected)


# ------------------------------------------------------------------ tests

class RequestConversionParityTest(unittest.TestCase):
    """같은 fan-out group에서 두 경로가 **같은 권위 `RadioTxRequest`**를 만든다(MASS-A/upper·ROC/lower)."""

    def test_same_authoritative_request_for_each_target(self):
        for target in ("MASS-A", "ROC"):
            with self.subTest(target=target):
                rf_off, launcher = _rf_off_path(), _launcher_path()
                children = _children(target)
                _commit(rf_off, children)
                _commit(launcher, children)
                self.assertEqual(len(rf_off.adapter.requests), 1)
                self.assertEqual(len(launcher.adapter.requests), 1)
                a, b = rf_off.adapter.requests[0], launcher.adapter.requests[0]
                self.assertEqual(_request_fields(a), _request_fields(b))       # 경로 간 동등
                # 독립 단언(계약상 올바른 값): 채널 이름·UTC label·clock id·표본 계약.
                self.assertEqual(a.channel, Stage7Channel(EXPECTED_CHANNEL_NAME[target]))
                self.assertEqual(channel_name(children[0].channel), EXPECTED_CHANNEL_NAME[target])
                utc = _HW_NS + _UTC_OFFSET_NS
                self.assertEqual((a.transmit_at.seconds, a.transmit_at.nanoseconds),
                                 (utc // 1_000_000_000, utc % 1_000_000_000))
                self.assertEqual(a.transmit_at.clock_id, "bridge-utc-7")      # bridge 소유 clock id
                self.assertEqual(a.samples.sample_count, 8192)
                self.assertEqual(a.samples.sample_rate_hz, 307_200)
                self.assertEqual(a.samples.data, children[0].samples_cf32le)  # 미리 직렬화한 표본 그대로
                # burst id 상관: 각 sink의 원장 조회 키가 실제 요청의 burst id다.
                self.assertEqual(a.burst_id, BurstId(rf_off.sink.burst_by_group["p"]))
                self.assertEqual(b.burst_id, BurstId(launcher.sink.burst_by_group["p"]))
                # 영수증: launcher는 UTC label을 돌려받아 receipt에 남긴다(target receipt 창 근거).
                self.assertEqual(launcher.sink.receipt_by_group["p"]["transmit_at_utc_ns"], utc)
                self.assertEqual(launcher.sink.receipt_by_group["p"]["wire_packet_digest"], "a" * 64)

    def test_logical_destination_not_physical_receiver_on_both_paths(self):
        for target in ("MASS-A", "ROC"):
            with self.subTest(target=target):
                rf_off, launcher = _rf_off_path(), _launcher_path()
                children = _children(target)
                self.assertEqual(children[0].rx_station, "ROC")          # 첫 child의 물리 수신자
                _commit(rf_off, children)
                _commit(launcher, children)
                for path in (rf_off, launcher):
                    self.assertEqual(len(path.sidecar.bound), 1)
                    _assert_bound_logical_destination(
                        self, path.sidecar.bound[0]["destination_station_id"], target)
                    self.assertEqual(path.sidecar.bound[0]["source_station_id"], STATIONS["MASS-B"])
                self.assertEqual(launcher.client.calls[0][1]["destination_station_id"], STATIONS[target])
                self.assertEqual(logical_destination_station_id(children, STATIONS), STATIONS[target])

    def test_route_binding_timing_and_digest_are_intended_differences(self):
        # launcher: route 결속은 commit의 RPC 안에서(prepare는 장치 무접촉), wire digest를 함께 싣는다.
        # RF-off: route 결속은 prepare에서(in-process), wire digest 없이 결속한다. 이 차이는 프로세스 경계에
        # 걸친 중간 상태를 남기지 않기 위한 설계이므로 그대로 단언한다.
        rf_off, launcher = _rf_off_path(), _launcher_path()
        children = _children("MASS-A")
        rf_off.sink.prepare_group(group_id="p", children=children)
        launcher.sink.prepare_group(group_id="p", children=children)
        self.assertEqual(len(rf_off.sidecar.bound), 1)
        self.assertEqual(launcher.sidecar.bound, [])
        self.assertEqual(launcher.client.calls, [])
        self.assertIsNone(rf_off.sidecar.bound[0]["wire_packet_digest"])
        launcher.sink.commit_group(NS(group_id="p", children=children))
        self.assertEqual(launcher.sidecar.bound[0]["wire_packet_digest"], "a" * 64)

    def test_missing_logical_destination_fails_closed_before_any_device_contact(self):
        for path in (_rf_off_path(), _launcher_path()):
            with self.subTest(path=type(path.sink).__name__):
                with self.assertRaises(RuntimeError) as ctx:
                    _commit(path, _children(None))
                self.assertIn("논리 목적지", str(ctx.exception))
                self.assertEqual(path.sidecar.bound, [])
                self.assertEqual(path.adapter.requests, [])


class CommitOutcomeParityTest(unittest.TestCase):
    """방사 전 확정 거부 vs 송신 여부 불명 — 두 경로가 같은 구분을 낸다. 거부 신호의 **형태**는 다르다."""

    def test_device_refusal_is_rejected_before_tx_on_both_paths(self):
        rf_off, launcher = _rf_off_path(mode="refuse"), _launcher_path(mode="refuse")
        children = _children("MASS-A")
        with self.assertRaises(BroadcastCommitRefused) as rf_ctx:
            _commit(rf_off, children)
        with self.assertRaises(BroadcastCommitRefused) as l_ctx:
            _commit(launcher, children)
        _assert_pre_tx_refusal(self, rf_ctx.exception)
        _assert_pre_tx_refusal(self, l_ctx.exception)
        # 원인 코드는 두 문구에 모두 남는다; launcher는 wire 결과에 구조화 details를 그대로 싣는다.
        self.assertIn("RADIO_LATE_REQUEST", str(rf_ctx.exception))
        self.assertIn("RADIO_LATE_REQUEST", str(l_ctx.exception))
        self.assertIn("SCHEDULE_TX_REJECTED", str(l_ctx.exception))
        _assert_refusal_details_preserved(self, launcher.client.results[-1],
                                          required_lead_ns=200_000_000, remaining_lead_ns=12_345)
        # 둘 다 요청은 장치까지 갔고 장치가 방사 전에 거부했다(TX 0, 영수증 없음).
        self.assertEqual(len(rf_off.adapter.requests), 1)
        self.assertEqual(len(launcher.adapter.requests), 1)
        self.assertEqual(launcher.sink.receipt_by_group, {})
        self.assertEqual(launcher.client.invalidated, 1)   # 거부 뒤에도 장치 상태 캐시는 버린다

    def test_route_bind_refusal_is_pre_tx_on_both_paths_with_different_signals(self):
        rf_off, launcher = _rf_off_path(route_refuse=True), _launcher_path(route_refuse=True)
        children = _children("MASS-A")
        # RF-off: prepare(in-process 결속)에서 RuntimeError → radiation이 staging 거부(BroadcastNotAdmitted).
        with self.assertRaises(RuntimeError) as rf_ctx:
            rf_off.sink.prepare_group(group_id="p", children=children)
        self.assertNotIsInstance(rf_ctx.exception, BroadcastCommitRefused)
        self.assertIn("route 결속 실패", str(rf_ctx.exception))
        # launcher: commit의 RPC가 accepted=False(ROUTE_BIND_REJECTED)를 회신 → 확정 거부.
        with self.assertRaises(BroadcastCommitRefused) as l_ctx:
            _commit(launcher, children)
        self.assertIn("ROUTE_BIND_REJECTED", str(l_ctx.exception))
        _assert_refusal_details_preserved(self, launcher.client.results[-1], expected_channel="lower")
        # 공통: 어느 경로도 schedule_tx에 닿지 않았다(TX 0).
        self.assertEqual(rf_off.adapter.requests, [])
        self.assertEqual(launcher.adapter.requests, [])

    def test_commit_exception_stays_unknown_not_refused_on_both_paths(self):
        rf_off, launcher = _rf_off_path(mode="raise"), _launcher_path(mode="raise")
        children = _children("ROC")
        with self.assertRaises(RuntimeError) as rf_ctx:
            _commit(rf_off, children)
        with self.assertRaises(PocDeviceCallError) as l_ctx:
            _commit(launcher, children)
        for exc in (rf_ctx.exception, l_ctx.exception):
            self.assertNotIsInstance(exc, BroadcastCommitRefused)   # 불명 → UNKNOWN 유지(강등 금지)
        self.assertIn("uhd stream error", str(l_ctx.exception))
        self.assertEqual(launcher.sink.receipt_by_group, {})

    def test_unlocked_bridge_is_unknown_on_both_paths_but_binds_no_route_on_launcher(self):
        rf_off, launcher = _rf_off_path(locked=False), _launcher_path(locked=False)
        children = _children("MASS-A")
        with self.assertRaises(PocDeviceUnavailable) as rf_ctx:
            _commit(rf_off, children)
        self.assertIn("bridge", str(rf_ctx.exception))
        self.assertEqual(len(rf_off.sidecar.bound), 1)          # RF-off는 prepare에서 이미 결속했다
        with self.assertRaises(PocDeviceCallError) as l_ctx:
            _commit(launcher, children)
        self.assertIn("PocDeviceUnavailable", str(l_ctx.exception))
        self.assertEqual(launcher.sidecar.bound, [])             # launcher는 결속 전에 fail-fast
        for path in (rf_off, launcher):
            self.assertEqual(path.adapter.requests, [])          # 어느 쪽도 schedule_tx에 닿지 않는다
        for exc in (rf_ctx.exception, l_ctx.exception):
            self.assertNotIsInstance(exc, BroadcastCommitRefused)

    def test_supervisor_interrupt_propagates_unchanged_on_both_paths(self):
        rf_off = _rf_off_path(mode="interrupt")
        launcher = _launcher_path(interrupt_on=("poc_schedule_group_tx",))
        for path in (rf_off, launcher):
            with self.subTest(path=type(path.sink).__name__):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    _commit(path, _children("ROC"))
                self.assertIn("operator interrupt", str(ctx.exception))


class ProviderSeverityParityTest(unittest.TestCase):
    """production provider(`for_production`)에 두 sink를 꽂아 severity·자원 반환·상관 보존을 비교한다."""

    def test_accepted_action_completes_from_the_ledger_on_both_paths(self):
        for build in (_rf_off_path, _launcher_path):
            with self.subTest(path=build.__name__):
                path = build()
                arbiter = _Arbiter()
                provider = _provider(path, arbiter=arbiter)
                submission = provider.submit(_replay("MASS-A"), request_id="r1")
                self.assertEqual(submission.physical_channel, PhysicalChannel.UPPER)
                self.assertEqual(len(path.adapter.requests), 1)
                self.assertEqual(path.adapter.requests[0].channel, Stage7Channel(UPPER_CHANNEL_NAME))
                parent = provider.parent_for("r1")
                burst_id = path.sink.burst_by_group[parent.parent_id]
                path.adapter.ledger[burst_id] = {"terminal": True,
                                                 "children": {0: "COMPLETED", 1: "COMPLETED"}}
                if path.client is None:
                    source = LedgerTerminalSource(path.adapter, path.sink)
                else:
                    source = LauncherLedgerTerminalSource(path.client, path.sink, max_polls=1,
                                                          poll_interval_s=0.0)
                terminals = source({}, {"request_id": "r1"}, parent)
                self.assertEqual(terminals, {c.child_id: ChildTerminal.COMPLETED.value
                                             for c in parent.children})
                self.assertEqual(provider.resolve_outcome("r1", terminals), ParentOutcome.COMPLETED)
                self.assertEqual(arbiter.released, arbiter.reserved)     # 예약 반환
                self.assertEqual(provider.active_group_count, 0)

    def test_device_refusal_is_request_rejected_with_resources_returned_on_both_paths(self):
        for build in (_rf_off_path, _launcher_path):
            with self.subTest(path=build.__name__):
                path = build(mode="refuse")
                arbiter = _Arbiter()
                provider = _provider(path, arbiter=arbiter)
                with self.assertRaises(PocError) as ctx:
                    provider.submit(_replay("ROC"), request_id="r1")
                self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
                self.assertEqual(ctx.exception.severity.value, "REQUEST_REJECTED")
                self.assertIn("RF 방사 전 거부", str(ctx.exception))
                self.assertEqual(arbiter.released, arbiter.reserved)     # 자원 반환(세션 유지)
                self.assertEqual(provider.active_group_count, 0)
                self.assertNotIn(ParentOutcome.OUTCOME_UNKNOWN, provider.outcomes_by_parent().values())

    def test_commit_exception_is_outcome_unknown_with_correlation_on_both_paths(self):
        for build in (_rf_off_path, _launcher_path):
            with self.subTest(path=build.__name__):
                path = build(mode="raise")
                arbiter = _Arbiter()
                provider = _provider(path, arbiter=arbiter)
                with self.assertRaises(PocError) as ctx:
                    provider.submit(_replay("ROC"), request_id="r1")
                self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
                self.assertEqual(ctx.exception.severity.value, "SESSION_INHIBIT")
                detail = ctx.exception.detail or {}
                parent = provider.parent_for("r1")
                self.assertEqual(detail.get("broadcast_parent_id"), parent.parent_id)
                self.assertEqual(detail.get("transfer_id"), parent.transfer_id)
                self.assertIn("uhd stream error", detail.get("commit_failure", ""))
                self.assertEqual(arbiter.released, [])                    # 소유권 불명 → 예약 유지
                self.assertIn(ParentOutcome.OUTCOME_UNKNOWN, provider.outcomes_by_parent().values())

    def test_normal_tx_holding_the_lane_refuses_before_any_device_contact_on_both_paths(self):
        for build in (_rf_off_path, _launcher_path):
            with self.subTest(path=build.__name__):
                path = build()
                provider = _provider(path, arbiter=_Arbiter(refuse=True))
                with self.assertRaises(PocError) as ctx:
                    provider.submit(_replay("MASS-A"), request_id="r1")
                self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
                self.assertIn("정상 TX가 점유", str(ctx.exception))
                self.assertEqual(path.adapter.requests, [])
                self.assertEqual(path.sidecar.bound, [])

    def test_interrupt_during_commit_propagates_and_marks_outcome_unknown_on_both_paths(self):
        for path in (_rf_off_path(mode="interrupt"),
                     _launcher_path(interrupt_on=("poc_schedule_group_tx",))):
            with self.subTest(path=type(path.sink).__name__):
                arbiter = _Arbiter()
                provider = _provider(path, arbiter=arbiter)
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    provider.submit(_replay("ROC"), request_id="r1")
                self.assertIn("operator interrupt", str(ctx.exception))   # 원형 보존
                cause = ctx.exception.__cause__
                self.assertIsInstance(cause, PocError)
                self.assertEqual(cause.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
                parent = provider.parent_for("r1")
                self.assertEqual((cause.detail or {}).get("broadcast_parent_id"), parent.parent_id)
                self.assertIn(ParentOutcome.OUTCOME_UNKNOWN, provider.outcomes_by_parent().values())
                self.assertEqual(arbiter.released, [])                    # cleanup까지 소유권 유지


class LedgerTerminalParityTest(unittest.TestCase):
    """권위 TX 원장 snapshot → child terminal: wire(str lane 키)와 in-process(int lane 키)가 같은 분류."""

    _CASES = (
        ({0: "COMPLETED", 1: "COMPLETED"}, ("COMPLETED", "COMPLETED")),
        ({0: "COMPLETED", 1: "FAILED"}, ("COMPLETED", "FAILED")),
        ({0: "SUBMITTED", 1: "COMPLETED"}, ("UNKNOWN", "COMPLETED")),   # 진행 중은 미확정 보존
        ({0: "COMPLETED"}, ("COMPLETED", "UNKNOWN")),                    # 미기록 lane은 UNKNOWN
        ({}, ("UNKNOWN", "UNKNOWN")),
    )

    def test_snapshot_classification_is_identical_across_paths(self):
        for ledger_children, expected in self._CASES:
            with self.subTest(children=ledger_children):
                rf_off, launcher = _rf_off_path(), _launcher_path()
                children = _children("ROC")
                _commit(rf_off, children)
                _commit(launcher, children)
                parent = NS(parent_id="p", children=children)
                snapshot = {"terminal": bool(ledger_children), "children": dict(ledger_children)}
                rf_off.adapter.ledger[rf_off.sink.burst_by_group["p"]] = snapshot
                launcher.adapter.ledger[launcher.sink.burst_by_group["p"]] = snapshot
                a = LedgerTerminalSource(rf_off.adapter, rf_off.sink)({}, {}, parent)
                b = LauncherLedgerTerminalSource(launcher.client, launcher.sink, max_polls=1,
                                                 poll_interval_s=0.0)({}, {}, parent)
                want = {children[0].child_id: expected[0], children[1].child_id: expected[1]}
                self.assertEqual(a, want)
                self.assertEqual(b, want)
                # wire snapshot은 str 키다 — helper는 두 키 형식을 같은 규칙으로 읽는다.
                wire = launcher.seam.tx_burst_state(launcher.sink.burst_by_group["p"])
                self.assertTrue(all(isinstance(k, str) for k in wire["children"]))
                self.assertEqual(child_terminals_from_ledger(wire, parent), want)
                self.assertEqual(child_terminals_from_ledger(snapshot, parent), want)

    def test_unknown_burst_or_missing_snapshot_is_unknown_on_both_paths(self):
        rf_off, launcher = _rf_off_path(), _launcher_path()
        children = _children("ROC")
        parent = NS(parent_id="never-committed", children=children)
        want = {c.child_id: ChildTerminal.UNKNOWN.value for c in children}
        self.assertEqual(LedgerTerminalSource(rf_off.adapter, rf_off.sink)({}, {}, parent), want)
        self.assertEqual(LauncherLedgerTerminalSource(launcher.client, launcher.sink, max_polls=1,
                                                      poll_interval_s=0.0)({}, {}, parent), want)
        self.assertEqual(launcher.client.calls, [])          # burst를 모르면 장치에 묻지 않는다
        _commit(rf_off, children)
        _commit(launcher, children)
        parent = NS(parent_id="p", children=children)        # 원장에 아직 없음(None snapshot)
        self.assertEqual(LedgerTerminalSource(rf_off.adapter, rf_off.sink)({}, {}, parent), want)
        self.assertEqual(LauncherLedgerTerminalSource(launcher.client, launcher.sink, max_polls=1,
                                                      poll_interval_s=0.0)({}, {}, parent), want)

    def test_lead_measurement_survives_the_wire_snapshot(self):
        launcher = _launcher_path()
        _commit(launcher, _children("ROC"))
        burst_id = launcher.sink.burst_by_group["p"]
        launcher.adapter.ledger[burst_id] = {
            "terminal": True, "children": {0: "COMPLETED", 1: "COMPLETED"},
            "lead": {"required_lead_ns": 200_000_000, "remaining_lead_ns": 250_000_000,
                     "margin_ns": 50_000_000, "note": "ignored-non-int"}}
        wire = launcher.seam.tx_burst_state(burst_id)
        self.assertEqual(wire["lead"], {"required_lead_ns": 200_000_000,
                                        "remaining_lead_ns": 250_000_000, "margin_ns": 50_000_000})


class FanoutValidationParityTest(unittest.TestCase):
    """세 sink(launcher·RF-off 권위·worker 직접)가 같은 fan-out child 계약 위반을 같은 문구로 거부한다."""

    def _sinks(self):
        return {
            "launcher": _launcher_path().sink,
            "rf_off": _rf_off_path().sink,
            "worker_direct": GroupTxRadioSink(group_tx=SoftwareGroupTx()),
        }

    def test_identical_rejections_for_malformed_groups(self):
        base = _children("ROC")
        cases = {
            "one_child": base[:1],
            "different_timestamps": (base[0], _children("ROC", hw_ns=_HW_NS + 1)[1]),
            "different_channels": (base[0], _children("MASS-A")[1]),
        }
        for label, children in cases.items():
            with self.subTest(case=label):
                messages = set()
                for name, sink in self._sinks().items():
                    with self.assertRaises(RuntimeError, msg=name) as ctx:
                        sink.prepare_group(group_id="p", children=children)
                    messages.add(str(ctx.exception))
                self.assertEqual(len(messages), 1, messages)              # 세 sink가 같은 사유

    def test_same_lane_is_rejected_only_by_the_worker_direct_boundary(self):
        # 권위 schedule_tx 경계는 한 요청으로 두 lane에 방사하므로 lane 구분을 요구하지 않는다(의도된 차이).
        children = _children("ROC", same_lane=True)
        with self.assertRaises(RuntimeError) as ctx:
            GroupTxRadioSink(group_tx=SoftwareGroupTx()).prepare_group(group_id="p", children=children)
        self.assertIn("서로 다른 두 TX lane", str(ctx.exception))
        for path in (_launcher_path(), _rf_off_path()):
            path.sink.prepare_group(group_id="p", children=children)   # 거부하지 않는다


class NegativeControlTest(unittest.TestCase):
    """검사기가 핵심 회귀(표적 매핑·거부 severity·details 손실·terminal 합성)를 실제로 잡는다."""

    def test_checker_rejects_physical_receiver_as_destination(self):
        children = _children("MASS-A")
        wrong = STATIONS[children[0].rx_station]                # 옛 결함: 첫 child의 물리 수신자(ROC)
        with self.assertRaises(AssertionError):
            _assert_bound_logical_destination(self, wrong, "MASS-A")
        _assert_bound_logical_destination(self, logical_destination_station_id(children, STATIONS), "MASS-A")

    def test_checker_rejects_plain_exception_as_refusal(self):
        with self.assertRaises(AssertionError):
            _assert_pre_tx_refusal(self, RuntimeError("권위 schedule_tx 거부: LEAD_TOO_SHORT"))
        _assert_pre_tx_refusal(self, BroadcastCommitRefused("권위 schedule_tx 거부: LEAD_TOO_SHORT"))

    def test_checker_rejects_lost_error_details(self):
        with self.assertRaises(AssertionError):
            _assert_refusal_details_preserved(
                self, {"accepted": False, "reason": "SCHEDULE_TX_REJECTED", "detail": "RADIO_LATE_REQUEST"},
                required_lead_ns=200_000_000)
        with self.assertRaises(AssertionError):
            _assert_refusal_details_preserved(
                self, {"accepted": False, "error_details": {"required_lead_ns": 1}}, required_lead_ns=200_000_000)

    def test_terminal_classifier_does_not_synthesize_completion(self):
        children = _children("ROC")
        parent = NS(parent_id="p", children=children)
        synthesized = {c.child_id: ChildTerminal.COMPLETED.value for c in children}
        for snapshot in (None, {"terminal": False, "children": {0: "SUBMITTED", 1: "SUBMITTED"}},
                         {"terminal": True, "children": {"0": "COMPLETED"}}):
            with self.subTest(snapshot=snapshot):
                self.assertNotEqual(child_terminals_from_ledger(snapshot, parent), synthesized)


if __name__ == "__main__":
    unittest.main()
