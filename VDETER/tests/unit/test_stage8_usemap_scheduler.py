"""Real PoC slot-query implementation must consult heard occupancy."""
import unittest
from types import SimpleNamespace

from tests.support.stage4 import deployment_profile
from vdeter.mac.models import LcAllocation, LcState
from vdeter.mac.tbb import build_wideband_canonical_tbb_profile
from vdeter.mac.tdma import EPOCH_2000_UTC_NANOSECONDS
from vdeter.stage7.configuration import LOWER_CHANNEL_NAME
from vdeter.stage7.testing.poc_device import Stage7PocDeviceSeam


class UseMapSchedulerTests(unittest.TestCase):
    def test_default_scheduler_queries_each_logical_targets_channel(self):
        from apps.stage8_launcher_seams import MacSlotScheduler
        from vdeter.stage7.configuration import UPPER_CHANNEL_NAME
        from vdeter.poc.api.types import ActionDescriptor, ActionKind

        class Query(MacSlotScheduler):
            def _query(self):
                self.queried = self._channel_name
                return {"slots": [dict(start_hw_ns=self._base_hw_ns + 1_000_000,
                                       frame_ordinal=1, tdma_channel=1, slot_in_frame=1)]}

        scheduler = Query(SimpleNamespace(hardware_time_ns=lambda: 1_000_000_000), None)
        for target, channel in (("ROC", LOWER_CHANNEL_NAME), ("MASS-A", UPPER_CHANNEL_NAME)):
            result = scheduler.resolve(ActionDescriptor(ActionKind.REPLAY_PACKET, target),
                                       request_id=target)
            self.assertEqual(channel, scheduler.queried)
            self.assertTrue(result.slot_key.startswith("mac-"))
            self.assertEqual(result.scheduled_hw_timestamp_ns * 307200 // 1000000000,
                             result.scheduled_sample)

    def test_exact_after_slots_does_not_move_to_later_free_mac_slot(self):
        from apps.stage8_launcher_seams import MacSlotScheduler
        from apps.stage8_stage7_seams import hw_ns_to_slot
        from vdeter.poc.api.types import ActionDescriptor, ActionKind
        from vdeter.poc.api.errors import PocError, PocErrorCode

        class LaterSlot(MacSlotScheduler):
            def _query(self):
                return {"slots": [dict(start_hw_ns=self._base_hw_ns + 1_000_000,
                                       frame_ordinal=1, tdma_channel=1, slot_in_frame=1)]}

        scheduler = LaterSlot(SimpleNamespace(hardware_time_ns=lambda: 1_000_000_000), None)
        scheduler.register_trigger("observed", slot=hw_ns_to_slot(1_000_000_000))
        action = ActionDescriptor(ActionKind.REPLAY_PACKET, "ROC",
                                  source_observation_id="observed",
                                  schedule={"kind": "after_slots", "slots": 20})
        with self.assertRaises(PocError) as caught:
            scheduler.resolve(action, request_id="fixed-slot")
        self.assertEqual(PocErrorCode.POC_DEADLINE_MISSED, caught.exception.code)

    def test_query_excludes_reserved_lc_and_restores_it_after_expiry(self):
        slot_map = build_wideband_canonical_tbb_profile(deployment_profile()).slot_map
        state = SimpleNamespace(lc_allocations=())
        mac = SimpleNamespace(state=state, coordinator=SimpleNamespace(
            active_slot_map=lambda _state: slot_map))
        bridge = SimpleNamespace(hardware_ns_to_utc_label=lambda n: n,
                                 utc_label_to_hardware_ns=lambda n: n)
        endpoint = SimpleNamespace(stage4=SimpleNamespace(mac=mac),
                                   dependencies=SimpleNamespace(radio=SimpleNamespace(bridge=bridge)))
        seam = Stage7PocDeviceSeam(endpoint_id="MASS-B", endpoint=endpoint,
                                   station_id_by_endpoint={})
        query = dict(channel_name=LOWER_CHANNEL_NAME,
                     from_hw_ns=EPOCH_2000_UTC_NANOSECONDS + 10_000_000_000, limit=128)
        baseline = seam.mac_transmit_slots(**query)["slots"]
        target = next(item for item in baseline if item["function"] == "DC")
        lc = target["logical_channel"]
        state.lc_allocations = (LcAllocation(lc, lc + 100, 222222222, 111111111,
                                            LcState.EXTERNAL_RESERVED, 0, 99999, 1, False),)
        occupied = seam.mac_transmit_slots(**query)["slots"]
        self.assertFalse(any(item["logical_channel"] == lc for item in occupied))
        self.assertTrue(occupied, "other free slots must remain available")
        state.lc_allocations = (LcAllocation(lc, lc + 100, 222222222, 111111111,
                                            LcState.EXTERNAL_RESERVED, 0, 0, 1, False),)
        self.assertEqual(baseline, seam.mac_transmit_slots(**query)["slots"])
