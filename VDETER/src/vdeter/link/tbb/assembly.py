"""Bounded TBB fragment candidate assembly, authentication, and explicit lifetime."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...common.contracts.link import (
    TbbAssemblyControlKind,
    TbbAssemblyControlRequest,
    TbbAssemblyControlResult,
)
from ...common.contracts.message import (
    DecodedMessage,
    TbbAssemblyStatus,
    TbbAssemblyUpdate,
    TbbCandidateKey,
    TbbContinuationFragmentBody,
    TbbEndFragmentBody,
    TbbStartFragmentBody,
    VerifiedTbb,
)
from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import PacketId
from ...common.results import InternalResult, Ok, internal_error
from ...common.time import TimePoint
from .crypto import TbbTrustStore, build_tbb_signing_input, sha256_digest, verify_tbb_digest
from .wire import decode_bulletin_board_payload

MAX_TBB_CANDIDATES = 64


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    packet_id: PacketId | None = None,
    **details,
):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        correlation=Correlation(packet_id=packet_id),
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class TbbAssemblyOutcome:
    update: TbbAssemblyUpdate
    verified_tbb: VerifiedTbb | None = None


@dataclass(slots=True)
class _Candidate:
    start: TbbStartFragmentBody
    fragments: dict[int, tuple[object, PacketId]] = field(default_factory=dict)

    @property
    def expected_count(self) -> int:
        return self.start.number_of_fragments

    @property
    def received_bytes(self) -> int:
        return sum(len(body.fragment_payload) for body, _ in self.fragments.values())

    def ordered_packet_ids(self) -> tuple[PacketId, ...]:
        return tuple(self.fragments[index][1] for index in sorted(self.fragments))


def _key(body) -> TbbCandidateKey:
    return TbbCandidateKey(
        body.source_id, body.control_station_id, body.bulletin_board_version
    )


class TbbAssembler:
    __slots__ = ("_trust_store", "_candidates", "_maximum_candidates")

    def __init__(
        self,
        trust_store: TbbTrustStore | None,
        maximum_candidates: int = MAX_TBB_CANDIDATES,
    ) -> None:
        if (
            isinstance(maximum_candidates, bool)
            or not isinstance(maximum_candidates, int)
            or not 1 <= maximum_candidates <= MAX_TBB_CANDIDATES
        ):
            raise ValueError("maximum_candidates must be 1..64")
        self._trust_store = trust_store
        self._maximum_candidates = maximum_candidates
        self._candidates: dict[TbbCandidateKey, _Candidate] = {}

    @property
    def active_count(self) -> int:
        return len(self._candidates)

    def process(
        self,
        packet_id: PacketId,
        received_at: TimePoint,
        message: DecodedMessage,
    ) -> InternalResult[TbbAssemblyOutcome]:
        if (
            not isinstance(packet_id, PacketId)
            or not isinstance(received_at, TimePoint)
            or not isinstance(message, DecodedMessage)
            or message.message_type not in (20, 21, 22)
        ):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "TBB assembly requires Packet metadata and Type 20/21/22",
                packet_id=packet_id if isinstance(packet_id, PacketId) else None,
            )
        body = message.body
        key = _key(body)
        candidate = self._candidates.get(key)

        if isinstance(body, TbbStartFragmentBody):
            if candidate is None:
                if len(self._candidates) >= self._maximum_candidates:
                    return _error(
                        ErrorCode.LINK_TBB_ASSEMBLY_CAPACITY_EXHAUSTED,
                        ErrorCategory.RESOURCE,
                        "TBB candidate capacity is exhausted",
                        packet_id=packet_id,
                        active_candidate_count=len(self._candidates),
                        capacity=self._maximum_candidates,
                    )
                candidate = _Candidate(body)
                candidate.fragments[0] = (body, packet_id)
                self._candidates[key] = candidate
                initial_status = TbbAssemblyStatus.STARTED
            else:
                existing = candidate.fragments.get(0)
                if existing is not None and existing[0] == body:
                    return Ok(
                        TbbAssemblyOutcome(
                            self._update(
                                key,
                                candidate,
                                TbbAssemblyStatus.DUPLICATE_IGNORED,
                                received_at,
                            )
                        )
                    )
                del self._candidates[key]
                return _error(
                    ErrorCode.LINK_TBB_FRAGMENT_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "TBB start fragment conflicts with the active candidate",
                    packet_id=packet_id,
                )
        else:
            if candidate is None:
                return _error(
                    ErrorCode.LINK_TBB_CANDIDATE_NOT_FOUND,
                    ErrorCategory.PROTOCOL,
                    "TBB continuation/end fragment has no active start candidate",
                    packet_id=packet_id,
                )
            index = body.fragment_number
            expected_end = candidate.expected_count - 1
            valid_type = (
                isinstance(body, TbbEndFragmentBody) and index == expected_end
            ) or (
                isinstance(body, TbbContinuationFragmentBody)
                and 1 <= index < expected_end
            )
            if not valid_type:
                del self._candidates[key]
                return _error(
                    ErrorCode.LINK_TBB_FRAGMENT_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "TBB fragment type/index conflicts with announced count",
                    packet_id=packet_id,
                    expected_fragment_count=candidate.expected_count,
                    actual_fragment_index=index,
                )
            existing = candidate.fragments.get(index)
            if existing is not None:
                if existing[0] == body:
                    return Ok(
                        TbbAssemblyOutcome(
                            self._update(
                                key,
                                candidate,
                                TbbAssemblyStatus.DUPLICATE_IGNORED,
                                received_at,
                            )
                        )
                    )
                del self._candidates[key]
                return _error(
                    ErrorCode.LINK_TBB_FRAGMENT_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "same TBB fragment index carries different bytes",
                    packet_id=packet_id,
                    actual_fragment_index=index,
                )
            candidate.fragments[index] = (body, packet_id)
            initial_status = TbbAssemblyStatus.INCOMPLETE

        if candidate.received_bytes > 233:
            del self._candidates[key]
            return _error(
                ErrorCode.LINK_TBB_FRAGMENT_SIZE_INVALID,
                ErrorCategory.PROTOCOL,
                "assembled TBB payload exceeds 233 bytes",
                packet_id=packet_id,
                assembled_bytes=candidate.received_bytes,
            )
        if len(candidate.fragments) != candidate.expected_count:
            return Ok(
                TbbAssemblyOutcome(
                    self._update(key, candidate, initial_status, received_at)
                )
            )

        payload = b"".join(
            candidate.fragments[index][0].fragment_payload
            for index in range(candidate.expected_count)
        )
        decoded = decode_bulletin_board_payload(payload)
        if decoded.is_err:
            del self._candidates[key]
            return decoded
        bulletin_board, fingerprint, signature = decoded.value
        signing_input = build_tbb_signing_input(
            body.source_id,
            body.control_station_id,
            candidate.start.service_capability,
            body.bulletin_board_version,
            bulletin_board,
            fingerprint,
        )
        if signing_input.is_err:
            del self._candidates[key]
            return signing_input
        verified = verify_tbb_digest(
            self._trust_store,
            fingerprint,
            received_at,
            sha256_digest(signing_input.value),
            signature,
        )
        if verified.is_err:
            del self._candidates[key]
            return verified
        packet_ids = candidate.ordered_packet_ids()
        verified_tbb = VerifiedTbb(
            source_id=body.source_id,
            control_station_id=body.control_station_id,
            service_capability=candidate.start.service_capability,
            bulletin_board_version=body.bulletin_board_version,
            bulletin_board=bulletin_board,
            certificate_fingerprint=fingerprint,
            authentication_sequence=signature,
            fragment_count=candidate.expected_count,
            packet_ids=packet_ids,
            received_at=received_at,
        )
        update = self._update(
            key, candidate, TbbAssemblyStatus.VERIFIED, received_at
        )
        del self._candidates[key]
        return Ok(TbbAssemblyOutcome(update, verified_tbb))

    def control(
        self, request: TbbAssemblyControlRequest
    ) -> InternalResult[TbbAssemblyControlResult]:
        if not isinstance(request, TbbAssemblyControlRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "TbbAssemblyControlRequest is required",
            )
        target = self._candidates.get(request.target)
        if request.kind is TbbAssemblyControlKind.REPLACE:
            replacement = self._candidates.get(request.replacement)
            if (
                target is None
                or replacement is None
                or request.target.source_id != request.replacement.source_id
                or request.target.control_station_id
                != request.replacement.control_station_id
                or request.target.bulletin_board_version
                == request.replacement.bulletin_board_version
            ):
                return _error(
                    ErrorCode.LINK_TBB_REPLACEMENT_INVALID,
                    ErrorCategory.PROTOCOL,
                    "TBB replacement candidates are not both active compatible versions",
                )
        if target is None:
            return Ok(
                TbbAssemblyControlResult(
                    request.kind, (), 0, 0, len(self._candidates)
                )
            )
        discarded_count = len(target.fragments)
        discarded_bytes = target.received_bytes
        del self._candidates[request.target]
        return Ok(
            TbbAssemblyControlResult(
                request.kind,
                (request.target,),
                discarded_count,
                discarded_bytes,
                len(self._candidates),
            )
        )

    @staticmethod
    def _update(
        key: TbbCandidateKey,
        candidate: _Candidate,
        status: TbbAssemblyStatus,
        received_at: TimePoint,
    ) -> TbbAssemblyUpdate:
        return TbbAssemblyUpdate(
            key=key,
            status=status,
            received_fragment_count=len(candidate.fragments),
            expected_fragment_count=candidate.expected_count,
            packet_ids=candidate.ordered_packet_ids(),
            updated_at=received_at,
        )


__all__ = (
    "MAX_TBB_CANDIDATES",
    "TbbAssembler",
    "TbbAssemblyOutcome",
)
