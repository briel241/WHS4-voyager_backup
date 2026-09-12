"""RX-ready NEXT_SAFE_SLOT must not bypass MAC, clocks, or exact-slot semantics."""
import unittest
from types import SimpleNamespace as NS
from apps.stage8_launcher_seams import MacSlotScheduler
from apps.stage8_stage7_seams import Stage7ReceiverReadiness, hw_ns_to_slot
from vdeter.stage8.shared_medium.radiation import ReadinessDecision, ReadinessRequest
from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind, PhysicalChannel
from vdeter.stage8.poc_api.errors import PocError, PocErrorCode


class ReadySlotTests(unittest.TestCase):
    def scheduler(self, ready):
        class Query(MacSlotScheduler):
            def _query(self):
                return {"slots": [dict(start_hw_ns=self._base_hw_ns + i * 27000000,
                    frame_ordinal=1, tdma_channel=i, slot_in_frame=1) for i in (0, 1)]}
        client = NS(radio_state=lambda: dict(clock_id='real', bridge_generation=1, correction_revision=2))
        return Query(NS(hardware_time_ns=lambda: 1000000000), client,
                     rx_readiness=NS(window_ready=ready))

    def test_next_safe_uses_later_mac_candidate_only_when_rx_ready(self):
        seen = []
        def ready(request):
            seen.append(request)
            return ReadinessDecision(len(seen) == 2)
        scheduler = self.scheduler(ready)
        result = scheduler.resolve(ActionDescriptor(ActionKind.REPLAY_PACKET, 'ROC'), request_id='1')
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[1].scheduled_hw_timestamp_ns, result.scheduled_hw_timestamp_ns)
        self.assertEqual('real', seen[0].clock_id)

    def test_no_receiver_window_refuses_all_candidates(self):
        scheduler = self.scheduler(lambda _: ReadinessDecision(False))
        with self.assertRaises(PocError) as caught:
            scheduler.resolve(ActionDescriptor(ActionKind.REPLAY_PACKET, 'ROC'), request_id='2')
        self.assertEqual(PocErrorCode.POC_DEADLINE_MISSED, caught.exception.code)

    def test_exact_relative_slot_is_not_shifted_for_readiness(self):
        seen = []
        def ready(request):
            seen.append(request)
            return ReadinessDecision(False)
        scheduler = self.scheduler(ready)
        scheduler.register_trigger('trigger', slot=hw_ns_to_slot(1000000000))
        with self.assertRaises(PocError):
            scheduler.resolve(ActionDescriptor(ActionKind.REPLAY_PACKET, 'ROC',
                source_observation_id='trigger', schedule={'kind': 'after_slots', 'slots': 20}), request_id='3')
        self.assertEqual(1, len(seen))

    def test_remote_readiness_maps_to_receiver_clock_and_requires_real_window(self):
        receiver = NS(rx_rf_lo_hz=None, rx_lo_hz=10, rx_continuous=True,
            rx_continuous_since_ns=lambda: 0,
            rx_windows={0: [dict(start_ns=1000, count=8192, consumed=False)]},
            map_from_sender=lambda sender, timestamp: (timestamp-4000000000, {'mapped': True}))
        checker = Stage7ReceiverReadiness({'ROC': receiver}, {PhysicalChannel.LOWER: 10}, source_worker=object())
        request = ReadinessRequest('ROC', PhysicalChannel.LOWER, 'slot', 0, 4000001000, 'clock', 1, 0)
        proof = checker.window_ready(request)
        self.assertTrue(proof.ready)
        self.assertEqual(1000, proof.proof['scheduled_hw_timestamp_ns'])
        receiver.rx_windows = {}
        self.assertEqual('RX_WINDOW_NOT_PLANNED', checker.window_ready(request).reason)


if __name__ == '__main__':
    unittest.main()
