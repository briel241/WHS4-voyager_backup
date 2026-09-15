#!/usr/bin/env python3
"""Stage 7 MASS/ROC 업무 시나리오 controller (Phase B).

기존 RR scenario controller(`VDETER/apps/stage7_scenario_controller.py`)를 **덮어쓰지
않는다.** 그 controller는 정적 fixture로 전송 특성(단편화·ACK·방향)만 재현하고
판정은 SHA/길이로 한다. 이 controller는 별도 파일로, 실제 ``RocAgent`` /
``MassAgent`` / ``cert_flow`` 업무 흐름을 돌려 SC-VDE-001/005/006을 실증한다.

경로 (지시서 §4 목표 흐름)::

    scenario_runner
    → Stage 7 coordinator
    → 이 MASS/ROC scenario controller
    → endpoint TEST-only socket (inline SUBMIT/STATUS)
    → endpoint process 내부 실제 VdeService
    → public DATA_RECEIVED (launcher event JSONL)
    → 실제 RocAgent / MassAgent / cert_flow
    → MASS가 만든 application ACK 동적 제출
    → ROC가 받은 ACK를 RocAgent.receive()로 처리
    → transfer ID 기반 status와 evidence 기록

transport는 주입형이다. ``MassRocScenarioController``는 채널
(``request(message)->response``)과 event 소스(``poll()`` / ``records`` /
``wait(predicate, timeout)``)만 알며 SOCK_SEQPACKET·B210·소프트웨어 무선을
직접 알지 않는다. 실장비에서는 ``main()``이 launcher의 ``<endpoint>.test.sock``
소켓과 event JSONL을 붙인다. 단위·계약시험에서는 in-process 하네스를 붙인다.

경계 원칙
    - public VdeService API와 wire format을 바꾸지 않는다.
    - private repository/queue/MAC/PHY/Link 내부에 접근하지 않는다.
    - private key는 어떤 evidence에도 남기지 않는다.
    - application bytes만 기록한다. RF frame·Link/MAC/PHY 객체는 노출하지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import sys
import time

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (APP_ROOT, os.path.join(APP_ROOT, "VDETER", "src"),
              os.path.join(APP_ROOT, "VDETER")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.textout import ensure_utf8_output  # noqa: E402

ensure_utf8_output()

from common import codec, config  # noqa: E402
from common import vdeter_bridge as bridge  # noqa: E402
from common.messages import application_ack as ack  # noqa: E402
from common.messages import route_instruction as route  # noqa: E402
from common.outbound import (  # noqa: E402
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
    Outbound,
)
from common.security import certificate as cert  # noqa: E402
from common.security.cert_flow import (  # noqa: E402
    mass_receive_certificate_report,
    roc_issue_certificate_report,
)
from common.security.trust_store import TrustStore  # noqa: E402

from apps.console_transport import add_envelope, strip_envelope  # noqa: E402

from MASS import MassAgent, ShipState  # noqa: E402
from ROC import RocAgent  # noqa: E402


# --------------------------------------------------------------------------
# 주소 매핑 — 응용 MMSI ↔ Stage 7 endpoint/station. 번역은 이 경계에서만.
# --------------------------------------------------------------------------

#: Stage 7 endpoint id → station id (`stage7_scenario_controller.py`와 동일 값).
STATION_BY_ENDPOINT = {"MASS-A": 111_111_111, "MASS-B": 333_333_333, "ROC": 222_222_222}

#: 응용 MMSI ↔ endpoint. 행위자는 MMSI-공간, 전선은 station-공간으로 다룬다.
ENDPOINT_BY_MMSI = {
    config.ROC_MMSI: "ROC",
    config.MASS_MMSI: "MASS-A",
    config.INTRUDER_MMSI: "MASS-B",
}
MMSI_BY_ENDPOINT = {ep: mmsi for mmsi, ep in ENDPOINT_BY_MMSI.items()}
MMSI_BY_STATION = {
    STATION_BY_ENDPOINT[ep]: MMSI_BY_ENDPOINT[ep] for ep in STATION_BY_ENDPOINT
}
ENDPOINT_IDS = ("MASS-A", "MASS-B", "ROC")


class ScenarioError(RuntimeError):
    """시나리오 단계가 계약대로 진행되지 않았다."""


def _default_agents() -> tuple:
    """시나리오 기본 행위자 쌍 (ROC, MASS-A)."""
    return (
        RocAgent(),
        MassAgent(state=ShipState(latitude=35.0, longitude=129.0,
                                  heading=90.0, sog=12.0)),
    )


class Stage7MassRocResult:
    """한 시나리오 실행의 evidence 누적기 — step·transfer·counter."""

    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self.steps: list[dict] = []
        self.transfers: list[dict] = []
        #: 이 시나리오가 제출한 payload sha 전부 — MASS-B 오수신 검사 대상.
        self.submitted_shas: list[str] = []
        self.counters = {
            "transfers_completed": 0,
            "transfers_failed": 0,
            "duplicates": 0,
            "wrong_destination": 0,
            "mismatched_ack": 0,
        }
        self.status = "PENDING"
        self.reason: str | None = None

    def step(self, name: str, ok: bool, **detail) -> None:
        # 판정 키(name/status)는 detail이 덮어쓸 수 없다.
        record = dict(detail)
        record["name"] = name
        record["status"] = "PASS" if ok else "FAIL"
        self.steps.append(record)

    def transfer(self, record: dict) -> None:
        self.transfers.append(record)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "reason": self.reason,
            "steps": self.steps,
            "transfers": self.transfers,
            "counters": self.counters,
        }


# --------------------------------------------------------------------------
# controller — transport 주입형 업무 흐름
# --------------------------------------------------------------------------

class MassRocScenarioController:
    """실제 RocAgent/MassAgent/cert_flow를 endpoint public Service로 돌린다.

    ``channels``: {endpoint_id: channel} — ``channel.request(message)``가
    endpoint test adapter의 response를 돌려준다.
    ``events``: launcher event JSONL 소스 — ``poll()``·``records``·
    ``wait(predicate, timeout)``. in-process 하네스에서는 poll()이 시계를
    진행시키며 DATA_RECEIVED를 수집한다.
    ``controller``: ``vdeter.stage7.testing.controller.Stage7ScenarioController``
    (inline SUBMIT/STATUS 명령 발행자).
    """

    def __init__(self, *, controller, events, now_fn=None,
                 status_timeout_s: float = 120.0,
                 receive_timeout_s: float = 120.0,
                 workload_id: str = "single", trace=None) -> None:
        self._controller = controller
        self._events = events
        if trace is None:
            from apps.stage7_trace import NullTrace
            trace = NullTrace()
        #: 상위 계층 계측기. 계측 유무가 명령 순서·판정을 바꾸지 않는다.
        self._trace = trace
        #: 같은 hardware session에서 여러 workload가 돌 때 step/scenario ID가
        #: 겹치면 adapter replay cache가 이전 응답을 돌려줄 수 있다. 모든
        #: 식별자에 workload child ID를 결속해 namespace를 분리한다.
        self._workload_id = workload_id
        self._now = now_fn if now_fn is not None else time.time
        self._status_timeout_s = status_timeout_s
        self._receive_timeout_s = receive_timeout_s
        self._cancel_requested = False
        self._workload_guard = None

    def cancel_workload(self):
        self._cancel_requested = True

    def _check_activity(self):
        if self._cancel_requested:
            raise ScenarioError("workload cancelled; no further service submissions")
        if self._workload_guard is not None:
            self._workload_guard()

    # ---- inline Service 호출 (public transfer ID / state) -------------------

    def _submit(self, endpoint_id: str, *, step_id: str, destination_station: int,
                payload_format: str, payload: bytes, ack_required: bool) -> tuple[str, dict]:
        """endpoint에서 canonical inline SUBMIT을 시키고 public transfer ID를 얻는다."""
        self._check_activity()
        request = {
            "destination": destination_station,
            "payload_format": payload_format,
            "payload_base64": base64.b64encode(payload).decode("ascii"),
            "ack_required": ack_required,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        }
        response = self._controller.invoke_service_inline(
            endpoint_id, scenario_id=f"mass-roc:{self._workload_id}",
            step_id=f"{self._workload_id}/{step_id}",
            service_operation="SUBMIT", request=request,
        )
        body = response.get("body") or {}
        if body.get("call_outcome") != "OK" or not body.get("transfer_id"):
            raise ScenarioError(
                f"{step_id}: SUBMIT 실패 ({body.get('call_outcome')}, "
                f"{body.get('public_error_code')})"
            )
        return body["transfer_id"], body

    def _status(self, endpoint_id: str, transfer_id: str) -> str | None:
        self._check_activity()
        response = self._controller.invoke_service_inline(
            endpoint_id, scenario_id=f"mass-roc:{self._workload_id}",
            step_id=f"{self._workload_id}/status/{transfer_id[:8]}",
            service_operation="STATUS",
            request={"transfer_id": transfer_id},
        )
        body = response.get("body") or {}
        if body.get("call_outcome") != "OK":
            return None
        return body.get("transfer_state")

    def _wait_terminal(self, endpoint_id: str, transfer_id: str) -> str | None:
        """transfer ID 기반 status가 terminal에 이를 때까지 대기(진행 급식)."""
        deadline = self._now() + self._status_timeout_s
        state = None
        while self._now() < deadline:
            state = self._status(endpoint_id, transfer_id)
            if state in ("COMPLETED", "FAILED", "CANCELLED"):
                return state
            self._events.poll()
            # STATUS 1건마다 endpoint의 TEST adapter가 replay 항목을 영구
            # 보관한다(command_id가 매번 새 uuid4라 재사용되지 않는다).
            # 상주 세션에서는 adapter 인스턴스 하나가 workload 4개·2시간을
            # 살므로 20ms 주기면 endpoint당 1만~1.5만 항목(수십 MB)이 쌓인다.
            # ACK 대기 하한이 65초라 폴링 총량이 그대로 누적량이다.
            # 0.5초로 낮추면 25배 줄고, terminal 감지는 최대 0.5초 늦어질 뿐
            # 판정 창(240초)에는 영향이 없다.
            time.sleep(0.5)
        return state

    def _wait_received(self, endpoint_id: str, payload_sha: str,
                       before: int = 0) -> dict | None:
        """DATA_RECEIVED가 도착할 때까지 대기하고 그 record를 돌려준다."""
        def predicate(records):
            self._check_activity()
            hits = _received(records, endpoint_id, payload_sha)
            return hits[before:] if len(hits) > before else None

        found = self._events.wait(predicate, self._receive_timeout_s)
        return found[0] if found else None

    def _received_count(self, endpoint_id: str, payload_sha: str) -> int:
        self._events.poll()
        return len(_received(self._events.records, endpoint_id, payload_sha))

    # ---- 공통: 명령→수신→ACK→상관 왕복 ------------------------------------

    def _deliver_command(self, result: Stage7MassRocResult, *, step: str,
                         sender_ep: str, receiver_ep: str, payload_format: str,
                         app_payload: bytes, ack_required: bool) -> dict:
        """sender→receiver로 application bytes를 실어 나르고 evidence를 남긴다.

        수신 record는 format·source·destination·SHA·길이·원문 byte 일치를
        전부 대조한다(`received_valid`) — 판정은 호출측 step이 이 값을 쓴다.

        반환: {transfer_id, transfer_state, received(record|None), received_valid}.
        """
        sha = hashlib.sha256(app_payload).hexdigest()
        dest_station = STATION_BY_ENDPOINT[receiver_ep]
        before = self._received_count(receiver_ep, sha)
        result.submitted_shas.append(sha)
        transfer_id, submit_body = self._submit(
            sender_ep, step_id=step, destination_station=dest_station,
            payload_format=payload_format, payload=app_payload,
            ack_required=ack_required,
        )
        self._trace.emit("submit", step=step, sender=sender_ep,
                         receiver=receiver_ep, transfer_id=transfer_id,
                         payload_format=payload_format,
                         payload_sha256=sha, payload_len=len(app_payload),
                         source_station=STATION_BY_ENDPOINT[sender_ep],
                         destination_station=dest_station,
                         ack_required=ack_required)
        state = self._wait_terminal(sender_ep, transfer_id)
        self._trace.emit("transfer_terminal", step=step,
                         transfer_id=transfer_id, terminal_state=state)
        received = self._wait_received(receiver_ep, sha, before=before)
        self._trace.emit("data_received", step=step, receiver=receiver_ep,
                         present=received is not None,
                         received_at_ns=(received or {}).get("received_at_ns"),
                         t_mono=(received or {}).get("t_mono"),
                         original_source=(received or {}).get("original_source"),
                         original_destination=(received or {}).get(
                             "original_destination"))

        # 오목적지·중복 관측 (증분 기준).
        wrong = 0
        for endpoint_id in ENDPOINT_IDS:
            hits = self._received_count(endpoint_id, sha)
            if endpoint_id == receiver_ep:
                delta = hits - before
                if delta > 1:
                    result.counters["duplicates"] += delta - 1
            elif hits:
                wrong += hits
        result.counters["wrong_destination"] += wrong

        if state == "COMPLETED":
            result.counters["transfers_completed"] += 1
        else:
            result.counters["transfers_failed"] += 1

        record = {
            "step": step,
            "scenario_id": result.scenario_id,
            "payload_format": payload_format,
            "payload_len": len(app_payload),
            "payload_sha256": sha,
            "source": MMSI_BY_ENDPOINT[sender_ep],
            "source_station": STATION_BY_ENDPOINT[sender_ep],
            "destination": MMSI_BY_ENDPOINT[receiver_ep],
            "destination_station": dest_station,
            "transfer_id": transfer_id,
            "submit_outcome": submit_body.get("call_outcome"),
            "status_result": state,
            "final_transfer_state": state,
            "wrong_destination": wrong,
            "received": None,
        }
        received_valid = False
        checks = None
        if received is not None:
            # 각 DATA_RECEIVED의 PASS 조건: format·original_source·
            # original_destination·SHA·길이·원문 byte 전부 일치해야 한다.
            checks = {
                "payload_format": received.get("payload_format") == payload_format,
                "original_source": (
                    received.get("original_source")
                    == STATION_BY_ENDPOINT[sender_ep]
                ),
                "original_destination": (
                    received.get("original_destination") == dest_station
                ),
                "payload_sha256": received.get("payload_sha256") == sha,
                "payload_len": received.get("payload_len") == len(app_payload),
                "byte_exact": self._received_payload(received) == app_payload,
            }
            received_valid = all(checks.values())
            record["received"] = {
                "payload_sha256": received.get("payload_sha256"),
                "payload_len": received.get("payload_len"),
                "payload_format": received.get("payload_format"),
                "original_source": received.get("original_source"),
                "original_destination": received.get("original_destination"),
                "received_at_ns": received.get("received_at_ns"),
                "t_mono": received.get("t_mono"),
                "byte_exact": checks["byte_exact"],
                "checks": checks,
                "valid": received_valid,
            }
        result.transfer(record)
        return {"transfer_id": transfer_id, "transfer_state": state,
                "received": received, "received_valid": received_valid,
                "received_checks": checks}

    def _received_payload(self, received: dict) -> bytes:
        return base64.b64decode((received.get("payload_base64") or "").encode("ascii"))

    def _source_mmsi(self, received: dict, *, expect_endpoint: str) -> int:
        """DATA_RECEIVED의 original_source(station)를 응용 MMSI로 번역·검증한다."""
        station = received.get("original_source")
        mmsi = MMSI_BY_STATION.get(station)
        if mmsi is None or station != STATION_BY_ENDPOINT[expect_endpoint]:
            raise ScenarioError(
                f"original_source 불일치: {station!r} "
                f"(기대 {expect_endpoint}={STATION_BY_ENDPOINT[expect_endpoint]})")
        return mmsi

    # ---- SC-VDE-005 노선 계획 교환 ----------------------------------------

    def run_sc_vde_005(self, *, now: float | None = None,
                       agents: tuple | None = None) -> Stage7MassRocResult:
        """SC-VDE-005 노선 계획 교환.

        `agents=(roc, mass)`를 주면 그 행위자로 실행한다 — SC-VDE-006이
        사전 조건으로 이 시나리오를 돌릴 때 상태(교환된 노선)가 본시험으로
        이어지게 하기 위한 것이다.
        """
        result = Stage7MassRocResult("SC-VDE-005")
        now = self._now() if now is None else now
        roc, mass = agents if agents is not None else _default_agents()
        try:
            # ROC: 실제 issue_route — waypoint 2개 이상·turn radius·SOG·leg geometry.
            waypoints = [
                route.waypoint_from_degrees(
                    129.076, 35.102, turn_radius_nm=0.5, planned_sog_knots=12.5,
                    leg_geometry=route.LEG_GEOMETRY_LEXODROME),
                route.waypoint_from_degrees(
                    129.333, 35.208, turn_radius_nm=1.2, planned_sog_knots=8.0,
                    leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
                route.waypoint_from_degrees(129.500, 35.300),
            ]
            command = roc.issue_route(config.MASS_MMSI, waypoints, now=now,
                                      route_type=route.ROUTE_TYPE_MANDATORY)
            original = codec.decode(command.payload)
            linkage = original.linkage_id
            app_command, _mid = strip_envelope(command.payload)

            delivered = self._deliver_command(
                result, step="005-route-command", sender_ep="ROC",
                receiver_ep="MASS-A",
                payload_format=bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND,
                app_payload=app_command, ack_required=True,
            )
            command_ok = (delivered["transfer_state"] == "COMPLETED"
                          and delivered["received_valid"])
            received = delivered["received"]
            result.step("005-command-transfer", command_ok,
                        transfer_id=delivered["transfer_id"],
                        state=delivered["transfer_state"],
                        received_checks=delivered["received_checks"])
            if received is None:
                raise ScenarioError("MASS-A가 노선 명령 DATA_RECEIVED를 받지 못함")

            # MASS: 실제 receive → 상태 반영 확인.
            envelope_payload = add_envelope(
                self._received_payload(received),
                bridge.message_id_for_payload_format(received["payload_format"]),
            )
            command_source = self._source_mmsi(received, expect_endpoint="ROC")
            ack_outbounds = mass.receive(envelope_payload, command_source)
            route_applied = (
                mass.has_route
                and mass.route_linkage_id == linkage
                and mass.remaining_waypoints == 3
                and mass.active_waypoint is not None
                and abs(mass.active_waypoint.planned_sog_knots - 12.5) < 1e-6
                and abs(mass.state.sog - 12.5) < 1e-6
            )
            result.step("005-route-applied", route_applied,
                        linkage_id=linkage,
                        remaining_waypoints=mass.remaining_waypoints,
                        sog=mass.state.sog)
            if not ack_outbounds:
                raise ScenarioError("MASS가 ACK Outbound를 만들지 않음")

            # MASS→ROC: 실제 ACK를 동적 제출.
            ack_out = ack_outbounds[0]
            app_ack, _mid = strip_envelope(ack_out.payload)
            ack_delivered = self._deliver_command(
                result, step="005-ack", sender_ep="MASS-A", receiver_ep="ROC",
                payload_format=bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_ACK,
                app_payload=app_ack, ack_required=False,
            )
            ack_ok = (ack_delivered["transfer_state"] == "COMPLETED"
                      and ack_delivered["received_valid"])
            ack_received = ack_delivered["received"]
            result.step("005-ack-transfer", ack_ok,
                        transfer_id=ack_delivered["transfer_id"],
                        state=ack_delivered["transfer_state"],
                        received_checks=ack_delivered["received_checks"])
            if ack_received is None:
                raise ScenarioError("ROC가 ACK DATA_RECEIVED를 받지 못함")

            # ROC: 실제 receive → 원 PendingCommand answered/acknowledged.
            ack_envelope = add_envelope(
                self._received_payload(ack_received),
                bridge.message_id_for_payload_format(ack_received["payload_format"]),
            )
            ack_source = self._source_mmsi(ack_received, expect_endpoint="MASS-A")
            roc.receive(ack_envelope, ack_source)
            pending = roc.commands.get(linkage)
            answered = (
                pending is not None and pending.answered
                and pending.status == ack.STATUS_ACKNOWLEDGED
                and pending.destination_id == config.MASS_MMSI
                and roc.mismatched_ack_count == 0
            )
            result.counters["mismatched_ack"] = roc.mismatched_ack_count
            source_match = ack_received.get("original_source") == \
                STATION_BY_ENDPOINT["MASS-A"]
            result.step("005-ack-correlated", answered and source_match,
                        linkage_id=linkage,
                        ack_status=None if pending is None else pending.status)

            # MASS-B 오수신 0 — 이 시나리오가 제출한 모든 payload 대상.
            mass_b_zero = all(
                self._received_count("MASS-B", sha) == 0
                for sha in result.submitted_shas
            )
            result.step("005-mass-b-clean", mass_b_zero)

            passed = all(s["status"] == "PASS" for s in result.steps) \
                and result.counters["wrong_destination"] == 0
            result.status = "PASS" if passed else "FAIL"
        except ScenarioError as exc:
            result.status = "FAIL"
            result.reason = str(exc)
        return result

    # ---- SC-VDE-006 원격 조종 (SC-VDE-005 선행) ---------------------------

    def _command_round_trip(self, result: Stage7MassRocResult, *, prefix: str,
                            roc, mass, outbound, now: float) -> None:
        """ROC 명령 1건: 전송→MASS 수신·반영→ACK 역송신→ROC 상관.

        step 이름: `{prefix}-transfer`·`{prefix}-ack-transfer`·
        `{prefix}-ack-correlated`. 상태 반영 판정은 호출측이 별도 step으로 한다.
        반환 대신 result에 기록하고, MASS가 만든 ACK 상관까지 마친다.
        """
        original = codec.decode(outbound.payload)
        linkage = original.linkage_id
        app_command, _mid = strip_envelope(outbound.payload)

        delivered = self._deliver_command(
            result, step=f"{prefix}-command", sender_ep="ROC",
            receiver_ep="MASS-A",
            payload_format=bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND,
            app_payload=app_command, ack_required=True,
        )
        result.step(f"{prefix}-transfer",
                    delivered["transfer_state"] == "COMPLETED"
                    and delivered["received_valid"],
                    transfer_id=delivered["transfer_id"],
                    state=delivered["transfer_state"],
                    received_checks=delivered["received_checks"])
        received = delivered["received"]
        if received is None:
            raise ScenarioError(f"MASS-A가 {prefix} 명령 DATA_RECEIVED를 받지 못함")

        envelope_payload = add_envelope(
            self._received_payload(received),
            bridge.message_id_for_payload_format(received["payload_format"]),
        )
        command_source = self._source_mmsi(received, expect_endpoint="ROC")
        ack_outbounds = mass.receive(envelope_payload, command_source)
        if not ack_outbounds:
            raise ScenarioError(f"MASS가 {prefix} ACK를 만들지 않음")

        app_ack, _mid = strip_envelope(ack_outbounds[0].payload)
        ack_delivered = self._deliver_command(
            result, step=f"{prefix}-ack", sender_ep="MASS-A", receiver_ep="ROC",
            payload_format=bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_ACK,
            app_payload=app_ack, ack_required=False,
        )
        result.step(f"{prefix}-ack-transfer",
                    ack_delivered["transfer_state"] == "COMPLETED"
                    and ack_delivered["received_valid"],
                    transfer_id=ack_delivered["transfer_id"],
                    state=ack_delivered["transfer_state"],
                    received_checks=ack_delivered["received_checks"])
        ack_received = ack_delivered["received"]
        if ack_received is None:
            raise ScenarioError(f"ROC가 {prefix} ACK DATA_RECEIVED를 받지 못함")

        ack_envelope = add_envelope(
            self._received_payload(ack_received),
            bridge.message_id_for_payload_format(ack_received["payload_format"]),
        )
        ack_source = self._source_mmsi(ack_received, expect_endpoint="MASS-A")
        roc.receive(ack_envelope, ack_source)
        pending = roc.commands.get(linkage)
        result.step(f"{prefix}-ack-correlated",
                    pending is not None and pending.answered
                    and pending.status == ack.STATUS_ACKNOWLEDGED
                    and roc.mismatched_ack_count == 0,
                    linkage_id=linkage,
                    ack_status=None if pending is None else pending.status)
        result.counters["mismatched_ack"] = roc.mismatched_ack_count

    def run_sc_vde_006(self, *, now: float | None = None,
                       standalone: bool = True,
                       agents: tuple | None = None,
                       precondition: Stage7MassRocResult | None = None) -> dict:
        """SC-VDE-006 본시험. 단독 실행이면 SC-VDE-005 사전 조건을 먼저 돌린다.

        사전 조건 005와 본시험은 **같은 행위자 쌍**을 쓴다 — 005가 교환한
        노선 상태 위에서 원격 조종이 시작돼야 하며, 그 연속성은
        `006-precondition-state` step이 실제 상태로 검증한다.

        `precondition`에 이미 실행한 005 결과를 주면(반드시 같은 `agents`
        쌍으로 실행한 것) 사전 조건을 다시 돌리지 않고 그 결과를 쓴다 —
        `run all`이 불필요한 RF command/ACK 왕복 한 세트를 아끼는 경로다.

        조종은 두 단계로 시험한다 (fallback은 SOG를 덮어쓰므로 분리):
        ① heading setpoint + waypoint 계획 SOG, ② Anchoring fallback.
        각각 상태 반영과 ACK 상관을 별도 step으로 판정한다.

        반환: {"precondition": <005 result dict or None>,
               "precondition_reused": <재사용 여부>,
               "result": <006 result dict>}.
        """
        now = self._now() if now is None else now
        if agents is not None:
            roc, mass = agents
        else:
            roc, mass = _default_agents()

        reused = precondition is not None
        pre = precondition
        if pre is None and standalone:
            # 사전 조건 005를 **같은 agent 쌍**으로 실행 — 상태가 이어진다.
            pre = self.run_sc_vde_005(now=now, agents=(roc, mass))
        precondition_dict = pre.to_dict() if pre is not None else None
        if pre is not None and pre.status != "PASS":
            result = Stage7MassRocResult("SC-VDE-006")
            result.status = "FAIL"
            result.reason = "SC-VDE-005 사전 조건 실패"
            return {"precondition": precondition_dict,
                    "precondition_reused": reused,
                    "result": result.to_dict()}

        result = Stage7MassRocResult("SC-VDE-006")
        try:
            # 사전 조건 완료 **상태** 검증 — 기록이 아니라 MASS의 실제 상태로.
            precondition_linkage = mass.route_linkage_id
            result.step(
                "006-precondition-state",
                mass.has_route
                and precondition_linkage != route.LINKAGE_ID_NOT_AVAILABLE,
                route_linkage_id=precondition_linkage,
                remaining_waypoints=mass.remaining_waypoints,
                sog=mass.state.sog,
            )

            # ① heading setpoint + 계획 SOG (fallback 없음 — SOG 반영을 본다).
            steering = roc.issue_route(
                config.MASS_MMSI,
                [
                    route.waypoint_from_degrees(129.05, 35.10, planned_sog_knots=6.0),
                    route.waypoint_from_degrees(129.10, 35.133, planned_sog_knots=4.0),
                ],
                now=now,
                route_type=route.ROUTE_TYPE_RECOMMENDED,
                heading_setpoint=92.5,
                fallback_state=route.FALLBACK_NONE,
            )
            app_steering, _mid = strip_envelope(steering.payload)
            self._command_round_trip(result, prefix="006-steering",
                                     roc=roc, mass=mass, outbound=steering,
                                     now=now)
            result.step("006-control-applied",
                        mass.heading_setpoint_degrees == 92.5
                        and abs(mass.state.sog - 6.0) < 1e-6
                        and mass.state.autonomy_mode == route_autonomy_rc()
                        and mass.fallback_state == route.FALLBACK_NONE,
                        heading=mass.heading_setpoint_degrees,
                        sog=mass.state.sog,
                        autonomy_mode=mass.state.autonomy_mode,
                        fallback=mass.fallback_name)

            # 멱등: 동일 조종 명령을 실제 Service로 재전송 → 재적용 없이 ACK만.
            before_duplicate = mass.duplicate_count
            waypoint_index_before = mass._waypoint_index
            resend = self._deliver_command(
                result, step="006-control-resend", sender_ep="ROC",
                receiver_ep="MASS-A",
                payload_format=bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND,
                app_payload=app_steering, ack_required=True,
            )
            if resend["received"] is None:
                raise ScenarioError("MASS-A가 재전송 명령 DATA_RECEIVED를 받지 못함")
            resend_envelope = add_envelope(
                self._received_payload(resend["received"]),
                bridge.message_id_for_payload_format(
                    resend["received"]["payload_format"]),
            )
            dup_outbounds = mass.receive(
                resend_envelope,
                self._source_mmsi(resend["received"], expect_endpoint="ROC"),
            )
            idempotent = (
                resend["transfer_state"] == "COMPLETED"
                and resend["received_valid"]
                and mass.duplicate_count == before_duplicate + 1
                and mass._waypoint_index == waypoint_index_before
                and bool(dup_outbounds)
            )
            result.step("006-idempotent-resend", idempotent,
                        duplicate_count=mass.duplicate_count,
                        resend_transfer_id=resend["transfer_id"],
                        resend_state=resend["transfer_state"])

            # ② fallback 지시 — 별도 명령으로 실제 반영(정지)까지 본다.
            fallback_command = roc.issue_route(
                config.MASS_MMSI, [], now=now,
                route_type=route.ROUTE_TYPE_RECOMMENDED,
                fallback_state=route.FALLBACK_ANCHORING,
            )
            self._command_round_trip(result, prefix="006-fallback",
                                     roc=roc, mass=mass,
                                     outbound=fallback_command, now=now)
            result.step("006-fallback-applied",
                        mass.fallback_name == "Anchoring"
                        and mass.fallback_state == route.FALLBACK_ANCHORING
                        and mass.state.sog == 0.0
                        and mass.state.autonomy_mode == route_autonomy_rc(),
                        fallback=mass.fallback_name,
                        sog=mass.state.sog,
                        autonomy_mode=mass.state.autonomy_mode)

            # MASS-B 오수신 0 — 이 시나리오가 제출한 모든 payload 대상.
            mass_b_zero = all(
                self._received_count("MASS-B", sha) == 0
                for sha in result.submitted_shas
            )
            result.step("006-mass-b-clean", mass_b_zero)

            passed = all(s["status"] == "PASS" for s in result.steps) \
                and result.counters["wrong_destination"] == 0
            result.status = "PASS" if passed else "FAIL"
        except ScenarioError as exc:
            result.status = "FAIL"
            result.reason = str(exc)
        return {"precondition": precondition_dict,
                "precondition_reused": reused,
                "result": result.to_dict()}

    # ---- SC-VDE-001 X.509 인증서 (정상/위조) ------------------------------

    def run_sc_vde_001(self, *, certificate_case: str = "normal",
                       now: float | None = None) -> Stage7MassRocResult:
        """정상 또는 위조 인증서 한 case를 돌린다.

        정상: 검증 통과 후 TrustStore 저장, 재수신 시 중복 저장 없음.
        위조: 구조 유지·서명만 변조 — transfer는 COMPLETED 가능하나 MASS가
        ``bad-signature``로 거부하고 TrustStore 불변.
        """
        result = Stage7MassRocResult(f"SC-VDE-001[{certificate_case}]")
        now = self._now() if now is None else now
        day = 86_400.0
        try:
            if not cert.is_standard_backend():
                # 실제 ECDSA P-256 backend를 요구한다 (지시서 §7).
                raise ScenarioError(
                    "SC-VDE-001은 표준 ECDSA P-256 backend가 필요하다 "
                    "(cryptography 미설치 — 대체 HMAC backend는 보안 판정 불가)")

            # ROC: CA와 vessel 인증서 생성. CA trust anchor는 실행 동안만 메모리 보유.
            ca_key, ca_public = cert.generate_key_pair()
            ca_der = cert.build_certificate(
                subject="ROC-CA", issuer="ROC-CA",
                subject_public_key=ca_public, issuer_private_key=ca_key,
                serial_number=1, not_before=now - day, not_after=now + 365 * day,
                roc_id="ROC-01", is_ca=True)
            _, vessel_public = cert.generate_key_pair()
            vessel_der = cert.build_certificate(
                subject=f"MASS-{STATION_BY_ENDPOINT['MASS-A']}", issuer="ROC-CA",
                subject_public_key=vessel_public, issuer_private_key=ca_key,
                serial_number=2, not_before=now - day, not_after=now + 30 * day,
                mass_id="MASS-01", roc_id="ROC-01",
                mmsi=str(STATION_BY_ENDPOINT["MASS-A"]), imo="9074729")

            if certificate_case == "forged":
                forged = bytearray(vessel_der)
                forged[-1] ^= 0x01              # 구조 유지, 서명만 변조.
                vessel_der = bytes(forged)

            outbound = roc_issue_certificate_report(
                vessel_der, mass_id=STATION_BY_ENDPOINT["MASS-A"])
            report_body = outbound.payload    # cert_flow 계약 — envelope 없음.
            # payload 길이는 실행마다 기록(고정값 판정 금지, 지시서 §7).
            result.step("001-report-built", True,
                        payload_len=len(report_body),
                        certificate_case=certificate_case)

            delivered = self._deliver_command(
                result, step="001-certificate", sender_ep="ROC",
                receiver_ep="MASS-A",
                payload_format=bridge.PAYLOAD_FORMAT_CERTIFICATE_REPORT,
                app_payload=report_body, ack_required=True,
            )
            result.step("001-transfer",
                        delivered["transfer_state"] == "COMPLETED"
                        and delivered["received_valid"],
                        transfer_id=delivered["transfer_id"],
                        state=delivered["transfer_state"],
                        received_checks=delivered["received_checks"])
            received = delivered["received"]
            if received is None:
                raise ScenarioError("MASS-A가 인증서 DATA_RECEIVED를 받지 못함")

            # MASS: 실제 검증 — 정상은 저장, 재수신 멱등; 위조는 거부·불변.
            received_body = self._received_payload(received)
            trust = TrustStore()
            outcomes = mass_receive_certificate_report(received_body, trust, ca_der, now)
            first = outcomes[0] if outcomes else (None, False, "no-entry")
            again = mass_receive_certificate_report(received_body, trust, ca_der, now)

            if certificate_case == "forged":
                security_ok = (
                    first[1] is False and first[2] == "bad-signature"
                    and len(trust) == 0
                )
                result.step("001-forged-rejected", security_ok,
                            reason=first[2], trust_store_size=len(trust))
                result.step("001-not-stored", len(trust) == 0,
                            trust_store_size=len(trust))
            else:
                stored_ok = first[1] is True and first[2] == "ok" and len(trust) == 1
                result.step("001-verified-stored", stored_ok,
                            reason=first[2], trust_store_size=len(trust))
                # 재수신 멱등 — 저장소 크기 불변.
                result.step("001-idempotent-store", len(trust) == 1,
                            trust_store_size_after_resend=len(trust))
                # again은 통과했으나 크기 불변임을 함께 본다.
                result.step("001-resend-ok",
                            bool(again) and again[0][1] is True,
                            trust_store_size=len(trust))

            # MASS-B 오수신 0 — 이 시나리오가 제출한 모든 payload 대상.
            mass_b_zero = all(
                self._received_count("MASS-B", sha) == 0
                for sha in result.submitted_shas
            )
            result.step("001-mass-b-clean", mass_b_zero)

            passed = all(s["status"] == "PASS" for s in result.steps) \
                and result.counters["wrong_destination"] == 0
            result.status = "PASS" if passed else "FAIL"
        except ScenarioError as exc:
            result.status = "FAIL"
            result.reason = str(exc)
        return result


def route_autonomy_rc() -> int:
    from common.messages import position_report as pos
    return pos.AUTONOMY_RC


# --------------------------------------------------------------------------
# event 소스 — launcher event JSONL tail (RR controller와 동일 형식)
# --------------------------------------------------------------------------

def _received(records, endpoint_id, sha):
    return [
        r for r in records[endpoint_id]
        if r.get("event_type") == "DATA_RECEIVED" and r.get("payload_sha256") == sha
    ]


class EventLog:
    """launcher event JSONL tail — public event만 본다 (RR controller와 동일).

    `events_dir`를 주면 그 디렉터리를 직접 읽는다 (persistent session에서는
    event JSONL이 session run-root에 있고 workload child의 run-root와 다르다).

    `start_at_end=True`면 시작 시점의 파일 끝을 baseline으로 잡고 그 이후
    event만 판정한다 — 같은 session에서 앞선 workload가 남긴 stale event가
    이번 workload의 PASS 조건에 섞이지 않게 하는 격리 장치다.
    """

    def __init__(self, run_root: str, *, events_dir: str | None = None,
                 start_at_end: bool = False) -> None:
        self._dir = events_dir or os.path.join(run_root, "evidence",
                                               "service_events")
        self._offsets = {ep: 0 for ep in ENDPOINT_IDS}
        self.records = {ep: [] for ep in ENDPOINT_IDS}
        self.baseline = {ep: 0 for ep in ENDPOINT_IDS}
        if start_at_end:
            for endpoint_id in ENDPOINT_IDS:
                path = os.path.join(self._dir, f"{endpoint_id}.jsonl")
                try:
                    self._offsets[endpoint_id] = os.path.getsize(path)
                except OSError:
                    self._offsets[endpoint_id] = 0
                self.baseline[endpoint_id] = self._offsets[endpoint_id]

    def poll(self) -> None:
        for endpoint_id in ENDPOINT_IDS:
            path = os.path.join(self._dir, f"{endpoint_id}.jsonl")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    handle.seek(self._offsets[endpoint_id])
                    chunk = handle.read()
                    self._offsets[endpoint_id] = handle.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.records[endpoint_id].append(json.loads(line))
                except ValueError:
                    continue

    def wait(self, predicate, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.poll()
            found = predicate(self.records)
            if found:
                return found
            time.sleep(0.1)
        return None


class PocWorkloadHost:
    """workload를 소유한 이 프로세스가 PoC supervisor를 호스팅한다(연결 §3.1, P01).

    장치는 launcher가 단독 소유하므로 provider seam은 §16.2 TEST adapter op으로만 장치에 닿는다
    (`apps/stage8_launcher_seams`). 이 class는 `poc_run`이 읽는 device 경계 공급자 계약을 만족시킨다.

    관측(P09)은 actor의 실제 PHY 복호 + 수신기 내부 복호 구간 gate에서만 온다.
    TEST 소켓은 실제 RX 채널·첫 표본 장치 시각을 운반한다. 다른 endpoint의 Service
    이벤트나 polling 시각으로 관측을 합성하지 않는다. 대상 application 효과는 별도 receipt다.
    """

    def __init__(self, *, controller, events, actor_endpoint: str = "MASS-B") -> None:
        from apps.stage8_launcher_seams import assemble_launcher_provider

        self._events = events
        self._controller = controller
        self._actor = actor_endpoint
        self._provider, self._bindings = assemble_launcher_provider(
            controller, actor_endpoint=actor_endpoint, endpoint_ids=ENDPOINT_IDS,
            station_id_by_endpoint=dict(STATION_BY_ENDPOINT))
        self._exchange = None

    # --- poc_run device 경계 계약 --------------------------------------------

    def selection_capabilities(self):
        """A(설치 capability)만 답한다 — 설치 증언이 유일한 근거다.

        이전 구현은 장치 seam의 `dual_supported`(worker에 read_tuning/apply_manual_tuning/
        restore_tuning **메서드가 존재하는가**)로 판정했다. 메서드 존재는 설치 적합성의 근거가
        아니며 미지원 설치에서도 광고될 수 있었다(fail-open). 이제 설치 증언만 본다.
        """
        from apps.standalone.poc_run import installed_server_config
        return installed_server_config()

    def prepare_poc_scope(self, descriptor):
        from apps.stage8_collision_exchange import CollisionExchange
        from apps.stage8_launcher_seams import assemble_launcher_provider
        from apps.standalone.poc_run import DUAL_CHANNEL_CAPABILITY, installed_server_config
        if self._exchange is not None:
            raise RuntimeError("workload RX scope already prepared")
        dual = DUAL_CHANNEL_CAPABILITY in descriptor.requested_capabilities
        # A: 설치 capability(증언). B: runtime readiness(아래 dual_ready). 둘을 섞지 않는다.
        installed = DUAL_CHANNEL_CAPABILITY in installed_server_config().advertised_capabilities()
        self._exchange = CollisionExchange(self._bindings["clients"], self._actor, dual=dual)
        self._exchange.start()
        self._provider, self._bindings = assemble_launcher_provider(
            self._controller, actor_endpoint=self._actor, endpoint_ids=ENDPOINT_IDS,
            station_id_by_endpoint=dict(STATION_BY_ENDPOINT),
            dual_channel_installed=installed)
        self._bindings["actor"].collision_session_id = self._exchange.session_id

    def poc_server_config(self):
        """설치 capability(A)만 돌려준다. `dual_ready`(B)는 여기에 섞지 않는다.

        runtime readiness는 별도 경로(`CollisionExchange.dual_ready`)가 생산·검증·철회하며,
        readiness 미충족은 해당 동작 단계에서 거부된다(capability 광고가 그 gate를 대신하지 않는다).
        """
        from apps.standalone.poc_run import installed_server_config
        return installed_server_config()

    def poc_provider(self):
        if self._exchange is None:
            from apps.stage8_collision_exchange import CollisionExchange
            self._exchange = CollisionExchange(self._bindings["clients"], self._actor)
            self._exchange.start()
            self._bindings["actor"].collision_session_id = self._exchange.session_id
        return self._provider

    def close_collision_exchange(self):
        if self._exchange is not None:
            self._exchange.close()
            return self._exchange.evidence
        return None

    def poc_terminal_source(self):
        return self._bindings["terminal_source"]

    def poc_target_response(self):
        from vdeter.poc.execution.verdict import TargetEffect

        def response(action, result, parent):
            from vdeter.mac.tdma import FRAME_NANOSECONDS
            binding = self._bindings["sink"].receipt_by_group.get(parent.parent_id)
            receipt = None
            if binding is not None and self._exchange is not None:
                start = binding["transmit_at_utc_ns"]
                receipt = self._exchange.target_receipt(parent.addressed_destination,
                    digest=binding["wire_packet_digest"], source=self._actor,
                    start_ns=start - FRAME_NANOSECONDS // 90,
                    end_ns=start + FRAME_NANOSECONDS // 90)
            if receipt is None:
                return TargetEffect.INDETERMINATE, "no unique target application receipt in selected slot neighbourhood"
            return TargetEffect.EFFECT_OBSERVED, (
                f"target application accepted packet={receipt['packet_digest']}; "
                "basis=MAC_PRESENTATION_SERVICE_ACCEPTED; not proof of exploit success")

        return response

    def poc_workload_steps(self, scenario_id):
        from vdeter.poc.execution.runner import scenario_started
        return [scenario_started()]

    # --- 실행 중 실 관측 -------------------------------------------------------

    def new_observations(self):
        """Only the actor's actual, pre-MAC-policy-cleared PHY observations."""
        from vdeter.poc.api.types import ObservationRecord, TuningState, PhysicalChannel
        from vdeter.stage7.configuration import LOWER_CHANNEL_NAME
        out = []
        for record in self._exchange.observations() if self._exchange is not None else ():
            out.append(ObservationRecord(
                physical_channel=PhysicalChannel.LOWER if record["physical_channel"] == LOWER_CHANNEL_NAME else PhysicalChannel.UPPER,
                tuning_state=TuningState.TUNED_OBSERVER,
                observation_id=f"{self._exchange.session_id}:{self._actor}:{record['observation_key']}:{record['device_hw_timestamp_ns']}",
                source_station=str(record["source_station"]),
                destination_station=str(record["destination_station"] or "BROADCAST"),
                device_hw_timestamp_ns=record["device_hw_timestamp_ns"],
                host_mono_timestamp_ns=record["host_mono_timestamp_ns"],
                body={k: v for k, v in record.items() if k not in (
                    "source_station", "destination_station", "physical_channel",
                    "device_hw_timestamp_ns", "host_mono_timestamp_ns")}
                | {"observed_at_endpoint": self._actor, "source_station_basis": "FIXED_PHYSICAL_INPUT_PATH",
                   "address_basis": "NOT_INFERRED_FROM_TRANSMITTER"}))
        return out


