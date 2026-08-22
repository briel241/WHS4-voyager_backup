"""TC-MAC-012: ACK 없는/필요 Short Data와 delivery ledger."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
import uuid

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
    PacketId,
    PayloadFormat,
    SequentialId,
    TransferId,
)
from vdeter.common.contracts.mac_execution import AckSlotFunction, MacReasonCode
from vdeter.common.contracts.message import AcknowledgeNackBody, ShortDataAckBody
from vdeter.mac import ack
from vdeter.mac.ack import retention_frames_for
from vdeter.mac.models import (
    AckPendingState,
    RxDeliveryProcedure,
    TransferContext,
    TransferPhase,
    TransferRole,
)
from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL
from vdeter.mac.tdma import frame_ordinal_at_nanoseconds


PAYLOAD_FORMAT = PayloadFormat("test.stage0.opaque")


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage4-test:lower-bound"


class Stage4ShortDataTest(unittest.TestCase):
    """TC-MAC-012의 Software Radio 정상 경로와 유한 replay 보존."""

    def test_TC_MAC_012_no_ack_and_ack_required_short_data_complete_after_delivery(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        mass = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID)
        roc = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == ROC_STATION_ID)

        for ack_required, payload in ((False, b"no-ack"), (True, b"with-ack")):
            accepted = mass.submit(SubmitTransferRequest(
                destination=Addressed(ROC_STATION_ID),
                payload_format=PAYLOAD_FORMAT,
                payload=payload,
                ack_required=ack_required,
            ))
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            mass.stage4.pump()
            drive_stage4_group(clock, endpoints, 60)
            status = mass.status(TransferStatusRequest(accepted.value.transfer_id))
            self.assertTrue(status.is_ok)
            self.assertIs(TransferState.COMPLETED, status.value.state)

        received = [
            event.received_data.payload
            for event in roc.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertIn(b"no-ack", received)
        self.assertIn(b"with-ack", received)
        self.assertTrue(
            any(
                record.procedure is RxDeliveryProcedure.SHORT_DATA_92
                for record in roc.stage4.mac.state.delivery_ledger
            )
        )

    def test_TC_MAC_012_type92_retry_retention_is_209_140_71_1_frames(self):
        self.assertEqual(
            [209, 140, 71, 1],
            [
                retention_frames_for(RxDeliveryProcedure.SHORT_DATA_92, number)
                for number in range(4)
            ],
        )
        self.assertEqual(3, retention_frames_for(RxDeliveryProcedure.SHORT_DATA_93, None))

    def test_TC_MAC_012_first_ack_loss_retries_without_redelivery_and_reack_completes(self):
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
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        medium = mass.radio._medium
        with medium._lock:
            blocked_link = next(
                link
                for link in medium._links
                if link.source_endpoint_id == "stage4-roc"
                and link.target_endpoint_id == "stage4-mass-a"
                and link.channel == UPPER_CHANNEL
            )
            medium._links.remove(blocked_link)

        payload = b"ack-loss-replay"
        accepted = mass.submit(
            SubmitTransferRequest(
                destination=Addressed(ROC_STATION_ID),
                payload_format=PAYLOAD_FORMAT,
                payload=payload,
                ack_required=True,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        mass.stage4.pump()
        for _ in range(280):
            drive_stage4_group(clock, endpoints, 1)
            context = mass.stage4.mac.state.transfer(accepted.value.transfer_id)
            if context is not None and context.retransmission_number >= 1:
                break
        self.assertGreaterEqual(
            context.retransmission_number,
            1,
            (
                context,
                mass.stage4.mac.state.timers,
                mass.stage4.health.entries(),
                roc.stage4.mac.state.delivery_ledger,
            ),
        )
        received_before_reack = [
            event.received_data.payload
            for event in roc.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertEqual([payload], received_before_reack)

        with medium._lock:
            medium._links.append(blocked_link)
        for _ in range(160):
            drive_stage4_group(clock, endpoints, 1)
            status = mass.status(TransferStatusRequest(accepted.value.transfer_id))
            if status.is_ok and status.value.state is TransferState.COMPLETED:
                break
        self.assertIs(TransferState.COMPLETED, status.value.state)
        received_after_reack = [
            event.received_data.payload
            for event in roc.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertEqual([payload], received_after_reack)
        self.assertTrue(
            any(
                record.highest_retransmission_number == 1
                for record in roc.stage4.mac.state.delivery_ledger
                if record.procedure is RxDeliveryProcedure.SHORT_DATA_92
            )
        )

    def test_TC_MAC_012_ack_required_short_data_fifo_waits_for_active_session(self):
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
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        active_id = TransferId(str(uuid.uuid4()))
        queued_id = TransferId(str(uuid.uuid4()))
        active = TransferContext(
            transfer_id=active_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.TRANSMITTING,
            route=route,
            ack_required=True,
            presentation_size=100,
            public_state=TransferState.TRANSMITTING,
        )
        queued = TransferContext(
            transfer_id=queued_id,
            role=TransferRole.LOCAL_SHORT_DATA,
            phase=TransferPhase.QUEUED,
            route=route,
            ack_required=True,
            presentation_size=8,
            public_state=TransferState.QUEUED,
            link_id=LinkId(11),
            physical_channel_name="LOWER",
        )
        state = coordinator.state.with_transfer(active).with_transfer(queued).evolve(
            local_fifo=(queued_id.value,)
        )
        blocked_commands = []
        blocked = coordinator._drive_local_work(state, "blocked", blocked_commands)
        self.assertEqual((queued_id.value,), blocked.local_fifo)
        self.assertEqual([], blocked_commands)

        released_commands = []
        released = coordinator._drive_local_work(
            state.with_transfer(
                replace(
                    active,
                    phase=TransferPhase.TERMINAL,
                    public_state=TransferState.COMPLETED,
                )
            ),
            "released",
            released_commands,
        )
        self.assertEqual((), released.local_fifo)
        self.assertIs(TransferPhase.BUILDING, released.transfer(queued_id).phase)
        self.assertTrue(released_commands or released.tx_intents)

    def test_TC_MAC_012_ack_replay_is_bounded_and_higher_retry_does_not_extend_expiry(self):
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
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
            node_source=NodeSource(MASS_A_STATION_ID),
            node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
        )
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())
        key = ack.delivery_key(
            RxDeliveryProcedure.SHORT_DATA_92,
            MASS_A_STATION_ID,
            Addressed(ROC_STATION_ID),
            SequentialId(9),
        )
        pending = ack.new_pending_record(
            key=key,
            procedure=RxDeliveryProcedure.SHORT_DATA_92,
            payload_length=4,
            digest=ack.payload_digest(b"data"),
            first_received_frame_ordinal=frame,
            highest_retransmission_number=0,
            pending_delivery_key=None,
        )
        live = ack.promoted_live_record(
            pending,
            completion_frame_ordinal=frame,
            ack_state=AckPendingState.ACK_PENDING,
            replay_context=None,
        )
        ack_body = AcknowledgeNackBody(
            source_id=ROC_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            session_id=0,
            ack_nack_masks=(0, 0, 0),
            cqi=0,
            acm_or_edn=0,
            power_setting=0,
        )
        replay = ack.build_short_data_replay_context(
            route=route,
            link_id=LinkId(11),
            physical_channel=UPPER_CHANNEL,
            slot_function=AckSlotFunction.ASC,
            ack_body=ack_body,
            resource_last_frame_ordinal=live.expires_after_frame,
        )
        live = replace(live, replay_context=replay)
        sent = replace(live, ack_state=AckPendingState.ACK_SENT)
        duplicate_actions = []
        duplicate_state = coordinator._receive_short_data(
            coordinator.state.with_ledger_record(sent),
            message=SimpleNamespace(
                route=route,
                sequential_id=SequentialId(9),
                presentation_data=b"data",
                message_type=92,
            ),
            body=ShortDataAckBody(
                MASS_A_STATION_ID,
                0,
                Addressed(ROC_STATION_ID),
                1,
                b"segment",
            ),
            decoded=SimpleNamespace(
                packet_id=PacketId(str(uuid.uuid4())),
                received_at=clock.now_utc(),
            ),
            channel=LOWER_CHANNEL,
            delivery_index=0,
            causation_event_id="duplicate",
            commands=[],
            actions=duplicate_actions,
        )
        observed_retry = duplicate_state.ledger_record(key)
        self.assertEqual(live.expires_after_frame, observed_retry.expires_after_frame)
        self.assertIs(live.replay_context, observed_retry.replay_context)
        self.assertIs(AckPendingState.ACK_PENDING, observed_retry.ack_state)
        self.assertEqual([], duplicate_actions)

        commands = []
        planned = coordinator._plan_pending_acks(
            duplicate_state,
            "ack-replay",
            commands,
        )
        record = next(item for item in planned.delivery_ledger if item.key == key)
        self.assertIsNotNone(record.replay_command_id)
        self.assertEqual(frame, record.last_replay_scheduled_frame)
        self.assertTrue(commands or planned.tx_intents)
        suppressed = replace(record, replay_command_id=None)
        repeated_commands = []
        repeated = coordinator._plan_pending_acks(
            planned.with_ledger_record(suppressed),
            "same-frame",
            repeated_commands,
        )
        self.assertEqual([], repeated_commands)
        self.assertEqual(record.last_replay_scheduled_frame, repeated.delivery_ledger[-1].last_replay_scheduled_frame)

        # 유효 resource horizon이 끝난 replay는 한 번 오류 evidence를 남기고
        # ACK_SENT로 종결하여 clock tick마다 같은 오류를 반복하지 않는다.
        expired = replace(
            repeated.delivery_ledger[-1],
            ack_state=AckPendingState.ACK_PENDING,
            replay_command_id=None,
            last_replay_scheduled_frame=None,
            expires_after_frame=frame + 10,
            replay_context=replace(
                replay, resource_last_frame_ordinal=frame - 1
            ),
        )
        roc.stage4.mac.repository.commit(repeated.with_ledger_record(expired))
        roc.stage4.health.clear()
        roc.stage4.advance_clock()
        self.assertTrue(
            any(
                reason_code is MacReasonCode.ACK_REPLAY_RESOURCE_EXPIRED
                for _, reason_code, _ in roc.stage4.health.entries()
            )
        )
        expired_after = roc.stage4.mac.state.ledger_record(key)
        self.assertIs(AckPendingState.ACK_SENT, expired_after.ack_state)
        evidence_count = len(roc.stage4.health.entries())
        roc.stage4.advance_clock()
        self.assertEqual(evidence_count, len(roc.stage4.health.entries()))

        # 32개의 아직 유효한 record는 축출하지 않고 신규 delivery를 구조화 오류로 거부한다.
        full_ledger = tuple(
            replace(
                live,
                key=index.to_bytes(2, "big"),
                expires_after_frame=frame + 10,
            )
            for index in range(32)
        )
        capacity_errors = []
        capacity_actions = []
        capacity_state = coordinator._receive_short_data(
            coordinator.state.evolve(delivery_ledger=full_ledger),
            message=SimpleNamespace(
                route=route,
                sequential_id=SequentialId(10),
                presentation_data=b"new-data",
                message_type=92,
            ),
            body=ShortDataAckBody(
                MASS_A_STATION_ID,
                0,
                Addressed(ROC_STATION_ID),
                0,
                b"new-data",
            ),
            decoded=SimpleNamespace(
                packet_id=PacketId(str(uuid.uuid4())),
                received_at=clock.now_utc(),
            ),
            channel=LOWER_CHANNEL,
            delivery_index=0,
            causation_event_id="capacity",
            commands=[],
            actions=capacity_actions,
            errors=capacity_errors,
        )
        self.assertEqual(32, len(capacity_state.delivery_ledger))
        self.assertEqual([], capacity_actions)
        self.assertEqual(1, len(capacity_errors))
        self.assertIs(
            MacReasonCode.RX_DEDUP_CAPACITY,
            capacity_errors[0].error.details.get("reason_code"),
        )

        for unsupported_number in (4, 254, 255):
            unsupported_errors = []
            unsupported_actions = []
            unchanged = coordinator._receive_short_data(
                coordinator.state.evolve(delivery_ledger=()),
                message=SimpleNamespace(
                    route=route,
                    sequential_id=SequentialId(11),
                    presentation_data=b"unsupported",
                    message_type=92,
                ),
                body=ShortDataAckBody(
                    MASS_A_STATION_ID,
                    0,
                    Addressed(ROC_STATION_ID),
                    unsupported_number,
                    b"unsupported",
                ),
                decoded=SimpleNamespace(
                    packet_id=PacketId(str(uuid.uuid4())),
                    received_at=clock.now_utc(),
                ),
                channel=LOWER_CHANNEL,
                delivery_index=0,
                causation_event_id="unsupported-retry",
                commands=[],
                actions=unsupported_actions,
                errors=unsupported_errors,
            )
            self.assertEqual((), unchanged.delivery_ledger)
            self.assertEqual([], unsupported_actions)
            self.assertIs(
                MacReasonCode.UNSUPPORTED_RETRANSMISSION_NUMBER,
                unsupported_errors[0].error.details.get("reason_code"),
            )

        conflict_errors = []
        conflict_actions = []
        conflict_state = coordinator._receive_short_data(
            coordinator.state.evolve(delivery_ledger=(sent,)),
            message=SimpleNamespace(
                route=route,
                sequential_id=SequentialId(9),
                presentation_data=b"different",
                message_type=92,
            ),
            body=ShortDataAckBody(
                MASS_A_STATION_ID,
                0,
                Addressed(ROC_STATION_ID),
                1,
                b"different",
            ),
            decoded=SimpleNamespace(
                packet_id=PacketId(str(uuid.uuid4())),
                received_at=clock.now_utc(),
            ),
            channel=LOWER_CHANNEL,
            delivery_index=0,
            causation_event_id="sequential-conflict",
            commands=[],
            actions=conflict_actions,
            errors=conflict_errors,
        )
        self.assertEqual((sent,), conflict_state.delivery_ledger)
        self.assertEqual([], conflict_actions)
        self.assertIs(
            MacReasonCode.RX_SEQUENTIAL_CONFLICT,
            conflict_errors[0].error.details.get("reason_code"),
        )


if __name__ == "__main__":
    unittest.main()
