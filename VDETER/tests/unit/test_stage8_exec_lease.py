"""MASS-B POC_ACTOR lease 배타성·반환 시험(실행 명세 §4.2, `S8-POC-REQ-27`). platform 무관."""

from __future__ import annotations

import unittest

from vdeter.poc.execution.lease import (
    MASS_B_SERIAL,
    LeaseError,
    MassBLease,
    MassBRole,
)


class LeaseTest(unittest.TestCase):
    def test_starts_normal(self):
        lease = MassBLease()
        self.assertEqual(lease.role, MassBRole.NORMAL)
        self.assertFalse(lease.is_leased)
        self.assertEqual(lease.serial, MASS_B_SERIAL)

    def test_acquire_release(self):
        lease = MassBLease()
        lease.acquire("w-1")
        self.assertTrue(lease.is_leased)
        self.assertEqual(lease.holder, "w-1")
        self.assertTrue(lease.release("w-1"))
        self.assertEqual(lease.role, MassBRole.NORMAL)
        self.assertIsNone(lease.holder)
        lease.assert_returned()

    def test_exclusive_double_acquire(self):
        lease = MassBLease()
        lease.acquire("w-1")
        with self.assertRaises(LeaseError):
            lease.acquire("w-2")

    def test_release_when_not_held_is_noop(self):
        lease = MassBLease()
        self.assertFalse(lease.release())
        lease.assert_returned()

    def test_release_wrong_holder(self):
        lease = MassBLease()
        lease.acquire("w-1")
        with self.assertRaises(LeaseError):
            lease.release("w-2")

    def test_assert_returned_fails_when_held(self):
        lease = MassBLease()
        lease.acquire("w-1")
        with self.assertRaises(LeaseError):
            lease.assert_returned()

    def test_sequential_workloads_exclusive(self):
        lease = MassBLease()
        for wid in ("w-1", "w-2", "w-3"):
            lease.acquire(wid)
            self.assertEqual(lease.holder, wid)
            lease.release(wid)
        lease.assert_returned()

    def test_history_recorded(self):
        lease = MassBLease()
        lease.acquire("w-1")
        lease.release("w-1")
        kinds = [h[0] for h in lease.history]
        self.assertEqual(kinds, ["ACQUIRE", "RELEASE"])

    def test_evidence_no_secret(self):
        lease = MassBLease()
        lease.acquire("w-1")
        ev = lease.as_evidence()
        self.assertEqual(ev["role"], "POC_ACTOR")
        self.assertEqual(ev["serial"], MASS_B_SERIAL)


if __name__ == "__main__":
    unittest.main()
