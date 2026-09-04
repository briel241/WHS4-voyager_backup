# voyager_3

Whitehat School 4기 팀 프로젝트 — 자율운항선박(MASS)↔원격운영센터(ROC) 무선통신
보안성 검토·암호통신. 이 저장소의 `integration/mass-roc-stage7` 브랜치는 **MASS/ROC
응용 계층 + Presentation Interface**를 최신 VDE-TER(Stage 7)와 통합해 담고, 링크·PHY
계층은 `VDETER/`(팀원 담당)가 맡는다.

## 환경 설정

실행 장비: Raspberry Pi 5 또는 Ubuntu PC 한 대, Ettus B210 세 대(MASS-A·MASS-B·ROC).
저장소를 받은 뒤 아래 순서로 준비한다. 설계와 결속 규칙은
[apps/standalone/README.md](apps/standalone/README.md)에 있다.

```bash
cd <repository>
./bin/vdeter setup
```

`setup`은 기계당 한 번 실행하며 다시 실행해도 기존 설정을 바꾸지 않는다. 하는 일:

- apt 패키지(gnuradio, uhd-host, python3-numpy, python3-venv) 확인·설치
- `var/venv` 가상환경 생성과 `requirements-standalone.lock` 설치
- identity(서명 키·인증서·trust store) 생성 — 이미 있으면 그대로 둔다
- 연결된 B210 목록 출력과 `hardware.toml` 초안 생성
- 설치 파일 실측(`var/installation/`)과 `~/.local/bin/vdeter` 명령 설치

이후 `hardware.toml`을 편집한다. 역할별 B210 serial, MASS-B 외부 전원 자산, 배선
프로파일을 기입하고 배선을 확인한 뒤 `wiring.approved = true`로 바꾼다. 역할 배정과
배선 승인은 자동으로 하지 않는다.

## 실행

```bash
vdeter qualify --yes          # 새 환경에서 한 번 — RF 자격 사다리(수 시간)
vdeter run                    # 대화형 메뉴
vdeter run all                # 전체 시나리오 (SC-VDE-005 → 006 → 001 normal → 001 forged)
vdeter results                # run 목록과 요약
```

- 새 기계, 새 설치본, 새 identity, 라이브러리 버전 변경은 모두 UNQUALIFIED로
  시작하며 `vdeter qualify`를 통과해야 `vdeter run`이 가능하다.
- 자격은 외부 공인 인증이 아닌 프로젝트 내부 RESEARCH_READY 자격이다.
- amd64 Ubuntu 지원 코드는 구현돼 있으나 실제 amd64 환경 시험은 장비 부재로
  유보됐다.
- `--no-progress`를 붙이면 진행바 대신 줄 단위 상태만 출력한다.

응용 계층만 따로 다룰 때:

```bash
python apps/scenario_runner.py           # 시나리오를 골라 실행(대화형)
python apps/console_server.py            # 콘솔 GUI — 브라우저에서 ROC/MASS 두 창
python -m pytest tests -q                # 응용 계층 전체 시험
```

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/사용법.md](docs/사용법.md) | **동작 원리 + 상황별 사용법** (장비 유무·터미널 명령 순서) |
| [docs/인수인계.md](docs/인수인계.md) | 인수인계 — 구조·진행상황·남은 작업 |
| [docs/scenarios/scenario_list.md](docs/scenarios/scenario_list.md) | 시나리오 목록·선정 현황 |

## 확정 3개 시나리오

- **SC-VDE-005** S-421 노선 계획 교환 
- **SC-VDE-006** MASS-ROC 원격 조종 
- **SC-VDE-001** X.509 인증서 송수신 
