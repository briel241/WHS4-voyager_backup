"""TC-PAYLOAD-003: SC-VDE Payload 공개 Service API software end-to-end.

요구사항: `PAYLOAD-REQ-01/04/06`, `DR-PAYLOAD-008`
(`payload_extension_baseline.md` §6.4.4, §9, §11, §13.1
`SVC-01`, `SHORT-01`, `SESSION-01`, `BYTE-01`)

- command n=2 (wire 32 bytes): `ack_required` 양 값 모두 Short Data
- command n=3 (wire 42 bytes)·n=16 (wire 167 bytes): addressed Data Session과
  fragmentation/reassembly의 byte-exact 복원
- ACK (wire 6 bytes): 항상 Short Data. Data Session 사례는 크기상 존재할 수 없으므로
  `NOT_APPLICABLE` 계산 근거를 assertion으로 남긴다 (§11)
- MF 3 application ACK는 자동·재귀 생성되지 않는다
- 송신 broadcast는 공개 transfer ID 없이 거부되고, 수신 broadcast의 known SC-VDE
  wire는 `DATA_RECEIVED` 없이 protocol 오류로 거부된다
"""

from __future__ import annotations

import unittest

from tests.support.stage4 import Type0OverrideProfile
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    received_payloads,
    start_stage5_group,
)
from tests.unit.test_payload_sc_vde_route_control import (
    pack_ack_application,
    pack_command_application,
)
from vdeter.presentation.codecs.sc_vde_route_control import command_application_bytes
from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import InternalEventType, ServiceEventType
from vdeter.common.identifiers import PayloadFormat

COMMAND = PayloadFormat("sc_vde.route_control.command")
ACK = PayloadFormat("sc_vde.route_control.ack")
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)

#: 현재 Link ID 11 Short Data 한도 (`stage5` §11): ACK 있음 33, ACK 없음 34 bytes.
SHORT_DATA_ACK_LIMIT = 33
SHORT_DATA_NOACK_LIMIT = 34


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "payload-scvde-test:lower-bound"


def _waypoints(count: int) -> tuple:
    return tuple(
        (77_430_000 + index, 21_060_000 + index, 30, 60, index % 3)
        for index in range(count)
    )


def _sc_vde_received(endpoint):
    return [
        event.received_data
        for event in endpoint.dependencies.service_event_sink.events()
        if event.event_type is ServiceEventType.DATA_RECEIVED
        and event.received_data is not None
        and event.received_data.payload_format in (COMMAND, ACK)
    ]


def _decoded_message_types(endpoint, mark: int) -> set:
    decoded = set()
    for event in endpoint.dependencies.internal_event_sink.events()[mark:]:
        if event.event_type is InternalEventType.MESSAGES_DECODED:
            for message in event.payload.messages:
                decoded.add(message.message_type)
    return decoded


class ScVdeEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = build_stage5_software_group(
            type0_override=FAST_TYPE0,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage5_group(cls.clock, cls.group)
        cls.by_station = cls.group.by_station()
        cls.roc = cls.by_station[ROC_STATION_ID.value]
        cls.mass = cls.by_station[MASS_A_STATION_ID.value]

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    def _transfer(self, source, destination, destination_station, payload_format, payload, *, ack):
        before = len(received_payloads(destination))
        destination_mark = len(destination.dependencies.internal_event_sink.events())
        accepted = source.submit(
            SubmitTransferRequest(
                destination=Addressed(destination_station),
                payload_format=payload_format,
                payload=payload,
                ack_required=ack,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        presentation = source._pending_presentation.get(accepted.value.transfer_id.value)
        self.assertIsNotNone(presentation)
        state = None
        for _ in range(600):
            drive_stage5_group(self.clock, self.group, 1)
            state = source.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value.state
            if (
                state is TransferState.COMPLETED
                and received_payloads(destination)[before:].count(payload) == 1
            ):
                break
        self.assertIs(TransferState.COMPLETED, state)
        # `BYTE-01`: 원본 application bytes의 길이·모든 byte가 그대로 복원된다.
        received = received_payloads(destination)[before:]
        self.assertEqual([payload], received)
        event = [
            item
            for item in destination.dependencies.service_event_sink.events()
            if item.event_type is ServiceEventType.DATA_RECEIVED
            and item.received_data is not None
            and item.received_data.payload == payload
        ][-1]
        self.assertEqual(payload_format, event.received_data.payload_format)
        return presentation, _decoded_message_types(destination, destination_mark)

    def test_TC_PAYLOAD_003_command_n2_short_data_both_ack_modes(self):
        # `SHORT-01`: n=2 wire 32 bytes는 ACK 있음(33)·없음(34) 한도 안이다.
        for ack, expected_type in ((True, 92), (False, 93)):
            with self.subTest(ack_required=ack):
                payload = pack_command_application(waypoints=_waypoints(2))
                presentation, decoded = self._transfer(
                    self.roc, self.mass, MASS_A_STATION_ID, COMMAND, payload, ack=ack
                )
                self.assertEqual(32, len(presentation.data))
                self.assertEqual(b"\xff\x7f\x02", presentation.data[:3])
                self.assertEqual(payload, presentation.data[3:])
                self.assertEqual(16383, presentation.vpfi.value)
                self.assertEqual(2, presentation.message_format)
                limit = SHORT_DATA_ACK_LIMIT if ack else SHORT_DATA_NOACK_LIMIT
                self.assertLessEqual(len(presentation.data), limit)
                # 수신 endpoint가 해당 Short Data message type을 실제로 decode했다.
                self.assertIn(expected_type, decoded)

    def test_TC_PAYLOAD_003_command_n0_and_n1_short_data_boundaries(self):
        # §11: n=0(wire 13)·n=1(wire 23)도 Short Data 대상 경계로 시험한다.
        for count, ack in ((0, False), (1, True)):
            with self.subTest(count=count, ack_required=ack):
                payload = pack_command_application(waypoints=_waypoints(count))
                presentation, decoded = self._transfer(
                    self.roc, self.mass, MASS_A_STATION_ID, COMMAND, payload, ack=ack
                )
                self.assertEqual(
                    command_application_bytes(count) + 3, len(presentation.data)
                )
                limit = SHORT_DATA_ACK_LIMIT if ack else SHORT_DATA_NOACK_LIMIT
                self.assertLessEqual(len(presentation.data), limit)
                self.assertIn(92 if ack else 93, decoded)

    def test_TC_PAYLOAD_003_command_n3_uses_addressed_data_session(self):
        # `SESSION-01`: n=3 wire 42 bytes는 Short Data 한도(33/34) 밖이다.
        payload = pack_command_application(waypoints=_waypoints(3))
        presentation, decoded = self._transfer(
            self.roc, self.mass, MASS_A_STATION_ID, COMMAND, payload, ack=True
        )
        self.assertEqual(42, len(presentation.data))
        self.assertGreater(len(presentation.data), SHORT_DATA_NOACK_LIMIT)
        # Data Session 자원할당(Type 4)이 수신 endpoint에서 decode됐다.
        self.assertIn(4, decoded)

    def test_TC_PAYLOAD_003_command_n16_session_fragmentation_round_trip(self):
        # `SESSION-01` 큰 정상값: n=16 wire 167 bytes의 fragmentation/reassembly.
        payload = pack_command_application(waypoints=_waypoints(16))
        self.assertEqual(164, len(payload))
        presentation, decoded = self._transfer(
            self.roc, self.mass, MASS_A_STATION_ID, COMMAND, payload, ack=False
        )
        self.assertEqual(167, len(presentation.data))
        self.assertIn(4, decoded)

    def test_TC_PAYLOAD_003_ack_short_data_both_ack_modes_and_session_na(self):
        for ack in (True, False):
            with self.subTest(ack_required=ack):
                payload = pack_ack_application(linkage=7, status=4, reason=0)
                presentation, decoded = self._transfer(
                    self.mass, self.roc, ROC_STATION_ID, ACK, payload, ack=ack
                )
                self.assertEqual(6, len(presentation.data))
                self.assertEqual(b"\xff\x7f\x03", presentation.data[:3])
                self.assertEqual(3, presentation.message_format)
                self.assertIn(92 if ack else 93, decoded)
        # `SESSION-01` NOT_APPLICABLE 계산 근거: MF 3 wire는 고정 6 bytes로 Short
        # Data 한도(33/34)를 넘을 수 없어 Data Session 대상 사례가 존재하지 않는다.
        self.assertLessEqual(6, SHORT_DATA_ACK_LIMIT)
        self.assertLess(6, SHORT_DATA_NOACK_LIMIT)

    def test_TC_PAYLOAD_003_mf3_is_not_generated_automatically_or_recursively(self):
        # `DR-PAYLOAD-008`: Link `ack_required=True` command 전달이 MF 3을 만들지
        # 않고, application이 보낸 MF 3에 대한 재귀 MF 3도 생기지 않는다.
        roc_ack_before = [
            item for item in _sc_vde_received(self.roc) if item.payload_format == ACK
        ]
        command_payload = pack_command_application(waypoints=_waypoints(2))
        self._transfer(
            self.roc, self.mass, MASS_A_STATION_ID, COMMAND, command_payload, ack=True
        )
        drive_stage5_group(self.clock, self.group, 40)
        roc_ack_after_command = [
            item for item in _sc_vde_received(self.roc) if item.payload_format == ACK
        ]
        self.assertEqual(len(roc_ack_before), len(roc_ack_after_command))
        # application이 명시적으로 submit한 MF 3 하나만 ROC에 도착한다.
        mass_command_count = len(
            [item for item in _sc_vde_received(self.mass) if item.payload_format == COMMAND]
        )
        ack_payload = pack_ack_application(linkage=7, status=4, reason=0)
        self._transfer(
            self.mass, self.roc, ROC_STATION_ID, ACK, ack_payload, ack=True
        )
        drive_stage5_group(self.clock, self.group, 40)
        roc_ack_final = [
            item for item in _sc_vde_received(self.roc) if item.payload_format == ACK
        ]
        self.assertEqual(len(roc_ack_before) + 1, len(roc_ack_final))
        # MF 3 전달(ack_required=True 포함)이 추가 SC-VDE 메시지를 유발하지 않는다.
        mass_after = [
            item for item in _sc_vde_received(self.mass) if item.payload_format == COMMAND
        ]
        self.assertEqual(mass_command_count, len(mass_after))
        self.assertEqual(
            [], [item for item in _sc_vde_received(self.mass) if item.payload_format == ACK]
        )

    def test_TC_PAYLOAD_003_disallowed_application_length_is_rejected_in_phase_a(self):
        # §9.1: 허용 집합 밖 길이는 Phase A `encoded_size_bound`에서 거부된다.
        for payload_format, payload in (
            (COMMAND, b"\x00" * 11),
            (COMMAND, b"\x00" * 30),
            (ACK, b"\x00" * 2),
            (ACK, b"\x00" * 4),
        ):
            with self.subTest(payload_format=payload_format.value, size=len(payload)):
                result = self.roc.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_A_STATION_ID),
                        payload_format=payload_format,
                        payload=payload,
                        ack_required=False,
                    )
                )
                self.assertTrue(result.is_err)
                self.assertEqual("INVALID_REQUEST", result.error.code)
                self.assertIsNone(result.error.transfer_id)

    def test_TC_PAYLOAD_003_broadcast_submit_is_rejected_without_public_id(self):
        # §6.4.4: 송신 broadcast는 admission 전에 거부되고 공개 transfer ID,
        # Service record와 하위 wire 작업을 남기지 않는다.
        for payload_format, payload in (
            (COMMAND, pack_command_application(waypoints=_waypoints(2))),
            (ACK, pack_ack_application()),
        ):
            with self.subTest(payload_format=payload_format.value):
                nonterminal_before = self.roc._nonterminal_count_locked()
                transfers_before = set(self.roc._transfers)
                result = self.roc.submit(
                    SubmitTransferRequest(
                        destination=Broadcast(),
                        payload_format=payload_format,
                        payload=payload,
                        ack_required=False,
                    )
                )
                self.assertTrue(result.is_err)
                self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)
                self.assertIsNone(result.error.transfer_id)
                self.assertEqual(
                    nonterminal_before, self.roc._nonterminal_count_locked()
                )
                self.assertEqual(transfers_before, set(self.roc._transfers))

    def test_TC_PAYLOAD_003_broadcast_rx_of_known_sc_vde_wire_is_not_delivered(self):
        # §6.4.4: 수신 broadcast의 known MF 2/3 wire는 protocol 구조 오류로 거부하고
        # `DATA_RECEIVED`를 만들지 않는다.
        broadcast_route = RouteAddresses(
            original_source=OriginalSource(ROC_STATION_ID),
            original_destination=OriginalDestination(Broadcast()),
        )
        command_wire = b"\xff\x7f\x02" + pack_command_application(
            waypoints=_waypoints(2)
        )
        ack_wire = b"\xff\x7f\x03" + pack_ack_application()
        for wire in (command_wire, ack_wire):
            with self.subTest(prefix=wire[:3].hex()):
                received_before = len(received_payloads(self.mass))
                result = self.mass._deliver_presentation_bytes(
                    broadcast_route, wire, None
                )
                self.assertTrue(result.is_err)
                self.assertEqual(
                    "PRESENTATION_STRUCTURE_INVALID", result.error.code
                )
                self.assertEqual(
                    received_before, len(received_payloads(self.mass))
                )

    def test_TC_PAYLOAD_003_public_dto_shape_is_unchanged(self):
        # `PAYLOAD-REQ-01`: 공개 submit DTO의 field set이 동결 계약 그대로다.
        self.assertEqual(
            ("destination", "payload_format", "payload", "ack_required"),
            SubmitTransferRequest.__slots__,
        )

    def test_TC_PAYLOAD_003_three_endpoints_share_the_approved_fingerprint(self):
        # §13.2: 세 endpoint 사이 registry fingerprint가 같고 §7.3 승인 TEST
        # 기대값과도 exact 일치한다.
        approved_test_fingerprint = (
            "438f4cc8207deb77b9994e24d57cf9b4279713e59bc2f94f2a9739d371dd8434"
        )
        fingerprints = {
            endpoint._presentation.registry_fingerprint()
            for endpoint in self.group.endpoints
        }
        self.assertEqual({approved_test_fingerprint}, fingerprints)


