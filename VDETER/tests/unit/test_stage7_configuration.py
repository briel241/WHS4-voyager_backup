"""Stage 7 schema v4 additive loader 시험 (`stage7_b210_system_baseline.md` §15).

frozen `config/loader.py`·`config/models.py`는 호출 결과 비교(공통 table
projection equivalence)에만 사용하고 절대 수정하지 않는다.
"""

import hashlib
import unittest

from vdeter.common.errors import ErrorCode
from vdeter.config.loader import load_configuration
from vdeter.b210_system.configuration import (
    ConfigurationLoader,
    OperationalGainInput,
    load_data_profile,
)

TEST_MANIFEST_SHA = hashlib.sha256(b"stage7-test-deployment-manifest").hexdigest()
TEST_ENV_SHA = hashlib.sha256(b"stage7-test-environment-manifest").hexdigest()

DATA_PROFILE_17 = 'data_link_id = 17\n'
DATA_PROFILE_19 = 'data_link_id = 19\n'


def build_source(
    *,
    endpoint_id: str = "MASS-A",
    role: str = "SHIP",
    station_id: int = 111111111,
    serial: str = "34D03DD",
    run_mode: str = "RF_OFF_QUALIFICATION",
    usb_transport: str = "DIRECT_USB3_5000M",
    usb_path: str = "3-1",
    external_power: str = "PI_BUS_POWER",
    lower_direction: str = "tx_enabled = true\nrx_enabled = false",
    upper_direction: str = "tx_enabled = true\nrx_enabled = true",
    lower_rx_gain: str = "0",
    upper_rx_gain: str = '"REQUIRED_UNSET"',
    mac_tx_channel: str = "VDETER_LOWER_100_TEST",
    mac_rx_channel: str = "VDETER_UPPER_100_TEST",
    mutate=None,
) -> str:
    text = f'''
schema_version = 4

[endpoint]
id = "{endpoint_id}"
role = "{role}"
station_id = {station_id}
scope = "TEST"
code_version = "stage7-b210-system"

[service]
max_payload_bytes = 65531
max_concurrent_transfers = 8

[link]
supported_link_ids = [11, 17, 19]

[mac]
link_id = 11
tx_channel = "{mac_tx_channel}"
rx_channel = "{mac_rx_channel}"
transmit_lead_nanoseconds = 250000000

[phy]
sample_rate_hz = 307200

[radio]
adapter = "b210-stage7-affine"
device_selector = "serial={serial}"
clock_reference = "INTERNAL"
sample_format = "cf32le"

[radio.timing]
profile = "INTERNAL_FREE_RUNNING"
clock_source = "INTERNAL"
time_source = "INTERNAL"
pps_source = "NONE"
mapping_attempt_count = 8
mapping_valid_for_nanoseconds = 1000000000
max_mapping_uncertainty_ns = 10000000

[radio.resource_limits]
tx_queue_max_count = 64
tx_queue_max_bytes = 33554432
rx_window_max_count = 64
rx_queue_max_count = 256
rx_queue_max_bytes = 67108864
stop_timeout_ns = 5000000000

[radio.b210_stream]
master_clock_rate_hz = 30720000
cpu_format = "fc32"
otw_format = "sc16"

[[radio.channels]]
name = "VDETER_LOWER_100_TEST"
center_frequency_hz = 157237500
sample_rate_hz = 307200
tx_gain_db = 0
rx_gain_db = {lower_rx_gain}
waveform_bandwidth_hz = 100000
analog_bandwidth_hz = 200000
{lower_direction}

[[radio.channels]]
name = "VDETER_UPPER_100_TEST"
center_frequency_hz = 161837500
sample_rate_hz = 307200
tx_gain_db = 0
rx_gain_db = {upper_rx_gain}
waveform_bandwidth_hz = 100000
analog_bandwidth_hz = 200000
{upper_direction}

[sources]
time = "SYSTEM"
random = "SYSTEM"
id = "UUID4"

[logging]
jsonl_enabled = true
console_enabled = false

[security]
private_key_ref = ""
certificate_ref = ""
trust_store_ref = "configs/b210_system/trust/tbb_trust_store.pem"

[host_execution]
profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"
provider_id = "none"
provider_version = "none"
host_os_id = "ubuntu"
host_os_version = "24.04"
host_machine_id = "a70b2048eb6743bd83ac9e0920b8d0b2"
pi_model_revision = "Raspberry Pi 5 Model B Rev 1.1 (d04171)"
pi_power_profile = "PI5_5V5A_HIGH_CURRENT"
usb_transport = "{usb_transport}"
usb_physical_path = "{usb_path}"
external_power_asset = "{external_power}"
environment_manifest_path = "results/runs/stage7-test/environment_manifest.json"
environment_manifest_sha256 = "{TEST_ENV_SHA}"

[stage7]
run_mode = "{run_mode}"
lane_map_profile = "STAGE7_C1_C6_TRIANGLE_V1"
frequency_translation_hz = 2290000000
lower_rf_tune_hz = 2447237500
upper_rf_tune_hz = 2451837500
phy_facade = "STAGE7_DIAGNOSTIC_PHY_V1"
timing_status_projection = "STAGE7_PUBLIC_UNAVAILABLE_PRIVATE_AFFINE_V1"
clock_bridge_profile = "STAGE7_PI_HOST_AFFINE_TIME_V1"
max_bridge_generations = 3
host_time_min_observations = 8
host_time_min_span_s = 2
host_time_max_acquisition_s = 600
host_time_observation_period_s = 1
host_time_max_bracket_ns = 2000000
host_time_tracking_period_s = 5
rf_time_reference_max_gap_s = 5
rf_time_loss_gap_s = 10
active_command_horizon_s = 120
cfo_profile = "STAGE7_RAW_TONE_CFO_V1"
cfo_numeric_profile = "STAGE7_NUMPY_FFT3072_BINARY64_V1"
cfo_accept_bound_hz = 1800
deployment_manifest_relpath = "results/runs/stage7-test/deployment_manifest.json"
deployment_manifest_sha256 = "{TEST_MANIFEST_SHA}"
registry_fingerprint = "0a2b8cb8a72727bd54c05c5dd6e6ac5aeeec7a0b51f6ebe41a72bfa2482ae5d2"
tbb_fixture_id = "VDETER_WIDEBAND_100_TBB_V3"
'''
    if mutate is not None:
        text = mutate(text)
    return text


