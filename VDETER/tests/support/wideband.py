"""wideband 2-PC 통합 시험 fixture (`DR-STAGE6-MR-001` §13.2).

Stage 5 fixture와 같은 station/역할/서명 값 위에서, 정확히 두 개의 100 kHz Physical
Channel alias와 combined three-entry `phy_profile` registry를 사용하는 Software Radio
group을 재현 가능하게 만든다.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    MEMBER_STATION_IDS,
    ROC_STATION_ID,
    ROLE_BY_STATION,
    RUN_START_MONOTONIC_NANOSECONDS,
    RUN_START_UTC_NANOSECONDS,
    Type0OverrideProfile,
    build_test_signer,
    crypto_dependencies,
    default_type0_override,
    deployment_profile,
)
from vdeter.common.contracts.service import ServiceRole
from vdeter.common.events import RecordingInternalEventSink, RecordingServiceEventSink
from vdeter.common.identifiers import DeterministicIdSource, StationId
from vdeter.common.random_source import SeededRandomSource
from vdeter.common.time import TestTimeSource
from vdeter.config.loader import load_configuration
from vdeter.mac.tbb import WIDEBAND_LOWER_CHANNEL, WIDEBAND_UPPER_CHANNEL
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope
from vdeter.profiles.payload_formats import STAGE5_PAYLOAD_CONTENT_VERSION
from vdeter.radio.software import (
    SoftwareRadioAdapter,
    SoftwareRadioLink,
    SoftwareRadioMedium,
)
from vdeter.runtime.wiring import (
    EndpointDependencies,
    InternalEventBus,
    build_registries,
    build_wideband_group,
)

VDETER_ROOT = Path(__file__).resolve().parents[2]

STATION_ENDPOINT_IDS = {
    MASS_A_STATION_ID.value: "wideband-ship-a",
    ROC_STATION_ID.value: "wideband-roc",
    MASS_B_STATION_ID.value: "wideband-ship-b",
}


class _RegistryConfiguration:
    class endpoint:
        scope = RegistryScope.OPERATIONAL
        code_version = "wideband-test"


def wideband_configuration(
    station_id: StationId, role: ServiceRole, endpoint_id: str, data_link_id: int
):
    """wideband Software config에서 endpoint 설정 snapshot을 만든다."""

    name = (
        "wideband_software_control.toml"
        if role is ServiceRole.CONTROL
        else "wideband_software_ship.toml"
    )
    text = (VDETER_ROOT / "configs" / name).read_text(encoding="utf-8")
    original_endpoint = (
        "wideband-software-control" if role is ServiceRole.CONTROL else "wideband-software-ship"
    )
    original_station = 222222222 if role is ServiceRole.CONTROL else 111111111
    text = text.replace(f'id = "{original_endpoint}"', f'id = "{endpoint_id}"')
    text = text.replace(
        f"station_id = {original_station}", f"station_id = {station_id.value}"
    )
    text = text.replace("data_link_id = 17", f"data_link_id = {data_link_id}")
    loaded = load_configuration(text, "<wideband-test>")
    if loaded.is_err:
        raise AssertionError(loaded.error)
    return loaded.value


def build_wideband_registries(payload_scope: RegistryScope = RegistryScope.TEST):
    """combined three-entry registry 묶음. payload registry scope만 선택 가능하다."""

    registries = build_registries(_RegistryConfiguration(), combined_phy=True)
    if registries.is_err:
        raise AssertionError(registries.error)
    selected = registries.value
    payload = build_payload_format_registry(payload_scope, STAGE5_PAYLOAD_CONTENT_VERSION)
    if payload.is_err:
        raise AssertionError(payload.error)
    frozen = payload.value.freeze()
    if frozen.is_err:
        raise AssertionError(frozen.error)
    return replace(selected, payload_format=payload.value)


def build_wideband_medium() -> SoftwareRadioMedium:
    links = []
    for target in ("wideband-ship-a", "wideband-ship-b"):
        links.append(SoftwareRadioLink("wideband-roc", target, WIDEBAND_UPPER_CHANNEL, 0))
    for source in ("wideband-ship-a", "wideband-ship-b"):
        links.append(SoftwareRadioLink(source, "wideband-roc", WIDEBAND_LOWER_CHANNEL, 0))
    return SoftwareRadioMedium(tuple(links))


def build_wideband_software_group(
    *,
    data_link_id: int = 17,
    payload_scope: RegistryScope = RegistryScope.TEST,
    with_type0: bool = True,
    type0_override: Type0OverrideProfile | None = None,
    service_sink_factory=None,
    policy=None,
    registries=None,
    configuration_mutator=None,
):
    """결정적인 Ship A, Ship B, ROC wideband group을 만든다. 아직 시작하지 않은 상태다."""

    registries = registries or build_wideband_registries(payload_scope)
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="wideband-utc",
        monotonic_clock_id="wideband-monotonic",
    )
    medium = build_wideband_medium()
    signer, certificate_der, fingerprint = build_test_signer()
    descriptors = []
    crypto_by_station = {}
    for index, station_id in enumerate(MEMBER_STATION_IDS):
        role = ROLE_BY_STATION[station_id.value]
        endpoint_id = STATION_ENDPOINT_IDS[station_id.value]
        configuration = wideband_configuration(station_id, role, endpoint_id, data_link_id)
        if configuration_mutator is not None:
            configuration = configuration_mutator(endpoint_id, configuration)
        internal = InternalEventBus(RecordingInternalEventSink())
        service_sink = (
            service_sink_factory(endpoint_id)
            if service_sink_factory is not None
            else RecordingServiceEventSink()
        )
        dependencies = EndpointDependencies(
            configuration=configuration,
            time_source=clock,
            random_source=SeededRandomSource(900 + index),
            id_source=DeterministicIdSource(6000 + index),
            radio=SoftwareRadioAdapter(
                clock,
                DeterministicIdSource(5000 + index),
                internal,
                endpoint_id=endpoint_id,
                medium=medium,
            ),
            registries=registries,
            internal_event_sink=internal,
            service_event_sink=service_sink,
        )
        selected_override = type0_override
        if selected_override is None and with_type0:
            selected_override = default_type0_override()
        deployment = deployment_profile(
            expected_fingerprint=fingerprint,
            type0_override=selected_override,
        )
        descriptors.append((dependencies, deployment))
        crypto_by_station[station_id.value] = crypto_dependencies(
            signer, certificate_der, is_control=role is ServiceRole.CONTROL
        )
    built = build_wideband_group(
        tuple(descriptors), crypto_by_station=crypto_by_station, policy=policy
    )
    if built.is_err:
        raise AssertionError(built.error)
    return clock, built.value


def build_wideband_descriptors(*, data_link_id: int = 17, with_type0: bool = True):
    """group 조립 없이 descriptor tuple만 만든다 (negative case 검증용)."""

    registries = build_wideband_registries(RegistryScope.TEST)
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="wideband-utc",
        monotonic_clock_id="wideband-monotonic",
    )
    medium = build_wideband_medium()
    signer, certificate_der, fingerprint = build_test_signer()
    descriptors = []
    crypto_by_station = {}
    for index, station_id in enumerate(MEMBER_STATION_IDS):
        role = ROLE_BY_STATION[station_id.value]
        endpoint_id = STATION_ENDPOINT_IDS[station_id.value]
        configuration = wideband_configuration(station_id, role, endpoint_id, data_link_id)
        internal = InternalEventBus(RecordingInternalEventSink())
        dependencies = EndpointDependencies(
            configuration=configuration,
            time_source=clock,
            random_source=SeededRandomSource(900 + index),
            id_source=DeterministicIdSource(6000 + index),
            radio=SoftwareRadioAdapter(
                clock,
                DeterministicIdSource(5000 + index),
                internal,
                endpoint_id=endpoint_id,
                medium=medium,
            ),
            registries=registries,
            internal_event_sink=internal,
            service_event_sink=RecordingServiceEventSink(),
        )
        override = default_type0_override() if with_type0 else None
        deployment = deployment_profile(
            expected_fingerprint=fingerprint, type0_override=override
        )
        descriptors.append((dependencies, deployment))
        crypto_by_station[station_id.value] = crypto_dependencies(
            signer, certificate_der, is_control=role is ServiceRole.CONTROL
        )
    return clock, tuple(descriptors), crypto_by_station
