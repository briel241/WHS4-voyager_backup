"""TC-RADIO-003..010 deterministic SoftwareRadioAdapter contract tests."""

from __future__ import annotations

import struct
import unittest
from dataclasses import replace

from tests.support.radio_stage3 import LOWER, UPPER, burst_id, cf32_block, limits, software_configuration, utc
from vdeter.common.contracts.radio import (
    RadioConfigureRequest,
    RadioDeviceState,
    RadioRxRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStopRequest,
    RadioTxRequest,
    SampleContinuity,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.events import EventPublished, InternalEventType, RecordingInternalEventSink
from vdeter.common.identifiers import BurstId, DeterministicIdSource
from vdeter.common.results import Ok
from vdeter.common.time import TestTimeSource as ControlledTimeSource
from vdeter.radio.software import SoftwareRadioAdapter, SoftwareRadioFault


class _UndeliveredSink:
    def publish(self, event):
        delivered = event.event_type is not InternalEventType.BURST_COMPLETED
        return Ok(EventPublished(event.event_id, delivered=delivered))


class _FailOnceSink:
    def __init__(self, event_type):
        self.event_type = event_type
        self.failed = False
        self.events = RecordingInternalEventSink()

    def publish(self, event):
        if event.event_type is self.event_type and not self.failed:
            self.failed = True
            return Ok(EventPublished(event.event_id, delivered=False))
        return self.events.publish(event)


class SoftwareRadioTest(unittest.TestCase):
    def make_adapter(self, *, configuration=None, faults=(), sink=None, **adapter_options):
        clock = ControlledTimeSource(
            utc_nanoseconds=1_000_000_000,
            monotonic_nanoseconds=0,
            utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = sink or RecordingInternalEventSink()
        adapter = SoftwareRadioAdapter(clock, DeterministicIdSource(7), events,
            endpoint_id="ship-a", faults=faults, fault_seed=11, **adapter_options)
        configured = adapter.configure(RadioConfigureRequest(configuration or software_configuration()))
        self.assertTrue(configured.is_ok, configured)
        started = adapter.start(RadioStartRequest("test"))
        self.assertTrue(started.is_ok, started)
        return adapter, clock, events

    def tx(self, at_ns=1_100_000_000, burst=1, values=None):
        at = utc(at_ns)
        samples = cf32_block(at) if values is None else cf32_block(at, values)
        return RadioTxRequest(burst_id(burst), UPPER, at, samples)

    def test_TC_RADIO_002_direct_adapter_configuration_enforces_exact_profile(self):
        configuration = software_configuration()
        invalid = replace(
            configuration,
            channels=(
                replace(configuration.channels[0], center_frequency_hz=157_237_501),
                configuration.channels[1],
            ),
        )
        adapter = SoftwareRadioAdapter()
        result = adapter.configure(RadioConfigureRequest(invalid))
        self.assertEqual(ErrorCode.RADIO_UNSUPPORTED_SETTING, result.error.code)

    def test_TC_RADIO_003_bytes_are_immutable_and_no_amplitude_rewrite_occurs(self):
        adapter, clock, events = self.make_adapter()
        request = self.tx(values=(0.5 + 0.25j,))
        expected = bytes(request.samples.data)
        self.assertTrue(adapter.schedule_tx(request).is_ok)
        self.assertEqual(expected, request.samples.data)
        clock.advance(101_000_000)
        adapter.process_due()
        completed = events.events_of(InternalEventType.BURST_COMPLETED)
        self.assertEqual(1, len(completed))

    def test_TC_RADIO_003_out_of_range_and_nonfinite_tx_are_rejected_whole(self):
        adapter, _, _ = self.make_adapter()
        for index, values in enumerate(((1.0 + 1.0j,), (complex(float("nan"), 0.0),))):
            result = adapter.schedule_tx(self.tx(burst=10 + index, values=values))
            self.assertTrue(result.is_err)
            self.assertEqual(ErrorCode.RADIO_TX_SAMPLE_OUT_OF_RANGE, result.error.code)

    def test_TC_RADIO_004_tx_backpressure_is_bounded_and_observable(self):
        configuration = software_configuration(resource_limits=limits(
            tx_queue_max_count=1, tx_queue_max_bytes=16,
        ))
        adapter, _, _ = self.make_adapter(configuration=configuration)
        self.assertTrue(adapter.schedule_tx(self.tx(burst=20)).is_ok)
        rejected = adapter.schedule_tx(self.tx(at_ns=1_200_000_000, burst=21))
        self.assertTrue(rejected.is_err)
        self.assertEqual(ErrorCode.RADIO_RESOURCE_EXHAUSTED, rejected.error.code)
        queue = adapter.status(RadioStatusRequest(UPPER)).value.queue_status[0]
        self.assertEqual((1, 1), (queue.tx_outstanding_count, queue.tx_rejected_count))

    def test_TC_RADIO_004_shared_tx_bytes_and_rx_window_conflicts_are_enforced(self):
        adapter, _, _ = self.make_adapter()
        at = utc(1_100_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(210), UPPER, at, cf32_block(at, (0.1 + 0.1j,) * 8),
        )).is_ok)
        overlap = adapter.schedule_tx(RadioTxRequest(
            burst_id(211), LOWER, at, cf32_block(at),
        ))
        self.assertEqual(ErrorCode.RADIO_RESOURCE_CONFLICT, overlap.error.code)
        first = adapter.request_rx(RadioRxRequest(UPPER, at, 80_000_000, 76_800))
        second = adapter.request_rx(RadioRxRequest(
            UPPER, utc(1_120_000_000), 80_000_000, 76_800,
        ))
        self.assertTrue(first.is_ok)
        self.assertEqual(ErrorCode.RADIO_RESOURCE_CONFLICT, second.error.code)

    def test_TC_RADIO_004_shared_byte_budget_and_per_channel_window_count_are_enforced(self):
        configuration = software_configuration(resource_limits=limits(
            tx_queue_max_count=4, tx_queue_max_bytes=16,
            rx_window_max_count=1,
        ))
        adapter, _, _ = self.make_adapter(configuration=configuration)
        first_at = utc(1_100_000_000)
        second_at = utc(1_200_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(212), LOWER, first_at, cf32_block(first_at),
        )).is_ok)
        exhausted = adapter.schedule_tx(RadioTxRequest(
            burst_id(213), UPPER, second_at, cf32_block(second_at),
        ))
        self.assertEqual(ErrorCode.RADIO_RESOURCE_EXHAUSTED, exhausted.error.code)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, utc(1_300_000_000), 1_000_000, 76_800,
        )).is_ok)
        window_full = adapter.request_rx(RadioRxRequest(
            UPPER, utc(1_302_000_000), 1_000_000, 76_800,
        ))
        self.assertEqual(ErrorCode.RADIO_RESOURCE_EXHAUSTED, window_full.error.code)

    def test_TC_RADIO_004_overflow_delivers_prefix_and_gap_suffix_then_incomplete(self):
        fault = SoftwareRadioFault(
            "rx", ErrorCode.RADIO_RX_OVERFLOW, channel=UPPER, sample_index=2,
        )
        adapter, clock, events = self.make_adapter(faults=(fault,))
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000, 76_800,
        )).is_ok)
        clock.advance(101_000_000)
        adapter.process_due()
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual(2, len(blocks))
        self.assertEqual(SampleContinuity.CONTIGUOUS, blocks[0].payload.continuity)
        self.assertEqual(SampleContinuity.GAP_BEFORE, blocks[1].payload.continuity)
        terminals = [value for value in events.events_of(InternalEventType.LAYER_ERROR)
            if value.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE]
        self.assertEqual(1, len(terminals))

    def test_TC_RADIO_005_lifecycle_stop_cancels_once_and_is_idempotent(self):
        adapter, _, events = self.make_adapter()
        self.assertTrue(adapter.schedule_tx(self.tx()).is_ok)
        first = adapter.stop(RadioStopRequest("stop"))
        second = adapter.stop(RadioStopRequest("repeat"))
        self.assertTrue(first.is_ok)
        self.assertEqual(first.value, second.value)
        terminals = events.events_of(InternalEventType.LAYER_ERROR)
        self.assertEqual(1, len(terminals))
        self.assertEqual(ErrorCode.RADIO_OPERATION_CANCELLED, terminals[0].error.code)
        self.assertEqual(RadioDeviceState.STOPPED, adapter.status(RadioStatusRequest()).value.device_state)

    def test_TC_RADIO_005_failed_stop_can_be_retried_and_generation_only_counts_success(self):
        fault = SoftwareRadioFault("stop", ErrorCode.RADIO_STOP_TIMEOUT)
        adapter, _, _ = self.make_adapter(faults=(fault,))
        first = adapter.stop(RadioStopRequest("timeout"))
        self.assertEqual(ErrorCode.RADIO_STOP_TIMEOUT, first.error.code)
        self.assertEqual(RadioDeviceState.FAILED, adapter.status(RadioStatusRequest()).value.device_state)
        self.assertTrue(adapter.stop(RadioStopRequest("retry")).is_ok)
        configured = adapter.configure(RadioConfigureRequest(software_configuration()))
        self.assertTrue(configured.is_ok)
        self.assertEqual(2, configured.value.applied_settings.configure_generation)

    def test_TC_RADIO_005_stop_preserves_active_software_rx_prefix(self):
        adapter, clock, events = self.make_adapter()
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000_000, 76_800,
        )).is_ok)
        clock.advance(120_000_000)
        stopped = adapter.stop(RadioStopRequest("partial RX"))
        self.assertTrue(stopped.is_ok, stopped)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual(1, len(blocks))
        self.assertEqual(1_536, blocks[0].payload.samples.sample_count)
        errors = events.events_of(InternalEventType.LAYER_ERROR)
        incomplete = [
            event for event in errors
            if event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
        ]
        self.assertEqual(1, len(incomplete))
        self.assertEqual(
            1_536,
            dict(incomplete[0].details.entries)["received_sample_count"],
        )

    def test_TC_RADIO_006_mapping_domains_expiry_and_clock_jump(self):
        adapter, clock, _ = self.make_adapter()
        status = adapter.status(RadioStatusRequest()).value.timing_status
        self.assertEqual("UTC", status.mapping.source_domain.value)
        self.assertEqual("RADIO_HARDWARE", status.mapping.target_domain.value)
        self.assertEqual(0, status.mapping.uncertainty_nanoseconds)
        clock.advance(1_000_000_001)
        stale = adapter.status(RadioStatusRequest()).value.timing_status
        self.assertEqual("STALE", stale.mapping_state.value)
        renewed_at = utc(2_100_000_001)
        renewed = adapter.schedule_tx(RadioTxRequest(
            burst_id(40), UPPER, renewed_at, cf32_block(renewed_at),
        ))
        self.assertTrue(renewed.is_ok, renewed)
        refreshed = adapter.status(RadioStatusRequest()).value.timing_status
        self.assertEqual("VALID", refreshed.mapping_state.value)
        self.assertIn(":cfg:1", refreshed.mapping.target_clock_id)

        adapter.stop(RadioStopRequest("reset"))
        adapter.configure(RadioConfigureRequest(software_configuration()))
        adapter.start(RadioStartRequest("jump"))
        clock.advance_utc(10_000_001)
        unavailable = adapter.status(RadioStatusRequest()).value.timing_status
        self.assertEqual("UNAVAILABLE", unavailable.mapping_state.value)
        future = clock.now_utc().shifted(100_000_000)
        rejected = adapter.schedule_tx(RadioTxRequest(
            burst_id(41), UPPER, future, cf32_block(future),
        ))
        self.assertEqual(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, rejected.error.code)

    def test_TC_RADIO_006_utc_jump_preserves_active_software_rx_prefix(self):
        adapter, clock, events = self.make_adapter()
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000_000, 76_800,
        )).is_ok)
        clock.advance(120_000_000)
        clock.advance_utc(10_000_001)
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual("UNAVAILABLE", status.timing_status.mapping_state.value)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual([1_536], [
            event.payload.samples.sample_count for event in blocks
        ])
        incomplete = [
            event for event in events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
        ]
        self.assertEqual(1, len(incomplete))

    def test_TC_RADIO_007_timed_tx_completion_and_underflow_terminal(self):
        fault = SoftwareRadioFault("tx", ErrorCode.RADIO_TX_UNDERFLOW,
            channel=UPPER, burst_id=burst_id(30))
        adapter, clock, events = self.make_adapter(faults=(fault,))
        self.assertTrue(adapter.schedule_tx(self.tx(burst=30)).is_ok)
        clock.advance(101_000_000)
        adapter.process_due()
        terminal = events.events_of(InternalEventType.LAYER_ERROR)[0]
        self.assertEqual(ErrorCode.RADIO_TX_UNDERFLOW, terminal.error.code)
        self.assertEqual(burst_id(30), terminal.correlation.burst_id)
        self.assertEqual(1, adapter.status(RadioStatusRequest()).value.underflow_count)

    def test_TC_RADIO_007_injected_lead_accepts_boundary_and_rejects_below(self):
        adapter, _, _ = self.make_adapter(minimum_schedule_lead_ns=100_000_000)
        boundary = self.tx(at_ns=1_100_000_000, burst=31)
        self.assertTrue(adapter.schedule_tx(boundary).is_ok)
        adapter.stop(RadioStopRequest("next"))
        adapter, _, _ = self.make_adapter(minimum_schedule_lead_ns=100_000_001)
        rejected = adapter.schedule_tx(self.tx(at_ns=1_100_000_000, burst=32))
        self.assertEqual(ErrorCode.RADIO_LATE_REQUEST, rejected.error.code)

    def test_TC_RADIO_008_finite_rx_rounding_and_generation_terminal(self):
        adapter, clock, events = self.make_adapter()
        start = utc(1_100_000_000)
        duration = 1_000_000
        expected = (duration * 76_800 + 500_000_000) // 1_000_000_000
        accepted = adapter.request_rx(RadioRxRequest(UPPER, start, duration, 76_800))
        self.assertTrue(accepted.is_ok)
        clock.advance(101_000_000)
        adapter.process_due()
        sample_event = events.events_of(InternalEventType.SAMPLES_RECEIVED)[0]
        terminal = events.events_of(InternalEventType.RADIO_RX_WINDOW_COMPLETED)[0]
        self.assertEqual(expected, sample_event.payload.samples.sample_count)
        self.assertEqual((expected, expected),
            (terminal.payload.expected_sample_count, terminal.payload.received_sample_count))
        self.assertEqual(1, terminal.payload.configure_generation)
        self.assertEqual("UTC", sample_event.payload.samples.first_sample_at.clock_domain.value)
        self.assertEqual("RADIO_HARDWARE", sample_event.payload.hardware_received_at.clock_domain.value)

    def test_TC_RADIO_008_link11_80ms_window_has_exact_6144_samples(self):
        adapter, clock, events = self.make_adapter()
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000_000, 76_800,
        )).is_ok)
        clock.advance(180_000_000)
        adapter.process_due()
        sample = events.events_of(InternalEventType.SAMPLES_RECEIVED)[0]
        self.assertEqual(6_144, sample.payload.samples.sample_count)

    def test_TC_RADIO_009_tx_rx_are_independently_outstanding(self):
        adapter, _, _ = self.make_adapter()
        self.assertTrue(adapter.schedule_tx(self.tx()).is_ok)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, utc(1_100_000_000), 1_000_000, 76_800)).is_ok)
        queue = adapter.status(RadioStatusRequest(UPPER)).value.queue_status[0]
        self.assertEqual((1, 1), (queue.tx_outstanding_count, queue.rx_window_outstanding_count))

    def test_TC_RADIO_010_event_delivery_failure_is_failed_without_retry(self):
        adapter, clock, _ = self.make_adapter(sink=_UndeliveredSink())
        self.assertTrue(adapter.schedule_tx(self.tx()).is_ok)
        clock.advance(101_000_000)
        adapter.process_due()
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual(RadioDeviceState.FAILED, status.device_state)
        self.assertTrue(status.event_delivery_status.failed)
        self.assertEqual(1, status.event_delivery_status.undelivered_terminal_count)
        recovered = adapter.stop(RadioStopRequest("delivery recovery"))
        self.assertTrue(recovered.is_ok, recovered)
        self.assertTrue(adapter.configure(RadioConfigureRequest(
            software_configuration(),
        )).is_ok)

    def test_TC_RADIO_010_nonterminal_delivery_failure_does_not_increment_terminal_count(self):
        clock = ControlledTimeSource(
            utc_nanoseconds=1_000_000_000,
            monotonic_nanoseconds=0,
            utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        sink = _FailOnceSink(InternalEventType.RADIO_STATUS_CHANGED)
        adapter = SoftwareRadioAdapter(
            clock, DeterministicIdSource(701), sink, endpoint_id="ship-a"
        )
        configured = adapter.configure(RadioConfigureRequest(software_configuration()))
        self.assertTrue(configured.is_err)
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual(RadioDeviceState.FAILED, status.device_state)
        self.assertEqual(0, status.event_delivery_status.undelivered_terminal_count)

    def test_TC_RADIO_010_sample_delivery_failure_terminalizes_active_rx_by_actual_count(self):
        sink = _FailOnceSink(InternalEventType.SAMPLES_RECEIVED)
        adapter, clock, _ = self.make_adapter(sink=sink)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 1_000_000, 76_800,
        )).is_ok)
        key = next(iter(adapter._rx_pending))
        clock.advance(101_000_000)
        adapter.process_due()
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual(RadioDeviceState.FAILED, status.device_state)
        self.assertEqual(1, status.event_delivery_status.undelivered_terminal_count)
        terminal_key = ("rx", key)
        self.assertEqual(
            ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
            adapter._terminal_codes[terminal_key],
        )

    def test_TC_RADIO_010_terminal_delivery_failure_drains_multiple_pending_operations(self):
        sink = _FailOnceSink(InternalEventType.BURST_COMPLETED)
        adapter, clock, _ = self.make_adapter(sink=sink)
        first = self.tx(at_ns=1_100_000_000, burst=720)
        second = self.tx(at_ns=1_300_000_000, burst=721)
        self.assertTrue(adapter.schedule_tx(first).is_ok)
        self.assertTrue(adapter.schedule_tx(second).is_ok)
        rx_keys = []
        for start_ns in (1_400_000_000, 1_500_000_000):
            request = RadioRxRequest(UPPER, utc(start_ns), 1_000_000, 76_800)
            self.assertTrue(adapter.request_rx(request).is_ok)
            rx_keys.append(next(
                key for key in adapter._rx_pending
                if key[2] == request.start_at.seconds
                and key[3] == request.start_at.nanoseconds
            ))

        clock.advance(101_000_000)
        adapter.process_due()
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual(RadioDeviceState.FAILED, status.device_state)
        self.assertEqual(4, status.event_delivery_status.undelivered_terminal_count)
        self.assertEqual(
            ErrorCode.RADIO_OPERATION_CANCELLED,
            adapter._terminal_codes[("tx", 1, second.burst_id.value)],
        )
        self.assertTrue(all(
            adapter._terminal_codes[("rx", key)]
            == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
            for key in rx_keys
        ))
        self.assertEqual(0, sum(
            value.tx_outstanding_count + value.rx_window_outstanding_count
            for value in status.queue_status
        ))
        self.assertEqual(0, adapter._medium.active_rx_reservation_count())

    def test_TC_RADIO_010_status_changes_are_emitted_but_polling_is_silent(self):
        adapter, _, events = self.make_adapter()
        before = len(events.events_of(InternalEventType.RADIO_STATUS_CHANGED))
        adapter.status(RadioStatusRequest())
        self.assertEqual(before, len(events.events_of(InternalEventType.RADIO_STATUS_CHANGED)))
        self.assertTrue(before >= 2)  # configure and start

    def test_TC_RADIO_010_rx_timeout_uses_rx_key_not_burst_correlation(self):
        fault = SoftwareRadioFault("rx", ErrorCode.RADIO_RX_TIMEOUT, channel=UPPER)
        adapter, clock, events = self.make_adapter(faults=(fault,))
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, utc(1_100_000_000), 1_000_000, 76_800)).is_ok)
        clock.advance(101_000_000)
        adapter.process_due()
        terminal = events.events_of(InternalEventType.LAYER_ERROR)[0]
        self.assertEqual(ErrorCode.RADIO_RX_TIMEOUT, terminal.error.code)
        self.assertIsNone(terminal.correlation.burst_id)
        self.assertEqual(1, terminal.details.get("configure_generation"))


if __name__ == "__main__":
    unittest.main()
