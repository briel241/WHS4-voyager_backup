"""콘솔 GUI의 전송 경로 — direct(직결) / software(소프트웨어 무선) / stage7(외부 주입).

세 경로가 같은 인터페이스를 쓴다. 구동기(console_server)는 어느 쪽인지 모르고
`send()`로 내보내고 `pump()`로 도착한 것을 받는다.

    direct   : payload를 그대로 상대에게 넘긴다. 무선 계층 없음. 순수 응용 로직 확인용.
    software : Software Radio 기반 실 VDETER Service API
               (submit/status/HandlerServiceEventSink). 자동 회귀시험·시연용.
    stage7   : 외부(launcher/integration)가 조립한 Stage 7 VDE-TER Service를
               주입받는다. MASS/ROC는 B210·Radio·PHY·MAC·Link를 직접 알지 않는다.

software와 stage7은 같은 공개 Service API 계약(`_ServiceApiTransport`)을 공유한다 —
다른 것은 Service 객체를 어디서 얻는가(직접 조립 vs 주입)와 시계 진행 방식뿐이다.

Stage 7 주입 seam
    VDETER 공개 API의 공식 연결점은 in-process 조립뿐이다:
    `vdeter.stage7.wiring.build_stage7_b210_endpoint(..., application_sink=...)`가
    반환하는 endpoint가 곧 `VdeService`다. 응용 쪽 연결은
    `ExternalServiceTransport.event_handler_for(mmsi)`를 `HandlerServiceEventSink`로
    감싸 application_sink로 넘기고, 조립된 endpoint를 `attach_service()`로 주입한다.
    별도 프로세스의 launcher에 접속하는 응용용 RPC는 공개 API에 없다(가이드 §15).
    임의 RPC를 만들지 않고 이 seam까지만 제공한다.

전송이 즉시 끝나지 않는 것이 정상이다. Service API 경로에서 `send()`는 "접수됨"만
뜻하고 실제 도착은 이후 `pump()`에서 나온다. 직결 경로도 같은 모양을 따르게 해서
구동기 코드가 갈라지지 않게 했다.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common import vdeter_bridge as bridge  # noqa: E402
from common.outbound import Outbound  # noqa: E402

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
    """요청한 VDETER 전송 경로를 조립할 수 없다."""


class _ServiceApiTransport:
    """공개 Service API만 쓰는 send/pump 공통 구현.

    소프트웨어 무선(software)과 외부 주입(stage7)이 공유한다. 하위 클래스는
    Service 확보(`_service_by_mmsi` 채우기)와 시계 진행(`_advance`)만 다르다.
    송신은 `common.vdeter_bridge.to_submit_request`로 DTO를 만들고, 수신은
    `bridge.handle_service_event`로 DATA_RECEIVED를 받는다 — 링크 내부
    (Link ID·MAC·PHY·Radio)는 어디서도 만지지 않는다.
    """

    name = "service"
    detail = "VDETER Service API"
    operational = True

    def __init__(self, station_by_mmsi: dict[int, int]) -> None:
        #: 응용 MMSI → VDETER station 번호. 주소 번역은 이 경계에서만 한다.
        self._station_by_mmsi = dict(station_by_mmsi)
        self._service_by_mmsi: dict[int, object] = {}
        self._arrivals: list[Arrival] = []
        self._inflight: list[dict] = []
        self.completed = 0
        self.failed = 0
        self._import_service_api()

    def _import_service_api(self) -> None:
        try:
            from vdeter.common.contracts.service import (  # noqa: F401
                TransferState,
                TransferStatusRequest,
            )
        except ImportError as exc:
            raise VdeterUnavailable(f"VDETER를 불러올 수 없습니다: {exc}") from exc
        self._TransferState = TransferState
        self._TransferStatusRequest = TransferStatusRequest

    # ------------------------------------------------------------ 수신 연결

    def event_handler_for(self, owner_mmsi: int):
        """owner 국의 ServiceEvent 처리 callable을 만든다.

        조립자(소프트웨어 그룹 또는 Stage 7 launcher/integration)가 이것을
        `HandlerServiceEventSink`로 감싸 endpoint에 연결한다.
        """

        def handle(event) -> None:
            bridge.handle_service_event(
                event, on_data=lambda inbound: self._on_inbound(owner_mmsi, inbound)
            )

        return handle

    def _on_inbound(self, owner_mmsi: int, inbound) -> None:
        message_id = bridge.message_id_for_payload_format(inbound.payload_format)
        if message_id is None:
            return
        # 행위자는 봉투가 붙은 payload를 기대하므로 도로 붙여 준다.
        self._arrivals.append(
            Arrival(
                payload=add_envelope(inbound.payload, message_id),
                source_id=self._mmsi_for_station(inbound.source_id),
                destination_id=owner_mmsi,
            )
        )

    # -------------------------------------------------------------- 송신

    def send(self, payload, *, source_id, destination_id, data_type=None) -> tuple[bool, str]:
        service = self._service_by_mmsi.get(source_id)
        station = self._station_by_mmsi.get(destination_id)
        if service is None or station is None:
            return False, f"VDETER에 없는 station (from {source_id} to {destination_id})"

        # data_type이 없으면 보낼 수 없다. 등록된 payload_format이 있어야 한다.
        fmt = bridge.PAYLOAD_FORMAT_BY_DATA_TYPE.get(data_type) if data_type else None
        if fmt is None:
            return False, f"등록된 payload_format이 없는 data_type: {data_type!r}"

        # 위치보고는 no-ACK 전용이다(DR-PAYLOAD-016). ack_required=True로 내면
        # INVALID_REQUEST로 거부된다. 명령·ACK은 링크 ACK을 받는다.
        ack_required = fmt != bridge.PAYLOAD_FORMAT_POSITION_REPORT

        # 행위자 payload에는 우리 봉투가 붙어 있다. VDETER에는 본문만 준다.
        # DTO 변환·계약 검사(길이·봉투 금지·addressed 전용)는 bridge가 맡는다.
        try:
            application, _ = strip_envelope(payload)
            request = bridge.to_submit_request(
                Outbound(
                    payload=application,
                    destination_id=station,
                    ack_required=ack_required,
                    data_type=data_type,
                )
            )
        except ValueError as exc:
            return False, str(exc)

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

    # VDETER가 알려주는 송신 station을 우리 MMSI로 되돌린다.
    def _mmsi_for_station(self, station_value) -> int:
        for mmsi, station in self._station_by_mmsi.items():
            if station == station_value:
                return mmsi
        return int(station_value)

    # -------------------------------------------------------------- 진행

    # 하위 클래스가 시계를 직접 쥐고 있으면 여기서 진행시킨다. 외부 주입
    # 경로는 상대가 자기 시계로 돌므로 기본은 아무것도 하지 않는다.
    def _advance(self) -> None:
        pass

    def pump(self) -> list[Arrival]:
        """전송을 진행시키고, 이번에 도착한 payload를 돌려준다."""
        if self._inflight:
            self._advance()
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
        pass


class SoftwareRadioTransport(_ServiceApiTransport):
    """Software Radio 기반 VDETER 그룹을 직접 조립해 쓰는 전송 경로.

    구조는 loopback 데모(`apps/_loopback_opaque.py`)와 같다 — 공식
    `HandlerServiceEventSink`로 endpoint에 붙고, `submit()`으로 내보내고,
    `status()`로 완료를 본다. 다른 점은 **그룹을 하나만 만들어 계속 쓴다**는
    것이다(데모는 전송마다 새로 만든다). 연속 전송이 되는 것은 확인했다.
    """

    name = "software"
    detail = "VDETER 소프트웨어 무선 (Service API 종단)"
    operational = True

    def __init__(self, mass_mmsi: int, roc_mmsi: int,
                 intruder_mmsi: int | None = None) -> None:
        try:
            sys.path.insert(0, os.path.join(_ROOT, "VDETER"))
            from tests.support.stage5 import (
                MASS_A_STATION_ID,
                MASS_B_STATION_ID,
                ROC_STATION_ID,
                STATION_ENDPOINT_IDS,
                build_stage5_software_group,
                drive_stage5_group,
                start_stage5_group,
            )
            from vdeter.service import HandlerServiceEventSink
        except ImportError as exc:  # pragma: no cover
            raise VdeterUnavailable(f"VDETER를 불러올 수 없습니다: {exc}") from exc

        stations = {
            mass_mmsi: MASS_A_STATION_ID.value,
            roc_mmsi: ROC_STATION_ID.value,
        }
        endpoint_owner = {
            STATION_ENDPOINT_IDS[MASS_A_STATION_ID.value]: mass_mmsi,
            STATION_ENDPOINT_IDS[ROC_STATION_ID.value]: roc_mmsi,
        }
        # 비인가 송신국은 실제로 **또 하나의 무선국**이다. 응용에서 만들어낸
        # 가상의 송신자가 아니라 같은 채널에 있는 제3국이라야 "송신자 인증이
        # 없다"는 사실이 그대로 재현된다. Stage 5 그룹의 MASS-B를 쓴다.
        if intruder_mmsi is not None:
            stations[intruder_mmsi] = MASS_B_STATION_ID.value
            endpoint_owner[
                STATION_ENDPOINT_IDS[MASS_B_STATION_ID.value]
            ] = intruder_mmsi

        super().__init__(stations)
        self._drive = drive_stage5_group

        def sink_factory(endpoint_id):
            owner = endpoint_owner.get(endpoint_id)
            if owner is None:
                return HandlerServiceEventSink(lambda _event: None)
            return HandlerServiceEventSink(self.event_handler_for(owner))

        try:
            self._clock, self._group = build_stage5_software_group(
                service_sink_factory=sink_factory
            )
            start_stage5_group(self._clock, self._group)
        except Exception as exc:  # pragma: no cover - cryptography 등 조립 실패
            raise VdeterUnavailable(f"소프트웨어 무선 그룹 조립 실패: {exc}") from exc

        by_station = self._group.by_station()
        self._service_by_mmsi = {
            mmsi: by_station[station]
            for mmsi, station in self._station_by_mmsi.items()
        }

    def _advance(self) -> None:
        self._drive(self._clock, self._group, CLOCK_STEPS_PER_TICK)

    def close(self) -> None:
        try:
            self._group.stop()
        except Exception:  # pragma: no cover
            pass


class ExternalServiceTransport(_ServiceApiTransport):
    """외부(launcher/integration)가 조립한 VDE-TER Service를 주입받는 전송 경로.

    Stage 7 실장비든 다른 배치든, `VdeService` 구현과 station 번호만 주면 된다.
    이 클래스는 B210·Radio·PHY·MAC·Link를 전혀 알지 않는다.

    조립 순서(모듈 docstring의 Stage 7 주입 seam 참조):
        transport = ExternalServiceTransport(station_by_mmsi={...})
        sink = HandlerServiceEventSink(transport.event_handler_for(mmsi))
        ... build_stage7_b210_endpoint(..., application_sink=sink) ...
        transport.attach_service(mmsi, endpoint)
    """

    name = "stage7"
    detail = "외부 주입 Stage 7 VDE-TER Service"
    operational = True

    def __init__(
        self,
        station_by_mmsi: dict[int, int],
        services_by_mmsi: dict[int, object] | None = None,
        *,
        advance=None,
    ) -> None:
        super().__init__(station_by_mmsi)
        #: 주입자가 시계/스케줄을 손으로 돌려야 하는 배치를 위한 hook(선택).
        self._advance_hook = advance
        for mmsi, service in (services_by_mmsi or {}).items():
            self.attach_service(mmsi, service)

    def attach_service(self, mmsi: int, service) -> None:
        """조립이 끝난 `VdeService`를 이 국의 송신 경로로 등록한다."""
        if mmsi not in self._station_by_mmsi:
            raise ValueError(f"station_by_mmsi에 없는 MMSI입니다: {mmsi}")
        self._service_by_mmsi[mmsi] = service

    def _advance(self) -> None:
        if self._advance_hook is not None:
            self._advance_hook()


def build_transport(kind: str, *, mass_mmsi: int, roc_mmsi: int,
                    intruder_mmsi: int | None = None):
    """`kind`에 맞는 전송 경로를 만든다. 조립할 수 없으면 사유와 함께 direct로 내려간다.

    "vdeter"는 이전 이름이며 software와 같다. "stage7"은 외부 주입이 필요해
    콘솔 CLI에서 바로 조립할 수 없다 — `ExternalServiceTransport`를 코드로
    구성해야 하며, 별도 프로세스 launcher에 접속하는 공개 RPC는 아직 없다.
    intruder_mmsi를 주면 software 경로에서 비인가 송신국(MASS-B)이 함께 선다.
    """
    if kind == "direct":
        return DirectTransport(), ""
    if kind in ("stage7", "external"):
        return DirectTransport(), (
            "stage7 전송은 외부에서 Service를 주입해야 한다 "
            "(ExternalServiceTransport 참조). 별도 프로세스 Stage 7 launcher에 "
            "접속하는 응용 RPC는 공개 API에 없다 — software 모드를 쓰거나 "
            "in-process 조립으로 주입하라."
        )
    try:
        return SoftwareRadioTransport(
            mass_mmsi=mass_mmsi, roc_mmsi=roc_mmsi, intruder_mmsi=intruder_mmsi
        ), ""
    except VdeterUnavailable as exc:
        return DirectTransport(), str(exc)


#: 이전 이름과의 호환. 기존 코드가 VdeterTransport를 import해도 계속 동작한다.
VdeterTransport = SoftwareRadioTransport
