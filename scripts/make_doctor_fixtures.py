#!/usr/bin/env python3
"""Fixtures the integrity checker (`python -m longbox.doctor`) is tested against.

Usage::

    python scripts/make_doctor_fixtures.py <folder>

It builds the standard library fixtures first (``scripts/make_fixtures.py``) and
then plants the cases the checks have to catch:

* an **identical duplicate** - the same issue copied byte for byte into a second
  folder (same size, same page count);
* a **lower-quality duplicate** - the same issue again, with fewer pixels per page;
* a **missing number** - a three-issue run of a reading-order folder
  (``001- Civil War 1.cbz`` ...) with number 3 absent, which also proves the
  leading order number does not create a fake series;
* a **single-issue series** ("Solo Run 1") that must NOT be reported as a gap;
* a **truncated archive that passes a shallow listing but fails ``--deep``** - one
  page's compressed bytes are overwritten while the central directory stays
  intact, so listing the archive works and reading the page does not;
* an **ambiguous run** - one series numbered across two volumes, where a "gap" must
  be reported as uncertain rather than asserted.

Nothing here is ever written into the owner's comics folder; this only builds
synthetic files in a folder you name.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURES = _load("longbox_make_fixtures", ROOT / "scripts" / "make_fixtures.py")


def comicinfo(series: str, number: str, volume: str | None = None, year: int | None = None,
              page_count: int = 0, width: int = 1988, height: int = 3056,
              publisher: str = "Marvel") -> str:
    """ComicInfo.xml with explicit page dimensions (make_fixtures fixes them)."""
    lines = ['<?xml version="1.0"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xmlns:xsd="http://www.w3.org/2001/XMLSchema">']

    def add(tag, value):
        if value is not None and value != "":
            lines.append(f"  <{tag}>{escape(str(value))}</{tag}>")

    add("Series", series)
    add("Number", number)
    add("Volume", volume)
    add("Year", year)
    add("Publisher", publisher)
    if page_count:
        add("PageCount", page_count)
        lines.append("  <Pages>")
        for index in range(page_count):
            attrs = [f'Image="{index}"']
            if index == 0:
                attrs += ['Type="FrontCover"']
            attrs += [f'ImageWidth="{width}"', f'ImageHeight="{height}"']
            lines.append("    <Page " + " ".join(attrs) + " />")
        lines.append("  </Pages>")
    lines.append("</ComicInfo>")
    return "\n".join(lines) + "\n"


def corrupt_member(path: Path, member: str) -> None:
    """Make one member unreadable without breaking the archive's directory.

    Listing the archive (what a scan does) still succeeds - reading that one page
    fails.  That is exactly the damage a shallow listing cannot see.
    """
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    data = bytearray(path.read_bytes())
    offset = info.header_offset
    name_length = int.from_bytes(data[offset + 26:offset + 28], "little")
    extra_length = int.from_bytes(data[offset + 28:offset + 30], "little")
    start = offset + 30 + name_length + extra_length
    data[start:start + info.compress_size] = b"\x00" * info.compress_size
    path.write_bytes(bytes(data))


def build(folder: Path, samples: Path = SAMPLES) -> dict[str, list[str] | str]:
    """Build the base fixtures plus the doctor cases; returns the planted paths."""
    folder.mkdir(parents=True, exist_ok=True)
    FIXTURES.build(folder, samples)
    planted: dict[str, list[str] | str] = {}

    # 1. identical duplicate: the same bytes in a second folder
    source = folder / "Saga 12 (2014) (Digital) (Zone-Empire).cbz"
    twin = folder / "Duplicates" / "Saga 12 (2014) (Digital) (Zone-Empire).cbz"
    twin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, twin)
    planted["identical"] = [str(source), str(twin)]

    # 2. lower-quality duplicate of the same issue: same page count, tiny pages
    lowres = folder / "Doctor Cases" / "Ultimate Spider-Man 001 (2000) (low-res webrip).cbz"
    FIXTURES.write_cbz(
        lowres, 5, tiny=True,
        comicinfo=comicinfo("Ultimate Spider-Man", "1", "2000", 2000, page_count=5,
                            width=800, height=1200))
    planted["lowres"] = [str(folder / "Ultimate Spider-Man 001 (2000).cbz"), str(lowres)]

    # 3. a reading-order run with number 3 missing (order number must not become a series)
    order = folder / "Reading Order"
    for position, number in ((1, 1), (2, 2), (3, 4)):
        path = order / f"{position:03d}- Civil War {number}.cbz"
        FIXTURES.write_cbz(path, 3)                      # no ComicInfo, like the real ones
    planted["gap"] = [str(order / f"{p:03d}- Civil War {n}.cbz")
                      for p, n in ((1, 1), (2, 2), (3, 4))]

    # 4. a single-issue series: one file, no gap to report
    solo = folder / "Doctor Cases" / "Solo Run 1 (2015).cbz"
    FIXTURES.write_cbz(solo, 3, comicinfo=comicinfo("Solo Run", "1", "2015", 2015, page_count=3))
    planted["solo"] = str(solo)

    # 5. lists fine, cannot be read: one page's bytes zeroed, directory untouched
    broken = folder / "Doctor Cases" / "Broken Member 01 (2020).cbz"
    FIXTURES.write_cbz(broken, 4, comicinfo=comicinfo("Broken Member", "1", "2020", 2020,
                                                      page_count=4))
    corrupt_member(broken, "page3.jpg")
    planted["truncated"] = str(broken)

    # 6. one series numbered across two volumes: the gap must be reported as uncertain
    for number, volume, year in ((1, "1", 1990), (2, "1", 1990), (3, "1", 1990),
                                 (4, "2", 2016), (5, "2", 2016), (7, "2", 2016)):
        path = folder / "Doctor Cases" / f"Loki {number} ({year}).cbz"
        FIXTURES.write_cbz(path, 3, comicinfo=comicinfo("Loki", str(number), volume, year,
                                                       page_count=3))
    planted["ambiguous"] = [str(folder / "Doctor Cases" / f"Loki {n} ({y}).cbz")
                            for n, y in ((1, 1990), (2, 1990), (3, 1990),
                                         (4, 2016), (5, 2016), (7, 2016))]
    return planted


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    folder = Path(argv[0]).expanduser()
    planted = build(folder)
    print(f"doctor fixtures written to {folder}")
    for name, value in planted.items():
        for path in (value if isinstance(value, list) else [value]):
            print(f"  {name:<10} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
