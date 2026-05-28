"""SQLite store for YTS movies and their lifecycle."""
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from config import settings

_DB_PATH = settings.data_dir / "yts.db"
_lock = threading.RLock()   # RLock allows reentrant (read inside tx)


SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    imdb_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    year           INTEGER,
    quality        TEXT,
    size_bytes     INTEGER,
    rating         REAL,
    genres         TEXT,
    poster_url     TEXT,
    synopsis       TEXT,
    imdb_url       TEXT,
    magnet         TEXT,
    info_hash      TEXT,
    yts_url        TEXT,
    rss_pub_at     INTEGER,
    added_at       INTEGER,
    status         TEXT NOT NULL,
    qbit_hash      TEXT,
    save_path      TEXT,
    final_video    TEXT,
    subtitle_path  TEXT,
    subtitle_status TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_movies_status ON movies(status);
CREATE INDEX IF NOT EXISTS idx_movies_added  ON movies(added_at);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    imdb_id     TEXT,
    level       TEXT NOT NULL,
    msg         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_imdb ON events(imdb_id);
"""

_MIGRATIONS = [
    "ALTER TABLE movies ADD COLUMN synopsis TEXT",
    "ALTER TABLE movies ADD COLUMN imdb_url TEXT",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_conn = _connect()
_conn.executescript(SCHEMA)
_conn.commit()

# Run migrations idempotently
for _sql in _MIGRATIONS:
    try:
        _conn.execute(_sql)
        _conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


_RUNTIME_FIELDS = {
    "status", "qbit_hash", "save_path", "final_video",
    "subtitle_path", "subtitle_status", "note",
}


def upsert_movie(row: dict) -> bool:
    cols = ",".join(row.keys())
    placeholders = ",".join("?" * len(row))
    updatable = [k for k in row if k != "imdb_id" and k not in _RUNTIME_FIELDS]
    if updatable:
        updates = ",".join(f"{k}=excluded.{k}" for k in updatable)
        sql = (
            f"INSERT INTO movies ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(imdb_id) DO UPDATE SET {updates}"
        )
    else:
        sql = (
            f"INSERT INTO movies ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(imdb_id) DO NOTHING"
        )
    with tx() as c:
        before = c.execute(
            "SELECT 1 FROM movies WHERE imdb_id=?", (row["imdb_id"],)
        ).fetchone()
        c.execute(sql, tuple(row.values()))
    return before is None


def update_movie(imdb_id: str, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(
            f"UPDATE movies SET {sets} WHERE imdb_id=?",
            (*fields.values(), imdb_id),
        )


def get_movie(imdb_id: str) -> Optional[dict]:
    with _lock:
        row = _conn.execute("SELECT * FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
    return dict(row) if row else None


def list_movies(status: Optional[str] = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM movies"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY added_at DESC LIMIT ?"
    args += (limit,)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def delete_movie(imdb_id: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM movies WHERE imdb_id=?", (imdb_id,))


def find_by_hash(info_hash: str) -> Optional[dict]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM movies WHERE info_hash=? COLLATE NOCASE OR qbit_hash=? COLLATE NOCASE",
            (info_hash, info_hash),
        ).fetchone()
    return dict(row) if row else None


def log_event(level: str, msg: str, imdb_id: Optional[str] = None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO events (ts, imdb_id, level, msg) VALUES (?,?,?,?)",
            (int(time.time()), imdb_id, level, msg),
        )


def recent_events(limit: int = 200) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def wal_checkpoint() -> None:
    """Force WAL checkpoint to keep WAL file small."""
    with _lock:
        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def prune_events(keep_days: int = 30) -> int:
    """Delete events older than keep_days. Returns rows deleted."""
    cutoff = int(time.time()) - keep_days * 86400
    with tx() as c:
        c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return c.execute("SELECT changes()").fetchone()[0]


def backup_db() -> Optional[Path]:
    """Copy DB to data/backups/, keep last DB_BACKUP_KEEP_DAYS days."""
    backup_dir = settings.data_dir / "backups"
    backup_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"yts_{ts}.db"
    try:
        with _lock:
            src_conn = sqlite3.connect(_DB_PATH)
            dst_conn = sqlite3.connect(dest)
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
        # Prune old backups
        cutoff = time.time() - settings.DB_BACKUP_KEEP_DAYS * 86400
        for f in backup_dir.glob("yts_*.db"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        return dest
    except Exception as e:
        log_event("warn", f"DB backup failed: {e}")
        return None
