"""Stage 4 fault-injection matrix — 모든 WorkloadConnection seam × 고장 종류.

구현자의 정상 동작 가정을 따르지 않고 **외부 seam을 적대적으로** 고장 낸다. 각 조합에서
다음 불변식을 강제한다(§11.2, §13, `S8-POC-REQ-19/20/27`).

(a) 일반 Exception은 public 경계(run/finish) 밖으로 raw 누출하지 않고 fail-closed 결과로 변환.
(b) KeyboardInterrupt/SystemExit/GeneratorExit은 원형 그대로 재전달.
(c) 어떤 경로에서도 MASS-B lease는 NORMAL로 반환(잔존 0).
(d) connection.close()는 정확히 1회.
(e) 결과는 None으로 남지 않는다(재진입 포함).
(f) 고장·중단을 COMPLETED로 기록하지 않는다.

예외: evidence 수집만 실패한 경우(일반 Exception)는 workload 자체가 성공한 것이므로
COMPLETED + evidence 저하 표식이 정상이다. 반면 evidence 중 **인터럽트**는 중단이므로
ABORTED로 기록하고 재전달한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from vdeter.poc.execution.lease import MassBLease
from vdeter.poc.execution.runner import WorkloadRunner, WorkloadSpec, scenario_started
from vdeter.poc.execution.snapshot import Snapshot
from vdeter.poc.execution.verdict import PocInfrastructure, ScenarioVerdict
from vdeter.poc.testing.execution_fakes import FakeTxExecutor
from vdeter.poc.testing.fakes import build_manifest

_POSIX = hasattr(os, "getuid") and hasattr(os, "mkfifo") and sys.platform != "win32"

MANIFEST = build_manifest()
SNAPSHOT = Snapshot("/s", "/s/p.py", "/s/p.poc.toml", "ab" * 32, "cd" * 32, "ef" * 32,
                    MANIFEST, "py", "3.12", 1, 2)

#: runner가 구동하는 모든 외부 seam(속성 포함).
SEAMS = (
    "start", "armed", "running", "emit_event", "pump", "poll_new_results",
    "submitted_action", "provide_terminal", "client_idle", "finish", "close",
    "infrastructure_error", "armed_reached", "evidence",
)
#: 일반 실행 결함 1종 + BaseException 3종.
FAULTS = (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)


class _FaultConn:
    """지정 seam에서 지정 예외를 던지는 WorkloadConnection."""

    def __init__(self, seam, exc_type):
        self.seam = seam
        self.exc_type = exc_type
        self.fired = False
        self.close_calls = 0
        self._armed = False
        self._served = False

    def _f(self, name):
        if self.seam == name:
            self.fired = True
            raise self.exc_type()

    def start(self):
        self._f("start")
        self._armed = True

    @property
    def armed(self):
        self._f("armed")
        return self._armed

    @property
    def running(self):
        self._f("running")
        return self._armed

    def emit_event(self, event_type, body):
        self._f("emit_event")

    def emit_observation(self, record):
        self._f("emit_event")

    def pump(self):
        self._f("pump")

    def poll_new_results(self):
        self._f("poll_new_results")
        if not self._served:
            self._served = True
            return [{"request_id": "r1", "admission": "accepted", "physical_channel": "lower",
                     "broadcast_parent_id": "p", "transfer_id": "t", "error_code": None}]
        return []

    def submitted_action(self, request_id):
        self._f("submitted_action")
        return {"action_kind": "REPLAY_PACKET", "target": "ROC", "schedule": {}, "payload": {}}

    def provide_terminal(self, request_id, *, action_execution, target_effect):
        self._f("provide_terminal")

    def client_idle(self):
        self._f("client_idle")
        return True

    def finish(self):
        self._f("finish")

    def close(self):
        self.close_calls += 1
        self._f("close")
        return {"engine": {"event_queue": 0, "replay_ring": 0, "inflight_actions": 0}}

    @property
    def infrastructure_error(self):
        self._f("infrastructure_error")
        return None

    def armed_reached(self):
        self._f("armed_reached")
        return self._armed

    def evidence(self):
        self._f("evidence")
        return {}


class ConnectionFaultMatrixTest(unittest.TestCase):
    """14 seam × 4 고장 = 56 조합."""

    def test_fault_matrix(self):
        for seam in SEAMS:
            for fault in FAULTS:
                with self.subTest(seam=seam, fault=fault.__name__):
                    self._check(seam, fault)

    def _check(self, seam, fault):
        conn = _FaultConn(seam, fault)
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=SNAPSHOT,
                                tx_executor=FakeTxExecutor())
        spec = WorkloadSpec(steps=(scenario_started(),), scenario_verdict=ScenarioVerdict.PASS)
        raised = None
        try:
            runner.run(spec)
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        # 재진입: 저장된 결과를 돌려줘야 하며 None이면 안 된다.
        result = runner._finalize(ScenarioVerdict.PASS, raised=None)

        if not conn.fired:
            # 성공 경로에서 호출되지 않는 seam(오류 분류 전용). 불변식 적용 대상 아님.
            self.assertIsNotNone(result)
            return

        is_base = not issubclass(fault, Exception)
        if is_base:
            # (b) 인터럽트는 원형 재전달
            self.assertIsInstance(raised, fault)
        else:
            # (a) 일반 Exception은 raw 누출 금지
            self.assertIsNone(raised, f"{seam}: raw 예외가 누출됨 {raised!r}")

        # (c) lease 반환, (d) close 1회, (e) 결과 non-None
        lease.assert_returned()
        self.assertEqual(lease.role.value, "NORMAL")
        self.assertEqual(conn.close_calls, 1, f"{seam}: close {conn.close_calls}회")
        self.assertIsNotNone(result, f"{seam}: 결과가 None")

        # (f) 고장·중단을 COMPLETED로 기록하지 않는다.
        if result.poc_infrastructure == PocInfrastructure.COMPLETED:
            degraded = result.evidence.get("connection") == {"error": "evidence unavailable"}
            self.assertTrue(
                seam == "evidence" and not is_base and degraded,
                f"{seam}/{fault.__name__}: 고장인데 COMPLETED로 기록됨")
        else:
            self.assertIn(result.poc_infrastructure,
                          (PocInfrastructure.ERROR, PocInfrastructure.ABORTED))
            # scenario verdict는 infra 고장과 독립(§11.1).
            self.assertEqual(result.scenario_verdict, ScenarioVerdict.PASS)

    def test_query_seam_interrupt_does_not_abort_cleanup(self):
        """infrastructure_error 질의의 인터럽트가 cleanup을 중단시키지 않는다(회귀 방지).

        수정 전: `_infra_error()`가 Exception만 흡수해 인터럽트가 `_build_result` 안에서 다시
        올라왔고, close·lease 반환·결과 저장이 모두 건너뛰어졌다(lease 누수).
        """
        for fault in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(fault=fault.__name__):
                conn = _FaultConn("infrastructure_error", fault)
                lease = MassBLease()
                runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                        scenario_id="SC-VDE-006", snapshot=SNAPSHOT,
                                        tx_executor=FakeTxExecutor())
                with self.assertRaises(fault):
                    runner.run(WorkloadSpec(steps=(scenario_started(),),
                                            scenario_verdict=ScenarioVerdict.PASS))
                lease.assert_returned()                      # lease 누수 없음
                self.assertEqual(conn.close_calls, 1)        # cleanup 수행됨
                result = runner._finalize(ScenarioVerdict.PASS, raised=None)
                self.assertIsNotNone(result)                 # 결과 보존
                self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)

    def test_evidence_interrupt_not_recorded_as_completed(self):
        """evidence 조립 중 인터럽트는 COMPLETED가 아니라 ABORTED로 기록된다(회귀 방지)."""
        for fault in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(fault=fault.__name__):
                conn = _FaultConn("evidence", fault)
                lease = MassBLease()
                runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                        scenario_id="SC-VDE-006", snapshot=SNAPSHOT,
                                        tx_executor=FakeTxExecutor())
                with self.assertRaises(fault):
                    runner.run(WorkloadSpec(steps=(scenario_started(),),
                                            scenario_verdict=ScenarioVerdict.PASS))
                result = runner._finalize(ScenarioVerdict.PASS, raised=None)
                self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
                lease.assert_returned()

    def test_evidence_exception_keeps_result_with_degraded_marker(self):
        """evidence 수집만 실패하면 workload는 COMPLETED이되 저하 표식을 남긴다(결과 유실 없음)."""
        conn = _FaultConn("evidence", RuntimeError)
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=SNAPSHOT,
                                tx_executor=FakeTxExecutor())
        result = runner.run(WorkloadSpec(steps=(scenario_started(),),
                                         scenario_verdict=ScenarioVerdict.PASS))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.evidence["connection"], {"error": "evidence unavailable"})
        lease.assert_returned()


@unittest.skipUnless(_POSIX, "catalog/snapshot open 의미는 POSIX 전용")
class FilesystemSeamFaultMatrixTest(unittest.TestCase):
    """seam 1·2: 특수 파일(FIFO)에 대한 O_RDONLY open이 scan/snapshot을 무기한 block하지
    않는지(fail-closed는 hang이 아니라 거부여야 한다). 회귀 시 alarm으로 시험이 실패한다.

    수정 전: open flags에 O_NONBLOCK이 없어 FIFO O_RDONLY open이 writer를 기다리며 block,
    아래의 S_ISREG 거부가 도달 불가였다.
    """

    def _with_hang_guard(self, fn, seconds=6):
        import signal

        def _alarm(signum, frame):
            raise TimeoutError("open이 special file에서 block됨(fail-closed 위반)")

        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(seconds)
        try:
            return fn()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def _catalog_dir(self):
        d = tempfile.mkdtemp(prefix="fault-cat-")
        os.chmod(d, 0o700)
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_scan_does_not_block_on_fifo_script(self):
        from vdeter.poc.execution.catalog import scan_catalog
        from vdeter.poc.testing.execution_fakes import poc_toml, write_poc_package

        d = self._catalog_dir()
        os.mkfifo(os.path.join(d, "f.py"))
        with open(os.path.join(d, "f.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="f", script_basename="f.py"))
        write_poc_package(d, "good")
        cat = self._with_hang_guard(lambda: scan_catalog(d))
        self.assertIn("good", cat.valid_by_id())            # 정상 package는 유효
        self.assertNotIn("f", cat.valid_by_id())            # FIFO package는 거부
        self.assertTrue(any(p.name == "f" for p in cat.invalid))

    def test_scan_does_not_block_on_fifo_manifest(self):
        from vdeter.poc.execution.catalog import scan_catalog

        d = self._catalog_dir()
        with open(os.path.join(d, "g.py"), "wb") as fh:
            fh.write(b"x = 1\n")
        os.mkfifo(os.path.join(d, "g.poc.toml"))
        cat = self._with_hang_guard(lambda: scan_catalog(d))
        self.assertNotIn("g", cat.valid_by_id())
        self.assertTrue(any(p.name == "g" for p in cat.invalid))

    def test_snapshot_does_not_block_on_fifo_source(self):
        """scan 이후 원본 script가 FIFO로 교체(TOCTOU)돼도 snapshot은 block 없이 거부한다."""
        from vdeter.poc.execution.snapshot import SnapshotError, create_snapshot
        from vdeter.poc.testing.execution_fakes import poc_toml

        d = self._catalog_dir()
        os.mkfifo(os.path.join(d, "good.py"))
        with open(os.path.join(d, "good.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="good", script_basename="good.py"))
        dest = tempfile.mkdtemp(prefix="fault-snap-")
        self.addCleanup(lambda: __import__("shutil").rmtree(dest, ignore_errors=True))
        with self.assertRaises(SnapshotError):
            self._with_hang_guard(
                lambda: create_snapshot(d, "good", dest_dir=os.path.join(dest, "s")))


if __name__ == "__main__":
    unittest.main()
