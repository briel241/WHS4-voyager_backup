"""Stage 7 B210 endpoint 조립 (`stage7` §15).

frozen `runtime/wiring.py`와 그 `build_wideband_group()` software-only
shared-medium 검증을 바꾸지 않는다. 이 module에만 Stage 7 B210용
`build_b210_endpoint()`와 resolved identity validator를 가산한다. 기존
builder를 monkey patch, 재등록 또는 이름 shadowing하지 않는다.
"""

from __future__ import annotations

from threading import RLock

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..config.models import LoadedConfiguration, fingerprint_of, radio_fingerprint_of
from ..link.data_plane import LinkDataPlane
from ..mac.deployment import build_canonical_deployment_profile
from ..mac.interface import Stage4Mac
from ..mac.models import MacRuntimeProfile, MacStateRepository
from ..mac.coordinator import MacCoordinator
from ..mac.tbb import (
    WIDEBAND_LOWER_CHANNEL,
    WIDEBAND_UPPER_CHANNEL,
    build_wideband_canonical_tbb_profile,
)
from ..presentation.adapter import build_presentation_adapter
from ..runtime.mac_commands import EndpointHealthLog, MacCommandOutbox, MacIngressQueue
from ..runtime.wiring import (
    InternalEventBus,
    _validate_type4_capacity,
    build_registries,
    build_wideband_profile,
    preflight_tbb_certificate,
    EndpointDependencies,
    STAGE5_MAX_APPLICATION_BYTES,
    STAGE5_MAX_CONCURRENT_TRANSFERS,
)
from ..service.models import Stage5ServicePolicy
from .configuration import ResolvedConfiguration
from .endpoint import Endpoint, PublicStartGate
from .errors import PrivateErrorCode
from .phy_facade import (
    DiagnosticPhyFacade,
    LaneRxIngress,
    PhyDiagnosticSink,
)
from .radio_adapter import LaneB210RadioAdapter, HostBridgeHandle
from .routing import RouteAwareLinkPort, RouteSidecar
from .support import LaneRxDispatcher, LaneRxStack, LaneStage5Support

#: frozen `_radio_schedule_horizon` 공식과 동일: 구조 검증용 canonical timing의
#: `mapping_valid_for_nanoseconds(1e9)`에서 한 hexslot 여유를 뺀 값. §16.1의
#: 120초 active command horizon은 bridge 유효성 경계이며 frozen MAC의 planning
#: horizon이 아니다 (Stage 5/6 검증 envelope 보존).
SCHEDULE_HORIZON_NS = 1_000_000_000 - 160_000_000
LEAD_NS = 100_000_000


