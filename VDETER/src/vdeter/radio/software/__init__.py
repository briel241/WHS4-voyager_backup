"""`SoftwareRadioAdapter` 골격 (`stage0_baseline.md` §9.2, §14 완료조건 6).

단계 0에서는 package와 class 골격, 그리고 구조화된 미구현 오류 경로만 제공한다. 실제
software sample I/O, timestamp 재현, RX window와 유실·불연속 주입은 단계 3 Radio
기준선이 구현한다.

이 module의 import는 GNU Radio, UHD, PMT 또는 장치 연결을 필요로 하지 않는다.
"""

from __future__ import annotations

from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.results import internal_error
from ..models import RadioAdapterKind

ADAPTER_KIND = RadioAdapterKind.SOFTWARE.value

_NOT_IMPLEMENTED_MESSAGE = (
    "SoftwareRadioAdapter는 단계 3 Radio 기준선에서 구현한다. 단계 0에서는 골격만 제공한다."
)


def _not_implemented(operation: str, correlation: Correlation = Correlation()):
    return internal_error(
        Layer.RADIO,
        ErrorCode.NOT_IMPLEMENTED,
        ErrorCategory.UNSUPPORTED,
        _NOT_IMPLEMENTED_MESSAGE,
        correlation=correlation,
        details=Details.of(adapter_kind=ADAPTER_KIND, operation=operation, stage="stage3"),
    )


class SoftwareRadioAdapter:
    """`RadioAdapter` 계약을 따르는 software adapter 골격.

    모든 method는 구조화된 `NOT_IMPLEMENTED` 오류를 반환한다. 예외를 던지거나 성공으로
    위장하지 않는다.
    """

    __slots__ = ()

    def configure(self, request):
        return _not_implemented("configure")

    def start(self, request):
        return _not_implemented("start")

    def stop(self, request):
        return _not_implemented("stop")

    def schedule_tx(self, request):
        burst_id = getattr(request, "burst_id", None)
        correlation = Correlation(burst_id=burst_id) if burst_id is not None else Correlation()
        return _not_implemented("schedule_tx", correlation)

    def request_rx(self, request):
        return _not_implemented("request_rx")

    def status(self, request):
        return _not_implemented("status")
