"""hardware.toml draft (HW-01, 2026-09-15) — RF-off contract tests.

증명하는 것 (요구사항 R6-01~07, 설계 보완서 §2)
  기본 프로필  : 실험실 topology(기대 serial 3종 + hub 2109:0817)가 관측되면 역할별
                 기대 매핑·hub asset(기대값)을 채운다. 배선·전원은 미확인 그대로.
  누락·불일치  : 기대 serial 미관측 역할은 REQUIRED_FILL + 사유, 예상 밖 장비는
                 어느 역할에도 배정하지 않는다. hub VID:PID 불일치는 asset을 채우지 않는다.
  다른 환경    : 기대 topology가 아니면 legacy(schema 1) 초안 구조 그대로 + 사유만.
                 expected=None이면 기대값·사유 없이 종전 초안.
  미승인 거부  : 어떤 초안도 그대로는 parse를 통과하지 않는다(approved·전원 증언·source).
  기존 파일    : 존재하면 바이트 하나 바꾸지 않는다.
  증언 미조작  : 활성 줄에 self_powered=true / individual_b210_dc=false /
                 approved=true / *_source= / 서명·날짜 키가 없다.
  운영자 완성  : 초안을 운영자가 완성하면(값 채움·source 해제·approved) parse 통과 —
                 초안이 유효한 출발점임을 보장한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"), os.path.join(_REPO, "VDETER", "src"),
               os.path.dirname(os.path.abspath(__file__))):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (  # noqa: E402
    hardware_draft as hd,
    hardware_profiles,
    local_config,
    local_preflight,
    setup_cmd,
    state,
)
from vdeter.b210_system import host_profiles  # noqa: E402
from test_standalone_pi_powered_hub import make_topology  # noqa: E402

ROLES = ("MASS-A", "MASS-B", "ROC")
LAB = hd.LAB_DEFAULT
LAB_B210 = (("5-1.1", "3515425", 5000), ("5-1.2", "34D03DD", 5000), ("5-1.4", "34D03E8", 5000))


# ---------------------------------------------------------------- helpers

def _observe(sysfs):
    return (local_preflight.probe_usb_serials(sysfs),
            hd.hub_draft_candidate(host_profiles.probe_usb_topology(sysfs)))


def _build(sysfs, expected=LAB):
    observed, candidate = _observe(sysfs)
    return hd.build_hardware_draft(observed=observed, candidate=candidate, expected=expected)


def _active_lines(text: str) -> list:
    """주석·빈 줄을 뺀 실제 TOML 줄(줄 끝 주석 제거)."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped.split("#", 1)[0].strip())
    return lines


def _section(text: str, header: str, next_header: str) -> str:
    return text.split(header)[1].split(next_header)[0]


def _write_and_parse_error(tmp, text: str) -> str:
    path = Path(tmp) / "hardware.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        local_config.parse_hardware_declaration(path)
    except local_config.ConfigGenerationError as exc:
        return str(exc)
    raise AssertionError("draft must not validate")


def _assert_no_attestation(text: str) -> None:
    active = _active_lines(text)
    assert "self_powered = true" not in active
    assert "individual_b210_dc = false" not in active
    assert "approved = true" not in active
    assert not any(line.startswith(("self_powered_source", "individual_b210_dc_source"))
                   for line in active), active
    assert "operator_attested" not in " ".join(active)
    # 서명·날짜·증언 시각 키를 만들지 않는다 ("assigned" 같은 단어 일치를 피해 단어 경계로 본다).
    forbidden = re.compile(
        r"\b(signed|signature|attested_at|approved_at|attested_by|signed_by)\b|^\s*date\s*=",
        re.IGNORECASE | re.MULTILINE)
    assert not forbidden.search(text), forbidden.search(text).group(0)


# ---------------------------------------------------------------- 1. 기본 프로필 일치

