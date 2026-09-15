"""Stage 7 software 기능 시나리오 (`stage7` §17.2, §18.3 software 판정 부분).

RUNNING group 하나 위에서 addressed one-hop, ACK, direct MASS↔MASS(retune),
Data Session과 relay를 public `VdeService`/`ServiceEventSink`만으로 수행한다.
"""

import unittest

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import SubmitTransferRequest
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat, StationId

from tests.support.stage4 import HAS_CRYPTO
from tests.support.stage7 import (
    STATION_BY_ENDPOINT,
    build_stage7_software_group,
    drive_stage7_group,
    service_events,
    start_stage7_group,
)

OPAQUE = PayloadFormat("test.stage0.opaque")


@unittest.skipUnless(HAS_CRYPTO, "cryptography extra가 필요합니다")
class TC_STAGE7_SOFTWARE_SCENARIOS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.clock,
            cls.medium,
            cls.plan,
            cls.coordinator,
            cls.members,
            cls.resolved,
        ) = build_stage7_software_group()
        start_stage7_group(
            cls.clock, cls.medium, cls.coordinator, cls.members, cls.resolved
        )
        cls.received_counts = {m: 0 for m in cls.members}

    def _submit(self, sender: str, destination: str, payload: bytes, *, ack: bool):
        endpoint = self.members[sender].endpoint
        result = endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(STATION_BY_ENDPOINT[destination])),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=ack,
            )
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        return result.value.transfer_id

    def _await_completed(self, sender: str, transfer_id, *, steps=200, chunk=50):
        for _ in range(0, steps, chunk):
            drive_stage7_group(self.clock, self.members, chunk)
            states = [
                event.transfer_state.value
                for event in service_events(
                    self.members[sender], ServiceEventType.TRANSFER_STATE_CHANGED
                )
                if event.transfer_id == transfer_id and event.transfer_state is not None
            ]
            if "COMPLETED" in states:
                return states
            if "FAILED" in states:
                self.fail(f"{sender} transfer FAILED: {states}")
        self.fail(f"{sender} transfer가 완료되지 않았습니다")

    def _assert_received(self, destination: str, payload: bytes):
        events = service_events(self.members[destination], ServiceEventType.DATA_RECEIVED)
        expected_index = self.received_counts[destination]
        self.assertGreater(len(events), expected_index, f"{destination} DATA_RECEIVED 없음")
        event = events[expected_index]
        self.received_counts[destination] = expected_index + 1
        self.assertEqual(event.received_data.payload, payload)
        return event

    def test_01_roc_to_mass_a_addressed(self):
        payload = bytes(range(20))
        transfer = self._submit("ROC", "MASS-A", payload, ack=False)
        self._await_completed("ROC", transfer)
        event = self._assert_received("MASS-A", payload)
        self.assertEqual(
            event.received_data.original_source.value, STATION_BY_ENDPOINT["ROC"]
        )

    def test_02_roc_to_mass_b_addressed(self):
        payload = bytes(range(10, 34))
        transfer = self._submit("ROC", "MASS-B", payload, ack=False)
        self._await_completed("ROC", transfer)
        self._assert_received("MASS-B", payload)

    def test_03_mass_b_to_roc(self):
        # RAC random-access 슬롯 추첨(선택 구간 235)이 늦게 잡힐 수 있어 길게 구동한다.
        payload = b"\x42" * 25
        transfer = self._submit("MASS-B", "ROC", payload, ack=False)
        self._await_completed("MASS-B", transfer, steps=1600, chunk=100)
        self._assert_received("ROC", payload)

    def test_04_mass_a_to_roc_with_ack(self):
        payload = b"\x24" * 24
        transfer = self._submit("MASS-A", "ROC", payload, ack=True)
        self._await_completed("MASS-A", transfer, steps=1600, chunk=100)
        self._assert_received("ROC", payload)

    def test_05_direct_mass_a_to_mass_b(self):
        payload = b"\x33" * 20
        transfer = self._submit("MASS-A", "MASS-B", payload, ack=False)
        self._await_completed("MASS-A", transfer, steps=1600, chunk=100)
        self._assert_received("MASS-B", payload)
        paths = {entry["path_id"] for entry in self.medium.delivery_log}
        self.assertIn("C5", paths, "direct path C5가 사용되지 않았습니다")

    def test_06_direct_mass_b_to_mass_a(self):
        payload = b"\x55" * 20
        transfer = self._submit("MASS-B", "MASS-A", payload, ack=False)
        self._await_completed("MASS-B", transfer, steps=1600, chunk=100)
        self._assert_received("MASS-A", payload)
        paths = {entry["path_id"] for entry in self.medium.delivery_log}
        self.assertIn("C6", paths)

    def test_07_data_session_mass_a_to_roc(self):
        payload = bytes((i % 256 for i in range(768)))
        transfer = self._submit("MASS-A", "ROC", payload, ack=True)
        self._await_completed("MASS-A", transfer, steps=2400, chunk=100)
        self._assert_received("ROC", payload)

    def test_08_relay_mass_a_to_mass_b(self):
        payload = bytes(((i * 7) % 256 for i in range(768)))
        transfer = self._submit("MASS-A", "MASS-B", payload, ack=True)
        # 송신측 COMPLETED는 first-hop 완료다. second-hop(ROC→MASS-B Data
        # Session)은 별도 자원할당 절차를 거치므로 수신까지 추가 구동한다.
        self._await_completed("MASS-A", transfer, steps=3200, chunk=100)
        expected_index = self.received_counts["MASS-B"]
        for _ in range(0, 4800, 100):
            events = service_events(
                self.members["MASS-B"], ServiceEventType.DATA_RECEIVED
            )
            if len(events) > expected_index:
                break
            drive_stage7_group(self.clock, self.members, 100)
        self._assert_received("MASS-B", payload)


if __name__ == "__main__":
    unittest.main()
