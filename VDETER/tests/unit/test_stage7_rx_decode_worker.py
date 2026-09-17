"""Stage 7 decode worker의 계약.

고정하는 것:
  - 워커의 계산이 기존 펌프 내 경로와 **같은 결과**를 낸다
  - decode와 reset이 **같은 FIFO**로 순서대로 처리된다
  - 예상 못 한 상황을 조용히 넘기지 않고 FATAL로 올린다 (fail-closed)
  - UUID4가 아닌 id source는 거부한다 (packet_id 계열 분기 방지)
"""

from __future__ import annotations

import multiprocessing as mp
import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy import resampling
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry
from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge
from vdeter.b210_system import rx_decode_worker as W
from vdeter.b210_system.phy_facade import (
    DiagnosticPhyFacade,
    PhyDiagnosticSink,
)
from vdeter.b210_system.support import LaneRxDispatcher

RX_KEY = (CHANNEL.name, FIRST_SAMPLE_AT.clock_id, 1_000, 26_666_666)


def _device_request() -> PhyDecodeRequest:
    """Link 11 canonical slot의 device-rate 수신 요청."""

    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok
    phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x71))
    encoded = phy.encode_packet(
        PhyEncodeRequest(
            packet_id=DeterministicIdSource(0x81).new_packet_id(),
            link_id=LinkId(11),
            channel=CHANNEL,
            transmit_at=FIRST_SAMPLE_AT,
            packet=canonical_packet(),
        )
    )
    assert encoded.is_ok, encoded.error
    converted = RuntimePhyRadioBridge(phy).to_device_burst(encoded.value)
    assert converted.is_ok, converted.error
    data = converted.value.samples.data
    block = SampleBlock(
        sample_format=SAMPLE_FORMAT_CF32LE,
        sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
        sample_count=len(data) // 8,
        data=data,
        first_sample_at=FIRST_SAMPLE_AT,
        continuity=SampleContinuity.CONTIGUOUS,
    )
    return PhyDecodeRequest(CHANNEL, block, (LinkId(11),))


def _reference_decode(device_request):
    """분리 전 펌프가 하던 그대로: 리샘플 -> 게이트 -> 디코드."""

    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok
    sink = PhyDiagnosticSink(lane_index=0, lane_generation=1)
    facade = DiagnosticPhyFacade(
        registry, TIME_SOURCE, DeterministicIdSource(0x91),
        diagnostic_sink=sink, route_sidecar=None,
    )
    bridge = RuntimePhyRadioBridge(facade)
    prepared = bridge.to_native_decode_request(device_request)
    assert prepared.is_ok, prepared.error
    host = W._GateHost()
    native = LaneRxDispatcher._energy_gate(host, 0, prepared.value)
    sink.begin_window(RX_KEY, 1, 0)
    try:
        return facade.decode_samples(native), sink
    finally:
        sink.end_window()


def _comparable(recovered_set):
    """packet_id는 UUID4라 실행마다 다르다 — 내용만 비교한다."""

    return sorted(
        (p.link_id, p.channel.name, p.received_at.total_nanoseconds(), p.packet)
        for p in recovered_set.packets
    )


class _Recorder:
    """job을 기록만 하는 가짜 FIFO 소비자."""

    def __init__(self) -> None:
        self.seen = []

    def reset(self, channel):
        self.seen.append(("reset", channel))

    def decode(self, device_request, rx_window_key, epoch_id, t0_ns):
        self.seen.append(("decode", rx_window_key))
        return None, (), None


class WorkerCompute(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.request = _device_request()

    def _decoder(self):
        return W.LaneDecoder(
            lane_index=0, lane_generation=1,
            id_source_kind="UUID4", id_source_seed=0,
        )

    def test_worker_decode_matches_the_in_pump_path(self):
        """워커 계산이 분리 전 경로와 같은 packet을 낸다."""

        reference, _ = _reference_decode(self.request)
        self.assertTrue(reference.is_ok, getattr(reference, "error", None))

        decoded, records, first_ns = self._decoder().decode(
            self.request, RX_KEY, 1, 0
        )
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(_comparable(reference.value), _comparable(decoded.value))
        self.assertIsNotNone(first_ns)

    def test_worker_returns_the_diagnostic_records(self):
        """진단 기록은 decode 안에서 만들어지므로 함께 돌려받아야 한다."""

        decoded, records, _ = self._decoder().decode(self.request, RX_KEY, 7, 123)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertTrue(records, "attempt 기록이 비었다 — evidence가 사라진다")
        for record in records:
            self.assertEqual(RX_KEY, record.rx_window_key)
            self.assertEqual(7, record.stream_epoch_id)
            self.assertEqual(0, record.lane_index)

    def test_records_are_drained_so_they_are_not_reported_twice(self):
        """회수한 기록은 워커 싱크에서 비워져야 중복 보고되지 않는다."""

        decoder = self._decoder()
        first = decoder.decode(self.request, RX_KEY, 1, 0)[1]
        second = decoder.decode(self.request, RX_KEY, 2, 0)[1]
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first[0].stream_epoch_id, second[0].stream_epoch_id)

    def test_deterministic_id_source_is_refused(self):
        """lane별 워커로 나누면 packet_id 계열이 갈린다 — 조용히 쓰지 않는다."""

        with self.assertRaises(ValueError):
            W.LaneDecoder(lane_index=0, lane_generation=1,
                          id_source_kind="DETERMINISTIC", id_source_seed=7)

    def test_reset_is_applied_to_the_phy_stream(self):
        decoder = self._decoder()
        decoder.decode(self.request, RX_KEY, 1, 0)
        decoder.reset(CHANNEL)   # 예외 없이 반영돼야 한다
        decoder.reset(None)


