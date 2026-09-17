"""Stage 7 endpoint 조립·adoption·RX fanout 스모크 (`stage7` §15)."""

import hashlib
import threading
import time
import unittest

from vdeter.common.contracts.radio import (
    RadioConfigureRequest,
    RadioDeviceState,
    RadioRxRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioTxRequest,
)
from vdeter.common.contracts.service import (
    EndpointHealth,
    ServiceRole,
    SubmitTransferRequest,
)
from vdeter.common.identifiers import PayloadFormat, StationId
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.common.addressing import Addressed
from vdeter.security.dependencies import E2eSecurityDependencies
from vdeter.b210_system.configuration import ConfigurationLoader
from vdeter.b210_system.time_bridge import AffineCoefficients, HostUtcAnchor
from vdeter.b210_system.wiring import RestartFactory, build_b210_endpoint

from tests.support.stage4 import (
    HAS_CRYPTO,
    crypto_dependencies,
    deployment_profile,
)
from tests.support.stage7 import stage7_test_signer
from tests.unit.test_stage7_configuration import (
    DATA_PROFILE_17,
    build_mass_b_source,
    build_roc_source,
    build_source,
)


class _FakeDualWorker:
    """실제 UHD 없이 dual-lane worker RPC surface를 흉내내는 시험 double."""

    instances = []
    serials = ("34D03DD", "3515425", "34D03E8")

    def __init__(self, device_selector, cpu_format, otw_format, **_kwargs):
        self.device_selector = device_selector
        self.serial = device_selector.split("=", 1)[1]
        self.submitted_tx = []
        self.submitted_rx = []
        self.closed = False
        self.started = False
        self._time0 = time.monotonic_ns()
        type(self).instances.append(self)

    @classmethod
    def discover(cls, device_selector):
        serial = device_selector.split("=", 1)[1]
        return (serial,) if serial in cls.serials else ()

    def configure_dual(self, plan):
        self.plan = plan
        lanes = {}
        for lane in ("0", "1"):
            lanes[lane] = {
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
        self.started = True

    def start_rx_continuous(self, start_hardware_time_ns=None):
        # 연속-RX 서비스 모드(radio_adapter가 start()에서 호출).
        self.rx_continuous = True
        return True

    def register_rx_window_lane(self, lane, hardware_time_ns, sample_count):
        self.submitted_rx.append((lane, hardware_time_ns, sample_count))
        return True

    def unregister_rx_lane(self, lane, hardware_time_ns=None):
        return True

    def hardware_time_ns(self):
        return time.monotonic_ns() - self._time0

    def submit_tx_group(self, bursts):
        self.submitted_tx.append(bursts)

    def submit_rx_lane(self, lane, hardware_time_ns, sample_count):
        self.submitted_rx.append((lane, hardware_time_ns, sample_count))

    def read_rx_cf32_lane(self, lane, sample_count, allow_partial=False, expected_hardware_time_ns=None):
        return None

    def poll_async(self):
        return ()

    def wait_for_activity(self, timeout_seconds):
        time.sleep(min(timeout_seconds, 0.001))

    def close(self, timeout_ns):
        self.closed = True
        return True


def _install_identity_bridge(endpoint, *, now_utc_ns):
    """label==raw, raw==hardware+offset의 identity affine bridge를 설치한다."""

    radio = endpoint.dependencies.radio
    anchor = HostUtcAnchor(
        host_raw_anchor_ns=now_utc_ns, host_utc_anchor_ns=now_utc_ns, bracket_width_ns=100
    )
    coefficients = AffineCoefficients(
        h0_ns=0, m0_ns=now_utc_ns - 5_000_000_000, alpha_num=0, alpha_den=1,
        beta_num=1, beta_den=1,
    )
    radio.bridge.install(
        state="ACTIVE",
        anchor=anchor,
        coefficients=coefficients,
        correction=(0, 1),
        clock_id="stage7-pi-host-affine-v1:stage7-unit-run:0",
        generation=0,
    )


@unittest.skipUnless(HAS_CRYPTO, "cryptography extra가 필요합니다")
class TC_STAGE7_WIRING_SMOKE(unittest.TestCase):
    def _build(self, source_builder=build_source, endpoint_station=111111111, *, e2e_security=None):
        loader = ConfigurationLoader()
        resolved = loader.load_endpoint_configuration(
            source_builder(), DATA_PROFILE_17, run_id="stage7-unit-run"
        )
        self.assertTrue(resolved.is_ok, getattr(resolved, "error", None))
        signer, certificate_der, fingerprint = stage7_test_signer()
        deployment = deployment_profile(expected_fingerprint=fingerprint)
        is_control = endpoint_station == 222222222
        crypto = crypto_dependencies(signer, certificate_der, is_control=is_control)
        built = build_b210_endpoint(
            resolved.value,
            deployment,
            e2e_security=e2e_security if e2e_security is not None else E2eSecurityDependencies.disabled(),
            crypto_dependencies=crypto,
            worker_factory=_FakeDualWorker,
            host_raw_clock=time.monotonic_ns,
        )
        self.assertTrue(built.is_ok, getattr(built, "error", None))
        return built.value

    @staticmethod
    def _open_submit_admission(endpoint):
        endpoint.public_service_published = True
        endpoint._started = True
        endpoint._admission_open = True
        endpoint._health = EndpointHealth.RUNNING

    def test_build_and_graph_validation(self):
        endpoint = self._build()
        self.assertIsNone(endpoint._validate_effective_graph())
        graph = endpoint.b210_graph
        self.assertIsNot(graph["rf_a_stack"], graph["rf_b_stack"])
        self.assertIs(endpoint._phy, graph["rf_a_stack"].facade)
        self.assertIs(endpoint._phy_radio_bridge, graph["rf_a_stack"].bridge)

    def test_enforced_b210_presentation_roundtrip_restart_authentication_and_replay(self):
        from tests.support.security import make_test_e2e_by_station
        from vdeter.common.addressing import OriginalSource, OriginalDestination, RouteAddresses
        from vdeter.common.contracts.presentation import PresentationEncodeRequest, PresentationDecodeRequest
        from vdeter.common.errors import ErrorCode
        from vdeter.common.identifiers import TransferId
        from uuid import uuid4

        dependencies = make_test_e2e_by_station({111111111: "MASS-A", 333333333: "MASS-B", 222222222: "ROC"})
        try:
            mass = self._build(e2e_security=dependencies[111111111])
            roc = self._build(build_roc_source, 222222222, e2e_security=dependencies[222222222])
            route = RouteAddresses(OriginalSource(StationId(111111111)),
                                   OriginalDestination(Addressed(StationId(222222222))))
            body = bytes.fromhex("01d000")
            request = PresentationEncodeRequest(TransferId(str(uuid4())), route,
                                                PayloadFormat("sc_vde.route_control.ack"), body)
            first = mass._presentation.encode_payload(request)
            self.assertTrue(first.is_ok, getattr(first, "error", None))
            decoded = roc._presentation.decode_payload(PresentationDecodeRequest(route, first.value.data))
            self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
            self.assertEqual(decoded.value.application_payload, body)
            replay = roc._presentation.decode_payload(PresentationDecodeRequest(route, first.value.data))
            self.assertEqual(replay.error.code, ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE)

            with mass._service_lock:
                mass._reset_stage5_runtime_locked()
            second = mass._presentation.encode_payload(request)
            self.assertTrue(second.is_ok, getattr(second, "error", None))
            self.assertEqual(int.from_bytes(second.value.data[6:10], "big"),
                             int.from_bytes(first.value.data[6:10], "big") + 1)
            wire = second.value.data
            tampered = wire[:-1] + bytes([wire[-1] ^ 1])
            rejected = roc._presentation.decode_payload(PresentationDecodeRequest(route, tampered))
            self.assertEqual(rejected.error.code, ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED)
            self.assertTrue(roc._presentation.decode_payload(PresentationDecodeRequest(route, wire)).is_ok)
        finally:
            for dependency in dependencies.values():
                dependency.presentation_security.close_final(timeout_seconds=5)

    def test_public_guard_rejects_before_publish(self):
        endpoint = self._build()
        result = endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"\x01",
                ack_required=False,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(result.error.code, "SERVICE_NOT_STARTED")

    def test_data_session_submission_is_admitted(self):
        endpoint = self._build()
        self._open_submit_admission(endpoint)

        result = endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=bytes(range(128)),
                ack_required=True,
            )
        )

        self.assertTrue(result.is_ok, getattr(result, "error", None))

    def test_lease_adoption_and_rx_fanout(self):
        endpoint = self._build()
        radio = endpoint.dependencies.radio
        opened = radio.open_qualification_lease(lease_id="lease-1")
        self.assertTrue(opened.is_ok, getattr(opened, "error", None))
        now_utc_ns = endpoint.dependencies.time_source.now_utc().total_nanoseconds()
        _install_identity_bridge(endpoint, now_utc_ns=now_utc_ns)
        configured = radio.configure(
            RadioConfigureRequest(configuration=endpoint._b210_resolved.radio)
        )
        self.assertTrue(configured.is_ok, getattr(configured, "error", None))
        self.assertEqual(configured.value.adapter_kind, "b210-stage7-affine")
        self.assertEqual(configured.value.device_state, RadioDeviceState.CONFIGURED)
        started = radio.start(RadioStartRequest(reason="unit"))
        self.assertTrue(started.is_ok, getattr(started, "error", None))
        # timing status는 §13.2.1 고정 투영이어야 한다.
        snapshot = radio.status(RadioStatusRequest()).value
        self.assertEqual(snapshot.timing_status.mapping_state.value, "UNAVAILABLE")
        self.assertIsNone(snapshot.timing_status.mapping)
        # logical RX 요청 하나가 두 lane child로 fan-out된다.
        worker = _FakeDualWorker.instances[-1]
        start_at = endpoint.dependencies.time_source.now_utc().shifted(3_000_000_000)
        rx = radio.request_rx(
            RadioRxRequest(
                channel=endpoint._b210_resolved.radio.channels[1].channel,
                start_at=start_at,
                duration_nanoseconds=26_666_667,
                sample_rate_hz=307_200,
            )
        )
        self.assertTrue(rx.is_ok, getattr(rx, "error", None))
        self.assertEqual(len(worker.submitted_rx), 2)
        self.assertEqual({lane for lane, _, _ in worker.submitted_rx}, {0, 1})
        stopped = radio.stop(__import__("vdeter.common.contracts.radio", fromlist=["RadioStopRequest"]).RadioStopRequest(reason="unit"))
        self.assertTrue(stopped.is_ok, getattr(stopped, "error", None))
        self.assertTrue(worker.closed)

    def test_restart_factory_rebuilds_new_generation(self):
        endpoint = self._build()
        graph_before = dict(endpoint.b210_graph)
        factory = endpoint._b210_restart_factory
        self.assertIsInstance(factory, RestartFactory)
        with endpoint._service_lock:
            endpoint._reset_stage5_runtime_locked()
        graph_after = endpoint.b210_graph
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(graph_after["lane_generation"], graph_before["lane_generation"] + 1)
        self.assertIsNot(graph_after["rf_a_stack"], graph_before["rf_a_stack"])
        self.assertIsNot(graph_after["rf_b_stack"], graph_before["rf_b_stack"])
        self.assertIsNone(endpoint._validate_effective_graph())

    def test_restart_factory_ten_cycle_loop(self):
        # §18 restart 내구: 10회 연속 재조립에도 graph 검증·sidecar 연속성 유지.
        endpoint = self._build()
        base_generation = endpoint.b210_graph["lane_generation"]
        sidecar = endpoint.b210_graph["sidecar"]
        # 이전 stack 객체 참조를 유지해야 id() 재사용으로 인한 오판이 없다.
        seen_stacks = [(endpoint.b210_graph["rf_a_stack"], endpoint.b210_graph["rf_b_stack"])]
        for cycle in range(1, 11):
            with endpoint._service_lock:
                endpoint._reset_stage5_runtime_locked()
            graph = endpoint.b210_graph
            self.assertEqual(graph["lane_generation"], base_generation + cycle)
            self.assertIs(graph["sidecar"], sidecar)
            self.assertEqual(sidecar.lane_generation, base_generation + cycle)
            for old_a, old_b in seen_stacks:
                self.assertIsNot(graph["rf_a_stack"], old_a)
                self.assertIsNot(graph["rf_b_stack"], old_b)
            seen_stacks.append((graph["rf_a_stack"], graph["rf_b_stack"]))
            self.assertIsNone(endpoint._validate_effective_graph())
        self.assertEqual(endpoint._b210_restart_factory.call_count, 10)

    def test_roc_build(self):
        endpoint = self._build(source_builder=build_roc_source, endpoint_station=222222222)
        self.assertIsNone(endpoint._validate_effective_graph())

    def test_mass_b_build(self):
        endpoint = self._build(source_builder=build_mass_b_source, endpoint_station=333333333)
        self.assertIsNone(endpoint._validate_effective_graph())


if __name__ == "__main__":
    unittest.main()
