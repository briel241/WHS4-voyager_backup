"""VDETER-TBB-SIG-v1 provider boundary and optional software crypto adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...common.contracts.message import BulletinBoardBody
from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.exceptions import InvalidValueError
from ...common.identifiers import StationId
from ...common.results import InternalResult, Ok, internal_error
from ...common.time import TimePoint
from .wire import encode_unsigned_bulletin_board

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _error(code: str, message: str, category: ErrorCategory = ErrorCategory.PROTOCOL, **details):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


@runtime_checkable
class TbbSigner(Protocol):
    def certificate_der(self) -> InternalResult[bytes]: ...

    def sign_sha256_digest(self, digest: bytes) -> InternalResult[bytes]: ...


@dataclass(frozen=True, slots=True)
class TrustedTbbCertificate:
    trust_id: str
    certificate_der: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.trust_id, str) or not self.trust_id or not self.trust_id.isascii():
            raise InvalidValueError("trust_id must be non-empty ASCII")
        if not isinstance(self.certificate_der, bytes) or not self.certificate_der:
            raise InvalidValueError("certificate_der must be non-empty bytes")


@runtime_checkable
class TbbTrustStore(Protocol):
    def find_approved_certificates(
        self, fingerprint: bytes, received_at: TimePoint
    ) -> InternalResult[tuple[TrustedTbbCertificate, ...]]: ...


@dataclass(frozen=True, slots=True)
class LinkCryptoDependencies:
    tbb_signer: TbbSigner | None = None
    tbb_trust_store: TbbTrustStore | None = None

    def __post_init__(self) -> None:
        if self.tbb_signer is not None and not isinstance(self.tbb_signer, TbbSigner):
            raise InvalidValueError("tbb_signer does not implement TbbSigner")
        if self.tbb_trust_store is not None and not isinstance(
            self.tbb_trust_store, TbbTrustStore
        ):
            raise InvalidValueError("tbb_trust_store does not implement TbbTrustStore")


def _read_der_tlv(data: bytes, offset: int) -> tuple[int, int, int, int]:
    if offset + 2 > len(data):
        raise ValueError("truncated DER TLV")
    tag = data[offset]
    first = data[offset + 1]
    if first < 0x80:
        length = first
        header = 2
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or offset + 2 + count > len(data):
            raise ValueError("invalid DER length")
        raw = data[offset + 2 : offset + 2 + count]
        if raw[0] == 0:
            raise ValueError("non-canonical DER length")
        length = int.from_bytes(raw, "big")
        if length < 0x80:
            raise ValueError("non-canonical DER long length")
        header = 2 + count
    start = offset + header
    end = start + length
    if end > len(data):
        raise ValueError("truncated DER value")
    return tag, start, end, end


def certificate_signature_value(certificate_der: bytes) -> InternalResult[bytes]:
    """Extract top-level Certificate.signatureValue BIT STRING content octets."""

    if not isinstance(certificate_der, bytes) or not certificate_der:
        return _error(
            ErrorCode.LINK_TBB_CERTIFICATE_MALFORMED,
            "certificate DER is empty or not bytes",
            ErrorCategory.VALIDATION,
        )
    try:
        tag, content_start, content_end, outer_end = _read_der_tlv(certificate_der, 0)
        if tag != 0x30 or outer_end != len(certificate_der):
            raise ValueError("Certificate must be one DER SEQUENCE")
        cursor = content_start
        for expected_index in range(3):
            field_tag, value_start, value_end, next_cursor = _read_der_tlv(
                certificate_der, cursor
            )
            if expected_index == 2:
                if field_tag != 0x03 or value_start == value_end:
                    raise ValueError("signatureValue must be a non-empty BIT STRING")
                if certificate_der[value_start] != 0:
                    raise ValueError("signatureValue unused-bits count must be zero")
                signature = certificate_der[value_start + 1 : value_end]
                if len(signature) < 6:
                    raise ValueError("signatureValue is shorter than 48 bits")
            cursor = next_cursor
        if cursor != content_end:
            raise ValueError("Certificate has trailing top-level fields")
        return Ok(signature)
    except ValueError as error:
        return _error(
            ErrorCode.LINK_TBB_CERTIFICATE_MALFORMED,
            str(error),
            ErrorCategory.VALIDATION,
        )


def certificate_fingerprint(certificate_der: bytes) -> InternalResult[bytes]:
    parsed = _load_p256_certificate(certificate_der)
    if parsed.is_err:
        return parsed
    signature = certificate_signature_value(certificate_der)
    if signature.is_err:
        return signature
    return Ok(signature.value[-6:])


def build_tbb_signing_input(
    source_id: StationId,
    control_station_id: int,
    service_capability: int,
    bulletin_board_version: int,
    bulletin_board: BulletinBoardBody,
    fingerprint: bytes,
) -> InternalResult[bytes]:
    if not isinstance(source_id, StationId) or source_id.value == 0:
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            "TBB Source ID must be non-zero",
            ErrorCategory.VALIDATION,
        )
    if (
        isinstance(control_station_id, bool)
        or not isinstance(control_station_id, int)
        or not 0 <= control_station_id <= 255
        or isinstance(service_capability, bool)
        or not isinstance(service_capability, int)
        or not 0 <= service_capability <= 2
        or isinstance(bulletin_board_version, bool)
        or not isinstance(bulletin_board_version, int)
        or not 0 <= bulletin_board_version <= 65535
        or not isinstance(fingerprint, bytes)
        or len(fingerprint) != 6
    ):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            "TBB signing metadata is invalid",
            ErrorCategory.VALIDATION,
        )
    unsigned = encode_unsigned_bulletin_board(bulletin_board)
    if unsigned.is_err:
        return unsigned
    return Ok(
        source_id.value.to_bytes(4, "big")
        + bytes((control_station_id, service_capability))
        + bulletin_board_version.to_bytes(2, "big")
        + unsigned.value
        + fingerprint
    )


def sha256_digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _crypto_modules():
    try:
        from cryptography import __version__ as cryptography_version
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except (ImportError, OSError) as error:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_UNAVAILABLE,
            "optional cryptography provider is unavailable",
            ErrorCategory.RESOURCE,
            provider_error_type=type(error).__name__,
        )
    try:
        major = int(cryptography_version.split(".", 1)[0])
    except (AttributeError, ValueError):
        major = -1
    if major != 50:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_UNAVAILABLE,
            "VDETER-TBB-SIG-v1 requires cryptography major version 50",
            ErrorCategory.RESOURCE,
            provider_major_version=major,
        )
    return Ok((x509, InvalidSignature, hashes, serialization, ec, utils))


def _load_p256_certificate(certificate_der: bytes):
    modules = _crypto_modules()
    if modules.is_err:
        return modules
    x509, _, _, serialization, ec, _ = modules.value
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
        public_key = certificate.public_key()
    except (TypeError, ValueError) as error:
        return _error(
            ErrorCode.LINK_TBB_CERTIFICATE_MALFORMED,
            "certificate DER could not be parsed",
            ErrorCategory.VALIDATION,
            provider_error_type=type(error).__name__,
        )
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROFILE_UNSUPPORTED,
            "certificate public key is not P-256",
            ErrorCategory.UNSUPPORTED,
        )
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return Ok((certificate, public_key, spki, modules.value))


def _p1363_to_der(signature: bytes, utils):
    if not isinstance(signature, bytes) or len(signature) != 64:
        return None
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 1 <= r < P256_ORDER or not 1 <= s < P256_ORDER:
        return None
    return utils.encode_dss_signature(r, s)


def verify_with_certificate(
    certificate_der: bytes, digest: bytes, signature: bytes
) -> InternalResult[bool]:
    if not isinstance(digest, bytes) or len(digest) != 32:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "SHA-256 digest must be 32 bytes",
            ErrorCategory.VALIDATION,
        )
    loaded = _load_p256_certificate(certificate_der)
    if loaded.is_err:
        return loaded
    _, public_key, _, modules = loaded.value
    _, InvalidSignature, hashes, _, ec, utils = modules
    der_signature = _p1363_to_der(signature, utils)
    if der_signature is None:
        return Ok(False)
    try:
        public_key.verify(
            der_signature,
            digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        return Ok(True)
    except InvalidSignature:
        return Ok(False)
    except Exception as error:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "crypto provider failed during signature verification",
            ErrorCategory.INTERNAL,
            provider_error_type=type(error).__name__,
        )


def sign_tbb_digest(signer: TbbSigner | None, digest: bytes):
    certificate = get_signer_certificate(signer)
    if certificate.is_err:
        return certificate
    try:
        signature = signer.sign_sha256_digest(digest)
        if not hasattr(signature, "is_err"):
            raise TypeError("sign_sha256_digest returned a non-Result")
        if signature.is_err:
            return signature
        if not isinstance(signature.value, bytes) or len(signature.value) != 64:
            raise TypeError("sign_sha256_digest returned a non-P1363 signature")
    except Exception as error:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "injected signer violated its provider contract",
            ErrorCategory.INTERNAL,
            provider_error_type=type(error).__name__,
        )
    verified = verify_with_certificate(certificate.value, digest, signature.value)
    if verified.is_err:
        return verified
    if not verified.value:
        return _error(
            ErrorCode.LINK_TBB_KEY_CERTIFICATE_MISMATCH,
            "signer output does not verify with its certificate public key",
            ErrorCategory.VALIDATION,
        )
    return Ok((certificate.value, signature.value))


def get_signer_certificate(signer: TbbSigner | None):
    if signer is None:
        return _error(
            ErrorCode.LINK_TBB_SIGNER_NOT_CONFIGURED,
            "TBB signer is not configured",
            ErrorCategory.RESOURCE,
        )
    if not isinstance(signer, TbbSigner):
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "injected signer does not implement TbbSigner",
            ErrorCategory.VALIDATION,
        )
    try:
        certificate = signer.certificate_der()
        if not hasattr(certificate, "is_err"):
            raise TypeError("certificate_der returned a non-Result")
        if certificate.is_err:
            return certificate
        if not isinstance(certificate.value, bytes) or not certificate.value:
            raise TypeError("certificate_der returned invalid bytes")
    except Exception as error:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "injected signer violated its provider contract",
            ErrorCategory.INTERNAL,
            provider_error_type=type(error).__name__,
        )
    return Ok(certificate.value)


def verify_tbb_digest(
    trust_store: TbbTrustStore | None,
    fingerprint: bytes,
    received_at: TimePoint,
    digest: bytes,
    signature: bytes,
):
    if trust_store is None:
        return _error(
            ErrorCode.LINK_TBB_TRUST_STORE_NOT_CONFIGURED,
            "TBB trust store is not configured",
            ErrorCategory.RESOURCE,
        )
    if not isinstance(trust_store, TbbTrustStore):
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "injected trust store does not implement TbbTrustStore",
            ErrorCategory.VALIDATION,
        )
    try:
        found = trust_store.find_approved_certificates(fingerprint, received_at)
        if not hasattr(found, "is_err"):
            raise TypeError("trust store returned a non-Result")
        if found.is_err:
            return found
        candidates = found.value
        if not isinstance(candidates, tuple) or any(
            not isinstance(item, TrustedTbbCertificate) for item in candidates
        ):
            raise TypeError("trust store returned invalid certificate tuple")
    except Exception as error:
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
            "injected trust store violated its provider contract",
            ErrorCategory.INTERNAL,
            provider_error_type=type(error).__name__,
        )
    if not candidates:
        return _error(
            ErrorCode.LINK_TBB_CERTIFICATE_NOT_FOUND,
            "no approved certificate matches the received fingerprint",
            ErrorCategory.UNSUPPORTED,
        )

    unique: dict[bytes, bytes] = {}
    for candidate in candidates:
        actual = certificate_fingerprint(candidate.certificate_der)
        if actual.is_err:
            return actual
        if actual.value != fingerprint:
            return _error(
                ErrorCode.LINK_TBB_FINGERPRINT_MISMATCH,
                "trust store returned a certificate with a different fingerprint",
                ErrorCategory.PROTOCOL,
                trust_id=candidate.trust_id,
            )
        loaded = _load_p256_certificate(candidate.certificate_der)
        if loaded.is_err:
            return loaded
        unique.setdefault(loaded.value[2], candidate.certificate_der)

    successful = 0
    for certificate_der in unique.values():
        verified = verify_with_certificate(certificate_der, digest, signature)
        if verified.is_err:
            return verified
        successful += int(verified.value)
    if successful == 0:
        return _error(
            ErrorCode.LINK_TBB_SIGNATURE_INVALID,
            "TBB signature did not verify with any distinct approved public key",
        )
    if successful > 1:
        return _error(
            ErrorCode.LINK_TBB_CERTIFICATE_AMBIGUOUS,
            "TBB signature verified with more than one distinct approved public key",
        )
    return Ok(successful)


class CryptographyTbbSigner:
    __slots__ = ("_private_key", "_certificate_der")

    def __init__(self, private_key, certificate_der: bytes) -> None:
        self._private_key = private_key
        self._certificate_der = bytes(certificate_der)

    def certificate_der(self):
        return Ok(self._certificate_der)

    def sign_sha256_digest(self, digest: bytes):
        modules = _crypto_modules()
        if modules.is_err:
            return modules
        _, _, hashes, _, ec, utils = modules.value
        if not isinstance(digest, bytes) or len(digest) != 32:
            return _error(
                ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID,
                "SHA-256 digest must be 32 bytes",
                ErrorCategory.VALIDATION,
            )
        try:
            der = self._private_key.sign(
                digest,
                ec.ECDSA(
                    utils.Prehashed(hashes.SHA256()), deterministic_signing=True
                ),
            )
            r, s = utils.decode_dss_signature(der)
            if s > P256_ORDER // 2:
                s = P256_ORDER - s
            return Ok(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        except Exception as error:
            return _error(
                ErrorCode.LINK_TBB_SIGNING_FAILED,
                "software signer failed",
                ErrorCategory.INTERNAL,
                provider_error_type=type(error).__name__,
            )


def create_cryptography_tbb_signer(
    private_key_data: bytes,
    certificate_der: bytes,
    password: bytes | None = None,
):
    modules = _crypto_modules()
    if modules.is_err:
        return modules
    _, _, _, serialization, ec, _ = modules.value
    if not isinstance(private_key_data, bytes) or not private_key_data:
        return _error(
            ErrorCode.LINK_TBB_PRIVATE_KEY_MALFORMED,
            "private key input is empty",
            ErrorCategory.VALIDATION,
        )
    try:
        if private_key_data.startswith(b"-----BEGIN"):
            private_key = serialization.load_pem_private_key(
                private_key_data, password=password
            )
        else:
            private_key = serialization.load_der_private_key(
                private_key_data, password=password
            )
    except (TypeError, ValueError) as error:
        return _error(
            ErrorCode.LINK_TBB_PRIVATE_KEY_MALFORMED,
            "private key could not be parsed",
            ErrorCategory.VALIDATION,
            provider_error_type=type(error).__name__,
        )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        return _error(
            ErrorCode.LINK_TBB_CRYPTO_PROFILE_UNSUPPORTED,
            "private key is not P-256",
            ErrorCategory.UNSUPPORTED,
        )
    signature_value = certificate_signature_value(certificate_der)
    if signature_value.is_err:
        return signature_value
    loaded = _load_p256_certificate(certificate_der)
    if loaded.is_err:
        return loaded
    return Ok(CryptographyTbbSigner(private_key, certificate_der))


__all__ = (
    "CryptographyTbbSigner",
    "LinkCryptoDependencies",
    "P256_ORDER",
    "TbbSigner",
    "TbbTrustStore",
    "TrustedTbbCertificate",
    "build_tbb_signing_input",
    "certificate_fingerprint",
    "certificate_signature_value",
    "create_cryptography_tbb_signer",
    "get_signer_certificate",
    "sha256_digest",
    "sign_tbb_digest",
    "verify_tbb_digest",
    "verify_with_certificate",
)
