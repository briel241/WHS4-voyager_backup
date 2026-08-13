"""2단계 송신 모드와 Message 기본값 설정"""

from __future__ import annotations

from .link.messages.type_92 import Type92Message
from .link.messages.type_93 import Type93Message

# 필드 크기만 만족하면 되는 임의 시험값
SOURCE_ID = 1001  # 4 byte
SESSION_ID = 1  # 1 byte
DESTINATION_ID = 2001  # 4 byte
RETRANSMISSION_ID = 0  # 1 byte, Type 92 전용
PAYLOAD = b"Hello"

# 다중 메시지 Packet 시험용 두 번째 값
SOURCE_ID_2 = 1002
DESTINATION_ID_2 = 2002
PAYLOAD_2 = b"World"

# 송신 모드
#   "interactive" -> 실행 중 터미널에서 Message와 반복 횟수를 계속 입력
#   "fixed"       -> 아래 TX_MESSAGES와 TX_REPEAT_COUNT 사용
TX_MODE = "interactive"

# 대화식 모드에서 반복 횟수를 입력하지 않았을 때의 기본값.
# 짧은 단발 Burst보다 PHY 동기 획득 가능성이 높은 값으로 둔다.
TX_CONSOLE_DEFAULT_REPEAT_COUNT = 10

# 고정 송신 모드에서 송신할 Message 목록
#   Message 1개  -> 단일 메시지 Packet
#   Message 2개+ -> 목록 순서대로 이어 붙인 다중 메시지 Packet
#   빈 목록      -> 송신하지 않음
# 다중 메시지로 보내려면 아래 Type93Message 주석을 풀면 된다.

TX_MESSAGES = [
    Type92Message(
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        destination_id=DESTINATION_ID,
        retransmission_id=RETRANSMISSION_ID,
        payload=PAYLOAD,
    ),
    # Type93Message(
    #     source_id=SOURCE_ID_2,
    #     session_id=SESSION_ID,
    #     destination_id=DESTINATION_ID_2,
    #     payload=PAYLOAD_2,
    # ),
]

# 고정 송신 모드의 Burst 반복 횟수
#   None -> 무한 반복
#   1    -> 한 번 송신
#   N    -> N번 송신
# 반복 단위는 개별 Message가 아니라 완성된 Burst 전체다.
TX_REPEAT_COUNT = None
