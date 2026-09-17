"""immutable snapshot과 SHA-256 결속·race fail-closed 시험(실행 명세 §5.3, `S8-POC-REQ-24/25`).

O_NOFOLLOW·inode 대조는 POSIX 전용이라 Windows에서는 skip한다.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

_POSIX = hasattr(os, "getuid") and sys.platform != "win32"

if _POSIX:
    from vdeter.poc.execution import snapshot as snapmod
    from vdeter.poc.execution.catalog import binding_digest
    from vdeter.poc.execution.snapshot import (
        SnapshotError,
        create_snapshot,
        verify_snapshot,
    )
    from vdeter.poc.testing.execution_fakes import poc_toml, write_poc_package


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
            st = real_fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                return st
            calls["n"] += 1
            # 첫 파일(script)의 'after' 호출(=2번째)에서 size가 커진 것처럼.
            if calls["n"] == 2:
                return _St(st, size=st.st_size + 10)
            return _St(st)

        with mock.patch.object(snapmod.os, "fstat", side_effect=fake_fstat):
            with self.assertRaises(SnapshotError) as ctx:
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        self.assertIn("부분 저장", ctx.exception.reason)

    def test_base_name_path_traversal_rejected_before_open(self):
        """base_name path traversal은 파일을 열기 전에 거부한다(§6.1, S8-POC-REQ-01/21).

        이전에는 manifest entrypoint 대조로 **우연히만** 막혔고 그 전에 이미 catalog 밖
        임의 상대경로 파일을 열어 읽었다.
        """
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        outside = os.path.join(self.dest_root, "outside")
        os.makedirs(outside, exist_ok=True)
        write_poc_package(outside, "secret", poc_id="secret")
        for bad in ("../outside/secret", "..", ".", "a/b", "./x", "p\n",
                    os.path.join(outside, "secret")):
            with self.subTest(base=bad):
                with self.assertRaises(SnapshotError) as ctx:
                    create_snapshot(self.poc, bad, dest_dir=self._dest("w-trav"))
                # 경로 검증 단계에서 거부되어야 한다(manifest 검증까지 가면 안 됨).
                self.assertTrue(
                    ("traversal" in ctx.exception.reason
                     or "구분자" in ctx.exception.reason
                     or "상위 경로" in ctx.exception.reason),
                    f"열기 전 거부가 아님: {ctx.exception.reason}")

    def test_valid_base_name_still_accepted(self):
        write_poc_package(self.poc, "good_poc", poc_id="good-poc")
        snap = create_snapshot(self.poc, "good_poc", dest_dir=self._dest("w-ok"))
        self.assertEqual(len(snap.script_sha256), 64)

    def test_group_other_writable_source_files_are_accepted(self):
        """group/other writable인 PoC 원본도 snapshot을 만들 수 있다(2026-09-13 정책).

        쓰기 권한 bit는 거부 사유가 아니다. 결속 digest·검증은 그대로이고 snapshot
        사본은 여전히 read-only(0o400)로 확정된다.
        """
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        for name in ("p.py", "p.poc.toml"):
            os.chmod(os.path.join(self.poc, name), 0o666)
        snap = create_snapshot(self.poc, "p", dest_dir=self._dest("w-writable"))
        self.assertEqual(len(snap.script_sha256), 64)
        self.assertEqual(snap.binding_digest,
                         binding_digest(snap.script_sha256, snap.manifest_sha256))
        self.assertTrue(verify_snapshot(snap))
        self.assertEqual(os.stat(snap.script_path).st_mode & 0o777, 0o400)

    def test_same_snapshot_under_umask_0002_and_0022(self):
        """실행 셸의 umask와 무관하게 같은 snapshot 결과여야 한다(2026-09-13 정책).

        시험 안에서만 umask를 바꾸고 즉시 되돌린다 — 시스템 설정은 건드리지 않는다.
        """
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        digests, modes = {}, {}
        for mask in (0o022, 0o002):
            os.umask(mask)
            source = tempfile.mkdtemp(prefix="poc-umask-src-")
            os.chmod(source, 0o700)
            self.addCleanup(shutil.rmtree, source, True)
            write_poc_package(source, "p", poc_id="p",
                              script=b"class Poc:\n    pass\n")
            modes[mask] = os.stat(os.path.join(source, "p.py")).st_mode & 0o777
            snap = create_snapshot(source, "p",
                                   dest_dir=self._dest(f"w-{mask:04o}"))
            digests[mask] = snap.binding_digest
            self.assertTrue(verify_snapshot(snap))
        self.assertEqual(digests[0o022], digests[0o002])
        # 이 시험이 실제로 정책을 검증했는지 확인한다.
        self.assertTrue(modes[0o002] & stat.S_IWGRP, f"{modes[0o002]:04o}")
        self.assertFalse(modes[0o022] & stat.S_IWGRP, f"{modes[0o022]:04o}")

    def test_source_directory_permission_rechecked(self):
        write_poc_package(self.poc, "p", poc_id="p")
        os.chmod(self.poc, 0o777)
        with self.assertRaises(SnapshotError), mock.patch.object(snapmod.os, "read") as read:
            create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        read.assert_not_called()

    def test_source_directory_owner_rechecked(self):
        write_poc_package(self.poc, "p", poc_id="p")
        with self.assertRaises(SnapshotError), mock.patch.object(snapmod.os, "read") as read:
            create_snapshot(self.poc, "p", dest_dir=self._dest("w1"), uid=os.getuid() + 1)
        read.assert_not_called()

    def test_source_mode_changed_during_read_rejected(self):
        write_poc_package(self.poc, "p", poc_id="p")
        real_read = os.read

        def change_mode(fd, count):
            data = real_read(fd, count)
            os.chmod(os.path.join(self.poc, "p.py"), 0o666)
            return data

        with mock.patch.object(snapmod.os, "read", side_effect=change_mode):
            with self.assertRaises(SnapshotError):
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))

    def test_read_io_failure_is_snapshot_error(self):
        write_poc_package(self.poc, "p", poc_id="p")
        with mock.patch.object(snapmod.os, "read", side_effect=OSError("read failure")):
            with self.assertRaises(SnapshotError):
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))

    def test_script_mutation_while_manifest_is_read_rejected(self):
        write_poc_package(self.poc, "p", poc_id="p")
        script_path = os.path.join(self.poc, "p.py")
        manifest_ino = os.stat(os.path.join(self.poc, "p.poc.toml")).st_ino
        real_read = os.read
        changed = False

        def mutate_script(fd, count):
            nonlocal changed
            data = real_read(fd, count)
            if not changed and os.fstat(fd).st_ino == manifest_ino:
                changed = True
                with open(script_path, "ab") as fh:
                    fh.write(b"\n# changed while manifest was read\n")
            return data

        with mock.patch.object(snapmod.os, "read", side_effect=mutate_script):
            with self.assertRaises(SnapshotError):
                create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        self.assertFalse(os.path.exists(self._dest("w1")))

    def test_destination_directory_symlink_rejected(self):
        write_poc_package(self.poc, "p", poc_id="p")
        outside = self._dest("outside")
        os.mkdir(outside)
        os.symlink(outside, self._dest("w1"))
        with self.assertRaises(SnapshotError):
            create_snapshot(self.poc, "p", dest_dir=self._dest("w1"))
        self.assertEqual(os.listdir(outside), [])

    def test_destination_directory_writable_rejected(self):
        write_poc_package(self.poc, "p", poc_id="p")
        dest = self._dest("w1")
        os.mkdir(dest)
        os.chmod(dest, 0o777)
        with self.assertRaises(SnapshotError):
            create_snapshot(self.poc, "p", dest_dir=dest)
        self.assertEqual(os.listdir(dest), [])

    def test_dest_preexisting_file_rejected(self):
        # 목적지에 기존 파일이 있으면 덮어쓰지 않고 fail-closed(§5.3, O_EXCL).
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        dest = self._dest("w1")
        # 목적지 directory는 production과 같은 명시 mode로 만든다(production도
        # `os.makedirs(dest_dir, mode=0o700)`). mode를 생략하면 umask 0002에서
        # group-writable이 되어 목적지 검사에 먼저 걸리고, 이 시험이 검증하려는
        # "기존 파일 거부"에 도달하지 못한다.
        os.makedirs(dest, mode=0o700, exist_ok=True)
        # snapshot이 쓸 대상(p.py)을 미리 만들어 둔다.
        with open(os.path.join(dest, "p.py"), "wb") as fh:
            fh.write(b"pre-existing")
        with self.assertRaises(SnapshotError) as ctx:
            create_snapshot(self.poc, "p", dest_dir=dest)
        self.assertIn("이미 존재", ctx.exception.reason)

    def test_dest_symlink_rejected(self):
        # 목적지 대상이 symlink면 링크 대상으로 새지 않고 fail-closed(O_NOFOLLOW/O_EXCL).
        write_poc_package(self.poc, "p", poc_id="p", script=b"class Poc:\n    pass\n")
        dest = self._dest("w1")
        os.makedirs(dest, mode=0o700, exist_ok=True)  # umask와 무관한 목적지 mode
        outside = os.path.join(self.dest_root, "outside.py")
        with open(outside, "wb") as fh:
            fh.write(b"outside")
        os.symlink(outside, os.path.join(dest, "p.py"))
        with self.assertRaises(SnapshotError):
            create_snapshot(self.poc, "p", dest_dir=dest)
        # 링크 대상은 침범되지 않았다.
        with open(outside, "rb") as fh:
            self.assertEqual(fh.read(), b"outside")

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
