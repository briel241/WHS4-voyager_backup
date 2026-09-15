"""(호환) 옛 공개 경로 `vdeter.stage8`. 실제 구현은 `vdeter.poc`에 있다.

2-3 구조 정리에서 이 package는 기능 이름 `vdeter.poc`로 옮겼다. 외부 PoC 저자가
이미 작성한 코드를 위해 **공개 surface(`poc_api` -> `vdeter.poc.api`)만** 옛 경로로
접근할 수 있게 남긴다. 구현을 복제하지 않고 새 module 객체를 그대로 가리킨다.

비공개 내부(`_supervisor`·`execution`·`integration`·`shared_medium`·`testing`)는
공개한 적이 없으므로 호환 경로를 제공하지 않는다. 포괄 호환 package를 만들면
public/private 경계가 무의미해진다.

이 package를 import하는 것만으로 하위 module이 적재되지 않는다(기능 OFF 구조적 0 유지).

제거 조건(날짜만으로 자동 제거하지 않는다):
1. 외부 사용자가 새 경로로 전환했음을 확인하고,
2. 변경 안내를 공지한 뒤,
3. 별도 제거 승인을 받는다.
"""

from __future__ import annotations

__all__ = ()
