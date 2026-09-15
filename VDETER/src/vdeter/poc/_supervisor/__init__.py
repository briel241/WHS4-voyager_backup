"""Stage 8 PoC 연결 API의 production **private** 구현.

VDETER runtime(PoC workload supervisor)만 이 package를 import한다. public SDK
(`vdeter.poc.api.sdk`)와 Stage 4·5 branch는 이 package를 직접 import하지
않는다. 이 package는 public 계약(`api`)을 import해 server 측 socket·spawn·
cleanup·evidence를 구현한다.
"""

from __future__ import annotations

__all__ = ()
