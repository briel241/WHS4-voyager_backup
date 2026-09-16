# voyager_3

자율운항선박(MASS)↔원격운영센터(ROC) VDE-TER 무선통신 보안성 검토·암호통신 프로젝트
(Whitehat School 4기). 링크·PHY·MAC·B210 시스템은 [VDETER/](VDETER/),
MASS/ROC 응용 계층은 [MASS/](MASS/)·[ROC/](ROC/)·[common/](common/), 단독 실행 CLI는
[apps/standalone/](apps/standalone/)에 있다.

## 구조

```text
bin/vdeter                 저장소 launcher (setup 전엔 시스템 python3, 이후 var/venv)
apps/standalone/           vdeter CLI — setup / qualify / run / results / capability / diag
apps/                      MASS/ROC workload controller·시나리오 runner·콘솔 서버·RF-off 세션
VDETER/src/vdeter/         VDE-TER 계층 구현 (link·mac·phy·radio·b210_system·poc …)
VDETER/apps/               coordinator·endpoint launcher 등 프로세스 진입점
VDETER/configs/b210_system/  system·endpoint·profile template, trust store
VDETER/tests/, tests/      계층 시험 / 응용·standalone 시험
VDETER/tools/, tools/      개발 환경 전용 도구 (배포 패키징·evidence 실행기 등)
docs/                      사용법·인수인계·시나리오·메시지 형식
var/, identity/            실행 state (git 밖) — 자격 release·run evidence·키
```

## 환경 설정

실행 장비: Raspberry Pi 5(또는 Ubuntu PC) 한 대 + Ettus B210 세 대(MASS-A·MASS-B·ROC).
Python 3.10 이상. 설계·결속 규칙은 [apps/standalone/README.md](apps/standalone/README.md).

```bash
cd <repository>
./bin/vdeter setup            # 기계당 1회, 멱등
```

`setup`이 하는 일: apt 패키지(gnuradio, uhd-host, python3-numpy, python3-venv) 확인·설치
→ `var/venv` 생성과 `requirements-standalone.lock` 설치 → identity(서명 키·인증서·trust
store) 생성(이미 있으면 유지) → 연결된 B210 관측과 `hardware.toml` 초안 → 설치 파일
실측(`var/installation/`) → `~/.local/bin/vdeter` 명령 설치.

이후 `hardware.toml`을 편집한다. 역할별 B210 serial, MASS-B 외부 전원 자산(또는
powered-USB3-hub 선언), 배선 프로파일을 기입하고 배선을 확인한 뒤 `wiring.approved = true`로
바꾼다. 역할 배정과 배선 승인은 자동으로 하지 않는다.

## 실행

```bash
vdeter qualify --yes          # 새 환경에서 1회 — software 묶음 → RF 자격 사다리(수 시간)
vdeter run                    # 대화형 메뉴
vdeter run all                # SC-VDE-005 → 006 → 001 normal → 001 forged
vdeter run SC-VDE-001 --case forged --json
vdeter results [run-id]       # run 목록 / 저장된 evidence 상세
vdeter capability [show|issue]  # 설치 capability 증언 (RF-off)
```

- `qualify`는 software 검증 묶음(unit·contract·integration·frozen-hash audit)을 **먼저**
  돌리고, 통과했을 때만 RF 사다리(RR-P0 → RR-P1 gain → RR-P2-17 → RR-P3-fault)를 시작한다.
  전부 PASS면 active qualification reference를 원자적으로 발급한다.
- 새 기계·새 설치본·새 identity·라이브러리 버전 변경·rf/operation 영역 파일 변경은
  UNQUALIFIED로 시작하며 `vdeter run`은 RF 접근 전에 거부한다. tooling(자격 도구·시험
  코드) 변경은 RF 사다리 없이 software 묶음 재검증(evidence reuse)만 요구하고, Markdown
  변경은 자격에 영향이 없다.
- 자격은 외부 공인 인증이 아닌 프로젝트 내부 RESEARCH_READY 자격이다.
- amd64 Ubuntu 지원 코드는 구현돼 있으나 실제 amd64 시험은 장비 부재로 유보됐다.
- 공통 옵션: `--no-progress`(진행바 대신 줄 단위 상태), `--diagnostics`(evidence 계측 상세),
  `--no-retry`(bring-up 자동 재시도 1회 비활성).

### Stage 8 PoC 실행 (opt-in)

```bash
vdeter run SC-VDE-005 --poc <package>.py       # 외부 PoC 패키지를 시나리오에 붙여 실행
vdeter run --poc-dir <catalog-dir>             # 대화형: 시나리오 먼저, PoC 다음
vdeter run SC-VDE-005 --poc <p>.py --rf-off-software   # B210 없이 실 coordinator RF-off 검증
```

`--poc`/`--poc-dir`가 없으면 PoC 코드는 로드되지 않는다. 공개 SDK는 `vdeter.poc.api`.

### 개발용 통제 RF 진단 (미자격 후보)

```bash
vdeter diag authorize --calibration-reference <reference.json> --plan <plan.json>
vdeter diag run [SC-VDE-00x ...] [--hold-after-ready S] [--poc PATH]
vdeter diag status
```

qualification이 아니며 reference를 발급·승격하지 않는다. 정상 `run`의 UNQUALIFIED 거부는
그대로다.

### 응용 계층만 따로

```bash
python apps/scenario_runner.py [SC-VDE-00x] [--json]   # 시나리오 재현 (장비 없음)
python apps/console_server.py [--host H] [--port P]    # 콘솔 GUI — 브라우저에서 ROC/MASS
python -m pytest tests -q                              # 응용·standalone 시험
```

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/사용법.md](docs/사용법.md) | 동작 원리 + 상황별 사용법 |
| [docs/인수인계.md](docs/인수인계.md) | 구조·진행상황·남은 작업 |
| [docs/구조_이름_대응표.md](docs/구조_이름_대응표.md) | 옛 모듈 이름(`stage7`·`stage8`) → 현행 이름 대응표 |
| [docs/scenarios/scenario_list.md](docs/scenarios/scenario_list.md) | 시나리오 목록·선정 현황 |
| [apps/standalone/README.md](apps/standalone/README.md) | standalone 실행 계층 설계·결속 규칙·종료 코드 |

## 확정 3개 시나리오

- **SC-VDE-005** S-421 노선 계획 교환
- **SC-VDE-006** MASS-ROC 원격 조종
- **SC-VDE-001** X.509 인증서 송수신 (normal / forged)
