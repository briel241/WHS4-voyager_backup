# VDETER Stage 7 — MASS/ROC Phase G 인수인계

작성 시각: 2026-08-31 (**Phase F 재자격 PASS 완료**, Phase G 미진입)
브랜치: `test/mass-roc-stage7-rf` · HEAD `419bfef`
작업 루트: `C:\Users\DH\Desktop\Project\VDETER-integration-worktree`

이 문서는 **Phase F 재자격까지 끝내고 Phase G에 들어가기 직전**의 상태를 정리한 것이다.
Phase G 본 실행은 하지 않았다 — 사용자가 "재자격 받고 승인을 기다려라, Phase G에
진입하지 말라"고 지시했기 때문이다.

**이어받는 사람은 §10.1의 1번부터 시작하면 된다.**

---

## 0. 한 문단 요약

MASS/ROC 업무 시나리오를 Stage 7 실장비(라즈베리파이 + B210 3대)에서 돌리기 위한
**bounded persistent service session**을 구현했다. 하드웨어를 세션당 한 번만 세우고,
그 위에서 시나리오(workload)를 여러 번 갈아끼우는 모델이다. 소프트웨어는 완성됐고
시험도 통과했지만, 실제 RF 실행에서 네 번 막혔다. 원인은 순서대로 (1) endpoint START
호출 누락, (2) admission gate 경로 불일치, (3) 펌프 게이트에서 `TBB_ACTIVE` 누락으로
인한 자기 교착이었다. 셋 다 고쳤다. (3)의 수정이 qualification-critical 파일을 건드리므로
**Phase F 재자격(RR-P1)을 다시 받았고 PASS했다**(`mass-roc-rr-p1-20260831e`,
`skipped_past_due = 0`). 남은 일은 그 재자격에 결속된 **새 reference를 발급하고
Phase G를 실행**하는 것이다.

---

## 1. 절대 지켜야 할 제약 (사용자 지시 — 위반 금지)

이 항목들은 사용자가 반복해서 명시한 것이다. 어기면 작업 자체가 무효가 된다.

### 1.1 qualification-critical 파일

RF 판정 결과에 영향을 주는 파일들은 Phase F 자격에 **해시로 결속**돼 있다.
임의로 수정하면 안 되고, 수정이 필요하면 **BLOCKED로 보고하고 사용자 승인을 받아야**
한다. 승인 후 수정했다면 **Phase F 재자격(RR-P1 RF 실행, 약 53분)이 반드시 따른다.**

두 계층으로 나뉜다:

| 계층 | 대상 | 결속 방식 | 변경 시 |
|---|---|---|---|
| `strict_files` (154개) | launcher RF/런타임, radio worker, PHY/MAC/timing, RF 설정, gain | **전체 파일 SHA-256** | 한 줄만 바꿔도 재자격 |
| `function_bound_files` (1개) | `VDETER/apps/stage7_coordinator_main.py` | **자격 함수 18개의 AST/source 해시** + 추가분 증명 | 자격 함수 밖이면 재자격 불필요 |

**절대 건드리면 안 되는 것** (사용자 명시):
`_run_rf_qualification`, CFO/adaptive gain/raw-PHY 경로, RF 설정, gain artifact,
qualification 임계값, 기존 RR 명령 경로, `rf_time.py`.

coordinator의 자격 함수 18개 (하나라도 AST가 바뀌면 재자격):
```
_live_b210_speed_map, _rediscover_b210, _run_rf_qualification, _run_rr_service_phase,
_spawn_controller, _spawn_mass_roc_controller, _worker_process_count, _write_plan,
load_group, run_mass_roc, run_p0_prepare_configure_arm, run_rr_p0, run_rr_p1,
run_rr_p2, run_rr_p3_fault, run_rr_p3_soak, spawn_and_connect, write_rr_evidence
```

**변경 전 반드시 이 검사를 돌려라** (로컬 Python 버전 차이 때문에 reference의 해시와
직접 비교하면 전부 불일치로 보인다 — HEAD 대비 비교가 정답):
```bash
git show HEAD:VDETER/apps/stage7_coordinator_main.py > /tmp/coord_head.py
python -c "
import ast,hashlib,io,sys
def fns(p):
    t=ast.parse(io.open(p,encoding='utf-8').read())
    return {n.name:(hashlib.sha256(ast.dump(n).encode()).hexdigest(),
                    hashlib.sha256(ast.unparse(n).encode()).hexdigest())
            for n in t.body if isinstance(n,ast.FunctionDef)}
a=fns(sys.argv[1]); b=fns('VDETER/apps/stage7_coordinator_main.py')
print('변경:',sorted(k for k in a if k in b and a[k]!=b[k]))
print('삭제:',sorted(k for k in a if k not in b))
print('추가:',sorted(k for k in b if k not in a))
" /tmp/coord_head.py
```

### 1.2 비밀 취급

- 개인키의 **내용·DER·base64·hex·SHA를 출력·복사·기록하지 않는다.** 계측에도 넣지 않는다.
- 키/인증서 정합은 **Pi 내부에서 유도 공개키를 비교**해 True/False만 출력한다.
- 개인키 파일 권한은 **0600**.
- `--force` 회전 금지.
- 배포 도구는 개인키를 싣지 않는다(정책). 새 배포마다 **Pi 안에서만** 기존 배포본의
  키를 복사해 프로비저닝한다. 사용자 승인은 Phase E/F/G 배포에 대해 이미 받았다.

### 1.3 git

- 커밋 메시지에 **Claude 마크(Co-Authored-By 등)를 넣지 않는다.** 사용자 identity로만 커밋.
- **push / merge / tag 금지.** 커밋까지만.

### 1.4 계측

계측은 **상위 orchestration 계층에만** 넣는다. 다음은 절대 계측 금지:
endpoint launcher 실시간 펌프, B210 worker 콜백, radio adapter 핫루프,
PHY/MAC/slot, timing/`rf_time`, `_run_rf_qualification`.
계측 유무가 판정을 바꾸면 안 되므로 **fail-open**이어야 한다(emit이 예외를 삼킨다).

