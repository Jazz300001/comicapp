"""Local JSON API: python -m longbox.server --path <comics folder> --db <db> --port 8765

Runs on the owner's own machine only (binds to 127.0.0.1 by default), no login,
no cloud.  Comics are looked up by database id - a client-supplied file path is
never accepted, so the API can never be talked into reading an arbitrary file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import archive as archive_mod
from . import db as db_mod
from . import parser as parser_mod
from .scanner import scan_library

SORT_OPTIONS = {
    "series": ("c.series_key IS NULL, c.series_key ASC, c.issue_sort IS NULL, "
               "c.issue_sort ASC, c.issue_number ASC, c.year ASC"),
    "issue": ("c.issue_sort IS NULL, c.issue_sort ASC, c.series_key ASC"),
    "year": "c.year IS NULL, c.year DESC, c.series_key ASC, c.issue_sort ASC",
    "title": "c.title IS NULL, lower(c.title) ASC, c.series_key ASC",
    "added": "c.id DESC",
    "indexed": "c.indexed_at DESC, c.id DESC",
}
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

BRIEF_FIELDS = (
    "id", "filename", "series", "series_key", "volume_raw", "volume_kind",
    "issue_number", "issue_sort", "year", "month", "publisher", "page_count",
    "cover_page", "comicinfo_present", "comicvine_issue_id", "archive_type",
    "status", "error_message", "file_size", "indexed_at",
)
CREATOR_FIELDS = ("writers", "pencillers", "inkers", "colorists", "letterers",
                  "cover_artists", "editors")
DETAIL_EXTRA_FIELDS = ("path", "title", "day", "summary", "notes", "web",
                       "scan_information", "comicinfo_page_count", "mtime",
                       "filename_parsed", "characters", "teams", "locations",
                       "pages")


def _loads(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _brief(row: sqlite3.Row) -> dict:
    item = {field: row[field] for field in BRIEF_FIELDS if field in row.keys()}
    item["read"] = bool(row["read"]) if "read" in row.keys() and row["read"] is not None else False
    item["last_page"] = int(row["last_page"]) if "last_page" in row.keys() and row["last_page"] else 0
    item["comicinfo_present"] = bool(item.get("comicinfo_present"))
    item["thumb_url"] = f"/api/comics/{item['id']}/thumbnail"
    return item


def _detail(row: sqlite3.Row) -> dict:
    item = _brief(row)
    for field in DETAIL_EXTRA_FIELDS:
        if field in row.keys():
            item[field] = row[field]
    item["filename_parsed"] = _loads(item.get("filename_parsed"), {})
    for field in CREATOR_FIELDS:
        item[field] = _loads(row[field] if field in row.keys() else None, [])
    item["characters"] = _loads(row["characters"], [])
    item["teams"] = _loads(row["teams"], [])
    item["locations"] = _loads(row["locations"], [])
    item["pages"] = _loads(row["pages"], [])
    item["comicinfo_raw"] = row["comicinfo_raw"]
    return item


class ProgressIn(BaseModel):
    last_page: int = Field(default=0, ge=0)


class ReadIn(BaseModel):
    read: bool = True


def create_app(comics_path: str | None = None, db_path: str | None = None,
               thumb_dir: str | None = None) -> FastAPI:
    comics_path = os.path.abspath(os.path.expanduser(
        comics_path or os.environ.get("LONGBOX_COMICS") or "."))
    db_path = os.path.abspath(os.path.expanduser(db_path or db_mod.default_db_path()))
    thumb_dir = os.path.abspath(os.path.expanduser(
        thumb_dir or os.environ.get("LONGBOX_THUMB_DIR")
        or os.path.join(os.path.dirname(db_path), "thumbs")))

    conn = db_mod.open_db(db_path)
    write_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        try:
            conn.close()
        except Exception:  # pragma: no cover - tidy close on shutdown
            pass

    app = FastAPI(title="Longbox", version="0.1.0",
                  description="Local comic library index.",
                  lifespan=lifespan)

    app.state.comics_path = comics_path
    app.state.db_path = db_path
    app.state.thumb_dir = thumb_dir
    app.state.conn = conn
    os.makedirs(thumb_dir, exist_ok=True)

    def fetch(comic_id: int) -> sqlite3.Row:
        row = conn.execute(
            'SELECT c.*, p.last_page AS last_page, p."read" AS "read" '
            "FROM comics c LEFT JOIN progress p ON p.comic_id = c.id WHERE c.id = ?",
            (comic_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no comic with id {comic_id}")
        return row

    # ------------------------------------------------------------------ meta
    @app.get("/")
    def root() -> dict:
        return {"name": "Longbox", "version": "0.1.0", "api": "/api/health",
                "comics_folder": comics_path}

    @app.get("/api/health")
    def health() -> dict:
        total = conn.execute("SELECT COUNT(*) AS c FROM comics").fetchone()["c"]
        return {"status": "ok", "version": "0.1.0", "comics": int(total),
                "comics_folder": comics_path, "db": db_path}

    @app.get("/api/stats")
    def stats() -> dict:
        def scalar(sql: str, *params) -> int:
            return int(conn.execute(sql, params).fetchone()[0] or 0)

        total = scalar("SELECT COUNT(*) FROM comics")
        errors = scalar("SELECT COUNT(*) FROM comics WHERE status = 'error'")
        missing = scalar("SELECT COUNT(*) FROM comics WHERE status = 'missing'")
        with_info = scalar("SELECT COUNT(*) FROM comics WHERE comicinfo_present = 1")
        read = scalar('SELECT COUNT(*) FROM comics c JOIN progress p ON p.comic_id = c.id '
                      'WHERE p."read" = 1')
        started = scalar("SELECT COUNT(*) FROM comics c JOIN progress p ON p.comic_id = c.id "
                         "WHERE p.last_page > 0")
        # readable archives whose embedded ComicInfo.xml itself failed to parse
        metadata_problems = scalar("SELECT COUNT(*) FROM comics "
                                   "WHERE status = 'ok' AND error_message IS NOT NULL")
        return {
            "total": total,
            "ok": total - errors - missing,
            "errors": errors,
            "metadata_problems": metadata_problems,
            "missing": missing,
            "with_comicinfo": with_info,
            "without_comicinfo": total - with_info,
            "read": read,
            "unread": total - read,
            "in_progress": started,
            "series": scalar("SELECT COUNT(DISTINCT series_key) FROM comics "
                             "WHERE series_key IS NOT NULL"),
            "pages": scalar("SELECT COALESCE(SUM(page_count), 0) FROM comics"),
            "comics_folder": comics_path,
        }

    # --------------------------------------------------------------- listing
    @app.get("/api/comics")
    def list_comics(
        q: str | None = None,
        series: str | None = None,
        year: int | None = None,
        read: bool | None = None,
        sort: str = "series",
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        where: list[str] = []
        params: list[Any] = []
        if q:
            needle = f"%{q.strip().casefold()}%"
            where.append("(lower(c.series) LIKE ? OR lower(c.title) LIKE ? OR "
                         "lower(c.writers) LIKE ? OR lower(c.characters) LIKE ? OR "
                         "lower(c.series_key) LIKE ?)")
            params.extend([needle] * 5)
        if series:
            where.append("(c.series_key = ? OR lower(c.series) = ?)")
            params.extend([parser_mod.normalise_series(series), series.strip().casefold()])
        if year is not None:
            where.append("c.year = ?")
            params.append(year)
        if read is True:
            where.append('p."read" = 1')
        elif read is False:
            where.append('COALESCE(p."read", 0) = 0')
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = SORT_OPTIONS.get(sort, SORT_OPTIONS["series"])

        base = "FROM comics c LEFT JOIN progress p ON p.comic_id = c.id" + clause
        total = int(conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT c.*, p.last_page AS last_page, p.\"read\" AS \"read\" {base} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {"items": [_brief(row) for row in rows], "total": total,
                "limit": limit, "offset": offset, "sort": sort}

    @app.get("/api/series")
    def series_list() -> dict:
        rows = conn.execute(
            "SELECT series_key, MIN(series) AS series, COUNT(*) AS count, "
            "MIN(year) AS first_year, MAX(year) AS last_year "
            "FROM comics WHERE series_key IS NOT NULL AND status != 'missing' "
            "GROUP BY series_key ORDER BY lower(MIN(series))"
        ).fetchall()
        return {"items": [{"series": row["series"], "series_key": row["series_key"],
                           "count": int(row["count"]), "first_year": row["first_year"],
                           "last_year": row["last_year"]} for row in rows],
                "total": len(rows)}

    # ---------------------------------------------------------------- detail
    @app.get("/api/comics/{comic_id}")
    def comic_detail(comic_id: int) -> dict:
        return _detail(fetch(comic_id))

    @app.get("/api/comics/{comic_id}/pages/{page_number}")
    def page_bytes(comic_id: int, page_number: int) -> Response:
        row = fetch(comic_id)
        pages = _loads(row["pages"], [])
        entry = next((p for p in pages if p.get("page") == page_number), None)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"page {page_number} not in comic {comic_id} "
                       f"(has {len(pages)} pages)")
        try:
            with archive_mod.open_archive(row["path"]) as handle:
                data = handle.read(entry["name"])
        except archive_mod.ArchiveError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"page member missing: {entry['name']}")
        return Response(
            content=data,
            media_type=archive_mod.content_type_for(entry["name"]),
            headers={"Cache-Control": "private, max-age=3600",
                     "X-Page-Name": entry["name"],
                     "X-Page-Number": str(page_number)},
        )

    @app.get("/api/comics/{comic_id}/thumbnail")
    def thumbnail(comic_id: int) -> Response:
        row = fetch(comic_id)
        cache_file = os.path.join(thumb_dir, f"{comic_id}.jpg")
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as handle:
                return Response(content=handle.read(), media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=86400"})
        if row["status"] != "ok":
            raise HTTPException(status_code=404,
                                detail=f"no thumbnail: {row['error_message'] or row['status']}")
        pages = _loads(row["pages"], [])
        if not pages:
            raise HTTPException(status_code=404, detail="archive has no image pages")
        wanted = int(row["cover_page"] or 1)
        order = [p for p in pages if p.get("page") == wanted] + \
                [p for p in pages if p.get("page") != wanted]
        last_error = "no readable page"
        for entry in order:
            try:
                with archive_mod.open_archive(row["path"]) as handle:
                    raw = handle.read(entry["name"])
                thumb = archive_mod.make_thumbnail(raw)
            except Exception as exc:  # noqa: BLE001 - try the next page
                last_error = f"{entry['name']}: {exc}"
                continue
            os.makedirs(thumb_dir, exist_ok=True)
            tmp = cache_file + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(thumb)
            os.replace(tmp, cache_file)
            return Response(content=thumb, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
        raise HTTPException(status_code=404, detail=f"thumbnail failed: {last_error}")

    # -------------------------------------------------------------- progress
    @app.post("/api/comics/{comic_id}/progress")
    def save_progress(comic_id: int, payload: ProgressIn) -> dict:
        fetch(comic_id)
        with write_lock:
            return db_mod.set_progress(conn, comic_id, last_page=payload.last_page)

    @app.post("/api/comics/{comic_id}/read")
    def save_read(comic_id: int, payload: ReadIn) -> dict:
        fetch(comic_id)
        with write_lock:
            return db_mod.set_progress(conn, comic_id, read=payload.read)

    # ------------------------------------------------------------------ scan
    @app.post("/api/scan")
    def rescan() -> dict:
        with write_lock:
            try:
                return scan_library(comics_path, db_path)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @app.on_event("shutdown")
    def _shutdown() -> None:  # pragma: no cover - tidy close on Ctrl+C
        try:
            conn.close()
        except Exception:
            pass

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m longbox.server",
        description="Serve the Longbox JSON API for a comics folder.",
    )
    parser.add_argument("--path", default=os.environ.get("LONGBOX_COMICS"),
                        help="comics folder (env: LONGBOX_COMICS)")
    parser.add_argument("--db", default=db_mod.default_db_path(),
                        help="SQLite database file (env: LONGBOX_DB)")
    parser.add_argument("--thumb-dir", default=os.environ.get("LONGBOX_THUMB_DIR"),
                        help="thumbnail cache folder (env: LONGBOX_THUMB_DIR, "
                             "default: <db folder>/thumbs)")
    parser.add_argument("--host", default=os.environ.get("LONGBOX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LONGBOX_PORT", "8765")))
    return parser


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    args = build_parser().parse_args(argv)
    if not args.path:
        print("error: --path is required (the comics folder)")
        return 2
    app = create_app(args.path, args.db, args.thumb_dir)
    print(f"Longbox API on http://{args.host}:{args.port}")
    print(f"  comics folder : {os.path.abspath(args.path)}")
    print(f"  database      : {os.path.abspath(args.db)}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
