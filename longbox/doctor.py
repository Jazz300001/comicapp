"""Library doctor - what is actually wrong with the collection.

CLI::

    python -m longbox.doctor --path "C:\\Users\\jasro\\Desktop\\comics" --db data\\longbox.db

Three checks, in the order the owner should care about them:

1. **Unreadable / broken archives** - every ``status='error'`` row in the index,
   grouped by reason (the same grouped shape the problems panel uses), plus an
   opt-in ``--deep`` pass that re-opens each archive and reads *every* page
   member, which catches truncation and corrupt members a listing cannot see.
2. **Duplicate issues** - more than one file for the same ``series_key`` +
   annual flag + ``issue_number``, with the evidence to tell an identical copy
   apart from a lower-quality one, and a *suggested* keeper.
3. **Missing issue numbers in a run** - per series, the numbers present and the
   gaps, with the noise filtered out (annuals, one-shots, non-numeric numbers,
   series that only have one or two issues) and with reading-order filenames
   (``008- Civil War 1.cbr``) grouped under the real series.

The comics folder is **read only**: this module never creates, writes, moves,
renames or deletes anything inside it.  A ``--deep`` run extracts nothing next to
the comics - any tool that needs a file on disk gets a system temp folder which is
removed again (``longbox.archive`` owns that).

Findings are persisted into the ``doctor_findings`` / ``doctor_runs`` tables of the
Longbox database so a later web panel can render them without re-running the
checks; ``--no-save`` keeps the run out of the database entirely.

Exit codes::

    0   no findings - nothing to fix
    1   findings exist (see the report)
    2   the run could not start (no comics folder, no index, bad arguments)
    130 interrupted with Ctrl-C (the findings found so far were saved)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from . import archive as archive_mod
from . import db as db_mod
from . import parser as parser_mod

#: bumped when the JSON/report shape changes, so a consumer can tell
SCHEMA_VERSION = 1

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

#: files listed per group in the text report (the JSON always carries them all
#: up to ``--examples``; the count is always the true count)
DEFAULT_EXAMPLES = 5

#: how many series lines the text report prints
MAX_SERIES_LINES = 60

#: "008- Civil War 1.cbr": leading position in a reading order, not a series name
_ORDER_PREFIX_RE = re.compile(r"^\s*(\d{1,4})\s*[-–—.]\s+")

_INTEGER_ISSUE_RE = re.compile(r"^\d+$")

#: a gap this wide is more likely two volumes of one title than a missing issue
WIDE_GAP = 20

#: advice shown for the reasons this module can produce itself; the scanner's
#: wording (server.FIX_HINTS) is reused for everything it shares, so the owner
#: reads the same sentence in the report and in the web panel.
_DOCTOR_HINTS = {
    "deep_corrupt_member": ("A page inside the archive cannot be read (the file is truncated "
                            "or damaged) - re-download this issue."),
    "deep_empty_page": "A page inside the archive is empty (0 bytes) - re-download this issue.",
    "deep_page_not_an_image": ("A page inside the archive is text, not an image - the file is "
                               "damaged or was never a comic."),
    "deep_page_count_mismatch": ("ComicInfo.xml lists a different number of pages than the "
                                 "archive holds - some pages are missing or extra."),
    "comicinfo_invalid": ("The embedded ComicInfo.xml could not be parsed (the pages still read "
                          "fine)."),
    "file_missing": "The file is no longer in the comics folder - the index is out of date.",
    "now_readable": ("This file failed at scan time but reads fine now - run "
                     "`python -m longbox.scan` to index it."),
    "identical_duplicate": "Two copies of the same file - one of them is spare.",
    "quality_differs": ("One copy has bigger pages than the other - keep the better scan, the "
                        "suggestion below is only a suggestion."),
    "gap": "An issue number in this run is not in your library.",
    "ambiguous_numbering": ("The numbers in this series do not form one clean run - the gap "
                            "below is not certain."),
    "unknown": "The scanner did not record a reason.",
}


def _hints() -> dict:
    """Scanner wording first, ours on top. FastAPI missing must not break the CLI."""
    hints = {}
    try:  # pragma: no cover - depends on the install, not on logic
        from .server import FIX_HINTS  # noqa: WPS433 (deliberate, optional import)

        hints.update(FIX_HINTS)
    except Exception:
        pass
    hints.update(_DOCTOR_HINTS)
    return hints


ADVICE = _hints()


# ---------------------------------------------------------------- helpers


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_id_for(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _json(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def human_size(size) -> str:
    if size is None:
        return "unknown size"
    size = int(size)
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def canonical_series(series: str | None, series_key: str | None):
    """``(display_name, key, order_number)`` with a leading reading-order number removed.

    The owner's reading orders are folders of files named ``008- Civil War 1.cbr``:
    the leading ``008`` is the position in the order, not part of the series and not
    the issue number.  The filename parser keeps it inside the series name, so
    grouping on the raw value would invent a series called "008- Civil War" with a
    one-issue run instead of putting the file into *Civil War*.  Strip it here, and
    remember the number so the report can say it did.
    """
    if not series or not str(series).strip():
        return series, series_key, None
    match = _ORDER_PREFIX_RE.match(str(series))
    if not match:
        return series, series_key, None
    rest = str(series)[match.end():].strip(" -_,;:.")
    if not rest:
        return series, series_key, None
    return rest, parser_mod.normalise_series(rest), match.group(1)


def _best_page(pages) -> tuple[int, int] | None:
    """Largest (width, height) in a ComicInfo page list - the best evidence we have."""
    best = None
    for page in pages or []:
        width, height = page.get("width"), page.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        if width <= 0 or height <= 0:
            continue
        if best is None or width * height > best[0] * best[1]:
            best = (width, height)
    return best


def _entry(row: dict) -> dict:
    """One index row as the checks want to see it (parsed once, grouped forever)."""
    parsed = _json(row.get("filename_parsed")) or {}
    series, series_key, order = canonical_series(row.get("series"), row.get("series_key"))
    pages = _json(row.get("pages")) or []
    page_list = pages if isinstance(pages, list) else []
    return {
        "path": str(row.get("path")),
        "filename": row.get("filename") or os.path.basename(str(row.get("path"))),
        "folder": os.path.dirname(str(row.get("path"))),
        "status": row.get("status") or "ok",
        "error_kind": row.get("error_kind"),
        "error_message": row.get("error_message"),
        "series": series,
        "series_raw": row.get("series"),
        "series_key": series_key,
        "order_number": order,
        "issue_number": None if row.get("issue_number") is None else str(row["issue_number"]),
        "annual": bool(parsed.get("annual")) or "annual" in (row.get("series_key") or ""),
        "volume_raw": row.get("volume_raw"),
        "file_size": row.get("file_size"),
        "page_count": row.get("page_count"),
        "comicinfo_page_count": row.get("comicinfo_page_count"),
        "comicinfo_present": bool(row.get("comicinfo_present")),
        "container": row.get("archive_container"),
        "extension": row.get("archive_type"),
        "mismatch": bool(row.get("container_mismatch")),
        "dimensions": _best_page(page_list),
        "metadata_message": (row.get("error_message")
                             if row.get("status") == "ok" and row.get("error_message") else None),
    }


def load_rows(conn, comics_path: str | None = None) -> list[dict]:
    """Index rows (optionally only those under ``comics_path``)."""
    rows = [dict(row) for row in conn.execute("SELECT * FROM comics ORDER BY path")]
    if not comics_path:
        return rows
    root = os.path.normcase(os.path.abspath(os.path.expanduser(comics_path)))
    keep = []
    for row in rows:
        path = os.path.normcase(os.path.abspath(str(row.get("path") or "")))
        if path == root or path.startswith(root + os.sep):
            keep.append(row)
    return keep


# ---------------------------------------------------------------- 1. unreadable


def check_unreadable(entries: list[dict]) -> dict:
    """Every row that did not index, grouped by reason (`error_kind`)."""
    groups: dict[str, dict] = {}
    broken_metadata = []
    for entry in entries:
        if entry["status"] == "ok":
            if entry["metadata_message"]:
                broken_metadata.append(entry)
            continue
        kind = (entry["error_kind"]
                or ("file_missing" if entry["status"] == "missing" else "unknown"))
        group = groups.setdefault(kind, {
            "kind": kind,
            "count": 0,
            "reason": entry["error_message"] or "no reason recorded",
            "fix": ADVICE.get(kind, ""),
            "files": [],
        })
        group["count"] += 1
        group["files"].append({
            "path": entry["path"], "filename": entry["filename"], "folder": entry["folder"],
            "status": entry["status"], "container": entry["container"],
            "extension": entry["extension"],
        })
    ordered = sorted(groups.values(), key=lambda group: (-group["count"], group["kind"]))
    for group in ordered:
        group["files"].sort(key=lambda item: (item["filename"] or "").casefold())
    return {
        "total": sum(group["count"] for group in ordered),
        "missing": sum(group["count"] for group in ordered if group["kind"] == "file_missing"),
        "groups": ordered,
        "broken_metadata": {
            "count": len(broken_metadata),
            "files": [{"path": e["path"], "filename": e["filename"],
                       "message": e["metadata_message"]} for e in broken_metadata],
            "fix": ADVICE.get("comicinfo_invalid", ""),
        },
    }


# ---------------------------------------------------------------- 2. duplicates


def _score(entry: dict) -> tuple:
    """Better copy first: readable, then biggest pages, then pages, then bytes."""
    width, height = entry["dimensions"] or (0, 0)
    return (
        0 if entry["status"] == "ok" else 1,
        -(width * height),
        -(int(entry["page_count"] or 0)),
        -(int(entry["file_size"] or 0)),
        entry["path"],
    )


def _describe_dimensions(entry: dict) -> str | None:
    if not entry["dimensions"]:
        return None
    return f"{entry['dimensions'][0]}x{entry['dimensions'][1]}"


def _copy_detail(entry: dict) -> dict:
    return {
        "path": entry["path"],
        "filename": entry["filename"],
        "folder": entry["folder"],
        "file_size": entry["file_size"],
        "file_size_human": human_size(entry["file_size"]),
        "page_count": entry["page_count"],
        "container": entry["container"],
        "extension": entry["extension"],
        "comicinfo_present": entry["comicinfo_present"],
        "dimensions": list(entry["dimensions"]) if entry["dimensions"] else None,
        "status": entry["status"],
        "error_kind": entry["error_kind"],
    }


def check_duplicates(entries: list[dict]) -> dict:
    """Issues that have more than one file, with the evidence and a suggested keeper."""
    groups: dict[tuple, list[dict]] = {}
    ungrouped = 0
    for entry in entries:
        if entry["status"] == "missing":
            ungrouped += 1                       # the file is gone: no decision to make
            continue
        if not entry["series_key"] or not entry["issue_number"]:
            ungrouped += 1
            continue
        groups.setdefault((entry["series_key"], entry["annual"], entry["issue_number"]),
                          []).append(entry)

    issues = []
    for (series_key, annual, issue_number), copies in groups.items():
        if len(copies) < 2:
            continue
        copies.sort(key=_score)
        readable = [c for c in copies if c["status"] == "ok"]
        sizes = {(c["file_size"], c["page_count"]) for c in readable}
        dimensions = {_describe_dimensions(c) for c in readable}
        identical = bool(readable) and len(readable) == len(copies) and len(sizes) == 1
        if identical and len(dimensions - {None}) > 1:
            identical = False                    # same size, different pixels: re-saved
        keeper = copies[0]
        series = copies[0]["series"] or series_key

        if identical:
            verdict = ("these look like the same file twice (identical size and page count), "
                       "probably one issue saved in two folders")
            suggestion = (f"keep either copy ({os.path.basename(keeper['path'])}) and remove the "
                          "other yourself - Longbox never deletes anything")
            kind = "identical_duplicate"
        elif not readable:
            verdict = "no copy of this issue can be opened"
            suggestion = "fix or replace the file(s): the reasons are in section 1"
            kind = "quality_differs"
        else:
            best = _describe_dimensions(keeper) or human_size(keeper["file_size"])
            others = [_describe_dimensions(c) or human_size(c["file_size"]) for c in copies[1:]]
            verdict = (f"different quality: {best} against {', '.join(others)}")
            suggestion = (f"{os.path.basename(keeper['path'])} looks like the better copy "
                          f"({best}) - suggestion only, check it by eye before removing anything")
            kind = "quality_differs"

        if keeper["status"] != "ok" and readable:
            keeper = readable[0]
            suggestion = (f"{os.path.basename(keeper['path'])} is the copy that still opens - "
                          "the other one cannot be read (section 1)")

        issues.append({
            "series": series,
            "series_key": series_key,
            "issue_number": issue_number,
            "annual": annual,
            "label": f"{series} #{issue_number}" + (" (annual)" if annual else ""),
            "copies": [_copy_detail(c) for c in copies],
            "identical": identical,
            "kind": kind,
            "verdict": verdict,
            "suggestion": suggestion,
            "keeper": keeper["path"],
            "dimensions_known": any(c["dimensions"] for c in readable),
        })
    issues.sort(key=lambda item: (item["series_key"], item["issue_number"]))
    return {
        "count": len(issues),
        "files": sum(len(item["copies"]) for item in issues),
        "issues": issues,
        "skipped": ungrouped,
    }


# ---------------------------------------------------------------- 3. missing numbers


def _ranges(numbers: list[int]) -> str:
    """``[1,2,3,5]`` -> ``"1-3, 5"``."""
    parts: list[str] = []
    start = previous = None
    for number in numbers:
        if start is None:
            start = previous = number
            continue
        if number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    if start is not None:
        parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def check_missing_numbers(entries: list[dict]) -> dict:
    """Per series: the numbered issues present, and the gaps between them."""
    runs: dict[str, dict] = {}
    skipped = {"no_issue_number": 0, "non_numeric": 0, "annuals": 0,
               "no_series": 0, "file_missing": 0}
    order_prefix_files = 0
    for entry in entries:
        if entry["order_number"]:
            order_prefix_files += 1
        if entry["annual"]:
            skipped["annuals"] += 1
            continue
        number = entry["issue_number"]
        if number is None or not str(number).strip():
            skipped["no_issue_number"] += 1
            continue
        if not _INTEGER_ISSUE_RE.match(str(number).strip()):
            skipped["non_numeric"] += 1            # 1.MU, 12.5, "Annual"...
            continue
        if not entry["series_key"]:
            skipped["no_series"] += 1
            continue
        if entry["status"] == "missing":
            skipped["file_missing"] += 1           # gone from disk: not "have"
            continue
        run = runs.setdefault(entry["series_key"], {
            "series": entry["series"], "series_key": entry["series_key"],
            "numbers": [], "files": 0, "volumes": set(), "entries": [],
        })
        run["numbers"].append(int(str(number).strip()))
        run["files"] += 1
        run["entries"].append(entry["filename"])
        if entry["volume_raw"]:
            run["volumes"].add(str(entry["volume_raw"]))

    series_reports = []
    small = 0
    for run in runs.values():
        numbers = sorted(set(run["numbers"]))
        if len(numbers) < 3:                       # one-shots and two-issue stubs: no run
            small += 1
            continue
        gaps = [number for number in range(numbers[0], numbers[-1] + 1)
                if number not in set(numbers)]
        volumes = sorted(run["volumes"])
        ambiguous = False
        ambiguity_reason = None
        if gaps:
            if len(volumes) > 1:
                ambiguous = True
                ambiguity_reason = ("the files carry more than one Volume value "
                                    f"({', '.join(volumes)}) - this may be two volumes of the "
                                    "same title numbered separately")
            elif gaps[-1] - gaps[0] > WIDE_GAP:
                ambiguous = True
                ambiguity_reason = ("the numbers jump across a very wide span - this may be two "
                                    "volumes of the same title numbered separately")
        series_reports.append({
            "series": run["series"],
            "series_key": run["series_key"],
            "files": run["files"],
            "present": numbers,
            "have": _ranges(numbers),
            "gaps": gaps,
            "missing": (str(len(gaps)) + (" issue" if len(gaps) == 1 else " issues")) if gaps else "",
            "complete": not gaps,
            "ambiguous": ambiguous,
            "note": ambiguity_reason,
            "volumes": volumes,
        })
    # series with a gap first - that is the actionable end of the list
    series_reports.sort(key=lambda item: (item["complete"] or item["ambiguous"], item["series_key"]))
    gaps_total = sum(len(item["gaps"]) for item in series_reports if not item["ambiguous"])
    return {
        "series_checked": len(series_reports),
        "series_with_gaps": sum(1 for item in series_reports
                                if item["gaps"] and not item["ambiguous"]),
        "missing_numbers": gaps_total,
        "series": series_reports,
        "skipped": skipped,
        "skipped_small_series": small,
        "order_prefix_files": order_prefix_files,
    }


# ---------------------------------------------------------------- --deep


def _image_header_size(data: bytes):
    """(width, height) from an image header - no pixel decode, no temp file."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return [int(image.size[0]), int(image.size[1])]
    except Exception:
        return None


