"""Stage 8 PoC provider seams를 **실 Stage 7 객체**에 코드 수준에서 결속(RF-off 검증 가능).

`SharedMediumActionProvider.for_production(...)`가 요구하는 device 경계 seam(radio_sink·scheduler·
arbiter·rx_readiness·waveform_factory)을, 실행 중인 Stage 7 software group의 **실 endpoint 객체**에
바인딩한다. 하드웨어 경계(B210 device I/O·RF)만 software double(`Stage7FakeMediumWorker` + 실
conducted-path 매질)이고, 나머지(frozen PHY 변조·group-TX worker 계약·MAC hardware clock·route
sidecar·수신)는 실제다.

용도: RF-off 통합 검증(`build_stage7_software_group`)에서 MASS-B를 `POC_ACTOR`로 두고 실 MAC/PHY/
group-TX/RX·TX terminal 공급자에 연결. production silent RF-off fallback이 아니라 **명시** 조립이다.

물리 실측 대기(코드 결속 완료이나 하드웨어에서만 확인 가능):
- 실 RF group atomicity·frozen PHY device-rate 샘플 포맷(여기 waveform은 frozen Phy11 변조를 쓰되
  실 RF commit atomicity는 실장비).
- MASS-B 브로드캐스트가 **service-tuned 수신기**에 동시 도달(WSM-G1 common-LO): 매질 delivery는
  `tx_lo_hz==rx_lo_hz`를 요구하므로 common-LO 정합에서만 RF-off 도달. 실 동시성은 실장비.
- 실 launcher socket cross-process 경로(in-process 매질이 3-process launcher를 못 가로지름).
"""

from __future__ import annotations

from vdeter.phy.transmitter import Phy11Transmitter
from vdeter.stage8.integration.device_boundary import Stage7GroupTxRadioSink
from vdeter.stage8.shared_medium import constants
from vdeter.stage8.shared_medium.iq import IqBurst
from vdeter.stage8.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider
from vdeter.stage8.shared_medium.radiation import ChildTerminal, ReadinessDecision

_LINK11_PACKET_BYTES = 54  # frozen Link 11 packet 크기(실 PHY 입력).


class Link11WaveformFactory:
    """waveform_factory seam → 실 frozen Stage 2 PHY 변조(`Phy11Transmitter`).

    `encode(source_station, channel, payload) -> IqBurst`. payload를 54-byte Link 11 packet으로
    정규화(길이 초과 거부, 미만 zero-pad)해 실 PHY 변조 sample을 만든다. RF-off는 이 변조를 검증하고,
    실 RF device-rate 샘플 포맷·atomicity는 실장비.
    """

    def __init__(self) -> None:
        self._phy = Phy11Transmitter()

    def encode(self, *, source_station: str, channel, payload: bytes) -> IqBurst:
        data = bytes(payload or b"")
        if len(data) > _LINK11_PACKET_BYTES:
            raise ValueError(
                f"PoC payload {len(data)}B가 Link 11 packet 상한 {_LINK11_PACKET_BYTES}B를 넘습니다")
        packet = data.ljust(_LINK11_PACKET_BYTES, b"\x00")
        stages = self._phy.encode(packet)          # 실 frozen PHY 변조
        return IqBurst(channel=channel, start_sample=0, samples=tuple(stages.samples))


