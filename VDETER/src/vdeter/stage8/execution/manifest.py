"""`.poc.toml` manifest의 fail-closed 검증(실행 명세 §6.3, `S8-POC-REQ-01`).

Python file을 import·compile·실행하지 않고(§6.1) manifest만 parse·검증한다. 실패는
:class:`ManifestError`로 유계 변환하며 reason은 비밀정보 없는 요약이다(§6.1, §12.4).
검증 통과 시 Stage 3 public :class:`PocManifestDescriptor`를 돌려준다.
"""

from __future__ import annotations

import re
import tomllib

from ..poc_api import protocol
from ..poc_api.types import PocManifestDescriptor
from .limits import MAX_MANIFEST_BYTES

#: 지원 manifest schema(단일 정의는 protocol.MANIFEST_SCHEMA).
SUPPORTED_SCHEMA = protocol.MANIFEST_SCHEMA
#: 고정 공격자 endpoint(§4.1, `POC-D02`).
REQUIRED_ATTACKER_ENDPOINT = "MASS-B"
#: manifest가 요청할 수 있는 target 상한 집합(§9.3).
ALLOWED_TARGET_UNIVERSE = frozenset(protocol.LOGICAL_TARGETS)
#: action 요청 총수 system 상한(연결 API limits와 동일 근거).
from ..poc_api.limits import MAX_WORKLOAD_ACTION_REQUESTS

_POC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENTRYPOINT_RE = re.compile(r"^(?P<file>[A-Za-z0-9_.-]+\.py):(?P<obj>[A-Za-z_][A-Za-z0-9_]*)$")
_SCENARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ManifestError(Exception):
    """manifest 검증 실패(fail-closed). reason은 비밀 없는 요약이다."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _require(cond: bool, reason: str) -> None:
    if not cond:
        raise ManifestError(reason)


def _str_list(value, field_name: str) -> tuple:
    _require(isinstance(value, list), f"{field_name}는 list여야 합니다")
    out = []
    for item in value:
        _require(isinstance(item, str), f"{field_name} 원소는 문자열이어야 합니다")
        out.append(item)
    return tuple(out)


def parse_and_validate(
    raw: bytes,
    *,
    script_basename: str,
    script_sha256: str,
    manifest_sha256: str,
) -> PocManifestDescriptor:
    """manifest bytes를 parse·검증해 descriptor를 만든다. 실패는 :class:`ManifestError`.

    `script_basename`은 sidecar 규칙(exact-basename `.py`)에 따른 실제 script 파일명이며
    entrypoint의 file 부분과 일치해야 한다. hash는 catalog/snapshot가 파일 bytes에서
    계산해 넘긴다(§6.3).
    """

    _require(isinstance(raw, (bytes, bytearray)), "manifest는 bytes여야 합니다")
    _require(len(raw) <= MAX_MANIFEST_BYTES, "manifest가 64 KiB 상한을 초과했습니다")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("manifest가 UTF-8이 아닙니다")
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError, ValueError) as error:
        raise ManifestError(f"TOML parse 실패: {error.__class__.__name__}")

    _require(isinstance(data, dict), "manifest 최상위는 table이어야 합니다")

    # schema / version
    _require(data.get("schema") == SUPPORTED_SCHEMA,
             f"schema는 {SUPPORTED_SCHEMA}여야 합니다")

    poc_id = data.get("poc_id")
    _require(isinstance(poc_id, str) and bool(_POC_ID_RE.fullmatch(poc_id)),
             "poc_id 형식 오류(소문자·숫자·하이픈, 최대 63자)")

    version = data.get("version")
    _require(isinstance(version, str) and bool(_VERSION_RE.fullmatch(version)),
             "version은 X.Y.Z 형식이어야 합니다")

    entrypoint = data.get("entrypoint")
    _require(isinstance(entrypoint, str), "entrypoint는 문자열이어야 합니다")
    ep = _ENTRYPOINT_RE.fullmatch(entrypoint)
    _require(ep is not None, "entrypoint는 '<file>.py:<Class>' 형식이어야 합니다")
    _require(ep.group("file") == script_basename,
             "entrypoint의 파일명이 sidecar script 파일명과 다릅니다")

    compatible = _str_list(data.get("compatible_scenarios", []), "compatible_scenarios")
    _require(len(compatible) >= 1, "compatible_scenarios는 최소 1개여야 합니다")
    for scenario in compatible:
        _require(bool(_SCENARIO_RE.fullmatch(scenario)),
                 "compatible_scenarios 원소 형식 오류")

    attacker = data.get("attacker_endpoint")
    _require(attacker == REQUIRED_ATTACKER_ENDPOINT,
             "attacker_endpoint는 MASS-B여야 합니다")

    targets = _str_list(data.get("allowed_targets", []), "allowed_targets")
    _require(len(targets) >= 1, "allowed_targets는 최소 1개여야 합니다")
    _require(frozenset(targets) <= ALLOWED_TARGET_UNIVERSE,
             "allowed_targets는 {ROC, MASS-A}의 부분집합이어야 합니다")
    _require(len(set(targets)) == len(targets), "allowed_targets에 중복이 있습니다")

    caps = _str_list(data.get("capabilities", []), "capabilities")
    _require(frozenset(caps) <= protocol.KNOWN_CAPABILITIES,
             "capabilities에 system allowlist 밖 항목이 있습니다")

    triggers = _str_list(data.get("triggers", []), "triggers")
    _require(frozenset(triggers) <= protocol.KNOWN_TRIGGERS,
             "triggers에 system allowlist 밖 항목이 있습니다")

    required = _str_list(data.get("required_capabilities", []), "required_capabilities")
    _require(frozenset(required) <= protocol.KNOWN_CAPABILITIES,
             "required_capabilities에 system allowlist 밖 항목이 있습니다")
    _require(frozenset(required) <= frozenset(caps),
             "required_capabilities는 capabilities의 부분집합이어야 합니다")

    max_actions = data.get("max_actions_requested")
    _require(isinstance(max_actions, int) and not isinstance(max_actions, bool),
             "max_actions_requested는 정수여야 합니다")
    _require(1 <= max_actions <= MAX_WORKLOAD_ACTION_REQUESTS,
             f"max_actions_requested는 1..{MAX_WORKLOAD_ACTION_REQUESTS} 범위여야 합니다")

    return PocManifestDescriptor(
        poc_id=poc_id,
        version=version,
        entrypoint=entrypoint,
        compatible_scenarios=compatible,
        attacker_endpoint=attacker,
        allowed_targets=targets,
        requested_capabilities=caps,
        requested_triggers=triggers,
        required_capabilities=required,
        script_sha256=script_sha256,
        manifest_sha256=manifest_sha256,
        max_actions_requested=max_actions,
    )


__all__ = (
    "SUPPORTED_SCHEMA",
    "REQUIRED_ATTACKER_ENDPOINT",
    "ALLOWED_TARGET_UNIVERSE",
    "ManifestError",
    "parse_and_validate",
)
