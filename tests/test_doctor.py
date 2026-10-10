"""Tests for the library integrity checker (``python -m longbox.doctor``).

The fixtures come from ``scripts/make_doctor_fixtures.py``: the standard library
plus a planted identical duplicate, a planted lower-quality duplicate, a planted
missing issue number in a reading-order run, a single-issue series, a series whose
numbering spans two volumes, and an archive that lists fine but cannot be read.

Read-only is not an assumption here: one test snapshots every file in the comics
folder (name, size, mtime, hash) before and after a full run, ``--deep`` included,
and fails if anything changed or appeared.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from longbox import archive as archive_mod  # noqa: E402
from longbox import db as db_mod  # noqa: E402
from longbox import doctor  # noqa: E402
from longbox import scanner  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAKE = _load("longbox_make_fixtures", ROOT / "scripts" / "make_fixtures.py")
DOCTOR_FIXTURES = _load("longbox_make_doctor_fixtures",
                        ROOT / "scripts" / "make_doctor_fixtures.py")


def copy_db(source: str, destination: Path) -> str:
    """WAL-safe copy of the index (shutil alone can lose the newest rows)."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(destination)


def snapshot(folder: Path) -> dict:
    """Every file under `folder`: name, size, mtime and hash. Read-only."""
    state = {}
    for dirpath, _dirnames, filenames in os.walk(folder):
        for filename in filenames:
            full = Path(dirpath) / filename
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
            stat = full.stat()
            state[str(full.relative_to(folder))] = (stat.st_size, stat.st_mtime_ns, digest)
    return state


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """The doctor fixture library, scanned once for the whole module."""
    folder = tmp_path_factory.mktemp("doctor_comics")
    planted = DOCTOR_FIXTURES.build(folder, ROOT / "samples")
    db_path = tmp_path_factory.mktemp("doctor_data") / "longbox.db"
    summary = scanner.scan_library(str(folder), str(db_path))
    return {"folder": folder, "db": str(db_path), "planted": planted, "summary": summary}


@pytest.fixture(scope="module")
def shallow(library):
    """The default (database-only) report, shared by most tests."""
    return doctor.run_doctor(str(library["folder"]), library["db"], save=False)


@pytest.fixture(scope="module")
def deep(library):
    """One --deep report, shared (it is the slow one)."""
    calls: list[tuple] = []
    report = doctor.run_doctor(str(library["folder"]), library["db"], deep=True, save=False,
                               deep_progress=lambda count, total, path: calls.append(
                                   (count, total, path)))
    report["_progress"] = calls
    return report


def _issues(report, label):
    return [issue for issue in report["duplicates"]["issues"] if issue["label"] == label]


def _series(report, name):
    return [item for item in report["missing_numbers"]["series"] if item["series"] == name]


# ------------------------------------------------------------- 1. unreadable


def test_unreadable_groups_match_the_index(library, shallow):
    """Grouped by reason with counts - the shape the problems panel uses."""
    groups = {group["kind"]: group for group in shallow["unreadable"]["groups"]}
    assert groups["container_unknown"]["count"] == 1
    assert groups["rar_tool_missing"]["count"] == 1
    assert "Broken Scan 04 (2015).cbz" in groups["container_unknown"]["files"][0]["filename"]
    assert groups["container_unknown"]["fix"]          # every group carries what to do
    assert shallow["unreadable"]["total"] == 2
    assert shallow["summary"]["unreadable"] == 2
    # the shallow pass is database only: no deep section, nothing opened
    assert "deep_check" not in shallow


def test_shallow_pass_never_opens_an_archive(library, monkeypatch):
    opened: list[str] = []

    def boom(path):
        opened.append(path)
        raise AssertionError("the shallow pass opened an archive")

    monkeypatch.setattr(archive_mod, "open_archive", boom)
    report = doctor.run_doctor(str(library["folder"]), library["db"], save=False)
    assert opened == []
    assert report["unreadable"]["total"] == 2          # still reported, from the index


def test_deep_pass_finds_what_a_listing_cannot(library, deep):
    """The planted archive lists fine (it scanned as ok) and fails only when read."""
    broken = library["planted"]["truncated"]
    groups = {group["kind"]: group for group in deep["deep_check"]["groups"]}
    assert "deep_corrupt_member" in groups
    files = [item["path"] for item in groups["deep_corrupt_member"]["files"]]
    assert broken in files
    assert deep["deep_check"]["failed"] == 1
    assert "page3.jpg" in groups["deep_corrupt_member"]["reason"]
    # the shallow index says this file is fine, which is exactly the point
    rows = {entry["path"]: entry for entry in doctor.load_rows(
        db_mod.connect(library["db"]), str(library["folder"]))}
    assert rows[broken]["status"] == "ok"
    assert deep["summary"]["deep_failures"] == 1
    # and the finding is persisted-shaped, not a separate report format
    kinds = [finding["kind"] for finding in deep["findings"] if finding["section"] == "unreadable"]
    assert "deep_corrupt_member" in kinds


