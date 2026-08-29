"""Stage 7 software fault 주입 시나리오 (`stage7` §17.2, TC-STAGE7-012 software 부분).

RUNNING group에서 conducted path C1의 burst를 한시적으로 유실시켜 DSCH ACK
손실을 주입하고, frozen MAC 재전송으로 transfer가 결국 COMPLETED되는지 public
API로만 확인한다. 유실 구간의 Type 0 동반 손실은 주기 broadcast로 복구된다.
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
class TC_STAGE7_ACK_DROP(unittest.TestCase):
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

    def _transfer_states(self, sender: str, transfer_id):
        return [
            event.transfer_state.value
            for event in service_events(
                self.members[sender], ServiceEventType.TRANSFER_STATE_CHANGED
            )
            if event.transfer_id == transfer_id and event.transfer_state is not None
        ]

    def test_ack_drop_then_retransmission_completes(self):
        payload = b"\x77" * 24
        submitted = self.members["MASS-A"].endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(STATION_BY_ENDPOINT["ROC"])),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=True,
            )
        )
        self.assertTrue(submitted.is_ok, getattr(submitted, "error", None))
        transfer_id = submitted.value.transfer_id

        # 1) 송신측이 WAITING_ACK에 도달할 때까지 구동한다.
        for _ in range(32):
            drive_stage7_group(self.clock, self.members, 100)
            states = self._transfer_states("MASS-A", transfer_id)
            if "WAITING_ACK" in states or "COMPLETED" in states:
                break
        states = self._transfer_states("MASS-A", transfer_id)
        self.assertIn("WAITING_ACK", states, f"WAITING_ACK 미도달: {states}")

        # 2) ACK 경로(C1: ROC→MASS-A)를 한시 유실시킨다. 재시도 한도를 넘지
        #    않도록 최대 2 burst만 유실한다.
        drop_state = {"count": 0}

        def drop(path_id, data, raw_ns):
            if path_id == "C1" and drop_state["count"] < 2:
                drop_state["count"] += 1
                return True
            return False

        self.medium.drop_filter = drop
        try:
            drive_stage7_group(self.clock, self.members, 400)
        finally:
            self.medium.drop_filter = None
        self.assertGreater(
            drop_state["count"], 0, "유실 filter가 C1 burst를 하나도 잡지 못했습니다"
        )
        self.assertEqual(len(self.medium.dropped_log), drop_state["count"])

        # 3) filter 해제 후 재전송으로 COMPLETED에 도달해야 하며 FAILED는 없다.
        for _ in range(0, 3200, 100):
            drive_stage7_group(self.clock, self.members, 100)
            states = self._transfer_states("MASS-A", transfer_id)
            if "COMPLETED" in states:
                break
            self.assertNotIn("FAILED", states, f"transfer FAILED: {states}")
        states = self._transfer_states("MASS-A", transfer_id)
        self.assertIn("COMPLETED", states, f"ACK 유실 후 미완료: {states}")

        received = [
            event.received_data.payload
            for event in service_events(
                self.members["ROC"], ServiceEventType.DATA_RECEIVED
            )
        ]
        self.assertIn(payload, received, "ROC가 payload를 수신하지 못했습니다")


if __name__ == "__main__":
    unittest.main()