def test_lab_default_match_prefills_expected_mapping_only():
    with tempfile.TemporaryDirectory() as tmp:
        draft = _build(make_topology(tmp))
        assert draft.profile == hardware_profiles.PI_POWERED_HUB_PROFILE
        assert draft.expected_label == "lab-default"
        assert draft.mismatches == []
        assert draft.expected_applied == {
            "endpoints": {"MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"},
            "usb_hub.asset_id": "HUB-VIA-2109-0817-01",
            "wiring.profile": "C1-C6-STANDARD",
        }
        text = draft.text
        assert 'serial = "34D03DD"' in _section(text, "[endpoints.MASS-A]", "[endpoints.MASS-B]")
        assert 'serial = "3515425"' in _section(text, "[endpoints.MASS-B]", "[endpoints.ROC]")
        assert 'serial = "34D03E8"' in _section(text, "[endpoints.ROC]", "[wiring]")
        assert 'asset_id = "HUB-VIA-2109-0817-01"' in text
        assert 'vid_pid = "2109:0817"' in text and "observed" in text.split('vid_pid = "2109:0817"')[1].splitlines()[0]
        assert 'profile = "C1-C6-STANDARD"' in text and "unverified until approved" in text
        # 기대값은 기대값이라고 적혀 있고, 배선은 확인된 것으로 적히지 않는다.
        assert "expected (lab-default) role mapping" in text
        assert "NOT confirmed by this line" in text
        assert "cannot be verified by software" in text
        assert text.startswith("# VDETER standalone hardware declaration — DRAFT written by `vdeter setup`; NOT approved.")
        _assert_no_attestation(text)
        # 초안 그대로는 거부 — approved·전원 증언·source 누락이 전부 사유에 있다.
        error = _write_and_parse_error(tmp, text)
        for needle in ("wiring.approved is not true", "self_powered must be true",
                       "individual_b210_dc must be false", "self_powered_source is missing",
                       "individual_b210_dc_source is missing"):
            assert needle in error, (needle, error)
        assert "REQUIRED_FILL" not in error  # 기대값은 채워졌으므로 기입 안내는 없다


# ---------------------------------------------------------------- 2. 누락·불일치

def test_missing_expected_device_is_left_unverified_and_no_substitute_is_assigned():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp, b210=(("5-1.1", "3515425", 5000), ("5-1.2", "34D03DD", 5000),
                                         ("5-1.4", "9999999", 5000)))
        draft = _build(sysfs)
        assert draft.profile == hardware_profiles.PI_POWERED_HUB_PROFILE
        applied = draft.expected_applied
        assert applied["endpoints"] == {"MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": None}
        assert applied["usb_hub.asset_id"] is None
        text = draft.text
        roc = _section(text, "[endpoints.ROC]", "[wiring]")
        assert 'serial = "REQUIRED_FILL"' in roc and "expected 34D03E8" in roc
        assert "9999999" not in _active_lines(text).__str__()   # 다른 장비 자동 배정 없음
        assert 'asset_id = "REQUIRED_FILL"' in text
        joined = " | ".join(draft.mismatches)
        assert "ROC: expected serial 34D03E8 (lab-default) is not observed" in joined
        assert "unexpected B210 serial 9999999 at 5-1.4" in joined and "not assigned" in joined
        assert "hub asset ID HUB-VIA-2109-0817-01 (lab-default) not applied" in joined
        # 초안 파일 안에도 같은 사유가 남는다.
        for note in draft.mismatches:
            assert note in text
        _assert_no_attestation(text)
        error = _write_and_parse_error(tmp, text)
        assert "endpoints.ROC.serial is still the draft value" in error


def test_hub_vid_pid_mismatch_keeps_asset_unfilled_but_keeps_observed_vid_pid():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp, hub=("5-1", "0bda:0411", 5000, "e0"))
        draft = _build(sysfs)
        assert draft.profile == hardware_profiles.PI_POWERED_HUB_PROFILE
        assert draft.expected_applied["endpoints"] == {
            "MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"}
        assert draft.expected_applied["usb_hub.asset_id"] is None
        assert 'vid_pid = "0bda:0411"' in draft.text          # 관측값 그대로
        assert 'asset_id = "REQUIRED_FILL"' in draft.text
        assert any("hub VID:PID observed 0bda:0411, expected 2109:0817" in m for m in draft.mismatches)
        assert "HUB-VIA-2109-0817-01" not in " ".join(_active_lines(draft.text))


# ---------------------------------------------------------------- 3. 다른 환경

def test_other_site_with_unknown_serials_prefills_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp, b210=(("5-1.1", "AAAAAAA", 5000), ("5-1.2", "BBBBBBB", 5000),
                                         ("5-1.4", "CCCCCCC", 5000)))
        draft = _build(sysfs)
        assert draft.profile == hardware_profiles.PI_POWERED_HUB_PROFILE
        assert draft.expected_applied["endpoints"] == {role: None for role in ROLES}
        assert draft.expected_applied["usb_hub.asset_id"] is None
        active = " ".join(_active_lines(draft.text))
        for serial in ("34D03DD", "3515425", "34D03E8", "AAAAAAA", "BBBBBBB", "CCCCCCC"):
            assert serial not in active                       # 어느 serial도 배정되지 않음
        assert active.count('serial = "REQUIRED_FILL"') == 3
        assert 'asset_id = "REQUIRED_FILL"' in draft.text
        assert len([m for m in draft.mismatches if "not observed below the hub" in m]) == 3
        assert len([m for m in draft.mismatches if "unexpected B210 serial" in m]) == 3
        # 관측 목록은 주석으로 남는다(참고용).
        assert "serial AAAAAAA 5000M" in draft.text
        _assert_no_attestation(draft.text)