def deep_read(path: str, comicinfo_page_count=None) -> dict:
    """Open one archive and read every page member; report what the listing hid."""
    problems: list[dict] = []
    with archive_mod.open_archive(path) as handle:
        pages = handle.pages()
        pages_read = 0
        cover = None
        for name in pages:
            try:
                data = handle.read(name)
            except archive_mod.ArchiveError as exc:
                problems.append({
                    "member": name,
                    "kind": "deep_corrupt_member",
                    "message": f"page '{name}' cannot be read: {exc}",
                })
                continue
            pages_read += 1
            if not data:
                problems.append({"member": name, "kind": "deep_empty_page",
                                 "message": f"page '{name}' is empty (0 bytes)"})
            elif not archive_mod.plausible_image_bytes(data):
                problems.append({"member": name, "kind": "deep_page_not_an_image",
                                 "message": f"page '{name}' is not image data"})
            if cover is None:
                size = _image_header_size(data)
                if size:
                    cover = size
        if comicinfo_page_count and pages and int(comicinfo_page_count) != len(pages):
            problems.append({
                "member": "ComicInfo.xml",
                "kind": "deep_page_count_mismatch",
                "message": (f"ComicInfo.xml lists {int(comicinfo_page_count)} pages but the "
                            f"archive holds {len(pages)}"),
            })
        total = len(pages)
        members = len(handle.names)
    return {
        "ok": not problems,
        "pages": total,
        "pages_read": pages_read,
        "members": members,
        "cover_size": cover,
        "problems": problems,
    }


