#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Turbo FEC Encoder for GNU Radio
=======================================

Target profile
--------------
- VDE-TER Link ID 11
- FEC input length: 432 bits
- Turbo FEC rate: 1/2
- Main punctured output: 864 bits
- Termination/tail output: 10 bits
- Total FEC output: 874 bits

GNU Radio I/O
-------------
This file defines an Embedded Python Block named ``blk`` when GNU Radio is
available.

Input message port  : "in"
Output message port : "out"

Input PDU data must be a PMT u8vector containing 432 *unpacked* bits:
    [0, 1, 1, 0, ...]   # 432 items, each item is 0 or 1

Output PDU data is a PMT u8vector containing 874 coded bits:
    [0, 1, 0, 1, ...]   # 874 items

Scope
-----
This block performs ONLY the Turbo FEC encoding stage.
CRC generation, byte-bit order conversion, scrambling, Link ID coding,
syncword insertion, modulation, and RRC filtering belong in separate blocks.

References
----------
1) ITU-R Recommendation M.2092-2 (02/2026), VDES common PHY FEC/Turbo coding.
2) Public implementation used as a cross-check:
   collinlokken/grc-ais-vdes-framebuilder, vde-ter-framebuilder.py

Notes for later extension
-------------------------
Profile parameters are kept in PROFILES so that additional Link IDs can be
added later without rewriting the encoder core.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

# GNU Radio is optional so that the encoder core can be unit-tested with
# ordinary Python on a machine where GNU Radio is not installed.
try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


# ---------------------------------------------------------------------------
# [각주 1] Link ID별 Turbo FEC 파라미터
# ---------------------------------------------------------------------------
#
# Link ID 11:
#   K = 432        : Turbo encoder에 들어가는 정보 비트 수
#   k1=2, k2=216  : K = k1 * k2, interleaver 계산 파라미터
#   primes         : 규격의 permutation 계산에 사용하는 정수 집합
#
# puncture:
#   두 RSC encoder의 한 clock 출력 순서를
#   [X, Y0, Y1, X', Y0', Y1'] 로 놓았을 때,
#   1 = 전송, 0 = 삭제(puncture)를 의미한다.
#
# Link ID 11의 main puncturing은 2-clock 주기로 반복되며
# 각 input bit마다 평균 2 coded bit를 남겨 rate 1/2를 만든다.
#
# tail_puncture:
#   RSC 상태를 0으로 종료(termination)시키는 6 clock
#   (encoder 1: 3 clocks + encoder 2: 3 clocks)에 적용한다.
#   Link ID 11에서는 총 10 tail bits가 남는다.
#
PROFILES: Dict[int, dict] = {
    11: {
        "fec_input_bits": 432,
        "k1": 2,
        "k2": 216,
        "primes": [127, 191, 241, 5, 83, 109, 107, 179],
        "puncture": [
            [1, 1, 0, 0, 0, 0],
            [1, 0, 0, 0, 1, 0],
        ],
        "tail_puncture": [
            [1, 1, 0, 0, 0, 0],
            [1, 1, 0, 0, 0, 0],
            [1, 0, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 0],
            [0, 0, 0, 1, 1, 0],
            [0, 0, 0, 1, 0, 0],
        ],
        "expected_main_bits": 864,
        "expected_tail_bits": 10,
        "expected_total_bits": 874,
    },
}


def _validate_bits(bits: Sequence[int], expected_len: int) -> List[int]:
    """
    [각주 2] 입력 검증 함수.

    Turbo encoder 입력은 packed byte(예: 0xA3)가 아니라,
    0 또는 1 값을 하나씩 가진 'unpacked bit vector'이다.

    예:
        [1, 0, 1, 0]  -> 4 bits
    """
    out = [int(b) for b in bits]

    if len(out) != expected_len:
        raise ValueError(
            f"Expected {expected_len} input bits, got {len(out)}."
        )

    if any(b not in (0, 1) for b in out):
        raise ValueError("Every input item must be 0 or 1.")

    return out


