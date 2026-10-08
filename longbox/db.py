"""SQLite storage for the library index.

One file, WAL, no server.  The schema keeps the full ComicInfo field set plus
the raw XML so that later phases (metadata graph, duplicates, timeline) never
have to re-open an archive.  `progress` is a separate table so reading state
survives a re-index.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

#: columns of `comics`, in insert order (excluding the autoincrement id)
COMIC_COLUMNS = (
    "path", "filename", "series", "series_key", "volume_raw", "volume_kind",
    "issue_number", "issue_sort", "year", "month", "day", "title", "publisher",
    "summary", "writers", "pencillers", "inkers", "colorists", "letterers",
    "cover_artists", "editors", "characters", "teams", "locations",
    "scan_information", "notes", "web", "comicvine_issue_id", "page_count",
    "comicinfo_page_count", "pages", "cover_page", "comicinfo_present",
    "comicinfo_raw", "filename_parsed", "file_size", "mtime", "archive_type",
    "status", "error_message", "indexed_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS comics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    path                TEXT NOT NULL UNIQUE,
    filename            TEXT,
    series              TEXT,
    series_key          TEXT,
    volume_raw          TEXT,
    volume_kind         TEXT,
    issue_number        TEXT,
    issue_sort          REAL,
    year                INTEGER,
    month               INTEGER,
    day                 INTEGER,
    title               TEXT,
    publisher           TEXT,
    summary             TEXT,
    writers             TEXT,
    pencillers          TEXT,
    inkers              TEXT,
    colorists           TEXT,
    letterers           TEXT,
    cover_artists       TEXT,
    editors             TEXT,
    characters          TEXT,
    teams               TEXT,
    locations           TEXT,
    scan_information    TEXT,
    notes               TEXT,
    web                 TEXT,
    comicvine_issue_id  TEXT,
    page_count          INTEGER,
    comicinfo_page_count INTEGER,
    pages               TEXT,
    cover_page          INTEGER,
    comicinfo_present   INTEGER NOT NULL DEFAULT 0,
    comicinfo_raw       TEXT,
    filename_parsed     TEXT,
    file_size           INTEGER,
    mtime               REAL,
    archive_type        TEXT,
    status              TEXT NOT NULL DEFAULT 'ok',
    error_message       TEXT,
    indexed_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_comics_series_key ON comics(series_key);
CREATE INDEX IF NOT EXISTS idx_comics_status ON comics(status);
CREATE INDEX IF NOT EXISTS idx_comics_year ON comics(year);
CREATE INDEX IF NOT EXISTS idx_comics_comicvine ON comics(comicvine_issue_id);

CREATE TABLE IF NOT EXISTS progress (
    comic_id    INTEGER PRIMARY KEY REFERENCES comics(id) ON DELETE CASCADE,
    last_page   INTEGER NOT NULL DEFAULT 0,
    "read"      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);
"""

#: JSON-encoded (list/dict) columns
JSON_COLUMNS = ("writers", "pencillers", "inkers", "colorists", "letterers",
                "cover_artists", "editors", "characters", "teams", "locations",
                "pages", "filename_parsed")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    """Open (creating if needed) the database with WAL enabled."""
    db_path = str(db_path)
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def open_db(db_path: str | os.PathLike) -> sqlite3.Connection:
    return init_db(connect(db_path))


def default_db_path() -> str:
    env = os.environ.get("LONGBOX_DB")
    if env:
        return env
    data_dir = os.environ.get("LONGBOX_DATA_DIR") or "data"
    return os.path.join(data_dir, "longbox.db")


def encode_row(data: dict) -> dict:
    row = {}
    for key in COMIC_COLUMNS:
        if key not in data:
            continue
        value = data[key]
        if key in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        row[key] = value
    return row


def upsert_comic(conn: sqlite3.Connection, data: dict) -> int:
    """Insert or update by `path`; returns the row id. Progress is untouched."""
    row = encode_row(data)
    if "path" not in row:
        raise ValueError("path is required")
    row.setdefault("indexed_at", utcnow())
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "path")
    sql = (
        f"INSERT INTO comics ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[col] for col in columns])
    conn.commit()
    found = conn.execute("SELECT id FROM comics WHERE path = ?", (row["path"],)).fetchone()
    return int(found["id"])


def mark_missing(conn: sqlite3.Connection, seen_paths: set[str], root: str) -> int:
    """Rows under `root` whose file is gone become status='missing'."""
    root_key = os.path.normcase(os.path.abspath(root))
    changed = 0
    rows = conn.execute("SELECT id, path, status FROM comics").fetchall()
    for row in rows:
        path = os.path.abspath(str(row["path"]))
        if not os.path.normcase(path).startswith(root_key):
            continue
        if os.path.normcase(path) in seen_paths:
            continue
        if row["status"] == "missing":
            continue
        conn.execute(
            "UPDATE comics SET status='missing', error_message=?, indexed_at=? WHERE id=?",
            ("file no longer present at this path", utcnow(), row["id"]),
        )
        changed += 1
    if changed:
        conn.commit()
    return changed


def get_progress(conn: sqlite3.Connection, comic_id: int) -> dict:
    row = conn.execute(
        'SELECT comic_id, last_page, "read", updated_at FROM progress WHERE comic_id = ?',
        (comic_id,),
    ).fetchone()
    if row is None:
        return {"comic_id": comic_id, "last_page": 0, "read": False, "updated_at": None}
    return {"comic_id": comic_id, "last_page": int(row["last_page"]),
            "read": bool(row["read"]), "updated_at": row["updated_at"]}


def set_progress(conn: sqlite3.Connection, comic_id: int, last_page: int | None = None,
                 read: bool | None = None) -> dict:
    current = get_progress(conn, comic_id)
    if last_page is not None:
        current["last_page"] = max(0, int(last_page))
    if read is not None:
        current["read"] = bool(read)
    conn.execute(
        'INSERT INTO progress (comic_id, last_page, "read", updated_at) VALUES (?, ?, ?, ?) '
        'ON CONFLICT(comic_id) DO UPDATE SET last_page=excluded.last_page, '
        '"read"=excluded."read", updated_at=excluded.updated_at',
        (comic_id, current["last_page"], 1 if current["read"] else 0, utcnow()),
    )
    conn.commit()
    return get_progress(conn, comic_id)
