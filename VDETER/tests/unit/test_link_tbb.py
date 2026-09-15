"""TC-LINK-013/014/020/025/027/028/029: TBB transport and authentication."""

from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from vdeter.common.contracts.link import (
    LinkDecodeRequest,
    TbbAssemblyControlKind,
    TbbAssemblyControlRequest,
    TbbEncodeRequest,
)
from vdeter.common.contracts.message import (
    BulletinBoardBody,
    ControlStationServiceArea,
    DecodedMessage,
    LogicalChannelGroup,
    PhysicalChannelBandwidth,
    PhysicalChannelDefinition,
    TbbAssemblyStatus,
    TbbCandidateKey,
    TbbStartFragmentBody,
)
from vdeter.common.errors import Correlation, ErrorCode
from vdeter.common.identifiers import DeterministicIdSource, StationId
from vdeter.common.results import Ok
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.link.data_plane import LinkDataPlane
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.tbb.assembly import TbbAssembler
from vdeter.link.tbb.crypto import (
    LinkCryptoDependencies,
    TrustedTbbCertificate,
    create_cryptography_tbb_signer,
    build_tbb_signing_input,
    certificate_fingerprint,
    certificate_signature_value,
    get_signer_certificate,
    sha256_digest,
    verify_tbb_digest,
)
from vdeter.link.tbb.transport import fragment_tbb_payload
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
UTC = TimePoint(100, 0, ClockDomain.UTC, "test.utc")


def _body():
    groups = tuple(LogicalChannelGroup(index, ()) for index in range(6))
    channel = PhysicalChannelDefinition(
        1, 2, PhysicalChannelBandwidth.KHZ_25, True, 3, 4, groups
    )
    return BulletinBoardBody(
        0x01020304,
        5,
        15,
        (channel,),
        0,
        ControlStationServiceArea(0, 0, 0, 0, 0),
    )


class _TrustStore:
    def __init__(self, certificates):
        self.certificates = certificates

    def find_approved_certificates(self, fingerprint, received_at):
        return Ok(self.certificates)


class _MalformedSigner:
    def certificate_der(self):
        return b"not-a-result"

    def sign_sha256_digest(self, digest):
        return b"not-a-result"


