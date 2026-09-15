"""Stage 4 MAC 시험 fixture (`stage4_mac_baseline.md` §7).

고정 `TimeSource`, seed와 ID로 3-endpoint Software Radio scenario를 재현 가능하게 만든다.
배치 값(station ID·TBB control station·service area)과 profile 조립기는 production module
`vdeter.stage7.deployment`의 것을 재사용하고, 여기서는 결정적 시험 기본값(고정 TBB 유효창·시험
fingerprint·Type 0 override·signer·trust store)만 주입한다. 인증서는 시험용 값이며 규범 값이 아니다.
"""

from __future__ import annotations

import datetime
import unittest
from dataclasses import replace
from pathlib import Path

from vdeter.common.contracts.service import ServiceRole
from vdeter.common.identifiers import StationId
from vdeter.common.results import Ok
from vdeter.mac.deployment import (
    MacDeploymentProfile,
    Stage4TbbFixtureProfile,
    Type0OverrideProfile,
)
from vdeter.mac.tbb import TBB_START_TIME_EPOCH_SECONDS
from vdeter.stage7.deployment import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    MEMBER_STATION_IDS,
    ROC_STATION_ID,
    SERVICE_AREA,
    TBB_CONTROL_STATION_ID,
    build_deployment_profile,
    build_tbb_fixture,
)

try:  # optional cryptography extra
    from cryptography import x509  # noqa: F401

    HAS_CRYPTO = True
except Exception:  # pragma: no cover - optional dependency
    HAS_CRYPTO = False

#: 구성원 station ID(MASS_A/ROC/MASS_B, MEMBER_STATION_IDS)·TBB_CONTROL_STATION_ID·SERVICE_AREA는
#: production 배치 module `vdeter.stage7.deployment`에서 import한 같은 객체다(위). 시험 전용 값은
#: 아래 고정 유효창·시험 fingerprint·Type 0 override·signer·trust store다.

#: 2000-01-01 이후 초. 아래 `RUN_START_UTC_NANOSECONDS`가 이 구간 안에 있다.
FIXTURE_START_TIME = 800_000_000
FIXTURE_VALIDITY_MINUTES = 60

#: 고정 시험 UTC. `valid_from` 직후의 분 경계다.
RUN_START_UTC_NANOSECONDS = (
    TBB_START_TIME_EPOCH_SECONDS + FIXTURE_START_TIME
) * 1_000_000_000
RUN_START_MONOTONIC_NANOSECONDS = 5_000_000_000

EXPECTED_CERTIFICATE_FINGERPRINT = bytes.fromhex("a1b2c3d4e5f6")


def tbb_fixture() -> Stage4TbbFixtureProfile:
    """결정적 시험 기본값(고정 start_time 800,000,000 s·60분)을 주입한 production TBB fixture."""
    return build_tbb_fixture(
        start_time=FIXTURE_START_TIME,
        validity=FIXTURE_VALIDITY_MINUTES,
        service_area=SERVICE_AREA,
    )


def deployment_profile(
    *,
    expected_fingerprint: bytes = EXPECTED_CERTIFICATE_FINGERPRINT,
    type0_override: Type0OverrideProfile | None = None,
    member_station_ids=MEMBER_STATION_IDS,
    roc_station_id: StationId = ROC_STATION_ID,
    tbb_control_station_id: int = TBB_CONTROL_STATION_ID,
    fixture: Stage4TbbFixtureProfile | None = None,
) -> MacDeploymentProfile:
    """시험 기본값(시험 fingerprint·`tbb_fixture()`)을 주입한 production 배치 profile."""
    return build_deployment_profile(
        expected_fingerprint=expected_fingerprint,
        tbb_fixture=fixture or tbb_fixture(),
        type0_override=type0_override,
        member_station_ids=member_station_ids,
        roc_station_id=roc_station_id,
        tbb_control_station_id=tbb_control_station_id,
    )


def default_type0_override() -> Type0OverrideProfile:
    return Type0OverrideProfile(selection_interval=235, short_data_limit=4, system_status=0)


