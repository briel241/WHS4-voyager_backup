"""`vdeter.b210_system.deployment` — launcher 배치 profile의 단일 production 구현 (STR-07, 묶음 1-3).

1-3 이전 launcher(`endpoint_launcher.py:309-334`)는 시험 fixture module `tests.support.stage4`의
`tbb_fixture()`를 `dataclasses.replace(start_time=UTC 자정, validity=2880)`로 고쳐 쓰고
`deployment_profile(expected_fingerprint=…, fixture=…)`를 불렀다. 여기의 대조 구현 `_legacy_launcher_profile`은
그 본문과 fixture 값을 literal로 옮긴 것이고, GOLDEN은 그 이전 조합을 **1-3 편집 전 트리**(1-2 커밋
255efaaf)에서 실행해 얻은 fingerprint다(`output/stage8-refactor-optimize/step1-3-20260915/golden_before_1_3.json`).
"""

from __future__ import annotations

import ast
import datetime
import io
import os
import unittest

from vdeter.common.contracts.mac_execution import ServiceAreaMode
from vdeter.common.contracts.message import ControlStationServiceArea
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import StationId
from vdeter.mac.deployment import (
    STAGE4_TBB_FIXTURE_ID,
    MacDeploymentProfile,
    Stage4TbbFixtureProfile,
    build_canonical_deployment_profile,
)
from vdeter.mac.tbb import (
    TBB_START_TIME_EPOCH_SECONDS,
    build_canonical_tbb_profile,
    build_wideband_canonical_tbb_profile,
)
from vdeter.b210_system import deployment as prod

FP_A = bytes.fromhex("0a1b2c3d4e5f")
FP_B = bytes.fromhex("a1b2c3d4e5f6")
UTC = datetime.timezone.utc
_EPOCH_2000 = datetime.datetime(2000, 1, 1, tzinfo=UTC)

#: (now Unix epoch 초, certificate fingerprint, 기대 start_time(2000-epoch 초),
#:  canonical TBB fingerprint, wideband canonical TBB fingerprint, deployment fingerprint)
#: — 1-3 이전 조합에서 계산한 golden.
GOLDEN = (
    (1789459800, FP_A, 842745600,          # 2026-09-15T08:10:00Z → 2026-09-15 자정
     "aef176b72b90c6032039d548cf36712989e4da3fef0a486a6f833078239871fd",
     "6a0509c24f430c14f064e8b1f81110340a0d92d92829b8c7c7afdad512da7452",
     "0a8d477f8533a2ce3b62311b1e60960f890ef31760d32d38f39b6d196de1b895"),
    (1789430399, FP_A, 842659200,          # 자정 1초 전 → 전날(2026-09-14) 자정
     "ae6faeaf67e7c882f2d429c7b93b556b3be7538e5eea987f86b50ef8b5b489d8",
     "3ac44c0f6e24b1cff74737b57b5b2e2de6dab84625624e5534a5522c38d61e36",
     "5598cfdc14137f54693e3097f5334965ae6c498c31afc6f11da703d726673b9e"),
    (1789516799, FP_B, 842745600,          # 그날 마지막 초 → 같은 날 자정
     "ca19ac70b00feb2aeb1eef40c598cdb950046097d2e10fd4471987a2d91f4d93",
     "1c1463f240e94566afc9cabb9cd6fb7b7abe7789e3844966e6bc1346707954c0",
     "1a11e49e01985ec6d45bc1dadf570c2f14b6ef73a9a1cf7e78a8e551cc6d54d0"),
    (946684801, FP_A, 0,                   # 2000-01-01T00:00:01Z → start_time 0
     "1fd6fae9b2d8d1da1da9363aeeebb16c2c3ee06ff460ebc4ea2563cd0245535e",
     "964db55da1867a2e41cba152c51c3ec3eab5d07ea6ec4b966b6b8d9d76335209",
     "f5e8610decafb396d71c0f1db7a23ef74ed7ef433ea2360231c4e9f58b526dee"),
    (4102444801, FP_B, 3155760000,         # 2100-01-01T00:00:01Z
     "4cd80ec2483c8f0b7923f6c7b95868ded3207af85713b33a476974c856431ba4",
     "b1bafaaab29415f85443f84d72f4b60505b759de4fc0aecd932a3519b91aa9c7",
     "63e6384f002149a8e245688da1fcf18cf6968f9b0a95519fb23d9e70d3e173b4"),
)

