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
                    fired_n = 0
                    while support.has_due_timer():
                        support.fire_due_timers()
                        fired_n += 1
                    if fired_n and os.environ.get("STAGE7_DIAG", "0") == "1":
                        print(
                            f"[diag] timers-fired n={fired_n} "
                            f"t={_time.monotonic():.3f}",
                            file=sys.stderr, flush=True,
                        )
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

    # STAGE7_DIAG=1: 조사 세션 전용 진단 계측 (기본 off — 정식 run 무접촉).
    # ① §15 사영 이전의 내부 오류코드 ② RX 창 등록/거부 ③ 수신 미결 계수
    # 주기 덤프 (누적 열화·세션 수신 실패의 공통 뿌리 가설 측정).
    _DIAG = os.environ.get("STAGE7_DIAG", "0") == "1"
    if _DIAG:
        try:
            from vdeter.runtime import projection as _dproj

            _diag_orig_tse = _dproj.to_service_error

            def _diag_tse(error):
                try:
                    print(
                        f"[diag] svc-err {getattr(error, 'code', '?')}: "
                        f"{str(getattr(error, 'message', ''))[:110]} "
                        f"det={str(getattr(error, 'details', ''))[:200]}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_tse(error)

            _dproj.to_service_error = _diag_tse
            for _dmod_name in ("vdeter.runtime.endpoint", "vdeter.runtime.stage5"):
                import importlib as _dimp
                _dmod = _dimp.import_module(_dmod_name)
                if getattr(_dmod, "to_service_error", None) is _diag_orig_tse:
                    _dmod.to_service_error = _diag_tse

            from vdeter.stage7.radio_adapter import (
                Stage7B210RadioAdapter as _DRA,
            )

            _diag_orig_rx = _DRA.request_rx

            def _diag_rx(self_r, request):
                res = _diag_orig_rx(self_r, request)
                try:
                    n = main._diag_rx_n = getattr(main, "_diag_rx_n", 0) + 1
                    # 오류는 전건, 정상은 스로틀 (펌프 타이밍 왜곡 방지).
                    if res.is_err or n <= 40 or n % 25 == 0:
                        out = "OK" if not res.is_err else (
                            f"ERR {getattr(res.error, 'code', '?')} "
                            f"{str(getattr(res.error, 'message', ''))[:60]}"
                        )
                        start = getattr(request, "start_at", None)
                        print(
                            f"[diag] rx-req #{n} "
                            f"ch={getattr(request, 'channel', None)} "
                            f"start={getattr(start, 'total_nanoseconds', lambda: '?')()} "
                            f"-> {out}",
                            file=sys.stderr, flush=True,
                        )
                except Exception:  # noqa: BLE001
                    pass
                return res

            _DRA.request_rx = _diag_rx

            def _diag_pending_census():
                try:
                    state = endpoint.stage4.mac.state
                    pend = getattr(state, "delivery_pending", ()) or ()
                    deps = getattr(state, "pending_dependencies", None)
                    dep_n = len(deps) if deps is not None else "?"
                    ledger = getattr(state, "delivery_ledger", ()) or ()
                    # C6: 수신 세션 잔류 감시 (감사③ — 회수 부재로 영구
                    # 잔류하면 해당 LC가 블랙홀化).
                    sessions = [
                        (
                            getattr(s, "logical_channel_rx", "?"),
                            getattr(s, "opened", "?"),
                            len(getattr(s, "seen_fragment_keys", ()) or ()),
                        )
                        for s in (getattr(state, "rx_sessions", ()) or ())
                    ]
                    print(
                        f"[diag] census t={_time.monotonic():.1f} "
                        f"delivery_pending={len(pend)} pending_deps={dep_n} "
                        f"ledger={len(ledger)} rx_sessions={sessions}",
                        file=sys.stderr, flush=True,
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"[diag] census 오류: {error}",
                          file=sys.stderr, flush=True)

            def _diag_census_loop():
                while not _pump_stop.is_set():
                    _diag_pending_census()
                    _pump_stop.wait(10.0)

            _threading.Thread(target=_diag_census_loop, daemon=True).start()

            # 정밀: 세션 조각 수신의 slot 판정 입력/출력 실측 (frozen
            # _receive_data_fragment는 self-호출이라 class-attr 래핑 유효).
            from vdeter.mac.coordinator import MacCoordinator as _DMC
            from vdeter.mac.tdma import (
                frame_ordinal_at_nanoseconds as _d_frame,
                position_at_nanoseconds as _d_pos,
            )
            from vdeter.mac.tbb import SlotFunction as _DSF

            _diag_orig_rdf = _DMC._receive_data_fragment

            def _diag_rdf(self_c, state, **kwargs):
                _pre_cmd = len(kwargs.get("commands") or ())
                _pre_err = len(kwargs.get("errors") or ())
                try:
                    decoded = kwargs.get("decoded")
                    channel = kwargs.get("channel")
                    received = decoded.received_at.total_nanoseconds()
                    pos = _d_pos(received)
                    smap = self_c.active_slot_map(state)
                    slot = smap.slot_at(
                        channel, pos.tdma_channel, pos.slot_in_frame
                    )
                    _sess = None
                    if slot is not None:
                        _sess = state.rx_session_for_channel(
                            slot.logical_channel
                        )
                    print(
                        f"[diag] frag-pre body={type(kwargs.get('body')).__name__} "
                        f"gfn={getattr(kwargs.get('body'), 'global_fragment_number', '?')} "
                        f"sess={'None' if _sess is None else f'opened={_sess.opened},seen={len(getattr(_sess, 'seen_fragment_keys', ()) or ())}'}",
                        file=sys.stderr, flush=True,
                    )
                    from vdeter.mac.access import enumerate_function_slots as _d_enum
                    frame = _d_frame(received)
                    from vdeter.mac.tdma import (
                        frame_ordinal_start_nanoseconds as _d_fstart,
                        FRAME_NANOSECONDS as _d_fns,
                    )
                    fs = _d_fstart(frame)
                    dcs = [
                        (item.tdma_channel, item.slot_in_frame,
                         getattr(item, 'logical_channel', '?'))
                        for item in _d_enum(
                            slot_map=smap, function=_DSF.DC,
                            physical_channel=channel,
                            from_utc_nanoseconds=fs,
                            until_utc_nanoseconds=fs + _d_fns,
                            transmit=False,
                        )
                    ][:8]
                    print(
                        f"[diag] frag-rx ch={getattr(channel,'name','?')} "
                        f"recv_ns={received} tdma={pos.tdma_channel} "
                        f"slot={pos.slot_in_frame} -> "
                        f"slot_fn={getattr(getattr(slot,'function',None),'name',None)} "
                        f"lc={getattr(slot,'logical_channel',None)} "
                        f"map_dc(frame)={dcs}",
                        file=sys.stderr, flush=True,
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"[diag] frag-rx probe 오류: {error}",
                          file=sys.stderr, flush=True)
                result_state = _diag_orig_rdf(self_c, state, **kwargs)
                try:
                    print(
                        f"[diag] frag-post dCmd="
                        f"{len(kwargs.get('commands') or ()) - _pre_cmd} "
                        f"dErr={len(kwargs.get('errors') or ()) - _pre_err}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return result_state

            _DMC._receive_data_fragment = _diag_rdf

            # 정밀: SLOT_MISSED 발생 순간의 산술 (procedure·의도시각·now·lead).
            import vdeter.mac.transfers as _dtr

            _diag_orig_sme = _dtr.slot_missed_error

            def _diag_sme(procedure):
                try:
                    import traceback as _dtb
                    caller = _dtb.extract_stack(limit=3)[0]
                    print(
                        f"[diag] slot-missed proc={procedure} "
                        f"at={caller.name}:{caller.lineno}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_sme(procedure)

            _dtr.slot_missed_error = _diag_sme

            # C1/C2: link 실행·무음 실패 싱크 개봉 (감사③ S1/S2 —
            # reassembly 명령의 실패는 correlation.transfer_id 부재로 전
            # 계층 무음이었다).
            from vdeter.runtime.endpoint import Stage4Support as _DS4

            _diag_orig_fail = _DS4._fail

            def _diag_fail(self_s, command, error):
                try:
                    payload = getattr(command, "payload", None)
                    req = getattr(payload, "request", None)
                    print(
                        f"[diag] dep-FAIL cmd={type(payload).__name__} "
                        f"req={type(req).__name__} code={getattr(error,'code','?')} "
                        f"det={str(getattr(error,'details',''))[:120]}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_fail(self_s, command, error)

            _DS4._fail = _diag_fail

            _diag_orig_xlink = _DS4._execute_link

            def _diag_xlink(self_s, command, payload):
                result = _diag_orig_xlink(self_s, command, payload)
                try:
                    req = getattr(payload, "request", payload)
                    rt = type(req).__name__
                    if "Reassembly" in rt or "Fragment" in rt:
                        if getattr(result, "is_err", False):
                            out = f"ERR {getattr(result.error,'code','?')}"
                        else:
                            v = getattr(result, "value", None)
                            out = (
                                f"OK status={getattr(getattr(v,'status',None),'value',None)} "
                                f"kind={getattr(getattr(v,'kind',None),'value',None)}"
                            )
                        print(
                            f"[diag] link-exec {rt} "
                            f"sh={getattr(getattr(req,'session_handle',None),'value','?')} "
                            f"-> {out}",
                            file=sys.stderr, flush=True,
                        )
                except Exception:  # noqa: BLE001
                    pass
                return result

            _DS4._execute_link = _diag_xlink

            # C5: 펌프-배송 경합 계수 (H1 판별 — 두 조각 배송 사이 pump 0회면
            # 경합 확정).
            _diag_orig_pump = _DS4.pump

            def _diag_pump(self_s, *a, **k):
                main._diag_pump_n = getattr(main, "_diag_pump_n", 0) + 1
                return _diag_orig_pump(self_s, *a, **k)

            _DS4.pump = _diag_pump

            from vdeter.runtime.endpoint import VdeTerEndpoint as _DVE

            _diag_orig_delrec = _DVE._deliver_recovered

            def _diag_delrec(self_e, *a, **k):
                try:
                    print(
                        f"[diag] deliver-recovered t={_time.monotonic():.3f} "
                        f"pump_n={getattr(main, '_diag_pump_n', 0)}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_delrec(self_e, *a, **k)

            _DVE._deliver_recovered = _diag_delrec

            # C4: 재조립기 내부
            from vdeter.link.data_session.reassembly import (
                DataSessionReassembler as _DRA2,
            )

            for _mname in ("control", "process"):
                _orig_m = getattr(_DRA2, _mname)

                def _mk(_orig_m=_orig_m, _mname=_mname):
                    def _w(self_r, request):
                        result = _orig_m(self_r, request)
                        try:
                            if getattr(result, "is_err", False):
                                out = f"ERR {getattr(result.error,'code','?')}"
                            else:
                                v = getattr(result, "value", None)
                                out = (
                                    f"OK {getattr(getattr(v,'status',None),'value',None) or getattr(getattr(v,'kind',None),'value',None)}"
                                )
                            print(
                                f"[diag] reasm-{_mname} "
                                f"sh={getattr(getattr(request,'session_handle',None),'value','?')} "
                                f"-> {out}",
                                file=sys.stderr, flush=True,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        return result
                    return _w

                setattr(_DRA2, _mname, _mk())

            # C7: 준무음 경로
            _diag_orig_ree = _DS4._record_endpoint_error

            def _diag_ree(self_s, error, *a, **k):
                try:
                    print(
                        f"[diag] ep-err {getattr(error,'code','?')} "
                        f"{str(getattr(error,'message',''))[:80]}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_ree(self_s, error, *a, **k)

            _DS4._record_endpoint_error = _diag_ree

            _diag_orig_odf = _DMC._on_dependency_failed

            def _diag_odf(self_c, state, envelope, *a, **k):
                try:
                    p = getattr(envelope, "payload", None)
                    print(
                        f"[diag] dep-failed-evt "
                        f"code={getattr(getattr(p,'error',None),'code','?')} "
                        f"xfer={getattr(getattr(envelope,'correlation',None),'transfer_id',None) is not None}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_odf(self_c, state, envelope, *a, **k)

            _DMC._on_dependency_failed = _diag_odf

            # 정밀: horizon 진입 타이머의 무장/발화 실측 (release 기아의
            # 원인 특정 — 스위프가 프레임 주기로만 도는 이유).
            _diag_orig_arm = _DMC._arm_horizon_entry

            def _diag_arm(self_c, state, *, entry_utc_nanoseconds,
                          causation_event_id, commands, errors):
                out = _diag_orig_arm(
                    self_c, state,
                    entry_utc_nanoseconds=entry_utc_nanoseconds,
                    causation_event_id=causation_event_id,
                    commands=commands, errors=errors,
                )
                try:
                    armed = out[1] if isinstance(out, tuple) else "?"
                    now_u = self_c._now_utc_nanoseconds()
                    print(
                        f"[diag] arm entry_in_ms="
                        f"{(entry_utc_nanoseconds - now_u) / 1e6:.0f} "
                        f"armed={armed} timers={len(getattr(state, 'timers', ()))}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return out

            _DMC._arm_horizon_entry = _diag_arm

            # 정밀: TX 의도 '생성' 순간 실측 — 어느 계획기가 몇 ms 여유의
            # 슬롯을 만드는가 (과거 슬롯 생성자의 특정).
            _diag_orig_doe = _DMC._defer_or_emit

            def _diag_doe(self_c, state, *, intent, causation_event_id,
                          commands, errors):
                try:
                    import traceback as _dtb2
                    now_c = self_c._now_utc_nanoseconds()
                    delta = (intent.transmit_at_nanoseconds - now_c) / 1e6
                    stack = _dtb2.extract_stack(limit=4)
                    chain = "<-".join(
                        f"{f.name}:{f.lineno}" for f in stack[:-1][-2:]
                    )
                    print(
                        f"[diag] intent-new kind={intent.kind} "
                        f"delta_ms={delta:.0f} xfer="
                        f"{intent.correlation_transfer_id is not None} "
                        f"by={chain}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_doe(
                    self_c, state, intent=intent,
                    causation_event_id=causation_event_id,
                    commands=commands, errors=errors,
                )

            _DMC._defer_or_emit = _diag_doe
            print("[diag] STAGE7_DIAG 계측 활성", file=sys.stderr, flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"[diag] 계측 장착 실패: {error}", file=sys.stderr, flush=True)

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
        import threading as _thr_rel

        # 프로파일 스왑은 인스턴스 전역이라 재진입/동시 진입 시 완화값이
        # 원본으로 오인 복원될 수 있다 — 직렬화로 차단.
        _rel_lock = _thr_rel.RLock()

        def _release_relaxed(self_c, state, causation_event_id, commands, errors):
            with _rel_lock:
                return _release_relaxed_locked(
                    self_c, state, causation_event_id, commands, errors
                )

        def _release_relaxed_locked(self_c, state, causation_event_id, commands, errors):
            original_profile = self_c._profile
            relaxed = _dc_rel.replace(
                original_profile,
                transmit_lead_nanoseconds=max(
                    original_profile.transmit_lead_nanoseconds - 200_000_000,
                    100_000_000,
                ),
            )
            if os.environ.get("STAGE7_DIAG", "0") == "1" and state.tx_intents:
                try:
                    now_d = self_c._now_utc_nanoseconds()
                    deltas = sorted(
                        round((i.transmit_at_nanoseconds - now_d) / 1e6)
                        for i in state.tx_intents
                    )[:8]
                    print(
                        f"[diag] release n={len(state.tx_intents)} "
                        f"delta_ms={deltas} "
                        f"floor_ms={relaxed.transmit_lead_nanoseconds / 1e6:.0f}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            self_c._profile = relaxed
            try:
                result_state = _orig_release(
                    self_c, state, causation_event_id, commands, errors
                )
            finally:
                self_c._profile = original_profile
            return result_state

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
            try:
                # F4-lite: 잔류 수신 세션 GC (svc-real-2 실측: idle 회수
                # 기제 부재로 이전 시도의 세션이 opened 여부와 무관하게
                # 영구 잔류 → 그 LC의 후속 조각 전량 무음 폐기). 계획된
                # 프레임 최대치를 현재 프레임의 대리치로 써서, 3프레임
                # (7.2s) 이상 무활동 세션을 상태에서 제거한다 — 다음
                # Start 조각이 새 OPEN으로 재개설한다.
                ns_g = getattr(plan, "next_state", None)
                sess_g = getattr(ns_g, "rx_sessions", ()) or ()
                planned_g = getattr(ns_g, "planned_frame_ordinals", ()) or ()
                if sess_g and planned_g:
                    cur_f = max(planned_g)
                    stale = [
                        s for s in sess_g
                        if getattr(s, "last_frame_ordinal", 0) < cur_f - 3
                    ]
                    for s in stale:
                        ns_g = ns_g.without_rx_session(
                            getattr(s.session_handle, "value",
                                    s.session_handle)
                        )
                        print(
                            "[launcher] rx-session-gc lc="
                            f"{getattr(s, 'logical_channel_rx', '?')} "
                            f"idle_frames={cur_f - s.last_frame_ordinal}",
                            file=sys.stderr, flush=True,
                        )
                    if stale:
                        import dataclasses as _dc3
                        plan = _dc3.replace(plan, next_state=ns_g)
            except Exception:  # noqa: BLE001
                pass
            return plan

        _MC.handle_event = _he_guarded
        # 단문→세션 승격 (svc-real-1 실측: link 계층은 "타 링크(19)의 단문
        # 용량 685B" 근거로 38~685B를 단문 후보로 내는데, frozen MAC 단문
        # 경로는 Link 11(37B) 고정이라 실을 수 없어 즉사 — 실물 인증서
        # 353B가 정확히 이 사각지대. 스펙(인증서=데이터 세션 90→4→74/75/76)
        # 대로, Link 11이 실제로 싣지 못하는 단문 후보는 세션으로 승격한다.
        _orig_submit_short = _MC._submit_short_data

        def _submit_short_or_session(self_c, state, requirement,
                                     causation_event_id):
            try:
                link11 = next(
                    (p for p in requirement.profiles
                     if getattr(p.link_id, "value", None) == 11),
                    None,
                )
                if link11 is None or not link11.short_data_eligible:
                    print(
                        "[launcher] short->session 승격 "
                        f"(link11 미적합, size="
                        f"{requirement.presentation_size})",
                        file=sys.stderr, flush=True,
                    )
                    return self_c._submit_data_session(
                        state, requirement, causation_event_id
                    )
            except Exception:  # noqa: BLE001 - 승격 실패 시 원 경로
                pass
            return _orig_submit_short(
                self_c, state, requirement, causation_event_id
            )

        _MC._submit_short_data = _submit_short_or_session
        # F-spread (svc-run-a 실측: 76이 OPEN 왕복 완료 전 도착 → frozen
        # 3079가 무오류 폐기 — H1 확정. 수신측 명령 적체 하에서 OPEN에
        # 시간을 주기 위해, 세션의 '마지막 조각'만 +800ms(5슬롯) 뒤 같은
        # LC의 DC 슬롯으로 이동한다. 마지막 조각만 미루므로 위치 순서는
        # 구조적으로 보존, 수신측 로직 무접촉).
        from vdeter.mac.tdma import (
            HEXSLOT_NANOSECONDS as _SPR_HEX,
            frame_ordinal_at_nanoseconds as _spr_frame,
            position_at_nanoseconds as _spr_pos,
        )

        _orig_doe_func = _MC._defer_or_emit

        def _doe_spread(self_c, state, *, intent, causation_event_id,
                        commands, errors):
            try:
                if (
                    intent.kind == "BUILD"
                    and intent.correlation_transfer_id is not None
                ):
                    request = intent.build_request
                    count = getattr(request, "frame_fragment_count", 1)
                    position = getattr(
                        request, "fragment_position_in_frame", 1
                    )
                    if count >= 2 and position == count:
                        import dataclasses as _dcs

                        base = intent.transmit_at_nanoseconds
                        smap2 = self_c.active_slot_map(state)
                        channel2 = None
                        plan2 = getattr(request, "plan", None)
                        channel2 = getattr(plan2, "channel", None)
                        pos0 = _spr_pos(base) if channel2 is not None else None
                        want_lc = None
                        if pos0 is not None:
                            slot0 = smap2.slot_at(
                                channel2, pos0.tdma_channel, pos0.slot_in_frame
                            )
                            want_lc = getattr(slot0, "logical_channel", None)
                        new_at = None
                        for k_step in range(5, 12):
                            cand = base + k_step * _SPR_HEX
                            posc = _spr_pos(cand)
                            slotc = smap2.slot_at(
                                channel2, posc.tdma_channel, posc.slot_in_frame
                            )
                            if (
                                slotc is not None
                                and getattr(slotc, "function", None) is not None
                                and getattr(slotc.function, "name", "") == "DC"
                                and (want_lc is None
                                     or slotc.logical_channel == want_lc)
                            ):
                                new_at = cand
                                break
                        if new_at is not None:
                            new_plan2 = _dcs.replace(
                                plan2, transmit_at=self_c._utc(new_at)
                            )
                            new_req2 = _dcs.replace(request, plan=new_plan2)
                            intent = _dcs.replace(
                                intent,
                                transmit_at_nanoseconds=new_at,
                                build_request=new_req2,
                            )
                            print(
                                f"[launcher] frag-spread pos={position}/{count} "
                                f"+{(new_at - base) / 1e6:.0f}ms",
                                file=sys.stderr, flush=True,
                            )
            except Exception:  # noqa: BLE001 - 실패 시 원 시각 유지
                pass
            return _orig_doe_func(
                self_c, state, intent=intent,
                causation_event_id=causation_event_id,
                commands=commands, errors=errors,
            )

        _MC._defer_or_emit = _doe_spread

        # F2-lite: reassembly 명령 실패(무음 싱크) 시 해당 수신 세션 회수 —
        # 다음 Start 조각이 새 OPEN을 발행할 수 있게 한다 (블랙홀 방지).
        _orig_odf_func = _MC._on_dependency_failed

        def _odf_recover(self_c, state, envelope, *a, **k):
            result_state = _orig_odf_func(self_c, state, envelope, *a, **k)
            try:
                corr = getattr(envelope, "correlation", None)
                sh = getattr(corr, "session_handle", None)
                sh_val = getattr(sh, "value", sh)
                if (
                    sh_val is not None
                    and getattr(corr, "transfer_id", None) is None
                ):
                    sessions = getattr(result_state, "rx_sessions", ()) or ()
                    match = next(
                        (s for s in sessions
                         if getattr(s.session_handle, "value",
                                    s.session_handle) == sh_val),
                        None,
                    )
                    if match is not None:
                        result_state = result_state.without_rx_session(sh_val)
                        print(
                            "[launcher] rx-session-recover (open 실패 회수) "
                            f"lc={getattr(match, 'logical_channel_rx', '?')}",
                            file=sys.stderr, flush=True,
                        )
            except Exception:  # noqa: BLE001
                pass
            return result_state

        _MC._on_dependency_failed = _odf_recover

        # F-release (svc-run-b 실측: 유예 해제 트리거가 ClockAdvanced/타이머
        # 뿐인데 ROC 펌프 회전이 0.5~2.1s로 성겨서, 유예 intent의 직행
        # 대역 [floor 100ms, horizon−슬롯 ≈680ms]를 "이번 해제 땐 이르고
        # 다음 해제 땐 이미 지남"으로 건너뛰는 livelock — 4/4 조각 전패.
        # 모든 MAC 이벤트 핸들러 뒤에 release를 편승시켜 트리거를 이벤트
        # 밀도(실측 ~25/s)로 촘촘하게 만든다. 핸들러의 commands 리스트에
        # 그대로 편입되므로 frozen 등록·배치 배관을 그대로 탄다. 오류는
        # frozen _on_clock_advanced와 동일하게 _finish로 마무리한다.)
        import vdeter.mac.coordinator as _cmod_r

        _CoordPlan = _cmod_r.CoordinatorPlan
        _LcInitial = _cmod_r.EndpointLifecycle.INITIAL
        _orig_handlers = dict(_MC._EVENT_HANDLERS)

        def _with_release(orig_h):
            def _h(self_c, state, envelope, commands):
                out = orig_h(self_c, state, envelope, commands)
                try:
                    if (
                        isinstance(out, _CoordPlan)
                        or not getattr(out, "tx_intents", None)
                        or out.lifecycle is _LcInitial
                    ):
                        return out
                    n_before = len(commands)
                    _err_r: list = []
                    out2 = self_c._release_due_tx_intents(
                        out, envelope.event_id, commands, _err_r
                    )
                    if len(commands) > n_before or _err_r:
                        print(
                            f"[launcher] evt-release emit="
                            f"{len(commands) - n_before} err={len(_err_r)} "
                            f"by={type(envelope.payload).__name__}",
                            file=sys.stderr, flush=True,
                        )
                    if _err_r:
                        return self_c._finish(
                            out2, commands, envelope.event_id, _err_r[0]
                        )
                    return out2
                except Exception:  # noqa: BLE001 - 실패 시 원 결과 유지
                    return out
            return _h

        _MC._EVENT_HANDLERS = {
            evt: (
                handler if evt.__name__ == "ClockAdvancedEvent"
                else _with_release(handler)
            )
            for evt, handler in _orig_handlers.items()
        }
        # 세션 조각 수용 판정의 경계 스냅 (svc-data-10 실측 + 정밀 분석:
        # PHY 역산이 램프 128샘플(416.67µs)을 무조건 빼므로, 탐지 지점이
        # 창 앞머리에 붙으면 received_at이 슬롯 경계 앞으로 넘어가 tdma
        # 채널 분류가 한 칸 밀림 — DC 조각이 관제채널로 오분류·거부.
        # 창=슬롯 정의상, 경계보다 램프 이내로 앞선 시각은 그 경계의 슬롯
        # 소속이다. 수용 판정 입력만 보정하며 다른 관측 계약 무접촉.
        import dataclasses as _dc_rdf

        from vdeter.mac.tdma import (
            CHANNEL_OFFSETS_NANOSECONDS as _RDF_OFFSETS,
            HEXSLOT_NANOSECONDS as _RDF_HEX,
            frame_ordinal_at_nanoseconds as _rdf_frame,
            frame_ordinal_start_nanoseconds as _rdf_fstart,
            position_at_nanoseconds as _rdf_pos,
        )

        _RDF_RAMP_NS = 416_667  # 128 samples @307.2kHz

        _orig_rdf_snap = _MC._receive_data_fragment

        def _rdf_boundary_snapped(self_c, state, **kwargs):
            try:
                decoded = kwargs.get("decoded")
                received = decoded.received_at.total_nanoseconds()
                ahead = received + _RDF_RAMP_NS
                p0 = _rdf_pos(received)
                p1 = _rdf_pos(ahead)
                if (p0.tdma_channel, p0.slot_in_frame) != (
                    p1.tdma_channel, p1.slot_in_frame
                ):
                    slot_start = (
                        _rdf_fstart(_rdf_frame(ahead))
                        + p1.slot_in_frame * _RDF_HEX
                        + _RDF_OFFSETS[p1.tdma_channel]
                    )
                    delta = slot_start - received
                    if 0 < delta <= _RDF_RAMP_NS:
                        kwargs = dict(kwargs)
                        kwargs["decoded"] = _dc_rdf.replace(
                            decoded,
                            received_at=decoded.received_at.shifted(delta),
                        )
                        print(
                            f"[launcher] frag-snap +{delta / 1e3:.0f}us "
                            f"tdma{p0.tdma_channel}->{p1.tdma_channel}",
                            file=sys.stderr, flush=True,
                        )
            except Exception:  # noqa: BLE001 - 보정 실패 시 원 판정 경로
                pass
            return _orig_rdf_snap(self_c, state, **kwargs)

        _MC._receive_data_fragment = _rdf_boundary_snapped
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
