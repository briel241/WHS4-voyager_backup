"""Stage 4 manifest 검증 시험(실행 명세 §6.3, `S8-POC-REQ-01`). platform 무관.

catalog 정상·오류 전수 중 manifest 부분. Python import 없이 fail-closed 검증을 확인한다.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.execution.manifest import ManifestError, parse_and_validate
from vdeter.stage8.testing.execution_fakes import poc_toml

SCRIPT = "replay_attack.py"
SHA = "ab" * 32
MSHA = "cd" * 32


def _parse(text: str, *, script_basename: str = SCRIPT):
    return parse_and_validate(text.encode("utf-8"),
                              script_basename=script_basename,
                              script_sha256=SHA, manifest_sha256=MSHA)


class ManifestValidTest(unittest.TestCase):
    def test_valid_manifest(self):
        d = _parse(poc_toml(script_basename=SCRIPT))
        self.assertEqual(d.poc_id, "replay-attack")
        self.assertEqual(d.version, "1.0.0")
        self.assertEqual(d.attacker_endpoint, "MASS-B")
        self.assertEqual(d.script_sha256, SHA)
        self.assertEqual(d.manifest_sha256, MSHA)
        self.assertIn("SC-VDE-006", d.compatible_scenarios)

    def test_required_capability_subset_ok(self):
        d = _parse(poc_toml(
            capabilities=("observe_packet", "replay_packet", "observe_dual_physical_channel"),
            required_capabilities=("observe_dual_physical_channel",),
        ))
        self.assertIn("observe_dual_physical_channel", d.required_capabilities)


class ManifestFailClosedTest(unittest.TestCase):
    def _assert_bad(self, text, needle=None, **kw):
        with self.assertRaises(ManifestError) as ctx:
            _parse(text, **kw)
        if needle:
            self.assertIn(needle, ctx.exception.reason)

    def test_wrong_schema(self):
        self._assert_bad(poc_toml().replace(
            "vdeter.stage8.poc-manifest.v1", "vdeter.stage8.poc-manifest.v2"), "schema")

    def test_bad_poc_id(self):
        self._assert_bad(poc_toml(poc_id="Bad_ID"), "poc_id")

    def test_bad_version(self):
        self._assert_bad(poc_toml(version="1.0"), "version")

    def test_entrypoint_format(self):
        self._assert_bad(poc_toml(entrypoint="replay_attack:Poc"), "entrypoint")

    def test_entrypoint_file_mismatch(self):
        self._assert_bad(poc_toml(entrypoint="other.py:Poc"), "파일명")

    def test_attacker_not_mass_b(self):
        self._assert_bad(poc_toml(attacker_endpoint="MASS-A"), "MASS-B")

    def test_target_outside_universe(self):
        self._assert_bad(poc_toml(allowed_targets=("ROC", "MASS-B")), "부분집합")

    def test_duplicate_target(self):
        self._assert_bad(poc_toml(allowed_targets=("ROC", "ROC")), "중복")

    def test_unknown_capability(self):
        self._assert_bad(poc_toml(capabilities=("observe_packet", "hack_everything")),
                         "capabilities")

    def test_unknown_trigger(self):
        self._assert_bad(poc_toml(triggers=("SCENARIO_STARTED", "BOOM")), "triggers")

    def test_required_not_subset_of_caps(self):
        self._assert_bad(poc_toml(
            capabilities=("observe_packet",),
            required_capabilities=("replay_packet",)), "부분집합")

    def test_max_actions_over_limit(self):
        self._assert_bad(poc_toml(max_actions_requested=33), "max_actions_requested")

    def test_max_actions_zero(self):
        self._assert_bad(poc_toml(max_actions_requested=0), "max_actions_requested")

    def test_oversize_manifest(self):
        big = poc_toml() + "\n# " + ("x" * (64 * 1024))
        self._assert_bad(big, "64 KiB")

    def test_not_utf8(self):
        with self.assertRaises(ManifestError) as ctx:
            parse_and_validate(b"\xff\xfe not utf8", script_basename=SCRIPT,
                               script_sha256=SHA, manifest_sha256=MSHA)
        self.assertIn("UTF-8", ctx.exception.reason)

    def test_bad_toml(self):
        self._assert_bad("this is = = not toml", "TOML")

    def test_missing_required_field(self):
        text = poc_toml()
        text = "\n".join(l for l in text.splitlines() if not l.startswith("poc_id"))
        self._assert_bad(text, "poc_id")

    def test_max_actions_bool_rejected(self):
        # TOML true는 bool. 정수 아님으로 거부.
        text = poc_toml().replace("max_actions_requested = 10", "max_actions_requested = true")
        self._assert_bad(text, "정수")


if __name__ == "__main__":
    unittest.main()
