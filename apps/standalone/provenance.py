"""참고용 provenance — 기준선 문서 pin (clone-bootstrap v2, informational only).

대상 기계의 setup·qualify·run·results는 **Git을 실행하지도, `.git` 파일을 읽지도
않는다**. Git 정보(commit·branch·dirty)는 실행 환경에서 관측 자체를 하지 않으며
provenance의 `git` 항목은 항상 ``None``으로 고정된다(2026-09-05 검토 반영 —
"Git 정보 관측도 실행환경에서 제거"의 엄격 적용). 구판 ``standalone-binding.json``
도 읽지 않는다.

이 모듈이 제공하는 값은 전부 *참고 기록*이며 qualification 유효성 판정이나 실행
차단에 쓰이지 않는다:

- :data:`STAGE7_BASELINE_PIN` — Stage 7 RR evidence 계약(coordinator
  ``--baseline-sha256`` 대조, final builder 승인 개정 사슬)이 요구하는 기준선
  버전·SHA의 **커밋된 상수**. 문서 실물은 host 감사 환경에서만 대조한다.
  installation digest를 이 자리에 넣지 않는다 (Git digest ↔ installation
  digest 위장 금지).
"""

from __future__ import annotations

#: Stage 7 기준선 문서 pin (0.9.23 — Phase I 자격에서 실측 일치 확인됨).
#: 개정 시 개발자가 final builder APPROVED_REVISION_CHAIN과 함께 갱신한다.
STAGE7_BASELINE_PIN = {
    "version": "0.9.23",
    "sha256": ("2a87c1d0f7bee78c13def8b153745ecbe42ac7dcf701d69abffc9141bc"
               "9cc026"),
    "document": "docs/core/stage7_b210_system_baseline.md",
    "note": ("committed provenance constant for the Stage 7 RR evidence "
             "contract; the document is not read on the target machine and "
             "this value is not a qualification-critical comparison input"),
}

PROVENANCE_NOTE = ("informational only — not used for qualification validity "
                   "or execution gating; git is neither executed nor observed "
                   "on the target machine (git=null by design)")


def build_provenance(root=None) -> dict:
    """reference·git_state에 담는 참고 provenance 묶음.

    `root`는 호환용 인자이며 읽지 않는다 — 실행 환경에서 `.git`·binding 등
    Git 유래 정보를 관측하지 않는다."""
    return {
        "note": PROVENANCE_NOTE,
        "git": None,
        "legacy_binding": None,
        "stage7_baseline": dict(STAGE7_BASELINE_PIN),
    }
