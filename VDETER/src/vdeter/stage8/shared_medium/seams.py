"""Stage 5 내부 seam(Protocol). 실제 UHD/adapter는 Stage 6에서 연결하고, RF-off에서는
spy/fake로 대체한다. duck-typing(`typing.Protocol`)이므로 명시적 상속이 필요 없다.

- `WaveformFactory` : parent waveform을 **정확히 한 번** 생성한다(single-burst invariant).
- `RadioBurstSink`  : fan-out group을 **atomic 2단계**(prepare_group -> commit_group)로 제출한다.
  per-child 순차 제출을 하지 않으므로 "한 child만 방사"가 구조적으로 불가능하다. 실장비에서는
  Stage 7의 group TX 경계(`Stage7B210RadioAdapter.schedule_tx` -> `worker.retune_tx_lo` +
  `worker.submit_tx_group`)에 결속한다: prepare_group = 두 lane staging/retune 예약,
  commit_group = 같은 hardware timestamp의 group TX 단일 atomic 제출.
- `SlotScheduler`   : PoC의 상대 schedule(NEXT_SAFE_SLOT / after_slots / after_ms)을 받아 기존
  MAC scheduler가 실제 hardware 시각을 계산한다. PoC는 hardware timestamp를 직접 제출하지 않는다.
  과거 시각·부족한 lead·닫힌 slot은 이동하지 않고 `POC_DEADLINE_MISSED`로 거부한다(§10).
- `RxReadinessProvider` : required receiver의 RX window readiness를 반환한다(§6.2).
- `ResourceArbiter` : 정상 TX 우선 자원 예약. PoC는 겹치면 거부되고 정상 TX를 취소하지 않는다.

이 module은 radiation을 import하지 않는다(순환 방지). 인자 type은 duck-typed다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WaveformFactory(Protocol):
    """parent 논리 burst의 waveform(IqBurst)을 생성한다. fan-out과 무관하게 한 번만 호출한다."""

    def encode(self, *, source_station: str, channel, payload: bytes):
        """:class:`~vdeter.stage8.shared_medium.iq.IqBurst`를 반환한다."""
        ...


@runtime_checkable
class RadioBurstSink(Protocol):
    """fan-out group을 atomic 2단계로 제출한다(§6.1, WSM-D07).

    - `prepare_group`: 두 child를 함께 staging한다(retune 예약·admission 검증). 어느 하나라도
      접수 불가하면 예외로 group 전체를 거부하고 아무 것도 방사하지 않는다. admission handle을 돌려준다.
    - `commit_group`: prepare된 group을 같은 hardware timestamp로 한 번에 방사한다(all-or-nothing).
      per-child 순차 제출 API는 없다.
    """

    def prepare_group(self, *, group_id: str, children) -> object:
        ...

    def commit_group(self, admission) -> None:
        ...


@runtime_checkable
class SlotScheduler(Protocol):
    """PoC 상대 schedule을 실제 slot으로 계산하는 기존 MAC scheduler 경계(§10, POC-D12).

    입력은 NEXT_SAFE_SLOT / after_slots / after_ms 세 표현뿐이며, 반환은
    :class:`~vdeter.stage8.shared_medium.radiation.ScheduledSlot`이다. 과거·닫힌·lead 부족 slot은
    이동하지 않고 `PocError(POC_DEADLINE_MISSED)`로 거부한다.

    반응형(after_slots/after_ms)의 trigger 기준 slot/timestamp는 client가 아니라 **VDETER가
    관측 event에서 `register_trigger`로 등록**한다(hardware timestamp 조작 방지). Stage 6 supervisor는
    관측 event(PACKET_OBSERVED·SCENARIO_STARTED·STEP_STARTED)를 낼 때 이 hook을 호출하고,
    workload cleanup에서 `clear_triggers`로 registry를 비운다(bounded·per-workload). trigger registry는
    구현이 bounded로 유지해야 한다(§10). scheduler가 register_trigger를 제공하지 않으면 reactive
    scheduling을 지원하지 않는 것으로 간주한다.
    """

    def resolve(self, action, *, request_id: str):
        ...

    def register_trigger(self, observation_id: str, *, slot: int, hw_timestamp_ns=None) -> None:
        ...

    def clear_triggers(self) -> None:
        ...


@runtime_checkable
class RxReadinessProvider(Protocol):
    """required receiver의 RX window readiness(§6.2). ready=False면 RF 전 fail-closed한다."""

    def window_ready(self, request) -> "ReadinessDecision":
        ...


@runtime_checkable
class ResourceArbiter(Protocol):
    """자원(slot/lane/time) 예약. 정상 scenario/infra TX가 PoC보다 우선한다(POC-D14).

    `reserve`가 False를 돌려주면 정상 TX가 이미 점유한 것이며, PoC action만 거부한다.
    정상 TX를 취소·재예약하지 않는다. PoC 예약은 terminal·cleanup에서 반드시 release한다.
    """

    def reserve(self, *, resource_key: str, owner: str) -> bool:
        ...

    def release(self, *, resource_key: str, owner: str) -> None:
        ...


__all__ = (
    "WaveformFactory",
    "RadioBurstSink",
    "SlotScheduler",
    "RxReadinessProvider",
    "ResourceArbiter",
)
