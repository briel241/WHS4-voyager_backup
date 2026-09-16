# VDETER Standalone — 실행 계층 설계·계약

`apps/standalone/`(`vdeter` CLI)의 동작 계약을 적는다. 코드 계약과 어긋나는 서술이
있으면 코드가 우선한다. 운용자는 §0~§2·§10~§11, 구현·감사자는 전체를 본다.
Stage 8 PoC 실행(`--poc`)·capability 증언은 각각 `poc_run.py`·`capability_issue_cmd.py`
docstring과 `docs/` 명세가 권위다.

---

## 0. Quick Start

```
git clone <repository>
cd <repository>
./bin/vdeter setup            # 기계당 1회, 멱등
# hardware.toml 편집 · wiring.approved = true
vdeter qualify --yes          # software 묶음 → RF 사다리 → reference 발급
vdeter run all
vdeter results
```

- `setup` 이후 Git 불필요: setup·qualify·run·results는 `git`을 호출하지 않고
  `.git`·`standalone-binding.json`·기준선 Markdown을 읽지 않는다. `.git` 없는
  tar/USB 복사본도 동일하게 동작한다. 일반 실행은 오프라인이다.
- `bin/vdeter`는 venv가 없으면 `setup`만 시스템 python3로 허용하고, 있으면
  `var/venv/bin/python3`로 CLI를 exec한다. setup이 `~/.local/bin/vdeter` 사용자
  launcher를 설치한다(멱등, 다른 프로그램 덮어쓰기 없음). 최상위 `vdeter` 파일이
  아닌 이유는 `VDETER/` 디렉터리와 대소문자 무시 파일시스템 충돌 때문이다.
- 자격은 외부 공인 인증이 아닌 RESEARCH_READY 내부 자격. amd64 지원 코드는
  구현됐지만 실제 amd64 시험은 유보.

**결속 요약(v2)** — `setup`이 설치 파일을 실측해 `var/installation/`에 네 manifest
(`installation-manifest.json`, `rf-manifest.json`, `operation-manifest.json`,
`tooling-manifest.json`; canonical JSON, 파일별 SHA-256, 영역별 SHA-256, atomic
write, `var/` 제외)를 만든다. 영역 분류는 `qualification_domains.py` 한곳이다:
**rf**(`VDETER/src/vdeter/**`·coordinator·launcher·RF config·RF phase 인자/run
mode 생성 코드) / **operation**(run_cmd·local_session·local_config·local_preflight·
identity·MASS/ROC 응용·scenario controller) / **tooling**(setup·qualify 비RF·
verification bundle·final builder·reference 발급/검증·자격 시험 코드) /
**non_binding**(Markdown·보고서·이미지). 미분류 실행 파일은 operation, 교차영향
파일은 여러 영역에 중복 결속. reference **v2**는 installation/rf/operation/tooling
digest·파일별 SHA map·라이브러리 버전·B210 serial과 역할·USB speed_class·배선
승인·gain artifact·identity fingerprint·resolved config SHA·TC 결과·evidence SHA·
qualification method·limitations를 결속한다. `provenance.git`은 항상 null이다.
`baseline_sha256`는 커밋 상수 pin(`provenance.STAGE7_BASELINE_PIN`)이다.

**진행 표시** — `progress.py`(표준 라이브러리). TTY: 20칸 바를 같은 줄에서 갱신,
단계 전환 시 확정. non-TTY/nohup/redirect: CR·ANSI 없는 UTC 줄 로그, 상태 변화·30초
heartbeat. 각 줄의 퍼센트는 그 단계 자체의 진행률, 전체는 `overall=NN%`. 의미 세 가지:
exact / estimated(퍼센트 뒤 `estimated`) / waiting(퍼센트 정지). `--no-progress`로
줄 단위 상태만 출력. READY 이후 시나리오 구간은 저 I/O 경로 — `running SC-…` 한 줄,
workload trace 행, 종료 시 `result: PASS/FAIL · 소요 · quiescence` 요약만 낸다.
상세는 종료 후 evidence(`workloads/<id>/`, `evidence/service_events/`)와
`vdeter results`. CLI 출력·오류 메시지·hardware.toml 초안 주석은 영어, 소스 주석·
이 README는 한국어. coordinator·launcher가 직접 내는 콘솔 원문은 그대로다.

## 1. 개요와 명령 생애주기

목표: **코드 + 장비(B210 3대)만으로** 임의의 native Ubuntu(arm64/amd64)에서
MASS-A/MASS-B/ROC 3-endpoint Stage 7 시스템을 단독 기동·자격·운용한다.
Windows host, SSH(자기참조 포함), Git(host·대상 모두), baseline Markdown, 외부
네트워크 서비스, 실행 중 dependency 다운로드에 의존하지 않는다.

```
./bin/vdeter setup # 기계당 1회. apt(온라인 허용)+venv+lock, identity 생성,
                   # hardware.toml 초안, installation manifest, 사용자 launcher.
                   # 멱등 — 기존 identity·hardware.toml 불변경.
(사용자)           # hardware.toml 편집·승인 (역할↔serial, MASS-B 전원, 배선)
vdeter qualify     # 단독 RR 재자격 (수 시간, RF 송신). 전체 PASS 시에만
                   # active reference(v2) 원자적 발급. 그 전까지 UNQUALIFIED.
vdeter run         # 일상 실행 (완전 오프라인). 메뉴 또는 비대화형 인자.
vdeter results     # 최근 run 목록·요약 재열람.
```

새 기계·의존성 버전 변경·`--new-identity` 재생성 환경은 UNQUALIFIED로
시작하며, `vdeter run`은 이를 RF 접근 전에 거부한다 (결정 7·10·11·13).

## 2. 상태 배치

설치 root = 저장소 checkout root. 코드 배포·업데이트가 삭제·덮어쓰지 않는
영속 state는 전부 아래 두 디렉터리에 있다 (`.gitignore` 등재, 결정 15).