def test_deep_pass_reports_progress_for_every_file(library, deep):
    progress = deep["_progress"]
    assert len(progress) == len(doctor.load_rows(db_mod.connect(library["db"]),
                                                 str(library["folder"])))
    assert progress[0][0] == 1 and progress[0][1] == len(progress)
    assert progress[-1][0] == progress[-1][1]


def test_nothing_is_written_inside_the_comics_folder(library):
    """A full run, --deep included, leaves the comics folder byte for byte identical."""
    before = snapshot(library["folder"])
    doctor.run_doctor(str(library["folder"]), library["db"], deep=True, save=False)
    after = snapshot(library["folder"])
    assert after == before
    assert not [name for name in after if name.endswith(".tmp") or name.endswith(".part")]
    assert not [name for name in after if os.path.basename(name).startswith("longbox")]


# ------------------------------------------------------------- 2. duplicates


def test_identical_duplicate_is_reported(library, shallow):
    issues = _issues(shallow, "Saga #12")
    assert len(issues) == 1
    issue = issues[0]
    assert issue["identical"] is True
    assert issue["kind"] == "identical_duplicate"
    assert len(issue["copies"]) == 2
    assert len({(copy["file_size"], copy["page_count"]) for copy in issue["copies"]}) == 1
    assert "same file" in issue["verdict"]
    assert "never deletes" in issue["suggestion"]
    assert {copy["path"] for copy in issue["copies"]} == set(library["planted"]["identical"])
    # the two copies really are in different folders
    assert len({copy["folder"] for copy in issue["copies"]}) == 2


def test_same_issue_at_two_qualities_names_the_better_copy(library, shallow):
    issues = _issues(shallow, "Ultimate Spider-Man #1")
    assert len(issues) == 1
    issue = issues[0]
    better, worse = library["planted"]["lowres"]
    assert issue["identical"] is False
    assert issue["kind"] == "quality_differs"
    assert issue["keeper"] == better
    assert "different quality" in issue["verdict"]
    assert "suggestion only" in issue["suggestion"]
    assert issue["copies"][0]["dimensions"] == [1988, 3056]
    assert issue["copies"][1]["dimensions"] == [800, 1200]
    assert issue["copies"][1]["path"] == worse
    # every copy carries the evidence the owner needs to judge
    for copy in issue["copies"]:
        assert copy["folder"] and copy["file_size"] and copy["page_count"]
        assert copy["container"] in {"zip", "rar4", "rar5", "7z", "unknown"}
        assert isinstance(copy["comicinfo_present"], bool)


def test_duplicate_section_counts_and_report_lines(library, shallow):
    assert shallow["duplicates"]["count"] == 2
    assert shallow["duplicates"]["files"] == 4
    text = doctor.format_report(shallow)
    assert "2. Duplicate issues - 2 issue(s)" in text
    assert "verdict   :" in text
    assert "suggestion:" in text


# ------------------------------------------------------------- 3. runs and gaps


def test_missing_number_in_a_reading_order_run(library, shallow):
    series = _series(shallow, "Civil War")
    assert len(series) == 1
    run = series[0]
    assert run["present"] == [1, 2, 4]
    assert run["gaps"] == [3]
    assert run["have"] == "1-2, 4"
    assert run["complete"] is False
    assert run["ambiguous"] is False
    assert run["files"] == 3


def test_reading_order_prefix_creates_no_fake_series_or_gap(library, shallow):
    """`001- Civil War 1.cbz` is one file in *Civil War*, not a series of its own."""
    keys = {item["series_key"] for item in shallow["missing_numbers"]["series"]}
    assert "civilwar" in keys
    assert not [key for key in keys if key and key[0].isdigit()]
    assert not [item for item in shallow["missing_numbers"]["series"]
                if item["series"] and item["series"][0].isdigit()]
    assert shallow["missing_numbers"]["order_prefix_files"] == 3
    # the three reading-order files group into exactly one series with one gap
    assert shallow["missing_numbers"]["series_with_gaps"] == 1
    assert "reading-order" in doctor.format_report(shallow)


