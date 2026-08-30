"""확정 3개 시나리오 카탈로그 xlsx 생성.

기존 `docs/시나리오_목록.xlsx`와 같은 열=시나리오 구조를 유지한다. 표를 손으로
고치지 않고 이 스크립트를 고쳐 다시 만든다 — 수치가 코드와 어긋나지 않게 하려는 것이다.

    .venv-crypto/bin/python tools/build_scenario_catalog.py

openpyxl이 필요하다 (`pip install openpyxl`).
"""

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

OUT = "docs/시나리오_카탈로그_확정3종.xlsx"

FONT = "Arial"
INK = "FF1F2937"
HEAD_FILL = "FFDCE6F1"
LABEL_FILL = "FFF2F2F2"
ACCENT_FILL = "FFFDE9F1"   # 조작 시나리오 강조
HAIR = Side(style="thin", color="FFB0BEC5")
BOX = Border(left=HAIR, right=HAIR, top=HAIR, bottom=HAIR)

# (라벨, SC-VDE-001, SC-VDE-005, SC-VDE-006)
ROWS = [
    ("시나리오 ID", "SC-VDE-001", "SC-VDE-005", "SC-VDE-006"),
    ("서비스 / 규격",
     "공개 키 인증서 송수신\nITU-T X.509 v3",
     "노선 계획 교환\nS-421 (S-100)",
     "MASS-ROC 원격 조종\nS-421 조작 데이터"),
    ("목적",
     "ROC가 MASS에 ECDSA 공개키 인증서를 전송한다",
     "ROC가 MASS에 항로(waypoint 열)를 지시하고 MASS가 추종한다",
     "ROC가 MASS에 조종(침로·복귀모드·속력)을 지시하고 MASS가 조타한다"),
    ("사전 조건",
     "신뢰 앵커 사전 탑재 · ROC 키쌍 보유",
     "MASS 위치보고 수신 중 · 세션 수립",
     "노선(SC-VDE-005) 교환 완료 · 제어 세션 수립"),
    ("참여자",
     "ROC 440000002 → MASS 440000001",
     "ROC 440000002 ↔ MASS 440000001",
     "ROC 440000002 ↔ MASS 440000001"),
    ("입력 데이터 · 형식",
     "R1027 Certificate Report 본문 (DER)",
     "노선지시 Message Format 2 (압축 비트)",
     "노선지시 Message Format 2 (Heading·Fallback 채움)"),
    ("입력 데이터 · 크기",
     "application 354 B (인증서 1건)\nwire = application + 2 B (VPFI 0 = 00 01)",
     "application 29 B (WP 2개)\nwire 32 B",
     "application 10 B (조종 단독)\n29 B (조종+WP 2개) / wire +3 B"),
    ("입력 데이터 · 내용",
     "X.509 v3 · secp256r1 · SHA-256 · 지문 48 bit",
     "waypoint(경위도·선회반경·계획SOG·leg) · 유효기간",
     "Heading Setpoint 0.1° · Fallback(투묘/DP/표류) · 계획 SOG"),
    ("payload_format",
     "x509.certificate_report\nVPFI 0 / MF 1 · 운영 scope",
     "명령 sc_vde.route_control.command (MF 2, Type 92)\n"
     "ACK sc_vde.route_control.ack (MF 3, Type 93)\n"
     "위치보고 sc_vde.position_report (MF 1, Type 93)",
     "노선과 동일 — 명령 MF 2 (Type 92)\nACK MF 3 (Type 93) · carrier ff7f 공유"),
    ("메시지 흐름",
     "ROC Type 90(자원요청)→Type 4(자원할당)→Type 74/75/76(fragment ~6)"
     "→MASS 재조립·검증→Type 13 ACK→ROC 전송완료",
     "ROC Type 92(addressed, ACK 요구)→MASS CRC·Dest 검사"
     "→검증 후 항로 반영→Type 13 ACK→MASS 추종",
     "ROC Type 92(addressed, ACK 요구)→MASS CRC·Dest 검사→검증 후 반영"
     "[제어권 MON→RC · 목표침로 · Fallback · 속력]→Type 13 ACK"
     "→MASS 조타 개시→응용 ACK(MF 3, Type 93)→ROC 상관"),
    ("채널 조건",
     "동축 루프백 · 패킷 손실 없음 · Link ID 17",
     "동축 루프백 · Link ID 11 또는 17",
     "동축 루프백 · Link ID 11 또는 17\n(조종 단독 13 B · WP 2개 32 B, WP 3개 42 B부터 Data Session)"),
    ("상태 정의",
     "신뢰저장소 반영 여부 (검증 통과/거부)",
     "제어권 = MON(감독 자율) 유지\n항행상태 0(under way using engine)",
     "제어권 Autonomy Mode 3bit — 1=RC · 2=MON · 3=AUT (프로젝트 정의)\n"
     "Fallback State 3bit — 0=none · 1=Anchoring · 2=DP · 3=Drifting (S-421)\n"
     "Navigation Status 4bit — 0=항행 · 1=at anchor · 2=not under command (AIS)"),
    ("예상 결과",
     "MASS가 검증 통과분만 신뢰저장소에 반영 · 위조본 거부",
     "MASS가 항로 수락(acknowledged) 후 waypoint 추종",
     "MASS가 조종 수락 후 지시 침로로 선회 · Fallback 시 항행 정지"),
    ("실패 조건",
     "서명 불일치 · 만료 · 지문 불일치 → 저장소 미반영",
     "waypoint 없음/좌표 N/A → incomplete · 중복 waypoint → route issues",
     "예약값(Sender·Fallback·Leg) → errors · 권한 위반 → not authorized"),
    ("수집 로그",
     "검증 결과(지문·사유) · 신뢰저장소 변화",
     "발행·ACK 상관 · 항적(계획 대 실제)",
     "제어권 전환(MON↔RC) · 침로 추종 · Fallback 전환 · 링크 두절"),
    ("구현 상태",
     "✅ 운영 종단 (실 VDETER codec)",
     "✅ 운영 종단 (TEST scope)",
     "✅ 운영 종단 (TEST scope)"),
]