```
<root>/var/
  qualification/
    releases/<id>/     # 자격 결과 불변 보관 (reference·final-result·verification)
    candidate/         # qualify 진행 중 scratch (승격 전)
    active.json        # 활성 release **포인터** — tmp fsync→os.replace→
                       # 디렉터리 fsync로 교체한다 (대상 Ubuntu 기준 전원
                       # 장애 내구). release 이동(fsync)→포인터 교체 순서라
                       # 어느 순간에도 "active 없음" 창이 없다. 적재는
                       # 포인터의 reference_sha256 결속으로만 한다.
                       # 이전 release가 곧 불변 이력이다.
  installation/        # setup 실측 manifest 4종 (installation/rf/operation/tooling)
  diagnostic/          # vdeter diag authorization·ledger (§7A)
  runs/<run-id>/       # run별 evidence (resolved-configs/, session/, logs/, ...)
  lock                 # 단일 인스턴스 flock 대상 (내용은 진단 정보만)
<root>/identity/
  private/             # 개인키 (0600). 어떤 산출물에도 원문·해시 불기재.
  certificates/        # 인증서 (공개)
  trust/               # trust store
  archive/             # --new-identity 시 공개 기록만 보관 (개인키는 폐기)
  identity-manifest.json
```

- 자동 run-id: `run-YYYYMMDD-HHMMSS` (UTC), 같은 초 충돌 시 `-2`, `-3` 접미사.
- 사용자 지정 run-id: 기존 디렉터리 존재 시 **시작 전 거부** (evidence 보호).
- 자동 삭제 없음. 시작 시 여유 공간 검사 — 경고 임계 미만이면 경고,
  세션 완주 불가 수준이면 시작 거부.

## 3. 프로세스 구조 (결정 6·17)

```
vdeter run                       # 로컬 실행 계층 (이 패키지)
└─ coordinator_main.py    # 무수정. 명시적 argv로 Popen (shell 문자열 금지)
   ├─ MASS-A endpoint launcher   # coordinator가 spawn (기존 그대로)
   ├─ MASS-B endpoint launcher
   ├─ ROC endpoint launcher
   └─ workload child (mass_roc_workload_controller.py)
```

로컬 실행 계층의 책임 (구현 계약):

1. 충돌하지 않는 새 session/run 디렉터리 생성 (§2 규칙).
2. coordinator를 shell 문자열이 아닌 **명시적 argv**로 `subprocess.Popen`.
3. 필요한 환경변수·설정 경로를 명시적으로 전달 (ambient 상태 상속 금지).
4. READY(`session/ready.json`)/FAILED/`done.rc`를 **bounded timeout**으로 감시.
5. request 파일은 임시 파일 작성 후 **atomic rename**으로 발행.
6. result 파일은 완성된 뒤에만 읽는다 (임시명→rename 규약 + JSON 파싱 성공 기준).
7. shutdown 마커 발행으로 종료 요청.
8. 정상 종료를 bounded 대기하고, 필요할 때만 bounded terminate→kill.
9. coordinator exit code와 실패 원인을 사용자에게 그대로 전달.
10. Ctrl+C에서도 shutdown→cleanup→B210 반환 순서 유지 (§10).

구현은 `subprocess`·`pathlib` 등 표준 라이브러리로 Ubuntu arm64/amd64에서
동일 경로로 동작한다. SSH 호출 0회. systemd 없음(향후 wrapper 옵션만).
endpoint별 재시작 없음 — 어떤 fault든 세션 전체 fail-closed.

동시 실행 보호: `var/lock`에 대한 **OS file lock(`fcntl.flock`)**. lock 판정에
PID를 쓰지 않는다(PID·run-id는 진단 기록만). flock은 프로세스 종료 시 OS가
자동 해제하므로 stale lock 문제가 없다.

## 4. 설정 계층과 hardware.toml

4계층 + identity (결정 7). resolved config 조립 규칙 (결정 14):

```
resolved = 배포 template(②, read-only)
         + hardware.toml(①)의 provisioning 필드 치환   # REQUIRED_UNSET 계열만
         + 자격 산물(③)의 주입                        # gain, 결속 SHA들
→ 기존 ConfigurationLoader로 검증 (무수정 재사용) → 실패 시 RF 전 중단
```

hardware.toml은 **사용자가 승인한 환경 의도만** 담는다. USB 포트 경로·실측
속도처럼 기계마다 달라지는 관측값은 선언이 아니라 자격 산물(③)에 기록한다.

```toml
# hardware.toml 스키마 (템플릿: templates/hardware.toml.example)
schema_version = 1
platform_profile = "pi"            # "pi" | "generic-pc" (§9 검사 프로파일 선택)

[endpoints.MASS-A]
serial = "34D03DD"                 # 사용자 입력 — 이 serial의 B210이 MASS-A

[endpoints.MASS-B]
serial = "3515425"
external_power_asset = "AT2024T"   # 사용자만 아는 물리 자산 ID (probe 불가)

[endpoints.ROC]
serial = "34D03E8"

[wiring]
profile = "C1-C6-STANDARD"         # 승인된 배선 profile 식별자
approved = true                    # "현재 배선이 선언대로"를 사용자가 승인
```

- `vdeter setup`이 장비 탐지 결과로 **초안**을 만들 수 있으나, `approved =
  true`는 사용자만 기록한다. 초안 자동 승인 금지 (결정 1·7).
- (2026-09-15 HW-01) 초안 생성은 `apps/standalone/hardware_draft.py` 한곳이다.
  기대 프로필(기본 실험실 프로필 `LAB_DEFAULT`: MASS-A 34D03DD / MASS-B 3515425 /
  ROC 34D03E8, hub `HUB-VIA-2109-0817-01` `2109:0817`, wiring `C1-C6-STANDARD`)을
  관측과 대조해, **관측된 기대 serial만** 역할별 기대 매핑으로 채우고 hub asset은
  VID:PID 일치 + 세 serial 관측일 때만 기대값으로 채운다. 누락·불일치는 REQUIRED_FILL
  + 사유(초안 주석·`var/setup-usb-topology.json`·setup 출력)로 남기고 다른 장비를
  배정하지 않는다. 채워진 값은 기대값이며 배선·전원 확인이 아니다 — `approved`와
  전원 증언은 초안이 쓰지 않고(`*_source` 줄은 주석 처리), 기존 파일은 손대지 않는다.
