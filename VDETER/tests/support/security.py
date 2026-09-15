"""Ephemeral reciprocal VDS2 material for TEST-only endpoints."""

import os
import tempfile
from pathlib import Path

from vdeter.common.identifiers import StationId
from vdeter.security.dependencies import E2eSecurityDependencies
from vdeter.security.session.manager import SecuritySessionManager
from vdeter.security.session.material import DirectionalSessionMaterial
from vdeter.security.session.provider import MaterialBundle
from vdeter.security.session.state_store import SQLiteSecurityStateStore


def make_test_e2e_by_station(endpoint_by_station: dict[int, str]):
    """Return independent endpoint stores; caller must call close_final on each dependency."""
    ids = sorted(endpoint_by_station)
    if len(ids) != 3:
        raise ValueError("The software fixture requires two ships and ROC")
    control = next(value for value, name in endpoint_by_station.items() if "roc" in name.lower())
    materials = {station: [] for station in ids}
    for peer in (station for station in ids if station != control):
        for source, destination in ((peer, control), (control, peer)):
            material = DirectionalSessionMaterial(
                StationId(source), StationId(destination), 1, os.urandom(32), os.urandom(8),
            )
            materials[source].append(material)
            materials[destination].append(material)
    guard = tempfile.TemporaryDirectory(prefix="vdeter-vds2-test-")
    result = {}
    for station, endpoint_id in endpoint_by_station.items():
        directory = Path(guard.name) / endpoint_id
        directory.mkdir(mode=0o700)
        bundle = MaterialBundle(endpoint_id, StationId(station), os.urandom(16),
                                "CSPRNG-256", "test-only", tuple(materials[station]))
        path = directory / "state.sqlite3"
        SQLiteSecurityStateStore.initialize(path, bundle)
        store = SQLiteSecurityStateStore(path, bundle)
        manager = SecuritySessionManager(StationId(station), bundle.directions, store)
        dependency = E2eSecurityDependencies.enforced(manager)
        dependency.presentation_security._temp_guard = guard
        result[station] = dependency
    return result
