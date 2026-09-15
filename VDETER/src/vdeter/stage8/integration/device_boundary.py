"""RF 장비 경계 adapter (production 연결 + RF-off software double).

Stage 5 provider의 `RadioBurstSink` seam(§6.1: prepare_group -> commit_group의 atomic 2단계)을
**실제 Stage 7 b210_worker의 group TX 계약**에 맞춘다. 실 worker interface(=`Stage7IsolatedDualLaneWorker`
/ `Stage7FakeMediumWorker`)는 다음이다:

    retune_tx_lo(target_hz, command_time_ns)     # 한 worker의 TX LO를 command time에 재조정
    submit_tx_group(bursts)                      # bursts = [(lane, data, hardware_time_ns), ...]
                                                 #   한 번의 atomic group TX 제출(같은 hardware time)

`Stage7GroupTxRadioSink`는 이 실 worker 계약에 **연결 가능한** production adapter다:

    prepare_group = 선택 채널로 TX LO retune(두 lane 공유 TX LO)  [staging]
    commit_group  = 두 child burst를 같은 hardware timestamp로 submit_tx_group 단일 호출 [atomic]

RF-off에서는 실 UHD/B210 대신 `SoftwareGroupTx`(실 worker와 **동일 signature**의 software double)를
주입한다. software commit의 성공은 실제 RF atomicity가 아니라 호출·좌표·소유권·순서 계약의 software
검증일 뿐이다(실 RF atomicity·실 group TX·실 frozen PHY 샘플 포맷은 Stage 6 실장비 hard gate).
"""

from __future__ import annotations

from ..shared_medium import constants
from ..shared_medium.iq import to_cf32le
from ..shared_medium.radiation import GroupAdmission

#: 기존 Stage 7 radio_adapter가 강제하는 timed retune 최소 lead(`_RETUNE_LEAD_NS`, 200 ms).
#: 값을 여기서 새로 정하지 않고 권위 구현과 같은 값을 인용한다(완화 금지).
RETUNE_LEAD_NS = 200_000_000


def validate_fanout_children(children, *, require_distinct_lanes: bool = False) -> tuple:
    """fan-out child 계약 검증 — 모든 radio sink(worker 직접·권위 `schedule_tx`·launcher RPC)가 같은 검사를 쓴다.

    정확히 두 child, 같은 hardware timestamp, 같은 선택 채널(공유 TX LO). `require_distinct_lanes`면
    두 child의 TX lane이 서로 달라야 한다 — worker 직접 제출 경계(`Stage7GroupTxRadioSink`)는 lane별
    burst를 한 group으로 싣기 때문이다. 권위 `schedule_tx` 경계(launcher·RF-off sink)는 한 요청으로 두
    lane에 방사하므로 lane 구분을 요구하지 않는다. 위반은 RuntimeError이고 상위 radiation이 방사 전
    거부(`BroadcastNotAdmitted`, TX 0)로 처리한다.
    """
    children = tuple(children)
    if len(children) != 2:
        raise RuntimeError("group에는 정확히 두 fan-out child가 있어야 합니다")
    if len({c.scheduled_hw_timestamp_ns for c in children}) != 1:
        raise RuntimeError("fan-out child는 같은 hardware timestamp를 공유해야 합니다")
    if require_distinct_lanes and len({c.tx_lane for c in children}) != 2:
        raise RuntimeError("fan-out child는 서로 다른 두 TX lane이어야 합니다")
    if len({c.channel for c in children}) != 1:
        raise RuntimeError("fan-out child는 같은 선택 채널을 공유해야 합니다(공유 TX LO)")
    return children


