"""Receiver-local policy, including the intentionally limited observability model."""
import unittest
from types import SimpleNamespace as NS
from vdeter.stage7.collision_runtime import RxCollisionGate, overlapping_indices, MAX_OBSERVATIONS


class LocalRxTests(unittest.TestCase):
    def setUp(self):
        self.gate = RxCollisionGate("work", "MASS-A")

    def row(self, lane, start=100, channel="upper", data=b"packet"):
        p = NS(packet=data, packet_id=NS(value=f"{lane}:{start}:{data}"))
        m = dict(lane=lane, device_hw_timestamp_ns=start, duration_ns=27,
                 physical_channel=channel, host_mono_timestamp_ns=999)
        return p, m

    def run_window(self, *rows, key="w"):
        item = (None, key, None, 0, 1000)
        self.assertTrue(self.gate.hold(key, item, rows))
        return self.gate.drain()[0][1]

    def test_distinct_lane_overlap_drops_both(self):
        self.assertEqual((), self.run_window(self.row(0), self.row(1, 126)))
        self.assertEqual(2, self.gate.poll()["counts"]["dropped"])

    def test_equal_packet_bytes_do_not_hide_overlap(self):
        self.assertEqual((), self.run_window(self.row(0), self.row(1)))

    def test_half_open_touch_is_not_overlap(self):
        self.assertEqual(2, len(self.run_window(self.row(0), self.row(1, 127, data=b"other"))))

    def test_other_channel_does_not_collide(self):
        self.assertEqual(2, len(self.run_window(self.row(0), self.row(1, channel="lower", data=b"other"))))

    def test_unobserved_interferer_is_not_invented(self):
        self.assertEqual(1, len(self.run_window(self.row(0))))
        obs = self.gate.poll()["observations"][0]
        self.assertNotIn("tx_id", obs)
        self.assertEqual("UNKNOWN", obs["destination_station"])

    def test_missing_phy_time_fails_closed(self):
        packet, meta = self.row(0)
        del meta["device_hw_timestamp_ns"]
        self.assertEqual((), self.run_window((packet, meta)))
        self.assertIsNotNone(self.gate.fault)

    def test_no_tx_bundle_or_wait_timer(self):
        self.assertFalse(hasattr(self.gate, "apply"))
        self.assertEqual(1, len(self.run_window(self.row(0))))

    def test_application_receipt_requires_actual_callback(self):
        row = self.row(0)
        self.run_window(row)
        self.assertEqual([], self.gate.poll()["application_receipts"])
        self.gate.application_delivered(row[0].packet_id.value)
        self.assertEqual(1, len(self.gate.poll()["application_receipts"]))
        self.gate.application_delivered(row[0].packet_id.value)
        self.assertEqual([], self.gate.poll()["application_receipts"])

    def test_consumer_overload_does_not_stall_normal_rx(self):
        for i in range(MAX_OBSERVATIONS + 2):
            self.assertEqual(1, len(self.run_window(self.row(0, start=100+i), key=str(i))))
        self.assertIsNotNone(self.gate.fault)
        self.assertEqual(MAX_OBSERVATIONS, len(self.gate.poll()["observations"]))

    def test_close_bypasses_new_local_scope(self):
        self.gate.begin_close()
        self.assertIsNone(self.gate.hold("new", None, ()))
