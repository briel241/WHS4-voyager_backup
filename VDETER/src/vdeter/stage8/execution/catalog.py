"""외부 PoC catalog의 비재귀·명시적 scan(실행 명세 §6.1, `S8-POC-REQ-21/22`).

- qualified source·installation tree 밖 사용자 소유 directory만 사용한다.
- directory 바로 아래의 `.poc.toml`만 비재귀적으로 열거하고, exact-basename `.py`가
  있는 항목만 package 후보로 만든다.
- owner·파일 종류·symlink·hard-link identity·path traversal·count·size를 fail-closed로
  검사한다. **PoC 파일의 group/other 쓰기 권한은 거부 사유가 아니다**(2026-09-13 정책):
  쓰기 가능한 파일도 나머지 계약을 충족하면 정상 수락한다. catalog **directory** 자체의
  mode 검사와 "읽는 도중 변경" 탐지(st_mode 포함 전후 대조)는 유지한다.
- **Python file을 import·compile·실행하지 않는다.** manifest만 parse한다(§6.1).
- watcher·주기 polling·RF 중 background scan을 만들지 않는다. 이 함수는 menu 표시 또는
  명시적 rescan에서 한 번 호출되는 control-plane 동작이다(§3.2).

owner/mode/symlink 검사는 POSIX(os.open dir_fd, O_NOFOLLOW, st_uid, st_mode)에
의존하므로 Linux에서 실행한다. import 자체는 platform 무관이다.
"""

from __future__ import annotations

import hashlib
import heapq
import os
import stat as statmod
from dataclasses import dataclass

from ..poc_api.types import PocManifestDescriptor
from .limits import MAX_CATALOG_PACKAGES, MAX_MANIFEST_BYTES, MAX_SCRIPT_BYTES
from .manifest import ManifestError, parse_and_validate

MANIFEST_SUFFIX = ".poc.toml"
SCRIPT_SUFFIX = ".py"


