"""Stage 8 6단계 통합 layer 구조 계약(기능 OFF 구조적 0·production testing-free·fail-closed).

- production 통합 module은 어떤 `.testing` module이나 RF-off harness(`rf_off`)를 import하지 않는다
  (시험용 seam이 production 조립에 조용히 끼어들 수 없음, §4).
- `entry.run_poc_workload`는 `device_seams`가 필수다(fail-closed; 조용한 RF-off 기본값 금지).
- `vdeter.poc.integration` package를 import하는 것만으로 thread/socket/process가 생기지 않는다.
- bare `import vdeter.poc`은 integration submodule을 로드하지 않는다.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
_INTEGRATION = os.path.join(_SRC, "vdeter", "poc", "integration")

#: production(=시험용 seam을 쓰지 않는) 통합 module. `rf_off`만 RF-off harness다.
_PRODUCTION_MODULES = (
    "__init__.py", "assembly.py", "device_boundary.py", "tx_executor.py",
    "supervisor_connection.py", "runtime.py", "entry.py",
)


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class IntegrationArchitectureTest(unittest.TestCase):
    def test_production_modules_do_not_import_testing_or_rf_off_harness(self):
        offenders = []
        for name in _PRODUCTION_MODULES:
            path = os.path.join(_INTEGRATION, name)
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            for line in text.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if ".testing" in stripped or "shared_medium.testing" in stripped:
                    offenders.append(f"{name}: {stripped}")
                if stripped.endswith("rf_off") or ".rf_off import" in stripped:
                    offenders.append(f"{name}: {stripped}")
        self.assertEqual(offenders, [], f"production 통합 module이 시험용 seam을 import함: {offenders}")

    def test_run_poc_workload_requires_device_seams(self):
        from vdeter.poc.integration.entry import run_poc_workload
        sig = inspect.signature(run_poc_workload)
        self.assertIn("device_seams", sig.parameters)
        self.assertIs(sig.parameters["device_seams"].default, inspect.Parameter.empty,
                      "device_seams는 필수여야 한다(조용한 RF-off 기본값 금지)")

    def test_for_production_fail_closed_without_seams(self):
        # DeviceSeams에 seam이 빠지면 assemble_provider가 for_production에서 fail-closed.
        from vdeter.poc.integration.assembly import DeviceSeams, assemble_provider
        from vdeter.poc.integration.device_boundary import SoftwareGroupTx
        seams = DeviceSeams(group_tx=SoftwareGroupTx(), arbiter=None, scheduler=None,
                            rx_readiness=None, waveform_factory=None)
        with self.assertRaises(ValueError):
            assemble_provider(seams)

    def test_import_integration_starts_no_thread_or_socket(self):
        result = _run_py(
            "import threading, sys\n"
            "before=threading.active_count()\n"
            "import vdeter.poc.integration\n"
            "import vdeter.poc.integration.assembly, vdeter.poc.integration.device_boundary\n"
            "import vdeter.poc.integration.tx_executor, vdeter.poc.integration.runtime\n"
            "assert threading.active_count()==before, 'thread leaked on import'\n"
            "assert not any('socket' in m and 'server' in m for m in sys.modules), 'no server'\n"
            "print('OK')\n"
        )
        self.assertIn("OK", result.stdout, result.stdout + result.stderr)

    def test_bare_stage8_import_excludes_integration(self):
        result = _run_py(
            "import sys, vdeter.poc\n"
            "loaded=[m for m in sys.modules if m.startswith('vdeter.poc.integration')]\n"
            "sys.exit(0 if not loaded else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