class Stage7HwTimeScheduler(NextSafeSlotScheduler):
    """scheduler seam → 실 worker **hardware clock**에 slot 기준을 결속.

    검증된 반응형 인과성 로직(NextSafeSlotScheduler)을 유지하되, slot grid의 현재 시각을 실
    `worker.hardware_time_ns()`에서 매 resolve마다 갱신한다(합성 counter 아님). 반응형 trigger는
    실 관측의 device hardware timestamp에서 온다(connection이 register). 실 MAC의 device clock에
    결속되므로 lead·과거 slot 판정이 실 시간축을 따른다.
    """

    def __init__(self, worker, *, lead_slots: int = 1) -> None:
        self._worker = worker
        super().__init__(current_slot=self._current_slot_from_hw(), lead_slots=lead_slots)

    def _current_slot_from_hw(self) -> int:
        hw = int(self._worker.hardware_time_ns())
        return max(0, hw // constants.SLOT_SAMPLES) if hasattr(constants, "SLOT_SAMPLES") else max(0, hw // 8192)

    def resolve(self, action, *, request_id: str):
        # 매 resolve마다 실 hardware clock으로 current slot을 전진(실 시간축).
        self._current = max(self._current, self._current_slot_from_hw())
        return super().resolve(action, request_id=request_id)


class Stage7WorkerArbiter:
    """arbiter seam → 실 worker TX lane 점유(정상 TX 우선).

    `reserve(resource_key, owner)`는 실 worker의 TX lane mute/점유 상태를 확인한다. 정상 서비스 TX가
    lane을 mute하고 있으면(정상 TX 우선) 예약 실패(False→RADIO_RESOURCE_CONFLICT). `release`는 예약
    해제. 실 "normal TX wins" 중재는 실장비에서 radio_adapter.schedule_tx lane-overlap이 담당하므로
    RF-off에서는 worker lane 상태로 근사 결속한다(실 동시 서비스 TX 충돌은 실장비).
    """

    def __init__(self, worker) -> None:
        self._worker = worker
        self._held: set = set()

    def reserve(self, *, resource_key: str, owner: str) -> bool:
        if resource_key in self._held:
            return False
        muted = getattr(self._worker, "tx_muted", None)
        values = muted.values() if hasattr(muted, "values") else (muted or ())
        if any(bool(v) for v in values):  # 정상 TX가 lane을 점유(mute) 중
            return False
        self._held.add(resource_key)
        return True

    def release(self, *, resource_key: str, owner: str) -> None:
        self._held.discard(resource_key)


class Stage7ReceiverReadiness:
    """rx_readiness seam → 실 수신기 RX 준비 상태.

    `window_ready(request) -> ReadinessDecision`. 대상 endpoint의 실 worker가 running이고 RX LO가
    송신 채널에 정합(WSM common-LO)이면 ready. 정합하지 않으면 not-ready(RF 전 fail-closed) — 실
    동시 service-tuned 수신은 WSM-G1 common-LO 실장비.
    """

    def __init__(self, workers_by_station: dict, channel_center_hz) -> None:
        self._workers = dict(workers_by_station)
        self._center = channel_center_hz

    def window_ready(self, request) -> ReadinessDecision:
        worker = self._workers.get(request.endpoint)
        if worker is None:
            return ReadinessDecision(ready=False, reason="RX_ENDPOINT_UNKNOWN")
        if getattr(worker, "rx_lo_hz", None) != self._center.get(request.channel):
            return ReadinessDecision(ready=False, reason="RX_LO_NOT_COMMON")  # common-LO 미정합
        return ReadinessDecision(ready=True, reason="READY",
                                 proof={"endpoint": request.endpoint, "rx_lo_hz": worker.rx_lo_hz})


def stage7_terminal_source(medium, *, tx_serial: str):
    """terminal_source → 실 conducted-path 매질 delivery로 child terminal 확정.

    `source(action, result, parent) -> {child_id: ChildTerminal}`. 각 child의 cable(C4/C6)이 실
    `medium.delivery_log`에 tx_serial로 기록됐으면 COMPLETED, 아니면 UNKNOWN(성공 합성 금지). 실
    매질 도달(tune 정합)에서만 COMPLETED가 된다. 실 RF atomicity는 실장비.
    """

    def source(action, result, parent):
        delivered = {d["path_id"] for d in medium.delivery_log if d.get("tx_serial") == tx_serial}
        out = {}
        for child in parent.children:
            cable = getattr(child, "cable", None)
            out[child.child_id] = (ChildTerminal.COMPLETED.value if cable in delivered
                                   else ChildTerminal.UNKNOWN.value)
        return out

    return source


def stage7_target_response(medium, *, tx_serial: str, station_to_endpoint: dict):
    """target_response → 실 수신기 도달 증거로 target effect 판정.

    `response(action, result, parent) -> (TargetEffect, reason)`. 논리 target endpoint로 가는 cable이
    실 매질에 도달했으면 EFFECT_OBSERVED(실 수신 evidence), 아니면 INDETERMINATE(합성 금지). 실 RX
    복호 품질·application delivery는 실장비.
    """
    from vdeter.stage8.execution.verdict import TargetEffect

    def response(action, result, parent):
        target_ep = station_to_endpoint.get(getattr(parent, "addressed_destination", None))
        delivered = {d["path_id"] for d in medium.delivery_log if d.get("tx_serial") == tx_serial}
        addressed_cable = None
        for child in parent.children:
            if getattr(child, "rx_station", None) == target_ep:
                addressed_cable = getattr(child, "cable", None)
        if addressed_cable and addressed_cable in delivered:
            return (TargetEffect.EFFECT_OBSERVED, "real medium delivery to addressed receiver")
        return (TargetEffect.INDETERMINATE, "no real receive evidence for addressed target")

    return response


def assemble_stage7_provider(members, *, channel_center_hz=None, dual_channel_installed: bool = False):
    """실행 중 software group의 **실 MASS-B endpoint 객체**로 production provider를 조립한다(no stub).

    반환 ``(provider, bindings)``. bindings는 실 worker·medium 참조(terminal/target source 조립용).
    하드웨어 경계(worker device I/O)만 fake medium이고 seam은 모두 실 Stage 7 객체다.
    """
    center = channel_center_hz or constants.CHANNEL_EFFECTIVE_CENTER_HZ
    mass_b = members["MASS-B"].endpoint.dependencies.radio.lease.worker
    workers = {ep: members[ep].endpoint.dependencies.radio.lease.worker for ep in members}
    provider = SharedMediumActionProvider.for_production(
        radio_sink=Stage7GroupTxRadioSink(group_tx=mass_b),      # 실 group-TX worker
        waveform_factory=Link11WaveformFactory(),                # 실 frozen PHY
        scheduler=Stage7HwTimeScheduler(mass_b),                 # 실 hardware clock slot
        arbiter=Stage7WorkerArbiter(mass_b),                     # 실 worker lane 점유
        rx_readiness=Stage7ReceiverReadiness(workers, center),   # 실 수신기 RX 준비
        dual_channel_installed=dual_channel_installed,
    )
    return provider, {"mass_b_worker": mass_b, "workers": workers}


__all__ = (
    "Link11WaveformFactory", "Stage7HwTimeScheduler", "Stage7WorkerArbiter",
    "Stage7ReceiverReadiness", "stage7_terminal_source", "stage7_target_response",
    "assemble_stage7_provider",
)
