"""resident session deadline 모델을 **실 child 종료**에 결속한다(실행 명세 §5.2, `S8-POC-REQ-26`).

Stage 4 `ResidentSession`은 순수 상태·deadline 모델이다(bring-up 2700 / workload 900 / close 600 =
기존 Stage 7 코드 상한, session absolute 7200 / READY idle 2700 = Stage 8 신규 정책 상한). 이 runtime은
그 모델을 **실 `SupervisorConnection`**(실 subprocess child)에 결속해, deadline 만료가 살아있는 실
child의 CLOSE→SIGTERM→SIGKILL 종료를 **구동**하게 한다. 순수 모델 시험만으로 enforcement를 완료로
보지 않으며, 실제 제어 흐름(만료 → 실 child 종료 → 잔존 0)을 검증한다.

시간은 `session.clock`으로 측정한다(시험은 fake clock으로 단축). production의 900/600초 **권위 값**은
`SessionPolicy`(기본은 Stage 7 mirror; Stage 6 통합에서 실제 Stage 7 세션 상수 주입)로 결속되며,
실제 900초 wall-clock 시험은 이 범위에서 수행하지 않는다(Stage 6 hard gate로 이관). 어느 deadline도
PoC 활동으로 연장·재무장하지 않는다(`ResidentSession`이 강제). `EXPIRED` 뒤에도 close 예산(close_s)을
적용하며 재호출로 예산을 연장하지 않는다.
"""

from __future__ import annotations

from ..execution.session import ResidentSession, SessionExpired


class ResidentPocRuntime:
    """`ResidentSession` deadline을 실 child 종료 제어 흐름에 결속한다.

    workload마다 새 `SupervisorConnection`(새 process·socket·lease)을 쓰고, deadline 만료 시
    그 connection의 실 child를 종료한다. 이 runtime은 session 경계(bring-up·workload·close)에서
    deadline을 확인하고, workload 실행 중 만료 시 `enforce_deadlines`로 살아있는 child를 종료한다.
    """

    def __init__(self, session: ResidentSession) -> None:
        self._session = session

    @property
    def session(self) -> ResidentSession:
        return self._session

    @property
    def state(self) -> str:
        return self._session.state.value

    def bring_up(self) -> None:
        """coordinator 시작 → bring-up 완료(→ READY). bring-up 상한·absolute를 확인한다(§5.2)."""

        self._session.start()
        self._session.complete_bringup()

    def begin_workload(self) -> None:
        """READY에서 workload 시작. absolute·idle deadline을 먼저 확인한다(§5.2, §5.4)."""

        self._session.begin_workload()

    def end_workload(self) -> None:
        """정상 workload 완료 → READY. workload 상한 확인 후 idle timer 재시작(§5.2)."""

        self._session.end_workload()

    def enforce_deadlines(self, connection) -> tuple:
        """현재 상태의 deadline을 확인한다. 만료면 **살아있는 실 child**를 종료한다.

        반환: 만료 시 ``(Deadline, close_report)``, 아니면 ``(None, None)``. 비협조 child는
        `connection.close()`가 CLOSE→SIGTERM→SIGKILL로 escalate하고 잔존을 검사한다(§13). close
        report의 `forced_kill`로 SIGKILL 여부를, `child_alive`/`socket_file_exists` 등으로 잔존 0을
        확인한다. provider 예약·trigger 정리도 connection.close()가 함께 수행한다(§10).
        """

        try:
            self._session.check_deadlines()
            return None, None
        except SessionExpired as expired:
            report = connection.close()
            return expired.deadline, report

    def close(self, connection=None) -> dict | None:
        """정상 session close. **teardown 전에 close 시작 시각을 기록**해(§13.9 close 예산에 실 teardown
        시간이 포함됨) 그 뒤 실 child를 종료하고, 예외·인터럽트에도 session cleanup(lease 반환·close 상한
        검사)을 수행한다. 원래 예외·인터럽트를 보존한다. `begin_close`는 멱등이라 재호출로 예산이
        연장되지 않는다. `EXPIRED` 상태에서도 lease cleanup을 건너뛰지 않는다."""

        self._session.begin_close()  # teardown 전에 close 예산 시작점을 고정한다.
        error = None
        report = None
        try:
            report = connection.close() if connection is not None else None
        except BaseException as exc:  # noqa: BLE001 - 원래 예외/인터럽트를 보존하고 cleanup은 계속
            error = exc
        try:
            # close 예산(close_s)을 실 teardown 시간까지 포함해 검사한다. 초과 시 SessionExpired(CLOSE).
            self._session.complete_close()
        except Exception as expired:  # noqa: BLE001
            if error is None:
                error = expired
        if error is not None:
            raise error
        return report


__all__ = ("ResidentPocRuntime",)
