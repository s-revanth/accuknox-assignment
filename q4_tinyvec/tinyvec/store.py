"""Durability: a write-ahead log plus atomic snapshots.

Rule: a write is acknowledged only after its WAL record is on disk (write + fsync).
So after any crash, everything a caller was told "ok" about can be rebuilt.

WAL record (one line):    <crc32 hex>\t<json {"seq":..,"op":"upsert"|"delete",...}>
  * the CRC detects a torn last line (process died mid-append): it is cut off on recovery.
  * a bad CRC anywhere *except* the last line is real corruption: recovery refuses to guess.

Snapshot: the whole state written into a fresh directory snap-<seq>/, fsynced, then published by
atomically replacing the one-line file CURRENT (write tmp -> fsync -> rename -> fsync dir).
A crash at any point leaves CURRENT naming either the old complete snapshot or the new one.
(LevelDB uses the same CURRENT -> MANIFEST trick.) After publishing, the WAL is emptied: records with
seq <= snapshot seq would be skipped on replay anyway, so a crash before the truncation is harmless.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zlib
from pathlib import Path

try:
    import fcntl  # POSIX only; on Windows plain os.fsync is used
except ImportError:  # pragma: no cover
    fcntl = None


def durable_sync(fd: int) -> None:
    """fsync() on macOS only hands data to the drive, which may still hold it in its cache.
    F_FULLFSYNC asks the drive to flush too (Postgres and SQLite do the same on macOS)."""
    if sys.platform == "darwin" and fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    """Make a rename durable. Windows can't open a directory for fsync, and NTFS journals renames itself."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CorruptWAL(Exception):
    pass


class WAL:
    def __init__(self, path: Path, fsync: bool = True):
        self.path, self.fsync = path, fsync
        self.f = open(path, "ab")

    def append(self, rec: dict) -> None:
        body = json.dumps(rec, separators=(",", ":")).encode()
        self.f.write(b"%08x\t%s\n" % (zlib.crc32(body), body))
        self.f.flush()
        if self.fsync:
            durable_sync(self.f.fileno())

    @staticmethod
    def read(path: Path) -> tuple[list[dict], int]:
        """Return (records, torn_bytes_cut). Truncates a torn tail in place."""
        if not path.exists():
            return [], 0
        data = path.read_bytes()
        records, pos = [], 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            rec = WAL._parse(data[pos:nl]) if nl != -1 else None
            if rec is None:
                if nl == -1 or nl == len(data) - 1:     # torn tail: no newline, or garbage last line
                    with open(path, "r+b") as f:
                        f.truncate(pos)
                        f.flush()
                        durable_sync(f.fileno())
                    return records, len(data) - pos
                raise CorruptWAL(f"bad record at byte {pos} in {path}")
            records.append(rec)
            pos = nl + 1
        return records, 0

    @staticmethod
    def _parse(line: bytes) -> dict | None:
        crc, sep, body = line.partition(b"\t")
        try:
            if sep and int(crc, 16) == zlib.crc32(body):
                return json.loads(body)
        except ValueError:
            pass
        return None

    def reset(self) -> None:
        self.f.close()
        tmp = self.path.with_suffix(".tmp")
        open(tmp, "wb").close()
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)
        self.f = open(self.path, "ab")

    def close(self) -> None:
        self.f.close()


def write_snapshot(root: Path, seq: int, files: dict[str, bytes]) -> None:
    snap = root / f"snap-{seq:012d}"
    tmpdir = root / f".tmp-snap-{seq:012d}"
    shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir()
    for name, blob in files.items():
        with open(tmpdir / name, "wb") as f:
            f.write(blob)
            f.flush()
            durable_sync(f.fileno())
    _fsync_dir(tmpdir)
    shutil.rmtree(snap, ignore_errors=True)
    os.replace(tmpdir, snap)
    cur_tmp = root / "CURRENT.tmp"
    with open(cur_tmp, "w") as f:
        f.write(snap.name)
        f.flush()
        durable_sync(f.fileno())
    os.replace(cur_tmp, root / "CURRENT")              # <- the commit point
    _fsync_dir(root)
    for old in root.glob("snap-*"):                    # garbage-collect older snapshots
        if old.name != snap.name:
            shutil.rmtree(old, ignore_errors=True)


def read_snapshot(root: Path) -> dict[str, bytes] | None:
    cur = root / "CURRENT"
    if not cur.exists():
        return None
    snap = root / cur.read_text().strip()
    return {p.name: p.read_bytes() for p in snap.iterdir()}
