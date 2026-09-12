import unittest
from unittest.mock import Mock
from apps.stage8_collision_exchange import CollisionExchange
from vdeter.stage7.collision_runtime import ENDPOINTS


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
        return CollisionExchange(clients, dual=True), clients

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
