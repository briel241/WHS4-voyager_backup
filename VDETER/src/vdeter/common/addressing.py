"""주소 모델 (`stage0_baseline.md` §6.1).

공개 Service API는 end-to-end original 주소만 노출한다. original과 node 주소는 같은
수치 carrier(`StationId`)를 쓰지만 DTO 역할과 field 이름을 합치지 않는다.
Broadcast의 wire 표현(값 0)은 Link codec만 다루며 공개 모델에서는 tag로 구분한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .exceptions import InvalidValueError
from .identifiers import StationId


class AddressKind(str, Enum):
    ADDRESSED = "ADDRESSED"
    BROADCAST = "BROADCAST"


@dataclass(frozen=True, slots=True)
class Address:
    """tagged 주소 모델. `ADDRESSED`는 station_id를 가지고 `BROADCAST`는 갖지 않는다."""

    kind: AddressKind
    station_id: StationId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AddressKind):
            raise InvalidValueError(f"Address.kind: AddressKind가 아닙니다 ({self.kind!r})")
        if self.kind is AddressKind.ADDRESSED:
            if not isinstance(self.station_id, StationId):
                raise InvalidValueError("Address: ADDRESSED에는 StationId가 필요합니다")
            if self.station_id.value == 0:
                raise InvalidValueError(
                    "Address: station ID 0은 broadcast wire 표현이며 ADDRESSED에 사용할 수 없습니다"
                )
        elif self.station_id is not None:
            raise InvalidValueError("Address: BROADCAST에는 station_id를 지정할 수 없습니다")

    @property
    def is_broadcast(self) -> bool:
        return self.kind is AddressKind.BROADCAST


def Addressed(station_id: StationId) -> Address:  # noqa: N802 - 기준선 §6.1의 tagged 이름
    """특정 station을 지정하는 주소를 만든다."""

    return Address(kind=AddressKind.ADDRESSED, station_id=station_id)


def Broadcast() -> Address:  # noqa: N802 - 기준선 §6.1의 tagged 이름
    """Broadcast 주소를 만든다."""

    return Address(kind=AddressKind.BROADCAST)


@dataclass(frozen=True, slots=True)
class OriginalSource:
    """end-to-end 최초 송신 station."""

    station_id: StationId

    def __post_init__(self) -> None:
        if not isinstance(self.station_id, StationId):
            raise InvalidValueError("OriginalSource: StationId가 필요합니다")


@dataclass(frozen=True, slots=True)
class OriginalDestination:
    """end-to-end 최종 수신 대상. addressed 또는 broadcast."""

    address: Address

    def __post_init__(self) -> None:
        if not isinstance(self.address, Address):
            raise InvalidValueError("OriginalDestination: Address가 필요합니다")


@dataclass(frozen=True, slots=True)
class NodeSource:
    """현재 hop의 송신 node. Link/MAC 내부 route metadata다."""

    station_id: StationId

    def __post_init__(self) -> None:
        if not isinstance(self.station_id, StationId):
            raise InvalidValueError("NodeSource: StationId가 필요합니다")


@dataclass(frozen=True, slots=True)
class NodeDestination:
    """현재 hop의 수신 node. Link/MAC 내부 route metadata다."""

    address: Address

    def __post_init__(self) -> None:
        if not isinstance(self.address, Address):
            raise InvalidValueError("NodeDestination: Address가 필요합니다")


@dataclass(frozen=True, slots=True)
class RouteAddresses:
    """original 주소와 선택적인 node 주소를 함께 보존하는 내부 route metadata."""

    original_source: OriginalSource
    original_destination: OriginalDestination
    node_source: NodeSource | None = None
    node_destination: NodeDestination | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.original_source, OriginalSource):
            raise InvalidValueError("RouteAddresses: original_source가 필요합니다")
        if not isinstance(self.original_destination, OriginalDestination):
            raise InvalidValueError("RouteAddresses: original_destination이 필요합니다")
        if self.node_source is not None and not isinstance(self.node_source, NodeSource):
            raise InvalidValueError("RouteAddresses: node_source 형식이 잘못되었습니다")
        if self.node_destination is not None and not isinstance(
            self.node_destination, NodeDestination
        ):
            raise InvalidValueError("RouteAddresses: node_destination 형식이 잘못되었습니다")
