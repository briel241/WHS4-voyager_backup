"""TOML 설정 loader (`stage0_baseline.md` §8).

- 설정 파일은 TOML만 사용한다.
- root `schema_version`은 필수다.
- unknown section과 field는 오류다.
- 운영 설정과 시험 설정은 명시적 root scope(`endpoint.scope`)로 분리한다.
- 설정은 승인된 standard profile을 선택할 수 있지만 표준 field를 override하지 않는다.
- secret, private key와 certificate material은 참조로만 지정한다.
- parse, schema validation, semantic validation을 구분해 구조화된 시작 오류로 변환한다.
- Python 3.11 이상은 `tomllib`, 3.10은 조건부 dependency `tomli`를 사용한다.
"""

from __future__ import annotations

from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SAMPLE_FORMAT_BYTES_PER_SAMPLE,
    TEST_SCOPE_SAMPLE_FORMATS,
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
from ..profiles.models import RegistryScope
from ..radio.models import TEST_SCOPE_ADAPTER_KINDS, RadioAdapterKind
from .models import (
    CONFIG_SCHEMA_CURRENT_VERSION,
    CONFIG_SCHEMA_SUPPORTED_VERSIONS,
    EndpointConfig,
    IdSourceKind,
    LinkConfig,
    LoadedConfiguration,
    LoggingConfig,
    MacConfig,
    PhyConfig,
    RandomSourceKind,
    SecurityConfig,
    ServiceConfig,
    SourceSelection,
    TEST_SCOPE_ID_SOURCES,
    TEST_SCOPE_RANDOM_SOURCES,
    TEST_SCOPE_TIME_SOURCES,
    TestScopeConfig,
    TimeSourceKind,
    fingerprint_of,
    radio_fingerprint_of,
)

try:  # Python 3.11 이상
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10 조건부 dependency
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - 환경 의존
        _toml = None

#: 설정으로 override할 수 없는 표준 profile field 이름.
FORBIDDEN_PROFILE_FIELDS = frozenset(
    {
        "packet_capacity_bytes",
        "max_messages_per_packet",
        "fragment_capacity_bytes",
        "profile_version",
        "modulation",
        "fec",
        "symbol_rate_hz",
        "vpfi",
        "message_format",
        "crc",
        "crc_polynomial",
        "training_sequence",
    }
)

