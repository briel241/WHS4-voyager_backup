"""Stage 7 host topology profiles — the single definition (additive, 2026-09-10).

Every consumer of "which USB topology binds the three B210s" derives its
expectation from this module instead of restating the rule:

    apps/standalone/hardware_profiles.py   hardware.toml schema/draft/critical subset
    apps/standalone/local_preflight.py     speed class per role + shared-hub check
    apps/standalone/local_config.py        endpoint template directory + provisioning
    vdeter.b210_system.configuration            per-endpoint usb_transport / power rule
    VDETER/apps/stage7_coordinator_main.py RR-P0 live link-speed / hub topology check

Two profiles exist. The legacy Pi profile is the qualified baseline and its
values are reproduced here **verbatim** (MASS-A/ROC direct USB3 5000M on Pi
bus power, MASS-B direct USB2 480M with a real external DC asset); nothing
about it is relaxed. The powered-hub profile describes a Raspberry Pi 5 whose
three B210s all sit on the downstream ports of ONE self-powered USB3 hub whose
uplink is a Pi SuperSpeed root port: every link is 5000M, the power source is
the hub's bus power and no per-B210 DC asset exists.

The USB descriptor `self-powered` bit (bmAttributes bit 6) proves only what
the hub reports about itself; whether the hub's external supply is actually
connected and whether a per-B210 DC brick is absent are operator attestations
recorded separately in hardware.toml (source `operator_attested`). Both are
required: a hub observed as bus-powered is refused even with an attestation,
and an attestation-less declaration is refused even when the descriptor bit is
set.

This module reads sysfs only (no Git, no network, no device open).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LEGACY_PI_PROFILE = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"
PI_POWERED_HUB_PROFILE = "STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1"
HOST_PROFILES = (LEGACY_PI_PROFILE, PI_POWERED_HUB_PROFILE)
DEFAULT_HOST_PROFILE = LEGACY_PI_PROFILE

ENDPOINT_ROLES = ("MASS-A", "MASS-B", "ROC")

#: resolved config `host_execution.usb_transport` canonical values.
TRANSPORT_DIRECT_USB3_5000M = "DIRECT_USB3_5000M"
TRANSPORT_DIRECT_USB2_480M_EXTERNAL_DC = "DIRECT_USB2_480M_EXTERNAL_DC"
TRANSPORT_POWERED_USB3_HUB_5000M = "POWERED_USB3_HUB_5000M"
USB_TRANSPORTS = (
    TRANSPORT_DIRECT_USB3_5000M,
    TRANSPORT_DIRECT_USB2_480M_EXTERNAL_DC,
    TRANSPORT_POWERED_USB3_HUB_5000M,
)
#: approved USB link speed per transport (sysfs `speed` value, Mbps).
TRANSPORT_SPEED_MBPS = {
    TRANSPORT_DIRECT_USB3_5000M: 5000,
    TRANSPORT_DIRECT_USB2_480M_EXTERNAL_DC: 480,
    TRANSPORT_POWERED_USB3_HUB_5000M: 5000,
}

#: resolved config `host_execution.external_power_asset` rule tokens.
POWER_PI_BUS = "PI_BUS_POWER"                        # literal value required
POWER_EXTERNAL_DC_ASSET = "EXTERNAL_DC_ASSET"        # rule: a real DC asset ID (never a token)
POWER_POWERED_USB3_HUB_BUS = "POWERED_USB3_HUB_BUS_POWER"   # literal value required
#: values that are never an acceptable external DC asset ID (legacy MASS-B).
NOT_A_DC_ASSET = ("PI_BUS_POWER", "none", "REQUIRED_UNSET", POWER_POWERED_USB3_HUB_BUS)

SPEED_CLASS_SUPER = "super"
SPEED_CLASS_HIGH = "high"

#: Ettus USRP B210 USB VID:PID and the USB hub device class.
B210_VID_PID = "2500:0020"
USB_HUB_DEVICE_CLASS = "09"
OPERATOR_ATTESTED = "operator_attested"


def speed_class(speed_mbps) -> str:
    """sysfs `speed` → class. super = USB3 (5000+), high = USB2 (480)."""
    try:
        value = int(speed_mbps)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 5000:
        return SPEED_CLASS_SUPER
    if value == 480:
        return SPEED_CLASS_HIGH
    return "other"


@dataclass(frozen=True)
class HostProfile:
    """One approved host topology (immutable definition)."""

    name: str
    platform_profiles: tuple
    endpoint_template_subdir: str
    usb_transport: dict
    power_source: dict
    shared_powered_hub: bool

    def speed_mbps(self, role: str) -> int:
        return TRANSPORT_SPEED_MBPS[self.usb_transport[role]]

    def speed_class(self, role: str) -> str:
        return speed_class(self.speed_mbps(role))

    def expected_sysfs_speed(self, role: str) -> str:
        return str(self.speed_mbps(role))

    @property
    def speed_classes(self) -> dict:
        return {role: self.speed_class(role) for role in ENDPOINT_ROLES}


LEGACY_PI = HostProfile(
    name=LEGACY_PI_PROFILE,
    platform_profiles=("pi", "generic-pc"),
    endpoint_template_subdir="endpoints",
    usb_transport={
        "MASS-A": TRANSPORT_DIRECT_USB3_5000M,
        "MASS-B": TRANSPORT_DIRECT_USB2_480M_EXTERNAL_DC,
        "ROC": TRANSPORT_DIRECT_USB3_5000M,
    },
    power_source={
        "MASS-A": POWER_PI_BUS,
        "MASS-B": POWER_EXTERNAL_DC_ASSET,
        "ROC": POWER_PI_BUS,
    },
    shared_powered_hub=False,
)

PI_POWERED_HUB = HostProfile(
    name=PI_POWERED_HUB_PROFILE,
    platform_profiles=("pi",),
    endpoint_template_subdir="endpoints-pi-powered-usb3-hub",
    usb_transport={role: TRANSPORT_POWERED_USB3_HUB_5000M for role in ENDPOINT_ROLES},
    power_source={role: POWER_POWERED_USB3_HUB_BUS for role in ENDPOINT_ROLES},
    shared_powered_hub=True,
)

PROFILES = {LEGACY_PI.name: LEGACY_PI, PI_POWERED_HUB.name: PI_POWERED_HUB}


def profile(name: str) -> HostProfile:
    """Definition by name; unknown names are a hard error (fail-closed)."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown host profile: {name!r}") from None


