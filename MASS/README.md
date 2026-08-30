# MASS — 선박 행위자

자율운항선박(Maritime Autonomous Surface Ship) 측 응용 행위자.

- [`mass_agent.py`](mass_agent.py) — `MassAgent`. 항로를 따라 움직이고 위치보고를
  내보내며, 받은 항로·경보·인증서를 검증·반영한다. 송신은 `common.outbound.Outbound`만
  만들고 링크에 어떻게 실리는지는 모른다.

공유 코드는 [`common/`](../common), 육상 측은 [`ROC/`](../ROC) 참조.
