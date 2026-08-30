"""SC-VDE-003 AIS 인증·무결성 값 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_ais_auth.py

시나리오: docs/scenarios/scenario_list.md SC-VDE-003

여기서 보는 것
    - 인증값 메시지 구조와 크기가 설계대로인가
    - 정상 인증값이 검증을 통과하는가
    - 위조·재전송·미확보 원본이 확실히 거부되는가
    - 검증 실패가 조용히 폐기되되 기록은 남는가 (M.2092-2 A4-4.15)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common import codec, security
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.outbound import DATA_TYPE_AIS_AUTH_INTEGRITY
from common.messages import ais_auth

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# 시험 대상 AIS 메시지 (AIS 채널로 수신했다고 가정)
AIS_RAW = b"!AIVDM,1,1,,A,14eG;o@034o8sd<L9i:a;WF>062D,0*7D"
AIS_MMSI = 440_000_009
AIS_TYPE = 1


def make_pair() -> tuple[RocAgent, MassAgent]:
    roc = RocAgent()
    mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))
    # 신뢰 앵커 사전 탑재. 무선 배포는 SC-VDE-001의 일이다.
    mass.trust(roc.fingerprint, roc.public_key, roc.mmsi)
    mass.observe_ais(AIS_MMSI, AIS_TYPE, AIS_RAW)
    return roc, mass


# ------------------------------------------------------- 백엔드

section("서명 백엔드 — M.2092-2 A4-4.15")

backend = security.default_backend()
print(f"     사용 중: {backend.name}")
if not backend.is_standard:
    print("     ⚠ 표준 백엔드가 아니다. 보안 실증 전에 다음을 실행해야 한다:")
    print("       /opt/homebrew/bin/python3 -m pip install cryptography")

check(security.SIGNATURE_SIZE == 64, "서명 크기 64 byte", "512 bit")
check(security.FINGERPRINT_SIZE == 6, "인증서 지문 6 byte", "48 bit")
check(security.CURVE_NAME == "secp256r1", "곡선 secp256r1")
check(security.DIGEST_NAME == "SHA-256", "다이제스트 SHA-256")

private_key, public_key = backend.generate_keypair()
signature = backend.sign(private_key, b"hello")
check(len(signature) == 64, "서명이 64 byte로 나온다")
check(backend.verify(public_key, b"hello", signature), "정상 서명이 검증된다")
check(not backend.verify(public_key, b"hello!", signature), "다른 데이터는 거부된다")
check(not backend.verify(public_key, b"hello", b"\x00" * 64), "위조 서명은 거부된다")

# 인증서 지문 = signatureValue 최하위 48 bit (A4-4.15)
fake_cert_sig = bytes(range(20))
check(
    security.fingerprint_from_certificate_signature(fake_cert_sig) == fake_cert_sig[-6:],
    "지문은 signatureValue 최하위 6 byte",
    "RFC 5480 4.1.1.3",
)

# ------------------------------------------------------- 메시지 구조

section("메시지 구조")

check(ais_auth.BODY_BITS == 628, "본문 628 bit",
      f"지문48+MMSI30+타입6+유효기간32+서명512 = {ais_auth.BODY_BITS}")

roc, mass = make_pair()
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)

check(len(outbound.payload) == 82, "payload 82 byte", f"{len(outbound.payload)} byte")
check(outbound.data_type == DATA_TYPE_AIS_AUTH_INTEGRITY, "data_type이 AIS_AUTH_INTEGRITY")
check(outbound.is_broadcast, "브로드캐스트로 나간다", "AIS 자체가 브로드캐스트")
check(not outbound.ack_required, "ACK를 요구하지 않는다 (Type 93)")

value = codec.decode(outbound.payload)
check(isinstance(value, ais_auth.AisAuthValue), "AisAuthValue로 복원된다")
check(value.target_mmsi == AIS_MMSI, "대상 MMSI 복원")
check(value.ais_message_type == AIS_TYPE, "AIS 메시지 타입 복원")
check(len(value.signature) == 64, "서명 64 byte 복원")
check(value.fingerprint == roc.fingerprint, "인증서 지문 복원")

section("용량 — Link ID별 수용 여부")

for link_id, packet, header, fits in [(11, 54, 13, False), (17, 234, 13, True), (19, 702, 13, True)]:
    available = packet - 4 - header
    actual = available >= len(outbound.payload)
    check(actual == fits,
          f"Link ID {link_id}: 가용 {available} B — {'수용' if fits else '초과'}",
          f"인증값 {len(outbound.payload)} B")

# ------------------------------------------------------- 정상 흐름

section("정상 흐름")

roc, mass = make_pair()
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)
mass.receive(outbound.payload, roc.mmsi, 10.0)

check(len(mass.ais_results) == 1, "검증 결과가 기록된다")
result = mass.ais_results[-1]
check(result.verified, "정상 인증값이 검증을 통과한다", f"reason={result.reason}")
check(mass.ais_verified_count == 1 and mass.ais_failed_count == 0, "성공 집계")

# VDETER 지원 포맷으로 매핑되는 data_type을 단다
check(outbound.data_type == "AIS_AUTH_INTEGRITY", "AIS 인증값 data_type")

# ------------------------------------------------------- 공격 케이스

section("공격 — 값 위조")

roc, mass = make_pair()
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)

# 서명 한가운데 바이트를 뒤집는다.
# payload 끝 4 bit는 byte 정렬 padding이라 서명이 아니다.
tampered = bytearray(outbound.payload)
tampered[50] ^= 0xFF
mass.receive(bytes(tampered), roc.mmsi, 10.0)
check(not mass.ais_results[-1].verified, "위조된 서명을 거부한다")
check(mass.ais_results[-1].reason == "bad-signature", "사유가 bad-signature")

# padding이 깨진 것은 우리 payload 형식이 아니므로 디코딩 단계에서 걸러진다
roc2, mass2 = make_pair()
broken = bytearray(roc2.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW).payload)
broken[-1] ^= 0x0F
before_ignored = mass2.ignored_count
mass2.receive(bytes(broken), roc2.mmsi, 10.0)
check(mass2.ignored_count == before_ignored + 1 and not mass2.ais_results,
      "형식이 깨진 payload는 디코딩에서 걸러진다", "잡음과 같은 취급")

section("공격 — 원본 AIS 메시지 위조")

roc, mass = make_pair()
# MASS가 들은 AIS 메시지가 공격자에 의해 바뀐 경우
mass.observe_ais(AIS_MMSI, AIS_TYPE, b"!AIVDM,1,1,,A,FORGED-POSITION,0*00")
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)
mass.receive(outbound.payload, roc.mmsi, 10.0)
check(not mass.ais_results[-1].verified, "원본이 다르면 검증에 실패한다",
      "AIS 메시지 위조를 잡아낸다")

section("공격 — 모르는 인증서로 서명")

roc, mass = make_pair()
attacker = RocAgent(mmsi=999_999_999)
forged = attacker.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)
mass.receive(forged.payload, attacker.mmsi, 10.0)
check(not mass.ais_results[-1].verified, "신뢰하지 않는 인증서를 거부한다")
check(mass.ais_results[-1].reason == "unknown-certificate", "사유가 unknown-certificate")

section("공격 — 대상 AIS 메시지를 듣지 못한 경우")

roc = RocAgent()
mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))
mass.trust(roc.fingerprint, roc.public_key, roc.mmsi)
# observe_ais를 하지 않는다
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0)
mass.receive(outbound.payload, roc.mmsi, 10.0)
check(not mass.ais_results[-1].verified, "원본이 없으면 검증하지 않는다")
check(mass.ais_results[-1].reason == "no-such-ais-message", "사유가 no-such-ais-message")

section("공격 — 유효기간 경과 (재전송 방어)")

roc, mass = make_pair()
# Valid until을 100초로 두고 200초에 수신
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0, valid_until=100)
mass.receive(outbound.payload, roc.mmsi, 200.0)
check(not mass.ais_results[-1].verified, "유효기간이 지난 값을 거부한다")
check(mass.ais_results[-1].reason == "expired", "사유가 expired")

# 유효기간 안이면 통과
roc, mass = make_pair()
outbound = roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=10.0, valid_until=100)
mass.receive(outbound.payload, roc.mmsi, 50.0)
check(mass.ais_results[-1].verified, "유효기간 안에서는 통과한다")

section("검증 실패의 부작용 — A4-4.15")

roc, mass = make_pair()
mass.state.heading = 90.0
before_heading = mass.state.heading
before_route = mass.has_route

forged_payload = bytearray(roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW).payload)
forged_payload[50] ^= 0xFF
mass.receive(bytes(forged_payload), roc.mmsi, 10.0)

check(mass.state.heading == before_heading and mass.has_route == before_route,
      "검증 실패가 항해 상태를 바꾸지 않는다",
      "수신하지 않은 것처럼 행동한다")
check(len(mass.ais_results) > 0, "그래도 기록은 남는다", "관찰 도구가 공격을 보여줄 수 있다")

section("여러 건 집계")

roc, mass = make_pair()
for i in range(3):
    mass.receive(roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW, now=float(i)).payload,
                 roc.mmsi, float(i))
bad = bytearray(roc.issue_ais_auth(AIS_MMSI, AIS_TYPE, AIS_RAW).payload)
bad[50] ^= 0xFF
mass.receive(bytes(bad), roc.mmsi, 10.0)

check(mass.ais_verified_count == 3, "성공 3건", f"{mass.ais_verified_count}건")
check(mass.ais_failed_count == 1, "실패 1건", f"{mass.ais_failed_count}건")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
