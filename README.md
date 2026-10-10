# Longbox

Your own comic library index. It reads the CBZ/CBR files already in your comics
folder, writes down what is inside them, and serves that as a small local web
address your browser can read. Nothing is uploaded, nothing is changed in your
comics folder, and there is no login.

**What works today:** the index, the data API and the whole browse/read web app —
a cover grid with search and filters, an issue page, and a reader that remembers
your place in every issue. Start at §4 and open <http://127.0.0.1:8765>; §5 walks
through the screens. The JSON API from the same index is documented in §5 too.

Everything happens on your PC. Your comics folder is opened **read-only**: Longbox
never edits, moves, renames or adds files there.

---

## 1. What you need

* **Windows 10 or 11** (or any PC with Python).
* **Python 3.11 or newer** — from <https://www.python.org/downloads/windows/>.
  During installation, tick **"Add python.exe to PATH"**.
* **Optional: a RAR reader for `.cbr` files** — most PCs already have one (see §8),
  and a `.cbr` that is really a zip needs nothing at all. If yours is missing one,
  install **7-Zip** from <https://www.7-zip.org/> (recommended) or **UnRAR** from
  <https://www.rarlab.com/download.htm>. Without either, your `.cbr` files are
  still listed — each one says why it could not be read, and nothing crashes.

Check Python is installed: open **PowerShell** and run

```
python --version
```

You should see something like `Python 3.12.4`.

## 2. Install once

In PowerShell, go to the folder you downloaded this project into and install the
three libraries it needs:

```
cd "C:\Users\jasro\Desktop\longbox"
python -m pip install -r requirements.txt
```

## 3. Build the index

```
python -m longbox.scan --path "C:\Users\jasro\Desktop\comics" --db data\longbox.db
```

This walks the comics folder, reads each archive, and writes everything into
`data\longbox.db` (a single file — that is the whole index). It prints a summary
at the end:

```
Longbox scan summary
  comics folder : C:\Users\jasro\Desktop\comics
  database      : C:\Users\jasro\Desktop\longbox\data\longbox.db
  found         : 14
  indexed (new) : 12
  updated       : 0
  errors        : 2
  missing       : 0
  rows in db    : 14
  elapsed       : 0.02s
```

Any file it could not read is listed underneath with the reason. Run the same
command again whenever you add comics — it updates the existing rows instead of
duplicating them, keeps your reading positions, and marks files you deleted or
moved as "missing".

## 4. Start it

```
python -m longbox.server --path "C:\Users\jasro\Desktop\comics" --db data\longbox.db --port 8765
```

Leave that window open while you use Longbox (close it or press **Ctrl+C** to
stop). It only listens on your own PC — nothing outside can reach it.

## 5. Using the app
Open <http://127.0.0.1:8765> in a browser on your PC. That one page is the whole
app — it has three screens and nothing to install.

**Library (the home screen)** — a grid of cover cards, one per comic, each with
its series, issue number, year, page count and a Read/Unread badge. Hover a card
you have already started and it offers "Continue p.N"; clicking the card goes
straight back to that page.

* The search box filters as you type (it matches series, title, writer and
  character). Series, year, read state and sort order are the dropdowns next to
  it; "File state" narrows the grid to files Longbox could not read.
* Every filter lives in the address bar — for example
  <http://127.0.0.1:8765/#/?q=x-men&read=false&sort=issue> — so refreshing, the
  Back button and bookmarks all keep the filter you had.
* "Load more" fetches the next 60 comics; the whole library is never loaded at
  once, which keeps it quick even with thousands of files.
* The strip along the top shows your totals. If any file failed it shows
  "N unreadable files": click that (or pick "Unreadable files" in File state) to
  see every filename with the reason it failed.
* "Rescan library" re-reads your comics folder without restarting, showing how
  many files it found, added, updated or could not read when it finishes.

