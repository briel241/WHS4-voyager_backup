"""Stage 7 수신 PHY decode worker (별도 프로세스).

**왜 프로세스인가.** phase-g-service-20260831-214853 실측에서 펌프 drain
292.9초 중 창 완료 하나(`drain.c`)가 283.1초(96.7%)를 먹었다. 그 안은
`to_native_decode_request`의 리샘플 37.93ms/창과 `decode_samples` 48.76ms/창
이고, 표본 경로는 0.14ms/건으로 무죄였다. 리샘플은 창당 2,048x97 = 198,656회
스칼라 복소 곱누산이며 PHY 패키지 전체가 순수 파이썬(numpy·C 확장 0개)이라
GIL이 풀리지 않는다 — 스레드로는 분리되지 않으므로 프로세스로 옮긴다.
Pi 5 실측: 4프로세스까지 3.99x 선형, 64KiB IPC 왕복 0.166ms(작업 87ms의 0.2%).

**이 모듈이 소유하는 것은 계산과 PHY stream 상태뿐이다.** RX ledger,
논리창, lane generation, MAC/dispatcher 상태는 전부 펌프가 소유하며 여기로
오지 않는다. 결과를 받은 펌프가 자기 스레드에서 반영한다.

**decode와 reset은 같은 FIFO로 들어온다.** `MultiRatePhy._transition_stream`이
기대 epoch 연속성으로 stream 보존/폐기를 판정하므로, 펌프측 reset
(`phy_bridge`의 `_reset_native`, `endpoint.reset_stream`)이 앞서 보낸 decode를
앞지르면 결과가 조용히 달라진다. 순서 보존은 이 큐 하나로 보장한다.

**fail-closed.** 워커는 예상 못 한 예외를 삼키지 않고 FATAL을 올린 뒤 죽는다.
재시작하지 않는다 — stream 연속성과 진단 기록이 함께 사라진 뒤의 조용한
재기동은 정상 운전처럼 보이는 공백을 만든다. 세션을 끝내는 판단은 펌프가 한다.
"""

from __future__ import annotations

import os
import traceback
from time import perf_counter as _perf

#: job 종류. (kind, job_id, lane_generation, payload...)
JOB_DECODE = "decode"
JOB_RESET = "reset"
JOB_STOP = "stop"

#: result 종류. (kind, job_id, lane_generation, payload...)
RESULT_OK = "ok"
RESULT_ERR = "err"
RESULT_FATAL = "fatal"
RESULT_READY = "ready"


class _GateHost:
    """`Stage7LaneRxDispatcher._energy_gate`가 요구하는 유일한 상태의 그릇.

    게이트는 `self._gate_state` 외의 self를 쓰지 않는다(support.py 실측).
    로직을 복제하면 펌프와 워커의 게이트 동작이 갈릴 수 있으므로 원본 함수를
    그대로 호출한다.
    """

    __slots__ = ("_gate_state",)

    def __init__(self) -> None:
        self._gate_state = {}


class LaneDecoder:
    """한 lane의 PHY 계산 일체. 프로세스 안에서만 산다."""

    __slots__ = ("lane_index", "lane_generation", "_facade", "_bridge",
                 "_gate_host", "_energy_gate")

    def __init__(self, *, lane_index: int, lane_generation: int,
                 id_source_kind: str, id_source_seed: int) -> None:
        from vdeter.common.identifiers import Uuid4IdSource
        from vdeter.common.time import SystemTimeSource
        from vdeter.profiles.link_phy import build_combined_phy_profile_registry
        from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge
        from vdeter.stage7.phy_facade import (
            Stage7DiagnosticPhyFacade,
            Stage7PhyDiagnosticSink,
        )
        from vdeter.stage7.support import Stage7LaneRxDispatcher

        if id_source_kind != "UUID4":
            # DETERMINISTIC은 단일 계열을 전제로 하므로 lane별 워커로 나누면
            # packet_id 계열이 갈린다 — 조용히 다른 값을 내지 않고 거부한다.
            raise ValueError(
                "decode worker는 UUID4 id source에서만 쓸 수 있습니다 "
                f"(현재: {id_source_kind})"
            )

        registry = build_combined_phy_profile_registry()
        registry = registry.value if hasattr(registry, "value") else registry
        freeze = getattr(registry, "freeze", None)
        if freeze is not None:
            freeze()

        self.lane_index = lane_index
        self.lane_generation = lane_generation
        sink = Stage7PhyDiagnosticSink(
            lane_index=lane_index, lane_generation=lane_generation
        )
        # route_sidecar는 TX(Packet->Burst 결속) 전용이라 RX 계산에 쓰이지
        # 않는다 — 워커는 RX만 수행하므로 두지 않는다.
        self._facade = Stage7DiagnosticPhyFacade(
            registry, SystemTimeSource(), Uuid4IdSource(),
            diagnostic_sink=sink, route_sidecar=None,
        )
        # bridge는 to_native_decode_request(리샘플)만 쓰기 위한 것이다.
        # 이 bridge의 ledger는 비어 있고 아무도 채우지 않는다 — 창 장부는
        # 펌프측 bridge가 소유한다.
        self._bridge = RuntimePhyRadioBridge(self._facade)
        self._gate_host = _GateHost()
        self._energy_gate = Stage7LaneRxDispatcher._energy_gate

    @property
    def diagnostic_sink(self):
        return self._facade.diagnostic_sink

    def reset(self, channel) -> None:
        """펌프측 reset을 PHY stream에 반영한다 (FIFO 순서대로)."""

        self._facade.reset_stream(channel)

    def decode(self, device_request, rx_window_key, epoch_id, t0_ns):
        """device-rate 요청 -> (결과 또는 오류, attempt 기록, 첫 표본 ns).

        펌프의 `on_lane_window_completed`가 하던 리샘플->게이트->디코드
        구간을 순서 그대로 수행한다.
        """

        prepared = self._bridge.to_native_decode_request(device_request)
        if prepared.is_err:
            return prepared, (), None
        native_request = prepared.value
        if native_request is None:
            # incomplete/gapped window: 펌프가 frozen 계약대로 처리한다.
            return None, (), None

        native_request = self._energy_gate(
            self._gate_host, self.lane_index, native_request
        )
        sink = self.diagnostic_sink
        sink.begin_window(rx_window_key, epoch_id, t0_ns)
        try:
            decoded = self._facade.decode_samples(native_request)
        finally:
            sink.end_window()

        records = tuple(_drain_sink_records(sink))
        first_sample_ns = (
            native_request.samples.first_sample_at.total_nanoseconds()
        )
        return decoded, records, first_sample_ns


