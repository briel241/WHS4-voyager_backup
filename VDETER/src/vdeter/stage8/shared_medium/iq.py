"""TX IQ wire representation + bounded RF-off noncollision fixture rendering.

No synthetic IQ sum/clipping/capture model. Real RX uses the TX-record gate.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from ..poc_api.types import PhysicalChannel
from .constants import MAX_BURST_SAMPLES, MAX_COLLISION_GROUP_BURSTS

#: byte-pinned wire format 이름(기존 등록 format 재사용).
SAMPLE_FORMAT = "cf32le"
#: sample당 byte 수(float32 I + float32 Q).
BYTES_PER_SAMPLE = 8


class IqError(ValueError):
    """IQ wire representation or bounded RF-off rendering violation."""


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
    wire_packet_digest: str | None = None
    #: 이미 만들어 둔 cf32le 직렬화. `cf32le`/`sha256`이 매 접근마다 8,192 표본을 다시 pack하는
    #: 것을 막는다(한 action에서 full-scale 검증·parent digest·장치 제출로 최소 3회 반복됐다).
    #: 생성자가 채워 준 경우에만 쓰며, 없으면 종전대로 계산한다(다른 factory 경로 호환).
    cf32le_bytes: bytes | None = None

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
        if self.cf32le_bytes is not None:
            # 캐시는 편의값이 아니라 계약이다 — samples와 어긋나면 장치에 다른 바이트가 나간다.
            if not isinstance(self.cf32le_bytes, bytes):
                raise IqError("IqBurst.cf32le_bytes: bytes여야 합니다")
            if len(self.cf32le_bytes) != len(self.samples) * BYTES_PER_SAMPLE:
                raise IqError("IqBurst.cf32le_bytes: samples와 길이가 맞지 않습니다")

    @property
    def end_sample(self) -> int:
        """exclusive end index."""

        return self.start_sample + len(self.samples)

    @property
    def cf32le(self) -> bytes:
        return self.cf32le_bytes if self.cf32le_bytes is not None else to_cf32le(self.samples)

    @property
    def sha256(self) -> str:
        if self.cf32le_bytes is not None:
            return hashlib.sha256(self.cf32le_bytes).hexdigest()
        return sha256_cf32le(self.samples)


def time_overlaps(a: IqBurst, b: IqBurst) -> bool:
    """두 burst가 sample-time 구간에서 실제 겹치면 True(경계 접촉은 비겹침)."""

    return a.start_sample < b.end_sample and b.start_sample < a.end_sample



@dataclass(frozen=True, slots=True)
class WindowRender:
    """RF-off fixture rendering; no addition, clipping, or capture effect."""
    channel: PhysicalChannel
    base_start_sample: int
    samples: tuple
    input_count: int
    overlap_sample_count: int
    dropped_input_count: int

    @property
    def cf32le(self):
        return to_cf32le(self.samples)

    @property
    def sha256(self):
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



def render_noncolliding(bursts) -> WindowRender:
    """Copy isolated inputs unchanged; any overlapped input is dropped entirely.

    RF-off sample coordinates only. Production decisions use the complete TX
    journal in stage7.collision_runtime, not this synthetic fixture renderer.
    """
    seq = list(bursts)
    if not seq or len(seq) > MAX_COLLISION_GROUP_BURSTS:
        raise IqError("bounded nonempty input group required")
    if any(b.channel != seq[0].channel for b in seq):
        raise IqError("mixed physical channels")
    base, end = min(b.start_sample for b in seq), max(b.end_sample for b in seq)
    if end - base > MAX_BURST_SAMPLES:
        raise IqError("render window exceeds bound")
    dropped, intersections = set(), []
    for i, a in enumerate(seq):
        for j, b in enumerate(seq[i + 1:], i + 1):
            if time_overlaps(a, b):
                dropped.update((i, j))
                intersections.append((max(a.start_sample, b.start_sample), min(a.end_sample, b.end_sample)))
    overlap, previous_end = 0, base
    for start, stop in sorted(intersections):
        overlap += max(0, stop - max(start, previous_end))
        previous_end = max(previous_end, stop)
    out = [0j] * (end - base)
    for i, burst in enumerate(seq):
        if i not in dropped:
            offset = burst.start_sample - base
            out[offset:offset + len(burst.samples)] = burst.samples
    return WindowRender(seq[0].channel, base, tuple(out), len(seq), overlap, len(dropped))


__all__ = ("SAMPLE_FORMAT", "BYTES_PER_SAMPLE", "IqError", "to_cf32le",
           "from_cf32le", "sha256_cf32le", "IqBurst", "time_overlaps",
           "WindowRender", "group_collisions", "render_noncolliding")
