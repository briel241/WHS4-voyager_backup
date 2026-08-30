"""
응용 메시지 <-> payload bytes 변환 (Presentation Interface 역할)

이 모듈이 docs/mass_roc_background.md 4.6이 말하는 "표준이 비워둔 PI 자리"다.
응용 로직이 다루는 dataclass와 VDE 링크 계층이 받는 payload bytes 사이를
옮긴다. 링크 계층은 이 bytes의 내용을 해석하지 않는다.

payload 구조
    [ VPFI 16 bit | Message ID 8 bit | 메시지별 본문 | byte 정렬 padding ]

    VPFI는 IALA R1027이 정한 16 bit Payload Format Identifier다.
    Message ID는 R1027이 허용하는 선택적 message identifier이며 8 bit를 쓴다.
    자세한 근거는 docs/mass_roc_message_set.md 8.1을 보라.

주의
    송신원·수신처 식별자는 여기에 넣지 않는다. VDE 링크 계층 Type 92/93의
    Source ID와 Destination ID가 이미 담고 있어 중복이기 때문이다.
"""

from __future__ import annotations

from . import config
from .bitfield import BitReader, BitWriter
from .messages.ais_auth import AisAuthValue
from .messages.application_ack import ApplicationAck
from .messages.environmental_info import EnvironmentalInfo
from .messages.navigational_warning import NavigationalWarning
from .messages.position_report import PositionReport
from .messages.registry import MESSAGE_CODECS, MESSAGE_IDS
from .messages.route_instruction import RouteInstruction
from .messages.weather_observation import WeatherObservation

# 새 메시지를 추가하면 여기에도 dataclass를 더한다.
ApplicationMessage = (
    PositionReport
    | RouteInstruction
    | ApplicationAck
    | AisAuthValue
    | NavigationalWarning
    | EnvironmentalInfo
    | WeatherObservation
)

VPFI_BITS = 16
MESSAGE_ID_BITS = 8

# 봉투 크기 (byte 정렬 padding 제외)
ENVELOPE_BITS = VPFI_BITS + MESSAGE_ID_BITS


# 응용 메시지를 payload bytes로 만든다.
def encode(message: ApplicationMessage, vpfi: int | None = None) -> bytes:
    message_id = MESSAGE_IDS.get(type(message))
    if message_id is None:
        raise ValueError(f"등록되지 않은 응용 메시지입니다 ({type(message).__name__})")

    codec = MESSAGE_CODECS[message_id]

    writer = BitWriter()
    writer.write_uint(config.VPFI if vpfi is None else vpfi, VPFI_BITS, "VPFI")
    writer.write_uint(message_id, MESSAGE_ID_BITS, "Message ID")
    codec.write_body(writer, message)
    writer.pad_to_byte()
    return writer.to_bytes()


# payload bytes에서 응용 메시지를 복원한다.
# VPFI가 맞지 않으면 우리 응용의 payload가 아니므로 오류다.
def decode(payload: bytes, vpfi: int | None = None) -> ApplicationMessage:
    expected_vpfi = config.VPFI if vpfi is None else vpfi

    reader = BitReader(payload)
    received_vpfi = reader.read_uint(VPFI_BITS, "VPFI")
    if received_vpfi != expected_vpfi:
        raise ValueError(
            f"VPFI가 다릅니다 (기대 0x{expected_vpfi:04X}, 수신 0x{received_vpfi:04X})"
        )

    message_id = reader.read_uint(MESSAGE_ID_BITS, "Message ID")
    codec = MESSAGE_CODECS.get(message_id)
    if codec is None:
        raise ValueError(f"등록되지 않은 Message ID입니다 ({message_id})")

    message = codec.read_body(reader)

    # 남은 bit는 byte 정렬 padding뿐이어야 한다.
    if reader.remaining >= 8:
        raise ValueError(f"본문 뒤에 {reader.remaining} bit가 남았습니다")
    reader.check_padding("padding")

    return message


# --------------------------------------------------------------------------
# VDETER application bytes (봉투 없음)
#
# VDETER가 SC-VDE format을 등록하면서 VPFI·Message Format은 VDETER
# Presentation codec이 붙이기로 계약이 정해졌다(가이드 §8.2). 따라서 우리가
# `submit(payload=...)`에 넣는 application bytes에는 봉투를 넣지 않는다.
#
#     application bytes  =  [ 본문 | byte 정렬 padding ]
#     wire (VDETER 생성)  =  [ ff 7f | MF | application bytes ]
#
# 위 encode()/decode()는 우리끼리 쓰는 봉투 포함 형식이라 그대로 남긴다.
# --------------------------------------------------------------------------


def encode_body(message: ApplicationMessage) -> bytes:
    """VDETER에 넘길 application bytes를 만든다. VPFI·Message ID를 붙이지 않는다."""
    message_id = MESSAGE_IDS.get(type(message))
    if message_id is None:
        raise ValueError(f"등록되지 않은 응용 메시지입니다 ({type(message).__name__})")

    writer = BitWriter()
    MESSAGE_CODECS[message_id].write_body(writer, message)
    writer.pad_to_byte()
    return writer.to_bytes()


def decode_body(payload: bytes, message_id: int) -> ApplicationMessage:
    """application bytes를 복원한다. message_id는 payload_format이 알려준다."""
    codec = MESSAGE_CODECS.get(message_id)
    if codec is None:
        raise ValueError(f"등록되지 않은 Message ID입니다 ({message_id})")

    reader = BitReader(payload)
    message = codec.read_body(reader)

    # 남은 bit는 byte 정렬 padding뿐이어야 하고 전부 0이어야 한다(가이드 §8.3).
    if reader.remaining >= 8:
        raise ValueError(f"본문 뒤에 {reader.remaining} bit가 남았습니다")
    reader.check_padding("padding")

    return message


# 파싱된 메시지를 콘솔 표시용 문자열로 만든다.
def describe(message: ApplicationMessage) -> str:
    message_id = MESSAGE_IDS.get(type(message))
    if message_id is None:
        return repr(message)
    codec = MESSAGE_CODECS[message_id]
    return f"[{codec.NAME}]\n{codec.describe(message)}"
