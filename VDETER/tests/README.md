# Tests

- `unit/`: Message, Packet, FEC, 변복조 등 계층별 단위 시험
- `integration/`: Link-PHY-Radio 연결 시험
- `hardware/`: 유선 B210 송수신 시험
- `vectors/`: Message ID, Link ID, 패킷, FEC, 변조 기준 벡터

`hardware/` 시험은 장비가 없는 일반 테스트 실행에서 자동으로 제외할 수 있게
별도 표시를 사용한다.
