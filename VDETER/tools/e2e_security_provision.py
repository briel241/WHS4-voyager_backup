"""Validate externally shared VDS2 materials and install fresh endpoint state."""

from __future__ import annotations

import argparse
import os
import sys
import json
import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vdeter.security.session.provider import (  # noqa: E402
    ProtectedFileSecurityMaterialProvider, validate_reciprocal_bundle,
)
from vdeter.security.session.state_store import SQLiteSecurityStateStore  # noqa: E402
from vdeter.common.identifiers import StationId  # noqa: E402


def _load_bundle(source: Path):
    bundles = []
    for endpoint_id, station_id in (("MASS-A", 111111111), ("MASS-B", 333333333), ("ROC", 222222222)):
        bundle = ProtectedFileSecurityMaterialProvider(source / f"{endpoint_id}.json").load_for_endpoint(
            endpoint_id=endpoint_id, local_station_id=StationId(station_id),
        )
        expected_peers = frozenset((222222222,)) if endpoint_id != "ROC" else frozenset((111111111, 333333333))
        from vdeter.security.session.provider import validate_endpoint_materials
        validate_endpoint_materials(bundle, expected_peers=expected_peers)
        bundles.append(bundle)
    validate_reciprocal_bundle(tuple(bundles))
    if len({bundle.state_id for bundle in bundles}) != len(bundles):
        raise ValueError("Endpoint state IDs must be unique")
    if len({bundle.provisioning_id for bundle in bundles}) != 1:
        raise ValueError("Provisioning IDs must match")
    return tuple(bundles)


def main(argv=None):
    if os.name == "posix":
        os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate-test-bundle",
                              help="Generate fresh CSPRNG test materials; not a key distribution protocol")
    generate.add_argument("--bundle-dir", type=Path, required=True)
    for command in ("validate-bundle", "initialize-state"):
        item = sub.add_parser(command)
        item.add_argument("--bundle-dir", type=Path, required=True)
        if command == "initialize-state":
            item.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-test-bundle":
            target = args.bundle_dir
            if target.resolve().is_relative_to(ROOT.parent.resolve()) or target.exists():
                raise ValueError("Fresh bundle directory must be outside repository")
            target.mkdir(mode=0o700)
            identities = {"MASS-A": 111111111, "MASS-B": 333333333, "ROC": 222222222}
            provisioning_id = secrets.token_hex(16)
            encode = lambda value: base64.b64encode(value).decode("ascii")
            directions = []
            # Each logical direction has a distinct key. Reciprocal endpoints get
            # the exact same material. No nonce/key is printed or saved to results.
            for mass in (111111111, 333333333):
                for source, destination in ((mass, 222222222), (222222222, mass)):
                    directions.append(dict(source_id=source, destination_id=destination,
                                           epoch=1, key_b64=encode(secrets.token_bytes(32)),
                                           nonce_prefix_b64=encode(secrets.token_bytes(8))))
            for endpoint_id, station in identities.items():
                record = dict(schema_version=1, endpoint_id=endpoint_id,
                              local_station_id=station, state_id_b64=encode(secrets.token_bytes(16)),
                              generation_method="CSPRNG-256", provisioning_id=provisioning_id,
                              directions=[d for d in directions if station in (d["source_id"], d["destination_id"])])
                fd = os.open(target / f"{endpoint_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            _load_bundle(target)
            print("generate-test-bundle: 3 endpoints; protected files created")
            return 0
        bundles = _load_bundle(args.bundle_dir)
        if args.command == "initialize-state":
            root = args.state_root
            if root.resolve().is_relative_to(ROOT.parent.resolve()):
                raise ValueError("Security state must be outside the repository")
            if not root.is_dir() or any((root / bundle.endpoint_id).exists() for bundle in bundles):
                raise ValueError("State root missing or endpoint installation already exists")
            # Consume the original bundle before installing a zero-counter DB.
            # Partial installation/crash requires fresh keys rather than a reset.
            marker = args.bundle_dir / ".state-initialization-used"
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"state_root": str(root.resolve()), "status": "consumed-before-install"}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            for bundle in bundles:
                target = root / bundle.endpoint_id
                target.mkdir(mode=0o700)
                source = args.bundle_dir / f"{bundle.endpoint_id}.json"
                destination = target / "materials.json"
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
                SQLiteSecurityStateStore.initialize(target / "state.sqlite3", bundle)
        print(f"{args.command}: {len(bundles)} endpoints, {sum(len(b.directions) for b in bundles)} directions")
        return 0
    except Exception as exc:
        print(f"Provisioning failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
