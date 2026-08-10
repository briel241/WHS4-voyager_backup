#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Turbo FEC Decoder for GNU Radio
=======================================

Target profile
--------------
- VDE-TER Link ID 11
- FEC coded input length : 874 bits
    - main punctured bits : 864
    - termination/tail    : 10
- Decoded output length   : 432 bits
- Turbo iterations        : configurable (default 6)

GNU Radio I/O
-------------
This file defines an Embedded Python Block named ``blk`` when GNU Radio is
available.

Message input port  : "in"
Message output port : "out"

Accepted input PDU payloads:

1) PMT u8vector, length 874
   Hard bits:
       [0, 1, 1, 0, ...]
   Each received bit is converted to an internal LLR.

2) PMT f32vector, length 874
   Soft LLR values from a demodulator:
       positive LLR -> bit 0 is more likely
       negative LLR -> bit 1 is more likely

Output:
    PMT u8vector, length 432
    decoded unpacked bits (0/1)

Important
---------
This decoder must receive the FEC codeword AFTER descrambling.
It does NOT perform:
- RF demodulation
- pi/4-QPSK demapping
- descrambling
- CRC checking
- byte bit-order restoration

Those stages should be separate GNU Radio blocks.

Decoder algorithm
-----------------
The VDE-TER standard defines the Turbo encoder/code structure. A receiver is
free to choose a suitable decoding implementation. This implementation uses:

    iterative Turbo decoding
        + two constituent 8-state RSC decoders
        + Max-Log-MAP SISO decoding
        + interleaved/deinterleaved extrinsic LLR exchange

Max-Log-MAP is a practical approximation of Log-MAP and is considerably
simpler to implement in a GNU Radio Embedded Python Block.

References
----------
1) ITU-R Recommendation M.2092-2 (02/2026), VDES FEC/Turbo coding.
2) Encoder cross-check:
   collinlokken/grc-ais-vdes-framebuilder,
   vde-ter-framebuilder.py

This file is designed to correspond to:
    vde_ter_turbo_encoder_link11.py
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple
import numpy as np

# GNU Radio is optional so the decoder core can be tested with plain Python.
try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


# =============================================================================
# [각주 1] Link ID별 FEC 프로파일
# =============================================================================
#
# 현재는 Link ID 11만 구현.
# 이후 Link ID 12~19를 추가할 때 이 딕셔너리에 각 profile의
# K, interleaver, puncturing 정보를 추가하면 decoder core를 재사용할 수 있다.
#
PROFILES: Dict[int, dict] = {
    11: {
        "fec_input_bits": 432,
        "coded_bits": 874,

        # VDES Turbo interleaver parameters
        "k1": 2,
        "k2": 216,
        "primes": [127, 191, 241, 5, 83, 109, 107, 179],

        # 한 clock에서 두 RSC가 만드는 6개 출력을
        # [X, Y0, Y1, X', Y0', Y1'] 순서로 놓았을 때
        # 1=전송, 0=puncture(미전송)
        "puncture": [
            [1, 1, 0, 0, 0, 0],
            [1, 0, 0, 0, 1, 0],
        ],

        # RSC termination 6 clocks에 대한 puncturing pattern.
        # Encoder 1 tail 3 clocks + Encoder 2 tail 3 clocks.
        "tail_puncture": [
            [1, 1, 0, 0, 0, 0],
            [1, 1, 0, 0, 0, 0],
            [1, 0, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 0],
            [0, 0, 0, 1, 1, 0],
            [0, 0, 0, 1, 0, 0],
        ],
    },
}


# =============================================================================
# [각주 2] Interleaver / Deinterleaver
# =============================================================================

