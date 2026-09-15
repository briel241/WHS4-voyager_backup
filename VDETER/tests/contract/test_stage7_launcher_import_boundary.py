"""Stage 7 launcher production 경로의 시험 package 의존 0 (STR-07, 묶음 1-3).

`VDETER/apps/endpoint_launcher.py`는 실장비 endpoint 프로세스다. 1-3 이전에는 `main()` 안에서
`tests.support.stage4`(시험 fixture)를 import해 배치 profile을 얻었다. 이제 배치 값·계산은 production module
`vdeter.b210_system.deployment`가 소유하며, launcher와 그 module의 전이 import 폐포 어디에도 `tests`·`apps`
package가 없어야 한다. 정적(AST; 함수 안 lazy import 포함) + 실행(subprocess, `tests`가 import 불가능한
PYTHONPATH) + 부정 대조(in-memory AST 변조)로 검사한다. 플랫폼 무관.
"""

from __future__ import annotations

import ast
import copy
import io
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_VDETER = os.path.normpath(os.path.join(_HERE, "..", ".."))
_PROJECT_ROOT = os.path.normpath(os.path.join(_VDETER, ".."))
_SRC = os.path.join(_VDETER, "src")
_LAUNCHER = os.path.join(_VDETER, "apps", "endpoint_launcher.py")
_DEPLOYMENT_MODULE = "vdeter.b210_system.deployment"
#: production이 import하면 안 되는 최상위 package(시험 support·시험 fixture).
_TEST_PACKAGES = ("tests",)


def _parse(path):
    with io.open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _module_name_of(path):
    """`_SRC` 아래 파일의 dotted module 이름과 package 여부."""
    relative = os.path.relpath(path, _SRC).replace(os.sep, "/")
    if relative.endswith("/__init__.py"):
        return relative[: -len("/__init__.py")].replace("/", "."), True
    return relative[: -len(".py")].replace("/", "."), False


