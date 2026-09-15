# Tools

개발과 분석에만 쓰는 보조 도구를 둔다. 실제 송수신 실행 파일은 `apps/`에 둔다.

| 도구 | 용도 |
| --- | --- |
| `run_stage0_tests.py` | `TC-ARCH-001..027`을 실행하고 `results/runs/<run-id>/`에 증거를 생성한다 |
| `run_stage1_link_tests.py` | `TC-LINK-001..034`와 Stage 0 회귀를 실행한다. formal 실행 전 `uv sync --frozen --extra crypto`가 필요하다 |
| `run_stage2_phy11_tests.py` | `TC-PHY11-001..016`과 Stage 0·1 전체 회귀를 실행한다. formal 실행 전 `uv sync --frozen --extra crypto`가 필요하다 |
| `generate_phy11_vectors.py` | production PHY를 import하지 않고 Link ID 11 독립 기준 벡터를 생성한다 |