#: 설정에 실제 값을 넣을 수 없는 secret 관련 이름.
FORBIDDEN_SECRET_FIELDS = frozenset(
    {
        "private_key",
        "secret",
        "password",
        "passphrase",
        "certificate",
        "certificate_body",
        "token",
    }
)

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
    }
)
_RADIO_V1_KEYS = frozenset(
    {"adapter", "device_selector", "clock_reference", "sample_format", "channels"}
)
_RADIO_V2_KEYS = _RADIO_V1_KEYS | frozenset({"timing", "resource_limits", "b210_stream"})
_SECTION_KEYS = {
    "endpoint": frozenset({"id", "role", "station_id", "scope", "code_version"}),
    "service": frozenset({"max_payload_bytes", "max_concurrent_transfers"}),
    "link": frozenset({"supported_link_ids"}),
    "mac": frozenset(
        {"link_id", "tx_channel", "rx_channel", "transmit_lead_nanoseconds", "data_link_id"}
    ),
    "phy": frozenset({"sample_rate_hz"}),
    "radio": _RADIO_V1_KEYS,
    "logging": frozenset({"jsonl_enabled", "console_enabled"}),
    "sources": frozenset({"time", "random", "id", "seed"}),
    "security": frozenset({"private_key_ref", "certificate_ref", "trust_store_ref"}),
    "test": frozenset({"peer_station_id"}),
}
_REQUIRED_SECTION_KEYS = {
    "endpoint": frozenset({"id", "role", "station_id", "scope", "code_version"}),
    "service": frozenset({"max_payload_bytes", "max_concurrent_transfers"}),
    "link": frozenset({"supported_link_ids"}),
    "mac": frozenset({"link_id", "tx_channel", "rx_channel", "transmit_lead_nanoseconds"}),
    "phy": frozenset({"sample_rate_hz"}),
    "radio": frozenset({"adapter", "clock_reference", "sample_format", "channels"}),
}
_CHANNEL_KEYS = frozenset(
    {"name", "center_frequency_hz", "sample_rate_hz", "tx_gain_db", "rx_gain_db"}
)
_V2_CHANNEL_KEYS = _CHANNEL_KEYS | frozenset(
    {
        "waveform_bandwidth_hz",
        "analog_bandwidth_hz",
        "tx_enabled",
        "rx_enabled",
        "tx_device_channel",
        "rx_device_channel",
        "tx_antenna",
        "rx_antenna",
    }
)
_V2_CHANNEL_REQUIRED_KEYS = _CHANNEL_KEYS | frozenset(
    {"waveform_bandwidth_hz", "analog_bandwidth_hz", "tx_enabled", "rx_enabled"}
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
_CANONICAL_CHANNELS = ("VDETER_LOWER_25_TEST", "VDETER_UPPER_25_TEST")
_WIDEBAND_CANONICAL_CHANNELS = ("VDETER_LOWER_100_TEST", "VDETER_UPPER_100_TEST")


def _error(code: str, message: str, **details):
    return internal_error(
        Layer.COMMON,
        code,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


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


def _check_forbidden(section: str, data: dict):
    for key in data:
        if key in FORBIDDEN_PROFILE_FIELDS:
            return _error(
                ErrorCode.PROFILE_OVERRIDE_REJECTED,
                "설정은 표준 profile field를 override할 수 없습니다",
                section=section,
                field=key,
            )
        if key in FORBIDDEN_SECRET_FIELDS:
            return _error(
                ErrorCode.CONFIG_SCHEMA_INVALID,
                "secret과 key material은 설정에 값으로 넣지 않고 참조로 지정합니다",
                section=section,
                field=key,
            )
    return None


def _require(section: str, data: dict, key: str):
    if key not in data:
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            f"[{section}]에 필수 field {key}가 없습니다",
            section=section,
            field=key,
        )
    return None


def parse_toml(text: str):
    """TOML 문자열을 dict로 parse한다. parse 실패는 구조화된 오류다."""

    if _toml is None:  # pragma: no cover - 환경 의존
        return _error(
            ErrorCode.CONFIG_PARSE_FAILED,
            "TOML parser가 없습니다. Python 3.10에서는 tomli를 설치하십시오.",
        )
    try:
        return Ok(_toml.loads(text))
    except Exception as error:
        return _error(ErrorCode.CONFIG_PARSE_FAILED, f"TOML parse 실패: {error}")


def load_configuration(text: str, source_path: str = ""):
    """TOML 문자열에서 frozen 설정 snapshot을 만든다."""

    parsed = parse_toml(text)
    if parsed.is_err:
        return parsed
    data = parsed.value

    # --- schema validation ---
    unknown = _check_unknown("<root>", data, _TOP_LEVEL_KEYS)
    if unknown is not None:
        return unknown
    schema_version = data.get("schema_version")
    if schema_version not in CONFIG_SCHEMA_SUPPORTED_VERSIONS:
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            f"root schema_version은 {CONFIG_SCHEMA_SUPPORTED_VERSIONS} 중 하나여야 합니다",
            schema_version=str(schema_version),
        )
    for section in ("endpoint", "service", "link", "mac", "phy", "radio"):
        missing = _require("<root>", data, section)
        if missing is not None:
            return missing
    for section, allowed in _SECTION_KEYS.items():
        content = data.get(section)
        if content is None:
            continue
        if not isinstance(content, dict):
            return _error(
                ErrorCode.CONFIG_SCHEMA_INVALID, f"[{section}]는 table이어야 합니다", section=section
            )
        # profile override와 inline secret은 unknown field보다 먼저 판정해 원인을
        # 구체적으로 보고한다.
        forbidden = _check_forbidden(section, content)
        if forbidden is not None:
            return forbidden
        section_allowed = _RADIO_V2_KEYS if section == "radio" and schema_version in (2, 3) else allowed
        unknown = _check_unknown(section, content, section_allowed)
        if unknown is not None:
            return unknown
        for key in _REQUIRED_SECTION_KEYS.get(section, ()):
            missing = _require(section, content, key)
            if missing is not None:
                return missing

    if schema_version in (2, 3):
        radio = data["radio"]
        for section_name, keys in (
            ("radio.timing", _TIMING_KEYS),
            ("radio.resource_limits", _RESOURCE_LIMIT_KEYS),
        ):
            short_name = section_name.split(".")[-1]
            content = radio.get(short_name)
            if not isinstance(content, dict):
                return _error(
                    ErrorCode.CONFIG_SCHEMA_INVALID,
                    f"[{section_name}] table이 필수입니다",
                    section=section_name,
                )
            unknown = _check_unknown(section_name, content, keys)
            if unknown is not None:
                return unknown
            for key in keys:
                missing = _require(section_name, content, key)
                if missing is not None:
                    return missing
        stream = radio.get("b210_stream")
        if stream is not None:
            if not isinstance(stream, dict):
                return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "[radio.b210_stream]는 table이어야 합니다")
            unknown = _check_unknown("radio.b210_stream", stream, _B210_STREAM_KEYS)
            if unknown is not None:
                return unknown
            for key in _B210_STREAM_KEYS:
                missing = _require("radio.b210_stream", stream, key)
                if missing is not None:
                    return missing

    try:
        built = _build(data, source_path)
    except VdeterValidationError as error:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, f"설정 값이 올바르지 않습니다: {error}")
    except KeyError as error:
        # schema 검사가 누락되더라도 설정 입력 문제를 raw KeyError로 노출하지 않는다.
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            f"필수 설정 field가 없습니다: {error.args[0]}",
            field=str(error.args[0]),
        )
    if built.is_err:
        return built

    configuration = built.value
    semantic = _validate_semantics(configuration)
    if semantic is not None:
        return semantic
    return Ok(configuration)


