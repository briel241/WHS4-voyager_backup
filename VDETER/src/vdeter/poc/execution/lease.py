"""MASS-B `POC_ACTOR` lease의 session-local 배타 lifecycle(실행 명세 §4.2, §3.1, `S8-POC-REQ-27`).

- MASS-B(serial `3515425`)의 정상 application 역할과 `POC_ACTOR` lease는 session-local
  배타적이다. PoC workload가 선택되면 정상 역할을 중지하고 lease를 독점한다.
- workload terminal·cleanup·session close에서 정상 소유권을 기존 runtime에 반환한다.
- 실패 경로에서도 lease 잔존이 0이어야 한다(호출자가 finally에서 release).
- 영구 hardware profile·serial-role·정상 ROC·MASS-A tuning은 이 lease로 바뀌지 않는다.

이 module은 순수 상태 모델이다. 실제 radio·UHD·tuning은 Stage 6 이후 production이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: 고정 공격자 endpoint와 serial(§4.1, `POC-D02`). 이 lease로 바뀌지 않는다.
MASS_B_ENDPOINT = "MASS-B"
MASS_B_SERIAL = "3515425"


class MassBRole(str, Enum):
    """MASS-B의 session-local application 역할(§4.1). 영구 hardware 역할이 아니다."""

    NORMAL = "NORMAL"          # 정상 MASS-B application participant
    POC_ACTOR = "POC_ACTOR"    # PoC workload가 독점 lease


class LeaseError(Exception):
    """lease 배타성 위반(fail-closed)."""


@dataclass
class MassBLease:
    """MASS-B 역할/lease의 session-local 배타 소유권 모델(§4.2).

    한 시점에 정상 역할과 `POC_ACTOR` lease를 동시에 활성화하지 않는다. acquire/release는
    단조롭게 전이하며 evidence용 history를 남긴다.
    """

    endpoint: str = MASS_B_ENDPOINT
    serial: str = MASS_B_SERIAL
    role: MassBRole = MassBRole.NORMAL
    holder: str | None = None
    history: list = field(default_factory=list)

    @property
    def is_leased(self) -> bool:
        return self.role == MassBRole.POC_ACTOR

    def acquire(self, workload_id: str) -> None:
        """정상 역할을 중지하고 `POC_ACTOR` lease를 독점한다(§4.2). 중복 acquire는 오류."""

        if self.role != MassBRole.NORMAL or self.holder is not None:
            raise LeaseError(
                f"MASS-B lease가 이미 {self.holder}에 보유됨(배타성 위반)"
            )
        try:
            # Establish identity before changing role so every partial acquisition
            # can be recovered without claiming an unidentified/foreign holder.
            self.holder = workload_id
            self.role = MassBRole.POC_ACTOR
            self.history.append(("ACQUIRE", workload_id))
        except BaseException:
            if self.holder == workload_id:
                self.role = MassBRole.NORMAL
                self.holder = None
            raise

    def release(self, workload_id: str | None = None) -> bool:
        """정상 소유권을 기존 runtime에 반환한다(§4.2). 보유 중이었으면 True.

        실패·강제 종료 경로에서도 안전하게 호출할 수 있도록 미보유 상태의 release는
        no-op로 True 아닌 False를 돌려준다(잔존 0 보장용).
        """

        if self.role != MassBRole.POC_ACTOR:
            return False
        if workload_id is not None and self.holder != workload_id:
            raise LeaseError(
                f"lease 반환 workload({workload_id})가 보유자({self.holder})와 다릅니다"
            )
        released = self.holder
        self.role = MassBRole.NORMAL
        self.holder = None
        self.history.append(("RELEASE", released))
        return True

    def assert_returned(self) -> None:
        """cleanup 잔존 검사(§13.10): 정상 역할로 반환됐는지 확인. 아니면 LeaseError."""

        if self.role != MassBRole.NORMAL or self.holder is not None:
            raise LeaseError(
                f"cleanup 뒤에도 MASS-B lease 잔존(role={self.role.value}, holder={self.holder})"
            )

    def as_evidence(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "serial": self.serial,
            "role": self.role.value,
            "holder": self.holder,
            "history": [list(item) for item in self.history],
        }


__all__ = (
    "MASS_B_ENDPOINT",
    "MASS_B_SERIAL",
    "MassBRole",
    "LeaseError",
    "MassBLease",
)
