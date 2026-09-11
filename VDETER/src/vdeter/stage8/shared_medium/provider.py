"""SharedMediumActionProvider — Stage 3 `ActionSubmitProvider` seam의 G1-호환 production 구현
(명세 §5.4, §9.4, §10, POC-D12/D14/D23, WSM-REQ-13; 연결 API §9.4, §14).

논리 target action을 canonical Physical Channel과 단일 parent transmission으로 유도하고, C4+C6
atomic fan-out으로 방사한다. client는 논리 target과 상대 schedule만 싣고, frequency·lane·gain·
waveform·hardware timestamp·slot·**trigger 기준시각**·동시 lower+upper TX를 직접 지정할 수 없다.

scheduling(§10.2): PoC는 NEXT_SAFE_SLOT / after_slots / after_ms만 표현한다.
- trigger 기준 slot/timestamp는 **VDETER가 관측 event에서 제공**한다(client trigger field는 거부).
- `after_slots`는 trigger **slot** 기준 N개 뒤, `after_ms`는 trigger **hardware timestamp** + N ms
  이후의 첫 합법 slot(올림)이다. 과거·닫힌·lead 부족 slot은 이동하지 않고 `POC_DEADLINE_MISSED`.
- schedule의 key·type·범위를 먼저 검증해 `POC_ACTION_SCHEMA_INVALID`로 구조화한다(raw 예외 금지).

오류는 Stage 3 오류 계약(`PocError`)으로만 표출한다: group commit 불명확은
`POC_TX_OUTCOME_UNKNOWN`(SESSION_INHIBIT)으로 변환하고 request→parent correlation과 cleanup을
보존한다. RF 방사 전 거부(readiness·staging·capacity)는 `POC_RADIO_RESOURCE_CONFLICT`로 자원을
반환한다. parent 생성부터 transmit까지는 하나의 보호 구간이라 어떤 예외에서도 예약이 누수되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..poc_api.errors import PocError, PocErrorCode
from ..poc_api.types import (
    ActionDescriptor,
    FORBIDDEN_ACTION_FIELDS,
    PhysicalChannel,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
)
from . import constants, tuning
from .radiation import (
    AtomicBroadcast,
    BroadcastCapacityError,
    BroadcastError,
    BroadcastNotAdmitted,
    BroadcastNotReady,
    BroadcastOutcomeUnknown,
    ParentOutcome,
    ScheduledSlot,
)

#: PoC가 schedule에 직접 실을 수 없는 field(hardware 시각·slot·trigger 기준시각은 VDETER/MAC이 결정).
FORBIDDEN_SCHEDULE_FIELDS = frozenset(
    {
        "scheduled_sample",
        "scheduled_hw_timestamp_ns",
        "slot_key",
        "hardware_timestamp",
        "hw_time",
        "transmit_at",
        "device_time",
        "trigger_slot",
        "trigger_hw_timestamp_ns",
        "trigger_time",
        "trigger_timestamp",
    }
)

#: 허용 상대 schedule 표현(§10.2).
SCHEDULE_KINDS = frozenset({"NEXT_SAFE_SLOT", "after_slots", "after_ms"})

#: kind별 허용 key allowlist. 그 밖의 key는 모두 거부한다(오타·우회 차단).
SCHEDULE_ALLOWED_KEYS = {
    "NEXT_SAFE_SLOT": frozenset({"kind"}),
    "after_slots": frozenset({"kind", "slots"}),
    "after_ms": frozenset({"kind", "ms"}),
}

#: 상대 offset 상한(bounded). 시험 통과 목적으로 완화하지 않는다.
MAX_AFTER_SLOTS = 4096
MAX_AFTER_MS = 600_000

#: MAC slot grid(기존 Stage 7 harness와 동일 규약: 8192 samples/slot).
_SLOT_SAMPLES = 8192


@dataclass(frozen=True, slots=True)
class _CachedSubmission:
    submission: ProviderSubmission


def validate_schedule(action: ActionDescriptor) -> tuple:
    """schedule의 kind·허용 key·type·범위를 검증해 `(kind, value)`를 돌려준다.

    위반은 모두 `POC_ACTION_SCHEMA_INVALID`로 구조화한다(raw ValueError/TypeError 금지).
    `value`는 after_slots의 slots, after_ms의 ms이며 NEXT_SAFE_SLOT에서는 None이다.
    """

    schedule = action.schedule
    if schedule is None:
        schedule = {}
    if not isinstance(schedule, dict):
        raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, "schedule은 object여야 합니다")

    kind = schedule.get("kind", "NEXT_SAFE_SLOT")
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS:
        raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"schedule.kind 미지원: {kind!r}")

    allowed = SCHEDULE_ALLOWED_KEYS[kind]
    for key in schedule:
        if key in FORBIDDEN_ACTION_FIELDS or key in FORBIDDEN_SCHEDULE_FIELDS:
            raise PocError(
                PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                f"schedule에 직접 지정할 수 없는 field: {key!r} (hardware 시각·trigger 기준은 VDETER가 결정)",
            )
        if key not in allowed:
            raise PocError(
                PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                f"schedule.kind={kind}에 허용되지 않은 key: {key!r}",
            )

    if kind == "NEXT_SAFE_SLOT":
        return kind, None

    field = "slots" if kind == "after_slots" else "ms"
    limit = MAX_AFTER_SLOTS if kind == "after_slots" else MAX_AFTER_MS
    if field not in schedule:
        raise PocError(
            PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"schedule.{field}가 필요합니다(kind={kind})"
        )
    value = schedule[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PocError(
            PocErrorCode.POC_ACTION_SCHEMA_INVALID,
            f"schedule.{field}는 정수여야 합니다(받은 값 type={type(value).__name__})",
        )
    if value < 0 or value > limit:
        raise PocError(
            PocErrorCode.POC_ACTION_SCHEMA_INVALID,
            f"schedule.{field}={value}가 허용 범위 0..{limit}를 벗어납니다",
        )
    return kind, value


def _default_packet_resolver(action: ActionDescriptor, request_id: str) -> bytes:
    payload = action.payload or {}
    if "packet_hex" in payload:
        try:
            return bytes.fromhex(str(payload["packet_hex"]))
        except ValueError as error:
            raise PocError(
                PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"packet_hex 형식 오류: {error}"
            ) from error
    if "packet_bytes" in payload and isinstance(payload["packet_bytes"], (bytes, bytearray)):
        return bytes(payload["packet_bytes"])
    raise PocError(
        PocErrorCode.POC_ACTION_SCHEMA_INVALID,
        "attack action에 packet 내용(packet_hex/packet_bytes)이 필요합니다",
    )


class NextSafeSlotScheduler:
    """RF-off 기본 MAC scheduler stub. 상대 schedule을 결정적 slot으로 계산한다.

    실장비에서는 기존 Stage 7 MAC scheduler가 이 역할을 한다. 반응형 trigger 기준은 **VDETER가
    register_trigger로 등록한 관측 event**에서만 온다(client 조작 불가).

    - `after_slots`: trigger **slot** + N.
    - `after_ms`: trigger **hardware timestamp** + N ms 이후의 첫 slot(올림) → 인과성 보장.
    - 과거·닫힌·lead 부족 slot은 이동하지 않고 `POC_DEADLINE_MISSED`.

    이 stub은 hardware timestamp를 sample 좌표와 같은 단위로 모델링한다(RF-off 결정적 근사).
    """

    def __init__(self, *, current_slot: int = 0, lead_slots: int = 1, samples_per_slot: int = _SLOT_SAMPLES,
                 samples_per_ms: int = 307, closed_slots=(), triggers=None) -> None:
        self._current = int(current_slot)
        self._lead = int(lead_slots)
        self._slot_samples = int(samples_per_slot)
        self._samples_per_ms = int(samples_per_ms)
        self._closed = set(closed_slots)
        #: observation_id -> (trigger_slot, trigger_hw_timestamp_ns). VDETER가 등록한다.
        #: 초기 주입 triggers도 register_trigger와 동일한 bound·evict 경로를 거친다(상한 우회 금지).
        self._triggers: dict = {}
        for obs, value in (triggers or {}).items():
            slot, hw = value if isinstance(value, (tuple, list)) else (value, None)
            self._put_trigger(str(obs), int(slot), None if hw is None else int(hw))

    def _put_trigger(self, observation_id: str, slot: int, hw_timestamp_ns: int | None) -> None:
        """trigger 등록의 단일 경로. workload당 bounded(§10); 상한 초과 시 가장 오래된 것부터 evict."""

        if observation_id not in self._triggers and len(self._triggers) >= constants.MAX_TRIGGER_REGISTRY:
            oldest = next(iter(self._triggers))  # dict insertion order = FIFO
            del self._triggers[oldest]
        self._triggers[observation_id] = (
            int(slot), int(hw_timestamp_ns if hw_timestamp_ns is not None else slot * self._slot_samples)
        )

    def register_trigger(self, observation_id: str, *, slot: int, hw_timestamp_ns: int | None = None) -> None:
        """VDETER가 관측 event를 낼 때 그 trigger 기준 slot/timestamp를 등록한다(client 아님).

        registry는 workload당 bounded다(§10). 상한 초과 시 가장 오래된 trigger부터 evict한다
        (오래된 trigger는 이미 과거 deadline이라 재사용 시 POC_DEADLINE_MISSED로 걸린다).
        """

        self._put_trigger(observation_id, slot, hw_timestamp_ns)

    def clear_triggers(self) -> None:
        """workload cleanup: 등록된 trigger를 모두 제거한다(잔존 0)."""

        self._triggers.clear()

    @property
    def trigger_count(self) -> int:
        return len(self._triggers)

    def resolve(self, action: ActionDescriptor, *, request_id: str) -> ScheduledSlot:
        kind, value = validate_schedule(action)
        earliest = self._current + self._lead

        if kind == "NEXT_SAFE_SLOT":
            slot = earliest
        else:
            # 반응형: trigger 기준은 VDETER가 등록한 관측에서만 얻는다.
            obs = action.source_observation_id
            if obs is None or obs not in self._triggers:
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                    "반응형 schedule에는 VDETER가 제공한 유효한 trigger 관측(source_observation_id)이 필요합니다",
                )
            trigger_slot, trigger_ns = self._triggers[obs]
            if kind == "after_slots":
                slot = trigger_slot + value            # trigger slot 기준
            else:  # after_ms: trigger hardware timestamp 기준 + N ms, 이후 첫 slot(올림)
                target_ns = trigger_ns + value * self._samples_per_ms
                slot = -(-target_ns // self._slot_samples)  # ceil division
                if slot * self._slot_samples < target_ns:   # 방어적 인과성 검사
                    raise PocError(
                        PocErrorCode.POC_DEADLINE_MISSED,
                        f"after_ms 인과성 위반(slot {slot} < target {target_ns}) — 이동하지 않음",
                    )

        if slot < earliest:
            raise PocError(
                PocErrorCode.POC_DEADLINE_MISSED,
                f"요청 slot {slot}이 earliest {earliest} 이전(과거/lead 부족) — 이동하지 않음",
            )
        if slot in self._closed:
            raise PocError(PocErrorCode.POC_DEADLINE_MISSED, f"slot {slot}이 닫힘 — 이동하지 않음")
        return ScheduledSlot(
            scheduled_sample=slot * self._slot_samples,
            scheduled_hw_timestamp_ns=slot * self._slot_samples,
            slot_key=f"slot-{slot}",
        )


class SharedMediumActionProvider:
    """`ActionSubmitProvider` 계약(derive_physical_channel + submit)의 production 구현.

    scheduler와 waveform_factory는 주입이 원칙이다. RF-off 기본값(stub scheduler·참조 modem)을 쓰려면
    `allow_rf_off_defaults=True`로 **명시 허용**해야 하며, 그렇지 않으면 생성 자체가 실패한다
    (production 조립에서 실물 주입 누락이 조용히 통과하지 않도록 fail-closed).
    """

    def __init__(
        self,
        *,
        radio_sink,
        rx_readiness,
        scheduler=None,
        waveform_factory=None,
        arbiter=None,
        dual_channel_installed: bool = False,
        packet_resolver=None,
        allow_rf_off_defaults: bool = False,
    ) -> None:
        missing = [
            name for name, value in (("scheduler", scheduler), ("waveform_factory", waveform_factory))
            if value is None
        ]
        if missing and not allow_rf_off_defaults:
            raise ValueError(
                f"{', '.join(missing)}를 주입하거나 allow_rf_off_defaults=True로 RF-off 기본값을 "
                "명시 허용해야 합니다(production fail-closed)"
            )
        self._broadcast = AtomicBroadcast(
            radio_sink=radio_sink,
            rx_readiness=rx_readiness,
            waveform_factory=waveform_factory,
            arbiter=arbiter,
        )
        self._arbiter = arbiter
        self._scheduler = scheduler or NextSafeSlotScheduler()
        self._dual_channel_installed = bool(dual_channel_installed)
        self._packet_resolver = packet_resolver or _default_packet_resolver
        self._cache: dict = {}          # request_id -> _CachedSubmission (exactly-once, 성공만)
        self._parents: dict = {}        # request_id -> parent (correlation)
        self._reservations: dict = {}   # request_id -> resource_key (자원 반환용)
        self._outcomes: dict = {}       # parent_id -> ParentOutcome (evidence 반영)
        #: release가 실패해 소유권이 불확실한 resource_key(복구 정보 보존·evidence). cleanup으로 지우지 않는다.
        self._unresolved_ownership: set = set()

    @classmethod
    def for_production(cls, *, radio_sink, rx_readiness, scheduler, waveform_factory,
                       arbiter=None, dual_channel_installed: bool = False, packet_resolver=None):
        """production 조립용 fail-closed 생성자. scheduler·frozen PHY factory가 필수다."""

        if scheduler is None:
            raise ValueError("production provider에는 실물 MAC scheduler가 필수입니다(fail-closed)")
        if waveform_factory is None:
            raise ValueError("production provider에는 frozen PHY waveform factory가 필수입니다(fail-closed)")
        return cls(
            radio_sink=radio_sink, rx_readiness=rx_readiness, scheduler=scheduler,
            waveform_factory=waveform_factory, arbiter=arbiter,
            dual_channel_installed=dual_channel_installed, packet_resolver=packet_resolver,
            allow_rf_off_defaults=False,
        )

    # --- ActionSubmitProvider 계약 -----------------------------------------
    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        if target not in TARGET_TO_PHYSICAL_CHANNEL:
            raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action: ActionDescriptor, *, request_id: str) -> ProviderSubmission:
        """논리 target action을 단일 parent·C4+C6 fan-out으로 방사한다(exactly-once)."""

        if request_id in self._cache:
            return self._cache[request_id].submission

        # --- 1. 자원 확보 전 검증(보유 자원 없음). 예상 밖 예외도 PocError로 구조화. ---
        try:
            target = action.target
            if target not in constants.ATTACK_ALLOWED_TARGETS:
                raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")
            self._reject_forbidden_fields(action)
            validate_schedule(action)
            channel = self.derive_physical_channel(target)
            packet = self._packet_resolver(action, request_id)
            slot = self._scheduler.resolve(action, request_id=request_id)
            if not isinstance(slot, ScheduledSlot):
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID, "scheduler가 ScheduledSlot을 반환하지 않음"
                )
        except PocError:
            raise
        except Exception as error:  # noqa: BLE001 - raw 예외를 Stage 3로 올리지 않는다
            raise PocError(
                PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"action 검증/스케줄 계산 실패: {error}"
            ) from error

        # --- 2. 자원 예약(정상 TX 우선) ---
        resource_key = f"{channel.value}:{slot.slot_key}"
        if self._arbiter is not None:
            # arbiter.reserve의 raw 예외도 public 경계 밖으로 내보내지 않는다(예약 미보유 → 누수 없음).
            try:
                reserved = self._arbiter.reserve(resource_key=resource_key, owner="POC")
            except PocError:
                raise
            except Exception as error:  # noqa: BLE001
                raise PocError(
                    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, f"arbiter reserve 실패: {error}"
                ) from error
            if not reserved:
                raise PocError(
                    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, f"자원 {resource_key}를 정상 TX가 점유 중"
                )
            self._reservations[request_id] = resource_key

        # --- 3. parent 생성 + atomic 방사: 하나의 보호 구간(예약 누수 0, PocError 변환 보장) ---
        parent = None
        try:
            parent = self._broadcast.build_parent(
                source_station=constants.ATTACKER_STATION,
                channel=channel,
                payload=packet,
                scheduled_sample=slot.scheduled_sample,
                scheduled_hw_timestamp_ns=slot.scheduled_hw_timestamp_ns,
                slot_key=slot.slot_key,
                addressed_destination=target,
            )
            # correlation을 방사 시도 시점부터 보존한다(OUTCOME_UNKNOWN 시 evidence·cleanup 위해).
            self._parents[request_id] = parent
            self._broadcast.transmit(parent)
        except BroadcastOutcomeUnknown as error:
            # commit 불명확 -> SESSION_INHIBIT. 자원 해제하지 않고(불확실) correlation 보존.
            self._outcomes[parent.parent_id] = ParentOutcome.OUTCOME_UNKNOWN
            raise PocError(
                PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                f"group commit 불명확 (parent {parent.parent_id})",
                detail={"broadcast_parent_id": parent.parent_id, "transfer_id": parent.transfer_id},
            ) from error
        except (BroadcastNotReady, BroadcastNotAdmitted, BroadcastCapacityError) as error:
            # RF 방사 전 거부(안전): 자원 반환하고 PoC action만 거부(정상 TX 유지).
            self._abort_attempt(request_id)
            raise PocError(
                PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, f"RF 방사 전 거부: {error}"
            ) from error
        except BroadcastError as error:
            self._abort_attempt(request_id)
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, f"broadcast 구조 오류: {error}") from error
        except PocError:
            self._abort_attempt(request_id)
            raise
        except Exception as error:  # noqa: BLE001 - waveform factory 등 임의 예외도 구조화 + 자원 반환
            self._abort_attempt(request_id)
            raise PocError(
                PocErrorCode.POC_PROTOCOL_ERROR, f"parent 생성/방사 실패: {error}"
            ) from error
        except BaseException:  # noqa: BLE001 - KeyboardInterrupt/SystemExit 등: 자원 반환 후 그대로 전파
            # BaseException은 PocError로 바꾸지 않고 전파하되, 예약 누수는 막는다(release 실패는
            # _unresolved_ownership에 기록). 운영자 중단 의도를 삼키지 않는다.
            self._abort_attempt(request_id)
            raise

        submission = ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=parent.parent_id,
            transfer_id=parent.transfer_id,
        )
        self._cache[request_id] = _CachedSubmission(submission=submission)
        return submission

    # --- terminal 취합 + 자원 회수 -----------------------------------------
    def resolve_outcome(self, request_id: str, terminals: dict):
        """parent outcome을 확정하고 PoC 자원 예약을 release한다(§6.1, §10). evidence에 반영.

        이미 확정된 parent의 재-resolve는 raw 예외 대신 저장된 결과를 idempotent하게 돌려준다
        (public 경계 밖으로 PocError 외 예외를 내지 않는다).
        """

        parent = self._parents.get(request_id)
        if parent is None:
            raise PocError(PocErrorCode.POC_NOT_RUNNING, f"알 수 없는 request_id: {request_id}")
        # 이미 확정된 parent → 저장 결과를 idempotent 반환(재-resolve 안전, 재해제 없음).
        if self._broadcast.is_resolved(parent.parent_id):
            return self._outcomes.get(parent.parent_id) or self._broadcast.outcome_of(parent.parent_id)
        try:
            outcome = self._broadcast.resolve_outcome(parent.parent_id, terminals)
        except BroadcastError as error:
            # invalid terminal·unknown parent: 성공 처리·reservation 해제 없이 구조화 오류로 변환.
            raise PocError(
                PocErrorCode.POC_PROTOCOL_ERROR, f"terminal 취합 실패: {error}"
            ) from error
        self._outcomes[parent.parent_id] = outcome
        if not self._release_reservation(request_id):
            # 예약 반환 실패 → 자원 소유권 불확실(SESSION_INHIBIT). outcome은 이미 저장됨.
            raise PocError(
                PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN,
                f"arbiter release 실패 — 자원 소유권 불확실 (parent {parent.parent_id})",
                detail={"broadcast_parent_id": parent.parent_id,
                        "unresolved_ownership": sorted(self._unresolved_ownership)},
            )
        return outcome

    def outcomes_by_parent(self) -> dict:
        """parent_id -> ParentOutcome. evidence 조립에 넘겨 최신 결과를 반영한다."""

        return dict(self._outcomes)

    def unresolved_ownership(self) -> tuple:
        """release 실패로 소유권이 불확실한 resource_key 목록(evidence·복구용)."""

        return tuple(sorted(self._unresolved_ownership))

    def register_observation_trigger(self, observation_id: str, *, slot: int, hw_timestamp_ns=None) -> None:
        """VDETER 관측 event → scheduler trigger 등록 production hook(§10).

        Stage 6 supervisor가 관측 event(PACKET_OBSERVED·SCENARIO_STARTED·STEP_STARTED)를 낼 때
        이 hook으로 trigger 기준 slot/timestamp를 등록한다. client는 trigger를 직접 만들 수 없다.
        scheduler가 register_trigger를 제공하지 않으면 reactive scheduling을 광고하지 않는 것이다.
        """

        register = getattr(self._scheduler, "register_trigger", None)
        if register is None:
            raise PocError(
                PocErrorCode.POC_PROTOCOL_ERROR,
                "scheduler가 trigger 등록을 지원하지 않습니다(reactive scheduling 미지원)",
            )
        register(observation_id, slot=slot, hw_timestamp_ns=hw_timestamp_ns)

    def build_evidence(self, parents, scene, *, broadcast_results=(), scenario_result=None, poc_result=None):
        """확정 outcome을 자동 반영해 evidence를 조립한다(PENDING 회귀 방지).

        `evidence.build_evidence`에 `outcomes=self.outcomes_by_parent()`를 주입하므로 호출자가
        outcomes 인자를 잊어도 확정된 terminal이 PENDING으로 되돌아가지 않는다.
        """

        from . import evidence as _evidence

        return _evidence.build_evidence(
            parents, scene, broadcast_results=broadcast_results,
            outcomes=self.outcomes_by_parent(), unresolved_ownership=self.unresolved_ownership(),
            scenario_result=scenario_result, poc_result=poc_result,
        )

    def _abort_attempt(self, request_id: str) -> None:
        """RF 방사 전/구조 실패: 예약을 반환하고 correlation 시도 기록을 지운다."""

        self._release_reservation(request_id)
        self._parents.pop(request_id, None)

    def _release_reservation(self, request_id: str) -> bool:
        """예약을 반환한다. tracking은 항상 pop하고, arbiter.release 예외는 삼켜 False를 돌려준다
        (release 실패로 나머지 정리가 중단되지 않게 한다)."""

        key = self._reservations.pop(request_id, None)  # 복구 정보를 잃지 않도록 먼저 pop
        if self._arbiter is not None and key is not None:
            try:
                self._arbiter.release(resource_key=key, owner="POC")
            except Exception:  # noqa: BLE001 - release 실패는 상위에서 OWNERSHIP_UNCERTAIN으로 승격
                self._unresolved_ownership.add(key)  # 실패 식별자 보존(evidence)
                return False
        return True

    # --- capability ---------------------------------------------------------
    def advertised_capabilities(self) -> frozenset:
        return tuning.advertised_capabilities(dual_channel_installed=self._dual_channel_installed)

    def prepare_dual_channel(self, *, required: bool):
        return tuning.resolve_dual_channel(
            required=required, dual_channel_installed=self._dual_channel_installed
        )

    # --- correlation / cleanup ---------------------------------------------
    def parent_for(self, request_id: str):
        return self._parents.get(request_id)

    @property
    def active_group_count(self) -> int:
        return self._broadcast.active_group_count

    def cleanup(self) -> None:
        """모든 PoC 자원 예약을 release하고 fan-out group·cache·correlation·trigger 잔존을 0으로 정리한다.

        개별 release/정리 실패가 나머지 정리를 중단시키지 않는다(잔존 0 보장). 하나라도 실패하면
        정리를 모두 마친 뒤 `POC_RADIO_OWNERSHIP_UNCERTAIN`(SESSION_INHIBIT)으로 승격한다.
        """

        failed = False
        try:
            if self._arbiter is not None:
                for key in list(self._reservations.values()):
                    try:
                        self._arbiter.release(resource_key=key, owner="POC")
                    except Exception:  # noqa: BLE001 - 개별 실패는 승격, 정리는 계속
                        failed = True
                        self._unresolved_ownership.add(key)  # 실패 식별자 보존(evidence)
        finally:
            self._reservations.clear()
            try:
                self._broadcast.cleanup()
            except Exception:  # noqa: BLE001
                failed = True
            clear = getattr(self._scheduler, "clear_triggers", None)
            if clear is not None:
                try:
                    clear()
                except Exception:  # noqa: BLE001
                    failed = True
            self._cache.clear()
            self._parents.clear()
            self._outcomes.clear()
        if failed:
            raise PocError(
                PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN,
                "cleanup 중 arbiter release/정리 실패 — 자원 소유권 불확실(정리는 완료됨)",
                detail={"unresolved_ownership": sorted(self._unresolved_ownership)},
            )

    # --- 내부 ---------------------------------------------------------------
    def _reject_forbidden_fields(self, action: ActionDescriptor) -> None:
        payload = action.payload or {}
        for key in payload:
            if key in FORBIDDEN_ACTION_FIELDS:
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"금지된 RF field 요청: {key!r}"
                )


__all__ = (
    "FORBIDDEN_SCHEDULE_FIELDS",
    "SCHEDULE_KINDS",
    "SCHEDULE_ALLOWED_KEYS",
    "MAX_AFTER_SLOTS",
    "MAX_AFTER_MS",
    "validate_schedule",
    "NextSafeSlotScheduler",
    "SharedMediumActionProvider",
)