def _build(data: dict, source_path: str):
    endpoint = data["endpoint"]
    radio_section = data["radio"]
    channels = radio_section["channels"]
    schema_version = data["schema_version"]
    if schema_version in (2, 3):
        adapter_name = radio_section.get("adapter")
        if adapter_name == RadioAdapterKind.SOFTWARE.value:
            if "device_selector" in radio_section:
                return _error(
                    ErrorCode.CONFIG_SCHEMA_INVALID,
                    "Software v2 설정에는 radio.device_selector를 둘 수 없습니다",
                )
            if "b210_stream" in radio_section:
                return _error(
                    ErrorCode.CONFIG_SCHEMA_INVALID,
                    "Software v2 설정에는 [radio.b210_stream]을 둘 수 없습니다",
                )
        elif adapter_name == RadioAdapterKind.B210.value:
            selector = radio_section.get("device_selector")
            if not _is_exact_serial_selector(selector):
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID,
                    "B210 device_selector는 exact serial=<serial> 한 항목이어야 합니다",
                )
            if "b210_stream" not in radio_section:
                return _error(
                    ErrorCode.CONFIG_SCHEMA_INVALID,
                    "B210 v2 설정에는 [radio.b210_stream]이 필요합니다",
                )
        else:
            return _error(
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                "v2 Radio adapter는 SOFTWARE 또는 B210이어야 합니다",
                adapter=str(adapter_name),
            )
    if not isinstance(channels, list) or not channels:
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID, "[[radio.channels]]가 최소 하나 필요합니다"
        )
    channel_settings = []
    channel_bindings = []
    for channel in channels:
        if not isinstance(channel, dict):
            return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "radio.channels 항목은 table이어야 합니다")
        channel_keys = _V2_CHANNEL_KEYS if schema_version in (2, 3) else _CHANNEL_KEYS
        required_channel_keys = (
            _V2_CHANNEL_REQUIRED_KEYS if schema_version in (2, 3) else _CHANNEL_KEYS
        )
        unknown = _check_unknown("radio.channels", channel, channel_keys)
        if unknown is not None:
            return unknown
        for key in required_channel_keys:
            missing = _require("radio.channels", channel, key)
            if missing is not None:
                return missing
        channel_settings.append(
            RadioChannelSetting(
                channel=PhysicalChannel(channel["name"]),
                center_frequency_hz=channel["center_frequency_hz"],
                sample_rate_hz=channel["sample_rate_hz"],
                tx_gain_db=channel["tx_gain_db"],
                rx_gain_db=channel["rx_gain_db"],
            )
        )
        if schema_version in (2, 3):
            channel_bindings.append(
                RadioChannelBinding(
                    channel=PhysicalChannel(channel["name"]),
                    waveform_bandwidth_hz=channel["waveform_bandwidth_hz"],
                    analog_bandwidth_hz=channel["analog_bandwidth_hz"],
                    tx_enabled=channel["tx_enabled"],
                    rx_enabled=channel["rx_enabled"],
                    tx_device_channel=channel.get("tx_device_channel"),
                    rx_device_channel=channel.get("rx_device_channel"),
                    tx_antenna=channel.get("tx_antenna"),
                    rx_antenna=channel.get("rx_antenna"),
                )
            )

    if schema_version in (2, 3):
        canonical = _CANONICAL_CHANNELS if schema_version == 2 else _WIDEBAND_CANONICAL_CHANNELS
        by_name = {value.channel.name: (value, channel_bindings[index]) for index, value in enumerate(channel_settings)}
        if set(by_name) != set(canonical) or len(channel_settings) != 2:
            return _error(
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                "radio.channels에는 이 schema의 lower와 upper channel이 각각 정확히 하나 필요합니다",
            )
        ordered = tuple(by_name[name] for name in canonical)
        channel_settings = [item[0] for item in ordered]
        channel_bindings = [item[1] for item in ordered]

    sources = data.get("sources", {})
    logging_section = data.get("logging", {})
    security_section = data.get("security", {})
    test_section = data.get("test", {})

    try:
        endpoint_config = EndpointConfig(
            schema_version=data["schema_version"],
            endpoint_id=endpoint["id"],
            role=ServiceRole(endpoint["role"]),
            station_id=StationId(endpoint["station_id"]),
            scope=RegistryScope(endpoint["scope"]),
            code_version=endpoint["code_version"],
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
                data_link_id=LinkId(data["mac"].get("data_link_id", 11)),
            ),
            phy=PhyConfig(sample_rate_hz=data["phy"]["sample_rate_hz"]),
            sources=SourceSelection(
                time=TimeSourceKind(sources.get("time", "SYSTEM")),
                random=RandomSourceKind(sources.get("random", "SYSTEM")),
                id=IdSourceKind(sources.get("id", "UUID4")),
                seed=sources.get("seed"),
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
        timing = None
        limits = None
        stream = None
        if schema_version in (2, 3):
            timing_section = radio_section["timing"]
            limits_section = radio_section["resource_limits"]
            timing = RadioTimingConfiguration(
                profile=RadioTimingProfile(timing_section["profile"]),
                clock_source=ClockReference(timing_section["clock_source"]),
                time_source=ClockReference(timing_section["time_source"]),
                pps_source=ClockReference(timing_section["pps_source"]),
                mapping_attempt_count=timing_section["mapping_attempt_count"],
                mapping_valid_for_nanoseconds=timing_section["mapping_valid_for_nanoseconds"],
                max_mapping_uncertainty_ns=timing_section["max_mapping_uncertainty_ns"],
            )
            limits = RadioResourceLimits(**limits_section)
            if "b210_stream" in radio_section:
                stream = RadioB210StreamConfiguration(**radio_section["b210_stream"])
        radio_config = RadioConfiguration(
            adapter_kind=RadioAdapterKind(radio_section["adapter"]).value,
            device_selector=radio_section.get("device_selector", ""),
            clock_reference=ClockReference(radio_section["clock_reference"]),
            channels=tuple(channel_settings),
            sample_format=radio_section["sample_format"],
            timing=timing,
            resource_limits=limits,
            channel_bindings=tuple(channel_bindings),
            b210_stream=stream,
        )
    except ValueError as error:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, f"설정 값이 올바르지 않습니다: {error}")

    return Ok(
        LoadedConfiguration(
            endpoint=endpoint_config,
            radio=radio_config,
            endpoint_fingerprint=fingerprint_of(endpoint_config),
            radio_fingerprint=radio_fingerprint_of(radio_config, schema_version),
            source_path=source_path,
        )
    )


