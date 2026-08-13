#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Test Harness: Error Injector
=====================================
Turbo Encoder와 Turbo Decoder '사이'에 끼워서 사용.

역할:
    입력 PDU(874bit 코드워드)에서 N개 비트를 무작위로 반전(flip)시켜
    "채널을 통과하며 오류가 생긴 상황"을 흉내낸다.
    메타데이터는 그대로 통과(원본 태그 보존).

Message input port  : "in"
Message output port : "out"
"""

from __future__ import annotations
import random

try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


def inject_errors(bits: list, n_flips: int, seed=None) -> tuple:
    """bits(list of 0/1) 중 n_flips개를 무작위로 뒤집는다. (뒤집힌 리스트, 위치목록) 반환."""
    rng = random.Random(seed)
    n_flips = min(n_flips, len(bits))
    positions = rng.sample(range(len(bits)), n_flips)
    out = list(bits)
    for p in positions:
        out[p] = 1 - out[p]
    return out, positions


if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """VDE-TER Error Injector (PDU bit-flip)"""

        def __init__(self, n_flips=5, seed=-1, verbose=False):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Error Injector",
                in_sig=None,
                out_sig=None,
            )
            self.n_flips = int(n_flips)
            self.seed = None if int(seed) < 0 else int(seed)
            self.verbose = bool(verbose)
            self.message_port_register_in(pmt.intern("in"))
            self.message_port_register_out(pmt.intern("out"))
            self.set_msg_handler(pmt.intern("in"), self._handle_pdu)

        def _handle_pdu(self, msg):
            try:
                if not pmt.is_pair(msg):
                    raise ValueError("Input must be a PDU pair.")

                meta = pmt.car(msg)
                data = pmt.cdr(msg)

                if not pmt.is_u8vector(data):
                    raise ValueError("PDU payload must be a u8vector.")

                bits = list(pmt.u8vector_elements(data))
                corrupted, positions = inject_errors(bits, self.n_flips, self.seed)

                out_vec = pmt.init_u8vector(len(corrupted), corrupted)
                out_pdu = pmt.cons(meta, out_vec)
                self.message_port_pub(pmt.intern("out"), out_pdu)

                if self.verbose:
                    print(f"[VDE-TER Error Injector] flipped {self.n_flips} bits "
                          f"at positions {positions[:10]}{'...' if len(positions) > 10 else ''}")

            except Exception as exc:
                print(f"[VDE-TER Error Injector ERROR] {exc}")


def _self_test():
    bits = [0] * 874
    corrupted, positions = inject_errors(bits, 5, seed=1)
    n_diff = sum(1 for a, b in zip(bits, corrupted) if a != b)
    assert n_diff == 5
    assert len(positions) == 5
    print("[PASS] Error Injector self-test:", n_diff, "bits flipped")


if __name__ == "__main__":
    _self_test()
