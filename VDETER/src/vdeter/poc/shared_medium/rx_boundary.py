"""RF-off lane/window fixture: copy nonoverlapping arrivals; drop overlapped bursts.

Not the production collision authority. Production compares co-decoded RX lane
intervals locally in a completed logical window. This fixture alone knows all
input arrivals. No IQ addition, clipping, ADC/capture model.
"""
from dataclasses import dataclass
from .iq import from_cf32le, to_cf32le

MAX_WINDOW_ARRIVALS = 8
ALIGNMENT_TOLERANCE_SAMPLES = 1

class RxBoundaryError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class RxArrival:
    arrival_id: str
    receiver_hw_ns: int
    effective_center_hz: int
    data: bytes
    source: str | None = None

@dataclass(frozen=True, slots=True)
class RxWindowRender:
    data: bytes
    sample_count: int
    input_count: int
    dropped_input_count: int
    not_tuned_count: int
    admitted: tuple
    rejected: tuple

    def as_evidence(self):
        return dict(model="TX_INTERVAL_DROP_V1", simulation_only=True,
                    rf_collision_measured=False, sample_count=self.sample_count,
                    input_count=self.input_count, dropped_input_count=self.dropped_input_count,
                    not_tuned_count=self.not_tuned_count,
                    admitted_arrivals=list(self.admitted), rejected_arrivals=list(self.rejected))

def compose_rx_window(*, arrivals, window_start_ns, sample_count,
                      sample_rate_hz, tuned_center_hz, channel):
    if type(sample_count) is not int or not 0 < sample_count <= 65536:
        raise RxBoundaryError("bounded positive sample count required")
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise RxBoundaryError("positive sample rate required")
    window_end = window_start_ns + (sample_count * 1_000_000_000 + sample_rate_hz - 1) // sample_rate_hz
    accepted, rejected, seen, not_tuned = [], [], set(), 0
    for arrival in arrivals or ():
        if len(seen) >= MAX_WINDOW_ARRIVALS:
            raise RxBoundaryError("arrival capacity exceeded")
        if arrival.arrival_id in seen:
            rejected.append((arrival.arrival_id, "DUPLICATE"))
            continue
        seen.add(arrival.arrival_id)
        if tuned_center_hz is None or arrival.effective_center_hz != tuned_center_hz:
            not_tuned += 1
            rejected.append((arrival.arrival_id, "NOT_TUNED"))
            continue
        samples = from_cf32le(arrival.data)
        end = arrival.receiver_hw_ns + (len(samples) * 1_000_000_000 + sample_rate_hz - 1) // sample_rate_hz
        if end <= window_start_ns or arrival.receiver_hw_ns >= window_end:
            rejected.append((arrival.arrival_id, "OUTSIDE_WINDOW"))
            continue
        accepted.append((arrival, samples, end))
    dropped = set()
    for i, (a, _, ae) in enumerate(accepted):
        for j, (b, _, be) in enumerate(accepted[i + 1:], i + 1):
            if a.receiver_hw_ns < be and b.receiver_hw_ns < ae:
                dropped.update((i, j))
    out = [0j] * sample_count
    for i, (arrival, samples, _) in enumerate(accepted):
        if i in dropped:
            rejected.append((arrival.arrival_id, "MODEL_COLLISION_DROP"))
            continue
        offset = round((arrival.receiver_hw_ns - window_start_ns) * sample_rate_hz / 1_000_000_000)
        if abs(offset) <= ALIGNMENT_TOLERANCE_SAMPLES:
            offset = 0
        lo, hi = max(0, offset), min(sample_count, offset + len(samples))
        out[lo:hi] = samples[lo - offset:hi - offset]
    return RxWindowRender(to_cf32le(out), sample_count, len(accepted), len(dropped),
                          not_tuned, tuple(a.arrival_id for a, _, _ in accepted), tuple(rejected))

__all__ = ("RxArrival", "RxWindowRender", "RxBoundaryError", "compose_rx_window",
           "MAX_WINDOW_ARRIVALS", "ALIGNMENT_TOLERANCE_SAMPLES")
