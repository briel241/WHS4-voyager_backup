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

import apps.coordinator_main as coordinator_main

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
COORDINATOR_REL = "VDETER/apps/coordinator_main.py"

#: qualification 당시(419bfef) 경로 — **과거 기록이라 바뀌지 않는다**.
#: git show 는 당시 경로로, 현재 파일 읽기는 위 현재 경로로 한다(둘을 구분한다).
COORDINATOR_REL_AT_QUALIFIED_COMMIT = "VDETER/apps/stage7_coordinator_main.py"

#: 2-3 구조 정리에서 **승인된 이름·경로 치환**만. 과거 원문을 현재 이름에 맞출 때만 쓰며,
#: 이 표에 없는 차이는 그대로 불일치로 검출된다(모든 import/문자열 차이를 무시하는
#: 광범위 정규화가 아니다).
#:
#: 근거: 직전 커밋(93bebbd) 원문에 이 표만 적용한 결과가 현재 파일과 byte-identical 임을
#: 확인했다 — output/stage8-refactor-optimize/step2-3-impl-20260915/rename-only-proof.json.
APPROVED_RENAMES = (
    ("vdeter.stage7", "vdeter.b210_system"),
    ("apps.stage7_trace", "apps.service_trace"),
    ("stage7_trace.py", "service_trace.py"),
    ("Stage7ServiceSession", "ServiceSession"),
    ("stage7_endpoint_launcher.py", "endpoint_launcher.py"),
    ("stage7_scenario_controller.py", "scenario_controller.py"),
    ("stage7_mass_roc_controller.py", "mass_roc_workload_controller.py"),
    ("Stage7ConfigurationLoader", "ConfigurationLoader"),
    ("Stage7OperationalGainInput", "OperationalGainInput"),
    ("Stage7SystemPlanLoader", "SystemPlanLoader"),
    ("Stage7RuntimeCoordinator", "RuntimeCoordinator"),
    ("Stage7BarrierFailure", "BarrierFailure"),
)

#: 경로 조각 치환(식별자 경계가 아니라 문자열 그대로). SB-6 의 config 디렉터리 이동.
APPROVED_PATH_FRAGMENTS = (
    ("configs/stage7", "configs/b210_system"),
    ('"configs", "stage7"', '"configs", "b210_system"'),
)


def _apply_approved_renames(source: str) -> str:
    """과거 원문에 승인 대응표만 적용한다(ASCII 식별자 경계 기준)."""
    import re as _re
    for _old, _new in APPROVED_RENAMES:
        source = _re.sub(r"(?<![A-Za-z0-9_])" + _re.escape(_old) + r"(?![A-Za-z0-9_])",
                         _new, source)
    for _old, _new in APPROVED_PATH_FRAGMENTS:
        source = source.replace(_old, _new)
    return source

#: Phase F qualification 시점 commit (reference의 qualified_source_commit).
QUALIFIED_COMMIT = "419bfef529d4907f77bca54c6b3e4d484afb260f"

#: qualification 경로가 불변이어야 하는 함수들.
QUALIFIED_FUNCTIONS = (
    "_run_rf_qualification",
    "run_rr_p0", "run_rr_p1", "run_rr_p2", "run_rr_p3_soak", "run_rr_p3_fault",
    "run_p0_prepare_configure_arm", "_run_rr_service_phase",
    "_spawn_controller", "_spawn_mass_roc_controller", "run_mass_roc",
    "spawn_and_connect", "load_group", "write_rr_evidence", "_write_plan",
    "_live_b210_speed_map", "_worker_process_count", "_rediscover_b210",
)


#: Phase I 승인 변경의 고정 fixture (2026-09-04, 사용자 조건부 승인).
#: 계약 A: 기존 Phase F/G qualification 함수는 qualified commit과 완전 일치.
#: 계약 B: 승인된 `run_rr_p3_fault`(물리 USB detach 소항목 제외)는 완료본의
#: AST/source SHA-256으로 고정 — 이후 변경은 다시 실패로 검출된다. 이름
#: allow-list가 아니라 해시 고정이다. root tests/와 단일 출처로 공유한다.
PHASE_I_BINDINGS_PATH = os.path.join(
    REPO, "tests", "fixtures", "stage7_phase_i_function_bindings.json")


def _phase_i_bindings() -> dict:
    with open(PHASE_I_BINDINGS_PATH, encoding="utf-8") as handle:
        bindings = json.load(handle)
    if (bindings.get("artifact_type") != "STAGE7_PHASE_I_APPROVED_FUNCTION_HASHES_V2"
            or bindings.get("source_sha256_binding", {}).get("method_id")
            != "literal-source-lf-v1"):
        raise ValueError("unsupported Phase-I source hash contract")
    return bindings


