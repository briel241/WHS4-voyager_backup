# VDETER Standalone (Phase I) — 설계 명세

문서 지위: Phase I W1 산출물. 2026-09-03~04 설계 협의(결정 22건)와 구현 승인
조건 4건을 코드 계약 수준으로 명문화한다. 이 문서가 W2~W6 구현의 기준이며,
기존 코드·evidence 계약과 어긋나는 서술이 발견되면 코드 계약이 우선한다.

대상 독자: 이 저장소를 처음 받은 운용자(§1~§2, §10~§11)와 구현·감사자(전체).

---

## 0. Quick Start (clone-bootstrap v2, 2026-09-05)

```
git clone <repository>
cd <repository>
./bin/vdeter setup
# hardware.toml 편집·approved=true
vdeter qualify --yes
vdeter run all
vdeter results
```

- `setup` 이후 Git 불필요: 대상 기계의 setup·qualify·run·results는 `git`
  명령을 호출하지 않고 `.git`·`standalone-binding.json`도 읽지 않는다 — Git
  정보는 관측 자체를 하지 않으며 reference·git_state의 git 필드는 항상 null
  이다. 기준선 Markdown도 읽지 않는다. `.git` 없는 **tar/USB 복사본**도
  동일하게 동작한다.
- 일반 실행은 오프라인. 새 환경은 qualification 1회. 현재 Pi의 기존
  qualification(v1 reference)은 **기존 설치본을 건드리지 않았기 때문에**
  보존된다. 그 설치 root를 이 main으로 갱신하면 v1 reference로는 v2 실행이
  거부되므로 `vdeter qualify`(전체 RF 사다리)를 다시 통과해야 한다.
- amd64 지원 코드는 구현됐지만 실제 amd64 시험은 유보. 자격은 외부 공인
  인증이 아닌 RESEARCH_READY 내부 자격.
- `bin/vdeter`(저장소 launcher)는 venv가 없으면 `setup`만 시스템 python3로
  허용하고, 있으면 `var/venv/bin/python3`로 CLI를 exec한다. setup이
  `~/.local/bin/vdeter` 사용자 launcher를 설치한다(타 프로그램 덮어쓰기 없음,
  멱등 갱신, 저장소 이동 시 stale 오류). 최상위 `vdeter` 파일이 아닌 이유는
  `VDETER/` 디렉터리와 대소문자 무시 파일시스템 충돌 때문이다.

**v2 결속 요약** — Git 기반 `standalone-binding.json`은 필수 입력에서 제거됐다.
`./bin/vdeter setup`이 설치 파일을 실측해 `var/installation/`에 네 manifest
(`installation-manifest.json`, `rf-manifest.json`, `operation-manifest.json`,
`tooling-manifest.json`; canonical JSON, 파일별 SHA-256, 영역별 SHA-256,
atomic write, `var/` 제외)를 만든다. 영역 분류(`qualification_domains.py`):
rf(PHY/MAC/Link/Radio·coordinator·launcher·pi_runtime·RF config·RF phase
인자/run mode·RF config 생성 코드) / operation(run_cmd·local_session·
local_config·local_preflight·identity·MASS/ROC application·scenario controller
·Service API 결속) / tooling(setup·qualify 비RF·verification bundle·final
builder·reference 발급/검증·분류기·qualification 시험 코드) / 비결속(Markdown·
보고서·이미지). 미분류 실행 파일은 operation, 교차영향 파일은 여러 영역에
중복 결속. reference **v2**는 installation/rf/operation/tooling SHA·파일별
SHA map·환경/라이브러리 버전·B210 serial과 역할·USB speed_class·배선 승인·
gain artifact·identity fingerprint·resolved config SHA·TC 결과·evidence SHA·
qualification method·limitations를 결속한다. `provenance`에는 기준선 pin만
있고 `git`은 항상 null이다(실행 환경에서 Git 정보를 관측하지 않음). v1
reference는 과거 evidence 조회용으로 계속 읽힌다. coordinator 계약
(`git_state.json`)은 무수정이며 git 필드는 항상 null, `installation-state.json`
(v2)을 additive로 함께 쓴다.
`baseline_sha256` 인자는 커밋 상수 pin(`provenance.STAGE7_BASELINE_PIN`)이다.