# --------------------------------------------------------------------------
# CLI — coordinator가 spawn하는 별도 process
# --------------------------------------------------------------------------

class _AdapterChannel:
    """SeqPacketClient 위의 §16.2 request/response 채널."""

    def __init__(self, client, timeout_s: float = 300.0) -> None:
        from threading import Lock
        self._client = client
        self._timeout_s = timeout_s
        self._request_lock = Lock()

    def request(self, message: dict) -> dict:
        budget = min(self._timeout_s, 1.0) if str(message.get("command_name", "")).startswith("poc_") else self._timeout_s
        deadline = time.monotonic() + budget
        if not self._request_lock.acquire(timeout=budget):
            raise TimeoutError("TEST socket busy")
        try:
            return self._request_locked(message, deadline)
        finally:
            self._request_lock.release()

    def _request_locked(self, message, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("TEST socket request budget exhausted")
        if callable(getattr(type(self._client), "send_bounded", None)):
            self._client.send_bounded(message, timeout=remaining)
        else:
            self._client.send(message)  # legacy in-process test transport
        while time.monotonic() < deadline:
            received = self._client.recv(timeout=min(2.0, max(0.001, deadline - time.monotonic())))
            if received is None:
                continue
            if received.get("command_id") == message["command_id"]:
                return received
        raise TimeoutError(f"test adapter response 초과: {message['command_name']}")


def _connect_channels(run_id: str):
    from vdeter.stage7.testing.controller import Stage7ScenarioController  # noqa: F401
    from vdeter.stage7.transport import SeqPacketClient, runtime_socket_root

    root = runtime_socket_root(run_id)
    channels = {}
    for endpoint_id in ENDPOINT_IDS:
        socket_path = os.path.join(root, f"{endpoint_id}.test.sock")
        deadline = time.monotonic() + 120.0
        client = None
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                try:
                    client = SeqPacketClient(socket_path)
                    break
                except OSError:
                    pass
            time.sleep(0.5)
        if client is None:
            raise ScenarioError(f"{endpoint_id} test socket 연결 실패")
        channels[endpoint_id] = _AdapterChannel(client)
    return channels


def _write_result(run_root: str, scenario_id: str, payload: dict) -> str:
    out_dir = os.path.join(run_root, "mass-roc-events")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{scenario_id.lower().replace('[', '_').replace(']', '')}.json")
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)
    return out


