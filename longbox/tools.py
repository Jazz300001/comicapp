"""Find a RAR reader on this PC at runtime and drive it read-only.

Nothing here ever extracts next to (or inside) the comics folder: when a tool has
to write something to disk it goes to the system temp directory and is deleted
again.  Archives are opened read-only; the comics folder is never modified.

Resolution order (documented in the README, asserted by the tests):

  1. ``unrar``  - WinRAR's ``UnRAR.exe`` counts
  2. ``unar``   - The Unarchiver
  3. ``bsdtar`` / ``tar.exe`` - Windows 10 1803+ ships ``C:\\Windows\\System32\\tar.exe``
     which *is* bsdtar and reads RAR.  GNU tar cannot read RAR, and on Linux
     ``/usr/bin/tar`` is usually GNU tar, so a ``tar`` found on PATH is only
     accepted when ``--version`` says "bsdtar"
  4. ``7z`` / ``7za`` / ``7zz`` - 7-Zip

A tool that is installed but not on PATH is still found: the usual Windows install
folders are probed too (see ``WINDOWS_LOCATIONS``), and ``LONGBOX_RAR_TOOL=<path>``
forces one specific executable (it is then trusted without the capability probe).

The first candidate that exists and passes a cheap capability probe is cached for
the run.  If that one turns out to fail on a real archive, the next candidate is
tried and the failing tool is skipped for the rest of the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass

#: shown when a RAR archive cannot be opened because no reader was found
RAR_TOOL_MISSING_MESSAGE = (
    "This is a RAR file and no RAR tool was found on this PC. Installing 7-Zip "
    "from 7-zip.org fixes it for all your .cbr files."
)

#: the same situation for a 7z container (rare, but the magic bytes are detected)
SEVENZ_MISSING_MESSAGE = (
    "This is a 7z file and no 7-Zip was found on this PC. Installing 7-Zip from "
    "7-zip.org fixes it."
)

#: the documented resolution order
TOOL_ORDER = ("unrar", "unar", "bsdtar", "7z")

#: executable names looked up on PATH, in order, per tool
PATH_NAMES = {
    "unrar": ("unrar", "unrar.exe", "UnRAR.exe"),
    "unar": ("unar", "unar.exe"),
    "bsdtar": ("bsdtar", "bsdtar.exe", "tar", "tar.exe"),
    "7z": ("7z", "7z.exe", "7za", "7za.exe", "7zz", "7zz.exe"),
}

#: ("env var holding the folder", ("path", "inside", "it")) - tried when the var is set
WINDOWS_LOCATIONS = {
    "unrar": (
        ("ProgramFiles", ("WinRAR", "UnRAR.exe")),
        ("ProgramFiles(x86)", ("WinRAR", "UnRAR.exe")),
    ),
    "bsdtar": (
        ("SystemRoot", ("System32", "tar.exe")),
        ("windir", ("System32", "tar.exe")),
    ),
    "7z": (
        ("ProgramFiles", ("7-Zip", "7z.exe")),
        ("ProgramFiles(x86)", ("7-Zip", "7z.exe")),
        ("ProgramW6432", ("7-Zip", "7z.exe")),
        ("LOCALAPPDATA", ("Programs", "7-Zip", "7z.exe")),
    ),
}

#: (extra argv, lowercase text the tool must print to prove what it is)
PROBE = {
    "unrar": ((), "unrar"),
    "unar": (("-v",), "unar"),
    "bsdtar": (("--version",), "bsdtar"),
    "7z": ((), "7-zip"),
}

#: how a user forces one specific executable
ENV_OVERRIDE = "LONGBOX_RAR_TOOL"

PROBE_TIMEOUT = 15
RUN_TIMEOUT = 600


class ToolError(Exception):
    """A tool was found but could not do what we asked."""


@dataclass(frozen=True)
class Tool:
    """One resolved executable that can list and read a RAR archive."""

    name: str
    path: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.name} ({self.path})"

    # -- running -----------------------------------------------------------
    def _run(self, args: list[str], timeout: int = RUN_TIMEOUT) -> bytes:
        cmd = [self.path] + [str(a) for a in args]
        kwargs: dict = {}
        if os.name == "nt":  # no console window popping up behind the browser
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout, check=False, **kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError(f"{self.name} could not be run: {exc}") from exc
        if proc.returncode != 0:
            detail = _text(proc.stderr).strip().splitlines()
            raise ToolError(f"{self.name} exited {proc.returncode}"
                            + (f": {detail[-1]}" if detail else ""))
        return proc.stdout

    # -- listing -----------------------------------------------------------
    def list_names(self, archive: str) -> list[str]:
        """Every member path in the archive, as stored (never absolute)."""
        if self.name == "unrar":
            return _lines(_text(self._run(["lb", "-p-", "-inul", "--", archive])))
        if self.name == "bsdtar":
            return _lines(_text(self._run(["-tf", archive])))
        if self.name == "7z":
            return _names_from_7z(_text(self._run(["l", "-slt", archive])), archive)
        if self.name == "unar":
            temp = tempfile.mkdtemp(prefix="longbox-rar-")
            try:
                self.extract_all(archive, temp)
                return _relative_files(temp)
            finally:
                _rmtree(temp)
        raise ToolError(f"unknown tool: {self.name}")  # pragma: no cover

    # -- reading -----------------------------------------------------------
    def read_member(self, archive: str, member: str) -> bytes:
        """One member's bytes, streamed to stdout where the tool supports it."""
        if self.name == "unrar":
            return self._run(["p", "-inul", "-p-", "--", archive, member])
        if self.name == "bsdtar":
            return self._run(["-xOf", archive, member])
        if self.name == "7z":
            return self._run(["x", "-y", "-so", "-bso0", "-bsp0", archive, member])
        if self.name == "unar":
            return self._run(["-q", "-o", "-", archive, member])
        raise ToolError(f"unknown tool: {self.name}")  # pragma: no cover

    # -- writing to a temp folder (the fallback when stdout is unusable) ---
    def extract_all(self, archive: str, dest_dir: str) -> None:
        if self.name == "unrar":
            self._run(["x", "-inul", "-p-", "-o+", "--", archive, _with_sep(dest_dir)])
        elif self.name == "unar":
            self._run(["-q", "-f", "-o", dest_dir, archive])
        elif self.name == "bsdtar":
            self._run(["-xf", archive, "-C", dest_dir])
        elif self.name == "7z":
            self._run(["x", "-y", "-bso0", "-bsp0", f"-o{dest_dir}", archive])
        else:  # pragma: no cover
            raise ToolError(f"unknown tool: {self.name}")

    def extract_member(self, archive: str, member: str, dest_dir: str) -> None:
        if self.name == "unrar":
            self._run(["x", "-inul", "-p-", "-o+", "--", archive, member,
                       _with_sep(dest_dir)])
        elif self.name == "unar":
            self._run(["-q", "-f", "-o", dest_dir, archive, member])
        elif self.name == "bsdtar":
            self._run(["-xf", archive, "-C", dest_dir, member])
        elif self.name == "7z":
            self._run(["x", "-y", "-bso0", "-bsp0", f"-o{dest_dir}", archive, member])
        else:  # pragma: no cover
            raise ToolError(f"unknown tool: {self.name}")


