"""workload 시작 직전 immutable snapshot과 SHA-256 결속(실행 명세 §5.3, `S8-POC-REQ-24/25`).

- 각 workload 제출 시점에 두 파일을 O_NOFOLLOW로 다시 읽고 함께 검증한다.
- read 전후의 device/inode/size/mtime과 regular-file·owner·mode를 대조한다. 읽는 동안
  하나라도 바뀌면 부분 저장·교체 race로 보고 RF 전에 거부한다(fail-closed).
- **PoC 원본 파일의 group/other 쓰기 권한은 거부 사유가 아니다**(2026-09-13 정책). 위
  전후 대조는 무결성 검사이지 권한 정책이 아니며, catalog·snapshot **directory**의 mode
  검사는 그대로 유지된다. snapshot 사본은 계속 read-only(0o400)로 확정한다.
- script·manifest 각각의 SHA-256과 두 파일 결속 digest를 기록한다.
- child는 원본이 아니라 이 snapshot을 실행한다. snapshot 확정 뒤 원본을 수정해도 현재
  workload에는 반영되지 않으며, 수정본은 다음 workload의 새 snapshot에서만 반영된다.
- hot reload·module reload·callback 교체는 이 계약 밖이다(§5.3, 상위 runner가 강제).

owner/mode/symlink·inode 대조는 POSIX에 의존하므로 Linux에서 실행한다.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as statmod
import sys
from dataclasses import dataclass

from ..poc_api.types import PocManifestDescriptor
from .catalog import (
    MANIFEST_SUFFIX,
    SCRIPT_SUFFIX,
    binding_digest,
)
from .limits import MAX_MANIFEST_BYTES, MAX_SCRIPT_BYTES
from .manifest import ManifestError, parse_and_validate


class SnapshotError(Exception):
    """snapshot 생성 실패(fail-closed). reason은 비밀 없는 요약."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


#: package base name은 catalog directory 바로 아래의 **단일 경로 요소**여야 한다(§6.1).
#: path separator·`..`·선행 dot를 배제해 path traversal을 파일을 열기 **전에** 거부한다
#: (`S8-POC-REQ-01/21`). 이전에는 manifest entrypoint 대조로 우연히만 막혔고 그 전에 이미
#: 임의 상대경로 파일을 열어 읽었다.
_SAFE_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _validate_base_name(base_name: str) -> None:
    if not isinstance(base_name, str) or not _SAFE_BASE_RE.fullmatch(base_name):
        raise SnapshotError("base_name이 안전한 단일 경로 요소가 아닙니다(path traversal 거부)")
    if base_name in (".", "..") or ".." in base_name.split("."):
        raise SnapshotError("base_name에 상위 경로 요소를 쓸 수 없습니다")
    if os.sep in base_name or (os.altsep and os.altsep in base_name) or "/" in base_name:
        raise SnapshotError("base_name에 경로 구분자를 쓸 수 없습니다")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """확정된 immutable snapshot(§5.3). child는 이 경로만 실행한다."""

    snapshot_dir: str
    script_path: str
    manifest_path: str
    script_sha256: str
    manifest_sha256: str
    binding_digest: str
    descriptor: PocManifestDescriptor
    interpreter: str
    interpreter_version: str
    source_script_ino: int
    source_manifest_ino: int

    def as_evidence(self) -> dict:
        return {
            "snapshot_dir": self.snapshot_dir,
            "script_sha256": self.script_sha256,
            "manifest_sha256": self.manifest_sha256,
            "binding_digest": self.binding_digest,
            "poc_id": self.descriptor.poc_id,
            "version": self.descriptor.version,
            "entrypoint": self.descriptor.entrypoint,
            "interpreter": self.interpreter,
            "interpreter_version": self.interpreter_version,
        }


