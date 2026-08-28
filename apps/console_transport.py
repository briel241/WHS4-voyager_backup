"""콘솔 GUI의 전송 경로 — 직결(direct) 또는 VDETER 무선.

두 경로가 같은 인터페이스를 쓴다. 구동기(console_server)는 어느 쪽인지 모르고
`send()`로 내보내고 `pump()`로 도착한 것을 받는다.

    direct  : payload를 그대로 상대에게 넘긴다. 무선 계층 없음. 시연 안정성 우선.
    vdeter  : 실 VDETER Service API(submit/status/HandlerServiceEventSink)를 탄다.
              지금은 소프트웨어 무선 그룹이고, 링크 팀 Stage 7 B210 launcher가
              나오면 `_build_group()`만 바꾸면 실장비로 간다. 응용 코드는 그대로다.

전송이 즉시 끝나지 않는 것이 정상이다. VDETER 경로는 시계를 진행시켜야 도착하므로
`send()`는 "접수됨"만 뜻하고 실제 도착은 이후 `pump()`에서 나온다. 직결 경로도
같은 모양을 따르게 해서 구동기 코드가 갈라지지 않게 했다.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common import codec  # noqa: E402
from common import vdeter_bridge as bridge  # noqa: E402
from common.outbound import (  # noqa: E402
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
)

#: 한 스텝에 진행시킬 VDETER 시계 스텝 수. 구동기 루프가 10Hz이므로 이 값이
#: 클수록 전송이 빨리 끝난다. 너무 크면 한 스텝이 오래 걸려 화면이 끊긴다.
CLOCK_STEPS_PER_TICK = 24

#: 이 횟수를 넘겨도 안 끝난 전송은 실패로 본다(무한 대기 방지).
MAX_TICKS_PER_TRANSFER = 200

#: 우리 봉투 크기. `[ VPFI 16bit | Message ID 8bit ]` = 3 byte.
_ENVELOPE_BYTES = 3


def strip_envelope(payload: bytes) -> tuple[bytes, int]:
    """행위자가 만든 payload에서 봉투를 뗀다 → (application bytes, message_id).

    VDETER는 VPFI·Message Format을 직접 붙이므로 application bytes만 받는다
    (API 가이드 §8.2). 행위자는 `codec.encode()`로 봉투를 붙여 내놓으므로
    여기서 떼어 준다.
    """
    payload = bytes(payload)
    if len(payload) < _ENVELOPE_BYTES:
        raise ValueError(f"봉투를 뗄 수 없는 payload ({len(payload)} byte)")
    return payload[_ENVELOPE_BYTES:], payload[2]


def add_envelope(application: bytes, message_id: int) -> bytes:
    """VDETER가 준 application bytes에 우리 봉투를 도로 붙인다.

    수신 행위자는 `codec.decode()`로 읽으므로 봉투가 있어야 한다.
    message_id는 payload_format이 알려준다.
    """
    from common import config

    vpfi = config.VPFI
    return bytes([(vpfi >> 8) & 0xFF, vpfi & 0xFF, message_id]) + bytes(application)


@dataclass
class Arrival:
    """도착한 application payload 하나."""

    payload: bytes
    source_id: int
    destination_id: int


class DirectTransport:
    """무선 계층 없이 payload를 그대로 전달한다."""

    name = "direct"
    detail = "무선 계층 없음 (에이전트 직결)"
    operational = False

    def __init__(self) -> None:
        self._arrivals: list[Arrival] = []

    def send(self, payload, *, source_id, destination_id, data_type=None) -> tuple[bool, str]:
        self._arrivals.append(
            Arrival(bytes(payload), source_id=source_id, destination_id=destination_id)
        )
        return True, ""

    def pump(self) -> list[Arrival]:
        arrivals, self._arrivals = self._arrivals, []
        return arrivals

    def stats(self) -> dict:
        return {"in_flight": 0, "completed": 0, "failed": 0}

    def close(self) -> None:
        pass


class VdeterUnavailable(RuntimeError):
    """VDETER 소프트웨어 무선 그룹을 조립할 수 없다."""


class VdeterTransport:
    """실 VDETER Service API를 타는 전송 경로.

    구조는 loopback 데모(`apps/_loopback_opaque.py`)와 같다 — 공식
    `HandlerServiceEventSink`로 endpoint에 붙고, `submit()`으로 내보내고,
    `status()`로 완료를 본다. 다른 점은 **그룹을 하나만 만들어 계속 쓴다**는
    것이다(데모는 전송마다 새로 만든다). 연속 전송이 되는 것은 확인했다.
    """

    name = "vdeter"
    detail = "VDETER 소프트웨어 무선 (Service API 종단)"
    operational = True

    def __init__(self, mass_mmsi: int, roc_mmsi: int) -> None:
        self._mass_mmsi = mass_mmsi
        self._roc_mmsi = roc_mmsi
        self._arrivals: list[Arrival] = []
        self._inflight: list[dict] = []
        self.completed = 0
        self.failed = 0
        self._build_group()

    # -------------------------------------------------------------- 조립

    # 링크 팀 Stage 7 B210 launcher가 나오면 이 메서드만 바꾼다.
    # 나머지(send/pump)는 Service API만 쓰므로 그대로 동작한다.
    def _build_group(self) -> None:
        try:
            sys.path.insert(0, os.path.join(_ROOT, "VDETER"))
            from tests.support.stage5 import (
                MASS_A_STATION_ID,
                ROC_STATION_ID,
                STATION_ENDPOINT_IDS,
                build_stage5_software_group,
                drive_stage5_group,
                start_stage5_group,
            )
            from vdeter.common.addressing import Addressed
            from vdeter.common.contracts.service import (
                SubmitTransferRequest,
                TransferState,
                TransferStatusRequest,
            )
            from vdeter.common.events import ServiceEventType
            from vdeter.common.identifiers import PayloadFormat
            from vdeter.service import HandlerServiceEventSink
        except ImportError as exc:  # pragma: no cover
            raise VdeterUnavailable(f"VDETER를 불러올 수 없습니다: {exc}") from exc

        self._Addressed = Addressed
        self._SubmitTransferRequest = SubmitTransferRequest
        self._TransferState = TransferState
        self._TransferStatusRequest = TransferStatusRequest
        self._PayloadFormat = PayloadFormat
        self._drive = drive_stage5_group

        # 우리 MMSI ↔ VDETER station
        self._station_by_mmsi = {
            self._mass_mmsi: MASS_A_STATION_ID,
            self._roc_mmsi: ROC_STATION_ID,
        }
        endpoint_of = {
            self._mass_mmsi: STATION_ENDPOINT_IDS[MASS_A_STATION_ID.value],
            self._roc_mmsi: STATION_ENDPOINT_IDS[ROC_STATION_ID.value],
        }
        mmsi_by_endpoint = {v: k for k, v in endpoint_of.items()}

        def sink_factory(endpoint_id):
            owner = mmsi_by_endpoint.get(endpoint_id)

            def handle(event) -> None:
                if event.event_type is not ServiceEventType.DATA_RECEIVED:
                    return
                received = event.received_data
                if received is None or owner is None:
                    return
                inbound = bridge.inbound_from_received(received)
                message_id = bridge.message_id_for_payload_format(inbound.payload_format)
                if message_id is None:
                    return
                # 행위자는 봉투가 붙은 payload를 기대하므로 도로 붙여 준다.
                self._arrivals.append(
                    Arrival(
                        payload=add_envelope(inbound.payload, message_id),
                        source_id=self._mmsi_for_station(inbound.source_id),
                        destination_id=owner,
                    )
                )

            return HandlerServiceEventSink(handle)

        try:
            self._clock, self._group = build_stage5_software_group(
                service_sink_factory=sink_factory
            )
            start_stage5_group(self._clock, self._group)
        except Exception as exc:  # pragma: no cover - cryptography 등 조립 실패
            raise VdeterUnavailable(f"소프트웨어 무선 그룹 조립 실패: {exc}") from exc

        by_station = self._group.by_station()
        self._service_by_mmsi = {
            mmsi: by_station[station.value]
            for mmsi, station in self._station_by_mmsi.items()
        }

    # -------------------------------------------------------------- 송신

    def send(self, payload, *, source_id, destination_id, data_type=None) -> tuple[bool, str]:
        service = self._service_by_mmsi.get(source_id)
        station = self._station_by_mmsi.get(destination_id)
        if service is None or station is None:
            return False, f"VDETER에 없는 station (from {source_id} to {destination_id})"

        # data_type이 없으면 보낼 수 없다. 위치보고 등은 아직 등록된 포맷이 없다.
        fmt = self._payload_format_for(data_type)
        if fmt is None:
            return False, f"등록된 payload_format이 없는 data_type: {data_type!r}"

        # 행위자 payload에는 우리 봉투가 붙어 있다. VDETER에는 본문만 준다.
        try:
            application, _ = strip_envelope(payload)
            bridge.check_sc_vde_payload(fmt, application)
        except ValueError as exc:
            return False, str(exc)

        request = self._SubmitTransferRequest(
            destination=self._Addressed(station),
            payload_format=self._PayloadFormat(fmt),
            payload=application,
            ack_required=True,
        )
        accepted = service.submit(request)
        if not accepted.is_ok:
            error = getattr(accepted, "error", None)
            code = getattr(error, "code", accepted)
            return False, f"submit 거부: {code}"

        self._inflight.append(
            {
                "service": service,
                "transfer_id": accepted.value.transfer_id,
                "ticks": 0,
            }
        )
        return True, ""

    @staticmethod
    def _payload_format_for(data_type):
        if data_type is None:
            return None
        return bridge.PAYLOAD_FORMAT_BY_DATA_TYPE.get(data_type)

    # VDETER가 알려주는 송신 station을 우리 MMSI로 되돌린다.
    def _mmsi_for_station(self, station_value) -> int:
        for mmsi, station in self._station_by_mmsi.items():
            if station.value == station_value:
                return mmsi
        return int(station_value)

    # -------------------------------------------------------------- 진행

    def pump(self) -> list[Arrival]:
        """시계를 진행시키고, 이번에 도착한 payload를 돌려준다."""
        if self._inflight:
            self._drive(self._clock, self._group, CLOCK_STEPS_PER_TICK)
            still: list[dict] = []
            terminal = (
                self._TransferState.COMPLETED,
                self._TransferState.FAILED,
                self._TransferState.CANCELLED,
            )
            for item in self._inflight:
                item["ticks"] += 1
                status = item["service"].status(
                    self._TransferStatusRequest(item["transfer_id"])
                )
                state = status.value.state if status.is_ok else None
                if state in terminal:
                    if state is self._TransferState.COMPLETED:
                        self.completed += 1
                    else:
                        self.failed += 1
                elif item["ticks"] >= MAX_TICKS_PER_TRANSFER:
                    self.failed += 1
                else:
                    still.append(item)
            self._inflight = still

        arrivals, self._arrivals = self._arrivals, []
        return arrivals

    def stats(self) -> dict:
        return {
            "in_flight": len(self._inflight),
            "completed": self.completed,
            "failed": self.failed,
        }

    def close(self) -> None:
        try:
            self._group.stop()
        except Exception:  # pragma: no cover
            pass


def build_transport(kind: str, *, mass_mmsi: int, roc_mmsi: int):
    """`kind`에 맞는 전송 경로를 만든다. vdeter가 안 되면 사유와 함께 direct로 내려간다."""
    if kind == "direct":
        return DirectTransport(), ""
    try:
        return VdeterTransport(mass_mmsi=mass_mmsi, roc_mmsi=roc_mmsi), ""
    except VdeterUnavailable as exc:
        return DirectTransport(), str(exc)
