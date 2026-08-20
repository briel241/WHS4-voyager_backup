"""Message codec 공통 계약 (`stage0_baseline.md` §5.4).

`MessageCodec`은 Type ID, DTO type, encode, decode, validate의 공통 의미를 제공한다.
새 Type은 새 `type_XX.py`와 `builtins.py`의 명시적 등록으로 추가하며 중앙 parser의 Type
union이나 CLI 분기를 수정하지 않는다. decorator와 import side effect 등록은 금지한다.

정확한 Message field와 codec method signature는 단계 1 Link 기준선에서 확정한다. 단계 0은
등록·조회 경계와 오류 계약만 고정한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...common.contracts.message import MessageBody
from ...common.results import InternalResult


@runtime_checkable
class MessageCodec(Protocol):
    """Message Type 하나의 wire format을 소유하는 codec."""

    def message_type(self) -> int:
        """이 codec이 담당하는 Message Type 번호."""

    def dto_type(self) -> type:
        """encode 입력과 decode 출력에 사용하는 DTO type."""

    def encode(self, message: MessageBody) -> InternalResult[bytes]:
        """`InternalResult[bytes]`를 반환한다."""

    def decode(self, data: bytes) -> InternalResult[MessageBody]:
        """`InternalResult[MessageBody]`를 반환한다."""

    def validate(self, message: MessageBody) -> InternalResult[None]:
        """`InternalResult[None]`을 반환한다."""
