"""Phase G — service-only phase의 additive 불변성·격리 계약 시험.

RF 접근 전에 기계로 증명해야 하는 것:
  ① `_run_rf_qualification` source/AST 불변
  ② 기존 RR phase dispatch 불변 (golden trace)
  ③ service phase가 `_run_rf_qualification`을 호출하지 않음
  ④ service phase가 CFO/gain 명령(start_cfo·calibrate_rf)을 발행하지 않음
  ⑤ service phase가 RR/RESEARCH_READY evidence를 만들지 않음
  ⑥ service phase가 `HOST_TIME_LOCKED` 경로(rf_time POST)를 거침
  ⑦ 결속 불일치 시 장치 접근 전 거부
  ⑧ 실패 시 cleanup 수행
"""

import ast
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

import apps.stage7_coordinator_main as coordinator_main

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
COORDINATOR_REL = "VDETER/apps/stage7_coordinator_main.py"

#: Phase F qualification 시점 commit (reference의 qualified_source_commit).
QUALIFIED_COMMIT = "c063d0db16f1074fb3c02f185c11342712a2031f"

#: qualification 경로가 불변이어야 하는 함수들.
QUALIFIED_FUNCTIONS = (
    "_run_rf_qualification",
    "run_rr_p0", "run_rr_p1", "run_rr_p2", "run_rr_p3_soak", "run_rr_p3_fault",
    "run_p0_prepare_configure_arm", "_run_rr_service_phase",
    "_spawn_controller", "_spawn_mass_roc_controller", "run_mass_roc",
    "spawn_and_connect", "load_group",
)


def _function_hashes(source: str) -> dict:
    """최상위 함수별 (AST dump, unparse) 해시. 중첩 함수는 부모에 포함된다."""
    tree = ast.parse(source)
    result = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            result[node.name] = (
                hashlib.sha256(ast.dump(node).encode("utf-8")).hexdigest(),
                hashlib.sha256(ast.unparse(node).encode("utf-8")).hexdigest(),
            )
    return result


