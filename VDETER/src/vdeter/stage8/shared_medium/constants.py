"""Stage 5 유선 공유매체 방사의 **고정 상수** 단일 정의.

모든 값은 (1) frozen 명세 `stage8_wired_shared_medium_broadcast_spec.md` VERSION 1.3와
(2) G1 RF-off capability evidence(`backups/stage8-wsm-common-lo-g1-20260910/`)의 readback에서만
유도한다. **값·부호를 다시 추정하지 않는다.** 시험 통과 목적으로 이 값을 바꾸지 않는다.

이 module을 import하는 것만으로 thread·socket·disk write가 생기지 않는다(기능 OFF 구조적 0).
"""

from __future__ import annotations

from ..poc_api.types import PhysicalChannel

# =============================================================================
# 1. G1 readback RF 상수 (evidence backups/stage8-wsm-common-lo-g1-20260910/)
#    공통 RF LO + per-lane DSP. UHD RX 규약: effective center = RF LO - DSP.
# =============================================================================

#: 공통 중간 RF LO(Hz). G1 실측(lower/upper midpoint). 세 B210 동일 readback.
COMMON_RF_LO_HZ = 2_449_537_500
#: lower lane DSP offset(Hz). effective = RF LO - (+DSP) → 낮은 주파수.
LOWER_DSP_HZ = 2_300_000
#: upper lane DSP offset(Hz). effective = RF LO - (-DSP) → 높은 주파수.
UPPER_DSP_HZ = -2_300_000
#: 기존 output sample rate(S/s). G1에서 유지됨. Stage 5는 변경하지 않는다.
OUTPUT_RATE_SPS = 307_200
#: 진단 analog RX bandwidth 요청(Hz). ±2.3 MHz DSP 동시 관측 위해 4.7 MHz(≥4.6).
ANALOG_RX_BANDWIDTH_HZ = 4_700_000

#: UHD RX 부호 규약(문서·evidence 표기용). 하드코딩 추측이 아니라 G1 실측 결과.
RX_EFFECTIVE_CONVENTION = "effective = rf_lo - dsp"

#: 두 lane effective center(Hz). RF LO - DSP에서 유도(재추정 아님).
LOWER_EFFECTIVE_CENTER_HZ = COMMON_RF_LO_HZ - LOWER_DSP_HZ  # 2_447_237_500
UPPER_EFFECTIVE_CENTER_HZ = COMMON_RF_LO_HZ - UPPER_DSP_HZ  # 2_451_837_500

#: G1 실측 허용오차(문서화용). 두 lane effective center 오차 ≈ ±0.7 Hz,
#: 두 번째 lane tune 뒤 첫 lane center shift = 0 Hz.
G1_EFFECTIVE_CENTER_TOLERANCE_HZ = 0.7
G1_SECOND_LANE_FIRST_SHIFT_HZ = 0

#: DSP offset → Physical Channel. lower=+DSP, upper=-DSP(effective = LO - DSP).
CHANNEL_DSP_HZ = {
    PhysicalChannel.LOWER: LOWER_DSP_HZ,
    PhysicalChannel.UPPER: UPPER_DSP_HZ,
}
CHANNEL_EFFECTIVE_CENTER_HZ = {
    PhysicalChannel.LOWER: LOWER_EFFECTIVE_CENTER_HZ,
    PhysicalChannel.UPPER: UPPER_EFFECTIVE_CENTER_HZ,
}

# =============================================================================
# 2. 고정 하드웨어 station·serial (명세 §3, 실행 §4.1). 변경하지 않는다.
# =============================================================================

STATION_ROC = "ROC"
STATION_MASS_A = "MASS-A"
STATION_MASS_B = "MASS-B"
ALL_STATIONS = (STATION_ROC, STATION_MASS_A, STATION_MASS_B)

STATION_SERIAL = {
    STATION_MASS_A: "34D03DD",
    STATION_MASS_B: "3515425",
    STATION_ROC: "34D03E8",
}

#: 공격자 endpoint는 MASS-B로 고정(POC-D02). 논리 target은 ROC 또는 MASS-A.
ATTACKER_STATION = STATION_MASS_B
ATTACK_ALLOWED_TARGETS = (STATION_ROC, STATION_MASS_A)

# =============================================================================
# 3. 고정 물리 배선과 송신자별 fan-out (명세 §3 표, §5.1, S8-D005)
#    cable ID는 물리 경로. 선택 Physical Channel의 동일 waveform을 두 cable에 동시 적재.
# =============================================================================

#: cable → (TX station, TX lane, RX station, RX lane). 명세 §3 고정 배선.
CABLE_PATHS = {
    "C1": (STATION_ROC, 0, STATION_MASS_A, 0),
    "C2": (STATION_MASS_A, 0, STATION_ROC, 0),
    "C3": (STATION_ROC, 1, STATION_MASS_B, 0),
    "C4": (STATION_MASS_B, 0, STATION_ROC, 1),
    "C5": (STATION_MASS_A, 1, STATION_MASS_B, 1),
    "C6": (STATION_MASS_B, 1, STATION_MASS_A, 1),
}

