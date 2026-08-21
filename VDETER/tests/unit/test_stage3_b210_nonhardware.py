"""B210 validation/dependency/configuration tests that do not claim hardware PASS."""

from __future__ import annotations

import pathlib
import hashlib
import json
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from dataclasses import replace

from tests.support.radio_stage3 import LOWER, UPPER, b210_configuration, burst_id, cf32_block, limits, utc
from vdeter.common.contracts.radio import (
    RadioConfigureRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStopRequest,
    RadioRxRequest,
    RadioTxRequest,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.events import EventPublished, InternalEventType, RecordingInternalEventSink
from vdeter.common.identifiers import BurstId, DeterministicIdSource
from vdeter.common.time import TestTimeSource as ControlledTimeSource
from vdeter.radio.b210 import (
    APT_PROFILE_ID,
    B210FixtureManifest,
    B210RadioAdapter,
    FixtureValidationProfile,
    GrUhdFlowgraphBackend,
    FixtureComponent,
    FixtureDirection,
    LossEvidence,
    NOMINAL_30DB_RISK_ACCEPTANCE_ID,
    TxPowerEvidence,
    apt_package_manifest_issues,
    apt_package_snapshot_issues,
    approved_host_environment_issues,
    load_fixture_manifest,
    live_raspberry_pi_usb_snapshot,
    missing_dependencies,
)
from vdeter.radio.b210.backend import _RxWindowAssembler
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.results import Ok


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


class _Backend:
    instances = []
    discovery = None
    mutate_readback = None

    @staticmethod
    def discover(selector):
        return _Backend.discovery or (selector.split("=", 1)[1],)

    def __init__(self, selector, cpu_format, otw_format):
        self.selector = selector
        self.started = False
        self.connected = False
        self.hardware_reads_at_connect = None
        self.call_order = []
        self.tx = []
        self.rx = []
        self.async_events = []
        self.read_results = []
        self.read_results_by_window = {}
        self.hardware_ns = 0
        self.hardware_reads = 0
        self.hardware_error = None
        self.read_error = None
        self.poll_error = None
        self.activity_wait_entered = None
        self.activity_wait_release = None
        self.closed = False
        self.stop_result = True
        self.stop_entered = None
        self.stop_release = None
        _Backend.instances.append(self)

    def configure(self, configuration):
        self.call_order.append("configure")
        serial = self.selector.split("=", 1)[1]
        bindings = {value.channel.name: value for value in configuration.channel_bindings}
        channels = {}
        initial_tx = None
        initial_rx = None
        for setting in configuration.channels:
            binding = bindings[setting.channel.name]
            channels[setting.channel.name] = {
                "center_frequency_hz": setting.center_frequency_hz,
                "tx_center_frequency_hz": (
                    setting.center_frequency_hz if binding.tx_enabled else None
                ),
                "rx_center_frequency_hz": (
                    setting.center_frequency_hz if binding.rx_enabled else None
                ),
                "tx_sample_rate_hz": setting.sample_rate_hz if binding.tx_enabled else None,
                "rx_sample_rate_hz": setting.sample_rate_hz if binding.rx_enabled else None,
                "tx_gain_db": setting.tx_gain_db if binding.tx_enabled else None,
                "rx_gain_db": setting.rx_gain_db if binding.rx_enabled else None,
                "tx_bandwidth_hz": binding.analog_bandwidth_hz if binding.tx_enabled else None,
                "rx_bandwidth_hz": binding.analog_bandwidth_hz if binding.rx_enabled else None,
                "tx_antenna": binding.tx_antenna,
                "rx_antenna": binding.rx_antenna,
                "tx_device_channel": binding.tx_device_channel,
                "rx_device_channel": binding.rx_device_channel,
            }
            if binding.tx_enabled:
                initial_tx = setting.channel.name
            if binding.rx_enabled:
                initial_rx = setting.channel.name
        result = {
            "serial": serial,
            "sink_serial": serial,
            "same_timebase": True,
            "master_clock_rate_hz": 30_720_000,
            "sink_master_clock_rate_hz": 30_720_000,
            "clock_source": "internal",
            "time_source": "internal",
            "sink_clock_source": "internal",
            "sink_time_source": "internal",
            "hardware_time_reset": True,
            "cpu_format": "fc32",
            "otw_format": "sc16",
            "channels": channels,
            "initial_tx_channel": initial_tx,
            "initial_rx_channel": initial_rx,
        }
        if _Backend.mutate_readback is not None:
            _Backend.mutate_readback(result)
        return result

    def connect(self):
        self.call_order.append("connect")
        self.hardware_reads_at_connect = self.hardware_reads
        self.connected = True

    def hardware_time_ns(self):
        if self.hardware_error is not None:
            raise self.hardware_error
        self.hardware_reads += 1
        return self.hardware_ns

    def start(self):
        if not self.connected:
            raise RuntimeError("backend must be connected before start")
        self.call_order.append("start")
        self.started = True

    def stop(self, timeout_ns=None):
        if self.stop_entered is not None:
            self.stop_entered.set()
        if self.stop_release is not None:
            self.stop_release.wait(1.0)
        self.started = False
        return self.stop_result

    def close(self, timeout_ns):
        self.closed = True
        return True

    def submit_tx(self, data, hardware_time_ns, center_frequency_hz, retune_time_ns):
        self.tx.append((bytes(data), hardware_time_ns, center_frequency_hz, retune_time_ns))

    def submit_rx(self, hardware_time_ns, sample_count, center_frequency_hz):
        self.rx.append((hardware_time_ns, sample_count, center_frequency_hz))

    def poll_async(self):
        if self.poll_error is not None:
            raise self.poll_error
        result, self.async_events = tuple(self.async_events), []
        return result

    def wait_for_activity(self, timeout_seconds):
        if self.activity_wait_entered is not None:
            self.activity_wait_entered.set()
        if self.activity_wait_release is not None:
            self.activity_wait_release.wait(1.0)
        else:
            time.sleep(min(timeout_seconds, 0.001))

    def read_rx_cf32(
        self,
        sample_count,
        *,
        allow_partial=False,
        expected_hardware_time_ns=None,
    ):
        if self.read_error is not None:
            raise self.read_error
        if expected_hardware_time_ns in self.read_results_by_window:
            return self.read_results_by_window.pop(expected_hardware_time_ns)
        return self.read_results.pop(0) if self.read_results else None


class B210NonHardwareTest(unittest.TestCase):
    def test_TC_RADIO_012_ubuntu_host_profile_is_exact_and_portable(self):
        approved = {
            "system": "Linux",
            "machine": "x86_64",
            "kernel_release": "6.8.0-test",
            "virtualization": "none",
            "hardware_model": "",
            "os_id": "ubuntu",
            "os_version_id": "24.04",
            "os_pretty_name": "Ubuntu 24.04 LTS",
            "python_version": "3.12.8",
        }
        self.assertEqual((), approved_host_environment_issues(approved))
        rejected = dict(approved, os_version_id="26.04")
        self.assertIn(
            "Ubuntu VERSION_ID must be 24.04",
            approved_host_environment_issues(rejected),
        )
        raspberry_pi = dict(
            approved,
            machine="aarch64",
            hardware_model="Raspberry Pi 5 Model B Rev 1.0",
        )
        self.assertEqual((), approved_host_environment_issues(raspberry_pi))
        wrong_arm_host = dict(raspberry_pi, hardware_model="Generic ARM64 Host")
        self.assertIn(
            "arm64 physical B210 host must be Raspberry Pi 5",
            approved_host_environment_issues(wrong_arm_host),
        )
        wsl = dict(
            approved,
            kernel_release="5.15.167.4-microsoft-standard-WSL2",
            virtualization="wsl",
        )
        self.assertIn(
            "WSL is not an approved physical B210 host",
            approved_host_environment_issues(wsl),
        )

    def test_TC_RADIO_012_ubuntu_apt_manifest_matches_runtime_and_holds(self):
        host = {
            "system": "Linux",
            "machine": "x86_64",
            "kernel_release": "6.8.0-test",
            "virtualization": "none",
            "hardware_model": "",
            "os_id": "ubuntu",
            "os_version_id": "24.04",
            "os_pretty_name": "Ubuntu 24.04 LTS",
            "python_version": "3.12.3",
        }
        packages = {
            "python": {
                "name": "python3", "version": "3.12.3-0ubuntu2", "architecture": "amd64",
            },
            "gnuradio": {
                "name": "gnuradio", "version": "3.10.9.2-1.1ubuntu2", "architecture": "amd64",
            },
            "gnuradio_uhd": {
                "name": "libgnuradio-uhd3.10.9t64",
                "version": "3.10.9.2-1.1ubuntu2", "architecture": "amd64",
            },
            "uhd_host": {
                "name": "uhd-host",
                "version": "4.6.0.0+ds1-5.1ubuntu0.24.04.1", "architecture": "amd64",
            },
            "uhd_runtime": {
                "name": "libuhd4.6.0t64",
                "version": "4.6.0.0+ds1-5.1ubuntu0.24.04.1", "architecture": "amd64",
            },
        }
        runtime = {
            "python": "3.12.3",
            "gnuradio": "3.10.9.2",
            "uhd": "4.6.0.0+ds1-5.1ubuntu0.24.04.1",
        }
        holds = sorted(record["name"] for record in packages.values())
        manifest = {
            "schema_version": 1,
            "profile_id": APT_PROFILE_ID,
            "created_at_utc": "2026-08-21T00:00:00Z",
            "host_environment": host,
            "runtime_versions": runtime,
            "packages": packages,
            "held_packages": holds,
            "outcome": "PASS",
        }
        self.assertEqual((), apt_package_snapshot_issues(packages, "x86_64"))
        self.assertEqual(
            (), apt_package_manifest_issues(manifest, host, runtime, packages, holds)
        )
        rejected = dict(manifest, held_packages=holds[:-1])
        self.assertTrue(
            apt_package_manifest_issues(rejected, host, runtime, packages, holds)
        )
        arm_packages = {
            role: dict(record, architecture="arm64")
            for role, record in packages.items()
        }
        arm_host = dict(
            host,
            machine="aarch64",
            hardware_model="Raspberry Pi 5 Model B Rev 1.0",
        )
        arm_manifest = dict(
            manifest,
            host_environment=arm_host,
            packages=arm_packages,
        )
        self.assertEqual(
            (), apt_package_manifest_issues(
                arm_manifest, arm_host, runtime, arm_packages, holds
            )
        )

    def test_TC_RADIO_012_raspberry_pi_5_usb_power_and_topology_gate(self):
        host = {"machine": "aarch64"}
        completed = (
            __import__("subprocess").CompletedProcess(
                (), 0, "usb_max_current_enable=1\n", ""
            ),
            __import__("subprocess").CompletedProcess((), 0, "throttled=0x0\n", ""),
            __import__("subprocess").CompletedProcess(
                (), 0, "|__ Port 1: Dev 2, 5000M\n|__ Port 2: Dev 3, 5000M\n", ""
            ),
        )
        with mock.patch("vdeter.radio.b210.environment.subprocess.run", side_effect=completed):
            snapshot, issues = live_raspberry_pi_usb_snapshot(host)
        self.assertEqual((), issues)
        self.assertTrue(snapshot["applicable"])

        insufficient = (
            __import__("subprocess").CompletedProcess(
                (), 0, "usb_max_current_enable=0\n", ""
            ),
            __import__("subprocess").CompletedProcess((), 0, "throttled=0x50005\n", ""),
            __import__("subprocess").CompletedProcess(
                (), 0, "|__ Port 1: Dev 2, 5000M\n", ""
            ),
        )
        with mock.patch("vdeter.radio.b210.environment.subprocess.run", side_effect=insufficient):
            _, issues = live_raspberry_pi_usb_snapshot(host)
        self.assertEqual(3, len(issues))

    def setUp(self):
        _Backend.instances.clear()
        _Backend.discovery = None
        _Backend.mutate_readback = None

    def test_b210_discovery_uses_uhd_host_for_broken_gr_uhd_wrapper(self):
        selector = "serial=34D03DD"
        gr_uhd = types.SimpleNamespace(
            find_devices=mock.Mock(
                side_effect=AttributeError(
                    "module 'gnuradio.uhd.uhd_python' has no attribute "
                    "'find_devices_raw'"
                )
            )
        )
        completed = __import__("subprocess").CompletedProcess(
            (),
            0,
            "Device Address:\n    serial: 34D03DD\n    type: b200\n",
            "",
        )

        with (
            mock.patch(
                "vdeter.radio.b210.backend.subprocess.run",
                return_value=completed,
            ) as run,
            mock.patch(
                "vdeter.radio.b210.backend.importlib.import_module",
                return_value=gr_uhd,
            ),
        ):
            found = GrUhdFlowgraphBackend.discover(selector)

        self.assertEqual(("34D03DD",), found)
        run.assert_called_once_with(
            ["uhd_find_devices", "--args=serial=34D03DD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_b210_discovery_does_not_mask_unrelated_attribute_error(self):
        gr_uhd = types.SimpleNamespace(
            find_devices=mock.Mock(side_effect=AttributeError("other failure"))
        )
        with mock.patch(
            "vdeter.radio.b210.backend.importlib.import_module",
            return_value=gr_uhd,
        ):
            with self.assertRaisesRegex(AttributeError, "other failure"):
                GrUhdFlowgraphBackend.discover("serial=34D03DD")

    def test_b210_discovery_maps_uhd_host_no_devices_to_empty_result(self):
        gr_uhd = types.SimpleNamespace(
            find_devices=mock.Mock(
                side_effect=AttributeError("no attribute 'find_devices_raw'")
            )
        )
        completed = __import__("subprocess").CompletedProcess(
            (), 1, "No UHD Devices Found\n", ""
        )
        with (
            mock.patch(
                "vdeter.radio.b210.backend.subprocess.run",
                return_value=completed,
            ),
            mock.patch(
                "vdeter.radio.b210.backend.importlib.import_module",
                return_value=gr_uhd,
            ),
        ):
            self.assertEqual(
                (), GrUhdFlowgraphBackend.discover("serial=VDETER-NOT-PRESENT")
            )

    def test_b210_discovery_error_records_exception_message(self):
        class _DiscoveryFailureBackend(_Backend):
            @staticmethod
            def discover(selector):
                raise AttributeError("specific discovery failure")

        adapter = B210RadioAdapter(backend_factory=_DiscoveryFailureBackend)
        failed = adapter.configure(RadioConfigureRequest(b210_configuration()))

        self.assertEqual(ErrorCode.RADIO_CONFIGURATION_FAILED, failed.error.code)
        self.assertEqual(
            "specific discovery failure",
            dict(failed.error.details.entries)["exception_message"],
        )

    def test_b210_block_error_records_exception_message(self):
        class _BlockFailureBackend:
            @staticmethod
            def discover(selector):
                return (selector.split("=", 1)[1],)

            def __init__(self, selector, cpu_format, otw_format):
                raise AttributeError("specific gr-uhd block failure")

        adapter = B210RadioAdapter(backend_factory=_BlockFailureBackend)
        failed = adapter.configure(RadioConfigureRequest(b210_configuration()))

        self.assertEqual(ErrorCode.RADIO_CONFIGURATION_FAILED, failed.error.code)
        self.assertEqual(
            "specific gr-uhd block failure",
            dict(failed.error.details.entries)["exception_message"],
        )

    @staticmethod
    def _wait_until(predicate, timeout=0.75):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("asynchronous B210 condition was not reached")

    def test_TC_RADIO_008_rx_assembler_correlates_by_time_not_registration_order(self):
        assembler = _RxWindowAssembler(sample_rate_hz=1_000)
        first_start = 100_000_000
        second_start = 110_000_000
        assembler.register_window(second_start, 4)
        assembler.register_window(first_start, 4)
        second = b"\x22" * 32
        assembler.feed(second, ((0, second_start),))

        missed = assembler.read(4, False, first_start)
        received = assembler.read(4, False, second_start)
        self.assertEqual((), missed["segments"])
        self.assertEqual(((second, second_start, False),), received["segments"])

    def test_TC_RADIO_008_rx_assembler_keeps_resumed_timestamp_in_same_window(self):
        assembler = _RxWindowAssembler(sample_rate_hz=1_000)
        start = 100_000_000
        assembler.register_window(start, 4)
        assembler.feed(b"\x11" * 16, ((0, start),))
        assembler.feed(b"\x22" * 16, ((0, start + 2_000_000),))
        received = assembler.read(4, False, start)
        self.assertEqual(
            (
                (b"\x11" * 16, start, False),
                (b"\x22" * 16, start + 2_000_000, True),
            ),
            received["segments"],
        )

    def test_TC_RADIO_010_rx_assembler_rejects_uncorrelated_native_timestamp(self):
        assembler = _RxWindowAssembler(sample_rate_hz=1_000)
        start = 100_000_000
        assembler.register_window(start, 4)
        assembler.feed(b"\x33" * 32, ((0, 200_000_000),))
        with self.assertRaises(RuntimeError):
            assembler.read(4, True, start)

    def test_TC_RADIO_012_dependency_failure_is_structured_and_import_isolated(self):
        if not missing_dependencies():
            self.skipTest("approved GNU Radio environment is present")
        adapter = B210RadioAdapter()
        result = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.RADIO_DEPENDENCY_MISSING, result.error.code)

    def test_TC_RADIO_001_programmatic_backend_has_required_gr_uhd_calls_and_no_multiusrp_or_grc(self):
        path = pathlib.Path(__file__).parents[2] / "src" / "vdeter" / "radio" / "b210" / "backend.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("uhd.usrp_source", source)
        self.assertIn("uhd.usrp_sink", source)
        self.assertIn("issue_stream_cmd", source)
        self.assertIn("STREAM_MODE_NUM_SAMPS_AND_DONE", source)
        self.assertIn("stream_mode_t.STREAM_MODE_NUM_SAMPS_AND_DONE", source)
        self.assertIn("collector_self.receive", source)
        self.assertIn("set_command_time", source)
        self.assertIn("clear_command_time", source)
        self.assertIn("set_clock_rate", source)
        self.assertIn("get_clock_rate", source)
        self.assertNotIn("set_master_clock_rate", source)
        self.assertNotIn("get_master_clock_rate", source)
        self.assertIn("def tx_center_frequency_hz", source)
        self.assertNotIn("MultiUSRP", source.replace("``MultiUSRP``", ""))
        self.assertNotIn("import radio_bringup", source)

    def test_TC_RADIO_012_fixture_preflight_requires_two_safe_40db_paths(self):
        component = FixtureComponent(
            "attenuator-a", "ATTENUATOR", 40_000, 0, 40_000,
            LossEvidence.MANUFACTURER_GUARANTEE, 100_000_000, 200_000_000, 30_000,
        )
        first = FixtureDirection("A", "TX/RX", "B", "RX2", (component,), 20_000, 0)
        second = FixtureDirection("B", "TX/RX", "A", "RX2", (component,), 20_000, 0)
        manifest = B210FixtureManifest(
            "fixture", "sha256:source", "sha256:receiver",
            (157_237_500, 161_837_500), (first, second),
            "2026-08-21T00:00:00Z", "0.2.3", "PASS",
        )
        self.assertEqual(-20_000, manifest.directions[0].p_rx_worst_millidbm)
        unsafe = FixtureComponent(
            "attenuator-b", "ATTENUATOR", 39_000, 0, 39_000,
            LossEvidence.MANUFACTURER_GUARANTEE, 100_000_000, 200_000_000, 30_000,
        )
        with self.assertRaises(InvalidValueError):
            B210FixtureManifest(
                "unsafe", "sha256:source", "sha256:receiver",
                (157_237_500, 161_837_500),
                (FixtureDirection("A", "TX/RX", "B", "RX2", (unsafe,), 20_000, 0), second),
                "2026-08-21T00:00:00Z", "0.2.3", "PASS",
            )

    def test_TC_RADIO_012_calibrated_tx_bound_can_secure_a_30db_fixture(self):
        with self.assertRaises(InvalidValueError):
            FixtureComponent(
                "unsafe-30db-claim", "ATTENUATOR", 30_000, 1_000, 30_000,
                LossEvidence.MANUFACTURER_GUARANTEE,
                100_000_000, 200_000_000, 30_000,
            )
        component = FixtureComponent(
            "attenuator-30db", "ATTENUATOR", 30_000, 0, 30_000,
            LossEvidence.MANUFACTURER_GUARANTEE,
            100_000_000, 200_000_000, 30_000,
        )
        digest = "sha256:" + "0" * 64
        calibrated = FixtureDirection(
            "A", "TX/RX", "B", "RX2", (component,), 10_000, 0,
            TxPowerEvidence.CALIBRATED_MEASUREMENT,
            "calibration/a.json", digest,
        )
        reverse = FixtureDirection(
            "B", "TX/RX", "A", "RX2", (component,), 10_000, 0,
            TxPowerEvidence.CALIBRATED_MEASUREMENT,
            "calibration/b.json", digest,
        )
        manifest = B210FixtureManifest(
            "fixture-30db", "sha256:source", "sha256:receiver",
            (157_237_500, 161_837_500), (calibrated, reverse),
            "2026-08-21T00:00:00Z", "0.2.3", "PASS",
        )
        self.assertEqual(-20_000, manifest.directions[0].p_rx_worst_millidbm)

        with self.assertRaises(InvalidValueError):
            FixtureDirection(
                "A", "TX/RX", "B", "RX2", (component,), 10_000, 0,
            )

    def test_TC_RADIO_012_user_accepted_nominal_30db_conducted_profile_is_narrow(self):
        root = pathlib.Path(__file__).parents[2]
        manifest = load_fixture_manifest(
            root
            / "configs"
            / "stage3_b210_fixture_manifest.30db-user-accepted.json"
        )

        self.assertEqual(
            FixtureValidationProfile.USER_ACCEPTED_NOMINAL_30DB_CONDUCTED,
            manifest.validation_profile,
        )
        self.assertEqual(
            NOMINAL_30DB_RISK_ACCEPTANCE_ID,
            manifest.risk_acceptance_id,
        )
        self.assertEqual({"34D03DD", "34D6357"}, {
            value for direction in manifest.directions
            for value in (direction.tx_serial, direction.rx_serial)
        })
        self.assertEqual(2, len({
            direction.components[0].component_id
            for direction in manifest.directions
        }))
        for direction in manifest.directions:
            self.assertEqual(0, direction.approved_tx_gain_db)
            self.assertEqual(20_000, direction.p_tx_bound_millidbm)
            self.assertEqual(
                TxPowerEvidence.USER_ACCEPTED_UNCALIBRATED,
                direction.tx_power_evidence,
            )
            self.assertEqual(0, direction.path_min_loss_millidb)
            self.assertEqual(20_000, direction.p_rx_worst_millidbm)
            self.assertEqual(
                LossEvidence.USER_ACCEPTED_UNVERIFIED_LABEL,
                direction.components[0].loss_evidence,
            )

        with self.assertRaises(InvalidValueError):
            replace(manifest, risk_acceptance_id=None)
        with self.assertRaises(InvalidValueError):
            replace(manifest.directions[0], approved_tx_gain_db=1)
        with self.assertRaises(InvalidValueError):
            replace(
                manifest,
                directions=(
                    replace(
                        manifest.directions[0],
                        components=(
                            replace(
                                manifest.directions[0].components[0],
                                component_id="ATT-B-30DB-2W-6GHZ-UNSERIALIZED",
                            ),
                        ),
                    ),
                    manifest.directions[1],
                ),
            )
        with self.assertRaises(InvalidValueError):
            replace(
                manifest,
                directions=(
                    replace(
                        manifest.directions[0],
                        components=(
                            replace(
                                manifest.directions[0].components[0],
                                maximum_input_millidbm=33_011,
                            ),
                        ),
                    ),
                    manifest.directions[1],
                ),
            )
        with self.assertRaises(InvalidValueError):
            replace(
                manifest,
                validation_profile=FixtureValidationProfile.CALIBRATED_PROTECTION,
            )

    def test_TC_RADIO_012_calibration_artifact_is_hash_verified_before_tx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            calibration = root / "calibration.json"
            calibration_payload = {
                "schema_version": 1,
                "outcome": "PASS",
                "calibration_id": "calibration-a",
                "device_serial": "A",
                "tx_gain_db": 0,
                "sample_format": "cf32le",
                "waveform_id": "VDETER-STAGE2-LINK11-FROZEN-CF32LE",
                "measurement_plane": "B210_TX_RX_CONNECTOR_EQUIVALENT",
                "measurement_device_id": "meter-1",
                "measured_at_utc": "2026-08-21T00:00:00Z",
                "measurements": [
                    {
                        "frequency_hz": 157237500,
                        "measured_output_millidbm": 8500,
                        "measurement_uncertainty_millidb": 500,
                        "conservative_bound_millidbm": 9000,
                    },
                    {
                        "frequency_hz": 161837500,
                        "measured_output_millidbm": 9000,
                        "measurement_uncertainty_millidb": 500,
                        "conservative_bound_millidbm": 9500,
                    },
                ],
            }
            calibration.write_text(json.dumps(calibration_payload), encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(calibration.read_bytes()).hexdigest()
            component = {
                "component_id": "attenuator-30db",
                "kind": "ATTENUATOR",
                "nominal_loss_millidb": 30000,
                "tolerance_millidb": 0,
                "guaranteed_min_loss_millidb": 30000,
                "loss_evidence": "MANUFACTURER_GUARANTEE",
                "minimum_frequency_hz": 100000000,
                "maximum_frequency_hz": 200000000,
                "maximum_input_millidbm": 30000,
            }
            def direction(left, right):
                return {
                    "tx_serial": left,
                    "tx_connector": "TX/RX",
                    "rx_serial": right,
                    "rx_connector": "RX2",
                    "p_tx_bound_millidbm": 10000,
                    "approved_tx_gain_db": 0,
                    "tx_power_evidence": "CALIBRATED_MEASUREMENT",
                    "tx_power_calibration_path": "calibration.json",
                    "tx_power_calibration_sha256": digest,
                    "components": [component],
                }
            manifest = {
                "fixture_id": "fixture-30db",
                "source_radio_fingerprint": "sha256:source",
                "receiver_radio_fingerprint": "sha256:receiver",
                "center_frequencies_hz": [157237500, 161837500],
                "directions": [direction("A", "B"), direction("B", "A")],
                "created_at_utc": "2026-08-21T00:00:00Z",
                "preflight_version": "0.2.3",
                "outcome": "PASS",
            }
            manifest["directions"][1]["tx_power_calibration_path"] = "calibration-b.json"
            calibration_b = root / "calibration-b.json"
            calibration_payload["calibration_id"] = "calibration-b"
            calibration_payload["device_serial"] = "B"
            calibration_b.write_text(json.dumps(calibration_payload), encoding="utf-8")
            manifest["directions"][1]["tx_power_calibration_sha256"] = (
                "sha256:" + hashlib.sha256(calibration_b.read_bytes()).hexdigest()
            )
            manifest_path = root / "fixture.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual("fixture-30db", load_fixture_manifest(manifest_path).fixture_id)
            calibration_payload["device_serial"] = "ALTERED"
            calibration.write_text(json.dumps(calibration_payload), encoding="utf-8")
            with self.assertRaises(InvalidValueError):
                load_fixture_manifest(manifest_path)

    def test_TC_RADIO_002_injected_backend_applies_full_readback(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, DeterministicIdSource(12), events,
            backend_factory=_Backend)
        configured = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertTrue(configured.is_ok, configured)
        self.assertEqual("TESTSERIAL", configured.value.applied_settings.device_id)
        self.assertEqual(8, _Backend.instances[0].hardware_reads)
        self.assertEqual(["configure", "connect"], _Backend.instances[0].call_order)
        self.assertEqual(8, _Backend.instances[0].hardware_reads_at_connect)
        for channel in configured.value.applied_settings.channels:
            if channel.binding.tx_enabled:
                self.assertEqual(76_800, channel.applied_tx_sample_rate_hz)
                self.assertGreaterEqual(channel.applied_tx_analog_bandwidth_hz, 200_000)
            if channel.binding.rx_enabled:
                self.assertEqual(76_800, channel.applied_rx_sample_rate_hz)
                self.assertGreaterEqual(channel.applied_rx_analog_bandwidth_hz, 200_000)

    def test_TC_RADIO_002_direct_adapter_configuration_enforces_exact_profile(self):
        configuration = b210_configuration()
        wrong_frequency = replace(
            configuration,
            channels=(
                replace(configuration.channels[0], center_frequency_hz=157_237_501),
                configuration.channels[1],
            ),
        )
        adapter = B210RadioAdapter(backend_factory=_Backend)
        rejected = adapter.configure(RadioConfigureRequest(wrong_frequency))
        self.assertEqual(ErrorCode.RADIO_UNSUPPORTED_SETTING, rejected.error.code)
        self.assertEqual([], _Backend.instances)

        wrong_timing = replace(
            configuration,
            timing=replace(configuration.timing, mapping_attempt_count=7),
        )
        rejected = adapter.configure(RadioConfigureRequest(wrong_timing))
        self.assertEqual(ErrorCode.RADIO_UNSUPPORTED_SETTING, rejected.error.code)
        self.assertEqual([], _Backend.instances)

    def test_TC_RADIO_005_failed_readback_rolls_back_without_consuming_generation(self):
        _Backend.mutate_readback = lambda value: value.update(master_clock_rate_hz=1)
        adapter = B210RadioAdapter(backend_factory=_Backend)
        failed = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertEqual(ErrorCode.RADIO_CONFIGURATION_FAILED, failed.error.code)
        self.assertTrue(_Backend.instances[0].closed)
        _Backend.mutate_readback = None
        configured = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertTrue(configured.is_ok, configured)
        self.assertEqual(1, configured.value.applied_settings.configure_generation)

    def test_TC_RADIO_005_failed_late_config_cleanup_is_retained_for_stop_retry(self):
        class _CleanupFailBackend(_Backend):
            cleanup_allowed = False

            def close(self, timeout_ns):
                self.closed = self.cleanup_allowed
                return self.cleanup_allowed

        def invalid_inactive_readback(value):
            row = value["channels"][LOWER.name]
            row["rx_sample_rate_hz"] = 76_800
            row["rx_gain_db"] = 1
            row["rx_bandwidth_hz"] = 200_000

        _Backend.mutate_readback = invalid_inactive_readback
        adapter = B210RadioAdapter(backend_factory=_CleanupFailBackend)
        failed = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertEqual(ErrorCode.RADIO_CONFIGURATION_FAILED, failed.error.code)
        self.assertEqual("FAILED", adapter.status(RadioStatusRequest()).value.device_state.value)
        _CleanupFailBackend.cleanup_allowed = True
        stopped = adapter.stop(RadioStopRequest("retry partial configure cleanup"))
        self.assertTrue(stopped.is_ok, stopped)

    def test_TC_RADIO_007_burst_ack_is_the_only_success_terminal(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, DeterministicIdSource(12), events,
            backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        at = utc(1_300_000_000)
        accepted = adapter.schedule_tx(RadioTxRequest(
            burst_id(1), UPPER, at, cf32_block(at)))
        self.assertTrue(accepted.is_ok, accepted)
        self.assertEqual(0, len(events.events_of(InternalEventType.BURST_COMPLETED)))
        _Backend.instances[0].async_events.append("BURST_ACK")
        self._wait_until(lambda: len(events.events_of(
            InternalEventType.BURST_COMPLETED
        )) == 1)
        terminals = events.events_of(InternalEventType.BURST_COMPLETED)
        self.assertEqual(1, len(terminals))
        self.assertEqual(burst_id(1), terminals[0].correlation.burst_id)

    def test_TC_RADIO_006_mapping_uses_eight_brackets_and_generation_clock_id(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        adapter = B210RadioAdapter(clock, backend_factory=_Backend)
        configured = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertTrue(configured.is_ok, configured)
        timing = adapter.status(RadioStatusRequest()).value.timing_status
        self.assertEqual(8, _Backend.instances[0].hardware_reads)
        self.assertEqual(0, timing.mapping.uncertainty_nanoseconds)
        self.assertEqual("b210:TESTSERIAL:cfg:1", timing.mapping.target_clock_id)

    def test_TC_RADIO_006_utc_jump_preserves_active_b210_rx_prefix(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(6_600), events,
            backend_factory=_Backend,
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000_000, 76_800,
        )).is_ok)
        entry = adapter._rx_ledger[0]
        partial_count = 24
        partial = b"".join(
            struct.pack("<ff", 0.125, 0.0) for _ in range(partial_count)
        )
        _Backend.instances[0].read_results_by_window[entry.hardware_start_ns] = (
            partial, entry.hardware_start_ns, 0,
        )
        clock.advance_utc(10_000_001)
        self._wait_until(
            lambda: adapter._timing_status.mapping_state.value == "UNAVAILABLE"
        )
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual("UNAVAILABLE", status.timing_status.mapping_state.value)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual([partial_count], [
            event.payload.samples.sample_count for event in blocks
        ])
        incomplete = [
            event for event in events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
        ]
        self.assertEqual(1, len(incomplete))
        self.assertEqual(
            partial_count,
            dict(incomplete[0].details.entries)["received_sample_count"],
        )

    def test_TC_RADIO_007_frequency_change_uses_200ms_lead_and_100ms_retune_gap(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        adapter = B210RadioAdapter(clock, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        at = utc(1_200_000_000)
        accepted = adapter.schedule_tx(RadioTxRequest(
            burst_id(50), LOWER, at, cf32_block(at),
        ))
        self.assertTrue(accepted.is_ok, accepted)
        submitted = _Backend.instances[0].tx[0]
        self.assertEqual(100_000_000, submitted[3])

        earlier = utc(1_150_000_000)
        rejected = adapter.schedule_tx(RadioTxRequest(
            burst_id(51), UPPER, earlier, cf32_block(earlier),
        ))
        self.assertEqual(ErrorCode.RADIO_LATE_REQUEST, rejected.error.code)

    def test_TC_RADIO_008_finite_rx_uses_exact_count_and_rx_time(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, DeterministicIdSource(80), events,
            backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        duration = 1_000_000
        expected = (duration * 76_800 + 500_000_000) // 1_000_000_000
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, duration, 76_800,
        )).is_ok)
        self.assertEqual(expected, _Backend.instances[0].rx[0][1])
        data = b"".join(struct.pack("<ff", 0.0, 0.0) for _ in range(expected))
        _Backend.instances[0].read_results.append((data, 100_000_000, 0))
        self._wait_until(lambda: len(events.events_of(
            InternalEventType.RADIO_RX_WINDOW_COMPLETED
        )) == 1)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual(expected, blocks[0].payload.samples.sample_count)
        self.assertEqual("RADIO_HARDWARE", blocks[0].payload.hardware_received_at.clock_domain.value)
        self.assertEqual(1, len(events.events_of(InternalEventType.RADIO_RX_WINDOW_COMPLETED)))

    def test_TC_RADIO_008_rx_terminal_arrives_without_status_polling(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        try:
            start = utc(1_100_000_000)
            self.assertTrue(adapter.request_rx(RadioRxRequest(
                UPPER, start, 1_000_000, 76_800,
            )).is_ok)
            entry = adapter._rx_ledger[0]
            data = b"".join(
                struct.pack("<ff", 0.0, 0.0)
                for _ in range(entry.expected_sample_count)
            )
            backend = _Backend.instances[-1]
            backend.read_results_by_window[entry.hardware_start_ns] = (
                data, entry.hardware_start_ns, 0,
            )
            backend.hardware_ns = entry.hardware_end_ns
            self._wait_until(lambda: len(events.events_of(
                InternalEventType.RADIO_RX_WINDOW_COMPLETED
            )) == 1)
        finally:
            adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_008_rx_deadline_distinguishes_timeout_and_short_read(self):
        def run(result, expected_code):
            clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
                monotonic_clock_id="radio-test-monotonic")
            events = RecordingInternalEventSink()
            adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
            self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
            self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
            start = utc(1_100_000_000)
            self.assertTrue(adapter.request_rx(RadioRxRequest(
                UPPER, start, 1_000_000, 76_800,
            )).is_ok)
            backend = _Backend.instances[-1]
            backend.hardware_ns = 101_000_000
            if result is not None:
                backend.read_results.append(result)
            self._wait_until(lambda: any(
                event.error is not None and event.error.code == expected_code
                for event in events.events_of(InternalEventType.LAYER_ERROR)
            ))
            terminal = [event for event in events.events_of(InternalEventType.LAYER_ERROR)
                if event.error.code == expected_code]
            self.assertEqual(1, len(terminal))

        run(None, ErrorCode.RADIO_RX_TIMEOUT)
        short = b"".join(struct.pack("<ff", 0.0, 0.0) for _ in range(10))
        run((short, 100_000_000, 0), ErrorCode.RADIO_RX_WINDOW_INCOMPLETE)

    def test_TC_RADIO_008_multiple_finite_rx_windows_keep_private_data_boundaries(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(8_800), events, backend_factory=_Backend
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        first_start = utc(1_100_000_000)
        second_start = utc(1_102_000_000)
        for start in (first_start, second_start):
            self.assertTrue(adapter.request_rx(RadioRxRequest(
                UPPER, start, 1_000_000, 76_800,
            )).is_ok)
        entries = tuple(adapter._rx_ledger)
        first_data = b"".join(struct.pack("<ff", 0.1, 0.0) for _ in range(10))
        second_data = b"".join(
            struct.pack("<ff", 0.2, 0.0)
            for _ in range(entries[1].expected_sample_count)
        )
        backend = _Backend.instances[0]
        backend.read_results_by_window[entries[0].hardware_start_ns] = (
            first_data, entries[0].hardware_start_ns, 0,
        )
        backend.read_results_by_window[entries[1].hardware_start_ns] = (
            second_data, entries[1].hardware_start_ns, 0,
        )
        backend.hardware_ns = entries[1].hardware_end_ns
        self._wait_until(lambda: not adapter._rx_ledger)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual(
            [10, entries[1].expected_sample_count],
            [event.payload.samples.sample_count for event in blocks],
        )
        errors = events.events_of(InternalEventType.LAYER_ERROR)
        self.assertEqual(1, sum(
            event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
            for event in errors
        ))
        self.assertEqual(
            1,
            len(events.events_of(InternalEventType.RADIO_RX_WINDOW_COMPLETED)),
        )

    def test_TC_RADIO_008_overflow_keeps_contiguous_segments_separate(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(8_900), events, backend_factory=_Backend
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 1_000_000, 76_800,
        )).is_ok)
        entry = adapter._rx_ledger[0]
        prefix_count = 30
        suffix_count = entry.expected_sample_count - prefix_count - 1
        prefix = b"".join(
            struct.pack("<ff", 0.1, 0.0) for _ in range(prefix_count)
        )
        suffix = b"".join(
            struct.pack("<ff", 0.2, 0.0) for _ in range(suffix_count)
        )
        suffix_hardware = entry.hardware_start_ns + (
            (prefix_count + 1) * 1_000_000_000 // 76_800
        )
        _Backend.instances[0].read_results_by_window[entry.hardware_start_ns] = {
            "segments": (
                (prefix, entry.hardware_start_ns, False),
                (suffix, suffix_hardware, True),
            ),
            "overflow_samples": 1,
            "trailing_gap": False,
        }
        _Backend.instances[0].hardware_ns = entry.hardware_end_ns
        self._wait_until(lambda: not adapter._rx_ledger)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual(
            [prefix_count, suffix_count],
            [event.payload.samples.sample_count for event in blocks],
        )
        self.assertEqual(
            ["CONTIGUOUS", "GAP_BEFORE"],
            [event.payload.samples.continuity.value for event in blocks],
        )
        errors = events.events_of(InternalEventType.LAYER_ERROR)
        self.assertEqual(1, sum(
            event.error.code == ErrorCode.RADIO_RX_OVERFLOW for event in errors
        ))
        self.assertEqual(1, sum(
            event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
            for event in errors
        ))
        self.assertEqual(1, adapter.status(RadioStatusRequest()).value.overflow_count)

    def test_TC_RADIO_009_tx_and_rx_can_overlap_but_same_direction_resources_cannot(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        adapter = B210RadioAdapter(clock, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        at = utc(1_100_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(90), UPPER, at, cf32_block(at),
        )).is_ok)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, at, 1_000_000, 76_800,
        )).is_ok)
        tx_conflict = adapter.schedule_tx(RadioTxRequest(
            burst_id(91), UPPER, at, cf32_block(at),
        ))
        rx_conflict = adapter.request_rx(RadioRxRequest(
            UPPER, at, 2_000_000, 76_800,
        ))
        self.assertEqual(ErrorCode.RADIO_RESOURCE_CONFLICT, tx_conflict.error.code)
        self.assertEqual(ErrorCode.RADIO_RESOURCE_CONFLICT, rx_conflict.error.code)

    def test_TC_RADIO_010_async_error_preserves_oldest_burst_correlation(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, DeterministicIdSource(100), events,
            backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        at = utc(1_100_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(100), UPPER, at, cf32_block(at),
        )).is_ok)
        _Backend.instances[0].async_events.append({
            "outcome": "UNDERFLOW", "hardware_time_ns": 100_000_000,
        })
        self._wait_until(lambda: any(
            event.error is not None
            and event.error.code == ErrorCode.RADIO_TX_UNDERFLOW
            for event in events.events_of(InternalEventType.LAYER_ERROR)
        ))
        terminal = [event for event in events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_TX_UNDERFLOW][0]
        self.assertEqual(burst_id(100), terminal.correlation.burst_id)

    def test_TC_RADIO_010_timestamped_async_error_matches_its_actual_burst(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(10_100), events,
            backend_factory=_Backend,
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        first_at = utc(1_100_000_000)
        second_at = utc(1_300_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(101), UPPER, first_at, cf32_block(first_at),
        )).is_ok)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(102), UPPER, second_at, cf32_block(second_at),
        )).is_ok)
        backend = _Backend.instances[0]
        backend.async_events.append({
            "outcome": "UNDERFLOW", "hardware_time_ns": 300_000_000,
        })
        self._wait_until(lambda: any(
            event.error is not None
            and event.error.code == ErrorCode.RADIO_TX_UNDERFLOW
            for event in events.events_of(InternalEventType.LAYER_ERROR)
        ))
        underflow = [
            event for event in events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_TX_UNDERFLOW
        ]
        self.assertEqual([burst_id(102)], [event.correlation.burst_id for event in underflow])
        self.assertEqual([burst_id(101)], [entry.request.burst_id for entry in adapter._tx_ledger])
        backend.async_events.append({
            "outcome": "BURST_ACK", "hardware_time_ns": 100_000_000,
        })
        self._wait_until(lambda: len(events.events_of(
            InternalEventType.BURST_COMPLETED
        )) == 1)
        completed = events.events_of(InternalEventType.BURST_COMPLETED)
        self.assertEqual([burst_id(101)], [event.correlation.burst_id for event in completed])

    def test_TC_RADIO_010_native_numeric_async_codes_and_time_spec_are_decoded(self):
        class _Pmt:
            @staticmethod
            def to_python(value):
                return value

        backend = object.__new__(GrUhdFlowgraphBackend)
        backend._pmt = _Pmt()
        expected = {
            0x01: "BURST_ACK",
            0x02: "UNDERFLOW",
            0x04: "SEQUENCE_ERROR",
            0x08: "TIME_ERROR",
            0x10: "UNDERFLOW",
            0x20: "SEQUENCE_ERROR",
        }
        for code, outcome in expected.items():
            with self.subTest(code=code):
                decoded = backend._decode_async(("async_msg", {
                    "event_code": code,
                    "time_spec": (7, 0.25),
                }))
                self.assertEqual(outcome, decoded["outcome"])
                self.assertEqual(7_250_000_000, decoded["hardware_time_ns"])

    def test_TC_RADIO_010_backend_rx_exception_is_typed_and_fails_adapter(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 1_000_000, 76_800,
        )).is_ok)
        _Backend.instances[0].read_error = RuntimeError("collector failure")
        self._wait_until(lambda: adapter._state.value == "FAILED")
        status = adapter.status(RadioStatusRequest())
        self.assertEqual("FAILED", status.value.device_state.value)
        errors = events.events_of(InternalEventType.LAYER_ERROR)
        self.assertEqual(1, len([
            event for event in errors
            if event.error.code == ErrorCode.RADIO_STREAM_FAILED
        ]))

    def test_TC_RADIO_010_monitor_failure_terminalizes_all_work_without_status(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        for value, nanoseconds in ((201, 1_300_000_000), (202, 1_500_000_000)):
            at = utc(nanoseconds)
            self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                burst_id(value), UPPER, at, cf32_block(at),
            )).is_ok)
        rx_at = utc(1_700_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, rx_at, 1_000_000, 76_800,
        )).is_ok)
        _Backend.instances[-1].poll_error = RuntimeError("async monitor lost")
        self._wait_until(lambda: adapter._state.value == "FAILED")
        self.assertEqual([], adapter._tx_ledger)
        self.assertEqual([], adapter._rx_ledger)
        codes = tuple(adapter._terminal_codes.values())
        self.assertEqual(2, codes.count(ErrorCode.RADIO_TX_OUTCOME_UNKNOWN))
        self.assertEqual(1, codes.count(ErrorCode.RADIO_STREAM_FAILED))
        adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_010_late_duplicate_ack_does_not_damage_next_tx(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        try:
            for value, nanoseconds in ((301, 1_300_000_000), (302, 1_500_000_000)):
                at = utc(nanoseconds)
                self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                    burst_id(value), UPPER, at, cf32_block(at),
                )).is_ok)
            backend = _Backend.instances[-1]
            first_hardware_time = adapter._tx_ledger[0].hardware_start_ns
            ack = {"outcome": "BURST_ACK", "hardware_time_ns": first_hardware_time}
            backend.async_events.append(ack)
            self._wait_until(lambda: len(events.events_of(
                InternalEventType.BURST_COMPLETED
            )) == 1)
            backend.async_events.append(ack)
            self._wait_until(lambda: adapter._duplicate_ack_count == 1)
            self.assertEqual("RUNNING", adapter._state.value)
            self.assertEqual(
                [burst_id(302)],
                [entry.request.burst_id for entry in adapter._tx_ledger],
            )
        finally:
            adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_007_ack_terminal_arrives_without_status_polling(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        try:
            at = utc(1_300_000_000)
            self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                burst_id(401), UPPER, at, cf32_block(at),
            )).is_ok)
            hardware_time = adapter._tx_ledger[0].hardware_start_ns
            _Backend.instances[-1].async_events.append({
                "outcome": "BURST_ACK", "hardware_time_ns": hardware_time,
            })
            self._wait_until(lambda: len(events.events_of(
                InternalEventType.BURST_COMPLETED
            )) == 1)
        finally:
            adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_010_status_is_snapshot_only_not_a_progress_trigger(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(
            b210_configuration()
        )).is_ok)
        backend = _Backend.instances[-1]
        backend.activity_wait_entered = threading.Event()
        backend.activity_wait_release = threading.Event()
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        self.assertTrue(backend.activity_wait_entered.wait(0.75))
        try:
            at = utc(1_300_000_000)
            self.assertTrue(adapter.schedule_tx(RadioTxRequest(
                burst_id(450), UPPER, at, cf32_block(at),
            )).is_ok)
            hardware_time = adapter._tx_ledger[0].hardware_start_ns
            backend.async_events.append({
                "outcome": "BURST_ACK", "hardware_time_ns": hardware_time,
            })

            snapshot = adapter.status(RadioStatusRequest())
            self.assertTrue(snapshot.is_ok, snapshot)
            self.assertEqual(0, len(events.events_of(
                InternalEventType.BURST_COMPLETED
            )))

            backend.activity_wait_release.set()
            self._wait_until(lambda: len(events.events_of(
                InternalEventType.BURST_COMPLETED
            )) == 1)
        finally:
            backend.activity_wait_release.set()
            adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_010_hardware_time_loss_terminates_all_pending_work(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(101), UPPER, start, cf32_block(start),
        )).is_ok)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 1_000_000, 76_800,
        )).is_ok)
        _Backend.instances[0].hardware_error = OSError("device disappeared")
        self._wait_until(lambda: adapter._state.value == "FAILED")
        status = adapter.status(RadioStatusRequest())
        self.assertEqual("FAILED", status.value.device_state.value)
        codes = [event.error.code for event in events.events_of(InternalEventType.LAYER_ERROR)]
        self.assertIn(ErrorCode.RADIO_TX_OUTCOME_UNKNOWN, codes)
        self.assertIn(ErrorCode.RADIO_DEVICE_DISCONNECTED, codes)
        self.assertEqual(0, sum(
            value.tx_outstanding_count for value in status.value.queue_status
        ))
        self.assertEqual(0, sum(
            value.rx_window_outstanding_count for value in status.value.queue_status
        ))

    def test_TC_RADIO_010_disconnect_is_detected_without_status_polling(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(clock, event_sink=events, backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        at = utc(1_300_000_000)
        self.assertTrue(adapter.schedule_tx(RadioTxRequest(
            burst_id(501), UPPER, at, cf32_block(at),
        )).is_ok)
        _Backend.instances[-1].hardware_error = OSError("device disappeared")
        self._wait_until(lambda: adapter._state.value == "FAILED")
        self.assertIn(
            ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
            adapter._terminal_codes.values(),
        )
        adapter.stop(RadioStopRequest("test cleanup"))

    def test_TC_RADIO_010_delivery_failure_marks_submitted_tx_unknown_and_rx_incomplete(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        sink = _FailOnceSink(InternalEventType.BURST_COMPLETED)
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(10_500), sink, backend_factory=_Backend
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        first_at = utc(1_300_000_000)
        second_at = utc(1_500_000_000)
        first = RadioTxRequest(burst_id(105), UPPER, first_at, cf32_block(first_at))
        second = RadioTxRequest(burst_id(106), UPPER, second_at, cf32_block(second_at))
        self.assertTrue(adapter.schedule_tx(first).is_ok)
        self.assertTrue(adapter.schedule_tx(second).is_ok)
        rx_start = utc(1_600_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, rx_start, 1_000_000, 76_800,
        )).is_ok)
        rx_key = adapter._rx_ledger[0].key
        partial = b"".join(
            struct.pack("<ff", 0.0, 0.0) for _ in range(10)
        )
        _Backend.instances[0].read_results.append((partial, 600_000_000, 0))
        _Backend.instances[0].async_events.append({
            "outcome": "BURST_ACK", "hardware_time_ns": 300_000_000,
        })
        self._wait_until(lambda: adapter._state.value == "FAILED")
        status = adapter.status(RadioStatusRequest()).value
        self.assertEqual("FAILED", status.device_state.value)
        self.assertEqual(3, status.event_delivery_status.undelivered_terminal_count)
        self.assertIsNone(adapter._terminal_codes[("tx", 1, first.burst_id.value)])
        self.assertEqual(
            ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
            adapter._terminal_codes[("tx", 1, second.burst_id.value)],
        )
        self.assertEqual(
            ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
            adapter._terminal_codes[("rx", rx_key)],
        )
        self.assertEqual(
            10,
            dict(adapter._terminal_details[("rx", rx_key)].entries)[
                "received_sample_count"
            ],
        )

    def test_TC_RADIO_010_nonterminal_b210_delivery_failure_keeps_terminal_count_zero(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        sink = _FailOnceSink(InternalEventType.RADIO_STATUS_CHANGED)
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(10_550), sink,
            backend_factory=_Backend,
        )
        configured = adapter.configure(RadioConfigureRequest(b210_configuration()))
        self.assertEqual(ErrorCode.RADIO_EVENT_DELIVERY_FAILED, configured.error.code)
        status = adapter.status(RadioStatusRequest()).value
        self.assertTrue(status.event_delivery_status.failed)
        self.assertEqual(0, status.event_delivery_status.undelivered_terminal_count)

    def test_TC_RADIO_010_sample_delivery_failure_preserves_actual_rx_count(self):
        clock = ControlledTimeSource(1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic")
        sink = _FailOnceSink(InternalEventType.SAMPLES_RECEIVED)
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(10_600), sink, backend_factory=_Backend
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 1_000_000, 76_800,
        )).is_ok)
        entry = adapter._rx_ledger[0]
        actual = entry.expected_sample_count
        data = b"".join(struct.pack("<ff", 0.0, 0.0) for _ in range(actual))
        _Backend.instances[0].read_results.append((data, 100_000_000, 0))
        self._wait_until(lambda: adapter._state.value == "FAILED")
        status = adapter.status(RadioStatusRequest()).value
        terminal_key = ("rx", entry.key)
        details = dict(adapter._terminal_details[terminal_key].entries)
        self.assertEqual("FAILED", status.device_state.value)
        self.assertEqual(1, status.event_delivery_status.undelivered_terminal_count)
        self.assertEqual(ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
                         adapter._terminal_codes[terminal_key])
        self.assertEqual(actual, details["received_sample_count"])

    def test_TC_RADIO_005_stop_timeout_is_retryable(self):
        adapter = B210RadioAdapter(backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        backend = _Backend.instances[0]
        backend.stop_result = False
        failed = adapter.stop(RadioStopRequest("timeout"))
        self.assertEqual(ErrorCode.RADIO_STOP_TIMEOUT, failed.error.code)
        backend.stop_result = True
        retried = adapter.stop(RadioStopRequest("retry"))
        self.assertTrue(retried.is_ok, retried)

    def test_TC_RADIO_005_stop_delivers_received_rx_prefix_before_incomplete(self):
        clock = ControlledTimeSource(
            1_000_000_000, 0, utc_clock_id="radio-test-utc",
            monotonic_clock_id="radio-test-monotonic",
        )
        events = RecordingInternalEventSink()
        adapter = B210RadioAdapter(
            clock, DeterministicIdSource(5_500), events,
            backend_factory=_Backend,
        )
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        start = utc(1_100_000_000)
        self.assertTrue(adapter.request_rx(RadioRxRequest(
            UPPER, start, 80_000_000, 76_800,
        )).is_ok)
        entry = adapter._rx_ledger[0]
        partial_count = 32
        partial = b"".join(
            struct.pack("<ff", 0.25, 0.0) for _ in range(partial_count)
        )
        _Backend.instances[0].read_results_by_window[entry.hardware_start_ns] = (
            partial, entry.hardware_start_ns, 0,
        )
        stopped = adapter.stop(RadioStopRequest("partial RX cleanup"))
        self.assertTrue(stopped.is_ok, stopped)
        blocks = events.events_of(InternalEventType.SAMPLES_RECEIVED)
        self.assertEqual([partial_count], [
            event.payload.samples.sample_count for event in blocks
        ])
        terminals = [
            event for event in events.events_of(InternalEventType.LAYER_ERROR)
            if event.error.code == ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
        ]
        self.assertEqual(1, len(terminals))
        self.assertEqual(
            partial_count,
            dict(terminals[0].details.entries)["received_sample_count"],
        )

    def test_TC_RADIO_005_blocking_backend_stop_is_bounded_and_retryable(self):
        configuration = replace(
            b210_configuration(),
            resource_limits=limits(stop_timeout_ns=20_000_000),
        )
        adapter = B210RadioAdapter(backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(configuration)).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        backend = _Backend.instances[0]
        backend.stop_entered = threading.Event()
        backend.stop_release = threading.Event()
        started = time.monotonic()
        failed = adapter.stop(RadioStopRequest("blocking stop"))
        elapsed = time.monotonic() - started
        self.assertEqual(ErrorCode.RADIO_STOP_TIMEOUT, failed.error.code)
        self.assertLess(elapsed, 0.5)
        self.assertEqual("FAILED", adapter.status(RadioStatusRequest()).value.device_state.value)
        backend.stop_release.set()
        retried = adapter.stop(RadioStopRequest("retry after release"))
        self.assertTrue(retried.is_ok, retried)

    def test_TC_RADIO_005_blocking_monitor_wait_shares_cleanup_deadline(self):
        configuration = replace(
            b210_configuration(),
            resource_limits=limits(stop_timeout_ns=20_000_000),
        )
        adapter = B210RadioAdapter(backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(configuration)).is_ok)
        backend = _Backend.instances[-1]
        backend.activity_wait_entered = threading.Event()
        backend.activity_wait_release = threading.Event()
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        self.assertTrue(backend.activity_wait_entered.wait(0.5))
        started = time.monotonic()
        failed = adapter.stop(RadioStopRequest("blocking monitor"))
        self.assertEqual(ErrorCode.RADIO_STOP_TIMEOUT, failed.error.code)
        self.assertLess(time.monotonic() - started, 0.5)
        backend.activity_wait_release.set()
        retried = adapter.stop(RadioStopRequest("retry monitor cleanup"))
        self.assertTrue(retried.is_ok, retried)
        self.assertIsNone(adapter._monitor_thread)

    def test_TC_RADIO_005_flowgraph_stop_and_wait_share_one_bounded_worker(self):
        class _Top:
            def __init__(self):
                self.release = threading.Event()
                self.stop_entered = threading.Event()
                self.wait_called = False

            def stop(self):
                self.stop_entered.set()
                self.release.wait(1.0)

            def wait(self):
                self.wait_called = True

        backend = object.__new__(GrUhdFlowgraphBackend)
        backend._top = _Top()
        backend._closed = False
        backend._started = True
        backend._cleanup_event = threading.Event()
        backend._cleanup_failure = []
        backend._cleanup_thread = None
        backend._cleanup_lock = threading.Lock()
        started = time.monotonic()
        self.assertFalse(backend.stop(20_000_000))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(backend._top.stop_entered.is_set())
        self.assertFalse(backend._top.wait_called)
        backend._top.release.set()
        self.assertTrue(backend.stop(1_000_000_000))
        self.assertTrue(backend._top.wait_called)

    def test_TC_RADIO_005_concurrent_stop_shares_one_cleanup(self):
        adapter = B210RadioAdapter(backend_factory=_Backend)
        self.assertTrue(adapter.configure(RadioConfigureRequest(b210_configuration())).is_ok)
        self.assertTrue(adapter.start(RadioStartRequest()).is_ok)
        backend = _Backend.instances[0]
        backend.stop_entered = threading.Event()
        backend.stop_release = threading.Event()
        results = []
        first = threading.Thread(target=lambda: results.append(adapter.stop(RadioStopRequest("one"))))
        second = threading.Thread(target=lambda: results.append(adapter.stop(RadioStopRequest("two"))))
        first.start()
        self.assertTrue(backend.stop_entered.wait(1.0))
        second.start()
        backend.stop_release.set()
        first.join(1.0)
        second.join(1.0)
        self.assertEqual(2, len(results))
        self.assertTrue(all(value.is_ok for value in results))
        self.assertEqual(results[0].value, results[1].value)


if __name__ == "__main__":
    unittest.main()