def _install_signal_handlers() -> None:
    """SIGTERM을 첫 Ctrl+C와 같은 정상 종료 절차로 바꾼다.

    coordinator는 session shutdown·workload abort에서 이 child를 `process.terminate()`로
    회수하고(`stage7_coordinator_main._terminate_process_bounded`) **30초를 기다린다**
    (`CHILD_TERMINATE_TIMEOUT_S`). 그런데 이 프로세스에 처분이 없으면 SIGTERM 기본 동작으로
    즉사해 `run_workload`의 `finally`가 실행되지 않는다 — PoC child·socket 정리
    (`abort_prepared`), RX scope 복원과 `collision_model_closed`, `poc-result` 기록이 통째로
    누락된다.

    실측(diag-20260914-142043, dual scope 활성 중 운영자 취소): child가 `exit_code=-15`로
    죽고 `poc-result.json`·`final-result.json`·`workload-trace.jsonl`이 하나도 남지 않았으며
    `$XDG_RUNTIME_DIR/vdeter-poc/<nonce>/poc.sock`이 그대로 잔존했다. 같은 코드의 정상 종료
    회차(라운드 4 diag-20260914-091317)에는 넷 다 정상 기록됐다 — 인터럽트 경로만의 누락이다.

    CLI(`apps/standalone/cli.py::_install_signal_handlers`)가 이미 같은 방식을 쓴다. 여기서
    새 정리 로직이나 새 상한을 만들지 않는다 — 기존 `finally`가 유예 안에서 돌게만 한다.
    """

    def to_interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, to_interrupt)
    except (ValueError, OSError, AttributeError):
        # 주 스레드가 아니거나 플랫폼이 지원하지 않으면 기존 동작 그대로 둔다.
        pass


