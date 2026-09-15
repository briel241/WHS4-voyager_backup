"""Stage 8 public/private import 경계와 기능 OFF 구조적 0 시험
(명세 §3.2, §19; REQ-01). 대부분 플랫폼 무관.
"""

from __future__ import annotations

import ast
import copy
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
_POC_PKG = os.path.join(_SRC, "vdeter", "poc")
# 기존 production 소스 트리(poc 본체·호환 shim·tests 제외)에서 poc를 참조하지 않아야 한다.
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

# Stage 6 통합에서 명시 승인된 **유일한** CLI opt-in adapter. 이 파일만 stage8을 import할 수 있고
# (S8-POC-API-REQ-01은 "PoC 미사용 경로에 자원 0"을 요구하며, PoC opt-in 자체를 영구 금지하는 것이
# 목적이 아니다), run_cmd가 --poc/--poc-dir일 때만 이 module을 lazy import한다(PoC OFF 구조적 0).
_SANCTIONED_CLI_ADAPTERS = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "standalone", "poc_run.py")),
})

# Stage 6 coordinator↔PoC 연결에서 명시 승인된 **RF-off software 세션 host** adapter.
# production CLI adapter(poc_run)와 달리, 이것은 `--rf-off-software`에서만 진입하는 **명시적
# 하드웨어 경계 double** harness다(실장비 실행에서는 진입하지 않는다). 따라서 시험 support의 Stage 7
# software group(`tests.support.stage7`)과 `vdeter.poc.testing` seam을 의도적으로 import하며
# (그것이 존재 이유), 아래 production-purity 규칙(.testing/rf_off 금지)에서 제외한다.
# 광범위 allowlist가 아니라 실제 결선에 필요한 파일만 명시 추가한다.
# - rf_off_software_session.py: 실 coordinator 프로세스의 RF-off software 세션 host.
#   `RfOffDeviceSession`이 실 Stage 7 software group을 띄우고(하드웨어 경계만 fake medium
#   worker) 권위 provider(`assemble_authoritative_provider`)를 그 members에 결속한다 — 그 결선
#   계약은 아래 `_rf_off_session_wiring`(AST)이 검사한다.
# - poc_device_seams_inprocess.py: Stage 8 PoC provider seam을 **실 Stage 7 객체**(worker·frozen PHY·MAC
#   hardware clock·route sidecar·수신기)에 결속하는 RF-off 통합 adapter(하드웨어 경계만 double).
_SANCTIONED_RF_OFF_SESSION = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "rf_off_software_session.py")),
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "poc_device_seams_inprocess.py")),
})

# 일반 운용 경로의 PoC device 결선 adapter. 장치는 launcher가 단독 소유하므로 supervisor(workload를
# 소유한 scenario controller 프로세스)는 §16.2 TEST adapter op으로만 장치에 닿는다. 이 파일이 그
# 경계를 조립하며, launcher **쪽** 구현(vdeter/b210_system/control_plane/poc_device.py)은 PoC 계층을 참조하지
# 않으므로 여기 목록에 없다(그 성질은 아래 규칙이 정적으로 강제한다).
_SANCTIONED_LAUNCHER_SEAMS = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "poc_device_seams_launcher.py")),
    # workload를 소유한 scenario controller child가 PoC supervisor를 호스팅한다(연결 §3.1).
    # stage8 참조는 전부 **method 안 lazy import**이며 `--poc-request` 없이는 실행되지 않는다 —
    # 그 성질은 아래 `test_controller_without_poc_request_loads_no_stage8`이 실제 실행으로 증명한다.
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "mass_roc_workload_controller.py")),
})

_POC_ADAPTERS = (_SANCTIONED_CLI_ADAPTERS | _SANCTIONED_RF_OFF_SESSION
                    | _SANCTIONED_LAUNCHER_SEAMS)


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # _SRC(vdeter package) + repo root(apps package) + VDETER(그 하위 apps) 모두 올린다.
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC, _PROJECT_ROOT, os.path.join(_PROJECT_ROOT, "VDETER")]
    ) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


