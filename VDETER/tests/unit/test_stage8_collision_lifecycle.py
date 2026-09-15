import unittest
from unittest.mock import Mock
from apps.workload_rx_scope import WorkloadRxScope
from vdeter.b210_system.collision_runtime import ENDPOINTS


class ScopeControlTests(unittest.TestCase):
    def build(self):
        clients = {}
        for ep in ENDPOINTS:
            state = {"fanout": False}
            def call(command, body, ep=ep, state=state):
                if command == "poc_wired_broadcast_optin":
                    previous = state["fanout"]
                    if body["enabled"] is not None:
                        state["fanout"] = body["enabled"]
                    return {"enabled": state["fanout"], "previous": previous}
                mode = body["mode"]
                if mode == "POLL":
                    return dict(session_id=body["session_id"], receiver=ep, fault=None,
                                counts={}, pending=0, decisions=[], observations=[], application_receipts=[],
                                dual_ready=ep == "MASS-B")
                return {"active": True, "enabled": True, "disabled": True}
            clients[ep] = Mock(call=Mock(side_effect=call))
        return WorkloadRxScope(clients, dual=True), clients

    def test_start_ready_close_no_worker_thread(self):
        scope, clients = self.build()
        scope.start()
        self.assertTrue(scope.dual_ready)
        self.assertFalse(hasattr(scope, "thread"))
        scope.close()
        self.assertFalse(scope.enabled)
        self.assertFalse(scope.dual_ready)
        for client in clients.values():
            modes = [c.args[1].get("mode") for c in client.call.call_args_list]
            self.assertNotIn("ROUND", modes)
            self.assertNotIn("SNAPSHOT", modes)

    def test_partial_enable_is_owned_before_call_and_cleanup(self):
        scope, clients = self.build()
        original = clients["MASS-B"].call.side_effect
        def fail(command, body):
            if body.get("mode") == "ENABLE":
                raise RuntimeError("reply lost after activation")
            return original(command, body)
        clients["MASS-B"].call.side_effect = fail
        with self.assertRaises(Exception):
            scope.start()
        self.assertFalse(scope.enabled)
        self.assertTrue(scope.evidence["cleanup_complete"])

    def test_wrong_poll_owner_fails_closed(self):
        scope, clients = self.build()
        scope.start()
        clients["MASS-B"].call.side_effect = lambda *a: dict(
            session_id="stale", receiver="MASS-B", fault=None)
        with self.assertRaises(Exception):
            scope.poll()

    def test_close_interrupt_restores_other_endpoints_then_reraises(self):
        scope, clients = self.build()
        scope.start()
        original = clients['MASS-A'].call.side_effect
        def interrupt(command, body):
            if body.get('mode') == 'CLOSE':
                raise KeyboardInterrupt()
            return original(command, body)
        clients['MASS-A'].call.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            scope.close()
        self.assertFalse(scope.enabled)
        self.assertFalse(scope.previous_fanout)
        self.assertTrue(scope.evidence['cleanup_complete'])
        self.assertFalse(scope.dual_ready)

    # --- tuning readback 값 보존 (라운드 4 미확인 항목 "tuning readback 값") -------
    #
    # 라운드 4까지 evidence에 남던 것은 "검증 실패의 부재"뿐이었다. scope는 적용 전/후·복원 후
    # readback을 이미 갖고 있으므로, 여기서는 그 값이 **추가 장치 조회 없이** evidence까지
    # 오는지와, 복원 실패 시 값·원인이 지워지지 않는지를 고정한다.

    _PREVIOUS = {"lane_center_hz": {0: 1, 1: 1}, "analog_bandwidth_hz": 200000}
    _READBACK = {"rx_lo_hz": 5, "lane_dsp_hz": {0: -2, 1: 2},
                 "lane_center_hz": {0: 7, 1: 3}, "analog_bandwidth_hz": 4700000}

    def test_poll_preserves_tuning_readback_values_in_evidence(self):
        scope, clients = self.build()
        original = clients["MASS-B"].call.side_effect

        def with_readback(command, body):
            reply = original(command, body)
            if body.get("mode") == "POLL":
                reply.update(dual_previous=self._PREVIOUS, dual_readback=self._READBACK)
            return reply
        clients["MASS-B"].call.side_effect = with_readback
        scope.start()
        actor = scope.evidence["endpoints"]["MASS-B"]
        self.assertEqual(actor["dual_previous"], self._PREVIOUS)
        self.assertEqual(actor["dual_readback"], self._READBACK)
        # dual이 아닌 endpoint는 값이 없다(None) — 있는 척하지 않는다.
        self.assertIsNone(scope.evidence["endpoints"]["MASS-A"]["dual_readback"])
        # 추가 장치 조회 없음: 값을 얻으려고 새 명령을 만들지 않는다.
        modes = {c.args[1].get("mode") for c in clients["MASS-B"].call.call_args_list
                 if c.args[0] == "poc_collision"}
        self.assertEqual(modes, {"ENABLE", "ACTIVATE", "POLL"})

    def test_close_preserves_restore_readback_and_verdict(self):
        scope, clients = self.build()
        original = clients["MASS-B"].call.side_effect

        def with_restore(command, body):
            reply = original(command, body)
            if body.get("mode") == "DISABLE":
                reply.update(restore_previous=self._PREVIOUS, restore_readback=dict(self._PREVIOUS),
                             restore_verified=True)
            return reply
        clients["MASS-B"].call.side_effect = with_restore
        scope.start()
        scope.close()
        self.assertEqual(scope.evidence["endpoints"]["MASS-B"]["restore"],
                         {"previous": self._PREVIOUS, "readback": self._PREVIOUS, "verified": True})
        # 값을 주지 않은 endpoint는 None으로 남는다 — 검증됐다고 꾸미지 않는다.
        self.assertEqual(scope.evidence["endpoints"]["MASS-A"]["restore"],
                         {"previous": None, "readback": None, "verified": None})
        self.assertTrue(scope.evidence["cleanup_complete"])

    def test_transient_restore_failure_is_kept_even_after_retry_succeeds(self):
        """첫 DISABLE이 복원 검증 실패로 예외, 둘째가 성공 — 재시도 계약은 그대로이고
        실패 이력(값·원인)이 최종 성공 기록에 덮여 사라지지 않는다."""
        scope, clients = self.build()
        original = clients["MASS-B"].call.side_effect
        cause = ("RX restore unverified; ownership retained; "
                 "previous={'analog_bandwidth_hz': 200000}; restored={'analog_bandwidth_hz': 4700000}")
        seen = {"disable": 0}

        def failing_once(command, body):
            if body.get("mode") == "DISABLE":
                seen["disable"] += 1
                if seen["disable"] == 1:
                    raise RuntimeError(cause)
                reply = original(command, body)
                reply.update(restore_previous=self._PREVIOUS, restore_readback=dict(self._PREVIOUS),
                             restore_verified=True)
                return reply
            return original(command, body)
        clients["MASS-B"].call.side_effect = failing_once
        scope.start()
        scope.close()
        record = scope.evidence["endpoints"]["MASS-B"]
        self.assertIs(record["restore"]["verified"], True)
        self.assertEqual(len(record["restore_errors"]), 1)
        self.assertIn("previous=", record["restore_errors"][0])
        self.assertIn("restored=", record["restore_errors"][0])
        self.assertTrue(scope.evidence["cleanup_complete"])

    def test_persistent_restore_failure_keeps_values_cause_and_ownership(self):
        """복원이 끝내 실패하면 예외(소유권 유지)이고 evidence에 값·원인이 남는다.
        7.2 s 재시도 창은 모듈의 time을 대체해 즉시 소진시킨다(실제 대기 없음)."""
        import itertools
        from unittest.mock import patch

        scope, clients = self.build()
        original = clients["MASS-B"].call.side_effect
        cause = "manual RX restore differs; ownership retained; previous={'rx_lo_hz': 1}; restored={'rx_lo_hz': 9}"

        def always_fail(command, body):
            if body.get("mode") == "DISABLE":
                raise RuntimeError(cause)
            return original(command, body)
        clients["MASS-B"].call.side_effect = always_fail
        scope.start()
        with patch("apps.workload_rx_scope.time") as fake_time:
            fake_time.monotonic.side_effect = itertools.count(0.0, 1.0)
            fake_time.sleep = lambda _s: None
            with self.assertRaises(Exception) as ctx:
                scope.close()
        self.assertIn("ownership", str(ctx.exception).lower())
        record = scope.evidence["endpoints"]["MASS-B"]
        self.assertEqual(record["restore"]["verified"], False)
        self.assertIn("restored={'rx_lo_hz': 9}", record["restore"]["error"])
        self.assertGreaterEqual(len(record["restore_errors"]), 1)
        self.assertLessEqual(len(record["restore_errors"]), 4)
        self.assertFalse(scope.evidence["cleanup_complete"])
        self.assertIn("MASS-B", scope.enabled)          # 소유권 유지
