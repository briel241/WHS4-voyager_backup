"""서비스 기동 구간 전용 디버그 하네스 (계측용 — RR evidence 미생성).

RF qualification(~22분)을 건너뛰고 prepare→configure→arm_rx→admission→
staged start→TBB 대기(→선택적 시나리오)까지 직행해, rr-p2 서비스 구간을
~5분 주기로 반복 계측한다. §16.1 admission PREPARE가 ARMED 상태를
허용하는 것을 이용한다. research_ready/ evidence를 만들지 않는다.

사용 (Pi에서):
    PYTHONPATH=src python3 tools/stage7_service_debug.py \
        --run-id svcdbg1 --configs-dir <run-configs dir> --run-root <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "src"))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "apps"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--configs-dir", required=True)
    parser.add_argument("--with-scenario", action="store_true")
    parser.add_argument("--gain-artifact", default=None)
    arguments = parser.parse_args()

    import stage7_coordinator_main as app
    from vdeter.stage7.coordinator import Stage7RuntimeCoordinator
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns

    os.makedirs(os.path.join(arguments.run_root, "evidence"), exist_ok=True)
    resolved, plan = app.load_group(arguments.run_id, arguments.configs_dir, arguments.gain_artifact)
    test_plan_sha = hashlib.sha256(
        json.dumps(
            {m: resolved[m].resolved_sha256 for m in app.ENDPOINT_IDS},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    processes, channels = app.spawn_and_connect(
        arguments.run_id, arguments.run_root, arguments.configs_dir, plan,
        arguments.gain_artifact, test_plan_sha=test_plan_sha,
    )
    coordinator = Stage7RuntimeCoordinator(
        plan, channels, host_raw_clock=read_monotonic_raw_ns,
        run_id=arguments.run_id,
    )
    present = list(channels)
    controller = None
    try:
        app.run_p0_prepare_configure_arm(
            coordinator, resolved, plan, arguments.run_root
        )
        print("[svcdbg] ARMED", flush=True)
        # 실제 §13.2 fit + 항등 보정 active bridge — 서비스 radio adoption이
        # bridge를 요구한다 (svcdbg13 실측: 'active host bridge 없이 adoption
        # 불가'). rf_time 검증은 생략(디버그 전용).
        import hashlib as _hashlib
        from vdeter.stage7.canonical_json import encode_canonical_json as _ecj
        from vdeter.stage7.ipc import body_set_sha256 as _bss
        acquire = coordinator.run_barrier(
            "acquire_host_time",
            {m: {"profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                 "acquisition_attempt": 1,
                 "max_acquisition_ns": 600_000_000_000} for m in present},
            members=present, concurrent_commit=True,
        )
        fits = {m: acquire[m]["body"]["result"] for m in present}
        for m in present:
            if fits[m] is None or fits[m].get("outcome") != "PASS":
                print(f"[svcdbg] FAIL: {m} fit", flush=True); return 1
        print("[svcdbg] fit 3/3", flush=True)
        active_clock = f"stage7-pi-host-affine-v1:{arguments.run_id}:0"
        candidate_set = [[m, _hashlib.sha256(_ecj(fits[m])).hexdigest()] for m in present]
        bodies = {}
        for m in present:
            bodies[m] = {
                "candidate_set": [list(x) for x in candidate_set],
                "candidate_set_sha256": _bss(candidate_set),
                "candidate_sha256": dict(candidate_set)[m],
                "active_clock_id": active_clock,
                "bridge_generation": 0,
                "alpha_num": fits[m]["alpha_num"], "alpha_den": fits[m]["alpha_den"],
                "beta_num": fits[m]["beta_num"], "beta_den": fits[m]["beta_den"],
                "correction_num": 0, "correction_den": 1,
            }
        coordinator.run_barrier(
            "prepare_active_bridge", bodies,
            commit_command_name="commit_active_bridge",
            abort_command_name="abort_active_bridge", members=present,
        )
        coordinator._bridge_generation = 0
        coordinator._bridge_members = set(present)
        print("[svcdbg] bridge COMMITTED — admission 직행", flush=True)

        gate_dir = os.path.join(arguments.run_root, "controller_gate")
        os.makedirs(gate_dir, exist_ok=True)
        controller = app._spawn_controller(
            arguments.run_id, arguments.run_root, arguments.configs_dir,
            test_plan_sha, "scenario" if arguments.with_scenario else "scenario",
        )

        def pump_events():
            for member in present:
                for event in coordinator._channels[member].poll_events():
                    res = coordinator.receive_event(member, event)
                    b = event.get("body", {})
                    print(f"[svcdbg] evt {member} {b.get('event_type')} "
                          f"{b.get('endpoint_state')} -> "
                          f"{getattr(res, 'value', res)}", flush=True)

        admission = coordinator.open_service_admission()
        open(os.path.join(gate_dir, "admission_open"), "w").close()
        print("[svcdbg] admission OPEN", flush=True)
        deadline = admission.value["service_start_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_intents_complete():
                break
            if read_monotonic_raw_ns() >= deadline:
                print("[svcdbg] FAIL: intent cutoff 초과", flush=True)
                try:
                    print("[svcdbg] faults:", getattr(coordinator, "_faults", None), flush=True)
                except Exception:
                    pass
                return 1
            time.sleep(0.2)
        print("[svcdbg] intents 3/3", flush=True)
        start_commits = coordinator.commit_service_start()
        staged_set = [
            [m, start_commits[m]["body"]["result"]["staged_service_started_sha256"]]
            for m in present
        ]
        print("[svcdbg] staged 3/3", flush=True)
        tbb_deadline = admission.value["tbb_publish_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_tbb_complete(0):
                print("[svcdbg] TBB_ACTIVE 3/3 — 관문 통과!", flush=True)
                break
            if read_monotonic_raw_ns() >= tbb_deadline:
                print("[svcdbg] FAIL: TBB deadline 초과", flush=True)
                return 1
            time.sleep(0.5)
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        open(os.path.join(gate_dir, "started_ok"), "w").close()
        print("[svcdbg] RUNNING — publish 완료", flush=True)
        if arguments.with_scenario:
            print("[svcdbg] scenario 대기...", flush=True)
            # 전체 6-시나리오 세트 최악 경로(~19분) + 여유
            for _ in range(3600):
                result_path = os.path.join(
                    arguments.run_root, "evidence", "scenario_result.json"
                )
                if os.path.exists(result_path):
                    print("[svcdbg] scenario_result:",
                          open(result_path).read()[:400], flush=True)
                    break
                time.sleep(1.0)
        return 0
    finally:
        try:
            coordinator.emergency_inhibit("SVCDBG_END", "svcdbg")
        except Exception:  # noqa: BLE001
            pass
        if controller is not None:
            controller.terminate()
        for process in processes.values():
            process.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
