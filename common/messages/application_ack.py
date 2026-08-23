"""
Application ACK - 응용 수준 확인응답

근거
    ITU-R M.1371 Annex 5의 IFM FI = 5 "Application acknowledgement"가 선례다.
    IMO SN.1/Circ.289 1.3이 국제 사용을 권고하는 시스템 메시지 4종 중 하나이며,
    같은 문서가 링크 계층 ACK와 명확히 구분한다.

        "...system acknowledgement on the VHF Data Link (VDL) by the receiving
         AIS station. This acknowledgement should not be confused with a user
         acknowledgment."

    즉 VDE Type 92 ACK(전파가 도달했다)와 응용 ACK(명령을 수락했다)는
    서로 다른 층위이며 둘 다 두는 것이 표준 관행이다.

주의
    FI = 5의 정확한 bit 구조는 ITU-R M.1371 Annex 5 소관이며 원문을 확보하지
    못했다. 아래 Status/Reason 값은 Message Linkage ID로 대상을 참조하는
    개념만 차용해 본 프로젝트가 정의한 것이다. 원문 확보 시 교체한다.
    docs/mass_roc_message_set.md 7.1 참조.

필드 구성 (본문 16 bit)
    [Message Linkage ID 10 | Status 3 | Reason 3]
"""

from __future__ import annotations

from dataclasses import dataclass

from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 3
NAME = "Application ACK"

LINKAGE_ID_BITS = 10
STATUS_BITS = 3
REASON_BITS = 3

BODY_BITS = LINKAGE_ID_BITS + STATUS_BITS + REASON_BITS

# Status - 본 프로젝트 정의
STATUS_RECEIVED = 0
STATUS_ACCEPTED = 1
STATUS_REJECTED = 2
STATUS_COMPLETED = 3

STATUS_NAMES = {
    STATUS_RECEIVED: "received",
    STATUS_ACCEPTED: "accepted",
    STATUS_REJECTED: "rejected",
    STATUS_COMPLETED: "completed",
}

# Reason - 본 프로젝트 정의. 거부·실패 사유.
REASON_NONE = 0
REASON_ROUTE_UNSUITABLE = 1
REASON_EXPIRED = 2
REASON_NOT_AUTHORIZED = 3

REASON_NAMES = {
    REASON_NONE: "none",
    REASON_ROUTE_UNSUITABLE: "route unsuitable",
    REASON_EXPIRED: "validity expired",
    REASON_NOT_AUTHORIZED: "not authorized",
}


@dataclass
class ApplicationAck:
    linkage_id: int
    status: int = STATUS_RECEIVED
    reason: int = REASON_NONE

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, f"reserved({self.status})")

    @property
    def reason_name(self) -> str:
        return REASON_NAMES.get(self.reason, f"reserved({self.reason})")


# 본문 bit를 writer에 이어 붙인다.
def write_body(writer: BitWriter, message: ApplicationAck) -> None:
    writer.write_uint(message.linkage_id, LINKAGE_ID_BITS, "Message Linkage ID")
    writer.write_uint(message.status, STATUS_BITS, "Status")
    writer.write_uint(message.reason, REASON_BITS, "Reason")


# reader에서 본문을 읽어 ApplicationAck을 만든다.
def read_body(reader: BitReader) -> ApplicationAck:
    return ApplicationAck(
        linkage_id=reader.read_uint(LINKAGE_ID_BITS, "Message Linkage ID"),
        status=reader.read_uint(STATUS_BITS, "Status"),
        reason=reader.read_uint(REASON_BITS, "Reason"),
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: ApplicationAck) -> str:
    return "\n".join(
        [
            f"Linkage ID: {message.linkage_id}",
            f"Status: {message.status_name}",
            f"Reason: {message.reason_name}",
        ]
    )
