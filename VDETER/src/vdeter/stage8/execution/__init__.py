"""Stage 8 4단계 — PoC 실행(execution) layer.

이 subpackage는 Stage 3의 **public** `poc_api`와 supervisor seam(`_supervisor.engine`/
`PocSupervisor`) 위에 PoC workload 실행 계약을 구현한다(실행 명세 §5~§13). 담는 것:

- `/poc` catalog 비재귀 scan·검증(§6.1)
- `.poc.toml` manifest fail-closed 검증(§6.3)
- scenario-first PoC 선택과 명시적 rescan(§6.1)
- workload 시작 직전 immutable snapshot과 SHA-256 결속(§5.3)
- resident session lifecycle와 신규 정책 상한(§5.2)
- MASS-B `POC_ACTOR` lease의 session-local 배타성(§4.2)
- on_start→ARMED→event→action→terminal→on_finish→cleanup 실행 lifecycle(§5.1)
- scenario/PoC/attack 독립 판정(§11)과 종료 후 bounded renderer(§12)

구조 원칙(명세 §3, roadmap §7):

- Stage 5(`feature/stage8-shared-medium`)의 private module을 import하지 않는다.
- shared-medium·common LO·DSP·fan-out·IQ collision·실제 RF는 구현하지 않는다.
  RF·scheduler는 명세형 fake endpoint/scheduler spy(`testing.execution_fakes`)로 대체한다.
- 이 package를 import하는 것만으로 process·thread·socket·timer·disk write가 생기지
  않는다(기능 OFF 구조적 0, §3.2). PoC workload가 명시적으로 선택·실행될 때만 자원이 생긴다.
- 기존 Stage 7 runtime은 이 package를 import하지 않는다(정적 경계 시험이 강제).
"""

from __future__ import annotations

__all__ = ()