def _consistent_read(dir_fd: int, name: str, max_bytes: int, *, uid: int):
    """O_NOFOLLOW openat + read 전후 대조로 일관된 bytes를 얻는다(§5.3).

    반환 (data, sha256, stat). 부분 저장·교체·symlink·비정규 파일은 SnapshotError.
    """

    # O_NONBLOCK: scan 이후 원본이 FIFO/특수 파일로 교체(TOCTOU)되어도 O_RDONLY open이
    # block되지 않게 한다. regular file read는 영향 없음(§5.3 fail-closed, 아래 regular 검사).
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        raise SnapshotError(f"{name} 열기 실패({error.__class__.__name__}; symlink 가능성)")
    try:
        before = os.fstat(fd)
        if not statmod.S_ISREG(before.st_mode):
            raise SnapshotError(f"{name}이 regular file이 아닙니다")
        if before.st_uid != uid:
            raise SnapshotError(f"{name} 소유자가 현재 UID가 아닙니다")
        # PoC 파일의 group/other 쓰기 권한은 **거부 사유가 아니다**(2026-09-13 정책).
        # 읽는 도중 mode가 바뀌면 아래 전후 대조가 여전히 거부한다(무결성 계약 유지).
        if before.st_size > max_bytes:
            raise SnapshotError(f"{name}이 크기 상한을 초과했습니다")

        h = hashlib.sha256()
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SnapshotError(f"{name}이 읽는 도중 크기 상한을 초과했습니다")
            h.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)

        after = os.fstat(fd)
        # read 전후 같은 inode의 size/mtime/identity가 바뀌면 부분 저장(in-place write).
        if (after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ino != before.st_ino
                or after.st_dev != before.st_dev
                or after.st_mode != before.st_mode
                or after.st_uid != before.st_uid):
            raise SnapshotError(f"{name}이 읽는 동안 변경되었습니다(부분 저장)")
        if after.st_size != total:
            raise SnapshotError(f"{name} 크기와 읽은 byte 수가 다릅니다")

        # 이름이 가리키는 현재 inode가 fd의 inode와 다르면 교체(rename over) race.
        try:
            path_st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotError(f"{name} 재stat 실패({error.__class__.__name__})")
        if (path_st.st_ino != before.st_ino) or (path_st.st_dev != before.st_dev):
            raise SnapshotError(f"{name}이 읽는 동안 교체되었습니다(replacement race)")
        if any(getattr(path_st, field) != getattr(before, field)
               for field in ("st_size", "st_mtime_ns", "st_mode", "st_uid")):
            raise SnapshotError(f"{name}이 읽는 동안 변경되었습니다(부분 저장)")

        return data, h.hexdigest(), before
    except OSError as error:
        raise SnapshotError(f"{name} 읽기 실패({error.__class__.__name__})") from error
    finally:
        os.close(fd)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise SnapshotError("snapshot은 POSIX(inode/owner 대조)에서만 지원됩니다")
    return getuid()


