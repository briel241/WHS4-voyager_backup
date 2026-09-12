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

from vdeter.phy import resampling
from vdeter.phy.burst.rrc import validate_cf32le_full_scale
from vdeter.phy.transmitter import Phy11Transmitter
from vdeter.stage8.integration.device_boundary import Stage7GroupTxRadioSink
from vdeter.stage8.shared_medium import constants
from vdeter.stage8.shared_medium.iq import IqBurst, to_cf32le
from vdeter.stage8.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider
from vdeter.stage8.shared_medium.radiation import ChildTerminal, ReadinessDecision, ScheduledSlot

_LINK11_PACKET_BYTES = 54  # frozen Link 11 packet 크기(실 PHY 입력).

#: P03 권위 시간 좌표: 한 device slot의 실제 길이(ns). 8192 samples / 307,200 Hz = 26.666… ms.
#: sample index(=slot*8192)와 hardware ns는 **다른 단위**이며 섞지 않는다. slot 경계 환산은 slot마다
#: 내림하면 오차가 누적되므로(200 ms lead가 30 ns 모자라는 식) 아래 정수 정밀 함수를 쓴다.
DEVICE_SLOT_NS = (resampling.DEVICE_SLOT_SAMPLES * 1_000_000_000) // resampling.DEVICE_SAMPLE_RATE_HZ

#: 1 slot의 정확한 길이를 유리수로 다루기 위한 분자/분모(누적 내림 오차 제거).
_SLOT_NS_NUM = resampling.DEVICE_SLOT_SAMPLES * 1_000_000_000
_SLOT_NS_DEN = resampling.DEVICE_SAMPLE_RATE_HZ


