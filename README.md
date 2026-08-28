# voyager_3

Whitehat School 4기 팀 프로젝트 — 자율운항선박(MASS)↔원격운영센터(ROC) 무선통신
보안성 검토·암호통신. 이 저장소의 `feat/mass-roc-app` 브랜치는 **MASS/ROC 응용 계층 +
Presentation Interface**를 담고, 링크·PHY 계층은 `VDETER/`(팀원 담당)가 맡는다.

## 빠른 시작

```bash
python3 apps/scenario_runner.py          # 시나리오를 골라 실행(대화형)
python3 apps/console_server.py           # 콘솔 GUI — 브라우저에서 ROC/MASS 두 창
```

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/사용법.md](docs/사용법.md) | **동작 원리 + 상황별 사용법** (장비 유무·터미널 명령 순서) |
| [docs/인수인계.md](docs/인수인계.md) | 인수인계 — 구조·진행상황·남은 작업 |
| [docs/scenarios/scenario_list.md](docs/scenarios/scenario_list.md) | 시나리오 목록·선정 현황 |

## 확정 3개 시나리오

- **SC-VDE-001** X.509 인증서 송수신
- **SC-VDE-005** S-421 노선 계획 교환
- **SC-VDE-006** MASS-ROC 원격 조종