def expected_sysfs_speed(usb_transport: str) -> str:
    """resolved `usb_transport` → approved sysfs link speed string ("5000"/"480")."""
    try:
        return str(TRANSPORT_SPEED_MBPS[usb_transport])
    except KeyError:
        raise ValueError(f"unknown usb_transport: {usb_transport!r}") from None


# --- sysfs physical topology ---------------------------------------------------


def _read(path: Path):
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parent_of(name: str) -> str:
    """sysfs device name → parent name: '5-1.2' → '5-1', '5-1' → 'usb5'."""
    if "." in name:
        return name.rsplit(".", 1)[0]
    return "usb" + name.split("-", 1)[0]


def port_of(name: str):
    tail = name.rsplit(".", 1)[-1] if "." in name else name.split("-", 1)[-1]
    return _int_or_none(tail)


def probe_usb_topology(sysfs_root: str = "/sys/bus/usb/devices") -> dict:
    """Physical topology snapshot: {"devices": {name: {...}}, "roots": {usbN: {...}}}.

    Device numbers are recorded for diagnosis only and are never bound.
    Interface entries ('5-1.2:1.0') are skipped."""
    root = Path(sysfs_root)
    devices, roots = {}, {}
    if not root.is_dir():
        return {"devices": devices, "roots": roots, "sysfs_root": sysfs_root}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        name = entry.name
        if ":" in name:
            continue
        if name.startswith("usb"):
            roots[name] = {"speed_mbps": _int_or_none(_read(entry / "speed")),
                           "busnum": _int_or_none(_read(entry / "busnum"))}
            continue
        vid = _read(entry / "idVendor")
        pid = _read(entry / "idProduct")
        if vid is None or pid is None:
            continue
        attributes = _read(entry / "bmAttributes")
        self_powered = None
        if attributes is not None:
            try:
                self_powered = bool(int(attributes, 16) & 0x40)
            except ValueError:
                self_powered = None
        devices[name] = {
            "vid_pid": f"{vid.lower()}:{pid.lower()}",
            "speed_mbps": _int_or_none(_read(entry / "speed")),
            "serial": _read(entry / "serial"),
            "device_class": (_read(entry / "bDeviceClass") or "").lower() or None,
            "product": _read(entry / "product"),
            "manufacturer": _read(entry / "manufacturer"),
            "maxchild": _int_or_none(_read(entry / "maxchild")),
            "devnum": _int_or_none(_read(entry / "devnum")),
            "busnum": _int_or_none(_read(entry / "busnum")),
            "bm_attributes": attributes,
            "descriptor_self_powered": self_powered,
            "parent": parent_of(name),
            "port": port_of(name),
        }
    return {"devices": devices, "roots": roots, "sysfs_root": sysfs_root}


