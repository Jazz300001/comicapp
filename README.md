# Longbox

Your own comic library index. It reads the CBZ/CBR files already in your comics
folder, writes down what is inside them, and serves that as a small local web
address your browser can read. Nothing is uploaded, nothing is changed in your
comics folder, and there is no login.

**What works today (step 1 of the project):** the index and the data API. You can
build the index, start the program, and read the library as data in your browser.
**The browse/read pages themselves are the next step** — for now the browser shows
JSON, not a grid of covers.

Everything happens on your PC. Your comics folder is opened **read-only**: Longbox
never edits, moves, renames or adds files there.

---

## 1. What you need

* **Windows 10 or 11** (or any PC with Python).
* **Python 3.11 or newer** — from <https://www.python.org/downloads/windows/>.
  During installation, tick **"Add python.exe to PATH"**.
* **Optional but recommended: UnRAR**, needed only for `.cbr` files
  (<https://www.rarlab.com/download.htm>, the "UnRAR for Windows" link). Put
  `UnRAR.exe` somewhere on your PATH. Without it, `.cbr` files are still listed,
  but marked as an error — see Troubleshooting.

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

## 8. If something looks wrong

* **Lists of `.cbr` files with "unrar not installed — CBR not readable"** — install
  UnRAR (step 1) and re-scan. Your `.cbz` files do not need it.
* **"corrupt zip (not a readable CBZ): File is not a zip file"** — that file is not
  actually a working CBZ (a half-finished download, usually). Longbox lists it with
  its name and number so you can find and replace it; it never crashes the scan.
* **A comic shows the wrong year** — the year Longbox displays is the one inside the
  file's `ComicInfo.xml` when there is one; the year in the filename is kept too and
  can be seen in the issue detail under `filename_parsed`.
* **Very long paths / "path too long"** — Windows needs long-path support enabled
  (Settings → System → For developers → "Enable Win32 long paths"), or move the
  comics folder closer to the drive root.
* **The scan finds nothing** — check the path is the folder that directly contains
  your `.cbz`/`.cbr` files (`Get-ChildItem "C:\Users\jasro\Desktop\comics"` should
  list them).

## 9. Try it on test comics first

If you want to see it work without touching your real folder, this builds a small
folder of made-up comics and indexes that instead:

```
python scripts\make_fixtures.py testcomics
python -m longbox.scan --path testcomics --db data\test.db
python -m longbox.server --path testcomics --db data\test.db --port 8765
```

---

## For whoever works on this next

* Tests: `python -m pytest` (89 tests, uses throwaway fixtures built by
  `scripts/make_fixtures.py`, never your real folder).
* `samples/` holds a real `ComicInfo.xml` from the collection plus `NOTES.md`, the
  eight facts the parser and tests are built around.
* Layout: `longbox/parser.py` (filenames), `longbox/comicinfo.py` (XML),
  `longbox/archive.py` (read-only CBZ/CBR + thumbnails), `longbox/db.py` (SQLite),
  `longbox/scanner.py` + `longbox/scan.py` (indexing CLI), `longbox/server.py`
  (web UI + JSON API CLI), `longbox/static/` (the UI: `index.html`, `app.js`,
  `styles.css` — plain files, no build step, no CDN, served at `/`).
* Not built yet, in order: Docker
  Compose with a read-only comics mount, integrity/duplicate checker, the metadata
  graph over the embedded `Characters`/`Teams`/`Locations`, panel extractor, then
  the timeline / character-network / "where am I?" views.
