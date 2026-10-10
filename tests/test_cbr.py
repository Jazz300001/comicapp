"""Container detection by magic bytes, and the RAR tool fallback chain.

The stub tools in here are real executables (small python scripts) put on a private
PATH, so the resolution order, the "skip a tool that fails" rule and the temp-folder
fallback are exercised end to end rather than asserted against a mock.
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from string import Template

import pytest

from longbox import archive as archive_mod
from longbox import db as db_mod
from longbox import scanner
from longbox import tools as tools_mod

GNU_TAR_BANNER = "tar (GNU tar) 1.35"
windows_skip = pytest.mark.skipif(os.name == "nt",
                                 reason="the stub tools are POSIX shell/python scripts")


def jpeg_bytes(size=(40, 60), colour=(10, 120, 200)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, colour)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


PAGE_BYTES = jpeg_bytes()


def write_zip(path: Path, names=("page1.jpg", "page2.jpg", "page3.jpg")) -> Path:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, PAGE_BYTES)
    return path


# --------------------------------------------------------------------- stubs
STUB = Template('''#!$python
import os, sys

KIND = "$kind"
LOG = "$log"
NAMES = $names
PAGE = bytes.fromhex("$page_hex")
PROBE_TEXT = "$probe"
PROBE_EXIT = $probe_exit
PROBE_STDERR = $probe_stderr
CHATTER = $chatter
FAIL_LIST = $fail_list
FAIL_EXTRACT = $fail_extract

args = sys.argv[1:]
with open(LOG, "a") as handle:
    handle.write(KIND + " " + " ".join(args) + "\\n")


def is_probe():
    if KIND in ("unrar", "7z"):
        return not args
    if KIND == "bsdtar":
        return args[:1] == ["--version"]
    return args[:1] == ["-v"]


def is_list():
    if KIND == "unrar":
        return args[:1] == ["lb"]
    if KIND == "bsdtar":
        return args[:1] == ["-tf"]
    if KIND == "7z":
        return args[:1] == ["l"]
    return False


def flag_value(flag):
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def joined_flag(prefix):
    for arg in args:
        if arg.startswith(prefix) and arg != prefix:
            return arg[len(prefix):]
    return None


out_dir = None
if KIND == "unrar" and args[:1] == ["x"]:
    out_dir = args[-1]
elif KIND == "unar":
    out_dir = flag_value("-o")
elif KIND == "bsdtar" and args[:1] == ["-xf"]:
    out_dir = flag_value("-C")
elif KIND == "7z" and args[:1] == ["x"] and "-so" not in args:
    out_dir = joined_flag("-o")

if is_probe():
    if PROBE_STDERR:
        sys.stderr.write(PROBE_TEXT + "\\n")
    else:
        sys.stdout.write(PROBE_TEXT + "\\n")
    sys.exit(PROBE_EXIT)

if is_list():
    if FAIL_LIST:
        sys.stderr.write("stub " + KIND + " cannot open it\\n")
        sys.exit(2)
    if KIND == "7z":
        sys.stdout.write("Path = /tmp/stub.rar\\nType = Rar\\n")
        for name in NAMES:
            sys.stdout.write("\\n----------\\nPath = " + name + "\\n")
    else:
        for name in NAMES:
            sys.stdout.write(name + "\\n")
    sys.exit(0)

if KIND == "unar" and out_dir == "-":
    sys.stdout.buffer.write(PAGE)
    sys.exit(0)

if out_dir:
    if FAIL_EXTRACT:
        sys.stderr.write("stub " + KIND + " cannot extract\\n")
        sys.exit(2)
    os.makedirs(out_dir, exist_ok=True)
    for name in NAMES:
        target = os.path.join(out_dir, name.replace("/", os.sep))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(PAGE)
    sys.exit(0)

if KIND == "unrar" and args[:1] == ["p"]:
    sys.stdout.buffer.write(PAGE)
    sys.exit(0)

if KIND == "bsdtar" and args[:1] == ["-xOf"]:
    sys.stdout.buffer.write(PAGE)
    sys.exit(0)

if KIND == "7z" and args[:1] == ["x"] and "-so" in args:
    if CHATTER:
        sys.stdout.write("Extracting  " + NAMES[0] + "\\nEverything is Ok\\n")
    else:
        sys.stdout.buffer.write(PAGE)
    sys.exit(0)

sys.exit(3)
''')

PROBE_TEXT = {
    "unrar": "UNRAR 6.24 freeware  Copyright (c) 1993-2023 Alexander Roshal",
    "bsdtar": "bsdtar 3.7.0 - libarchive 3.7.0 zlib/1.2.11",
    "7z": "7-Zip 23.01 (x64) : Copyright (c) 1999-2023 Igor Pavlov : 2023-06-20",
    "unar": "unar 1.10.7 (The Unarchiver)",
}


class Stubs:
    """A private PATH with fake tools in it, and a log of every call they saw."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "bin"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "tool-calls.log"
        monkeypatch.setenv("PATH", str(self.dir))
        monkeypatch.delenv(tools_mod.ENV_OVERRIDE, raising=False)
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                         "LOCALAPPDATA", "SystemRoot", "windir"):
            monkeypatch.delenv(variable, raising=False)
        tools_mod.reset_cache()

    def add(self, kind: str, name: str | None = None, *, probe: str | None = None,
            names=("page1.jpg", "page2.jpg", "page3.jpg"), page: bytes = PAGE_BYTES,
            chatter: bool = False, fail_list: bool = False,
            fail_extract: bool = False, probe_exit: int = 0, probe_stderr: bool = False,
            directory: Path | None = None) -> str:
        target = (directory or self.dir) / (name or kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(STUB.substitute(
            python=sys.executable, kind=kind, log=str(self.log),
            names=repr(list(names)), page_hex=page.hex(),
            probe=probe if probe is not None else PROBE_TEXT[kind],
            probe_exit=probe_exit, probe_stderr=probe_stderr,
            chatter=chatter, fail_list=fail_list, fail_extract=fail_extract))
        target.chmod(0o755)
        # discovery is cached for the run, so a tool that has just appeared (or just
        # been replaced) has to invalidate it - otherwise the next call answers from
        # a scan that predates the change
        tools_mod.reset_cache()
        return str(target)

    def clear_calls(self) -> None:
        """Forget the calls so far, so the log shows only what happens next.

        Every invocation is logged, capability probes included; a test that is
        about the order tools are tried *on an archive* clears the log after the
        deliberate `candidates()` call that triggers discovery.
        """
        self.log.write_text("")

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.strip()]

    def order_of_kinds(self) -> list[str]:
        seen: list[str] = []
        for line in self.calls():
            kind = line.split(" ", 1)[0]
            if kind not in seen:
                seen.append(kind)
        return seen


