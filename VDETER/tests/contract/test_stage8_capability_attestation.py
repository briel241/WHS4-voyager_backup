"""Stage 8 설치 capability 증언 계약 시험 (POC-D22, POC-API-D13, S8-POC-REQ-28, WSM-REQ-12).

세 가지를 **분리**해 검증한다.

    A. 설치 capability — 설치 증언만이 근거다(메서드 존재·runtime 값 불가).
    B. runtime readiness — 별도 생산·검증·철회 경로이며 A를 대신하지 않는다.
    C. RF 실행 권한/자격 — capability 광고가 이 gate를 우회하지 않는다.

플랫폼 무관(장치·소켓 불필요).
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from apps.standalone import capability_attestation as CA
from apps.standalone.state import StatePaths
from vdeter.poc.execution.selection import unavailable_required_capabilities
from vdeter.poc.api import protocol
from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.negotiation import (
    HelloRequest,
    ServerCapabilityConfig,
    negotiate,
)
from vdeter.poc.shared_medium import tuning

CAP = protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL


def _installation(install="i", rf="r", operation="o", tooling="t") -> dict:
    return {"installation_sha256": install * 64,
            "domains": {"rf": {"sha256": rf * 64},
                        "operation": {"sha256": operation * 64},
                        "tooling": {"sha256": tooling * 64}}}


def _items(result="PASS") -> dict:
    return {name: {"result": result, "evidence_sha256": "e" * 64, "note": "fixture"}
            for name in CA.REQUIRED_VERIFICATION[CAP]}


def _attested(paths, installation, *, items=None) -> dict:
    record = CA.build_attestation(
        installation, capability=CAP, items=items or _items(),
        producer="test-fixture", method="rf-off-verification-fixture")
    CA.write_attestation(paths, record)
    return record


class _Descriptor:
    """PoC manifest descriptor 대역 — required capability만 쓰는 최소 형태."""

    def __init__(self, required=(), requested=()):
        self.required_capabilities = tuple(required)
        self.requested_capabilities = tuple(requested or required)


class AttestationIsTheOnlyBasisTest(unittest.TestCase):
    """A: 설치 증언만이 근거다."""

    def setUp(self):
        self.paths = StatePaths(tempfile.mkdtemp())
        self.installation = _installation()

    def test_absent_attestation_is_unsupported(self):
        supported, reasons = CA.support(self.paths, self.installation, capability=CAP)
        self.assertFalse(supported)
        self.assertTrue(reasons)

    def test_valid_attestation_is_supported(self):
        _attested(self.paths, self.installation)
        supported, reasons = CA.support(self.paths, self.installation, capability=CAP)
        self.assertTrue(supported, reasons)
        self.assertEqual(reasons, [])

    def test_method_presence_is_not_a_basis(self):
        """worker에 메서드가 있어도(구 `dual_supported`) 증언이 없으면 미지원이다."""

        class MethodsPresentWorker:
            def read_tuning(self): ...
            def apply_manual_tuning(self, *a): ...
            def restore_tuning(self, *a): ...

        present = all(callable(getattr(MethodsPresentWorker(), n, None))
                      for n in ("read_tuning", "apply_manual_tuning", "restore_tuning"))
        self.assertTrue(present)                      # 전제: 메서드는 존재한다
        supported, _ = CA.support(self.paths, self.installation, capability=CAP)
        self.assertFalse(supported)                   # 그래도 미지원

    def test_runtime_readiness_true_is_not_a_basis(self):
        """B(dual_ready)=True여도 설치 증언이 없으면 A는 거짓이다."""
        dual_ready = True
        supported, _ = CA.support(self.paths, self.installation, capability=CAP)
        self.assertTrue(dual_ready)
        self.assertFalse(supported)

    def test_poc_self_declaration_cannot_promote(self):
        """PoC manifest가 required로 선언해도 설치가 미지원이면 승격되지 않는다."""
        server = ServerCapabilityConfig()             # 증언 없음 → 미광고
        descriptor = _Descriptor(required=(CAP,), requested=(CAP,))
        self.assertEqual(unavailable_required_capabilities(descriptor, server), [CAP])


class StaleAndTamperedTest(unittest.TestCase):
    """누락·불일치·변조·stale·미검증은 전부 fail-closed."""

    def setUp(self):
        self.paths = StatePaths(tempfile.mkdtemp())
        self.installation = _installation()
        self.record = _attested(self.paths, self.installation)

    def _unsupported(self, installation=None):
        supported, reasons = CA.support(
            self.paths, installation or self.installation, capability=CAP)
        self.assertFalse(supported)
        self.assertTrue(reasons)
        return reasons

    def test_stale_installation_digest(self):
        self._unsupported(_installation(install="x"))

    def test_stale_rf_digest(self):
        self._unsupported(_installation(rf="z"))

    def test_stale_operation_digest(self):
        self._unsupported(_installation(operation="q"))

    def test_other_installation_evidence_cannot_promote(self):
        """다른 설치의 증언을 가져와도 현재 설치에서는 승격되지 않는다."""
        other = _installation(install="a", rf="b", operation="c")
        CA.write_attestation(self.paths, CA.build_attestation(
            other, capability=CAP, items=_items(), producer="other-host", method="m"))
        self._unsupported()

    def test_item_not_pass(self):
        bad = copy.deepcopy(self.record)
        bad["items"][CA.REQUIRED_VERIFICATION[CAP][0]]["result"] = "FAIL"
        CA.write_attestation(self.paths, bad)
        self._unsupported()

    def test_required_item_missing(self):
        bad = copy.deepcopy(self.record)
        del bad["items"][CA.REQUIRED_VERIFICATION[CAP][1]]
        CA.write_attestation(self.paths, bad)
        self._unsupported()

    def test_contract_version_mismatch(self):
        bad = copy.deepcopy(self.record)
        bad["contract_version"] = CA.CONTRACT_VERSION + 98
        CA.write_attestation(self.paths, bad)
        self._unsupported()

    def test_attestation_for_another_capability(self):
        bad = copy.deepcopy(self.record)
        bad["capability"] = "observe_packet"
        CA.write_attestation(self.paths, bad)
        self._unsupported()

    def test_corrupt_file(self):
        CA.attestation_path(self.paths).write_text("{not json", encoding="utf-8")
        self._unsupported()

    def test_wrong_artifact_type_is_rejected(self):
        bad = copy.deepcopy(self.record)
        bad["artifact_type"] = "SOMETHING_ELSE"
        CA.attestation_path(self.paths).write_text(json.dumps(bad), encoding="utf-8")
        self._unsupported()

    def test_build_refuses_incomplete_items(self):
        with self.assertRaises(CA.AttestationError):
            CA.build_attestation(self.installation, capability=CAP, items={},
                                 producer="p", method="m")

    def test_build_refuses_unknown_capability(self):
        with self.assertRaises(CA.AttestationError):
            CA.build_attestation(self.installation, capability="bogus",
                                 items=_items(), producer="p", method="m")


class AdvertisementAgreesEndToEndTest(unittest.TestCase):
    """설치 근거 → 사전검사 → WELCOME 협상 값이 일치한다."""

    def setUp(self):
        self.paths = StatePaths(tempfile.mkdtemp())
        self.installation = _installation()

    def _server(self):
        supported, _ = CA.support(self.paths, self.installation, capability=CAP)
        return ServerCapabilityConfig(wsm_g1_production_installed=bool(supported))

    @staticmethod
    def _hello():
        return HelloRequest(
            protocol_major=protocol.PROTOCOL_MAJOR,
            minor_min=protocol.SERVER_MIN_MINOR, minor_max=protocol.SERVER_MAX_MINOR,
            requested_capabilities=frozenset({"observe_packet", CAP}),
            requested_triggers=frozenset({"SCENARIO_STARTED"}))

    @staticmethod
    def _manifest():
        from vdeter.poc.testing.fakes import build_manifest
        return build_manifest(requested_capabilities=("observe_packet", CAP),
                              required_capabilities=(CAP,))

    def test_unsupported_refused_before_import_or_spawn(self):
        server = self._server()
        descriptor = _Descriptor(required=(CAP,), requested=(CAP,))
        # 사전검사(import·spawn 이전 단계)에서 이미 거부된다.
        self.assertEqual(unavailable_required_capabilities(descriptor, server), [CAP])
        with self.assertRaises(PocError) as ctx:
            negotiate(server, self._manifest(), self._hello(), connection_epoch=1)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)

    def test_supported_flows_through_precheck_and_welcome(self):
        _attested(self.paths, self.installation)
        server = self._server()
        descriptor = _Descriptor(required=(CAP,), requested=(CAP,))
        self.assertEqual(unavailable_required_capabilities(descriptor, server), [])
        negotiated = negotiate(server, self._manifest(), self._hello(), connection_epoch=1)
        self.assertIn(CAP, negotiated.capabilities)
        # 사전검사와 WELCOME이 같은 근거를 쓴다.
        self.assertIn(CAP, server.advertised_capabilities())


class CapabilityDoesNotGrantReadinessOrRfTest(unittest.TestCase):
    """A가 B나 C를 자동 승인하지 않는다."""

    def setUp(self):
        self.paths = StatePaths(tempfile.mkdtemp())
        self.installation = _installation()
        _attested(self.paths, self.installation)

    def test_installed_but_not_ready_is_refused_by_the_readiness_gate(self):
        """설치는 지원(A=true)이어도 runtime readiness(B)가 없으면 해당 동작이 거부된다.

        A는 증언에서, B는 provider에 주입된 값에서 온다. 여기서는 A=true인 상태로
        B를 만족하지 못한 provider가 그대로 fail-closed임을 provider 계약으로 확인한다.
        """
        supported, _ = CA.support(self.paths, self.installation, capability=CAP)
        self.assertTrue(supported)                     # A = true

        from vdeter.poc.shared_medium import testing as TD
        from vdeter.poc.shared_medium.provider import SharedMediumActionProvider

        not_ready = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=TD.CountingWaveformFactory(),
            dual_channel_installed=False,              # B 미충족 상태의 provider
            allow_rf_off_defaults=True)
        self.assertNotIn(CAP, not_ready.advertised_capabilities())
        with self.assertRaises(PocError) as ctx:
            not_ready.prepare_dual_channel(required=True)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)

    def test_attestation_carries_no_rf_or_qualification_authority(self):
        record = CA.load_attestation(self.paths)
        described = CA.describe(self.paths, self.installation, capability=CAP)
        # 실신호·자격 항목은 소프트웨어 증언으로 대체되지 않고 별도로 남는다.
        self.assertTrue(record["hardware_only_items"])
        self.assertTrue(described["hardware_only_items"])
        for item in record["hardware_only_items"]:
            self.assertNotIn(item, record["items"])


class CallerProducerConflictTest(unittest.TestCase):
    """caller 설정과 session(설치 증언) 결과가 다르면 조용히 덮어쓰지 않고 거부한다."""

    class _Session:
        """`prepare_poc_scope`/`poc_server_config`를 가진 최소 session 대역."""

        def __init__(self, advertise: bool):
            self._advertise = advertise
            self.prepared = False

        def prepare_poc_scope(self, descriptor):
            self.prepared = True

        def poc_server_config(self):
            return ServerCapabilityConfig(wsm_g1_production_installed=self._advertise)

    class _Snapshot:
        descriptor = _Descriptor(required=(), requested=())

    def test_conflicting_caller_config_is_refused(self):
        from apps.standalone import poc_run

        session = self._Session(advertise=False)       # 설치 증언: 미지원
        caller = ServerCapabilityConfig(wsm_g1_production_installed=True)  # 근거 없는 caller 주장
        with self.assertRaises(poc_run.PocRunError) as ctx:
            poc_run.prepare_workload(
                session, self._Snapshot(), scenario_id="SC-VDE-006",
                run_id="conflict", workload_id="w1", server_config=caller)
        self.assertIn("server capability conflict", str(ctx.exception))
        self.assertTrue(session.prepared)              # scope는 준비됐고, 거부는 그 뒤 계약 검사다

    def test_agreeing_caller_config_is_not_refused_for_conflict(self):
        """같은 값이면 충돌로 거부하지 않는다(이후 단계 실패는 이 시험의 대상이 아니다)."""
        from apps.standalone import poc_run

        session = self._Session(advertise=False)
        caller = ServerCapabilityConfig(wsm_g1_production_installed=False)
        try:
            poc_run.prepare_workload(
                session, self._Snapshot(), scenario_id="SC-VDE-006",
                run_id="agree", workload_id="w1", server_config=caller)
        except poc_run.PocRunError as exc:
            self.assertNotIn("server capability conflict", str(exc))
        except Exception:
            pass                                        # device 경계 미제공 등 이후 단계 실패는 무관


if __name__ == "__main__":
    unittest.main()
