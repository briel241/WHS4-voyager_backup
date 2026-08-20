"""TBB canonical transmission fragmentation and Link ID 11 Packet construction."""

from __future__ import annotations

from ...common.contracts.link import EncodedPacket, TbbEncodeRequest, TbbEncodedPacketSet
from ...common.contracts.message import (
    TbbContinuationFragmentBody,
    TbbEndFragmentBody,
    TbbStartFragmentBody,
)
from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import IdSource, LinkId
from ...common.results import Err, InternalResult, Ok, internal_error
from ..packet.builder import build_packet
from .crypto import (
    TbbSigner,
    build_tbb_signing_input,
    certificate_fingerprint,
    get_signer_certificate,
    sha256_digest,
    sign_tbb_digest,
)
from .wire import encode_bulletin_board_payload

TBB_LINK_ID = LinkId(11)
TBB_FIRST_CAPACITY = 38
TBB_LATER_CAPACITY = 39
TBB_MAX_PAYLOAD = 233


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


def fragment_tbb_payload(
    source_id,
    control_station_id: int,
    service_capability: int,
    bulletin_board_version: int,
    payload: bytes,
) -> InternalResult[tuple[object, ...]]:
    """Apply the approved minimum-count, front-filled 38/39 byte partition."""

    if not isinstance(payload, bytes) or not 1 <= len(payload) <= TBB_MAX_PAYLOAD:
        return _error(
            ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED,
            ErrorCategory.UNSUPPORTED,
            "logical TBB payload must contain 1..233 bytes",
            logical_payload_bytes=len(payload) if isinstance(payload, bytes) else -1,
        )
    if len(payload) <= TBB_FIRST_CAPACITY:
        return Ok(
            (
                TbbStartFragmentBody(
                    source_id,
                    control_station_id,
                    service_capability,
                    bulletin_board_version,
                    1,
                    payload,
                ),
            )
        )
    chunks = [payload[:TBB_FIRST_CAPACITY]]
    remaining = payload[TBB_FIRST_CAPACITY:]
    while remaining:
        chunks.append(remaining[:TBB_LATER_CAPACITY])
        remaining = remaining[TBB_LATER_CAPACITY:]
    count = len(chunks)
    bodies = [
        TbbStartFragmentBody(
            source_id,
            control_station_id,
            service_capability,
            bulletin_board_version,
            count,
            chunks[0],
        )
    ]
    for index, chunk in enumerate(chunks[1:], start=1):
        body_type = TbbEndFragmentBody if index == count - 1 else TbbContinuationFragmentBody
        bodies.append(
            body_type(
                source_id,
                control_station_id,
                bulletin_board_version,
                index,
                chunk,
            )
        )
    return Ok(tuple(bodies))


def encode_tbb(
    request: TbbEncodeRequest,
    signer: TbbSigner | None,
    id_source: IdSource,
    message_registry,
    link_profile_registry,
) -> InternalResult[TbbEncodedPacketSet]:
    if not isinstance(request, TbbEncodeRequest):
        return _error(
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "TbbEncodeRequest is required",
        )
    certificate = get_signer_certificate(signer)
    if certificate.is_err:
        return certificate
    fingerprint = certificate_fingerprint(certificate.value)
    if fingerprint.is_err:
        return fingerprint
    signing_input = build_tbb_signing_input(
        request.source_id,
        request.control_station_id,
        request.service_capability,
        request.bulletin_board_version,
        request.bulletin_board,
        fingerprint.value,
    )
    if signing_input.is_err:
        return signing_input
    digest = sha256_digest(signing_input.value)
    signed = sign_tbb_digest(signer, digest)
    if signed.is_err:
        return signed
    if signed.value[0] != certificate.value:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            ErrorCategory.INTERNAL,
            "signer certificate changed during one encode operation",
        )
    payload = encode_bulletin_board_payload(
        request.bulletin_board, fingerprint.value, signed.value[1]
    )
    if payload.is_err:
        return payload
    fragmented = fragment_tbb_payload(
        request.source_id,
        request.control_station_id,
        request.service_capability,
        request.bulletin_board_version,
        payload.value,
    )
    if fragmented.is_err:
        return fragmented

    packets = []
    for body in fragmented.value:
        built = build_packet(
            TBB_LINK_ID,
            (body,),
            message_registry,
            link_profile_registry,
        )
        if built.is_err:
            return Err(built.error.with_correlation(request.correlation))
        packets.append(
            EncodedPacket(
                packet_id=id_source.new_packet_id(),
                link_id=TBB_LINK_ID,
                packet=built.value.packet,
                message_types=built.value.message_types,
                padding_size=built.value.padding_size,
                crc=built.value.crc,
            )
        )
    return Ok(
        TbbEncodedPacketSet(
            packets=tuple(packets),
            certificate_fingerprint=fingerprint.value,
            authentication_sequence=signed.value[1],
        )
    )


__all__ = (
    "TBB_FIRST_CAPACITY",
    "TBB_LATER_CAPACITY",
    "TBB_LINK_ID",
    "TBB_MAX_PAYLOAD",
    "encode_tbb",
    "fragment_tbb_payload",
)
