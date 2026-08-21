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
from ..link.messages.builtins import build_message_registry
from ..link.models import LinkCapabilities
from ..link.stub import StubLink
from ..mac.models import MacPlanningDefaults
from ..mac.stub import StubMac
from ..phy.models import PhyCapabilities
from ..phy.stub import StubPhy
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
):
    """설정 하나로 endpoint 의존성 전체를 조립한다."""

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

    radio_result = build_radio_adapter(configuration, time_source, id_source, internal_sink)
    if radio_result.is_err:
        return radio_result

    registries_result = build_registries(configuration)
    if registries_result.is_err:
        return registries_result

    return Ok(
        EndpointDependencies(
            configuration=configuration,
            time_source=time_source,
            random_source=random_source,
            id_source=id_source,
            radio=radio_result.value,
            registries=registries_result.value,
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