def _validate_semantics(configuration: LoadedConfiguration):
    endpoint = configuration.endpoint
    radio = configuration.radio
    operational = endpoint.scope is RegistryScope.OPERATIONAL

    if endpoint.mac.link_id not in endpoint.link.supported_link_ids:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "mac.link_id가 link.supported_link_ids에 없습니다",
            link_id=endpoint.mac.link_id.value,
        )
    channel_names = radio.channel_names()
    for name, channel in (
        ("mac.tx_channel", endpoint.mac.tx_channel),
        ("mac.rx_channel", endpoint.mac.rx_channel),
    ):
        if channel.name not in channel_names:
            return _error(
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                f"{name}이 radio.channels에 없습니다",
                field=name,
                channel=channel.name,
            )
    if radio.sample_format not in SAMPLE_FORMAT_BYTES_PER_SAMPLE:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "등록되지 않은 sample format입니다",
            sample_format=radio.sample_format,
        )

    if endpoint.schema_version == 3:
        v3_error = _validate_v3_semantics(configuration)
        if v3_error is not None:
            return v3_error
    elif endpoint.schema_version == CONFIG_SCHEMA_CURRENT_VERSION:
        if endpoint.mac.data_link_id.value != 11:
            return _error(
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                "v2 설정의 mac.data_link_id는 11이어야 합니다",
                data_link_id=endpoint.mac.data_link_id.value,
            )
        v2_error = _validate_v2_semantics(configuration)
        if v2_error is not None:
            return v2_error

    if not operational:
        return None

    adapter = RadioAdapterKind(radio.adapter_kind)
    if adapter in TEST_SCOPE_ADAPTER_KINDS:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에서는 시험 전용 Radio adapter를 선택할 수 없습니다",
            adapter=adapter.value,
        )
    if radio.sample_format in TEST_SCOPE_SAMPLE_FORMATS:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에서는 시험 전용 sample format을 사용할 수 없습니다",
            sample_format=radio.sample_format,
        )
    if endpoint.sources.time in TEST_SCOPE_TIME_SOURCES:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에서는 시험 TimeSource를 선택할 수 없습니다",
            time_source=endpoint.sources.time.value,
        )
    if endpoint.sources.random in TEST_SCOPE_RANDOM_SOURCES:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에서는 고정 seed RandomSource를 선택할 수 없습니다",
            random_source=endpoint.sources.random.value,
        )
    if endpoint.sources.id in TEST_SCOPE_ID_SOURCES:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에서는 deterministic IdSource를 선택할 수 없습니다",
            id_source=endpoint.sources.id.value,
        )
    if endpoint.test.peer_station_id is not None:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "운영 설정에는 [test] section을 둘 수 없습니다",
        )
    return None