- station identity·주파수·sample-rate·channel·판정 기준은 ②에 고정 — ①로
  변경 불가(치환 대상 필드가 아님). ② 변경은 manifest 검증이 fail-closed.
- (2026-09-10 additive) `schema_version = 2`는 Pi powered-USB3-hub host
  profile 전용이다 — §15. schema 1은 위 legacy Pi profile 그대로다.

## 5. standalone-binding (legacy — v2에서 필수 입력 아님)

`VDETER/configs/b210_system/standalone-binding.json`. 대상 기계의 setup·qualify·run은
이 파일을 **읽지 않는다** — 결속은 §0의 installation manifest로 대체됐다. 파일이 남아
있어도 참고 provenance로만 기록되고 설치 스캔에서 제외되며, Git에 커밋하지 않는다
(커밋하면 HEAD가 바뀌어 `source_head`와 어긋난다).

생성 주체는 개발 PC 패키징 도구 `VDETER/tools/make_standalone_binding.py`(git 필요,
dirty 트리 거부)다. 내용: `artifact_type=STAGE7_STANDALONE_BINDING_V1`, `source_head`
(배포물이 잘린 commit), `source_worktree_dirty=false`, `baseline_version`/`baseline_sha256`
(기준선 Markdown 실측), `frozen_files`(`stage7_frozen_hash_audit.FROZEN_FILES` 14개의
정규화(LF) 내용 SHA-256).

**배포 패키지**: `VDETER/tools/deploy_standalone_to_pi.py --commit <sha> --host <user@host>
--remote-root <dir>`가 지정 commit의 추적 파일(`git archive`) + 생성된 binding +
자기 기술 manifest(`deploy-package-manifest.json`)를 SSH/SCP **1회**로 전송하고 원격에서
전수 SHA·binding head를 대조한다. results/·var/·identity/·hardware.toml·.git 제외.
전송 뒤 setup·qualify·run은 host 없이 동작한다.

## 6. git_state.json 호환 생성 (승인 조건 1)

coordinator `_load_run_metadata`/`validate_rr_binding`은 configs-dir의
`git_state.json`을 읽는다. **파일명·스키마를 유지**하되 내용은 Git·Markdown
없이 구성한다. 필드별 출처 (v2 현행; "기존" 열은 host prepare 도구의 v1
이력이다):

| 필드 | 기존(host prepare, v1) 출처 | standalone v2 출처 |
|---|---|---|
| `run_id` | 요청 run-id | 동일 (현재 run) |
| `qualification_profile` | `"RESEARCH_READY_MINIMAL"` | 동일 상수 |
| `rr_phase` | profile 지정 | run: `"rr-p2-17"` (서비스 workload 결속 규약 유지) / qualify: 해당 RR phase |
| `data_link_id` | phase 말미 17/19 | 동일 규칙 |
| `baseline_version` / `baseline_sha256` | host Markdown 재해시 | **커밋 상수 pin `provenance.STAGE7_BASELINE_PIN`** (문서를 읽지 않음; RR evidence 계약용 참고값) |
| `git_commit` / `git_branch` / `git_dirty` | host `git rev-parse`·`git status` | **항상 null** — 실행 환경에서 git 명령도 `.git` 읽기도 하지 않으며 coordinator는 대조하지 않는다 |
| 무결성 결속 (additive) | 없음 | `installation_sha256`·`rf/operation/tooling_manifest_sha256`·`installation_verified` (= `var/installation/` manifest 재측정 통과) + 별도 `installation-state.json`; 미통과면 config 생성 자체를 거부 |
| `environment_manifest_sha256` | SSH probe | 로컬 환경 probe 실측 |
| `hardware` | SSH probe usb_devices | 로컬 sysfs 실측 |
| `implicit_retry` | `0` | 동일 상수 (coordinator가 0 강제) |
| `phase_deadlines_s` | prepare 하드코드 | 동일 값 — coordinator `RR_PHASE_DEADLINE_S` 정본과 일치 필수: rr-p0=900, rr-p1=3600, rr-p2-17/19=4200, rr-p3-fault=2400 (rr-p1-gain=1200, soak=600은 비판정 기록용) |

coordinator 검증 계약 (실측, 무수정 준수 대상):

- `mass-roc-session`: profile=RESEARCH_READY_MINIMAL, `rr_phase ∈ {rr-p2-17,
  rr-p2-19}`, run_id 일치, `--baseline-sha256` 인자와 일치, implicit_retry=0,
  디버그 env(`STAGE7_SVCDBG_ALLOW_RX_READY`·`STAGE7_SCENARIO_FILTER`) 금지.
- RR phase: 위에 더해 `rr_phase == phase`, p2는 link 결속(17/19),
  `phase_deadlines_s[phase] == 실행기 정본` 대조.

`--baseline-sha256` 인자에는 `STAGE7_BASELINE_PIN["sha256"]`을 넘긴다.
run·qualify 공통 config 생성기(`local_config.py`)가 이 표를 구현한다.
fixtures(start-*/submit-*/stop)는 기존 prepare 도구와 동일 규칙·동일
canonical JSON으로 로컬 생성한다.

## 7. vdeter qualify — RR 사다리와 reference 발급 (결정 20, 승인 조건 2·3)

사다리 (2026-09-16 묶음 선행으로 개정):

```
0. verification bundle         # RF 앞. 실패하면 RF phase 0건·승격 0건
1. RR-P0                      → TC-STAGE7-RR-001
2. RR-P1 (+gain 측정)         → TC-STAGE7-RR-002·003·004 + gain artifact
3. candidate reference 생성    # 이후 단계·향후 run이 쓸 결속값 확정
4. RR-P2-17                   → TC-STAGE7-RR-005
5. RR-P3-fault                → TC-STAGE7-RR-007 (판정 필수)
6. 0단계 묶음 재확인 → final builder 검증   # 묶음을 다시 돌리지 않는다
7. 전체 PASS → candidate를 releases/<id>로 이동 후 active 포인터 원자 교체
```

