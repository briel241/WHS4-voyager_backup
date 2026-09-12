"""Real proxy type must carry owner state, not rely on fake worker attributes."""
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace as NS
from threading import RLock
from vdeter.stage7.b210_worker import Stage7IsolatedDualLaneWorker, Stage7DualLaneFlowgraphBackend
from vdeter.stage7.testing.poc_device import Stage7PocDeviceSeam, PocDeviceUnavailable


class IsolatedWorkerStateTests(unittest.TestCase):
    def test_actual_proxy_routes_owner_state_in_one_rpc(self):
        proxy = object.__new__(Stage7IsolatedDualLaneWorker)
        owned = dict(hardware_time_ns=123, tx_lo_hz=10, rx_lo_hz=20, tx_muted=[False, True],
            rx_continuous=True, rx_continuous_since_ns=100, state_basis='DEVICE_OWNER_CONTROL_STATE')
        radio = NS(lease=NS(worker=proxy), bridge=None, _condition=RLock(), _rx_children={})
        seam = Stage7PocDeviceSeam(endpoint_id='ROC', endpoint=NS(dependencies=NS(radio=radio)),
                                   station_id_by_endpoint={})
        with patch.object(Stage7IsolatedDualLaneWorker, '_rpc', return_value=owned) as rpc:
            result = seam.radio_state()
        rpc.assert_called_once_with('poc_runtime_state')
        self.assertEqual([False, True], result['tx_muted'])
        self.assertEqual(20, result['rx_lo_hz'])
        self.assertEqual(123, result['hardware_time_ns'])
        self.assertTrue(result['rx_continuous'])

    def test_owner_state_does_not_read_tuning_or_change_hardware(self):
        owner = NS(_control_lock=RLock(), hardware_time_ns=Mock(return_value=500),
            _tx_lo_frequency_hz=10, _rx_lo_frequency_hz=20, _tx_muted=[True, False],
            _rx_continuous_since_ns=100)
        state = Stage7DualLaneFlowgraphBackend.poc_runtime_state(owner)
        self.assertEqual(500, state['hardware_time_ns'])
        self.assertEqual('LAST_ACCEPTED_COMMAND_NOT_TIMED_COMPLETION', state['tx_lo_basis'])
        state['tx_muted'][0] = False
        self.assertEqual([True, False], owner._tx_muted)
        owner.hardware_time_ns.assert_called_once()

    def test_malformed_owner_reply_is_not_free_radio(self):
        proxy = object.__new__(Stage7IsolatedDualLaneWorker)
        radio = NS(lease=NS(worker=proxy), bridge=None)
        seam = Stage7PocDeviceSeam(endpoint_id='ROC', endpoint=NS(dependencies=NS(radio=radio)),
                                   station_id_by_endpoint={})
        for reply in ({}, dict(hardware_time_ns=1, tx_muted=[0, 0])):
            with patch.object(Stage7IsolatedDualLaneWorker, '_rpc', return_value=reply), self.assertRaises(PocDeviceUnavailable):
                seam.radio_state()

    def test_tuned_comparison_uses_existing_one_hz_readback_contract(self):
        from apps.stage8_stage7_seams import Stage7ReceiverReadiness
        worker = NS(rx_rf_lo_hz=100, lane_rx_center_hz={0: 199, 1: 202})
        self.assertEqual((0,), Stage7ReceiverReadiness._tuned_lanes(worker, 200))
        worker.rx_rf_lo_hz, worker.rx_lo_hz = None, 199
        self.assertEqual((0, 1), Stage7ReceiverReadiness._tuned_lanes(worker, 200))
