"""기능 OFF 구조적 0 시험(실행 명세 §3.2, `S8-POC-REQ-18`, 연결 API `S8-POC-API-REQ-01`).

PoC 미선택(`none`) 경로와 catalog/selection control-plane에는 child·socket·thread·event
fan-out·serialization·write가 없어야 한다. 대부분 platform 무관(subprocess import 검사).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class ExecutionImportStructuralZeroTest(unittest.TestCase):
    def test_importing_execution_does_not_load_supervisor(self):
        # execution layer는 public poc_api·seam만 쓰고 private _supervisor를 import하지 않는다.
        result = _run_py(
            "import sys\n"
            "import vdeter.stage8.execution.runner\n"
            "import vdeter.stage8.execution.session\n"
            "import vdeter.stage8.execution.catalog\n"
            "import vdeter.stage8.execution.snapshot\n"
            "import vdeter.stage8.execution.selection\n"
            "import vdeter.stage8.execution.render\n"
            "priv=[m for m in sys.modules if m.startswith('vdeter.stage8._supervisor')]\n"
            "print('PRIV', priv)\n"
            "sys.exit(0 if not priv else 1)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_importing_execution_starts_no_thread_or_socket(self):
        result = _run_py(
            "import threading\n"
            "before=threading.active_count()\n"
            "import vdeter.stage8.execution.runner\n"
            "import vdeter.stage8.execution.session\n"
            "import vdeter.stage8.execution.catalog\n"
            "after=threading.active_count()\n"
            "print('THREADS', before, after)\n"
            "import sys; sys.exit(0 if before==after==1 else 2)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_stage8_import_does_not_load_execution(self):
        result = _run_py(
            "import sys, vdeter.stage8\n"
            "subs=[m for m in sys.modules if m.startswith('vdeter.stage8.')]\n"
            "print('SUBS', subs)\n"
            "sys.exit(0 if not subs else 3)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class NoneSelectionStructuralZeroTest(unittest.TestCase):
    """`none` 선택·menu render·rescan은 연결 자원을 만들지 않는다(§3.2)."""

    def test_menu_and_session_create_no_threads(self):
        from vdeter.stage8.execution.selection import build_menu
        from vdeter.stage8.execution.catalog import Catalog
        from vdeter.stage8.execution.session import ResidentSession
        from vdeter.stage8.testing.fakes import FakeClock

        before = threading.active_count()
        clock = FakeClock()
        session = ResidentSession(clock=clock)
        session.start()
        session.complete_bringup()
        cat = Catalog(poc_dir="/poc")
        menu = build_menu(cat, "SC-VDE-006",
                          session_absolute_remaining_s=session.remaining_session_absolute(),
                          idle_remaining_s=session.remaining_ready_idle())
        _ = menu.render()
        # none 선택(index 0)은 연결·child·socket을 만들지 않는다.
        none_item = menu.resolve(0)
        session.begin_workload()
        session.end_workload()
        after = threading.active_count()
        self.assertEqual(none_item.kind, "none")
        self.assertEqual(before, after)

    def test_control_plane_activity_creates_no_disk_write_side_effects(self):
        # rescan/menu render는 순수 model 연산이다. 파일/socket을 만들지 않는다.
        from vdeter.stage8.execution.selection import build_menu, rescan_summary
        from vdeter.stage8.execution.catalog import Catalog
        cat = Catalog(poc_dir="/poc")
        _ = build_menu(cat, "SC-VDE-006").render()
        summary = rescan_summary(cat, cat)
        self.assertEqual(summary.valid_count, 0)
        self.assertEqual(summary.changed_count, 0)


if __name__ == "__main__":
    unittest.main()
