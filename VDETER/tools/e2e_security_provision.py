"""Validate externally shared VDS2 materials and install fresh endpoint state."""

from __future__ import annotations

import argparse
import os
import sys
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
    for command in ("validate-bundle", "initialize-state"):
        item = sub.add_parser(command)
        item.add_argument("--bundle-dir", type=Path, required=True)
        if command == "initialize-state":
            item.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bundles = _load_bundle(args.bundle_dir)
        if args.command == "initialize-state":
            root = args.state_root
            if root.resolve().is_relative_to(ROOT.parent.resolve()):
                raise ValueError("Security state must be outside the repository")
            if not root.is_dir() or any((root / bundle.endpoint_id).exists() for bundle in bundles):
                raise ValueError("State root missing or endpoint installation already exists")
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
