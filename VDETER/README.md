# VDETER

VDE-TER 프로토콜 구현 패키지(`vdeter`). Service·Presentation·Link·MAC·PHY·Radio 계층과
B210 3-endpoint 시스템(coordinator·endpoint launcher), Stage 8 PoC 연결 API를 담는다.
저장소 전체의 설치·자격·실행 절차는 [상위 README](../README.md)와
[apps/standalone/README.md](../apps/standalone/README.md)를 따른다. 이 문서는 패키지
구조와 개발 환경에서의 실행·시험만 다룬다.

## 구조

```text
VDETER/
├─ src/vdeter/
│  ├─ common/          # 공통 DTO, 오류, 식별자, 시간, 직렬화
│  ├─ config/          # TOML 설정 loader·model
│  ├─ service/         # 외부 애플리케이션용 Service API
│  ├─ presentation/    # Payload Format 처리와 registry
│  ├─ link/            # 메시지, 패킷·CRC, 분할·재조립, short data, data session, TBB
│  ├─ mac/             # TBB bootstrap, 자원 요청·할당, ACK/NACK
│  ├─ phy/             # Link ID 11·17·19 PHY profile, FEC, 변조, burst 송수신
│  ├─ radio/           # Radio 인터페이스와 Fake·Software·B210(gr-uhd) adapter
│  ├─ profiles/        # 메시지·Payload·PHY profile과 registry
│  ├─ runtime/         # 계층 조립과 event routing
│  ├─ b210_system/     # B210 3-endpoint 시스템: coordinator·endpoint·worker·IPC·
│  │                   #   host profile·RF time·control_plane, testing/(RR 시험계획·evidence)
│  └─ poc/             # Stage 8 PoC 연결 API: api/(public SDK)·_supervisor/(private)·
│                      #   execution/·shared_medium/·integration/·testing/
├─ apps/               # 프로세스 진입점 (아래)
├─ configs/            # 설정 template — b210_system/{system.toml, endpoints/, profiles/,
│                      #   trust/, testing/}, stage0~5 software/B210 예제
├─ tests/              # unit / contract / integration / hardware / vectors / support
├─ tools/              # 개발 환경 전용 도구 (tools/README.md)
├─ pyproject.toml      # requires-python >=3.10; extras: dsp, crypto, test, vectors
└─ uv.lock
```

`apps/` 진입점:

| 파일 | 역할 |
| --- | --- |
| `coordinator_main.py` | Runtime Coordinator 프로세스. `vdeter run`/`qualify`가 명시적 argv로 spawn |
| `endpoint_launcher.py` | endpoint(MASS-A/MASS-B/ROC) 프로세스 launcher. coordinator가 spawn |
| `scenario_controller.py` | TEST-only Scenario Controller 실행기 |
| `stage0_endpoint.py` | 계층 조립·설정 검증용 빈 endpoint |
| `stage5_service_api_demo.py` | MASS-A → ROC X.509 Service API software radio 예제 |

## 개발 환경 구성

실장비 실행은 `./bin/vdeter setup`이 만드는 `var/venv`를 쓰므로 별도 설치가 필요 없다.
개발 PC에서 패키지만 다룰 때:

```bash
cd VDETER
python -m pip install -e ".[dsp,crypto,test]"     # 또는: uv sync --extra dsp --extra crypto
```

pip 설치 없이 쓰려면 저장소 root에서 `PYTHONPATH=.:VDETER:VDETER/src`를 준다.

## 실행·시험

```bash
python apps/stage0_endpoint.py configs/stage0_fake_endpoint.toml   # 계층 조립 확인
python tools/run_stage7_tests.py --run-id stage7-software-<date>    # B210 시스템 software 시험 + evidence
PYTHONPATH=src python -m pytest tests/unit tests/contract -q         # 시험만 (pip -e 설치 시 PYTHONPATH 불필요)
```

시험 실행 방법은 [tests/README.md](tests/README.md), 도구 목록은
[tools/README.md](tools/README.md). Raspberry Pi 5의 B210 실물 시험 안내는
[`STAGE3_PI5_B210_TESTING.md`](STAGE3_PI5_B210_TESTING.md).

## 결속 영역

`src/vdeter/**`·`configs/b210_system/**`·`apps/coordinator_main.py`·`apps/endpoint_launcher.py`·
`apps/scenario_controller.py`는 standalone 자격의 **rf 영역**이다 — 한 바이트라도 바뀐 설치본은 UNQUALIFIED가 되어 `vdeter qualify`(전체 RF
사다리)를 다시 통과해야 한다. Markdown은 비결속이다. 영역 분류는
`apps/standalone/qualification_domains.py`.
