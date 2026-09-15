"""Strict externally-provisioned material bundle readers."""

import base64
import binascii
import json
import os
import hmac
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ...common.identifiers import StationId
from .material import DirectionalSessionMaterial


@dataclass(frozen=True, slots=True)
class MaterialBundle:
    endpoint_id: str
    local_station_id: StationId
    state_id: bytes = field(repr=False)
    generation_method: str = ""
    provisioning_id: str = ""
    directions: tuple[DirectionalSessionMaterial, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if (type(self.endpoint_id) is not str or not self.endpoint_id
                or not isinstance(self.local_station_id, StationId)
                or self.local_station_id.value == 0
                or type(self.state_id) is not bytes or len(self.state_id) != 16
                or self.generation_method != "CSPRNG-256"
                or type(self.provisioning_id) is not str or not self.provisioning_id
                or type(self.directions) is not tuple):
            raise ValueError("Invalid material bundle metadata")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate material JSON field")
        result[key] = value
    return result


def _fields(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise ValueError("Material JSON schema mismatch")


def _decode(value, length):
    if type(value) is not str:
        raise ValueError("Material field encoding invalid")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Material field encoding invalid") from exc
    if len(raw) != length or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Material field encoding invalid")
    return raw


def parse_material_bundle(data: bytes) -> MaterialBundle:
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
        _fields(record, {
            "schema_version", "endpoint_id", "local_station_id", "state_id_b64",
            "generation_method", "provisioning_id", "directions",
        })
        if type(record["schema_version"]) is not int or record["schema_version"] != 1 or type(record["endpoint_id"]) is not str or not record["endpoint_id"]:
            raise ValueError("Material metadata invalid")
        if record["generation_method"] != "CSPRNG-256" or type(record["provisioning_id"]) is not str or not record["provisioning_id"]:
            raise ValueError("Material provenance invalid")
        local_id = StationId(record["local_station_id"])
        state_id = _decode(record["state_id_b64"], 16)
        if type(record["directions"]) is not list or not record["directions"]:
            raise ValueError("Missing directions")
        materials = []
        for direction in record["directions"]:
            _fields(direction, {"source_id", "destination_id", "epoch", "key_b64", "nonce_prefix_b64"})
            materials.append(DirectionalSessionMaterial(
                StationId(direction["source_id"]), StationId(direction["destination_id"]),
                direction["epoch"], _decode(direction["key_b64"], 32),
                _decode(direction["nonce_prefix_b64"], 8),
            ))
        bundle = MaterialBundle(record["endpoint_id"], local_id, state_id,
                                record["generation_method"], record["provisioning_id"], tuple(materials))
        validate_endpoint_materials(bundle)
        return bundle
    except (UnicodeError, TypeError, KeyError, ValueError) as exc:
        raise ValueError("Material bundle invalid") from exc


def validate_endpoint_materials(bundle: MaterialBundle, *, expected_peers: frozenset[int] | None = None) -> None:
    seen_directions = set()
    seen_keys = set()
    peers = set()
    for material in bundle.directions:
        direction = material.direction
        if direction in seen_directions or material.aes_key in seen_keys:
            raise ValueError("Duplicate material direction or key")
        seen_directions.add(direction)
        seen_keys.add(material.aes_key)
        if bundle.local_station_id.value not in direction[:2]:
            raise ValueError("Material direction excludes local station")
        peers.add(direction[1] if direction[0] == bundle.local_station_id.value else direction[0])
    for peer in peers:
        if ((bundle.local_station_id.value, peer, 1) not in seen_directions
                or (peer, bundle.local_station_id.value, 1) not in seen_directions):
            raise ValueError("Both peer directions are required")
    if expected_peers is not None and peers != expected_peers:
        raise ValueError("Peer inventory mismatch")


def validate_reciprocal_bundle(bundles: tuple[MaterialBundle, ...]) -> None:
    by_direction = {}
    all_keys = {}
    for bundle in bundles:
        validate_endpoint_materials(bundle)
        for material in bundle.directions:
            direction = material.direction
            previous = by_direction.setdefault(direction, [])
            previous.append((bundle.local_station_id.value, material))
            other = all_keys.setdefault(material.aes_key, direction)
            if other != direction:
                raise ValueError("Key reused across logical directions")
    for direction, copies in by_direction.items():
        if len(copies) != 2 or {item[0] for item in copies} != set(direction[:2]):
            raise ValueError("Missing reciprocal TX/RX material")
        if (not hmac.compare_digest(copies[0][1].aes_key, copies[1][1].aes_key)
                or not hmac.compare_digest(copies[0][1].nonce_prefix, copies[1][1].nonce_prefix)):
            raise ValueError("Reciprocal material mismatch")


class SecurityMaterialProvider(Protocol):
    def load_for_endpoint(self, *, endpoint_id: str, local_station_id: StationId) -> MaterialBundle: ...


class InMemorySecurityMaterialProvider:
    def __init__(self, bundle: MaterialBundle):
        validate_endpoint_materials(bundle)
        self._bundle = bundle

    def load_for_endpoint(self, *, endpoint_id: str, local_station_id: StationId) -> MaterialBundle:
        if self._bundle.endpoint_id != endpoint_id or self._bundle.local_station_id != local_station_id:
            raise ValueError("Material endpoint mismatch")
        return self._bundle


class ProtectedFileSecurityMaterialProvider:
    def __init__(self, path: Path):
        self._path = Path(path)

    def load_for_endpoint(self, *, endpoint_id: str, local_station_id: StationId) -> MaterialBundle:
        if self._path.is_symlink() or self._path.parent.is_symlink():
            raise ValueError("Material symlink forbidden")
        stat = self._path.stat()
        if not self._path.is_file() or (os.name == "posix" and (stat.st_mode & 0o777) != 0o600):
            raise ValueError("Material file permission/type invalid")
        if os.name == "posix" and (stat.st_uid != os.geteuid() or (self._path.parent.stat().st_mode & 0o777) != 0o700):
            raise ValueError("Material owner/directory permission invalid")
        bundle = parse_material_bundle(self._path.read_bytes())
        if bundle.endpoint_id != endpoint_id or bundle.local_station_id != local_station_id:
            raise ValueError("Material endpoint mismatch")
        return bundle