def _imports_of(tree, module_name=None, is_package=False):
    """모든 import 문(함수 안 lazy import 포함)의 절대 module 이름과 행 번호. 상대 import는 해석한다."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and module_name is not None:
                parts = module_name.split(".")
                package = parts if is_package else parts[:-1]
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.append((module, node.lineno))
            # `from pkg import sub` 형태의 submodule import도 폐포에 넣는다(존재하면).
            for alias in node.names:
                found.append((f"{module}.{alias.name}" if module else alias.name, node.lineno))
    return found


def _test_package_imports(tree, module_name=None, is_package=False):
    """`tests` package(또는 그 하위)를 import하는 (module, lineno) 목록."""
    return [(module, lineno) for module, lineno in _imports_of(tree, module_name, is_package)
            if any(module == pkg or module.startswith(pkg + ".") for pkg in _TEST_PACKAGES)]


def _resolve_vdeter(module):
    """`vdeter.*` module 이름을 `_SRC` 아래 파일로 해석한다(없으면 None)."""
    if not (module == "vdeter" or module.startswith("vdeter.")):
        return None
    base = os.path.join(_SRC, *module.split("."))
    if os.path.isfile(base + ".py"):
        return base + ".py"
    if os.path.isfile(os.path.join(base, "__init__.py")):
        return os.path.join(base, "__init__.py")
    return None


def _vdeter_closure(root_module):
    """`root_module`에서 시작한 `vdeter.*` 전이 import 폐포: {module: (path, [비-vdeter import 목록])}."""
    closure = {}
    queue = [root_module]
    while queue:
        module = queue.pop()
        if module in closure:
            continue
        path = _resolve_vdeter(module)
        if path is None:
            continue
        name, is_package = _module_name_of(path)
        imports = _imports_of(_parse(path), name, is_package)
        closure[module] = (path, [m for m, _ in imports if not m.startswith("vdeter")])
        for imported, _lineno in imports:
            if imported.startswith("vdeter") and imported not in closure:
                queue.append(imported)
    return closure


def _run_py(code, pythonpath):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class LauncherImportBoundaryTest(unittest.TestCase):
    def test_launcher_source_imports_no_test_package(self):
        """launcher 파일의 모든 import 문(함수 안 포함)에 `tests.*`가 없다(정적)."""
        offenders = _test_package_imports(_parse(_LAUNCHER))
        self.assertEqual(offenders, [], f"launcher가 시험 package를 import함: {offenders}")

    def test_launcher_takes_deployment_from_the_production_module(self):
        """배치 profile은 `vdeter.b210_system.deployment`에서 온다(시험 fixture 우회 없음)."""
        imports = {module for module, _ in _imports_of(_parse(_LAUNCHER))}
        self.assertIn(_DEPLOYMENT_MODULE, imports)
        self.assertIn(f"{_DEPLOYMENT_MODULE}.build_deployment_profile", imports)
        self.assertIn(f"{_DEPLOYMENT_MODULE}.build_utc_day_tbb_fixture", imports)
        self.assertNotIn("tests.support.stage4", imports)

    def test_deployment_module_closure_is_free_of_test_and_app_packages(self):
        """`vdeter.b210_system.deployment`의 전이 import 폐포(vdeter 안)는 `tests`·`apps`를 import하지 않는다."""
        closure = _vdeter_closure(_DEPLOYMENT_MODULE)
        self.assertIn(_DEPLOYMENT_MODULE, closure)
        self.assertIn("vdeter.mac.deployment", closure)
        self.assertIn("vdeter.mac.tbb", closure)
        offenders = {
            module: [m for m in externals if m.split(".")[0] in ("tests", "apps")]
            for module, (_path, externals) in closure.items()
        }
        offenders = {k: v for k, v in offenders.items() if v}
        self.assertEqual(offenders, {}, f"production 폐포가 시험/앱 package를 import함: {offenders}")

    def test_deployment_module_executes_with_only_src_on_the_path(self):
        """실행 검사: `tests`가 import **불가능한** PYTHONPATH(src만)로 module을 적재·실행한다."""
        result = _run_py(
            "import sys\n"
            f"import {_DEPLOYMENT_MODULE} as m\n"
            "fx = m.build_utc_day_tbb_fixture(now_epoch_seconds=1789459800)\n"
            "p = m.build_deployment_profile(expected_fingerprint=bytes(6), tbb_fixture=fx)\n"
            "bad = sorted(k for k in sys.modules if k.split('.')[0] in ('tests', 'apps'))\n"
            "print('START', fx.start_time, 'BAD', bad)\n"
            "sys.exit(0 if not bad and fx.start_time == 842745600 else 3)\n",
            pythonpath=[_SRC],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_module_import_loads_no_test_package(self):
        """launcher module import(main 미실행)로 `tests` package가 적재되지 않는다."""
        result = _run_py(
            "import sys, importlib\n"
            "importlib.import_module('apps.endpoint_launcher')\n"
            "bad = sorted(k for k in sys.modules if k.split('.')[0] == 'tests')\n"
            "print('BAD', bad)\n"
            "sys.exit(0 if not bad else 4)\n",
            pythonpath=[_VDETER, _SRC],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_checker_flags_test_package_imports(self):
        """부정 대조: 검사기는 module 수준·함수 안·상대 경로의 `tests` import를 잡는다(production 파일 무변조)."""
        launcher = _parse(_LAUNCHER)
        self.assertEqual(_test_package_imports(launcher), [])        # 대조 기준
        main = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "main")

        # (1) 1-3 이전 launcher의 실제 import 문을 main 안에 다시 넣는다.
        mutated = copy.deepcopy(launcher)
        mutated_main = next(n for n in mutated.body
                            if isinstance(n, ast.FunctionDef) and n.name == "main")
        injected = ast.parse("from tests.support.stage4 import deployment_profile, tbb_fixture").body[0]
        ast.increment_lineno(injected, mutated_main.lineno)     # main 본문 안 행 번호로 옮긴다
        mutated_main.body.insert(0, injected)
        ast.fix_missing_locations(mutated)
        flagged = _test_package_imports(mutated)
        self.assertTrue(flagged, "함수 안 tests.support import를 놓쳤다")
        self.assertTrue(any(m == "tests.support.stage4" for m, _ in flagged), flagged)
        self.assertGreater(min(lineno for _, lineno in flagged), main.lineno,
                           "잡힌 import가 main() 본문 안의 것이어야 한다")

        # (2) module 수준 `import tests` / `from tests import support` 도 잡힌다.
        for source in ("import tests", "from tests import support", "import tests.support.stage7 as s7"):
            with self.subTest(source=source):
                self.assertTrue(_test_package_imports(ast.parse(source)), source)

        # (3) 상대 import 해석: vdeter.b210_system.x 안의 `from ..mac import tbb`는 vdeter로 해석돼 잡히지 않고,
        #     production 이름이 `tests`로 시작하는 다른 module(`testsuite`)도 오탐하지 않는다.
        relative = ast.parse("from ..mac import tbb\nimport testsuite.things\n")
        self.assertEqual(_test_package_imports(relative, "vdeter.b210_system.deployment", False), [])
        resolved = {m for m, _ in _imports_of(relative, "vdeter.b210_system.deployment", False)}
        self.assertIn("vdeter.mac", resolved)
        self.assertIn("vdeter.mac.tbb", resolved)

    def test_closure_checker_flags_a_test_import_inside_production(self):
        """부정 대조: 폐포 검사는 production module의 (함수 안) `tests` import도 잡는다."""
        path = _resolve_vdeter(_DEPLOYMENT_MODULE)
        name, is_package = _module_name_of(path)
        tree = _parse(path)
        self.assertEqual(_test_package_imports(tree, name, is_package), [])   # 대조 기준
        mutated = copy.deepcopy(tree)
        function = next(n for n in mutated.body
                        if isinstance(n, ast.FunctionDef) and n.name == "build_deployment_profile")
        function.body.insert(0, ast.parse("from tests.support.stage4 import SERVICE_AREA").body[0])
        ast.fix_missing_locations(mutated)
        self.assertEqual([m for m, _ in _test_package_imports(mutated, name, is_package)],
                         ["tests.support.stage4", "tests.support.stage4.SERVICE_AREA"])


if __name__ == "__main__":
    unittest.main()
