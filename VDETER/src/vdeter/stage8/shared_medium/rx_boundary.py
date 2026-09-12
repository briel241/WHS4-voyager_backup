"""수신 창 경계의 **실제 합성 입력** 구성(명세 §7.1~§7.3, P12).

기존 `synthesize_same_channel`은 겹치는 IQ를 선형 합산하고 virtual frontend로 clipping한다. 그러나
그 함수는 지금까지 **송신 parent에 사후로만** 불렸다 — 수신 창에 실제로 도달한 입력을 모아 한 번
decode하는 경계가 없었다. 그 결과 (a) 정상 TX가 합성 입력에서 빠지고 (b) 한 창에 둘이 겹치면 하나가
조용히 버려졌다.

이 module이 그 경계다. 한 수신기의 한 lane·한 창에 대해:

1. **채널 구분**: 그 lane이 실제로 tuned된 실효 중심과 일치하는 도달만 받는다. 다른 채널의 IQ는
   섞지 않는다(§7.1). 일치하지 않으면 `NOT_TUNED`이며 수신 실패가 아니다.
2. **시간 정렬**: 도달 시각을 **수신기 자신의** hardware ns → 창 기준 sample index로 환산한다.
   송신자 좌표를 그대로 쓰지 않는다.
3. **합산**: 겹치는 sample은 복소 선형 합산 + virtual clipping, 겹치지 않는 구간은 단일 입력을
   그대로 둔다(byte-identity 보존).
4. **중복 방지**: 같은 도달을 두 번 합산하지 않는다(도달 식별자 기준).
5. **단일 decode**: 합성 결과 하나만 기존 PHY 경계로 내보낸다. source별로 따로 복호해 합치지 않는다.

정상 TX와 PoC TX를 구분하지 않는다 — 둘 다 같은 매질에 실린 입력이다. 결과는 **결정적 근사 모델**
이며(`WIRED_SHARED_MEDIUM_EMULATION`), 실제 ADC 포화를 증명하지 않는다. evidence가 그렇게 표시한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import frontend
from .iq import IqBurst, from_cf32le, synthesize_same_channel, to_cf32le

#: 한 창에 합성할 수 있는 최대 도달 수(유계 — 무한 누적 금지).
MAX_WINDOW_ARRIVALS = 8

#: 창 정렬 허용 오차(sample). 장치 assembler가 창 경계에 맞추는 것과 같은 값이며
#: 시간 이동이 아니라 양자화 잔차를 흡수한다. 이 값을 넓히면 실제 지연을 0으로
#: 뭉개게 되므로 넓히지 않는다.
ALIGNMENT_TOLERANCE_SAMPLES = 1


class RxBoundaryError(ValueError):
    """수신 경계 합성 계약 위반."""


@dataclass(frozen=True, slots=True)
class RxArrival:
    """한 수신 lane에 도달한 IQ 하나(정상 TX·PoC TX 구분 없음)."""

    arrival_id: str
    #: 수신기 자신의 hardware ns(송신자 좌표가 아니다).
    receiver_hw_ns: int
    #: 이 도달이 실린 실효 중심 주파수(Hz). 창의 tuned 중심과 일치해야 합성에 들어간다.
    effective_center_hz: int
    data: bytes
    source: str | None = None


@dataclass(frozen=True, slots=True)
class RxComposite:
    """한 창의 합성 결과와 그 근거."""

    data: bytes
    sample_count: int
    input_count: int
    overlap_sample_count: int
    clip_count: int
    pre_clip_peak: float
    post_clip_peak: float
    not_tuned_count: int
    admitted: tuple
    rejected: tuple
    frontend_version: str = frontend.VIRTUAL_FRONTEND_VERSION

    def as_evidence(self) -> dict:
        """모델 결과임을 명시한 evidence(실 RF 수신 주장 아님)."""

        return {
            "model": "WIRED_SHARED_MEDIUM_EMULATION",
            "virtual_clipping": "deterministic_approximation",
            "analog_adc_saturation_proven": False,
            "frontend_version": self.frontend_version,
            "sample_count": self.sample_count,
            "input_count": self.input_count,
            "overlap_sample_count": self.overlap_sample_count,
            "clip_count": self.clip_count,
            "pre_clip_peak": f"{self.pre_clip_peak:.6f}",
            "post_clip_peak": f"{self.post_clip_peak:.6f}",
            "not_tuned_count": self.not_tuned_count,
            "admitted_arrivals": list(self.admitted),
            "rejected_arrivals": list(self.rejected),
        }


def compose_rx_window(*, arrivals, window_start_ns: int, sample_count: int,
                      sample_rate_hz: int, tuned_center_hz, channel) -> RxComposite:
    """한 수신 창의 **합성 입력 하나**를 만든다. 이것만 기존 PHY 복호로 들어간다.

    `tuned_center_hz`가 None이면 그 lane은 어디에도 tuned되지 않은 것이므로 모든 도달이
    `NOT_TUNED`이고 합성 결과는 무음이다(수신 실패가 아니라 정상 상태).
    """

    window_span_ns = (int(sample_count) * 1_000_000_000 + int(sample_rate_hz) - 1) \
        // int(sample_rate_hz)
    window_end_ns = int(window_start_ns) + window_span_ns
    ns_per_sample = 1_000_000_000 / float(sample_rate_hz)

    bursts = []
    admitted = []
    rejected = []
    not_tuned = 0
    seen = set()
    for arrival in arrivals or ():
        if arrival.arrival_id in seen:
            # 같은 도달을 두 번 합산하지 않는다(중복 합산 금지).
            rejected.append((arrival.arrival_id, "DUPLICATE"))
            continue
        seen.add(arrival.arrival_id)
        if tuned_center_hz is None or arrival.effective_center_hz != tuned_center_hz:
            not_tuned += 1
            rejected.append((arrival.arrival_id, "NOT_TUNED"))
            continue
        samples = from_cf32le(arrival.data)
        arrival_end_ns = arrival.receiver_hw_ns + int(len(samples) * ns_per_sample)
        if arrival_end_ns <= int(window_start_ns) or arrival.receiver_hw_ns >= window_end_ns:
            rejected.append((arrival.arrival_id, "OUTSIDE_WINDOW"))
            continue
        # 수신기 좌표로 정렬한다: 창 시작 기준 sample offset(부분 겹침이면 음수일 수 있다).
        offset = int(round((arrival.receiver_hw_ns - int(window_start_ns)) / ns_per_sample))
        # 한 sample 이내 차이는 시간 이동이 아니라 양자화 잔차다. 장치 assembler가 창에 맞춰
        # 정렬하는 것과 같이 0으로 스냅한다 — 그러지 않으면 정렬된 단일 입력이 한 sample
        # 밀려 byte-identity(따라서 복호)가 깨진다.
        if abs(offset) <= ALIGNMENT_TOLERANCE_SAMPLES:
            offset = 0
        bursts.append(IqBurst(channel=channel, start_sample=offset, samples=samples))
        admitted.append(arrival.arrival_id)
        if len(bursts) > MAX_WINDOW_ARRIVALS:
            raise RxBoundaryError(
                f"창 하나의 도달 수가 상한 {MAX_WINDOW_ARRIVALS}를 넘습니다")

    total = int(sample_count)
    if not bursts:
        # 도달 없음 또는 전부 NOT_TUNED → 무음 창(없는 수신을 만들지 않는다).
        return RxComposite(data=b"\x00" * (total * 8), sample_count=total,
                           input_count=0, overlap_sample_count=0, clip_count=0,
                           pre_clip_peak=0.0, post_clip_peak=0.0,
                           not_tuned_count=not_tuned,
                           admitted=tuple(admitted), rejected=tuple(rejected))

    composite = synthesize_same_channel(bursts)
    # 합성 좌표를 창 좌표로 옮겨 담는다(창 밖으로 나간 부분은 잘라낸다 — 창은 고정 길이다).
    out = [0j] * total
    base = composite.base_start_sample
    for index, sample in enumerate(composite.samples):
        position = base + index
        if 0 <= position < total:
            out[position] = sample
    return RxComposite(
        data=to_cf32le(out), sample_count=total,
        input_count=composite.input_count,
        overlap_sample_count=composite.overlap_sample_count,
        clip_count=composite.clip_count,
        pre_clip_peak=composite.pre_clip_peak,
        post_clip_peak=composite.post_clip_peak,
        not_tuned_count=not_tuned,
        admitted=tuple(admitted), rejected=tuple(rejected),
    )


__all__ = ("RxArrival", "RxComposite", "RxBoundaryError", "compose_rx_window",
           "MAX_WINDOW_ARRIVALS", "ALIGNMENT_TOLERANCE_SAMPLES")
