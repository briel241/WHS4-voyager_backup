"""TC-RADIO-011 SoftwareRadioMedium determinism and directed-link behavior."""

from __future__ import annotations

import struct
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.support.radio_stage3 import UPPER, burst_id, cf32_block, software_configuration, utc
from vdeter.common.contracts.radio import (
    RadioConfigureRequest,
    RadioRxRequest,
    RadioStartRequest,
    RadioTxRequest,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.events import InternalEventType, RecordingInternalEventSink
from vdeter.common.identifiers import BurstId, DeterministicIdSource
from vdeter.common.serialization import to_reference_json
from vdeter.common.time import TestTimeSource as ControlledTimeSource
from vdeter.radio.software import (
    SoftwareRadioAdapter,
    SoftwareRadioFault,
    SoftwareRadioLink,
    SoftwareRadioMedium,
)


class SoftwareMediumTest(unittest.TestCase):
    def _adapter(self, endpoint, clock, medium, seed, events, faults=()):
        adapter = SoftwareRadioAdapter(clock, DeterministicIdSource(seed), events,
            endpoint_id=endpoint, medium=medium, faults=faults, fault_seed=99)
        self.assertTrue(adapter.configure(RadioConfigureRequest(software_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        return adapter

    def test_TC_RADIO_011_directed_delay_zero_fill_and_overlap_sum(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        medium = SoftwareRadioMedium((
            SoftwareRadioLink("source-a", "receiver", UPPER, 0),
            SoftwareRadioLink("source-b", "receiver", UPPER, 1),
        ))
        source_a = self._adapter("source-a", clock, medium, 1, RecordingInternalEventSink())
        source_b = self._adapter("source-b", clock, medium, 2, RecordingInternalEventSink())
        receiver_events = RecordingInternalEventSink()
        receiver = self._adapter("receiver", clock, medium, 3, receiver_events)
        at = utc(1_100_000_000)
        self.assertTrue(source_a.schedule_tx(RadioTxRequest(
            burst_id(1), UPPER, at, cf32_block(at, (0.25 + 0.0j, 0.5 + 0.0j)))).is_ok)
        self.assertTrue(source_b.schedule_tx(RadioTxRequest(
            burst_id(2), UPPER, at, cf32_block(at, (0.75 + 0.0j, 0.25 + 0.0j)))).is_ok)
        # 39,063 ns rounds to three samples at 76.8 ksps.
        self.assertTrue(receiver.request_rx(RadioRxRequest(UPPER, at, 39_063, 76_800)).is_ok)
        clock.advance(101_000_000)
        receiver.process_due()
        block = receiver_events.events_of(InternalEventType.SAMPLES_RECEIVED)[0].payload.samples
        values = tuple(complex(i, q) for i, q in struct.iter_unpack("<ff", block.data))
        self.assertEqual((0.25 + 0j, 1.25 + 0j, 0.25 + 0j), values)

        unlinked_events = RecordingInternalEventSink()
        unlinked = self._adapter("unlinked", clock, medium, 4, unlinked_events)
        later = utc(clock.now_utc().total_nanoseconds())
        self.assertTrue(unlinked.request_rx(RadioRxRequest(UPPER, later, 39_063, 76_800)).is_ok)
        clock.advance(1_000_000)
        unlinked.process_due()
        silence = unlinked_events.events_of(InternalEventType.SAMPLES_RECEIVED)[0].payload.samples.data
        self.assertEqual(bytes(24), silence)

    def test_TC_RADIO_011_same_fault_plan_produces_same_terminal(self):
        snapshots = []
        for run in range(2):
            clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
                monotonic_clock_id="radio-test-monotonic")
            events = RecordingInternalEventSink()
            fault = SoftwareRadioFault("tx", ErrorCode.RADIO_LATE_COMMAND,
                channel=UPPER, burst_id=burst_id(3))
            adapter = self._adapter("source", clock, SoftwareRadioMedium(), 55, events, (fault,))
            at = utc(1_100_000_000)
            self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                burst_id(3), UPPER, at, cf32_block(at))).is_ok)
            clock.advance(101_000_000)
            adapter.process_due()
            event = events.events_of(InternalEventType.LAYER_ERROR)[0]
            snapshots.append(to_reference_json(event))
        self.assertEqual(snapshots[0], snapshots[1])

    def test_TC_RADIO_011_fault_target_hardware_time_and_one_shot_are_exact(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        fault = SoftwareRadioFault(
            "tx", ErrorCode.RADIO_TX_UNDERFLOW,
            channel=UPPER, hardware_time_ns=100_000_000,
            adapter_id="target", persistent=False,
        )
        other_events = RecordingInternalEventSink()
        target_events = RecordingInternalEventSink()
        other = self._adapter(
            "other", clock, SoftwareRadioMedium(), 61, other_events, (fault,)
        )
        target = self._adapter(
            "target", clock, SoftwareRadioMedium(), 62, target_events, (fault,)
        )

        first_at = utc(1_100_000_000)
        self.assertTrue(other.schedule_tx(RadioTxRequest(
            burst_id(61), UPPER, first_at, cf32_block(first_at),
        )).is_ok)
        self.assertTrue(target.schedule_tx(RadioTxRequest(
            burst_id(62), UPPER, first_at, cf32_block(first_at),
        )).is_ok)
        clock.advance(101_000_000)
        other.process_due()
        target.process_due()
        self.assertEqual(1, len(other_events.events_of(InternalEventType.BURST_COMPLETED)))
        underflows = [
            event for event in target_events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_TX_UNDERFLOW
        ]
        self.assertEqual(1, len(underflows))

        second_at = utc(1_202_000_000)
        self.assertTrue(target.schedule_tx(RadioTxRequest(
            burst_id(63), UPPER, second_at, cf32_block(second_at),
        )).is_ok)
        clock.advance(102_000_000)
        target.process_due()
        self.assertEqual(1, len(target_events.events_of(InternalEventType.BURST_COMPLETED)))
        self.assertEqual(1, len([
            event for event in target_events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_TX_UNDERFLOW
        ]))

    def test_TC_RADIO_011_retention_is_bounded_and_active_rx_is_protected(self):
        medium = SoftwareRadioMedium(
            (SoftwareRadioLink("source", "receiver", UPPER),),
            retention_nanoseconds=5_000_000,
            max_retained_transmissions=16,
        )
        protected_at = utc(1_100_000_000)
        protected_key = (1, UPPER.name, "protected")
        medium.register_rx(
            "receiver", protected_key, UPPER, protected_at, 76_800, 2
        )
        protected = RadioTxRequest(
            burst_id(700), UPPER, protected_at,
            cf32_block(protected_at, (0.5 + 0j, 0.25 + 0j)),
        )
        medium.submit("source", protected, 1)

        for index in range(1, 501):
            at = utc(1_100_000_000 + index * 1_000_000)
            medium.submit(
                "source",
                RadioTxRequest(burst_id(700 + index), UPPER, at, cf32_block(at)),
                1,
            )
        self.assertLessEqual(medium.retained_transmission_count(), 16)
        rendered = medium.render("receiver", UPPER, protected_at, 76_800, 2)
        self.assertEqual(protected.samples.data, rendered)
        medium.release_rx("receiver", protected_key)
        self.assertEqual(0, medium.active_rx_reservation_count())

        final_at = utc(2_000_000_000)
        medium.submit(
            "source",
            RadioTxRequest(burst_id(1_500), UPPER, final_at, cf32_block(final_at)),
            1,
        )
        self.assertLessEqual(medium.retained_transmission_count(), 2)

    def test_TC_RADIO_011_concurrent_submit_and_render_are_safe(self):
        medium = SoftwareRadioMedium(
            (SoftwareRadioLink("source", "receiver", UPPER),),
            retention_nanoseconds=10_000_000,
            max_retained_transmissions=64,
        )

        def exercise(index):
            at = utc(1_100_000_000 + index * 1_000_000)
            medium.submit(
                "source",
                RadioTxRequest(burst_id(2_000 + index), UPPER, at, cf32_block(at)),
                1,
            )
            return medium.render("receiver", UPPER, at, 76_800, 1)

        with ThreadPoolExecutor(max_workers=8) as executor:
            rendered = tuple(executor.map(exercise, range(200)))
        self.assertTrue(all(len(value) == 8 for value in rendered))
        self.assertLessEqual(medium.retained_transmission_count(), 64)


if __name__ == "__main__":
    unittest.main()