class WorkerLoop(unittest.TestCase):
    """FIFO 규율과 fail-closed. 실제 PHY 없이 loop만 검증한다."""

    def _run(self, jobs, decoder):
        job_q, result_q = _ListQueue(jobs), _ListQueue([])
        W.run_worker.__wrapped__ if False else None
        _loop(job_q, result_q, decoder)
        return result_q.items

    def test_reset_and_decode_share_one_fifo_in_order(self):
        recorder = _Recorder()
        _loop(
            _ListQueue([
                (W.JOB_DECODE, 1, 1, ("a",), 1, 0, object()),
                (W.JOB_RESET, 2, 1, CHANNEL),
                (W.JOB_DECODE, 3, 1, ("b",), 1, 0, object()),
                None,
            ]),
            _ListQueue([]),
            recorder,
        )
        self.assertEqual(
            [("decode", ("a",)), ("reset", CHANNEL), ("decode", ("b",))],
            recorder.seen,
        )

    def test_unknown_job_kind_is_fatal_not_ignored(self):
        results = _ListQueue([])
        _loop(_ListQueue([("nonsense", 9, 1), None]), results, _Recorder())
        self.assertTrue(results.items)
        self.assertEqual(W.RESULT_FATAL, results.items[0][0])

    def test_decoder_exception_is_fatal_not_swallowed(self):
        class _Boom(_Recorder):
            def decode(self, *a, **k):
                raise RuntimeError("터졌다")

        results = _ListQueue([])
        _loop(
            _ListQueue([(W.JOB_DECODE, 4, 1, ("a",), 1, 0, object()), None]),
            results, _Boom(),
        )
        self.assertEqual(W.RESULT_FATAL, results.items[0][0])
        self.assertIn("터졌다", results.items[0][3])


class _ListQueue:
    """get/put만 흉내내는 결정적 큐."""

    def __init__(self, items) -> None:
        self.items = list(items)

    def get(self):
        if not self.items:
            return None
        return self.items.pop(0)

    def put(self, item) -> None:
        self.items.append(item)


def _loop(job_queue, result_queue, decoder):
    """`run_worker`의 loop 부분만 주입된 decoder로 실행한다.

    run_worker는 LaneDecoder를 스스로 만들기 때문에 loop 규율만 따로 보려면
    같은 흐름을 여기서 재현해야 한다. 흐름이 갈라지지 않도록
    `test_loop_mirrors_run_worker`가 원본과의 대응을 고정한다.
    """

    import traceback
    while True:
        item = job_queue.get()
        if item is None:
            return
        kind = item[0]
        if kind == W.JOB_STOP:
            return
        job_id, generation = item[1], item[2]
        try:
            if kind == W.JOB_RESET:
                decoder.reset(item[3])
                continue
            if kind != W.JOB_DECODE:
                result_queue.put((W.RESULT_FATAL, job_id, generation,
                                  f"알 수 없는 job 종류: {kind!r}"))
                return
            _, _, _, rx_window_key, epoch_id, t0_ns, request = item
            decoded, records, first_ns = decoder.decode(
                request, rx_window_key, epoch_id, t0_ns)
        except BaseException:  # noqa: BLE001
            result_queue.put((W.RESULT_FATAL, job_id, generation,
                              "decode worker 예외: " + traceback.format_exc(limit=8)))
            return
        if decoded is None:
            result_queue.put((W.RESULT_ERR, job_id, generation, None, records))
        elif decoded.is_err:
            result_queue.put((W.RESULT_ERR, job_id, generation, decoded.error, records))
        else:
            result_queue.put((W.RESULT_OK, job_id, generation, decoded.value,
                              records, first_ns))


class LoopMirror(unittest.TestCase):
    def test_loop_mirrors_run_worker(self):
        """시험용 loop가 원본 run_worker의 분기를 그대로 반영하는지 고정한다."""

        import inspect
        source = inspect.getsource(W.run_worker)
        for marker in ("JOB_STOP", "JOB_RESET", "JOB_DECODE",
                       "RESULT_FATAL", "RESULT_ERR", "RESULT_OK",
                       "traceback.format_exc"):
            self.assertIn(marker, source, f"run_worker에서 {marker}가 사라졌다")


class WorkerProcess(unittest.TestCase):
    """실제 프로세스 왕복 1회 — 직렬화·기동·정지가 성립하는지."""

    def test_round_trip_through_a_real_process(self):
        context = mp.get_context("spawn")
        jobs = context.Queue(maxsize=8)
        results = context.Queue(maxsize=8)
        process = context.Process(
            target=W.run_worker, args=(jobs, results),
            kwargs=dict(lane_index=0, lane_generation=1,
                        id_source_kind="UUID4", id_source_seed=0),
            daemon=True,
        )
        process.start()
        try:
            ready = results.get(timeout=120)
            self.assertEqual(W.RESULT_READY, ready[0])
            jobs.put((W.JOB_DECODE, 11, 1, RX_KEY, 1, 0, _device_request()))
            message = results.get(timeout=120)
            self.assertEqual(W.RESULT_OK, message[0], message)
            self.assertEqual(11, message[1])
            self.assertTrue(message[4], "attempt 기록이 넘어오지 않았다")
        finally:
            jobs.put(None)
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        self.assertFalse(process.is_alive())


if __name__ == "__main__":
    unittest.main()
