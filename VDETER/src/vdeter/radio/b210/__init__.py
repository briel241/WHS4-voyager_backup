"""`B210RadioAdapter` 골격 (`stage0_baseline.md` §9.2, §9.3, §14 완료조건 6).

단계 3에서 구현할 첫 B210 backend는 headless·programmatic GNU Radio UHD adapter다.
Link, MAC, Presentation과 Service protocol 로직은 GRC flowgraph에 넣지 않는다.

단계 0에서는 package·class 골격과 의존성 격리만 제공한다.

- 이 module의 import는 GNU Radio나 UHD가 없어도 성공해야 한다.
- 의존성 부재와 미구현은 예외가 아니라 구조화된 오류로 보고한다.
- 실제 장치 open, streaming과 timed command는 단계 3에서 구현한다.
"""

from __future__ import annotations

import importlib.util

from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.results import internal_error
from ..models import RadioAdapterKind

ADAPTER_KIND = RadioAdapterKind.B210.value

#: 단계 3에서 필요한 외부 module. 단계 0에서는 존재 여부만 확인하고 import하지 않는다.
REQUIRED_MODULES = ("gnuradio", "uhd")

_NOT_IMPLEMENTED_MESSAGE = (
    "B210RadioAdapter는 단계 3 Radio 기준선에서 구현한다. 단계 0에서는 골격만 제공한다."
)


def missing_dependencies() -> tuple[str, ...]:
    """설치되지 않은 필수 module 이름을 반환한다. import는 하지 않는다."""

    missing = []
    for name in REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return tuple(missing)


def _dependency_error(operation: str, missing: tuple[str, ...], correlation: Correlation):
    return internal_error(
        Layer.RADIO,
        ErrorCode.RADIO_DEPENDENCY_MISSING,
        ErrorCategory.DEVICE,
        "B210 adapter에 필요한 외부 dependency가 없습니다",
        correlation=correlation,
        details=Details.of(
            adapter_kind=ADAPTER_KIND,
            operation=operation,
            missing_modules=tuple(missing),
        ),
    )


def _not_implemented(operation: str, correlation: Correlation = Correlation()):
    missing = missing_dependencies()
    if missing:
        return _dependency_error(operation, missing, correlation)
    return internal_error(
        Layer.RADIO,
        ErrorCode.NOT_IMPLEMENTED,
        ErrorCategory.UNSUPPORTED,
        _NOT_IMPLEMENTED_MESSAGE,
        correlation=correlation,
        details=Details.of(adapter_kind=ADAPTER_KIND, operation=operation, stage="stage3"),
    )


class B210RadioAdapter:
    """`RadioAdapter` 계약을 따르는 USRP B210 adapter 골격.

    dependency가 없으면 `RADIO_DEPENDENCY_MISSING`, 있으면 `NOT_IMPLEMENTED` 오류를
    구조화된 결과로 반환한다. 어느 경우에도 예외를 던지거나 성공으로 위장하지 않는다.
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