def main(argv=None) -> int:
    # 인자 해석 전에 설치한다 — 이후 모든 구간이 정리 경로를 타야 한다.
    _install_signal_handlers()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--test-plan-sha", required=True)
    parser.add_argument("--fixtures-dir", required=True,
                        help="START/STOP fixture 디렉터리 (RR controller와 공유)")
    parser.add_argument("--scenario-id", required=True,
                        choices=("SC-VDE-001", "SC-VDE-005", "SC-VDE-006", "all",
                                 "none"))
    parser.add_argument("--certificate-case", default="normal",
                        choices=("normal", "forged", "both"))
    parser.add_argument(
        "--workload-id", default=None,
        help="persistent session의 workload child ID (step/scenario namespace 결속)")
    parser.add_argument(
        "--lifecycle",
        choices=("owned", "session", "session-start", "session-stop"),
        default="owned",
        help="owned(기본): 이 프로세스가 endpoint START/STOP을 소유한다. "
             "session: hardware session이 소유하므로 START/STOP을 하지 않는다. "
             "session-start/session-stop: session 소유자를 대신해 endpoint "
             "lifecycle 한 단계만 수행하고 즉시 종료한다 (시나리오 미실행)")
    parser.add_argument(
        "--gate-dir", default=None,
        help="admission_open gate 디렉터리. 기본값은 <run-root>/controller_gate. "
             "session lifecycle 대행은 자기 run-root가 아니라 session run-root의 "
             "gate를 봐야 하므로 반드시 명시한다")
    parser.add_argument(
        "--events-dir", default=None,
        help="session run-root의 service_events 디렉터리 (session lifecycle 전용)")
    parser.add_argument(
        "--diagnostics", action="store_true",
        help="evidence 상세도를 높인다 (터미널 출력량은 늘리지 않는다)")
    parser.add_argument(
        "--poc-request", default=None,
        help="Stage 8 PoC 요청 JSON 경로. 주면 이 프로세스가 workload-local PoC supervisor를 "
             "호스팅한다(연결 §3.1). 주지 않으면 기존 scenario 실행과 완전히 같다")
    arguments = parser.parse_args(argv)
    workload_id = arguments.workload_id or "single"

    from vdeter.stage7.hw_operations import read_monotonic_raw_ns
    from vdeter.stage7.testing.controller import Stage7ScenarioController

    # START/STOP은 기존 access-controlled fixture 경로를 그대로 쓴다 (§16.2).
    fixtures = {}
    for name in sorted(os.listdir(arguments.fixtures_dir)):
        if name.endswith(".json"):
            with open(os.path.join(arguments.fixtures_dir, name), "rb") as handle:
                data = handle.read()
            fixtures[name] = (data, hashlib.sha256(data).hexdigest())

    channels = _connect_channels(arguments.run_id)
    inline_controller = Stage7ScenarioController(
        run_id=arguments.run_id,
        resolved_test_plan_sha256=arguments.test_plan_sha,
        adapter_channels=channels,
        host_raw_clock=read_monotonic_raw_ns,
    )
    session_owned = arguments.lifecycle == "session"
    events = EventLog(arguments.run_root, events_dir=arguments.events_dir,
                      start_at_end=session_owned)
    from apps.stage7_trace import TraceWriter
    trace = TraceWriter(
        os.path.join(arguments.run_root, "workload-trace.jsonl"),
        session_id=arguments.run_id, workload_id=workload_id,
        diagnostics=arguments.diagnostics)
    controller = MassRocScenarioController(controller=inline_controller,
                                           events=events,
                                           status_timeout_s=240.0,
                                           receive_timeout_s=240.0,
                                           workload_id=workload_id,
                                           trace=trace)
    trace.emit("workload_begin", scenario_id=arguments.scenario_id,
               certificate_case=arguments.certificate_case,
               lifecycle=arguments.lifecycle,
               controller_instance=hex(id(controller)),
               event_cursor_start=dict(events.baseline))

    gate_dir = arguments.gate_dir or os.path.join(arguments.run_root,
                                                  "controller_gate")
    os.makedirs(gate_dir, exist_ok=True)

    def invoke_fixture(endpoint_id, operation, relpath, step_id):
        data, sha = fixtures[relpath]
        response = inline_controller.invoke_service_fixture(
            endpoint_id, scenario_id="mass-roc", step_id=step_id,
            service_operation=operation,
            local_fixture_relpath=relpath, local_fixture_sha256=sha,
        )
        body = response.get("body") or {}
        return body.get("call_outcome") == "OK", body

    if arguments.lifecycle in ("session-start", "session-stop"):
        # session 소유자(coordinator) 대행: endpoint lifecycle 한 단계만
        # 수행한다. adapter socket은 이 프로세스에만 있으므로 START/STOP은
        # 반드시 이 경로를 지난다 — coordinator는 barrier만 소유한다.
        rc = _run_session_lifecycle_step(
            arguments.lifecycle, invoke_fixture, gate_dir, trace)
        trace.close()
        return rc

    if session_owned:
        # hardware session이 endpoint lifecycle을 소유한다. 이 child는
        # application workload만 실행하고 START/STOP·admission을 만지지 않는다.
        print(f"[mass-roc-controller] session workload {workload_id} 시작 "
              f"(endpoint lifecycle 미소유)", flush=True)
    else:
        _own_endpoint_lifecycle_start(
            inline_controller, invoke_fixture, gate_dir)

    # --- Stage 8 PoC: scenario **전에** arm한다(§5.1). 실패면 scenario 자극 0 ---------
    poc_host = None
    poc_prepared = None
    poc_evidence = None
    if arguments.poc_request:
        from apps.standalone import poc_run as _poc_run
        with open(arguments.poc_request, "rb") as handle:
            poc_request = json.loads(handle.read().decode("utf-8"))
        poc_host = PocWorkloadHost(controller=inline_controller, events=events)
        try:
            # 위임 요청은 선택이 **이미 끝난** 것이다(CLI menu 또는 진단 계획이 poc_id를 확정). 해석
            # 규칙(비대화형·확정 선택·baseline 판정)은 RF-off 세션과 같은 `poc_run.snapshot_for_request`
            # 하나가 소유한다 — 이 프로세스는 menu를 띄우지 않고 새 package를 탐색·추천하지 않는다.
            snapshot = _poc_run.snapshot_for_request(
                poc_request, scenario_id=arguments.scenario_id,
                dest_dir=os.path.join(arguments.run_root, "poc-snapshot"),
                server_config=poc_host.selection_capabilities())
            if snapshot is not None:
                poc_prepared = _poc_run.prepare_workload(
                    poc_host, snapshot, scenario_id=arguments.scenario_id,
                    run_id=arguments.run_id, workload_id=workload_id,
                    socket_root=None, src_path=poc_request.get("src_path"))
            else:
                poc_evidence = _poc_run.baseline_evidence(
                    workload_id=workload_id, scenario_id=arguments.scenario_id)
        except Exception as exc:  # noqa: BLE001 - arm 실패는 fail-closed: scenario를 돌리지 않는다
            collision_cleanup_error = None
            try:
                poc_host.close_collision_exchange()
            except Exception as cleanup_exc:
                collision_cleanup_error = str(cleanup_exc)
            poc_evidence = _poc_run.arm_failure_evidence(
                exc, workload_id=workload_id, scenario_id=arguments.scenario_id)
            trace.emit("poc_arm_failed", error=f"{type(exc).__name__}: {exc}")
            _write_result(arguments.run_root, "poc-result", poc_evidence)
            final = {
                "artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1",
                "run_id": arguments.run_id, "workload_id": workload_id,
                "lifecycle": arguments.lifecycle, "scenario_id": arguments.scenario_id,
                "certificate_case": arguments.certificate_case,
                "status": "FAIL",
                "error": "PoC arm 실패 — scenario를 제출하지 않았습니다(fail-closed)",
                "cleanup_errors": [collision_cleanup_error] if collision_cleanup_error else [], "results": {},
                "command_log": inline_controller.command_log,
            }
            _write_result(arguments.run_root, "final-result", final)
            trace.close()
            print("[mass-roc-controller] 종결 FAIL (poc arm)", flush=True)
            return 1
        except BaseException as exc:  # noqa: BLE001 - 운영자 중단: 정리·기록 후 **원형 재전파**
            # arm 구간의 중단(KeyboardInterrupt/SystemExit)은 바로 위 `except Exception`에
            # 걸리지 않아 종결 기록을 한 줄도 남기지 않았다. 자원 회수는 두 곳이 나눠 맡는다:
            #   - PoC child·socket·provider 예약 → `poc_run.prepare_workload`가 이미
            #     cleanup-only로 정리하고(그 evidence를 인터럽트에 싣는다) 원형 재전달한다.
            #   - workload RX scope(`CollisionExchange`) → **이 프로세스가 소유**하므로 여기서
            #     닫는다. `prepare_poc_scope`가 `arm()`보다 **먼저** 열기 때문에 중단 창이
            #     `prepare_workload`의 인터럽트 처리 구간보다 넓다.
            # 새 정리 로직·새 상한·신규 action을 만들지 않는다 — 일반 예외 경로와 같은 호출이다.
            collision_cleanup_error = None
            try:
                poc_host.close_collision_exchange()
            except BaseException as cleanup_exc:  # noqa: BLE001 - 중단 사유를 덮지 않는다
                collision_cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                exc.add_note(f"workload RX scope cleanup: {collision_cleanup_error}")
            # 값을 합성하지 않는다. 실제로 수행된 정리의 evidence가 인터럽트에 실려 있으면
            # 그것을 쓰고, 없으면(= scope 준비 중 중단이라 workload가 아직 없음) 그 사실만 적는다.
            poc_evidence = getattr(exc, "evidence", None) or {
                "status": "ERROR", "poc_infrastructure": "ABORTED",
                "error": f"운영자 중단 — PoC arm 중 {type(exc).__name__}"}
            poc_evidence.setdefault("workload_id", workload_id)
            poc_evidence.setdefault("scenario_id", arguments.scenario_id)
            if poc_host._exchange is not None:
                # close 뒤에 읽는다 — `cleanup_complete`와 복원 readback이 확정된 상태다.
                poc_evidence["collision_model"] = poc_host._exchange.evidence
            trace.emit("poc_arm_interrupted", error=type(exc).__name__,
                       cleanup_error=collision_cleanup_error,
                       evidence_from_cleanup=getattr(exc, "evidence", None) is not None)
            _write_result(arguments.run_root, "poc-result", poc_evidence)
            final = {
                "artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1",
                "run_id": arguments.run_id, "workload_id": workload_id,
                "lifecycle": arguments.lifecycle, "scenario_id": arguments.scenario_id,
                "certificate_case": arguments.certificate_case,
                "status": "FAIL",
                # 중단 사유를 보존한다 — 정상 종료로 재분류하지 않는다.
                "error": f"운영자 중단 — PoC arm 중 {type(exc).__name__}(scenario 미제출)",
                "cleanup_errors": [collision_cleanup_error] if collision_cleanup_error else [],
                "results": {},
                "command_log": inline_controller.command_log,
            }
            _write_result(arguments.run_root, "final-result", final)
            trace.close()
            print("[mass-roc-controller] 종결 ABORT (poc arm 중단)", flush=True)
            raise

    results: dict = {}
    run_error: str | None = None
    cleanup_errors: list[str] = []
    thread = None
    poc_failure = None
    scenario_success = False
    try:
        if poc_prepared is not None:
            # scenario를 **백그라운드로** 돌리고 그 동안 실 복호 관측을 PoC에 전달한다.
            # 그래야 반응형 action이 scenario terminal **이전에** 제출된다(§8.3 인과성).
            import threading as _threading

            box: dict = {}
            controller._workload_guard = poc_host._exchange.check
            poc_prepared.deliver_scenario_start()

            def _run_scenarios():
                try:
                    box["results"] = run_scenarios(
                        controller, arguments.scenario_id,
                        arguments.certificate_case, trace=trace)
                except Exception as exc:  # noqa: BLE001
                    box["error"] = f"{type(exc).__name__}: {exc}"

            thread = _threading.Thread(target=_run_scenarios, daemon=True)
            thread.start()
            delivered = 0
            not_delivered = 0
            while thread.is_alive():
                for observation in poc_host.new_observations():
                    # 건네준 수가 아니라 **실제로 전달된 수**를 센다. infrastructure 실패가 선
                    # 뒤의 관측은 전달되지 않으므로 따로 적는다 — 그러지 않으면 evidence가
                    # 전달량을 과다 기록한다(실패 뒤 관측까지 전달된 것처럼 보인다).
                    if poc_prepared.deliver_observation(observation):
                        delivered += 1
                    else:
                        not_delivered += 1
                time.sleep(0.1)
            thread.join(timeout=5.0)
            results = box.get("results") or {}
            run_error = box.get("error")
            trace.emit("poc_observations_delivered", count=delivered,
                       not_delivered=not_delivered)
        else:
            results = run_scenarios(controller, arguments.scenario_id,
                                    arguments.certificate_case, trace=trace)
    except Exception as exc:  # noqa: BLE001 - 원래 예외를 evidence에 보존
        poc_failure = exc if poc_prepared is not None else None
        run_error = f"{type(exc).__name__}: {exc}"
        import traceback as _tb
        trace.emit("workload_exception", error=run_error,
                   traceback=_tb.format_exc()[-2000:])
    finally:
        scenario_success = run_error is None and bool(results) and all(_result_ok(item) for item in results.values())
        # Stop the scenario producer before restoring model state. A failing
        # observer must not leave a daemon submitting the next workload's TX.
        controller.cancel_workload()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                cleanup_errors.append("scenario producer still alive; session must inhibit")
                run_error = run_error or cleanup_errors[-1]
                from apps.standalone.poc_run import collision_state_error
                poc_failure = poc_failure or collision_state_error(cleanup_errors[-1])
        import sys as _sys
        interrupted = _sys.exc_info()[1]
        if interrupted is not None and not isinstance(interrupted, Exception) and poc_prepared is not None:
            from apps.standalone import poc_run as _poc_run
            try:
                _poc_run.abort_prepared({"workload": poc_prepared}, cause=interrupted,
                                       out=lambda line: trace.emit("poc_abort", detail=line))
            except BaseException as cleanup_exc:
                interrupted.add_note(f"PoC interrupt cleanup: {cleanup_exc}")
        # Finish the model while endpoint pumps/clocks still exist. Pending RX
        # is drained (or explicitly failed), never replayed after this workload.
        if poc_host is not None:
            try:
                collision_evidence = poc_host.close_collision_exchange()
                if collision_evidence is not None:
                    trace.emit("collision_model_closed", evidence=collision_evidence)
            except Exception as exc:
                cleanup_errors.append(f"collision cleanup: {exc}")
                run_error = run_error or f"collision cleanup: {exc}"
                poc_failure = poc_failure or exc
        if not session_owned:
            for endpoint_id in ENDPOINT_IDS:
                try:
                    invoke_fixture(endpoint_id, "STOP", "stop.json",
                                   f"stop-{endpoint_id}")
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(f"{endpoint_id} STOP 실패: {exc}")
        if interrupted is not None and not isinstance(interrupted, Exception):
            # 운영자 중단의 종결 기록은 **반드시 여기서** 한다.
            #
            # 중단은 `except Exception`에 걸리지 않으므로, 이 `finally`가 끝나는 즉시 try 문
            # **전체 밖으로** 전파된다. 아래 종결 구간(1462행~)은 try 바깥에 있어 도달하지
            # 못한다 — 그것을 try/finally로 감싸는 것으로는 이 경로가 덮이지 않는다.
            # 실측으로 확인했다: 감싸기만 넣은 판(fixed-scenario 회차)은 감싸기가 없는 판
            # (prefix-scenario 회차)과 **동일하게** 산출물 3종 전량 부재·rc -2였고, 두 회차의
            # traceback이 같은 지점(`deliver_scenario_start` → `pump` → `selectors.select`)을
            # 가리켰다. 감싸기는 종결 구간 자체가 다시 중단되는 좁은 경우만 막는다.
            #
            # 정리는 위에서 이미 끝났다(producer 정지 → PoC abort → RX scope 복원 → STOP).
            # 그 뒤 상태로 evidence를 기록한다. `WorkloadRunner.abort`는 idempotent이므로
            # (`_result` 캐시) 아래 종결이 부르는 두 번째 abort는 같은 결과를 돌려줄 뿐
            # 신규 event·action·FINISH를 만들지 않는다.
            run_error = run_error or f"운영자 중단 — {type(interrupted).__name__}(scenario 미완)"
            if poc_prepared is not None:
                # abort 경로를 강제한다. `finish_workload`는 FINISH를 전달하므로 취소에선 금지.
                poc_failure = poc_failure or interrupted
            trace.emit("workload_interrupted", error=type(interrupted).__name__,
                       phase="scenario")
            try:
                _finalize_workload(
                    arguments=arguments, workload_id=workload_id, results=results,
                    run_error=run_error, cleanup_errors=cleanup_errors,
                    poc_prepared=poc_prepared, poc_evidence=poc_evidence,
                    poc_failure=poc_failure, poc_host=poc_host,
                    scenario_success=False, events=events, trace=trace,
                    inline_controller=inline_controller, controller=controller)
            except BaseException as final_exc:  # noqa: BLE001 - 중단 사유를 덮지 않는다
                if final_exc is not interrupted:
                    interrupted.add_note(
                        f"workload finalize during interrupt: "
                        f"{type(final_exc).__name__}: {final_exc}")
                try:
                    trace.close()
                except BaseException:  # noqa: BLE001 - 계측 실패가 중단을 바꾸지 않는다
                    pass
            # 중단은 삼키지 않는다 — 이 finally가 끝나면 원형 그대로 전파된다.

    # 정상 경로의 종결. 위 `finally`가 중단 경로를 이미 기록했으므로 여기 오는 것은
    # 중단이 없었던 실행뿐이다(중단은 finally 종료와 함께 전파돼 이 줄에 닿지 않는다).
    #
    # 실측 배경(diag-20260914-142043 / diag-20260914-150428, 둘 다 dual scope 활성 중 취소):
    #   세션 1 child 로그 108 B·traceback 없음·rc -15 (처분 부재 → SIGTERM 즉사)
    #   세션 2 child 로그 584 B·traceback 있음·rc -2  (처분 발화 → KeyboardInterrupt 미처리 전파;
    #           Pi Python 3.12.3 실측: 미처리 KeyboardInterrupt는 자기 SIGINT 재발생으로 rc -2)
    #   두 회차 모두 poc-result.json / final-result.json / workload-trace.jsonl **전량 부재**.
    #   같은 코드의 정상 종료 회차(diag-20260914-091317)에는 넷 다 남는다.
    # 두 회차 모두 workload_abort는 terminate_sent=true·kill_sent=false — 발신자는 coordinator의
    # `_terminate_process_bounded`(SIGTERM) 하나이며 process-group 전파가 아니다.
    #
    # 아래 감싸기는 **종결 구간 자체가 다시 중단되는** 경우만 담당한다(그 경우에도 trace를
    # 닫아 계측을 남긴다). 새 상한·새 정리 로직·신규 action은 만들지 않는다.
    interrupted_exit: BaseException | None = None
    try:
        return _finalize_workload(
            arguments=arguments, workload_id=workload_id, results=results,
            run_error=run_error, cleanup_errors=cleanup_errors,
            poc_prepared=poc_prepared, poc_evidence=poc_evidence,
            poc_failure=poc_failure, poc_host=poc_host,
            scenario_success=scenario_success, events=events, trace=trace,
            inline_controller=inline_controller, controller=controller)
    except BaseException as exc:  # noqa: BLE001 - 중단을 덮지 않는다
        if isinstance(exc, Exception):
            raise
        interrupted_exit = exc
        raise
    finally:
        if interrupted_exit is not None:
            # 종결 구간 자체가 다시 중단됐다 — 그 사실을 남기고 원래 중단을 재전파한다.
            try:
                trace.emit("workload_finalize_interrupted",
                           error=type(interrupted_exit).__name__)
                trace.close()
            except BaseException:  # noqa: BLE001 - 계측 실패가 중단을 바꾸지 않는다
                pass


