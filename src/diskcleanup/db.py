from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import VideoRecord


TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  root TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  quick_hash TEXT,
  sha256 TEXT,
  duration REAL,
  width INTEGER,
  height INTEGER,
  codec TEXT,
  bit_rate INTEGER,
  fps REAL,
  frames INTEGER,
  fingerprint TEXT NOT NULL DEFAULT '[]',
  fingerprint_interval REAL,
  fingerprint_profile TEXT,
  fingerprint_count INTEGER NOT NULL DEFAULT 0,
  scanned_at TEXT NOT NULL,
  error TEXT
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_files_quick_hash ON files (quick_hash);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files (sha256);
CREATE INDEX IF NOT EXISTS idx_files_size_mtime ON files (size, mtime_ns);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(TABLE_SCHEMA)
    ensure_columns(connection)
    connection.executescript(INDEX_SCHEMA)
    return connection


def ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(files)").fetchall()
    }
    if "quick_hash" not in columns:
        connection.execute("ALTER TABLE files ADD COLUMN quick_hash TEXT")
    if "fingerprint_profile" not in columns:
        connection.execute("ALTER TABLE files ADD COLUMN fingerprint_profile TEXT")


def get_record(connection: sqlite3.Connection, path: Path) -> VideoRecord | None:
    row = connection.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
    return row_to_record(row) if row else None


def upsert_record(connection: sqlite3.Connection, record: VideoRecord) -> None:
    connection.execute(
        """
        INSERT INTO files (
          path, root, size, mtime_ns, quick_hash, sha256, duration, width,
          height, codec, bit_rate, fps, frames, fingerprint, fingerprint_interval,
          fingerprint_profile,
          fingerprint_count, scanned_at, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          root = excluded.root,
          size = excluded.size,
          mtime_ns = excluded.mtime_ns,
          quick_hash = excluded.quick_hash,
          sha256 = excluded.sha256,
          duration = excluded.duration,
          width = excluded.width,
          height = excluded.height,
          codec = excluded.codec,
          bit_rate = excluded.bit_rate,
          fps = excluded.fps,
          frames = excluded.frames,
          fingerprint = excluded.fingerprint,
          fingerprint_interval = excluded.fingerprint_interval,
          fingerprint_profile = excluded.fingerprint_profile,
          fingerprint_count = excluded.fingerprint_count,
          scanned_at = excluded.scanned_at,
          error = excluded.error
        """,
        (
            str(record.path),
            str(record.root),
            record.size,
            record.mtime_ns,
            record.quick_hash,
            record.sha256,
            record.duration,
            record.width,
            record.height,
            record.codec,
            record.bit_rate,
            record.fps,
            record.frames,
            json.dumps(list(record.fingerprint)),
            record.fingerprint_interval,
            record.fingerprint_profile,
            len(record.fingerprint),
            utc_now(),
            record.error,
        ),
    )


def list_records(connection: sqlite3.Connection) -> list[VideoRecord]:
    rows = connection.execute("SELECT * FROM files ORDER BY path").fetchall()
    return [row_to_record(row) for row in rows]


def remove_missing_records(connection: sqlite3.Connection) -> int:
    rows = connection.execute("SELECT path FROM files").fetchall()
    removed = 0
    for row in rows:
        if not Path(row["path"]).exists():
            connection.execute("DELETE FROM files WHERE path = ?", (row["path"],))
            removed += 1
    return removed


def row_to_record(row: sqlite3.Row) -> VideoRecord:
    fingerprint = tuple(int(value) for value in json.loads(row["fingerprint"] or "[]"))
    return VideoRecord(
        path=Path(row["path"]),
        root=Path(row["root"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        sha256=row["sha256"],
        duration=row["duration"],
        width=row["width"],
        height=row["height"],
        codec=row["codec"],
        bit_rate=row["bit_rate"],
        fps=row["fps"],
        frames=row["frames"],
        fingerprint=fingerprint,
        fingerprint_interval=row["fingerprint_interval"],
        error=row["error"],
        quick_hash=row["quick_hash"],
        fingerprint_profile=row["fingerprint_profile"],
    )
