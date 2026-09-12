import unittest
from types import SimpleNamespace as NS
from threading import RLock
from unittest.mock import Mock
from vdeter.stage7.rx_scope import WorkloadRxScope
from vdeter.stage7.collision_runtime import CollisionRuntimeError
from vdeter.stage7.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME, LOWER_RF_TUNE_HZ, UPPER_RF_TUNE_HZ


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