def _build_permutation(profile: dict) -> List[int]:
    """
    [각주 3] VDES Turbo interleaver permutation 생성.

    s = 1 ... K에 대해 규격의 permutation 식으로 pi(s)를 계산한다.
    반환값은 Python에서 바로 쓰기 위해 0-based index로 바꾼다.

    interleaved[s-1] = input[pi(s)-1]
    """
    k1 = int(profile["k1"])
    k2 = int(profile["k2"])
    primes = profile["primes"]
    K = k1 * k2

    if k1 % 2 != 0:
        raise ValueError("k1 must be even for the VDES permutation formula.")

    half_k1 = k1 // 2
    permutation: List[int] = []

    for s in range(1, K + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = ((s - 1) // 2) - i * k2
        t = (19 * i + 1) % half_k1
        q = (t % 8) + 1
        c = (primes[q - 1] * j + 21 * m) % k2
        pi_s = 2 * (t + c * half_k1 + 1) - m

        permutation.append(int(pi_s) - 1)

    # permutation이 0..K-1을 정확히 한 번씩 포함하는지 확인.
    if sorted(permutation) != list(range(K)):
        raise RuntimeError("Generated interleaver permutation is not bijective.")

    return permutation


def _interleave(bits: Sequence[int], permutation: Sequence[int]) -> List[int]:
    """
    [각주 4] 두 번째 RSC encoder에 넣을 입력 순서를 재배열한다.

    Turbo Code는 같은 정보열을
      - Encoder 1: 원래 순서
      - Encoder 2: interleaved 순서
    로 각각 인코딩한다.
    """
    return [int(bits[index]) for index in permutation]


def _rsc_encode(
    bits: Sequence[int],
) -> Tuple[List[int], List[int], List[int], List[int], List[int], List[int]]:
    """
    [각주 5] VDES용 8-state RSC(Recursive Systematic Convolutional) encoder.

    각 input bit마다 3개 출력을 계산:
        X  : systematic bit (= 입력 bit)
        Y0 : parity output 0
        Y1 : parity output 1

    내부 메모리 D[0:3]는 모두 0에서 시작한다.

    정보열을 모두 처리한 뒤에는 3 clock의 termination 동작을 수행하여
    내부 상태를 000으로 되돌리기 위한 tail 출력도 생성한다.

    반환:
        X, Y0, Y1, X_tail, Y0_tail, Y1_tail
    """
    D = [0, 0, 0]

    X: List[int] = []
    Y0: List[int] = []
    Y1: List[int] = []

    for raw_bit in bits:
        bit = int(raw_bit) & 1

        # Recursive feedback.
        feedback = D[2] ^ D[1] ^ bit

        # Systematic + parity outputs.
        x = bit
        y0 = feedback ^ D[0] ^ D[2]
        y1 = feedback ^ D[0] ^ D[1] ^ D[2]

        X.append(x)
        Y0.append(y0)
        Y1.append(y1)

        # Shift register update:
        # [D0, D1, D2] <- [feedback, old D0, old D1]
        D = [feedback, D[0], D[1]]

    # ------------------------------------------------------------------
    # [각주 6] RSC termination / tail generation
    # ------------------------------------------------------------------
    # 정보열 처리가 끝난 뒤 3 clock 동안 encoder를 zero state로 복귀시킨다.
    # tail systematic input은 현재 state에서 D1 XOR D2로 정해지고,
    # feedback node에는 결과적으로 0이 들어가 state가 3번 shift되며 000이 된다.
    #
    X_tail: List[int] = []
    Y0_tail: List[int] = []
    Y1_tail: List[int] = []

    for _ in range(3):
        x_tail = D[1] ^ D[2]
        feedback = 0

        y0_tail = feedback ^ D[0] ^ D[2]
        y1_tail = feedback ^ D[0] ^ D[1] ^ D[2]

        X_tail.append(x_tail)
        Y0_tail.append(y0_tail)
        Y1_tail.append(y1_tail)

        D = [feedback, D[0], D[1]]

    if D != [0, 0, 0]:
        raise RuntimeError("RSC termination failed to return state to zero.")

    return X, Y0, Y1, X_tail, Y0_tail, Y1_tail


def _apply_puncture(
    streams: Sequence[Sequence[int]],
    pattern: Sequence[Sequence[int]],
) -> List[int]:
    """
    [각주 7] Main data puncturing.

    streams의 순서:
        [X, Y0, Y1, X', Y0', Y1']

    pattern 값의 의미:
        0 -> 해당 bit 삭제
        1 -> 해당 bit 1회 출력
        2,3,... -> 해당 bit를 그 횟수만큼 반복 출력

    현재 Link ID 11에서는 0/1만 사용하지만,
    추후 다른 puncturing/repetition profile로 확장하기 쉽게 일반화했다.
    """
    if len(streams) != 6:
        raise ValueError("Exactly 6 RSC streams are required.")

    K = len(streams[0])
    if any(len(stream) != K for stream in streams):
        raise ValueError("All six RSC streams must have the same length.")

    output: List[int] = []

    for n in range(K):
        row = pattern[n % len(pattern)]

        if len(row) != 6:
            raise ValueError("Each puncturing row must have 6 entries.")

        for stream_index, repeat_count in enumerate(row):
            repeat_count = int(repeat_count)
            if repeat_count < 0:
                raise ValueError("Puncturing entries cannot be negative.")

            output.extend(
                [int(streams[stream_index][n])] * repeat_count
            )

    return output


def _apply_tail_puncture(
    tail1: Tuple[Sequence[int], Sequence[int], Sequence[int]],
    tail2: Tuple[Sequence[int], Sequence[int], Sequence[int]],
    pattern: Sequence[Sequence[int]],
) -> List[int]:
    """
    [각주 8] 두 RSC encoder의 termination tail puncturing.

    총 6 tail clocks:
      clock 0~2 : Encoder 1의 X,Y0,Y1 tail
      clock 3~5 : Encoder 2의 X',Y0',Y1' tail

    Link ID 11에서는 puncturing 결과 총 10 bit가 출력된다.
    """
    if len(pattern) != 6:
        raise ValueError("Tail puncturing pattern must contain 6 rows.")

    if any(len(s) != 3 for s in tail1 + tail2):
        raise ValueError("Each RSC encoder must generate exactly 3 tail clocks.")

    output: List[int] = []

    # Encoder 1 termination clocks.
    for clock in range(3):
        row = pattern[clock]
        sources = [
            tail1[0][clock],
            tail1[1][clock],
            tail1[2][clock],
            0, 0, 0,
        ]

        for value, repeat_count in zip(sources, row):
            output.extend([int(value)] * int(repeat_count))

    # Encoder 2 termination clocks.
    for clock in range(3):
        row = pattern[clock + 3]
        sources = [
            0, 0, 0,
            tail2[0][clock],
            tail2[1][clock],
            tail2[2][clock],
        ]

        for value, repeat_count in zip(sources, row):
            output.extend([int(value)] * int(repeat_count))

    return output


def turbo_encode(bits: Sequence[int], link_id: int = 11) -> List[int]:
    """
    [각주 9] 외부에서 호출하는 핵심 함수.

    처리 순서:
        432-bit FEC input
            -> RSC Encoder #1
            -> Interleaver
            -> RSC Encoder #2
            -> rate-1/2 puncturing
            -> RSC termination/tail puncturing
            -> 874 coded bits

    주의:
    - CRC를 계산하지 않는다.
    - byte 단위 bit-order reversal을 하지 않는다.
    - scrambling을 하지 않는다.
    - π/4-QPSK 변조를 하지 않는다.
    """
    if link_id not in PROFILES:
        raise ValueError(
            f"Unsupported Link ID {link_id}. "
            f"Currently implemented: {sorted(PROFILES)}"
        )

    profile = PROFILES[link_id]
    K = int(profile["fec_input_bits"])

    input_bits = _validate_bits(bits, K)
    permutation = _build_permutation(profile)

    # RSC Encoder #1: 원래 bit 순서.
    x, y0, y1, xt, y0t, y1t = _rsc_encode(input_bits)

    # RSC Encoder #2: interleaved bit 순서.
    interleaved = _interleave(input_bits, permutation)
    xp, y0p, y1p, xpt, y0pt, y1pt = _rsc_encode(interleaved)

    main_bits = _apply_puncture(
        [x, y0, y1, xp, y0p, y1p],
        profile["puncture"],
    )

    tail_bits = _apply_tail_puncture(
        (xt, y0t, y1t),
        (xpt, y0pt, y1pt),
        profile["tail_puncture"],
    )

    encoded = main_bits + tail_bits

    # 길이는 구현 오류를 가장 빨리 잡을 수 있는 sanity check.
    if len(main_bits) != profile["expected_main_bits"]:
        raise RuntimeError(
            f"Unexpected main FEC length: {len(main_bits)} "
            f"(expected {profile['expected_main_bits']})."
        )

    if len(tail_bits) != profile["expected_tail_bits"]:
        raise RuntimeError(
            f"Unexpected FEC tail length: {len(tail_bits)} "
            f"(expected {profile['expected_tail_bits']})."
        )

    if len(encoded) != profile["expected_total_bits"]:
        raise RuntimeError(
            f"Unexpected total FEC length: {len(encoded)} "
            f"(expected {profile['expected_total_bits']})."
        )

    return encoded


# ===========================================================================
# GNU Radio Embedded Python Block
# ===========================================================================
#
# GRC에서 Embedded Python Block을 만들고 이 파일 내용을 넣으면,
# message port "in"으로 432-bit u8vector PDU를 받고
# message port "out"으로 874-bit u8vector PDU를 내보낸다.
#
if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """
        VDE-TER Turbo FEC Encoder (PDU)
        Default: Link ID 11
        """

        def __init__(self, LinkID=11, verbose=False):
            # [각주 10] LinkID를 parameter로 둔 이유:
            # 현재는 11만 구현했지만 이후 PROFILES에 다른 Link ID를 추가하면
            # 같은 block interface를 유지할 수 있다.
            gr.basic_block.__init__(
                self,
                name="VDE-TER Turbo FEC Encoder",
                in_sig=None,
                out_sig=None,
            )

            self.LinkID = int(LinkID)
            self.verbose = bool(verbose)

            if self.LinkID not in PROFILES:
                raise ValueError(
                    f"Unsupported Link ID {self.LinkID}. "
                    f"Implemented profiles: {sorted(PROFILES)}"
                )

            # PDU message ports.
            self.message_port_register_in(pmt.intern("in"))
            self.message_port_register_out(pmt.intern("out"))
            self.set_msg_handler(pmt.intern("in"), self._handle_pdu)

        def _handle_pdu(self, msg):
            """
            [각주 11] GNU Radio PDU 처리 함수.

            PDU 형식:
                (metadata, u8vector)

            metadata는 그대로 보존해서 output PDU로 전달한다.
            """
            try:
                if not pmt.is_pair(msg):
                    raise ValueError(
                        "Input must be a PDU pair: (metadata, u8vector)."
                    )

                meta = pmt.car(msg)
                data = pmt.cdr(msg)

                if not pmt.is_u8vector(data):
                    raise ValueError(
                        "PDU payload must be a PMT u8vector of unpacked bits."
                    )

                bits = list(pmt.u8vector_elements(data))
                encoded = turbo_encode(bits, self.LinkID)

                out_vec = pmt.init_u8vector(len(encoded), encoded)
                out_pdu = pmt.cons(meta, out_vec)

                self.message_port_pub(pmt.intern("out"), out_pdu)

                if self.verbose:
                    print(
                        f"[VDE-TER Turbo FEC] Link ID {self.LinkID}: "
                        f"{len(bits)} -> {len(encoded)} bits"
                    )

            except Exception as exc:
                # Embedded Python Block 디버깅 시 터미널에서 원인을 바로 볼 수 있게 한다.
                print(f"[VDE-TER Turbo FEC ERROR] {exc}")


def _self_test() -> None:
    """
    GNU Radio 없이도 실행 가능한 최소 self-test.

    확인 항목:
    1) 432 bits 입력 허용
    2) 결과가 정확히 874 bits인지
    3) 출력이 모두 0/1인지
    4) all-zero 입력에서 RSC/tail 처리가 안정적인지
    5) deterministic pseudo-pattern 입력에서도 길이가 유지되는지
    """
    zeros = [0] * 432
    z = turbo_encode(zeros, 11)

    assert len(z) == 874
    assert set(z).issubset({0, 1})
    assert z == [0] * 874

    # 단순하지만 재현 가능한 비트 패턴.
    test_bits = [((i * 7 + 3) ^ (i >> 2)) & 1 for i in range(432)]
    coded = turbo_encode(test_bits, 11)

    assert len(coded) == 874
    assert set(coded).issubset({0, 1})

    print("[PASS] Link ID 11 Turbo encoder self-test")
    print("       input  : 432 bits")
    print("       output : 874 bits (864 main + 10 tail)")


if __name__ == "__main__":
    _self_test()

