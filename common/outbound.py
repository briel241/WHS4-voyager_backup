"""
행위자가 내보내는 송신 요청

행위자는 VDE 링크 계층을 직접 알지 못한다. 대신 "이 payload를 누구에게,
ACK를 요구하며 보내라"는 요청만 만들고, 링크 연결은 어댑터가 맡는다.
이렇게 두면 mass_roc 패키지가 VDETER_Link에 의존하지 않는다.

필드 이름과 의미는 `docs/vde_service_api(prototype).md`의 `SendRequest` 계약을
그대로 따른다. 그 문서 3.1.2와 6장이 정한 경계는 다음과 같다.

    API 사용자는 Link ID, Message Type, Session ID, Retransmission ID,
    VPFI, CRC, FEC, Burst byte 또는 GNU Radio 객체를 만들거나 전달하지 않는다.

따라서 여기에는 Type 92/93 같은 링크 계층 값을 두지 않는다. `ack_required`가
점대점 ACK 필요 여부를 나타내고, 그것을 어떤 Message Type으로 실을지는
VDE-TER Service가 정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

# broadcast 수신자 ID. API 계약 3.1.2가 정한 값이다.
BROADCAST_ID = 0

# docs/vde_service_api(prototype).md 1장이 정의한 application data 분류.
DATA_TYPE_TBB = "TBB"
DATA_TYPE_AIS_AUTH_INTEGRITY = "AIS_AUTH_INTEGRITY"
DATA_TYPE_ELECTRONIC_CHART = "ELECTRONIC_CHART"
DATA_TYPE_X509_CERTIFICATE = "X509_CERTIFICATE"

API_DATA_TYPES = frozenset(
    {
        DATA_TYPE_TBB,
        DATA_TYPE_AIS_AUTH_INTEGRITY,
        DATA_TYPE_ELECTRONIC_CHART,
        DATA_TYPE_X509_CERTIFICATE,
    }
)

# 주의
#     위 네 가지에는 MASS/ROC 운항 메시지(위치보고·항로지시·응답)에 해당하는
#     값이 없다. 즉 현재 API 계약으로는 우리 운항 메시지를 보낼 분류가 없다.
#     API 소유자와 합의해 분류를 추가해야 하며, 그전까지 data_type은 None으로
#     둔다. 임의로 만들어 쓰지 않는다.
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
                "(vde_service_api 3.1.2, M.2092-2 Table 40)"
            )
        if self.data_type is not None and self.data_type not in API_DATA_TYPES:
            raise ValueError(
                f"API 계약에 없는 data_type입니다 ({self.data_type!r}). "
                f"허용 값: {sorted(API_DATA_TYPES)}"
            )

    @property
    def is_broadcast(self) -> bool:
        return self.destination_id == BROADCAST_ID
