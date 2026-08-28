#!/usr/bin/env python3
"""MASS/ROC 콘솔 서버 — 시뮬레이션 루프 + 로컬 HTTP 서버

    python3 apps/console_server.py            # 기본 포트 8420
    python3 apps/console_server.py --port 9000

브라우저 두 창으로 같은 시뮬레이션을 서로 다른 관점에서 본다.

    http://localhost:8420/roc     육상 원격운영센터(ROC) 콘솔
    http://localhost:8420/mass    본선(MASS) 콘솔

구조
    메인 스레드            http.server(ThreadingHTTPServer)로 요청 처리
    시뮬레이션 스레드 1개   10 Hz로 tick()을 돌리고 스냅샷 JSON을 갱신
    공유 상태             threading.Lock 하나로 보호

원칙
    - MassAgent·RocAgent·common/** 는 손대지 않는다. 이 파일은 그 위에 얹는
      구동기일 뿐이다. 상태 필드(state.sog 등)에 값을 넣는 것은 허용 범위다.
    - 표준 라이브러리만 쓴다.
    - 무선 계층을 거치지 않는다. payload를 두 행위자 사이에 직접 넘긴다.
      다만 "링크 두절" 모의를 위해 넘기는 길목 하나(deliver_to_mass /
      _deliver_to_roc)를 두고, 그곳에서 끊을 수 있게 했다.

나중에 얹을 자리 (지시서 §8)
    MASS에게 명령이 들어가는 지점은 Simulation.deliver_to_mass(outbound,
    source_id, now) 하나뿐이다. 위조 송신국 실증은 source_id만 바꿔 같은
    길을 타면 된다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 저장소 루트를 import 경로에 넣는다 (apps/scenario_runner.py와 같은 방식).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from MASS.mass_agent import MassAgent, ShipState  # noqa: E402
from ROC.roc_agent import RocAgent  # noqa: E402
from common import config  # noqa: E402
from common.codec import decode  # noqa: E402
from common.messages import application_ack as ack  # noqa: E402
from common.messages import position_report as pos  # noqa: E402
from common.messages import route_instruction as route  # noqa: E402
from common.outbound import (  # noqa: E402
    DATA_TYPE_POSITION_REPORT,
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
    Outbound,
)

from apps.console_transport import build_transport  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console_static")
STATIC_FILES = {
    "console.css": "text/css; charset=utf-8",
    "console.js": "application/javascript; charset=utf-8",
}
PAGES = {"/roc": "roc.html", "/mass": "mass.html"}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420

# 시뮬레이션 루프 주기(초). 브라우저는 4 Hz로 폴링하므로 이보다 촘촘하면 된다.
LOOP_INTERVAL_SEC = 0.1

# 시연 초기 상태 — 부산 앞바다. 속력은 반드시 넣는다 (지시서 §4.1 함정).
START_LATITUDE = 35.060
START_LONGITUDE = 129.120
START_HEADING = 60.0
DEFAULT_SOG_KNOTS = 12.0

# 항적·로그 보관 상한 (지시서 §7.2)
TRACK_MAX_POINTS = 600
LOG_MAX_ENTRIES = 200
# 항적 점은 직전 점에서 이만큼(도) 이상 떨어졌을 때만 추가한다 (약 30 m).
TRACK_MIN_STEP_DEG = 0.0003

# 원격 조종(006)을 실제 항법에 반영하기 위한 가상 목표점 거리(NM).
#
# MassAgent는 waypoint만 따라가고 Heading Setpoint 자체로는 조타하지 않는다.
# 게다가 waypoint가 없는 지시는 "incomplete"로 거절한다(_evaluate). 그래서
# 구동기가 목표 침로 방향으로 이 거리만큼 떨어진 waypoint 하나를 함께 실어
# 보낸다. 메시지 자체에는 Heading Setpoint와 Fallback State가 그대로 실린다.
CONTROL_LEG_NM = 3.0

# ROC가 관측한 위치가 없을 때 조종 목표점을 계산할 기준.
_FALLBACK_ORIGIN = (START_LATITUDE, START_LONGITUDE)


# --------------------------------------------------------------------------
# 시뮬레이션
# --------------------------------------------------------------------------


class Simulation:
    """두 행위자를 잇고, 화면에 낼 스냅샷을 만든다. 모든 공개 메서드는 락을 잡는다."""

    def __init__(self, transport_kind: str = "direct") -> None:
        self.lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        # 전송 경로. vdeter를 못 만들면 사유와 함께 direct로 내려간다.
        self.transport, self.transport_note = build_transport(
            transport_kind, mass_mmsi=config.MASS_MMSI, roc_mmsi=config.ROC_MMSI
        )
        self._reset_unlocked()

    # ---------------------------------------------------------------- 초기화

    def _reset_unlocked(self) -> None:
        self.mass = MassAgent(
            state=ShipState(
                latitude=START_LATITUDE,
                longitude=START_LONGITUDE,
                heading=START_HEADING,
                sog=DEFAULT_SOG_KNOTS,
            )
        )
        self.roc = RocAgent()
        self.started = time.monotonic()
        self.now = 0.0

        # 링크 두절 모의. False면 두 방향 모두 payload를 버린다.
        self.link_up = True
        self.dropped = 0

        # ROC 명령 큐. 요청 스레드가 넣고 시뮬레이션 스레드가 비운다.
        self._pending: list[tuple[Outbound, int, dict]] = []
        # 발행 순서대로의 명령 이력. roc.commands는 linkage_id 재사용(취소) 시
        # 덮어써지므로 표시용 이력은 여기서 따로 갖는다.
        self._history: list[dict] = []
        self._current_linkage: int | None = None
        # ACK가 도착했을 때 원 명령을 찾기 위한 표. 전송이 비동기라 필요하다.
        self._meta_by_linkage: dict[int, dict] = {}

        # 화면용 누적 자료
        self.track: list[tuple[float, float]] = []
        self.log: list[dict] = []
        self.mass_log: list[dict] = []
        # ROC가 마지막으로 발행한 노선(계획 항로), MASS가 마지막으로 수락한 항로
        self.planned_route: list[tuple[float, float]] = []
        self.mass_route: list[tuple[float, float]] = []
        # MASS가 마지막으로 수락한 조종 필드
        self.mass_control: dict | None = None

        self._snapshot_json = b"{}"
        self._record_track()
        self._build_snapshot_unlocked()

    def reset(self) -> None:
        with self.lock:
            self._reset_unlocked()

    # ---------------------------------------------------------------- 스레드

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while self._running:
            self.step()
            time.sleep(LOOP_INTERVAL_SEC)

    # 한 번의 시뮬레이션 진행. 지시서 §7.1의 루프 본문이다.
    def step(self) -> None:
        now = time.monotonic() - self.started
        with self.lock:
            self.now = now
            for out in self.mass.tick(now):
                self._deliver_to_roc(out, now)
            self.roc.tick(now)
            self._drain_pending(now)
            self._pump_transport(now)
            self._record_track()
            self._build_snapshot_unlocked()

    # 전송 경로에서 도착한 payload를 수신 행위자에게 넣는다.
    #
    # 직결이면 같은 스텝에 도착하고, VDETER면 시계가 진행된 뒤 몇 스텝 후에
    # 도착한다. 구동기는 그 차이를 몰라도 된다.
    def _pump_transport(self, now: float) -> None:
        for arrival in self.transport.pump():
            if arrival.destination_id == self.mass.mmsi:
                self._mass_receives(arrival.payload, arrival.source_id, now)
            elif arrival.destination_id == self.roc.mmsi:
                self._roc_receives(arrival.payload, arrival.source_id, now)

    # ---------------------------------------------------------------- 전달

    # MASS→ROC. 위치보고가 이 길을 탄다.
    #
    # sc_vde.position_report(MF 1)로 등록돼 transport를 탄다. addressed 전용이라
    # broadcast가 아니라 ROC를 지정해 보낸다 (DR-PAYLOAD-016).
    def _deliver_to_roc(self, outbound: Outbound, now: float) -> None:
        if not self.link_up:
            self.dropped += 1
            return
        ok, error = self.transport.send(
            outbound.payload,
            source_id=self.mass.mmsi,
            destination_id=self.roc.mmsi,
            data_type=DATA_TYPE_POSITION_REPORT,
        )
        if not ok:
            self.dropped += 1
            self._log(now, "MASS", "ROC", "lost", f"위치보고 전송 거부: {error}")

    # ROC(또는 누구든)→MASS. 명령이 MASS로 나가는 유일한 지점이다 (지시서 §8).
    # meta는 구동기가 표시·부수효과에 쓰는 정보이며 행위자에게는 가지 않는다.
    def deliver_to_mass(
        self, outbound: Outbound, source_id: int, now: float, meta: dict | None = None
    ) -> None:
        meta = meta or {}
        sender = self._station_name(source_id)
        text = meta.get("text") or outbound.label or "메시지"

        if not self.link_up:
            self.dropped += 1
            self._log(now, sender, "MASS", "lost", f"{text} — 링크 두절로 전달 실패")
            return

        # ACK가 돌아왔을 때 어떤 명령이었는지 찾을 수 있게 남긴다.
        linkage = meta.get("linkage_id")
        if linkage is not None:
            self._meta_by_linkage[linkage] = dict(meta, sender=sender, text=text)

        ok, error = self.transport.send(
            outbound.payload,
            source_id=source_id,
            destination_id=self.mass.mmsi,
            data_type=DATA_TYPE_ROUTE_CONTROL_COMMAND,
        )
        if not ok:
            self.dropped += 1
            self._log(now, sender, "MASS", "lost", f"{text} — 전송 거부: {error}")
            return

        self._log(now, sender, "MASS", "send", text)

    # ------------------------------------------------------- 수신 (도착 시점)

    # MASS가 payload를 받는다. 응답(ACK)이 나오면 다시 전송 경로로 돌려보낸다.
    def _mass_receives(self, payload: bytes, source_id: int, now: float) -> None:
        sender = self._station_name(source_id)
        replies = self.mass.receive(payload, source_id=source_id, now=now)

        for reply in replies:
            verdict = self._read_ack(reply)
            meta = self._meta_by_linkage.get(verdict["linkage_id"], {})
            text = meta.get("text", "명령")
            self._apply_verdict(meta, verdict)
            self.mass_log.append(
                {
                    "t": round(now, 1),
                    "from": sender,
                    "source_id": source_id,
                    "text": text,
                    "accepted": verdict["accepted"],
                    "status": verdict["status"],
                    "status_name": verdict["status_name"],
                    "reason_name": verdict["reason_name"],
                    "linkage_id": verdict["linkage_id"],
                }
            )
            del self.mass_log[:-LOG_MAX_ENTRIES]
            self._log(
                now,
                "MASS",
                sender,
                "ack" if verdict["accepted"] else "reject",
                verdict["status_name"]
                + ("" if verdict["accepted"] else f" ({verdict['reason_name']})"),
            )
            if not self.link_up:
                self.dropped += 1
                continue
            ok, error = self.transport.send(
                reply.payload,
                source_id=self.mass.mmsi,
                destination_id=source_id,
                data_type=DATA_TYPE_ROUTE_CONTROL_ACK,
            )
            if not ok:
                self.dropped += 1
                self._log(now, "MASS", sender, "lost", f"ACK 전송 거부: {error}")

    # ROC가 payload를 받는다. 위치보고면 항적이 갱신되고, ACK면 이력에 적힌다.
    def _roc_receives(self, payload: bytes, source_id: int, now: float) -> None:
        self.roc.receive(payload, source_id=source_id, now=now)
        verdict = self._read_ack_payload(payload)
        if verdict is not None:
            self._record_answer(verdict, now)

    # 큐에 쌓인 ROC 명령을 MASS에게 넘긴다.
    def _drain_pending(self, now: float) -> None:
        pending, self._pending = self._pending, []
        for outbound, source_id, meta in pending:
            self.deliver_to_mass(outbound, source_id, now, meta)

    # ROC에게 실제로 닿은 응용 ACK를 표시용 이력에 적는다.
    #
    # roc.commands는 Linkage ID가 재사용되면(취소는 원 항로의 ID를 그대로 쓴다)
    # 이전 기록을 덮어쓴다. 이력 표시는 그 영향을 받지 않도록 여기서 따로 적는다.
    def _record_answer(self, verdict: dict, now: float) -> None:
        for item in reversed(self._history):
            if item["linkage_id"] == verdict["linkage_id"] and item.get("status") is None:
                item["status"] = verdict["status"]
                item["status_name"] = verdict["status_name"]
                item["reason_name"] = verdict["reason_name"]
                item["accepted"] = verdict["accepted"]
                item["answered_at"] = now
                return

    # payload가 응용 ACK면 판정을 돌려주고, 아니면 None (위치보고 등).
    def _read_ack_payload(self, payload: bytes) -> dict | None:
        try:
            message = decode(payload)
        except ValueError:
            return None
        if not isinstance(message, ack.ApplicationAck):
            return None
        status = message.status
        return {
            "linkage_id": message.linkage_id,
            "status": status,
            "status_name": ack.STATUS_NAMES.get(status, f"reserved({status})"),
            "reason_name": ack.REASON_NAMES.get(message.reason, "?"),
            "accepted": status == ack.STATUS_ACKNOWLEDGED,
        }

    # MASS가 돌려준 응용 ACK를 읽는다. 해석에 실패하면 label로 대신한다.
    def _read_ack(self, reply: Outbound) -> dict:
        try:
            message = decode(reply.payload)
        except ValueError:
            message = None
        if isinstance(message, ack.ApplicationAck):
            status = message.status
            return {
                "linkage_id": message.linkage_id,
                "status": status,
                "status_name": ack.STATUS_NAMES.get(status, f"reserved({status})"),
                "reason_name": ack.REASON_NAMES.get(message.reason, "?"),
                "accepted": status == ack.STATUS_ACKNOWLEDGED,
            }
        return {
            "linkage_id": None,
            "status": None,
            "status_name": reply.label,
            "reason_name": "?",
            "accepted": "acknowledged" in reply.label.lower(),
        }

    # 수락된 명령의 부수효과. 행위자는 손대지 않고 상태 필드만 넣는다.
    #
    # - 원격 조종(006) 수락 → 제어권이 ROC로 넘어갔으므로 autonomy_mode = RC.
    #   planned SOG가 있으면 state.sog에 넣는다 (지시서 §4.1: 속력은 구동기 몫).
    # - 노선 지시(005) 수락 → 본선이 항로를 스스로 따라가므로 감독 자율(MON).
    #   앞서 받은 조종은 새 노선으로 대체된 것으로 본다.
    # - 항로 취소 수락 → 세션은 남고 제어권은 본선으로: autonomy_mode = MON.
    def _apply_verdict(self, meta: dict, verdict: dict) -> None:
        if not verdict["accepted"]:
            return
        kind = meta.get("kind")
        if kind == "route":
            self.mass_route = list(meta.get("waypoints", []))
            self.mass_control = None
            self.mass.state.autonomy_mode = pos.AUTONOMY_MON
        elif kind == "control":
            self.mass_route = list(meta.get("waypoints", []))
            self.mass.state.autonomy_mode = pos.AUTONOMY_RC
            self.mass_control = {
                "heading": meta.get("heading"),
                "fallback": meta.get("fallback"),
                "fallback_name": route.FALLBACK_NAMES.get(meta.get("fallback"), "?"),
            }
            if meta.get("sog") is not None:
                self.mass.state.sog = float(meta["sog"])
        elif kind == "cancel":
            self.mass_route = []
            self.mass_control = None
            self.mass.state.autonomy_mode = pos.AUTONOMY_MON

    # ---------------------------------------------------------------- 명령 발행

    # POST /api/command 본문을 받아 ROC 행위자에게 명령을 만들게 하고 큐에 넣는다.
    # 반환값은 응답 JSON. 잘못된 요청이면 ValueError.
    def submit_command(self, body: dict) -> dict:
        kind = body.get("kind")
        with self.lock:
            now = self.now
            if kind == "route":
                outbound, meta = self._make_route(body, now)
            elif kind == "control":
                outbound, meta = self._make_control(body, now)
            elif kind == "cancel":
                outbound, meta = self._make_cancel(body, now)
            else:
                raise ValueError(f"알 수 없는 명령 종류: {kind!r}")

            self._pending.append((outbound, self.roc.mmsi, meta))
            self._history.append(
                {
                    "linkage_id": meta["linkage_id"],
                    "kind": kind,
                    "text": meta["text"],
                    "issued_at": now,
                }
            )
            del self._history[:-LOG_MAX_ENTRIES]
            return {"ok": True, "linkage_id": meta["linkage_id"], "text": meta["text"]}

    def _make_route(self, body: dict, now: float) -> tuple[Outbound, dict]:
        waypoints = _parse_waypoints(body.get("waypoints"))
        route_type = int(body.get("route_type", route.ROUTE_TYPE_MANDATORY))
        if route_type not in route.ROUTE_TYPE_NAMES or route_type == route.ROUTE_TYPE_CANCELLATION:
            raise ValueError(f"항로 종류가 올바르지 않습니다: {route_type}")
        duration = int(body.get("duration", route.DURATION_NOT_AVAILABLE))
        if not 0 <= duration <= route.DURATION_NOT_AVAILABLE:
            raise ValueError("duration 범위를 벗어났습니다")

        outbound = self.roc.issue_route(
            self.mass.mmsi, waypoints, now=now, route_type=route_type, duration=duration
        )
        linkage_id = self._linkage_of(outbound)
        self._current_linkage = linkage_id
        self.planned_route = list(waypoints)
        name = route.ROUTE_TYPE_NAMES.get(route_type, str(route_type))
        return outbound, {
            "kind": "route",
            "linkage_id": linkage_id,
            "waypoints": waypoints,
            "text": f"노선 지시 · 웨이포인트 {len(waypoints)}개 · {name}",
        }

    def _make_control(self, body: dict, now: float) -> tuple[Outbound, dict]:
        heading = float(body.get("heading", -1))
        if not 0.0 <= heading < 360.0:
            raise ValueError("목표 침로는 0 이상 360 미만이어야 합니다")
        fallback = int(body.get("fallback", route.FALLBACK_NONE))
        if fallback not in route.FALLBACK_NAMES:
            raise ValueError(f"복귀 모드가 올바르지 않습니다: {fallback}")
        sog = body.get("sog")
        if sog is not None:
            sog = float(sog)
            if not 0.0 <= sog <= 102.2:
                raise ValueError("속력은 0~102.2 knot 범위여야 합니다")

        # ROC는 자기가 관측한 위치를 기준으로 목표점을 잡는다.
        origin = self._observed_position() or _FALLBACK_ORIGIN
        target = _project(origin[0], origin[1], heading, CONTROL_LEG_NM)
        waypoints = [target]

        outbound = self.roc.issue_route(
            self.mass.mmsi,
            waypoints,
            now=now,
            route_type=route.ROUTE_TYPE_RECOMMENDED,
            heading_setpoint=heading,
            fallback_state=fallback,
        )
        linkage_id = self._linkage_of(outbound)
        self._current_linkage = linkage_id
        self.planned_route = [origin, target]
        text = f"원격 조종 · 침로 {heading:05.1f}° · 복귀 {route.FALLBACK_NAMES[fallback]}"
        if sog is not None:
            text += f" · 속력 {sog:.1f} kn"
        return outbound, {
            "kind": "control",
            "linkage_id": linkage_id,
            "waypoints": waypoints,
            "heading": heading,
            "fallback": fallback,
            "sog": sog,
            "text": text,
        }

    def _make_cancel(self, body: dict, now: float) -> tuple[Outbound, dict]:
        linkage_id = body.get("linkage_id", self._current_linkage)
        if linkage_id is None:
            raise ValueError("취소할 항로가 없습니다. 먼저 노선이나 조종을 발행하세요")
        linkage_id = int(linkage_id)
        outbound = self.roc.cancel_route(self.mass.mmsi, linkage_id, now=now)
        self.planned_route = []
        return outbound, {
            "kind": "cancel",
            "linkage_id": linkage_id,
            "text": f"항로 취소 · Linkage {linkage_id}",
        }

    # 방금 만든 Outbound의 Linkage ID. RocAgent가 마지막에 등록한 명령이다.
    def _linkage_of(self, outbound: Outbound) -> int:
        try:
            message = decode(outbound.payload)
            if isinstance(message, route.RouteInstruction):
                return message.linkage_id
        except ValueError:
            pass
        return max(self.roc.commands) if self.roc.commands else 0

    def _observed_position(self) -> tuple[float, float] | None:
        track = self.roc.tracks.get(self.mass.mmsi)
        if track is None:
            return None
        lat, lon = track.report.latitude_degrees, track.report.longitude_degrees
        if lat is None or lon is None:
            return None
        return lat, lon

    # ---------------------------------------------------------------- 링크

    def set_link(self, up: bool) -> dict:
        with self.lock:
            if up != self.link_up:
                self.link_up = up
                self._log(
                    self.now,
                    "SIM",
                    "—",
                    "link",
                    "무선 링크 복구" if up else "무선 링크 두절 (모의)",
                )
            return {"ok": True, "link_up": self.link_up}

    # ---------------------------------------------------------------- 기록

    def _log(self, now: float, sender: str, receiver: str, kind: str, text: str) -> None:
        self.log.append(
            {"t": round(now, 1), "from": sender, "to": receiver, "kind": kind, "text": text}
        )
        del self.log[:-LOG_MAX_ENTRIES]

    def _record_track(self) -> None:
        lat, lon = self.mass.state.latitude, self.mass.state.longitude
        if self.track:
            last_lat, last_lon = self.track[-1]
            if abs(lat - last_lat) < TRACK_MIN_STEP_DEG and abs(lon - last_lon) < TRACK_MIN_STEP_DEG:
                return
        self.track.append((lat, lon))
        del self.track[:-TRACK_MAX_POINTS]

    def _station_name(self, source_id: int) -> str:
        if source_id == self.roc.mmsi:
            return "ROC"
        if source_id == self.mass.mmsi:
            return "MASS"
        return str(source_id)

    # ---------------------------------------------------------------- 스냅샷

    @property
    def snapshot_json(self) -> bytes:
        with self.lock:
            return self._snapshot_json

    def _build_snapshot_unlocked(self) -> None:
        self._snapshot_json = json.dumps(
            self._snapshot_dict(self.now), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def _snapshot_dict(self, now: float) -> dict:
        mass, roc = self.mass, self.roc
        state = mass.state
        waypoint = mass.active_waypoint
        next_waypoint = None
        if waypoint is not None and waypoint.latitude_degrees is not None:
            next_waypoint = {
                "lat": round(waypoint.latitude_degrees, 5),
                "lon": round(waypoint.longitude_degrees, 5),
            }

        lost = set(roc.link_lost)
        tracks = []
        for source_id, track in sorted(roc.tracks.items()):
            report = track.report
            tracks.append(
                {
                    "source_id": source_id,
                    "updated_at": round(track.updated_at, 1),
                    "age": round(now - track.updated_at, 1),
                    "report_count": track.report_count,
                    "lat": _r(report.latitude_degrees),
                    "lon": _r(report.longitude_degrees),
                    "sog": report.sog_knots,
                    "cog": report.cog_degrees,
                    "heading": report.heading_degrees,
                    "autonomy_mode": report.autonomy_mode,
                    "autonomy_name": report.autonomy_mode_name,
                    "lost": source_id in lost,
                }
            )

        # 명령 이력. 응답은 ROC에게 실제로 닿은 ACK만 기록돼 있다(_record_answer).
        # 같은 Linkage ID가 다시 발행되면(취소 재시도 등) 응답 없는 앞선 발행은
        # "재발행됨"으로 표시한다.
        latest_index = {item["linkage_id"]: index for index, item in enumerate(self._history)}
        commands = []
        for index, item in enumerate(self._history):
            answered = item.get("status") is not None
            superseded = not answered and latest_index[item["linkage_id"]] != index
            if answered:
                status_name = item["status_name"]
            elif superseded:
                status_name = "재발행됨"
            else:
                status_name = "대기중"
            commands.append(
                {
                    "linkage_id": item["linkage_id"],
                    "kind": item["kind"],
                    "text": item["text"],
                    "issued_at": round(item["issued_at"], 1),
                    "answered": answered,
                    "status": item.get("status"),
                    "status_name": status_name,
                    "superseded": superseded,
                    "reason_name": item.get("reason_name", "") if answered else "",
                    "accepted": bool(item.get("accepted")) if answered else False,
                    "answered_at": round(item["answered_at"], 1) if answered else None,
                }
            )

        return {
            "t": round(now, 1),
            "link_up": self.link_up,
            "dropped": self.dropped,
            "transport": {
                "name": self.transport.name,
                "detail": self.transport.detail,
                "operational": self.transport.operational,
                **self.transport.stats(),
            },
            "mass": {
                "mmsi": mass.mmsi,
                "lat": round(state.latitude, 6),
                "lon": round(state.longitude, 6),
                "heading": round(state.heading, 1),
                "sog": round(state.sog, 1),
                "autonomy_mode": state.autonomy_mode,
                "autonomy_name": pos.AUTONOMY_MODE_NAMES.get(state.autonomy_mode, "?"),
                "has_route": mass.has_route,
                "remaining_waypoints": mass.remaining_waypoints,
                "route_linkage_id": mass.route_linkage_id,
                "next_waypoint": next_waypoint,
                "rejected_count": mass.rejected_count,
                "ignored_count": mass.ignored_count,
                "control": self.mass_control,
                "route": [[round(a, 5), round(b, 5)] for a, b in self.mass_route],
                "track": [[round(a, 5), round(b, 5)] for a, b in self.track],
                "log": self.mass_log[-60:],
            },
            "roc": {
                "mmsi": roc.mmsi,
                "report_timeout": roc.report_timeout,
                "tracks": tracks,
                "commands": commands,
                "current_linkage_id": self._current_linkage,
                "planned_route": [[round(a, 5), round(b, 5)] for a, b in self.planned_route],
            },
            "log": self.log[-80:],
        }


# --------------------------------------------------------------------------
# 도우미
# --------------------------------------------------------------------------


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


# 요청 본문의 waypoint 목록을 [(위도, 경도), ...]로 검증한다.
def _parse_waypoints(raw) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("웨이포인트가 하나 이상 필요합니다")
    if len(raw) > route.MAX_WAYPOINTS:
        raise ValueError(f"웨이포인트는 최대 {route.MAX_WAYPOINTS}개입니다")
    result = []
    for index, item in enumerate(raw, start=1):
        try:
            lat, lon = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError(f"WP{index} 좌표를 읽을 수 없습니다") from exc
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(f"WP{index} 좌표 범위를 벗어났습니다")
        result.append((lat, lon))
    return result


# 위치에서 침로 방향으로 distance_nm만큼 떨어진 점 (평면 근사, 짧은 거리용).
def _project(lat: float, lon: float, heading: float, distance_nm: float) -> tuple[float, float]:
    rad = math.radians(heading)
    dlat = distance_nm * math.cos(rad) / 60.0
    dlon = distance_nm * math.sin(rad) / 60.0 / max(math.cos(math.radians(lat)), 1e-6)
    return round(lat + dlat, 6), round(lon + dlon, 6)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class ConsoleHandler(BaseHTTPRequestHandler):
    simulation: Simulation  # main()에서 주입한다
    server_version = "MassRocConsole/1.0"

    # 기본 access log는 4 Hz 폴링으로 시끄러우므로 끈다.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._redirect("/roc")
        elif path in PAGES:
            self._send_file(os.path.join(STATIC_DIR, PAGES[path]), "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
            if name in STATIC_FILES:
                self._send_file(os.path.join(STATIC_DIR, name), STATIC_FILES[name])
            else:
                self._send_json({"error": "not found"}, 404)
        elif path == "/api/state":
            self._send_bytes(self.simulation.snapshot_json, "application/json; charset=utf-8")
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/command":
                self._send_json(self.simulation.submit_command(body))
            elif path == "/api/reset":
                self.simulation.reset()
                self._send_json({"ok": True})
            elif path == "/api/link":
                self._send_json(self.simulation.set_link(bool(body.get("up", True))))
            else:
                self._send_json({"error": "not found"}, 404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    # ---- 응답 도우미

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("본문이 올바른 JSON이 아닙니다") from exc
        if not isinstance(body, dict):
            raise ValueError("본문은 JSON 객체여야 합니다")
        return body

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status)

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self._send_bytes(data, content_type)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MASS/ROC 콘솔 GUI 서버")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--transport",
        choices=("direct", "vdeter"),
        default="direct",
        help="direct=무선 계층 없이 직결(기본), vdeter=실 VDETER Service API 종단",
    )
    args = parser.parse_args(argv)

    simulation = Simulation(transport_kind=args.transport)
    ConsoleHandler.simulation = simulation

    server = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    server.daemon_threads = True
    simulation.start()

    base = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    print(f"전송 경로: {simulation.transport.name} — {simulation.transport.detail}")
    if simulation.transport_note:
        print(f"  ⚠ vdeter를 쓸 수 없어 direct로 내려갔습니다: {simulation.transport_note}")
    print("MASS/ROC 콘솔이 떴습니다. 두 창을 나란히 여세요.")
    print(f"  ROC  콘솔  {base}/roc")
    print(f"  MASS 콘솔  {base}/mass")
    print("종료: Ctrl+C")

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        simulation.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