class _StaticTrustStore:
    """기대 fingerprint로 승인 certificate를 돌려주는 시험 trust store."""

    __slots__ = ("_certificates", "calls")

    def __init__(self, certificates) -> None:
        self._certificates = tuple(certificates)
        self.calls = []

    def find_approved_certificates(self, fingerprint, received_at):
        self.calls.append((fingerprint, received_at))
        return Ok(self._certificates)


class _EmptyTrustStore:
    __slots__ = ()

    def find_approved_certificates(self, fingerprint, received_at):
        return Ok(())


def build_test_signer():
    """P-256 시험 signer와 certificate DER, 그 fingerprint를 만든다."""

    if not HAS_CRYPTO:
        raise unittest.SkipTest("optional cryptography extra is not installed")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from vdeter.link.tbb.crypto import certificate_fingerprint, create_cryptography_tbb_signer

    key = ec.derive_private_key(7, ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VDETER STAGE4 TEST")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(4)
        .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2040, 1, 1, tzinfo=datetime.timezone.utc))
        .sign(key, hashes.SHA256(), ecdsa_deterministic=True)
    )
    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    private_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = create_cryptography_tbb_signer(private_der, certificate_der).value
    fingerprint = certificate_fingerprint(certificate_der).value
    return signer, certificate_der, fingerprint


def crypto_dependencies(signer, certificate_der, *, is_control: bool):
    from vdeter.link.tbb.crypto import LinkCryptoDependencies, TrustedTbbCertificate

    trust = _StaticTrustStore((TrustedTbbCertificate("stage4", certificate_der),))
    return LinkCryptoDependencies(
        tbb_signer=signer if is_control else None, tbb_trust_store=trust
    )


ROLE_BY_STATION = {
    MASS_A_STATION_ID.value: ServiceRole.SHIP,
    ROC_STATION_ID.value: ServiceRole.CONTROL,
    MASS_B_STATION_ID.value: ServiceRole.SHIP,
}


def _stage4_configuration(station_id: StationId, role: ServiceRole, endpoint_id: str):
    """동결 Stage 3 Software Radio 설정에서 Stage 4 endpoint 설정을 만든다."""

    from vdeter.config.loader import load_configuration

    root = Path(__file__).resolve().parents[2]
    name = (
        "stage3_software_control.toml"
        if role is ServiceRole.CONTROL
        else "stage3_software_ship.toml"
    )
    text = (root / "configs" / name).read_text(encoding="utf-8")
    original_endpoint = (
        "software-control" if role is ServiceRole.CONTROL else "software-ship"
    )
    original_station = 222222222 if role is ServiceRole.CONTROL else 111111111
    text = text.replace(f'id = "{original_endpoint}"', f'id = "{endpoint_id}"')
    text = text.replace(
        f"station_id = {original_station}", f"station_id = {station_id.value}"
    )
    loaded = load_configuration(text, "<stage4-test>")
    if loaded.is_err:
        raise AssertionError(loaded.error)
    return loaded.value


