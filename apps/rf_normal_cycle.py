"""[LEGACY 진단 도구] RF 벤치 테스트용 다리 — 응용 payload를 사람이 링크 콘솔에 옮기는 보조.

⚠ 구형(Stage 3 시절, 2-B210 단방향 벤치) 진단 절차의 보조 도구다. 현재 운영
  경로가 아니다 — MASS/ROC 운영·회귀 경로는 VDE-TER 공개 Service API를 쓴다
  (tests/test_mass_roc_link_integration.py, apps/console_transport.py 참조).
  이 도구 자체는 링크 계층을 import하지 않고 응용 payload bytes만 다루므로
  수동 벤치 진단용으로 보존한다.

실행:
    # 한 사이클의 송신 재료를 뽑는다 (장비 없이)
    python apps/rf_normal_cycle.py payloads

    # 수신기 콘솔이 찍은 Payload (hex)를 응용 메시지로 되돌린다
    python apps/rf_normal_cycle.py decode "FF 01 04 ..."

배경:
    구식 벤치에서는 송신 어댑터가 Type 92/93 Message를 받아 Burst로 만들어
    USRP로 내보냈고, 이 도구는 그 어댑터가 그대로 받을 수 있는 형태(콘솔 hex
    입력 / config.TX_MESSAGES 블록)로 응용 payload를 뽑았다.

    물리 링크는 단방향(A=송신 전용, B=수신 전용)이므로, 이 사이클은 살아있는
    양방향 handshake가 아니라 '한 사이클에 실제로 전파를 타는 네 메시지를
    송신기에서 차례로 내보내고 수신기에서 모두 복원'하는 재현이다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common import codec, config
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.outbound import Outbound
from common.messages import position_report as pos


# 한 사이클에서 전파를 타는 한 건. label과 방향은 사람이 읽기 위한 것이고,
# type_id / destination_id / payload는 링크 어댑터가 실제로 쓰는 값이다.
@dataclass
class OnAir:
    order: int
    direction: str          # "MASS→ROC" 또는 "ROC→MASS"
    label: str
    type_id: int            # 92 (ACK 요구) 또는 93 (ACK 불요)
    source_id: int
    destination_id: int
    payload: bytes


# ack_required가 Type 92/93 선택을 결정한다 (응용 계층의 결정).
def _type_of(outbound: Outbound) -> int:
    return 92 if outbound.ack_required else 93


def _on_air_from(order: int, direction: str, source_id: int, out: Outbound) -> OnAir:
    return OnAir(
        order=order,
        direction=direction,
        label=out.label,
        type_id=_type_of(out),
        source_id=source_id,
        destination_id=out.destination_id,
        payload=out.payload,
    )


# 정상 시나리오 한 사이클이 실제로 전파에 싣는 메시지들을 순서대로 만든다.
#   1) MASS 위치보고           Type 93 브로드캐스트
#   2) ROC 항로 지시           Type 92 지정 (ACK 요구)
#   3) MASS 응용 ACK           Type 93 지정
#   4) MASS 변경된 위치보고     Type 93 브로드캐스트
def build_cycle() -> list[OnAir]:
    mass = MassAgent(
        state=ShipState(
            latitude=35.1000,
            longitude=129.1000,
            heading=90.0,
            sog=12.0,
            navigation_status=0,
        ),
    )
    roc = RocAgent()

    route = [
        (35.1050, 129.1050),
        (35.1120, 129.1080),
        (35.1200, 129.1080),
    ]

    cycle: list[OnAir] = []

    # 1) 명령 전 위치보고 — 침로 90도로 동진 중
    before = pos.from_measurements(
        latitude_degrees=35.1000,
        longitude_degrees=129.1000,
        sog_knots=12.0,
        cog_degrees=90.0,
        heading_degrees=90,
        navigation_status=0,
        autonomy_mode=pos.AUTONOMY_RC,
    )
    cycle.append(OnAir(
        order=1, direction="MASS→ROC", label="Position Report (before)",
        type_id=93, source_id=mass.mmsi, destination_id=0,
        payload=codec.encode(before),
    ))

    # 2) ROC 항로 지시
    command = roc.issue_route(mass.mmsi, route, now=0.0)
    cycle.append(_on_air_from(2, "ROC→MASS", roc.mmsi, command))

    # 3) MASS가 항로를 받아들이고 ACK — 실제 행위자로 생성해 진짜 응답을 싣는다
    replies = mass.receive(command.payload, roc.mmsi, now=0.0)
    if not replies:
        raise RuntimeError("MASS가 항로 지시에 응답하지 않았다 — 사이클 생성 실패")
    for reply in replies:
        cycle.append(_on_air_from(3, "MASS→ROC", mass.mmsi, reply))

    # 4) 명령 후 위치보고 — 새 침로로 선회 중
    after = pos.from_measurements(
        latitude_degrees=35.1010,
        longitude_degrees=129.1012,
        sog_knots=11.5,
        cog_degrees=48.0,
        heading_degrees=48,
        navigation_status=0,
        autonomy_mode=pos.AUTONOMY_RC,
    )
    cycle.append(OnAir(
        order=len(cycle) + 1, direction="MASS→ROC", label="Position Report (after)",
        type_id=93, source_id=mass.mmsi, destination_id=0,
        payload=codec.encode(after),
    ))

    return cycle


# 송신 콘솔(cli/console_tx.py)이 받는 hex 형식: 공백으로 나눈 두 자리 16진수.
def _hex_spaced(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


# 대화식 송신 모드에서 손으로 넣을 입력을 그대로 출력한다.
# 물리 링크가 단방향이므로 송신기(A)는 사이클의 네 메시지를 모두 내보낸다.
def print_console_inputs(cycle: list[OnAir]) -> None:
    print("─" * 72)
    print("대화식 송신 (TX_MODE = \"interactive\") — 아래를 순서대로 프롬프트에 입력")
    print("─" * 72)
    print("송신기는 A(=송신 전용) 한 대다. destination은 payload 안에서 구분되며,")
    print("링크는 브로드캐스트(dest 0)든 지정이든 같은 대역으로 나간다.\n")
    for item in cycle:
        print(f"[{item.order}] {item.direction}  {item.label}  (Type {item.type_id})")
        print(f"      Number of messages [1]: 1")
        print(f"      Message 1 type [92]: {item.type_id}")
        print(f"      Payload (text or hex:01 02 FF): hex:{_hex_spaced(item.payload)}")
        print(f"      Repeat count (blank = infinite): 1")
        print()


# 반복 실행용 config.TX_MESSAGES 블록. 파일을 고치기 싫으면 위의 대화식을 쓴다.
def print_tx_messages_block(cycle: list[OnAir]) -> None:
    print("─" * 72)
    print("반복 실행용 (TX_MODE = \"fixed\") — 구식 링크 벤치 config의 TX_MESSAGES를")
    print("아래로 교체. 한 메시지씩 gain/거리 조정하며 재현할 때 편하다. (LEGACY)")
    print("─" * 72)
    lines = ["TX_MESSAGES = ["]
    for item in cycle:
        payload_hex = "".join(f"{b:02x}" for b in item.payload)
        comment = f"  # [{item.order}] {item.direction} {item.label}"
        if item.type_id == 92:
            lines.append(f"    Type92Message(")
            lines.append(f"        source_id={item.source_id}, session_id=0,")
            lines.append(f"        destination_id={item.destination_id}, retransmission_id=0,")
            lines.append(f"        payload=bytes.fromhex(\"{payload_hex}\"),{comment}")
            lines.append(f"    ),")
        else:
            lines.append(f"    Type93Message(")
            lines.append(f"        source_id={item.source_id}, session_id=0,")
            lines.append(f"        destination_id={item.destination_id},")
            lines.append(f"        payload=bytes.fromhex(\"{payload_hex}\"),{comment}")
            lines.append(f"    ),")
    lines.append("]")
    print("\n".join(lines))
    print()


def cmd_payloads() -> int:
    cycle = build_cycle()

    print("=" * 72)
    print("정상 시나리오 한 사이클 — 전파에 싣는 메시지")
    print(f"  MASS MMSI {config.MASS_MMSI} / ROC MMSI {config.ROC_MMSI} / VPFI 0x{config.VPFI:04X}")
    print("=" * 72)
    print(f"{'#':>2}  {'방향':<9} {'Type':>4} {'dest':>10} {'payload':>7}  메시지")
    for item in cycle:
        dest = "broadcast" if item.destination_id == 0 else str(item.destination_id)
        print(f"{item.order:>2}  {item.direction:<9} {item.type_id:>4} {dest:>10} "
              f"{len(item.payload):>5} B  {item.label}")
    print()

    print_console_inputs(cycle)
    print_tx_messages_block(cycle)

    print("─" * 72)
    print("수신 확인")
    print("─" * 72)
    print("수신기(B) 콘솔이 각 메시지의 'Payload (hex): ..' 줄을 찍는다. 그 hex를")
    print("이 도구의 decode에 넣으면 응용 필드로 복원되는지 확인할 수 있다:")
    print("    python3 apps/rf_normal_cycle.py decode \"<붙여넣은 hex>\"")
    print()
    print("네 메시지가 모두 복원되고 좌표·침로·항로 필드가 위 값과 일치하면 통과다.")
    return 0


def cmd_decode(hex_text: str) -> int:
    cleaned = hex_text.replace("0x", "").replace(",", " ")
    try:
        payload = bytes.fromhex(cleaned.replace(" ", "").replace("\n", ""))
    except ValueError:
        # 공백 구분 형식도 받아준다
        try:
            payload = bytes(int(token, 16) for token in cleaned.split())
        except ValueError:
            print("hex를 해석하지 못했다. 예: \"FF 01 04 12 34\" 또는 \"ff010412\"")
            return 2

    print(f"payload {len(payload)} byte")
    print(f"  hex: {_hex_spaced(payload)}")
    try:
        message = codec.decode(payload)
    except ValueError as error:
        print(f"\n응용 계층이 거부했다: {error}")
        print("  VPFI 불일치이면 우리 payload가 아니거나(남의 트래픽) 비트가 깨진 것이다.")
        print("  M.2092-2 §A4-4.15 원칙: 수신하지 않은 것처럼 행동하되 기록은 남긴다.")
        return 1

    print(f"\n복원됨: {type(message).__name__}")
    print(codec.describe(message))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "decode":
        if len(argv) < 2:
            print("사용법: rf_normal_cycle.py decode \"<hex>\"")
            return 2
        return cmd_decode(" ".join(argv[1:]))
    # 기본은 송신 재료 출력
    return cmd_payloads()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