def logical_destination_station_id(children, station_id_by_endpoint) -> int:
    """장치 경계에 실을 목적지 station id = parent의 **논리** 목적지(`addressed_destination`).

    child의 `rx_station`은 그 child가 도달하는 **물리** 수신자라서 둘은 다르다 — MASS-B fan-out의 첫
    child는 C4=ROC이므로 논리 target이 MASS-A(upper)여도 물리 수신자를 실으면 권위 route가 canonical
    표에서 다른 기대 채널(lower)을 골라 LANE_ROUTE_INVALID로 거부한다(실물 C 재검증 결함). 목적지를
    모르면 물리 수신자로 대체하지 않고 거부한다(fail-closed). 양 경로(launcher·RF-off)가 같은 규칙을 쓴다.
    """
    destination = getattr(children[0], "addressed_destination", None)
    target = dict(station_id_by_endpoint).get(destination)
    if target is None:
        raise RuntimeError(
            f"논리 목적지 station id를 알 수 없습니다: {destination!r} "
            "(물리 수신자 rx_station으로 대체하지 않는다)")
    return int(target)


class Stage7GroupTxRadioSink:
    """production `RadioBurstSink`. **실 Stage 7 worker의 group TX 계약**에 결속한다(§6.1, WSM-REQ-01/03).

    `group_tx`는 실 worker와 같은 두 method를 제공해야 한다(fail-closed):

    - ``retune_tx_lo(target_hz, command_time_ns)`` — TX LO를 선택 채널로 command time에 재조정.
    - ``submit_tx_group(bursts)`` — ``bursts=[(lane, data, hardware_time_ns), ...]``를 한 번에
      atomic 방사. ``data``는 cf32le IQ bytes(실 worker 포맷).

    두 fan-out child는 같은 송신자(MASS-B)·같은 채널·같은 hardware timestamp를 공유하고 lane만
    다르다(B210 TX A/B 공유 LO). per-child 순차 방사 경로가 없으므로 "한 child만 방사"가 구조적으로
    불가능하다. staging 실패는 group 전체 거부(상위 radiation이 BroadcastNotAdmitted로 처리).
    """

    def __init__(self, *, group_tx) -> None:
        for method in ("retune_tx_lo", "submit_tx_group"):
            if not callable(getattr(group_tx, method, None)):
                raise ValueError(
                    f"group_tx는 실 Stage 7 worker 계약의 callable {method}를 제공해야 합니다 "
                    "(fail-closed). 실장비는 Stage7 b210_worker, RF-off는 SoftwareGroupTx를 주입."
                )
        self._group_tx = group_tx

    def prepare_group(self, *, group_id: str, children) -> GroupAdmission:
        children = validate_fanout_children(children, require_distinct_lanes=True)
        channel = children[0].channel
        tx_time_ns = children[0].scheduled_hw_timestamp_ns
        target_hz = constants.CHANNEL_EFFECTIVE_CENTER_HZ[channel]
        # 두 lane은 TX LO를 공유한다(B210 TX A/B). timed retune은 TX 시각과 같은 시각이 아니라
        # **최소 retune lead만큼 앞선 시각**에 발행한다(기존 radio_adapter와 같은 규약:
        # `worker.retune_tx_lo(desired, hardware_start - _RETUNE_LEAD_NS)`). TX 시각에 retune을
        # 걸면 LO 정착 전에 방사하게 된다(P03). lead 값은 완화하지 않고 권위 상수를 인용한다.
        # 실 시간축(실 MAC scheduler 결속)에서는 TX 시각이 now+200 ms 이후이므로 lead가 그대로 적용된다.
        # RF-off stub scheduler는 hardware timestamp를 sample 좌표와 같은 단위로 모델링해 TX 시각이
        # 수천 ns 규모다(실시간 아님). 그 좌표에서만 0으로 clamp하며, 이때도 retune은 TX보다 **앞선다**.
        # 실 경로의 lead를 줄이지 않는다(clamp는 tx_time < lead인 비실시간 좌표에서만 일어난다).
        command_time_ns = max(0, tx_time_ns - RETUNE_LEAD_NS)
        self._group_tx.retune_tx_lo(target_hz, command_time_ns)
        return GroupAdmission(group_id=group_id, children=children)

    def commit_group(self, admission: GroupAdmission) -> None:
        children = admission.children
        # 두 child burst를 같은 hardware timestamp로 한 group TX 호출에 실어 atomic 방사한다.
        bursts = tuple(
            (child.tx_lane, to_cf32le(child.samples), child.scheduled_hw_timestamp_ns)
            for child in children
        )
        self._group_tx.submit_tx_group(bursts)


