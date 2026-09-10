"""workload 시작 직전 immutable snapshot과 SHA-256 결속(실행 명세 §5.3, `S8-POC-REQ-24/25`).

- 각 workload 제출 시점에 두 파일을 O_NOFOLLOW로 다시 읽고 함께 검증한다.
- read 전후의 device/inode/size/mtime과 regular-file·owner·mode를 대조한다. 읽는 동안
  하나라도 바뀌면 부분 저장·교체 race로 보고 RF 전에 거부한다(fail-closed).
- script·manifest 각각의 SHA-256과 두 파일 결속 digest를 기록한다.
- child는 원본이 아니라 이 snapshot을 실행한다. snapshot 확정 뒤 원본을 수정해도 현재
  workload에는 반영되지 않으며, 수정본은 다음 workload의 새 snapshot에서만 반영된다.
- hot reload·module reload·callback 교체는 이 계약 밖이다(§5.3, 상위 runner가 강제).

owner/mode/symlink·inode 대조는 POSIX에 의존하므로 Linux에서 실행한다.
"""

from __future__ import annotations

import hashlib
import os
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

    반환 (data, sha256, dev, ino). 부분 저장·교체·symlink·비정규 파일은 SnapshotError.
    """

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
        if before.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH):
            raise SnapshotError(f"{name}이 group/other writable입니다")
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
                or after.st_dev != before.st_dev):
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

        return data, h.hexdigest(), before.st_dev, before.st_ino
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

    uid = _current_uid() if uid is None else uid
    script_name = base_name + SCRIPT_SUFFIX
    manifest_name = base_name + MANIFEST_SUFFIX

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(poc_dir, flags)
    except OSError as error:
        raise SnapshotError(f"catalog directory 열기 실패({error.__class__.__name__})")
    try:
        script_data, script_sha, s_dev, s_ino = _consistent_read(
            dir_fd, script_name, MAX_SCRIPT_BYTES, uid=uid)
        manifest_data, manifest_sha, m_dev, m_ino = _consistent_read(
            dir_fd, manifest_name, MAX_MANIFEST_BYTES, uid=uid)
    finally:
        os.close(dir_fd)

    if (s_dev, s_ino) == (m_dev, m_ino):
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
    os.makedirs(dest_dir, exist_ok=True)
    snap_script = os.path.join(dest_dir, script_name)
    snap_manifest = os.path.join(dest_dir, manifest_name)
    _write_immutable(snap_script, script_data)
    _write_immutable(snap_manifest, manifest_data)

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
        source_script_ino=s_ino,
        source_manifest_ino=m_ino,
    )


def _write_immutable(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o400)
    except OSError:
        pass


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