def create_snapshot(
    poc_dir: str,
    base_name: str,
    *,
    dest_dir: str,
    scenario_id: str | None = None,
    uid: int | None = None,
) -> Snapshot:
    """`<base>.py`와 `<base>.poc.toml`을 일관되게 읽어 immutable snapshot을 만든다(§5.3).

    `dest_dir`는 workload evidence 아래 snapshot 경로다(호출자가 생성). 두 파일을 복사하고
    read-only mode로 고정한다. scenario_id를 주면 compatibility를 확인한다.
    """

    _validate_base_name(base_name)  # 파일을 열기 전에 path traversal 거부(§6.1)
    uid = _current_uid() if uid is None else uid
    script_name = base_name + SCRIPT_SUFFIX
    manifest_name = base_name + MANIFEST_SUFFIX

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(poc_dir, flags)
    except OSError as error:
        raise SnapshotError(f"catalog directory 열기 실패({error.__class__.__name__})")
    try:
        directory = os.fstat(dir_fd)
        if not statmod.S_ISDIR(directory.st_mode) or directory.st_uid != uid:
            raise SnapshotError("catalog directory 소유자 또는 종류가 잘못되었습니다")
        if directory.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH):
            raise SnapshotError("catalog directory가 group/other writable입니다")
        script_data, script_sha, script_stat = _consistent_read(
            dir_fd, script_name, MAX_SCRIPT_BYTES, uid=uid)
        manifest_data, manifest_sha, manifest_stat = _consistent_read(
            dir_fd, manifest_name, MAX_MANIFEST_BYTES, uid=uid)
        # Recheck both names after the pair has been read. A script change while
        # reading the manifest must not silently bind bytes from different edits.
        for name, before in ((script_name, script_stat), (manifest_name, manifest_stat)):
            after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if any(getattr(before, field) != getattr(after, field)
                   for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid")):
                raise SnapshotError(f"{name}이 snapshot pair를 읽는 동안 변경되었습니다")
    except OSError as error:
        raise SnapshotError(f"snapshot source 확인 실패({error.__class__.__name__})") from error
    finally:
        os.close(dir_fd)

    if (script_stat.st_dev, script_stat.st_ino) == (manifest_stat.st_dev, manifest_stat.st_ino):
        raise SnapshotError("script와 manifest가 같은 inode입니다")

    try:
        descriptor = parse_and_validate(
            manifest_data,
            script_basename=script_name,
            script_sha256=script_sha,
            manifest_sha256=manifest_sha,
        )
    except ManifestError as error:
        raise SnapshotError(f"manifest 검증 실패: {error.reason}")

    if scenario_id is not None and scenario_id not in descriptor.compatible_scenarios:
        raise SnapshotError(f"scenario {scenario_id}가 compatible_scenarios에 없습니다")

    bind = binding_digest(script_sha, manifest_sha)

    # snapshot 복사(immutable): dest_dir에 script/manifest를 쓰고 read-only로 고정.
    snap_script = os.path.join(dest_dir, script_name)
    snap_manifest = os.path.join(dest_dir, manifest_name)
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        dest_fd = os.open(dest_dir, flags)
    except OSError as error:
        raise SnapshotError(f"snapshot directory 생성 또는 열기 실패({error.__class__.__name__})") from error
    try:
        destination = os.fstat(dest_fd)
        if (not statmod.S_ISDIR(destination.st_mode) or destination.st_uid != uid
                or destination.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH)):
            raise SnapshotError("snapshot directory 소유자 또는 mode가 안전하지 않습니다")
        _write_immutable(script_name, script_data, dir_fd=dest_fd)
        _write_immutable(manifest_name, manifest_data, dir_fd=dest_fd)
    finally:
        os.close(dest_fd)

    return Snapshot(
        snapshot_dir=dest_dir,
        script_path=snap_script,
        manifest_path=snap_manifest,
        script_sha256=script_sha,
        manifest_sha256=manifest_sha,
        binding_digest=bind,
        descriptor=descriptor,
        interpreter=sys.executable or "",
        interpreter_version=sys.version.split()[0],
        source_script_ino=script_stat.st_ino,
        source_manifest_ino=manifest_stat.st_ino,
    )


def _write_immutable(path: str, data: bytes, *, dir_fd: int | None = None) -> None:
    """snapshot 대상 파일을 fail-closed로 생성한다(§5.3).

    O_CREAT|O_EXCL|O_NOFOLLOW로 **새 파일만** 만든다. 대상에 기존 파일·symlink가 있으면
    덮어쓰지 않고 :class:`SnapshotError`(O_TRUNC로 남의 파일/링크를 침범하지 않는다).
    short write를 검사해 전체가 기록됐는지 보장한다.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o400, dir_fd=dir_fd)
    except FileExistsError:
        raise SnapshotError(f"snapshot 대상이 이미 존재합니다: {os.path.basename(path)}")
    except OSError as error:
        raise SnapshotError(f"snapshot 파일 생성 실패({error.__class__.__name__})")
    try:
        view = memoryview(data)
        total = 0
        while total < len(view):
            written = os.write(fd, view[total:])
            if written <= 0:
                raise SnapshotError("snapshot write가 진행되지 않음(short write)")
            total += written
        os.fchmod(fd, 0o400)
    except OSError as error:
        raise SnapshotError(f"snapshot 파일 기록 실패({error.__class__.__name__})") from error
    finally:
        os.close(fd)


def verify_snapshot(snapshot: Snapshot) -> bool:
    """snapshot 파일이 기록된 SHA-256과 일치하는지 확인한다(원본 수정 미반영 증거).

    child가 실행하는 snapshot 경로의 현재 내용이 확정 시점 hash와 같으면 True.
    """

    for path, expected in (
        (snapshot.script_path, snapshot.script_sha256),
        (snapshot.manifest_path, snapshot.manifest_sha256),
    ):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            return False
    return True


__all__ = (
    "SnapshotError",
    "Snapshot",
    "create_snapshot",
    "verify_snapshot",
)