def _config_error(message: str, **details):
    return internal_error(
        Layer.RUNTIME,
        ErrorCode.CONFIG_SEMANTIC_INVALID,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


class RestartFactory:
    """§15 restart seam: frozen reset 뒤 lane graph를 새 generation으로 재조립한다.

    factory는 hardware configure/start, qualification lease 재사용, worker spawn
    또는 public lifecycle event를 수행하지 않는다.
    """

    __slots__ = ("_roc_station_id", "call_count", "_lock")

    def __init__(self, *, roc_station_id: int) -> None:
        self._roc_station_id = roc_station_id
        self.call_count = 0
        self._lock = RLock()

    def rebuild(self, endpoint: Endpoint) -> None:
        with self._lock:
            self.call_count += 1
            resolved = endpoint._b210_resolved
            dependencies = endpoint.dependencies
            support = endpoint._stage4
            if resolved is None or support is None:
                raise RuntimeError("stage7 graph binding이 없습니다")
            old_graph = endpoint._b210_graph or {}
            old_sidecar = old_graph.get("sidecar")
            new_lane_generation = (
                old_sidecar.lane_generation + 1 if old_sidecar is not None else 1
            )
            if old_sidecar is not None:
                # §15.1.3 순서로 이전 generation ledger를 finalize한다.
                old_sidecar.restart(new_lane_generation)
                sidecar = old_sidecar
            else:
                sidecar = RouteSidecar(lane_generation=new_lane_generation)
            # frozen reset이 만든 임시 일반 Link/PHY를 public 관측 전에 폐기하고
            # 완전한 새 lane graph로 교체한다.
            graph = _assemble_lane_graph(
                endpoint,
                dependencies,
                resolved,
                sidecar,
                lane_generation=new_lane_generation,
                roc_station_id=self._roc_station_id,
            )
            radio = dependencies.radio
            if isinstance(radio, LaneB210RadioAdapter):
                radio.rebind_lane_runtime(new_lane_generation, graph["dispatcher"])


def _assemble_lane_graph(
    endpoint: Endpoint,
    dependencies: EndpointDependencies,
    resolved: ResolvedConfiguration,
    sidecar: RouteSidecar,
    *,
    lane_generation: int,
    roc_station_id: int,
    crypto_dependencies=None,
) -> dict:
    """wrapper·두 lane stack·dispatcher를 조립해 endpoint에 atomic 결속한다."""

    delegate = LinkDataPlane(
        dependencies.id_source,
        dependencies.registries.message_codec,
        dependencies.registries.link_profile,
        crypto_dependencies=crypto_dependencies
        if crypto_dependencies is not None
        else endpoint._crypto_dependencies,
    )
    wrapper = RouteAwareLinkPort(delegate, sidecar, roc_station_id=roc_station_id)
    radio_ref = dependencies.radio

    def hardware_clock_id():
        current = getattr(radio_ref, "hardware_clock_id", None)
        if current is not None:
            return current
        return (
            f"stage7-b210-hardware-v1:{resolved.run_id}:{resolved.endpoint_id}:"
            f"{lane_generation}"
        )
    stacks = []
    for lane_index in (0, 1):
        sink = PhyDiagnosticSink(
            lane_index=lane_index, lane_generation=lane_generation
        )
        facade = DiagnosticPhyFacade(
            dependencies.registries.phy_profile,
            dependencies.time_source,
            dependencies.id_source,
            diagnostic_sink=sink,
            route_sidecar=sidecar if lane_index == 0 else None,
        )
        ingress = LaneRxIngress(
            run_id=resolved.run_id,
            endpoint_id=resolved.endpoint_id,
            b210_serial=resolved.b210_serial,
            lane_index=lane_index,
            lane_generation=lane_generation,
            radio_generation=lane_generation,
            expected_hardware_clock_id=hardware_clock_id,
        )
        stacks.append(
            LaneRxStack(
                lane_index=lane_index,
                facade=facade,
                ingress=ingress,
                lane_generation=lane_generation,
            )
        )
    support = endpoint._stage4
    dispatcher = LaneRxDispatcher(support, stacks, lane_generation=lane_generation)
    support.bind_lane_runtime(dispatcher)
    endpoint._link = wrapper
    endpoint._phy = stacks[0].facade
    endpoint._phy_radio_bridge = stacks[0].bridge
    graph = {
        "sidecar": sidecar,
        "wrapper": wrapper,
        "rf_a_stack": stacks[0],
        "rf_b_stack": stacks[1],
        "dispatcher": dispatcher,
        "lane_generation": lane_generation,
        "hardware_clock_id": hardware_clock_id,
    }
    endpoint._b210_graph = graph
    return graph


def build_b210_endpoint(
    resolved: ResolvedConfiguration,
    deployment,
    *,
    e2e_security,
    crypto_dependencies=None,
    readiness_utc=None,
    policy=None,
    application_sink=None,
    worker_factory=None,
    intent_notifier=None,
    host_raw_clock=None,
    pairwise_edge=None,
    monitor_enabled=True,
    time_source_override=None,
    id_source_override=None,
    random_source_override=None,
):
    """process-local `VdeService`를 구현하는 `Endpoint`를 반환한다 (§15).

    TEST harness와 정상 application 조립은 이 함수가 반환한 endpoint에 서로 다른
    sink/handler를 주입한다. Coordinator/group wrapper가 아니다.
    """

    if not isinstance(resolved, ResolvedConfiguration):
        return _config_error("ResolvedConfiguration이 필요합니다")
    from ..security.dependencies import E2eSecurityDependencies
    if not isinstance(e2e_security, E2eSecurityDependencies):
        return _config_error("명시적 E2E security dependency가 필요합니다")
    endpoint_config = resolved.endpoint
    if endpoint_config.service.max_payload_bytes != STAGE5_MAX_APPLICATION_BYTES:
        return _config_error("max_payload_bytes는 65,531이어야 합니다")
    if endpoint_config.service.max_concurrent_transfers != STAGE5_MAX_CONCURRENT_TRANSFERS:
        return _config_error("max_concurrent_transfers는 8이어야 합니다")

    loaded = LoadedConfiguration(
        endpoint=endpoint_config,
        radio=resolved.radio,
        endpoint_fingerprint=fingerprint_of(endpoint_config),
        radio_fingerprint=radio_fingerprint_of(resolved.radio, 3),
        source_path=resolved.source_path,
    )
    from ..common.events import RecordingServiceEventSink

    gate = PublicStartGate(application_sink or RecordingServiceEventSink())

    # Link 11·17·19 운용 profile은 OPERATIONAL registry에 등록된다. Stage 4~6과
    # 같은 조립 패턴으로 combined three-entry phy registry를 사용한다.
    from ..profiles.models import RegistryScope

    class _RegistrySelection:
        class endpoint:
            scope = RegistryScope.OPERATIONAL
            code_version = endpoint_config.code_version

    registries = build_registries(_RegistrySelection(), combined_phy=True)
    if registries.is_err:
        return registries
    if endpoint_config.scope is not RegistryScope.OPERATIONAL:
        # payload format catalogue는 endpoint scope를 따른다 (TEST fixture 포함).
        from dataclasses import replace as _replace

        from ..presentation.registry import build_payload_format_registry
        from ..profiles.payload_formats import STAGE5_PAYLOAD_CONTENT_VERSION

        payload_registry = build_payload_format_registry(
            endpoint_config.scope, STAGE5_PAYLOAD_CONTENT_VERSION
        )
        if payload_registry.is_err:
            return payload_registry
        frozen = payload_registry.value.freeze()
        if frozen.is_err:
            return frozen
        registries = Ok(_replace(registries.value, payload_format=payload_registry.value))
    registry_fingerprint = registries.value.phy_profile.fingerprint().fingerprint
    if registry_fingerprint != resolved.registry_fingerprint:
        return _config_error(
            "combined registry fingerprint가 resolved identity와 다릅니다",
            fingerprint=registry_fingerprint,
        )

    # frozen `build_radio_adapter()`는 schema v4/`b210-stage7-affine`을 절대
    # 처리하지 않는다 (§15). dependencies는 frozen source helper로 직접 조립한다.
    bridge_handle = HostBridgeHandle()
    sidecar = RouteSidecar(lane_generation=0)

    from ..common.events import RecordingInternalEventSink
    from ..runtime.wiring import (
        build_id_source,
        build_random_source,
        build_time_source,
    )

    internal_sink = InternalEventBus(RecordingInternalEventSink())
    time_source = time_source_override or build_time_source(loaded).value
    random_source = random_source_override or build_random_source(loaded).value
    id_source = id_source_override or build_id_source(loaded).value
    radio = LaneB210RadioAdapter(
        time_source,
        id_source,
        internal_sink,
        resolved=resolved,
        sidecar=sidecar,
        worker_factory=worker_factory,
        bridge=bridge_handle,
        pairwise_edge=pairwise_edge,
        monitor_enabled=monitor_enabled,
    )
    dependencies = EndpointDependencies(
        configuration=loaded,
        time_source=time_source,
        random_source=random_source,
        id_source=id_source,
        radio=radio,
        registries=registries.value,
        internal_event_sink=internal_sink,
        service_event_sink=gate,
    )

    wideband = build_wideband_profile(dependencies, deployment)
    if wideband.is_err:
        return wideband
    wideband_profile = wideband.value
    if wideband_profile.data_link_id != resolved.data_link_id:
        return _config_error("resolved data_link_id가 wideband profile과 다릅니다")

    from ..common.contracts.service import ServiceRole

    is_control = endpoint_config.role is ServiceRole.CONTROL
    signer = None if crypto_dependencies is None else crypto_dependencies.tbb_signer
    trust_store = (
        None if crypto_dependencies is None else crypto_dependencies.tbb_trust_store
    )
    preflight = preflight_tbb_certificate(
        readiness_utc=readiness_utc or dependencies.time_source.now_utc(),
        signer=signer,
        trust_store=trust_store,
        expected_fingerprint=deployment.expected_tbb_certificate_fingerprint,
        is_control=is_control,
    )
    if preflight.is_err:
        return preflight

    canonical_tbb = build_wideband_canonical_tbb_profile(deployment)
    capacity = _validate_type4_capacity(dependencies.registries.link_profile)
    if capacity.is_err:
        return capacity

    endpoint = Endpoint(dependencies, policy or Stage5ServicePolicy(), resolved=resolved)
    adapter = build_presentation_adapter(dependencies.registries.payload_format,
                                         e2e_security=e2e_security)
    if adapter.is_err:
        return adapter
    endpoint._presentation = adapter.value
    endpoint._crypto_dependencies = crypto_dependencies
    endpoint._wideband_profile = wideband_profile

    profile = MacRuntimeProfile(
        canonical_tbb=canonical_tbb,
        deployment_fingerprint=wideband_profile.wideband_deployment_fingerprint,
        local_station_id=endpoint_config.station_id,
        is_control=is_control,
        # 계획 lead 300ms: 어댑터 물리 하한 100ms 위 200ms 여유 (svcdbg80
        # 확증 — 상수 100ms는 하한과 여유 0이라 SLOT_MISSED 부류의 뿌리.
        # TOML transmit_lead는 이 경로에 흐르지 않았음). 수신 lead 20ms:
        # 연속 RX 창은 등록만 하면 되므로 RPC 지연+여유로 충분.
        transmit_lead_nanoseconds=(
            # MASS-B(USB2) 전용 상향: BUILD 해제지연 실측(svcdbg83/85)
            # — B는 BBSC 계획이 없어 밴드 부작용 무관.
            450_000_000 if resolved.endpoint_id == "MASS-B" else 300_000_000
        ),
        receive_lead_nanoseconds=20_000_000,
        channel_switch_gap_nanoseconds=LEAD_NS,
        sample_rate_hz=endpoint_config.phy.sample_rate_hz,
        schedule_horizon_nanoseconds=SCHEDULE_HORIZON_NS,
        data_link_id=wideband_profile.data_link_id,
        lower_channel=WIDEBAND_LOWER_CHANNEL,
        upper_channel=WIDEBAND_UPPER_CHANNEL,
    )
    repository = MacStateRepository()
    lock = RLock()
    health = EndpointHealthLog()
    outbox = MacCommandOutbox(dependencies.id_source, lock, health)
    ingress_queue = MacIngressQueue(lock, dependencies.time_source, dependencies.id_source)
    coordinator = MacCoordinator(
        profile=profile,
        deployment=deployment,
        time_source=dependencies.time_source,
        random_source=dependencies.random_source,
        id_source=dependencies.id_source,
        link_profile_registry=dependencies.registries.link_profile,
        phy_profile_registry=dependencies.registries.phy_profile,
        repository=repository,
    )
    mac = Stage4Mac(coordinator, repository, outbox, lock, dependencies.id_source)
    endpoint._mac = mac
    support = LaneStage5Support(endpoint, mac, outbox, ingress_queue, health, lock)
    endpoint.attach_stage4(support)

    roc_station_id = deployment.roc_station_id.value
    restart_factory = RestartFactory(roc_station_id=roc_station_id)
    graph = _assemble_lane_graph(
        endpoint,
        dependencies,
        resolved,
        sidecar,
        lane_generation=0,
        roc_station_id=roc_station_id,
        crypto_dependencies=crypto_dependencies,
    )
    radio.bind_lane_dispatcher(graph["dispatcher"])
    endpoint.bind_b210_system(
        start_gate=gate,
        graph=graph,
        restart_factory=restart_factory,
        intent_notifier=intent_notifier,
        host_raw_clock=host_raw_clock,
    )
    mismatch = endpoint._validate_effective_graph()
    if mismatch is not None:
        return mismatch
    return Ok(endpoint)


#: (호환) 문서화된 링크 팀 in-process seam 의 옛 이름. `docs/사용법.md`·
#: `docs/인수인계.md` 가 "공식 in-process 연결점"으로 이 이름을 안내해 왔으므로,
#: **같은 함수 객체**를 가리키는 최소 alias 로 유지한다(구현 복제 없음).
#: 제거는 날짜가 아니라 전환 확인·안내·별도 승인으로 결정한다.
build_stage7_b210_endpoint = build_b210_endpoint

__all__ = (
    "LEAD_NS",
    "SCHEDULE_HORIZON_NS",
    "RestartFactory",
    "build_b210_endpoint",
    "build_stage7_b210_endpoint",
)