def _is_exact_serial_selector(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("serial="):
        return False
    serial = value[len("serial=") :]
    return bool(
        serial
        and serial.isascii()
        and serial.isprintable()
        and not any(character.isspace() for character in serial)
        and "," not in serial
        and "=" not in serial
    )


def _validate_v2_semantics(configuration: LoadedConfiguration):
    endpoint = configuration.endpoint
    radio = configuration.radio
    try:
        adapter = RadioAdapterKind(radio.adapter_kind)
    except ValueError:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "v2 Radio adapter가 잘못되었습니다")
    if adapter not in (RadioAdapterKind.SOFTWARE, RadioAdapterKind.B210):
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "v2는 SOFTWARE/B210 adapter만 허용합니다")
    if radio.clock_reference is not ClockReference.INTERNAL or radio.sample_format != SAMPLE_FORMAT_CF32LE:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v2 Radio는 INTERNAL clock_reference와 cf32le sample format이 필요합니다",
        )
    timing = radio.timing
    if timing is None or radio.resource_limits is None:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "v2 timing/resource_limits가 필요합니다")
    if (
        timing.profile is not RadioTimingProfile.INTERNAL_FREE_RUNNING
        or timing.clock_source is not ClockReference.INTERNAL
        or timing.time_source is not ClockReference.INTERNAL
        or timing.pps_source is not ClockReference.NONE
        or timing.mapping_attempt_count != 8
        or timing.mapping_valid_for_nanoseconds != 1_000_000_000
        or timing.max_mapping_uncertainty_ns != 10_000_000
    ):
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "승인된 INTERNAL_FREE_RUNNING timing 설정과 다릅니다")

    settings = {value.channel.name: value for value in radio.channels}
    bindings = {value.channel.name: value for value in radio.channel_bindings}
    if tuple(value.channel.name for value in radio.channels) != _CANONICAL_CHANNELS or tuple(
        value.channel.name for value in radio.channel_bindings
    ) != _CANONICAL_CHANNELS:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "radio channel canonical order가 잘못되었습니다")
    expected_directions = {
        ServiceRole.SHIP: ((True, False), (True, True)),
        ServiceRole.CONTROL: ((False, True), (True, False)),
    }[endpoint.role]
    expected_frequencies = {
        "VDETER_LOWER_25_TEST": 157_237_500,
        "VDETER_UPPER_25_TEST": 161_837_500,
    }
    for index, name in enumerate(_CANONICAL_CHANNELS):
        setting = settings[name]
        binding = bindings[name]
        if (
            setting.center_frequency_hz != expected_frequencies[name]
            or setting.sample_rate_hz != 76_800
            or not 0 <= setting.tx_gain_db <= 89
            or not 0 <= setting.rx_gain_db <= 76
            or binding.waveform_bandwidth_hz != 25_000
            or (binding.tx_enabled, binding.rx_enabled) != expected_directions[index]
        ):
            return _error(
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                "channel 주파수/rate/gain/bandwidth 또는 역할 방향이 승인 설정과 다릅니다",
                channel=name,
            )
        if adapter is RadioAdapterKind.SOFTWARE:
            if (
                binding.analog_bandwidth_hz != 25_000
                or any(
                    value is not None
                    for value in (
                        binding.tx_device_channel,
                        binding.rx_device_channel,
                        binding.tx_antenna,
                        binding.rx_antenna,
                    )
                )
            ):
                return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "Software binding에 hardware key를 사용할 수 없습니다", channel=name)
        else:
            if binding.analog_bandwidth_hz != 200_000:
                return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "B210 analog bandwidth는 200000 Hz여야 합니다", channel=name)
            for enabled, device_channel, antenna, expected_antenna in (
                (binding.tx_enabled, binding.tx_device_channel, binding.tx_antenna, "TX/RX"),
                (binding.rx_enabled, binding.rx_device_channel, binding.rx_antenna, "RX2"),
            ):
                if enabled:
                    if device_channel != 0 or antenna != expected_antenna:
                        return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "B210 활성 방향 binding이 잘못되었습니다", channel=name)
                elif device_channel is not None or antenna is not None:
                    return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "B210 비활성 방향 hardware key는 금지됩니다", channel=name)
    if adapter is RadioAdapterKind.SOFTWARE:
        if radio.device_selector or radio.b210_stream is not None:
            return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "Software backend에 B210 설정을 사용할 수 없습니다")
    else:
        if not _is_exact_serial_selector(radio.device_selector) or radio.b210_stream is None:
            return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "B210 selector/stream 설정이 필요합니다")
    return None


