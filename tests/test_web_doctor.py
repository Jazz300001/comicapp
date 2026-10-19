"""The library doctor's findings in the web app.

`python -m longbox.doctor` stores what it found in `doctor_findings`; the app
only reads that table (`/api/doctor`) and the panel in `static/app.js` renders
it.  These tests pin the payload the panel depends on and that showing it can
never change a comic file.
"""
from __future__ import annotations
import hashlib
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from longbox import db as db_mod  # noqa: E402
from longbox import doctor  # noqa: E402
from longbox import scanner  # noqa: E402
from longbox import server  # noqa: E402

STATIC = ROOT / "longbox" / "static"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def snapshot(folder: str) -> dict:
    """Every file under `folder`: size, mtime and hash. Read-only."""
    state = {}
    for dirpath, _dirnames, filenames in os.walk(folder):
        for filename in filenames:
            full = Path(dirpath) / filename
            stat = full.stat()
            state[str(full.relative_to(folder))] = (
                stat.st_size, stat.st_mtime_ns, hashlib.sha256(full.read_bytes()).hexdigest())
    return state


@pytest.fixture(scope="module")
def checked_library(tmp_path_factory):
    """The doctor fixture library, scanned and checked once for this module."""
    folder = tmp_path_factory.mktemp("doctor_web_comics")
    DOCTOR_FIXTURES.build(folder, ROOT / "samples")
    db_path = tmp_path_factory.mktemp("doctor_web_data") / "longbox.db"
    scanner.scan_library(str(folder), str(db_path))
    report = doctor.run_doctor(str(folder), str(db_path))       # saves its findings
    return {"folder": str(folder), "db": str(db_path), "report": report}


@pytest.fixture()
def checked_client(checked_library, tmp_path):
    """The app in front of an index that has a doctor run stored in it."""
    from fastapi.testclient import TestClient
    fresh = copy_db(checked_library["db"], tmp_path / "longbox.db")
    app = server.create_app(checked_library["folder"], fresh, str(tmp_path / "thumbs"))
    with TestClient(app) as test_client:
        yield test_client


def _section(payload, name):
    return next((item for item in payload["sections"] if item["section"] == name), None)


# ------------------------------------------------------------- no run yet
def test_without_a_run_the_panel_says_which_command_to_run(client):
    """`client` has an index that no check has ever looked at."""
    response = client.get("/api/doctor")
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_run"] is False
    assert payload["run"] is None
    assert payload["sections"] == [] and payload["total"] == 0
    assert payload["command"] == "python -m longbox.doctor"
    assert payload["deep_command"] == "python -m longbox.doctor --deep"
    assert "read-only" in payload["note"].lower()
    assert "move" not in payload["note"].lower() or "never" in payload["note"].lower()


def test_a_run_with_no_findings_renders_empty_not_broken(checked_client):
    """Coverage for the other empty case: a run that found nothing to report."""
    conn = db_mod.connect(checked_client.app.state.db_path)
    try:
        conn.execute("DELETE FROM doctor_findings")
        conn.execute("UPDATE doctor_runs SET exit_code = 0")
        conn.commit()
    finally:
        conn.close()
    payload = checked_client.get("/api/doctor").json()
    assert payload["has_run"] is True
    assert payload["total"] == 0
    assert payload["sections"] == []
    assert payload["run"]["exit_code"] == 0


# ------------------------------------------------------------- the real payload
def test_findings_are_grouped_by_section_with_human_labels(checked_client):
    payload = checked_client.get("/api/doctor").json()
    assert payload["has_run"] is True
    sections = {item["section"]: item for item in payload["sections"]}
    assert set(sections) == {"unreadable", "duplicate", "missing_number"}
    assert payload["total"] == sum(item["count"] for item in payload["sections"])
    assert payload["run"]["summary"]["findings"] == payload["total"]
    assert sections["unreadable"]["label"] == "Unreadable or broken archives"
    assert sections["duplicate"]["label"] == "Duplicate issues"
    assert sections["missing_number"]["label"] == "Missing issue numbers"
    for section in payload["sections"]:
        assert section["count"] == len(section["findings"])
        for finding in section["findings"]:
            assert finding["summary"]
            assert finding["severity"] in {"error", "warning", "info"}
            assert isinstance(finding["files"], list)
            assert finding["files_total"] >= len(finding["files"])


def test_duplicate_findings_carry_the_verdict_and_the_files(checked_client):
    payload = checked_client.get("/api/doctor").json()
    section = _section(payload, "duplicate")
    assert section, "the doctor fixture library has duplicate issues"
    for finding in section["findings"]:
        assert finding["verdict"], finding["summary"]
        assert finding["suggestion"]
        assert finding["files_total"] >= 2
        assert len(finding["files"]) == finding["files_total"]
        assert [file["filename"] for file in finding["files"] if file.get("filename")]
        keepers = [file for file in finding["files"] if file.get("keeper")]
        assert len(keepers) == 1, "the panel marks exactly one suggested keeper"
        assert keepers[0]["path"] == finding["path"]


def test_unreadable_findings_carry_the_reason_and_what_to_do(checked_client):
    payload = checked_client.get("/api/doctor").json()
    section = _section(payload, "unreadable")
    assert section
    kinds = {finding["kind"] for finding in section["findings"]}
    assert kinds, "the fixture library has files that cannot be opened"
    for finding in section["findings"]:
        if finding["kind"] == "comicinfo_invalid":
            continue                       # metadata-only problem, nothing to install
        assert finding["fix"], f"{finding['kind']} has no fix hint for the owner"
        if finding["files_total"]:
            assert any(file.get("path") for file in finding["files"])


