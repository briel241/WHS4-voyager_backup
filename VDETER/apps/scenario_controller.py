"""Stage 7 TEST-only Scenario Controller 실행기 (`stage7` §16.2, v0.9.1 G/H항).

`RESEARCH_READY_MINIMAL` RR-P2/RR-P3 soak의 기능 자극을 세 endpoint의 test
adapter 소켓을 통해 public `VdeService` 호출로만 수행하고, 판정은 launcher가
기록하는 public event JSONL(sha256+length)로만 한다. SUT 구성원이 아니며
Runtime Coordinator·endpoint·worker 기능을 대신하지 않는다.

coordinator와의 동기화는 run_root의 gate 파일로 한다:
- coordinator → `controller_gate/admission_open`: START 진입 허용
- controller  → `controller_gate/started_ok`: 세 START 응답 OK
- controller  → `evidence/scenario_result.json` + exit code: 시나리오 종결
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (APP_ROOT, os.path.join(APP_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

ENDPOINT_IDS = ("MASS-A", "MASS-B", "ROC")
STATION_BY_ENDPOINT = {"MASS-A": "111111111", "MASS-B": "333333333", "ROC": "222222222"}
UNKNOWN_STATION = "999999999"


def payload_bytes(length: int, seed: int) -> bytes:
    return bytes((i * 7 + seed) % 256 for i in range(length))


class AdapterChannel:
    """SeqPacketClient 위의 §16.2 request/response 채널."""

    def __init__(self, client, timeout_s: float = 300.0) -> None:
        self._client = client
        self._timeout_s = timeout_s

    def request(self, message: dict) -> dict:
        self._client.send(message)
        deadline = time.monotonic() + self._timeout_s
        while time.monotonic() < deadline:
            received = self._client.recv(timeout=2.0)
            if received is None:
                continue
            if received.get("command_id") == message["command_id"]:
                return received
        raise TimeoutError(f"test adapter response 초과: {message['command_name']}")


class EventLog:
    """launcher event JSONL tail — public event만 본다."""

    def __init__(self, run_root: str) -> None:
        self._dir = os.path.join(run_root, "evidence", "service_events")
        self._offsets = {ep: 0 for ep in ENDPOINT_IDS}
        self.records = {ep: [] for ep in ENDPOINT_IDS}

    def poll(self) -> None:
        for endpoint_id in ENDPOINT_IDS:
            path = os.path.join(self._dir, f"{endpoint_id}.jsonl")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    handle.seek(self._offsets[endpoint_id])
                    chunk = handle.read()
                    self._offsets[endpoint_id] = handle.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.records[endpoint_id].append(json.loads(line))
                except ValueError:
                    continue

    def wait(self, predicate, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.poll()
            found = predicate(self.records)
            if found:
                return found
            time.sleep(0.5)
        return None


def _received(records, endpoint_id, sha):
    return [
        r for r in records[endpoint_id]
        if r.get("event_type") == "DATA_RECEIVED" and r.get("payload_sha256") == sha
    ]


def _terminal_states(records, endpoint_id):
    return [
        r for r in records[endpoint_id]
        if r.get("event_type") == "TRANSFER_STATE_CHANGED"
        and r.get("transfer_state") in ("COMPLETED", "FAILED")
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--test-plan-sha", required=True)
    parser.add_argument("--fixtures-dir", required=True)
    parser.add_argument("--mode", required=True, choices=("scenario", "soak"))
    parser.add_argument("--soak-seconds", type=int, default=600)
    parser.add_argument("--min-transfers", type=int, default=10)
    arguments = parser.parse_args(argv)

    from vdeter.b210_system.hw_operations import read_monotonic_raw_ns
    from vdeter.b210_system.control_plane.controller import ScenarioController
    from vdeter.b210_system.transport import SeqPacketClient, runtime_socket_root

    fixtures = {}
    for name in sorted(os.listdir(arguments.fixtures_dir)):
        if name.endswith(".json"):
            data = open(os.path.join(arguments.fixtures_dir, name), "rb").read()
            fixtures[name] = (data, hashlib.sha256(data).hexdigest())

    root = runtime_socket_root(arguments.run_id)
    channels = {}
    for endpoint_id in ENDPOINT_IDS:
        socket_path = os.path.join(root, f"{endpoint_id}.test.sock")
        deadline = time.monotonic() + 120.0
        client = None
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                try:
                    client = SeqPacketClient(socket_path)
                    break
                except OSError:
                    pass
            time.sleep(0.5)
        if client is None:
            print(f"[controller] {endpoint_id} test socket 연결 실패", flush=True)
            return 2
        channels[endpoint_id] = AdapterChannel(client)
    controller = ScenarioController(
        run_id=arguments.run_id,
        resolved_test_plan_sha256=arguments.test_plan_sha,
        adapter_channels=channels,
        host_raw_clock=read_monotonic_raw_ns,
    )
    events = EventLog(arguments.run_root)
    gate_dir = os.path.join(arguments.run_root, "controller_gate")
    os.makedirs(gate_dir, exist_ok=True)

    steps = []
    counters = {"transfers_completed": 0, "transfers_failed_expected": 0,
                "retries": 0, "wrong_destination": 0, "duplicates": 0,
                "unexpected_errors": 0}

    def record(name, status, **detail):
        steps.append({"name": name, "status": status, **detail})
        print(f"[controller] step {name}: {status} {detail}", flush=True)

    def invoke(endpoint_id, operation, relpath, step_id):
        data, sha = fixtures[relpath]
        response = controller.invoke_service_fixture(
            endpoint_id,
            scenario_id=f"rr-{arguments.mode}",
            step_id=step_id,
            service_operation=operation,
            local_fixture_relpath=relpath,
            local_fixture_sha256=sha,
        )
        body = response.get("body") or {}
        return body.get("call_outcome") == "OK", body

    # --- 1. admission_open 대기 → 세 START 병렬 --------------------------------
    gate = os.path.join(gate_dir, "admission_open")
    # RF 사다리(CFO+fit+APPLY 재검증+rf_time PRE/POST) 실측 ~25분이 admission
    # 전에 선행한다 — 대기 상한은 phase guard(70분, 0.9.19)에 동기한다
    # (rr-p2-17 run d 실측: 300s로는 사다리 완료 전에 포기; 2,400s는 구
    # 40분 guard 기준이라 사다리 지연 시 guard보다 먼저 포기하는 부정합).
    deadline = time.monotonic() + 4_200.0
    while not os.path.exists(gate):
        if time.monotonic() > deadline:
            record("await-admission", "FAIL", reason="admission_open gate 4200s 초과")
            return 1
        time.sleep(0.2)

    start_results = {}

    def _start(endpoint_id):
        ok, body = invoke(
            endpoint_id, "START", f"start-{endpoint_id.lower()}.json",
            f"start-{endpoint_id}",
        )
        start_results[endpoint_id] = (ok, body)

    threads = [threading.Thread(target=_start, args=(ep,)) for ep in ENDPOINT_IDS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300.0)
    if not all(start_results.get(ep, (False, None))[0] for ep in ENDPOINT_IDS):
        record("start-all", "FAIL", results={
            ep: start_results.get(ep, (False, {}))[1] for ep in ENDPOINT_IDS})
        return 1
    record("start-all", "PASS")
    open(os.path.join(gate_dir, "started_ok"), "w").close()

    def transfer(step_id, sender, dest_relpath, expect_receiver, payload_sha,
                 timeout_s=180.0, expect_fail=False, max_attempts=3,
                 gating=True):
        # 한정 재시도 ≤2회 (기준선 v0.9.8, 사용자 승인 2026-08-29): 시도별
        # timeout 불변, 전 시도 기록(attempts/retries 계수), 전패 시 FAIL
        # 유지. 무선 per-attempt 확률성 인정 — 하위 계층 예산과 정합.
        # gating=False (v0.9.13): 유실 허용 반복(주기 보고 재현) — 실패를
        # LOSS로 기록하되 세트 판정에 관여하지 않는다 (전 시도 기록 불변).
        # v0.9.13: soak가 동일 payload를 재사용하므로 수신 hit은 제출 전
        # 스냅샷 대비 증분으로 판정한다 (이전 cycle 배달의 허위 중복/허위
        # 수신 방지).
        attempts = []
        outcome = None
        before_hits = (
            len(_received(events.records, expect_receiver, payload_sha))
            if expect_receiver is not None else 0
        )
        for attempt in range(max_attempts):
            before_terminals = len(_terminal_states(events.records, sender))
            ok, body = invoke(sender, "SUBMIT", dest_relpath, step_id)
            if not ok:
                attempts.append({"attempt": attempt + 1, "result": "SUBMIT_REJECTED"})
                if expect_fail:
                    # 미지 목적지 등은 제출 단계 즉시 거부가 더 강한 정상
                    # 방어다 (svcdbg76 실측: destination_not_deployment_member).
                    # 0.9.20: 거부 "사유"까지 검증 — 무관한 거부(예: 자원
                    # 고갈)의 허위 PASS 방지. 목적지/지원성 계열만 인정.
                    # 0.9.22: body의 유일 신호는 public_error_code(=ErrorCode
                    # 문자열)뿐 — reason(destination_...)은 body에 노출되지
                    # 않으므로 매칭 불가능한 죽은 마커 2종을 제거해 조임.
                    body_text = json.dumps(body, ensure_ascii=False)
                    accepted_markers = (
                        "UNSUPPORTED_REQUEST", "INVALID_REQUEST",
                    )
                    if not any(m in body_text for m in accepted_markers):
                        record(step_id, "FAIL",
                               reason="예상 밖 거부 사유 (목적지/지원성 계열 아님)",
                               body=body, attempts=attempts)
                        return False
                    counters["transfers_failed_expected"] += 1
                    record(step_id, "PASS",
                           terminal="SUBMIT_REJECTED(정상 즉시 거부)",
                           body=body, attempts=attempts)
                    return True
                record(step_id, "FAIL", reason="submit 거부", body=body,
                       attempts=attempts)
                return False

            def predicate(records):
                terminals = _terminal_states(records, sender)[before_terminals:]
                if expect_fail:
                    failed = [t for t in terminals if t["transfer_state"] == "FAILED"]
                    return failed or None
                done = [t for t in terminals if t["transfer_state"] == "COMPLETED"]
                if not done:
                    return None
                if expect_receiver is not None:
                    got = _received(records, expect_receiver, payload_sha)
                    if len(got) <= before_hits:
                        return None
                return done

            outcome = events.wait(predicate, timeout_s)
            attempts.append({
                "attempt": attempt + 1,
                "result": "OK" if outcome is not None else "TIMEOUT",
            })
            if outcome is not None:
                break
            if attempt + 1 < max_attempts:
                counters["retries"] += 1  # 재시도 정의 = 후속 시도가 있는 실패만
        if outcome is None:
            if not gating:
                record(step_id, "LOSS", gating=False,
                       reason="유실 허용 반복 — 미배달", attempts=attempts)
                return False
            record(step_id, "FAIL",
                   reason=f"timeout 대기 초과 ({max_attempts}시도 전패)",
                   attempts=attempts)
            return False
        # wrong destination / duplicate 검사 (증분 기준 — payload 재사용 대응)
        wrong = 0
        for endpoint_id in ENDPOINT_IDS:
            hits = _received(events.records, endpoint_id, payload_sha)
            if endpoint_id == expect_receiver:
                delta = len(hits) - before_hits
                if delta > 1:
                    counters["duplicates"] += delta - 1
            elif hits:
                wrong += len(hits)
        counters["wrong_destination"] += wrong
        if expect_fail:
            # 0.9.20 보강: 지연형 실패도 원인 검사 — 무관 장애(자원 고갈
            # 등)를 예상 실패로 오인하지 않게 목적지/시간류만 인정.
            fail_text = json.dumps(outcome, ensure_ascii=False)
            fail_markers = (
                "destination", "TIMEOUT", "UNSUPPORTED_REQUEST",
                "INVALID_REQUEST", "ROUTE",
            )
            if not any(m in fail_text for m in fail_markers):
                record(step_id, "FAIL",
                       reason="예상 밖 실패 원인 (목적지/시간류 아님)",
                       terminal=fail_text[:200], attempts=attempts)
                return False
            counters["transfers_failed_expected"] += 1
            record(step_id, "PASS", terminal="FAILED(예상된 timeout/NACK 처리)", attempts=attempts)
        else:
            counters["transfers_completed"] += 1
            record(step_id, "PASS", attempts=attempts)
        return wrong == 0

    # v0.9.11 정리: 확정 응용 시나리오 요구 전송 유형만 유지.
    # data-roc-to-a = 관제→함정 데이터 세션(인증서 ≤1024B 운송로).
    payloads = {
        "short-roc-to-a": payload_bytes(29, 1),
        "data-roc-to-a": payload_bytes(353, 7),
        "short-b-to-roc-noack": payload_bytes(30, 5),
        "timeout-unknown-dest": payload_bytes(29, 6),
    }
    shas = {k: hashlib.sha256(v).hexdigest() for k, v in payloads.items()}

    overall_ok = True
    if arguments.mode == "scenario":
        # v0.9.18 (2026-08-30): 데이터 세션 재현 실증(svc-run-o 완주,
        # 1시도·재시도 0)으로 data-roc-to-a를 판정 세트에 편입 — 4종.
        # (v0.9.9의 candidate 6종 체계는 소멸: 직송/relay는 계획 정의가
        # 없어 FILTER로도 실행 불가 — FILTER는 아래 4종의 부분집합 선택.)
        # v0.9.19 반영: data를 1순위로 — 실측상 귀(수신 창)는 서비스
        # 시작 직후의 신선한 창에서 가장 신뢰됨(svcdbg O·q 성공 조건).
        # 세트 판정은 순서 무관.
        judged_plan = (
            ("data-roc-to-a", "ROC", "MASS-A", False),
            ("short-roc-to-a", "ROC", "MASS-A", False),
            ("short-b-to-roc-noack", "MASS-B", "ROC", False),
            ("timeout-unknown-dest", "MASS-A", None, True),
        )
        plan = judged_plan
        scenario_filter = os.environ.get("STAGE7_SCENARIO_FILTER")
        if scenario_filter:
            allowed = {x.strip() for x in scenario_filter.split(",") if x.strip()}
            plan = tuple(p for p in plan if p[0] in allowed)
            print(f"[controller] scenario filter: {sorted(allowed)}", flush=True)
        for name, sender, receiver, expect_fail in plan:
            ok = transfer(
                name, sender, f"submit-{name}.json", receiver, shas[name],
                timeout_s=240.0 if expect_fail else 180.0,
                expect_fail=expect_fail,
            )
            overall_ok = overall_ok and ok
            # fail-fast 제거 (2026-08-29): 실패해도 나머지 시나리오를 계속
            # 실행해 회차당 전 항목 실측을 확보한다. 세트 판정은 불변
            # (모든 단계 기록·overall은 전 항목 기준).
    else:  # soak
        soak_deadline = time.monotonic() + arguments.soak_seconds
        cycle = 0
        # v0.9.13 soak 재정의 (재현 프레임 정합, 사용자 승인 2026-08-29):
        # 판정 대상 = 관제→함정 반복(≥min_transfers, v0.9.8 재시도 준거).
        # 함정→관제는 응용 의미(주기 위치보고 — 유실 허용, 주기 도착=생존
        # 판정, AIS 계열 설계)에 맞춰 1시도·60s 반복으로 성공률을 실측
        # 기록한다(판정 비관여, 전 시도 기록 — 은폐 없음).
        counters["roc_to_ship_completed"] = 0
        counters["ship_report_attempts"] = 0
        counters["ship_report_delivered"] = 0
        while time.monotonic() < soak_deadline:
            ok = transfer(
                f"soak-{cycle}-short-roc-to-a", "ROC",
                "submit-short-roc-to-a.json", "MASS-A",
                shas["short-roc-to-a"], timeout_s=180.0,
            )
            overall_ok = overall_ok and ok
            if ok:
                counters["roc_to_ship_completed"] += 1
            if time.monotonic() >= soak_deadline:
                break
            # 주기 보고는 격사이클(~2분 간격) — 판정 반복(관제→함정)의
            # 페이스를 보장하면서 주기성 재현을 유지한다.
            if cycle % 2 == 1:
                counters["ship_report_attempts"] += 1
                delivered = transfer(
                    f"soak-{cycle}-ship-report", "MASS-B",
                    "submit-short-b-to-roc-noack.json", "ROC",
                    shas["short-b-to-roc-noack"], timeout_s=60.0,
                    max_attempts=1, gating=False,
                )
                if delivered:
                    counters["ship_report_delivered"] += 1
            cycle += 1
        if counters["roc_to_ship_completed"] < arguments.min_transfers:
            record("soak-min-transfers", "FAIL",
                   completed=counters["roc_to_ship_completed"],
                   required=arguments.min_transfers)
            overall_ok = False

    # 예상 밖 ERROR event 집계 (timeout subcase의 FAILED terminal은 제외).
    events.poll()
    for endpoint_id in ENDPOINT_IDS:
        for item in events.records[endpoint_id]:
            if item.get("event_type") == "ERROR":
                counters["unexpected_errors"] += 1

    for endpoint_id in ENDPOINT_IDS:
        invoke(endpoint_id, "STOP", "stop.json", f"stop-{endpoint_id}")
    record("stop-all", "PASS")

    # v0.9.9 게이트 정합: 오목적지 0 불변; duplicates <= retries
    # (재시도 지연 완료 산물); 전 단계 PASS 시 오류 이벤트는 residual.
    # v0.9.13: 유실 허용 반복(gating=False, LOSS)은 세트 판정에서 제외 —
    # 성공률은 counters(ship_report_*)와 steps 기록으로 전량 보존된다.
    steps_all_pass = all(
        item.get("status") == "PASS"
        for item in steps if item.get("gating", True)
    )
    counters["residual_error_events"] = counters["unexpected_errors"]
    overall_ok = (overall_ok
                  and counters["wrong_destination"] == 0
                  and counters["duplicates"] <= counters["retries"]
                  and (steps_all_pass or counters["unexpected_errors"] == 0))
    result = {
        "artifact_type": "STAGE7_RR_SCENARIO_RESULT_V1",
        "baseline_revision": "0.9.22",
        # 0.9.22: 판정 세트를 줄일 수 있는 필터는 콘솔 print만이 아니라
        # 산출물에도 기계 기록한다 (정식 run은 coordinator가 활성 거부).
        "scenario_filter": os.environ.get("STAGE7_SCENARIO_FILTER") or None,
        "unresolved_under_investigation": [
            "data-a-to-roc", "direct-short-a-to-b", "relay-data-a-to-b",
        ],
        "deferred_follow_up": ["link-19-qualification"],
        "mode": arguments.mode,
        "run_id": arguments.run_id,
        "status": "PASS" if overall_ok else "FAIL",
        "steps": steps,
        "counters": counters,
        "command_log": controller.command_log,
    }
    out = os.path.join(arguments.run_root, "evidence", "scenario_result.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=True, sort_keys=True, indent=1)
    print(f"[controller] 종결 {result['status']} → {out}", flush=True)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
