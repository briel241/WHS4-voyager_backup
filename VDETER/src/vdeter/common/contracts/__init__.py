"""계층 경계를 넘는 공개 DTO (`stage0_baseline.md` §5.3, decision record `DR-STAGE0-001`).

`stage0_baseline.md` §4.1.1(2)(3)은 계층 package가 서로를 import하지 않도록 제한한다.
따라서 두 계층 사이를 오가는 공개 DTO는 이 package에 정의하고, 각 계층 package는
`common`만 import한다. 계층별 책임의 판정 원본은 상위 명세 §6과 각 단계 기준선이다.
`TC-ARCH-002`와 `TC-ARCH-004`는 구현에서 관찰 가능한 import·상태 경계를 검사한다.

module 사이의 정적 의존 방향은 다음과 같으며 순환을 만들지 않는다.

```text
radio → phy → mac → link
message → mac, link
presentation, service: 다른 contract module에 의존하지 않음
```
"""

from __future__ import annotations
