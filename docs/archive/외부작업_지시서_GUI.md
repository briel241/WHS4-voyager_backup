# 작업 지시서 — MASS/ROC 콘솔 GUI

> **이 문서를 받는 사람에게**: 당신은 이 프로젝트의 파일을 갖고 있지 않습니다.
> 이 문서 하나로 작업할 수 있도록 필요한 인터페이스를 전부 옮겨 적었습니다.
> **§9의 "받아야 할 파일"만 요청해서 받고**, 나머지는 이 문서를 근거로 만드세요.
> 확인이 필요하면 추측하지 말고 물어보세요.

---

## 1. 무엇을 만드나 (한 문단)

자율운항선박(MASS)과 육상 원격운영센터(ROC)가 무선으로 항로를 주고받는 시뮬레이션이
이미 파이썬으로 구현돼 있다. 지금은 터미널에서 시나리오를 골라 돌리면 결과가 텍스트로
나온다. **이것을 브라우저 두 창(ROC 콘솔 / MASS 콘솔)으로 볼 수 있는 로컬 웹 앱**으로
만드는 것이 이번 작업이다. 배가 항로를 따라 움직이는 것이 실시간으로 보이고, ROC 쪽에서
버튼으로 명령을 내리면 MASS가 받아 반영하는 모습이 두 화면에 각각 나타나야 한다.

**중요**: 기존 시뮬레이션 로직(`MassAgent`·`RocAgent`)은 **한 줄도 고치지 않는다.**
이미 검증된 코드다. 당신이 만드는 것은 그 위에 얹는 **구동기 + 화면**뿐이다.

---

## 2. 절대 지켜야 할 제약

| # | 제약 | 이유 |
|:-:|---|---|
| 1 | `MASS/mass_agent.py`, `ROC/roc_agent.py`, `common/**` 를 **수정하지 않는다** | 단위시험 13종이 통과 중인 검증된 코드다 |
| 2 | **외부 패키지를 쓰지 않는다.** 파이썬 표준 라이브러리만 | 팀이 설치 문제로 이미 한 번 고생했다. `python3 apps/…` 한 줄로 떠야 한다 |
| 3 | 새 파일은 전부 `apps/` 아래에 만든다 | 기존 구조를 지킨다 |
| 4 | 주석·UI 문구는 **한국어**, 코드 식별자는 영어 | 저장소 관례 |
| 5 | 파이썬 3.12 기준 | 개발 환경 |

> 제약 2가 특히 중요하다. Flask·FastAPI·websockets 등을 쓰지 말고
> `http.server` + `json` + 폴링으로 만든다. 4Hz 폴링이면 충분하다.

---

## 3. 만들 파일

```
apps/console_server.py     시뮬레이션 루프 + HTTP 서버 (진입점)
apps/console_static/
    roc.html               ROC 콘솔 화면
    mass.html              MASS 콘솔 화면
    console.css            공용 스타일 (두 화면이 같은 디자인 언어)
    console.js             공용 폴링·렌더 로직
```

실행:

```bash
python3 apps/console_server.py
# → http://localhost:8420/roc   와  http://localhost:8420/mass  를 안내 출력
```

---

## 4. 기존 코드 인터페이스 (이대로 쓰면 된다)

아래는 **실제 코드에서 그대로 옮긴 것**이다. 이 시그니처를 신뢰해도 된다.

### 4.1 MassAgent — 배

```python
from MASS.mass_agent import MassAgent, ShipState

@dataclass
class ShipState:
    latitude: float
    longitude: float
    heading: float = 0.0          # 침로 (도)
    sog: float = 0.0              # 속력 (knot)
    navigation_status: int = 0
    autonomy_mode: int = 2        # 1=RC(원격제어) 2=MON(감독자율) 3=AUT(독립자율)

@dataclass
class MassAgent:
    mmsi: int = 440_000_001
    state: ShipState = ShipState(latitude=35.0, longitude=129.0)
    report_interval: float = 2.0   # 위치보고 주기(초)
    max_turn_rate: float = 20.0    # 선회율 상한(도/분)
    arrival_radius: float = 0.05   # 웨이포인트 도달 판정 반경(NM)
```

