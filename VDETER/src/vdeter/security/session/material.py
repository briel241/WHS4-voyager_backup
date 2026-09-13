"""Immutable peer- and direction-specific material."""

from dataclasses import dataclass, field
from hashlib import sha256

from ...common.identifiers import StationId
from ..constants import KEY_BYTES, NONCE_PREFIX_BYTES


@dataclass(frozen=True, slots=True)
class DirectionalSessionMaterial:
    source_id: StationId
    destination_id: StationId
    key_epoch: int
    aes_key: bytes = field(repr=False)
    nonce_prefix: bytes = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.source_id, StationId)
                or not isinstance(self.destination_id, StationId)
                or self.source_id.value == 0 or self.destination_id.value == 0
                or self.source_id == self.destination_id or type(self.key_epoch) is not int
                or self.key_epoch != 1
                or type(self.aes_key) is not bytes or len(self.aes_key) != KEY_BYTES
                or type(self.nonce_prefix) is not bytes
                or len(self.nonce_prefix) != NONCE_PREFIX_BYTES):
            raise ValueError("Invalid directional security material")

    @property
    def direction(self) -> tuple[int, int, int]:
        return self.source_id.value, self.destination_id.value, self.key_epoch

    @property
    def material_id(self) -> bytes:
        inner = sha256(self.aes_key + self.nonce_prefix).digest()
        return sha256(
            b"VDS2-state" + self.source_id.value.to_bytes(4, "big")
            + self.destination_id.value.to_bytes(4, "big")
            + self.key_epoch.to_bytes(2, "big") + inner
        ).digest()
