"""Stage 5 Service 통합 시험 fixture (`stage5_service_integration_baseline.md` §11).

고정 `TimeSource`, seed와 ID로 Ship A, Ship B, ROC 세 endpoint Software Radio group을
재현 가능하게 만든다. station ID, 좌표와 인증서는 시험용 값이며 규범 값이 아니다.
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
from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL
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
    build_stage5_group,
)
from vdeter.security.dependencies import E2eSecurityDependencies
from tests.support.security import make_test_e2e_by_station

VDETER_ROOT = Path(__file__).resolve().parents[2]

STATION_ENDPOINT_IDS = {
    MASS_A_STATION_ID.value: "stage5-ship-a",
    ROC_STATION_ID.value: "stage5-roc",
    MASS_B_STATION_ID.value: "stage5-ship-b",
}


class _RegistryConfiguration:
    class endpoint:
        scope = RegistryScope.OPERATIONAL
        code_version = "stage5-test"


def stage5_configuration(station_id: StationId, role: ServiceRole, endpoint_id: str):
    """동결 Stage 5 Software config에서 endpoint 설정 snapshot을 만든다."""

    name = (
        "stage5_software_control.toml"
        if role is ServiceRole.CONTROL
        else "stage5_software_ship.toml"
    )
    text = (VDETER_ROOT / "configs" / name).read_text(encoding="utf-8")
    original_endpoint = (
        "stage5-software-control" if role is ServiceRole.CONTROL else "stage5-software-ship"
    )
    original_station = 222222222 if role is ServiceRole.CONTROL else 111111111
    text = text.replace(f'id = "{original_endpoint}"', f'id = "{endpoint_id}"')
    text = text.replace(
        f"station_id = {original_station}", f"station_id = {station_id.value}"
    )
    loaded = load_configuration(text, "<stage5-test>")
    if loaded.is_err:
        raise AssertionError(loaded.error)
    return loaded.value


def build_stage5_registries(payload_scope: RegistryScope = RegistryScope.TEST):
    """Stage 5 registry 묶음. payload registry scope만 선택 가능하다."""

    registries = build_registries(_RegistryConfiguration())
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


def build_stage5_medium() -> SoftwareRadioMedium:
    links = []
    for target in ("stage5-ship-a", "stage5-ship-b"):
        links.append(SoftwareRadioLink("stage5-roc", target, UPPER_CHANNEL, 0))
    for source in ("stage5-ship-a", "stage5-ship-b"):
        links.append(SoftwareRadioLink(source, "stage5-roc", LOWER_CHANNEL, 0))
    links.extend(
        (
            SoftwareRadioLink("stage5-ship-a", "stage5-ship-b", UPPER_CHANNEL, 0),
            SoftwareRadioLink("stage5-ship-b", "stage5-ship-a", UPPER_CHANNEL, 0),
        )
    )
    return SoftwareRadioMedium(tuple(links))


def build_stage5_software_group(
    *,
    payload_scope: RegistryScope = RegistryScope.TEST,
    with_type0: bool = True,
    type0_override: Type0OverrideProfile | None = None,
    random_source_factory=None,
    radio_faults_by_endpoint=None,
    service_sink_factory=None,
    policy=None,
    e2e_security_mode="DISABLED",
):
    """결정적인 Ship A, Ship B, ROC Stage 5 group을 만든다. 아직 시작하지 않은 상태다."""

    registries = build_stage5_registries(payload_scope)
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="stage5-utc",
        monotonic_clock_id="stage5-monotonic",
    )
    medium = build_stage5_medium()
    signer, certificate_der, fingerprint = build_test_signer()
    descriptors = []
    crypto_by_station = {}
    radio_faults_by_endpoint = radio_faults_by_endpoint or {}
    for index, station_id in enumerate(MEMBER_STATION_IDS):
        role = ROLE_BY_STATION[station_id.value]
        endpoint_id = STATION_ENDPOINT_IDS[station_id.value]
        configuration = stage5_configuration(station_id, role, endpoint_id)
        internal = InternalEventBus(RecordingInternalEventSink())
        service_sink = (
            service_sink_factory(endpoint_id)
            if service_sink_factory is not None
            else RecordingServiceEventSink()
        )
        dependencies = EndpointDependencies(
            configuration=configuration,
            time_source=clock,
            random_source=(
                random_source_factory(index)
                if random_source_factory is not None
                else SeededRandomSource(700 + index)
            ),
            id_source=DeterministicIdSource(4000 + index),
            radio=SoftwareRadioAdapter(
                clock,
                DeterministicIdSource(3000 + index),
                internal,
                endpoint_id=endpoint_id,
                medium=medium,
                faults=tuple(radio_faults_by_endpoint.get(endpoint_id, ())),
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
    if e2e_security_mode == "ENFORCED":
        e2e_by_station = make_test_e2e_by_station(STATION_ENDPOINT_IDS)
    elif e2e_security_mode == "DISABLED":
        e2e_by_station = {station.value: E2eSecurityDependencies.disabled()
                          for station in MEMBER_STATION_IDS}
    else:
        raise ValueError("Explicit E2E security mode is required")
    built = build_stage5_group(
        tuple(descriptors), crypto_by_station=crypto_by_station, policy=policy,
        e2e_security_by_station=e2e_by_station,
    )
    if built.is_err:
        raise AssertionError(built.error)
    return clock, built.value


def drive_stage5_group(clock, group, steps: int, step_nanoseconds: int = 160_000_000):
    """공유 clock과 세 endpoint dispatcher를 결정적 순서로 진행한다."""

    endpoints = group.endpoints
    for _ in range(steps):
        clock.advance(step_nanoseconds)
        for endpoint in endpoints:
            endpoint.radio.process_due()
        for endpoint in endpoints:
            support = endpoint.stage4
            support.advance_clock()
            while support.has_due_timer():
                support.fire_due_timers()
            support.pump()


def start_stage5_group(clock, group, acquisition_steps: int = 80):
    """승인된 순서로 group을 시작하고 canonical TBB 획득까지 진행한다."""

    started = group.start()
    if started.is_err:
        raise AssertionError(started.error)
    drive_stage5_group(clock, group, acquisition_steps)
    if any(
        endpoint.stage4.mac.state.active_tbb is None for endpoint in group.endpoints
    ):
        raise AssertionError("canonical TBB acquisition did not complete")


def service_events_of(endpoint, event_type=None):
    events = endpoint.dependencies.service_event_sink.events()
    if event_type is None:
        return events
    return tuple(event for event in events if event.event_type is event_type)


def received_payloads(endpoint):
    return [
        event.received_data.payload
        for event in endpoint.dependencies.service_event_sink.events()
        if event.received_data is not None
    ]