> `has_route`·`active_waypoint`·`remaining_waypoints`·`route_linkage_id`는 모두
> **속성(property)** 이다. 괄호 없이 읽는다.

**쓸 메서드·속성**

| 호출 | 반환 | 뜻 |
|---|---|---|
| `mass.tick(now: float)` | `list[Outbound]` | **시간을 now(초)로 진행**시키고 이번에 보낼 것을 돌려준다. 배가 움직이는 것은 이 호출로만 일어난다 |
| `mass.receive(payload: bytes, source_id: int, now: float)` | `list[Outbound]` | payload 하나를 처리(항로 지시 수신 등). 응답(ACK)이 있으면 반환 |
| `mass.state` | `ShipState` | 현재 위치·침로·속력 |
| `mass.has_route` | `bool` | 항로를 갖고 있나 |
| `mass.active_waypoint` | `Waypoint \| None` | 지금 향하는 웨이포인트 |
| `mass.remaining_waypoints` | `int` | 남은 웨이포인트 수 |
| `mass.route_linkage_id` | `int` | 현재 항로의 상관 ID |
| `mass.rejected_count` | `int` | 거절한 명령 수 |
| `mass.ignored_count` | `int` | 해석 못 해 버린 수신 수 |

> `tick(now)`의 `now`는 **단조 증가하는 초 단위 실수**다. 처음 호출 때는 시간이
> 진행되지 않고 기준점만 잡힌다. 이후 호출부터 (now - 직전 now) 만큼 배가 나아간다.

### ⚠️ 반드시 알아야 할 함정 — 속력은 스스로 설정되지 않는다

`MassAgent`는 **`state.sog`(속력)를 읽기만 하고 스스로 넣지 않는다.** 항로를 받아도
침로만 돌리고 속력은 그대로다. 초기값이 `0.0`이므로 **아무것도 안 하면 배는 제자리에서
방향만 돌린다.** (실제로 확인한 동작이다.)

```python
# ✗ 이렇게만 하면 배가 안 움직인다
mass = MassAgent()
roc.issue_route(...)      # 침로만 바뀌고 위치는 그대로

# ✓ 구동기가 속력을 직접 넣어야 한다
mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0, sog=12.0))
#  또는
mass.state.sog = 12.0
```

웨이포인트의 `planned_sog`(계획 속력)는 **자동 반영되지 않는다.** 원격 조종에서 속도를
바꾸는 것을 보여주려면 구동기가 `mass.state.sog`를 직접 갱신하라. 이건 에이전트 수정이
아니라 **상태 필드에 값을 넣는 것**이므로 제약 1에 어긋나지 않는다.

기본 시연 값으로 `sog = 12.0` 정도를 권한다(위 예시 기준 2분에 약 0.4NM 이동).

### 4.2 RocAgent — 관제센터

```python
from ROC.roc_agent import RocAgent, Track, PendingCommand

@dataclass
class Track:              # 관측 중인 선박 하나
    source_id: int
    report: PositionReport
    updated_at: float
    report_count: int = 1

@dataclass
class PendingCommand:     # 내보낸 명령 하나
    linkage_id: int
    destination_id: int
    issued_at: float
    status: int | None = None      # None이면 아직 응답 없음
    reason: int = 0
    answered_at: float | None = None
    # .answered  -> bool

@dataclass
class RocAgent:
    mmsi: int = 440_000_002
    report_timeout: float = 10.0   # 이 시간 넘게 보고 없으면 두절로 본다
```

**쓸 메서드·속성**

| 호출 | 반환 | 뜻 |
|---|---|---|
| `roc.receive(payload, source_id, now)` | `list[Outbound]` (항상 빈 리스트) | 위치보고·ACK을 받아 내부 상태 갱신 |
| `roc.tick(now)` | `list[Outbound]` | 시간 진행 |
| `roc.issue_route(...)` | `Outbound` | **항로 지시를 만든다** (아래 상세) |
| `roc.cancel_route(destination_id, linkage_id, now)` | `Outbound` | 항로 취소 |
| `roc.tracks` | `dict[int, Track]` | 관측 중인 선박들 |
| `roc.commands` | `dict[int, PendingCommand]` | 내보낸 명령들 (키 = linkage_id) |
| `roc.link_lost` | `list[int]` | 두절 판정된 선박. **속성이다 — 괄호 없이** |
| `roc.unanswered` | `list[PendingCommand]` | 응답 없는 명령. **속성이다 — 괄호 없이** |

