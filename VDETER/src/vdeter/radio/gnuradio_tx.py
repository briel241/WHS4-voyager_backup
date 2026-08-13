"""
GNU Radio TX 어댑터 - 기존 Vector Source 자리를 대신한다.

고정 설정 또는 터미널 Queue에서 받은 Message들을 encode하고 VDE Packet과
Temporary Burst를 만든 뒤 uint8 stream으로 출력한다. Constellation Modulator가
각 byte를 MSB-first bit로 풀어 차동 부호화와 BPSK/RRC 처리를 수행한다.
"""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread

import numpy as np
from gnuradio import gr

from .. import config
from ..phy.burst import temporary_builder
from ..cli.console_tx import TxRequest, read_request
from ..link.messages.registry import MESSAGE_CODECS
from ..link.packet import builder as packet_builder

_MODE_INTERACTIVE = "interactive"
_MODE_FIXED = "fixed"


# Message 목록으로 Temporary Burst bytes 하나를 만든다.
# 목록이 비어 있으면 b""를 반환하여 송신하지 않는다.
def build_burst(messages: list) -> bytes:
    if not messages:
        return b""

    encoded: list[bytes] = []
    for index, message in enumerate(messages, start=1):
        codec = MESSAGE_CODECS.get(message.type_id)
        if codec is None:
            raise ValueError(f"Message {index}: 등록되지 않은 Type {message.type_id}")
        encoded.append(codec.encode(message))

    return temporary_builder.build(packet_builder.build(encoded))


# Temporary Burst를 분할 출력하고 대화식 송신 요청 Queue를 관리한다.
class VDETxSource(gr.sync_block):
    def __init__(self, mode: str | None = None):
        gr.sync_block.__init__(
            self,
            name="VDE TX Adapter",
            in_sig=None,
            out_sig=[np.uint8],
        )

        self._mode = config.TX_MODE if mode is None else str(mode)
        if self._mode not in (_MODE_INTERACTIVE, _MODE_FIXED):
            raise ValueError(
                f"TX_MODE는 '{_MODE_INTERACTIVE}' 또는 '{_MODE_FIXED}'여야 합니다 "
                f"({self._mode!r})"
            )

        self._queue: Queue[TxRequest] = Queue()
        self._stop_event = Event()
        self._input_thread: Thread | None = None

        self._current_request: TxRequest | None = None
        self._burst = b""
        self._position = 0
        self._completed_repeats = 0

        if self._mode == _MODE_FIXED and config.TX_MESSAGES:
            self._activate_request(
                TxRequest(
                    messages=list(config.TX_MESSAGES),
                    repeat_count=config.TX_REPEAT_COUNT,
                )
            )

    # 실제 Flowgraph 실행 시에만 콘솔 입력 스레드를 시작한다.
    # grcc가 블록을 불러올 때는 start()가 호출되지 않으므로 입력을 요구하지 않는다.
    def start(self):
        if self._mode == _MODE_INTERACTIVE:
            self._input_thread = Thread(
                target=self._read_console,
                name="vde-tx-console",
                daemon=True,
            )
            self._input_thread.start()
        else:
            self._print_active_request("Fixed transmission")

        return True

    # Flowgraph 종료 요청을 입력 스레드와 work()에 알린다.
    def stop(self):
        self._stop_event.set()
        return True

    # 터미널 입력을 GNU Radio work()와 분리하여 TxRequest Queue에 넣는다.
    def _read_console(self) -> None:
        print(
            "[VDE TX] Interactive mode. Enter /quit at the message-count prompt to stop.",
            flush=True,
        )

        while not self._stop_event.is_set():
            request = read_request()
            if request is None:
                self._stop_event.set()
                print("[VDE TX] Input closed. Finishing queued requests.", flush=True)
                return

            self._queue.put(request)
            self._print_request("Queued", request)

    # Queue에서 다음 요청을 가져온다. 입력 대기 중 CPU 점유를 줄이기 위해 잠시 기다린다.
    def _load_next_request(self) -> bool:
        try:
            request = self._queue.get(timeout=0.1)
        except Empty:
            return False

        self._activate_request(request)
        return True

    # 새 요청의 Message들을 하나의 Burst로 만들고 출력 상태를 초기화한다.
    def _activate_request(self, request: TxRequest) -> None:
        self._current_request = request
        self._burst = build_burst(request.messages)
        self._position = 0
        self._completed_repeats = 0

    # 현재 요청을 완료하고 다음 요청을 받을 수 있도록 상태를 비운다.
    def _finish_request(self) -> None:
        # 대화식 모드에서는 input() 프롬프트와 work() 스레드의 출력이
        # 같은 줄에 겹치지 않도록 비동기 완료 로그를 출력하지 않는다.
        if self._mode == _MODE_FIXED and self._current_request is not None:
            self._print_request("Completed", self._current_request)

        self._current_request = None
        self._burst = b""
        self._position = 0
        self._completed_repeats = 0

    # GNU Radio 출력 buffer에 현재 Burst의 이어지는 구간만 복사한다.
    def work(self, input_items, output_items):
        out = output_items[0]

        if not self._burst:
            if self._mode == _MODE_FIXED:
                return -1

            if self._stop_event.is_set() and self._queue.empty():
                return -1

            if not self._load_next_request():
                # UHD Sink가 대화식 입력을 기다리는 동안 underflow되지 않도록
                # Modulator 앞단에 0x00 idle byte를 계속 공급한다.
                out[:] = 0
                return len(out)

        count = min(len(out), len(self._burst) - self._position)
        out[:count] = np.frombuffer(
            self._burst,
            dtype=np.uint8,
            count=count,
            offset=self._position,
        )
        self._position += count

        # 반복 횟수는 work() 호출이 아니라 Burst 마지막 byte 출력 시점에 증가한다.
        if self._position >= len(self._burst):
            self._completed_repeats += 1
            repeat_count = self._current_request.repeat_count

            if repeat_count is None or self._completed_repeats < repeat_count:
                self._position = 0
            else:
                self._finish_request()

        return count

    # 요청의 Message Type과 반복 횟수를 한 줄로 표시한다.
    def _print_request(self, action: str, request: TxRequest) -> None:
        types = ", ".join(f"Type {message.type_id}" for message in request.messages)
        repeats = "infinite" if request.repeat_count is None else str(request.repeat_count)
        print(
            f"[VDE TX] {action} | Messages {len(request.messages)} ({types}) | "
            f"Repeat {repeats}",
            flush=True,
        )

    # 현재 Burst의 Packet 크기를 포함한 송신 요약을 표시한다.
    def _print_active_request(self, action: str) -> None:
        if self._current_request is None or not self._burst:
            if self._mode == _MODE_FIXED:
                print("[VDE TX] TX_MESSAGES is empty. Nothing to transmit.", flush=True)
            return

        header_size = len(temporary_builder.PREAMBLE) + temporary_builder.PACKET_LENGTH_SIZE
        packet_size = len(self._burst) - header_size
        self._print_request(action, self._current_request)
        print(
            f"[VDE TX] VDE Packet {packet_size} bytes | "
            f"Temporary Burst {len(self._burst)} bytes",
            flush=True,
        )
