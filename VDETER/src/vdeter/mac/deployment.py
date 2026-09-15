"""Stage 4 배치 profile과 deployment fingerprint (`DR-STAGE4-MAC-011`, `012`).

세 endpoint(MASS-A, MASS-B, ROC)가 같은 구성원·역할·배치 설정을 쓰는지 group 시작 전에
검증한다. local `station_id`와 `ServiceRole`은 endpoint마다 다르므로 fingerprint 입력에서
제외하고 group role/set 검증으로 판정한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..common.contracts.mac_execution import ServiceAreaMode
from ..common.contracts.message import ControlStationServiceArea
from ..common.contracts.service import ServiceRole
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import InvalidValueError
from ..common.identifiers import StationId
from ..common.results import Ok, internal_error

DEPLOYMENT_SCHEMA = "VDETER-STAGE4-DEPLOYMENT-V1"
TBB_CERTIFICATE_FINGERPRINT_BYTES = 6
MEMBER_STATION_COUNT = 3

#: Type 0 override가 허용하는 system status 값 (`DR-STAGE4-MAC-005`).
ALLOWED_SYSTEM_STATUS = (0, 10, 20, 30)
SYSTEM_STATUS_NORMAL = 0
SYSTEM_STATUS_BUSY = 10
SYSTEM_STATUS_TEMPORARILY_OUT_OF_SERVICE = 20
SYSTEM_STATUS_SCHEDULED_OUT_OF_SERVICE = 30
OUT_OF_SERVICE_STATUS = (
    SYSTEM_STATUS_TEMPORARILY_OUT_OF_SERVICE,
    SYSTEM_STATUS_SCHEDULED_OUT_OF_SERVICE,
)


def canonical_json_bytes(value: object) -> bytes:
    """재귀 key 정렬, separators `(',', ':')`, ASCII escape 없는 UTF-8 canonical JSON."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_lowercase_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidValueError(f"{name}: {low}..{high} 정수여야 합니다")
    return value