def _build_permutation(profile: dict) -> np.ndarray:
    """
    VDES Turbo interleaver permutation pi(s)를 생성한다.

    반환:
        permutation[out_index] = original_index

    즉:
        interleaved = original[permutation]
    """
    k1 = int(profile["k1"])
    k2 = int(profile["k2"])
    primes = profile["primes"]
    K = k1 * k2

    half_k1 = k1 // 2
    if half_k1 <= 0:
        raise ValueError("Invalid k1.")

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

    permutation = np.asarray(permutation, dtype=np.int32)

    if not np.array_equal(np.sort(permutation), np.arange(K)):
        raise RuntimeError("Interleaver permutation is not bijective.")

    return permutation


def _inverse_permutation(permutation: np.ndarray) -> np.ndarray:
    """
    deinterleaver index 생성.

    interleaved = original[permutation]
    original    = interleaved[inverse]
    """
    inverse = np.empty_like(permutation)
    inverse[permutation] = np.arange(len(permutation), dtype=np.int32)
    return inverse


# =============================================================================
# [각주 3] 8-state RSC trellis
# =============================================================================
#
# Encoder와 동일한 상태식:
#
#   feedback = D2 XOR D1 XOR u
#   X        = u
#   Y0       = feedback XOR D0 XOR D2
#   Y1       = feedback XOR D0 XOR D1 XOR D2
#
#   next state = [feedback, D0, D1]
#
# 3개의 memory bit -> 2^3 = 8 states.
#

def _state_to_bits(state: int) -> Tuple[int, int, int]:
    """state 번호(0~7)를 [D0,D1,D2]로 변환."""
    return (
        (state >> 2) & 1,
        (state >> 1) & 1,
        state & 1,
    )


def _bits_to_state(D0: int, D1: int, D2: int) -> int:
    """[D0,D1,D2]를 state 번호(0~7)로 변환."""
    return (int(D0) << 2) | (int(D1) << 1) | int(D2)


def _build_trellis():
    """
    데이터 clock용 trellis와 termination clock용 trellis 생성.

    데이터 clock:
        각 state에서 u=0 또는 u=1, 총 2개 branch.

    termination clock:
        encoder state를 000으로 되돌리기 위해 입력 tail bit가
        state에 의해 결정되므로 state당 branch가 1개뿐이다.
    """
    next_state = np.zeros((8, 2), dtype=np.int32)
    y0 = np.zeros((8, 2), dtype=np.int8)
    y1 = np.zeros((8, 2), dtype=np.int8)

    tail_next = np.zeros(8, dtype=np.int32)
    tail_u = np.zeros(8, dtype=np.int8)
    tail_y0 = np.zeros(8, dtype=np.int8)
    tail_y1 = np.zeros(8, dtype=np.int8)

    for state in range(8):
        D0, D1, D2 = _state_to_bits(state)

        # 일반 information-bit transition
        for u in (0, 1):
            feedback = D2 ^ D1 ^ u

            next_state[state, u] = _bits_to_state(
                feedback, D0, D1
            )

            y0[state, u] = feedback ^ D0 ^ D2
            y1[state, u] = feedback ^ D0 ^ D1 ^ D2

        # termination transition:
        # u_tail = D1 XOR D2를 넣으면 feedback=0이 된다.
        u_tail = D1 ^ D2

        tail_u[state] = u_tail
        tail_next[state] = _bits_to_state(0, D0, D1)
        tail_y0[state] = D0 ^ D2
        tail_y1[state] = D0 ^ D1 ^ D2

    # 모든 state가 3 termination clocks 후 000으로 가는지 확인.
    for initial_state in range(8):
        state = initial_state
        for _ in range(3):
            state = int(tail_next[state])
        if state != 0:
            raise RuntimeError("Invalid RSC termination trellis.")

    return next_state, y0, y1, tail_next, tail_u, tail_y0, tail_y1


(
    _NEXT_STATE,
    _Y0,
    _Y1,
    _TAIL_NEXT,
    _TAIL_U,
    _TAIL_Y0,
    _TAIL_Y1,
) = _build_trellis()


