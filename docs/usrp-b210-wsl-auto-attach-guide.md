# USRP B210을 WSL 2에 자동으로 연결하기

이 문서는 USRP B210을 Windows에 꽂았을 때, **이미 실행 중인 WSL 2**에 `usbipd-win`으로 자동 attach하는 방법을 설명한다. 별도 `.ps1` 파일은 만들지 않는다. 아래 PowerShell 블록 하나가 감시 코드를 예약 작업 안에 `EncodedCommand`로 저장한다.

기본값은 다음과 같다.

| 항목 | 기본값 |
| --- | --- |
| WSL 배포판 | `Ubuntu-24.04` |
| USB VID:PID | `2500:0020` |
| 예약 작업 이름 | `Usbipd-USRP-AutoAttach` |
| 실행 계정 | 등록 명령을 실행한 현재 Windows 사용자 |

> [!IMPORTANT]
> 이 자동화는 장치를 **bind(공유)하지 않고**, WSL을 **시작하지도 않는다**. 관리자가 장치를 미리 bind해야 하며, 지정한 WSL 배포판이 실행 중일 때만 이미 공유된 장치를 attach한다.

이 가이드의 명령과 `usbipd state` 필드 사용은 `usbipd-win 5.x` 문법을 기준으로 작성했으며 `5.3.0`을 기준 버전으로 검토했다. 업그레이드에 따라 명령이나 JSON 필드가 바뀔 수 있으므로 실행 전에 반드시 `usbipd --help`, `usbipd attach --help`, `usbipd state`를 확인한다.

## PC마다 확인하거나 바꿀 값

| 항목 | 확인 방법과 영향 |
| --- | --- |
| `$DistroName` | `wsl --list --verbose`에 표시되는 이름과 대소문자까지 맞춘다. 기본값은 `Ubuntu-24.04`이다. |
| `$Vid`, `$TargetPid` | `usbipd list` 또는 `usbipd state`에서 B210의 실제 VID:PID를 확인한다. 기본값은 `2500:0020`이다. 16진수 네 자리로 쓰며 `0x`를 붙이지 않는다. PowerShell의 `$PID`는 읽기 전용 프로세스 ID이므로 PID 설정에는 `$TargetPid`를 사용한다. |
| `$TaskName` | 기존 작업과 겹치지 않게 정한다. 기본값은 `Usbipd-USRP-AutoAttach`이다. 같은 이름이 있으면 이 가이드의 설정으로 대체된다. |
| Windows 사용자/도메인 | 작업은 등록 블록을 실행한 `도메인\\사용자` 또는 `컴퓨터\\사용자`의 대화형 로그온으로 등록된다. 다른 계정의 WSL에는 적용되지 않는다. |
| WSL 버전 | 대상 배포판의 `VERSION`이 반드시 `2`여야 한다. |
| Windows/CPU | 공식 요구사항인 지원 Windows 빌드와 x64 또는 ARM64 여부를 확인한다. x86은 지원 대상이 아니다. |
| WSL/커널 | Store 배포 WSL과 USB/IP 지원 커널을 최신으로 유지한다. 공식 문서는 Linux 커널 `5.10.60.1` 이상을 요구한다. |
| usbipd-win 버전 | 이 코드는 5.x의 `attach --wsl` 구문과 `state` JSON의 `Devices`, `BusId`, `PersistedGuid`, `ClientIPAddress`, `InstanceId` 필드를 사용한다. 버전이 다르면 도움말과 실제 JSON을 우선한다. |
| USB 포트/BUSID | 포트를 바꾸거나 재열거하면 BUSID가 달라질 수 있다. 고정값으로 입력하지 않는다. |
| 네트워크 정책 | WSL과 Windows 호스트 사이의 USB/IP 통신에 TCP 3240이 사용된다. 방화벽, VPN, 보안 에이전트 정책을 확인한다. |
| 그룹 정책 | WMI 이벤트 구독이나 사용자 예약 작업 생성/실행이 그룹 정책으로 막힐 수 있다. 이 경우 관리자에게 확인한다. |
| PowerShell 버전 | 감시 작업은 `Register-WmiEvent`가 기본 제공되는 **Windows PowerShell 5.1**을 명시적으로 실행한다. PowerShell 7에서는 이 cmdlet이 제공되지 않거나 호환 모듈 동작이 달라질 수 있다. |

## 1. 사전 준비

### 1.1 버전과 상태 확인

일반 PowerShell에서 다음을 실행한다.

```powershell
wsl --list --verbose
wsl --version
usbipd --version
usbipd --help
usbipd attach --help
usbipd state
```

확인할 내용은 다음과 같다.

- `Ubuntu-24.04`처럼 실제로 사용할 **배포판의 정확한 이름**
- 해당 배포판의 `VERSION`이 `2`인지 여부
- `usbipd-win`이 5.x인지 여부(이 문서의 검토 버전은 5.3.0)
- `usbipd state`가 JSON을 반환하며 위 표의 필드를 포함하는지 여부

WSL 또는 커널이 오래되었다면 관리자 PowerShell에서 업데이트한다. `wsl --install`은 아직 WSL이 설치되지 않은 PC에서만 필요하다.

