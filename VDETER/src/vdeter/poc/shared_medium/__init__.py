"""Stage 5 유선 공유매체 방사 (`stage8_wired_shared_medium_broadcast_spec.md` VERSION 1.3).

이 package는 Stage 3 public API(`vdeter.poc.api`)와 자기 seam만 사용하고 Stage 4 PoC
실행 구현을 import하지 않는다. common-LO/per-lane-DSP production 경계, 물리 fan-out, tuned
observation, IQ collision 모델을 담는다.

기능 OFF 구조적 0: 기존 Stage 7 runtime은 이 package를 import하지 않으며, import 자체로는
thread·socket·disk write·직렬화가 생기지 않는다. 실제 RF 송신·UHD·qualification은 Stage 6
이후로 이관된다(WSM-G4/G5).
"""

from __future__ import annotations

from . import (
    constants,
    decoder,
    evidence,
    iq,
    observation,
    provider,
    radiation,
    seams,
    topology,
    tuning,
)
from .decoder import DecodeResult, DecodeStatus, build_burst, decode, encode_payload
from .iq import WindowRender, IqBurst, group_collisions, render_noncolliding
from .observation import DeliveryRow, SceneResult, observe_scene
from .provider import NextSafeSlotScheduler, SharedMediumActionProvider
from .radiation import (
    AtomicBroadcast,
    BroadcastAtomicityError,
    BroadcastCapacityError,
    BroadcastError,
    BroadcastNotAdmitted,
    BroadcastNotReady,
    BroadcastOutcomeUnknown,
    ChildPath,
    ChildTerminal,
    ParentOutcome,
    ParentTransmission,
    ScheduledSlot,
)
from .tuning import CommonLoTuningPlan, derive_dual_channel_plan, resolve_dual_channel

__all__ = (
    "constants",
    "iq",
    "decoder",
    "tuning",
    "topology",
    "seams",
    "radiation",
    "observation",
    "provider",
    "evidence",
    "IqBurst",
    "WindowRender",
    "group_collisions",
    "render_noncolliding",
    "DecodeStatus",
    "DecodeResult",
    "decode",
    "encode_payload",
    "build_burst",
    "AtomicBroadcast",
    "ParentTransmission",
    "ChildPath",
    "ChildTerminal",
    "ParentOutcome",
    "ScheduledSlot",
    "BroadcastError",
    "BroadcastNotReady",
    "BroadcastNotAdmitted",
    "BroadcastOutcomeUnknown",
    "BroadcastAtomicityError",
    "BroadcastCapacityError",
    "SharedMediumActionProvider",
    "NextSafeSlotScheduler",
    "SceneResult",
    "DeliveryRow",
    "observe_scene",
    "CommonLoTuningPlan",
    "derive_dual_channel_plan",
    "resolve_dual_channel",
)