**검증 범위 (2026-09-05 clean checkout 실측)** — ignored 산출물이 없는 새
checkout에서 root 시험(standalone·MASS/ROC·host/v1 계약) 260 passed·2 skipped
(POSIX 전용), integration `-k stage7` 10 passed. 반면 `VDETER/tests` unit+
contract는 13건이 실패하는데, 이는 standalone 기능이 아니라 과거 evidence
(`VDETER/results/runs/…`)·`VDETER/legacy`·docs/core 같은 저장소 외부 자료를
요구하는 governance 시험이다(standalone bundle은 그중 6 node-id를 명시
deselect). 따라서 **"전체 저장소의 모든 시험이 clean clone에서 통과한다"고는
말할 수 없다**. standalone 동결의 blocker는 아니며, VDETER 시험 인프라 정리
(fixture화 또는 governance 표시)를 후속 항목으로 남긴다.

**진행 표시(presentation)** — `progress.py`(표준 라이브러리). setup·qualify·
run bring-up: TTY 20칸 유니코드 바(불가 시 ASCII), 같은 줄 갱신, 단계 전환
시 확정; non-TTY/nohup/redirect: CR·ANSI 없는 UTC 줄 로그, 상태 변화·30초
heartbeat. **각 줄(단계)의 퍼센트는 그 단계 자체의 진행률**(0→100%, 단계 안에서만
단조)이고, 사다리·bring-up 전체 진행률은 `overall=NN%` 필드다. 세 의미: exact
(실제 분모, `estimated` 없음)/estimated(가중치, 퍼센트 뒤 `estimated`)/waiting
(host-time 등 완료 불명, 퍼센트 정지). `--no-progress`.
READY 이후 시나리오 구간은 **저 I/O 경로**(2026-09-06 R1 선택 롤백, 88fa6df와
동일): `running SC-…` 한 줄, workload trace 행(세션의 trace tail, on_trace)과
종료 시 `result: PASS/FAIL · 소요 · quiescence` 요약만 터미널에 낸다.
service_events 폴링·실시간 [TX]/[RX]/[STEP] 세로 블록·transfer별 강제 trace
flush·presentation 파일 갱신은 **실행 경로에 없다**(mirror는 READY에서 끊는다).
상세 분석은 종료 후 evidence(`workloads/<id>/`, `evidence/service_events/`)와
`vdeter results`로 한다. bring-up 단계 줄은 영어(coordinator의 `연결됨`은
표시에서 `connected`).
CLI 출력·오류 메시지·setup이 쓰는 hardware.toml 초안 주석은 모두 영어다(2026-09-05
사용자 결정; 소스 주석·docstring·이 README는 한국어 유지). coordinator·launcher가
직접 내는 콘솔 원문(예: `config 적재 실패`)은 RF 계층 무수정 원칙에 따라 그대로다.
파생값은 `derived=true`. 같은 정보를 `var/runs/<id>/presentation/`·
`var/progress/*.log`에 보존한다. 판정·threshold·deadline·retry는 불변.

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
└─ stage7_coordinator_main.py    # 무수정. 명시적 argv로 Popen (shell 문자열 금지)
   ├─ MASS-A endpoint launcher   # coordinator가 spawn (기존 그대로)
   ├─ MASS-B endpoint launcher
   ├─ ROC endpoint launcher
   └─ workload child (stage7_mass_roc_controller.py)
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
→ 기존 Stage7ConfigurationLoader로 검증 (무수정 재사용) → 실패 시 RF 전 중단
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
- station identity·주파수·sample-rate·channel·판정 기준은 ②에 고정 — ①로
  변경 불가(치환 대상 필드가 아님). ② 변경은 manifest 검증이 fail-closed.
- (2026-09-10 additive) `schema_version = 2`는 Pi powered-USB3-hub host
  profile 전용이다 — §15. schema 1은 위 legacy Pi profile 그대로다.

## 5. standalone-binding (legacy — v2에서 필수 입력 아님)

