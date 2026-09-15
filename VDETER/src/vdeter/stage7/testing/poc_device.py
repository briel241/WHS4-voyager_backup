"""launcher 소유 radio 자원에 대한 **TEST-only PoC device 경계**(§16.2 최소 확장).

Stage 8 PoC supervisor는 workload를 소유한 프로세스(scenario controller child)에 있고, 장치는
launcher 프로세스가 단독 소유한다. 그 사이를 잇는 유일한 경로가 이 seam이다 — supervisor가 장치
객체를 직접 잡지 않고, 이미 있는 TEST adapter socket으로 **canonical 값만** 주고받는다.

설계 규칙:

- **권위 구현 재사용**: 송신은 `Stage7B210RadioAdapter.schedule_tx`(최소 lead·timed retune·lane 겹침·
  정상 TX 우선·TX 원장) 하나로만 나간다. 별도 PoC 예약 원장이나 우회 송신 경로를 만들지 않는다.
- **상위 계층 미의존**: 이 module은 PoC 계층 package를 import하지 않는다(Stage 7 것만 쓴다).
  launcher 진입점의 import 폐포에 상위 계층 전체가 끌려 들어가 qualification 결속 범위가 불필요하게
  넓어지는 것을 막는다. 여기서 다루는 것은 bytes·int·str뿐이다. 이 성질은 import 경계 시험이
  정적으로 강제한다.
- **상태를 만들지 않는다**: 조회는 장치·원장이 이미 가진 것만 돌려준다. 모르면 `None`이고, 그것을
  호출자가 성공으로 바꾸지 못하게 사유를 함께 싣는다.
- **fail-closed**: lease가 없거나 bridge가 잠기지 않았거나 값이 계약을 벗어나면 거부한다.
"""

from __future__ import annotations

from ...common.contracts.radio import RadioTxRequest, SampleBlock
from ...common.identifiers import BurstId, PhysicalChannel
from ...common.time import ClockDomain, TimePoint
from ..configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME

#: 이 seam이 받는 채널 이름(권위 Stage 7 canonical 채널 이름만).
CHANNEL_NAMES = (LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME)

#: device burst 계약(권위 `schedule_tx`가 강제하는 값과 **같은 값**을 여기서도 먼저 거른다).
DEVICE_SAMPLE_RATE_HZ = 307_200
DEVICE_SLOT_SAMPLES = 8_192
_CF32LE_BYTES_PER_SAMPLE = 8

#: 조회 응답에 싣는 RX window 최대 개수(유계 — 원장을 통째로 노출하지 않는다).
_MAX_RX_WINDOWS = 8


class PocDeviceUnavailable(RuntimeError):
    """PoC device 경계를 쓸 수 없다(lease 없음·bridge 미잠금 등). 성공으로 위장하지 않는다."""


def utc_label_for_hardware_ns(bridge, hardware_ns: int) -> int:
    """hardware ns → 권위 host time bridge의 UTC label(ns). bridge가 잠기지 않았으면 거부한다."""

    to_utc = getattr(bridge, "hardware_ns_to_utc_label", None)
    utc_ns = to_utc(int(hardware_ns)) if callable(to_utc) else None
    if utc_ns is None:
        raise PocDeviceUnavailable(
            "host time bridge가 잠겨 있지 않아 TX 시각을 UTC로 변환할 수 없습니다")
    return int(utc_ns)


