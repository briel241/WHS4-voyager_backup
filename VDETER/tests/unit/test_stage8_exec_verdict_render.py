"""verdict 독립성과 bounded renderer·results read-only 시험(실행 명세 §11, §12).

`S8-POC-REQ-15/16/17/18` 관련. platform 무관(순수 model·문자열).
"""

from __future__ import annotations

import copy
import unittest

from vdeter.stage8.execution.render import (
    NOT_RECORDED,
    load_result,
    render_detailed,
    render_results,
    render_results_file,
    render_rf_console,
    save_result,
)
from vdeter.stage8.execution.verdict import (
    ActionExecution,
    AttackRecord,
    PocInfrastructure,
    ScenarioVerdict,
    TargetEffect,
    WorkloadResult,
)


def _attack(**kw):
    base = dict(
        request_id="r1", action_kind="REPLAY_PACKET", target="ROC",
        trigger="PACKET_OBSERVED", source_observation_id="obs-1", schedule={"after_slots": 2},
        action_execution=ActionExecution.TX_COMPLETED, target_effect=TargetEffect.NO_EFFECT,
        physical_channel="lower", broadcast_parent_id="p1", transfer_id="t1", reason=None,
    )
    base.update(kw)
    return AttackRecord(**base)


def _result(**kw):
    base = dict(
        run_id="run1", workload_id="w-1", scenario_id="SC-VDE-006",
        poc_id="replay-attack", poc_version="1.0.0", attacker_endpoint="MASS-B",
        script_sha256="ab" * 32, manifest_sha256="cd" * 32, binding_digest="ef" * 32,
        interpreter="/usr/bin/python3", scenario_verdict=ScenarioVerdict.PASS,
        poc_infrastructure=PocInfrastructure.COMPLETED, attacks=(),
        infrastructure_error_code=None, lifecycle_transitions=("ARMED", "CLOSED"),
        evidence={},
    )
    base.update(kw)
    return WorkloadResult(**base)


class VerdictIndependenceTest(unittest.TestCase):
    def test_three_axes_independent(self):
        # 공격이 REJECTED_BY_TARGET(bad-signature)여도 scenario는 PASS로 남는다(§11.1).
        r = _result(
            scenario_verdict=ScenarioVerdict.PASS,
            poc_infrastructure=PocInfrastructure.COMPLETED,
            attacks=(_attack(action_kind="FORGE_PACKET",
                             action_execution=ActionExecution.TX_COMPLETED,
                             target_effect=TargetEffect.REJECTED_BY_TARGET,
                             reason="bad-signature"),),
        )
        self.assertEqual(r.scenario_verdict, ScenarioVerdict.PASS)
        self.assertEqual(r.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(r.attacks[0].target_effect, TargetEffect.REJECTED_BY_TARGET)

    def test_counts(self):
        r = _result(attacks=(
            _attack(action_execution=ActionExecution.TX_COMPLETED, target_effect=TargetEffect.NO_EFFECT),
            _attack(action_execution=ActionExecution.TX_COMPLETED, target_effect=TargetEffect.EFFECT_OBSERVED),
            _attack(action_execution=ActionExecution.REJECTED_BEFORE_TX, target_effect=TargetEffect.NOT_APPLICABLE),
        ))
        self.assertEqual(r.attacks_requested, 3)
        self.assertEqual(r.attacks_completed, 2)
        self.assertEqual(r.effects_observed, 1)

    def test_action_execution_vs_target_effect_distinct(self):
        # RF 전 거부와 target 거부를 같은 REJECTED로 섞지 않는다(§11.1).
        before = _attack(action_execution=ActionExecution.REJECTED_BEFORE_TX,
                         target_effect=TargetEffect.NOT_APPLICABLE, reason="POC_RADIO_RESOURCE_CONFLICT")
        by_target = _attack(action_execution=ActionExecution.TX_COMPLETED,
                            target_effect=TargetEffect.REJECTED_BY_TARGET, reason="bad-signature")
        self.assertNotEqual(before.action_execution, by_target.action_execution)


class RenderTest(unittest.TestCase):
    def test_detailed_report_shape(self):
        r = _result(attacks=(_attack(),))
        text = render_detailed(r)
        self.assertIn("[POC RUN]", text)
        self.assertIn("[ATTACK 0001]", text)
        self.assertIn("[FINAL]", text)
        self.assertIn("scenario_verdict: PASS", text)
        self.assertIn("tx_result: TX_COMPLETED", text)

    def test_not_recorded_for_missing(self):
        r = _result(interpreter=None, attacks=(_attack(source_observation_id=None, schedule={}),))
        text = render_detailed(r)
        self.assertIn(NOT_RECORDED, text)

    def test_rf_console_low_load_terminal_only(self):
        r = _result(attacks=(_attack(),))
        lines = render_rf_console(r)
        # 상태 전이 + action terminal만. packet별 block 없음.
        self.assertTrue(any(l.startswith("POC ARMED") for l in lines))
        self.assertTrue(any("TERMINAL" in l for l in lines))

    def test_bounded_lines_scale_with_attacks(self):
        r0 = _result(attacks=())
        r5 = _result(attacks=tuple(_attack(request_id=f"r{i}") for i in range(5)))
        self.assertLess(len(render_detailed(r0)), len(render_detailed(r5)))


class ResultsReadOnlyTest(unittest.TestCase):
    def test_roundtrip_identity(self):
        r = _result(attacks=(_attack(), _attack(request_id="r2", target="MASS-A",
                                                 physical_channel="upper")))
        serialized = r.as_json_value()
        restored = WorkloadResult.from_json_value(serialized)
        self.assertEqual(render_detailed(r), render_detailed(restored))

    def test_render_results_matches_and_does_not_mutate(self):
        r = _result(attacks=(_attack(),))
        serialized = r.as_json_value()
        snapshot = copy.deepcopy(serialized)
        out = render_results(serialized)
        self.assertEqual(out, render_detailed(r))
        # read-only: 입력 dict 불변(§12.3).
        self.assertEqual(serialized, snapshot)


class ResultsFileReadOnlyTest(unittest.TestCase):
    """`vdeter results` 파일 경계: 재출력이 evidence 파일을 바꾸지 않는다(§12.3, REQ-18)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="results-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_render_results_file_does_not_modify_evidence(self):
        import hashlib
        import os
        import time
        result = _result(attacks=(_attack(),))
        path = os.path.join(self.dir, "result.json")
        save_result(result, path)

        def facts():
            st = os.stat(path)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            return digest, st.st_size, st.st_mtime_ns

        before = facts()
        time.sleep(0.01)
        text = render_results_file(path)
        text_again = render_results_file(path)
        after = facts()
        # run 시점과 같은 model·renderer로 동일 출력.
        self.assertEqual(text, render_detailed(result))
        self.assertEqual(text, text_again)
        # content·SHA-256·size·mtime 불변.
        self.assertEqual(before, after)

    def test_load_result_roundtrip(self):
        import os
        result = _result(attacks=(_attack(), _attack(request_id="r2", target="MASS-A")))
        path = os.path.join(self.dir, "r.json")
        save_result(result, path)
        restored = load_result(path)
        self.assertEqual(render_detailed(restored), render_detailed(result))
        self.assertEqual(restored.attacks_requested, 2)


if __name__ == "__main__":
    unittest.main()
