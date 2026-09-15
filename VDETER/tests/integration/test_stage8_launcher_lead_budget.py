"""권위 lead 재확인 **이후** 구간이 여유를 먹는지 본다(D6 회귀).

`LauncherProviderTest`는 `time_acceleration = 1`로 고정돼 있다. 그 파일 주석이 이유를 밝힌다 —
모의 시계가 빠르면 slot 선택과 실제 `schedule_tx` 제출 사이의 파이프라인(파형 생성·직렬화·
readiness·RPC 왕복)이 lead를 소진해 `RADIO_LATE_REQUEST`가 되기 때문이다. 같은 주석이 대조
실측(배속 1 → late 0, 배속 4 → late 4, 벽시계는 사실상 동일)까지 기록해 두었다.

즉 기존 하네스는 그 구간의 **비용을 줄인 게 아니라 시계를 늦춰 우회**했다. 이 시험은 그
우회를 쓰지 않는다: 하네스 기본 배속 4를 **그대로 둔 채** 같은 provider 경로를 돌린다.

경계를 분명히 한다. 배속 4는 실시간 40 ms마다 모의 시계를 160 ms 점프시키므로 마지막 lead
확인 이후의 작업 비용을 **4배로 확대**한다. 따라서 이것은 그 구간 비용에 대한 **민감도
시험이지 하드웨어 판정이 아니다**:

- 통과해도 실물에서 lead가 충분하다는 뜻이 **아니다**(실 장치 시계는 1배다).
- 실패하면 그 구간에 여전히 비용이 남아 있다는 뜻이다.

기존 시험의 `time_acceleration = 1`은 건드리지 않는다 — 통과를 만들려고 knob을 느슨하게
바꾸지 않으며, 권위 lead·timeout·retry 값도 바꾸지 않는다.
"""

import sys
import unittest

from tests.integration import test_stage8_launcher_poc_device as _fixture


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class LeadBudgetUnderFastClockTest(unittest.TestCase):
    """마지막 lead 확인 이후 구간의 비용을 확대한 조건에서 제출이 성립하는가."""

    #: 하네스 기본값. 느리게 만들지 않는다(이 시험의 요점이 그것이다).
    time_acceleration = 4

    setUp = _fixture.LauncherProviderTest.setUp
    _stop_child = _fixture.LauncherProviderTest._stop_child
    _call = _fixture.LauncherProviderTest._call
    _provider = _fixture.LauncherProviderTest._provider

    test_submit_keeps_lead_when_post_check_work_is_magnified = (
        _fixture.LauncherProviderTest
        .test_provider_submit_radiates_through_the_launcher_and_confirms_from_the_ledger)
