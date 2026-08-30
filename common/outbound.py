"""
행위자가 내보내는 송신 요청

행위자는 VDE 링크 계층을 직접 알지 못한다. 대신 "이 payload를 누구에게,
ACK를 요구하며 보내라"는 요청만 만들고, 링크 연결은 어댑터가 맡는다.
이렇게 두면 mass_roc 패키지가 링크 계층 구현에 의존하지 않는다.

실제 링크 연동은 `common/vdeter_bridge.py`가 이 `Outbound`를 VDETER의
`SubmitTransferRequest`로 바꿔서 한다(가이드: `docs/vdeter_mass_roc_api_guide.md`).
그 경계는 다음과 같다.

    API 사용자는 Link ID, Message Type, Session ID, VPFI, CRC, FEC, Burst byte
    또는 GNU Radio 객체를 만들거나 전달하지 않는다.

따라서 여기에는 Type 92/93 같은 링크 계층 값을 두지 않는다. `ack_required`가
점대점 ACK 필요 여부를 나타내고, 그것을 어떤 Message Type으로 실을지는
VDE-TER Service가 정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

# broadcast 수신자 ID.
BROADCAST_ID = 0

# application data 분류. vdeter_bridge가 VDETER payload_format으로 매핑한다.
DATA_TYPE_TBB = "TBB"
DATA_TYPE_AIS_AUTH_INTEGRITY = "AIS_AUTH_INTEGRITY"
DATA_TYPE_ELECTRONIC_CHART = "ELECTRONIC_CHART"
DATA_TYPE_X509_CERTIFICATE = "X509_CERTIFICATE"

# SC-VDE 노선·원격조종 (가이드 §8.3·§8.4에서 계약 승인됨).
# 노선(SC-VDE-005)과 조종(SC-VDE-006)은 같은 Message Format 2를 공유하므로
# data_type도 하나다. 업무 구분(Heading·Fallback 유무)은 응용이 판단한다.
DATA_TYPE_ROUTE_CONTROL_COMMAND = "ROUTE_CONTROL_COMMAND"
DATA_TYPE_ROUTE_CONTROL_ACK = "ROUTE_CONTROL_ACK"

# 위치보고 (가이드 §8, MF 1). addressed(MASS→ROC) 전용이고 ack_required=False다
# (DR-PAYLOAD-016). AIS는 브로드캐스트지만 VDETER는 MASS 브로드캐스트를 지원하지
# 않아 addressed로 승인됐다.
DATA_TYPE_POSITION_REPORT = "POSITION_REPORT"

API_DATA_TYPES = frozenset(
    {
        DATA_TYPE_TBB,
        DATA_TYPE_AIS_AUTH_INTEGRITY,
        DATA_TYPE_ELECTRONIC_CHART,
        DATA_TYPE_X509_CERTIFICATE,
        DATA_TYPE_ROUTE_CONTROL_COMMAND,
        DATA_TYPE_ROUTE_CONTROL_ACK,
        DATA_TYPE_POSITION_REPORT,
    }
)

# 주의
#     노선·조종 두 분류는 TEST/PROJECT scope 전용이다(가이드 §8.5). 서명·replay
#     보호가 없으므로 실제 선박·조종 장비를 구동하는 데 쓰면 안 된다.
#     위치보고·경보·환경·기상에는 아직 대응 분류가 없다. 임의로 만들어 쓰지 않고
#     data_type은 None으로 둔다.
DATA_TYPE_UNASSIGNED = None


@dataclass
class Outbound:
    payload: bytes
    destination_id: int = BROADCAST_ID
    ack_required: bool = False
    data_type: str | None = DATA_TYPE_UNASSIGNED
    content_type: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        # M.2092-2 Table 40: Type 92의 Destination MMSI는 0(broadcast)일 수 없다.
        # API 계약 3.1.2도 broadcast에는 ack_required=false를 쓰라고 정한다.
        if self.ack_required and self.destination_id == BROADCAST_ID:
            raise ValueError(
                "broadcast 송신에는 ack_required=True를 쓸 수 없습니다 "
                "(M.2092-2 Table 40)"
            )
        if self.data_type is not None and self.data_type not in API_DATA_TYPES:
            raise ValueError(
                f"API 계약에 없는 data_type입니다 ({self.data_type!r}). "
                f"허용 값: {sorted(API_DATA_TYPES)}"
            )

    @property
    def is_broadcast(self) -> bool:
        return self.destination_id == BROADCAST_ID
