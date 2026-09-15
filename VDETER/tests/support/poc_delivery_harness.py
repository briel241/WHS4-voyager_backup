"""Observation-only test wrapper; D-10 itself is the production shared function.

Records acceptance after the real Presentation/Service call returns. No fake
delivery, packet content, promotion replica or additional radio implementation.
"""
import hashlib
import json
import sys
from vdeter.runtime.stage5 import Stage5Endpoint
from tests.support.poc_device_harness import main


def run():
    original = Stage5Endpoint._deliver_presentation_bytes
    def record(endpoint, route, data, received_at):
        result = original(endpoint, route, data, received_at)
        print('[accepted-delivery] ' + json.dumps({
            'station': endpoint._dependencies.configuration.endpoint.station_id.value,
            'ok': result.is_ok,
            'payload_hashes': [hashlib.sha256(bytes(data)[k:]).hexdigest() for k in (0, 3, 4)],
        }), file=sys.stderr, flush=True)
        return result
    Stage5Endpoint._deliver_presentation_bytes = record
    try:
        return main()
    finally:
        Stage5Endpoint._deliver_presentation_bytes = original


if __name__ == '__main__':
    raise SystemExit(run())