> **v2 (2026-09-05)**: 대상 기계 실행 경로는 이 파일을 더 이상 요구하지 않는다.
> 결속은 §0의 installation manifest(`var/installation/`)로 대체됐다. 이 절은
> 개발 PC 패키징 도구(`deploy_standalone_to_pi.py`)와 과거 evidence 이해를
> 위한 이력이다. 파일이 남아 있어도 참고 provenance로만 기록되고 설치 스캔에서
> 제외된다.

일반 실행·qualify가 host Git·Markdown을 읽지 않기 위한 **결속 상수 파일**.
경로: `VDETER/configs/stage7/standalone-binding.json`.

**생성 주체는 개발 환경 전용 패키징 도구 `VDETER/tools/
make_standalone_binding.py` (신규 파일 계획에 포함, git 필요)** 이며 대상
기계에서는 읽기 전용이다. **대상 기계의 setup은 Git을 읽지 않는다.**
정적 커밋이 아니라 배포물 생성 시점 산출인 이유: `source_head`는 배포물이
잘린 commit이라 커밋 후 고정될 수 없다.

```jsonc
{
  "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
  "generated_utc": "...",
  "source_head": "<40-hex git head — 배포물 원본 commit>",
  "source_worktree_dirty": false,        // dirty 트리에서 패키징 금지
  "baseline_version": "0.9.23",
  "baseline_sha256": "2a87c1d0f7bee78c13def8b153745ecbe42ac7dcf701d69abffc9141bc9cc026",
  "frozen_files": { "src/vdeter/config/models.py": "<정규화(LF) sha256>", ... }
                                         // stage7_frozen_hash_audit.FROZEN_FILES 14개
}
```

- `baseline_sha256`는 자격이 승계할 **이력 metadata**다. 일반 실행·qualify는
  Markdown을 다시 읽거나 SHA를 재계산하지 않는다. 문서 실물 대조는 문서가
  존재하는 host 감사 환경(audit)에서만 수행한다 (결정 8·20-2).
- **배포 패키지**(V2 b안): `VDETER/tools/deploy_standalone_to_pi.py`가 지정
  commit의 추적 파일(`git archive` 원본) + 생성된 binding + 자기 기술
  manifest를 하나로 묶어 SSH/SCP **1회**로 전송하고 원격에서 전수 SHA·
  binding head를 대조한다. results/·var/·identity/·hardware.toml·.git 제외.
  전송 뒤 setup·qualify·run은 host 없이 동작한다 — 그것이 standalone 기준.
  binding은 커밋하지 않는다(커밋하면 HEAD가 바뀌어 source_head와 어긋난다).
- 확인 실측(2026-09-04): 현행 `docs/core/stage7_b210_system_baseline.md`
  SHA-256 = 위 값과 정확 일치 (v0.9.23 자격 결속값 그대로).

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

사다리 (사용자 확정 순서):

```
1. RR-P0                      → TC-STAGE7-RR-001
2. RR-P1 (+gain 측정)         → TC-STAGE7-RR-002·003·004 + gain artifact
3. candidate reference 생성    # 이후 단계·향후 run이 쓸 결속값 확정
4. RR-P2-17                   → TC-STAGE7-RR-005
5. RR-P3-fault                → TC-STAGE7-RR-007 (판정 필수)
6. verification bundle → final builder 검증
7. 전체 PASS → candidate를 releases/<id>로 이동 후 active 포인터 원자 교체
```

**Link 19 제외 (2026-09-04 정책 변경, 사용자 지시)**: RR-P2-19(TC-RR-006)는
standalone 자격 사다리에서 제거했다. 근거 — 현재 프로젝트의 운용 시나리오
3종은 Link 11/17만 쓰고, final builder는 v0.9.10부터 RR-006을 DEFERRED(기록
전용)로 다룬다. 2차 ladder(qual-20260904-055636)가 rr-p2-19에서 ROC RX
`LinkIdDetectionError`(frozen PHY 계층)로 정지한 것이 계기다. Link 19의
PHY·profile·registry·단위시험 코드는 삭제·수정하지 않으며, 최종 reference와
보고서에 다음 제한을 명시한다: *Link 19 implementation present / not
exercised by standalone qualification / hardware performance not guaranteed /
operational scenarios use Link 11/17 only* (`qualify_cmd.LINK19_LIMITATION`).