def build_poc_tx_request(*, bridge, burst_id: str, channel_name: str, transmit_at_hw_ns: int,
                         samples: bytes, transmit_at_utc_ns=None):
    """PoC group TX를 권위 `radio_adapter.schedule_tx`에 낼 `RadioTxRequest`를 만든다.

    launcher 경로(`Stage7PocDeviceSeam.schedule_group_tx`)와 RF-off 세션의 in-process sink
    (`apps/poc_device_seams_inprocess.Stage7AuthoritativeRadioSink`)가 **같은** 구성을 쓴다. 시각 변환
    (hardware→UTC label)과 UTC label 축의 clock id는 bridge가 소유한다 — 임의 문자열로 채우면 요청이
    갖지 않은 clock 정체를 주장하게 된다(`schedule_tx`는 이 필드를 검증하지 않아 조용히 통과한다).
    표본은 device-rate cf32le 한 slot(8,192)이어야 한다. 파형을 만들지도 값을 해석하지도 않는다.
    `transmit_at_utc_ns`를 주면(호출자가 route 결속 **전에** 미리 변환해 fail-fast한 경우) 그 값을 쓴다.
    반환 ``(request, transmit_at_utc_ns)``.
    """

    if channel_name not in CHANNEL_NAMES:
        raise ValueError(f"허용되지 않은 채널 이름: {channel_name!r}")
    data = bytes(samples)
    if len(data) != DEVICE_SLOT_SAMPLES * _CF32LE_BYTES_PER_SAMPLE:
        raise ValueError(
            f"device burst 크기 위반: {len(data)}B "
            f"!= {DEVICE_SLOT_SAMPLES * _CF32LE_BYTES_PER_SAMPLE}B")
    utc_ns = (utc_label_for_hardware_ns(bridge, transmit_at_hw_ns)
              if transmit_at_utc_ns is None else int(transmit_at_utc_ns))
    clock_id = getattr(bridge, "clock_id", None)
    if not clock_id:
        raise PocDeviceUnavailable("bridge clock_id가 없습니다 — TX 시각을 결속할 수 없습니다")
    transmit_at = TimePoint(
        seconds=utc_ns // 1_000_000_000,
        nanoseconds=utc_ns % 1_000_000_000,
        clock_domain=ClockDomain.UTC,
        clock_id=str(clock_id),
    )
    block = SampleBlock(
        sample_format="cf32le",
        sample_rate_hz=DEVICE_SAMPLE_RATE_HZ,
        sample_count=DEVICE_SLOT_SAMPLES,
        data=data,
        first_sample_at=transmit_at,
    )
    request = RadioTxRequest(
        burst_id=BurstId(str(burst_id)),
        channel=PhysicalChannel(channel_name),
        transmit_at=transmit_at,
        samples=block,
    )
    return request, utc_ns


