"""공통 식별자 (`stage0_baseline.md` §6.1).

local opaque ID는 UUID4 문자열 wrapper이고, 수치 식별자는 규범 문서가 정의한 범위를
그대로 강제한다. local ID의 문자열 내용에는 protocol 의미를 부여하지 않으며 wire field로
encode하지 않는다.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .exceptions import InvalidValueError

# --- 값 범위 (규범 근거는 stage0_baseline.md §6.1 표) ---

SEQUENTIAL_ID_MIN = 0
SEQUENTIAL_ID_MAX = 16383  # 14-bit, M.2092-2 §A4-4.29
LINK_ID_MIN = 0
LINK_ID_MAX = 63  # 6-bit, M.2092-2 §A2-1.2.3.4
STATION_ID_MIN = 0
STATION_ID_MAX = (1 << 32) - 1  # 4-byte Source/Destination carrier
VPFI_MIN = 0
VPFI_MAX = 268435455  # 28-bit, IALA R1027 §3.1
PAYLOAD_FORMAT_MAX_LENGTH = 64
LOCAL_ID_LENGTH = 36  # canonical UUID text form


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(f"{field}: 정수가 아닙니다 ({value!r})")
    return value


def _require_range(value: int, low: int, high: int, field: str) -> None:
    if not low <= value <= high:
        raise InvalidValueError(f"{field}: 허용 범위 {low}..{high}를 벗어났습니다 ({value})")


def _require_uuid4_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidValueError(f"{field}: 문자열이 아닙니다 ({value!r})")
    if len(value) != LOCAL_ID_LENGTH or value != value.lower():
        raise InvalidValueError(f"{field}: 정규 소문자 UUID 표기가 아닙니다 ({value!r})")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise InvalidValueError(f"{field}: UUID로 해석할 수 없습니다 ({value!r})") from error
    if parsed.version != 4:
        raise InvalidValueError(f"{field}: UUID version 4가 아닙니다 (version {parsed.version})")
    if str(parsed) != value:
        raise InvalidValueError(f"{field}: 정규 UUID 표기가 아닙니다 ({value!r})")
    return value


@dataclass(frozen=True, slots=True)
class TransferId:
    """Service가 발급하는 송신 요청 추적 ID. wire로 전송하지 않는다."""

    value: str

    def __post_init__(self) -> None:
        _require_uuid4_text(self.value, "TransferId")


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """MAC이 소유하는 활성 Data Session runtime context ID. wire field가 아니다."""

    value: str

    def __post_init__(self) -> None:
        _require_uuid4_text(self.value, "SessionHandle")


@dataclass(frozen=True, slots=True)
class PacketId:
    """Packet 처리·오류 상관관계용 local ID. wire field가 아니다."""

    value: str

    def __post_init__(self) -> None:
        _require_uuid4_text(self.value, "PacketId")


@dataclass(frozen=True, slots=True)
class BurstId:
    """PHY burst와 Radio 명령 상관관계용 local ID. wire field가 아니다."""

    value: str

    def __post_init__(self) -> None:
        _require_uuid4_text(self.value, "BurstId")


@dataclass(frozen=True, slots=True)
class SequentialId:
    """VDE Segment transaction 식별자. 14-bit wire 값이며 Link가 소유한다."""

    value: int

    def __post_init__(self) -> None:
        value = _require_int(self.value, "SequentialId")
        _require_range(value, SEQUENTIAL_ID_MIN, SEQUENTIAL_ID_MAX, "SequentialId")


@dataclass(frozen=True, slots=True)
class LinkId:
    """6-bit Link ID. registry 등록 여부와 운용 capability는 별도로 검증한다."""

    value: int

    def __post_init__(self) -> None:
        value = _require_int(self.value, "LinkId")
        _require_range(value, LINK_ID_MIN, LINK_ID_MAX, "LinkId")


@dataclass(frozen=True, slots=True)
class StationId:
    """4-byte Source/Destination carrier. 의미 validation은 주소 종류와 Message 규칙이 한다."""

    value: int

    def __post_init__(self) -> None:
        value = _require_int(self.value, "StationId")
        _require_range(value, STATION_ID_MIN, STATION_ID_MAX, "StationId")


@dataclass(frozen=True, slots=True)
class Vpfi:
    """IALA R1027 VDE Payload Format Identifier (28-bit)."""

    value: int

    def __post_init__(self) -> None:
        value = _require_int(self.value, "Vpfi")
        _require_range(value, VPFI_MIN, VPFI_MAX, "Vpfi")


@dataclass(frozen=True, slots=True)
class PayloadFormat:
    """Service·registry가 사용하는 local payload format key. wire VPFI가 아니다."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidValueError(f"PayloadFormat: 문자열이 아닙니다 ({self.value!r})")
        if not self.value or len(self.value) > PAYLOAD_FORMAT_MAX_LENGTH:
            raise InvalidValueError(
                f"PayloadFormat: 1..{PAYLOAD_FORMAT_MAX_LENGTH}자여야 합니다 ({self.value!r})"
            )
        if not self.value.isascii() or not self.value.isprintable():
            raise InvalidValueError(f"PayloadFormat: 출력 가능한 ASCII가 아닙니다 ({self.value!r})")


