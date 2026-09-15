"""Stage 8 6단계(전반부) — PoC 실행 layer(Stage 4)와 유선 공유매체 방사 provider(Stage 5)를
실제 사용자 진입점 형태의 production 실행 경로로 결속하는 **통합 전용** 코드.

이 subpackage는 Stage 4(`execution`)와 Stage 5(`shared_medium`)를 처음으로 한 실행 경로에
연결한다(실행 명세 §5, §9~§13; WSM §6~§9; roadmap §6 통합 순서 1~2).

연결하는 것:

- device 경계 adapter(`device_boundary`): Stage 5 provider의 RadioBurstSink seam을 Stage 7 group
  TX 경계(`retune_tx_lo` + `submit_tx_group`)에 **연결 가능한** production adapter로 감싼다.
  RF-off에서는 실장비 대신 software group-TX harness를 **명시적으로** 주입한다.
- provider 조립(`assembly`): `SharedMediumActionProvider.for_production`을 fail-closed로 조립한다.
  시험용 scheduler·waveform factory·arbiter를 **조용히 기본값으로 쓰지 않는다**(§4).
- terminal 유도(`tx_executor`): accepted action의 terminal을 fake effect 모델이 아니라 **실제
  provider outcome**(parent/child terminal 취합)에서 유도한다.
- 연결(`supervisor_connection`): Stage 4 runner의 `WorkloadConnection` seam을 **실제
  `PocSupervisor`**(실 subprocess child·실 Unix socket·SDK·engine)로 구현하고, 관측 event의
  observation_id를 scheduler trigger 등록에 결속한다(반응형 인과성).
- resident runtime(`runtime`): resident session deadline 모델을 **실 child 종료**(CLOSE→SIGTERM→
  SIGKILL)에 결속한다.
- 진입 driver(`entry`): scenario 선택 → catalog → immutable snapshot → workload runner →
  세 독립 판정 → renderer의 `vdeter run --poc-dir` 형상 driver.

구조 원칙:

- 기존 Stage 4/5 파일을 **수정하지 않는다**(통합 전용 신규 파일만 추가).
- 이 package를 import하는 것만으로 process·thread·socket·timer·disk write가 생기지 않는다
  (기능 OFF 구조적 0). PoC workload가 명시적으로 실행될 때만 자원이 생긴다.
- **실 RF atomicity·실 B210 group TX·frozen PHY waveform·common-LO tuning·실 MAC scheduler·
  실 arbiter·실 RX window·재qualification은 Stage 6 실장비 단계**다. RF-off에서는 장비 경계만
  software harness로 대체하며, software commit의 성공을 실제 RF atomicity 증명으로 주장하지 않는다.
"""

from __future__ import annotations

__all__ = ()
