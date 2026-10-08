# Real ComicInfo.xml samples from the owner's collection

The owner pasted parsed XML from several comics in `C:\Users\jasro\Desktop\comics` (X-Men #11–15, the
2024 Marvel run). `comicinfo_xmen_11.xml` reproduces one of them verbatim as XML (the `<Pages>` list is
trimmed to a few entries; the real file lists all 27).

## Facts that matter for the indexer

1. **His files already carry rich ComicInfo.xml.** Metadata is scraped from ComicVine, including
   `Characters`, `Teams` and `Locations` — each name followed by its ComicVine id in square brackets,
   e.g. `Cyclops [1459]`, `X-Men [3173]`, `Alaska [55827]`. Multi-valued fields are comma-separated.
   This is the raw material for the knowledge-graph phase, and most of it is already on disk.
2. **`<Volume>` is inconsistent and must NOT be used as a volume number.** Across one series:
   X-Men #11 → `2024`, #12 → `158814`, #13 → `158814`, #14 → `2024`, #15 → `158814`.
   `2024` looks like the volume's start year, `158814` looks like the ComicVine volume id.
   Grouping comics by `(Series, Volume)` would split one series into two. Store the raw value, tag it
   (`volume_year` / `volume_id` / `other`), group by series name, and treat volume as a displayed
   disambiguator only.
3. **`<Web>` contains the ComicVine issue id** — `.../x-men-11-live-capture/4000-1095610/` → issue
   `1095610`. `<Notes>` repeats it as `[CVDB1095610]`. Useful as a stable external id for later
   enrichment and duplicate detection.
4. **Page 0 is the front cover** (`Image="0" ... Type="FrontCover"`). Use the cover page for
   thumbnails, not whatever sorts first.
5. **The last page is often junk** — a noticeably smaller image (1080×1529 vs 1988×3056), no `Type`.
   Fine to show, but don't assume uniform page dimensions and don't treat the last page as content.
6. **`Number` is a string** as far as we're concerned (annuals, one-shots, `#1.MU`, decimals).
7. `<Summary>` contains real newlines that must survive into the UI.
8. Some issues have no creator credits (X-Men #14 and #15 have no Writer/Penciller) — every field must
   be optional, and `PageCount` may disagree with the actual number of pages when the last page is junk.

## Fixture requirement

The synthetic fixtures should include one archive whose ComicInfo.xml is a copy of
`comicinfo_xmen_11.xml` and one whose `Volume` is the `158814`-style id, so the mixed-volume case above
is covered by a test instead of being discovered later against the real collection.
