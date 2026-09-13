# AES-256-GCM VDS2 통합 결과

## 기준선

- 작업 브랜치: `integration/aes256-gcm-vdeter`
- 수정 전 통합 기준 및 현재 HEAD: `origin/main`의 `1918da68d8ce18101b8309a7879e7d99714a7fda`
- 참조 구현: `origin/feature/aes256-gcm-e2e`의 `a8ace84da4bf6c49536e12813ca77cc1fb7155ae`
- 명세와 Git에서 확인한 공통 조상 커밋: `6b0e952c29bda685ec735e984b0703d4ea98f8f8`

`origin/main`의 통합 지점은 유효했다. `RegistryPresentationAdapter`는 TX 코덱 인코딩과 RX 접두부 디스패치를 모두 담당하고, Stage5는 생성된 Presentation 바이트를 캐시하며, Link는 최종 전송 길이에 따라 Short Data 또는 Data Session을 선택한다. 기능 브랜치는 AES-GCM 래퍼와 재전송 방지 윈도우 동작을 참조하는 용도로만 사용했다. 기능 브랜치의 `crypto_module` 트리와 전송/분할 코드는 복사하거나 병합하지 않았다.

## 구현 내용

새로 추가한 `VDETER/src/vdeter/security/` 파일은 다음 기능을 제공한다.

- VDS2 상수: 제어 값 `0x80`, 도메인 `VDS2`, 7바이트 헤더, 12바이트 nonce, 22바이트 AAD, 16바이트 태그 및 명세에 정의된 사용 한도.
- `prefix || header || ciphertext || tag` 형식의 프로토콜 인코딩. 정확히 3바이트인 VPFI/MF 접두부는 평문으로 유지하고 애플리케이션 본문만 암호화한다.
- `cryptography` 50.x를 이용한 분리형 AES-256-GCM과 외부에 단일 형태로 노출되는 인증 실패 처리.
- 명시적인 REQUIRED/DISABLED 정책. 프로젝트의 TEST SC-VDE 형식 3개는 REQUIRED이고, 레거시 인증서, AIS 인증 및 불투명 형식은 DISABLED이다.
- 쌍별, 방향별 키 자료 모델과 엄격한 JSON 로딩. 키와 nonce 접두부는 `repr`, 로그, CLI, 환경 변수 및 SQLite에 노출하거나 저장하지 않는다.
- endpoint, station, state ID 및 material ID에 바인딩된 SQLite 상태. TX는 카운터를 1024개 블록 단위로 예약하며, RX는 검증 횟수, 실패 횟수 및 64개 메시지 재전송 방지 비트맵을 영속화한다. 인접 잠금 파일로 프로세스의 동시 소유를 방지한다.
- 세션 관리, 메트릭, 제한 시간이 있는 최종 drain, 덮어쓰기, 초기화, 강제 적용 또는 일부 설치를 거부하는 대역 외 프로비저닝 도구.

수정한 통합 지점에는 Presentation 어댑터, 공통 오류 코드 및 공개 오류 변환, Stage5/Wideband/Stage7 빌더, Stage7 coordinator와 launcher 인자, 소프트웨어 MASS/ROC fixture 및 독립 실행 호출부가 포함된다. 기존 Link, segment, fragmentation, reassembly, MAC, PHY, 코덱, payload registry 및 service DTO는 변경하지 않았다.

운영 코드 파일 구성은 다음과 같다.

- `VDETER/src/vdeter/security/**`: 새 프로토콜, 암호화, 정책, 메트릭, 키 자료/provider, SQLite 상태, 재전송 방지, 세션, 의존성 및 Presentation 오케스트레이터.
- `VDETER/src/vdeter/presentation/adapter.py`: 보안 적용 시 크기 상한과 REQUIRED protect/unprotect 호출.
- `VDETER/src/vdeter/common/errors.py`, `runtime/projection.py`: 안정적인 내부 오류 코드와 민감 정보를 제거한 공개 오류 매핑.
- `VDETER/src/vdeter/runtime/wiring.py`, `runtime/stage5.py`, `stage7/wiring.py`: 명시적인 의존성과 endpoint 수명 동안의 상태 유지.
- `VDETER/apps/stage7_coordinator_main.py`, `stage7_endpoint_launcher.py`: 필수 모드 전달, 외부 상태 로딩, payload를 포함하지 않는 ENFORCED 이벤트 기록 및 최종 drain.
- `apps/stage7_run.py`, `stage7_session.py`, `stage7_service_layer.py`, `standalone/local_session.py`, `standalone/qualify_cmd.py`, `VDETER/tools/stage7_service_debug.py`: 모든 호출부에서 명시적인 실행 모드 지정.
- `apps/mass_roc_software.py`, `apps/stage7_mass_roc_controller.py`: ENFORCED 소프트웨어 그래프와 hash/length 기반 실시간 payload 검증.
- `VDETER/tools/e2e_security_provision.py`, `apps/standalone/qualification_domains.py`: 최초 설치 프로비저닝과 qualification domain 분류.
- `VDETER/tests/support/security.py`, `VDETER/tests/unit/security/**` 및 수정된 기존 지원/테스트 파일: 상호 대응하는 임시 fixture, VDS2 인수 테스트 및 명시적인 DISABLED 회귀 호출부.

