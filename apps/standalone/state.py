"""VDETER standalone 영속 state 계층 (Phase I W2 — README §2·§3).

책임
    - 설치 root와 `var/`·`identity/` 경로 해석, 영속 디렉터리 보장
      (`var/`는 코드 배포·업데이트가 삭제·덮어쓰지 않는 state — .gitignore 등재)
    - 단일 인스턴스 OS file lock — 판정은 flock 계열 lock 자체로만 하고,
      lock 파일에 적는 PID·run-id는 **진단 기록일 뿐 판정에 쓰지 않는다**
      (결정 17). 프로세스가 죽으면 OS가 lock을 해제하므로 stale lock이 없다.
    - run 디렉터리 할당: 자동 run-id는 접미사 증가로 충돌 해소, 사용자 지정
      run-id는 기존 evidence 보호를 위해 존재 시 시작 전 거부 (결정 15).
      할당의 원자성은 `mkdir` 자체가 담보한다 (검사-후-생성 경쟁 없음).
    - 디스크 여유 검사: 경고/거부 2단계 (자동 삭제는 하지 않는다).

이 모듈은 표준 라이브러리만 사용한다. 대상은 Ubuntu(arm64/amd64)이며,
Windows 분기는 개발 환경에서 unit 시험을 돌리기 위한 것이다.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

try:  # Ubuntu (대상 환경)
    import fcntl

    def _lock_nonblocking(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

except ImportError:  # Windows 개발 환경 — unit 시험 전용 대체
    import msvcrt

    def _lock_nonblocking(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


#: 이 파일 기준 설치 root (= 저장소 checkout root).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: 디스크 여유 임계 (README §2). 자동 삭제 없음 — 경고/거부만.
DISK_WARN_FREE_BYTES = 4 * (1 << 30)
DISK_REFUSE_FREE_BYTES = 1 * (1 << 30)


class StateError(RuntimeError):
    """영속 state 계층의 거부 — 항상 RF 접근 전에 발생한다."""


class StatePaths:
    """설치 root 아래 영속 state 경로 (생성은 `ensure_var()`가 담당)."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else REPO_ROOT
        self.var_dir = self.root / "var"
        self.runs_dir = self.var_dir / "runs"
        self.qualification_dir = self.var_dir / "qualification"
        #: 자격 결과는 변경 불가 release 디렉터리에 쌓이고, active는 포인터
        #: 파일 하나다 — 포인터만 tmp→os.replace로 원자 교체한다 (W5 보완 2:
        #: 두 rename 사이 전원 장애로 active가 사라지는 창 제거).
        self.qualification_releases = self.qualification_dir / "releases"
        self.qualification_candidate = self.qualification_dir / "candidate"
        self.active_pointer = self.qualification_dir / "active.json"
        self.lock_path = self.var_dir / "lock"
        self.identity_dir = self.root / "identity"
        self.identity_private = self.identity_dir / "private"
        self.identity_certificates = self.identity_dir / "certificates"
        self.identity_trust = self.identity_dir / "trust"
        self.identity_archive = self.identity_dir / "archive"
        self.identity_manifest = self.identity_dir / "identity-manifest.json"
        #: 개발용 통제 RF 진단(`vdeter diag`) 상태 — qualification 디렉터리와
        #: 분리된다. 생성은 `diag authorize`가 담당하며 `ensure_var()`는 만들지
        #: 않는다(부재 자체가 "진단 승인 없음"의 의미를 가진다).
        self.diagnostic_dir = self.var_dir / "diagnostic"
        self.diagnostic_authorization = self.diagnostic_dir / "authorization.json"
        self.diagnostic_ledger = self.diagnostic_dir / "ledger.json"

    def ensure_var(self) -> None:
        """`var/` 계열 영속 디렉터리를 보장한다 (identity는 setup 소관)."""
        for directory in (
            self.var_dir, self.runs_dir, self.qualification_dir,
            self.qualification_releases, self.qualification_candidate,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class SingleInstanceLock:
    """`var/lock`에 대한 OS file lock — 동시 이중 세션 원천 차단 (결정 17).

    판정은 lock 획득 성공/실패뿐이다. 획득 후 기록하는 내용(PID·run-id·
    시각)은 사람이 잔존 상황을 진단할 때 읽는 정보이지 판정 입력이 아니다.
    """

    def __init__(self, lock_path: Path | str) -> None:
        self.lock_path = Path(lock_path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, diagnostics: dict | None = None) -> None:
        if self._handle is not None:
            raise StateError("lock already held")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+")
        try:
            _lock_nonblocking(handle)
        except OSError as exc:
            handle.close()
            raise StateError(
                "another vdeter process is running (single-instance lock "
                f"unavailable: {self.lock_path})") from exc
        self._handle = handle
        # 진단 기록 (판정 비사용). 기록 실패가 lock을 깨지 않는다.
        try:
            payload = {"pid": os.getpid(),
                       "acquired_utc": time.strftime(
                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            payload.update(diagnostics or {})
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            handle.flush()
        except OSError:
            pass

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def allocate_run_dir(runs_dir: Path | str, run_id: str | None = None,
                     *, clock=time.time) -> tuple[str, Path]:
    """run 디렉터리를 원자적으로 할당한다. 반환: (run_id, run_root).

    - 자동(run_id=None): `run-YYYYMMDD-HHMMSS`(UTC), 충돌 시 `-2`, `-3` …
      접미사 증가. 할당은 `mkdir(exist_ok=False)` 성공으로 확정된다.
    - 사용자 지정: 형식 검증 후, 이미 존재하면 **시작 전 거부** (기존
      evidence 보호 — 결정 15). 접미사 우회를 하지 않는다.
    """
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    if run_id is not None:
        if not run_id or any(sep in run_id for sep in ("/", "\\", "..")):
            raise StateError(f"malformed run-id: {run_id!r}")
        run_root = runs_dir / run_id
        try:
            run_root.mkdir(exist_ok=False)
        except FileExistsError:
            raise StateError(
                f"run-id {run_id!r} already exists: {run_root} — refused to protect existing "
                "evidence; use a different run-id"
            ) from None
        return run_id, run_root

    base = "run-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    candidate = base
    suffix = 1
    while True:
        run_root = runs_dir / candidate
        try:
            run_root.mkdir(exist_ok=False)
            return candidate, run_root
        except FileExistsError:
            suffix += 1
            candidate = f"{base}-{suffix}"
            if suffix > 1000:  # 방어적 상한 — 실사용에서 도달 불가
                raise StateError(f"run-id suffixes exhausted: {base}")


def check_disk_space(path: Path | str,
                     *, warn_bytes: int = DISK_WARN_FREE_BYTES,
                     refuse_bytes: int = DISK_REFUSE_FREE_BYTES) -> dict:
    """여유 공간 상태. status: ok | warn | refuse (자동 삭제는 없다)."""
    usage = shutil.disk_usage(str(path))
    if usage.free < refuse_bytes:
        status = "refuse"
    elif usage.free < warn_bytes:
        status = "warn"
    else:
        status = "ok"
    return {"free_bytes": usage.free, "total_bytes": usage.total,
            "status": status, "warn_bytes": warn_bytes,
            "refuse_bytes": refuse_bytes}


def fsync_dir(path: Path | str) -> None:
    """디렉터리 항목 변경(rename/replace)을 내구화한다 (POSIX 전용, 관용)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path | str, payload: dict) -> None:
    """임시 파일 **fsync** → `os.replace` → 부모 디렉터리 **fsync**.

    (README §3-5 + W5 보완 2차) 전원 장애 기준 내구성: 대상(Ubuntu)에서는
    replace 시점 이후 장애에도 새 내용 또는 이전 내용 중 하나가 온전하게
    남는다 — 절반 기록된 파일이 남지 않는다. 디렉터리 fsync는 POSIX
    전용이며 Windows 개발 환경에서는 프로세스 수준 원자성만 보증된다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