# --------------------------------------------------------------------- helpers
def _with_sep(path: str) -> str:
    return path if path.endswith(os.sep) else path + os.sep


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _text(data: bytes) -> str:
    """Decode tool output: utf-8 when it is valid, else the local ANSI codepage.

    latin-1 is the last resort because it always round-trips, so a member name we
    read out of a listing can be handed straight back to the same tool.
    """
    for encoding in ("utf-8", "mbcs" if os.name == "nt" else "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover


def _lines(text: str) -> list[str]:
    return [line.strip().replace("\\", "/")
            for line in text.splitlines() if line.strip()]


def _names_from_7z(text: str, archive: str) -> list[str]:
    """`7z l -slt` prints one block per member, each starting with 'Path = '."""
    names = [line.split("=", 1)[1].strip() for line in text.splitlines()
             if line.startswith("Path = ")]
    # the first block describes the archive itself
    if names and os.path.normcase(os.path.abspath(names[0])) == \
            os.path.normcase(os.path.abspath(archive)):
        names.pop(0)
    return [name.replace("\\", "/") for name in names]


def _relative_files(root: str) -> list[str]:
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found, key=lambda name: name.casefold())


def _exists(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def _name_for_path(path: str) -> str | None:
    """Guess which family an explicitly configured executable belongs to."""
    base = os.path.basename(path).casefold()
    if "unrar" in base or base.startswith("rar"):
        return "unrar"
    if "unar" in base:
        return "unar"
    if base.startswith("7z"):
        return "7z"
    if "tar" in base:
        return "bsdtar"
    return None


def _candidate_paths(name: str) -> list[str]:
    paths: list[str] = []
    for executable in PATH_NAMES.get(name, ()):
        found = shutil.which(executable)
        if found:
            paths.append(found)
    for variable, parts in WINDOWS_LOCATIONS.get(name, ()):
        root = os.environ.get(variable)
        if not root:
            continue
        candidate = os.path.join(root, *parts)
        if os.name != "nt" or _exists(candidate):  # on POSIX only keep real files
            if _exists(candidate):
                paths.append(candidate)
    seen: set[str] = set()
    unique = []
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if path and key not in seen and _exists(path):
            seen.add(key)
            unique.append(path)
    return unique


def _probe(tool: Tool) -> bool:
    """Does this executable really do the job?  (GNU tar is rejected here.)"""
    args, needle = PROBE.get(tool.name, ((), ""))
    try:
        output = tool._run(list(args), timeout=PROBE_TIMEOUT)
    except ToolError:
        return False
    return needle in _text(output).casefold()


# ------------------------------------------------------------------- resolution
_lock = threading.RLock()
_cache: dict = {"candidates": None, "resolved": None, "broken": []}


def reset_cache() -> None:
    """Forget everything resolved so far (tests, and after installing a tool)."""
    with _lock:
        _cache.update({"candidates": None, "resolved": None, "broken": []})


def discover() -> list[Tool]:
    """Every tool on this PC that is in the documented order and passes its probe."""
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    if override:
        if _exists(override):
            return [Tool(_name_for_path(override) or "unrar", override)]
        return []
    found: list[Tool] = []
    for name in TOOL_ORDER:
        for path in _candidate_paths(name):
            tool = Tool(name, path)
            if _probe(tool):
                found.append(tool)
                break
    return found


def candidates() -> list[Tool]:
    """`discover()`, cached for the run."""
    with _lock:
        if _cache["candidates"] is None:
            _cache["candidates"] = discover()
        return list(_cache["candidates"])


def resolve() -> Tool | None:
    """The first candidate that works, cached for the run."""
    with _lock:
        if _cache["resolved"] is not None:
            return _cache["resolved"]
        for tool in candidates():
            if tool.path in _cache["broken"]:
                continue
            _cache["resolved"] = tool
            return tool
        return None


def remaining_after(tool: Tool) -> list[Tool]:
    """`tool` first, then the rest of the chain, skipping tools known to fail."""
    with _lock:
        tools = candidates()
        broken = list(_cache["broken"])
    start = 0
    for index, candidate in enumerate(tools):
        if candidate.path == tool.path:
            start = index
            break
    hits = [candidate for candidate in tools[start:] if candidate.path not in broken]
    return hits or [tool]


def report_success(tool: Tool) -> None:
    with _lock:
        _cache["resolved"] = tool
        _cache["broken"] = [path for path in _cache["broken"] if path != tool.path]


def report_failure(tool: Tool) -> None:
    with _lock:
        if tool.path not in _cache["broken"]:
            _cache["broken"].append(tool.path)
        if _cache["resolved"] is not None and _cache["resolved"].path == tool.path:
            _cache["resolved"] = None


def tool_summary() -> str:
    """One short line for the scan summary: which reader is in use, if any."""
    tool = resolve()
    return str(tool) if tool else "none found"
