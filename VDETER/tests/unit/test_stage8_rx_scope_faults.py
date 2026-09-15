import unittest
from types import SimpleNamespace as NS
from threading import RLock
from unittest.mock import Mock
from vdeter.b210_system.rx_scope import WorkloadRxScope
from vdeter.b210_system.collision_runtime import CollisionRuntimeError
from vdeter.b210_system.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME, LOWER_RF_TUNE_HZ, UPPER_RF_TUNE_HZ


class RxScopeFaultTests(unittest.TestCase):
    def setUp(self):
        self.previous = {"lane_center_hz": {0: UPPER_RF_TUNE_HZ, 1: UPPER_RF_TUNE_HZ}, "analog_bandwidth_hz": 200000}
        mid = (LOWER_RF_TUNE_HZ + UPPER_RF_TUNE_HZ) // 2
        self.readback = dict(rx_lo_hz=mid, lane_dsp_hz={0: -2300000, 1: 2300000},
            lane_center_hz={0: UPPER_RF_TUNE_HZ, 1: LOWER_RF_TUNE_HZ}, analog_bandwidth_hz=4700000)
        self.worker = Mock(read_tuning=Mock(return_value=self.previous),
            apply_manual_tuning=Mock(return_value=self.readback), restore_tuning=Mock(return_value=self.previous))
        self.radio = NS(_generation=1, bridge=NS(clock_id="bridge", generation=2),
            _condition=RLock(), _rx_children={}, set_rx_lane_channels=Mock())
        self.support = NS(mac=NS(coordinator=NS(_rx_planner_paused=True, _observer_channel_names=())),
            lane_dispatcher=NS(_windows={}, stacks=[]), _rx_commands={}, collision_gate=None)
        seam = NS(_radio=lambda: self.radio, _worker=lambda: self.worker,
            _endpoint=NS(stage4=self.support), endpoint_id="MASS-B", _station_id_by_endpoint={})
        self.scope = WorkloadRxScope(seam, "owner", True)
        self.scope._replan = Mock()

    def test_pending_windows_prevent_tuning(self):
        self.support._rx_commands["old"] = object()
        self.assertFalse(self.scope.activate()["active"])
        self.worker.apply_manual_tuning.assert_not_called()

    def test_lane_mapping_and_restore(self):
        self.scope.activate()
        self.radio.set_rx_lane_channels.assert_called_with({0: UPPER_CHANNEL_NAME, 1: LOWER_CHANNEL_NAME})
        self.assertFalse(self.scope.poll()["dual_ready"])
        for i, channel in enumerate((LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME)):
            self.radio._rx_children[i] = NS(terminal=False, request=NS(channel=NS(name=channel)))
        self.assertTrue(self.scope.poll()["dual_ready"])
        self.scope.close()
        self.assertFalse(self.scope.disable()["disabled"])
        self.radio._rx_children.clear()
        self.assertTrue(self.scope.disable()["disabled"])
        self.radio.set_rx_lane_channels.assert_called_with(None)

    def test_partial_apply_and_interrupt_remain_owned_for_restore(self):
        self.worker.apply_manual_tuning.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.scope.activate()
        self.assertTrue(self.scope.touched)
        self.scope.close()
        self.assertTrue(self.scope.disable()["disabled"])
        self.worker.restore_tuning.assert_called_once()

    def test_bad_readback_is_not_capability(self):
        self.readback["lane_center_hz"][1] = UPPER_RF_TUNE_HZ
        with self.assertRaises(CollisionRuntimeError):
            self.scope.activate()
        self.assertFalse(self.scope.active)
        self.scope.close()
        self.assertTrue(self.scope.disable()["disabled"])

    def test_restore_failure_retains_ownership_and_retries(self):
        self.scope.activate()
        self.scope.close()
        self.worker.restore_tuning.side_effect = RuntimeError("USB readback failed")
        with self.assertRaises(RuntimeError):
            self.scope.disable()
        self.assertTrue(self.scope.touched)
        self.worker.restore_tuning.side_effect = None
        self.assertTrue(self.scope.disable()["disabled"])

    def test_restart_fences_observations(self):
        self.scope.activate()
        self.radio._generation += 1
        self.assertEqual("RX_CLOCK_GENERATION_CHANGED", self.scope.poll()["fault"])

    def test_missing_restore_snapshot_never_tunes(self):
        self.worker.read_tuning.return_value = {}
        with self.assertRaises(CollisionRuntimeError):
            self.scope.activate()
        self.worker.apply_manual_tuning.assert_not_called()

    # --- readback 값 보존 (라운드 4 미확인 항목 "tuning readback 값") ---------------

    def test_poll_returns_previous_and_applied_readback(self):
        """적용 전(previous)·적용 후(readback) 값이 POLL 응답에 실린다 — 둘 다 activate가 이미
        확보한 값이라 여기서 장치를 다시 묻지 않는다."""
        self.scope.activate()
        reply = self.scope.poll()
        self.assertEqual(reply["dual_previous"], self.previous)
        self.assertEqual(reply["dual_readback"], self.readback)
        self.worker.read_tuning.assert_called_once()

    def test_disable_returns_restore_readback_and_verdict(self):
        """disable()가 복원 목표·실측·검증 결과를 함께 돌려준다(추가 조회 없음)."""
        self.scope.activate()
        self.scope.close()
        reply = self.scope.disable()
        self.assertTrue(reply["disabled"])
        self.assertEqual(reply["restore_previous"], self.previous)
        self.assertEqual(reply["restore_readback"], self.previous)
        self.assertIs(reply["restore_verified"], True)
        self.worker.restore_tuning.assert_called_once()
        self.worker.read_tuning.assert_called_once()

    def test_restore_mismatch_keeps_values_and_cause(self):
        """복원 readback이 목표와 어긋나면 예외(소유권 유지)이고, 값과 원인이 남는다.
        B210 재발견이나 예외 부재가 아니라 **수치 비교**가 PASS의 근거다."""
        self.scope.activate()
        self.scope.close()
        wrong = {"lane_center_hz": {0: UPPER_RF_TUNE_HZ, 1: LOWER_RF_TUNE_HZ},
                 "analog_bandwidth_hz": 200000}
        self.worker.restore_tuning.return_value = wrong
        with self.assertRaises(CollisionRuntimeError) as ctx:
            self.scope.disable()
        message = str(ctx.exception)
        self.assertIn("RX restore unverified; ownership retained", message)
        self.assertIn("previous=", message)
        self.assertIn("restored=", message)
        self.assertEqual(self.scope.restored, wrong)
        self.assertIsNotNone(self.scope.restore_error)
        self.assertTrue(self.scope.touched)                     # 소유권 유지
        # 재시도로 올바른 값이 오면 검증이 통과하고 원인은 해소된다.
        self.worker.restore_tuning.return_value = self.previous
        reply = self.scope.disable()
        self.assertIs(reply["restore_verified"], True)
        self.assertIsNone(self.scope.restore_error)
        self.assertFalse(self.scope.touched)

    def test_non_dual_scope_reports_no_restore(self):
        """dual이 아니면 복원할 것이 없다 — verified는 None이지 True가 아니다."""
        scope = WorkloadRxScope(self.scope.seam, "owner", False)
        scope._replan = Mock()
        scope.activate()
        scope.close()
        reply = scope.disable()
        self.assertTrue(reply["disabled"])
        self.assertIsNone(reply["restore_readback"])
        self.assertIsNone(reply["restore_verified"])
        self.worker.apply_manual_tuning.assert_not_called()
        self.worker.restore_tuning.assert_not_called()
