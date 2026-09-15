"""권위 lead 재확인 **이후** 구간에서 비용을 치르지 않는다(D6 보완의 계약시험).

실물 C 재검증(`diag-20260914-051722`)에서 PoC action이 `RADIO_LATE_REQUEST`로 거부됐다.
스케줄러는 슬롯을 고르기 직전 최소 lead를 다시 확인하지만(`MacSlotScheduler.resolve`), 그
확인 **이후에** parent waveform 생성(변조·4배 보간·full-scale·digest)과 cf32le 직렬화가
남아 있었고 그 비용은 어느 계약에도 계상돼 있지 않았다.

여기서 고정하는 것은 **시간이 아니라 구조**다 — 타이밍은 기계 속도에 따라 흔들리지만, "무엇이
언제 호출되는가"는 결정적이다. 그래서 지연을 주입해 통과시키는 방식이 아니라 다음을 고정한다.

1. 파형은 슬롯 선택 **전에** 만들어진다.
2. 임계 구간의 sink는 미리 만든 직렬화를 쓰고 **다시 계산하지 않는다**.
3. 그 캐시는 재계산 결과와 **바이트 동일**하다(다르면 장치에 다른 파형이 나간다).
4. 캐시를 제공하지 않는 factory도 그대로 동작한다(fallback).

권위 lead 값·timeout·retry·슬롯 의미는 이 시험이 바꾸지 않는다.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from vdeter.poc.api.types import ActionDescriptor, ActionKind, PhysicalChannel
from vdeter.poc.shared_medium import testing as TD
from vdeter.poc.shared_medium.iq import IqError, IqBurst, sha256_cf32le, to_cf32le
from vdeter.poc.shared_medium.provider import (
    NextSafeSlotScheduler,
    SharedMediumActionProvider,
)
from vdeter.poc.shared_medium.radiation import ChildPath, ReferenceWaveformFactory

_STATIONS = {"MASS-A": 111_111_111, "ROC": 222_222_222}


def _action(target="ROC"):
    return ActionDescriptor(
        kind=ActionKind.REPLAY_PACKET, target=target, source_observation_id=None,
        payload={"packet_hex": "48454c4c4f"}, schedule={"kind": "NEXT_SAFE_SLOT"},
    )


class _OrderRecorder:
    """factory와 scheduler의 호출 순서를 한 리스트에 기록한다."""

    def __init__(self) -> None:
        self.order: list = []

    def factory(self, inner=None):
        recorder, inner = self, inner or ReferenceWaveformFactory()

        class _Factory:
            def encode(self, *, source_station, channel, payload):
                recorder.order.append("waveform")
                return inner.encode(source_station=source_station, channel=channel,
                                    payload=payload)

        return _Factory()

    def scheduler(self):
        recorder = self

        class _Scheduler(NextSafeSlotScheduler):
            def resolve(self, action, *, request_id):
                recorder.order.append("slot")
                return super().resolve(action, request_id=request_id)

        return _Scheduler(current_slot=0, lead_slots=1)


def _provider(*, factory, scheduler, sink=None):
    return SharedMediumActionProvider(
        radio_sink=sink or TD.CountingRadioSink(),
        rx_readiness=TD.ReadyReadiness(),
        scheduler=scheduler,
        waveform_factory=factory,
        arbiter=TD.FakeArbiter(),
        allow_rf_off_defaults=True,
    )


class WaveformPrecedesSlotSelectionTest(unittest.TestCase):
    """P1: 슬롯과 무관한 파형 생성은 슬롯 선택 앞에서 끝난다."""

    def test_waveform_is_built_before_the_slot_is_chosen(self):
        recorder = _OrderRecorder()
        prov = _provider(factory=recorder.factory(), scheduler=recorder.scheduler())
        prov.submit(_action(), request_id="r1")
        self.assertEqual(recorder.order[:2], ["waveform", "slot"],
                         f"파형이 슬롯 선택 뒤에 생성됐다: {recorder.order}")

    def test_waveform_is_built_exactly_once_per_action(self):
        # 앞당긴 파형을 build_parent가 다시 만들면 비용을 옮긴 게 아니라 늘린 것이다.
        recorder = _OrderRecorder()
        prov = _provider(factory=recorder.factory(), scheduler=recorder.scheduler())
        prov.submit(_action(), request_id="r1")
        self.assertEqual(recorder.order.count("waveform"), 1, recorder.order)


class PreSerializedBytesTest(unittest.TestCase):
    """P2: 임계 구간의 sink는 미리 만든 직렬화를 쓰고 다시 계산하지 않는다."""

    @staticmethod
    def _children(samples, *, cf32le):
        return tuple(
            ChildPath(child_id=f"p:{cable}", parent_id="p", transfer_id="t", cable=cable,
                      tx_station="MASS-B", tx_lane=lane, rx_station=rx, rx_lane=1,
                      channel=PhysicalChannel.UPPER, payload_digest="d" * 64,
                      waveform_sha256="w" * 64, scheduled_sample=0,
                      scheduled_hw_timestamp_ns=5_000_000_000, samples=samples,
                      wire_packet_digest="a" * 64, addressed_destination="MASS-A",
                      samples_cf32le=cf32le)
            for cable, lane, rx in (("C4", 0, "ROC"), ("C6", 1, "MASS-A"))
        )

    def test_launcher_sink_sends_the_preserialized_bytes_without_recomputing(self):
        import base64

        from apps.poc_device_seams_launcher import LauncherRadioSink

        samples = (0.5 + 0.5j,) * 8
        expected = to_cf32le(samples)
        calls: list = []

        class _Client:
            def call(self, name, body):
                calls.append((name, body))
                return {"accepted": True, "burst_id": "b", "transmit_at_utc_ns": 1,
                        "reason": None, "detail": None}

            def invalidate(self):
                pass

        sink = LauncherRadioSink(_Client(), channel_name_of=lambda c: c.value,
                                 station_id_by_endpoint=_STATIONS)
        children = self._children(samples, cf32le=expected)
        # 재계산하면 즉시 실패한다 — "쓰지 않는다"를 구조가 아니라 실행으로 고정한다.
        with patch("vdeter.poc.shared_medium.iq.to_cf32le",
                   side_effect=AssertionError("임계 구간에서 cf32le를 다시 계산했다")):
            sink.commit_group(sink.prepare_group(group_id="p", children=children))
        sent = base64.b64decode(calls[0][1]["samples_base64"])
        self.assertEqual(sent, expected)

    def test_absent_cache_falls_back_to_computing(self):
        # 캐시를 주지 않는 factory 경로도 그대로 동작해야 한다(계약 확장이지 교체가 아니다).
        import base64

        from apps.poc_device_seams_launcher import LauncherRadioSink

        samples = (0.25 - 0.25j,) * 8

        class _Client:
            def __init__(self):
                self.body = None

            def call(self, name, body):
                self.body = body
                return {"accepted": True, "burst_id": "b", "transmit_at_utc_ns": 1,
                        "reason": None, "detail": None}

            def invalidate(self):
                pass

        client = _Client()
        sink = LauncherRadioSink(client, channel_name_of=lambda c: c.value,
                                 station_id_by_endpoint=_STATIONS)
        children = self._children(samples, cf32le=None)
        sink.commit_group(sink.prepare_group(group_id="p", children=children))
        self.assertEqual(base64.b64decode(client.body["samples_base64"]), to_cf32le(samples))


class CachedSerializationIdentityTest(unittest.TestCase):
    """캐시가 재계산과 다르면 장치에 다른 파형이 나간다 — 바이트 동일성을 고정한다."""

    def test_cached_bytes_match_recomputation(self):
        samples = tuple(complex(i / 32.0, -i / 32.0) for i in range(8))
        cached = IqBurst(channel=PhysicalChannel.UPPER, start_sample=0, samples=samples,
                         cf32le_bytes=to_cf32le(samples))
        plain = IqBurst(channel=PhysicalChannel.UPPER, start_sample=0, samples=samples)
        self.assertEqual(cached.cf32le, plain.cf32le)
        self.assertEqual(cached.cf32le, to_cf32le(samples))
        self.assertEqual(cached.sha256, plain.sha256)
        self.assertEqual(cached.sha256, sha256_cf32le(samples))

    def test_mismatched_cache_length_is_refused(self):
        samples = (1 + 0j,) * 4
        with self.assertRaises(IqError):
            IqBurst(channel=PhysicalChannel.UPPER, start_sample=0, samples=samples,
                    cf32le_bytes=to_cf32le(samples)[:-8])

    def test_non_bytes_cache_is_refused(self):
        samples = (1 + 0j,) * 4
        with self.assertRaises(IqError):
            IqBurst(channel=PhysicalChannel.UPPER, start_sample=0, samples=samples,
                    cf32le_bytes="not-bytes")


class ChildrenCarryCacheTest(unittest.TestCase):
    """build_parent가 미리 만든 직렬화를 child까지 나르는가(없으면 sink가 다시 만든다)."""

    def test_children_carry_the_cache_when_the_factory_provides_it(self):
        from vdeter.poc.shared_medium.radiation import AtomicBroadcast

        payload = b"lead-window"

        class _CachingFactory:
            def encode(self, *, source_station, channel, payload):
                burst = ReferenceWaveformFactory().encode(
                    source_station=source_station, channel=channel, payload=payload)
                return IqBurst(channel=burst.channel, start_sample=burst.start_sample,
                               samples=burst.samples,
                               wire_packet_digest=burst.wire_packet_digest,
                               cf32le_bytes=to_cf32le(burst.samples))

        ab = AtomicBroadcast(radio_sink=TD.CountingRadioSink(),
                             rx_readiness=TD.ReadyReadiness(),
                             waveform_factory=_CachingFactory())
        parent = ab.build_parent(source_station="MASS-B", channel=PhysicalChannel.UPPER,
                                 payload=payload, scheduled_sample=0,
                                 scheduled_hw_timestamp_ns=1000, slot_key="slot-1",
                                 addressed_destination="MASS-A")
        for child in parent.children:
            self.assertIsNotNone(child.samples_cf32le)
            self.assertEqual(child.samples_cf32le, to_cf32le(parent.waveform.samples))

    def test_children_have_no_cache_when_the_factory_omits_it(self):
        from vdeter.poc.shared_medium.radiation import AtomicBroadcast

        ab = AtomicBroadcast(radio_sink=TD.CountingRadioSink(),
                             rx_readiness=TD.ReadyReadiness(),
                             waveform_factory=ReferenceWaveformFactory())
        parent = ab.build_parent(source_station="MASS-B", channel=PhysicalChannel.UPPER,
                                 payload=b"no-cache", scheduled_sample=0,
                                 scheduled_hw_timestamp_ns=1000, slot_key="slot-1",
                                 addressed_destination="MASS-A")
        for child in parent.children:
            self.assertIsNone(child.samples_cf32le)


if __name__ == "__main__":
    unittest.main()