#: 시험 fixture 기본값(`tests.support.stage4.deployment_profile()`)의 canonical TBB fingerprint — 1-3 이전 값.
FIXTURE_DEFAULT_TBB_FINGERPRINT = "30278f63fe65f9b361f3b231b2ada10ab8c65f3a1f28c5de5328ea8005901db3"


def _legacy_launcher_profile(now_epoch: int, fingerprint: bytes) -> MacDeploymentProfile:
    """1-3 이전 launcher 본문 + `tests.support.stage4` fixture 값을 literal로 옮긴 대조 구현(변경 금지)."""
    now_2000 = int(now_epoch) - TBB_START_TIME_EPOCH_SECONDS // 1
    midnight_2000 = (now_2000 // 86_400) * 86_400
    fixture = Stage4TbbFixtureProfile(
        fixture_id="STAGE4_25K_TEST_TBB_V1",
        start_time=midnight_2000,
        validity=2_880,
        service_area=ControlStationServiceArea(
            longitude_1_tenth_minutes=1000,
            latitude_1_tenth_minutes=2000,
            longitude_2_tenth_minutes=3000,
            latitude_2_tenth_minutes=4000,
            priority=0,
        ),
    )
    return MacDeploymentProfile(
        service_area_mode=ServiceAreaMode.INSIDE_CONFIGURED,
        roc_station_id=StationId(222222222),
        tbb_control_station_id=7,
        member_station_ids=(StationId(111111111), StationId(222222222), StationId(333333333)),
        expected_tbb_certificate_fingerprint=fingerprint,
        tbb_fixture=fixture,
        type0_override=None,
    )


def _launcher_profile(now_epoch: int, fingerprint: bytes) -> MacDeploymentProfile:
    """launcher `main()`이 지금 하는 조합(`vdeter.b210_system.deployment`만 사용)."""
    fixture = prod.build_utc_day_tbb_fixture(now_epoch_seconds=now_epoch)
    return prod.build_deployment_profile(expected_fingerprint=fingerprint, tbb_fixture=fixture)


class LauncherDeploymentEquivalenceTest(unittest.TestCase):
    """같은 고정 입력에서 새 조립기 == 이전 조합 (field·직렬화·fingerprint)."""

    def test_new_builder_equals_legacy_composition_field_by_field(self):
        for now, fingerprint, start_time, *_ in GOLDEN:
            with self.subTest(now=now):
                new = _launcher_profile(now, fingerprint)
                legacy = _legacy_launcher_profile(now, fingerprint)
                self.assertEqual(new, legacy)                       # frozen dataclass 전체 비교
                self.assertEqual(new.tbb_fixture, legacy.tbb_fixture)
                self.assertEqual(new.tbb_fixture.start_time, start_time)

    def test_serialization_is_byte_identical_to_legacy(self):
        for now, fingerprint, *_ in GOLDEN:
            with self.subTest(now=now):
                new = _launcher_profile(now, fingerprint)
                legacy = _legacy_launcher_profile(now, fingerprint)
                new_tbb, legacy_tbb = build_canonical_tbb_profile(new), build_canonical_tbb_profile(legacy)
                self.assertEqual(new_tbb.canonical_bytes, legacy_tbb.canonical_bytes)
                self.assertEqual(build_wideband_canonical_tbb_profile(new).canonical_bytes,
                                 build_wideband_canonical_tbb_profile(legacy).canonical_bytes)
                self.assertEqual(
                    build_canonical_deployment_profile(new, new_tbb.canonical_tbb_fingerprint).canonical_bytes,
                    build_canonical_deployment_profile(
                        legacy, legacy_tbb.canonical_tbb_fingerprint).canonical_bytes)

    def test_golden_fingerprints_from_pre_change_tree(self):
        """1-3 편집 전 트리에서 실행한 이전 조합의 fingerprint를 그대로 재현한다."""
        for now, fingerprint, start_time, tbb_fp, wide_fp, dep_fp in GOLDEN:
            with self.subTest(now=now):
                profile = _launcher_profile(now, fingerprint)
                tbb = build_canonical_tbb_profile(profile)
                self.assertEqual(profile.tbb_fixture.start_time, start_time)
                self.assertEqual(tbb.canonical_tbb_fingerprint, tbb_fp)
                self.assertEqual(build_wideband_canonical_tbb_profile(profile).canonical_tbb_fingerprint,
                                 wide_fp)
                self.assertEqual(
                    build_canonical_deployment_profile(profile, tbb_fp).deployment_fingerprint, dep_fp)

    def test_deployment_values_preserved(self):
        profile = _launcher_profile(1789459800, FP_A)
        self.assertEqual(profile.tbb_fixture.fixture_id, STAGE4_TBB_FIXTURE_ID)
        self.assertEqual(profile.tbb_fixture.fixture_id, "STAGE4_25K_TEST_TBB_V1")
        self.assertEqual(profile.tbb_fixture.validity, 2_880)
        self.assertEqual(profile.tbb_fixture.service_area,
                         ControlStationServiceArea(1000, 2000, 3000, 4000, 0))
        self.assertIs(profile.service_area_mode, ServiceAreaMode.INSIDE_CONFIGURED)
        self.assertEqual([m.value for m in profile.member_station_ids],
                         [111111111, 222222222, 333333333])
        self.assertEqual(profile.roc_station_id.value, 222222222)
        self.assertEqual(profile.mass_a_station_id.value, 111111111)
        self.assertEqual(profile.mass_b_station_id.value, 333333333)
        self.assertEqual(profile.tbb_control_station_id, 7)
        self.assertIsNone(profile.type0_override)
        self.assertEqual(profile.expected_tbb_certificate_fingerprint, FP_A)


class UtcDayBoundaryTest(unittest.TestCase):
    """start_time = now가 속한 UTC 날짜의 자정(2000-epoch 초); 유효기간 48 h."""

    def _utc(self, epoch_seconds):
        return datetime.datetime.fromtimestamp(epoch_seconds, UTC)

    def test_start_time_is_utc_midnight_of_the_same_utc_date(self):
        for now in (1789459800, 1789430399, 1789516799, 946684801, 4102444801, 1_700_000_000):
            with self.subTest(now=now):
                start = prod.utc_midnight_start_time(now)
                self.assertEqual(start % prod.SECONDS_PER_DAY, 0)
                start_utc = _EPOCH_2000 + datetime.timedelta(seconds=start)
                self.assertEqual((start_utc.hour, start_utc.minute, start_utc.second), (0, 0, 0))
                self.assertEqual(start_utc.date(), self._utc(now).date())

    def test_day_boundary_flips_exactly_at_utc_midnight(self):
        midnight = 1789430400                                  # 2026-09-15T00:00:00Z
        self.assertEqual(self._utc(midnight).time(), datetime.time(0, 0, 0))
        day = prod.SECONDS_PER_DAY
        self.assertEqual(prod.utc_midnight_start_time(midnight - 1),
                         prod.utc_midnight_start_time(midnight) - day)
        self.assertEqual(prod.utc_midnight_start_time(midnight),
                         prod.utc_midnight_start_time(midnight + day - 1))
        self.assertEqual(prod.utc_midnight_start_time(midnight + day),
                         prod.utc_midnight_start_time(midnight) + day)

    def test_validity_window_covers_now_and_the_following_utc_day(self):
        for now in (1789430400, 1789516799):                   # 자정, 그날 마지막 초
            with self.subTest(now=now):
                fixture = prod.build_utc_day_tbb_fixture(now_epoch_seconds=now)
                self.assertEqual(fixture.validity, prod.UTC_DAY_TBB_VALIDITY_MINUTES)
                self.assertEqual(fixture.validity, 2_880)
                valid_from = TBB_START_TIME_EPOCH_SECONDS + fixture.start_time
                valid_until = valid_from + fixture.validity * 60
                self.assertLessEqual(valid_from, now)
                self.assertEqual(valid_until - valid_from, 2 * prod.SECONDS_PER_DAY)
                self.assertGreater(valid_until, now + prod.SECONDS_PER_DAY)  # 다음 날 끝까지

    def test_now_before_2000_is_rejected_not_clamped(self):
        # 이전 조합도 Stage4TbbFixtureProfile 검증(start_time 0..2^32-1)으로 거부했다. 조용한 보정 없음.
        with self.assertRaises(InvalidValueError):
            prod.build_utc_day_tbb_fixture(now_epoch_seconds=TBB_START_TIME_EPOCH_SECONDS - 1)
        self.assertEqual(prod.utc_midnight_start_time(TBB_START_TIME_EPOCH_SECONDS), 0)


class EndpointIndependenceTest(unittest.TestCase):
    """세 endpoint(is_control 무관)가 같은 UTC 날·같은 trust store로 독립 계산하면 같은 profile."""

    def test_three_endpoints_compute_identical_profile_and_fingerprint(self):
        profiles = [_launcher_profile(1789459800 + offset, FP_A)
                    for offset in (0, 7, 3600)]                  # 같은 UTC 날의 다른 시각
        self.assertEqual(profiles[0], profiles[1])
        self.assertEqual(profiles[0], profiles[2])
        fingerprints = {
            build_canonical_deployment_profile(
                p, build_canonical_tbb_profile(p).canonical_tbb_fingerprint).deployment_fingerprint
            for p in profiles}
        self.assertEqual(len(fingerprints), 1)

    def test_certificate_fingerprint_passes_through_and_changes_the_deployment(self):
        a, b = _launcher_profile(1789459800, FP_A), _launcher_profile(1789459800, FP_B)
        self.assertIs(a.expected_tbb_certificate_fingerprint, FP_A)
        self.assertIs(b.expected_tbb_certificate_fingerprint, FP_B)
        self.assertNotEqual(build_canonical_tbb_profile(a).canonical_tbb_fingerprint,
                            build_canonical_tbb_profile(b).canonical_tbb_fingerprint)

    def test_fingerprint_length_is_enforced_by_the_profile(self):
        fixture = prod.build_utc_day_tbb_fixture(now_epoch_seconds=1789459800)
        with self.assertRaises(InvalidValueError):
            prod.build_deployment_profile(expected_fingerprint=b"\x00" * 5, tbb_fixture=fixture)


class TestSupportThinWrapperTest(unittest.TestCase):
    """`tests.support.stage4`는 production 조립기에 결정적 시험 기본값만 주입한다(wall clock 무관)."""

    def test_fixture_defaults_are_unchanged_and_deterministic(self):
        from tests.support import stage4

        self.assertEqual(stage4.FIXTURE_START_TIME, 800_000_000)
        self.assertEqual(stage4.FIXTURE_VALIDITY_MINUTES, 60)
        expected = prod.build_tbb_fixture(start_time=800_000_000, validity=60)
        self.assertEqual(stage4.tbb_fixture(), expected)
        self.assertEqual(stage4.tbb_fixture(), stage4.tbb_fixture())
        default = stage4.deployment_profile()
        self.assertEqual(default, stage4.deployment_profile())
        self.assertEqual(default.expected_tbb_certificate_fingerprint, bytes.fromhex("a1b2c3d4e5f6"))
        self.assertEqual(default.tbb_fixture, expected)
        self.assertIsNone(default.type0_override)
        self.assertEqual(build_canonical_tbb_profile(default).canonical_tbb_fingerprint,
                         FIXTURE_DEFAULT_TBB_FINGERPRINT)

    def test_fixture_reuses_production_deployment_constants(self):
        from tests.support import stage4

        self.assertIs(stage4.MEMBER_STATION_IDS, prod.MEMBER_STATION_IDS)
        self.assertIs(stage4.ROC_STATION_ID, prod.ROC_STATION_ID)
        self.assertIs(stage4.MASS_A_STATION_ID, prod.MASS_A_STATION_ID)
        self.assertIs(stage4.MASS_B_STATION_ID, prod.MASS_B_STATION_ID)
        self.assertIs(stage4.SERVICE_AREA, prod.SERVICE_AREA)
        self.assertEqual(stage4.TBB_CONTROL_STATION_ID, prod.TBB_CONTROL_STATION_ID)
        override = stage4.default_type0_override()
        profile = stage4.deployment_profile(expected_fingerprint=FP_A, type0_override=override)
        self.assertEqual(profile, prod.build_deployment_profile(
            expected_fingerprint=FP_A, tbb_fixture=stage4.tbb_fixture(), type0_override=override))

    def test_fixture_module_does_not_read_the_wall_clock(self):
        from tests.support import stage4

        with io.open(stage4.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=stage4.__file__)
        clock_calls = [
            (node.lineno, node.func.attr) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"time", "time_ns", "now", "utcnow", "monotonic", "monotonic_ns"}
        ]
        self.assertEqual(clock_calls, [], "시험 fixture가 wall clock을 읽는다")
        self.assertEqual(os.path.basename(stage4.__file__), "stage4.py")


if __name__ == "__main__":
    unittest.main()
