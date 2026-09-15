"""Payload Format Registry (`stage0_baseline.md` §7.2).

Presentation이 소유하는 registry다. 송신은 local `payload_format` key로, 수신은
`(Vpfi, message_format)` key로 조회한다. 운영 registry에는 IALA R1027에 할당된 값만
등록하고 시험용 VPFI를 운영에 사용하지 않는다 (`PRES-REQ-01`).

단계 0에서는 실제 VPFI 할당과 codec을 확정하지 않으므로 운영 registry가 비어 있다.
지원 Payload Format catalogue와 각 codec은 단계 5 Presentation 기준선이 확정한다.
"""

from __future__ import annotations

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import PayloadFormat, Vpfi
from ..common.results import Ok, internal_error
from ..profiles.models import ExplicitRegistry, RegistryScope
from ..profiles.payload_formats import PayloadFormatEntry, entries_for

PAYLOAD_FORMAT_REGISTRY_NAME = "payload_format"


class PayloadFormatRegistry:
    """`payload_format` 조회와 `(vpfi, message_format)` 역조회를 함께 제공한다."""

    __slots__ = ("_registry",)

    def __init__(self, registry: ExplicitRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> ExplicitRegistry:
        return self._registry

    @property
    def scope(self) -> RegistryScope:
        return self._registry.scope

    @property
    def is_frozen(self) -> bool:
        return self._registry.is_frozen

    def register(self, entry: PayloadFormatEntry):
        return self._registry.register(entry)

    def freeze(self):
        return self._registry.freeze()

    def fingerprint(self):
        return self._registry.fingerprint()

    def entry_count(self) -> int:
        return self._registry.entry_count()

    def lookup(self, payload_format: PayloadFormat):
        """송신 lookup. unknown format은 그 요청만 거부한다."""

        return self._registry.lookup(payload_format.value)

    def lookup_wire(self, vpfi: Vpfi, message_format: int | None = None):
        """수신 lookup. `(vpfi, message_format)`에 대응하는 항목을 찾는다."""

        if not self._registry.is_frozen:
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.REGISTRY_NOT_FROZEN,
                ErrorCategory.VALIDATION,
                "Payload Format registry가 freeze되기 전에는 조회할 수 없습니다",
                details=Details.of(registry=PAYLOAD_FORMAT_REGISTRY_NAME),
            )
        for entry in self._registry.entries():
            if entry.wire_key() == (vpfi.value, message_format):
                return Ok(entry)
        return internal_error(
            Layer.PRESENTATION,
            ErrorCode.REGISTRY_UNKNOWN_KEY,
            ErrorCategory.UNSUPPORTED,
            "등록되지 않은 VPFI 또는 message format입니다",
            details=Details.of(
                registry=PAYLOAD_FORMAT_REGISTRY_NAME,
                vpfi=vpfi.value,
                message_format=message_format,
            ),
        )


def build_payload_format_registry(
    scope: RegistryScope, content_version: str = "stage0"
):
    """registry를 만들고 승인된 catalogue를 명시적으로 등록한다. freeze는 호출자가 한다."""

    registry = ExplicitRegistry(
        PAYLOAD_FORMAT_REGISTRY_NAME, scope, content_version, PayloadFormatEntry
    )
    registered = registry.register_all(entries_for(scope))
    if registered.is_err:
        return registered
    return Ok(PayloadFormatRegistry(registry))