```powershell
wsl --install
wsl --update
```

이미 설치된 배포판을 WSL 2로 전환해야 할 때는 정확한 이름으로 다음을 실행한다.

```powershell
wsl --set-version Ubuntu-24.04 2
```

`usbipd` 명령이 없다면 공식 [`usbipd-win` 저장소](https://github.com/dorssel/usbipd-win)의 최신 MSI 또는 공식 설치 방법을 사용한다. **usbipd-win 설치와 서비스/드라이버 설정에는 관리자 승인이 필요하다.** 전체 요구사항과 수동 연결 절차는 Microsoft의 [WSL에서 USB 장치 연결](https://learn.microsoft.com/windows/wsl/connect-usb) 문서를 참고한다.

### 1.2 B210 장치 인스턴스를 bind

이 단계만 **관리자 권한 PowerShell**에서 실행한다.

1. B210을 연결하고 장치와 현재 BUSID를 찾는다.

   ```powershell
   usbipd list
   usbipd state
   ```

2. 출력에서 확인한 BUSID를 사용해 공유한다. 아래 `4-4`는 예시일 뿐이며 그대로 복사하지 않는다.

   ```powershell
   usbipd bind --busid 4-4
   usbipd list
   ```

3. B210이 `WestBridge`에서 `USRP B200`으로 재열거된 뒤 Windows가 이를 별도 장치 인스턴스로 취급하면, 새로 나타난 BUSID도 같은 방법으로 **최초 한 번** bind한다.

   ```powershell
   usbipd list
   usbipd bind --busid <재열거된-실제-BUSID>
   ```

각 상태가 `Shared`인지 확인한다. 동일 VID:PID라도 장치 인스턴스별 공유 상태가 다를 수 있다. 재열거 단계가 너무 빨라 bind할 기회가 없으면 먼저 수동 attach하여 상태를 전환한 뒤 새 인스턴스를 확인하거나, 장치 관리자와 `usbipd state`의 `InstanceId`를 함께 확인한다.

> [!NOTE]
> `bind`는 관리자 권한이 필요하지만, 이후 `attach`와 아래 예약 작업 등록·실행은 일반 사용자 권한으로 할 수 있다. 조직 정책이 작업 등록을 제한하면 예외다.

## 2. 로그인 자동 연결 작업 등록

다음 블록은 **일반 사용자 Windows PowerShell 5.1**에서 실행한다. 블록 맨 위의 네 값만 환경에 맞게 바꾼다.

> [!WARNING]
> `$TaskName`과 같은 이름의 예약 작업이 이미 있으면 그 작업의 동작, 트리거, 보안 주체 및 설정을 이 블록이 대체한다. 기존 작업이 필요한지 먼저 확인한다.

```powershell
# ---- 이 네 값만 PC 환경에 맞게 조정 ----
$DistroName = 'Ubuntu-24.04'
$Vid        = '2500'
$TargetPid  = '0020'
$TaskName   = 'Usbipd-USRP-AutoAttach'
# -----------------------------------------

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# 작은따옴표가 들어간 사용자 값을 감시 스크립트의 리터럴로 안전하게 넣는다.
$DistroLiteral = $DistroName.Replace("'", "''")
$VidLiteral    = $Vid.Replace("'", "''")
$PidLiteral    = $TargetPid.Replace("'", "''")

$WatcherScript = @'
$DistroName = '__DISTRO__'
$Vid = '__VID__'
$TargetPid = '__PID__'
$SourceIdentifier = 'Usbipd-USRP-DeviceArrival'

function Test-TargetDistroRunning {
    # wsl.exe의 UTF-16/NUL 혼합 출력을 PowerShell 문자열로 정규화한다.
    $RunningText = (& wsl.exe --list --running --quiet 2>$null | Out-String)
    $RunningText = $RunningText.Replace([string][char]0, [string]::Empty)
    $RunningNames = @(
        $RunningText -split "`r?`n" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    return $RunningNames -contains $DistroName
}

function Connect-BoundUsrp {
    # 이 검사는 WSL을 시작하지 않는다. 이미 실행 중인 경우에만 계속한다.
    if (-not (Test-TargetDistroRunning)) {
        return
    }

    $StateText = (& usbipd.exe state 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($StateText)) {
        return
    }

    try {
        $State = $StateText | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return
    }

    $HardwareIdPattern = 'VID_{0}&PID_{1}' -f [regex]::Escape($Vid), [regex]::Escape($TargetPid)
    $Targets = @(
        $State.Devices | Where-Object {
            $_.BusId -and
            $_.PersistedGuid -and
            (-not $_.ClientIPAddress) -and
            ($_.InstanceId -match $HardwareIdPattern)
        }
    )

    foreach ($Device in $Targets) {
        & usbipd.exe attach --wsl --busid $Device.BusId 2>$null
    }
}

try {
    Connect-BoundUsrp

    Register-WmiEvent `
        -Query 'SELECT * FROM Win32_DeviceChangeEvent WHERE EventType = 2' `
        -SourceIdentifier $SourceIdentifier `
        -ErrorAction Stop | Out-Null

    while ($true) {
        Wait-Event -SourceIdentifier $SourceIdentifier | Out-Null
        Remove-Event -SourceIdentifier $SourceIdentifier -ErrorAction SilentlyContinue

        # 재열거와 연속 장치 이벤트가 usbipd 상태에 반영될 시간을 준다.
        Start-Sleep -Milliseconds 750
        Connect-BoundUsrp
    }
}
finally {
    Unregister-Event -SourceIdentifier $SourceIdentifier -ErrorAction SilentlyContinue
    Remove-Event -SourceIdentifier $SourceIdentifier -ErrorAction SilentlyContinue
}
'@

