"""시나리오 러너 — 지원 시나리오 합격 판정과 SC-VDE-001 미지원 처리를 확인.

요청 작업(구현·재현 인터페이스·로그·분석)이 지원 시나리오(005·006)에 대해 온전히
도는지 잠근다. SC-VDE-001은 인증서 Data Session이 링크 계층에서 미완성이라
운영 실행이 **미지원**이어야 한다 — 실행을 요청하면 모의 성공이 아니라
구조화된 미지원 결과가 나와야 한다. cryptography 없이도 돈다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.observe.scenario_analysis import analyze
from common.observe.scenario_log import ScenarioRun

from apps.scenario_runner import SCENARIOS, UNSUPPORTED_SCENARIOS, unsupported_result

NOW = 1_760_000_000.0  # 현실적 기준 시각 (인증서 유효기간 계산에 필요)


def test_scenario_registration_split():
    # 운영 지원: 노선·조종. 미지원(코드 보존): 인증서.
    for scenario_id in ("SC-VDE-005", "SC-VDE-006"):
        assert scenario_id in SCENARIOS, f"{scenario_id} 미등록"
    assert "SC-VDE-001" in UNSUPPORTED_SCENARIOS, "SC-VDE-001 미지원 등록 누락"
    assert "SC-VDE-001" not in SCENARIOS, "SC-VDE-001이 운영 목록에 있으면 안 된다"
    print("  등록 분리 OK:", ", ".join(sorted(SCENARIOS)),
          "/ 미지원:", ", ".join(sorted(UNSUPPORTED_SCENARIOS)))


def test_supported_scenarios_pass():
    for scenario_id, (name, runner) in sorted(SCENARIOS.items()):
        run, expected = runner(now=NOW)
        assert isinstance(run, ScenarioRun)
        summary = analyze(run, expected)
        assert summary["passed"], (
            f"{scenario_id}({name}) 불합격: "
            f"{[c for c in summary['checks'] if not c['ok']]}")
        assert summary["checks"], f"{scenario_id} 판정 항목 없음"
        print(f"  {scenario_id} {name}: 합격 ({len(summary['checks'])}개 체크)")


def test_certificate_scenario_returns_structured_unsupported():
    result = unsupported_result("SC-VDE-001", NOW)
    assert result["status"] == "unsupported"
    assert result["supported"] is False
    assert "Data Session" in result["reason"]
    assert result["scenario_id"] == "SC-VDE-001"
    print("  SC-VDE-001 구조화된 미지원 결과 OK")


def test_control_scenario_verifies_control_fields():
    # SC-VDE-006은 조종 필드(heading/fallback/SOG)가 압축 코덱을 왕복했음을
    # verify 항목으로 남긴다.
    run, expected = SCENARIOS["SC-VDE-006"][1](now=NOW)
    verifies = [e for e in run.entries if e.kind == "verify"]
    assert verifies, "조종 필드 검증 기록이 없다"
    assert all(e.detail.get("ok") is True for e in verifies)
    assert expected.get("verify_ok") is True
    print("  SC-VDE-006 조종 필드 압축 왕복 검증 기록 OK")


if __name__ == "__main__":
    test_scenario_registration_split()
    test_supported_scenarios_pass()
    test_certificate_scenario_returns_structured_unsupported()
    test_control_scenario_verifies_control_fields()
    print("PASS: 시나리오 러너 (지원 2종 + 미지원 처리)")