**`issue_route` 시그니처 (그대로)**

```python
roc.issue_route(
    destination_id: int,                          # 받을 배의 MMSI
    waypoints_degrees: list[tuple[float, float]], # [(위도, 경도), ...] ← 순서 주의
    now: float = 0.0,
    route_type: int = 1,             # 1=Mandatory 2=Recommended 31=Cancellation
    duration: int = 262_143,         # 262143 = N/A
    heading_setpoint: float | None = None,  # 도. None이면 N/A → 노선(005)
    fallback_state: int = 0,         # 0=None 1=Anchoring 2=DP 3=Drifting
) -> Outbound
```

> **핵심**: `heading_setpoint`와 `fallback_state`를 **비우면 노선 지시(SC-VDE-005)**,
> **채우면 원격 조종(SC-VDE-006)** 이다. 같은 메시지이고 이 두 필드로만 갈린다.
> GUI에서도 이 구분을 그대로 노출하면 된다.

> ⚠️ `waypoints_degrees`는 **(위도, 경도)** 순서다. 다른 곳은 (경도, 위도)인 함수도
> 있으니 헷갈리지 말 것.

### 4.3 Outbound — 송신 요청 (에이전트가 돌려주는 것)

```python
@dataclass
class Outbound:
    payload: bytes            # 이미 인코딩된 바이트
    destination_id: int = 0   # 0 = broadcast
    ack_required: bool = False
    data_type: str | None = None
    content_type: str | None = None
    label: str = ""           # 사람이 읽을 이름. 예: "Route Instruction"
```

### 4.4 두 에이전트를 잇는 법 — 이것이 전부다

```python
# ROC가 만든 것을 MASS에게 전달
for out in roc_outbounds:
    replies = mass.receive(out.payload, source_id=roc.mmsi, now=now)
    # replies(ACK 등)는 다시 ROC에게
    for reply in replies:
        roc.receive(reply.payload, source_id=mass.mmsi, now=now)

# MASS가 주기적으로 내보내는 위치보고를 ROC에게
for out in mass.tick(now):
    roc.receive(out.payload, source_id=mass.mmsi, now=now)
```

> 이번 GUI에서는 **무선 계층을 거치지 않는다.** payload를 그냥 직접 넘기면 된다.
> (실제 VDE-TER 전송은 별도 loopback 데모가 이미 검증해 뒀다. GUI는 행위자 동작을
> 보여주는 것이 목적이다.)

### 4.5 표시에 쓸 상수·헬퍼

```python
from common.messages import route_instruction as route
from common.messages import application_ack as ack
from common.messages import position_report as pos

# 항로 종류
route.ROUTE_TYPE_MANDATORY      # 1
route.ROUTE_TYPE_RECOMMENDED    # 2
route.ROUTE_TYPE_CANCELLATION   # 31
route.ROUTE_TYPE_NAMES          # {1: "Mandatory", ...} 표시용 dict

# 복귀 모드 (원격 조종)
route.FALLBACK_NONE                 # 0
route.FALLBACK_ANCHORING            # 1
route.FALLBACK_DYNAMIC_POSITIONING  # 2
route.FALLBACK_DRIFTING             # 3
route.FALLBACK_NAMES                # 표시용 dict

# ACK 상태
ack.STATUS_ACKNOWLEDGED   # 4  정상 수신·검증
ack.STATUS_ERRORS         # 7
ack.STATUS_INCOMPLETE     # 8
ack.STATUS_ROUTE_ISSUES   # 9  해석되나 경로 문제
ack.STATUS_NAMES          # 표시용 dict
ack.REASON_NAMES          # 표시용 dict

# 자율운항 모드 (상태 불일치 실증에 중요)
pos.AUTONOMY_RC   # 1  원격제어 — 제어권이 ROC에 있다
pos.AUTONOMY_MON  # 2  감독 자율 — 세션은 있고 제어권은 본선
pos.AUTONOMY_AUT  # 3  독립 자율 — 세션 없음
pos.AUTONOMY_MODE_NAMES

# Waypoint 읽기
wp.latitude_degrees    # float | None
wp.longitude_degrees   # float | None

# PositionReport 읽기 (ROC가 tracks에서 꺼낸 것)
report.latitude_degrees, report.longitude_degrees
report.sog_knots, report.cog_degrees
report.autonomy_mode
```

