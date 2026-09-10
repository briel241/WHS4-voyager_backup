"""immutable snapshot과 SHA-256 결속·race fail-closed 시험(실행 명세 §5.3, `S8-POC-REQ-24/25`).

O_NOFOLLOW·inode 대조는 POSIX 전용이라 Windows에서는 skip한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_POSIX = hasattr(os, "getuid") and sys.platform != "win32"

if _POSIX:
    from vdeter.stage8.execution import snapshot as snapmod
    from vdeter.stage8.execution.catalog import binding_digest
    from vdeter.stage8.execution.snapshot import (
        SnapshotError,
        create_snapshot,
        verify_snapshot,
    )
    from vdeter.stage8.testing.execution_fakes import poc_toml, write_poc_package


@unittest.skipUnless(_POSIX, "snapshot은 POSIX 전용")
class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-src-")
        os.chmod(self.poc, 0o700)
        self.dest_root = tempfile.mkdtemp(prefix="poc-snap-")

    def tearDown(self):
        shutil.rmtree(self.poc, ignore_errors=True)
        shutil.rmtree(self.dest_root, ignore_errors=True)

    def _dest(self, name):
        return os.path.join(self.dest_root, name)

    def test_creates_immutable_snapshot(self):
        write_poc_package(self.poc, "replay_attack", poc_id="replay-attack",
                          script=b"class Poc:\n    pass\n")
        snap = create_snapshot(self.poc, "replay_attack", dest_dir=self._dest("w1"),
                               scenario_id="SC-VDE-006")
        self.assertEqual(len(snap.script_sha256), 64)
        self.assertEqual(snap.binding_digest,
                         binding_digest(snap.script_sha256, snap.manifest_sha256))
        self.assertTrue(os.path.exists(snap.script_path))
        self.assertTrue(verify_snapshot(snap))
        # snapshot 파일은 read-only(0o400).
        mode = os.stat(snap.script_path).st_mode & 0o777
        self.assertEqual(mode, 0o400)

    def test_original_edit_not_reflected_next_reflected(self):
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    v=1\n")
        snap_a = create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        sha_a = snap_a.script_sha256
        # 원본 수정(다음 workload에서만 반영, §5.3).
        os.chmod(os.path.join(self.poc, "p.py"), 0o600)
        with open(os.path.join(self.poc, "p.py"), "wb") as fh:
            fh.write(b"class Poc:\n    v=2\n")
        # snapshot A는 그대로.
        self.assertTrue(verify_snapshot(snap_a))
        # 다음 workload의 새 snapshot B는 변경 반영.
        snap_b = create_snapshot(self.poc, "p", dest_dir=self._dest("w2"))
        self.assertNotEqual(snap_a.script_sha256, snap_b.script_sha256)
        self.assertEqual(sha_a, snap_a.script_sha256)  # A 불변

    def test_symlink_script_rejected(self):
        target = os.path.join(self.poc, "target.py")
        with open(target, "wb") as fh:
            fh.write(b"class Poc: pass\n")
        os.symlink(target, os.path.join(self.poc, "s.py"))
        with open(os.path.join(self.poc, "s.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="s", script_basename="s.py"))
        with self.assertRaises(SnapshotError):
            create_snapshot(self.poc, "s", dest_dir=self._dest("w1"))

    def test_invalid_manifest_rejected(self):
        write_poc_package(self.poc, "bad", poc_id="bad",
                          manifest_text="schema = \"wrong\"\n")
        with self.assertRaises(SnapshotError):
            create_snapshot(self.poc, "bad", dest_dir=self._dest("w1"))

    def test_incompatible_scenario_rejected(self):
        write_poc_package(self.poc, "p", poc_id="p", compatible_scenarios=("SC-VDE-006",))
        with self.assertRaises(SnapshotError) as ctx:
            create_snapshot(self.poc, "p", dest_dir=self._dest("w1"), scenario_id="SC-VDE-999")
        self.assertIn("compatible", ctx.exception.reason)

    def test_partial_write_detected(self):
        # read 전후 fstat의 size/mtime이 바뀌면 부분 저장으로 거부(§5.3).
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        real_fstat = os.fstat
        calls = {"n": 0}

        class _St:
            def __init__(self, st, size=None, mtime=None):
                self.st_mode = st.st_mode
                self.st_uid = st.st_uid
                self.st_dev = st.st_dev
                self.st_ino = st.st_ino
                self.st_size = size if size is not None else st.st_size
                self.st_mtime_ns = mtime if mtime is not None else st.st_mtime_ns

        def fake_fstat(fd):
            calls["n"] += 1
            st = real_fstat(fd)
            # 첫 파일(script)의 'after' 호출(=2번째)에서 size가 커진 것처럼.
            if calls["n"] == 2:
                return _St(st, size=st.st_size + 10)
            return _St(st)

        with mock.patch.object(snapmod.os, "fstat", side_effect=fake_fstat):
            with self.assertRaises(SnapshotError) as ctx:
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        self.assertIn("부분 저장", ctx.exception.reason)

    def test_replacement_race_detected(self):
        # 이름이 가리키는 inode가 fd inode와 다르면 교체 race로 거부(§5.3).
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        real_stat = os.stat

        class _St:
            def __init__(self, st):
                self.st_ino = st.st_ino + 999999
                self.st_dev = st.st_dev

        def fake_stat(path, *a, **kw):
            return _St(real_stat(path, *a, **kw))

        with mock.patch.object(snapmod.os, "stat", side_effect=fake_stat):
            with self.assertRaises(SnapshotError) as ctx:
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        self.assertIn("교체", ctx.exception.reason)


if __name__ == "__main__":
    unittest.main()