def config_error(message: str, **details):
    return internal_error(
        Layer.MAC,
        ErrorCode.CONFIG_SEMANTIC_INVALID,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class Type0OverrideProfile:
    """ROC가 한 번 송신하는 Type 0 override 값 (`DR-STAGE4-MAC-005`)."""

    selection_interval: int
    short_data_limit: int
    system_status: int

    def __post_init__(self) -> None:
        _int(self.selection_interval, "Type0OverrideProfile.selection_interval", 0, 511)
        _int(self.short_data_limit, "Type0OverrideProfile.short_data_limit", 0, 127)
        if isinstance(self.system_status, bool) or self.system_status not in ALLOWED_SYSTEM_STATUS:
            raise InvalidValueError(
                "Type0OverrideProfile.system_status: 0, 10, 20, 30 중 하나여야 합니다"
            )

    def canonical_json_value(self) -> dict:
        return {
            "media_access_priority_level": 0,
            "selection_interval": self.selection_interval,
            "short_data_limit": self.short_data_limit,
            "system_status": self.system_status,
        }


@dataclass(frozen=True, slots=True)
class Stage4TbbFixtureProfile:
    """canonical Stage 4 TBB fixture 설정 (`DR-STAGE4-MAC-004`).

    `validity=0`, 누락 값, 현재 UTC로부터의 자동 계산과 endpoint별 default를 허용하지 않는다.
    """

    fixture_id: str
    start_time: int
    validity: int
    service_area: ControlStationServiceArea

    def __post_init__(self) -> None:
        if self.fixture_id != STAGE4_TBB_FIXTURE_ID:
            raise InvalidValueError(
                f"Stage4TbbFixtureProfile.fixture_id: 정확히 {STAGE4_TBB_FIXTURE_ID}여야 합니다"
            )
        _int(self.start_time, "Stage4TbbFixtureProfile.start_time", 0, (1 << 32) - 1)
        _int(self.validity, "Stage4TbbFixtureProfile.validity", 1, 64800)
        if not isinstance(self.service_area, ControlStationServiceArea):
            raise InvalidValueError(
                "Stage4TbbFixtureProfile.service_area: ControlStationServiceArea여야 합니다"
            )
        if self.service_area.priority != 0:
            raise InvalidValueError(
                "Stage4TbbFixtureProfile.service_area.priority: 완료 profile에서 정확히 0이어야 합니다"
            )


STAGE4_TBB_FIXTURE_ID = "STAGE4_25K_TEST_TBB_V1"


@dataclass(frozen=True, slots=True)
class MacDeploymentProfile:
    """endpoint 조립 시 Runtime이 주입하는 Stage 4 배치 설정."""

    service_area_mode: ServiceAreaMode
    roc_station_id: StationId
    tbb_control_station_id: int
    member_station_ids: tuple[StationId, ...]
    expected_tbb_certificate_fingerprint: bytes
    tbb_fixture: Stage4TbbFixtureProfile
    type0_override: Type0OverrideProfile | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.service_area_mode, ServiceAreaMode):
            raise InvalidValueError("MacDeploymentProfile.service_area_mode: ServiceAreaMode여야 합니다")
        if not isinstance(self.roc_station_id, StationId) or self.roc_station_id.value == 0:
            raise InvalidValueError("MacDeploymentProfile.roc_station_id: 0이 아닌 StationId여야 합니다")
        if isinstance(self.tbb_control_station_id, bool):
            raise InvalidValueError("MacDeploymentProfile.tbb_control_station_id: bool은 허용하지 않습니다")
        _int(self.tbb_control_station_id, "MacDeploymentProfile.tbb_control_station_id", 0, 255)
        members = self.member_station_ids
        if not isinstance(members, tuple) or len(members) != MEMBER_STATION_COUNT or any(
            not isinstance(item, StationId) for item in members
        ):
            raise InvalidValueError(
                "MacDeploymentProfile.member_station_ids: 정확히 세 StationId tuple이어야 합니다"
            )
        values = tuple(item.value for item in members)
        if any(value == 0 for value in values):
            raise InvalidValueError("MacDeploymentProfile.member_station_ids: 0은 허용하지 않습니다")
        if len(set(values)) != MEMBER_STATION_COUNT:
            raise InvalidValueError("MacDeploymentProfile.member_station_ids: 중복 ID가 있습니다")
        if tuple(sorted(values)) != values:
            raise InvalidValueError(
                "MacDeploymentProfile.member_station_ids: StationId.value 오름차순이어야 합니다"
            )
        if self.roc_station_id.value not in values:
            raise InvalidValueError(
                "MacDeploymentProfile.member_station_ids: roc_station_id를 포함해야 합니다"
            )
        fingerprint = self.expected_tbb_certificate_fingerprint
        if not isinstance(fingerprint, bytes) or len(fingerprint) != TBB_CERTIFICATE_FINGERPRINT_BYTES:
            raise InvalidValueError(
                "MacDeploymentProfile.expected_tbb_certificate_fingerprint: 정확히 6 bytes여야 합니다"
            )
        if not isinstance(self.tbb_fixture, Stage4TbbFixtureProfile):
            raise InvalidValueError(
                "MacDeploymentProfile.tbb_fixture: Stage4TbbFixtureProfile이어야 합니다"
            )
        if self.type0_override is not None and not isinstance(
            self.type0_override, Type0OverrideProfile
        ):
            raise InvalidValueError(
                "MacDeploymentProfile.type0_override: Type0OverrideProfile이어야 합니다"
            )

    @property
    def mass_station_ids(self) -> tuple[StationId, ...]:
        """ROC를 제외한 두 MASS를 `StationId.value` 오름차순으로 반환한다."""

        return tuple(
            item for item in self.member_station_ids if item.value != self.roc_station_id.value
        )

    @property
    def mass_a_station_id(self) -> StationId:
        """ROC를 제외한 두 ID 중 작은 값."""

        return self.mass_station_ids[0]

    @property
    def mass_b_station_id(self) -> StationId:
        """ROC를 제외한 두 ID 중 큰 값."""

        return self.mass_station_ids[1]

    def is_member(self, station_id: StationId) -> bool:
        return any(item.value == station_id.value for item in self.member_station_ids)

    def peer_role_name(self, station_id: StationId) -> str:
        if station_id.value == self.roc_station_id.value:
            return "ROC"
        if station_id.value == self.mass_a_station_id.value:
            return "MASS_A"
        if station_id.value == self.mass_b_station_id.value:
            return "MASS_B"
        return "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CanonicalStage4DeploymentProfile:
    """`deployment_fingerprint`를 소유하는 파생 불변 profile."""

    profile: MacDeploymentProfile
    canonical_tbb_fingerprint: str
    deployment_fingerprint: str
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.profile, MacDeploymentProfile):
            raise InvalidValueError("CanonicalStage4DeploymentProfile.profile이 잘못되었습니다")
        for name in ("canonical_tbb_fingerprint", "deployment_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or value != value.lower():
                raise InvalidValueError(
                    f"CanonicalStage4DeploymentProfile.{name}: SHA-256 lowercase hex여야 합니다"
                )
        if not isinstance(self.canonical_bytes, bytes) or not self.canonical_bytes:
            raise InvalidValueError("CanonicalStage4DeploymentProfile.canonical_bytes가 비어 있습니다")


def deployment_canonical_value(
    profile: MacDeploymentProfile, canonical_tbb_fingerprint: str
) -> dict:
    """`VDETER-STAGE4-DEPLOYMENT-V1` canonical JSON object."""

    override = profile.type0_override
    return {
        "canonical_tbb_fingerprint": canonical_tbb_fingerprint,
        "expected_tbb_certificate_fingerprint": profile.expected_tbb_certificate_fingerprint.hex(),
        "member_station_ids": [item.value for item in profile.member_station_ids],
        "roc_station_id": profile.roc_station_id.value,
        "schema": DEPLOYMENT_SCHEMA,
        "service_area_mode": profile.service_area_mode.value,
        "tbb_control_station_id": profile.tbb_control_station_id,
        "type0_override": None if override is None else override.canonical_json_value(),
    }


def build_canonical_deployment_profile(
    profile: MacDeploymentProfile, canonical_tbb_fingerprint: str
) -> CanonicalStage4DeploymentProfile:
    canonical = canonical_json_bytes(
        deployment_canonical_value(profile, canonical_tbb_fingerprint)
    )
    return CanonicalStage4DeploymentProfile(
        profile=profile,
        canonical_tbb_fingerprint=canonical_tbb_fingerprint,
        deployment_fingerprint=sha256_lowercase_hex(canonical),
        canonical_bytes=canonical,
    )


@dataclass(frozen=True, slots=True)
class EndpointDescriptor:
    """group 검증에 넣는 endpoint 하나의 `(station_id, role, profile)`."""

    station_id: StationId
    role: ServiceRole
    profile: MacDeploymentProfile

    def __post_init__(self) -> None:
        if not isinstance(self.station_id, StationId) or self.station_id.value == 0:
            raise InvalidValueError("EndpointDescriptor.station_id: 0이 아닌 StationId여야 합니다")
        if not isinstance(self.role, ServiceRole):
            raise InvalidValueError("EndpointDescriptor.role: ServiceRole이어야 합니다")
        if not isinstance(self.profile, MacDeploymentProfile):
            raise InvalidValueError("EndpointDescriptor.profile: MacDeploymentProfile이어야 합니다")


#: group 안에서 완전히 같아야 하는 profile field.
_GROUP_IDENTICAL_FIELDS = (
    "member_station_ids",
    "service_area_mode",
    "roc_station_id",
    "tbb_control_station_id",
    "expected_tbb_certificate_fingerprint",
    "type0_override",
)


def validate_endpoint_group(
    descriptors: tuple[EndpointDescriptor, ...],
    canonical_tbb_fingerprints: tuple[str, ...],
):
    """세 endpoint descriptor를 한 번에 검증한다 (`DR-STAGE4-MAC-012`).

    성공하면 각 descriptor에 대응하는 `CanonicalStage4DeploymentProfile` tuple을 반환한다.
    """

    if not isinstance(descriptors, tuple) or len(descriptors) != MEMBER_STATION_COUNT or any(
        not isinstance(item, EndpointDescriptor) for item in descriptors
    ):
        return config_error(
            "Stage 4 endpoint group은 정확히 세 EndpointDescriptor여야 합니다",
            descriptor_count=len(descriptors) if isinstance(descriptors, tuple) else -1,
        )
    if not isinstance(canonical_tbb_fingerprints, tuple) or len(
        canonical_tbb_fingerprints
    ) != MEMBER_STATION_COUNT:
        return config_error("canonical TBB fingerprint는 endpoint마다 하나씩 필요합니다")

    first = descriptors[0].profile
    for index, descriptor in enumerate(descriptors[1:], start=1):
        for name in _GROUP_IDENTICAL_FIELDS:
            if getattr(descriptor.profile, name) != getattr(first, name):
                return config_error(
                    "endpoint group의 배치 설정이 서로 다릅니다",
                    mismatched_field=name,
                    descriptor_index=index,
                )
        if descriptor.profile.tbb_fixture.service_area != first.tbb_fixture.service_area:
            return config_error(
                "endpoint group의 TBB service_area가 서로 다릅니다", descriptor_index=index
            )

    if len(set(canonical_tbb_fingerprints)) != 1:
        return config_error("세 endpoint의 canonical_tbb_fingerprint가 서로 다릅니다")
    canonical_tbb_fingerprint = canonical_tbb_fingerprints[0]

    control = tuple(item for item in descriptors if item.role is ServiceRole.CONTROL)
    ships = tuple(item for item in descriptors if item.role is ServiceRole.SHIP)
    if len(control) != 1:
        return config_error(
            "ServiceRole.CONTROL은 정확히 하나여야 합니다", control_count=len(control)
        )
    if len(ships) != 2:
        return config_error("ServiceRole.SHIP은 정확히 둘이어야 합니다", ship_count=len(ships))
    if control[0].station_id.value != first.roc_station_id.value:
        return config_error("CONTROL endpoint의 station_id가 roc_station_id와 다릅니다")

    actual = tuple(sorted(item.station_id.value for item in descriptors))
    expected = tuple(item.value for item in first.member_station_ids)
    if actual != expected:
        return config_error(
            "실제 station ID 집합이 member_station_ids와 다릅니다",
            actual_station_ids=actual,
            member_station_ids=expected,
        )

    canonical_profiles = tuple(
        build_canonical_deployment_profile(item.profile, canonical_tbb_fingerprint)
        for item in descriptors
    )
    fingerprints = {item.deployment_fingerprint for item in canonical_profiles}
    if len(fingerprints) != 1:
        return config_error("세 endpoint의 deployment_fingerprint가 서로 다릅니다")
    return Ok(canonical_profiles)


__all__ = (
    "ALLOWED_SYSTEM_STATUS",
    "CanonicalStage4DeploymentProfile",
    "DEPLOYMENT_SCHEMA",
    "EndpointDescriptor",
    "MEMBER_STATION_COUNT",
    "MacDeploymentProfile",
    "OUT_OF_SERVICE_STATUS",
    "STAGE4_TBB_FIXTURE_ID",
    "SYSTEM_STATUS_BUSY",
    "SYSTEM_STATUS_NORMAL",
    "SYSTEM_STATUS_SCHEDULED_OUT_OF_SERVICE",
    "SYSTEM_STATUS_TEMPORARILY_OUT_OF_SERVICE",
    "Stage4TbbFixtureProfile",
    "TBB_CERTIFICATE_FINGERPRINT_BYTES",
    "Type0OverrideProfile",
    "build_canonical_deployment_profile",
    "canonical_json_bytes",
    "config_error",
    "deployment_canonical_value",
    "sha256_lowercase_hex",
    "validate_endpoint_group",
)
