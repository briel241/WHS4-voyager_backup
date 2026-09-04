"""Stage 7 additive schema v4 configuration loader (`stage7_b210_system_baseline.md` §15).

`Stage7ConfigurationLoader`와 private immutable `Stage7ResolvedConfiguration`이
schema v4를 전담한다. 이 loader는 frozen `load_configuration()`과
`RadioAdapterKind(...)`를 호출하거나 기존 `build_radio_adapter()`로 결과를 보내지
않는다. `build_stage7_b210_endpoint()`만 `Stage7ResolvedConfiguration`을 받을 수
있다.

- source schema v4의 top-level key 집합은 frozen v3 공통 table에
  `host_execution|stage7`을 더한 닫힌 집합이다.
- 공통 table은 v3와 같은 key·type·범위를 사용한다.
- `[radio]`와 그 하위 table은 §15의 Stage 7 규칙으로 대체한다.
- `REQUIRED_UNSET`은 exact serial, `host_execution` provisioning field와
  `[[radio.channels]].rx_gain_db`에만 쓸 수 있고 resolved object에는 placeholder가
  하나도 남을 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    ClockReference,
    RadioB210StreamConfiguration,
    RadioChannelBinding,
    RadioChannelSetting,
    RadioConfiguration,
    RadioResourceLimits,
    RadioTimingConfiguration,
    RadioTimingProfile,
)
from ..common.contracts.service import ServiceRole
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import VdeterValidationError
from ..common.identifiers import LinkId, PhysicalChannel, StationId
from ..common.results import Ok, internal_error
from ..config.models import (
    EndpointConfig,
    IdSourceKind,
    LinkConfig,
    LoggingConfig,
    MacConfig,
    PhyConfig,
    RandomSourceKind,
    SecurityConfig,
    ServiceConfig,
    SourceSelection,
    TestScopeConfig,
    TimeSourceKind,
)
from ..profiles.models import RegistryScope
from .canonical_json import canonical_sha256, encode_canonical_json, sha256_hex

try:  # Python 3.11 이상
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - 환경 의존
    _toml = None

# --- canonical literal (§3, §9, §10, §13, §15) --------------------------------

STAGE7_SOURCE_SCHEMA_VERSION = 4
STAGE7_ADAPTER_KIND = "b210-stage7-affine"
STAGE7_HOST_PROFILE = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"
STAGE7_PI_POWER_PROFILE = "PI5_5V5A_HIGH_CURRENT"
STAGE7_LANE_MAP_PROFILE = "STAGE7_C1_C6_TRIANGLE_V1"
STAGE7_PHY_FACADE = "STAGE7_DIAGNOSTIC_PHY_V1"
STAGE7_TIMING_STATUS_PROJECTION = "STAGE7_PUBLIC_UNAVAILABLE_PRIVATE_AFFINE_V1"
STAGE7_CLOCK_BRIDGE_PROFILE = "STAGE7_PI_HOST_AFFINE_TIME_V1"
STAGE7_CFO_PROFILE = "STAGE7_RAW_TONE_CFO_V1"
STAGE7_CFO_NUMERIC_PROFILE = "STAGE7_NUMPY_FFT3072_BINARY64_V1"
STAGE7_TBB_FIXTURE_ID = "VDETER_WIDEBAND_100_TBB_V3"
STAGE7_REGISTRY_FINGERPRINT = (
    "0a2b8cb8a72727bd54c05c5dd6e6ac5aeeec7a0b51f6ebe41a72bfa2482ae5d2"
)
STAGE7_FIXTURE_CLASSIFICATION = "USER_ACCEPTED_UNATTENUATED_FUNCTIONAL_CONDUCTED_V1"

FREQUENCY_TRANSLATION_HZ = 2_290_000_000
LOWER_LOGICAL_CENTER_HZ = 157_237_500
UPPER_LOGICAL_CENTER_HZ = 161_837_500
LOWER_RF_TUNE_HZ = 2_447_237_500
UPPER_RF_TUNE_HZ = 2_451_837_500
DEVICE_SAMPLE_RATE_HZ = 307_200
WAVEFORM_BANDWIDTH_HZ = 100_000
ANALOG_BANDWIDTH_HZ = 200_000
MASTER_CLOCK_RATE_HZ = 30_720_000

RUN_MODES = ("RF_OFF_QUALIFICATION", "GAIN_CALIBRATION", "OPERATIONAL")
ENDPOINT_IDS = ("MASS-A", "MASS-B", "ROC")
PATH_IDS = ("C1", "C2", "C3", "C4", "C5", "C6")
REQUIRED_UNSET = "REQUIRED_UNSET"

LOWER_CHANNEL_NAME = "VDETER_LOWER_100_TEST"
UPPER_CHANNEL_NAME = "VDETER_UPPER_100_TEST"
CANONICAL_CHANNEL_NAMES = (LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME)

USB_TRANSPORTS = ("DIRECT_USB3_5000M", "DIRECT_USB2_480M_EXTERNAL_DC")

#: §15 stage7 table의 exact 고정 정수 값.
STAGE7_FIXED_INTEGERS = {
    "frequency_translation_hz": FREQUENCY_TRANSLATION_HZ,
    "lower_rf_tune_hz": LOWER_RF_TUNE_HZ,
    "upper_rf_tune_hz": UPPER_RF_TUNE_HZ,
    "max_bridge_generations": 3,
    "host_time_min_observations": 8,
    "host_time_min_span_s": 2,
    "host_time_max_acquisition_s": 600,
    "host_time_observation_period_s": 1,
    "host_time_max_bracket_ns": 2_000_000,
    "host_time_tracking_period_s": 5,
    "rf_time_reference_max_gap_s": 5,
    "rf_time_loss_gap_s": 10,
    "active_command_horizon_s": 120,
    "cfo_accept_bound_hz": 1_800,
}

#: §15 stage7 table의 exact 고정 문자열 값.
STAGE7_FIXED_STRINGS = {
    "lane_map_profile": STAGE7_LANE_MAP_PROFILE,
    "phy_facade": STAGE7_PHY_FACADE,
    "timing_status_projection": STAGE7_TIMING_STATUS_PROJECTION,
    "clock_bridge_profile": STAGE7_CLOCK_BRIDGE_PROFILE,
    "cfo_profile": STAGE7_CFO_PROFILE,
    "cfo_numeric_profile": STAGE7_CFO_NUMERIC_PROFILE,
    "registry_fingerprint": STAGE7_REGISTRY_FINGERPRINT,
    "tbb_fixture_id": STAGE7_TBB_FIXTURE_ID,
}

# --- 닫힌 key 집합 (§15) -------------------------------------------------------

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "endpoint",
        "service",
        "link",
        "mac",
        "phy",
        "radio",
        "logging",
        "sources",
        "security",
        "test",
        "host_execution",
        "stage7",
    }
)
_RADIO_KEYS = frozenset(
    {
        "adapter",
        "device_selector",
        "clock_reference",
        "sample_format",
        "timing",
        "resource_limits",
        "b210_stream",
        "channels",
    }
)
_TIMING_KEYS = frozenset(
    {
        "profile",
        "clock_source",
        "time_source",
        "pps_source",
        "mapping_attempt_count",
        "mapping_valid_for_nanoseconds",
        "max_mapping_uncertainty_ns",
    }
)
_RESOURCE_LIMIT_KEYS = frozenset(
    {
        "tx_queue_max_count",
        "tx_queue_max_bytes",
        "rx_window_max_count",
        "rx_queue_max_count",
        "rx_queue_max_bytes",
        "stop_timeout_ns",
    }
)
_B210_STREAM_KEYS = frozenset({"master_clock_rate_hz", "cpu_format", "otw_format"})
#: v3 wideband channel key 집합에서 hardware key 네 개를 금지한 Stage 7 channel key.
_CHANNEL_KEYS = frozenset(
    {
        "name",
        "center_frequency_hz",
        "sample_rate_hz",
        "tx_gain_db",
        "rx_gain_db",
        "waveform_bandwidth_hz",
        "analog_bandwidth_hz",
        "tx_enabled",
        "rx_enabled",
    }
)
_FORBIDDEN_CHANNEL_KEYS = frozenset(
    {"tx_device_channel", "rx_device_channel", "tx_antenna", "rx_antenna"}
)
_HOST_EXECUTION_KEYS = (
    "profile",
    "provider_id",
    "provider_version",
    "host_os_id",
    "host_os_version",
    "host_machine_id",
    "pi_model_revision",
    "pi_power_profile",
    "usb_transport",
    "usb_physical_path",
    "external_power_asset",
    "environment_manifest_path",
    "environment_manifest_sha256",
)
#: host_execution에서 source template의 `REQUIRED_UNSET`을 허용하는 provisioning field.
_HOST_EXECUTION_PROVISIONING_KEYS = frozenset(
    {
        "host_os_id",
        "host_os_version",
        "host_machine_id",
        "pi_model_revision",
        "usb_transport",
        "usb_physical_path",
        "external_power_asset",
        "environment_manifest_path",
        "environment_manifest_sha256",
    }
)
_STAGE7_KEYS = (
    "run_mode",
    "lane_map_profile",
    "frequency_translation_hz",
    "lower_rf_tune_hz",
    "upper_rf_tune_hz",
    "phy_facade",
    "timing_status_projection",
    "clock_bridge_profile",
    "max_bridge_generations",
    "host_time_min_observations",
    "host_time_min_span_s",
    "host_time_max_acquisition_s",
    "host_time_observation_period_s",
    "host_time_max_bracket_ns",
    "host_time_tracking_period_s",
    "rf_time_reference_max_gap_s",
    "rf_time_loss_gap_s",
    "active_command_horizon_s",
    "cfo_profile",
    "cfo_numeric_profile",
    "cfo_accept_bound_hz",
    "deployment_manifest_relpath",
    "deployment_manifest_sha256",
    "registry_fingerprint",
    "tbb_fixture_id",
)
_COMMON_SECTION_KEYS = {
    "endpoint": frozenset({"id", "role", "station_id", "scope", "code_version"}),
    "service": frozenset({"max_payload_bytes", "max_concurrent_transfers"}),
    "link": frozenset({"supported_link_ids"}),
    # Stage 7의 data_link_id는 단일 data profile이 소유한다. endpoint file에는
    # 두지 않는다 (보수적 권장안; §11/§15).
    "mac": frozenset({"link_id", "tx_channel", "rx_channel", "transmit_lead_nanoseconds"}),
    "phy": frozenset({"sample_rate_hz"}),
    "logging": frozenset({"jsonl_enabled", "console_enabled"}),
    "sources": frozenset({"time", "random", "id", "seed"}),
    "security": frozenset({"private_key_ref", "certificate_ref", "trust_store_ref"}),
    "test": frozenset({"peer_station_id"}),
}
_REQUIRED_COMMON_SECTIONS = ("endpoint", "service", "link", "mac", "phy")
_DATA_PROFILE_KEYS = frozenset({"data_link_id"})

MAX_U63 = (1 << 63) - 1
MAX_U31 = (1 << 31) - 1

#: role별 (lower, upper)의 (tx_enabled, rx_enabled) canonical 방향 (§15).
_EXPECTED_DIRECTIONS = {
    ServiceRole.CONTROL: ((False, True), (True, False)),
    ServiceRole.SHIP: ((True, False), (True, True)),
}

# --- role별 canonical lane map (§5, §15.1) ------------------------------------

#: endpoint_id -> lane_index(0=RF_A,1=RF_B) -> {"tx_path","rx_path","tx_channel","rx_channel"}
CANONICAL_LANE_MAP = {
    "ROC": (
        {"tx_path": "C1", "rx_path": "C2", "tx_channel": UPPER_CHANNEL_NAME, "rx_channel": LOWER_CHANNEL_NAME},
        {"tx_path": "C3", "rx_path": "C4", "tx_channel": UPPER_CHANNEL_NAME, "rx_channel": LOWER_CHANNEL_NAME},
    ),
    "MASS-A": (
        {"tx_path": "C2", "rx_path": "C1", "tx_channel": LOWER_CHANNEL_NAME, "rx_channel": UPPER_CHANNEL_NAME},
        {"tx_path": "C5", "rx_path": "C6", "tx_channel": UPPER_CHANNEL_NAME, "rx_channel": UPPER_CHANNEL_NAME},
    ),
    "MASS-B": (
        {"tx_path": "C4", "rx_path": "C3", "tx_channel": LOWER_CHANNEL_NAME, "rx_channel": UPPER_CHANNEL_NAME},
        {"tx_path": "C6", "rx_path": "C5", "tx_channel": UPPER_CHANNEL_NAME, "rx_channel": UPPER_CHANNEL_NAME},
    ),
}

#: §7 canonical role/station 표.
CANONICAL_ROLES = {
    "MASS-A": (ServiceRole.SHIP, 111_111_111),
    "MASS-B": (ServiceRole.SHIP, 333_333_333),
    "ROC": (ServiceRole.CONTROL, 222_222_222),
}


def _error(code: str, message: str, **details):
    return internal_error(
        Layer.RUNTIME,
        code,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


def _schema_error(message: str, **details):
    return _error(ErrorCode.CONFIG_SCHEMA_INVALID, message, **details)


def _semantic_error(message: str, **details):
    return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, message, **details)


def _check_unknown(section: str, data: dict, allowed):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        return _error(
            ErrorCode.CONFIG_UNKNOWN_FIELD,
            f"[{section}]에 알 수 없는 field가 있습니다",
            section=section,
            unknown_fields=tuple(unknown),
        )
    return None


def _require_table(section: str, data: dict):
    content = data.get(section)
    if not isinstance(content, dict):
        return None, _schema_error(f"[{section}] table이 필수입니다", section=section)
    return content, None


def _require_key(section: str, data: dict, key: str):
    if key not in data:
        return _schema_error(
            f"[{section}]에 필수 field {key}가 없습니다", section=section, field=key
        )
    return None


def _int_value(section: str, key: str, value, low: int, high: int):
    if isinstance(value, bool) or not isinstance(value, int):
        return None, _schema_error(
            f"[{section}].{key}는 bool이 아닌 TOML integer여야 합니다", section=section, field=key
        )
    if not low <= value <= high:
        return None, _semantic_error(
            f"[{section}].{key} 범위 초과", section=section, field=key, value=value
        )
    return value, None


def _string_value(section: str, key: str, value):
    if not isinstance(value, str):
        return None, _schema_error(
            f"[{section}].{key}는 문자열이어야 합니다", section=section, field=key
        )
    return value, None


def _bool_value(section: str, key: str, value):
    if not isinstance(value, bool):
        return None, _schema_error(
            f"[{section}].{key}는 bool이어야 합니다", section=section, field=key
        )
    return value, None


def is_valid_run_id(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._-"
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        return False
    return all(character in allowed for character in value)


def is_valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def is_valid_relpath(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8")) > 256:
        return False
    if value.startswith("/") or "\\" in value or ":" in value:
        return False
    segments = value.split("/")
    return all(segment not in ("", ".", "..") for segment in segments)


def is_exact_serial(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 32:
        return False
    return (
        value.isascii()
        and value.isprintable()
        and not any(character.isspace() for character in value)
        and "," not in value
        and "=" not in value
    )


# --- resolved 구성 요소 --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stage7HostExecutionPlan:
    """resolved `[host_execution]` (placeholder 0)."""

    profile: str
    provider_id: str
    provider_version: str
    host_os_id: str
    host_os_version: str
    host_machine_id: str
    pi_model_revision: str
    pi_power_profile: str
    usb_transport: str
    usb_physical_path: str
    external_power_asset: str
    environment_manifest_path: str
    environment_manifest_sha256: str

    def as_json_value(self) -> dict:
        return {key: getattr(self, key) for key in _HOST_EXECUTION_KEYS}


@dataclass(frozen=True, slots=True)
class Stage7LaneBinding:
    """한 physical lane(RF_A/RF_B)의 canonical 결선."""

    lane_index: int
    lane_id: str
    tx_path: str
    rx_path: str
    tx_channel: str
    rx_channel: str
    tx_antenna: str
    rx_antenna: str

    def as_json_value(self) -> dict:
        return {
            "lane_index": self.lane_index,
            "lane_id": self.lane_id,
            "tx_path": self.tx_path,
            "rx_path": self.rx_path,
            "tx_channel": self.tx_channel,
            "rx_channel": self.rx_channel,
            "tx_antenna": self.tx_antenna,
            "rx_antenna": self.rx_antenna,
        }


@dataclass(frozen=True, slots=True)
class Stage7TimePlan:
    """resolved `[stage7]` 시간·CFO parameter."""

    clock_bridge_profile: str
    max_bridge_generations: int
    host_time_min_observations: int
    host_time_min_span_s: int
    host_time_max_acquisition_s: int
    host_time_observation_period_s: int
    host_time_max_bracket_ns: int
    host_time_tracking_period_s: int
    rf_time_reference_max_gap_s: int
    rf_time_loss_gap_s: int
    active_command_horizon_s: int
    cfo_profile: str
    cfo_numeric_profile: str
    cfo_accept_bound_hz: int

    def as_json_value(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(frozen=True, slots=True)
class Stage7OperationalGainInput:
    """OPERATIONAL run에 결속하는 signed gain artifact 입력 (§9.2)."""

    endpoint_common_rx_gain_db: int
    source_gain_artifact_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.endpoint_common_rx_gain_db, bool)
            or not isinstance(self.endpoint_common_rx_gain_db, int)
            # §9.2 v0.8.16: 탐색 상한 75 dB (TX 0 dB 고정 실측 근거).
            or not 0 <= self.endpoint_common_rx_gain_db <= 75
            or self.endpoint_common_rx_gain_db % 3 != 0
        ):
            raise VdeterValidationError(
                "endpoint_common_rx_gain_db: 0..75 dB, 3 dB step이어야 합니다"
            )
        if not is_valid_sha256(self.source_gain_artifact_sha256):
            raise VdeterValidationError("source_gain_artifact_sha256: sha256이 아닙니다")


@dataclass(frozen=True, slots=True)
class Stage7ResolvedConfiguration:
    """run별 immutable endpoint resolved config (§15).

    TEST controller/scenario plan은 이 object의 field가 아니다. common endpoint
    projection의 `schema_version`은 frozen constructor가 허용하는 `3`이지만
    `source_schema_version=4`와 Stage 7 fingerprint를 함께 보존하며 v3
    loader/group으로 재입력하지 않는다.
    """

    source_schema_version: int
    run_id: str
    run_mode: str
    endpoint_id: str
    b210_serial: str
    endpoint: EndpointConfig
    radio: RadioConfiguration
    host_execution: Stage7HostExecutionPlan
    time_plan: Stage7TimePlan
    lane_map_profile: str
    lanes: tuple[Stage7LaneBinding, Stage7LaneBinding]
    frequency_translation_hz: int
    lower_rf_tune_hz: int
    upper_rf_tune_hz: int
    data_link_id: int
    phy_facade: str
    timing_status_projection: str
    deployment_manifest_relpath: str
    deployment_manifest_sha256: str
    registry_fingerprint: str
    tbb_fixture_id: str
    endpoint_common_rx_gain_db: int
    source_gain_artifact_sha256: str | None
    source_sha256: str
    source_path: str
    data_profile_sha256: str
    resolved_canonical_bytes: bytes
    resolved_sha256: str

    def rf_tune_for_channel(self, channel_name: str) -> int:
        if channel_name == LOWER_CHANNEL_NAME:
            return self.lower_rf_tune_hz
        if channel_name == UPPER_CHANNEL_NAME:
            return self.upper_rf_tune_hz
        raise VdeterValidationError(f"알 수 없는 logical channel: {channel_name}")


def _resolved_json_value(
    *,
    run_id: str,
    run_mode: str,
    endpoint_id: str,
    b210_serial: str,
    endpoint: EndpointConfig,
    radio: RadioConfiguration,
    host_execution: Stage7HostExecutionPlan,
    time_plan: Stage7TimePlan,
    lanes,
    data_link_id: int,
    deployment_manifest_relpath: str,
    deployment_manifest_sha256: str,
    endpoint_common_rx_gain_db: int,
    source_gain_artifact_sha256,
    source_sha256: str,
    data_profile_sha256: str,
) -> dict:
    channels = []
    for setting, binding in zip(radio.channels, radio.channel_bindings):
        channels.append(
            {
                "name": setting.channel.name,
                "center_frequency_hz": setting.center_frequency_hz,
                "sample_rate_hz": setting.sample_rate_hz,
                "tx_gain_db": setting.tx_gain_db,
                "rx_gain_db": setting.rx_gain_db,
                "waveform_bandwidth_hz": binding.waveform_bandwidth_hz,
                "analog_bandwidth_hz": binding.analog_bandwidth_hz,
                "tx_enabled": binding.tx_enabled,
                "rx_enabled": binding.rx_enabled,
            }
        )
    return {
        "resolved_type": "STAGE7_RESOLVED_ENDPOINT_CONFIG_V1",
        "source_schema_version": STAGE7_SOURCE_SCHEMA_VERSION,
        "run_id": run_id,
        "run_mode": run_mode,
        "endpoint_id": endpoint_id,
        "b210_serial": b210_serial,
        "role": endpoint.role.value,
        "station_id": endpoint.station_id.value,
        "scope": endpoint.scope.value,
        "code_version": endpoint.code_version,
        "service": {
            "max_payload_bytes": endpoint.service.max_payload_bytes,
            "max_concurrent_transfers": endpoint.service.max_concurrent_transfers,
        },
        "link": {
            "supported_link_ids": [item.value for item in endpoint.link.supported_link_ids]
        },
        "mac": {
            "link_id": endpoint.mac.link_id.value,
            "tx_channel": endpoint.mac.tx_channel.name,
            "rx_channel": endpoint.mac.rx_channel.name,
            "transmit_lead_nanoseconds": endpoint.mac.transmit_lead_nanoseconds,
            "data_link_id": endpoint.mac.data_link_id.value,
        },
        "phy": {"sample_rate_hz": endpoint.phy.sample_rate_hz},
        "sources": {
            "time": endpoint.sources.time.value,
            "random": endpoint.sources.random.value,
            "id": endpoint.sources.id.value,
            "seed": endpoint.sources.seed,
        },
        "logging": {
            "jsonl_enabled": endpoint.logging.jsonl_enabled,
            "console_enabled": endpoint.logging.console_enabled,
        },
        "security": {
            "private_key_ref": endpoint.security.private_key_ref,
            "certificate_ref": endpoint.security.certificate_ref,
            "trust_store_ref": endpoint.security.trust_store_ref,
        },
        "radio": {
            "adapter": radio.adapter_kind,
            "device_selector": radio.device_selector,
            "clock_reference": radio.clock_reference.value,
            "sample_format": radio.sample_format,
            "channels": channels,
        },
        "host_execution": host_execution.as_json_value(),
        "stage7": {
            "run_mode": run_mode,
            "lane_map_profile": STAGE7_LANE_MAP_PROFILE,
            "frequency_translation_hz": FREQUENCY_TRANSLATION_HZ,
            "lower_rf_tune_hz": LOWER_RF_TUNE_HZ,
            "upper_rf_tune_hz": UPPER_RF_TUNE_HZ,
            "phy_facade": STAGE7_PHY_FACADE,
            "timing_status_projection": STAGE7_TIMING_STATUS_PROJECTION,
            "deployment_manifest_relpath": deployment_manifest_relpath,
            "deployment_manifest_sha256": deployment_manifest_sha256,
            "registry_fingerprint": STAGE7_REGISTRY_FINGERPRINT,
            "tbb_fixture_id": STAGE7_TBB_FIXTURE_ID,
            **time_plan.as_json_value(),
        },
        "lanes": [lane.as_json_value() for lane in lanes],
        "data_link_id": data_link_id,
        "endpoint_common_rx_gain_db": endpoint_common_rx_gain_db,
        "source_gain_artifact_sha256": source_gain_artifact_sha256,
        "source_sha256": source_sha256,
        "data_profile_sha256": data_profile_sha256,
    }


def load_data_profile(text: str):
    """단일 `data-profile.toml`을 해석한다. `data_link_id`는 17 또는 19만 허용한다."""

    if _toml is None:  # pragma: no cover - 환경 의존
        return _error(ErrorCode.CONFIG_PARSE_FAILED, "TOML parser가 없습니다")
    try:
        data = _toml.loads(text)
    except Exception as error:  # noqa: BLE001
        return _error(ErrorCode.CONFIG_PARSE_FAILED, f"TOML parse 실패: {error}")
    unknown = _check_unknown("<data-profile>", data, _DATA_PROFILE_KEYS)
    if unknown is not None:
        return unknown
    missing = _require_key("<data-profile>", data, "data_link_id")
    if missing is not None:
        return missing
    value = data["data_link_id"]
    if isinstance(value, bool) or not isinstance(value, int) or value not in (17, 19):
        return _semantic_error(
            "data_link_id는 17 또는 19만 허용합니다", data_link_id=str(value)
        )
    return Ok(value)


class Stage7ConfigurationLoader:
    """schema v4 endpoint file 하나와 data profile 하나를 run별 immutable
    resolved config로 만든다."""

    __slots__ = ()

    def load_endpoint_configuration(
        self,
        text: str,
        data_profile_text: str,
        *,
        run_id: str,
        operational_gain: Stage7OperationalGainInput | None = None,
        source_path: str = "",
    ):
        if _toml is None:  # pragma: no cover - 환경 의존
            return _error(ErrorCode.CONFIG_PARSE_FAILED, "TOML parser가 없습니다")
        if not is_valid_run_id(run_id):
            return _semantic_error("run_id 형식이 잘못되었습니다", run_id=str(run_id))
        data_link = load_data_profile(data_profile_text)
        if data_link.is_err:
            return data_link
        data_link_id = data_link.value
        try:
            data = _toml.loads(text)
        except Exception as error:  # noqa: BLE001
            return _error(ErrorCode.CONFIG_PARSE_FAILED, f"TOML parse 실패: {error}")

        # --- top level ---
        unknown = _check_unknown("<root>", data, _TOP_LEVEL_KEYS)
        if unknown is not None:
            return unknown
        if data.get("schema_version") != STAGE7_SOURCE_SCHEMA_VERSION:
            return _schema_error(
                "schema_version은 integer literal 4여야 합니다",
                schema_version=str(data.get("schema_version")),
            )
        if isinstance(data.get("schema_version"), bool):
            return _schema_error("schema_version은 bool이 아닌 integer여야 합니다")
        for section in (*_REQUIRED_COMMON_SECTIONS, "radio", "host_execution", "stage7"):
            if section not in data:
                return _schema_error(f"[{section}] table이 필수입니다", section=section)

        # --- 공통 table (v3와 같은 key·type·범위) ---
        for section, allowed in _COMMON_SECTION_KEYS.items():
            content = data.get(section)
            if content is None:
                continue
            if not isinstance(content, dict):
                return _schema_error(f"[{section}]는 table이어야 합니다", section=section)
            unknown = _check_unknown(section, content, allowed)
            if unknown is not None:
                return unknown
        for section in _REQUIRED_COMMON_SECTIONS:
            required = {
                "endpoint": ("id", "role", "station_id", "scope", "code_version"),
                "service": ("max_payload_bytes", "max_concurrent_transfers"),
                "link": ("supported_link_ids",),
                "mac": ("link_id", "tx_channel", "rx_channel", "transmit_lead_nanoseconds"),
                "phy": ("sample_rate_hz",),
            }[section]
            for key in required:
                missing = _require_key(section, data[section], key)
                if missing is not None:
                    return missing

        # --- radio table (§15 Stage 7 규칙) ---
        radio_section, error = _require_table("radio", data)
        if error is not None:
            return error
        unknown = _check_unknown("radio", radio_section, _RADIO_KEYS)
        if unknown is not None:
            return unknown
        for key in _RADIO_KEYS:
            missing = _require_key("radio", radio_section, key)
            if missing is not None:
                return missing
        if radio_section["adapter"] != STAGE7_ADAPTER_KIND:
            return _semantic_error(
                "radio.adapter는 b210-stage7-affine이어야 합니다",
                adapter=str(radio_section["adapter"]),
            )
        if radio_section["clock_reference"] != "INTERNAL":
            return _semantic_error("radio.clock_reference는 INTERNAL이어야 합니다")
        if radio_section["sample_format"] != SAMPLE_FORMAT_CF32LE:
            return _semantic_error("radio.sample_format은 cf32le여야 합니다")
        selector, error = _string_value("radio", "device_selector", radio_section["device_selector"])
        if error is not None:
            return error
        if not selector.startswith("serial="):
            return _semantic_error(
                "radio.device_selector는 serial=<exact-serial>이어야 합니다"
            )
        serial = selector[len("serial="):]
        serial_is_placeholder = serial == REQUIRED_UNSET
        if not serial_is_placeholder and not is_exact_serial(serial):
            return _semantic_error("radio.device_selector serial이 잘못되었습니다")

        timing_section = radio_section["timing"]
        if not isinstance(timing_section, dict):
            return _schema_error("[radio.timing]는 table이어야 합니다")
        unknown = _check_unknown("radio.timing", timing_section, _TIMING_KEYS)
        if unknown is not None:
            return unknown
        for key in _TIMING_KEYS:
            missing = _require_key("radio.timing", timing_section, key)
            if missing is not None:
                return missing
        if (
            timing_section["profile"] != "INTERNAL_FREE_RUNNING"
            or timing_section["clock_source"] != "INTERNAL"
            or timing_section["time_source"] != "INTERNAL"
            or timing_section["pps_source"] != "NONE"
            or timing_section["mapping_attempt_count"] != 8
            or timing_section["mapping_valid_for_nanoseconds"] != 1_000_000_000
            or timing_section["max_mapping_uncertainty_ns"] != 10_000_000
        ):
            return _semantic_error(
                "radio.timing은 INTERNAL_FREE_RUNNING/INTERNAL/INTERNAL/NONE과 "
                "canonical 8/1000000000/10000000이어야 합니다"
            )
        for key in ("mapping_attempt_count", "mapping_valid_for_nanoseconds", "max_mapping_uncertainty_ns"):
            if isinstance(timing_section[key], bool):
                return _schema_error(f"radio.timing.{key}는 bool이 아닌 integer여야 합니다")

        limits_section = radio_section["resource_limits"]
        if not isinstance(limits_section, dict):
            return _schema_error("[radio.resource_limits]는 table이어야 합니다")
        unknown = _check_unknown("radio.resource_limits", limits_section, _RESOURCE_LIMIT_KEYS)
        if unknown is not None:
            return unknown
        for key in _RESOURCE_LIMIT_KEYS:
            missing = _require_key("radio.resource_limits", limits_section, key)
            if missing is not None:
                return missing
            if isinstance(limits_section[key], bool) or not isinstance(limits_section[key], int):
                return _schema_error(
                    f"radio.resource_limits.{key}는 bool이 아닌 integer여야 합니다"
                )

        stream_section = radio_section["b210_stream"]
        if not isinstance(stream_section, dict):
            return _schema_error("[radio.b210_stream]는 table이어야 합니다")
        unknown = _check_unknown("radio.b210_stream", stream_section, _B210_STREAM_KEYS)
        if unknown is not None:
            return unknown
        for key in _B210_STREAM_KEYS:
            missing = _require_key("radio.b210_stream", stream_section, key)
            if missing is not None:
                return missing
        if (
            stream_section["master_clock_rate_hz"] != MASTER_CLOCK_RATE_HZ
            or isinstance(stream_section["master_clock_rate_hz"], bool)
            or stream_section["cpu_format"] != "fc32"
            or stream_section["otw_format"] != "sc16"
        ):
            return _semantic_error("radio.b210_stream은 30720000/fc32/sc16이어야 합니다")

        channels = radio_section["channels"]
        if not isinstance(channels, list) or len(channels) != 2:
            return _schema_error(
                "[[radio.channels]]는 lower/upper 두 entry여야 합니다"
            )

        # --- host_execution ---
        host_section, error = _require_table("host_execution", data)
        if error is not None:
            return error
        unknown = _check_unknown("host_execution", host_section, _HOST_EXECUTION_KEYS)
        if unknown is not None:
            return unknown
        host_values = {}
        for key in _HOST_EXECUTION_KEYS:
            missing = _require_key("host_execution", host_section, key)
            if missing is not None:
                return missing
            value, error = _string_value("host_execution", key, host_section[key])
            if error is not None:
                return error
            host_values[key] = value
        if host_values["profile"] != STAGE7_HOST_PROFILE:
            return _semantic_error(
                "host_execution.profile이 canonical Stage 7 profile이 아닙니다",
                profile=host_values["profile"],
            )
        if host_values["provider_id"] != "none" or host_values["provider_version"] != "none":
            return _semantic_error("host_execution.provider_id/version은 canonical none이어야 합니다")
        if host_values["pi_power_profile"] != STAGE7_PI_POWER_PROFILE:
            return _semantic_error("host_execution.pi_power_profile이 승인값과 다릅니다")
        for key, value in host_values.items():
            if value == REQUIRED_UNSET and key not in _HOST_EXECUTION_PROVISIONING_KEYS:
                return _semantic_error(
                    "REQUIRED_UNSET은 provisioning field에만 허용됩니다", field=key
                )

        # --- stage7 ---
        stage7_section, error = _require_table("stage7", data)
        if error is not None:
            return error
        unknown = _check_unknown("stage7", stage7_section, _STAGE7_KEYS)
        if unknown is not None:
            return unknown
        for key in _STAGE7_KEYS:
            missing = _require_key("stage7", stage7_section, key)
            if missing is not None:
                return missing
        run_mode = stage7_section["run_mode"]
        if run_mode not in RUN_MODES:
            return _semantic_error(
                "stage7.run_mode는 RF_OFF_QUALIFICATION|GAIN_CALIBRATION|OPERATIONAL이어야 합니다",
                run_mode=str(run_mode),
            )
        for key, expected in STAGE7_FIXED_STRINGS.items():
            if stage7_section[key] != expected:
                return _semantic_error(
                    f"stage7.{key}가 canonical literal과 다릅니다",
                    field=key,
                    value=str(stage7_section[key]),
                )
        for key, expected in STAGE7_FIXED_INTEGERS.items():
            value = stage7_section[key]
            if isinstance(value, bool) or not isinstance(value, int):
                return _schema_error(f"stage7.{key}는 bool이 아닌 integer여야 합니다")
            if value != expected:
                return _semantic_error(
                    f"stage7.{key}가 이 문서의 고정값과 다릅니다", field=key, value=value
                )
            if not 0 <= value <= MAX_U63:
                return _semantic_error(f"stage7.{key} 범위 초과", field=key)
        for key in ("deployment_manifest_relpath", "deployment_manifest_sha256"):
            if stage7_section[key] == REQUIRED_UNSET:
                return _semantic_error(
                    "REQUIRED_UNSET은 이 field에 허용되지 않습니다", field=key
                )
        if not is_valid_relpath(stage7_section["deployment_manifest_relpath"]):
            return _semantic_error("stage7.deployment_manifest_relpath가 relpath 규칙에 어긋납니다")
        if not is_valid_sha256(stage7_section["deployment_manifest_sha256"]):
            return _semantic_error("stage7.deployment_manifest_sha256이 sha256이 아닙니다")

        # --- 공통 table 값 구성 (frozen v3와 같은 type/범위 규칙) ---
        endpoint_section = data["endpoint"]
        endpoint_id = endpoint_section.get("id")
        if endpoint_id not in ENDPOINT_IDS:
            return _semantic_error(
                "endpoint.id는 MASS-A|MASS-B|ROC여야 합니다", endpoint_id=str(endpoint_id)
            )
        expected_role, expected_station = CANONICAL_ROLES[endpoint_id]
        sources_section = data.get("sources", {})
        logging_section = data.get("logging", {})
        security_section = data.get("security", {})
        test_section = data.get("test", {})
        try:
            endpoint_config = EndpointConfig(
                schema_version=3,
                endpoint_id=endpoint_section["id"],
                role=ServiceRole(endpoint_section["role"]),
                station_id=StationId(endpoint_section["station_id"]),
                scope=RegistryScope(endpoint_section["scope"]),
                code_version=endpoint_section["code_version"],
                service=ServiceConfig(
                    max_payload_bytes=data["service"]["max_payload_bytes"],
                    max_concurrent_transfers=data["service"]["max_concurrent_transfers"],
                ),
                link=LinkConfig(
                    supported_link_ids=tuple(
                        LinkId(value) for value in data["link"]["supported_link_ids"]
                    )
                ),
                mac=MacConfig(
                    link_id=LinkId(data["mac"]["link_id"]),
                    tx_channel=PhysicalChannel(data["mac"]["tx_channel"]),
                    rx_channel=PhysicalChannel(data["mac"]["rx_channel"]),
                    transmit_lead_nanoseconds=data["mac"]["transmit_lead_nanoseconds"],
                    data_link_id=LinkId(data_link_id),
                ),
                phy=PhyConfig(sample_rate_hz=data["phy"]["sample_rate_hz"]),
                sources=SourceSelection(
                    time=TimeSourceKind(sources_section.get("time", "SYSTEM")),
                    random=RandomSourceKind(sources_section.get("random", "SYSTEM")),
                    id=IdSourceKind(sources_section.get("id", "UUID4")),
                    seed=sources_section.get("seed"),
                ),
                logging=LoggingConfig(
                    jsonl_enabled=logging_section.get("jsonl_enabled", True),
                    console_enabled=logging_section.get("console_enabled", False),
                ),
                security=SecurityConfig(
                    private_key_ref=security_section.get("private_key_ref", ""),
                    certificate_ref=security_section.get("certificate_ref", ""),
                    trust_store_ref=security_section.get("trust_store_ref", ""),
                ),
                test=TestScopeConfig(
                    peer_station_id=(
                        StationId(test_section["peer_station_id"])
                        if "peer_station_id" in test_section
                        else None
                    )
                ),
            )
        except (VdeterValidationError, ValueError) as error:
            return _semantic_error(f"설정 값이 올바르지 않습니다: {error}")

        if endpoint_config.role is not expected_role:
            return _semantic_error(
                "endpoint role이 §7 canonical mapping과 다릅니다",
                endpoint_id=endpoint_id,
                role=endpoint_config.role.value,
            )
        if endpoint_config.station_id.value != expected_station:
            return _semantic_error(
                "station ID가 §7 canonical mapping과 다릅니다",
                endpoint_id=endpoint_id,
                station_id=endpoint_config.station_id.value,
            )
        if endpoint_config.mac.link_id.value != 11:
            return _semantic_error("mac.link_id(제어)는 11이어야 합니다")
        supported = {value.value for value in endpoint_config.link.supported_link_ids}
        if supported != {11, 17, 19}:
            return _semantic_error(
                "link.supported_link_ids는 {11, 17, 19}여야 합니다",
                supported=tuple(sorted(supported)),
            )
        if endpoint_config.phy.sample_rate_hz != DEVICE_SAMPLE_RATE_HZ:
            return _semantic_error("phy.sample_rate_hz는 307200이어야 합니다")
        if (
            endpoint_config.mac.tx_channel.name not in CANONICAL_CHANNEL_NAMES
            or endpoint_config.mac.rx_channel.name not in CANONICAL_CHANNEL_NAMES
        ):
            return _semantic_error("mac.tx_channel/rx_channel이 wideband channel이 아닙니다")

        # --- channels 해석 (§15) ---
        expected_directions = _EXPECTED_DIRECTIONS[endpoint_config.role]
        by_name = {}
        for channel in channels:
            if not isinstance(channel, dict):
                return _schema_error("radio.channels 항목은 table이어야 합니다")
            forbidden = sorted(set(channel) & _FORBIDDEN_CHANNEL_KEYS)
            if forbidden:
                return _schema_error(
                    "source channel에 hardware key를 둘 수 없습니다",
                    fields=tuple(forbidden),
                )
            unknown = _check_unknown("radio.channels", channel, _CHANNEL_KEYS)
            if unknown is not None:
                return unknown
            for key in _CHANNEL_KEYS:
                missing = _require_key("radio.channels", channel, key)
                if missing is not None:
                    return missing
            by_name[channel.get("name")] = channel
        if tuple(item.get("name") for item in channels) != CANONICAL_CHANNEL_NAMES:
            return _semantic_error(
                "radio.channels는 canonical alias 순서의 lower/upper 두 entry여야 합니다"
            )

        resolved_settings = []
        resolved_bindings = []
        expected_centers = {
            LOWER_CHANNEL_NAME: LOWER_LOGICAL_CENTER_HZ,
            UPPER_CHANNEL_NAME: UPPER_LOGICAL_CENTER_HZ,
        }
        common_gain = 0
        gain_artifact = None
        if run_mode == "OPERATIONAL":
            if operational_gain is None:
                return _semantic_error(
                    "OPERATIONAL run은 signed gain artifact 입력이 필요합니다"
                )
            common_gain = operational_gain.endpoint_common_rx_gain_db
            gain_artifact = operational_gain.source_gain_artifact_sha256
        elif operational_gain is not None:
            return _semantic_error(
                "RF-off/gain-calibration run에는 gain artifact를 결속할 수 없습니다"
            )

        for index, name in enumerate(CANONICAL_CHANNEL_NAMES):
            channel = by_name[name]
            tx_enabled, error = _bool_value("radio.channels", "tx_enabled", channel["tx_enabled"])
            if error is not None:
                return error
            rx_enabled, error = _bool_value("radio.channels", "rx_enabled", channel["rx_enabled"])
            if error is not None:
                return error
            if (tx_enabled, rx_enabled) != expected_directions[index]:
                return _semantic_error(
                    "role별 TX/RX enable이 §15 canonical 방향과 다릅니다", channel=name
                )
            for key, expected in (
                ("center_frequency_hz", expected_centers[name]),
                ("sample_rate_hz", DEVICE_SAMPLE_RATE_HZ),
                ("waveform_bandwidth_hz", WAVEFORM_BANDWIDTH_HZ),
                ("analog_bandwidth_hz", ANALOG_BANDWIDTH_HZ),
                ("tx_gain_db", 0),
            ):
                value = channel[key]
                if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                    return _semantic_error(
                        f"radio.channels.{key}가 고정값과 다릅니다", channel=name, field=key
                    )
            rx_gain_source = channel["rx_gain_db"]
            if rx_gain_source == REQUIRED_UNSET:
                if not rx_enabled:
                    return _semantic_error(
                        "RX-disabled channel의 rx_gain_db는 canonical 0이어야 합니다",
                        channel=name,
                    )
                resolved_rx_gain = common_gain if run_mode == "OPERATIONAL" else 0
            elif isinstance(rx_gain_source, bool) or not isinstance(rx_gain_source, int):
                return _schema_error(
                    "rx_gain_db는 integer 또는 REQUIRED_UNSET이어야 합니다", channel=name
                )
            else:
                if not rx_enabled:
                    if rx_gain_source != 0:
                        return _semantic_error(
                            "RX-disabled channel의 rx_gain_db는 canonical 0이어야 합니다",
                            channel=name,
                        )
                    resolved_rx_gain = 0
                else:
                    expected_gain = common_gain if run_mode == "OPERATIONAL" else 0
                    if rx_gain_source != expected_gain:
                        return _semantic_error(
                            "rx_gain_db가 run_mode의 resolved gain과 다릅니다",
                            channel=name,
                            value=rx_gain_source,
                        )
                    resolved_rx_gain = expected_gain
            resolved_settings.append(
                RadioChannelSetting(
                    channel=PhysicalChannel(name),
                    center_frequency_hz=expected_centers[name],
                    sample_rate_hz=DEVICE_SAMPLE_RATE_HZ,
                    tx_gain_db=0,
                    rx_gain_db=resolved_rx_gain,
                )
            )
            resolved_bindings.append(
                RadioChannelBinding(
                    channel=PhysicalChannel(name),
                    waveform_bandwidth_hz=WAVEFORM_BANDWIDTH_HZ,
                    analog_bandwidth_hz=ANALOG_BANDWIDTH_HZ,
                    tx_enabled=tx_enabled,
                    rx_enabled=rx_enabled,
                    tx_device_channel=None,
                    rx_device_channel=None,
                    tx_antenna=None,
                    rx_antenna=None,
                )
            )

        # --- resolved placeholder 검증 ---
        if serial_is_placeholder:
            return _semantic_error(
                "resolved config에는 exact serial placeholder가 남을 수 없습니다",
                endpoint_id=endpoint_id,
            )
        placeholders = tuple(
            key for key, value in host_values.items() if value == REQUIRED_UNSET
        )
        if placeholders:
            return _semantic_error(
                "resolved config에는 host_execution placeholder가 남을 수 없습니다",
                fields=placeholders,
            )
        if host_values["usb_transport"] not in USB_TRANSPORTS:
            return _semantic_error(
                "usb_transport가 canonical mapping이 아닙니다",
                usb_transport=host_values["usb_transport"],
            )
        expected_transport = (
            "DIRECT_USB2_480M_EXTERNAL_DC" if endpoint_id == "MASS-B" else "DIRECT_USB3_5000M"
        )
        if host_values["usb_transport"] != expected_transport:
            return _semantic_error(
                "endpoint별 canonical USB transport와 다릅니다",
                endpoint_id=endpoint_id,
                usb_transport=host_values["usb_transport"],
            )
        if endpoint_id == "MASS-B":
            if host_values["external_power_asset"] in ("PI_BUS_POWER", "none", REQUIRED_UNSET):
                return _semantic_error(
                    "MASS-B external_power_asset은 실제 외부 DC asset ID여야 합니다"
                )
        else:
            if host_values["external_power_asset"] != "PI_BUS_POWER":
                return _semantic_error(
                    "ROC/MASS-A external_power_asset은 PI_BUS_POWER여야 합니다",
                    endpoint_id=endpoint_id,
                )
        if not is_valid_sha256(host_values["environment_manifest_sha256"]):
            return _semantic_error("environment_manifest_sha256이 sha256이 아닙니다")

        try:
            timing = RadioTimingConfiguration(
                profile=RadioTimingProfile.INTERNAL_FREE_RUNNING,
                clock_source=ClockReference.INTERNAL,
                time_source=ClockReference.INTERNAL,
                pps_source=ClockReference.NONE,
                mapping_attempt_count=8,
                mapping_valid_for_nanoseconds=1_000_000_000,
                max_mapping_uncertainty_ns=10_000_000,
            )
            limits = RadioResourceLimits(**limits_section)
            stream = RadioB210StreamConfiguration(
                master_clock_rate_hz=MASTER_CLOCK_RATE_HZ,
                cpu_format="fc32",
                otw_format="sc16",
            )
            radio_config = RadioConfiguration(
                adapter_kind=STAGE7_ADAPTER_KIND,
                device_selector=selector,
                clock_reference=ClockReference.INTERNAL,
                channels=tuple(resolved_settings),
                sample_format=SAMPLE_FORMAT_CF32LE,
                timing=timing,
                resource_limits=limits,
                channel_bindings=tuple(resolved_bindings),
                b210_stream=stream,
            )
        except (VdeterValidationError, ValueError) as error:
            return _semantic_error(f"Radio 설정 값이 올바르지 않습니다: {error}")

        host_execution = Stage7HostExecutionPlan(**host_values)
        time_plan = Stage7TimePlan(
            clock_bridge_profile=STAGE7_CLOCK_BRIDGE_PROFILE,
            max_bridge_generations=3,
            host_time_min_observations=8,
            host_time_min_span_s=2,
            host_time_max_acquisition_s=600,
            host_time_observation_period_s=1,
            host_time_max_bracket_ns=2_000_000,
            host_time_tracking_period_s=5,
            rf_time_reference_max_gap_s=5,
            rf_time_loss_gap_s=10,
            active_command_horizon_s=120,
            cfo_profile=STAGE7_CFO_PROFILE,
            cfo_numeric_profile=STAGE7_CFO_NUMERIC_PROFILE,
            cfo_accept_bound_hz=1_800,
        )
        lane_entries = CANONICAL_LANE_MAP[endpoint_id]
        lanes = tuple(
            Stage7LaneBinding(
                lane_index=index,
                lane_id="RF_A" if index == 0 else "RF_B",
                tx_path=entry["tx_path"],
                rx_path=entry["rx_path"],
                tx_channel=entry["tx_channel"],
                rx_channel=entry["rx_channel"],
                tx_antenna="TX/RX",
                rx_antenna="RX2",
            )
            for index, entry in enumerate(lane_entries)
        )

        source_sha256 = sha256_hex(text.encode("utf-8"))
        data_profile_sha256 = sha256_hex(data_profile_text.encode("utf-8"))
        resolved_value = _resolved_json_value(
            run_id=run_id,
            run_mode=run_mode,
            endpoint_id=endpoint_id,
            b210_serial=serial,
            endpoint=endpoint_config,
            radio=radio_config,
            host_execution=host_execution,
            time_plan=time_plan,
            lanes=lanes,
            data_link_id=data_link_id,
            deployment_manifest_relpath=stage7_section["deployment_manifest_relpath"],
            deployment_manifest_sha256=stage7_section["deployment_manifest_sha256"],
            endpoint_common_rx_gain_db=common_gain,
            source_gain_artifact_sha256=gain_artifact,
            source_sha256=source_sha256,
            data_profile_sha256=data_profile_sha256,
        )
        resolved_bytes = encode_canonical_json(resolved_value)
        return Ok(
            Stage7ResolvedConfiguration(
                source_schema_version=STAGE7_SOURCE_SCHEMA_VERSION,
                run_id=run_id,
                run_mode=run_mode,
                endpoint_id=endpoint_id,
                b210_serial=serial,
                endpoint=endpoint_config,
                radio=radio_config,
                host_execution=host_execution,
                time_plan=time_plan,
                lane_map_profile=STAGE7_LANE_MAP_PROFILE,
                lanes=lanes,
                frequency_translation_hz=FREQUENCY_TRANSLATION_HZ,
                lower_rf_tune_hz=LOWER_RF_TUNE_HZ,
                upper_rf_tune_hz=UPPER_RF_TUNE_HZ,
                data_link_id=data_link_id,
                phy_facade=STAGE7_PHY_FACADE,
                timing_status_projection=STAGE7_TIMING_STATUS_PROJECTION,
                deployment_manifest_relpath=stage7_section["deployment_manifest_relpath"],
                deployment_manifest_sha256=stage7_section["deployment_manifest_sha256"],
                registry_fingerprint=STAGE7_REGISTRY_FINGERPRINT,
                tbb_fixture_id=STAGE7_TBB_FIXTURE_ID,
                endpoint_common_rx_gain_db=common_gain,
                source_gain_artifact_sha256=gain_artifact,
                source_sha256=source_sha256,
                source_path=source_path,
                data_profile_sha256=data_profile_sha256,
                resolved_canonical_bytes=resolved_bytes,
                resolved_sha256=canonical_sha256(resolved_value),
            )
        )


__all__ = (
    "CANONICAL_CHANNEL_NAMES",
    "CANONICAL_LANE_MAP",
    "CANONICAL_ROLES",
    "ENDPOINT_IDS",
    "FREQUENCY_TRANSLATION_HZ",
    "LOWER_CHANNEL_NAME",
    "LOWER_LOGICAL_CENTER_HZ",
    "LOWER_RF_TUNE_HZ",
    "PATH_IDS",
    "REQUIRED_UNSET",
    "RUN_MODES",
    "STAGE7_ADAPTER_KIND",
    "STAGE7_CFO_NUMERIC_PROFILE",
    "STAGE7_CFO_PROFILE",
    "STAGE7_CLOCK_BRIDGE_PROFILE",
    "STAGE7_FIXTURE_CLASSIFICATION",
    "STAGE7_HOST_PROFILE",
    "STAGE7_LANE_MAP_PROFILE",
    "STAGE7_PHY_FACADE",
    "STAGE7_REGISTRY_FINGERPRINT",
    "STAGE7_SOURCE_SCHEMA_VERSION",
    "STAGE7_TBB_FIXTURE_ID",
    "STAGE7_TIMING_STATUS_PROJECTION",
    "Stage7ConfigurationLoader",
    "Stage7HostExecutionPlan",
    "Stage7LaneBinding",
    "Stage7OperationalGainInput",
    "Stage7ResolvedConfiguration",
    "Stage7TimePlan",
    "UPPER_CHANNEL_NAME",
    "UPPER_LOGICAL_CENTER_HZ",
    "UPPER_RF_TUNE_HZ",
    "is_exact_serial",
    "is_valid_relpath",
    "is_valid_run_id",
    "is_valid_sha256",
    "load_data_profile",
)
