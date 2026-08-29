"""Stage 7 endpoint process launcher (`stage7` §14, §16).

Runtime Coordinator가 Pi에서 endpoint별로 spawn하는 process다. resolved
endpoint config를 적재하고 `Stage7Endpoint`+`Stage7B210RadioAdapter`를 조립한
뒤 `SOCK_SEQPACKET` socket에서 coordinator command를 처리한다.

TBB 신뢰자료는 `configs/stage7/trust/`의 provisioned TEST identity를 쓴다.
private key 내용은 어떤 log/evidence에도 기록하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (APP_ROOT, os.path.join(APP_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)


class _StaticTrustStore:
    """provisioned certificate 하나를 승인하는 trust store."""

    __slots__ = ("_certificates",)

    def __init__(self, certificates) -> None:
        self._certificates = tuple(certificates)

    def find_approved_certificates(self, fingerprint, received_at):
        from vdeter.common.results import Ok

        return Ok(self._certificates)


def _load_trust(root: str, *, is_control: bool):
    """(crypto_dependencies, expected_fingerprint_bytes)를 만든다."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate

    from vdeter.link.tbb.crypto import (
        LinkCryptoDependencies,
        TrustedTbbCertificate,
        certificate_fingerprint,
        create_cryptography_tbb_signer,
    )

    trust_dir = os.path.join(root, "configs", "stage7", "trust")
    with open(os.path.join(trust_dir, "tbb_trust_store.pem"), "rb") as handle:
        certificate = load_pem_x509_certificate(handle.read())
    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    fingerprint = certificate_fingerprint(certificate_der)
    if fingerprint.is_err:
        raise SystemExit(f"trust store fingerprint 계산 실패: {fingerprint.error}")
    signer = None
    if is_control:
        key_path = os.path.join(trust_dir, "roc_tbb_signer_key.pem")
        with open(key_path, "rb") as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
        private_der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        created = create_cryptography_tbb_signer(private_der, certificate_der)
        if created.is_err:
            raise SystemExit(f"TBB signer 생성 실패: {created.error}")
        signer = created.value
    store = _StaticTrustStore((TrustedTbbCertificate("stage7", certificate_der),))
    return (
        LinkCryptoDependencies(tbb_signer=signer, tbb_trust_store=store),
        fingerprint.value,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stage 7 endpoint launcher")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--config", required=True, help="resolved endpoint TOML 경로")
    parser.add_argument("--data-profile", required=True)
    parser.add_argument("--run-root", required=True, help="plans/evidence run 디렉터리")
    parser.add_argument("--lifecycle-generation", type=int, required=True)
    parser.add_argument("--coordinator-pid", type=int, required=True)
    parser.add_argument("--gain-artifact", default=None, help="OPERATIONAL 모드 gain artifact JSON")
    parser.add_argument(
        "--test-plan-sha", default=None,
        help="RR TEST run: §16.2 test adapter를 이 resolved test plan SHA로 활성화",
    )
    parser.add_argument(
        "--fixtures-dir", default=None,
        help="RR TEST run: adapter fixture 디렉터리 (relpath→bytes)",
    )
    arguments = parser.parse_args(argv)

    from vdeter.common.contracts.service import ServiceRole
    from vdeter.stage7.configuration import (
        Stage7ConfigurationLoader,
        Stage7OperationalGainInput,
    )
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns
    from vdeter.stage7.pi_runtime import Stage7PiOperations
    from vdeter.stage7.runtime_client import Stage7EndpointRuntimeClient
    from vdeter.stage7.transport import (
        SeqPacketServer,
        ensure_socket_root,
        runtime_socket_root,
    )
    from vdeter.stage7.wiring import build_stage7_b210_endpoint

    with open(arguments.config, "r", encoding="utf-8") as handle:
        config_text = handle.read()
    with open(arguments.data_profile, "r", encoding="utf-8") as handle:
        data_profile_text = handle.read()
    operational_gain = None
    if arguments.gain_artifact is not None:
        with open(arguments.gain_artifact, "rb") as handle:
            artifact = json.loads(handle.read().decode("utf-8"))
        common = artifact["endpoint_common_rx_gain_db"]
        # 세 endpoint 공통 gain은 endpoint별로 다르다 (§9.2 artifact).
        value = (
            common[arguments.endpoint_id] if isinstance(common, dict) else common
        )
        operational_gain = Stage7OperationalGainInput(
            endpoint_common_rx_gain_db=int(value),
            source_gain_artifact_sha256=artifact["source_gain_artifact_sha256"],
        )
    loaded = Stage7ConfigurationLoader().load_endpoint_configuration(
        config_text,
        data_profile_text,
        run_id=arguments.run_id,
        operational_gain=operational_gain,
        source_path=arguments.config,
    )
    if loaded.is_err:
        print(f"[launcher] config 적재 실패: {loaded.error}", file=sys.stderr)
        return 2
    resolved = loaded.value
    if resolved.endpoint_id != arguments.endpoint_id:
        print("[launcher] endpoint_id 불일치", file=sys.stderr)
        return 2

    is_control = resolved.endpoint.role is ServiceRole.CONTROL
    crypto, expected_fingerprint = _load_trust(APP_ROOT, is_control=is_control)

    from tests.support.stage4 import deployment_profile, tbb_fixture

    # TBB 유효창을 실제 UTC로 결속한다 — 시험 fixture 기본값(2000-epoch
    # 800,000,000s ≈ 2025-05, validity 60분)은 실물 UTC에서 만료 상태라
    # CONTROL start가 MAC_RESOURCE_UNAVAILABLE("활성 TBB 유효기간")로
    # 거부된다 (rr-p2-17 run k 실측 + 로컬 재현). 세 endpoint가 동일 값을
    # 독립 계산하도록 UTC 자정 기준으로 결정적으로 정하고(48h 유효),
    # 값은 아래 evidence 로그로 남긴다.
    import dataclasses as _dc
    import time as _time

    from vdeter.mac.tbb import TBB_START_TIME_EPOCH_SECONDS

    _now_2000 = int(_time.time()) - TBB_START_TIME_EPOCH_SECONDS // 1
    _midnight_2000 = (_now_2000 // 86_400) * 86_400
    fixture = _dc.replace(
        tbb_fixture(), start_time=_midnight_2000, validity=2_880
    )
    print(
        f"[launcher] TBB fixture start_time={_midnight_2000} validity=2880min",
        flush=True,
    )
    deployment = deployment_profile(
        expected_fingerprint=expected_fingerprint,
        fixture=fixture,
    )

    # intent notifier는 runtime client 생성 후 바인딩한다 (§16.1 event).
    notifier_slot = {"notifier": None}

    def intent_notifier(event_fields: dict) -> bool:
        notifier = notifier_slot["notifier"]
        if notifier is None:
            return False
        return notifier(event_fields)

    # anchored time source (rr-p2 svcdbg14 실측 근거): frozen MAC의 horizon
    # wakeup 마감은 (now_monotonic, now_utc) 순간쌍 산술이라, 두 시계를
    # 따로 읽으면 µs급 표집 편차로 같은 entry의 마감이 재계산마다 미세
    # 요동한다 → 같은 timer_id 교체 시도 → frozen 검증 거부가 반복되며
    # MAC batch가 정체한다(TBB 활성 불능의 근본 원인). UTC를 monotonic
    # 기준 고정 anchor 파생으로 만들어 두 시계를 lockstep으로 유지한다
    # (fake 하네스와 동일 성질; anchor 오차는 부팅 시각 NTP 정합 ±ms로
    # 슬롯 격자 2.4s 대비 무해).
    from vdeter.common.time import ClockDomain, SystemTimeSource, TimePoint

    class _AnchoredSystemTimeSource(SystemTimeSource):
        # 20ms 틱 양자화 + 고정 anchor (160ms는 TX lead 100ms를 갉아 어댑터의
        # lead 검사에서 방사 대부분이 거부됐다 — svcdbg19 실측 ACK 2/7): 한 틱 안의 utc/monotonic 읽기 쌍이
        # 항상 동일한 값을 보므로 (utc − mono) skew가 0이 된다 — fake
        # 하네스(drive 160ms 스텝)와 같은 성질. svcdbg17 실측: 비양자화
        # anchor는 두 읽기의 간극(수백 ns)이 그대로 skew로 남아 horizon
        # 마감 재산출이 38ns~16µs 이르게 나와 같은 id 교체 충돌이 계속됐다.
        __slots__ = ("_anchor_mono_ns", "_anchor_utc_ns")
        _QUANTUM_NS = 20_000_000

        def __init__(self):
            super().__init__()
            self._anchor_mono_ns = time.monotonic_ns()
            self._anchor_utc_ns = time.time_ns()

        def _pair_q(self):
            # 절대 UTC 20ms 격자에 위상 정렬: 세 endpoint가 같은 격자
            # 위상을 공유해야 슬롯 창과 버스트가 맞물린다 (svcdbg21 실측:
            # 자기-기준 격자는 국 간 ±20ms 위상차로 복원 0/방사 100).
            mono = time.monotonic_ns()
            utc_est = self._anchor_utc_ns + (mono - self._anchor_mono_ns)
            utc_q = utc_est - (utc_est % self._QUANTUM_NS)
            mono_q = self._anchor_mono_ns + (utc_q - self._anchor_utc_ns)
            return mono_q, utc_q

        def now_monotonic(self):
            mono_q, _ = self._pair_q()
            return TimePoint.from_nanoseconds(
                mono_q, ClockDomain.MONOTONIC, self._monotonic_clock_id
            )

        def now_utc(self):
            _, utc_q = self._pair_q()
            return TimePoint.from_nanoseconds(
                utc_q, ClockDomain.UTC, self._utc_clock_id
            )

    built = build_stage7_b210_endpoint(
        resolved,
        deployment,
        crypto_dependencies=crypto,
        intent_notifier=intent_notifier,
        host_raw_clock=read_monotonic_raw_ns,
        monitor_enabled=True,
        time_source_override=_AnchoredSystemTimeSource(),
    )
    if built.is_err:
        print(f"[launcher] endpoint 조립 실패: {built.error}", file=sys.stderr)
        return 2
    endpoint = built.value

    import hashlib as _hashlib
    import threading as _threading

    send_lock = _threading.Lock()
    progress_client_slot = {"client": None}

    def _progress_notifier(note: str) -> None:
        # `_send_event`가 locked sender를 사용하므로 여기서 lock을 잡지 않는다.
        client_ref = progress_client_slot["client"]
        if client_ref is None:
            return
        client_ref._send_event(
            endpoint_state=client_ref.state,
            artifact_sha256=_hashlib.sha256(note.encode("utf-8")).hexdigest(),
        )

    operations = Stage7PiOperations(
        endpoint=endpoint,
        resolved=resolved,
        run_root=arguments.run_root,
        endpoint_id=arguments.endpoint_id,
        progress_notifier=_progress_notifier,
    )

    # 실시간 런타임 펌프 (rr-p2-17 run o 실측 근거): frozen 런타임의
    # advance_clock/fire_due_timers/pump와 radio.poll_once는 호스트가
    # 주기적으로 구동해야 하는 설계인데(software 하네스의
    # drive_stage7_group가 그 역할), 실물 launcher에 구동 루프가 없어
    # 서비스 MAC이 어떤 radio 명령도 내지 못했다(svc-tx/rx 0건 실측 —
    # TBB 방송·RX 창 전무의 근본 원인). 여기서 50ms 주기로 구동한다.
    _pump_stop = _threading.Event()

    def _pump_loop():
        # MAC 주기 40ms: 시계 20ms 양자화(_AnchoredSystemTimeSource)가
        # wakeup 교체 id 충돌(run p)을 근원 해결했으므로, 연속 주기가 같은
        # 양자에 못 들어가는 최소 간격(2 양자)까지 내려 서비스 RX 창 발행
        # 지연을 줄인다 — svcdbg36 실측: 160ms 주기에서 ACK 청취 창이
        # +117µs 지각 시작(OFFGRID) 또는 통째로 빈 창(스트림 명령 미발화).
        last_mac_cycle = 0.0
        while not _pump_stop.is_set():
            try:
                endpoint.dependencies.radio.poll_once()
                drain = getattr(main, "_drain_dispatch_queue", None)
                if drain is not None:
                    drain()
                now_mono = _time.monotonic()
                support = endpoint.stage4
                if support is not None and now_mono - last_mac_cycle >= 0.04:
                    last_mac_cycle = now_mono
                    cycle_started = _time.monotonic()
                    support.advance_clock()
                    while support.has_due_timer():
                        support.fire_due_timers()
                    support.pump()
                    cycle_ms = (_time.monotonic() - cycle_started) * 1e3
                    # 펌프 적체는 비정상 신호일 때만 로그로 남긴다.
                    try:
                        slow = cycle_ms > 250.0
                        if slow:
                            ingress = getattr(support, "ingress", None)
                            depth = len(getattr(ingress, "_events", ()) or ())
                            held = len(getattr(ingress, "_held", ()) or ())
                            rxcmd = len(getattr(support, "_rx_commands", {}) or {})
                            dispatcher_ref = getattr(support, "_lane_dispatcher", None) \
                                or getattr(endpoint, "_lane_dispatcher", None)
                            dwin = len(getattr(dispatcher_ref, "_windows", {}) or {})
                            print(
                                f"[launcher] pump t={now_mono:.3f} "
                                f"cycle_ms={cycle_ms:.0f} depth={depth} held={held} "
                                f"rxcmd={rxcmd} dwin={dwin}",
                                file=sys.stderr, flush=True,
                            )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as error:  # noqa: BLE001 - 펌프는 계속 돈다
                now = _time.monotonic()
                last = getattr(_pump_loop, "_last_err", 0.0)
                if now - last > 5.0:
                    _pump_loop._last_err = now
                    print(f"[launcher] pump 오류: {error}", file=sys.stderr, flush=True)
            _pump_stop.wait(0.05)

    _pump_thread = _threading.Thread(target=_pump_loop, daemon=True)
    _pump_thread.start()

    root = runtime_socket_root(arguments.run_id)
    ensure_socket_root(root)
    socket_path = os.path.join(root, f"{arguments.endpoint_id}.sock")
    server = SeqPacketServer(socket_path)
    print(f"[launcher] {arguments.endpoint_id} listening {socket_path}", flush=True)

    def _locked_send(message):
        # send_message는 None을 반환한다 — bool(None)=False가 intent/TBB
        # notifier의 "전송 실패" 경로를 오발시켜 pending start를 소거했다
        # (rr-p2-17 run f/h 실측: 전송은 성공, 반환값만 falsy). 성공 시
        # True를 명시 반환하고 실패는 예외로 전파한다.
        with send_lock:
            server.send_message(message)
        return True

    client = Stage7EndpointRuntimeClient(
        endpoint_id=arguments.endpoint_id,
        run_id=arguments.run_id,
        plan_sha256=os.environ.get("STAGE7_PLAN_SHA256", "0" * 64),
        lifecycle_generation=arguments.lifecycle_generation,
        endpoint=endpoint,
        operations=operations,
        event_sender=_locked_send,
        host_raw_clock=read_monotonic_raw_ns,
    )
    notifier_slot["notifier"] = client.make_intent_notifier()
    progress_client_slot["client"] = client

    # 계측(rr-p2 기동 거부 내부사유 추적): frozen MAC begin_running의 내부
    # 오류는 §15 투영으로 지워진다 — 반환 직전에 원문을 stderr로 남긴다.
    # frozen 파일은 건드리지 않고 인스턴스 래핑만 한다 (실패해도 무해).
    try:
        _mac_cls = type(endpoint.stage4.mac)
        _orig_begin_running = _mac_cls.begin_running

        def _begin_running_logged(self_mac):
            result = _orig_begin_running(self_mac)
            try:
                if result.is_err:
                    print(
                        f"[launcher] begin_running ERR: {str(result.error)[:600]}",
                        file=sys.stderr, flush=True,
                    )
            except Exception:  # noqa: BLE001
                pass
            return result

        _mac_cls.begin_running = _begin_running_logged
        from vdeter.runtime.stage5 import Stage5Endpoint as _S5

        _orig_fail_rollback = _S5._fail_start_rollback

        def _fail_rollback_logged(self_ep, error, rollback):
            print(
                f"[launcher] start-component FAIL: {str(error)[:600]}",
                file=sys.stderr, flush=True,
            )
            return _orig_fail_rollback(self_ep, error, rollback)

        _S5._fail_start_rollback = _fail_rollback_logged
        from vdeter.stage7.support import Stage7LaneRxDispatcher as _D7

        # 배송 3계열은 _dispatch_lock으로 직렬화해 원본을 그대로 호출한다
        # (frozen ledger dict의 스레드 경쟁 차단 — 원인 사슬은 기준선 개정
        # 기록·evidence 참조).
        _orig_reg = _D7.register_window
        _orig_samples = _D7.on_lane_samples
        _orig_lane_err = _D7.on_lane_error

        def _reg_locked(self_d, command, request, canonical_allowed, *, timing_binding):
            with _dispatch_lock:
                return _orig_reg(self_d, command, request, canonical_allowed,
                                 timing_binding=timing_binding)

        def _samples_locked(self_d, lane_generation, lane_index, child_key, received):
            with _dispatch_lock:
                return _orig_samples(
                    self_d, lane_generation, lane_index, child_key, received
                )

        def _lane_err_locked(self_d, lane_generation, lane_index, child_key, code, details):
            with _dispatch_lock:
                return _orig_lane_err(
                    self_d, lane_generation, lane_index, child_key, code, details
                )

        from vdeter.mac.coordinator import MacCoordinator as _MC

        # 해제검사 완화 (근거: frozen 규칙은 계획·해제가
        # 같은 lead라 최조기 슬롯 의도가 다음 해제 평가(≤40ms 뒤)에서
        # SLOT_MISSED — 데이터 전송 확정 실패. 해제측만 120ms 완화해
        # 슬랙을 보장한다. 늦은 제출의 물리 방어는 어댑터 _LEAD_NS
        # 100ms가 계속 수행. 계획·창·방송 경로는 불변.)
        import dataclasses as _dc_rel
        from vdeter.mac.coordinator import MacCoordinator as _MC_rel

        _orig_release = _MC_rel._release_due_tx_intents

        def _release_relaxed(self_c, state, causation_event_id, commands, errors):
            original_profile = self_c._profile
            relaxed = _dc_rel.replace(
                original_profile,
                transmit_lead_nanoseconds=max(
                    original_profile.transmit_lead_nanoseconds - 200_000_000,
                    100_000_000,
                ),
            )
            self_c._profile = relaxed
            try:
                return _orig_release(
                    self_c, state, causation_event_id, commands, errors
                )
            finally:
                self_c._profile = original_profile

        _MC_rel._release_due_tx_intents = _release_relaxed
        # 데이터 세션 채널 기본값 교정 (실측 + 소스 확정:
        # coordinator 3873/5838의 resolve_data_session_plan 호출이 lower/upper
        # 인자 누락 → frozen 기본값 25_TEST → wideband 지도(100_TEST 전용)에
        # 없는 채널 RAC 조회로 재시도 경로 확정 실패. 누락 시 wideband
        # 채널을 주입한다 — 명시 인자 호출(1835)은 불변).
        import vdeter.mac.routing as _rt_mod
        from vdeter.mac.tbb import (
            WIDEBAND_LOWER_CHANNEL as _WB_LO,
            WIDEBAND_UPPER_CHANNEL as _WB_UP,
        )

        # 기본값 함정 부류 일괄 봉합 (svcdbg79 확증: ASC 탐색도 UPPER_25
        # 기본값 호출 존재 — routing의 25_TEST 기본 인자 4개 함수 전체를
        # wideband 주입으로 래핑. 명시 인자 호출은 setdefault라 불변).
        def _wideband_defaults(orig, positional_ok=False):
            def wrapped(*args, **kwargs):
                if not positional_ok and args:
                    return orig(*args, **kwargs)
                kwargs.setdefault("lower", _WB_LO)
                kwargs.setdefault("upper", _WB_UP)
                return orig(*args, **kwargs)
            return wrapped

        for _fn_name in (
            "resolve_data_session_plan",
            "resolve_short_data_direction",
            "receive_short_data_placement",
        ):
            setattr(_rt_mod, _fn_name,
                    _wideband_defaults(getattr(_rt_mod, _fn_name)))
        _rt_mod.receive_physical_channel = _wideband_defaults(
            _rt_mod.receive_physical_channel, positional_ok=True
        )
        # TBB pending 표식 교정 2종 (frozen dispatch 맵이 클래스 정의 시점
        # 원본을 캡처하므로 늦은 결속인 handle_event를 래핑한다).
        _orig_he = _MC.handle_event

        def _he_guarded(self_c, envelope):
            plan = _orig_he(self_c, envelope)
            try:
                # TBB 활성 감시견: pending 표식이 4s 이상 지속되면 해제해
                # 방송 주기를 캡핑한다 (버스트 26.7ms라 이중 방사 위험 없음,
                # TBB는 반복 방송이라 의미상 무해).
                ns_w = getattr(plan, "next_state", None)
                tbb_w = getattr(ns_w, "tbb_broadcast", None)
                if tbb_w is not None and tbb_w.encoded_packets:
                    pid = tbb_w.pending_command_id
                    now_m = _time.monotonic()
                    if pid is None:
                        main._tbb_pend = None
                    else:
                        prev = getattr(main, "_tbb_pend", None)
                        if prev is None or prev[0] != pid:
                            main._tbb_pend = (pid, now_m)
                        elif now_m - prev[1] > 4.0:
                            import dataclasses as _dc2
                            plan = _dc2.replace(
                                plan,
                                next_state=ns_w.evolve(
                                    tbb_broadcast=_dc2.replace(
                                        tbb_w, pending_command_id=None
                                    )
                                ),
                            )
                            main._tbb_pend = None
            except Exception:  # noqa: BLE001
                pass
            try:
                p = getattr(envelope, "payload", None)
                if type(p).__name__ == "PhyTxCompletedEvent":
                    # TBB pending 표식 교정: 완료-명령 상관 불일치로 표식이
                    # 안 지워지면 다음 방송이 명령 만료까지 잠긴다. 완료
                    # 패킷이 TBB 조각으로 검증될 때만 표식을 해제한다.
                    ns2 = getattr(plan, "next_state", None)
                    tbb = getattr(ns2, "tbb_broadcast", None)
                    if (
                        ns2 is not None and tbb is not None
                        and tbb.pending_command_id is not None
                        and any(
                            getattr(ep, "packet_id", None) == p.packet_id
                            for ep in (tbb.encoded_packets or ())
                        )
                    ):
                        import dataclasses as _dc
                        cleared = _dc.replace(tbb, pending_command_id=None)
                        plan = _dc.replace(
                            plan, next_state=ns2.evolve(tbb_broadcast=cleared)
                        )
            except Exception:  # noqa: BLE001
                pass
            return plan

        _MC.handle_event = _he_guarded
        # dispatcher 직렬화 락 (svcdbg52 실측: 배송을 adapter 락 밖으로 빼자
        # 창 등록(MAC 스레드)과 샘플 관측(monitor 스레드)이 frozen ledger
        # dict 위에서 경쟁 — 'dictionary changed size'). adapter 락과 분리해
        # TX 경로는 계속 자유롭다.
        import threading as _thr_dsp

        _dispatch_lock = _thr_dsp.RLock()
        # 배송의 펌프-스레드 이관 (락 전도 재검증 결론: monitor가 L2를 쥔 채
        # _deliver_recovered→MacPort L3를 요구, pump는 L3 하에 창 등록 L2를
        # 요구 — ABBA 순환 잔존. monitor는 큐 적재만 하고 originals는 pump
        # 스레드가 실행 → 락 방향 L3→L2 단방향화, 순환 소멸).
        from collections import deque as _deque

        _dispatch_queue = _deque()

        def _q_samples(self_d, lane_generation, lane_index, child_key, received):
            _dispatch_queue.append(
                ("s", self_d, lane_generation, lane_index, child_key, received)
            )

        def _q_completed(self_d, lane_generation, lane_index, child_key, completion):
            _dispatch_queue.append(
                ("c", self_d, lane_generation, lane_index, child_key, completion)
            )

        def _q_lane_err(self_d, lane_generation, lane_index, child_key, code, details):
            _dispatch_queue.append(
                ("e", self_d, lane_generation, lane_index, child_key, code, details)
            )

        def _drain_dispatch_queue():
            for _ in range(256):
                try:
                    item = _dispatch_queue.popleft()
                except IndexError:
                    return
                kind = item[0]
                try:
                    if kind == "s":
                        _samples_locked(*item[1:])
                    elif kind == "c":
                        _completed_locked(*item[1:])
                    else:
                        _lane_err_locked(*item[1:])
                except Exception as error:  # noqa: BLE001
                    print(f"[launcher] dispatch-drain 오류: {error}",
                          file=sys.stderr, flush=True)

        main._drain_dispatch_queue = _drain_dispatch_queue
        _orig_completed = _D7.on_lane_window_completed

        def _completed_locked(self_d, lane_generation, lane_index, child_key, completion):
            with _dispatch_lock:
                return _orig_completed(self_d, lane_generation, lane_index,
                                       child_key, completion)

        _D7.on_lane_error = _q_lane_err
        _D7.register_window = _reg_locked
        _D7.on_lane_samples = _q_samples
        _D7.on_lane_window_completed = _q_completed
    except Exception as error:  # noqa: BLE001
        print(f"[launcher] 런타임 배선 실패: {error}", flush=True)
        raise

    server.accept(
        expected_pid=arguments.coordinator_pid,
        expected_uid=os.getuid(),  # type: ignore[attr-defined]
        timeout=60.0,
    )
    print(f"[launcher] coordinator 연결됨 pid={arguments.coordinator_pid}", flush=True)

    # SIGTERM(coordinator 정리)에도 lease 반납과 evidence 기록이 실행되도록
    # SystemExit로 변환해 finally를 통과시킨다. 고아 worker가 B210을 계속
    # 점유하는 것을 막는다.
    import signal as _signal

    def _terminate(_signum, _frame):
        raise SystemExit(4)

    _signal.signal(_signal.SIGTERM, _terminate)

    def _try_emit_tbb_active() -> bool:
        """staged start 이후 frozen MAC이 실제 TBB를 획득하면 §16.1 record를
        정확히 한 번 전송한다. 아직 미획득이면 False."""

        import hashlib as _hashlib

        from vdeter.stage7.records import Stage7TbbActiveRecord

        state = endpoint.stage4.mac.state
        active = state.active_tbb
        if active is None:
            return False
        fingerprint = active.fingerprint
        try:
            fingerprint_bytes = bytes.fromhex(fingerprint)
        except ValueError:
            fingerprint_bytes = _hashlib.sha256(fingerprint.encode("utf-8")).digest()
        if len(fingerprint_bytes) != 32:
            fingerprint_bytes = _hashlib.sha256(fingerprint.encode("utf-8")).digest()
        record = Stage7TbbActiveRecord(
            endpoint_id=arguments.endpoint_id,
            readiness_kind=(
                "ROC_BROADCAST_READY" if arguments.endpoint_id == "ROC" else "MASS_ACQUIRED"
            ),
            lifecycle_generation=client.lifecycle_generation,
            bridge_generation=client.bridge_generation,
            tbb_fingerprint_sha256=fingerprint_bytes,
            valid_from_utc_ns=active.valid_from_nanoseconds,
            valid_until_utc_ns=active.valid_until_nanoseconds,
            activation_frame_ordinal=active.activation_nanoseconds,
            local_tbb_evidence_sha256=_hashlib.sha256(
                f"{arguments.endpoint_id}:{fingerprint}".encode("utf-8")
            ).digest(),
        )
        return bool(client.emit_tbb_active(record))

    # --- v0.9.1 G항: RR TEST adapter 스레드 + public event JSONL ------------------
    test_threads = []
    if arguments.test_plan_sha:
        import glob as _glob
        import json as _json_local

        from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter

        fixtures = {}
        if arguments.fixtures_dir and os.path.isdir(arguments.fixtures_dir):
            for path in sorted(_glob.glob(os.path.join(arguments.fixtures_dir, "*.json"))):
                with open(path, "rb") as handle:
                    fixtures[os.path.basename(path)] = handle.read()
        adapter = Stage7ServiceApiTestAdapter(
            endpoint_id=arguments.endpoint_id,
            service=endpoint,
            resolved_test_plan_sha256=arguments.test_plan_sha,
            authorized_controller=(os.getuid(),),  # type: ignore[attr-defined]
            fixtures=fixtures,
        )
        test_socket_path = os.path.join(root, f"{arguments.endpoint_id}.test.sock")
        test_server = SeqPacketServer(test_socket_path)

        def _adapter_loop():
            try:
                test_server.accept(
                    expected_pid=None,
                    expected_uid=os.getuid(),  # type: ignore[attr-defined]
                    timeout=3600.0,
                )
                while True:
                    message = test_server.recv_message(timeout=5.0)
                    if message is None:
                        continue
                    response = adapter.handle_command(
                        message, peer=(os.getuid(),)  # type: ignore[attr-defined]
                    )
                    test_server.send_message(response)
            except (ConnectionError, OSError):
                return

        def _event_jsonl_loop():
            events_dir = os.path.join(arguments.run_root, "evidence", "service_events")
            os.makedirs(events_dir, exist_ok=True)
            path = os.path.join(events_dir, f"{arguments.endpoint_id}.jsonl")
            # 이벤트 0건으로 끝나는 run에서도 파일 존재를 보장한다
            # (svcdbg52: TBB 관문 붕괴 시 MASS-A.jsonl 부재 — 감사 혼동 방지).
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
            seen = 0
            while True:
                try:
                    sink = endpoint.start_gate.application_sink
                    events = tuple(sink.events())
                except Exception:  # noqa: BLE001
                    events = ()
                if len(events) > seen:
                    with open(path, "a", encoding="utf-8") as handle:
                        for event in events[seen:]:
                            record = {
                                "t_mono": round(time.monotonic(), 3),
                                "event_type": getattr(
                                    getattr(event, "event_type", None), "value",
                                    str(getattr(event, "event_type", None)),
                                ),
                                "transfer_id": getattr(
                                    getattr(event, "transfer_id", None), "value",
                                    None,
                                ),
                                "transfer_state": getattr(
                                    getattr(event, "transfer_state", None), "value",
                                    None,
                                ),
                                "error_code": getattr(
                                    getattr(event, "error", None), "code", None
                                ),
                            }
                            error_object = getattr(event, "error", None)
                            if error_object is not None:
                                record["error_message"] = str(
                                    getattr(error_object, "message", "")
                                )[:200]
                                error_details = getattr(error_object, "details", None)
                                if error_details is not None:
                                    record["error_details"] = str(error_details)[:300]
                            received = getattr(event, "received_data", None)
                            if received is not None:
                                payload = getattr(received, "payload", b"") or b""
                                record["payload_sha256"] = (
                                    _hashlib.sha256(payload).hexdigest()
                                )
                                record["payload_len"] = len(payload)
                                source = getattr(received, "source", None)
                                record["source"] = getattr(
                                    getattr(source, "station_id", None), "value", None
                                )
                            handle.write(_json_local.dumps(record, ensure_ascii=True))
                            handle.write("\n")
                    seen = len(events)
                time.sleep(0.5)

        for target in (_adapter_loop, _event_jsonl_loop):
            thread = _threading.Thread(target=target, daemon=True)
            thread.start()
            test_threads.append(thread)
        print(f"[launcher] test adapter 대기: {test_socket_path}", flush=True)

    exit_code = 0
    tbb_emitted = False
    try:
        while True:
            watching_tbb = client.state == "SERVICE_START_STAGED" and not tbb_emitted
            message = server.recv_message(timeout=0.5 if watching_tbb else 120.0)
            if message is None:
                if watching_tbb:
                    try:
                        tbb_emitted = _try_emit_tbb_active()
                    except Exception as error:  # noqa: BLE001
                        print(f"[launcher] TBB emit 실패: {error}", flush=True)
                continue
            response = client.handle_command(message)
            _locked_send(response)
            if (
                message.get("command_name") == "stop"
                and message.get("barrier_phase") == "COMMIT"
            ):
                break
    except ConnectionError:
        exit_code = 3
    except SystemExit as stop_signal:
        exit_code = int(stop_signal.code or 0)
    finally:
        try:
            endpoint.dependencies.radio.close_qualification_lease()
        except Exception:  # noqa: BLE001 - 종료 경로 최선 노력
            pass
        evidence_dir = os.path.join(arguments.run_root, "evidence")
        os.makedirs(evidence_dir, exist_ok=True)
        evidence_path = os.path.join(
            evidence_dir, f"{arguments.endpoint_id}-runtime.json"
        )
        with open(evidence_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "endpoint_id": arguments.endpoint_id,
                    "run_id": arguments.run_id,
                    "final_state": client.state,
                    "stale_rejections": list(client.stale_rejections()),
                    "operations_evidence": operations.evidence,
                },
                handle,
                ensure_ascii=True,
                sort_keys=True,
                indent=1,
            )
        server.close()
    print(f"[launcher] {arguments.endpoint_id} 종료 state={client.state}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