### 1.5 실행 규율

- **RF 세션은 병렬 불가.** Pi 한 대에 B210 3대뿐이라 두 세션이 동시에 뜨면 장치 경합으로
  양쪽 다 깨진다. 분석·조사는 병렬로 해도 되지만 RF 실행은 직렬.
- 이미 RF가 시작된 상태에서 새 설계를 이어붙이지 않는다. STOP/cleanup 후 상태를 보고하고
  **새 배포와 fresh session으로 다시 시작**한다.
- 각 단계가 끝나면 **멈추고 보고**한다. 다음 단계는 사용자 승인 후.
- 승인 요청이나 핵심 보고 시 **알림음**을 울린다 (`[console]::beep`).

---

## 2. 시스템 구성

```
호스트 (Windows)                     Raspberry Pi (voyager3@192.168.0.179)
─────────────────                    ────────────────────────────────────
apps/scenario_runner.py  ──ssh──▶   apps/stage7_coordinator_main.py
  menu --transport stage7                    │ SOCK_SEQPACKET × 3
  --profile <profile.json>                   ▼
                                     stage7_endpoint_launcher.py × 3
apps/stage7_session.py                 ROC / MASS-A / MASS-B
apps/stage7_service_layer.py                 │
apps/stage7_profile.py                       ▼ USB
apps/stage7_trace.py                   B210 × 3
apps/stage7_mass_roc_controller.py
```

B210 시리얼 ↔ 역할 (**고정, 바꾸지 말 것**):

| 역할 | 시리얼 | USB 경로 | 속도 |
|---|---|---|---|
| MASS-A | `34D03DD` | 3-1 | 5000 Mbps |
| MASS-B | `3515425` | 4-2 | **480 Mbps (USB 2.0)** |
| ROC | `34D03E8` | 5-1 | 5000 Mbps |

MASS-B의 USB 2.0은 **정상이다.** 성공했던 모든 실행에서 동일했으므로 실패 원인 후보에서
배제하라.

배선: `STAGE7_C1_C6_TRIANGLE_V1` (6경로 C1~C6, pair `{RA, RB, AB}`)

**중요**: `evaluate_pre_correction_set`은 `{RA,RB,AB}` 6경로 또는 단일 pair만 받는다.
`("RA","RB")` 같은 조합은 "지원하지 않는 pair 조합" 예외가 난다. 즉 **4경로 축소는 불가능**하다.

---

## 3. Phase G 목표 (사용자가 지시한 최종 모델)

### 3.1 핵심 요구

CLI 한 번 실행 = **하드웨어 bring-up 한 번**. 그 뒤 메뉴에서 시나리오를 고를 때마다
**application 상태만** 새로 만든다.

| 세션당 1회 (workload 수와 무관) | workload마다 새로 |
|---|---|
| B210 acquire | controller / agent 객체 |
| endpoint START | TrustStore 인스턴스 |
| acquire_host_time | event log baseline |
| C1~C6 RF-time PRE/POST | step/scenario ID namespace |
| active bridge commit | |
| HOST_TIME_LOCKED | |
| service admission → TBB → publish | |
| endpoint STOP + cleanup | |

- workload 사이에 **quiescence를 기계로 확인**한다. 실패하면 다음 workload를 시작하지 않고
  세션을 fail-closed 종료한다.
- `q` 또는 fault에서 **한 번만** 전체 cleanup.
- 진입점은 **정확히 이것 하나**여야 한다:
  ```
  python3 apps/scenario_runner.py menu --transport stage7 --profile <profile.json>
  ```
- Phase G 실 RF 범위: **4개 workload를 각 1회씩**
  `SC-VDE-005` → `SC-VDE-006`(standalone) → `SC-VDE-001 normal` → `SC-VDE-001 forged`.
  메뉴의 `a`(전체 배치)는 계약 시험으로만 증명하고 RF로 돌리지 않는다.

### 3.2 절대 하지 않는 것

service session은 **RF qualification이 아니다.** 다음을 실행하지 않는다:
`_run_rf_qualification`, CFO qualification, adaptive gain 탐색, raw-PHY qualification.
증거는 `OPERATIONAL_SERVICE`로만 표기하고 **RR/RESEARCH_READY로 표기하지 않는다.**
자격은 Phase F reference가 담보한다.

검증 지표: `qualification_commands_issued`가 **빈 배열**이어야 한다
(`start_cfo`, `calibrate_rf`가 하나도 없어야 함).

---

## 4. 지금까지 한 것

### 4.1 단계별 이력

| 단계 | 목표 | 하드웨어 | 결과 |
|---|---|---|---|
| B | MASS/ROC ↔ Stage 7 백엔드 구현 | 없음 | 완료 |
| C | 소프트웨어 전용 검증 | 없음 | PASS |
| D | Pi 배포 + resolved config | Pi만 | PASS |
| E | RF-off P0 (preflight 10주기 + RR-P0) | B210 무송신 | PASS |
| F | RR-P1 자격 재취득 | **실제 RF** | PASS (`mass-roc-rr-p1-20260831c`) |
| **F′** | **RR-P1 재자격 (펌프 게이트 수정 반영)** | **실제 RF** | **PASS** (`mass-roc-rr-p1-20260831e`) |
| G | 상주 세션 4 workload | 실제 RF | **미진입 — 여기서부터 이어받는다** |

### 4.2 커밋 (최근 → 과거)

