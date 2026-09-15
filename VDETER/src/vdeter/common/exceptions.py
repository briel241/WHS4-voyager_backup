"""공통 예외 계층.

`stage0_baseline.md` §5.2에 따라 예상 가능한 validation·protocol·timeout·resource·device
오류는 계층 경계를 raw exception으로 넘지 않는다. 이 module의 예외는 계층 *내부*에서
잘못된 값을 즉시 알리기 위한 것이며, 경계에서는 `common.results.try_call` 등을 사용해
`InternalProtocolError`로 변환한다.

`VdeterInternalError`는 programming error와 깨진 invariant를 뜻하며 성공이나 입력 오류로
위장하지 않는다.
"""

from __future__ import annotations


class VdeterError(Exception):
    """VDE-TER 구현이 발생시키는 모든 예외의 기반."""


class VdeterValidationError(VdeterError):
    """공개 DTO·식별자·설정 값이 계약을 만족하지 않는다."""


class InvalidValueError(VdeterValidationError):
    """단일 값이 정의된 범위·형식을 벗어났다."""


class ClockMismatchError(VdeterValidationError):
    """서로 다른 clock domain 또는 clock id의 시각을 직접 비교·차감하려 했다."""


class RegistryError(VdeterValidationError):
    """Registry 등록·검증·조회 계약 위반."""


class RegistryFrozenError(RegistryError):
    """freeze된 registry를 변경하려 했다."""


class ConfigError(VdeterValidationError):
    """설정 parse 또는 validation 실패."""


class SerializationError(VdeterValidationError):
    """reference JSON 표현 생성 또는 복원 실패."""


class VdeterInternalError(VdeterError):
    """programming error 또는 깨진 invariant. 입력 오류로 위장하지 않는다."""
