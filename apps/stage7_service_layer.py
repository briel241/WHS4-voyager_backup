"""Phase G formal service 실행 계층의 내부 로직 (CLI 아님).

정식 사용자 진입점은 `apps/scenario_runner.py menu --transport stage7
--profile <profile.json>` 하나뿐이고, 그 아래 `apps/stage7_run.py`가 이 모듈의
함수를 호출한다. 이 파일에는 `__main__` 진입점을 두지 않는다.

한 session의 순서 (§3)
    1. profile/reference schema·SHA 검증        (stage7_profile)
    2. qualification-critical manifest 대조      (원격 배포본 실측)
    3. baseline/gain/config 결속 확인
    4. B210 serial/role/USB topology 확인
    5. 경쟁 프로세스·장치 점유 확인
    6. Pi throttled 확인
    7. trust 인증서·개인키 존재/0600/key-cert match 확인
    8~13. coordinator `mass-roc-service` phase (경량 time fit → admission →
          시나리오 1건 → STOP → cleanup·B210 반환)

하나라도 실패하면 RF 실행 전 fail-closed하고, 이미 시작했으면 안전 cleanup 후
FAIL로 끝낸다. direct/software backend로 fallback하지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 세션 간 상태를 공유하지 않는다 — 매 세션 새 run-id.
SESSION_RUN_ID_PREFIX = "svc"

#: Stage 7 hardware/session ownership과 충돌하는 실제 process argv 패턴.
#: B210 backend는 multiprocessing ``spawn`` child라 모듈 파일명이 argv에
#: 나타나지 않을 수 있으므로 ``spawn_main`` 계열도 보수적으로 차단한다.
_COMPETING_PROCESS_PATTERN = (
    "stage7_coordinator_main|stage7_endpoint_launcher|"
    "stage7_mass_roc_controller|multiprocessing\\.spawn|spawn_main|"
    "vdeter-b210-gr-uhd|uhd_|usrp"
)

#: 대화형 `a) 전체 실행`이 차례로 도는 독립 child 계획.
DEFAULT_BATCH_PLAN = (
    ("SC-VDE-005", "normal"),
    ("SC-VDE-006", "normal"),
    ("SC-VDE-001", "normal"),
    ("SC-VDE-001", "forged"),
)


class ServiceSessionError(RuntimeError):
    """service session이 RF 접근 전(또는 안전 cleanup 후) 실패했다."""


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)


def _ssh(profile: dict, script: str, *, timeout: float = 300.0,
         check: bool = True, runner=subprocess.run):
    """Pi에서 명령을 실행한다 (BatchMode). 비밀정보는 출력하지 않는다.

    원격 파일에는 UTF-8 한글 주석이 있다. Windows 기본 로케일(cp949)로 디코드하면
    깨지므로 인코딩을 명시한다.
    """
    command = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               profile["pi"]["host"], script]
    # stdin을 반드시 끊는다. ssh는 원격으로 넘길 stdin을 읽으므로, 지정하지
    # 않으면 부모의 stdin을 상속받아 **대화형 메뉴의 입력을 통째로 삼킨다**.
    # phase-g-service-20260831-111702 실측: bring-up 22분 동안의 ssh 호출들이
    # 입력을 다 먹어, SESSION READY 직후 메뉴가 첫 input()에서 EOF를 만나
    # workload를 하나도 못 돌리고 종료했다. -n과 DEVNULL을 둘 다 건다.
    done = runner(command, capture_output=True, text=True, timeout=timeout,
                  encoding="utf-8", errors="replace",
                  stdin=subprocess.DEVNULL)
    if check and done.returncode != 0:
        raise ServiceSessionError(
            f"원격 명령 실패(rc={done.returncode}): "
            f"{(done.stdout or '')[-300:]}{(done.stderr or '')[-300:]}")
    return done


def remote_deploy_root(profile: dict) -> str:
    return "$HOME/" + profile["pi"]["deploy_relpath"]


def _verify_function_bound(profile: dict, root: str, runner) -> dict:
    """orchestration 분기가 additive로 늘어나는 파일을 **함수 단위**로 결속 확인.

    전체 파일 해시는 달라질 수 있다(신규 phase 추가). 대신 배포본 실물을 가져와
    qualification 경로 함수의 AST/source 해시와 RR phase dispatch가 Phase F와
    같은지 확인한다. 하나라도 다르면 RF 접근을 거부한다.
    """
    import ast
    import hashlib

    bound = (profile["_reference"]["qualification_critical_manifest"]
             .get("function_bound_files") or {})
    outcome = {}
    for relpath, spec in sorted(bound.items()):
        done = _ssh(profile, f"cat {root}/{relpath}", timeout=180.0, runner=runner)
        source = done.stdout
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ServiceSessionError(f"{relpath} 파싱 실패: {exc}") from exc

        deployed_file_sha256 = hashlib.sha256(
            source.encode("utf-8")).hexdigest()
        expected_file_sha256 = spec.get("phase_g_file_sha256")
        if not expected_file_sha256:
            raise ServiceSessionError(
                f"{relpath} reference에 phase_g_file_sha256 결속이 없습니다")
        if deployed_file_sha256 != expected_file_sha256:
            raise ServiceSessionError(
                f"{relpath}의 배포본 전체 파일이 Phase G reference와 다릅니다 "
                "— RF 접근 거부")

        actual = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                actual[node.name] = {
                    "ast_sha256": hashlib.sha256(
                        ast.dump(node).encode("utf-8")).hexdigest(),
                    "source_sha256": hashlib.sha256(
                        ast.unparse(node).encode("utf-8")).hexdigest(),
                }
        expected = spec.get("qualified_functions") or {}
        if not expected:
            raise ServiceSessionError(
                f"{relpath} reference에 qualified_functions 결속이 없습니다")
        changed = sorted(name for name, value in expected.items()
                          if actual.get(name) != value)
        missing = sorted(name for name in expected if name not in actual)

        expected_dispatch = spec.get("rr_phase_dispatch")
        if not isinstance(expected_dispatch, dict) or not expected_dispatch:
            raise ServiceSessionError(
                f"{relpath} reference에 rr_phase_dispatch 결속이 없습니다")
        phase_names = {
            "p0", "hold", "p1", "p2", "gaincal", "rr-p0", "rr-p1",
            "rr-p2-17", "rr-p2-19", "rr-p3-fault", "rr-p3-soak-17",
            "rr-p3-soak-19", "mass-roc-service",
        }
        actual_dispatch = {}
        main = next((node for node in tree.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "main"), None)
        if main is None:
            raise ServiceSessionError(f"{relpath}에 main 함수가 없습니다")
        for node in ast.walk(main):
            if not isinstance(node, ast.If):
                continue
            phases = [constant.value for constant in ast.walk(node.test)
                      if isinstance(constant, ast.Constant)
                      and isinstance(constant.value, str)
                      and constant.value in phase_names]
            called = sorted({
                call.func.id
                for statement in node.body
                for call in ast.walk(statement)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id.startswith("run_")
            })
            for phase in phases:
                if called:
                    previous = actual_dispatch.get(phase, [])
                    actual_dispatch[phase] = sorted(set(previous + called))
        dispatch_mismatch = {
            phase: {"expected": expected, "actual": actual_dispatch.get(phase)}
            for phase, expected in expected_dispatch.items()
            if actual_dispatch.get(phase) != expected
        }
        # reference의 rr_phase_dispatch는 qualified RR 분기 golden trace다.
        # 이후 추가된 service 분기는 전체-file SHA로 별도 결속되므로 여기서는
        # 기존 phase mapping의 변경/누락만 거부하고 additive key는 기록만 한다.
        dispatch_additions = {
            phase: called for phase, called in actual_dispatch.items()
            if phase not in expected_dispatch
        }
        dispatch_changed = bool(dispatch_mismatch)
        outcome[relpath] = {
            "qualified_functions_checked": len(expected),
            "changed": changed, "missing": missing,
            "dispatch_changed": dispatch_changed,
            "dispatch_mismatch": dispatch_mismatch,
            "dispatch_additions": dispatch_additions,
            "deployed_file_sha256": deployed_file_sha256,
            "phase_f_file_sha256": spec.get("phase_f_file_sha256"),
            "phase_g_file_sha256": expected_file_sha256,
            "pass": not changed and not missing and not dispatch_changed,
        }
        if changed or missing or dispatch_changed:
            raise ServiceSessionError(
                f"{relpath}의 qualification 경로가 변경됐다 — RF 접근 거부 "
                f"(changed={changed}, missing={missing}, "
                f"dispatch_mismatch={dispatch_mismatch})")
    return outcome


def verify_remote_environment(profile: dict, *, runner=subprocess.run) -> dict:
    """§3 2~7단계. 실패하면 ServiceSessionError (RF 접근 전 차단)."""
    reference = profile["_reference"]
    manifest = reference["qualification_critical_manifest"]
    # v2: 전체 파일 해시로 결속하는 strict 목록과, 함수 단위로 결속하는
    # function-bound 목록을 분리한다. (v1 호환: files 키만 있으면 전부 strict)
    critical = manifest.get("strict_files") or manifest.get("files") or {}
    root = remote_deploy_root(profile)
    report = {}

    listing = "\n".join(sorted(critical))
    marker = "STAGE7_CRITICAL_LIST_END"
    script = (
        "cd " + root + " && while IFS= read -r f; do "
        'if [ -f "$f" ]; then sha256sum "$f"; else echo "MISSING $f"; fi; '
        "done <<'" + marker + "'\n" + listing + "\n" + marker + "\n"
    )
    done = _ssh(profile, script, timeout=900.0, runner=runner)
    remote, missing = {}, []
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith("MISSING "):
            missing.append(line[8:])
        elif line:
            digest, path = line.split(None, 1)
            remote[path.strip()] = digest
    mismatch = sorted(p for p, h in critical.items()
                      if remote.get(p) is not None and remote[p] != h)
    absent = sorted(p for p in critical if p not in remote and p not in missing)
    report["critical_manifest"] = {
        "checked": len(critical), "missing": missing + absent,
        "mismatch": mismatch, "pass": not (missing or absent or mismatch),
    }
    if missing or absent or mismatch:
        raise ServiceSessionError(
            "qualification-critical 파일 불일치 — RF 접근 거부 "
            f"(missing={(missing + absent)[:3]}, mismatch={mismatch[:3]})")

    # orchestration 분기가 추가되는 파일은 함수 단위로 결속을 확인한다.
    report["function_bound"] = _verify_function_bound(profile, root, runner)

    endpoints = profile["endpoints"]
    serials = " ".join(sorted(e["serial"] for e in endpoints.values()))
    trust = profile["trust"]
    script = (
        'echo "##USB"\n'
        'for d in /sys/bus/usb/devices/*/; do s="$d/serial"; [ -f "$s" ] || continue; '
        'ser=$(cat "$s" 2>/dev/null); for want in ' + serials + '; do '
        '[ "$ser" = "$want" ] && echo "$(basename $d) $ser $(cat $d/speed 2>/dev/null)"; '
        'done; done\n'
        'echo "##PROC"\n'
        'pgrep -af "' + _COMPETING_PROCESS_PATTERN + '" '
        '| grep -v pgrep || echo none\n'
        'echo "##THROTTLE"\nvcgencmd get_throttled\n'
        'echo "##TRUST"\n'
        'stat -c "%n %a" ' + root + "/" + trust["certificate_path"] + " "
        + root + "/" + trust["trust_store_path"] + " "
        + root + "/" + trust["private_key_path"] + ' 2>&1\n'
        'echo "##KEYCERT"\n'
        'K=$(openssl pkey -in ' + root + "/" + trust["private_key_path"]
        + ' -pubout -outform DER 2>/dev/null | sha256sum | cut -d" " -f1)\n'
        'C=$(openssl x509 -in ' + root + "/" + trust["certificate_path"]
        + ' -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER '
        '2>/dev/null | sha256sum | cut -d" " -f1)\n'
        'if [ -n "$K" ] && [ "$K" = "$C" ]; then echo match=true; '
        'else echo match=false; fi\n'
    )
    done = _ssh(profile, script, timeout=180.0, runner=runner)
    sections, current = {}, None
    for line in done.stdout.splitlines():
        if line.startswith("##"):
            current = line[2:]
            sections[current] = []
        elif current is not None:
            sections[current].append(line.strip())

    usb = {}
    for line in sections.get("USB", []):
        parts = line.split()
        if len(parts) >= 3:
            usb[parts[1]] = {"usb_path": parts[0], "speed_mbps": int(parts[2])}
    topology_ok = all(
        usb.get(info["serial"], {}).get("usb_path") == info["usb_path"]
        and usb.get(info["serial"], {}).get("speed_mbps") == info["speed_mbps"]
        for info in endpoints.values()
    )
    report["usb_topology"] = {"observed": usb, "pass": topology_ok}
    if not topology_ok:
        raise ServiceSessionError(f"B210 serial/USB topology 불일치: {usb}")

    procs = [p for p in sections.get("PROC", []) if p and p != "none"]
    report["competing_processes"] = {"observed": procs, "pass": not procs}
    if procs:
        raise ServiceSessionError(f"경쟁 프로세스/장치 점유: {procs[:3]}")

    throttled = (sections.get("THROTTLE") or [""])[0]
    report["throttled"] = {"value": throttled,
                           "pass": throttled == "throttled=0x0"}
    if throttled != "throttled=0x0":
        raise ServiceSessionError(f"Pi throttled: {throttled}")

    modes = {}
    for line in sections.get("TRUST", []):
        parts = line.split()
        if len(parts) == 2:
            modes[os.path.basename(parts[0])] = parts[1]
    key_mode = modes.get(os.path.basename(trust["private_key_path"]))
    match = (sections.get("KEYCERT") or [""])[0] == "match=true"
    report["trust"] = {
        "private_key_present": key_mode is not None,
        "private_key_mode": key_mode,
        "key_cert_match": match,
        "pass": key_mode == trust["private_key_required_mode"] and match,
    }
    if not report["trust"]["pass"]:
        raise ServiceSessionError(
            f"trust 조건 불일치 (mode={key_mode}, key_cert_match={match})")
    return report


def prepare_service_config(profile: dict, run_id: str, logs: list, *,
                           runner=subprocess.run) -> dict:
    """공식 prepare 도구로 fresh resolved config를 만든다 (재사용 금지)."""
    preparation = profile["config_preparation"]
    command = [
        sys.executable,
        os.path.join(_ROOT, "VDETER", "tools", "prepare_stage7_run_configs.py"),
        "--run-id", run_id,
        "--deploy-run-id", profile["pi"]["deploy_run_id"],
        "--host", profile["pi"]["host"],
        "--external-power-asset-mass-b",
        preparation["external_power_asset_mass_b"],
        "--run-mode", preparation["run_mode"],
        "--rr-phase", preparation["rr_phase"],
        "--gain-artifact", profile["gain"]["artifact_input_path"],
    ]
    # 이 도구도 내부에서 ssh를 돈다 — stdin을 끊지 않으면 대화형 메뉴 입력을
    # 삼킨다 (`_ssh` 주석 참조).
    done = runner(command, capture_output=True, text=True, cwd=_ROOT, timeout=900,
                  encoding="utf-8", errors="replace",
                  stdin=subprocess.DEVNULL)
    logs.append(("prepare-config", (done.stdout or "") + (done.stderr or "")))
    if done.returncode != 0:
        raise ServiceSessionError(
            f"resolved config 생성 실패(rc={done.returncode}): "
            f"{((done.stdout or '') + (done.stderr or ''))[-300:]}")

    local_dir = os.path.join(_ROOT, "VDETER", "results", "runs", run_id,
                             "resolved-configs")
    with open(os.path.join(local_dir, "git_state.json"), "rb") as handle:
        metadata = json.loads(handle.read().decode("utf-8"))
    if metadata.get("run_id") != run_id:
        raise ServiceSessionError("생성된 config의 run-id 결속 불일치")
    if metadata.get("git_dirty") is not False:
        raise ServiceSessionError("config가 dirty worktree로 결속됐습니다")
    if metadata.get("baseline_sha256") != profile["baseline_sha256"]:
        raise ServiceSessionError("config baseline SHA가 profile과 다릅니다")
    return {"local_dir": local_dir, "metadata": metadata}


def build_coordinator_script(profile: dict, run_id: str, scenario_id: str,
                             certificate_case: str) -> str:
    """Pi에서 실행할 coordinator 명령 (service-only phase)."""
    root = remote_deploy_root(profile)
    return (
        "set -e\n"
        "cd " + root + "/VDETER\n"
        "mkdir -p " + root + "/runs/" + run_id + "\n"
        "PYTHONPYCACHEPREFIX=/tmp/stage7-service-pyc "
        + profile["pi"]["venv_python"] + " apps/stage7_coordinator_main.py"
        + " --run-id " + run_id
        + " --phase mass-roc-service"
        + " --run-root " + root + "/runs/" + run_id
        + " --configs-dir " + root + "/run-configs/" + run_id
        + " --gain-artifact " + root + "/"
        + profile["gain"]["remote_artifact_input_relpath"]
        + " --baseline-sha256 " + profile["baseline_sha256"]
        # --workload는 넘기지 않는다. mass-roc-service phase 자체가 MASS/ROC
        # workload이며, 기존 --workload mass-roc 게이트는 rr-p2-17/19 전용이라
        # 함께 주면 충돌한다 (기존 게이트는 그대로 둔다).
        + " --scenario-id " + scenario_id
        + " --certificate-case " + certificate_case + "\n"
    )


def run_service_session(profile: dict, *, scenario_id: str,
                        certificate_case: str = "normal",
                        run_id: str | None = None,
                        runner=subprocess.run,
                        clock=time.time) -> dict:
    """독립 service session 하나를 끝까지 조율한다."""
    from apps import stage7_profile

    started = clock()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started))
    tag = scenario_id.lower().replace("sc-vde-", "sc")
    if scenario_id == "SC-VDE-001":
        tag += "-" + certificate_case
    run_id = run_id or f"{SESSION_RUN_ID_PREFIX}-{tag}-{stamp}"

    session_dir = os.path.join(_ROOT, profile["evidence_root"], run_id)
    if os.path.exists(session_dir):
        raise ServiceSessionError(f"session 디렉터리 중복: {session_dir}")
    os.makedirs(session_dir)

    logs, marks = [], {}

    def mark(name):
        marks[name] = round(clock() - started, 1)

    result = {
        "artifact_type": "STAGE7_SERVICE_SESSION_RESULT_V1",
        "judgement_scope": "OPERATIONAL_SERVICE",
        "not_a_qualification": (
            "RF qualification이 아니다 — CFO/adaptive gain/raw-PHY 미실행. "
            "자격은 Phase F qualification reference가 담보한다."),
        "run_id": run_id,
        "scenario_id": scenario_id,
        "certificate_case": certificate_case if scenario_id == "SC-VDE-001" else None,
        "profile_id": profile["profile_id"],
        "qualification_run_id": profile["_reference"]["qualification"]["run_id"],
        "reference_sha256": profile["_reference"]["_reference_sha256"],
        "status": "FAIL",
        "error": None,
        "fallback_used": None,
    }
    try:
        result["local_validation"] = stage7_profile.validate_local_bindings(profile)
        mark("local_validation")

        result["remote_preflight"] = verify_remote_environment(
            profile, runner=runner)
        mark("remote_preflight")

        prepared = prepare_service_config(profile, run_id, logs, runner=runner)
        result["config"] = {
            "local_dir": os.path.relpath(prepared["local_dir"], _ROOT),
            "resolved_run_id": prepared["metadata"]["run_id"],
            "git_dirty": prepared["metadata"]["git_dirty"],
            "git_commit": prepared["metadata"].get("git_commit"),
        }
        mark("config_prepared")

        script = build_coordinator_script(
            profile, run_id, scenario_id, certificate_case)
        done = _ssh(profile, script,
                    timeout=float(profile.get("session_timeout_s", 3600)),
                    check=False, runner=runner)
        logs.append(("coordinator", (done.stdout or "") + (done.stderr or "")))
        result["coordinator_rc"] = done.returncode
        mark("coordinator")

        runner(["scp", "-q", "-r",
                profile["pi"]["host"] + ":" + profile["pi"]["deploy_relpath"]
                + "/runs/" + run_id,
                os.path.join(session_dir, "remote-run")],
               capture_output=True, text=True, timeout=900,
               encoding="utf-8", errors="replace",
               stdin=subprocess.DEVNULL)
        service_path = os.path.join(
            session_dir, "remote-run", "evidence", "operational_service",
            "service_result.json")
        service_result = None
        if os.path.isfile(service_path):
            with open(service_path, "rb") as handle:
                service_result = json.loads(handle.read().decode("utf-8"))
        result["service_result"] = service_result
        mark("evidence_collected")

        scenario_status = ((service_result or {}).get("scenario") or {}).get("status")
        issued = (service_result or {}).get("qualification_commands_issued") or []
        result["qualification_commands_issued"] = issued
        result["status"] = "PASS" if (
            done.returncode == 0
            and (service_result or {}).get("status") == "PASS"
            and scenario_status == "PASS"
            and not issued
        ) else "FAIL"
        if result["status"] != "PASS":
            result["error"] = (
                f"coordinator rc={done.returncode}, "
                f"service={(service_result or {}).get('status')}, "
                f"scenario={scenario_status}, qualification_cmds={issued}")
    except Exception as exc:  # noqa: BLE001 - 사유를 evidence에 보존
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        mark("total")
        result["phase_elapsed_s"] = marks
        for name, text in logs:
            with open(os.path.join(session_dir, name + ".log"), "w",
                      encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        _write_json(os.path.join(session_dir, "session-result.json"), result)
        result["evidence_dir"] = os.path.relpath(session_dir, _ROOT)
    return result


def run_service_batch(profile: dict, plan=None, *, session_fn=None) -> dict:
    """대화형 `a` — 각 시나리오를 **독립 child session**으로 차례 실행한다.

    공유 session(`scenario_id=all`)을 절대 호출하지 않는다. child마다 별도
    run-id·별도 resolved config·별도 endpoint START/STOP을 갖는다.
    """
    plan = tuple(plan or DEFAULT_BATCH_PLAN)
    session_fn = session_fn or run_service_session
    children = []
    for scenario_id, certificate_case in plan:
        child = session_fn(profile, scenario_id=scenario_id,
                           certificate_case=certificate_case)
        children.append(child)
        if child.get("status") != "PASS":
            break  # 실패 시 다음 child를 시작하지 않는다 (각자 cleanup 수행)
    return {
        "artifact_type": "STAGE7_SERVICE_BATCH_RESULT_V1",
        "judgement_scope": "OPERATIONAL_SERVICE",
        "shared_session_used": False,
        "plan": [list(item) for item in plan],
        "children": children,
        "child_run_ids": [c.get("run_id") for c in children],
        "status": "PASS" if (len(children) == len(plan)
                             and all(c.get("status") == "PASS" for c in children))
                  else "FAIL",
    }
