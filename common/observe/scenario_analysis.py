"""시나리오 종료 후 분석·판정 (작업 C).

실행 로그(``ScenarioRun``)를 기대치(``expected``)와 대조해 합격/불합격,
지연 통계, 재전송 횟수를 낸다. 반환값은 JSON 직렬화 가능한 dict 다.

--------------------------------------------------------------------------
expected 키 (있는 항목만 검사한다 — 없는 키는 체크를 만들지 않는다)
--------------------------------------------------------------------------
    signature_verified   bool  kind="verify" 항목이 하나 이상 있고 전부 ok=True (서명 검증)
    verify_ok            bool  signature_verified와 같은 검사, 서명 아닌 일반 검증용 이름
    payload_sha256       str   재조립 payload SHA-256 hex 가 이 값과 일치
    fragments            int   fragment 총 개수가 이 값과 일치, 빠진 번호 없음
    all_fragments_acked  bool  ack 개수가 fragment 개수 이상
    max_retransmissions  int   재전송 횟수가 이 값 이하
    ack_status           int   마지막 ACK 상태 코드가 이 값 (S-421 4=Acknowledged)
    waypoints            int   항로 웨이포인트 개수가 이 값과 일치
    max_latency_seconds  float 최대 지연이 이 값 이하
    no_duplicate_store   bool  같은 지문을 두 번 저장하지 않았음 (멱등)
    min_entries          int   로그 항목이 이 개수 이상

판정 체크 예:
  * 인증서(SC-VDE-001): 재조립 payload 바이트 일치, 서명 검증 성공,
    전 fragment ACK, 중복 실행 없음(멱등).
  * 항로(SC-VDE-005): ACK status=4(Acknowledged), waypoint 개수 일치.

--------------------------------------------------------------------------
지연 측정 규칙
--------------------------------------------------------------------------
``detail["corr"]`` 이 같은 **첫 send** 와 **첫 ack/recv** 를 한 쌍으로 본다.
corr 이 없는 항목은 시각 순서대로 FIFO 로 짝짓는다. 재전송이 있으면 첫 송신
기준이라 지연이 재전송 시간을 포함한다(운용상 체감 지연과 같다).
"""

from __future__ import annotations

import json

try:
    from .scenario_log import ScenarioRun
except ImportError:  # `python3 common/observe/scenario_analysis.py`로 직접 실행하는 경우
    import os as _os
    import sys as _sys

    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    from common.observe.scenario_log import ScenarioRun

#: S-421 Edition 1.0.0 ACK 상태 코드
ACK_STATUS_NAMES = {
    0: "received",
    4: "Acknowledged",
    7: "Errors",
    8: "Incomplete",
    9: "Route issues",
}


# --------------------------------------------------------------------------
# 지표 계산
# --------------------------------------------------------------------------

def _latencies(run: ScenarioRun) -> list[float]:
    """송신 → 응답 지연 목록(초)."""
    pending_by_corr: dict[object, float] = {}
    pending_anonymous: list[float] = []
    results: list[float] = []

    for entry in run.ordered():
        corr = entry.detail.get("corr")
        if entry.kind == "send":
            if corr is not None:
                pending_by_corr.setdefault(corr, entry.time)   # 첫 송신 기준
            else:
                pending_anonymous.append(entry.time)
        elif entry.kind in ("ack", "recv"):
            if corr is not None:
                start = pending_by_corr.pop(corr, None)
            else:
                start = pending_anonymous.pop(0) if pending_anonymous else None
            if start is not None and entry.time >= start:
                results.append(entry.time - start)
    return results


def _latency_stats(values: list[float]) -> dict:
    if not values:
        return {"min": None, "max": None, "mean": None, "count": 0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "count": len(values),
    }


def _retransmissions(run: ScenarioRun) -> int:
    """재전송 횟수. 명시 플래그 또는 같은 corr 의 2회차 이후 송신을 센다."""
    seen: set[object] = set()
    count = 0
    for entry in run.ordered():
        if entry.kind != "send":
            continue
        corr = entry.detail.get("corr")
        repeated = corr is not None and corr in seen
        if entry.detail.get("retransmission") is True or repeated:
            count += 1
        if corr is not None:
            seen.add(corr)
    return count


def _fragment_numbers(run: ScenarioRun) -> list[int]:
    numbers = []
    for entry in run.entries:
        value = entry.detail.get("fragment")
        if isinstance(value, int) and value not in numbers:
            numbers.append(value)
    return sorted(numbers)


def _last_detail(run: ScenarioRun, kind: str, key: str):
    value = None
    for entry in run.ordered():
        if entry.kind == kind and key in entry.detail:
            value = entry.detail[key]
    return value


def _any_detail(run: ScenarioRun, key: str):
    for entry in run.ordered():
        if key in entry.detail:
            return entry.detail[key]
    return None