def test_non_hub_topology_writes_legacy_draft_with_reasons_and_no_prefill():
    with tempfile.TemporaryDirectory() as tmp:
        # 우리 serial 3종이 root 포트에 직결된 legacy 배치 — 기대 topology(hub) 아님.
        sysfs = make_topology(tmp, hub=None, roots=("usb3", "usb4", "usb5"),
                              b210=(("3-1", "34D03DD", 5000), ("4-2", "3515425", 480),
                                    ("5-1", "34D03E8", 5000)))
        draft = _build(sysfs)
        assert draft.profile == hardware_profiles.LEGACY_PI_PROFILE
        text = draft.text
        assert "schema_version = 1" in text and "hardware_profile" not in text
        assert "usb_hub" not in text
        active = _active_lines(text)
        assert active.count('serial = "REQUIRED_FILL"') == 3
        assert 'external_power_asset = "REQUIRED_FILL"' in active
        assert "approved = false" in active
        assert draft.expected_applied["endpoints"] == {role: None for role in ROLES}
        assert len(draft.mismatches) == 1 and "not observed" in draft.mismatches[0]
        assert "Expected profile lab-default" in text and "NOT observed" in text
        assert "not below one common USB hub" in text or "root controller" in text
        _assert_no_attestation(text)
        _write_and_parse_error(tmp, text)


def test_no_expected_profile_keeps_plain_draft_shape():
    with tempfile.TemporaryDirectory() as tmp:
        hub = _build(make_topology(tmp), expected=None)
        assert hub.expected_label is None and hub.mismatches == []
        assert hub.expected_applied["endpoints"] == {role: None for role in ROLES}
        active = " ".join(_active_lines(hub.text))
        assert active.count('serial = "REQUIRED_FILL"') == 3
        assert 'asset_id = "REQUIRED_FILL"' in active
        assert "lab-default" not in hub.text and "Expected profile" not in hub.text
        assert 'vid_pid = "2109:0817"' in hub.text
        legacy = hd.build_hardware_draft(observed={}, candidate=None, expected=None)
        assert legacy.profile == hardware_profiles.LEGACY_PI_PROFILE
        assert "(no USB serial detected" in legacy.text and "Expected profile" not in legacy.text
        assert legacy.mismatches == []


def test_custom_expected_profile_is_an_explicit_input():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp, hub=("5-1", "0bda:0411", 5000, "e0"),
                              b210=(("5-1.1", "AAAAAAA", 5000), ("5-1.2", "BBBBBBB", 5000),
                                    ("5-1.4", "CCCCCCC", 5000)))
        other = hd.ExpectedHardware(
            label="site-B", host_profile=hardware_profiles.PI_POWERED_HUB_PROFILE,
            serial_by_role={"MASS-A": "AAAAAAA", "MASS-B": "BBBBBBB", "ROC": "CCCCCCC"},
            hub_asset_id="HUB-SITE-B", hub_vid_pid="0bda:0411", wiring_profile="C1-C6-STANDARD")
        draft = _build(sysfs, expected=other)
        assert draft.mismatches == []
        assert draft.expected_applied["endpoints"] == other.serial_by_role
        assert 'asset_id = "HUB-SITE-B"' in draft.text
        assert "expected (site-B) role mapping" in draft.text
        _assert_no_attestation(draft.text)
        # 같은 관측을 기본(lab-default) 기대로 보면 아무것도 채우지 않는다.
        lab = _build(sysfs)
        assert lab.expected_applied["endpoints"] == {role: None for role in ROLES}
        assert lab.expected_applied["usb_hub.asset_id"] is None


# ---------------------------------------------------------------- 4. 미승인 거부 · 운영자 완성

def test_operator_completion_turns_the_prefilled_draft_into_a_valid_declaration():
    with tempfile.TemporaryDirectory() as tmp:
        text = _build(make_topology(tmp)).text
        # approved만 켜도 여전히 거부(전원 증언·source 미완).
        partial = text.replace("approved = false", "approved = true")
        error = _write_and_parse_error(tmp, partial)
        assert "self_powered must be true" in error and "self_powered_source is missing" in error
        # 운영자 완성: bool 변경 + source 줄 해제 → 통과 (초안이 유효한 출발점).
        completed = (partial
                     .replace("self_powered = false", "self_powered = true")
                     .replace("individual_b210_dc = true", "individual_b210_dc = false")
                     .replace('# self_powered_source = "operator_attested"',
                              'self_powered_source = "operator_attested"')
                     .replace('# individual_b210_dc_source = "operator_attested"',
                              'individual_b210_dc_source = "operator_attested"'))
        path = Path(tmp) / "hardware.toml"
        path.write_text(completed, encoding="utf-8", newline="\n")
        parsed = local_config.parse_hardware_declaration(path)
        assert parsed["endpoints"] == {"MASS-A": {"serial": "34D03DD"},
                                       "MASS-B": {"serial": "3515425"},
                                       "ROC": {"serial": "34D03E8"}}
        assert parsed["usb_hub"]["asset_id"] == "HUB-VIA-2109-0817-01"
        assert parsed["wiring"] == {"profile": "C1-C6-STANDARD", "approved": True}
        assert hardware_profiles.critical_topology(parsed)["hub"] == {
            "asset_id": "HUB-VIA-2109-0817-01", "vid_pid": "2109:0817"}


