"""Workload RX scope controller on existing TEST sockets (no exchange thread).

The historical filename is retained for one integration cycle; this object
exchanges NO transmitter records. Normal RX never waits for these polls.

`evidence` (bounded, memory only; serialized by the caller after the workload):
  model, rf_collision_measured, session_id, cleanup_complete,
  endpoints[<endpoint>]:
    counts, pending, last_decisions      — from POLL
    dual_previous, dual_readback         — tuning readback before/after apply (None when not dual);
                                           values the scope already holds, no extra device query
    restore: {previous, readback, verified}   — from the DISABLE reply; verified is
                                           None (nothing to restore) / True / False
    restore_errors                       — bounded (4) history of DISABLE failures, kept even
                                           when a later retry succeeds
"""
from collections import deque
import time
import uuid
from vdeter.b210_system.collision_runtime import ENDPOINTS, MAX_OBSERVATIONS, MODEL


def _unavailable(message, *, ownership=False):
    from apps.standalone.poc_run import collision_state_error
    return collision_state_error(message, ownership=ownership)


class WorkloadRxScope:
    def __init__(self, clients, actor="MASS-B", *, dual=False, guard=None):
        self.clients, self.actor, self.dual, self.guard = clients, actor, dual, guard
        self.session_id = uuid.uuid4().hex
        self.enabled = []
        self.previous_fanout = {}
        self.application_receipts = deque(maxlen=128)
        self.queue = deque()
        self.error = None
        self.dual_ready = False
        self.evidence = {"model": MODEL, "rf_collision_measured": False,
                         "session_id": self.session_id, "endpoints": {}}

    def call(self, endpoint, mode, **body):
        return self.clients[endpoint].call("poc_collision", dict(mode=mode, session_id=self.session_id, **body))

    def _guard(self):
        if self.guard is not None:
            self.guard()

    def start(self):
        try:
            for endpoint in ENDPOINTS:
                self.enabled.append(endpoint)  # own cleanup BEFORE remote mutation
                self.call(endpoint, "ENABLE", dual=self.dual and endpoint == self.actor)
                previous = self.clients[endpoint].call("poc_wired_broadcast_optin", {"enabled": None})
                self.previous_fanout[endpoint] = previous["enabled"]
                self.clients[endpoint].call("poc_wired_broadcast_optin", {"enabled": True})
            # Only lifecycle waits: bounded by three existing 2.4-s MAC frames.
            deadline = time.monotonic() + 7.2
            remaining = set(ENDPOINTS)
            while remaining:
                self._guard()
                for endpoint in tuple(remaining):
                    if self.call(endpoint, "ACTIVATE").get("active"):
                        remaining.remove(endpoint)
                if not remaining:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("RX drain/tuning activation deadline")
                time.sleep(.01)
            while True:
                self.poll()
                if not self.dual or self.dual_ready:
                    break
                self._guard()
                if time.monotonic() >= deadline:
                    raise RuntimeError("actual dual RX windows not accepted before readiness deadline")
                time.sleep(.01)
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup:
                exc.add_note(f"RX scope cleanup: {cleanup}")
                if isinstance(exc, Exception) and not isinstance(cleanup, Exception):
                    cleanup.add_note(f"RX activation failed: {exc}")
                    raise cleanup from exc
            if isinstance(exc, Exception):
                raise _unavailable(f"RX scope activation failed: {exc}",
                    ownership=bool(self.enabled or self.previous_fanout)) from exc
            raise

    def check(self):
        if self.error is not None:
            self.dual_ready = False
            raise _unavailable(f"RX observation scope failed: {self.error}")

    def poll(self):
        self.check()
        for endpoint in self.enabled:
            rx = self.call(endpoint, "POLL")
            if rx.get("session_id") != self.session_id or rx.get("receiver") != endpoint or rx.get("fault"):
                self.error = RuntimeError(f"{endpoint}: {rx.get('fault', 'binding mismatch')}")
                self.check()
            record = self.evidence["endpoints"].setdefault(endpoint, {})
            record.update({
                "counts": rx["counts"], "pending": rx["pending"], "last_decisions": rx["decisions"][-16:],
                # tuning readback 값(적용 전/후). scope가 POLL 응답에 이미 싣는 값이라 추가
                # 조회가 없다. dual이 아닌 endpoint는 None이다.
                "dual_previous": rx.get("dual_previous"), "dual_readback": rx.get("dual_readback")})
            self.application_receipts.extend(rx.get("application_receipts", ()))
            if endpoint == self.actor:
                self.dual_ready = bool(rx.get("dual_ready"))
                if len(self.queue) + len(rx["observations"]) > MAX_OBSERVATIONS:
                    self.error = RuntimeError("PoC consumer too slow")
                    self.check()
                self.queue.extend(rx["observations"])

    def observations(self):
        self.poll()
        result = list(self.queue)
        self.queue.clear()
        return result

    def target_receipt(self, endpoint, *, digest, source, start_ns, end_ns):
        self.poll()
        found = [r for r in self.application_receipts
                 if r.get("receiver") == endpoint and r.get("packet_digest") == digest
                 and r.get("source_endpoint") == source
                 and type(r.get("rx_utc_ns")) is int and start_ns <= r["rx_utc_ns"] < end_ns]
        return dict(found[0]) if len(found) == 1 else None

    def close(self):
        self.dual_ready = False  # revoke capability even when restoration fails
        failures = []
        interrupted = None
        for endpoint in tuple(self.enabled):
            try:
                self.call(endpoint, "CLOSE")
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    interrupted = interrupted or exc
                failures.append(f"{endpoint}: {exc}")
        deadline = time.monotonic() + 7.2
        while self.enabled and time.monotonic() < deadline:
            for endpoint in tuple(self.enabled):
                try:
                    reply = self.call(endpoint, "DISABLE")
                    if reply.get("disabled"):
                        self.enabled.remove(endpoint)
                        # 복원 readback(적용 전 목표 / 복원 후 실측 / 검증 결과)을 남긴다.
                        # 값은 scope가 이미 확보한 것이고 여기서 장치를 다시 묻지 않는다.
                        self.evidence["endpoints"].setdefault(endpoint, {})["restore"] = {
                            "previous": reply.get("restore_previous"),
                            "readback": reply.get("restore_readback"),
                            "verified": reply.get("restore_verified")}
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        interrupted = interrupted or exc
                    failures.append(f"{endpoint}: {exc}")
                    failures = failures[-8:]
                    # 복원 실패도 evidence에 남긴다 — 값은 scope가 예외 메시지에 실어 보낸다.
                    # 뒤의 재시도가 성공하더라도 실패 이력은 지우지 않는다(유계 4건).
                    record = self.evidence["endpoints"].setdefault(endpoint, {})
                    record["restore"] = {"verified": False, "error": str(exc)[:600]}
                    record["restore_errors"] = (record.get("restore_errors") or [])[-3:] + [str(exc)[:600]]
            if self.enabled:
                try:
                    time.sleep(.01)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        raise
                    interrupted = interrupted or exc
        for endpoint, previous in tuple(self.previous_fanout.items()):
            try:
                self.clients[endpoint].call("poc_wired_broadcast_optin", {"enabled": previous})
                del self.previous_fanout[endpoint]
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    interrupted = interrupted or exc
                failures.append(f"fanout {endpoint}: {exc}")
        self.queue.clear()
        if self.enabled or self.previous_fanout:
            self.evidence["cleanup_complete"] = False
            if interrupted is not None:
                interrupted.add_note(f"RX ownership unresolved: {self.enabled}; {list(self.previous_fanout)}")
                raise interrupted
            raise _unavailable(f"RX scope restoration incomplete: {failures[-8:]}", ownership=True)
        self.dual_ready = False
        self.evidence["cleanup_complete"] = True
        if interrupted is not None:
            raise interrupted
        self.check()
