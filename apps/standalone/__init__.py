"""VDETER standalone 실행 계층 (Phase I).

`vdeter` 단일 명령(setup / qualify / run / results)으로 대상 기계(Ubuntu
arm64/amd64)에서 host·SSH·Git 없이 Stage 7 시스템을 기동·자격·운용한다.

이 패키지는 orchestration 계층이다 — coordinator·endpoint launcher·RF/PHY/
MAC·판정 코드는 수정하지 않고 로컬 자식 프로세스로 spawn하며, 기존 세션
디렉터리 파일 IPC(requests/·results/·shutdown·done.rc) 계약을 그대로 쓴다.

설계 명세와 결정 근거: 같은 디렉터리의 README.md (Phase I W1).
기존 host 실행 경로(apps/scenario_runner.py --transport stage7)는 Phase F~H
evidence 재현·감사 전용으로 무수정 보존된다.
"""
