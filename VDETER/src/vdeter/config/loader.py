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
    SAMPLE_FORMAT_BYTES_PER_SAMPLE,
    TEST_SCOPE_SAMPLE_FORMATS,
    ClockReference,
    RadioChannelSetting,
    RadioConfiguration,
)
from ..common.contracts.service import ServiceRole
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import VdeterValidationError
from ..common.identifiers import LinkId, PhysicalChannel, StationId
from ..common.results import Ok, internal_error
from ..profiles.models import RegistryScope
from ..radio.models import TEST_SCOPE_ADAPTER_KINDS, RadioAdapterKind
from .models import (
    CONFIG_SCHEMA_VERSION,
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
_SECTION_KEYS = {
    "endpoint": frozenset({"id", "role", "station_id", "scope", "code_version"}),
    "service": frozenset({"max_payload_bytes", "max_concurrent_transfers"}),
    "link": frozenset({"supported_link_ids"}),
    "mac": frozenset({"link_id", "tx_channel", "rx_channel", "transmit_lead_nanoseconds"}),
    "phy": frozenset({"sample_rate_hz"}),
    "radio": frozenset(
        {"adapter", "device_selector", "clock_reference", "sample_format", "channels"}
    ),
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
    if data.get("schema_version") != CONFIG_SCHEMA_VERSION:
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            f"root schema_version은 {CONFIG_SCHEMA_VERSION}이어야 합니다",
            schema_version=str(data.get("schema_version")),
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
        unknown = _check_unknown(section, content, allowed)
        if unknown is not None:
            return unknown
        for key in _REQUIRED_SECTION_KEYS.get(section, ()):
            missing = _require(section, content, key)
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
    if not isinstance(channels, list) or not channels:
        return _error(
            ErrorCode.CONFIG_SCHEMA_INVALID, "[[radio.channels]]가 최소 하나 필요합니다"
        )
    channel_settings = []
    for channel in channels:
        if not isinstance(channel, dict):
            return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "radio.channels 항목은 table이어야 합니다")
        unknown = _check_unknown("radio.channels", channel, _CHANNEL_KEYS)
        if unknown is not None:
            return unknown
        for key in _CHANNEL_KEYS:
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
        radio_config = RadioConfiguration(
            adapter_kind=RadioAdapterKind(radio_section["adapter"]).value,
            device_selector=radio_section.get("device_selector", ""),
            clock_reference=ClockReference(radio_section["clock_reference"]),
            channels=tuple(channel_settings),
            sample_format=radio_section["sample_format"],
        )
    except ValueError as error:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, f"설정 값이 올바르지 않습니다: {error}")

    return Ok(
        LoadedConfiguration(
            endpoint=endpoint_config,
            radio=radio_config,
            endpoint_fingerprint=fingerprint_of(endpoint_config),
            radio_fingerprint=fingerprint_of(radio_config),
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


def load_configuration_file(path):
    """파일 경로에서 설정을 읽는다. 읽기 실패도 구조화된 오류로 변환한다."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return _error(ErrorCode.CONFIG_PARSE_FAILED, f"설정 파일을 읽을 수 없습니다: {error}")
    return load_configuration(text, str(path))
