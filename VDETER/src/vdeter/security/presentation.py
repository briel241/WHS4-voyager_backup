"""Protect the codec body while retaining the exact VPFI/MF dispatch prefix."""

from threading import Condition
from time import perf_counter_ns
from hashlib import sha256

from ..common.errors import ErrorCode
from ..common.results import Ok
from .constants import PDU_OVERHEAD
from .errors import SecurityFault, fault_result
from .metrics import SecurityMetrics
from .policy import SecurityPolicy
from .protocol.aad import build_aad
from .protocol.header import SecurityHeader
from .protocol.nonce import build_nonce
from .protocol.pdu import build_security_pdu, parse_security_pdu


class PresentationSecurity:
    def __init__(self, policy, manager, metrics=None):
        self.policy = policy
        self.manager = manager
        self.metrics = metrics or SecurityMetrics()
        self._condition = Condition()
        self._closing = False
        self._closed = False
        self._inflight = 0
        self.experiment_observer = None

    def _observe(self, *, stage, wire, route, elapsed_ns=None, body=None, error=None,
                 transfer_id=None):
        observer = self.experiment_observer
        if observer is not None:
            source = route.original_source.station_id.value
            destination = route.original_destination.address.station_id.value
            # An opaque fingerprint of EXISTING dispatch/header/route fields links
            # tag/ciphertext failures without logging the header, counter or nonce.
            # It is diagnostic metadata, never proof of authenticated delivery.
            fingerprint = None
            if self.manager is not None and len(wire) >= 10 and stage != "protect_error":
                fingerprint = sha256(wire[:10] + source.to_bytes(4, "big")
                                     + destination.to_bytes(4, "big")).hexdigest()
            observer.record(stage=stage, wire_sha256=sha256(wire).hexdigest(),
                            security_message_fingerprint=fingerprint,
                            source=source, destination=destination,
                            pdu_bytes=len(wire), body_bytes=len(body) if body is not None else None,
                            body_sha256=sha256(body).hexdigest() if body is not None else None,
                            elapsed_ns=elapsed_ns, error=error, transfer_id=transfer_id)

    def encoded_overhead(self, entry) -> int:
        return PDU_OVERHEAD if self.policy.for_format(entry) is SecurityPolicy.REQUIRED else 0

    def _record_fault(self, fault: SecurityFault) -> None:
        metrics = {
            ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE: "security_replay_duplicate_total",
            ErrorCode.PRESENTATION_SECURITY_REPLAY_TOO_OLD: "security_replay_too_old_total",
            ErrorCode.PRESENTATION_SECURITY_EPOCH_MISMATCH: "security_epoch_mismatch_total",
            ErrorCode.PRESENTATION_TRUNCATED: "security_malformed_pdu_total",
            ErrorCode.PRESENTATION_STRUCTURE_INVALID: "security_malformed_pdu_total",
            ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED: "security_malformed_pdu_total",
            ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED: "security_usage_exhausted_total",
            ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE: "security_state_store_failure_total",
        }
        metric = metrics.get(fault.code)
        if metric:
            self.metrics.increment(metric)

    def _admit(self):
        with self._condition:
            if self._closing:
                raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE)
            self._inflight += 1

    def _release(self):
        with self._condition:
            self._inflight -= 1
            self._condition.notify_all()

    @staticmethod
    def _pair(route):
        if route.original_destination.address.is_broadcast:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
        return route.original_source.station_id, route.original_destination.address.station_id

    def protect(self, *, entry, codec, plain_wire: bytes, route, transfer_id=None):
        try:
            self._admit()
            try:
                if self.policy.for_format(entry) is SecurityPolicy.DISABLED:
                    self._observe(stage="protect", wire=plain_wire, route=route,
                                  body=plain_wire[3:], transfer_id=transfer_id)
                    return Ok(plain_wire)
                prefix = codec.discriminator.prefix
                if type(plain_wire) is not bytes or not plain_wire.startswith(prefix):
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
                body = plain_wire[3:]
                source, destination = self._pair(route)
                session = self.manager.tx_session(source_id=source, destination_id=destination)
                counter = session.reserve_invocation(len(body))
                header = SecurityHeader(1, counter)
                header_bytes = header.to_bytes()
                aad = build_aad(prefix=prefix, route=route, security_header=header_bytes)
                nonce = build_nonce(session.material.nonce_prefix, counter)
                from .crypto.aes_gcm import encrypt_detached

                started = perf_counter_ns() if self.experiment_observer is not None else None
                ciphertext, tag = encrypt_detached(key=session.material.aes_key, nonce=nonce,
                                                   plaintext=body, aad=aad)
                elapsed = perf_counter_ns() - started if started is not None else None
                self.metrics.increment("security_encrypt_total")
                wire = prefix + build_security_pdu(header, ciphertext, tag)
                self._observe(stage="protect", wire=wire, route=route, elapsed_ns=elapsed,
                              body=body, transfer_id=transfer_id)
                return Ok(wire)
            finally:
                self._release()
        except SecurityFault as fault:
            self._record_fault(fault)
            self._observe(stage="protect_error", wire=plain_wire, route=route,
                          error=fault.code, transfer_id=transfer_id)
            return fault_result(fault)

    def unprotect(self, *, entry, codec, secure_wire: bytes, route):
        elapsed = None
        try:
            self._admit()
            try:
                if self.policy.for_format(entry) is SecurityPolicy.DISABLED:
                    self._observe(stage="unprotect", wire=secure_wire, route=route,
                                  body=secure_wire[3:])
                    return Ok(secure_wire)
                prefix = codec.discriminator.prefix
                if not secure_wire.startswith(prefix):
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
                header, ciphertext, tag = parse_security_pdu(secure_wire[3:])
                bound = codec.encoded_size_bound(len(ciphertext))
                if bound.is_err or bound.value != 3 + len(ciphertext):
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_CIPHERTEXT_LENGTH_INVALID)
                source, destination = self._pair(route)
                session = self.manager.rx_session(source_id=source, destination_id=destination,
                                                  key_epoch=header.key_epoch)
                with session.lock:
                    session.replay_window.precheck(header.message_counter)
                aad = build_aad(prefix=prefix, route=route, security_header=header.to_bytes())
                nonce = build_nonce(session.material.nonce_prefix, header.message_counter)
                reservation = session.store.reserve_rx_verification(session.material.material_id, len(ciphertext))
                try:
                    from .crypto.aes_gcm import decrypt_detached

                    started = perf_counter_ns() if self.experiment_observer is not None else None
                    plaintext = decrypt_detached(key=session.material.aes_key, nonce=nonce,
                                                 ciphertext=ciphertext, tag=tag, aad=aad)
                    elapsed = perf_counter_ns() - started if started is not None else None
                except SecurityFault as fault:
                    elapsed = perf_counter_ns() - started if started is not None else None
                    if fault.code == ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED:
                        session.store.commit_rx_authentication_failure(reservation)
                        self.metrics.increment("security_auth_failure_total")
                    raise
                except Exception:
                    session.store.inhibit_rx(reservation)
                with session.lock:
                    try:
                        snapshot = session.replay_window.candidate(header.message_counter)
                    except SecurityFault:
                        session.store.release_rx_duplicate(reservation)
                        raise
                    session.store.commit_rx_authenticated(reservation, snapshot.highest_counter, snapshot.replay_bitmap)
                    session.replay_window.commit(snapshot)
                self.metrics.increment("security_decrypt_success_total")
                self._observe(stage="unprotect", wire=secure_wire, route=route,
                              elapsed_ns=elapsed, body=plaintext)
                return Ok(prefix + plaintext)
            finally:
                self._release()
        except SecurityFault as fault:
            self._record_fault(fault)
            self._observe(stage="unprotect_error", wire=secure_wire, route=route,
                          error=fault.code, elapsed_ns=elapsed)
            return fault_result(fault)

    def close_final(self, *, timeout_seconds: float) -> None:
        with self._condition:
            if self._closed:
                return
            self._closing = True
            if not self._condition.wait_for(lambda: self._inflight == 0, timeout_seconds):
                raise TimeoutError("Security final close timed out")
            self._closed = True
        if self.manager is not None:
            self.manager.close()
