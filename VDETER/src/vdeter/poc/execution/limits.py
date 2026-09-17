"""Stage 8 4단계 실행 layer의 상한·deadline 단일 정의(실행 명세 §5.2, `S8-POC-REQ-26`).

두 출처를 명확히 구분한다.

1. **Stage 8 신규 정책 상한** — 기존 서비스 세션 코드에 없던 상한. 이 module이 유일한 정의
   위치이며 resident session이 신규 상수로 강제한다.
2. **기존 서비스 세션 코드 상한** — `apps/service_session.py`의 `SESSION_BRINGUP_TIMEOUT_S`·
   `WORKLOAD_TIMEOUT_S`·`SESSION_CLOSE_TIMEOUT_S`가 authoritative 정의다. Stage 4는 그
   값을 **바꾸지 않으며**, RF-off resident-session 모델에 주입할 기본값으로만 mirror한다
   (Stage 6 통합 시 실제 서비스 세션 상수로 주입). 여기서 재선언하는 것은 새 권위가 아니라
   문서화된 기본값이며, 값이 다르면 그것은 결함이다.

어느 상한도 PoC 활동(활성 workload·rescan·callback·action 제출)으로 연장·재무장하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Stage 8 신규 정책 상한 (이 module이 유일한 정의) -------------------------

#: coordinator 시작부터 bring-up·전체 workload·STOP·cleanup까지의 절대 총 상한.
SESSION_ABSOLUTE_DEADLINE_S = 7200.0  # 2시간

#: `SESSION READY`에서 다음 workload 요청이 없을 때의 idle 상한. 정상 workload 뒤 다시 시작.
READY_IDLE_TIMEOUT_S = 2700.0  # 45분 (bring-up timeout과 값은 같지만 별개 상한)


# --- 기존 서비스 세션 코드 상한 (mirror 기본값; authoritative 정의는 apps/service_session.py) --

#: `apps/service_session.py:SESSION_BRINGUP_TIMEOUT_S`와 동일해야 하는 mirror 기본값.
SERVICE_BRINGUP_TIMEOUT_S = 2700.0
#: `apps/service_session.py:WORKLOAD_TIMEOUT_S`와 동일해야 하는 mirror 기본값.
SERVICE_WORKLOAD_TIMEOUT_S = 900.0
#: `apps/service_session.py:SESSION_CLOSE_TIMEOUT_S`와 동일해야 하는 mirror 기본값.
SERVICE_CLOSE_TIMEOUT_S = 600.0


# --- catalog scan 상한 (실행 명세 §6.1, `S8-POC-REQ-22`) ----------------------

#: 한 scan에서 검사하는 최대 package 수.
MAX_CATALOG_PACKAGES = 64
#: Python script 파일 최대 크기.
MAX_SCRIPT_BYTES = 1 * 1024 * 1024  # 1 MiB
#: manifest(.poc.toml) 파일 최대 크기.
MAX_MANIFEST_BYTES = 64 * 1024  # 64 KiB


@dataclass(frozen=True, slots=True)
class ServiceSessionTimeouts:
    """resident session에 주입하는 기존 서비스 세션 코드 상한(불변).

    기본값은 위 mirror 상수다. Stage 6 통합에서는 실제 서비스 세션 상수를 넣는다.
    Stage 4는 이 값을 완화·연장하지 않으며 fake-clock 경계 시험으로만 확인한다.
    """

    bringup_s: float = SERVICE_BRINGUP_TIMEOUT_S
    workload_s: float = SERVICE_WORKLOAD_TIMEOUT_S
    close_s: float = SERVICE_CLOSE_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """resident session에 적용하는 상한 묶음(신규 정책 + 기존 서비스 세션 코드 상한).

    두 신규 상한은 기존 코드 상한을 대체하지 않고 그 위에 함께 적용된다(§5.2).
    """

    session_absolute_s: float = SESSION_ABSOLUTE_DEADLINE_S
    ready_idle_s: float = READY_IDLE_TIMEOUT_S
    service: ServiceSessionTimeouts = ServiceSessionTimeouts()


__all__ = (
    "SESSION_ABSOLUTE_DEADLINE_S",
    "READY_IDLE_TIMEOUT_S",
    "SERVICE_BRINGUP_TIMEOUT_S",
    "SERVICE_WORKLOAD_TIMEOUT_S",
    "SERVICE_CLOSE_TIMEOUT_S",
    "ServiceSessionTimeouts",
    "SessionPolicy",
    "MAX_CATALOG_PACKAGES",
    "MAX_SCRIPT_BYTES",
    "MAX_MANIFEST_BYTES",
)
