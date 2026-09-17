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
_POC_PKG = os.path.join(_SRC, "vdeter", "poc")
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

# Stage 6 RF-off 통합에서 명시 승인된 shared-medium adapter: PoC provider seam을 실 Stage 7 객체에
# 결속해 `SharedMediumActionProvider.for_production`을 조립한다(하드웨어 경계만 double). "기능 OFF
# 구조적 0"은 PoC 미사용 경로에 대한 것이고, 이 adapter는 RF-off 통합 검증에서만 import된다.
_SANCTIONED_SHARED_MEDIUM_ADAPTERS = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "poc_device_seams_inprocess.py")),
    # RF-off software 세션 host: 실 Stage 7 seam 결속 시 common-LO center(shared_medium.constants)를
    # 참조한다. `--rf-off-software`에서만 진입하는 검증 전용 경로다.
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "rf_off_software_session.py")),
    # 일반 운용 경로의 launcher device 결선 adapter: provider 조립(common-LO center·IQ 직렬화)에
    # shared_medium을 쓴다. 장치는 launcher가 소유하므로 §16.2 op으로만 닿는다.
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "poc_device_seams_launcher.py")),
    # workload controller child(= PoC supervisor 호스트): 관측의 physical channel을 권위 role 표
    # (`shared_medium.topology`)에서 유도한다. 참조는 전부 method 안 lazy import이며
    # `--poc-request` 없이는 실행되지 않는다(import 경계 시험이 실제 실행으로 증명한다).
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "mass_roc_workload_controller.py")),
})


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class StructuralZeroTest(unittest.TestCase):
    def test_import_starts_no_thread(self):
        result = _run_py(
            "import threading, sys\n"
            "before=threading.active_count()\n"
            "import vdeter.poc.shared_medium\n"
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
            "import vdeter.poc.shared_medium\n"
            "sys.exit(0 if calls['sock']==0 and calls['write']==0 else 3)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_supervisor_private(self):
        result = _run_py(
            "import sys, vdeter.poc.shared_medium\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.poc._supervisor')]\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_numpy(self):
        result = _run_py(
            "import sys, vdeter.poc.shared_medium\n"
            "import vdeter.poc.shared_medium.provider\n"
            "import vdeter.poc.shared_medium.observation\n"
            "sys.exit(0 if 'numpy' not in sys.modules else 4)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_does_not_load_stage4_poc_execution(self):
        result = _run_py(
            "import sys, vdeter.poc.shared_medium\n"
            "bad=[m for m in sys.modules if 'poc_execution' in m or m.startswith('vdeter.poc.poc_exec')]\n"
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
                if os.path.normpath(dirpath).startswith(os.path.normpath(_POC_PKG)):
                    continue  # poc 본체 자체는 제외(내부 참조는 허용)
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if "shared_medium" in text and os.path.normpath(path) not in \
                            _SANCTIONED_SHARED_MEDIUM_ADAPTERS:
                        offenders.append(os.path.relpath(path, _PROJECT_ROOT))
        self.assertEqual(offenders, [], f"기존 소스가 shared_medium을 참조함: {offenders}")

    def test_shared_medium_source_does_not_import_supervisor(self):
        base = os.path.join(_POC_PKG, "shared_medium")
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
