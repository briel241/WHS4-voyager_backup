"""Stage 7 canonical record byte layout (`stage7` §16.1).

`Stage7ServiceStartIntentRecord`와 `Stage7TbbActiveRecord`의 exact bytes를
정의한다. digest는 이 exact bytes의 SHA-256 lowercase hex다.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from dataclasses import dataclass

MAX_U63 = (1 << 63) - 1
MAX_U31 = (1 << 31) - 1

READINESS_KINDS = {
    "ROC_BROADCAST_READY": 0,
    "MASS_ACQUIRED": 1,
    "MASS_LOCAL_TEST_READY": 2,
}
READINESS_KIND_NAMES = {value: key for key, value in READINESS_KINDS.items()}

ROLE_CODES = {"SHIP": 0, "CONTROL": 1}


class Stage7RecordError(ValueError):
    """record encoding 계약 위반."""


def _uuid_raw_bytes(intent_window_id: str) -> bytes:
    """canonical lowercase hyphenated UUID → RFC 4122 network-order 16 bytes.

    Windows GUID mixed-endian memory layout은 사용하지 않는다.
    """

    if (
        not isinstance(intent_window_id, str)
        or len(intent_window_id) != 36
        or intent_window_id != intent_window_id.lower()
    ):
        raise Stage7RecordError("intent_window_id는 lowercase canonical UUID여야 합니다")
    try:
        parsed = uuid.UUID(intent_window_id)
    except ValueError as error:
        raise Stage7RecordError("intent_window_id를 UUID로 해석할 수 없습니다") from error
    if str(parsed) != intent_window_id:
        raise Stage7RecordError("intent_window_id가 canonical 표기가 아닙니다")
    return bytes.fromhex(intent_window_id.replace("-", ""))


def _u64_be(value: int, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_U63:
        raise Stage7RecordError(f"{name}: 0..2^63-1 정수여야 합니다")
    return struct.pack(">Q", value)


def _u32_be(value: int, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= (1 << 32) - 1:
        raise Stage7RecordError(f"{name}: uint32 범위여야 합니다")
    return struct.pack(">I", value)


@dataclass(frozen=True, slots=True)
class Stage7ServiceStartIntentRecord:
    intent_window_id: str
    service_start_deadline_host_raw_ns: int
    endpoint_id: str
    local_station_id: int
    role: str  # "SHIP" | "CONTROL"
    config_ref: str

    def exact_bytes(self) -> bytes:
        if self.role not in ROLE_CODES:
            raise Stage7RecordError("role은 SHIP 또는 CONTROL이어야 합니다")
        endpoint_bytes = self.endpoint_id.encode("ascii")
        if not 1 <= len(endpoint_bytes) <= 255:
            raise Stage7RecordError("endpoint_id 길이가 잘못되었습니다")
        try:
            config_ref_bytes = self.config_ref.encode("utf-8")
        except UnicodeEncodeError as error:
            raise Stage7RecordError(
                "config_ref는 Unicode scalar sequence여야 합니다"
            ) from error
        if len(config_ref_bytes) > (1 << 32) - 1:
            raise Stage7RecordError("config_ref UTF-8 길이가 uint32를 넘습니다")
        return b"".join(
            (
                b"STAGE7_SERVICE_START_INTENT_V1\x00",
                _uuid_raw_bytes(self.intent_window_id),
                _u64_be(self.service_start_deadline_host_raw_ns, "service_start_deadline"),
                bytes((len(endpoint_bytes),)),
                endpoint_bytes,
                _u32_be(self.local_station_id, "local_station_id"),
                bytes((ROLE_CODES[self.role],)),
                _u32_be(len(config_ref_bytes), "config_ref_len"),
                config_ref_bytes,
            )
        )

    def digest(self) -> str:
        return hashlib.sha256(self.exact_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class Stage7TbbActiveRecord:
    endpoint_id: str
    readiness_kind: str  # ROC_BROADCAST_READY | MASS_ACQUIRED | MASS_LOCAL_TEST_READY
    lifecycle_generation: int
    bridge_generation: int
    tbb_fingerprint_sha256: bytes
    valid_from_utc_ns: int
    valid_until_utc_ns: int
    activation_frame_ordinal: int
    local_tbb_evidence_sha256: bytes

    def exact_bytes(self) -> bytes:
        if self.readiness_kind not in READINESS_KINDS:
            raise Stage7RecordError("readiness_kind가 잘못되었습니다")
        endpoint_bytes = self.endpoint_id.encode("ascii")
        if not 1 <= len(endpoint_bytes) <= 255:
            raise Stage7RecordError("endpoint_id 길이가 잘못되었습니다")
        for name, value in (
            ("tbb_fingerprint_sha256", self.tbb_fingerprint_sha256),
            ("local_tbb_evidence_sha256", self.local_tbb_evidence_sha256),
        ):
            if not isinstance(value, bytes) or len(value) != 32:
                raise Stage7RecordError(f"{name}는 32 raw bytes여야 합니다")
        return b"".join(
            (
                b"STAGE7_TBB_ACTIVE_V1\x00",
                bytes((len(endpoint_bytes),)),
                endpoint_bytes,
                bytes((READINESS_KINDS[self.readiness_kind],)),
                _u32_be(self.lifecycle_generation, "lifecycle_generation"),
                _u32_be(self.bridge_generation, "bridge_generation"),
                self.tbb_fingerprint_sha256,
                _u64_be(self.valid_from_utc_ns, "valid_from_utc_ns"),
                _u64_be(self.valid_until_utc_ns, "valid_until_utc_ns"),
                _u64_be(self.activation_frame_ordinal, "activation_frame_ordinal"),
                self.local_tbb_evidence_sha256,
            )
        )

    def digest(self) -> str:
        return hashlib.sha256(self.exact_bytes()).hexdigest()


__all__ = (
    "READINESS_KINDS",
    "READINESS_KIND_NAMES",
    "ROLE_CODES",
    "Stage7RecordError",
    "Stage7ServiceStartIntentRecord",
    "Stage7TbbActiveRecord",
)
