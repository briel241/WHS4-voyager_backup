#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Test Harness: Tag Original Bits
========================================
Turbo Encoder에 들어가기 '직전'에 붙여서 사용.

역할:
    입력 PDU의 데이터(원본 432bit)를 메타데이터에 'original' 키로 복사해 저장.
    데이터 자체는 그대로 통과시킴(인코더 입력은 안 건드림).

이 메타데이터는 Encoder/Decoder를 거쳐도 그대로 보존되므로(두 블록 모두
car(meta)를 안 건드리고 그대로 out_pdu에 다시 씀), 최종 Decoder 출력에서
meta['original']을 꺼내 디코딩 결과와 대조할 수 있다.

Message input port  : "in"
Message output port : "out"
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


def tag_original(meta_dict: dict, data: list) -> dict:
    """meta_dict에 'original' 키로 data의 복사본을 추가한 새 dict를 반환."""
    new_meta = dict(meta_dict) if meta_dict else {}
    new_meta['original'] = list(data)
    return new_meta


if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """VDE-TER Tag Original Bits (PDU passthrough + 원본 태깅)"""

        def __init__(self, verbose=False):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Tag Original",
                in_sig=None,
                out_sig=None,
            )
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

                # meta(dict pmt)에 original 키 추가
                new_meta = pmt.make_dict()
                if pmt.is_dict(meta):
                    # 기존 meta 항목 보존
                    keys = pmt.dict_keys(meta)
                    for i in range(pmt.length(keys)):
                        k = pmt.nth(i, keys)
                        v = pmt.dict_ref(meta, k, pmt.PMT_NIL)
                        new_meta = pmt.dict_add(new_meta, k, v)

                original_vec = pmt.init_u8vector(len(bits), bits)
                new_meta = pmt.dict_add(new_meta, pmt.intern("original"), original_vec)

                out_pdu = pmt.cons(new_meta, data)
                self.message_port_pub(pmt.intern("out"), out_pdu)

                if self.verbose:
                    print(f"[VDE-TER Tag Original] tagged {len(bits)} original bits")

            except Exception as exc:
                print(f"[VDE-TER Tag Original ERROR] {exc}")


def _self_test():
    meta = {}
    data = [0, 1, 1, 0, 1]
    new_meta = tag_original(meta, data)
    assert new_meta['original'] == data
    print("[PASS] Tag Original self-test")


if __name__ == "__main__":
    _self_test()
