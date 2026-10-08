"""Library scanner: walk a comics folder, index every .cbz/.cbr into SQLite.

The comics folder is read-only: archives are opened for reading, nothing is
extracted and nothing is written next to them.  A file that cannot be read
becomes a row with status='error' (never a crash), and a file that disappeared
becomes status='missing' rather than silently staying "present".
"""

from __future__ import annotations

import os
import time
from typing import Callable, Iterator

from . import archive as archive_mod
from . import comicinfo as comicinfo_mod
from . import db as db_mod
from . import parser as parser_mod

ProgressCallback = Callable[[int, int, str], None]


def iter_archives(root: str) -> Iterator[str]:
    """Every .cbz/.cbr under `root`, recursively, case-insensitive, sorted."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].casefold() in archive_mod.ARCHIVE_EXTENSIONS:
                yield os.path.join(dirpath, filename)


def _merge(preferred, fallback):
    """ComicInfo wins when non-empty, filename value survives in filename_parsed."""
    if isinstance(preferred, (list, tuple)):
        return list(preferred) if preferred else list(fallback or [])
    if preferred is None or (isinstance(preferred, str) and not preferred.strip()):
        return fallback
    return preferred


def _pages_payload(page_names: list[str], info_pages: list[dict] | None) -> list[dict]:
    """Page list for the DB: 1-based page number + member name, no bytes."""
    info_pages = info_pages or []
    by_index = {}
    for entry in info_pages:
        index = entry.get("image")
        if isinstance(index, int):
            by_index[index] = entry
    payload = []
    for position, name in enumerate(page_names, start=1):
        info = by_index.get(position - 1, {})
        payload.append({
            "page": position,
            "name": name.replace("\\", "/"),
            "size": info.get("size"),
            "width": info.get("width"),
            "height": info.get("height"),
            "type": info.get("type"),
        })
    return payload


def read_comic(path: str) -> dict:
    """Read one archive. Returns index data; raises archive.ArchiveError."""
    filename_parsed = parser_mod.parse_filename(path)
    stat = os.stat(path)
    data: dict = {
        "path": os.path.abspath(path),
        "filename": os.path.basename(path),
        "file_size": stat.st_size,
        "mtime": stat.st_mtime,
        "archive_type": archive_mod.archive_type(path),
        "filename_parsed": filename_parsed,
        "status": "ok",
        "error_message": None,
        "comicinfo_present": 0,
        "comicinfo_raw": None,
    }

    with archive_mod.open_archive(path) as handle:
        page_names = handle.pages()
        info_member = handle.comicinfo_member()
        info = None
        if info_member:
            raw = handle.read(info_member)
            try:
                info = comicinfo_mod.parse_comicinfo(raw)
                data["comicinfo_raw"] = raw.decode("utf-8", errors="replace")
                data["comicinfo_present"] = 1
            except comicinfo_mod.ComicInfoError as exc:
                data["error_message"] = f"ComicInfo.xml unreadable: {exc}"

    name_series = filename_parsed.get("series")
    name_year = filename_parsed.get("year")
    name_volume = filename_parsed.get("volume")
    name_issue = filename_parsed.get("issue_number")

    series = _merge(info["series"] if info else None, name_series)
    issue_number = _merge(info["number"] if info else None, name_issue)
    year = _merge(info["year"] if info else None, name_year)
    volume_raw = _merge(info["volume"] if info else None, name_volume)

    data.update({
        "series": series,
        "series_key": parser_mod.normalise_series(series),
        "issue_number": issue_number,
        "issue_sort": parser_mod._issue_sort_value(issue_number),
        "year": year,
        "month": info["month"] if info else None,
        "day": info["day"] if info else None,
        "title": info["title"] if info else None,
        "publisher": info["publisher"] if info else None,
        "summary": info["summary"] if info else None,
        "notes": info["notes"] if info else None,
        "web": info["web"] if info else None,
        "scan_information": info["scan_information"] if info else None,
        "writers": info["writers"] if info else [],
        "pencillers": info["pencillers"] if info else [],
        "inkers": info["inkers"] if info else [],
        "colorists": info["colorists"] if info else [],
        "letterers": info["letterers"] if info else [],
        "cover_artists": info["cover_artists"] if info else [],
        "editors": info["editors"] if info else [],
        "characters": info["characters"] if info else [],
        "teams": info["teams"] if info else [],
        "locations": info["locations"] if info else [],
        "volume_kind": info["volume_kind"] if info else ("volume_number" if volume_raw else None),
        "volume_raw": volume_raw,
        "comicvine_issue_id": info["comicvine_issue_id"] if info else None,
        "comicinfo_page_count": info["page_count"] if info else None,
        "page_count": len(page_names),
        "pages": _pages_payload(page_names, info["pages"] if info else None),
        "cover_page": archive_mod.cover_position(page_names, info["pages"] if info else None)
        if page_names else None,
    })
    return data


def scan_library(comics_path: str, db_path: str,
                 progress: ProgressCallback | None = None) -> dict:
    """Index every archive under `comics_path`; returns a summary dict."""
    started = time.time()
    comics_path = os.path.abspath(os.path.expanduser(comics_path))
    if not os.path.isdir(comics_path):
        raise FileNotFoundError(f"comics folder not found: {comics_path}")

    conn = db_mod.open_db(db_path)
    seen: set[str] = set()
    summary = {
        "path": comics_path,
        "db": os.path.abspath(db_path),
        "found": 0,
        "indexed": 0,
        "updated": 0,
        "errors": 0,
        "missing": 0,
        "error_files": [],
        "elapsed": 0.0,
    }

    try:
        before = {row["path"] for row in conn.execute("SELECT path FROM comics").fetchall()}
        for count, path in enumerate(iter_archives(comics_path), start=1):
            seen.add(os.path.normcase(os.path.abspath(path)))
            summary["found"] += 1
            if progress:
                progress(count, 0, path)
            try:
                data = read_comic(path)
            except archive_mod.ArchiveError as exc:
                data = {
                    "path": os.path.abspath(path),
                    "filename": os.path.basename(path),
                    "archive_type": archive_mod.archive_type(path),
                    "filename_parsed": parser_mod.parse_filename(path),
                    "file_size": os.path.getsize(path) if os.path.exists(path) else None,
                    "mtime": os.path.getmtime(path) if os.path.exists(path) else None,
                    "status": "error",
                    "error_message": str(exc),
                }
                parsed = data["filename_parsed"]
                data.update({
                    "series": parsed["series"],
                    "series_key": parsed["series_key"],
                    "issue_number": parsed["issue_number"],
                    "issue_sort": parsed["issue_sort"],
                    "year": parsed["year"],
                    "volume_raw": parsed["volume"],
                    "volume_kind": "volume_number" if parsed["volume"] else None,
                    "writers": [], "pencillers": [], "inkers": [], "colorists": [],
                    "letterers": [], "cover_artists": [], "editors": [],
                    "characters": [], "teams": [], "locations": [],
                    "pages": [], "comicinfo_present": 0, "comicinfo_raw": None,
                })
            except Exception as exc:  # never let one bad file stop the scan
                data = {
                    "path": os.path.abspath(path),
                    "filename": os.path.basename(path),
                    "status": "error",
                    "error_message": f"unexpected error: {exc.__class__.__name__}: {exc}",
                }

            # a readable archive with a broken ComicInfo.xml is still indexed,
            # but it is reported as an error so it shows up in the stats
            is_new = os.path.abspath(path) not in before
            db_mod.upsert_comic(conn, data)
            if data.get("status") == "error":
                summary["errors"] += 1
                summary["error_files"].append((data["path"], data.get("error_message")))
            elif is_new:
                summary["indexed"] += 1
            else:
                summary["updated"] += 1

        summary["missing"] = db_mod.mark_missing(conn, seen, comics_path)
    finally:
        conn.close()

    summary["elapsed"] = round(time.time() - started, 2)
    summary["total_in_db"] = _count(db_path)
    return summary


def _count(db_path: str) -> int:
    conn = db_mod.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) AS c FROM comics").fetchone()["c"])
    finally:
        conn.close()