def _finalize_workload(*, arguments, workload_id, results, run_error, cleanup_errors,
                       poc_prepared, poc_evidence, poc_failure, poc_host,
                       scenario_success, events, trace, inline_controller, controller) -> int:
    """workload 종결 — PoC 종료·evidence 기록·trace flush.

    중단(KeyboardInterrupt/SystemExit) 중에도 호출된다. 여기서 새 PoC action을 만들지 않고,
    이미 제출된 action의 상태를 성공이나 취소로 추정하지 않는다(`abort_prepared`는 cleanup-only).
    실제로 수행되지 않은 복원·결과를 합성하지 않는다 — 각 값은 실행된 경로가 돌려준 것만 쓴다.
    """

    overall = run_error is None and bool(results)         and all(_result_ok(item) for item in results.values())

    # --- Stage 8 PoC 종료: **실제 scenario verdict**를 명시 전달한다(§11.1 독립 판정) --------
    if poc_prepared is not None:
        from apps.standalone import poc_run as _poc_run
        # 판정을 재계산하지 않는다 — 방금 확정한 이 workload의 scenario 결과를 그대로 매핑한다.
        scenario_verdict = _poc_run.scenario_verdict_from_status(
            "PASS" if scenario_success else "FAIL")
        try:
            if poc_failure is not None:
                poc_evidence = _poc_run.abort_prepared(
                    {"workload": poc_prepared}, cause=poc_failure,
                    scenario_verdict=scenario_verdict,
                    out=lambda line: trace.emit("poc_abort", detail=line))
            else:
                poc_evidence = _poc_run.finish_workload(
                    poc_prepared, scenario_verdict=scenario_verdict)
        except BaseException as exc:  # noqa: BLE001 - finish 실패도 cleanup-only로 정리
            prep = {"workload": poc_prepared}
            poc_evidence = _poc_run.abort_prepared(
                prep, cause=exc, out=lambda line: trace.emit("poc_abort", detail=line))
            if not isinstance(exc, Exception):
                raise
        poc_evidence = poc_evidence or {"status": "ERROR", "poc_infrastructure": "ERROR"}
        poc_evidence.setdefault("workload_id", workload_id)
        poc_evidence.setdefault("scenario_id", arguments.scenario_id)
        if poc_host is not None and poc_host._exchange is not None:
            poc_evidence["collision_model"] = poc_host._exchange.evidence
    if poc_evidence is not None:
        _write_result(arguments.run_root, "poc-result", poc_evidence)
        trace.emit("poc_result", infrastructure=poc_evidence.get("poc_infrastructure"),
                   status=poc_evidence.get("status"))

    for scenario_id, payload in results.items():
        _write_result(arguments.run_root, scenario_id, payload)
    final = {
        "artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1",
        "run_id": arguments.run_id,
        "workload_id": workload_id,
        "lifecycle": arguments.lifecycle,
        "scenario_id": arguments.scenario_id,
        "certificate_case": arguments.certificate_case,
        "status": "PASS" if overall else "FAIL",
        "error": run_error,
        "cleanup_errors": cleanup_errors,
        "event_baseline": getattr(events, "baseline", None),
        "results": results,
        "command_log": inline_controller.command_log,
    }
    _write_result(arguments.run_root, "final-result", final)
    # workload 경계에서 buffer를 비우고, application 객체 폐기를 남긴다.
    events.poll()
    trace.emit("workload_end", status=final["status"],
               error=run_error, cleanup_errors=cleanup_errors,
               event_cursor_end={ep: len(records)
                                 for ep, records in events.records.items()},
               application_objects_released=True)
    trace.close()
    del controller
    print(f"[mass-roc-controller] 종결 {final['status']}", flush=True)
    return 0 if overall else 1


