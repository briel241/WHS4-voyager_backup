"""NEG-001(b)/NEG-002/NEG-003: 통합 seam 주입형 negative 시험.

요구사항: 공통 §13.2.2 — 모두 MASS-A→ROC 경로 맥락에서 실행하며 Link 11
fallback이 없어야 한다.

- NEG-001 잔여 2: config/registry는 정상인 채 MAC의 선택 Link capability만
  제거(coordinator `_link_id_for` seam) → MAC `UNSUPPORTED_PROFILE` 거부.
- NEG-002 2: MASS-A lower TX setting을 post-load로 변조(loader 우회, adapter
  적용/검증 시점 거부) → `RADIO_UNSUPPORTED_SETTING` 원 오류 보존, transfer
  `DEPENDENCY_FAILED`류 종료.
- NEG-003 6: 제어 window에 17/19, DC window에 11(`_window_intent_for` seam 우회
  주입 → coordinator mapping guard가 `CONFIG_SEMANTIC_INVALID`), ROC data
  window의 native 76,800 계획(post-load phy rate 변조 → runtime timing guard),
  empty tuple 주입(`PhyReceiveWindowPlan` constructor 우회 → bridge가
  ledger·Radio 전 거부).
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from vdeter.mac.coordinator import MacCoordinator
from vdeter.radio.software import SoftwareRadioAdapter

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
)
from tests.support.stage5 import drive_stage5_group, start_stage5_group
from tests.support.wideband import build_wideband_software_group
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.mac_execution import PhyReceiveWindowPlan
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.errors import Correlation
from vdeter.common.identifiers import PayloadFormat
from vdeter.mac.tbb import SlotFunction

FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)
OPAQUE = PayloadFormat("test.stage0.opaque")
PAYLOAD_768 = bytes(range(256)) * 3


def _internal_error_codes(endpoint):
    """internal event 오류와 Stage 4 health 기록을 합친 (code, details) 목록."""

    sink = endpoint.dependencies.internal_event_sink
    events = sink.events() if hasattr(sink, "events") else getattr(sink, "_sink").events()
    codes = []
    for event in events:
        error = getattr(event, "error", None)
        if error is not None:
            codes.append((str(error.code), dict(error.details.as_dict())))
    stage4 = getattr(endpoint, "stage4", None)
    if stage4 is not None:
        for code, _reason, details in stage4.health.entries():
            codes.append((str(code), dict(details.as_dict())))
    return codes


def _drive_transfer(clock, group, source, transfer_id, max_steps=500):
    state = None
    for _ in range(max_steps):
        drive_stage5_group(clock, group, 1)
        state = source.status(TransferStatusRequest(transfer_id)).value.state
        if state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED):
            break
    return state


class Neg001MacCapabilityTest(unittest.TestCase):
    """NEG-001 잔여: MAC capability만 제거 → UNSUPPORTED_PROFILE, fallback 없음."""

    def _run(self, data_link_id: int):
        clock, group = build_wideband_software_group(
            data_link_id=data_link_id, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            by_station = group.by_station()
            mass_a = by_station[MASS_A_STATION_ID.value]
            original = MacCoordinator._link_id_for

            def crippled(self, link_id_value: int = 11):
                if link_id_value == data_link_id:
                    from vdeter.mac import transfers as transfer_module

                    return transfer_module.unsupported_profile_error(link_id_value)
                return original(self, link_id_value)

            with patch.object(MacCoordinator, "_link_id_for", crippled):
                accepted = mass_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(ROC_STATION_ID),
                        payload_format=OPAQUE,
                        payload=PAYLOAD_768,
                        ack_required=True,
                    )
                )
                self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
                state = _drive_transfer(
                    clock, group, mass_a, accepted.value.transfer_id
                )
            self.assertIs(TransferState.FAILED, state)
            snapshot = mass_a.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value
            self.assertIsNotNone(snapshot.error)
            unsupported = [
                (code, details)
                for code, details in _internal_error_codes(mass_a)
                if details.get("reason_code") == "UNSUPPORTED_PROFILE"
                or "UNSUPPORTED_PROFILE" in str(code)
            ]
            self.assertGreater(len(unsupported), 0)
            # Link 11 fallback 없음: ROC에 payload가 전달되지 않았다.
            from tests.support.stage5 import received_payloads

            roc = by_station[ROC_STATION_ID.value]
            self.assertEqual([], received_payloads(roc))
        finally:
            group.stop()

    def test_NEG_001_link17_capability_removed(self):
        self._run(17)

    def test_NEG_001_link19_capability_removed(self):
        self._run(19)

    def _run_binding_removed(self, data_link_id):
        """NEG-001: `VDETER_LOWER_100_TEST` TX binding 제거 → 시작 거부."""

        from pathlib import Path

        from vdeter.config.loader import load_configuration

        ship = (
            Path(__file__).resolve().parents[2] / "configs" / "wideband_software_ship.toml"
        ).read_text(encoding="utf-8")
        text = ship.replace("data_link_id = 17", f"data_link_id = {data_link_id}")
        text = text.replace("tx_enabled = true", "tx_enabled = false", 1)
        loaded = load_configuration(text, "<neg001-binding>")
        self.assertTrue(loaded.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", loaded.error.code)

    def test_NEG_001_link17_lower_tx_binding_removed(self):
        self._run_binding_removed(17)

    def test_NEG_001_link19_lower_tx_binding_removed(self):
        self._run_binding_removed(19)


class Neg002AdapterApplyTest(unittest.TestCase):
    """NEG-002: adapter 적용/검증 시점의 lower TX setting 거부."""

    def _run(self, data_link_id: int):
        def mutator(endpoint_id, configuration):
            if endpoint_id != "wideband-ship-a":
                return configuration
            # loader 우회(post-load): lower TX setting rate를 비정합값으로 변조.
            channels = tuple(
                replace(setting, sample_rate_hz=153_600)
                if setting.channel.name == "VDETER_LOWER_100_TEST"
                else setting
                for setting in configuration.radio.channels
            )
            return replace(
                configuration, radio=replace(configuration.radio, channels=channels)
            )

        clock, group = build_wideband_software_group(
            data_link_id=data_link_id,
            type0_override=FAST_TYPE0,
            configuration_mutator=mutator,
        )
        try:
            by_station = group.by_station()
            mass_a = by_station[MASS_A_STATION_ID.value]
            # 이 조립에서 adapter는 setting을 start의 configure에서 적용·검증하므로
            # "적용 거부"는 start 실패로 표면화된다. 원 오류는 internal evidence에
            # 보존되고, 시작 불가로 어떤 transfer도 (fallback 포함) 진행될 수 없다.
            started = group.start()
            self.assertTrue(started.is_err)
            codes = [code for code, _ in _internal_error_codes(mass_a)]
            self.assertIn("RADIO_UNSUPPORTED_SETTING", codes)
            rejected = mass_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(ROC_STATION_ID),
                    payload_format=OPAQUE,
                    payload=PAYLOAD_768,
                    ack_required=True,
                )
            )
            self.assertTrue(rejected.is_err)
            from tests.support.stage5 import received_payloads

            self.assertEqual([], received_payloads(by_station[ROC_STATION_ID.value]))
        finally:
            group.stop()

    def test_NEG_002_link17_lower_tx_setting_rejected(self):
        self._run(17)

    def test_NEG_002_link19_lower_tx_setting_rejected(self):
        self._run(19)


class Neg003WindowIntentTest(unittest.TestCase):
    """NEG-003: window intent·rate·empty tuple 위반의 계획-전 거부."""

    def _run_intent(self, data_link_id: int, inject_on_control: int | None,
                    inject_on_data: int | None, expected_observed: int,
                    expected_expected: int):
        clock, group = build_wideband_software_group(
            data_link_id=data_link_id, type0_override=FAST_TYPE0
        )
        try:
            by_station_pre = group.by_station()
            mass_a = by_station_pre[MASS_A_STATION_ID.value]
            original = MacCoordinator._window_intent_for

            def nonconforming(self, window, control_link_id, data_link):
                if window.slot_function in (SlotFunction.DC, SlotFunction.DSCH):
                    return inject_on_data if inject_on_data is not None else original(
                        self, window, control_link_id, data_link
                    )
                return inject_on_control if inject_on_control is not None else original(
                    self, window, control_link_id, data_link
                )

            with patch.object(MacCoordinator, "_window_intent_for", nonconforming):
                started = group.start()
                self.assertTrue(started.is_ok, getattr(started, "error", None))
                mapping_errors = []
                for _ in range(120):
                    drive_stage5_group(clock, group, 1)
                    for endpoint in group.endpoints:
                        mapping_errors += [
                            details
                            for _code, details in _internal_error_codes(endpoint)
                            if details.get("procedure") == "RX_WINDOW_INTENT"
                        ]
                    if mapping_errors:
                        break
                # 위반 window는 계획 전에 거부되고 Link 11 fallback 계획이 없다.
                self.assertGreater(len(mapping_errors), 0)
                sample = mapping_errors[0]
                self.assertEqual(expected_observed, sample.get("observed_link_id"))
                self.assertEqual(expected_expected, sample.get("expected_link_id"))
                functions = {
                    entry["slot_function"]
                    for endpoint in group.endpoints
                    for entry in endpoint._phy_radio_bridge.ledger_snapshot()
                }
                self.assertNotIn("CONTROL", functions)
        finally:
            group.stop()

    def test_NEG_003_control_window_with_link17_is_rejected(self):
        self._run_intent(17, inject_on_control=17, inject_on_data=None,
                         expected_observed=17, expected_expected=11)

    def test_NEG_003_control_window_with_link19_is_rejected(self):
        self._run_intent(19, inject_on_control=19, inject_on_data=None,
                         expected_observed=19, expected_expected=11)

    def test_NEG_003_data_window_with_link11_is_rejected(self):
        # DC/DSCH window는 할당 뒤에만 계획되므로 실행 중 주입한다.
        clock, group = build_wideband_software_group(
            data_link_id=17, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            by_station = group.by_station()
            mass_a = by_station[MASS_A_STATION_ID.value]
            original = MacCoordinator._window_intent_for

            def nonconforming(self, window, control_link_id, data_link):
                if window.slot_function in (SlotFunction.DC, SlotFunction.DSCH):
                    return 11
                return original(self, window, control_link_id, data_link)

            with patch.object(MacCoordinator, "_window_intent_for", nonconforming):
                accepted = mass_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(ROC_STATION_ID),
                        payload_format=OPAQUE,
                        payload=PAYLOAD_768,
                        ack_required=True,
                    )
                )
                self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
                mapping_errors = []
                for _ in range(200):
                    drive_stage5_group(clock, group, 1)
                    for endpoint in group.endpoints:
                        mapping_errors += [
                            details
                            for code, details in _internal_error_codes(endpoint)
                            if details.get("procedure") == "RX_WINDOW_INTENT"
                        ]
                    if mapping_errors:
                        break
            self.assertGreater(len(mapping_errors), 0)
            self.assertEqual(11, mapping_errors[0].get("observed_link_id"))
            self.assertEqual(17, mapping_errors[0].get("expected_link_id"))
            # Link 11 fallback 없음: ROC application에 payload가 전달되지 않았다.
            from tests.support.stage5 import received_payloads

            self.assertEqual([], received_payloads(by_station[ROC_STATION_ID.value]))
        finally:
            group.stop()

    def _run_native_rate(self, data_link_id: int):
        def mutator(endpoint_id, configuration):
            if endpoint_id != "wideband-roc":
                return configuration
            # loader 우회: ROC의 RX 계획 rate를 native 76,800으로 변조.
            endpoint = configuration.endpoint
            return replace(
                configuration,
                endpoint=replace(
                    endpoint, phy=replace(endpoint.phy, sample_rate_hz=76_800)
                ),
            )

        clock, group = build_wideband_software_group(
            data_link_id=data_link_id,
            type0_override=FAST_TYPE0,
            configuration_mutator=mutator,
        )
        try:
            group.start()
            drive_stage5_group(clock, group, 40)
            roc = group.by_station()[ROC_STATION_ID.value]
            timing_errors = [
                details
                for _code, details in _internal_error_codes(roc)
                if details.get("expected_sample_count") == 2_048
            ]
            self.assertGreater(len(timing_errors), 0)
            # Radio 호출 전 거부: ROC ledger에 admitted window가 없다.
            self.assertEqual((), roc._phy_radio_bridge.ledger_snapshot())
        finally:
            group.stop()

    def test_NEG_003_roc_native_rate_plan_link17(self):
        self._run_native_rate(17)

    def test_NEG_003_roc_native_rate_plan_link19(self):
        self._run_native_rate(19)

    def test_NEG_003_empty_tuple_injection_at_runtime_seam(self):
        clock, group = build_wideband_software_group(
            data_link_id=17, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group, acquisition_steps=80)
            mass_a = group.by_station()[MASS_A_STATION_ID.value]
            stage4 = mass_a.stage4
            bridge = mass_a._phy_radio_bridge
            ledger_before = bridge.ledger_snapshot()
            plan = Mock(spec=PhyReceiveWindowPlan)
            plan.allowed_link_ids = ()
            plan.channel = Mock()
            plan.channel.name = "VDETER_UPPER_100_TEST"
            plan.duration_nanoseconds = 26_666_666
            command = Mock()
            command.command_id = "neg003-empty-intent"
            command.correlation = Correlation()
            command.causation_event_id = "neg003-cause"
            with patch.object(
                SoftwareRadioAdapter, "request_rx", autospec=True
            ) as radio_spy:
                stage4._execute_rx(command, plan)
                # ledger entry·Radio 요청을 만들기 전에 거부한다.
                radio_spy.assert_not_called()
            self.assertEqual(len(ledger_before), len(bridge.ledger_snapshot()))
            stage4.pump()
            # 구조화 code(CONFIG_SEMANTIC_INVALID) 자체는 ledger 단위 gate
            # (TC-PHY-MULTI-010-10)가 고정한다. 여기서는 §13.2의 통합 계약 —
            # ledger entry·Radio 요청 부재 — 를 고정했다.
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