**Issue page** — click a cover. It shows the metadata that is already inside the
file: series, issue, title, date, publisher, page count, the full summary with
its line breaks, the creator credits, and chips for the characters, teams and
locations the file records. "Read" (or "Continue from page N" if you have
started it) opens the reader; there is also a Mark as read/unread button and a
link back to the library that keeps your filters. If the file itself is
unreadable, you get the reason here instead of a broken reader.

**Reader** — one comic page, fitted to the window.

* Click the page, or press Space / → / PageDown, for the next page. ← / PageUp
  go back, Home and End jump to the first and last page, Esc returns to the
  issue page.
* The next page is preloaded while you read, so turning the page is instant; a
  slow page shows "Loading page N…" instead of a blank screen.
* Your place is saved by itself (a moment after you stop turning pages, and
  again when you close the tab), so the issue always resumes where you stopped.
* Reaching the last page marks the issue read.
* On the last page, if the next issue of that series is in your library, a
  "Next issue" link appears.
* If a page cannot be read, the reader says which page failed and offers Try
  again or Skip to the next page — one bad page never blocks the reader.

### The same data as JSON
Open these addresses (in a browser, or with `curl` in a second PowerShell window).
127.0.0.1 means "this computer":

* Health check — <http://127.0.0.1:8765/api/health>
* First 20 comics — <http://127.0.0.1:8765/api/comics?limit=20>
* Search — <http://127.0.0.1:8765/api/comics?q=x-men>
* Your series list with counts — <http://127.0.0.1:8765/api/series>
* Totals — <http://127.0.0.1:8765/api/stats>
* The years in your library — <http://127.0.0.1:8765/api/years>
* Only the unreadable files, with the reason — <http://127.0.0.1:8765/api/comics?status=error>
* Only the files that have gone missing — <http://127.0.0.1:8765/api/comics?status=missing>
* One issue in full — <http://127.0.0.1:8765/api/comics/1>
* A cover image — <http://127.0.0.1:8765/api/comics/1/thumbnail>
* Page 1 of an issue — <http://127.0.0.1:8765/api/comics/1/pages/1>

The same thing from PowerShell, if you prefer:

```
curl http://127.0.0.1:8765/api/health
curl "http://127.0.0.1:8765/api/comics?q=x-men&limit=2"
curl http://127.0.0.1:8765/api/stats
curl -X POST "http://127.0.0.1:8765/api/scan"
```

`POST /api/scan` re-reads the comics folder on the spot, so you can add comics and
click/curl that instead of stopping the program.

There is also an **automatic list of every address** at
<http://127.0.0.1:8765/docs>.

## 6. Where your data lives

Everything Longbox creates is inside this project folder:

```
data\longbox.db     the index (one SQLite file - safe to copy, safe to delete and rebuild)
data\thumbs\        small cached cover images, made on demand
```