def test_single_issue_series_is_not_reported_as_a_gap(library, shallow):
    assert library["planted"]["solo"]
    assert _series(shallow, "Solo Run") == []


def test_ambiguous_numbering_is_flagged_not_asserted(library, shallow):
    run = _series(shallow, "Loki")[0]
    assert run["gaps"] == [6]              # the numbers really are missing 6
    assert run["ambiguous"] is True        # but they span two volumes
    assert "two volumes" in run["note"]
    assert run["volumes"] == ["1", "2"]
    # it is reported, yet it does not count as a confident gap
    assert shallow["missing_numbers"]["series_with_gaps"] == 1
    text = doctor.format_report(shallow)
    assert "possibly missing 6" in text
    assert "Loki: have 1-5, 7" in text


def test_noise_is_skipped_and_counted(library, shallow):
    """annuals, one-shots and non-numeric numbers are excluded and said out loud."""
    missing = shallow["missing_numbers"]
    assert missing["skipped"]["annuals"] == 1                 # X-Men Annual 3
    assert missing["skipped_small_series"] >= 1               # Solo Run, Saga, Batman...
    assert all(len(item["present"]) >= 3 for item in missing["series"])
    text = doctor.format_report(shallow)
    assert "Not counted as runs:" in text


def _tiny_library(tmp_path, spec):
    """Build (and scan) a small library from (filename, pages, comicinfo) triples."""
    folder = tmp_path / "comics"
    for filename, pages, comicinfo in spec:
        MAKE.write_cbz(folder / filename, pages, comicinfo=comicinfo)
    db_path = tmp_path / "data" / "longbox.db"
    scanner.scan_library(str(folder), str(db_path))
    return folder, str(db_path)


def test_null_and_non_numeric_issue_numbers_are_skipped(tmp_path):
    folder, db_path = _tiny_library(tmp_path, [
        ("Wibble One Shot (2015).cbz", 3, None),                     # no issue number
        ("Wibble 6.5 (2015).cbz", 3, None),                          # non-numeric number
        ("Wibble 1 (2015).cbz", 3, None),
        ("Wibble 2 (2015).cbz", 3, None),
        ("Wibble 4 (2015).cbz", 3, None),
    ])
    report = doctor.run_doctor(str(folder), db_path, save=False)
    skipped = report["missing_numbers"]["skipped"]
    assert skipped["no_issue_number"] == 1
    assert skipped["non_numeric"] == 1
    run = _series(report, "Wibble")[0]
    assert run["have"] == "1-2, 4" and run["gaps"] == [3]


# ------------------------------------------------------------- report / JSON / exit codes


def test_exit_code_zero_when_nothing_is_wrong(tmp_path):
    folder, db_path = _tiny_library(tmp_path, [
        ("Clean One 1 (2020).cbz", 3, MAKE.build_comicinfo("Clean One", "1", "1", 2020,
                                                           page_count=3)),
        ("Other Title 1 (2021).cbz", 3, MAKE.build_comicinfo("Other Title", "1", "1", 2021,
                                                             page_count=3)),
    ])
    assert doctor.main(["--path", str(folder), "--db", db_path, "--no-save"]) == 0


def test_exit_code_and_text_report(library, capsys):
    assert doctor.main(["--path", str(library["folder"]), "--db", library["db"],
                        "--no-save"]) == 1
    text = capsys.readouterr().out
    assert "1. Unreadable or broken archives" in text
    assert "2. Duplicate issues" in text
    assert "3. Missing issue numbers in a run" in text
    assert "nothing was written, moved or renamed inside the comics folder" in text
    assert "Exit code 1: findings exist." in text
    assert "Broken Member 01 (2020).cbz" not in text      # only --deep finds it


def test_usage_exit_codes(tmp_path, library):
    assert doctor.main(["--path", str(tmp_path / "nope"), "--db", library["db"]]) == 2
    assert doctor.main(["--path", str(library["folder"]),
                        "--db", str(tmp_path / "no-such.db")]) == 2


