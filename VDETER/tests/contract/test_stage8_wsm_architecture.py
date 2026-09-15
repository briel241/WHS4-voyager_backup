"""Stage 5 shared-medium: architecture 계약시험 (production/test 분리·fail-closed).

finding D: production 경로가 test double을 import하거나 RF-off fallback을 조용히 쓰지 못하게
구조적으로 강제한다. RF-off 기본값(stub scheduler·참조 modem)은 명시 opt-in으로만 접근한다.
"""

from __future__ import annotations

import ast
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SM = os.path.normpath(os.path.join(_HERE, "..", "..", "src", "vdeter", "poc", "shared_medium"))


def _imports(path):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            names.append(mod)
            names += [f"{mod}.{a.name}" for a in node.names]
    return names


class ProductionDoesNotImportTestingTest(unittest.TestCase):
    def test_no_production_module_imports_testing(self):
        offenders = []
        for name in os.listdir(_SM):
            if not name.endswith(".py") or name == "testing.py":
                continue
            for imp in _imports(os.path.join(_SM, name)):
                if "testing" in imp.split(".")[-1] or imp.endswith(".testing") or ".testing" in imp:
                    offenders.append((name, imp))
        self.assertEqual(offenders, [], f"production 모듈이 test double을 import함: {offenders}")


class ConstructorFailClosedArchTest(unittest.TestCase):
    """기본 생성자는 RF-off 기본값을 명시 허용하지 않으면 구조적으로 생성 실패한다."""

    def test_plain_constructor_without_optin_fails(self):
        from vdeter.poc.shared_medium import testing as TD
        from vdeter.poc.shared_medium.provider import SharedMediumActionProvider

        with self.assertRaises(ValueError):
            SharedMediumActionProvider(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            )

    def test_for_production_requires_both_dependencies(self):
        from vdeter.poc.shared_medium import testing as TD
        from vdeter.poc.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider

        with self.assertRaises(ValueError):
            SharedMediumActionProvider.for_production(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                scheduler=None, waveform_factory=TD.CountingWaveformFactory(),
            )
        with self.assertRaises(ValueError):
            SharedMediumActionProvider.for_production(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                scheduler=NextSafeSlotScheduler(), waveform_factory=None,
            )


class SlotSchedulerContractTest(unittest.TestCase):
    """trigger 등록·정리가 SlotScheduler 계약에 포함된다(finding A 계약 gap)."""

    def test_scheduler_contract_has_trigger_registration(self):
        from vdeter.poc.shared_medium.provider import NextSafeSlotScheduler

        sched = NextSafeSlotScheduler()
        self.assertTrue(hasattr(sched, "register_trigger"))
        self.assertTrue(hasattr(sched, "clear_triggers"))
        self.assertTrue(hasattr(sched, "trigger_count"))


if __name__ == "__main__":
    unittest.main()
