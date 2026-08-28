"""제안 codec 검증 — 우리 인코더 출력이 이 codec을 왕복하는지 확인한다.

    .venv-crypto/bin/python docs/proposals/test_sc_vde_position_report.py

이 시험은 제안 codec(`sc_vde_position_report.py`)이 실제로 동작함을 보이기 위한 것이다.
링크 팀이 이관하면 VDETER 쪽 시험으로 옮기면 된다.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for path in (_ROOT, os.path.join(_ROOT, "VDETER", "src"), _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from sc_vde_position_report import (  # noqa: E402
    POSITION_REPORT_APPLICATION_BYTES,
    POSITION_REPORT_WIRE_BYTES,
    SC_VDE_POSITION_REPORT_WIRE_PREFIX,
    ScVdePositionReportCodec,
    read_fields,
)

from common import codec  # noqa: E402
from common.messages import position_report as pos  # noqa: E402

#: 명세(`docs/전선형식_위치보고_SC-VDE-005.md`)의 참조 벡터
REFERENCE = bytes.fromhex("000 1f493b73c05057940760 5ef04".replace(" ", ""))


def _sample():
    return pos.from_measurements(
        longitude_degrees=129.076,
        latitude_degrees=35.102,
        sog_knots=12.5,
        cog_degrees=47.2,
        heading_degrees=47,
        navigation_status=0,
        rate_of_turn=0,
        timestamp=30,
        autonomy_mode=pos.AUTONOMY_MON,
    )


def test_matches_reference_vector() -> None:
    application = codec.encode_body(_sample())
    assert application == REFERENCE, (
        f"참조 벡터 불일치\n  기대: {REFERENCE.hex(' ')}\n  실제: {application.hex(' ')}"
    )
    assert len(application) == POSITION_REPORT_APPLICATION_BYTES
    print(f"  우리 인코더 출력 {len(application)} byte == 명세 참조 벡터 [OK]")


def test_roundtrip_through_codec() -> None:
    c = ScVdePositionReportCodec()
    application = codec.encode_body(_sample())

    encoded = c.encode(application)
    assert encoded.is_ok, f"encode 실패: {getattr(encoded, 'error', encoded)}"
    wire = encoded.value
    assert len(wire) == POSITION_REPORT_WIRE_BYTES, f"wire {len(wire)} byte"
    assert wire[:3] == SC_VDE_POSITION_REPORT_WIRE_PREFIX, wire[:3].hex(" ")
    print(f"  encode → wire {len(wire)} byte, prefix {wire[:3].hex(' ')} [OK]")

    decoded = c.decode(wire)
    assert decoded.is_ok, f"decode 실패: {getattr(decoded, 'error', decoded)}"
    assert decoded.value == application, "왕복 후 bytes 불일치"
    print("  decode → application 복원 일치 [OK]")

    # 응용이 다시 읽어도 값이 살아 있어야 한다.
    message = codec.decode_body(decoded.value, pos.MESSAGE_ID)
    assert round(message.latitude_degrees, 3) == 35.102
    assert round(message.longitude_degrees, 3) == 129.076
    assert message.sog_knots == 12.5
    assert message.cog_degrees == 47.2
    assert message.autonomy_mode == pos.AUTONOMY_MON
    print("  응용 복원 — 위경도·SOG·COG·Autonomy 보존 [OK]")


def test_field_offsets_agree_with_our_encoder() -> None:
    """codec의 bit 오프셋 표가 우리 인코더와 같은 자리를 가리키는지."""
    application = codec.encode_body(_sample())
    raw = read_fields(application)
    message = codec.decode_body(application, pos.MESSAGE_ID)

    pairs = [
        ("navigation_status", message.navigation_status),
        ("sog", message.sog),
        ("longitude", message.longitude),
        ("latitude", message.latitude),
        ("cog", message.cog),
        ("true_heading", message.true_heading),
        ("timestamp", message.timestamp),
        ("autonomy_mode", message.autonomy_mode),
    ]
    for name, expected in pairs:
        assert raw[name] == expected, f"{name}: codec {raw[name]} vs 응용 {expected}"
    print(f"  필드 오프셋 {len(pairs)}개 일치 [OK]")


def test_rejects_bad_input() -> None:
    c = ScVdePositionReportCodec()
    application = bytearray(codec.encode_body(_sample()))

    cases = []

    # 길이 위반
    cases.append(("13 byte", bytes(application[:-1])))
    cases.append(("15 byte", bytes(application) + b"\x00"))

    # padding bit 1
    bad_padding = bytearray(application)
    bad_padding[-1] |= 0x01
    cases.append(("padding bit 1", bytes(bad_padding)))

    # Autonomy Mode 예약값(7) — 마지막 3 bit
    bad_autonomy = bytearray(application)
    bad_autonomy[-1] = (bad_autonomy[-1] & 0x01) | (7 << 1)
    cases.append(("Autonomy Mode 7", bytes(bad_autonomy)))

    for label, bad in cases:
        result = c.encode(bad)
        assert result.is_err, f"{label}을 막지 못했습니다"
        print(f"  {label} 거부됨 [OK]")

    # 잘못된 prefix
    wrong = bytes.fromhex("ff7f02") + bytes(application)
    result = c.decode(wrong)
    assert result.is_err, "잘못된 prefix를 막지 못했습니다"
    print("  잘못된 prefix(ff 7f 02) 거부됨 [OK]")


def main() -> int:
    tests = [
        ("참조 벡터 일치", test_matches_reference_vector),
        ("codec 왕복", test_roundtrip_through_codec),
        ("필드 오프셋 정합", test_field_offsets_agree_with_our_encoder),
        ("잘못된 입력 거부", test_rejects_bad_input),
    ]
    for name, fn in tests:
        print(f"[{name}]")
        fn()
    print("\nPASS: 제안 codec이 우리 인코더와 정합하고 구조 검증도 동작함")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
