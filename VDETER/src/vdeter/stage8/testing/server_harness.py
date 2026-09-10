"""server(supervisor engine) 측 시험 harness helper(test-support).

이 module은 private `_supervisor.engine`을 import하므로 public SDK surface가 아니다.
server 측 시험만 import한다(공개 계약 시험은 `poc_api`·`testing.fakes`만 쓴다).
"""

from __future__ import annotations

from .._supervisor.engine import SupervisorEngine
from ..poc_api.messages import build_message
from ..poc_api.negotiation import ServerCapabilityConfig
from .fakes import FakeActionSubmitProvider, FakeClock, build_manifest

DEFAULT_TOKEN = bytes(range(32))
DEFAULT_TOKEN_HEX = DEFAULT_TOKEN.hex()


def make_engine(*, manifest=None, server=None, provider=None, token=DEFAULT_TOKEN,
                session="s1", workload="w1", run_id="r1", scenario="SC-VDE-006"):
    return SupervisorEngine(
        session_id=session,
        workload_id=workload,
        run_id=run_id,
        scenario_id=scenario,
        expected_token=token,
        manifest=manifest or build_manifest(),
        server_config=server or ServerCapabilityConfig(),
        provider=provider if provider is not None else FakeActionSubmitProvider(),
    )


def hello_message(*, token_hex=DEFAULT_TOKEN_HEX, run_id="r1", session="s1", workload="w1",
                  epoch=0, connect_kind="initial", caps=("observe_packet", "replay_packet"),
                  triggers=("SCENARIO_STARTED", "PACKET_OBSERVED"), required=(),
                  last_completed=0, unresolved=()):
    manifest = build_manifest(requested_capabilities=caps, requested_triggers=triggers,
                              required_capabilities=required)
    body = {
        "token": token_hex,
        "run_id": run_id,
        "client_nonce": "nonce",
        "protocol_minor_min": 0,
        "protocol_minor_max": 0,
        "connect_kind": connect_kind,
        "manifest": manifest.as_hello_value(),
        "requested_capabilities": list(caps) + list(required),
        "requested_triggers": list(triggers),
        "last_completed_sequence": last_completed,
        "unresolved_request_ids": list(unresolved),
    }
    return build_message("HELLO", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0, body=body)


def armed_message(*, session="s1", workload="w1", epoch=1):
    return build_message("ARMED", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0, body={"client_nonce": "x"})


def ack_message(cumulative, *, session="s1", workload="w1", epoch=1):
    return build_message("EVENT_ACK", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0,
                         body={"cumulative_sequence": cumulative})


def action_submit_message(request_id, *, kind="REPLAY_PACKET", target="ROC",
                          schedule=None, payload=None, source_observation_id=None,
                          session="s1", workload="w1", epoch=1):
    action = {
        "action_kind": kind,
        "target": target,
        "schedule": dict(schedule or {}),
        "payload": dict(payload or {}),
    }
    if source_observation_id is not None:
        action["source_observation_id"] = source_observation_id
    return build_message("ACTION_SUBMIT", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0,
                         body={"request_id": request_id, "action": action})


def drive_to_running(engine, clock: FakeClock):
    """handshake + ARMED를 태워 engine을 RUNNING으로 만든다."""

    from ..poc_api.messages import parse_envelope

    engine.start_handshake(clock.monotonic())
    engine.receive(parse_envelope(hello_message()), clock.monotonic())
    engine.receive(parse_envelope(armed_message()), clock.monotonic())
    return engine


__all__ = (
    "DEFAULT_TOKEN",
    "DEFAULT_TOKEN_HEX",
    "make_engine",
    "hello_message",
    "armed_message",
    "ack_message",
    "action_submit_message",
    "drive_to_running",
)