**묶음 선행·같은 실행 재사용**: software 묶음은 한 실행에 정확히 1회다. 묶음 옆
`verification/bundle-binding.json`에 `ladder_id`·installation digest·영역별 digest·
환경 manifest digest·python 실행파일·시험 선택 조건(`UNIT_CONTRACT_TARGETS`·
`INTEGRATION_TARGETS`·`GOVERNANCE_DESELECT`)·묶음 판정·산출물 4개의 SHA-256을
남기고, **RF 진입 직전**과 **승격 직전**에 설치 manifest를 다시 적재·재측정해
대조한다. 하나라도 어긋나면 묶음을 조용히 다시 돌리지 않고 fail-closed로 끝낸다
(RF 미시작 또는 미승격, 기존 active 유지). 다른 run·과거 자격·수동 사전 점검의
묶음은 `ladder_id` 불일치로 거부되고, 손상·부분 기록·산출물 누락도 PASS로 해석하지
않는다. 성공 경로의 RF 시간은 줄지 않는다.

**verification bundle**(대상 기계에서 생성, final builder `--verification-root` 입력):

- `frozen-hash-audit.json` — v2 installation audit(`installation_sha256`·
  `installation_verified`·rf 영역 파일별 대조). builder는 v1(`head_commit` ↔ git HEAD)과
  v2(`installation_sha256` ↔ 현행 digest)를 필드명으로 구분해 병행 지원한다(40-hex ≠
  64-hex). 하나라도 불일치·검증 불능이면 fail-closed.
- `unit-contract.log` — `VDETER/tests/unit`+`contract` 완주 로그. governance 시험
  6 node-id는 `GOVERNANCE_DESELECT`로 명시 deselect(목록·사유를 evidence에 기록).
- `integration-stage7.log` — `VDETER/tests/integration -k stage7` 완주 로그.

**phase별 run_mode 고정**(`qualify_cmd.PHASE_RUN_MODE`): rr-p0 = RF_OFF_QUALIFICATION,
rr-p1 = GAIN_CALIBRATION(gain 입력 없음, 새 gain artifact 산출), rr-p2-17·rr-p3-fault =
OPERATIONAL(rr-p1 산출 gain 결속). endpoint runtime은 GAIN_CALIBRATION에서만 gain
탐색을 수락한다. final builder 판정 필수 TC = RR-001~005 + RR-007
(`stage7_rr_final_builder.REQUIRED_TCS`). RR-006은 DEFERRED(기록 전용), RR-008 soak은
범위 제외. 어느 단계든 실패 시 candidate 미승격, 기존 active reference 유지. gain
재측정 결과는 기록·비교만 한다(자동 해소로 간주하지 않음).

**fail-closed 보강**:

- 각 phase는 독립 process group으로 실행한다. timeout·중단 시 group 전체를 TERM→KILL로
  정리하고 group 소멸을 bounded 재확인한다. leader가 rc=0으로 끝나도 손자가 남으면
  `cleanup_anomaly`로 기록하고 강제 정리한다 — **정리에 성공해도 anomaly가 있으면 그
  phase는 자격 실패**다. verification pytest 실행에도 timeout이 있다.
- GNU Radio·UHD·numpy·cryptography 버전 4종이 비어 있지 않은 문자열로 실측되지 않으면
  P0 시작 전에 거부한다.

**범위 제외(정책, `limitations`에 명시)**:

- **Link 19**: RR-P2-19(TC-RR-006)는 사다리에서 제거. 운용 시나리오 3종은 Link 11/17만
  쓴다. Link 19 코드는 삭제·수정하지 않는다(`qualify_cmd.LINK19_LIMITATION`).
- **RR-P3-fault 물리 USB detach**: TC-RR-007은 소프트웨어 장애 소항목 6개
  (stale-generation-command, worker-kill, coordinator-ipc-loss,
  restart-generation-fencing, scenario-controller-ipc-loss, stale-callback)로만
  판정한다 — 전부 PASS면 PASS. B210 hot-unplug·재열거 복구는 미보증
  (`qualify_cmd.USB_DETACH_LIMITATION`). 판정 완화가 아니라 범위 제외다.

**reference 발급**: `stage7_issue_qualification_reference.py` CLI는 SSH·Git 결합이라
직접 호출하지 않고 import 폐포 계산·critical manifest 조립·reference 스키마
(`STAGE7_QUALIFICATION_REFERENCE` schema 1)·atomic publish·발급 직후 `run_profile`
재검증만 재사용한다. 기준은 로컬 설치 tree 실측 단일 기준 + installation manifest
검증이다. final builder에는 현행 installation digest를 돌려주는 `head_provider`를
주입한다(계약 시험 `VDETER/tests/unit/test_stage7_rr_final_builder_installation_audit.py`).

**evidence_reuse 발급**(`qualify_cmd.run_qualify_reuse`, CLI 옵션 아님): rf 영역 파일과
RF-critical symbol(사다리 순서·run mode·phase 인자·gain 소비 phase·`default_phase_runner`)
이 함수 단위 byte-identical이고 operation 영역도 byte-identical일 때만 이전 release의 RF
evidence를 재사용해 `qualification_method=evidence_reuse` reference를 발급한다. 하나라도
다르면 거부한다. `vdeter qualify` CLI는 항상 전체 사다리(`run_qualify`)다.

## 7A. vdeter diag — 개발용 통제 RF 진단 (2026-09-14, 실행 명세 §5A)

미자격 후보에서 승인된 범위의 소규모 실물 진단을 수행하는 별도 경로. **qualification이 아니며**
reference를 발급·승격하지 않고, §7·§8의 정상 run 거부 규칙을 바꾸지 않는다.

```
vdeter diag authorize --calibration-reference <reference.json> --plan <plan.json>
vdeter diag run [SCENARIO ...] [--hold-after-ready S] [--poc PATH|--poc-dir DIR] [--rf-off-software]
vdeter diag status
```

