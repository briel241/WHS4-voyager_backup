"""Registry 공통 생명주기와 표준 profile model (`stage0_baseline.md` §7, §8).

모든 registry는 다음 생명주기를 따른다.

```text
CREATE → EXPLICIT_REGISTER → VALIDATE → FREEZE → LOOKUP
```

- endpoint 시작 전에 `builtins`와 승인된 config가 항목을 명시적으로 등록한다.
- duplicate key, 잘못된 범위, 상호 모순 entry는 시작 실패다.
- `freeze()` 뒤 등록·수정·삭제할 수 없고 lookup 결과는 immutable이다.
- unknown lookup은 registry를 변경하거나 fallback하지 않고 그 요청만 거부한다.
- decorator, import side effect, silent overwrite와 implicit default profile은 금지한다.
- operational scope와 test scope는 별도 registry instance로 분리한다.

`profiles`는 immutable 표준 자료만 제공하고 runtime 상태를 담지 않는다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from ..common.errors import (
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
)
from ..common.exceptions import InvalidValueError, RegistryFrozenError
from ..common.results import Ok, internal_error
from ..common.serialization import to_reference_json

REGISTRY_SCHEMA_VERSION = 1


class RegistryScope(str, Enum):
    """운영 registry와 시험 registry를 분리하기 위한 scope."""

    OPERATIONAL = "OPERATIONAL"
    TEST = "TEST"


class SupportStatus(str, Enum):
    """항목의 지원 상태. 단계 0에서는 계획 상태만 표현한다."""

    PLANNED = "PLANNED"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SourceReference:
    """표준 값의 출처. 판본과 정확한 절·표 위치를 함께 기록한다."""

    document: str
    edition: str
    location: str

    def __post_init__(self) -> None:
        for name in ("document", "edition", "location"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InvalidValueError(f"SourceReference.{name}: 비어 있지 않은 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class RegistryFingerprint:
    """registry 내용의 결정적 지문. 시작 log와 시험 증거에 기록한다."""

    name: str
    scope: RegistryScope
    schema_version: int
    content_version: str
    entry_count: int
    fingerprint: str


class ExplicitRegistry:
    """명시적 등록·검증·freeze·조회를 강제하는 registry.

    entry는 frozen dataclass여야 하며 `registry_key()`와 `scope` 속성을 제공해야 한다.
    """

    __slots__ = ("_name", "_scope", "_content_version", "_entries", "_frozen", "_entry_type")

    def __init__(
        self,
        name: str,
        scope: RegistryScope,
        content_version: str,
        entry_type: type,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise InvalidValueError("ExplicitRegistry.name: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(scope, RegistryScope):
            raise InvalidValueError("ExplicitRegistry.scope: RegistryScope여야 합니다")
        if not isinstance(content_version, str) or not content_version:
            raise InvalidValueError("ExplicitRegistry.content_version이 비어 있습니다")
        dataclass_params = getattr(entry_type, "__dataclass_params__", None)
        if (
            not isinstance(entry_type, type)
            or not dataclasses.is_dataclass(entry_type)
            or dataclass_params is None
            or not dataclass_params.frozen
        ):
            raise InvalidValueError("ExplicitRegistry.entry_type: frozen dataclass여야 합니다")
        self._name = name
        self._scope = scope
        self._content_version = content_version
        self._entry_type = entry_type
        self._entries = {}
        self._frozen = False

    # --- 상태 ---

    @property
    def name(self) -> str:
        return self._name

    @property
    def scope(self) -> RegistryScope:
        return self._scope

    @property
    def content_version(self) -> str:
        return self._content_version

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def entry_count(self) -> int:
        return len(self._entries)

    def keys(self) -> tuple:
        return tuple(sorted(self._entries))

    # --- 등록과 검증 ---

    def register(self, entry) -> object:
        """항목 하나를 명시적으로 등록한다. 중복과 scope 불일치는 거부한다."""

        if self._frozen:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_FROZEN,
                ErrorCategory.VALIDATION,
                f"{self._name} registry는 freeze되어 변경할 수 없습니다",
                details=Details.of(registry=self._name),
            )
        if not isinstance(entry, self._entry_type):
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_INVALID_ENTRY,
                ErrorCategory.VALIDATION,
                f"{self._name} registry에는 {self._entry_type.__name__}만 등록할 수 있습니다",
                details=Details.of(registry=self._name, entry_type=type(entry).__name__),
            )
        if entry.scope is not self._scope:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_SCOPE_MISMATCH,
                ErrorCategory.VALIDATION,
                "운영 registry와 시험 registry는 서로의 항목을 등록할 수 없습니다",
                details=Details.of(
                    registry=self._name,
                    registry_scope=self._scope.value,
                    entry_scope=entry.scope.value,
                ),
            )
        key = entry.registry_key()
        if key in self._entries:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_DUPLICATE_KEY,
                ErrorCategory.VALIDATION,
                f"{self._name} registry에 이미 등록된 key입니다",
                details=Details.of(registry=self._name, key=str(key)),
            )
        self._entries[key] = entry
        return Ok(entry)

    def register_all(self, entries) -> object:
        """여러 항목을 등록한다. 첫 실패에서 중단하고 그 오류를 반환한다."""

        for entry in entries:
            result = self.register(entry)
            if result.is_err:
                return result
        return Ok(self.entry_count())

    def validate(self) -> object:
        """entry 사이의 상호 모순을 검사한다. 기본 구현은 key 일관성만 확인한다."""

        for key, entry in self._entries.items():
            if entry.registry_key() != key:
                return internal_error(
                    Layer.COMMON,
                    ErrorCode.REGISTRY_INVALID_ENTRY,
                    ErrorCategory.VALIDATION,
                    f"{self._name} registry의 key와 entry가 일치하지 않습니다",
                    details=Details.of(registry=self._name, key=str(key)),
                )
        return Ok(self.entry_count())

    def freeze(self) -> object:
        """검증 후 registry를 동결한다. 이후 등록·수정·삭제할 수 없다."""

        if self._frozen:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_FROZEN,
                ErrorCategory.VALIDATION,
                f"{self._name} registry는 이미 freeze되었습니다",
                details=Details.of(registry=self._name),
            )
        validated = self.validate()
        if validated.is_err:
            return validated
        self._frozen = True
        return Ok(self.fingerprint())

    # --- 조회 ---

    def lookup(self, key) -> object:
        """등록된 항목을 조회한다. unknown key는 그 요청만 거부한다."""

        if not self._frozen:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_NOT_FROZEN,
                ErrorCategory.VALIDATION,
                f"{self._name} registry가 freeze되기 전에는 조회할 수 없습니다",
                details=Details.of(registry=self._name),
            )
        entry = self._entries.get(key)
        if entry is None:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_UNKNOWN_KEY,
                ErrorCategory.UNSUPPORTED,
                f"{self._name} registry에 등록되지 않은 key입니다",
                details=Details.of(registry=self._name, key=str(key)),
            )
        return Ok(entry)

    def contains(self, key) -> bool:
        return key in self._entries

    def entries(self) -> tuple:
        return tuple(self._entries[key] for key in sorted(self._entries))

    # --- 지문 ---

    def canonical_content(self) -> dict:
        payload = []
        for entry in self._entries.values():
            # codec 객체처럼 portable하지 않은 field를 가진 entry는 `fingerprint_content()`로
            # 지문 대상 자료를 명시한다. 그 밖의 entry는 reference JSON 표현을 사용한다.
            content_factory = getattr(entry, "fingerprint_content", None)
            content = content_factory() if callable(content_factory) else to_reference_json(entry)
            payload.append(json.dumps(content, ensure_ascii=False, sort_keys=True))
        return {
            "name": self._name,
            "scope": self._scope.value,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "content_version": self._content_version,
            "entries": sorted(payload),
        }

    def fingerprint(self) -> RegistryFingerprint:
        """등록 순서와 무관한 결정적 지문을 만든다."""

        canonical = json.dumps(self.canonical_content(), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return RegistryFingerprint(
            name=self._name,
            scope=self._scope,
            schema_version=REGISTRY_SCHEMA_VERSION,
            content_version=self._content_version,
            entry_count=len(self._entries),
            fingerprint=digest,
        )

    def require_frozen(self) -> None:
        if not self._frozen:
            raise RegistryFrozenError(f"{self._name} registry가 freeze되지 않았습니다")
