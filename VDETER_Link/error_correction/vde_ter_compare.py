#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Test Harness: Compare
===============================
Turbo Decoder '뒤'에 붙여서 사용 (파이프라인 맨 끝).

역할:
    입력 PDU의 meta에서 'original'(Tag Original 블록이 심어둔 원본 432bit)을 꺼내고,
    data(디코더가 복원한 432bit)와 비교해서 몇 bit가 다른지, 성공/실패인지 콘솔에 출력.

Message input port : "in"
(출력 포트 없음 - 최종 종단 블록)
"""

from __future__ import annotations

try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


def compare_bits(original: list, decoded: list) -> tuple:
    """(다른 bit 개수, 성공여부) 반환"""
    if len(original) != len(decoded):
        return -1, False
    n_diff = sum(1 for a, b in zip(original, decoded) if a != b)
    return n_diff, (n_diff == 0)


if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """VDE-TER Compare (원본 vs 디코딩 결과 대조, 종단 블록)"""

        def __init__(self, verbose=True):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Compare",
                in_sig=None,
                out_sig=None,
            )
            self.verbose = bool(verbose)
            self.message_port_register_in(pmt.intern("in"))
            self.set_msg_handler(pmt.intern("in"), self._handle_pdu)

        def _handle_pdu(self, msg):
            try:
                if not pmt.is_pair(msg):
                    raise ValueError("Input must be a PDU pair.")

                meta = pmt.car(msg)
                data = pmt.cdr(msg)

                if not pmt.is_u8vector(data):
                    raise ValueError("PDU payload must be a u8vector.")

                decoded = list(pmt.u8vector_elements(data))

                orig_pmt = pmt.dict_ref(meta, pmt.intern("original"), pmt.PMT_NIL)
                if pmt.eqv(orig_pmt, pmt.PMT_NIL):
                    print("[VDE-TER Compare ERROR] meta에 'original' 키가 없음 "
                          "(Tag Original 블록을 인코더 앞에 연결했는지 확인)")
                    return
                original = list(pmt.u8vector_elements(orig_pmt))

                n_diff, success = compare_bits(original, decoded)

                print("=" * 50)
                print(f"[VDE-TER Compare] original={len(original)}bit, "
                      f"decoded={len(decoded)}bit")
                print(f"  다른 bit 수 : {n_diff}")
                print(f"  결과        : {'SUCCESS (완전 복원)' if success else 'FAIL (오류 잔존)'}")
                print("=" * 50)

            except Exception as exc:
                print(f"[VDE-TER Compare ERROR] {exc}")


def _self_test():
    original = [0, 1, 1, 0, 1]
    decoded_ok = [0, 1, 1, 0, 1]
    decoded_bad = [0, 1, 0, 0, 1]

    n_diff, ok = compare_bits(original, decoded_ok)
    assert n_diff == 0 and ok

    n_diff, ok = compare_bits(original, decoded_bad)
    assert n_diff == 1 and not ok

    print("[PASS] Compare self-test")


if __name__ == "__main__":
    _self_test()
