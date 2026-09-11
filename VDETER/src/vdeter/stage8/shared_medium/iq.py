"""IQ sample-domain 표현·직렬화·합성 (명세 §5.1, §7.1, §7.2).

- byte-pinned IQ는 기존 `cf32le` format(interleaved float32 little-endian I,Q)을 쓴다
  (`common/contracts/radio.py`의 `SAMPLE_FORMAT_CF32LE`와 동일 wire 표현). numpy를
  쓰지 않고 stdlib `struct`만 사용한다(clean checkout 재현성·의존성 0).
- 합성 arithmetic은 명세 §7.1 결정만 사용한다: **같은 Physical Channel이며 시간이 겹치는
  구간만** complex IQ 선형 합산, 겹치지 않는 구간은 해당 input을 그대로 사용(copy).
  이 선형 복소 합산은 기존 `radio/software/medium.py`의 render 합산 규약과 동일하다.
- composite full-scale 초과는 versioned virtual frontend(:mod:`frontend`)로만 clipping한다.
  명세에 없는 정규화·scaling·보정 상수는 추가하지 않는다.

이 module을 import해도 thread·socket·disk write가 생기지 않는다.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from ..poc_api.types import PhysicalChannel
from . import frontend
from .constants import MAX_BURST_SAMPLES, MAX_COLLISION_GROUP_BURSTS

#: byte-pinned wire format 이름(기존 등록 format 재사용).
SAMPLE_FORMAT = "cf32le"
#: sample당 byte 수(float32 I + float32 Q).
BYTES_PER_SAMPLE = 8


class IqError(ValueError):
    """IQ 표현·합성 규칙 위반."""


def to_cf32le(samples) -> bytes:
    """complex sample 열 → interleaved float32 LE bytes(byte-pinned)."""

    seq = tuple(samples)
    if len(seq) > MAX_BURST_SAMPLES:
        raise IqError(f"sample 수 {len(seq)}가 상한 {MAX_BURST_SAMPLES}를 넘습니다")
    flat = []
    for sample in seq:
        c = complex(sample)
        flat.append(c.real)
        flat.append(c.imag)
    return struct.pack("<%df" % (len(seq) * 2), *flat)


def from_cf32le(data: bytes) -> tuple:
    """interleaved float32 LE bytes → complex sample tuple."""

    if not isinstance(data, (bytes, bytearray)):
        raise IqError("bytes 입력이 필요합니다")
    if len(data) % BYTES_PER_SAMPLE != 0:
        raise IqError(f"cf32le bytes는 {BYTES_PER_SAMPLE}의 배수여야 합니다 ({len(data)})")
    count = len(data) // BYTES_PER_SAMPLE
    if count > MAX_BURST_SAMPLES:
        raise IqError(f"sample 수 {count}가 상한 {MAX_BURST_SAMPLES}를 넘습니다")
    flat = struct.unpack("<%df" % (count * 2), bytes(data))
    return tuple(complex(flat[i], flat[i + 1]) for i in range(0, len(flat), 2))


def sha256_cf32le(samples) -> str:
    """byte-pinned IQ의 SHA-256(cf32le bytes 기준)."""

    return hashlib.sha256(to_cf32le(samples)).hexdigest()


@dataclass(frozen=True, slots=True)
class IqBurst:
    """한 waveform burst. parent가 한 번 생성하고 fan-out child가 공유한다.

    `start_sample`은 receiver 공통 hardware-time/sample 좌표의 시작 index다(§7.1 정렬).
    """

    channel: PhysicalChannel
    start_sample: int
    samples: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise IqError("IqBurst.channel: PhysicalChannel이어야 합니다")
        if isinstance(self.start_sample, bool) or not isinstance(self.start_sample, int):
            raise IqError("IqBurst.start_sample: 정수여야 합니다")
        if self.start_sample < 0:
            raise IqError("IqBurst.start_sample: 0 이상이어야 합니다")
        if not isinstance(self.samples, tuple):
            raise IqError("IqBurst.samples: tuple이어야 합니다")
        if len(self.samples) == 0:
            raise IqError("IqBurst.samples: 비어 있을 수 없습니다")
        if len(self.samples) > MAX_BURST_SAMPLES:
            raise IqError(f"IqBurst.samples: 상한 {MAX_BURST_SAMPLES} 초과")

    @property
    def end_sample(self) -> int:
        """exclusive end index."""

        return self.start_sample + len(self.samples)

    @property
    def cf32le(self) -> bytes:
        return to_cf32le(self.samples)

    @property
    def sha256(self) -> str:
        return sha256_cf32le(self.samples)


def time_overlaps(a: IqBurst, b: IqBurst) -> bool:
    """두 burst가 sample-time 구간에서 실제 겹치면 True(경계 접촉은 비겹침)."""

    return a.start_sample < b.end_sample and b.start_sample < a.end_sample


@dataclass(frozen=True, slots=True)
class CompositeResult:
    """한 receiver·channel의 겹침 구간 합성 결과(§7.1, §7.2).

    비겹침 구간은 단일 input을 그대로 copy했고, 겹침 구간만 선형 합산 후 virtual frontend
    clipping을 적용했다. per-source·pre/post-clip peak과 clip count를 evidence로 남긴다.
    """

    channel: PhysicalChannel
    base_start_sample: int
    samples: tuple
    input_count: int
    overlap_sample_count: int
    clip_count: int
    pre_clip_peak: float
    post_clip_peak: float
    per_source_peak: tuple
    frontend_version: str = frontend.VIRTUAL_FRONTEND_VERSION

    @property
    def cf32le(self) -> bytes:
        return to_cf32le(self.samples)

    @property
    def sha256(self) -> str:
        return sha256_cf32le(self.samples)


def group_collisions(bursts) -> list:
    """burst들을 collision group으로 나눈다(명세 §7.1, §7.3, WSM-REQ-05).

    두 burst는 (1) 같은 Physical Channel이고 (2) sample-time이 실제 겹칠 때만 같은 group이며,
    겹침 연결은 전이적으로 묶는다(union-find). 다른 channel이거나 시간이 겹치지 않는 burst는
    별도 group이 되어 서로 충돌로 처리하지 않는다. group 크기 1은 단일 관측(합성 없음)이다.

    입력 순서를 보존한 list[list[IqBurst]]를 돌려준다.
    """

    items = list(bursts)
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        for j in range(i + 1, n):
            if items[i].channel == items[j].channel and time_overlaps(items[i], items[j]):
                union(i, j)

    groups: dict = {}
    order: list = []
    for idx in range(n):
        root = find(idx)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(items[idx])
    return [groups[root] for root in order]


def synthesize_same_channel(bursts) -> CompositeResult:
    """같은 Physical Channel의 겹치는 peer IQ를 hardware-time으로 정렬·합성한다(§7.1).

    - 겹치는 sample: complex 선형 합산 후 virtual frontend clipping.
    - 겹치지 않는 sample: 단일 input을 그대로 copy(합산·clip 없음 → byte-identity 보존).
    - 모든 burst는 같은 channel이어야 한다(다른 channel은 caller가 분리한다).
    - fan-out sibling(같은 parent) 검출은 상위 collision grouping이 보장한다.
    """

    seq = list(bursts)
    if not seq:
        raise IqError("합성할 burst가 없습니다")
    if len(seq) > MAX_COLLISION_GROUP_BURSTS:
        raise IqError(
            f"collision group burst 수 {len(seq)}가 상한 {MAX_COLLISION_GROUP_BURSTS}를 넘습니다"
        )
    channel = seq[0].channel
    for burst in seq:
        if burst.channel != channel:
            raise IqError("synthesize_same_channel: 서로 다른 Physical Channel은 합성하지 않습니다")

    base = min(b.start_sample for b in seq)
    end = max(b.end_sample for b in seq)
    length = end - base
    if length > MAX_BURST_SAMPLES:
        raise IqError(f"composite 길이 {length}가 상한 {MAX_BURST_SAMPLES}를 넘습니다")

    # position별 contributor 수를 먼저 세어 단일/다중을 구분한다.
    contributors = [0] * length
    for burst in seq:
        off = burst.start_sample - base
        for i in range(len(burst.samples)):
            contributors[off + i] += 1

    out = [0j] * length
    # 단일 contributor 위치는 그대로 copy(§7.1: 겹치지 않는 구간은 해당 input만).
    for burst in seq:
        off = burst.start_sample - base
        for i, sample in enumerate(burst.samples):
            pos = off + i
            if contributors[pos] == 1:
                out[pos] = sample
            else:
                out[pos] = out[pos] + sample  # 겹침: 선형 복소 합산

    pre_clip_peak = frontend.axis_peak(out)

    clip_count = 0
    overlap_sample_count = 0
    for pos in range(length):
        if contributors[pos] >= 2:
            overlap_sample_count += 1
            raw = out[pos]
            if frontend.is_clipped(raw):
                clip_count += 1
                out[pos] = frontend.clip_sample(raw)
        # 단일 contributor는 copy 그대로(clip 미적용) → byte-identity 보존.

    post_clip_peak = frontend.axis_peak(out)
    per_source_peak = tuple(frontend.axis_peak(b.samples) for b in seq)

    return CompositeResult(
        channel=channel,
        base_start_sample=base,
        samples=tuple(out),
        input_count=len(seq),
        overlap_sample_count=overlap_sample_count,
        clip_count=clip_count,
        pre_clip_peak=pre_clip_peak,
        post_clip_peak=post_clip_peak,
        per_source_peak=per_source_peak,
    )


__all__ = (
    "SAMPLE_FORMAT",
    "BYTES_PER_SAMPLE",
    "IqError",
    "to_cf32le",
    "from_cf32le",
    "sha256_cf32le",
    "IqBurst",
    "time_overlaps",
    "CompositeResult",
    "group_collisions",
    "synthesize_same_channel",
)
