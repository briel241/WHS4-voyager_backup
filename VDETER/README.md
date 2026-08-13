# VDETER

VDE-TER 구현을 계층별로 정리한 신규 루트 디렉터리다.

- `error_correction`과 초기 `bitstream` 실험은 원본 그대로 `legacy/`에 보관한다.
- 실제 신규 구조에는 기존 `no_error_correction` 코드만 옮겼다.
- 현재 구현은 Type 92/93 Message, Packet, 시험용 Temporary Burst 및 BPSK
  GNU Radio TX/RX 경로다.

## 구조

```text
VDETER/
├─ src/vdeter/
│  ├─ common/                 # bit/byte 공통 유틸리티
│  ├─ link/
│  │  ├─ messages/            # Type 92/93 codec과 registry
│  │  ├─ packet/              # Packet builder/parser
│  │  └─ config.py            # 링크 송신 모드와 Message 기본값
│  ├─ profiles/               # 표준별 Message/Link ID 프로파일
│  ├─ phy/
│  │  ├─ burst/               # F9A8 기반 Temporary Burst
│  │  ├─ fec/                 # 채널 부호화 구현 예정
│  │  └─ modulation/          # 변복조 구현 예정
│  ├─ mac/                    # MAC 구현 예정
│  ├─ radio/                  # GNU Radio TX/RX adapter
│  └─ cli/                    # 대화식 송신 입력
├─ configs/                   # 장비·운용 환경별 설정
├─ apps/                      # 사용자 실행 진입점
├─ grc/
│  ├─ vdeter_tx.grc
│  ├─ vdeter_rx.grc
│  └─ generated/              # GRC 생성 Python과 Embedded Block wrapper
├─ tests/                     # 단위·통합·유선 장비 시험과 시험 벡터
├─ tools/                     # 개발·분석용 보조 도구
├─ results/                   # 시험 결과와 기준 데이터
├─ docs/                      # VDETER 요구사항·계획·GRC 설정 문서
└─ legacy/
   ├─ error_correction/       # 기존 Turbo FEC 실험
   ├─ bitstream/              # 초기 비트스트림 실험
   ├─ grc/                    # 기존 저장소 루트의 초기 GRC 실험
   └─ docs/                   # 기존 저장소 루트의 장비 안내·WBS 문서
```

## 개발 기준

신규 코드는 `src/vdeter/`에서 개발한다. `legacy/` 파일은 수정하지 않고 비교와
회귀 확인에만 사용한다. VDETER Flowgraph 원본은 `grc/`에 있고 생성 Python은
`grc/generated/`에 둔다. 저장소 루트에 있던 초기 GRC 실험은 `legacy/grc/`에
보관한다.

`profiles/`, `phy/fec/`, `phy/modulation/`, `mac/`과 신규 최상위 디렉터리는
향후 구현 경계를 먼저 고정한 골격이다. 디렉터리가 존재한다고 해당 기능이 구현된
것은 아니다.

Python 패키지를 사용할 환경에서는 다음처럼 editable install한다.

```text
python -m pip install -e .
```

GNU Radio와 PMT는 일반 pip 의존성이 아니라 GNU Radio 실행환경에서 제공한다.
