"""phy의 numpy 선택 의존이 계약을 지키는지 고정한다.

pyproject는 numpy를 기본 의존이 아니라 `dsp` 선택 의존으로 두고
"PHY·Radio 구현 단계에서 사용한다"고 선언한다. 이 시험은 그 계약의
세 가지 귀결을 고정한다 — 허용된 두 모듈(`phy/resampling.py`,
`phy/burst/rrc.py`)은:

1. numpy가 **없어도** import·동작·규격 판정이 그대로 통과한다.
2. numpy 경로와 순수 Python 폴백의 수치 차이가 반올림 수준을 넘지 않는다.
3. 같은 device 창을 넣으면 두 경로의 **최종 복호 결과(패킷 바이트,
   CRC 통과 여부)가 동일**하다.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import os
import random
import struct
import unittest
from unittest import mock

from vdeter.phy import resampling
from vdeter.phy.burst import rrc

_HAS_NUMPY = resampling._numpy is not None

#: §13.5 gate가 동결한 decimate impulse 응답의 cf32le SHA-256.
_DECIM_IMPULSE_SHA256 = (
    "6c0d0965ddba3d2dfcd0742615c0e987d98f2fde7ecf8e256e5ca976bc54ac8c"
)

_SRC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
)


def _load_module_without_numpy(alias: str, relative: str):
    """numpy import가 ImportError를 일으키는 환경을 재현해 모듈을 적재한다."""

    path = os.path.join(_SRC_ROOT, "vdeter", "phy", *relative.split("/"))
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("numpy 차단 (폴백 시험)")
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    builtins.__import__ = _blocked
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import
    return module


def _noise(seed: int, count: int, scale: float = 1e-3):
    rng = random.Random(seed)
    return tuple(
        complex(rng.gauss(0.0, scale), rng.gauss(0.0, scale))
        for _ in range(count)
    )


class ImportAndSpecDecisionWithoutNumpy(unittest.TestCase):
    """조건 1: numpy 부재 환경에서 import·동작·규격 판정."""

    def test_resampling_imports_and_passes_spec_decision(self):
        module = _load_module_without_numpy("resampling_nonumpy", "resampling.py")
        self.assertIsNone(module._numpy)

        impulse = [0j] * module.DEVICE_SLOT_SAMPLES
        impulse[4096] = 1 + 0j
        produced = module.decimate_slot(tuple(impulse))
        self.assertEqual(module.NATIVE_SLOT_SAMPLES, len(produced))
        packed = b"".join(
            struct.pack("<2f", value.real, value.imag) for value in produced
        )
        self.assertEqual(
            _DECIM_IMPULSE_SHA256,
            hashlib.sha256(packed).hexdigest(),
            "numpy 없는 폴백이 §13.5 동결 impulse 판정을 통과하지 못한다",
        )

    def test_rrc_imports_and_matches_installed_reference(self):
        module = _load_module_without_numpy("rrc_nonumpy", "burst/rrc.py")
        self.assertIsNone(module._numpy)
        self.assertEqual(rrc.RRC_TAP_BINARY64_BITS, module.RRC_TAP_BINARY64_BITS)

        window = _noise(5, 256)
        self.assertEqual(
            rrc._matched_filter_reference(window),
            module.matched_filter(window),
            "numpy 없는 matched_filter가 참조 구현과 다르다",
        )


class MatchedFilterEquivalence(unittest.TestCase):
    """조건 2 (rrc): 벡터 경로 수치 오차 상한. decimate 쪽 상한은
    `test_resampling_vectorized.py`가 이미 고정한다."""

    @unittest.skipUnless(_HAS_NUMPY, "numpy 없음 — 벡터 경로가 존재하지 않는다")
    def test_vector_path_stays_at_rounding_level(self):
        for seed in (2, 9, 31):
            window = _noise(seed, 2048, scale=0.05)
            reference = rrc._matched_filter_reference(window)
            produced = rrc.matched_filter(window)
            scale = max(abs(value) for value in reference)
            worst = max(
                abs(want - got) for want, got in zip(reference, produced)
            )
            self.assertLessEqual(
                worst, max(scale, 1e-30) * 1e-12,
                f"seed={seed}: 최대 오차 {worst:.3e}")


class DecodeVerdictEquivalence(unittest.TestCase):
    """조건 3: 같은 device 창 → 두 경로의 최종 복호·CRC 판정 동치.

    수신기는 CRC를 통과한 패킷만 돌려주므로, 두 경로 모두 RECOVERED로
    같은 canonical 패킷 바이트를 복원하면 CRC 판정까지 동치다.
    """

    def _decode_device_window(self):
        from tests.support.phy11 import (
            FIRST_SAMPLE_AT,
            canonical_packet,
            operational_phy,
        )
        from vdeter.common.contracts.phy import (
            PhyDecodeRequest,
            PhyEncodeRequest,
            PhyRecoveryStatus,
        )
        from vdeter.common.contracts.radio import (
            SAMPLE_FORMAT_CF32LE,
            SampleBlock,
            SampleContinuity,
        )
        from vdeter.common.identifiers import (
            DeterministicIdSource,
            LinkId,
            PhysicalChannel,
        )
        from vdeter.common.time import ClockDomain, TimePoint
        from vdeter.phy.burst.rrc import from_test_iq_int16_le
        from vdeter.phy.multilink import MultiRatePhy
        from vdeter.profiles.link_phy import build_combined_phy_profile_registry
        from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge

        class _StaticTime:
            _utc = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "np-eq-utc")
            _mono = TimePoint(10, 0, ClockDomain.MONOTONIC, "np-eq-monotonic")

            def now_utc(self):
                return self._utc

            def now_monotonic(self):
                return self._mono

        packet = canonical_packet()
        native_phy = operational_phy()
        encoded = native_phy.encode_packet(PhyEncodeRequest(
            packet_id=DeterministicIdSource(0xEE).new_packet_id(),
            link_id=LinkId(11),
            channel=PhysicalChannel("VDETER_UPPER_100_TEST"),
            transmit_at=FIRST_SAMPLE_AT,
            packet=packet,
        ))
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        native = from_test_iq_int16_le(encoded.value.samples.data)
        device = resampling.interpolate_slot(native)
        payload = b"".join(
            struct.pack("<2f", value.real, value.imag) for value in device
        )

        registry = build_combined_phy_profile_registry().value
        self.assertTrue(registry.freeze().is_ok)
        phy = MultiRatePhy(registry, _StaticTime(), DeterministicIdSource(0xBE))
        bridge = RuntimePhyRadioBridge(phy)
        prepared = bridge.to_native_decode_request(PhyDecodeRequest(
            channel=PhysicalChannel("VDETER_UPPER_100_TEST"),
            samples=SampleBlock(
                sample_format=SAMPLE_FORMAT_CF32LE,
                sample_rate_hz=307_200,
                sample_count=len(device),
                data=payload,
                first_sample_at=_StaticTime._utc,
                continuity=SampleContinuity.CONTIGUOUS,
            ),
            allowed_link_ids=(LinkId(11),),
        ))
        self.assertTrue(prepared.is_ok, getattr(prepared, "error", None))
        decoded = phy.decode_samples(prepared.value)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertIs(PhyRecoveryStatus.RECOVERED, decoded.value.status)
        self.assertEqual(1, len(decoded.value.packets))
        return packet, decoded.value.packets[0].packet

    @unittest.skipUnless(_HAS_NUMPY, "numpy 없음 — 비교할 벡터 경로가 없다")
    def test_both_paths_recover_the_same_packet_bytes(self):
        sent, via_numpy = self._decode_device_window()
        self.assertEqual(sent, via_numpy)

        with mock.patch.object(resampling, "_numpy", None), \
                mock.patch.object(rrc, "_numpy", None):
            sent_fallback, via_fallback = self._decode_device_window()
        self.assertEqual(sent, sent_fallback)
        self.assertEqual(
            via_numpy, via_fallback,
            "numpy 경로와 폴백 경로의 복호 결과가 다르다")


if __name__ == "__main__":
    unittest.main()