```
419bfef  펌프 게이트에 TBB_ACTIVE/TBB_ACQUIRING 추가 — 자기 교착 해소   ← 재자격 대상
0b306d1  세션 절대 시한을 idle 상한과 분리 (45분 → 2시간)
fd358a3  coordinator session phase에 구조화 계측 추가
0762a31  session lifecycle 대행이 session run-root의 admission gate를 보도록 수정
ae894d9  persistent session이 endpoint START/STOP을 실제로 수행하도록 수정
cd7dc41  bounded persistent service session과 상위 계층 계측 (Phase G 최종 모델)
d8922f4  service phase 명령에서 --workload 제거
568267d  원격 명령 출력 인코딩을 UTF-8로 명시
b47cee6  qualification-critical 경계를 다중 granularity로 (2-tier)
4d5ecc4  service-only phase와 formal service 계층
c063d0d  실시간 펌프를 서비스 구간으로 한정 — rr-p1 gain revalidate 회귀 봉합  ← Phase F 자격 커밋
af9066f  Stage 7 연동 시험 backend (Phase B)
```

### 4.3 코드 인벤토리

**신규 (호스트 측 서비스 계층)**

| 파일 | 역할 |
|---|---|
| `apps/stage7_session.py` | `Stage7ServiceSession` — 상주 세션 수명주기. `start()` / `run_workload()` / `close()` / `collect_evidence()`. 상태: `NEW→VALIDATING→STARTING→TIME_LOCKING→SERVICE_ADMISSION→READY→WORKLOAD_RUNNING→QUIESCING→STOPPING→CLOSED/FAILED` |
| `apps/stage7_service_layer.py` | SSH 래퍼, `verify_remote_environment()`(preflight), `prepare_service_config()`, `build_coordinator_script()` |
| `apps/stage7_profile.py` | `load_profile()`, `load_reference()`(사이드카 SHA 검증), `validate_local_bindings()` |
| `apps/stage7_trace.py` | `TraceWriter`(버퍼링 JSONL, seq + monotonic/UTC 이중 시각, session/workload ID 결속), `NullTrace`, `scrub()`(비밀 마스킹) |
| `apps/stage7_mass_roc_controller.py` | MASS/ROC 업무 컨트롤러. **기존 RR 컨트롤러와 별개** |

**수정**

| 파일 | 변경 |
|---|---|
| `apps/scenario_runner.py` | `menu --transport {direct,stage7} --profile --verbose --diagnostics` |
| `VDETER/apps/stage7_coordinator_main.py` | `run_mass_roc_session()`, `run_mass_roc_service()`, `_spawn_session_lifecycle()`, `_open_session_trace()` **추가**. 자격 함수 18개는 불변 |
| `VDETER/apps/stage7_endpoint_launcher.py` | `_PUMP_ACTIVE_STATES`에 `TBB_ACQUIRING`, `TBB_ACTIVE` 추가 ← **strict, 재자격 원인** |

**시험** (전체 869 passed, 8 skipped)

```
tests/test_stage7_persistent_session.py      상주 세션·격리·계측·시한 계약
tests/test_stage7_service_cli.py             메뉴 CLI (실제 SSH 미접촉, _FakeSession mock)
tests/test_mass_roc_stage7_backend.py        stage7 전송 백엔드
VDETER/tests/unit/test_stage7_service_phase.py    자격 함수 불변·추가 전용 증명
VDETER/tests/unit/test_stage7_inline_service.py
VDETER/tests/unit/test_stage7_mass_roc_workload.py
```

실행: `VDETER/.venv/Scripts/python.exe -m pytest tests VDETER/tests/unit -q` (약 4분 20초)

---

## 5. Phase G 실 RF 시도 이력 — 매번 왜 죽었나

| # | 배포 | 실패 지점 | RF 도달 | 소요 | 원인 |
|---|---|---|---|---|---|
| 0 | pilot `svc-sc005-…022449` | TBB publish 시한 | 예 | 22.7분 | (펌프 게이트 도입 전) MASS-A rx-gate `gated=1 passed=0` |
| 1 | `…122422` | service-start intent 30초 창 | 예 | 24.1분 | **세션 소유 모드에 endpoint START 호출 자체가 없었음** |
| 2 | `…fix1` | preflight (critical 3개 missing) | **아니오** | 초 | 배포 도구 오선택 — VDETER 하위 트리만 업로드 |
| 3 | `…fix2` | config 생성 | **아니오** | 초 | 로컬 배포 매니페스트 사본 부재 |
| 4 | `…fix2` | service-start intent 30초 창 | 예 | 26.2분 | **대행이 잘못된 gate 경로 대기** |
| 5 | `…fix3` | **TBB publish 시한** | 예 | 28.6분 | **펌프 게이트에 `TBB_ACTIVE` 누락 → 자기 교착** |

2·3번은 RF 이전 fail-closed — 안전 장치가 설계대로 작동한 사례다.

### 5.1 각 결함의 정확한 내용

**(1) endpoint START 누락**
상주 세션 모델에서 endpoint lifecycle 소유권을 workload child → session으로 옮기면서
START 호출자를 없애기만 하고 session 쪽에 다시 넣지 않았다. admission을 열어도 아무도
START를 안 하니 endpoint가 intent를 남기지 못하고 30초 창이 만료.

→ 수정: 시나리오를 실행하지 않는 lifecycle 대행 추가
(`--lifecycle session-start|session-stop --scenario-id none`). coordinator가 admission
직전에 START 대행을 spawn하고 publish 이후 회수한다. RR 경로와 같은 구조.

**(2) gate 경로 불일치**
대행의 run-root는 `<run_root>/lifecycle/<단계>`인데 gate 경로를 자기 run-root 기준으로
계산해 `<run_root>/lifecycle/session-start/controller_gate/admission_open`을 기다렸다.
coordinator가 쓰는 곳은 `<run_root>/controller_gate`. 대행은 생기지 않을 파일을 조용히
기다렸다 (`session-start.log`가 완전히 비어 있던 것이 증거).

→ 수정: `--gate-dir`를 추가해 coordinator가 session run-root의 gate를 명시 전달.

**(3) 펌프 게이트 자기 교착 ← 현재 재자격 원인**

