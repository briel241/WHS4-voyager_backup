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

# 위치보고가 이 시간 동안 오지 않으면 통신 두절로 본다.
# 별도 heartbeat 메시지를 두지 않고 위치보고 도착을 생존 신호로 쓴다.
# 근거는 docs/mass_roc_message_set.md 7.3.
POSITION_REPORT_TIMEOUT_SEC = 10.0

# 시험용 국 식별자 (MMSI). VDE 링크 계층의 Source/Destination ID로 넘어간다.
MASS_MMSI = 440_000_001
ROC_MMSI = 440_000_002