자격 실행의 fail-closed 보강 (W5 승인 조건):
- 각 phase는 독립 process group으로 실행한다. timeout·중단 시 group 전체를
  TERM→KILL로 정리하고 **group 자체의 소멸**을 bounded 재확인한다. leader가
  정상 rc=0으로 끝나도 손자가 남으면 자연 소멸을 짧게 기다린 뒤
  `cleanup_anomaly`로 기록하고 강제 정리·재확인한다 — **정리에 성공해도
  anomaly가 있으면 그 phase는 자격 실패**다(coordinator 자체 회수가 실패한
  run). phase 후 잔존 프로세스 0 실측은 별도 사후 판정으로 유지한다.
  verification pytest 실행에도 timeout이 있다.
- GNU Radio·UHD·numpy·cryptography 버전 4종이 비어 있지 않은 문자열로
  실측되지 않으면 **P0 시작 전에 거부**한다 — 부재를 자격값으로 기록하는
  경로가 없다.

- RR-P3-soak(RR-008)은 범위 제외. 어느 단계든 실패 시 candidate 미승격,
  기존 active reference 유지.
- **phase별 run_mode는 명시 고정**(`qualify_cmd.PHASE_RUN_MODE`, 2026-09-04
  승인): rr-p0 = RF_OFF_QUALIFICATION, **rr-p1 = GAIN_CALIBRATION**(gain
  입력 없음, 새 gain artifact 산출), rr-p2-17·rr-p3-fault = OPERATIONAL
  (rr-p1 산출 gain 결속). 1차 RF qualification이 rr-p1을 template 기본값
  (RF_OFF)으로 생성해 `calibrate_rf PREPARE REJECTED`로 실패한 뒤 고정했다
  — endpoint runtime은 GAIN_CALIBRATION에서만 gain 탐색을 수락한다.
- final builder 판정 필수 TC = RR-001~005 + RR-007 (`stage7_rr_final_builder.
  REQUIRED_TCS` 실측, 무변경). 사다리는 이를 정확히 산출한다. RR-006은
  builder의 DEFERRED_TCS(기록 전용) 그대로이며 없거나 미실행이어도 판정에
  들어가지 않는다.
- gain 재측정 결과는 기록·비교한다. ROC lane1 75dB가 지속되면 기존 관찰·
  물리 경로 점검 항목을 유지한다 (자동 해소로 간주하지 않음 — 결정 19).

**RR-P3-fault 물리 USB detach 제외 (2026-09-04 정책 변경, 사용자 지시)**:
TC-STAGE7-RR-007은 소프트웨어 장애 소항목 6개(stale-generation-command,
worker-kill, coordinator-ipc-loss, restart-generation-fencing,
scenario-controller-ipc-loss, stale-callback)로만 판정한다 — 6개 전부 PASS면
PASS, 하나라도 FAIL이면 FAIL. **물리 USB detach·재연결·USB3 속도 복원·UHD
재열거 복구**는 현재 프로젝트의 운용 시나리오와 무관한 하드웨어 장애 복구
시험이라 standalone 자격 범위에서 제외했다(coordinator `run_rr_p3_fault`에서
소항목 제거; HARDWARE_BLOCKED 생성·USER-ACTION 안내·
`STAGE7_P3_USB_DETACH_WAIT_S`·물리 조작 대기 없음). 이것은 **VDE-TER 프로토콜
판정 완화가 아니라 범위 제외**다. 제한 명시(`qualify_cmd.USB_DETACH_LIMITATION`,
reference `limitations`): *physical USB detach recovery not exercised / B210
hot-unplug/re-enumeration recovery not guaranteed / 정상 세션 종료와 다음 실행의
B210 재발견은 기존대로 검증 / VDE-TER 정상 통신·Link 17·세 시나리오 보증에
영향 없음*. 3차 ladder(qual-20260904-081804)가 usb-detach HARDWARE_BLOCKED로
정지한 것이 계기이며, 그 evidence와 Phase F의 usb-detach PASS evidence는
과거 기록으로 보존한다. **알려진 문제(미수정)**: coordinator
`write_rr_evidence`의 `assert status in (PASS, FAIL, ABORTED)`는
HARDWARE_BLOCKED를 거부해 TC 파일이 기록되지 않는다 — 해당 경로가 더 이상
도달되지 않으므로 기록만 한다.

