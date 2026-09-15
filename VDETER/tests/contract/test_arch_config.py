"""TC-ARCH-020, 021, 022: 설정 loader, 상태 격리와 Radio adapter 선택.

요구사항: `ARCH-REQ-04`, `ARCH-REQ-05`, `ARCH-REQ-06`, `ARCH-REQ-07`
"""

from __future__ import annotations

import unittest

from tests.support.fixtures import (
    B210_CONFIG_PATH,
    FAKE_CONFIG_PATH,
    SOFTWARE_CONFIG_PATH,
    build_fake_endpoint,
    load_fake_configuration,
    read_config_text,
)

from vdeter.common.contracts.radio import (
    RadioConfigureRequest,
    RadioRxRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStopRequest,
    RadioTxRequest,
)
from vdeter.common.contracts.service import ServiceStartRequest, ServiceStopRequest
from vdeter.common.errors import ErrorCode
from vdeter.config.loader import load_configuration, load_configuration_file
from vdeter.config.models import CONFIG_SCHEMA_VERSION
from vdeter.profiles.models import RegistryScope
from vdeter.radio.b210 import B210RadioAdapter, missing_dependencies
from vdeter.radio.fake import FakeRadioAdapter
from vdeter.radio.models import RadioAdapterKind
from vdeter.radio.software import SoftwareRadioAdapter
from vdeter.runtime.endpoint import build_endpoint
from vdeter.runtime.wiring import build_dependencies


class ConfigLoaderTest(unittest.TestCase):
    """TC-ARCH-020: TOML load, schema version, unknown field와 profile override 거부."""

    def setUp(self):
        self.text = read_config_text(FAKE_CONFIG_PATH)

    def test_TC_ARCH_020_valid_configuration_loads(self):
        result = load_configuration_file(FAKE_CONFIG_PATH)
        self.assertTrue(result.is_ok, result.error if result.is_err else "")
        configuration = result.value
        self.assertEqual(CONFIG_SCHEMA_VERSION, configuration.endpoint.schema_version)
        self.assertIs(RegistryScope.TEST, configuration.endpoint.scope)
        self.assertTrue(configuration.endpoint_fingerprint.startswith("sha256:"))
        self.assertTrue(configuration.radio_fingerprint.startswith("sha256:"))
        self.assertNotEqual(
            configuration.endpoint_fingerprint, configuration.radio_fingerprint
        )

    def test_TC_ARCH_020_missing_schema_version_is_rejected(self):
        text = self.text.replace("schema_version = 1", "", 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SCHEMA_INVALID, result.error.code)

    def test_TC_ARCH_020_missing_required_section_fields_are_structured(self):
        removals = (
            ("service", "max_payload_bytes = 65536"),
            ("link", "supported_link_ids = [63]   # 시험 전용 Link ID. 규범 profile이 아니다."),
            ("mac", "link_id = 63"),
            ("phy", "[phy]\nsample_rate_hz = 4000"),
            ("radio", 'sample_format = "test-iq-int16-le"'),
        )
        for section, line in removals:
            with self.subTest(section=section):
                text = self.text.replace(line, "", 1)
                result = load_configuration(text)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.CONFIG_SCHEMA_INVALID, result.error.code)

    def test_TC_ARCH_020_zero_local_station_id_is_rejected(self):
        text = self.text.replace("station_id = 111111111", "station_id = 0", 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, result.error.code)

    def test_TC_ARCH_020_zero_test_peer_station_id_is_rejected(self):
        text = self.text.replace("peer_station_id = 222222222", "peer_station_id = 0", 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, result.error.code)

    def test_TC_ARCH_020_wrong_schema_version_is_rejected(self):
        text = self.text.replace("schema_version = 1", "schema_version = 2", 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SCHEMA_INVALID, result.error.code)

    def test_TC_ARCH_020_unknown_field_is_rejected(self):
        text = self.text.replace(
            '[service]\nmax_payload_bytes', '[service]\nunexpected_field = 1\nmax_payload_bytes', 1
        )
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_UNKNOWN_FIELD, result.error.code)

    def test_TC_ARCH_020_unknown_section_is_rejected(self):
        result = load_configuration(self.text + "\n[unexpected]\nvalue = 1\n")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_UNKNOWN_FIELD, result.error.code)

    def test_TC_ARCH_020_profile_override_is_rejected(self):
        text = self.text.replace(
            "[link]\nsupported_link_ids", "[link]\npacket_capacity_bytes = 100\nsupported_link_ids", 1
        )
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PROFILE_OVERRIDE_REJECTED, result.error.code)

    def test_TC_ARCH_020_inline_secret_is_rejected(self):
        text = self.text.replace(
            '[security]\nprivate_key_ref = ""', '[security]\nprivate_key = "MIIB"', 1
        )
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertIn(
            result.error.code,
            (ErrorCode.CONFIG_SCHEMA_INVALID, ErrorCode.CONFIG_UNKNOWN_FIELD),
        )

    def test_TC_ARCH_020_parse_error_is_structured(self):
        result = load_configuration("schema_version = = 1")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_PARSE_FAILED, result.error.code)

    def test_TC_ARCH_020_semantic_error_is_structured(self):
        text = self.text.replace('tx_channel = "stage0-test-tx"', 'tx_channel = "missing"', 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, result.error.code)

    def test_TC_ARCH_020_operational_scope_rejects_test_scope_choices(self):
        text = self.text.replace('scope = "TEST"', 'scope = "OPERATIONAL"', 1)
        result = load_configuration(text)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, result.error.code)

    def test_TC_ARCH_020_missing_file_is_structured(self):
        result = load_configuration_file("configs/does_not_exist.toml")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CONFIG_PARSE_FAILED, result.error.code)


