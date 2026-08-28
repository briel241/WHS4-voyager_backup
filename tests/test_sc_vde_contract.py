"""SC-VDE 노선·조종 API 계약 적합성 시험 (가이드 §8.2~§8.4)

링크 팀이 승인한 `sc_vde.route_control.command` / `.ack` 계약을 우리 인코더가
바이트 단위로 지키는지 확인한다. 검사 대상은 셋이다.

    1. `codec.encode_body()`가 승인된 참조 벡터와 정확히 같은 bytes를 낸다
    2. application bytes에 봉투(ff 7f 02/03·임시 ff 01)가 들어가지 않는다
    3. 길이·addressed 규칙 위반을 submit 전에 막는다

VDETER 빌드에 SC-VDE codec이 없어도(§17) 이 시험은 돈다 — 계약은 bytes 수준에서
검증할 수 있기 때문이다.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common import codec, vdeter_bridge as bridge
from common.messages import application_ack as ack
from common.messages import route_instruction as route
from common.outbound import (
    DATA_TYPE_ROUTE_CONTROL_ACK,
    DATA_TYPE_ROUTE_CONTROL_COMMAND,
    Outbound,
)

# 가이드 §8.3·§8.4의 승인된 참조 application bytes (봉투 없음).
APPROVED_ROUTE = bytes.fromhex(
    "01c84018f3ffffe10024 9db9e0282bca0190faa5 00a1c14256c01e0508".replace(" ", "")
)
APPROVED_CONTROL = bytes.fromhex(
    "01c84018f3ffff39d224 9d7cf0282b3400f078a4 ef910141a6f8078284".replace(" ", "")
)
APPROVED_ACK = bytes.fromhex("01d000")


def _route_message():
    """가이드 노선 예제와 같은 노선 (Heading N/A, Fallback None)."""
    return route.RouteInstruction(
        waypoints=[
            route.waypoint_from_degrees(
                129.076, 35.102, turn_radius_nm=0.5, planned_sog_knots=12.5,
                leg_geometry=route.LEG_GEOMETRY_LEXODROME),
            route.waypoint_from_degrees(
                129.333, 35.208, turn_radius_nm=1.2, planned_sog_knots=8.0,
                leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
        ],
        linkage_id=7,
        sender_classification=route.SENDER_AUTHORITY,
    )


def _control_message():
    """가이드 원격조종 예제와 같은 명령 (Heading 92.5°, Anchoring)."""
    return route.RouteInstruction(
        waypoints=[
            route.waypoint_from_degrees(
                129.05, 35.10, turn_radius_nm=0.3, planned_sog_knots=6.0,
                leg_geometry=route.LEG_GEOMETRY_LEXODROME),
            route.waypoint_from_degrees(
                129.10, 35.133, turn_radius_nm=0.3, planned_sog_knots=4.0,
                leg_geometry=route.LEG_GEOMETRY_LEXODROME),
        ],
        heading_setpoint=route.heading_setpoint_from_degrees(92.5),
        fallback_state=route.FALLBACK_ANCHORING,
        linkage_id=7,
        sender_classification=route.SENDER_AUTHORITY,
    )


def test_reference_vectors() -> None:
    """승인된 참조 벡터와 바이트 단위로 일치해야 한다."""
    for name, message, expected in [
        ("노선", _route_message(), APPROVED_ROUTE),
        ("원격조종", _control_message(), APPROVED_CONTROL),
    ]:
        actual = codec.encode_body(message)
        assert actual == expected, (
            f"{name} application bytes 불일치\n"
            f"  기대: {expected.hex(' ')}\n"
            f"  실제: {actual.hex(' ')}"
        )
        assert len(actual) == 29, f"{name}: 29 byte여야 하는데 {len(actual)}"
        print(f"  {name} 참조 벡터 {len(actual)} byte 일치 [OK]")


def test_ack_vector() -> None:
    """ACK은 정확히 3 byte이고 승인 벡터와 같아야 한다."""
    message = ack.ApplicationAck(
        linkage_id=7,
        status=ack.STATUS_ACKNOWLEDGED,
        reason=ack.REASON_NONE,
    )
    actual = codec.encode_body(message)
    assert actual == APPROVED_ACK, (
        f"ACK 불일치\n  기대: {APPROVED_ACK.hex(' ')}\n  실제: {actual.hex(' ')}"
    )
    assert len(actual) == bridge.SC_VDE_ACK_LENGTH
    print(f"  ACK 참조 벡터 {len(actual)} byte 일치 [OK]")


def test_roundtrip_without_envelope() -> None:
    """봉투 없이 보낸 bytes가 payload_format이 알려주는 ID로 복원돼야 한다."""
    body = codec.encode_body(_control_message())
    message_id = bridge.message_id_for_payload_format(
        bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND)
    assert message_id == 2, f"command는 MF 2여야 함 (현재 {message_id})"

    decoded = codec.decode_body(body, message_id)
    assert decoded.heading_setpoint_degrees == 92.5
    assert decoded.fallback_name == "Anchoring"
    assert len(decoded.waypoints) == 2
    print("  봉투 없는 왕복 — heading·fallback·waypoint 보존 [OK]")


def test_envelope_is_rejected() -> None:
    """봉투를 붙이면 submit 전에 막아야 한다 (가이드 §8.2)."""
    body = codec.encode_body(_control_message())
    fmt = bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND

    for label, bad in [
        ("wire header ff 7f 02", bytes.fromhex("ff7f02") + body),
        ("임시 VPFI ff 01", bytes.fromhex("ff01") + bytes([2]) + body),
        ("우리 봉투 포함 encode()", codec.encode(_control_message())),
    ]:
        try:
            bridge.check_sc_vde_payload(fmt, bad)
        except ValueError:
            print(f"  {label} 거부됨 [OK]")
        else:
            raise AssertionError(f"{label}을 막지 못했습니다")


def test_length_rules() -> None:
    """허용되지 않는 길이와 3 byte 아닌 ACK을 막아야 한다."""
    cmd_fmt = bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND
    ack_fmt = bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_ACK

    bridge.check_sc_vde_payload(cmd_fmt, bytes(29))     # 허용 길이
    bridge.check_sc_vde_payload(ack_fmt, bytes(3))      # ACK 정확히 3

    for fmt, bad, label in [
        (cmd_fmt, bytes(30), "command 30 byte"),
        (ack_fmt, bytes(4), "ACK 4 byte"),
    ]:
        try:
            bridge.check_sc_vde_payload(fmt, bad)
        except ValueError:
            print(f"  {label} 거부됨 [OK]")
        else:
            raise AssertionError(f"{label}을 막지 못했습니다")


def test_broadcast_is_rejected() -> None:
    """SC-VDE는 addressed 전용이다 (가이드 §8.3·§8.4)."""
    if not bridge.VDETER_AVAILABLE:
        print("  SKIP: VDETER 없음 (broadcast 규칙)")
        return

    body = codec.encode_body(_control_message())
    outbound = Outbound(
        payload=body,
        destination_id=0,                       # broadcast
        data_type=DATA_TYPE_ROUTE_CONTROL_COMMAND,
    )
    try:
        bridge.to_submit_request(outbound)
    except ValueError:
        print("  broadcast 거부됨 [OK]")
    else:
        raise AssertionError("broadcast를 막지 못했습니다")


def test_data_type_mapping() -> None:
    """data_type이 승인된 payload_format 키로 매핑돼야 한다."""
    pairs = [
        (DATA_TYPE_ROUTE_CONTROL_COMMAND, "sc_vde.route_control.command"),
        (DATA_TYPE_ROUTE_CONTROL_ACK, "sc_vde.route_control.ack"),
    ]
    for data_type, expected in pairs:
        actual = bridge.PAYLOAD_FORMAT_BY_DATA_TYPE[data_type]
        assert actual == expected, f"{data_type} → {actual} (기대 {expected})"
        print(f"  {data_type} → {expected} [OK]")


def main() -> int:
    tests = [
        ("승인 참조 벡터 (노선·조종)", test_reference_vectors),
        ("승인 참조 벡터 (ACK)", test_ack_vector),
        ("봉투 없는 왕복", test_roundtrip_without_envelope),
        ("봉투 포함 거부", test_envelope_is_rejected),
        ("길이 규칙", test_length_rules),
        ("broadcast 금지", test_broadcast_is_rejected),
        ("data_type 매핑", test_data_type_mapping),
    ]
    for name, fn in tests:
        print(f"[{name}]")
        fn()
    print("\nPASS: SC-VDE 노선·조종 API 계약 적합")
    return 0


if __name__ == "__main__":
    sys.exit(main())
