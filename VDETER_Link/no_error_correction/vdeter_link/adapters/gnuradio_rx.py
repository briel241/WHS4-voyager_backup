"""
GNU Radio RX 어댑터 - 기존 Payload Console 자리를 대신한다.

Correlator가 준 unpacked bit stream과 preamble_detected tag를
Temporary Burst Extractor -> Packet Parser -> Type별 decode() 순으로 넘긴다.

이 파일은 GNU Radio의 stream/tag/chunk 처리와 결과 표시만 한다.
고정 40 bit 수집, 고정 5 byte packing, Hello 문자열 해석은 하지 않는다.
bit 수집은 Extractor, byte 변환은 binary_rw, 필드 해석은 Type별 codec의 몫이다.
"""

from __future__ import annotations

import numpy as np
import pmt
from gnuradio import gr

from ..burst.temporary_extractor import Extractor
from ..packet import parser as packet_parser

DEFAULT_TAG_NAME = "preamble_detected"


# 파싱이 끝난 Message를 지정된 Console 양식으로 만든다.
def format_message(index: int, message) -> str:
    lines = [
        f"[VDE Message {index}]",
        f"Type: {message.type_id}",
        f"Length: {message.length} bytes",
        f"Source ID: {message.source_id}",
        f"Session ID: {message.session_id}",
        f"Destination ID: {message.destination_id}",
    ]

    # Retransmission ID는 그 필드를 가진 Type에서만 표시한다 (Type 93에는 없다).
    if hasattr(message, "retransmission_id"):
        lines.append(f"Retransmission ID: {message.retransmission_id}")

    lines.append("Payload (hex): " + " ".join(f"{byte:02X}" for byte in message.payload))
    try:
        text = message.payload.decode("utf-8")
    except UnicodeDecodeError:
        # 손상되었거나 임의 binary인 Payload도 텍스트 줄을 생략하지 않는다.
        text = message.payload.decode("utf-8", errors="replace")
        lines.append(f"Payload (text): {text} [invalid UTF-8]")
    else:
        lines.append(f"Payload (text): {text}")

    return "\n".join(lines)


# bit stream을 받아 VDE Message를 복원해 출력하는 GNU Radio Sink.
class VDERxSink(gr.sync_block):
    def __init__(self, tag_name: str = DEFAULT_TAG_NAME):
        gr.sync_block.__init__(
            self,
            name="VDE RX Adapter",
            in_sig=[np.uint8],
            out_sig=None,
        )
        self._tag_key = pmt.intern(tag_name)
        self._extractor = Extractor()

    def work(self, input_items, output_items):
        bits = input_items[0]
        start = self.nitems_read(0)
        tags = self.get_tags_in_range(0, start, start + len(bits), self._tag_key)

        # Extractor는 chunk 시작을 0으로 보는 상대 위치만 다룬다.
        offsets = [int(tag.offset - start) for tag in tags]
        for packet in self._extractor.consume(bits, offsets):
            self._report(packet)

        return len(bits)

    def _report(self, packet: bytes) -> None:
        try:
            messages = packet_parser.parse(packet)
        except ValueError as error:
            # 잡음에서 나온 preamble 검출이면 길이나 Type이 맞지 않는다.
            # print(f"[VDE Packet] 파싱 실패 ({len(packet)} byte): {error}", flush=True)
            return

        for index, message in enumerate(messages, start=1):
            print(format_message(index, message), flush=True)
            print(flush=True)