def deep_check(entries: list[dict], progress=None, results: dict | None = None) -> dict:
    """Read every page of every indexed archive. Slow on purpose; Ctrl-C is safe."""
    results = results if results is not None else {}
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        if progress:
            progress(index, total, entry["path"])
        path = entry["path"]
        if not os.path.exists(path):
            results[path] = {"ok": False, "pages": 0, "pages_read": 0, "cover_size": None,
                             "problems": [{"member": None, "kind": "file_missing",
                                           "message": "the file is no longer there"}]}
            continue
        try:
            results[path] = deep_read(path, entry.get("comicinfo_page_count"))
        except archive_mod.ArchiveError as exc:
            results[path] = {"ok": False, "pages": 0, "pages_read": 0, "cover_size": None,
                             "problems": [{"member": None, "kind": exc.kind,
                                           "message": str(exc)}]}
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # one bad file must not end the run
            results[path] = {"ok": False, "pages": 0, "pages_read": 0, "cover_size": None,
                             "problems": [{"member": None, "kind": "unexpected_error",
                                           "message": f"{exc.__class__.__name__}: {exc}"}]}
    return results


def _group_problems(items: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for item in items:
        group = groups.setdefault(item["kind"], {
            "kind": item["kind"], "count": 0, "reason": item["message"],
            "fix": ADVICE.get(item["kind"], ""), "files": [],
        })
        group["count"] += 1
        group["files"].append({"path": item["path"], "filename": item["filename"],
                               "message": item["message"]})
    return sorted(groups.values(), key=lambda group: (-group["count"], group["kind"]))


def _deep_section(entries: list[dict], results: dict, elapsed: float) -> dict:
    """What the deep pass found beyond the shallow index."""
    by_path = {entry["path"]: entry for entry in entries}
    new_problems = []
    recovered = []
    for path, result in results.items():
        entry = by_path.get(path)
        if entry is None:
            continue
        if entry["status"] == "ok":
            if not result["ok"]:
                for problem in result["problems"]:
                    new_problems.append({
                        "path": path, "filename": entry["filename"], "entry": entry,
                        "member": problem.get("member"), "kind": problem["kind"],
                        "message": problem["message"],
                    })
        elif result["ok"]:
            recovered.append({"path": path, "filename": entry["filename"],
                              "was": entry["error_kind"] or entry["status"]})
    return {
        "checked": len(results),
        "failed": len({item["path"] for item in new_problems}),
        "elapsed": round(elapsed, 2),
        "problems": new_problems,
        "groups": _group_problems(new_problems),
        "recovered": recovered,
    }


# ---------------------------------------------------------------- report


def build_report(entries: list[dict], comics_path: str | None, db_path: str | None,
                 deep: bool = False, deep_results: dict | None = None,
                 deep_elapsed: float = 0.0, generated_at: str | None = None,
                 elapsed: float = 0.0) -> dict:
    report = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at or utcnow(),
        "comics_path": comics_path,
        "db": os.path.abspath(db_path) if db_path else None,
        "deep": deep,
        "files_indexed": len(entries),
        "elapsed": round(elapsed, 2),
        "unreadable": check_unreadable(entries),
        "duplicates": check_duplicates(entries),
        "missing_numbers": check_missing_numbers(entries),
    }
    if deep:
        report["deep_check"] = _deep_section(entries, deep_results or {}, deep_elapsed)
    report["summary"] = {
        "files": len(entries),
        "unreadable": report["unreadable"]["total"],
        "missing_files": report["unreadable"]["missing"],
        "broken_metadata": report["unreadable"]["broken_metadata"]["count"],
        "duplicate_issues": report["duplicates"]["count"],
        "series_with_gaps": report["missing_numbers"]["series_with_gaps"],
        "missing_numbers": report["missing_numbers"]["missing_numbers"],
    }
    if deep:
        report["summary"]["deep_failures"] = report["deep_check"]["failed"]
        report["summary"]["deep_recovered"] = len(report["deep_check"]["recovered"])
    findings = collect_findings(report)
    report["findings"] = findings
    report["summary"]["findings"] = len(findings)
    report["exit_code"] = EXIT_FINDINGS if findings else EXIT_CLEAN
    return report


