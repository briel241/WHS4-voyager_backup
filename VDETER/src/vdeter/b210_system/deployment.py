"""Stage 7 실장비 배치 profile 조립 (`endpoint_launcher` production 경로).

세 endpoint(MASS-A·ROC·MASS-B)는 **같은** `MacDeploymentProfile`을 독립 계산해야 group이 시작된다
(`vdeter.mac.deployment`의 deployment fingerprint 대조). 이 module이 그 단일 production 구현이다.

이전에는 launcher가 시험 fixture module(`tests.support.stage4`)의 `tbb_fixture`·`deployment_profile`을
직접 import해 배치 값을 얻었다(STR-07). 값과 계산은 그대로 옮겼다:

- 구성원 station ID(111111111·222222222·333333333; `configs/b210_system/endpoints/*.toml`의 `station_id`와
  같다), ROC station ID, TBB control station ID 7, 배치 service area, Stage 4 canonical fixture ID.
- TBB 유효창: 실 UTC **자정**(2000-epoch 초; 세 endpoint가 같은 UTC 날에 독립 계산해도 같은 값) 시작,
  48 h(2,880분) 유효. 시험 fixture 기본값(2000-epoch 800,000,000 s ≈ 2025-05, 60분)은 실 UTC에서
  만료 상태라 CONTROL start가 MAC_RESOURCE_UNAVAILABLE("활성 TBB 유효기간")로 거부된다
  (rr-p2-17 run k 실측).

시험 support(`VDETER/tests/support/stage4.py`)는 여기의 조립기에 결정적 시험 기본값(고정 유효창·시험
fingerprint)만 주입하는 얇은 wrapper다. 이 module은 `tests`·`apps` package를 import하지 않는다
(production 경로의 시험 package 의존 0).
"""

from __future__ import annotations

from ..common.contracts.mac_execution import ServiceAreaMode
from ..common.contracts.message import ControlStationServiceArea
from ..common.identifiers import StationId
from ..mac.deployment import (
    STAGE4_TBB_FIXTURE_ID,
    MacDeploymentProfile,
    Stage4TbbFixtureProfile,
    Type0OverrideProfile,
)
from ..mac.tbb import TBB_START_TIME_EPOCH_SECONDS

#: 승인된 이름 매핑. ROC를 제외한 두 ID 중 작은 값이 MASS-A, 큰 값이 MASS-B다
#: (`MacDeploymentProfile.mass_a_station_id`/`mass_b_station_id`).
MASS_A_STATION_ID = StationId(111111111)
ROC_STATION_ID = StationId(222222222)
MASS_B_STATION_ID = StationId(333333333)
#: `MacDeploymentProfile.member_station_ids`는 `StationId.value` 오름차순 tuple이어야 한다.
MEMBER_STATION_IDS = (MASS_A_STATION_ID, ROC_STATION_ID, MASS_B_STATION_ID)
#: TBB control station ID (0..255).
TBB_CONTROL_STATION_ID = 7

#: 배치 service area(1/10분 단위). `priority`는 완료 profile에서 정확히 0이어야 한다.
SERVICE_AREA = ControlStationServiceArea(
    longitude_1_tenth_minutes=1000,
    latitude_1_tenth_minutes=2000,
    longitude_2_tenth_minutes=3000,
    latitude_2_tenth_minutes=4000,
    priority=0,
)

SECONDS_PER_DAY = 86_400
#: 실 UTC 결속 TBB 유효기간(분) = 48 h. UTC 자정에서 시작하므로 그날 어느 시각에 띄워도 다음 날
#: 끝까지 유효하다.
UTC_DAY_TBB_VALIDITY_MINUTES = 2_880


def build_tbb_fixture(
    *,
    start_time: int,
    validity: int,
    service_area: ControlStationServiceArea = SERVICE_AREA,
) -> Stage4TbbFixtureProfile:
    """Stage 4 canonical fixture ID와 배치 service area로 TBB fixture profile을 만든다.

    `start_time`은 2000-01-01T00:00:00Z 기준 초, `validity`는 분(1..64800). 값 검증은
    `Stage4TbbFixtureProfile`이 한다(자동 계산·endpoint별 기본값 없음).
    """

    return Stage4TbbFixtureProfile(
        fixture_id=STAGE4_TBB_FIXTURE_ID,
        start_time=start_time,
        validity=validity,
        service_area=service_area,
    )


def utc_midnight_start_time(now_epoch_seconds: int) -> int:
    """`now`(Unix epoch 초)가 속한 UTC 날짜의 자정을 2000-epoch 초로 돌려준다.

    2000-01-01T00:00:00Z 자체가 UTC 자정이므로 2000-epoch의 날 경계 = UTC 날 경계다. 세 endpoint가
    같은 UTC 날에 독립 계산하면 같은 값이 나온다. 결과는 `SECONDS_PER_DAY`의 배수다.
    """

    now_2000 = int(now_epoch_seconds) - TBB_START_TIME_EPOCH_SECONDS
    return (now_2000 // SECONDS_PER_DAY) * SECONDS_PER_DAY


def build_utc_day_tbb_fixture(*, now_epoch_seconds: int) -> Stage4TbbFixtureProfile:
    """실 UTC에 결속한 TBB fixture: 오늘 UTC 자정 시작, 48 h 유효(launcher 실장비 경로)."""

    return build_tbb_fixture(
        start_time=utc_midnight_start_time(now_epoch_seconds),
        validity=UTC_DAY_TBB_VALIDITY_MINUTES,
    )


def build_deployment_profile(
    *,
    expected_fingerprint: bytes,
    tbb_fixture: Stage4TbbFixtureProfile,
    type0_override: Type0OverrideProfile | None = None,
    member_station_ids: tuple[StationId, ...] = MEMBER_STATION_IDS,
    roc_station_id: StationId = ROC_STATION_ID,
    tbb_control_station_id: int = TBB_CONTROL_STATION_ID,
    service_area_mode: ServiceAreaMode = ServiceAreaMode.INSIDE_CONFIGURED,
) -> MacDeploymentProfile:
    """세 endpoint 공통 배치 profile을 만든다.

    `expected_fingerprint`는 provisioned TBB certificate fingerprint(정확히 6 bytes; launcher는 trust
    store 인증서에서 계산한다). 값 검증은 `MacDeploymentProfile`이 한다.
    """

    return MacDeploymentProfile(
        service_area_mode=service_area_mode,
        roc_station_id=roc_station_id,
        tbb_control_station_id=tbb_control_station_id,
        member_station_ids=member_station_ids,
        expected_tbb_certificate_fingerprint=expected_fingerprint,
        tbb_fixture=tbb_fixture,
        type0_override=type0_override,
    )


__all__ = (
    "MASS_A_STATION_ID",
    "MASS_B_STATION_ID",
    "MEMBER_STATION_IDS",
    "ROC_STATION_ID",
    "SECONDS_PER_DAY",
    "SERVICE_AREA",
    "TBB_CONTROL_STATION_ID",
    "UTC_DAY_TBB_VALIDITY_MINUTES",
    "build_deployment_profile",
    "build_tbb_fixture",
    "build_utc_day_tbb_fixture",
    "utc_midnight_start_time",
)