@pytest.fixture()
def stubs(tmp_path, monkeypatch) -> Stubs:
    return Stubs(tmp_path, monkeypatch)


@pytest.fixture()
def rar_file(tmp_path) -> Path:
    """A file that really starts with the RAR4 signature (no RAR creator here)."""
    path = tmp_path / "comics" / "Superman Unlimited 1 (2025).cbr"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive_mod.MAGIC_SIGNATURES[1][0] + bytes(64))
    return path


# ----------------------------------------------------------- container bytes
def test_container_is_detected_by_magic_bytes_never_by_extension(tmp_path):
    cases = [
        ("Batman 1.cbr", b"PK\x03\x04" + bytes(20), "zip", 1),
        ("Batman 1.cbz", archive_mod.MAGIC_SIGNATURES[1][0] + bytes(20), "rar4", 1),
        ("Batman 1.cbr", archive_mod.MAGIC_SIGNATURES[1][0] + bytes(20), "rar4", 0),
        ("Batman 1.cbr", b"Rar!\x1a\x07\x01\x00" + bytes(20), "rar5", 0),
        ("Batman 1.cbz", b"Rar!\x1a\x07\x01\x00" + bytes(20), "rar5", 1),
        ("Batman 1.cbr", b"7z\xbc\xaf\x27\x1c" + bytes(20), "7z", 1),
        ("Batman 1.cbz", b"this is not an archive at all\n", "unknown", 0),
        ("Batman 1.cbz", b"PK\x05\x06" + bytes(18), "zip", 0),  # empty zip
    ]
    for name, payload, container, mismatch in cases:
        path = tmp_path / name
        path.write_bytes(payload)
        assert archive_mod.detect_container(str(path)) == container, name
        assert archive_mod.container_mismatch(str(path), container) == mismatch, name


def test_rar5_signature_is_rarp5_not_rar4(tmp_path):
    path = tmp_path / "New Run 1.cbr"
    path.write_bytes(archive_mod.MAGIC_SIGNATURES[0][0] + bytes(32))
    assert archive_mod.detect_container(str(path)) == "rar5"
    with pytest.raises(archive_mod.ArchiveError) as caught:
        with archive_mod.open_archive(str(path)):
            pass
    assert caught.value.container == "rar5"


