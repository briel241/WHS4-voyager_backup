"""PoC provider seam을 **launcher 소유 장치**에 프로세스 경계 너머로 결속한다(P01/P02).

`stage8_stage7_seams`는 같은 프로세스 안의 Stage 7 객체를 직접 잡는다(RF-off 검증 경로). 일반 운용
경로에서는 장치를 launcher가 단독 소유하므로, supervisor(=workload를 소유한 scenario controller
프로세스)는 §16.2 TEST adapter op으로만 장치에 닿는다.

핵심 설계: **검증된 seam 로직을 다시 쓰지 않는다.** `_RemoteWorker`가 실 worker와 같은 속성 표면을
`poc_radio_state` 응답으로 채워 주므로, 이미 시험된 `Stage7HwTimeScheduler`(P03)·
`Stage7WorkerArbiter`(P04)·`Stage7ReceiverReadiness`(P06)를 **그대로** 쓴다. 새로 만드는 것은 프로세스
경계를 실제로 건너가는 두 가지 — 송신 sink와 TX 원장 terminal source — 뿐이다.

송신은 launcher 쪽에서 권위 `radio_adapter.schedule_tx` 하나로만 나간다(여기서 우회 경로를 만들지
않는다). 조회는 장치·원장이 이미 가진 것만 돌려받고, 모르면 `None`으로 보존한다.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid

from vdeter.stage7.testing.controller import PocDeviceCallError, Stage7ScenarioController

from apps.stage8_stage7_seams import (
    Link11WaveformFactory,
    Stage7HwTimeScheduler,
    Stage7ReceiverReadiness,
    Stage7WorkerArbiter,
)

#: 장치 상태 조회 캐시 수명(초). 한 번의 scheduler/arbiter/readiness 판정 안에서 같은 상태를 쓰되,
#: 판정 사이에는 반드시 다시 읽는다(오래된 상태로 송신 결정을 내리지 않는다).
_STATE_TTL_S = 0.05


class LauncherDeviceClient:
    """한 endpoint의 launcher PoC device op 창구. 판정하지 않고 사실만 나른다."""

    def __init__(self, controller: Stage7ScenarioController, endpoint_id: str) -> None:
        self._controller = controller
        self.endpoint_id = endpoint_id
        self._state = None
        self._state_at = 0.0
        self.collision_session_id = None

    def call(self, command_name: str, body: dict):
        if command_name == "poc_schedule_group_tx" and self.collision_session_id is not None:
            body = dict(body, collision_session_id=self.collision_session_id)
        response = self._controller.poc_command(self.endpoint_id, command_name, body)
        return Stage7ScenarioController.poc_result(response)

    def radio_state(self, *, fresh: bool = False) -> dict:
        now = time.monotonic()
        if fresh or self._state is None or (now - self._state_at) > _STATE_TTL_S:
            self._state = self.call("poc_radio_state", {})
            self._state_at = now
        return self._state

    def invalidate(self) -> None:
        self._state = None


class _RemoteWorker:
    """실 worker와 **같은 속성 표면**을 launcher 조회 응답으로 채우는 얇은 shim.

    이 shim 덕분에 in-process에서 시험된 scheduler·arbiter·readiness 구현을 한 줄도 고치지 않고
    프로세스 경계 너머에 쓴다. 값을 만들어내지 않는다 — 응답에 없으면 `None`이고, 그 `None`을
    소비하는 쪽이 보수적으로(fail-closed) 판단한다.
    """

    def __init__(self, client: LauncherDeviceClient) -> None:
        self._client = client

    def _state(self):
        return self._client.radio_state()

    def map_from_sender(self, sender, hardware_ns):
        utc = sender._client.call("poc_clock_map", {"direction": "HW_TO_UTC", "timestamp_ns": hardware_ns})
        mapped = self._client.call("poc_clock_map", {"direction": "UTC_TO_HW", "timestamp_ns": utc["timestamp_ns"]})
        return mapped["timestamp_ns"], {"sender_bridge": utc, "receiver_bridge": mapped}

    # scheduler(P03)
    def hardware_time_ns(self):
        value = self._state().get("hardware_time_ns")
        if value is None:
            raise PocDeviceCallError(
                f"{self._client.endpoint_id}: hardware time을 읽을 수 없습니다")
        return int(value)

    @property
    def tx_lo_hz(self):
        return self._state().get("tx_lo_hz")

    # arbiter(P04) — 권위 속성명 그대로 노출한다.
    @property
    def _tx_muted(self):
        return self._state().get("tx_muted")

    # readiness(P06)
    @property
    def rx_lo_hz(self):
        return self._state().get("rx_lo_hz")

    @property
    def rx_rf_lo_hz(self):
        # manual common-LO를 적용한 설치에서만 lane별 중심으로 판정한다(적용 전에는 None).
        return self._state().get("rx_rf_lo_hz")

    @property
    def lane_rx_center_hz(self):
        raw = self._state().get("lane_rx_center_hz") or {}
        return {int(lane): int(value) for lane, value in raw.items()}

    @property
    def rx_windows(self):
        raw = self._state().get("rx_windows") or {}
        return {int(lane): list(entries) for lane, entries in raw.items()}

    @property
    def rx_continuous(self):
        return bool(self._state().get("rx_continuous"))

    def rx_continuous_since_ns(self):
        return self._state().get("rx_continuous_since_ns")


class LauncherRadioSink:
    """radio_sink seam → launcher의 권위 `schedule_tx`로 프로세스 경계를 건너 방사한다.

    `prepare_group`은 장치를 건드리지 않고 fan-out child 계약만 검증한다(같은 시각·같은 채널·2개).
    실제 예약(route 결속)과 송신은 `commit_group` 한 번의 RPC에서 launcher가 수행한다 — 그래야
    "예약만 되고 송신은 안 된" 중간 상태가 프로세스 경계에 걸쳐 남지 않는다.
    """

    def __init__(self, client: LauncherDeviceClient, *, channel_name_of,
                 station_id_by_endpoint: dict) -> None:
        self._client = client
        self._channel_name_of = channel_name_of
        self._station_id = dict(station_id_by_endpoint)
        #: group_id → burst_id(권위 원장 조회 키).
        self.burst_by_group: dict = {}
        self.receipt_by_group: dict = {}

    @staticmethod
    def _validate(children) -> tuple:
        children = tuple(children)
        if len(children) != 2:
            raise RuntimeError("group에는 정확히 두 fan-out child가 있어야 합니다")
        if len({c.scheduled_hw_timestamp_ns for c in children}) != 1:
            raise RuntimeError("fan-out child는 같은 hardware timestamp를 공유해야 합니다")
        if len({c.channel for c in children}) != 1:
            raise RuntimeError("fan-out child는 같은 선택 채널을 공유해야 합니다(공유 TX LO)")
        return children

    def prepare_group(self, *, group_id: str, children):
        from vdeter.stage8.shared_medium.radiation import GroupAdmission

        children = self._validate(children)
        self.burst_by_group[group_id] = str(uuid.uuid4())
        return GroupAdmission(group_id=group_id, children=children)

    def commit_group(self, admission) -> None:
        from vdeter.stage8.shared_medium.iq import to_cf32le
        from vdeter.stage8.shared_medium.radiation import BroadcastCommitRefused

        children = admission.children
        burst_id = self.burst_by_group.get(admission.group_id)
        if burst_id is None:
            raise RuntimeError("commit 전에 prepare_group이 필요합니다(예약 없음)")
        target = self._station_id.get(getattr(children[0], "rx_station", None))
        if target is None:
            raise RuntimeError(f"수신 station id를 알 수 없습니다: {children[0].rx_station}")
        samples = to_cf32le(children[0].samples)
        result = self._client.call("poc_schedule_group_tx", {
            "burst_id": burst_id,
            "channel_name": self._channel_name_of(children[0].channel),
            "transmit_at_hw_ns": int(children[0].scheduled_hw_timestamp_ns),
            "samples_base64": base64.b64encode(samples).decode("ascii"),
            "samples_sha256": hashlib.sha256(samples).hexdigest(),
            "destination_station_id": int(target),
            "request_digest": str(getattr(children[0], "payload_digest", "") or ""),
            "wire_packet_digest": children[0].wire_packet_digest,
        })
        accepted = result.get("accepted") if isinstance(result, dict) else None
        if accepted is True:
            self.receipt_by_group[admission.group_id] = {
                "transmit_at_utc_ns": result["transmit_at_utc_ns"],
                "wire_packet_digest": children[0].wire_packet_digest}
        self._client.invalidate()     # 송신 후 장치 상태는 달라졌다 — 캐시를 믿지 않는다
        if accepted is True:
            return
        if accepted is False:
            # 권위 장치가 **방사 전에** 스스로 거부를 회신했다(route bind 실패 또는 schedule_tx
            # 거부). 방사 여부를 모르는 실패가 아니므로 UNKNOWN으로 승격하지 않는다 — 상위가
            # 자원을 반환하고 PoC action만 거부한다(정상 TX·세션은 유지).
            raise BroadcastCommitRefused(
                f"권위 schedule_tx 거부: {result.get('reason')} {result.get('detail')}")
        # accepted 판정을 읽을 수 없는 응답은 방사 여부가 불명이다 -> UNKNOWN 유지(강등 금지).
        raise RuntimeError(
            f"권위 schedule_tx 응답에 accepted 판정이 없습니다: {str(result)[:160]}")


class LauncherLedgerTerminalSource:
    """terminal_source → launcher의 **권위 TX 원장**을 조회해 child terminal을 확정한다.

    상태를 만들지 않는다. 원장이 아직 확정(`terminal`)하지 않았으면 유계로 기다리되, 끝까지
    확정되지 않으면 `UNKNOWN`으로 **보존**한다(성공 추정 금지 → 상위가 SESSION_INHIBIT).
    """

    def __init__(self, client: LauncherDeviceClient, sink: LauncherRadioSink, *,
                 max_polls: int = 64, poll_interval_s: float = 0.1, guard=None) -> None:
        self._client = client
        self._sink = sink
        self._max_polls = int(max_polls)
        self._interval = float(poll_interval_s)
        #: P11: 폴링 중에도 권위 deadline/중단을 확인한다(블로킹 구간을 무한정 두지 않는다).
        self._guard = guard

    def __call__(self, action, result, parent) -> dict:
        from vdeter.stage8.shared_medium.radiation import ChildTerminal

        burst_id = self._sink.burst_by_group.get(parent.parent_id)
        lanes = {child.child_id: getattr(child, "tx_lane", None) for child in parent.children}
        if burst_id is None:
            return {cid: ChildTerminal.UNKNOWN.value for cid in lanes}
        snapshot = None
        for _ in range(self._max_polls):
            if self._guard is not None:
                self._guard()
            try:
                snapshot = self._client.call("poc_tx_burst_state", {"burst_id": burst_id})
            except PocDeviceCallError:
                snapshot = None      # 조회 실패를 성공으로 바꾸지 않는다
            if snapshot is not None and snapshot.get("terminal"):
                break
            time.sleep(self._interval)
        states = (snapshot or {}).get("children") or {}
        out = {}
        for child_id, lane in lanes.items():
            state = states.get(str(lane), states.get(lane))
            if state == "COMPLETED":
                out[child_id] = ChildTerminal.COMPLETED.value
            elif state == "FAILED":
                out[child_id] = ChildTerminal.FAILED.value
            else:
                out[child_id] = ChildTerminal.UNKNOWN.value   # 진행 중·미기록은 미확정 보존
        return out


class MacSlotScheduler(Stage7HwTimeScheduler):
    """scheduler seam → **실 MAC slot map**이 송신을 허용한 slot만 고른다(P03).

    기반 `Stage7HwTimeScheduler`가 실 hardware clock·권위 lead(100/200 ms)·반응형 인과성을
    이미 담당한다. 이 subclass는 그 위에 한 가지를 더한다: 기반이 고른 device-grid 시각 이후로
    **MAC이 실제로 송신을 허용한 첫 slot**으로 옮긴다.

    왜 필요한가: device slot 격자(8,192 표본 등간격)는 MAC의 TDMA 계획과 무관하다. 격자 시각을
    그대로 쓰면 MAC이 승인하지 않은 구간에 송신을 요청하게 된다(§7.3 "MAC이 승인하지 않은 동시
    TX를 이 기능이 새로 허용하지 않는다").

    slot을 만들어내지 않는다 — launcher가 실 slot map에서 읽어 준 후보 중에서만 고른다. 후보가
    없으면 `POC_DEADLINE_MISSED`로 거부한다(임의로 뒤로 미루거나 격자 시각으로 되돌아가지 않는다).
    """

    def __init__(self, worker, client: LauncherDeviceClient, *, channel_name: str | None = None,
                 current_channel=None, rx_readiness=None) -> None:
        super().__init__(worker, current_channel=current_channel)
        self._client = client
        self._channel_name = channel_name
        self._fixed_channel_name = channel_name
        self._rx_readiness = rx_readiness

    def _query(self) -> dict:
        """launcher에서 실 MAC 송신 가능 slot 후보를 읽는다(시험이 대체할 수 있는 유일한 지점)."""
        return self._client.call("poc_mac_slots", {
            "channel_name": self._channel_name,
            "from_hw_ns": int(getattr(self, "_base_hw_ns", 0)),
            "limit": 8,
        })

    def resolve(self, action, *, request_id: str):
        from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
        from vdeter.stage8.poc_api.types import TARGET_TO_PHYSICAL_CHANNEL
        from vdeter.stage8.shared_medium.radiation import ScheduledSlot

        target_channel = stage7_channel_name(TARGET_TO_PHYSICAL_CHANNEL[action.target])
        if self._fixed_channel_name is not None and self._fixed_channel_name != target_channel:
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                           "MAC slot query channel differs from logical target")
        # The canonical target determines the query on every action; there is
        # no device-grid fallback when the caller omits a channel override.
        self._channel_name = target_channel
        base = super().resolve(action, request_id=request_id)   # 실 clock·권위 lead·인과성
        self._base_hw_ns = int(base.scheduled_hw_timestamp_ns)
        answer = self._query()
        slots = (answer or {}).get("slots") or []
        # slot 조회 자체가 시간을 쓴다(RPC 왕복). 조회 전에 계산한 lead는 이미 일부 소진됐으므로
        # **다시 읽은 현재 시각** 기준으로 권위 최소 lead를 여전히 만족하는 첫 후보를 고른다.
        # lead를 줄여 통과시키지 않는다 — 만족하는 후보가 없으면 거부한다.
        now_ns = self._hw_now_ns()
        earliest = now_ns + self._required_lead_ns()
        usable = [slot for slot in slots
                  if int(slot["start_hw_ns"]) >= max(earliest, self._base_hw_ns)]
        if action.schedule.get("kind") == "after_slots":
            # An exact relative slot is not NEXT_SAFE_SLOT. If that slot is
            # unavailable (including heard occupancy), do not silently move
            # the reactive action to a later MAC slot.
            usable = [slot for slot in usable
                      if int(slot["start_hw_ns"]) == self._base_hw_ns]
        chosen = None
        for slot in usable:
            if self._rx_readiness is not None:
                from vdeter.stage8.shared_medium.radiation import ReadinessRequest
                state = self._client.radio_state()
                decision = self._rx_readiness.window_ready(ReadinessRequest(
                    endpoint=action.target, channel=TARGET_TO_PHYSICAL_CHANNEL[action.target],
                    slot_key="mac-{frame_ordinal}-{tdma_channel}-{slot_in_frame}".format(**slot),
                    scheduled_sample=int(slot["start_hw_ns"]) * 307200 // 1000000000,
                    scheduled_hw_timestamp_ns=int(slot["start_hw_ns"]),
                    clock_id=state.get("clock_id"), bridge_generation=state.get("bridge_generation"),
                    correction_revision=state.get("correction_revision")))
                if not decision.ready:
                    continue
            # Readiness RPCs consume lead too. Never weaken the authority's lead.
            if int(slot["start_hw_ns"]) >= self._hw_now_ns() + self._required_lead_ns():
                chosen = slot
                break
        if chosen is None:
            raise PocError(
                PocErrorCode.POC_DEADLINE_MISSED,
                "실 MAC 후보에 lead와 수신 창 준비를 만족하는 slot이 없습니다 — 거부",
                detail={"channel": self._channel_name,
                        "earliest_hw_ns": earliest,
                        "candidates": len(slots),
                        "slot_map_id": (answer or {}).get("slot_map_id")})
        return ScheduledSlot(
            scheduled_sample=(int(chosen["start_hw_ns"]) * 307_200) // 1_000_000_000,
            scheduled_hw_timestamp_ns=int(chosen["start_hw_ns"]),
            # slot_key를 MAC 좌표로 만든다 — 자원 예약 키가 실제 TDMA slot과 1:1이 된다.
            slot_key=("mac-{frame_ordinal}-{tdma_channel}-{slot_in_frame}"
                      .format(**chosen)),
        )


def stage7_channel_name(stage8_channel) -> str:
    """Stage 8 `PhysicalChannel` → 권위 Stage 7 canonical 채널 **이름**."""

    from vdeter.stage7.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME
    from vdeter.stage8.poc_api.types import PhysicalChannel as Stage8Channel

    mapping = {Stage8Channel.LOWER: LOWER_CHANNEL_NAME,
               Stage8Channel.UPPER: UPPER_CHANNEL_NAME}
    name = mapping.get(stage8_channel)
    if name is None:
        raise RuntimeError(f"알 수 없는 Physical Channel: {stage8_channel!r}")
    return name


def assemble_launcher_provider(controller, *, actor_endpoint: str,
                               endpoint_ids, station_id_by_endpoint: dict,
                               channel_center_hz=None, dual_channel_installed: bool = False,
                               guard=None, mac_channel_name=None):
    """launcher 소유 장치에 결속된 production provider를 조립한다.

    반환 ``(provider, bindings)``. bindings에 client·sink가 있어 terminal source를 만들 수 있다.
    seam 다섯 개 중 scheduler·arbiter·rx_readiness·waveform_factory는 in-process에서 이미 시험된
    구현을 그대로 쓰고, radio_sink만 프로세스 경계를 건너는 구현이다.
    """
    from vdeter.stage8.shared_medium import constants
    from vdeter.stage8.shared_medium.provider import SharedMediumActionProvider

    center = channel_center_hz or constants.CHANNEL_EFFECTIVE_CENTER_HZ
    clients = {endpoint: LauncherDeviceClient(controller, endpoint) for endpoint in endpoint_ids}
    actor = clients[actor_endpoint]
    workers = {endpoint: _RemoteWorker(client) for endpoint, client in clients.items()}
    sink = LauncherRadioSink(actor, channel_name_of=stage7_channel_name,
                             station_id_by_endpoint=station_id_by_endpoint)
    # Always use the real MAC query (including heard reservations). The default
    # production path must not silently fall back to the device sample grid.
    readiness = Stage7ReceiverReadiness(workers, center, source_worker=workers[actor_endpoint])
    scheduler = MacSlotScheduler(workers[actor_endpoint], actor,
                                 channel_name=mac_channel_name, rx_readiness=readiness)
    provider = SharedMediumActionProvider.for_production(
        radio_sink=sink,
        rx_readiness=readiness,
        scheduler=scheduler,
        waveform_factory=Link11WaveformFactory(),
        arbiter=Stage7WorkerArbiter(workers[actor_endpoint]),
        dual_channel_installed=dual_channel_installed,
    )
    terminal_source = LauncherLedgerTerminalSource(actor, sink, guard=guard)
    return provider, {"clients": clients, "actor": actor, "sink": sink,
                      "workers": workers, "terminal_source": terminal_source}


__all__ = (
    "LauncherDeviceClient",
    "LauncherRadioSink",
    "LauncherLedgerTerminalSource",
    "assemble_launcher_provider",
    "stage7_channel_name",
)
