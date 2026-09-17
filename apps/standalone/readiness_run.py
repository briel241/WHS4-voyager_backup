"""readiness preparation orchestration 재료 — `run_cmd`(writer)·`status_cmd`(reader) 공용 (spec §5–§7, §9).

여기에는 **정책 상수와 순수 함수**만 있다: stable binding 조립, calibration source 선택, warm/cold
machine-readable plan, workload admission reserve, 종료 판정(승격/inhibit/summary status), candidate
evidence 수집. 장치 open·RF·state 쓰기는 하지 않는다(state 쓰기는 `readiness.ReadinessWriter`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

from apps.standalone import hardware_profiles, installation_manifest, local_config
from apps.standalone import readiness, readiness_classifier, readiness_closure, readiness_probe

# ------------------------------------------------------------ 정책 상수 (명세 값, 변경 금지)

#: warm bring-up 상한 — 기존 local-session 값 재사용 (2700 s). coordinator absolute 7200과 별개.
WARM_BRINGUP_TIMEOUT_S = 2700.0
#: cold 전용 lifecycle deadline — CALIBRATION_PREPARATION 시작부터 LINK17_PROBE 완료까지.
READINESS_COLD_PREPARATION_S = 3900.0
SESSION_ABSOLUTE_DEADLINE_S = 7200.0
WORKLOAD_TIMEOUT_S = 900.0
CLOSE_TIMEOUT_S = 600.0
CHILD_TERM_S = 30.0
CHILD_KILL_S = 10.0
SAFETY_MARGIN_S = 60.0
#: §5.1.1: READY 발급과 매 workload 제출 직전 남은 absolute budget 최소.
WORKLOAD_ADMISSION_RESERVE_S = (WORKLOAD_TIMEOUT_S + CLOSE_TIMEOUT_S + CHILD_TERM_S
                                + CHILD_KILL_S + SAFETY_MARGIN_S)   # 1600
assert WORKLOAD_ADMISSION_RESERVE_S == 1600.0

SAFETY_POLICY_ID = "VDETER_READINESS_SAFETY_V1"
HOST_TIME_POLICY_ID = "STAGE7_PI_HOST_AFFINE_TIME_V1/RESTART_SERIES_V1"
RF_TIME_POLICY_ID = "STAGE7_RF_TIME_C1C6_SEQUENTIAL_75S_10X6P8S_V1"
CALIBRATION_POLICY_ID = "STAGE7_RR_P1_ADAPTIVE_SEARCH_AND_SEAL_V1"
BOOT_CHECK_POLICY_ID = "VDETER_READINESS_BOOT_CHECK_V1"
CLOSURE_POLICY_ID = readiness_closure.READINESS_CLOSURE_POLICY_ID
CLASSIFIER_POLICY_ID = readiness_classifier.CLASSIFIER_POLICY_ID
PROBE_POLICY_ID = readiness_probe.PROBE_POLICY_ID

POLICY_IDS = {
    "classifier": CLASSIFIER_POLICY_ID,
    "host_time": HOST_TIME_POLICY_ID,
    "rf_time": RF_TIME_POLICY_ID,
    "probe": PROBE_POLICY_ID,
    "safety": SAFETY_POLICY_ID,
    "closure": CLOSURE_POLICY_ID,
    "boot_check": BOOT_CHECK_POLICY_ID,
    "calibration": CALIBRATION_POLICY_ID,
}

SAFETY_POLICY = {
    "policy_id": SAFETY_POLICY_ID,
    "warm_bringup_timeout_s": int(WARM_BRINGUP_TIMEOUT_S),
    "cold_preparation_timeout_s": int(READINESS_COLD_PREPARATION_S),
    "session_absolute_deadline_s": int(SESSION_ABSOLUTE_DEADLINE_S),
    "workload_timeout_s": int(WORKLOAD_TIMEOUT_S),
    "close_timeout_s": int(CLOSE_TIMEOUT_S),
    "child_term_s": int(CHILD_TERM_S),
    "child_kill_s": int(CHILD_KILL_S),
    "safety_margin_s": int(SAFETY_MARGIN_S),
    "workload_admission_reserve_s": int(WORKLOAD_ADMISSION_RESERVE_S),
    "implicit_retry_warm": 0,
    "implicit_retry_cold": 0,
    "legacy_host_time_single_retry": "not invoked on the readiness path",
    "cross_command_session_handoff": False,
    "abort_conditions": [
        "bring-up timeout (warm 2700 s / cold 3900 s) -> no new TX, bounded close",
        "phase progress stall / signal / interrupt -> bounded close, no promotion",
        "TX outcome or ownership uncertain -> inhibit, never PASS synthesis",
        "readiness state unreadable/corrupt/orphan-invalid -> refuse before RF",
    ],
}

#: TDMA frame(2.4 s)당 ROC BBSC TBB fragment 1 — production control RF producer (M0 §2.2).
TDMA_FRAME_S = 2.4
CONTROL_BURSTS_PER_TRANSFER = 3 + 3 + 4 + 4   # RAC + ASC/DC + data(1+3) + ACK(1+3)


# ------------------------------------------------------------ stable binding

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_machine_id(path: str = "/etc/machine-id"):
    """stable machine identity — `/etc/machine-id` (대상 Ubuntu). 없으면 dbus machine-id.
    POSIX가 아닌 개발 환경(Windows unit test)에서만 hostname 기반 값을 쓴다(RF 실행 경로 아님)."""
    value = local_config.read_host_machine_id(path)
    if value:
        return value
    value = local_config.read_host_machine_id("/var/lib/dbus/machine-id")
    if value:
        return value
    if os.name != "posix":
        import platform
        return "dev-host:%s" % platform.node()
    return None


def read_boot_id(path: str = "/proc/sys/kernel/random/boot_id"):
    try:
        return Path(path).read_text(encoding="ascii", errors="replace").strip() or None
    except OSError:
        return None


def rf_config_binding(repo_root, hardware: dict) -> dict:
    """endpoint template·profile·system.toml digest + RF center/sample rate/channel/lane (§8.1 wiring/RF)."""
    repo_root = Path(repo_root)
    definition = hardware_profiles.definition_of(hardware)
    template_dir = repo_root / "VDETER" / "configs" / "b210_system"
    files = {
        "VDETER/configs/b210_system/system.toml": template_dir / "system.toml",
        "VDETER/configs/b210_system/profiles/data-profile.toml":
            template_dir / "profiles" / "data-profile.toml",
    }
    endpoints = {}
    for endpoint in local_config.ENDPOINTS:
        rel = "VDETER/configs/b210_system/%s/%s.toml" % (definition.endpoint_template_subdir, endpoint)
        files[rel] = template_dir / definition.endpoint_template_subdir / (endpoint + ".toml")
    digest_map = {}
    for rel, path in sorted(files.items()):
        digest_map[rel] = _sha(path.read_bytes())
    for endpoint in local_config.ENDPOINTS:
        path = template_dir / definition.endpoint_template_subdir / (endpoint + ".toml")
        with open(path, "rb") as handle:
            template = tomllib.load(handle)
        channels = {}
        for channel in template.get("radio", {}).get("channels", []):
            channels[channel["name"]] = {
                "center_frequency_hz": int(channel["center_frequency_hz"]),
                "sample_rate_hz": int(channel["sample_rate_hz"]),
                "tx_enabled": bool(channel["tx_enabled"]),
                "rx_enabled": bool(channel["rx_enabled"]),
                "waveform_bandwidth_hz": int(channel["waveform_bandwidth_hz"]),
            }
        mac = template.get("mac", {})
        stage7 = template.get("stage7", {})
        endpoints[endpoint.upper()] = {
            "channels": channels,
            "tx_channel": mac.get("tx_channel"), "rx_channel": mac.get("rx_channel"),
            "link_id": int(mac.get("link_id", 0)),
            "lane_map_profile": stage7.get("lane_map_profile"),
            "frequency_translation_hz": int(stage7.get("frequency_translation_hz", 0)),
            "lower_rf_tune_hz": int(stage7.get("lower_rf_tune_hz", 0)),
            "phy_sample_rate_hz": int(template.get("phy", {}).get("sample_rate_hz", 0)),
        }
    return {
        "template_digest_sha256": _sha(json.dumps(digest_map, sort_keys=True,
                                                  separators=(",", ":")).encode("utf-8")),
        "template_subdir": definition.endpoint_template_subdir,
        "endpoints": endpoints,
    }


def hardware_binding(hardware: dict, preflight: dict) -> dict:
    definition = hardware_profiles.definition_of(hardware)
    observed = ((preflight.get("checks") or {}).get("b210") or {}).get("observed_by_role") or {}
    endpoints = {}
    for role in hardware_profiles.ROLES:
        seen = observed.get(role) or {}
        endpoints[role] = {
            "serial": ((hardware.get("endpoints") or {}).get(role) or {}).get("serial"),
            "speed_class": seen.get("speed_class"),
            "usb_transport": definition.usb_transport[role],
        }
    return {
        "endpoints": endpoints,
        "usb_topology": (hardware_profiles.critical_topology(hardware)
                         if hardware_profiles.is_pi_powered_hub(hardware) else None),
    }


def platform_binding(hardware: dict, machine_id) -> dict:
    return {
        "platform_profile": hardware.get("platform_profile"),
        "host_profile": hardware_profiles.profile_of(hardware),
        "machine_id_sha256": _sha(str(machine_id).encode("utf-8")) if machine_id else None,
        "boot_check_policy_id": BOOT_CHECK_POLICY_ID,
    }


def installation_binding(record: dict) -> dict:
    digests = installation_manifest.digests_of(record)
    return {
        "rf_manifest_sha256": digests["rf_manifest_sha256"],
        "operation_manifest_sha256": digests["operation_manifest_sha256"],
        "readiness_closure_sha256": readiness_closure.closure_sha256(
            readiness_closure.closure_files(record)),
        "rf_critical_symbols_sha256": readiness_closure.critical_symbols_sha256(record),
    }


def build_binding(*, installation_record: dict, hardware: dict, preflight: dict,
                  identity_fingerprint: str, machine_id, repo_root, calibration: dict) -> dict:
    binding = {
        "installation": installation_binding(installation_record),
        "identity_fingerprint": identity_fingerprint,
        "platform": platform_binding(hardware, machine_id),
        "hardware": hardware_binding(hardware, preflight),
        "wiring": {"profile": (hardware.get("wiring") or {}).get("profile"),
                   "approved": (hardware.get("wiring") or {}).get("approved") is True},
        "rf_config": rf_config_binding(repo_root, hardware),
        "calibration": dict(calibration),
        "policy_ids": dict(POLICY_IDS),
        "probe_plan_sha256": readiness_probe.probe_plan_sha256(),
    }
    errors = readiness.validate_binding(binding)
    if errors:
        raise readiness.ReadinessError("stable binding invalid: " + "; ".join(errors))
    return binding


def binding_without_calibration(binding: dict) -> dict:
    return {k: v for k, v in binding.items() if k != "calibration"}


# ------------------------------------------------------------ calibration source

def calibration_from_reference(reference: dict) -> dict:
    gain = (reference.get("standalone_critical") or {}).get("gain") or {}
    return {
        "source": "qualification_reference:%s" % reference.get("_reference_sha256"),
        "artifact_sha256": gain.get("artifact_sha256"),
        "artifact_input_sha256": gain.get("artifact_input_sha256"),
        "endpoint_common_rx_gain_db": dict(gain.get("endpoint_common_rx_gain_db") or {}),
        "calibration_policy_id": CALIBRATION_POLICY_ID,
    }


def calibration_from_readiness(record: dict) -> dict:
    calibration = dict((record.get("binding") or {}).get("calibration") or {})
    candidate = record.get("calibration_candidate")
    if candidate and candidate.get("artifact_sha256"):
        # cold run이 봉인한 candidate가 이 release 안에 포함됐다 — 이후 run의 applicable calibration.
        return {
            "source": "readiness_calibration:%s" % record.get("readiness_id"),
            "artifact_sha256": candidate["artifact_sha256"],
            "artifact_input_sha256": candidate["artifact_input_sha256"],
            "endpoint_common_rx_gain_db": dict(candidate["endpoint_common_rx_gain_db"]),
            "calibration_policy_id": CALIBRATION_POLICY_ID,
        }
    return calibration


def reference_calibration_applicable(reference: dict, *, hardware: dict, preflight: dict,
                                     environment_manifest: dict,
                                     installation_record: dict) -> dict:
    """stale qualification의 gain을 calibration source로 쓸 수 있는가 — `diag authorize`와 같은 결속
    동일성(platform/host/wiring profile/endpoints/usb_topology/library 4종/RF critical symbols).
    installation digest·identity는 비교하지 않는다(교정값은 장비·배선·RF 결속이지 코드가 아니다)."""
    src = reference.get("standalone_critical") or {}
    observed = ((preflight.get("checks") or {}).get("b210") or {}).get("observed_by_role") or {}
    endpoints = {role: {"serial": info.get("serial"), "speed_class": info.get("speed_class")}
                 for role, info in sorted(observed.items()) if info}
    differences = []

    def same(name, a, b):
        if a != b:
            differences.append("%s: source=%r current=%r" % (name, a, b))

    same("platform_profile", src.get("platform_profile"), hardware.get("platform_profile"))
    same("host_profile", src.get("host_profile") or hardware_profiles.LEGACY_PI_PROFILE,
         hardware_profiles.profile_of(hardware))
    same("wiring_profile", (src.get("wiring") or {}).get("profile"),
         (hardware.get("wiring") or {}).get("profile"))
    same("endpoints", src.get("endpoints"), endpoints)
    same("usb_topology", src.get("usb_topology"),
         hardware_profiles.critical_topology(hardware)
         if hardware_profiles.is_pi_powered_hub(hardware) else None)
    same("library_versions", src.get("library_versions"),
         {name: environment_manifest.get("%s_version" % name)
          for name in local_config.CRITICAL_LIBRARIES})
    same("rf_critical_symbols", (reference.get("installation") or {}).get("rf_critical_symbols"),
         installation_record.get("rf_critical_symbols"))
    gain = src.get("gain") or {}
    for key in ("endpoint_common_rx_gain_db", "artifact_sha256", "artifact_input_sha256"):
        if not gain.get(key):
            differences.append("gain.%s missing in the reference" % key)
    return {"applicable": not differences, "differences": differences}


def readiness_calibration_applicable(record: dict, current_partial_binding: dict) -> dict:
    """active readiness release의 calibration을 재사용할 수 있는가 — calibration을 제외한 stable
    binding 그룹 중 장비·배선·RF·platform·RF critical symbols가 같아야 한다."""
    recorded = (record or {}).get("binding") or {}
    differences = []
    for group in ("platform", "hardware", "wiring", "rf_config"):
        for reason in readiness.binding_differences(recorded.get(group), current_partial_binding.get(group)):
            differences.append(group + reason[len("binding"):])
    if ((recorded.get("installation") or {}).get("rf_critical_symbols_sha256")
            != (current_partial_binding.get("installation") or {}).get("rf_critical_symbols_sha256")):
        differences.append("installation.rf_critical_symbols_sha256 differs")
    calibration = calibration_from_readiness(record)
    if not calibration.get("artifact_sha256"):
        differences.append("release carries no calibration artifact")
    return {"applicable": not differences, "differences": differences, "calibration": calibration}


def select_calibration_source(*, reference, qualification_applicable: bool,
                              readiness_snapshot: dict, hardware: dict, preflight: dict,
                              environment_manifest: dict, installation_record: dict,
                              partial_binding: dict) -> dict:
    """warm(applicable artifact) / cold(artifact 없음·stale) 결정 (§5.1). 임의 gain 금지."""
    if reference is not None and qualification_applicable:
        return {"mode": "warm", "authority": "qualification", "reason": "active qualification applicable",
                "calibration": calibration_from_reference(reference)}
    if reference is not None and not (reference.get("not_a_qualification")):
        check = reference_calibration_applicable(
            reference, hardware=hardware, preflight=preflight,
            environment_manifest=environment_manifest, installation_record=installation_record)
        if check["applicable"]:
            calibration = calibration_from_reference(reference)
            calibration["source"] += " (qualification authority STALE; hardware/wiring/RF binding equal)"
            return {"mode": "warm", "authority": "qualification_stale_calibration",
                    "reason": "qualification stale but its gain artifact binds the same hardware/"
                              "wiring/RF configuration", "calibration": calibration}
        stale_reasons = check["differences"]
    else:
        stale_reasons = []
    active = (readiness_snapshot or {}).get("active") or {}
    if active.get("valid") and active.get("record"):
        check = readiness_calibration_applicable(active["record"], partial_binding)
        if check["applicable"]:
            return {"mode": "warm", "authority": "readiness", "reason":
                    "active readiness release calibration applicable",
                    "calibration": check["calibration"]}
        stale_reasons.extend(check["differences"])
    return {"mode": "cold", "authority": None,
            "reason": "no applicable calibration artifact (" + ("; ".join(stale_reasons[:4])
                                                                  or "none recorded") + ")",
            "calibration": None}


def gain_input_for(calibration: dict) -> dict:
    return {"endpoint_common_rx_gain_db": dict(calibration["endpoint_common_rx_gain_db"]),
            "source_gain_artifact_sha256": calibration["artifact_sha256"]}


def cold_calibration_placeholder(readiness_id: str) -> dict:
    """cold run의 binding.calibration — 봉인 전에는 source만 확정되고 SHA는 봉인 뒤 채운다."""
    return {"source": "readiness_calibration:%s" % readiness_id, "artifact_sha256": None,
            "artifact_input_sha256": None, "endpoint_common_rx_gain_db": {},
            "calibration_policy_id": CALIBRATION_POLICY_ID}


# ------------------------------------------------------------ plans / budgets

def adaptive_train_bursts() -> dict:
    """coordinator `build_adaptive_gain_paths` 산식 재계산 (RF-off 시험이 coordinator 값과 대조)."""
    window_slots = 3 + 10
    coarse = 8
    out = {}
    for spacing_ms in (120, 240):
        spacing_ns = spacing_ms * 1_000_000
        skip = -(-3_000_000_000 // spacing_ns)
        fine_gap = -(-25_000_000_000 // spacing_ns)
        tick = coarse * (window_slots + skip)
        fine_tick = tick - skip + fine_gap
        out[spacing_ms] = fine_tick + window_slots
    return out


def readiness_plan(mode: str, *, readiness_id: str, run_id: str, calibration: dict | None,
                   preparation_cap_s: float) -> dict:
    """machine-readable preparation RF plan (§5.2). 실제 on-air burst 계수가 아니라 제출 기준 상한이다."""
    trains = adaptive_train_bursts()
    rf_time = {"paths": ["C1", "C2", "C3", "C4", "C5", "C6"], "bursts_per_path": 10,
               "burst_interval_s_x10": 68, "path_spacing_s": 75, "lead_s": 20,
               "phases": ["PRE_CORRECTION", "POST_CORRECTION"],
               "planned_bursts": 6 * 10 * 2}
    probe = readiness_probe.probe_plan()
    max_frames = math.ceil(preparation_cap_s / TDMA_FRAME_S)
    bootstrap_control_budget = {
        "producer_map": [
            "ROC TBB broadcast on BBSC: 1 fragment per TDMA frame (2.4 s) while RUNNING",
            "sender RAC resource request: <= 3 attempts per transfer",
            "ROC ASC/DC allocation: 1 per request attempt",
            "data segment: initial + <= 3 retransmissions per transfer",
            "ACK (DSCH): initial + <= 3 retransmissions per transfer",
        ],
        "tbb_bbsc_max_bursts": max_frames,
        "tbb_frame_s_x10": 24,
        "control_bursts_per_transfer": CONTROL_BURSTS_PER_TRANSFER,
        "probe_transfers": len(readiness_probe.PROBE_VECTOR),
        "probe_control_bursts_max": CONTROL_BURSTS_PER_TRANSFER * len(readiness_probe.PROBE_VECTOR),
        "unclassified_producers": [],
    }
    plan = {
        "artifact_type": "VDETER_READINESS_PREPARATION_PLAN_V1",
        "schema_version": 1,
        "readiness_id": readiness_id,
        "run_id": run_id,
        "mode": mode,
        "phase": "READINESS_PREPARATION" if mode == "warm" else "CALIBRATION_PREPARATION",
        "authority_scope": "RUN_READINESS_PREPARATION",
        "qualification_status": "NOT_QUALIFIED",
        "hard_cap_s": int(preparation_cap_s),
        "implicit_retry": 0,
        "host_time": {"profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                      "max_acquisition_s": 600, "acquisition_attempts": 1 if mode == "warm" else 2,
                      "selection_policy": HOST_TIME_POLICY_ID},
        "rf_time": rf_time,
        "link17_probe": probe,
        "bootstrap_control_budget": bootstrap_control_budget,
        "safety_policy": dict(SAFETY_POLICY),
        "calibration": calibration,
    }
    if mode == "cold":
        cfo = {"paths": ["C1", "C3", "C5"], "bursts_per_path": 10, "burst_interval_s_x10": 68,
               "path_spacing_s": 75, "probe_tx_gain_db": 40, "probe_rx_gain_db": 40,
               "operation_deadline_s": 420, "planned_bursts": 30}
        adaptive = {"paths_120ms": ["C1", "C2", "C3", "C5"], "paths_240ms": ["C4", "C6"],
                    "coarse_gains_db": [0, 12, 24, 36, 48, 60, 72, 75],
                    "window_warmup": 3, "window_judged": 10, "fine_step_db": 3,
                    "train_bursts_120ms": trains[120], "train_bursts_240ms": trains[240],
                    "planned_bursts": 4 * trains[120] + 2 * trains[240]}
        reval = {"paths": rf_time["paths"], "warmup": 3, "judged": 100,
                 "planned_bursts": 6 * 103, "path_spacing_s": 75}
        plan["cfo"] = cfo
        plan["adaptive_gain"] = adaptive
        plan["gain_revalidation"] = reval
        plan["gain_phase_deadline_s"] = 1200
        plan["explicit_planned_bursts_total"] = (cfo["planned_bursts"] + adaptive["planned_bursts"]
                                                 + reval["planned_bursts"] + rf_time["planned_bursts"])
    else:
        plan["explicit_planned_bursts_total"] = rf_time["planned_bursts"]
    return plan


def plan_bytes(plan: dict) -> bytes:
    return (json.dumps(plan, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("utf-8")


# ------------------------------------------------------------ workload admission

def workload_admission(remaining_absolute_s) -> dict:
    """§5.1.1: `remaining_absolute >= 1600` 이어야 READY 발급/새 workload 제출."""
    if remaining_absolute_s is None:
        return {"admitted": False, "remaining_s": None,
                "reserve_s": int(WORKLOAD_ADMISSION_RESERVE_S),
                "reason": "session absolute deadline unknown"}
    remaining = float(remaining_absolute_s)
    admitted = remaining >= WORKLOAD_ADMISSION_RESERVE_S
    return {"admitted": admitted, "remaining_s": round(remaining, 1),
            "reserve_s": int(WORKLOAD_ADMISSION_RESERVE_S),
            "reason": None if admitted else "remaining absolute budget below the admission reserve"}


# ------------------------------------------------------------ 종료 판정

TERMINATION_EXPLICIT_QUIT = "explicit_quit"
TERMINATION_PLAN_COMPLETE = "plan_complete"
TERMINATION_WORKLOAD_STOP = "workload_stop"
TERMINATION_ADMISSION_RESERVE = "admission_reserve"
TERMINATION_USER_CANCELLED = "user_cancelled"
TERMINATION_INPUT_LOST = "input_lost"
TERMINATION_SESSION_ERROR = "session_error"
TERMINATION_PROBE_FAILED = "probe_failed"
TERMINATION_BRINGUP_FAILED = "bringup_failed"
TERMINATION_NOT_ADMITTED = "not_admitted"

_NORMAL_TERMINATIONS = (TERMINATION_EXPLICIT_QUIT, TERMINATION_PLAN_COMPLETE,
                        TERMINATION_WORKLOAD_STOP, TERMINATION_ADMISSION_RESERVE)


def decide_outcome(*, readiness_enabled: bool, prepared: bool, probe_pass: bool,
                   ready_issued: bool, termination: str, user_results: list,
                   close_class: dict, topology_after_pass, bringup_class: dict,
                   active_same_binding: bool, interactive: bool) -> dict:
    """승격/inhibit/summary status 판정 — 합성 없음, 누락 항목은 실패로."""
    workload_verdicts = [readiness_classifier.classify_workload(r) for r in user_results]
    passed = sum(1 for r in user_results if r.get("status") == "PASS")
    reasons = []
    inhibit_codes = set(bringup_class.get("inhibit_codes") or [])
    inhibit_codes.update(close_class.get("inhibit_codes") or [])
    for verdict in workload_verdicts:
        inhibit_codes.update(verdict.get("inhibit_codes") or [])
    cancelled = termination in (TERMINATION_USER_CANCELLED,)
    input_lost = termination == TERMINATION_INPUT_LOST
    close_pass = bool(close_class.get("pass")) and topology_after_pass is not False

    if cancelled:
        summary_status = "USER_CANCELLED"
    elif input_lost:
        summary_status = "INPUT_LOST"
    elif termination == TERMINATION_EXPLICIT_QUIT and not user_results:
        summary_status = "NO_USER_WORKLOAD" if close_pass and probe_pass else "FAIL"
    elif termination in (TERMINATION_BRINGUP_FAILED, TERMINATION_PROBE_FAILED,
                         TERMINATION_SESSION_ERROR, TERMINATION_NOT_ADMITTED):
        summary_status = "FAIL"
    else:
        all_user_pass = user_results and passed == len(user_results)
        summary_status = "PASS" if (close_pass and all_user_pass) else "FAIL"
        if not interactive and termination == TERMINATION_ADMISSION_RESERVE and close_pass:
            # 계획된 workload를 reserve 부족으로 다 제출하지 못함 — 성공으로 합성하지 않는다.
            summary_status = "FAIL"

    promote = (readiness_enabled and prepared and probe_pass and ready_issued and close_pass
               and termination in _NORMAL_TERMINATIONS
               and all(v["verdict"] in ("PASS", "APPLICATION_ONLY") for v in workload_verdicts)
               and not inhibit_codes)
    if readiness_enabled and not promote:
        if not prepared:
            reasons.append("preparation did not PASS")
        if not probe_pass:
            reasons.append("Link 17 readiness probe did not PASS")
        if not ready_issued:
            reasons.append("READY_FOR_WORKLOAD was not issued")
        if not close_pass:
            reasons.append("cleanup/ownership/rediscovery not PASS: %s"
                           % (close_class.get("codes") or []))
        if termination not in _NORMAL_TERMINATIONS:
            reasons.append("termination=%s does not issue readiness" % termination)
        bad = [v for v in workload_verdicts if v["verdict"] not in ("PASS", "APPLICATION_ONLY")]
        if bad:
            reasons.append("user workload verdict(s) %s" % sorted({v["verdict"] for v in bad}))
        if inhibit_codes:
            reasons.append("operational safety codes observed: %s" % sorted(inhibit_codes))
    inhibit = bool(readiness_enabled and active_same_binding and inhibit_codes)
    readiness_status = "PASS" if promote else ("CANCELLED" if (cancelled or input_lost) else "FAIL")
    return {
        "promote": promote, "inhibit": inhibit, "inhibit_codes": sorted(inhibit_codes),
        "readiness_status": readiness_status, "summary_status": summary_status,
        "cancelled": cancelled, "reasons": reasons,
        "workload_verdicts": workload_verdicts, "user_workloads_passed": passed,
        "termination": termination,
    }


# ------------------------------------------------------------ evidence 수집

_RUN_EVIDENCE_FILES = (
    "run.json", "summary.json", "session/ready.json", "session/closed.json",
    "evidence/operational_service/session_result.json",
    "evidence/coordinator-mass-roc-session.json",
    "evidence/coordinator-mass-roc-session-cold.json",
    "resolved-configs/git_state.json", "resolved-configs/installation-state.json",
    "plans/readiness-plan.json", "plans/cfo_plan.json", "plans/gain_plan.json",
    "plans/rf_time_plan.json", "evidence/gain_artifact.json", "evidence/gain_artifact_input.json",
    "readiness/closure-report.json", "readiness/preparation.json",
)


def collect_run_evidence(run_root, *, probe_workload_id=None, extra: dict | None = None) -> dict:
    """candidate evidence로 복사할 {relpath: bytes}. 큰 runtime evidence는 SHA만 index에 남긴다."""
    run_root = Path(run_root)
    files = {}
    for rel in _RUN_EVIDENCE_FILES:
        path = run_root / rel
        if path.is_file():
            files["run/" + rel] = path.read_bytes()
    if probe_workload_id:
        for rel in ("mass-roc-events/final-result.json", "workload-trace.jsonl"):
            path = run_root / "workloads" / probe_workload_id / rel
            if path.is_file():
                files["run/workloads/probe/" + rel.replace("/", "_")] = path.read_bytes()
    digests = {}
    evidence_dir = run_root / "evidence"
    if evidence_dir.is_dir():
        for path in sorted(evidence_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(run_root).as_posix()
                if "run/" + rel not in files:
                    digests[rel] = {"sha256": _sha(path.read_bytes()), "size": path.stat().st_size}
    files["run/evidence-digests.json"] = (json.dumps(
        {"artifact_type": "VDETER_READINESS_RUN_EVIDENCE_DIGESTS_V1", "run_root": str(run_root),
         "files": digests}, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("utf-8")
    for rel, data in (extra or {}).items():
        files[rel] = data
    return files


def evidence_sha256_of(files: dict) -> str:
    """inhibit record의 cause evidence SHA — 수집한 evidence 전체의 canonical SHA map digest."""
    return _sha(json.dumps({k: _sha(v) for k, v in sorted(files.items())},
                           sort_keys=True, separators=(",", ":")).encode("utf-8"))
