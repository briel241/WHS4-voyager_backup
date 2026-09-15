"""Stage 7 B210 유선 시스템 통합 (additive; `stage7_b210_system_baseline.md`).

이 package는 Stage 0~6 동결 공개 DTO·Port·wire·hash-bound 구현을 변경하지 않는
additive module만 담는다. frozen `config`, `runtime`, `radio`, `phy`, `link`,
`mac`, `service` package의 어떤 소스도 수정하지 않으며, core Runtime이 이
package를 역참조(import)해서는 안 된다 (TEST→launcher composition 방향만 허용).
"""
