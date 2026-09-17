"""readiness state store 계약 시험 (spec §8, §9, §12 — RF·장치·qualification namespace 없음).

증명하는 것
  digest    : canonical JSON golden, key 순서 무관, 정수 무손실, float/NaN 거부, 변조 시 digest 변화,
              제외 필드가 binding에 들어오면 거부
  migration : 최초 state(generation=1, active=null, inhibit=null) 원자 생성·멱등, 손상 state 재생성 금지,
              marker 없는 설치 = MIGRATION_REQUIRED, marker 있는데 state 없음 = STATE_UNREADABLE(fail-closed)
  reader    : lock 없이 snapshot, ENOENT/불완전 JSON 50 ms × 3회 재시도, .tmp 잔존 무시
  writer    : lock 필수, candidate→release rename→state 원자 교체(generation +1, active 교체, inhibit 해제),
              stale writer 거부(state 불변, release는 ORPHANED_UNPROMOTED), 중복 release id 거부
  crash     : rename 전 crash = candidate 잔존·state 불변, rename 후 crash = valid orphan 보존·무자동승격
  inhibit   : record 먼저 완결 → state에 inhibit 추가(active 보존) → INHIBITED; 변조 = STATE_CORRUPT;
              orphan inhibit = INHIBITED(fail-closed); 복구 = 새 release 승격 하나의 state 교체
  applicab. : 같은 binding = APPLICABLE, 필드 차이 = STALE(사유), 투영 QUALIFIED/OPERATIONAL_READY/UNVERIFIED
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _entry in (_REPO, os.path.join(_REPO, "VDETER"), os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import readiness as rd  # noqa: E402
from apps.standalone import state as state_module  # noqa: E402


# ---------------------------------------------------------------- 재료

def binding(**over) -> dict:
    base = {
        "installation": {"rf_manifest_sha256": "a1" * 32, "operation_manifest_sha256": "b2" * 32,
                         "readiness_closure_sha256": "c3" * 32,
                         "rf_critical_symbols_sha256": "d4" * 32},
        "identity_fingerprint": "0011223344aa",
        "platform": {"platform_profile": "pi", "host_profile": "STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1",
                     "machine_id_sha256": "e5" * 32,
                     "boot_check_policy_id": "VDETER_READINESS_BOOT_CHECK_V1"},
        "hardware": {"endpoints": {"MASS-A": {"serial": "34D03DD", "speed_class": "super",
                                              "usb_transport": "PI_NATIVE_USB3_5000M"}},
                     "usb_topology": None},
        "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
        "rf_config": {"template_digest_sha256": "f6" * 32,
                      "channels": {"VDETER_UPPER_100_TEST": {"center_frequency_hz": 161837500,
                                                             "sample_rate_hz": 307200}}},
        "calibration": {"artifact_sha256": "ab" * 32, "artifact_input_sha256": "cd" * 32,
                        "source": "qualification_reference:ef" + "ef" * 31,
                        "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69, "ROC": 75},
                        "calibration_policy_id": "STAGE7_RR_P1_ADAPTIVE_SEARCH_AND_SEAL_V1"},
        "policy_ids": {"classifier": "VDETER_READINESS_FAILURE_CLASSIFIER_V1",
                       "host_time": "STAGE7_PI_HOST_AFFINE_TIME_V1/RESTART_SERIES_V1",
                       "rf_time": "STAGE7_RF_TIME_SEQUENTIAL_V1",
                       "probe": "VDETER_READINESS_LINK17_PROBE_V1",
                       "safety": "VDETER_READINESS_SAFETY_V1",
                       "closure": "VDETER_READINESS_EXECUTION_CLOSURE_V1"},
        "probe_plan_sha256": "12" * 32,
    }
    base.update(over)
    return base


def record_for(readiness_id, bind=None, status="PASS"):
    return rd.build_readiness_record(
        readiness_id=readiness_id, binding=bind or binding(), status=status,
        record_metadata={"source_run_id": "run-x"},
        preparation_checks={"preflight": {"pass": True}, "host_time": {"pass": True},
                            "rf_time_pre": {"pass": True}, "active_bridge": {"pass": True},
                            "rf_time_post": {"pass": True}, "link17_bidirectional": {"pass": True},
                            "pre_workload_quiescence": {"pass": True}},
        execution_close={"cleanup": {"pass": True}, "post_close_discovery": {"pass": True}},
        session={"boot_id": "boot-1", "session_id": "run-x"})


def marked_installation() -> dict:
    return {"artifact_type": "STAGE7_STANDALONE_INSTALLATION_MANIFEST_V2", "files": {},
            **rd.MANIFEST_MARKER}


class Env:
    def __init__(self, tmp):
        self.paths = state_module.StatePaths(tmp)
        self.paths.ensure_var()
        self.lock = state_module.SingleInstanceLock(self.paths.lock_path)
        self.lock.acquire()

    def load(self, installation=None):
        return rd.load_readiness_state(self.paths, installation_record=installation
                                       or marked_installation(), sleeper=lambda _s: None)

    def write_candidate(self, readiness_id, bind=None, status="PASS"):
        return rd.write_immutable_record(
            self.paths.readiness_candidate / readiness_id, basename=rd.RECORD_BASENAME,
            record=record_for(readiness_id, bind, status),
            evidence={"summary.json": b'{"ok": true}\n', "sub/run.json": b"{}\n"},
            producer="test")

    def writer(self):
        return rd.ReadinessWriter(self.paths, self.lock)


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(tmp)
        rd.ensure_initial_state(e.paths)
        yield e
        e.lock.release()


# ---------------------------------------------------------------- digest

def test_canonical_digest_golden_and_key_order_independent():
    b = binding()
    digest = rd.readiness_digest(b)
    # golden: canonical bytes는 encode_canonical_json 규칙 그대로다.
    from vdeter.b210_system.canonical_json import encode_canonical_json
    assert digest == hashlib.sha256(encode_canonical_json(b)).hexdigest()
    reordered = json.loads(json.dumps(b, sort_keys=False))
    reordered["policy_ids"] = dict(reversed(list(reordered["policy_ids"].items())))
    assert rd.readiness_digest(reordered) == digest
    assert len(digest) == 64
    # 정수 무손실: 큰 정수도 문자열이 아니라 정수로 남는다.
    big = binding(rf_config={"template_digest_sha256": "f6" * 32, "big": 2 ** 70})
    assert rd.readiness_digest(big) != digest
    assert b"1180591620717411303424" in rd.canonical_bytes(big)


def test_digest_rejects_float_nan_duplicate_and_excluded_fields():
    with pytest.raises(rd.ReadinessError):
        rd.readiness_digest(binding(rf_config={"x": 1.5}))
    with pytest.raises(rd.ReadinessError):
        rd.readiness_digest(binding(rf_config={"x": float("nan")}))
    with pytest.raises(rd.ReadinessError):
        rd.readiness_digest(binding(session={"boot_id": "x"}))   # 제외 필드는 binding에 못 들어온다
    with pytest.raises(rd.ReadinessError):
        rd.readiness_digest({k: v for k, v in binding().items() if k != "calibration"})
    # 중복 key는 canonical decoder가 거부한다 (json.loads 경유 검증).
    from vdeter.b210_system.canonical_json import decode_canonical_json
    with pytest.raises(Exception):
        decode_canonical_json(b'{"a":1,"a":2}')


def test_tamper_changes_digest_and_record_sha(env):
    info = env.write_candidate("ready-20260917T000000Z-00000001")
    tampered = copy.deepcopy(info["record"])
    tampered["binding"]["calibration"]["endpoint_common_rx_gain_db"]["ROC"] = 74
    assert rd.readiness_digest(tampered["binding"]) != info["record"]["readiness_digest_sha256"]
    assert rd.sha256_hex(rd.encode_record(tampered)) != info["sha256"]


# ---------------------------------------------------------------- migration / initial state

def test_initial_state_created_once_and_never_regenerated():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state_module.StatePaths(tmp)
        first = rd.ensure_initial_state(paths)
        assert first["created"] is True
        assert first["state"] == {"artifact_type": rd.READINESS_STATE_TYPE, "schema_version": 1,
                                  "generation": 1, "active": None, "inhibit": None,
                                  "promotion_history": [], "inhibit_history": [],
                                  "updated_utc": first["state"]["updated_utc"]}
        second = rd.ensure_initial_state(paths)
        assert second["created"] is False and second["state"]["generation"] == 1
        paths.readiness_state.write_text("{not json", encoding="utf-8")
        with pytest.raises(state_module.StateError):
            rd.ensure_initial_state(paths)       # 손상 state는 조용히 재생성하지 않는다
        assert not (paths.readiness_dir / "state.json.tmp").exists()


def test_loader_migration_required_without_marker(env):
    snap = env.load(installation={"files": {}})
    assert snap["status"] == "MIGRATION_REQUIRED" and snap["fail_closed"] is True
    assert rd.evaluate_applicability(snap, binding())["operational_readiness"] == "MIGRATION_REQUIRED"


def test_loader_state_missing_with_marker_is_unreadable_fail_closed(env):
    env.paths.readiness_state.unlink()
    snap = env.load()
    assert snap["status"] == "STATE_UNREADABLE"
    assert snap["fail_closed"] and snap["workload_rf_blocked"]
    assert snap["reason_code"] == "STATE_UNREADABLE"
    env.paths.readiness_state.write_text('{"artifact_type": "X"}', encoding="utf-8")
    snap = env.load()
    assert snap["status"] == "STATE_CORRUPT" and snap["fail_closed"]


def test_loader_normal_absent(env):
    snap = env.load()
    assert snap["status"] == "ABSENT" and not snap["fail_closed"] and not snap["workload_rf_blocked"]
    assert snap["generation"] == 1
    assert rd.project_authority(qualification_applicable=False,
                                readiness_applicable=False) == "UNVERIFIED"


# ---------------------------------------------------------------- reader

def test_reader_retries_enoent_then_gives_up(env):
    env.paths.readiness_state.unlink()
    sleeps = []
    read = rd.read_state_snapshot(env.paths.readiness_state, sleeper=sleeps.append)
    assert read["outcome"] == "MISSING" and read["attempts"] == 3
    assert sleeps == [0.05, 0.05]


def test_reader_recovers_from_incomplete_json_within_retries(env):
    good = env.paths.readiness_state.read_bytes()
    env.paths.readiness_state.write_bytes(good[: len(good) // 2])   # rename 경계의 불완전 파일 흉내
    calls = []

    def sleeper(_s):
        calls.append(1)
        if len(calls) == 2:
            env.paths.readiness_state.write_bytes(good)

    read = rd.read_state_snapshot(env.paths.readiness_state, sleeper=sleeper)
    assert read["outcome"] == "OK" and read["attempts"] == 3


def test_reader_ignores_tmp_residue_and_needs_no_lock(env):
    (env.paths.readiness_dir / "state.json.tmp").write_text("garbage", encoding="utf-8")
    env.lock.release()
    snap = rd.load_readiness_state(env.paths, installation_record=marked_installation())
    assert snap["status"] == "ABSENT"
    env.lock.acquire()


# ---------------------------------------------------------------- writer / promotion

def test_writer_requires_held_lock(env):
    free = state_module.SingleInstanceLock(env.paths.lock_path)
    with pytest.raises(rd.ReadinessError):
        rd.ReadinessWriter(env.paths, free)


def test_promote_creates_release_and_bumps_generation(env):
    rid = "ready-20260917T000000Z-0000aaaa"
    info = env.write_candidate(rid)
    assert (env.paths.readiness_candidate / rid / rd.RECORD_BASENAME).is_file()
    assert (env.paths.readiness_candidate / rid / "evidence" / "sub" / "run.json").is_file()
    new_state = env.writer().promote(rid, expected_generation=1)
    assert new_state["generation"] == 2
    assert new_state["active"]["release"] == rid
    assert new_state["active"]["record_sha256"] == info["sha256"]
    assert new_state["inhibit"] is None
    assert not (env.paths.readiness_candidate / rid).exists()
    assert (env.paths.readiness_releases / rid / rd.RECORD_BASENAME).is_file()
    snap = env.load()
    assert snap["status"] == "ACTIVE" and snap["active"]["valid"] is True
    assert snap["orphans"] == {"readiness": [], "inhibits": []}
    verdict = rd.evaluate_applicability(snap, binding())
    assert verdict["operational_readiness"] == "APPLICABLE" and verdict["digest_match"]
    assert rd.project_authority(qualification_applicable=False,
                                readiness_applicable=True) == "OPERATIONAL_READY"
    assert rd.project_authority(qualification_applicable=True,
                                readiness_applicable=True) == "QUALIFIED"


def test_promote_refuses_stale_generation_and_duplicate_release(env):
    rid = "ready-20260917T000000Z-0000bbbb"
    env.write_candidate(rid)
    before = env.paths.readiness_state.read_bytes()
    with pytest.raises(rd.StaleWriterError):
        env.writer().promote(rid, expected_generation=7)
    assert env.paths.readiness_state.read_bytes() == before        # state 불변
    assert (env.paths.readiness_candidate / rid).is_dir()          # candidate 그대로
    env.writer().promote(rid, expected_generation=1)
    # 같은 id를 새 candidate로 다시 쓰고 승격하면 거부 (동일 ID 재사용 금지).
    env.write_candidate(rid)
    with pytest.raises(rd.ReadinessError):
        env.writer().promote(rid, expected_generation=2)


def test_stale_writer_after_rename_leaves_valid_orphan_unpromoted(env, monkeypatch):
    rid = "ready-20260917T000000Z-0000cccc"
    env.write_candidate(rid)
    writer = env.writer()
    original = writer.snapshot
    calls = {"n": 0}

    def racing_snapshot():
        calls["n"] += 1
        state = original()
        if calls["n"] == 3:
            # rename 뒤·교체 직전에 다른 writer가 generation을 올린 상황.
            bumped = dict(state)
            bumped["generation"] = state["generation"] + 1
            rd.write_state_atomic(env.paths.readiness_state, bumped)
            return bumped
        return state

    monkeypatch.setattr(writer, "snapshot", racing_snapshot)
    with pytest.raises(rd.StaleWriterError):
        writer.promote(rid, expected_generation=1)
    snap = env.load()
    assert snap["status"] == "ABSENT"
    assert [o["status"] for o in snap["orphans"]["readiness"]] == ["ORPHANED_UNPROMOTED"]
    assert snap["fail_closed"] is False     # valid orphan은 차단 사유가 아니다
    # 다음 run은 항상 새 id를 쓰며 orphan을 자동 승격하지 않는다.
    new = "ready-20260917T000001Z-0000dddd"
    env.write_candidate(new)
    state = env.writer().promote(new, expected_generation=2)
    assert state["active"]["release"] == new


def test_generation_must_increase_by_exactly_one_and_be_integer(env):
    state = rd.read_state_snapshot(env.paths.readiness_state)["state"]
    for bad in (0, -1, "2", 1.5, True):
        broken = dict(state)
        broken["generation"] = bad
        assert rd.validate_state(broken)
    env.write_candidate("ready-20260917T000000Z-0000eeee")
    env.writer().promote("ready-20260917T000000Z-0000eeee", expected_generation=1)
    assert rd.read_state_snapshot(env.paths.readiness_state)["state"]["generation"] == 2


# ---------------------------------------------------------------- crash points

def test_crash_before_rename_keeps_candidate_and_state(env):
    rid = "ready-20260917T000000Z-0000ffff"
    env.write_candidate(rid)
    snap = env.load()
    assert snap["status"] == "ABSENT" and snap["orphans"]["readiness"] == []
    # candidate 디렉터리는 active가 아니며 loader는 candidate/를 release로 읽지 않는다.
    assert (env.paths.readiness_candidate / rid).is_dir()


def test_crash_after_rename_before_state_replace_is_valid_orphan(env):
    rid = "ready-20260917T000000Z-00001111"
    env.write_candidate(rid)
    os.rename(env.paths.readiness_candidate / rid, env.paths.readiness_releases / rid)
    snap = env.load()
    assert snap["status"] == "ABSENT"
    assert snap["orphans"]["readiness"][0]["status"] == "ORPHANED_UNPROMOTED"
    assert not snap["fail_closed"]


def test_invalid_orphan_release_is_fail_closed(env):
    rid = "ready-20260917T000000Z-00002222"
    env.write_candidate(rid)
    os.rename(env.paths.readiness_candidate / rid, env.paths.readiness_releases / rid)
    (env.paths.readiness_releases / rid / (rd.RECORD_BASENAME + ".sha256")).write_text(
        "0" * 64 + "\n", encoding="utf-8")
    snap = env.load()
    assert snap["orphans"]["readiness"][0]["status"] == "ORPHAN_INVALID"
    assert snap["fail_closed"] is True and snap["reason_code"] == "ORPHAN_INVALID"


def test_active_record_tamper_is_stale_not_deleted(env):
    rid = "ready-20260917T000000Z-00003333"
    env.write_candidate(rid)
    env.writer().promote(rid, expected_generation=1)
    path = env.paths.readiness_releases / rid / rd.RECORD_BASENAME
    text = path.read_text(encoding="utf-8").replace('"ROC": 75', '"ROC": 74')
    path.write_text(text, encoding="utf-8")
    snap = env.load()
    assert snap["status"] == "ACTIVE" and snap["active"]["valid"] is False
    assert rd.evaluate_applicability(snap, binding())["operational_readiness"] == "STALE"
    assert rd.read_state_snapshot(env.paths.readiness_state)["state"]["active"]["release"] == rid
    # sidecar 위조까지 맞춰도 state pointer sha와 달라 적용 불가.
    (path.parent / (rd.RECORD_BASENAME + ".sha256")).write_text(
        rd.sha256_hex(path.read_bytes()) + "\n", encoding="utf-8")
    assert env.load()["active"]["valid"] is False


# ---------------------------------------------------------------- inhibit

def _write_inhibit(env, inhibit_id, bind=None, reason="OWNERSHIP_UNCERTAIN"):
    record = rd.build_inhibit_record(
        inhibit_id=inhibit_id, binding=bind or binding(), reason_code=reason,
        source_run_id="run-y", evidence_sha256="9a" * 32,
        detail={"observed": "escalation used"}, active_release=None)
    return rd.write_immutable_record(env.paths.readiness_inhibits / inhibit_id,
                                     basename=rd.INHIBIT_BASENAME, record=record,
                                     evidence={"close.json": b"{}\n"}, producer="test")


def test_inhibit_flow_preserves_active_and_blocks_workload(env):
    rid = "ready-20260917T000000Z-00004444"
    env.write_candidate(rid)
    env.writer().promote(rid, expected_generation=1)
    iid = "inhibit-20260917T000100Z-00005555"
    _write_inhibit(env, iid)
    state = env.writer().inhibit(iid, expected_generation=2)
    assert state["generation"] == 3
    assert state["active"]["release"] == rid and state["inhibit"]["release"] == iid
    assert state["inhibit"]["reason_code"] == "OWNERSHIP_UNCERTAIN"
    snap = env.load()
    assert snap["status"] == "INHIBITED" and snap["workload_rf_blocked"]
    assert snap["fail_closed"] is False      # preparation RF는 허용, workload만 차단
    verdict = rd.evaluate_applicability(snap, binding())
    assert verdict["operational_readiness"] == "INHIBITED" and verdict["inhibit_applies"]
    # 복구: 새 release 승격 하나의 state 교체가 active 교체와 inhibit 해제를 함께 한다.
    new = "ready-20260917T000200Z-00006666"
    env.write_candidate(new)
    state = env.writer().promote(new, expected_generation=3)
    assert state["active"]["release"] == new and state["inhibit"] is None
    assert env.load()["status"] == "ACTIVE"
    assert (env.paths.readiness_inhibits / iid).is_dir()     # 이력 삭제 없음
    assert (env.paths.readiness_releases / rid).is_dir()


def test_inhibit_record_tamper_is_state_corrupt_not_absent(env):
    iid = "inhibit-20260917T000100Z-00007777"
    _write_inhibit(env, iid)
    env.writer().inhibit(iid, expected_generation=1)
    (env.paths.readiness_inhibits / iid / "evidence" / "close.json").write_text("{tampered}", encoding="utf-8")
    snap = env.load()
    assert snap["status"] == "STATE_CORRUPT" and snap["reason_code"] == "INHIBITED_STATE_CORRUPT"
    assert snap["fail_closed"] and snap["workload_rf_blocked"]


def test_dangling_inhibit_pointer_is_state_corrupt(env):
    state = rd.read_state_snapshot(env.paths.readiness_state)["state"]
    state["generation"] = 2
    state["inhibit"] = {"release": "inhibit-20260917T000100Z-00008888", "record_sha256": "1" * 64,
                        "readiness_digest_sha256": "2" * 64, "reason_code": "X",
                        "evidence_sha256": "3" * 64, "created_utc": "2026-09-17T00:00:00Z"}
    state["inhibit_history"] = ["inhibit-20260917T000100Z-00008888"]
    # 외부 변조를 흉내 낸다 (writer를 거치지 않고 파일을 직접 쓴다).
    env.paths.readiness_state.write_bytes(rd.encode_record(state))
    snap = env.load()
    assert snap["status"] == "STATE_CORRUPT" and snap["fail_closed"]
    # pointer가 이력에도 없는 경우는 schema 단계에서 STATE_CORRUPT다.
    state["inhibit_history"] = []
    env.paths.readiness_state.write_bytes(rd.encode_record(state))
    assert env.load()["status"] == "STATE_CORRUPT"


def test_orphan_inhibit_is_inhibited_even_when_valid(env):
    iid = "inhibit-20260917T000100Z-00009999"
    _write_inhibit(env, iid)
    snap = env.load()
    assert snap["status"] == "INHIBITED" and snap["workload_rf_blocked"]
    assert snap["orphans"]["inhibits"][0]["status"] == "ORPHAN_INHIBIT_VALID"
    assert rd.evaluate_applicability(snap, binding())["inhibit_applies"]


def test_inhibit_for_other_binding_still_blocks_until_new_preparation(env):
    iid = "inhibit-20260917T000100Z-0000aaaa"
    _write_inhibit(env, iid, bind=binding(identity_fingerprint="other"))
    env.writer().inhibit(iid, expected_generation=1)
    verdict = rd.evaluate_applicability(env.load(), binding())
    assert verdict["operational_readiness"] == "INHIBITED" and verdict["inhibit_applies"]


# ---------------------------------------------------------------- applicability

def test_applicability_stale_reports_fields(env):
    rid = "ready-20260917T000000Z-0000bbb1"
    env.write_candidate(rid)
    env.writer().promote(rid, expected_generation=1)
    snap = env.load()
    changed = binding()
    changed["hardware"]["endpoints"]["MASS-A"]["serial"] = "DIFFERENT"
    changed["calibration"]["artifact_sha256"] = "ff" * 32
    verdict = rd.evaluate_applicability(snap, changed)
    assert verdict["operational_readiness"] == "STALE" and verdict["digest_match"] is False
    joined = "\n".join(verdict["reasons"])
    assert "binding.hardware.endpoints.MASS-A.serial" in joined
    assert "binding.calibration.artifact_sha256" in joined
    # STALE은 state.active를 지우지 않는다.
    assert rd.read_state_snapshot(env.paths.readiness_state)["state"]["active"]["release"] == rid


# ---------------------------------------------------------------- record hygiene

def test_record_rejects_traversal_and_malformed_ids(env):
    with pytest.raises(rd.ReadinessError):
        rd.write_immutable_record(env.paths.readiness_candidate / "ready-20260917T000000Z-0000cc11",
                                  basename=rd.RECORD_BASENAME,
                                  record=record_for("ready-20260917T000000Z-0000cc11"),
                                  evidence={"../escape.json": b"{}"}, producer="test")
    with pytest.raises(rd.ReadinessError):
        record_for("ready-bad")
    with pytest.raises(rd.ReadinessError):
        record_for("ready-20260917T000000Z-0000cc22", status="MAYBE")
    assert not rd.is_valid_id("../x")


def test_verify_record_dir_detects_evidence_and_index_tamper(env):
    rid = "ready-20260917T000000Z-0000dd11"
    env.write_candidate(rid)
    directory = env.paths.readiness_candidate / rid
    assert rd.verify_record_dir(directory, basename=rd.RECORD_BASENAME,
                                expected_type=rd.READINESS_RECORD_TYPE)["valid"]
    (directory / "evidence" / "summary.json").write_bytes(b'{"ok": false}\n')
    check = rd.verify_record_dir(directory, basename=rd.RECORD_BASENAME,
                                 expected_type=rd.READINESS_RECORD_TYPE)
    assert not check["valid"] and any("evidence sha/size" in p for p in check["problems"])
    (directory / "evidence" / "summary.json").write_bytes(b'{"ok": true}\n')
    (directory / rd.EVIDENCE_INDEX_BASENAME).write_bytes(b"{}")
    check = rd.verify_record_dir(directory, basename=rd.RECORD_BASENAME,
                                 expected_type=rd.READINESS_RECORD_TYPE)
    assert not check["valid"] and any("index" in p for p in check["problems"])


def test_release_history_lists_all_releases(env):
    a, b = "ready-20260917T000000Z-0000ee11", "ready-20260917T000001Z-0000ee22"
    env.write_candidate(a)
    env.writer().promote(a, expected_generation=1)
    env.write_candidate(b)
    env.writer().promote(b, expected_generation=2)
    snap = env.load()
    assert [(r["release"], r["active"]) for r in snap["history"]] == [(a, False), (b, True)]
    assert snap["orphans"]["readiness"] == []   # previous active는 이력이지 orphan이 아니다
    assert rd.read_state_snapshot(env.paths.readiness_state)["state"]["active"][
        "previous_release"] == a