**reference 발급 (승인 조건 2)**: `stage7_issue_qualification_reference.py`
CLI는 SSH·Git 결합이라 직접 호출하지 않는다. 재사용하는 것 — import 폐포
계산·critical manifest 조립 규칙·reference 스키마(`STAGE7_QUALIFICATION_
REFERENCE` schema 1)·atomic publish 패턴·발급 직후 `stage7_profile` 재검증.
대체하는 것 — 3중 대조(Phase F 기록/Pi 실측/로컬)를 **로컬 배포 tree 실측
단일 기준 + deploy manifest 검증**으로, `require_clean_worktree`를 binding
`source_worktree_dirty=false` + 배포 무결 검증으로. `qualified_source_commit`
에는 binding `source_head`를 기록한다.

**final builder 결속 (승인 조건 3 → v2)**: `build(..., head_provider=...)`에
현행 **installation digest**를 돌려주는 provider를 주입한다. verification
bundle의 `frozen-hash-audit.json`은 v2 installation audit(`installation_sha256`
·`installation_verified`·rf 영역 파일별 대조)이며, builder는 v1(`head_commit`
↔ git HEAD)과 v2(`installation_sha256` ↔ 현행 digest)를 필드명으로 구분해
병행 지원한다(계약시험 `VDETER/tests/unit/test_stage7_rr_final_builder_
installation_audit.py`). 두 값은 서로 위장할 수 없다(40-hex ≠ 64-hex).

**verification bundle 로컬 생성**: final builder `--verification-root` 요구
3종을 대상 기계에서 생성한다.

- `frozen-hash-audit.json` — git blob 대조 대신 **frozen 14파일의 정규화(LF)
  내용 sha256 ↔ binding `frozen_files` 대조** (동일 정규화 규칙 재사용).
  `all_unchanged`·`files`·`head_commit`(=binding source_head)·
  `worktree_dirty`(=배포 tree manifest 전수 검증 통과 시 false) 기록.
  하나라도 불일치·검증 불능이면 fail-closed.
- `unit-contract.log` — unit+contract suite 로컬 완주 로그.
- `integration-stage7.log` — stage7 integration 부분집합 완주 로그.

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

## 14. Phase I 결과 기록 (2026-09-04/05, Pi 5 arm64 standalone)

이 절은 Phase I(standalone 실행 경로)의 **실측 결과와 보증 범위**를
기록한다. §1–§13의 설계 명세는 변경하지 않는다.

### 14.1 최종 판정

Raspberry Pi 5(Ubuntu 24.04, aarch64) standalone 배포 트리에서 `vdeter
setup → qualify → run all → results` 생애주기를 완주했다. 자격은
**QUALIFIED(final_status RESEARCH_READY, profile RESEARCH_READY_MINIMAL)**,
운용 시험 `vdeter run all`은 **4/4 PASS**다. 이 자격은 외부 공인 인증이
아닌 **프로젝트 내부 RESEARCH_READY 자격**이며, 통제된 conducted
케이블 testbed에서의 연구 실험 진입 가능성만을 뜻한다.

### 14.2 활성 qualification reference 결속값

