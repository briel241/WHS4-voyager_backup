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
)
from vdeter.stage8.testing.execution_fakes import ResultWriteError, write_result_fixture
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

    def test_prearmed_failure_does_not_claim_validated_or_armed(self):
        r = _result(lifecycle_transitions=("LEASE_ACQUIRED", "ERROR", "CLOSED"),
                    poc_infrastructure=PocInfrastructure.ERROR)
        text = "\n".join(render_rf_console(r))
        self.assertNotIn("VALIDATED", text)
        self.assertNotIn("ARMED", text)
        self.assertIn("infrastructure=ERROR", text)

    def test_rejected_attack_does_not_claim_scheduled(self):
        r = _result(attacks=(_attack(action_execution=ActionExecution.REJECTED_BEFORE_TX,
                                     target_effect=TargetEffect.NOT_APPLICABLE),))
        text = "\n".join(render_rf_console(r))
        self.assertNotIn("SCHEDULED", text)
        self.assertIn("REJECTED_BEFORE_TX", text)

    def test_missing_poc_identity_is_not_recorded(self):
        text = render_detailed(_result(poc_id=None, poc_version=None))
        self.assertIn("poc: not recorded", text)
        self.assertNotIn("None@None", text)

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
        write_result_fixture(result, path)

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

    def test_overwrite_of_confirmed_evidence_rejected(self):
        """확정된 evidence는 재호출로 조용히 덮어쓰이지 않는다(§12.3/§12.4, 계약: 확정 1회)."""
        import hashlib
        import os
        path = os.path.join(self.dir, "result.json")
        write_result_fixture(_result(run_id="run-A"), path)
        with open(path, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        before_mtime = os.stat(path).st_mtime_ns

        with self.assertRaises(ResultWriteError):
            write_result_fixture(_result(run_id="run-B"), path)   # 다른 내용
        with self.assertRaises(ResultWriteError):
            write_result_fixture(_result(run_id="run-A"), path)   # 동일 내용도 거부(계약 고정)

        with open(path, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after)                   # content·SHA 불변
        self.assertEqual(before_mtime, os.stat(path).st_mtime_ns)
        self.assertIn("run-A", render_results_file(path))  # 원본 유지
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".result-")], [])

    def test_symlink_destination_rejected(self):
        """목적지가 symlink면 거부하고 링크 대상을 건드리지 않는다."""
        import os
        outside = os.path.join(self.dir, "outside.json")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL")
        link = os.path.join(self.dir, "link.json")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlink 생성 권한 없음")
        with self.assertRaises(ResultWriteError):
            write_result_fixture(_result(), link)
        with open(outside, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ORIGINAL")  # 링크 대상 무손상

    def test_write_failure_leaves_no_partial_evidence(self):
        """확정 중 실패해도 대상 경로에 부분 JSON·임시 파일을 남기지 않는다(atomic)."""
        import os
        from unittest import mock
        path = os.path.join(self.dir, "fail.json")
        with mock.patch("vdeter.stage8.testing.execution_fakes.os.link",
                        side_effect=OSError("disk full")):
            with self.assertRaises(ResultWriteError):
                write_result_fixture(_result(), path)
        self.assertFalse(os.path.exists(path))  # 부분 파일 없음
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".result-")], [])

    def test_short_write_rejected(self):
        """os.write가 진행되지 않으면 short write로 거부하고 대상을 만들지 않는다."""
        import os
        from unittest import mock
        path = os.path.join(self.dir, "short.json")
        with mock.patch("vdeter.stage8.testing.execution_fakes.os.write", return_value=0):
            with self.assertRaises(ResultWriteError):
                write_result_fixture(_result(), path)
        self.assertFalse(os.path.exists(path))
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".result-")], [])

    def test_load_result_roundtrip(self):
        import os
        result = _result(attacks=(_attack(), _attack(request_id="r2", target="MASS-A")))
        path = os.path.join(self.dir, "r.json")
        write_result_fixture(result, path)
        restored = load_result(path)
        self.assertEqual(render_detailed(restored), render_detailed(result))
        self.assertEqual(restored.attacks_requested, 2)


if __name__ == "__main__":
    unittest.main()
