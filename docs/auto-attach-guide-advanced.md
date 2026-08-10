
## 동작 원리

등록된 작업은 현재 사용자가 로그인할 때 Windows PowerShell 5.1로 시작된다.

1. 시작 직후 장치를 한 번 검사한다.
2. `Win32_DeviceChangeEvent`의 장치 도착 이벤트를 기다린다.
3. 이벤트가 오면 실행 중인 배포판 목록을 확인한다.
4. `usbipd state` JSON에서 지정한 VID:PID와 일치하면서 다음 조건을 모두 만족하는 장치를 찾는다.
   - 현재 BUSID가 있다.
   - `PersistedGuid`가 있어 이미 bind되어 있다.
   - `ClientIPAddress`가 없어 아직 attach되지 않았다.
5. 찾은 장치마다 **그 시점의 BUSID**로 `usbipd attach --wsl`을 실행한다.

B210은 초기 `WestBridge` 상태에서 펌웨어 로드 후 `USRP B200`으로 재열거될 수 있다. 이때 장치 인스턴스와 BUSID가 달라질 수 있으므로 BUSID를 설정에 저장하면 안 된다. 이 감시 작업은 매번 현재 상태를 다시 읽어 재열거와 USB 포트 변경을 처리한다. 같은 VID:PID의 공유 장치가 여러 개면 조건을 만족하는 장치를 모두 처리한다.

WSL 2의 USB 연결은 WSL 가상 머신 단위로 이뤄진다. `usbipd attach` 자체에는 배포판 이름을 전달하지 않는다. 이 문서의 `$DistroName`은 의도한 배포판이 이미 실행 중인지 확인하는 데만 쓰인다.


### 정상 작동 체크리스트

- [ ] WSL이 실행 중이고, 시작 시점에 이미 연결되어 있던 공유 B210이 작업 시작 후 약 3초 안에 attach된다.
- [ ] 장치를 다른 USB 포트에 꽂아 BUSID가 바뀌어도 새 BUSID로 attach된다.
- [ ] WSL에서 `lsusb -d 2500:0020`으로 장치가 보인다.
- [ ] 두 개 이상의 USB 포트에서 각각 시험하고, `WestBridge`에서 `USRP B200`으로 재열거된 뒤에도 처리된다.
- [ ] WSL을 완전히 종료한 상태에서는 장치를 꽂아도 WSL이 시작되지 않는다.
- [ ] 다른 VID:PID 장치와 아직 bind되지 않은 대상 VID:PID 장치는 무시된다.
- [ ] 같은 VID:PID로 미리 bind된 장치를 여러 개 연결하면 모두 attach된다.
- [ ] 로그아웃 후 다시 로그인하면 작업이 `Running` 상태로 복구된다.

시작 직후 1회 검사는 즉시 실행되지만, 프로세스 시작과 `usbipd` 응답 시간은 PC마다 다르다. 3초 기준을 넘으면 아래 문제 해결 절차로 작업 상태와 수동 명령을 먼저 확인한다.



### 문제 해결

| 증상/확인 결과 | 원인 | 조치 |
| --- | --- | --- |
| 대상 장치의 `PersistedGuid`가 없거나 비어 있음 | 아직 bind되지 않음 | 관리자 PowerShell에서 현재 인스턴스의 BUSID로 `usbipd bind --busid …`를 한 번 실행한다. 재열거된 인스턴스도 확인한다. |
| 대상 장치의 `BusId`가 없음 | 장치가 현재 물리적으로 연결되지 않았거나 상태 JSON이 예상과 다름 | 케이블, 전원, USB 포트를 확인하고 `usbipd list`, `usbipd state`를 다시 본다. BUSID를 코드에 고정하지 않는다. |
| 작업은 `Running`인데 attach되지 않음 | `$DistroName` 불일치 또는 대상 배포판 중지 | `wsl --list --verbose`와 `wsl --list --running --quiet`의 정확한 이름을 비교한다. 사용자가 배포판을 시작한 뒤 장치를 다시 꽂거나 작업을 재시작한다. |
| WSL이 저절로 시작되지 않음 | 의도한 동작 | 이 자동화는 WSL 자동 시작을 하지 않는다. 먼저 배포판을 직접 시작한다. |
| `A firewall appears to be blocking the connection` 경고 | 방화벽/VPN/보안 정책이 TCP 3240을 차단 | usbipd-win 설치가 만든 방화벽 규칙과 네트워크 프로필을 확인한다. VPN을 분리 시험하고 조직 관리자에게 TCP 3240 정책을 문의한다. |
| `usbipd state` 변환 실패 또는 필드가 없음 | usbipd-win 버전별 명령/JSON 차이 | `usbipd --version`, `usbipd --help`, `usbipd attach --help`, 원본 `usbipd state`를 확인한다. 가능하면 5.x로 업데이트하고, 실제 필드에 맞춰 감시 코드의 필터를 조정한다. |
| `usbipd attach`가 알 수 없는 옵션을 보고함 | 구버전 문법 사용 | 이 문서는 5.x 문법이다. 설치 버전의 도움말과 [`usbipd-win` 문서](https://github.com/dorssel/usbipd-win)를 우선한다. 오래된 예제의 `usbipd wsl attach`와 혼용하지 않는다. |
| 한 번은 붙지만 재열거 후 사라짐 | `WestBridge`와 `USRP B200`이 서로 다른 장치 인스턴스로 등록됨 | 두 상태가 나타날 때 각각 `usbipd list/state`로 확인하고 관리자 권한으로 최초 한 번씩 bind한다. 실제 VID:PID가 달라진다면 이 단일 VID:PID 필터의 범위 밖이므로 값을 재검토한다. |
| 하나만 attach됨 | 다른 장치가 미공유, 이미 다른 클라이언트에 연결됨, 또는 VID:PID가 다름 | 각 장치의 `InstanceId`, `PersistedGuid`, `ClientIPAddress`, `BusId`를 `usbipd state`에서 비교한다. |
| 작업이 즉시 `Ready`로 돌아가거나 반복 실패 | PowerShell/WMI/작업 정책 문제 또는 내장 명령 오류 | `Get-ScheduledTaskInfo`를 확인하고, 작업 스케줄러의 Microsoft-Windows-TaskScheduler/Operational 로그를 활성화해 오류를 본다. 조직의 WMI 및 예약 작업 정책도 확인한다. |
| Windows에서는 `Attached`지만 WSL 일반 사용자로 접근 불가 | Linux udev 권한 문제 | WSL의 `lsusb`로 열거를 먼저 확인한 뒤 UHD/배포판에 맞는 udev 규칙을 별도로 설치한다. |

수동으로 자동화 경계를 분리해서 시험할 때는 다음 순서를 사용한다.

```powershell
# 일반 PowerShell: WSL은 사용자가 미리 실행해 둔다.
wsl --list --running --quiet
usbipd state
usbipd attach --wsl --busid <usbipd-state에서-확인한-BUSID>
```

수동 attach도 실패하면 예약 작업보다 WSL, usbipd-win, bind 상태, 방화벽을 먼저 해결해야 한다.



### 범위 밖

이 작업은 다음 기능을 수행하지 않는다.

- 새 장치의 자동 bind 또는 usbipd-win 정책 생성
- WSL 또는 특정 배포판 자동 시작
- UHD 설치, FPGA/펌웨어 배포, Linux udev 규칙 설정
- VID:PID가 서로 다른 임의 USB 장치의 범용 감시
