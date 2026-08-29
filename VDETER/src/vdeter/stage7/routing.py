"""Stage 7 TX route 결속 (`stage7_b210_system_baseline.md` §15, §15.1).

`Stage7RouteAwareLinkPort`는 frozen `LinkPort`의 아홉 method를 모두 구현하고
정확히 한 `LinkDataPlane` delegate를 소유한다. route-aware `build_packet`,
`encode_packet`, `encode_tbb`는 §15.1 extractor와 sidecar transaction을 적용한 뒤
delegate의 같은 method를 정확히 한 번 호출하고, 나머지 여섯 method는 request
object를 변경하지 않은 채 delegate에 정확히 한 번 전달한다.

frozen Runtime의 `link_command_id`는 wrapper method 인자가 아니므로 route key로
추측하거나 thread-local/stack inspection으로 가져오지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from ..common.addressing import Address, AddressKind
from ..common.contracts.link import (
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    TbbAssemblyControlRequest,
    TbbEncodeRequest,
)
from ..common.contracts.message import (
    BODY_TYPE_TO_MESSAGE_TYPE,
    MediaAccessControlBody,
    PaddingBody,
    ResourceRequestBody,
)
from ..common.contracts.phy import PhyEncodeRequest
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import SerializationError
from ..common.identifiers import DeterministicIdSource, LinkId, PhysicalChannel
from ..common.results import Err, Ok, internal_error
from ..common.serialization import to_reference_json
from ..common.time import ClockDomain, TimePoint
from .canonical_json import encode_canonical_json, sha256_hex
from .configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME
from .errors import Stage7PrivateErrorCode

MAX_U63 = (1 << 63) - 1

#: §7 canonical station ↔ endpoint 표.
STATION_TO_ENDPOINT = {
    111_111_111: "MASS-A",
    333_333_333: "MASS-B",
    222_222_222: "ROC",
}
ENDPOINT_TO_STATION = {value: key for key, value in STATION_TO_ENDPOINT.items()}

#: §15.1 canonical mapping.
#: key는 (tx_endpoint, destination_endpoint | "BROADCAST"),
#: value는 (channel, ((lane_index, path_id), ...)).
CANONICAL_ROUTE_TABLE = {
    ("ROC", "MASS-A"): (UPPER_CHANNEL_NAME, ((0, "C1"),)),
    ("ROC", "MASS-B"): (UPPER_CHANNEL_NAME, ((1, "C3"),)),
    ("ROC", "BROADCAST"): (UPPER_CHANNEL_NAME, ((0, "C1"), (1, "C3"))),
    ("MASS-A", "ROC"): (LOWER_CHANNEL_NAME, ((0, "C2"),)),
    ("MASS-B", "ROC"): (LOWER_CHANNEL_NAME, ((0, "C4"),)),
    ("MASS-A", "MASS-B"): (UPPER_CHANNEL_NAME, ((1, "C5"),)),
    ("MASS-B", "MASS-A"): (UPPER_CHANNEL_NAME, ((1, "C6"),)),
}

#: §17.1 P1 signed single-child subset: qualification_edge → 유지되는 ROC child.
PAIRWISE_EDGE_CHILD = {
    "MASS-A": (0, "C1"),
    "MASS-B": (1, "C3"),
}

_ROUTE_BEARING_GENERAL_TYPES = frozenset({0, 4, 13, 90})


def _private_error(private_code: Stage7PrivateErrorCode, message: str, **details):
    """§15.2: private code를 기존 internal `ErrorCode`로 한 번 투영한다."""

    return internal_error(
        Layer.RUNTIME,
        ErrorCode.INTERNAL_ERROR,
        ErrorCategory.INTERNAL,
        message,
        details=Details.of(stage7_private_code=private_code.value, **details),
    )


@dataclass(frozen=True, slots=True)
class Stage7RouteContext:
    """§15.1 sidecar가 보존하는 immutable route context."""

    method_kind: str
    source_station_id: int
    destination_kind: str  # "ADDRESSED" | "BROADCAST"
    destination_station_id: int | None
    original_source_station_id: int | None
    original_destination_kind: str | None
    original_destination_station_id: int | None
    request_digest: str

    def destination_key(self):
        return (self.destination_kind, self.destination_station_id)


@dataclass(frozen=True, slots=True)
class LaneExecutionPlan:
    """Burst→물리 lane 배치 계획. `Stage7B210RadioAdapter`가 소비한다."""

    burst_id_value: str
    lane_generation: int
    channel_name: str
    children: tuple  # ((lane_index, path_id), ...)
    skipped: tuple  # ((lane_index, path_id, "SKIPPED_ABSENT_BY_PLAN"), ...)
    route: Stage7RouteContext
    fanout_scope: str  # "SINGLE_CHILD" | "TWO_CHILD_BROADCAST" | "PAIRWISE_SINGLE_CHILD"


def _address_key(address: Address):
    if address.kind is AddressKind.BROADCAST:
        return ("BROADCAST", None)
    return ("ADDRESSED", address.station_id.value)


def canonical_request_digest(request) -> str | None:
    """frozen `to_reference_json` 결과의 §16 canonical bytes SHA-256 (실패 시 None)."""

    try:
        reference = to_reference_json(request)
        return sha256_hex(encode_canonical_json(_jsonable(reference)))
    except (SerializationError, ValueError, TypeError):
        return None


def _jsonable(value):
    """reference JSON 값을 canonical encoder 입력으로 정규화한다."""

    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def extract_build_route(request: LinkBuildRequest):
    """`LinkBuildRequest.plan.route`가 authoritative source다 (§15.1 표)."""

    route = request.plan.route
    has_node_source = route.node_source is not None
    has_node_destination = route.node_destination is not None
    if has_node_source and has_node_destination:
        source = route.node_source.station_id.value
        destination = _address_key(route.node_destination.address)
    elif not has_node_source and not has_node_destination:
        source = route.original_source.station_id.value
        destination = _address_key(route.original_destination.address)
    else:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_AMBIGUOUS,
            "route의 node field가 한쪽만 있습니다",
        )
    return (
        {
            "source": source,
            "destination": destination,
            "original_source": route.original_source.station_id.value,
            "original_destination": _address_key(route.original_destination.address),
        },
        None,
    )


def extract_general_encode_route(request: LinkPacketEncodeRequest, *, roc_station_id: int):
    """Type 0·4·13·90 route 추출 (§15.1 표). padding은 route-neutral이다."""

    extracted = []
    for message in request.messages:
        body = message.body
        message_type = BODY_TYPE_TO_MESSAGE_TYPE[type(body)]
        if isinstance(body, PaddingBody):
            continue
        if message_type not in _ROUTE_BEARING_GENERAL_TYPES:
            # frozen Link가 build-path 오류로 거부할 입력이다. wrapper는 route를
            # 추측하지 않고 delegate 오류를 그대로 반환하도록 통과시킨다.
            return None, None
        if isinstance(body, MediaAccessControlBody):
            if body.source_id.value != roc_station_id:
                return None, _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                    "Stage 7 Type 0은 ROC source만 허용합니다",
                    source_station_id=body.source_id.value,
                )
            extracted.append(
                {
                    "source": body.source_id.value,
                    "destination": ("BROADCAST", None),
                    "original_source": None,
                    "original_destination": None,
                }
            )
        elif isinstance(body, ResourceRequestBody):
            extracted.append(
                {
                    "source": body.node_source_id.value,
                    "destination": _address_key(body.node_destination),
                    "original_source": body.original_source_id.value,
                    "original_destination": _address_key(body.original_destination),
                }
            )
        else:  # Type 4 / Type 13
            extracted.append(
                {
                    "source": body.source_id.value,
                    "destination": _address_key(body.destination),
                    "original_source": None,
                    "original_destination": None,
                }
            )
    if not extracted:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_CONTEXT_MISSING,
            "route-bearing Message가 없습니다",
        )
    # §15.1: effective source·destination kind/value가 하나라도 다르면 ambiguous.
    # Type 4·13은 original 의미가 없는 body이므로(표의 `source_id → destination`)
    # original을 주장하는 Message(Type 90)와의 혼합은 충돌이 아니다. original은
    # 그것을 실제로 가진 Message들 사이에서만 exact 일치해야 한다 (frozen ROC
    # ASC 경로는 [Type 90, Type 4]를 한 encode로 묶는다).
    first = extracted[0]
    for candidate in extracted[1:]:
        if (
            candidate["source"] != first["source"]
            or candidate["destination"] != first["destination"]
        ):
            return None, _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_AMBIGUOUS,
                "한 encode request에 서로 다른 route가 섞여 있습니다",
            )
    original_claims = {
        (entry["original_source"], entry["original_destination"])
        for entry in extracted
        if entry["original_source"] is not None
        or entry["original_destination"] is not None
    }
    if len(original_claims) > 1:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_AMBIGUOUS,
            "한 encode request에 서로 다른 original route가 섞여 있습니다",
        )
    original_source, original_destination = (
        next(iter(original_claims)) if original_claims else (None, None)
    )
    return (
        {
            "source": first["source"],
            "destination": first["destination"],
            "original_source": original_source,
            "original_destination": original_destination,
        },
        None,
    )


def resolve_lane_plan_route(
    endpoint_id: str,
    context: Stage7RouteContext,
    channel_name: str,
    *,
    pairwise_edge: str | None = None,
):
    """§15.1 canonical mapping으로 (children, skipped, fanout_scope)를 결정한다."""

    source_endpoint = STATION_TO_ENDPOINT.get(context.source_station_id)
    if source_endpoint != endpoint_id:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
            "route source가 local endpoint와 다릅니다",
            source_station_id=context.source_station_id,
            endpoint_id=endpoint_id,
        )
    if context.destination_kind == "BROADCAST":
        destination_endpoint = "BROADCAST"
    else:
        destination_endpoint = STATION_TO_ENDPOINT.get(context.destination_station_id)
        if destination_endpoint is None:
            return None, _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "destination station이 canonical member가 아닙니다",
                destination_station_id=context.destination_station_id,
            )
    entry = CANONICAL_ROUTE_TABLE.get((endpoint_id, destination_endpoint))
    if entry is None:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
            "canonical route 표에 없는 조합입니다",
            endpoint_id=endpoint_id,
            destination=str(destination_endpoint),
        )
    expected_channel, children = entry
    if channel_name != expected_channel:
        return None, _private_error(
            Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
            "logical channel이 canonical mapping과 다릅니다",
            channel=channel_name,
            expected_channel=expected_channel,
        )
    skipped = ()
    fanout_scope = "SINGLE_CHILD" if len(children) == 1 else "TWO_CHILD_BROADCAST"
    if destination_endpoint == "BROADCAST" and pairwise_edge is not None:
        kept = PAIRWISE_EDGE_CHILD.get(pairwise_edge)
        if kept is None:
            return None, _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "signed qualification edge가 잘못되었습니다",
                qualification_edge=str(pairwise_edge),
            )
        skipped = tuple(
            (lane, path, "SKIPPED_ABSENT_BY_PLAN")
            for lane, path in children
            if (lane, path) != kept
        )
        children = (kept,)
        fanout_scope = "PAIRWISE_SINGLE_CHILD"
    return (children, skipped, fanout_scope), None


class _PendingEntry:
    __slots__ = ("ordinal", "method_kind", "request_digest", "route")

    def __init__(self, ordinal, method_kind, request_digest, route) -> None:
        self.ordinal = ordinal
        self.method_kind = method_kind
        self.request_digest = request_digest
        self.route = route


class _BurstReservation:
    __slots__ = ("state", "plan", "terminal")

    def __init__(self, plan) -> None:
        self.state = "RESERVED"
        self.plan = plan
        self.terminal = None


class Stage7RouteSidecar:
    """route-call→Packet→Burst→reservation ledger (§15.1)."""

    __slots__ = (
        "_lock",
        "_lane_generation",
        "_next_ordinal",
        "_pending",
        "_packets",
        "_bursts",
        "_reservations",
        "_evidence",
    )

    def __init__(self, *, lane_generation: int = 0) -> None:
        self._lock = RLock()
        self._lane_generation = lane_generation
        self._next_ordinal = 0
        self._pending = {}
        self._packets = {}
        self._bursts = {}
        self._reservations = {}
        self._evidence = []

    @property
    def lane_generation(self) -> int:
        return self._lane_generation

    def evidence(self) -> tuple:
        with self._lock:
            return tuple(self._evidence)

    def _record(self, kind: str, **fields) -> None:
        self._evidence.append(
            {"kind": kind, "lane_generation": self._lane_generation, **fields}
        )

    # --- pending route-call --------------------------------------------------

    def begin_pending(self, method_kind: str, request_digest: str, route: dict):
        with self._lock:
            if self._next_ordinal > MAX_U63:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                    "route_call_ordinal이 u63를 초과했습니다",
                )
            ordinal = self._next_ordinal
            self._next_ordinal += 1
            entry = _PendingEntry(ordinal, method_kind, request_digest, route)
            self._pending[(self._lane_generation, ordinal)] = entry
            return Ok(entry)

    def abort_pending(self, entry: _PendingEntry, reason: str) -> None:
        with self._lock:
            removed = self._pending.pop((self._lane_generation, entry.ordinal), None)
            if removed is not None:
                self._record(
                    "route_call_aborted",
                    ordinal=entry.ordinal,
                    reason=reason,
                    request_digest=entry.request_digest,
                )

    def promote_packets(self, entry: _PendingEntry, packet_ids, *, link_id: int):
        """delegate 성공 시 한 lock transaction에서 모든 Packet ID를 atomic promote한다."""

        with self._lock:
            key = (self._lane_generation, entry.ordinal)
            if key not in self._pending:
                return _private_error(
                    Stage7PrivateErrorCode.STALE_LANE_EVENT,
                    "pending route-call이 이미 폐기되었습니다",
                    ordinal=entry.ordinal,
                )
            for packet_id in packet_ids:
                if (self._lane_generation, packet_id.value) in self._packets:
                    self._record(
                        "route_call_collision",
                        ordinal=entry.ordinal,
                        colliding_id=packet_id.value,
                    )
                    self._pending.pop(key, None)
                    return _private_error(
                        Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                        "Packet ID가 이미 다른 route에 결속되어 있습니다",
                        local_id=packet_id.value,
                    )
            context = Stage7RouteContext(
                method_kind=entry.method_kind,
                source_station_id=entry.route["source"],
                destination_kind=entry.route["destination"][0],
                destination_station_id=entry.route["destination"][1],
                original_source_station_id=entry.route.get("original_source"),
                original_destination_kind=(
                    entry.route["original_destination"][0]
                    if entry.route.get("original_destination")
                    else None
                ),
                original_destination_station_id=(
                    entry.route["original_destination"][1]
                    if entry.route.get("original_destination")
                    else None
                ),
                request_digest=entry.request_digest,
            )
            for packet_id in packet_ids:
                self._packets[(self._lane_generation, packet_id.value)] = context
            self._pending.pop(key, None)
            self._record(
                "route_call_promoted",
                ordinal=entry.ordinal,
                promoted_ids=tuple(item.value for item in packet_ids),
                link_id=link_id,
                request_digest=entry.request_digest,
            )
            return Ok(context)

    # --- Packet → Burst ------------------------------------------------------

    def lookup_packet(self, packet_id_value: str):
        with self._lock:
            context = self._packets.get((self._lane_generation, packet_id_value))
            if context is None:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_CONTEXT_MISSING,
                    "Packet route entry가 없습니다",
                    local_id=packet_id_value,
                )
            return Ok(context)

    def bind_burst(self, packet_id_value: str, burst_id_value: str):
        with self._lock:
            context = self._packets.get((self._lane_generation, packet_id_value))
            if context is None:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_CONTEXT_MISSING,
                    "Packet route entry가 없어 Burst를 결속할 수 없습니다",
                    local_id=packet_id_value,
                )
            existing = self._bursts.get((self._lane_generation, burst_id_value))
            if existing is not None and existing is not context:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                    "Burst ID가 이미 다른 route에 결속되어 있습니다",
                    local_id=burst_id_value,
                )
            self._bursts[(self._lane_generation, burst_id_value)] = context
            self._record(
                "burst_bound", bound_packet=packet_id_value, bound_burst=burst_id_value
            )
            return Ok(context)

    def release_packet(self, packet_id_value: str) -> None:
        """transfer/Packet terminal에서 packet entry를 폐기한다."""

        with self._lock:
            if self._packets.pop((self._lane_generation, packet_id_value), None) is not None:
                self._record("packet_released", released_id=packet_id_value)

    # --- Burst reservation ---------------------------------------------------

    def reserve_burst(
        self,
        burst_id_value: str,
        endpoint_id: str,
        channel_name: str,
        *,
        pairwise_edge: str | None = None,
    ):
        """`RadioTxRequest.burst_id`로 entry를 reserve해 `LaneExecutionPlan`을 만든다."""

        with self._lock:
            context = self._bursts.get((self._lane_generation, burst_id_value))
            if context is None:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_CONTEXT_MISSING,
                    "Burst route entry가 없습니다",
                    local_id=burst_id_value,
                )
            reservation = self._reservations.get((self._lane_generation, burst_id_value))
            if reservation is not None:
                if reservation.state == "TERMINAL":
                    # exact replay는 cached terminal만 관측한다.
                    return Ok(reservation.plan)
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                    "Burst reservation이 이미 RESERVED 상태입니다",
                    local_id=burst_id_value,
                )
            resolved, error = resolve_lane_plan_route(
                endpoint_id, context, channel_name, pairwise_edge=pairwise_edge
            )
            if error is not None:
                return error
            children, skipped, fanout_scope = resolved
            plan = LaneExecutionPlan(
                burst_id_value=burst_id_value,
                lane_generation=self._lane_generation,
                channel_name=channel_name,
                children=children,
                skipped=skipped,
                route=context,
                fanout_scope=fanout_scope,
            )
            self._reservations[(self._lane_generation, burst_id_value)] = _BurstReservation(plan)
            self._record(
                "burst_reserved",
                reserved_burst=burst_id_value,
                children=children,
                skipped=skipped,
                fanout_scope=fanout_scope,
            )
            return Ok(plan)

    def terminal_burst(self, burst_id_value: str, outcome: str) -> None:
        with self._lock:
            reservation = self._reservations.get((self._lane_generation, burst_id_value))
            if reservation is not None and reservation.state != "TERMINAL":
                reservation.state = "TERMINAL"
                reservation.terminal = outcome
                self._record(
                    "burst_terminal", terminal_burst=burst_id_value, outcome=outcome
                )

    # --- 종료·재시작 (§15.1.3 순서) -------------------------------------------

    def restart(self, new_lane_generation: int) -> None:
        with self._lock:
            if new_lane_generation <= self._lane_generation:
                raise ValueError("lane_generation은 단조 증가해야 합니다")
            for _key, entry in tuple(self._pending.items()):
                self._record(
                    "route_call_finalized_on_restart",
                    ordinal=entry.ordinal,
                    request_digest=entry.request_digest,
                )
            self._pending.clear()
            for (_generation, packet_id), _context in tuple(self._packets.items()):
                self._record("packet_finalized_on_restart", released_id=packet_id)
            self._packets.clear()
            for (_generation, burst_id), reservation in tuple(self._reservations.items()):
                if reservation.state != "TERMINAL":
                    self._record("burst_cancelled_on_restart", cancelled_burst=burst_id)
            self._reservations.clear()
            self._bursts.clear()
            self._lane_generation = new_lane_generation
            self._next_ordinal = 0


class Stage7RouteAwareLinkPort:
    """frozen `LinkPort` 아홉 method wrapper (§15)."""

    __slots__ = ("_delegate", "_sidecar", "_roc_station_id", "delegate_calls")

    def __init__(self, delegate, sidecar: Stage7RouteSidecar, *, roc_station_id: int) -> None:
        self._delegate = delegate
        self._sidecar = sidecar
        self._roc_station_id = roc_station_id
        #: 시험 spy: method 이름 → delegate 호출 횟수.
        self.delegate_calls = {}

    @property
    def sidecar(self) -> Stage7RouteSidecar:
        return self._sidecar

    @property
    def delegate(self):
        return self._delegate

    def _count(self, name: str) -> None:
        self.delegate_calls[name] = self.delegate_calls.get(name, 0) + 1

    # --- 여섯 pass-through ---------------------------------------------------

    def analyze_transfer(self, request: LinkAnalyzeRequest):
        self._count("analyze_transfer")
        return self._delegate.analyze_transfer(request)

    def decode_packet(self, request: LinkDecodeRequest):
        self._count("decode_packet")
        return self._delegate.decode_packet(request)

    def process_fragment(self, request: LinkFragmentProcessRequest):
        self._count("process_fragment")
        return self._delegate.process_fragment(request)

    def control_reassembly(self, request: LinkReassemblyControlRequest):
        self._count("control_reassembly")
        return self._delegate.control_reassembly(request)

    def control_transfer(self, request: LinkTransferControlRequest):
        self._count("control_transfer")
        return self._delegate.control_transfer(request)

    def control_tbb_assembly(self, request: TbbAssemblyControlRequest):
        self._count("control_tbb_assembly")
        return self._delegate.control_tbb_assembly(request)

    # --- route-aware 세 method -----------------------------------------------

    def _route_call(self, method_kind: str, request, route, delegate_method, packet_ids_of):
        digest = canonical_request_digest(request)
        if digest is None:
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "reference serialization이 실패해 delegate를 호출하지 않습니다",
                method_kind=method_kind,
            )
        pending = self._sidecar.begin_pending(method_kind, digest, route)
        if pending.is_err:
            return pending
        entry = pending.value
        result = delegate_method(request)
        self._count(method_kind)
        if result.is_err:
            self._sidecar.abort_pending(entry, "delegate_error")
            return result
        promoted = self._sidecar.promote_packets(
            entry, packet_ids_of(result.value), link_id=self._link_id_of(result.value)
        )
        if promoted.is_err:
            # delegate success를 public 성공으로 노출하기 전의 sidecar 불변식
            # 실패만 Stage 7 pre-RF 오류로 닫는다.
            return promoted
        return result

    @staticmethod
    def _link_id_of(value) -> int:
        link_id = getattr(value, "link_id", None)
        if link_id is not None:
            return link_id.value
        packets = getattr(value, "packets", ())
        if packets:
            return packets[0].link_id.value
        return -1

    def build_packet(self, request: LinkBuildRequest):
        if not isinstance(request, LinkBuildRequest):
            self._count("build_packet")
            return self._delegate.build_packet(request)
        route, error = extract_build_route(request)
        if error is not None:
            return error
        return self._route_call(
            "build_packet",
            request,
            route,
            self._delegate.build_packet,
            lambda value: (value.packet_id,),
        )

    def encode_packet(self, request: LinkPacketEncodeRequest):
        if not isinstance(request, LinkPacketEncodeRequest):
            self._count("encode_packet")
            return self._delegate.encode_packet(request)
        route, error = extract_general_encode_route(
            request, roc_station_id=self._roc_station_id
        )
        if error is not None:
            return error
        if route is None:
            # wrong build path 입력: frozen Link의 build-path 오류를 그대로 반환한다.
            self._count("encode_packet")
            return self._delegate.encode_packet(request)
        return self._route_call(
            "encode_packet",
            request,
            route,
            self._delegate.encode_packet,
            lambda value: (value.packet_id,),
        )

    def encode_tbb(self, request: TbbEncodeRequest):
        if not isinstance(request, TbbEncodeRequest):
            self._count("encode_tbb")
            return self._delegate.encode_tbb(request)
        if request.source_id.value != self._roc_station_id:
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "TBB encode는 ROC source만 허용합니다",
                source_station_id=request.source_id.value,
            )
        route = {
            "source": request.source_id.value,
            "destination": ("BROADCAST", None),
            "original_source": None,
            "original_destination": None,
        }
        return self._route_call(
            "encode_tbb",
            request,
            route,
            self._delegate.encode_tbb,
            lambda value: tuple(packet.packet_id for packet in value.packets),
        )


# --- TEST-only raw-PHY qualification route seeder (§15) -----------------------


@dataclass(frozen=True, slots=True)
class Stage7QualificationPhyRequest:
    """signed qualification step에 결속된 private immutable raw-PHY 요청."""

    step_id: str
    source_endpoint_id: str
    destination_endpoint_id: str
    path_id: str
    link_id: int
    channel_name: str
    transmit_at: TimePoint
    packet: bytes


@dataclass(frozen=True, slots=True)
class Stage7SignedQualificationStep:
    """signed immutable qualification plan의 한 step (plan hash에 결속)."""

    step_id: str
    plan_sha256: str
    source_endpoint_id: str
    destination_endpoint_id: str
    path_id: str
    link_id: int
    channel_name: str
    packet_sha256: str
    run_mode: str  # GAIN_CALIBRATION | OPERATIONAL


class Stage7QualificationRouteSeeder:
    """TEST-only capability: Link call 없는 raw PHY/CFO/gain 자격 route를
    같은 sidecar ledger에 결속한다. production Service/MAC, P2/P3 normal data
    composition과 application에는 조립하지 않는다."""

    __slots__ = (
        "_sidecar",
        "_steps",
        "_id_source",
        "_endpoint_id",
        "_run_mode",
        "_expected_clock_id",
        "call_count",
    )

    def __init__(
        self,
        sidecar: Stage7RouteSidecar,
        signed_steps,
        *,
        endpoint_id: str,
        run_mode: str,
        deterministic_seed: int,
        expected_clock_id: str,
    ) -> None:
        if run_mode not in ("GAIN_CALIBRATION", "OPERATIONAL"):
            raise ValueError("seeder는 GAIN_CALIBRATION|OPERATIONAL에서만 조립할 수 있습니다")
        self._sidecar = sidecar
        self._steps = {step.step_id: step for step in signed_steps}
        self._id_source = DeterministicIdSource(deterministic_seed)
        self._endpoint_id = endpoint_id
        self._run_mode = run_mode
        self._expected_clock_id = expected_clock_id
        self.call_count = 0

    def seed(self, request: Stage7QualificationPhyRequest, phy_facade):
        step = self._steps.get(request.step_id)
        if step is None:
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "signed qualification step이 아닙니다",
                step_id=request.step_id,
            )
        if (
            step.source_endpoint_id != request.source_endpoint_id
            or step.destination_endpoint_id != request.destination_endpoint_id
            or step.path_id != request.path_id
            or step.link_id != request.link_id
            or step.channel_name != request.channel_name
            or step.run_mode != self._run_mode
            or step.source_endpoint_id != self._endpoint_id
        ):
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "qualification step field가 signed plan과 exact-equal이 아닙니다",
                step_id=request.step_id,
            )
        if sha256_hex(request.packet) != step.packet_sha256:
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "Packet bytes가 signed fixture digest와 다릅니다",
                step_id=request.step_id,
            )
        if (
            request.transmit_at.clock_domain is not ClockDomain.UTC
            or request.transmit_at.clock_id != self._expected_clock_id
        ):
            return _private_error(
                Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                "transmit_at이 현재 qualification active clock이 아닙니다",
                step_id=request.step_id,
            )
        destination_station = ENDPOINT_TO_STATION[step.destination_endpoint_id]
        route = {
            "source": ENDPOINT_TO_STATION[step.source_endpoint_id],
            "destination": ("ADDRESSED", destination_station),
            "original_source": None,
            "original_destination": None,
        }
        digest = sha256_hex(
            encode_canonical_json(
                {
                    "seed_type": "STAGE7_QUALIFICATION_ROUTE_V1",
                    "step_id": step.step_id,
                    "plan_sha256": step.plan_sha256,
                    "fixture_sha256": step.packet_sha256,
                }
            )
        )
        pending = self._sidecar.begin_pending("qualification_seed", digest, route)
        if pending.is_err:
            return pending
        packet_id = self._id_source.new_packet_id()
        promoted = self._sidecar.promote_packets(
            pending.value, (packet_id,), link_id=request.link_id
        )
        if promoted.is_err:
            return promoted
        phy_request = PhyEncodeRequest(
            packet_id=packet_id,
            link_id=LinkId(request.link_id),
            channel=PhysicalChannel(request.channel_name),
            transmit_at=request.transmit_at,
            packet=request.packet,
        )
        self.call_count += 1
        return phy_facade.encode_packet(phy_request)


__all__ = (
    "CANONICAL_ROUTE_TABLE",
    "ENDPOINT_TO_STATION",
    "LaneExecutionPlan",
    "PAIRWISE_EDGE_CHILD",
    "STATION_TO_ENDPOINT",
    "Stage7QualificationPhyRequest",
    "Stage7QualificationRouteSeeder",
    "Stage7RouteAwareLinkPort",
    "Stage7RouteContext",
    "Stage7RouteSidecar",
    "Stage7SignedQualificationStep",
    "canonical_request_digest",
    "extract_build_route",
    "extract_general_encode_route",
    "resolve_lane_plan_route",
)
