# MASS-ROC 조작 데이터 및 ACK 메시지 형식

이 문서는 ROC(Remote Operation Centre)가 MASS(Maritime Autonomous Surface Ship)에 전송하는 조작 데이터와, MASS가 해당 데이터를 수신·검증한 뒤 ROC에 반환하는 ACK 데이터의 권장 논리 형식을 정의한다.

여기서 제시하는 JSON은 구현과 시험을 위한 **응용 계층 논리 데이터 모델**이다. S-421의 필드명과 열거값을 최대한 재사용하지만, 그 자체가 IEC 63173-1에서 규정한 S-421 GML 문서와 동일한 직렬화 형식은 아니다.

## 1. 메시지 형식 구성 근거

### 1.1 적용 자료

| 근거 | 메시지 형식에 반영한 내용 |
|---|---|
| [IEC 63173-1:2021 / S-421 Edition 1.0.0](https://webstore.iec.ch/en/publication/32931) | 경로 식별자와 판번호, 경로 상태, 대상 선박 MMSI, 웨이포인트, leg의 기하 유형, 웨이포인트별 계획 대지속력(SOG) 등의 필드명과 열거값을 사용한다. S-421은 경로계획 교환 형식을 규정하며 일반적인 원격조종 명령 전체를 규정하지는 않는다. |
| [IHO S-421 등록정보](https://registry.iho.int/productspec/view.do?idx=185%26product_ID%3DS-421%26statusS%3D5%26domainS%3DALL%26category%3Dproduct_ID%26searchValue%3D) | 본 문서가 사용하는 기준 버전을 출판된 S-421 Edition 1.0.0으로 고정한다. 이 버전은 S-100 Edition 4.0.0을 기반으로 한다. |
| Sumon, Kim, and Rokseth (2025), [*Hazard analysis of autonomous vessel operation during the interaction and execution between remote operation centre controller and onboard controllers*](https://doi.org/10.1186/s41072-025-00214-2) | ROC의 항해계획 업로드·갱신과 운용 명령, 속도 및 heading setpoint, fallback 개시를 조작 데이터의 근거로 사용한다. 논문에서 언급한 fallback 상태인 Anchoring, DP, vessel drifting을 값으로 사용한다. |
| 사용자 지정 요구사항 | 위치는 WGS84 타원체 기반 지리좌표계와 DMM 형식을 사용한다. 모든 메시지에 송신자와 수신자의 MMSI를 넣는다. 근거 없는 사용자 정의 필드와 `security` 필드는 제외한다. |

### 1.2 필드 출처 구분

- **S-421 기반 필드:** `routeFormatVersion`, `routeID`, `routeEditionNo`, `routeInfo`, `routeWaypointID`, `routeWaypointTurnRadius`, `routeWaypointLegGeometryType`, `routeScheduleElementPlanSOG` 등이다.
- **논문 기반 제안 필드:** `headingSetPoint`과 `fallbackState`이다. 논문은 이 제어 개념을 제시하지만, 필드명·자료형·직렬화 방식까지 표준화하지는 않는다.
- **사용자 요구 필드:** `sourceMMSI`, `destinationMMSI`와 DMM 좌표의 구성요소이다. S-421의 `routeInfoVesselMMSI`만으로는 ROC와 MASS 양쪽을 모두 식별할 수 없으므로 메시지 외부에 송신자·수신자 MMSI를 둔다.

### 1.3 좌표 표현

논리 JSON에서는 위치를 다음과 같은 DMM 구조로 표현한다.

- 위도: 도 `0-90`, 분 `0 이상 60 미만`, 반구 `N` 또는 `S`
- 경도: 도 `0-180`, 분 `0 이상 60 미만`, 반구 `E` 또는 `W`
- 좌표계: `WGS84`
- 형식: `DMM`

S-421 GML 데이터로 직렬화할 때에는 DMM을 십진도(DD)로 변환해야 한다.

```text
DD = degrees + minutes / 60
S 또는 W 반구이면 DD에 음수 부호를 적용한다.
```

### 1.4 주요 열거값

`routeInfoStatus`는 S-421 Edition 1.0.0의 숫자 코드를 사용한다.

| 코드 | 명칭 | 본 형식에서의 용도 |
|---:|---|---|
| `3` | `Recommended` | ROC가 MASS에 보내는 권고 경로 기반 조작 데이터 |
| `4` | `Acknowledged` | MASS가 경로 정보를 정상 수신·검증했다는 응용 계층 ACK |
| `7` | `Errors` | 스키마 또는 응용 수준 검증 오류 |
| `8` | `Incomplete` | 처리에 필요한 필드 누락 |
| `9` | `Route issues` | 데이터는 해석되지만 경로상 문제가 발견됨 |

`routeWaypointLegGeometryType`은 다음 값을 사용한다.

| 코드 | S-421 Edition 1.0.0 명칭 | 의미 |
|---:|---|---|
| `1` | `Lexodrome` | 등각항로 또는 rhumb line |
| `2` | `Orthodrome` | 대권항로 또는 great-circle route |

`fallbackState`는 S-421 열거값이 아니다. 논문에 직접 제시된 다음 문자열만 사용한다.

| 값 | 의미 |
|---|---|
| `Anchoring` | 투묘를 통한 선박 이동 제한 |
| `DynamicPositioning` | DP 시스템을 이용한 위치 유지 |
| `Drifting` | 적극적인 위치 유지 없이 표류 상태로 전환 |

Fallback을 지시하지 않는 메시지에는 임의의 `None` 또는 `Normal` 값을 만들지 않고 `fallbackState` 필드 자체를 생략한다.

## 2. 조작 데이터 예시

다음 예시는 MMSI `004400001`인 ROC가 MMSI `440123456`인 MASS에 WGS84/DMM 기반 웨이포인트, 계획 SOG, heading setpoint 및 DP fallback을 전달하는 경우이다. MMSI와 좌표는 형식을 설명하기 위한 예시값이다.

```json
{
  "sourceMMSI": "004400001",
  "destinationMMSI": "440123456",
  "routeFormatVersion": "1.0",
  "routeID": "ROC-440123456-0001",
  "routeEditionNo": 1,
  "routeInfo": {
    "routeInfoName": "ROC maneuver route 0001",
    "routeInfoAuthor": "ROC",
    "routeInfoEditionTime": "2026-08-25T02:00:00Z",
    "routeInfoStatus": 3,
    "routeInfoValidityStart": "2026-08-25T02:00:00Z",
    "routeInfoValidityEnd": "2026-08-25T02:30:00Z",
    "routeInfoVesselMMSI": "440123456"
  },
  "headingSetPoint": 75.0,
  "routeWaypoints": [
    {
      "routeWaypointID": 1,
      "position": {
        "coordinateReferenceSystem": "WGS84",
        "format": "DMM",
        "latitude": {
          "degrees": 35,
          "minutes": 6.5000,
          "hemisphere": "N"
        },
        "longitude": {
          "degrees": 129,
          "minutes": 2.5000,
          "hemisphere": "E"
        }
      },
      "routeWaypointTurnRadius": 0.3
    },
    {
      "routeWaypointID": 2,
      "position": {
        "coordinateReferenceSystem": "WGS84",
        "format": "DMM",
        "latitude": {
          "degrees": 35,
          "minutes": 7.2500,
          "hemisphere": "N"
        },
        "longitude": {
          "degrees": 129,
          "minutes": 5.0000,
          "hemisphere": "E"
        }
      },
      "routeWaypointTurnRadius": 0.3,
      "routeWaypointLeg": {
        "routeWaypointLegGeometryType": 1
      }
    }
  ],
  "routeSchedule": [
    {
      "routeWaypointID": 1,
      "routeScheduleElementPlanSOG": 10.0
    },
    {
      "routeWaypointID": 2,
      "routeScheduleElementPlanSOG": 10.0
    }
  ],
  "fallbackState": "DynamicPositioning"
}
```

`routeWaypointLeg`는 S-421 구조에 맞춰 **직전 웨이포인트에서 현재 웨이포인트까지의 leg**를 나타낸다. 따라서 첫 번째 웨이포인트에는 없고 두 번째 이후 웨이포인트에 나타난다.

## 3. 조작 데이터 필드별 설명

표의 필수 여부는 이 문서에서 제안하는 조작 데이터 프로파일 기준이다. `조건부`는 해당 제어 기능을 사용하는 경우에만 포함한다는 뜻이다.

| 필드 경로 | 자료형·허용값 | 필수 여부 | 설명 및 근거 |
|---|---|---|---|
| `sourceMMSI` | 9자리 숫자 문자열 | 필수 | 조작 데이터를 송신한 ROC의 MMSI. 선행 0을 보존하기 위해 문자열로 표현한다. 사용자 요구 필드이다. |
| `destinationMMSI` | 9자리 숫자 문자열 | 필수 | 조작 데이터를 수신할 MASS의 MMSI. `routeInfo.routeInfoVesselMMSI`와 같은 선박을 가리켜야 한다. 사용자 요구 필드이다. |
| `routeFormatVersion` | 문자열, 예: `"1.0"` | 필수 | S-421 경로 형식 버전. 본 문서는 S-421 Edition 1.0.0을 기준으로 한다. |
| `routeID` | 문자열 | 필수 | 경로를 고유하게 식별한다. ACK에서 원래 조작 데이터를 참조하는 상관 식별자로 다시 사용한다. S-421 기반 필드이다. |
| `routeEditionNo` | 양의 정수 | 필수 | 동일한 `routeID`를 가진 경로의 개정 번호. 경로를 수정할 때 증가시키며 ACK에서 동일한 값을 반환한다. S-421 기반 필드이다. |
| `routeInfo` | 객체 | 필수 | S-421 `RouteInfo`에 해당하는 경로 메타데이터 묶음이다. |
| `routeInfo.routeInfoName` | 문자열 | 필수 | 경로 이름. S-421 `RouteInfo`의 필수 속성이다. |
| `routeInfo.routeInfoAuthor` | 문자열 | 권장 | 경로 작성 주체. 예시에서는 `ROC`를 사용하며, 정확한 송신자 식별은 `sourceMMSI`가 담당한다. S-421 기반 필드이다. |
| `routeInfo.routeInfoEditionTime` | ISO 8601 UTC `dateTime` | 권장 | 현재 경로 판본을 작성한 시각. S-421 기반 필드이다. |
| `routeInfo.routeInfoStatus` | 정수 `3` | 필수 | `Recommended`. ROC 경로를 조작 지시로 사용하는 본 프로파일에서 가장 가까운 S-421 상태값이다. S-421 자체가 원격조종 의미를 부여하는 것은 아니다. |
| `routeInfo.routeInfoValidityStart` | ISO 8601 UTC `dateTime` | 권장 | 조작 경로가 유효해지는 시각. S-421 기반 필드이다. |
| `routeInfo.routeInfoValidityEnd` | ISO 8601 UTC `dateTime` | 권장 | 조작 경로의 유효기간 종료 시각. 시작 시각보다 뒤여야 한다. S-421 기반 필드이다. |
| `routeInfo.routeInfoVesselMMSI` | 9자리 숫자 문자열 | 필수 | 해당 경로를 적용할 선박의 MMSI. 조작 데이터에서는 `destinationMMSI`와 같아야 한다. S-421 기반 필드이다. |
| `headingSetPoint` | 실수, `0.0 이상 360.0 미만` | 조건부 | 진북 기준 목표 heading을 도 단위로 나타낸다. 논문의 speed/heading setpoint 제어에 근거하지만 필드명과 인코딩은 본 프로파일의 제안이다. |
| `routeWaypoints` | 객체 배열 | 조건부 | 경로 추종 명령에 포함할 웨이포인트 목록. 둘 이상의 웨이포인트를 순서대로 전달한다. S-421 `RouteWaypoints` 구조를 단순화한 표현이다. |
| `routeWaypoints[].routeWaypointID` | 정수 | `routeWaypoints` 사용 시 필수 | 경로 내 웨이포인트 식별자. 중복되어서는 안 되며 `routeSchedule[].routeWaypointID`의 참조 대상이 된다. S-421 기반 필드이다. |
| `routeWaypoints[].position` | 객체 | `routeWaypoints` 사용 시 필수 | 웨이포인트의 WGS84/DMM 위치 정보이다. S-421의 point geometry를 사용자 지정 DMM 표현으로 나타낸다. |
| `position.coordinateReferenceSystem` | 고정 문자열 `WGS84` | 필수 | WGS84 타원체 기반 지리좌표계. 사용자 지정 요구사항이다. |
| `position.format` | 고정 문자열 `DMM` | 필수 | 도·십진분(Degrees and Decimal Minutes) 좌표 형식. 사용자 지정 요구사항이다. |
| `position.latitude.degrees` | 정수 `0-90` | 필수 | 위도의 도 부분이다. |
| `position.latitude.minutes` | 실수 `0 이상 60 미만` | 필수 | 위도의 십진분 부분이다. |
| `position.latitude.hemisphere` | `N` 또는 `S` | 필수 | 위도 반구이다. |
| `position.longitude.degrees` | 정수 `0-180` | 필수 | 경도의 도 부분이다. |
| `position.longitude.minutes` | 실수 `0 이상 60 미만` | 필수 | 경도의 십진분 부분이다. |
| `position.longitude.hemisphere` | `E` 또는 `W` | 필수 | 경도 반구이다. |
| `routeWaypoints[].routeWaypointTurnRadius` | 0 이상의 실수, 해리(NM) | `routeWaypoints` 사용 시 필수 | 웨이포인트 선회반경. S-421 Edition 1.0.0에서 단위는 nautical mile이다. |
| `routeWaypoints[].routeWaypointLeg` | 객체 | 두 번째 이후 웨이포인트에서 조건부 | 직전 웨이포인트에서 현재 웨이포인트까지의 leg 정보이다. 첫 번째 웨이포인트에는 생략한다. |
| `routeWaypointLeg.routeWaypointLegGeometryType` | 정수 `1` 또는 `2` | `routeWaypointLeg` 사용 시 필수 | `1=Lexodrome`, `2=Orthodrome`. S-421 Edition 1.0.0 열거값이다. |
| `routeSchedule` | 객체 배열 | 속도 지시 시 필수 | 웨이포인트별 계획 속도를 연결한다. S-421 `RouteScheduleElement`를 단순화한 표현이다. |
| `routeSchedule[].routeWaypointID` | 정수 | 필수 | 계획 속도가 적용되는 웨이포인트를 참조한다. |
| `routeSchedule[].routeScheduleElementPlanSOG` | 0 이상의 실수, knot | 필수 | 계획 대지속력(SOG). S-421의 `routeScheduleElementPlanSOG` 필드이다. |
| `fallbackState` | `Anchoring`, `DynamicPositioning`, `Drifting` | 조건부 | ROC가 predefined fallback을 지시할 때 사용한다. 개념과 세 상태는 논문에 근거하지만 필드명과 문자열 인코딩은 본 프로파일의 제안이다. Fallback을 지시하지 않으면 필드를 생략한다. |

조작 데이터에는 근거 없이 만들었던 `messageType`, `commandID`, `sequenceNumber`, `executionMode`, `priority`, `security` 등의 필드를 포함하지 않는다. 메시지 종류의 구분, 재전송 순서 제어, 인증·무결성 보호가 필요하다면 VDE-TER 또는 그 상위 전송·보안 프로파일에서 별도로 정의해야 한다.

## 4. ACK 데이터 예시

다음은 MASS가 위 조작 데이터를 정상 수신하고 응용 계층 검증을 완료한 경우의 ACK이다. 조작 데이터의 송신자·수신자 MMSI를 서로 바꾸고, 동일한 `routeID`와 `routeEditionNo`를 반환한다.

```json
{
  "sourceMMSI": "440123456",
  "destinationMMSI": "004400001",
  "routeFormatVersion": "1.0",
  "routeID": "ROC-440123456-0001",
  "routeEditionNo": 1,
  "routeInfo": {
    "routeInfoName": "ROC maneuver route 0001",
    "routeInfoStatus": 4,
    "routeInfoVesselMMSI": "440123456"
  }
}
```

이 ACK는 **응용 계층에서 S-421 경로 정보의 수신·검증 결과를 알리는 메시지**이다. 무선 프레임 또는 VDE-TER 전송 자체의 성공을 확인하는 링크 계층 ACK와는 역할이 다르며, `routeInfoStatus=4`는 조작 실행 완료까지 의미하지 않는다.

오류 ACK에서는 `routeInfoStatus`를 `7`, `8`, `9` 중 하나로 바꾸고, 필요하면 S-421의 `routeInfoDescription`에 오류나 경로 문제를 설명한다.

```json
{
  "routeInfo": {
    "routeInfoName": "ROC maneuver route 0001",
    "routeInfoStatus": 8,
    "routeInfoDescription": "routeWaypointID 2: position missing",
    "routeInfoVesselMMSI": "440123456"
  }
}
```

## 5. ACK 데이터 필드별 설명

| 필드 경로 | 자료형·허용값 | 필수 여부 | 설명 및 근거 |
|---|---|---|---|
| `sourceMMSI` | 9자리 숫자 문자열 | 필수 | ACK를 송신한 MASS의 MMSI. 원 조작 데이터의 `destinationMMSI`와 같아야 한다. 사용자 요구 필드이다. |
| `destinationMMSI` | 9자리 숫자 문자열 | 필수 | ACK를 수신할 ROC의 MMSI. 원 조작 데이터의 `sourceMMSI`와 같아야 한다. 사용자 요구 필드이다. |
| `routeFormatVersion` | 문자열 `"1.0"` | 필수 | ACK가 참조하는 경로 데이터의 형식 버전이다. S-421 Edition 1.0.0을 사용한다. |
| `routeID` | 문자열 | 필수 | ACK 대상 조작 데이터의 경로 식별자. 원 조작 데이터와 정확히 같아야 한다. S-421 기반 필드이며 별도 사용자 정의 `commandID`를 두지 않고 상관 식별자로 사용한다. |
| `routeEditionNo` | 양의 정수 | 필수 | ACK 대상 경로의 판번호. 동일한 `routeID`의 다른 개정본을 혼동하지 않도록 원 조작 데이터와 같은 값을 반환한다. |
| `routeInfo` | 객체 | 필수 | 수신·검증한 경로의 상태 정보를 담는다. |
| `routeInfo.routeInfoName` | 문자열 | 필수 | 원 조작 데이터의 경로 이름. S-421 `RouteInfo`의 필수 속성이므로 ACK에도 유지한다. |
| `routeInfo.routeInfoStatus` | 정수 `4`, `7`, `8`, `9` | 필수 | `4=Acknowledged`, `7=Errors`, `8=Incomplete`, `9=Route issues`. S-421 Edition 1.0.0의 상태 코드이다. |
| `routeInfo.routeInfoDescription` | 문자열 | 조건부 | `7`, `8`, `9` 상태에서 오류 위치나 경로 문제를 설명한다. 정상 ACK에서는 생략할 수 있는 S-421 기반 필드이다. |
| `routeInfo.routeInfoVesselMMSI` | 9자리 숫자 문자열 | 필수 | 경로의 대상 선박이면서 ACK 송신 선박인 MASS의 MMSI. `sourceMMSI`와 같아야 한다. S-421 기반 필드이다. |

S-421 Edition 1.0.0의 `Acknowledged`는 경로의 수신·확인을 표현하지만, 실제 조타·추진 명령의 시작이나 완료 상태까지 구분하지는 않는다. 실행 시작·완료·거부를 별도로 확인하려면 추가 상태 모델이 필요하지만, 이는 현재 두 근거자료에서 규정되지 않은 확장이므로 본 형식에서는 제외한다.
