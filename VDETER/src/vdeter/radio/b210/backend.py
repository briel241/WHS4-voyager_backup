"""Private headless programmatic GNU Radio gr-uhd flowgraph for USRP B210.

The production path constructs ``gnuradio.uhd.usrp_source`` and ``usrp_sink``
directly.  It does not import GRC output and does not create a Python ``MultiUSRP``
or direct UHD streamer.
"""

from __future__ import annotations

import importlib
import subprocess
import time
from collections import deque
from threading import Event, Lock, Thread


class _RxWindowAssembler:
    """Thread-safe finite-window sample assembler independent of GNU Radio.

    ``rx_time`` is correlated by the registered hardware interval, never by
    registration order.  Keeping this state machine outside the gr block makes
    the safety-critical correlation rules directly testable without a B210 or
    GNU Radio installation.
    """

    def __init__(self, maximum_bytes=8_388_608, sample_rate_hz=76_800):
        self._max_bytes = maximum_bytes
        self._retained_bytes = 0
        self._sample_rate_hz = sample_rate_hz
        self._windows = []
        self._current = None
        self._stream_error = None
        self._lock = Lock()

    def set_max_bytes(self, maximum):
        with self._lock:
            self._max_bytes = maximum

    def set_sample_rate(self, sample_rate_hz):
        with self._lock:
            self._sample_rate_hz = sample_rate_hz

    def register_window(self, hardware_time_ns, sample_count):
        window = {
            "expected_hardware_time_ns": hardware_time_ns,
            "expected_sample_count": sample_count,
            "actual_hardware_time_ns": None,
            "next_fragment_hardware_time_ns": None,
            "observed_sample_count": 0,
            "overflow_samples": 0,
            "fragments": [],
            "gap_pending": False,
            "started": False,
            "complete": False,
        }
        with self._lock:
            if any(
                value["expected_hardware_time_ns"] == hardware_time_ns
                for value in self._windows
            ):
                raise ValueError("duplicate RX window hardware time")
            self._windows.append(window)
            self._windows.sort(key=lambda value: value["expected_hardware_time_ns"])

    def unregister_window(self, hardware_time_ns):
        with self._lock:
            for window in tuple(self._windows):
                if window["expected_hardware_time_ns"] == hardware_time_ns:
                    if self._current is window:
                        self._current = None
                    self._retained_bytes -= sum(
                        len(fragment[0]) for fragment in window["fragments"]
                    )
                    self._windows.remove(window)
                    return

    def feed(self, encoded, rx_times=()):
        if len(encoded) % 8:
            raise ValueError("RX cf32le bytes must be sample aligned")
        count = len(encoded) // 8
        ordered = sorted(rx_times, key=lambda value: value[0])
        with self._lock:
            cursor = 0
            for offset, hardware_time_ns in ordered:
                offset = max(cursor, min(count, int(offset)))
                if offset > cursor:
                    self._append(encoded[cursor * 8 : offset * 8], offset - cursor)
                self._begin_timestamp(int(hardware_time_ns))
                cursor = offset
            if cursor < count:
                self._append(encoded[cursor * 8 :], count - cursor)

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
                # A later native timestamp proves this earlier finite command
                # produced no correlatable samples.  The adapter will turn the
                # empty completed result into the required timeout terminal.
                window["complete"] = True
        if len(candidates) != 1:
            self._stream_error = (
                "RX timestamp does not identify exactly one registered window: "
                f"{actual_hardware_time_ns}"
            )
            self._current = None
            return
        window = candidates[0]
        if self._current is not None and self._current is not window:
            self._current["complete"] = True
        if window["started"]:
            # A second rx_time inside one finite window denotes a new native
            # contiguous fragment (for example after an overflow/restart).
            window["gap_pending"] = window["observed_sample_count"] > 0
        else:
            window["started"] = True
            window["actual_hardware_time_ns"] = actual_hardware_time_ns
        window["next_fragment_hardware_time_ns"] = actual_hardware_time_ns
        self._current = window

    def _append(self, encoded, sample_count):
        window = self._current
        if window is None or sample_count <= 0:
            return
        remaining = window["expected_sample_count"] - window["observed_sample_count"]
        accepted_input = min(sample_count, max(0, remaining))
        if accepted_input <= 0:
            window["complete"] = True
            self._current = None
            return
        available = max(0, (self._max_bytes - self._retained_bytes) // 8)
        accepted = min(accepted_input, available)
        observed_before = window["observed_sample_count"]
        if accepted:
            if not window["fragments"] or window["gap_pending"]:
                fragment_start = window["next_fragment_hardware_time_ns"]
                if fragment_start is None:
                    actual_start = window["actual_hardware_time_ns"]
                    fragment_start = None if actual_start is None else (
                        actual_start
                        + _sample_offset_ns(observed_before, self._sample_rate_hz)
                    )
                window["fragments"].append(
                    [bytearray(), fragment_start, window["gap_pending"]]
                )
            window["fragments"][-1][0].extend(encoded[: accepted * 8])
            self._retained_bytes += accepted * 8
            window["gap_pending"] = False
            window["next_fragment_hardware_time_ns"] = None
        dropped = accepted_input - accepted
        if dropped:
            window["overflow_samples"] += dropped
            window["gap_pending"] = True
        window["observed_sample_count"] += accepted_input
        if window["observed_sample_count"] >= window["expected_sample_count"]:
            window["complete"] = True
            self._current = None

    def read(self, sample_count, allow_partial, expected_hardware_time_ns=None):
        with self._lock:
            if self._stream_error is not None:
                raise RuntimeError(self._stream_error)
            window = next(
                (
                    value for value in self._windows
                    if expected_hardware_time_ns is None
                    or value["expected_hardware_time_ns"] == expected_hardware_time_ns
                ),
                None,
            )
            if window is None:
                return None
            if (
                not window["complete"]
                and window["observed_sample_count"] < sample_count
                and not allow_partial
            ):
                return None
            if self._current is window:
                self._current = None
            self._windows.remove(window)
            fragments = tuple(
                (bytes(data), hardware_time_ns, gap_before)
                for data, hardware_time_ns, gap_before in window["fragments"]
            )
            self._retained_bytes -= sum(len(data) for data, _, _ in fragments)
            return {
                "segments": fragments,
                "overflow_samples": window["overflow_samples"],
                "trailing_gap": window["gap_pending"],
            }


class GrUhdFlowgraphBackend:
    """One-source/one-sink channel-0 B210 flowgraph built directly in Python."""

    __slots__ = (
        "_gr", "_uhd", "_pmt", "_numpy", "_top", "_source", "_sink",
        "_tx_source", "_rx_collector", "_async_collector", "_async_messages",
        "_async_lock", "_control_lock", "_started", "_closed", "_tx_center_frequency_hz",
        "_rx_center_frequency_hz", "_cleanup_event", "_cleanup_failure",
        "_cleanup_thread", "_cleanup_lock", "_connected", "_activity_event",
    )

    def __init__(self, device_selector: str, cpu_format: str, otw_format: str) -> None:
        gr = importlib.import_module("gnuradio.gr")
        uhd = importlib.import_module("gnuradio.uhd")
        pmt = importlib.import_module("pmt")
        numpy = importlib.import_module("numpy")
        self._gr, self._uhd, self._pmt, self._numpy = gr, uhd, pmt, numpy
        self._top = gr.top_block("vdeter-b210-radio")
        self._activity_event = Event()
        activity_event = self._activity_event
        stream_args = uhd.stream_args(cpu_format=cpu_format, otw_format=otw_format, channels=[0])
        self._source = uhd.usrp_source(device_selector, stream_args, False)
        self._sink = uhd.usrp_sink(device_selector, stream_args, "")

        class _TimedBurstSource(gr.sync_block):
            def __init__(source_self):
                super().__init__(name="vdeter-b210-timed-bursts", in_sig=None, out_sig=[numpy.complex64])
                source_self._queue = deque()
                source_self._current = None
                source_self._index = 0
                source_self._lock = Lock()
                source_self._underflow_probe_after_samples = None
                source_self._underflow_probe_pause_seconds = 0.0
                source_self._underflow_probe_done = False

            def enqueue(source_self, samples, hardware_time_ns):
                with source_self._lock:
                    source_self._queue.append((samples, hardware_time_ns))

            def arm_underflow_probe(source_self, after_samples, pause_seconds):
                with source_self._lock:
                    source_self._underflow_probe_after_samples = after_samples
                    source_self._underflow_probe_pause_seconds = pause_seconds
                    source_self._underflow_probe_done = False

            def work(source_self, input_items, output_items):
                output = output_items[0]
                with source_self._lock:
                    if source_self._current is None:
                        if not source_self._queue:
                            return 0
                        source_self._current = source_self._queue.popleft()
                        source_self._index = 0
                    samples, hardware_time_ns = source_self._current
                    if (
                        not source_self._underflow_probe_done
                        and source_self._underflow_probe_after_samples is not None
                        and source_self._index >= source_self._underflow_probe_after_samples
                    ):
                        source_self._underflow_probe_done = True
                        time.sleep(source_self._underflow_probe_pause_seconds)
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
                            0, absolute + produced - 1,
                            pmt.intern("tx_eob"), pmt.PMT_T,
                        )
                        source_self._current = None
                        source_self._index = 0
                    return produced

        class _BoundedRxCollector(gr.sync_block):
            def __init__(collector_self):
                super().__init__(name="vdeter-b210-rx-collector", in_sig=[numpy.complex64], out_sig=None)
                collector_self._assembler = _RxWindowAssembler()

            def set_max_bytes(collector_self, maximum):
                collector_self._assembler.set_max_bytes(maximum)

            def set_sample_rate(collector_self, sample_rate_hz):
                collector_self._assembler.set_sample_rate(sample_rate_hz)

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
                collector_self._assembler.feed(encoded, tuple(rx_times))
                activity_event.set()
                return count

            def read(
                collector_self,
                sample_count,
                allow_partial,
                expected_hardware_time_ns=None,
            ):
                return collector_self._assembler.read(
                    sample_count, allow_partial, expected_hardware_time_ns
                )

        self._tx_source = _TimedBurstSource()
        self._rx_collector = _BoundedRxCollector()
        self._async_messages = []
        self._async_lock = Lock()
        messages = self._async_messages
        messages_lock = self._async_lock

        class _AsyncCollector(gr.basic_block):
            def __init__(collector_self):
                super().__init__(name="vdeter-b210-async", in_sig=None, out_sig=None)
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
        self._tx_center_frequency_hz = None
        self._rx_center_frequency_hz = None
        self._cleanup_event = Event()
        self._cleanup_failure = []
        self._cleanup_thread = None
        self._cleanup_lock = Lock()
        self._connected = False

    @staticmethod
    def discover(device_selector: str) -> tuple[str, ...]:
        uhd = importlib.import_module("gnuradio.uhd")
        try:
            found = uhd.find_devices(device_selector)
        except AttributeError as error:
            # GNU Radio 3.10's Debian/Ubuntu wrapper still calls the removed
            # ``uhd_python.find_devices_raw`` symbol.  Use the approved
            # uhd-host discovery utility for this exact packaging defect.  The
            # fallback is discovery-only; the production streaming backend
            # continues to use the programmatic gr-uhd source and sink above.
            if "find_devices_raw" not in str(error):
                raise
            completed = subprocess.run(
                ["uhd_find_devices", f"--args={device_selector}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            output = completed.stdout + "\n" + completed.stderr
            if completed.returncode != 0:
                if "No UHD Devices Found" in output:
                    return ()
                raise RuntimeError(
                    "uhd_find_devices failed with exit code "
                    f"{completed.returncode}"
                ) from error
            serials = []
            for line in output.splitlines():
                field = line.strip()
                if field.startswith("serial:"):
                    serials.append(field.split(":", 1)[1].strip())
            return tuple(serials)
        serials = []
        for entry in found:
            if isinstance(entry, dict) and entry.get("serial"):
                serials.append(str(entry["serial"]))
                continue
            value = entry.to_string() if hasattr(entry, "to_string") else str(entry)
            for field in value.split(","):
                if field.strip().startswith("serial="):
                    serials.append(field.strip().split("=", 1)[1])
        return tuple(serials)

    def configure(self, configuration) -> dict:
        """Apply settings while inactive and return actual gr-uhd readbacks."""
        stream = configuration.b210_stream
        self._source.set_clock_rate(stream.master_clock_rate_hz, 0)
        self._sink.set_clock_rate(stream.master_clock_rate_hz, 0)
        self._source.set_clock_source("internal", 0)
        self._sink.set_clock_source("internal", 0)
        self._source.set_time_source("internal", 0)
        self._sink.set_time_source("internal", 0)

        source_info = self._source.get_usrp_info(0)
        sink_info = self._sink.get_usrp_info(0)
        source_serial = str(source_info.get("mboard_serial") or source_info.get("serial"))
        sink_serial = str(sink_info.get("mboard_serial") or sink_info.get("serial"))
        bindings = {value.channel.name: value for value in configuration.channel_bindings}
        channels = {}
        initial_tx_channel = None
        initial_rx_channel = None
        for setting in configuration.channels:
            binding = bindings[setting.channel.name]
            readback = {
                "center_frequency_hz": setting.center_frequency_hz,
                "tx_center_frequency_hz": None,
                "rx_center_frequency_hz": None,
                "tx_sample_rate_hz": None,
                "rx_sample_rate_hz": None,
                "tx_gain_db": None,
                "rx_gain_db": None,
                "tx_bandwidth_hz": None,
                "rx_bandwidth_hz": None,
                "tx_antenna": None,
                "rx_antenna": None,
                "tx_device_channel": binding.tx_device_channel,
                "rx_device_channel": binding.rx_device_channel,
            }
            if binding.tx_enabled:
                if not _range_contains(
                    self._sink.get_gain_range(0), setting.tx_gain_db
                ):
                    raise ValueError("requested TX gain is outside the B210 range")
                self._sink.set_samp_rate(setting.sample_rate_hz)
                self._sink.set_gain(setting.tx_gain_db, 0)
                self._sink.set_bandwidth(binding.analog_bandwidth_hz, 0)
                self._sink.set_antenna(binding.tx_antenna, 0)
                self._sink.set_center_freq(setting.center_frequency_hz, 0)
                readback.update(
                    center_frequency_hz=int(round(self._sink.get_center_freq(0))),
                    tx_center_frequency_hz=int(round(self._sink.get_center_freq(0))),
                    tx_sample_rate_hz=int(round(self._sink.get_samp_rate())),
                    tx_gain_db=int(round(self._sink.get_gain(0))),
                    tx_bandwidth_hz=int(round(self._sink.get_bandwidth(0))),
                    tx_antenna=str(self._sink.get_antenna(0)),
                )
                self._tx_center_frequency_hz = readback["center_frequency_hz"]
                initial_tx_channel = setting.channel.name
            if binding.rx_enabled:
                if not _range_contains(
                    self._source.get_gain_range(0), setting.rx_gain_db
                ):
                    raise ValueError("requested RX gain is outside the B210 range")
                self._source.set_samp_rate(setting.sample_rate_hz)
                self._source.set_gain(setting.rx_gain_db, 0)
                self._source.set_bandwidth(binding.analog_bandwidth_hz, 0)
                self._source.set_antenna(binding.rx_antenna, 0)
                self._source.set_center_freq(setting.center_frequency_hz, 0)
                rx_frequency = int(round(self._source.get_center_freq(0)))
                if not binding.tx_enabled:
                    readback["center_frequency_hz"] = rx_frequency
                readback.update(
                    rx_center_frequency_hz=rx_frequency,
                    rx_sample_rate_hz=int(round(self._source.get_samp_rate())),
                    rx_gain_db=int(round(self._source.get_gain(0))),
                    rx_bandwidth_hz=int(round(self._source.get_bandwidth(0))),
                    rx_antenna=str(self._source.get_antenna(0)),
                )
                self._rx_center_frequency_hz = rx_frequency
                initial_rx_channel = setting.channel.name
            channels[setting.channel.name] = readback

        self._rx_collector.set_max_bytes(configuration.resource_limits.rx_queue_max_bytes)
        self._rx_collector.set_sample_rate(76_800)
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
            "cpu_format": stream.cpu_format,
            "otw_format": stream.otw_format,
            "channels": channels,
            "initial_tx_channel": initial_tx_channel,
            "initial_rx_channel": initial_rx_channel,
        }

    def connect(self) -> None:
        """Connect the inactive graph only after readback and clock mapping pass."""

        if self._connected:
            return
        self._top.connect(self._tx_source, self._sink)
        self._top.connect(self._source, self._rx_collector)
        self._top.msg_connect(
            (self._sink, "async_msgs"), (self._async_collector, "in")
        )
        self._connected = True

    def start(self) -> None:
        if not self._connected:
            raise RuntimeError("gr-uhd flowgraph is not connected")
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
                    try:
                        # Both calls belong to the same bounded cleanup attempt.
                        # In particular, a blocking top_block.stop() must not hold
                        # the adapter caller beyond stop_timeout_ns.
                        self._top.stop()
                        self._top.wait()
                    except Exception as error:  # surfaced as a structured adapter error
                        self._cleanup_failure.append(error)
                    finally:
                        self._cleanup_event.set()

                self._cleanup_thread = Thread(
                    target=cleanup_graph,
                    name="vdeter-b210-stop",
                    daemon=True,
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

    def hardware_time_ns(self) -> int:
        value = self._source.get_time_now(0)
        return int(value.get_full_secs()) * 1_000_000_000 + int(round(value.get_frac_secs() * 1_000_000_000))

    def tx_center_frequency_hz(self) -> int:
        """Return a private hardware readback for the physical-test evidence path."""
        return int(round(self._sink.get_center_freq(0)))

    def arm_async_underflow_preflight(
        self, after_samples: int = 8_192, pause_seconds: float = 0.5
    ) -> None:
        """Arm one private source starvation used only by the physical preflight."""

        self._tx_source.arm_underflow_probe(after_samples, pause_seconds)

    def submit_tx(
        self,
        data: bytes,
        hardware_time_ns: int,
        center_frequency_hz: int,
        retune_time_ns: int | None,
    ) -> None:
        if retune_time_ns is not None:
            with self._control_lock:
                self._sink.set_command_time(self._time_spec(retune_time_ns), 0)
                try:
                    self._sink.set_center_freq(center_frequency_hz, 0)
                finally:
                    self._sink.clear_command_time(0)
            self._tx_center_frequency_hz = center_frequency_hz
        samples = self._numpy.frombuffer(data, dtype=self._numpy.dtype("<c8")).astype(
            self._numpy.complex64, copy=True
        )
        self._tx_source.enqueue(samples, hardware_time_ns)

    def submit_rx(self, hardware_time_ns: int, sample_count: int, center_frequency_hz: int) -> None:
        if self._rx_center_frequency_hz != center_frequency_hz:
            with self._control_lock:
                self._source.set_command_time(self._time_spec(hardware_time_ns), 0)
                try:
                    self._source.set_center_freq(center_frequency_hz, 0)
                finally:
                    self._source.clear_command_time(0)
            self._rx_center_frequency_hz = center_frequency_hz
        stream_mode = getattr(
            self._uhd.stream_cmd,
            "STREAM_MODE_NUM_SAMPS_AND_DONE",
            None,
        )
        if stream_mode is None:
            stream_mode = (
                self._uhd.stream_mode_t.STREAM_MODE_NUM_SAMPS_AND_DONE
            )
        command = self._uhd.stream_cmd(stream_mode)
        command.num_samps = sample_count
        command.stream_now = False
        command.time_spec = self._time_spec(hardware_time_ns)
        self._rx_collector.register_window(hardware_time_ns, sample_count)
        try:
            self._source.issue_stream_cmd(command)
        except Exception:
            self._rx_collector.unregister_window(hardware_time_ns)
            raise

    def poll_async(self) -> tuple[dict, ...]:
        with self._async_lock:
            messages, self._async_messages[:] = tuple(self._async_messages), []
        return tuple(self._decode_async(message) for message in messages)

    def wait_for_activity(self, timeout_seconds: float) -> None:
        self._activity_event.wait(timeout_seconds)
        self._activity_event.clear()

    def read_rx_cf32(
        self,
        sample_count: int,
        *,
        allow_partial: bool = False,
        expected_hardware_time_ns: int | None = None,
    ):
        return self._rx_collector.read(
            sample_count, allow_partial, expected_hardware_time_ns
        )

    def _decode_async(self, message) -> dict:
        try:
            decoded = self._pmt.to_python(message)
        except Exception:
            decoded = message
        event_code = _find_named_value(decoded, "event_code")
        if isinstance(event_code, bool):
            event_code = None
        try:
            event_code = int(event_code) if event_code is not None else None
        except (TypeError, ValueError):
            event_code = None
        text = _flatten_text(decoded).upper()
        # UHD async_metadata_t uses bit values 0x1..0x40. Error bits take
        # precedence over ACK if a backend ever combines flags.
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
        return {
            "outcome": outcome,
            "hardware_time_ns": _find_hardware_time_ns(decoded),
        }

    def _time_spec(self, nanoseconds: int):
        return self._uhd.time_spec(
            nanoseconds // 1_000_000_000,
            (nanoseconds % 1_000_000_000) / 1_000_000_000.0,
        )


def _flatten_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(f"{_flatten_text(key)} {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _sample_offset_ns(sample_offset: int, sample_rate_hz: int) -> int:
    """Convert a nonnegative sample offset with positive round-half-up semantics."""

    return (sample_offset * 1_000_000_000 + sample_rate_hz // 2) // sample_rate_hz


def _range_contains(meta_range, requested: int) -> bool:
    """Read the gr-uhd meta-range without silently relying on gain clamping."""

    start = meta_range.start() if callable(getattr(meta_range, "start", None)) else meta_range.start
    stop = meta_range.stop() if callable(getattr(meta_range, "stop", None)) else meta_range.stop
    return float(start) <= requested <= float(stop)


def _find_hardware_time_ns(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("b'", "").rstrip("'")
            if normalized in {"hardware_time_ns", "time_ns"} and isinstance(item, int):
                return item
            if normalized in {"time_spec", "time"}:
                converted = _time_value_ns(item)
                if converted is not None:
                    return converted
            found = _find_hardware_time_ns(item)
            if found is not None:
                return found
    elif isinstance(value, (tuple, list)):
        for item in value:
            found = _find_hardware_time_ns(item)
            if found is not None:
                return found
    return None


def _find_named_value(value, wanted):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("b'", "").rstrip("'")
            if normalized == wanted:
                return item
            found = _find_named_value(item, wanted)
            if found is not None:
                return found
    elif isinstance(value, (tuple, list)):
        for item in value:
            found = _find_named_value(item, wanted)
            if found is not None:
                return found
    return None


def _time_value_ns(value):
    if isinstance(value, (tuple, list)) and len(value) == 2:
        seconds, fraction = value
        if isinstance(seconds, int) and isinstance(fraction, (int, float)):
            return seconds * 1_000_000_000 + int(round(float(fraction) * 1_000_000_000))
    if hasattr(value, "get_full_secs") and hasattr(value, "get_frac_secs"):
        return (
            int(value.get_full_secs()) * 1_000_000_000
            + int(round(value.get_frac_secs() * 1_000_000_000))
        )
    return None