### 4.6 기본값

```python
MASS_MMSI = 440_000_001
ROC_MMSI  = 440_000_002
POSITION_REPORT_INTERVAL_SEC = 2.0
POSITION_REPORT_TIMEOUT_SEC  = 10.0
```

---

## 5. 재사용할 것 — 항적 그리기

`common/observe/report.py`에 **위경도를 화면 좌표로 옮기는 코드가 이미 있다.**
이걸 새로 만들지 말고 같은 방식을 쓰라. (파일을 받아서 참고할 것 — §9)

핵심은 이렇다. 경도는 위도가 높을수록 좁아지므로 `cos(위도)` 보정을 해야 항적이
찌그러지지 않는다.

```python
mean_lat = math.radians((lat_min + lat_max) / 2)
span_y = max(lat_max - lat_min, 1e-9)
span_x = max((lon_max - lon_min) * math.cos(mean_lat), 1e-9)
scale  = min(usable_w / span_x, usable_h / span_y)   # 가로세로 비 유지

x = left + (lon - lon_min) * math.cos(mean_lat) * scale
y = top  + (lat_max - lat) * scale    # SVG는 y가 아래로 커지므로 북쪽이 위가 되게 뒤집는다
```

**GUI에서는 이 계산을 자바스크립트로 옮겨서** 브라우저가 매 프레임 다시 그리게 하는
편이 낫다(서버가 SVG 문자열을 만들어 보내는 것보다 가볍다). 서버는 **위경도 숫자만**
JSON으로 주고, 그리기는 브라우저가 한다.

---

## 6. 화면 요구사항

두 화면은 **같은 디자인 언어**를 쓰되 내용이 다르다.

### 6.1 ROC 콘솔 (`/roc`) — 육상 관제

| 영역 | 내용 |
|---|---|
| 항적 해도 | 계획 항로(점선 + WP 표시) + 실제 관측 항적(실선). 현재 위치 강조 |
| 추적 목록 | 선박별: MMSI, 최종 수신 시각, 보고 횟수, **두절 여부** |
| **명령 발행** | 아래 상세 |
| 명령 이력 | linkage_id, 보낸 시각, 상태(대기중/Acknowledged/거절+사유) |

**명령 발행 조작부** — 이게 GUI의 핵심이다.

- **노선 지시(005)**: 웨이포인트 목록(해도 클릭으로 찍거나 입력), 항로 종류 선택 → 발행
- **원격 조종(006)**: 목표 침로(0–359.9°) + 복귀 모드 선택 → 발행
- **항로 취소**: 현재 linkage_id로 취소 발행

### 6.2 MASS 콘솔 (`/mass`) — 본선

| 영역 | 내용 |
|---|---|
| 항적 해도 | 본선 시점. 현재 위치·침로 방향 표시 |
| 현재 상태 | 위도·경도, 침로, 속력, **자율운항 모드(RC/MON/AUT)** |
| 항로 상태 | 남은 웨이포인트 수, 다음 웨이포인트 좌표, 상관 ID |
| 수신 로그 | 받은 명령과 **검증 결과**(수락/거절+사유). 거절 건은 눈에 띄게 |
| 카운터 | 거절 수, 해석 실패 수 |

### 6.3 디자인 지침

