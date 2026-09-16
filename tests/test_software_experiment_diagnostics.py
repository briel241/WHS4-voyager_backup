"""Software trace projection is bounded, run-scoped and contains no raw payload."""
from pathlib import Path
import json
import hashlib
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'VDETER' / 'src'))
sys.path.insert(0, str(ROOT))
from apps.mass_roc_software import SoftwareMassRocHarness, _DrivenEventLog
from vdeter.common.experiment import ExperimentBuffer
from vdeter.common.events import InternalEvent, InternalEventType, EventSeverity, ServiceEvent, ServiceEventType
from vdeter.common.errors import Layer
from vdeter.common.time import TimePoint, ClockDomain
from vdeter.common.contracts.phy import RecoveredPacketSet, PhyRecoveryStatus
from vdeter.common.identifiers import PhysicalChannel


@pytest.mark.parametrize('limit,expected_drop', [(2, 3), (10, 0)])
def test_software_trace_retains_poll_service_and_internal_metadata(tmp_path, limit, expected_drop):
    class Clock:
        value = 0
        def now_monotonic(self):
            return TimePoint.from_nanoseconds(self.value, ClockDomain.MONOTONIC, 'test-simulated')
    harness = SoftwareMassRocHarness.__new__(SoftwareMassRocHarness)
    harness._clock = Clock()
    harness.diagnostics = ExperimentBuffer(limit=limit)
    harness.experiment_buffers = {}
    harness.events_by_endpoint = {'ROC': [], 'MASS-A': [], 'MASS-B': []}
    timepoint = harness._clock.now_monotonic()
    internal = InternalEvent('internal-1', InternalEventType.PACKETS_RECOVERED,
                             timepoint, EventSeverity.INFO, Layer.PHY,
                             payload=RecoveredPacketSet(PhysicalChannel('upper'), timepoint,
                                                        PhyRecoveryStatus.NO_BURST_DETECTED))
    # Minimal metadata-only sink entries for the two packet-boundary projections.
    packet = b'test-packet-bytes'
    built = SimpleNamespace(event_type=InternalEventType.PACKET_BUILT,
        correlation=internal.correlation, event_id='built', occurred_at=timepoint,
        error=None, payload=SimpleNamespace(packet=packet))
    decoded = SimpleNamespace(event_type=InternalEventType.PACKET_DECODE_REQUESTED,
        correlation=internal.correlation, event_id='decoded', occurred_at=timepoint,
        error=None, payload=SimpleNamespace(packet=SimpleNamespace(packet=packet)))
    endpoint = SimpleNamespace(dependencies=SimpleNamespace(
        configuration=SimpleNamespace(endpoint=SimpleNamespace(endpoint_id='stage5-roc')),
        internal_event_sink=SimpleNamespace(events=lambda: [internal, built, decoded])))
    harness._group = SimpleNamespace(endpoints=[endpoint])
    def drive(clock, group, steps):
        clock.value += steps
        harness.events_by_endpoint['ROC'].append(ServiceEvent(
            'service-1', ServiceEventType.SERVICE_STARTED, clock.now_monotonic(), EventSeverity.INFO))
    harness._drive_fn = drive
    events = _DrivenEventLog(harness)
    events.poll()
    assert harness._clock.value == 8
    assert len(events.records['ROC']) == 1
    harness.write_experiment_telemetry(tmp_path, run_id='one-run')
    rows = [json.loads(line) for file in (tmp_path / 'evidence/security_compare').glob('*.jsonl')
            for line in file.read_text(encoding='utf-8').splitlines()]
    assert all(row['run_id'] == 'one-run' for row in rows)
    poll = next(row for row in rows if row['stage'] == 'software_poll')
    assert poll['simulated_before_ns'] == 0 and poll['simulated_after_ns'] == 8
    assert poll['wall_elapsed_ns'] >= 0 and poll['record_counts']['ROC'] == 1
    assert rows[-1]['dropped'] == expected_drop
    if expected_drop == 0:
        recovered = next(row for row in rows if row['stage'] == 'software_internal_event')
        assert recovered['recovered_packet_count'] == 0
        assert recovered['recovery_status'] == 'NO_BURST_DETECTED'
        fingerprints = [row['packet_sha256'] for row in rows if 'packet_sha256' in row]
        assert fingerprints == [hashlib.sha256(packet).hexdigest()] * 2
    assert not any(key in row for row in rows
                   for key in ('payload', 'payload_base64', 'key', 'nonce', 'header', 'counter'))
