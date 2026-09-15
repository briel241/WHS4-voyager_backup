"""
관찰 도구

구상도의 "어떻게 변하는지 확인하는 도구"다. 기록(recorder)과 표시(report)를
나눠 두어, 같은 기록을 콘솔로도 HTML 보고서로도 낼 수 있다.
"""

from .recorder import (
    EVENT_COMMAND,
    EVENT_LINK,
    EVENT_NOTE,
    EVENT_REJECT,
    EVENT_ROUTE,
    OUTCOME_DELIVERED,
    OUTCOME_LOST,
    OUTCOME_NO_SLOT,
    OUTCOME_REJECTED,
    OUTCOME_LABELS,
    Event,
    Recorder,
    Sample,
    Transfer,
    render_console,
)
from .report import write_report

__all__ = [
    "Recorder",
    "Sample",
    "Transfer",
    "Event",
    "render_console",
    "write_report",
    "EVENT_COMMAND",
    "EVENT_ROUTE",
    "EVENT_LINK",
    "EVENT_REJECT",
    "EVENT_NOTE",
    "OUTCOME_DELIVERED",
    "OUTCOME_LOST",
    "OUTCOME_NO_SLOT",
    "OUTCOME_REJECTED",
    "OUTCOME_LABELS",
]