- 해상·항해 계기 느낌. 어두운 배경도 좋고 해도(nautical chart) 느낌도 좋다.
- **상태가 색으로 읽혀야 한다** — 정상/대기/거절/두절이 한눈에 구분될 것.
- 숫자(좌표·침로·속력)는 고정폭 글꼴 + 자릿수 정렬.
- 두 창을 나란히 놓고 보는 것이 기본 사용법이다. 창 폭이 좁아도 깨지지 말 것.
- 애니메이션은 절제. 배 위치가 부드럽게 이어지는 정도면 충분하다.

---

## 7. 서버 설계 지침

### 7.1 구조

```
메인 스레드           : http.server 로 요청 처리
시뮬레이션 스레드 1개  : 일정 간격으로 tick() 돌리고 상태 갱신
공유 상태            : threading.Lock 으로 보호
```

시뮬레이션 루프는 대략:

```python
while running:
    now = time.monotonic() - start
    with lock:
        for out in mass.tick(now):
            roc.receive(out.payload, source_id=mass.mmsi, now=now)
        roc.tick(now)
        # 발행 대기 중인 명령이 있으면 여기서 MASS에게 전달
        drain_pending_commands(now)
        snapshot = build_snapshot(now)   # JSON으로 낼 dict
    time.sleep(0.1)   # 10Hz
```

### 7.2 엔드포인트

| 경로 | 용도 |
|---|---|
| `GET /roc`, `GET /mass` | HTML 페이지 |
| `GET /static/...` | css·js |
| `GET /api/state` | 현재 스냅샷 JSON (두 화면이 같이 씀) |
| `POST /api/command` | ROC 명령 발행 (JSON 본문) |
| `POST /api/reset` | 시뮬레이션 초기화 |

**`/api/state` 응답 예시** (이 모양을 맞춰라)

```json
{
  "t": 42.3,
  "mass": {
    "mmsi": 440000001,
    "lat": 35.1024, "lon": 129.0761,
    "heading": 47.2, "sog": 12.5,
    "autonomy_mode": 1, "autonomy_name": "RC",
    "has_route": true,
    "remaining_waypoints": 2,
    "route_linkage_id": 7,
    "next_waypoint": {"lat": 35.208, "lon": 129.333},
    "rejected_count": 0, "ignored_count": 0,
    "track": [[35.100, 129.050], [35.101, 129.055]]
  },
  "roc": {
    "mmsi": 440000002,
    "tracks": [
      {"source_id": 440000001, "updated_at": 41.8, "report_count": 21,
       "lat": 35.1024, "lon": 129.0761, "sog": 12.5, "cog": 47.2,
       "lost": false}
    ],
    "commands": [
      {"linkage_id": 7, "issued_at": 10.2, "answered": true,
       "status": 4, "status_name": "Acknowledged", "reason_name": "None"}
    ],
    "planned_route": [[35.102, 129.076], [35.208, 129.333]]
  },
  "log": [
    {"t": 10.2, "from": "ROC", "to": "MASS", "kind": "send",
     "text": "항로 지시 (웨이포인트 2개)"},
    {"t": 10.3, "from": "MASS", "to": "ROC", "kind": "ack",
     "text": "Acknowledged"}
  ]
}
```

- `track`은 배가 지나온 위경도 목록(항적 그리기용). 서버가 누적해서 갖고 있다가
  준다. 너무 길어지면 앞을 잘라라(예: 최근 600점).
- `log`도 서버가 누적. 최근 200건 정도만.

**`POST /api/command` 요청 본문**

```json
{ "kind": "route",
  "waypoints": [[35.102, 129.076], [35.208, 129.333]],
  "route_type": 1 }

{ "kind": "control", "heading": 92.5, "fallback": 1 }

{ "kind": "cancel" }
```

서버는 이걸 받아 `roc.issue_route(...)` / `roc.cancel_route(...)`를 부르고, 나온
`Outbound`를 다음 루프에서 MASS에게 전달한다.

### 7.3 주의

- `http.server`의 기본 핸들러는 요청마다 스레드를 안 쓴다. `ThreadingHTTPServer`를 써라.
- 브라우저 두 창이 동시에 폴링하므로 **공유 상태는 반드시 락으로 감싼다.**
- 스냅샷은 미리 만들어 두고 요청 시엔 직렬화만 하는 편이 낫다.
- 종료(Ctrl+C) 시 스레드가 깔끔히 끝나게 할 것.