def _drain_sink_records(sink):
    """워커 싱크에 쌓인 attempt 기록을 꺼내 비운다.

    진단 기록은 `decode_samples` 안에서 만들어지므로(phy_facade의
    `_build_recovered_set`) 여기서 회수해 펌프측 싱크로 되돌려야 evidence가
    보존된다. 회수에 실패해도 디코드 결과는 유효하므로 fail-open이다.
    """

    try:
        records = getattr(sink, "_records", None)
        if not records:
            return ()
        drained = tuple(records.values())
        records.clear()
        return drained
    except Exception:  # noqa: BLE001
        return ()


def _watch_parent(initial_ppid: int, interval: float = 1.0) -> None:
    """부모가 사라지면 스스로 죽는다.

    런처가 fail-closed로 `os._exit`할 때 multiprocessing의 atexit 정리가
    건너뛰어져 워커가 고아로 남는다 (phase-g-service-20260901-005253 이후
    PPID 1인 워커가 살아남아 다음 세션의 경쟁 프로세스 검사를 막았다).
    호스트측 검사에만 기대지 않고 워커가 직접 끊는다.
    """

    import time as _t

    while True:
        _t.sleep(interval)
        try:
            if os.getppid() != initial_ppid:
                os._exit(0)
        except Exception:  # noqa: BLE001
            os._exit(0)


def run_worker(job_queue, result_queue, *, lane_index: int,
               lane_generation: int, id_source_kind: str,
               id_source_seed: int) -> None:
    """워커 프로세스 진입점. FIFO를 순서대로 비운다."""

    try:
        import threading as _th

        _th.Thread(target=_watch_parent, args=(os.getppid(),),
                   daemon=True, name="rx-decode-parent-watch").start()
    except Exception:  # noqa: BLE001
        pass

    try:
        decoder = LaneDecoder(
            lane_index=lane_index, lane_generation=lane_generation,
            id_source_kind=id_source_kind, id_source_seed=id_source_seed,
        )
    except BaseException:  # noqa: BLE001
        _emit(result_queue,
              (RESULT_FATAL, None, lane_generation,
               "decode worker 초기화 실패: " + traceback.format_exc(limit=6)))
        return

    _emit(result_queue, (RESULT_READY, None, lane_generation, os.getpid()))

    while True:
        try:
            item = job_queue.get()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if item is None:
            return
        kind = item[0]
        if kind == JOB_STOP:
            return
        job_id = item[1]
        generation = item[2]
        try:
            if kind == JOB_RESET:
                decoder.reset(item[3])
                continue
            if kind != JOB_DECODE:
                _emit(result_queue,
                      (RESULT_FATAL, job_id, generation,
                       f"알 수 없는 job 종류: {kind!r}"))
                return
            _, _, _, rx_window_key, epoch_id, t0_ns, device_request = item
            _decode_started = _perf()
            decoded, records, first_sample_ns = decoder.decode(
                device_request, rx_window_key, epoch_id, t0_ns
            )
            decode_seconds = _perf() - _decode_started
        except BaseException:  # noqa: BLE001
            # 조용히 넘어가지 않는다 — 펌프가 세션을 fail-closed로 끝낸다.
            _emit(result_queue,
                  (RESULT_FATAL, job_id, generation,
                   "decode worker 예외: " + traceback.format_exc(limit=8)))
            return

        # decode_seconds를 실어 보낸다 — 펌프가 왕복에서 빼면 job 큐 대기가
        # 남는다. 워커 왕복 1,449ms(MASS-A 실측)가 계산인지 대기인지 가른다.
        if decoded is None:
            _emit(result_queue,
                  (RESULT_ERR, job_id, generation, None, records,
                   decode_seconds))
        elif decoded.is_err:
            _emit(result_queue,
                  (RESULT_ERR, job_id, generation, decoded.error, records,
                   decode_seconds))
        else:
            _emit(result_queue,
                  (RESULT_OK, job_id, generation, decoded.value, records,
                   first_sample_ns, decode_seconds))


def _emit(result_queue, message) -> None:
    try:
        result_queue.put(message)
    except Exception:  # noqa: BLE001
        # 부모가 이미 사라진 경우다. 여기서 할 수 있는 일이 없다.
        pass
