# VDETER tests

| 디렉터리 | 내용 |
| --- | --- |
| `unit/` | 계층 내부 단위 시험 — link·mac·phy·radio·b210_system(coordinator·launcher·configuration·shutdown)·poc·standalone 결속 |
| `contract/` | 계층 계약·공통 model·아키텍처(`TC-ARCH-*`)·registry·Stage 8 engine/exec 계약 |
| `integration/` | 계층 연결·endpoint·B210 시스템(`test_stage7_*`)·Stage 8 실 seam·wideband 통합 |
| `hardware/` | 실물 B210 송수신 — `VDETER_B210_HARDWARE_TEST=1`로 arm할 때만 실행 |
| `vectors/` | Message ID, Link ID, 패킷, FEC, 변조 기준 벡터 |
| `support/` | 공통 fixture와 공개 DTO catalogue |
| `fixtures/` | 시험 고정 자료 |

## 실행

저장소 root에서 (pip 설치 없이):

```bash
PYTHONPATH=.:VDETER:VDETER/src python -m pytest VDETER/tests/unit VDETER/tests/contract -q
PYTHONPATH=.:VDETER:VDETER/src python -m pytest VDETER/tests/integration -q -k stage7
```

Windows는 `PYTHONPATH=<root>;<root>\VDETER;<root>\VDETER\src`. `VDETER/`에서 실행할 때는
`PYTHONPATH=src`. `pip install -e VDETER`를 했으면 PYTHONPATH가 필요 없다.

evidence를 남기는 단계별 실행기는 [`../tools/README.md`](../tools/README.md) — 예:

```bash
python VDETER/tools/run_stage7_tests.py --run-id stage7-software-<date>   # B210 시스템 software 시험
python VDETER/tools/run_stage0_tests.py                                    # TC-ARCH-001..027
```

## 규약

- 표준 `unittest.TestCase`로 작성해 외부 dependency 없이 실행한다. pytest도 수집한다.
- 시험 함수 이름에 case ID를 `test_TC_<AREA>_<NNN>_...` 형식으로 넣는다(`TC_ARCH`, `TC_LINK`,
  `TC_MAC`, `TC_PHY11`, `TC_RADIO`, `TC_STAGE5`, `TC_PAYLOAD` …). 단계별 실행기가 이 이름으로
  `TC-*`별 PASS/FAIL을 집계한다.
- `hardware/`는 장비가 없는 일반 실행에서 skip된다. 어떤 단계도 hardware 결과를 완료
  조건으로 쓰지 않는다.
- governance 시험(`docs/core/traceability.toml`·`legacy/`·과거 `results/` 의존)은 배포
  tree·clean clone에서 구조적으로 실패한다. `vdeter qualify`의 software 묶음은 이 중
  **6 node-id만** `--deselect`한다(`apps/standalone/qualify_cmd.GOVERNANCE_DESELECT`,
  목록·사유를 evidence에 기록). 그 밖의 skip·ignore는 없다.
- 일부 Stage 8 시험(Unix socket·fork 경로)은 POSIX 전용이라 Windows에서 skip된다 —
  전체 판정은 Linux(WSL 포함)에서 한다.
- `VDETER/src/vdeter/**`는 standalone 자격의 rf 영역이다. 시험 코드(`VDETER/tests/**`)는
  tooling 영역이라 바꿔도 RF 재자격은 필요 없지만 software 묶음은 다시 돈다.