def build_roc_source(**overrides) -> str:
    defaults = dict(
        endpoint_id="ROC",
        role="CONTROL",
        station_id=222222222,
        serial="34D03E8",
        usb_path="5-1",
        lower_direction="tx_enabled = false\nrx_enabled = true",
        upper_direction="tx_enabled = true\nrx_enabled = false",
        lower_rx_gain='"REQUIRED_UNSET"',
        upper_rx_gain="0",
        mac_tx_channel="VDETER_UPPER_100_TEST",
        mac_rx_channel="VDETER_LOWER_100_TEST",
    )
    defaults.update(overrides)
    return build_source(**defaults)


def build_mass_b_source(**overrides) -> str:
    defaults = dict(
        endpoint_id="MASS-B",
        station_id=333333333,
        serial="3515425",
        usb_transport="DIRECT_USB2_480M_EXTERNAL_DC",
        usb_path="4-2",
        external_power="DC-PSU-TEST-FIXTURE-001",
    )
    defaults.update(overrides)
    return build_source(**defaults)


class TC_STAGE7_DATA_PROFILE(unittest.TestCase):
    def test_17_and_19_allowed(self):
        self.assertEqual(load_data_profile(DATA_PROFILE_17).value, 17)
        self.assertEqual(load_data_profile(DATA_PROFILE_19).value, 19)

    def test_link_11_rejected(self):
        result = load_data_profile("data_link_id = 11\n")
        self.assertTrue(result.is_err)
        self.assertEqual(result.error.code, ErrorCode.CONFIG_SEMANTIC_INVALID)

    def test_unknown_key_rejected(self):
        result = load_data_profile("data_link_id = 17\nfallback = true\n")
        self.assertTrue(result.is_err)
        self.assertEqual(result.error.code, ErrorCode.CONFIG_UNKNOWN_FIELD)

    def test_removed_data_session_gate_key_rejected(self):
        result = load_data_profile(
            "data_link_id = 17\ndata_session_enabled = false\n"
        )
        self.assertTrue(result.is_err)
        self.assertEqual(result.error.code, ErrorCode.CONFIG_UNKNOWN_FIELD)

    def test_bool_rejected(self):
        result = load_data_profile("data_link_id = true\n")
        self.assertTrue(result.is_err)