def build_stage4_software_group(
    *,
    with_type0: bool = True,
    type0_override: Type0OverrideProfile | None = None,
    with_test_payload_format: bool = False,
    random_source_factory=None,
    fixture: Stage4TbbFixtureProfile | None = None,
):
    """결정적인 세 endpoint Software Radio group을 정식 시험에서 재사용한다."""

    from vdeter.common.events import RecordingInternalEventSink, RecordingServiceEventSink
    from vdeter.common.identifiers import DeterministicIdSource
    from vdeter.common.random_source import SeededRandomSource
    from vdeter.common.time import TestTimeSource
    from vdeter.profiles.models import RegistryScope
    from vdeter.radio.software import (
        SoftwareRadioAdapter,
        SoftwareRadioLink,
        SoftwareRadioMedium,
    )
    from vdeter.runtime.wiring import (
        EndpointDependencies,
        InternalEventBus,
        build_registries,
        build_stage4_group,
    )
    from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL

    class _RegistryConfiguration:
        class endpoint:
            scope = RegistryScope.OPERATIONAL
            code_version = "stage4-test"

    registries = build_registries(_RegistryConfiguration())
    if registries.is_err:
        raise AssertionError(registries.error)
    selected_registries = registries.value
    if with_test_payload_format:
        from vdeter.presentation.registry import build_payload_format_registry

        payload_registry = build_payload_format_registry(
            RegistryScope.TEST, "stage4-test-payload"
        )
        if payload_registry.is_err:
            raise AssertionError(payload_registry.error)
        frozen = payload_registry.value.freeze()
        if frozen.is_err:
            raise AssertionError(frozen.error)
        selected_registries = replace(
            selected_registries, payload_format=payload_registry.value
        )
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="stage4-utc",
        monotonic_clock_id="stage4-monotonic",
    )
    station_endpoint = {
        MASS_A_STATION_ID.value: "stage4-mass-a",
        ROC_STATION_ID.value: "stage4-roc",
        MASS_B_STATION_ID.value: "stage4-mass-b",
    }
    links = []
    for target in ("stage4-mass-a", "stage4-mass-b"):
        links.append(SoftwareRadioLink("stage4-roc", target, UPPER_CHANNEL, 0))
    for source in ("stage4-mass-a", "stage4-mass-b"):
        links.append(SoftwareRadioLink(source, "stage4-roc", LOWER_CHANNEL, 0))
    links.extend(
        (
            SoftwareRadioLink("stage4-mass-a", "stage4-mass-b", UPPER_CHANNEL, 0),
            SoftwareRadioLink("stage4-mass-b", "stage4-mass-a", UPPER_CHANNEL, 0),
        )
    )
    medium = SoftwareRadioMedium(tuple(links))
    signer, certificate_der, fingerprint = build_test_signer()
    descriptors = []
    crypto_by_station = {}
    for index, station_id in enumerate(MEMBER_STATION_IDS):
        role = ROLE_BY_STATION[station_id.value]
        endpoint_id = station_endpoint[station_id.value]
        configuration = _stage4_configuration(station_id, role, endpoint_id)
        internal = InternalEventBus(RecordingInternalEventSink())
        dependencies = EndpointDependencies(
            configuration=configuration,
            time_source=clock,
            random_source=(
                random_source_factory(index)
                if random_source_factory is not None
                else SeededRandomSource(500 + index)
            ),
            id_source=DeterministicIdSource(2000 + index),
            radio=SoftwareRadioAdapter(
                clock,
                DeterministicIdSource(1000 + index),
                internal,
                endpoint_id=endpoint_id,
                medium=medium,
            ),
            registries=selected_registries,
            internal_event_sink=internal,
            service_event_sink=RecordingServiceEventSink(),
        )
        selected_override = type0_override
        if selected_override is None and with_type0:
            selected_override = default_type0_override()
        deployment = deployment_profile(
            expected_fingerprint=fingerprint,
            type0_override=selected_override,
            fixture=fixture,
        )
        descriptors.append((dependencies, deployment))
        crypto_by_station[station_id.value] = crypto_dependencies(
            signer, certificate_der, is_control=role is ServiceRole.CONTROL
        )
    built = build_stage4_group(
        tuple(descriptors), crypto_by_station=crypto_by_station
    )
    if built.is_err:
        raise AssertionError(built.error)
    return clock, built.value


def drive_stage4_group(clock, endpoints, steps: int, step_nanoseconds: int = 160_000_000):
    """공유 clock과 세 endpoint dispatcher를 결정적 순서로 진행한다."""

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


def start_stage4_group(clock, endpoints, acquisition_steps: int = 80):
    """세 endpoint를 RUNNING으로 올리고 canonical TBB/Type 0 획득까지 진행한다."""

    from vdeter.common.contracts.service import ServiceStartRequest

    for endpoint in endpoints:
        configuration = endpoint.dependencies.configuration.endpoint
        started = endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )
        if started.is_err:
            raise AssertionError(started.error)
        ready = endpoint.stage4.mac.begin_running()
        if ready.is_err:
            raise AssertionError(ready.error)
        endpoint.stage4.pump()
    drive_stage4_group(clock, endpoints, acquisition_steps)
    if any(endpoint.stage4.mac.state.active_tbb is None for endpoint in endpoints):
        raise AssertionError("canonical TBB acquisition did not complete")