def _run_session_lifecycle_step(lifecycle, invoke_fixture, gate_dir, trace):
    """persistent session의 endpoint START/STOP 한 단계만 수행한다.

    session 모델에서 endpoint lifecycle은 session이 소유하지만, TEST adapter
    socket 연결은 이 controller 프로세스에만 존재한다. coordinator는 barrier를
    소유하고, 실제 service START/STOP 호출은 이 대행 프로세스가 수행한다.
    """
    import threading

    operation = "START" if lifecycle == "session-start" else "STOP"
    trace.emit("session_lifecycle_step_begin", operation=operation)

    if operation == "START":
        # admission이 열리기 전 START는 거절된다 — gate를 먼저 기다린다.
        gate = os.path.join(gate_dir, "admission_open")
        deadline = time.monotonic() + 600.0
        while not os.path.exists(gate):
            if time.monotonic() > deadline:
                print("[mass-roc-controller] admission_open gate 600s 초과",
                      flush=True)
                trace.emit("session_lifecycle_step_end", operation=operation,
                           ok=False, reason="admission_gate_timeout")
                return 1
            time.sleep(0.2)

    outcomes: dict = {}

    def _call(endpoint_id):
        relpath = (f"start-{endpoint_id.lower()}.json" if operation == "START"
                   else "stop.json")
        try:
            ok, body = invoke_fixture(
                endpoint_id, operation, relpath,
                f"{operation.lower()}-{endpoint_id}")
        except Exception as exc:  # noqa: BLE001 - 세 endpoint를 독립 판정
            outcomes[endpoint_id] = (False, {"error": f"{type(exc).__name__}: {exc}"})
            return
        outcomes[endpoint_id] = (ok, body)

    threads = [threading.Thread(target=_call, args=(endpoint,))
               for endpoint in ENDPOINT_IDS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300.0)

    ok = all(outcomes.get(endpoint, (False, None))[0] for endpoint in ENDPOINT_IDS)
    trace.emit("session_lifecycle_step_end", operation=operation, ok=ok,
               outcomes={endpoint: bool(value[0])
                         for endpoint, value in outcomes.items()})
    if not ok:
        print(f"[mass-roc-controller] session {operation} 실패: {outcomes}",
              flush=True)
        return 1
    print(f"[mass-roc-controller] session {operation} 3/3 OK", flush=True)
    return 0