def collect_findings(report: dict) -> list[dict]:
    """One row per thing the owner has to look at - the shape the UI panel reads."""
    findings: list[dict] = []
    for group in report["unreadable"]["groups"]:
        findings.append({
            "section": "unreadable", "severity": "error", "kind": group["kind"],
            "series": None, "series_key": None, "issue_number": None,
            "path": group["files"][0]["path"] if group["files"] else None,
            "summary": (f"{group['count']} file(s) cannot be read: {group['reason']}"),
            "detail": {"count": group["count"], "reason": group["reason"],
                       "fix": group["fix"], "files": group["files"]},
        })
    metadata = report["unreadable"]["broken_metadata"]
    if metadata["count"]:
        findings.append({
            "section": "unreadable", "severity": "info", "kind": "comicinfo_invalid",
            "series": None, "series_key": None, "issue_number": None,
            "path": metadata["files"][0]["path"] if metadata["files"] else None,
            "summary": (f"{metadata['count']} readable file(s) have a broken ComicInfo.xml "
                        f"(the pages still read)"),
            "detail": metadata,
        })
    for issue in report["duplicates"]["issues"]:
        findings.append({
            "section": "duplicate", "severity": "warning", "kind": issue["kind"],
            "series": issue["series"], "series_key": issue["series_key"],
            "issue_number": issue["issue_number"], "path": issue["keeper"],
            "summary": f"{issue['label']}: {len(issue['copies'])} copies - {issue['verdict']}",
            "detail": {"verdict": issue["verdict"], "suggestion": issue["suggestion"],
                       "keeper": issue["keeper"], "copies": issue["copies"]},
        })
    for series in report["missing_numbers"]["series"]:
        if series["gaps"]:
            findings.append({
                "section": "missing_number",
                "severity": "info" if series["ambiguous"] else "warning",
                "kind": "ambiguous_numbering" if series["ambiguous"] else "gap",
                "series": series["series"], "series_key": series["series_key"],
                "issue_number": None, "path": None,
                "summary": (f"{series['series']}: have {series['have']}"
                            + (f" (possibly missing {_ranges(series['gaps'])})"
                               if series["ambiguous"] else f" (missing {_ranges(series['gaps'])})")),
                "detail": series,
            })
    if report.get("deep"):
        for group in report["deep_check"]["groups"]:
            findings.append({
                "section": "unreadable", "severity": "error", "kind": group["kind"],
                "series": None, "series_key": None, "issue_number": None,
                "path": group["files"][0]["path"] if group["files"] else None,
                "summary": (f"{group['count']} file(s) look fine when listed but fail when read: "
                            f"{group['reason']}"),
                "detail": {"count": group["count"], "reason": group["reason"],
                           "fix": group["fix"], "files": group["files"]},
            })
        recovered = report["deep_check"]["recovered"]
        if recovered:
            findings.append({
                "section": "unreadable", "severity": "info", "kind": "now_readable",
                "series": None, "series_key": None, "issue_number": None,
                "path": recovered[0]["path"],
                "summary": (f"{len(recovered)} file(s) failed at scan time but read fine now - "
                            "run `python -m longbox.scan` to index them"),
                "detail": {"count": len(recovered), "files": recovered},
            })
    return findings


