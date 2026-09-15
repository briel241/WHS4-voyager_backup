"""작업 C 수용조건 검증 (지시서 6.3). 표준 라이브러리 assert 만 사용.

    python3 tests/test_scenario_log.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.observe.scenario_analysis import analyze, to_json, to_text
from common.observe.scenario_log import LogEntry, ScenarioRun

T0 = 1_760_000_000.0
PAYLOAD_HASH = "a" * 64          # 재조립 payload SHA-256 을 흉내낸 값
FINGERPRINT = "451b98cdf3f6"


def build_healthy_run(fragments=3, *, verify_ok=True, retransmit=False):
    """SC-VDE-001 정상 흐름을 흉내낸 로그를 만든다.

    송신(t=+0.0, +1.0, +2.0) 각각에 대해 ACK 가 0.5 초 뒤에 온다
    → 지연은 전부 0.5 초.
    """
    run = ScenarioRun(scenario_id="SC-VDE-001", started=T0)
    for index in range(1, fragments + 1):
        base = T0 + (index - 1) * 1.0
        corr = f"frag-{index}"
        run.record(base, "ROC->MASS", "send",
                   f"X509 cert frag {index}/{fragments}",
                   corr=corr, source_id=1, destination_id=42,
                   fragment=index, fragment_total=fragments)
        if retransmit and index == 2:
            run.record(base + 0.2, "ROC->MASS", "send",
                       f"X509 cert frag {index}/{fragments} 재전송",
                       corr=corr, source_id=1, destination_id=42,
                       fragment=index, fragment_total=fragments,
                       retransmission=True)
        run.record(base + 0.5, "MASS->ROC", "ack",
                   f"frag {index} ACK",
                   corr=corr, source_id=42, destination_id=1, status=4)

    run.record(T0 + fragments * 1.0, "MASS->ROC", "verify",
               "인증서 서명 검증",
               ok=verify_ok,
               reason="ok" if verify_ok else "bad-signature",
               payload_sha256=PAYLOAD_HASH,
               fingerprint=FINGERPRINT,
               stored=verify_ok)
    run.finish(T0 + fragments * 1.0 + 0.1)
    return run


EXPECTED_CERT = {
    "scenario_id": "SC-VDE-001",
    "signature_verified": True,
    "payload_sha256": PAYLOAD_HASH,
    "fragments": 3,
    "all_fragments_acked": True,
    "max_retransmissions": 0,
    "ack_status": 4,
    "max_latency_seconds": 2.0,
    "no_duplicate_store": True,
}


# --------------------------------------------------------------------------
# 1. 정상 시나리오는 passed=True
# --------------------------------------------------------------------------

def check_pass():
    summary = analyze(build_healthy_run(), EXPECTED_CERT)
    assert summary["passed"] is True, summary["checks"]
    assert summary["scenario_id"] == "SC-VDE-001"
    names = {c["name"] for c in summary["checks"]}
    assert "signature-verified" in names
    assert "payload-matches" in names
    assert "fragments-complete" in names
    assert all(c["ok"] for c in summary["checks"]), summary["checks"]

    # expected 가 비면 판정할 게 없으므로 passed=False (조용한 통과 방지)
    assert analyze(build_healthy_run(), {})["passed"] is False


# --------------------------------------------------------------------------
# 2. 서명 실패 항목 → passed=False, 해당 check ok=False
# --------------------------------------------------------------------------

def check_signature_failure():
    summary = analyze(build_healthy_run(verify_ok=False), EXPECTED_CERT)
    assert summary["passed"] is False

    by_name = {c["name"]: c for c in summary["checks"]}
    assert by_name["signature-verified"]["ok"] is False
    assert "bad-signature" in by_name["signature-verified"]["note"]
    # 서명과 무관한 체크는 여전히 통과해야 한다
    assert by_name["fragments-complete"]["ok"] is True
    assert by_name["ack-status"]["ok"] is True


def check_other_failures():
    # fragment 누락
    run = build_healthy_run(fragments=2)
    summary = analyze(run, dict(EXPECTED_CERT, fragments=3))
    by_name = {c["name"]: c for c in summary["checks"]}
    assert by_name["fragments-complete"]["ok"] is False
    assert summary["passed"] is False

    # 재전송 초과
    summary = analyze(build_healthy_run(retransmit=True), EXPECTED_CERT)
    by_name = {c["name"]: c for c in summary["checks"]}
    assert by_name["retransmissions-within-limit"]["ok"] is False
    assert summary["retransmissions"] == 1

    # 재전송을 1회까지 허용하면 통과
    summary = analyze(build_healthy_run(retransmit=True),
                      dict(EXPECTED_CERT, max_retransmissions=1))
    assert summary["passed"] is True

    # ACK 상태 불일치 (7=Errors)
    run = ScenarioRun(scenario_id="SC-VDE-005", started=T0)
    run.record(T0, "ROC->MASS", "send", "항로지시", corr="r1", waypoints=5)
    run.record(T0 + 0.4, "MASS->ROC", "ack", "S-421 ACK", corr="r1", status=7)
    run.finish(T0 + 1.0)
    summary = analyze(run, {"scenario_id": "SC-VDE-005",
                            "ack_status": 4, "waypoints": 5})
    by_name = {c["name"]: c for c in summary["checks"]}
    assert by_name["ack-status"]["ok"] is False
    assert "Errors" in by_name["ack-status"]["note"]
    assert by_name["waypoint-count"]["ok"] is True

    # 중복 저장 탐지 (멱등 위반)
    run = build_healthy_run()
    run.finished = None
    run.record(T0 + 9, "MASS->ROC", "verify", "같은 인증서 재저장",
               ok=True, stored=True, fingerprint=FINGERPRINT)
    run.finish(T0 + 10)
    summary = analyze(run, EXPECTED_CERT)
    by_name = {c["name"]: c for c in summary["checks"]}
    assert by_name["trust-store-idempotent"]["ok"] is False


# --------------------------------------------------------------------------
# 3. 지연 통계가 로그 시각으로부터 올바르게 계산됨
# --------------------------------------------------------------------------

def check_latency_stats():
    run = ScenarioRun(scenario_id="SC-VDE-001", started=T0)
    # 지연 0.5 / 1.5 / 2.5 초가 되도록 배치
    for index, delay in enumerate((0.5, 1.5, 2.5), start=1):
        corr = f"c{index}"
        run.record(T0 + index * 10, "ROC->MASS", "send", f"send {index}",
                   corr=corr, fragment=index)
        run.record(T0 + index * 10 + delay, "MASS->ROC", "ack", f"ack {index}",
                   corr=corr, status=4)
    run.finish(T0 + 100)

    stats = analyze(run, {"ack_status": 4})["latency_seconds"]
    assert stats["count"] == 3, stats
    assert abs(stats["min"] - 0.5) < 1e-9, stats
    assert abs(stats["max"] - 2.5) < 1e-9, stats
    assert abs(stats["mean"] - 1.5) < 1e-9, stats

    # 최대 지연 상한 판정
    assert analyze(run, {"max_latency_seconds": 3.0})["passed"] is True
    assert analyze(run, {"max_latency_seconds": 2.0})["passed"] is False

    # 로그 순서가 뒤섞여 기록돼도 시각 기준으로 계산한다
    shuffled = ScenarioRun(scenario_id="SC-VDE-001", started=T0)
    shuffled.record(T0 + 2.0, "MASS->ROC", "ack", "ack", corr="x", status=4)
    shuffled.record(T0, "ROC->MASS", "send", "send", corr="x")
    shuffled.finish(T0 + 3)
    stats = analyze(shuffled, {"ack_status": 4})["latency_seconds"]
    assert abs(stats["max"] - 2.0) < 1e-9, stats

    # 응답이 없으면 지연 쌍도 없다
    lonely = ScenarioRun(scenario_id="SC-VDE-001", started=T0)
    lonely.record(T0, "ROC->MASS", "send", "응답 없는 송신", corr="y")
    lonely.finish(T0 + 1)
    assert analyze(lonely, {})["latency_seconds"]["count"] == 0


# --------------------------------------------------------------------------
# 4. to_text 가 예외 없이 사람이 읽는 문자열을 만든다
# --------------------------------------------------------------------------

def check_to_text():
    for run, expected in ((build_healthy_run(), EXPECTED_CERT),
                          (build_healthy_run(verify_ok=False), EXPECTED_CERT),
                          (ScenarioRun("SC-X", T0), {})):
        text = to_text(analyze(run, expected))
        assert isinstance(text, str) and text.strip(), repr(text)
        assert "시나리오" in text
        assert ("합격" in text) or ("불합격" in text)

    text = to_text(analyze(build_healthy_run(), EXPECTED_CERT))
    assert "PASS" in text and "FAIL" not in text
    text = to_text(analyze(build_healthy_run(verify_ok=False), EXPECTED_CERT))
    assert "FAIL" in text


# --------------------------------------------------------------------------
# 5. JSON 직렬화 가능 (지시서 6.3 마지막 줄)
# --------------------------------------------------------------------------

def check_json_serializable():
    run = build_healthy_run()
    text = run.to_json()
    restored = ScenarioRun.from_dict(json.loads(text))
    assert restored.scenario_id == run.scenario_id
    assert len(restored.entries) == len(run.entries)
    assert restored.entries[0].detail == run.entries[0].detail
    assert restored.finished == run.finished
    # 복원한 로그로도 같은 판정이 나와야 한다
    assert analyze(restored, EXPECTED_CERT) == analyze(run, EXPECTED_CERT)

    summary = analyze(run, EXPECTED_CERT)
    assert json.loads(to_json(summary)) == summary


# --------------------------------------------------------------------------
# 6. 로거 자체의 방어 규칙
# --------------------------------------------------------------------------

def check_logger_guards():
    run = ScenarioRun(scenario_id="SC-VDE-001", started=T0)
    run.record(T0, "ROC->MASS", "send", "ok")
    assert run.duration is None, "종료 전에는 소요 시간이 없다"

    for bad in (("SIDEWAYS", "send"), ("ROC->MASS", "explode")):
        try:
            LogEntry(time=T0, direction=bad[0], kind=bad[1], message="x")
        except ValueError:
            pass
        else:
            raise AssertionError(f"잘못된 값이 허용됐다: {bad}")

    run.finish(T0 + 5)
    assert run.duration == 5.0
    try:
        run.record(T0 + 6, "ROC->MASS", "send", "종료 후 기록")
    except RuntimeError:
        pass
    else:
        raise AssertionError("종료 후 기록이 허용됐다")

    # finish 시각은 마지막 항목보다 이를 수 없다
    late = ScenarioRun(scenario_id="SC-X", started=T0)
    late.record(T0 + 100, "ROC->MASS", "note", "늦은 항목")
    late.finish(T0 + 1)
    assert late.finished == T0 + 100


CHECKS = (
    ("1. 정상 시나리오 합격", check_pass),
    ("2. 서명 실패 판정", check_signature_failure),
    ("2b. 기타 실패 판정", check_other_failures),
    ("3. 지연 통계", check_latency_stats),
    ("4. to_text", check_to_text),
    ("5. JSON 직렬화", check_json_serializable),
    ("6. 로거 방어 규칙", check_logger_guards),
)


if __name__ == "__main__":
    for title, check in CHECKS:
        try:
            check()
        except AssertionError as exc:
            raise AssertionError(f"{title} 실패: {exc}") from exc
