"""B210 유선 시스템 통합 (additive; `stage7_b210_system_baseline.md`).

동결 core 프로토콜 스택을 실제 B210 3노드 유선 시스템(Pi 5 + B210 3대 SMA 직결)
위에 조립·구동·조정하는 계층이다. 이름은 이 범위를 정의한 동결 baseline 문서의
도메인 용어를 따른다(이전 이름 `stage7` 은 개발 순서였다).

이 package는 Stage 0~6 동결 공개 DTO·Port·wire·hash-bound 구현을 변경하지 않는
additive module만 담는다. frozen `config`, `runtime`, `radio`, `phy`, `link`,
`mac`, `service` package의 어떤 소스도 수정하지 않으며, core Runtime이 이
package를 역참조(import)해서는 안 된다.

- `control_plane` : 진입점이 실행 중에 쓰는 런타임 제어면(TEST adapter·scenario
  controller·PoC device seam).
- `testing`       : 시험 전용 자료(RR 시험계획·evidence 번들).
"""