class ScVdeOperationalScopeServiceTest(unittest.TestCase):
    """`AUTH-SCOPE-01`/§9.1: OPERATIONAL scope endpoint의 SC-VDE 운영 사용 거부.

    TEST 전용 entry는 OPERATIONAL registry에 없으므로 submit이 Phase A
    `encoded_size_bound` lookup에서 `UNSUPPORTED_REQUEST`로 거부되고 공개
    transfer ID·Service record를 만들지 않는다.
    """

    def test_TC_PAYLOAD_003_operational_endpoint_rejects_sc_vde_submit_in_phase_a(self):
        from vdeter.profiles.models import RegistryScope

        clock, group = build_stage5_software_group(
            payload_scope=RegistryScope.OPERATIONAL, type0_override=FAST_TYPE0
        )
        try:
            start_stage5_group(clock, group)
            roc = group.by_station()[ROC_STATION_ID.value]
            mass = group.by_station()[MASS_A_STATION_ID.value]
            for source, destination_station, payload_format, payload in (
                (roc, MASS_A_STATION_ID, COMMAND, pack_command_application(waypoints=_waypoints(2))),
                (mass, ROC_STATION_ID, ACK, pack_ack_application()),
            ):
                with self.subTest(payload_format=payload_format.value):
                    transfers_before = set(source._transfers)
                    result = source.submit(
                        SubmitTransferRequest(
                            destination=Addressed(destination_station),
                            payload_format=payload_format,
                            payload=payload,
                            ack_required=False,
                        )
                    )
                    self.assertTrue(result.is_err)
                    self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)
                    self.assertIsNone(result.error.transfer_id)
                    self.assertEqual(transfers_before, set(source._transfers))
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
