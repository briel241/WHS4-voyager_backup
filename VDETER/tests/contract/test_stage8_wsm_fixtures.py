"""Stage 5 shared-medium: byte-pinned IQ golden-vector 계약시험 (명세 §11.1, WSM-G3).

RF-off 요구 매핑: byte-pinned IQ fixture + SHA-256 manifest, positive/negative control,
5-hard-gate(단일 burst 합성 전후 동일성), 15(frozen decoder 결과), 14(payload 임의 합성 0).
fixture는 문서화된 참조 modem에서 결정적으로 재생성되며 코드가 같은 vector를 내는지 대조한다.
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
        comp = IQ.synthesize_same_channel([a, b])
        self.assertEqual(comp.cf32le, _load("composite_capture_lower.iq"))

    def test_collision_composite_matches_pinned_bytes(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P3)
        comp = IQ.synthesize_same_channel([a, b])
        self.assertEqual(comp.cf32le, _load("composite_collision_lower.iq"))

    def test_partial_composite_matches_pinned_bytes(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 40, P3)
        comp = IQ.synthesize_same_channel([a, b])
        self.assertEqual(comp.cf32le, _load("composite_partial_lower.iq"))


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
        comp = IQ.synthesize_same_channel([burst])
        self.assertEqual(comp.cf32le, burst.cf32le)
        self.assertEqual(D.decode(comp.samples).payload, P1)


if __name__ == "__main__":
    unittest.main()