`VDETER/apps/stage7_endpoint_launcher.py`의 `_PUMP_ACTIVE_STATES`에
`TBB_ACTIVE`가 빠져 있었다. 서비스 구간 상태 전이는
`SERVICE_ADMISSION_READY → SERVICE_START_STAGED → TBB_ACTIVE → RUNNING`인데
경유 상태 하나가 게이트 목록에서 누락됐다.

ROC는 방송자라 **수신 없이** encode 완료 시점에 `_activate_canonical_tbb`로
self-activate한다(`src/vdeter/mac/coordinator.py:4091`). 그 즉시 `client.state`가
`TBB_ACTIVE`가 되고, 게이트에 없는 상태이므로 **ROC 자신의 펌프가 멈춘다.**
TBB 방송은 1주기당 fragment 1개 순환이고 완료 관측이 `radio.poll_once()`(=펌프)에만
달려 있어, 첫 조각 이후 방송이 끊긴다. MASS-A/B는 조립할 4조각이 공중에 없어
`SERVICE_START_STAGED`에 영구 정체하고, publish는 MASS 획득을 기다리므로 펌프도
영영 재개되지 않는다.

**결정적 증거 — rx-gate 로그 줄 수** (펌프 활동의 직접 지표):

| 실행 | ROC | MASS-A | MASS-B |
|---|---:|---:|---:|
| 성공 `rr-p2-17-20260830j` | 82 | 75 | 76 |
| 성공 `rr-p2-17-20260830k` | 64 | 64 | 64 |
| **실패 `phase-g-service-20260831-050301`** | **0** | 9 | 26 |

ROC만 정확히 0이고, ROC가 TBB_ACTIVE에 도달한 유일한 노드다.

**배제된 후보** (조사로 부정됨 — 다시 의심하지 말 것):
- TBB 90초 창 부족? → 성공 실행 admission→publish 실측 **9.9~11.8초**, 9배 여유.
- gain 부족? → 69 dB로 성공/실패 동일. 잡음 바닥·수신 전력도 동급.
- 자격 사다리 생략 영향? → 무관. 수신 감도 저하 흔적 없음.
- MASS-B USB 2.0? → 성공 실행 전부에서 동일.
- TBB 유효기간? → `[2026-08-31 00:00Z, 2026-09-02 00:00Z)`로 실행 시각 포함. 서명·
  fingerprint·wire round-trip 모두 RF 없는 재현으로 PASS. **획득 로직 자체는 무결.**

→ 수정: 게이트에 `TBB_ACQUIRING`, `TBB_ACTIVE` 추가. qualification 사다리 구간
(`CONFIGURED`·`CFO_*`·`RF_GAIN_*`·`HOST_TIME_*`) 제외는 그대로라 `c063d0d`가 막은
rr-p1 회귀 보호는 유지된다.

**(4) cleanup 계측 순서 결함** (부수 수정)
세션 `finally`가 `main()`의 launcher teardown보다 먼저 돌아, 정상 종료 중에도
`workers_after_stop: 3` / B210 재발견 전부 실패로 오보고했다. launcher를 실제로 내린
뒤 계측하도록 수정 → 이후 실행에서 `workers_after_stop: 0`, 재발견 3/3 true 확인.

**(5) 세션 절대 시한 부족** (부수 수정)
`PhaseGuard` deadline은 진행이 있어도 갱신되지 않는 절대 시각인데, 세션 guard가
idle 상한(45분)을 그대로 쓰고 있었다. bring-up이 최대 28.6분을 먹으므로 workload에
16분밖에 남지 않는 구조. **사용자 승인으로 `SESSION_DEADLINE_S = 2시간` 신설.**
`SESSION_IDLE_TIMEOUT_S = 45분`은 stall 감시 + 요청 대기 상한으로 유지.

---

## 6. 실측 시간 데이터

### 6.1 service session bring-up 구간 (누적 경과 초)

| 구간 | pilot | 시도1 | 시도4 | 시도5 |
|---|---:|---:|---:|---:|
| prepare/configure/arm_rx | 15.3 | 15.2 | 15.5 | 14.9 |
| acquire_host_time | 326.1 | 319.0 | 445.5 | 498.0 |
| RF-time PRE (C1~C6) | 783.3 | 776.2 | 902.7 | 955.2 |
| active bridge | 783.4 | 776.2 | 902.7 | — |
| RF-time POST → HOST_TIME_LOCKED | 1240.6 | 1233.5 | 1359.9 | 1412.5 |
| **총 소요** | **1361.0** | **1444.4** | **1572.0** | **1715.2** |

**병목**: RF-time PRE+POST가 매번 **914.4~914.5초**로 편차 0.2초 미만. 경로당 약 76초 ×
12회(6경로 × 2단계)의 고정 비용. 전체의 58%, admission 도달 시간의 67%.
변동 요인은 `acquire_host_time` 하나뿐(289~498초).

**주의**: bring-up이 계속 늘고 있다(1361 → 1444 → 1572 → 1715).
CLI의 `SESSION_BRINGUP_TIMEOUT_S = 2700`(45분) 대비 여유가 16분으로 줄었다. 감시 대상.

### 6.2 Phase F 자격(RR-P1) 구간 (누적, 참고)

```
start_cfo        247.6
acquire_host_time 536.3
calibrate_rf     1622.0   ← 적응 gain, 가장 큼
rf_time_pre      2079.2
rf_time_post     2536.5
raw_phy          3179.2
총               3180.2 (53분)
```

service session은 `start_cfo` + `calibrate_rf` + `raw_phy` = 1976초를 정확히 건너뛴다.

### 6.3 현재 시한 구조

