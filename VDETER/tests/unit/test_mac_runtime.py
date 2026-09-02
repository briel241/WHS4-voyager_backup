"""TC-MAC-015: bounded runtime state, ledger, and failure vocabulary."""

from __future__ import annotations

import unittest
from dataclasses import replace
from threading import Event, RLock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
    build_stage4_software_group,
    start_stage4_group,
)
from vdeter.common.addressing import (
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    LinkAnalyzeRequest,
    LinkPacketEncodeRequest,
    LinkTransferControlRequest,
    TbbAssemblyControlKind,
    TbbAssemblyControlResult,
    TransferControlKind,
)
from vdeter.common.contracts.mac_execution import (
    CancelWakeupCommand,
    ClockAdvancedEvent,
    CommandCompletedEvent,
    LinkResultEvent,
    MacEventEnvelope,
    MacCommandBatch,
    MacCommandEnvelope,
    MacFailureReason,
    MacOutboxReservation,
    InvokeLinkCommand,
    MacReasonCode,
    PresentationDeliveryResultEvent,
    PublishTransferStateCommand,
    RxDeliveryProcedure,
    ScheduleWakeupCommand,
    TimerPurpose,
    WakeupFiredEvent,
)
from vdeter.common.contracts.message import (
    MediaAccessControlBody,
    MessageEncodeRequest,
    TbbCandidateKey,
)
from vdeter.common.contracts.service import (
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferState,
)
from vdeter.common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
)
from vdeter.common.results import Err, Ok, internal_error
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PayloadFormat,
    SequentialId,
    StationId,
    TransferId,
)
from vdeter.common.time import SyncStatus, TestTimeSource as ControlledTimeSource
from vdeter.mac import ack
from vdeter.mac.models import (
    AckPendingState,
    EndpointLifecycle,
    PendingCommandClass,
    PendingCommandRecord,
    RelayRecord,
    MacState,
    MacStateRepository,
    Stage4Limits,
    ScheduledTxIntent,
    TimerRecord,
    TransferContext,
    TransferPhase,
    TransferRole,
)
from vdeter.mac.interface import Stage4Mac
from vdeter.mac import transfers as transfer_errors
from vdeter.mac.tbb import LOWER_CHANNEL
from vdeter.runtime.mac_commands import (
    EndpointHealthLog,
    MacCommandOutbox,
    MacIngressQueue,
)
from vdeter.runtime.projection import PublicErrorCode, to_service_error


