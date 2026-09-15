"""TC-MAC-013: 폐쇄형 group과 direct MASS↔MASS 방향."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
    build_stage4_software_group,
    deployment_profile,
    drive_stage4_group,
    start_stage4_group,
)
from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.mac_execution import MacReasonCode
from vdeter.common.contracts.service import (
    ServiceRole,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import PayloadFormat, StationId
from vdeter.mac.deployment import (
    EndpointDescriptor,
    build_canonical_deployment_profile,
    validate_endpoint_group,
)
from vdeter.mac.routing import (
    ShortDataDirection,
    resolve_data_session_plan,
    resolve_short_data_direction,
)
from vdeter.mac.tbb import build_canonical_tbb_profile


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage4-test:lower-bound"


class Stage4RoutingTest(unittest.TestCase):
    """TC-MAC-013의 방향 판정과 direct wire 경로."""

    def test_TC_MAC_013_closed_group_direction_and_relay_decisions(self):
        profile = deployment_profile()
        direct_route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(MASS_B_STATION_ID)),
        )
        short = resolve_short_data_direction(
            profile=profile,
            local_station_id=MASS_A_STATION_ID,
            route=direct_route,
            ack_required=True,
        )
        self.assertTrue(short.is_ok)
        self.assertIs(ShortDataDirection.MASS_TO_MASS, short.value.direction)
        session = resolve_data_session_plan(
            profile=profile, local_station_id=MASS_A_STATION_ID, route=direct_route
        )
        self.assertTrue(session.is_ok)
        self.assertTrue(session.value.relay_required)
        outside = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(StationId(444444444))),
        )
        self.assertTrue(resolve_short_data_direction(
            profile=profile,
            local_station_id=MASS_A_STATION_ID,
            route=outside,
            ack_required=False,
        ).is_err)

    def test_TC_MAC_013_group_validation_and_deployment_golden_vector(self):
        profile = deployment_profile()
        canonical_tbb = build_canonical_tbb_profile(profile)
        descriptors = (
            EndpointDescriptor(MASS_A_STATION_ID, ServiceRole.SHIP, profile),
            EndpointDescriptor(ROC_STATION_ID, ServiceRole.CONTROL, profile),
            EndpointDescriptor(MASS_B_STATION_ID, ServiceRole.SHIP, profile),
        )
        fingerprints = (canonical_tbb.canonical_tbb_fingerprint,) * 3
        validated = validate_endpoint_group(descriptors, fingerprints)
        self.assertTrue(validated.is_ok, getattr(validated, "error", None))
        self.assertEqual(1, len({item.deployment_fingerprint for item in validated.value}))
        canonical_deployment = build_canonical_deployment_profile(
            profile, canonical_tbb.canonical_tbb_fingerprint
        )
        self.assertEqual(
            b'{"canonical_tbb_fingerprint":"30278f63fe65f9b361f3b231b2ada10ab8c65f3a1f28c5de5328ea8005901db3","expected_tbb_certificate_fingerprint":"a1b2c3d4e5f6","member_station_ids":[111111111,222222222,333333333],"roc_station_id":222222222,"schema":"VDETER-STAGE4-DEPLOYMENT-V1","service_area_mode":"INSIDE_CONFIGURED","tbb_control_station_id":7,"type0_override":null}',
            canonical_deployment.canonical_bytes,
        )
        self.assertEqual(
            "caf903911895c4707e8dad28fe0c9efa0ac45cd48ccbe441858281e8da2a7f7b",
            canonical_deployment.deployment_fingerprint,
        )

        role_matrices = (
            tuple(replace(item, role=ServiceRole.SHIP) for item in descriptors),
            (
                replace(descriptors[0], role=ServiceRole.CONTROL),
                descriptors[1],
                descriptors[2],
            ),
            (
                replace(descriptors[0], role=ServiceRole.CONTROL),
                replace(descriptors[1], role=ServiceRole.SHIP),
                descriptors[2],
            ),
        )
        for index, invalid in enumerate(role_matrices):
            with self.subTest(role_matrix=index):
                self.assertTrue(
                    validate_endpoint_group(invalid, fingerprints).is_err
                )

        wrong_set = (
            descriptors[0],
            descriptors[1],
            replace(descriptors[2], station_id=StationId(444444444)),
        )
        self.assertTrue(validate_endpoint_group(wrong_set, fingerprints).is_err)
        mismatched_profile = replace(
            profile, expected_tbb_certificate_fingerprint=bytes.fromhex("010203040506")
        )
        self.assertTrue(
            validate_endpoint_group(
                (descriptors[0], descriptors[1], replace(descriptors[2], profile=mismatched_profile)),
                fingerprints,
            ).is_err
        )
        self.assertTrue(
            validate_endpoint_group(
                descriptors,
                (fingerprints[0], fingerprints[1], "f" * 64),
            ).is_err
        )

        for members in (
            (MASS_A_STATION_ID, ROC_STATION_ID, ROC_STATION_ID),
            (ROC_STATION_ID, MASS_A_STATION_ID, MASS_B_STATION_ID),
        ):
            with self.subTest(members=tuple(item.value for item in members)):
                with self.assertRaises(InvalidValueError):
                    deployment_profile(member_station_ids=members)

    def test_TC_MAC_013_short_data_addressing_and_broadcast_matrix(self):
        profile = deployment_profile()

        def route(source, destination, *, node_source=None, node_destination=None):
            return RouteAddresses(
                original_source=OriginalSource(source),
                original_destination=OriginalDestination(destination),
                node_source=node_source,
                node_destination=node_destination,
            )

        directions = (
            (ROC_STATION_ID, Addressed(MASS_A_STATION_ID), ShortDataDirection.ROC_TO_MASS),
            (MASS_A_STATION_ID, Addressed(ROC_STATION_ID), ShortDataDirection.MASS_TO_ROC),
            (MASS_A_STATION_ID, Addressed(MASS_B_STATION_ID), ShortDataDirection.MASS_TO_MASS),
        )
        for source, destination, expected in directions:
            selected = resolve_short_data_direction(
                profile=profile,
                local_station_id=source,
                route=route(source, destination),
                ack_required=True,
            )
            self.assertIs(expected, selected.value.direction)

        type92_broadcast = resolve_short_data_direction(
            profile=profile,
            local_station_id=ROC_STATION_ID,
            route=route(ROC_STATION_ID, Broadcast()),
            ack_required=True,
        )
        self.assertTrue(type92_broadcast.is_err)
        roc_broadcast = resolve_short_data_direction(
            profile=profile,
            local_station_id=ROC_STATION_ID,
            route=route(ROC_STATION_ID, Broadcast()),
            ack_required=False,
        )
        self.assertIs(ShortDataDirection.ROC_BROADCAST, roc_broadcast.value.direction)
        mass_broadcast = resolve_short_data_direction(
            profile=profile,
            local_station_id=MASS_A_STATION_ID,
            route=route(MASS_A_STATION_ID, Broadcast()),
            ack_required=False,
        )
        self.assertEqual(
            MacReasonCode.MASS_BROADCAST_NOT_SUPPORTED,
            mass_broadcast.error.details.get("reason_code"),
        )
        mismatched_node = resolve_short_data_direction(
            profile=profile,
            local_station_id=MASS_A_STATION_ID,
            route=route(
                MASS_A_STATION_ID,
                Addressed(MASS_B_STATION_ID),
                node_source=NodeSource(MASS_A_STATION_ID),
                node_destination=NodeDestination(Addressed(ROC_STATION_ID)),
            ),
            ack_required=False,
        )
        self.assertTrue(mismatched_node.is_err)

    def test_TC_MAC_013_direct_mass_to_mass_type92_uses_upper_rac_and_ack(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        source = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID)
        target = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == MASS_B_STATION_ID)
        accepted = source.submit(SubmitTransferRequest(
            destination=Addressed(MASS_B_STATION_ID),
            payload_format=PayloadFormat("test.stage0.opaque"),
            payload=b"direct-mass",
            ack_required=True,
        ))
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        source.stage4.pump()
        drive_stage4_group(clock, endpoints, 60)
        status = source.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertIs(TransferState.COMPLETED, status.value.state)
        received = [
            event.received_data.payload
            for event in target.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertIn(b"direct-mass", received)


if __name__ == "__main__":
    unittest.main()
