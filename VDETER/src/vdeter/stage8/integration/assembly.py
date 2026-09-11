"""production provider 조립(fail-closed).

`SharedMediumActionProvider.for_production`은 scheduler·waveform factory·arbiter·radio_sink·
rx_readiness가 모두 주입되어야 하며 하나라도 없으면 생성 자체가 실패한다(§4: production 경로가
시험용 기본값을 조용히 쓰지 못하게). 이 module은 그 조립을 한 곳에 모은다. RF-off software seam은
이 module이 아니라 `rf_off.py`에서 **명시적으로만** 만들며, 이 production 조립 module은 어떤
`.testing` module도 import하지 않는다.

실장비(Stage 6)에서는 `DeviceSeams`에 실제 Stage 7 seam(group TX worker·MAC scheduler·resource
arbiter·RX readiness·frozen PHY waveform factory)을 넣는다. `waveform_factory`의 실장비 구현은
frozen PHY(`vdeter.phy.transmitter.Link11Phy`)이며 RF-off의 참조 factory가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..shared_medium.provider import SharedMediumActionProvider
from .device_boundary import Stage7GroupTxRadioSink


@dataclass(frozen=True)
class DeviceSeams:
    """provider가 요구하는 device 경계 seam 묶음.

    - `group_tx`: Stage 7 group TX 경계(retune_tx_lo/submit_tx_group). `Stage7GroupTxRadioSink`로
      감싸 RadioBurstSink가 된다.
    - `arbiter`: 정상 TX 우선 자원 arbiter(reserve/release).
    - `scheduler`: MAC scheduler(resolve/register_trigger/clear_triggers).
    - `rx_readiness`: required receiver RX window readiness(window_ready).
    - `waveform_factory`: waveform 생성(encode). 실장비는 frozen PHY, RF-off는 참조 factory.
    """

    group_tx: object
    arbiter: object
    scheduler: object
    rx_readiness: object
    waveform_factory: object


def assemble_provider(seams: DeviceSeams, *, dual_channel_installed: bool = False,
                      packet_resolver=None) -> SharedMediumActionProvider:
    """`DeviceSeams`로 fail-closed production provider를 조립한다.

    device 경계(`group_tx`)를 `Stage7GroupTxRadioSink`로 감싸 RadioBurstSink 계약을 만든 뒤
    `SharedMediumActionProvider.for_production`을 호출한다. for_production은 scheduler·waveform
    factory·arbiter·radio_sink·rx_readiness가 모두 실제 callable seam인지 검증하고 하나라도
    누락이면 ValueError로 거부한다(조용한 RF-off 기본값 금지).
    """

    radio_sink = Stage7GroupTxRadioSink(group_tx=seams.group_tx)
    return SharedMediumActionProvider.for_production(
        radio_sink=radio_sink,
        rx_readiness=seams.rx_readiness,
        scheduler=seams.scheduler,
        waveform_factory=seams.waveform_factory,
        arbiter=seams.arbiter,
        dual_channel_installed=dual_channel_installed,
        packet_resolver=packet_resolver,
    )


__all__ = ("DeviceSeams", "assemble_provider")
