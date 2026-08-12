"""대화식 VDE-TER 송신 요청 입력"""

from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..messages.type_92 import Type92Message
from ..messages.type_93 import Type93Message

VDEMessage = Type92Message | Type93Message # Type 추가시 여기서도 추가해야 한다.


# 한 VDE Packet에 넣을 Message들과 완성된 Burst의 반복 횟수를 묶는다.
@dataclass
class TxRequest:
    messages: list[VDEMessage]
    repeat_count: int | None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("TxRequest에는 Message가 최소 하나 필요합니다")
        if self.repeat_count is not None and self.repeat_count < 1:
            raise ValueError("반복 횟수는 1 이상이어야 합니다")


# 일반 문자열은 UTF-8로, hex: 접두사가 붙은 값은 16진수 bytes로 변환한다.
def read_payload() -> bytes:
    while True:
        value = input("Payload (text or hex:01 02 FF): ")
        stripped = value.strip()

        if not stripped.lower().startswith("hex:"):
            return value.encode("utf-8")

        try:
            return bytes.fromhex(stripped[4:].strip())
        except ValueError:
            print("Invalid hexadecimal payload. Example: hex:01 02 A0 FF", flush=True)


# Type 번호와 Payload를 입력받고 config.py의 고정 ID로 Message를 만든다.
def read_message(index: int) -> VDEMessage:
    while True:
        type_text = input(f"Message {index} type [92]: ").strip()

        try:
            type_id = int(type_text or "92")
        except ValueError:
            print("Message type must be 92 or 93.", flush=True)
            continue

        if type_id not in (92, 93):
            print("Only Type 92 and Type 93 are supported.", flush=True)
            continue

        payload = read_payload()

        if type_id == 92:
            return Type92Message(
                source_id=config.SOURCE_ID,
                session_id=config.SESSION_ID,
                destination_id=config.DESTINATION_ID,
                retransmission_id=config.RETRANSMISSION_ID,
                payload=payload,
            )

        return Type93Message(
            source_id=config.SOURCE_ID,
            session_id=config.SESSION_ID,
            destination_id=config.DESTINATION_ID,
            payload=payload,
        )


# 단일 또는 다중 Message와 반복 횟수를 입력받아 송신 요청을 만든다.
# /quit, EOF 또는 Ctrl+C가 입력되면 None을 반환한다.
def read_request() -> TxRequest | None:
    try:
        while True:
            count_text = input("\nNumber of messages [1], or /quit: ").strip()

            if count_text.lower() == "/quit":
                return None

            try:
                message_count = int(count_text or "1")
            except ValueError:
                print("Message count must be an integer.", flush=True)
                continue

            if message_count < 1:
                print("Message count must be at least 1.", flush=True)
                continue

            break

        messages = [read_message(index) for index in range(1, message_count + 1)]

        while True:
            repeat_text = input(
                f"Repeat count [{config.TX_CONSOLE_DEFAULT_REPEAT_COUNT}]: "
            ).strip()

            try:
                repeat_count = int(
                    repeat_text or str(config.TX_CONSOLE_DEFAULT_REPEAT_COUNT)
                )
            except ValueError:
                print("Repeat count must be an integer.", flush=True)
                continue

            if repeat_count < 1:
                print("Repeat count must be at least 1.", flush=True)
                continue

            return TxRequest(messages=messages, repeat_count=repeat_count)
    except (EOFError, KeyboardInterrupt):
        return None
