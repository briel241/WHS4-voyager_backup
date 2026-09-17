"""standalone hardware declaration ↔ host topology profile (additive, 2026-09-10).

hardware.toml carries only the operator-approved environment intent. Its
schema selects the host profile whose rules are defined **once** in
`vdeter.b210_system.host_profiles` (speed class per role, canonical usb_transport,
power source, shared-hub contract):

  schema_version = 1   STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1 (legacy Pi profile)
                       — read exactly as before: MASS-A/ROC super, MASS-B high
                       (480M) with a real `external_power_asset`. Files that also
                       carry `hardware_profile` / `[usb_hub]` are contradictory
                       and refused.
  schema_version = 2   `hardware_profile` is mandatory and must be
                       STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1: one self-powered
                       USB3 hub (`[usb_hub]` asset ID, VID:PID, two operator
                       attestations with their source), three serials, wiring.
                       No per-endpoint power asset, no USB path (paths, ports
                       and device numbers are observed and recorded, never
                       declared or bound). Unknown or missing keys are refused.

What software can and cannot prove is kept apart: the hub descriptor
`self-powered` bit, link speeds and the shared-parent topology are sysfs
observations (preflight/RR-P0); "the external supply is connected" and "no
per-B210 DC brick is used" are operator attestations recorded with their
source. Both must hold. Nothing here reads Git or the network.

The setup-time draft (hub candidate detection and rendering) lives in
`apps.standalone.hardware_draft`; this module keeps the schema, its validation
and the qualification-critical subset.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (_REPO_ROOT / "VDETER", _REPO_ROOT / "VDETER" / "src"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from vdeter.b210_system import host_profiles  # noqa: E402

LEGACY_PI_PROFILE = host_profiles.LEGACY_PI_PROFILE
PI_POWERED_HUB_PROFILE = host_profiles.PI_POWERED_HUB_PROFILE
HARDWARE_PROFILES = host_profiles.HOST_PROFILES
ROLES = host_profiles.ENDPOINT_ROLES
OPERATOR_ATTESTED = host_profiles.OPERATOR_ATTESTED
POWERED_USB3_HUB_BUS_POWER = host_profiles.POWER_POWERED_USB3_HUB_BUS

SCHEMA_LEGACY = 1
SCHEMA_PI_POWERED_HUB = 2
SCHEMA_VERSIONS = (SCHEMA_LEGACY, SCHEMA_PI_POWERED_HUB)
DRAFT = "REQUIRED_FILL"

_TOP_KEYS_V2 = frozenset({"schema_version", "platform_profile", "hardware_profile",
                          "usb_hub", "endpoints", "wiring"})
_HUB_KEYS = ("asset_id", "vid_pid", "self_powered", "self_powered_source",
             "individual_b210_dc", "individual_b210_dc_source")
_ENDPOINT_KEYS_V2 = frozenset({"serial"})
_WIRING_KEYS = frozenset({"profile", "approved"})
_VID_PID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
#: schema 1 (legacy) must not carry the powered-hub keys — contradictory.
_V2_ONLY_TOP_KEYS = ("hardware_profile", "usb_hub")


class HardwareProfileError(ValueError):
    """Declaration/observation contract violation — always before RF access."""


# ------------------------------------------------------------ profile selection

def profile_of(declaration: dict) -> str:
    """Host profile named by a parsed hardware.toml (schema 1 → legacy Pi)."""
    if (declaration or {}).get("schema_version") == SCHEMA_PI_POWERED_HUB:
        return (declaration or {}).get("hardware_profile")
    return LEGACY_PI_PROFILE


def definition_of(declaration: dict) -> host_profiles.HostProfile:
    """The single profile definition for a declaration (unknown = ValueError)."""
    return host_profiles.profile(profile_of(declaration))


def is_pi_powered_hub(declaration: dict) -> bool:
    return profile_of(declaration) == PI_POWERED_HUB_PROFILE


# ------------------------------------------------------------ declaration checks

def validate_legacy_declaration(declaration: dict) -> list:
    """schema_version 1 contradiction guard — powered-hub keys are refused."""
    errors = []
    for key in _V2_ONLY_TOP_KEYS:
        if key in (declaration or {}):
            errors.append(f"{key} is not a schema_version 1 (legacy Pi profile) field — "
                          "the Pi powered-USB3-hub profile is declared with schema_version = 2")
    return errors


def validate_pi_powered_hub_declaration(declaration: dict) -> list:
    """schema_version 2 / STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1 — error strings.

    Serial presence/uniqueness and wiring approval are the caller's common
    checks; this validates the profile-specific contract (closed key sets,
    hub asset/VID:PID, both attestations with source, no per-endpoint power
    asset, no declared USB paths)."""
    errors = []
    declaration = declaration or {}
    unknown = sorted(set(declaration) - _TOP_KEYS_V2)
    if unknown:
        errors.append(f"unknown top-level field(s) for schema_version 2: {unknown}")
    profile_name = declaration.get("hardware_profile")
    if profile_name != PI_POWERED_HUB_PROFILE:
        errors.append(f"hardware_profile must be '{PI_POWERED_HUB_PROFILE}' for "
                      f"schema_version 2 (declared {profile_name!r})")
        return errors
    definition = host_profiles.profile(profile_name)
    platform_profile = declaration.get("platform_profile")
    if platform_profile not in definition.platform_profiles:
        errors.append(f"platform_profile must be one of {list(definition.platform_profiles)} "
                      f"for {profile_name} (declared {platform_profile!r})")

    hub = declaration.get("usb_hub")
    if not isinstance(hub, dict):
        errors.append("[usb_hub] table is missing (asset_id, vid_pid, self_powered, "
                      "self_powered_source, individual_b210_dc, individual_b210_dc_source)")
        hub = {}
    unknown = sorted(set(hub) - set(_HUB_KEYS))
    if unknown:
        errors.append(f"unknown usb_hub field(s): {unknown}")
    for key in _HUB_KEYS:
        if key not in hub:
            errors.append(f"usb_hub.{key} is missing")
    asset = hub.get("asset_id")
    if "asset_id" in hub and (not isinstance(asset, str) or not asset.strip()
                              or asset == DRAFT):
        errors.append("usb_hub.asset_id is still the draft value — record the physical "
                      "asset ID of the self-powered USB3 hub (the operator's inventory ID)")
    vid_pid = hub.get("vid_pid")
    if "vid_pid" in hub and not (isinstance(vid_pid, str) and _VID_PID_RE.match(vid_pid)):
        errors.append("usb_hub.vid_pid must be 'vvvv:pppp' (lowercase hex, as observed in sysfs)")
    if "self_powered" in hub and hub.get("self_powered") is not True:
        errors.append("usb_hub.self_powered must be true — the operator attests that the "
                      "hub's external power supply is connected (false = contract violated)")
    if "self_powered_source" in hub and hub.get("self_powered_source") != OPERATOR_ATTESTED:
        errors.append(f"usb_hub.self_powered_source must be '{OPERATOR_ATTESTED}' — "
                      "hub power is not a software measurement")
    if "individual_b210_dc" in hub and hub.get("individual_b210_dc") is not False:
        errors.append("usb_hub.individual_b210_dc must be false — this profile has no "
                      "per-B210 DC supply (true = contract violated)")
    if ("individual_b210_dc_source" in hub
            and hub.get("individual_b210_dc_source") != OPERATOR_ATTESTED):
        errors.append(f"usb_hub.individual_b210_dc_source must be '{OPERATOR_ATTESTED}'")

    endpoints = declaration.get("endpoints") or {}
    unknown_roles = sorted(set(endpoints) - set(ROLES))
    if unknown_roles:
        errors.append(f"unknown endpoint role(s): {unknown_roles}")
    for role in ROLES:
        entry = endpoints.get(role) or {}
        if not isinstance(entry, dict):
            continue
        if "external_power_asset" in entry:
            errors.append(f"endpoints.{role}.external_power_asset is not a "
                          f"{profile_name} field (no per-B210 DC supply) — remove it; "
                          "a legacy DC asset ID must not be declared under this profile")
        extra = sorted(set(entry) - _ENDPOINT_KEYS_V2 - {"external_power_asset"})
        if extra:
            errors.append(f"unknown endpoints.{role} field(s): {extra} — USB paths and "
                          "speeds are observed, not declared")
    wiring = declaration.get("wiring")
    if isinstance(wiring, dict):
        unknown = sorted(set(wiring) - _WIRING_KEYS)
        if unknown:
            errors.append(f"unknown wiring field(s): {unknown}")
    return errors


# ------------------------------------------------------------ binding subset

def critical_topology(declaration: dict) -> dict:
    """reference `standalone_critical.usb_topology` — the qualification-critical
    subset of the approved powered-hub declaration (compared by `vdeter run`).

    Paths, ports, device numbers and network addresses are deliberately
    excluded (observation only)."""
    hub = (declaration or {}).get("usb_hub") or {}
    endpoints = (declaration or {}).get("endpoints") or {}
    definition = host_profiles.profile(PI_POWERED_HUB_PROFILE)
    return {
        "host_profile": PI_POWERED_HUB_PROFILE,
        "hub": {"asset_id": hub.get("asset_id"), "vid_pid": hub.get("vid_pid")},
        "endpoints": {role: {"serial": (endpoints.get(role) or {}).get("serial"),
                             "speed_class": definition.speed_class(role),
                             "usb_transport": definition.usb_transport[role]}
                      for role in ROLES},
        "power": {
            "source": POWERED_USB3_HUB_BUS_POWER,
            "hub_self_powered": hub.get("self_powered"),
            "hub_self_powered_source": hub.get("self_powered_source"),
            "individual_b210_dc": hub.get("individual_b210_dc"),
            "individual_b210_dc_source": hub.get("individual_b210_dc_source"),
        },
        "network_binding": "excluded",
    }


def topology_mismatches(expected: dict, current: dict) -> list:
    """Field-by-field comparison of two critical topology subsets."""
    mismatches = []
    expected = expected or {}
    current = current or {}
    if expected.get("host_profile") != current.get("host_profile"):
        mismatches.append("host_profile %r qualified, %r declared"
                          % (expected.get("host_profile"), current.get("host_profile")))
    for key in ("asset_id", "vid_pid"):
        if (expected.get("hub") or {}).get(key) != (current.get("hub") or {}).get(key):
            mismatches.append("USB hub %s %r qualified, %r declared"
                              % (key, (expected.get("hub") or {}).get(key),
                                 (current.get("hub") or {}).get(key)))
    for role in ROLES:
        want = (expected.get("endpoints") or {}).get(role) or {}
        have = (current.get("endpoints") or {}).get(role) or {}
        for key in ("serial", "speed_class", "usb_transport"):
            if want.get(key) != have.get(key):
                mismatches.append("%s: %s %r qualified, %r declared"
                                  % (role, key, want.get(key), have.get(key)))
    for key in ("source", "hub_self_powered", "hub_self_powered_source",
                "individual_b210_dc", "individual_b210_dc_source"):
        if (expected.get("power") or {}).get(key) != (current.get("power") or {}).get(key):
            mismatches.append("power provenance %s %r qualified, %r declared"
                              % (key, (expected.get("power") or {}).get(key),
                                 (current.get("power") or {}).get(key)))
    return mismatches


def topology_observations(preflight: dict) -> dict:
    """reference `hardware_observations` — recorded, never compared."""
    check = ((preflight or {}).get("checks") or {}).get("usb_topology") or {}
    hub = check.get("hub") or {}
    return {
        "hub": {key: hub.get(key) for key in (
            "usb_path", "vid_pid", "speed_mbps", "product", "manufacturer",
            "device_class", "maxchild", "parent", "devnum", "bm_attributes",
            "descriptor_self_powered")},
        "endpoints": {role: (None if info is None else {
            key: info.get(key) for key in ("usb_path", "hub_port", "speed_mbps",
                                           "speed_class", "devnum")})
                      for role, info in sorted((check.get("observed_by_role") or {}).items())},
        "other_hubs": check.get("other_hubs") or [],
        "descriptor_note": ("descriptor_self_powered is the hub's own bmAttributes bit 6; "
                            "the connected external supply is an operator attestation "
                            "recorded in standalone_critical.usb_topology.power"),
    }