def _git_show(path_at_commit: str) -> str:
    done = subprocess.run(["git", "-C", REPO, "show", path_at_commit],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    if done.returncode != 0:
        raise unittest.SkipTest(f"git show 실패: {path_at_commit}")
    return done.stdout


class TC_QUALIFIED_PATH_INVARIANT(unittest.TestCase):
    """① ② qualification 경로 source/AST 불변."""

    @classmethod
    def setUpClass(cls):
        cls.old = _function_hashes(
            _git_show(f"{QUALIFIED_COMMIT}:{COORDINATOR_REL}"))
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as handle:
            cls.new = _function_hashes(handle.read())

    def test_run_rf_qualification_ast_and_source_invariant(self):
        self.assertIn("_run_rf_qualification", self.old)
        self.assertEqual(self.old["_run_rf_qualification"],
                         self.new["_run_rf_qualification"],
                         "_run_rf_qualification이 변경됐다 — Phase F 재qualification 필요")

    def test_all_qualified_functions_invariant(self):
        changed = [name for name in QUALIFIED_FUNCTIONS
                   if self.old.get(name) != self.new.get(name)]
        self.assertEqual(changed, [], f"qualification 경로 함수 변경: {changed}")

    def test_no_qualified_function_removed(self):
        removed = sorted(set(self.old) - set(self.new))
        self.assertEqual(removed, [], f"함수 삭제: {removed}")

    def test_change_is_additive_only(self):
        """변경 함수는 dispatch 계층 둘뿐이고, 나머지는 신규 추가여야 한다."""
        changed = sorted(n for n in self.old if n in self.new
                         and self.old[n] != self.new[n])
        self.assertEqual(changed, ["main", "validate_rr_binding"], str(changed))
        added = sorted(set(self.new) - set(self.old))
        self.assertEqual(added, ["run_mass_roc_service"], str(added))


class _FakeArgs:
    def __init__(self, **kwargs):
        self.phase = kwargs.get("phase")
        self.run_id = kwargs.get("run_id", "svc-test")
        self.baseline_sha256 = kwargs.get("baseline_sha256", "b" * 64)
        self.workload = kwargs.get("workload", "rr")
        self.scenario_id = kwargs.get("scenario_id", "SC-VDE-005")
        self.certificate_case = kwargs.get("certificate_case", "normal")


def _rr_metadata(**overrides):
    metadata = {
        "qualification_profile": "RESEARCH_READY_MINIMAL",
        "rr_phase": "rr-p2-17",
        "run_id": "svc-test",
        "baseline_sha256": "b" * 64,
        "implicit_retry": 0,
        "data_link_id": 17,
        "phase_deadlines_s": {"rr-p0": 900, "rr-p1": 3600, "rr-p2-17": 4200,
                              "rr-p2-19": 4200, "rr-p3-fault": 2400},
    }
    metadata.update(overrides)
    return metadata


class TC_BINDING_GATE(unittest.TestCase):
    """⑦ 결속 불일치는 장치 접근 전에 거부된다."""

    def setUp(self):
        self._saved = {}
        for key in ("STAGE7_SVCDBG_ALLOW_RX_READY", "STAGE7_SCENARIO_FILTER"):
            self._saved[key] = os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value

    def test_service_phase_accepts_bound_config(self):
        coordinator_main.validate_rr_binding(
            _FakeArgs(phase="mass-roc-service"), _rr_metadata())

    def test_service_phase_rejects_run_id_mismatch(self):
        with self.assertRaises(SystemExit) as caught:
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="mass-roc-service"),
                _rr_metadata(run_id="other-run"))
        self.assertIn("SERVICE_BINDING_REJECTED", str(caught.exception))

    def test_service_phase_rejects_baseline_mismatch(self):
        with self.assertRaises(SystemExit):
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="mass-roc-service"),
                _rr_metadata(baseline_sha256="c" * 64))

    def test_service_phase_rejects_debug_switch(self):
        os.environ["STAGE7_SVCDBG_ALLOW_RX_READY"] = "1"
        with self.assertRaises(SystemExit) as caught:
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="mass-roc-service"), _rr_metadata())
        self.assertIn("STAGE7_SVCDBG_ALLOW_RX_READY", str(caught.exception))

    def test_service_phase_rejects_non_research_ready_profile(self):
        with self.assertRaises(SystemExit):
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="mass-roc-service"),
                _rr_metadata(qualification_profile=None))

    def test_existing_rr_binding_unchanged(self):
        """기존 RR phase 결속 판정은 그대로다 (additive 변경 확인)."""
        coordinator_main.validate_rr_binding(
            _FakeArgs(phase="rr-p2-17"), _rr_metadata())
        with self.assertRaises(SystemExit) as caught:
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="rr-p2-17"), _rr_metadata(run_id="other"))
        self.assertIn("RR_BINDING_REJECTED", str(caught.exception))
        # 기존 full-qualification phase는 RR profile config를 계속 거부한다.
        with self.assertRaises(SystemExit):
            coordinator_main.validate_rr_binding(
                _FakeArgs(phase="p2"), _rr_metadata())