| 항목 | 값 | 정의 위치 |
|---|---:|---|
| 세션 절대 시한 | **2시간** | `SESSION_DEADLINE_S` (coordinator) |
| idle 상한 (요청 없으면 종료) | 45분 | `SESSION_IDLE_TIMEOUT_S` |
| workload 하나당 | 900초 | `WORKLOAD_TIMEOUT_S` (`apps/stage7_session.py`) |
| bring-up 대기 (CLI) | 45분 | `SESSION_BRINGUP_TIMEOUT_S` |
| 종료 대기 | 600초 | `SESSION_CLOSE_TIMEOUT_S` |
| intent 창 | 30초 | 프로토콜 상수 |
| TBB publish 창 | 90초 | `SERVICE_START_TBB_PUBLISH_TIMEOUT_NS` |

---

## 7. 아키텍처 상세 (이어받는 사람이 알아야 할 것)

### 7.1 세션 bring-up barrier 순서

```
prepare → configure → arm_rx
  → acquire_host_time (affine fit)
  → validate_rf_time PRE (C1~C6, pair RA/RB/AB)
  → prepare_active_bridge → commit_active_bridge
  → validate_rf_time POST  ⇒ HOST_TIME_LOCKED
  → open_service_admission          ← 여기서 START 대행 spawn (직전)
  → [gate 파일 admission_open 작성]
  → 30초 안에 intent 3/3            ← 대행이 START 호출해야 성립
  → commit_service_start
  → 90초 안에 TBB_ACTIVE 3/3        ← ROC 방송 + MASS 획득
  → publish_service_start           ⇒ RUNNING
  → [workload 루프]
  → session-stop 대행 → stop barrier → cleanup
```

`HOST_TIME_LOCKED`가 admission을 허용하는 **유일한 도달 가능 상태**다.
(`"ARMED"`는 도달 불가능한 레거시 값이니 무시하라.)

### 7.2 lifecycle 대행이 왜 필요한가

endpoint의 service START/STOP은 **TEST adapter socket**(`<endpoint>.test.sock`)을 통해서만
가능하고, 그 연결은 controller 프로세스만 갖는다. 세션 모델에서 lifecycle의 주인은
session(coordinator)이지만 실제 호출은 대행 프로세스가 한다.

```
coordinator                          대행 controller (--lifecycle session-start)
──────────                          ────────────────────────────────────────
spawn(대행, --gate-dir=<run_root>/controller_gate)
open_service_admission()
write <run_root>/controller_gate/admission_open  ──▶  gate 감지
                                                      START × 3 (스레드, 블록)
intent 3/3 대기 ◀──────────────────────────────────── endpoint가 intent 기록
commit_service_start
TBB 대기 → publish_service_start  ──────────────────▶ START 반환
대행 회수 (rc 확인)
```

START는 publish까지 블록하므로 **admission 직전에 spawn하고 publish 이후에 회수**해야
한다. 순서를 바꾸면 교착이다.

### 7.3 workload 격리

각 workload는 별도 자식 프로세스(`--lifecycle session`)로 돌고, endpoint START/STOP과
admission을 만지지 않는다. 격리 수단:
- `EventLog(start_at_end=True)` — 이전 workload 이벤트를 보지 않도록 baseline 분리
- `scenario_id = f"mass-roc:{workload_id}"`, `step_id = f"{workload_id}/{step_id}"` — ID namespace 분리
- workload마다 새 controller/agent/TrustStore 인스턴스

앞뒤 게이트:
- **health gate** (workload 직전): launcher 생존 수, worker 3, inhibited 아님,
  faults/bridge_generation이 baseline과 동일, USB 속도 동일
- **quiescence gate** (workload 직후): child 종료, non-terminal transfer 0,
  worker 3, faults/bridge/USB 불변

### 7.4 계측

`apps/stage7_trace.py`의 `TraceWriter`. 레코드에 `seq`, `event`, `session_id`,
`workload_id`, `t_monotonic_s`, `t_utc`가 자동으로 붙고 `scrub()`이 비밀 키를 `<redacted>`로
바꾼다.

산출물:
- 로컬 `<evidence_root>/<run_id>/session-trace.jsonl` (호스트 세션 계층)
- 원격 `<run_root>/session-coordinator-trace.jsonl` (coordinator session phase)
- 원격 `<run_root>/evidence/service_events/{ROC,MASS-A,MASS-B}.jsonl` (endpoint 공개 이벤트)
- 원격 `<child_root>/workload-trace.jsonl` (workload별)

coordinator 계측 이벤트 (진단에 실제로 필요했던 것들):
```
session_phase_begin, phase_mark, rf_time_pre_summary, rf_time_post_summary,
lifecycle_proxy_spawn  ← gate_dir·gate_file·proxy_run_root·argv 전문
admission_open, intent_wait, intents_complete, intent_failed
   ← intents_recorded(ledger 스냅샷)·expected·proxy_alive·proxy_rc·gate_exists, 5초 주기
tbb_wait, tbb_complete, tbb_failed  ← tbb_active vs tbb_expected
service_start_committed, service_published, lifecycle_proxy_reaped
workload_health_gate, workload_child_spawn, workload_result
session_exception (traceback), session_closed
```

**이 계측이 없어서 gate 경로 결함 하나를 잡는 데 RF 실행이 세 번(약 80분) 들었다.**
비슷한 문제가 나오면 먼저 trace를 봐라.

---

## 8. 실행 절차 (정확한 명령)

### 8.1 Phase F 재자격 (RR-P1)