def _function_source(lines: list, node: ast.FunctionDef) -> str:
    """함수의 **문자 그대로의 원문**(decorator 포함, LF 정규화).

    이전에는 `ast.unparse` 출력으로 source 해시를 만들었으나, unparse는 **CPython
    patch 버전에 따라 출력이 달라진다**(PEP 701 f-string quote reuse). 같은 파일을
    3.12.13에서 파생한 해시가 3.12.3에서 불일치로 나타나, 함수가 전혀 바뀌지
    않았는데도 결속 시험이 실패했다(qual-20260913 실측).

    파일 원문은 인터프리터와 무관하므로 버전 안정적이고, 주석·공백까지 포함해
    unparse보다 **엄격하다**. 함수 본문이 변조되면 당연히 달라진다.
    """
    start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    return "\n".join(lines[start:node.end_lineno]) + "\n"


def _function_hashes(source: str) -> dict:
    """최상위 함수별 (AST dump, 원문) 해시. 중첩 함수는 부모에 포함된다."""
    lines = source.replace("\r\n", "\n").split("\n")
    tree = ast.parse(source)
    result = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            result[node.name] = (
                hashlib.sha256(ast.dump(node).encode("utf-8")).hexdigest(),
                hashlib.sha256(
                    _function_source(lines, node).encode("utf-8")).hexdigest(),
            )
    return result


def _qualification_drift(old: dict, new: dict) -> dict:
    """계약 A·B 판정 (시험 본체가 변조 시나리오에도 재사용).

    반환: {"unexpected_changed": [...], "pinned_mismatch": [...],
           "unexpected_added": [...], "removed": [...]} — 전부 비어야 통과."""
    bindings = _phase_i_bindings()
    approved = {name: (spec["ast_sha256"], spec["source_sha256"])
                for name, spec in bindings["functions"].items()}
    unexpected_changed = [
        name for name in QUALIFIED_FUNCTIONS
        if name not in approved and old.get(name) != new.get(name)]
    pinned_mismatch = [
        name for name, pinned in approved.items() if new.get(name) != pinned]
    expected_added = set(bindings["added_functions"]) | {
        "_service_session_terminal_status", "_terminate_process_bounded"}
    unexpected_added = sorted(set(new) - set(old) - expected_added)
    removed = sorted(set(old) - set(new))
    return {"unexpected_changed": unexpected_changed,
            "pinned_mismatch": pinned_mismatch,
            "unexpected_added": unexpected_added, "removed": removed}


def _mutate_function(source: str, name: str) -> str:
    """지정 함수 본문 첫 문장 앞에 무해한 문장을 삽입해 AST/source를 바꾼다."""
    tree = ast.parse(source)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = source.splitlines(keepends=True)
    first = node.body[0].lineno - 1
    indent = len(lines[first]) - len(lines[first].lstrip())
    lines.insert(first, " " * indent + "_phase_i_mutation_probe = 1\n")
    return "".join(lines)


