"""공통 RF LO + per-lane DSP production tuning 경계와 capability gate (명세 §5.3, §5.4).

- 값·부호는 G1 readback과 constants에서만 유도한다(재추정 금지). effective = RF LO - DSP.
- 이 구조는 `observe_dual_physical_channel` capability가 필요한 Stage 8 경로에서만 활성화한다
  (POC_ACTOR MASS-B). 정상 ROC·MASS-A role tuning은 이 module이 바꾸지 않는다.
- server는 WSM-G1-호환 production 구현이 설치된 경우에만 capability를 광고한다. required인데
  미지원이면 silent single-channel fallback 없이 RF 전에 fail-closed한다(WSM-REQ-12).

WSM-G1은 하드웨어 구성 가능성만 증명한다(RF-off). production tuning 경로(WSM-G2) 구현이며,
실신호 dual-channel decode(WSM-G4)·qualification(WSM-G5)은 Stage 6 이후로 이관된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..api.errors import PocError, PocErrorCode
from ..api.types import PhysicalChannel
from . import constants

#: dual-channel 관측 capability 이름(명세·API 공통).
DUAL_CHANNEL_CAPABILITY = "observe_dual_physical_channel"

#: fail-closed 사유(shared-medium 계층 명칭, 명세 §5.3.4 BROADCAST_CAPABILITY_UNAVAILABLE).
BROADCAST_CAPABILITY_UNAVAILABLE = "BROADCAST_CAPABILITY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CommonLoTuningPlan:
    """POC_ACTOR MASS-B의 공통 RF LO + per-lane DSP 관측 계획(§5.3).

    모든 값은 G1 evidence/constants에서 유도한다. UHD RX 규약 effective = RF LO - DSP.
    """

    common_rf_lo_hz: int
    lower_dsp_hz: int
    upper_dsp_hz: int
    lower_effective_hz: int
    upper_effective_hz: int
    output_rate_sps: int
    analog_rx_bandwidth_hz: int
    convention: str = constants.RX_EFFECTIVE_CONVENTION

    def effective_center_hz(self, channel: PhysicalChannel) -> int:
        return constants.CHANNEL_EFFECTIVE_CENTER_HZ[channel]

    def dsp_hz(self, channel: PhysicalChannel) -> int:
        return constants.CHANNEL_DSP_HZ[channel]

    def as_evidence(self) -> dict:
        return {
            "common_rf_lo_hz": self.common_rf_lo_hz,
            "lower_dsp_hz": self.lower_dsp_hz,
            "upper_dsp_hz": self.upper_dsp_hz,
            "lower_effective_hz": self.lower_effective_hz,
            "upper_effective_hz": self.upper_effective_hz,
            "output_rate_sps": self.output_rate_sps,
            "analog_rx_bandwidth_hz": self.analog_rx_bandwidth_hz,
            "convention": self.convention,
            "capability": DUAL_CHANNEL_CAPABILITY,
        }


def derive_dual_channel_plan() -> CommonLoTuningPlan:
    """G1 readback·constants에서 dual-channel 관측 계획을 유도한다(재추정 없음)."""

    return CommonLoTuningPlan(
        common_rf_lo_hz=constants.COMMON_RF_LO_HZ,
        lower_dsp_hz=constants.LOWER_DSP_HZ,
        upper_dsp_hz=constants.UPPER_DSP_HZ,
        lower_effective_hz=constants.LOWER_EFFECTIVE_CENTER_HZ,
        upper_effective_hz=constants.UPPER_EFFECTIVE_CENTER_HZ,
        output_rate_sps=constants.OUTPUT_RATE_SPS,
        analog_rx_bandwidth_hz=constants.ANALOG_RX_BANDWIDTH_HZ,
    )


def advertised_capabilities(*, dual_channel_installed: bool) -> frozenset:
    """server가 광고하는 shared-medium capability 집합.

    WSM-G1-호환 production 구현이 설치된 경우에만 `observe_dual_physical_channel`을 광고한다.
    현재 frozen 설치는 미지원이므로 기본값 False → 빈 집합.
    """

    if dual_channel_installed:
        return frozenset({DUAL_CHANNEL_CAPABILITY})
    return frozenset()


def resolve_dual_channel(*, required: bool, dual_channel_installed: bool) -> CommonLoTuningPlan | None:
    """dual-channel 관측 요구를 해소하거나 fail-closed한다(WSM-REQ-12).

    - required=False: dual-channel을 준비하지 않는다(None). 정상 single-channel 관측.
    - required=True & 설치 지원: 공통 LO 계획을 준비해 돌려준다.
    - required=True & 미지원: silent fallback 없이 `POC_CAPABILITY_UNAVAILABLE`로 fail-closed.
    """

    if not required:
        return None
    if not dual_channel_installed:
        raise PocError(
            PocErrorCode.POC_CAPABILITY_UNAVAILABLE,
            "observe_dual_physical_channel: WSM-G1-호환 production 구현 미설치",
            detail={"reason": BROADCAST_CAPABILITY_UNAVAILABLE, "capability": DUAL_CHANNEL_CAPABILITY},
        )
    return derive_dual_channel_plan()


__all__ = (
    "DUAL_CHANNEL_CAPABILITY",
    "BROADCAST_CAPABILITY_UNAVAILABLE",
    "CommonLoTuningPlan",
    "derive_dual_channel_plan",
    "advertised_capabilities",
    "resolve_dual_channel",
)