class SoftwareGroupTx:
    """RF-off software group-TX double. **실 Stage 7 worker와 동일 signature**를 모사하되 실 UHD/B210을
    열지 않는다. 순서·단일 timestamp·exactly-once를 강제하고 호출을 기록한다(§C: 호출·좌표·소유권 검증).

    강제 불변:

    - `submit_tx_group`은 해당 hardware time에 `retune_tx_lo`(staging)가 선행돼야 한다(TX 순서).
    - 한 group의 모든 burst는 같은 hardware timestamp를 공유한다.
    - 같은 (lane 집합, hardware time)을 두 번 submit하지 않는다(exactly-once).

    `fail_on_retune`/`fail_on_submit`로 staging/commit 실패를 주입해 상위 fail-closed 계약을 시험한다.
    이 double의 submit 성공은 실제 RF atomicity가 아니다.
    """

    def __init__(self, *, fail_on_retune: bool = False, fail_on_submit: bool = False) -> None:
        self.retunes: list = []          # (target_hz, command_time_ns)
        self.submits: list = []          # tuple of (lane, data, hardware_time_ns)
        self._staged_times: set = set()  # retune된 command_time_ns
        self._submitted: set = set()     # (frozenset(lanes), hardware_time_ns)
        self._fail_retune = bool(fail_on_retune)
        self._fail_submit = bool(fail_on_submit)

    def retune_tx_lo(self, target_hz, command_time_ns) -> None:
        if self._fail_retune:
            raise RuntimeError("injected retune_tx_lo failure (staging 거부)")
        self.retunes.append((target_hz, int(command_time_ns)))
        self._staged_times.add(int(command_time_ns))

    def submit_tx_group(self, bursts) -> None:
        bursts = tuple(bursts)
        times = {int(hw) for (_lane, _data, hw) in bursts}
        if len(times) != 1:
            raise RuntimeError("group burst는 같은 hardware timestamp를 공유해야 합니다")
        hw = next(iter(times))
        # staging(retune)은 방사 시각보다 **앞서야** 한다. 기존 검사는 retune 시각이 TX 시각과 정확히
        # 같기를 요구했으나, 권위 규약은 retune을 최소 lead만큼 앞선 시각에 발행하는 것이다(P03).
        if not any(staged <= hw for staged in self._staged_times):
            raise RuntimeError(
                "submit_tx_group before retune_tx_lo (TX 순서 위반: staging 전 방사)")
        key = (frozenset(int(lane) for (lane, _d, _h) in bursts), hw)
        if key in self._submitted:
            raise RuntimeError("group already submitted (exactly-once 위반)")
        if self._fail_submit:
            # commit 진입 후 실패 → 상위 radiation이 OUTCOME_UNKNOWN 처리(부분 방사 없음).
            raise RuntimeError("injected submit_tx_group failure (commit 불명확)")
        self._submitted.add(key)
        self.submits.append(bursts)

    # --- 검증 helper (worker에는 group_id가 없으므로 count/lane 기준) ---------
    @property
    def committed_groups(self) -> int:
        return len(self.submits)

    @property
    def radiated_children(self) -> int:
        return sum(len(bursts) for bursts in self.submits)

    @property
    def radiated_lanes(self) -> tuple:
        out = []
        for bursts in self.submits:
            out.extend(int(lane) for (lane, _d, _h) in bursts)
        return tuple(out)


__all__ = ("Stage7GroupTxRadioSink", "SoftwareGroupTx", "validate_fanout_children",
           "logical_destination_station_id")