class Stage4RuntimeTest(unittest.TestCase):
    def test_TC_MAC_015_all_independent_bounds_and_failure_reasons_are_closed(self):
        self.assertEqual((64, 64), (Stage4Limits.INGRESS_EVENTS, Stage4Limits.COMMAND_OUTBOX))
        self.assertEqual(96, Stage4Limits.PENDING_DEPENDENCY_TOTAL)
        self.assertEqual(96, 68 + 20 + 8)
        self.assertEqual((32, 16, 3), (
            Stage4Limits.RX_DELIVERY_LEDGER,
            Stage4Limits.LOCAL_TRANSFER_FIFO,
            Stage4Limits.ACK_LEDGER_FRAMES,
        ))
        self.assertEqual(11, len(MacFailureReason))

    @staticmethod
    def _control_intent(coordinator, intent_id: str, transmit_at: int, *, transfer_id=None):
        body = MediaAccessControlBody(
            source_id=coordinator.local_station_id,
            media_access_priority_level=0,
            random_access_selection_interval=8,
            short_data_message_limit=4,
            system_status=0,
        )
        correlation = Correlation(transfer_id=transfer_id) if transfer_id is not None else Correlation()
        return ScheduledTxIntent(
            intent_id=intent_id,
            kind="ENCODE",
            transmit_at_nanoseconds=transmit_at,
            channel_name=LOWER_CHANNEL.name,
            correlation_transfer_id=(transfer_id.value if transfer_id is not None else None),
            encode_request=LinkPacketEncodeRequest(
                link_id=LinkId(11),
                messages=(MessageEncodeRequest(body=body, correlation=correlation),),
                correlation=correlation,
            ),
        )

    def test_TC_MAC_015_scheduled_tx_intent_32_is_bounded_and_33rd_is_explicit(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        now = clock.now_utc().total_nanoseconds()
        transmit_at = now + coordinator.profile.schedule_horizon_nanoseconds + 1_000_000_000
        state = coordinator.state.evolve(lifecycle=EndpointLifecycle.RUNNING)
        commands: list = []
        errors: list = []

        for index in range(Stage4Limits.SCHEDULED_TX_INTENTS):
            state = coordinator._defer_or_emit(
                state,
                intent=self._control_intent(
                    coordinator, f"bounded-intent-{index}", transmit_at + index
                ),
                causation_event_id="intent-capacity",
                commands=commands,
                errors=errors,
            )
        self.assertEqual([], errors)
        self.assertEqual(Stage4Limits.SCHEDULED_TX_INTENTS, len(state.tx_intents))
        self.assertEqual(1, sum(timer.purpose is TimerPurpose.RETRY for timer in state.timers))

        rejected = self._control_intent(coordinator, "bounded-intent-33", transmit_at + 33)
        state_after_reject = coordinator._defer_or_emit(
            state,
            intent=rejected,
            causation_event_id="intent-capacity",
            commands=[],
            errors=errors,
        )
        self.assertEqual(Stage4Limits.SCHEDULED_TX_INTENTS, len(state_after_reject.tx_intents))
        self.assertNotIn(rejected.intent_id, tuple(item.intent_id for item in state_after_reject.tx_intents))
        self.assertEqual(ErrorCode.MAC_RESOURCE_UNAVAILABLE, errors[-1].error.code)
        self.assertEqual(
            MacFailureReason.RESOURCE_UNAVAILABLE.value,
            errors[-1].error.details.get("mac_reason"),
        )

        transfer_id = DeterministicIdSource(161).new_transfer_id()
        transfer = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_SHORT_DATA,
            phase=TransferPhase.BUILDING,
            route=RouteAddresses(
                original_source=OriginalSource(MASS_A_STATION_ID),
                original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            ),
            ack_required=False,
            presentation_size=32,
            public_state=TransferState.QUEUED,
        )
        transfer_commands: list = []
        transfer_errors: list = []
        failed = coordinator._defer_or_emit(
            state.with_transfer(transfer),
            intent=self._control_intent(
                coordinator, "bounded-transfer-33", transmit_at + 34, transfer_id=transfer_id
            ),
            causation_event_id="intent-capacity-transfer",
            commands=transfer_commands,
            errors=transfer_errors,
        )
        self.assertEqual([], transfer_errors)
        self.assertIs(TransferPhase.TERMINAL, failed.transfer(transfer_id).phase)
        self.assertEqual(TransferState.FAILED, failed.transfer(transfer_id).public_state)
        self.assertTrue(any(
            isinstance(command.payload, PublishTransferStateCommand)
            and command.payload.error.code == ErrorCode.MAC_RESOURCE_UNAVAILABLE
            for command in transfer_commands
        ))

    def test_TC_MAC_015_horizon_wakeup_is_self_scheduled_and_late_fire_is_slot_missed(self):
        for late in (False, True):
            with self.subTest(late=late):
                clock, endpoints = build_stage4_software_group(with_type0=False)
                mass = next(
                    endpoint
                    for endpoint in endpoints
                    if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
                )
                support = mass.stage4
                coordinator = support.mac.coordinator
                coordinator._profile = replace(
                    coordinator.profile,
                    receive_lead_nanoseconds=1,
                    transmit_lead_nanoseconds=1,
                )
                now_utc = clock.now_utc().total_nanoseconds()
                transmit_at = (
                    now_utc
                    + coordinator.profile.schedule_horizon_nanoseconds
                    + 500_000_000
                )
                intent = self._control_intent(
                    coordinator, f"horizon-intent-{late}", transmit_at
                )
                commands: list = []
                errors: list = []
                deferred = coordinator._defer_or_emit(
                    coordinator.state.evolve(lifecycle=EndpointLifecycle.RUNNING),
                    intent=intent,
                    causation_event_id="horizon-seed",
                    commands=commands,
                    errors=errors,
                )
                self.assertEqual([], errors)
                timer = next(
                    item for item in deferred.timers if item.purpose is TimerPurpose.RETRY
                )
                self.assertTrue(any(
                    isinstance(command.payload, ScheduleWakeupCommand)
                    and command.payload.purpose is TimerPurpose.RETRY
                    for command in commands
                ))
                support.mac.repository.commit(deferred)
                support._timers[timer.timer_id] = (
                    timer.deadline_monotonic_nanoseconds,
                    timer.purpose,
                )

                if late:
                    delta = transmit_at - clock.now_utc().total_nanoseconds() + 1
                else:
                    delta = (
                        timer.deadline_monotonic_nanoseconds
                        - clock.now_monotonic().total_nanoseconds()
                    )
                clock.advance(delta)
                # advance_clock() 없이 Runtime timer만 발화한다.
                support.fire_due_timers()
                self.assertNotIn(
                    intent.intent_id,
                    tuple(item.intent_id for item in support.mac.state.tx_intents),
                )
                if late:
                    self.assertTrue(any(
                        details.get("mac_reason") == MacFailureReason.SLOT_MISSED.value
                        for _code, _reason, details in support.health.entries()
                    ))

    def test_horizon_rearm_same_entry_keeps_existing_timer_instead_of_self_replace(self):
        """같은 horizon 진입이 더 이른 환산 deadline으로 재무장되면 기존 유지.

        UTC 재보정 후 같은 entry의 monotonic 환산값이 이르어지면 종전엔
        `replaces_timer_id == timer_id` 자기 교체로 `InvalidValueError`가 나
        decode 반영 경로가 죽었다 (2026-09-02 정식 Phase G MASS-B 크래시).
        """

        clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        entry_utc = clock.now_utc().total_nanoseconds() + 2_000_000_000
        now_monotonic = clock.now_monotonic().total_nanoseconds()
        computed_deadline = now_monotonic + (
            entry_utc - clock.now_utc().total_nanoseconds())
        seeded = coordinator.state.evolve(
            lifecycle=EndpointLifecycle.RUNNING
        ).with_timer(TimerRecord(
            timer_id=f"horizon-{entry_utc}",
            purpose=TimerPurpose.RETRY,
            deadline_monotonic_nanoseconds=computed_deadline + 5_000_000,
        ))

        commands: list = []
        errors: list = []
        # 수정 전엔 여기서 InvalidValueError가 났다.
        next_state, ok = coordinator._arm_horizon_entry(
            seeded,
            entry_utc_nanoseconds=entry_utc,
            causation_event_id="rearm-same-entry",
            commands=commands,
            errors=errors,
        )
        self.assertTrue(ok)
        self.assertEqual([], errors)
        self.assertEqual([], commands, "같은 진입 재무장은 새 command를 만들지 않는다")
        kept = next(t for t in next_state.timers
                    if t.purpose is TimerPurpose.RETRY)
        self.assertEqual(f"horizon-{entry_utc}", kept.timer_id)
        self.assertEqual(computed_deadline + 5_000_000,
                         kept.deadline_monotonic_nanoseconds)

        # 다른 진입의 느린 timer는 종전처럼 교체된다 (회귀 방지).
        other_state = coordinator.state.evolve(
            lifecycle=EndpointLifecycle.RUNNING
        ).with_timer(TimerRecord(
            timer_id="horizon-999",
            purpose=TimerPurpose.RETRY,
            deadline_monotonic_nanoseconds=computed_deadline + 5_000_000,
        ))
        commands2: list = []
        replaced_state, ok2 = coordinator._arm_horizon_entry(
            other_state,
            entry_utc_nanoseconds=entry_utc,
            causation_event_id="rearm-other-entry",
            commands=commands2,
            errors=errors,
        )
        self.assertTrue(ok2)
        wakeup = next(c.payload for c in commands2
                      if isinstance(c.payload, ScheduleWakeupCommand))
        self.assertEqual("horizon-999", wakeup.replaces_timer_id)
        self.assertNotEqual(wakeup.timer_id, wakeup.replaces_timer_id)
        self.assertEqual(
            {f"horizon-{entry_utc}"},
            {t.timer_id for t in replaced_state.timers
             if t.purpose is TimerPurpose.RETRY})

    def test_TC_MAC_001_commit_precedes_dispatch_and_failure_aborts_reservation(self):
        def facade_fixture(*, fail_commit=False):
            lock = RLock()
            ids = DeterministicIdSource(151)
            health = EndpointHealthLog()
            outbox = MacCommandOutbox(ids, lock, health)
            repository = MacStateRepository(MacState())
            old_state = repository.state
            next_state = old_state.evolve(lifecycle=EndpointLifecycle.DRAINING)
            command = MacCommandEnvelope(
                command_id="commit-order-command",
                causation_event_id="commit-order-event",
                correlation=Correlation(),
                payload=CancelWakeupCommand("commit-order-timer"),
            )
            batch = MacCommandBatch(
                batch_id="commit-order-batch",
                causation_event_id="commit-order-event",
                commands=(command,),
            )
            plan = SimpleNamespace(result=Ok(None), next_state=next_state, batch=batch)

            class Coordinator:
                def handle_event(self, _event):
                    return plan

            facade = Stage4Mac(Coordinator(), repository, outbox, lock, ids)
            observations = []
            worker_started = Event()
            worker_finished = Event()
            worker_result = []

            def dispatch():
                worker_started.set()
                worker_result.append((outbox.take_ready(), repository.state))
                worker_finished.set()

            worker = Thread(target=dispatch, name="stage4-commit-order-dispatch")

            def commit_hook(_next_state):
                observations.append(
                    (
                        outbox.occupancy(),
                        outbox.reserved_count(),
                        outbox.ready_count(),
                        outbox.peek_ready(),
                        repository.state,
                    )
                )
                if fail_commit:
                    raise RuntimeError("injected state commit failure")
                worker.start()
                self.assertTrue(worker_started.wait(1.0))
                self.assertFalse(worker_finished.wait(0.05))

            repository.set_commit_hook(commit_hook)
            return (
                facade,
                repository,
                outbox,
                old_state,
                next_state,
                command,
                observations,
                worker,
                worker_finished,
                worker_result,
            )

        (
            facade,
            repository,
            outbox,
            old_state,
            next_state,
            command,
            observations,
            worker,
            worker_finished,
            worker_result,
        ) = facade_fixture()
        committed = facade.handle_event(object())
        self.assertTrue(committed.is_ok, getattr(committed, "error", None))
        self.assertTrue(worker_finished.wait(1.0))
        worker.join(1.0)
        self.assertEqual([(1, 1, 0, (), old_state)], observations)
        self.assertIs(next_state, repository.state)
        self.assertEqual([((command,), next_state)], worker_result)
        self.assertEqual(0, outbox.occupancy())

        (
            failed_facade,
            failed_repository,
            failed_outbox,
            failed_old_state,
            _failed_next_state,
            _failed_command,
            failed_observations,
            failed_worker,
            _failed_worker_finished,
            _failed_worker_result,
        ) = facade_fixture(fail_commit=True)
        rejected = failed_facade.handle_event(object())
        self.assertTrue(rejected.is_err)
        self.assertEqual(ErrorCode.MAC_TRANSFER_FAILED, rejected.error.code)
        self.assertIs(failed_old_state, failed_repository.state)
        self.assertEqual(0, failed_repository.commit_count)
        self.assertEqual([(1, 1, 0, (), failed_old_state)], failed_observations)
        self.assertEqual((0, 0, 0), (
            failed_outbox.occupancy(),
            failed_outbox.reserved_count(),
            failed_outbox.ready_count(),
        ))
        self.assertFalse(failed_worker.is_alive())

    def test_TC_MAC_001_sink_reentry_empty_receipt_and_receipt_reenqueue_are_closed(self):
        lock = RLock()
        ids = DeterministicIdSource(162)
        repository = MacStateRepository(MacState())
        empty_plan = SimpleNamespace(
            result=Ok(None), next_state=repository.state, batch=None
        )
        outer_state = repository.state.evolve(lifecycle=EndpointLifecycle.DRAINING)
        command = MacCommandEnvelope(
            command_id="reentry-command",
            causation_event_id="reentry-event",
            correlation=Correlation(),
            payload=CancelWakeupCommand("reentry-timer"),
        )
        outer_plan = SimpleNamespace(
            result=Ok(None),
            next_state=outer_state,
            batch=MacCommandBatch(
                batch_id="reentry-batch",
                causation_event_id="reentry-event",
                commands=(command,),
            ),
        )

        class Coordinator:
            def handle_event(self, _event):
                return empty_plan

        class ReenteringSink:
            def __init__(self):
                self.facade = None
                self.reentry = None
                self.published = False

            def submit(self, batch):
                self.reentry = self.facade.handle_event(SimpleNamespace())
                return Ok(MacOutboxReservation(
                    reservation_id="reentry-reservation",
                    batch_id=batch.batch_id,
                    command_ids=batch.command_ids(),
                ))

            def publish(self, _reservation):
                self.published = True

            def abort(self, _reservation):
                raise AssertionError("successful outer commit must not abort")

        sink = ReenteringSink()
        facade = Stage4Mac(Coordinator(), repository, sink, lock, ids)
        sink.facade = facade
        outer = facade._apply(outer_plan, receipt=True)
        self.assertTrue(outer.is_ok)
        self.assertTrue(sink.published)
        self.assertTrue(sink.reentry.is_err)
        self.assertEqual(
            "SYNCHRONOUS_SINK_REENTRY",
            sink.reentry.error.details.get("reason_code"),
        )
        self.assertIs(repository.state, outer_state)

        empty = facade.handle_event(SimpleNamespace())
        self.assertTrue(empty.is_ok)
        self.assertIsNone(empty.value.batch_id)
        self.assertEqual((), empty.value.command_ids)
        time_source = ControlledTimeSource(utc_nanoseconds=10, monotonic_nanoseconds=10)
        with self.assertRaises(InvalidValueError):
            MacEventEnvelope(
                event_id="receipt-reenqueue",
                occurred_at_utc=time_source.now_utc(),
                observed_at_monotonic=time_source.now_monotonic(),
                ingress_sequence=0,
                correlation=Correlation(),
                payload=empty.value,
            )

    def test_TC_MAC_015_all_failure_reasons_use_the_approved_transfer_projection(self):
        def injected(reason, code, category=ErrorCategory.PROTOCOL):
            return internal_error(
                Layer.MAC,
                code,
                category,
                "injected Stage 4 projection fault",
                details=Details.of(mac_reason=reason.value),
            ).error

        cases = (
            (
                MacFailureReason.EVENT_INVALID,
                injected(MacFailureReason.EVENT_INVALID, ErrorCode.MAC_TRANSFER_FAILED),
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.COMMAND_RESULT_UNMATCHED,
                injected(
                    MacFailureReason.COMMAND_RESULT_UNMATCHED,
                    ErrorCode.MAC_TRANSFER_FAILED,
                ),
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.SLOT_MISSED,
                transfer_errors.slot_missed_error("fault-matrix").error,
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.TIME_UNAVAILABLE,
                injected(
                    MacFailureReason.TIME_UNAVAILABLE,
                    ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                ),
                ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
                PublicErrorCode.INTERNAL_ERROR,
            ),
            (
                MacFailureReason.RESOURCE_UNAVAILABLE,
                transfer_errors.resource_busy_error().error,
                ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.LC_CONFLICT,
                transfer_errors.lc_conflict_error().error,
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.ACK_TIMEOUT,
                transfer_errors.ack_timeout_error().error,
                ErrorCode.MAC_TRANSFER_TIMEOUT,
                PublicErrorCode.TRANSFER_TIMEOUT,
            ),
            (
                MacFailureReason.RETRY_EXHAUSTED,
                transfer_errors.retry_exhausted_error(resource_request=False).error,
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.DEPENDENCY_FAILED,
                injected(
                    MacFailureReason.DEPENDENCY_FAILED,
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                ),
                ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                PublicErrorCode.INTERNAL_ERROR,
            ),
            (
                MacFailureReason.SESSION_ABORTED,
                transfer_errors.tbb_expired_error(during_session=True).error,
                ErrorCode.MAC_TRANSFER_FAILED,
                PublicErrorCode.TRANSFER_FAILED,
            ),
            (
                MacFailureReason.FEATURE_NOT_SUPPORTED,
                transfer_errors.unsupported_profile_error(17).error,
                ErrorCode.REGISTRY_UNKNOWN_KEY,
                PublicErrorCode.UNSUPPORTED_REQUEST,
            ),
        )
        self.assertEqual(set(MacFailureReason), {item[0] for item in cases})
        for reason, error, internal_code, public_code in cases:
            with self.subTest(reason=reason.value):
                self.assertEqual(reason.value, error.details.get("mac_reason"))
                self.assertEqual(internal_code, error.code)
                self.assertEqual(public_code, to_service_error(error).code)

        time_fallback = injected(
            MacFailureReason.TIME_UNAVAILABLE, ErrorCode.MAC_TRANSFER_FAILED
        )
        dependency_fallback = injected(
            MacFailureReason.DEPENDENCY_FAILED, ErrorCode.MAC_TRANSFER_FAILED
        )
        type90_exhausted = transfer_errors.retry_exhausted_error(
            resource_request=True
        ).error
        self.assertEqual(ErrorCode.MAC_TRANSFER_FAILED, time_fallback.code)
        self.assertEqual(ErrorCode.MAC_TRANSFER_FAILED, dependency_fallback.code)
        self.assertEqual(ErrorCode.MAC_RESOURCE_UNAVAILABLE, type90_exhausted.code)

    def test_TC_MAC_015_delivery_key_and_inclusive_expiry_are_deterministic(self):
        source = StationId(111111111)
        destination = Addressed(StationId(222222222))
        key = ack.delivery_key(
            RxDeliveryProcedure.DATA_SESSION,
            source,
            destination,
            SequentialId(0x1234),
        )
        expected = (
            b"DATA_SESSION\x00"
            + source.value.to_bytes(4, "big")
            + b"\x01"
            + destination.station_id.value.to_bytes(4, "big")
            + b"\x124"
        )
        self.assertEqual(expected, key)
        pending = ack.new_pending_record(
            key=key,
            procedure=RxDeliveryProcedure.DATA_SESSION,
            payload_length=4,
            digest=ack.payload_digest(b"data"),
            first_received_frame_ordinal=10,
            highest_retransmission_number=None,
            pending_delivery_key=("pending",),
        )
        live = ack.promoted_live_record(
            pending,
            completion_frame_ordinal=20,
            ack_state=AckPendingState.ACK_NONE,
            replay_context=None,
        )
        self.assertFalse(ack.is_expired(live, 23))
        self.assertTrue(ack.is_expired(live, 24))

    def test_TC_MAC_015_invalid_reservations_and_draining_are_bounded_and_non_destructive(self):
        ids = DeterministicIdSource(150)
        health = EndpointHealthLog()
        outbox = MacCommandOutbox(ids, RLock(), health, capacity=2)

        def envelope(command_id, payload):
            return MacCommandEnvelope(
                command_id=command_id,
                causation_event_id="cause",
                correlation=Correlation(),
                payload=payload,
            )

        first = MacCommandBatch(
            batch_id="batch-1",
            causation_event_id="cause",
            commands=(envelope("cancel-1", CancelWakeupCommand("timer-1")),),
        )
        reserved = outbox.submit(first)
        self.assertTrue(reserved.is_ok)
        forged = MacOutboxReservation(
            reservation_id=reserved.value.reservation_id,
            batch_id="wrong-batch",
            command_ids=reserved.value.command_ids,
        )
        outbox.publish(forged)
        self.assertEqual((1, 1, 0), (outbox.occupancy(), outbox.reserved_count(), outbox.ready_count()))
        self.assertIs(
            MacReasonCode.OUTBOX_RESERVATION_INVALID,
            health.entries()[-1][1],
        )
        outbox.publish(reserved.value)
        outbox.publish(reserved.value)
        self.assertEqual((1, 0, 1), (outbox.occupancy(), outbox.reserved_count(), outbox.ready_count()))

        outbox.clear()
        outbox.set_draining(True)
        blocked = MacCommandBatch(
            batch_id="batch-blocked",
            causation_event_id="cause",
            commands=(
                envelope(
                    "non-terminal",
                    PublishTransferStateCommand(
                        transfer_id=TransferId("00000000-0000-4000-8000-000000000150"),
                        state=TransferState.TRANSMITTING,
                    ),
                ),
            ),
        )
        self.assertTrue(outbox.submit(blocked).is_err)
        allowed = MacCommandBatch(
            batch_id="batch-cleanup",
            causation_event_id="cause",
            commands=(envelope("cancel-2", CancelWakeupCommand("timer-2")),),
        )
        self.assertTrue(outbox.submit(allowed).is_ok)

    def test_TC_MAC_015_ingress_backpressure_is_bounded_and_preserves_result_order(self):
        clock = ControlledTimeSource(1, 1)
        queue = MacIngressQueue(
            RLock(),
            clock,
            DeterministicIdSource(155),
            capacity=2,
            held_capacity=2,
        )
        for _ in range(2):
            self.assertTrue(
                queue.enqueue(
                    ClockAdvancedEvent(sync_status=SyncStatus.SYNCHRONIZED)
                ).is_ok
            )
        rejected_local = queue.enqueue(
            ClockAdvancedEvent(sync_status=SyncStatus.SYNCHRONIZED)
        )
        self.assertTrue(rejected_local.is_err)
        self.assertEqual(0, queue.held_count())

        first = queue.enqueue(
            WakeupFiredEvent(
                timer_id="accepted-timer-result",
                purpose=TimerPurpose.RETRY,
                scheduled_for_monotonic=clock.now_monotonic(),
            ),
            correlation=Correlation(),
            event_id="held-first",
            priority=True,
        )
        second = queue.enqueue(
            PresentationDeliveryResultEvent(
                packet_id=DeterministicIdSource(1550).new_packet_id(),
                delivery_index=0,
                delivered_at_monotonic=clock.now_monotonic(),
            ),
            correlation=Correlation(),
            event_id="held-second",
            priority=True,
        )
        self.assertTrue(first.is_err and second.is_err)
        self.assertEqual(2, queue.held_count())
        overflow = queue.enqueue(
            CommandCompletedEvent(command_id="overflow-result"),
            correlation=Correlation(),
            causation_command_id="overflow-result",
            priority=True,
        )
        self.assertTrue(overflow.is_err)
        self.assertEqual(2, queue.held_count())

        queue.drain()
        self.assertEqual(2, queue.release_held())
        self.assertEqual(
            ("held-first", "held-second"),
            (queue.pop_next().event_id, queue.pop_next().event_id),
        )

        equality_queue = MacIngressQueue(
            RLock(), clock, DeterministicIdSource(1551), capacity=4, held_capacity=2
        )
        equality_queue.enqueue(
            CommandCompletedEvent(command_id="deadline-valid-result"),
            causation_command_id="deadline-valid-result",
            event_id="deadline-valid-result",
            priority=True,
        )
        equality_queue.enqueue(
            WakeupFiredEvent(
                timer_id="deadline-timer",
                purpose=TimerPurpose.RESOURCE_RESPONSE_TIMEOUT,
                scheduled_for_monotonic=clock.now_monotonic(),
            ),
            event_id="deadline-timeout",
            priority=True,
        )
        self.assertEqual(
            ("deadline-valid-result", "deadline-timeout"),
            (
                equality_queue.pop_next().event_id,
                equality_queue.pop_next().event_id,
            ),
        )

    def test_TC_MAC_015_terminal_notification_retry_exhaustion_and_missing_rx_close_cleanup(self):
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        support = mass.stage4
        transfer_id = DeterministicIdSource(163).new_transfer_id()
        command = MacCommandEnvelope(
            command_id="terminal-retry-exhaustion",
            causation_event_id="terminal-retry",
            correlation=Correlation(transfer_id=transfer_id),
            payload=PublishTransferStateCommand(
                transfer_id=transfer_id,
                state=TransferState.FAILED,
                error=transfer_errors.endpoint_stopping_error().error,
            ),
        )
        consumer_error = internal_error(
            Layer.RUNTIME,
            ErrorCode.INTERNAL_ERROR,
            ErrorCategory.INTERNAL,
            "injected terminal consumer rejection",
        )
        with patch.object(
            type(mass),
            "_set_transfer_state",
            autospec=True,
            return_value=Err(consumer_error.error),
        ) as rejected_consumer:
            support._execute(command)
            support.pump()
        self.assertEqual(3, rejected_consumer.call_count)
        self.assertNotIn(command.command_id, support._consumer_attempts)
        self.assertEqual(0, support.outbox.occupancy())

        rx_command_id = "missing-rx-close"
        rx_pending = PendingCommandRecord(
            command_id=rx_command_id,
            command_class=PendingCommandClass.RX,
            payload_kind="InvokePhyCommand",
            accepted=True,
            request_key=("RX", "missing-window", 123),
        )
        support.mac.repository.commit(
            support.mac.state.evolve(
                lifecycle=EndpointLifecycle.RUNNING,
                pending_commands=(rx_pending,),
            )
        )
        support._rx_commands[("missing-window", 123)] = command
        support.begin_stop("missing-rx-close")
        self.assertIn(("missing-window", 123), support._rx_commands)
        support.finish_stop()
        self.assertEqual({}, support._rx_commands)
        self.assertIsNone(support.mac.state.pending_command(rx_command_id))
        self.assertIn(rx_command_id, support.mac.state.recent_terminal_ids)

    def test_TC_MAC_015_wrong_link_variant_is_non_destructive_and_dependency_code_is_preserved(self):
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        canonical = coordinator.profile.canonical_tbb
        wrong_command_id = "wrong-link-variant"
        pending = PendingCommandRecord(
            command_id=wrong_command_id,
            command_class=PendingCommandClass.NON_CLEANUP_NON_RX,
            payload_kind="InvokeLinkCommand",
            request_key=("LINK", "TbbEncodeRequest"),
        )
        state = coordinator.state.evolve(pending_commands=(pending,))
        wrong_result = TbbAssemblyControlResult(
            kind=TbbAssemblyControlKind.ABORT,
            discarded_candidates=(
                TbbCandidateKey(
                    source_id=canonical.source_id,
                    control_station_id=canonical.control_station_id,
                    bulletin_board_version=canonical.bulletin_board_version,
                ),
            ),
            discarded_fragment_count=1,
            discarded_bytes=10,
            active_candidate_count=0,
        )
        rejected = coordinator._on_link_result(
            state,
            SimpleNamespace(
                event_id="wrong-result",
                causation_command_id=wrong_command_id,
                correlation=Correlation(),
                payload=SimpleNamespace(
                    command_id=wrong_command_id, result=wrong_result
                ),
            ),
            [],
        )
        self.assertTrue(rejected.result.is_err)
        self.assertIs(state, rejected.next_state)
        self.assertIs(pending, rejected.next_state.pending_command(wrong_command_id))
        self.assertEqual(
            MacFailureReason.COMMAND_RESULT_UNMATCHED.value,
            rejected.result.error.details.get("mac_reason"),
        )

        service_sink = mass.dependencies.service_event_sink
        service_event_count = len(service_sink.events())
        mass.stage4.mac.repository.commit(state)
        self.assertTrue(
            mass.stage4.enqueue(
                LinkResultEvent(
                    command_id=wrong_command_id,
                    result=wrong_result,
                ),
                causation_command_id=wrong_command_id,
                priority=True,
            ).is_ok
        )
        mass.stage4.pump()
        self.assertIs(
            pending,
            coordinator.state.pending_command(wrong_command_id),
        )
        self.assertEqual(service_event_count, len(service_sink.events()))
        self.assertEqual(
            MacFailureReason.COMMAND_RESULT_UNMATCHED.value,
            mass.stage4.health.entries()[-1][2].get("mac_reason"),
        )

        ids = DeterministicIdSource(159)
        transfer_id = ids.new_transfer_id()
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        transfer = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_SHORT_DATA,
            phase=TransferPhase.BUILDING,
            route=route,
            ack_required=False,
            presentation_size=8,
            public_state=TransferState.QUEUED,
        )
        failed_command_id = "original-dependency-code"
        failure_pending = PendingCommandRecord(
            command_id=failed_command_id,
            command_class=PendingCommandClass.NON_CLEANUP_NON_RX,
            payload_kind="InvokeLinkCommand",
            request_key=("LINK", "LinkPacketEncodeRequest"),
        )
        failure_state = coordinator.state.with_transfer(transfer).evolve(
            pending_commands=(failure_pending,)
        )
        original = internal_error(
            Layer.LINK,
            ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
            ErrorCategory.PROTOCOL,
            "injected original dependency failure",
            correlation=Correlation(transfer_id=transfer_id),
        ).error
        commands = []
        failed = coordinator._on_dependency_failed(
            failure_state,
            SimpleNamespace(
                event_id="dependency-failed",
                causation_command_id=failed_command_id,
                correlation=Correlation(transfer_id=transfer_id),
                payload=SimpleNamespace(
                    command_id=failed_command_id, error=original
                ),
            ),
            commands,
        )
        self.assertIs(TransferPhase.TERMINAL, failed.transfer(transfer_id).phase)
        published = [
            command.payload
            for command in commands
            if isinstance(command.payload, PublishTransferStateCommand)
        ]
        self.assertEqual(1, len(published))
        self.assertIs(original, published[0].error)

    def test_TC_MAC_015_timer_schedule_is_idempotent_but_reuse_cannot_overwrite(self):
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        support = endpoints[0].stage4
        deadline = support._endpoint._time().now_monotonic()

        def command(command_id, purpose=TimerPurpose.FRAME_BOUNDARY):
            return MacCommandEnvelope(
                command_id=command_id,
                causation_event_id="timer-contract",
                correlation=Correlation(),
                payload=ScheduleWakeupCommand(
                    timer_id="same-timer",
                    deadline_monotonic=deadline,
                    purpose=purpose,
                ),
            )

        support._execute(command("first"))
        first_result = support.ingress.pop_next()
        self.assertIsInstance(first_result.payload, CommandCompletedEvent)
        self.assertEqual(
            (deadline.total_nanoseconds(), TimerPurpose.FRAME_BOUNDARY),
            support._timers["same-timer"],
        )

        support._execute(command("duplicate"))
        duplicate_result = support.ingress.pop_next()
        self.assertIsInstance(duplicate_result.payload, CommandCompletedEvent)
        self.assertEqual(1, len(support._timers))

        support._execute(command("conflict", TimerPurpose.LEASE_EXPIRY))
        conflict_result = support.ingress.pop_next()
        self.assertEqual("conflict", conflict_result.payload.command_id)
        self.assertEqual(
            MacFailureReason.EVENT_INVALID.value,
            conflict_result.payload.error.details.get("mac_reason"),
        )
        self.assertEqual(
            (deadline.total_nanoseconds(), TimerPurpose.FRAME_BOUNDARY),
            support._timers["same-timer"],
        )

    def test_TC_MAC_015_relay_analysis_cleanup_ownership_and_failure_evidence(self):
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        support = roc.stage4
        coordinator = support.mac.coordinator
        first_hop = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(StationId(333333333))),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        second_hop = RouteAddresses(
            original_source=first_hop.original_source,
            original_destination=first_hop.original_destination,
            node_source=NodeSource(ROC_STATION_ID),
            node_destination=NodeDestination(Addressed(StationId(333333333))),
        )

        def prepare_relay(seed, *, commit_failure=False):
            transfer_id = DeterministicIdSource(seed).new_transfer_id()
            request = LinkAnalyzeRequest(
                transfer_id=transfer_id,
                route=second_hop,
                ack_required=True,
                presentation_data=b"R" * 1000,
            )
            command = coordinator._new_command(
                "relay-analysis",
                Correlation(transfer_id=transfer_id),
                InvokeLinkCommand(request=request),
            )
            relay = RelayRecord(
                relay_transfer_id=transfer_id,
                original_route=first_hop,
                second_hop_route=second_hop,
                presentation_data=request.presentation_data,
                analyze_command_id=command.command_id,
            )
            state = support.mac.state.with_relay_record(relay)
            registered = coordinator._register_pending(state, [command])
            self.assertTrue(registered.is_ok, getattr(registered, "error", None))
            support.mac.repository.commit(registered.value)
            if commit_failure:
                support.mac.repository.set_commit_hook(
                    lambda _state: (_ for _ in ()).throw(RuntimeError("injected commit failure"))
                )
            support._execute_link(command, command.payload)
            self.assertIn(command.command_id, support._relay_analysis_cleanup)
            return transfer_id, command

        admitted_id, admitted_command = prepare_relay(156)
        support.pump()
        admitted = support.mac.state.relay_record(admitted_id.value)
        self.assertTrue(admitted.admitted)
        self.assertNotIn(admitted_command.command_id, support._relay_analysis_cleanup)

        failed_id, failed_command = prepare_relay(157, commit_failure=True)
        support.pump()
        support.mac.repository.set_commit_hook(None)
        self.assertFalse(support.mac.state.relay_record(failed_id.value).admitted)
        self.assertNotIn(failed_command.command_id, support._relay_analysis_cleanup)
        self.assertTrue(any(
            reason == MacReasonCode.STATE_COMMIT_FAILED
            for _code, reason, _details in support.health.entries()
        ))

        cleanup_id = DeterministicIdSource(158).new_transfer_id()
        analyzed = roc._link.analyze_transfer(
            LinkAnalyzeRequest(
                transfer_id=cleanup_id,
                route=second_hop,
                ack_required=True,
                presentation_data=b"cleanup-failure",
            )
        )
        self.assertTrue(analyzed.is_ok, getattr(analyzed, "error", None))
        cleanup_command_id = "relay-cleanup-failure"
        cleanup_request = LinkTransferControlRequest(
            kind=TransferControlKind.FAIL,
            transfer_id=cleanup_id,
            session_handle=None,
            packet_progress=(),
            correlation=Correlation(transfer_id=cleanup_id),
        )
        support._relay_analysis_cleanup[cleanup_command_id] = cleanup_request
        original_link = roc._link

        class _FailingCleanup:
            def control_transfer(self, _request):
                return Err(internal_error(
                    Layer.LINK,
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                    ErrorCategory.INTERNAL,
                    "injected relay cleanup failure",
                ).error)

        roc._link = _FailingCleanup()
        try:
            self.assertFalse(support._cleanup_relay_analysis(cleanup_command_id))
        finally:
            roc._link = original_link
        self.assertIn(cleanup_command_id, support._relay_analysis_cleanup)
        self.assertTrue(any(
            reason == MacFailureReason.DEPENDENCY_FAILED.value
            for _code, reason, _details in support.health.entries()
        ))
        self.assertTrue(support._cleanup_relay_analysis(cleanup_command_id))

    def test_TC_MAC_015_saturated_stop_drains_in_bounded_waves_and_late_events_cannot_revive(self):
        _clock, endpoints = build_stage4_software_group(with_type0=False)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        support = mass.stage4
        transfer_id = DeterministicIdSource(160).new_transfer_id()
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        transfer = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_SHORT_DATA,
            phase=TransferPhase.BUILDING,
            route=route,
            ack_required=False,
            presentation_size=32,
            public_state=TransferState.QUEUED,
        )
        timers = tuple(
            TimerRecord(
                timer_id=f"saturated-timer-{index}",
                purpose=TimerPurpose.LEASE_EXPIRY,
                deadline_monotonic_nanoseconds=10_000_000_000 + index,
            )
            for index in range(Stage4Limits.ACTIVE_TIMERS)
        )
        stale_cleanup = tuple(
            PendingCommandRecord(
                command_id=f"saturated-cleanup-{index}",
                command_class=PendingCommandClass.CLEANUP,
                payload_kind="CancelWakeupCommand",
                request_key=("TIMER_CANCEL", f"old-timer-{index}"),
            )
            for index in range(Stage4Limits.PENDING_DEPENDENCY_CLEANUP_RESERVE)
        )
        saturated = support.mac.state.evolve(
            lifecycle=EndpointLifecycle.RUNNING,
            transfers=(transfer,),
            timers=timers,
            pending_commands=stale_cleanup,
        )
        support.mac.repository.commit(saturated)
        support._timers.update(
            {
                timer.timer_id: (
                    timer.deadline_monotonic_nanoseconds,
                    timer.purpose,
                )
                for timer in timers
            }
        )

        support.begin_stop("saturated-stop")
        draining = support.mac.state
        self.assertIs(EndpointLifecycle.DRAINING, draining.lifecycle)
        self.assertEqual(Stage4Limits.ACTIVE_TIMERS, len(draining.timers))
        self.assertIsNotNone(draining.stop_drain_timer_id)
        self.assertIn("stop-drain", support._timers)
        self.assertNotIn(timers[-1].timer_id, support._timers)
        self.assertIsNot(TransferPhase.TERMINAL, draining.transfer(transfer_id).phase)

        # 이미 수락한 결과는 STOP_DRAIN 전까지 정상적으로 소비된다.
        self.assertTrue(
            support.enqueue(
                CommandCompletedEvent(command_id=stale_cleanup[0].command_id),
                causation_command_id=stale_cleanup[0].command_id,
                priority=True,
            ).is_ok
        )
        support.pump()
        self.assertIsNone(
            support.mac.state.pending_command(stale_cleanup[0].command_id)
        )

        support.finish_stop()
        stopped = support.mac.state
        self.assertIs(EndpointLifecycle.STOPPED, stopped.lifecycle)
        self.assertIs(TransferPhase.TERMINAL, stopped.transfer(transfer_id).phase)
        self.assertEqual(TransferState.FAILED, stopped.transfer(transfer_id).public_state)
        self.assertEqual((), stopped.timers)
        self.assertEqual((), stopped.pending_commands)
        self.assertEqual((), stopped.stop_cleanup_commands)
        self.assertEqual(0, len(support._timers))
        self.assertEqual((0, 0, 0), (
            support.outbox.occupancy(),
            support.outbox.reserved_count(),
            support.outbox.ready_count(),
        ))
        self.assertTrue(
            {item.command_id for item in stale_cleanup}.issubset(
                set(stopped.recent_terminal_ids)
            )
        )

        service_sink = mass.dependencies.service_event_sink
        service_event_count = len(service_sink.events())
        self.assertTrue(
            support.enqueue(
                CommandCompletedEvent(command_id=stale_cleanup[1].command_id),
                causation_command_id=stale_cleanup[1].command_id,
                priority=True,
            ).is_ok
        )
        support.pump()
        self.assertIs(stopped, support.mac.state)
        self.assertEqual(service_event_count, len(service_sink.events()))
        self.assertEqual(
            MacFailureReason.COMMAND_RESULT_UNMATCHED.value,
            support.health.entries()[-1][2].get("mac_reason"),
        )

        first_timer = timers[0]
        self.assertTrue(
            support.enqueue(
                WakeupFiredEvent(
                    timer_id=first_timer.timer_id,
                    purpose=first_timer.purpose,
                    scheduled_for_monotonic=mass._time().now_monotonic(),
                ),
                priority=True,
            ).is_ok
        )
        support.pump()
        self.assertIs(stopped, support.mac.state)
        self.assertIs(TransferPhase.TERMINAL, support.mac.state.transfer(transfer_id).phase)

    def test_TC_MAC_015_public_stop_drains_then_restarts_with_fresh_stage4_state(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
        )
        start_stage4_group(clock, endpoints)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )

        mass.stage4.begin_stop("test-drain")
        self.assertIs(EndpointLifecycle.DRAINING, mass.stage4.mac.state.lifecycle)
        self.assertTrue(mass.stage4.outbox.draining)
        rejected = mass.submit(
            SubmitTransferRequest(
                destination=Addressed(ROC_STATION_ID),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"blocked-while-draining",
                ack_required=False,
            )
        )
        self.assertTrue(rejected.is_ok)
        self.assertIs(
            TransferState.FAILED,
            mass._transfers[rejected.value.transfer_id.value].state,
        )
        mass.stage4.finish_stop()
        self.assertIs(EndpointLifecycle.STOPPED, mass.stage4.mac.state.lifecycle)

        stopped = mass.stop(ServiceStopRequest(reason="test-restart"))
        self.assertTrue(stopped.is_ok, getattr(stopped, "error", None))
        self.assertIs(EndpointLifecycle.INITIAL, mass.stage4.mac.state.lifecycle)
        self.assertIs(mass.stage4.mac, mass._mac)
        self.assertEqual((0, 0), (mass.stage4.outbox.occupancy(), mass.stage4.ingress.depth()))

        retry_command = MacCommandEnvelope(
            command_id="consumer-retry",
            causation_event_id="consumer-failure",
            correlation=Correlation(),
            payload=CancelWakeupCommand("consumer-retry-timer"),
        )
        mass.stage4._retry_consumer_command(retry_command)
        self.assertEqual(1, mass.stage4.outbox.ready_count())
        mass.stage4.outbox.clear()

        configuration = mass.dependencies.configuration.endpoint
        restarted = mass.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id,
                role=configuration.role,
            )
        )
        self.assertTrue(restarted.is_ok, getattr(restarted, "error", None))
        self.assertTrue(mass.stage4.mac.begin_running().is_ok)
        mass.stage4.pump()
        self.assertIs(EndpointLifecycle.RUNNING, mass.stage4.mac.state.lifecycle)


if __name__ == "__main__":
    unittest.main()
