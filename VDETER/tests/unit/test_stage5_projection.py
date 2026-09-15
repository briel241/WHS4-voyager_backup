"""TC-STAGE5-007: 계층별 Service 오류 projection.

요구사항: `STAGE5-REQ-07` (`stage5_service_integration_baseline.md` §9.5, `DR-STAGE5-005`)
"""

from __future__ import annotations

import unittest

from vdeter.common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
    ServiceErrorCategory,
)
from vdeter.common.identifiers import SessionHandle, TransferId
from vdeter.runtime.projection import (
    PUBLIC_DETAIL_KEYS,
    PublicErrorCode,
    to_service_error,
)

TRANSFER_ID = TransferId("9f1b2c3d-4e5f-4a6b-8c7d-0123456789ab")


def _make(layer, code, category, **details):
    return InternalProtocolError(
        layer=layer,
        code=code,
        category=category,
        message="stage5 projection test",
        correlation=Correlation(),
        details=Details.of(**details) if details else Details(),
    )


class Stage5ProjectionMappingTest(unittest.TestCase):
    def test_TC_STAGE5_007_layer_error_codes_use_the_approved_projection(self):
        cases = (
            # Service/자원
            (Layer.SERVICE, ErrorCode.SERVICE_RESOURCE_UNAVAILABLE, ErrorCategory.RESOURCE,
             PublicErrorCode.RESOURCE_UNAVAILABLE, ServiceErrorCategory.RESOURCE),
            # Presentation RX
            (Layer.PRESENTATION, ErrorCode.PRESENTATION_TRUNCATED, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            (Layer.PRESENTATION, ErrorCode.PRESENTATION_STRUCTURE_INVALID, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            (Layer.PRESENTATION, ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, ErrorCategory.UNSUPPORTED,
             PublicErrorCode.UNSUPPORTED_REQUEST, ServiceErrorCategory.UNSUPPORTED),
            (Layer.PRESENTATION, ErrorCode.PRESENTATION_DISCRIMINATOR_CONFLICT,
             ErrorCategory.VALIDATION,
             PublicErrorCode.INVALID_REQUEST, ServiceErrorCategory.VALIDATION),
            # Link protocol
            (Layer.LINK, ErrorCode.LINK_CRC_MISMATCH, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            (Layer.LINK, ErrorCode.LINK_FRAGMENT_CONFLICT, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            (Layer.LINK, ErrorCode.LINK_UNSUPPORTED_MESSAGE_TYPE, ErrorCategory.UNSUPPORTED,
             PublicErrorCode.UNSUPPORTED_REQUEST, ServiceErrorCategory.UNSUPPORTED),
            # MAC (Stage 4 동결 migration 유지)
            (Layer.MAC, ErrorCode.MAC_TRANSFER_TIMEOUT, ErrorCategory.TIMEOUT,
             PublicErrorCode.TRANSFER_TIMEOUT, ServiceErrorCategory.TIMEOUT),
            (Layer.MAC, ErrorCode.MAC_TRANSFER_FAILED, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            # PHY
            (Layer.PHY, ErrorCode.PHY_TRANSMIT_DEADLINE_EXPIRED, ErrorCategory.TIMEOUT,
             PublicErrorCode.TRANSFER_TIMEOUT, ServiceErrorCategory.TIMEOUT),
            (Layer.PHY, ErrorCode.PHY_FEC_DECODE_FAILED, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            (Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
             PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
            # Radio
            (Layer.RADIO, ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_DEVICE_DISCONNECTED, ErrorCategory.DEVICE,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_REFERENCE_LOST, ErrorCategory.DEVICE,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_TX_UNDERFLOW, ErrorCategory.DEVICE,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_RX_OVERFLOW, ErrorCategory.DEVICE,
             PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
            (Layer.RADIO, ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE,
             PublicErrorCode.RESOURCE_UNAVAILABLE, ServiceErrorCategory.RESOURCE),
            (Layer.RADIO, ErrorCode.RADIO_LATE_REQUEST, ErrorCategory.TIMEOUT,
             PublicErrorCode.TRANSFER_TIMEOUT, ServiceErrorCategory.TIMEOUT),
        )
        for layer, code, category, expected_code, expected_category in cases:
            with self.subTest(code=code):
                public = to_service_error(_make(layer, code, category))
                self.assertEqual(expected_code, public.code)
                self.assertEqual(expected_category, public.category)

    def test_TC_STAGE5_007_unmapped_codes_generalize_to_internal(self):
        public = to_service_error(
            _make(Layer.MAC, "SOME_FUTURE_STAGE6_CODE", ErrorCategory.PROTOCOL)
        )
        self.assertEqual(PublicErrorCode.INTERNAL_ERROR, public.code)
        self.assertEqual(ServiceErrorCategory.INTERNAL, public.category)

    def test_TC_STAGE5_007_frozen_stage4_invariant_projection_is_preserved(self):
        # Stage 4 동결 시험이 고정한 invariant projection을 바꾸지 않는다.
        for code in (ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCode.LINK_TRANSFER_PROGRESS_INVALID):
            with self.subTest(code=code):
                public = to_service_error(_make(Layer.MAC, code, ErrorCategory.PROTOCOL))
                self.assertEqual(PublicErrorCode.INTERNAL_ERROR, public.code)

    def test_TC_STAGE5_007_retryable_flag_is_preserved(self):
        error = InternalProtocolError(
            layer=Layer.SERVICE,
            code=ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
            category=ErrorCategory.RESOURCE,
            message="stage5",
            retryable=True,
        )
        self.assertTrue(to_service_error(error).retryable)


class Stage5ProjectionDetailAllowlistTest(unittest.TestCase):
    def test_TC_STAGE5_007_stage5_allowlist_keys_are_kept(self):
        error = _make(
            Layer.SERVICE,
            ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
            ErrorCategory.RESOURCE,
            limit_name="max_nonterminal_transfers",
            limit_value=8,
            current_value=8,
            endpoint_id="stage5-ship-a",
            previous_health="RUNNING",
            health="DEGRADED",
        )
        public = to_service_error(error)
        for key in (
            "limit_name",
            "limit_value",
            "current_value",
            "endpoint_id",
            "previous_health",
            "health",
        ):
            self.assertIn(key, public.details.keys())
            self.assertIn(key, PUBLIC_DETAIL_KEYS)

    def test_TC_STAGE5_007_internal_only_details_are_removed(self):
        error = _make(
            Layer.MAC,
            ErrorCode.MAC_TRANSFER_FAILED,
            ErrorCategory.PROTOCOL,
            mac_reason="ACK_TIMEOUT",
            timer_id="t-1",
            channel="VDETER_UPPER_25_TEST",
            link_id=11,
            message_type=92,
            slot_number=42,
            retransmission_number=2,
        )
        public = to_service_error(error)
        for key in (
            "mac_reason",
            "timer_id",
            "channel",
            "link_id",
            "message_type",
            "slot_number",
            "retransmission_number",
        ):
            self.assertNotIn(key, public.details.keys())

    def test_TC_STAGE5_007_only_transfer_id_correlation_is_exposed(self):
        error = InternalProtocolError(
            layer=Layer.MAC,
            code=ErrorCode.MAC_TRANSFER_FAILED,
            category=ErrorCategory.PROTOCOL,
            message="stage5",
            correlation=Correlation(
                transfer_id=TRANSFER_ID,
                session_handle=SessionHandle("b3b8f9d4-42a5-4a4e-8d19-6b1a2c3d4e5f"),
            ),
        )
        public = to_service_error(error)
        self.assertEqual(TRANSFER_ID, public.transfer_id)
        self.assertNotIn("session_handle", public.details.keys())

    def test_TC_STAGE5_007_public_message_is_stable_not_internal(self):
        error = InternalProtocolError(
            layer=Layer.RADIO,
            code=ErrorCode.RADIO_STOP_TIMEOUT,
            category=ErrorCategory.TIMEOUT,
            message="device handle 0xDEADBEEF stream stall",
        )
        public = to_service_error(error)
        self.assertNotIn("0xDEADBEEF", public.message)


if __name__ == "__main__":
    unittest.main()