```bash
DEPLOY=phase-f-fix2-20260831-requal
RUN=mass-roc-rr-p1-20260831e
HOST=voyager3@192.168.0.179
BASELINE=2a87c1d0f7bee78c13def8b153745ecbe42ac7dcf701d69abffc9141bc9cc026

# 1. 배포 (VDETER 하위 트리 — RR 경로는 이걸로 충분)
VDETER/.venv/Scripts/python.exe VDETER/tools/deploy_stage7_to_pi.py \
    --host $HOST --run-id $DEPLOY

# 2. trust 개인키 프로비저닝 (Pi 내부에서만, 내용 미출력)
ssh $HOST '
  SRC=$HOME/vdeter-stage7/phase-f-fix-20260831-085442/VDETER/configs/stage7/trust
  DST=$HOME/vdeter-stage7/'"$DEPLOY"'/configs/stage7/trust
  umask 077; cp -p "$SRC/roc_tbb_signer_key.pem" "$DST/"; chmod 600 "$DST/roc_tbb_signer_key.pem"'

# 3. resolved config
VDETER/.venv/Scripts/python.exe VDETER/tools/prepare_stage7_run_configs.py \
    --run-id $RUN --deploy-run-id $DEPLOY --host $HOST \
    --external-power-asset-mass-b AT2024T \
    --run-mode GAIN_CALIBRATION --rr-phase rr-p1

# 4. 자격 실행 (약 53분)
ssh $HOST "cd \$HOME/vdeter-stage7/$DEPLOY && mkdir -p runs/$RUN && \
  PYTHONPYCACHEPREFIX=/tmp/stage7-requal-pyc \$HOME/vdeter-stage7/venv/bin/python3 \
  apps/stage7_coordinator_main.py --run-id $RUN --phase rr-p1 \
  --run-root \$HOME/vdeter-stage7/$DEPLOY/runs/$RUN \
  --configs-dir \$HOME/vdeter-stage7/$DEPLOY/run-configs/$RUN \
  --baseline-sha256 $BASELINE"
```

**PASS 판정 기준**: `TC-STAGE7-RR-002/003/004` 전부 PASS,
`skipped_past_due_all_tx_paths == 0`, `revalidate_all_paths_passed == true`,
`retries == 0`, raw-PHY `wrong_bytes_total == 0`.

### 8.2 Phase G (재자격 PASS 이후 — 아직 실행하지 말 것)

```bash
# 1. Phase G용 배포 — ★ 도구가 다르다 ★
#    Phase G는 워크트리 루트 레이아웃(<root>/VDETER/... + <root>/apps/...)이 필요하다.
#    deploy_stage7_to_pi.py는 VDETER 하위 트리만 올리므로 Phase G에는 못 쓴다.
#    기존 Phase G 배포본을 Pi 안에서 복제하고 변경 파일만 덮어쓰는 방식을 썼다:
ssh $HOST '
  OLD=$HOME/vdeter-stage7/phase-g-session-20260831-fix3
  NEW=$HOME/vdeter-stage7/<새이름>
  mkdir -p $NEW && cd $OLD && tar cf - --exclude=./runs . | (cd $NEW && tar xf -)
  mkdir -p $NEW/runs'
scp VDETER/apps/stage7_coordinator_main.py  $HOST:vdeter-stage7/<새이름>/VDETER/apps/
scp VDETER/apps/stage7_endpoint_launcher.py $HOST:vdeter-stage7/<새이름>/VDETER/apps/
scp apps/stage7_mass_roc_controller.py      $HOST:vdeter-stage7/<새이름>/apps/
# → 배포 후 로컬/원격 SHA-256 일치 확인 필수

# 2. deployment_manifest.json 재생성 (Pi에서 walk + sha256) 후
#    로컬 VDETER/results/runs/<새이름>/deployment_manifest.json 으로 내려받기
#    ★ 이 로컬 사본이 없으면 prepare_stage7_run_configs가 FileNotFoundError로 죽는다

# 3. 새 qualification reference 발급 (재자격 run에 결속)
#    scratchpad/phase_g_issue_reference.py 참고 — Phase F evidence를 실제로 읽어
#    critical 해시를 3중 대조(배포 기록 / Pi 실측 / 로컬)한다. 수동 전사 금지.

# 4. profile 작성 후 실행
python3 apps/scenario_runner.py menu --transport stage7 --profile <profile.json> --diagnostics
# 입력: 2\n3\n1\nnormal\n1\nforged\nq\n
#   (2=SC-VDE-005, 3=SC-VDE-006, 1=SC-VDE-001 normal, 1=SC-VDE-001 forged, q=종료)
```

### 8.3 실행 전 안전 게이트 (매번)

```bash
git rev-parse --abbrev-ref HEAD && git status --porcelain   # clean이어야 함
ssh $HOST '
  pgrep -af "stage7_|uhd_|usrp" | grep -v pgrep || echo "프로세스 0"
  for d in /sys/bus/usb/devices/*/; do s="$d/serial"; [ -f "$s" ] || continue
    ser=$(cat "$s"); case "$ser" in 34D03DD|3515425|34D03E8)
      echo "$(basename $d) $ser $(cat $d/speed)M";; esac; done
  vcgencmd get_throttled
  df -h /home | tail -1'
```

기대값: 프로세스 0, B210 3대(3-1/4-2/5-1, 5000/480/5000), `throttled=0x0`.

**주의**: `pgrep -af worker`로 검색하면 리눅스 커널 스레드(`kworker/*`)가 수백 개
나온다. 그건 무관하다. `stage7_|uhd_|usrp`로 좁혀라.

---

## 9. 현재 상황 — Phase F 재자격 PASS, Phase G 직전에서 정지

### 9.1 재자격 결과 (확정)

실행 `mass-roc-rr-p1-20260831e`, 배포 `phase-f-fix2-20260831-requal`,
coordinator **rc=0**, 소요 **3328.7초(55.5분)**.

| 판정 항목 | 결과 |
|---|---|
| TC-STAGE7-RR-002 (CFO) | **PASS** — 3경로 × 10버스트, retries 0 |
| TC-STAGE7-RR-003 (적응 gain) | **PASS** — 후보 10 → 최종 100버스트, retries 0 |
| TC-STAGE7-RR-004 (RF-time + raw-PHY) | **PASS** — raw-PHY 요약 6, **wrong_bytes 0**, retries 0 |
| **`skipped_past_due` 합계** | **0** (ROC/MASS-A/MASS-B × 12항목 전부) |
| adaptive gain | 69/69/69 (lane 0·1 모두 69) — Phase F와 동일 |
| RAW_PHY 경로별 버스트 | C1~C6 각 100/103 — 누락 없음 |
| RF-time 보정값 | `ROC (0,1)`, `MASS-A (-329579,4)`, `MASS-B (-430501,4)` |