Deleting `data\` costs you nothing but a re-scan (your reading positions would go
too, since they live in the same file). **Nothing is ever written into your comics
folder** — Longbox will happily run even if that folder is read-only.

## 7. Doing something else

* Different comics folder / index location — just change `--path` and `--db`.
* If you prefer settings over typing flags, these environment variables work
  instead: `LONGBOX_COMICS`, `LONGBOX_DB`, `LONGBOX_PORT`, `LONGBOX_HOST`,
  `LONGBOX_THUMB_DIR`.
* Port already in use? Add `--port 8899` to both commands.
* To keep it private to your PC (the default), don't pass `--host`.

## 8. CBR files: how Longbox reads them

Longbox decides what a file really is from its **first bytes**, never from its
name. So a file named `.cbr` that is really a zip — very common in collections
put together by other tools — is read by Python itself and needs **no extra
software at all**. A reader is only needed when the bytes really are a RAR (or a
7z) archive.

For those, Longbox tries readers **in this order**:

1. `unrar` — WinRAR's `UnRAR.exe` counts
2. `unar` — The Unarchiver
3. bsdtar — Windows 10 (1803+) and Windows 11 ship one at
   `C:\Windows\System32\tar.exe`
4. `7z` — 7-Zip

It looks on your PATH *and* in the usual install folders, so you never have to
add anything to PATH: `C:\Windows\System32\tar.exe`, `C:\Program Files\7-Zip\7z.exe`,
`C:\Program Files\WinRAR\UnRAR.exe`, plus the `Program Files (x86)`,
`ProgramW6432` and `LOCALAPPDATA\Programs` equivalents. The first reader that
really opens your file wins; if one exists but cannot open a particular archive,
the next one in the list is tried, and a reader that fails is not asked again
during that run. Because Windows' own `tar.exe` *is* bsdtar and reads RAR, most
PCs need nothing installed at all.

If your reader lives somewhere unusual, point Longbox straight at it and restart
the server (permanent, in PowerShell):

```
setx LONGBOX_RAR_TOOL "D:\tools\UnRAR.exe"
```

or just for that window:

```
$env:LONGBOX_RAR_TOOL = "D:\tools\UnRAR.exe"
```

**Check a file by hand in PowerShell.** Use one of your real files:

```
tar -tf "C:\Users\jasro\Desktop\comics\Batman 1.cbr"
```

That prints the page names inside the file. If it does not, install 7-Zip from
<https://www.7-zip.org/> and ask it directly:

```
& "C:\Program Files\7-Zip\7z.exe" l "C:\Users\jasro\Desktop\comics\Batman 1.cbr"
```

If neither of them can list the file, the file itself is damaged — that is not
something Longbox can fix, but it will tell you which files those are.

**When no reader is found.** Longbox does not stop, and it does not repeat the
same error 165 times. Every unreadable file gets one row with a short reason, and
the top of the page groups them: *"165 files — this is a RAR file and no RAR tool
was found on this PC. Installing 7-Zip from 7-zip.org fixes it for all your .cbr
files."* Install the tool it names, then click **Rescan library** — no need to
restart or rebuild anything.

Longbox never unpacks your comics next to themselves: where a tool has to write
something to disk it goes to a temporary folder that is deleted straight away,
and your comics folder stays untouched.

## 9. If something looks wrong

* **The "Problems" line names a missing RAR tool** — install the tool it names
  (7-Zip is the easiest), then click **Rescan library**. What the message means:
  the file really is a RAR archive and no program on your PC can unpack it yet.
  Your `.cbz` files are unaffected, and a `.cbr` that is really a zip never needs
  anything installed.
* **"this is not a comic archive: no zip, rar or 7z signature in …"** — that file
  is not an archive at all (a half-finished download, usually). Longbox lists it
  with its name and number so you can find and replace it; it never crashes the
  scan.
* **A comic shows the wrong year** — the year Longbox displays is the one inside the
  file's `ComicInfo.xml` when there is one; the year in the filename is kept too and
  can be seen in the issue detail under `filename_parsed`.
* **Very long paths / "path too long"** — Windows needs long-path support enabled
  (Settings → System → For developers → "Enable Win32 long paths"), or move the
  comics folder closer to the drive root.
* **The scan finds nothing** — check the path is the folder that directly contains
  your `.cbz`/`.cbr` files (`Get-ChildItem "C:\Users\jasro\Desktop\comics"` should
  list them).

## 10. Try it on test comics first

If you want to see it work without touching your real folder, this builds a small
folder of made-up comics and indexes that instead:

```
python scripts\make_fixtures.py testcomics
python -m longbox.scan --path testcomics --db data\test.db
python -m longbox.server --path testcomics --db data\test.db --port 8765
```

---

## 11. Checking your library — broken files, duplicates, missing numbers

The doctor reads the index (never the comics folder) and tells you what is wrong with
your collection. It only reports: it does not move, rename, edit or delete anything,
and it never writes inside your comics folder.

```
python -m longbox.doctor --path "C:\Users\jasro\Desktop\comics" --db data\longbox.db
```

Add `--json` for scripting, `--no-save` to keep the run out of the database, and
`--deep` when you want the slow, thorough pass. The report has three sections:

1. **Unreadable or broken archives** — every file that did not index, grouped by
   reason, so the 165 "no RAR tool" files are one line with a count, not 165
   messages. Each group says what to do (`Install 7-Zip from 7-zip.org…`). Files
   that are simply gone from disk are counted here too.
2. **Duplicate issues** — issues that exist more than once, with each copy's folder,
   size, page count, container and whether it has ComicInfo. You get a verdict:
   *identical copies* (same size and page count — one file saved twice) or *different
   quality* (one copy has bigger page images), and a **suggested** keeper. It is a
   suggestion from the evidence in your files, so look at the copies before you
   remove one — Longbox never deletes anything for you.
3. **Missing issue numbers in a run** — per series, the numbers you have and the gaps,
   e.g. `Civil War: have 1-2, 4 (3 file(s)) - MISSING 3`. Annuals, one-shots and files
   with no (or a non-numeric) issue number are left out of the runs and counted at the
   bottom of the section, and series with fewer than three numbered issues are not
   called runs at all — so a single `Solo Run 1` is not a "missing 2". Reading-order
   filenames (`008- Civil War 1.cbr`) are grouped under the real series, so the order
   number never invents a series or a false gap. Where a series is genuinely ambiguous
   (its numbers span two volumes), the gap is reported as *possibly missing* with the
   reason, instead of being asserted.

**`--deep`** (opt-in, the only slow mode) re-opens every archive and reads *every*
page, which catches what a listing cannot: a page whose data is truncated, an empty
page, or a ComicInfo page count that disagrees with the archive. It prints progress as
it goes and is safe to interrupt with Ctrl-C. It extracts nothing next to your comics
(any tool that needs a file on disk gets a system temp folder, which is removed
again). The default run reads the database only and takes a second or two.

Exit codes: **0** nothing to fix, **1** findings exist, **2** the run could not start
(no comics folder, no index yet), **130** you interrupted `--deep`.
`python -m longbox.doctor --help` lists every flag.

Findings are also saved into two tables in the Longbox database so a later web panel
can show them without re-running the checks: `doctor_runs` (one row: when, where,
how long, counts) and `doctor_findings` (one row per thing to look at: `section`,
`severity`, `kind`, series/issue, a plain-English `summary`, and a JSON `detail` with
the evidence). The tables always hold the most recent run only, so a fixed problem
does not keep showing up.

Try the whole thing on the throwaway fixtures before your real folder:

```
python scripts\make_doctor_fixtures.py testdoctor
python -m longbox.scan --path testdoctor --db data\testdoctor.db
python -m longbox.doctor --path testdoctor --db data\testdoctor.db
python -m longbox.doctor --path testdoctor --db data\testdoctor.db --deep
```

---

## For whoever works on this next

* Tests: `python -m pytest` (the whole suite, uses throwaway fixtures built by
  `scripts/make_fixtures.py`, never your real folder).
* `samples/` holds a real `ComicInfo.xml` from the collection plus `NOTES.md`, the
  eight facts the parser and tests are built around.
* Layout: `longbox/parser.py` (filenames), `longbox/comicinfo.py` (XML),
  `longbox/archive.py` (read-only CBZ/CBR + thumbnails), `longbox/db.py` (SQLite),
  `longbox/scanner.py` + `longbox/scan.py` (indexing CLI), `longbox/doctor.py`
  (integrity, duplicates, gaps + its own CLI),
  `longbox/server.py` (web UI + JSON API CLI), `longbox/static/` (the UI:
  `index.html`, `app.js`, `styles.css` — plain files, no build step, no CDN, served
  at `/`). The doctor's fixtures live in `scripts/make_doctor_fixtures.py`.
* Known rough edge: the filename parser keeps a leading reading-order number inside
  the series name (`008- Civil War 1.cbr` → series `008- Civil War`); the doctor
  strips it when grouping, but a re-scan of the owner's reading-order folders would
  be cleaner if the parser did it too.
* Not built yet, in order: Docker
  Compose with a read-only comics mount, the problems/duplicates panel on top of
  `doctor_findings`, the metadata graph over the embedded
  `Characters`/`Teams`/`Locations`, panel extractor, then
  the timeline / character-network / "where am I?" views.
