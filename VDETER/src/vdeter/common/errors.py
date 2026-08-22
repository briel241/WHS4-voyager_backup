"""구조화된 내부 오류와 Service 공개 오류 (`stage0_baseline.md` §5.2, §6.3).

내부 계층은 `InternalProtocolError`로 layer, machine-readable code, category와 가능한 모든
correlation을 보존한다. Service 경계에서는 Runtime의 projection이 이를 `ServiceError`로
변환하며 내부 전용 correlation과 details를 제거한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .exceptions import InvalidValueError
from .identifiers import BurstId, PacketId, SequentialId, SessionHandle, TransferId


class Layer(str, Enum):
    COMMON = "COMMON"
    SERVICE = "SERVICE"
    PRESENTATION = "PRESENTATION"
    LINK = "LINK"
    MAC = "MAC"
    PHY = "PHY"
    RADIO = "RADIO"
    RUNTIME = "RUNTIME"


class ErrorCategory(str, Enum):
    VALIDATION = "VALIDATION"
    UNSUPPORTED = "UNSUPPORTED"
    PROTOCOL = "PROTOCOL"
    TIMEOUT = "TIMEOUT"
    RESOURCE = "RESOURCE"
    DEVICE = "DEVICE"
    INTERNAL = "INTERNAL"


class ServiceErrorCategory(str, Enum):
    """Client에 노출할 수 있는 공개 category."""

    VALIDATION = "VALIDATION"
    UNSUPPORTED = "UNSUPPORTED"
    PROTOCOL = "PROTOCOL"
    TIMEOUT = "TIMEOUT"
    RESOURCE = "RESOURCE"
    DEVICE = "DEVICE"
    INTERNAL = "INTERNAL"


# --- details 제약 (기준선 §6.3) ---

DETAILS_MAX_ENTRIES = 32
DETAILS_MAX_STRING_BYTES = 1024
DETAILS_MAX_JSON_BYTES = 16 * 1024
_DETAIL_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

#: 크기와 무관하게 details에 넣을 수 없는 key. 길이·digest·경로만 기록한다.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "payload",
        "payload_bytes",
        "payload_data",
        "payload_hex",
        "payload_text",
        "application_payload",
        "presentation_data",
        "packet_bytes",
        "message_bytes",
        "samples",
        "sample_data",
        "iq",
        "iq_data",
        "baseband",
        "certificate",
        "certificate_body",
        "device_handle",
        "memory_address",
        # 내부 correlation 식별자는 Details가 아니라 Correlation에만 둔다. 이 금지는
        # ServiceError/ServiceEvent를 직접 구성하는 경로에서도 내부 ID가 새는 것을 막는다.
        "session_handle",
        "sequential_id",
        "packet_id",
        "fragment_id",
        "fragment_index",
        "fragment_count",
        "burst_id",
    }
)
_FORBIDDEN_DETAIL_SUBSTRINGS = (
    "secret",
    "password",
    "passphrase",
    "private_key",
    "signing_key",
    "token",
    "credential",
)


def _validate_detail_scalar(key: str, value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float):
        raise InvalidValueError(f"details[{key}]: floating-point 값은 허용하지 않습니다")
    if isinstance(value, int):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > DETAILS_MAX_STRING_BYTES:
            raise InvalidValueError(
                f"details[{key}]: 문자열이 {DETAILS_MAX_STRING_BYTES} bytes를 넘습니다"
            )
        return
    raise InvalidValueError(
        f"details[{key}]: str/int/bool/None 또는 그 tuple만 허용합니다 "
        f"({type(value).__name__})"
    )


def _validate_detail_value(key: str, value: object) -> None:
    if isinstance(value, tuple):
        for item in value:
            _validate_detail_scalar(key, item)
        return
    _validate_detail_scalar(key, value)


@dataclass(frozen=True, slots=True)
class Details:
    """portable key/value snapshot. 중첩 mapping, object, float, repr()을 허용하지 않는다."""

    entries: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise InvalidValueError("Details.entries: tuple이어야 합니다")
        if len(self.entries) > DETAILS_MAX_ENTRIES:
            raise InvalidValueError(
                f"Details: entry는 최대 {DETAILS_MAX_ENTRIES}개입니다 ({len(self.entries)})"
            )
        seen = set()
        for entry in self.entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise InvalidValueError("Details.entries: (key, value) tuple이어야 합니다")
            key, value = entry
            if not isinstance(key, str) or not _DETAIL_KEY_PATTERN.match(key):
                raise InvalidValueError(
                    f"details key: ASCII lower snake case 1..64자여야 합니다 ({key!r})"
                )
            if key in seen:
                raise InvalidValueError(f"details key 중복: {key}")
            seen.add(key)
            if key in FORBIDDEN_DETAIL_KEYS or any(
                token in key for token in _FORBIDDEN_DETAIL_SUBSTRINGS
            ):
                raise InvalidValueError(
                    f"details key {key!r}: payload/secret/sample 내용은 기록하지 않습니다"
                )
            _validate_detail_value(key, value)
        encoded = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > DETAILS_MAX_JSON_BYTES:
            raise InvalidValueError(
                f"Details: JSON 표현이 {DETAILS_MAX_JSON_BYTES} bytes를 넘습니다"
            )

    @classmethod
    def of(cls, **values: object) -> "Details":
        return cls(tuple(sorted(values.items())))

    @classmethod
    def from_mapping(cls, values: dict) -> "Details":
        entries = []
        for key, value in values.items():
            entries.append((key, tuple(value) if isinstance(value, list) else value))
        return cls(tuple(sorted(entries, key=lambda item: item[0])))

    def as_dict(self) -> dict:
        return {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in self.entries
        }

    def get(self, key: str, default: object = None) -> object:
        for entry_key, value in self.entries:
            if entry_key == key:
                return value
        return default

    def keys(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.entries)

    def is_empty(self) -> bool:
        return not self.entries


EMPTY_DETAILS = Details()


@dataclass(frozen=True, slots=True)
class Correlation:
    """계층 사이와 보호된 내부 log에서 보존하는 추적 식별자 묶음.

    wire Session ID는 로컬 추적값으로 사용하지 않으므로 이 DTO에 포함하지 않는다.
    """

    transfer_id: TransferId | None = None
    session_handle: SessionHandle | None = None
    sequential_id: SequentialId | None = None
    packet_id: PacketId | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None
    burst_id: BurstId | None = None

    def __post_init__(self) -> None:
        for name, expected in (
            ("transfer_id", TransferId),
            ("session_handle", SessionHandle),
            ("sequential_id", SequentialId),
            ("packet_id", PacketId),
            ("burst_id", BurstId),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise InvalidValueError(f"Correlation.{name}: {expected.__name__}가 아닙니다")
        for name in ("fragment_index", "fragment_count"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidValueError(f"Correlation.{name}: 0 이상의 정수여야 합니다")

    FIELD_NAMES = (
        "transfer_id",
        "session_handle",
        "sequential_id",
        "packet_id",
        "fragment_index",
        "fragment_count",
        "burst_id",
    )

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in Correlation.FIELD_NAMES)

    def merged(self, other: "Correlation") -> "Correlation":
        """비어 있는 field만 other의 값으로 채운다."""

        def pick(name: str):
            value = getattr(self, name)
            return value if value is not None else getattr(other, name)

        return Correlation(
            transfer_id=pick("transfer_id"),
            session_handle=pick("session_handle"),
            sequential_id=pick("sequential_id"),
            packet_id=pick("packet_id"),
            fragment_index=pick("fragment_index"),
            fragment_count=pick("fragment_count"),
            burst_id=pick("burst_id"),
        )


EMPTY_CORRELATION = Correlation()


def _validate_code(code: str, field_name: str) -> None:
    if not isinstance(code, str) or not _CODE_PATTERN.match(code):
        raise InvalidValueError(f"{field_name}: 안정된 대문자 machine code여야 합니다 ({code!r})")


@dataclass(frozen=True, slots=True)
class InternalProtocolError:
    """계층 경계를 넘는 내부 오류. raw exception 대신 이 DTO를 결과로 반환한다."""

    layer: Layer
    code: str
    category: ErrorCategory
    message: str
    retryable: bool = False
    correlation: Correlation = field(default=EMPTY_CORRELATION)
    details: Details = field(default=EMPTY_DETAILS)

    def __post_init__(self) -> None:
        if not isinstance(self.layer, Layer):
            raise InvalidValueError(f"InternalProtocolError.layer: Layer가 아닙니다 ({self.layer!r})")
        if not isinstance(self.category, ErrorCategory):
            raise InvalidValueError("InternalProtocolError.category: ErrorCategory가 아닙니다")
        _validate_code(self.code, "InternalProtocolError.code")
        if not isinstance(self.message, str) or not self.message:
            raise InvalidValueError(
                "InternalProtocolError.message: 비어 있지 않은 문자열이어야 합니다"
            )
        if not isinstance(self.retryable, bool):
            raise InvalidValueError("InternalProtocolError.retryable: bool이어야 합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("InternalProtocolError.correlation: Correlation이 아닙니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("InternalProtocolError.details: Details가 아닙니다")

    def with_correlation(self, correlation: Correlation) -> "InternalProtocolError":
        """비어 있는 correlation field만 채운 새 오류를 만든다."""

        return InternalProtocolError(
            layer=self.layer,
            code=self.code,
            category=self.category,
            message=self.message,
            retryable=self.retryable,
            correlation=self.correlation.merged(correlation),
            details=self.details,
        )


@dataclass(frozen=True, slots=True)
class ServiceError:
    """Client에 노출하는 공개 오류. 내부 correlation을 포함하지 않는다."""

    code: str
    category: ServiceErrorCategory
    message: str
    retryable: bool = False
    transfer_id: TransferId | None = None
    details: Details = field(default=EMPTY_DETAILS)

    def __post_init__(self) -> None:
        _validate_code(self.code, "ServiceError.code")
        if not isinstance(self.category, ServiceErrorCategory):
            raise InvalidValueError("ServiceError.category: ServiceErrorCategory가 아닙니다")
        if not isinstance(self.message, str) or not self.message:
            raise InvalidValueError("ServiceError.message: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.retryable, bool):
            raise InvalidValueError("ServiceError.retryable: bool이어야 합니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("ServiceError.transfer_id: TransferId가 아닙니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("ServiceError.details: Details가 아닙니다")


class ErrorCode:
    """계층 경계에서 사용하는 안정된 machine-readable code."""

    # 공통 검증
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_VALUE = "INVALID_VALUE"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # lifecycle
    SERVICE_NOT_STARTED = "SERVICE_NOT_STARTED"
    SERVICE_ALREADY_STARTED = "SERVICE_ALREADY_STARTED"
    ENDPOINT_NOT_RUNNING = "ENDPOINT_NOT_RUNNING"
    UNKNOWN_TRANSFER = "UNKNOWN_TRANSFER"

    # registry, config
    REGISTRY_DUPLICATE_KEY = "REGISTRY_DUPLICATE_KEY"
    REGISTRY_FROZEN = "REGISTRY_FROZEN"
    REGISTRY_NOT_FROZEN = "REGISTRY_NOT_FROZEN"
    REGISTRY_UNKNOWN_KEY = "REGISTRY_UNKNOWN_KEY"
    REGISTRY_SCOPE_MISMATCH = "REGISTRY_SCOPE_MISMATCH"
    REGISTRY_INVALID_ENTRY = "REGISTRY_INVALID_ENTRY"
    CONFIG_PARSE_FAILED = "CONFIG_PARSE_FAILED"
    CONFIG_SCHEMA_INVALID = "CONFIG_SCHEMA_INVALID"
    CONFIG_SEMANTIC_INVALID = "CONFIG_SEMANTIC_INVALID"
    CONFIG_UNKNOWN_FIELD = "CONFIG_UNKNOWN_FIELD"
    PROFILE_OVERRIDE_REJECTED = "PROFILE_OVERRIDE_REJECTED"

    # 시간
    CLOCK_DOMAIN_MISMATCH = "CLOCK_DOMAIN_MISMATCH"
    CLOCK_MAPPING_UNAVAILABLE = "CLOCK_MAPPING_UNAVAILABLE"

    # Radio
    RADIO_NOT_CONFIGURED = "RADIO_NOT_CONFIGURED"
    RADIO_NOT_STARTED = "RADIO_NOT_STARTED"
    RADIO_ALREADY_STARTED = "RADIO_ALREADY_STARTED"
    RADIO_DEPENDENCY_MISSING = "RADIO_DEPENDENCY_MISSING"
    RADIO_UNSUPPORTED_SETTING = "RADIO_UNSUPPORTED_SETTING"
    RADIO_UNKNOWN_CHANNEL = "RADIO_UNKNOWN_CHANNEL"
    RADIO_DEVICE_NOT_FOUND = "RADIO_DEVICE_NOT_FOUND"
    RADIO_CONFIGURATION_FAILED = "RADIO_CONFIGURATION_FAILED"
    RADIO_RESOURCE_EXHAUSTED = "RADIO_RESOURCE_EXHAUSTED"
    RADIO_RESOURCE_CONFLICT = "RADIO_RESOURCE_CONFLICT"
    RADIO_LATE_REQUEST = "RADIO_LATE_REQUEST"
    RADIO_LATE_COMMAND = "RADIO_LATE_COMMAND"
    RADIO_TX_UNDERFLOW = "RADIO_TX_UNDERFLOW"
    RADIO_RX_OVERFLOW = "RADIO_RX_OVERFLOW"
    RADIO_RX_TIMEOUT = "RADIO_RX_TIMEOUT"
    RADIO_RX_WINDOW_INCOMPLETE = "RADIO_RX_WINDOW_INCOMPLETE"
    RADIO_STREAM_FAILED = "RADIO_STREAM_FAILED"
    RADIO_DEVICE_DISCONNECTED = "RADIO_DEVICE_DISCONNECTED"
    RADIO_REFERENCE_LOST = "RADIO_REFERENCE_LOST"
    RADIO_OPERATION_CANCELLED = "RADIO_OPERATION_CANCELLED"
    RADIO_TX_OUTCOME_UNKNOWN = "RADIO_TX_OUTCOME_UNKNOWN"
    RADIO_STOP_TIMEOUT = "RADIO_STOP_TIMEOUT"
    RADIO_EVENT_DELIVERY_FAILED = "RADIO_EVENT_DELIVERY_FAILED"
    RADIO_TX_SAMPLE_OUT_OF_RANGE = "RADIO_TX_SAMPLE_OUT_OF_RANGE"

    # 직렬화
    SERIALIZATION_FAILED = "SERIALIZATION_FAILED"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"

    # Stage 1 Link: 지원·field
    LINK_UNSUPPORTED_LINK_ID = "LINK_UNSUPPORTED_LINK_ID"
    LINK_UNSUPPORTED_MESSAGE_TYPE = "LINK_UNSUPPORTED_MESSAGE_TYPE"
    LINK_UNSUPPORTED_MESSAGE_PROFILE = "LINK_UNSUPPORTED_MESSAGE_PROFILE"
    LINK_UNSUPPORTED_SEGMENT_TYPE = "LINK_UNSUPPORTED_SEGMENT_TYPE"
    LINK_INVALID_MESSAGE_FIELD = "LINK_INVALID_MESSAGE_FIELD"
    LINK_RESERVED_FIELD_NONZERO = "LINK_RESERVED_FIELD_NONZERO"

    # Stage 1 Link: Packet·Message
    LINK_PACKET_LENGTH_INVALID = "LINK_PACKET_LENGTH_INVALID"
    LINK_PACKET_CAPACITY_EXCEEDED = "LINK_PACKET_CAPACITY_EXCEEDED"
    LINK_TRANSFER_SIZE_UNSUPPORTED = "LINK_TRANSFER_SIZE_UNSUPPORTED"
    LINK_CRC_MISMATCH = "LINK_CRC_MISMATCH"
    LINK_MESSAGE_LENGTH_INVALID = "LINK_MESSAGE_LENGTH_INVALID"
    LINK_MESSAGE_BUILD_PATH_REQUIRED = "LINK_MESSAGE_BUILD_PATH_REQUIRED"
    LINK_PADDING_INVALID = "LINK_PADDING_INVALID"
    LINK_PACKET_CONTENT_INVALID = "LINK_PACKET_CONTENT_INVALID"

    # Stage 1 Link: Session·fragment
    LINK_UNKNOWN_SESSION = "LINK_UNKNOWN_SESSION"
    LINK_UNKNOWN_TRANSFER = "LINK_UNKNOWN_TRANSFER"
    LINK_SESSION_CONFLICT = "LINK_SESSION_CONFLICT"
    LINK_DATA_ROUTE_INCOMPLETE = "LINK_DATA_ROUTE_INCOMPLETE"
    LINK_DATA_ROUTE_MISMATCH = "LINK_DATA_ROUTE_MISMATCH"
    LINK_DATA_FRAGMENT_PAYLOAD_EMPTY = "LINK_DATA_FRAGMENT_PAYLOAD_EMPTY"
    LINK_FRAGMENT_COUNT_INVALID = "LINK_FRAGMENT_COUNT_INVALID"
    LINK_FRAGMENT_NUMBER_INVALID = "LINK_FRAGMENT_NUMBER_INVALID"
    LINK_FRAGMENT_CONFLICT = "LINK_FRAGMENT_CONFLICT"
    LINK_REASSEMBLY_INCOMPLETE = "LINK_REASSEMBLY_INCOMPLETE"
    LINK_REASSEMBLY_CAPACITY_EXHAUSTED = "LINK_REASSEMBLY_CAPACITY_EXHAUSTED"
    LINK_SEGMENT_LENGTH_INVALID = "LINK_SEGMENT_LENGTH_INVALID"
    LINK_SEGMENT_SESSION_BOUNDARY = "LINK_SEGMENT_SESSION_BOUNDARY"
    LINK_TRANSFER_PROGRESS_INVALID = "LINK_TRANSFER_PROGRESS_INVALID"
    LINK_TRANSFER_PROGRESS_CONFLICT = "LINK_TRANSFER_PROGRESS_CONFLICT"
    LINK_TRANSFER_PROGRESS_NONCONTIGUOUS = "LINK_TRANSFER_PROGRESS_NONCONTIGUOUS"
    LINK_TRANSFER_TERMINAL_CONFLICT = "LINK_TRANSFER_TERMINAL_CONFLICT"
    LINK_TRANSFER_PROFILE_SWITCH_UNSAFE = "LINK_TRANSFER_PROFILE_SWITCH_UNSAFE"
    LINK_SEQUENTIAL_ID_CALLER_FORBIDDEN = "LINK_SEQUENTIAL_ID_CALLER_FORBIDDEN"
    LINK_SEQUENTIAL_ID_EXHAUSTED = "LINK_SEQUENTIAL_ID_EXHAUSTED"

    # Stage 1 Link: TBB
    LINK_TBB_ASSEMBLY_CAPACITY_EXHAUSTED = "LINK_TBB_ASSEMBLY_CAPACITY_EXHAUSTED"
    LINK_TBB_CANDIDATE_NOT_FOUND = "LINK_TBB_CANDIDATE_NOT_FOUND"
    LINK_TBB_FRAGMENT_CONFLICT = "LINK_TBB_FRAGMENT_CONFLICT"
    LINK_TBB_FRAGMENT_PAYLOAD_EMPTY = "LINK_TBB_FRAGMENT_PAYLOAD_EMPTY"
    LINK_TBB_FRAGMENT_SIZE_INVALID = "LINK_TBB_FRAGMENT_SIZE_INVALID"
    LINK_TBB_REPLACEMENT_INVALID = "LINK_TBB_REPLACEMENT_INVALID"
    LINK_TBB_CRYPTO_UNAVAILABLE = "LINK_TBB_CRYPTO_UNAVAILABLE"
    LINK_TBB_SIGNER_NOT_CONFIGURED = "LINK_TBB_SIGNER_NOT_CONFIGURED"
    LINK_TBB_TRUST_STORE_NOT_CONFIGURED = "LINK_TBB_TRUST_STORE_NOT_CONFIGURED"
    LINK_TBB_CRYPTO_PROVIDER_INVALID = "LINK_TBB_CRYPTO_PROVIDER_INVALID"
    LINK_TBB_PRIVATE_KEY_MALFORMED = "LINK_TBB_PRIVATE_KEY_MALFORMED"
    LINK_TBB_CERTIFICATE_MALFORMED = "LINK_TBB_CERTIFICATE_MALFORMED"
    LINK_TBB_CERTIFICATE_NOT_FOUND = "LINK_TBB_CERTIFICATE_NOT_FOUND"
    LINK_TBB_CERTIFICATE_AMBIGUOUS = "LINK_TBB_CERTIFICATE_AMBIGUOUS"
    LINK_TBB_FINGERPRINT_MISMATCH = "LINK_TBB_FINGERPRINT_MISMATCH"
    LINK_TBB_CRYPTO_PROFILE_UNSUPPORTED = "LINK_TBB_CRYPTO_PROFILE_UNSUPPORTED"
    LINK_TBB_KEY_CERTIFICATE_MISMATCH = "LINK_TBB_KEY_CERTIFICATE_MISMATCH"
    LINK_TBB_SIGNATURE_INVALID = "LINK_TBB_SIGNATURE_INVALID"
    LINK_TBB_SIGNING_FAILED = "LINK_TBB_SIGNING_FAILED"

    # Stage 1 Link: registry/profile
    LINK_CODEC_NOT_REGISTERED = "LINK_CODEC_NOT_REGISTERED"
    LINK_PROFILE_NOT_FOUND = "LINK_PROFILE_NOT_FOUND"
    LINK_PROFILE_MISMATCH = "LINK_PROFILE_MISMATCH"

    # Stage 4 MAC: 승인된 가산 migration (DR-STAGE4-MAC-015)
    MAC_TRANSFER_FAILED = "MAC_TRANSFER_FAILED"
    MAC_TRANSFER_TIMEOUT = "MAC_TRANSFER_TIMEOUT"
    MAC_RESOURCE_UNAVAILABLE = "MAC_RESOURCE_UNAVAILABLE"

    # Stage 2 Link ID 11 PHY
    PHY_UNSUPPORTED_LINK_ID = "PHY_UNSUPPORTED_LINK_ID"
    PHY_PACKET_LENGTH_INVALID = "PHY_PACKET_LENGTH_INVALID"
    PHY_SAMPLE_RATE_MISMATCH = "PHY_SAMPLE_RATE_MISMATCH"
    PHY_TRANSMIT_DEADLINE_EXPIRED = "PHY_TRANSMIT_DEADLINE_EXPIRED"
    PHY_SYNCHRONIZATION_FAILED = "PHY_SYNCHRONIZATION_FAILED"
    PHY_LINK_ID_DETECTION_FAILED = "PHY_LINK_ID_DETECTION_FAILED"
    PHY_LINK_ID_NOT_ALLOWED = "PHY_LINK_ID_NOT_ALLOWED"
    PHY_FEC_DECODE_FAILED = "PHY_FEC_DECODE_FAILED"
    PHY_INTERNAL_INVARIANT_BROKEN = "PHY_INTERNAL_INVARIANT_BROKEN"
