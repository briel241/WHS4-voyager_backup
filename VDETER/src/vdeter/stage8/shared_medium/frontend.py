"""Versioned virtual receive frontend transfer function (명세 §7.2, WSM-D09).

합산된 composite IQ가 승인된 full-scale를 넘으면 **자동 정규화하지 않고** 고정·versioned된
transfer function으로 포화·clipping한다. 이 값은 명세 §7.2가 "구현 전에 명세 fixture로
pin"하라고 요구한 항목이며, 여기 단일 정의로 고정한다. 시험 통과 목적으로 바꾸지 않는다.

이 virtual clipping은 단일 실제 ADC의 아날로그 포화를 증명하지 않는 결정적 근사이며
(`WIRED_SHARED_MEDIUM_EMULATION`), evidence에 그렇게 표시한다.

pinned 값(§7.2):
- full-scale 정의: complex baseband 각 축(I, Q)을 [-1.0, +1.0]로 독립 hard-clip.
- clipping 방식: 직사각(per-component) hard clip. magnitude scaling(원형 clip) 아님.
- clipping sample count: I 또는 Q가 full-scale를 벗어난 complex sample 수.
- peak 계산: composite의 모든 sample에서 max(|I|, |Q|)(축 최대 절댓값).
- quantization/추가 noise: **적용하지 않음**(v1). 적용 시 명세 변경 승인 필요.
"""

from __future__ import annotations

#: transfer function 버전. evidence·golden vector에 기록한다.
VIRTUAL_FRONTEND_VERSION = "WSM-VFE-1"

#: complex baseband 각 축의 full-scale(정규화 단위). §7.2 pinned.
FULL_SCALE = 1.0

#: v1은 quantization·추가 noise를 적용하지 않는다(명세 fixture pin).
QUANTIZATION = "none"


def clip_component(value: float) -> float:
    """단일 축(I 또는 Q)을 [-FULL_SCALE, +FULL_SCALE]로 hard-clip."""

    if value > FULL_SCALE:
        return FULL_SCALE
    if value < -FULL_SCALE:
        return -FULL_SCALE
    return value


def clip_sample(sample: complex) -> complex:
    """complex sample의 I·Q를 독립적으로 hard-clip(직사각 clipping)."""

    return complex(clip_component(sample.real), clip_component(sample.imag))


def is_clipped(sample: complex) -> bool:
    """이 complex sample이 어느 축이든 full-scale를 벗어나면 True."""

    return abs(sample.real) > FULL_SCALE or abs(sample.imag) > FULL_SCALE


def axis_peak(samples) -> float:
    """sample 열의 축 최대 절댓값 peak = max(|I|, |Q|)."""

    peak = 0.0
    for sample in samples:
        ri = sample.real if sample.real >= 0.0 else -sample.real
        qi = sample.imag if sample.imag >= 0.0 else -sample.imag
        if ri > peak:
            peak = ri
        if qi > peak:
            peak = qi
    return peak


__all__ = (
    "VIRTUAL_FRONTEND_VERSION",
    "FULL_SCALE",
    "QUANTIZATION",
    "clip_component",
    "clip_sample",
    "is_clipped",
    "axis_peak",
)