class StateIsolationTest(unittest.TestCase):
    """TC-ARCH-021: profile/config/runtime 분리와 재시작 초기화."""

    def test_TC_ARCH_021_configuration_snapshot_is_immutable(self):
        import dataclasses

        configuration = load_fake_configuration()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            configuration.endpoint.endpoint_id = "other"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            configuration.radio.device_selector = "other"

    def test_TC_ARCH_021_profile_registry_is_not_configuration(self):
        endpoint, dependencies = build_fake_endpoint()
        registries = dependencies.registries
        self.assertTrue(registries.link_profile.is_frozen)
        blocked = registries.link_profile.register(registries.link_profile.entries()[0])
        self.assertTrue(blocked.is_err)
        self.assertEqual(ErrorCode.REGISTRY_FROZEN, blocked.error.code)

    def test_TC_ARCH_021_runtime_state_is_not_restored_after_restart(self):
        from vdeter.common.addressing import Addressed
        from vdeter.common.contracts.service import SubmitTransferRequest, TransferStatusRequest
        from vdeter.common.identifiers import PayloadFormat, StationId

        endpoint, dependencies = build_fake_endpoint()
        configuration = dependencies.configuration.endpoint
        endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )
        accepted = endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"payload",
            )
        )
        self.assertTrue(accepted.is_ok)
        endpoint.stop(ServiceStopRequest(reason="restart test"))

        restarted, restarted_dependencies = build_fake_endpoint()
        restarted.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )
        result = restarted.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertTrue(result.is_err)
        self.assertEqual("UNKNOWN_TRANSFER", result.error.code)
        self.assertEqual((), restarted.radio.scheduled_bursts())

    def test_TC_ARCH_021_registry_content_is_identical_across_restarts(self):
        _first, first_dependencies = build_fake_endpoint()
        _second, second_dependencies = build_fake_endpoint()
        self.assertEqual(
            first_dependencies.registries.fingerprints(),
            second_dependencies.registries.fingerprints(),
        )


