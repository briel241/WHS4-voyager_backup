# Legacy

이 폴더는 구조 개편 전 실험 원본을 수정하지 않은 상태로 보관한다.

- `error_correction/`: 기존 Turbo FEC 코드, 시험 보조 블록 및 `fec.grc`
- `bitstream/`: 초기 `F9A8 + Hello` 비트스트림 수신 실험
- `grc/`: 저장소 루트에 있던 초기 Hello 송수신 및 루프백 GRC 실험
- `docs/`: 저장소 루트에 있던 WSL/USRP 장비 안내와 WBS 문서

현재 신규 구조의 기준 코드는 기존 `no_error_correction`에서 옮긴
`src/vdeter/`이며, 레거시 파일은 비교와 회귀 확인에만 사용한다.
