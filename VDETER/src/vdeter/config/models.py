"""Endpoint 설정 model (`stage0_baseline.md` §8).

설정은 TOML load 뒤 frozen snapshot으로 고정한다. 표준 profile, 장비 설정과 runtime
상태는 서로 다른 수명주기를 가지며 이 module은 설정만 다룬다.

- Standard Profile: `vdeter.profiles`가 소유하는 immutable 표준 자료
- Endpoint Config: 이 module의 frozen snapshot
- Radio Device Config: `RadioConfiguration`으로 분리한 frozen snapshot
- Runtime State: memory 전용이며 설정에 쓰지 않는다

설정은 승인된 standard profile을 *선택*할 수 있지만 profile의 표준 field를 override하지
않는다. endpoint 시작 뒤 hot reload와 adapter hot-swap은 단계 0 범위가 아니다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from ..common.contracts.radio import RadioConfiguration
from ..common.contracts.service import ServiceRole
from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId, PhysicalChannel, StationId
from ..common.serialization import to_reference_json
from ..profiles.models import RegistryScope

CONFIG_SCHEMA_VERSION = 1


class TimeSourceKind(str, Enum):
    SYSTEM = "SYSTEM"
    TEST = "TEST"


class RandomSourceKind(str, Enum):
    SYSTEM = "SYSTEM"
    SEEDED = "SEEDED"


class IdSourceKind(str, Enum):
    UUID4 = "UUID4"
    DETERMINISTIC = "DETERMINISTIC"


#: 운영 설정에서 선택할 수 없는 test scope 구현.
TEST_SCOPE_TIME_SOURCES = frozenset({TimeSourceKind.TEST})
TEST_SCOPE_RANDOM_SOURCES = frozenset({RandomSourceKind.SEEDED})
TEST_SCOPE_ID_SOURCES = frozenset({IdSourceKind.DETERMINISTIC})


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """endpoint 시작 시 선택하는 시간·난수·ID 구현. 실행 중 교체하지 않는다."""

    time: TimeSourceKind
    random: RandomSourceKind
    id: IdSourceKind
    seed: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.time, TimeSourceKind):
            raise InvalidValueError("SourceSelection.time: TimeSourceKind여야 합니다")
        if not isinstance(self.random, RandomSourceKind):
            raise InvalidValueError("SourceSelection.random: RandomSourceKind여야 합니다")
        if not isinstance(self.id, IdSourceKind):
            raise InvalidValueError("SourceSelection.id: IdSourceKind여야 합니다")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise InvalidValueError("SourceSelection.seed: 정수여야 합니다")
        needs_seed = (
            self.random is RandomSourceKind.SEEDED or self.id is IdSourceKind.DETERMINISTIC
        )
        if needs_seed and self.seed is None:
            raise InvalidValueError("SourceSelection: 재현 가능한 구현에는 seed가 필요합니다")


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    max_payload_bytes: int
    max_concurrent_transfers: int

    def __post_init__(self) -> None:
        for name in ("max_payload_bytes", "max_concurrent_transfers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"ServiceConfig.{name}: 양의 정수여야 합니다")


@dataclass(frozen=True, slots=True)
class LinkConfig:
    supported_link_ids: tuple[LinkId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.supported_link_ids, tuple) or any(
            not isinstance(link_id, LinkId) for link_id in self.supported_link_ids
        ):
            raise InvalidValueError("LinkConfig.supported_link_ids: LinkId tuple이어야 합니다")
        if not self.supported_link_ids:
            raise InvalidValueError("LinkConfig.supported_link_ids: 최소 하나가 필요합니다")


@dataclass(frozen=True, slots=True)
class MacConfig:
    link_id: LinkId
    tx_channel: PhysicalChannel
    rx_channel: PhysicalChannel
    transmit_lead_nanoseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("MacConfig.link_id: LinkId여야 합니다")
        for name in ("tx_channel", "rx_channel"):
            if not isinstance(getattr(self, name), PhysicalChannel):
                raise InvalidValueError(f"MacConfig.{name}: PhysicalChannel이어야 합니다")
        value = self.transmit_lead_nanoseconds
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidValueError("MacConfig.transmit_lead_nanoseconds: 0 이상이어야 합니다")


@dataclass(frozen=True, slots=True)
class PhyConfig:
    sample_rate_hz: int

    def __post_init__(self) -> None:
        value = self.sample_rate_hz
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidValueError("PhyConfig.sample_rate_hz: 양의 정수여야 합니다")


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    jsonl_enabled: bool = True
    console_enabled: bool = False

    def __post_init__(self) -> None:
        for name in ("jsonl_enabled", "console_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise InvalidValueError(f"LoggingConfig.{name}: bool이어야 합니다")


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """secret과 key material의 *참조*만 담는다. 실제 값은 설정에 넣지 않는다."""

    private_key_ref: str = ""
    certificate_ref: str = ""
    trust_store_ref: str = ""

    def __post_init__(self) -> None:
        for name in ("private_key_ref", "certificate_ref", "trust_store_ref"):
            if not isinstance(getattr(self, name), str):
                raise InvalidValueError(f"SecurityConfig.{name}: 문자열 참조여야 합니다")


@dataclass(frozen=True, slots=True)
class TestScopeConfig:
    """시험 전용 설정. 운영 scope에서는 사용할 수 없다."""

    peer_station_id: StationId | None = None

    def __post_init__(self) -> None:
        if self.peer_station_id is not None and not isinstance(self.peer_station_id, StationId):
            raise InvalidValueError("TestScopeConfig.peer_station_id: StationId여야 합니다")
        if self.peer_station_id is not None and self.peer_station_id.value == 0:
            raise InvalidValueError(
                "TestScopeConfig.peer_station_id: broadcast 예약값 0을 사용할 수 없습니다"
            )


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """frozen endpoint 설정 snapshot."""

    schema_version: int
    endpoint_id: str
    role: ServiceRole
    station_id: StationId
    scope: RegistryScope
    code_version: str
    service: ServiceConfig
    link: LinkConfig
    mac: MacConfig
    phy: PhyConfig
    sources: SourceSelection
    logging: LoggingConfig
    security: SecurityConfig
    test: TestScopeConfig

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA_VERSION:
            raise InvalidValueError(
                f"EndpointConfig.schema_version: {CONFIG_SCHEMA_VERSION}이어야 합니다"
            )
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id:
            raise InvalidValueError("EndpointConfig.endpoint_id가 비어 있습니다")
        if not isinstance(self.role, ServiceRole):
            raise InvalidValueError("EndpointConfig.role: ServiceRole이어야 합니다")
        if not isinstance(self.station_id, StationId):
            raise InvalidValueError("EndpointConfig.station_id: StationId여야 합니다")
        if self.station_id.value == 0:
            raise InvalidValueError(
                "EndpointConfig.station_id: broadcast 예약값 0을 local station에 사용할 수 없습니다"
            )
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("EndpointConfig.scope: RegistryScope여야 합니다")
        if not isinstance(self.code_version, str) or not self.code_version:
            raise InvalidValueError("EndpointConfig.code_version이 비어 있습니다")


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    """endpoint 설정과 Radio 장비 설정을 분리해 담은 frozen snapshot 묶음."""

    endpoint: EndpointConfig
    radio: RadioConfiguration
    endpoint_fingerprint: str
    radio_fingerprint: str
    source_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointConfig):
            raise InvalidValueError("LoadedConfiguration.endpoint: EndpointConfig여야 합니다")
        if not isinstance(self.radio, RadioConfiguration):
            raise InvalidValueError("LoadedConfiguration.radio: RadioConfiguration이어야 합니다")


def fingerprint_of(instance) -> str:
    """frozen 설정 snapshot의 결정적 지문."""

    canonical = json.dumps(to_reference_json(instance), ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