class TbbTransportTest(unittest.TestCase):
    def test_TC_LINK_027_canonical_fragment_boundaries(self):
        expected = {
            38: (38,),
            39: (38, 1),
            77: (38, 39),
            78: (38, 39, 1),
            233: (38, 39, 39, 39, 39, 39),
        }
        for size, lengths in expected.items():
            with self.subTest(size=size):
                fragmented = fragment_tbb_payload(
                    StationId(1), 2, 2, 3, bytes(size)
                ).value
                self.assertEqual(
                    lengths,
                    tuple(len(item.fragment_payload) for item in fragmented),
                )
        rejected = fragment_tbb_payload(StationId(1), 2, 2, 3, bytes(234))
        self.assertEqual(ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED, rejected.error.code)

    def test_TC_LINK_025_candidate_capacity_and_control(self):
        assembler = TbbAssembler(None)
        keys = []
        for index in range(64):
            body = TbbStartFragmentBody(StationId(index + 1), 1, 2, 3, 2, b"x")
            key = TbbCandidateKey(body.source_id, 1, 3)
            keys.append(key)
            result = assembler.process(
                DeterministicIdSource(index).new_packet_id(),
                UTC,
                DecodedMessage(20, 13, body=body),
            )
            self.assertIs(result.value.update.status, TbbAssemblyStatus.STARTED)
        overflow = assembler.process(
            DeterministicIdSource(100).new_packet_id(),
            UTC,
            DecodedMessage(
                20, 13, body=TbbStartFragmentBody(StationId(65), 1, 2, 3, 2, b"x")
            ),
        )
        self.assertEqual(
            ErrorCode.LINK_TBB_ASSEMBLY_CAPACITY_EXHAUSTED, overflow.error.code
        )
        aborted = assembler.control(
            TbbAssemblyControlRequest(
                TbbAssemblyControlKind.ABORT, keys[0], None, Correlation()
            )
        ).value
        self.assertEqual(63, aborted.active_candidate_count)
        repeated = assembler.control(
            TbbAssemblyControlRequest(
                TbbAssemblyControlKind.ABORT, keys[0], None, Correlation()
            )
        ).value
        self.assertEqual((), repeated.discarded_candidates)

    def test_TC_LINK_025_duplicate_conflict_and_replacement(self):
        assembler = TbbAssembler(None)
        first = TbbStartFragmentBody(StationId(1), 1, 2, 3, 2, b"x")
        packet_ids = DeterministicIdSource(25)
        started = assembler.process(
            packet_ids.new_packet_id(), UTC, DecodedMessage(20, 13, body=first)
        )
        duplicate = assembler.process(
            packet_ids.new_packet_id(), UTC, DecodedMessage(20, 13, body=first)
        )
        self.assertIs(started.value.update.status, TbbAssemblyStatus.STARTED)
        self.assertIs(
            duplicate.value.update.status, TbbAssemblyStatus.DUPLICATE_IGNORED
        )

        conflict = assembler.process(
            packet_ids.new_packet_id(),
            UTC,
            DecodedMessage(
                20,
                13,
                body=TbbStartFragmentBody(StationId(1), 1, 2, 3, 2, b"y"),
            ),
        )
        self.assertEqual(ErrorCode.LINK_TBB_FRAGMENT_CONFLICT, conflict.error.code)
        self.assertEqual(0, assembler.active_count)

        version_3 = TbbStartFragmentBody(StationId(1), 1, 2, 3, 2, b"x")
        version_4 = TbbStartFragmentBody(StationId(1), 1, 2, 4, 2, b"x")
        assembler.process(
            packet_ids.new_packet_id(), UTC, DecodedMessage(20, 13, body=version_3)
        )
        assembler.process(
            packet_ids.new_packet_id(), UTC, DecodedMessage(20, 13, body=version_4)
        )
        replaced = assembler.control(
            TbbAssemblyControlRequest(
                TbbAssemblyControlKind.REPLACE,
                TbbCandidateKey(StationId(1), 1, 3),
                TbbCandidateKey(StationId(1), 1, 4),
                Correlation(),
            )
        ).value
        self.assertEqual((TbbCandidateKey(StationId(1), 1, 3),), replaced.discarded_candidates)
        self.assertEqual(1, replaced.active_candidate_count)

    def test_TC_LINK_020_malformed_signer_provider_is_structured(self):
        result = get_signer_certificate(_MalformedSigner())
        self.assertEqual(ErrorCode.LINK_TBB_CRYPTO_PROVIDER_INVALID, result.error.code)

    def test_TC_LINK_028_trust_store_decision_table(self):
        fingerprint = b"ABCDEF"
        digest = bytes(32)
        signature = bytes(64)

        missing = verify_tbb_digest(
            _TrustStore(()), fingerprint, UTC, digest, signature
        )
        self.assertEqual(ErrorCode.LINK_TBB_CERTIFICATE_NOT_FOUND, missing.error.code)

        one = TrustedTbbCertificate("one", b"certificate-one")
        two = TrustedTbbCertificate("two", b"certificate-two")
        with mock.patch(
            "vdeter.link.tbb.crypto.certificate_fingerprint",
            return_value=Ok(b"UVWXYZ"),
        ):
            mismatch = verify_tbb_digest(
                _TrustStore((one,)), fingerprint, UTC, digest, signature
            )
        self.assertEqual(ErrorCode.LINK_TBB_FINGERPRINT_MISMATCH, mismatch.error.code)

        with (
            mock.patch(
                "vdeter.link.tbb.crypto.certificate_fingerprint",
                side_effect=(Ok(fingerprint), Ok(fingerprint)),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto._load_p256_certificate",
                side_effect=(
                    Ok((None, None, b"same-spki", None)),
                    Ok((None, None, b"same-spki", None)),
                ),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto.verify_with_certificate",
                return_value=Ok(True),
            ) as verifier,
        ):
            deduplicated = verify_tbb_digest(
                _TrustStore((one, two)), fingerprint, UTC, digest, signature
            )
        self.assertEqual(1, deduplicated.value)
        verifier.assert_called_once()

        with (
            mock.patch(
                "vdeter.link.tbb.crypto.certificate_fingerprint",
                return_value=Ok(fingerprint),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto._load_p256_certificate",
                return_value=Ok((None, None, b"one-spki", None)),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto.verify_with_certificate",
                return_value=Ok(False),
            ),
        ):
            invalid = verify_tbb_digest(
                _TrustStore((one,)), fingerprint, UTC, digest, signature
            )
        self.assertEqual(ErrorCode.LINK_TBB_SIGNATURE_INVALID, invalid.error.code)

        with (
            mock.patch(
                "vdeter.link.tbb.crypto.certificate_fingerprint",
                side_effect=(Ok(fingerprint), Ok(fingerprint)),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto._load_p256_certificate",
                side_effect=(
                    Ok((None, None, b"spki-one", None)),
                    Ok((None, None, b"spki-two", None)),
                ),
            ),
            mock.patch(
                "vdeter.link.tbb.crypto.verify_with_certificate",
                return_value=Ok(True),
            ),
        ):
            ambiguous = verify_tbb_digest(
                _TrustStore((one, two)), fingerprint, UTC, digest, signature
            )
        self.assertEqual(ErrorCode.LINK_TBB_CERTIFICATE_AMBIGUOUS, ambiguous.error.code)


