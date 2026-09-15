"""Stage 7 software 통합 하니스 (`stage7_b210_system_baseline.md` §7 소프트웨어 검증).

실제 UHD 없이 C1..C6 conducted 토폴로지를 결정적으로 흉내내는 fake dual-lane
worker/매질과 3-endpoint software group builder를 제공한다. frozen protocol
경로(Service→Presentation→Link→MAC→PHY→Radio)는 실제 코드가 수행하고, 전파
구간만 sample bytes 전달로 대체한다.

이 매질 PASS는 hardware P0~P3 판정을 대체하지 않는다 (software-only 판정 전용).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from vdeter.common.identifiers import DeterministicIdSource, StationId
from vdeter.common.random_source import SeededRandomSource
from vdeter.common.time import TestTimeSource
from vdeter.b210_system.configuration import ConfigurationLoader
from vdeter.b210_system.coordinator import ResolvedSystemPlan, RuntimeCoordinator
from vdeter.b210_system.ipc import ENDPOINT_IDS
from vdeter.b210_system.records import TbbActiveRecord
from vdeter.b210_system.runtime_client import EndpointRuntimeClient
from vdeter.b210_system.time_bridge import AffineCoefficients, HostUtcAnchor
from vdeter.b210_system.wiring import build_b210_endpoint

from tests.support.stage4 import (
    RUN_START_MONOTONIC_NANOSECONDS,
    RUN_START_UTC_NANOSECONDS,
    build_test_signer,
    crypto_dependencies,
    deployment_profile,
    default_type0_override,
)
from tests.unit.test_stage7_configuration import (
    DATA_PROFILE_17,
    DATA_PROFILE_19,
    build_mass_b_source,
    build_roc_source,
    build_source,
)

def stage7_test_signer():
    """TEST signer. frozen TBB crypto provider 요건을 만족하지 못하는 환경
    (예: Pi target의 cryptography 41 — `VDETER-TBB-SIG-v1`은 major ≥50 요구,
    `ecdsa_deterministic` kwarg 부재)에서는 `unittest.SkipTest`로 정직하게
    건너뛴다. silent fallback으로 frozen 계약을 우회하지 않는다.
    """

    import unittest as _unittest

    try:
        return build_test_signer()
    except TypeError as error:
        raise _unittest.SkipTest(
            "frozen TBB crypto provider 요건 미충족 환경: "
            f"deterministic ECDSA 미지원 cryptography ({error})"
        ) from error


SERIAL_TO_ENDPOINT = {"34D03DD": "MASS-A", "3515425": "MASS-B", "34D03E8": "ROC"}
ENDPOINT_TO_SERIAL = {value: key for key, value in SERIAL_TO_ENDPOINT.items()}
STATION_BY_ENDPOINT = {"MASS-A": 111111111, "MASS-B": 333333333, "ROC": 222222222}

#: (tx_serial, lane) → (rx_serial, lane, path_id) — §5 C1..C6.
CONDUCTED_PATHS = {
    ("34D03E8", 0): ("34D03DD", 0, "C1"),
    ("34D03E8", 1): ("3515425", 0, "C3"),
    ("34D03DD", 0): ("34D03E8", 0, "C2"),
    ("3515425", 0): ("34D03E8", 1, "C4"),
    ("34D03DD", 1): ("3515425", 1, "C5"),
    ("3515425", 1): ("34D03DD", 1, "C6"),
}

#: worker hardware clock의 host-raw 대비 offset (t0): raw = hardware + t0.
WORKER_T0_NS = {
    "34D03DD": 3_000_000_000,
    "3515425": 7_000_000_000,
    "34D03E8": 11_000_000_000,
}

DEVICE_RATE = 307_200
SLOT_SAMPLES = 8_192
SLOT_BYTES = SLOT_SAMPLES * 8
SLOT_DURATION_NS = (SLOT_SAMPLES * 1_000_000_000 + DEVICE_RATE - 1) // DEVICE_RATE


class Stage7FakeMedium:
    """C1..C6 directed 전달과 worker registry를 소유한다."""

    def __init__(self, host_raw_clock) -> None:
        self.host_raw_clock = host_raw_clock
        self.workers = {}
        self.delivery_log = []
        #: TC-STAGE7-012: (path_id, data, raw_ns) -> True면 이 burst를 유실시킨다.
        self.drop_filter = None
        self.dropped_log = []
        self.tune_mismatch_log = []

    def register(self, worker) -> None:
        self.workers[worker.serial] = worker

    def transmit(self, sender, lane: int, data: bytes, hardware_time_ns: int) -> None:
        raw_ns = hardware_time_ns + WORKER_T0_NS[sender.serial]
        path = CONDUCTED_PATHS.get((sender.serial, lane))
        if path is None:
            return
        rx_serial, rx_lane, path_id = path
        receiver = self.workers.get(rx_serial)
        if receiver is None:
            return
        # 수신 lane의 **실효 중심 주파수**로 정합을 본다. manual common-LO tuning이 걸려 있으면
        # lane마다 다르므로(effective = rf_lo - dsp), 단일 rx_lo_hz로 판정하면 dual-channel 수신을
        # 재현할 수 없다. manual tuning을 **적용한 적이 없으면**(rx_rf_lo_hz is None) 종전대로
        # rx_lo_hz 하나로 판정한다 — 기존 단일 LO 설치 동작을 그대로 보존한다.
        rx_center = receiver.rx_lo_hz
        if getattr(receiver, "rx_rf_lo_hz", None) is not None:
            rx_center = (receiver.lane_rx_center_hz or {}).get(rx_lane, rx_center)
        if sender.tx_lo_hz != rx_center:
            # tune 불일치 경로는 물리적으로 복원되지 않는다 (진단용 기록만 남긴다).
            self.tune_mismatch_log.append(
                {
                    "path_id": path_id,
                    "tx_serial": sender.serial,
                    "tx_lo_hz": sender.tx_lo_hz,
                    "rx_lo_hz": rx_center,
                    "rx_lane": rx_lane,
                    "raw_ns": raw_ns,
                    "bytes": len(data),
                }
            )
            return
        if self.drop_filter is not None and self.drop_filter(path_id, data, raw_ns):
            self.dropped_log.append(
                {"path_id": path_id, "raw_ns": raw_ns, "bytes": len(data)}
            )
            return
        receiver_hw_ns = raw_ns - WORKER_T0_NS[rx_serial]
        receiver.pending_bursts.setdefault(rx_lane, []).append(
            {"hardware_time_ns": receiver_hw_ns, "data": data, "path_id": path_id,
             # 합성 경계(§7.1)가 채널을 구분하고 같은 도달을 두 번 합산하지 않게 하는 값.
             "tx_lo_hz": sender.tx_lo_hz,
             "arrival_id": f"{path_id}-{raw_ns}-{len(self.delivery_log)}"}
        )
        self.delivery_log.append(
            {
                "path_id": path_id,
                "tx_serial": sender.serial,
                "rx_serial": rx_serial,
                "raw_ns": raw_ns,
                "bytes": len(data),
                "digest": hashlib.sha256(data).hexdigest(),
            }
        )


class Stage7FakeMediumWorker:
    """medium에 결속된 결정적 dual-lane worker double."""

    def __init__(self, device_selector, cpu_format, otw_format, *, medium: Stage7FakeMedium):
        self.serial = device_selector.split("=", 1)[1]
        self.medium = medium
        self.tx_lo_hz = None
        self.rx_lo_hz = None
        self.pending_bursts = {0: [], 1: []}
        self.rx_windows = {0: [], 1: []}
        self.rx_continuous = False
        self.rx_continuous_since = None
        #: lane별 마지막 창 합성 결과(§7.1 모델 증거). 시험이 겹침·clip을 확인한다.
        self.last_rx_composite = {0: None, 1: None}
        self.async_events = []
        self.closed = False
        self.tx_muted = [False, False]
        #: manual RX tuning 모델 상태(실 worker의 apply_manual_tuning/read_tuning/restore_tuning).
        self.rx_rf_lo_hz = None
        self.lane_dsp_hz = {}
        self.lane_rx_center_hz = {}
        self.analog_bandwidth_hz = None
        #: 시험용 장치 결함 주입(기본 없음) — 적용 실패·복구 실패·LO 오차·BW 상한.
        self.tuning_fault = None
        self.restore_fault = None
        self.tuning_lo_error_hz = 0
        self.tuning_bandwidth_cap_hz = None
        medium.register(self)

    # --- factory surface -----------------------------------------------------

    @classmethod
    def factory(cls, medium: Stage7FakeMedium):
        def build(device_selector, cpu_format, otw_format, **_kwargs):
            return cls(device_selector, cpu_format, otw_format, medium=medium)

        build.discover = lambda selector: (
            (selector.split("=", 1)[1],)
            if selector.split("=", 1)[1] in SERIAL_TO_ENDPOINT
            else ()
        )
        return build

    @staticmethod
    def discover(device_selector):
        serial = device_selector.split("=", 1)[1]
        return (serial,) if serial in SERIAL_TO_ENDPOINT else ()

    # --- worker RPC surface ---------------------------------------------------

    def configure_dual(self, plan):
        self.plan = plan
        self.tx_lo_hz = plan["tx_lo_hz"]
        self.rx_lo_hz = plan["rx_lo_hz"]
        # 시작 tuning도 모델 상태에 반영한다(manual 적용 전 read_tuning/복구 기준점).
        self.analog_bandwidth_hz = int(plan["analog_bandwidth_hz"])
        self.lane_rx_center_hz = {lane: int(plan["rx_lo_hz"]) for lane in (0, 1)}
        self.rx_rf_lo_hz = None
        self.lane_dsp_hz = {}
        lanes = {
            lane: {
                "tx_center_frequency_hz": plan["tx_lo_hz"],
                "rx_center_frequency_hz": plan["rx_lo_hz"],
                "tx_sample_rate_hz": plan["sample_rate_hz"],
                "rx_sample_rate_hz": plan["sample_rate_hz"],
                "tx_gain_db": plan["tx_gain_db"][int(lane)],
                "rx_gain_db": plan["rx_gain_db"][int(lane)],
                "tx_bandwidth_hz": plan["analog_bandwidth_hz"],
                "rx_bandwidth_hz": plan["analog_bandwidth_hz"],
                "tx_antenna": "TX/RX",
                "rx_antenna": "RX2",
            }
            for lane in ("0", "1")
        }
        return {
            "serial": self.serial,
            "sink_serial": self.serial,
            "same_timebase": True,
            "master_clock_rate_hz": plan["master_clock_rate_hz"],
            "sink_master_clock_rate_hz": plan["master_clock_rate_hz"],
            "clock_source": "internal",
            "time_source": "internal",
            "sink_clock_source": "internal",
            "sink_time_source": "internal",
            "hardware_time_reset": True,
            "cpu_format": plan["cpu_format"],
            "otw_format": plan["otw_format"],
            "tx_lo_hz": plan["tx_lo_hz"],
            "rx_lo_hz": plan["rx_lo_hz"],
            "lanes": lanes,
        }

    def connect(self):
        pass

    def start(self):
        pass

    def start_rx_continuous(self, start_hardware_time_ns=None):
        """Mirror the B210 worker's service-mode continuous RX contract.

        실 worker와 같이 **언제부터** 연속 RX가 유효한지 기록한다(실 worker는 dual-channel streamer
        정렬을 위해 now+200 ms에 시작한다). 상위 readiness가 "켜져 있다"가 아니라 "예약 시각이 시작
        이후"를 확인할 수 있어야 한다.
        """

        self.rx_continuous = True
        self.rx_continuous_since = (int(start_hardware_time_ns)
                                    if start_hardware_time_ns is not None
                                    else int(self.hardware_time_ns()) + 200_000_000)
        return True

    def rx_continuous_since_ns(self):
        return self.rx_continuous_since if self.rx_continuous else None

    def stop_rx_continuous(self):
        self.rx_continuous = False
        self.rx_continuous_since = None
        #: lane별 마지막 창 합성 결과(§7.1 모델 증거). 시험이 겹침·clip을 확인한다.
        self.last_rx_composite = {0: None, 1: None}
        return True

    def hardware_time_ns(self):
        return int(self.medium.host_raw_clock()) - WORKER_T0_NS[self.serial]

    def retune_tx_lo(self, target_hz, command_time_ns):
        self.tx_lo_hz = target_hz

    # --- manual RX tuning model (실 worker와 같은 surface) -----------------------
    #
    # echo가 아니라 **장치 모델**이다: analog bandwidth는 장치가 고를 수 있는 격자로 양자화되고,
    # lane별 실효 중심은 승인된 규약 effective = rf_lo - dsp로 계산된다. 따라서 계획과 다른 값이
    # 실제로 나올 수 있고 readback 불일치가 시험에서 재현된다(무조건 통과하는 echo 금지).

    #: B210 analog filter가 고르는 최소 격자(모델). 요청보다 좁아지지 않도록 **올림**한다.
    TUNING_BANDWIDTH_STEP_HZ = 100_000

    def _quantize_bandwidth(self, requested_hz):
        step = self.TUNING_BANDWIDTH_STEP_HZ
        return ((int(requested_hz) + step - 1) // step) * step

    def read_tuning(self):
        return {
            "lane_center_hz": dict(self.lane_rx_center_hz),
            "analog_bandwidth_hz": self.analog_bandwidth_hz,
            **({"rx_lo_hz": self.rx_rf_lo_hz,
                "lane_dsp_hz": dict(self.lane_dsp_hz)}
               if self.rx_rf_lo_hz is not None else {}),
        }

    def apply_manual_tuning(self, rx_lo_hz, lane_dsp_hz, analog_bandwidth_hz):
        if self.tuning_fault is not None:
            raise RuntimeError(self.tuning_fault)
        previous = self.read_tuning()
        offsets = {int(lane): int(value) for lane, value in dict(lane_dsp_hz).items()}
        if set(offsets) != {0, 1}:
            raise ValueError(f"lane DSP offset은 lane 0·1 모두 필요합니다: {sorted(offsets)}")
        bandwidth = self._quantize_bandwidth(analog_bandwidth_hz)
        if self.tuning_bandwidth_cap_hz is not None:
            bandwidth = min(bandwidth, int(self.tuning_bandwidth_cap_hz))
        self.rx_rf_lo_hz = int(rx_lo_hz) + int(self.tuning_lo_error_hz)
        self.lane_dsp_hz = dict(offsets)
        self.analog_bandwidth_hz = bandwidth
        # 승인된 규약: effective = rf_lo - dsp (WSM-G1 실측).
        self.lane_rx_center_hz = {lane: self.rx_rf_lo_hz - offsets[lane] for lane in (0, 1)}
        # 단일 값만 읽는 기존 소비자에게는 lane 0(lower) 실효 중심을 보인다.
        self.rx_lo_hz = self.lane_rx_center_hz[0]
        return {
            "rx_lo_hz": self.rx_rf_lo_hz,
            "lane_dsp_hz": dict(self.lane_dsp_hz),
            "analog_bandwidth_hz": self.analog_bandwidth_hz,
            "lane_rf_lo_hz": {lane: self.rx_rf_lo_hz for lane in (0, 1)},
            "lane_center_hz": dict(self.lane_rx_center_hz),
            "previous": previous,
        }

    def restore_tuning(self, snapshot):
        if self.restore_fault is not None:
            raise RuntimeError(self.restore_fault)
        lane_dsp = snapshot.get("lane_dsp_hz")
        rx_lo = snapshot.get("rx_lo_hz")
        bandwidth = snapshot.get("analog_bandwidth_hz")
        if lane_dsp is not None and rx_lo is not None and bandwidth is not None:
            saved = self.tuning_lo_error_hz
            self.tuning_lo_error_hz = 0          # 복구는 기록된 실측값 그대로 되돌린다
            try:
                return self.apply_manual_tuning(rx_lo, lane_dsp, bandwidth)
            finally:
                self.tuning_lo_error_hz = saved
        centers = snapshot.get("lane_center_hz") or {}
        self.rx_rf_lo_hz = None
        self.lane_dsp_hz = {}
        if bandwidth is not None:
            self.analog_bandwidth_hz = int(bandwidth)
        self.lane_rx_center_hz = {int(lane): value for lane, value in centers.items()
                                  if value is not None}
        self.rx_lo_hz = self.lane_rx_center_hz.get(0)
        return self.read_tuning()

    def set_rx_gain(self, lane, gain_db):
        return float(gain_db)

    def set_tx_gain(self, lane, gain_db):
        return float(gain_db)

    def set_tx_mute(self, lane, muted):
        self.tx_muted[lane] = bool(muted)

    def submit_tx_group(self, bursts):
        for lane, data, hardware_time_ns in bursts:
            if self.tx_muted[lane]:
                raise RuntimeError(f"lane {lane} TX gate is inhibited")
            self.medium.transmit(self, lane, data, hardware_time_ns)
            end_ns = hardware_time_ns + SLOT_DURATION_NS
            self.async_events.append(
                {"outcome": "BURST_ACK", "hardware_time_ns": hardware_time_ns,
                 "due_ns": end_ns, "channel": lane}
            )

    def submit_rx_lane(self, lane, hardware_time_ns, sample_count):
        self.rx_windows[lane].append(
            {"start_ns": hardware_time_ns, "count": sample_count, "consumed": False}
        )

    def register_rx_window_lane(self, lane, hardware_time_ns, sample_count):
        # 실구현 동등성 (0.9.22, R2 감사 M-5): 실제 assembler는 같은 창의
        # 중복 등록을 거부한다 — fake의 무조건 관용은 중복 등록 결함을
        # software 시험에서 은폐한다(svcdbg29 부류).
        for window in self.rx_windows[lane]:
            if window["start_ns"] == hardware_time_ns and not window["consumed"]:
                raise ValueError(
                    f"duplicate rx window: lane={lane} start={hardware_time_ns}"
                )
        self.submit_rx_lane(lane, hardware_time_ns, sample_count)
        return True

    def unregister_rx_lane(self, lane, hardware_time_ns=None):
        for window in self.rx_windows[lane]:
            if hardware_time_ns is None or window["start_ns"] == hardware_time_ns:
                window["consumed"] = True
        return True

    def read_rx_cf32_lane(self, lane, sample_count, allow_partial=False, expected_hardware_time_ns=None):
        now = self.hardware_time_ns()
        for window in self.rx_windows[lane]:
            if window["consumed"]:
                continue
            if expected_hardware_time_ns is not None and window["start_ns"] != expected_hardware_time_ns:
                continue
            end_ns = window["start_ns"] + (
                window["count"] * 1_000_000_000 + DEVICE_RATE - 1
            ) // DEVICE_RATE
            if now < end_ns + 20_000_000:
                return None
            window["consumed"] = True
            # P12: 이 창에 도달한 **모든** 입력을 수신기 좌표로 정렬해 한 번 합성한다(§7.1).
            # 이전에는 정렬이 맞는 **첫** burst 하나만 쓰고 나머지를 조용히 버려, 겹침이 있어도
            # 충돌이 재현되지 않았고 정상 TX와 PoC TX가 합성 입력에서 빠졌다.
            composite = self._compose_window(lane, window)
            self.last_rx_composite[lane] = composite
            return {
                "segments": ((composite.data, window["start_ns"], False),),
                "overflow_samples": 0,
                "trailing_gap": False,
            }
        return None

    def _tuned_center_hz(self, lane):
        """이 lane이 실제로 듣고 있는 실효 중심. manual common-LO면 lane마다 다르다."""
        if getattr(self, "rx_rf_lo_hz", None) is not None:
            return (self.lane_rx_center_hz or {}).get(lane)
        return self.rx_lo_hz

    def _compose_window(self, lane, window):
        """창 하나의 합성 입력을 만든다 — 권위 합성기(§7.1)를 그대로 쓴다."""
        from vdeter.poc.api.types import PhysicalChannel
        from vdeter.poc.shared_medium.rx_boundary import RxArrival, compose_rx_window

        # 합성기에 넘기는 channel은 **묶음 표식**이다(측정값이 아니다). 실제 채널 구분은 아래
        # `tuned_center_hz` 일치 검사가 한다. 이 토폴로지에서 lane 0/1은 lower/upper에 고정이다.
        channel = PhysicalChannel.LOWER if lane == 0 else PhysicalChannel.UPPER

        span_ns = (window["count"] * 1_000_000_000 + DEVICE_RATE - 1) // DEVICE_RATE
        start, end = window["start_ns"], window["start_ns"] + span_ns
        arrivals, remaining = [], []
        for burst in self.pending_bursts[lane]:
            burst_span = (len(burst["data"]) // 8 * 1_000_000_000 + DEVICE_RATE - 1) // DEVICE_RATE
            if burst["hardware_time_ns"] + burst_span <= start or burst["hardware_time_ns"] >= end:
                remaining.append(burst)       # 이 창과 무관 — 다음 창을 위해 남긴다
                continue
            arrivals.append(RxArrival(
                arrival_id=burst.get("arrival_id") or f"{burst['path_id']}-{burst['hardware_time_ns']}",
                receiver_hw_ns=int(burst["hardware_time_ns"]),
                effective_center_hz=int(burst.get("tx_lo_hz") or 0),
                data=burst["data"], source=burst.get("path_id")))
        # 소비한 도달은 제거한다 — 같은 입력이 다음 창에서 다시 합산되지 않게(중복 합산 금지).
        self.pending_bursts[lane] = remaining
        return compose_rx_window(
            arrivals=arrivals, window_start_ns=start, sample_count=window["count"],
            sample_rate_hz=DEVICE_RATE, tuned_center_hz=self._tuned_center_hz(lane),
            channel=channel)

    def poll_async(self):
        now = self.hardware_time_ns()
        due = [e for e in self.async_events if e["due_ns"] <= now]
        self.async_events = [e for e in self.async_events if e["due_ns"] > now]
        return tuple({k: v for k, v in e.items() if k != "due_ns"} for e in due)

    def wait_for_activity(self, timeout_seconds):
        return None

    def close(self, timeout_ns):
        self.closed = True
        return True


class Stage7HarnessOperations:
    """software 판정 범위의 runtime client operations glue."""

    def __init__(self, radio, anchor, worker_t0_ns):
        self.radio = radio
        self.anchor = anchor
        self.worker_t0_ns = worker_t0_ns
        self.calls = []

    def check_prepare(self, body):
        return True

    def check_configure(self, body):
        return body["expected_serial"] == self.radio._resolved.b210_serial

    def do_configure(self, body):
        opened = self.radio.open_qualification_lease(lease_id=body["endpoint_config_sha256"][:16])
        self.calls.append("open_lease")
        return opened.is_ok

    def run_cfo(self, body):
        self.calls.append("cfo")
        return True

    def acquire_host_time(self, body):
        return {
            "summary_type": "HOST_AFFINE_FIT_V1",
            "outcome": "PASS",
            "alpha_num": self.worker_t0_ns,
            "alpha_den": 1,
            "beta_num": 1,
            "beta_den": 1,
        }

    def check_calibrate(self, body):
        return True

    def run_calibrate(self, body):
        self.calls.append(f"calibrate:{body['calibration_mode']}")
        return True

    def validate_rf_time(self, body):
        return [
            {
                "summary_type": "RF_TIME_MEASUREMENT_V1",
                "validation_phase": body["validation_phase"],
                "outcome": "PASS",
            }
        ]

    def check_active_candidate(self, body):
        return True

    def install_active_bridge(self, body):
        coefficients = AffineCoefficients(
            h0_ns=0,
            m0_ns=self.worker_t0_ns,
            alpha_num=0,
            alpha_den=1,
            beta_num=1,
            beta_den=1,
        )
        self.radio.bridge.install(
            state="ACTIVE",
            anchor=self.anchor,
            coefficients=coefficients,
            correction=(body["correction_num"], body["correction_den"]),
            clock_id=body["active_clock_id"],
            generation=body["bridge_generation"],
        )
        self.calls.append("install_bridge")
        return True

    def abort_active_bridge(self, body):
        self.radio.bridge.invalidate()

    def do_stop(self, body):
        self.calls.append("stop")

    def do_inhibit(self, body):
        self.calls.append("inhibit")


@dataclass
class Stage7SoftwareMember:
    endpoint_id: str
    endpoint: object
    client: EndpointRuntimeClient
    operations: Stage7HarnessOperations
    worker: object = None


class _Channel:
    def __init__(self, client, coordinator_ref):
        self.client = client
        self.coordinator_ref = coordinator_ref

    def request(self, message):
        return self.client.handle_command(message)

    def send_event(self, event):
        coordinator = self.coordinator_ref[0]
        if coordinator is None:
            return False
        return coordinator.receive_event(self.client.endpoint_id, event).is_ok

    def poll_events(self):
        return ()


def build_stage7_software_group(*, data_profile=DATA_PROFILE_17, run_id="stage7-sw-run"):
    """세 Endpoint + coordinator + fake 매질의 결정적 group을 만든다.

    software 판정용 `run_mode=OPERATIONAL` resolved run이다 (TX는 fake 매질).
    """

    import os as _os

    from vdeter.b210_system.configuration import OperationalGainInput

    # RX 에너지 게이트는 lane별 **잡음 바닥** 10분위수로 문턴을 잡는다.
    # fake 매질에는 잡음이 없어(침묵=0, 그 외=버스트) 워밍업 통계가
    # 버스트 전력으로 채워져 문턴=버스트×8이 되고, 이후 버스트를
    # 0-치환해 복호가 전면 중단된다 (numpy 없으면 게이트 import가
    # 실패해 fail-open이라 그동안 숨어 있던 불일치). 공식 kill switch로
    # 꼜 잡음 바닥 가정이 성립하는 RF 경로(Pi 실기 시험)에만 맡긴다.
    _os.environ["STAGE7_RX_ENERGY_GATE"] = "0"

    operational_gain = OperationalGainInput(
        endpoint_common_rx_gain_db=0,
        source_gain_artifact_sha256=hashlib.sha256(b"stage7-sw-gain").hexdigest(),
    )
    clock = TestTimeSource(
        RUN_START_UTC_NANOSECONDS,
        RUN_START_MONOTONIC_NANOSECONDS,
        utc_clock_id="stage7-utc",
        monotonic_clock_id="stage7-monotonic",
    )

    def host_raw_clock():
        return clock.now_monotonic().total_nanoseconds()

    medium = Stage7FakeMedium(host_raw_clock)
    signer, certificate_der, fingerprint = stage7_test_signer()
    anchor = HostUtcAnchor(
        host_raw_anchor_ns=RUN_START_MONOTONIC_NANOSECONDS,
        host_utc_anchor_ns=RUN_START_UTC_NANOSECONDS,
        bracket_width_ns=100,
    )
    loader = ConfigurationLoader()
    sources = {
        "MASS-A": build_source(run_mode="OPERATIONAL"),
        "MASS-B": build_mass_b_source(run_mode="OPERATIONAL"),
        "ROC": build_roc_source(run_mode="OPERATIONAL"),
    }
    members = {}
    resolved_by_endpoint = {}
    coordinator_ref = [None]
    channels = {}
    for index, endpoint_id in enumerate(ENDPOINT_IDS):
        resolved = loader.load_endpoint_configuration(
            sources[endpoint_id],
            data_profile,
            run_id=run_id,
            operational_gain=operational_gain,
        )
        if resolved.is_err:
            raise AssertionError(resolved.error)
        resolved_by_endpoint[endpoint_id] = resolved.value
        deployment = deployment_profile(
            expected_fingerprint=fingerprint, type0_override=default_type0_override()
        )
        crypto = crypto_dependencies(
            signer, certificate_der, is_control=endpoint_id == "ROC"
        )
        built = build_b210_endpoint(
            resolved.value,
            deployment,
            crypto_dependencies=crypto,
            worker_factory=Stage7FakeMediumWorker.factory(medium),
            host_raw_clock=host_raw_clock,
            monitor_enabled=False,
            time_source_override=clock,
            id_source_override=DeterministicIdSource(3000 + index),
            random_source_override=SeededRandomSource(700 + index),
        )
        if built.is_err:
            raise AssertionError(built.error)
        endpoint = built.value
        operations = Stage7HarnessOperations(
            endpoint.dependencies.radio, anchor, WORKER_T0_NS[ENDPOINT_TO_SERIAL[endpoint_id]]
        )
        client = EndpointRuntimeClient(
            endpoint_id=endpoint_id,
            run_id=run_id,
            plan_sha256="0" * 64,  # plan 생성 후 갱신한다.
            lifecycle_generation=1,
            endpoint=endpoint,
            operations=operations,
            event_sender=None,
            host_raw_clock=host_raw_clock,
        )
        channel = _Channel(client, coordinator_ref)
        client._event_sender = channel.send_event
        endpoint._intent_notifier = client.make_intent_notifier()
        channels[endpoint_id] = channel
        members[endpoint_id] = Stage7SoftwareMember(
            endpoint_id=endpoint_id,
            endpoint=endpoint,
            client=client,
            operations=operations,
        )

    from vdeter.b210_system.coordinator import SystemPlanLoader

    system_toml = (
        '[system]\n'
        'profile = "STAGE7_PI5_THREE_ENDPOINT_V1"\n'
        'runtime_coordinator_protocol = "vdeter.b210_system.runtime-coordinator.v1"\n'
        'runtime_body_schema = "STAGE7_RUNTIME_BODY_V1"\n'
        'socket_root = "/run/user/<uid>/vdeter-stage7/runtime"\n'
        'member_count = 3\n'
        'service_start_intent_timeout_ns = 30000000000\n'
        'service_start_tbb_publish_timeout_ns = 90000000000\n'
        '[[members]]\nendpoint_id = "MASS-A"\nconfig_relpath = "configs/stage7/endpoints/mass-a.toml"\n'
        '[[members]]\nendpoint_id = "MASS-B"\nconfig_relpath = "configs/stage7/endpoints/mass-b.toml"\n'
        '[[members]]\nendpoint_id = "ROC"\nconfig_relpath = "configs/stage7/endpoints/roc.toml"\n'
    )
    plan_result = SystemPlanLoader().load(
        system_toml,
        resolved_by_endpoint,
        data_profile_sha256=resolved_by_endpoint["MASS-A"].data_profile_sha256,
        deployment_sha256=resolved_by_endpoint["MASS-A"].deployment_manifest_sha256,
    )
    if plan_result.is_err:
        raise AssertionError(plan_result.error)
    plan = plan_result.value
    for member in members.values():
        member.client.plan_sha256 = plan.resolved_sha256
    coordinator = RuntimeCoordinator(
        plan,
        channels,
        host_raw_clock=host_raw_clock,
        run_id=run_id,
        lifecycle_generation=1,
    )
    coordinator_ref[0] = coordinator
    return clock, medium, plan, coordinator, members, resolved_by_endpoint


def drive_stage7_group(clock, members, steps: int, step_nanoseconds: int = 160_000_000):
    """공유 clock, fake worker poll과 세 endpoint dispatcher를 결정적으로 진행한다."""

    ordered = [members[endpoint_id] for endpoint_id in ENDPOINT_IDS]
    for _ in range(steps):
        clock.advance(step_nanoseconds)
        for member in ordered:
            member.endpoint.dependencies.radio.poll_once()
        for member in ordered:
            support = member.endpoint.stage4
            if support is None:
                continue
            support.advance_clock()
            while support.has_due_timer():
                support.fire_due_timers()
            support.pump()


def bring_group_to_admission(coordinator, members, resolved_by_endpoint, host_raw_clock):
    """§16.1 정상 P2 command 순서로 admission open까지 진행한다."""

    import hashlib as _hashlib

    from vdeter.b210_system.ipc import body_set_sha256

    def _sha(text: str) -> str:
        return _hashlib.sha256(text.encode("utf-8")).hexdigest()

    all_paths = ["C1", "C2", "C3", "C4", "C5", "C6"]
    coordinator.run_barrier(
        "prepare",
        {
            m: {
                "deployment_sha256": resolved_by_endpoint[m].deployment_manifest_sha256,
                "endpoint_plan_relpath": f"configs/stage7/endpoints/{m.lower()}.toml",
                "endpoint_plan_sha256": resolved_by_endpoint[m].resolved_sha256,
                "environment_manifest_sha256": _sha("environment"),
                "member_set_sha256": _sha("member-set"),
                "p1_member_plan_relpath": None,
                "p1_member_plan_sha256": None,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "configure",
        {
            m: {
                "endpoint_config_sha256": resolved_by_endpoint[m].resolved_sha256,
                "expected_serial": resolved_by_endpoint[m].b210_serial,
                "usb_physical_path": resolved_by_endpoint[m].host_execution.usb_physical_path,
                "radio_generation": 1,
            }
            for m in ENDPOINT_IDS
        },
    )
    now = host_raw_clock()
    coordinator.run_barrier(
        "arm_rx",
        {
            m: {
                "rx_plan_sha256": _sha("rx-plan"),
                "ready_deadline_host_raw_ns": now + 60_000_000_000,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "start_cfo",
        {
            m: {
                "cfo_plan_sha256": _sha("cfo-plan"),
                "path_ids": list(all_paths),
                "operation_deadline_host_raw_ns": now + 420_000_000_000,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "acquire_host_time",
        {
            m: {
                "profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                "acquisition_attempt": 0,
                "max_acquisition_ns": 600_000_000_000,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "calibrate_rf",
        {
            m: {
                "calibration_mode": "APPLY_AND_REVERIFY",
                "gain_plan_sha256": _sha("gain-plan"),
                "source_gain_artifact_sha256": resolved_by_endpoint[m].source_gain_artifact_sha256,
                "operational_config_sha256": resolved_by_endpoint[m].resolved_sha256,
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "PRE_CORRECTION",
                "rf_time_plan_sha256": _sha("rf-time"),
                "clock_id": "stage7-pi-host-affine-provisional-v1:sw:0",
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    candidate_set = [[m, _sha(f"candidate:{m}")] for m in ENDPOINT_IDS]
    candidate_bodies = {
        m: {
            "candidate_set": [list(item) for item in candidate_set],
            "candidate_set_sha256": body_set_sha256(candidate_set),
            "candidate_sha256": dict(candidate_set)[m],
            "active_clock_id": "stage7-pi-host-affine-v1:stage7-sw-run:0",
            "bridge_generation": 0,
            "alpha_num": 0,
            "alpha_den": 1,
            "beta_num": 1,
            "beta_den": 1,
            "correction_num": 0,
            "correction_den": 1,
        }
        for m in ENDPOINT_IDS
    }
    coordinator.run_barrier(
        "prepare_active_bridge",
        candidate_bodies,
        commit_command_name="commit_active_bridge",
        abort_command_name="abort_active_bridge",
    )
    coordinator._bridge_generation = 0
    coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "POST_CORRECTION",
                "rf_time_plan_sha256": _sha("rf-time"),
                "clock_id": "stage7-pi-host-affine-v1:stage7-sw-run:0",
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    for member in members.values():
        assert member.client.state == "HOST_TIME_LOCKED", member.client.state
    return coordinator.open_service_admission()


def start_stage7_group(clock, medium, coordinator, members, resolved_by_endpoint,
                       *, acquisition_steps=120):
    """admission→intent→staged→TBB 획득→publish까지 진행해 RUNNING을 만든다."""

    import threading as _threading
    import time as _time

    from vdeter.common.contracts.service import ServiceStartRequest

    def host_raw_clock():
        return clock.now_monotonic().total_nanoseconds()

    opened = bring_group_to_admission(coordinator, members, resolved_by_endpoint, host_raw_clock)
    if opened.is_err:
        raise AssertionError(opened.error)
    start_results = {}

    def call_start(endpoint_id):
        endpoint = members[endpoint_id].endpoint
        configuration = endpoint.dependencies.configuration.endpoint
        start_results[endpoint_id] = endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )

    threads = [
        _threading.Thread(target=call_start, args=(endpoint_id,), daemon=True)
        for endpoint_id in ENDPOINT_IDS
    ]
    for thread in threads:
        thread.start()
    for _ in range(200):
        if coordinator.required_intents_complete():
            break
        _time.sleep(0.01)
    if not coordinator.required_intents_complete():
        raise AssertionError("required intent가 수집되지 않았습니다")
    commits = coordinator.commit_service_start()
    staged_set = []
    for endpoint_id in ENDPOINT_IDS:
        result = commits[endpoint_id]["body"]["result"]
        staged_set.append([endpoint_id, result["staged_service_started_sha256"]])
    drive_stage7_group(clock, members, acquisition_steps)
    for endpoint_id in ENDPOINT_IDS:
        if members[endpoint_id].endpoint.stage4.mac.state.active_tbb is None:
            raise AssertionError(f"{endpoint_id} TBB acquisition 미완")
    emit_tbb_active_records(members)
    coordinator.publish_service_start(staged_set, bridge_generation=0)
    for thread in threads:
        thread.join(timeout=10)
    for endpoint_id in ENDPOINT_IDS:
        result = start_results.get(endpoint_id)
        if result is None or result.is_err:
            raise AssertionError(f"{endpoint_id} start 실패: {getattr(result, 'error', None)}")
    return start_results


def service_events(member, event_type):
    sink = member.endpoint.start_gate.application_sink
    return [event for event in sink.events() if event.event_type is event_type]


def emit_tbb_active_records(members, deployment_fingerprint_hex=None):
    """세 endpoint의 frozen MAC active snapshot에서 §16.1 record를 만들어 전송한다."""

    records = {}
    for endpoint_id, member in members.items():
        state = member.endpoint.stage4.mac.state
        active = state.active_tbb
        if active is None:
            raise AssertionError(f"{endpoint_id} active TBB가 없습니다")
        fingerprint = active.fingerprint
        try:
            fingerprint_bytes = bytes.fromhex(fingerprint)
        except ValueError:
            fingerprint_bytes = hashlib.sha256(fingerprint.encode("utf-8")).digest()
        if len(fingerprint_bytes) != 32:
            fingerprint_bytes = hashlib.sha256(fingerprint.encode("utf-8")).digest()
        record = TbbActiveRecord(
            endpoint_id=endpoint_id,
            readiness_kind="ROC_BROADCAST_READY" if endpoint_id == "ROC" else "MASS_ACQUIRED",
            lifecycle_generation=member.client.lifecycle_generation,
            bridge_generation=member.client.bridge_generation,
            tbb_fingerprint_sha256=fingerprint_bytes,
            valid_from_utc_ns=active.valid_from_nanoseconds,
            valid_until_utc_ns=active.valid_until_nanoseconds,
            activation_frame_ordinal=active.activation_nanoseconds,
            local_tbb_evidence_sha256=hashlib.sha256(
                f"{endpoint_id}:{fingerprint}".encode("utf-8")
            ).digest(),
        )
        records[endpoint_id] = record
        if not member.client.emit_tbb_active(record):
            raise AssertionError(f"{endpoint_id} TBB_ACTIVE event 전송 실패")
    return records
