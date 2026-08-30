# integration/mass-roc-stage7 통합 회귀 증거 (2026-08-30)

브랜치 `integration/mass-roc-stage7` (main `2036cb2` + `origin/feat/mass-roc-app`
`3b521ff` 병합 + 보완 커밋)에 대한 시험 실행 기록. 실행 인터프리터:
`VDETER\.venv\Scripts\python.exe` (Python 3.12.13, cryptography 50.0.0, pytest 9.1.1).

## A. 정적·기본

| 항목 | 결과 |
| --- | --- |
| `git diff --check` (main..HEAD 전체) | 통과 (whitespace 오류 0) |
| Python 전체 모듈 compile (`compileall` MASS·ROC·common·apps·tests·tools) | 통과 |
| `pytest tests -q` (ignore 없음) | **23 passed** / 실패 0 / 오류 0 / skip 0 |
| 직접 실행형 시험 13종 (tests/*.py) | 전부 통과 — CP949 콘솔에서도 중단 없음 |
| `tools/check_docs.py` | PASS (검사 47건) |

## B·C. MASS/ROC 응용·시나리오 소프트웨어 종단

| 항목 | 결과 |
| --- | --- |
| `tests/test_mass_roc_link_integration.py` (신규 Service API 종단 회귀) | 4/4 통과 — SC-VDE-005 waypoint·계획SOG 복원·적용, SC-VDE-006 heading·fallback 복원·적용, 전송상태 COMPLETED, source/destination, MASS-B 주소 격리, 재전송 중복 적용 방지 |
| `apps/route_loopback_demo.py` / `control_loopback_demo.py` | PASS (sc_vde 운영 format, TEST scope) |
| `apps/cert_loopback_demo.py` | PASS — 소프트웨어 loopback 참고용 (Data Session 아님) |
| `apps/scenario_runner.py` (005·006 합격, 001 구조화된 미지원 결과) | 통과 |
| 콘솔 GUI 기동 (`--transport software`) | HTTP 200, transport=software, operational=true |
| 전송 리팩터 smoke (software 왕복·vdeter 별칭·stage7 주입 seam) | 통과 (completed 2/1, failed 0) |

## D. VDE-TER 회귀 (VDETER/ 무수정 — 동결 계층)

| 실행 | 결과 | evidence |
| --- | --- | --- |
| `tools/run_stage7_tests.py --run-id stage7-software-massroc-20260830` | **overall=PASS** — suite 16종 248건 실패 0·오류 0, frozen_hash_audit PASS(기준 8aa0d33), import_graph_audit PASS. 하드웨어 판정 TC(P0~P3)는 `NOT_RUN` 유지 | `VDETER/results/runs/stage7-software-massroc-20260830/` |
| `tools/run_stage5_tests.py --run-id stage5-massroc-integration-20260830` | **gate=PASS** — Stage 5 모듈 188건 실패 0·오류 0·skip 0 + **Stage 0~4 전체 pytest 11,550건 실패 0·오류 0·skip 11** (3,343s), 총 4,411s | `VDETER/results/runs/stage5-massroc-integration-20260830/` |

기존 skip 11건 사유(전부 기존·환경/장비 게이트, 신규 skip 없음):

- 8 × `NumPy 결속 환경(Pi)에서 실행` — Stage 7 CFO 수치 시험, Pi 전용
- 1 × `run only after TC-RADIO-012/013 physical hardware PASS` — 실장비 게이트
- 2 × `VDETER_IMPAIRMENT_FULL=1일 때만 case당 random 64 Packet 실행` — opt-in 확장 스윕

Stage 6(Link 17/19) 회귀는 위 Stage 0~4 pytest 스윕의 link/wideband unit·integration
시험으로 포함 실행됐다. 별도 대규모 DSP 게이트(run_phy_awgn 등)는 동결 당시 evidence
(`VDETER/results/`)로 유지되며 이번 변경(VDETER/ 무수정)의 영향 범위 밖이다.

## B210 실물시험

**수행하지 않음** — 3노드(Pi 5 + B210 3대) 장비에 이 세션에서 접근할 수 없음.
소프트웨어 검증까지 완료했고, B210 실물시험은 main 병합 전 남은 blocker 항목이다.
