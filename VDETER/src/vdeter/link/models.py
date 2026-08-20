"""Link 계층 내부 model (`stage0_baseline.md` §4.1.1).

공개 경계 DTO는 `common.contracts.link`와 `common.contracts.message`에 있다
(decision record `DR-STAGE0-001`).

Sequential ID 할당·재사용 정책, Packet 용량, padding, CRC와 fragment 용량은 단계 1 Link
기준선이 소유한다. 이 module은 단계 0에서 필요한 계층 내부 표현만 정의한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId


@dataclass(frozen=True, slots=True)
class LinkCapabilities:
    """endpoint 설정이 허용한 Link ID 집합. 표준 profile 값이 아니다."""

    supported_link_ids: tuple[LinkId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.supported_link_ids, tuple) or any(
            not isinstance(link_id, LinkId) for link_id in self.supported_link_ids
        ):
            raise InvalidValueError("LinkCapabilities.supported_link_ids: LinkId tuple이어야 합니다")

    def supports(self, link_id: LinkId) -> bool:
        return link_id in self.supported_link_ids
