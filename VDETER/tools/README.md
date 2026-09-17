# Tools

개발 환경 전용 보조 도구. 실제 송수신 프로세스는 `apps/`에, 대상 기계의 실행 경로는
`apps/standalone/`(`vdeter` CLI)에 있다. 대상 기계의 setup·qualify·run은 이 디렉터리의
도구를 호출하지 않는다(단, `stage7_rr_final_builder.py`·`stage7_frozen_hash_audit.py`·
`stage7_issue_qualification_reference.py`는 qualify가 **import**해 재사용한다).

실행은 `VDETER/`에서 `python tools/<도구>.py`. 각 도구는 자기 위치에서 `src/`를
`sys.path`에 넣으므로 PYTHONPATH가 필요 없다. 인자는 `--help`.

## standalone 배포·결속 (개발 PC → 대상 기계)

| 도구 | 용도 |
| --- | --- |
| `make_standalone_binding.py` | `configs/b210_system/standalone-binding.json` 생성 — git head·dirty 여부·기준선 SHA·frozen 14파일 SHA. dirty 트리 거부. 커밋하지 않는다 |
| `deploy_standalone_to_pi.py` | 지정 commit의 추적 파일(`git archive`) + binding + `deploy-package-manifest.json`을 SSH/SCP 1회로 전송·원격 대조. `--commit <sha> --host <user@host> --remote-root <dir> [--dry-run] [--update] [--out-dir]` |
| `stage7_frozen_hash_audit.py` | frozen 14파일의 현재 트리 내용을 기준 ref blob과 대조(LF 정규화). `--base <ref> --out <json>` |
| `stage7_rr_final_builder.py` | RESEARCH_READY_MINIMAL final result builder — RR-001~005·007 판정, verification bundle 결속. `--evidence-root <dir>… --out <json> --software-ok --frozen-hash-ok --verification-root <dir>` |
| `stage7_issue_qualification_reference.py` | host/v1 경로의 qualification reference·Phase G profile 발급(SSH·git 결합). standalone qualify는 스키마·조립 규칙만 import |

## B210 시스템 (host 경로 · 재현·감사·계측용)

| 도구 | 용도 |
| --- | --- |
| `run_stage7_tests.py` | B210 시스템 software 시험 실행 + evidence bundle. `--run-id stage7-software-<date>` |
| `run_stage7_p0_preflight.py` | Pi RF-off preflight(P0). `--run-id <id> --cycles N --output <json>` |
| `prepare_stage7_run_configs.py` | run별 resolved config 준비(host prepare, v1 경로). `--run-id --deploy-run-id --host --external-power-asset-mass-b --run-mode` |
| `provision_stage7_trust.py` | TEST TBB signer/trust 자료 provisioning. `--force` |
| `deploy_stage7_to_pi.py` | Pi 배포(v1 host 경로; standalone은 위 `deploy_standalone_to_pi.py`). `--host --run-id [--dry-run]` |
| `stage7_service_debug.py` | RF qualification을 건너뛰고 서비스 기동 구간만 반복 계측하는 디버그 하네스. RR evidence 미생성 |
| `stage7_iq_replay.py` | rr-p1 IQ 덤프 오프라인 분석(`profile` / `align` / `decode`). 계측 전용, 판정에 쓰지 않음 |

## 단계별 시험 실행기·evidence

| 도구 | 용도 |
| --- | --- |
| `run_stage0_tests.py` | `TC-ARCH-001..027` 실행, `results/runs/<run-id>/` 증거 |
| `run_stage1_link_tests.py` | `TC-LINK-*` + Stage 0 회귀 |
| `run_stage2_phy11_tests.py` | `TC-PHY11-*` + Stage 0·1 회귀 |
| `run_stage3_radio_tests.py` | Stage 3 software 시험, 부분 evidence |
| `run_stage3_b210_no_tx_preflight.py` | 승인 apt stack + B210 configure/readback 검증, RF 송신 없음 |
| `run_stage3_b210_hardware_tests.py` | 명시적으로 arm한 실물 B210 gate(`VDETER_B210_HARDWARE_TEST=1`, ship/control serial env) |
| `finalize_stage3_radio_evidence.py` | Stage 3 Radio 최종 evidence 조립·독립 감사 |
| `run_stage4_mac_tests.py` | `TC-MAC-001..018` |
| `stage4_smoke.py` | 3-endpoint smoke — 그룹 구성 후 TBB broadcast/acquisition |
| `run_stage5_tests.py` | `TC-STAGE5-001..020` |
| `run_phy_profile_tests.py` | Stage 6 Link 17·19 PHY profile 검증 |
| `run_phy_awgn_tests.py` | Link 17·19 AWGN characterization·noise-only 병렬 실행 |
| `run_resampler_gate_tests.py` | 변환 경계 gate(`TC-PHY-MULTI-011` 입력) |

## 운용 readiness 시간 최적화 판정 (RF-off, 2026-09-17)

| 도구 | 용도 |
| --- | --- |
| `readiness_host_time_replay.py` | OPT-01: 저장 runtime evidence의 host-time accepted series를 baseline(`RESTART_SERIES_V1`)·후보(`REJECT_BRACKET_KEEP_SERIES_V1`) 규칙에 replay + synthetic adversarial(wide bracket·역행·regime change) 대조. 출력 JSON 1개, 장치 없음 |
| `readiness_rf_time_occupancy.py` | OPT-02: C1~C6 RF-time 경로의 정적 점유 matrix와 순차 처리기 충돌 모델로 부분 병렬 활성화 여부를 판정(현재 `DISABLED`). 출력 JSON 1개 |

## 벡터·manifest 생성

| 도구 | 용도 |
| --- | --- |
| `generate_phy11_vectors.py` | production PHY를 import하지 않는 Link ID 11 독립 기준 벡터 |
| `generate_phy17_19_vectors.py` | Link ID 17·19 독립 기준 벡터 v2 (`pip install -e ".[vectors]"` — mpmath) |
| `capture_stage3_b210_ubuntu_apt_manifest.py` | 승인 Ubuntu apt SDR stack manifest 채집(B210 open 없음) |
| `print_stage3_b210_fingerprints.py` | Stage 3 B210 radio fingerprint 2종 출력(RF open 없음) |