def format_report(report: dict, examples: int = DEFAULT_EXAMPLES) -> str:
    """The plain-English report the owner reads."""
    lines: list[str] = []
    summary = report["summary"]
    lines.append("Longbox library doctor")
    lines.append(f"  comics folder : {report['comics_path'] or '(not given - every row in the index)'}")
    lines.append(f"  index         : {report['db'] or '(in-memory)'}"
                 f"  -  {summary['files']:,} files indexed")
    lines.append("  read-only     : nothing was written, moved or renamed inside the comics folder")
    lines.append(f"  generated     : {report['generated_at']}"
                 f"  ({report['elapsed']}s)")
    lines.append("")

    unreadable = report["unreadable"]
    lines.append(f"1. Unreadable or broken archives - {unreadable['total']} file(s)")
    if not unreadable["groups"]:
        lines.append("   None: every file in the index opened when it was scanned.")
    for group in unreadable["groups"]:
        lines.append(f"   [{group['count']:>4}] {group['kind']}")
        lines.append(f"          what: {group['reason']}")
        if group["fix"]:
            lines.append(f"          do  : {group['fix']}")
        for item in group["files"][:examples]:
            where = "" if item["folder"] == report["comics_path"] else f"   (in {item['folder']})"
            lines.append(f"          e.g. {item['filename']}{where}")
        if len(group["files"]) > examples:
            lines.append(f"          ... and {len(group['files']) - examples} more file(s) with "
                         "this exact reason (see --json for the full list)")
    metadata = unreadable["broken_metadata"]
    if metadata["count"]:
        lines.append(f"   Also {metadata['count']} readable file(s) have a broken ComicInfo.xml "
                     "(the pages still read, the metadata is missing):")
        for item in metadata["files"][:examples]:
            lines.append(f"          e.g. {item['filename']} - {item['message']}")
    if unreadable["missing"]:
        lines.append(f"   {unreadable['missing']} of these are simply gone from the comics folder "
                     "- re-run `python -m longbox.scan` to tidy the index.")
    lines.append("")

    duplicates = report["duplicates"]
    lines.append(f"2. Duplicate issues - {duplicates['count']} issue(s) have more than one file"
                 f" ({duplicates['files']} files)")
    if not duplicates["issues"]:
        lines.append("   None: every issue in the index is there once.")
    for index, issue in enumerate(duplicates["issues"], start=1):
        lines.append(f"   {issue['label']} - {len(issue['copies'])} copies")
        for position, copy in enumerate(issue["copies"]):
            dims = (f"{copy['dimensions'][0]}x{copy['dimensions'][1]}"
                    if copy["dimensions"] else "page size not recorded")
            lines.append(f"     {'ABCDEFGH'[position] if position < 8 else position}. "
                         f"{copy['filename']}")
            lines.append(f"        {copy['folder']}")
            lines.append(f"        {copy['file_size_human']} | {copy['page_count']} pages | "
                         f"{copy['container']} | ComicInfo: "
                         f"{'yes' if copy['comicinfo_present'] else 'no'} | {dims}"
                         + ("" if copy["status"] == "ok"
                            else f" | UNREADABLE ({copy['error_kind']})"))
        lines.append(f"        verdict   : {issue['verdict']}")
        lines.append(f"        suggestion: {issue['suggestion']}")
        if not issue["dimensions_known"]:
            lines.append("        (page sizes are not recorded for these files - re-run with "
                         "--deep to measure the cover of each copy)")
    if duplicates["skipped"]:
        lines.append(f"   ({duplicates['skipped']} file(s) skipped: no series, no issue number, or "
                     "the file is gone)")
    lines.append("")

    missing = report["missing_numbers"]
    lines.append(f"3. Missing issue numbers in a run - {missing['series_with_gaps']} series with a gap"
                 f" ({missing['missing_numbers']} issue(s) missing)")
    if not missing["series"]:
        lines.append("   None: no series with three or more numbered issues.")
    for series in missing["series"][:MAX_SERIES_LINES]:
        state = "complete" if series["complete"] else f"MISSING {_ranges(series['gaps'])}"
        if series["ambiguous"]:
            state = f"possibly missing {_ranges(series['gaps'])}"
        lines.append(f"   {series['series']}: have {series['have']}"
                     f" ({series['files']} file(s)) - {state}")
        if series["note"]:
            lines.append(f"      careful: {series['note']}")
    if len(missing["series"]) > MAX_SERIES_LINES:
        lines.append(f"   ... and {len(missing['series']) - MAX_SERIES_LINES} more series "
                     "(see --json)")
    skipped = missing["skipped"]
    lines.append("   Not counted as runs: "
                 f"{missing['skipped_small_series']} series with fewer than 3 numbered issues, "
                 f"{skipped['annuals']} annual(s), "
                 f"{skipped['no_issue_number']} file(s) with no issue number, "
                 f"{skipped['non_numeric']} with a non-numeric number"
                 + (f", {skipped['file_missing']} whose file is gone" if skipped["file_missing"] else ""))
    if missing["order_prefix_files"]:
        lines.append(f"   {missing['order_prefix_files']} file(s) carry a leading reading-order "
                     "number (e.g. 008- Civil War 1.cbr) - they are grouped under the real series, "
                     "so they do not create a separate series or a false gap.")
    lines.append("")

    if report.get("deep"):
        deep = report["deep_check"]
        lines.append(f"Deep check (reads every page of {deep['checked']} archive(s), {deep['elapsed']}s)")
        if not deep["problems"]:
            lines.append("   Every page of every archive could be read.")
        else:
            lines.append(f"   {deep['failed']} file(s) list fine but cannot be read completely:")
            for group in deep["groups"]:
                lines.append(f"   [{group['count']:>4}] {group['kind']}")
                lines.append(f"          what: {group['reason']}")
                if group["fix"]:
                    lines.append(f"          do  : {group['fix']}")
                for item in group["files"][:examples]:
                    lines.append(f"          e.g. {item['filename']}")
                    lines.append(f"               {item['path']}")
                if len(group["files"]) > examples:
                    lines.append(f"          ... and {len(group['files']) - examples} more")
        if deep["recovered"]:
            lines.append(f"   {len(deep['recovered'])} file(s) that failed at scan time read fine "
                         "now - run `python -m longbox.scan` to index them:")
            for item in deep["recovered"][:examples]:
                lines.append(f"          e.g. {item['filename']}")
        lines.append("")

    lines.append(f"Findings: {summary['findings']} - "
                 f"{summary['unreadable']} unreadable file(s), "
                 f"{summary['duplicate_issues']} duplicate issue(s), "
                 f"{summary['series_with_gaps']} series with a gap.")
    if summary["findings"]:
        lines.append("Nothing in your comics folder was changed. Fix what is actionable, then "
                     "re-run `python -m longbox.scan` and this check.")
        lines.append(f"Exit code {report['exit_code']}: findings exist.")
    else:
        lines.append("Nothing to fix.")
        lines.append("Exit code 0: no findings.")
    return "\n".join(lines)