# ---------------------------------------------------------------- 5. 기존 파일 보존 · setup 통합

def test_existing_hardware_toml_is_never_touched():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp)
        paths = state.StatePaths(Path(tmp) / "root")
        paths.ensure_var()
        existing = (paths.root / "hardware.toml")
        body = "# operator file\nschema_version = 2\napproved_by_someone = true\n"
        existing.write_text(body, encoding="utf-8", newline="\n")
        before = hashlib.sha256(existing.read_bytes()).hexdigest()
        mtime = existing.stat().st_mtime_ns
        observed, candidate = _observe(sysfs)
        result = setup_cmd.draft_hardware_declaration(paths, observed, candidate=candidate)
        assert result == {"created": False, "path": str(existing), "profile": None,
                          "expected_label": None, "expected_applied": {}, "mismatches": []}
        assert hashlib.sha256(existing.read_bytes()).hexdigest() == before
        assert existing.stat().st_mtime_ns == mtime


def test_run_setup_records_expected_decisions_and_stays_idempotent():
    from test_standalone_setup import Done, make_mini_root

    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_topology(tmp)
        paths = state.StatePaths(make_mini_root(Path(tmp) / "setup"))
        kwargs = dict(
            runner=lambda argv, **k: Done(0),
            usb_probe=lambda: local_preflight.probe_usb_serials(sysfs),
            topology_probe=lambda: host_profiles.probe_usb_topology(sysfs),
            apt_install=False, make_venv=False, install_launcher=False,
            crypto_available=lambda p: True)
        report = setup_cmd.run_setup(paths, **kwargs)
        assert report["pass"]
        draft = report["steps"]["hardware_draft"]
        assert draft["created"] and draft["profile"] == hardware_profiles.PI_POWERED_HUB_PROFILE
        assert draft["expected_label"] == "lab-default" and draft["mismatches"] == []
        assert draft["expected_applied"]["endpoints"] == {
            "MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"}
        snapshot = json.loads((paths.var_dir / "setup-usb-topology.json").read_text(encoding="utf-8"))
        assert snapshot["hub_candidate"]["candidate"] is True
        assert snapshot["hardware_draft"]["expected_applied"] == draft["expected_applied"]
        assert snapshot["hardware_draft"]["mismatches"] == []
        assert "never written by setup" in snapshot["hardware_draft"]["note"]
        text = (paths.root / "hardware.toml").read_text(encoding="utf-8")
        _assert_no_attestation(text)
        sha = hashlib.sha256((paths.root / "hardware.toml").read_bytes()).hexdigest()
        # 재실행: 초안 무변경, 결정 기록은 created=False.
        again = setup_cmd.run_setup(paths, **kwargs)
        assert again["steps"]["hardware_draft"]["created"] is False
        assert hashlib.sha256((paths.root / "hardware.toml").read_bytes()).hexdigest() == sha
        # 다른 환경(기대 없음)에서도 같은 orchestration이 동작한다.
        paths2 = state.StatePaths(make_mini_root(Path(tmp) / "plain"))
        report2 = setup_cmd.run_setup(paths2, expected_hardware=None, **kwargs)
        assert report2["steps"]["hardware_draft"]["expected_label"] is None
        assert report2["steps"]["hardware_draft"]["mismatches"] == []
        text2 = (paths2.root / "hardware.toml").read_text(encoding="utf-8")
        assert 'serial = "REQUIRED_FILL"' in text2 and "lab-default" not in text2


def test_draft_text_never_contains_an_active_source_token_or_timestamp():
    with tempfile.TemporaryDirectory() as tmp:
        for sysfs in (make_topology(tmp + "/a"),
                      make_topology(tmp + "/b", hub=None, roots=("usb3", "usb4", "usb5"),
                                    b210=(("3-1", "34D03DD", 5000), ("4-2", "3515425", 480),
                                          ("5-1", "34D03E8", 5000)))):
            text = _build(sysfs).text
            _assert_no_attestation(text)
            assert not re.search(r"\d{4}-\d{2}-\d{2}", text)   # 날짜 자동 생성 없음
