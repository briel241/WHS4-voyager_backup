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

# Stage 6 통합에서 명시 승인된 **유일한** CLI opt-in adapter. 이 파일만 stage8을 import할 수 있고
# (S8-POC-API-REQ-01은 "PoC 미사용 경로에 자원 0"을 요구하며, PoC opt-in 자체를 영구 금지하는 것이
# 목적이 아니다), run_cmd가 --poc/--poc-dir일 때만 이 module을 lazy import한다(PoC OFF 구조적 0).
_SANCTIONED_CLI_ADAPTERS = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "standalone", "poc_run.py")),
})

# Stage 6 coordinator↔PoC 연결에서 명시 승인된 **RF-off software 세션 host** adapter.
# production CLI adapter(poc_run)와 달리, 이것은 `--rf-off-software`에서만 진입하는 **명시적
# 하드웨어 경계 double** harness다(실장비 실행에서는 진입하지 않는다). 따라서 rf_off·testing seam을
# 의도적으로 import하며(그것이 존재 이유), 아래 production-purity 규칙(.testing/rf_off 금지)에서 제외한다.
# 광범위 allowlist가 아니라 실제 결선에 필요한 파일만 명시 추가한다.
# - stage8_rf_off_session.py: 실 coordinator 프로세스의 RF-off software 세션 host.
# - stage8_stage7_seams.py: Stage 8 PoC provider seam을 **실 Stage 7 객체**(worker·frozen PHY·MAC
#   hardware clock·route sidecar·수신기)에 결속하는 RF-off 통합 adapter(하드웨어 경계만 double).
_SANCTIONED_RF_OFF_SESSION = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "stage8_rf_off_session.py")),
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "stage8_stage7_seams.py")),
})

# 일반 운용 경로의 PoC device 결선 adapter. 장치는 launcher가 단독 소유하므로 supervisor(workload를
# 소유한 scenario controller 프로세스)는 §16.2 TEST adapter op으로만 장치에 닿는다. 이 파일이 그
# 경계를 조립하며, launcher **쪽** 구현(vdeter/stage7/testing/poc_device.py)은 PoC 계층을 참조하지
# 않으므로 여기 목록에 없다(그 성질은 아래 규칙이 정적으로 강제한다).
_SANCTIONED_LAUNCHER_SEAMS = frozenset({
    os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "stage8_launcher_seams.py")),
})

_STAGE8_ADAPTERS = (_SANCTIONED_CLI_ADAPTERS | _SANCTIONED_RF_OFF_SESSION
                    | _SANCTIONED_LAUNCHER_SEAMS)


def _run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # _SRC(vdeter package) + repo root(apps package) + VDETER(그 하위 apps) 모두 올린다.
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC, _PROJECT_ROOT, os.path.join(_PROJECT_ROOT, "VDETER")]
    ) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


class ImportBoundaryTest(unittest.TestCase):
    def _stage8_referencing_files(self):
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
                if os.path.normpath(dirpath).startswith(os.path.normpath(_STAGE8)):
                    continue  # stage8 자체 제외
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if "vdeter.stage8" in text or "stage8.poc_api" in text:
                        hits.append(os.path.normpath(path))
        return hits

    def test_S8_POC_API_REQ_01_only_sanctioned_cli_adapter_imports_stage8(self):
        """오직 승인된 CLI opt-in adapter(poc_run.py)만 stage8을 참조한다(정적 증거).

        S8-POC-API-REQ-01의 목적은 **PoC 미사용 경로의 구조적 0**이지, PoC opt-in 자체의 영구 금지가
        아니다. Stage 6에서 실제 CLI(`vdeter run --poc/--poc-dir`)를 연결하되, stage8 의존을 단 하나의
        sanctioned adapter로 좁히고 나머지 모든 기존 소스는 여전히 stage8을 참조하지 않는다.
        """
        offenders = [p for p in self._stage8_referencing_files()
                     if p not in _STAGE8_ADAPTERS]
        rel = [os.path.relpath(p, _PROJECT_ROOT) for p in offenders]
        self.assertEqual(rel, [], f"승인 adapter 밖에서 stage8을 참조함: {rel}")

    def test_sanctioned_adapter_is_exact_not_a_blanket_dir(self):
        """allowlist는 whole-directory가 아니라 정확히 poc_run.py 하나다(광범위 우회 금지).

        승인 adapter는 실제로 stage8을 참조해야 하고(opt-in 존재), 같은 디렉터리의 다른 CLI
        파일(cli.py·run_cmd.py)은 stage8을 직접 참조하지 않는다(opt-in을 adapter로 국한).
        """
        hits = set(self._stage8_referencing_files())
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
        각 파일은 하드웨어 경계 double 성격을 명시한다: rf_off_session은 명시적 RF-off double(rf_off),
        stage7_seams는 실 Stage 7 객체 결속 + fake medium worker 경계다.
        """
        hits = set(self._stage8_referencing_files())
        for adapter in _SANCTIONED_RF_OFF_SESSION:
            self.assertIn(adapter, hits, "RF-off 통합 adapter가 stage8을 실제로 import해야 함")
        session_host = os.path.normpath(os.path.join(_PROJECT_ROOT, "apps",
                                                     "stage8_rf_off_session.py"))
        with open(session_host, "r", encoding="utf-8") as fh:
            self.assertIn("rf_off", fh.read(), "RF-off 세션 host는 명시적 RF-off double(rf_off)을 쓴다")
        seams = os.path.normpath(os.path.join(_PROJECT_ROOT, "apps", "stage8_stage7_seams.py"))
        with open(seams, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Phy11Transmitter", text, "seams adapter는 실 frozen PHY에 결속한다")
        self.assertIn("for_production", text, "seams adapter는 stub 아닌 production provider를 조립한다")

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
            "bad=[m for m in sys.modules if m.startswith('vdeter.stage8')]\n"
            "print('LOADED', bad)\n"
            "sys.exit(0 if not bad else 4)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sanctioned_adapter_can_import_stage8(self):
        """opt-in: 승인 adapter는 stage8을 정상 import할 수 있다(연결이 실제로 가능)."""
        result = _run_py(
            "import sys, apps.standalone.poc_run\n"
            "ok=[m for m in sys.modules if m.startswith('vdeter.stage8')]\n"
            "sys.exit(0 if ok else 5)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
