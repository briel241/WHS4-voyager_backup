"""socket root·workload directory 선택과 권한·symlink·path 검증(명세 §4.2).

- socket root: (1) 현재 UID 소유이고 group/other가 쓸 수 없는 `XDG_RUNTIME_DIR`,
  (2) 아니면 `/tmp/vdeter-poc-<uid>`.
- fallback root는 symlink가 아닌 실제 directory로 원자적으로 만들고 owner·mode를 매번
  확인한다.
- workload마다 예측 불가능한 128-bit nonce directory(0700)를 만들고 `poc.sock`(0600)을 둔다.
- bind 전에 pathname encoded 길이를 확인한다.
- 기존 임의 path를 자동 unlink하지 않는다. 새 workload directory 안에 예상 socket이
  이미 있으면 collision으로 fail-closed한다.

Linux 전용(os.getuid·stat mode). 비-Linux에서는 import는 되지만 사용 시 OSError가 난다.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass

from ..poc_api.errors import PocError, PocErrorCode
from ..poc_api.limits import (
    MAX_UNIX_PATH_BYTES,
    SOCKET_MODE,
    SOCKET_ROOT_MODE,
    WORKLOAD_DIR_MODE,
    WORKLOAD_NONCE_BYTES,
)

SOCKET_FILENAME = "poc.sock"


def _transport_error(message: str, **detail) -> PocError:
    return PocError(PocErrorCode.POC_TRANSPORT_UNAVAILABLE, message, detail=detail)


def current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise _transport_error("이 플랫폼에는 getuid가 없습니다(Linux 전용)")
    return getuid()


def check_path_length(path: str) -> None:
    """sun_path 길이(NUL 포함)를 bind 전에 확인한다(§4.2)."""

    if len(path.encode("utf-8")) + 1 > MAX_UNIX_PATH_BYTES:
        raise _transport_error(
            "Unix socket path가 상한을 넘습니다",
            length=len(path.encode("utf-8")),
            limit=MAX_UNIX_PATH_BYTES,
        )


def _is_real_dir_owned_securely(path: str, uid: int) -> bool:
    """path가 symlink가 아닌 실제 directory이고 uid 소유이며 group/other 비기록."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid != uid:
        return False
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


def select_socket_root(uid: int | None = None) -> str:
    """socket root 경로를 고른다(§4.2). 생성은 :func:`ensure_socket_root`가 한다."""

    if uid is None:
        uid = current_uid()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and _is_real_dir_owned_securely(xdg, uid):
        return os.path.join(xdg, "vdeter-poc")
    return f"/tmp/vdeter-poc-{uid}"


def ensure_socket_root(root: str, uid: int | None = None) -> None:
    """socket root를 symlink 아닌 실제 dir(0700)로 만들고 owner·mode를 매번 확인한다."""

    if uid is None:
        uid = current_uid()
    # 중간 단계도 symlink가 아니어야 한다.
    if os.path.islink(root):
        raise _transport_error("socket root가 symlink입니다", root=root)
    os.makedirs(root, mode=SOCKET_ROOT_MODE, exist_ok=True)
    os.chmod(root, SOCKET_ROOT_MODE)
    if not _is_real_dir_owned_securely(root, uid):
        raise _transport_error("socket root 소유/권한 검증 실패", root=root)


def _path_has_symlink_component(path: str) -> bool:
    """path의 어떤 구성요소라도 symlink이면 True."""

    current = path
    seen = set()
    while current and current not in ("/", ""):
        if current in seen:
            break
        seen.add(current)
        if os.path.islink(current):
            return True
        current = os.path.dirname(current)
    return False


@dataclass
class WorkloadSocket:
    root: str
    workload_dir: str
    socket_path: str
    nonce_hex: str

    def nonce_digest(self) -> str:
        """evidence용 nonce digest(실제 nonce 원문 대신 기록, §4.2)."""

        return hashlib.sha256(self.nonce_hex.encode("ascii")).hexdigest()[:16]

    def evidence(self) -> dict:
        return {
            "socket_type": "AF_UNIX/SOCK_SEQPACKET",
            "root_mode": oct(SOCKET_ROOT_MODE),
            "workload_dir_mode": oct(WORKLOAD_DIR_MODE),
            "socket_mode": oct(SOCKET_MODE),
            "nonce_digest": self.nonce_digest(),
        }


def create_workload_socket(root: str, uid: int | None = None) -> WorkloadSocket:
    """root 아래 nonce workload dir(0700)와 socket path를 만든다(bind 전 단계).

    symlink·path-length·collision을 fail-closed로 검증한다.
    """

    if uid is None:
        uid = current_uid()
    ensure_socket_root(root, uid)
    nonce_hex = secrets.token_hex(WORKLOAD_NONCE_BYTES)
    workload_dir = os.path.join(root, nonce_hex)
    if os.path.lexists(workload_dir):
        raise _transport_error("workload directory nonce 충돌", workload_dir=workload_dir)
    os.mkdir(workload_dir, mode=WORKLOAD_DIR_MODE)
    os.chmod(workload_dir, WORKLOAD_DIR_MODE)
    socket_path = os.path.join(workload_dir, SOCKET_FILENAME)
    check_path_length(socket_path)
    if _path_has_symlink_component(workload_dir):
        raise _transport_error("socket path에 symlink 구성요소가 있습니다", path=socket_path)
    if os.path.lexists(socket_path):
        # 새 workload dir 안에 예상 socket이 이미 있으면 collision으로 fail-closed(§4.2).
        raise _transport_error("예상 socket이 이미 존재합니다(collision)", path=socket_path)
    return WorkloadSocket(
        root=root, workload_dir=workload_dir, socket_path=socket_path, nonce_hex=nonce_hex
    )


def cleanup_workload_socket(ws: WorkloadSocket) -> None:
    """socket entry와 workload directory를 제거한다(§4.2; close 뒤 호출)."""

    try:
        os.unlink(ws.socket_path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        os.rmdir(ws.workload_dir)
    except FileNotFoundError:
        pass
    except OSError:
        pass


__all__ = (
    "SOCKET_FILENAME",
    "current_uid",
    "check_path_length",
    "select_socket_root",
    "ensure_socket_root",
    "WorkloadSocket",
    "create_workload_socket",
    "cleanup_workload_socket",
)