class Stage7PocDeviceSeam:
    """launcher 프로세스 안에서 radio adapter/worker/route sidecar를 감싸는 TEST-only seam."""

    __slots__ = ("_endpoint", "endpoint_id", "_station_id_by_endpoint", "_rx_scope")

    def __init__(self, *, endpoint_id: str, endpoint, station_id_by_endpoint: dict) -> None:
        self.endpoint_id = endpoint_id
        self._endpoint = endpoint
        self._station_id_by_endpoint = dict(station_id_by_endpoint)
        self._rx_scope = None

    # --- 내부 접근자 ----------------------------------------------------------

    def _radio(self):
        radio = getattr(getattr(self._endpoint, "dependencies", None), "radio", None)
        if radio is None:
            raise PocDeviceUnavailable("endpoint에 radio adapter가 없습니다")
        return radio

    def _worker(self):
        radio = self._radio()
        lease = getattr(radio, "lease", None)
        worker = getattr(lease, "worker", None)
        if worker is None:
            raise PocDeviceUnavailable("radio lease가 열려 있지 않습니다(worker 없음)")
        return worker

    # --- 조회 ----------------------------------------------------------------

    def collision_command(self, body):
        from ..rx_scope import WorkloadRxScope
        from ..collision_runtime import CollisionRuntimeError
        mode, owner = body.get("mode"), body.get("session_id")
        allowed = {"ENABLE": {"mode", "session_id", "dual"}}
        for name in ("ACTIVATE", "POLL", "CLOSE", "DISABLE", "SUPPORT"):
            allowed[name] = {"mode", "session_id"}
        if mode not in allowed or set(body) != allowed[mode]:
            raise ValueError("RX scope command keys invalid")
        if not isinstance(owner, str) or not 0 < len(owner) <= 128:
            raise ValueError("RX scope owner invalid")
        if mode == "SUPPORT":
            worker = self._worker()
            return {"dual_supported": self.endpoint_id == "MASS-B" and all(
                callable(getattr(worker, n, None)) for n in ("read_tuning", "apply_manual_tuning", "restore_tuning"))}
        with self._endpoint.stage4.lock:
            if mode == "ENABLE":
                if self._rx_scope is not None:
                    if self._rx_scope.owner != owner or self._rx_scope.dual != body["dual"]:
                        raise CollisionRuntimeError("RX scope already owned")
                else:
                    self._rx_scope = WorkloadRxScope(self, owner, body["dual"])
                if self._rx_scope.dual and not self._rx_scope.active and not self._rx_scope.closed:
                    self._endpoint.stage4.mac.coordinator._rx_planner_paused = True
                return {"enabled": True}
            if self._rx_scope is None:
                if mode in ("CLOSE", "DISABLE"):
                    return {"disabled": True, "active": False}
                raise CollisionRuntimeError("RX scope absent")
            if self._rx_scope.owner != owner:
                raise CollisionRuntimeError("RX scope owner mismatch")
            result = getattr(self._rx_scope, mode.lower())()
            if mode == "DISABLE" and result.get("disabled"):
                self._rx_scope = None
            return _jsonable_tuning(result)

    def clock_map(self, *, direction, timestamp_ns):
        if type(timestamp_ns) is not int or timestamp_ns < 0 or direction not in ("HW_TO_UTC", "UTC_TO_HW"):
            raise ValueError("invalid clock mapping request")
        bridge = self._radio().bridge
        method = bridge.hardware_ns_to_utc_label if direction == "HW_TO_UTC" else bridge.utc_label_to_hardware_ns
        value = method(timestamp_ns)
        if value is None:
            raise PocDeviceUnavailable("active bridge unavailable")
        return {"timestamp_ns": int(value), "clock_id": bridge.clock_id, "generation": bridge.generation}

    def radio_state(self) -> dict:
        """scheduler·arbiter·readiness가 쓰는 **실 장치 상태**. 없는 값은 None으로 둔다."""

        radio = self._radio()
        worker = self._worker()
        bridge = getattr(radio, "bridge", None)
        runtime_state = getattr(worker, "poc_runtime_state", None)
        owned = runtime_state() if callable(runtime_state) else None
        if owned is not None and (
                not isinstance(owned, dict)
                or type(owned.get("hardware_time_ns")) is not int
                or not isinstance(owned.get("tx_muted"), list)
                or len(owned["tx_muted"]) != 2
                or any(type(v) is not bool for v in owned["tx_muted"])):
            raise PocDeviceUnavailable("invalid device-owner state")

        def _call(name):
            method = getattr(worker, name, None)
            if not callable(method):
                return None
            try:
                return method()
            except Exception:  # noqa: BLE001 - 조회 실패를 값으로 위장하지 않는다
                return None

        windows = {}
        with radio._condition:
            for child in radio._rx_children.values():
                if child.terminal:
                    continue
                rows = windows.setdefault(str(child.lane_index), [])
                rows.append({"start_ns": child.hardware_start_ns, "count": child.expected_sample_count,
                             "channel": child.request.channel.name, "consumed": False})
            windows = {lane: rows[-_MAX_RX_WINDOWS:] for lane, rows in windows.items()}

        muted = owned["tx_muted"] if owned is not None else getattr(worker, "_tx_muted", None)
        if muted is None:
            muted = getattr(worker, "tx_muted", None)
        muted_list = None
        if muted is not None:
            values = muted.values() if hasattr(muted, "values") else muted
            try:
                muted_list = [bool(v) for v in values]
            except TypeError:
                muted_list = None

        # Lifecycle already verifies tuning. No extra UHD readback per state poll.
        tuning = (self._rx_scope.readback if self._rx_scope is not None and self._rx_scope.touched else None) or {}
        lane_centers = tuning.get("lane_center_hz") or getattr(worker, "lane_rx_center_hz", None) or {}
        return {
            "endpoint_id": self.endpoint_id,
            "hardware_time_ns": owned["hardware_time_ns"] if owned is not None else _call("hardware_time_ns"),
            "tx_lo_hz": owned.get("tx_lo_hz") if owned is not None else getattr(worker, "tx_lo_hz", None),
            "rx_lo_hz": owned.get("rx_lo_hz") if owned is not None else (getattr(worker, "rx_lo_hz", None) or getattr(worker, "_rx_lo_frequency_hz", None) or lane_centers.get(0)),
            "rx_rf_lo_hz": tuning.get("rx_lo_hz"),
            "lane_rx_center_hz": {str(int(k)): int(v) for k, v in lane_centers.items()},
            "tx_muted": muted_list,
            "rx_continuous": bool(owned.get("rx_continuous")) if owned is not None else bool(getattr(worker, "rx_continuous", False)),
            "rx_continuous_since_ns": owned.get("rx_continuous_since_ns") if owned is not None else _call("rx_continuous_since_ns"),
            "device_state_basis": owned.get("state_basis") if owned is not None else "IN_PROCESS_WORKER",
            "tx_lo_basis": owned.get("tx_lo_basis") if owned is not None else "IN_PROCESS_WORKER",
            "rx_windows": windows,
            # 두 clock id는 **다른 것**이다: hardware(장치 시간축)와 bridge(UTC label 축).
            # 둘을 섞으면 좌표 결속 증거가 무의미해지므로 각각 싣는다.
            "hardware_clock_id": getattr(radio, "hardware_clock_id", None),
            "clock_id": getattr(bridge, "clock_id", None),
            "bridge_generation": getattr(bridge, "generation", None),
            "bridge_state": getattr(bridge, "state", None),
            "correction_revision": getattr(bridge, "correction_revision", None),
            "bridge_locked": getattr(bridge, "clock_id", None) is not None,
        }

    def wired_broadcast_optin(self, *, enabled) -> dict:
        """정상 송신의 공유매체 fan-out opt-in을 session 범위로 켜고 끈다(P08, §4.1).

        `enabled`가 None이면 조회만 한다. 켜면 이 endpoint의 **정상 service 송신**도 송신자의 두
        물리 경로로 방사된다(논리 주소·정상 tuning 행렬 불변). 이미 승격된 route는 바뀌지 않으므로
        진행 중인 burst의 계획을 도중에 흔들지 않는다.
        """

        radio = self._radio()
        sidecar = getattr(radio, "_sidecar", None)
        if sidecar is None:
            raise PocDeviceUnavailable("radio adapter에 route sidecar가 없습니다")
        toggle = getattr(sidecar, "set_wired_broadcast_optin", None)
        if not callable(toggle):
            raise PocDeviceUnavailable("설치가 공유매체 opt-in을 제공하지 않습니다")
        if enabled is None:
            return {"endpoint_id": self.endpoint_id,
                    "enabled": bool(sidecar.wired_broadcast_optin), "previous": None}
        previous = toggle(bool(enabled))
        return {"endpoint_id": self.endpoint_id,
                "enabled": bool(sidecar.wired_broadcast_optin),
                "previous": bool(previous)}

    def mac_transmit_slots(self, *, channel_name: str, from_hw_ns: int,
                           limit: int = 8) -> dict:
        """**실 MAC slot map**에서 이 endpoint가 송신할 수 있는 다음 slot들을 돌려준다(P03).

        Stage 8 scheduler가 device slot 격자(8,192 표본 등간격)로 시각을 고르면 MAC이 승인하지 않은
        구간에 송신을 요청할 수 있다. 여기서는 **활성 TBB의 slot map**(없으면 bootstrap)에서
        `transmit_enabled`인 slot만 모아 준다 — 즉 "MAC이 이 채널에서 송신을 허용한 시각"이다.
        slot을 새로 만들지 않고 계획표를 읽기만 한다.

        시각은 두 좌표를 **분리해** 싣는다: MAC의 UTC label과 그것을 bridge로 환산한 device
        hardware ns. 환산이 불가능하면(bridge 미잠금) 그 slot은 싣지 않는다 — 장치가 쓸 수 없는
        시각을 후보로 주지 않는다.
        """

        if channel_name not in CHANNEL_NAMES:
            raise ValueError(f"허용되지 않은 채널 이름: {channel_name!r}")
        from ...mac.access import enumerate_function_slots
        from ...mac.tbb import SlotFunction
        from ...mac.tdma import FRAME_NANOSECONDS
        from ...mac.resources import externally_reserved

        mac = getattr(getattr(self._endpoint, "stage4", None), "mac", None)
        # `Stage4Mac`은 port 계약이고 slot map 소유자는 그 뒤의 `MacCoordinator`다.
        coordinator = getattr(mac, "coordinator", None)
        state = getattr(mac, "state", None)
        if coordinator is None or state is None:
            raise PocDeviceUnavailable("endpoint에서 MAC slot map에 닿을 수 없습니다")
        slot_map = coordinator.active_slot_map(state)
        radio = self._radio()
        bridge = getattr(radio, "bridge", None)
        to_utc = getattr(bridge, "hardware_ns_to_utc_label", None)
        to_hw = getattr(bridge, "utc_label_to_hardware_ns", None)
        if not callable(to_utc) or not callable(to_hw):
            raise PocDeviceUnavailable("host time bridge가 없어 MAC slot 시각을 환산할 수 없습니다")
        from_utc = to_utc(int(from_hw_ns))
        if from_utc is None:
            raise PocDeviceUnavailable("host time bridge가 잠겨 있지 않습니다")

        channel = PhysicalChannel(channel_name)
        candidates = []
        # 두 frame 안에서 찾는다(MAC 자신의 탐색 상한과 같은 폭). 없으면 빈 목록이다.
        for function in SlotFunction:
            try:
                found = enumerate_function_slots(
                    slot_map=slot_map, function=function, physical_channel=channel,
                    from_utc_nanoseconds=int(from_utc),
                    until_utc_nanoseconds=int(from_utc) + 2 * FRAME_NANOSECONDS,
                    transmit=True)
            except Exception:  # noqa: BLE001 - 한 function 실패가 전체를 막지 않는다
                continue
            for item in found:
                if externally_reserved(state.lc_allocations, item.logical_channel,
                                       item.frame_ordinal):
                    continue
                hardware_ns = to_hw(int(item.start_nanoseconds))
                if hardware_ns is None:
                    continue        # 장치 좌표로 환산 불가 → 후보로 주지 않는다
                candidates.append({
                    "function": function.value,
                    "tdma_channel": int(item.tdma_channel),
                    "slot_in_frame": int(item.slot_in_frame),
                    "frame_ordinal": int(item.frame_ordinal),
                    "logical_channel": int(item.logical_channel),
                    "start_utc_ns": int(item.start_nanoseconds),
                    "start_hw_ns": int(hardware_ns),
                    "duration_ns": int(item.duration_nanoseconds),
                })
        candidates.sort(key=lambda entry: entry["start_hw_ns"])
        return {
            "channel_name": channel_name,
            "from_hw_ns": int(from_hw_ns),
            "slot_map_id": str(getattr(slot_map, "map_id", "")),
            "slots": candidates[:max(1, int(limit))],
        }

    def tx_burst_state(self, burst_id: str):
        """권위 TX 원장 조회(읽기 전용). 원장에 없으면 None — 성공으로 추정하지 않는다."""

        radio = self._radio()
        query = getattr(radio, "tx_burst_state", None)
        if not callable(query):
            raise PocDeviceUnavailable("radio adapter가 TX 원장 조회를 제공하지 않습니다")
        snapshot = query(str(burst_id))
        if snapshot is None:
            return None
        result = {
            "terminal": bool(snapshot.get("terminal")),
            "children": {str(int(lane)): str(state)
                         for lane, state in (snapshot.get("children") or {}).items()},
        }
        # 권위 원장이 수락 시점에 남긴 lead 관측을 그대로 나른다. 여기서 만들거나 해석하지
        # 않으며, 없으면 키 자체를 싣지 않는다(0으로 위장 금지). 새 RPC를 만들지 않는다 —
        # terminal 판정이 이미 부르는 이 조회의 응답에 실려 온다.
        lead = snapshot.get("lead")
        if isinstance(lead, dict):
            carried = {key: int(lead[key])
                       for key in ("required_lead_ns", "remaining_lead_ns", "margin_ns")
                       if isinstance(lead.get(key), int) and not isinstance(lead[key], bool)}
            if carried:
                result["lead"] = carried
        return result

    # --- 송신 ----------------------------------------------------------------

    def schedule_group_tx(self, *, burst_id: str, channel_name: str, transmit_at_hw_ns: int,
                          samples: bytes, destination_station_id: int,
                          request_digest: str, wire_packet_digest=None, collision_session_id=None) -> dict:
        """공유매체 fan-out route를 결속하고 **권위 `schedule_tx`** 한 번으로 제출한다.

        여기서 파형을 만들지 않는다 — supervisor가 frozen PHY로 만든 device-rate 표본을 그대로
        싣는다(그 표본이 실제로 나가는지가 P05 증거다). 시각 변환(hardware→UTC label)은 bridge를
        소유한 이 프로세스가 한다(모델 시각을 장치에 직접 밀어넣지 않는다).
        """

        if channel_name not in CHANNEL_NAMES:
            raise ValueError(f"허용되지 않은 채널 이름: {channel_name!r}")
        data = bytes(samples)
        if len(data) != DEVICE_SLOT_SAMPLES * _CF32LE_BYTES_PER_SAMPLE:
            raise ValueError(
                f"device burst 크기 위반: {len(data)}B "
                f"!= {DEVICE_SLOT_SAMPLES * _CF32LE_BYTES_PER_SAMPLE}B")

        radio = self._radio()
        if collision_session_id is not None or self._rx_scope is not None:
            gate = self._endpoint.stage4.collision_gate
            if (gate is None or gate.session_id != collision_session_id or gate.fault
                    or not gate.accepting):
                raise PocDeviceUnavailable("collision session is not admitting PoC TX")
            if (not isinstance(wire_packet_digest, str) or len(wire_packet_digest) != 64
                    or any(c not in "0123456789abcdef" for c in wire_packet_digest)):
                raise ValueError("wire packet SHA-256 required before TX")
        sidecar = getattr(radio, "_sidecar", None)
        if sidecar is None:
            raise PocDeviceUnavailable("radio adapter에 route sidecar가 없습니다")
        bind = getattr(sidecar, "bind_broadcast_fanout_burst", None)
        if not callable(bind):
            raise PocDeviceUnavailable("route sidecar가 공유매체 fan-out 결속을 제공하지 않습니다")

        bridge = getattr(radio, "bridge", None)
        # route 결속 **전에** bridge를 확인한다(bridge 미잠금이면 route를 결속하지 않고 거부).
        utc_ns = utc_label_for_hardware_ns(bridge, transmit_at_hw_ns)

        source_station = self._station_id_by_endpoint.get(self.endpoint_id)
        if source_station is None:
            raise PocDeviceUnavailable(f"station id를 알 수 없습니다: {self.endpoint_id}")
        bound = bind(str(burst_id), source_station_id=int(source_station),
                     destination_station_id=int(destination_station_id),
                     request_digest=str(request_digest or ""), wire_packet_digest=wire_packet_digest)
        if getattr(bound, "is_err", False):
            # schedule_tx 거부와 같은 이유로 `details`를 보존한다(여기에 stage7 private code가 있다).
            bind_details = getattr(getattr(bound, "error", None), "details", None)
            return {"accepted": False, "burst_id": str(burst_id),
                    "error_details": (bind_details.as_dict()
                                      if hasattr(bind_details, "as_dict") else None),
                    "reason": "ROUTE_BIND_REJECTED",
                    "detail": str(getattr(bound, "error", bound))[:200]}

        # 요청 구성은 RF-off 세션 sink와 같은 조립기(`build_poc_tx_request`)를 쓴다 — clock id는
        # bridge 소유, 표본은 device-rate 한 slot. 위에서 미리 변환한 UTC label을 그대로 싣는다.
        request, _utc = build_poc_tx_request(
            bridge=bridge, burst_id=burst_id, channel_name=channel_name,
            transmit_at_hw_ns=transmit_at_hw_ns, samples=data, transmit_at_utc_ns=utc_ns)
        result = radio.schedule_tx(request)      # 권위 경로(lead·retune·충돌·원장)
        if getattr(result, "is_err", False):
            error = getattr(result, "error", None)
            # `code`만 남기면 왜 거부됐는지 사후에 가릴 수 없다 — 실물 C에서 실제로 그랬다
            # (`RADIO_LATE_REQUEST`만 남고 `required_lead_ns`는 사라졌다). `Details`는 스스로
            # payload·secret 키를 금지하고 크기 상한을 강제하므로 그대로 실어도 안전하다.
            # 여기서 값을 만들거나 해석하지 않는다 — 장치가 준 사실만 나른다.
            details = getattr(error, "details", None)
            return {"accepted": False, "burst_id": str(burst_id),
                    "reason": "SCHEDULE_TX_REJECTED",
                    "detail": str(getattr(error, "code", error))[:200],
                    "error_details": details.as_dict() if hasattr(details, "as_dict") else None}
        return {"accepted": True, "burst_id": str(burst_id),
                "transmit_at_utc_ns": int(utc_ns), "reason": None, "detail": None}

    # --- tuning (P07) ---------------------------------------------------------

    def tuning(self, *, mode: str, rx_lo_hz=None, lane_dsp_hz=None,
               analog_bandwidth_hz=None, snapshot=None) -> dict:
        """manual common-LO tuning 적용/조회/복구를 launcher 소유 worker에 건다."""

        if mode in ("APPLY", "RESTORE", "apply", "restore") and self._endpoint.stage4.collision_gate is not None:
            raise PocDeviceUnavailable("close collision RX scope before changing its tuning binding")
        worker = self._worker()
        if mode == "READ":
            read = getattr(worker, "read_tuning", None)
            if not callable(read):
                raise PocDeviceUnavailable("worker가 read_tuning을 제공하지 않습니다")
            return _jsonable_tuning(read())
        if mode == "APPLY":
            apply_fn = getattr(worker, "apply_manual_tuning", None)
            if not callable(apply_fn):
                raise PocDeviceUnavailable(
                    "설치가 manual common-LO tuning(apply_manual_tuning)을 제공하지 않습니다")
            offsets = {int(lane): int(value) for lane, value in dict(lane_dsp_hz or {}).items()}
            return _jsonable_tuning(apply_fn(int(rx_lo_hz), offsets, int(analog_bandwidth_hz)))
        if mode == "RESTORE":
            restore = getattr(worker, "restore_tuning", None)
            if not callable(restore):
                raise PocDeviceUnavailable("worker가 restore_tuning을 제공하지 않습니다")
            return _jsonable_tuning(restore(_tuning_from_wire(snapshot or {})))
        raise ValueError(f"알 수 없는 tuning mode: {mode!r}")


def _jsonable_tuning(value):
    """tuning readback을 canonical JSON 값으로 만든다(int 키 → str, float 금지)."""

    if isinstance(value, dict):
        return {str(key): _jsonable_tuning(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_tuning(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    return int(value)


def _tuning_from_wire(value: dict) -> dict:
    """wire의 str 키 lane map을 worker가 쓰는 int 키로 되돌린다."""

    out = dict(value)
    for key in ("lane_dsp_hz", "lane_center_hz", "lane_rf_lo_hz"):
        nested = out.get(key)
        if isinstance(nested, dict):
            out[key] = {int(lane): int(item) for lane, item in nested.items()
                        if item is not None}
    return out


__all__ = ("Stage7PocDeviceSeam", "PocDeviceUnavailable", "CHANNEL_NAMES",
           "DEVICE_SAMPLE_RATE_HZ", "DEVICE_SLOT_SAMPLES")
