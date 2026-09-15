"""B210 시스템의 **런타임 제어면**.

endpoint launcher·scenario controller·workload controller가 실행 중에 import하는
제어 경로다(시험 전용 harness가 아니다):

- `adapter`     : §16.2 TEST adapter socket op 으로 public Service 를 구동한다.
- `controller`  : 시나리오 제어면(RR/RESEARCH 자극과 판정 수집).
- `poc_device`  : launcher 가 소유한 장치에 대한 PoC device seam.

이전 이름은 `testing`이었으나, launcher(진입점)가 런타임에 이 module들을 import하므로
"시험 전용"이라는 뜻이 실제와 어긋났다. 시험 계획·evidence 자료는 `..testing`에 남아 있다.
"""

from .adapter import ServiceApiTestAdapter
from .controller import ScenarioController

__all__ = (
    "ScenarioController",
    "ServiceApiTestAdapter",
)