## 송수신 흐름

TX는 먼저 코덱 검증과 인코딩을 수행하고 정확한 VPFI/MF 접두부를 유지한다. 이어 영속적인 방향별 카운터를 예약하고 헤더/nonce/AAD를 구성한 뒤 본문만 암호화하여 최종 보안 Presentation 바이트를 기존 Link 경로에 전달한다. Stage5가 해당 바이트를 캐시하므로 Link 재전송은 새 카운터를 할당하지 않는다. 보안 길이 판정은 23바이트를 추가로 반영한다. 이에 따라 Link 11은 위치 보고와 경로 명령을 Data Session으로 전송하고, 3바이트 MF3 ACK는 Type 93 Short Data를 계속 사용할 수 있다.

RX는 기존 Link 재조립을 완료한 뒤 Presentation 디스패치를 수행한다. 평문 접두부로 코덱을 선택하고 VDS2를 파싱 및 검증하며, 코덱이 허용하는 전체 본문 길이 집합을 검사한다. 그다음 상태를 변경하지 않는 재전송 사전 검사를 수행하고 검증 횟수를 예약한 뒤 인증 및 복호화한다. 재전송 방지 상태를 영속적으로 커밋하고 평문 Presentation wire를 재구성한 후 기존 코덱을 호출한다. REQUIRED 형식의 평문과 유효하지 않은 태그는 fallback이나 애플리케이션 전달 없이 폐기한다.

## 설치 및 실행

기존에 선언된 extra를 설치한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e 'VDETER[crypto,test,dsp]'
```

배포 시스템은 저장소 외부에 소유자만 읽을 수 있는 키 자료 파일 3개를 배치한다. 해당 파일을 검증한 뒤 비어 있는 상태 루트에 한 번만 설치한다.

```bash
sudo install -d -m 0700 -o vdeter -g vdeter /var/lib/vdeter/security-bundles
sudo install -d -m 0700 -o vdeter -g vdeter /var/lib/vdeter/security
sudo -u vdeter python3 VDETER/tools/e2e_security_provision.py validate-bundle \
  --bundle-dir /var/lib/vdeter/security-bundles
sudo -u vdeter python3 VDETER/tools/e2e_security_provision.py initialize-state \
  --bundle-dir /var/lib/vdeter/security-bundles \
  --state-root /var/lib/vdeter/security