| 항목 | 값 |
|---|---|
| release / active pointer | `qual-20260904-134421-r2` (promoted 2026-09-04T18:39:00Z) |
| reference SHA-256 | `aa72badcfbf24520552ccc3a57e495e312a616ea0669e988146222fb0614d46b` |
| qualification_method | `evidence_reuse` — 전체 사다리 `qual-20260904-134421`의 RF evidence 재사용 (RR-P0 → RR-P1 → RR-P2-17 → RR-P3-fault, RR-001·002·003·004·005·007 PASS, baseline 0.9.23 / `2a87c1d0…`) |
| supersedes | `qual-20260904-134421` reference `1ed9fb4f…` — 발급 결함(endpoints에 `speed_class` 누락)으로 대체, **삭제하지 않고 보존** |
| qualified_source_commit | `88fa6dfcf133e047606de55c71849bdb32486368` (= standalone-binding head, frozen 14) |
| deploy manifest | `21695f1227a104ec…` (519 files) — resolved config `deployment_manifest_sha256`와 동일 |
| platform_profile | `pi` |
| endpoints (critical `{serial, speed_class}`) | MASS-A `34D03DD`/super · MASS-B `3515425`/high · ROC `34D03E8`/super (usb_path·speed_mbps는 관측 metadata) |
| identity fingerprint | `b7ea9461ea15` |
| library versions | gnuradio 3.10.9.2 · UHD 4.6.0.0 · numpy 1.26.4 · cryptography 50.0.1 |
| gain | MASS-A/MASS-B/ROC 69/69/69 dB (RR-P1 재측정, artifact `f086abcf…`) |
| binding_domains | rf_evidence 205 files(이전 manifest `e74cb12b…` + qualify_cmd RF-critical symbol 5종 AST 해시) · operation 70 · qualification_tool 225 · test_doc(.md) 19 비결속 |
| verification bundle (r2) | unit+contract 1037 passed / 10 skipped / **29 deselected**(governance 6 node-id, `verification/governance-deselected.json`) · integration `-k stage7` 10 passed · frozen-hash audit all_unchanged |

evidence_reuse 자동 허용 조건은 **rf 영역 byte-identical AND operation
영역 byte-identical**이며, RF-critical symbol(사다리 순서·run mode·phase
인자·gain 소비 phase·`default_phase_runner`) 해시가 하나라도 다르면 거부한다.
qualification 시험 파일·선택 목록 변경은 RF 재실행 없이 software bundle
재실행을 요구하고, Markdown 변경은 자격에 영향이 없다.

### 14.3 `vdeter run all` 결과 (run-20260904-184019)

2026-09-04T18:40:20Z – 19:06:41Z(1580 s), bring-up 1회 READY, 자동 재시도
0회. hardware lifecycle(host-time 획득·active bridge·B210 acquire·endpoint
start/stop·rf_time pre/post·service admission) 각 1회.

| workload | 판정 | 소요 |
|---|---|---|
| SC-VDE-005 (normal) | PASS | 43.0 s |
| SC-VDE-006 (normal) | PASS | 75.2 s |
| SC-VDE-001 normal | PASS | 18.9 s |
| SC-VDE-001 forged | PASS (위조 인증서 `bad-signature` 거부, trust_store_size 0) | 17.7 s |

wrong_dest·duplicates·mismatched_ack·failed 모두 0, payload 무결성·ACK
correlation·quiescence 확인. 종료 후 workers_after_stop 0, 잔존 프로세스 0,
cleanup PASS, B210 3대 재발견(5000/480/5000 Mbps), throttled 0x0. summary의
`reference_sha256`는 활성 reference와 동일. MASS-B의 host-time bracket
내부 재획득 1회는 run 수준 재시도가 아니며 알려진 가용성 제한이다.

### 14.4 발견·수정한 결함

- **RR-P1 TC-004 RAW-PHY 슬롯 정렬**: RX 캡처 슬롯 시간이 PRE_CORRECTION
  기준이라 TX(active bridge correction, MASS-A +210.6 µs) 대비 burst onset이
  복호 여유(≈60–70 sample) 안쪽으로 밀리는 잠복 결함. host 경로에도 존재하던
  timing margin 결함이며 standalone 배포가 유발한 것이 아니다. `pi_runtime`
  에서 RAW_PHY phase가 TX와 동일한 시간 변환 함수를 쓰도록 수정(commit
  `2978e9c`), 오프라인 재생 18/18, 결정적 시험 12건, RF 재시험 C1–C6 PASS.
- **verification bundle governance 시험**: docs/core·traceability·저장소
  구조 의존 시험 6 node-id를 명시적 `--deselect`로 분리(코드·evidence에 목록과
  사유 기록, host V1/CI에서는 계속 실행). import 격리 실패 4건은 별도
  프로세스에서 PASS 확인.