**핵심 확인**: `skipped_past_due = 0`. Phase F가 원래 실패했던 지점이 이 계수
(C2=19·C4=3·C6=1 누락)였고, 그걸 막으려 넣은 게 펌프 게이트다. 게이트에 서비스 상태
두 개를 **추가**만 했으므로 사다리 구간 보호는 유지된다는 가설이었는데, 실측으로
확인됐다. **TBB 교착을 풀면서 `c063d0d`의 회귀 보호를 깨지 않았다.**

타이밍은 Phase F 대비 148초(4.7%) 느리다(3180.2 → 3328.7초). 판정 영향 없음.
`acquire_host_time`이 실행마다 289~498초로 요동치는 것과 같은 성격의 변동이다.

### 9.2 Pi 상태 (실행 종료 후 확인 완료)

stage7/UHD 프로세스 **0**, B210 3대 모두 원래 USB 경로·속도로 정상
(34D03DD 3-1/5000M, 3515425 4-2/480M, 34D03E8 5-1/5000M), `throttled=0x0`.

### 9.3 왜 여기서 멈췄나

사용자 지시: **"일단 재자격을 받고 내 승인을 기다려. 페이즈 G에 진입하지 말아봐."**
따라서 Phase G 배포·reference 발급·세션 실행은 **하나도 하지 않았다.**
이어받는 사람은 §10.1부터 시작하되, 각 단계 뒤에 사용자 보고·승인 규율을 지켜야 한다.

### 9.4 이 시점의 결속 정보

```
qualified_commit        419bfef  (펌프 게이트 수정 포함)
qualified_run           mass-roc-rr-p1-20260831e
qualified_deploy        phase-f-fix2-20260831-requal
deployment_manifest     45baacf168d619465bbf76a03a338abb6f276eb65816549633ecf3a40fc13d7e
baseline_sha256         2a87c1d0f7bee78c13def8b153745ecbe42ac7dcf701d69abffc9141bc9cc026
원격 evidence           ~/vdeter-stage7/phase-f-fix2-20260831-requal/runs/mass-roc-rr-p1-20260831e/
```

**주의**: 기존 reference(`VDETER/results/phase-g/phase-g-20260831-fix3/`)는 **구
Phase F 실행(`mass-roc-rr-p1-20260831c`, 커밋 `c063d0d`)에 결속**돼 있다.
launcher가 바뀌었으므로 그대로 쓰면 strict 해시 불일치로 preflight가 거부한다.
**반드시 새 reference를 발급해야 한다.**

---

## 10. 이어서 할 일

### 10.1 다음 작업 (여기서부터 이어받는다)

재자격은 끝났다(§9). 아래 1번부터가 남은 작업이다.
**각 단계 뒤에 사용자에게 보고하고 승인을 받은 뒤 다음으로 넘어간다.**

**1. Phase F evidence 로컬 회수**
원격 `~/vdeter-stage7/phase-f-fix2-20260831-requal/runs/mass-roc-rr-p1-20260831e/`를
`VDETER/results/phase-f/<새 phase-f 디렉터리>/`로 내려받는다.
기존 `phase-f-20260831-051614/`의 구성(`final-result.json`, `commands.txt`,
`local-file-hashes.json`, `rr-p1-evidence/`, `gain_artifact*.json`)을 참고해 같은 형태로 만든다.
Pi의 critical 파일 실측 해시(`logs/pi-critical-hashes.txt`)도 함께 받아야 3중 대조가 가능하다.

**2. 새 qualification reference 발급**
`scratchpad/phase_g_issue_reference.py`를 재자격 run 기준으로 갱신해 쓴다.
그 스크립트는 critical 해시를 ① Phase F 배포 시점 기록 ② Pi 실측 ③ 현재 로컬의
**3중으로 대조**하고 하나라도 어긋나면 BLOCKED로 종료한다. **수동 전사 금지.**
갱신해야 할 상수: `F_EVID`, `F_DEPLOY`, `P1_RUN`, `EXPECT_DEPLOY_MANIFEST`,
`QUALIFIED_COMMIT`(→ `419bfef…`), gain 해시(재자격 결과값으로).
산출물: `qualification-reference.json` + `qualification-reference.sha256` +
`qualification-critical-manifest.json` + `local-file-hashes.json`.
`stage7_endpoint_launcher.py`의 strict 해시가 **새 값으로 바뀌어야 정상**이다.

**3. Phase G 배포**
§8.2 방식(기존 Phase G 배포본 복제 + 변경 파일 덮어쓰기). **`deploy_stage7_to_pi.py`를
쓰면 안 된다** — 레이아웃이 다르다. 이번엔 launcher도 바뀌었으므로 덮어쓸 파일이 3개다:
`VDETER/apps/stage7_coordinator_main.py`, `VDETER/apps/stage7_endpoint_launcher.py`,
`apps/stage7_mass_roc_controller.py`. 로컬/원격 SHA-256 일치 확인 필수.
이어서 (a) `deployment_manifest.json` 재생성 + **로컬 사본 내려받기**,
(b) trust 개인키 Pi 내부 프로비저닝(chmod 600).

**4. Phase G profile 작성 + preflight 단독 검증**
`VDETER/results/phase-g/phase-g-20260831-fix3/stage7-profile.json`을 본으로 삼아
`pi.deploy_relpath`, `pi.deploy_run_id`, `orchestration.deployment_manifest_sha256`,
`orchestration.source_commit`, `evidence_root`를 갱신하고 2번의 새 reference를 옆에 둔다.
세션을 돌리기 **전에** preflight만 단독 실행해 확인한다:
```python
from apps.stage7_profile import load_profile
from apps.stage7_service_layer import verify_remote_environment
p = load_profile("<profile.json>"); r = verify_remote_environment(p)
# 기대: critical_manifest {checked:154, missing:[], mismatch:[], pass:True}
#       function_bound  {qualified_functions_checked:18, changed:[], missing:[], pass:True}
#       usb pass:True, throttled pass:True, trust pass:True
```