---

## 8. 나중에 얹을 자리 — 지금은 비워두되 구조만 마련

이 프로젝트의 다음 작업은 **공격 실증**이다. "위조 송신국이 가짜 항로를 주입하면 배가
항로를 벗어난다"를 보여주는 것. 지금 만들지는 않되, **나중에 버튼 하나 추가로 되도록**
구조를 열어 두라.

구체적으로: 명령을 MASS에게 넣는 지점을 함수 하나로 모아 두고, `source_id`를 인자로
받게 하라. 그러면 나중에 `source_id=위조국_MMSI`로 같은 경로를 타면 끝이다.

```python
def deliver_to_mass(outbound, source_id, now):   # ← 이런 모양으로 모아 둘 것
    ...
```

---

## 9. 받아야 할 파일

작업 전에 **아래 파일들을 요청해서 받으라.** 이것만 있으면 된다.

| 파일 | 왜 필요한가 |
|---|---|
| `MASS/mass_agent.py` | 배 행위자 (읽기 전용 — 인터페이스 확인용) |
| `ROC/roc_agent.py` | 관제 행위자 (읽기 전용) |
| `common/observe/report.py` | **항적 좌표 변환·SVG 그리기 참고** |
| `common/messages/route_instruction.py` | 항로·조종 상수와 Waypoint |
| `common/messages/position_report.py` | 위치보고 필드 |
| `common/messages/application_ack.py` | ACK 상태·사유 상수 |
| `common/outbound.py` | Outbound 정의 |
| `common/config.py` | 기본 MMSI·주기 |
| `apps/scenario_runner.py` | 기존 시나리오가 에이전트를 어떻게 부르는지 참고 |

> 나머지(codec·VDE-TER 링크 계층·인증서)는 **이번 작업에 필요 없다.** 받지 않아도 된다.

---

## 10. 완료 기준

- [ ] `python3 apps/console_server.py` 한 줄로 뜨고, 접속 주소를 출력한다
- [ ] 외부 패키지 설치가 전혀 필요 없다 (`pip install` 불필요)
- [ ] `/roc`과 `/mass`를 두 창에 띄우면 **같은 시뮬레이션의 서로 다른 관점**이 보인다
- [ ] ROC에서 노선을 발행하면 → MASS 화면에 수신 로그가 뜨고 → **배가 그 항로로 움직인다**
      (§4.1 함정 참고 — `state.sog`를 넣지 않으면 제자리에서 돌기만 한다)
- [ ] ROC 명령 이력에 ACK 결과(Acknowledged 등)가 돌아와 표시된다
- [ ] 원격 조종(침로+복귀모드) 발행이 노선 발행과 구분되어 동작한다
- [ ] 항적이 해도 위에 누적되어 그려진다 (찌그러지지 않게 cos 보정)
- [ ] 두절(10초 이상 보고 없음) 상태가 ROC 화면에 표시된다
- [ ] `MASS/`, `ROC/`, `common/` 의 기존 파일이 **하나도 수정되지 않았다**
- [ ] Ctrl+C로 깔끔히 종료된다

---

## 11. 배경 (읽어두면 좋지만 필수는 아님)

- **MASS** = Maritime Autonomous Surface Ship. 선원이 안 탄 자율운항선박.
- **ROC** = Remote Operation Centre. 육상에서 배를 감독·조종하는 관제센터.
- 이 프로젝트는 **해상 무선 보안 연구**다. 배와 육지를 잇는 VDE-TER 무선 표준에
  송신자 인증이 없어서, 누구나 ROC인 척 위조 명령을 보낼 수 있다는 문제를 다룬다.
- 그래서 **자율운항 모드(RC/MON/AUT)** 가 중요하다. "배는 ROC가 조종 중이라 믿는데
  ROC는 아니라고 믿는" 상태 불일치가 가장 위험한 시나리오이고, 두 화면을 나란히
  놓았을 때 그것이 눈에 보이는 것이 이 GUI의 가장 큰 값이다.
- 지금 만드는 것은 **연구 시연 도구**이지 실제 선박 제어 장비가 아니다.
