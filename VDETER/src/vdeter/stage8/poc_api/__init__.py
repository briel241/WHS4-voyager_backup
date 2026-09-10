"""Stage 8 PoC 연결 API의 **public** surface.

Stage 4·5와 외부 PoC SDK는 이 package만 import한다. 이 package와 하위 module은
production private 구현(`vdeter.stage8._supervisor`)을 import하지 않는다(import 경계).
이 규칙은 `test_stage8_import_boundary`가 정적·동적으로 검증한다.
"""

from __future__ import annotations

from . import codec, errors, limits, messages, negotiation, protocol, seams, transport, types
from .codec import (
    CodecError,
    canonical_sha256,
    decode_bytes_field,
    decode_wire_packet,
    encode_bytes_field,
    encode_canonical,
    encode_wire_packet,
)
from .errors import ERROR_SEVERITY, PocError, PocErrorCode, Severity, escalate, severity_of
from .messages import ParsedMessage, build_message, new_message_id, parse_envelope
from .negotiation import HelloRequest, ServerCapabilityConfig, negotiate, restore_matches
from .sdk import ClientState, PocCallbacks, PocContext, SdkEngine
from .seams import (
    ActionSubmitProvider,
    ChildHandle,
    ChildSpawner,
    MonotonicClock,
    ObservationProvider,
)
from .transport import OversizedPacket, SeqPacketEndpoint, connect, connected_pair
from .types import (
    ActionDescriptor,
    ActionKind,
    ActionResult,
    ActionTerminal,
    AdmissionResult,
    Event,
    FORBIDDEN_ACTION_FIELDS,
    Negotiation,
    ObservationRecord,
    PhysicalChannel,
    PocManifestDescriptor,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
    TuningState,
)

__all__ = (
    "protocol",
    "limits",
    "errors",
    "codec",
    "types",
    "seams",
    "messages",
    "negotiation",
    "transport",
    # transport
    "OversizedPacket",
    "SeqPacketEndpoint",
    "connect",
    "connected_pair",
    # codec
    "CodecError",
    "decode_wire_packet",
    "encode_wire_packet",
    "encode_canonical",
    "canonical_sha256",
    "encode_bytes_field",
    "decode_bytes_field",
    # errors
    "PocError",
    "PocErrorCode",
    "Severity",
    "ERROR_SEVERITY",
    "severity_of",
    "escalate",
    # messages
    "ParsedMessage",
    "build_message",
    "parse_envelope",
    "new_message_id",
    # negotiation
    "ServerCapabilityConfig",
    "HelloRequest",
    "negotiate",
    "restore_matches",
    # sdk
    "SdkEngine",
    "PocContext",
    "PocCallbacks",
    "ClientState",
    # seams
    "MonotonicClock",
    "ActionSubmitProvider",
    "ObservationProvider",
    "ChildHandle",
    "ChildSpawner",
    # types
    "PhysicalChannel",
    "TuningState",
    "TARGET_TO_PHYSICAL_CHANNEL",
    "ActionKind",
    "FORBIDDEN_ACTION_FIELDS",
    "AdmissionResult",
    "Negotiation",
    "PocManifestDescriptor",
    "Event",
    "ActionDescriptor",
    "ActionResult",
    "ActionTerminal",
    "ProviderSubmission",
    "ObservationRecord",
)