**5. Phase G 세션 실행 (본 목표)**
```bash
python3 apps/scenario_runner.py menu --transport stage7 --profile <profile.json> --diagnostics
# 입력: 2\n3\n1\nnormal\n1\nforged\nq\n
```
bring-up 약 25~30분 + workload 4개. 이번엔 TBB를 통과해야 정상이다.

**통과 확인 순서** (이전에 막혔던 지점들):
| 지점 | 확인 방법 |
|---|---|
| intent 3/3 (30초 창) | 콘솔 `terminal:commit_service_start` 도달 — 시도 5에서 이미 통과 |
| **TBB_ACTIVE 3/3 (90초 창)** | **이번 수정의 검증 지점.** trace `tbb_complete`의 `tbb_active` 3개 |
| publish → RUNNING | `[coordinator] endpoint START 3/3 확인`, `SESSION READY` |
| workload 루프 | **실 RF 최초 검증 구간** — health/quiescence gate 실동작 |

**판정 기준**: 4 workload 전부 PASS, quiescence 전부 pass,
`qualification_commands_issued == []`, cleanup `workers_after_stop == 0` ·
재발견 3/3 · `throttled=0x0`.

**6. 실측값 반영**
첫 성공 실행에서 **시나리오별 실제 RF 소요**를 측정해 `WORKLOAD_TIMEOUT_S`(현재 근거
없는 900초)를 근거 있는 값으로 조정한다. bring-up 시간도 기록해 §6.1 추세에 더한다.

### 10.2 아직 한 번도 실 RF로 검증되지 않은 것

- **workload 루프 전체.** publish → RUNNING 이후 코드는 계약 시험만 통과했고 실제
  RF에서 돌아본 적이 없다.
- MASS/ROC 시나리오(005/006/001)의 **실제 RF 소요 시간.** 이 값이 없어서
  `WORKLOAD_TIMEOUT_S`를 근거 없이 900초로 두고 있다. 첫 성공 실행에서 실측해 조정할 것.
- quiescence gate / health gate의 실 RF 동작.

### 10.3 알려진 함정

| 함정 | 증상 | 대응 |
|---|---|---|
| Phase G에 `deploy_stage7_to_pi.py` 사용 | preflight에서 critical 3개 missing | Phase G는 워크트리 루트 레이아웃 필요. §8.2 방식 사용 |
| 로컬 deployment manifest 사본 없음 | `prepare_stage7_run_configs`가 FileNotFoundError | Pi에서 scp로 `VDETER/results/runs/<deploy>/`에 내려받기 |
| 새 배포에 trust 개인키 없음 | ROC launcher 즉시 rc=1 | Pi 내부 복사 + chmod 600 (§8.1 2단계) |
| 자격 함수 해시를 reference와 직접 비교 | 18개 전부 불일치로 보임 | 로컬 Python 버전 차이. **HEAD 대비**로 비교할 것 |
| `pgrep -af worker` | kworker 수백 개 | `stage7_\|uhd_\|usrp`로 좁힐 것 |
| Windows 콘솔 인코딩 | `UnicodeEncodeError: cp949` | `PYTHONIOENCODING=utf-8` |
| pytest `--timeout` 옵션 | unrecognized arguments | 이 저장소 pytest에는 없음 |
| 4경로 RF-time 축소 시도 | "지원하지 않는 pair 조합" | 6경로 고정. 축소 불가 |

### 10.4 감시 항목

- **bring-up 시간 증가 추세** (1361 → 1444 → 1572 → 1715초). CLI 상한 45분까지 여유 16분.
- `acquire_host_time` 변동 폭(289~498초)이 예측을 어렵게 한다.
- `launcher_clean_exit: false`는 실패 경로에서 launcher를 강제 종료했다는 정직한 기록이다.
  정상 종료 시에는 true여야 한다.

### 10.5 참고 경로

```
로컬 evidence     VDETER/results/phase-{e,f,g}/
구 Phase F 자격   VDETER/results/phase-f/phase-f-20260831-051614/   (커밋 c063d0d 결속)
                    final-result.json, commands.txt, summary.md
★ 재자격 evidence ~/vdeter-stage7/phase-f-fix2-20260831-requal/
                    runs/mass-roc-rr-p1-20260831e/   ← 아직 로컬 미회수
구 Phase G 프로파일 VDETER/results/phase-g/phase-g-20260831-fix3/   (구 reference — 재사용 불가)
                    stage7-profile.json, qualification-reference.json(+.sha256)
원격 배포         ~/vdeter-stage7/<deploy-run-id>/
원격 실행         ~/vdeter-stage7/<deploy>/runs/<run-id>/
                    coordinator-console.log, logs/*.launcher.log,
                    session/{ready,closed}.json, evidence/,
                    session-coordinator-trace.jsonl
scratchpad 도구   phase_g_issue_reference.py   reference 발급 (3중 대조)
                  phase_g_reference_v2.py      2-tier manifest 생성
                  phase_f_requal2.sh           재자격 실행 스크립트 (이번에 사용)
```

### 10.6 마지막으로 한 일 (인수인계 경계)

```
419bfef  펌프 게이트 수정 (커밋)
         → 배포 phase-f-fix2-20260831-requal
         → trust 개인키 Pi 내부 프로비저닝
         → 재자격 mass-roc-rr-p1-20260831e 실행 → PASS (rc=0, skipped_past_due 0)
         → Pi 안전 확인 (프로세스 0, B210 3/3, throttled 0x0)
         → 사용자 보고 후 정지. Phase G 미진입.
```
