"""Single-owner, crash-consistent SQLite VDS2 state."""

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ...common.errors import ErrorCode
from ..constants import (
    MAX_AUTH_BLOCKS_PER_MESSAGE, MAX_AUTH_FAILURES, MAX_COUNTER,
    MAX_GCM_BLOCKS, TX_BLOCK_SIZE,
)
from ..errors import SecurityFault
from .material import DirectionalSessionMaterial
from .provider import MaterialBundle

_SCHEMA = """
CREATE TABLE metadata(schema_version INTEGER NOT NULL, endpoint_id TEXT NOT NULL,
                      local_station_id INTEGER NOT NULL, state_id BLOB NOT NULL);
CREATE TABLE tx_state(material_id BLOB PRIMARY KEY, source_id INTEGER NOT NULL,
                      destination_id INTEGER NOT NULL, epoch INTEGER NOT NULL,
                      reserved_until INTEGER NOT NULL DEFAULT 0,
                      gcm_blocks_reserved INTEGER NOT NULL DEFAULT 0,
                      status TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE rx_state(material_id BLOB PRIMARY KEY, source_id INTEGER NOT NULL,
                      destination_id INTEGER NOT NULL, epoch INTEGER NOT NULL,
                      highest_counter INTEGER NOT NULL DEFAULT 0,
                      replay_bitmap BLOB NOT NULL,
                      verification_blocks_used INTEGER NOT NULL DEFAULT 0,
                      failed_authentications INTEGER NOT NULL DEFAULT 0,
                      status TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE rx_reservation(reservation_id BLOB PRIMARY KEY, material_id BLOB NOT NULL,
                            blocks INTEGER NOT NULL, FOREIGN KEY(material_id)
                            REFERENCES rx_state(material_id));
"""


def _auth_blocks(body_length: int) -> int:
    if type(body_length) is not int or body_length < 0 or body_length > 164:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_CIPHERTEXT_LENGTH_INVALID)
    return 2 + (body_length + 15) // 16 + 1


@dataclass(frozen=True, slots=True)
class RxVerificationReservation:
    reservation_id: bytes
    material_id: bytes


class SecurityStateStore(Protocol):
    def reserve_tx_block(self, material_id: bytes) -> tuple[int, int]: ...
    def reserve_rx_verification(self, material_id: bytes, ciphertext_length: int) -> RxVerificationReservation: ...
    def commit_rx_authenticated(self, reservation: RxVerificationReservation, highest_counter: int, replay_bitmap: int) -> None: ...
    def commit_rx_authentication_failure(self, reservation: RxVerificationReservation) -> None: ...
    def inhibit_rx(self, reservation: RxVerificationReservation) -> None: ...
    def close(self) -> None: ...


