# 문서 경로 안내

이 폴더의 문서는 기존 `no_error_correction` 구현과 함께 이동했다. 문서 본문에
남아 있는 일부 상대 경로는 구조 개편 전 이름을 설명한다. 현재 코드 위치는 다음과
같다.

| 기존 경로 | 현재 경로 |
| --- | --- |
| `vdeter_link/messages/` | `src/vdeter/link/messages/` |
| `vdeter_link/packet/` | `src/vdeter/link/packet/` |
| `vdeter_link/burst/` | `src/vdeter/phy/burst/` |
| `vdeter_link/utils/` | `src/vdeter/common/` |
| `vdeter_link/adapters/` | `src/vdeter/radio/` |
| `vdeter_link/cli/` | `src/vdeter/cli/` |
| `vdeter_link/config.py` | `src/vdeter/link/config.py` |

새 문서와 코드 예시는 현재 경로와 `vdeter` 패키지명을 기준으로 작성한다.
