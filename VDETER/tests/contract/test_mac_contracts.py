"""TC-MAC-001: additive Stage 4 execution contracts and atomic outbox."""

from __future__ import annotations

import unittest
from threading import RLock

from vdeter.common.contracts.mac_execution import (
    CancelWakeupCommand,
    LINK_COMMAND_REQUEST_TYPES,
    LINK_REQUEST_TO_RESULT_TYPE,
    LINK_RESULT_PAYLOAD_TYPES,
    MacCommandBatch,
    MacCommandEnvelope,
)
from vdeter.common.errors import Correlation
from vdeter.common.identifiers import DeterministicIdSource
from vdeter.mac.execution import MacCommandSink, MacExecutionPort
from vdeter.mac.interface import MacPort
from vdeter.mac.models import MacState, MacStateRepository
from vdeter.runtime.mac_commands import EndpointHealthLog, MacCommandOutbox


class Stage4MacContractTest(unittest.TestCase):
    def test_TC_MAC_001_ports_result_union_and_snapshot_commit_are_closed(self):
        self.assertEqual(set(LINK_COMMAND_REQUEST_TYPES), set(LINK_REQUEST_TO_RESULT_TYPE))
        self.assertEqual(7, len(LINK_REQUEST_TO_RESULT_TYPE))
        self.assertEqual(
            {
                "LinkAnalyzeRequest": "TransferRequirement",
                "LinkPacketEncodeRequest": "EncodedPacket",
                "TbbEncodeRequest": "TbbEncodedPacketSet",
                "LinkFragmentProcessRequest": "FragmentProcessResult",
                "LinkReassemblyControlRequest": "ReassemblyControlResult",
                "LinkTransferControlRequest": "LinkTransferControlResult",
                "TbbAssemblyControlRequest": "TbbAssemblyControlResult",
            },
            {
                request.__name__: result.__name__
                for request, result in LINK_REQUEST_TO_RESULT_TYPE.items()
            },
        )
        self.assertEqual(
            {
                "TransferRequirement",
                "BuiltPacket",
                "EncodedPacket",
                "TbbEncodedPacketSet",
                "FragmentProcessResult",
                "ReassemblyControlResult",
                "LinkTransferControlResult",
                "TbbAssemblyControlResult",
            },
            {item.__name__ for item in LINK_RESULT_PAYLOAD_TYPES},
        )
        self.assertTrue(getattr(MacPort, "_is_runtime_protocol", False))
        self.assertTrue(getattr(MacExecutionPort, "_is_runtime_protocol", False))
        self.assertTrue(getattr(MacCommandSink, "_is_runtime_protocol", False))

        original = MacState()
        repository = MacStateRepository(original)
        repository.set_commit_hook(lambda _next: (_ for _ in ()).throw(RuntimeError("fail")))
        with self.assertRaises(RuntimeError):
            repository.commit(original.evolve(seeded=True))
        self.assertIs(original, repository.state)
        self.assertFalse(repository.state.seeded)

    def test_TC_MAC_001_outbox_reserves_then_publishes_or_aborts_atomically(self):
        ids = DeterministicIdSource(91)
        outbox = MacCommandOutbox(ids, RLock(), EndpointHealthLog(), capacity=2)

        def batch(suffix: str):
            command = MacCommandEnvelope(
                command_id=f"command-{suffix}",
                causation_event_id="cause",
                correlation=Correlation(),
                payload=CancelWakeupCommand(timer_id=f"timer-{suffix}"),
            )
            return MacCommandBatch(
                batch_id=f"batch-{suffix}", causation_event_id="cause", commands=(command,)
            )

        reserved = outbox.submit(batch("publish"))
        self.assertTrue(reserved.is_ok)
        self.assertEqual((1, 0), (outbox.reserved_count(), outbox.ready_count()))
        outbox.publish(reserved.value)
        self.assertEqual((0, 1), (outbox.reserved_count(), outbox.ready_count()))

        aborted = outbox.submit(batch("abort"))
        self.assertTrue(aborted.is_ok)
        outbox.abort(aborted.value)
        self.assertEqual((1, 1), (outbox.occupancy(), outbox.ready_count()))


if __name__ == "__main__":
    unittest.main()