class SQLiteSecurityStateStore:
    def __init__(self, path: Path, bundle: MaterialBundle):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        self._lock_fd = None
        self._db = None
        try:
            if not self._path.is_file() or self._path.is_symlink():
                raise ValueError("State DB unavailable")
            if os.name == "posix":
                stat = self._path.stat()
                if (stat.st_mode & 0o777) != 0o600 or stat.st_uid != os.geteuid():
                    raise ValueError("State DB permissions invalid")
                if (self._path.parent.stat().st_mode & 0o777) != 0o700:
                    raise ValueError("State directory permissions invalid")
            self._acquire_lock()
            self._db = sqlite3.connect(f"file:{self._path.as_posix()}?mode=rw", uri=True,
                                       isolation_level=None, check_same_thread=False, timeout=0)
            self._configure()
            self._validate(bundle)
            self._recover_orphans()
        except Exception:
            self.close()
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE) from None

    @classmethod
    def initialize(cls, path: Path, bundle: MaterialBundle) -> None:
        path = Path(path)
        if path.exists() or not path.parent.is_dir():
            raise ValueError("State installation must be new")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        db = None
        try:
            db = sqlite3.connect(path)
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO metadata VALUES (1,?,?,?)",
                       (bundle.endpoint_id, bundle.local_station_id.value, bundle.state_id))
            for material in bundle.directions:
                table = "tx_state" if material.source_id == bundle.local_station_id else "rx_state"
                if table == "tx_state":
                    db.execute("INSERT INTO tx_state(material_id,source_id,destination_id,epoch) VALUES (?,?,?,?)",
                               (material.material_id, *material.direction))
                else:
                    db.execute("INSERT INTO rx_state(material_id,source_id,destination_id,epoch,replay_bitmap) VALUES (?,?,?,?,?)",
                               (material.material_id, *material.direction, bytes(8)))
            db.commit()
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            if db is not None:
                db.close()

    def _configure(self):
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=0")

    def _acquire_lock(self):
        lock_path = self._path.with_name("state.lock")
        self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            if os.fstat(self._lock_fd).st_size == 0:
                os.write(self._lock_fd, b"\x00")
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(self._lock_fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _validate(self, bundle: MaterialBundle):
        db = self._db
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("State DB corrupt")
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise ValueError("State DB version invalid")
        metadata = db.execute("SELECT schema_version,endpoint_id,local_station_id,state_id FROM metadata").fetchall()
        if metadata != [(1, bundle.endpoint_id, bundle.local_station_id.value, bundle.state_id)]:
            raise ValueError("State binding mismatch")
        expected_tx = {m.material_id: m.direction for m in bundle.directions if m.source_id == bundle.local_station_id}
        expected_rx = {m.material_id: m.direction for m in bundle.directions if m.destination_id == bundle.local_station_id}
        tx = db.execute("SELECT material_id,source_id,destination_id,epoch,reserved_until,gcm_blocks_reserved,status FROM tx_state").fetchall()
        rx = db.execute("SELECT material_id,source_id,destination_id,epoch,highest_counter,replay_bitmap,verification_blocks_used,failed_authentications,status FROM rx_state").fetchall()
        if {row[0]: tuple(row[1:4]) for row in tx} != expected_tx or {row[0]: tuple(row[1:4]) for row in rx} != expected_rx:
            raise ValueError("State material mismatch")
        for row in tx:
            if not (0 <= row[4] <= MAX_COUNTER and 0 <= row[5] <= MAX_GCM_BLOCKS and row[6] == "ACTIVE"):
                raise ValueError("TX state unavailable")
        for row in rx:
            bitmap = row[5]
            if (not 0 <= row[4] <= MAX_COUNTER or type(bitmap) is not bytes or len(bitmap) != 8
                    or (row[4] == 0) != (int.from_bytes(bitmap, "big") == 0)
                    or not 0 <= row[6] <= MAX_GCM_BLOCKS or not 0 <= row[7] <= MAX_AUTH_FAILURES
                    or row[8] != "ACTIVE"):
                raise ValueError("RX state unavailable")

    def _recover_orphans(self):
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                for material_id, count in db.execute("SELECT material_id,COUNT(*) FROM rx_reservation GROUP BY material_id"):
                    db.execute("UPDATE rx_state SET failed_authentications=failed_authentications+? WHERE material_id=?", (count, material_id))
                db.execute("DELETE FROM rx_reservation")
                db.commit()
            except Exception:
                db.rollback()
                raise
            if db.execute("SELECT 1 FROM rx_state WHERE failed_authentications>=?", (MAX_AUTH_FAILURES,)).fetchone():
                raise ValueError("RX failure budget exhausted")

    def tx_reserved_until(self, material_id: bytes) -> int:
        with self._lock:
            return self._db.execute("SELECT reserved_until FROM tx_state WHERE material_id=?", (material_id,)).fetchone()[0]

    def reserve_tx_block(self, material_id: bytes) -> tuple[int, int]:
        with self._lock:
            try:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                old, blocks, status = db.execute("SELECT reserved_until,gcm_blocks_reserved,status FROM tx_state WHERE material_id=?", (material_id,)).fetchone()
                if status != "ACTIVE" or old >= MAX_COUNTER:
                    db.execute("UPDATE tx_state SET status='EXHAUSTED' WHERE material_id=?", (material_id,))
                    db.commit()
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED)
                new = min(old + TX_BLOCK_SIZE, MAX_COUNTER)
                charge = (new - old) * MAX_AUTH_BLOCKS_PER_MESSAGE
                if blocks + charge > MAX_GCM_BLOCKS:
                    db.execute("UPDATE tx_state SET status='EXHAUSTED' WHERE material_id=?", (material_id,))
                    db.commit()
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED)
                db.execute("UPDATE tx_state SET reserved_until=?,gcm_blocks_reserved=? WHERE material_id=?",
                           (new, blocks + charge, material_id))
                db.commit()
                return old + 1, new
            except SecurityFault:
                raise
            except Exception:
                self._db.rollback()
                raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE) from None

    def rx_snapshot(self, material_id: bytes) -> tuple[int, int]:
        with self._lock:
            highest, bitmap = self._db.execute("SELECT highest_counter,replay_bitmap FROM rx_state WHERE material_id=?", (material_id,)).fetchone()
            return highest, int.from_bytes(bitmap, "big")

    def reserve_rx_verification(self, material_id: bytes, ciphertext_length: int) -> RxVerificationReservation:
        blocks = _auth_blocks(ciphertext_length)
        with self._lock:
            try:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                used, failures, status = db.execute("SELECT verification_blocks_used,failed_authentications,status FROM rx_state WHERE material_id=?", (material_id,)).fetchone()
                outstanding = db.execute("SELECT COUNT(*) FROM rx_reservation WHERE material_id=?", (material_id,)).fetchone()[0]
                if status != "ACTIVE" or used + blocks > MAX_GCM_BLOCKS or failures + outstanding >= MAX_AUTH_FAILURES:
                    db.execute("UPDATE rx_state SET status='EXHAUSTED' WHERE material_id=?", (material_id,))
                    db.commit()
                    raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED)
                reservation = RxVerificationReservation(uuid4().bytes, material_id)
                db.execute("UPDATE rx_state SET verification_blocks_used=? WHERE material_id=?", (used + blocks, material_id))
                db.execute("INSERT INTO rx_reservation VALUES (?,?,?)", (reservation.reservation_id, material_id, blocks))
                db.commit()
                return reservation
            except SecurityFault:
                raise
            except Exception:
                self._db.rollback()
                raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE) from None

    def commit_rx_authenticated(self, reservation: RxVerificationReservation, highest_counter: int, replay_bitmap: int) -> None:
        with self._lock:
            try:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                deleted = db.execute("DELETE FROM rx_reservation WHERE reservation_id=? AND material_id=?",
                                     (reservation.reservation_id, reservation.material_id))
                if deleted.rowcount != 1:
                    raise ValueError("Missing verification reservation")
                db.execute("UPDATE rx_state SET highest_counter=?,replay_bitmap=? WHERE material_id=?",
                           (highest_counter, replay_bitmap.to_bytes(8, "big"), reservation.material_id))
                db.commit()
            except Exception:
                self._db.rollback()
                raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE) from None

    def release_rx_duplicate(self, reservation: RxVerificationReservation) -> None:
        with self._lock:
            self._db.execute("DELETE FROM rx_reservation WHERE reservation_id=? AND material_id=?",
                             (reservation.reservation_id, reservation.material_id))

    def commit_rx_authentication_failure(self, reservation: RxVerificationReservation) -> None:
        with self._lock:
            try:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                deleted = db.execute("DELETE FROM rx_reservation WHERE reservation_id=? AND material_id=?",
                                     (reservation.reservation_id, reservation.material_id))
                if deleted.rowcount != 1:
                    raise ValueError("Missing verification reservation")
                db.execute("UPDATE rx_state SET failed_authentications=failed_authentications+1 WHERE material_id=?",
                           (reservation.material_id,))
                db.commit()
            except Exception:
                self._db.rollback()
                raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE) from None

    def inhibit_rx(self, reservation: RxVerificationReservation) -> None:
        with self._lock:
            try:
                db = self._db
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM rx_reservation WHERE reservation_id=? AND material_id=?",
                           (reservation.reservation_id, reservation.material_id))
                db.execute("UPDATE rx_state SET status='INHIBITED' WHERE material_id=?",
                           (reservation.material_id,))
                db.commit()
            except Exception:
                self._db.rollback()
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._db is not None:
                    try:
                        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        self._db.close()
                        self._db = None
            finally:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
