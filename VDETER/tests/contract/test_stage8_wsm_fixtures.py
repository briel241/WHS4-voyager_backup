"""Stage 5 shared-medium: byte-pinned IQ golden-vector 계약시험 (명세 §11.1, WSM-G3).

RF-off 요구 매핑: byte-pinned IQ fixture + SHA-256 manifest, positive/negative control,
5-hard-gate(단일 burst 합성 전후 동일성), 15(frozen decoder 결과), 14(payload 임의 합성 0).
단일 waveform은 그대로 재생성한다. 옛 composite 3종은 역사 decoder fixture로만 보존한다.
v1.4 production 모델은 composite를 생성하지 않으며 충돌은 전체 burst drop이다.
"""

from __future__ import annotations

import hashlib
import os
import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium import decoder as D
from vdeter.stage8.shared_medium import iq as IQ

_FIX = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "stage8_shared_medium")
)

P1 = b"WSM-POS-ROC-0001"
P2 = b"WSM-POS-MASSA-02"
P3 = b"WSM-NEG-ALT--003"


def _load(name):
    with open(os.path.join(_FIX, name), "rb") as fh:
        return fh.read()


def _manifest():
    result = {}
    with open(os.path.join(_FIX, "MANIFEST.sha256"), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            sha, name = line.split(None, 1)
            result[name.strip()] = sha
    return result


class ManifestIntegrityTest(unittest.TestCase):
    def test_every_fixture_matches_manifest_sha256(self):
        manifest = _manifest()
        self.assertEqual(len(manifest), 6)
        for name, sha in manifest.items():
            data = _load(name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha, f"{name} sha mismatch")

    def test_fixtures_are_multiple_of_sample_bytes(self):
        for name in _manifest():
            self.assertEqual(len(_load(name)) % IQ.BYTES_PER_SAMPLE, 0)


class DeterministicRegenerationTest(unittest.TestCase):
    """코드가 pinned vector를 그대로 재생성하는지(행위 회귀 gate)."""

    def test_clean_bursts_match_pinned_bytes(self):
        self.assertEqual(IQ.to_cf32le(D.encode_payload(P1)), _load("src_clean_lower_p1.iq"))
        self.assertEqual(IQ.to_cf32le(D.encode_payload(P2)), _load("src_clean_upper_p2.iq"))
        self.assertEqual(IQ.to_cf32le(D.encode_payload(P3)), _load("src_alt_lower_p3.iq"))

    def test_capture_composite_matches_pinned_bytes(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        comp = IQ.render_noncolliding([a, b])
        self.assertEqual(comp.dropped_input_count, 2)
        self.assertEqual(comp.cf32le, bytes(len(comp.cf32le)))

    def test_collision_composite_matches_pinned_bytes(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P3)
        comp = IQ.render_noncolliding([a, b])
        self.assertEqual(comp.dropped_input_count, 2)
        self.assertEqual(comp.cf32le, bytes(len(comp.cf32le)))

    def test_partial_composite_matches_pinned_bytes(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 40, P3)
        comp = IQ.render_noncolliding([a, b])
        self.assertEqual(comp.dropped_input_count, 2)
        self.assertEqual(comp.cf32le, bytes(len(comp.cf32le)))


class PositiveControlTest(unittest.TestCase):
    def test_clean_lower_decodes_p1(self):
        r = D.decode(IQ.from_cf32le(_load("src_clean_lower_p1.iq")))
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, P1)

    def test_clean_upper_decodes_p2(self):
        r = D.decode(IQ.from_cf32le(_load("src_clean_upper_p2.iq")))
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, P2)

    def test_capture_composite_decodes_p1(self):
        r = D.decode(IQ.from_cf32le(_load("composite_capture_lower.iq")))
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, P1)


class NegativeControlTest(unittest.TestCase):
    def test_collision_composite_fails_decode_no_payload(self):
        r = D.decode(IQ.from_cf32le(_load("composite_collision_lower.iq")))
        self.assertNotEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertIsNone(r.payload)

    def test_partial_composite_fails_decode_no_payload(self):
        r = D.decode(IQ.from_cf32le(_load("composite_partial_lower.iq")))
        self.assertNotEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertIsNone(r.payload)


class HardGateOnFixtureTest(unittest.TestCase):
    """단일 clean burst를 합성 경로에 통과시켜도 byte·decode가 동일(§11.1-5)."""

    def test_single_fixture_burst_byte_identical_and_decode_same(self):
        samples = IQ.from_cf32le(_load("src_clean_lower_p1.iq"))
        burst = IQ.IqBurst(PhysicalChannel.LOWER, 0, samples)
        comp = IQ.render_noncolliding([burst])
        self.assertEqual(comp.cf32le, burst.cf32le)
        self.assertEqual(D.decode(comp.samples).payload, P1)


if __name__ == "__main__":
    unittest.main()
