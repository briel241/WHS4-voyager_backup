"""Deterministic real-boundary failure probes; no device I/O."""
import unittest
from unittest.mock import Mock, patch
from tests.unit import test_stage8_rx_scope_faults as fixture_module
from tests.unit import test_stage8_collision_lifecycle as control_module


class PrehardwareSafetyTests(unittest.TestCase):
    def test_nan_readback_is_not_accepted(self):
        fixture = fixture_module.RxScopeFaultTests()
        fixture.setUp()
        fixture.readback['lane_center_hz'][1] = float('nan')
        with self.assertRaises(Exception):
            fixture.scope.activate()
        self.assertFalse(fixture.scope.active)

    def test_restore_nan_keeps_cleanup_ownership(self):
        fixture = fixture_module.RxScopeFaultTests()
        fixture.setUp()
        fixture.scope.activate()
        fixture.scope.close()
        fixture.worker.restore_tuning.return_value = dict(
            analog_bandwidth_hz=float('nan'), lane_center_hz=fixture.previous['lane_center_hz'])
        with self.assertRaises(Exception):
            fixture.scope.disable()
        self.assertTrue(fixture.scope.touched)

    def test_lane_one_rf_lo_mismatch_rejected(self):
        fixture = fixture_module.RxScopeFaultTests()
        fixture.setUp()
        mid = fixture.readback['rx_lo_hz']
        fixture.readback['lane_rf_lo_hz'] = {0: mid, 1: mid+100}
        with self.assertRaises(Exception):
            fixture.scope.activate()
        self.assertFalse(fixture.scope.active)

    def test_failed_restore_revokes_advertised_dual(self):
        scope, clients = control_module.ScopeControlTests().build()
        scope.start()
        clients['MASS-B'].call.side_effect = RuntimeError('device unavailable')
        with patch('apps.workload_rx_scope.time.monotonic', side_effect=[0, 8]):
            with self.assertRaises(Exception):
                scope.close()
        self.assertFalse(scope.dual_ready)
        self.assertFalse(scope.evidence['cleanup_complete'])

    def test_start_cleanup_uncertainty_is_session_inhibit(self):
        from vdeter.poc.api.errors import PocError
        scope, clients = control_module.ScopeControlTests().build()
        clients['MASS-A'].call.side_effect = RuntimeError('reply lost')
        with patch('apps.workload_rx_scope.time.monotonic', side_effect=[0, 8]):
            with self.assertRaises(PocError) as caught:
                scope.start()
        self.assertEqual('SESSION_INHIBIT', caught.exception.severity.value)

    def test_all_poc_queries_have_bounded_send_and_response_budget(self):
        from apps.mass_roc_workload_controller import _AdapterChannel
        class Client:
            def __init__(self):
                self.budgets = []
            def send_bounded(self, message, *, timeout):
                self.message = message
                self.budgets.append(timeout)
            def recv(self, *, timeout):
                self.budgets.append(timeout)
                return {'command_id': self.message['command_id']}
        for command in ('poc_collision', 'poc_radio_state', 'poc_mac_slots',
                        'poc_clock_map', 'poc_schedule_group_tx', 'poc_tx_burst_state'):
            with self.subTest(command=command):
                client = Client()
                _AdapterChannel(client).request({'command_name': command, 'command_id': 'one'})
                self.assertTrue(all(0 < b <= 1 for b in client.budgets))

    def test_real_socket_send_applies_budget_before_first_write(self):
        from vdeter.b210_system.transport import SeqPacketClient
        client = object.__new__(SeqPacketClient)
        client._socket = Mock()
        client.send_bounded({'version': 1}, timeout=.25)
        self.assertEqual('settimeout', client._socket.mock_calls[0][0])
        client._socket.settimeout.assert_called_once_with(.25)
        client._socket.sendall.assert_called_once()
        client._socket.reset_mock()
        with self.assertRaises(TimeoutError):
            client.send_bounded({}, timeout=0)
        client._socket.sendall.assert_not_called()

    def test_generation_change_rejects_before_tuning(self):
        fixture = fixture_module.RxScopeFaultTests()
        fixture.setUp()
        fixture.radio._generation += 1
        with self.assertRaises(Exception):
            fixture.scope.activate()
        fixture.worker.apply_manual_tuning.assert_not_called()

    def test_cleanup_interrupt_during_start_is_not_hidden_by_ordinary_error(self):
        scope, clients = control_module.ScopeControlTests().build()
        clients['MASS-A'].call.side_effect = RuntimeError('activation failed')
        scope.close = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            scope.start()
        scope.close.assert_called_once()
