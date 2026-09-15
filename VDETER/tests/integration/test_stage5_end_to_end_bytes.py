"""TC-STAGE5-018: format별 end-to-end bytes와 packet evidence.

요구사항: `STAGE5-REQ-18`, `STAGE5-REQ-11`, `STAGE5-REQ-19`
(`stage5_service_integration_baseline.md` §6.6, §8.5, §11.2)
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from tests.support.stage4 import Type0OverrideProfile
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    received_payloads,
    start_stage5_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import InternalEventType, ServiceEventType
from vdeter.common.identifiers import PayloadFormat

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)

OPAQUE = PayloadFormat("test.stage0.opaque")
X509 = PayloadFormat("x509.certificate_report")
AIS = PayloadFormat("ais.authentication_integrity.scheme1")


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage5-test:lower-bound"


class Stage5EndToEndBytesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = build_stage5_software_group(
            type0_override=FAST_TYPE0,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage5_group(cls.clock, cls.group)
        cls.by_station = cls.group.by_station()

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    @staticmethod
    def _internal_counts(endpoint, mark: int) -> dict:
        counts = {}
        decoded_types = {}
        for internal in endpoint.dependencies.internal_event_sink.events()[mark:]:
            counts[internal.event_type.value] = counts.get(internal.event_type.value, 0) + 1
            if internal.event_type is InternalEventType.MESSAGES_DECODED:
                for message in internal.payload.messages:
                    key = str(message.message_type)
                    decoded_types[key] = decoded_types.get(key, 0) + 1
        counts["decoded_message_types"] = decoded_types
        return counts

    def _write_checkpoint_artifact(self, name: str, checkpoint: dict) -> None:
        """§8.5 correlation checkpoint를 접근 통제된 evidence run에 보존한다.

        runner가 `VDETER_STAGE5_ACTIVE_RUN`을 설정한 실행에서만 기록한다. 운영 log가
        아니라 자동시험 run 전용 artifact이며 raw payload 대신 길이·digest를 담는다.
        """

        import json
        import os

        run_id = os.environ.get("VDETER_STAGE5_ACTIVE_RUN")
        if not run_id:
            return
        target = (
            Path(__file__).resolve().parents[2]
            / "results"
            / "runs"
            / run_id
            / "artifacts"
            / "stage5-checkpoints"
        )
        target.mkdir(parents=True, exist_ok=True)
        with open(target / f"{name}.json", "w", encoding="utf-8", newline="\n") as handle:
            json.dump(checkpoint, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")

    def _transfer(self, payload_format, payload, *, destination_station, ack, artifact=None):
        source = self.by_station[MASS_A_STATION_ID.value]
        destination = self.by_station[destination_station.value]
        before = len(received_payloads(destination))
        source_mark = len(source.dependencies.internal_event_sink.events())
        destination_mark = len(destination.dependencies.internal_event_sink.events())
        source_digest = hashlib.sha256(payload).hexdigest()
        accepted = source.submit(
            SubmitTransferRequest(
                destination=Addressed(destination_station),
                payload_format=payload_format,
                payload=payload,
                ack_required=ack,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        expected_wire = source._pending_presentation.get(
            accepted.value.transfer_id.value
        )
        for _ in range(400):
            drive_stage5_group(self.clock, self.group, 1)
            state = source.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value.state
            if state is TransferState.COMPLETED:
                break
        self.assertIs(TransferState.COMPLETED, state)
        received = received_payloads(destination)[before:]
        self.assertEqual(1, len(received))
        # §8.5 checkpoint 1↔7·8: 송신 원본 bytes와 수신 bytes의 길이·전체 byte 일치.
        # digest는 evidence index용이고 실제 비교는 bytes 동등 연산으로 수행한다.
        self.assertEqual(len(payload), len(received[0]))
        self.assertEqual(payload, received[0])
        self.assertEqual(source_digest, hashlib.sha256(received[0]).hexdigest())
        event = [
            item
            for item in destination.dependencies.service_event_sink.events()
            if item.event_type is ServiceEventType.DATA_RECEIVED
            and item.received_data.payload == payload
        ][-1]
        self.assertEqual(payload_format, event.received_data.payload_format)
        if artifact is not None and expected_wire is not None:
            source_counts = self._internal_counts(source, source_mark)
            destination_counts = self._internal_counts(destination, destination_mark)
            # §8.5의 여덟 checkpoint를 하나의 correlation artifact로 묶는다.
            self._write_checkpoint_artifact(
                artifact,
                {
                    "scenario": artifact,
                    "transfer_id": accepted.value.transfer_id.value,
                    "checkpoint_1_tx_application": {
                        "bytes": len(payload),
                        "sha256": source_digest,
                    },
                    "checkpoint_2_presentation": {
                        "vpfi": expected_wire.vpfi.value,
                        "message_format": expected_wire.message_format,
                        "encoded_bytes": len(expected_wire.data),
                        "encoded_sha256": hashlib.sha256(expected_wire.data).hexdigest(),
                    },
                    "checkpoint_3_4_link_packet": {
                        "packets_built": source_counts.get("PACKET_BUILT", 0),
                        "original_source": source.dependencies.configuration.endpoint.station_id.value,
                        "original_destination": destination_station.value,
                    },
                    "checkpoint_5_phy_radio": {
                        "tx_bursts_encoded": source_counts.get("BURST_ENCODED", 0),
                        "tx_bursts_scheduled": source_counts.get("BURST_SCHEDULED", 0),
                        "rx_packet_sets_recovered": destination_counts.get(
                            "PACKETS_RECOVERED", 0
                        ),
                    },
                    "checkpoint_6_ack_frames": {
                        "sender_decoded_message_types": source_counts[
                            "decoded_message_types"
                        ],
                        "receiver_decoded_message_types": destination_counts[
                            "decoded_message_types"
                        ],
                    },
                    "checkpoint_7_rx_application": {
                        "bytes": len(received[0]),
                        "sha256": hashlib.sha256(received[0]).hexdigest(),
                    },
                    "checkpoint_8_rx_service": {
                        "original_source": event.received_data.original_source.value,
                        "payload_format": payload_format.value,
                        "received_at_utc_nanoseconds": event.received_data.received_at.total_nanoseconds(),
                    },
                    "byte_for_byte_equal": payload == received[0],
                },
            )
        return accepted.value.transfer_id, expected_wire

    def test_TC_STAGE5_018_opaque_short_data_bytes_and_packet_evidence(self):
        payload = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.application.bin").read_bytes()
        expected_wire = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.wire.bin").read_bytes()
        source = self.by_station[MASS_A_STATION_ID.value]
        transfer_id, presentation = self._transfer(
            OPAQUE,
            payload,
            destination_station=MASS_B_STATION_ID,
            ack=True,
            artifact="opaque-short-data",
        )
        # checkpoint 2: Presentation encoded bytes가 승인된 golden wire와 같다.
        self.assertIsNotNone(presentation)
        self.assertEqual(expected_wire, presentation.data)
        # checkpoint 3·4: Link Packet build 내부 evidence가 존재한다.
        built = [
            event
            for event in source.dependencies.internal_event_sink.events_of(
                InternalEventType.PACKET_BUILT
            )
            if event.correlation.transfer_id == transfer_id
        ]
        self.assertTrue(built)

    def test_TC_STAGE5_018_x509_certificate_report_end_to_end(self):
        payload = (VECTOR_ROOT / "gv_stage5_pres_x509_001.application.bin").read_bytes()
        expected_wire = (VECTOR_ROOT / "gv_stage5_pres_x509_001.wire.bin").read_bytes()
        transfer_id, presentation = self._transfer(
            X509,
            payload,
            destination_station=ROC_STATION_ID,
            ack=False,
            artifact="x509-certificate-report",
        )
        self.assertIsNotNone(presentation)
        self.assertEqual(expected_wire, presentation.data)

    def test_TC_STAGE5_018_ais_scheme1_end_to_end_unsigned_segment_type0(self):
        payload = (VECTOR_ROOT / "gv_stage5_pres_ais1_001.application.bin").read_bytes()
        expected_wire = (VECTOR_ROOT / "gv_stage5_pres_ais1_001.wire.bin").read_bytes()
        transfer_id, presentation = self._transfer(
            AIS,
            payload,
            destination_station=ROC_STATION_ID,
            ack=False,
            artifact="ais-authentication-scheme1",
        )
        self.assertIsNotNone(presentation)
        self.assertEqual(expected_wire, presentation.data)
        # unsigned VDE Segment Type 0만 운반한다: Stage 1 segment codec은 Type 1·2·3을
        # 거부하므로 (`decode_segment`), 수신 재조립·전달 성공 자체가 Type 0 사용의
        # 증거다. encode 경로도 직접 확인한다.
        from vdeter.link.segment.codec import (
            SEGMENT_TYPE_MASK,
            SEGMENT_TYPE_SHIFT,
            decode_segment,
            encode_unsigned_segment,
        )
        from vdeter.common.identifiers import SequentialId

        encoded_segment = encode_unsigned_segment(
            sequential_id=SequentialId(1), presentation_data=presentation.data
        )
        self.assertTrue(encoded_segment.is_ok)
        segment_bytes = encoded_segment.value
        type_and_id = int.from_bytes(segment_bytes[0:2], "big")
        self.assertEqual(0, (type_and_id >> SEGMENT_TYPE_SHIFT) & SEGMENT_TYPE_MASK)
        decoded = decode_segment(segment_bytes)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(presentation.data, decoded.value.presentation_data)


if __name__ == "__main__":
    unittest.main()
