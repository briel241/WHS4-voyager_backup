"""실행 가능한 MASS-A → ROC X.509 Service API Software Radio 예제.

프로젝트 루트에서 다음처럼 실행한다.

    uv run python -m apps.stage5_service_api_demo

`tests.support.stage5`는 세 endpoint의 결정적 Software Radio 배포 환경만 조립한다. 시나리오의
시작·송신·상태 조회·event 수신·종료는 실제 `VdeService`와 `HandlerServiceEventSink` 공개
계약을 사용한다.
"""

from __future__ import annotations

from tests.support.stage4 import Type0OverrideProfile, build_test_signer
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    STATION_ENDPOINT_IDS,
    build_stage5_software_group,
    drive_stage5_group,
    start_stage5_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat
from vdeter.service import HandlerServiceEventSink

CERTIFICATE_FORMAT = PayloadFormat("x509.certificate_report")
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)


def build_certificate_report(issuer_fingerprint: bytes, certificate_der: bytes) -> bytes:
    """X.509 certificate 한 개를 R1027 Certificate Report body로 만든다."""

    if len(issuer_fingerprint) != 6:
        raise ValueError("issuer_fingerprint must be exactly 6 bytes")
    if not 1 <= len(certificate_der) <= 0xFFFF:
        raise ValueError("certificate_der length must be 1..65535")
    return b"".join(
        (
            b"\x01",
            issuer_fingerprint,
            b"\x00",
            len(certificate_der).to_bytes(2, "big"),
            certificate_der,
        )
    )


class MassApplication:
    """MASS-A의 송신 상태 event를 처리한다."""

    def __init__(self) -> None:
        self.states = {}
        self.errors = {}

    def handle_service_event(self, event) -> None:
        if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED:
            self.states[event.transfer_id] = event.transfer_state
            if event.error is not None:
                self.errors[event.transfer_id] = event.error
        elif event.event_type is ServiceEventType.ERROR:
            print("MASS-A asynchronous error:", event.error)


class RocApplication:
    """ROC의 application data 수신 event를 처리한다."""

    def __init__(self) -> None:
        self.received = []

    def handle_service_event(self, event) -> None:
        if event.event_type is ServiceEventType.DATA_RECEIVED:
            self.received.append(event.received_data)
        elif event.event_type is ServiceEventType.ERROR:
            print("ROC asynchronous error:", event.error)


def main() -> int:
    mass_app = MassApplication()
    roc_app = RocApplication()

    handlers = {
        STATION_ENDPOINT_IDS[MASS_A_STATION_ID.value]: mass_app.handle_service_event,
        STATION_ENDPOINT_IDS[ROC_STATION_ID.value]: roc_app.handle_service_event,
    }

    def service_sink_factory(endpoint_id: str):
        # MASS-B는 이 시나리오의 업무 참여자가 아니므로 event를 수락만 한다.
        handler = handlers.get(endpoint_id, lambda _event: None)
        return HandlerServiceEventSink(handler)

    clock, group = build_stage5_software_group(
        type0_override=FAST_TYPE0,
        service_sink_factory=service_sink_factory,
    )
    services = group.by_station()
    mass_a_service = services[MASS_A_STATION_ID.value]
    roc_service = services[ROC_STATION_ID.value]

    _signer, certificate_der, issuer_fingerprint = build_test_signer()
    report = build_certificate_report(issuer_fingerprint, certificate_der)

    started = False
    try:
        # group bootstrap은 각 endpoint의 실제 service.start()를 승인 순서로 호출한다.
        start_stage5_group(clock, group)
        started = True

        submitted = mass_a_service.submit(
            SubmitTransferRequest(
                destination=Addressed(ROC_STATION_ID),
                payload_format=CERTIFICATE_FORMAT,
                payload=report,
                ack_required=False,
            )
        )
        if submitted.is_err:
            raise RuntimeError(f"submit failed: {submitted.error.message}")
        transfer_id = submitted.value.transfer_id

        state = submitted.value.state
        for _ in range(400):
            drive_stage5_group(clock, group, 1)
            status = mass_a_service.status(TransferStatusRequest(transfer_id))
            if status.is_err:
                raise RuntimeError(f"status failed: {status.error.message}")
            state = status.value.state
            if state is TransferState.COMPLETED and roc_app.received:
                break

        if state is not TransferState.COMPLETED:
            error = mass_app.errors.get(transfer_id)
            raise RuntimeError(f"transfer did not complete: state={state}, error={error}")
        if mass_app.states.get(transfer_id) is not TransferState.COMPLETED:
            raise RuntimeError("MASS-A did not receive the COMPLETED event")
        if len(roc_app.received) != 1:
            raise RuntimeError(f"ROC received {len(roc_app.received)} matching events")

        received = roc_app.received[0]
        if received.original_source != MASS_A_STATION_ID:
            raise RuntimeError("ROC original_source does not identify MASS-A")
        if received.payload_format != CERTIFICATE_FORMAT:
            raise RuntimeError("ROC payload_format mismatch")
        if received.payload != report:
            raise RuntimeError("ROC payload differs from the submitted Certificate Report")

        print("PASS: MASS-A -> ROC X.509 Certificate Report")
        print("transfer_id:", transfer_id.value)
        print("payload_bytes:", len(report))
        return 0
    finally:
        if started:
            stopped = group.stop()
            if stopped.is_err:
                print("warning: group stop failed:", stopped.error)


if __name__ == "__main__":
    raise SystemExit(main())
