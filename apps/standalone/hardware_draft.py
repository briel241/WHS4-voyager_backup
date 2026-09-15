"""hardware.toml draft generation for `vdeter setup` (setup-time only).

`vdeter setup` writes a hardware.toml draft when none exists. This module is the
single place that decides what that draft contains. It handles three inputs
explicitly and keeps them apart in the written file:

  1. the **expected profile** — the lab default agreed with the operator
     (``LAB_DEFAULT``) or another ``ExpectedHardware`` the caller passes; every
     value in it is an expectation, never a measurement;
  2. the **observation** — the sysfs snapshot taken at setup (B210 serials,
     link speeds, hub VID:PID, paths), summarised by :func:`hub_draft_candidate`;
  3. the **confirmation state** — a new draft is always unapproved
     (``wiring.approved = false``) and carries no operator attestation: the
     two power attestations keep their fail-closed values and their
     ``*_source`` lines are written commented out, so the draft carries no
     active ``operator_attested`` value — the token appears only in the
     commented guidance the operator uncomments after a physical check.

Rules for filling a value from the expected profile:

  - a role serial is filled only when that exact serial was observed as a
    B210 below the hub; it records the *expected* role→serial mapping (the
    device exists) and says nothing about the RF cable wiring — approval stays
    with the operator;
  - the hub asset ID cannot be measured; it is filled only when the observed
    hub VID:PID equals the expected one *and* every expected serial was
    observed (the environment is recognised as the expected lab);
  - a missing or unexpected device is reported as a mismatch and left
    ``REQUIRED_FILL``; no other device is ever assigned in its place;
  - when the observed topology is not the expected powered-hub topology the
    legacy (schema 1) draft is written exactly as before, plus the mismatch
    notes; nothing is prefilled there.

The declaration schema, its validation and the qualification-critical subset
stay in :mod:`apps.standalone.hardware_profiles`; this module only renders
text that that schema will later parse once the operator has completed it.
Nothing here reads Git, the network or a device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.standalone import hardware_profiles

host_profiles = hardware_profiles.host_profiles

DRAFT = hardware_profiles.DRAFT
ROLES = hardware_profiles.ROLES
PI_POWERED_HUB_PROFILE = hardware_profiles.PI_POWERED_HUB_PROFILE
LEGACY_PI_PROFILE = hardware_profiles.LEGACY_PI_PROFILE
OPERATOR_ATTESTED = hardware_profiles.OPERATOR_ATTESTED

#: wiring profile identifier written by every draft (unchanged historical value).
DEFAULT_WIRING_PROFILE = "C1-C6-STANDARD"


@dataclass(frozen=True)
class ExpectedHardware:
    """An expected environment — values the operator expects to find, not facts.

    ``host_profile`` names the host topology the expectation applies to. Only
    the powered-hub profile supports prefilling; for any other observed
    topology the expectation is reported as a mismatch and nothing is filled.
    """

    label: str
    host_profile: str
    serial_by_role: dict
    hub_asset_id: str | None
    hub_vid_pid: str | None
    wiring_profile: str = DEFAULT_WIRING_PROFILE


#: The lab default profile (requirements R6, 2026-09-15): Raspberry Pi 5 +
#: one self-powered USB3 hub + three B210s. Expected values only.
LAB_DEFAULT = ExpectedHardware(
    label="lab-default",
    host_profile=PI_POWERED_HUB_PROFILE,
    serial_by_role={"MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"},
    hub_asset_id="HUB-VIA-2109-0817-01",
    hub_vid_pid="2109:0817",
    wiring_profile=DEFAULT_WIRING_PROFILE,
)


@dataclass
class HardwareDraft:
    """Result of :func:`build_hardware_draft` — the text plus what was decided."""

    text: str
    profile: str
    expected_label: str | None
    #: what the expected profile contributed: {"endpoints": {role: serial|None},
    #: "usb_hub.asset_id": str|None, "wiring.profile": str}
    expected_applied: dict = field(default_factory=dict)
    #: human-readable reasons why an expected value was not applied, or why
    #: the observation differs from the expectation (empty = full match).
    mismatches: list = field(default_factory=list)
    #: the hub candidate summary the draft was built from (observation).
    observation: dict = field(default_factory=dict)


# ------------------------------------------------------------ observation

def hub_draft_candidate(topology: dict) -> dict:
    """Setup-time observation: are exactly three B210s below one self-powered
    SuperSpeed hub? Roles are never assigned automatically."""
    devices = (topology or {}).get("devices") or {}
    b210 = {name: d for name, d in sorted(devices.items())
            if d.get("vid_pid") == host_profiles.B210_VID_PID}
    serials = [d.get("serial") for d in b210.values()]
    result = {"candidate": False, "b210_count": len(b210), "reasons": [],
              "devices": [{"usb_path": name, "hub_port": d.get("port"),
                           "serial": d.get("serial"), "speed_mbps": d.get("speed_mbps"),
                           "parent": d.get("parent")} for name, d in b210.items()]}
    if len(b210) != 3 or len(set(serials)) != 3 or None in serials:
        result["reasons"].append("exactly three B210s with distinct serials are required")
        return result
    probe = host_profiles.check_shared_powered_hub(
        {f"B210-{i}": serial for i, serial in enumerate(serials, 1)}, topology)
    hub = probe.get("hub") or {}
    result.update({
        "candidate": bool(probe["pass"]),
        "reasons": list(probe["errors"]),
        "hub_path": hub.get("usb_path"),
        "hub_vid_pid": hub.get("vid_pid"),
        "hub_speed_mbps": hub.get("speed_mbps"),
        "hub_product": hub.get("product"),
        "hub_descriptor_self_powered": hub.get("descriptor_self_powered"),
    })
    return result


# ------------------------------------------------------------ expectation vs observation

def _compare_hub_candidate(candidate: dict, expected: ExpectedHardware | None) -> dict:
    """Decide, per field, whether an expected value may be written.

    Returns {"serial_fill": {role: {"serial", "usb_path", "speed_mbps"}|None},
             "asset_fill": str|None, "vid_pid_match": bool|None,
             "unassigned": [device...], "mismatches": [str]}."""
    devices = candidate.get("devices") or []
    by_serial = {d.get("serial"): d for d in devices}
    fill = {role: None for role in ROLES}
    mismatches = []
    if expected is None:
        return {"serial_fill": fill, "asset_fill": None, "vid_pid_match": None,
                "unassigned": list(devices), "mismatches": mismatches}
    if expected.host_profile != PI_POWERED_HUB_PROFILE:
        mismatches.append(
            "expected profile %s (%s) is not the observed powered-hub topology — "
            "nothing prefilled" % (expected.host_profile, expected.label))
        return {"serial_fill": fill, "asset_fill": None, "vid_pid_match": None,
                "unassigned": list(devices), "mismatches": mismatches}

    expected_serials = set()
    for role in ROLES:
        want = expected.serial_by_role.get(role)
        if not want:
            mismatches.append("%s: the %s profile names no expected serial — left %s"
                              % (role, expected.label, DRAFT))
            continue
        expected_serials.add(want)
        seen = by_serial.get(want)
        if seen is None:
            mismatches.append(
                "%s: expected serial %s (%s) is not observed below the hub — left %s, "
                "no other device assigned" % (role, want, expected.label, DRAFT))
            continue
        fill[role] = {"serial": want, "usb_path": seen.get("usb_path"),
                      "speed_mbps": seen.get("speed_mbps"), "hub_port": seen.get("hub_port")}
    unassigned = [d for d in devices if d.get("serial") not in expected_serials]
    for d in unassigned:
        mismatches.append(
            "unexpected B210 serial %s at %s (%sM) is not in the %s profile — not assigned "
            "to any role" % (d.get("serial"), d.get("usb_path"), d.get("speed_mbps"),
                             expected.label))

    observed_vid_pid = candidate.get("hub_vid_pid")
    vid_pid_match = None
    if expected.hub_vid_pid:
        vid_pid_match = observed_vid_pid == expected.hub_vid_pid
        if not vid_pid_match:
            mismatches.append(
                "hub VID:PID observed %s, expected %s (%s) — asset ID left %s"
                % (observed_vid_pid, expected.hub_vid_pid, expected.label, DRAFT))
    all_roles_observed = all(fill[role] is not None for role in ROLES)
    asset_fill = None
    if expected.hub_asset_id:
        if vid_pid_match and all_roles_observed:
            asset_fill = expected.hub_asset_id
        elif vid_pid_match is not False:
            mismatches.append(
                "hub asset ID %s (%s) not applied: the three expected serials were not all "
                "observed, so this may not be the expected hub — left %s"
                % (expected.hub_asset_id, expected.label, DRAFT))
    return {"serial_fill": fill, "asset_fill": asset_fill, "vid_pid_match": vid_pid_match,
            "unassigned": unassigned, "mismatches": mismatches}


# ------------------------------------------------------------ rendering

_HEADER_UNAPPROVED = (
    "# VDETER standalone hardware declaration — DRAFT written by `vdeter setup`; NOT approved.",
    "# Nothing in this file has been confirmed by the operator yet. Values marked",
    "# 'observed' were read from sysfs at setup time; values marked 'expected' come",
    "# from an expected profile and are expectations, not measurements. Review every",
    "# value, complete the REQUIRED_FILL fields, record what only a physical check can",
    "# confirm (power, attestations), then set wiring.approved = true.",
)


def _serial_line(role: str, filled, expected: ExpectedHardware | None,
                 extra_note: str | None = None) -> str:
    """One `serial = …` line with a single trailing comment (never two)."""
    if filled is not None:
        note = ('expected (%s) role mapping; observed at %s %sM — the RF cable for this '
                'role is NOT confirmed by this line'
                % (expected.label, filled.get("usb_path"), filled.get("speed_mbps")))
        value = filled["serial"]
    elif expected is not None and expected.serial_by_role.get(role):
        note = ('UNVERIFIED: expected %s (%s) not observed — fill in the serial of the '
                'B210 wired for this role' % (expected.serial_by_role[role], expected.label))
        value = DRAFT
    else:
        note = None
        value = DRAFT
    notes = [n for n in (note, extra_note) if n]
    line = 'serial = "%s"' % value
    return line + ("   # " + "; ".join(notes) if notes else "")


def render_pi_powered_hub_draft(candidate: dict, *, expected: ExpectedHardware | None,
                                comparison: dict) -> str:
    """schema 2 draft text — observation as comments, expected values only where
    :func:`_compare_hub_candidate` allowed them, attestations fail-closed and
    their source lines commented out (no attestation token is generated)."""
    device_lines = [
        "#   hub port %s / %s: serial %s %sM" % (
            d.get("hub_port"), d.get("usb_path"), d.get("serial"), d.get("speed_mbps"))
        for d in candidate.get("devices") or []]
    note_lines = ["#   - %s" % note for note in comparison["mismatches"]]
    if expected is not None:
        expectation_lines = [
            "# Expected profile: %s — serials %s, hub asset %s, hub VID:PID %s, wiring %s."
            % (expected.label,
               ", ".join("%s=%s" % (role, expected.serial_by_role.get(role)) for role in ROLES),
               expected.hub_asset_id, expected.hub_vid_pid, expected.wiring_profile),
            "# Expected values below are filled only where the observation supports them;",
            "# they remain expectations until approved.",
        ]
        if note_lines:
            expectation_lines.append("# Expectation vs observation:")
            expectation_lines.extend(note_lines)
        else:
            expectation_lines.append("# Expectation vs observation: every expected serial and "
                                     "the hub VID:PID were observed.")
    else:
        expectation_lines = ["# No expected profile was given: fill every REQUIRED_FILL field."]
    asset_fill = comparison["asset_fill"]
    if asset_fill is not None:
        asset_line = ('asset_id = "%s"   # expected (%s) inventory ID — cannot be verified '
                      'by software; confirm it matches the label on the hub' % (asset_fill, expected.label))
    else:
        asset_line = 'asset_id = "%s"               # physical asset ID of the powered USB3 hub' % DRAFT
    wiring_profile = expected.wiring_profile if expected is not None else DEFAULT_WIRING_PROFILE
    return "\n".join([
        *_HEADER_UNAPPROVED,
        "# Profile draft: %s" % PI_POWERED_HUB_PROFILE,
        "# (Raspberry Pi 5, three B210s on ONE self-powered USB3 hub, all 5000M, hub bus power,",
        "#  no per-B210 DC asset). Observed at setup — paths, ports and device numbers are",
        "#  recorded for reference only and are never declared or bound:",
        "#   hub: VID:PID %s at %s, %sM, product %r, descriptor self-powered bit=%s" % (
            candidate.get("hub_vid_pid"), candidate.get("hub_path"),
            candidate.get("hub_speed_mbps"), candidate.get("hub_product"),
            candidate.get("hub_descriptor_self_powered")),
        *device_lines,
        *expectation_lines,
        "# The role (MASS-A/MASS-B/ROC) to serial mapping is only an expectation until the",
        "# operator who knows the RF wiring confirms it. The hub's external supply and the",
        "# absence of per-B210 DC supplies cannot be measured by software: change the two",
        "# attestations below only after checking the physical setup.",
        "",
        "schema_version = 2",
        'platform_profile = "pi"',
        'hardware_profile = "%s"' % PI_POWERED_HUB_PROFILE,
        "",
        "[usb_hub]",
        asset_line,
        'vid_pid = "%s"                      # observed; must match the hub above the B210s'
        % candidate.get("hub_vid_pid"),
        "self_powered = false                     # UNVERIFIED — set true only after checking that the hub's external supply is connected",
        '# self_powered_source = "%s"   # uncomment when you set self_powered = true (the schema requires this source token)' % OPERATOR_ATTESTED,
        "individual_b210_dc = true                # UNVERIFIED — set false only after checking that no per-B210 DC brick is used",
        '# individual_b210_dc_source = "%s"   # uncomment when you set individual_b210_dc = false' % OPERATOR_ATTESTED,
        "",
        "[endpoints.MASS-A]",
        _serial_line("MASS-A", comparison["serial_fill"]["MASS-A"], expected),
        "",
        "[endpoints.MASS-B]",
        _serial_line("MASS-B", comparison["serial_fill"]["MASS-B"], expected,
                     extra_note="5000M on the hub — no external_power_asset here"),
        "",
        "[endpoints.ROC]",
        _serial_line("ROC", comparison["serial_fill"]["ROC"], expected),
        "",
        "[wiring]",
        'profile = "%s"   # expected wiring profile identifier — the physical wiring is unverified until approved'
        % wiring_profile,
        "approved = false   # set to true only after verifying the wiring; required to run",
        "",
    ])


def render_legacy_draft(observed: dict, *, expected: ExpectedHardware | None,
                        candidate: dict | None) -> str:
    """schema 1 draft text — identical structure to the historical draft
    (serials/asset REQUIRED_FILL, roles never assigned) plus, when an expected
    profile was given, the reasons the expected topology was not observed."""
    observed_lines = [
        f"#   serial {serial}: usb_path={info.get('usb_path')} "
        f"speed={info.get('speed_mbps')}M"
        for serial, info in sorted(observed.items())
    ] or ["#   (no USB serial detected — connect the devices and rerun setup)"]
    expectation_lines = []
    if expected is not None:
        expectation_lines = [
            "# Expected profile %s (%s) was NOT observed, so nothing is prefilled:"
            % (expected.label, expected.host_profile)]
        reasons = list((candidate or {}).get("reasons") or []) or [
            "no USB topology snapshot was available"]
        expectation_lines.extend("#   - %s" % reason for reason in reasons)
    wiring_profile = expected.wiring_profile if expected is not None else DEFAULT_WIRING_PROFILE
    return "\n".join([
        *_HEADER_UNAPPROVED,
        "# The role (MASS-A/MASS-B/ROC) to serial mapping cannot be decided automatically:",
        "# the operator who knows the physical wiring and power setup must fill it in.",
        "# Detected devices (for reference):",
        *observed_lines,
        *expectation_lines,
        "",
        "schema_version = 1",
        'platform_profile = "pi"   # "pi" | "generic-pc"',
        "",
        '[endpoints.MASS-A]',
        'serial = "%s"' % DRAFT,
        "",
        '[endpoints.MASS-B]',
        'serial = "%s"' % DRAFT,
        'external_power_asset = "%s"' % DRAFT,
        "",
        '[endpoints.ROC]',
        'serial = "%s"' % DRAFT,
        "",
        "[wiring]",
        'profile = "%s"   # expected wiring profile identifier — the physical wiring is unverified until approved'
        % wiring_profile,
        "approved = false   # set to true only after verifying the wiring; required to run",
        "",
    ])


# ------------------------------------------------------------ entry point

def build_hardware_draft(*, observed: dict, candidate: dict | None,
                         expected: ExpectedHardware | None = LAB_DEFAULT) -> HardwareDraft:
    """Build the draft from the observation and the expected profile.

    ``observed``: local_preflight.probe_usb_serials() result (serial → path/speed).
    ``candidate``: :func:`hub_draft_candidate` result (or None when no topology
    snapshot exists). ``expected``: the expected profile, or None to write the
    plain draft with no expectations (today's behaviour in another environment).
    """
    candidate = candidate or {"candidate": False, "reasons": [], "devices": []}
    if candidate.get("candidate"):
        comparison = _compare_hub_candidate(candidate, expected)
        text = render_pi_powered_hub_draft(candidate, expected=expected, comparison=comparison)
        applied = {
            "endpoints": {role: (info or {}).get("serial")
                          for role, info in comparison["serial_fill"].items()},
            "usb_hub.asset_id": comparison["asset_fill"],
            "wiring.profile": (expected.wiring_profile if expected is not None
                               else DEFAULT_WIRING_PROFILE),
        }
        return HardwareDraft(text=text, profile=PI_POWERED_HUB_PROFILE,
                             expected_label=(expected.label if expected else None),
                             expected_applied=applied,
                             mismatches=list(comparison["mismatches"]),
                             observation=candidate)
    mismatches = []
    if expected is not None:
        reasons = list(candidate.get("reasons") or []) or ["no USB topology snapshot was available"]
        mismatches.append("expected profile %s (%s) not observed: %s"
                          % (expected.label, expected.host_profile, "; ".join(reasons)))
    text = render_legacy_draft(observed, expected=expected, candidate=candidate)
    return HardwareDraft(text=text, profile=LEGACY_PI_PROFILE,
                         expected_label=(expected.label if expected else None),
                         expected_applied={"endpoints": {role: None for role in ROLES},
                                           "usb_hub.asset_id": None,
                                           "wiring.profile": (expected.wiring_profile
                                                              if expected is not None
                                                              else DEFAULT_WIRING_PROFILE)},
                         mismatches=mismatches, observation=candidate)


__all__ = (
    "DEFAULT_WIRING_PROFILE",
    "ExpectedHardware",
    "HardwareDraft",
    "LAB_DEFAULT",
    "build_hardware_draft",
    "hub_draft_candidate",
    "render_legacy_draft",
    "render_pi_powered_hub_draft",
)
