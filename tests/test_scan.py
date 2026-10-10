"""Scanner behaviour: indexing, mixed volumes, errors, idempotency, missing files."""

from __future__ import annotations

import shutil
from pathlib import Path

from longbox import archive as archive_mod
from longbox import scanner
from longbox import tools as tools_mod

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "comicinfo_xmen_11.xml"


def test_summary_counts(scan_summary):
    assert scan_summary["found"] == 15
    # 13 readable - including a zip renamed .cbr - plus 2 unreadable = 15
    assert scan_summary["indexed"] == 13
    assert scan_summary["errors"] == 2            # corrupt .cbz + RAR with no tool
    assert scan_summary["missing"] == 0
    assert scan_summary["total_in_db"] == 15


def test_corrupt_archive_is_recorded_as_an_error_not_a_crash(conn):
    row = conn.execute(
        "SELECT * FROM comics WHERE filename = 'Broken Scan 04 (2015).cbz'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "error"
    # the bytes identify no container at all, so the reason is the honest one: we
    # do not know what this file is - not "corrupt zip", which would claim we did
    assert row["error_kind"] == archive_mod.ERROR_CONTAINER_UNKNOWN
    assert "not a comic archive" in row["error_message"].lower()
    assert row["archive_container"] == "unknown"
    # the filename is still parsed, so the row is useful even when unreadable
    assert row["series"] == "Broken Scan"
    assert row["issue_number"] == "4"
    assert row["year"] == 2015


def test_zip_named_cbr_is_indexed_by_its_magic_bytes(conn):
    """A .cbr whose bytes are really a zip opens with the standard library."""
    row = conn.execute(
        "SELECT * FROM comics WHERE filename = 'Batman #405 (1987) (digital).cbr'"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["archive_container"] == "zip"
    assert row["container_mismatch"] == 1          # named .cbr, really a zip
    assert row["page_count"] == 3
    assert row["error_message"] is None
    pages = __import__("json").loads(row["pages"])
    assert [p["name"] for p in pages] == ["page1.jpg", "page2.jpg", "page3.jpg"]


def test_rar_signature_is_recorded_with_one_actionable_reason(conn):
    row = conn.execute(
        "SELECT * FROM comics WHERE filename = 'RAR Signature Only 1 (2025).cbr'"
    ).fetchone()
    assert row["archive_container"] == "rar4"
    assert row["container_mismatch"] == 0
    assert row["status"] == "error"
    # either this PC has no RAR tool at all, or the one it has cannot list this
    # signature-only file: both are ONE grouped reason, never a crash or a silent row
    assert row["error_kind"] in {"rar_tool_missing", "archive_tool_failed"}
    assert row["path"] and row["file_size"] == 71
    if row["error_kind"] == "rar_tool_missing":
        assert row["error_message"] == tools_mod.RAR_TOOL_MISSING_MESSAGE


def test_mixed_volume_one_series_stays_one_series(conn):
    rows = conn.execute(
        "SELECT series, series_key, volume_raw, volume_kind, issue_number "
        "FROM comics WHERE series = 'X-Men' ORDER BY issue_sort"
    ).fetchall()
    assert [r["issue_number"] for r in rows] == ["11", "12", "13", "14"]
    # one series_key for all four issues, whatever <Volume> says
    assert {r["series_key"] for r in rows} == {"xmen"}
    assert {r["volume_raw"] for r in rows} == {"2024", "158814"}
    assert {r["volume_kind"] for r in rows} == {"volume_year", "volume_id"}
    assert len({(r["series_key"], r["volume_raw"]) for r in rows}) > 1  # the trap exists


def test_real_comicinfo_sample_is_indexed_losslessly(conn):
    row = conn.execute(
        "SELECT * FROM comics WHERE filename LIKE 'X-Men 011%'"
    ).fetchone()
    assert row["comicinfo_present"] == 1
    assert row["comicinfo_raw"] == SAMPLE.read_text(encoding="utf-8")
    assert "Cyclops [1459]" in row["characters"]
    assert "X-Men [3173]" in row["teams"]
    assert "Alaska [55827]" in row["locations"]
    assert row["comicvine_issue_id"] == "1095610"
    assert row["volume_raw"] == "2024" and row["volume_kind"] == "volume_year"
    # the real ComicInfo says 27 pages and this fixture really has 27 pages
    assert row["comicinfo_page_count"] == 27
    assert row["page_count"] == 27


def test_issue_without_creator_credits(conn):
    row = conn.execute("SELECT * FROM comics WHERE filename = 'X-Men 014 (2025).cbz'").fetchone()
    assert row["status"] == "ok"
    assert row["comicinfo_present"] == 1
    assert row["writers"] == "[]" and row["pencillers"] == "[]"
    assert row["publisher"] == "Marvel"


def test_comicinfo_wins_over_filename_but_both_are_kept(conn):
    row = conn.execute(
        "SELECT * FROM comics WHERE filename LIKE 'Spider-Man 2099%'"
    ).fetchone()
    assert row["year"] == 1993                      # ComicInfo wins
    assert row["filename_parsed"] is not None
    parsed = __import__("json").loads(row["filename_parsed"])
    assert parsed["year"] == 1992                   # the disagreement is preserved
    assert parsed["series"] == "Spider-Man 2099"


def test_nested_subfolders_are_scanned(conn):
    row = conn.execute("SELECT * FROM comics WHERE filename LIKE 'Spider-Man 2099%'").fetchone()
    assert row is not None and row["status"] == "ok"
    assert Path(row["path"]).parent.name == "Sub Folder"


def test_file_without_comicinfo_uses_the_filename(conn):
    row = conn.execute(
        "SELECT * FROM comics WHERE filename LIKE 'Bêtes%'"
    ).fetchone()
    assert row["comicinfo_present"] == 0
    assert row["comicinfo_raw"] is None
    assert row["series"] == "Bêtes du Nord Töme"
    assert row["issue_number"] == "3"
    assert row["year"] == 2019
    assert row["page_count"] == 3


def test_pages_are_in_natural_order_and_carry_their_metadata(conn):
    row = conn.execute("SELECT * FROM comics WHERE filename LIKE 'Saga%'").fetchone()
    pages = __import__("json").loads(row["pages"])
    assert [p["page"] for p in pages] == list(range(1, 11))
    # names 1.jpg .. 10.jpg, so lexicographic order would put 10.jpg second
    assert [p["name"] for p in pages][:3] == ["1.jpg", "2.jpg", "3.jpg"]
    assert pages[9]["name"] == "10.jpg"
    assert pages[0]["type"] == "FrontCover"
    assert pages[0]["width"] == 1988 and pages[0]["height"] == 3056
    assert pages[-1]["width"] == 160         # the smaller junk last page
    assert row["cover_page"] == 1


def test_front_cover_page_is_used_for_the_cover(conn):
    row = conn.execute("SELECT * FROM comics WHERE filename LIKE 'X-Men 011%'").fetchone()
    pages = __import__("json").loads(row["pages"])
    assert pages[0]["name"] == "page1.jpg"
    assert pages[9]["name"] == "page10.jpg"      # natural, not lexicographic
    assert row["cover_page"] == 1


def test_rescan_is_idempotent(result):
    first = result.scan()
    before = {row["path"]: row["id"] for row in result.query("SELECT id, path FROM comics")}
    second = result.scan()
    after = {row["path"]: row["id"] for row in result.query("SELECT id, path FROM comics")}
    assert first["found"] == second["found"] == 15
    assert second["indexed"] == 0
    assert second["updated"] == 13  # the 2 unreadable files stay errors
    assert second["errors"] == 2
    assert before == after                       # same rows, same ids, no duplicates


def test_progress_survives_a_rescan(result):
    from longbox import db as db_mod

    result.scan()
    row = result.row("Saga 12 (2014) (Digital) (Zone-Empire).cbz")
    connection = db_mod.open_db(result.db)
    try:
        db_mod.set_progress(connection, row["id"], last_page=4)
    finally:
        connection.close()
    result.scan()
    connection = db_mod.open_db(result.db)
    try:
        progress = db_mod.get_progress(connection, row["id"])
    finally:
        connection.close()
    assert progress["last_page"] == 4


def test_disappeared_file_is_marked_missing_not_left_present(result):
    result.scan()
    target = result.folder / "Saga 12 (2014) (Digital) (Zone-Empire).cbz"
    target.unlink()
    summary = result.scan()
    assert summary["missing"] == 1
    row = result.row("Saga 12 (2014) (Digital) (Zone-Empire).cbz")
    assert row["status"] == "missing"
    assert row["path"]  # the row is kept, so history is not lost


def test_scanning_a_missing_folder_raises_a_clear_error(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        scanner.scan_library(str(tmp_path / "nope"), str(tmp_path / "x.db"))


def test_scan_never_writes_inside_the_comics_folder(result):
    before = sorted(p.name for p in result.folder.rglob("*"))
    result.scan()
    after = sorted(p.name for p in result.folder.rglob("*"))
    assert before == after


def test_unicode_path_is_indexed_without_mojibake(conn):
    row = conn.execute("SELECT * FROM comics WHERE series LIKE 'Bêtes%'").fetchone()
    assert "Bêtes" in row["series"]
    assert Path(row["path"]).name.startswith("Bêtes")