# ---------------------------------------------------------------- persistence


def save_run(conn, report: dict) -> str:
    """Store this run and its findings; the tables always hold the latest run only.

    A later UI panel renders whatever is in `doctor_findings`, so keeping yesterday's
    findings would show the owner problems that may already be fixed.
    """
    run_id = run_id_for()
    conn.execute("DELETE FROM doctor_findings")
    conn.execute("DELETE FROM doctor_runs")
    conn.execute(
        "INSERT INTO doctor_runs (run_id, started_at, finished_at, comics_path, db_path, "
        "deep, exit_code, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, report["generated_at"], utcnow(), report["comics_path"], report["db"],
         1 if report["deep"] else 0, report["exit_code"],
         json.dumps(report["summary"], ensure_ascii=False)),
    )
    conn.executemany(
        "INSERT INTO doctor_findings (run_id, section, severity, kind, series, series_key, "
        "issue_number, summary, detail, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, f["section"], f["severity"], f["kind"], f["series"], f["series_key"],
          f["issue_number"], f["summary"], json.dumps(f["detail"], ensure_ascii=False),
          f["path"], report["generated_at"]) for f in report["findings"]],
    )
    conn.commit()
    return run_id


# ---------------------------------------------------------------- entry point


def run_doctor(comics_path: str | None = None, db_path: str | None = None, *,
               deep: bool = False, progress=None, save: bool = True,
               conn=None, deep_progress=None) -> dict:
    """Run the three checks and return the report dict (see ``build_report``)."""
    started = time.time()
    own_conn = conn is None
    if conn is None:
        conn = db_mod.open_db(db_path or db_mod.default_db_path())
    try:
        entries = [_entry(row) for row in load_rows(conn, comics_path)]
        generated_at = utcnow()
        deep_results = None
        deep_elapsed = 0.0
        if deep:
            deep_started = time.time()
            deep_results = deep_check(entries, progress=deep_progress or progress)
            deep_elapsed = time.time() - deep_started
            for entry in entries:                     # measured cover, when available
                result = deep_results.get(entry["path"]) or {}
                if result.get("cover_size") and not entry["dimensions"]:
                    entry["dimensions"] = tuple(result["cover_size"])
        report = build_report(entries, comics_path, db_path, deep=deep,
                              deep_results=deep_results, deep_elapsed=deep_elapsed,
                              generated_at=generated_at, elapsed=time.time() - started)
        if save:
            report["run_id"] = save_run(conn, report)
    finally:
        if own_conn:
            conn.close()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m longbox.doctor",
        description=("Check an indexed comics library: broken archives, duplicate issues and "
                     "missing issue numbers. Reads the index only (--deep re-opens the archives); "
                     "never writes anything inside the comics folder."),
    )
    parser.add_argument("--path", default=os.environ.get("LONGBOX_COMICS"),
                        help="comics folder; only files under it are reported "
                             "(env: LONGBOX_COMICS)")
    parser.add_argument("--db", default=db_mod.default_db_path(),
                        help="SQLite index written by `python -m longbox.scan` "
                             "(env: LONGBOX_DB, default data/longbox.db)")
    parser.add_argument("--deep", action="store_true",
                        help="also re-open every archive and read every page - the only slow "
                             "mode; the default pass reads the database only")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument("--no-save", action="store_true",
                        help="do not write the findings into the database")
    parser.add_argument("--examples", type=int, default=DEFAULT_EXAMPLES,
                        help=f"example files shown per group in the text report "
                             f"(default {DEFAULT_EXAMPLES})")
    parser.add_argument("--quiet", action="store_true", help="no progress output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    comics_path = os.path.abspath(os.path.expanduser(args.path)) if args.path else None
    if comics_path and not os.path.isdir(comics_path):
        print(f"error: comics folder not found: {comics_path}", file=sys.stderr)
        return EXIT_USAGE
    db_path = os.path.abspath(os.path.expanduser(args.db))
    if not os.path.exists(db_path):
        print(f"error: no index at {db_path} - run "
              f"`python -m longbox.scan --path <comics folder> --db {args.db}` first",
              file=sys.stderr)
        return EXIT_USAGE

    def progress(count: int, total: int, path: str) -> None:
        print(f"  reading [{count}/{total}] {os.path.basename(path)}", file=sys.stderr)

    deep_progress = None
    if args.deep and not args.quiet:
        print(f"deep check: re-opening every archive and reading every page - this is the slow "
              f"mode and can take minutes on a big library (Ctrl-C is safe)", file=sys.stderr)
        deep_progress = progress

    report = None
    try:
        report = run_doctor(comics_path, db_path, deep=args.deep, save=not args.no_save,
                            deep_progress=deep_progress)
    except KeyboardInterrupt:
        print("\ninterrupted - findings found so far were kept", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # never a traceback for the owner
        print(f"error: the check could not run: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report, examples=max(0, args.examples)))
    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