@dataclass(frozen=True, slots=True)
class PhysicalChannel:
    """MAC이 지정하고 Radio 설정이 주파수로 해석하는 Physical Channel 이름."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise InvalidValueError(f"PhysicalChannel: 문자열이 아닙니다 ({self.name!r})")
        if not self.name or len(self.name) > 64:
            raise InvalidValueError(f"PhysicalChannel: 1..64자여야 합니다 ({self.name!r})")
        if not self.name.isascii() or not self.name.isprintable():
            raise InvalidValueError(
                f"PhysicalChannel: 출력 가능한 ASCII가 아닙니다 ({self.name!r})"
            )


@runtime_checkable
class IdSource(Protocol):
    """local opaque ID 생성원. endpoint 시작 시 운영·시험 구현을 선택한다."""

    def new_transfer_id(self) -> TransferId: ...

    def new_session_handle(self) -> SessionHandle: ...

    def new_packet_id(self) -> PacketId: ...

    def new_burst_id(self) -> BurstId: ...

    def new_event_id(self) -> str: ...

    def description(self) -> str: ...


class Uuid4IdSource:
    """운영 구현. system entropy 기반 UUID4를 사용한다."""

    __slots__ = ()

    def _new(self) -> str:
        return str(uuid.uuid4())

    def new_transfer_id(self) -> TransferId:
        return TransferId(self._new())

    def new_session_handle(self) -> SessionHandle:
        return SessionHandle(self._new())

    def new_packet_id(self) -> PacketId:
        return PacketId(self._new())

    def new_burst_id(self) -> BurstId:
        return BurstId(self._new())

    def new_event_id(self) -> str:
        return self._new()

    def description(self) -> str:
        return "uuid4:system-entropy"


class DeterministicIdSource:
    """시험 구현. 기록된 seed로 UUID4 형식 값을 재현 가능하게 생성한다."""

    __slots__ = ("_seed", "_random", "_count")

    def __init__(self, seed: int) -> None:
        self._seed = _require_int(seed, "DeterministicIdSource seed")
        self._random = random.Random(seed)
        self._count = 0

    def _new(self) -> str:
        self._count += 1
        return str(uuid.UUID(int=self._random.getrandbits(128), version=4))

    def new_transfer_id(self) -> TransferId:
        return TransferId(self._new())

    def new_session_handle(self) -> SessionHandle:
        return SessionHandle(self._new())

    def new_packet_id(self) -> PacketId:
        return PacketId(self._new())

    def new_burst_id(self) -> BurstId:
        return BurstId(self._new())

    def new_event_id(self) -> str:
        return self._new()

    def issued_count(self) -> int:
        return self._count

    def description(self) -> str:
        return f"deterministic-uuid4:seed={self._seed}"
