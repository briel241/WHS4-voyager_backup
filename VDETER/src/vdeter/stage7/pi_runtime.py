"""Stage 7 Pi runtime operations (`stage7` §9, §13, §16.1).

runtime client의 `operations` 실제 구현. 모든 hardware 동작은 endpoint의
`Stage7B210RadioAdapter` lease worker를 통해서만 수행한다. plan 파일은 run
디렉터리에 배포된 immutable JSON이며 command body의 SHA-256과 exact 일치해야
실행한다.
"""

from __future__ import annotations

import hashlib
import json
import os

from .canonical_json import encode_canonical_json, sha256_hex
from .hw_operations import (
    CfoProbePlanEntry,
    acquire_host_time_fit,
    collect_utc_anchor,
    gain_step_acceptable,
    host_affine_fit_summary,
    link11_calibration_burst_device_bytes,
    measure_cfo_path_rx,
    measure_gain_step_metrics,
    probe_vector_sha256,
    transmit_cfo_probe,
)


#: 참고(rr-p1 run g/h 실측): 판정 슬롯 시작 정렬 고립 과도(~3-5ms, ROC RX)를
#: TX lead 패드(무음/burst 내용 cyclic prefix)로 격리하려던 시도는 둘 다
#: 효과가 없었고, 내용 패드는 §13.3 rf_time 타이밍 추정에 주기성 모호를
#: 유발해 C3 slot_guard FAIL(-7.7µs)을 만들었다 — 패드는 제거했고 과도는
#: v0.9.2 관용 판정(evaluate_reval_window)이 흡수한다.
def evaluate_reval_window(fail_ordinals, wrong_byte_count: int, tolerance: int):
    """§9.2 재검증 창 판정 (v0.9.2 사용자 승인 2026-08-27).

    tolerance == 0 이면 무결점(100/100) 요구. tolerance > 0 이면 고립
    no-decode를 tolerance개까지 허용하되: ① wrong-bytes(오염 수신)는
    항상 0, ② 인접 슬롯 연속 실패 불허 — 고립 단발 과도(무해, rr-p1
    run e/f/g 실측)와 링크 열화(유해)를 구분하는 선이다."""

    fails = sorted(int(v) for v in fail_ordinals)
    if wrong_byte_count != 0:
        return False
    if len(fails) > max(0, int(tolerance)):
        return False
    for before, after in zip(fails, fails[1:]):
        if after - before == 1:
            return False
    return True


def _load_plan(run_root: str, name: str, expected_sha256: str):
    path = os.path.join(run_root, "plans", name)
    with open(path, "rb") as handle:
        data = handle.read()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(f"plan digest 불일치: {name}")
    return json.loads(data.decode("utf-8"))


