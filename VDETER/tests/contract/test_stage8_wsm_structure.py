"""Stage 5 shared-medium: 기능 OFF 구조적 0·import 경계 계약시험.

RF-off 요구 매핑: 19(feature OFF 구조적 0), 20 지원(기존 경로 무변경), Stage 4 미참조,
_supervisor private 미import, numpy 비의존.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
_STAGE8 = os.path.join(_SRC, "vdeter", "stage8")
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class StructuralZeroTest(unittest.TestCase):
    def test_import_starts_no_thread(self):
        result = _run_py(
            "import threading, sys\n"
            "before=threading.active_count()\n"
            "import vdeter.stage8.shared_medium\n"
            "after=threading.active_count()\n"
            "sys.exit(0 if before==after==1 else 2)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_opens_no_socket_or_disk_write(self):
        # socket.socket과 open(write)을 감시해 import 중 생성·쓰기가 없음을 확인.
        result = _run_py(
            "import builtins, socket, sys\n"
            "calls={'sock':0,'write':0}\n"
            "_os=socket.socket.__init__\n"
            "def _s(self,*a,**k):\n"
            "    calls['sock']+=1; return _os(self,*a,**k)\n"
            "socket.socket.__init__=_s\n"
            "_open=builtins.open\n"
            "def _o(f,mode='r',*a,**k):\n"
            "    if any(m in mode for m in ('w','a','x','+')): calls['write']+=1\n"
            "    return _open(f,mode,*a,**k)\n"
            "builtins.open=_o\n"
            "import vdeter.stage8.shared_medium\n"
            "sys.exit(0 if calls['sock']==0 and calls['write']==0 else 3)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_supervisor_private(self):
        result = _run_py(
            "import sys, vdeter.stage8.shared_medium\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.stage8._supervisor')]\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_numpy(self):
        result = _run_py(
            "import sys, vdeter.stage8.shared_medium\n"
            "import vdeter.stage8.shared_medium.provider\n"
            "import vdeter.stage8.shared_medium.observation\n"
            "sys.exit(0 if 'numpy' not in sys.modules else 4)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_stage4_poc_execution(self):
        result = _run_py(
            "import sys, vdeter.stage8.shared_medium\n"
            "bad=[m for m in sys.modules if 'poc_execution' in m or m.startswith('vdeter.stage8.poc_exec')]\n"
            "sys.exit(0 if not bad else 5)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ImportBoundaryTest(unittest.TestCase):
    def test_no_existing_source_imports_shared_medium(self):
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
                    continue  # stage8 자체 제외(shared_medium 내부 참조는 허용)
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if "shared_medium" in text:
                        offenders.append(os.path.relpath(path, _PROJECT_ROOT))
        self.assertEqual(offenders, [], f"기존 소스가 shared_medium을 참조함: {offenders}")

    def test_shared_medium_source_does_not_import_supervisor(self):
        base = os.path.join(_STAGE8, "shared_medium")
        offenders = []
        for name in os.listdir(base):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(base, name), "r", encoding="utf-8") as fh:
                text = fh.read()
            if "_supervisor" in text:
                offenders.append(name)
        self.assertEqual(offenders, [], f"shared_medium이 _supervisor를 참조함: {offenders}")


if __name__ == "__main__":
    unittest.main()
