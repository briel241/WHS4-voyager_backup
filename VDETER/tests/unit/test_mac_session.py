"""TC-MAC-011: Data Session fragmentation, renewal, ACK, and completion."""

from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
    build_stage4_software_group,
    drive_stage4_group,
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
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.identifiers import (
    LinkId,
    PayloadFormat,
    SessionHandle,
    TransferId,
)
from vdeter.common.contracts.message import AcknowledgeNackBody
from vdeter.common.contracts.mac_execution import (
    InvokeLinkCommand,
    MacReasonCode,
    PublishTransmissionPlanCommand,
)
from vdeter.common.errors import (
    Correlation,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
)
from vdeter.common.contracts.link import (
    TransferControlKind,
    TransferPacketOutcome,
)
from vdeter.mac import ack
from vdeter.mac.ack import SessionFrameLedgerEntry
from vdeter.mac.models import (
    AckPendingState,
    LcAllocation,
    LcState,
    PendingCommandClass,
    PendingCommandRecord,
    RxFrameReception,
    RxDeliveryProcedure,
    RxSessionContext,
    TransferContext,
    TransferPhase,
    TransferRole,
)
from vdeter.mac.resources import Direction, duplex_pair
from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL
from vdeter.mac.tdma import FRAME_NANOSECONDS, frame_ordinal_at_nanoseconds
from vdeter.mac.transfers import plan_session_frames


PAYLOAD_FORMAT = PayloadFormat("test.stage0.opaque")


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage4-test:lower-bound"


