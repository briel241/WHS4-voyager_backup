"""Explicit VDS2 TEST/PROJECT payload security policy."""

from dataclasses import dataclass
from enum import Enum

from ..profiles.payload_formats import (
    AIS_SCHEME1_PAYLOAD_FORMAT, CERTIFICATE_REPORT_PAYLOAD_FORMAT,
    SC_VDE_ACK_PAYLOAD_FORMAT, SC_VDE_COMMAND_PAYLOAD_FORMAT,
    SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT, TEST_PAYLOAD_FORMAT,
)


class SecurityPolicy(str, Enum):
    REQUIRED = "REQUIRED"
    DISABLED = "DISABLED"


_REQUIRED = frozenset((
    SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT,
    SC_VDE_COMMAND_PAYLOAD_FORMAT,
    SC_VDE_ACK_PAYLOAD_FORMAT,
))
_KNOWN = _REQUIRED | frozenset((
    CERTIFICATE_REPORT_PAYLOAD_FORMAT, AIS_SCHEME1_PAYLOAD_FORMAT, TEST_PAYLOAD_FORMAT,
))


@dataclass(frozen=True, slots=True)
class PayloadSecurityPolicy:
    profile_id: str
    required: frozenset[str]

    @classmethod
    def enforced(cls):
        return cls("VDS2_SC_VDE_REQUIRED_V1", _REQUIRED)

    @classmethod
    def legacy_all_disabled(cls):
        return cls("LEGACY_ALL_DISABLED", frozenset())

    def for_format(self, entry) -> SecurityPolicy:
        name = entry.payload_format.value
        if self.profile_id == "LEGACY_ALL_DISABLED":
            return SecurityPolicy.DISABLED
        if name not in _KNOWN:
            raise ValueError("Unregistered security policy format")
        return SecurityPolicy.REQUIRED if name in self.required else SecurityPolicy.DISABLED

    def validate_registry(self, registry, codecs: dict) -> None:
        names = {entry.payload_format.value for entry in registry.registry.entries()}
        if self.profile_id == "LEGACY_ALL_DISABLED":
            if self.required:
                raise ValueError("Legacy policy cannot require encryption")
            return
        if not names <= _KNOWN or not self.required <= names:
            raise ValueError("Security policy/registry mismatch")
        for entry in registry.registry.entries():
            if self.for_format(entry) is not SecurityPolicy.REQUIRED:
                continue
            codec = codecs[entry.codec_ref]
            discriminator = codec.discriminator
            if (len(discriminator.prefix) != 3 or discriminator.third_byte_mask != 0
                    or discriminator.prefix != b"\xff\x7f" + bytes((entry.message_format,))
                    or not getattr(codec, "addressed_only", False)):
                raise ValueError("Required codec prefix/route mismatch")
            maximum = codec.encoded_size_bound(codec.max_application_bytes)
            if maximum.is_err or maximum.value != codec.max_application_bytes + 3:
                raise ValueError("Required codec maximum mismatch")
        if self.required and max(codecs[entry.codec_ref].max_application_bytes for entry in registry.registry.entries() if entry.payload_format.value in self.required) != 164:
            raise ValueError("Required codec GCM budget changed")
