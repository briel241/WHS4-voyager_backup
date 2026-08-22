"""Endpoint dependency 조립 (`stage0_baseline.md` §9.1).

`EndpointDependencies`는 시작 시 `TimeSource`, `RandomSource`, `IdSource`, Radio adapter,
Payload Format·Message·Link·PHY registry, `InternalEventSink`와 `ServiceEventSink`를
주입한다. Runtime은 config의 adapter 선택을 검증한 뒤 한 번 wiring하며 실행 중 교체하지
않는다.

이 module은 계층을 조립하기만 하고 protocol 규칙을 구현하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.events import (
    InternalEventSink,
    RecordingInternalEventSink,
    RecordingServiceEventSink,
    ServiceEventSink,
)
from ..common.identifiers import DeterministicIdSource, IdSource, Uuid4IdSource
from ..common.random_source import RandomSource, SeededRandomSource, SystemRandomSource
from ..common.exceptions import VdeterInternalError
from ..common.results import Ok, internal_error
from ..common.time import SystemTimeSource, TestTimeSource, TimeSource
from ..config.models import (
    IdSourceKind,
    LoadedConfiguration,
    RandomSourceKind,
    TimeSourceKind,
)
from threading import RLock

from ..common.contracts.radio import RadioStatusRequest
from ..common.contracts.service import ServiceRole
from ..link.data_plane import LinkDataPlane
from ..link.messages.builtins import build_message_registry
from ..link.models import LinkCapabilities
from ..link.stub import StubLink
from ..link.tbb.crypto import certificate_fingerprint
from ..mac.coordinator import MacCoordinator
from ..mac.deployment import (
    EndpointDescriptor,
    MacDeploymentProfile,
    build_canonical_deployment_profile,
    config_error,
    validate_endpoint_group,
)
from ..mac.interface import Stage4Mac
from ..mac.models import MacPlanningDefaults, MacRuntimeProfile, MacStateRepository
from ..mac.stub import StubMac
from ..mac.tbb import build_canonical_tbb_profile
from ..phy.models import PhyCapabilities
from ..phy.stub import StubPhy
from ..phy.transmitter import Link11Phy
from .mac_commands import EndpointHealthLog, MacCommandOutbox, MacIngressQueue

#: registry가 반환하는 Type 4 고정 길이와 Link ID 11 Packet 용량식 (`DR-STAGE4-MAC-007`).
TYPE4_MESSAGE_BYTES = 17
EXPECTED_TYPE4_PADDING_BYTES = 16
EXPECTED_PACKET_BYTES = 54

#: Radio mapping 기준시각 오차를 흡수하는 scheduling horizon 안전여유 (한 hexslot).
SCHEDULE_HORIZON_MARGIN_NANOSECONDS = 160_000_000
from ..presentation.registry import PayloadFormatRegistry, build_payload_format_registry
from ..presentation.stub import StubPresentationAdapter
from ..profiles.link_phy import (
    build_link_profile_registry,
    build_message_profile_registry,
    build_phy_profile_registry,
)
from ..profiles.models import ExplicitRegistry
from ..radio.interface import RadioAdapter
from ..radio.b210 import B210RadioAdapter
from ..radio.fake import FakeRadioAdapter
from ..radio.models import RadioAdapterKind
from ..radio.software import SoftwareRadioAdapter


class InternalEventBus:
    """계층이 올린 내부 event를 기록하고 Runtime router에 전달한다.

    Runtime은 이 bus를 통해서만 하위 계층 event를 받는다. router는 endpoint 조립 시
    한 번 bind하며 실행 중 교체하지 않는다. bus는 event 종류를 해석하지 않고 router가
    routing 여부를 판단한다.
    """

    __slots__ = ("_sink", "_router")

    def __init__(self, sink) -> None:
        self._sink = sink
        self._router = None

    @property
    def sink(self):
        return self._sink

    def bind_router(self, router) -> None:
        if self._router is not None:
            raise VdeterInternalError("InternalEventBus router는 한 번만 bind할 수 있습니다")
        self._router = router

    def publish(self, event):
        result = self._sink.publish(event)
        if result.is_err:
            return result
        if self._router is not None:
            self._router.route_event(event)
        return result

    def events(self) -> tuple:
        return self._sink.events()

    def events_of(self, event_type) -> tuple:
        return self._sink.events_of(event_type)

    def clear(self) -> None:
        self._sink.clear()


@dataclass(frozen=True, slots=True)
class EndpointRegistries:
    """freeze된 registry 묶음과 각각의 독립 지문."""

    payload_format: PayloadFormatRegistry
    message_codec: ExplicitRegistry
    message_profile: ExplicitRegistry
    link_profile: ExplicitRegistry
    phy_profile: ExplicitRegistry

    def fingerprints(self) -> tuple[tuple[str, str], ...]:
        return (
            ("payload_format", self.payload_format.fingerprint().fingerprint),
            ("message_codec", self.message_codec.fingerprint().fingerprint),
            ("message_profile", self.message_profile.fingerprint().fingerprint),
            ("link_profile", self.link_profile.fingerprint().fingerprint),
            ("phy_profile", self.phy_profile.fingerprint().fingerprint),
        )


@dataclass(frozen=True, slots=True)
class EndpointDependencies:
    """endpoint 시작 시 주입하는 의존성. 실행 중 교체하지 않는다."""

    configuration: LoadedConfiguration
    time_source: TimeSource
    random_source: RandomSource
    id_source: IdSource
    radio: RadioAdapter
    registries: EndpointRegistries
    internal_event_sink: InternalEventBus
    service_event_sink: ServiceEventSink


def build_time_source(configuration: LoadedConfiguration):
    kind = configuration.endpoint.sources.time
    if kind is TimeSourceKind.SYSTEM:
        return Ok(SystemTimeSource())
    return Ok(TestTimeSource())


def build_random_source(configuration: LoadedConfiguration):
    selection = configuration.endpoint.sources
    if selection.random is RandomSourceKind.SYSTEM:
        return Ok(SystemRandomSource())
    return Ok(SeededRandomSource(selection.seed))


def build_id_source(configuration: LoadedConfiguration):
    selection = configuration.endpoint.sources
    if selection.id is IdSourceKind.UUID4:
        return Ok(Uuid4IdSource())
    return Ok(DeterministicIdSource(selection.seed))


def build_radio_adapter(configuration: LoadedConfiguration, time_source, id_source, event_sink):
    """config의 adapter 선택을 검증한 뒤 adapter를 만든다."""

    kind = RadioAdapterKind(configuration.radio.adapter_kind)
    if kind is RadioAdapterKind.FAKE:
        return Ok(FakeRadioAdapter(time_source, id_source, event_sink))
    if kind is RadioAdapterKind.SOFTWARE:
        return Ok(
            SoftwareRadioAdapter(
                time_source,
                id_source,
                event_sink,
                endpoint_id=configuration.endpoint.endpoint_id,
            )
        )
    if kind is RadioAdapterKind.B210:
        return Ok(B210RadioAdapter(time_source, id_source, event_sink))
    return internal_error(
        Layer.RUNTIME,
        ErrorCode.UNSUPPORTED_REQUEST,
        ErrorCategory.UNSUPPORTED,
        "지원하지 않는 Radio adapter 선택입니다",
        details=Details.of(adapter=str(kind)),
    )


def build_registries(configuration: LoadedConfiguration):
    """모든 registry를 만들고 명시적으로 등록한 뒤 freeze한다."""

    scope = configuration.endpoint.scope
    version = configuration.endpoint.code_version

    payload_result = build_payload_format_registry(scope, version)
    if payload_result.is_err:
        return payload_result
    payload_format = payload_result.value
    message_result = build_message_registry(scope, version)
    if message_result.is_err:
        return message_result
    message_codec = message_result.value
    message_profile_result = build_message_profile_registry(scope, version)
    if message_profile_result.is_err:
        return message_profile_result
    message_profile = message_profile_result.value
    link_profile_result = build_link_profile_registry(scope, version)
    if link_profile_result.is_err:
        return link_profile_result
    link_profile = link_profile_result.value
    phy_profile_result = build_phy_profile_registry(scope, version)
    if phy_profile_result.is_err:
        return phy_profile_result
    phy_profile = phy_profile_result.value

    for registry in (payload_format, message_codec, message_profile, link_profile, phy_profile):
        frozen = registry.freeze()
        if frozen.is_err:
            return frozen

    return Ok(
        EndpointRegistries(
            payload_format=payload_format,
            message_codec=message_codec,
            message_profile=message_profile,
            link_profile=link_profile,
            phy_profile=phy_profile,
        )
    )


def build_dependencies(
    configuration: LoadedConfiguration,
    internal_event_sink=None,
    service_event_sink=None,
    registries=None,
    radio=None,
):
    """설정 하나로 endpoint 의존성 전체를 조립한다.

    `registries`와 `radio`는 Stage 4 3-endpoint 시험이 운영 registry와 공유 Software Radio
    medium을 주입하기 위한 선택 인자다. 생략하면 기존 Stage 0 동작과 완전히 같다.
    """

    if not isinstance(configuration, LoadedConfiguration):
        return internal_error(
            Layer.RUNTIME,
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "LoadedConfiguration이 필요합니다",
        )
    internal_sink = internal_event_sink or RecordingInternalEventSink()
    if not isinstance(internal_sink, InternalEventBus):
        internal_sink = InternalEventBus(internal_sink)
    service_sink = service_event_sink or RecordingServiceEventSink()

    time_source = build_time_source(configuration).value
    random_source = build_random_source(configuration).value
    id_source = build_id_source(configuration).value

    if radio is None:
        radio_result = build_radio_adapter(configuration, time_source, id_source, internal_sink)
        if radio_result.is_err:
            return radio_result
        radio = radio_result.value

    if registries is None:
        registries_result = build_registries(configuration)
        if registries_result.is_err:
            return registries_result
        registries = registries_result.value

    return Ok(
        EndpointDependencies(
            configuration=configuration,
            time_source=time_source,
            random_source=random_source,
            id_source=id_source,
            radio=radio,
            registries=registries,
            internal_event_sink=internal_sink,
            service_event_sink=service_sink,
        )
    )


def build_stage0_layers(dependencies: EndpointDependencies, event_sink):
    """단계 0 Fake/Stub 계층을 조립한다.

    각 stub은 test scope 구현이며 후속 단계에서 같은 Port 계약으로 교체한다.
    """

    endpoint = dependencies.configuration.endpoint
    presentation = StubPresentationAdapter(dependencies.registries.payload_format)
    link = StubLink(
        dependencies.id_source, LinkCapabilities(endpoint.link.supported_link_ids)
    )
    phy = StubPhy(
        dependencies.time_source,
        dependencies.id_source,
        PhyCapabilities(
            supported_link_ids=endpoint.link.supported_link_ids,
            sample_rate_hz=endpoint.phy.sample_rate_hz,
        ),
        dependencies.configuration.radio.sample_format,
    )
    peer = endpoint.test.peer_station_id or endpoint.station_id
    rx_route = RouteAddresses(
        original_source=OriginalSource(peer),
        original_destination=OriginalDestination(Addressed(endpoint.station_id)),
    )
    mac = StubMac(
        dependencies.time_source,
        dependencies.id_source,
        MacPlanningDefaults(
            link_id=endpoint.mac.link_id,
            tx_channel=endpoint.mac.tx_channel,
            rx_channel=endpoint.mac.rx_channel,
            transmit_lead_nanoseconds=endpoint.mac.transmit_lead_nanoseconds,
        ),
        event_sink,
        rx_route,
    )
    return presentation, link, mac, phy


# --- Stage 4 조립 (`stage4_mac_baseline.md` §6, `DR-STAGE4-MAC-012`) ---


def build_stage4_layers(dependencies: EndpointDependencies, crypto_dependencies=None):
    """Stage 4 실제 경로 계층을 조립한다.

    Link ID 11 `Packet -> PHY -> Radio`를 실제로 사용하는 Stage 1 Link와 Stage 2 PHY를
    조립한다. Stage 0 Stub은 legacy route 회귀에서만 계속 사용한다.
    """

    endpoint = dependencies.configuration.endpoint
    presentation = StubPresentationAdapter(dependencies.registries.payload_format)
    link = LinkDataPlane(
        dependencies.id_source,
        dependencies.registries.message_codec,
        dependencies.registries.link_profile,
        crypto_dependencies=crypto_dependencies,
    )
    phy = Link11Phy(
        dependencies.registries.phy_profile,
        dependencies.time_source,
        dependencies.id_source,
        dependencies.configuration.radio.sample_format,
    )
    return presentation, link, phy


def preflight_tbb_certificate(
    *,
    readiness_utc,
    signer,
    trust_store,
    expected_fingerprint: bytes,
    is_control: bool,
):
    """ROC signer와 MASS trust store를 기대 fingerprint에 preflight한다.

    동결 Stage 1 protocol의 반환형을 바꾸는 새 Link API가 아니다. 세 endpoint는 같은 주입
    `TimeSource`에서 얻은 단일 `readiness_utc`를 사용한다.
    """

    if is_control:
        if signer is None:
            return config_error("ROC endpoint에 TbbSigner가 구성되지 않았습니다")
        certificate = signer.certificate_der()
        if certificate.is_err:
            return config_error(
                "ROC signer certificate를 읽을 수 없습니다",
                origin_code=certificate.error.code,
            )
        fingerprint = certificate_fingerprint(certificate.value)
        if fingerprint.is_err:
            return config_error(
                "ROC signer certificate fingerprint 계산이 실패했습니다",
                origin_code=fingerprint.error.code,
            )
        if fingerprint.value != expected_fingerprint:
            return config_error(
                "ROC signer certificate fingerprint가 배포 설정과 다릅니다",
                expected_fingerprint=expected_fingerprint.hex(),
                observed_fingerprint=fingerprint.value.hex(),
            )
        return Ok(fingerprint.value)

    if trust_store is None:
        return config_error("MASS endpoint에 TbbTrustStore가 구성되지 않았습니다")
    approved = trust_store.find_approved_certificates(expected_fingerprint, readiness_utc)
    if approved.is_err:
        return config_error(
            "MASS trust store 조회가 실패했습니다", origin_code=approved.error.code
        )
    if not approved.value:
        return config_error(
            "기대 fingerprint에 대응하는 승인 certificate가 없습니다",
            expected_fingerprint=expected_fingerprint.hex(),
        )
    observed_fingerprints = []
    for candidate in approved.value:
        certificate_der = getattr(candidate, "certificate_der", None)
        if not isinstance(certificate_der, bytes) or not certificate_der:
            continue
        observed = certificate_fingerprint(certificate_der)
        if observed.is_ok:
            observed_fingerprints.append(observed.value)
    if expected_fingerprint not in observed_fingerprints:
        # A trust-store lookup key is not itself proof that the returned value
        # belongs to that key.  Validate the provisioned certificate bytes during
        # readiness so a bad store cannot defer this mismatch until first traffic.
        return config_error(
            "MASS trust store certificate fingerprint가 배포 설정과 다릅니다",
            expected_fingerprint=expected_fingerprint.hex(),
            observed_fingerprints=",".join(
                item.hex() for item in observed_fingerprints
            ),
        )
    return Ok(expected_fingerprint)


def build_stage4_endpoint(
    dependencies: EndpointDependencies,
    deployment: MacDeploymentProfile,
    *,
    crypto_dependencies=None,
    readiness_utc=None,
):
    """Stage 4 endpoint 하나를 조립한다. group 검증은 `build_stage4_group`이 한다."""

    from ..runtime.endpoint import Stage4Support, build_endpoint

    endpoint_config = dependencies.configuration.endpoint
    is_control = endpoint_config.role is ServiceRole.CONTROL
    signer = None if crypto_dependencies is None else crypto_dependencies.tbb_signer
    trust_store = None if crypto_dependencies is None else crypto_dependencies.tbb_trust_store
    preflight = preflight_tbb_certificate(
        readiness_utc=readiness_utc or dependencies.time_source.now_utc(),
        signer=signer,
        trust_store=trust_store,
        expected_fingerprint=deployment.expected_tbb_certificate_fingerprint,
        is_control=is_control,
    )
    if preflight.is_err:
        return preflight

    canonical_tbb = build_canonical_tbb_profile(deployment)

    # Type 4 두 Message의 승인된 용량식을 registry 값으로 시작 시 검증한다.
    capacity = _validate_type4_capacity(dependencies.registries.link_profile)
    if capacity.is_err:
        return capacity

    endpoint_result = build_endpoint(dependencies)
    if endpoint_result.is_err:
        return endpoint_result
    endpoint = endpoint_result.value

    presentation, link, phy = build_stage4_layers(dependencies, crypto_dependencies)
    endpoint._presentation = presentation
    endpoint._link = link
    endpoint._phy = phy

    capabilities = _radio_capabilities(dependencies.radio)
    horizon = _radio_schedule_horizon(dependencies.configuration)
    profile = MacRuntimeProfile(
        canonical_tbb=canonical_tbb,
        deployment_fingerprint=build_canonical_deployment_profile(
            deployment, canonical_tbb.canonical_tbb_fingerprint
        ).deployment_fingerprint,
        local_station_id=endpoint_config.station_id,
        is_control=is_control,
        transmit_lead_nanoseconds=capabilities[0],
        receive_lead_nanoseconds=capabilities[1],
        channel_switch_gap_nanoseconds=capabilities[2],
        sample_rate_hz=endpoint_config.phy.sample_rate_hz,
        schedule_horizon_nanoseconds=horizon,
    )
    repository = MacStateRepository()
    lock = RLock()
    health = EndpointHealthLog()
    outbox = MacCommandOutbox(dependencies.id_source, lock, health)
    ingress = MacIngressQueue(lock, dependencies.time_source, dependencies.id_source)
    coordinator = MacCoordinator(
        profile=profile,
        deployment=deployment,
        time_source=dependencies.time_source,
        random_source=dependencies.random_source,
        id_source=dependencies.id_source,
        link_profile_registry=dependencies.registries.link_profile,
        phy_profile_registry=dependencies.registries.phy_profile,
        repository=repository,
    )
    mac = Stage4Mac(coordinator, repository, outbox, lock, dependencies.id_source)
    endpoint._mac = mac
    support = Stage4Support(endpoint, mac, outbox, ingress, health, lock)
    endpoint.attach_stage4(support)
    return Ok(endpoint)


def _radio_capabilities(radio):
    """Radio capability의 TX/RX 최소 lead time과 channel-switch gap을 읽는다.

    Runtime은 endpoint 시작 시 이 값을 MAC에 제공하고 MAC은 이보다 늦은 새 계획을 만들지
    않는다. adapter가 capability를 노출하지 않으면 lead time 0으로 본다.
    """

    applied = getattr(radio, "_applied", None)
    capabilities = getattr(applied, "capabilities", None)
    if capabilities is None:
        return 0, 0, 0
    return (
        capabilities.minimum_schedule_lead_ns,
        capabilities.minimum_rx_schedule_lead_ns,
        capabilities.minimum_tx_channel_switch_gap_ns,
    )


def _radio_schedule_horizon(configuration):
    """Radio가 수락하는 가장 먼 미래 시각까지의 거리.

    Stage 3 `INTERNAL_FREE_RUNNING` profile은 UTC↔hardware mapping을
    `mapping_valid_for_nanoseconds` 동안만 유효하다고 정의하고, 그 밖의 요청을
    `CLOCK_MAPPING_UNAVAILABLE`로 거부한다. MAC은 이 상한을 Radio capability로 받아
    더 먼 계획을 Radio에 내보내지 않는다. 동결 Stage 3 값은 바꾸지 않는다.

    mapping 기준시각이 요청 시점보다 조금 앞설 수 있으므로 한 hexslot을 안전여유로 뺀다.
    """

    timing = getattr(configuration.radio, "timing", None)
    if timing is None:
        return 0
    horizon = timing.mapping_valid_for_nanoseconds - SCHEDULE_HORIZON_MARGIN_NANOSECONDS
    return max(horizon, 0)


def _validate_type4_capacity(link_profile_registry):
    """Type 4 `17*2=34`, Message 영역 50, Type 81 padding 16, CRC 4, Packet 54를 검증한다."""

    found = link_profile_registry.lookup(11)
    if found.is_err:
        return config_error("Link ID 11 profile이 registry에 없습니다")
    profile = found.value
    message_region = profile.message_region_bytes
    crc_bytes = profile.crc_size_bytes
    packet_capacity = profile.packet_capacity_bytes
    two_type4 = 2 * TYPE4_MESSAGE_BYTES
    if two_type4 > message_region:
        return config_error(
            "Type 4 두 Message가 Link ID 11 Message 영역을 넘습니다",
            two_type4_bytes=two_type4,
            message_region_bytes=message_region,
        )
    padding = message_region - two_type4
    if padding != EXPECTED_TYPE4_PADDING_BYTES:
        return config_error(
            "Type 81 padding 계산이 승인된 값과 다릅니다",
            padding_bytes=padding,
            expected_padding_bytes=EXPECTED_TYPE4_PADDING_BYTES,
        )
    if message_region + crc_bytes != packet_capacity:
        return config_error(
            "CRC를 Message 영역 안의 byte로 계산할 수 없습니다",
            message_region_bytes=message_region,
            crc_bytes=crc_bytes,
            packet_capacity_bytes=packet_capacity,
        )
    if packet_capacity != EXPECTED_PACKET_BYTES:
        return config_error(
            "Link ID 11 Packet 용량이 54 bytes가 아닙니다",
            packet_capacity_bytes=packet_capacity,
        )
    return Ok(packet_capacity)


def build_stage4_group(descriptors, *, crypto_by_station=None):
    """세 endpoint descriptor를 한 번에 검증하고 group을 조립한다.

    `(station_id, ServiceRole, MacDeploymentProfile)` 세 쌍의 구성원·역할·배포 설정이 모두
    같고 `deployment_fingerprint`와 `canonical_tbb_fingerprint`가 일치할 때만 시작한다.
    """

    if not isinstance(descriptors, tuple) or len(descriptors) != 3:
        return config_error("Stage 4 group은 정확히 세 endpoint여야 합니다")
    entries = []
    for dependencies, deployment in descriptors:
        endpoint_config = dependencies.configuration.endpoint
        entries.append(
            EndpointDescriptor(
                station_id=endpoint_config.station_id,
                role=endpoint_config.role,
                profile=deployment,
            )
        )
    fingerprints = tuple(
        build_canonical_tbb_profile(deployment).canonical_tbb_fingerprint
        for _dependencies, deployment in descriptors
    )
    validated = validate_endpoint_group(tuple(entries), fingerprints)
    if validated.is_err:
        return validated

    # 세 endpoint는 같은 주입 TimeSource에서 얻은 단일 readiness_utc를 사용한다.
    readiness_utc = descriptors[0][0].time_source.now_utc()
    endpoints = []
    crypto_by_station = crypto_by_station or {}
    for dependencies, deployment in descriptors:
        station_id = dependencies.configuration.endpoint.station_id
        built = build_stage4_endpoint(
            dependencies,
            deployment,
            crypto_dependencies=crypto_by_station.get(station_id.value),
            readiness_utc=readiness_utc,
        )
        if built.is_err:
            return built
        endpoints.append(built.value)
    return Ok(tuple(endpoints))