class Stage4DataSessionTest(unittest.TestCase):
    def test_TC_MAC_011_frame_planning_uses_1_to_14_fragments_and_renewal(self):
        self.assertEqual((1,), tuple(p.frame_fragment_count for p in plan_session_frames(
            total_fragments=1, fragments_per_frame=14
        )))
        plans = plan_session_frames(total_fragments=29, fragments_per_frame=14)
        self.assertEqual((14, 14, 1), tuple(p.frame_fragment_count for p in plans))
        self.assertEqual((1, 1, 0), tuple(p.continue_resource_usage for p in plans))
        self.assertEqual((0, 1, 2), tuple(p.session_frame_index for p in plans))

    def test_TC_MAC_011_three_consecutive_ack_absences_end_the_session(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        transfer_id = TransferId(str(uuid.uuid4()))
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        context = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.WAITING_ACK,
            route=route,
            ack_required=True,
            presentation_size=8,
            public_state=TransferState.WAITING_ACK,
            link_id=LinkId(11),
            frame_ledger=(
                SessionFrameLedgerEntry(
                    session_frame_index=0,
                    frame_ordinal=1,
                    frame_fragment_count=1,
                    packet_ids=(str(uuid.uuid4()),),
                    retransmission_counts=(0,),
                ),
            ),
            total_fragments=1,
            next_fragment_index=1,
        )
        state = coordinator.state.with_transfer(context)
        for index in range(2):
            commands = []
            state = coordinator._on_data_ack_timeout(
                state, f"absence-{index}", commands
            )
            self.assertIsNot(TransferPhase.TERMINAL, state.transfer(transfer_id).phase)
        commands = []
        state = coordinator._on_data_ack_timeout(state, "absence-2", commands)
        terminal = state.transfer(transfer_id)
        self.assertIs(TransferPhase.TERMINAL, terminal.phase)
        self.assertIs(TransferState.FAILED, terminal.public_state)
        self.assertTrue(commands)

    def test_TC_MAC_011_nack_retries_only_selected_fragment_and_exhausts_at_three(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        pair = duplex_pair(
            coordinator.active_slot_map(coordinator.state),
            Direction.SHIP_TO_ROC,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        self.assertIsNotNone(pair)
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())
        allocation = LcAllocation(
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            state=LcState.ACTIVE,
            activation_frame_ordinal=frame,
            last_valid_frame_ordinal=frame + 3,
            tdma_channel=pair.tdma_channel,
            issued_by_local=False,
        )
        transfer_id = TransferId(str(uuid.uuid4()))
        first_packet, second_packet = str(uuid.uuid4()), str(uuid.uuid4())
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        context = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.WAITING_ACK,
            route=route,
            ack_required=True,
            presentation_size=16,
            public_state=TransferState.WAITING_ACK,
            session_handle=SessionHandle(str(uuid.uuid4())),
            link_id=LinkId(11),
            physical_channel_name=LOWER_CHANNEL.name,
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            frame_ledger=(
                SessionFrameLedgerEntry(
                    session_frame_index=0,
                    frame_ordinal=frame,
                    frame_fragment_count=2,
                    packet_ids=(first_packet, second_packet),
                    retransmission_counts=(0, 0),
                    fragment_confirmed=(False, False),
                ),
            ),
            total_fragments=2,
            next_fragment_index=2,
            session_frame_index=1,
        )
        state = coordinator.state.evolve(lc_allocations=(allocation,)).with_transfer(context)
        nack_second = AcknowledgeNackBody(
            source_id=ROC_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            session_id=0,
            ack_nack_masks=(0, 0, 0b10),
            cqi=0,
            acm_or_edn=0,
            power_setting=0,
        )

        absence_commands = []
        state = coordinator._on_data_ack_timeout(
            state, "absence-before-late-nack", absence_commands
        )
        self.assertEqual(1, state.transfer(transfer_id).absent_ack_frames)
        self.assertEqual(1, len(state.transfer(transfer_id).frame_ledger))

        for retry_count in range(1, 4):
            commands = []
            state = coordinator._apply_session_ack(
                state,
                state.transfer(transfer_id),
                nack_second,
                f"nack-{retry_count}",
                commands,
            )
            updated = state.transfer(transfer_id)
            self.assertEqual(0, updated.absent_ack_frames)
            entry = updated.frame_ledger[0]
            self.assertEqual((True, False), entry.confirmed_positions)
            self.assertEqual((0, retry_count), entry.retransmission_counts)
            self.assertEqual(((0, 2),), updated.retry_fragment_keys)
            self.assertTrue(commands)
            progress = next(
                command.payload.request
                for command in commands
                if isinstance(command.payload, InvokeLinkCommand)
                and command.payload.request.kind is TransferControlKind.PROGRESS
            )
            if retry_count == 1:
                self.assertEqual(
                    (TransferPacketOutcome.SUCCESS, TransferPacketOutcome.DISCARDED),
                    tuple(item.outcome for item in progress.packet_progress),
                )
            else:
                self.assertEqual(
                    (TransferPacketOutcome.DISCARDED,),
                    tuple(item.outcome for item in progress.packet_progress),
                )
            retry_requests = [
                intent.build_request
                for intent in state.tx_intents
                if intent.build_request is not None
                and intent.build_request.session_frame_index == 0
            ] + [
                command.payload.request
                for command in commands
                if isinstance(command.payload, PublishTransmissionPlanCommand)
                and command.payload.request.session_frame_index == 0
            ]
            self.assertEqual(2, retry_requests[-1].fragment_position_in_frame)
            replacement_packet = str(uuid.uuid4())
            replacement_entry = SessionFrameLedgerEntry(
                session_frame_index=entry.session_frame_index,
                frame_ordinal=entry.frame_ordinal,
                frame_fragment_count=entry.frame_fragment_count,
                packet_ids=(entry.packet_ids[0], replacement_packet),
                retransmission_counts=entry.retransmission_counts,
                fragment_confirmed=entry.confirmed_positions,
            )
            state = state.with_transfer(
                coordinator._advanced_context(
                    updated,
                    phase=TransferPhase.WAITING_ACK,
                    frame_ledger=(replacement_entry,),
                    retry_fragment_keys=(),
                )
            )

        commands = []
        state = coordinator._apply_session_ack(
            state,
            state.transfer(transfer_id),
            nack_second,
            "nack-exhausted",
            commands,
        )
        self.assertIs(TransferPhase.TERMINAL, state.transfer(transfer_id).phase)
        self.assertIs(TransferState.FAILED, state.transfer(transfer_id).public_state)

    def test_TC_MAC_011_receiver_builds_nack_mask_and_maps_late_retry_to_original_frame(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        coordinator = roc.stage4.mac.coordinator
        pair = duplex_pair(
            coordinator.active_slot_map(coordinator.state),
            Direction.SHIP_TO_ROC,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())
        allocation = LcAllocation(
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            state=LcState.ACTIVE,
            activation_frame_ordinal=frame,
            last_valid_frame_ordinal=frame + 3,
            tdma_channel=pair.tdma_channel,
            issued_by_local=True,
        )
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        reception = RxFrameReception(
            session_frame_index=0,
            frame_fragment_count=3,
            received_positions=(1, 3),
            terminal_observed=True,
        )
        session = RxSessionContext(
            session_handle=SessionHandle(str(uuid.uuid4())),
            route=route,
            peer_station_id_value=MASS_A_STATION_ID.value,
            logical_channel_rx=pair.logical_channel_tx,
            opened=True,
            session_frame_index=1,
            last_frame_ordinal=frame,
            frame_receptions=(reception,),
        )
        commands = []
        planned = coordinator._schedule_data_session_ack(
            coordinator.state.evolve(lc_allocations=(allocation,)),
            session=session,
            observed_nanoseconds=clock.now_utc().total_nanoseconds(),
            causation_event_id="receiver-nack",
            commands=commands,
        )
        encode_requests = [
            command.payload.request
            for command in commands
            if isinstance(command.payload, InvokeLinkCommand)
        ] + [
            intent.encode_request
            for intent in planned.tx_intents
            if intent.encode_request is not None
        ]
        body = encode_requests[-1].messages[0].body
        self.assertEqual((0, 0, 0b010), body.ack_nack_masks)

        target, receptions = coordinator._record_rx_position(
            session,
            frame_fragment_count=3,
            position=2,
            terminal_observed=False,
        )
        self.assertEqual(0, target)
        self.assertTrue(receptions[0].complete)

    def test_TC_MAC_011_delivery_failure_removes_pending_record_and_replay_uses_ledger_horizon(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        coordinator = roc.stage4.mac.coordinator
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())
        delivery_id = "session-delivery-failure"
        ledger_key = b"data-session-delivery"
        pending = ack.new_pending_record(
            key=ledger_key,
            procedure=RxDeliveryProcedure.DATA_SESSION,
            payload_length=7,
            digest=ack.payload_digest(b"payload"),
            first_received_frame_ordinal=frame,
            highest_retransmission_number=None,
            pending_delivery_key=("SESSION_DELIVERY", delivery_id),
        )
        failed_state = coordinator.state.evolve(
            delivery_ledger=(pending,),
            delivery_pending=(
                (("SESSION_DELIVERY", delivery_id), ledger_key, None),
            ),
            pending_commands=(
                PendingCommandRecord(
                    command_id=delivery_id,
                    command_class=PendingCommandClass.NON_CLEANUP_NON_RX,
                    payload_kind="DeliverPresentationDataCommand",
                ),
            ),
        )
        failure = InternalProtocolError(
            layer=Layer.SERVICE,
            code=ErrorCode.MAC_TRANSFER_FAILED,
            category=ErrorCategory.INTERNAL,
            message="injected Data Session delivery failure",
        )
        after_failure = coordinator._on_dependency_failed(
            failed_state,
            SimpleNamespace(
                event_id="delivery-failed",
                causation_command_id=delivery_id,
                correlation=Correlation(),
                payload=SimpleNamespace(command_id=delivery_id, error=failure),
            ),
            [],
        )
        self.assertEqual((), after_failure.delivery_ledger)
        self.assertEqual((), after_failure.delivery_pending)
        self.assertIsNone(after_failure.pending_command(delivery_id))

        pair = duplex_pair(
            coordinator.active_slot_map(coordinator.state),
            Direction.SHIP_TO_ROC,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        replay_context = ack.build_data_session_replay_context(
            route=RouteAddresses(
                original_source=OriginalSource(ROC_STATION_ID),
                original_destination=OriginalDestination(
                    Addressed(MASS_A_STATION_ID)
                ),
            ),
            link_id=LinkId(11),
            physical_channel=pair.rx_physical_channel,
            logical_channel=pair.logical_channel_rx,
            ack_body=ack.build_ack_body(
                source_id=ROC_STATION_ID,
                destination=Addressed(MASS_A_STATION_ID),
            ),
            resource_last_frame_ordinal=frame + 10,
        )
        live = ack.promoted_live_record(
            pending,
            completion_frame_ordinal=frame,
            ack_state=AckPendingState.ACK_PENDING,
            replay_context=replay_context,
        )
        self.assertEqual(frame + 3, live.expires_after_frame)
        commands = []
        planned = coordinator._plan_pending_acks(
            coordinator.state.evolve(delivery_ledger=(live,)),
            "replay-within-ledger",
            commands,
            errors=[],
        )
        intents = [intent for intent in planned.tx_intents if intent.ledger_key == ledger_key]
        if intents:
            replay_frame = frame_ordinal_at_nanoseconds(
                intents[-1].transmit_at_nanoseconds
            )
        else:
            transmit_at = next(
                item[1]
                for item in planned.delivery_pending
                if item[0] == ("CTRL_TIME", commands[-1].command_id)
            )
            replay_frame = frame_ordinal_at_nanoseconds(
                transmit_at
            )
        self.assertLessEqual(replay_frame, live.expires_after_frame)

        clock.advance(FRAME_NANOSECONDS * 4)
        expired_errors = []
        expired_commands = []
        expired = coordinator._plan_pending_acks(
            coordinator.state.evolve(delivery_ledger=(live,)),
            "replay-after-ledger",
            expired_commands,
            errors=expired_errors,
        )
        self.assertEqual([], expired_commands)
        self.assertEqual(
            MacReasonCode.ACK_REPLAY_RESOURCE_EXPIRED,
            expired_errors[0].error.details.get("reason_code"),
        )
        self.assertIs(
            AckPendingState.ACK_SENT,
            expired.delivery_ledger[0].ack_state,
        )

    def test_TC_MAC_011_multi_frame_session_crosses_wire_and_final_ack_completes(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        mass = next(
            e for e in endpoints
            if e.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        roc = next(
            e for e in endpoints
            if e.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        payload = bytes(range(256)) * 3
        accepted = mass.submit(SubmitTransferRequest(
            destination=Addressed(ROC_STATION_ID),
            payload_format=PAYLOAD_FORMAT,
            payload=payload,
            ack_required=True,
        ))
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        mass.stage4.pump()
        for _ in range(100):
            drive_stage4_group(clock, endpoints, 1)
            current_status = mass.status(
                TransferStatusRequest(accepted.value.transfer_id)
            )
            if current_status.value.state is TransferState.COMPLETED:
                break

        status = mass.status(TransferStatusRequest(accepted.value.transfer_id))
        context = mass.stage4.mac.state.transfer(accepted.value.transfer_id)
        self.assertIs(
            TransferState.COMPLETED,
            status.value.state,
            context,
        )
        self.assertGreaterEqual(context.session_frame_index, 2)
        received = [
            event.received_data.payload
            for event in roc.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertEqual([payload], received)
        self.assertTrue(any(
            record.procedure is RxDeliveryProcedure.DATA_SESSION
            for record in roc.stage4.mac.state.delivery_ledger
        ))


if __name__ == "__main__":
    unittest.main()
