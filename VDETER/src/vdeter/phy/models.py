"""PHY 계층 내부 model (`stage0_baseline.md` §4.1.1).

공개 경계 DTO는 `common.contracts.phy`에 있다 (decision record `DR-STAGE0-001`).
Link ID별 waveform, FEC, modulation, burst parameter는 PHY Profile registry가 제공하며
값은 단계 2·6에서 확정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId


@dataclass(frozen=True, slots=True)
class PhyCapabilities:
    """endpoint 설정이 허용한 PHY Link ID 집합과 sample 설정."""

    supported_link_ids: tuple[LinkId, ...] = ()
    sample_rate_hz: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.supported_link_ids, tuple) or any(
            not isinstance(link_id, LinkId) for link_id in self.supported_link_ids
        ):
            raise InvalidValueError("PhyCapabilities.supported_link_ids: LinkId tuple이어야 합니다")
        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or self.sample_rate_hz <= 0
        ):
            raise InvalidValueError("PhyCapabilities.sample_rate_hz: 양의 정수여야 합니다")

    def supports(self, link_id: LinkId) -> bool:
        return link_id in self.supported_link_ids