# --------------------------------------------------------------------------
# 개별 체크
# --------------------------------------------------------------------------

def _verify_entries_all_ok(run: ScenarioRun, noun: str) -> tuple[bool, str]:
    verifications = run.by_kind("verify")
    if not verifications:
        return False, f"{noun} 기록이 없다"
    failed = [e for e in verifications if e.detail.get("ok") is not True]
    if failed:
        reasons = ", ".join(str(e.detail.get("reason", "unknown")) for e in failed)
        return False, f"{noun} 실패 {len(failed)}건: {reasons}"
    return True, f"{noun} {len(verifications)}건 모두 통과"


def _check_signature(run: ScenarioRun, _expected) -> tuple[bool, str]:
    return _verify_entries_all_ok(run, "서명 검증")


def _check_verify_ok(run: ScenarioRun, _expected) -> tuple[bool, str]:
    return _verify_entries_all_ok(run, "검증")


def _check_payload(run: ScenarioRun, expected) -> tuple[bool, str]:
    actual = _any_detail(run, "payload_sha256")
    if actual is None:
        return False, "재조립 payload 해시 기록이 없다"
    if actual != expected:
        return False, f"payload 불일치 (기대 {expected[:12]}…, 실제 {str(actual)[:12]}…)"
    return True, "재조립 payload 가 원본과 바이트 일치"


def _check_fragments(run: ScenarioRun, expected) -> tuple[bool, str]:
    numbers = _fragment_numbers(run)
    if not numbers:
        return False, "fragment 기록이 없다"
    if numbers != list(range(1, expected + 1)):
        missing = sorted(set(range(1, expected + 1)) - set(numbers))
        return False, f"fragment {expected}개 중 누락 {missing or '없음'}, 관측 {numbers}"
    return True, f"fragment {expected}/{expected} 수신"


def _check_all_acked(run: ScenarioRun, _expected) -> tuple[bool, str]:
    fragments = len(_fragment_numbers(run))
    acks = len(run.by_kind("ack"))
    if fragments and acks < fragments:
        return False, f"fragment {fragments}개 중 ACK {acks}개"
    if not fragments and acks == 0:
        return False, "ACK 기록이 없다"
    return True, f"ACK {acks}건"


def _check_retransmissions(run: ScenarioRun, expected) -> tuple[bool, str]:
    count = _retransmissions(run)
    if count > expected:
        return False, f"재전송 {count}회 (허용 {expected}회)"
    return True, f"재전송 {count}회 (허용 {expected}회)"


def _check_ack_status(run: ScenarioRun, expected) -> tuple[bool, str]:
    status = _last_detail(run, "ack", "status")
    if status is None:
        return False, "ACK 상태 코드 기록이 없다"
    name = ACK_STATUS_NAMES.get(status, "?")
    if status != expected:
        want = ACK_STATUS_NAMES.get(expected, "?")
        return False, f"ACK status={status}({name}), 기대 {expected}({want})"
    return True, f"ACK status={status}({name})"


def _check_waypoints(run: ScenarioRun, expected) -> tuple[bool, str]:
    actual = _any_detail(run, "waypoints")
    if actual is None:
        return False, "웨이포인트 개수 기록이 없다"
    if actual != expected:
        return False, f"웨이포인트 {actual}개, 기대 {expected}개"
    return True, f"웨이포인트 {expected}개 일치"


def _check_latency(run: ScenarioRun, expected) -> tuple[bool, str]:
    stats = _latency_stats(_latencies(run))
    if stats["max"] is None:
        return False, "지연을 잴 송수신 쌍이 없다"
    if stats["max"] > expected:
        return False, f"최대 지연 {stats['max']:.3f}s (허용 {expected}s)"
    return True, f"최대 지연 {stats['max']:.3f}s (허용 {expected}s)"


def _check_no_duplicate_store(run: ScenarioRun, _expected) -> tuple[bool, str]:
    stored: list = []
    for entry in run.ordered():
        if entry.detail.get("stored") is True:
            stored.append(entry.detail.get("fingerprint"))
    duplicates = {f for f in stored if stored.count(f) > 1}
    if duplicates:
        return False, f"같은 지문이 중복 저장됐다: {sorted(map(str, duplicates))}"
    return True, f"저장 {len(stored)}건, 중복 없음"


def _check_min_entries(run: ScenarioRun, expected) -> tuple[bool, str]:
    if len(run.entries) < expected:
        return False, f"로그 {len(run.entries)}건 (최소 {expected}건)"
    return True, f"로그 {len(run.entries)}건"