# --------------------------------------------------------------- the big win
def test_zip_named_cbr_opens_with_no_external_tool(tmp_path, stubs):
    """165 of the owner's unreadable files may be exactly this: zips named .cbr."""
    comics = tmp_path / "comics"
    path = write_zip(comics / "Civil War 1 (2006).cbr",
                     names=("page1.jpg", "page2.jpg", "page10.jpg"))

    assert tools_mod.resolve() is None          # nothing on this PATH at all
    with archive_mod.open_archive(str(path)) as handle:
        assert handle.container == "zip"
        assert handle.mismatch == 1             # named .cbr, really a zip
        assert handle.pages() == ["page1.jpg", "page2.jpg", "page10.jpg"]  # natural
        assert handle.read("page2.jpg") == PAGE_BYTES
        thumb = archive_mod.make_thumbnail(handle.read("page1.jpg"))
    assert thumb[:2] == b"\xff\xd8"
    assert stubs.calls() == []                  # no external tool was run

    data = scanner.read_comic(str(path))
    assert data["status"] == "ok"
    assert data["archive_container"] == "zip"
    assert data["container_mismatch"] == 1
    assert data["page_count"] == 3
    assert data["cover_page"] == 1


def test_a_real_zip_cbz_is_not_flagged_as_misnamed(tmp_path):
    path = write_zip(tmp_path / "X-Men 1 (2025).cbz")
    data = scanner.read_comic(str(path))
    assert data["archive_container"] == "zip" and data["container_mismatch"] == 0


# ------------------------------------------------------------ fallback chain
@windows_skip
def test_tool_chain_tries_the_documented_order_and_skips_what_fails(tmp_path, stubs, rar_file):
    stubs.add("unrar", fail_list=True)      # 1st: exists, cannot list
    stubs.add("unar", fail_extract=True)    # 2nd: exists, cannot unpack
    stubs.add("bsdtar")                     # 3rd: works
    stubs.add("7z", chatter=True)           # 4th: never reached

    assert [tool.name for tool in tools_mod.candidates()] == ["unrar", "unar", "bsdtar", "7z"]
    stubs.clear_calls()          # discovery has probed all four; log only the work now

    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "bsdtar"                 # first one that works
        assert handle.pages() == ["page1.jpg", "page2.jpg", "page3.jpg"]
        assert handle.read("page2.jpg") == PAGE_BYTES
        assert tools_mod.resolve().name == "bsdtar"         # cached for the run

    assert stubs.order_of_kinds() == ["unrar", "unar", "bsdtar"]
    assert any(line.startswith("bsdtar -xOf") for line in stubs.calls())


@windows_skip
def test_the_first_working_tool_is_the_one_used(tmp_path, stubs, rar_file):
    for kind in ("unrar", "unar", "bsdtar", "7z"):
        stubs.add(kind)
    assert [tool.name for tool in tools_mod.candidates()] == ["unrar", "unar", "bsdtar", "7z"]
    stubs.clear_calls()
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "unrar"
        assert handle.read("page3.jpg") == PAGE_BYTES
    assert stubs.order_of_kinds() == ["unrar"]
    assert any(line.startswith("unrar p") for line in stubs.calls())


@windows_skip
def test_gnu_tar_is_rejected_and_bsdtar_is_accepted(tmp_path, stubs, rar_file):
    stubs.add("bsdtar", name="tar", probe=GNU_TAR_BANNER)   # /usr/bin/tar is usually GNU
    assert tools_mod.candidates() == []
    with pytest.raises(archive_mod.ArchiveError) as caught:
        with archive_mod.open_archive(str(rar_file)):
            pass
    assert caught.value.kind == "rar_tool_missing"
    assert str(caught.value) == tools_mod.RAR_TOOL_MISSING_MESSAGE

    stubs.add("bsdtar")                                     # ...but bsdtar reads RAR
    assert [tool.name for tool in tools_mod.candidates()] == ["bsdtar"]


@windows_skip
def test_a_probe_that_cannot_tell_does_not_drop_the_tool(tmp_path, stubs, rar_file):
    """The probe is a guess; opening a real archive is the only proof.

    Real UnRAR builds answer a bare ``unrar`` with a usage screen on *stderr* and a
    non-zero exit, and some wrappers have no ``--version`` at all.  Reading that as
    "this PC has no RAR tool" is exactly how the owner is told nothing can read his
    .cbr files while UnRAR.exe sits in ``C:\\Program Files\\WinRAR``.  So: an
    inconclusive probe keeps the tool in the chain, and the real archive decides.
    """
    stubs.add("unrar", probe="Usage: <command> [options]  (no version flag here)",
              probe_exit=1, probe_stderr=True)          # says nothing recognisable
    assert [tool.name for tool in tools_mod.candidates()] == ["unrar"]

    stubs.clear_calls()
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "unrar"              # tried, and it worked
        assert handle.pages() == ["page1.jpg", "page2.jpg", "page3.jpg"]
        assert handle.read("page1.jpg") == PAGE_BYTES


