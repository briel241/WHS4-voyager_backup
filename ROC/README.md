# ROC — 육상 행위자

원격운영센터(Remote Operation Centre) 측 응용 행위자.

- [`roc_agent.py`](roc_agent.py) — `RocAgent`. 항로 지시·조종 데이터·AIS 인증값·
  항행경보·X.509 인증서를 만들어 내보낸다. 송신은 `common.outbound.Outbound`만 만들고
  링크에 어떻게 실리는지는 모른다.

공유 코드는 [`common/`](../common), 선박 측은 [`MASS/`](../MASS) 참조.
