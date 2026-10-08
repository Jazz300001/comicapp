"""ComicInfo.xml parsing.

Every field is optional: some issues have no creator credits at all, and a
missing tag is never an error.  Multi-valued tags are comma separated in the XML
and become Python lists here (stored as JSON text in SQLite for now - junction
tables are a later phase).

The two facts from the owner's real files that shape this module:

  * ``<Volume>`` is inconsistent: ``2024`` in some issues, ``158814`` in others,
    for the same series.  It is stored raw and tagged (``volume_year`` /
    ``volume_id`` / ``other``) and is NEVER used for grouping.
  * ``<Web>`` carries the ComicVine issue id (``.../4000-1095610/``) and
    ``<Notes>`` repeats it as ``[CVDB1095610]`` in some scrapers.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

#: XML tag -> (python key, kind).  kind: "text", "int", "list"
FIELD_MAP: dict[str, tuple[str, str]] = {
    "Title": ("title", "text"),
    "Series": ("series", "text"),
    "Number": ("number", "text"),
    "Volume": ("volume", "text"),
    "Summary": ("summary", "text"),
    "Notes": ("notes", "text"),
    "Year": ("year", "int"),
    "Month": ("month", "int"),
    "Day": ("day", "int"),
    "Publisher": ("publisher", "text"),
    "Writer": ("writers", "list"),
    "Penciller": ("pencillers", "list"),
    "Inker": ("inkers", "list"),
    "Colorist": ("colorists", "list"),
    "Letterer": ("letterers", "list"),
    "CoverArtist": ("cover_artists", "list"),
    "Editor": ("editors", "list"),
    "Web": ("web", "text"),
    "PageCount": ("page_count", "int"),
    "Characters": ("characters", "list"),
    "Teams": ("teams", "list"),
    "Locations": ("locations", "list"),
    "ScanInformation": ("scan_information", "text"),
}

_WEB_ISSUE_ID_RE = re.compile(r"/4000-(\d+)")
_NOTES_ISSUE_ID_RE = re.compile(r"\[\s*CVDB\s*(\d+)\s*\]", re.IGNORECASE)
_TRAILING_ID_RE = re.compile(r"(?<!\d)(\d{4,})/?\s*$")


class ComicInfoError(Exception):
    """Raised when ComicInfo.xml is not parseable at all."""


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d+", value)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:  # pragma: no cover
        return None


def split_multi(value: str | None) -> list[str]:
    """Comma separated ComicInfo field -> list of non-empty trimmed values."""
    if not value:
        return []
    parts = [part.strip() for part in str(value).replace("\r\n", "\n").split(",")]
    return [part for part in parts if part]


def classify_volume(raw: str | None) -> tuple[str | None, str | None]:
    """(raw, kind) where kind is volume_year / volume_id / other / None.

    Never a grouping key - see the module docstring.
    """
    if raw is None or not str(raw).strip():
        return None, None
    value = str(raw).strip()
    if re.fullmatch(r"(?:19|20)\d{2}", value):
        return value, "volume_year"
    if re.fullmatch(r"\d+", value):
        return value, "volume_id"
    return value, "other"


def extract_comicvine_issue_id(web: str | None, notes: str | None = None) -> str | None:
    """ComicVine issue id from <Web> (preferred) or <Notes> [CVDBnnn]."""
    if web:
        match = _WEB_ISSUE_ID_RE.search(web)
        if match:
            return match.group(1)
        match = _TRAILING_ID_RE.search(web.strip())
        if match:
            return match.group(1)
    if notes:
        match = _NOTES_ISSUE_ID_RE.search(notes)
        if match:
            return match.group(1)
    return None


def _parse_pages(element: ET.Element | None) -> list[dict]:
    pages: list[dict] = []
    if element is None:
        return pages
    for page in element:
        if _strip_namespace(page.tag).lower() != "page":
            continue
        entry = {
            "image": _to_int(page.get("Image")),
            "size": _to_int(page.get("ImageSize")),
            "width": _to_int(page.get("ImageWidth")),
            "height": _to_int(page.get("ImageHeight")),
            "type": (page.get("Type") or "").strip() or None,
        }
        pages.append(entry)
    return pages


def parse_comicinfo(data: str | bytes) -> dict:
    """Parse ComicInfo.xml text/bytes into a plain dict (all keys always present)."""
    if isinstance(data, bytes):
        text = _decode(data)
    else:
        text = str(data)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ComicInfoError(f"ComicInfo.xml is not valid XML: {exc}") from exc

    result: dict = {
        "title": None, "series": None, "number": None, "volume": None,
        "volume_kind": None, "summary": None, "notes": None,
        "year": None, "month": None, "day": None, "publisher": None,
        "writers": [], "pencillers": [], "inkers": [], "colorists": [],
        "letterers": [], "cover_artists": [], "editors": [],
        "web": None, "page_count": None,
        "characters": [], "teams": [], "locations": [],
        "scan_information": None, "pages": [],
        "comicvine_issue_id": None,
    }

    for child in root:
        tag = _strip_namespace(child.tag)
        if tag.lower() == "pages":
            result["pages"] = _parse_pages(child)
            continue
        mapped = FIELD_MAP.get(tag)
        if mapped is None:
            continue
        key, kind = mapped
        raw = (child.text or "").strip() or None
        if kind == "list":
            result[key] = split_multi(raw)
        elif kind == "int":
            result[key] = _to_int(raw)
        else:
            result[key] = raw

    result["volume"], result["volume_kind"] = classify_volume(result["volume"])
    result["comicvine_issue_id"] = extract_comicvine_issue_id(result["web"], result["notes"])
    return result


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