class TC_STAGE7_LOADER_POSITIVE(unittest.TestCase):
    def setUp(self):
        self.loader = ConfigurationLoader()

    def _load(self, text, data_profile=DATA_PROFILE_17, **kwargs):
        return self.loader.load_endpoint_configuration(
            text, data_profile, run_id="stage7-unit-run", **kwargs
        )

    def test_mass_a_rf_off_resolves(self):
        result = self._load(build_source())
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        resolved = result.value
        self.assertEqual(resolved.source_schema_version, 4)
        self.assertEqual(resolved.endpoint.schema_version, 3)
        self.assertEqual(resolved.endpoint_id, "MASS-A")
        self.assertEqual(resolved.b210_serial, "34D03DD")
        self.assertEqual(resolved.data_link_id, 17)
        self.assertEqual(resolved.endpoint.mac.data_link_id.value, 17)
        self.assertEqual(resolved.radio.adapter_kind, "b210-stage7-affine")
        self.assertEqual(resolved.endpoint_common_rx_gain_db, 0)
        self.assertEqual(
            tuple(setting.rx_gain_db for setting in resolved.radio.channels), (0, 0)
        )
        for binding in resolved.radio.channel_bindings:
            self.assertIsNone(binding.tx_device_channel)
            self.assertIsNone(binding.rx_device_channel)
            self.assertIsNone(binding.tx_antenna)
            self.assertIsNone(binding.rx_antenna)
        self.assertEqual(len(resolved.resolved_sha256), 64)
        self.assertEqual(
            (resolved.lanes[0].tx_path, resolved.lanes[0].rx_path), ("C2", "C1")
        )
        self.assertEqual(
            (resolved.lanes[1].tx_path, resolved.lanes[1].rx_path), ("C5", "C6")
        )

    def test_roc_lane_map(self):
        result = self._load(build_roc_source())
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        resolved = result.value
        self.assertEqual(
            (resolved.lanes[0].tx_path, resolved.lanes[0].rx_path), ("C1", "C2")
        )
        self.assertEqual(
            (resolved.lanes[1].tx_path, resolved.lanes[1].rx_path), ("C3", "C4")
        )
        self.assertEqual(resolved.lanes[0].tx_channel, "VDETER_UPPER_100_TEST")
        self.assertEqual(resolved.lanes[0].rx_channel, "VDETER_LOWER_100_TEST")

    def test_mass_b_requires_external_dc_asset(self):
        result = self._load(build_mass_b_source())
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(result.value.b210_serial, "3515425")

    def test_operational_binds_gain_artifact(self):
        gain = OperationalGainInput(
            endpoint_common_rx_gain_db=9,
            source_gain_artifact_sha256=TEST_MANIFEST_SHA,
        )
        result = self._load(
            build_source(run_mode="OPERATIONAL"), operational_gain=gain
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        resolved = result.value
        self.assertEqual(resolved.endpoint_common_rx_gain_db, 9)
        self.assertEqual(resolved.source_gain_artifact_sha256, TEST_MANIFEST_SHA)
        # RX-enabled upper channel만 공통 gain, RX-disabled lower는 canonical 0.
        gains = {s.channel.name: s.rx_gain_db for s in resolved.radio.channels}
        self.assertEqual(gains["VDETER_UPPER_100_TEST"], 9)
        self.assertEqual(gains["VDETER_LOWER_100_TEST"], 0)

    def test_data_profile_19_resolves_independently(self):
        result = self._load(build_source(), data_profile=DATA_PROFILE_19)
        self.assertTrue(result.is_ok)
        self.assertEqual(result.value.data_link_id, 19)

    def test_resolved_sha_differs_by_data_link(self):
        first = self._load(build_source(), data_profile=DATA_PROFILE_17).value
        second = self._load(build_source(), data_profile=DATA_PROFILE_19).value
        self.assertNotEqual(first.resolved_sha256, second.resolved_sha256)

    def test_common_table_projection_matches_v3_loader(self):
        """공통 table을 frozen v3 loader가 읽은 결과와 field-by-field 비교한다."""

        resolved = self._load(build_source()).value
        v3_text = _v3_software_equivalent()
        v3 = load_configuration(v3_text)
        self.assertTrue(v3.is_ok, getattr(v3, "error", None))
        left = resolved.endpoint
        right = v3.value.endpoint
        self.assertEqual(left.endpoint_id, right.endpoint_id)
        self.assertEqual(left.role, right.role)
        self.assertEqual(left.station_id, right.station_id)
        self.assertEqual(left.scope, right.scope)
        self.assertEqual(left.code_version, right.code_version)
        self.assertEqual(left.service, right.service)
        self.assertEqual(left.link, right.link)
        self.assertEqual(left.mac, right.mac)
        self.assertEqual(left.phy, right.phy)
        self.assertEqual(left.sources, right.sources)
        self.assertEqual(left.logging, right.logging)
        self.assertEqual(left.security, right.security)
        self.assertEqual(left.test, right.test)


def _v3_software_equivalent() -> str:
    """같은 공통 table을 가진 frozen v3 SOFTWARE 설정 (비교용)."""

    return '''
schema_version = 3

[endpoint]
id = "MASS-A"
role = "SHIP"
station_id = 111111111
scope = "TEST"
code_version = "stage7-b210-system"

[service]
max_payload_bytes = 65531
max_concurrent_transfers = 8

[link]
supported_link_ids = [11, 17, 19]

[mac]
link_id = 11
tx_channel = "VDETER_LOWER_100_TEST"
rx_channel = "VDETER_UPPER_100_TEST"
transmit_lead_nanoseconds = 250000000
data_link_id = 17

[phy]
sample_rate_hz = 307200

[radio]
adapter = "SOFTWARE"
clock_reference = "INTERNAL"
sample_format = "cf32le"

[radio.timing]
profile = "INTERNAL_FREE_RUNNING"
clock_source = "INTERNAL"
time_source = "INTERNAL"
pps_source = "NONE"
mapping_attempt_count = 8
mapping_valid_for_nanoseconds = 1000000000
max_mapping_uncertainty_ns = 10000000

[radio.resource_limits]
tx_queue_max_count = 64
tx_queue_max_bytes = 33554432
rx_window_max_count = 64
rx_queue_max_count = 256
rx_queue_max_bytes = 67108864
stop_timeout_ns = 5000000000

[[radio.channels]]
name = "VDETER_LOWER_100_TEST"
center_frequency_hz = 157237500
sample_rate_hz = 307200
tx_gain_db = 0
rx_gain_db = 0
waveform_bandwidth_hz = 100000
analog_bandwidth_hz = 100000
tx_enabled = true
rx_enabled = false

[[radio.channels]]
name = "VDETER_UPPER_100_TEST"
center_frequency_hz = 161837500
sample_rate_hz = 307200
tx_gain_db = 0
rx_gain_db = 0
waveform_bandwidth_hz = 100000
analog_bandwidth_hz = 100000
tx_enabled = false
rx_enabled = true

[sources]
time = "SYSTEM"
random = "SYSTEM"
id = "UUID4"

[logging]
jsonl_enabled = true
console_enabled = false

[security]
private_key_ref = ""
certificate_ref = ""
trust_store_ref = "configs/b210_system/trust/tbb_trust_store.pem"
'''


class TC_STAGE7_LOADER_NEGATIVE(unittest.TestCase):
    def setUp(self):
        self.loader = ConfigurationLoader()

    def _expect_error(self, text, *codes, data_profile=DATA_PROFILE_17, **kwargs):
        result = self.loader.load_endpoint_configuration(
            text, data_profile, run_id="stage7-unit-run", **kwargs
        )
        self.assertTrue(result.is_err, "오류를 기대했지만 성공했습니다")
        if codes:
            self.assertIn(result.error.code, codes)
        return result.error

    def test_wrong_schema_version(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace("schema_version = 4", "schema_version = 3")),
            ErrorCode.CONFIG_SCHEMA_INVALID,
        )

    def test_unknown_top_level_key(self):
        self._expect_error(
            build_source(mutate=lambda t: t + "\n[extra]\nx = 1\n"),
            ErrorCode.CONFIG_UNKNOWN_FIELD,
        )

    def test_wrong_adapter(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                'adapter = "b210-stage7-affine"', 'adapter = "B210"'
            )),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_hardware_channel_key_forbidden(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "tx_enabled = true\nrx_enabled = true",
                "tx_enabled = true\nrx_enabled = true\ntx_device_channel = 0",
            )),
            ErrorCode.CONFIG_SCHEMA_INVALID,
        )

    def test_serial_placeholder_rejected_in_resolved(self):
        self._expect_error(
            build_source(serial="REQUIRED_UNSET"),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_wrong_station_id(self):
        self._expect_error(
            build_source(station_id=123456789), ErrorCode.CONFIG_SEMANTIC_INVALID
        )

    def test_wrong_usb_transport_for_mass_b(self):
        self._expect_error(
            build_mass_b_source(usb_transport="DIRECT_USB3_5000M"),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_mass_b_pi_bus_power_rejected(self):
        self._expect_error(
            build_mass_b_source(external_power="PI_BUS_POWER"),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_mass_b_required_unset_power_rejected(self):
        self._expect_error(
            build_mass_b_source(external_power="REQUIRED_UNSET"),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_wrong_role_direction(self):
        self._expect_error(
            build_source(upper_direction="tx_enabled = false\nrx_enabled = true"),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_nonzero_tx_gain(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace("tx_gain_db = 0", "tx_gain_db = 3", 1)),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_operational_without_gain_artifact(self):
        self._expect_error(
            build_source(run_mode="OPERATIONAL"), ErrorCode.CONFIG_SEMANTIC_INVALID
        )

    def test_rf_off_with_gain_artifact_rejected(self):
        gain = OperationalGainInput(
            endpoint_common_rx_gain_db=3,
            source_gain_artifact_sha256=TEST_MANIFEST_SHA,
        )
        self._expect_error(
            build_source(), ErrorCode.CONFIG_SEMANTIC_INVALID, operational_gain=gain
        )

    def test_wrong_run_mode(self):
        self._expect_error(
            build_source(run_mode="CALIBRATE"), ErrorCode.CONFIG_SEMANTIC_INVALID
        )

    def test_wrong_registry_fingerprint(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "0a2b8cb8a72727bd54c05c5dd6e6ac5aeeec7a0b51f6ebe41a72bfa2482ae5d2",
                "0" * 64,
            )),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_bool_as_integer_rejected(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "cfo_accept_bound_hz = 1800", "cfo_accept_bound_hz = true"
            )),
            ErrorCode.CONFIG_SCHEMA_INVALID,
        )

    def test_missing_stage7_key(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace("cfo_accept_bound_hz = 1800\n", "")),
            ErrorCode.CONFIG_SCHEMA_INVALID,
        )

    def test_wrong_fixed_value(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "active_command_horizon_s = 120", "active_command_horizon_s = 121"
            )),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_deployment_manifest_placeholder_rejected(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                'deployment_manifest_relpath = "results/runs/stage7-test/deployment_manifest.json"',
                'deployment_manifest_relpath = "REQUIRED_UNSET"',
            )),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_data_link_id_in_mac_rejected(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "transmit_lead_nanoseconds = 250000000",
                "transmit_lead_nanoseconds = 250000000\ndata_link_id = 17",
            )),
            ErrorCode.CONFIG_UNKNOWN_FIELD,
        )

    def test_wrong_analog_bandwidth(self):
        self._expect_error(
            build_source(mutate=lambda t: t.replace(
                "analog_bandwidth_hz = 200000", "analog_bandwidth_hz = 100000", 1
            )),
            ErrorCode.CONFIG_SEMANTIC_INVALID,
        )

    def test_wrong_channel_order(self):
        def swap(text: str) -> str:
            lower_start = text.index("[[radio.channels]]")
            sources_start = text.index("[sources]")
            block = text[lower_start:sources_start]
            first, second = block.split("[[radio.channels]]")[1:]
            swapped = (
                "[[radio.channels]]" + second.rstrip() + "\n\n[[radio.channels]]" + first
            )
            return text[:lower_start] + swapped + "\n" + text[sources_start:]

        self._expect_error(build_source(mutate=swap), ErrorCode.CONFIG_SEMANTIC_INVALID)

    def test_frozen_loader_rejects_v4(self):
        """frozen v1~3 loader가 schema v4를 절대 처리하지 않는지 확인한다."""

        result = load_configuration(build_source())
        self.assertTrue(result.is_err)
        # frozen loader는 top-level unknown key(host_execution/stage7)를 먼저
        # 거부한다. 어느 코드든 v4는 구조화 거부되며 dispatch 0이다.
        self.assertIn(
            result.error.code,
            (ErrorCode.CONFIG_UNKNOWN_FIELD, ErrorCode.CONFIG_SCHEMA_INVALID),
        )