@windows_skip
def test_a_tool_that_fails_on_a_real_archive_is_skipped_for_the_run(tmp_path, stubs, rar_file):
    """Unclassifiable -> still tried; demonstrably broken -> retired for the run."""
    stubs.add("unrar", probe="Usage: <command> [options]  (no version flag here)",
              probe_exit=1, probe_stderr=True, fail_list=True)   # cannot open it
    stubs.add("bsdtar")                                          # this one can
    assert [tool.name for tool in tools_mod.candidates()] == ["unrar", "bsdtar"]

    stubs.clear_calls()
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "bsdtar"             # the chain moved on
        assert handle.read("page2.jpg") == PAGE_BYTES
    assert stubs.order_of_kinds() == ["unrar", "bsdtar"]
    assert tools_mod.resolve().name == "bsdtar"

    stubs.clear_calls()                                 # second archive of the run
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "bsdtar"
    assert stubs.order_of_kinds() == ["bsdtar"]         # the broken one was not retried


@windows_skip
def test_a_tool_outside_path_is_still_found(tmp_path, stubs, monkeypatch, rar_file):
    """Windows' own C:\\Windows\\System32\\tar.exe and 7-Zip's install folder."""
    program_files = tmp_path / "Program Files"
    stubs.add("7z", name="7z.exe", directory=program_files / "7-Zip")
    assert stubs.calls() == []                      # nothing on PATH
    monkeypatch.setenv("ProgramFiles", str(program_files))

    assert [tool.name for tool in tools_mod.candidates()] == ["7z"]
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.tool.name == "7z"
        assert handle.pages() == ["page1.jpg", "page2.jpg", "page3.jpg"]
        assert handle.read("page1.jpg") == PAGE_BYTES
    assert any(line.startswith("7z l -slt") for line in stubs.calls())


@windows_skip
def test_a_configured_tool_path_wins(tmp_path, stubs, monkeypatch, rar_file):
    configured = stubs.add("unrar", name="my-unrar-copy")
    stubs.add("7z")
    monkeypatch.setenv(tools_mod.ENV_OVERRIDE, configured)
    tools_mod.reset_cache()
    assert [tool.name for tool in tools_mod.candidates()] == ["unrar"]
    with archive_mod.open_archive(str(rar_file)) as handle:
        assert handle.pages() == ["page1.jpg", "page2.jpg", "page3.jpg"]


@windows_skip
def test_stdout_chatter_falls_back_to_a_temp_folder(tmp_path, stubs, rar_file):
    stubs.add("7z", chatter=True)     # 7z talking on stdout instead of streaming bytes
    before = set(Path(tempfile.gettempdir()).glob("longbox-rar-*"))
    with archive_mod.open_archive(str(rar_file)) as handle:
        page = handle.read("page1.jpg")
    assert page == PAGE_BYTES                     # the real page, not the chatter
    assert any(" -o" in line or line.endswith("x") for line in
               [call for call in stubs.calls() if call.startswith("7z x")])
    after = set(Path(tempfile.gettempdir()).glob("longbox-rar-*"))
    assert after == before                        # nothing left behind
    assert not any(rar_file.parent.glob("longbox*"))   # never next to the comics