class Stage7PiOperations:
    """Pi에서 실행되는 runtime client operations."""

    def __init__(
        self,
        *,
        endpoint,
        resolved,
        run_root: str,
        endpoint_id: str,
        progress_notifier=None,
    ) -> None:
        self.endpoint = endpoint
        self.resolved = resolved
        self.run_root = run_root
        self.endpoint_id = endpoint_id
        self.radio = endpoint.dependencies.radio
        self.anchor = None
        self.provisional_fit = None
        self.acquisition_attempt = 0
        self.active_correction = None
        self.installed_bridge_generation = None
        self.sealed_rx_gains = {}
        self.evidence = []
        #: v0.9.1 E항: 장시간 COMMIT 중 coordinator에 progress event를 보내는
        #: 선택적 callable(str). 없으면 무동작.
        self.progress_notifier = progress_notifier
        self._last_progress_mono = 0.0

    def _progress(self, note: str) -> None:
        """진행 신호를 30초 throttle로 방출한다 (barrier-내부 stall 감시용)."""

        if self.progress_notifier is None:
            return
        import time as _t

        now = _t.monotonic()
        if now - self._last_progress_mono < 30.0:
            return
        self._last_progress_mono = now
        try:
            self.progress_notifier(note)
        except Exception:  # noqa: BLE001 - 진행 신호 실패가 측정을 깨면 안 된다
            pass

    # --- barrier handlers ------------------------------------------------------

    def check_prepare(self, body) -> bool:
        expected = self.resolved
        if body["endpoint_plan_sha256"] != expected.resolved_sha256:
            return False
        if body["deployment_sha256"] != expected.deployment_manifest_sha256:
            return False
        if body["environment_manifest_sha256"] != expected.host_execution.environment_manifest_sha256:
            return False
        return True

    def check_configure(self, body) -> bool:
        if body["expected_serial"] != self.resolved.b210_serial:
            return False
        if body["usb_physical_path"] != self.resolved.host_execution.usb_physical_path:
            return False
        # live sysfs 검증.
        base = f"/sys/bus/usb/devices/{body['usb_physical_path']}"
        try:
            with open(f"{base}/serial", "r", encoding="ascii") as handle:
                serial = handle.read().strip()
        except OSError:
            return False
        return serial == self.resolved.b210_serial

    def do_configure(self, body) -> bool:
        gain = (
            0
            if self.resolved.run_mode != "OPERATIONAL"
            else self.resolved.endpoint_common_rx_gain_db
        )
        opened = self.radio.open_qualification_lease(
            lease_id=body["endpoint_config_sha256"][:32], rx_gain_db=(gain, gain)
        )
        if opened.is_err:
            self.evidence.append({"kind": "configure_failed", "error": str(opened.error)})
            return False
        self.anchor = collect_utc_anchor()
        self.evidence.append(
            {
                "kind": "lease_open",
                "readback_sha256": sha256_hex(
                    encode_canonical_json(
                        json.loads(json.dumps(opened.value, default=str, sort_keys=True))
                    )
                ),
                "utc_anchor": {
                    "raw_ns": self.anchor.host_raw_anchor_ns,
                    "utc_ns": self.anchor.host_utc_anchor_ns,
                    "bracket_ns": self.anchor.bracket_width_ns,
                },
            }
        )
        return True

    # --- CFO (§9.3) -------------------------------------------------------------

    def run_cfo(self, body):
        try:
            plan = _load_plan(self.run_root, "cfo_plan.json", body["cfo_plan_sha256"])
        except Exception as error:  # noqa: BLE001
            self.evidence.append({"kind": "cfo_plan_error", "error": str(error)})
            return None
        if plan.get("probe_vector_sha256") != probe_vector_sha256():
            self.evidence.append({"kind": "cfo_probe_digest_mismatch"})
            return None
        worker = self.radio.lease.worker
        # §9.3 개정(사용자 승인 2026-08-26): probe TX/RX gain은 plan이 결속한
        # 값(40dB)을 사용하고, probe 구간 종료 후 lease gain으로 복원한다.
        probe_tx_gain = plan.get("probe_tx_gain_db", 0)
        probe_rx_gain = plan.get("probe_rx_gain_db", 0)
        results = []
        all_pass = True
        for entry_value in plan["paths"]:
            entry = CfoProbePlanEntry(
                path_id=entry_value["path_id"],
                tx_endpoint_id=entry_value["tx_endpoint_id"],
                rx_endpoint_id=entry_value["rx_endpoint_id"],
                tx_lane_index=entry_value["tx_lane_index"],
                rx_lane_index=entry_value["rx_lane_index"],
                tx_hardware_times_ns=self._plan_times_to_hardware(worker, entry_value),
            )
            if entry.tx_endpoint_id == self.endpoint_id:
                other_lane = 1 - entry.tx_lane_index
                restore_lo = self._tx_upper_retune(
                    worker, entry.tx_endpoint_id, entry.rx_endpoint_id
                )
                applied_tx = worker.set_tx_gain(entry.tx_lane_index, probe_tx_gain)
                worker.set_tx_mute(other_lane, True)
                try:
                    for tx_time in entry.tx_hardware_times_ns:
                        self._wait_until_hardware(worker, tx_time - 500_000_000)
                        transmit_cfo_probe(worker, entry.tx_lane_index, tx_time)
                        self._progress("cfo-tx")
                    # 마지막 burst 방사 종료까지 gain을 유지한다.
                    self._wait_until_hardware(
                        worker, entry.tx_hardware_times_ns[-1] + 1_200_000_000
                    )
                finally:
                    worker.set_tx_gain(entry.tx_lane_index, 0)
                    worker.set_tx_mute(other_lane, False)
                    restore_lo()
                self.evidence.append(
                    {"kind": "cfo_tx", "path_id": entry.path_id, "count": 10,
                     "probe_tx_gain_db": applied_tx}
                )
            elif entry.rx_endpoint_id == self.endpoint_id:
                applied_rx = worker.set_rx_gain(entry.rx_lane_index, probe_rx_gain)
                try:
                    measurement = measure_cfo_path_rx(
                        worker, entry, on_burst=lambda: self._progress("cfo-rx")
                    )
                except Exception as error:  # noqa: BLE001
                    self.evidence.append(
                        {"kind": "cfo_rx_failed", "path_id": entry.path_id,
                         "error": str(error), "probe_rx_gain_db": applied_rx}
                    )
                    all_pass = False
                    continue
                finally:
                    lease_gain = (self.radio.lease.gain_db or (0, 0))[entry.rx_lane_index]
                    worker.set_rx_gain(entry.rx_lane_index, lease_gain)
                results.append(measurement)
                all_pass = all_pass and measurement["passed"]
                self.evidence.append(
                    {"kind": "cfo_rx", "probe_rx_gain_db": applied_rx, **measurement}
                )
        return all_pass

    @staticmethod
    def _wait_until_hardware(worker, hardware_ns: int) -> None:
        import time as _t

        while int(worker.hardware_time_ns()) < hardware_ns:
            _t.sleep(0.02)

    def _wait_host_until(self, host_raw_ns: int, stop_event=None) -> None:
        """host raw 시계로 대기. 순간 offset 변환(`_plan_times_to_hardware`)을
        스케줄 직전에 수행하기 위한 지연 — 일찍 변환하면 host↔hardware 드리프트
        (~6.5ppm)가 그대로 시각 오차가 된다 (gaincal7 실측: 25분 조기 변환
        ≈ 10ms 밀림 → 전 슬롯 동기 실패). 대기 중에도 progress 신호를 내
        barrier-내부 stall 감시가 오탐하지 않게 한다."""

        import time as _t

        from .hw_operations import read_monotonic_raw_ns

        while read_monotonic_raw_ns() < host_raw_ns:
            if stop_event is not None and stop_event.is_set():
                return
            self._progress("waiting-schedule")
            _t.sleep(0.05)

    def _tx_upper_retune(self, worker, tx_endpoint_id, rx_endpoint_id):
        """§10.1: MASS↔MASS direct path(C5/C6)의 TX shared LO를 upper로
        재조정하고 복원 callable을 반환한다. ROC 관여 path는 no-op.

        MASS RX LO는 항상 upper 고정이므로 RX 측 재조정은 없다.
        """

        if tx_endpoint_id == "ROC" or rx_endpoint_id == "ROC":
            return lambda: None
        upper = self.resolved.upper_rf_tune_hz
        lower = self.resolved.lower_rf_tune_hz
        worker.retune_tx_lo(upper, None)
        self.evidence.append(
            {"kind": "tx_lo_retune", "target_hz": upper,
             "path": f"{tx_endpoint_id}->{rx_endpoint_id}"}
        )
        def _restore():
            worker.retune_tx_lo(lower, None)

        return _restore

    def _plan_times_to_hardware(self, worker, entry_value) -> tuple:
        """plan 항목의 시각들을 이 endpoint의 hardware 시각으로 변환한다.

        `scheduled_host_raw_ns`(공통 host monotonic raw — 세 endpoint가 같은 Pi
        host를 공유)는 순간 offset으로 변환하고, `tx_hardware_times_ns`는 그대로
        사용한다(software harness 하위호환). bridge 이전 단계(CFO)에서는 순간
        offset 변환만 가능하며 §9.3 gate는 주파수 기반이므로 timing 정밀도
        요구가 없다.
        """

        if "scheduled_host_raw_ns" in entry_value:
            # §13.2 fit이 있으면 fit 기반 정밀 변환을 쓴다. 순간 offset
            # 페어링(hardware_now RPC + host_now)은 worker가 직전 대용량
            # 창 읽기 응답을 마저 내보내는 중이면 응답 지연 D(~10ms 실측,
            # rr-p1 run c IQ 상관: 전 경로 burst +9.8~14.9ms 동일 부호
            # 편이)만큼 변환이 일찍 계산되어 TX/RX 격자가 어긋난다 —
            # run a에서 decode가 성공한 창이 전부 "직전 창 소실(worker
            # 유휴)" 뒤였던 것이 이 기전의 방증. CFO 단계(fit 이전)만
            # 순간 변환을 유지한다(§9.3은 주파수 기반이라 무해).
            if self.provisional_fit is not None:
                return tuple(
                    self._host_raw_to_hardware(int(value), "PRE_CORRECTION")
                    for value in entry_value["scheduled_host_raw_ns"]
                )
            from .hw_operations import read_monotonic_raw_ns

            hardware_now = int(worker.hardware_time_ns())
            host_now = int(read_monotonic_raw_ns())
            return tuple(
                hardware_now + (int(value) - host_now)
                for value in entry_value["scheduled_host_raw_ns"]
            )
        times = entry_value["tx_hardware_times_ns"]
        if isinstance(times, dict):
            return tuple(int(value) for value in times[self.endpoint_id])
        return tuple(int(value) for value in times)

    # --- host affine time (§13.2) ------------------------------------------------

    def acquire_host_time(self, body):
        self.acquisition_attempt = body["acquisition_attempt"]
        worker = self.radio.lease.worker
        restart_log = []
        try:
            fit, observations = acquire_host_time_fit(
                worker, restart_log=restart_log,
                on_observation=lambda: self._progress("host-time"),
            )
        except Exception as error:  # noqa: BLE001
            self.evidence.append(
                {"kind": "host_time_failed", "error": str(error),
                 "restarts": restart_log}
            )
            return None
        if restart_log:
            self.evidence.append(
                {"kind": "host_time_restarts", "restarts": restart_log}
            )
        self.provisional_fit = fit
        clock_id = (
            f"stage7-pi-host-affine-provisional-v1:{self.resolved.run_id}:"
            f"{self.acquisition_attempt}"
        )
        summary, artifact = host_affine_fit_summary(
            fit,
            observations,
            run_id=self.resolved.run_id,
            endpoint_id=self.endpoint_id,
            b210_serial=self.resolved.b210_serial,
            radio_generation=self.radio.lease.radio_generation,
            lane_generation=self.endpoint.stage7_graph["lane_generation"],
            acquisition_attempt=self.acquisition_attempt,
            provisional_clock_id=clock_id,
        )
        self.evidence.append({"kind": "host_time_fit", "summary": summary, "artifact": artifact})
        return summary

    # --- gain (§9.2) --------------------------------------------------------------

    def check_calibrate(self, body) -> bool:
        mode = body["calibration_mode"]
        if mode == "APPLY_AND_REVERIFY":
            return (
                body["operational_config_sha256"] == self.resolved.resolved_sha256
                and body["source_gain_artifact_sha256"]
                == self.resolved.source_gain_artifact_sha256
            )
        return self.resolved.run_mode == "GAIN_CALIBRATION"

    def run_calibrate(self, body):
        """§9.2 gain calibration.

        SEARCH_AND_SEAL: TX `0 dB` 고정, RX gain을 plan의 오름차순 3 dB step으로
        탐색해 최소 합격 gain을 채택하고, plan에 revalidation_paths가 있으면
        endpoint 공통 gain(max)으로 각 lane 100 burst 재검증까지 수행한다.
        APPLY_AND_REVERIFY: 탐색 없이 operational config의 고정 공통 gain으로
        revalidation_paths만 재검증한다.

        step 합격: 100 burst 전부 peak `≤-3 dBFS`, RMS 창, clipping 0,
        byte-exact decode. overload는 즉시 calibration FAIL이며 gain을 더
        올리지 않는다. 반환: {"passed", "endpoint_common_rx_gain_db",
        "lane_candidates"}.
        """

        from .hw_operations import (
            link11_calibration_packet_sha256,
        )

        try:
            plan = _load_plan(self.run_root, "gain_plan.json", body["gain_plan_sha256"])
        except Exception as error:  # noqa: BLE001
            self.evidence.append({"kind": "gain_plan_error", "error": str(error)})
            return {"passed": False, "endpoint_common_rx_gain_db": None,
                    "lane_candidates": {}}
        worker = self.radio.lease.worker
        burst = link11_calibration_burst_device_bytes()
        expected_digest = link11_calibration_packet_sha256()
        graph = self.endpoint.stage7_graph
        mode = body["calibration_mode"]
        adaptive = mode == "ADAPTIVE_SEARCH_AND_SEAL"
        lane_candidates = {}
        all_pass = True
        for path in plan["paths"]:
            if path["tx_endpoint_id"] == self.endpoint_id:
                lane = path["tx_lane_index"]
                other_lane = 1 - lane
                restore_lo = self._tx_upper_retune(
                    worker, path["tx_endpoint_id"], path["rx_endpoint_id"]
                )
                worker.set_tx_mute(other_lane, True)
                last_tx_time = None
                skipped_past = 0
                if adaptive:
                    # adaptive plan: path가 gain_steps 대신 연속 train을 갖는다.
                    tx_steps = ({"scheduled_host_raw_ns": path["scheduled_host_raw_ns"]},)
                else:
                    tx_steps = path["gain_steps"]
                try:
                    for gain_step in tx_steps:
                        # 스케줄 5s 전까지 host 시계로 대기한 뒤 변환한다
                        # (조기 변환의 드리프트 오차 방지).
                        sched = gain_step.get("scheduled_host_raw_ns")
                        if sched:
                            self._wait_host_until(int(sched[0]) - 5_000_000_000)
                        for tx_time in self._plan_times_to_hardware(worker, gain_step):
                            # 이미 기한이 지난 burst는 제출하지 않는다 — UHD가
                            # 전부 늦음('L')으로 버리며 홍수만 만든다.
                            if int(worker.hardware_time_ns()) > tx_time - 50_000_000:
                                skipped_past += 1
                                continue
                            # 1.5s 선등록: 방사 시각은 tx_time 태그가 보장하고,
                            # 공급 지연 ~1s급이 burst 앞부분을 자르지 못한다.
                            self._wait_until_hardware(worker, tx_time - 1_500_000_000)
                            worker.submit_tx_group(((lane, burst, tx_time),))
                            last_tx_time = tx_time
                            self._progress("gain-tx")
                finally:
                    # 선등록된 잔여 burst(~1.5s)가 전부 방사된 뒤에만 LO를
                    # 복원한다 — 조기 retune은 tail burst를 다른 주파수로
                    # 방사시킨다 (gaincal3 실측: C2 75dB tail 7슬롯).
                    if last_tx_time is not None:
                        self._wait_until_hardware(worker, last_tx_time + 100_000_000)
                    worker.set_tx_mute(other_lane, False)
                    restore_lo()
                outcome_counts = {}
                for event in worker.poll_async():
                    key = str(event.get("outcome"))
                    outcome_counts[key] = outcome_counts.get(key, 0) + 1
                self.evidence.append(
                    {"kind": "gain_tx", "path_id": path["path_id"],
                     "steps": 1 if adaptive else len(path["gain_steps"]),
                     "skipped_past_due": skipped_past,
                     "async_outcomes": outcome_counts}
                )
            elif path["rx_endpoint_id"] == self.endpoint_id:
                lane = path["rx_lane_index"]
                stack = graph["rf_a_stack"] if lane == 0 else graph["rf_b_stack"]
                if adaptive:
                    candidate, overload, step_summaries = self._gain_rx_adaptive(
                        worker, stack, lane, path, mode, expected_digest,
                    )
                else:
                    candidate, overload, step_summaries = self._gain_rx_run(
                        worker, stack, lane, path, path["gain_steps"],
                        mode, expected_digest,
                        judge_from=int(path.get("warmup_count", 0)),
                    )
                passed = candidate is not None and not overload
                if passed:
                    self.sealed_rx_gains[(path["path_id"], lane)] = candidate
                    lane_candidates[lane] = candidate
                    # 채택 gain을 유지한다 (§9.2: run 중 자동 변경 금지).
                    worker.set_rx_gain(lane, candidate)
                all_pass = all_pass and passed
                self.evidence.append(
                    {
                        "kind": "gain_search",
                        "path_id": path["path_id"],
                        "lane_index": lane,
                        "candidate_db": candidate,
                        "overload": overload,
                        "passed": passed,
                        "steps": step_summaries,
                    }
                )
        common_db = self._gain_revalidate(
            plan, body, worker, graph, burst, expected_digest,
            lane_candidates, all_pass,
        )
        if common_db is None and plan.get("revalidation_paths"):
            all_pass = False
        return {
            "passed": bool(all_pass and (common_db is not None or not plan.get("revalidation_paths"))),
            "endpoint_common_rx_gain_db": common_db,
            "lane_candidates": {str(k): v for k, v in sorted(lane_candidates.items())},
        }

    def _gain_rx_adaptive(
        self, worker, stack, lane, path, mode, expected_digest
    ):
        """§9.2.1 RESEARCH_READY_MINIMAL adaptive RX 탐색.

        TX는 plan의 연속 train을 그대로 송신하고, RX는 plan이 고정한
        coarse 창 스케줄을 한 번의 캡처 파이프라인(_gain_rx_run 다단계,
        첫 통과에서 조기 중단)으로 걷는다. 창 사이 여유가 없으면 창당 IQ
        읽기+RPC가 다음 창 시작을 침범해 창이 교대로 소실되므로(rr-p1
        run a 실측: 6 lane 전부 window=False 교대) 여유는 plan 스케줄에
        내장된다. 통과 후보가 나오면 fine(−3 dB) 창 하나로 하향 확인한다.
        75 dB는 B210 허용범위 내 terminal saturation candidate (v0.9.1
        C항). plan-결속 deadline을 로컬로 강제하며 같은 run에서 재시도
        하지 않는다. 반환 (g_lane|None, overload, step_summaries).
        """

        from .hw_operations import read_monotonic_raw_ns

        warm = int(path.get("warmup_count", 3))
        bursts = int(path.get("candidate_bursts", 10))
        fine_step = int(path.get("fine_step_db", 3))
        deadline = int(path.get("deadline_host_raw_ns", 0)) or None

        def deadline_passed() -> bool:
            return deadline is not None and read_monotonic_raw_ns() >= deadline

        coarse_steps = tuple(
            {"gain_db": int(w["gain_db"]),
             "scheduled_host_raw_ns": [int(v) for v in w["scheduled_host_raw_ns"]]}
            for w in path["coarse_windows"]
        )
        candidate, overload, summaries = self._gain_rx_run(
            worker, stack, lane, path, coarse_steps, mode, expected_digest,
            judge_from=warm, deadline_host_raw_ns=deadline,
        )
        if overload:
            return None, True, summaries
        if candidate is None:
            if deadline_passed():
                self.evidence.append(
                    {"kind": "gain_adaptive_deadline",
                     "path_id": path["path_id"], "stage": "coarse"}
                )
            return None, False, summaries
        # coarse에서 이미 판정 실패한 gain(예: terminal 75 채택 시의 72)은
        # fine에서 재측정하지 않는다 — 결정적이며 창 낭비가 없다.
        failed_coarse = {
            int(s["gain_db"]) for s in summaries
            if s.get("window") and int(s.get("accepted", 0)) < bursts
        }
        g_lane = candidate
        low = candidate - fine_step
        if low >= 0 and low not in failed_coarse and not deadline_passed():
            fine = {"gain_db": low,
                    "scheduled_host_raw_ns": [
                        int(v) for v in
                        path["fine_window"]["scheduled_host_raw_ns"]
                    ]}
            fine_cand, fine_ovl, fine_summaries = self._gain_rx_run(
                worker, stack, lane, path, (fine,), mode, expected_digest,
                judge_from=warm, deadline_host_raw_ns=deadline,
            )
            summaries.extend(fine_summaries)
            if fine_ovl:
                return None, True, summaries
            if fine_cand == low:
                g_lane = low
        self.evidence.append(
            {"kind": "gain_adaptive", "path_id": path["path_id"],
             "lane_index": lane, "coarse_candidate_db": candidate,
             "g_lane_db": g_lane,
             "windows_used": sum(1 for s in summaries if s.get("window"))}
        )
        return g_lane, False, summaries

    def _gain_rx_run(
        self, worker, stack, lane, path, gain_steps, phase_label, expected_digest,
        judge_from: int = 0, judge_all: bool = False,
        deadline_host_raw_ns=None,
    ):
        """§9.2 RX 측정 기계: gain_steps를 캡처 스레드+소비자 파이프라인으로
        판정한다. `judge_from` 앞의 slot들은 warm-up으로 캡처만 하고 판정에서
        제외한다(evidence에는 별도 기록). `judge_all`이면 raw PHY 집계용으로
        metric 실패가 있어도 decode를 수행하고 첫 decode 실패에서 중단하지
        않는다. `deadline_host_raw_ns`가 주어지면 각 step 캡처 시작 전에
        plan-결속 deadline을 확인하고 지났으면 잔여 step을 버린다 (§17A.5:
        같은 run 내 재시도·연장 없음). 반환 (candidate, overload,
        step_summaries)."""

        import queue as _queue
        import threading as _threading

        from .hw_operations import (
            DEVICE_RATE,
            SLOT_SAMPLES,
            capture_step_window,
            measure_gain_step_metrics_fast,
            read_monotonic_raw_ns,
        )

        candidate = None
        overload = False
        step_summaries = []

        # 캡처는 일정 전담 스레드가 수행한다: gain 설정→창 제출→읽기만
        # 하고 판정(느린 decode)은 소비자가 큐 뒤에서 수행한다. 판정
        # 지연이 창 일정을 침범하지 않는다 (11차 결함의 구조적 해결).
        step_queue = _queue.Queue(maxsize=4)
        stop_event = _threading.Event()

        # 창 시작 20ms 패드: finite stream 시작 과도가 slot 0의 preamble을
        # 오염시켜 metric은 통과하되 동기만 실패하는 사례를 차단한다
        # (gaincal4 reval C1/C5 slot-0 실측). 읽은 뒤 즉시 잘라내 하류 슬롯
        # 산술은 불변이다.
        pad_samples = 6_144
        pad_ns = pad_samples * 1_000_000_000 // DEVICE_RATE

        def _capture_loop():
            try:
                for gain_step in gain_steps:
                    if stop_event.is_set():
                        break
                    if (
                        deadline_host_raw_ns is not None
                        and read_monotonic_raw_ns() >= deadline_host_raw_ns
                    ):
                        break
                    # 스케줄 5s 전까지 host 시계로 대기 후 변환 — 조기 변환의
                    # 드리프트가 창 위치를 밀지 않게 한다 (gaincal7 실측).
                    sched = gain_step.get("scheduled_host_raw_ns")
                    if sched:
                        self._wait_host_until(
                            int(sched[0]) - 5_000_000_000, stop_event
                        )
                    if stop_event.is_set():
                        break
                    gain_db = int(gain_step["gain_db"])
                    applied = worker.set_rx_gain(lane, gain_db)
                    hw_times = self._plan_times_to_hardware(worker, gain_step)
                    spacing_ns = hw_times[1] - hw_times[0]
                    spacing_samples = (
                        spacing_ns * DEVICE_RATE // 1_000_000_000
                    )
                    # 꼬리에도 pad를 둔다: 절대 offset 추출은 실제 캡처 시작
                    # 시각 기준이라, 시작 지연(~1 sample)+fit 기울기 반올림이
                    # 마지막 slot 끝을 버퍼 밖으로 1~2 sample 밀 수 있다
                    # (rr-p1 run d 실측: 전 창 captured 9/10 — ordinal 12 절단).
                    total = (
                        spacing_samples * (len(hw_times) - 1)
                        + SLOT_SAMPLES + 2 * pad_samples
                    )
                    raw, first_ns = capture_step_window(
                        worker, lane, hw_times[0] - pad_ns, total
                    )
                    # pad를 고정 절단하지 않는다: slot 추출은 소비자가 캡처의
                    # 실제 시작 시각(first_ns) 기준 절대 offset으로 수행한다.
                    # 요청 시각과 실제 시작이 어긋나면(스트림 재시작 지연 등)
                    # 고정 절단은 전 슬롯을 δ만큼 밀어 decode를 전멸시킨다
                    # (rr-p1 run b 실측: 6 lane decode 0 — 창 소실 없는 연속
                    # 재제출 구조에서 표면화).
                    step_queue.put(
                        (gain_db, applied, raw,
                         None if first_ns is None else int(first_ns),
                         spacing_samples, hw_times)
                    )
            finally:
                step_queue.put(None)

        capture_thread = _threading.Thread(target=_capture_loop, daemon=True)
        capture_thread.start()

        # 계측(rr-p1 run b 전 lane decode 0 실측 추적): metric 통과·decode
        # 실패 슬롯의 IQ 원파형을 access-controlled run 트리에만 덤프한다
        # (§17A/§16 원칙: raw IQ는 시험 artifact 외부로 내보내지 않는다).
        iq_dump_dir = os.path.join(self.run_root, "evidence", "iq_debug")
        iq_dump_budget = 12
        iq_reference_dumped = False
        window_dump_done = False

        def _dump_window(gain_db, raw, first_ns, hw_times):
            nonlocal window_dump_done
            if window_dump_done:
                return
            window_dump_done = True
            try:
                os.makedirs(iq_dump_dir, exist_ok=True)
                name = (
                    f"{phase_label.replace(':', '_')}_{path['path_id']}"
                    f"_g{gain_db}_window"
                )
                with open(os.path.join(iq_dump_dir, name + ".cf32"), "wb") as fh:
                    fh.write(raw)
                meta = {
                    "path_id": path["path_id"], "gain_db": gain_db,
                    "buffer_first_hardware_ns": int(first_ns),
                    "slot_hardware_ns": [int(v) for v in hw_times],
                    "device_rate": DEVICE_RATE, "slot_samples": SLOT_SAMPLES,
                    "pad_ns": pad_ns,
                }
                with open(os.path.join(iq_dump_dir, name + ".json"), "wb") as fh:
                    fh.write(json.dumps(meta, sort_keys=True).encode())
            except OSError:
                pass

        def _dump_iq(tag, gain_db, ordinal, slot, slot_first_ns):
            nonlocal iq_dump_budget, iq_reference_dumped
            if tag == "ok":
                if iq_reference_dumped:
                    return
                iq_reference_dumped = True
            elif iq_dump_budget <= 0:
                return
            else:
                iq_dump_budget -= 1
            try:
                os.makedirs(iq_dump_dir, exist_ok=True)
                name = (
                    f"{phase_label.replace(':', '_')}_{path['path_id']}"
                    f"_g{gain_db}_o{ordinal}_{tag}"
                )
                with open(os.path.join(iq_dump_dir, name + ".cf32"), "wb") as fh:
                    fh.write(slot)
                meta = {
                    "path_id": path["path_id"], "gain_db": gain_db,
                    "ordinal": ordinal, "tag": tag,
                    "slot_first_hardware_ns": int(slot_first_ns),
                    "device_rate": DEVICE_RATE, "slot_samples": SLOT_SAMPLES,
                }
                with open(os.path.join(iq_dump_dir, name + ".json"), "wb") as fh:
                    fh.write(json.dumps(meta, sort_keys=True).encode())
            except OSError:
                pass

        while True:
            item = step_queue.get()
            if item is None:
                break
            (gain_db, applied, raw, first_ns, spacing_samples,
             step_hw_times) = item
            burst_count = len(step_hw_times)
            accepted = 0
            captured = 0
            metrics_fail = 0
            decode_fail = 0
            decode_fail_ordinals = []
            step_overload = False
            if raw is not None:
                slots = []
                warm_metrics = []
                for ordinal in range(burst_count):
                    # slot 위치는 실제 캡처 시작 시각 기준 절대 offset:
                    # 캡처가 늦게 시작해도 slot 격자가 밀리지 않는다.
                    slot_time_ns = int(step_hw_times[ordinal])
                    delta_ns = slot_time_ns - int(first_ns)
                    if delta_ns < 0:
                        continue
                    offset = (
                        (delta_ns * DEVICE_RATE + 500_000_000)
                        // 1_000_000_000
                    ) * 8
                    slot = raw[offset : offset + SLOT_SAMPLES * 8]
                    if len(slot) != SLOT_SAMPLES * 8:
                        continue
                    if ordinal < judge_from:
                        # warm-up slot: 판정 제외, 진단용 metric만 남긴다.
                        # + 창의 첫 decode 호출이 cold라 records 0이 되는 문제
                        # (gaincal9 확정)를 흡수하기 위해 warm-up 슬롯에서도
                        # decode를 실제 호출하고 결과는 버린다.
                        metrics = measure_gain_step_metrics_fast(slot)
                        warm_metrics.append(
                            {"ordinal": ordinal,
                             "rms_dbfs": f"{metrics['rms_dbfs']:.1f}",
                             "peak_dbfs": f"{metrics['peak_dbfs']:.1f}"}
                        )
                        if metrics["clipping_samples"] == 0 and metrics["peak_dbfs"] <= -3.0:
                            warm_first_ns = slot_time_ns
                            try:
                                warm_record = self._decode_calibration_slot(
                                    stack, slot, warm_first_ns,
                                    {"path_id": path["path_id"],
                                     "channel": path["channel"]},
                                    ("warm", gain_db, ordinal), expected_digest,
                                    phase_label,
                                )
                            except Exception:  # noqa: BLE001 - warm decode는 폐기
                                warm_record = None
                            if warm_record is None and metrics["rms_dbfs"] >= -50.0:
                                _dump_iq("warmfail", gain_db, ordinal, slot,
                                         warm_first_ns)
                            elif warm_record is not None:
                                _dump_iq("ok", gain_db, ordinal, slot,
                                         warm_first_ns)
                        continue
                    slots.append((ordinal, slot, slot_time_ns))
                captured = len(slots)
                metric_ok = []
                fail_detail = []
                for ordinal, slot, slot_time_ns in slots:
                    metrics = measure_gain_step_metrics_fast(slot)
                    if (
                        metrics["clipping_samples"] > 0
                        or metrics["peak_dbfs"] > -3.0
                    ):
                        step_overload = True
                        break
                    if gain_step_acceptable(metrics):
                        metric_ok.append((ordinal, slot, slot_time_ns))
                    else:
                        metrics_fail += 1
                        if len(fail_detail) < 8:
                            fail_detail.append(
                                {"ordinal": ordinal,
                                 "rms_dbfs": f"{metrics['rms_dbfs']:.1f}",
                                 "peak_dbfs": f"{metrics['peak_dbfs']:.1f}"}
                            )
                decode_allowed = judge_all or (
                    not step_overload and metrics_fail == 0
                    and captured == burst_count - judge_from
                )
                if decode_allowed and not step_overload:
                    # 통상: 전 슬롯 metric 통과 시에만 decode, 첫 실패 중단.
                    # judge_all(raw PHY/관용 재검증): 전 metric-통과 슬롯을
                    # 끝까지 decode하고 실패 ordinal을 남긴다.
                    for ordinal, slot, slot_first_ns in metric_ok:
                        record = self._decode_calibration_slot(
                            stack, slot, slot_first_ns,
                            {"path_id": path["path_id"],
                             "channel": path["channel"]},
                            (gain_db, ordinal), expected_digest,
                            phase_label,
                        )
                        if record is None:
                            decode_fail += 1
                            decode_fail_ordinals.append(int(ordinal))
                            _dump_iq("judgefail", gain_db, ordinal, slot,
                                     slot_first_ns)
                            if not judge_all:
                                break
                            continue
                        _dump_iq("ok", gain_db, ordinal, slot, slot_first_ns)
                        accepted += 1
            summary = {
                "gain_db": gain_db, "applied_db": applied,
                "captured": captured, "accepted": accepted,
                "metrics_fail": metrics_fail, "decode_fail": decode_fail,
                "overload": step_overload, "window": raw is not None,
            }
            if judge_all:
                summary["decode_fail_ordinals"] = decode_fail_ordinals
            if raw is not None:
                summary["start_delta_ns"] = (
                    int(first_ns) - (int(step_hw_times[0]) - pad_ns)
                )
                if decode_fail and not step_overload:
                    _dump_window(gain_db, raw, first_ns, step_hw_times)
            if raw is not None and fail_detail:
                summary["metrics_fail_detail"] = fail_detail
            if raw is not None and judge_from and warm_metrics:
                summary["warmup_metrics"] = warm_metrics
            step_summaries.append(summary)
            self._progress(f"gain-step {gain_db}dB")
            if step_overload:
                overload = True
                stop_event.set()
                break
            if accepted >= burst_count - judge_from:
                candidate = gain_db
                stop_event.set()
                break
        # 잔여 캡처 항목을 비워 스레드를 종료시킨다.
        stop_event.set()
        while True:
            try:
                leftover = step_queue.get_nowait()
            except _queue.Empty:
                break
            if leftover is None:
                break
        capture_thread.join(timeout=60.0)
        return candidate, overload, step_summaries

    def _gain_revalidate(
        self, plan, body, worker, graph, burst, expected_digest,
        lane_candidates, search_pass,
    ):
        """§9.2 공통 gain 재검증: endpoint 공통 gain(max)으로 각 RX lane을
        100 burst 재검증한다. APPLY_AND_REVERIFY에서는 operational config의
        고정 gain만 사용한다. 성공 시 공통 gain(dB)을, 실패/불요 시 None을
        반환한다 (재검증 path가 없는 plan이면 None이 정상)."""

        reval_paths = plan.get("revalidation_paths") or ()
        if not reval_paths:
            return None
        mode = body["calibration_mode"]
        my_rx_lanes = sorted(
            {p["rx_lane_index"] for p in reval_paths
             if p["rx_endpoint_id"] == self.endpoint_id}
        )
        if mode in ("SEARCH_AND_SEAL", "ADAPTIVE_SEARCH_AND_SEAL"):
            if my_rx_lanes and any(l not in lane_candidates for l in my_rx_lanes):
                common_db = None
            else:
                common_db = max(
                    (lane_candidates[l] for l in my_rx_lanes), default=None
                )
        else:
            common_db = self.resolved.endpoint_common_rx_gain_db
        phase_label = f"{mode}:REVALIDATE"
        # 자기 탐색이 실패했어도 TX 의무는 수행한다 — 스킵하면 다른
        # endpoint의 재검증 evidence가 무신호로 오염되어 진단을 가린다
        # (판정은 어차피 이 endpoint의 실패로 닫힌다).
        all_ok = bool(search_pass)
        for entry in reval_paths:
            entry_deadline = int(entry.get("deadline_host_raw_ns", 0)) or None
            if entry_deadline is not None:
                from .hw_operations import read_monotonic_raw_ns

                if read_monotonic_raw_ns() >= entry_deadline:
                    self.evidence.append(
                        {"kind": "gain_revalidate_deadline",
                         "path_id": entry["path_id"]}
                    )
                    all_ok = False
                    break
            times = [int(v) for v in entry["scheduled_host_raw_ns"]]
            if entry["tx_endpoint_id"] == self.endpoint_id:
                lane = entry["tx_lane_index"]
                other_lane = 1 - lane
                restore_lo = self._tx_upper_retune(
                    worker, entry["tx_endpoint_id"], entry["rx_endpoint_id"]
                )
                worker.set_tx_mute(other_lane, True)
                last_tx_time = None
                skipped_past = 0
                try:
                    # 스케줄 5s 전까지 host 시계로 대기 후 변환 (드리프트 방지).
                    sched = entry.get("scheduled_host_raw_ns")
                    if sched:
                        self._wait_host_until(int(sched[0]) - 5_000_000_000)
                    for hardware_time in self._plan_times_to_hardware(worker, entry):
                        if int(worker.hardware_time_ns()) > hardware_time - 50_000_000:
                            skipped_past += 1
                            continue
                        self._wait_until_hardware(worker, hardware_time - 1_500_000_000)
                        worker.submit_tx_group(((lane, burst, hardware_time),))
                        last_tx_time = hardware_time
                        self._progress("reval-tx")
                finally:
                    # 잔여 선등록 burst 방사 종료 후에만 LO 복원 (gaincal3
                    # 실측: C5/C6 reval tail 13/7슬롯이 조기 retune으로 소실).
                    if last_tx_time is not None:
                        self._wait_until_hardware(worker, last_tx_time + 100_000_000)
                    worker.set_tx_mute(other_lane, False)
                    restore_lo()
                outcome_counts = {}
                for event in worker.poll_async():
                    key = str(event.get("outcome"))
                    outcome_counts[key] = outcome_counts.get(key, 0) + 1
                self.evidence.append(
                    {"kind": "gain_revalidate_tx", "path_id": entry["path_id"],
                     "count": len(times), "skipped_past_due": skipped_past,
                     "async_outcomes": outcome_counts}
                )
            elif entry["rx_endpoint_id"] == self.endpoint_id:
                if common_db is None:
                    # 자기 탐색 실패: 판정할 공통 gain이 없다. TX 의무는
                    # 계속 수행해야 하므로 loop는 이어간다.
                    all_ok = False
                    continue
                lane = entry["rx_lane_index"]
                stack = graph["rf_a_stack"] if lane == 0 else graph["rf_b_stack"]
                step = {"gain_db": common_db,
                        "scheduled_host_raw_ns": entry["scheduled_host_raw_ns"]}
                # v0.9.2(사용자 승인 2026-08-27): plan이 고립 no-decode
                # 허용치를 주면 judge_all로 끝까지 세고 evaluate_reval_window
                # 로 판정한다. 허용치 0이면 종전 무결점 판정 그대로다.
                tolerance = int(entry.get("reval_max_no_decode", 0))
                evidence_mark = len(self.evidence)
                candidate, overload, steps = self._gain_rx_run(
                    worker, stack, lane, entry, (step,), phase_label,
                    expected_digest,
                    judge_from=int(entry.get("warmup_count", 0)),
                    judge_all=tolerance > 0,
                )
                applied = steps[0]["applied_db"] if steps else None
                readback_ok = (
                    applied is not None and abs(float(applied) - common_db) <= 0.5
                )
                if tolerance > 0:
                    summary_step = steps[0] if steps else {}
                    # v0.9.3: digest 게이트가 거부한 FEC 오복원은 '수용된
                    # 오염'이 아니라 결손 슬롯이다 — 해당 슬롯은 이미
                    # decode_fail_ordinals에 계수돼 있으므로 별도 거부권을
                    # 주지 않고 evidence로만 남긴다. 수용된 오염은 구조적
                    # 으로 0(digest 불일치 payload는 수용 경로가 없다).
                    rejected_mis_recovered = 0
                    for item in self.evidence[evidence_mark:]:
                        if item.get("kind") != "decode_fail_detail":
                            continue
                        if str(item.get("ordinal", "")).startswith("('warm'"):
                            continue
                        for attempt in item.get("attempts") or ():
                            if attempt.get("has_digest") and not attempt.get("digest_match"):
                                rejected_mis_recovered += 1
                    expected_judged = len(step["scheduled_host_raw_ns"]) - int(
                        entry.get("warmup_count", 0)
                    )
                    passed = (
                        not overload and readback_ok
                        and int(summary_step.get("captured", 0)) == expected_judged
                        and int(summary_step.get("metrics_fail", 0)) == 0
                        and evaluate_reval_window(
                            summary_step.get("decode_fail_ordinals") or (),
                            0, tolerance,
                        )
                    )
                    self.evidence.append(
                        {"kind": "gain_revalidate_loss_accounting",
                         "path_id": entry["path_id"],
                         "fail_ordinals": summary_step.get("decode_fail_ordinals") or [],
                         "rejected_mis_recovered": rejected_mis_recovered,
                         "accepted_wrong_bytes": 0}
                    )
                else:
                    passed = candidate == common_db and not overload and readback_ok
                self.evidence.append(
                    {"kind": "gain_revalidate", "path_id": entry["path_id"],
                     "lane_index": lane, "common_db": common_db,
                     "applied_db": applied, "readback_ok": readback_ok,
                     "overload": overload, "passed": passed, "steps": steps}
                )
                if passed:
                    self.sealed_rx_gains[(entry["path_id"], lane)] = common_db
                    worker.set_rx_gain(lane, common_db)
                all_ok = all_ok and passed
        if not all_ok:
            return None
        if my_rx_lanes:
            self.evidence.append(
                {"kind": "gain_common", "common_db": common_db,
                 "lane_candidates": {str(k): v for k, v in sorted(lane_candidates.items())}}
            )
            return common_db
        # RX lane이 없는 endpoint(현 topology에는 없음)는 재검증 대상이 없다.
        return common_db

    # --- RF timestamp (§13.3) ------------------------------------------------------

    def _rf_time_correction(self, phase: str) -> tuple[int, int]:
        if phase == "POST_CORRECTION" and self.active_correction is not None:
            return self.active_correction
        return (0, 1)

    def _host_raw_to_hardware(self, host_raw_ns: int, phase: str):
        if self.provisional_fit is None:
            return None
        coefficients = self.provisional_fit.coefficients
        return coefficients.inverse_corrected(
            int(host_raw_ns), *self._rf_time_correction(phase)
        )

    def _hardware_to_host_raw(self, hardware_ns: int, phase: str):
        coefficients = self.provisional_fit.coefficients
        return coefficients.forward_corrected(
            int(hardware_ns), *self._rf_time_correction(phase)
        )

    def validate_rf_time(self, body):
        from .hw_operations import link11_calibration_packet_sha256

        try:
            plan = _load_plan(self.run_root, "rf_time_plan.json", body["rf_time_plan_sha256"])
        except Exception as error:  # noqa: BLE001
            self.evidence.append({"kind": "rf_time_plan_error", "error": str(error)})
            return None
        phase = body["validation_phase"]
        if self.provisional_fit is None:
            self.evidence.append({"kind": "rf_time_no_fit", "phase": phase})
            return None
        worker = self.radio.lease.worker
        burst = link11_calibration_burst_device_bytes()
        expected_digest = link11_calibration_packet_sha256()
        summaries = []
        for entry in plan["paths"]:
            times = [int(value) for value in entry["scheduled_host_raw_ns"]]
            if entry["tx_endpoint_id"] == self.endpoint_id:
                lane = entry["tx_lane_index"]
                margins_ms = []
                outcomes = []
                skipped_past = 0
                # 직전 경로(이 endpoint가 RX였던 창의 decode 꼬리 등)에서
                # 남은 async event를 이 경로 판정에 섞지 않는다 (run l 실측:
                # C2 TX flood의 TIME_ERROR가 C5 poll에 계상됨).
                stale_drained = len(list(worker.poll_async()))
                restore_lo = self._tx_upper_retune(
                    worker, entry["tx_endpoint_id"], entry["rx_endpoint_id"]
                )
                try:
                    for host_time in times:
                        hardware_time = self._host_raw_to_hardware(host_time, phase)
                        # 이미 기한이 지난 burst는 제출하지 않는다 — UHD가
                        # 전부 TIME_ERROR로 버리며 flood만 만든다 (§9.2 TX
                        # 루프와 동일 위생; run l RAW_PHY 실측).
                        if int(worker.hardware_time_ns()) > hardware_time - 50_000_000:
                            skipped_past += 1
                            continue
                        self._wait_until_hardware(worker, hardware_time - 1_500_000_000)
                        margins_ms.append(
                            round((hardware_time - int(worker.hardware_time_ns())) / 1e6, 1)
                        )
                        worker.submit_tx_group(((lane, burst, hardware_time),))
                        self._progress("rf-time-tx")
                    # 마지막 burst 종료까지 대기 후 async 판정을 수집한다.
                    self._wait_until_hardware(
                        worker,
                        self._host_raw_to_hardware(times[-1], phase) + 1_000_000_000,
                    )
                finally:
                    restore_lo()
                for event in worker.poll_async():
                    outcomes.append(event.get("outcome"))
                self.evidence.append(
                    {"kind": "rf_time_tx", "path_id": entry["path_id"],
                     "phase": phase, "count": len(times),
                     "skipped_past_due": skipped_past,
                     "stale_async_drained": stale_drained,
                     "submit_margins_ms": margins_ms,
                     "async_outcomes": outcomes[-24:]}
                )
                if phase == "RAW_PHY":
                    outcome_counts = {}
                    for value in outcomes:
                        key = str(value)
                        outcome_counts[key] = outcome_counts.get(key, 0) + 1
                    tx_summary = {
                        "summary_type": "STAGE7_RAW_PHY_TX_SUMMARY_V1",
                        "path_id": entry["path_id"], "role": "tx",
                        "tx_submitted": len(times) - skipped_past,
                        "skipped_past_due": skipped_past,
                        "async_outcome_counts": outcome_counts,
                        "outcome": "PASS" if not any(
                            k in outcome_counts for k in
                            ("TIME_ERROR", "UNDERFLOW", "OVERFLOW", "STREAM_ERROR")
                        ) else "FAIL",
                    }
                    summaries.append(tx_summary)
                    self.evidence.append(
                        {"kind": "rf_time_summary", **tx_summary}
                    )
            elif entry["rx_endpoint_id"] == self.endpoint_id:
                if phase == "RAW_PHY":
                    summary = self._measure_raw_phy_path(entry, expected_digest)
                else:
                    summary = self._measure_rf_time_path(
                        entry, times, phase, expected_digest, body
                    )
                summaries.append(summary)
                # 경로별 즉시 기록: 뒤 경로에서 예외가 나도 앞 evidence가
                # 소실되지 않는다 (rr-p2-17 run c 실측).
                self.evidence.append({"kind": "rf_time_summary", **summary})
        return summaries

    def _measure_raw_phy_path(self, entry, expected_digest):
        """v0.9.1 D항: 한 directed path의 raw PHY 100 burst 실측.

        sealed 공통 gain에서 step-창을 캡처해 warm-up 제외 판정 슬롯 전부를
        decode하고 decoded/no-decode/wrong-byte를 센다 (첫 실패 중단 없음)."""

        graph = self.endpoint.stage7_graph
        lane = entry["rx_lane_index"]
        stack = graph["rf_a_stack"] if lane == 0 else graph["rf_b_stack"]
        worker = self.radio.lease.worker
        sealed = self.sealed_rx_gains.get((entry["path_id"], lane))
        if sealed is None:
            # calibrate 재검증이 이 run에서 seal하지 않은 lane — 측정 불가.
            return {
                "summary_type": "STAGE7_RAW_PHY_SUMMARY_V1",
                "path_id": entry["path_id"], "outcome": "FAIL",
                "reason": "no_sealed_gain", "tx_planned": len(entry["scheduled_host_raw_ns"]),
                "decoded_ok": 0, "wrong_bytes": 0, "no_decode": 0, "captured": 0,
            }
        warm = int(entry.get("warmup_count", 3))
        step = {"gain_db": int(sealed),
                "scheduled_host_raw_ns": entry["scheduled_host_raw_ns"]}
        evidence_mark = len(self.evidence)
        candidate, overload, steps = self._gain_rx_run(
            worker, stack, lane, entry, (step,), "RAW_PHY", expected_digest,
            judge_from=warm, judge_all=True,
        )
        summary_step = steps[-1] if steps else {}
        # v0.9.3: digest 게이트가 거부한 FEC 오복원은 결손(no_decode)으로
        # 계수한다 — '수용된' 오염 payload는 구조적으로 0이다 (§17A.3
        # "accepted wrong bytes"의 해석 명문화).
        rejected_mis_recovered = 0
        for item in self.evidence[evidence_mark:]:
            if item.get("kind") != "decode_fail_detail":
                continue
            for attempt in item.get("attempts") or ():
                if attempt.get("has_digest") and not attempt.get("digest_match"):
                    rejected_mis_recovered += 1
        decoded_ok = int(summary_step.get("accepted", 0))
        judged = int(summary_step.get("captured", 0))
        no_decode = max(0, judged - decoded_ok)
        outcome = "PASS" if (
            not overload and judged == 100 and decoded_ok >= 90
        ) else "FAIL"
        return {
            "summary_type": "STAGE7_RAW_PHY_SUMMARY_V1",
            "path_id": entry["path_id"],
            "outcome": outcome,
            "gain_db": int(sealed),
            "captured": judged,
            "decoded_ok": decoded_ok,
            "no_decode": no_decode,
            "wrong_bytes": 0,
            "rejected_mis_recovered": rejected_mis_recovered,
            "overload": bool(overload),
            "metrics_fail": int(summary_step.get("metrics_fail", 0)),
        }

    def _measure_rf_time_path(self, entry, times, phase, expected_digest, body):
        from .hw_operations import capture_slot_window
        from .rf_time import build_rf_time_measurement_summary

        graph = self.endpoint.stage7_graph
        lane = entry["rx_lane_index"]
        stack = graph["rf_a_stack"] if lane == 0 else graph["rf_b_stack"]
        worker = self.radio.lease.worker
        from .hw_operations import (
            SLOT_SAMPLES,
            DEVICE_RATE,
            measure_gain_step_metrics_fast as measure_gain_step_metrics_fast_local,
        )

        slot_duration_ns = (SLOT_SAMPLES * 1_000_000_000 + DEVICE_RATE - 1) // DEVICE_RATE
        d_values = []
        raw_hashes = []
        window_start_offsets = []
        window_rms_dbfs = []
        decode_count = 0
        continuity_ok = True
        for ordinal, host_time in enumerate(times):
            # 슬롯 처리 전체(변환/캡처/metric/decode)를 예외 격리한다 —
            # 예외는 해당 슬롯 결손으로 계수하고 traceback을 evidence에
            # 남긴다 (§13.3 결손 예산이 판정; 반복되면 decode_count 미달로
            # 정당히 FAIL). rr-p2-17 run c/e 실측: 슬롯 처리 예외가 barrier
            # 까지 전파되어 summary가 소실되고 원인이 은폐됐다.
            try:
                hardware_expected = self._host_raw_to_hardware(host_time, phase)
                data, actual_first_ns = capture_slot_window(
                    worker, lane, hardware_expected
                )
                self._progress("rf-time-rx")
                if data is None:
                    continuity_ok = False
                    continue
                window_start_offsets.append(
                    int(actual_first_ns) - int(hardware_expected)
                )
                raw_hashes.append(hashlib.sha256(data).hexdigest())
                # canonical JSON은 float를 금지한다 — 십진 문자열 (§16).
                window_rms_dbfs.append(
                    f"{measure_gain_step_metrics_fast_local(data)['rms_dbfs']:.1f}"
                )
                record = self._decode_calibration_slot(
                    stack, data, int(actual_first_ns), entry, ordinal,
                    expected_digest, phase,
                )
                if record is None:
                    continue
                m_rx = self._hardware_to_host_raw(
                    record.rx_sample0_hardware_ns, phase
                )
            except Exception:  # noqa: BLE001 - 예외는 결손 슬롯로 계수
                import traceback as _tb

                self.evidence.append(
                    {"kind": "rf_time_slot_exception",
                     "path_id": entry["path_id"], "phase": phase,
                     "ordinal": ordinal,
                     "traceback": _tb.format_exc()[-1500:]}
                )
                continue
            decode_count += 1
            d_values.append(int(m_rx) - int(host_time))
        # §13.3 "모든 실제 RX window가 frozen slot/guard 안": burst 포함성은
        # decode가 구조적으로 증명하고, 여기서는 복원된 sample-0 지연 d_k가
        # 양의 chain 지연으로서 한 slot 길이 안인지 확인한다 (140 µs 급의
        # 정상 TX+RX 체인 지연은 P_ij로 다뤄지는 §13.3 설계 대상이다).
        # §13.3 원문: "모든 실제 RX window가 frozen slot/guard 안" — 판정
        # 대상은 캡처 창 배치이며, 개별 d_k(D_ij)는 endpoint 시계 편향
        # O_ij를 포함해 부호를 가질 수 있다 (§13.3의 O/P 수학이 signed D를
        # 전제; 양수 요구는 pair 평균 P_ij에만 있다). 종전 `0 <= d` 하한은
        # 오구현으로 fit 오프셋 오차(±수백 µs)에 의해 부호가 무작위인 D를
        # 동전던지기 판정으로 만들었다 (rr-p1 run k 실측: −50/−126 µs FAIL,
        # +99..+836 µs PASS — 전부 동일 오차 범위).
        slot_ok = (
            bool(d_values)
            and all(
                -slot_duration_ns < value < slot_duration_ns
                for value in d_values
            )
            and all(
                abs(int(offset)) <= 2_000_000
                for offset in window_start_offsets
            )
        )
        artifact = {
            "artifact_type": "STAGE7_RF_TIME_RAW_V1",
            "run_id": self.resolved.run_id,
            "path_id": entry["path_id"],
            "validation_phase": phase,
            "d_values_ns": list(d_values),
            "window_start_offsets_ns": window_start_offsets,
            "window_rms_dbfs": window_rms_dbfs,
            "raw_window_sha256": raw_hashes,
            "decode_count": decode_count,
        }
        artifact_bytes = encode_canonical_json(artifact)
        artifact_dir = os.path.join(self.run_root, "evidence")
        os.makedirs(artifact_dir, exist_ok=True)
        artifact_path = os.path.join(
            artifact_dir, f"rf-time-{entry['path_id']}-{phase}.json"
        )
        with open(artifact_path, "wb") as handle:
            handle.write(artifact_bytes)
        return build_rf_time_measurement_summary(
            run_id=self.resolved.run_id,
            validation_phase=phase,
            path_id=entry["path_id"],
            radio_generation=self.radio.lease.radio_generation,
            lane_generation=graph["lane_generation"],
            bridge_generation=None if phase == "PRE_CORRECTION" else self.installed_bridge_generation,
            clock_id=body["clock_id"],
            d_values_ns=d_values,
            scheduled_first_ns=times[0],
            scheduled_last_ns=times[-1],
            phy_sample_rate_hz=76_800,
            decode_count=decode_count,
            timestamps_continuous=continuity_ok,
            windows_in_slot=slot_ok,
            raw_artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        )

    def _decode_calibration_slot(
        self, stack, raw_cf32le, first_hardware_ns, entry, ordinal, expected_digest,
        phase,
    ):
        from ..common.contracts.phy import PhyDecodeRequest
        from ..common.contracts.radio import (
            SAMPLE_FORMAT_CF32LE,
            SampleBlock,
            SampleContinuity,
        )
        from ..common.identifiers import LinkId, PhysicalChannel
        from ..common.time import ClockDomain, TimePoint
        from ..phy import resampling

        raw_host = self._hardware_to_host_raw(first_hardware_ns, "PRE_CORRECTION")
        utc_ns = self.anchor.utc_label(raw_host) if self.anchor is not None else raw_host
        first_sample_at = TimePoint(
            utc_ns // 1_000_000_000,
            utc_ns % 1_000_000_000,
            ClockDomain.UTC,
            "stage7-rf-time-provisional",
        )
        device_request = PhyDecodeRequest(
            channel=PhysicalChannel(entry["channel"]),
            samples=SampleBlock(
                sample_format=SAMPLE_FORMAT_CF32LE,
                sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
                sample_count=resampling.DEVICE_SLOT_SAMPLES,
                data=raw_cf32le,
                first_sample_at=first_sample_at,
                continuity=SampleContinuity.CONTIGUOUS,
            ),
            allowed_link_ids=(LinkId(11),),
        )
        def fail_detail(stage, **extra):
            self.evidence.append(
                {"kind": "decode_fail_detail", "stage": stage, "phase": phase,
                 "path_id": entry["path_id"], "ordinal": repr(ordinal), **extra}
            )
            return None

        native = stack.bridge.to_native_decode_request(device_request)
        if native.is_err:
            return fail_detail("bridge", error=str(native.error))
        if native.value is None:
            return fail_detail("bridge_incomplete_window")
        # sink 기록은 run 동안 누적되므로 PRE/POST가 같은 key를 쓰면 POST의
        # 유일성 검사(len==1)가 PRE 기록에 걸려 전 창이 탈락한다 — phase 포함.
        window_key = ("stage7-rf-time", phase, entry["path_id"], ordinal)
        sink = stack.facade.diagnostic_sink
        sink.begin_window(window_key, ordinal, first_hardware_ns)
        try:
            decoded = stack.facade.decode_samples(native.value)
        finally:
            sink.end_window()
        if decoded.is_err:
            return fail_detail("decode", error=str(decoded.error))
        window_records = sink.records_for_window(window_key)
        records = [
            record
            for record in window_records
            if record.packet_digest == expected_digest
        ]
        if len(records) != 1:
            return fail_detail(
                "records",
                match_count=len(records),
                first_hardware_ns=int(first_hardware_ns),
                raw_host_ns=int(raw_host),
                utc_ns=int(utc_ns),
                anchor_present=self.anchor is not None,
                attempts=[
                    {"status": record.estimator_status,
                     "digest_match": record.packet_digest == expected_digest,
                     "has_digest": record.packet_digest is not None,
                     "cfo_hz": f"{record.cfo_hz:.1f}"}
                    for record in window_records
                ],
            )
        return records[0]

    # --- active bridge -------------------------------------------------------------

    def check_active_candidate(self, body) -> bool:
        return self.provisional_fit is not None

    def install_active_bridge(self, body) -> bool:
        if self.provisional_fit is None or self.anchor is None:
            return False
        coefficients = self.provisional_fit.coefficients
        self.radio.bridge.install(
            state="ACTIVE",
            anchor=self.anchor,
            coefficients=coefficients,
            correction=(body["correction_num"], body["correction_den"]),
            clock_id=body["active_clock_id"],
            generation=body["bridge_generation"],
        )
        self.active_correction = (body["correction_num"], body["correction_den"])
        self.installed_bridge_generation = body["bridge_generation"]
        self.evidence.append(
            {"kind": "active_bridge_installed", "generation": body["bridge_generation"]}
        )
        return True

    def abort_active_bridge(self, body) -> None:
        self.radio.bridge.invalidate()

    # --- stop/inhibit ---------------------------------------------------------------

    def do_stop(self, body) -> None:
        self.evidence.append({"kind": "stop", "frame": body.get("stop_at_frame_ordinal")})
        try:
            self.radio.close_qualification_lease()
        except Exception:  # noqa: BLE001
            pass

    def do_inhibit(self, body) -> None:
        worker = self.radio.lease.worker
        if worker is not None:
            try:
                worker.set_tx_mute(0, True)
                worker.set_tx_mute(1, True)
            except Exception:  # noqa: BLE001
                pass
        self.evidence.append({"kind": "inhibit", "private_code": body.get("private_code")})


__all__ = ("Stage7PiOperations",)
