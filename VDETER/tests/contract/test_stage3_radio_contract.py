"""TC-RADIO-001, 002 and the non-hardware portion of TC-RADIO-014."""

from __future__ import annotations

import dataclasses
import ast
import pathlib
import unittest

from tests.support.fixtures import FAKE_CONFIG_PATH
from tests.support.radio_stage3 import software_v2_toml
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_BYTES_PER_SAMPLE,
    SAMPLE_FORMAT_CF32LE,
    RadioRxWindowCompleted,
)
from vdeter.common.events import INTERNAL_EVENT_PAYLOAD_TYPES, InternalEventType
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.serialization import from_reference_json, to_reference_json
from vdeter.config.loader import load_configuration, load_configuration_file
from vdeter.config.models import (
    CONFIG_SCHEMA_CURRENT_VERSION,
    CONFIG_SCHEMA_SUPPORTED_VERSIONS,
)
from vdeter.radio.b210 import B210RadioAdapter
from vdeter.radio.interface import RadioAdapter
from vdeter.radio.software import SoftwareRadioAdapter


class RadioContractTest(unittest.TestCase):
    def test_TC_RADIO_001_adapters_implement_the_same_public_port(self):
        self.assertIsInstance(SoftwareRadioAdapter(), RadioAdapter)
        self.assertIsInstance(B210RadioAdapter(), RadioAdapter)
        methods = {"configure", "start", "stop", "schedule_tx", "request_rx", "status"}
        self.assertEqual(methods, {name for name in methods if hasattr(SoftwareRadioAdapter, name)})
        self.assertEqual(methods, {name for name in methods if hasattr(B210RadioAdapter, name)})

    def test_TC_RADIO_002_schema_v2_is_explicit_and_canonical(self):
        self.assertEqual(2, CONFIG_SCHEMA_CURRENT_VERSION)
        # `DR-STAGE6-MR-001` (stage6_phy_common_baseline.md §13.2.2): wideband 2-PC
        # profile 전용 schema 3을 additive로 허용한다. v1/v2 의미는 아래 단언들이
        # 그대로 고정한다.
        self.assertEqual((1, 2, 3), CONFIG_SCHEMA_SUPPORTED_VERSIONS)
        left = load_configuration(software_v2_toml(reverse_channels=False))
        right = load_configuration(software_v2_toml(reverse_channels=True))
        self.assertTrue(left.is_ok, left)
        self.assertTrue(right.is_ok, right)
        self.assertEqual(left.value.radio_fingerprint, right.value.radio_fingerprint)
        self.assertEqual(
            ("VDETER_LOWER_25_TEST", "VDETER_UPPER_25_TEST"),
            left.value.radio.channel_names(),
        )
        self.assertEqual(8, SAMPLE_FORMAT_BYTES_PER_SAMPLE[SAMPLE_FORMAT_CF32LE])

    def test_TC_RADIO_002_role_direction_and_hardware_keys_are_closed(self):
        invalid = software_v2_toml().replace(
            "analog_bandwidth_hz = 25000", "analog_bandwidth_hz = 200000", 1
        )
        self.assertTrue(load_configuration(invalid).is_err)
        invalid_role = software_v2_toml().replace(
            "tx_enabled = true\nrx_enabled = false",
            "tx_enabled = false\nrx_enabled = true",
            1,
        )
        result = load_configuration(invalid_role)
        self.assertTrue(result.is_err)
        self.assertEqual("RADIO_UNSUPPORTED_SETTING", result.error.code)

    def test_TC_RADIO_002_operational_b210_ship_and_control_configs_are_loadable(self):
        root = pathlib.Path(__file__).parents[2]
        expected = {
            "stage3_b210_ship.toml": ((True, False), (True, True)),
            "stage3_b210_control.toml": ((False, True), (True, False)),
        }
        fingerprints = set()
        for name, directions in expected.items():
            with self.subTest(name=name):
                result = load_configuration_file(root / "configs" / name)
                self.assertTrue(result.is_ok, result)
                radio = result.value.radio
                self.assertEqual(directions, tuple(
                    (value.tx_enabled, value.rx_enabled)
                    for value in radio.channel_bindings
                ))
                fingerprints.add(result.value.radio_fingerprint)
        self.assertEqual(2, len(fingerprints))

    def test_TC_RADIO_014_rx_terminal_is_frozen_serializable_and_registered(self):
        sample = next(
            value for name, value in __import__("tests.support.dto_catalog", fromlist=["public_dto_samples"])
            .public_dto_samples() if name == "RadioRxWindowCompleted"
        )
        self.assertTrue(dataclasses.is_dataclass(sample))
        self.assertTrue(sample.__dataclass_params__.frozen)
        encoded = to_reference_json(sample)
        self.assertEqual(sample, from_reference_json(RadioRxWindowCompleted, encoded))
        self.assertIn(RadioRxWindowCompleted, INTERNAL_EVENT_PAYLOAD_TYPES)
        self.assertEqual("RADIO_RX_WINDOW_COMPLETED", InternalEventType.RADIO_RX_WINDOW_COMPLETED.value)
        with self.assertRaises(InvalidValueError):
            RadioRxWindowCompleted(
                sample.configure_generation, sample.channel, sample.requested_start_at,
                sample.requested_duration_nanoseconds, 2, 1, sample.completed_at,
            )

    def test_TC_RADIO_002_inactive_applied_direction_requires_all_none(self):
        sample = next(
            value for name, value in __import__(
                "tests.support.dto_catalog", fromlist=["public_dto_samples"]
            ).public_dto_samples()
            if name == "RadioAppliedChannelSetting"
        )
        inactive_rx = dataclasses.replace(sample.binding, rx_enabled=False)
        with self.assertRaises(InvalidValueError):
            dataclasses.replace(
                sample,
                binding=inactive_rx,
                applied_rx_sample_rate_hz=None,
                applied_rx_gain_db=0,
                applied_rx_analog_bandwidth_hz=None,
            )

    def test_TC_RADIO_014_v1_legacy_hash_is_preserved_and_v2_is_additive(self):
        legacy = load_configuration_file(FAKE_CONFIG_PATH)
        self.assertTrue(legacy.is_ok, legacy)
        self.assertEqual(
            "sha256:2e2ff53ac2d96eebe55879169536eab12cfa5249efa7192788ee8f568597c989",
            legacy.value.radio_fingerprint,
        )

        original = load_configuration(software_v2_toml())
        changed = load_configuration(software_v2_toml().replace(
            "stop_timeout_ns = 5000000000",
            "stop_timeout_ns = 4000000000",
            1,
        ))
        self.assertTrue(original.is_ok, original)
        self.assertTrue(changed.is_ok, changed)
        self.assertNotEqual(
            original.value.radio_fingerprint,
            changed.value.radio_fingerprint,
        )

    def test_TC_RADIO_014_radio_has_no_concrete_upper_layer_or_packet_logic_dependency(self):
        radio_root = pathlib.Path(__file__).parents[2] / "src" / "vdeter" / "radio"
        forbidden_modules = {"mac", "link", "phy", "service"}
        forbidden_symbols = {"Packet", "Crc", "CRC", "Fec", "FEC", "Tbb", "TBB"}
        violations = []
        for path in sorted(radio_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    segments = set(node.module.split("."))
                    if segments & forbidden_modules:
                        violations.append(f"{path.name}: import {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if set(alias.name.split(".")) & forbidden_modules:
                            violations.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.Name) and node.id in forbidden_symbols:
                    violations.append(f"{path.name}: symbol {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in forbidden_symbols:
                    violations.append(f"{path.name}: attribute {node.attr}")
        self.assertEqual([], violations)

    def test_TC_RADIO_014_evidence_scope_declares_upper_only_tbb_nonconformance(self):
        from tools.run_stage3_radio_tests import CONFORMANCE_SCOPE

        self.assertEqual("PROJECT_UPPER_ONLY", CONFORMANCE_SCOPE["roc_tbb_profile"])
        self.assertEqual(
            "VDETER_UPPER_25_TEST",
            CONFORMANCE_SCOPE["roc_tbb_transmit_channel"],
        )
        self.assertEqual("NOT_SUPPORTED", CONFORMANCE_SCOPE["lower_leg_tbb_receive"])
        self.assertEqual(
            "NOT_VERIFIED",
            CONFORMANCE_SCOPE["dual_leg_tbb_interoperability"],
        )


if __name__ == "__main__":
    unittest.main()
