"""Pi powered-USB3-hub host profile — RF-off contract tests (2026-09-10).

증명하는 것 (apps/standalone/README.md §4·§8·§9·§15)
  단일 정의  : legacy Pi 상수(ROLE_SPEED_CLASS·coordinator _EXPECTED_USB_SPEED·
               USB_TRANSPORTS)가 vdeter.b210_system.host_profiles의 legacy 정의와
               값 동일 — 규칙은 한 곳에서만 나온다.
  legacy 보존: schema 1 선언·preflight·loader가 종전과 같이 MASS-B 480M +
               외부 DC asset을 요구하고, 5000M·asset 누락·powered-hub 키 혼입은
               종전처럼(또는 모순으로) 거부한다.
  새 profile : schema 2 = STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1 — 세 endpoint
               super(5000M) + 같은 self-powered SuperSpeed hub + 개별 DC 없음
               + hub asset/attestation 결속; 480M·다른 hub/root·bus-powered
               hub·cascade·asset/attestation 누락·모순·serial 불일치·초과
               장치는 전부 fail-closed.
  계약 일치  : setup 초안 → hardware.toml → 실제 Stage7ConfigurationLoader →
               preflight → RR-P0 기대값(coordinator)이 모두 같은 profile
               정의에서 나온다.
  결속       : issue_reference/compare_critical의 host_profile·usb_topology,
               installation manifest 영역 분류와 digest 결정론.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"), os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (  # noqa: E402
    hardware_profiles,
    installation_manifest,
    local_config,
    local_preflight,
    qualification_domains as qd,
    qualify_cmd,
    run_cmd,
    setup_cmd,
    state,
)
from vdeter.b210_system import configuration, host_profiles  # noqa: E402

REPO = Path(_REPO)
HUB = host_profiles.PI_POWERED_HUB_PROFILE
LEGACY = host_profiles.LEGACY_PI_PROFILE
ROLES = ("MASS-A", "MASS-B", "ROC")
SERIALS = {"MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"}
MACHINE_ID = "a70b2048eb6743bd83ac9e0920b8d0b2"

LEGACY_HARDWARE = {
    "schema_version": 1,
    "platform_profile": "pi",
    "endpoints": {
        "MASS-A": {"serial": "34D03DD"},
        "MASS-B": {"serial": "3515425", "external_power_asset": "AT2024T"},
        "ROC": {"serial": "34D03E8"},
    },
    "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
}

HUB_HARDWARE = {
    "schema_version": 2,
    "platform_profile": "pi",
    "hardware_profile": HUB,
    "usb_hub": {
        "asset_id": "HUB-VIA-2109-0817-01",
        "vid_pid": "2109:0817",
        "self_powered": True,
        "self_powered_source": "operator_attested",
        "individual_b210_dc": False,
        "individual_b210_dc_source": "operator_attested",
    },
    "endpoints": {
        "MASS-A": {"serial": "34D03DD"},
        "MASS-B": {"serial": "3515425"},
        "ROC": {"serial": "34D03E8"},
    },
    "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
}

ENV_MANIFEST = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": "unit-test",
    "usb_devices": "/sys/bus/usb/devices/5-1.2/serial 34D03DD",
    "numpy_version": "1.26.4",
    "cryptography_version": "50.0.1",
    "uhd_version": "UHD 4.6.0.0",
    "gnuradio_version": "3.10.9.2",
}


# ---------------------------------------------------------------- helpers

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"%s"' % value


def write_toml(tmp, declaration: dict, name="hardware.toml") -> Path:
    """dict → hardware.toml (top-level scalars, then tables)."""
    lines = []
    for key, value in declaration.items():
        if not isinstance(value, dict):
            lines.append("%s = %s" % (key, _toml_value(value)))
    for key, value in declaration.items():
        if key == "endpoints":
            for role, body in value.items():
                lines.append('[endpoints."%s"]' % role)
                for k, v in body.items():
                    lines.append("%s = %s" % (k, _toml_value(v)))
        elif isinstance(value, dict):
            lines.append("[%s]" % key)
            for k, v in value.items():
                lines.append("%s = %s" % (k, _toml_value(v)))
    path = Path(tmp) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def hub_decl(mutate=None) -> dict:
    declaration = copy.deepcopy(HUB_HARDWARE)
    if mutate:
        mutate(declaration)
    return declaration


def make_topology(tmp, *, hub=("5-1", "2109:0817", 5000, "e0"), roots=("usb5", "usb4"),
                  b210=(("5-1.1", "3515425", 5000), ("5-1.2", "34D03DD", 5000),
                        ("5-1.4", "34D03E8", 5000)),
                  extra_devices=(), hub_class="09", cascaded=False) -> str:
    """가짜 sysfs 트리 — 실제 Pi 5 + VIA 2109:0817 hub 관측(2026-09-10)을 본뜬 것.

    extra_devices: [(name, vid_pid, speed, serial|None, class, bmAttr)]"""
    root = Path(tmp) / "sysfs"
    root.mkdir(parents=True, exist_ok=True)
    for r in roots:
        d = root / r
        d.mkdir(exist_ok=True)
        (d / "speed").write_text("5000" if r == "usb5" else "480")
        (d / "busnum").write_text(r[3:])
    devnum = 2

    def device(name, vid_pid, speed, *, serial=None, cls="ff", attr="80",
               product="USRP B200", maxchild=0):
        nonlocal devnum
        d = root / name
        d.mkdir(exist_ok=True)
        vid, pid = vid_pid.split(":")
        (d / "idVendor").write_text(vid)
        (d / "idProduct").write_text(pid)
        (d / "speed").write_text(str(speed))
        (d / "bDeviceClass").write_text(cls)
        (d / "bmAttributes").write_text(attr)
        (d / "product").write_text(product)
        (d / "manufacturer").write_text("test")
        (d / "maxchild").write_text(str(maxchild))
        (d / "devnum").write_text(str(devnum))
        (d / "busnum").write_text(name.split("-")[0])
        if serial is not None:
            (d / "serial").write_text(serial)
        devnum += 1

    if hub is not None:
        hub_name, vid_pid, speed, attr = hub
        if cascaded:
            device("5-2", "2109:0817", 5000, cls="09", attr="e0",
                   product="upstream hub", maxchild=4)
        device(hub_name, vid_pid, speed, cls=hub_class, attr=attr,
               product="USB3.0 Hub", maxchild=4)
    for name, serial, speed in b210:
        device(name, "2500:0020", speed, serial=serial)
    for name, vid_pid, speed, serial, cls, attr in extra_devices:
        device(name, vid_pid, speed, serial=serial, cls=cls, attr=attr,
               product="extra")
    # interface entries ('5-1.1:1.0') must be ignored by the probe — NTFS cannot
    # create such a name, so the check only runs where the OS allows it.
    try:
        (root / "5-1.1:1.0").mkdir(exist_ok=True)
    except OSError:
        pass
    return str(root)


def preflight_kwargs(tmp, hardware, sysfs):
    paths = state.StatePaths(tmp)
    paths.ensure_var()
    return dict(
        hardware=hardware, paths=paths,
        key_path=Path(tmp) / "k.pem", cert_path=Path(tmp) / "c.pem",
        usb_probe=lambda: local_preflight.probe_usb_serials(sysfs),
        topology_probe=lambda: host_profiles.probe_usb_topology(sysfs),
        residual_probe=lambda: [],
        platform_probe=lambda p: {"pass": True, "profile": p, "error": None},
        key_mode_check=lambda p: {"pass": True, "present": True,
                                  "mode": "0o600", "error": None},
        key_cert_check=lambda k, c: True,
    )


def load_coordinator():
    spec = importlib.util.spec_from_file_location(
        "stage7_coordinator_main_pi_hub_test",
        REPO / "VDETER" / "apps" / "stage7_coordinator_main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hub_usb_paths():
    return {"MASS-A": "5-1.2", "MASS-B": "5-1.1", "ROC": "5-1.4"}


def generate(tmp, hardware, *, usb_paths=None, machine_id=MACHINE_ID, phase="rr-p0",
             run_mode="RF_OFF_QUALIFICATION"):
    record = installation_manifest.measure(REPO)
    return local_config.generate_run_configs(
        repo_root=REPO, run_root=Path(tmp) / "run", run_id="pi-hub-test",
        rr_phase=phase, hardware=hardware, installation=record,
        installation_verified=True, run_mode=run_mode,
        environment_manifest=dict(ENV_MANIFEST), usb_paths=usb_paths,
        host_machine_id=machine_id)


# ---------------------------------------------------------------- 1. 단일 정의

def test_single_definition_legacy_constants_are_derived_not_duplicated():
    legacy = host_profiles.LEGACY_PI
    assert legacy.name == LEGACY == configuration.STAGE7_HOST_PROFILE
    assert local_preflight.ROLE_SPEED_CLASS == legacy.speed_classes == {
        "MASS-A": "super", "MASS-B": "high", "ROC": "super"}
    coordinator = load_coordinator()
    assert coordinator._EXPECTED_USB_SPEED == {
        role: legacy.expected_sysfs_speed(role) for role in ROLES} == {
        "MASS-A": "5000", "MASS-B": "480", "ROC": "5000"}
    assert configuration.USB_TRANSPORTS == host_profiles.USB_TRANSPORTS
    assert configuration.STAGE7_HOST_PROFILES == host_profiles.HOST_PROFILES == (LEGACY, HUB)
    hub = host_profiles.PI_POWERED_HUB
    assert hub.speed_classes == {role: "super" for role in ROLES}
    assert hub.platform_profiles == ("pi",)
    assert hub.shared_powered_hub and not legacy.shared_powered_hub
    assert legacy.usb_transport == {"MASS-A": "DIRECT_USB3_5000M",
                                    "MASS-B": "DIRECT_USB2_480M_EXTERNAL_DC",
                                    "ROC": "DIRECT_USB3_5000M"}
    assert legacy.power_source["MASS-B"] == host_profiles.POWER_EXTERNAL_DC_ASSET
    assert set(hub.power_source.values()) == {"POWERED_USB3_HUB_BUS_POWER"}
    try:
        host_profiles.profile("NOT_A_PROFILE")
        raise AssertionError("unknown profile accepted")
    except ValueError:
        pass
    try:
        host_profiles.expected_sysfs_speed("NOT_A_TRANSPORT")
        raise AssertionError("unknown transport accepted")
    except ValueError:
        pass


# ---------------------------------------------------------------- 2·3. legacy 보존

def test_legacy_schema1_unchanged_and_powered_hub_keys_are_contradictory():
    with tempfile.TemporaryDirectory() as tmp:
        parsed = local_config.parse_hardware_declaration(write_toml(tmp, LEGACY_HARDWARE))
        assert hardware_profiles.profile_of(parsed) == LEGACY
        assert parsed["endpoints"]["MASS-B"]["external_power_asset"] == "AT2024T"

        def without_asset(d):
            d["endpoints"]["MASS-B"].pop("external_power_asset")

        def with_profile_key(d):
            d["hardware_profile"] = HUB

        def with_hub_table(d):
            d["usb_hub"] = dict(HUB_HARDWARE["usb_hub"])

        def bad_schema(d):
            d["schema_version"] = 3

        for mutate, needle in ((without_asset, "external_power_asset is missing"),
                               (with_profile_key, "not a schema_version 1"),
                               (with_hub_table, "not a schema_version 1"),
                               (bad_schema, "schema_version must be 1")):
            declaration = copy.deepcopy(LEGACY_HARDWARE)
            mutate(declaration)
            try:
                local_config.parse_hardware_declaration(write_toml(tmp, declaration))
                raise AssertionError("must be refused: %s" % needle)
            except local_config.ConfigGenerationError as exc:
                assert needle in str(exc), (needle, str(exc))


def test_legacy_preflight_still_requires_mass_b_480m_and_refuses_5000m():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp, hub=None, roots=("usb3", "usb4", "usb5"),
                              b210=(("3-1", "34D03DD", 5000), ("4-2", "3515425", 480),
                                    ("5-1", "34D03E8", 5000)))
        report = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp, LEGACY_HARDWARE, sysfs))
        assert report["pass"], report["failures"]
        assert report["host_profile"] == LEGACY
        assert "usb_topology" not in report["checks"]  # legacy: no hub contract
        assert report["checks"]["b210"]["observed_by_role"]["MASS-B"]["speed_class"] == "high"

        sysfs2 = make_topology(tmp + "/b", b210=(("5-1.1", "3515425", 5000),
                                                  ("5-1.2", "34D03DD", 5000),
                                                  ("5-1.4", "34D03E8", 5000)))
        report2 = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp + "/b", LEGACY_HARDWARE, sysfs2))
        assert not report2["pass"]
        assert any("MASS-B: USB speed class mismatch — required high, observed super(5000M)"
                   in f for f in report2["failures"]), report2["failures"]


def test_legacy_config_generation_and_loader_rules_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        generated = generate(tmp, LEGACY_HARDWARE)
        mass_b = (Path(generated["configs_dir"]) / "endpoints" / "mass-b.toml").read_text(
            encoding="utf-8")
        assert 'usb_transport = "DIRECT_USB2_480M_EXTERNAL_DC"' in mass_b
        assert 'external_power_asset = "AT2024T"' in mass_b
        assert 'profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"' in mass_b
        assert generated["git_state"]["hardware"]["host_profile"] == LEGACY
        assert generated["installation_state"]["endpoint_template_dir"] == (
            "VDETER/configs/stage7/endpoints")
        # legacy MASS-B must not accept the powered-hub power token as a DC asset.
        loader = configuration.Stage7ConfigurationLoader()
        profile_text = (REPO / "VDETER/configs/stage7/profiles/data-profile.toml").read_text(
            encoding="utf-8")
        forged = mass_b.replace('external_power_asset = "AT2024T"',
                                'external_power_asset = "POWERED_USB3_HUB_BUS_POWER"')
        result = loader.load_endpoint_configuration(
            forged, profile_text, run_id="pi-hub-test",
            source_path="resolved-configs/endpoints/mass-b.toml")
        assert result.is_err and "external_power_asset" in str(result.error)


# ---------------------------------------------------------------- 4·5·9·10. 선언

def test_hub_schema2_declaration_passes_without_mass_b_dc_asset():
    with tempfile.TemporaryDirectory() as tmp:
        parsed = local_config.parse_hardware_declaration(write_toml(tmp, HUB_HARDWARE))
        assert hardware_profiles.profile_of(parsed) == HUB
        assert hardware_profiles.is_pi_powered_hub(parsed)
        assert "external_power_asset" not in parsed["endpoints"]["MASS-B"]
        critical = hardware_profiles.critical_topology(parsed)
        assert critical["hub"] == {"asset_id": "HUB-VIA-2109-0817-01", "vid_pid": "2109:0817"}
        assert critical["endpoints"]["MASS-B"] == {
            "serial": "3515425", "speed_class": "super",
            "usb_transport": "POWERED_USB3_HUB_5000M"}
        assert critical["power"]["source"] == "POWERED_USB3_HUB_BUS_POWER"
        assert critical["power"]["individual_b210_dc"] is False
        assert critical["network_binding"] == "excluded"


def test_hub_declaration_fail_closed_cases():
    cases = [
        (lambda d: d["usb_hub"].update(asset_id="REQUIRED_FILL"), "asset_id is still the draft"),
        (lambda d: d["usb_hub"].pop("asset_id"), "usb_hub.asset_id is missing"),
        (lambda d: d["usb_hub"].update(self_powered=False), "self_powered must be true"),
        (lambda d: d["usb_hub"].pop("self_powered"), "usb_hub.self_powered is missing"),
        (lambda d: d["usb_hub"].update(self_powered_source="measured"),
         "self_powered_source must be"),
        (lambda d: d["usb_hub"].update(individual_b210_dc=True),
         "individual_b210_dc must be false"),
        (lambda d: d["usb_hub"].update(individual_b210_dc_source="guess"),
         "individual_b210_dc_source must be"),
        (lambda d: d["usb_hub"].update(vid_pid="VIA"), "vid_pid must be"),
        (lambda d: d["usb_hub"].update(adapter_rating="5V4A"), "unknown usb_hub field"),
        (lambda d: d.pop("usb_hub"), "[usb_hub] table is missing"),
        (lambda d: d["endpoints"]["MASS-B"].update(external_power_asset="AT2024T"),
         "external_power_asset is not a"),
        (lambda d: d["endpoints"]["MASS-B"].update(usb_path="5-1.1"),
         "USB paths and speeds are observed, not declared"),
        (lambda d: d.update(platform_profile="generic-pc"), "platform_profile must be one of"),
        (lambda d: d.update(hardware_profile=LEGACY), "hardware_profile must be"),
        (lambda d: d.update(mystery=1), "unknown top-level field"),
        (lambda d: d["wiring"].update(approved=False), "approve"),
        (lambda d: d["wiring"].update(cable_set="x"), "unknown wiring field"),
        (lambda d: d["endpoints"]["MASS-A"].update(serial="3515425"), "more than one role"),
        (lambda d: d["endpoints"]["ROC"].pop("serial"), "ROC.serial"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for mutate, needle in cases:
            try:
                local_config.parse_hardware_declaration(write_toml(tmp, hub_decl(mutate)))
                raise AssertionError("must be refused: %s" % needle)
            except local_config.ConfigGenerationError as exc:
                assert needle in str(exc), (needle, str(exc))


# ---------------------------------------------------------------- 4~8·10. preflight

def test_hub_preflight_passes_three_super_links_on_one_self_powered_hub():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp)
        report = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp, HUB_HARDWARE, sysfs))
        assert report["pass"], report["failures"]
        assert report["host_profile"] == HUB
        b210 = report["checks"]["b210"]
        assert b210["pass"] and {r["speed_class"] for r in b210["observed_by_role"].values()} == {"super"}
        topology = report["checks"]["usb_topology"]
        assert topology["pass"], topology["errors"]
        assert topology["hub"]["usb_path"] == "5-1"
        assert topology["hub"]["vid_pid"] == "2109:0817"
        assert topology["hub"]["descriptor_self_powered"] is True
        assert topology["hub"]["parent"] == "usb5"
        assert {role: r["usb_path"] for role, r in topology["observed_by_role"].items()} == (
            hub_usb_paths())
        assert topology["observed_by_role"]["MASS-B"]["hub_port"] == 1
        assert "5-1.1:1.0" not in topology["b210_devices"]


def test_hub_preflight_refuses_each_topology_violation():
    with tempfile.TemporaryDirectory() as tmp:
        cases = {
            "480m": (dict(b210=(("5-1.1", "3515425", 480), ("5-1.2", "34D03DD", 5000),
                                ("5-1.4", "34D03E8", 5000))),
                     "MASS-B: B210 3515425 at 5-1.1 is 480M"),
            "other_root": (dict(b210=(("5-1.1", "3515425", 5000), ("5-1.2", "34D03DD", 5000),
                                      ("3-1", "34D03E8", 5000))),
                           "not below one common USB hub"),
            "other_hub": (dict(b210=(("5-1.1", "3515425", 5000), ("5-1.2", "34D03DD", 5000),
                                     ("5-2.1", "34D03E8", 5000)),
                               extra_devices=(("5-2", "2109:0817", 5000, None, "09", "e0"),)),
                          "not below one common USB hub"),
            "bus_powered_hub": (dict(hub=("5-1", "2109:0817", 5000, "a0")),
                                "does not report self-powered"),
            "hub_480m": (dict(hub=("4-1", "2109:2817", 480, "e0"),
                              b210=(("4-1.1", "3515425", 480), ("4-1.2", "34D03DD", 480),
                                    ("4-1.4", "34D03E8", 480))),
                         "hub 4-1 link is 480M"),
            "cascaded": (dict(hub=("5-2.1", "2109:0817", 5000, "e0"), cascaded=True,
                              b210=(("5-2.1.1", "3515425", 5000), ("5-2.1.2", "34D03DD", 5000),
                                    ("5-2.1.4", "34D03E8", 5000))),
                         "cascaded below 5-2"),
            "not_a_hub": (dict(hub_class="ff"), "is not a USB hub"),
            "wrong_vid_pid": (dict(hub=("5-1", "05e3:0626", 5000, "e0")),
                              "approved hub is 2109:0817"),
            "missing_device": (dict(b210=(("5-1.1", "3515425", 5000),
                                          ("5-1.2", "34D03DD", 5000))),
                               "ROC: B210 serial 34D03E8 is not visible"),
            "extra_b210": (dict(extra_devices=(("3-1", "2500:0020", 5000, "34D6357", "ff", "80"),)),
                           "unexpected B210 outside the declared roles"),
            "direct_on_root": (dict(hub=None, roots=("usb3", "usb4", "usb5"),
                                    b210=(("5-1", "3515425", 5000), ("5-2", "34D03DD", 5000),
                                          ("5-3", "34D03E8", 5000))),
                               "directly below root controller usb5"),
        }
        for name, (spec, needle) in cases.items():
            sysfs = make_topology(tmp + "/" + name, **spec)
            report = local_preflight.run_required_preflight(
                **preflight_kwargs(tmp + "/" + name, HUB_HARDWARE, sysfs))
            assert not report["pass"], name
            text = " | ".join(report["failures"])
            assert needle in text, (name, text)

        # serial/role mismatch: declared serial not the one observed for that role.
        def swapped(d):
            d["endpoints"]["ROC"]["serial"] = "34D6357"
        sysfs = make_topology(tmp + "/swap")
        report = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp + "/swap", hub_decl(swapped), sysfs))
        assert not report["pass"]
        assert any("ROC: declared serial 34D6357 is not visible" in f
                   for f in report["failures"]), report["failures"]

        # probe failure is fail-closed, not a pass.
        kwargs = preflight_kwargs(tmp + "/probe", HUB_HARDWARE, make_topology(tmp + "/probe"))

        def broken():
            raise OSError("sysfs unreadable")
        kwargs["topology_probe"] = broken
        report = local_preflight.run_required_preflight(**kwargs)
        assert not report["pass"] and any("probe failed" in f for f in report["failures"])


# ---------------------------------------------------------------- 4·12. config loader

def test_hub_config_generation_with_real_loader_and_fail_closed_provisioning():
    with tempfile.TemporaryDirectory() as tmp:
        generated = generate(tmp, HUB_HARDWARE, usb_paths=hub_usb_paths())
        configs = Path(generated["configs_dir"])
        assert generated["git_state"]["hardware"]["host_profile"] == HUB
        assert generated["installation_state"]["host_profile"] == HUB
        assert generated["installation_state"]["endpoint_template_dir"] == (
            "VDETER/configs/stage7/endpoints-pi-powered-usb3-hub")
        loader = configuration.Stage7ConfigurationLoader()
        profile_text = (configs / "profiles" / "data-profile.toml").read_text(encoding="utf-8")
        for endpoint, role in (("mass-a", "MASS-A"), ("mass-b", "MASS-B"), ("roc", "ROC")):
            text = (configs / "endpoints" / f"{endpoint}.toml").read_text(encoding="utf-8")
            assert 'profile = "%s"' % HUB in text
            assert 'usb_transport = "POWERED_USB3_HUB_5000M"' in text
            assert 'external_power_asset = "POWERED_USB3_HUB_BUS_POWER"' in text
            assert 'device_selector = "serial=%s"' % SERIALS[role] in text
            assert 'usb_physical_path = "%s"' % hub_usb_paths()[role] in text
            assert 'host_machine_id = "%s"' % MACHINE_ID in text
            host_block = text.split("\n[host_execution]\n")[1].split("\n[stage7]\n")[0]
            assert "REQUIRED_UNSET" not in host_block, host_block
            loaded = loader.load_endpoint_configuration(
                text, profile_text, run_id="pi-hub-test",
                source_path=f"resolved-configs/endpoints/{endpoint}.toml")
            assert loaded.is_ok, (endpoint, getattr(loaded, "error", None))
            resolved = loaded.value
            assert resolved.host_execution.profile == HUB
            assert resolved.host_execution.usb_transport == "POWERED_USB3_HUB_5000M"
            assert resolved.b210_serial == SERIALS[role]
            assert resolved.resolved_sha256 == generated["resolved_sha256"][endpoint]

        for kwargs, needle in ((dict(usb_paths=None), "USB topology path unknown"),
                               (dict(usb_paths={"MASS-A": "5-1.2"}), "['MASS-B', 'ROC']"),
                               (dict(usb_paths=hub_usb_paths(), machine_id=""),
                                "machine-id")):
            try:
                generate(tmp + "/x", HUB_HARDWARE, **kwargs)
                raise AssertionError("must be refused: %s" % needle)
            except local_config.ConfigGenerationError as exc:
                assert needle in str(exc), (needle, str(exc))


def test_loader_refuses_mixed_or_relaxed_hub_configs():
    with tempfile.TemporaryDirectory() as tmp:
        generated = generate(tmp, HUB_HARDWARE, usb_paths=hub_usb_paths())
        configs = Path(generated["configs_dir"])
        profile_text = (configs / "profiles" / "data-profile.toml").read_text(encoding="utf-8")
        text = (configs / "endpoints" / "mass-b.toml").read_text(encoding="utf-8")
        loader = configuration.Stage7ConfigurationLoader()

        def load(mutated):
            return loader.load_endpoint_configuration(
                mutated, profile_text, run_id="pi-hub-test",
                source_path="resolved-configs/endpoints/mass-b.toml")

        assert load(text).is_ok
        for old, new, needle in (
            ('external_power_asset = "POWERED_USB3_HUB_BUS_POWER"',
             'external_power_asset = "AT2024T"', "POWERED_USB3_HUB_BUS_POWER"),
            ('external_power_asset = "POWERED_USB3_HUB_BUS_POWER"',
             'external_power_asset = "PI_BUS_POWER"', "POWERED_USB3_HUB_BUS_POWER"),
            ('usb_transport = "POWERED_USB3_HUB_5000M"',
             'usb_transport = "DIRECT_USB2_480M_EXTERNAL_DC"', "canonical USB transport"),
            ('usb_transport = "POWERED_USB3_HUB_5000M"',
             'usb_transport = "DIRECT_USB3_5000M"', "canonical USB transport"),
            ('profile = "%s"' % HUB, 'profile = "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1"',
             "canonical USB transport"),
            ('profile = "%s"' % HUB, 'profile = "PI_POWERED_USB3_HUB_UNKNOWN"',
             "canonical Stage 7 profile"),
        ):
            assert text.count(old) == 1, old
            result = load(text.replace(old, new))
            assert result.is_err, (old, new)
            assert needle in str(result.error), (needle, str(result.error))


# ---------------------------------------------------------------- 11·12. preflight ↔ RR-P0

def test_rr_p0_expected_speed_and_hub_check_derive_from_resolved_profile():
    coordinator = load_coordinator()
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp)
        loader = configuration.Stage7ConfigurationLoader()
        resolved = {}
        for hardware, usb_paths in ((HUB_HARDWARE, hub_usb_paths()), (LEGACY_HARDWARE, None)):
            generated = generate(tmp + "/" + str(hardware["schema_version"]), hardware,
                                 usb_paths=usb_paths)
            configs = Path(generated["configs_dir"])
            profile_text = (configs / "profiles" / "data-profile.toml").read_text(
                encoding="utf-8")
            resolved[hardware["schema_version"]] = {
                role: loader.load_endpoint_configuration(
                    (configs / "endpoints" / f"{role.lower()}.toml").read_text(encoding="utf-8"),
                    profile_text, run_id="pi-hub-test",
                    source_path=f"resolved-configs/endpoints/{role.lower()}.toml").value
                for role in ROLES}
        # legacy resolved → exactly the historical table; hub resolved → 5000 x3.
        assert {m: coordinator._expected_usb_speed(resolved[1], m) for m in ROLES} == (
            coordinator._EXPECTED_USB_SPEED)
        assert {m: coordinator._expected_usb_speed(resolved[2], m) for m in ROLES} == {
            "MASS-A": "5000", "MASS-B": "5000", "ROC": "5000"}
        # preflight and RR-P0 consult the same definition for the same declaration.
        preflight = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp, HUB_HARDWARE, sysfs))
        for role in ROLES:
            assert preflight["checks"]["b210"]["observed_by_role"][role]["speed_class"] == (
                host_profiles.speed_class(int(coordinator._expected_usb_speed(resolved[2], role))))
        # RR-P0 hub check: same checker as preflight, same sysfs → same verdict.
        original = host_profiles.probe_usb_topology
        host_profiles.probe_usb_topology = lambda: original(sysfs)
        try:
            serials = {m: resolved[2][m].b210_serial for m in ROLES}
            live = coordinator._shared_hub_topology_check(resolved[2], serials)
            assert live["pass"], live["errors"]
            assert live["hub"]["usb_path"] == "5-1"
            assert live["observed_by_role"] == preflight["checks"]["usb_topology"]["observed_by_role"]
            assert coordinator._shared_hub_topology_check(
                resolved[1], {m: resolved[1][m].b210_serial for m in ROLES}) is None
            bad = make_topology(tmp + "/bad", hub=("5-1", "2109:0817", 5000, "a0"))
            host_profiles.probe_usb_topology = lambda: original(bad)
            live_bad = coordinator._shared_hub_topology_check(resolved[2], serials)
            assert not live_bad["pass"] and "self-powered" in " ".join(live_bad["errors"])
        finally:
            host_profiles.probe_usb_topology = original
        # mixed profiles across endpoints are refused, never averaged.
        mixed = dict(resolved[2])
        mixed["ROC"] = resolved[1]["ROC"]
        verdict = coordinator._shared_hub_topology_check(mixed, {m: "x" for m in ROLES})
        assert verdict["pass"] is False and "mixed host profiles" in verdict["errors"][0]


# ---------------------------------------------------------------- 12. setup 초안 → 계약

def test_setup_draft_selects_profile_from_observed_topology_and_stays_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        hub_sysfs = make_topology(tmp + "/hub")
        legacy_sysfs = make_topology(tmp + "/legacy", hub=None, roots=("usb3", "usb4", "usb5"),
                                     b210=(("3-1", "34D03DD", 5000), ("4-2", "3515425", 480),
                                           ("5-1", "34D03E8", 5000)))
        for label, sysfs, expected in (("hub", hub_sysfs, HUB), ("legacy", legacy_sysfs, LEGACY)):
            paths = state.StatePaths(Path(tmp) / label / "root")
            paths.ensure_var()
            observed = local_preflight.probe_usb_serials(sysfs)
            topology = host_profiles.probe_usb_topology(sysfs)
            draft = setup_cmd.draft_hardware_declaration(paths, observed, topology=topology)
            assert draft["created"] and draft["profile"] == expected, label
            text = (paths.root / "hardware.toml").read_text(encoding="utf-8")
            if expected == HUB:
                assert "schema_version = 2" in text
                assert 'hardware_profile = "%s"' % HUB in text
                assert 'vid_pid = "2109:0817"' in text
                assert "self_powered = false" in text and "individual_b210_dc = true" in text
                # HW-01 (2026-09-15): this fixture IS the lab default topology
                # (expected serials + hub 2109:0817 observed), so the draft
                # prefills the expected role mapping and hub asset as
                # *expected* values; approval/attestations stay unwritten.
                assert 'asset_id = "HUB-VIA-2109-0817-01"' in text
                assert 'serial = "34D03DD"' in text.split("[endpoints.MASS-A]")[1].split("[endpoints.MASS-B]")[0]
                assert 'serial = "3515425"' in text.split("[endpoints.MASS-B]")[1].split("[endpoints.ROC]")[0]
                assert 'serial = "34D03E8"' in text.split("[endpoints.ROC]")[1].split("[wiring]")[0]
                assert "NOT approved" in text and "approved = false" in text
                assert draft["mismatches"] == [] and draft["expected_label"] == "lab-default"
                assert "hub port 1 / 5-1.1: serial 3515425 5000M" in text
                assert "external_power_asset" not in text.split("[endpoints.MASS-A]")[1].split("[wiring]")[0].replace("5000M on the hub — no external_power_asset here", "")
            else:
                assert "schema_version = 1" in text
                assert 'external_power_asset = "REQUIRED_FILL"' in text
                assert "hardware_profile" not in text
            # the draft never validates as written (approval + attestations pending).
            try:
                local_config.parse_hardware_declaration(paths.root / "hardware.toml")
                raise AssertionError("draft must not validate")
            except local_config.ConfigGenerationError:
                pass
            again = setup_cmd.draft_hardware_declaration(paths, observed, topology=topology)
            assert again["created"] is False

        # run_setup records the topology snapshot (no device-number binding) and
        # the hub candidate; venv/apt/launcher are skipped as in the existing
        # setup test, on the same minimal installation tree it uses.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_standalone_setup import Done, make_mini_root
        paths = state.StatePaths(make_mini_root(Path(tmp) / "setup"))
        report = setup_cmd.run_setup(
            paths, runner=lambda argv, **k: Done(0),
            usb_probe=lambda: local_preflight.probe_usb_serials(hub_sysfs),
            topology_probe=lambda: host_profiles.probe_usb_topology(hub_sysfs),
            apt_install=False, make_venv=False, install_launcher=False,
            crypto_available=lambda p: True)
        assert report["pass"]
        assert report["steps"]["hardware_draft"]["profile"] == HUB
        snapshot = json.loads((paths.var_dir / "setup-usb-topology.json").read_text(encoding="utf-8"))
        assert snapshot["artifact_type"] == "STAGE7_STANDALONE_SETUP_USB_TOPOLOGY_V1"
        assert snapshot["hub_candidate"]["candidate"] is True
        assert snapshot["hub_candidate"]["hub_vid_pid"] == "2109:0817"
        assert report["steps"]["b210_discovery"]["hub_candidate"]["b210_count"] == 3

        # draft → operator fill → parse → preflight → loader: one contract.
        filled = hub_decl()
        parsed = local_config.parse_hardware_declaration(write_toml(tmp + "/setup", filled))
        preflight = local_preflight.run_required_preflight(
            **preflight_kwargs(tmp + "/setup", parsed, hub_sysfs))
        assert preflight["pass"], preflight["failures"]
        usb_paths = {role: info["usb_path"] for role, info in
                     preflight["checks"]["b210"]["observed_by_role"].items()}
        assert usb_paths == hub_usb_paths()
        generated = generate(tmp + "/setup", parsed, usb_paths=usb_paths)
        assert generated["resolved_sha256"].keys() == {"mass-a", "mass-b", "roc"}


# ---------------------------------------------------------------- 결속: reference / run

def _fake_preflight(sysfs, hardware=None):
    with tempfile.TemporaryDirectory() as tmp:
        return local_preflight.run_required_preflight(
            **preflight_kwargs(tmp, hardware or HUB_HARDWARE, sysfs))


def _issue(record, preflight, hardware, **over):
    endpoints_observed, observations = qualify_cmd.critical_endpoints_from_preflight(preflight)
    kwargs = dict(
        ladder_id="qual-pi-hub-test", installation=record,
        tc_results={"TC-STAGE7-RR-001": "PASS", "TC-STAGE7-RR-002": "PASS",
                    "TC-STAGE7-RR-003": "PASS", "TC-STAGE7-RR-004": "PASS"},
        endpoints_observed=endpoints_observed,
        platform_profile=hardware["platform_profile"],
        wiring=hardware["wiring"], identity_fingerprint="dbe31e3e41cd",
        gain={"artifact_sha256": "ab" * 32, "artifact_input_sha256": "cd" * 32,
              "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69, "ROC": 69},
              "source_run_id": "qual-x-rr-p1"},
        environment_manifest=dict(ENV_MANIFEST),
        environment_manifest_sha256="ef" * 32,
        phase_run_ids={"rr-p0": "qual-pi-hub-test-rr-p0"},
        endpoint_observations=observations,
        host_profile=hardware_profiles.profile_of(hardware),
        usb_topology=(hardware_profiles.critical_topology(hardware)
                      if hardware_profiles.is_pi_powered_hub(hardware) else None),
        hardware_observations=(hardware_profiles.topology_observations(preflight)
                               if hardware_profiles.is_pi_powered_hub(hardware) else None))
    kwargs.update(over)
    return qualify_cmd.issue_reference(**kwargs)


def test_reference_binds_hub_topology_and_refuses_relaxation():
    with tempfile.TemporaryDirectory() as tmp:
        record = installation_manifest.measure(REPO)
        preflight = _fake_preflight(make_topology(tmp))
        reference = _issue(record, preflight, HUB_HARDWARE)
        critical = reference["standalone_critical"]
        assert critical["host_profile"] == HUB
        assert critical["usb_topology"] == hardware_profiles.critical_topology(HUB_HARDWARE)
        assert critical["endpoints"]["MASS-B"]["speed_class"] == "super"
        assert reference["hardware_observations"]["hub"]["descriptor_self_powered"] is True
        assert reference["hardware_observations"]["endpoints"]["MASS-B"]["usb_path"] == "5-1.1"
        # legacy issuance keeps its shape (no profile keys at all).
        legacy_pf = _fake_preflight(make_topology(
            tmp + "/legacy", hub=None, roots=("usb3", "usb4", "usb5"),
            b210=(("3-1", "34D03DD", 5000), ("4-2", "3515425", 480), ("5-1", "34D03E8", 5000))),
            LEGACY_HARDWARE)
        legacy_ref = _issue(record, legacy_pf, LEGACY_HARDWARE, host_profile=None,
                            usb_topology=None, hardware_observations=None)
        assert "host_profile" not in legacy_ref["standalone_critical"]
        assert "usb_topology" not in legacy_ref["standalone_critical"]
        assert "hardware_observations" not in legacy_ref
        for over, needle in ((dict(usb_topology=None), "requires the USB hub topology"),
                             (dict(host_profile="PI_POWERED_USB3_HUB_UNKNOWN"), "unknown host_profile")):
            try:
                _issue(record, preflight, HUB_HARDWARE, **over)
                raise AssertionError("must be refused: %s" % needle)
            except qualify_cmd.QualifyError as exc:
                assert needle in str(exc), (needle, str(exc))
        degraded = json.loads(json.dumps(preflight))
        degraded["checks"]["b210"]["observed_by_role"]["MASS-B"].update(
            speed_class="high", speed_mbps=480)
        try:
            _issue(record, degraded, HUB_HARDWARE)
            raise AssertionError("high-speed endpoint accepted under the hub profile")
        except qualify_cmd.QualifyError as exc:
            assert "super-speed (5000M) link on every endpoint: ['MASS-B']" in str(exc)
        # the written reference (with the additive keys) passes the real
        # reference verifier and the active-pointer loader byte-for-byte.
        paths = state.StatePaths(Path(tmp) / "root")
        paths.ensure_var()
        release = paths.qualification_candidate
        written = qualify_cmd.write_reference(release, reference)
        assert written["validated_run_id"] == "qual-pi-hub-test"
        promoted = qualify_cmd.promote_candidate(paths, release_id="qual-pi-hub-test")
        active = qualify_cmd.load_active_reference(paths)
        assert active is not None and active["_reference_sha256"] == written["sha256"]
        assert active["standalone_critical"]["host_profile"] == HUB
        assert active["standalone_critical"]["usb_topology"]["hub"]["asset_id"] == (
            "HUB-VIA-2109-0817-01")
        assert promoted["release"] == "qual-pi-hub-test"


def test_compare_critical_enforces_profile_and_hub_topology():
    with tempfile.TemporaryDirectory() as tmp:
        record = installation_manifest.measure(REPO)
        sysfs = make_topology(tmp)
        preflight = _fake_preflight(sysfs)
        reference = _issue(record, preflight, HUB_HARDWARE)
        base = dict(installation=record, hardware=HUB_HARDWARE, preflight=preflight,
                    identity_fingerprint="dbe31e3e41cd",
                    environment_manifest=dict(ENV_MANIFEST))
        assert run_cmd.compare_critical(reference, **base)["pass"], run_cmd.compare_critical(
            reference, **base)["mismatches"]

        def mismatches(**over):
            kwargs = dict(base)
            kwargs.update(over)
            return run_cmd.compare_critical(reference, **kwargs)["mismatches"]

        other_asset = hub_decl(lambda d: d["usb_hub"].update(asset_id="HUB-OTHER-02"))
        assert any("asset_id 'HUB-VIA-2109-0817-01' qualified, 'HUB-OTHER-02' declared" in m
                   for m in mismatches(hardware=other_asset))
        other_vid = hub_decl(lambda d: d["usb_hub"].update(vid_pid="05e3:0626"))
        assert any("vid_pid" in m for m in mismatches(hardware=other_vid))
        assert any("host_profile" in m for m in mismatches(hardware=LEGACY_HARDWARE))
        failed_topology = json.loads(json.dumps(preflight))
        failed_topology["checks"]["usb_topology"].update(
            {"pass": False, "errors": ["hub 5-1 USB descriptor does not report self-powered"]})
        assert any("USB hub topology preflight did not pass" in m
                   for m in mismatches(preflight=failed_topology))
        # a legacy reference never qualifies a powered-hub declaration and vice versa.
        legacy_reference = json.loads(json.dumps(reference))
        legacy_reference["standalone_critical"].pop("host_profile")
        legacy_reference["standalone_critical"].pop("usb_topology")
        text = " | ".join(run_cmd.compare_critical(legacy_reference, **base)["mismatches"])
        assert "host_profile 'STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1' qualified" in text
        no_topology = json.loads(json.dumps(reference))
        no_topology["standalone_critical"].pop("usb_topology")
        assert any("carries no USB hub topology binding" in m
                   for m in run_cmd.compare_critical(no_topology, **base)["mismatches"])


def test_run_topology_after_close_is_recorded_and_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        good = make_topology(tmp + "/good")
        after = run_cmd._topology_after_close(
            HUB_HARDWARE, lambda: host_profiles.probe_usb_topology(good))
        assert after["pass"] and after["hub"]["usb_path"] == "5-1"
        line = run_cmd.topology_after_line(after)
        assert line.startswith("[topology]  after close: B210s rediscovered below the approved hub 5-1")
        assert "MASS-B 3515425@5-1.1 5000M" in line
        bad = make_topology(tmp + "/bad", b210=(("5-1.1", "3515425", 480),
                                                 ("5-1.2", "34D03DD", 5000),
                                                 ("5-1.4", "34D03E8", 5000)))
        after_bad = run_cmd._topology_after_close(
            HUB_HARDWARE, lambda: host_profiles.probe_usb_topology(bad))
        assert not after_bad["pass"]
        assert run_cmd.topology_after_line(after_bad).startswith("[topology]  after close: PROBLEMS")

        def broken():
            raise OSError("no sysfs")
        assert run_cmd._topology_after_close(HUB_HARDWARE, broken)["pass"] is False


# ---------------------------------------------------------------- 14. manifest 영역·digest

def test_new_files_classify_into_bound_domains_and_digests_are_deterministic():
    assert qd.classify_domains("VDETER/src/vdeter/b210_system/host_profiles.py") == (qd.RF,)
    for endpoint in ("mass-a", "mass-b", "roc"):
        assert qd.classify_domains(
            f"VDETER/configs/stage7/endpoints-pi-powered-usb3-hub/{endpoint}.toml") == (qd.RF,)
    assert qd.classify_domains("apps/standalone/hardware_profiles.py") == (qd.RF, qd.OPERATION)
    assert qd.classify_domains(
        "apps/standalone/templates/hardware.pi-powered-usb3-hub.toml.example") == (qd.TOOLING,)
    assert qd.classify_domains("tests/test_standalone_pi_powered_hub.py") == (qd.TOOLING,)
    first = installation_manifest.measure(REPO, now="T0")
    second = installation_manifest.measure(REPO, now="T1")
    assert installation_manifest.digests_of(first) == installation_manifest.digests_of(second)
    files = first["files"]
    assert files["VDETER/src/vdeter/b210_system/host_profiles.py"]["domains"] == ["rf"]
    assert files["apps/standalone/hardware_profiles.py"]["domains"] == ["rf", "operation"]
    for endpoint in ("mass-a", "mass-b", "roc"):
        path = f"VDETER/configs/stage7/endpoints-pi-powered-usb3-hub/{endpoint}.toml"
        assert files[path]["domains"] == ["rf"]
        assert files[f"VDETER/configs/stage7/endpoints/{endpoint}.toml"]["domains"] == ["rf"]
    rf_files = installation_manifest.domain_manifest(first, qd.RF)["files"]
    assert "VDETER/src/vdeter/b210_system/host_profiles.py" in rf_files
    assert "VDETER/configs/stage7/endpoints-pi-powered-usb3-hub/mass-b.toml" in rf_files


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Pi powered-USB3-hub profile RF-off contract")
