# 팀 main → AES-256-GCM 통합 결과 (2026-09-17)

팀 `integration/aes256-gcm-vdeter`에 최신 main 구조를 병합하고 기존 AES-256-GCM 기능을 유지했다.

- 수정 전 integration: `3fdf0011bc7e8de177d5ce59f3fc66b71ba99925`
- 입력 팀 main: `f1879a8b7b9b7d119931b8655b6477bcf038e296`
- 코드 병합 커밋: `cbd06e4128942a39985e6fdd237be6d252e31d59` (첫 부모는 기존 integration, 둘째 부모는 팀 main)
- 확인된 수정 전 개인 원격 백업: [`backup/pre-main-merge-20260917-200743`](https://github.com/briel241/voyager_backup/tree/backup/pre-main-merge-20260917-200743), SHA는 수정 전 integration과 정확히 일치.

## 주요 수정

- main의 `vdeter.b210_system`, `coordinator_main.py`, `endpoint_launcher.py`, `mass_roc_workload_controller.py` 구조에 기존 AES 보안 의존성·실험 전달·수신 검증을 결합했다.
- 하드웨어 비교 도구는 현재 `VDETER/apps/coordinator_main.py`를 참조한다. 실제 파일 존재 및 모드 전달을 장비 없이 검사한다.
- 새 `mass-roc-session-cold`에도 기존 MASS/ROC의 ENFORCED 정책을 적용한다. DISABLED 거부는 장치 로딩·spawn 전에 검증한다.
- main 하니스의 원래 바이트·줄바꿈을 보존하고 AES 의존성 두 줄만 추가했다.
- 보안 모듈 `VDETER/src/vdeter/security`는 수정 전 integration과 동일하다. 암호화·복호화, 태그 검증, Nonce/Counter, 영속 상태와 replay 처리를 유지한다.
- B210 fake worker 조립에서 ENFORCED 왕복, 재시작 후 Counter 증가, 태그 오염 거부, 중복 replay 거부를 추가 검증한다.
- coordinator는 main과 비교해 정확히 네 함수만 변경됐다: `spawn_and_connect`, `_spawn_mass_roc_controller`, `run_mass_roc_service`, `main`. 해당 테스트 고정 해시는 이전 값과 근거를 남겼으며 문장·주석 변조 거부 검증을 확장했다. production qualification 검증과 과거 reference는 그대로다.
- 기존 main 테스트의 환경변수 복원 누락을 수정해 다른 시험에 디버그 설정이 남지 않게 했다.

## 장비 없는 검증

| 범위 | 실제 실행 결과 |
|---|---|
| AES/B210 및 관련 단위·계약 | 190 passed, 1 warning, 95 subtests passed in 6.55s |
| Stage5 lifecycle 통합 | 13 passed, 1 warning, 3 subtests passed in 0.73s |
| 앱 전체 회귀 | 472 passed, 36 skipped in 275.13s (0:04:35) |
| Stage5/wideband/Stage7/Stage8 소프트웨어 연결 | 102 passed, 57 subtests passed in 1029.29s (0:17:09) |

최종 검증은 CPython 3.12.10, pytest 9.1.1, cryptography 50.0.1, numpy 2.5.3에서 실행했다.
Windows에서 POSIX/bash/socket 등에 의존하는 건너뛴 시험은 통과로 기록하지 않는다.
기존 main에서도 재현된 영어 출력 검사는 한글 임시 경로 때문에 실패했으며, 검증 조건을 유지한 채 ASCII 임시 경로에서 재실행해 확인했다.
package-from-HEAD 검사는 실제 병합 커밋 후 재검증했다. 수정 중 파일 해시가 바뀐 초기 실행은 최종 통과 근거로 사용하지 않는다.
실제 B210 연결·장비 탐색·RF 송수신·실장비 명령은 실행하지 않았다. 하드웨어 자격/성능 판정은 NOT_RUN이다.

## 보존 및 업로드 범위

작업 checkout은 `voyager-team-integration`이며 시작 시 깨끗했고 팀 integration 최신 SHA와 같았다.
다른 checkout의 문서·실험·hardware-test 미커밋 변경은 별도 ZIP/patch/SHA-256 manifest로 보존하고 포함하지 않았다.
실제 비밀키·인증정보·생성 DB는 커밋 대상이 아니다.
팀 main과 로컬 main에는 쓰기 작업을 하지 않는다. 완성 커밋은 새 개인 post 백업에 먼저 올려 SHA를 확인한 뒤, 팀 integration을 재fetch하고 단일 브랜치 일반 push만 수행한다.
최종 post 백업 이름·최종 SHA·원격 일치 확인과 원본 작업 보존 위치는 작업 공간의 `main-merge-audit/publication.json`, `initial_state.json`, `pre-existing-work.json`, `pre-team-integration.bundle`에 기록한다.