def _own_endpoint_lifecycle_start(inline_controller, invoke_fixture, gate_dir):
    """`owned` lifecycle: admission gate 대기 후 세 endpoint START (기존 동작)."""
    import threading

    gate = os.path.join(gate_dir, "admission_open")
    deadline = time.monotonic() + 4_200.0
    while not os.path.exists(gate):
        if time.monotonic() > deadline:
            print("[mass-roc-controller] admission_open gate 4200s 초과", flush=True)
            return 1
        time.sleep(0.2)


    start_results: dict = {}

    def _start(endpoint_id):
        ok, body = invoke_fixture(
            endpoint_id, "START", f"start-{endpoint_id.lower()}.json",
            f"start-{endpoint_id}",
        )
        start_results[endpoint_id] = (ok, body)

    threads = [threading.Thread(target=_start, args=(ep,)) for ep in ENDPOINT_IDS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300.0)
    if not all(start_results.get(ep, (False, None))[0] for ep in ENDPOINT_IDS):
        print(f"[mass-roc-controller] START 실패: {start_results}", flush=True)
        return 1
    open(os.path.join(gate_dir, "started_ok"), "w").close()
    print("[mass-roc-controller] start-all PASS", flush=True)
    return 0

def _result_ok(payload: dict) -> bool:
    if "result" in payload:  # 006 형식 (precondition + result)
        pre = payload.get("precondition")
        pre_ok = pre is None or pre.get("status") == "PASS"
        return pre_ok and payload["result"].get("status") == "PASS"
    return payload.get("status") == "PASS"