def _validate_v3_semantics(configuration: LoadedConfiguration):
    """`DR-STAGE6-MR-001` §13.2.2: 2-PC 100 kHz wideband profile semantic 검증."""

    endpoint = configuration.endpoint
    radio = configuration.radio
    try:
        adapter = RadioAdapterKind(radio.adapter_kind)
    except ValueError:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "v3 Radio adapter가 잘못되었습니다")
    if adapter is not RadioAdapterKind.SOFTWARE:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 wideband profile은 Software adapter만 지원한다. B210은 Stage 7 범위다.",
        )
    if radio.clock_reference is not ClockReference.INTERNAL or radio.sample_format != SAMPLE_FORMAT_CF32LE:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 Radio는 INTERNAL clock_reference와 cf32le sample format이 필요합니다",
        )
    timing = radio.timing
    if timing is None or radio.resource_limits is None:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "v3 timing/resource_limits가 필요합니다")
    if (
        timing.profile is not RadioTimingProfile.INTERNAL_FREE_RUNNING
        or timing.clock_source is not ClockReference.INTERNAL
        or timing.time_source is not ClockReference.INTERNAL
        or timing.pps_source is not ClockReference.NONE
        or timing.mapping_attempt_count != 8
        or timing.mapping_valid_for_nanoseconds != 1_000_000_000
        or timing.max_mapping_uncertainty_ns != 10_000_000
    ):
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "승인된 INTERNAL_FREE_RUNNING timing 설정과 다릅니다")

    if endpoint.mac.data_link_id.value not in (17, 19):
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 wideband profile의 mac.data_link_id는 17 또는 19여야 합니다",
            data_link_id=endpoint.mac.data_link_id.value,
        )
    if endpoint.mac.link_id.value != 11:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 wideband profile의 mac.link_id(제어)는 11이어야 합니다",
        )
    supported = {value.value for value in endpoint.link.supported_link_ids}
    if 11 not in supported or endpoint.mac.data_link_id.value not in supported:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 link.supported_link_ids에는 11과 data_link_id가 있어야 합니다",
        )
    if endpoint.phy.sample_rate_hz != 307_200:
        return _error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            "v3 wideband device rate는 307,200 samples/s여야 합니다",
            sample_rate_hz=endpoint.phy.sample_rate_hz,
        )

    settings = {value.channel.name: value for value in radio.channels}
    bindings = {value.channel.name: value for value in radio.channel_bindings}
    if tuple(value.channel.name for value in radio.channels) != _WIDEBAND_CANONICAL_CHANNELS or tuple(
        value.channel.name for value in radio.channel_bindings
    ) != _WIDEBAND_CANONICAL_CHANNELS:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "wideband channel canonical order가 잘못되었습니다")
    expected_directions = {
        ServiceRole.SHIP: ((True, False), (False, True)),
        ServiceRole.CONTROL: ((False, True), (True, False)),
    }[endpoint.role]
    expected_frequencies = {
        "VDETER_LOWER_100_TEST": 157_237_500,
        "VDETER_UPPER_100_TEST": 161_837_500,
    }
    for index, name in enumerate(_WIDEBAND_CANONICAL_CHANNELS):
        setting = settings[name]
        binding = bindings[name]
        if (
            setting.center_frequency_hz != expected_frequencies[name]
            or setting.sample_rate_hz != 307_200
            or setting.tx_gain_db != 0
            or setting.rx_gain_db != 0
            or binding.waveform_bandwidth_hz != 100_000
            or binding.analog_bandwidth_hz != 100_000
            or (binding.tx_enabled, binding.rx_enabled) != expected_directions[index]
        ):
            return _error(
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                "wideband channel 주파수/rate/gain/bandwidth 또는 역할 방향이 승인 설정과 다릅니다",
                channel=name,
            )
        if any(
            value is not None
            for value in (
                binding.tx_device_channel,
                binding.rx_device_channel,
                binding.tx_antenna,
                binding.rx_antenna,
            )
        ):
            return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "Software binding에 hardware key를 사용할 수 없습니다", channel=name)
    if radio.device_selector or radio.b210_stream is not None:
        return _error(ErrorCode.RADIO_UNSUPPORTED_SETTING, "Software backend에 B210 설정을 사용할 수 없습니다")
    return None


def load_configuration_file(path):
    """파일 경로에서 설정을 읽는다. 읽기 실패도 구조화된 오류로 변환한다."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return _error(ErrorCode.CONFIG_PARSE_FAILED, f"설정 파일을 읽을 수 없습니다: {error}")
    return load_configuration(text, str(path))