#: expected 키 → (체크 이름, 판정 함수)
_CHECKS = {
    "signature_verified": ("signature-verified", _check_signature),
    "verify_ok": ("fields-verified", _check_verify_ok),
    "payload_sha256": ("payload-matches", _check_payload),
    "fragments": ("fragments-complete", _check_fragments),
    "all_fragments_acked": ("all-fragments-acked", _check_all_acked),
    "max_retransmissions": ("retransmissions-within-limit", _check_retransmissions),
    "ack_status": ("ack-status", _check_ack_status),
    "waypoints": ("waypoint-count", _check_waypoints),
    "max_latency_seconds": ("latency-within-limit", _check_latency),
    "no_duplicate_store": ("trust-store-idempotent", _check_no_duplicate_store),
    "min_entries": ("log-not-empty", _check_min_entries),
}


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------

def analyze(run: ScenarioRun, expected: dict) -> dict:
    """실행 로그를 기대치와 대조해 요약을 낸다.

    반환 예::

        {
          "scenario_id": "SC-VDE-001",
          "passed": True,
          "checks": [{"name": "signature-verified", "ok": True, "note": "..."}, ...],
          "latency_seconds": {"min": .., "max": .., "mean": .., "count": ..},
          "retransmissions": 0,
          "notes": [...],
        }

    ``expected`` 에 없는 키는 검사하지 않는다. bool 값 체크는 ``False`` 로
    끄면 그 항목을 아예 만들지 않는다.
    """
    if not isinstance(expected, dict):
        raise TypeError("expected 는 dict 여야 한다")

    checks = []
    for key, (name, function) in _CHECKS.items():
        if key not in expected:
            continue
        want = expected[key]
        if want is False or want is None:
            continue                         # 끈 체크
        ok, note = function(run, want)
        checks.append({"name": name, "ok": bool(ok), "note": note})

    notes = list(expected.get("notes", []))
    for entry in run.ordered():
        if entry.kind == "note":
            notes.append(entry.message)

    unknown = sorted(set(expected) - set(_CHECKS) - {"notes", "scenario_id"})
    if unknown:
        notes.append(f"분석기가 모르는 기대 항목(무시함): {', '.join(unknown)}")
    if run.finished is None:
        notes.append("finish() 가 호출되지 않아 소요 시간이 없다")

    return {
        "scenario_id": expected.get("scenario_id", run.scenario_id),
        "passed": all(c["ok"] for c in checks) and bool(checks),
        "checks": checks,
        "latency_seconds": _latency_stats(_latencies(run)),
        "retransmissions": _retransmissions(run),
        "entry_count": len(run.entries),
        "duration_seconds": run.duration,
        "notes": notes,
    }


def to_text(summary: dict) -> str:
    """콘솔용 사람 읽는 판정문."""
    verdict = "합격" if summary.get("passed") else "불합격"
    lines = [
        f"시나리오 {summary.get('scenario_id', '?')} — {verdict}",
        "-" * 56,
    ]

    checks = summary.get("checks") or []
    if not checks:
        lines.append("  (판정 항목 없음 — expected 가 비었다)")
    for check in checks:
        mark = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"  [{mark}] {check.get('name', '?')}: {check.get('note', '')}")

    latency = summary.get("latency_seconds") or {}
    if latency.get("count"):
        lines.append(
            f"  지연(초): 최소 {latency['min']:.3f} / 평균 {latency['mean']:.3f}"
            f" / 최대 {latency['max']:.3f}  (측정 {latency['count']}쌍)")
    else:
        lines.append("  지연(초): 측정된 송수신 쌍 없음")

    lines.append(f"  재전송: {summary.get('retransmissions', 0)}회"
                 f" / 로그 {summary.get('entry_count', 0)}건")
    duration = summary.get("duration_seconds")
    if duration is not None:
        lines.append(f"  소요: {duration:.3f}초")

    for note in summary.get("notes") or []:
        lines.append(f"  * {note}")
    return "\n".join(lines)


def to_json(summary: dict, *, indent: int = 2) -> str:
    """판정 요약을 JSON 문자열로."""
    return json.dumps(summary, ensure_ascii=False, indent=indent)


# 이 파일은 라이브러리다. 직접 실행하면 자체 점검용 예시 판정을 보여준다.
if __name__ == "__main__":
    demo = ScenarioRun(scenario_id="DEMO", started=0.0)
    demo.record(0.0, "ROC->MASS", "send", "예시 송신", corr="d1")
    demo.record(0.5, "MASS->ROC", "ack", "예시 ACK", corr="d1", status=4)
    demo.finish(0.6)
    print("이 파일은 라이브러리입니다(직접 실행 대상 아님). 자체 점검용 예시 판정:\n")
    print(to_text(analyze(demo, {"ack_status": 4, "max_latency_seconds": 2.0})))
    print("\n실제 시나리오 로그·판정을 보려면:  python3 apps/scenario_runner.py")
