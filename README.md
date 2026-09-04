# voyager_3

Whitehat School 4기 팀 프로젝트 — 자율운항선박(MASS)↔원격운영센터(ROC) 무선통신
보안성 검토·암호통신. 이 저장소의 `integration/mass-roc-stage7` 브랜치는 **MASS/ROC
응용 계층 + Presentation Interface**를 최신 VDE-TER(Stage 7)와 통합해 담고, 링크·PHY
계층은 `VDETER/`(팀원 담당)가 맡는다.

## 빠른 시작

```bash
python apps/scenario_runner.py           # 시나리오를 골라 실행(대화형)
python apps/console_server.py            # 콘솔 GUI — 브라우저에서 ROC/MASS 두 창
python -m pytest tests -q                # 응용 계층 전체 시험
```

## Standalone 실행 (Raspberry Pi / Ubuntu, B210 3대)

새 Ubuntu 기계에서 다음 순서만으로 독립 실행한다. 자세한 설계·결속 규칙은
[apps/standalone/README.md](apps/standalone/README.md).

```bash
git clone <repository>
cd <repository>
./bin/vdeter setup            # 기계당 1회 (apt·venv·identity·hardware.toml 초안·
                              #  installation manifest·~/.local/bin/vdeter 설치)
# hardware.toml 편집: 역할↔B210 serial, MASS-B 전원, 배선 확인 후 approved=true
vdeter qualify --yes          # 새 환경 1회 RF 자격 (수 시간)
vdeter run                    # 대화형 메뉴
vdeter run all                # 비대화형 전체 시나리오
vdeter results
```

- `setup` 이후에는 **Git이 필요 없다**. 대상 기계의 setup·qualify·run·results는
  git 명령을 실행하지 않고 기준선 Markdown도 읽지 않는다. `.git`이 없는
  **tar/USB 복사본**에서도 `./bin/vdeter setup` 뒤 동일하게 동작한다.
- 결속은 `var/installation/` 설치 manifest(rf·operation·tooling 영역 digest)와
  reference v2로 성립한다. 첫 번째 명령이 `./bin/vdeter`인 이유: 최상위
  `vdeter` 파일은 `VDETER/` 디렉터리와 대소문자 무시 파일시스템(Windows 개발
  checkout)에서 충돌하므로 `bin/`에 둔다. setup이 `~/.local/bin/vdeter`를
  설치하면 그 뒤는 한 단어 `vdeter`로 쓴다.
- 일반 실행(`qualify`·`run`·`results`)은 오프라인이다. 인터넷은 `setup`만 쓴다.
- 새 환경(새 기계·새 설치본·새 identity·라이브러리 변경)은 UNQUALIFIED로
  시작하며 `vdeter qualify` 1회가 필요하다. 현재 Pi(voyager3)의 기존
  qualification(reference `aa72badc…`)은 그대로 보존되며 이 변경으로 갱신하지
  않는다.
- amd64 Ubuntu(generic-pc) 지원 코드는 구현돼 있으나 실제 amd64 환경 시험은
  장비 부재로 유보됐다.
- 자격은 외부 공인 인증이 아닌 프로젝트 내부 RESEARCH_READY 자격이다.
- 진행 표시: setup·qualify·run bring-up은 20칸 진행바(TTY) 또는 UTC 시각이
  붙은 줄 로그(nohup/redirect)로 표시한다. `--no-progress`로 진행바를 끈다.
  READY 이후 시나리오 구간은 퍼센트 없이 송수신·판정 데이터를 줄 단위로 낸다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/사용법.md](docs/사용법.md) | **동작 원리 + 상황별 사용법** (장비 유무·터미널 명령 순서) |
| [docs/인수인계.md](docs/인수인계.md) | 인수인계 — 구조·진행상황·남은 작업 |
| [docs/scenarios/scenario_list.md](docs/scenarios/scenario_list.md) | 시나리오 목록·선정 현황 |

## 확정 3개 시나리오

- **SC-VDE-005** S-421 노선 계획 교환 — 소프트웨어 무선 종단 완료
- **SC-VDE-006** MASS-ROC 원격 조종 — 소프트웨어 무선 종단 완료
- **SC-VDE-001** X.509 인증서 송수신 — 소프트웨어 무선 종단 완료
