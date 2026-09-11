"""RF-off 통합용 **명시적** software device seam 조립(production 아님).

production 조립(`assembly.assemble_provider`)은 실 Stage 7 seam이 모두 주입되어야 하며 시험용
기본값을 조용히 쓰지 않는다. 이 module은 RF-off 통합·시험에서 **명시적으로** software harness를
구성하는 유일한 경로다(이 함수를 호출해야만 software 대체가 일어난다). production 코드
(`assembly`·`entry`·`device_boundary`·`tx_executor`·`supervisor_connection`·`runtime`)는 이 module이나
어떤 `.testing` module도 import하지 않는다.

RF-off harness는 실 UHD/B210을 열지 않는다: group TX는 `SoftwareGroupTx`(순서·단일 timestamp·
exactly-once 강제), arbiter/RX readiness는 shared_medium 소유 software double, scheduler는 stub
MAC scheduler(`NextSafeSlotScheduler`), waveform은 참조 factory(`ReferenceWaveformFactory`, frozen
PHY 아님)다. 실장비(Stage 6)에서는 이 함수를 쓰지 않고 실 Stage 7 seam으로 `DeviceSeams`를 만든다.
"""

from __future__ import annotations

from ..shared_medium.provider import NextSafeSlotScheduler
from ..shared_medium.radiation import ReferenceWaveformFactory
from ..shared_medium.testing import FakeArbiter, ReadyReadiness, SelectiveReadiness
from .assembly import DeviceSeams, assemble_provider
from .device_boundary import SoftwareGroupTx


def rf_off_seams(*, closed_slots=(), normal_reserved=(), not_ready_endpoints=(),
                 fail_on_retune: bool = False, fail_on_submit: bool = False) -> DeviceSeams:
    """RF-off 통합용 명시적 software device seam 묶음.

    - `closed_slots`: scheduler가 닫힌 slot으로 볼 slot(POC_DEADLINE_MISSED 시험).
    - `normal_reserved`: arbiter가 정상 TX 점유로 볼 resource_key(RADIO_RESOURCE_CONFLICT 시험).
    - `not_ready_endpoints`: RX window 미준비 endpoint(BroadcastNotReady 시험).
    - `fail_on_retune`/`fail_on_submit`: group TX staging/commit 실패 주입.
    """

    rx_readiness = (SelectiveReadiness(not_ready_endpoints=not_ready_endpoints)
                    if not_ready_endpoints else ReadyReadiness())
    return DeviceSeams(
        group_tx=SoftwareGroupTx(fail_on_retune=fail_on_retune, fail_on_submit=fail_on_submit),
        arbiter=FakeArbiter(normal_reserved=normal_reserved),
        scheduler=NextSafeSlotScheduler(closed_slots=closed_slots),
        rx_readiness=rx_readiness,
        waveform_factory=ReferenceWaveformFactory(),
    )


def rf_off_terminal_source():
    """RF-off 시험 경계에서 **명시적으로** 공급하는 child terminal source.

    software group TX가 accepted action의 두 fan-out child를 방사 완료했다고 보아 COMPLETED를 돌려준다.
    실장비(Stage 6)에서는 실 RF terminal 콜백이 이 자리를 대신한다. 이것은 시험용 장비 double의 명시적
    terminal 공급이며 실 RF 성공 증명이 아니다(production 기본값으로 존재하지 않는다 — 미주입 시 executor는
    TX_UNKNOWN으로 남긴다).
    """

    from ..shared_medium.radiation import ChildTerminal

    def source(action, result, parent):
        return {child.child_id: ChildTerminal.COMPLETED.value for child in parent.children}

    return source


def assemble_rf_off_provider(*, dual_channel_installed: bool = False, packet_resolver=None,
                             **seam_kwargs) -> tuple:
    """RF-off provider와 그 device seam을 함께 돌려준다(시험·entry 편의).

    반환: ``(provider, seams)``. seams.group_tx로 실제 방사(submit) 기록을 검사할 수 있다.
    """

    seams = rf_off_seams(**seam_kwargs)
    provider = assemble_provider(seams, dual_channel_installed=dual_channel_installed,
                                 packet_resolver=packet_resolver)
    return provider, seams


__all__ = ("rf_off_seams", "rf_off_terminal_source", "assemble_rf_off_provider")
