"""Service ↔ Presentation ↔ Link 경계 DTO (`stage0_baseline.md` §5.3).

Presentation Adapter는 application payload와 VDE payload 표현 사이의 변환만 소유한다.
`payload_format`은 별도 Link metadata로 전달하지 않고 Presentation data의 VPFI로 표현한다
(상위 명세 §7.3). 실제 VPFI 할당과 codec은 단계 5 Presentation 기준선이 확정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..addressing import RouteAddresses
from ..exceptions import InvalidValueError
from ..identifiers import PayloadFormat, TransferId, Vpfi
from ..time import TimePoint


@dataclass(frozen=True, slots=True)
class PresentationEncodeRequest:
    """송신 payload 준비 요청."""

    transfer_id: TransferId
    route: RouteAddresses
    payload_format: PayloadFormat
    application_payload: bytes
    ack_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("PresentationEncodeRequest.transfer_id: TransferId여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("PresentationEncodeRequest.route: RouteAddresses여야 합니다")
        if not isinstance(self.payload_format, PayloadFormat):
            raise InvalidValueError(
                "PresentationEncodeRequest.payload_format: PayloadFormat이어야 합니다"
            )
        if not isinstance(self.application_payload, bytes):
            raise InvalidValueError(
                "PresentationEncodeRequest.application_payload: bytes여야 합니다"
            )
        if not isinstance(self.ack_required, bool):
            raise InvalidValueError("PresentationEncodeRequest.ack_required: bool이어야 합니다")


@dataclass(frozen=True, slots=True)
class PresentationData:
    """VPFI와 payload 표현이 적용된 완전한 Presentation data.

    `payload_format`은 여기에 담지 않는다. 수신 측은 VPFI와 `message_format`으로 local
    `payload_format`을 복원한다.
    """

    transfer_id: TransferId
    route: RouteAddresses
    vpfi: Vpfi
    data: bytes
    message_format: int | None = None
    ack_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("PresentationData.transfer_id: TransferId여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("PresentationData.route: RouteAddresses여야 합니다")
        if not isinstance(self.vpfi, Vpfi):
            raise InvalidValueError("PresentationData.vpfi: Vpfi여야 합니다")
        if not isinstance(self.data, bytes):
            raise InvalidValueError("PresentationData.data: bytes여야 합니다")
        if self.message_format is not None:
            if isinstance(self.message_format, bool) or not isinstance(self.message_format, int):
                raise InvalidValueError("PresentationData.message_format: 정수여야 합니다")
            if self.message_format < 0:
                raise InvalidValueError("PresentationData.message_format: 0 이상이어야 합니다")
        if not isinstance(self.ack_required, bool):
            raise InvalidValueError("PresentationData.ack_required: bool이어야 합니다")


@dataclass(frozen=True, slots=True)
class PresentationDecodeRequest:
    """수신 payload 복원 요청. 검증과 재조립을 마친 data만 전달한다."""

    route: RouteAddresses
    data: bytes
    received_at: TimePoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("PresentationDecodeRequest.route: RouteAddresses여야 합니다")
        if not isinstance(self.data, bytes):
            raise InvalidValueError("PresentationDecodeRequest.data: bytes여야 합니다")
        if self.received_at is not None and not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("PresentationDecodeRequest.received_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class ApplicationData:
    """복원된 application payload와 end-to-end 주소."""

    route: RouteAddresses
    payload_format: PayloadFormat
    application_payload: bytes
    received_at: TimePoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("ApplicationData.route: RouteAddresses여야 합니다")
        if not isinstance(self.payload_format, PayloadFormat):
            raise InvalidValueError("ApplicationData.payload_format: PayloadFormat이어야 합니다")
        if not isinstance(self.application_payload, bytes):
            raise InvalidValueError("ApplicationData.application_payload: bytes여야 합니다")
        if self.received_at is not None and not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("ApplicationData.received_at: TimePoint여야 합니다")
