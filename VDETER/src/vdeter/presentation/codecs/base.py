"""Stage 5 Presentation codec 공통 계약 (`stage5_service_integration_baseline.md` §6).

codec은 application bytes와 완전한 VPFI message bytes 사이의 변환만 소유한다.
Segment, fragment, CRC, ACK, Session, slot 또는 재전송은 다루지 않는다 (`PRES-REQ-01..06`).

각 codec은 다음을 제공한다.

- registry `codec_ref`와 wire metadata (VPFI, message format)
- 수신 discriminator용 고정 wire prefix (`stage5` §6.1.1)
- 결정적 encoded 크기 상한 (`encoded_size_bound`) — Service pre-admission 검증용
- `encode(application bytes) -> wire bytes`
- `decode(wire bytes) -> application bytes` (byte-for-byte 복원)
"""

from __future__ import annotations

from dataclasses import dataclass

from ...common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
)
from ...common.exceptions import InvalidValueError
from ...common.results import internal_error

#: Stage 5 공통 application payload 상한 (`DR-STAGE5-002`).
MAX_APPLICATION_BYTES = 65_531
#: Stage 1 VDE Segment Payload Size 상한.
MAX_ENCODED_BYTES = 65_535


@dataclass(frozen=True, slots=True)
class WireDiscriminator:
    """수신 prefix 판별자. byte prefix와 선택적 세 번째 byte의 bit pattern."""

    prefix: bytes
    third_byte_mask: int = 0
    third_byte_value: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, bytes) or not self.prefix:
            raise InvalidValueError("WireDiscriminator.prefix: 비어 있지 않은 bytes여야 합니다")
        for name in ("third_byte_mask", "third_byte_value"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise InvalidValueError(f"WireDiscriminator.{name}: 0..255 정수여야 합니다")
        if self.third_byte_mask and len(self.prefix) != 2:
            raise InvalidValueError(
                "WireDiscriminator: third byte bit pattern은 2-byte prefix에만 결합합니다"
            )

    def conflicts_with(self, other: "WireDiscriminator") -> bool:
        """두 판별자가 같은 wire bytes를 동시에 수락할 수 있으면 True."""

        common = min(len(self.prefix), len(other.prefix))
        if self.prefix[:common] != other.prefix[:common]:
            return False
        if self.third_byte_mask and other.third_byte_mask:
            shared_mask = self.third_byte_mask & other.third_byte_mask
            if (self.third_byte_value & shared_mask) != (other.third_byte_value & shared_mask):
                return False
        return True


def presentation_error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    correlation: Correlation = Correlation(),
    **details: object,
):
    return internal_error(
        Layer.PRESENTATION,
        code,
        category,
        message,
        correlation=correlation,
        details=Details.of(**details) if details else Details(),
    )


def encode_structure_error(message: str, *, correlation: Correlation = Correlation(), **details):
    """송신 application bytes의 형식 구조 오류. admission 전 `INVALID_REQUEST`로 projection된다."""

    return presentation_error(
        ErrorCode.INVALID_REQUEST,
        ErrorCategory.VALIDATION,
        message,
        correlation=correlation,
        **details,
    )


def decode_structure_error(message: str, *, correlation: Correlation = Correlation(), **details):
    """수신 wire bytes의 known-codec 구조 오류. `TRANSFER_FAILED/PROTOCOL`로 projection된다."""

    return presentation_error(
        ErrorCode.PRESENTATION_STRUCTURE_INVALID,
        ErrorCategory.PROTOCOL,
        message,
        correlation=correlation,
        **details,
    )


def truncated_error(message: str, *, correlation: Correlation = Correlation(), **details):
    """수신 wire bytes truncation. `TRANSFER_FAILED/PROTOCOL`로 projection된다."""

    return presentation_error(
        ErrorCode.PRESENTATION_TRUNCATED,
        ErrorCategory.PROTOCOL,
        message,
        correlation=correlation,
        **details,
    )
