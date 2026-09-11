"""RF 장비 경계 adapter (production 연결 + RF-off software harness).

Stage 5 provider의 `RadioBurstSink` seam(§6.1: prepare_group -> commit_group의 atomic 2단계)을
Stage 7 group TX 경계에 연결한다. WSM `seams.py`가 정한 매핑을 그대로 구현한다:

    prepare_group  = 두 lane staging/retune 예약 (`retune_tx_lo`)
    commit_group   = 같은 hardware timestamp의 group TX 단일 atomic 제출 (`submit_tx_group`)

`Stage7GroupTxRadioSink`는 실장비 Stage 7 `b210_worker`(retune_tx_lo/submit_tx_group)에 **연결
가능한** production adapter다. RF-off에서는 실장비 대신 `SoftwareGroupTx`를 주입한다. 이 software
harness는 **실제 UHD/B210을 열지 않고** 호출 순서(두 lane retune 후 단일 group submit·단일
hardware timestamp·exactly-once)만 강제·기록한다. software commit의 성공은 **실제 RF
atomicity 증명이 아니다**(실 RF atomicity·실 group TX는 Stage 6 실장비 hard gate).
"""

from __future__ import annotations

from ..shared_medium.radiation import GroupAdmission


class Stage7GroupTxRadioSink:
    """production `RadioBurstSink`. Stage 7 group TX 경계에 결속한다(§6.1, WSM-REQ-01/03).

    `group_tx`는 다음 두 method를 제공해야 한다(fail-closed; 없으면 생성 실패):

    - ``retune_tx_lo(*, lane, channel, hardware_timestamp_ns)`` — 한 lane을 staging/retune 예약.
    - ``submit_tx_group(*, group_id, lanes, hardware_timestamp_ns)`` — 두 lane을 같은 hardware
      timestamp로 한 번에 방사(all-or-nothing). ``lanes``는 ``(tx_lane, child_id, waveform_sha256)``.

    per-child 순차 방사 경로가 없으므로 "한 child만 방사"가 구조적으로 불가능하다. prepare 실패는
    RadioBurstSink 계약에 따라 group 전체를 거부한다(상위 radiation이 BroadcastNotAdmitted로 처리).
    """

    def __init__(self, *, group_tx) -> None:
        for method in ("retune_tx_lo", "submit_tx_group"):
            if not callable(getattr(group_tx, method, None)):
                raise ValueError(
                    f"group_tx는 callable {method}를 제공해야 합니다(fail-closed device 경계). "
                    "실장비에서는 Stage 7 b210_worker, RF-off에서는 SoftwareGroupTx를 주입하십시오."
                )
        self._group_tx = group_tx

    def prepare_group(self, *, group_id: str, children) -> GroupAdmission:
        children = tuple(children)
        if len(children) != 2:
            raise RuntimeError("group에는 정확히 두 fan-out child가 있어야 합니다")
        timestamps = {c.scheduled_hw_timestamp_ns for c in children}
        if len(timestamps) != 1:
            raise RuntimeError("fan-out child는 같은 hardware timestamp를 공유해야 합니다")
        lanes = {c.tx_lane for c in children}
        if len(lanes) != 2:
            raise RuntimeError("fan-out child는 서로 다른 두 TX lane이어야 합니다")
        # 두 lane을 함께 staging/retune 예약한다(아직 방사하지 않는다).
        for child in children:
            self._group_tx.retune_tx_lo(
                lane=child.tx_lane,
                channel=child.channel.value,
                hardware_timestamp_ns=child.scheduled_hw_timestamp_ns,
            )
        return GroupAdmission(group_id=group_id, children=children)

    def commit_group(self, admission: GroupAdmission) -> None:
        children = admission.children
        timestamp = next(iter({c.scheduled_hw_timestamp_ns for c in children}))
        # 두 lane을 하나의 group TX 호출로 같은 hardware timestamp에 방사한다(atomic).
        self._group_tx.submit_tx_group(
            group_id=admission.group_id,
            lanes=tuple((c.tx_lane, c.child_id, c.waveform_sha256) for c in children),
            hardware_timestamp_ns=timestamp,
        )


class SoftwareGroupTx:
    """RF-off software group-TX harness. Stage 7 group TX 경계를 **모사**하되 실 UHD/B210을 열지
    않는다. 순서·단일 timestamp·exactly-once를 강제하고 호출을 기록한다(§C: software 순서 검증).

    강제하는 불변:

    - `submit_tx_group`은 두 lane이 모두 `retune_tx_lo`로 staging된 뒤에만 허용된다(TX 순서).
    - group의 모든 lane은 같은 hardware timestamp를 공유한다.
    - 같은 group_id를 두 번 submit하지 않는다(exactly-once).

    `fail_on_*`로 각 단계 실패를 주입해 상위 fail-closed 계약(prepare 실패=방사 0, commit 실패=
    OUTCOME_UNKNOWN)을 시험한다. 이 harness의 submit 성공은 실제 RF atomicity가 아니다.
    """

    def __init__(self, *, fail_on_retune: bool = False, fail_on_submit: bool = False) -> None:
        self.retunes: list = []          # (lane, channel, hardware_timestamp_ns)
        self.submits: list = []          # (group_id, lanes, hardware_timestamp_ns)
        self._staged: dict = {}          # hardware_timestamp_ns -> set(lane)
        self._submitted_groups: set = set()
        self._fail_retune = bool(fail_on_retune)
        self._fail_submit = bool(fail_on_submit)

    def retune_tx_lo(self, *, lane: int, channel: str, hardware_timestamp_ns: int) -> None:
        if self._fail_retune:
            raise RuntimeError("injected retune_tx_lo failure (staging 거부)")
        self.retunes.append((lane, channel, hardware_timestamp_ns))
        self._staged.setdefault(hardware_timestamp_ns, set()).add(lane)

    def submit_tx_group(self, *, group_id: str, lanes, hardware_timestamp_ns: int) -> None:
        wanted = {lane for (lane, _cid, _wf) in lanes}
        staged = self._staged.get(hardware_timestamp_ns, set())
        if not wanted.issubset(staged):
            raise RuntimeError(
                "submit_tx_group before both lanes retuned (TX 순서 위반: RX/staging 준비 전 방사)"
            )
        if group_id in self._submitted_groups:
            raise RuntimeError(f"group {group_id} already submitted (exactly-once 위반)")
        if self._fail_submit:
            # commit 진입 후 실패 → 상위 radiation이 OUTCOME_UNKNOWN으로 처리(부분 방사 없음).
            raise RuntimeError("injected submit_tx_group failure (commit 불명확)")
        self._submitted_groups.add(group_id)
        self.submits.append((group_id, tuple(lanes), hardware_timestamp_ns))
        self._staged.pop(hardware_timestamp_ns, None)

    # --- 검증 helper --------------------------------------------------------
    @property
    def committed_group_ids(self) -> tuple:
        return tuple(gid for (gid, _lanes, _ts) in self.submits)

    @property
    def radiated_child_ids(self) -> tuple:
        out = []
        for (_gid, lanes, _ts) in self.submits:
            out.extend(cid for (_lane, cid, _wf) in lanes)
        return tuple(out)


__all__ = ("Stage7GroupTxRadioSink", "SoftwareGroupTx")
