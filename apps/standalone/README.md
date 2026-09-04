# VDETER Standalone (Phase I) — 설계 명세

문서 지위: Phase I W1 산출물. 2026-09-03~04 설계 협의(결정 22건)와 구현 승인
조건 4건을 코드 계약 수준으로 명문화한다. 이 문서가 W2~W6 구현의 기준이며,
기존 코드·evidence 계약과 어긋나는 서술이 발견되면 코드 계약이 우선한다.

대상 독자: 이 저장소를 처음 받은 운용자(§1~§2, §10~§11)와 구현·감사자(전체).

---

## 1. 개요와 명령 생애주기

목표: **코드 + 장비(B210 3대)만으로** 임의의 native Ubuntu(arm64/amd64)에서
MASS-A/MASS-B/ROC 3-endpoint Stage 7 시스템을 단독 기동·자격·운용한다.
Windows host, SSH(자기참조 포함), host Git, baseline Markdown, 외부 네트워크
서비스, 실행 중 dependency 다운로드에 의존하지 않는다.

```
vdeter setup       # 기계당 1회. apt(온라인 허용)+venv+lock, identity 생성,
                   # hardware.toml 초안 생성. 멱등 — 기존 identity 불변경.
(사용자)           # hardware.toml 편집·승인 (역할↔serial, MASS-B 전원, 배선)
vdeter qualify     # 단독 RR 재자격 (수 시간, RF 송신). 전체 PASS 시에만
                   # active reference 원자적 발급. 그 전까지 UNQUALIFIED.
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

## 5. standalone-binding (패키징 시 생성)

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
- 확인 실측(2026-09-04): 현행 `docs/core/stage7_b210_system_baseline.md`
  SHA-256 = 위 값과 정확 일치 (v0.9.23 자격 결속값 그대로).

## 6. git_state.json 호환 생성 (승인 조건 1)

coordinator `_load_run_metadata`/`validate_rr_binding`은 configs-dir의
`git_state.json`을 읽는다. **파일명·스키마를 유지**하되 내용은 host Git·
Markdown 없이 구성한다. 필드별 출처:

| 필드 | 기존(host prepare) 출처 | standalone 출처 |
|---|---|---|
| `run_id` | 요청 run-id | 동일 (현재 run) |
| `qualification_profile` | `"RESEARCH_READY_MINIMAL"` | 동일 상수 |
| `rr_phase` | profile 지정 | run: `"rr-p2-17"` (서비스 workload 결속 규약 유지) / qualify: 해당 RR phase |
| `data_link_id` | phase 말미 17/19 | 동일 규칙 |
| `baseline_version` / `baseline_sha256` | host Markdown 재해시 | **standalone-binding 승계** |
| `git_commit` | host `git rev-parse` | **standalone-binding `source_head`** |
| `git_dirty` | host `git status` | 배포 tree 무결 검증(§9) 통과 시 `false`, 미통과면 run 자체를 거부 |
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

`--baseline-sha256` 인자에는 binding의 `baseline_sha256`을 넘긴다.
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
5. RR-P2-19                   → TC-STAGE7-RR-006 (기록 전용 — 판정 이관 항목)
6. RR-P3-fault                → TC-STAGE7-RR-007 (판정 필수)
7. final builder 검증          # 아래 verification bundle 포함
8. 전체 PASS → candidate를 releases/<id>로 이동 후 active 포인터 원자 교체
```

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
- final builder 판정 필수 TC = RR-001~005 + RR-007 (`stage7_rr_final_builder.
  REQUIRED_TCS` 실측). 사다리는 이를 정확히 산출하며 RR-006은 기록 전용.
- gain 재측정 결과는 기록·비교한다. ROC lane1 75dB가 지속되면 기존 관찰·
  물리 경로 점검 항목을 유지한다 (자동 해소로 간주하지 않음 — 결정 19).

**reference 발급 (승인 조건 2)**: `stage7_issue_qualification_reference.py`
CLI는 SSH·Git 결합이라 직접 호출하지 않는다. 재사용하는 것 — import 폐포
계산·critical manifest 조립 규칙·reference 스키마(`STAGE7_QUALIFICATION_
REFERENCE` schema 1)·atomic publish 패턴·발급 직후 `stage7_profile` 재검증.
대체하는 것 — 3중 대조(Phase F 기록/Pi 실측/로컬)를 **로컬 배포 tree 실측
단일 기준 + deploy manifest 검증**으로, `require_clean_worktree`를 binding
`source_worktree_dirty=false` + 배포 무결 검증으로. `qualified_source_commit`
에는 binding `source_head`를 기록한다.

**final builder head 결속 (승인 조건 3)**: `build(..., head_provider=...)`에
binding `source_head`를 돌려주는 provider를 주입한다. verification bundle의
`frozen-hash-audit.json` `head_commit`도 같은 값이므로 "audit ↔ 현행 head
일치" 검사가 배포 환경에서 성립한다.

**verification bundle 로컬 생성**: final builder `--verification-root` 요구
3종을 대상 기계에서 생성한다.

- `frozen-hash-audit.json` — git blob 대조 대신 **frozen 14파일의 정규화(LF)
  내용 sha256 ↔ binding `frozen_files` 대조** (동일 정규화 규칙 재사용).
  `all_unchanged`·`files`·`head_commit`(=binding source_head)·
  `worktree_dirty`(=배포 tree manifest 전수 검증 통과 시 false) 기록.
  하나라도 불일치·검증 불능이면 fail-closed.
- `unit-contract.log` — unit+contract suite 로컬 완주 로그.
- `integration-stage7.log` — stage7 integration 부분집합 완주 로그.

## 8. qualification-critical 경계 (결정 13·14 정합 정의)

일반 `vdeter run`이 reference와 **대조하는** critical 값 (불일치 = UNQUALIFIED,
RF 거부):

- deploy manifest 검증 결과 (배포 tree 파일 전수 sha256)
- B210 serial↔역할 매핑, USB **속도 등급** (MASS-A/ROC=super-speed,
  MASS-B=high-speed)
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
