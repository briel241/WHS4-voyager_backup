"""노선·조종 loopback 공용 하네스 (opaque 포맷).

`route_loopback_demo.py`(노선)와 `control_loopback_demo.py`(조종)가 공유하는 부분:
VDETER 소프트웨어 무선 그룹 조립, opaque 포맷 전송, 공식 `HandlerServiceEventSink`
수신, 압축 코덱 왕복 검증. **시나리오별 payload 생성은 각 데모 파일이 담당한다.**

이 모듈은 진입점(데모)이 아니라 헬퍼다(파일명 `_` 접두사). import 실패해도 sys.exit
하지 않고 `HARNESS_AVAILABLE` 플래그만 세운다 — SKIP 판단은 데모의 `preflight()`가 한다.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import unittest

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
    from vdeter.service import HandlerServiceEventSink  # 공식 이벤트 binding

    HARNESS_AVAILABLE = True
    _IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - VDETER 없는 환경
    HARNESS_AVAILABLE = False
    _IMPORT_ERROR = exc

from common import codec
from common import vdeter_bridge as bridge

OPAQUE = "test.stage0.opaque"   # ⚠ 시험 전용 통과 포맷
MAX_DRIVE_STEPS = 800


class _ReceiverApp:
    """공식 binding — DATA_RECEIVED로 온 application data를 모은다.

    실 launcher/Stage 7이 우리 앱에 넘길 방식과 동일: HandlerServiceEventSink로
    endpoint에 연결되고, handle은 event를 받아 두기만 한다.
    """

    def __init__(self) -> None:
        self.received = []

    def handle(self, event) -> None:
        if event.event_type is ServiceEventType.DATA_RECEIVED:
            self.received.append(event.received_data)


def _send_opaque(payload: bytes):
    """ROC→MASS로 payload를 opaque 포맷으로 보내고 (전송상태, 수신 payload) 반환.

    전송마다 그룹을 격리한다(ACK 전송 연속 시 상태 얽힘 회피).
    """
    receiver = _ReceiverApp()
    mass_endpoint_id = STATION_ENDPOINT_IDS[MASS_A_STATION_ID.value]

    def service_sink_factory(endpoint_id):
        handler = receiver.handle if endpoint_id == mass_endpoint_id else (lambda _e: None)
        return HandlerServiceEventSink(handler)

    clock, group = build_stage5_software_group(service_sink_factory=service_sink_factory)
    start_stage5_group(clock, group)
    try:
        roc = group.by_station()[ROC_STATION_ID.value]

        request = SubmitTransferRequest(
            destination=Addressed(MASS_A_STATION_ID),
            payload_format=PayloadFormat(OPAQUE),
            payload=payload,
            ack_required=True,
        )
        accepted = roc.submit(request)
        if not accepted.is_ok:
            raise RuntimeError(f"submit 거부: {getattr(accepted, 'error', accepted)}")

        state = None
        for _ in range(MAX_DRIVE_STEPS):
            drive_stage5_group(clock, group, 1)
            state = roc.status(TransferStatusRequest(accepted.value.transfer_id)).value.state
            if state in (TransferState.COMPLETED, TransferState.FAILED,
                         TransferState.CANCELLED):
                break
    finally:
        group.stop()

    # 공식 수신 경로: ServiceEvent → vdeter_bridge.Inbound → payload
    received = [bridge.inbound_from_received(rd).payload for rd in receiver.received]
    return state, received


def run_scenario(title, message, checks) -> None:
    """압축 메시지를 encode → opaque 전송 → 수신·복원·필드 검증 → 출력."""
    payload = codec.encode(message)
    state, received = _send_opaque(payload)
    print(f"[{title}] 압축 payload {len(payload)} byte 제출")
    print(f"  전송 상태     : {state.value}")
    byte_equal = bool(received) and received[0] == payload
    print(f"  수신 byte 일치: {byte_equal}")
    assert state is TransferState.COMPLETED, f"{title}: COMPLETED 아님 ({state})"
    assert byte_equal, f"{title}: 수신 byte 불일치"

    decoded = codec.decode(received[0])
    for label, actual, expected in checks(decoded):
        mark = "OK" if actual == expected else "FAIL"
        print(f"  {label}: {actual} (기대 {expected}) [{mark}]")
        assert actual == expected, f"{title}: {label} 불일치"
    print()


def preflight() -> bool:
    """하네스·cryptography가 사용 가능하면 True. 아니면 SKIP 안내 후 False."""
    if not HARNESS_AVAILABLE:
        print(f"SKIP: VDETER 소프트웨어 무선 하네스를 불러올 수 없습니다 ({_IMPORT_ERROR}).")
        return False
    try:
        _clock, _group = build_stage5_software_group()  # 조립 가능 여부(=cryptography) 확인
        _group.stop()
    except unittest.SkipTest as exc:
        print(f"SKIP: 소프트웨어 그룹 조립에 cryptography가 필요합니다 ({exc}).")
        return False
    return True
