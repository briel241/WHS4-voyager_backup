"""Stage 7 TEST-only harness (`stage7` §15, §16.2, §17).

이 package는 시험 build/composition에서만 조립한다. core Runtime, Service,
Presentation, Link, MAC, PHY, Radio 또는 정상 application entrypoint가 이
package를 import해서는 안 된다 (import 방향: TEST→launcher composition/public
Service).
"""

from .plan import (
    P1_MEMBER_PLAN_TABLE,
    Stage7P1QualificationMemberPlan,
    Stage7ResolvedTestPlan,
    Stage7TestPlanLoader,
)
from .adapter import Stage7ServiceApiTestAdapter
from .controller import Stage7ScenarioController

__all__ = (
    "P1_MEMBER_PLAN_TABLE",
    "Stage7P1QualificationMemberPlan",
    "Stage7ResolvedTestPlan",
    "Stage7ScenarioController",
    "Stage7ServiceApiTestAdapter",
    "Stage7TestPlanLoader",
)
