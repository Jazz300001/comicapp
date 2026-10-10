#!/usr/bin/env python3
"""Build synthetic comic fixtures:  python scripts/make_fixtures.py <folder>

These stand in for the owner's real collection (which we cannot read).  They
cover the awkward cases on purpose: names with release-group tags, underscores,
unicode and a year, an annual, a one-shot with no issue number, a series whose
`<Volume>` is a year in some issues and a ComicVine id in others, 10- and
27-page archives (page ordering), a corner no-ComicInfo file, a corrupt .cbz, a
zip renamed .cbr, and a nested subfolder with a space in its name.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

#: real RAR4 magic bytes, used by the signature-only fixture in build()
RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"

PAGE_WIDTH, PAGE_HEIGHT = 300, 450
SMALL_PAGE_SIZE = (100, 150)
JUNK_PAGE_SIZE = (160, 226)


def page_jpeg(number: int, size: tuple[int, int] = (PAGE_WIDTH, PAGE_HEIGHT)) -> bytes:
    """A page with its number drawn on it, so page order is provable."""
    hue = (number * 37) % 200
    image = Image.new("RGB", size, (245, 244 - hue // 4, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle([2, 2, size[0] - 3, size[1] - 3], outline=(30, 30, 30), width=2)
    try:
        from PIL import ImageFont

        font = ImageFont.load_default(size=max(18, size[1] // 4))
    except Exception:  # pragma: no cover - very old Pillow
        font = None
    draw.text((size[0] // 2, size[1] // 2), str(number), fill=(20, 20, 20),
              font=font, anchor="mm")
    draw.text((10, 10), f"page {number}", fill=(90, 90, 90), font=None)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


def build_comicinfo(series: str, number: str | None = None, volume: str | None = None,
                    year: int | None = None, month: int | None = None, day: int | None = None,
                    title: str | None = None, publisher: str | None = None,
                    summary: str | None = None, notes: str | None = None,
                    web: str | None = None, writers: tuple = (), pencillers: tuple = (),
                    characters: tuple = (), teams: tuple = (), locations: tuple = (),
                    editors: tuple = (), page_count: int = 0) -> str:
    """Minimal but realistic ComicInfo.xml, with a Pages list like the real ones."""
    lines = ['<?xml version="1.0"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xmlns:xsd="http://www.w3.org/2001/XMLSchema">']
    # emits ComicInfo's own field order/style, close to the real files
    def add(tag, value):
        if value is not None and value != "":
            lines.append(f"  <{tag}>{escape(str(value))}</{tag}>")

    add("Title", title)
    add("Series", series)
    add("Number", number)
    add("Volume", volume)
    add("Summary", summary)
    add("Notes", notes)
    add("Year", year)
    add("Month", month)
    add("Day", day)
    if writers:
        add("Writer", ", ".join(writers))
    if pencillers:
        add("Penciller", ", ".join(pencillers))
    if editors:
        add("Editor", ", ".join(editors))
    add("Publisher", publisher)
    add("Web", web)
    if page_count:
        add("PageCount", page_count)
    for tag, values in (("Characters", characters), ("Teams", teams), ("Locations", locations)):
        if values:
            add(tag, ", ".join(values))
    if page_count:
        lines.append("  <Pages>")
        for index in range(page_count):
            attrs = [f'Image="{index}"']
            if index == 0:
                attrs += [f'ImageSize="{3472085 + index}"', 'ImageWidth="1988"',
                          'ImageHeight="3056"', 'Type="FrontCover"']
            elif index == page_count - 1:
                # the owner's files often end with one smaller junk page
                attrs += ['ImageSize="575709"', f'ImageWidth="{JUNK_PAGE_SIZE[0]}"',
                          f'ImageHeight="{JUNK_PAGE_SIZE[1]}"']
            else:
                attrs += [f'ImageSize="{2026451 + index}"', 'ImageWidth="1988"',
                          'ImageHeight="3056"']
            lines.append("    <Page " + " ".join(attrs) + " />")
        lines.append("  </Pages>")
    lines.append("</ComicInfo>")
    return "\n".join(lines) + "\n"


def write_cbz(path: Path, page_count: int, page_names: list[str] | None = None,
              comicinfo: str | None = None, tiny: bool = False,
              extra_files: dict[str, bytes] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    size = SMALL_PAGE_SIZE if tiny else (PAGE_WIDTH, PAGE_HEIGHT)
    names = page_names or [f"page{i}.jpg" for i in range(1, page_count + 1)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if comicinfo is not None:
            archive.writestr("ComicInfo.xml", comicinfo)
        for index, name in enumerate(names, start=1):
            page_size = JUNK_PAGE_SIZE if index == page_count else size
            archive.writestr(name, page_jpeg(index, page_size))
        for name, payload in (extra_files or {}).items():
            archive.writestr(name, payload)


def build(folder: Path, samples: Path = SAMPLES) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    real_xml = (samples / "comicinfo_xmen_11.xml").read_text(encoding="utf-8")

    def record(path: Path) -> None:
        written.append(str(path))

    # 1-2. plain, well-named CBZs with ComicInfo
    p = folder / "Ultimate Spider-Man 001 (2000).cbz"
    write_cbz(p, 5, comicinfo=build_comicinfo(
        "Ultimate Spider-Man", "1", "2000", 2000, 10, publisher="Marvel",
        title="Powerless", writers=("Brian Michael Bendis",), pencillers=("Mark Bagley",),
        characters=("Peter Parker",), teams=(), locations=("New York State [56310]",),
        summary="Peter Parker gets bitten by a radioactive spider.\n\nA new life begins.",
        page_count=5))
    record(p)

    p = folder / "Ultimate Spider-Man 002 (2000).cbz"
    write_cbz(p, 4, comicinfo=build_comicinfo(
        "Ultimate Spider-Man", "2", "2000", 2000, 11, publisher="Marvel",
        writers=("Brian Michael Bendis",), page_count=4))
    record(p)

    # 3. the real 27-page ComicInfo.xml from the owner's collection, copied verbatim.
    #    Page images are named page1..page27 so natural ordering is exercised too.
    p = folder / "X-Men 011 (2025) (digital) (Marika-Empire).cbz"
    write_cbz(p, 27, page_names=[f"page{i}.jpg" for i in range(1, 28)],
              comicinfo=real_xml, tiny=True,
              extra_files={"__MACOSX/._page1.jpg": b"junk", "readme.txt": b"not a page\n"})
    record(p)

    # 4-6. the mixed <Volume> case: 2024 (volume start year) and 158814 (ComicVine id)
    p = folder / "X-Men 012 (2025) (digital) (Marika-Empire).cbz"
    write_cbz(p, 4, comicinfo=build_comicinfo(
        "X-Men", "12", "158814", 2025, 5, publisher="Marvel", title="Cold Front",
        summary="The hunt for the fugitive continues.", page_count=4))
    record(p)

    p = folder / "X-Men 013 (2025) (digital).cbz"
    write_cbz(p, 5, comicinfo=build_comicinfo(
        "X-Men", "13", "158814", 2025, 6, publisher="Marvel",
        notes="Scraped metadata from ComicVine [CVDB1095612].", page_count=5))
    record(p)

    p = folder / "X-Men 014 (2025).cbz"
    # no creator credits at all - every field optional, must not error
    write_cbz(p, 3, comicinfo=build_comicinfo("X-Men", "14", "2024", 2025, 7,
                                              publisher="Marvel", page_count=3))
    record(p)

    # 7. annual
    p = folder / "X-Men Annual 3 (1990).cbz"
    write_cbz(p, 4, comicinfo=build_comicinfo(
        "X-Men Annual", "3", "1990", 1990, publisher="Marvel",
        writers=("Chris Claremont",), page_count=4))
    record(p)

    # 8-9. hash issue with a year + digital tag; and a .cbr-named archive (no rar tool
    #      exists here, so it is expected to land as an error row - see README)
    p = folder / "Batman #404 (1987) (digital).cbz"
    write_cbz(p, 5, comicinfo=build_comicinfo(
        "Batman", "404", "1987", 1987, 2, publisher="DC Comics", title="Year One, Part One",
        writers=("Frank Miller",), pencillers=("David Mazzucchelli",),
        characters=("Bruce Wayne [1463]", "James Gordon [1462]"),
        locations=("Gotham City [2540111]",), page_count=5))
    record(p)

    p = folder / "Batman #405 (1987) (digital).cbr"
    # Named .cbr but the magic bytes are PK: a zip renamed .cbr, which is common in
    # the wild and is read by the standard library with NO external tool at all.
    write_cbz(p, 3)
    record(p)

    # 10. 10 pages named 1.jpg..10.jpg -> proves natural page ordering (not lexicographic)
    p = folder / "Saga 12 (2014) (Digital) (Zone-Empire).cbz"
    write_cbz(p, 10, page_names=[f"{i}.jpg" for i in range(1, 11)],
              comicinfo=build_comicinfo(
                  "Saga", "12", "2014", 2014, 5, publisher="Image Comics",
                  writers=("Brian K. Vaughan",), pencillers=("Fiona Staples",),
                  characters=("Alana [1001]", "Marko [1002]", "Hazel [1003]"),
                  teams=("Wreath [2001]",), locations=("Cleave [3001]",),
                  page_count=10))
    record(p)

    # 11. unicode + underscored noise tags, no ComicInfo at all
    p = folder / "Bêtes du Nord Töme 3 (2019) (French) [c2c].cbz"
    write_cbz(p, 3)
    record(p)

    # 12. a one-shot: no issue number in the filename -> issue_number is null
    p = folder / "Locke & Key One Shot (2011).cbz"
    write_cbz(p, 3, comicinfo=build_comicinfo("Locke & Key One Shot", "1", "2011", 2011,
                                              publisher="IDW Publishing", page_count=3))
    record(p)

    # 13. nested subfolder with a space, series name containing digits.
    #     ComicInfo deliberately disagrees with the filename year (1993 vs (1992))
    #     so the "keep both" rule is provable by a test.
    p = folder / "Nested Dir" / "Sub Folder" / "Spider-Man 2099 5 (1992).cbz"
    write_cbz(p, 6, comicinfo=build_comicinfo(
        "Spider-Man 2099", "5", "1992", 1993, 3, publisher="Marvel",
        writers=("Peter David",), characters=("Miguel O'Hara [1024]",), page_count=6))
    record(p)

    # 14. deliberately corrupt: plain text with a .cbz extension
    p = folder / "Broken Scan 04 (2015).cbz"
    p.write_bytes(b"this is not a zip file at all\n" * 20)
    record(p)

    # 15. RAR signature only. The bytes start with the real RAR4 magic, which is all
    #     the scanner needs to classify it as a RAR container; there is no RAR
    #     compressor available in this environment, so the payload is padding and
    #     NO tool can list it. It exists to prove the "no RAR tool on this PC" path:
    #     the row is kept, with one short actionable reason, not an empty issue.
    p = folder / "RAR Signature Only 1 (2025).cbr"
    p.write_bytes(RAR4_SIGNATURE + bytes(64))
    record(p)

    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    folder = Path(argv[0]).expanduser()
    samples = Path(argv[1]) if len(argv) > 1 else SAMPLES
    written = build(folder, samples)
    print(f"fixtures written to {folder}")
    total = 0
    for path in written:
        size = os.path.getsize(path)
        total += size
        print(f"  {size:>7,} bytes  {os.path.relpath(path, folder)}")
    print(f"{len(written)} archives, {total:,} bytes total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