NOTES = [
    "확정 3개 시나리오. 크기는 구현 실측값이며 wire = application + VPFI/MF 헤더다.",
    "SC-VDE-006은 SC-VDE-005와 같은 Message Format 2를 쓴다. Heading Setpoint와 "
    "Fallback State를 채웠는지로만 갈리고, codec은 하나다.",
    "VDE Type은 addressed/broadcast가 아니라 ack_required가 가른다 — "
    "92=Short data with ACK(Table 40), 93=Short data without ACK(Table 41). "
    "위치보고는 addressed지만 no-ACK이므로 Type 93이다.",
    "상태는 세 가지가 따로다 — Autonomy Mode(누가 제어하나, 프로젝트 정의), "
    "Fallback State(어떤 안전 상태로 가나, S-421), "
    "Navigation Status(지금 무엇을 하나, AIS M.1371 Table 45). "
    "조종 명령 하나가 셋을 함께 바꾼다.",
    "sc_vde 두 형식은 서명·replay 보호가 없는 TEST 전용이다. 실제 조타에 쓰지 않는다.",
    "참여자 MMSI는 구현 config 기준(MASS 440000001 / ROC 440000002).",
]

wb = Workbook()
ws = wb.active
ws.title = "확정 3종"

ws["A1"] = "MASS/ROC 확정 개발 시나리오 (3종)"
ws["A1"].font = Font(name=FONT, size=14, bold=True, color=INK)
ws.merge_cells("A1:D1")

ws["A2"] = "IALA G1117 서비스 기반 · 크기는 구현 실측 · payload_format은 VDETER registry 등록 기준"
ws["A2"].font = Font(name=FONT, size=9, italic=True, color="FF6B7280")
ws.merge_cells("A2:D2")

start = 4
for i, (label, b, c, d) in enumerate(ROWS):
    r = start + i
    ws.cell(r, 1, label)
    ws.cell(r, 2, b)
    ws.cell(r, 3, c)
    ws.cell(r, 4, d)

    header = i == 0
    for col in range(1, 5):
        cell = ws.cell(r, col)
        cell.border = BOX
        cell.alignment = Alignment(
            wrap_text=True, vertical="center" if header else "top",
            horizontal="center" if header else "left")
        bold = header or col == 1
        cell.font = Font(name=FONT, size=10 if not header else 11, bold=bold, color=INK)
        if col == 1:
            cell.fill = PatternFill("solid", fgColor=LABEL_FILL)
        elif header:
            cell.fill = PatternFill("solid", fgColor=HEAD_FILL)
        elif col == 4 and label in ("메시지 흐름", "채널 조건"):
            cell.fill = PatternFill("solid", fgColor=ACCENT_FILL)

    if label in ("메시지 흐름", "채널 조건"):
        ws.row_dimensions[r].height = 58
    elif label in ("payload_format", "상태 정의"):
        ws.row_dimensions[r].height = 52
    elif "\n" in (b + c + d):
        ws.row_dimensions[r].height = 34

ws.column_dimensions["A"].width = 20
for col in "BCD":
    ws.column_dimensions[col].width = 46

note_row = start + len(ROWS) + 1
ws.cell(note_row, 1, "주석").font = Font(name=FONT, size=10, bold=True, color=INK)
for i, text in enumerate(NOTES):
    r = note_row + 1 + i
    ws.cell(r, 1, f"{i + 1}.")
    ws.cell(r, 1).font = Font(name=FONT, size=9, color="FF6B7280")
    ws.cell(r, 2, text)
    ws.cell(r, 2).font = Font(name=FONT, size=9, color="FF6B7280")
    ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
    ws.row_dimensions[r].height = 26

ws.freeze_panes = "B5"
wb.save(OUT)
print("생성:", OUT)
