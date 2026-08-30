"""VDETER Service API 연동 브리지 (실 링크 연동).

`docs/vdeter_mass_roc_api_guide.md`가 정한 공개 계약과 우리 행위자를 잇는다.
행위자는 `Outbound`(payload·수신처·ACK 필요 여부)만 만들고, 이 모듈이 그것을
VDETER의 타입 DTO `SubmitTransferRequest`로 바꾼다. 반대로 VDETER가 올려주는
`ServiceEvent(DATA_RECEIVED)`를 우리 응용이 다룰 `Inbound`로 바꾼다.

경계 (API 가이드 §1.1)
    - 응용은 payload를 **VDE header 없는 완성 bytes**로 넘긴다. Link ID·Message
      Type·CRC·VPFI·slot은 만들지도 전달하지도 않는다.
    - 응용은 `VdeService` 구현을 직접 만들지 않는다. launcher/integration이
      event sink와 연결해 주입한 `VdeService`를 받아 쓴다.

payload_format 매핑
    우리 `Outbound.data_type`(운항 분류)을 VDETER registry의 payload_format
    문자열 키로 바꾼다. **운영 지원되는 두 가지만** 매핑한다. 나머지(항로·경보·
    환경·기상)는 registry에 format이 없어 아직 보낼 수 없다(가이드 §8).

vdeter import
    운영 환경에서는 launcher가 `vdeter`를 import 가능하게 준비한다. 저장소
    안에서 바로 쓸 수 있도록, import 실패 시 in-repo `VDETER/src`를 한 번
    시도한다. 그래도 없으면 `VDETER_AVAILABLE=False`로 두고, DTO를 만드는
    함수는 명확한 오류를 낸다(우리 다른 코드의 import는 깨지 않는다).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .outbound import (
    BROADCAST_ID,
    DATA_TYPE_AIS_AUTH_INTEGRITY,
    DATA_TYPE_POSITION_REPORT,
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
    DATA_TYPE_X509_CERTIFICATE,
    Outbound,
)

# --------------------------------------------------------------------------
# vdeter import (가드)
# --------------------------------------------------------------------------


def _vdeter_submodule_ok() -> bool:
    # bare `import vdeter`는 대소문자 무시 파일시스템에서 in-repo `VDETER/`
    # 디렉터리에 namespace로 절반만 걸려 거짓 성공할 수 있다. 실제로 쓰는
    # 서브모듈까지 import돼야 사용 가능으로 본다.
    try:
        import vdeter.common.contracts.service  # noqa: F401
        return True
    except ImportError:
        return False


def _try_import_vdeter():
    if _vdeter_submodule_ok():
        return True
    # in-repo 소스를 얹고, namespace로 절반 잡힌 캐시를 지운 뒤 다시 시도한다.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(here, "VDETER", "src")
    if not os.path.isdir(candidate):
        return False
    if candidate not in sys.path:
        sys.path.insert(0, candidate)
    for name in [n for n in sys.modules if n == "vdeter" or n.startswith("vdeter.")]:
        del sys.modules[name]
    return _vdeter_submodule_ok()


VDETER_AVAILABLE = _try_import_vdeter()

if VDETER_AVAILABLE:
    from vdeter.common.addressing import Addressed, Broadcast
    from vdeter.common.contracts.service import (
        ReceivedApplicationData,
        SubmitTransferRequest,
    )
    from vdeter.common.events import ServiceEvent, ServiceEventType
    from vdeter.common.identifiers import PayloadFormat, StationId


# --------------------------------------------------------------------------
# payload_format 매핑 (가이드 §8)
# --------------------------------------------------------------------------

#: 운영 지원되는 VDETER payload_format 키
PAYLOAD_FORMAT_CERTIFICATE_REPORT = "x509.certificate_report"
PAYLOAD_FORMAT_AIS_SCHEME1 = "ais.authentication_integrity.scheme1"

#: SC-VDE 노선·조종·위치보고 (TEST/PROJECT scope 전용, 가이드 §8.1)
PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND = "sc_vde.route_control.command"
PAYLOAD_FORMAT_ROUTE_CONTROL_ACK = "sc_vde.route_control.ack"
PAYLOAD_FORMAT_POSITION_REPORT = "sc_vde.position_report"

#: TEST scope에서만 조회되는 키. OPERATIONAL endpoint에서는
#: UNSUPPORTED_REQUEST가 정상 결과다(가이드 §8.1).
TEST_SCOPE_PAYLOAD_FORMATS = frozenset(
    {
        PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND,
        PAYLOAD_FORMAT_ROUTE_CONTROL_ACK,
        PAYLOAD_FORMAT_POSITION_REPORT,
    }
)

#: 각 SC-VDE format이 싣는 Message Format(= 우리 Message ID).
#: 수신 시 payload_format이 곧 message_id를 알려준다(application bytes에 없으므로).
MESSAGE_ID_BY_PAYLOAD_FORMAT = {
    PAYLOAD_FORMAT_POSITION_REPORT: 1,
    PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND: 2,
    PAYLOAD_FORMAT_ROUTE_CONTROL_ACK: 3,
}

#: 우리 data_type → VDETER payload_format 문자열.
PAYLOAD_FORMAT_BY_DATA_TYPE = {
    DATA_TYPE_X509_CERTIFICATE: PAYLOAD_FORMAT_CERTIFICATE_REPORT,
    DATA_TYPE_AIS_AUTH_INTEGRITY: PAYLOAD_FORMAT_AIS_SCHEME1,
    DATA_TYPE_ROUTE_CONTROL_COMMAND: PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND,
    DATA_TYPE_ROUTE_CONTROL_ACK: PAYLOAD_FORMAT_ROUTE_CONTROL_ACK,
    DATA_TYPE_POSITION_REPORT: PAYLOAD_FORMAT_POSITION_REPORT,
}

#: 위치보고 application 길이 (본문 111 bit + padding 1 bit)
SC_VDE_POSITION_REPORT_LENGTH = 14

#: SC-VDE command의 허용 application byte 길이 (가이드 §8.3, n=0..16)
SC_VDE_COMMAND_LENGTHS = frozenset(
    {10, 20, 29, 39, 48, 58, 68, 77, 87, 97, 106, 116, 125, 135, 145, 154, 164}
)

#: SC-VDE application ACK은 정확히 3 bytes (가이드 §8.4)
SC_VDE_ACK_LENGTH = 3


class UnsupportedPayloadFormat(ValueError):
    """VDETER registry가 아직 지원하지 않는 data_type."""


def _require_vdeter() -> None:
    if not VDETER_AVAILABLE:
        raise RuntimeError(
            "vdeter 패키지를 import할 수 없습니다. launcher가 vdeter를 제공하거나 "
            "VDETER/src가 있어야 합니다."
        )


def payload_format_for(data_type: str | None):
    """data_type을 VDETER `PayloadFormat`으로 바꾼다. 미지원이면 오류."""
    _require_vdeter()
    if data_type is None:
        raise UnsupportedPayloadFormat(
            "data_type이 없어 payload_format을 정할 수 없습니다. 운항 메시지"
            "(위치보고·항로·경보 등)는 아직 VDETER registry에 format이 없습니다."
        )
    key = PAYLOAD_FORMAT_BY_DATA_TYPE.get(data_type)
    if key is None:
        raise UnsupportedPayloadFormat(
            f"VDETER가 지원하지 않는 data_type입니다: {data_type!r}. "
            f"현재 지원: {sorted(PAYLOAD_FORMAT_BY_DATA_TYPE)}"
        )
    return PayloadFormat(key)


# --------------------------------------------------------------------------
# 송신: Outbound → SubmitTransferRequest
# --------------------------------------------------------------------------


#: VDETER가 wire에 붙이는 header. application bytes에 들어 있으면 안 된다.
_WIRE_PREFIXES = (bytes.fromhex("ff7f02"), bytes.fromhex("ff7f03"))
#: 우리가 예전에 쓰던 임시 VPFI. 수신 fallback으로도 허용하지 않는다.
_LEGACY_PREFIX = bytes.fromhex("ff01")


def check_sc_vde_payload(payload_format: str, payload: bytes) -> None:
    """SC-VDE application bytes가 계약을 지키는지 확인한다(가이드 §8.2·§8.3·§8.4).

    봉투(`ff 7f 02/03`)나 임시 VPFI(`ff 01`)를 앞에 붙이면 VDETER가 구조 오류
    또는 unknown wire key로 거부하므로, 그 전에 여기서 막는다.
    """
    if payload_format not in TEST_SCOPE_PAYLOAD_FORMATS:
        return

    payload = bytes(payload)
    if payload.startswith(_WIRE_PREFIXES) or payload.startswith(_LEGACY_PREFIX):
        raise ValueError(
            "application bytes에 VPFI/Message Format 봉투가 들어 있습니다. "
            "VDETER가 붙이므로 본문만 넣어야 합니다 (codec.encode_body 사용)."
        )

    if payload_format == PAYLOAD_FORMAT_POSITION_REPORT:
        if len(payload) != SC_VDE_POSITION_REPORT_LENGTH:
            raise ValueError(
                f"위치보고는 정확히 {SC_VDE_POSITION_REPORT_LENGTH} bytes여야 합니다 "
                f"(현재 {len(payload)})"
            )
    elif payload_format == PAYLOAD_FORMAT_ROUTE_CONTROL_ACK:
        if len(payload) != SC_VDE_ACK_LENGTH:
            raise ValueError(
                f"application ACK은 정확히 {SC_VDE_ACK_LENGTH} bytes여야 합니다 "
                f"(현재 {len(payload)})"
            )
    elif len(payload) not in SC_VDE_COMMAND_LENGTHS:
        raise ValueError(
            f"SC-VDE command 길이가 허용값이 아닙니다 ({len(payload)} bytes). "
            f"허용: {sorted(SC_VDE_COMMAND_LENGTHS)}"
        )


def message_id_for_payload_format(payload_format: str) -> int | None:
    """수신한 payload_format이 알려주는 Message Format(= Message ID)."""
    return MESSAGE_ID_BY_PAYLOAD_FORMAT.get(payload_format)


def to_submit_request(outbound: Outbound):
    """우리 `Outbound`를 VDETER `SubmitTransferRequest`로 바꾼다.

    payload는 그대로 넘긴다(VDE header 없는 완성 bytes). 수신처가
    broadcast면 `Broadcast()`, 아니면 `Addressed(StationId(...))`.
    """
    _require_vdeter()
    payload_format = payload_format_for(outbound.data_type)

    # SC-VDE 노선·조종은 addressed 전용이고 application 길이가 정해져 있다
    # (가이드 §8.3·§8.4). 어기면 VDETER가 거부하므로 여기서 먼저 막는다.
    check_sc_vde_payload(payload_format.value, outbound.payload)
    if payload_format.value in TEST_SCOPE_PAYLOAD_FORMATS:
        if outbound.destination_id == BROADCAST_ID:
            raise ValueError(
                f"{payload_format.value}는 addressed 전용입니다 (broadcast 금지)"
            )

    if outbound.destination_id == BROADCAST_ID:
        destination = Broadcast()
    else:
        destination = Addressed(StationId(outbound.destination_id))

    return SubmitTransferRequest(
        destination=destination,
        payload_format=payload_format,
        payload=bytes(outbound.payload),
        ack_required=outbound.ack_required,
    )


# --------------------------------------------------------------------------
# 수신: ReceivedApplicationData → Inbound
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Inbound:
    """VDETER 수신 통지를 응용이 다루기 쉬운 형태로 편 것."""

    source_id: int              # end-to-end 최초 송신 station
    payload_format: str         # VDETER payload_format 키
    payload: bytes              # 복원된 application bytes
    is_broadcast: bool          # 최종 수신 대상이 broadcast였는지

    @property
    def is_certificate_report(self) -> bool:
        return self.payload_format == PAYLOAD_FORMAT_CERTIFICATE_REPORT

    @property
    def is_ais_scheme1(self) -> bool:
        return self.payload_format == PAYLOAD_FORMAT_AIS_SCHEME1


def inbound_from_received(received) -> Inbound:
    """VDETER `ReceivedApplicationData`를 `Inbound`로 바꾼다."""
    _require_vdeter()
    if not isinstance(received, ReceivedApplicationData):
        raise TypeError("ReceivedApplicationData가 아닙니다")
    return Inbound(
        source_id=received.original_source.value,
        payload_format=received.payload_format.value,
        payload=bytes(received.payload),
        is_broadcast=received.original_destination.is_broadcast,
    )


def handle_service_event(event, *, on_data=None, on_state=None, on_error=None):
    """VDETER `ServiceEvent` 하나를 종류별 콜백으로 분기한다.

    launcher가 준 `HandlerServiceEventSink`에 이 함수를 부분 적용해 연결한다.
    `on_data`는 `Inbound`를, `on_state`는 (transfer_id, transfer_state)를,
    `on_error`는 ServiceError를 받는다.
    """
    _require_vdeter()
    if not isinstance(event, ServiceEvent):
        raise TypeError("ServiceEvent가 아닙니다")

    if event.event_type is ServiceEventType.DATA_RECEIVED:
        if event.received_data is None:
            raise ValueError("DATA_RECEIVED에 received_data가 없습니다")
        if on_data is not None:
            on_data(inbound_from_received(event.received_data))
    elif event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED:
        if on_state is not None:
            on_state(event.transfer_id, event.transfer_state)
    elif event.event_type is ServiceEventType.ERROR:
        if on_error is not None:
            on_error(event.error)