def test_missing_number_findings_say_what_is_missing(checked_client):
    payload = checked_client.get("/api/doctor").json()
    section = _section(payload, "missing_number")
    assert section
    for finding in section["findings"]:
        assert finding["series"]
        assert "missing" in finding["summary"].lower()
        if finding["kind"] == "ambiguous_numbering":
            assert finding["note"], "an ambiguous gap explains itself"


def test_the_run_says_when_it_ran_and_how_to_refresh(checked_client):
    payload = checked_client.get("/api/doctor").json()
    run = payload["run"]
    assert run["run_id"] and run["finished_at"] and run["started_at"]
    assert run["deep"] is False
    assert run["exit_code"] == 1               # the fixture library has findings
    assert run["comics_path"] == checked_client.app.state.comics_path


def test_a_deep_run_is_reported_as_deep_and_offers_the_deep_command(checked_client):
    """The panel tells the owner to repeat the check the way it was last run."""
    conn = db_mod.connect(checked_client.app.state.db_path)
    try:
        conn.execute("UPDATE doctor_runs SET deep = 1")
        conn.commit()
    finally:
        conn.close()
    payload = checked_client.get("/api/doctor").json()
    assert payload["run"]["deep"] is True
    assert payload["deep_command"] == "python -m longbox.doctor --deep"


def test_file_lists_are_capped_on_request(checked_client):
    payload = checked_client.get("/api/doctor", params={"limit": 1}).json()
    assert payload["sections"]
    for section in payload["sections"]:
        for finding in section["findings"]:
            assert len(finding["files"]) <= 1
            assert finding["files_total"] >= len(finding["files"])
    assert checked_client.get("/api/doctor", params={"limit": 0}).status_code == 422


def test_an_index_without_the_doctor_tables_is_answered_gracefully(tmp_path):
    """An index written before the doctor existed must not answer with a 500."""
    from fastapi.testclient import TestClient
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE IF EXISTS doctor_runs")
        conn.execute("DROP TABLE IF EXISTS doctor_findings")
        conn.commit()
    finally:
        conn.close()
    app = server.create_app(str(tmp_path), str(db_path), str(tmp_path / "thumbs"))
    with TestClient(app) as test_client:
        payload = test_client.get("/api/doctor").json()
    assert payload["has_run"] is False
    assert payload["command"] == "python -m longbox.doctor"


# ------------------------------------------------------------- read-only spirit
def test_showing_the_findings_touches_nothing(checked_client, checked_library):
    before = snapshot(checked_library["folder"])
    for params in ({}, {"limit": 3}):
        assert checked_client.get("/api/doctor", params=params).status_code == 200
    assert snapshot(checked_library["folder"]) == before
    conn = db_mod.connect(checked_client.app.state.db_path)
    try:
        comics = conn.execute("SELECT path, mtime, file_size, status FROM comics "
                              "ORDER BY path").fetchall()
        findings = conn.execute("SELECT COUNT(*) AS n FROM doctor_findings").fetchone()["n"]
    finally:
        conn.close()
    assert findings == checked_client.get("/api/doctor").json()["total"]
    assert comics and all(row["status"] in {"ok", "error", "missing"} for row in comics)


def test_no_ui_action_can_change_a_comic_through_this_route(checked_client):
    """GET only - and the same path answers 405 to every writing verb."""
    for verb in ("post", "put", "patch", "delete"):
        response = getattr(checked_client, verb)("/api/doctor")
        assert response.status_code == 405
    paths = checked_client.get("/openapi.json").json()["paths"]
    assert set(paths["/api/doctor"]) == {"get"}


def test_doctor_runs_through_the_cli_are_what_the_panel_shows(checked_client, checked_library):
    """The stored run and the CLI report are the same findings."""
    report = checked_library["report"]
    payload = checked_client.get("/api/doctor").json()
    assert payload["run"]["run_id"] == report["run_id"]
    stored = [finding["summary"] for section in payload["sections"]
              for finding in section["findings"]]
    assert stored == [finding["summary"] for finding in report["findings"]]


# ------------------------------------------------------------------- the panel
def test_the_ui_reads_the_doctor_endpoint_and_renders_it():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'api("/api/doctor")' in js
    assert "function doctorPanel" in js
    assert "parts.push(doctorPanel())" in js
    # the two empty states the brief asks for, in the owner's words
    assert "No check has run yet" in js
    assert "python -m longbox.doctor" in js
    assert "loadDoctor()" in js
    for word in ("Verdict", "suggestion", "keeper"):
        assert word in js
    # the panel shows the file it is talking about
    assert "file.filename" in js


def test_the_panel_cannot_call_anything_that_changes_files():
    """No fetch in the UI may hit a route that writes, and none may leave the PC."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function doctorFileList"):js.index("async function loadStats()")]
    assert "fetch(" not in body and "POST" not in body
    for verb in ("post(", "put(", "delete("):
        assert verb not in body
    assert "http://" not in js and "https://" not in js


def test_the_panel_is_styled_and_has_no_external_resources():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert ".doctor" in css and ".keeper" in css
    assert "@import" not in css and "http" not in css


def test_the_shell_is_untouched_and_the_panel_comes_from_app_js(client):
    """The panel renders inside the existing shell: no new page, no inline data."""
    html = client.get("/").text
    assert "/static/app.js" in html
    assert '<main id="view"' in html
    assert "/api/" not in html and "doctor" not in html.lower()
