"""
AIS Authentication / Integrity Value — SC-VDE-003

목적
    AIS 채널로 수신한 AIS 메시지의 진위와 무결성을, VDE-TER로 따로 전달되는
    값으로 검증한다. AIS 자체에는 인증이 없으므로 VDE-TER가 사후에 보증한다.

표준 근거와 그 한계
    M.2092-2는 "VDES가 디지털 서명을 검증하고 AIS 메시지 인증을 지원해야 한다"고
    권고(recommends 15)하지만, **AIS 인증값의 메시지 구조는 정의하지 않는다.**
    "Authentication and integrity sequence"라는 필드는 문서 전체에서 불리틴 보드
    (Type 20)에 한 번 나올 뿐이다. 실제 규격은 IALA G1192와 IEC 63514(개발 중)
    소관이며 본 문서 작성 시점에 확보하지 못했다.

    따라서 아래 구조는 **표준에서 확정된 값만 차용하고 나머지는 본 프로젝트가
    정의한 것**이다. 확정된 부분과 정의한 부분을 표로 구분해 둔다.

    | 필드                 | 크기    | 출처                          |
    | -------------------- | ------- | ----------------------------- |
    | Certificate fingerprint | 48 bit | **A4-4.15 (표준)**            |
    | Target MMSI          | 30 bit  | AIS MMSI 크기 (M.1371)        |
    | AIS message type     |  6 bit  | AIS Message ID 크기 (M.1371)  |
    | Valid until          | 32 bit  | **Table 55 패턴 차용 (표준)** |
    | Signature            | 512 bit | **A4-4.15 (표준)**            |

서명 범위
    A4-4.29의 방식을 따른다 — 서명자 식별자를 앞에 붙여 계산하되 전송하지 않는다.
    이렇게 하면 신분 사칭을 전송 비용 없이 막는다.

        signer_mmsi || target_mmsi || ais_message_type || valid_until
        || sha256(원본 AIS 메시지)

    검증자는 AIS 채널로 받은 원본 메시지를 가지고 있어야 한다. 그것이 이 값의
    존재 이유다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..bitfield import BitReader, BitWriter
from ..security import signature as sig

MESSAGE_ID = 4
NAME = "AIS Auth/Integrity"

FINGERPRINT_BITS = sig.FINGERPRINT_SIZE * 8  # 48
TARGET_MMSI_BITS = 30
AIS_MESSAGE_TYPE_BITS = 6
VALID_UNTIL_BITS = 32
SIGNATURE_BITS = sig.SIGNATURE_SIZE * 8  # 512

BODY_BITS = (
    FINGERPRINT_BITS
    + TARGET_MMSI_BITS
    + AIS_MESSAGE_TYPE_BITS
    + VALID_UNTIL_BITS
    + SIGNATURE_BITS
)

# Table 55와 같은 뜻 — 유효기간을 두지 않음
VALID_UNTIL_NONE = 0


@dataclass
class AisAuthValue:
    fingerprint: bytes
    target_mmsi: int
    ais_message_type: int
    signature: bytes
    valid_until: int = VALID_UNTIL_NONE

    def __post_init__(self) -> None:
        if len(self.fingerprint) != sig.FINGERPRINT_SIZE:
            raise ValueError(
                f"인증서 지문은 {sig.FINGERPRINT_SIZE} byte여야 합니다 "
                f"({len(self.fingerprint)})"
            )
        if len(self.signature) != sig.SIGNATURE_SIZE:
            raise ValueError(
                f"서명은 {sig.SIGNATURE_SIZE} byte여야 합니다 ({len(self.signature)})"
            )

    @property
    def has_expiry(self) -> bool:
        return self.valid_until != VALID_UNTIL_NONE

    # 유효기간이 지났는지 판정한다. now는 1970-01-01 UTC 기준 초.
    def is_expired(self, now: float) -> bool:
        return self.has_expiry and now > self.valid_until


# 서명 대상 바이트열을 만든다. 서명자 MMSI는 앞에 붙이되 전송하지 않는다.
def signing_input(
    signer_mmsi: int,
    target_mmsi: int,
    ais_message_type: int,
    valid_until: int,
    ais_message: bytes,
) -> bytes:
    return b"".join(
        [
            signer_mmsi.to_bytes(4, "big"),
            target_mmsi.to_bytes(4, "big"),
            ais_message_type.to_bytes(1, "big"),
            valid_until.to_bytes(4, "big"),
            hashlib.sha256(ais_message).digest(),
        ]
    )


# 원본 AIS 메시지에 대한 인증값을 만든다.
def build(
    private_key,
    fingerprint: bytes,
    signer_mmsi: int,
    target_mmsi: int,
    ais_message_type: int,
    ais_message: bytes,
    valid_until: int = VALID_UNTIL_NONE,
    backend: sig.SignatureBackend | None = None,
) -> AisAuthValue:
    engine = backend or sig.default_backend()
    payload = signing_input(
        signer_mmsi, target_mmsi, ais_message_type, valid_until, ais_message
    )
    return AisAuthValue(
        fingerprint=fingerprint,
        target_mmsi=target_mmsi,
        ais_message_type=ais_message_type,
        signature=engine.sign(private_key, payload),
        valid_until=valid_until,
    )


# 인증값이 원본 AIS 메시지에 대해 유효한지 검증한다.
#
# 실패 사유를 구분해 돌려준다. 검증 실패는 조용히 폐기하되 기록은 남겨야
# 하므로(A4-4.15) 사유가 필요하다.
def verify(
    value: AisAuthValue,
    public_key,
    signer_mmsi: int,
    ais_message: bytes,
    now: float = 0.0,
    backend: sig.SignatureBackend | None = None,
) -> tuple[bool, str]:
    if value.is_expired(now):
        return False, "expired"

    engine = backend or sig.default_backend()
    payload = signing_input(
        signer_mmsi,
        value.target_mmsi,
        value.ais_message_type,
        value.valid_until,
        ais_message,
    )
    if not engine.verify(public_key, payload, value.signature):
        return False, "bad-signature"
    return True, "ok"


def write_body(writer: BitWriter, message: AisAuthValue) -> None:
    for byte in message.fingerprint:
        writer.write_uint(byte, 8, "Certificate fingerprint")
    writer.write_uint(message.target_mmsi, TARGET_MMSI_BITS, "Target MMSI")
    writer.write_uint(message.ais_message_type, AIS_MESSAGE_TYPE_BITS, "AIS message type")
    writer.write_uint(message.valid_until, VALID_UNTIL_BITS, "Valid until")
    for byte in message.signature:
        writer.write_uint(byte, 8, "Signature")


def read_body(reader: BitReader) -> AisAuthValue:
    fingerprint = bytes(
        reader.read_uint(8, "Certificate fingerprint") for _ in range(sig.FINGERPRINT_SIZE)
    )
    target_mmsi = reader.read_uint(TARGET_MMSI_BITS, "Target MMSI")
    ais_message_type = reader.read_uint(AIS_MESSAGE_TYPE_BITS, "AIS message type")
    valid_until = reader.read_uint(VALID_UNTIL_BITS, "Valid until")
    signature = bytes(reader.read_uint(8, "Signature") for _ in range(sig.SIGNATURE_SIZE))
    return AisAuthValue(
        fingerprint=fingerprint,
        target_mmsi=target_mmsi,
        ais_message_type=ais_message_type,
        signature=signature,
        valid_until=valid_until,
    )


def describe(message: AisAuthValue) -> str:
    expiry = "없음" if not message.has_expiry else str(message.valid_until)
    return "\n".join(
        [
            f"Certificate fingerprint: {message.fingerprint.hex(' ').upper()}",
            f"Target MMSI: {message.target_mmsi}",
            f"AIS message type: {message.ais_message_type}",
            f"Valid until: {expiry}",
            f"Signature: {message.signature[:8].hex(' ').upper()} … "
            f"({len(message.signature)} byte)",
        ]
    )
