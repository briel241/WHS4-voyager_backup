"""Production D-10 dispatch is shared, not reimplemented in a test replica."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from vdeter.b210_system.short_data_compat import wrap_short_data_submission, PROMOTION_PRESENTATION_LIMIT


class D10SharedTest(unittest.TestCase):
    def test_existing_promotion_boundary_and_idempotent_install(self):
        original = Mock(return_value='short')
        coordinator = NS(_submit_data_session=Mock(return_value='session'))
        # Explicit eligibilities come from authoritative Link analysis, not payload guesses.
        cases = [(33, True, 'short'), (34, False, 'session'), (47, False, 'session'),
                 (415, False, 'session'), (641, False, 'session'), (642, False, 'short')]
        wrapped = wrap_short_data_submission(original)
        self.assertIs(wrapped, wrap_short_data_submission(wrapped))
        self.assertEqual(641, PROMOTION_PRESENTATION_LIMIT)
        for size, eligible, expected in cases:
            with self.subTest(size=size):
                request = NS(presentation_size=size, profiles=(NS(link_id=NS(value=11), short_data_eligible=eligible),))
                original.reset_mock()
                coordinator._submit_data_session.reset_mock()
                self.assertEqual(expected, wrapped(coordinator, 'state', request, 'event'))
                self.assertEqual(int(expected == 'short'), original.call_count)
                self.assertEqual(int(expected == 'session'), coordinator._submit_data_session.call_count)

    def test_frozen_fallback_and_operator_interrupt_unchanged(self):
        original = Mock(return_value='short')
        coordinator = NS(_submit_data_session=Mock(side_effect=RuntimeError('rejected')))
        req = NS(presentation_size=47, profiles=())
        wrapped = wrap_short_data_submission(original)
        self.assertEqual('short', wrapped(coordinator, None, req, None))
        coordinator._submit_data_session.side_effect = KeyboardInterrupt()
        original.reset_mock()
        with self.assertRaises(KeyboardInterrupt):
            wrapped(coordinator, None, req, None)
        original.assert_not_called()

    def test_launcher_and_both_harness_hosts_call_same_factory(self):
        repo = Path(__file__).resolve().parents[3]
        for path in (repo/'VDETER/apps/stage7_endpoint_launcher.py',
                     repo/'VDETER/tests/support/poc_device_harness.py',
                     repo/'apps/rf_off_software_session.py'):
            with self.subTest(path=str(path)):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                self.assertTrue(any(isinstance(n, ast.ImportFrom) and n.module == 'vdeter.b210_system.short_data_compat'
                                    for n in ast.walk(tree)))
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name) and n.func.id == 'wrap_short_data_submission']
                self.assertEqual(1, len(calls))
                self.assertFalse(any(isinstance(n, ast.FunctionDef) and n.name == '_submit_short_or_session'
                                     for n in ast.walk(tree)))
