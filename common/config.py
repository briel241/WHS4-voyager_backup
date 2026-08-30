"""MASS/ROC 응용 계층 기본값"""

from __future__ import annotations

# VDE Payload Format Identifier (IALA R1027, 16 bit)
#
# 정식 VPFI 값 배정은 IALA 소관이다. VPFI 0은 Encapsulated AIS로 이미
# 배정되어 있으므로, 시험용으로 배정되지 않은 대역의 임의값을 쓴다.
# 필드 위치와 크기(16 bit)는 R1027에 맞춰 두었으므로 정식 값이 배정되면
# 이 상수만 바꾸면 된다.
VPFI = 0xFF01

# 위치보고 송신 주기(초).
# AIS는 속력·기동 상태에 따라 2~180초로 가변이지만, 시험에서는 고정한다.
POSITION_REPORT_INTERVAL_SEC = 2.0

# AIS Class A 보고 주기 (ITU-R M.1371 Table 1)
#
# 실제 AIS는 고정 주기가 아니라 속력·선회 여부에 따라 달라진다. 고정 2초로
# 두면 12노트 항해 중에도 표준(10초)의 5배를 내보내 채널 부하가 과장되고,
# PER·성능 측정이 실제와 달라진다.
#
#   정박·계류(3노트 이하)        3분
#   0-14노트                     10초
#   0-14노트 + 침로 변경          3⅓초
#   14-23노트                     6초
#   14-23노트 + 침로 변경          2초
#   23노트 초과                    2초
#
# 선회 판정은 "침로 변경 중"이며, 여기서는 rate_of_turn이 유의미한 값일 때로 본다.
POSITION_REPORT_INTERVAL_ANCHORED = 180.0
POSITION_REPORT_INTERVAL_SLOW = 10.0
POSITION_REPORT_INTERVAL_SLOW_TURNING = 10.0 / 3.0
POSITION_REPORT_INTERVAL_MEDIUM = 6.0
POSITION_REPORT_INTERVAL_MEDIUM_TURNING = 2.0
POSITION_REPORT_INTERVAL_FAST = 2.0

# 위 구간을 가르는 속력(노트)
SPEED_ANCHORED_MAX = 3.0
SPEED_SLOW_MAX = 14.0
SPEED_MEDIUM_MAX = 23.0

# 이 값(도/분)을 넘으면 침로 변경 중으로 본다.
TURNING_RATE_THRESHOLD = 5.0

# ---------------------------------------------------------------------------
# 원격 제어 운용 (IMO MASS Code)
# ---------------------------------------------------------------------------
#
# MASS Code는 "정상 운용 조건을 유지할 수 없을 때를 대비한 사전 정의된 fallback
# 상태와 비상 계획"을 요구한다. 실제 운용에서는 연결이나 시스템 건전성이 기준
# 아래로 떨어지면 선박이 사전 정의된 fallback으로 전환하고 제어권이 본선으로
# 돌아온다.
#
# 제어권을 가진 국에서 이 시간(초) 동안 아무것도 오지 않으면 원격 제어를
# 유지할 수 없다고 본다. 실제 값은 선박의 운항 한계(ODD)와 비상 계획이 정한다.
CONTROL_LINK_TIMEOUT_SEC = 30.0

# 연결이 끊겼을 때 들어갈 fallback. 실제로는 수심·해역·선박 능력에 따라
# 비상 계획이 정하며, 여기서는 위치를 유지하는 쪽을 기본으로 둔다.
DEFAULT_LINK_LOSS_FALLBACK = 2  # route.FALLBACK_DYNAMIC_POSITIONING

# 위치보고가 이 시간 동안 오지 않으면 통신 두절로 본다.
# 별도 heartbeat 메시지를 두지 않고 위치보고 도착을 생존 신호로 쓴다.
# 근거는 docs/mass_roc_message_set.md 7.3.
POSITION_REPORT_TIMEOUT_SEC = 10.0

# 시험용 국 식별자 (MMSI). VDE 링크 계층의 Source/Destination ID로 넘어간다.
MASS_MMSI = 440_000_001
ROC_MMSI = 440_000_002