- `authorize`(RF-off, 장치 open 없음): 배선 승인 → installation 적재·재측정 → identity → 필수
  preflight(sysfs·키) → 출처 reference 적재(sidecar·TC PASS) → 후보 결속 블록 조립(§8과 같은 항목)
  → **교정 적합성**(출처와 후보의 platform/host profile·배선 profile·endpoints·hub topology·
  라이브러리 4종·RF-critical 심볼 동일; 아니면 발급 거부 + 필요한 교정 보고) → 정상 run과 같은
  블록 검사로 자기 검증 → `var/diagnostic/authorization.json` + `.sha256`. `var/qualification`에는
  쓰지 않는다. plan: `label, allowed_scenarios, poc_allowed, max_runs, max_workloads_per_run,
  expires_utc[, max_hold_after_ready_s ≤ 600][, allowed_workloads]`.
  `allowed_workloads`(선택)는 `{scenario, case, poc_id}` 객체 목록으로 **조합**을 표현한다 —
  `case`는 `normal`/`forged`(기본 `normal`), `poc_id`는 `--poc-dir` catalog에서 prompt 없이
  고를 PoC id(생략·`null`이면 그 workload는 baseline). 이것이 있어야 SC-VDE-001 `forged`와
  동일 세션 baseline↔PoC 전환을 비대화형 진단에서 지정할 수 있다. 각 `scenario`는
  `allowed_scenarios`에 있어야 하고, `poc_id`는 `poc_allowed=true`일 때만 쓸 수 있으며,
  항목 수는 `max_workloads_per_run`을 넘을 수 없다. 없으면 종전대로
  `allowed_scenarios × normal`로 해석한다(기존 계획 파일 호환). 선택·실행은 정상 run과 **같은**
  `run_cmd.execute`·`select_and_snapshot` 경로이며 별도 실행 엔진을 만들지 않는다.
- `run`: authorization(sidecar·만료·plan)과 ledger(한도)를 **장치 접근 전** 검사 → `run_cmd.execute`
  를 그대로 호출(주입: 진단 loader·블록 검사·비대화형 계획·**재시도 없음**). 인자 없이 실행하면
  RX-only 세션(bring-up → `--hold-after-ready` 동안 READY 유지 → close). run id 접두 `diag-`.
  coordinator는 빈 측정을 설계대로 fail-closed 판정하므로 RX-only 세션은 coordinator rc=1로 끝난다.
  진단 계층은 실패 사유가 빈 측정 하나뿐이고 `service_stop_rc=0`·cleanup PASS·잔존 worker 0·
  inhibit 아님·인터럽트 아님·close 후 topology 정상일 때만 이를 "기대된 빈 측정"으로 보고 진단
  종료코드를 0으로 기록한다(명세 §5A.3). coordinator의 rc·status·error 원문과 보정 사실은
  ledger의 `rx_only_assessment`·`diagnostic_exit_code_override`에 그대로 남으며, 반환값·콘솔·
  ledger는 같은 종료코드를 말한다. 사유가 하나라도 더 섞이면 FAIL 그대로다.
  배너 `DIAGNOSTIC, NOT_QUALIFIED`; run.json/summary.json은 `reference_sha256=null`,
  `qualification_status=NOT_QUALIFIED`, `diagnostic_authorization_sha256`; `RESULT … qualification=NOT_QUALIFIED`.
  종료 후 `active.json`·`releases/` 불변을 확인해 ledger에 기록한다.
- 한도: run 수·run당 workload·시나리오 집합·PoC 허용·READY 유지·만료는 CLI가 강제(장치 접근 전).
  bring-up 2700 s·workload 900 s·close 600 s·단일 인스턴스는 기존 세션 코드가 강제(불변). burst 단위
  RF 계수는 evidence에 없다(transfer 단위 counters만) — 강제한다고 주장하지 않는다.
- 종료 코드는 §11과 같다(`4`=authorization 없음/변조/만료/stale, `2`=plan 범위·한도 초과).

## 7B. 진단 스위치(`STAGE7_DIAG`)가 바꾸는 것 (2026-09-16, 3-1 — 제한적 동작 변경)

이름과 켜는 방법은 그대로다. 바뀐 것은 **무엇이 이 스위치를 따르는가**이다. 이전에는 세 갈래였다
— env 소비자만 스위치를 따르고, coordinator trace 는 항상 상세, workload trace 는 항상 기본.
지금은 한 갈래다.

### 7B.1 우선순위와 단일 출처

1. `vdeter run --diagnostics` 가 **유일한 출처**다. 별도 `--no-diagnostics` 는 없다(생략 = off).
2. `local_session.build_child_env` 가 자식 env 를 만든다 — 상위 환경의 `STAGE7_*` 는 **전부 제거**되므로
   ambient `STAGE7_DIAG=1` 만으로는 켜지지 않는다(정식 run 오염 방지, 종전 동작).
3. coordinator·launcher·workload controller 는 그 env 하나만 읽는다. 새 설정 계층·새 옵션은 없다.
   coordinator 는 자식 workload controller 에게 같은 상태를 `--diagnostics` 로 넘긴다.
4. `vdeter qualify` 의 RR 사다리는 `diagnostics` 를 넘기지 않는다 → 항상 off(종전대로).

`run.json`·`summary.json` 의 `diagnostics` 필드는 **요청값이 아니라 적용값**이다 — 자식 env 를 만드는
같은 함수에서 읽어 적는다.

### 7B.2 항상 남는 기록 (off 에서도)

| 기록 | 왜 |
| --- | --- |
| `service_events/*.jsonl` | 시나리오 판정 입력 + stall guard liveness |
| 단계 시각·`phase_elapsed_s` | RR-004 PASS 조건, `vdeter results` 단계표 |
| coordinator trace 의 판정 원인·요약 이벤트 | `session_ready`·`intents_complete`·`tbb_complete`·`workload_result`·`session_closed`, 그리고 진행바가 읽는 `rf_time_pre/post_summary` |
| `closed.json`·RR evidence·재현 환경 필드 | 자격·재현 입력 |
| lead 여유 3키·tuning readback/복원·lease 소유권 | 성공·실패 공통 계약 |
| 실패 사유·거부 코드·취소/정리 카운터·잔존 0 | 진단 여부로 달라지면 안 되는 것 |

`session-coordinator-trace.jsonl` **파일 자체는 양쪽에서 생긴다.** 파일을 켜고 끄는 것이 아니라 그
안의 선택적 이벤트만 나뉜다. guard 진행 통지도 스위치와 무관하게 계속 돈다.

### 7B.3 on 에서만 남는 기록

