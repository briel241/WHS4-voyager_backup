"""MASS/ROC 응용 ↔ VDE-TER Service API 종단 회귀 — 소프트웨어 무선, 장비 없음.

실행: python tests/test_mass_roc_link_integration.py
      (pytest tests -q 로도 수집·실행된다)

구식 로컬 사본(VDETER_Link/, vdeter_link.*) 기반이던 이전 판을 대체한다.
payload는 이제 공개 Service API만으로 실제 VDE-TER 소프트웨어 무선 스택을
왕복한다. 링크 내부(Type 92/93·Packet·Burst)는 어디서도 만지지 않는다.

    ROC 행위자 Outbound
    → common.vdeter_bridge.to_submit_request  (SubmitTransferRequest)
    → service.submit()  →  SoftwareRadio 송수신  →  전송 상태 COMPLETED
    → HandlerServiceEventSink(DATA_RECEIVED) → bridge.Inbound
    → MASS 행위자 처리 → 응용 ACK가 역방향으로 같은 경로를 왕복

검증 범위: SC-VDE-005 노선(waypoint·계획 SOG 복원·적용), SC-VDE-006 원격
조종(heading·fallback 복원·적용), 응용 ACK 결속, source/destination, 주소
격리(MASS-B 미수신), 재전송 중복 적용 방지.

전제: in-repo VDETER/ + cryptography. 없으면 SKIP을 알리고 끝낸다(직접 실행)
또는 pytest skip으로 표시된다 — 이 저장소의 회귀 환경에는 둘 다 있다.
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common import codec, config
from common import vdeter_bridge as bridge
from common.messages import application_ack as ack
from common.messages import position_report as pos
from common.messages import route_instruction as route
from common.outbound import (
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
    Outbound,
)
from MASS import MassAgent, ShipState
from ROC import RocAgent

from apps.console_transport import add_envelope, strip_envelope

try:
    from tests.support.stage5 import (
        MASS_A_STATION_ID,
        MASS_B_STATION_ID,
        ROC_STATION_ID,
        STATION_ENDPOINT_IDS,
        build_stage5_software_group,
        drive_stage5_group,
        start_stage5_group,
    )
    from vdeter.common.events import ServiceEventType
    from vdeter.common.contracts.service import TransferState, TransferStatusRequest
    from vdeter.service import HandlerServiceEventSink

    HARNESS_AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - VDETER 없는 환경
    HARNESS_AVAILABLE = False
    _IMPORT_ERROR = exc

#: 소프트웨어 그룹의 세 번째 국(MASS-B). 주소 격리 검증에 쓴다.
MASS_B_MMSI = 440_000_003

#: 전송 하나가 끝날 때까지 진행시키는 최대 시계 스텝 수 (_loopback_opaque와 동일).
MAX_DRIVE_STEPS = 800


class _SkipHarness(Exception):
    """이 환경에서 소프트웨어 무선 그룹을 조립할 수 없다."""


class _Radio:
    """세 국(MASS-A·ROC·MASS-B)이 붙은 소프트웨어 무선 그룹 하나.

    console_transport.VdeterTransport와 같은 구조로, 공개 Service API만 쓴다.
    수신은 공식 HandlerServiceEventSink로 모으고, 응용 MMSI ↔ VDETER station
    번역은 이 하네스가 경계에서 맡는다(운영에서는 launcher/integration의 몫).
    """

    def __init__(self) -> None:
        if not HARNESS_AVAILABLE:
            raise _SkipHarness(f"VDETER import 실패: {_IMPORT_ERROR}")

        self.station_by_mmsi = {
            config.MASS_MMSI: MASS_A_STATION_ID,
            config.ROC_MMSI: ROC_STATION_ID,
            MASS_B_MMSI: MASS_B_STATION_ID,
        }
        self.mmsi_by_station = {
            station.value: mmsi for mmsi, station in self.station_by_mmsi.items()
        }
        mmsi_by_endpoint = {
            STATION_ENDPOINT_IDS[station.value]: mmsi
            for mmsi, station in self.station_by_mmsi.items()
        }
        #: 국별로 도착한 (source_mmsi, payload_format, application bytes)
        self.received: dict[int, list[tuple[int, str, bytes]]] = {
            mmsi: [] for mmsi in self.station_by_mmsi
        }

        def sink_factory(endpoint_id):
            owner = mmsi_by_endpoint[endpoint_id]

            def handle(event) -> None:
                bridge.handle_service_event(
                    event,
                    on_data=lambda inbound: self.received[owner].append(
                        (
                            self.mmsi_by_station[inbound.source_id],
                            inbound.payload_format,
                            inbound.payload,
                        )
                    ),
                )

            return HandlerServiceEventSink(handle)

        try:
            self.clock, self.group = build_stage5_software_group(
                service_sink_factory=sink_factory
            )
        except unittest.SkipTest as exc:  # cryptography 미설치 등
            raise _SkipHarness(str(exc)) from exc
        start_stage5_group(self.clock, self.group)
        by_station = self.group.by_station()
        self.service_by_mmsi = {
            mmsi: by_station[station.value]
            for mmsi, station in self.station_by_mmsi.items()
        }

    def close(self) -> None:
        self.group.stop()

    # 행위자 Outbound 하나를 무선으로 나른다. 반환: 종단 전송 상태.
    def transfer(self, outbound: Outbound, *, source_mmsi: int, data_type: str):
        application, _message_id = strip_envelope(outbound.payload)
        destination_station = self.station_by_mmsi[outbound.destination_id]
        request = bridge.to_submit_request(
            Outbound(
                payload=application,
                destination_id=destination_station.value,
                ack_required=outbound.ack_required,
                data_type=data_type,
                label=outbound.label,
            )
        )
        service = self.service_by_mmsi[source_mmsi]
        accepted = service.submit(request)
        assert accepted.is_ok, f"submit 거부: {getattr(accepted, 'error', accepted)}"

        transfer_id = accepted.value.transfer_id
        state = None
        for _ in range(MAX_DRIVE_STEPS):
            drive_stage5_group(self.clock, self.group, 1)
            state = service.status(TransferStatusRequest(transfer_id)).value.state
            if state in (
                TransferState.COMPLETED,
                TransferState.FAILED,
                TransferState.CANCELLED,
            ):
                break
        return state

    # 국에 도착한 것을 꺼내 행위자 payload(봉투 포함)로 되돌린다.
    def take_arrivals(self, owner_mmsi: int) -> list[tuple[int, bytes]]:
        arrivals, self.received[owner_mmsi] = self.received[owner_mmsi], []
        result = []
        for source_mmsi, payload_format, application in arrivals:
            message_id = bridge.message_id_for_payload_format(payload_format)
            assert message_id is not None, f"뜻밖의 payload_format: {payload_format}"
            result.append((source_mmsi, add_envelope(application, message_id)))
        return result


def _skip(reason: str) -> None:
    try:
        import pytest
    except ImportError:  # 직접 실행 + pytest 미설치
        raise _SkipHarness(reason)
    pytest.skip(reason)


def _open_radio() -> _Radio:
    try:
        return _Radio()
    except _SkipHarness as exc:
        _skip(str(exc))
        raise  # pytest.skip이 이미 던졌다 — 정적 분석용


# ROC 명령 Outbound를 무선으로 보내 MASS가 처리하게 하고, 응용 ACK를 역방향
# 무선으로 되돌려 ROC에 결속시킨다. 반환: (명령 전송 상태, ACK 전송 상태).
def _command_round_trip(radio: _Radio, roc: RocAgent, mass: MassAgent, command: Outbound):
    command_state = radio.transfer(
        command, source_mmsi=roc.mmsi, data_type=DATA_TYPE_ROUTE_CONTROL_COMMAND
    )

    ack_outbounds = []
    for source_mmsi, payload in radio.take_arrivals(mass.mmsi):
        ack_outbounds.extend(mass.receive(payload, source_mmsi))

    ack_state = None
    for outbound in ack_outbounds:
        ack_state = radio.transfer(
            outbound, source_mmsi=mass.mmsi, data_type=DATA_TYPE_ROUTE_CONTROL_ACK
        )
    for source_mmsi, payload in radio.take_arrivals(roc.mmsi):
        roc.receive(payload, source_mmsi)

    return command_state, ack_state


# ------------------------------------------------ SC-VDE-005 노선 명령


def test_sc_vde_005_route_round_trip():
    radio = _open_radio()
    try:
        roc = RocAgent()
        mass = MassAgent(
            state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0)
        )
        waypoints = [
            route.waypoint_from_degrees(129.076, 35.102, turn_radius_nm=0.5,
                                        planned_sog_knots=12.5,
                                        leg_geometry=route.LEG_GEOMETRY_LEXODROME),
            route.waypoint_from_degrees(129.333, 35.208, turn_radius_nm=1.2,
                                        planned_sog_knots=8.0,
                                        leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
            route.waypoint_from_degrees(129.500, 35.300),
        ]
        command = roc.issue_route(mass.mmsi, waypoints)
        original = codec.decode(command.payload)

        command_state, ack_state = _command_round_trip(radio, roc, mass, command)

        # 전송 상태 — 명령·ACK 둘 다 링크 종단 완료.
        assert command_state is TransferState.COMPLETED
        assert ack_state is TransferState.COMPLETED

        # 주소 격리 — 지정 수신자가 아닌 MASS-B에는 아무것도 도착하지 않는다.
        assert radio.take_arrivals(MASS_B_MMSI) == []

        # waypoint·계획 SOG 복원과 실제 적용.
        assert mass.has_route
        assert mass.route_linkage_id == original.linkage_id
        assert mass.remaining_waypoints == 3
        active = mass.active_waypoint
        assert abs(active.longitude_degrees - 129.076) < 1e-4
        assert abs(active.latitude_degrees - 35.102) < 1e-4
        assert active.planned_sog_knots == 12.5
        assert mass.state.sog == 12.5  # 첫 구간 계획 SOG가 명령 속력이 된다

        # 응용 ACK — ROC가 자신이 보낸 명령에 결속했다.
        pending = roc.commands[original.linkage_id]
        assert pending.answered
        assert pending.status == ack.STATUS_ACKNOWLEDGED
        assert roc.mismatched_ack_count == 0
    finally:
        radio.close()


# ------------------------------------------- SC-VDE-006 원격 조종 명령


def test_sc_vde_006_remote_control_round_trip():
    radio = _open_radio()
    try:
        roc = RocAgent()
        mass = MassAgent(
            state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0)
        )

        # ① heading setpoint + 계획 SOG — 복원되고 실제로 배에 반영된다.
        steering = roc.issue_route(
            mass.mmsi,
            [
                route.waypoint_from_degrees(129.05, 35.10, planned_sog_knots=6.0),
                route.waypoint_from_degrees(129.10, 35.133, planned_sog_knots=4.0),
            ],
            route_type=route.ROUTE_TYPE_RECOMMENDED,
            heading_setpoint=92.5,
        )
        command_state, ack_state = _command_round_trip(radio, roc, mass, steering)
        assert command_state is TransferState.COMPLETED
        assert ack_state is TransferState.COMPLETED
        assert mass.heading_setpoint_degrees == 92.5
        assert mass.state.sog == 6.0
        assert mass.state.autonomy_mode == pos.AUTONOMY_RC  # 원격 제어로 전환

        # ② fallback 지시 — 복원되고 배를 실제로 세운다.
        fallback = roc.issue_route(
            mass.mmsi,
            [],
            route_type=route.ROUTE_TYPE_RECOMMENDED,
            fallback_state=route.FALLBACK_ANCHORING,
        )
        command_state, ack_state = _command_round_trip(radio, roc, mass, fallback)
        assert command_state is TransferState.COMPLETED
        assert ack_state is TransferState.COMPLETED
        assert mass.fallback_name == "Anchoring"
        assert mass.state.sog == 0.0

        # 두 명령 모두 응용 ACK로 결속됐다.
        answered = [c for c in roc.commands.values() if c.answered]
        assert len(answered) == 2
        assert all(c.status == ack.STATUS_ACKNOWLEDGED for c in answered)
        assert radio.take_arrivals(MASS_B_MMSI) == []
    finally:
        radio.close()


# ------------------------------------------------ 재전송 중복 적용 방지


def test_duplicate_retransmission_not_reapplied():
    radio = _open_radio()
    try:
        roc = RocAgent()
        mass = MassAgent(
            state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0)
        )
        command = roc.issue_route(
            mass.mmsi,
            [
                route.waypoint_from_degrees(129.05, 35.10, planned_sog_knots=6.0),
                route.waypoint_from_degrees(129.10, 35.133, planned_sog_knots=4.0),
            ],
        )

        first_state, first_ack = _command_round_trip(radio, roc, mass, command)
        assert first_state is TransferState.COMPLETED
        assert first_ack is TransferState.COMPLETED
        assert mass.state.sog == 6.0

        # 항해가 진행된 뒤 같은 payload가 재전송으로 다시 도착한다.
        mass.state.sog = 9.9
        second_state, second_ack = _command_round_trip(radio, roc, mass, command)
        assert second_state is TransferState.COMPLETED
        assert second_ack is TransferState.COMPLETED  # ACK는 다시 나간다

        assert mass.duplicate_count == 1        # 재적용하지 않고 세기만 한다
        assert mass.state.sog == 9.9            # 계획 SOG로 되돌리지 않는다
        assert mass.remaining_waypoints == 2    # 진행 상태가 리셋되지 않는다
    finally:
        radio.close()


# ------------------------------------------------ 계층 격리 (무선 불필요)


def test_foreign_payload_rejected():
    mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))
    before = mass.ignored_count
    replies = mass.receive(b"\xde\xad\xbe\xef", source_id=config.ROC_MMSI)
    assert replies == []
    assert mass.ignored_count == before + 1


# ------------------------------------------------------------------ 직접 실행

_TESTS = (
    test_foreign_payload_rejected,
    test_sc_vde_005_route_round_trip,
    test_sc_vde_006_remote_control_round_trip,
    test_duplicate_retransmission_not_reapplied,
)


def main() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except _SkipHarness as exc:
            print(f"SKIP {fn.__name__}: {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    if failed:
        print(f"실패 {failed}건")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
