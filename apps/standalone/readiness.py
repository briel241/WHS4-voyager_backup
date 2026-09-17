"""Operational readiness state store — `var/readiness/` (spec §3, §8, §9, §12).

`docs/core/operational_readiness_and_bringup_spec.md`(SHA-256 654377ff…)가 규범이다.
이 모듈은 **readiness namespace의 저장·적재·비교**만 담당한다. RF를 송신하지도, 장치를
열지도, qualification namespace(`var/qualification`)를 읽거나 쓰지도 않는다.

    var/readiness/
      candidate/<readiness-id>/   operational-readiness.json(+.sha256), evidence-index.json, evidence/...
      releases/<readiness-id>/    후보가 rename된 불변 release
      inhibits/<inhibit-id>/      operational-inhibit.json(+.sha256), evidence-index.json, evidence/...
      state.json                  유일한 mutable control pointer (active + inhibit + generation)

규범 요약 (구현이 지키는 것)
  - stable applicability binding만 `readiness_digest_sha256`에 넣는다 (§8.1). session 관측값·시각·
    fit 계수·PRE/POST 요약·evidence index는 record SHA로만 결속한다. 포함/제외 목록은 코드 상수다.
  - canonical digest는 기존 `vdeter.b210_system.canonical_json.encode_canonical_json`(§8.2: UTF-8,
    key 정렬, 정수 무손실, 중복 key 금지, float/NaN 금지)을 재사용한다.
  - `state.json.generation`은 1 이상 정수이며 새 state는 직전 검증 snapshot보다 정확히 1 크다.
    writer는 `vdeter run`의 전역 단일-instance lock을 쥔 채로만 쓰고, 교체 직전에 generation이 읽은
    값과 같은지 다시 확인한다(다르면 stale writer → 덮어쓰지 않고 fail-closed).
  - reader는 lock 없이 파일 하나를 완전 snapshot으로 읽고 rename 경계의 ENOENT/불완전 JSON만
    50 ms 간격 최대 3회 재시도한다.
  - 최초 유효 state(generation=1, active=null, inhibit=null)만 정상 `ABSENT`다. installation manifest의
    migration marker가 없으면 `MIGRATION_REQUIRED`, marker가 있는데 state를 못 읽으면
    `STATE_UNREADABLE`(fail-closed).
  - valid orphan readiness release는 `ORPHANED_UNPROMOTED` 이력, invalid orphan은 `ORPHAN_INVALID`
    (RF 전 차단), unreferenced inhibit는 유효해도 `INHIBITED`(fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from apps.standalone.state import StateError, fsync_dir

READINESS_STATE_TYPE = "VDETER_OPERATIONAL_READINESS_STATE_V1"
READINESS_RECORD_TYPE = "VDETER_OPERATIONAL_READINESS_V1"
INHIBIT_RECORD_TYPE = "VDETER_OPERATIONAL_INHIBIT_V1"
EVIDENCE_INDEX_TYPE = "VDETER_OPERATIONAL_READINESS_EVIDENCE_INDEX_V1"
SCHEMA_VERSION = 1
ISSUER_VERSION = "vdeter run operational-readiness v1"

RECORD_BASENAME = "operational-readiness.json"
INHIBIT_BASENAME = "operational-inhibit.json"
EVIDENCE_INDEX_BASENAME = "evidence-index.json"
EVIDENCE_DIR = "evidence"

#: installation manifest에 setup/migration이 기록하는 marker (§9). 이 marker가 있는 설치는
#: `state.json`을 읽지 못하면 fail-closed다 — state 삭제가 새 설치의 정상 UNVERIFIED로 위장될 수 없다.
MANIFEST_MARKER = {"readiness_state_required": True,
                   "readiness_state_schema_version": SCHEMA_VERSION}

#: lock-free reader의 rename 경계 재시도 (§9.0.1).
READER_RETRY_COUNT = 3
READER_RETRY_INTERVAL_S = 0.05

#: readiness/inhibit ID 형식 — 경로 조각으로 안전해야 한다.
_ID_RE = re.compile(r"^(ready|inhibit|revoke)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

#: `binding`의 필수 그룹 (§8.1 표). 이 목록 밖의 key는 binding에 들어올 수 없다(암묵 제외 금지).
BINDING_GROUPS = (
    "installation",          # rf/operation manifest SHA, readiness closure SHA, critical RF symbol SHA
    "identity_fingerprint",  # identity
    "platform",              # platform profile, host profile, stable machine identity, boot 검증 정책 ID
    "hardware",              # 역할별 serial/speed class/transport, hub topology/asset/VID:PID/power 증언
    "wiring",                # wiring profile·approval
    "rf_config",             # endpoint template digest, RF center/sample rate/channel/lane
    "calibration",           # artifact SHA, source/provenance, 역할별 gain, calibration policy ID
    "policy_ids",            # host-time/RF-time/probe/safety/classifier/closure/boot 정책 ID
    "probe_plan_sha256",     # Link 17 probe plan SHA
)
#: record에는 있지만 applicability digest에서 **명시적으로 제외**하는 최상위 필드 (§8.1).
EXCLUDED_FROM_DIGEST = (
    "artifact_type", "schema_version", "status", "not_a_qualification", "readiness_id",
    "readiness_digest_sha256", "record_metadata", "preparation_checks", "execution_close",
    "session", "evidence", "qualification_provenance", "limitations", "observations",
    "calibration_candidate", "workloads", "digest_policy",
)

READINESS_LIMITATIONS = (
    "NOT_QUALIFIED",
    "does not cover the full RF/fault/software qualification ladder",
    "Link 17 readiness probe does not stand in for RR-P2/RR-P3 or raw-PHY qualification",
)


class ReadinessError(RuntimeError):
    """readiness namespace 거부 — 항상 RF 접근 전이거나 승격 전이다."""


class StaleWriterError(ReadinessError):
    """교체 직전 generation이 읽은 값과 달라졌다 — state를 덮어쓰지 않는다."""


# ------------------------------------------------------------ canonical / sha

def _ensure_canonical_importable() -> None:
    import sys
    here = Path(__file__).resolve().parent.parent.parent
    for entry in (here / "VDETER", here / "VDETER" / "src"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def canonical_bytes(value) -> bytes:
    """§8.2 canonical JSON — 기존 Stage 7 encoder 재사용 (float/NaN/중복 key 거부)."""
    _ensure_canonical_importable()
    from vdeter.b210_system.canonical_json import encode_canonical_json
    return encode_canonical_json(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def readiness_digest(binding: dict) -> str:
    """`readiness_digest_sha256 = SHA-256(canonical_json(binding))`."""
    errors = validate_binding(binding)
    if errors:
        raise ReadinessError("binding is not digestible: " + "; ".join(errors))
    return sha256_hex(canonical_bytes(binding))


def encode_record(payload: dict) -> bytes:
    """record 파일 바이트 — reference와 같은 가독 형식(정렬·ASCII·indent 1·LF)."""
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1)
            + "\n").encode("utf-8")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_readiness_id(prefix: str = "ready", *, now=None, nonce: str | None = None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return "%s-%s-%s" % (prefix, stamp, nonce or secrets.token_hex(4))


def is_valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


# ------------------------------------------------------------ binding schema

def _walk_no_float(value, path, errors):
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        errors.append("%s: float is not allowed in a stable binding" % path)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk_no_float(item, "%s[%d]" % (path, index), errors)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append("%s: non-string key" % path)
                continue
            _walk_no_float(item, "%s.%s" % (path, key), errors)
        return
    errors.append("%s: unsupported type %s" % (path, type(value).__name__))


def validate_binding(binding) -> list:
    """§8.1 그룹 전부 존재, 초과 key 없음, float 없음. 반환: 오류 목록(비면 통과)."""
    errors = []
    if not isinstance(binding, dict):
        return ["binding is not an object"]
    for group in BINDING_GROUPS:
        if group not in binding:
            errors.append("binding.%s missing" % group)
    for key in sorted(set(binding) - set(BINDING_GROUPS)):
        errors.append("binding.%s is not a stable applicability group" % key)
    if not _is_sha(binding.get("probe_plan_sha256")):
        errors.append("binding.probe_plan_sha256 is not a sha256")
    if not isinstance(binding.get("identity_fingerprint"), str) or not binding.get(
            "identity_fingerprint"):
        errors.append("binding.identity_fingerprint missing")
    for group in ("installation", "platform", "hardware", "wiring", "rf_config",
                  "calibration", "policy_ids"):
        if group in binding and not isinstance(binding[group], dict):
            errors.append("binding.%s is not an object" % group)
    _walk_no_float(binding, "binding", errors)
    return errors


def binding_differences(recorded: dict, current: dict) -> list:
    """stable binding 두 개를 그룹·필드 단위로 대조한 stale 사유 목록 (status/run 공용)."""
    reasons = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a:
                    reasons.append("%s.%s: recorded absent, current %r" % (path, key, b[key]))
                elif key not in b:
                    reasons.append("%s.%s: recorded %r, current absent" % (path, key, a[key]))
                else:
                    walk(a[key], b[key], "%s.%s" % (path, key))
            return
        if a != b:
            reasons.append("%s: recorded %r, current %r" % (path, _short(a), _short(b)))

    ordered = [g for g in BINDING_GROUPS if g != "calibration"] + ["calibration"]
    recorded = recorded or {}
    current = current or {}
    for group in ordered:
        if group in recorded or group in current:
            walk(recorded.get(group), current.get(group), "binding." + group)
    for key in sorted(set(recorded) | set(current)):
        if key not in BINDING_GROUPS:
            walk(recorded.get(key), current.get(key), "binding." + key)
    return reasons


def _short(value):
    if isinstance(value, str) and len(value) == 64 and _SHA_RE.match(value):
        return value[:12] + "…"
    return value


# ------------------------------------------------------------ state file

def initial_state(*, now=None) -> dict:
    return {
        "artifact_type": READINESS_STATE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "generation": 1,
        "active": None,
        "inhibit": None,
        #: append-only 이력 — 한 번이라도 state가 참조한 release/inhibit id. orphan 판정은 이 이력에
        #: 없는 디렉터리만 대상으로 한다(이전 active·해제된 inhibit는 이력이지 orphan이 아니다).
        "promotion_history": [],
        "inhibit_history": [],
        "updated_utc": now or utc_now(),
    }


def validate_state(state) -> list:
    """schema/generation/pointer 형식 검증. 반환: 오류 목록."""
    errors = []
    if not isinstance(state, dict):
        return ["state is not an object"]
    if state.get("artifact_type") != READINESS_STATE_TYPE:
        errors.append("artifact_type %r" % state.get("artifact_type"))
    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version %r" % state.get("schema_version"))
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        errors.append("generation %r is not an integer >= 1" % (generation,))
    for pointer_name, required in (
        ("active", ("release", "record_sha256", "readiness_digest_sha256", "promoted_utc")),
        ("inhibit", ("release", "record_sha256", "readiness_digest_sha256", "reason_code",
                     "evidence_sha256", "created_utc")),
    ):
        pointer = state.get(pointer_name)
        if pointer is None:
            continue
        if not isinstance(pointer, dict):
            errors.append("%s is not an object or null" % pointer_name)
            continue
        for key in required:
            if key not in pointer:
                errors.append("%s.%s missing" % (pointer_name, key))
        if not is_valid_id(pointer.get("release")):
            errors.append("%s.release %r is not a valid id" % (pointer_name, pointer.get("release")))
        for key in ("record_sha256", "readiness_digest_sha256"):
            if not _is_sha(pointer.get(key)):
                errors.append("%s.%s is not a sha256" % (pointer_name, key))
        if pointer_name == "inhibit" and not _is_sha(pointer.get("evidence_sha256")):
            errors.append("inhibit.evidence_sha256 is not a sha256")
    for history_name in ("promotion_history", "inhibit_history"):
        history = state.get(history_name)
        if history is None:
            continue
        if not isinstance(history, list) or any(not is_valid_id(item) for item in history):
            errors.append("%s is not a list of valid ids" % history_name)
        elif len(set(history)) != len(history):
            errors.append("%s has duplicate ids" % history_name)
    active = state.get("active")
    if (isinstance(active, dict) and isinstance(state.get("promotion_history"), list)
            and active.get("release") not in state["promotion_history"]):
        errors.append("active.release is not in promotion_history")
    inhibit = state.get("inhibit")
    if (isinstance(inhibit, dict) and isinstance(state.get("inhibit_history"), list)
            and inhibit.get("release") not in state["inhibit_history"]):
        errors.append("inhibit.release is not in inhibit_history")
    return errors


def write_state_atomic(path: Path, state: dict) -> bytes:
    """tmp + fsync + os.replace + parent fsync (§9). 반환: 기록 바이트."""
    errors = validate_state(state)
    if errors:
        raise ReadinessError("refusing to write an invalid state: " + "; ".join(errors))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode_record(state)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
    # 재독해 검증 — 쓴 바이트가 그대로 읽히는지 (setup/migration 요구: 원자 생성·fsync·재독해).
    with open(path, "rb") as handle:
        if handle.read() != data:
            raise ReadinessError("state.json re-read differs from the written bytes")
    return data


def read_state_snapshot(path: Path, *, retries: int = READER_RETRY_COUNT,
                        interval_s: float = READER_RETRY_INTERVAL_S,
                        sleeper=time.sleep) -> dict:
    """lock 없는 snapshot reader (§9.0.1).

    반환: {"state": dict|None, "outcome": "OK"|"MISSING"|"UNREADABLE"|"INVALID",
           "attempts": n, "problems": [...]}. ENOENT/불완전 JSON은 최대 `retries`회 재시도한다."""
    path = Path(path)
    problems = []
    attempts = 0
    last = None
    while attempts < retries:
        attempts += 1
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            last = "MISSING"
            problems.append("attempt %d: state.json missing" % attempts)
        except OSError as exc:
            last = "UNREADABLE"
            problems.append("attempt %d: %s" % (attempts, exc))
        else:
            try:
                state = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                last = "UNREADABLE"
                problems.append("attempt %d: incomplete/invalid JSON (%s)" % (attempts, exc))
            else:
                errors = validate_state(state)
                if errors:
                    return {"state": state, "outcome": "INVALID", "attempts": attempts,
                            "problems": errors}
                return {"state": state, "outcome": "OK", "attempts": attempts,
                        "problems": []}
        if attempts < retries:
            sleeper(interval_s)
    return {"state": None, "outcome": last or "MISSING", "attempts": attempts,
            "problems": problems}


# ------------------------------------------------------------ immutable records

def _evidence_index(files: dict, *, producer: str) -> dict:
    """evidence index — 상대경로·sha256·size·producer·required (record·state 자기참조 없음)."""
    entries = {}
    for relpath in sorted(files):
        spec = files[relpath]
        entries[relpath] = {
            "sha256": spec["sha256"], "size": spec["size"],
            "producer": spec.get("producer", producer),
            "required": bool(spec.get("required", True)),
        }
    return {"artifact_type": EVIDENCE_INDEX_TYPE, "schema_version": SCHEMA_VERSION,
            "files": entries}


def _write_bytes_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_immutable_record(directory: Path, *, basename: str, record: dict,
                           evidence: dict, producer: str) -> dict:
    """candidate/inhibit 디렉터리에 evidence → index → record → sidecar 순으로 fsync 기록.

    `evidence`: {relpath: bytes} (record 안의 `evidence.files`와 index로 결속된다).
    `record["evidence"]`는 여기서 채운다(index sha·files). 반환: {"path", "sha256", "index_sha256"}."""
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ReadinessError("record directory is not empty: %s" % directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for relpath, data in sorted(evidence.items()):
        if not isinstance(data, (bytes, bytearray)):
            raise ReadinessError("evidence %s must be bytes" % relpath)
        if relpath.startswith("/") or ".." in relpath.split("/"):
            raise ReadinessError("evidence relpath is not relative: %s" % relpath)
        target = directory / EVIDENCE_DIR / relpath
        _write_bytes_fsync(target, bytes(data))
        files[EVIDENCE_DIR + "/" + relpath] = {"sha256": sha256_hex(bytes(data)),
                                               "size": len(data), "producer": producer}
    index = _evidence_index(files, producer=producer)
    index_bytes = encode_record(index)
    _write_bytes_fsync(directory / EVIDENCE_INDEX_BASENAME, index_bytes)
    index_sha = sha256_hex(index_bytes)
    record = dict(record)
    record["evidence"] = {"index_sha256": index_sha,
                          "index_relpath": EVIDENCE_INDEX_BASENAME,
                          "files": {k: v["sha256"] for k, v in files.items()}}
    record_bytes = encode_record(record)
    sha = sha256_hex(record_bytes)
    _write_bytes_fsync(directory / basename, record_bytes)
    _write_bytes_fsync(directory / (basename + ".sha256"),
                       ("%s  %s\n" % (sha, basename)).encode("utf-8"))
    fsync_dir(directory)
    fsync_dir(directory / EVIDENCE_DIR)
    return {"path": directory / basename, "sha256": sha, "index_sha256": index_sha,
            "record": record}


def verify_record_dir(directory: Path, *, basename: str, expected_type: str) -> dict:
    """불변 record 디렉터리 검증: sidecar == 재계산 SHA, artifact_type, schema, evidence index와
    파일 전수 SHA/size, binding digest 재계산 == 기록. 반환 {"valid", "problems", "record", "sha256"}."""
    directory = Path(directory)
    problems = []
    record = None
    sha = None
    path = directory / basename
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"valid": False, "problems": ["record unreadable: %s" % exc],
                "record": None, "sha256": None}
    sha = sha256_hex(raw)
    try:
        sidecar = (directory / (basename + ".sha256")).read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        problems.append("sidecar missing or empty")
        sidecar = None
    if sidecar is not None and sidecar != sha:
        problems.append("sidecar sha %s… != record sha %s…" % (sidecar[:12], sha[:12]))
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"valid": False, "problems": problems + ["record is not JSON: %s" % exc],
                "record": None, "sha256": sha}
    if not isinstance(record, dict):
        return {"valid": False, "problems": problems + ["record is not an object"],
                "record": None, "sha256": sha}
    if record.get("artifact_type") != expected_type:
        problems.append("artifact_type %r != %r" % (record.get("artifact_type"), expected_type))
    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version %r" % record.get("schema_version"))
    if record.get("not_a_qualification") is not True:
        problems.append("not_a_qualification marker missing")
    binding = record.get("binding")
    digest = record.get("readiness_digest_sha256")
    binding_errors = validate_binding(binding)
    if binding_errors:
        problems.extend("binding: " + e for e in binding_errors)
    else:
        recomputed = sha256_hex(canonical_bytes(binding))
        if recomputed != digest:
            problems.append("readiness_digest_sha256 mismatch (recorded %s…, recomputed %s…)"
                            % (str(digest)[:12], recomputed[:12]))
    for excluded in EXCLUDED_FROM_DIGEST:
        if excluded in (binding or {}):
            problems.append("binding contains excluded field %s" % excluded)
    evidence = record.get("evidence") or {}
    try:
        index_bytes = (directory / EVIDENCE_INDEX_BASENAME).read_bytes()
    except OSError:
        problems.append("evidence index missing")
        index_bytes = None
    if index_bytes is not None:
        if sha256_hex(index_bytes) != evidence.get("index_sha256"):
            problems.append("evidence index sha mismatch")
        try:
            index = json.loads(index_bytes.decode("utf-8"))
        except ValueError:
            index = None
            problems.append("evidence index is not JSON")
        if isinstance(index, dict):
            if index.get("artifact_type") != EVIDENCE_INDEX_TYPE:
                problems.append("evidence index artifact_type %r" % index.get("artifact_type"))
            listed = index.get("files") or {}
            if {k: v.get("sha256") for k, v in listed.items()} != (evidence.get("files") or {}):
                problems.append("evidence index files differ from record.evidence.files")
            for relpath, entry in sorted(listed.items()):
                if relpath.startswith("/") or ".." in relpath.split("/"):
                    problems.append("evidence path traversal: %s" % relpath)
                    continue
                target = directory / relpath
                try:
                    data = target.read_bytes()
                except OSError:
                    if entry.get("required", True):
                        problems.append("evidence missing: %s" % relpath)
                    continue
                if sha256_hex(data) != entry.get("sha256") or len(data) != entry.get("size"):
                    problems.append("evidence sha/size mismatch: %s" % relpath)
    return {"valid": not problems, "problems": problems, "record": record, "sha256": sha}


# ------------------------------------------------------------ record builders

def build_readiness_record(*, readiness_id: str, binding: dict, status: str,
                           record_metadata: dict, preparation_checks: dict,
                           execution_close: dict, session: dict,
                           qualification_provenance=None, observations=None,
                           calibration_candidate=None, workloads=None) -> dict:
    """§12 schema. `binding`은 stable applicability 값만, 나머지는 record SHA로만 결속된다."""
    if status not in ("PASS", "FAIL", "CANCELLED"):
        raise ReadinessError("readiness status must be PASS/FAIL/CANCELLED")
    if not is_valid_id(readiness_id) or not readiness_id.startswith("ready-"):
        raise ReadinessError("readiness_id %r is malformed" % readiness_id)
    digest = readiness_digest(binding)
    metadata = dict(record_metadata)
    metadata.setdefault("issuer_version", ISSUER_VERSION)
    metadata.setdefault("created_utc", utc_now())
    session = dict(session)
    session.setdefault("cross_command_handoff_allowed", False)
    session.setdefault("session_reuse_scope", "THIS_RUN_ONLY")
    record = {
        "artifact_type": READINESS_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "not_a_qualification": True,
        "readiness_id": readiness_id,
        "binding": binding,
        "readiness_digest_sha256": digest,
        "digest_policy": {"included_groups": list(BINDING_GROUPS),
                          "excluded_top_level_fields": list(EXCLUDED_FROM_DIGEST)},
        "record_metadata": metadata,
        "preparation_checks": preparation_checks,
        "execution_close": execution_close,
        "session": session,
        "observations": observations or {},
        "workloads": workloads or [],
        "calibration_candidate": calibration_candidate,
        "qualification_provenance": qualification_provenance,
        "limitations": list(READINESS_LIMITATIONS),
    }
    return record


def build_inhibit_record(*, inhibit_id: str, binding: dict, reason_code: str,
                         source_run_id: str, evidence_sha256: str, detail: dict,
                         active_release=None, observations=None) -> dict:
    if not is_valid_id(inhibit_id) or not inhibit_id.startswith("inhibit-"):
        raise ReadinessError("inhibit_id %r is malformed" % inhibit_id)
    if not isinstance(reason_code, str) or not reason_code:
        raise ReadinessError("reason_code missing")
    digest = readiness_digest(binding)
    return {
        "artifact_type": INHIBIT_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "status": "INHIBIT",
        "not_a_qualification": True,
        "inhibit_id": inhibit_id,
        "binding": binding,
        "readiness_digest_sha256": digest,
        "digest_policy": {"included_groups": list(BINDING_GROUPS),
                          "excluded_top_level_fields": list(EXCLUDED_FROM_DIGEST)},
        "reason_code": reason_code,
        "record_metadata": {"issuer_version": ISSUER_VERSION, "source_run_id": source_run_id,
                            "created_utc": utc_now()},
        "cause_evidence_sha256": evidence_sha256,
        "inhibited_active_release": active_release,
        "detail": detail,
        "observations": observations or {},
        "limitations": list(READINESS_LIMITATIONS),
    }


# ------------------------------------------------------------ writer (lock holder)

class ReadinessWriter:
    """state.json의 단일 writer — 기존 `vdeter run` 전역 lock을 쥔 채로만 동작한다 (§9.0.1)."""

    def __init__(self, paths, lock) -> None:
        if lock is None or not getattr(lock, "held", False):
            raise ReadinessError("readiness writer requires the held single-instance lock")
        self.paths = paths
        self.lock = lock

    def snapshot(self) -> dict:
        snap = read_state_snapshot(self.paths.readiness_state)
        if snap["outcome"] != "OK":
            raise ReadinessError("state.json is not a valid snapshot (%s): %s"
                                 % (snap["outcome"], "; ".join(snap["problems"])))
        return snap["state"]

    def _commit(self, expected_generation: int, mutate) -> dict:
        """교체 직전 generation 재확인 → 새 state 원자 교체."""
        current = self.snapshot()
        if current["generation"] != expected_generation:
            raise StaleWriterError(
                "state generation changed while this run was preparing (%r -> %r); "
                "the candidate stays as inactive evidence"
                % (expected_generation, current["generation"]))
        new_state = mutate(dict(current))
        new_state["generation"] = expected_generation + 1
        new_state["updated_utc"] = utc_now()
        write_state_atomic(self.paths.readiness_state, new_state)
        return new_state

    def promote(self, readiness_id: str, *, expected_generation: int) -> dict:
        """candidate/<id> → releases/<id>(rename+fsync) → state 원자 교체(active=<id>, inhibit=null)."""
        if not self.lock.held:
            raise ReadinessError("lock released before promotion")
        candidate = self.paths.readiness_candidate / readiness_id
        target = self.paths.readiness_releases / readiness_id
        check = verify_record_dir(candidate, basename=RECORD_BASENAME,
                                  expected_type=READINESS_RECORD_TYPE)
        if not check["valid"]:
            raise ReadinessError("candidate is not a complete readiness record: "
                                 + "; ".join(check["problems"]))
        if check["record"].get("status") != "PASS":
            raise ReadinessError("only a PASS candidate can be promoted")
        if target.exists():
            raise ReadinessError("release id already exists: %s" % target)
        if any(p.name == readiness_id for p in self.paths.readiness_inhibits.glob("*")):
            raise ReadinessError("id collides with an inhibit record: %s" % readiness_id)
        if readiness_id in (self.snapshot().get("promotion_history") or []):
            raise ReadinessError("release id was already promoted once: %s" % readiness_id)
        # stale-writer 선검사: rename 전에 generation이 이미 달라졌으면 candidate를 그대로 둔다.
        if self.snapshot()["generation"] != expected_generation:
            raise StaleWriterError("state generation changed before promotion; candidate kept")
        os.rename(candidate, target)
        fsync_dir(self.paths.readiness_releases)
        fsync_dir(self.paths.readiness_candidate)
        record = check["record"]

        def mutate(state):
            history = list(state.get("promotion_history") or [])
            if readiness_id in history:
                raise ReadinessError("release id was already promoted once: %s" % readiness_id)
            history.append(readiness_id)
            state["active"] = {"release": readiness_id, "record_sha256": check["sha256"],
                               "readiness_digest_sha256": record["readiness_digest_sha256"],
                               "promoted_utc": utc_now(),
                               "previous_release": (state.get("active") or {}).get("release")}
            state["inhibit"] = None
            state["promotion_history"] = history
            return state

        try:
            return self._commit(expected_generation, mutate)
        except StaleWriterError:
            # release는 이미 완결됐다 — 자동 승격하지 않는 valid orphan(ORPHANED_UNPROMOTED)으로 남는다.
            raise

    def inhibit(self, inhibit_id: str, *, expected_generation: int) -> dict:
        """inhibits/<id>가 완전히 기록·검증된 뒤 state에 inhibit 참조 추가 (active 보존)."""
        directory = self.paths.readiness_inhibits / inhibit_id
        check = verify_record_dir(directory, basename=INHIBIT_BASENAME,
                                  expected_type=INHIBIT_RECORD_TYPE)
        if not check["valid"]:
            raise ReadinessError("inhibit record incomplete: " + "; ".join(check["problems"]))
        record = check["record"]

        def mutate(state):
            history = list(state.get("inhibit_history") or [])
            if inhibit_id in history:
                raise ReadinessError("inhibit id was already referenced once: %s" % inhibit_id)
            history.append(inhibit_id)
            state["inhibit"] = {
                "release": inhibit_id, "record_sha256": check["sha256"],
                "readiness_digest_sha256": record["readiness_digest_sha256"],
                "reason_code": record["reason_code"],
                "evidence_sha256": record["cause_evidence_sha256"],
                "created_utc": utc_now(),
            }
            state["inhibit_history"] = history
            return state

        return self._commit(expected_generation, mutate)

    def revoke(self, *, expected_generation: int, reason: str) -> dict:
        """명시 revoke — active pointer만 비운다(release 이력 삭제 없음)."""

        def mutate(state):
            state["active"] = None
            state["revoked"] = {"reason": reason, "revoked_utc": utc_now(),
                                "previous_active": (state.get("active") or {}).get("release")}
            return state

        return self._commit(expected_generation, mutate)


# ------------------------------------------------------------ orphan scan

def scan_orphans(paths, state) -> dict:
    """state가 참조하지 않는 release/inhibit 디렉터리 분류 (§9.0.2)."""
    active_id = ((state or {}).get("active") or {}).get("release")
    inhibit_id = ((state or {}).get("inhibit") or {}).get("release")
    promoted = set((state or {}).get("promotion_history") or ())
    referenced_inhibits = set((state or {}).get("inhibit_history") or ())
    readiness = []
    inhibits = []
    for directory in sorted(paths.readiness_releases.glob("*")):
        if not directory.is_dir() or directory.name == active_id or directory.name in promoted:
            continue
        check = verify_record_dir(directory, basename=RECORD_BASENAME,
                                  expected_type=READINESS_RECORD_TYPE)
        readiness.append({
            "release": directory.name,
            "status": "ORPHANED_UNPROMOTED" if check["valid"] else "ORPHAN_INVALID",
            "problems": check["problems"],
            "readiness_digest_sha256": (check["record"] or {}).get("readiness_digest_sha256"),
            "record_sha256": check["sha256"],
        })
    for directory in sorted(paths.readiness_inhibits.glob("*")):
        if (not directory.is_dir() or directory.name == inhibit_id
                or directory.name in referenced_inhibits):
            continue
        check = verify_record_dir(directory, basename=INHIBIT_BASENAME,
                                  expected_type=INHIBIT_RECORD_TYPE)
        inhibits.append({
            "release": directory.name,
            "status": "ORPHAN_INHIBIT_VALID" if check["valid"] else "ORPHAN_INHIBIT_INVALID",
            "reason_code": (check["record"] or {}).get("reason_code"),
            "problems": check["problems"],
        })
    return {"readiness": readiness, "inhibits": inhibits}


def historical_releases(paths, state) -> list:
    """모든 release(active 포함)의 이력 요약 — status에 표시. 수정하지 않는다."""
    active_id = ((state or {}).get("active") or {}).get("release")
    rows = []
    for directory in sorted(paths.readiness_releases.glob("*")):
        if not directory.is_dir():
            continue
        check = verify_record_dir(directory, basename=RECORD_BASENAME,
                                  expected_type=READINESS_RECORD_TYPE)
        record = check["record"] or {}
        rows.append({"release": directory.name,
                     "active": directory.name == active_id,
                     "promoted": directory.name in set((state or {}).get("promotion_history") or ()),
                     "valid": check["valid"],
                     "status": record.get("status"),
                     "created_utc": (record.get("record_metadata") or {}).get("created_utc"),
                     "readiness_digest_sha256": record.get("readiness_digest_sha256")})
    return rows


# ------------------------------------------------------------ loader (status·run 공용)

def manifest_has_marker(installation_record: dict) -> bool:
    return all((installation_record or {}).get(k) == v for k, v in MANIFEST_MARKER.items())


def load_readiness_state(paths, *, installation_record: dict, sleeper=time.sleep) -> dict:
    """§9 loader — `vdeter status`와 `vdeter run`이 **같은 함수**를 쓴다.

    반환 snapshot:
      status        MIGRATION_REQUIRED | STATE_UNREADABLE | STATE_CORRUPT | ABSENT | ACTIVE | INHIBITED
      fail_closed   True면 RF(readiness preparation 포함) 전에 거부
      workload_rf_blocked  True면 workload RF는 이 process의 새 preparation PASS 뒤에만 허용(INHIBITED)
      state / generation / active(record·sha·release·valid) / inhibit(record·sha·release) / orphans / problems
    """
    snapshot = {"status": None, "fail_closed": False, "workload_rf_blocked": False,
                "reason_code": None, "state": None, "generation": None,
                "active": None, "inhibit": None, "orphans": {"readiness": [], "inhibits": []},
                "history": [], "problems": []}
    if not manifest_has_marker(installation_record):
        snapshot.update(status="MIGRATION_REQUIRED", fail_closed=True,
                        reason_code="MIGRATION_REQUIRED",
                        problems=["installation manifest has no readiness migration marker "
                                  "(readiness_state_required=true) — run `vdeter setup` "
                                  "(RF-free migration) first"])
        return snapshot
    read = read_state_snapshot(paths.readiness_state, sleeper=sleeper)
    snapshot["problems"].extend(read["problems"])
    if read["outcome"] in ("MISSING", "UNREADABLE"):
        snapshot.update(status="STATE_UNREADABLE", fail_closed=True,
                        workload_rf_blocked=True, reason_code="STATE_UNREADABLE")
        return snapshot
    if read["outcome"] == "INVALID":
        snapshot.update(status="STATE_CORRUPT", fail_closed=True,
                        workload_rf_blocked=True, reason_code="STATE_CORRUPT",
                        state=read["state"])
        return snapshot
    state = read["state"]
    snapshot["state"] = state
    snapshot["generation"] = state["generation"]

    # release id 중복(release와 inhibit가 같은 id) 또는 generation 위반은 STATE_CORRUPT.
    release_ids = {p.name for p in paths.readiness_releases.glob("*") if p.is_dir()}
    inhibit_ids = {p.name for p in paths.readiness_inhibits.glob("*") if p.is_dir()}
    if release_ids & inhibit_ids:
        snapshot.update(status="STATE_CORRUPT", fail_closed=True, workload_rf_blocked=True,
                        reason_code="STATE_CORRUPT")
        snapshot["problems"].append("release/inhibit id collision: %s"
                                    % sorted(release_ids & inhibit_ids))
        return snapshot

    orphans = scan_orphans(paths, state)
    snapshot["orphans"] = orphans
    snapshot["history"] = historical_releases(paths, state)

    inhibit_pointer = state.get("inhibit")
    if inhibit_pointer is not None:
        directory = paths.readiness_inhibits / inhibit_pointer["release"]
        check = verify_record_dir(directory, basename=INHIBIT_BASENAME,
                                  expected_type=INHIBIT_RECORD_TYPE)
        record = check["record"]
        bound = (check["valid"] and check["sha256"] == inhibit_pointer["record_sha256"]
                 and record.get("readiness_digest_sha256")
                 == inhibit_pointer["readiness_digest_sha256"]
                 and record.get("cause_evidence_sha256") == inhibit_pointer["evidence_sha256"])
        if not bound:
            snapshot.update(status="STATE_CORRUPT", fail_closed=True, workload_rf_blocked=True,
                            reason_code="INHIBITED_STATE_CORRUPT")
            snapshot["problems"].append(
                "state.inhibit references an inhibit record that is missing, tampered or "
                "unbound (%s)" % "; ".join(check["problems"] or ["pointer/sha mismatch"]))
            snapshot["inhibit"] = {"release": inhibit_pointer["release"], "valid": False}
            return snapshot
        snapshot["inhibit"] = {"release": inhibit_pointer["release"], "valid": True,
                               "record": record, "sha256": check["sha256"],
                               "reason_code": record.get("reason_code")}

    active_pointer = state.get("active")
    if active_pointer is not None:
        directory = paths.readiness_releases / active_pointer["release"]
        check = verify_record_dir(directory, basename=RECORD_BASENAME,
                                  expected_type=READINESS_RECORD_TYPE)
        record = check["record"]
        bound = (check["valid"] and check["sha256"] == active_pointer["record_sha256"]
                 and record.get("readiness_digest_sha256")
                 == active_pointer["readiness_digest_sha256"]
                 and record.get("status") == "PASS")
        snapshot["active"] = {"release": active_pointer["release"], "valid": bool(bound),
                              "record": record if bound else None,
                              "sha256": check["sha256"],
                              "problems": check["problems"] if not bound else []}
        if not bound and not check["problems"]:
            snapshot["active"]["problems"] = ["state pointer sha/digest differs from the release"]

    # orphan 판정 — invalid readiness orphan은 RF 전 차단, inhibit orphan은 유효해도 INHIBITED.
    invalid_orphans = [o for o in orphans["readiness"] if o["status"] == "ORPHAN_INVALID"]
    if invalid_orphans:
        snapshot["problems"].append("invalid orphan readiness release(s): %s"
                                    % [o["release"] for o in invalid_orphans])
    if orphans["inhibits"]:
        snapshot["problems"].append("unreferenced inhibit record(s): %s"
                                    % [o["release"] for o in orphans["inhibits"]])

    if snapshot["inhibit"] is not None or orphans["inhibits"]:
        snapshot.update(status="INHIBITED", workload_rf_blocked=True,
                        reason_code=(snapshot["inhibit"] or {}).get("reason_code")
                        or "ORPHAN_INHIBIT")
        snapshot["fail_closed"] = bool(invalid_orphans)
        return snapshot
    if invalid_orphans:
        snapshot.update(status="ACTIVE" if snapshot["active"] else "ABSENT",
                        fail_closed=True, reason_code="ORPHAN_INVALID")
        return snapshot
    if snapshot["active"] is None:
        if state["generation"] >= 1:
            snapshot.update(status="ABSENT", reason_code="ABSENT")
        return snapshot
    snapshot.update(status="ACTIVE", reason_code="ACTIVE")
    return snapshot


# ------------------------------------------------------------ applicability (status·run 공용)

def evaluate_applicability(snapshot: dict, current_binding: dict) -> dict:
    """active readiness ↔ 현재 stable binding 대조 — status/run이 같은 함수를 쓴다.

    반환 {"operational_readiness": APPLICABLE|STALE|INHIBITED|ABSENT|MIGRATION_REQUIRED|
          STATE_UNREADABLE|STATE_CORRUPT, "reasons": [...], "digest_match": bool|None,
          "inhibit_applies": bool, "active_release": id|None}"""
    status = snapshot["status"]
    result = {"operational_readiness": status, "reasons": list(snapshot.get("problems") or []),
              "digest_match": None, "inhibit_applies": False,
              "active_release": ((snapshot.get("active") or {}).get("release"))}
    current_digest = readiness_digest(current_binding) if current_binding else None
    inhibit = snapshot.get("inhibit")
    if status == "INHIBITED":
        recorded = (inhibit or {}).get("record") or {}
        # inhibit는 같은 stable binding에 적용된다; 다른 binding이면 stale로만 표시하되 orphan
        # inhibit(참조 없음)는 항상 fail-closed다.
        if inhibit is None:
            result["inhibit_applies"] = True
        else:
            result["inhibit_applies"] = (recorded.get("readiness_digest_sha256")
                                         == current_digest) or current_digest is None
            if not result["inhibit_applies"]:
                result["reasons"].append("inhibit was recorded for a different stable binding; "
                                         "it still blocks workload RF until a new preparation "
                                         "PASSes in this process")
                result["inhibit_applies"] = True
        return result
    if status != "ACTIVE":
        return result
    active = snapshot["active"]
    if not active.get("valid"):
        result["operational_readiness"] = "STALE"
        result["reasons"].extend(active.get("problems") or ["active record not applicable"])
        return result
    recorded_binding = active["record"]["binding"]
    reasons = binding_differences(recorded_binding, current_binding)
    result["digest_match"] = active["record"]["readiness_digest_sha256"] == current_digest
    if reasons or not result["digest_match"]:
        result["operational_readiness"] = "STALE"
        result["reasons"].extend(reasons or ["readiness digest differs"])
        return result
    result["operational_readiness"] = "APPLICABLE"
    return result


def project_authority(*, qualification_applicable: bool, readiness_applicable: bool) -> str:
    """§3.2 단일 요약 투영 — 영속 권위 등급일 뿐 workload RF 허가가 아니다."""
    if qualification_applicable:
        return "QUALIFIED"
    if readiness_applicable:
        return "OPERATIONAL_READY"
    return "UNVERIFIED"


# ------------------------------------------------------------ migration (RF-free)

def ensure_initial_state(paths, *, now=None) -> dict:
    """setup/migration: `state.json`이 없으면 generation=1·active=null·inhibit=null을 원자 생성·fsync·
    재독해 검증한다. 이미 있으면 검증만 하고 **덮어쓰지 않는다**. 반환 {"created", "state", "problems"}."""
    paths.readiness_dir.mkdir(parents=True, exist_ok=True)
    for directory in (paths.readiness_candidate, paths.readiness_releases,
                      paths.readiness_inhibits):
        directory.mkdir(parents=True, exist_ok=True)
    read = read_state_snapshot(paths.readiness_state, retries=1)
    if read["outcome"] == "OK":
        return {"created": False, "state": read["state"], "problems": []}
    if read["outcome"] == "MISSING":
        state = initial_state(now=now)
        write_state_atomic(paths.readiness_state, state)
        return {"created": True, "state": state, "problems": []}
    # 손상·불완전 state는 setup이 조용히 재생성하지 않는다 — inhibit 의미를 지울 수 있다.
    raise StateError("readiness state.json exists but is %s (%s) — it is not regenerated by "
                     "setup; investigate var/readiness before continuing"
                     % (read["outcome"], "; ".join(read["problems"])))