| 기록 | 성격 |
| --- | --- |
| coordinator trace 의 `phase_mark`·`intent_wait`·`tbb_wait` | 주기·표식 자료. 기계 소비자 0 |
| launcher `_COST` 누적과 `[diag] cost` 한 줄 요약 | 병목 해부 |
| launcher 창 추적(`_win_trace`)·제출 시각(`_submit_utc`)·큐 적재 시각(`_c_stamp`) | 소비자가 이미 on 전용이었다 — 적재를 소비 조건에 맞췄다 |
| workload trace 상세도 | 부모가 적용 상태를 자식에게 넘긴다 |

### 7B.4 바꾸지 않은 것

RF·PHY·MAC·TDMA·타이밍·임계값·시나리오 판정·재시도·RX gate·tuning 정책은 그대로다.
IQ 원파형 덤프(`evidence/iq_debug/`)는 이번에 **손대지 않았다** — 그 JSON sidecar 가
`collect_phase_evidence_sha256` 를 통해 reference 의 evidence SHA map 에 실리므로, 게이트를 걸면
자격 reference 가 기록하는 범위가 달라진다(별도 승인 사항).

**이는 제한적 동작 변경이다.** off 로 돌린 실행의 `session-coordinator-trace.jsonl` 에는 이전보다
이벤트가 적고(7B.3 의 3종), launcher 의 선택적 적재가 돌지 않는다. 자격 판정·필수 evidence·진행
통지는 불변이며, 해시가 바뀐 자격 함수는
`tests/fixtures/stage7_diagnostics_gating_function_bindings.json` 에 **고정 해시**로 재결속했다
(이름 허용목록이 아니므로 이후 추가 변경은 다시 거부된다). 비용 절감은 **측정하지 않았고**
수치로 주장하지 않는다 — 근거는 구조 일관성(적재와 소비가 같은 조건)이다.

## 7C. launcher 종료 대기 — 공통 마감시각 동시 관찰 (2026-09-16, 3-3)

같은 phase 의 launcher 종료를 endpoint 별로 **직렬** 대기하던 네 경로
(`run_rr_p0`·`run_mass_roc_service`·`run_mass_roc_session` finally·`main`
teardown)를, 하나의 monotonic 마감시각까지 **함께 관찰**하도록 바꿨다.

```
기존: MASS-A 60초 → MASS-B 60초 → ROC 60초   (최악 180초)
변경: 세 endpoint 를 함께 관찰, 공통 마감시각까지 최대 60초
```

| 유지 | 내용 |
| --- | --- |
| 상한 값 | 60 s(세 경로)·30 s(`main` teardown) 그대로. 낮추지 않았다 |
| 종료 요청 순서 | STOP barrier·start 대행 회수 순서 불변 |
| 자연 종료 판정 | `poll()` 기준 불변. 마감시각에 딱 끝난 것도 종료로 본다 |
| escalation | 마감 뒤 **남은 프로세스에만** 기존 정책 적용 — `run_mass_roc_session` 은 `_terminate_process_bounded`(TERM→KILL), `main` 은 terminate. 원래 강제 종료가 없던 두 경로에는 새로 넣지 않았다 |
| 합격 기준 | `launcher_clean_exit`·cleanup·소유권·inhibit 판정 불변. 종료되지 않은 endpoint 를 성공으로 합성하지 않는다 |

endpoint 별 종료 상태를 `launcher_exits`(자연 종료 여부·exit code·대기 시간,
강제 종료 시 `cleanup`)로 evidence 에 남긴다 — 먼저 끝난 endpoint 의 결과가
나중 것 때문에 유실되지 않는다. 관찰기는 신호를 보내지 않고 아무것도 제출하지
않는다. 새 async framework·thread pool·scheduler 는 만들지 않았고 상한 값과
마감 뒤 정책은 호출자가 명시한다(경로마다 의미가 다르다).

**시간 효과는 최악 경로에서만 주장한다.** 정상 경로(launcher 가 곧바로
종료)는 종전에도 몇 초였고 이번 변경으로 줄지 않는다. 취소·무응답처럼 실제로
기다리던 경로에서만 줄며, 그 값은 실측으로만 보고한다.

## 8. qualification-critical 경계 (결정 13·14 정합 정의)

일반 `vdeter run`이 reference와 **대조하는** critical 값 (불일치 = UNQUALIFIED,
RF 거부):

- (v2) installation manifest 적재·현재 파일 재측정 검증 + installation/rf/
  operation/tooling digest ↔ reference v2 대조 (영역별 조치 안내: rf/operation
  변경 → `vdeter qualify`, tooling 변경 → software bundle 재실행). Git 상태는
  대조하지 않는다. v1 reference(legacy git 결속)는 run에서 거부된다.
- (v1 이력) deploy manifest 검증 결과 (배포 tree 파일 전수 sha256)
- B210 serial↔역할 매핑, USB **속도 등급** (legacy Pi profile: MASS-A/ROC=
  super-speed, MASS-B=high-speed; Pi powered-USB3-hub profile: 3대 super-speed
  + `host_profile`·`usb_topology` 결속 — §15)
- **GNU Radio·UHD·numpy·cryptography 버전** — reference에 기록된 critical
  subset과 일치 (qualify 시점 실측이 기준)
- gain artifact SHA, identity fingerprint, platform_profile
- 필수 preflight: 키 존재·0600·key-cert 일치, config schema, 잔존 프로세스,
  디스크 여유

**기록만 하고 차단하지 않는 값** (결정 12·13): USB 포트 경로, 위 4종 외의
패키지 버전. 환경 manifest **정확 일치**는 qualify·audit 전용.

## 9. Preflight (결정 12·13)

- platform_profile=`pi`: `vcgencmd get_throttled == throttled=0x0` 필수 +
  기종 기록. `generic-pc`: CPU governor·thermal 상태는 **기록만** — Phase I
  에서는 정의되지 않은 최소 기준을 강제하지 않는다.
- 잔존 프로세스: 기존 `_COMPETING_PROCESS_PATTERN`과 동일 패턴을 로컬
  `pgrep`으로. 발견 시 **자동 kill 없이 시작 거부** + 수동 조치 안내.
- MASS-B 외부 전원: 전기적 probe 불가 — hardware.toml 선언 존재만 확인.

