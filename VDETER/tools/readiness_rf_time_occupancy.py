#!/usr/bin/env python3
"""OPT-02 RF-time C1~C6 점유 matrix·병렬 group 판정·scheduler collision (operational_readiness spec §13.4).

RF·장치 없음. 정적 topology(`coordinator_main.P2_PATHS`, `PATH_CHANNEL_ROLE`)와 endpoint 실행 구조
(`pi_runtime.validate_rf_time`: plan entry **순차** 처리, TX entry는 마지막 burst+1 s까지 블록, TX 전
공유 TX LO retune/복원, RX는 lane별 slot 창 캡처 RPC 순차)에서 다음을 기계 산출한다.

  1. occupancy matrix   path별 (TX device/lane/LO channel, RX device/lane)
  2. candidate groups   (C1,C3) (C2,C4) C5 C6 (C5,C6) — 명세 §13.4 조사 후보
  3. collision rules    같은 device 동시 TX 두 lane(공유 TX LO·timed TX 경로), 같은 device 동시 RX 두 lane
                        (캡처 RPC 직렬), 같은 device TX+RX 동시(full-duplex 근거 없음), 공통 LO retune 충돌
  4. scheduler test     현재 순차 처리기 모델에 겹침 schedule을 넣으면 두 번째 entry의 burst가 과거 시각이 되어
                        skipped_past_due → decode < 9/10 → FAIL 임을 시뮬레이션 (RF-off)
  5. wall-clock model   순차 457.2 s vs 이론적 부분 병렬(측정 독립성 확보 시) — 목표 12~16분 도달 여부

결정 record(JSON)를 --out에 쓴다. 활성화 판정은 실물 반복 동등성(§14.2-7) 전에는 항상 DISABLED다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for entry in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "src")):
    entry = os.path.abspath(entry)
    if entry not in sys.path:
        sys.path.insert(0, entry)

P2_PATHS = (
    ("C1", "ROC", "MASS-A", 0, 0),
    ("C2", "MASS-A", "ROC", 0, 0),
    ("C3", "ROC", "MASS-B", 1, 0),
    ("C4", "MASS-B", "ROC", 0, 1),
    ("C5", "MASS-A", "MASS-B", 1, 1),
    ("C6", "MASS-B", "MASS-A", 1, 1),
)
PATH_CHANNEL_ROLE = {"C1": "upper", "C2": "lower", "C3": "upper", "C4": "lower",
                     "C5": "upper", "C6": "upper"}
CANDIDATE_GROUPS = (("C1", "C3"), ("C2", "C4"), ("C5",), ("C6",), ("C5", "C6"))
BURSTS = 10
BURST_INTERVAL_S = 6.8
PATH_SPACING_S = 75.0
LEAD_S = 20.0
TX_TAIL_S = 1.0


def occupancy_matrix() -> dict:
    rows = {}
    for path_id, tx, rx, tx_lane, rx_lane in P2_PATHS:
        rows[path_id] = {"tx_device": tx, "tx_lane": tx_lane, "tx_lo_channel": PATH_CHANNEL_ROLE[path_id],
                         "rx_device": rx, "rx_lane": rx_lane,
                         "rx_channel": "upper" if PATH_CHANNEL_ROLE[path_id] == "upper" else "lower"}
    return rows


def group_conflicts(group, matrix) -> list:
    """명세 §13.4 충돌 규칙 — 하나라도 있으면 그 group은 순차로 남긴다."""
    conflicts = []
    paths = [matrix[p] for p in group]
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = paths[i], paths[j]
            if a["tx_device"] == b["tx_device"]:
                conflicts.append("same device concurrent TX on two lanes (%s): shared TX LO retune/restore "
                                 "per path and timed-TX submit path are serialized in the endpoint"
                                 % a["tx_device"])
            if a["rx_device"] == b["rx_device"]:
                conflicts.append("same device concurrent RX capture on two lanes (%s): capture_slot_window "
                                 "is a serialized worker RPC per lane" % a["rx_device"])
            if a["tx_device"] == b["rx_device"] or a["rx_device"] == b["tx_device"]:
                conflicts.append("same device simultaneous TX and RX (%s/%s): no full-duplex safety basis "
                                 "(self-interference, LO leakage)" % (a["tx_device"], b["rx_device"]))
    return conflicts


def sequential_scheduler_collision(group, matrix) -> dict:
    """현재 endpoint 처리 모델(순차 entry)에 겹침 schedule을 줄 때 결과를 RF-off로 시뮬레이션한다.

    각 endpoint는 plan entry를 순서대로 처리한다. TX entry는 마지막 burst + 1 s까지 블록하고, 그 뒤에
    시작하는 다음 entry의 이미 지난 burst는 `skipped_past_due`(제출 안 함). 겹침 group = 두 path가 같은
    start를 갖는다."""
    start = LEAD_S
    schedule = {p: [start + k * BURST_INTERVAL_S for k in range(BURSTS)] for p in group}
    result = {}
    # endpoint별 entry 처리 순서 = plan 순서(group 내 나열 순서)
    for device in ("ROC", "MASS-A", "MASS-B"):
        clock = 0.0
        for p in group:
            row = matrix[p]
            if row["tx_device"] == device:
                skipped = sum(1 for t in schedule[p] if t - 0.05 < clock)
                clock = max(clock, schedule[p][-1] + TX_TAIL_S)
                result.setdefault(p, {})["tx_skipped_past_due"] = skipped
            elif row["rx_device"] == device:
                # RX capture per burst window (blocking until window end)
                captured = sum(1 for t in schedule[p] if t >= clock)
                clock = max(clock, schedule[p][-1] + 0.5)
                result.setdefault(p, {})["rx_windows_capturable"] = captured
    verdict = {}
    for p in group:
        tx_ok = result[p].get("tx_skipped_past_due", 0) == 0
        rx_ok = result[p].get("rx_windows_capturable", BURSTS) >= 9
        verdict[p] = {"pass": tx_ok and rx_ok, **result[p]}
    return {"schedule_overlap": len(group) > 1, "paths": verdict,
            "group_pass": all(v["pass"] for v in verdict.values())}


def wall_clock_model(groups) -> dict:
    """PRE/POST 한 phase의 wall-clock: lead + (group 수 - 1) × 75 s + 9 × 6.8 s + 1 s."""
    sequential = LEAD_S + 5 * PATH_SPACING_S + 9 * BURST_INTERVAL_S + TX_TAIL_S
    grouped = LEAD_S + (len(groups) - 1) * PATH_SPACING_S + 9 * BURST_INTERVAL_S + TX_TAIL_S
    return {"sequential_phase_s": round(sequential, 1), "grouped_phase_s": round(grouped, 1),
            "pre_plus_post_sequential_s": round(2 * sequential, 1),
            "pre_plus_post_grouped_s": round(2 * grouped, 1)}


def decision(baseline_host_time_p50_s: float = 433.9, baseline_host_time_min_s: float = 296.8) -> dict:
    matrix = occupancy_matrix()
    groups = []
    for group in CANDIDATE_GROUPS:
        conflicts = group_conflicts(group, matrix) if len(group) > 1 else []
        collision = sequential_scheduler_collision(group, matrix)
        groups.append({"group": list(group), "conflicts": conflicts,
                       "scheduler_collision_current_code": collision,
                       "eligible_for_overlap": not conflicts and len(group) > 1})
    eligible = [g["group"] for g in groups if g["eligible_for_overlap"]]
    # 안전 group 후보만으로 짠 phase 구성: eligible 쌍 + 나머지 단독
    covered = {p for g in eligible for p in g}
    phase_groups = [tuple(g) for g in eligible] + [(p,) for p in ("C1", "C2", "C3", "C4", "C5", "C6")
                                                   if p not in covered]
    model = wall_clock_model(phase_groups)
    # 명세 §13.4 조사 후보 (C1,C3)/(C2,C4)/C5/C6 를 **가정적으로** 전부 안전하다고 두었을 때의 상한 —
    # 실제로는 위 collision 규칙으로 부적격이지만, 목표 12~16분의 도달 가능성을 보기 위한 천장값이다.
    hypothetical = wall_clock_model([("C1", "C3"), ("C2", "C4"), ("C5",), ("C6",)])
    sequential_total_p50 = 13.6 + baseline_host_time_p50_s + model["pre_plus_post_sequential_s"] + 11.8
    grouped_total_p50 = 13.6 + baseline_host_time_p50_s + model["pre_plus_post_grouped_s"] + 11.8
    grouped_total_min = 13.6 + baseline_host_time_min_s + model["pre_plus_post_grouped_s"] + 11.8
    hypothetical_total_p50 = 13.6 + baseline_host_time_p50_s + hypothetical["pre_plus_post_grouped_s"] + 11.8
    hypothetical_total_min = 13.6 + baseline_host_time_min_s + hypothetical["pre_plus_post_grouped_s"] + 11.8
    return {
        "artifact_type": "VDETER_OPT02_RF_TIME_OCCUPANCY_DECISION_V1",
        "policy_id_active": "STAGE7_RF_TIME_C1C6_SEQUENTIAL_75S_10X6P8S_V1",
        "occupancy_matrix": matrix,
        "candidate_groups": groups,
        "eligible_groups_by_static_topology": eligible,
        "current_code_can_overlap": any(g["scheduler_collision_current_code"]["group_pass"]
                                        for g in groups if len(g["group"]) > 1),
        "wall_clock_model": model,
        "hypothetical_spec_candidate_groups_model": hypothetical,
        "bringup_to_ready_model_s": {
            "sequential_p50": round(sequential_total_p50, 1),
            "grouped_p50": round(grouped_total_p50, 1),
            "grouped_with_min_host_time": round(grouped_total_min, 1),
            "hypothetical_spec_groups_p50": round(hypothetical_total_p50, 1),
            "hypothetical_spec_groups_min_host_time": round(hypothetical_total_min, 1),
            "target_window_s": [720, 960],
            "target_reachable_even_if_grouped": grouped_total_min <= 960,
            "target_reachable_hypothetical_best_case": hypothetical_total_min <= 960,
            "target_reachable_hypothetical_p50": hypothetical_total_p50 <= 960,
        },
        "bottleneck": [
            "RF-time PRE and POST are deterministic 457.2 s each (6 paths x 75 s spacing) = 914.4 s",
            "host-time acquisition 296.8..599.4 s (median 433.9) — u_beta <= 1 ppm needs ~300 s of span",
            "even a full (C1,C3)/(C2,C4) overlap leaves PRE+POST = 614.4 s; with the minimum observed "
            "host-time the total is ~936 s, inside 12-16 min only at the best case",
        ],
        "activation": "DISABLED",
        "activation_reason": (
            "static topology admits (C1,C3) and (C2,C4) only if ROC can transmit on two lanes (C1/C3) or "
            "capture two RX lanes (C2/C4) concurrently; the frozen endpoint validate_rf_time processes plan "
            "entries sequentially (per-path TX LO retune/restore, blocking TX tail, serialized slot-window "
            "capture RPC), so any overlapping schedule collides in the current code (scheduler test FAIL). "
            "Enabling overlap therefore requires an rf-domain change to pi_runtime/validate_rf_time plus "
            "physical B210 repetition equivalence (10/10 decode, spread/closure, self-interference and "
            "timestamp independence) per §13.4/§14.2-7. Not proven in this scope: sequential path kept."),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--host-time-p50", type=float, default=433.9)
    parser.add_argument("--host-time-min", type=float, default=296.8)
    args = parser.parse_args(argv)
    record = decision(args.host_time_p50, args.host_time_min)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, ensure_ascii=True, indent=1, sort_keys=True)
    print(json.dumps({k: record[k] for k in ("eligible_groups_by_static_topology",
                                             "current_code_can_overlap", "wall_clock_model",
                                             "bringup_to_ready_model_s", "activation")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