@unittest.skipUnless(HAS_CRYPTO, "optional cryptography extra is not installed")
class TbbCryptoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.derive_private_key(1, ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VDETER TBB Test")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
            .sign(key, hashes.SHA256(), ecdsa_deterministic=True)
        )
        cls.certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        private_der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cls.signer = create_cryptography_tbb_signer(
            private_der, cls.certificate_der
        ).value

    def setUp(self):
        self.ids = DeterministicIdSource(14029)
        messages = build_message_registry(RegistryScope.OPERATIONAL).value
        profiles = build_link_profile_registry(RegistryScope.OPERATIONAL).value
        messages.freeze()
        profiles.freeze()
        trust = _TrustStore((TrustedTbbCertificate("fixed", self.certificate_der),))
        crypto = LinkCryptoDependencies(self.signer, trust)
        self.link = LinkDataPlane(
            self.ids, messages, profiles, crypto_dependencies=crypto
        )

    def _request(self):
        return TbbEncodeRequest(StationId(1), 2, 2, 3, _body(), Correlation())

    def test_TC_LINK_013_014_029_signed_round_trip_with_end_before_middle(self):
        encoded = self.link.encode_tbb(self._request())
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(3, len(encoded.value.packets))
        self.assertEqual(64, len(encoded.value.authentication_sequence))
        results = []
        for index in (0, 2, 1):
            packet = encoded.value.packets[index]
            result = self.link.decode_packet(
                LinkDecodeRequest(packet.packet_id, packet.link_id, UTC, packet.packet)
            )
            self.assertTrue(result.is_ok, getattr(result, "error", None))
            results.append(result.value)
        self.assertEqual(
            (TbbAssemblyStatus.STARTED, TbbAssemblyStatus.INCOMPLETE, TbbAssemblyStatus.VERIFIED),
            tuple(item.tbb_assembly_updates[0].status for item in results),
        )
        self.assertEqual(_body(), results[-1].verified_tbbs[0].bulletin_board)

    def test_TC_LINK_014_project_approved_fixed_vector(self):
        vector_path = Path(__file__).parents[1] / "vectors" / "tbb" / "tbb_sig_v1.json"
        vector = json.loads(vector_path.read_text(encoding="utf-8"))
        fingerprint = certificate_fingerprint(self.certificate_der).value
        signing_input = build_tbb_signing_input(
            StationId(1), 2, 2, 3, _body(), fingerprint
        ).value
        encoded = self.link.encode_tbb(self._request()).value
        self.assertEqual("PROJECT-APPROVED VDETER-TBB-SIG-v1", vector["source"])
        self.assertEqual(
            vector["certificate_signature_value_hex"],
            certificate_signature_value(self.certificate_der).value.hex(),
        )
        self.assertEqual(vector["certificate_fingerprint_hex"], fingerprint.hex())
        self.assertEqual(vector["signing_input_hex"], signing_input.hex())
        self.assertEqual(vector["sha256_digest_hex"], sha256_digest(signing_input).hex())
        self.assertEqual(
            vector["p1363_low_s_signature_hex"],
            encoded.authentication_sequence.hex(),
        )
        self.assertEqual(vector["packet_hex"], [item.packet.hex() for item in encoded.packets])

    def test_TC_LINK_020_028_missing_dependencies_and_certificate(self):
        no_signer = LinkDataPlane(
            self.ids,
            self.link._message_registry,
            self.link._link_profile_registry,
        ).encode_tbb(self._request())
        self.assertEqual(ErrorCode.LINK_TBB_SIGNER_NOT_CONFIGURED, no_signer.error.code)

        encoded = self.link.encode_tbb(self._request()).value
        empty_crypto = LinkCryptoDependencies(None, _TrustStore(()))
        receiver = LinkDataPlane(
            DeterministicIdSource(1),
            self.link._message_registry,
            self.link._link_profile_registry,
            crypto_dependencies=empty_crypto,
        )
        final = None
        for packet in encoded.packets:
            final = receiver.decode_packet(
                LinkDecodeRequest(packet.packet_id, packet.link_id, UTC, packet.packet)
            )
        self.assertEqual(ErrorCode.LINK_TBB_CERTIFICATE_NOT_FOUND, final.error.code)


if __name__ == "__main__":
    unittest.main()