def check_shared_powered_hub(serial_by_role: dict, topology: dict, *,
                             hub_vid_pid: str | None = None) -> dict:
    """PI_POWERED_HUB contract against a sysfs snapshot — fail-closed.

    Passes only when: every declared serial is exactly one B210 at a 5000M
    link; all three share one parent; that parent is a USB hub (class 09) on a
    5000M link whose own parent is a root controller; the hub descriptor
    reports self-powered; the hub VID:PID equals the approved one (when
    given); and no other B210 is present anywhere. Paths, ports and device
    numbers are recorded, not bound."""
    devices = (topology or {}).get("devices") or {}
    roots = (topology or {}).get("roots") or {}
    errors = []
    b210 = {name: d for name, d in devices.items() if d.get("vid_pid") == B210_VID_PID}
    by_serial = {}
    for name, d in b210.items():
        by_serial.setdefault(d.get("serial"), []).append(name)

    observed_by_role = {}
    for role, serial in sorted((serial_by_role or {}).items()):
        names = by_serial.get(serial) or []
        if not names:
            errors.append(f"{role}: B210 serial {serial} is not visible on USB — "
                          "check the hub cable and the hub power")
            observed_by_role[role] = None
            continue
        if len(names) > 1:
            errors.append(f"{role}: serial {serial} appears at more than one USB path {names}")
        name = names[0]
        d = devices[name]
        record = {"serial": serial, "usb_path": name, "parent": d["parent"],
                  "hub_port": d["port"], "speed_mbps": d["speed_mbps"],
                  "speed_class": speed_class(d["speed_mbps"]), "devnum": d["devnum"]}
        observed_by_role[role] = record
        if record["speed_class"] != SPEED_CLASS_SUPER:
            errors.append(
                f"{role}: B210 {serial} at {name} is {d['speed_mbps']}M — every endpoint must be "
                f"a SuperSpeed (5000M) link under {PI_POWERED_HUB_PROFILE}; a USB2/480M "
                "fallback is refused (reseat the cable/port and retry)")

    parents = sorted({r["parent"] for r in observed_by_role.values() if r})
    hub_report = {"usb_path": None, "observed": False}
    if len(parents) > 1:
        errors.append("the three B210s are not below one common USB hub: "
                      + ", ".join(f"{role} {r['usb_path']} (parent {r['parent']})"
                                  for role, r in sorted(observed_by_role.items()) if r))
    elif len(parents) == 1:
        hub_name = parents[0]
        hub = devices.get(hub_name)
        hub_report["usb_path"] = hub_name
        if hub is None:
            errors.append(f"the B210s hang directly below root controller {hub_name} — they "
                          "must be below one self-powered USB3 hub")
        else:
            hub_report.update({
                "observed": True, "vid_pid": hub["vid_pid"], "speed_mbps": hub["speed_mbps"],
                "product": hub["product"], "manufacturer": hub["manufacturer"],
                "device_class": hub["device_class"], "maxchild": hub["maxchild"],
                "parent": hub["parent"], "devnum": hub["devnum"],
                "bm_attributes": hub["bm_attributes"],
                "descriptor_self_powered": hub["descriptor_self_powered"],
            })
            if hub["device_class"] != USB_HUB_DEVICE_CLASS:
                errors.append(f"device {hub_name} above the B210s is not a USB hub "
                              f"(class {hub['device_class']})")
            if speed_class(hub["speed_mbps"]) != SPEED_CLASS_SUPER:
                errors.append(f"hub {hub_name} link is {hub['speed_mbps']}M, not SuperSpeed "
                              "(5000M) — the hub uplink must be on a Pi USB3 port")
            if hub["parent"] not in roots:
                errors.append(f"hub {hub_name} is cascaded below {hub['parent']} — the "
                              "powered hub must be attached directly to a Pi root port")
            if hub["descriptor_self_powered"] is not True:
                errors.append(f"hub {hub_name} USB descriptor does not report self-powered "
                              f"(bmAttributes {hub['bm_attributes']}) — a bus-powered hub "
                              "is refused")
            if hub_vid_pid is not None and hub["vid_pid"] != hub_vid_pid:
                errors.append(f"hub {hub_name} is {hub['vid_pid']} but the approved hub is "
                              f"{hub_vid_pid} (hardware.toml usb_hub.vid_pid)")

    declared_paths = {r["usb_path"] for r in observed_by_role.values() if r}
    extra = sorted(n for n in b210 if n not in declared_paths)
    if extra:
        errors.append("unexpected B210 outside the declared roles: " + ", ".join(
            f"{n} (serial {b210[n]['serial']}, parent {b210[n]['parent']}, "
            f"{b210[n]['speed_mbps']}M)" for n in extra)
            + " — exactly the three declared B210s are allowed")
    other_hubs = [
        {"usb_path": n, "vid_pid": d["vid_pid"], "speed_mbps": d["speed_mbps"],
         "product": d["product"], "parent": d["parent"]}
        for n, d in sorted(devices.items())
        if d.get("device_class") == USB_HUB_DEVICE_CLASS and n != hub_report["usb_path"]]
    return {
        "pass": not errors,
        "errors": errors,
        "host_profile": PI_POWERED_HUB_PROFILE,
        "hub": hub_report,
        "observed_by_role": observed_by_role,
        "b210_devices": {n: {"serial": d["serial"], "speed_mbps": d["speed_mbps"],
                             "parent": d["parent"], "devnum": d["devnum"]}
                         for n, d in sorted(b210.items())},
        "other_hubs": other_hubs,
    }


__all__ = (
    "B210_VID_PID",
    "DEFAULT_HOST_PROFILE",
    "ENDPOINT_ROLES",
    "HOST_PROFILES",
    "HostProfile",
    "LEGACY_PI",
    "LEGACY_PI_PROFILE",
    "NOT_A_DC_ASSET",
    "OPERATOR_ATTESTED",
    "PI_POWERED_HUB",
    "PI_POWERED_HUB_PROFILE",
    "POWER_EXTERNAL_DC_ASSET",
    "POWER_PI_BUS",
    "POWER_POWERED_USB3_HUB_BUS",
    "PROFILES",
    "SPEED_CLASS_HIGH",
    "SPEED_CLASS_SUPER",
    "TRANSPORT_DIRECT_USB2_480M_EXTERNAL_DC",
    "TRANSPORT_DIRECT_USB3_5000M",
    "TRANSPORT_POWERED_USB3_HUB_5000M",
    "TRANSPORT_SPEED_MBPS",
    "USB_HUB_DEVICE_CLASS",
    "USB_TRANSPORTS",
    "check_shared_powered_hub",
    "expected_sysfs_speed",
    "parent_of",
    "port_of",
    "probe_usb_topology",
    "profile",
    "speed_class",
)
