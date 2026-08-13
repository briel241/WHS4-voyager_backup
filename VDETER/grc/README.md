# GNU Radio 파일

- `vdeter_tx.grc`, `vdeter_rx.grc`: 직접 수정하는 Flowgraph 원본
- `generated/`: GRC가 생성한 실행 Python 및 Embedded Python Block wrapper

Flowgraph의 Embedded Python Block은 알고리즘을 복사하지 않고
`src/vdeter/radio/`의 구현을 import한다. `.grc`를 변경한 뒤에는
`generated/` 파일을 다시 생성한다.
