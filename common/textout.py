"""직접 실행 스크립트의 출력 인코딩 안전장치.

Windows 콘솔(CP949)에서 파이프·리디렉션으로 실행하면 stdout 인코딩이 cp949가
되어, 긴 대시(—)·체크 표시(✅) 같은 문자가 `UnicodeEncodeError`로 시험 로직
자체를 중단시킨다. `python -X utf8`을 붙이면 해결되지만 사용자가 매번 기억할
수 없으므로, 직접 실행 진입점이 이 모듈의 `ensure_utf8_output()`을 가장 먼저
호출해 같은 효과를 낸다.

동작
    - stdout·stderr를 UTF-8 + `errors="replace"`로 재구성한다.
    - 재구성이 불가능한 스트림(캡처 객체 등)은 그대로 두되, 이미 pytest가
      캡처 중이면 인코딩 문제가 없으므로 안전하다.
    - Linux/macOS에서는 대부분 이미 UTF-8이라 사실상 no-op이다.

시험을 skip하거나 출력 문자를 지우는 방식이 아니라, 어떤 콘솔에서든 같은
로직이 끝까지 실행되게 하는 것이 목적이다.
"""

from __future__ import annotations

import sys


def ensure_utf8_output() -> None:
    """stdout·stderr를 UTF-8(errors=replace)로 재구성한다. 실패해도 조용히 넘어간다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 닫힌/특수 스트림
            pass
