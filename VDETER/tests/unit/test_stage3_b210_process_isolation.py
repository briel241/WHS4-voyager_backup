"""Process-boundary tests for native gr-uhd failure containment."""

from __future__ import annotations

import os
import time
import unittest

from vdeter.radio.b210 import (
    B210BackendUnavailable,
    ProcessIsolatedGrUhdBackend,
)


class _IsolatedFakeBackend:
    def __init__(self, selector, cpu_format, otw_format):
        self.selector = selector
        self.cpu_format = cpu_format
        self.otw_format = otw_format

    def configure(self, configuration):
        return {"configuration": configuration, "selector": self.selector}

    def connect(self):
        return None

    def start(self):
        return None

    def stop(self, timeout_ns):
        return True

    def hardware_time_ns(self):
        return 123_456_789

    def poll_async(self):
        return ({"outcome": "BURST_ACK", "hardware_time_ns": 123},)

    def submit_tx(self, *args):
        return None

    def submit_rx(self, *args):
        return None

    def read_rx_cf32(self, sample_count, **kwargs):
        return (b"\0" * (sample_count * 8), 123, 0)


class _NativeAbortBackend(_IsolatedFakeBackend):
    def hardware_time_ns(self):
        # Model the observed UHD/libusb native terminate path: no Python
        # exception and no worker finally block, just a dead process.
        os._exit(73)


class _BlockingStopBackend(_IsolatedFakeBackend):
    def stop(self, timeout_ns):
        time.sleep(10.0)
        return True


class Stage3B210ProcessIsolationTests(unittest.TestCase):
    def _backend(self, backend_type):
        return ProcessIsolatedGrUhdBackend(
            "serial=TEST",
            "fc32",
            "sc16",
            _backend_type=backend_type,
        )

    def test_TC_RADIO_001_process_proxy_preserves_backend_contract_and_results(self):
        backend = self._backend(_IsolatedFakeBackend)
        self.assertEqual(
            {"configuration": ("immutable", 1), "selector": "serial=TEST"},
            backend.configure(("immutable", 1)),
        )
        backend.connect()
        backend.start()
        self.assertEqual(123_456_789, backend.hardware_time_ns())
        self.assertEqual("BURST_ACK", backend.poll_async()[0]["outcome"])
        self.assertEqual(16, len(backend.read_rx_cf32(2)[0]))
        self.assertTrue(backend.stop(1_000_000_000))

    def test_TC_RADIO_010_native_abort_is_contained_as_backend_unavailable(self):
        backend = self._backend(_NativeAbortBackend)
        with self.assertRaises(B210BackendUnavailable):
            backend.hardware_time_ns()
        self.assertTrue(backend.stop(1_000_000_000))

    def test_TC_RADIO_005_blocking_child_stop_is_bounded_and_retryable(self):
        backend = self._backend(_BlockingStopBackend)
        started = time.monotonic()
        self.assertFalse(backend.stop(50_000_000))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(backend.stop(50_000_000))


if __name__ == "__main__":
    unittest.main()
