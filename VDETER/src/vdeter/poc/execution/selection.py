"""scenario-first PoC 선택과 명시적 rescan(실행 명세 §6.1, `S8-POC-REQ-23`).

- 사용자가 먼저 scenario를 고르면 compatible PoC와 `none`만 선택 가능하게 표시한다.
- incompatible·malformed package는 이유와 함께 구분해 표시하고 선택할 수 없다.
- required capability를 server가 광고하지 않으면 선택 불가로 표시한다(§6.3, fail-closed).
- rescan은 valid·invalid·changed 수를 요약하고 changed는 이전/현재 SHA-256 짧은 prefix를
  보여준다(전체 hash는 evidence).
- menu는 session absolute·idle remaining을 요청 시 **한 번** 표시하며 초 단위 countdown·
  background refresh·polling을 하지 않는다(§6.1, §3.3).

이 module은 순수 model이다. timer·thread·polling·I/O를 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..api.negotiation import ServerCapabilityConfig
from .catalog import Catalog, PackageEntry

NONE_CHOICE = "none"
SHA_PREFIX_LEN = 12


class SelectionError(Exception):
    """PoC source 선택 계약 위반(§5.3, §6.1). fail-closed."""


class SourceMode(str, Enum):
    """PoC source 지정 방식(§5.3, §6.1)."""

    OFF = "OFF"          # --poc/--poc-dir 없음 → PoC 기능 기본 OFF(구조적 부하 0)
    DIRECT = "DIRECT"    # --poc <file.py>: 비대화형 고정 재현
    CATALOG = "CATALOG"  # --poc-dir <dir>: 대화형 workload별 선택


def resolve_source_mode(*, poc: str | None = None, poc_dir: str | None = None,
                        interactive: bool = True) -> SourceMode:
    """`--poc`/`--poc-dir`를 mode로 해석한다(§5.3, §6.1). 위반은 :class:`SelectionError`.

    - 두 option을 동시에 쓸 수 없다(§5.3).
    - `--poc-dir`는 대화형 mode에서만 쓴다. `run all`이나 scenario를 명시한 비대화형 실행에서
      prompt를 열지 않는다(§6.1).
    - 둘 다 없으면 catalog를 암묵 탐색하지 않고 PoC 기능은 기본 OFF다(§6.1).
    """

    if poc and poc_dir:
        raise SelectionError("--poc와 --poc-dir는 동시에 사용할 수 없습니다(§5.3)")
    if poc_dir:
        if not interactive:
            raise SelectionError(
                "--poc-dir는 대화형 mode 전용입니다(비대화형에서 prompt 금지, §6.1)")
        return SourceMode.CATALOG
    if poc:
        return SourceMode.DIRECT
    return SourceMode.OFF


@dataclass(frozen=True, slots=True)
class SelectableItem:
    """선택 가능한 menu 항목(§6.1). index 0은 항상 `none`."""

    index: int
    kind: str  # "none" | "poc"
    poc_id: str | None = None
    version: str | None = None
    entry: PackageEntry | None = None


@dataclass(frozen=True, slots=True)
class ExcludedItem:
    """선택 불가 항목(incompatible/capability/invalid)과 이유(§6.1)."""

    name: str
    reason: str
    category: str  # "incompatible" | "capability" | "invalid"


@dataclass(frozen=True, slots=True)
class SelectionMenu:
    """scenario에 대한 한 시점의 선택 menu(§6.1). render는 timer 없는 단회 snapshot."""

    scenario_id: str
    items: tuple = ()
    excluded: tuple = ()
    session_absolute_remaining_s: float | None = None
    idle_remaining_s: float | None = None
    truncated: bool = False

    def selectable_ids(self) -> set:
        return {i.poc_id for i in self.items if i.kind == "poc"}

    def resolve(self, choice) -> SelectableItem | None:
        """숫자 index 또는 poc_id로 항목을 찾는다. 없으면 None."""

        for item in self.items:
            if str(choice) == str(item.index):
                return item
            if item.kind == "poc" and choice == item.poc_id:
                return item
        return None

    def render(self) -> str:
        """menu를 한 번 문자열로 만든다(§6.1 예시 형식). 초 단위 countdown 없음."""

        lines = [f"scenario: {self.scenario_id}", "", "PoC package"]
        for item in self.items:
            if item.kind == "none":
                lines.append(f"  {item.index}) none (baseline scenario)")
            else:
                lines.append(f"  {item.index}) {item.poc_id} {item.version}")
        for ex in self.excluded:
            lines.append(f"  [INVALID] {ex.name}: {ex.reason}")
        lines.append("  r) rescan directory")
        lines.append("  q) close session")
        if self.session_absolute_remaining_s is not None or self.idle_remaining_s is not None:
            sess = _fmt(self.session_absolute_remaining_s)
            idle = _fmt(self.idle_remaining_s)
            lines.append(f"  (session remaining {sess}, idle remaining {idle})")
        lines.append("select poc >")
        return "\n".join(lines)


def _fmt(seconds) -> str:
    if seconds is None:
        return "n/a"
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def unavailable_required_capabilities(descriptor, server: ServerCapabilityConfig) -> list:
    """descriptor의 required capability 중 server가 광고하지 않은 것(정렬). 없으면 빈 목록.

    catalog(`build_menu`)와 direct(`--poc`) 경로가 **같은** 설치 capability 사전 검증을 쓰도록
    manifest descriptor에 직접 작용한다(R9, §6.3 fail-closed).
    """
    required = frozenset(descriptor.required_capabilities)
    advertised = server.advertised_capabilities()
    return sorted(required - advertised)


def _required_caps_available(entry: PackageEntry, server: ServerCapabilityConfig) -> tuple:
    unavailable = unavailable_required_capabilities(entry.descriptor, server)
    return (not unavailable), unavailable


def build_menu(
    catalog: Catalog,
    scenario_id: str,
    *,
    server_config: ServerCapabilityConfig | None = None,
    session_absolute_remaining_s: float | None = None,
    idle_remaining_s: float | None = None,
) -> SelectionMenu:
    """scenario 선택 뒤의 menu를 만든다(§6.1). compatible PoC와 `none`만 선택 가능."""

    server = server_config or ServerCapabilityConfig()
    items = [SelectableItem(index=0, kind="none")]
    excluded: list = []

    index = 1
    for entry in sorted(catalog.valid, key=lambda p: (p.poc_id, p.version)):
        if scenario_id not in entry.descriptor.compatible_scenarios:
            excluded.append(ExcludedItem(
                name=f"{entry.poc_id} {entry.version}",
                reason=f"scenario {scenario_id} not in compatible_scenarios",
                category="incompatible",
            ))
            continue
        ok, unavailable = _required_caps_available(entry, server)
        if not ok:
            excluded.append(ExcludedItem(
                name=f"{entry.poc_id} {entry.version}",
                reason=f"required capability unavailable: {','.join(unavailable)}",
                category="capability",
            ))
            continue
        items.append(SelectableItem(
            index=index, kind="poc",
            poc_id=entry.poc_id, version=entry.version, entry=entry,
        ))
        index += 1

    for bad in catalog.invalid:
        excluded.append(ExcludedItem(
            name=bad.name or bad.manifest_name,
            reason=bad.reason,
            category="invalid",
        ))

    return SelectionMenu(
        scenario_id=scenario_id,
        items=tuple(items),
        excluded=tuple(excluded),
        session_absolute_remaining_s=session_absolute_remaining_s,
        idle_remaining_s=idle_remaining_s,
        truncated=catalog.truncated,
    )


@dataclass(frozen=True, slots=True)
class ChangedPackage:
    """rescan에서 script/manifest hash가 바뀐 package(§6.1)."""

    poc_id: str
    prev_script_prefix: str
    curr_script_prefix: str
    prev_script_sha256: str
    curr_script_sha256: str
    prev_manifest_sha256: str
    curr_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RescanSummary:
    """rescan 결과 요약(§6.1). valid·invalid·changed 수와 변경 hash prefix."""

    valid_count: int
    invalid_count: int
    changed: tuple = ()

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    def render(self) -> str:
        lines = [
            f"rescan: valid={self.valid_count} invalid={self.invalid_count} "
            f"changed={self.changed_count}"
        ]
        for c in self.changed:
            lines.append(f"  changed {c.poc_id}: {c.prev_script_prefix} -> {c.curr_script_prefix}")
        return "\n".join(lines)


def rescan_summary(previous: Catalog, current: Catalog) -> RescanSummary:
    """이전/현재 catalog를 비교해 valid·invalid·changed를 집계한다(§6.1)."""

    prev_by_id = previous.valid_by_id()
    changed: list = []
    for entry in sorted(current.valid, key=lambda p: p.poc_id):
        old = prev_by_id.get(entry.poc_id)
        if old is None:
            continue
        if (old.script_sha256 != entry.script_sha256
                or old.manifest_sha256 != entry.manifest_sha256):
            changed.append(ChangedPackage(
                poc_id=entry.poc_id,
                prev_script_prefix=old.script_sha256[:SHA_PREFIX_LEN],
                curr_script_prefix=entry.script_sha256[:SHA_PREFIX_LEN],
                prev_script_sha256=old.script_sha256,
                curr_script_sha256=entry.script_sha256,
                prev_manifest_sha256=old.manifest_sha256,
                curr_manifest_sha256=entry.manifest_sha256,
            ))
    return RescanSummary(
        valid_count=len(current.valid),
        invalid_count=len(current.invalid),
        changed=tuple(changed),
    )


__all__ = (
    "NONE_CHOICE",
    "SelectionError",
    "SourceMode",
    "resolve_source_mode",
    "SHA_PREFIX_LEN",
    "SelectableItem",
    "ExcludedItem",
    "SelectionMenu",
    "build_menu",
    "unavailable_required_capabilities",
    "ChangedPackage",
    "RescanSummary",
    "rescan_summary",
)
