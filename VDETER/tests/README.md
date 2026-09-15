# Tests

- `unit/`: 계층 내부 단위 시험
- `contract/`: 계층 계약과 공통 model 시험 (`TC-ARCH-*`)
- `integration/`: 계층 연결과 endpoint 시험
- `hardware/`: 유선 B210 송수신 시험
- `vectors/`: Message ID, Link ID, 패킷, FEC, 변조 기준 벡터
- `support/`: 공통 fixture와 공개 DTO catalogue

## 실행

```text
python tools/run_stage0_tests.py            # TC-ARCH-001..027 + results/runs 증거
python -m unittest discover -s tests -t .   # 시험만 실행
```

표준 `unittest`로 작성해 외부 dependency 없이 실행한다. `unittest.TestCase` 기반이므로
pytest가 설치된 환경에서는 `pytest tests`로도 수집된다.

시험 함수 이름에는 대응하는 시험 case ID를 `test_TC_ARCH_005_...` 형식으로 포함한다.
`tools/run_stage0_tests.py`가 이 이름으로 `TC-*`별 PASS/FAIL을 집계한다.

`hardware/` 시험은 장비가 없는 일반 실행에서 제외한다. 단계 0의 필수 시험은 모두 장비
없이 실행하는 architecture·contract 시험이며, hardware 결과를 단계 0 완료 조건으로
사용하지 않는다.
