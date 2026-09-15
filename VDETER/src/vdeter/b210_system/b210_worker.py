"""Stage 7 dual-lane B210 worker backend (`stage7` §6, §10.1, §15).

frozen `radio/b210/` channel-0 backend는 수정하지 않는다. 이 module이 device
channel 0/1 dual-lane capability를 가산한다. worker 하나가 한 B210의 두 lane과
shared TX/RX LO를 단독 소유하며 기존 backend class를 monkey patch하거나 동시에
같은 serial에 열지 않는다.

process 격리는 frozen `ProcessIsolatedGrUhdBackend`의 공개 `_backend_type` 주입
인자로 재사용한다 (frozen 코드 수정 0).

B210은 두 TX channel이 한 TX LO를, 두 RX channel이 한 RX LO를 공유한다 (§10.1).
따라서 tune은 lane별이 아니라 LO별이며, lane별로 서로 다른 주파수를 동시에
송신하도록 계획하지 않는다.
"""

from __future__ import annotations

import importlib
import gc
import time
from collections import deque
from threading import Event, Lock, Thread

from ..radio.b210.backend import (
    _RxWindowAssembler,
    _find_hardware_time_ns,
    _find_named_value,
    _flatten_text,
    _range_contains,
    _sample_offset_ns,
)
from ..radio.b210.isolated_backend import ProcessIsolatedGrUhdBackend


class _ServiceRxWindowAssembler(_RxWindowAssembler):
    """frozen assembler의 stray ``rx_time`` 끈적(sticky) 오류를 강등한 stage7 변형.

    frozen ``_begin_timestamp``는 등록 창에 정확히 하나로 상관되지 않는
    timestamp가 단 한 번만 들어와도 ``_stream_error``를 영구 세팅해 이후
    모든 read를 raise시킨다 → adapter가 장치 손실로 오판, endpoint 수신
    전면 정지(svcdbg34 실측: MASS-A DEVICE_ERROR 102·요청 폭주 스핀).
    교정(P1)의 희소 창에선 안전장치였지만 서비스 밀집 스케줄에선 overflow
    재시작 tag 등 상관 불가 tag가 실재하므로, 여기서는 해당 조각만 폐기
    하고 스트림은 계속한다. 폐기된 샘플은 어차피 어떤 창에도 귀속 불가
    하므로 판정 무결성에 영향 없다 — 창이 못 차면 기존 INCOMPLETE/TIMEOUT
    경로로 창 단위 실패가 그대로 드러난다. 동결 파일은 불변(서브클래스
    교체만; 사용자 위임 2026-08-28)."""

    def _begin_timestamp(self, actual_hardware_time_ns):
        candidates = []
        for window in self._windows:
            start = window["expected_hardware_time_ns"]
            end = start + _sample_offset_ns(
                window["expected_sample_count"], self._sample_rate_hz
            )
            if not window["complete"] and start <= actual_hardware_time_ns < end:
                candidates.append(window)
            elif not window["started"] and end <= actual_hardware_time_ns:
                window["complete"] = True
        if len(candidates) != 1:
            # 연속 RX에선 창 밖 stray rx_time이 실재한다 — 고아 tag는
            # 조용히 버리고 창 상태만 유지한다.
            self._orphan_tag_count = getattr(self, "_orphan_tag_count", 0) + 1
            self._current = None
            return
        # (아래는 frozen _begin_timestamp와 동일)
        window = candidates[0]
        if self._current is not None and self._current is not window:
            self._current["complete"] = True
        if window["started"]:
            window["gap_pending"] = window["observed_sample_count"] > 0
        else:
            window["started"] = True
            window["actual_hardware_time_ns"] = actual_hardware_time_ns
        window["next_fragment_hardware_time_ns"] = actual_hardware_time_ns
        self._current = window

    def window_starts(self):
        """연속 RX 모드의 합성 태그 주입용: 미시작·미완료 창 (start, count) 목록."""

        with self._lock:
            return tuple(
                (w["expected_hardware_time_ns"], w["expected_sample_count"])
                for w in self._windows
                if not w["complete"] and not w["started"]
            )

LANE_COUNT = 2


