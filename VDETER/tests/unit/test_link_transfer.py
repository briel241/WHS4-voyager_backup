"""TC-LINK-019/023/034: transmit context, progress, and tombstones."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import (
    Addressed,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkTransferControlRequest,
    TransferControlKind,
    TransferKind,
    TransferPacketOutcome,
    TransferPacketProgress,
)
from vdeter.common.contracts.mac import TransmissionPlan, TransmissionProcedure
from vdeter.common.errors import Correlation, ErrorCode
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PhysicalChannel,
    SessionHandle,
    StationId,
    TransferId,
)
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.transfer import LinkTransferManager, MAX_TERMINAL_TOMBSTONES
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


ROUTE = RouteAddresses(
    OriginalSource(StationId(0x01020304)),
    OriginalDestination(Addressed(StationId(0x05060708))),
)
CHANNEL = PhysicalChannel("test-data")
TRANSMIT_AT = TimePoint(1, 0, ClockDomain.UTC, "test.utc")


class LinkTransferManagerTest(unittest.TestCase):
    def setUp(self):
        self.ids = DeterministicIdSource(19023034)
        messages = build_message_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        messages.freeze()
        profiles.freeze()
        self.manager = LinkTransferManager(self.ids, messages, profiles)

    def _analyze(
        self,
        payload: bytes,
        *,
        ack_required: bool = False,
        transfer_id: TransferId | None = None,
    ):
        transfer_id = transfer_id or self.ids.new_transfer_id()
        result = self.manager.analyze_transfer(
            LinkAnalyzeRequest(transfer_id, ROUTE, ack_required, payload)
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        return transfer_id, result.value

    def _plan(
        self,
        transfer_id: TransferId,
        procedure: TransmissionProcedure,
        link_id: int,
        session_handle: SessionHandle | None = None,
    ) -> TransmissionPlan:
        return TransmissionPlan(
            transfer_id=transfer_id,
            link_id=LinkId(link_id),
            channel=CHANNEL,
            transmit_at=TRANSMIT_AT,
            procedure=procedure,
            route=ROUTE,
            session_handle=session_handle,
        )

    def _build_short(self, transfer_id: TransferId, payload: bytes):
        return self.manager.build_packet(
            LinkBuildRequest(
                plan=self._plan(
                    transfer_id, TransmissionProcedure.SHORT_DATA, 11
                ),
                presentation_data=payload,
            )
        )

    def _build_data(
        self,
        transfer_id: TransferId,
        session_handle: SessionHandle,
        payload: bytes,
        *,
        link_id: int,
        frame: int,
        count: int,
        position: int,
    ):
        return self.manager.build_packet(
            LinkBuildRequest(
                plan=self._plan(
                    transfer_id,
                    TransmissionProcedure.DATA_SESSION,
                    link_id,
                    session_handle,
                ),
                presentation_data=payload,
                session_frame_index=frame,
                frame_fragment_count=count,
                fragment_position_in_frame=position,
            )
        )

    def _control(
        self,
        kind: TransferControlKind,
        *,
        transfer_id: TransferId | None = None,
        session_handle: SessionHandle | None = None,
        packet_progress: tuple[TransferPacketProgress, ...] = (),
    ):
        return self.manager.control_transfer(
            LinkTransferControlRequest(
                kind=kind,
                transfer_id=transfer_id,
                session_handle=session_handle,
                packet_progress=packet_progress,
                correlation=Correlation(
                    transfer_id=transfer_id,
                    session_handle=session_handle,
                ),
            )
        )

    def _progress(
        self,
        transfer_id: TransferId,
        *items: tuple[object, TransferPacketOutcome],
        session_handle: SessionHandle | None = None,
    ):
        return self._control(
            TransferControlKind.PROGRESS,
            transfer_id=transfer_id,
            session_handle=session_handle,
            packet_progress=tuple(
                TransferPacketProgress(packet.packet_id, outcome)
                for packet, outcome in items
            ),
        )

    def test_TC_LINK_019_terminal_controls_release_context_and_id(self):
        payload = b"complete"
        transfer_id, requirement = self._analyze(payload)
        self.assertIs(requirement.candidate, TransferKind.SHORT_DATA)
        self.assertIsNone(requirement.sequential_id)
        built = self._build_short(transfer_id, payload).value
        progress = self._progress(
            transfer_id, (built, TransferPacketOutcome.SUCCESS)
        ).value
        self.assertEqual(len(payload) + 4, progress.progress[0].committed_prefix_bytes)
        self.assertEqual(1, self.manager.active_sequential_id_count)

        completed = self._control(
            TransferControlKind.COMPLETE, transfer_id=transfer_id
        ).value
        self.assertEqual((transfer_id,), completed.released_transfer_ids)
        self.assertEqual((built.sequential_id,), completed.released_sequential_ids)
        self.assertEqual(0, self.manager.active_transfer_count)
        self.assertEqual(0, self.manager.active_sequential_id_count)
        repeated = self._control(
            TransferControlKind.COMPLETE, transfer_id=transfer_id
        ).value
        self.assertEqual((), repeated.released_transfer_ids)

        for terminal in (TransferControlKind.FAIL, TransferControlKind.CANCEL):
            transfer_id, _ = self._analyze(b"terminal-" + terminal.value.encode())
            self._build_short(
                transfer_id, b"terminal-" + terminal.value.encode()
            ).value
            released = self._control(terminal, transfer_id=transfer_id)
            self.assertTrue(released.is_ok, getattr(released, "error", None))

        session_handle = self.ids.new_session_handle()
        session_transfers = []
        for payload in (b"session-a", b"session-b"):
            transfer_id, _ = self._analyze(payload)
            packet = self._build_data(
                transfer_id,
                session_handle,
                payload,
                link_id=11,
                frame=0,
                count=1,
                position=1,
            )
            self.assertTrue(packet.is_ok, getattr(packet, "error", None))
            session_transfers.append(transfer_id)
        aborted = self._control(
            TransferControlKind.ABORT_SESSION,
            session_handle=session_handle,
        ).value
        self.assertEqual(tuple(session_transfers), aborted.released_transfer_ids)
        self.assertEqual(2, len(aborted.released_sequential_ids))
        self.assertEqual(0, self.manager.active_sequential_id_count)
        repeated_abort = self._control(
            TransferControlKind.ABORT_SESSION,
            session_handle=session_handle,
        ).value
        self.assertEqual((), repeated_abort.released_transfer_ids)

    def test_TC_LINK_019_complete_rejects_outstanding_or_uncommitted(self):
        payload = b"not-yet-complete"
        transfer_id, _ = self._analyze(payload)
        built = self._build_short(transfer_id, payload).value
        outstanding = self._control(
            TransferControlKind.COMPLETE, transfer_id=transfer_id
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_PROGRESS_INVALID, outstanding.error.code
        )
        self._progress(
            transfer_id, (built, TransferPacketOutcome.DISCARDED)
        ).value
        uncommitted = self._control(
            TransferControlKind.COMPLETE, transfer_id=transfer_id
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_PROGRESS_INVALID, uncommitted.error.code
        )
        self.assertEqual(1, self.manager.active_sequential_id_count)

    def test_TC_LINK_023_progress_and_safe_profile_repartition(self):
        payload = bytes(range(100))  # Segment is 104 bytes: 35, 35, 34 on Link ID 11.
        transfer_id, requirement = self._analyze(payload)
        by_link = {item.link_id.value: item for item in requirement.profiles}
        self.assertEqual(3, by_link[11].data_session_fragment_count)
        self.assertEqual(1, by_link[11].minimum_tdma_frame_count)
        session_handle = self.ids.new_session_handle()
        packets = [
            self._build_data(
                transfer_id,
                session_handle,
                payload,
                link_id=11,
                frame=0,
                count=3,
                position=position,
            ).value
            for position in (1, 2, 3)
        ]
        first = self._progress(
            transfer_id,
            (packets[0], TransferPacketOutcome.SUCCESS),
            session_handle=session_handle,
        ).value
        self.assertEqual(35, first.progress[0].committed_prefix_bytes)

        unsafe = self._build_data(
            transfer_id,
            session_handle,
            payload,
            link_id=17,
            frame=1,
            count=1,
            position=1,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_PROFILE_SWITCH_UNSAFE, unsafe.error.code
        )

        resolved = self._progress(
            transfer_id,
            (packets[1], TransferPacketOutcome.DISCARDED),
            (packets[2], TransferPacketOutcome.DISCARDED),
            session_handle=session_handle,
        ).value
        self.assertEqual(35, resolved.progress[0].committed_prefix_bytes)
        switched = self._build_data(
            transfer_id,
            session_handle,
            payload,
            link_id=17,
            frame=1,
            count=1,
            position=1,
        )
        self.assertTrue(switched.is_ok, getattr(switched, "error", None))
        self.assertEqual(35, switched.value.segment_offset)
        self.assertEqual(69, switched.value.segment_length)
        self.assertEqual(3, switched.value.fragment.index)
        self.assertEqual(packets[0].sequential_id, switched.value.sequential_id)

        duplicate = self._progress(
            transfer_id,
            (packets[0], TransferPacketOutcome.SUCCESS),
            session_handle=session_handle,
        )
        self.assertTrue(duplicate.is_ok, getattr(duplicate, "error", None))
        conflict = self._progress(
            transfer_id,
            (packets[0], TransferPacketOutcome.DISCARDED),
            session_handle=session_handle,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_PROGRESS_CONFLICT, conflict.error.code
        )

    def test_TC_LINK_023_gap_success_blocks_profile_switch(self):
        payload = bytes(range(100))
        transfer_id, _ = self._analyze(payload)
        session_handle = self.ids.new_session_handle()
        packets = [
            self._build_data(
                transfer_id,
                session_handle,
                payload,
                link_id=11,
                frame=0,
                count=3,
                position=position,
            ).value
            for position in (1, 2, 3)
        ]
        self._progress(
            transfer_id,
            (packets[0], TransferPacketOutcome.DISCARDED),
            (packets[1], TransferPacketOutcome.SUCCESS),
            (packets[2], TransferPacketOutcome.DISCARDED),
            session_handle=session_handle,
        ).value
        rejected = self._build_data(
            transfer_id,
            session_handle,
            payload,
            link_id=17,
            frame=1,
            count=1,
            position=1,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_PROGRESS_NONCONTIGUOUS, rejected.error.code
        )

    def test_TC_LINK_034_tombstone_conflicts_late_progress_and_restart(self):
        payload = b"terminal-data"
        transfer_id, _ = self._analyze(payload)
        session_handle = self.ids.new_session_handle()
        built = self._build_data(
            transfer_id,
            session_handle,
            payload,
            link_id=11,
            frame=0,
            count=1,
            position=1,
        ).value
        self._control(
            TransferControlKind.FAIL,
            transfer_id=transfer_id,
            session_handle=session_handle,
        ).value
        repeated = self._control(
            TransferControlKind.FAIL,
            transfer_id=transfer_id,
            session_handle=session_handle,
        )
        self.assertTrue(repeated.is_ok, getattr(repeated, "error", None))
        kind_conflict = self._control(
            TransferControlKind.CANCEL,
            transfer_id=transfer_id,
            session_handle=session_handle,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT, kind_conflict.error.code
        )
        other_session = self.ids.new_session_handle()
        session_conflict = self._control(
            TransferControlKind.FAIL,
            transfer_id=transfer_id,
            session_handle=other_session,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT, session_conflict.error.code
        )
        late = self._progress(
            transfer_id,
            (built, TransferPacketOutcome.SUCCESS),
            session_handle=session_handle,
        )
        self.assertEqual(
            ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT, late.error.code
        )
        unknown = self._control(
            TransferControlKind.FAIL,
            transfer_id=self.ids.new_transfer_id(),
        )
        self.assertEqual(ErrorCode.LINK_UNKNOWN_TRANSFER, unknown.error.code)

        self.manager.restart()
        self.assertEqual(0, self.manager.terminal_tombstone_count)
        after_restart = self._control(
            TransferControlKind.FAIL,
            transfer_id=transfer_id,
            session_handle=session_handle,
        )
        self.assertEqual(ErrorCode.LINK_UNKNOWN_TRANSFER, after_restart.error.code)
        restarted_transfer, _ = self._analyze(b"allocator-restart")
        restarted = self._build_short(restarted_transfer, b"allocator-restart").value
        self.assertEqual(0, restarted.sequential_id.value)

    def test_TC_LINK_034_tombstone_fifo_limit(self):
        oldest = None
        newest = None
        for index in range(MAX_TERMINAL_TOMBSTONES + 1):
            payload = index.to_bytes(2, "big")
            transfer_id, _ = self._analyze(payload)
            self._build_short(transfer_id, payload).value
            released = self._control(
                TransferControlKind.FAIL, transfer_id=transfer_id
            )
            self.assertTrue(released.is_ok, getattr(released, "error", None))
            oldest = transfer_id if oldest is None else oldest
            newest = transfer_id
        self.assertEqual(
            MAX_TERMINAL_TOMBSTONES, self.manager.terminal_tombstone_count
        )
        evicted = self._control(
            TransferControlKind.FAIL, transfer_id=oldest
        )
        self.assertEqual(ErrorCode.LINK_UNKNOWN_TRANSFER, evicted.error.code)
        retained = self._control(
            TransferControlKind.FAIL, transfer_id=newest
        )
        self.assertTrue(retained.is_ok, getattr(retained, "error", None))


if __name__ == "__main__":
    unittest.main()
