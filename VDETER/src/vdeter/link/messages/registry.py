"""
Type 번호 -> Type별 codec 모듈

Packet Parser가 Message 첫 byte의 Type으로 codec을 고를 때 쓴다.
Registry class를 만들지 않는다. Type이 늘면 이 dictionary에 한 줄 더한다.
"""

from __future__ import annotations

from . import type_92, type_93

MESSAGE_CODECS = {
    92: type_92,
    93: type_93,
}