## 10. 정상 종료 순서 (결정 17·19 — 명문화 항목)

coordinator cleanup의 순서 계약 (기존 구현 그대로, 로컬 계층은 이 완료를
검증만 한다):

```
1. shutdown 마커 감지 → 새 workload 접수 중단 (요청 루프 탈출)
2. 진행 중 workload child가 있으면 session_shutdown abort → bounded 회수
3. endpoint STOP barrier (신규 RX 차단·진행 작업 정리 포함)
4. endpoint launcher 프로세스 회수 (terminate→wait→kill bounded) — decode
   worker는 launcher 종료에 따라 회수된다 (별도의 명시적 pool shutdown을
   주장하지 않는다)
5. 종료 후 worker 프로세스 0 검증 (잔존 검사)
6. B210 3대 handle 반환
7. closed.json (cleanup pass 여부) → done.rc 기록
```

로컬 계층의 신호 의미론:

- **첫 Ctrl+C / SIGTERM**: 위 정상 절차 개시. close 제한시간 초과 시 자동
  terminate→kill 에스컬레이션.
- **두 번째 Ctrl+C**: 제한시간을 기다리지 않고 즉시 에스컬레이션.
- 강제 종료 후 `closed.json`·`done.rc` 부재 또는 cleanup 미확인이면 **성공으로
  간주하지 않는다** — 실패로 정직 보고하고 잔존 검사 결과를 함께 출력.
- 오류 종료도 동일 close 경로. 어떤 경로든 그 시점까지의 evidence 보존.

bring-up 자동 재시도 (결정 16): host-time 획득의 일시적 실패로 **명확히
분류된** 첫 시도 fail-closed에 한해, cleanup PASS + 잔존 프로세스 0 + B210
3대 재발견을 모두 확인한 뒤 새 run-id로 세션 전체를 최대 1회 재시도.
설정 오류·장비 불일치·키 오류·UNQUALIFIED·cleanup 실패는 재시도 없이 즉시
종료. 두 시도의 evidence 모두 보존. 성공률은 보장값이 아니며 Phase I
반복시험으로 평가한다.

## 11. CLI 출력 규격 (결정 21)

- 모든 출력 **영어**. 요약 원칙: 터미널=상위 진행·판정·소요·evidence 경로,
  상세=evidence 파일.
- 시나리오 진행은 **발생 즉시 실시간 출력** — 로컬 `workload-trace.jsonl`
  tail(0.2~0.5초 폴링), 표시 알갱이는 기존 trace 이벤트(submit/transfer/
  received/verdict) 단위. controller에 이벤트 추가 없음.
- 타임스탬프: bring-up 줄 = 세션 시작 기준 `[MM:SS]`, 시나리오 줄 = 해당
  workload 시작 기준(각 0:00부터). 절대시각은 evidence trace에 이미 존재.
- 비대화형 실행 마지막 줄: `RESULT overall=<PASS|FAIL> scenarios=<n>/<m>
  run-id=<id> evidence=<path>`. `--json` 시 요약 JSON 출력.
- 색상: TTY에서만 PASS/FAIL 강조, 비-TTY 자동 무색. spinner·progress bar 금지.
- 오류 3요소 고정 형식: what was rejected & why / impact / what to do.

종료 코드: `0`=PASS · `1`=실행 FAIL(시나리오/세션/cleanup) · `2`=사용자 설정
오류 · `3`=장비 불일치 · `4`=UNQUALIFIED · `5`=내부/환경 오류 ·
`130`=사용자 중단.

## 12. 검증 계획 (결정 6·9·14)

- **V1**: 신규 계층 unit/contract — 가짜 coordinator 하네스(주입 runner/
  spawn)로 §3 책임 10항목·§10 신호 의미론·§6 필드 생성을 검증. 기존 회귀
  전체 무영향 확인.
- **V2 (Pi 실장비)**: `setup` 멱등 → `qualify` 완주(새 reference, RR-P1
  gain 재측정 기록) → `vdeter run all` 4종 evidence → bring-up 재시도 정책
  반복시험 → Ctrl+C·오류 종료·flock·results·secret 스캔 감사.
- **V3 (amd64)**: 장비 없는 Ubuntu PC에서 setup·UNQUALIFIED 거부·오류
  메시지 경로 확인.

신규 standalone 경로는 Phase I에서 **최초 자격 검증이 필수**다. Phase H
reference는 재사용하지 않는다 (결정 9·14).

## 13. 변경하지 않는 것

coordinator·endpoint launcher·RF/PHY/MAC·시간 획득 알고리즘·threshold·
deadline·판정 기준 전부, 기존 host 실행 경로 파일 전부(재현·감사 전용),
Phase H evidence, strict 하드닝 4건(`_stale_events`·`depth.inflight`·무잡음
RX 에너지 게이트·duplicate ACK 미검증 범위 — 알려진 한계로 승계).

기준선 충돌 기록(동결 검토 시 재제시): host git/MD 결속 제거, 플랫폼
일반화(Pi 5 전제), USB 포트 경로 비강제, 일반 실행 검증 범위 한정.

## 14. 자격 이력 (요약)

현행 자격 상태의 권위는 설치 root의 `var/qualification/active.json`(release 포인터)과
그 release의 reference·`.sha256` sidecar다. 이 README는 자격값을 적지 않는다.

- Phase I(2026-09-04/05, Pi 5 arm64) standalone 생애주기 완주: QUALIFIED
  (RESEARCH_READY), `vdeter run all` 4/4 PASS. 이때 확정한 정책 — 결속 3영역 분리,
  governance 시험 6 node-id deselect, evidence_reuse 조건(rf·operation byte-identical),
  `critical_endpoints_from_preflight()`의 `{serial, speed_class}` 기록 — 은 §7·§8에
  반영돼 있다.
- Pi powered-USB3-hub profile(2026-09-10)과 Stage 8 리팩터링·시간 최적화
  (2026-09-16, §7 묶음 선행·§7B·§7C)는 각각 새 사다리로 재자격했다.
- 보증 범위는 reference가 결속한 **동일 설치 tree·B210 3대·identity·라이브러리
  조합**에 한정된다. 다른 장비·다른 설치본은 `vdeter qualify` 없이 UNQUALIFIED다.

