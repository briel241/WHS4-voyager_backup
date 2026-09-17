"""OPT-01 replay 도구·OPT-02 점유 matrix 도구의 RF-off 계약 시험 (spec §13.3, §13.4, §14.1)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from fractions import Fraction
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rf_time_occupancy_matrix_and_decision_are_deterministic(tmp_path):
    occ = _load("readiness_rf_time_occupancy")
    matrix = occ.occupancy_matrix()
    assert matrix["C1"] == {"tx_device": "ROC", "tx_lane": 0, "tx_lo_channel": "upper",
                            "rx_device": "MASS-A", "rx_lane": 0, "rx_channel": "upper"}
    assert matrix["C4"]["tx_device"] == "MASS-B" and matrix["C4"]["rx_lane"] == 1
    # 명세 조사 후보는 전부 같은 device의 동시 TX 또는 동시 RX를 요구한다 → 충돌 규칙에 걸린다.
    assert any("concurrent TX" in c for c in occ.group_conflicts(("C1", "C3"), matrix))
    assert any("concurrent RX" in c for c in occ.group_conflicts(("C2", "C4"), matrix))
    assert any("simultaneous TX and RX" in c for c in occ.group_conflicts(("C5", "C6"), matrix))
    assert occ.group_conflicts(("C5",), matrix) == []
    # 현재 순차 처리기 모델: 겹침 schedule은 두 번째 entry의 TX가 전부 past-due.
    collision = occ.sequential_scheduler_collision(("C1", "C3"), matrix)
    assert collision["schedule_overlap"] and collision["group_pass"] is False
    assert collision["paths"]["C3"]["tx_skipped_past_due"] == 10
    single = occ.sequential_scheduler_collision(("C5",), matrix)
    assert single["group_pass"] is True
    record = occ.decision()
    assert record["activation"] == "DISABLED"
    assert record["eligible_groups_by_static_topology"] == []
    assert record["current_code_can_overlap"] is False
    assert record["wall_clock_model"]["sequential_phase_s"] == 457.2
    assert record["hypothetical_spec_candidate_groups_model"]["grouped_phase_s"] == 307.2
    assert record["bringup_to_ready_model_s"]["target_reachable_hypothetical_p50"] is False
    out = tmp_path / "opt02.json"
    assert occ.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text("utf-8"))["artifact_type"] == \
        "VDETER_OPT02_RF_TIME_OCCUPANCY_DECISION_V1"


def _write_runtime_json(path: Path, brackets, restarts):
    observations = []
    for m0, h, m1 in brackets:
        observations.append({"m_before_ns": m0, "hardware_ns": h, "m_after_ns": m1,
                             "m_sample_ns": (m0 + m1) // 2, "u_sample_ns": m1 - m0,
                             "u_mid_ns": -((-(m1 - m0)) // 2)})
    doc = {"endpoint_id": "MASS-A", "operations_evidence": [
        {"kind": "host_time_restarts", "restarts": restarts},
        {"kind": "host_time_fit", "summary": {"outcome": "PASS"},
         "artifact": {"observations": observations, "residuals_ns": []}}]}
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_host_time_replay_tool_reports_equivalence_and_adversarial(tmp_path):
    replay = _load("readiness_host_time_replay")
    brackets = []
    start_host, start_hw = 1_000_000_000_000, 5_000_000_000_000
    for k in range(60):
        m = start_host + k * 1_000_000_000
        hw_ns = start_hw + int(Fraction(k * 1_000_000_000) * (1 + Fraction(65, 10) / 1_000_000))
        brackets.append((m - 10_000, hw_ns, m + 10_000))   # 20 µs → PASS at ~30 samples
    root = tmp_path / "evidence"
    root.mkdir()
    _write_runtime_json(root / "MASS-A-runtime.json", brackets,
                        [{"reason": "bracket", "discarded": 40}])
    out = tmp_path / "opt01.json"
    rc = replay.main([str(root), "--out", str(out), "--seed", "1"])
    report = json.loads(out.read_text("utf-8"))
    assert report["series_count"] == 1
    assert report["summary"]["replay_equal"] == 1
    assert report["summary"]["regime_change_candidate_false_accepts"] == 0
    assert report["summary"]["equivalence_gate"] == "PASS" and rc == 0
    assert report["availability"]["brackets_discarded_by_baseline_total"] == 40
    wide = [v for v in report["adversarial"] if v["variant"] == "wide_x1"][0]
    assert wide["baseline_pass"] and wide["candidate_pass"]
    assert wide["candidate_calls"] < wide["baseline_calls"]
    regime = [v for v in report["adversarial"] if v["variant"] == "regime_change"][0]
    assert regime["candidate_pass"] is False           # 섞인 시리즈를 수용하지 않는다
    regression = [v for v in report["adversarial"] if v["variant"] == "regression"][0]
    assert regression["baseline_pass"] == regression["candidate_pass"]
