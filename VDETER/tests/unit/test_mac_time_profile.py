"""TC-MAC-016: supported clock profiles and explicit RF verification boundary."""

from __future__ import annotations

import unittest
from pathlib import Path

from vdeter.common.time import ClockDomain, SyncStatus, SystemTimeSource, TestTimeSource as ControlledTimeSource


class Stage4TimeProfileTest(unittest.TestCase):
    def test_TC_MAC_016_system_and_test_time_sources_keep_mapping_boundary(self):
        system = SystemTimeSource()
        self.assertIs(SyncStatus.UNKNOWN, system.sync_status())
        self.assertTrue(system.mapping(ClockDomain.UTC, ClockDomain.MONOTONIC).is_err)

        controlled = ControlledTimeSource(utc_nanoseconds=10_000, monotonic_nanoseconds=2_000)
        mapping = controlled.mapping(ClockDomain.UTC, ClockDomain.MONOTONIC)
        self.assertTrue(mapping.is_ok)
        self.assertIs(SyncStatus.SYNCHRONIZED, controlled.sync_status())
        controlled.advance(160_000_000)
        self.assertEqual((160_010_000, 160_002_000), (
            controlled.now_utc().total_nanoseconds(),
            controlled.now_monotonic().total_nanoseconds(),
        ))

    def test_TC_MAC_016_radiated_rf_remains_explicitly_not_verified(self):
        runner = Path(__file__).resolve().parents[2] / "tools" / "run_stage3_radio_tests.py"
        text = runner.read_text(encoding="utf-8")
        self.assertIn('"radiated_rf_compliance": "NOT_VERIFIED"', text)
        self.assertIn('"dual_leg_tbb_interoperability": "NOT_VERIFIED"', text)


if __name__ == "__main__":
    unittest.main()