if __name__ == "__main__":
    unittest.main()


def build_pi_powered_hub_source(**overrides) -> str:
    """Pi powered-USB3-hub host profile source (2026-09-10): same Pi, every endpoint
    a 5000M hub link on hub bus power; host_machine_id/usb_physical_path provisioned."""
    defaults = dict(usb_transport="POWERED_USB3_HUB_5000M", usb_path="5-1.2",
                    external_power="POWERED_USB3_HUB_BUS_POWER")
    defaults.update(overrides)

    def to_hub_profile(text: str) -> str:
        assert text.count('profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"') == 1
        return text.replace('profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"',
                            'profile = "STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1"')
    mutate = defaults.pop("mutate", None)
    defaults["mutate"] = (lambda t: mutate(to_hub_profile(t))) if mutate else to_hub_profile
    return build_source(**defaults)


class TC_STAGE7_HOST_PROFILE_PI_POWERED_HUB(unittest.TestCase):
    """host_execution.profile STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1 (additive) — the
    legacy Pi profile keeps its exact MASS-B USB2/DC-asset rules; the powered-hub
    profile requires POWERED_USB3_HUB_5000M + POWERED_USB3_HUB_BUS_POWER on every
    endpoint. Both rule sets come from vdeter.b210_system.host_profiles."""

    def setUp(self):
        self.loader = ConfigurationLoader()

    def _load(self, text):
        return self.loader.load_endpoint_configuration(
            text, DATA_PROFILE_17, run_id="stage7-unit-run")

    def _expect_error(self, text, needle):
        result = self._load(text)
        self.assertTrue(result.is_err, "expected rejection: %s" % needle)
        self.assertEqual(result.error.code, ErrorCode.CONFIG_SEMANTIC_INVALID)
        self.assertIn(needle, str(result.error))

    def test_profiles_and_transports_come_from_single_definition(self):
        from vdeter.b210_system import configuration, host_profiles
        self.assertEqual(configuration.HOST_PROFILE_TABLE, host_profiles.HOST_PROFILES)
        self.assertEqual(configuration.HOST_PROFILE, host_profiles.LEGACY_PI_PROFILE)
        self.assertEqual(configuration.HOST_PROFILE_PI_POWERED_HUB,
                         host_profiles.PI_POWERED_HUB_PROFILE)
        self.assertEqual(configuration.USB_TRANSPORTS, host_profiles.USB_TRANSPORTS)
        self.assertEqual(configuration.USB_TRANSPORTS[:2],
                         ("DIRECT_USB3_5000M", "DIRECT_USB2_480M_EXTERNAL_DC"))

    def test_mass_b_resolves_on_hub_without_dc_asset(self):
        result = self._load(build_pi_powered_hub_source(
            endpoint_id="MASS-B", station_id=333333333, serial="3515425", usb_path="5-1.1"))
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        host = result.value.host_execution
        self.assertEqual(host.profile, "STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1")
        self.assertEqual(host.usb_transport, "POWERED_USB3_HUB_5000M")
        self.assertEqual(host.external_power_asset, "POWERED_USB3_HUB_BUS_POWER")
        self.assertEqual(host.pi_power_profile, "PI5_5V5A_HIGH_CURRENT")
        self.assertEqual(result.value.b210_serial, "3515425")

    def test_mass_a_and_roc_resolve_on_hub(self):
        self.assertTrue(self._load(build_pi_powered_hub_source()).is_ok)
        roc = build_roc_source(usb_transport="POWERED_USB3_HUB_5000M", usb_path="5-1.4",
                               external_power="POWERED_USB3_HUB_BUS_POWER",
                               mutate=lambda t: t.replace(
                                   'profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"',
                                   'profile = "STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1"'))
        result = self._load(roc)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(result.value.host_execution.usb_transport, "POWERED_USB3_HUB_5000M")

    def test_hub_profile_refuses_dc_asset_pi_bus_and_direct_transports(self):
        for overrides, needle in (
            (dict(external_power="AT2024T"), "POWERED_USB3_HUB_BUS_POWER"),
            (dict(external_power="PI_BUS_POWER"), "POWERED_USB3_HUB_BUS_POWER"),
            (dict(external_power="REQUIRED_UNSET"), "placeholder"),
            (dict(usb_transport="DIRECT_USB3_5000M"), "canonical USB transport"),
            (dict(usb_transport="DIRECT_USB2_480M_EXTERNAL_DC"), "canonical USB transport"),
        ):
            with self.subTest(overrides=overrides):
                self._expect_error(build_pi_powered_hub_source(
                    endpoint_id="MASS-B", station_id=333333333, serial="3515425",
                    usb_path="5-1.1", **overrides), needle)

    def test_legacy_profile_rules_are_not_relaxed(self):
        # legacy MASS-B still needs USB2 + a real DC asset; the hub tokens are refused.
        self._expect_error(build_mass_b_source(usb_transport="POWERED_USB3_HUB_5000M"),
                           "canonical USB transport")
        self._expect_error(build_mass_b_source(external_power="POWERED_USB3_HUB_BUS_POWER"),
                           "external_power_asset")
        self._expect_error(build_source(external_power="POWERED_USB3_HUB_BUS_POWER"),
                           "PI_BUS_POWER")
        self._expect_error(build_source(usb_transport="POWERED_USB3_HUB_5000M"),
                           "canonical USB transport")
        self.assertTrue(self._load(build_mass_b_source()).is_ok)

    def test_unknown_profile_rejected(self):
        self._expect_error(build_source(mutate=lambda t: t.replace(
            'profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"',
            'profile = "STAGE7_PI5_POWERED_USB3_HUB_UNKNOWN"')), "canonical Stage 7 profile")