class RadioAdapterSelectionTest(unittest.TestCase):
    """TC-ARCH-022: Fake lifecycle과 Software/B210 골격의 구조화된 오류."""

    def test_TC_ARCH_022_fake_adapter_is_selected_for_fake_config(self):
        _endpoint, dependencies = build_fake_endpoint()
        self.assertIsInstance(dependencies.radio, FakeRadioAdapter)

    def test_TC_ARCH_022_fake_adapter_lifecycle(self):
        endpoint, dependencies = build_fake_endpoint()
        configuration = dependencies.configuration
        radio = dependencies.radio

        self.assertTrue(
            radio.configure(RadioConfigureRequest(configuration=configuration.radio)).is_ok
        )
        self.assertTrue(radio.start(RadioStartRequest()).is_ok)
        status = radio.status(RadioStatusRequest())
        self.assertTrue(status.is_ok)
        self.assertTrue(status.value.clock_locked)
        rx = radio.request_rx(
            RadioRxRequest(
                channel=configuration.endpoint.mac.rx_channel,
                start_at=dependencies.time_source.now_utc(),
                duration_nanoseconds=1000,
                sample_rate_hz=configuration.endpoint.phy.sample_rate_hz,
            )
        )
        self.assertTrue(rx.is_ok)
        first_stop = radio.stop(RadioStopRequest())
        second_stop = radio.stop(RadioStopRequest())
        self.assertTrue(first_stop.is_ok)
        self.assertEqual(first_stop.value, second_stop.value)

    def test_TC_ARCH_022_fake_adapter_rejects_before_start(self):
        from tests.support import dto_catalog

        endpoint, dependencies = build_fake_endpoint()
        radio = dependencies.radio
        result = radio.schedule_tx(
            RadioTxRequest(
                burst_id=dto_catalog.BURST_ID,
                channel=dependencies.configuration.endpoint.mac.tx_channel,
                transmit_at=dependencies.time_source.now_utc(),
                samples=dto_catalog.SAMPLES,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.RADIO_NOT_CONFIGURED, result.error.code)

    def test_TC_ARCH_022_fake_adapter_rejects_unknown_channel(self):
        from tests.support import dto_catalog
        from vdeter.common.identifiers import PhysicalChannel

        endpoint, dependencies = build_fake_endpoint()
        radio = dependencies.radio
        radio.configure(RadioConfigureRequest(configuration=dependencies.configuration.radio))
        radio.start(RadioStartRequest())
        result = radio.schedule_tx(
            RadioTxRequest(
                burst_id=dto_catalog.BURST_ID,
                channel=PhysicalChannel("unknown-channel"),
                transmit_at=dependencies.time_source.now_utc(),
                samples=dto_catalog.SAMPLES,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.RADIO_UNKNOWN_CHANNEL, result.error.code)

    def test_TC_ARCH_022_software_adapter_validates_request_before_state(self):
        adapter = SoftwareRadioAdapter()
        for name in ("configure", "start", "stop", "schedule_tx", "request_rx", "status"):
            with self.subTest(operation=name):
                result = getattr(adapter, name)(None)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)

    def test_TC_ARCH_022_b210_adapter_validates_request_before_dependency(self):
        adapter = B210RadioAdapter()
        for name in ("configure", "start", "stop", "schedule_tx", "request_rx", "status"):
            with self.subTest(operation=name):
                result = getattr(adapter, name)(None)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)

    def test_TC_ARCH_022_skeleton_adapters_are_selectable_from_config(self):
        for path, expected in (
            (SOFTWARE_CONFIG_PATH, SoftwareRadioAdapter),
            (B210_CONFIG_PATH, B210RadioAdapter),
        ):
            with self.subTest(config=path):
                configuration = load_configuration_file(path)
                self.assertTrue(configuration.is_ok)
                dependencies = build_dependencies(configuration.value)
                self.assertTrue(dependencies.is_ok)
                self.assertIsInstance(dependencies.value.radio, expected)

    def test_TC_ARCH_022_v1_software_config_fails_without_implicit_v2_migration(self):
        configuration = load_configuration_file(SOFTWARE_CONFIG_PATH).value
        dependencies = build_dependencies(configuration).value
        endpoint = build_endpoint(dependencies).value
        result = endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.endpoint.station_id,
                role=configuration.endpoint.role,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual("DEVICE_ERROR", result.error.code)
        self.assertEqual("UNHEALTHY", endpoint.health.value)

    def test_TC_ARCH_022_adapter_kind_scope(self):
        from vdeter.radio.models import TEST_SCOPE_ADAPTER_KINDS

        self.assertIn(RadioAdapterKind.FAKE, TEST_SCOPE_ADAPTER_KINDS)
        self.assertNotIn(RadioAdapterKind.SOFTWARE, TEST_SCOPE_ADAPTER_KINDS)
        self.assertNotIn(RadioAdapterKind.B210, TEST_SCOPE_ADAPTER_KINDS)


if __name__ == "__main__":
    unittest.main()
