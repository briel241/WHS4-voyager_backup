"""version과 capability 협상(명세 §7.2, `POC-API-D04`, `POC-API-D13`).

server가 한 번 협상해 `WELCOME`을 만든다. 협상 결과는 workload 동안 바뀌지 않으며
reconnect는 같은 결과만 복원한다(확대·변경 금지).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import protocol
from .errors import PocError, PocErrorCode
from .types import Negotiation, PocManifestDescriptor


@dataclass(frozen=True, slots=True)
class ServerCapabilityConfig:
    """server가 광고하는 capability/trigger와 minor 범위.

    `wsm_g1_production_installed`가 False(현재 frozen 설치)이면
    `observe_dual_physical_channel`을 광고하지 않는다(§7.2.4, §19).
    """

    wsm_g1_production_installed: bool = False
    min_minor: int = protocol.SERVER_MIN_MINOR
    max_minor: int = protocol.SERVER_MAX_MINOR
    base_capabilities: frozenset = protocol.DEFAULT_SERVER_CAPABILITIES
    triggers: frozenset = protocol.DEFAULT_SERVER_TRIGGERS

    def advertised_capabilities(self) -> frozenset:
        caps = set(self.base_capabilities)
        if self.wsm_g1_production_installed:
            caps.add(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL)
        else:
            caps.discard(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL)
        return frozenset(caps)


@dataclass(frozen=True, slots=True)
class HelloRequest:
    """HELLO body의 협상 관련 부분(§7.1)."""

    protocol_major: int
    minor_min: int
    minor_max: int
    requested_capabilities: frozenset
    requested_triggers: frozenset


def negotiate(
    server: ServerCapabilityConfig,
    manifest: PocManifestDescriptor,
    hello: HelloRequest,
    *,
    connection_epoch: int,
) -> Negotiation:
    """§7.2 규칙으로 협상한다. 실패는 고정 code의 :class:`PocError`.

    1. major 불일치 → `POC_VERSION_INCOMPATIBLE`.
    2. 공통 최고 minor.
    3. capability = manifest allowlist ∩ server 광고 ∩ client 요청.
    4. required capability가 하나라도 승인되지 않으면 `POC_CAPABILITY_UNAVAILABLE`.
    """

    if hello.protocol_major != protocol.PROTOCOL_MAJOR:
        raise PocError(
            PocErrorCode.POC_VERSION_INCOMPATIBLE,
            "protocol major 불일치",
            detail={"client_major": hello.protocol_major, "server_major": protocol.PROTOCOL_MAJOR},
        )

    low = max(server.min_minor, hello.minor_min)
    high = min(server.max_minor, hello.minor_max)
    if low > high:
        raise PocError(
            PocErrorCode.POC_VERSION_INCOMPATIBLE,
            "공통 minor 범위 없음",
            detail={
                "server_range": [server.min_minor, server.max_minor],
                "client_range": [hello.minor_min, hello.minor_max],
            },
        )
    chosen_minor = high

    advertised = server.advertised_capabilities()
    manifest_allow = frozenset(manifest.requested_capabilities) | frozenset(
        manifest.required_capabilities
    )
    approved_caps = (
        manifest_allow & advertised & frozenset(hello.requested_capabilities)
    )
    approved_triggers = frozenset(manifest.requested_triggers) & server.triggers & frozenset(
        hello.requested_triggers
    )

    required = frozenset(manifest.required_capabilities)
    unavailable = required - approved_caps
    if unavailable:
        # silent single-channel fallback 없이 연결·import·RF 전에 fail-closed(§7.2.4, §19).
        raise PocError(
            PocErrorCode.POC_CAPABILITY_UNAVAILABLE,
            "required capability 미지원",
            detail={
                "required": sorted(required),
                "advertised": sorted(advertised),
                "unavailable": sorted(unavailable),
            },
        )

    return Negotiation(
        protocol_minor=chosen_minor,
        capabilities=approved_caps,
        triggers=approved_triggers,
        connection_epoch=connection_epoch,
    )


def restore_matches(previous: Negotiation, current: Negotiation) -> bool:
    """reconnect 복원이 이전 협상과 같은지(확대·변경 금지, §7.2, §12.2)."""

    return (
        previous.protocol_minor == current.protocol_minor
        and previous.capabilities == current.capabilities
        and previous.triggers == current.triggers
    )


__all__ = (
    "ServerCapabilityConfig",
    "HelloRequest",
    "negotiate",
    "restore_matches",
)
