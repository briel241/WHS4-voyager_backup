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
보존한다. RF 방사 전 거부(readiness·staging·capacity·**commit 명시 거부**)는
`POC_RADIO_RESOURCE_CONFLICT`로 자원을 반환한다. 권위 장치가 스스로 거부를 회신한 것은 "방사
여부 불명"이 아니므로 UNKNOWN으로 승격하지 않는다. parent 생성부터 transmit까지는 하나의 보호
구간이라 어떤 예외에서도 예약이 누수되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..api.codec import canonical_sha256
from ..api.errors import PocError, PocErrorCode
from ..api.limits import MAX_WORKLOAD_ACTION_REQUESTS
from ..api.types import (
    ActionDescriptor,
    ActionKind,
    FORBIDDEN_ACTION_FIELDS,
    PhysicalChannel,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
)
from . import constants, tuning
from .radiation import (
    AtomicBroadcast,
    BroadcastCapacityError,
    BroadcastCommitRefused,
    BroadcastError,
    BroadcastNotAdmitted,
    BroadcastNotReady,
    BroadcastOutcomeUnknown,
    BroadcastResult,
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
    digest: str
    broadcast_result: BroadcastResult


def _action_digest(action: ActionDescriptor) -> str:
    """Canonical immutable identity, including the internal bytes-only resolver extension."""

    body = {"action_kind": ActionKind(action.kind).value, "target": action.target,
            "schedule": action.schedule or {}, "payload": dict(action.payload or {})}
    if action.source_observation_id is not None:
        body["source_observation_id"] = action.source_observation_id
    packet = body["payload"].get("packet_bytes")
    if isinstance(packet, (bytes, bytearray)):
        body["payload"]["packet_bytes"] = bytes(packet).hex()
        body["internal_binary_packet"] = True
    return canonical_sha256(body)


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

    def slot_for_hw_ns(self, hw_timestamp_ns: int) -> int:
        """hardware timestamp → 이 scheduler의 slot 좌표. **slot 단위의 단일 소유자**다.

        상위(연결 layer)가 관측 hardware timestamp로 trigger를 등록할 때 slot 환산을 직접 하지 않고
        이 함수를 쓴다. 기본 stub은 hardware timestamp를 sample 좌표와 같은 단위로 모델링하므로
        `_slot_samples`(=8192)로 나눈다. 실 MAC scheduler 결속(subclass)은 실제 device slot 길이를
        `_slot_samples`로 두므로 같은 식이 실 시간축에서 성립한다.
        """

        return max(0, int(hw_timestamp_ns) // self._slot_samples)

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
            if slot <= trigger_slot or slot * self._slot_samples < trigger_ns:
                raise PocError(
                    PocErrorCode.POC_DEADLINE_MISSED,
                    "반응 TX는 관측 trigger 이후 slot이어야 합니다 — 이동하지 않음",
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
        self._scheduler = NextSafeSlotScheduler() if scheduler is None else scheduler
        self._dual_channel_installed = bool(dual_channel_installed)
        self._packet_resolver = _default_packet_resolver if packet_resolver is None else packet_resolver
        self._cache: dict = {}          # request_id -> _CachedSubmission (exactly-once, 성공만)
        self._parents: dict = {}        # request_id -> parent (correlation)
        self._reservations: dict = {}   # request_id -> resource_key (자원 반환용)
        self._outcomes: dict = {}       # parent_id -> ParentOutcome (evidence 반영)
        #: release가 실패해 소유권이 불확실한 resource_key(복구 정보 보존·evidence). cleanup으로 지우지 않는다.
        self._unresolved_ownership: set = set()
        self._cleanup_failures: set = set()  # component failures are not arbiter resource keys

    @classmethod
    def for_production(cls, *, radio_sink, rx_readiness, scheduler, waveform_factory,
                       arbiter=None, dual_channel_installed: bool = False, packet_resolver=None):
        """production 조립용 fail-closed 생성자. scheduler·frozen PHY factory가 필수다."""

        if scheduler is None:
            raise ValueError("production provider에는 실물 MAC scheduler가 필수입니다(fail-closed)")
        if waveform_factory is None:
            raise ValueError("production provider에는 frozen PHY waveform factory가 필수입니다(fail-closed)")
        for name, dependency, methods in (
            ("radio_sink", radio_sink, ("prepare_group", "commit_group")),
            ("rx_readiness", rx_readiness, ("window_ready",)),
            ("scheduler", scheduler, ("resolve", "register_trigger", "clear_triggers")),
            ("waveform_factory", waveform_factory, ("encode",)),
            ("arbiter", arbiter, ("reserve", "release")),
        ):
            if any(not callable(getattr(dependency, method, None)) for method in methods):
                raise ValueError(f"production {name} requires callable methods: {', '.join(methods)}")
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

        self._raise_if_ownership_uncertain()
        if not isinstance(request_id, str) or not request_id:
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                           "request_id는 비어 있지 않은 문자열이어야 합니다")
        if ParentOutcome.OUTCOME_UNKNOWN in self._outcomes.values():
            raise PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                           "이전 TX 결과가 불명확합니다 — cleanup 전 재방사 금지")
        try:
            digest = _action_digest(action)
        except Exception as error:
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                           "action canonical identity 형식 오류") from error
        if request_id in self._cache:
            cached = self._cache[request_id]
            if cached.digest != digest:
                raise PocError(PocErrorCode.POC_REQUEST_ID_CONFLICT,
                               "같은 request_id에 다른 action body가 제출됨")
            return cached.submission
        # Retain exactly-once correlation for this workload, bounded by the frozen API limit.
        if len(self._cache) >= MAX_WORKLOAD_ACTION_REQUESTS:
            raise PocError(PocErrorCode.POC_ACTION_LIMIT_EXCEEDED,
                           "workload action 상한 초과 — cleanup 전 추가 admission 금지")

        # --- 1. 자원 확보 전 검증(보유 자원 없음). 예상 밖 예외도 PocError로 구조화. ---
        try:
            target = action.target
            if target not in constants.ATTACK_ALLOWED_TARGETS:
                raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")
            self._reject_forbidden_fields(action)
            validate_schedule(action)
            channel = self.derive_physical_channel(target)
            packet = self._packet_resolver(action, request_id)
            # waveform은 슬롯과 무관하다(변조·보간·full-scale·digest). 슬롯 선택 **전에** 만들어
            # 권위 lead 재확인 이후의 임계 구간에서 이 비용을 뺀다. 슬롯 의미·lead 값은 불변이며,
            # NEXT_SAFE_SLOT은 종전대로 resolve 시점에 상대 계산된다(확정 시각을 옮기지 않는다).
            # 파형 생성 실패의 **공개 오류 코드는 종전 그대로** POC_PROTOCOL_ERROR다. 이 호출이
            # 검증 블록 안으로 올라왔다고 해서 action schema 오류로 바뀌면 안 된다(계약 불변).
            try:
                waveform = self._broadcast.build_waveform(
                    source_station=constants.ATTACKER_STATION, channel=channel, payload=packet
                )
            except PocError:
                raise
            except Exception as error:  # noqa: BLE001 - raw 예외를 Stage 3로 올리지 않는다
                raise PocError(
                    PocErrorCode.POC_PROTOCOL_ERROR, f"parent waveform 생성 실패: {error}"
                ) from error
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
            if resource_key in self._reservations.values():
                raise PocError(PocErrorCode.POC_RADIO_RESOURCE_CONFLICT,
                               f"자원 {resource_key}에 다른 PoC action이 진행 중")
            # arbiter.reserve가 예외를 내면 자원을 이미 잡았는지 불확실하다. "미예약"으로 가정하지 않고
            # OWNERSHIP_UNCERTAIN(SESSION_INHIBIT)으로 처리하며, 잡혔을 수 있는 식별자를
            # _unresolved_ownership에 보존한다(cleanup이 회수 재시도). BaseException은 그대로 전파.
            try:
                reserved = self._arbiter.reserve(resource_key=resource_key, owner="POC")
            except BaseException as error:  # ownership may change before any exception
                self._unresolved_ownership.add(resource_key)
                if not isinstance(error, Exception):
                    raise
                raise PocError(
                    PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN,
                    f"arbiter reserve 예외 — 자원 소유권 불확실: {error}",
                    detail={"unresolved_ownership": sorted(self._unresolved_ownership)},
                ) from error
            if not isinstance(reserved, bool):
                self._unresolved_ownership.add(resource_key)
                self._raise_if_ownership_uncertain()
            if not reserved:
                # 명확히 미예약(정상 TX 점유) → conflict.
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
                waveform=waveform,
            )
            # correlation을 방사 시도 시점부터 보존한다(OUTCOME_UNKNOWN 시 evidence·cleanup 위해).
            self._parents[request_id] = parent
            broadcast_result = self._broadcast.transmit(parent)
        except BroadcastOutcomeUnknown as error:
            # commit 불명확 -> SESSION_INHIBIT. 자원 해제하지 않고(불확실) correlation 보존.
            self._outcomes[parent.parent_id] = ParentOutcome.OUTCOME_UNKNOWN
            raise PocError(
                PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                f"group commit 불명확 (parent {parent.parent_id})",
                detail={"broadcast_parent_id": parent.parent_id, "transfer_id": parent.transfer_id,
                        # 불명확의 **원인 문구**를 유계로 보존한다. 이것이 없으면 evidence에
                        # "unknown"만 남아 사후에 원인을 가릴 수 없다(비밀 없는 요약만).
                        "commit_failure": str(error)[:200]},
            ) from error
        except (BroadcastNotReady, BroadcastNotAdmitted, BroadcastCapacityError,
                BroadcastCommitRefused) as error:
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
        except BaseException as error:  # noqa: BLE001 - KeyboardInterrupt/SystemExit 등
            # commit에 진입한 뒤의 인터럽트는 pre-fire 취소가 아니라 TX outcome unknown이다: 부분 방사를
            # 잃지 않도록 correlation·reservation을 유지하고 OUTCOME_UNKNOWN을 기록한 뒤 인터럽트를
            # 그대로 재전달한다(SESSION_INHIBIT). commit 진입 전(pre-fire)이면 예약을 반환한다.
            if parent is not None and self._broadcast.is_resolved(parent.parent_id):
                self._outcomes[parent.parent_id] = ParentOutcome.OUTCOME_UNKNOWN
                # Keep the same operator interruption, but hand known correlation
                # across the engine boundary before workload cleanup clears it.
                failure = PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                    "interrupted group commit", detail={
                        "broadcast_parent_id": parent.parent_id,
                        "transfer_id": parent.transfer_id})
                failure.__cause__ = error.__cause__
                raise error from failure
            try:
                self._abort_attempt(request_id)
            except BaseException as cleanup_error:
                # Ownership was tracked by the release path; cleanup must not replace the
                # operator's original interruption, even if it fails or is interrupted too.
                raise error from cleanup_error
            raise

        submission = ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=parent.parent_id,
            transfer_id=parent.transfer_id,
        )
        # Keep the real readiness result in the existing workload-bounded cache;
        # exactly-once reuse and cleanup retain/clear the same evidence snapshot.
        self._cache[request_id] = _CachedSubmission(
            submission=submission, digest=digest, broadcast_result=broadcast_result
        )
        return submission

    # --- terminal 취합 + 자원 회수 -----------------------------------------
    def resolve_outcome(self, request_id: str, terminals: dict):
        """parent outcome을 확정하고 PoC 자원 예약을 release한다(§6.1, §10). evidence에 반영.

        이미 확정된 parent의 재-resolve는 raw 예외 대신 저장된 결과를 idempotent하게 돌려준다
        (public 경계 밖으로 PocError 외 예외를 내지 않는다).
        """

        self._raise_if_ownership_uncertain()
        parent = self._parents.get(request_id)
        if parent is None:
            raise PocError(PocErrorCode.POC_NOT_RUNNING, f"알 수 없는 request_id: {request_id}")
        # 이미 확정된 parent → 저장 결과를 idempotent 반환(재-resolve 안전, 재해제 없음).
        if self._broadcast.is_resolved(parent.parent_id):
            return self._outcomes.get(parent.parent_id) or self._broadcast.outcome_of(parent.parent_id)
        try:
            outcome = self._broadcast.resolve_outcome(parent.parent_id, terminals)
        except Exception as error:
            # invalid terminal·unknown parent: 성공 처리·reservation 해제 없이 구조화 오류로 변환.
            raise PocError(
                PocErrorCode.POC_PROTOCOL_ERROR, f"terminal 취합 실패: {error}"
            ) from error
        self._outcomes[parent.parent_id] = outcome
        if outcome == ParentOutcome.OUTCOME_UNKNOWN:
            # A missing/unknown child terminal carries the same uncertainty as a failed commit.
            # Keep ownership until explicit workload cleanup; new submissions are inhibited.
            return outcome
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

        return tuple(sorted(self._unresolved_ownership | self._cleanup_failures))

    def _raise_if_ownership_uncertain(self) -> None:
        unresolved = self.unresolved_ownership()
        if unresolved:
            raise PocError(
                PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN,
                "미반환 자원 또는 미완료 cleanup — SESSION_INHIBIT",
                detail={"unresolved_ownership": list(unresolved)},
            )

    def slot_for_hw_ns(self, hw_timestamp_ns: int) -> int:
        """scheduler의 slot 좌표 환산을 그대로 노출한다(연결 layer가 slot 단위를 직접 정하지 않게).

        scheduler가 이 hook을 제공하지 않으면 `None`을 돌려주고, 호출자는 자체 fallback을 쓴다.
        """

        convert = getattr(self._scheduler, "slot_for_hw_ns", None)
        return None if convert is None else int(convert(hw_timestamp_ns))

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

    def build_evidence(self, parents, scene, *, broadcast_results=None, scenario_result=None, poc_result=None):
        """확정 outcome을 자동 반영해 evidence를 조립한다(PENDING 회귀 방지).

        `evidence.build_evidence`에 `outcomes=self.outcomes_by_parent()`를 주입하므로 호출자가
        outcomes 인자를 잊어도 확정된 terminal이 PENDING으로 되돌아가지 않는다.
        """

        from . import evidence as _evidence

        parents = tuple(parents)
        if broadcast_results is None:
            parent_ids = {parent.parent_id for parent in parents}
            broadcast_results = tuple(
                cached.broadcast_result for cached in self._cache.values()
                if cached.broadcast_result.parent.parent_id in parent_ids
            )
        return _evidence.build_evidence(
            parents, scene, broadcast_results=broadcast_results,
            readiness_by_parent={
                parent.parent_id: proof for parent in parents
                if (proof := self._broadcast.readiness_of(parent.parent_id))
            },
            outcomes=self.outcomes_by_parent(), unresolved_ownership=self.unresolved_ownership(),
            scenario_result=scenario_result, poc_result=poc_result,
        )

    def _abort_attempt(self, request_id: str) -> None:
        """RF 방사 전/구조 실패: 예약을 반환하고 correlation 시도 기록을 지운다."""

        try:
            self._release_reservation(request_id)
        finally:
            self._parents.pop(request_id, None)
        self._raise_if_ownership_uncertain()

    def _release_reservation(self, request_id: str) -> bool:
        """예약 반환 실패는 recovery key로 보존한다. 일반 예외는 False, 인터럽트는 재전달한다."""

        key = self._reservations.pop(request_id, None)
        if self._arbiter is not None and key is not None:
            try:
                self._arbiter.release(resource_key=key, owner="POC")
            except BaseException as error:  # preserve ownership even during interruption
                self._unresolved_ownership.add(key)  # 실패 식별자 보존(evidence)
                if not isinstance(error, Exception):
                    raise
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
        """PoC 자원 예약을 release하고 fan-out group·cache·correlation·trigger 잔존을 0으로 정리한다.

        - 추적 중인 예약 + 과거 미반환(unresolved) 식별자를 모두 release **재시도**한다(복구 시도).
          release가 성공한 식별자는 unresolved에서 제거하고, 여전히 실패하면 unresolved로 남긴다.
        - 개별 release/정리 실패가 나머지 정리를 중단시키지 않는다(tracking 잔존 0 보장).
        - unresolved ownership이 남아 있는 동안에는 **매 cleanup마다** `POC_RADIO_OWNERSHIP_UNCERTAIN`
          (SESSION_INHIBIT)으로 승격한다(거짓 성공 반환 금지). 전부 회수돼야 조용히 성공한다.
        """

        # 재시도 대상 = 현재 추적 예약 ∪ 과거 미반환 식별자.
        retry_keys = set(self._reservations.values()) | set(self._unresolved_ownership)
        self._unresolved_ownership.update(retry_keys)
        interrupt = None
        if self._arbiter is not None:
            for key in sorted(retry_keys):
                try:
                    self._arbiter.release(resource_key=key, owner="POC")
                    self._unresolved_ownership.discard(key)
                except BaseException as error:
                    if not isinstance(error, Exception) and interrupt is None:
                        interrupt = error
        self._reservations.clear()
        # Retry each component with its actual owner; an arbiter cannot clear a trigger failure.
        for marker, operation in (
            ("__broadcast_cleanup_failed__", self._broadcast.cleanup),
            ("__trigger_cleanup_failed__", self._clear_scheduler_triggers),
        ):
            try:
                operation()
                self._cleanup_failures.discard(marker)
            except BaseException as error:
                self._cleanup_failures.add(marker)
                if not isinstance(error, Exception) and interrupt is None:
                    interrupt = error
        self._cache.clear()
        self._parents.clear()
        self._outcomes.clear()
        if interrupt is not None:
            raise interrupt
        self._raise_if_ownership_uncertain()

    def _clear_scheduler_triggers(self) -> None:
        clear = getattr(self._scheduler, "clear_triggers", None)
        if clear is not None:
            clear()

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
