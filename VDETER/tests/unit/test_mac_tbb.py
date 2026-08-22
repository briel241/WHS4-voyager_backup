"""TC-MAC-003/004: Stage 4 TBB 방송·적용 계약."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from tests.support.stage4 import (
    FIXTURE_START_TIME,
    ROC_STATION_ID,
    build_test_signer,
    build_stage4_software_group,
    deployment_profile,
    drive_stage4_group,
    tbb_fixture,
)
from vdeter.common.contracts.service import ServiceStartRequest
from vdeter.common.contracts.service import TransferState
from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.mac.tdma import frame_ordinal_at_nanoseconds, next_frame_start_nanoseconds
from vdeter.common.contracts.link import (
    TbbAssemblyControlKind,
    TbbAssemblyControlResult,
)
from vdeter.common.contracts.message import VerifiedTbb
from vdeter.common.contracts.mac_execution import MacReasonCode, TimerPurpose
from vdeter.common.errors import ErrorCategory, ErrorCode, Layer
from vdeter.common.identifiers import DeterministicIdSource, LinkId, StationId
from vdeter.common.results import Ok, internal_error
from vdeter.common.contracts.message import (
    LogicalChannelFunction,
    PhysicalChannelBandwidth,
    TbbAssemblyStatus,
    TbbAssemblyUpdate,
    TbbCandidateKey,
)
from vdeter.link.tbb.crypto import TrustedTbbCertificate
from vdeter.mac.deployment import build_canonical_deployment_profile
from vdeter.mac.models import (
    LcAllocation,
    LcState,
    RxSessionContext,
    Stage4Limits,
    TimerRecord,
    TransferContext,
    TransferPhase,
    TransferRole,
)
from vdeter.mac.tbb import build_canonical_tbb_profile
from vdeter.runtime.wiring import preflight_tbb_certificate


def _verified_tbb(canonical, clock, ids, **changes):
    values = dict(
        source_id=canonical.source_id,
        control_station_id=canonical.control_station_id,
        service_capability=canonical.service_capability,
        bulletin_board_version=canonical.bulletin_board_version,
        bulletin_board=canonical.bulletin_board,
        certificate_fingerprint=canonical.expected_certificate_fingerprint,
        authentication_sequence=bytes(64),
        fragment_count=1,
        packet_ids=(ids.new_packet_id(),),
        received_at=clock.now_utc(),
    )
    values.update(changes)
    return VerifiedTbb(**values)


def _reason(error_result):
    return error_result.error.details.get("reason_code")


class Stage4TbbTest(unittest.TestCase):
    """TC-MAC-003: ROC 방송 시작과 canonical self-activation을 검증한다."""

    def test_TC_MAC_003_roc_self_applies_the_same_canonical_tbb_at_next_frame_boundary(self):
        clock, endpoints = build_stage4_software_group(with_type0=True)
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        now = clock.now_utc().total_nanoseconds()

        for endpoint in endpoints:
            configuration = endpoint.dependencies.configuration.endpoint
            started = endpoint.start(
                ServiceStartRequest(
                    local_station_id=configuration.station_id, role=configuration.role
                )
            )
            self.assertTrue(started.is_ok, getattr(started, "error", None))
            ready = endpoint.stage4.mac.begin_running()
            self.assertTrue(ready.is_ok, getattr(ready, "error", None))
            endpoint.stage4.pump()

        active = roc.stage4.mac.state.active_tbb
        self.assertIsNotNone(active)
        self.assertEqual(
            active.fingerprint,
            roc.stage4.mac.coordinator.profile.canonical_tbb.canonical_tbb_fingerprint,
        )
        self.assertEqual(active.activation_nanoseconds, next_frame_start_nanoseconds(now))
        self.assertFalse(active.is_active_at(now))

        drive_stage4_group(clock, endpoints, 20)
        self.assertTrue(
            roc.stage4.mac.state.active_tbb.is_active_at(
                clock.now_utc().total_nanoseconds()
            )
        )
        self.assertTrue(roc.stage4.mac.state.type0_lease is not None)

    def test_TC_MAC_003_preflight_and_canonical_golden_vectors_are_closed(self):
        signer, certificate_der, fingerprint = build_test_signer()
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        readiness = endpoints[0].dependencies.time_source.now_utc()

        class Trust:
            def __init__(self, result):
                self.result = result
                self.calls = []

            def find_approved_certificates(self, requested, received_at):
                self.calls.append((requested, received_at))
                return self.result

        missing_signer = preflight_tbb_certificate(
            readiness_utc=readiness,
            signer=None,
            trust_store=None,
            expected_fingerprint=fingerprint,
            is_control=True,
        )
        self.assertTrue(missing_signer.is_err)
        wrong_signer = preflight_tbb_certificate(
            readiness_utc=readiness,
            signer=signer,
            trust_store=None,
            expected_fingerprint=bytes(6),
            is_control=True,
        )
        self.assertTrue(wrong_signer.is_err)
        self.assertEqual(fingerprint.hex(), wrong_signer.error.details.get("observed_fingerprint"))

        missing_store = preflight_tbb_certificate(
            readiness_utc=readiness,
            signer=None,
            trust_store=None,
            expected_fingerprint=fingerprint,
            is_control=False,
        )
        self.assertTrue(missing_store.is_err)
        origin_code = ErrorCode.LINK_TBB_CERTIFICATE_NOT_FOUND
        failing_store = Trust(
            internal_error(
                Layer.LINK,
                origin_code,
                ErrorCategory.VALIDATION,
                "injected trust lookup failure",
            )
        )
        lookup_failure = preflight_tbb_certificate(
            readiness_utc=readiness,
            signer=None,
            trust_store=failing_store,
            expected_fingerprint=fingerprint,
            is_control=False,
        )
        self.assertTrue(lookup_failure.is_err)
        self.assertEqual(origin_code, lookup_failure.error.details.get("origin_code"))

        for certificates in ((), (TrustedTbbCertificate("wrong", certificate_der),)):
            with self.subTest(candidate_count=len(certificates)):
                expected = fingerprint if not certificates else bytes(6)
                trust = Trust(Ok(certificates))
                rejected = preflight_tbb_certificate(
                    readiness_utc=readiness,
                    signer=None,
                    trust_store=trust,
                    expected_fingerprint=expected,
                    is_control=False,
                )
                self.assertTrue(rejected.is_err)
                self.assertEqual((expected, readiness), trust.calls[0])

        trust = Trust(Ok((TrustedTbbCertificate("stage4", certificate_der),)))
        accepted = preflight_tbb_certificate(
            readiness_utc=readiness,
            signer=None,
            trust_store=trust,
            expected_fingerprint=fingerprint,
            is_control=False,
        )
        self.assertEqual(fingerprint, accepted.value)

        deployment = deployment_profile(expected_fingerprint=fingerprint)
        canonical = build_canonical_tbb_profile(deployment)
        self.assertEqual(
            "520ac22305661443e50b4800867ac69c724aab5d0ad18be9f900b26216c1fa25",
            canonical.canonical_tbb_fingerprint,
        )
        self.assertEqual(
            canonical.canonical_tbb_fingerprint,
            __import__("hashlib").sha256(canonical.canonical_bytes).hexdigest(),
        )
        canonical_deployment = build_canonical_deployment_profile(
            deployment, canonical.canonical_tbb_fingerprint
        )
        self.assertEqual(
            "20a7536d2992933ced08637a559dd4bf5b9e8145d80a68f099cf1e9e6f1b1c79",
            canonical_deployment.deployment_fingerprint,
        )

    def test_TC_MAC_003_broadcast_sequence_validity_boundaries_and_fingerprint_guard(self):
        future_fixture = replace(tbb_fixture(), start_time=FIXTURE_START_TIME + 1)
        clock, endpoints = build_stage4_software_group(
            with_type0=False, fixture=future_fixture
        )
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        configuration = roc.dependencies.configuration.endpoint
        self.assertTrue(
            roc.start(
                ServiceStartRequest(
                    local_station_id=configuration.station_id,
                    role=configuration.role,
                )
            ).is_ok
        )
        self.assertTrue(roc.stage4.mac.begin_running().is_ok)
        roc.stage4.pump()
        coordinator = roc.stage4.mac.coordinator
        canonical = coordinator.profile.canonical_tbb
        broadcast = coordinator.state.tbb_broadcast
        self.assertLess(clock.now_utc().total_nanoseconds(), canonical.valid_from_nanoseconds())
        message_types = tuple(packet.message_types[0] for packet in broadcast.encoded_packets)
        self.assertEqual(20, message_types[0])
        self.assertEqual(22, message_types[-1])
        self.assertTrue(all(item == 21 for item in message_types[1:-1]))

        # Isolate protocol boundary behavior from the Stage 3 one-second Radio
        # scheduling capability used by the integrated fixture.
        coordinator._profile = replace(
            coordinator.profile, schedule_horizon_nanoseconds=7_200_000_000
        )

        idle = broadcast.__class__(
            next_fragment_index=broadcast.next_fragment_index,
            fragment_count=broadcast.fragment_count,
            pending_command_id=None,
            encoded_packets=broadcast.encoded_packets,
            certificate_fingerprint=broadcast.certificate_fingerprint,
        )
        commands = []
        before_valid = coordinator._next_bbsc_transmit(
            coordinator.state.evolve(tbb_broadcast=idle), "before-valid", commands
        )
        self.assertEqual(1, len(commands))
        self.assertIsNotNone(before_valid.tbb_broadcast.pending_command_id)

        guarded_commands = []
        guarded = coordinator._next_bbsc_transmit(
            coordinator.state.evolve(
                tbb_broadcast=replace(idle, certificate_fingerprint=bytes(6))
            ),
            "fingerprint-guard",
            guarded_commands,
        )
        self.assertEqual([], guarded_commands)
        self.assertIsNone(guarded.tbb_broadcast.pending_command_id)

        clock.advance_utc(
            canonical.valid_until_nanoseconds()
            - clock.now_utc().total_nanoseconds()
        )
        expired_commands = []
        expired = coordinator._next_bbsc_transmit(
            coordinator.state.evolve(tbb_broadcast=idle),
            "valid-until",
            expired_commands,
        )
        self.assertEqual([], expired_commands)
        self.assertIsNone(expired.tbb_broadcast.pending_command_id)

    def test_TC_MAC_004_duplicate_is_idempotent_and_station_mismatch_is_rejected(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        canonical = coordinator.profile.canonical_tbb
        ids = DeterministicIdSource(404)
        verified = VerifiedTbb(
            source_id=canonical.source_id,
            control_station_id=canonical.control_station_id,
            service_capability=canonical.service_capability,
            bulletin_board_version=canonical.bulletin_board_version,
            bulletin_board=canonical.bulletin_board,
            certificate_fingerprint=canonical.expected_certificate_fingerprint,
            authentication_sequence=bytes(64),
            fragment_count=1,
            packet_ids=(ids.new_packet_id(),),
            received_at=clock.now_utc(),
        )
        first, error = coordinator._apply_verified_tbb(
            coordinator.state, verified, "cause", []
        )
        self.assertIsNone(error)
        duplicate, error = coordinator._apply_verified_tbb(first, verified, "cause", [])
        self.assertIsNone(error)
        self.assertIs(first, duplicate)
        self.assertFalse(first.active_tbb.is_active_at(first.active_tbb.valid_until_nanoseconds))

        wrong_station = VerifiedTbb(
            source_id=StationId(canonical.source_id.value + 1),
            control_station_id=canonical.control_station_id,
            service_capability=canonical.service_capability,
            bulletin_board_version=canonical.bulletin_board_version,
            bulletin_board=canonical.bulletin_board,
            certificate_fingerprint=canonical.expected_certificate_fingerprint,
            authentication_sequence=bytes(64),
            fragment_count=1,
            packet_ids=(ids.new_packet_id(),),
            received_at=clock.now_utc(),
        )
        unchanged, error = coordinator._apply_verified_tbb(first, wrong_station, "cause", [])
        self.assertIs(first, unchanged)
        self.assertTrue(error.is_err)
        self.assertEqual(MacReasonCode.UNEXPECTED_CONTROL_STATION, _reason(error))

    def test_TC_MAC_003_each_received_semantic_field_kind_is_hashed_from_the_wire(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        canonical = coordinator.profile.canonical_tbb
        board = canonical.bulletin_board
        pc = board.physical_channels[0]
        group = pc.logical_channel_groups[0]
        definition = group.definitions[0]
        ids = DeterministicIdSource(430)

        def with_board(**changes):
            return replace(board, **changes)

        def with_pc(**changes):
            changed = replace(pc, **changes)
            return with_board(physical_channels=(changed,) + board.physical_channels[1:])

        def with_definition(**changes):
            changed_definition = replace(definition, **changes)
            changed_group = replace(
                group, definitions=(changed_definition,) + group.definitions[1:]
            )
            changed_pc = replace(
                pc,
                logical_channel_groups=(changed_group,) + pc.logical_channel_groups[1:],
            )
            return with_board(
                physical_channels=(changed_pc,) + board.physical_channels[1:]
            )

        area = board.service_area
        mutations = {
            "service_capability": dict(service_capability=1),
            "bulletin_board_version": dict(bulletin_board_version=2),
            "certificate_fingerprint": dict(
                certificate_fingerprint=bytes([canonical.expected_certificate_fingerprint[0] ^ 1])
                + canonical.expected_certificate_fingerprint[1:]
            ),
            "start_time": dict(bulletin_board=with_board(start_time=board.start_time + 1)),
            "validity": dict(bulletin_board=with_board(validity=board.validity + 1)),
            "tdma_frame_size": dict(bulletin_board=with_board(tdma_frame_size=5)),
            "service_area_longitude": dict(
                bulletin_board=with_board(
                    service_area=replace(
                        area,
                        longitude_1_tenth_minutes=area.longitude_1_tenth_minutes + 1,
                    )
                )
            ),
            "service_area_latitude": dict(
                bulletin_board=with_board(
                    service_area=replace(
                        area,
                        latitude_2_tenth_minutes=area.latitude_2_tenth_minutes + 1,
                    )
                )
            ),
            "service_area_priority": dict(
                bulletin_board=with_board(service_area=replace(area, priority=1))
            ),
            "physical_channel_order": dict(
                bulletin_board=with_board(
                    physical_channels=tuple(reversed(board.physical_channels))
                )
            ),
            "channel_number": dict(
                bulletin_board=with_pc(channel_number=pc.channel_number + 2)
            ),
            "frequency": dict(bulletin_board=with_pc(frequency=pc.frequency + 1)),
            "bandwidth": dict(
                bulletin_board=with_pc(bandwidth=PhysicalChannelBandwidth.KHZ_100)
            ),
            "transmit_enabled": dict(
                bulletin_board=with_pc(transmit_enabled=not pc.transmit_enabled)
            ),
            "selection_interval": dict(
                bulletin_board=with_pc(
                    random_access_selection_interval=pc.random_access_selection_interval + 1
                )
            ),
            "short_data_limit": dict(
                bulletin_board=with_pc(
                    short_data_message_limit=pc.short_data_message_limit + 1
                )
            ),
            "logical_function": dict(
                bulletin_board=with_definition(
                    function=(
                        LogicalChannelFunction.RANDOM_ACCESS
                        if definition.function is not LogicalChannelFunction.RANDOM_ACCESS
                        else LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING
                    )
                )
            ),
            "logical_repeat": dict(
                bulletin_board=with_definition(repeat=not definition.repeat)
            ),
            "logical_duration": dict(
                bulletin_board=with_definition(duration=definition.duration + 1)
            ),
        }
        for name, changes in mutations.items():
            with self.subTest(field=name):
                unchanged, error = coordinator._apply_verified_tbb(
                    coordinator.state,
                    _verified_tbb(canonical, clock, ids, **changes),
                    "tamper",
                    [],
                )
                self.assertIs(coordinator.state, unchanged)
                self.assertIsNotNone(error)
                self.assertEqual(MacReasonCode.TBB_FIXTURE_MISMATCH, _reason(error))

    def test_TC_MAC_004_metadata_is_excluded_and_active_error_precedence_is_exact(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        canonical = coordinator.profile.canonical_tbb
        ids = DeterministicIdSource(440)
        verified = _verified_tbb(canonical, clock, ids)
        active, error = coordinator._apply_verified_tbb(
            coordinator.state, verified, "first", []
        )
        self.assertIsNone(error)

        metadata_only = replace(
            verified,
            authentication_sequence=bytes([1]) * 64,
            fragment_count=2,
            packet_ids=(ids.new_packet_id(), ids.new_packet_id()),
        )
        duplicate, error = coordinator._apply_verified_tbb(
            active, metadata_only, "duplicate", []
        )
        self.assertIs(active, duplicate)
        self.assertIsNone(error)

        changed_board = replace(
            canonical.bulletin_board,
            service_area=replace(canonical.bulletin_board.service_area, priority=1),
        )
        same_version, error = coordinator._apply_verified_tbb(
            active,
            _verified_tbb(canonical, clock, ids, bulletin_board=changed_board),
            "same-version",
            [],
        )
        self.assertIs(active, same_version)
        self.assertEqual(MacReasonCode.TBB_FIXTURE_MISMATCH, _reason(error))

        other_version, error = coordinator._apply_verified_tbb(
            active,
            _verified_tbb(
                canonical,
                clock,
                ids,
                bulletin_board_version=canonical.bulletin_board_version + 1,
                bulletin_board=changed_board,
            ),
            "other-version",
            [],
        )
        self.assertIs(active, other_version)
        self.assertEqual(MacReasonCode.TBB_VERSION_SWITCH_NOT_SUPPORTED, _reason(error))

    def test_TC_MAC_004_activation_at_valid_until_is_rejected_without_snapshot(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        canonical = coordinator.profile.canonical_tbb
        clock.advance_utc(
            canonical.valid_until_nanoseconds()
            - clock.now_utc().total_nanoseconds()
            - 1
        )
        unchanged, error = coordinator._apply_verified_tbb(
            coordinator.state,
            _verified_tbb(canonical, clock, DeterministicIdSource(450)),
            "expiry-boundary",
            [],
        )
        self.assertIs(coordinator.state, unchanged)
        self.assertIsNone(unchanged.active_tbb)
        self.assertEqual(MacReasonCode.TBB_EXPIRED, _reason(error))

    def test_TC_MAC_004_expiry_aborts_active_transfer_reassembly_and_lc(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        canonical = coordinator.profile.canonical_tbb
        state, error = coordinator._activate_canonical_tbb(
            coordinator.state,
            fingerprint=canonical.canonical_tbb_fingerprint,
            bulletin_board_version=canonical.bulletin_board_version,
            source_id=canonical.source_id,
            control_station_id=canonical.control_station_id,
        )
        self.assertIsNone(error)
        ids = DeterministicIdSource(455)
        transfer_id = ids.new_transfer_id()
        session_handle = ids.new_session_handle()
        route = RouteAddresses(
            original_source=OriginalSource(coordinator.local_station_id),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        transfer = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.TRANSMITTING,
            route=route,
            ack_required=True,
            presentation_size=1000,
            public_state=TransferState.TRANSMITTING,
            session_handle=session_handle,
            link_id=LinkId(11),
            physical_channel_name="LOWER",
            logical_channel_tx=2,
            logical_channel_rx=16,
        )
        rx_session = RxSessionContext(
            session_handle=session_handle,
            route=route,
            peer_station_id_value=ROC_STATION_ID.value,
            logical_channel_rx=16,
            opened=True,
        )
        allocation = LcAllocation(
            logical_channel_tx=2,
            logical_channel_rx=16,
            owner_station_id_value=coordinator.local_station_id.value,
            peer_station_id_value=ROC_STATION_ID.value,
            state=LcState.ACTIVE,
            activation_frame_ordinal=1,
            last_valid_frame_ordinal=10_000_000,
            tdma_channel=1,
            issued_by_local=False,
        )
        state = state.with_transfer(transfer).with_rx_session(rx_session).evolve(
            lc_allocations=(allocation,)
        )
        clock.advance_utc(
            canonical.valid_until_nanoseconds()
            - clock.now_utc().total_nanoseconds()
        )
        commands = []
        expired = coordinator._expire_state(state, "expiry", commands)
        self.assertIsNone(expired.active_tbb)
        self.assertEqual((), expired.lc_allocations)
        self.assertIs(TransferPhase.TERMINAL, expired.transfer(transfer_id).phase)
        self.assertIsNone(expired.rx_session(session_handle.value))
        self.assertEqual(3, len(commands))
        publish = next(
            command.payload
            for command in commands
            if hasattr(command.payload, "state")
        )
        self.assertEqual(
            MacReasonCode.TBB_EXPIRED_DURING_SESSION,
            publish.error.details.get("reason_code"),
        )

    def test_TC_MAC_003_partial_candidate_deadline_is_not_extended_and_cleanup_is_result_gated(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        canonical = coordinator.profile.canonical_tbb
        ids = DeterministicIdSource(460)
        key = TbbCandidateKey(
            source_id=canonical.source_id,
            control_station_id=canonical.control_station_id,
            bulletin_board_version=canonical.bulletin_board_version,
        )
        first_packet = ids.new_packet_id()
        started_update = TbbAssemblyUpdate(
            key=key,
            status=TbbAssemblyStatus.STARTED,
            received_fragment_count=1,
            expected_fragment_count=4,
            packet_ids=(first_packet,),
            updated_at=clock.now_utc(),
        )
        commands = []
        started = coordinator._handle_tbb_assembly_updates(
            coordinator.state, (started_update,), "started", commands
        )
        first_frame = frame_ordinal_at_nanoseconds(
            clock.now_utc().total_nanoseconds()
        )
        self.assertEqual(
            first_frame + 13, started.tbb_candidate.deadline_frame_ordinal
        )
        self.assertEqual(1, len(commands))
        original_timer = started.timer(started.tbb_candidate.timer_id)

        duplicate = TbbAssemblyUpdate(
            key=key,
            status=TbbAssemblyStatus.INCOMPLETE,
            received_fragment_count=2,
            expected_fragment_count=4,
            packet_ids=(first_packet, ids.new_packet_id()),
            updated_at=clock.now_utc(),
        )
        duplicate_commands = []
        unchanged = coordinator._handle_tbb_assembly_updates(
            started, (duplicate,), "duplicate", duplicate_commands
        )
        self.assertIs(started, unchanged)
        self.assertEqual([], duplicate_commands)
        self.assertIs(original_timer, unchanged.timer(original_timer.timer_id))

        timeout_commands = []
        timed_out = coordinator._on_tbb_assembly_timeout(
            started.without_timer(original_timer.timer_id),
            "timeout",
            timeout_commands,
        )
        self.assertIsNotNone(timed_out.tbb_candidate)
        self.assertIs(
            TbbAssemblyControlKind.TIMEOUT,
            timeout_commands[0].payload.request.kind,
        )
        cleaned = coordinator._apply_link_result(
            timed_out,
            SimpleNamespace(command_id=timeout_commands[0].command_id),
            TbbAssemblyControlResult(
                kind=TbbAssemblyControlKind.TIMEOUT,
                discarded_candidates=(key,),
                discarded_fragment_count=2,
                discarded_bytes=20,
                active_candidate_count=0,
            ),
            SimpleNamespace(event_id="timeout-result"),
            [],
        )
        self.assertIsNone(cleaned.tbb_candidate)

        verified_commands = []
        verified = coordinator._handle_tbb_assembly_updates(
            started,
            (
                replace(
                    duplicate,
                    status=TbbAssemblyStatus.VERIFIED,
                    received_fragment_count=4,
                    packet_ids=(
                        first_packet,
                        ids.new_packet_id(),
                        ids.new_packet_id(),
                        ids.new_packet_id(),
                    ),
                ),
            ),
            "verified",
            verified_commands,
        )
        self.assertIsNone(verified.tbb_candidate)
        self.assertIsNone(verified.timer(original_timer.timer_id))
        self.assertEqual(1, len(verified_commands))

        unexpected = replace(
            started_update,
            key=replace(key, source_id=StationId(canonical.source_id.value + 1)),
        )
        abort_commands = []
        rejected = coordinator._handle_tbb_assembly_updates(
            coordinator.state, (unexpected,), "unexpected", abort_commands
        )
        self.assertIsNone(rejected.tbb_candidate)
        self.assertIs(
            TbbAssemblyControlKind.ABORT,
            abort_commands[0].payload.request.kind,
        )

        full = coordinator.state.evolve(
            timers=tuple(
                TimerRecord(
                    timer_id=f"occupied-{index}",
                    purpose=TimerPurpose.FRAME_BOUNDARY,
                    deadline_monotonic_nanoseconds=index + 1,
                )
                for index in range(Stage4Limits.ACTIVE_TIMERS)
            )
        )
        capacity_commands = []
        capacity_rejected = coordinator._handle_tbb_assembly_updates(
            full, (started_update,), "capacity", capacity_commands
        )
        self.assertIsNone(capacity_rejected.tbb_candidate)
        self.assertEqual(1, len(capacity_commands))
        self.assertIs(
            TbbAssemblyControlKind.ABORT,
            capacity_commands[0].payload.request.kind,
        )

    def test_TC_MAC_003_already_expired_roc_fixture_fails_running_readiness(self):
        expired_fixture = replace(
            tbb_fixture(),
            start_time=FIXTURE_START_TIME - 120,
            validity=1,
        )
        _clock, endpoints = build_stage4_software_group(
            with_type0=False, fixture=expired_fixture
        )
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        configuration = roc.dependencies.configuration.endpoint
        self.assertTrue(
            roc.start(
                ServiceStartRequest(
                    local_station_id=configuration.station_id,
                    role=configuration.role,
                )
            ).is_ok
        )
        ready = roc.stage4.mac.begin_running()
        self.assertTrue(ready.is_err)
        self.assertEqual(
            MacReasonCode.TBB_EXPIRED,
            ready.error.details.get("reason_code"),
        )
        self.assertIsNone(roc.stage4.mac.state.active_tbb)


if __name__ == "__main__":
    unittest.main()