def run_scenarios(controller: MassRocScenarioController, scenario_id: str,
                  certificate_case: str, trace=None) -> dict:
    """scenario_id/certificate_case에 맞춰 시나리오를 실행하고 결과 dict를 모은다.

    `all`에서는 SC-VDE-005를 한 번만 실행하고 **같은 agent 쌍·그 결과**를
    SC-VDE-006의 사전 조건으로 넘긴다 — 006이 사전 조건 005를 다시 돌려
    RF command/ACK 왕복 한 세트를 낭비하지 않게 한다. 단독 SC-VDE-006은
    여전히 자기 사전 조건 005를 스스로 실행한다.
    """
    if trace is None:
        from apps.stage7_trace import NullTrace
        trace = NullTrace()
    results: dict = {}
    trace.emit("scenarios_begin", scenario_id=scenario_id,
               certificate_case=certificate_case)
    if scenario_id == "all":
        roc, mass = _default_agents()
        pre = controller.run_sc_vde_005(agents=(roc, mass))
        results["SC-VDE-005"] = pre.to_dict()
        results["SC-VDE-006"] = controller.run_sc_vde_006(
            standalone=False, agents=(roc, mass), precondition=pre)
    elif scenario_id == "SC-VDE-005":
        results["SC-VDE-005"] = controller.run_sc_vde_005().to_dict()
    elif scenario_id == "SC-VDE-006":
        results["SC-VDE-006"] = controller.run_sc_vde_006(standalone=True)
    if scenario_id in ("SC-VDE-001", "all"):
        cases = ("normal", "forged") if certificate_case == "both" \
            else (certificate_case,)
        for case in cases:
            results[f"SC-VDE-001[{case}]"] = \
                controller.run_sc_vde_001(certificate_case=case).to_dict()
    for name, payload in results.items():
        body = payload.get("result", payload)
        trace.emit("scenario_result", scenario=name,
                   status=body.get("status"), reason=body.get("reason"),
                   counters=body.get("counters"),
                   transfers=len(body.get("transfers") or []),
                   steps=[(s["name"], s["status"])
                          for s in (body.get("steps") or [])])
    trace.emit("scenarios_end", results=sorted(results))
    return results


if __name__ == "__main__":
    raise SystemExit(main())