# ---------------------------------------------------------- no tool on the PC
def test_no_tools_at_all_keeps_the_rows_and_gives_one_actionable_reason(tmp_path, stubs):
    comics = tmp_path / "comics"
    comics.mkdir(parents=True, exist_ok=True)
    good = write_zip(comics / "X-Men 1 (2025).cbz", names=("page1.jpg", "page2.jpg"))
    rar = comics / "House of M Tie-In 2 (2005).cbr"
    rar.write_bytes(archive_mod.MAGIC_SIGNATURES[1][0] + bytes(128))
    before = sorted(p.name for p in comics.rglob("*"))

    summary = scanner.scan_library(str(comics), str(tmp_path / "longbox.db"))
    assert summary["found"] == 2 and summary["errors"] == 1
    assert summary["indexed"] == 1
    assert summary["error_kinds"] == {"rar_tool_missing": 1}
    assert summary["reader"] == "none found"

    conn = db_mod.open_db(str(tmp_path / "longbox.db"))
    try:
        rows = {row["filename"]: row for row in conn.execute("SELECT * FROM comics")}
        row = rows[rar.name]
        # the row is kept and is still useful: filename parsed, container recorded
        assert row["status"] == "error"
        assert row["archive_container"] == "rar4" and row["container_mismatch"] == 0
        assert row["series"] == "House of M Tie-In" and row["issue_number"] == "2"
        assert row["error_kind"] == "rar_tool_missing"
        assert row["error_message"] == tools_mod.RAR_TOOL_MISSING_MESSAGE
        assert "7-Zip" in row["error_message"] and len(row["error_message"]) < 200
        assert rows[good.name]["status"] == "ok"
        assert rows[good.name]["page_count"] == 2
    finally:
        conn.close()

    assert sorted(p.name for p in comics.rglob("*")) == before   # read-only folder
    assert stubs.calls() == []


def test_an_unreadable_file_is_never_counted_as_indexed_twice(tmp_path, stubs):
    comics = tmp_path / "comics"
    comics.mkdir(parents=True)
    (comics / "FF by Hickman 1 (2019).cbr").write_bytes(
        archive_mod.MAGIC_SIGNATURES[1][0] + bytes(32))
    db = str(tmp_path / "longbox.db")
    first = scanner.scan_library(str(comics), db)
    second = scanner.scan_library(str(comics), db)
    assert first["indexed"] == 0 and first["errors"] == 1
    assert second["indexed"] == 0 and second["updated"] == 0 and second["errors"] == 1
    assert second["total_in_db"] == 1


# ------------------------------------------------------------------ migration
#: a realistic v1 database: every column the first release shipped, i.e. today's
#: column list minus the three that were added later (db.ADDED_COLUMNS).  Built
#: from the real list so it cannot drift out of date silently.
_ADDED = {name for name, _ in db_mod.ADDED_COLUMNS}
_V1_TYPES = {
    "path": "TEXT NOT NULL UNIQUE",
    "issue_sort": "REAL",
    "year": "INTEGER",
    "month": "INTEGER",
    "day": "INTEGER",
    "page_count": "INTEGER",
    "comicinfo_page_count": "INTEGER",
    "cover_page": "INTEGER",
    "comicinfo_present": "INTEGER NOT NULL DEFAULT 0",
    "file_size": "INTEGER",
    "mtime": "REAL",
    "status": "TEXT NOT NULL DEFAULT 'ok'",
}
OLD_SCHEMA = (
    "CREATE TABLE comics (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    + ",\n".join(f"    {name} {_V1_TYPES.get(name, 'TEXT')}"
                 for name in db_mod.COMIC_COLUMNS if name not in _ADDED)
    + "\n);\n"
    "CREATE INDEX idx_comics_series_key ON comics(series_key);\n"
    "CREATE INDEX idx_comics_status ON comics(status);\n"
    "CREATE INDEX idx_comics_year ON comics(year);\n"
    "CREATE INDEX idx_comics_comicvine ON comics(comicvine_issue_id);\n"
    'CREATE TABLE progress (\n'
    "    comic_id INTEGER PRIMARY KEY REFERENCES comics(id) ON DELETE CASCADE,\n"
    "    last_page INTEGER NOT NULL DEFAULT 0,\n"
    '    "read" INTEGER NOT NULL DEFAULT 0,\n'
    "    updated_at TEXT\n"
    ");\n"
)


def test_an_old_database_is_migrated_in_place(tmp_path):
    """The owner already has a library database; new columns must be added to it."""
    db_file = tmp_path / "longbox.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO comics (path, filename, series, archive_type, status, "
                 "error_message) VALUES ('C:/comics/a.cbr', 'a.cbr', 'X-Men', 'cbr', "
                 "'error', 'unrar not installed - CBR not readable')")
    conn.execute("INSERT INTO progress (comic_id, last_page) VALUES (1, 7)")
    conn.commit()
    conn.close()

    connection = db_mod.open_db(str(db_file))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(comics)")}
        assert {"archive_container", "container_mismatch", "error_kind"} <= columns
        row = connection.execute("SELECT * FROM comics").fetchone()
        assert row["error_kind"] == "rar_tool_missing"      # backfilled, not guessed
        assert row["series"] == "X-Men"
        assert connection.execute("SELECT * FROM progress").fetchone()["last_page"] == 7
    finally:
        connection.close()
