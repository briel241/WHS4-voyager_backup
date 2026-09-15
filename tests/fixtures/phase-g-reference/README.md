# Phase G qualification reference fixture

clean checkout에서 host/v1 계약 시험(`tests/test_stage7_host_fail_closed.py`,
`tests/test_stage7_persistent_session.py`, `tests/test_stage7_service_cli.py`)이
실행되도록 고정한 Phase G 자료다 (2026-09-05).

- `qualification-reference.json` + `.sha256`: phase-g-formal-20260902b의 reference를
  byte-identical로 복사 (SHA cbe0fc9e…). 계약 A는 이 무수정 reference로 검증한다.
- `gain_artifact.json`, `gain_artifact_input.json`: phase-f-requal-20260902 rr-p1b의
  gain artifact (reference의 gain SHA와 일치).
- `stage7-profile.json`: 같은 profile에서 Pi host를 `pi@pi.invalid`로 익명화하고
  gain 경로·evidence_root를 fixture/ignored 경로로 바꾼 사본. 개인키·비밀은 없다.