```

실제 MASS/ROC 서비스 경로는 `--e2e-security-mode ENFORCED`로 실행한다. 레거시 RF qualification 경로는 `DISABLED`를 명시적으로 전달하며, 보안 검증 근거로 사용하지 않는다. launcher는 `/var/lib/vdeter/security/<endpoint-id>/materials.json`과 `state.sqlite3`을 읽는다. 상태가 없거나, 일부만 설치되었거나, 서로 일치하지 않거나, 손상되었거나, 잠금을 획득할 수 없거나, 권한이 잘못된 경우 시작에 실패한다. DB가 손실되거나 복구된 사실을 알고 있다면 기존 material bundle을 재사용하지 않는다. 새 키, nonce 접두부, state ID, provisioning ID 및 상태 DB를 함께 배포해야 한다.

로컬 소프트웨어 검증에는 다음 명령을 사용했다.

```powershell
.\.venv\Scripts\python.exe apps\scenario_runner.py run SC-VDE-005 --transport software --no-save --json
.\.venv\Scripts\python.exe apps\scenario_runner.py run SC-VDE-006 --transport software --no-save --json
cd VDETER
..\.venv\Scripts\python.exe -m pytest -q tests/unit/security/test_vds2.py
```

## 테스트 결과

- 통과: VDS2 단위 및 상태 테스트 17개. 결정적 AES/header/nonce/AAD 벡터, 변조, 재전송 순서/중복/오래된 메시지 검사, 동시 재전송 처리, 본문 전용 어댑터 보호, REQUIRED 평문 거부, 전체 SC 코덱 길이 집합, 양방향 카운터, SQLite 비정상 종료 시 gap, RX 재시작 후 재전송 처리, 고아 검증 횟수 반영, 소유권 잠금, 최종 사용 한도, 예상치 못한 검증 오류 발생 시 억제, 키 자료 파싱, 상호 대응 쌍, 프로비저닝 덮어쓰기 금지, 오류 변환 및 최종 close를 검증했다.
- 통과: 코덱, registry 및 Presentation 회귀 테스트 110개와 subtest 185개. 기존 pytest 경고 1건은 테스트 함수가 tuple을 반환한다는 내용이다.
- 통과: 이전 Presentation dispatch 및 Data Session 회귀 테스트 36개와 subtest 38개.
- 통과: Stage7 mock backend 및 MASS/ROC software backend 테스트 14개.
- 통과: 선별한 Link/MAC, Stage5 lifecycle/Data Session, Stage7 wiring 및 wideband 회귀 테스트 54개와 subtest 7개. 기존 수집 경고 1건은 생성자가 있는 `TestTimeSource`에 관한 것이다.
- 재검증: `VDETER/tests/unit/test_stage7_service_phase.py` 전체 실행 결과는 `6 failed, 17 passed`였다. `TC_QUALIFIED_PATH_INVARIANT` 클래스 전체를 제외한 실행 결과는 `15 passed, 8 deselected`였다. 제외된 8개 중 6개는 전체 실행에서 실패했고, `test_no_qualified_function_removed`와 `test_run_rf_qualification_ast_and_source_invariant`는 정상 통과했지만 클래스 단위 제외로 함께 빠졌다. 따라서 Stage7 서비스 테스트 전체가 통과한 것은 아니다.
- 통과: qualification domain 및 설치 manifest 테스트 24개.
- 통과: SC-VDE-005 software-radio ENFORCED 시나리오. 5개 단계와 명령 및 ACK가 완료되었고, 애플리케이션 본문이 바이트 단위로 정확했으며 중복 또는 잘못된 목적지 전달이 없었다.
- 통과: SC-VDE-006 software-radio ENFORCED 시나리오. 선행 조건인 005와 11개 단계가 모두 통과했으며 steering/fallback 명령과 ACK가 바이트 단위로 정확하게 완료되었다.
- 통과: Python compileall 및 `git diff --check`.

## 미완료 사항 및 한계

- 이 Windows 환경에는 B210/UHD 테스트 구성과 배포용 키 자료가 없으므로 B210/UHD 전도성 RF qualification, 보안이 적용된 Stage7 하드웨어 왕복, 하드웨어 장애/soak 테스트 및 새로 고정한 qualification reference를 실행하지 못했다.
- qualification 불변성 테스트 6개가 실패했다. 새 필수 모드 전달로 변경된 `spawn_and_connect`와 `main`이 기존 qualification 기준의 허용 변경 집합과 다르다. `run_rr_p0`와 `run_rr_p3_fault`는 고정된 source 해시와 일치하지만 AST 해시가 현재 Python 3.13에서 불일치한다. fixture는 Python 3.12에서 검증된 것이므로 버전별 AST 표현 차이가 원인으로 분석되지만, Python 3.12 재검증 전에는 환경 원인으로 확정할 수 없다. 기준값은 수동으로 수정하지 않았으며, 실제 B210 qualification 및 새 참조 산출물 발행이 필요하다.
- 전체 저장소 테스트 suite는 실행하지 않았다. Presentation, Link/MAC, Stage5, Stage7, backend, scenario 및 security 중심의 테스트 suite를 실행했다.
- 이 프로파일은 키 교환, 서명, 자동 rekey/epoch rotation, 하드웨어 기반 키 저장소, 적대적 RF 환경의 rate limiting, 공격자가 전체 snapshot을 제어하는 경우의 rollback 탐지 또는 표준/운영 SC-VDE 상호운용성을 구현하지 않는다. `FF 7F`는 프로젝트 TEST/PROJECT 전용으로 유지된다.
- 별도의 InvalidTag rate limiting은 설계상 포함하지 않았다. 유효하지 않은 태그는 계속 fail closed로 처리되며, 재전송 상태를 갱신하거나 평문을 반환하지 않고 영속적인 검증/실패 예산을 소모한다.
- ENFORCED 영속 이벤트 JSONL에는 애플리케이션 payload 바이트를 기록하지 않는다. payload hash/length와 메타데이터만 기록하며, 시나리오 검증은 이를 실행 중인 controller 프로세스가 보유한 본문과 비교한다.
