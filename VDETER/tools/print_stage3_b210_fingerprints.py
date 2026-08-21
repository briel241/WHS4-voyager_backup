"""Print the two canonical Stage 3 B210 radio fingerprints without opening RF."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for path in (ROOT, SRC):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.config.loader import load_configuration_file  # noqa: E402
from vdeter.config.models import radio_fingerprint_of  # noqa: E402


def _load(name, serial):
    result = load_configuration_file(os.path.join(ROOT, "configs", name))
    if result.is_err:
        raise RuntimeError(f"failed to load {name}: {result.error}")
    configuration = dataclasses.replace(
        result.value.radio, device_selector=f"serial={serial}"
    )
    return configuration, radio_fingerprint_of(configuration, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ship-serial", required=True)
    parser.add_argument("--control-serial", required=True)
    args = parser.parse_args()
    if args.ship_serial == args.control_serial:
        parser.error("ship and control serials must differ")
    ship, ship_fingerprint = _load("stage3_b210_ship.toml", args.ship_serial)
    control, control_fingerprint = _load(
        "stage3_b210_control.toml", args.control_serial
    )
    print(json.dumps({
        "ship": {
            "serial": args.ship_serial,
            "device_selector": ship.device_selector,
            "radio_fingerprint": ship_fingerprint,
        },
        "control": {
            "serial": args.control_serial,
            "device_selector": control.device_selector,
            "radio_fingerprint": control_fingerprint,
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
