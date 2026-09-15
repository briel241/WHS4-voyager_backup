"""Stage 7 TEST plan loader (`stage7` §15).

`Stage7TestPlanLoader`는 TEST-only `orchestration.toml` sidecar, immutable
scenario plan과 세 endpoint resolved identity 및 `Stage7ResolvedSystemPlan`
digest를 결속해 `Stage7ResolvedTestPlan`을 만든다. full plan은 Scenario
Controller와 test adapter만 소비한다.

P1에서만 closed record와 system-plan digest를 결속한 최소 immutable
`Stage7P1QualificationMemberPlan` exact bytes/hash를 별도 산출한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.results import Ok, internal_error
from ..canonical_json import canonical_sha256, encode_canonical_json, sha256_hex
from ..ipc import TEST_BODY_SCHEMA, TEST_PROTOCOL, is_relpath, is_run_id, is_u63

try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    _toml = None

_CONTROLLER_KEYS = frozenset(
    {
        "controller_protocol",
        "controller_body_schema",
        "scenario_horizon_ns",
        "evidence_root_relpath",
    }
)

#: §15 P1 closed record canonical 표.
P1_MEMBER_PLAN_TABLE = {
    "TC-STAGE7-004": {
        "present_member_set": ("MASS-A", "ROC"),
        "excluded_member_set": ("MASS-B",),
        "path_ids": ("C1", "C2"),
        "qualification_edge": "MASS-A",
        "readiness_profile": "ROC_RF_SINGLE_CHILD",
    },
    "TC-STAGE7-005": {
        "present_member_set": ("MASS-B", "ROC"),
        "excluded_member_set": ("MASS-A",),
        "path_ids": ("C3", "C4"),
        "qualification_edge": "MASS-B",
        "readiness_profile": "ROC_RF_SINGLE_CHILD",
    },
    "TC-STAGE7-006": {
        "present_member_set": ("MASS-A", "MASS-B"),
        "excluded_member_set": ("ROC",),
        "path_ids": ("C5", "C6"),
        "qualification_edge": None,
        "readiness_profile": "MASS_LOCAL_TEST_READY",
    },
}

_CANONICAL_MEMBER_ORDER = ("MASS-A", "MASS-B", "ROC")


def _error(code, message, **details):
    return internal_error(
        Layer.RUNTIME,
        code,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class Stage7P1QualificationMemberPlan:
    """P1 최소 immutable projection (§15). scenario payload/fault/판정은 없다."""

    step_id: str
    test_case_id: str
    qualification_profile: str
    time_profile: str
    present_member_set: tuple
    excluded_member_set: tuple
    path_ids: tuple
    qualification_edge: str | None
    readiness_profile: str
    system_plan_sha256: str
    endpoint_config_sha256s: tuple  # ((endpoint_id, sha256), ...)

    def canonical_value(self) -> dict:
        return {
            "plan_type": "STAGE7_P1_QUALIFICATION_MEMBER_PLAN_V1",
            "step_id": self.step_id,
            "test_case_id": self.test_case_id,
            "qualification_profile": self.qualification_profile,
            "time_profile": self.time_profile,
            "present_member_set": list(self.present_member_set),
            "excluded_member_set": list(self.excluded_member_set),
            "path_ids": list(self.path_ids),
            "qualification_edge": self.qualification_edge,
            "readiness_profile": self.readiness_profile,
            "system_plan_sha256": self.system_plan_sha256,
            "endpoint_config_sha256s": [list(item) for item in self.endpoint_config_sha256s],
        }

    def exact_bytes(self) -> bytes:
        return encode_canonical_json(self.canonical_value())

    def sha256(self) -> str:
        return sha256_hex(self.exact_bytes())


@dataclass(frozen=True, slots=True)
class Stage7ResolvedTestPlan:
    """TEST-only resolved plan. Runtime/endpoint에는 전달하지 않는다."""

    run_id: str
    data_link_id: int
    controller_protocol: str
    controller_body_schema: str
    scenario_horizon_ns: int
    evidence_root_relpath: str
    system_plan_sha256: str
    endpoint_config_sha256s: tuple
    scenario_plan_sha256: str
    p1_member_plan: Stage7P1QualificationMemberPlan | None
    resolved_sha256: str


class Stage7TestPlanLoader:
    """orchestration sidecar와 scenario plan을 resolved test plan으로 만든다."""

    __slots__ = ()

    def load(
        self,
        orchestration_toml_text: str,
        *,
        run_id: str,
        data_link_id: int,
        system_plan_sha256: str,
        endpoint_config_sha256s: dict,
        scenario_plan_value: dict,
        p1_test_case_id: str | None = None,
        p1_step_id: str | None = None,
    ):
        if _toml is None:  # pragma: no cover
            return _error(ErrorCode.CONFIG_PARSE_FAILED, "TOML parser가 없습니다")
        if not is_run_id(run_id):
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "run_id 위반")
        if data_link_id not in (17, 19):
            return _error(
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                "data_link_id는 17 또는 19여야 합니다 (자동 선택·전환·fallback 금지)",
            )
        try:
            data = _toml.loads(orchestration_toml_text)
        except Exception as error:  # noqa: BLE001
            return _error(ErrorCode.CONFIG_PARSE_FAILED, f"TOML parse 실패: {error}")
        unknown = sorted(set(data) - {"controller"})
        if unknown:
            return _error(ErrorCode.CONFIG_UNKNOWN_FIELD, "unknown key", keys=tuple(unknown))
        controller = data.get("controller")
        if not isinstance(controller, dict) or set(controller) != _CONTROLLER_KEYS:
            return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "[controller] key 집합 위반")
        if controller["controller_protocol"] != TEST_PROTOCOL:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "controller_protocol 위반")
        if controller["controller_body_schema"] != TEST_BODY_SCHEMA:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "controller_body_schema 위반")
        horizon = controller["scenario_horizon_ns"]
        if isinstance(horizon, bool) or not is_u63(horizon):
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "scenario_horizon_ns 위반")
        if not is_relpath(controller["evidence_root_relpath"]):
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "evidence_root_relpath 위반")

        members = tuple(
            (endpoint_id, endpoint_config_sha256s[endpoint_id])
            for endpoint_id in _CANONICAL_MEMBER_ORDER
        )
        p1_plan = None
        if p1_test_case_id is not None:
            table = P1_MEMBER_PLAN_TABLE.get(p1_test_case_id)
            if table is None:
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID,
                    "P1 test case가 canonical 표에 없습니다",
                    test_case=p1_test_case_id,
                )
            union = tuple(
                sorted(
                    table["present_member_set"] + table["excluded_member_set"],
                    key=_CANONICAL_MEMBER_ORDER.index,
                )
            )
            if union != _CANONICAL_MEMBER_ORDER:
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID, "P1 member 합집합 위반"
                )
            p1_plan = Stage7P1QualificationMemberPlan(
                step_id=p1_step_id or f"p1-{p1_test_case_id.lower()}",
                test_case_id=p1_test_case_id,
                qualification_profile="PAIRWISE_EDGE_QUALIFICATION_V1",
                time_profile="PAIRWISE_HOST_TIME_QUALIFICATION_V1",
                present_member_set=table["present_member_set"],
                excluded_member_set=table["excluded_member_set"],
                path_ids=table["path_ids"],
                qualification_edge=table["qualification_edge"],
                readiness_profile=table["readiness_profile"],
                system_plan_sha256=system_plan_sha256,
                endpoint_config_sha256s=members,
            )

        scenario_sha = canonical_sha256(scenario_plan_value)
        resolved_value = {
            "plan_type": "STAGE7_RESOLVED_TEST_PLAN_V1",
            "run_id": run_id,
            "data_link_id": data_link_id,
            "controller_protocol": TEST_PROTOCOL,
            "controller_body_schema": TEST_BODY_SCHEMA,
            "scenario_horizon_ns": horizon,
            "evidence_root_relpath": controller["evidence_root_relpath"],
            "system_plan_sha256": system_plan_sha256,
            "endpoint_config_sha256s": [list(item) for item in members],
            "scenario_plan_sha256": scenario_sha,
            "p1_member_plan_sha256": None if p1_plan is None else p1_plan.sha256(),
        }
        return Ok(
            Stage7ResolvedTestPlan(
                run_id=run_id,
                data_link_id=data_link_id,
                controller_protocol=TEST_PROTOCOL,
                controller_body_schema=TEST_BODY_SCHEMA,
                scenario_horizon_ns=horizon,
                evidence_root_relpath=controller["evidence_root_relpath"],
                system_plan_sha256=system_plan_sha256,
                endpoint_config_sha256s=members,
                scenario_plan_sha256=scenario_sha,
                p1_member_plan=p1_plan,
                resolved_sha256=canonical_sha256(resolved_value),
            )
        )


__all__ = (
    "P1_MEMBER_PLAN_TABLE",
    "Stage7P1QualificationMemberPlan",
    "Stage7ResolvedTestPlan",
    "Stage7TestPlanLoader",
)
