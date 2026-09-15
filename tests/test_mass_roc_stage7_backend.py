"""MASS/ROC stage7 backend 계약 시험 — 가짜 loopback Service, 장비 없음.

실행: VDETER\\.venv\\Scripts\\python.exe -m pytest tests/test_mass_roc_stage7_backend.py -q

실제 Stage 7 경로의 소켓·RF만 가짜로 바꾸고 나머지는 전부 실물을 쓴다:

    MassRocScenarioController (실제 업무 흐름)
    → Stage7ScenarioController.invoke_service_inline (실제 §16.2 명령)
    → Stage7ServiceApiTestAdapter (실제 TEST adapter)
    → LoopbackService (가짜 — submit이 상대 endpoint의 event JSONL에
      launcher의 service_event_record 형식 그대로 DATA_RECEIVED를 쓴다)
    → EventLog tail (실제 controller가 실장비에서 읽는 것과 같은 코드)
    → 실제 RocAgent / MassAgent / cert_flow

검증 범위 (Phase B 테스트 요구 7·8·9·10·12·13·14·15·16·18):
inline SUBMIT·transfer ID 전달·STATUS·DATA_RECEIVED 직렬화·실제 행위자
왕복·006 사전 조건·인증서 정상/위조·MASS-B 오수신·private key 부재.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.security import certificate as cert

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    ReceivedApplicationData,
    TransferAccepted,
    TransferState,
    TransferStatusSnapshot,
)
from vdeter.common.events import EventSeverity, ServiceEvent, ServiceEventType
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.common.results import Ok
from vdeter.common.time import ClockDomain, TimePoint

from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
from vdeter.stage7.testing.controller import Stage7ScenarioController

from apps.stage7_endpoint_launcher import service_event_record
from apps.stage7_mass_roc_controller import (
    ENDPOINT_IDS,
    EventLog,
    MassRocScenarioController,
    STATION_BY_ENDPOINT,
    run_scenarios,
    _result_ok,
)

_PEER = (777, 1000)
_TP = TimePoint(1, 0, ClockDomain.UTC, "test-utc")
_PLAN_SHA = "f" * 64

ENDPOINT_BY_STATION = {v: k for k, v in STATION_BY_ENDPOINT.items()}


class LoopbackService:
    """submit 즉시 수신측 event JSONL에 배달하는 가짜 public Service.

    배달 record는 launcher의 `service_event_record`를 그대로 사용한다 —
    controller가 실장비에서 읽을 형식과 이 시험이 읽는 형식이 같은 코드로
    묶인다.
    """

    def __init__(self, endpoint_id: str, run_root: str,
                 extra_deliver_to: tuple = (),
                 destination_override: int | None = None) -> None:
        self.endpoint_id = endpoint_id
        self.station = STATION_BY_ENDPOINT[endpoint_id]
        self._events_dir = os.path.join(run_root, "evidence", "service_events")
        os.makedirs(self._events_dir, exist_ok=True)
        self._extra_deliver_to = tuple(extra_deliver_to)
        #: 시험용 — DATA_RECEIVED의 original_destination을 다른 station으로
        #: 오염시킨다 (수신 metadata 검증이 실제로 판정하는지 확인).
        self._destination_override = destination_override
        self.submits = []

    def _deliver(self, receiver_ep: str, request) -> None:
        destination = request.destination
        if self._destination_override is not None:
            destination = Addressed(StationId(self._destination_override))
        received = ReceivedApplicationData(
            original_source=StationId(self.station),
            original_destination=destination,
            payload_format=request.payload_format,
            payload=request.payload,
            received_at=_TP,
        )
        event = ServiceEvent(
            event_id=str(uuid.uuid4()),
            event_type=ServiceEventType.DATA_RECEIVED,
            occurred_at=_TP,
            severity=EventSeverity.INFO,
            received_data=received,
        )
        record = service_event_record(event, include_payload=False)
        path = os.path.join(self._events_dir, f"{receiver_ep}.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True))
            handle.write("\n")

    def submit(self, request):
        self.submits.append(request)
        destination_station = request.destination.station_id.value
        receiver_ep = ENDPOINT_BY_STATION[destination_station]
        self._deliver(receiver_ep, request)
        for extra_ep in self._extra_deliver_to:
            self._deliver(extra_ep, request)
        return Ok(TransferAccepted(
            transfer_id=TransferId(str(uuid.uuid4())),
            state=TransferState.QUEUED, accepted_at=_TP,
        ))

    def status(self, request):
        return Ok(TransferStatusSnapshot(
            transfer_id=request.transfer_id, state=TransferState.COMPLETED,
            updated_at=_TP,
        ))


class _Channel:
    def __init__(self, adapter):
        self._adapter = adapter

    def request(self, message):
        return self._adapter.handle_command(message, _PEER)


def build_harness(run_root: str, *, wrong_destination_from: str | None = None,
                  destination_override_from: str | None = None,
                  destination_override: int | None = None):
    """(services, controller) — 실제 adapter/controller/EventLog 위의 시험대."""
    services = {}
    channels = {}
    for endpoint_id in ENDPOINT_IDS:
        extra = ("MASS-B",) if endpoint_id == wrong_destination_from else ()
        override = (destination_override
                    if endpoint_id == destination_override_from else None)
        service = LoopbackService(endpoint_id, run_root, extra_deliver_to=extra,
                                  destination_override=override)
        services[endpoint_id] = service
        channels[endpoint_id] = _Channel(Stage7ServiceApiTestAdapter(
            endpoint_id=endpoint_id, service=service,
            resolved_test_plan_sha256=_PLAN_SHA,
            authorized_controller=_PEER,
        ))
    inline = Stage7ScenarioController(
        run_id="stage7-backend-test",
        resolved_test_plan_sha256=_PLAN_SHA,
        adapter_channels=channels,
        host_raw_clock=lambda: 1_000_000_000,
    )
    controller = MassRocScenarioController(
        controller=inline, events=EventLog(run_root),
        status_timeout_s=5.0, receive_timeout_s=5.0,
    )
    return services, controller


class TC_EVENT_RECORD(unittest.TestCase):
    """테스트 요구 10 — DATA_RECEIVED record의 public 필드 직렬화."""

    def test_data_received_record_fields(self):
        payload = bytes(range(64))
        received = ReceivedApplicationData(
            original_source=StationId(222_222_222),
            original_destination=Addressed(StationId(111_111_111)),
            payload_format=PayloadFormat("sc_vde.route_control.command"),
            payload=payload,
            received_at=_TP,
        )
        event = ServiceEvent(
            event_id="e1", event_type=ServiceEventType.DATA_RECEIVED,
            occurred_at=_TP, severity=EventSeverity.INFO,
            received_data=received,
        )
        record = service_event_record(event, mono_clock=lambda: 12.345)
        self.assertEqual(record["event_type"], "DATA_RECEIVED")
        self.assertEqual(record["payload_len"], len(payload))
        self.assertEqual(record["payload_sha256"],
                         hashlib.sha256(payload).hexdigest())
        self.assertEqual(base64.b64decode(record["payload_base64"]), payload)
        self.assertEqual(record["payload_format"], "sc_vde.route_control.command")
        # 예전 `received.source` 접근은 항상 None이었다 — original_source 기반 수정.
        self.assertEqual(record["source"], 222_222_222)
        self.assertEqual(record["original_source"], 222_222_222)
        self.assertEqual(record["original_destination"], 111_111_111)
        self.assertEqual(record["received_at_ns"], _TP.total_nanoseconds())
        self.assertEqual(record["t_mono"], 12.345)
        secure_record = service_event_record(event, include_payload=False)
        self.assertNotIn("payload_base64", secure_record)
        self.assertEqual(secure_record["payload_sha256"], hashlib.sha256(payload).hexdigest())

    def test_state_change_record_keeps_existing_shape(self):
        event = ServiceEvent(
            event_id="e2", event_type=ServiceEventType.TRANSFER_STATE_CHANGED,
            occurred_at=_TP, severity=EventSeverity.INFO,
            transfer_id=TransferId(str(uuid.uuid4())),
            transfer_state=TransferState.COMPLETED,
        )
        record = service_event_record(event, mono_clock=lambda: 1.0)
        self.assertEqual(record["event_type"], "TRANSFER_STATE_CHANGED")
        self.assertEqual(record["transfer_state"], "COMPLETED")
        # 수신 payload 필드는 DATA_RECEIVED에만 있다 (기존 RR 판정 형식 보존).
        self.assertNotIn("payload_base64", record)
        self.assertNotIn("payload_sha256", record)


class TC_AGENT_ROUND_TRIP(unittest.TestCase):
    """테스트 요구 7·8·9·12 — 실제 RocAgent→MassAgent→ACK→RocAgent 경로."""

    def test_sc_vde_005_full_path(self):
        with tempfile.TemporaryDirectory() as run_root:
            services, controller = build_harness(run_root)
            result = controller.run_sc_vde_005()
        detail = result.to_dict()
        self.assertEqual(detail["status"], "PASS", detail)
        # 명령·ACK 두 전송 모두 public transfer ID로 상관됐다.
        self.assertEqual(len(detail["transfers"]), 2)
        for transfer in detail["transfers"]:
            self.assertTrue(transfer["transfer_id"])
            self.assertEqual(transfer["final_transfer_state"], "COMPLETED")
            self.assertTrue(transfer["received"]["byte_exact"])
            # 각 DATA_RECEIVED는 format·source·destination·SHA·길이를 전부
            # 대조해 PASS 조건으로 삼는다.
            self.assertTrue(transfer["received"]["valid"],
                            transfer["received"]["checks"])
            self.assertTrue(all(transfer["received"]["checks"].values()))
        # ROC가 명령을, MASS-A가 ACK를 실제로 submit했다.
        self.assertEqual(len(services["ROC"].submits), 1)
        self.assertEqual(len(services["MASS-A"].submits), 1)
        self.assertEqual(detail["counters"]["wrong_destination"], 0)
        self.assertEqual(detail["counters"]["duplicates"], 0)

    def test_sc_vde_006_standalone_runs_005_precondition(self):
        """테스트 요구 13 — 단독 006은 005 사전 조건을 먼저 수행·분리 기록.

        사전 조건은 기록만이 아니라 **상태 연속**이어야 한다: 005가 교환한
        노선이 같은 MassAgent에 남아 있는 채로 006이 시작한다.
        """
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = build_harness(run_root)
            outcome = controller.run_sc_vde_006(standalone=True)
        self.assertIsNotNone(outcome["precondition"])
        self.assertEqual(outcome["precondition"]["scenario_id"], "SC-VDE-005")
        self.assertEqual(outcome["precondition"]["status"], "PASS")
        self.assertEqual(outcome["result"]["status"], "PASS", outcome["result"])
        steps = {s["name"]: s for s in outcome["result"]["steps"]}
        # 상태 연속 — 006 시작 시점의 MASS에 005의 노선이 실제로 있다.
        state_step = steps["006-precondition-state"]
        self.assertEqual(state_step["status"], "PASS", state_step)
        self.assertEqual(state_step["remaining_waypoints"], 3)  # 005의 노선
        self.assertGreater(state_step["route_linkage_id"], 0)
        # 조종 반영·멱등 재수신.
        self.assertEqual(steps["006-control-applied"]["status"], "PASS")
        self.assertEqual(steps["006-idempotent-resend"]["status"], "PASS")
        # fallback은 별도 명령으로 시험하고 실제 정지(SOG 0)까지 판정한다.
        fallback_step = steps["006-fallback-applied"]
        self.assertEqual(fallback_step["status"], "PASS", fallback_step)
        self.assertEqual(fallback_step["fallback"], "Anchoring")
        self.assertEqual(fallback_step["sog"], 0.0)
        self.assertEqual(steps["006-fallback-ack-correlated"]["status"], "PASS")

    def test_sc_vde_006_without_precondition_state_fails(self):
        """사전 조건 상태(교환된 노선)가 없으면 006은 시작 단계에서 FAIL이다."""
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = build_harness(run_root)
            outcome = controller.run_sc_vde_006(standalone=False)
        self.assertIsNone(outcome["precondition"])
        self.assertEqual(outcome["result"]["status"], "FAIL")
        steps = {s["name"]: s for s in outcome["result"]["steps"]}
        self.assertEqual(steps["006-precondition-state"]["status"], "FAIL")

    def test_received_destination_metadata_verified(self):
        """수신 record의 original_destination 오염은 PASS 조건에서 걸린다."""
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = build_harness(
                run_root,
                destination_override_from="ROC",
                destination_override=STATION_BY_ENDPOINT["MASS-B"],
            )
            result = controller.run_sc_vde_005()
        detail = result.to_dict()
        self.assertEqual(detail["status"], "FAIL")
        steps = {s["name"]: s for s in detail["steps"]}
        command_step = steps["005-command-transfer"]
        self.assertEqual(command_step["status"], "FAIL")
        self.assertFalse(
            command_step["received_checks"]["original_destination"])
        # 나머지 대조(원문·SHA·format·source)는 성해서, 실패 원인이 정확히
        # destination임이 evidence에 남는다.
        self.assertTrue(command_step["received_checks"]["byte_exact"])
        self.assertTrue(command_step["received_checks"]["original_source"])

    def test_mass_b_wrong_reception_detected(self):
        """테스트 요구 16 — MASS-B로 새는 배달은 오수신으로 판정된다."""
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = build_harness(
                run_root, wrong_destination_from="ROC")
            result = controller.run_sc_vde_005()
        detail = result.to_dict()
        self.assertEqual(detail["status"], "FAIL")
        self.assertGreater(detail["counters"]["wrong_destination"], 0)
        failed = {s["name"] for s in detail["steps"] if s["status"] == "FAIL"}
        self.assertIn("005-mass-b-clean", failed)


class TC_CERTIFICATE(unittest.TestCase):
    """테스트 요구 14·15·18 — 인증서 정상 저장 / 위조 거부 / private key 부재."""

    def setUp(self):
        if not cert.is_standard_backend():
            self.fail("cryptography가 필요하다 — 회귀 환경에는 설치되어 있어야 "
                      "하며 skip으로 PASS를 대신하지 않는다")

    def _run(self, case: str):
        captured_privates = []
        original = cert.generate_key_pair

        def capturing_generate():
            private, public = original()
            captured_privates.append(private)
            return private, public

        cert.generate_key_pair = capturing_generate
        try:
            with tempfile.TemporaryDirectory() as run_root:
                _services, controller = build_harness(run_root)
                result = controller.run_sc_vde_001(certificate_case=case)
                evidence_text = json.dumps(result.to_dict(), ensure_ascii=False)
                events_dir = os.path.join(run_root, "evidence", "service_events")
                for name in os.listdir(events_dir):
                    with open(os.path.join(events_dir, name), "r",
                              encoding="utf-8") as handle:
                        evidence_text += handle.read()
        finally:
            cert.generate_key_pair = original
        return result.to_dict(), evidence_text, captured_privates

    def test_normal_certificate_verified_and_stored(self):
        detail, _text, _keys = self._run("normal")
        self.assertEqual(detail["status"], "PASS", detail)
        names = {s["name"]: s for s in detail["steps"]}
        self.assertEqual(names["001-verified-stored"]["trust_store_size"], 1)
        self.assertEqual(
            names["001-idempotent-store"]["trust_store_size_after_resend"], 1)
        self.assertEqual(len(detail["transfers"]), 1)
        self.assertEqual(detail["transfers"][0]["final_transfer_state"],
                         "COMPLETED")
        # payload 길이는 고정값 판정이 아니라 실행 기록이다.
        self.assertGreater(detail["transfers"][0]["payload_len"], 0)

    def test_forged_certificate_rejected_not_stored(self):
        detail, _text, _keys = self._run("forged")
        self.assertEqual(detail["status"], "PASS", detail)
        names = {s["name"]: s for s in detail["steps"]}
        self.assertEqual(names["001-forged-rejected"]["reason"], "bad-signature")
        self.assertEqual(names["001-forged-rejected"]["trust_store_size"], 0)
        self.assertEqual(names["001-not-stored"]["trust_store_size"], 0)
        # 링크 전달 자체(COMPLETED)와 보안 판정(거부)은 구분된다.
        self.assertEqual(detail["transfers"][0]["final_transfer_state"],
                         "COMPLETED")

    def test_private_keys_absent_from_evidence(self):
        """테스트 요구 18 — 생성된 모든 private key가 evidence에 없다."""
        from cryptography.hazmat.primitives import serialization

        detail, evidence_text, captured = self._run("normal")
        self.assertEqual(detail["status"], "PASS")
        self.assertGreaterEqual(len(captured), 2)  # CA + vessel
        self.assertNotIn("BEGIN", evidence_text)
        self.assertNotIn("PRIVATE KEY", evidence_text)
        for private in captured:
            der = private.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self.assertNotIn(base64.b64encode(der).decode("ascii"),
                             evidence_text)
            self.assertNotIn(der.hex(), evidence_text)


class TC_RUN_SCENARIOS(unittest.TestCase):
    """run all 조합 — 시나리오 선택·certificate both 분기·전체 판정."""

    def test_run_all_with_both_certificate_cases(self):
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = build_harness(run_root)
            results = run_scenarios(controller, "all", "both")
        self.assertEqual(
            sorted(results),
            ["SC-VDE-001[forged]", "SC-VDE-001[normal]",
             "SC-VDE-005", "SC-VDE-006"])
        for scenario_id, payload in results.items():
            self.assertTrue(_result_ok(payload), (scenario_id, payload))

    def test_run_all_shares_005_precondition_with_006(self):
        """run all은 005를 한 번만 실행하고 그 상태·결과를 006이 재사용한다 —
        사전 조건 005의 중복 재실행(불필요한 RF command/ACK 왕복)이 없다."""
        with tempfile.TemporaryDirectory() as run_root:
            services, controller = build_harness(run_root)
            results = run_scenarios(controller, "all", "both")
        outcome_006 = results["SC-VDE-006"]
        # 006의 사전 조건은 첫 005 실행의 결과를 그대로 재사용했다.
        self.assertTrue(outcome_006["precondition_reused"])
        self.assertEqual(outcome_006["precondition"], results["SC-VDE-005"])
        # 상태 연속 — 재사용인데도 005의 노선이 006 시작 상태에 실재한다.
        steps = {s["name"]: s for s in outcome_006["result"]["steps"]}
        self.assertEqual(steps["006-precondition-state"]["status"], "PASS")
        self.assertEqual(steps["006-precondition-state"]["remaining_waypoints"], 3)
        # 전송 횟수 정산 = 중복 왕복 부재의 직접 증거:
        # ROC 6건(005 명령 1 + 006 조종·재전송·fallback 3 + 001 정상·위조 2),
        # MASS-A 3건(005 ACK + 006 조종 ACK + 006 fallback ACK).
        # 사전 조건 005를 재실행했다면 각각 7건/4건이 된다.
        self.assertEqual(len(services["ROC"].submits), 6)
        self.assertEqual(len(services["MASS-A"].submits), 3)
        self.assertEqual(len(services["MASS-B"].submits), 0)

    def test_standalone_006_still_runs_own_precondition(self):
        """단독 006은 여전히 자기 사전 조건 005를 실행한다 (재사용 아님)."""
        with tempfile.TemporaryDirectory() as run_root:
            services, controller = build_harness(run_root)
            outcome = controller.run_sc_vde_006(standalone=True)
        self.assertFalse(outcome["precondition_reused"])
        self.assertEqual(outcome["precondition"]["status"], "PASS")
        # 005 명령 1 + 006 조종·재전송·fallback 3 = ROC 4건.
        self.assertEqual(len(services["ROC"].submits), 4)


if __name__ == "__main__":
    unittest.main()
