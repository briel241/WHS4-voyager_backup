"""설치 capability 증언 **발급기** — 이 설치에서 직접 검증을 돌리고 그 결과를 결속한다.

`capability_attestation`은 증언을 **읽는** 쪽(유일한 권위 producer)이고, 이 module은 그 증언을
**만드는** 쪽이다. 둘을 나눠 두면 소비자가 생산 절차에 의존하지 않는다.

설계 원칙(승인 범위):

- **기존 체계 재사용**: installation manifest 검증(`verify_installation`)·자식 실행
  (`qualify_cmd.run_child_bounded`)·자식 환경 정책(`local_session.build_child_env`)·원자적 기록
  (`write_attestation`)을 그대로 쓴다. 새 registry·자격 원장·서명 체계를 만들지 않는다.
- **발급기가 직접 확인한다**: CLI에서 "G2=PASS"를 입력받지 않는다. 발급기가 **자기가 suite를
  실행**하고, 실행 전후의 설치 digest가 같은지까지 확인한 뒤에만 기록한다.
- **근거가 아닌 것**: module 존재, source digest 일치, PoC·client 자기 선언, fixture.
- **fail-closed**: 검증 실패·timeout·중단·cleanup 이상·설치 불일치·skip 포함은 전부 거부.
- **원자성**: 기록은 `write_attestation`(tmp→replace + sidecar) 한 번이고, 기록 직후 소비자
  (`capability_attestation.support`)로 재검증한다. 재검증이 실패하면 기록을 지운다 — 중간 상태가
  유효한 증언처럼 남지 않는다.

검증 범위(§ 구분):

- `WSM-G2` production 구현 설치 — 이 설치에 common-LO tuning 경로가 **설치돼 있고 계약대로
  동작**하는가. RF 없이 판정 가능하다.
- `WSM-G3` RF-off software/contract 회귀 — capability 광고·거부·철회와 실행 계약.
- `WSM-G4`(실신호 dual-channel 복호, 4.7 MHz calibration/noise/threshold)와 `WSM-G5`(변경 설치의
  full qualification)는 **여기서 판정하지 않는다**. 소프트웨어 증언으로 대체하지 않는다.

이 증언은 설치 적합성(A)만 뜻한다. runtime readiness(B)나 RF 실행 권한·qualification(C)을
부여하지 않으며, `vdeter qualify` gate를 우회하지 않는다.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path

from apps.standalone import capability_attestation, installation_manifest, qualify_cmd
from apps.standalone.local_session import build_child_env
from apps.standalone.state import REPO_ROOT, StatePaths

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2

#: `WSM-G2` 증거 — production common-LO tuning 경로(적용·readback·복원)와 그 안전 경계를 실제로
#: 실행하는 시험. "module이 있다"가 아니라 "이 설치에서 계약대로 동작한다"를 본다.
#:
#: 마지막 항목은 **실 Stage 7 seam**을 실제로 구동하는 integration 시험이다. 단위·계약 시험만으로는
#: "구현이 설치돼 있다"까지만 말할 수 있고 "설치된 production 경로가 실제로 물린다"를 말할 수 없어
#: 포함한다. 비-Linux에서는 skip되는데, 이 발급기는 skip을 PASS로 세지 않으므로 그런 환경에서는
#: 증언이 발급되지 않는다(의도된 fail-closed — 배포 대상은 Linux다).
G2_TARGETS = (
    "VDETER/tests/unit/test_stage8_wsm_tuning_topology.py",
    "VDETER/tests/unit/test_stage8_rx_scope_faults.py",
    "VDETER/tests/unit/test_stage8_isolated_worker_state.py",
    "VDETER/tests/unit/test_stage8_rx_ready_scheduler.py",
    "VDETER/tests/unit/test_stage8_prehardware_safety.py",
    "VDETER/tests/contract/test_stage8_wsm_structure.py",
    "VDETER/tests/integration/test_stage8_real_stage7_seams.py",
)

#: `WSM-G3` 증거 — RF-off software/contract 회귀. capability 광고 근거·사전검사·WELCOME 협상·
#: 철회와 실행 계약을 고정하는 시험.
G3_TARGETS = (
    "VDETER/tests/contract/test_stage8_capability_attestation.py",
    "VDETER/tests/contract/test_stage8_wsm_provider.py",
    "VDETER/tests/contract/test_stage8_exec_selection.py",
    "VDETER/tests/contract/test_stage8_exec_runner.py",
    "VDETER/tests/unit/test_stage8_messages_negotiation.py",
    "VDETER/tests/unit/test_stage8_collision_lifecycle.py",
    "VDETER/tests/unit/test_stage8_exec_manifest.py",
)

VERIFICATION = (
    ("WSM-G2_production_common_lo_implementation_installed", G2_TARGETS),
    ("WSM-G3_rf_off_software_contract_regression", G3_TARGETS),
)

#: suite 실행 상한. 기존 verification bundle의 unit/contract 상한을 그대로 인용한다.
SUITE_TIMEOUT_S = qualify_cmd.UNIT_CONTRACT_TIMEOUT_S

_COUNT = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)")


class IssueRefused(RuntimeError):
    """증언을 발급할 수 없다. 부분 기록을 남기지 않는다."""


def _counts(summary_text: str) -> dict:
    return {("error" if name.startswith("error") else name): int(value)
            for value, name in _COUNT.findall(summary_text)}


def _suite_ok(log_path: Path, result: dict) -> tuple:
    """`(ok, counts, reason)` — rc만 보지 않는다. skip·xfail을 PASS로 세지 않는다.

    pytest는 **모두 skip이어도 rc=0**이다(Stage 8 핵심 시험은 비-Linux에서 skip된다).
    그래서 수집·통과 건수를 함께 본다: 통과가 1건 이상이고 skip/실패/오류가 0이어야 한다.
    """
    if result.get("timed_out"):
        return False, {}, "suite timed out"
    if result.get("interrupted"):
        return False, {}, "suite interrupted"
    if result.get("cleanup_anomaly"):
        return False, {}, "child process group cleanup anomaly"
    if result.get("rc") != 0:
        return False, {}, f"pytest exit code {result.get('rc')}"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, {}, f"suite log unreadable: {exc}"
    # 요약이 **마지막 줄이라고 가정하지 않는다**: warnings summary 등이 뒤에 붙을 수 있다.
    # 카운트 패턴을 실제로 담은 줄 중 마지막 것을 쓴다. 하나도 없으면 아래에서 거부된다.
    summaries = [line for line in text.splitlines() if _COUNT.search(line)]
    counts = _counts(summaries[-1] if summaries else "")
    if counts.get("passed", 0) < 1:
        return False, counts, "no test actually passed (all skipped/deselected?)"
    for name in ("failed", "error", "skipped", "xfailed", "xpassed"):
        if counts.get(name, 0):
            return False, counts, f"{counts[name]} {name} — not counted as PASS"
    return True, counts, ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_issue(paths: StatePaths, *, capability: str, repo_root: Path | None = None,
                     python_executable: str | None = None, out=print) -> dict:
    """이 설치를 검증하고, 통과할 때만 증언을 발급한다. 반환: 발급 요약."""

    repo_root = Path(repo_root or REPO_ROOT)
    python_executable = python_executable or sys.executable
    if capability not in capability_attestation.REQUIRED_VERIFICATION:
        raise IssueRefused(f"unknown capability: {capability!r}")

    # 1) 설치 기록을 적재하고 **현재 파일과 대조**한다. 기록만 믿지 않는다.
    try:
        installation = installation_manifest.load_installation(paths)
    except installation_manifest.InstallationError as exc:
        raise IssueRefused(f"installation record unusable: {exc}") from exc
    report = installation_manifest.verify_installation(repo_root, installation)
    if not report.get("pass"):
        raise IssueRefused(
            "installation does not match its manifest — "
            + installation_manifest.describe_mismatch(report))
    before = installation_manifest.digests_of(installation)
    out("  installation verified: %s (files %d)"
        % (before["installation_sha256"][:12], installation["file_count"]))

    # 2) 요구 검증 항목을 **직접 실행**한다(입력받지 않는다).
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_root = installation_manifest.installation_dir(paths) / "capability-verification" / stamp
    log_root.mkdir(parents=True, exist_ok=True)   # var/ 아래 — scan 제외라 digest 불변
    env = build_child_env()
    items = {}
    required = capability_attestation.REQUIRED_VERIFICATION[capability]
    for name, targets in VERIFICATION:
        if name not in required:
            continue
        log_path = log_root / f"{name}.log"
        argv = [python_executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                *[str(repo_root / target) for target in targets]]
        out(f"  verifying {name} ({len(targets)} files)…")
        result = qualify_cmd.run_child_bounded(
            argv, cwd=repo_root, env=env, stdout_path=log_path, timeout_s=SUITE_TIMEOUT_S)
        ok, counts, reason = _suite_ok(log_path, result)
        out("    %s  %s" % ("PASS" if ok else "REFUSED", counts or reason))
        if not ok:
            raise IssueRefused(f"{name}: {reason} (log: {log_path})")
        items[name] = {
            "result": "PASS",
            "evidence_sha256": _sha256_file(log_path),
            "note": "%s | targets=%d | %s" % (stamp, len(targets), counts),
        }

    # 3) 검증 중에 트리가 바뀌지 않았는지 확인한다(검증 대상과 결속 대상의 동일성).
    after = installation_manifest.digests_of(installation_manifest.measure(repo_root))
    if after != before:
        raise IssueRefused(
            "installation changed while verifying — refusing to bind an attestation to a "
            "tree that is no longer the one that was verified")

    # 4) 기록 후 **소비자로 즉시 재검증**한다. 실패하면 지운다(부분 유효 상태 금지).
    record = capability_attestation.build_attestation(
        installation, capability=capability, items=items,
        producer="vdeter capability issue (this installation)",
        method="issuer-executed RF-off verification bound to the measured installation digest")
    written = capability_attestation.write_attestation(paths, record)
    supported, reasons = capability_attestation.support(
        paths, installation, capability=capability)
    if not supported:
        path = capability_attestation.attestation_path(paths)
        for stale in (path, path.with_name(path.name + ".sha256")):
            try:
                stale.unlink()
            except OSError:
                pass
        raise IssueRefused("attestation failed its own consumer check: %s" % "; ".join(reasons))
    return {"path": written["path"], "sha256": written["sha256"],
            "installation_sha256": before["installation_sha256"],
            "capability": capability, "items": sorted(items), "log_root": str(log_root)}


#: 이 하위명령이 다루는 capability. 이름의 단일 출처는 `capability_attestation`이며, 아래
#: assert가 둘이 어긋난 채로 동작하는 것을 막는다(조용한 불일치 금지).
CAPABILITY = "observe_dual_physical_channel"
assert CAPABILITY in capability_attestation.REQUIRED_VERIFICATION, (
    "capability name must match capability_attestation.REQUIRED_VERIFICATION")


def main(args, paths: StatePaths, out=print) -> int:
    capability = CAPABILITY
    if getattr(args, "action", "show") == "show":
        try:
            installation = installation_manifest.load_installation(paths)
        except installation_manifest.InstallationError as exc:
            out(f"capability: installation record unusable: {exc}")
            return EXIT_CONFIG
        summary = capability_attestation.describe(
            paths, installation, capability=capability)
        out("capability            : %s" % summary["capability"])
        out("supported             : %s" % summary["supported"])
        for reason in summary["reasons"]:
            out("  reason              : %s" % reason)
        out("attested installation : %s" % (summary["attested_installation_sha256"] or "-"))
        out("producer              : %s" % (summary["producer"] or "-"))
        out("hardware-only items   : %s" % ", ".join(summary["hardware_only_items"]))
        out("note                  : %s" % summary["note"])
        return EXIT_PASS if summary["supported"] else EXIT_FAIL

    out("vdeter capability issue — RF-off verification of THIS installation")
    out("  scope: installation capability only. This does not grant runtime readiness,")
    out("         RF execution authority or qualification, and starts no transmission.")
    try:
        issued = verify_and_issue(paths, capability=capability, out=out)
    except IssueRefused as exc:
        out("refused: %s" % exc)
        return EXIT_FAIL
    out("issued: %s" % issued["path"])
    out("  bound installation : %s" % issued["installation_sha256"][:12])
    out("  record sha256      : %s" % issued["sha256"][:12])
    out("  verified items     : %s" % ", ".join(issued["items"]))
    out("  evidence logs      : %s" % issued["log_root"])
    return EXIT_PASS


__all__ = ("G2_TARGETS", "G3_TARGETS", "VERIFICATION", "SUITE_TIMEOUT_S",
           "IssueRefused", "verify_and_issue", "main")