# =============================================================================
# [각주 4] Depuncturing
# =============================================================================

def _to_llr(
    received: Sequence[float],
    *,
    hard_input: bool,
    hard_llr: float,
    expected_len: int,
) -> np.ndarray:
    """
    입력을 LLR(Log-Likelihood Ratio)로 변환.

    이 코드의 LLR convention:
        LLR > 0  -> bit 0 쪽이 더 유력
        LLR < 0  -> bit 1 쪽이 더 유력

    hard_input=True:
        입력 0 -> +hard_llr
        입력 1 -> -hard_llr

    hard_input=False:
        입력 자체를 LLR로 간주.
    """
    values = np.asarray(received, dtype=np.float64)

    if len(values) != expected_len:
        raise ValueError(
            f"Expected {expected_len} coded values, got {len(values)}."
        )

    if hard_input:
        if np.any((values != 0) & (values != 1)):
            raise ValueError("Hard input must contain only 0 or 1.")
        return np.where(values == 0, float(hard_llr), -float(hard_llr))

    if not np.all(np.isfinite(values)):
        raise ValueError("Soft LLR input contains NaN or infinity.")

    return values


def _depuncture(
    received_llr: np.ndarray,
    profile: dict,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    punctured 874-value codeword를 RSC별 observation으로 복원한다.

    반환:
        streams : shape (6, K)
            [X, Y0, Y1, X', Y0', Y1']

        tail1 : shape (3,3)
            Encoder 1 termination observations
            [X_tail, Y0_tail, Y1_tail]

        tail2 : shape (3,3)
            Encoder 2 termination observations

    puncture된 bit는 수신 관측이 없으므로 LLR=0으로 둔다.
    LLR=0은 decoder 입장에서 "0/1 어느 쪽인지 정보 없음"을 뜻한다.
    """
    K = int(profile["fec_input_bits"])

    streams = np.zeros((6, K), dtype=np.float64)
    tail1 = np.zeros((3, 3), dtype=np.float64)
    tail2 = np.zeros((3, 3), dtype=np.float64)

    cursor = 0

    # Main 432 clocks
    pattern = profile["puncture"]

    for n in range(K):
        row = pattern[n % len(pattern)]

        for stream_index, count in enumerate(row):
            for _ in range(int(count)):
                streams[stream_index, n] += received_llr[cursor]
                cursor += 1

    # Encoder 1 termination: first 3 tail pattern rows.
    tail_pattern = profile["tail_puncture"]

    for clock in range(3):
        row = tail_pattern[clock]

        for stream_index in range(3):
            for _ in range(int(row[stream_index])):
                tail1[stream_index, clock] += received_llr[cursor]
                cursor += 1

    # Encoder 2 termination: last 3 tail pattern rows.
    for clock in range(3):
        row = tail_pattern[clock + 3]

        for stream_index in range(3, 6):
            for _ in range(int(row[stream_index])):
                tail2[stream_index - 3, clock] += received_llr[cursor]
                cursor += 1

    if cursor != len(received_llr):
        raise RuntimeError(
            f"Depuncturing consumed {cursor} values, "
            f"but input contains {len(received_llr)}."
        )

    return streams, tail1, tail2


# =============================================================================
# [각주 5] Max-Log-MAP constituent decoder
# =============================================================================

def _siso_max_log_map(
    systematic_llr: np.ndarray,
    parity_y0_llr: np.ndarray,
    parity_y1_llr: np.ndarray,
    tail_llr: np.ndarray,
    apriori_llr: np.ndarray,
) -> np.ndarray:
    """
    하나의 RSC constituent code에 대한 SISO decoder.

    SISO:
        Soft Input Soft Output

    입력:
        systematic_llr : 정보 bit의 channel LLR
        parity_y0_llr  : Y0 parity channel LLR
        parity_y1_llr  : Y1 parity channel LLR
        tail_llr       : termination 3 clocks 관측
        apriori_llr    : 다른 Turbo decoder가 전달한 extrinsic 정보

    출력:
        posterior LLR, 길이 K

    Max-Log-MAP:
        trellis의 forward metric(alpha)와 backward metric(beta)를 구하고,
        bit=0 경로와 bit=1 경로의 최대 metric 차이로 posterior LLR을 계산.

    LLR > 0 -> decoded bit 0
    LLR < 0 -> decoded bit 1
    """
    K = len(systematic_llr)
    T = K + 3                  # 3 termination clocks 포함
    NEG_INF = -1.0e30

    # ---------------------------------------------------------------------
    # Branch metric gamma
    # ---------------------------------------------------------------------
    #
    # received LLR L과 후보 bit b에 대해
    #      0.5 * (1 - 2b) * L
    # 을 더한다.
    #
    gamma = np.empty((K, 8, 2), dtype=np.float64)

    for t in range(K):
        sys_plus_apriori = systematic_llr[t] + apriori_llr[t]

        for state in range(8):
            for u in (0, 1):
                gamma[t, state, u] = 0.5 * (
                    (1 - 2 * u) * sys_plus_apriori
                    + (1 - 2 * int(_Y0[state, u])) * parity_y0_llr[t]
                    + (1 - 2 * int(_Y1[state, u])) * parity_y1_llr[t]
                )

    # ---------------------------------------------------------------------
    # Forward recursion alpha
    # ---------------------------------------------------------------------
    alpha = np.full((T + 1, 8), NEG_INF, dtype=np.float64)

    # RSC encoder는 state 000에서 시작.
    alpha[0, 0] = 0.0

    for t in range(K):
        next_metric = np.full(8, NEG_INF, dtype=np.float64)

        for state in range(8):
            base = alpha[t, state]

            for u in (0, 1):
                ns = int(_NEXT_STATE[state, u])
                metric = base + gamma[t, state, u]

                if metric > next_metric[ns]:
                    next_metric[ns] = metric

        # 수치가 불필요하게 커지는 것을 막기 위한 normalization.
        next_metric -= np.max(next_metric)
        alpha[t + 1] = next_metric

    # 3 termination clocks
    for tail_clock in range(3):
        t = K + tail_clock
        next_metric = np.full(8, NEG_INF, dtype=np.float64)

        for state in range(8):
            u = int(_TAIL_U[state])
            ns = int(_TAIL_NEXT[state])

            branch = 0.5 * (
                (1 - 2 * u) * tail_llr[0, tail_clock]
                + (1 - 2 * int(_TAIL_Y0[state])) * tail_llr[1, tail_clock]
                + (1 - 2 * int(_TAIL_Y1[state])) * tail_llr[2, tail_clock]
            )

            metric = alpha[t, state] + branch

            if metric > next_metric[ns]:
                next_metric[ns] = metric

        next_metric -= np.max(next_metric)
        alpha[t + 1] = next_metric

    # ---------------------------------------------------------------------
    # Backward recursion beta
    # ---------------------------------------------------------------------
    beta = np.full((T + 1, 8), NEG_INF, dtype=np.float64)

    # termination 이후 최종 state는 000으로 제한.
    beta[T, 0] = 0.0

    # termination 구간 backward
    for tail_clock in range(2, -1, -1):
        t = K + tail_clock
        current = np.full(8, NEG_INF, dtype=np.float64)

        for state in range(8):
            u = int(_TAIL_U[state])
            ns = int(_TAIL_NEXT[state])

            branch = 0.5 * (
                (1 - 2 * u) * tail_llr[0, tail_clock]
                + (1 - 2 * int(_TAIL_Y0[state])) * tail_llr[1, tail_clock]
                + (1 - 2 * int(_TAIL_Y1[state])) * tail_llr[2, tail_clock]
            )

            current[state] = branch + beta[t + 1, ns]

        current -= np.max(current)
        beta[t] = current

    # information 구간 backward
    for t in range(K - 1, -1, -1):
        current = np.full(8, NEG_INF, dtype=np.float64)

        for state in range(8):
            best = NEG_INF

            for u in (0, 1):
                ns = int(_NEXT_STATE[state, u])
                metric = gamma[t, state, u] + beta[t + 1, ns]

                if metric > best:
                    best = metric

            current[state] = best

        current -= np.max(current)
        beta[t] = current

    # ---------------------------------------------------------------------
    # Posterior LLR
    # ---------------------------------------------------------------------
    posterior = np.empty(K, dtype=np.float64)

    for t in range(K):
        best_bit0 = NEG_INF
        best_bit1 = NEG_INF

        for state in range(8):
            for u in (0, 1):
                ns = int(_NEXT_STATE[state, u])

                metric = (
                    alpha[t, state]
                    + gamma[t, state, u]
                    + beta[t + 1, ns]
                )

                if u == 0:
                    if metric > best_bit0:
                        best_bit0 = metric
                else:
                    if metric > best_bit1:
                        best_bit1 = metric

        posterior[t] = best_bit0 - best_bit1

    return posterior


# =============================================================================
# [각주 6] Iterative Turbo Decoder
# =============================================================================

def turbo_decode(
    received: Sequence[float],
    link_id: int = 11,
    iterations: int = 6,
    *,
    hard_input: bool = True,
    hard_llr: float = 2.0,
) -> Tuple[List[int], np.ndarray]:
    """
    외부에서 직접 호출할 수 있는 Turbo decoder 핵심 함수.

    Parameters
    ----------
    received:
        hard_input=True
            874개의 0/1 coded bits

        hard_input=False
            874개의 soft LLR values

    link_id:
        현재 11 지원.
        추후 PROFILES 확장을 위해 parameter로 유지.

    iterations:
        Turbo decoder 반복 횟수.
        기본값 6.
        증가시키면 성능이 좋아질 수 있지만 처리시간도 증가한다.

    hard_llr:
        hard bit를 내부 LLR로 바꿀 때 사용하는 신뢰도 크기.
        단순 bit-flip 실험용 기본값 2.0.
        실제 무선 링크에서는 hard input보다 demodulator의 soft LLR 사용을 권장.

    Returns
    -------
    decoded_bits:
        길이 432의 0/1 list

    posterior_llr:
        최종 decoded bit의 posterior LLR.
        디버깅/성능분석 시 사용할 수 있다.
    """
    if link_id not in PROFILES:
        raise ValueError(
            f"Unsupported Link ID {link_id}. "
            f"Implemented profiles: {sorted(PROFILES)}"
        )

    if iterations < 1:
        raise ValueError("iterations must be >= 1.")

    if hard_llr <= 0:
        raise ValueError("hard_llr must be > 0.")

    profile = PROFILES[link_id]
    K = int(profile["fec_input_bits"])

    received_llr = _to_llr(
        received,
        hard_input=hard_input,
        hard_llr=hard_llr,
        expected_len=int(profile["coded_bits"]),
    )

    # punctured stream을 각 constituent encoder 관측으로 복원.
    streams, tail1, tail2 = _depuncture(received_llr, profile)

    # streams:
    #   0 = X  (systematic)
    #   1 = Y0
    #   2 = Y1
    #   3 = X'  (Link ID 11 main puncturing에서는 전송되지 않음)
    #   4 = Y0'
    #   5 = Y1'
    #
    systematic_1 = streams[0]

    permutation = _build_permutation(profile)
    inverse = _inverse_permutation(permutation)

    # 두 번째 RSC의 information bit는 interleaved(original)이므로
    # systematic channel observation도 동일한 순서로 interleave한다.
    systematic_2 = systematic_1[permutation]

    # Decoder 1이 Decoder 2에게 받은 a-priori 정보.
    apriori_1 = np.zeros(K, dtype=np.float64)

    posterior_2 = None

    # ---------------------------------------------------------------------
    # Turbo iterations
    # ---------------------------------------------------------------------
    for _ in range(int(iterations)):

        # Constituent Decoder #1
        posterior_1 = _siso_max_log_map(
            systematic_1,
            streams[1],
            streams[2],
            tail1,
            apriori_1,
        )

        # Extrinsic = posterior - systematic(channel) - prior
        extrinsic_1 = posterior_1 - systematic_1 - apriori_1

        # 과도한 값이 반복 과정에서 폭증하지 않도록 제한.
        extrinsic_1 = np.clip(extrinsic_1, -50.0, 50.0)

        # Decoder #2 순서로 interleave.
        apriori_2 = extrinsic_1[permutation]

        # Constituent Decoder #2
        posterior_2 = _siso_max_log_map(
            systematic_2,
            streams[4],
            streams[5],
            tail2,
            apriori_2,
        )

        extrinsic_2 = posterior_2 - systematic_2 - apriori_2
        extrinsic_2 = np.clip(extrinsic_2, -50.0, 50.0)

        # 다시 original bit 순서로 deinterleave하여 Decoder #1에 전달.
        apriori_1 = extrinsic_2[inverse]

    # Decoder #2 posterior는 interleaved order.
    # 원래 432-bit 순서로 되돌린다.
    final_llr = posterior_2[inverse]

    # 이 코드의 LLR convention:
    #   >= 0 -> 0
    #   <  0 -> 1
    decoded = (final_llr < 0.0).astype(np.uint8)

    return decoded.tolist(), final_llr


# =============================================================================
# GNU Radio Embedded Python Block
# =============================================================================

if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """
        VDE-TER Turbo FEC Decoder (PDU)

        Parameters exposed in GRC:
            LinkID      : default 11
            iterations  : default 6
            hard_llr    : default 2.0
            verbose     : print decode information
        """

        def __init__(
            self,
            LinkID=11,
            iterations=6,
            hard_llr=2.0,
            verbose=False,
        ):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Turbo FEC Decoder",
                in_sig=None,
                out_sig=None,
            )

            self.LinkID = int(LinkID)
            self.iterations = int(iterations)
            self.hard_llr = float(hard_llr)
            self.verbose = bool(verbose)

            if self.LinkID not in PROFILES:
                raise ValueError(
                    f"Unsupported Link ID {self.LinkID}. "
                    f"Implemented profiles: {sorted(PROFILES)}"
                )

            # Message/PDU ports
            self.message_port_register_in(pmt.intern("in"))
            self.message_port_register_out(pmt.intern("out"))

            self.set_msg_handler(
                pmt.intern("in"),
                self._handle_pdu,
            )

        def _handle_pdu(self, msg):
            """
            GNU Radio PDU handler.

            u8vector:
                hard decision 0/1 input

            f32vector:
                soft LLR input

            metadata는 그대로 output PDU에 전달한다.
            """
            try:
                if not pmt.is_pair(msg):
                    raise ValueError(
                        "Input must be a PDU pair: (metadata, vector)."
                    )

                meta = pmt.car(msg)
                payload = pmt.cdr(msg)

                # ---------------------------------------------------------
                # Hard-bit PDU
                # ---------------------------------------------------------
                if pmt.is_u8vector(payload):
                    received = list(pmt.u8vector_elements(payload))

                    decoded, final_llr = turbo_decode(
                        received,
                        link_id=self.LinkID,
                        iterations=self.iterations,
                        hard_input=True,
                        hard_llr=self.hard_llr,
                    )

                    mode = "hard"

                # ---------------------------------------------------------
                # Soft-LLR PDU
                # ---------------------------------------------------------
                elif hasattr(pmt, "is_f32vector") and pmt.is_f32vector(payload):
                    received = list(pmt.f32vector_elements(payload))

                    decoded, final_llr = turbo_decode(
                        received,
                        link_id=self.LinkID,
                        iterations=self.iterations,
                        hard_input=False,
                    )

                    mode = "soft-LLR"

                else:
                    raise ValueError(
                        "PDU payload must be either "
                        "u8vector(874 hard bits) or "
                        "f32vector(874 LLR values)."
                    )

                out_vec = pmt.init_u8vector(len(decoded), decoded)
                out_pdu = pmt.cons(meta, out_vec)

                self.message_port_pub(
                    pmt.intern("out"),
                    out_pdu,
                )

                if self.verbose:
                    confidence = float(np.mean(np.abs(final_llr)))

                    print(
                        f"[VDE-TER Turbo Decoder] "
                        f"Link ID {self.LinkID}, "
                        f"mode={mode}, "
                        f"iterations={self.iterations}, "
                        f"{len(received)} -> {len(decoded)} bits, "
                        f"mean |LLR|={confidence:.3f}"
                    )

            except Exception as exc:
                print(f"[VDE-TER Turbo Decoder ERROR] {exc}")


# =============================================================================
# Standalone self-test
# =============================================================================

def _self_test() -> None:
    """
    같은 폴더의 vde_ter_turbo_encoder_link11.py를 사용해 검증.

    Test A:
        오류 없는 874-bit codeword -> 원본 432 bits 완전 복원

    Test B:
        874 coded bits 중 20개를 고정 seed로 뒤집음
        -> Turbo decoder가 원본 432 bits를 복원하는지 확인

    주의:
        Turbo Code가 임의의 20 bit 오류를 항상 보장해서 고친다는 뜻은 아니다.
        이 self-test는 encoder/decoder 연결 및 기본 오류정정 동작 확인용이다.
    """
    import importlib.util
    from pathlib import Path

    encoder_path = Path(__file__).with_name(
        "vde_ter_turbo_encoder_link11.py"
    )

    if not encoder_path.exists():
        print(
            "[SKIP] Encoder file not found next to decoder; "
            "decoder module itself was loaded successfully."
        )
        return

    spec = importlib.util.spec_from_file_location(
        "vde_encoder_for_test",
        str(encoder_path),
    )
    encoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(encoder)

    rng = np.random.default_rng(20260810)

    original = rng.integers(
        0, 2, size=432, dtype=np.uint8
    ).tolist()

    encoded = encoder.turbo_encode(original, 11)

    # Test A: no channel error
    decoded, _ = turbo_decode(
        encoded,
        link_id=11,
        iterations=6,
        hard_input=True,
        hard_llr=2.0,
    )

    errors = int(
        np.count_nonzero(
            np.asarray(decoded, dtype=np.uint8)
            != np.asarray(original, dtype=np.uint8)
        )
    )

    assert errors == 0

    print("[PASS] Test A: no-error decode")
    print("       874 coded bits -> 432 original bits")
    print("       decoded bit errors = 0")

    # Test B: deterministic 20 coded-bit flips
    corrupted = np.asarray(
        encoded,
        dtype=np.uint8,
    )

    flip_positions = rng.choice(
        len(corrupted),
        size=20,
        replace=False,
    )

    corrupted[flip_positions] ^= 1

    decoded_corrupt, _ = turbo_decode(
        corrupted.tolist(),
        link_id=11,
        iterations=6,
        hard_input=True,
        hard_llr=2.0,
    )

    residual_errors = int(
        np.count_nonzero(
            np.asarray(decoded_corrupt, dtype=np.uint8)
            != np.asarray(original, dtype=np.uint8)
        )
    )

    assert residual_errors == 0

    print("[PASS] Test B: 20 coded-bit flips corrected")
    print("       inserted channel bit errors = 20")
    print("       decoded bit errors          = 0")


if __name__ == "__main__":
    _self_test()

