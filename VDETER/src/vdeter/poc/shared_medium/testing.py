"""Stage 5 shared-medium 시험용 test double(spy/counter).

Stage 4 track과 파일을 공유하지 않도록 shared_medium 소유 module에 둔다. UHD 없이 실행 가능한
순수 software double이며, atomic group commit·single-burst·fan-out·priority·fail-closed·
deadline·terminal 취합을 검증한다.
"""

from __future__ import annotations

from .radiation import BroadcastCommitRefused, GroupAdmission, ReadinessDecision


class CountingRadioSink:
    """RF submit spy(atomic 2단계). group을 prepare -> commit하며 per-child 순차 제출 경로가 없다.

    - `fail_on_prepare`: prepare_group에서 예외 -> group 전체 거부, commit 0, 방사 child 0.
    - `fail_on_commit`: commit_group에서 예외 -> group commit 불명확(상위가 OUTCOME_UNKNOWN 처리).
    - `refuse_on_commit`: commit_group이 **방사 전 명시적 거부**를 올린다(TX 0 확정, 불명확 아님).
    commit_group은 atomic이라 admission의 두 child를 모두 기록하거나(성공) 기록 전에 예외를 던진다.
    """

    def __init__(self, *, fail_on_prepare: bool = False, fail_on_commit: bool = False,
                 refuse_on_commit: bool = False) -> None:
        self.prepare_calls = 0
        self.commit_calls = 0
        self.prepared_group_ids: list = []
        self.committed_child_ids: list = []
        self.per_child_count: dict = {}
        self.submitted_waveforms: set = set()
        self.submitted_parents: dict = {}
        self._fail_prepare = fail_on_prepare
        self._fail_commit = fail_on_commit
        self._refuse_commit = refuse_on_commit

    def prepare_group(self, *, group_id: str, children) -> GroupAdmission:
        self.prepare_calls += 1
        if self._fail_prepare:
            raise RuntimeError(f"injected prepare_group failure for {group_id}")
        self.prepared_group_ids.append(group_id)
        return GroupAdmission(group_id=group_id, children=tuple(children))

    def commit_group(self, admission: GroupAdmission) -> None:
        self.commit_calls += 1
        if self._refuse_commit:
            raise BroadcastCommitRefused(f"injected commit refusal for {admission.group_id}")
        if self._fail_commit:
            raise RuntimeError(f"injected commit_group failure for {admission.group_id}")
        # atomic: 두 child를 함께 기록(부분 기록 없음).
        for child in admission.children:
            self.committed_child_ids.append(child.child_id)
            self.per_child_count[child.child_id] = self.per_child_count.get(child.child_id, 0) + 1
            self.submitted_waveforms.add(child.waveform_sha256)
            self.submitted_parents.setdefault(child.parent_id, []).append(child.child_id)

    # 방사(commit)된 child 수. 기존 시험 호환용 별칭.
    @property
    def submit_calls(self) -> int:
        return len(self.committed_child_ids)

    @property
    def submitted_child_ids(self) -> list:
        return self.committed_child_ids

    @property
    def duplicate_child_submits(self) -> int:
        return sum(1 for count in self.per_child_count.values() if count > 1)


class CountingWaveformFactory:
    """waveform 생성 횟수를 세는 factory. fan-out으로 waveform이 재생성되지 않음을 증명한다."""

    def __init__(self, inner=None) -> None:
        from .radiation import ReferenceWaveformFactory

        self._inner = inner or ReferenceWaveformFactory()
        self.encode_calls = 0

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
            return False
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
