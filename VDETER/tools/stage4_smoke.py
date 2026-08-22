"""Stage 4 3-endpoint smoke: build the group and run the TBB broadcast/acquisition."""

import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    MEMBER_STATION_IDS,
    ROC_STATION_ID,
    ROLE_BY_STATION,
    RUN_START_MONOTONIC_NANOSECONDS,
    RUN_START_UTC_NANOSECONDS,
    build_test_signer,
    crypto_dependencies,
    default_type0_override,
    deployment_profile,
)
from vdeter.common.contracts.service import ServiceRole, ServiceStartRequest
from vdeter.common.events import RecordingInternalEventSink, RecordingServiceEventSink
from vdeter.common.identifiers import DeterministicIdSource
from vdeter.common.random_source import SeededRandomSource
from vdeter.common.time import TestTimeSource
from vdeter.config.loader import load_configuration
from vdeter.profiles.models import RegistryScope
from vdeter.radio.software import SoftwareRadioAdapter, SoftwareRadioLink, SoftwareRadioMedium
from vdeter.runtime.wiring import (
    EndpointDependencies,
    InternalEventBus,
    build_registries,
    build_stage4_group,
)
from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL

SHIP_TEXT = open("configs/stage3_software_ship.toml", encoding="utf-8").read()
CONTROL_TEXT = open("configs/stage3_software_control.toml", encoding="utf-8").read()


def make_configuration(station_id, role, endpoint_id):
    text = CONTROL_TEXT if role is ServiceRole.CONTROL else SHIP_TEXT
    text = text.replace(
        'id = "software-control"' if role is ServiceRole.CONTROL else 'id = "software-ship"',
        f'id = "{endpoint_id}"',
    )
    old_station = "222222222" if role is ServiceRole.CONTROL else "111111111"
    text = text.replace(f"station_id = {old_station}", f"station_id = {station_id.value}")
    loaded = load_configuration(text, "<stage4-smoke>")
    assert loaded.is_ok, loaded.error
    return loaded.value


def build_group():
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="stage4-utc",
        monotonic_clock_id="stage4-monotonic",
    )
    registries = build_registries_operational()
    medium = SoftwareRadioMedium(
        tuple(
            SoftwareRadioLink(source, target, channel, 0)
            for source, target, channel in _links()
        )
    )
    signer, certificate_der, fingerprint = build_test_signer()
    descriptors = []
    crypto_by_station = {}
    sinks = {}
    for index, station_id in enumerate(MEMBER_STATION_IDS):
        role = ROLE_BY_STATION[station_id.value]
        endpoint_id = _endpoint_id(station_id)
        configuration = make_configuration(station_id, role, endpoint_id)
        internal = InternalEventBus(RecordingInternalEventSink())
        service = RecordingServiceEventSink()
        radio = SoftwareRadioAdapter(
            clock,
            DeterministicIdSource(1000 + index),
            internal,
            endpoint_id=endpoint_id,
            medium=medium,
        )
        dependencies = EndpointDependencies(
            configuration=configuration,
            time_source=clock,
            random_source=SeededRandomSource(500 + index),
            id_source=DeterministicIdSource(2000 + index),
            radio=radio,
            registries=registries,
            internal_event_sink=internal,
            service_event_sink=service,
        )
        deployment = deployment_profile(
            expected_fingerprint=fingerprint, type0_override=default_type0_override()
        )
        descriptors.append((dependencies, deployment))
        crypto_by_station[station_id.value] = crypto_dependencies(
            signer, certificate_der, is_control=role is ServiceRole.CONTROL
        )
        sinks[station_id.value] = (internal, service)
    group = build_stage4_group(tuple(descriptors), crypto_by_station=crypto_by_station)
    assert group.is_ok, group.error
    return clock, medium, group.value, sinks


def build_registries_operational():
    class _Configuration:
        class endpoint:
            scope = RegistryScope.OPERATIONAL
            code_version = "stage4"

    built = build_registries(_Configuration())
    assert built.is_ok, built.error
    return built.value


def _endpoint_id(station_id):
    if station_id.value == ROC_STATION_ID.value:
        return "stage4-roc"
    if station_id.value == MASS_A_STATION_ID.value:
        return "stage4-mass-a"
    return "stage4-mass-b"


def _links():
    roc = "stage4-roc"
    mass_a = "stage4-mass-a"
    mass_b = "stage4-mass-b"
    result = []
    # ROC transmits on the upper leg; both MASS receive it.
    for target in (mass_a, mass_b):
        result.append((roc, target, UPPER_CHANNEL))
    # MASS transmit on the lower leg to ROC and on the upper leg to each other.
    for source in (mass_a, mass_b):
        result.append((source, roc, LOWER_CHANNEL))
    result.append((mass_a, mass_b, UPPER_CHANNEL))
    result.append((mass_b, mass_a, UPPER_CHANNEL))
    return tuple(result)


def main():
    clock, medium, endpoints, sinks = build_group()
    print("group built:", [e.dependencies.configuration.endpoint.endpoint_id for e in endpoints])
    for endpoint in endpoints:
        config = endpoint.dependencies.configuration.endpoint
        started = endpoint.start(
            ServiceStartRequest(local_station_id=config.station_id, role=config.role)
        )
        print("start", config.endpoint_id, started.is_ok, getattr(started, "error", None))
        if started.is_ok:
            ready = endpoint.stage4.mac.begin_running()
            print("  begin_running", ready.is_ok, getattr(ready, "error", None))
            if ready.is_ok:
                endpoint.stage4.pump()
                state = endpoint.stage4.mac.state
                print(
                    "  planned windows",
                    len(state.planned_rx_windows),
                    "pending",
                    len(state.pending_commands),
                    "tbb fragments",
                    state.tbb_broadcast.fragment_count,
                )


if __name__ == "__main__":
    main()


def drive(clock, endpoints, steps, step_ns=160_000_000):
    """Advance the shared clock and pump every endpoint deterministically."""
    for _ in range(steps):
        clock.advance(step_ns)
        for endpoint in endpoints:
            endpoint.radio.process_due()
        for endpoint in endpoints:
            support = endpoint.stage4
            support.advance_clock()
            while support.has_due_timer():
                support.fire_due_timers()
            support.pump()


def run_acquisition():
    clock, medium, endpoints, sinks = build_group()
    from vdeter.common.contracts.service import ServiceStartRequest
    for endpoint in endpoints:
        config = endpoint.dependencies.configuration.endpoint
        endpoint.start(ServiceStartRequest(local_station_id=config.station_id, role=config.role))
        endpoint.stage4.mac.begin_running()
        endpoint.stage4.pump()
    drive(clock, endpoints, 120)
    for endpoint in endpoints:
        config = endpoint.dependencies.configuration.endpoint
        state = endpoint.stage4.mac.state
        active = state.active_tbb
        print(
            config.endpoint_id,
            "active_tbb=", None if active is None else active.fingerprint[:12],
            "windows=", len(state.planned_rx_windows),
            "pending=", len(state.pending_commands),
            "candidate=", state.tbb_candidate is not None,
            "type0_lease=", state.type0_lease is not None,
        )