class Stage7DualLaneFlowgraphBackend:
    """한 B210의 device channel 0/1을 함께 소유하는 programmatic gr-uhd flowgraph.

    구조는 frozen `GrUhdFlowgraphBackend`와 같은 one-top-block 패턴이되
    dual-channel `usrp_source`/`usrp_sink`, lane별 timed burst source와 lane별
    bounded RX collector를 가진다.
    """

    __slots__ = (
        "_gr", "_uhd", "_pmt", "_numpy", "_top", "_source", "_sink",
        "_tx_sources", "_rx_collectors", "_async_collector", "_async_messages",
        "_async_lock", "_control_lock", "_started", "_closed",
        "_tx_lo_frequency_hz", "_rx_lo_frequency_hz",
        "_cleanup_event", "_cleanup_failure", "_cleanup_thread", "_cleanup_lock",
        "_connected", "_activity_event", "_tx_muted", "_companion_windows",
        "_rx_plan_max_bytes", "_rx_plan_sample_rate_hz", "_rx_assembler_resets",
        "_tx_probe_count", "_manual_rx_tuning", "_rx_continuous_since_ns",
    )

    def __init__(self, device_selector: str, cpu_format: str, otw_format: str) -> None:
        gr = importlib.import_module("gnuradio.gr")
        uhd = importlib.import_module("gnuradio.uhd")
        pmt = importlib.import_module("pmt")
        numpy = importlib.import_module("numpy")
        self._gr, self._uhd, self._pmt, self._numpy = gr, uhd, pmt, numpy
        self._top = gr.top_block("vdeter-stage7-b210-dual")
        self._activity_event = Event()
        activity_event = self._activity_event
        stream_args = uhd.stream_args(
            cpu_format=cpu_format, otw_format=otw_format, channels=[0, 1]
        )
        # §9.3의 1초 finite 캡처(307,200 sample)는 기본 USB 전송 버퍼
        # (num_recv_frames 기본값 ≈0.14s 분량)로는 host 스케줄링 지연 한 번에
        # overflow가 나고, UHD는 finite 수신을 overrun에서 중단한다. 프레임 수를
        # 256으로 확대해 수백 ms의 지연을 흡수한다. 1024는 USB2 포트(MASS-B)에서
        # 전송 메모리 할당 실패(Boost assertion)를 일으켜 사용하지 않는다. 장치
        # identity/설정 readback에는 영향이 없다.
        def _with_args(selector: str, extra: str) -> str:
            return f"{selector},{extra}" if selector else extra

        self._source = uhd.usrp_source(
            _with_args(device_selector, "num_recv_frames=256"), stream_args, False
        )
        self._sink = uhd.usrp_sink(
            _with_args(device_selector, "num_send_frames=256"), stream_args, ""
        )

        class _TimedBurstSource(gr.sync_block):
            """frozen backend와 같은 timed tx_sob/tx_eob tagging (lane별 인스턴스)."""

            def __init__(source_self, lane_index: int):
                super().__init__(
                    name=f"vdeter-stage7-tx-lane{lane_index}",
                    in_sig=None,
                    out_sig=[numpy.complex64],
                )
                source_self._queue = deque()
                source_self._current = None
                source_self._index = 0
                source_self._lock = Lock()
                source_self._lane = lane_index
                # 유휴 source(0 반환)는 GR 스케줄러가 ~100-250ms 간격으로만
                # 재호출한다(svcdbg49/51 실측: 첫 burst 픽업 185-246ms 지각
                # → TIME_ERROR). 메시지 포트 수신은 블록 스레드를 즉시
                # 깨우므로, enqueue 직후 wake 메시지로 픽업을 ms 수준으로
                # 당긴다.
                source_self.message_port_register_in(pmt.intern("wake"))
                source_self.set_msg_handler(pmt.intern("wake"), source_self._on_wake)

            def _on_wake(source_self, _msg):
                # 기상 전용 — 처리 없음 (GR gateway는 이름 결속이라 실명 메서드 필요).
                return None

            def enqueue(source_self, samples, hardware_time_ns):
                with source_self._lock:
                    source_self._queue.append(
                        (samples, hardware_time_ns, time.monotonic())
                    )

            def work(source_self, input_items, output_items):
                # frozen 단일-lane source와 같은 timed burst 의미. dual-channel
                # 정렬은 `submit_tx_group`의 zero companion burst가 보장한다.
                output = output_items[0]
                with source_self._lock:
                    if source_self._current is None:
                        if not source_self._queue:
                            return 0
                        source_self._current = source_self._queue.popleft()
                        source_self._index = 0
                        import os as _os
                        if _os.environ.get("STAGE7_DIAG", "0") == "1":
                            try:
                                import sys as _sys
                                delay_ms = (
                                    time.monotonic() - source_self._current[2]
                                ) * 1e3
                                print(
                                    f"[diag-worker] tx-pickup lane"
                                    f"{source_self._lane} "
                                    f"t_ns={source_self._current[1]} "
                                    f"delay_ms={delay_ms:.1f} "
                                    f"qlen={len(source_self._queue)}",
                                    file=_sys.stderr, flush=True,
                                )
                            except Exception:  # noqa: BLE001
                                pass
                    samples, hardware_time_ns = source_self._current[:2]
                    remaining = len(samples) - source_self._index
                    produced = min(len(output), remaining)
                    absolute = source_self.nitems_written(0)
                    if source_self._index == 0:
                        seconds, nanoseconds = divmod(hardware_time_ns, 1_000_000_000)
                        tx_time = pmt.make_tuple(
                            pmt.from_uint64(seconds),
                            pmt.from_double(nanoseconds / 1_000_000_000.0),
                        )
                        source_self.add_item_tag(0, absolute, pmt.intern("tx_time"), tx_time)
                        source_self.add_item_tag(0, absolute, pmt.intern("tx_sob"), pmt.PMT_T)
                    output[:produced] = samples[source_self._index : source_self._index + produced]
                    source_self._index += produced
                    if source_self._index == len(samples):
                        source_self.add_item_tag(
                            0, absolute + produced - 1, pmt.intern("tx_eob"), pmt.PMT_T
                        )
                        source_self._current = None
                        source_self._index = 0
                    return produced

        class _BoundedRxCollector(gr.sync_block):
            def __init__(collector_self, lane_index: int):
                super().__init__(
                    name=f"vdeter-stage7-rx-lane{lane_index}",
                    in_sig=[numpy.complex64],
                    out_sig=None,
                )
                collector_self._assembler = _ServiceRxWindowAssembler()

            def set_max_bytes(collector_self, maximum):
                collector_self._assembler.set_max_bytes(maximum)

            def set_sample_rate(collector_self, sample_rate_hz):
                collector_self._assembler.set_sample_rate(sample_rate_hz)
                collector_self._svc_rate = int(sample_rate_hz)

            def register_window(collector_self, hardware_time_ns, sample_count):
                collector_self._assembler.register_window(hardware_time_ns, sample_count)

            def unregister_window(collector_self, hardware_time_ns):
                collector_self._assembler.unregister_window(hardware_time_ns)

            def work(collector_self, input_items, output_items):
                values = input_items[0]
                count = len(values)
                if not count:
                    return 0
                start = collector_self.nitems_read(0)
                tags = collector_self.get_tags_in_window(0, 0, count)
                encoded = values.astype(numpy.dtype("<c8"), copy=False).tobytes()
                rx_times = []
                for tag in tags:
                    if pmt.symbol_to_string(tag.key) != "rx_time":
                        continue
                    decoded = pmt.to_python(tag.value)
                    if isinstance(decoded, (tuple, list)) and len(decoded) == 2:
                        hardware_time_ns = (
                            int(decoded[0]) * 1_000_000_000
                            + int(round(float(decoded[1]) * 1_000_000_000))
                        )
                        rx_times.append((int(tag.offset - start), hardware_time_ns))
                rx_times.sort(key=lambda value: value[0])
                # 연속 RX 모드: 실 rx_time 태그는 스트림 시작·overflow 재시작
                # 때만 온다. 등록 창 시작점마다 태그를 합성해 frozen assembler의
                # 태그-상관 계약에 맞춘다 (svcdbg41 실측 근거: 인접 창의
                # 창별 유한 스트림 재시작이 창 2/3를 빈 창으로 만들었다).
                try:
                    if getattr(collector_self, "_svc_continuous", False):
                        anchor = getattr(collector_self, "_svc_anchor", None)
                        if rx_times:
                            last_offset, last_hw = rx_times[-1]
                            anchor = (start + last_offset, last_hw)
                            collector_self._svc_anchor = anchor
                        if anchor is not None:
                            anchor_abs, anchor_hw = anchor
                            rate = getattr(collector_self, "_svc_rate", 307_200)
                            # 핫패스 경감: 창 목록 스캔을 5회당 1회 캐시 갱신
                            # (등록→시작 최소 lead 250ms ≫ 캐시 신선도 ~66ms).
                            n_call = getattr(collector_self, "_svc_ncall", 0) + 1
                            collector_self._svc_ncall = n_call
                            cached = getattr(collector_self, "_svc_wcache", None)
                            if cached is None or n_call % 5 == 0:
                                cached = collector_self._assembler.window_starts()
                                collector_self._svc_wcache = cached
                            synthetic = []
                            for w_start, _w_count in cached:
                                relative_ns = w_start - anchor_hw
                                sample_delta = (
                                    relative_ns * rate + 500_000_000
                                ) // 1_000_000_000
                                offset_in_chunk = anchor_abs + sample_delta - start
                                if 0 <= offset_in_chunk < count:
                                    synthetic.append(
                                        (int(offset_in_chunk), int(w_start))
                                    )
                            if synthetic:
                                rx_times = sorted(rx_times + synthetic)
                except Exception:  # noqa: BLE001 - 합성 실패는 기존 경로로 진행
                    pass
                collector_self._assembler.feed(encoded, tuple(rx_times))
                activity_event.set()
                return count

            def read(collector_self, sample_count, allow_partial, expected_hardware_time_ns=None):
                return collector_self._assembler.read(
                    sample_count, allow_partial, expected_hardware_time_ns
                )

        self._tx_sources = tuple(_TimedBurstSource(lane) for lane in range(LANE_COUNT))
        self._rx_collectors = tuple(_BoundedRxCollector(lane) for lane in range(LANE_COUNT))
        # dual-channel stream command가 양 lane에 만드는 고아 rx_time 태그를
        # 막기 위한 companion 창 등록 이력 (lane별, submit_rx_lane 참조).
        self._companion_windows = {0: [], 1: []}
        self._async_messages = []
        self._async_lock = Lock()
        messages = self._async_messages
        messages_lock = self._async_lock

        class _AsyncCollector(gr.basic_block):
            def __init__(collector_self):
                super().__init__(name="vdeter-stage7-b210-async", in_sig=None, out_sig=None)
                port = pmt.intern("in")
                collector_self.message_port_register_in(port)
                collector_self.set_msg_handler(port, collector_self.receive)

            def receive(collector_self, message):
                with messages_lock:
                    messages.append(message)
                activity_event.set()

        self._async_collector = _AsyncCollector()
        self._control_lock = Lock()
        self._started = False
        self._closed = False
        self._tx_lo_frequency_hz = None
        self._rx_lo_frequency_hz = None
        self._cleanup_event = Event()
        self._cleanup_failure = []
        self._cleanup_thread = None
        self._cleanup_lock = Lock()
        self._connected = False
        self._tx_muted = [False, False]
        #: 마지막으로 적용한 manual RX tuning(WSM-G1 공통 LO + lane DSP + BW). None이면 미적용.
        self._manual_rx_tuning = None
        #: 연속 RX가 유효해지는 hardware ns(서비스 모드). 연속 RX가 아니면 None.
        self._rx_continuous_since_ns = None

    @staticmethod
    def discover(device_selector: str) -> tuple[str, ...]:
        from ..radio.b210.backend import GrUhdFlowgraphBackend

        return GrUhdFlowgraphBackend.discover(device_selector)

    # --- configure -----------------------------------------------------------

    def configure_dual(self, plan: dict) -> dict:
        """비활성 graph에 dual-lane 설정을 적용하고 실제 readback을 반환한다.

        `plan`은 picklable dict다 (process 격리 RPC로 전달).

        ```
        {"master_clock_rate_hz": int, "cpu_format": str, "otw_format": str,
         "sample_rate_hz": int, "analog_bandwidth_hz": int,
         "tx_lo_hz": int, "rx_lo_hz": int,
         "tx_gain_db": [g0, g1], "rx_gain_db": [g0, g1],
         "rx_queue_max_bytes": int, "device_sample_rate_hz": int}
        ```
        """

        mcr = plan["master_clock_rate_hz"]
        self._source.set_clock_rate(mcr, 0)
        self._sink.set_clock_rate(mcr, 0)
        self._source.set_clock_source("internal", 0)
        self._sink.set_clock_source("internal", 0)
        self._source.set_time_source("internal", 0)
        self._sink.set_time_source("internal", 0)

        source_info = self._source.get_usrp_info(0)
        sink_info = self._sink.get_usrp_info(0)
        source_serial = str(source_info.get("mboard_serial") or source_info.get("serial"))
        sink_serial = str(sink_info.get("mboard_serial") or sink_info.get("serial"))

        self._sink.set_samp_rate(plan["sample_rate_hz"])
        self._source.set_samp_rate(plan["sample_rate_hz"])
        lanes = {}
        for lane in range(LANE_COUNT):
            if not _range_contains(self._sink.get_gain_range(lane), plan["tx_gain_db"][lane]):
                raise ValueError("requested TX gain is outside the B210 range")
            if not _range_contains(self._source.get_gain_range(lane), plan["rx_gain_db"][lane]):
                raise ValueError("requested RX gain is outside the B210 range")
            self._sink.set_gain(plan["tx_gain_db"][lane], lane)
            self._source.set_gain(plan["rx_gain_db"][lane], lane)
            self._sink.set_bandwidth(plan["analog_bandwidth_hz"], lane)
            self._source.set_bandwidth(plan["analog_bandwidth_hz"], lane)
            self._sink.set_antenna("TX/RX", lane)
            self._source.set_antenna("RX2", lane)
        # shared LO: 두 lane에 같은 tune을 적용한다 (§10.1).
        for lane in range(LANE_COUNT):
            self._sink.set_center_freq(plan["tx_lo_hz"], lane)
            self._source.set_center_freq(plan["rx_lo_hz"], lane)
        self._tx_lo_frequency_hz = int(round(self._sink.get_center_freq(0)))
        self._rx_lo_frequency_hz = int(round(self._source.get_center_freq(0)))

        for lane in range(LANE_COUNT):
            collector = self._rx_collectors[lane]
            collector.set_max_bytes(plan["rx_queue_max_bytes"])
            collector.set_sample_rate(plan["device_sample_rate_hz"])
            self._rx_plan_max_bytes = plan["rx_queue_max_bytes"]
            self._rx_plan_sample_rate_hz = plan["device_sample_rate_hz"]
            lanes[str(lane)] = {
                "tx_center_frequency_hz": int(round(self._sink.get_center_freq(lane))),
                "rx_center_frequency_hz": int(round(self._source.get_center_freq(lane))),
                "tx_sample_rate_hz": int(round(self._sink.get_samp_rate())),
                "rx_sample_rate_hz": int(round(self._source.get_samp_rate())),
                "tx_gain_db": int(round(self._sink.get_gain(lane))),
                "rx_gain_db": int(round(self._source.get_gain(lane))),
                "tx_bandwidth_hz": int(round(self._sink.get_bandwidth(lane))),
                "rx_bandwidth_hz": int(round(self._source.get_bandwidth(lane))),
                "tx_antenna": str(self._sink.get_antenna(lane)),
                "rx_antenna": str(self._source.get_antenna(lane)),
            }

        self._source.set_time_now(self._time_spec(0), 0)
        source_hardware_time_ns = self.hardware_time_ns()
        sink_time = self._sink.get_time_now(0)
        sink_hardware_time_ns = (
            int(sink_time.get_full_secs()) * 1_000_000_000
            + int(round(sink_time.get_frac_secs() * 1_000_000_000))
        )
        return {
            "serial": source_serial,
            "sink_serial": sink_serial,
            "same_timebase": (
                source_serial == sink_serial
                and abs(source_hardware_time_ns - sink_hardware_time_ns) <= 10_000_000
            ),
            "master_clock_rate_hz": int(round(self._source.get_clock_rate(0))),
            "sink_master_clock_rate_hz": int(round(self._sink.get_clock_rate(0))),
            "clock_source": str(self._source.get_clock_source(0)).lower(),
            "time_source": str(self._source.get_time_source(0)).lower(),
            "sink_clock_source": str(self._sink.get_clock_source(0)).lower(),
            "sink_time_source": str(self._sink.get_time_source(0)).lower(),
            "hardware_time_reset": (
                abs(source_hardware_time_ns) <= 10_000_000
                and abs(sink_hardware_time_ns) <= 10_000_000
            ),
            "cpu_format": plan["cpu_format"],
            "otw_format": plan["otw_format"],
            "tx_lo_hz": self._tx_lo_frequency_hz,
            "rx_lo_hz": self._rx_lo_frequency_hz,
            "lanes": lanes,
        }

    def connect(self) -> None:
        if self._connected:
            return
        for lane in range(LANE_COUNT):
            self._top.connect((self._tx_sources[lane], 0), (self._sink, lane))
            self._top.connect((self._source, lane), (self._rx_collectors[lane], 0))
        self._top.msg_connect((self._sink, "async_msgs"), (self._async_collector, "in"))
        self._connected = True

    def start(self) -> None:
        if not self._connected:
            raise RuntimeError("stage7 dual-lane flowgraph is not connected")
        self._top.start()
        self._started = True

    def stop(self, timeout_ns: int) -> bool:
        if self._closed:
            return True
        with self._cleanup_lock:
            if self._cleanup_thread is None:
                self._cleanup_event.clear()
                self._cleanup_failure.clear()

                def cleanup_graph():
                    top = self._top
                    try:
                        top.stop()
                        top.wait()
                    except Exception as error:  # noqa: BLE001 - structured 반환
                        self._cleanup_failure.append(error)
                    finally:
                        self._connected = False
                        self._top = None
                        self._source = None
                        self._sink = None
                        self._tx_sources = None
                        self._rx_collectors = None
                        self._async_collector = None
                        del top
                        gc.collect()
                        self._cleanup_event.set()

                self._cleanup_thread = Thread(
                    target=cleanup_graph, name="vdeter-stage7-b210-stop", daemon=True
                )
                self._cleanup_thread.start()
            finished = self._cleanup_event
        if not finished.wait(timeout_ns / 1_000_000_000):
            return False
        if self._cleanup_failure:
            raise self._cleanup_failure[0]
        self._started = False
        self._closed = True
        return True

    def close(self, timeout_ns: int) -> bool:
        return self.stop(timeout_ns)

    # --- time·tune·상태 -------------------------------------------------------

    def hardware_time_ns(self) -> int:
        value = self._source.get_time_now(0)
        return int(value.get_full_secs()) * 1_000_000_000 + int(
            round(value.get_frac_secs() * 1_000_000_000)
        )

    def lo_readback(self) -> dict:
        return {
            "tx_lo_hz": int(round(self._sink.get_center_freq(0))),
            "rx_lo_hz": int(round(self._source.get_center_freq(0))),
            "tx_lo_lane1_hz": int(round(self._sink.get_center_freq(1))),
            "rx_lo_lane1_hz": int(round(self._source.get_center_freq(1))),
        }

    def poc_runtime_state(self) -> dict:
        """Owner-process state in one RPC; no repeated tuning/sensor USB reads.

        RX center is the verified configure/restore state (manual scope supplies
        its own verified per-lane readback). TX LO is the last accepted command,
        not proof that a future timed retune has already occurred.
        """
        with self._control_lock:
            return {
                "hardware_time_ns": self.hardware_time_ns(),
                "tx_lo_hz": self._tx_lo_frequency_hz,
                "rx_lo_hz": self._rx_lo_frequency_hz,
                "tx_muted": list(self._tx_muted),
                "rx_continuous": self._rx_continuous_since_ns is not None,
                "rx_continuous_since_ns": self._rx_continuous_since_ns,
                "state_basis": "DEVICE_OWNER_CONTROL_STATE",
                "tx_lo_basis": "LAST_ACCEPTED_COMMAND_NOT_TIMED_COMPLETION",
            }

    # --- manual RX tuning (WSM-G1 공통 LO + lane DSP + analog BW) ---------------

    def _tune_policy(self, name: str):
        """`uhd.tune_request`의 MANUAL policy 상수. 없으면 미지원으로 거부한다(추정 금지)."""

        request_type = getattr(self._uhd, "tune_request", None)
        if request_type is None:
            raise RuntimeError("uhd.tune_request가 없습니다 — manual tuning 미지원 설치")
        policy = getattr(request_type, name, None)
        if policy is None:
            raise RuntimeError(f"uhd.tune_request.{name}가 없습니다 — manual tuning 미지원 설치")
        return policy

    def _rx_tuning_snapshot(self) -> dict:
        """현재 RX tuning을 **장치에서 읽어** 돌려준다(계획값 되풀이 아님).

        `lane_center_hz`는 장치가 보고하는 lane별 실제 중심 주파수(= RF LO와 DSP의 합성 결과)다.
        manual tuning을 적용한 적이 있으면 그때 장치가 보고한 RF/DSP 분해값도 함께 보존한다.
        """

        snapshot = {
            "lane_center_hz": {
                lane: int(round(self._source.get_center_freq(lane)))
                for lane in range(LANE_COUNT)
            },
            "analog_bandwidth_hz": int(round(self._source.get_bandwidth(0))),
        }
        applied = self._manual_rx_tuning
        if applied is not None:
            snapshot["rx_lo_hz"] = int(applied["rx_lo_hz"])
            snapshot["lane_dsp_hz"] = dict(applied["lane_dsp_hz"])
        return snapshot

    def read_tuning(self) -> dict:
        """RX tuning 실측 snapshot(복구용). 장치를 변경하지 않는다."""

        with self._control_lock:
            return self._rx_tuning_snapshot()

    def apply_manual_tuning(self, rx_lo_hz: int, lane_dsp_hz, analog_bandwidth_hz: int) -> dict:
        """두 lane에 **공통 RF LO + lane별 DSP offset + analog RX bandwidth**를 적용한다.

        WSM-G1 공통 LO 계획(lower/upper 동시 수신)을 장치에 거는 유일한 경로다. UHD `tune_request`의
        RF/DSP policy를 **둘 다 MANUAL**로 두어 공통 RF LO를 고정하고 lane별 CORDIC(DSP) offset만
        다르게 준다 — 그렇게 하지 않으면 UHD가 lane마다 RF LO를 임의로 다시 잡아 공통 LO가 깨진다.

        반환은 **장치가 보고한 값**이다(요청값 반복이 아니다): `tune_result`의 실제 RF/DSP와
        `get_center_freq`/`get_bandwidth` readback. 상위(`CommonLoTuningApplier`)가 이 값으로 계획
        일치를 판정한다. 여기서는 판정하지 않고 실측만 돌려준다.
        """

        uhd = self._uhd
        offsets = {}
        for lane in range(LANE_COUNT):
            value = (lane_dsp_hz.get(lane, lane_dsp_hz.get(str(lane)))
                     if hasattr(lane_dsp_hz, "get") else lane_dsp_hz[lane])
            if value is None:
                raise ValueError(f"lane {lane}의 DSP offset이 없습니다")
            offsets[lane] = int(value)
        manual_rf = self._tune_policy("POLICY_MANUAL")
        with self._control_lock:
            previous = self._rx_tuning_snapshot()
            lanes = {}
            for lane in range(LANE_COUNT):
                # analog filter를 먼저 넓힌다 — 좁은 상태로 DSP offset을 걸면 그 순간 lane 하나가
                # 통과대역 밖에 놓인다.
                self._source.set_bandwidth(int(analog_bandwidth_hz), lane)
                request = uhd.tune_request(float(int(rx_lo_hz) - offsets[lane]))
                request.rf_freq_policy = manual_rf
                request.rf_freq = float(int(rx_lo_hz))
                request.dsp_freq_policy = manual_rf
                request.dsp_freq = float(offsets[lane])
                result = self._source.set_center_freq(request, lane)
                lanes[lane] = {
                    "actual_rf_freq_hz": int(round(float(result.actual_rf_freq))),
                    "actual_dsp_freq_hz": int(round(float(result.actual_dsp_freq))),
                    "center_freq_hz": int(round(self._source.get_center_freq(lane))),
                    "analog_bandwidth_hz": int(round(self._source.get_bandwidth(lane))),
                }
            # 장치가 보고한 lane별 RF LO가 서로 다르면 공통 LO가 아니다 — 상위가 거부할 수 있게
            # 사실대로 싣는다(여기서 평균·대표값으로 뭉개지 않는다).
            readback = {
                "rx_lo_hz": lanes[0]["actual_rf_freq_hz"],
                "lane_dsp_hz": {lane: lanes[lane]["actual_dsp_freq_hz"]
                                for lane in range(LANE_COUNT)},
                "analog_bandwidth_hz": min(lanes[lane]["analog_bandwidth_hz"]
                                           for lane in range(LANE_COUNT)),
                "lane_rf_lo_hz": {lane: lanes[lane]["actual_rf_freq_hz"]
                                  for lane in range(LANE_COUNT)},
                "lane_center_hz": {lane: lanes[lane]["center_freq_hz"]
                                   for lane in range(LANE_COUNT)},
                "previous": previous,
            }
            self._manual_rx_tuning = {
                "rx_lo_hz": readback["rx_lo_hz"],
                "lane_dsp_hz": dict(readback["lane_dsp_hz"]),
                "analog_bandwidth_hz": readback["analog_bandwidth_hz"],
            }
            self._rx_lo_frequency_hz = readback["rx_lo_hz"]
            return readback

    def restore_tuning(self, snapshot: dict) -> dict:
        """`read_tuning`/`apply_manual_tuning`이 준 직전 상태로 되돌린다(복구 경로).

        분해된 RF/DSP가 있으면 그대로 manual로 되돌리고, 없으면(=manual 적용 전 상태) lane별 실제
        중심 주파수를 그대로 다시 건다. 되돌린 뒤의 **실측**을 돌려준다.
        """

        lane_dsp = snapshot.get("lane_dsp_hz")
        rx_lo = snapshot.get("rx_lo_hz")
        bandwidth = snapshot.get("analog_bandwidth_hz")
        if lane_dsp is not None and rx_lo is not None and bandwidth is not None:
            return self.apply_manual_tuning(int(rx_lo), dict(lane_dsp), int(bandwidth))
        centers = snapshot.get("lane_center_hz") or {}
        with self._control_lock:
            for lane in range(LANE_COUNT):
                center = centers.get(lane, centers.get(str(lane)))
                if center is None:
                    continue
                if bandwidth is not None:
                    self._source.set_bandwidth(int(bandwidth), lane)
                self._source.set_center_freq(float(int(center)), lane)
            self._manual_rx_tuning = None
            restored = self._rx_tuning_snapshot()
            self._rx_lo_frequency_hz = restored["lane_center_hz"][0]
            return restored

    def sensor_readback(self) -> dict:
        readback = {}
        try:
            names = tuple(self._source.get_mboard_sensor_names(0))
            if "ref_locked" in names:
                readback["ref_locked"] = str(self._source.get_mboard_sensor("ref_locked", 0))
        except Exception:  # noqa: BLE001
            pass
        return readback

    def set_tx_mute(self, lane: int, muted: bool) -> None:
        """hardware TX gate: mute lane은 gain 0/무송신으로 유지한다."""

        self._tx_muted[lane] = bool(muted)

    def retune_tx_lo(self, target_hz: int, command_time_ns: int | None) -> None:
        """§10.1 direct MASS↔MASS retune: shared TX LO를 timed 재조정한다."""

        with self._control_lock:
            if command_time_ns is not None:
                self._sink.set_command_time(self._time_spec(command_time_ns), 0)
            try:
                for lane in range(LANE_COUNT):
                    self._sink.set_center_freq(target_hz, lane)
            finally:
                if command_time_ns is not None:
                    self._sink.clear_command_time(0)
        self._tx_lo_frequency_hz = target_hz

    # --- TX/RX ---------------------------------------------------------------

    def submit_tx_group(self, bursts) -> None:
        """한 timed TX를 lane group으로 제출한다.

        `bursts`는 `[(lane, data_bytes, hardware_time_ns), ...]` (1 또는 2개)다.
        dual-channel `usrp_sink`는 두 입력을 정렬 소비하므로, 한 lane만 실제
        burst를 가지면 다른 lane에는 같은 길이·같은 시각의 zero companion
        burst를 채워 정렬을 유지한다. zero burst는 RF energy가 없는 정렬용
        padding이며 별도 송신 주장으로 집계하지 않는다.
        """

        if not bursts or len(bursts) > LANE_COUNT:
            raise ValueError("bursts는 1..2개여야 합니다")
        by_lane = {}
        for lane, data, hardware_time_ns in bursts:
            if lane in by_lane:
                raise ValueError("같은 lane에 중복 burst를 제출할 수 없습니다")
            if self._tx_muted[lane]:
                raise RuntimeError(f"lane {lane} TX gate is inhibited")
            by_lane[lane] = (data, hardware_time_ns)
        lengths = {len(data) for data, _ in by_lane.values()}
        times = {time_ns for _, time_ns in by_lane.values()}
        if len(lengths) != 1 or len(times) != 1:
            raise ValueError("group burst는 길이와 first-sample 시각이 같아야 합니다")
        length = lengths.pop()
        time_ns = times.pop()
        import os as _os
        if _os.environ.get("STAGE7_DIAG", "0") == "1":
            import sys as _sys
            lead_ms = (time_ns - int(self.hardware_time_ns())) / 1e6
            print(
                f"[diag-worker] tx-submit lanes={sorted(by_lane)} "
                f"t_ns={time_ns} lead_ms={lead_ms:.1f}",
                file=_sys.stderr, flush=True,
            )
        for lane in range(LANE_COUNT):
            if lane in by_lane:
                data, hardware_time_ns = by_lane[lane]
                samples = self._numpy.frombuffer(
                    data, dtype=self._numpy.dtype("<c8")
                ).astype(self._numpy.complex64, copy=True)
            else:
                samples = self._numpy.zeros(length // 8, dtype=self._numpy.complex64)
                hardware_time_ns = time_ns
            self._tx_sources[lane].enqueue(samples, hardware_time_ns)
            try:
                # 유휴 source 블록 스레드 즉시 기상 (콜드 픽업 지각 방지).
                self._tx_sources[lane].to_basic_block()._post(
                    self._pmt.intern("wake"), self._pmt.PMT_T
                )
            except Exception:  # noqa: BLE001 - wake 실패해도 기존 폴링 경로로 진행
                pass

    def bracket_hardware_time(self) -> tuple[int, int, int]:
        """§13.2 bracket: UHD get_time을 worker 안에서 host raw로 감싼다.

        (host_raw_before_ns, hardware_time_ns, host_raw_after_ns). RPC 왕복
        지연이 bracket 폭에 들어가지 않는다."""

        import time as _t

        m_before = _t.clock_gettime_ns(_t.CLOCK_MONOTONIC_RAW)
        hardware_ns = self.hardware_time_ns()
        m_after = _t.clock_gettime_ns(_t.CLOCK_MONOTONIC_RAW)
        return (m_before, int(hardware_ns), m_after)

    def set_rx_gain(self, lane: int, gain_db: float) -> float:
        """§9.3 probe/§9.2 search용 RX gain 변경. 실제 readback을 반환한다."""

        if not _range_contains(self._source.get_gain_range(lane), gain_db):
            raise ValueError("requested RX gain is outside the B210 range")
        self._source.set_gain(gain_db, lane)
        return float(self._source.get_gain(lane))

    def set_tx_gain(self, lane: int, gain_db: float) -> float:
        """§9.3 probe/§9.2 search용 TX gain 변경. 실제 readback을 반환한다."""

        if not _range_contains(self._sink.get_gain_range(lane), gain_db):
            raise ValueError("requested TX gain is outside the B210 range")
        self._sink.set_gain(gain_db, lane)
        return float(self._sink.get_gain(lane))

    def submit_rx_lane(self, lane: int, hardware_time_ns: int, sample_count: int) -> None:
        stream_mode = getattr(
            self._uhd.stream_cmd, "STREAM_MODE_NUM_SAMPS_AND_DONE", None
        )
        if stream_mode is None:
            stream_mode = self._uhd.stream_mode_t.STREAM_MODE_NUM_SAMPS_AND_DONE
        command = self._uhd.stream_cmd(stream_mode)
        command.num_samps = sample_count
        command.stream_now = False
        command.time_spec = self._time_spec(hardware_time_ns)
        other = 1 - lane
        # dual-channel usrp_source의 stream command는 두 channel에 함께
        # 적용되므로 상대 lane collector에도 같은 창(companion)을 등록해
        # 고아 rx_time 태그가 stream error를 만들지 않게 한다. 이전 companion
        # 창(과거 stream의 미소비 데이터)은 여기서 폐기한다 — directed lane은
        # 한 시점에 하나만 활성이라(§9.2) 상대 lane의 실제 창과 겹치지 않는다.
        for stale in self._companion_windows[lane]:
            self._rx_collectors[lane].unregister_window(stale)
        self._companion_windows[lane] = []
        for stale in self._companion_windows[other]:
            self._rx_collectors[other].unregister_window(stale)
        self._companion_windows[other] = []
        self._rx_collectors[lane].register_window(hardware_time_ns, sample_count)
        companion_added = False
        try:
            self._rx_collectors[other].register_window(hardware_time_ns, sample_count)
            self._companion_windows[other].append(hardware_time_ns)
            companion_added = True
        except ValueError:
            # 서비스 양-lane fanout: 상대 lane에 같은 시각의 실제 창이 이미
            # 등록돼 있으면 companion이 불필요하다 — 실제 창이 stream 정렬을
            # 대신한다 (svcdbg29 실측: companion과 실제 창의 같은-시각 충돌이
            # 'duplicate RX window hardware time'으로 서비스 RX 전체를 거부).
            pass
        if not companion_added:
            # 상대 lane의 같은 시각 실제 창이 이미 stream 명령을 발행했다 —
            # dual-channel stream은 양 채널을 함께 캡처하므로 재발행하면
            # 이중 캡처가 된다. 등록만 하고 명령은 생략한다.
            return
        try:
            self._source.issue_stream_cmd(command)
        except Exception:
            self._rx_collectors[lane].unregister_window(hardware_time_ns)
            self._rx_collectors[other].unregister_window(hardware_time_ns)
            self._companion_windows[other] = [
                t for t in self._companion_windows[other]
                if t != hardware_time_ns
            ]
            raise

    def register_rx_window_lane(self, lane: int, hardware_time_ns: int, sample_count: int) -> None:
        self._rx_collectors[lane].register_window(hardware_time_ns, sample_count)

    def unregister_rx_lane(self, lane: int, hardware_time_ns: int) -> None:
        # timeout으로 폐기된 창을 collector에서 해제해 보관 byte를 돌려준다.
        # (svcdbg41 실측: 미해제 stale 창이 max_bytes 예산을 소진 → 이후 모든
        # 창이 byte 0 수용의 빈 창(segs=0)이 되는 누적 기아.)
        self._rx_collectors[lane].unregister_window(hardware_time_ns)
        self._companion_windows[lane] = [
            t for t in self._companion_windows[lane] if t != hardware_time_ns
        ]

    def start_rx_continuous(self) -> None:
        """서비스 모드 연속 RX: 창별 유한 스트림 대신 단일 연속 스트림을 연다.

        인접 창의 창별 재시작 지연(svcdbg41: 빈 창 2/3, +117µs 지각)이
        원천 소멸한다. 창 경계는 collector가 합성 rx_time 태그로 공급한다."""

        command = self._uhd.stream_cmd(
            self._uhd.stream_mode_t.STREAM_MODE_START_CONTINUOUS
        )
        # dual-channel streamer는 stream_now 즉시 시작을 거부한다
        # ("stream now on multiple channels ... fail to time align") —
        # 시각 지정 시작으로 양 채널을 정렬한다.
        command.stream_now = False
        start_ns = int(self.hardware_time_ns()) + 200_000_000
        command.time_spec = self._time_spec(start_ns)
        for collector in self._rx_collectors:
            collector._svc_anchor = None
            collector._svc_continuous = True
        self._source.issue_stream_cmd(command)
        # 연속 RX가 **언제부터** 유효한지 보존한다. 상위 readiness는 "연속 RX가 켜져 있다"가 아니라
        # "예약 TX 시각이 이 시작 이후"임을 확인해야 한다 — 시작 전 시각은 수신되지 않는다.
        self._rx_continuous_since_ns = start_ns

    def rx_continuous_since_ns(self):
        """연속 RX 스트림이 유효해지는 hardware ns. 연속 RX가 아니면 None."""

        return self._rx_continuous_since_ns

    def stop_rx_continuous(self) -> None:
        command = self._uhd.stream_cmd(
            self._uhd.stream_mode_t.STREAM_MODE_STOP_CONTINUOUS
        )
        command.stream_now = True
        try:
            self._source.issue_stream_cmd(command)
        finally:
            for collector in self._rx_collectors:
                collector._svc_continuous = False
            self._rx_continuous_since_ns = None

    def read_rx_cf32_lane(
        self,
        lane: int,
        sample_count: int,
        allow_partial: bool = False,
        expected_hardware_time_ns: int | None = None,
    ):
        try:
            return self._rx_collectors[lane].read(
                sample_count, allow_partial, expected_hardware_time_ns
            )
        except RuntimeError as error:
            if "RX timestamp" not in str(error):
                raise
            # frozen assembler의 stream 상관 오류는 sticky여서 read가 영구
            # raise → adapter가 장치 손실로 오판·전면 정지한다. 서비스
            # 밀집 스케줄에선 stray rx_time(overflow 재시작 tag 등)이 실재
            # 하므로 lane 사망 대신 assembler 교체로 강등한다 — 미회수
            # 창은 read None → 기존 RX_TIMEOUT 경로로 창 단위 실패 처리.
            self._rx_assembler_resets = getattr(self, "_rx_assembler_resets", 0) + 1
            import sys as _sys
            print(
                f"[stage7-worker] lane{lane} stream 상관 오류 #"
                f"{self._rx_assembler_resets} — assembler 재생성: {error}",
                file=_sys.stderr, flush=True,
            )
            fresh = _ServiceRxWindowAssembler()
            fresh.set_max_bytes(getattr(self, "_rx_plan_max_bytes", 8_388_608))
            fresh.set_sample_rate(getattr(self, "_rx_plan_sample_rate_hz", 307_200))
            self._rx_collectors[lane]._assembler = fresh
            self._companion_windows[lane] = []
            return None

    def poll_async(self) -> tuple[dict, ...]:
        with self._async_lock:
            messages, self._async_messages[:] = tuple(self._async_messages), []
        return tuple(self._decode_async(message) for message in messages)

    def wait_for_activity(self, timeout_seconds: float) -> None:
        self._activity_event.wait(timeout_seconds)
        self._activity_event.clear()

    def _decode_async(self, message) -> dict:
        try:
            decoded = self._pmt.to_python(message)
        except Exception:  # noqa: BLE001
            decoded = message
        event_code = _find_named_value(decoded, "event_code")
        if isinstance(event_code, bool):
            event_code = None
        try:
            event_code = int(event_code) if event_code is not None else None
        except (TypeError, ValueError):
            event_code = None
        text = _flatten_text(decoded).upper()
        channel = _find_named_value(decoded, "channel")
        if event_code is not None and event_code & 0x08:
            outcome = "TIME_ERROR"
        elif event_code is not None and event_code & (0x04 | 0x20):
            outcome = "SEQUENCE_ERROR"
        elif event_code is not None and event_code & (0x02 | 0x10):
            outcome = "UNDERFLOW"
        elif event_code is not None and event_code & 0x01:
            outcome = "BURST_ACK"
        elif "BURST_ACK" in text:
            outcome = "BURST_ACK"
        elif "UNDERFLOW" in text:
            outcome = "UNDERFLOW"
        elif "TIME" in text or "LATE" in text:
            outcome = "TIME_ERROR"
        elif "SEQ" in text:
            outcome = "SEQUENCE_ERROR"
        else:
            outcome = "STREAM_ERROR"
        try:
            channel = int(channel) if channel is not None else None
        except (TypeError, ValueError):
            channel = None
        return {
            "outcome": outcome,
            "hardware_time_ns": _find_hardware_time_ns(decoded),
            "channel": channel,
        }

    def _time_spec(self, nanoseconds: int):
        return self._uhd.time_spec(
            nanoseconds // 1_000_000_000,
            (nanoseconds % 1_000_000_000) / 1_000_000_000.0,
        )


class Stage7IsolatedDualLaneWorker(ProcessIsolatedGrUhdBackend):
    """frozen process 격리 proxy의 additive dual-lane 확장.

    frozen `_rpc` 기반으로 lane-aware method만 추가한다. spawn된 child process가
    `Stage7DualLaneFlowgraphBackend` 하나를 단독 소유한다.
    """

    def __init__(self, device_selector: str, cpu_format: str, otw_format: str, **kwargs) -> None:
        kwargs.setdefault("_backend_type", Stage7DualLaneFlowgraphBackend)
        super().__init__(device_selector, cpu_format, otw_format, **kwargs)

    @staticmethod
    def discover(device_selector: str) -> tuple[str, ...]:
        return Stage7DualLaneFlowgraphBackend.discover(device_selector)

    def configure_dual(self, plan: dict) -> dict:
        return self._rpc(
            "configure_dual", plan, timeout_seconds=self._STARTUP_TIMEOUT_SECONDS
        )

    def lo_readback(self) -> dict:
        return self._rpc("lo_readback")

    def read_tuning(self) -> dict:
        return self._rpc("read_tuning")

    def apply_manual_tuning(self, rx_lo_hz: int, lane_dsp_hz, analog_bandwidth_hz: int) -> dict:
        return self._rpc("apply_manual_tuning", int(rx_lo_hz),
                         {int(lane): int(value) for lane, value in dict(lane_dsp_hz).items()},
                         int(analog_bandwidth_hz))

    def restore_tuning(self, snapshot: dict) -> dict:
        return self._rpc("restore_tuning", dict(snapshot))

    def sensor_readback(self) -> dict:
        return self._rpc("sensor_readback")

    def poc_runtime_state(self) -> dict:
        return self._rpc("poc_runtime_state")

    def set_tx_mute(self, lane: int, muted: bool) -> None:
        self._rpc("set_tx_mute", lane, muted)

    def retune_tx_lo(self, target_hz: int, command_time_ns: int | None) -> None:
        self._rpc("retune_tx_lo", target_hz, command_time_ns)

    def submit_tx_group(self, bursts) -> None:
        self._rpc("submit_tx_group", bursts)

    def bracket_hardware_time(self):
        return self._rpc("bracket_hardware_time")

    def set_rx_gain(self, lane: int, gain_db: float) -> float:
        return self._rpc("set_rx_gain", lane, gain_db)

    def set_tx_gain(self, lane: int, gain_db: float) -> float:
        return self._rpc("set_tx_gain", lane, gain_db)

    def submit_rx_lane(self, lane: int, hardware_time_ns: int, sample_count: int) -> None:
        self._rpc("submit_rx_lane", lane, hardware_time_ns, sample_count)

    def register_rx_window_lane(self, lane: int, hardware_time_ns: int, sample_count: int) -> None:
        self._rpc("register_rx_window_lane", lane, hardware_time_ns, sample_count)

    def unregister_rx_lane(self, lane: int, hardware_time_ns: int) -> None:
        self._rpc("unregister_rx_lane", lane, hardware_time_ns)

    def start_rx_continuous(self) -> None:
        self._rpc("start_rx_continuous")

    def rx_continuous_since_ns(self):
        return self._rpc("rx_continuous_since_ns")

    def stop_rx_continuous(self) -> None:
        self._rpc("stop_rx_continuous")

    def read_rx_cf32_lane(
        self,
        lane: int,
        sample_count: int,
        *,
        allow_partial: bool = False,
        expected_hardware_time_ns: int | None = None,
    ):
        return self._rpc(
            "read_rx_cf32_lane",
            lane,
            sample_count,
            allow_partial=allow_partial,
            expected_hardware_time_ns=expected_hardware_time_ns,
        )


__all__ = (
    "LANE_COUNT",
    "Stage7DualLaneFlowgraphBackend",
    "Stage7IsolatedDualLaneWorker",
)
