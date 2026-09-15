"""Stage 7 Runtime Coordinator (`stage7` §6, §14, §16, §16.1).

`Stage7RuntimeCoordinator`는 system plan에 결속된 세 공통 endpoint launcher를
child로 시작하고 group preflight·lifecycle, host/RF time candidate와 atomic
barrier, coordinated inhibit만 소유한다. public VDE-TER API나 상위 MASS/ROC
application이 아니며 Service scenario·payload·업무 판단을 소유하지 않는다.

transport는 주입형이다. Linux 배치는 `transport.py`의 Unix-domain
`SOCK_SEQPACKET` 채널을, software 시험은 in-process 채널을 사용한다.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from .canonical_json import canonical_sha256, encode_canonical_json, sha256_hex
from .configuration import Stage7ConfigurationLoader, Stage7OperationalGainInput
from .errors import Stage7ControlPlane, Stage7PrivateErrorCode, Stage7PrivateFault
from .ipc import (
    ACTIVE_BRIDGE_COMMANDS,
    COMMAND_TIMEOUTS_S,
    ENDPOINT_IDS,
    NON_BARRIER_COMMANDS,
    RUNTIME_PROTOCOL,
    SERVICE_START_INTENT_TIMEOUT_NS,
    SERVICE_START_TBB_PUBLISH_TIMEOUT_NS,
    Stage7IpcError,
    body_set_sha256,
    body_sha256_of,
    build_barrier_manifest,
    validate_runtime_command,
    validate_runtime_event,
    validate_runtime_response,
)

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    _toml = None

SYSTEM_PROFILE = "STAGE7_PI5_THREE_ENDPOINT_V1"
RUNTIME_BODY_SCHEMA = "STAGE7_RUNTIME_BODY_V1"
CANONICAL_SOCKET_ROOT = "/run/user/<uid>/vdeter-stage7/runtime"

_SYSTEM_KEYS = frozenset(
    {
        "profile",
        "runtime_coordinator_protocol",
        "runtime_body_schema",
        "socket_root",
        "member_count",
        "service_start_intent_timeout_ns",
        "service_start_tbb_publish_timeout_ns",
    }
)
_MEMBER_KEYS = frozenset({"endpoint_id", "config_relpath"})


def _error(code, message, **details):
    return internal_error(
        Layer.RUNTIME,
        code,
        ErrorCategory.VALIDATION,
        message,
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class Stage7ResolvedSystemPlan:
    """immutable system plan (§15). Runtime Coordinator만 전체를 소비한다."""

    profile: str
    socket_root: str
    member_count: int
    service_start_intent_timeout_ns: int
    service_start_tbb_publish_timeout_ns: int
    members: tuple  # ((endpoint_id, config_relpath, config_sha256), ...)
    data_profile_sha256: str
    deployment_sha256: str
    tbb_fixture_id: str
    registry_fingerprint: str
    source_sha256: str
    resolved_sha256: str

    def member_config_sha(self, endpoint_id: str) -> str:
        for member_id, _relpath, sha in self.members:
            if member_id == endpoint_id:
                return sha
        raise KeyError(endpoint_id)


class Stage7SystemPlanLoader:
    """`system.toml`과 세 endpoint resolved config를 exact 검증해 system plan을 만든다."""

    __slots__ = ()

    def load(
        self,
        system_toml_text: str,
        member_resolved_configs: dict,
        *,
        data_profile_sha256: str,
        deployment_sha256: str,
    ):
        if _toml is None:  # pragma: no cover
            return _error(ErrorCode.CONFIG_PARSE_FAILED, "TOML parser가 없습니다")
        try:
            data = _toml.loads(system_toml_text)
        except Exception as error:  # noqa: BLE001
            return _error(ErrorCode.CONFIG_PARSE_FAILED, f"TOML parse 실패: {error}")
        unknown = sorted(set(data) - {"system", "members"})
        if unknown:
            return _error(
                ErrorCode.CONFIG_UNKNOWN_FIELD, "unknown top-level key", keys=tuple(unknown)
            )
        system = data.get("system")
        if not isinstance(system, dict):
            return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "[system] table이 필수입니다")
        if set(system) != _SYSTEM_KEYS:
            return _error(
                ErrorCode.CONFIG_SCHEMA_INVALID,
                "[system] key 집합 위반",
                keys=tuple(sorted(set(system) ^ _SYSTEM_KEYS)),
            )
        if system["profile"] != SYSTEM_PROFILE:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "system profile 위반")
        if system["runtime_coordinator_protocol"] != RUNTIME_PROTOCOL:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "runtime protocol 위반")
        if system["runtime_body_schema"] != RUNTIME_BODY_SCHEMA:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "runtime body schema 위반")
        socket_root = system["socket_root"]
        if not isinstance(socket_root, str) or not socket_root:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "socket_root 위반")
        if system["member_count"] != 3 or isinstance(system["member_count"], bool):
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "member_count는 3이어야 합니다")
        for key, expected in (
            ("service_start_intent_timeout_ns", SERVICE_START_INTENT_TIMEOUT_NS),
            ("service_start_tbb_publish_timeout_ns", SERVICE_START_TBB_PUBLISH_TIMEOUT_NS),
        ):
            value = system[key]
            if isinstance(value, bool) or value != expected:
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID, f"{key}는 canonical 값이어야 합니다"
                )
        members = data.get("members")
        if not isinstance(members, list) or len(members) != 3:
            return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "[[members]]는 세 entry여야 합니다")
        resolved_members = []
        seen = []
        for index, entry in enumerate(members):
            if not isinstance(entry, dict) or set(entry) != _MEMBER_KEYS:
                return _error(ErrorCode.CONFIG_SCHEMA_INVALID, "member entry key 위반")
            endpoint_id = entry["endpoint_id"]
            if endpoint_id != ENDPOINT_IDS[index]:
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID,
                    "canonical member 순서 MASS-A,MASS-B,ROC 위반",
                    endpoint_id=str(endpoint_id),
                )
            if endpoint_id in seen:
                return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "duplicate member")
            seen.append(endpoint_id)
            resolved = member_resolved_configs.get(endpoint_id)
            if resolved is None:
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID,
                    "member resolved config가 없습니다",
                    endpoint_id=endpoint_id,
                )
            resolved_members.append(
                (endpoint_id, entry["config_relpath"], resolved.resolved_sha256)
            )

        # 세 endpoint의 host/deployment/TBB/registry identity exact 검증.
        first = member_resolved_configs[ENDPOINT_IDS[0]]
        for endpoint_id in ENDPOINT_IDS[1:]:
            other = member_resolved_configs[endpoint_id]
            if (
                other.registry_fingerprint != first.registry_fingerprint
                or other.tbb_fixture_id != first.tbb_fixture_id
                or other.deployment_manifest_sha256 != first.deployment_manifest_sha256
                or other.data_profile_sha256 != first.data_profile_sha256
                or other.run_id != first.run_id
                or other.run_mode != first.run_mode
                or other.host_execution.host_machine_id
                != first.host_execution.host_machine_id
            ):
                return _error(
                    ErrorCode.CONFIG_SEMANTIC_INVALID,
                    "세 endpoint identity가 exact-equal이 아닙니다",
                    endpoint_id=endpoint_id,
                )
        if data_profile_sha256 != first.data_profile_sha256:
            return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, "data profile digest 불일치")

        resolved_value = {
            "resolved_type": "STAGE7_RESOLVED_SYSTEM_PLAN_V1",
            "profile": SYSTEM_PROFILE,
            "socket_root": socket_root,
            "member_count": 3,
            "service_start_intent_timeout_ns": SERVICE_START_INTENT_TIMEOUT_NS,
            "service_start_tbb_publish_timeout_ns": SERVICE_START_TBB_PUBLISH_TIMEOUT_NS,
            "members": [
                {"endpoint_id": m, "config_relpath": r, "config_sha256": s}
                for m, r, s in resolved_members
            ],
            "run_id": first.run_id,
            "run_mode": first.run_mode,
            "data_profile_sha256": data_profile_sha256,
            "deployment_sha256": deployment_sha256,
            "tbb_fixture_id": first.tbb_fixture_id,
            "registry_fingerprint": first.registry_fingerprint,
        }
        return Ok(
            Stage7ResolvedSystemPlan(
                profile=SYSTEM_PROFILE,
                socket_root=socket_root,
                member_count=3,
                service_start_intent_timeout_ns=SERVICE_START_INTENT_TIMEOUT_NS,
                service_start_tbb_publish_timeout_ns=SERVICE_START_TBB_PUBLISH_TIMEOUT_NS,
                members=tuple(resolved_members),
                data_profile_sha256=data_profile_sha256,
                deployment_sha256=deployment_sha256,
                tbb_fixture_id=first.tbb_fixture_id,
                registry_fingerprint=first.registry_fingerprint,
                source_sha256=sha256_hex(system_toml_text.encode("utf-8")),
                resolved_sha256=canonical_sha256(resolved_value),
            )
        )


class Stage7BarrierFailure(RuntimeError):
    def __init__(self, message: str, *, private_code: Stage7PrivateErrorCode):
        super().__init__(message)
        self.private_code = private_code


class Stage7RuntimeCoordinator:
    """§16.1 barrier/ledger 로직. transport와 process spawn은 주입형이다.

    `channels`: {endpoint_id: channel}. channel은 `request(message_dict) ->
    response_dict`와 `poll_events() -> tuple[dict, ...]`를 제공한다.
    """

    __slots__ = (
        "_plan",
        "_run_id",
        "_channels",
        "_host_raw_clock",
        "_lifecycle_generation",
        "_bridge_generation",
        "_bridge_members",
        "_intent_ledger",
        "_tbb_ledger",
        "_events",
        "_faults",
        "_inhibited",
        "_admission",
        "_command_log",
        "_present_members",
    )

    def __init__(
        self,
        plan: Stage7ResolvedSystemPlan,
        channels: dict,
        *,
        host_raw_clock,
        run_id: str,
        lifecycle_generation: int = 1,
        present_members: tuple = ENDPOINT_IDS,
    ) -> None:
        self._plan = plan
        self._run_id = run_id
        self._channels = dict(channels)
        self._host_raw_clock = host_raw_clock
        self._lifecycle_generation = lifecycle_generation
        self._bridge_generation = None
        # active bridge에 참여한 endpoint 집합. None이면 아직 bridge가 없거나
        # 전원 참여로 간주한다. 불참 endpoint의 bridge_generation은 계속 None
        # 이므로 expected 값도 None으로 stamp해야 stale 거부가 나지 않는다.
        self._bridge_members = None
        self._intent_ledger = {}
        self._tbb_ledger = {}
        self._events = []
        self._faults = []
        self._inhibited = False
        self._admission = None
        self._command_log = []
        self._present_members = tuple(present_members)

    # --- 관찰 -----------------------------------------------------------------

    @property
    def inhibited(self) -> bool:
        return self._inhibited

    @property
    def bridge_generation(self):
        return self._bridge_generation

    @property
    def admission(self):
        return self._admission

    def faults(self) -> tuple:
        return tuple(self._faults)

    def command_log(self) -> tuple:
        return tuple(self._command_log)

    def intent_ledger_snapshot(self) -> tuple:
        return tuple(sorted(self._intent_ledger.items()))

    def tbb_ledger_snapshot(self) -> tuple:
        return tuple(sorted(self._tbb_ledger.items()))

    # --- envelope 조립 ----------------------------------------------------------

    def _now(self) -> int:
        return int(self._host_raw_clock())

    def _fault(self, private_code: Stage7PrivateErrorCode, message: str, **fields):
        self._faults.append(
            Stage7PrivateFault(
                private_code=private_code,
                phase=fields.pop("phase", "runtime"),
                endpoint_id=fields.pop("endpoint_id", "-"),
                run_id=self._run_id,
                control_plane=Stage7ControlPlane.RUNTIME,
                details={"message": message, **fields},
            )
        )

    def _make_command(
        self,
        command_name: str,
        endpoint_id: str,
        body: dict,
        *,
        barrier_id,
        barrier_phase,
        barrier_manifest_sha256,
        commit_at_host_raw_ns=None,
        deadline_override_ns=None,
    ) -> dict:
        issued = self._now()
        deadline = (
            issued + COMMAND_TIMEOUTS_S[command_name] * 1_000_000_000
            if deadline_override_ns is None
            else deadline_override_ns
        )
        full_body = dict(body)
        if command_name not in NON_BARRIER_COMMANDS:
            full_body.setdefault(
                "logical_action",
                "active_bridge_install"
                if command_name in ACTIVE_BRIDGE_COMMANDS
                else command_name,
            )
            full_body.setdefault("phase_plan_sha256", self._plan.resolved_sha256)
            full_body.setdefault(
                "commit_at_host_raw_ns",
                commit_at_host_raw_ns if commit_at_host_raw_ns is not None else issued,
            )
        digest = body_sha256_of(full_body)
        message = {
            "protocol": RUNTIME_PROTOCOL,
            "kind": "command",
            "command_name": command_name,
            "run_id": self._run_id,
            "resolved_system_plan_sha256": self._plan.resolved_sha256,
            "command_id": str(_uuid.uuid4()),
            "endpoint_id": endpoint_id,
            "expected_lifecycle_generation": self._lifecycle_generation,
            "expected_bridge_generation": (
                self._bridge_generation
                if self._bridge_members is None or endpoint_id in self._bridge_members
                else None
            ),
            "barrier_id": barrier_id,
            "barrier_phase": barrier_phase,
            "barrier_manifest_sha256": barrier_manifest_sha256,
            "issued_host_raw_ns": issued,
            "deadline_host_raw_ns": deadline,
            "request_body_sha256": digest,
            "body_sha256": digest,
            "body": full_body,
        }
        validate_runtime_command(message)
        return message

    def _request(self, endpoint_id: str, message: dict) -> dict:
        channel = self._channels[endpoint_id]
        response = channel.request(message)
        validate_runtime_response(response, message)
        self._command_log.append(
            {
                "endpoint_id": endpoint_id,
                "command_name": message["command_name"],
                "command_id": message["command_id"],
                "barrier_phase": message["barrier_phase"],
                "status": response["body"]["status"],
            }
        )
        return response

    # --- barrier engine (§16) ----------------------------------------------------

    def run_barrier(
        self,
        command_name: str,
        per_endpoint_bodies: dict,
        *,
        commit_at_host_raw_ns=None,
        deadline_override_ns=None,
        commit_command_name=None,
        abort_command_name=None,
        members=None,
        concurrent_commit: bool = False,
    ):
        """PREPARE→COMMIT (실패 시 ABORT/emergency)를 §16 규칙으로 실행한다.

        반환: {endpoint_id: commit_response}. 실패는 `Stage7BarrierFailure`다.
        `concurrent_commit=True`는 공통 plan 시각의 TX/RX가 동시에 실행돼야
        하는 RF 측정 barrier에서 COMMIT을 병렬 전송한다.
        """

        if self._inhibited:
            raise Stage7BarrierFailure(
                "TX-inhibited group에는 새 barrier를 열 수 없습니다",
                private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
            )
        members = tuple(members if members is not None else self._present_members)
        barrier_id = str(_uuid.uuid4())
        commit_at = commit_at_host_raw_ns if commit_at_host_raw_ns is not None else self._now()
        digests = []
        prepared_messages = {}
        for endpoint_id in members:
            body = dict(per_endpoint_bodies[endpoint_id])
            full_body = dict(body)
            full_body.setdefault(
                "logical_action",
                "active_bridge_install"
                if command_name in ACTIVE_BRIDGE_COMMANDS
                else (commit_command_name or command_name).replace(
                    "prepare_active_bridge", "active_bridge_install"
                ),
            )
            full_body.setdefault("phase_plan_sha256", self._plan.resolved_sha256)
            full_body.setdefault("commit_at_host_raw_ns", commit_at)
            digests.append((endpoint_id, body_sha256_of(full_body)))
            prepared_messages[endpoint_id] = full_body
        manifest = build_barrier_manifest(tuple(digests))

        prepared = []
        for endpoint_id in members:
            message = self._make_command(
                command_name,
                endpoint_id,
                prepared_messages[endpoint_id],
                barrier_id=barrier_id,
                barrier_phase="PREPARE",
                barrier_manifest_sha256=manifest,
                commit_at_host_raw_ns=commit_at,
                deadline_override_ns=deadline_override_ns,
            )
            try:
                response = self._request(endpoint_id, message)
            except Exception as error:  # noqa: BLE001 - peer loss
                self._abort_barrier(
                    abort_command_name or command_name,
                    prepared,
                    prepared_messages,
                    barrier_id,
                    manifest,
                    commit_at,
                )
                self._fault(
                    Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                    f"PREPARE peer 손실: {type(error).__name__}",
                    endpoint_id=endpoint_id,
                )
                raise Stage7BarrierFailure(
                    "PREPARE peer 손실",
                    private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                ) from error
            if response["body"]["status"] != "PREPARED":
                self._abort_barrier(
                    abort_command_name or command_name,
                    prepared,
                    prepared_messages,
                    barrier_id,
                    manifest,
                    commit_at,
                )
                raise Stage7BarrierFailure(
                    f"{endpoint_id} PREPARE {response['body']['status']}",
                    private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                )
            prepared.append(endpoint_id)

        commits = {}
        commit_name = commit_command_name or command_name
        commit_messages = {
            endpoint_id: self._make_command(
                commit_name,
                endpoint_id,
                prepared_messages[endpoint_id],
                barrier_id=barrier_id,
                barrier_phase="COMMIT",
                barrier_manifest_sha256=manifest,
                commit_at_host_raw_ns=commit_at,
                deadline_override_ns=deadline_override_ns,
            )
            for endpoint_id in members
        }
        if concurrent_commit:
            # RF 측정/시각 barrier: TX·RX endpoint의 COMMIT handler가 공통 plan
            # 시각에 동시에 실행돼야 하므로 병렬 전송한다. 채널은 endpoint별로
            # 분리돼 있어 각 thread가 자기 채널만 사용한다.
            import threading as _threading

            outcomes = {}

            def _commit_one(endpoint_id: str) -> None:
                try:
                    outcomes[endpoint_id] = ("ok", self._request(endpoint_id, commit_messages[endpoint_id]))
                except Exception as error:  # noqa: BLE001
                    outcomes[endpoint_id] = ("error", error)

            threads = [
                _threading.Thread(target=_commit_one, args=(endpoint_id,), daemon=True)
                for endpoint_id in members
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            for endpoint_id in members:
                kind, value = outcomes.get(endpoint_id, ("error", RuntimeError("no outcome")))
                if kind == "error":
                    self.emergency_inhibit(
                        Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED.value, barrier_id
                    )
                    raise Stage7BarrierFailure(
                        "COMMIT peer 손실",
                        private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                    ) from value
                status = value["body"]["status"]
                if status in ("REJECTED", "TIMED_OUT"):
                    self.emergency_inhibit(
                        Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED.value, barrier_id
                    )
                    raise Stage7BarrierFailure(
                        f"{endpoint_id} COMMIT {status}",
                        private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                    )
                commits[endpoint_id] = value
            return commits
        for endpoint_id in members:
            message = commit_messages[endpoint_id]
            try:
                response = self._request(endpoint_id, message)
            except Exception as error:  # noqa: BLE001
                self.emergency_inhibit(
                    Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED.value, barrier_id
                )
                raise Stage7BarrierFailure(
                    "COMMIT peer 손실",
                    private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                ) from error
            status = response["body"]["status"]
            if status in ("REJECTED", "TIMED_OUT"):
                self.emergency_inhibit(
                    Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED.value, barrier_id
                )
                raise Stage7BarrierFailure(
                    f"{endpoint_id} COMMIT {status}",
                    private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
                )
            commits[endpoint_id] = response
        return commits

    def _abort_barrier(
        self, abort_name, prepared, bodies, barrier_id, manifest, commit_at
    ) -> None:
        for endpoint_id in prepared:
            try:
                message = self._make_command(
                    abort_name,
                    endpoint_id,
                    bodies[endpoint_id],
                    barrier_id=barrier_id,
                    barrier_phase="ABORT",
                    barrier_manifest_sha256=manifest,
                    commit_at_host_raw_ns=commit_at,
                )
                self._request(endpoint_id, message)
            except Exception:  # noqa: BLE001
                continue

    def emergency_inhibit(self, private_code: str, root_cause_id: str) -> None:
        self._inhibited = True
        for endpoint_id in self._present_members:
            try:
                message = self._make_command(
                    "emergency_inhibit",
                    endpoint_id,
                    {"private_code": private_code[:64], "root_cause_id": root_cause_id},
                    barrier_id=None,
                    barrier_phase=None,
                    barrier_manifest_sha256=None,
                )
                self._request(endpoint_id, message)
            except Exception:  # noqa: BLE001
                continue

    # --- Service-start admission (§16.1) -----------------------------------------

    def open_service_admission(self, *, commit_at_host_raw_ns=None):
        commit_at = (
            commit_at_host_raw_ns if commit_at_host_raw_ns is not None else self._now()
        )
        window_id = str(_uuid.uuid4())
        start_deadline = commit_at + SERVICE_START_INTENT_TIMEOUT_NS
        tbb_deadline = start_deadline + SERVICE_START_TBB_PUBLISH_TIMEOUT_NS
        body = {
            "intent_window_id": window_id,
            "intent_timeout_ns": SERVICE_START_INTENT_TIMEOUT_NS,
            "tbb_publish_timeout_ns": SERVICE_START_TBB_PUBLISH_TIMEOUT_NS,
            "service_start_deadline_host_raw_ns": start_deadline,
            "tbb_publish_deadline_host_raw_ns": tbb_deadline,
        }
        deadline_override = commit_at + 5_000_000_000
        commits = self.run_barrier(
            "open_service_admission",
            {endpoint_id: dict(body) for endpoint_id in self._present_members},
            commit_at_host_raw_ns=commit_at,
            deadline_override_ns=deadline_override,
        )
        self._admission = {
            "intent_window_id": window_id,
            "service_start_deadline_host_raw_ns": start_deadline,
            "tbb_publish_deadline_host_raw_ns": tbb_deadline,
            "commit_at_host_raw_ns": commit_at,
        }
        return Ok(self._admission)

    def receive_event(self, endpoint_id: str, event: dict):
        """endpoint socket에서 도착한 event 하나를 ledger에 반영한다."""

        try:
            validate_runtime_event(event)
        except Stage7IpcError as error:
            self._fault(
                Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID,
                str(error),
                endpoint_id=endpoint_id,
            )
            self.emergency_inhibit(
                Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID.value, str(_uuid.uuid4())
            )
            return _error(ErrorCode.INTERNAL_ERROR, "CONTROL_MESSAGE_INVALID")
        if event["endpoint_id"] != endpoint_id:
            self._fault(
                Stage7PrivateErrorCode.CONTROL_PEER_INVALID,
                "socket peer와 endpoint_id 불일치",
                endpoint_id=endpoint_id,
            )
            self.emergency_inhibit(
                Stage7PrivateErrorCode.CONTROL_PEER_INVALID.value, str(_uuid.uuid4())
            )
            return _error(ErrorCode.INTERNAL_ERROR, "CONTROL_PEER_INVALID")
        if (
            event["run_id"] != self._run_id
            or event["resolved_system_plan_sha256"] != self._plan.resolved_sha256
        ):
            self._fault(
                Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID,
                "run/plan 불일치 event",
                endpoint_id=endpoint_id,
            )
            self.emergency_inhibit(
                Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID.value, str(_uuid.uuid4())
            )
            return _error(ErrorCode.INTERNAL_ERROR, "CONTROL_MESSAGE_INVALID")
        self._events.append((endpoint_id, event))
        body = event["body"]
        if body["event_type"] == "STATE_CHANGED" and event["command_id"] is None:
            if body["endpoint_state"] == "SERVICE_START_PENDING":
                return self._record_intent(endpoint_id, event)
            if body["endpoint_state"] == "TBB_ACTIVE":
                return self._record_tbb_active(endpoint_id, event)
        return Ok(None)

    def _record_intent(self, endpoint_id: str, event: dict):
        admission = self._admission
        if admission is None:
            return self._invalid_event(endpoint_id, "committed admission window가 없습니다")
        body = event["body"]
        if body["lifecycle_generation"] != self._lifecycle_generation:
            return self._invalid_event(endpoint_id, "old/future lifecycle generation intent")
        if event["issued_host_raw_ns"] >= admission["service_start_deadline_host_raw_ns"]:
            return self._invalid_event(endpoint_id, "intent cutoff 이후 event")
        key = (
            self._run_id,
            self._plan.resolved_sha256,
            self._lifecycle_generation,
            admission["intent_window_id"],
            endpoint_id,
        )
        digest = body["artifact_sha256"]
        existing = self._intent_ledger.get(key)
        if existing is not None:
            if existing == digest:
                # idempotent duplicate: evidence만 남기고 무변경.
                return Ok("DUPLICATE")
            return self._invalid_event(endpoint_id, "conflicting intent digest")
        self._intent_ledger[key] = digest
        return Ok("RECORDED")

    def _record_tbb_active(self, endpoint_id: str, event: dict):
        body = event["body"]
        if body["lifecycle_generation"] != self._lifecycle_generation:
            return self._invalid_event(endpoint_id, "old/future generation TBB event")
        key = (
            self._run_id,
            self._plan.resolved_sha256,
            self._lifecycle_generation,
            body["bridge_generation"],
            endpoint_id,
        )
        digest = body["artifact_sha256"]
        existing = self._tbb_ledger.get(key)
        if existing is not None:
            if existing == digest:
                return Ok("DUPLICATE")
            return self._invalid_event(endpoint_id, "conflicting TBB digest")
        self._tbb_ledger[key] = digest
        return Ok("RECORDED")

    def _invalid_event(self, endpoint_id: str, reason: str):
        self._fault(
            Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID,
            reason,
            endpoint_id=endpoint_id,
        )
        self.emergency_inhibit(
            Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID.value, str(_uuid.uuid4())
        )
        return _error(ErrorCode.INTERNAL_ERROR, reason)

    def required_intents_complete(self) -> bool:
        if self._admission is None:
            return False
        window = self._admission["intent_window_id"]
        for endpoint_id in self._present_members:
            key = (
                self._run_id,
                self._plan.resolved_sha256,
                self._lifecycle_generation,
                window,
                endpoint_id,
            )
            if key not in self._intent_ledger:
                return False
        return True

    def intent_set(self):
        window = self._admission["intent_window_id"]
        return [
            [
                endpoint_id,
                self._intent_ledger[
                    (
                        self._run_id,
                        self._plan.resolved_sha256,
                        self._lifecycle_generation,
                        window,
                        endpoint_id,
                    )
                ],
            ]
            for endpoint_id in self._present_members
        ]

    def commit_service_start(self):
        """required intent 전부 수신 뒤에만 발행한다 (§16.1)."""

        admission = self._admission
        if admission is None or not self.required_intents_complete():
            raise Stage7BarrierFailure(
                "required intent가 닫히지 않았습니다",
                private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
            )
        now = self._now()
        if now >= admission["service_start_deadline_host_raw_ns"]:
            raise Stage7BarrierFailure(
                "intent cutoff이 지났습니다",
                private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
            )
        intent_set = self.intent_set()
        body = {
            "intent_window_id": admission["intent_window_id"],
            "intent_set": intent_set,
            "intent_set_sha256": body_set_sha256(intent_set),
            "service_start_deadline_host_raw_ns": admission[
                "service_start_deadline_host_raw_ns"
            ],
            "tbb_publish_deadline_host_raw_ns": admission[
                "tbb_publish_deadline_host_raw_ns"
            ],
        }
        return self.run_barrier(
            "commit_service_start",
            {endpoint_id: dict(body) for endpoint_id in self._present_members},
            deadline_override_ns=min(
                self._now() + 5_000_000_000,
                admission["service_start_deadline_host_raw_ns"] - 1,
            )
            if admission["service_start_deadline_host_raw_ns"] > self._now() + 1
            else None,
        )

    def required_tbb_complete(self, bridge_generation) -> bool:
        for endpoint_id in self._present_members:
            key = (
                self._run_id,
                self._plan.resolved_sha256,
                self._lifecycle_generation,
                bridge_generation,
                endpoint_id,
            )
            if key not in self._tbb_ledger:
                return False
        return True

    def tbb_active_set(self, bridge_generation):
        return [
            [
                endpoint_id,
                self._tbb_ledger[
                    (
                        self._run_id,
                        self._plan.resolved_sha256,
                        self._lifecycle_generation,
                        bridge_generation,
                        endpoint_id,
                    )
                ],
            ]
            for endpoint_id in self._present_members
        ]

    def publish_service_start(self, start_result_set, *, bridge_generation, publish_at=None):
        if not self.required_tbb_complete(bridge_generation):
            raise Stage7BarrierFailure(
                "required TBB-active set이 닫히지 않았습니다",
                private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
            )
        admission = self._admission
        publish_at = publish_at if publish_at is not None else self._now() + 1_000_000_000
        if admission is not None and publish_at > admission["tbb_publish_deadline_host_raw_ns"]:
            raise Stage7BarrierFailure(
                "publish가 TBB publish deadline을 넘습니다",
                private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED,
            )
        tbb_set = self.tbb_active_set(bridge_generation)
        body = {
            "start_result_set": start_result_set,
            "start_result_set_sha256": body_set_sha256(start_result_set),
            "tbb_active_set": tbb_set,
            "tbb_active_set_sha256": body_set_sha256(tbb_set),
            "publish_at_host_raw_ns": publish_at,
        }
        return self.run_barrier(
            "publish_service_start",
            {endpoint_id: dict(body) for endpoint_id in self._present_members},
            commit_at_host_raw_ns=publish_at,
            deadline_override_ns=publish_at + 5_000_000_000,
        )


__all__ = (
    "CANONICAL_SOCKET_ROOT",
    "Stage7BarrierFailure",
    "Stage7ResolvedSystemPlan",
    "Stage7RuntimeCoordinator",
    "Stage7SystemPlanLoader",
)
