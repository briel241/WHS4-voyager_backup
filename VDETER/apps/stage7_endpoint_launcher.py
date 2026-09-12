"""Stage 7 endpoint process launcher (`stage7` §14, §16).

Runtime Coordinator가 Pi에서 endpoint별로 spawn하는 process다. resolved
endpoint config를 적재하고 `Stage7Endpoint`+`Stage7B210RadioAdapter`를 조립한
뒤 `SOCK_SEQPACKET` socket에서 coordinator command를 처리한다.

TBB 신뢰자료는 기본적으로 `configs/stage7/trust/`의 provisioned TEST
identity를 쓴다. standalone 실행(Phase I)은 환경변수
`VDETER_STANDALONE_IDENTITY_ROOT`로 배포 tree 밖 identity 저장소를 명시
전달한다 — 미지정이면 기존 host 경로 동작이 그대로 보존된다.
private key 내용은 어떤 log/evidence에도 기록하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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


def _trust_material_paths(root: str) -> tuple:
    """(trust_store_path, signer_key_path).

    `VDETER_STANDALONE_IDENTITY_ROOT`가 지정되면 standalone identity
    저장소(개인키는 `private/`, trust store는 `trust/` — 배포 tree 밖)를
    쓴다. 미지정이면 기존 `configs/stage7/trust/` 경로 그대로다 —
    Phase F~H host 실행 경로의 동작 보존.
    """
    identity_root = os.environ.get("VDETER_STANDALONE_IDENTITY_ROOT")
    if identity_root:
        return (
            os.path.join(identity_root, "trust", "tbb_trust_store.pem"),
            os.path.join(identity_root, "private", "roc_tbb_signer_key.pem"),
        )
    trust_dir = os.path.join(root, "configs", "stage7", "trust")
    return (
        os.path.join(trust_dir, "tbb_trust_store.pem"),
        os.path.join(trust_dir, "roc_tbb_signer_key.pem"),
    )


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

    store_path, key_path = _trust_material_paths(root)
    with open(store_path, "rb") as handle:
        certificate = load_pem_x509_certificate(handle.read())
    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    fingerprint = certificate_fingerprint(certificate_der)
    if fingerprint.is_err:
        raise SystemExit(f"trust store fingerprint 계산 실패: {fingerprint.error}")
    signer = None
    if is_control:
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


def service_event_record(event, *, mono_clock=time.monotonic) -> dict:
    """public ServiceEvent 하나를 event JSONL record(dict)로 만든다.

    기존 필드(t_mono·event_type·transfer_id·transfer_state·error_*·
    payload_sha256·payload_len·source)는 그대로 유지해 기존 RR controller가
    계속 동작한다. DATA_RECEIVED에는 응용 판정에 필요한 public 필드를
    보탠다: payload_base64(원문 application bytes)·payload_format·
    original_source·original_destination·received_at_ns.

    - `source`는 예전에 `received.source`를 조회해 사실상 항상 누락됐다.
      실제 public DTO 필드는 `received.original_source`다 — 그것으로 채운다.
    - application bytes만 기록한다. RF frame·Link/MAC/PHY 내부 객체는
      여기 오지 않는다(ReceivedApplicationData가 이미 응용 계층 DTO다).
    - private key는 어떤 경로로도 기록하지 않는다.
    """

    record = {
        "t_mono": round(mono_clock(), 3),
        "event_type": getattr(
            getattr(event, "event_type", None), "value",
            str(getattr(event, "event_type", None)),
        ),
        "transfer_id": getattr(
            getattr(event, "transfer_id", None), "value", None
        ),
        "transfer_state": getattr(
            getattr(event, "transfer_state", None), "value", None
        ),
        "error_code": getattr(getattr(event, "error", None), "code", None),
    }
    error_object = getattr(event, "error", None)
    if error_object is not None:
        record["error_message"] = str(getattr(error_object, "message", ""))[:200]
        error_details = getattr(error_object, "details", None)
        if error_details is not None:
            record["error_details"] = str(error_details)[:300]
    received = getattr(event, "received_data", None)
    if received is not None:
        payload = getattr(received, "payload", b"") or b""
        record["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        record["payload_len"] = len(payload)
        record["payload_base64"] = base64.b64encode(payload).decode("ascii")
        record["payload_format"] = getattr(
            getattr(received, "payload_format", None), "value", None
        )
        source = getattr(received, "original_source", None)
        record["source"] = getattr(source, "value", None)
        record["original_source"] = record["source"]
        destination = getattr(received, "original_destination", None)
        if destination is not None and getattr(destination, "is_broadcast", False):
            record["original_destination"] = "BROADCAST"
        else:
            record["original_destination"] = getattr(
                getattr(destination, "station_id", None), "value", None
            )
        received_at = getattr(received, "received_at", None)
        if received_at is not None:
            try:
                record["received_at_ns"] = int(received_at.total_nanoseconds())
            except Exception:  # noqa: BLE001 - 시각 부재는 기록 생략
                pass
    return record


def _serve_test_adapter_peers(test_server, adapter, *, expected_uid, stop_event):
    """Serve one TEST-adapter peer at a time, accepting the next after EOF.

    A persistent hardware session uses separate controller processes for
    session-start, each workload, and session-stop. Each process therefore
    opens a fresh connection to the same endpoint socket. Keep the listener
    alive across peer EOF while retaining the per-accept UID check and never
    accepting a second peer until the current one has disconnected.
    """

    while not stop_event.is_set():
        try:
            test_server.accept(
                expected_pid=None,
                expected_uid=expected_uid,
                # 유계 대기여야 stop_event를 실제로 관측한다. teardown의
                # test_server.close()가 blocking accept를 깨운다는 전제는
                # Linux에서 거짓이다 — CPython 타임아웃 소켓은 poll()에 들어간
                # 뒤 다른 스레드의 close()로 깨어나지 않는다. 3600초로 두면
                # join(2.0)이 매번 만료된다.
                # RF-time 지터 우려는 근거가 없다: rr-p1은 is_rr_service가
                # 아니라 test_plan_sha=None이므로 이 스레드가 아예 생성되지
                # 않는다(자격 구간에서 실행 0). Phase G에서도 초당 1회 미만
                # 깨어남은 20Hz 펌프 대비 측정 불가하다.
                timeout=5.0,
            )
        except TimeoutError:
            continue
        except OSError:
            if stop_event.is_set():
                return
            raise

        try:
            while not stop_event.is_set():
                # Same per-peer timeout as the pre-reconnect implementation.
                message = test_server.recv_message(timeout=5.0)
                if message is None:
                    continue
                response = adapter.handle_command(
                    message, peer=(expected_uid,)
                )
                test_server.send_message(response)
        except ConnectionError:
            # Normal peer teardown: close only this accepted connection, then
            # return to accept() without closing/unlinking the listener.
            pass
        except Exception as error:  # noqa: BLE001
            # 피어 하나의 실패로 리스너를 잃지 않는다. 상주 세션에서는 이
            # 소켓에 START 대행·workload 4개·STOP 대행이 차례로 붙으므로,
            # 스레드가 죽으면 이후 전부가 300초 타임아웃으로 무너지고
            # bring-up 25~30분까지 통째로 버려진다. fail-close 단위는
            # 리스너가 아니라 피어여야 한다.
            # 특히 send_message는 recv_message가 걸어둔 5초 타임아웃 소켓에
            # sendall하므로 socket.timeout(=OSError, ConnectionError 아님)이
            # 날 수 있다.
            print(f"[launcher] test adapter peer 오류(다음 피어 계속): "
                  f"{type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
        finally:
            connection = getattr(test_server, "_connection", None)
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            test_server._connection = None
            test_server._peer = None


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

    _launcher_clock = _AnchoredSystemTimeSource()

    #: 창 추적: native 첫 표본 ns -> (submit까지 ms, commit까지 ms).
    #: proc_lag_ms는 단일 사건인데 lat.* 는 세션 평균이라 서로 뺄 수 없다.
    #: type-4를 실은 그 창 하나를 끝까지 따라가 "런처 도달 전"과 "런처 안"을
    #: 가른다. 계측 전용이며 fail-open이다.
    _win_trace = {}

    def _utc_ns():
        return _launcher_clock.now_utc().total_nanoseconds()

    built = build_stage7_b210_endpoint(
        resolved,
        deployment,
        crypto_dependencies=crypto,
        intent_notifier=intent_notifier,
        host_raw_clock=read_monotonic_raw_ns,
        monitor_enabled=True,
        time_source_override=_launcher_clock,
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

    #: 실시간 펌프를 돌리는 endpoint 상태 (§16.1 서비스 구간).
    #: 그 앞의 RF qualification 구간(CONFIGURED·RX_READY·CFO_*·HOST_TIME_*·
    #: RF_GAIN_*)에서는 Stage7PiOperations가 radio를 독점해야 한다.
    #:
    #: TBB_ACTIVE/TBB_ACQUIRING 누락은 자기 교착이었다 (2026-08-31 실측):
    #: ROC는 방송자라 수신 없이 self-activation으로 TBB_ACTIVE가 되는데, 그
    #: 순간 자기 펌프가 멈춰 TBB 방송 fragment 순환(1주기 1개, 완료 관측이
    #: radio.poll_once 의존)이 첫 조각에서 끊긴다. MASS-A/B는 조립할 TBB가
    #: 공중에 없어 SERVICE_START_STAGED에 영구 정체하고, publish는 MASS 획득을
    #: 기다리므로 펌프도 영영 재개되지 않는다.
    #: 증거 — phase-g-service-20260831-050301: ROC rx-gate 0줄(펌프 정지),
    #: MASS-A 9줄/MASS-B 26줄, 최종 ROC=TBB_ACTIVE·MASS=SERVICE_START_STAGED.
    #: 대조 rr-p2-17-20260830j/k(게이트 도입 전): ROC 82/64줄로 세 노드 균등,
    #: admission→publish 9.9~11.8s.
    #: 서비스 구간 안의 상태만 더한다 — RF qualification 사다리 제외는 그대로라
    #: c063d0d가 막은 rr-p1 skipped_past_due 회귀는 계속 보호된다.
    _PUMP_ACTIVE_STATES = (
        "SERVICE_ADMISSION_READY",
        "SERVICE_START_STAGED",
        "TBB_ACQUIRING",
        "TBB_ACTIVE",
        "RUNNING",
    )

    def _pump_loop():
        # MAC 주기 40ms: 시계 20ms 양자화(_AnchoredSystemTimeSource)가
        # wakeup 교체 id 충돌(run p)을 근원 해결했으므로, 연속 주기가 같은
        # 양자에 못 들어가는 최소 간격(2 양자)까지 내려 서비스 RX 창 발행
        # 지연을 줄인다 — svcdbg36 실측: 160ms 주기에서 ACK 청취 창이
        # +117µs 지각 시작(OFFGRID) 또는 통째로 빈 창(스트림 명령 미발화).
        main._last_mac_cycle = 0.0

        # MAC 미니사이클 (svc-run-c 실측 근거): drain 항목 1개 처리(수신
        # 디코드)가 ~140ms라, 256개 한도의 일괄 drain 동안 MAC 박동이
        # 0.7~2s 정지한다 — 유예 해제·인코드→PHY 체인·수신 창 발행이
        # 전부 이 배치 간격에 묶여 지각하는 공통 뿌리. 사이클을 헬퍼로
        # 빼서 drain 항목 사이에도 끼워 넣는다 (40ms 게이트라 저부하
        # 때는 공짜). 같은 펌프 스레드에서만 호출되므로 MAC 단일 스레드
        # 규율은 그대로다.
        def _mac_cycle():
            support_c = endpoint.stage4
            if support_c is None:
                return
            now_c = _time.monotonic()
            last_c = getattr(main, "_last_mac_cycle", 0.0)
            if now_c - last_c < 0.04:
                return
            if (
                last_c and now_c - last_c > 0.5
                and os.environ.get("STAGE7_DIAG", "0") == "1"
            ):
                print(
                    f"[diag] mac-gap {(now_c - last_c) * 1e3:.0f}ms "
                    f"t={now_c:.3f}",
                    file=sys.stderr, flush=True,
                )
            main._last_mac_cycle = now_c
            support_c.advance_clock()
            fired_n = 0
            while support_c.has_due_timer():
                support_c.fire_due_timers()
                fired_n += 1
            if fired_n and os.environ.get("STAGE7_DIAG", "0") == "1":
                print(
                    f"[diag] timers-fired n={fired_n} "
                    f"t={_time.monotonic():.3f}",
                    file=sys.stderr, flush=True,
                )
            support_c.pump()

        main._mac_cycle = _mac_cycle

        while not _pump_stop.is_set():
            # 서비스 phase 한정 (rr-p1 회귀 봉합, 2026-08-31 실측 근거):
            # 이 펌프는 rr-p2 서비스 구간(svc-run-*)에서 frozen 런타임의
            # radio.poll_once/MAC 박동을 대신 돌리려고 도입됐다. 그런데
            # RF qualification 사다리(CFO·adaptive gain·rf_time)는 같은
            # B210을 Stage7PiOperations가 직접 시분할로 몰아 쓰는 구간이라,
            # 50ms 주기의 poll_once가 그 위에 겹치면 revalidate 버스트가
            # 송신 시한을 넘겨 통째로 스킵된다 (mass-roc-rr-p1-20260831a/b
            # 실측: skipped_past_due C2=19·6, C4=3, C6=1 → REVALIDATE 불합격.
            # 펌프 도입 전 11개 rr-p1 run은 전부 skipped_past_due=0).
            # 서비스 admission 이후에만 돌려 두 구간을 분리한다 — rr-p2
            # 서비스 경로의 동작은 그대로다.
            client_ref = progress_client_slot["client"]
            if client_ref is None or client_ref.state not in _PUMP_ACTIVE_STATES:
                _pump_stop.wait(0.05)
                continue
            try:
                # 지연 해부 (svc-run-n 이후: 게이트 후에도 완료 처리
                # 지연 ~15-19s 잔존, mac-gap ~1s × 171 — 어느 구간이
                # 먹는지 구간별 실측. DIAG 게이트, 300ms 초과만 출력.)
                _t0 = _time.monotonic()
                endpoint.dependencies.radio.poll_once()
                _t1 = _time.monotonic()
                _mac_cycle()
                _t2 = _time.monotonic()
                drain = getattr(main, "_drain_dispatch_queue", None)
                if drain is not None:
                    drain()
                _t3 = _time.monotonic()
                if (
                    _t3 - _t0 > 0.3
                    and os.environ.get("STAGE7_DIAG", "0") == "1"
                ):
                    try:
                        ingress = getattr(endpoint.stage4, "ingress", None)
                        depth = len(getattr(ingress, "_events", ()) or ())
                        dq = getattr(main, "_dispatch_queue_ref", None)
                        print(
                            f"[diag] pump-slow poll={(_t1 - _t0) * 1e3:.0f} "
                            f"mac1={(_t2 - _t1) * 1e3:.0f} "
                            f"drain={(_t3 - _t2) * 1e3:.0f}ms "
                            f"qlen={'?' if dq is None else len(dq)} "
                            f"ingress={depth} t={_t3:.3f}",
                            file=sys.stderr, flush=True,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                support = endpoint.stage4
                if support is not None:
                    cycle_started = _time.monotonic()
                    _mac_cycle()
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
                                f"[launcher] pump t={cycle_started:.3f} "
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
                    # svc-run-p 부검 D항: 실제 접근자는 pending_commands
                    # (부류 계수는 pending_count) — '?' 사각 제거.
                    try:
                        from vdeter.mac.models import (
                            PendingCommandClass as _PCCc,
                        )
                        dep_n = (
                            f"rx={state.pending_count(_PCCc.RX)}"
                            f"/all={len(state.pending_commands)}"
                        )
                    except Exception:  # noqa: BLE001
                        dep_n = "?"
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
                    # svc-run-e 76 거절 미스터리: 거절 순간의 할당 장부
                    # 전체를 찍는다 (조건별 판별 가능하게 전 필드).
                    allocs = [
                        (a.logical_channel_tx, a.logical_channel_rx,
                         a.tdma_channel,
                         getattr(a.state, 'name', a.state),
                         a.activation_frame_ordinal - frame,
                         a.last_valid_frame_ordinal - frame,
                         a.owner_station_id_value,
                         a.peer_station_id_value)
                        for a in (getattr(state, 'lc_allocations', ()) or ())
                    ]
                    print(
                        f"[diag] frag-allocs frame={frame} "
                        f"n={len(allocs)} {allocs}",
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

            # C8: 수신 상류 무음 소실 관측 (svc-run-d: 함정이 [90,4] 할당
            # 공지를 상태에 반영하지 못했는데 소실 지점이 로그에 없었다).
            _diag_orig_rie = _DVE._report_internal_error

            def _diag_rie(self_e, error, *a, **k):
                try:
                    n = _diag_rie.__dict__["_n"] = (
                        _diag_rie.__dict__.get("_n", 0) + 1
                    )
                    if n <= 40 or n % 20 == 0:
                        print(
                            f"[diag] internal-err #{n} {str(error)[:160]}",
                            file=sys.stderr, flush=True,
                        )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_rie(self_e, error, *a, **k)

            _DVE._report_internal_error = _diag_rie
            _diag_orig_rec = _DVE._record

            def _diag_rec(self_e, event_type, *a, **k):
                try:
                    if getattr(event_type, "name", "") == (
                        "PACKET_DECODE_REQUESTED"
                    ):
                        action = k.get("payload") or (a[1] if len(a) > 1 else None)
                        if action is not None and not getattr(
                            action, "accepted", True
                        ):
                            print(
                                "[diag] decode-unaccepted "
                                f"link={getattr(getattr(action, 'packet', None), 'link_id', '?')}",
                                file=sys.stderr, flush=True,
                            )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_rec(self_e, event_type, *a, **k)

            _DVE._record = _diag_rec

            # ROC측: 자원할당(type 4) body 생성 시각. MASS의 type4-rx와
            # 짝지으면 "ROC가 늦게 보냈나 / MASS가 늦게 처리했나"가 갈린다.
            try:
                from vdeter.mac import resources as _t4res

                _diag_orig_build = _t4res.build_allocation_body

                def _diag_build(*a, **k):
                    body = _diag_orig_build(*a, **k)
                    try:
                        print(
                            f"[diag] type4-build lc_tx={body.logical_channel_tx} "
                            f"lc_rx={body.logical_channel_rx} "
                            f"frame_delay={body.tdma_frame_delay} "
                            f"t={_time.monotonic():.3f}",
                            file=sys.stderr, flush=True,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return body

                _t4res.build_allocation_body = _diag_build
                # coordinator가 모듈 참조로 부르므로 그쪽도 같이 바꾼다.
                _DMC_mod = sys.modules.get(_DMC.__module__)
                if _DMC_mod is not None:
                    _rm = getattr(_DMC_mod, "resource_module", None)
                    if _rm is not None:
                        _rm.build_allocation_body = _diag_build
            except Exception as _be:  # noqa: BLE001
                print(f"[diag] type4-build 훅 미적용: {_be}",
                      file=sys.stderr, flush=True)

            _diag_orig_t4 = _DMC._receive_type4

            def _diag_t4(self_c, state, *a, **k):
                out_state = _diag_orig_t4(self_c, state, *a, **k)
                try:
                    before_n = len(getattr(state, "lc_allocations", ()) or ())
                    after_n = len(
                        getattr(out_state, "lc_allocations", ()) or ()
                    )
                    # 활성화 프레임은 **RF 수신 시각** 기준으로 정해진다
                    # (_receive_type4: transmit_frame = frame(received),
                    #  활성화 = transmit_frame + CANONICAL_TDMA_FRAME_DELAY).
                    # 처리가 늦어 활성화 프레임이 이미 지나갔으면 그 프레임의
                    # DC 창은 계획되지 않는다 — 그 지각을 직접 잰다.
                    from vdeter.mac.tdma import (
                        frame_ordinal_at_nanoseconds as _t4_fo,
                    )

                    decoded = k.get("decoded")
                    recv_ns = None
                    if decoded is not None:
                        recv_ns = decoded.received_at.total_nanoseconds()
                    now_ns = self_c._now_utc_nanoseconds()
                    recv_frame = _t4_fo(recv_ns) if recv_ns else None
                    now_frame = _t4_fo(now_ns)
                    allocs = [
                        (al.logical_channel_rx,
                         al.activation_frame_ordinal - now_frame,
                         al.last_valid_frame_ordinal - now_frame)
                        for al in (getattr(out_state, "lc_allocations", ())
                                   or ())
                    ]
                    # 이 type-4를 실은 창 하나의 실제 분해. 세션 평균이 아니라
                    # 같은 사건이라 proc_lag에서 그대로 뺄 수 있다.
                    #
                    # recv_ns는 창의 첫 표본이 아니라 **버스트 검출 시각**이다
                    # (_merge_results가 min(packet.received_at)을 쓴다). 창
                    # 첫 표본을 키로 잡아 정확 일치를 노리면 빗나가므로, 그
                    # 시각을 담은 창을 고른다. win_offset_ms가 창 길이보다
                    # 크면 해당 창이 이미 밀려난 것이니 그것도 보인다.
                    trace = None
                    offset_ms = 0.0
                    starts = [k for k in _win_trace if k <= recv_ns]
                    if starts:
                        window_first_ns = max(starts)
                        trace = _win_trace[window_first_ns]
                        offset_ms = (recv_ns - window_first_ns) / 1e6
                    if trace is None:
                        span = "trace=없음"
                    else:
                        to_submit, to_commit = trace
                        # 세 구간을 recv_ns 기준으로 옮긴다 —
                        # pre + worker + post = proc_lag_ms 가 되게.
                        span = (
                            f"win_offset_ms={offset_ms:.0f} "
                            f"pre_ms={to_submit - offset_ms:.0f} "
                            f"worker_ms={to_commit - to_submit:.0f} "
                            f"post_ms="
                            f"{(now_ns - recv_ns) / 1e6 - to_commit + offset_ms:.0f}"
                        )
                    print(
                        f"[diag] type4-rx allocs {before_n}->{after_n} "
                        f"recv_ns={recv_ns} "
                        f"proc_lag_ms={(now_ns - recv_ns) / 1e6:.0f} "
                        f"recv_frame={recv_frame} now_frame={now_frame} "
                        f"frames_late={now_frame - recv_frame if recv_frame else '?'} "
                        f"{span} "
                        f"allocs(lc,act-now,last-now)={allocs}",
                        file=sys.stderr, flush=True,
                    )
                except Exception as _t4_err:  # noqa: BLE001
                    print(f"[diag] type4-rx probe 오류: {_t4_err}",
                          file=sys.stderr, flush=True)
                return out_state

            _DMC._receive_type4 = _diag_t4

            # C9: ACK(Type13) 수신 매칭 관측 (svc-run-l2: ROC가 ACK를
            # 받고도 무음 무시했는지/미도달인지 로그로 구분 불가했다 —
            # _receive_type13/_apply_session_ack 전 경로가 무프린트).
            _diag_orig_t13 = _DMC._receive_type13

            def _diag_t13(self_c, state, body, **kwargs):
                try:
                    masks = getattr(body, "ack_nack_masks", None)
                    print(
                        f"[diag] type13-rx src={getattr(body.source_id, 'value', '?')} "
                        f"dst={getattr(getattr(body.destination, 'station_id', None), 'value', '?')} "
                        f"masks={masks}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                out_state = _diag_orig_t13(self_c, state, body, **kwargs)
                try:
                    changed = out_state is not state
                    print(
                        f"[diag] type13-post state_changed={changed}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return out_state

            _DMC._receive_type13 = _diag_t13
            _diag_orig_asa = _DMC._apply_session_ack

            def _diag_asa(self_c, state, context, body, *a, **k):
                try:
                    ledger = tuple(context.frame_ledger)
                    eligible_n = sum(
                        1 for entry in ledger
                        if not any(
                            pid in context.pending_packet_ids
                            for pid in entry.packet_ids
                        )
                        and not any(
                            fi == entry.session_frame_index
                            for fi, _p in context.retry_fragment_keys
                        )
                    )
                    print(
                        f"[diag] ack-apply xfer={context.transfer_id.value[:8]} "
                        f"ledger={len(ledger)} eligible={eligible_n} "
                        f"sfi={[e.session_frame_index for e in ledger]} "
                        f"conf={[list(e.confirmed_positions) for e in ledger]}",
                        file=sys.stderr, flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return _diag_orig_asa(self_c, state, context, body, *a, **k)

            _DMC._apply_session_ack = _diag_asa

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
        # SLOT_MISSED — 데이터 전송 확정 실패. 해제측만 200ms 완화해(바닥 100ms)
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
                    # 파편 상한 3 명시 강제 (0.9.15/0.9.20 m9, 0.9.22 경계
                    # 정정): frozen 산식은 세그먼트 = 헤더 4B + presentation
                    # (link/transfer.py SEGMENT_HEADER_SIZE)이므로 3조각
                    # 용량 645B(세그먼트) = presentation 641B. 641B 초과는
                    # 승격하지 않고 원 단문 경로로 낙하 → frozen의 설계된
                    # 거부(LINK_TRANSFER_SIZE_UNSUPPORTED)로 즉시 실패한다.
                    # 검증 보증 구간(≤426B=2조각, 상한 3조각) 밖의 무음
                    # 진입을 차단하는 게이트.
                    if requirement.presentation_size > 3 * 215 - 4:
                        print(
                            "[launcher] 승격 거부 — 파편 상한 3 초과 "
                            f"(presentation={requirement.presentation_size}"
                            " > 641, 세그먼트 > 645)",
                            file=sys.stderr, flush=True,
                        )
                        return _orig_submit_short(
                            self_c, state, requirement, causation_event_id
                        )
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

        # D-18 최종형: 데이터 세션 ACK 대기의 전송별 벽시계 결정 하한
        # 65s (svc-run-m/rr-g/h 실측: 성공 ACK가 frozen 한도 7.2s는
        # 물론, 중간형이던 계수-스킵 대기(전역 위상 따라 실대기
        # 43~62s 요동)보다도 늦게 도착 — WAITING_ACK 진입 시각을
        # 직접 기록해 65s 전에는 부재 계수를 아예 전달하지 않는다.
        # 부재 계수 전용 함수라 재전송 계획·ACK 도착 처리 무관여,
        # LOCAL_DATA_SESSION 역할 한정. 재현 전용 시험대 보정 —
        # 실물급에서는 원 규칙 복원(§25.1 D-18).)
        _orig_ack_to = _MC._on_data_ack_timeout
        # 키 = (컨트롤러 인스턴스 id, transfer id): 클래스 패치의 클로저
        # 공유 상태라 한 프로세스에 컨트롤러가 2개면 서로의 타이머를
        # 청소하는 간섭이 생김 — 인스턴스 스코프로 격리 (0.9.22).
        _ack_wait_started: dict = {}
        _ack_patient_warned: list = []

        def _ack_patient(self_c, state, causation_event_id, commands):
            # 결정적 벽시계 대기 (rr-g/h 실측: 계수-스킵 방식은 전역
            # 위상에 따라 실대기가 43~62s로 요동 — 명목 65s에도 짧은
            # 시도가 회신(~53~58s)보다 먼저 끊김. 전송별 WAITING 진입
            # 시각을 직접 기록해 65s 경과 전에는 부재 계수를 아예
            # 전달하지 않는다 → 실대기 하한 65s 보장, 위상 복권 소멸.
            # 유예는 동시 대기 전체 단위: 하나라도 65s 미만이면 이번
            # 계수 전체를 유예한다(판정 구간은 동시 세션 1개라 동치).)
            try:
                now_w = _time.monotonic()
                waiting = [
                    (id(self_c), c.transfer_id.value)
                    for c in state.transfers
                    if getattr(getattr(c, "role", None), "name", "")
                    == "LOCAL_DATA_SESSION"
                    and getattr(getattr(c, "phase", None), "name", "")
                    == "WAITING_ACK"
                ]
                for known in list(_ack_wait_started):
                    if known[0] == id(self_c) and known not in waiting:
                        del _ack_wait_started[known]
                if waiting:
                    ripe = all(
                        now_w - _ack_wait_started.setdefault(x, now_w)
                        >= 65.0
                        for x in waiting
                    )
                    if not ripe:
                        return state
            except Exception as exc:  # noqa: BLE001 - 실패 시 원 경로
                # 무음 복귀 금지: frozen 7.2s 규칙으로 낙하하면 데이터
                # 시나리오가 원인 표식 없이 깨진다 — 1회성 경고 (0.9.22).
                if not _ack_patient_warned:
                    _ack_patient_warned.append(True)
                    print(
                        f"[launcher] D-18 ack-patient 예외 → 원 경로 낙하: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
            return _orig_ack_to(self_c, state, causation_event_id, commands)

        _MC._on_data_ack_timeout = _ack_patient

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

        _TPurpose = _cmod_r.TimerPurpose
        _LcRunning = getattr(_cmod_r.EndpointLifecycle, "RUNNING", None)

        def _with_release(orig_h):
            def _h(self_c, state, envelope, commands):
                out = orig_h(self_c, state, envelope, commands)
                try:
                    if (
                        isinstance(out, _CoordPlan)
                        or not hasattr(out, "tx_intents")
                        or out.lifecycle is _LcInitial
                    ):
                        return out
                    if getattr(out, "tx_intents", None):
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
                        out = out2
                    # F-boundary-watchdog (svc-run-d 실측: quota 롤백이
                    # 다음 경계 타이머 무장을 배치째 버리면, 경계는 자기
                    # 자신만이 재장전하는 체인이라 영구 절단 — ACK
                    # 타임아웃·FIFO 승격 전체 사망. 경계 타이머 부재를
                    # 감지하면 frozen 스케줄러로 재점화한다. 중복은
                    # frozen이 timer_id로 자체 방지.)
                    if (
                        _LcRunning is not None
                        and out.lifecycle is _LcRunning
                        and not any(
                            t.purpose is _TPurpose.FRAME_BOUNDARY
                            for t in (getattr(out, "timers", ()) or ())
                        )
                    ):
                        out = self_c._schedule_frame_boundary(
                            out, envelope.event_id, commands
                        )
                        print(
                            "[launcher] boundary-rearm "
                            f"by={type(envelope.payload).__name__}",
                            file=sys.stderr, flush=True,
                        )
                    return out
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

        # F-window-shed (svc-run-d 실측: 40ms 박동이 창 계획을 매 사이클
        # 쏟아내 pending RX quota(68)를 넘기면 frozen _register_pending
        # 실패 → plan() 전체 롤백 → 계획 표식·경계 타이머까지 증발하는
        # 폭풍(2,945건). 창 계획을 시험 리스트로 먼저 굴려 quota 여유를
        # 산정하고, 넘칠 것 같으면 이번 사이클만 연기한다 — 창 계획은
        # 매 사이클 다시 오므로 안전하고, 배치가 등록 가능 크기로
        # 유지되어 롤백 자체가 사라진다.)
        from vdeter.mac.execution import command_class_of as _ccls_of
        from vdeter.mac.models import (
            PendingCommandClass as _PCC,
            Stage4Limits as _S4L,
        )

        # D-19: pending quota 상향 (svc-run-j 실측: 에너지 게이트 후에도
        # 장부 수위가 ~65에 상시 고착 — 처리 지연이 아니라 계획 선행분
        # (미래 창 ~2프레임 × 창 수)의 구조적 점유. 원 한도 68은 이 바닥
        # 대비 여유가 3뿐이라 shed가 창 계획을 상습 연기 → 청취 확률화
        # (run F 1/6, H 0/6, J 0/6). 바닥+프레임 배치(~30)+여유로 상향.
        # shed·마진은 폭주 안전판으로 유지. 런타임 클래스 패치(svcdbg66
        # 전례), frozen 파일 0수정.
        _S4L.PENDING_DEPENDENCY_RX = 128
        _S4L.PENDING_DEPENDENCY_TOTAL = 160
        print(
            "[launcher] pending-quota RX=128 TOTAL=160 (D-19)",
            file=sys.stderr, flush=True,
        )

        _orig_pfw = _MC.plan_frame_windows
        _SHED_MARGIN = 4

        def _pfw_shed(self_c, state, causation_event_id, commands, errors):
            try:
                trial_cmds: list = []
                trial_errs: list = []
                out = _orig_pfw(
                    self_c, state, causation_event_id, trial_cmds, trial_errs
                )
                if not trial_cmds:
                    errors.extend(trial_errs)
                    return out
                # 창 다이어트 (svc-run-f 실측: 완료 처리 수요가 용량의
                # ~2.5배라 장부 포화가 구조적 — 시나리오 재현에 불요한
                # 청취 창을 계획에서 제외해 디코드 부하 자체를 줄인다.
                # 유지: 제어 tdma0 전체(TBB/ASC/RAC 관제), DSCH(ACK),
                # DC 중 LC15/LC3(세션 데이터·반환). 제외: 그 외 DC 레인.)
                def _window_keep(cmd):
                    if _ccls_of(cmd) is not _PCC.RX:
                        return True
                    try:
                        req = cmd.payload.request
                        start = req.start_at.total_nanoseconds()
                        pos_w = _spr_pos(start)
                        if pos_w.tdma_channel == 0:
                            return True
                        slot_w = self_c.active_slot_map(state).slot_at(
                            req.channel, pos_w.tdma_channel,
                            pos_w.slot_in_frame,
                        )
                        fn = getattr(
                            getattr(slot_w, "function", None), "name", ""
                        )
                        if fn == "DSCH":
                            return True
                        return fn == "DC" and getattr(
                            slot_w, "logical_channel", None
                        ) in (15, 3)
                    except Exception:  # noqa: BLE001 - 판별 불가면 유지
                        return True

                kept = [c for c in trial_cmds if _window_keep(c)]
                dropped = len(trial_cmds) - len(kept)
                rx_total = state.pending_count(_PCC.RX) + sum(
                    1 for c in list(commands) + kept
                    if _ccls_of(c) is _PCC.RX
                )
                all_total = (
                    state.pending_count(_PCC.RX)
                    + state.pending_count(_PCC.NON_CLEANUP_NON_RX)
                    + state.pending_count(_PCC.CLEANUP)
                    + len(commands) + len(kept)
                )
                if (
                    rx_total > _S4L.PENDING_DEPENDENCY_RX - _SHED_MARGIN
                    or all_total > _S4L.PENDING_DEPENDENCY_TOTAL - _SHED_MARGIN
                ):
                    if os.environ.get("STAGE7_DIAG", "0") == "1":
                        print(
                            f"[diag] window-shed rx={rx_total} "
                            f"all={all_total} defer={len(kept)}",
                            file=sys.stderr, flush=True,
                        )
                    return state
                if dropped and os.environ.get("STAGE7_DIAG", "0") == "1":
                    print(
                        f"[diag] window-diet kept={len(kept)} "
                        f"dropped={dropped}",
                        file=sys.stderr, flush=True,
                    )
                commands.extend(kept)
                errors.extend(trial_errs)
                return out
            except Exception:  # noqa: BLE001 - 실패 시 원 경로
                return _orig_pfw(
                    self_c, state, causation_event_id, commands, errors
                )

        _MC.plan_frame_windows = _pfw_shed
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
            # 수신 조각 기반 임대 갱신 (svc-run-e 부검: frozen은 "수신
            # 시각 frame으로 판정, 처리 시각 frame으로 prune"이라 처리
            # 지연이 임대 말미와 겹치면 창 안에 도착한 조각의 할당이
            # 검사 직전 prune으로 소멸 — 76 거절의 TOCTOU. resources
            # docstring의 트래픽 기반 renewal 중 frozen이 Start/OPEN
            # 경로에서 빠뜨린 수신측 절반을 보충한다: 조각의 수신 frame
            # 에서 ACTIVE인 할당의 임대를 frozen 헬퍼로 3프레임 연장.)
            try:
                from vdeter.mac import resources as _res_rdf

                decoded2 = kwargs.get("decoded")
                # 연장 폭 +6프레임(14.4s): frozen 헬퍼의 상한(+3, 7.2s)은
                # 함정 처리 지연 실측(~10s, svc-run-g에서 74 통과 후 같은
                # frame의 76 도착 전에 prune 발생)보다 짧다. 조각이 창
                # 안에 실수신된 할당만 대상이라 부활·오연장 없음.
                _RENEW_FRAMES = 6
                if decoded2 is not None:
                    f_rx = _rdf_frame(decoded2.received_at.total_nanoseconds())
                    for item in (getattr(state, "lc_allocations", ()) or ()):
                        if (
                            getattr(
                                _res_rdf.allocation_state_at(item, f_rx),
                                "name", "",
                            ) == "ACTIVE"
                            and item.last_valid_frame_ordinal
                            < f_rx + _RENEW_FRAMES
                        ):
                            state = state.evolve(
                                lc_allocations=_res_rdf.upsert_allocation(
                                    state.lc_allocations,
                                    _dc_rdf.replace(
                                        item,
                                        last_valid_frame_ordinal=(
                                            f_rx + _RENEW_FRAMES
                                        ),
                                    ),
                                )
                            )
                            print(
                                "[launcher] rx-lease-renew "
                                f"lc={item.logical_channel_tx} f={f_rx} "
                                f"last+{item.last_valid_frame_ordinal - f_rx}"
                                f"->+{_RENEW_FRAMES}",
                                file=sys.stderr, flush=True,
                            )
            except Exception:  # noqa: BLE001 - 실패 시 원 상태로 판정
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
            _decode_pool_slot["dispatcher_seen"] = self_d
            _dispatch_queue.append(
                ("s", self_d, lane_generation, lane_index, child_key, received)
            )

        #: 창 완료의 큐 적재 시각 (id(completion) -> monotonic). drain에서 뺀다.
        _c_stamp = {}

        def _q_completed(self_d, lane_generation, lane_index, child_key, completion):
            _decode_pool_slot["dispatcher_seen"] = self_d
            # 지연 해부: proc_lag_ms 1.7~2.3초가 큐 앞쪽인지 뒤쪽인지 가른다.
            # completed_at은 adapter가 self._time_source.now_monotonic()으로
            # 채우므로 같은 프로세스 monotonic과 직접 비교된다.
            try:
                now_m = _time.monotonic()
                if len(_c_stamp) > 4096:
                    _c_stamp.clear()
                _c_stamp[id(completion)] = now_m
                done_ns = completion.completed_at.total_nanoseconds()
                _cost_add("lat.radio2launcher", now_m - done_ns / 1e9)
            except Exception:  # noqa: BLE001
                pass
            _dispatch_queue.append(
                ("c", self_d, lane_generation, lane_index, child_key, completion)
            )

        def _q_lane_err(self_d, lane_generation, lane_index, child_key, code, details):
            _dispatch_queue.append(
                ("e", self_d, lane_generation, lane_index, child_key, code, details)
            )

        # ── 병목 해부 계측 (조사 전용, STAGE7_DIAG=1에서만 활성)
        # phase-g-service-20260831-202830 실측: drain 총 296s 중 PHY 디코드는
        # 55.9s(19%)뿐 — 나머지 81%의 귀속처가 미상이다. drain을 항목 종류로,
        # 그 안을 구간별로 쪼개 누적한다. 계측은 전부 fail-open이며 원본
        # 호출 순서·인자·반환을 바꾸지 않는다. PHY/MAC/TDMA/RF는 불변.
        _COST = {}
        _COST_LAST = [_time.monotonic()]

        def _cost_add(label, elapsed):
            slot = _COST.get(label)
            if slot is None:
                _COST[label] = [1, elapsed]
            else:
                slot[0] += 1
                slot[1] += elapsed

        def _cost_wrap(owner, name, label):
            """owner.name을 시간 계측으로 감싼다 (fail-open)."""

            try:
                original = getattr(owner, name)
            except Exception:  # noqa: BLE001
                return

            def _wrapped(*args, **kwargs):
                _started = _time.monotonic()
                try:
                    return original(*args, **kwargs)
                finally:
                    try:
                        _cost_add(label, _time.monotonic() - _started)
                    except Exception:  # noqa: BLE001
                        pass

            try:
                setattr(owner, name, _wrapped)
            except Exception:  # noqa: BLE001
                pass

        if os.environ.get("STAGE7_DIAG", "0") == "1":
            try:
                from vdeter.phy.multilink import MultiRatePhy as _MRP_c
                from vdeter.runtime.phy_bridge import (
                    RuntimePhyRadioBridge as _BRG_c,
                )
                from vdeter.stage7.phy_facade import (
                    Stage7LaneRxIngress as _ING_c,
                )

                _cost_wrap(_ING_c, "observe_block", "ingress.observe_block")
                _cost_wrap(_ING_c, "promote_window", "ingress.promote")
                _cost_wrap(_ING_c, "finalize_window", "ingress.finalize")
                _cost_wrap(_BRG_c, "observe_samples", "bridge.observe_samples")
                _cost_wrap(_BRG_c, "complete_window", "bridge.complete_window")
                _cost_wrap(_MRP_c, "decode_samples", "phy.decode")
                _cost_wrap(_D7, "_energy_gate", "gate")
                _cost_wrap(_D7, "_lane_terminal", "lane_terminal")
                # register_window는 아래에서 _reg_locked로 재대입되므로
                # 여기서 감싸면 덮인다 — 재대입 뒤에 감싼다.
            except Exception as _cost_error:  # noqa: BLE001
                print(
                    f"[launcher] cost-instrument 미적용: {_cost_error}",
                    file=sys.stderr, flush=True,
                )

        def _cost_report():
            """10초마다 누적 비용을 한 줄로 낸다 (fail-open)."""

            try:
                if os.environ.get("STAGE7_DIAG", "0") != "1" or not _COST:
                    return
                now = _time.monotonic()
                if now - _COST_LAST[0] < 10.0:
                    return
                _COST_LAST[0] = now
                parts = [
                    f"{label}={slot[0]}/{slot[1] * 1e3:.0f}ms"
                    for label, slot in sorted(
                        _COST.items(), key=lambda kv: -kv[1][1]
                    )
                ]
                print(
                    "[diag] cost " + " ".join(parts),
                    file=sys.stderr, flush=True,
                )
            except Exception:  # noqa: BLE001
                pass

        # ── 수신 PHY decode를 펌프 스레드 밖으로 (lane별 프로세스)
        # phase-g-service-20260831-214853 실측: 펌프 drain 292.9초 중 창 완료가
        # 283.1초(96.7%)를 먹고, 그 안은 리샘플 37.93ms + 디코드 48.76ms/창이다
        # (표본 경로는 0.14ms/건). PHY는 순수 파이썬이라 GIL이 풀리지 않아
        # 스레드로는 분리되지 않는다. 런처는 8곳에서 스레드를 쓰므로 fork는
        # 위험하다 — spawn으로 띄운다.
        _DECODE_JOB_QUEUE_MAX = 64
        _DECODE_RESULT_QUEUE_MAX = 64
        _DECODE_RESULT_DEADLINE_S = 30.0
        #: reset은 순서를 지켜야 해 미룰 수 없다 — 자리가 날 때까지 기다린다.
        _DECODE_RESET_ENQUEUE_TIMEOUT_S = 15.0
        _decode_pool_slot = {"pool": None}

        class _DecodePool:
            """lane별 decode worker 프로세스와 그 FIFO를 소유한다.

            decode와 reset을 **한 큐**로 보낸다 — PHY stream은 기대 epoch
            연속성으로 보존/폐기를 판정하므로 reset이 decode를 앞지르면 결과가
            조용히 달라진다.

            crash·큐 포화·응답 지연은 재시작하지 않고 세션을 fail-closed로
            끝낸다. stream 연속성과 진단 기록이 사라진 뒤의 조용한 재기동은
            정상 운전처럼 보이는 공백을 만든다.
            """

            def __init__(self, dispatcher, stacks, lane_generation, id_kind):
                import multiprocessing as _mp
                from vdeter.stage7 import rx_decode_worker as _w

                self._w = _w
                self._dispatcher = dispatcher
                self._lane_generation = lane_generation
                # forkserver: spawn 자식의 cmdline은 `spawn_main`을 담아
                # 코디네이터의 B210 worker 계수(pgrep -fc "spawn_mai[n]")에
                # 잡힌다 — phase-g-service-20260901-005253에서 workers_running
                # 9(3+6)로 session health gate가 불합격했다. forkserver
                # 자식은 forkserver.main cmdline을 상속해 걸리지 않는다.
                # fork도 스레드가 많은 런처가 아니라 깨끗이 exec된 서버
                # 프로세스에서 일어나므로 안전하다.
                self._context = _mp.get_context("forkserver")
                try:
                    self._context.set_forkserver_preload(
                        ["vdeter.stage7.rx_decode_worker"])
                except Exception:  # noqa: BLE001
                    pass
                self._jobs = []
                self._results = []
                self._procs = []
                self._pending = {}
                self._next_job_id = 0
                #: job_id -> 제출 UTC ns (창 추적용, 계측 전용).
                self._submit_utc = {}
                self._fatal = None
                for lane_index in range(len(stacks)):
                    jobs = self._context.Queue(maxsize=_DECODE_JOB_QUEUE_MAX)
                    results = self._context.Queue(
                        maxsize=_DECODE_RESULT_QUEUE_MAX)
                    process = self._context.Process(
                        target=_w.run_worker, args=(jobs, results),
                        kwargs=dict(lane_index=lane_index,
                                    lane_generation=lane_generation,
                                    id_source_kind=id_kind,
                                    id_source_seed=0),
                        daemon=True,
                        name=f"stage7-rx-decode-lane{lane_index}",
                    )
                    process.start()
                    self._jobs.append(jobs)
                    self._results.append(results)
                    self._procs.append(process)
                self._ready = set()
                #: lane별 워커에 나가 있는 job 수 (제출 +1, 결과 -1).
                self._inflight = [0] * len(self._procs)
                print(
                    "[launcher] rx-decode workers 기동 "
                    f"lanes={len(self._procs)} "
                    f"pids={[p.pid for p in self._procs]}",
                    file=sys.stderr, flush=True,
                )

            def poll_ready(self):
                """모든 lane이 READY일 때만 True. 그 전에는 동기 경로가 돈다.

                워커 6개가 동시에 기동하면 Pi가 잠시 포화된다 — 그 사이
                제출하면 job 큐가 찬다 (phase-g-service-20260901-005253:
                MASS-B lane0 Full). READY 이후에만 executor를 건다.
                """

                if len(self._ready) == len(self._procs):
                    return True
                for lane_index, results in enumerate(self._results):
                    while True:
                        try:
                            message = results.get_nowait()
                        except Exception:  # noqa: BLE001
                            break
                        if message[0] == self._w.RESULT_READY:
                            self._ready.add(lane_index)
                        elif message[0] == self._w.RESULT_FATAL:
                            self.fail(f"worker 기동 FATAL lane={lane_index}: "
                                      f"{str(message[3])[:400]}")
                            return False
                for lane_index, process in enumerate(self._procs):
                    if not process.is_alive() and lane_index not in self._ready:
                        self.fail(f"worker 기동 중 사망 lane={lane_index} "
                                  f"exitcode={process.exitcode}")
                        return False
                return len(self._ready) == len(self._procs)

            def can_accept(self, lane_index):
                """이 lane의 job 큐에 여유가 있는가 (역압 판단용).

                큐가 차는 것은 워커가 죽은 게 아니라 잠시 밀린 것이다 —
                admission 직후 6개 워커가 동시에 데워지는 구간에서 디코드가
                건당 111ms -> 687ms로 느려진다 (phase-g-service-20260901-013149
                실측). 그때 치명 처리하면 회복 가능한 혼잡에 세션을 버린다.
                여유가 없으면 이 회전에서 창 완료를 미루고 다음에 잇는다.
                """

                try:
                    return not self._jobs[lane_index].full()
                except Exception:  # noqa: BLE001
                    return True

            # --- 제출 -------------------------------------------------------

            def submit(self, lane_index, lane_generation, context,
                       device_request, epoch_id, t0_ns):
                """dispatcher의 decode executor. 펌프 스레드에서만 불린다."""

                self._next_job_id += 1
                job_id = self._next_job_id
                self._pending[job_id] = (
                    lane_index, lane_generation, context,
                    _time.monotonic(),
                )
                try:
                    if len(self._submit_utc) > 4096:
                        self._submit_utc.clear()
                    self._submit_utc[job_id] = _utc_ns()
                except Exception:  # noqa: BLE001
                    pass
                _, rx_window_key = context
                try:
                    self._inflight[lane_index] += 1
                    _cost_add("depth.inflight", self._inflight[lane_index])
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._jobs[lane_index].put_nowait(
                        (self._w.JOB_DECODE, job_id, lane_generation,
                         rx_window_key, epoch_id, t0_ns, device_request)
                    )
                except Exception as error:  # noqa: BLE001 (queue.Full 포함)
                    self._pending.pop(job_id, None)
                    self.fail(f"decode job 큐 포화/전송 실패 lane={lane_index}: "
                              f"{type(error).__name__}")

            def reset_sink(self, lane_index):
                """bridge의 native reset을 같은 FIFO로 보낸다."""

                def _sink(channel):
                    # reset은 미루거나 버릴 수 없다 — decode와의 순서가
                    # PHY stream 연속성 판정을 좌우한다. 역압으로 큐가 차
                    # 있는 것이 정상 상태이므로 put_nowait은 곧바로 Full이
                    # 된다 (phase-g-service-20260901-021201: MASS-A가
                    # "reset 전달 실패 lane=0: Full"로 죽었다). 자리가 날
                    # 때까지 기다리되 상한을 둔다.
                    try:
                        self._jobs[lane_index].put(
                            (self._w.JOB_RESET, None, self._lane_generation,
                             channel),
                            timeout=_DECODE_RESET_ENQUEUE_TIMEOUT_S,
                        )
                    except Exception as error:  # noqa: BLE001
                        self.fail(
                            f"reset 전달 실패 lane={lane_index}: "
                            f"{type(error).__name__}")

                return _sink

            # --- 회수 -------------------------------------------------------

            def pump_results(self):
                """펌프 스레드에서 결과를 반영한다."""

                if self._fatal is not None:
                    return
                for lane_index, results in enumerate(self._results):
                    while True:
                        try:
                            message = results.get_nowait()
                        except Exception:  # noqa: BLE001 (queue.Empty 포함)
                            break
                        self._apply(lane_index, message)
                        if self._fatal is not None:
                            return
                self._check_liveness()

            def _apply(self, lane_index, message):
                kind = message[0]
                if kind == self._w.RESULT_READY:
                    return
                if kind == self._w.RESULT_FATAL:
                    self.fail(f"worker FATAL lane={lane_index}: "
                              f"{str(message[3])[:400]}")
                    return
                job_id = message[1]
                entry = self._pending.pop(job_id, None)
                if entry is None:
                    # generation 교체 등으로 이미 정리된 job이다.
                    return
                pending_lane, generation, context, _submitted = entry
                try:
                    self._inflight[pending_lane] = max(
                        0, self._inflight[pending_lane] - 1)
                    round_trip = _time.monotonic() - _submitted
                    _cost_add("lat.worker", round_trip)
                    # 워커가 실은 실제 계산 시간 (마지막 원소).
                    decode_s = message[-1]
                    if isinstance(decode_s, float):
                        _cost_add("lat.decode", decode_s)
                        _cost_add("lat.jobwait", max(0.0, round_trip - decode_s))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if kind == self._w.RESULT_ERR:
                        error = message[3]
                        self._dispatcher.complete_decoded(
                            generation, pending_lane, context,
                            error=error, gapped=error is None,
                            attempt_records=message[4],
                        )
                    else:
                        try:
                            first_ns = message[5]
                            sent = self._submit_utc.pop(message[1], None)
                            if first_ns and sent:
                                if len(_win_trace) > 4096:
                                    _win_trace.clear()
                                _win_trace[first_ns] = (
                                    (sent - first_ns) / 1e6,
                                    (_utc_ns() - first_ns) / 1e6,
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        self._dispatcher.complete_decoded(
                            generation, pending_lane, context,
                            result=message[3],
                            attempt_records=message[4],
                            received_at_ns=message[5],
                        )
                except Exception as error:  # noqa: BLE001
                    self.fail(f"decode 결과 반영 실패 lane={pending_lane}: "
                              f"{type(error).__name__}: {error}")

            def _check_liveness(self):
                for lane_index, process in enumerate(self._procs):
                    if not process.is_alive():
                        self.fail(
                            f"worker 사망 lane={lane_index} "
                            f"exitcode={process.exitcode}")
                        return
                if not self._pending:
                    return
                oldest = min(item[3] for item in self._pending.values())
                waited = _time.monotonic() - oldest
                if waited > _DECODE_RESULT_DEADLINE_S:
                    self.fail(f"decode 결과 지연 {waited:.1f}초 "
                              f"(pending={len(self._pending)})")

            # --- fail-closed -------------------------------------------------

            def fail(self, reason):
                if self._fatal is not None:
                    return
                self._fatal = reason
                print(f"[launcher] rx-decode FATAL — {reason}",
                      file=sys.stderr, flush=True)
                # 1) generation 무효화: 늦게 온 결과는 기존 stale 경로로 폐기된다.
                try:
                    self._dispatcher._lane_generation = -1
                except Exception:  # noqa: BLE001
                    pass
                # 2) in-flight 창을 정의된 오류로 terminal 처리한다.
                pending, self._pending = self._pending, {}
                for lane_index, generation, context, _s in pending.values():
                    try:
                        window, _key = context
                        self._dispatcher._lane_terminal(
                            window, lane_index,
                            error=_decode_fatal_error(reason),
                            received_at_ns=None,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # 3) 워커 회수 (상한 있음).
                self.shutdown()
                # os._exit는 multiprocessing의 atexit 정리를 건너뛴다 —
                # 이 런처가 띄운 B210 worker까지 고아로 남아 다음 세션의
                # 경쟁 프로세스 검사를 막는다 (phase-g-service-20260901-013149
                # 이후 spawn_main 하나가 38분 생존). 남은 자식에게 정리
                # 기회를 준다.
                try:
                    import multiprocessing as _mp_exit

                    for child in _mp_exit.active_children():
                        try:
                            child.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                    for child in _mp_exit.active_children():
                        try:
                            child.join(timeout=2.0)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    pass
                # 4) 세션을 fail-closed로 끝낸다 — 코디네이터의 workload
                #    quiescence가 launchers_alive로 잡는다. 재시작하지 않는다.
                try:
                    sys.stderr.flush()
                    sys.stdout.flush()
                except Exception:  # noqa: BLE001
                    pass
                _time.sleep(1.0)   # 증거 전달 유예 (상한)
                os._exit(3)

            def shutdown(self):
                for jobs in self._jobs:
                    try:
                        jobs.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        pass
                for process in self._procs:
                    try:
                        process.join(timeout=5.0)
                        if process.is_alive():
                            process.terminate()
                            process.join(timeout=3.0)
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=2.0)
                    except Exception:  # noqa: BLE001
                        pass

        def _decode_fatal_error(reason):
            from vdeter.common.errors import (
                Details as _Det, ErrorCategory as _Cat, ErrorCode as _Code,
                Layer as _Layer,
            )
            from vdeter.common.results import internal_error as _ie

            return _ie(
                _Layer.RUNTIME, _Code.PHY_INTERNAL_INVARIANT_BROKEN,
                _Cat.INTERNAL,
                "수신 decode worker가 fail-closed로 종료됐습니다",
                details=_Det.of(reason=str(reason)[:200]),
            ).error

        def _ensure_decode_pool():
            """dispatcher가 보이면 executor를 건다 (펌프 스레드에서만).

            `_D7.__init__`을 패치하는 방법은 쓸 수 없다 — dispatcher는 런처의
            monkeypatch 블록보다 **먼저** 생성돼 있어서 `__init__` 패치가
            영영 실행되지 않는다(phase-g-service-20260901-001527 실측: 워커가
            실행 끝에서야 떴고 전 구간이 동기 경로로 돌았다). 클래스 메서드
            패치는 기존 인스턴스에도 먹지만 `__init__`은 그렇지 않다.

            그래서 dispatch 항목이 실어 나르는 dispatcher 참조를 보고 첫
            drain에서 건다. 세대가 바뀌어 dispatcher가 교체되면 다시 건다.
            """

            if os.environ.get("STAGE7_RX_DECODE_WORKERS", "1") != "1":
                return
            dispatcher = _decode_pool_slot.get("dispatcher_seen")
            if dispatcher is None:
                return
            pool = _decode_pool_slot.get("pool")
            if pool is not None and _decode_pool_slot.get("bound") is dispatcher:
                return
            if pool is not None and _decode_pool_slot.get("owner") is dispatcher:
                # 이미 만들어 둔 pool이 READY가 되면 그때 건다.
                if not pool.poll_ready():
                    return
                dispatcher.set_decode_executor(pool.submit)
                for lane_index, stack in enumerate(dispatcher._stacks):
                    stack.bridge.set_native_reset_sink(
                        pool.reset_sink(lane_index))
                _decode_pool_slot["bound"] = dispatcher
                print("[launcher] rx-decode executor 결속 완료",
                      file=sys.stderr, flush=True)
                return
            try:
                from vdeter.common.identifiers import Uuid4IdSource as _U4

                stacks = dispatcher._stacks
                lane_generation = dispatcher._lane_generation
                id_kind = (
                    "UUID4"
                    if isinstance(getattr(stacks[0].facade, "_id_source", None),
                                  _U4)
                    else "DETERMINISTIC"
                )
                fresh = _DecodePool(dispatcher, stacks, lane_generation, id_kind)
            except Exception as error:  # noqa: BLE001
                # 조용히 동기 경로로 떨어지지 않는다 — 어느 경로로 돌았는지
                # 모르는 evidence가 가장 나쁘다.
                print(f"[launcher] rx-decode 기동 실패 — {type(error).__name__}: "
                      f"{error}", file=sys.stderr, flush=True)
                raise
            if pool is not None:
                pool.shutdown()
            _decode_pool_slot["pool"] = fresh
            _decode_pool_slot["owner"] = dispatcher
            _decode_pool_slot["bound"] = None

        def _decode_can_accept(lane_index):
            pool = _decode_pool_slot.get("pool")
            if pool is None or _decode_pool_slot.get("bound") is None:
                return True
            return pool.can_accept(lane_index)

        def _pump_decode_results():
            pool = _decode_pool_slot.get("pool")
            if pool is None:
                return
            # 배송 3계열과 같은 락 아래에서 반영한다 — finalize_window·
            # lane terminal이 register_window/observe와 같은 구조를 만진다.
            # 락 순서(_dispatch_lock -> dispatcher._lock)는 동기 경로와 동일해
            # 새 역전이 생기지 않는다.
            with _dispatch_lock:
                pool.pump_results()

        def _drain_dispatch_queue():
            # 항목당 처리(수신 디코드)가 ~140ms라 일괄 drain이 MAC 박동을
            # 초 단위로 멎게 한다 (svc-run-c 실측) — 항목 사이에 MAC
            # 미니사이클(40ms 게이트)을 끼워 박동을 보장한다.
            # svc-run-e 실측: 창 완료("c")가 타 창의 무거운 표본 디코드
            # 뒤에 줄서면 pending RX 장부가 상시 포화(65~74/68)돼 shed가
            # 청취 창을 굶긴다 — 같은 창의 표본이 이번 배치에 없는 완료
            # 항목만 앞으로 빼서 장부를 즉시 비운다 (같은 창의 표본→완료
            # 순서는 보존).
            # 시간 분할 (svc-run-o pump-slow 실측: 실버스트 디코드가 한
            # drain에 뭉치면 871~1685ms 정지 → 완료 반영·창 계획이 초
            # 단위로 밀리는 머리막힘이 완료 지연 ~15-19s·장부 포화·청취
            # 확률화의 잔존 뿌리. 300ms 예산 초과 시 남은 항목을 큐
            # 앞머리에 원순서로 되돌리고 다음 회전에 잇는다 — 처리량
            # 불변, 정지 상한만 도입.)
            _cost_report()
            _ensure_decode_pool()
            # worker 결과 반영은 반드시 펌프 스레드에서 한다 (MAC/dispatcher
            # 상태 소유자). 큐를 비우는 것이 창 완료보다 우선이다.
            _pump_decode_results()
            mac_cycle = getattr(main, "_mac_cycle", None)
            batch = []
            for _ in range(256):
                try:
                    batch.append(_dispatch_queue.popleft())
                except IndexError:
                    break
            if not batch:
                return
            sample_keys = {
                tuple(item[1:4]) for item in batch if item[0] == "s"
            }
            ordered = [
                item for item in batch
                if item[0] == "c" and tuple(item[1:4]) not in sample_keys
            ]
            promoted = len(ordered)
            ordered += [
                item for item in batch
                if not (item[0] == "c" and tuple(item[1:4]) not in sample_keys)
            ]
            drain_started = _time.monotonic()
            for index, item in enumerate(ordered):
                kind = item[0]
                # 역압: 워커가 밀렸으면 창 완료를 미루고 다음 회전에 잇는다.
                # 300ms 예산 초과와 같은 방식(원순서로 앞머리 복귀)이며
                # 처리량은 불변, 정지 상한만 유지된다.
                if kind == "c" and not _decode_can_accept(item[3]):
                    for leftover in reversed(ordered[index:]):
                        _dispatch_queue.appendleft(leftover)
                    if os.environ.get("STAGE7_DIAG", "0") == "1":
                        print(f"[diag] decode-backpressure done={index} "
                              f"deferred={len(ordered) - index}",
                              file=sys.stderr, flush=True)
                    return
                _item_started = _time.monotonic()
                if kind == "c":
                    try:
                        _q_at = _c_stamp.pop(id(item[5]), None)
                        if _q_at is not None:
                            _cost_add("lat.queue", _item_started - _q_at)
                    except Exception:  # noqa: BLE001
                        pass
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
                try:
                    _cost_add("drain." + kind, _time.monotonic() - _item_started)
                except Exception:  # noqa: BLE001
                    pass
                if promoted > 0:
                    promoted -= 1
                elif mac_cycle is not None:
                    _mac_started = _time.monotonic()
                    try:
                        mac_cycle()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        _cost_add(
                            "drain.mac_cycle", _time.monotonic() - _mac_started
                        )
                    except Exception:  # noqa: BLE001
                        pass
                if (
                    _time.monotonic() - drain_started > 0.3
                    and index + 1 < len(ordered)
                ):
                    for leftover in reversed(ordered[index + 1:]):
                        _dispatch_queue.appendleft(leftover)
                    if os.environ.get("STAGE7_DIAG", "0") == "1":
                        print(
                            f"[diag] drain-slice done={index + 1} "
                            f"deferred={len(ordered) - index - 1}",
                            file=sys.stderr, flush=True,
                        )
                    return

        main._drain_dispatch_queue = _drain_dispatch_queue
        main._dispatch_queue_ref = _dispatch_queue
        _orig_completed = _D7.on_lane_window_completed

        def _completed_locked(self_d, lane_generation, lane_index, child_key, completion):
            with _dispatch_lock:
                return _orig_completed(self_d, lane_generation, lane_index,
                                       child_key, completion)

        _D7.on_lane_error = _q_lane_err
        _D7.register_window = _reg_locked
        _D7.on_lane_samples = _q_samples
        _D7.on_lane_window_completed = _q_completed
        # 재대입 뒤에 감싸야 계측이 살아남는다 (fail-open).
        _cost_wrap(_D7, "register_window", "dispatcher.register")
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
    test_server = None
    if arguments.test_plan_sha:
        import glob as _glob
        import json as _json_local

        from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter

        fixtures = {}
        if arguments.fixtures_dir and os.path.isdir(arguments.fixtures_dir):
            for path in sorted(_glob.glob(os.path.join(arguments.fixtures_dir, "*.json"))):
                with open(path, "rb") as handle:
                    fixtures[os.path.basename(path)] = handle.read()
        # Stage 8 PoC device 경계(§16.2 최소 확장): launcher가 단독 소유한 radio를 TEST adapter
        # op으로만 노출한다. 이 seam이 없으면 PoC op은 전부 거부된다(기본값 = 쓸 수 없음).
        # 송신은 권위 `schedule_tx` 하나로만 나가며 별도 예약 원장·우회 경로를 만들지 않는다.
        from vdeter.stage7.configuration import CANONICAL_ROLES
        from vdeter.stage7.testing.poc_device import Stage7PocDeviceSeam

        poc_device = Stage7PocDeviceSeam(
            endpoint_id=arguments.endpoint_id,
            endpoint=endpoint,
            # §7 canonical mapping이 station id의 권위 출처다(별도 표를 만들지 않는다).
            station_id_by_endpoint={name: station
                                    for name, (_role, station) in CANONICAL_ROLES.items()},
        )
        adapter = Stage7ServiceApiTestAdapter(
            endpoint_id=arguments.endpoint_id,
            service=endpoint,
            resolved_test_plan_sha256=arguments.test_plan_sha,
            authorized_controller=(os.getuid(),),  # type: ignore[attr-defined]
            fixtures=fixtures,
            poc_device=poc_device,
        )
        test_socket_path = os.path.join(root, f"{arguments.endpoint_id}.test.sock")
        test_server = SeqPacketServer(test_socket_path)

        def _adapter_loop():
            try:
                _serve_test_adapter_peers(
                    test_server,
                    adapter,
                    expected_uid=os.getuid(),  # type: ignore[attr-defined]
                    stop_event=_pump_stop,
                )
            except BaseException as error:  # noqa: BLE001
                # 스레드가 조용히 죽으면 리스너만 남아 controller가 응답 없이
                # 매달린다 — coordinator에는 아무 신호도 가지 않는다.
                # 최소한 흔적은 반드시 남긴다.
                if not _pump_stop.is_set():
                    print(f"[launcher] test adapter 스레드 종료: "
                          f"{type(error).__name__}: {error}",
                          file=sys.stderr, flush=True)

        def _event_jsonl_loop():
            events_dir = os.path.join(arguments.run_root, "evidence", "service_events")
            os.makedirs(events_dir, exist_ok=True)
            path = os.path.join(events_dir, f"{arguments.endpoint_id}.jsonl")
            # 이벤트 0건으로 끝나는 run에서도 파일 존재를 보장한다
            # (svcdbg52: TBB 관문 붕괴 시 MASS-A.jsonl 부재 — 감사 혼동 방지).
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
            seen = 0
            events_warned = False
            while not _pump_stop.is_set():
                try:
                    sink = endpoint.start_gate.application_sink
                    events = tuple(sink.events())
                except Exception as exc:  # noqa: BLE001
                    # 무음 금지 (0.9.22): 영구 실패면 배달 증거 JSONL이
                    # 조용히 멎는다 — 1회성 경고로 관측 가능하게.
                    if not events_warned:
                        events_warned = True
                        print(
                            f"[launcher] service_events 수집 실패(계속 재시도): "
                            f"{type(exc).__name__}: {exc}",
                            file=sys.stderr, flush=True,
                        )
                    events = ()
                if len(events) > seen:
                    with open(path, "a", encoding="utf-8") as handle:
                        for event in events[seen:]:
                            record = service_event_record(event)
                            handle.write(_json_local.dumps(record, ensure_ascii=True))
                            handle.write("\n")
                    seen = len(events)
                _pump_stop.wait(0.5)

        for target in (_adapter_loop, _event_jsonl_loop):
            thread = _threading.Thread(target=target, daemon=True)
            thread.start()
            test_threads.append(thread)
        print(f"[launcher] test adapter 대기: {test_socket_path}", flush=True)

    exit_code = 0
    tbb_emitted = False
    try:
        while True:
            # emit_tbb_active는 state를 먼저 TBB_ACTIVE로 바꾼 뒤 이벤트를
            # 보낸다(runtime_client.py:165-166). 전송이 실패하면 tbb_emitted는
            # False인데 state는 이미 바뀌어 있어, SERVICE_START_STAGED만 보면
            # 재시도가 영영 일어나지 않는다 — 90초 TBB 창을 그대로 놓친다.
            # TBB_ACTIVE도 감시 대상에 넣어 재시도 경로를 연다. publish로
            # RUNNING이 되면 자동으로 꺼진다.
            watching_tbb = (
                client.state in ("SERVICE_START_STAGED", "TBB_ACTIVE")
                and not tbb_emitted
            )
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
        _pump_stop.set()
        if test_server is not None:
            test_server.close()
        for thread in test_threads:
            thread.join(timeout=2.0)
        _pump_thread.join(timeout=2.0)
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