- **결속 3영역 분리**: 파일 전체 결속 때문에 tooling 수정만으로 RF 재실행이
  강제되던 구조를 rf / operation / qualification_tool / test_doc으로 분리.
- **발급 결함**: evidence_reuse 경로가 endpoints를 `{serial, usb_path,
  speed_mbps}`로 기록해 `compare_critical`이 거부 → 두 발급 경로가 공통
  `critical_endpoints_from_preflight()`로 `{serial, speed_class}`를 생성하고
  발급 시 speed_class ∈ {super, high}를 검증하도록 수정(commit `88fa6df`),
  r2로 재발급.

### 14.5 마무리 감사 (2026-09-05, RF 없음)

- 소프트웨어 시험: root(standalone·domains·run/CLI·reference·preflight)
  231 passed / 1 skipped · VDETER unit+contract 1075 passed / 1 skipped ·
  integration `-k stage7` 10 passed · `git diff --check` 이상 없음.
- 결속 감사(실제 검증기 `load_reference`·`deploy_manifest.verify`·
  `compare_critical`·preflight): 17/17 PASS.
- evidence 보존: `VDETER/results/phase-i/PHASE-I-FINAL-AUDIT/`(git 비추적)
  347 files, 42,331,378 bytes, Pi 원본 289 files SHA-256 전부 일치, JSON/JSONL
  파싱 오류 0, 내부 참조(pointer→release→reference·sidecar·final-result 원본
  결속·run summary·resolved config·supersedes) 전부 PASS. manifest
  `EVIDENCE-MANIFEST.json` SHA-256 `cd522ec7992ab49dc8f272416ac5043bd2079973f525a9e14c9025a2ed62539b`.
- secret 검사: evidence·보고서·추적 파일에 개인키 본문·PEM private 마커·
  비밀번호/토큰·SSH 비밀·identity private 파일 없음(추적 `.pem` 2개는 공개
  인증서·trust store). 공개 fingerprint·장비 serial은 허용된 공개 기록.
- 생성형 `standalone-binding.json`은 release artifact로 배포 트리·package에만
  보존하고 Git에 커밋하지 않는다.

### 14.6 보증 범위와 유보 항목

| 항목 | 상태 |
|---|---|
| Link 19 | 현재 운용 범위에서 제외 |
| USB hot-unplug recovery | 미보증 |
| 장기 soak | 미수행 |
| host-time bracket 재획득 가능성 | 알려진 가용성 제한 |
| amd64 Ubuntu (generic-pc) | 지원 경로 구현 완료, 실제 환경 검증은 장비 부재로 유보 (§12 V3 미수행). amd64 지원 구현 완료, 실제 환경 검증 미수행 |
| 자격의 성격 | 외부 공인 인증이 아닌 프로젝트 내부 RESEARCH_READY 자격 |
| strict 하드닝 4건 (§13) | 알려진 한계로 승계 |

보증 범위는 **위 결속값과 동일한 Pi 5 arm64 배포 트리·B210 3대·identity·
라이브러리 조합**에 한정된다. 다른 장비·다른 배포 트리는 `vdeter qualify`
재발급 없이는 UNQUALIFIED로 거부된다.


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
| endpoint template | `VDETER/configs/stage7/endpoints/` | `VDETER/configs/stage7/endpoints-pi-powered-usb3-hub/` (serial·usb_physical_path·host_machine_id provisioning) |
| reference 결속 | 종전 critical subset | + `standalone_critical.host_profile`, `standalone_critical.usb_topology`(hub asset/VID:PID·역할별 serial/speed_class/transport·전원 출처), `hardware_observations`(기록만) |

규칙의 단일 출처는 `VDETER/src/vdeter/stage7/host_profiles.py`다. `local_preflight.
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
5000M로 관측될 때 이 schema의 초안(asset REQUIRED_FILL, attestation은 거부되는
기본값)을 쓰고, 그 밖에는 legacy 초안을 쓴다(`var/setup-usb-topology.json`에
관측 topology 기록). `vdeter run`은 close 뒤 sysfs를 재관측해 세 장치가 같은 hub
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