def test_json_output_shape(library, capsys):
    code = doctor.main(["--path", str(library["folder"]), "--db", library["db"],
                        "--json", "--no-save"])
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exit_code"] == 1
    assert payload["schema"] == 1
    assert payload["deep"] is False
    assert set(payload) >= {"schema", "generated_at", "comics_path", "db", "deep",
                            "files_indexed", "unreadable", "duplicates",
                            "missing_numbers", "summary", "findings", "exit_code"}
    assert set(payload["summary"]) >= {"files", "unreadable", "duplicate_issues",
                                       "series_with_gaps", "findings"}
    group = payload["unreadable"]["groups"][0]
    assert set(group) == {"kind", "count", "reason", "fix", "files"}
    issue = payload["duplicates"]["issues"][0]
    assert set(issue) >= {"series", "series_key", "issue_number", "label", "copies",
                          "identical", "kind", "verdict", "suggestion", "keeper"}
    assert set(issue["copies"][0]) >= {"path", "filename", "folder", "file_size",
                                       "page_count", "container", "comicinfo_present",
                                       "dimensions"}
    run = payload["missing_numbers"]["series"][0]
    assert set(run) >= {"series", "series_key", "present", "have", "gaps", "complete",
                        "ambiguous"}
    finding = payload["findings"][0]
    assert set(finding) == {"section", "severity", "kind", "series", "series_key",
                            "issue_number", "path", "summary", "detail"}
    json.dumps(payload)                                    # serialisable, no sets


def test_deep_json_reports_the_deep_section(library, capsys):
    assert doctor.main(["--path", str(library["folder"]), "--db", library["db"], "--json",
                        "--deep", "--no-save"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["deep"] is True
    assert payload["deep_check"]["failed"] == 1
    assert payload["deep_check"]["checked"] == payload["files_indexed"]
    assert "deep check" in captured.err                    # the slow mode says so up front
    assert "reading [" in captured.err                     # and prints progress


# ------------------------------------------------------------- persistence


def test_findings_are_persisted_to_the_database(library, tmp_path):
    db_path = copy_db(library["db"], tmp_path / "longbox.db")
    report = doctor.run_doctor(str(library["folder"]), db_path, deep=True)
    conn = db_mod.connect(db_path)
    try:
        runs = conn.execute("SELECT * FROM doctor_runs").fetchall()
        findings = conn.execute("SELECT * FROM doctor_findings").fetchall()
    finally:
        conn.close()
    assert report["run_id"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == report["run_id"]
    assert runs[0]["deep"] == 1
    assert runs[0]["exit_code"] == 1
    assert json.loads(runs[0]["summary"])["findings"] == len(report["findings"])
    assert len(findings) == len(report["findings"])
    sections = {row["section"] for row in findings}
    assert sections == {"unreadable", "duplicate", "missing_number"}
    kinds = {row["kind"] for row in findings}
    assert {"identical_duplicate", "quality_differs", "gap", "deep_corrupt_member"} <= kinds
    for row in findings:
        assert row["severity"] in {"error", "warning", "info"}
        assert row["summary"]
        json.loads(row["detail"])


def test_second_run_replaces_the_previous_findings(library, tmp_path):
    """A stale finding must not survive into the next run: the panel shows the latest."""
    db_path = copy_db(library["db"], tmp_path / "longbox.db")
    doctor.run_doctor(str(library["folder"]), db_path)
    conn = db_mod.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO doctor_findings (run_id, section, severity, kind, summary, created_at)"
            " VALUES ('stale-run', 'unreadable', 'error', 'stale_kind', 'stale', 'then')")
        conn.commit()
    finally:
        conn.close()
    second = doctor.run_doctor(str(library["folder"]), db_path)
    runs = conn_raw(db_path, "doctor_runs")
    findings = conn_raw(db_path, "doctor_findings")
    assert len(runs) == 1
    assert not [row for row in findings if row["kind"] == "stale_kind"]
    assert {row["run_id"] for row in findings} == {runs[0]["run_id"]}
    assert second["run_id"] == runs[0]["run_id"]
    assert len(findings) == len(second["findings"])


def conn_raw(db_path, table):
    conn = db_mod.connect(db_path)
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def test_no_save_leaves_no_findings_in_the_database(library, tmp_path):
    db_path = copy_db(library["db"], tmp_path / "longbox.db")
    report = doctor.run_doctor(str(library["folder"]), db_path, save=False)
    rows = conn_raw(db_path, "doctor_findings")
    runs = conn_raw(db_path, "doctor_runs")
    assert rows == [] and runs == []
    assert report["findings"] and "run_id" not in report


def test_index_is_not_modified_by_a_check(library, tmp_path):
    """The doctor may add its own tables but never touches a `comics` row."""
    db_path = copy_db(library["db"], tmp_path / "longbox.db")
    before = conn_raw(db_path, "comics")
    doctor.run_doctor(str(library["folder"]), db_path, deep=True)
    after = conn_raw(db_path, "comics")
    assert before == after
    progress_before = conn_raw(db_path, "progress")
    assert progress_before == []