_RF_OFF_SESSION_HOST = os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "rf_off_software_session.py"))
_RF_OFF_SESSION_CLASS = "RfOffDeviceSession"
_STAGE7_GROUP_SUPPORT = "tests.support.stage7"
_STAGE7_GROUP_BUILDER = "build_stage7_software_group"
_STAGE7_GROUP_STARTER = "start_stage7_group"
_SEAMS_MODULE = "apps.poc_device_seams_inprocess"
_AUTHORITATIVE_PROVIDER = "assemble_authoritative_provider"


def _call_name(node):
    """`Call` node의 호출 대상이 단순 이름이면 그 이름, 아니면 None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _is_self_attr(node, attr):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and node.attr == attr)


def _rf_off_session_wiring(tree):
    """`RfOffDeviceSession`↔실 Stage 7 software group 결선을 AST에서 읽어 dict로 돌려준다.

    - `imported_from`: `__init__` 안 from-import 이름 → module.
    - `group_names`: `build_stage7_software_group()` 반환값을 받은 변수 이름들(clock·medium·members…).
    - `group_started`: `start_stage7_group(...)`이 그 group 변수들로 호출됐는지.
    - `provider_factory` / `provider_members_arg` / `provider_has_station_map`: `self._provider`에
      대입되는 호출의 함수 이름, 그 첫 위치 인자(group members여야 한다), `station_id_by_endpoint` kw 유무.
    - `poc_provider_returns_wired`: `poc_provider()`가 `self._provider`를 그대로 돌려주는지.
    """
    klass = next((n for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name == _RF_OFF_SESSION_CLASS), None)
    if klass is None:
        return None
    methods = {n.name: n for n in klass.body if isinstance(n, ast.FunctionDef)}
    init = methods.get("__init__")
    result = {"imported_from": {}, "group_names": set(), "group_started": False,
              "provider_factory": None, "provider_members_arg": None,
              "provider_has_station_map": False, "poc_provider_returns_wired": False}
    if init is None:
        return result
    for node in ast.walk(init):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                result["imported_from"][alias.asname or alias.name] = node.module
    for node in ast.walk(init):
        if isinstance(node, ast.Assign) and _call_name(node.value) == _STAGE7_GROUP_BUILDER:
            for target in node.targets:
                elts = target.elts if isinstance(target, ast.Tuple) else [target]
                result["group_names"].update(e.id for e in elts if isinstance(e, ast.Name))
    for node in ast.walk(init):
        if isinstance(node, ast.Expr) and _call_name(node.value) == _STAGE7_GROUP_STARTER:
            args = [a.id for a in node.value.args if isinstance(a, ast.Name)]
            if args and set(args) <= result["group_names"]:
                result["group_started"] = True
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            targets = []
            for target in node.targets:
                targets.extend(target.elts if isinstance(target, ast.Tuple) else [target])
            if any(_is_self_attr(t, "_provider") for t in targets):
                call = node.value
                result["provider_factory"] = _call_name(call)
                first = call.args[0] if call.args else None
                result["provider_members_arg"] = first.id if isinstance(first, ast.Name) else None
                result["provider_has_station_map"] = any(
                    kw.arg == "station_id_by_endpoint" for kw in call.keywords)
    poc_provider = methods.get("poc_provider")
    if poc_provider is not None:
        result["poc_provider_returns_wired"] = any(
            isinstance(n, ast.Return) and _is_self_attr(n.value, "_provider")
            for n in ast.walk(poc_provider))
    return result


def _assert_rf_off_session_wired(case, tree):
    """결선 계약: 실 software group을 만들고 시작한 뒤, 그 members로 **권위 provider**를 조립해
    `poc_provider()`로 낸다. (문자열 부분 일치가 아니라 AST 구조로 판정한다.)"""
    wiring = _rf_off_session_wiring(tree)
    case.assertIsNotNone(wiring, f"{_RF_OFF_SESSION_CLASS} class가 세션 host에 없다")
    imported = wiring["imported_from"]
    case.assertEqual(imported.get(_STAGE7_GROUP_BUILDER), _STAGE7_GROUP_SUPPORT,
                     "software group builder는 tests.support.stage7에서 가져와야 한다")
    case.assertEqual(imported.get(_STAGE7_GROUP_STARTER), _STAGE7_GROUP_SUPPORT,
                     "software group starter는 tests.support.stage7에서 가져와야 한다")
    case.assertTrue(wiring["group_names"], "build_stage7_software_group() 반환값을 받지 않는다")
    case.assertTrue(wiring["group_started"],
                    "start_stage7_group이 그 group(clock·medium·coordinator·members·resolved)으로 "
                    "호출되지 않는다 — Stage 7 software group 연결이 끊겼다")
    case.assertEqual(wiring["provider_factory"], _AUTHORITATIVE_PROVIDER,
                     "self._provider는 권위 schedule_tx 경로 provider(assemble_authoritative_provider)"
                     "여야 한다(stub·fallback seam 아님)")
    case.assertEqual(imported.get(_AUTHORITATIVE_PROVIDER), _SEAMS_MODULE,
                     "권위 provider 조립기는 apps.poc_device_seams_inprocess에서 가져와야 한다")
    case.assertIn(wiring["provider_members_arg"], wiring["group_names"],
                  "provider는 실행 중 software group의 members로 조립해야 한다")
    case.assertTrue(wiring["provider_has_station_map"],
                    "provider 조립에 station_id_by_endpoint 매핑이 빠졌다")
    case.assertTrue(wiring["poc_provider_returns_wired"],
                    "poc_provider()가 결속된 self._provider를 돌려주지 않는다")


class _RenameCalls(ast.NodeTransformer):
    """이름 `old`(import alias·호출 대상)를 `new`로 바꾼다. 부정 대조용 in-memory 변조."""

    def __init__(self, old, new, module=None):
        self.old, self.new, self.module, self.changed = old, new, module, 0

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == self.old:
                alias.name = self.new
                if self.module is not None:
                    node.module = self.module
                self.changed += 1
        return node

    def visit_Name(self, node):
        if node.id == self.old:
            self.changed += 1
            return ast.copy_location(ast.Name(id=self.new, ctx=node.ctx), node)
        return node


class _DropCall(ast.NodeTransformer):
    """`name(...)` 문장을 제거한다(연결 제거). 부정 대조용 in-memory 변조."""

    def __init__(self, name):
        self.name, self.changed = name, 0

    def visit_Expr(self, node):
        if _call_name(node.value) == self.name:
            self.changed += 1
            return None
        return node


class _DetachProviderMembers(ast.NodeTransformer):
    """권위 provider 조립의 첫 인자만 group members가 아닌 다른 객체로 바꾼다(연결 제거).

    이름을 전역으로 바꾸면 group 변수·start 호출까지 같이 바뀌어 변조 후에도 자기 일관적이므로
    (검사가 통과해 버린다), 호출 인자 하나만 바꾼다.
    """

    def __init__(self):
        self.changed = 0

    def visit_Call(self, node):
        self.generic_visit(node)
        if _call_name(node) == _AUTHORITATIVE_PROVIDER and node.args:
            node.args[0] = ast.copy_location(
                ast.Name(id="detached_members", ctx=ast.Load()), node.args[0])
            self.changed += 1
        return node


class ImportBoundaryTest(unittest.TestCase):
    def _poc_referencing_files(self):
        """apps/common/MASS/ROC/src(stage8 자체 제외)에서 stage8을 문자열 참조하는 파일 목록."""
        hits = []
        scan_roots = [
            os.path.join(_SRC, "vdeter"),
            os.path.join(_PROJECT_ROOT, "apps"),
            os.path.join(_PROJECT_ROOT, "common"),
            os.path.join(_PROJECT_ROOT, "MASS"),
            os.path.join(_PROJECT_ROOT, "ROC"),
        ]
        for root in scan_roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                if os.path.normpath(dirpath).startswith(os.path.normpath(_POC_PKG)):
                    continue  # poc 본체 자체는 제외
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if "vdeter.poc" in text or "poc.api" in text:
                        hits.append(os.path.normpath(path))
        return hits

    def test_S8_POC_API_REQ_01_only_sanctioned_cli_adapter_imports_stage8(self):
        """오직 승인된 CLI opt-in adapter(poc_run.py)만 stage8을 참조한다(정적 증거).

        S8-POC-API-REQ-01의 목적은 **PoC 미사용 경로의 구조적 0**이지, PoC opt-in 자체의 영구 금지가
        아니다. Stage 6에서 실제 CLI(`vdeter run --poc/--poc-dir`)를 연결하되, stage8 의존을 단 하나의
        sanctioned adapter로 좁히고 나머지 모든 기존 소스는 여전히 stage8을 참조하지 않는다.
        """
        offenders = [p for p in self._poc_referencing_files()
                     if p not in _POC_ADAPTERS]
        rel = [os.path.relpath(p, _PROJECT_ROOT) for p in offenders]
        self.assertEqual(rel, [], f"승인 adapter 밖에서 stage8을 참조함: {rel}")

    def test_controller_without_poc_request_loads_no_stage8(self):
        """PoC를 요청하지 않으면 scenario controller는 stage8을 **적재하지 않는다**(구조적 0).

        controller가 allowlist에 있는 이유는 PoC supervisor를 호스팅하기 때문이지만, 그 참조는 전부
        method 안 lazy import다. import만으로 stage8이 딸려오면 "PoC OFF 경로 구조적 0"이 깨지므로
        실제로 실행해 확인한다(정적 문자열 검사로 대신하지 않는다).
        """
        done = _run_py(
            "import importlib, sys;"
            "importlib.import_module('apps.mass_roc_workload_controller');"
            "print(sorted(m for m in sys.modules if m.startswith('vdeter.poc')))"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("[]", done.stdout.strip(),
                      f"controller import만으로 stage8이 적재됐다: {done.stdout.strip()}")

    def test_sanctioned_adapter_is_exact_not_a_blanket_dir(self):
        """allowlist는 whole-directory가 아니라 정확히 poc_run.py 하나다(광범위 우회 금지).

        승인 adapter는 실제로 stage8을 참조해야 하고(opt-in 존재), 같은 디렉터리의 다른 CLI
        파일(cli.py·run_cmd.py)은 stage8을 직접 참조하지 않는다(opt-in을 adapter로 국한).
        """
        hits = set(self._poc_referencing_files())
        for adapter in _SANCTIONED_CLI_ADAPTERS:
            self.assertIn(adapter, hits, "승인 adapter가 stage8을 실제로 import해야 함")
        standalone = os.path.join(_PROJECT_ROOT, "apps", "standalone")
        for name in ("cli.py", "run_cmd.py", "local_session.py"):
            path = os.path.normpath(os.path.join(standalone, name))
            self.assertNotIn(path, hits,
                             f"{name}은 stage8을 직접 참조하면 안 됨(poc_run adapter로만)")

    def test_rf_off_session_adapters_are_exact_and_verification_only(self):
        """RF-off 통합 adapter는 정확히 이 파일들이며, 실제로 stage8을 참조하는 검증 전용 harness다
        (하드웨어 경계 double; production 실장비 경로에서 진입하지 않는다).

        production CLI 파일(cli/run_cmd/local_session)은 이들을 통해서도 stage8을 직접 참조하지 않는다
        (여전히 stage8 문자열 0). coordinator(VDETER/apps)는 scan root 밖이라 여기서 검사하지 않는다.
        각 파일의 double 성격: rf_off_session은 `RfOffDeviceSession`이 **실 Stage 7 software
        group**(tests.support.stage7; 하드웨어 경계만 fake medium worker)을 띄워 권위 provider를 그
        members에 결속한다(AST 결선 계약 검사); stage7_seams는 실 frozen PHY·production provider 조립이다.
        """
        hits = set(self._poc_referencing_files())
        for adapter in _SANCTIONED_RF_OFF_SESSION:
            self.assertIn(adapter, hits, "RF-off 통합 adapter가 stage8을 실제로 import해야 함")
        with open(_RF_OFF_SESSION_HOST, "r", encoding="utf-8") as fh:
            _assert_rf_off_session_wired(self, ast.parse(fh.read(), filename=_RF_OFF_SESSION_HOST))
        seams = os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "poc_device_seams_inprocess.py"))
        with open(seams, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Phy11Transmitter", text, "seams adapter는 실 frozen PHY에 결속한다")
        self.assertIn("for_production", text, "seams adapter는 stub 아닌 production provider를 조립한다")

    def test_rf_off_session_wiring_check_rejects_broken_or_wrong_wiring(self):
        """부정 대조: 결선 검사는 (a) software group 연결 제거, (b) 잘못된 provider 결속에서 실패한다.

        production 파일은 건드리지 않고 그 AST의 in-memory 사본만 변조한다. 각 변조가 실제로 노드를
        바꿨음을 함께 확인해 검사가 공허하게 통과하지 않게 한다.
        """
        with open(_RF_OFF_SESSION_HOST, "r", encoding="utf-8") as fh:
            original = ast.parse(fh.read(), filename=_RF_OFF_SESSION_HOST)
        _assert_rf_off_session_wired(self, original)     # 대조 기준: 원본은 통과

        mutations = {
            # (a) 연결 제거: software group을 시작하지 않는다.
            "group_not_started": _DropCall(_STAGE7_GROUP_STARTER),
            # (a) 연결 제거: provider를 group members가 아닌 다른 객체로 조립한다.
            "provider_not_from_group": _DetachProviderMembers(),
            # (b) 잘못된 공급자: 권위 경로 대신 worker 직접 제출 fallback 조립기.
            "fallback_provider": _RenameCalls(_AUTHORITATIVE_PROVIDER, "assemble_inprocess_provider"),
            # (b) 잘못된 공급자: 실 Stage 7 객체가 아닌 rf_off stub seam module에서 조립.
            "stub_seam_module": _RenameCalls(_AUTHORITATIVE_PROVIDER, _AUTHORITATIVE_PROVIDER,
                                             module="vdeter.poc.integration.rf_off"),
        }
        for label, transformer in mutations.items():
            with self.subTest(mutation=label):
                mutated = transformer.visit(copy.deepcopy(original))
                self.assertGreater(transformer.changed, 0, f"{label}: 변조가 아무 노드도 바꾸지 않았다")
                with self.assertRaises(AssertionError, msg=f"{label}: 깨진 결선을 통과시켰다"):
                    _assert_rf_off_session_wired(self, mutated)

    def test_sanctioned_adapter_does_not_import_testing_or_rf_off(self):
        """production adapter는 어떤 .testing이나 RF-off harness(rf_off)도 import하지 않는다(§4).

        device 경계 seam은 session이 제공하며, adapter가 시험용 seam을 조용히 끌어오지 않는다.
        """
        for adapter in _SANCTIONED_CLI_ADAPTERS:
            with open(adapter, "r", encoding="utf-8") as fh:
                text = fh.read()
            for line in text.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                self.assertNotIn(".testing", stripped, f"{adapter}: {stripped}")
                self.assertFalse(stripped.endswith("rf_off") or ".rf_off import" in stripped,
                                 f"{adapter}: {stripped}")

    def test_poc_off_cli_import_does_not_load_stage8(self):
        """PoC OFF: cli·run_cmd를 import해도 stage8을 로드하지 않는다(run_cmd가 lazy import)."""
        result = _run_py(
            "import sys\n"
            "import apps.standalone.cli, apps.standalone.run_cmd\n"
            "bad=[m for m in sys.modules if m.startswith('vdeter.poc')]\n"
            "print('LOADED', bad)\n"
            "sys.exit(0 if not bad else 4)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sanctioned_adapter_can_import_stage8(self):
        """opt-in: 승인 adapter는 stage8을 정상 import할 수 있다(연결이 실제로 가능)."""
        result = _run_py(
            "import sys, apps.standalone.poc_run\n"
            "ok=[m for m in sys.modules if m.startswith('vdeter.poc')]\n"
            "sys.exit(0 if ok else 5)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_public_api_import_excludes_supervisor(self):
        result = _run_py(
            "import sys, vdeter.poc.api\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.poc._supervisor')]\n"
            "print('PRIV', priv)\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sdk_import_excludes_supervisor(self):
        result = _run_py(
            "import sys, vdeter.poc.api.sdk\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.poc._supervisor')]\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_S8_POC_API_REQ_01_import_starts_no_thread_or_socket(self):
        result = _run_py(
            "import threading, sys\n"
            "before=threading.active_count()\n"
            "import vdeter.poc.api\n"
            "import vdeter.poc._supervisor.supervisor\n"
            "after=threading.active_count()\n"
            "print('THREADS', before, after)\n"
            "sys.exit(0 if before==after==1 else 2)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_stage8_package_loads_no_submodules(self):
        result = _run_py(
            "import sys, vdeter.poc\n"
            "subs=[m for m in sys.modules if m.startswith('vdeter.poc.')]\n"
            "print('SUBS', subs)\n"
            "sys.exit(0 if not subs else 3)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class OldPublicPathRemovedTest(unittest.TestCase):
    """옛 공개 경로 `vdeter.stage8` 은 **제거**됐다 (2026-09-16 사용자 결정).

    2-3 구조 정리에서 공개 surface 를 `vdeter.poc.api` 로 옮기면서 옛 경로를 같은
    객체로 가리키는 최소 호환 shim 을 두었으나, 사용자가 호환을 포기하기로 결정해
    제거했다. 따라서 옛 경로는 공개·비공개를 가리지 않고 **전부 열리지 않아야** 한다.
    """

    def test_old_public_path_is_gone(self):
        """옛 공개 경로가 더는 import 되지 않는다."""
        for module in ("vdeter.stage8", "vdeter.stage8.poc_api",
                       "vdeter.stage8.poc_api.sdk", "vdeter.stage8.poc_api.codec"):
            result = _run_py("import %s\n" % module)
            self.assertNotEqual(
                result.returncode, 0,
                "%s 이 아직 열린다 — 호환 제거 결정과 어긋난다" % module)

    def test_old_private_paths_are_gone(self):
        """비공개 내부도 옛 경로로 열리지 않는다(원래부터 비공개였다)."""
        for module in ("vdeter.stage8._supervisor", "vdeter.stage8.execution",
                       "vdeter.stage8.integration", "vdeter.stage8.shared_medium",
                       "vdeter.stage8.testing"):
            result = _run_py("import %s\n" % module)
            self.assertNotEqual(result.returncode, 0,
                                "%s 이 옛 경로로 열렸다" % module)

    def test_new_public_path_still_works(self):
        """새 공식 경로는 정상이다 — 제거가 공개 surface 자체를 깨지 않았다."""
        result = _run_py(
            "import importlib, sys\n"
            "import vdeter.poc.api as api\n"
            "names = ('codec','errors','limits','messages','negotiation',\n"
            "         'protocol','sdk','seams','transport','types')\n"
            "for n in names:\n"
            "    assert importlib.import_module('vdeter.poc.api.' + n) is getattr(api, n), n\n"
            "assert api.PocErrorCode and api.SdkEngine and api.encode_wire_packet\n"
            "sys.exit(0)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shim_files_are_absent_from_the_source_tree(self):
        """호환 shim 파일이 소스 트리에 남아 있지 않다."""
        for relative in (("vdeter", "stage8", "__init__.py"),
                         ("vdeter", "stage8", "poc_api", "__init__.py")):
            path = os.path.join(_SRC, *relative)
            self.assertFalse(os.path.exists(path), "호환 shim 이 남아 있다: %s" % path)
        self.assertFalse(os.path.isdir(os.path.join(_SRC, "vdeter", "stage8")),
                         "vdeter/stage8 디렉터리가 남아 있다")

    def test_production_sources_use_the_new_path_only(self):
        """저장 식별자·이 계약시험 밖에서 옛 경로 문자열이 없다.

        허용 범주는 파일 단위로 명시한다(디렉터리 통째 제외 금지). 저장되는 wire 값
        `vdeter.stage8.poc-api.*`·`vdeter.stage8.poc-manifest.*`는 모듈 경로가 아니라
        **값**이므로 계약상 보존 대상이다.
        """
        allowed_files = {os.path.normpath(os.path.abspath(__file__))}
        offenders = []
        for root in (_SRC, os.path.join(_PROJECT_ROOT, "apps"),
                     os.path.join(_PROJECT_ROOT, "VDETER", "apps"),
                     os.path.join(_PROJECT_ROOT, "VDETER", "tools"),
                     os.path.join(_PROJECT_ROOT, "tools")):
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.normpath(os.path.join(dirpath, name))
                    if path in allowed_files:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if "vdeter.stage8" not in line and "vdeter/stage8" not in line:
                                continue
                            # 저장되는 프로토콜/manifest 식별자 값은 보존 대상이다.
                            if "vdeter.stage8.poc-" in line:
                                continue
                            offenders.append("%s:%d %s"
                                             % (os.path.relpath(path, _PROJECT_ROOT),
                                                lineno, line.strip()[:120]))
        self.assertEqual(offenders, [],
                         "production이 옛 경로를 참조한다: %s" % offenders)


if __name__ == "__main__":
    unittest.main()
