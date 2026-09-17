#!/usr/bin/env python3
"""OPT-01 저장 evidence replay + synthetic adversarial 대조 (operational_readiness spec §13.3, §14.1).

입력: `*-runtime.json`(endpoint runtime evidence)의 `host_time_fit.artifact.observations`(accepted series)와
`host_time_restarts`. 폐기된 raw bracket은 기록되지 않으므로(폐기 개수만) 두 종류의 대조를 한다.

  1. replay-equivalence : accepted series 전체를 두 규칙(RESTART_SERIES_V1 / REJECT_BRACKET_KEEP_SERIES_V1)에
                          그대로 통과시켜 같은 fit(계수·u_beta·d_ppm)이 나오는지 확인한다 (wide bracket이 없는
                          스트림에서 규칙은 정의상 동일해야 한다).
  2. synthetic-adversarial: 각 accepted series에 (a) wide bracket 1~3개를 무작위 위치에 주입, (b) hardware 역행
                          1회 주입, (c) wide bracket 뒤 host offset step(regime change) 주입 — 후보 규칙이
                          baseline보다 false-accept를 늘리지 않는지(거부는 동일, 수용은 같은 fit) 대조한다.
  3. availability       : 기록된 baseline restart(폐기 개수)에 대해 후보 규칙이 유지했을 표본 수를 셈한다
                          (recorded `discarded` 합계 = 후보가 절약했을 bracket 수의 상한).

RF·장치 없음. 출력은 JSON report 하나(--out). qualification namespace를 읽거나 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from fractions import Fraction

_HERE = os.path.dirname(os.path.abspath(__file__))
for entry in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "src")):
    entry = os.path.abspath(entry)
    if entry not in sys.path:
        sys.path.insert(0, entry)

from vdeter.b210_system import hw_operations as hw  # noqa: E402
from vdeter.b210_system.time_bridge import BracketObservation, TimeBridgeError  # noqa: E402


class _Worker:
    def __init__(self, brackets):
        self._brackets = list(brackets)
        self.calls = 0

    def bracket_hardware_time(self):
        if not self._brackets:
            raise TimeBridgeError("stream exhausted")
        self.calls += 1
        return self._brackets.pop(0)


def _run(brackets, policy):
    """fake clock/sleep으로 스트림을 통과시킨다. 반환 (fit|None, calls, log, error)."""
    worker = _Worker(brackets)
    state = {"i": 0}

    def raw():
        idx = min(state["i"], len(brackets) - 1)
        return brackets[idx][0]

    def sleep(_s):
        state["i"] += 1

    original = (hw.read_monotonic_raw_ns, hw._time.sleep)
    hw.read_monotonic_raw_ns, hw._time.sleep = raw, sleep
    log = []
    try:
        fit, obs = hw.acquire_host_time_fit(worker, restart_log=log, selection_policy=policy)
        return fit, worker.calls, log, None
    except TimeBridgeError as error:
        return None, worker.calls, log, str(error)
    finally:
        hw.read_monotonic_raw_ns, hw._time.sleep = original


def _fit_key(fit):
    if fit is None:
        return None
    c = fit.coefficients
    return {"alpha": [c.alpha_num, c.alpha_den], "beta": [c.beta_num, c.beta_den],
            "u_beta_ppm": fit.u_beta_ppm, "d_ppm": [fit.d_ppm_num, fit.d_ppm_den],
            "observations": fit.observation_count, "span_ns": fit.span_ns}


def load_series(roots):
    out = []
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", "*-runtime.json"), recursive=True)):
            try:
                doc = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            restarts = []
            for entry in doc.get("operations_evidence") or []:
                if entry.get("kind") == "host_time_restarts":
                    restarts = entry.get("restarts") or []
                if entry.get("kind") == "host_time_fit":
                    obs = entry.get("artifact", {}).get("observations") or []
                    brackets = [(o["m_before_ns"], o["hardware_ns"], o["m_after_ns"]) for o in obs]
                    out.append({"path": path, "brackets": brackets, "restarts": restarts,
                                "summary": entry.get("summary")})
    return out


def adversarial_variants(brackets, rng):
    n = len(brackets)
    variants = []
    # (a) wide brackets at random positions before the earliest possible PASS
    for count in (1, 2, 3):
        b = list(brackets)
        for pos in sorted(rng.sample(range(2, max(3, n // 3)), count)):
            m0, h, _m1 = b[pos]
            b[pos] = (m0, h, m0 + 3_000_000)
        variants.append(("wide_x%d" % count, b))
    # (b) hardware regression once
    b = list(brackets)
    pos = n // 4
    m0, _h, m1 = b[pos]
    b[pos] = (m0, b[pos - 1][1] - 1_000, m1)
    variants.append(("regression", b))
    # (c) regime change behind a wide bracket (host offset +5 ms)
    b = list(brackets)
    pos = n // 3
    m0, h, _m1 = b[pos]
    b[pos] = (m0, h, m0 + 4_000_000)
    b = b[:pos + 1] + [(x + 5_000_000, y, z + 5_000_000) for x, y, z in b[pos + 1:]]
    variants.append(("regime_change", b))
    return variants


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("roots", nargs="+", help="directories to scan for *-runtime.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    series = load_series(args.roots)
    if args.limit:
        series = series[: args.limit]
    rng = random.Random(args.seed)
    report = {"artifact_type": "VDETER_OPT01_HOST_TIME_REPLAY_V1", "seed": args.seed,
              "series_count": len(series), "replay": [], "adversarial": [], "availability": {}}
    equal = 0
    saved_total = 0
    restart_series = 0
    false_accept_delta = 0
    for item in series:
        brackets = item["brackets"]
        base = _run(brackets, hw.HOST_TIME_SELECTION_BASELINE)
        cand = _run(brackets, hw.HOST_TIME_SELECTION_CANDIDATE)
        same = _fit_key(base[0]) == _fit_key(cand[0])
        equal += int(same)
        report["replay"].append({"path": item["path"], "observations": len(brackets),
                                 "baseline": _fit_key(base[0]), "candidate": _fit_key(cand[0]),
                                 "equal": same, "baseline_calls": base[1], "candidate_calls": cand[1]})
        discarded = sum(int(r.get("discarded", 0)) for r in item["restarts"])
        if item["restarts"]:
            restart_series += 1
            saved_total += discarded
        for name, variant in adversarial_variants(brackets, rng):
            vb = _run(variant, hw.HOST_TIME_SELECTION_BASELINE)
            vc = _run(variant, hw.HOST_TIME_SELECTION_CANDIDATE)
            verdict = {"path": item["path"], "variant": name,
                       "baseline_pass": vb[0] is not None, "candidate_pass": vc[0] is not None,
                       "baseline_calls": vb[1], "candidate_calls": vc[1],
                       "baseline_error": vb[3], "candidate_error": vc[3],
                       "fit_equal": (_fit_key(vb[0]) == _fit_key(vc[0])) if vb[0] and vc[0] else None}
            if verdict["candidate_pass"] and not verdict["baseline_pass"]:
                if name == "regime_change":
                    false_accept_delta += 1     # 후보가 baseline이 거부한 것을 수용 = false-accept 증가
                verdict["candidate_only_pass"] = True
            report["adversarial"].append(verdict)
    report["availability"] = {
        "series_with_baseline_restarts": restart_series,
        "brackets_discarded_by_baseline_total": saved_total,
        "note": "discarded raw brackets are not recorded; the candidate would have kept up to this many "
                "(upper bound on availability gain), subject to the unchanged fit gates",
    }
    adv = report["adversarial"]
    wide = [v for v in adv if v["variant"].startswith("wide")]
    report["summary"] = {
        "replay_equal": equal, "replay_total": len(series),
        "adversarial_total": len(adv),
        "regime_change_candidate_false_accepts": false_accept_delta,
        "regime_change_candidate_passes": sum(
            1 for v in adv if v["variant"] == "regime_change" and v["candidate_pass"]),
        "wide_bracket_variants": len(wide),
        "wide_bracket_candidate_passes_earlier": sum(
            1 for v in wide if v["candidate_pass"] and v["baseline_pass"]
            and v["candidate_calls"] < v["baseline_calls"]),
        "wide_bracket_candidate_passes_where_baseline_exhausts_stream": sum(
            1 for v in wide if v["candidate_pass"] and not v["baseline_pass"]),
        "wide_bracket_both_exhaust_stream": sum(
            1 for v in wide if not v["candidate_pass"] and not v["baseline_pass"]),
        "wide_bracket_candidate_fails_where_baseline_passes": sum(
            1 for v in wide if not v["candidate_pass"] and v["baseline_pass"]),
        "regression_both_reset_pass": sum(
            1 for v in adv if v["variant"] == "regression" and v["candidate_pass"] == v["baseline_pass"]),
        "stream_limit_note": ("recorded series contain only the accepted brackets (~270-326 at 1 s); "
                              "a baseline restart inside a recorded series usually exhausts the stream "
                              "before re-acquisition, so baseline_pass=false there means 'not enough "
                              "recorded brackets', not a baseline defect"),
        "equivalence_gate": ("PASS" if equal == len(series) and false_accept_delta == 0
                             and not any(not v["candidate_pass"] and v["baseline_pass"] for v in wide)
                             else "FAIL"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=True, indent=1, sort_keys=True)
    print(json.dumps(report["summary"], indent=1))
    return 0 if report["summary"]["equivalence_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
