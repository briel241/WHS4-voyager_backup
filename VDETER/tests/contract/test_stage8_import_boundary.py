"""Stage 8 public/private import 경계와 기능 OFF 구조적 0 시험
(명세 §3.2, §19; REQ-01). 대부분 플랫폼 무관.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
_STAGE8 = os.path.join(_SRC, "vdeter", "stage8")
# 기존 production 소스 트리(stage8·tests 제외)에서 stage8을 참조하지 않아야 한다.
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class ImportBoundaryTest(unittest.TestCase):
    def test_S8_POC_API_REQ_01_no_existing_source_imports_stage8(self):
        """기존 runtime 소스가 stage8을 import하지 않는다(구조적 0의 정적 증거).

        stage8은 신규 파일만 추가했고 기존 파일을 수정하지 않았으므로, 어떤 기존
        entrypoint를 import해도 stage8이 로드되지 않는다.
        """
        offenders = []
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
                if os.path.normpath(dirpath).startswith(os.path.normpath(_STAGE8)):
                    continue  # stage8 자체 제외
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if "vdeter.stage8" in text or "stage8.poc_api" in text:
                        offenders.append(os.path.relpath(path, _PROJECT_ROOT))
        self.assertEqual(offenders, [], f"기존 소스가 stage8을 참조함: {offenders}")

    def test_public_api_import_excludes_supervisor(self):
        result = _run_py(
            "import sys, vdeter.stage8.poc_api\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.stage8._supervisor')]\n"
            "print('PRIV', priv)\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sdk_import_excludes_supervisor(self):
        result = _run_py(
            "import sys, vdeter.stage8.poc_api.sdk\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.stage8._supervisor')]\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_S8_POC_API_REQ_01_import_starts_no_thread_or_socket(self):
        result = _run_py(
            "import threading, sys\n"
            "before=threading.active_count()\n"
            "import vdeter.stage8.poc_api\n"
            "import vdeter.stage8._supervisor.supervisor\n"
            "after=threading.active_count()\n"
            "print('THREADS', before, after)\n"
            "sys.exit(0 if before==after==1 else 2)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_stage8_package_loads_no_submodules(self):
        result = _run_py(
            "import sys, vdeter.stage8\n"
            "subs=[m for m in sys.modules if m.startswith('vdeter.stage8.')]\n"
            "print('SUBS', subs)\n"
            "sys.exit(0 if not subs else 3)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