유보 항목: Link 19(운용 범위 제외) · USB hot-unplug recovery(미보증) · 장기 soak(미수행)
· host-time bracket 재획득(알려진 가용성 제한) · amd64 generic-pc(구현 완료, 실제 환경
검증 미수행) · strict 하드닝 4건(§13).

## 15. Pi powered-USB3-hub host profile (2026-09-10, additive)

같은 Raspberry Pi 5에서 B210 3대를 **하나의 self-powered USB3 hub**(uplink =
Pi SuperSpeed root 포트) 아래에 두는 topology를 위한 두 번째 host profile.
기존 legacy Pi profile은 값·판정·reference 모양 모두 **그대로**다.

| | legacy `STAGE7_PI5_NATIVE_2XUSB3_1XUSB2_V1` | `STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1` |
|---|---|---|
| hardware.toml | `schema_version = 1` (hardware_profile·usb_hub 키 금지 = 모순) | `schema_version = 2` + `hardware_profile` + `[usb_hub]` |
| speed class | MASS-A super / **MASS-B high(480M)** / ROC super | 세 endpoint 전부 **super(5000M)** |
| 전원 | MASS-A/ROC `PI_BUS_POWER`, MASS-B 실제 외부 DC asset(`external_power_asset`) | 세 endpoint `POWERED_USB3_HUB_BUS_POWER`; 개별 DC asset 선언 금지 |
| usb_transport | `DIRECT_USB3_5000M` / `DIRECT_USB2_480M_EXTERNAL_DC` | `POWERED_USB3_HUB_5000M` ×3 |
| topology 검사 | serial↔역할 + 속도 등급 | + 세 장치가 **같은** class-09 SuperSpeed hub 아래, hub는 root 포트 직결, descriptor self-powered 비트, 승인 VID:PID, 초과 B210 없음 |
| endpoint template | `VDETER/configs/b210_system/endpoints/` | `VDETER/configs/b210_system/endpoints-pi-powered-usb3-hub/` (serial·usb_physical_path·host_machine_id provisioning) |
| reference 결속 | 종전 critical subset | + `standalone_critical.host_profile`, `standalone_critical.usb_topology`(hub asset/VID:PID·역할별 serial/speed_class/transport·전원 출처), `hardware_observations`(기록만) |

규칙의 단일 출처는 `VDETER/src/vdeter/b210_system/host_profiles.py`다. `local_preflight.
ROLE_SPEED_CLASS`·coordinator `_EXPECTED_USB_SPEED`는 legacy 표 값 그대로이며
(시험이 정의와의 동일성을 고정), preflight·config loader·RR-P0는 hardware.toml /
resolved config의 profile에서 기대값을 가져온다. RR-P0는 powered-hub profile일 때
만 sysfs 공유 hub 검사를 추가로 수행한다(`usb_hub_topology`).

관측값과 선언값의 분리: USB path·hub port·link speed·device number·hub descriptor
`self-powered` 비트(bmAttributes bit 6)는 sysfs **관측**이며 기록만 한다(device
number는 결속 아님). "hub 외부 전원이 연결돼 있다"와 "B210 개별 DC 전원이 없다"는
소프트웨어가 측정할 수 없는 **operator attestation**이며 `[usb_hub]`에 출처
(`operator_attested`)와 함께 기록한다. 둘 다 필요하다 — descriptor가 bus-powered로
관측되면 attestation이 있어도 거부, attestation이 없으면 descriptor 비트가 있어도
거부.

```toml
schema_version = 2
platform_profile = "pi"
hardware_profile = "STAGE7_PI5_POWERED_USB3_HUB_3XUSB3_V1"

[usb_hub]
asset_id = "HUB-VIA-2109-0817-01"     # 운용자 inventory ID (probe 불가)
vid_pid = "2109:0817"                 # sysfs 관측값과 일치해야 함
self_powered = true                   # operator attestation
self_powered_source = "operator_attested"
individual_b210_dc = false            # operator attestation
individual_b210_dc_source = "operator_attested"

[endpoints.MASS-A]
serial = "34D03DD"
[endpoints.MASS-B]
serial = "3515425"                    # 5000M hub link — external_power_asset 없음
[endpoints.ROC]
serial = "34D03E8"

[wiring]
profile = "C1-C6-STANDARD"
approved = true
```

`vdeter setup`은 정확히 세 B210이 하나의 self-powered SuperSpeed hub 아래에
5000M로 관측될 때 이 schema의 초안을 쓰고, 그 밖에는 legacy 초안을 쓴다
(`var/setup-usb-topology.json`에 관측 topology와 초안 결정 기록). 초안은 기대
프로필(§4, 기본 실험실 프로필)과 관측을 대조해 관측된 기대 serial·hub asset을
**기대값**으로 채우되 attestation은 거부되는 기본값(`self_powered=false`,
`individual_b210_dc=true`, `*_source` 줄 주석 처리)과 `approved=false`를 유지한다. `vdeter run`은 close 뒤 sysfs를 재관측해 세 장치가 같은 hub
아래 5000M로 돌아왔는지 기록·판정한다(`usb_topology_after_close`).

fail-closed 목록(전부 RF 전 거부): 어느 장치든 480M, 다른 hub/root 아래, hub
없음·cascade·480M link·class 비-hub·descriptor bus-powered·VID:PID 불일치, 초과
B210, serial/역할 불일치, asset/attestation 누락·모순, 개별 `external_power_asset`
선언, schema 1에 powered-hub 키 혼입, 알 수 없는 필드, host_machine_id/usb path
provisioning 불가. legacy profile에서 MASS-B 5000M 또는 DC asset 누락은 종전처럼
거부되며, hub 전원 token(`POWERED_USB3_HUB_BUS_POWER`)은 DC asset으로 받지 않는다.

시험: `tests/test_standalone_pi_powered_hub.py`, `VDETER/tests/unit/
test_stage7_configuration.py::TC_STAGE7_HOST_PROFILE_PI_POWERED_HUB`,
`tests/fixtures/stage7_phase_i_function_bindings.json`(run_rr_p0 재고정; CPython
3.12.3/3.12.13 해시 동일 확인).
