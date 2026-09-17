"""runner가 사용하는 workload 연결 seam(Protocol) 정의.

runner(§5.1 실행 lifecycle)는 이 좁은 seam으로 Stage 3 connection layer를 구동한다.
RF-off에서는 SupervisorEngine + SdkEngine loopback(`testing.execution_fakes.LoopbackConnection`)이
이 seam을 구현하고, Stage 6 통합에서는 실제 `PocSupervisor` wrapper가 같은 seam을 구현한다.
이렇게 "runner의 실행 로직"과 "message가 오가는 방식"을 분리한다.

seam은 message 배관만 담당한다. lease·verdict·scenario 우선순위 기록·cleanup 판정은 runner가
한다(그것이 Stage 4 실행 계약의 본체다).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..api.types import ObservationRecord


@runtime_checkable
class WorkloadConnection(Protocol):
    """한 workload의 연결 배관 seam(§3.1, §5.1)."""

    def start(self) -> None:
        """spawn/handshake를 태워 `ARMED`까지 진행한다(§5.1). 실패는 infrastructure_error로 남긴다."""
        ...

    @property
    def armed(self) -> bool:
        """`on_start()` 성공 후 workload 시작 가능(ARMED) 여부(§5.5)."""
        ...

    @property
    def running(self) -> bool:
        """workload event 처리 가능(RUNNING) 여부."""
        ...

    def emit_event(self, event_type: str, body: dict) -> None:
        """scenario/step event 하나를 client에 전달하고 callback을 구동한다(§8)."""
        ...

    def emit_observation(self, record: ObservationRecord) -> None:
        """관측 하나를 처리한다(§8.4). NOT_TUNED면 event를 만들지 않고 counter만 기록한다."""
        ...

    def pump(self) -> None:
        """대기 중인 message를 양방향으로 교환해 quiescent 상태로 만든다."""
        ...

    def poll_new_results(self) -> list:
        """직전 poll 이후 새로 관측된 `ACTION_RESULT` body 목록(accepted/rejected)."""
        ...

    def submitted_action(self, request_id: str) -> dict | None:
        """request_id로 제출된 action body(§9.4)를 돌려준다. 없으면 None."""
        ...

    def provide_terminal(self, request_id: str, *, action_execution: str,
                         target_effect: str | None) -> None:
        """accepted action의 terminal을 확정해 client에 전달한다(§9.1 ACTION_TERMINAL)."""
        ...

    def client_idle(self) -> bool:
        """client에 pending descriptor·in-flight·unresolved가 모두 없는지."""
        ...

    def finish(self) -> None:
        """workload terminal 뒤 finish→on_finish→close handshake를 태운다(§5.1, §13)."""
        ...

    def close(self) -> dict:
        """cleanup을 수행하고 잔존 검사 report(process/socket/queue/ring/inflight)를 돌려준다(§13)."""
        ...

    @property
    def infrastructure_error(self):
        """확정된 infrastructure 오류(:class:`PocError`) 또는 None(§11.2)."""
        ...

    def armed_reached(self) -> bool:
        """workload 중 한 번이라도 ARMED에 도달했는지(pre-ARMED 오류 구분용)."""
        ...

    def evidence(self) -> dict:
        """연결 evidence(협상·counter·high-water·delivery matrix 등, §16.1)."""
        ...


__all__ = ("WorkloadConnection",)
