"""Exact seven-byte VDS2 Security Header."""

from dataclasses import dataclass
from struct import Struct

from ...common.errors import ErrorCode
from ..constants import HEADER_BYTES, MAX_COUNTER, SECURITY_CONTROL
from ..errors import SecurityFault

_WIRE = Struct(">BHI")
assert _WIRE.size == HEADER_BYTES


@dataclass(frozen=True, slots=True)
class SecurityHeader:
    key_epoch: int
    message_counter: int

    def __post_init__(self):
        if type(self.key_epoch) is not int or self.key_epoch != 1:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_EPOCH_MISMATCH)
        if type(self.message_counter) is not int or not 1 <= self.message_counter <= MAX_COUNTER:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID)

    def to_bytes(self) -> bytes:
        return _WIRE.pack(SECURITY_CONTROL, self.key_epoch, self.message_counter)

    @classmethod
    def parse(cls, data: bytes) -> "SecurityHeader":
        if type(data) is not bytes or len(data) != HEADER_BYTES:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED)
        control, epoch, counter = _WIRE.unpack(data)
        if control != SECURITY_CONTROL:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED)
        return cls(epoch, counter)
