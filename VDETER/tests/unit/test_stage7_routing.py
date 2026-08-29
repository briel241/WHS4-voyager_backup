"""Stage 7 route wrapper·sidecar 시험 (`stage7_b210_system_baseline.md` §15.1)."""

import hashlib
import unittest

from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    EncodedPacket,
    LinkBuildRequest,
    LinkPacketEncodeRequest,
    LinkTransferControlRequest,
    TbbEncodeRequest,
    TbbEncodedPacketSet,
    TransferControlKind,
    BuiltPacket,
)
from vdeter.common.contracts.mac import TransmissionPlan, TransmissionProcedure
from vdeter.common.contracts.message import (
    AcknowledgeNackBody,
    BulletinBoardBody,
    MediaAccessControlBody,
    MessageEncodeRequest,
    PaddingBody,
    ResourceAllocationBody,
    ResourceRequestBody,
    ShortDataAckBody,
)
from vdeter.common.errors import Correlation, ErrorCategory, ErrorCode, Layer
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PhysicalChannel,
    StationId,
)
from vdeter.common.results import Err, Ok, internal_error
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.stage7.canonical_json import sha256_hex
from vdeter.stage7.routing import (
    Stage7QualificationPhyRequest,
    Stage7QualificationRouteSeeder,
    Stage7RouteAwareLinkPort,
    Stage7RouteSidecar,
    Stage7SignedQualificationStep,
    canonical_request_digest,
    resolve_lane_plan_route,
)

MASS_A = 111_111_111
MASS_B = 333_333_333
ROC = 222_222_222
UTC = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "stage7-test-utc")
UPPER = "VDETER_UPPER_100_TEST"
LOWER = "VDETER_LOWER_100_TEST"

_ids = DeterministicIdSource(0x57A6E7)


def _plan(route):
    return TransmissionPlan(
        transfer_id=_ids.new_transfer_id(),
        link_id=LinkId(11),
        channel=PhysicalChannel(LOWER),
        transmit_at=UTC,
        procedure=TransmissionProcedure.SHORT_DATA,
        route=route,
    )


def _route(original_source, original_destination, node_source=None, node_destination=None):
    return RouteAddresses(
        original_source=OriginalSource(StationId(original_source)),
        original_destination=OriginalDestination(original_destination),
        node_source=None if node_source is None else NodeSource(StationId(node_source)),
        node_destination=(
            None if node_destination is None else NodeDestination(node_destination)
        ),
    )


def _encoded_packet(link_id=11):
    crc = b"\x01\x02\x03\x04"
    return EncodedPacket(
        packet_id=_ids.new_packet_id(),
        link_id=LinkId(link_id),
        packet=b"\x00" * 50 + crc,
        message_types=(4,),
        padding_size=0,
        crc=crc,
    )


class _FakeDelegate:
    """delegate 호출을 기록하고 준비된 결과를 돌려주는 시험 double."""

    def __init__(self):
        self.calls = []
        self.next_result = None

    def _respond(self, name, request):
        self.calls.append((name, request))
        if self.next_result is not None:
            result = self.next_result
            self.next_result = None
            return result
        if name == "build_packet":
            return Ok(
                BuiltPacket(
                    packet_id=_ids.new_packet_id(),
                    transfer_id=request.plan.transfer_id,
                    link_id=request.plan.link_id,
                    packet=b"\x00" * 8,
                )
            )
        if name == "encode_packet":
            return Ok(_encoded_packet())
        if name == "encode_tbb":
            return Ok(
                TbbEncodedPacketSet(
                    packets=(_encoded_packet(), _encoded_packet()),
                    certificate_fingerprint=b"\x01\x02\x03\x04\x05\x06",
                    authentication_sequence=b"\x00" * 64,
                )
            )
        return Ok(("passthrough", name))

    def analyze_transfer(self, request):
        return self._respond("analyze_transfer", request)

    def build_packet(self, request):
        return self._respond("build_packet", request)

    def encode_packet(self, request):
        return self._respond("encode_packet", request)

    def encode_tbb(self, request):
        return self._respond("encode_tbb", request)

    def decode_packet(self, request):
        return self._respond("decode_packet", request)

    def process_fragment(self, request):
        return self._respond("process_fragment", request)

    def control_reassembly(self, request):
        return self._respond("control_reassembly", request)

    def control_transfer(self, request):
        return self._respond("control_transfer", request)

    def control_tbb_assembly(self, request):
        return self._respond("control_tbb_assembly", request)


