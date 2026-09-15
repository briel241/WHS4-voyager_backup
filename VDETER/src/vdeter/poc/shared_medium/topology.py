"""고정 배선 fan-out·물리 도달·role tuning·자연 관측 행렬 (명세 §3, §5.4, §5.5).

물리 도달(fan-out)과 tuned observation을 분리한다: 물리 신호는 비송신 두 endpoint에 항상
도달하지만, 관측·decode 여부는 관측 시점의 tuned Physical Channel이 결정한다.
"""

from __future__ import annotations

from ..api.types import PhysicalChannel, TARGET_TO_PHYSICAL_CHANNEL
from . import constants


def fanout_cables(sender: str) -> tuple:
    """송신자 → atomic child 두 cable(§3, S8-D005). ROC=C1+C3, MASS-A=C2+C5, MASS-B=C4+C6."""

    if sender not in constants.SENDER_FANOUT_CABLES:
        raise ValueError(f"알 수 없는 송신자: {sender!r}")
    return constants.SENDER_FANOUT_CABLES[sender]


def physical_reach(sender: str) -> tuple:
    """송신자 → fan-out이 물리적으로 도달하는 비송신 endpoint 두 곳(addressed 무관)."""

    if sender not in constants.SENDER_PHYSICAL_REACH:
        raise ValueError(f"알 수 없는 송신자: {sender!r}")
    return constants.SENDER_PHYSICAL_REACH[sender]


def cable_receiver(cable: str) -> str:
    """cable → 물리 수신 endpoint(§3 배선 표)."""

    return constants.CABLE_PATHS[cable][2]


def role_rx_channel(endpoint: str) -> PhysicalChannel:
    """정상 role별 수신 Physical Channel(ROC=lower, MASS-A=upper, MASS-B=upper)."""

    return constants.ROLE_RX_CHANNEL[endpoint]


def receiver_tuned_channels(endpoint: str, *, dual_channel_active: bool = False) -> frozenset:
    """endpoint가 실제 감시 중인 Physical Channel 집합.

    정상 role은 한 channel만 감시한다. MASS-B가 POC_ACTOR로 dual-channel capability를
    협상한 경우에만 lower/upper 동시 감시(공통 LO + per-lane DSP)를 준비한다. 정상 ROC·MASS-A
    tuning은 절대 바꾸지 않는다.
    """

    if endpoint == constants.STATION_MASS_B and dual_channel_active:
        return frozenset({PhysicalChannel.LOWER, PhysicalChannel.UPPER})
    return frozenset({role_rx_channel(endpoint)})


def natural_tuned(sender: str, channel: PhysicalChannel) -> frozenset:
    """(송신자, 선택 channel)의 자연 TUNED 비송신 endpoint 집합(§5.5 행렬 기대값)."""

    return constants.NATURAL_TUNED_MATRIX[(sender, channel)]


def attack_channel(target: str) -> PhysicalChannel:
    """공격 논리 target → canonical Physical Channel(§9.4, POC-D23). ROC=lower, MASS-A=upper."""

    if target not in TARGET_TO_PHYSICAL_CHANNEL:
        raise KeyError(target)
    return TARGET_TO_PHYSICAL_CHANNEL[target]


def expected_delivery(
    sender: str,
    channel: PhysicalChannel,
    *,
    dual_channel_active: bool = False,
) -> dict:
    """송신자·channel의 endpoint별 기대 TUNED/NOT_TUNED 행렬(§5.5).

    물리 도달 두 endpoint 각각에 대해, receiver_tuned_channels에 이 channel이 있으면
    TUNED_OBSERVER, 아니면 NOT_TUNED로 기대한다. dual-channel POC_ACTOR MASS-B는 lower도
    추가 관측한다(협상된 경우만).
    """

    reach = physical_reach(sender)
    result = {}
    for endpoint in reach:
        tuned = channel in receiver_tuned_channels(endpoint, dual_channel_active=dual_channel_active)
        result[endpoint] = "TUNED_OBSERVER" if tuned else "NOT_TUNED"
    return result


__all__ = (
    "fanout_cables",
    "physical_reach",
    "cable_receiver",
    "role_rx_channel",
    "receiver_tuned_channels",
    "natural_tuned",
    "attack_channel",
    "expected_delivery",
)