class TC_SERVICE_PHASE_ISOLATION(unittest.TestCase):
    """③ ④ ⑤ ⑥ service phase가 qualification을 실행하지 않는다 (source 수준)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as handle:
            cls.tree = ast.parse(handle.read())
        cls.service = next(
            node for node in cls.tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_mass_roc_service")
        cls.source = ast.unparse(cls.service)
        # docstring은 "무엇을 하지 않는지"를 설명하므로 금지어 검사에서 제외한다.
        body = list(cls.service.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        cls.code_source = "\n".join(ast.unparse(node) for node in body)

    def test_does_not_call_run_rf_qualification(self):
        called = {node.func.id for node in ast.walk(self.service)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("_run_rf_qualification", called)
        # docstring 제외 본문에도 등장하지 않는다.
        self.assertNotIn("_run_rf_qualification", self.code_source)

    def test_issues_no_cfo_or_gain_barriers(self):
        for forbidden in ('"start_cfo"', "'start_cfo'",
                          '"calibrate_rf"', "'calibrate_rf'"):
            # 판정용 참조(발행 금지 확인)는 허용하되 barrier 호출은 없어야 한다.
            pass
        barriers = [node.args[0].value for node in ast.walk(self.service)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_barrier"
                    and node.args and isinstance(node.args[0], ast.Constant)]
        self.assertNotIn("start_cfo", barriers, str(barriers))
        self.assertNotIn("calibrate_rf", barriers, str(barriers))
        self.assertIn("acquire_host_time", barriers)
        self.assertIn("validate_rf_time", barriers)
        self.assertIn("stop", barriers)

    def test_reaches_host_time_locked_path(self):
        """POST_CORRECTION 검증을 거쳐야 admission이 가능하다."""
        self.assertIn("POST_CORRECTION", self.code_source)
        self.assertIn("open_service_admission", self.code_source)
        # 빈 summary로 인한 공허참 통과를 명시 거부한다.
        self.assertIn("POST_CORRECTION summary가 비어", self.code_source)

    def test_does_not_write_research_ready_evidence(self):
        self.assertNotIn("write_rr_evidence", self.code_source)
        self.assertNotIn("research_ready", self.code_source)
        self.assertIn("operational_service", self.code_source)
        self.assertIn("STAGE7_OPERATIONAL_SERVICE_RESULT_V1", self.code_source)

    def test_does_not_run_raw_phy(self):
        self.assertNotIn("raw_phy", self.code_source)

    def test_cleanup_on_path(self):
        """실패 경로에서도 controller 종료와 evidence 기록이 finally에 있다."""
        finallys = [node for node in ast.walk(self.service)
                    if isinstance(node, ast.Try) and node.finalbody]
        self.assertTrue(finallys)
        body = "\n".join(ast.unparse(node) for handler in finallys
                         for node in handler.finalbody)
        self.assertIn("controller.terminate()", body)
        self.assertIn("service_result.json", body)


class TC_PHASE_DISPATCH(unittest.TestCase):
    """② 기존 phase dispatch 불변 + 신규 phase 인자 게이트."""

    def test_phase_choices_are_superset(self):
        old_source = _git_show(f"{QUALIFIED_COMMIT}:{COORDINATOR_REL}")
        old_choices = set()
        for node in ast.walk(ast.parse(old_source)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                for keyword in node.keywords:
                    if keyword.arg == "choices" and any(
                        isinstance(arg, ast.Constant) and arg.value == "--phase"
                        for arg in node.args
                    ):
                        old_choices = {e.value for e in keyword.value.elts}
        parser = coordinator_main.__dict__  # 파서는 main 내부 — source로 확인
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as handle:
            new_source = handle.read()
        self.assertTrue(old_choices, "기존 phase 목록을 찾지 못했다")
        for choice in old_choices:
            self.assertIn(f'"{choice}"', new_source, f"{choice} 소실")
        self.assertIn('"mass-roc-service"', new_source)
        self.assertIsNotNone(parser)

    def test_service_phase_rejects_shared_all_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                coordinator_main.main([
                    "--run-id", "svc-x", "--phase", "mass-roc-service",
                    "--run-root", tmp, "--scenario-id", "all",
                    "--gain-artifact", os.path.join(tmp, "g.json"),
                ])
            self.assertIn("all", str(caught.exception))

    def test_service_phase_requires_gain_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                coordinator_main.main([
                    "--run-id", "svc-x", "--phase", "mass-roc-service",
                    "--run-root", tmp, "--scenario-id", "SC-VDE-005",
                ])
            self.assertIn("gain-artifact", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
