"""Radio Port (`stage0_baseline.md` §5.3, §9.2, 상위 명세 §7.6).

`FakeRadioAdapter`, `SoftwareRadioAdapter`와 `B210RadioAdapter`는 같은 논리 계약을
구현한다. Radio는 Packet, Message, Link ID 정책, CRC, FEC 또는 payload를 해석하지 않고
sample과 clock 입출력, 장치 수명주기만 담당한다.

sample dtype, buffer ownership, backpressure, 최소 lead time과 clock uncertainty 한계는
단계 3 Radio 기준선에서 확정한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.radio import (
    RadioConfigurationApplied,
    RadioConfigureRequest,
    RadioRxAccepted,
    RadioRxRequest,
    RadioStarted,
    RadioStartRequest,
    RadioStatusSnapshot,
    RadioStatusRequest,
    RadioStopped,
    RadioStopRequest,
    RadioTxAccepted,
    RadioTxRequest,
)
from ..common.results import InternalResult


@runtime_checkable
class RadioAdapter(Protocol):
    """PHY ↔ Radio 경계 계약. 모든 adapter가 같은 데이터 의미를 제공한다."""

    def configure(
        self, request: RadioConfigureRequest
    ) -> InternalResult[RadioConfigurationApplied]:
        """`InternalResult[RadioConfigurationApplied]`를 반환한다."""

    def start(self, request: RadioStartRequest) -> InternalResult[RadioStarted]:
        """`InternalResult[RadioStarted]`를 반환한다."""

    def stop(self, request: RadioStopRequest) -> InternalResult[RadioStopped]:
        """`InternalResult[RadioStopped]`를 반환한다."""

    def schedule_tx(self, request: RadioTxRequest) -> InternalResult[RadioTxAccepted]:
        """`InternalResult[RadioTxAccepted]`를 반환한다. 실제 완료는 event로 통지한다."""

    def request_rx(self, request: RadioRxRequest) -> InternalResult[RadioRxAccepted]:
        """`InternalResult[RadioRxAccepted]`를 반환한다."""

    def status(self, request: RadioStatusRequest) -> InternalResult[RadioStatusSnapshot]:
        """`InternalResult[RadioStatusSnapshot]`을 반환한다."""
