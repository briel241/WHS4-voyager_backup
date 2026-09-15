"""TC-MAC-002: integer TDMA boundaries and exact RX windows."""

from __future__ import annotations

import unittest

from tests.support.stage4 import deployment_profile
from vdeter.mac.tbb import build_canonical_tbb_profile
from vdeter.mac.tdma import (
    CHANNEL_OFFSETS_NANOSECONDS,
    EPOCH_2000_UTC_NANOSECONDS,
    FRAME_NANOSECONDS,
    HEXSLOT_NANOSECONDS,
    channel_slot_duration_nanoseconds,
    position_at_nanoseconds,
    rx_windows_for_frame,
    samples_for_duration,
    slot_bounds_nanoseconds,
)


class Stage4TdmaTest(unittest.TestCase):
    def test_TC_MAC_002_half_open_integer_boundaries_have_no_accumulated_error(self):
        self.assertEqual((0, 26_666_667, 53_333_333, 80_000_000, 106_666_667, 133_333_333, 160_000_000), CHANNEL_OFFSETS_NANOSECONDS)
        self.assertEqual(HEXSLOT_NANOSECONDS, sum(
            channel_slot_duration_nanoseconds(channel) for channel in range(6)
        ))
        minute = EPOCH_2000_UTC_NANOSECONDS
        for channel in range(6):
            start, end = slot_bounds_nanoseconds(minute, 0, channel)
            self.assertEqual(channel, position_at_nanoseconds(start).tdma_channel)
            if channel < 5:
                self.assertEqual(channel + 1, position_at_nanoseconds(end).tdma_channel)
        self.assertEqual(1, position_at_nanoseconds(minute + FRAME_NANOSECONDS).tdma_frame_index)

    def test_TC_MAC_002_each_logical_slot_window_is_nonoverlapping_2048_samples(self):
        slot_map = build_canonical_tbb_profile(deployment_profile()).slot_map
        frame = 0
        windows = rx_windows_for_frame(slot_map.slots, frame)
        ordered = sorted(windows, key=lambda item: (item.channel_name, item.start_nanoseconds))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.channel_name == current.channel_name:
                self.assertLessEqual(previous.end_nanoseconds, current.start_nanoseconds)
        for window in windows:
            self.assertEqual(2048, samples_for_duration(window.duration_nanoseconds, 76_800))


if __name__ == "__main__":
    unittest.main()