def _wrapper():
    delegate = _FakeDelegate()
    sidecar = Stage7RouteSidecar(lane_generation=0)
    wrapper = Stage7RouteAwareLinkPort(delegate, sidecar, roc_station_id=ROC)
    return delegate, sidecar, wrapper


def _private_code(error):
    return (error.details.as_dict() or {}).get("stage7_private_code")


class TC_STAGE7_ROUTE_EXTRACTION(unittest.TestCase):
    def test_build_packet_original_route(self):
        delegate, sidecar, wrapper = _wrapper()
        request = LinkBuildRequest(
            plan=_plan(_route(MASS_A, Addressed(StationId(ROC)))),
            presentation_data=b"\x01\x02",
        )
        result = wrapper.build_packet(request)
        self.assertTrue(result.is_ok)
        self.assertEqual(len(delegate.calls), 1)
        context = sidecar.lookup_packet(result.value.packet_id.value).value
        self.assertEqual(context.source_station_id, MASS_A)
        self.assertEqual(context.destination_key(), ("ADDRESSED", ROC))

    def test_build_packet_node_route_takes_precedence(self):
        delegate, sidecar, wrapper = _wrapper()
        request = LinkBuildRequest(
            plan=_plan(
                _route(
                    MASS_A,
                    Addressed(StationId(MASS_B)),
                    node_source=MASS_A,
                    node_destination=Addressed(StationId(ROC)),
                )
            ),
            presentation_data=b"\x01\x02",
        )
        result = wrapper.build_packet(request)
        self.assertTrue(result.is_ok)
        context = sidecar.lookup_packet(result.value.packet_id.value).value
        self.assertEqual(context.destination_key(), ("ADDRESSED", ROC))
        self.assertEqual(context.original_destination_station_id, MASS_B)

    def test_build_packet_half_node_route_is_ambiguous(self):
        delegate, _sidecar, wrapper = _wrapper()
        request = LinkBuildRequest(
            plan=_plan(
                _route(MASS_A, Addressed(StationId(ROC)), node_source=MASS_A)
            ),
            presentation_data=b"\x01\x02",
        )
        result = wrapper.build_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_AMBIGUOUS")
        self.assertEqual(delegate.calls, [])

    def test_encode_packet_type4_route(self):
        delegate, sidecar, wrapper = _wrapper()
        body = ResourceAllocationBody(
            source_id=StationId(ROC),
            destination=Addressed(StationId(MASS_A)),
            logical_channel_tx=1,
            logical_channel_rx=2,
            link_id=LinkId(17),
            tdma_frame_delay=1,
            session_id=0,
            cqi=0,
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(MessageEncodeRequest(body=body, correlation=Correlation()),),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_ok)
        context = sidecar.lookup_packet(result.value.packet_id.value).value
        self.assertEqual(context.source_station_id, ROC)
        self.assertEqual(context.destination_key(), ("ADDRESSED", MASS_A))

    def test_encode_packet_mixed_routes_rejected_before_delegate(self):
        delegate, _sidecar, wrapper = _wrapper()
        first = ResourceAllocationBody(
            source_id=StationId(ROC),
            destination=Addressed(StationId(MASS_A)),
            logical_channel_tx=1,
            logical_channel_rx=2,
            link_id=LinkId(17),
            tdma_frame_delay=1,
            session_id=0,
            cqi=0,
        )
        second = AcknowledgeNackBody(
            source_id=StationId(ROC),
            destination=Addressed(StationId(MASS_B)),
            session_id=0,
            ack_nack_masks=(0, 0, 0),
            cqi=0,
            acm_or_edn=0,
            power_setting=0,
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(
                MessageEncodeRequest(body=first, correlation=Correlation()),
                MessageEncodeRequest(body=second, correlation=Correlation()),
            ),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_AMBIGUOUS")
        self.assertEqual(delegate.calls, [])

    def test_encode_packet_roc_asc_type90_type4_pair_allowed(self):
        # frozen ROC ASC 경로는 [Type 90, Type 4]를 한 encode로 묶는다 (§15.1).
        # Type 4에는 original 의미가 없으므로 node hop이 같으면 혼합이 아니다.
        from vdeter.mac.resources import (
            build_allocation_body,
            build_resource_request_body,
        )

        delegate, sidecar, wrapper = _wrapper()
        announcement = build_resource_request_body(
            original_source=StationId(MASS_A),
            node_source=StationId(ROC),
            node_destination=Addressed(StationId(MASS_B)),
            original_destination=Addressed(StationId(MASS_B)),
        )
        allocation = build_allocation_body(
            source_id=StationId(ROC),
            destination=Addressed(StationId(MASS_B)),
            logical_channel_tx=1,
            logical_channel_rx=2,
            link_id=LinkId(17),
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(
                MessageEncodeRequest(body=announcement, correlation=Correlation()),
                MessageEncodeRequest(body=allocation, correlation=Correlation()),
            ),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        context = sidecar.lookup_packet(result.value.packet_id.value).value
        self.assertEqual(context.source_station_id, ROC)
        self.assertEqual(context.destination_key(), ("ADDRESSED", MASS_B))
        self.assertEqual(context.original_source_station_id, MASS_A)

    def test_encode_packet_conflicting_original_routes_rejected(self):
        from vdeter.mac.resources import build_resource_request_body

        delegate, _sidecar, wrapper = _wrapper()
        first = build_resource_request_body(
            original_source=StationId(MASS_A),
            node_source=StationId(ROC),
            node_destination=Addressed(StationId(MASS_B)),
            original_destination=Addressed(StationId(MASS_B)),
        )
        second = build_resource_request_body(
            original_source=StationId(MASS_B),
            node_source=StationId(ROC),
            node_destination=Addressed(StationId(MASS_B)),
            original_destination=Addressed(StationId(MASS_B)),
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(
                MessageEncodeRequest(body=first, correlation=Correlation()),
                MessageEncodeRequest(body=second, correlation=Correlation()),
            ),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_AMBIGUOUS")
        self.assertEqual(delegate.calls, [])

    def test_encode_packet_padding_only_missing_context(self):
        delegate, _sidecar, wrapper = _wrapper()
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(MessageEncodeRequest(body=PaddingBody(), correlation=Correlation()),),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_CONTEXT_MISSING")
        self.assertEqual(delegate.calls, [])

    def test_encode_packet_wrong_build_path_returns_frozen_error(self):
        delegate, _sidecar, wrapper = _wrapper()
        body = ShortDataAckBody(
            source_id=StationId(MASS_A),
            session_id=0,
            destination=Addressed(StationId(ROC)),
            retransmission_number=0,
            segment=b"\x01",
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(MessageEncodeRequest(body=body, correlation=Correlation()),),
            correlation=Correlation(),
        )
        delegate.next_result = internal_error(
            Layer.LINK,
            ErrorCode.LINK_MESSAGE_BUILD_PATH_REQUIRED,
            ErrorCategory.VALIDATION,
            "build path required",
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(result.error.code, ErrorCode.LINK_MESSAGE_BUILD_PATH_REQUIRED)
        self.assertEqual(len(delegate.calls), 1)

    def test_type0_non_roc_source_invalid(self):
        delegate, _sidecar, wrapper = _wrapper()
        body = MediaAccessControlBody(
            source_id=StationId(MASS_A),
            media_access_priority_level=0,
            random_access_selection_interval=1,
            short_data_message_limit=1,
            system_status=0,
        )
        request = LinkPacketEncodeRequest(
            link_id=LinkId(11),
            messages=(MessageEncodeRequest(body=body, correlation=Correlation()),),
            correlation=Correlation(),
        )
        result = wrapper.encode_packet(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_INVALID")
        self.assertEqual(delegate.calls, [])

    def test_encode_tbb_binds_all_packets_broadcast(self):
        delegate, sidecar, wrapper = _wrapper()
        request = TbbEncodeRequest(
            source_id=StationId(ROC),
            control_station_id=7,
            service_capability=2,
            bulletin_board_version=1,
            bulletin_board=_bulletin_board(),
            correlation=Correlation(),
        )
        result = wrapper.encode_tbb(request)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        for packet in result.value.packets:
            context = sidecar.lookup_packet(packet.packet_id.value).value
            self.assertEqual(context.destination_key(), ("BROADCAST", None))
            self.assertEqual(context.source_station_id, ROC)

    def test_encode_tbb_non_roc_rejected(self):
        delegate, _sidecar, wrapper = _wrapper()
        request = TbbEncodeRequest(
            source_id=StationId(MASS_A),
            control_station_id=7,
            service_capability=2,
            bulletin_board_version=1,
            bulletin_board=_bulletin_board(),
            correlation=Correlation(),
        )
        result = wrapper.encode_tbb(request)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_INVALID")
        self.assertEqual(delegate.calls, [])

    def test_passthrough_methods_exactly_once(self):
        delegate, _sidecar, wrapper = _wrapper()
        request = LinkTransferControlRequest(
            kind=TransferControlKind.FAIL,
            transfer_id=_ids.new_transfer_id(),
            session_handle=None,
            packet_progress=(),
            correlation=Correlation(),
        )
        result = wrapper.control_transfer(request)
        self.assertTrue(result.is_ok)
        self.assertEqual(len(delegate.calls), 1)
        self.assertIs(delegate.calls[0][1], request)

    def test_request_digest_is_deterministic(self):
        request = LinkBuildRequest(
            plan=_plan(_route(MASS_A, Addressed(StationId(ROC)))),
            presentation_data=b"\x01\x02",
        )
        self.assertEqual(
            canonical_request_digest(request), canonical_request_digest(request)
        )


def _bulletin_board():
    from vdeter.mac.tbb import build_canonical_tbb_profile

    from tests.support.stage4 import deployment_profile

    return build_canonical_tbb_profile(deployment_profile()).bulletin_board


class TC_STAGE7_LANE_PLAN(unittest.TestCase):
    def _context(self, source, destination_kind, destination=None, method="build_packet"):
        from vdeter.stage7.routing import Stage7RouteContext

        return Stage7RouteContext(
            method_kind=method,
            source_station_id=source,
            destination_kind=destination_kind,
            destination_station_id=destination,
            original_source_station_id=None,
            original_destination_kind=None,
            original_destination_station_id=None,
            request_digest="0" * 64,
        )

    def test_roc_broadcast_two_children(self):
        resolved, error = resolve_lane_plan_route(
            "ROC", self._context(ROC, "BROADCAST"), UPPER
        )
        self.assertIsNone(error)
        children, skipped, scope = resolved
        self.assertEqual(children, ((0, "C1"), (1, "C3")))
        self.assertEqual(skipped, ())
        self.assertEqual(scope, "TWO_CHILD_BROADCAST")

    def test_roc_addressed_mass_b_single_child(self):
        resolved, error = resolve_lane_plan_route(
            "ROC", self._context(ROC, "ADDRESSED", MASS_B), UPPER
        )
        self.assertIsNone(error)
        self.assertEqual(resolved[0], ((1, "C3"),))

    def test_mass_a_to_roc_lower(self):
        resolved, error = resolve_lane_plan_route(
            "MASS-A", self._context(MASS_A, "ADDRESSED", ROC), LOWER
        )
        self.assertIsNone(error)
        self.assertEqual(resolved[0], ((0, "C2"),))

    def test_mass_direct_upper_rf_b(self):
        resolved, error = resolve_lane_plan_route(
            "MASS-A", self._context(MASS_A, "ADDRESSED", MASS_B), UPPER
        )
        self.assertIsNone(error)
        self.assertEqual(resolved[0], ((1, "C5"),))
        resolved, error = resolve_lane_plan_route(
            "MASS-B", self._context(MASS_B, "ADDRESSED", MASS_A), UPPER
        )
        self.assertIsNone(error)
        self.assertEqual(resolved[0], ((1, "C6"),))

    def test_mass_broadcast_invalid(self):
        resolved, error = resolve_lane_plan_route(
            "MASS-A", self._context(MASS_A, "BROADCAST"), UPPER
        )
        self.assertIsNone(resolved)
        self.assertEqual(_private_code(error.error), "LANE_ROUTE_INVALID")

    def test_roc_lower_tx_invalid(self):
        resolved, error = resolve_lane_plan_route(
            "ROC", self._context(ROC, "ADDRESSED", MASS_A), LOWER
        )
        self.assertIsNone(resolved)
        self.assertEqual(_private_code(error.error), "LANE_ROUTE_INVALID")

    def test_pairwise_edge_single_child(self):
        resolved, error = resolve_lane_plan_route(
            "ROC", self._context(ROC, "BROADCAST"), UPPER, pairwise_edge="MASS-B"
        )
        self.assertIsNone(error)
        children, skipped, scope = resolved
        self.assertEqual(children, ((1, "C3"),))
        self.assertEqual(skipped, ((0, "C1", "SKIPPED_ABSENT_BY_PLAN"),))
        self.assertEqual(scope, "PAIRWISE_SINGLE_CHILD")


class TC_STAGE7_SIDECAR_LIFECYCLE(unittest.TestCase):
    def test_burst_binding_and_reservation(self):
        delegate, sidecar, wrapper = _wrapper()
        request = LinkBuildRequest(
            plan=_plan(_route(MASS_A, Addressed(StationId(ROC)))),
            presentation_data=b"\x01\x02",
        )
        packet_id = wrapper.build_packet(request).value.packet_id
        burst_id = _ids.new_burst_id()
        bound = sidecar.bind_burst(packet_id.value, burst_id.value)
        self.assertTrue(bound.is_ok)
        plan = sidecar.reserve_burst(burst_id.value, "MASS-A", LOWER)
        self.assertTrue(plan.is_ok)
        self.assertEqual(plan.value.children, ((0, "C2"),))
        # RESERVED 중 재-reserve는 거부한다.
        duplicate = sidecar.reserve_burst(burst_id.value, "MASS-A", LOWER)
        self.assertTrue(duplicate.is_err)
        sidecar.terminal_burst(burst_id.value, "COMPLETED")
        replay = sidecar.reserve_burst(burst_id.value, "MASS-A", LOWER)
        self.assertTrue(replay.is_ok)

    def test_burst_without_packet_entry_missing(self):
        _delegate, sidecar, _wrapper_ = _wrapper()
        result = sidecar.bind_burst("0" * 36, "1" * 36)
        self.assertTrue(result.is_err)
        self.assertEqual(_private_code(result.error), "LANE_ROUTE_CONTEXT_MISSING")

    def test_restart_discards_old_generation(self):
        delegate, sidecar, wrapper = _wrapper()
        request = LinkBuildRequest(
            plan=_plan(_route(MASS_A, Addressed(StationId(ROC)))),
            presentation_data=b"\x01\x02",
        )
        packet_id = wrapper.build_packet(request).value.packet_id
        sidecar.restart(1)
        self.assertEqual(sidecar.lane_generation, 1)
        stale = sidecar.lookup_packet(packet_id.value)
        self.assertTrue(stale.is_err)
        self.assertEqual(_private_code(stale.error), "LANE_ROUTE_CONTEXT_MISSING")
        with self.assertRaises(ValueError):
            sidecar.restart(1)


class TC_STAGE7_QUALIFICATION_SEEDER(unittest.TestCase):
    def _step(self, packet: bytes):
        return Stage7SignedQualificationStep(
            step_id="step-c2-l11",
            plan_sha256="a" * 64,
            source_endpoint_id="MASS-A",
            destination_endpoint_id="ROC",
            path_id="C2",
            link_id=11,
            channel_name=LOWER,
            packet_sha256=sha256_hex(packet),
            run_mode="GAIN_CALIBRATION",
        )

    def _request(self, packet: bytes):
        return Stage7QualificationPhyRequest(
            step_id="step-c2-l11",
            source_endpoint_id="MASS-A",
            destination_endpoint_id="ROC",
            path_id="C2",
            link_id=11,
            channel_name=LOWER,
            transmit_at=TimePoint(1_700_000_100, 0, ClockDomain.UTC, "pair-clock"),
            packet=packet,
        )

    def test_signed_step_seeds_once(self):
        packet = b"\x00" * 54
        sidecar = Stage7RouteSidecar()
        seeder = Stage7QualificationRouteSeeder(
            sidecar,
            (self._step(packet),),
            endpoint_id="MASS-A",
            run_mode="GAIN_CALIBRATION",
            deterministic_seed=7,
            expected_clock_id="pair-clock",
        )

        captured = {}

        class _FakeFacade:
            def encode_packet(self, request):
                captured["request"] = request
                return Ok("encoded")

        result = seeder.seed(self._request(packet), _FakeFacade())
        self.assertTrue(result.is_ok)
        self.assertEqual(seeder.call_count, 1)
        entry = sidecar.lookup_packet(captured["request"].packet_id.value)
        self.assertTrue(entry.is_ok)
        self.assertEqual(entry.value.destination_key(), ("ADDRESSED", ROC))

    def test_wrong_packet_bytes_rejected(self):
        packet = b"\x00" * 54
        sidecar = Stage7RouteSidecar()
        seeder = Stage7QualificationRouteSeeder(
            sidecar,
            (self._step(packet),),
            endpoint_id="MASS-A",
            run_mode="GAIN_CALIBRATION",
            deterministic_seed=7,
            expected_clock_id="pair-clock",
        )
        request = self._request(b"\xff" * 54)
        result = seeder.seed(request, None)
        self.assertTrue(result.is_err)
        self.assertEqual(seeder.call_count, 0)

    def test_unknown_step_rejected(self):
        packet = b"\x00" * 54
        sidecar = Stage7RouteSidecar()
        seeder = Stage7QualificationRouteSeeder(
            sidecar,
            (),
            endpoint_id="MASS-A",
            run_mode="GAIN_CALIBRATION",
            deterministic_seed=7,
            expected_clock_id="pair-clock",
        )
        result = seeder.seed(self._request(packet), None)
        self.assertTrue(result.is_err)

    def test_wrong_clock_rejected(self):
        packet = b"\x00" * 54
        sidecar = Stage7RouteSidecar()
        seeder = Stage7QualificationRouteSeeder(
            sidecar,
            (self._step(packet),),
            endpoint_id="MASS-A",
            run_mode="GAIN_CALIBRATION",
            deterministic_seed=7,
            expected_clock_id="pair-clock",
        )
        request = Stage7QualificationPhyRequest(
            step_id="step-c2-l11",
            source_endpoint_id="MASS-A",
            destination_endpoint_id="ROC",
            path_id="C2",
            link_id=11,
            channel_name=LOWER,
            transmit_at=TimePoint(1_700_000_100, 0, ClockDomain.UTC, "other-clock"),
            packet=packet,
        )
        result = seeder.seed(request, None)
        self.assertTrue(result.is_err)

    def test_normal_run_mode_cannot_assemble(self):
        with self.assertRaises(ValueError):
            Stage7QualificationRouteSeeder(
                Stage7RouteSidecar(),
                (),
                endpoint_id="MASS-A",
                run_mode="RF_OFF_QUALIFICATION",
                deterministic_seed=7,
                expected_clock_id="x",
            )


if __name__ == "__main__":
    unittest.main()