$WatcherScript = $WatcherScript.Replace('__DISTRO__', $DistroLiteral)
$WatcherScript = $WatcherScript.Replace('__VID__', $VidLiteral)
$WatcherScript = $WatcherScript.Replace('__PID__', $PidLiteral)

# Windows PowerShell의 -EncodedCommand는 UTF-16LE("Unicode") 바이트를 Base64로 받는다.
$EncodedCommand = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($WatcherScript)
)

$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $EncodedCommand"
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# 기존 작업이 실행 중이어도 동일 이름으로 안정적으로 다시 등록할 수 있게 중지한다.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

# 등록 직후에도 사용할 수 있게 한 번 시작한다. 이후에는 로그인 시 자동 시작된다.
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
```

등록 블록은 다음 속성을 적용한다.

- 현재 사용자 로그인 트리거
- 일반 사용자(`Limited`) 권한
- 숨김 실행과 Windows PowerShell 창 숨김
- 실행 시간 제한 없음
- 비정상 종료 시 1분 간격으로 최대 999회 재시작
- 이미 실행 중이면 새 인스턴스를 만들지 않는 `IgnoreNew`
- 감시 본문을 UTF-16LE Base64 `EncodedCommand`로 작업 동작에 내장

`Start-ScheduledTask`는 감시 프로세스만 시작한다. 대상 배포판이 중지되어 있으면 감시 프로세스는 계속 대기하지만 `wsl.exe`로 배포판을 실행하지 않는다.

## 3. 동작 확인

### Windows에서 확인

일반 PowerShell에서 다음을 실행한다.

```powershell
$TaskName = 'Usbipd-USRP-AutoAttach'

Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State

Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, LastTaskResult, NextRunTime

usbipd list
usbipd state
```

감시 작업은 이벤트를 기다리는 동안에도 정상적으로 `Running` 상태다. `LastTaskResult`는 작업이 종료된 뒤의 결과이므로 현재 실행 중인 작업의 성공 여부를 단독으로 판정하는 값은 아니다.

작업에 저장된 실행 명령을 확인하려면 다음을 사용한다.

```powershell
(Get-ScheduledTask -TaskName 'Usbipd-USRP-AutoAttach').Actions |
    Select-Object Execute, Arguments
```

### WSL에서 확인

먼저 의도한 배포판을 사용자가 직접 실행한다. Windows PowerShell에서 다음과 같이 열 수 있다.

```powershell
wsl --distribution Ubuntu-24.04
```

그 다음 B210을 꽂고 WSL 셸에서 확인한다.

```bash
lsusb
lsusb -d 2500:0020
```

장치는 Windows의 `usbipd list`에서 `Attached`로, WSL의 `lsusb`에서 `2500:0020`으로 보여야 한다. Linux 일반 사용자의 장치 접근 권한이나 UHD용 udev 규칙은 별도 문제이며 이 자동화의 범위에 포함되지 않는다.

## 4. 운영 명령

아래 명령은 모두 일반 PowerShell에서 실행할 수 있다.

### 작업 중지

```powershell
Stop-ScheduledTask -TaskName 'Usbipd-USRP-AutoAttach'
```

### 작업 수동 시작

```powershell
Start-ScheduledTask -TaskName 'Usbipd-USRP-AutoAttach'
```

### 설정 변경 또는 재등록

등록 블록 맨 위의 네 값을 수정한 뒤 블록 전체를 다시 실행한다. 같은 작업 이름이면 기존 설정을 대체한다. 작업 이름 자체를 바꾸면 예전 작업은 남으므로 필요하면 별도로 제거한다.

### 완전 제거

다음 명령은 예약 작업만 제거한다. 장치의 bind 상태와 `usbipd-win` 설치는 그대로 유지된다.

```powershell
$TaskName = 'Usbipd-USRP-AutoAttach'
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
```

장치 공유까지 해제하려면 **관리자 PowerShell**에서 실제 BUSID를 확인한 뒤 명시적으로 실행한다. 이는 자동화 제거의 필수 단계가 아니다.

```powershell
usbipd list
usbipd unbind --busid <실제-BUSID>
```

## 참고 자료

- Microsoft Learn: [WSL에서 USB 장치 연결](https://learn.microsoft.com/windows/wsl/connect-usb)
- GitHub: [`dorssel/usbipd-win`](https://github.com/dorssel/usbipd-win)
- GitHub: [`usbipd-win` 릴리스](https://github.com/dorssel/usbipd-win/releases)
