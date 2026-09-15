# VDETER

Raspberry Pi 5에서 Stage 3 B210 실물시험을 수행하려면
[`STAGE3_PI5_B210_TESTING.md`](STAGE3_PI5_B210_TESTING.md)를 따른다.

VDE-TER 프로토콜의 Stage 0~4 구현이다. Stage 0~4는 구현·검증을 완료해 각각의 기준선으로
동결됐으며, Stage 5 Service API·Presentation·독립 Software 전체 통합은 기준선 초안 상태로
아직 구현을 시작하지 않았다.

현재 포함 범위는 다음과 같다.

- 단계 0: 공통 계약, 계층별 인터페이스와 런타임 골격
- 단계 1: Link 메시지, 패킷·CRC, 분할·재조립, short data, data session과 TBB
- 단계 2: Link ID 11 PHY profile, FEC, interleaving, scrambling, π/4-QPSK와 burst 송수신 처리
- Service, Presentation, Link, MAC, PHY, Radio 계층의 확장 가능한 인터페이스
- 동일 공개 계약의 Fake·Software·B210 Radio adapter
- Python에서 직접 구성하는 headless programmatic GNU Radio/gr-uhd B210 backend
- 현재 두 B210을 위한 사용자 승인 명목 30 dB 유선 기능시험 profile
- TBB bootstrap, 자원 요청·할당, Short Data, Data Session, ACK/NACK와 3-endpoint Software
  수직 경로를 포함하는 Stage 4 MAC

판정 원본 명세서, 생성된 검증 결과와 과거 실험 자료는 저장소 밖에서 관리한다. 구현 검증에
필요한 시험 코드·고정 벡터·Pi 5 B210 실행 안내는 이 브랜치에 포함한다.

## 구조

```text
VDETER/
├─ src/vdeter/
│  ├─ common/          # 공통 DTO, 오류, 식별자, 시간, 직렬화
│  ├─ service/         # 외부 애플리케이션용 Service API
│  ├─ presentation/    # Payload Format 처리와 registry
│  ├─ link/            # 메시지, 패킷, 전송 절차와 TBB
│  ├─ mac/             # MAC 인터페이스와 모델
│  ├─ phy/             # Link ID 11 송수신 처리
│  ├─ radio/           # Radio 인터페이스와 adapter
│  ├─ profiles/        # 메시지·Payload·PHY profile
│  ├─ config/          # TOML 설정 loader
│  └─ runtime/         # 계층 조립과 event routing
├─ apps/               # 실행 진입점
├─ configs/            # endpoint 설정 예제
├─ pyproject.toml
└─ uv.lock
```

## 환경 구성

Python 3.10 이상과 `uv` 사용을 권장한다.

```powershell
uv sync --extra dsp --extra crypto
```

`uv`를 사용하지 않는 경우 다음과 같이 설치할 수 있다.

```powershell
python -m pip install -e ".[dsp,crypto]"
```

## 실행 확인

현재 제공되는 실행 진입점은 계층 조립과 설정 검증용 endpoint다.

```powershell
uv run python apps/stage0_endpoint.py configs/stage0_fake_endpoint.toml
```

Stage 3 B210 adapter와 장비시험은 `TC-RADIO-012/013/015` 및 최종 evidence audit까지 PASS해
v0.2.4 `VERIFIED/FROZEN`이다. 완성된 MAC·Service 전체 경로의 B210 재검증은 Stage 7에서
수행한다. 상위 MASS/ROC 애플리케이션이 사용할 Service 전체 통합은 Stage 5 이후 범위다.