#: 송신자 → atomic child paths(정확히 2개). ROC=C1+C3, MASS-A=C2+C5, MASS-B=C4+C6.
SENDER_FANOUT_CABLES = {
    STATION_ROC: ("C1", "C3"),
    STATION_MASS_A: ("C2", "C5"),
    STATION_MASS_B: ("C4", "C6"),
}

#: 단일 논리 burst의 fan-out child 수는 정확히 2(불변). degraded 1-path·순차 fallback 금지.
FANOUT_CHILDREN = 2

# =============================================================================
# 4. 정상 role tuning과 송신자별 자연 관측 행렬 (명세 §5.4, §5.5, WSM-D14)
#    정상 role: ROC=lower 수신, MASS-A=upper 수신, MASS-B=upper 수신.
# =============================================================================

#: 정상 role별 수신 Physical Channel(§5.5). Stage 5는 ROC·MASS-A tuning을 바꾸지 않는다.
ROLE_RX_CHANNEL = {
    STATION_ROC: PhysicalChannel.LOWER,
    STATION_MASS_A: PhysicalChannel.UPPER,
    STATION_MASS_B: PhysicalChannel.UPPER,
}

#: 송신자별 물리 도달(fan-out) 비송신 endpoint 두 곳(§3 표). addressed 무관 물리 도달.
SENDER_PHYSICAL_REACH = {
    STATION_ROC: (STATION_MASS_A, STATION_MASS_B),
    STATION_MASS_A: (STATION_ROC, STATION_MASS_B),
    STATION_MASS_B: (STATION_ROC, STATION_MASS_A),
}

#: §5.5 자연 관측 행렬. key=(sender, channel) → 자연적으로 TUNED인 비송신 endpoint 집합.
#: 이 값이 delivery matrix 기대값이다. 두 수신기 무조건 decode를 요구하지 않는다.
NATURAL_TUNED_MATRIX = {
    (STATION_ROC, PhysicalChannel.UPPER): frozenset({STATION_MASS_A, STATION_MASS_B}),
    (STATION_MASS_A, PhysicalChannel.LOWER): frozenset({STATION_ROC}),
    (STATION_MASS_A, PhysicalChannel.UPPER): frozenset({STATION_MASS_B}),
    (STATION_MASS_B, PhysicalChannel.LOWER): frozenset({STATION_ROC}),
    (STATION_MASS_B, PhysicalChannel.UPPER): frozenset({STATION_MASS_A}),
}

# =============================================================================
# 5. 자원 상한 (bounded; 명세 §9.2 "bounded memory enqueue", §13 크기 상한)
#    보수적 고정 상한. 시험 통과 목적으로 완화하지 않는다.
# =============================================================================

#: 한 receiver·channel·overlap window의 collision group에 포함되는 최대 burst 수.
MAX_COLLISION_GROUP_BURSTS = 8
#: 단일 burst의 최대 IQ complex sample 수(byte-pinned fixture·합성 buffer 상한).
MAX_BURST_SAMPLES = 1 << 16  # 65536
#: 한 parent transmission이 만드는 fan-out child 수 상한(= FANOUT_CHILDREN).
MAX_FANOUT_CHILDREN = FANOUT_CHILDREN
#: delivery matrix에 기록·직렬화하는 row 상한(bounded evidence). 초과분은 dropped counter로만.
MAX_DELIVERY_MATRIX_ENTRIES = 4096
#: 동시에 in-flight(commit됐으나 terminal 미확정)일 수 있는 broadcast group 상한.
MAX_ACTIVE_BROADCAST_GROUPS = 64
#: MAC scheduler trigger registry 상한(workload당 bounded; 초과 시 가장 오래된 것부터 evict).
MAX_TRIGGER_REGISTRY = 4096


__all__ = (
    "COMMON_RF_LO_HZ",
    "LOWER_DSP_HZ",
    "UPPER_DSP_HZ",
    "OUTPUT_RATE_SPS",
    "ANALOG_RX_BANDWIDTH_HZ",
    "RX_EFFECTIVE_CONVENTION",
    "LOWER_EFFECTIVE_CENTER_HZ",
    "UPPER_EFFECTIVE_CENTER_HZ",
    "G1_EFFECTIVE_CENTER_TOLERANCE_HZ",
    "G1_SECOND_LANE_FIRST_SHIFT_HZ",
    "CHANNEL_DSP_HZ",
    "CHANNEL_EFFECTIVE_CENTER_HZ",
    "STATION_ROC",
    "STATION_MASS_A",
    "STATION_MASS_B",
    "ALL_STATIONS",
    "STATION_SERIAL",
    "ATTACKER_STATION",
    "ATTACK_ALLOWED_TARGETS",
    "CABLE_PATHS",
    "SENDER_FANOUT_CABLES",
    "FANOUT_CHILDREN",
    "ROLE_RX_CHANNEL",
    "SENDER_PHYSICAL_REACH",
    "NATURAL_TUNED_MATRIX",
    "MAX_COLLISION_GROUP_BURSTS",
    "MAX_BURST_SAMPLES",
    "MAX_FANOUT_CHILDREN",
    "MAX_DELIVERY_MATRIX_ENTRIES",
    "MAX_ACTIVE_BROADCAST_GROUPS",
    "MAX_TRIGGER_REGISTRY",
)
