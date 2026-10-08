"""Shared pytest fixtures: synthetic comics + a scanned database.

The fixtures are built once per test session by scripts/make_fixtures.py (the
same script the owner/team can run by hand), scanned into a throwaway SQLite
file, and then copied per test so progress writes never leak between tests.
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from longbox import db as db_mod  # noqa: E402
from longbox import scanner  # noqa: E402
from longbox import server  # noqa: E402


def load_fixture_builder():
    path = ROOT / "scripts" / "make_fixtures.py"
    spec = importlib.util.spec_from_file_location("longbox_make_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_db(source: str, destination: Path) -> str:
    """Proper copy of a WAL database (shutil on the .db file alone can lose data)."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(destination)


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("comics")
    load_fixture_builder().build(folder, ROOT / "samples")
    return folder


@pytest.fixture(scope="session")
def scan_summary(fixtures_dir, tmp_path_factory) -> dict:
    db_path = tmp_path_factory.mktemp("data") / "longbox.db"
    summary = scanner.scan_library(str(fixtures_dir), str(db_path))
    summary["db_path"] = str(db_path)
    return summary


@pytest.fixture(scope="session")
def db_path(scan_summary) -> str:
    return scan_summary["db_path"]


@pytest.fixture()
def conn(tmp_path, db_path):
    connection = db_mod.connect(copy_db(db_path, tmp_path / "longbox.db"))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client(tmp_path, db_path, fixtures_dir):
    from fastapi.testclient import TestClient

    fresh_db = copy_db(db_path, tmp_path / "longbox.db")
    app = server.create_app(str(fixtures_dir), fresh_db, str(tmp_path / "thumbs"))
    with TestClient(app) as test_client:
        test_client.thumb_dir = str(tmp_path / "thumbs")
        yield test_client


@pytest.fixture()
def result(fixtures_dir, tmp_path):
    """Helper: build a private copy of the fixtures and scan it on demand."""
    class Result:
        def __init__(self, folder: Path, tmp_path: Path):
            self.folder = folder
            self.tmp_path = tmp_path
            self.db = str(tmp_path / "longbox.db")

        def scan(self) -> dict:
            return scanner.scan_library(str(self.folder), self.db)

        def query(self, sql: str, params=()):
            connection = db_mod.connect(self.db)
            connection.row_factory = sqlite3.Row
            try:
                return connection.execute(sql, params).fetchall()
            finally:
                connection.close()

        def row(self, filename: str):
            rows = self.query("SELECT * FROM comics WHERE filename = ?", (filename,))
            return rows[0] if rows else None

    folder = tmp_path / "comics"
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixtures_dir, folder)
    return Result(folder, tmp_path)