class CatalogError(Exception):
    """directory 수준 fail-closed 오류(owner·mode·symlink·비디렉터리·부재)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class FileFacts:
    """snapshot cross-check(§5.3)를 위한 파일 identity·상태 기록."""

    path: str
    size: int
    dev: int
    ino: int
    mtime_ns: int
    mode: int
    uid: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """검증을 통과한 package(§6.1). descriptor는 Stage 3 public type."""

    poc_id: str
    version: str
    descriptor: PocManifestDescriptor
    script: FileFacts
    manifest: FileFacts

    @property
    def script_sha256(self) -> str:
        return self.script.sha256

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def binding_digest(self) -> str:
        """script·manifest 두 SHA-256의 결속 digest(§5.3)."""
        return binding_digest(self.script.sha256, self.manifest.sha256)


@dataclass(frozen=True, slots=True)
class InvalidPackage:
    """malformed·검증 실패 package(§6.1). reason은 비밀 없는 요약."""

    name: str
    manifest_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """한 scan 결과. valid·invalid와 count 통계(§6.1, rescan 요약용)."""

    poc_dir: str
    valid: tuple = ()
    invalid: tuple = ()
    total_manifests_found: int = 0
    examined: int = 0
    truncated: bool = False

    def valid_by_id(self) -> dict:
        return {p.poc_id: p for p in self.valid}


def binding_digest(script_sha256: str, manifest_sha256: str) -> str:
    """두 파일 SHA-256의 순서 있는 결속 digest(§5.3). 재현 가능·안정적."""

    h = hashlib.sha256()
    h.update(b"vdeter.stage8.poc.binding.v1\n")
    h.update(script_sha256.encode("ascii"))
    h.update(b"\n")
    h.update(manifest_sha256.encode("ascii"))
    h.update(b"\n")
    return h.hexdigest()


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # non-POSIX
        raise CatalogError("catalog scan은 POSIX(owner/mode/symlink 검사)에서만 지원됩니다")
    return getuid()


def _open_dir(poc_dir: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(poc_dir, flags)
    except FileNotFoundError:
        raise CatalogError("catalog directory가 존재하지 않습니다")
    except NotADirectoryError:
        raise CatalogError("catalog 경로가 directory가 아닙니다")
    except OSError as error:
        # O_NOFOLLOW로 symlink directory를 열면 ELOOP.
        raise CatalogError(f"catalog directory 열기 실패: {error.__class__.__name__}")


def _read_regular_file(dir_fd: int, name: str, max_bytes: int, *, uid: int):
    """dir_fd 기준 openat + O_NOFOLLOW로 regular file을 bounded read하고 facts 계산.

    symlink면 ELOOP로 예외, oversize면 예외. ((facts, data), None) 또는 (None, reason).
    """

    # O_NONBLOCK: FIFO/특수 파일에 대한 O_RDONLY open이 writer를 기다리며 무기한 block되는
    # 것을 막는다(그렇지 않으면 아래 S_ISREG 거부가 도달 불가). regular file의 read는
    # O_NONBLOCK 영향을 받지 않으므로 그대로 둔다(§6.1 fail-closed).
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        return None, f"열기 실패({error.__class__.__name__}; symlink 가능성)"
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            return None, "regular file이 아닙니다"
        if st.st_uid != uid:
            return None, "파일 소유자가 현재 UID가 아닙니다"
        # PoC 파일의 group/other 쓰기 권한은 **거부 사유가 아니다**(2026-09-13 정책).
        # 나머지 계약(regular file·소유자·symlink·크기·읽는 도중 변조)은 그대로다.
        if st.st_size > max_bytes:
            return None, "파일이 크기 상한을 초과했습니다"
        h = hashlib.sha256()
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None, "파일이 크기 상한을 초과했습니다"
            h.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid")
        if total != st.st_size or any(getattr(st, f) != getattr(after, f) for f in fields):
            return None, "파일이 읽는 동안 변경되었습니다"
        path_st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if any(getattr(st, f) != getattr(path_st, f) for f in fields):
            return None, "파일이 읽는 동안 교체 또는 변경되었습니다"
        facts = FileFacts(
            path=name,
            size=st.st_size,
            dev=st.st_dev,
            ino=st.st_ino,
            mtime_ns=st.st_mtime_ns,
            mode=st.st_mode,
            uid=st.st_uid,
            sha256=h.hexdigest(),
        )
        return (facts, data), None
    except OSError as error:
        return None, f"파일 읽기 실패({error.__class__.__name__})"
    finally:
        os.close(fd)


def scan_catalog(poc_dir: str, *, uid: int | None = None) -> Catalog:
    """catalog를 한 번 scan한다(§6.1). directory 수준 위반은 :class:`CatalogError`.

    package 수준 문제는 :class:`InvalidPackage`로 담아 계속 진행한다(RF 전 해당 선택만
    거부).
    """

    uid = _current_uid() if uid is None else uid
    dir_fd = _open_dir(poc_dir)
    try:
        dst = os.fstat(dir_fd)
        if not statmod.S_ISDIR(dst.st_mode):
            raise CatalogError("catalog 경로가 directory가 아닙니다")
        if dst.st_uid != uid:
            raise CatalogError("catalog directory 소유자가 현재 UID가 아닙니다")
        if dst.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH):
            raise CatalogError("catalog directory가 group/other writable입니다")

        total = 0
        # Count every candidate without retaining an unbounded directory listing.
        with os.scandir(dir_fd) as entries:
            def manifest_names():
                nonlocal total
                for entry in entries:
                    if entry.name.endswith(MANIFEST_SUFFIX):
                        total += 1
                        yield entry.name

            examine = heapq.nsmallest(MAX_CATALOG_PACKAGES, manifest_names())
        truncated = total > MAX_CATALOG_PACKAGES

        valid: list = []
        invalid: list = []
        seen_ids: dict = {}
        seen_inodes: dict = {}

        for manifest_name in examine:
            base = manifest_name[: -len(MANIFEST_SUFFIX)]
            script_name = base + SCRIPT_SUFFIX
            if not base:
                invalid.append(InvalidPackage(base, manifest_name, "빈 basename"))
                continue

            script_res, script_reason = _read_regular_file(dir_fd, script_name, MAX_SCRIPT_BYTES, uid=uid)
            if script_res is None:
                invalid.append(InvalidPackage(base, manifest_name,
                                              f"script {script_name}: {script_reason}"))
                continue
            manifest_res, manifest_reason = _read_regular_file(dir_fd, manifest_name, MAX_MANIFEST_BYTES, uid=uid)
            if manifest_res is None:
                invalid.append(InvalidPackage(base, manifest_name,
                                              f"manifest: {manifest_reason}"))
                continue

            script_facts, _script_data = script_res
            manifest_facts, manifest_data = manifest_res

            # hard-link identity 충돌(§6.1): 두 파일이 같은 inode거나 다른 package와 공유.
            script_key = (script_facts.dev, script_facts.ino)
            manifest_key = (manifest_facts.dev, manifest_facts.ino)
            if script_key == manifest_key:
                invalid.append(InvalidPackage(base, manifest_name,
                                              "script와 manifest가 같은 inode입니다"))
                continue
            collision = None
            for key in (script_key, manifest_key):
                if key in seen_inodes:
                    collision = seen_inodes[key]
                    break
            if collision is not None:
                invalid.append(InvalidPackage(base, manifest_name,
                                              f"hard-link identity 충돌({collision})"))
                continue

            try:
                descriptor = parse_and_validate(
                    manifest_data,
                    script_basename=script_name,
                    script_sha256=script_facts.sha256,
                    manifest_sha256=manifest_facts.sha256,
                )
            except ManifestError as error:
                invalid.append(InvalidPackage(base, manifest_name, error.reason))
                continue

            if descriptor.poc_id in seen_ids:
                invalid.append(InvalidPackage(base, manifest_name,
                                              f"중복 poc_id({descriptor.poc_id})"))
                continue

            seen_ids[descriptor.poc_id] = base
            seen_inodes[script_key] = base
            seen_inodes[manifest_key] = base
            valid.append(PackageEntry(
                poc_id=descriptor.poc_id,
                version=descriptor.version,
                descriptor=descriptor,
                script=script_facts,
                manifest=manifest_facts,
            ))

        return Catalog(
            poc_dir=poc_dir,
            valid=tuple(valid),
            invalid=tuple(invalid),
            total_manifests_found=total,
            examined=len(examine),
            truncated=truncated,
        )
    finally:
        os.close(dir_fd)


__all__ = (
    "MANIFEST_SUFFIX",
    "SCRIPT_SUFFIX",
    "CatalogError",
    "FileFacts",
    "PackageEntry",
    "InvalidPackage",
    "Catalog",
    "binding_digest",
    "scan_catalog",
)
