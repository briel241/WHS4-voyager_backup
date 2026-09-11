"""scenario-first 선택과 rescan 요약 시험(실행 명세 §6.1, `S8-POC-REQ-23`). platform 무관.

Catalog 객체를 직접 구성해(파일시스템 없이) selection model을 검증한다.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.execution.catalog import Catalog, FileFacts, InvalidPackage, PackageEntry
from vdeter.stage8.execution.manifest import parse_and_validate
from vdeter.stage8.execution.selection import (
    SelectionError,
    SourceMode,
    build_menu,
    rescan_summary,
    resolve_source_mode,
)
from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
from vdeter.stage8.testing.execution_fakes import poc_toml


def _facts(name, sha):
    return FileFacts(path=name, size=100, dev=1, ino=hash(name) & 0xFFFF,
                     mtime_ns=1, mode=0o100600, uid=1000, sha256=sha)


def _entry(poc_id, *, scenarios=("SC-VDE-006",), required=(), caps=("observe_packet", "replay_packet"),
           script_sha="ab" * 32, manifest_sha="cd" * 32):
    script_name = poc_id.replace("-", "_") + ".py"
    text = poc_toml(poc_id=poc_id, script_basename=script_name,
                    compatible_scenarios=scenarios, capabilities=caps,
                    required_capabilities=required)
    desc = parse_and_validate(text.encode(), script_basename=script_name,
                              script_sha256=script_sha, manifest_sha256=manifest_sha)
    return PackageEntry(poc_id=poc_id, version=desc.version, descriptor=desc,
                        script=_facts(script_name, script_sha),
                        manifest=_facts(script_name + ".poc.toml", manifest_sha))


class MenuTest(unittest.TestCase):
    def test_none_always_present(self):
        cat = Catalog(poc_dir="/poc")
        menu = build_menu(cat, "SC-VDE-006")
        self.assertEqual(menu.items[0].kind, "none")
        self.assertEqual(menu.items[0].index, 0)

    def test_only_compatible_selectable(self):
        cat = Catalog(poc_dir="/poc", valid=(
            _entry("replay-attack", scenarios=("SC-VDE-006",)),
            _entry("other-attack", scenarios=("SC-VDE-999",)),
        ))
        menu = build_menu(cat, "SC-VDE-006")
        self.assertEqual(menu.selectable_ids(), {"replay-attack"})
        # incompatible은 excluded에 이유와 함께.
        reasons = {e.name: e for e in menu.excluded}
        self.assertTrue(any(e.category == "incompatible" for e in menu.excluded))

    def test_invalid_shown_not_selectable(self):
        cat = Catalog(poc_dir="/poc",
                      valid=(_entry("replay-attack"),),
                      invalid=(InvalidPackage("broken", "broken.poc.toml", "schema는 ...여야"),))
        menu = build_menu(cat, "SC-VDE-006")
        self.assertIn("broken", {e.name for e in menu.excluded})
        self.assertNotIn("broken", menu.selectable_ids())

    def test_required_capability_unavailable_excluded(self):
        # server가 dual-channel을 광고하지 않으면(기본) required 요청 package는 선택 불가.
        cat = Catalog(poc_dir="/poc", valid=(
            _entry("dual", caps=("observe_packet", "observe_dual_physical_channel"),
                   required=("observe_dual_physical_channel",)),
        ))
        menu = build_menu(cat, "SC-VDE-006", server_config=ServerCapabilityConfig())
        self.assertEqual(menu.selectable_ids(), set())
        self.assertTrue(any(e.category == "capability" for e in menu.excluded))

    def test_required_capability_available_when_advertised(self):
        cat = Catalog(poc_dir="/poc", valid=(
            _entry("dual", caps=("observe_packet", "observe_dual_physical_channel"),
                   required=("observe_dual_physical_channel",)),
        ))
        server = ServerCapabilityConfig(wsm_g1_production_installed=True)
        menu = build_menu(cat, "SC-VDE-006", server_config=server)
        self.assertIn("dual", menu.selectable_ids())

    def test_resolve_by_index_and_id(self):
        cat = Catalog(poc_dir="/poc", valid=(_entry("replay-attack"),))
        menu = build_menu(cat, "SC-VDE-006")
        self.assertEqual(menu.resolve(0).kind, "none")
        self.assertEqual(menu.resolve(1).poc_id, "replay-attack")
        self.assertEqual(menu.resolve("replay-attack").poc_id, "replay-attack")
        self.assertIsNone(menu.resolve(99))

    def test_menu_render_single_snapshot_no_ticker(self):
        cat = Catalog(poc_dir="/poc", valid=(_entry("replay-attack"),))
        menu = build_menu(cat, "SC-VDE-006",
                          session_absolute_remaining_s=3600, idle_remaining_s=1800)
        text = menu.render()
        # remaining time은 한 번만 표시. 두 번 render해도 동일(내부 timer 없음).
        self.assertEqual(text, menu.render())
        self.assertIn("session remaining", text)
        self.assertIn("select poc >", text)


class SourceModeTest(unittest.TestCase):
    """`--poc`/`--poc-dir` 해석 계약(§5.3, §6.1)."""

    def test_off_when_neither_given(self):
        # 둘 다 없으면 catalog를 암묵 탐색하지 않고 PoC 기능은 기본 OFF(§6.1).
        self.assertEqual(resolve_source_mode(), SourceMode.OFF)

    def test_direct_mode(self):
        self.assertEqual(resolve_source_mode(poc="./replay_attack.py"), SourceMode.DIRECT)

    def test_catalog_mode_interactive(self):
        self.assertEqual(resolve_source_mode(poc_dir="/home/u/vdeter-pocs"), SourceMode.CATALOG)

    def test_both_options_rejected(self):
        # §5.3: 두 option은 동시에 사용할 수 없다.
        with self.assertRaises(SelectionError):
            resolve_source_mode(poc="./a.py", poc_dir="/poc")

    def test_poc_dir_rejected_when_non_interactive(self):
        # §6.1: run all·비대화형 실행에서 prompt를 열지 않는다.
        with self.assertRaises(SelectionError):
            resolve_source_mode(poc_dir="/poc", interactive=False)

    def test_direct_mode_allowed_when_non_interactive(self):
        self.assertEqual(resolve_source_mode(poc="./a.py", interactive=False), SourceMode.DIRECT)


class RescanTest(unittest.TestCase):
    def test_counts(self):
        prev = Catalog(poc_dir="/poc", valid=(_entry("a"), _entry("b")),
                       invalid=(InvalidPackage("x", "x.poc.toml", "bad"),))
        curr = Catalog(poc_dir="/poc", valid=(_entry("a"), _entry("b")),
                       invalid=(InvalidPackage("x", "x.poc.toml", "bad"),))
        summary = rescan_summary(prev, curr)
        self.assertEqual(summary.valid_count, 2)
        self.assertEqual(summary.invalid_count, 1)
        self.assertEqual(summary.changed_count, 0)

    def test_changed_detected_with_prefix(self):
        prev = Catalog(poc_dir="/poc", valid=(_entry("a", script_sha="11" * 32),))
        curr = Catalog(poc_dir="/poc", valid=(_entry("a", script_sha="22" * 32),))
        summary = rescan_summary(prev, curr)
        self.assertEqual(summary.changed_count, 1)
        c = summary.changed[0]
        self.assertEqual(c.poc_id, "a")
        self.assertEqual(c.prev_script_prefix, "11" * 6)
        self.assertEqual(c.curr_script_prefix, "22" * 6)
        # 전체 hash도 evidence용으로 보존.
        self.assertEqual(c.prev_script_sha256, "11" * 32)
        self.assertEqual(c.curr_script_sha256, "22" * 32)

    def test_new_package_not_counted_as_changed(self):
        prev = Catalog(poc_dir="/poc", valid=(_entry("a"),))
        curr = Catalog(poc_dir="/poc", valid=(_entry("a"), _entry("b")))
        summary = rescan_summary(prev, curr)
        self.assertEqual(summary.changed_count, 0)
        self.assertEqual(summary.valid_count, 2)


if __name__ == "__main__":
    unittest.main()