def _git_show(path_at_commit: str) -> str:
    done = subprocess.run(["git", "-C", REPO, "show", path_at_commit],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    if done.returncode != 0:
        stderr = (done.stderr or "")
        # 환경 문제(git 부재·저장소 접근 불가)만 skip 한다. 경로가 그 commit 에 없는
        # 것은 결속이 깨졌다는 뜻이므로 조용히 넘기지 않는다.
        if "not a git repository" in stderr or "cannot be used without a working tree" in stderr:
            raise unittest.SkipTest(f"git 저장소 접근 불가: {stderr.strip()[:120]}")
        raise AssertionError(
            f"qualification 당시 경로를 읽지 못했다: {path_at_commit} — {stderr.strip()[:200]}")
    return done.stdout


class TC_QUALIFIED_PATH_INVARIANT(unittest.TestCase):
    """① ② qualification 경로 source/AST 불변."""

    @classmethod
    def setUpClass(cls):
        cls.old = _function_hashes(
            _apply_approved_renames(
            _git_show(f"{QUALIFIED_COMMIT}:"
                      f"{COORDINATOR_REL_AT_QUALIFIED_COMMIT}")))
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as handle:
            cls.new = _function_hashes(handle.read())

    def test_run_rf_qualification_ast_and_source_invariant(self):
        self.assertIn("_run_rf_qualification", self.old)
        self.assertEqual(self.old["_run_rf_qualification"],
                         self.new["_run_rf_qualification"],
                         "_run_rf_qualification이 변경됐다 — Phase F 재qualification 필요")

    def test_all_qualified_functions_invariant(self):
        """계약 A: Phase I 승인 함수(run_rr_p3_fault)를 제외한 qualification
        함수 전부가 qualified commit과 완전 일치. 계약 B: 승인 함수는 고정
        해시와 정확히 일치."""
        drift = _qualification_drift(self.old, self.new)
        self.assertEqual(drift["unexpected_changed"], [],
                         f"qualification 경로 함수 변경: {drift}")
        self.assertEqual(drift["pinned_mismatch"], [],
                         f"Phase I 승인 함수가 고정 해시와 다름: {drift}")

    def test_phase_i_function_pinned_by_hash_not_by_name(self):
        """계약 B: run_rr_p3_fault는 승인 완료본의 AST/source SHA-256과 정확히
        일치해야 하며(이름 allow-list 아님), qualified commit 판과는 달라야
        한다(실제로 변경된 완료본임을 증명)."""
        pinned = _phase_i_bindings()["functions"]["run_rr_p3_fault"]
        self.assertEqual(self.new["run_rr_p3_fault"],
                         (pinned["ast_sha256"], pinned["source_sha256"]))
        self.assertNotEqual(self.old["run_rr_p3_fault"],
                            self.new["run_rr_p3_fault"])

    def test_no_qualified_function_removed(self):
        removed = sorted(set(self.old) - set(self.new))
        self.assertEqual(removed, [], f"함수 삭제: {removed}")

    def test_change_is_additive_only(self):
        """변경 함수 집합은 정확히 {run_mass_roc_session(Phase G),
        run_rr_p0(Pi powered-USB3-hub profile, 2026-09-10),
        run_rr_p3_fault(Phase I)} + 3-1 진단 게이트로 재고정한
        {_open_session_trace, _spawn_mass_roc_controller} + 3-3 종료 대기
        구조 변경으로 재고정한 {main, run_mass_roc_service}; 추가 함수는
        Phase G helper 2개 + Phase I `_p3_fault_status` + powered-hub
        helper 2개 + 3-1 `_diagnostics_enabled` + 3-3 `_await_launcher_exits`
        로 한정.

        기대 집합은 fixture 에서 합성하되 **정확 일치**로 판정한다 — 승인
        기록에 없는 함수가 하나라도 바뀌면 실패다."""
        bindings = _phase_i_bindings()
        approved_changed = set(
            bindings["expected_changed_vs_qualified_commit"])
        for entry in bindings.get("rebinding_history", ()):
            approved_changed |= set(entry.get("functions_repinned", ()))
        changed = sorted(n for n in self.old if n in self.new
                         and self.old[n] != self.new[n])
        self.assertEqual(changed, sorted(approved_changed), str(changed))
        self.assertEqual(changed, ["_open_session_trace",
                                   "_spawn_mass_roc_controller",
                                   "main", "run_mass_roc_service",
                                   "run_mass_roc_session", "run_rr_p0",
                                   "run_rr_p3_fault"])
        added = sorted(set(self.new) - set(self.old))
        self.assertEqual(added, ["_await_launcher_exits",
                                 "_diagnostics_enabled",
                                 "_expected_usb_speed",
                                 "_p3_fault_status",
                                 "_service_session_terminal_status",
                                 "_shared_hub_topology_check",
                                 "_terminate_process_bounded"],
                         str(added))
        self.assertEqual(_qualification_drift(self.old, self.new)[
            "unexpected_added"], [])

    def test_powered_hub_rr_p0_pinned_by_hash_and_changed_vs_qualified(self):
        """2026-09-10 계약 B(추가): run_rr_p0는 Pi powered-USB3-hub profile
        완료본의 AST/source SHA-256과 정확히 일치해야 하며(이름 allow-list
        아님), qualified commit 판과는 달라야 한다."""
        pinned = _phase_i_bindings()["functions"]["run_rr_p0"]
        self.assertEqual(self.new["run_rr_p0"],
                         (pinned["ast_sha256"], pinned["source_sha256"]))
        self.assertNotEqual(self.old["run_rr_p0"], self.new["run_rr_p0"])
        # 기존 Pi 고정표는 그대로다 (legacy profile 유도값과 동일).
        self.assertIn('_EXPECTED_USB_SPEED = {"MASS-A": "5000", "MASS-B": "480", '
                      '"ROC": "5000"}', open(os.path.join(REPO, COORDINATOR_REL),
                                             encoding="utf-8").read())

    def test_guard_detects_mutation_of_other_qualified_function(self):
        """다른 qualification 함수 하나(run_rr_p1)를 임의 변경하면 가드 실패."""
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as h:
            source = h.read()
        mutated = _function_hashes(_mutate_function(source, "run_rr_p1"))
        drift = _qualification_drift(self.old, mutated)
        self.assertEqual(drift["unexpected_changed"], ["run_rr_p1"])

    def test_guard_detects_mutation_of_pinned_phase_i_function(self):
        """새 run_rr_p3_fault의 해시가 바뀌면(추가 변경) 가드 실패."""
        with open(os.path.join(REPO, COORDINATOR_REL), encoding="utf-8") as h:
            source = h.read()
        mutated = _function_hashes(_mutate_function(source, "run_rr_p3_fault"))
        drift = _qualification_drift(self.old, mutated)
        self.assertEqual(drift["pinned_mismatch"], ["run_rr_p3_fault"])
        self.assertEqual(drift["unexpected_changed"], [])


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
        old_source = _apply_approved_renames(
            _git_show(f"{QUALIFIED_COMMIT}:"
                      f"{COORDINATOR_REL_AT_QUALIFIED_COMMIT}"))
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
