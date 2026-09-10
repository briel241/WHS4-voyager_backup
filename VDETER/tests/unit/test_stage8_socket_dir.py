"""Stage 8 socket root·workload dir 권한·symlink·path 검증 시험(명세 §4.2; REQ-03).

Linux 전용(os.getuid·POSIX mode). 비-Linux(Windows)에서는 skip한다.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest

_LINUX = hasattr(os, "getuid")

if _LINUX:
    from vdeter.stage8._supervisor import socket_dir as sd
    from vdeter.stage8.poc_api.errors import PocError, PocErrorCode


@unittest.skipUnless(_LINUX, "Linux 전용 socket-dir 시험")
class SocketDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vdeter-poc-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_S8_POC_API_REQ_03_path_length_check(self):
        sd.check_path_length("/tmp/short.sock")  # 통과
        long_path = "/tmp/" + ("a" * 120) + "/poc.sock"
        with self.assertRaises(PocError) as ctx:
            sd.check_path_length(long_path)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TRANSPORT_UNAVAILABLE)

    def test_select_root_fallback_when_xdg_absent(self):
        uid = os.getuid()
        old = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            self.assertEqual(sd.select_socket_root(uid), f"/tmp/vdeter-poc-{uid}")
        finally:
            if old is not None:
                os.environ["XDG_RUNTIME_DIR"] = old

    def test_select_root_uses_secure_xdg(self):
        uid = os.getuid()
        xdg = os.path.join(self.tmp, "xdg")
        os.mkdir(xdg, mode=0o700)
        os.chmod(xdg, 0o700)
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = xdg
        try:
            self.assertEqual(sd.select_socket_root(uid), os.path.join(xdg, "vdeter-poc"))
        finally:
            if old is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = old

    def test_ensure_root_rejects_symlink(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target, mode=0o700)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        with self.assertRaises(PocError):
            sd.ensure_socket_root(link, os.getuid())

    def test_group_writable_root_rejected(self):
        root = os.path.join(self.tmp, "root")
        os.mkdir(root, mode=0o770)
        os.chmod(root, 0o770)  # group-writable
        self.assertFalse(sd._is_real_dir_owned_securely(root, os.getuid()))

    def test_S8_POC_API_REQ_03_create_and_cleanup_workload_socket(self):
        root = os.path.join(self.tmp, "root")
        ws = sd.create_workload_socket(root, os.getuid())
        self.assertTrue(os.path.isdir(ws.workload_dir))
        mode = stat.S_IMODE(os.lstat(ws.workload_dir).st_mode)
        self.assertEqual(mode, 0o700)
        self.assertFalse(os.path.lexists(ws.socket_path))  # bind 전
        self.assertEqual(len(ws.nonce_digest()), 16)
        self.assertEqual(ws.socket_path, os.path.join(ws.workload_dir, "poc.sock"))
        sd.cleanup_workload_socket(ws)
        self.assertFalse(os.path.lexists(ws.workload_dir))


if __name__ == "__main__":
    unittest.main()
