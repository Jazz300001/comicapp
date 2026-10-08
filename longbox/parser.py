"""Filename -> series / volume / issue / year.

Pure stdlib, no I/O, no database.  Filenames in a real collection are messy:
release-group tags, brackets, underscores, spaces, unicode.  Everything here is
best effort: a missing year or issue number is not an error, it is just None.

Judgement calls (documented on purpose):
  * ``X-Men Annual 3 (1990).cbz`` -> series "X-Men Annual", issue "3", annual=True.
    Keeping "Annual" in the series name means Annual #3 never collides with
    X-Men #3 inside one series, which is how collectors shelve them.
  * The issue number is the *display* string ("1", "1.MU", "12.5"); a parallel
    numeric ``issue_sort`` exists so sorting works and is None when the number
    is not numeric.
"""

from __future__ import annotations

import os
import re
import unicodedata

#: release-group noise.  Bracketed noise ("(digital)", "[c2c]") is dropped
#: wherever it appears; these words are only dropped from the *tail* of a name
#: that has no issue number, because "Broken Scan 04" is a real series name and
#: blind word-stripping would turn it into "Broken".
NOISE_WORDS = {
    "c2c", "digital", "scan", "scanned", "fixed", "repack", "webrip", "hd", "sd",
    "complete", "retail", "remastered", "no-ads", "cbz", "cbr",
}

ARCHIVE_EXTENSIONS = (".cbz", ".cbr")

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_BRACKETED_YEAR_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_BRACKETED_RE = re.compile(r"[\(\[]\s*[^\(\)\[\]]*\s*[\)\]]")
_VOLUME_RE = re.compile(r"\b(?:vol(?:ume)?\.?|v)\s*[-_. ]?\s*(\d{1,4})\b", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"\bannuals?\b", re.IGNORECASE)
_HASH_ISSUE_RE = re.compile(r"#\s*(\d+(?:[.,][0-9A-Za-z]+)*)")
_ISSUE_TOKEN_RE = re.compile(r"^(\d+(?:[.,][0-9A-Za-z]+)*)$")
_LEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)")
_TRAILING_JUNK_RE = re.compile(r"[\s\-_,;:.]+$")


def split_extension(filename: str) -> tuple[str, str | None]:
    """Return (stem, extension-without-dot-or-None).

    Both separators are honoured so a Windows path parses the same way on Linux
    (and vice versa) - the owner's files live on Windows.
    """
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(name)
    ext = ext.lstrip(".").lower() or None
    return stem, ext


def _clean_spaces(text: str) -> str:
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_trailing_noise(text: str) -> str:
    """Drop release-group words from the end of a name that has no issue number."""
    tokens = text.split(" ")
    while tokens and tokens[-1].strip(" -_,;:.").casefold() in NOISE_WORDS:
        tokens.pop()
    return " ".join(tokens).strip()


def normalise_series(name: str | None) -> str | None:
    """Normalised series name used for grouping.

    Lowercase, accents folded, everything that is not a letter or digit dropped
    ("Ultimate Spider-Man" -> "ultimatespiderman").  Deliberately does NOT include
    the volume: one series must stay one series even when `<Volume>` is the
    inconsistent mess it is in the owner's files.
    """
    if not name or not str(name).strip():
        return None
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    key = re.sub(r"[^0-9a-z]+", "", text)
    if not key:  # non-latin script: keep the raw name minus punctuation instead
        key = re.sub(r"\s+", "", text.strip())
    return key or None


def _issue_sort_value(issue_number: str | None) -> float | None:
    if issue_number is None:
        return None
    match = _LEADING_NUMBER_RE.match(str(issue_number).strip())
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees a float
        return None


def _normalise_issue_text(raw: str) -> str:
    """'001' -> '1', '12.5' -> '12.5', '1.MU' -> '1.MU'."""
    raw = raw.strip().rstrip(".").strip()
    if raw.isdigit():
        return str(int(raw))
    return raw


def parse_filename(filename: str) -> dict:
    """Parse one filename (or full path) into series / volume / issue / year."""
    stem, ext = split_extension(filename)
    work = _clean_spaces(stem)

    # ---- year -------------------------------------------------------------
    year = None
    bracketed = _BRACKETED_YEAR_RE.search(work)
    if bracketed:
        year = int(bracketed.group(1))
        work = work[: bracketed.start()] + " " + work[bracketed.end():]
    else:
        # a bare year token, but never the issue number of "Spider-Man 2099 ..."
        matches = [m for m in _YEAR_RE.finditer(work)]
        for match in reversed(matches):
            before = work[: match.start()]
            after = work[match.end():]
            if after.strip(" -_,;:") == "":  # the year is the last token
                year = int(match.group(1))
                work = before
                break

    # ---- release-group noise ---------------------------------------------
    work = _BRACKETED_RE.sub(" ", work)
    work = _clean_spaces(work)

    # ---- volume -----------------------------------------------------------
    volume = None
    vol_match = _VOLUME_RE.search(work)
    if vol_match:
        volume = str(int(vol_match.group(1)))
        work = (work[: vol_match.start()] + " " + work[vol_match.end():]).strip()

    # ---- annual -----------------------------------------------------------
    annual = bool(_ANNUAL_RE.search(work))

    # ---- issue ------------------------------------------------------------
    issue_number = None
    series_part = work
    hash_match = _HASH_ISSUE_RE.search(work)
    if hash_match:
        issue_number = _normalise_issue_text(hash_match.group(1))
        series_part = work[: hash_match.start()]
    else:
        tokens = work.split(" ")
        for index in range(len(tokens) - 1, -1, -1):
            token_match = _ISSUE_TOKEN_RE.match(tokens[index])
            if token_match:
                issue_number = _normalise_issue_text(token_match.group(1))
                series_part = " ".join(tokens[:index])
                break

    if issue_number is None:
        # no number to anchor on: only now is it safe to strip trailing noise
        series_part = _strip_trailing_noise(series_part)
    series = _TRAILING_JUNK_RE.sub("", _clean_spaces(series_part))
    series = series or None

    return {
        "series": series,
        "series_key": normalise_series(series),
        "volume": volume,
        "issue_number": issue_number,
        "issue_sort": _issue_sort_value(issue_number),
        "year": year,
        "annual": annual,
        "extension": ext,
    }
