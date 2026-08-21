"""TC-RADIO-012/013 tests that require two physical, cabled B210 devices.

The module is inert unless VDETER_B210_HARDWARE_TEST=1. It never substitutes a
mock or SoftwareRadioAdapter for a hardware result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import platform
import time
import unittest

from tests.support.radio_stage3 import LOWER, UPPER
from vdeter.common.contracts.phy import (
    PhyDecodeRequest,
    PhyEncodeRequest,
    PhyRecoveryStatus,
)
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    RadioConfigureRequest,
    RadioDeviceState,
    RadioRxRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStopRequest,
    RadioTxRequest,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.events import InternalEventType, RecordingInternalEventSink
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.time import SystemTimeSource
from vdeter.config.models import radio_fingerprint_of
from vdeter.config.loader import load_configuration_file
from vdeter.phy.transmitter import Link11Phy
from vdeter.profiles.link_phy import build_phy_profile_registry
from vdeter.profiles.models import RegistryScope
from vdeter.radio.b210 import (
    APPROVED_GNURADIO_VERSION,
    APPROVED_UHD_VERSION,
    B210RadioAdapter,
    GrUhdFlowgraphBackend,
    approved_host_environment_issues,
    host_environment_snapshot,
    load_fixture_manifest,
)


ARMED = os.environ.get("VDETER_B210_HARDWARE_TEST") == "1"
DISCONNECT_ARMED = os.environ.get("VDETER_B210_USB_DISCONNECT_TEST") == "1"
LIFECYCLE_REPETITIONS = 10
DIRECTION_REPETITIONS = 100
FULL_DUPLEX_REPETITIONS = 100
CAPTURE_DURATION_NS = 80_000_000
CAPTURE_PRE_ROLL_NS = 26_666_667
EXPECTED_CAPTURE_SAMPLES = 6_144
SCHEDULE_AHEAD_NS = 500_000_000
WAIT_SECONDS = 5.0


def load_tests(loader, tests, pattern):
    """Keep the hardware gate out of unrelated unittest discovery runs."""
    return tests if ARMED else unittest.TestSuite()


@unittest.skipUnless(
    ARMED,
    "two physical B210 devices and an approved attenuated fixture must be explicitly armed",
)
class B210HardwareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gnuradio import gr
        from gnuradio import uhd

        cls.ship_serial = os.environ["VDETER_B210_SHIP_SERIAL"]
        cls.control_serial = os.environ["VDETER_B210_CONTROL_SERIAL"]
        if cls.ship_serial == cls.control_serial:
            raise AssertionError("the two B210 serial numbers must differ")
        host_issues = approved_host_environment_issues(host_environment_snapshot())
        if host_issues:
            raise AssertionError(
                "the approved hardware host is Ubuntu 24.04 LTS x86_64: "
                + "; ".join(host_issues)
            )
        if tuple(map(int, platform.python_version_tuple()[:2])) != (3, 12):
            raise AssertionError("the approved hardware environment requires Python 3.12")
        if gr.version() != APPROVED_GNURADIO_VERSION:
            raise AssertionError(
                f"GNU Radio must be {APPROVED_GNURADIO_VERSION}, got {gr.version()!r}"
            )
        uhd_version = str(getattr(uhd, "get_version_string", lambda: "")())
        if uhd_version != APPROVED_UHD_VERSION:
            raise AssertionError(
                f"UHD must be {APPROVED_UHD_VERSION}, got {uhd_version!r}"
            )

        root = pathlib.Path(__file__).resolve().parents[2]
        ship = load_configuration_file(root / "configs" / "stage3_b210_ship.toml")
        control = load_configuration_file(
            root / "configs" / "stage3_b210_control.toml"
        )
        if ship.is_err or control.is_err:
            raise AssertionError("operational Stage 3 B210 configs did not load")
        cls.ship_configuration = dataclasses.replace(
            ship.value.radio, device_selector=f"serial={cls.ship_serial}"
        )
        cls.control_configuration = dataclasses.replace(
            control.value.radio, device_selector=f"serial={cls.control_serial}"
        )
        cls.manifest = load_fixture_manifest(
            os.environ["VDETER_B210_FIXTURE_MANIFEST"]
        )
        serial_pairs = {
            (value.tx_serial, value.rx_serial) for value in cls.manifest.directions
        }
        if serial_pairs != {
            (cls.ship_serial, cls.control_serial),
            (cls.control_serial, cls.ship_serial),
        }:
            raise AssertionError("fixture directions do not match the two configured serials")
        if cls.manifest.source_radio_fingerprint != radio_fingerprint_of(
            cls.ship_configuration, 2
        ):
            raise AssertionError("fixture ship radio fingerprint does not match")
        if cls.manifest.receiver_radio_fingerprint != radio_fingerprint_of(
            cls.control_configuration, 2
        ):
            raise AssertionError("fixture control radio fingerprint does not match")
        configurations = {
            cls.ship_serial: cls.ship_configuration,
            cls.control_serial: cls.control_configuration,
        }
        for direction in cls.manifest.directions:
            configuration = configurations[direction.tx_serial]
            bindings = {
                value.channel.name: value
                for value in configuration.channel_bindings
            }
            tx_gains = {
                value.tx_gain_db for value in configuration.channels
                if bindings[value.channel.name].tx_enabled
            }
            if tx_gains != {direction.approved_tx_gain_db}:
                raise AssertionError(
                    "fixture approved TX gain does not match the transmitting configuration"
                )
        registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        if registry.freeze().is_err:
            raise AssertionError("Link ID 11 PHY registry could not be frozen")
        cls.registry = registry
        cls.evidence_records = []

    def _adapter(self, configuration, seed):
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            SystemTimeSource(), DeterministicIdSource(seed), events
        )
        configured = adapter.configure(RadioConfigureRequest(configuration))
        self.assertTrue(configured.is_ok, configured)
        started = adapter.start(RadioStartRequest("physical B210 test"))
        self.assertTrue(started.is_ok, started)
        return adapter, events, configured.value

    def _stop(self, *adapters):
        for adapter in adapters:
            stopped = adapter.stop(RadioStopRequest("physical B210 test cleanup"))
            self.assertTrue(stopped.is_ok, stopped)

    def _phy(self, clock, seed):
        return Link11Phy(
            self.registry,
            clock,
            DeterministicIdSource(seed),
            SAMPLE_FORMAT_CF32LE,
        )

    def _wait(self, adapters, predicate, event_sets, offsets, message):
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            for adapter in adapters:
                status = adapter.status(RadioStatusRequest())
                self.assertTrue(status.is_ok, status)
            for events, offset in zip(event_sets, offsets, strict=True):
                errors = events.events_of(InternalEventType.LAYER_ERROR)[offset:]
                if errors:
                    self.fail(f"{message}: {errors[0].error}")
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"{message}: timed out after {WAIT_SECONDS} seconds")

    def _clean_status_evidence(self, adapter, events=None):
        result = adapter.status(RadioStatusRequest())
        self.assertTrue(result.is_ok, result)
        status = result.value
        self.assertEqual(0, status.underflow_count)
        self.assertEqual(0, status.overflow_count)
        self.assertEqual(0, status.late_command_count)
        self.assertFalse(status.event_delivery_status.failed)
        outcome_unknown_count = sum(
            value == ErrorCode.RADIO_TX_OUTCOME_UNKNOWN
            for value in adapter._terminal_codes.values()
        )
        gap_count = 0 if events is None else sum(
            event.payload.samples.continuity.value != "CONTIGUOUS"
            for event in events.events_of(InternalEventType.SAMPLES_RECEIVED)
        )
        self.assertEqual(0, outcome_unknown_count)
        self.assertEqual(0, gap_count)
        self.assertTrue(all(
            value.tx_outstanding_count == 0
            and value.rx_window_outstanding_count == 0
            for value in status.queue_status
        ))
        return {
            "device_state": status.device_state.value,
            "tx_underflow_count": status.underflow_count,
            "rx_overflow_count": status.overflow_count,
            "late_command_count": status.late_command_count,
            "event_delivery_failed": status.event_delivery_status.failed,
            "undelivered_terminal_count": (
                status.event_delivery_status.undelivered_terminal_count
            ),
            "tx_outcome_unknown_count": outcome_unknown_count,
            "rx_gap_count": gap_count,
            "utc_flags": [value.value for value in status.timing_status.utc_flags],
            "mapping_uncertainty_nanoseconds": (
                status.timing_status.mapping.uncertainty_nanoseconds
            ),
            "queues": [
                {
                    "channel": value.channel.name,
                    "tx_outstanding_count": value.tx_outstanding_count,
                    "rx_window_outstanding_count": value.rx_window_outstanding_count,
                    "rx_dropped_block_count": value.rx_dropped_block_count,
                    "rx_dropped_sample_count": value.rx_dropped_sample_count,
                }
                for value in status.queue_status
            ],
        }

    @staticmethod
    def _terminal_hardware_time(event):
        return dict(event.details.entries).get("hardware_time_ns")

    @staticmethod
    def _rx_window_evidence(request, terminal):
        payload = terminal.payload
        return {
            "configure_generation": payload.configure_generation,
            "channel": request.channel.name,
            "start_seconds": request.start_at.seconds,
            "start_nanoseconds": request.start_at.nanoseconds,
            "clock_id": request.start_at.clock_id,
            "duration_nanoseconds": request.duration_nanoseconds,
            "expected_sample_count": payload.expected_sample_count,
            "received_sample_count": payload.received_sample_count,
        }

    def _wait_backend_outcome(self, backend, expected, message):
        deadline = time.monotonic() + WAIT_SECONDS
        observed = []
        while time.monotonic() < deadline:
            observed.extend(backend.poll_async())
            if any(value["outcome"] == expected for value in observed):
                return observed
            time.sleep(0.01)
        self.fail(f"{message}: {expected} not observed; got {observed}")

    @staticmethod
    def _native_backend(configuration):
        """Start the production backend with one native async consumer.

        These probes inspect raw UHD error classes.  Starting a full adapter
        here would create its required monitoring worker, which would race the
        preflight itself for the same native async queue.
        """

        stream = configuration.b210_stream
        backend = GrUhdFlowgraphBackend(
            configuration.device_selector,
            stream.cpu_format,
            stream.otw_format,
        )
        readback = backend.configure(configuration)
        backend.connect()
        backend.start()
        return backend, readback

    def _run_direction(self, source_configuration, receiver_configuration, channel, seed):
        clock = SystemTimeSource()
        source_events = RecordingInternalEventSink()
        receiver_events = RecordingInternalEventSink()
        source = B210RadioAdapter(clock, DeterministicIdSource(seed), source_events)
        receiver = B210RadioAdapter(
            clock, DeterministicIdSource(seed + 1), receiver_events
        )
        self.assertTrue(source.configure(RadioConfigureRequest(source_configuration)).is_ok)
        self.assertTrue(receiver.configure(RadioConfigureRequest(receiver_configuration)).is_ok)
        self.assertTrue(source.start(RadioStartRequest()).is_ok)
        self.assertTrue(receiver.start(RadioStartRequest()).is_ok)
        phy = self._phy(clock, seed + 2)
        packet_ids = DeterministicIdSource(seed + 3)
        try:
            for repetition in range(DIRECTION_REPETITIONS):
                payload = bytes((repetition + index) % 256 for index in range(54))
                transmit_at = clock.now_utc().shifted(SCHEDULE_AHEAD_NS)
                burst = phy.encode_packet(PhyEncodeRequest(
                    packet_ids.new_packet_id(), LinkId(11), channel,
                    transmit_at, payload,
                ))
                self.assertTrue(burst.is_ok, burst)
                rx_count = len(receiver_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                ))
                block_count = len(receiver_events.events_of(
                    InternalEventType.SAMPLES_RECEIVED
                ))
                source_error_count = len(source_events.events_of(
                    InternalEventType.LAYER_ERROR
                ))
                receiver_error_count = len(receiver_events.events_of(
                    InternalEventType.LAYER_ERROR
                ))
                rx_start = transmit_at.shifted(-CAPTURE_PRE_ROLL_NS)
                rx_request = RadioRxRequest(
                    channel, rx_start, CAPTURE_DURATION_NS, 76_800
                )
                accepted_rx = receiver.request_rx(rx_request)
                self.assertTrue(accepted_rx.is_ok, accepted_rx)
                accepted_tx = source.schedule_tx(RadioTxRequest(
                    burst.value.burst_id, channel, transmit_at, burst.value.samples
                ))
                self.assertTrue(accepted_tx.is_ok, accepted_tx)
                self._wait(
                    (source, receiver),
                    lambda: (
                        len(receiver_events.events_of(
                            InternalEventType.RADIO_RX_WINDOW_COMPLETED
                        )) > rx_count
                        and any(
                            event.correlation.burst_id == burst.value.burst_id
                            for event in source_events.events_of(
                                InternalEventType.BURST_COMPLETED
                            )
                        )
                    ),
                    (source_events, receiver_events),
                    (source_error_count, receiver_error_count),
                    f"{channel.name} repetition {repetition}",
                )
                blocks = receiver_events.events_of(InternalEventType.SAMPLES_RECEIVED)
                self.assertEqual(block_count + 1, len(blocks))
                block = blocks[-1].payload.samples
                self.assertEqual(EXPECTED_CAPTURE_SAMPLES, block.sample_count)
                tx_terminals = [
                    event for event in source_events.events_of(
                        InternalEventType.BURST_COMPLETED
                    )
                    if event.correlation.burst_id == burst.value.burst_id
                ]
                self.assertEqual(1, len(tx_terminals))
                rx_terminals = receiver_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                )[rx_count:]
                self.assertEqual(1, len(rx_terminals))
                decoded = phy.decode_samples(PhyDecodeRequest(
                    channel, block, (LinkId(11),)
                ))
                self.assertTrue(decoded.is_ok, decoded)
                self.assertEqual(PhyRecoveryStatus.RECOVERED, decoded.value.status)
                self.assertEqual(payload, decoded.value.packets[0].packet)
                expected_frequency = next(
                    value.center_frequency_hz
                    for value in source_configuration.channels
                    if value.channel == channel
                )
                tx_frequency = source._backend.tx_center_frequency_hz()
                self.assertEqual(expected_frequency, tx_frequency)
                source_status = self._clean_status_evidence(source, source_events)
                receiver_status = self._clean_status_evidence(receiver, receiver_events)
                self.evidence_records.append({
                    "test_case": "TC-RADIO-013",
                    "mode": "lower" if channel == LOWER else "upper",
                    "repetition": repetition,
                    "burst_id": burst.value.burst_id.value,
                    "channel": channel.name,
                    "packet_sha256": hashlib.sha256(payload).hexdigest(),
                    "sample_sha256": hashlib.sha256(block.data).hexdigest(),
                    "sample_format": block.sample_format,
                    "sample_rate_hz": block.sample_rate_hz,
                    "sample_count": block.sample_count,
                    "clock_id": block.first_sample_at.clock_id,
                    "continuity": block.continuity.value,
                    "recovery": decoded.value.status.value,
                    "tx_hardware_time_ns": self._terminal_hardware_time(
                        tx_terminals[0]
                    ),
                    "rx_window": self._rx_window_evidence(
                        rx_request, rx_terminals[0]
                    ),
                    "tx_success_terminal_count": len(tx_terminals),
                    "rx_success_terminal_count": len(rx_terminals),
                    "source_status": source_status,
                    "receiver_status": receiver_status,
                    "source_radio_fingerprint": radio_fingerprint_of(
                        source_configuration, 2
                    ),
                    "receiver_radio_fingerprint": radio_fingerprint_of(
                        receiver_configuration, 2
                    ),
                    "tx_center_frequency_readback_hz": tx_frequency,
                })
        finally:
            self._stop(source, receiver)

    def test_TC_RADIO_012_versions_discovery_readback_and_lifecycle_repeat_10(self):
        for configuration in (
            self.ship_configuration,
            self.control_configuration,
        ):
            adapter = B210RadioAdapter()
            previous_stop = None
            for generation in range(1, LIFECYCLE_REPETITIONS + 1):
                configured = adapter.configure(RadioConfigureRequest(configuration))
                self.assertTrue(configured.is_ok, configured)
                applied = configured.value.applied_settings
                self.assertEqual(generation, applied.configure_generation)
                self.assertEqual(
                    configuration.device_selector.split("=", 1)[1], applied.device_id
                )
                for channel in applied.channels:
                    self.assertEqual(channel.requested_center_frequency_hz,
                                     channel.applied_center_frequency_hz)
                    if channel.binding.tx_enabled:
                        self.assertEqual(76_800, channel.applied_tx_sample_rate_hz)
                    if channel.binding.rx_enabled:
                        self.assertEqual(76_800, channel.applied_rx_sample_rate_hz)
                self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
                status = adapter.status(RadioStatusRequest())
                self.assertTrue(status.is_ok, status)
                self.assertEqual(RadioDeviceState.RUNNING, status.value.device_state)
                stopped = adapter.stop(RadioStopRequest())
                repeated = adapter.stop(RadioStopRequest())
                self.assertTrue(stopped.is_ok, stopped)
                self.assertEqual(stopped.value, repeated.value)
                previous_stop = stopped.value
                self.evidence_records.append({
                    "test_case": "TC-RADIO-012",
                    "mode": "lifecycle",
                    "device_id": applied.device_id,
                    "radio_fingerprint": radio_fingerprint_of(configuration, 2),
                    "configuration_schema_version": 2,
                    "configure_generation": generation,
                    "channels": list(configured.value.channels),
                    "sample_format": configured.value.sample_format,
                    "device_state_before_stop": status.value.device_state.value,
                    "device_state_after_stop": stopped.value.device_state.value,
                    "minimum_schedule_lead_ns": configured.value.capabilities.minimum_schedule_lead_ns,
                    "minimum_rx_schedule_lead_ns": configured.value.capabilities.minimum_rx_schedule_lead_ns,
                    "minimum_tx_channel_switch_gap_ns": configured.value.capabilities.minimum_tx_channel_switch_gap_ns,
                    "mapping_uncertainty_nanoseconds": status.value.timing_status.mapping.uncertainty_nanoseconds,
                    "utc_flags": [
                        value.value for value in status.value.timing_status.utc_flags
                    ],
                    "tx_underflow_count": status.value.underflow_count,
                    "rx_overflow_count": status.value.overflow_count,
                    "late_command_count": status.value.late_command_count,
                    "applied_channels": [
                        {
                            "channel": value.channel.name,
                            "requested_center_frequency_hz": value.requested_center_frequency_hz,
                            "applied_center_frequency_hz": value.applied_center_frequency_hz,
                            "requested_sample_rate_hz": value.requested_sample_rate_hz,
                            "applied_tx_sample_rate_hz": value.applied_tx_sample_rate_hz,
                            "applied_rx_sample_rate_hz": value.applied_rx_sample_rate_hz,
                            "requested_tx_gain_db": value.requested_tx_gain_db,
                            "requested_rx_gain_db": value.requested_rx_gain_db,
                            "applied_tx_gain_db": value.applied_tx_gain_db,
                            "applied_rx_gain_db": value.applied_rx_gain_db,
                            "applied_tx_analog_bandwidth_hz": value.applied_tx_analog_bandwidth_hz,
                            "applied_rx_analog_bandwidth_hz": value.applied_rx_analog_bandwidth_hz,
                        }
                        for value in applied.channels
                    ],
                })
            self.assertIsNotNone(previous_stop)

    def test_TC_RADIO_012_negative_selector_unsupported_and_late_admission(self):
        wrong = dataclasses.replace(
            self.ship_configuration, device_selector="serial=VDETER-NOT-PRESENT"
        )
        missing = B210RadioAdapter().configure(RadioConfigureRequest(wrong))
        self.assertTrue(missing.is_err)
        self.assertEqual(ErrorCode.RADIO_DEVICE_NOT_FOUND, missing.error.code)

        changed_channel = dataclasses.replace(
            self.ship_configuration.channels[0], sample_rate_hz=48_000
        )
        unsupported_configuration = dataclasses.replace(
            self.ship_configuration,
            channels=(changed_channel,) + self.ship_configuration.channels[1:],
        )
        unsupported = B210RadioAdapter().configure(
            RadioConfigureRequest(unsupported_configuration)
        )
        self.assertTrue(unsupported.is_err)
        self.assertEqual(ErrorCode.RADIO_UNSUPPORTED_SETTING, unsupported.error.code)

        adapter, _events, _configured = self._adapter(self.ship_configuration, 12_000)
        try:
            clock = SystemTimeSource()
            transmit_at = clock.now_utc().shifted(50_000_000)
            phy = self._phy(clock, 12_001)
            burst = phy.encode_packet(PhyEncodeRequest(
                DeterministicIdSource(12_002).new_packet_id(), LinkId(11),
                LOWER, transmit_at, bytes(range(54)),
            ))
            self.assertTrue(burst.is_ok, burst)
            late = adapter.schedule_tx(RadioTxRequest(
                burst.value.burst_id, LOWER, transmit_at, burst.value.samples
            ))
            self.assertTrue(late.is_err)
            self.assertEqual(ErrorCode.RADIO_LATE_REQUEST, late.error.code)
            self.evidence_records.append({
                "test_case": "TC-RADIO-012",
                "mode": "negative_admission",
                "wrong_serial_code": missing.error.code,
                "unsupported_setting_code": unsupported.error.code,
                "late_request_code": late.error.code,
            })
        finally:
            self._stop(adapter)

    def test_TC_RADIO_012_no_tx_80ms_window_has_no_link11_burst(self):
        clock = SystemTimeSource()
        events = RecordingInternalEventSink()
        receiver = B210RadioAdapter(clock, DeterministicIdSource(12_100), events)
        self.assertTrue(receiver.configure(RadioConfigureRequest(
            self.control_configuration
        )).is_ok)
        self.assertTrue(receiver.start(RadioStartRequest()).is_ok)
        try:
            start = clock.now_utc().shifted(SCHEDULE_AHEAD_NS)
            rx_request = RadioRxRequest(
                LOWER, start, CAPTURE_DURATION_NS, 76_800
            )
            self.assertTrue(receiver.request_rx(rx_request).is_ok)
            errors_before = len(events.events_of(InternalEventType.LAYER_ERROR))
            self._wait(
                (receiver,),
                lambda: bool(events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                )),
                (events,), (errors_before,), "no-TX 80 ms window",
            )
            block = events.events_of(InternalEventType.SAMPLES_RECEIVED)[-1].payload.samples
            self.assertEqual(EXPECTED_CAPTURE_SAMPLES, block.sample_count)
            decoded = self._phy(clock, 12_101).decode_samples(PhyDecodeRequest(
                LOWER, block, (LinkId(11),)
            ))
            self.assertTrue(decoded.is_ok, decoded)
            self.assertEqual(
                PhyRecoveryStatus.NO_BURST_DETECTED, decoded.value.status
            )
            terminals = events.events_of(
                InternalEventType.RADIO_RX_WINDOW_COMPLETED
            )
            self.assertEqual(1, len(terminals))
            status_evidence = self._clean_status_evidence(receiver, events)
            self.evidence_records.append({
                "test_case": "TC-RADIO-012",
                "mode": "no_tx_window",
                "channel": LOWER.name,
                "duration_nanoseconds": CAPTURE_DURATION_NS,
                "sample_count": block.sample_count,
                "sample_sha256": hashlib.sha256(block.data).hexdigest(),
                "recovery": decoded.value.status.value,
                "rx_window": self._rx_window_evidence(rx_request, terminals[0]),
                "status": status_evidence,
            })
        finally:
            self._stop(receiver)

    @unittest.skipUnless(
        DISCONNECT_ARMED,
        "controlled USB disconnect must be separately armed and physically performed",
    )
    def test_Z_TC_RADIO_012_controlled_usb_disconnect_is_typed_and_cleanup_succeeds(self):
        adapter, events, _configured = self._adapter(
            self.control_configuration, 12_200
        )
        child_pid = adapter._backend._process.pid
        self.assertNotEqual(os.getpid(), child_pid)
        delay = int(os.environ.get("VDETER_B210_USB_DISCONNECT_DELAY_SECONDS", "10"))
        print(
            f"Disconnect USB for B210 {self.control_serial} within {delay} seconds.",
            flush=True,
        )
        time.sleep(delay)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            status = adapter.status(RadioStatusRequest())
            if status.is_ok and status.value.device_state is RadioDeviceState.FAILED:
                break
            time.sleep(0.05)
        else:
            self.fail("B210 disconnect was not observed within 30 seconds")
        errors = events.events_of(InternalEventType.LAYER_ERROR)
        self.assertTrue(any(
            event.error.code == ErrorCode.RADIO_DEVICE_DISCONNECTED
            for event in errors
        ))
        stopped = adapter.stop(RadioStopRequest("disconnect cleanup"))
        self.assertTrue(stopped.is_ok, stopped)
        self.evidence_records.append({
            "test_case": "TC-RADIO-012",
            "mode": "controlled_usb_disconnect",
            "device_id": self.control_serial,
            "terminal_code": ErrorCode.RADIO_DEVICE_DISCONNECTED,
            "parent_pid": os.getpid(),
            "gr_uhd_child_pid": child_pid,
            "parent_survived_native_disconnect": True,
            "backend_boundary": "SPAWNED_SUBPROCESS",
            "cleanup": "PASS",
        })

    def test_TC_RADIO_012_future_burst_prequeue_ack_is_exactly_once_and_ordered(self):
        clock = SystemTimeSource()
        adapter, events, configured = self._adapter(
            self.ship_configuration, 12_300
        )
        phy = self._phy(clock, 12_301)
        packet_ids = DeterministicIdSource(12_302)
        try:
            base = clock.now_utc().shifted(SCHEDULE_AHEAD_NS)
            bursts = []
            for index in range(3):
                transmit_at = base.shifted(index * 100_000_000)
                encoded = phy.encode_packet(PhyEncodeRequest(
                    packet_ids.new_packet_id(), LinkId(11), UPPER,
                    transmit_at, bytes((index + value) % 256 for value in range(54)),
                ))
                self.assertTrue(encoded.is_ok, encoded)
                bursts.append(encoded.value)
            errors_before = len(events.events_of(InternalEventType.LAYER_ERROR))
            for burst in bursts:
                self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                    burst.burst_id, UPPER, burst.samples.first_sample_at, burst.samples,
                )).is_ok)
            expected_ids = [value.burst_id for value in bursts]
            scheduled_hardware_times = [
                entry.hardware_start_ns for entry in adapter._tx_ledger
            ]
            self._wait(
                (adapter,),
                lambda: len([
                    event for event in events.events_of(
                        InternalEventType.BURST_COMPLETED
                    )
                    if event.correlation.burst_id in expected_ids
                ]) == len(expected_ids),
                (events,), (errors_before,), "future burst prequeue ACK",
            )
            terminals = [
                event for event in events.events_of(InternalEventType.BURST_COMPLETED)
                if event.correlation.burst_id in expected_ids
            ]
            observed_ids = [event.correlation.burst_id for event in terminals]
            self.assertEqual(expected_ids, observed_ids)
            self.assertTrue(all(observed_ids.count(value) == 1 for value in expected_ids))
            status = self._clean_status_evidence(adapter, events)
            self.evidence_records.append({
                "test_case": "TC-RADIO-012",
                "mode": "async_prequeue",
                "device_id": configured.applied_settings.device_id,
                "channel": UPPER.name,
                "burst_ids": [value.value for value in expected_ids],
                "scheduled_hardware_times_ns": scheduled_hardware_times,
                "ack_hardware_times_ns": [
                    self._terminal_hardware_time(value) for value in terminals
                ],
                "ack_count_per_burst": {
                    value.value: observed_ids.count(value) for value in expected_ids
                },
                "ack_order": [value.value for value in observed_ids],
                "radio_fingerprint": radio_fingerprint_of(
                    self.ship_configuration, 2
                ),
                "status": status,
            })
        finally:
            self._stop(adapter)

    def test_TC_RADIO_012_actual_async_time_error_and_underflow_are_distinct(self):
        observations = {}

        late_backend, late_readback = self._native_backend(self.ship_configuration)
        try:
            hardware_now = late_backend.hardware_time_ns()
            late_backend.submit_tx(
                bytes(8 * 64),
                max(0, hardware_now - 50_000_000),
                161_837_500,
                None,
            )
            late_native = self._wait_backend_outcome(
                late_backend, "TIME_ERROR", "actual past-time TX probe"
            )
            observations["time_error"] = late_native
        finally:
            self.assertTrue(late_backend.stop(5_000_000_000))

        underflow_backend, underflow_readback = self._native_backend(
            self.ship_configuration
        )
        try:
            underflow_backend.arm_async_underflow_preflight(8_192, 0.5)
            underflow_backend.submit_tx(
                bytes(76_800 * 2 * 8),
                underflow_backend.hardware_time_ns() + 100_000_000,
                161_837_500,
                None,
            )
            underflow_native = self._wait_backend_outcome(
                underflow_backend, "UNDERFLOW", "actual source-starvation probe"
            )
            observations["underflow"] = underflow_native
        finally:
            self.assertTrue(underflow_backend.stop(5_000_000_000))

        self.assertTrue(any(
            value["outcome"] == "TIME_ERROR"
            for value in observations["time_error"]
        ))
        self.assertTrue(any(
            value["outcome"] == "UNDERFLOW"
            for value in observations["underflow"]
        ))
        self.evidence_records.append({
            "test_case": "TC-RADIO-012",
            "mode": "async_error_distinction",
            "time_error_device_id": late_readback["serial"],
            "underflow_device_id": underflow_readback["serial"],
            "time_error_native_events": observations["time_error"],
            "underflow_native_events": observations["underflow"],
            "time_error_distinct": True,
            "underflow_distinct": True,
        })

    def test_TC_RADIO_013_lower_upper_lower_retune_preserves_upper_rx(self):
        clock = SystemTimeSource()
        ship_events = RecordingInternalEventSink()
        control_events = RecordingInternalEventSink()
        ship = B210RadioAdapter(
            clock, DeterministicIdSource(13_500), ship_events
        )
        control = B210RadioAdapter(
            clock, DeterministicIdSource(13_501), control_events
        )
        self.assertTrue(ship.configure(RadioConfigureRequest(
            self.ship_configuration
        )).is_ok)
        self.assertTrue(control.configure(RadioConfigureRequest(
            self.control_configuration
        )).is_ok)
        self.assertTrue(ship.start(RadioStartRequest()).is_ok)
        self.assertTrue(control.start(RadioStartRequest()).is_ok)
        phy = self._phy(clock, 13_502)
        packet_ids = DeterministicIdSource(13_503)
        try:
            now = clock.now_utc()
            rx_start = now.shifted(150_000_000)
            rx_request = RadioRxRequest(UPPER, rx_start, 800_000_000, 76_800)
            self.assertTrue(ship.request_rx(rx_request).is_ok)

            # Leave enough time for the Pi's first gr-uhd TX scheduler wake-up.
            # This case judges ship-side retune/RX continuity, not minimum-lead
            # capability; the 50 ms rejection is covered separately by TC-012.
            control_at = now.shifted(400_000_000)
            control_payload = bytes((0xA0 + index) % 256 for index in range(54))
            control_burst = phy.encode_packet(PhyEncodeRequest(
                packet_ids.new_packet_id(), LinkId(11), UPPER,
                control_at, control_payload,
            ))
            self.assertTrue(control_burst.is_ok, control_burst)
            self.assertTrue(control.schedule_tx(RadioTxRequest(
                control_burst.value.burst_id, UPPER, control_at,
                control_burst.value.samples,
            )).is_ok)

            channels = (LOWER, UPPER, LOWER)
            times = tuple(
                now.shifted(value) for value in (250_000_000, 550_000_000, 850_000_000)
            )
            ship_bursts = []
            ship_errors = len(ship_events.events_of(InternalEventType.LAYER_ERROR))
            control_errors = len(control_events.events_of(InternalEventType.LAYER_ERROR))
            readbacks = []
            for index, (channel, transmit_at) in enumerate(zip(channels, times, strict=True)):
                encoded = phy.encode_packet(PhyEncodeRequest(
                    packet_ids.new_packet_id(), LinkId(11), channel,
                    transmit_at, bytes((index * 31 + value) % 256 for value in range(54)),
                ))
                self.assertTrue(encoded.is_ok, encoded)
                ship_bursts.append(encoded.value)
                self.assertTrue(ship.schedule_tx(RadioTxRequest(
                    encoded.value.burst_id, channel, transmit_at,
                    encoded.value.samples,
                )).is_ok)
                self._wait(
                    (ship, control),
                    lambda target=transmit_at.shifted(50_000_000): (
                        clock.now_utc() >= target
                    ),
                    (ship_events, control_events),
                    (ship_errors, control_errors),
                    f"retune readback {index}",
                )
                readback = ship._backend.tx_center_frequency_hz()
                expected = 157_237_500 if channel == LOWER else 161_837_500
                self.assertEqual(expected, readback)
                readbacks.append(readback)

            expected_ship_ids = [value.burst_id for value in ship_bursts]
            self._wait(
                (ship, control),
                lambda: (
                    len(ship_events.events_of(
                        InternalEventType.RADIO_RX_WINDOW_COMPLETED
                    )) == 1
                    and all(
                        sum(
                            event.correlation.burst_id == burst_id
                            for event in ship_events.events_of(
                                InternalEventType.BURST_COMPLETED
                            )
                        ) == 1
                        for burst_id in expected_ship_ids
                    )
                    and sum(
                        event.correlation.burst_id == control_burst.value.burst_id
                        for event in control_events.events_of(
                            InternalEventType.BURST_COMPLETED
                        )
                    ) == 1
                ),
                (ship_events, control_events),
                (ship_errors, control_errors),
                "lower-upper-lower retune with upper RX",
            )
            blocks = ship_events.events_of(InternalEventType.SAMPLES_RECEIVED)
            self.assertEqual(1, len(blocks))
            block = blocks[0].payload.samples
            self.assertEqual(61_440, block.sample_count)
            self.assertEqual("CONTIGUOUS", block.continuity.value)
            decoded = phy.decode_samples(PhyDecodeRequest(
                UPPER, block, (LinkId(11),)
            ))
            self.assertTrue(decoded.is_ok, decoded)
            self.assertEqual(PhyRecoveryStatus.RECOVERED, decoded.value.status)
            self.assertIn(control_payload, [value.packet for value in decoded.value.packets])
            ship_terminals = [
                event for event in ship_events.events_of(
                    InternalEventType.BURST_COMPLETED
                )
                if event.correlation.burst_id in expected_ship_ids
            ]
            self.assertEqual(expected_ship_ids, [
                value.correlation.burst_id for value in ship_terminals
            ])
            rx_terminal = ship_events.events_of(
                InternalEventType.RADIO_RX_WINDOW_COMPLETED
            )[0]
            ship_status = self._clean_status_evidence(ship, ship_events)
            control_status = self._clean_status_evidence(control, control_events)
            self.evidence_records.append({
                "test_case": "TC-RADIO-013",
                "mode": "timed_retune_upper_rx",
                "channels": [value.name for value in channels],
                "burst_ids": [value.value for value in expected_ship_ids],
                "scheduled_utc": [
                    {
                        "seconds": value.seconds,
                        "nanoseconds": value.nanoseconds,
                        "clock_id": value.clock_id,
                    }
                    for value in times
                ],
                "ack_hardware_times_ns": [
                    self._terminal_hardware_time(value) for value in ship_terminals
                ],
                "retune_readbacks_hz": readbacks,
                "upper_rx_window": self._rx_window_evidence(
                    rx_request, rx_terminal
                ),
                "upper_rx_sample_sha256": hashlib.sha256(block.data).hexdigest(),
                "upper_rx_sample_format": block.sample_format,
                "upper_rx_sample_rate_hz": block.sample_rate_hz,
                "upper_rx_sample_count": block.sample_count,
                "upper_rx_clock_id": block.first_sample_at.clock_id,
                "upper_rx_continuity": block.continuity.value,
                "control_burst_id": control_burst.value.burst_id.value,
                "control_packet_sha256": hashlib.sha256(control_payload).hexdigest(),
                "control_recovery": decoded.value.status.value,
                "ship_status": ship_status,
                "control_status": control_status,
                "ship_radio_fingerprint": radio_fingerprint_of(
                    self.ship_configuration, 2
                ),
                "control_radio_fingerprint": radio_fingerprint_of(
                    self.control_configuration, 2
                ),
            })
        finally:
            self._stop(ship, control)

    def test_TC_RADIO_013_lower_link11_known_vector_repeat_100(self):
        self._run_direction(
            self.ship_configuration, self.control_configuration, LOWER, 13_000
        )

    def test_TC_RADIO_013_upper_link11_known_vector_repeat_100(self):
        self._run_direction(
            self.control_configuration, self.ship_configuration, UPPER, 14_000
        )

    def test_TC_RADIO_013_full_duplex_known_vector_pairs_repeat_100(self):
        clock = SystemTimeSource()
        ship_events = RecordingInternalEventSink()
        control_events = RecordingInternalEventSink()
        ship = B210RadioAdapter(clock, DeterministicIdSource(15_000), ship_events)
        control = B210RadioAdapter(
            clock, DeterministicIdSource(15_001), control_events
        )
        self.assertTrue(ship.configure(RadioConfigureRequest(
            self.ship_configuration
        )).is_ok)
        self.assertTrue(control.configure(RadioConfigureRequest(
            self.control_configuration
        )).is_ok)
        self.assertTrue(ship.start(RadioStartRequest()).is_ok)
        self.assertTrue(control.start(RadioStartRequest()).is_ok)
        phy = self._phy(clock, 15_002)
        packet_ids = DeterministicIdSource(15_003)
        try:
            for repetition in range(FULL_DUPLEX_REPETITIONS):
                lower_payload = bytes((repetition + i) % 256 for i in range(54))
                upper_payload = bytes((255 - repetition - i) % 256 for i in range(54))
                transmit_at = clock.now_utc().shifted(SCHEDULE_AHEAD_NS)
                lower = phy.encode_packet(PhyEncodeRequest(
                    packet_ids.new_packet_id(), LinkId(11), LOWER,
                    transmit_at, lower_payload,
                ))
                upper = phy.encode_packet(PhyEncodeRequest(
                    packet_ids.new_packet_id(), LinkId(11), UPPER,
                    transmit_at, upper_payload,
                ))
                self.assertTrue(lower.is_ok, lower)
                self.assertTrue(upper.is_ok, upper)
                ship_blocks = len(ship_events.events_of(InternalEventType.SAMPLES_RECEIVED))
                control_blocks = len(control_events.events_of(InternalEventType.SAMPLES_RECEIVED))
                ship_rx_terminals = len(ship_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                ))
                control_rx_terminals = len(control_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                ))
                ship_errors = len(ship_events.events_of(InternalEventType.LAYER_ERROR))
                control_errors = len(control_events.events_of(InternalEventType.LAYER_ERROR))
                rx_start = transmit_at.shifted(-CAPTURE_PRE_ROLL_NS)
                control_rx_request = RadioRxRequest(
                    LOWER, rx_start, CAPTURE_DURATION_NS, 76_800
                )
                ship_rx_request = RadioRxRequest(
                    UPPER, rx_start, CAPTURE_DURATION_NS, 76_800
                )
                self.assertTrue(control.request_rx(control_rx_request).is_ok)
                self.assertTrue(ship.request_rx(ship_rx_request).is_ok)
                self.assertTrue(ship.schedule_tx(RadioTxRequest(
                    lower.value.burst_id, LOWER, transmit_at, lower.value.samples
                )).is_ok)
                self.assertTrue(control.schedule_tx(RadioTxRequest(
                    upper.value.burst_id, UPPER, transmit_at, upper.value.samples
                )).is_ok)
                self._wait(
                    (ship, control),
                    lambda: (
                        len(ship_events.events_of(InternalEventType.SAMPLES_RECEIVED))
                        > ship_blocks
                        and len(control_events.events_of(
                            InternalEventType.SAMPLES_RECEIVED
                        )) > control_blocks
                        and len(ship_events.events_of(
                            InternalEventType.RADIO_RX_WINDOW_COMPLETED
                        )) > ship_rx_terminals
                        and len(control_events.events_of(
                            InternalEventType.RADIO_RX_WINDOW_COMPLETED
                        )) > control_rx_terminals
                        and any(
                            event.correlation.burst_id == lower.value.burst_id
                            for event in ship_events.events_of(
                                InternalEventType.BURST_COMPLETED
                            )
                        )
                        and any(
                            event.correlation.burst_id == upper.value.burst_id
                            for event in control_events.events_of(
                                InternalEventType.BURST_COMPLETED
                            )
                        )
                    ),
                    (ship_events, control_events),
                    (ship_errors, control_errors),
                    f"full-duplex repetition {repetition}",
                )
                ship_block = ship_events.events_of(
                    InternalEventType.SAMPLES_RECEIVED
                )[-1].payload.samples
                control_block = control_events.events_of(
                    InternalEventType.SAMPLES_RECEIVED
                )[-1].payload.samples
                decoded_upper = phy.decode_samples(PhyDecodeRequest(
                    UPPER, ship_block, (LinkId(11),)
                ))
                decoded_lower = phy.decode_samples(PhyDecodeRequest(
                    LOWER, control_block, (LinkId(11),)
                ))
                self.assertTrue(decoded_upper.is_ok, decoded_upper)
                self.assertTrue(decoded_lower.is_ok, decoded_lower)
                self.assertEqual(upper_payload, decoded_upper.value.packets[0].packet)
                self.assertEqual(lower_payload, decoded_lower.value.packets[0].packet)
                ship_tx_terminals = [
                    event for event in ship_events.events_of(
                        InternalEventType.BURST_COMPLETED
                    )
                    if event.correlation.burst_id == lower.value.burst_id
                ]
                control_tx_terminals = [
                    event for event in control_events.events_of(
                        InternalEventType.BURST_COMPLETED
                    )
                    if event.correlation.burst_id == upper.value.burst_id
                ]
                self.assertEqual(1, len(ship_tx_terminals))
                self.assertEqual(1, len(control_tx_terminals))
                ship_rx_terminals = ship_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                )[ship_rx_terminals:]
                control_rx_terminals = control_events.events_of(
                    InternalEventType.RADIO_RX_WINDOW_COMPLETED
                )[control_rx_terminals:]
                self.assertEqual(1, len(ship_rx_terminals))
                self.assertEqual(1, len(control_rx_terminals))
                ship_tx_frequency = ship._backend.tx_center_frequency_hz()
                control_tx_frequency = control._backend.tx_center_frequency_hz()
                self.assertEqual(157_237_500, ship_tx_frequency)
                self.assertEqual(161_837_500, control_tx_frequency)
                ship_status = self._clean_status_evidence(ship, ship_events)
                control_status = self._clean_status_evidence(control, control_events)
                self.evidence_records.append({
                    "test_case": "TC-RADIO-013",
                    "mode": "full_duplex",
                    "repetition": repetition,
                    "lower_burst_id": lower.value.burst_id.value,
                    "upper_burst_id": upper.value.burst_id.value,
                    "lower_packet_sha256": hashlib.sha256(lower_payload).hexdigest(),
                    "upper_packet_sha256": hashlib.sha256(upper_payload).hexdigest(),
                    "lower_sample_sha256": hashlib.sha256(control_block.data).hexdigest(),
                    "upper_sample_sha256": hashlib.sha256(ship_block.data).hexdigest(),
                    "sample_format": SAMPLE_FORMAT_CF32LE,
                    "sample_rate_hz": 76_800,
                    "sample_count_per_direction": EXPECTED_CAPTURE_SAMPLES,
                    "lower_recovery": decoded_lower.value.status.value,
                    "upper_recovery": decoded_upper.value.status.value,
                    "lower_tx_hardware_time_ns": self._terminal_hardware_time(
                        ship_tx_terminals[0]
                    ),
                    "upper_tx_hardware_time_ns": self._terminal_hardware_time(
                        control_tx_terminals[0]
                    ),
                    "ship_rx_window": self._rx_window_evidence(
                        ship_rx_request, ship_rx_terminals[0]
                    ),
                    "control_rx_window": self._rx_window_evidence(
                        control_rx_request, control_rx_terminals[0]
                    ),
                    "ship_status": ship_status,
                    "control_status": control_status,
                    "ship_tx_center_frequency_readback_hz": ship_tx_frequency,
                    "control_tx_center_frequency_readback_hz": control_tx_frequency,
                })
        finally:
            self._stop(ship, control)


if __name__ == "__main__":
    unittest.main()
