"""catalog 비재귀·명시적 scan 시험(실행 명세 §6.1, `S8-POC-REQ-21/22`).

owner/mode/symlink/hard-link·count/size·no-import를 fail-closed로 검증한다. POSIX 전용
(os.getuid, O_NOFOLLOW, dir_fd, symlink, chmod)이라 Windows에서는 skip한다.
"""

from __future__ import annotations

import os
import shutil
import stat as statmod
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_POSIX = hasattr(os, "getuid") and sys.platform != "win32"

if _POSIX:
    from vdeter.poc.execution import catalog as catmod
    from vdeter.poc.execution.catalog import (
        CatalogError,
        binding_digest,
        scan_catalog,
    )
    from vdeter.poc.execution.limits import (
        MAX_CATALOG_PACKAGES,
        MAX_MANIFEST_BYTES,
        MAX_SCRIPT_BYTES,
    )
    from vdeter.poc.testing.execution_fakes import poc_toml, write_poc_package


@unittest.skipUnless(_POSIX, "catalog scan은 POSIX 전용")
class CatalogScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="poc-cat-")
        os.chmod(self.dir, 0o700)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_valid_package(self):
        write_poc_package(self.dir, "replay_attack", poc_id="replay-attack")
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 1)
        self.assertEqual(len(cat.invalid), 0)
        entry = cat.valid[0]
        self.assertEqual(entry.poc_id, "replay-attack")
        self.assertEqual(len(entry.script_sha256), 64)
        self.assertEqual(entry.binding_digest,
                         binding_digest(entry.script_sha256, entry.manifest_sha256))

    def test_owner_mismatch_rejected(self):
        write_poc_package(self.dir, "a", poc_id="a")
        with self.assertRaises(CatalogError) as ctx:
            scan_catalog(self.dir, uid=os.getuid() + 12345)
        self.assertIn("소유자", ctx.exception.reason)

    def test_group_other_writable_dir_rejected(self):
        write_poc_package(self.dir, "a", poc_id="a")
        os.chmod(self.dir, 0o777)
        with self.assertRaises(CatalogError) as ctx:
            scan_catalog(self.dir)
        self.assertIn("writable", ctx.exception.reason)

    def test_symlink_directory_rejected(self):
        real = tempfile.mkdtemp(prefix="poc-real-")
        os.chmod(real, 0o700)
        link = self.dir + "-link"
        os.symlink(real, link)
        try:
            with self.assertRaises(CatalogError):
                scan_catalog(link)
        finally:
            os.remove(link)
            shutil.rmtree(real, ignore_errors=True)

    def test_missing_directory_rejected(self):
        with self.assertRaises(CatalogError):
            scan_catalog(os.path.join(self.dir, "nope"))

    def test_symlink_script_is_invalid(self):
        # manifest는 정상, script는 symlink → 해당 package invalid(§6.1).
        target = os.path.join(self.dir, "target.py")
        with open(target, "wb") as fh:
            fh.write(b"class Poc: pass\n")
        os.symlink(target, os.path.join(self.dir, "sneaky.py"))
        with open(os.path.join(self.dir, "sneaky.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="sneaky", script_basename="sneaky.py"))
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 0)
        self.assertEqual(len(cat.invalid), 1)
        self.assertIn("sneaky", cat.invalid[0].name)

    def test_writable_package_files_are_accepted(self):
        """group/other writable인 PoC 파일도 나머지 계약을 충족하면 수락한다.

        2026-09-13 정책: 쓰기 권한 bit는 실행 거부 사유가 아니다. 파일 종류·소유자·
        symlink·크기·읽는 도중 변조 검사와 catalog directory mode 검사는 그대로다.
        """
        for filename in ("p.py", "p.poc.toml"):
            with self.subTest(filename=filename):
                write_poc_package(self.dir, "p", poc_id="p")
                os.chmod(os.path.join(self.dir, filename), 0o666)
                cat = scan_catalog(self.dir)
                self.assertEqual([p.poc_id for p in cat.valid], ["p"],
                                 [i.reason for i in cat.invalid])
                self.assertEqual(len(cat.invalid), 0)
                entry = cat.valid[0]
                self.assertEqual(entry.binding_digest,
                                 binding_digest(entry.script_sha256,
                                                entry.manifest_sha256))
                os.chmod(os.path.join(self.dir, filename), 0o600)

    def test_same_result_under_umask_0002_and_0022(self):
        """실행 셸의 umask와 무관하게 같은 결과여야 한다(2026-09-13 정책).

        umask 0002(비대화형 ssh 기본값)에서 만들어진 group-writable package도 0022와
        똑같이 수락된다. 시험 안에서만 umask를 바꾸고 즉시 되돌린다 — 시스템 설정은
        건드리지 않는다.
        """
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        results, modes = {}, {}
        for mask in (0o022, 0o002):
            os.umask(mask)
            directory = tempfile.mkdtemp(prefix="poc-umask-")
            os.chmod(directory, 0o700)
            self.addCleanup(shutil.rmtree, directory, True)
            write_poc_package(directory, "p", poc_id="p")
            modes[mask] = os.stat(
                os.path.join(directory, "p.py")).st_mode & 0o777
            cat = scan_catalog(directory)
            results[mask] = ([p.poc_id for p in cat.valid],
                             [i.reason for i in cat.invalid])
            self.assertEqual(results[mask], (["p"], []),
                             f"umask {mask:04o}, mode {modes[mask]:04o}")
        self.assertEqual(results[0o022], results[0o002])
        # 이 시험이 실제로 정책을 검증했는지 확인한다: 0002에서는 group-writable
        # 파일이, 0022에서는 그렇지 않은 파일이 만들어져야 한다.
        self.assertTrue(modes[0o002] & statmod.S_IWGRP, f"{modes[0o002]:04o}")
        self.assertFalse(modes[0o022] & statmod.S_IWGRP, f"{modes[0o022]:04o}")

    def test_file_owner_mismatch_is_invalid(self):
        write_poc_package(self.dir, "p", poc_id="p")
        real_fstat = os.fstat

        def foreign_owner(fd):
            st = real_fstat(fd)
            if statmod.S_ISREG(st.st_mode):
                return SimpleNamespace(st_mode=st.st_mode, st_uid=os.getuid() + 1)
            return st

        with mock.patch.object(catmod.os, "fstat", side_effect=foreign_owner):
            cat = scan_catalog(self.dir)
        self.assertFalse(cat.valid)
        self.assertIn("소유자", cat.invalid[0].reason)

    def test_read_failure_is_invalid_package(self):
        write_poc_package(self.dir, "p", poc_id="p")
        with mock.patch.object(catmod.os, "read", side_effect=OSError("I/O failure")):
            cat = scan_catalog(self.dir)
        self.assertFalse(cat.valid)
        self.assertEqual(len(cat.invalid), 1)

    def test_inplace_mutation_during_read_is_invalid(self):
        write_poc_package(self.dir, "p", poc_id="p")
        real_read = os.read
        changed = False

        def change_mode(fd, count):
            nonlocal changed
            data = real_read(fd, count)
            if not changed:
                changed = True
                os.chmod(os.path.join(self.dir, "p.py"), 0o666)
            return data

        with mock.patch.object(catmod.os, "read", side_effect=change_mode):
            cat = scan_catalog(self.dir)
        self.assertFalse(cat.valid)
        self.assertIn("변경", cat.invalid[0].reason)

    def test_non_recursive(self):
        sub = os.path.join(self.dir, "sub")
        os.makedirs(sub)
        os.chmod(sub, 0o700)
        write_poc_package(sub, "deep", poc_id="deep")
        write_poc_package(self.dir, "top", poc_id="top")
        cat = scan_catalog(self.dir)
        ids = {p.poc_id for p in cat.valid}
        self.assertEqual(ids, {"top"})  # subdir는 열거하지 않음

    def test_missing_script_invalid(self):
        with open(os.path.join(self.dir, "lonely.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="lonely", script_basename="lonely.py"))
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 0)
        self.assertEqual(len(cat.invalid), 1)

    def test_duplicate_poc_id(self):
        write_poc_package(self.dir, "one", poc_id="dup")
        write_poc_package(self.dir, "two", poc_id="dup")
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 1)
        self.assertEqual(len(cat.invalid), 1)
        self.assertIn("중복", cat.invalid[0].reason)

    def test_hardlink_identity_collision(self):
        write_poc_package(self.dir, "orig", poc_id="orig")
        # 다른 package가 orig.py를 hard-link로 재사용 → identity 충돌.
        os.link(os.path.join(self.dir, "orig.py"), os.path.join(self.dir, "clone.py"))
        with open(os.path.join(self.dir, "clone.poc.toml"), "w", encoding="utf-8") as fh:
            fh.write(poc_toml(poc_id="clone", script_basename="clone.py"))
        cat = scan_catalog(self.dir)
        reasons = [p.reason for p in cat.invalid]
        self.assertTrue(any("hard-link" in r for r in reasons))

    def test_count_boundary(self):
        for i in range(MAX_CATALOG_PACKAGES + 1):
            write_poc_package(self.dir, f"pkg{i:03d}", poc_id=f"pkg-{i:03d}")
        cat = scan_catalog(self.dir)
        self.assertEqual(cat.total_manifests_found, MAX_CATALOG_PACKAGES + 1)
        self.assertEqual(cat.examined, MAX_CATALOG_PACKAGES)
        self.assertTrue(cat.truncated)

    def test_count_cap_preserves_sorted_selection_without_full_listing(self):
        for i in reversed(range(MAX_CATALOG_PACKAGES + 3)):
            write_poc_package(self.dir, f"pkg{i:03d}", poc_id=f"pkg-{i:03d}")
        with mock.patch.object(catmod.os, "listdir", side_effect=AssertionError("unbounded listing")):
            cat = scan_catalog(self.dir)
        self.assertEqual(cat.total_manifests_found, MAX_CATALOG_PACKAGES + 3)
        self.assertEqual([p.poc_id for p in cat.valid],
                         [f"pkg-{i:03d}" for i in range(MAX_CATALOG_PACKAGES)])

    def test_script_size_boundary(self):
        big = b"# " + b"x" * MAX_SCRIPT_BYTES  # > 1 MiB
        write_poc_package(self.dir, "big", poc_id="big", script=big)
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 0)
        self.assertTrue(any("상한" in p.reason for p in cat.invalid))

    def test_manifest_size_boundary(self):
        text = poc_toml(poc_id="mbig", script_basename="mbig.py") + "\n# " + "y" * MAX_MANIFEST_BYTES
        write_poc_package(self.dir, "mbig", poc_id="mbig", manifest_text=text)
        cat = scan_catalog(self.dir)
        self.assertEqual(len(cat.valid), 0)
        self.assertTrue(any("상한" in p.reason for p in cat.invalid))

    def test_scan_does_not_import_python(self):
        # import되면 예외를 던지는 script. scan은 bytes만 읽으므로 import하지 않는다.
        poison = b"raise RuntimeError('poc must not be imported during scan')\n"
        write_poc_package(self.dir, "poison_poc", poc_id="poison-poc", script=poison)
        before = set(sys.modules)
        cat = scan_catalog(self.dir)  # 예외 없이 완료해야 한다
        after = set(sys.modules)
        self.assertEqual(len(cat.valid), 1)  # manifest는 정상 → valid
        # poc module이 sys.modules에 새로 로드되지 않았다.
        new_mods = after - before
        self.assertFalse(any("poison" in m for m in new_mods), new_mods)


if __name__ == "__main__":
    unittest.main()
