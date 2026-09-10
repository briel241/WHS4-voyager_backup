"""Stage 8 PoC 연결 API (`stage8_poc_connection_api_spec.md` VERSION 1.3).

이 package는 VDETER와 workload별 외부 Python PoC process 사이의 좁고 versioned된
연결 경계만 담는다. Stage 4(PoC 실행)와 Stage 5(유선 공유매체 방사)는 이 경계의
**public** surface(`vdeter.stage8.poc_api`)만 사용하며 private 구현
(`vdeter.stage8._supervisor`)을 직접 import하지 않는다.

구조 원칙(명세 §3, §19):

- `poc_api`  : Stage 4·5와 외부 PoC SDK가 사용하는 public 계약(schema·type·SDK·seam).
- `_supervisor` : VDETER runtime만 import하는 production private 구현(server·socket·spawn·cleanup).
- `testing`  : 시험과 후속 track이 쓰는 test double(fake clock/provider/child).

이 package를 import하는 것만으로 process·thread·socket·timer·disk write가 생기지
않는다(기능 OFF 구조적 0). 기존 Stage 7 runtime은 이 package를 import하지 않으며,
PoC workload가 명시적으로 supervisor를 생성할 때만 자원이 생긴다.
"""

from __future__ import annotations

__all__ = ()
