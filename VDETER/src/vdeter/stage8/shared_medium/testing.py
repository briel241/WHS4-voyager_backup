"""Stage 5 shared-medium 시험용 test double(spy/counter).

Stage 4 track과 파일을 공유하지 않도록 shared_medium 소유 module에 둔다. UHD 없이 실행 가능한
순수 software double이며, exactly-once·single-burst·fan-out·priority·fail-closed를 검증한다.
"""

from __future__ import annotations

from .radiation import ReadinessDecision, ReferenceWaveformFactory


class CountingRadioSink:
    """RF submit spy. child submit 횟수·중복을 세어 exactly-once/no-duplicate를 증명한다.

    `fail_on_index`를 주면 그 index(0-base)의 submit에서 예외를 던져 atomic rollback을 시험한다.
    """

    def __init__(self, *, fail_on_index=None) -> None:
        self.submit_calls = 0
        self.submitted_child_ids: list = []
        self.per_child_count: dict = {}
        self.submitted_waveforms: set = set()
        self.submitted_parents: dict = {}
        self._fail_on_index = fail_on_index

    def submit_child(self, child) -> None:
        index = self.submit_calls
        self.submit_calls += 1
        if self._fail_on_index is not None and index == self._fail_on_index:
            raise RuntimeError(f"injected RF submit failure at index {index}")
        self.submitted_child_ids.append(child.child_id)
        self.per_child_count[child.child_id] = self.per_child_count.get(child.child_id, 0) + 1
        self.submitted_waveforms.add(child.waveform_sha256)
        self.submitted_parents.setdefault(child.parent_id, []).append(child.child_id)

    @property
    def duplicate_child_submits(self) -> int:
        return sum(1 for count in self.per_child_count.values() if count > 1)


class CountingWaveformFactory:
    """waveform 생성 횟수를 세는 factory. fan-out으로 waveform이 재생성되지 않음을 증명한다."""

    def __init__(self, inner=None) -> None:
        self._inner = inner or ReferenceWaveformFactory()
        self.encode_calls = 0
        self.encoded_payload_digests: list = []

    def encode(self, *, source_station: str, channel, payload: bytes):
        self.encode_calls += 1
        return self._inner.encode(source_station=source_station, channel=channel, payload=payload)


class ReadyReadiness:
    """모든 required receiver가 준비된 상태(정상 경로)."""

    def __init__(self) -> None:
        self.requests: list = []

    def window_ready(self, request) -> ReadinessDecision:
        self.requests.append(request)
        return ReadinessDecision(
            ready=True,
            reason="READY",
            proof={
                "endpoint": request.endpoint,
                "channel": request.channel.value,
                "slot_key": request.slot_key,
                "clock_id": request.clock_id,
                "bridge_generation": request.bridge_generation,
                "correction_revision": request.correction_revision,
            },
        )


class SelectiveReadiness:
    """지정한 endpoint만 not-ready로 만들어 RF 전 fail-closed를 시험한다."""

    def __init__(self, *, not_ready_endpoints=()) -> None:
        self._not_ready = set(not_ready_endpoints)
        self.requests: list = []

    def window_ready(self, request) -> ReadinessDecision:
        self.requests.append(request)
        if request.endpoint in self._not_ready:
            return ReadinessDecision(ready=False, reason="RX_WINDOW_NOT_READY")
        return ReadinessDecision(ready=True, reason="READY")


class FakeArbiter:
    """정상 TX 우선 자원 arbiter. 미리 점유된 resource는 정상 TX 소유로 본다.

    PoC의 reserve는 정상 점유 자원에서 False를 돌려주고, 정상 예약을 절대 취소하지 않는다.
    """

    def __init__(self, *, normal_reserved=()) -> None:
        self.owner_by_key: dict = {key: "NORMAL" for key in normal_reserved}
        self.reserve_calls: list = []
        self.release_calls: list = []

    def reserve(self, *, resource_key: str, owner: str) -> bool:
        self.reserve_calls.append((resource_key, owner))
        existing = self.owner_by_key.get(resource_key)
        if existing is not None and existing != owner:
            return False  # 정상 TX가 점유 중 → PoC 거부(정상 TX 유지).
        self.owner_by_key[resource_key] = owner
        return True

    def release(self, *, resource_key: str, owner: str) -> None:
        self.release_calls.append((resource_key, owner))
        if self.owner_by_key.get(resource_key) == owner:
            del self.owner_by_key[resource_key]


__all__ = (
    "CountingRadioSink",
    "CountingWaveformFactory",
    "ReadyReadiness",
    "SelectiveReadiness",
    "FakeArbiter",
)
