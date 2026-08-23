"""wideband 2-PC 3-endpoint 통합 시험 (`DR-STAGE6-MR-001` §13.2.2).

정확히 두 개의 100 kHz alias 위에서 Link 11 제어(변환 경계)와 Link 17·19 data session이
함께 동작하는지, 그리고 무-fallback 조건이 지켜지는지 검증한다.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
)
from tests.support.stage5 import (
    drive_stage5_group,
    received_payloads,
    start_stage5_group,
)
from tests.support.wideband import (
    VDETER_ROOT,
    build_wideband_descriptors,
    build_wideband_software_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import InternalEventType
from vdeter.common.identifiers import PayloadFormat
from vdeter.config.loader import load_configuration
from vdeter.runtime.wiring import build_wideband_group

FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)
OPAQUE = PayloadFormat("test.stage0.opaque")
#: §13.2.2 통합 payload: `00..ff` byte sequence 세 번, 정확히 768 bytes.
PAYLOAD_768 = bytes(range(256)) * 3


def _internal_events(endpoint):
    sink = endpoint.dependencies.internal_event_sink
    return sink.events() if hasattr(sink, "events") else getattr(sink, "_sink").events()


def _recovered_data_link_ids(endpoint):
    """54 bytes보다 큰(=wideband data) 복원 Packet의 Link ID 집합."""

    observed = set()
    for event in _internal_events(endpoint):
        if event.event_type is not InternalEventType.PACKETS_RECOVERED:
            continue
        for packet in getattr(event.payload, "packets", ()):
            if len(packet.packet) > 54:
                observed.add(packet.link_id.value)
    return observed


def _drive_transfer(clock, group, source, transfer_id, max_steps=700):
    state = None
    for _ in range(max_steps):
        drive_stage5_group(clock, group, 1)
        state = source.status(TransferStatusRequest(transfer_id)).value.state
        if state in (
            TransferState.COMPLETED,
            TransferState.FAILED,
            TransferState.CANCELLED,
        ):
            break
    return state


class WidebandGroupIdentityTest(unittest.TestCase):
    def test_group_start_uses_the_2pc_wideband_slotmap(self):
        clock, group = build_wideband_software_group(
            data_link_id=17, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            profiles = set()
            for endpoint in group.endpoints:
                active = endpoint.stage4.mac.state.active_tbb
                self.assertIsNotNone(active)
                self.assertEqual("VDETER_WIDEBAND_100_TBB_V3", active.slot_map.map_id)
                channels = {
                    slot.physical_channel.name for slot in active.slot_map.slots
                }
                self.assertEqual(
                    {"VDETER_LOWER_100_TEST", "VDETER_UPPER_100_TEST"}, channels
                )
                wideband = endpoint._wideband_profile
                profiles.add(
                    (
                        wideband.fixture_id,
                        wideband.data_link_id,
                        wideband.base_canonical_tbb_fingerprint,
                        wideband.wideband_canonical_tbb_fingerprint,
                        wideband.wideband_deployment_fingerprint,
                        wideband.combined_registry_content_version,
                        wideband.combined_registry_fingerprint,
                    )
                )
            # 세 endpoint의 wideband identity는 exact-equal이다.
            self.assertEqual(1, len(profiles))
            identity = next(iter(profiles))
            self.assertEqual("VDETER_WIDEBAND_100_TBB_V3", identity[0])
            self.assertEqual(17, identity[1])
            self.assertEqual("stage6-phy11-17-19-v7", identity[5])
        finally:
            group.stop()


class _ScenarioMixin:
    data_link_id = 17

    def _run_direct(self, source_station, destination_station):
        clock, group = build_wideband_software_group(
            data_link_id=self.data_link_id, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            by_station = group.by_station()
            source = by_station[source_station.value]
            destination = by_station[destination_station.value]
            accepted = source.submit(
                SubmitTransferRequest(
                    destination=Addressed(destination_station),
                    payload_format=OPAQUE,
                    payload=PAYLOAD_768,
                    ack_required=True,
                )
            )
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            state = _drive_transfer(clock, group, source, accepted.value.transfer_id)
            self.assertIs(TransferState.COMPLETED, state)
            payloads = received_payloads(destination)
            self.assertEqual([PAYLOAD_768], payloads)
            other = {17, 19} - {self.data_link_id}
            observed = _recovered_data_link_ids(destination)
            # data Packet은 모두 실행 `data_link_id`이고 다른 Link로의 fallback이 없다.
            self.assertEqual({self.data_link_id}, observed)
            self.assertFalse(other & observed)
        finally:
            group.stop()

    def _run_relay(self):
        clock, group = build_wideband_software_group(
            data_link_id=self.data_link_id, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            by_station = group.by_station()
            ship_a = by_station[MASS_A_STATION_ID.value]
            ship_b = by_station[MASS_B_STATION_ID.value]
            roc = by_station[ROC_STATION_ID.value]
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=PAYLOAD_768,
                    ack_required=True,
                )
            )
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            payloads_b = []
            for _ in range(900):
                drive_stage5_group(clock, group, 1)
                payloads_b = received_payloads(ship_b)
                if payloads_b:
                    break
            self.assertEqual([PAYLOAD_768], payloads_b)
            # ROC application 전달은 0회이고, 두 hop 모두 실행 data Link ID다.
            self.assertEqual([], received_payloads(roc))
            self.assertEqual({self.data_link_id}, _recovered_data_link_ids(roc))
            self.assertEqual({self.data_link_id}, _recovered_data_link_ids(ship_b))
        finally:
            group.stop()


class WidebandLink17ScenarioTest(_ScenarioMixin, unittest.TestCase):
    data_link_id = 17

    def test_mass_a_to_roc(self):
        self._run_direct(MASS_A_STATION_ID, ROC_STATION_ID)

    def test_roc_to_mass_a(self):
        self._run_direct(ROC_STATION_ID, MASS_A_STATION_ID)

    def test_mass_a_to_mass_b_via_roc(self):
        self._run_relay()


class WidebandLink19ScenarioTest(_ScenarioMixin, unittest.TestCase):
    data_link_id = 19

    def test_mass_a_to_roc(self):
        self._run_direct(MASS_A_STATION_ID, ROC_STATION_ID)

    def test_roc_to_mass_a(self):
        self._run_direct(ROC_STATION_ID, MASS_A_STATION_ID)

    def test_mass_a_to_mass_b_via_roc(self):
        self._run_relay()


class WidebandNegativeTest(unittest.TestCase):
    def _ship_toml(self) -> str:
        return (VDETER_ROOT / "configs" / "wideband_software_ship.toml").read_text(
            encoding="utf-8"
        )

    def test_data_link_outside_supported_set_is_rejected(self):
        text = self._ship_toml().replace(
            "supported_link_ids = [11, 17, 19]", "supported_link_ids = [11, 19]"
        )
        loaded = load_configuration(text, "<neg>")
        self.assertTrue(loaded.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", loaded.error.code)

    def test_v2_schema_rejects_a_wideband_data_link(self):
        stage5 = (VDETER_ROOT / "configs" / "stage5_software_ship.toml").read_text(
            encoding="utf-8"
        )
        text = stage5.replace("[mac]\nlink_id = 11", "[mac]\nlink_id = 11\ndata_link_id = 17")
        loaded = load_configuration(text, "<neg>")
        self.assertTrue(loaded.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", loaded.error.code)

    def test_v3_schema_rejects_link11_data_link(self):
        text = self._ship_toml().replace("data_link_id = 17", "data_link_id = 11")
        loaded = load_configuration(text, "<neg>")
        self.assertTrue(loaded.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", loaded.error.code)

    def test_v3_schema_rejects_native_channel_rate(self):
        text = self._ship_toml().replace("sample_rate_hz = 307200", "sample_rate_hz = 76800")
        loaded = load_configuration(text, "<neg>")
        self.assertTrue(loaded.is_err)
        self.assertIn(
            loaded.error.code, ("CONFIG_SEMANTIC_INVALID", "RADIO_UNSUPPORTED_SETTING")
        )

    def test_v3_schema_rejects_25khz_bandwidth(self):
        text = self._ship_toml().replace(
            "waveform_bandwidth_hz = 100000", "waveform_bandwidth_hz = 25000"
        )
        loaded = load_configuration(text, "<neg>")
        self.assertTrue(loaded.is_err)
        self.assertEqual("RADIO_UNSUPPORTED_SETTING", loaded.error.code)

    def test_group_with_mixed_data_link_ids_is_rejected(self):
        clock, descriptors, crypto = build_wideband_descriptors(data_link_id=17)
        _clock_19, descriptors_19, _crypto_19 = build_wideband_descriptors(data_link_id=19)
        mixed = (descriptors[0], descriptors[1], descriptors_19[2])
        built = build_wideband_group(mixed, crypto_by_station=crypto)
        self.assertTrue(built.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", built.error.code)

    def test_NEG_001_missing_wideband_tx_profile_is_rejected(self):
        """NEG-001 부분: 선택 TX profile(17 또는 19)이 registry에 없으면 시작 거부."""

        from dataclasses import replace as dataclass_replace

        from vdeter.common.results import Ok
        from vdeter.profiles import link_phy
        from vdeter.profiles.models import ExplicitRegistry, RegistryScope

        for removed, data_link_id in ((17, 17), (19, 19)):
            with self.subTest(removed=removed):
                clock, descriptors, crypto = build_wideband_descriptors(
                    data_link_id=data_link_id
                )
                entries = tuple(
                    entry
                    for entry in link_phy.combined_phy_profile_entries()
                    if entry.link_id.value != removed
                )
                broken = ExplicitRegistry(
                    "phy_profile",
                    RegistryScope.OPERATIONAL,
                    link_phy.STAGE6_REGISTRY_CONTENT_VERSION,
                    link_phy.PhyProfileEntry,
                )
                registered = broken.register_all(entries)
                self.assertIsInstance(registered, Ok)
                frozen = broken.freeze()
                self.assertTrue(frozen.is_ok)
                crippled = tuple(
                    (
                        dataclass_replace(
                            dependencies,
                            registries=dataclass_replace(
                                dependencies.registries, phy_profile=broken
                            ),
                        ),
                        deployment,
                    )
                    for dependencies, deployment in descriptors
                )
                built = build_wideband_group(crippled, crypto_by_station=crypto)
                self.assertTrue(built.is_err)
                self.assertEqual("CONFIG_SEMANTIC_INVALID", built.error.code)


if __name__ == "__main__":
    unittest.main()
