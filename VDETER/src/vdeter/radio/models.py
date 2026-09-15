"""Radio 계층 내부 model (`stage0_baseline.md` §4.1.1, §9).

공개 경계 DTO는 `common.contracts.radio`에 있다 (decision record `DR-STAGE0-001`).
"""

from __future__ import annotations

from enum import Enum


class RadioAdapterKind(str, Enum):
    """endpoint 시작 시 선택하는 Radio adapter 종류.

    - `FAKE`: 단계 0 interface 시험용. 장치를 열지 않는다. test scope 전용이다.
    - `SOFTWARE`: 단계 3에서 구현할 software sample I/O adapter.
    - `B210`: 단계 3에서 구현할 GNU Radio/UHD 기반 USRP B210 adapter.
    """

    FAKE = "FAKE"
    SOFTWARE = "SOFTWARE"
    B210 = "B210"


#: 운영 설정에서 선택할 수 없는 test scope adapter.
TEST_SCOPE_ADAPTER_KINDS = frozenset({RadioAdapterKind.FAKE})