def slot_to_hw_ns(slot_index: int) -> int:
    """slot index → hardware ns. slot 경계는 26,666,666.67 ns라 정수로 떨어지지 않으므로 **올림**한다.

    내림하면 경계 직전 값이 되어 그 시각이 한 slot 앞으로 해석된다(예약이 의도한 slot보다 이름).
    올림은 예약 시각이 slot 경계 이상임을 보장하고 `hw_ns_to_slot`와 왕복이 성립한다.
    """
    return -(-int(slot_index) * _SLOT_NS_NUM // _SLOT_NS_DEN)


def hw_ns_to_slot(hw_timestamp_ns: int) -> int:
    """hardware ns → slot index(내림, 정확)."""
    return max(0, int(hw_timestamp_ns) * _SLOT_NS_DEN // _SLOT_NS_NUM)


def first_slot_at_or_after(hw_timestamp_ns: int) -> int:
    """주어진 hardware ns 이상이 되는 첫 slot index(올림, 정확)."""
    ns = max(0, int(hw_timestamp_ns))
    return -(-ns * _SLOT_NS_DEN // _SLOT_NS_NUM)

#: P03 권위 lead: 기존 radio_adapter가 강제하는 최소 TX lead와 채널 전환(retune) lead.
#: 값을 완화하지 않고 그대로 인용한다(vdeter/stage7/radio_adapter.py `_LEAD_NS`/`_RETUNE_LEAD_NS`).
_TX_LEAD_NS = 100_000_000
_RETUNE_LEAD_NS = 200_000_000


class Link11WaveformFactory:
    """waveform_factory seam → 실 frozen Stage 2 PHY 변조 + **권위 native→device 변환**(P05).

    `encode(source_station, channel, payload) -> IqBurst`. payload를 54-byte Link 11 packet으로
    정규화(길이 초과 거부, 미만 zero-pad)해 실 `Phy11Transmitter`로 native 변조한 뒤, 기존 정상 경로
    (`runtime/phy_bridge.to_device_burst`)와 **같은 권위 변환**을 적용한다:

    1. native rate/count 불변식 검사(76,800 Hz · 2,048 samples) — frozen PHY 출력 검증.
    2. `resampling.interpolate_slot` 4배 보간 → device 307,200 Hz · 8,192 samples(26.666… ms).
    3. `validate_cf32le_full_scale`로 device full-scale 초과 거부.

    이전에는 native 2,048 samples를 그대로 group sink에 넘겨 device rate에서 6.666… ms짜리 잘린
    burst가 됐다(P05). 보간기·frozen 변조기·decoder 기준은 바꾸지 않고 기존 검증 경로를 재사용한다.
    """

    def __init__(self) -> None:
        self._phy = Phy11Transmitter()

    def encode(self, *, source_station: str, channel, payload: bytes) -> IqBurst:
        data = bytes(payload or b"")
        if len(data) > _LINK11_PACKET_BYTES:
            raise ValueError(
                f"PoC payload {len(data)}B가 Link 11 packet 상한 {_LINK11_PACKET_BYTES}B를 넘습니다")
        packet = data.ljust(_LINK11_PACKET_BYTES, b"\x00")
        stages = self._phy.encode(packet)          # 실 frozen PHY native 변조
        native = tuple(stages.samples)
        # native 불변식(frozen PHY 계약) — 기존 phy_bridge와 동일 검사.
        if len(native) != resampling.NATIVE_SLOT_SAMPLES:
            raise ValueError(
                f"Link 11 native burst count mismatch: {len(native)} != "
                f"{resampling.NATIVE_SLOT_SAMPLES}")
        device = resampling.interpolate_slot(native)   # 권위 4배 보간 → device rate
        if len(device) != resampling.DEVICE_SLOT_SAMPLES:
            raise ValueError(
                f"device burst count mismatch: {len(device)} != {resampling.DEVICE_SLOT_SAMPLES}")
        validate_cf32le_full_scale(to_cf32le(device))  # 권위 full-scale 검증
        return IqBurst(channel=channel, start_sample=0, samples=device)


class Stage7HwTimeScheduler(NextSafeSlotScheduler):
    """scheduler seam → 실 device slot grid·실 hardware clock·권위 lead에 결속(P03).

    기반 `NextSafeSlotScheduler`는 스스로 "RF-off stub — hardware timestamp를 sample 좌표와 같은
    단위로 모델링"이라고 문서화한다. 그 결과 slot·sample·ns가 모두 `slot * 8192`로 뭉개져, hardware
    now 1,000,000,000 ns에서 NEXT_SAFE_SLOT lead가 **5,632 ns**로 나왔다(실 device slot 26,666,666 ns).

    이 subclass는 검증된 반응형 인과성 로직(trigger 기준·과거 slot 미이동·닫힌 slot 거부)은 그대로
    쓰고, **시간 좌표만** 권위 값으로 교체한다:

    - slot 경계 = `DEVICE_SLOT_NS`(8,192 / 307,200 Hz = 26,666,666 ns). sample index는 `slot * 8,192`로
      **분리** 유지한다(두 단위를 같은 값으로 쓰지 않는다).
    - 현재 slot은 매 resolve마다 실 `worker.hardware_time_ns()`에서 읽는다(합성 counter 아님).
    - 최소 lead는 기존 radio_adapter의 `_LEAD_NS`(100 ms), 채널 전환이 필요하면 `_RETUNE_LEAD_NS`
      (200 ms)를 slot 수로 올림 적용한다. lead를 늘려서 통과시키지 않으며, 부족하면 기반 클래스가
      `POC_DEADLINE_MISSED`로 거부한다(slot 무단 이동 없음).

    반응형 trigger의 hardware timestamp도 같은 실 시간축이므로(`emit_observation`이 실 관측 hw-time을
    등록) after_ms 인과성이 실제 시각 기준으로 판정된다.
    """

    def __init__(self, worker, *, current_channel=None) -> None:
        self._worker = worker
        self._current_channel = current_channel
        # 기반 클래스의 내부 "sample" 좌표를 **hardware ns**로 통일한다: slot 길이=DEVICE_SLOT_NS,
        # 1 ms=1,000,000 ns. 이렇게 해야 after_ms 인과성(trigger_ns + N ms)과 slot 경계 비교가 모두
        # 실제 시간축에서 이뤄진다(stub의 sample/ns 혼용 제거). 외부로 돌려주는 ScheduledSlot에서는
        # sample index와 hardware ns를 다시 분리한다(resolve 참조).
        super().__init__(current_slot=self._current_slot_from_hw(), lead_slots=self._lead_slots(),
                         samples_per_slot=DEVICE_SLOT_NS, samples_per_ms=1_000_000)

    # --- 실 시간 좌표 -------------------------------------------------------
    def _hw_now_ns(self) -> int:
        return int(self._worker.hardware_time_ns())

    def _current_slot_from_hw(self) -> int:
        """실 hardware ns → device slot index. sample index와 혼용하지 않는다."""
        return hw_ns_to_slot(self._hw_now_ns())

    def _required_lead_ns(self) -> int:
        """권위 최소 lead. 채널 전환(retune)이 필요한 경우 200 ms, 아니면 100 ms."""
        return _RETUNE_LEAD_NS if self._retune_needed() else _TX_LEAD_NS

    def _retune_needed(self) -> bool:
        current = getattr(self._worker, "tx_lo_hz", None)
        if current is None or self._current_channel is None:
            return True  # TX LO 미확정이면 보수적으로 retune lead를 요구한다(완화 금지)
        return current != constants.CHANNEL_EFFECTIVE_CENTER_HZ.get(self._current_channel)

    def _lead_slots(self) -> int:
        """지금부터 **최소 lead 이후**가 되는 첫 slot까지의 slot 수(정확).

        `ceil(required/slot)`을 내림된 slot 길이로 계산하면 실제 lead가 몇 ns 모자랄 수 있으므로,
        목표 시각(now+required)을 정확히 만족하는 첫 slot을 구해 그 차이를 lead slot 수로 쓴다.
        lead를 늘려 통과시키지 않고, 권위 최소값을 **만족하는 최소 slot**을 고른다.
        """
        now_ns = self._hw_now_ns()
        target = now_ns + self._required_lead_ns()
        return max(1, first_slot_at_or_after(target) - hw_ns_to_slot(now_ns))

    def resolve(self, action, *, request_id: str) -> ScheduledSlot:
        # 매 resolve마다 실 hardware clock·권위 lead를 반영한다(고정 stub 값 사용 안 함).
        self._current = max(self._current, self._current_slot_from_hw())
        self._lead = self._lead_slots()
        slot = super().resolve(action, request_id=request_id)
        # 기반 좌표는 ns로 통일해 두었으므로 scheduled_sample에는 ns가 들어 있다. 여기서 slot index를
        # 되돌려 **sample index와 hardware ns를 분리**해 돌려준다(두 단위를 같은 값으로 두지 않는다).
        slot_index = slot.scheduled_sample // DEVICE_SLOT_NS
        return ScheduledSlot(
            scheduled_sample=slot_index * resampling.DEVICE_SLOT_SAMPLES,   # sample 좌표
            scheduled_hw_timestamp_ns=slot_to_hw_ns(slot_index),            # 실 시간 좌표(정확)
            slot_key=slot.slot_key,
        )


def worker_tx_muted(worker) -> tuple:
    """실 worker의 TX lane mute 상태를 읽는다(P04).

    권위 구현 `Stage7IsolatedDualLaneWorker`는 `_tx_muted`(lane별 list, `set_tx_mute`가 갱신)를 쓴다.
    signature 호환 software double은 같은 의미의 `tx_muted`를 노출한다. 둘 다 없으면 상태를 **모른다**는
    뜻이므로 빈 tuple이 아니라 `None`을 돌려 호출자가 보수적으로 판단하게 한다(모름을 "해제"로 만들지 않음).
    """
    for name in ("_tx_muted", "tx_muted"):
        state = getattr(worker, name, None)
        if state is None:
            continue
        values = state.values() if hasattr(state, "values") else state
        try:
            return tuple(bool(v) for v in values)
        except TypeError:
            continue
    return None


class Stage7WorkerArbiter:
    """arbiter seam → 실 TX 소유권 경계(정상 TX 우선·lane 겹침 거부)에 결속(P04).

    `reserve(resource_key, owner)`가 확인하는 것:

    1. **정상 TX 우선**: 실 worker의 TX lane mute 상태(`_tx_muted`, 권위 구현 속성)를 읽는다. 정상
       서비스 TX가 retune·배타 구간에서 lane을 mute하면 PoC 예약을 거부한다(RADIO_RESOURCE_CONFLICT).
       상태를 읽을 수 없으면(속성 부재) **보수적으로 거부**한다 — 모름을 사용 가능으로 바꾸지 않는다.
    2. **중복·겹침**: 같은 resource_key(채널:slot) 재예약을 거부한다. resource_key는 slot 단위이므로
       같은 slot의 두 PoC action이 겹칠 수 없다(기존 radio_adapter의 lane-overlap 거부와 같은 의도).

    `release`는 보유분만 해제한다. **보유하지 않은 key의 release는 소유권 불명확 신호**이므로 조용히
    무시하지 않고 예외로 올린다 — 상위 provider가 `_unresolved_ownership`에 보존한다(정상 해제로 바꾸지 않음).

    실 동시 서비스 TX와의 hardware 수준 경합·실 RF atomicity는 실장비에서 확정한다.
    """

    def __init__(self, worker) -> None:
        self._worker = worker
        self._held: set = set()

    def reserve(self, *, resource_key: str, owner: str) -> bool:
        if resource_key in self._held:
            return False                      # 같은 자원 중복 예약 금지
        muted = worker_tx_muted(self._worker)
        if muted is None:
            return False                      # mute 상태 불명 → 보수적 거부(fail-closed)
        if any(muted):
            return False                      # 정상 TX가 lane 점유 중 → 정상 TX 우선
        self._held.add(resource_key)
        return True

    def release(self, *, resource_key: str, owner: str) -> None:
        if resource_key not in self._held:
            # 보유하지 않은 자원의 해제 요청: 소유권 상태가 불명확하다. 성공으로 위장하지 않는다.
            raise RuntimeError(f"release of unheld resource {resource_key!r} (소유권 불명확)")
        self._held.discard(resource_key)

    @property
    def held(self) -> frozenset:
        return frozenset(self._held)


class Stage7ReceiverReadiness:
    """rx_readiness seam → **실제 수신창 계획·수락 상태**에 근거한 readiness(P06).

    `window_ready(request) -> ReadinessDecision`. 이전 구현은 `rx_lo_hz` 일치만 봤다. 이제 실 수신기의
    상태를 세 가지로 확인한다:

    1. **tuning**: 실 worker의 RX LO가 송신 채널 center와 일치하는가. 불일치는 §5.5 자연 관측 행렬상
       정상적인 `NOT_TUNED`일 수 있다(수신 실패가 아님) — 그 경우 `NOT_TUNED` 사유로 not-ready를
       돌려주되 이를 infrastructure 실패로 격상하지 않는다. 정상 endpoint의 tuning을 바꾸지 않는다.
    2. **수신창 coverage**: 실 worker에 등록된 RX window(`rx_windows`, 권위 MAC/radio adapter가 등록)
       중 **미소비**이면서 예약 TX 시각을 덮는 창이 있는가(`start <= tx < start + count/rate`).
    3. **clock/generation 결속**: request의 clock_id·bridge_generation·correction_revision을 증거로 보존한다.

    창이 없으면 not-ready(RF 전 fail-closed)다. 창을 여기서 새로 만들지 않는다(권위 소유자는 MAC이다).
    실 RX 창 충전·복호 품질은 실장비에서 확정한다.
    """

    def __init__(self, workers_by_station: dict, channel_center_hz) -> None:
        self._workers = dict(workers_by_station)
        self._center = channel_center_hz

    @staticmethod
    def _covering_window(worker, tx_ns: int):
        """예약 TX 시각을 덮는 **미소비** RX window(있으면). 실 등록 창만 본다(합성 없음)."""
        windows = getattr(worker, "rx_windows", None)
        if not windows:
            return None
        for lane, entries in (windows.items() if hasattr(windows, "items") else enumerate(windows)):
            for entry in entries or ():
                if entry.get("consumed"):
                    continue
                start = int(entry.get("start_ns", 0))
                count = int(entry.get("count", 0))
                span = count * 1_000_000_000 // resampling.DEVICE_SAMPLE_RATE_HZ
                if start <= tx_ns < start + span:
                    return {"lane": lane, "start_ns": start, "count": count}
        return None

    def window_ready(self, request) -> ReadinessDecision:
        worker = self._workers.get(request.endpoint)
        if worker is None:
            return ReadinessDecision(ready=False, reason="RX_ENDPOINT_UNKNOWN")
        expected = self._center.get(request.channel)
        actual = getattr(worker, "rx_lo_hz", None)
        if actual != expected:
            # §5.5 자연 관측 행렬상 정상일 수 있는 상태다(수신 실패로 만들지 않는다).
            return ReadinessDecision(ready=False, reason="NOT_TUNED")
        covering = self._covering_window(worker, int(request.scheduled_hw_timestamp_ns))
        # 서비스 모드 수신은 **연속 RX**다(`start_rx_continuous`: 창별 유한 스트림 대신 단일 연속
        # 스트림, 창 경계는 collector가 태그). 따라서 수신 가능 여부는 (a) 예약 시각을 덮는 등록 창이
        # 있거나 (b) 연속 RX가 켜져 있으면 성립한다. 둘 다 아니면 수신이 계획되지 않은 것이다.
        continuous = bool(getattr(worker, "rx_continuous", False))
        if covering is None and not continuous:
            return ReadinessDecision(ready=False, reason="RX_WINDOW_NOT_PLANNED")
        return ReadinessDecision(ready=True, reason="READY", proof={
            "endpoint": request.endpoint,
            "rx_lo_hz": actual,
            "rx_window": covering,                       # 실 등록 창(있으면 계획·수락 증거)
            "rx_continuous": continuous,                 # 실 연속 RX 상태
            "clock_id": request.clock_id,
            "bridge_generation": request.bridge_generation,
            "correction_revision": request.correction_revision,
        })


#: 실 worker async TX outcome → child terminal(P10). 권위 구현 `b210_worker._decode_async`가 내는
#: outcome 이름을 그대로 쓴다. 목록에 없는 outcome은 성공으로 추정하지 않는다(UNKNOWN).
_TX_OUTCOME_TERMINAL = {
    "BURST_ACK": ChildTerminal.COMPLETED,
    "UNDERFLOW": ChildTerminal.FAILED,
    "TIME_ERROR": ChildTerminal.FAILED,
    "SEQUENCE_ERROR": ChildTerminal.FAILED,
    "STREAM_ERROR": ChildTerminal.FAILED,
}


class Stage7TxTerminalSource:
    """terminal_source → **실 async TX 상태**로 child terminal 확정(P10).

    이전 구현은 fake 매질 `delivery_log`에 cable 기록이 있으면 COMPLETED로 봤다. 그것은 매질 도달
    기록이지 송신기의 TX 완료 통지가 아니며, **과거 송신 기록이 현재 송신을 완료 처리**할 수 있었다.

    이제 실 worker의 비동기 TX 통지(`poll_async()` → `{"outcome", "hardware_time_ns", "channel"}`)를
    수집해 parent의 **예약 hardware 시각**과 child의 **lane**에 결속한다.

    **현재 한계(P10 미완, 정직 표기):** 이 통지의 권위 소유자는 `Stage7B210RadioAdapter`다. adapter가
    `poll_once()`에서 `worker.poll_async()`를 드레인해 자신의 TX 원장(`_tx_parents`)을 갱신한다. PoC
    group TX는 adapter의 `schedule_tx`를 거치지 않고 worker에 직접 제출되므로, 그 BURST_ACK은 adapter가
    소비하되 대응하는 원장 항목이 없어 버려진다. 따라서 PoC TX terminal은 **UNKNOWN으로 남는다**.
    이것을 매질 delivery 로그로 COMPLETED라고 만들지 않는다(이전 구현의 결함). 해소하려면 PoC TX를
    권위 `schedule_tx` 경로로 보내야 하고, 그러려면 sidecar route 예약이 공유매체 fan-out(2 child)을
    지원해야 한다(P08과 맞물린 계약 결정). 아래 결속 로직은 그 배선이 끝나면 그대로 동작한다.

    결속 규칙:

    - 해당 (시각, lane)의 `BURST_ACK` → `COMPLETED`
    - 해당 (시각, lane)의 오류 outcome(UNDERFLOW/TIME_ERROR/…) → `FAILED`
    - 아직 통지가 없으면 → `UNKNOWN`(**접수됐으나 미확정**; 성공으로 추정하지 않음)

    통지는 한 번만 드레인되므로 내부에 누적 보관하고, 시각·lane이 일치하는 것만 소비한다. 다른 시각의
    통지는 남겨 두어 과거/미래 송신끼리 섞이지 않는다. 실 RF atomicity·실 전파 결과는 실장비에서 확정한다.
    """

    def __init__(self, worker, *, pump=None, max_polls: int = 64) -> None:
        self._worker = worker
        self._events: list = []
        #: 시간 전진 훅. 실장비/실행 중에는 시간이 스스로 흐르므로 불필요하지만, 결정적 software
        #: group은 harness가 clock을 돌려야 통지가 due가 된다. 통지를 만들지 않고 **기다리기만** 한다.
        self._pump = pump
        self._max_polls = int(max_polls)

    def _drain(self) -> None:
        poll = getattr(self._worker, "poll_async", None)
        if not callable(poll):
            return
        try:
            for event in poll() or ():
                self._events.append(dict(event))
        except Exception:  # noqa: BLE001 - 통지 수집 실패가 판정을 성공으로 바꾸지 않는다
            pass

    def _match(self, tx_ns: int, lane):
        """이 (예약 시각, lane)에 결속된 통지만 고른다. 다른 시각의 통지는 소비하지 않는다."""
        for event in self._events:
            if int(event.get("hardware_time_ns", -2)) != tx_ns:
                continue                          # 다른 송신의 통지(과거 포함) — 재사용 금지
            channel = event.get("channel")
            if channel is not None and lane is not None and int(channel) != int(lane):
                continue
            mapped = _TX_OUTCOME_TERMINAL.get(str(event.get("outcome")))
            if mapped is not None:
                return mapped
        return None

    def _hw_now(self):
        now = getattr(self._worker, "hardware_time_ns", None)
        try:
            return int(now()) if callable(now) else None
        except Exception:  # noqa: BLE001
            return None

    def __call__(self, action, result, parent) -> dict:
        tx_ns = int(getattr(parent, "scheduled_hw_timestamp_ns", -1))
        lanes = {child.child_id: getattr(child, "tx_lane", None) for child in parent.children}
        # TX는 예약 시각(보통 지금+100~200 ms)에 일어나고 통지는 그 뒤에 온다. **접수 직후의 부재를
        # 즉시 최종 UNKNOWN으로 확정하지 않고**, 예약 시각+1 slot이 지날 때까지 유계로 기다린다.
        due_ns = tx_ns + DEVICE_SLOT_NS
        resolved = {}
        for _ in range(self._max_polls):
            self._drain()
            resolved = {cid: self._match(tx_ns, lane) for cid, lane in lanes.items()}
            if all(value is not None for value in resolved.values()):
                break                                  # 모든 child terminal 확정
            now = self._hw_now()
            if now is not None and now > due_ns and self._pump is None:
                break                                  # 예약 시각이 지났는데도 통지 없음 → 미확정
            if self._pump is None:
                break                                  # 시간 전진 수단 없음 → 현재 상태로 보고
            try:
                self._pump()                           # 시간만 전진(통지 합성 아님)
            except Exception:  # noqa: BLE001
                break
        # 미확정은 UNKNOWN으로 **보존**한다(성공 추정 금지). 상위가 SESSION_INHIBIT로 처리한다.
        return {cid: (value or ChildTerminal.UNKNOWN).value for cid, value in resolved.items()}


def stage7_terminal_source(worker, *, pump=None):
    """실 worker 비동기 TX 통지에 결속한 terminal source(P10). `pump`는 시간 전진 훅(선택)."""
    return Stage7TxTerminalSource(worker, pump=pump)


def stage7_target_response(members, *, station_to_endpoint=None):
    """target_response → **대상 endpoint의 실제 application 관측**으로 target effect 판정(P09).

    이전 구현은 매질 `delivery_log`에 cable 기록이 있으면 `EFFECT_OBSERVED`로 봤다. 그러나 물리 도달은
    공격 **효과**가 아니다(§11.1: action_execution과 target_effect는 분리). 물리적으로 도달해도 PHY
    복호·Link 검증·주소 수락을 통과해 application에 전달돼야 효과다.

    이제 대상 endpoint의 실 application sink(`endpoint.start_gate.application_sink`)에서 이 action 시점
    **이후** 도착한 수신 event가 있는지 본다. 있으면 `EFFECT_OBSERVED`, 없으면 `INDETERMINATE`다.
    RF-off에서 합성 packet은 보통 정상 복호·주소 수락을 통과하지 못하므로 `INDETERMINATE`가 정직한
    결과다(없는 효과를 만들지 않는다). 실 복호 품질·간섭·충돌 효과는 실장비에서 확정한다.
    """
    from vdeter.stage8.execution.verdict import TargetEffect

    mapping = dict(station_to_endpoint or {ep: ep for ep in members})

    def _application_events(endpoint_id):
        member = members.get(endpoint_id)
        if member is None:
            return ()
        sink = getattr(getattr(member.endpoint, "start_gate", None), "application_sink", None)
        events = getattr(sink, "events", None)
        try:
            return tuple(events()) if callable(events) else tuple(events or ())
        except Exception:  # noqa: BLE001 - 관측 수집 실패를 효과로 바꾸지 않는다
            return ()

    def response(action, result, parent):
        target_ep = mapping.get(getattr(parent, "addressed_destination", None))
        if target_ep is None:
            return (TargetEffect.INDETERMINATE, "addressed target endpoint unknown")
        before = response.baseline.get(target_ep)
        now = len(_application_events(target_ep))
        if before is None:
            response.baseline[target_ep] = now          # 첫 조회: 기준선만 잡는다
            return (TargetEffect.INDETERMINATE,
                    "no application-level receive evidence bound to this action")
        if now > before:
            return (TargetEffect.EFFECT_OBSERVED,
                    "real application receive event at addressed target after this action")
        return (TargetEffect.INDETERMINATE,
                "no application-level receive evidence bound to this action")

    response.baseline = {}
    return response


def stage7_device_seams(members, *, channel_center_hz=None):
    """실행 중 software group의 실 MASS-B endpoint 객체로 `DeviceSeams`를 만든다(no stub).

    `assemble_provider(DeviceSeams)` 경로(poc_run→prepare_poc_workload)가 그대로 실 seam으로 provider를
    조립하도록, 5개 seam을 실 Stage 7 객체로 채운다: group_tx=실 worker(assemble가 Stage7GroupTxRadioSink로
    감쌈)·arbiter=실 lane 점유·scheduler=실 hardware clock slot·rx_readiness=실 수신기 RX·waveform=실 frozen PHY.
    """
    from vdeter.stage8.integration.assembly import DeviceSeams

    center = channel_center_hz or constants.CHANNEL_EFFECTIVE_CENTER_HZ
    mass_b = members["MASS-B"].endpoint.dependencies.radio.lease.worker
    workers = {ep: members[ep].endpoint.dependencies.radio.lease.worker for ep in members}
    return DeviceSeams(
        group_tx=mass_b,                                      # 실 group-TX worker(하드웨어 경계 double)
        arbiter=Stage7WorkerArbiter(mass_b),                  # 실 worker lane 점유
        scheduler=Stage7HwTimeScheduler(mass_b),              # 실 hardware clock slot
        rx_readiness=Stage7ReceiverReadiness(workers, center),  # 실 수신기 RX 준비
        waveform_factory=Link11WaveformFactory(),             # 실 frozen PHY
    )


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
    "stage7_device_seams", "assemble_stage7_provider",
)
