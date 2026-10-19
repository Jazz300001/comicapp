/* Longbox web UI.
 *
 * Plain vanilla JavaScript: one file, no build step, no libraries, no external
 * requests - everything it talks to is the local Longbox API on the same
 * origin.  Screens live in the URL hash so refresh, the back button and
 * bookmarks all work:
 *
 *   #/                        library (filters are in the hash too)
 *   #/comics/{id}             issue detail
 *   #/comics/{id}/page/{n}    reader
 */
"use strict";

const PAGE_SIZE = 60;
const PROGRESS_DEBOUNCE_MS = 1200;
/* Reading takes the whole window: the class below hides the library chrome
   (topbar + filter bar) and zeroes the page padding while an issue is open. */
const READER_BODY_CLASS = "reader-open";
/* A page smaller than the stage is grown to fit it, but a thumbnail-sized scan
   must not be blown up into mush. */
const MAX_PAGE_UPSCALE = 3;
/* .stage has 8px of padding on each side; the page is fitted inside that. */
const STAGE_PADDING = 16;

const viewEl = document.getElementById("view");
const controlsEl = document.getElementById("library-controls");
const statsEl = document.getElementById("stats");
const rescanBtn = document.getElementById("rescan");
const toastEl = document.getElementById("toast");

const qEl = document.getElementById("q");
const seriesEl = document.getElementById("series");
const yearEl = document.getElementById("year");
const readEl = document.getElementById("read");
const statusEl = document.getElementById("status");
const sortEl = document.getElementById("sort");
const clearEl = document.getElementById("clear-filters");

const SORT_VALUES = ["series", "issue", "year", "title", "added", "indexed"];
const CREATOR_ROLES = [
  ["writers", "Writers"],
  ["pencillers", "Pencillers"],
  ["inkers", "Inkers"],
  ["colorists", "Colourists"],
  ["letterers", "Letterers"],
  ["cover_artists", "Cover artists"],
  ["editors", "Editors"],
];

/* --------------------------------------------------------------- utilities */

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : (many || one + "s")}`;
}

async function api(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("Cannot reach the Longbox server. Is it still running?");
  }
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch (err) { body = null; }
  }
  if (!response.ok) {
    const detail = body && (body.detail || body.message);
    throw new Error(typeof detail === "string" && detail
      ? detail
      : `The server answered ${response.status} ${response.statusText}`);
  }
  if (body === null) throw new Error("The server sent a reply Longbox could not read.");
  return body;
}

let toastTimer = null;
function toast(message, kind) {
  toastEl.textContent = message;
  toastEl.className = "toast" + (kind ? ` toast-${kind}` : "");
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 8000);
}

function notice(kind, title, body, actions) {
  return `<div class="notice notice-${kind}"><h2>${esc(title)}</h2>` +
    (body ? `<p>${esc(body)}</p>` : "") +
    (actions || "") + "</div>";
}

function errorPanel(title, message) {
  return notice("error", title, message);
}

/* ----------------------------------------------------------------- routing */

function parseRoute() {
  const raw = (location.hash || "#/").replace(/^#/, "");
  const question = raw.indexOf("?");
  const path = question === -1 ? raw : raw.slice(0, question);
  const params = new URLSearchParams(question === -1 ? "" : raw.slice(question + 1));
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "comics" && parts[1]) {
    const id = Number(parts[1]);
    if (!Number.isFinite(id) || id <= 0) return { name: "notfound" };
    if (parts[2] === "page" && parts[3]) {
      const page = Number(parts[3]);
      if (Number.isFinite(page) && page > 0) return { name: "reader", id, page };
    }
    return { name: "detail", id };
  }
  return { name: "library", params };
}

/* ----------------------------------------------------------- library state */

const library = {
  q: "", series: "", year: "", read: "", status: "", sort: "series",
  items: [], total: 0, loading: false, loaded: false, error: null,
};

let libraryRequest = 0;

function libraryParams() {
  const params = new URLSearchParams();
  if (library.q) params.set("q", library.q);
  if (library.series) params.set("series", library.series);
  if (library.year) params.set("year", library.year);
  if (library.read) params.set("read", library.read);
  if (library.status) params.set("status", library.status);
  if (library.sort && library.sort !== "series") params.set("sort", library.sort);
  return params;
}

function libraryHash() {
  const query = libraryParams().toString();
  return query ? `#/?${query}` : "#/";
}

function filtersActive() {
  return Boolean(library.q || library.series || library.year || library.read ||
                 library.status || (library.sort && library.sort !== "series"));
}

function readLibraryFromParams(params) {
  library.q = params.get("q") || "";
  library.series = params.get("series") || "";
  library.year = params.get("year") || "";
  library.read = params.get("read") || "";
  library.status = params.get("status") || "";
  const sort = params.get("sort") || "series";
  library.sort = SORT_VALUES.indexOf(sort) === -1 ? "series" : sort;
}

function applyFilters(patch) {
  Object.assign(library, patch);
  const hash = libraryHash();
  if (hash === location.hash) {
    // Same place in the URL (e.g. only whitespace changed): re-render directly,
    // because assigning an identical hash fires no hashchange event.
    renderRoute();
  } else {
    location.hash = hash;
  }
}

function syncControls() {
  if (document.activeElement !== qEl) qEl.value = library.q;
  seriesEl.value = library.series;
  yearEl.value = library.year;
  readEl.value = library.read;
  statusEl.value = library.status;
  sortEl.value = library.sort;
}

/* ------------------------------------------------------------ doctor panel */

/* What the last `python -m longbox.doctor` run found.  The findings are read
   from the database the CLI wrote; the page never runs a check itself and no
   button here can change a comic file. */
let lastDoctor = null;

async function loadDoctor() {
  try {
    lastDoctor = await api("/api/doctor");
  } catch (err) {
    lastDoctor = { error: err.message };
  }
  return lastDoctor;
}

function stampLabel(value) {
  if (!value) return "an unknown time";
  const when = new Date(value.replace(" ", "T"));
  if (isNaN(when.getTime())) return value;
  return when.toLocaleString();
}

function doctorFileList(finding) {
  if (!finding.files || !finding.files.length) return "";
  const rows = finding.files.map((file) => {
    const facts = [];
    if (file.file_size_human) facts.push(file.file_size_human);
    if (file.page_count) facts.push(`${file.page_count} pages`);
    if (file.container) facts.push(file.container);
    if (file.dimensions && file.dimensions.length === 2) {
      facts.push(`${file.dimensions[0]}x${file.dimensions[1]}`);
    }
    if (file.status && file.status !== "ok") {
      facts.push(`UNREADABLE (${file.error_kind || "no reason recorded"})`);
    }
    return `<li class="problem">
  <span class="problem-name">${esc(file.filename || file.path || "")}` +
    `${file.keeper ? ' <span class="keeper">the doctor would keep this copy</span>' : ""}</span>
  ${file.folder ? `<span class="problem-path">${esc(file.folder)}</span>` : ""}
  ${facts.length ? `<span class="problem-path">${esc(facts.join(" | "))}</span>` : ""}
</li>`;
  }).join("");
  const more = finding.files_total > finding.files.length
    ? `<li class="problem-path">… and ${finding.files_total - finding.files.length} more</li>` : "";
  const label = finding.files_total === 1 ? "Show the file" : `Show the ${finding.files_total} files`;
  return `<details><summary>${esc(label)}</summary>
    <ul class="problem-list">${rows}${more}</ul></details>`;
}

function doctorFinding(finding) {
  const where = finding.series
    ? `<span class="problem-path">${esc(finding.series)}` +
      `${finding.issue_number ? ` #${esc(finding.issue_number)}` : ""}</span>`
    : "";
  const lines = [
    finding.verdict ? `Verdict: ${finding.verdict}` : "",
    finding.suggestion ? `The doctor suggests: ${finding.suggestion}` : "",
    finding.fix || "",
    finding.note ? `Careful: ${finding.note}` : "",
  ].filter(Boolean).map((line) => `<div class="problem-fix">${esc(line)}</div>`).join("");
  return `<li class="problem doctor-finding">
  <span class="problem-name">${esc(finding.summary)}</span>
  ${where}${lines}${doctorFileList(finding)}
</li>`;
}

function doctorSection(section) {
  return `<div class="problem-group doctor-section">
  <div class="problem-head"><strong>${section.count}</strong> ` +
    `${section.count === 1 ? "finding" : "findings"} &mdash; ${esc(section.label)}</div>
  <details><summary>Show what the doctor found</summary>
    <ul class="problem-list">${section.findings.map(doctorFinding).join("")}</ul></details>
</div>`;
}

function doctorPanel() {
  if (!lastDoctor) return "";
  const head = '<h2>Library doctor</h2>';
  if (lastDoctor.error) {
    return `<section class="problems doctor">${head}
  <p class="muted small">Could not read the doctor's findings: ${esc(lastDoctor.error)}</p>
</section>`;
  }
  if (!lastDoctor.has_run) {
    return `<section class="problems doctor">${head}
  <p class="muted small">No check has run yet &mdash; on your PC run
  <code>${esc(lastDoctor.command)}</code> (add <code>--deep</code> to read every page of every
  archive), then reload this page. ${esc(lastDoctor.note)}</p>
</section>`;
  }
  const run = lastDoctor.run || {};
  const kind = run.deep ? "deep check, every page was read" : "quick check of what is indexed";
  const refresh = run.deep ? lastDoctor.deep_command : lastDoctor.command;
  const summary = lastDoctor.sections.length
    ? lastDoctor.sections.map(doctorSection).join("")
    : '<p class="muted small">Nothing wrong: every indexed archive opened, no issue has two ' +
      'files and no numbered run has a gap.</p>';
  return `<section class="problems doctor">${head}
  <p class="muted small">Last run ${esc(stampLabel(run.finished_at))} &middot;
  ${plural(lastDoctor.total, "finding")} &middot; ${esc(kind)}.<br>
  To check again, run <code>${esc(refresh)}</code> on your PC, then reload this page.
  ${esc(lastDoctor.note)}</p>
  ${summary}</section>`;
}

/* ------------------------------------------------------------- stats strip */

let lastStats = null;

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    lastStats = stats;
    renderStats(stats);
  } catch (err) {
    statsEl.innerHTML = `<span class="stat-chip error">${esc(err.message)}</span>`;
  }
}

function renderStats(stats) {
  const chips = [
    `<span class="stat-chip">${plural(stats.total, "comic")}</span>`,
    `<span class="stat-chip">${plural(stats.series, "series", "series")}</span>`,
    `<span class="stat-chip">${stats.unread} unread</span>`,
    `<span class="stat-chip">${plural(stats.pages, "page")}</span>`,
  ];
  if (stats.in_progress) {
    chips.push(`<span class="stat-chip">${stats.in_progress} in progress</span>`);
  }
  if (stats.errors) {
    chips.push(`<button type="button" class="stat-chip error" data-status="error">` +
      `${plural(stats.errors, "unreadable file")} &rsaquo;</button>`);
  }
  if (stats.missing) {
    chips.push(`<button type="button" class="stat-chip warn" data-status="missing">` +
      `${plural(stats.missing, "missing file")} &rsaquo;</button>`);
  }
  if (stats.metadata_problems) {
    chips.push(`<span class="stat-chip warn">${stats.metadata_problems} with bad metadata</span>`);
  }
  statsEl.innerHTML = chips.join("");
}

/* ---------------------------------------------------------------- rescaning */

async function rescan() {
  if (rescanBtn.disabled) return;
  rescanBtn.disabled = true;
  rescanBtn.classList.add("busy");
  rescanBtn.textContent = "Rescanning…";
  try {
    const summary = await api("/api/scan", { method: "POST" });
    const lines = [
      `Scan finished in ${summary.elapsed}s.`,
      `Found ${summary.found} files: ${summary.indexed} new, ${summary.updated} updated, ` +
        `${summary.errors} unreadable, ${summary.missing} missing.`,
      `The library now holds ${plural(summary.total_in_db, "comic")}.`,
    ];
    (summary.error_files || []).slice(0, 5).forEach((entry) => {
      const file = Array.isArray(entry) ? entry[0] : entry;
      const reason = Array.isArray(entry) ? entry[1] : "";
      lines.push(`• ${file}${reason ? " — " + reason : ""}`);
    });
    toast(lines.join("\n"), summary.errors ? "error" : "ok");
    await loadStats();
    loadFilterOptions();
    await renderRoute();
  } catch (err) {
    toast(`Rescan failed: ${err.message}`, "error");
  } finally {
    rescanBtn.disabled = false;
    rescanBtn.classList.remove("busy");
    rescanBtn.textContent = "Rescan library";
  }
}

/* --------------------------------------------------------- filter dropdowns */

async function loadFilterOptions() {
  try {
    const [series, years] = await Promise.all([api("/api/series"), api("/api/years")]);
    seriesEl.innerHTML = ['<option value="">All series</option>'].concat(
      series.items.map((item) =>
        `<option value="${esc(item.series || item.series_key)}">` +
        `${esc(item.series || item.series_key)} (${item.count})</option>`)
    ).join("");
    yearEl.innerHTML = ['<option value="">All years</option>'].concat(
      years.items.map((item) => `<option value="${item.year}">${item.year} (${item.count})</option>`)
    ).join("");
  } catch (err) {
    toast(`Could not load the filter lists: ${err.message}`, "error");
  }
}

/* ------------------------------------------------------------------ library */

async function renderRoute() {
  const route = parseRoute();
  controlsEl.hidden = route.name !== "library";
  setReader(null);
  /* the reader owns the whole window the moment the address says so, so the
     chrome never flashes back in while the issue is being fetched; renderReader
     hands the window back if the issue cannot be opened. */
  setReaderMode(route.name === "reader");
  if (route.name === "library") {
    readLibraryFromParams(route.params);
    sessionStorage.setItem("longbox.library", libraryHash());
    syncControls();
    /* the doctor's findings are fetched before the grid so the panel is in the
       first paint, and re-fetched on every visit: if the owner runs the CLI in
       another window, coming back to the library shows the new result. */
    await loadDoctor();
    await loadLibrary();
  } else if (route.name === "detail") {
    await renderDetail(route.id);
  } else if (route.name === "reader") {
    await renderReader(route.id, route.page);
  } else {
    viewEl.innerHTML = errorPanel("That address makes no sense",
      "Use the library to pick a comic.") + backLink();
  }
}

function backLink(text) {
  return `<p><a href="${esc(sessionStorage.getItem("longbox.library") || "#/")}">` +
    `&larr; ${esc(text || "Back to library")}</a></p>`;
}

async function loadLibrary() {
  const params = libraryParams();
  const want = Math.min(500, Math.max(PAGE_SIZE, library.items.length));
  params.set("limit", String(want));
  params.set("offset", "0");
  library.loading = true;
  library.error = null;
  paintLibrary();
  const token = ++libraryRequest;
  try {
    const data = await api(`/api/comics?${params.toString()}`);
    if (token !== libraryRequest) return;
    library.items = data.items;
    library.total = data.total;
    library.loaded = true;
  } catch (err) {
    if (token !== libraryRequest) return;
    library.error = err.message;
    library.items = [];
    library.total = 0;
  }
  library.loading = false;
  paintLibrary();
}

async function loadMore() {
  if (library.loading) return;
  const params = libraryParams();
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(library.items.length));
  library.loading = true;
  paintLibrary();
  const token = ++libraryRequest;
  try {
    const data = await api(`/api/comics?${params.toString()}`);
    if (token !== libraryRequest) return;
    const seen = {};
    library.items.forEach((item) => { seen[item.id] = true; });
    library.items = library.items.concat(data.items.filter((item) => !seen[item.id]));
    library.total = data.total;
  } catch (err) {
    if (token !== libraryRequest) return;
    toast(`Could not load more comics: ${err.message}`, "error");
  }
  library.loading = false;
  paintLibrary();
}

function issueLabel(item) {
  if (item.issue_number) return `#${item.issue_number}`;
  return item.issue_sort === null || item.issue_sort === undefined ? "" : `#${item.issue_sort}`;
}

function coverCard(item) {
  const resume = item.last_page > 0;
  const href = resume ? `#/comics/${item.id}/page/${item.last_page}`
                      : `#/comics/${item.id}`;
  const badges = [];
  if (item.status === "error") {
    badges.push('<span class="badge badge-error">Unreadable</span>');
  } else if (item.status === "missing") {
    badges.push('<span class="badge badge-missing">Missing</span>');
  }
  badges.push(item.read
    ? '<span class="badge badge-read">Read</span>'
    : '<span class="badge badge-unread">Unread</span>');

  const fallback = item.status === "ok" ? "No cover image" : "No cover (file problem)";
  const pages = item.page_count ? plural(item.page_count, "page") : "pages unknown";
  const year = item.year ? String(item.year) : "year unknown";

  return `<article class="card">
  <a class="card-cover" href="${esc(href)}" title="${esc((item.series || "Untitled") + " " + issueLabel(item))}">
    <div class="cover-fallback">${esc(fallback)}</div>
    <img class="cover-img" loading="lazy" alt="" src="${esc(item.thumb_url)}">
    <div class="card-badges">${badges.join("")}</div>
    ${resume ? `<span class="card-continue">Continue p.${item.last_page}</span>` : ""}
  </a>
  <div class="card-meta">
    <div class="card-series" title="${esc(item.series || item.filename || "")}">${esc(item.series || "Unknown series")}</div>
    <div class="card-line"><span class="card-issue">${esc(issueLabel(item))}</span><span>${esc(year)}</span></div>
    <div class="card-title" title="${esc(item.title || "")}">${esc(item.title || pages)}</div>
  </div>
</article>`;
}

/* Everything that failed to index, grouped by reason: a collection that is mostly
   .cbr on a PC with no RAR tool has hundreds of rows with the same sentence, and
   repeating it 165 times buries the one thing the owner has to do. */
function problemsPanel() {
  const missingView = library.status === "missing";
  const label = missingView ? "Missing files" : "Unreadable files";
  const items = library.items;
  if (!items.length) return "";
  const groups = new Map();
  items.forEach((item) => {
    const kind = item.error_kind ||
      (item.status === "missing" ? "file_missing" : "unknown");
    const reason = item.error_message || (item.status === "missing"
      ? "file no longer present at this path" : "no reason recorded");
    const key = `${kind}\u0000${reason}`;
    if (!groups.has(key)) {
      const hint = (lastStats && lastStats.errors_by_kind || [])
        .find((entry) => entry.kind === kind);
      groups.set(key, { kind, reason, fix: (hint && hint.fix) || "", files: [] });
    }
    groups.get(key).files.push(item);
  });
  const ordered = [...groups.values()].sort((a, b) => b.files.length - a.files.length);
  const blocks = ordered.map((group) => {
    const names = group.files.slice(0, 400).map((item) => `<li class="problem">
  <span class="problem-name">${esc(item.filename || item.path || "")}</span>
  <span class="problem-path">${esc(item.path || "")}</span>
</li>`).join("");
    const more = group.files.length > 400
      ? `<li class="problem-path">… and ${group.files.length - 400} more</li>` : "";
    return `<div class="problem-group">
  <div class="problem-head"><strong>${group.files.length}</strong> ${esc(
      group.files.length === 1 ? "file" : "files")} &mdash; ${esc(group.reason)}</div>
  ${group.fix ? `<div class="problem-fix">${esc(group.fix)}</div>` : ""}
  <details><summary>Show the file names</summary><ul class="problem-list">${names}${more}</ul></details>
</div>`;
  }).join("");
  return `<section class="problems"><h2>${esc(label)} (${items.length})</h2>${blocks}</section>`;
}

/* What the file *really* is, from its magic bytes - a .cbr that is really a zip
   opens with no external tool at all, and the owner should see that. */
function containerLabel(comic) {
  const names = { zip: "ZIP", rar4: "RAR (v4)", rar5: "RAR (v5)", "7z": "7z" };
  const container = names[comic.archive_container] || comic.archive_container || "";
  if (!container) return "";
  const ext = comic.archive_type ? `.${comic.archive_type}` : "";
  return comic.container_mismatch && ext
    ? `${container} — file is named ${ext}` : container;
}

/* One place that decides whether a cover is showing or the placeholder is.
   `ok` means "a real image is on screen": the image is left visible and the
   parent gets `cover-ready`, which drops `.cover-fallback` (styles.css). On a
   genuine failure the image is hidden (`.thumb-failed`) and the fallback —
   with its case-specific text — stays. */
function setCoverState(img, ok) {
  img.classList.toggle("thumb-failed", !ok);
  const holder = img.closest(".card-cover, .issue-cover");
  if (holder) holder.classList.toggle("cover-ready", ok);
}
function wireThumbs(root) {
  root.querySelectorAll("img.cover-img").forEach((img) => {
    img.addEventListener("error", () => { setCoverState(img, false); });
    img.addEventListener("load", () => { setCoverState(img, true); });
    /* A cached thumbnail is often already decoded before this code runs — its
       `load` event fired in the past and will never fire for us, so asking for
       the current state is the only way to dismiss the placeholder then.
       `complete` is also true for a broken image, hence the naturalWidth test. */
    if (img.complete) setCoverState(img, img.naturalWidth > 0);
  });
}

function paintLibrary() {
  const parts = [];
  parts.push(doctorPanel());
  if (library.status) parts.push(problemsPanel());
  if (library.error) {
    parts.push(errorPanel("Could not load your library", library.error));
  } else if (library.loading && !library.items.length) {
    parts.push('<p class="muted">Loading…</p>');
  } else if (!library.items.length && library.loaded) {
    if (filtersActive()) {
      parts.push(notice("warn", "No comics match those filters",
        "Try a different search, or clear the filters.",
        '<button type="button" class="btn" id="clear-empty">Clear filters</button>'));
    } else {
      parts.push(notice("warn", "Nothing is indexed yet",
        "Point Longbox at your comics folder and press Rescan library — " +
        "that reads every .cbz/.cbr file and fills this page.",
        '<button type="button" class="btn btn-primary" id="rescan-empty">Rescan library</button>'));
    }
  }

  if (library.items.length) {
    parts.push(`<div class="grid">${library.items.map(coverCard).join("")}</div>`);
    const footer = [];
    if (library.items.length < library.total) {
      footer.push(`<button type="button" class="btn" id="load-more"${library.loading ? " disabled" : ""}>` +
        `${library.loading ? "Loading…" : "Load more"}</button>`);
    }
    footer.push(`<span class="muted small">Showing ${library.items.length} of ` +
      `${plural(library.total, "comic")}</span>`);
    parts.push(`<div class="grid-footer">${footer.join("")}</div>`);
  }

  viewEl.innerHTML = parts.join("");
  wireThumbs(viewEl);

  const more = document.getElementById("load-more");
  if (more) more.addEventListener("click", loadMore);
  const empty = document.getElementById("rescan-empty");
  if (empty) empty.addEventListener("click", rescan);
  const clear = document.getElementById("clear-empty");
  if (clear) clear.addEventListener("click", clearFilters);
}

function clearFilters() {
  library.q = "";
  library.series = "";
  library.year = "";
  library.read = "";
  library.status = "";
  library.sort = "series";
  qEl.value = "";
  applyFilters({});
}

/* ------------------------------------------------------------------- detail */

function dateLabel(comic) {
  const parts = [comic.year, comic.month, comic.day];
  if (!parts[0]) return "";
  if (!parts[1]) return String(parts[0]);
  if (!parts[2]) return `${parts[0]}-${String(parts[1]).padStart(2, "0")}`;
  return `${parts[0]}-${String(parts[1]).padStart(2, "0")}-${String(parts[2]).padStart(2, "0")}`;
}

function fact(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<dt>${esc(label)}</dt><dd>${value}</dd>`;
}

function chipList(items) {
  if (!items || !items.length) return '<p class="muted small">None recorded in the file.</p>';
  return `<ul class="chips">${items.map((item) => `<li class="chip">${esc(item)}</li>`).join("")}</ul>`;
}

function creditsList(comic) {
  const rows = [];
  CREATOR_ROLES.forEach(([field, label]) => {
    const names = comic[field];
    if (Array.isArray(names) && names.length) {
      rows.push(`<li><span class="role">${esc(label)}:</span> ${esc(names.join(", "))}</li>`);
    }
  });
  return rows.length ? `<ul class="credits">${rows.join("")}</ul>`
                     : '<p class="muted small">No creator credits in this file.</p>';
}

function detailUrl(id) { return `#/comics/${id}`; }

async function renderDetail(id) {
  viewEl.innerHTML = '<p class="muted">Loading issue…</p>';
  let comic;
  try {
    comic = await api(`/api/comics/${id}`);
  } catch (err) {
    viewEl.innerHTML = errorPanel("Could not open that issue", err.message) + backLink();
    return;
  }

  const title = comic.title ? `${comic.series || "Untitled"} — ${comic.title}`
                            : (comic.series || "Untitled");
  const pages = comic.page_count || 0;
  const broken = comic.status !== "ok";

  const readLabel = comic.read ? "Read again"
    : comic.last_page > 0 ? `Continue from page ${comic.last_page}` : "Read";
  const actions = [];
  if (!broken && pages > 0) {
    const target = comic.read ? 1 : Math.min(comic.last_page > 0 ? comic.last_page : 1, pages);
    actions.push(`<a class="btn btn-primary" href="#/comics/${comic.id}/page/${target}">` +
      `${esc(readLabel)}</a>`);
  } else if (broken) {
    actions.push('<span class="muted small">No reader available — this file has a problem.</span>');
  } else {
    actions.push('<span class="muted small">No readable pages in this file.</span>');
  }
  if (!broken && pages > 0) {
    actions.push(`<button type="button" class="btn" id="toggle-read">` +
      `${comic.read ? "Mark as unread" : "Mark as read"}</button>`);
  }
  actions.push(`<a class="btn btn-quiet" href="${esc(sessionStorage.getItem("longbox.library") || "#/")}">` +
    `&larr; Library</a>`);

  const problem = broken ? notice(comic.status === "missing" ? "warn" : "error",
    comic.status === "missing" ? "This file is missing" : "This file could not be read",
    comic.error_message || "The scanner did not record a reason.") : "";

  viewEl.innerHTML = `<section class="issue">
  ${problem}
  <div class="issue-head">
    <div class="issue-cover">
      <div class="cover-fallback">${broken ? "No cover (file problem)" : "No cover image"}</div>
      <img class="cover-img" alt="" src="${esc(comic.thumb_url)}">
    </div>
    <div class="issue-body">
      <h1>${esc(title)}</h1>
      <p class="issue-sub">${esc(comic.series || "Unknown series")} ${esc(issueLabel(comic))}
        ${dateLabel(comic) ? "· " + esc(dateLabel(comic)) : ""}</p>
      <div class="issue-actions">${actions.join("")}</div>
      <dl class="facts">
        ${fact("Series", esc(comic.series || ""))}
        ${fact("Issue", esc(issueLabel(comic)))}
        ${fact("Title", esc(comic.title || ""))}
        ${fact("Year / month / day", esc(dateLabel(comic)))}
        ${fact("Publisher", esc(comic.publisher || ""))}
        ${fact("Pages", pages ? esc(String(pages)) : "")}
        ${fact("Container", esc(containerLabel(comic)))}
        ${fact("Read state", comic.read ? "Read" : "Unread")}
        ${fact("Last page read", comic.last_page ? esc(String(comic.last_page)) : "")}
        ${fact("Volume (as written in the file)", esc(comic.volume_raw || ""))}
        ${fact("ComicVine issue id", esc(comic.comicvine_issue_id || ""))}
        ${fact("File", esc(comic.filename || ""))}
        ${fact("Full path", esc(comic.path || ""))}
        ${fact("Archive type", esc(comic.archive_type || ""))}
        ${fact("ComicInfo.xml present", comic.comicinfo_present ? "yes" : "no")}
      </dl>
    </div>
  </div>

  <h2 class="section">Summary</h2>
  ${comic.summary ? `<p class="summary">${esc(comic.summary)}</p>`
                  : '<p class="muted small">This file has no summary.</p>'}

  <h2 class="section">Credits</h2>
  ${creditsList(comic)}

  <h2 class="section">Characters</h2>
  ${chipList(comic.characters)}

  <h2 class="section">Teams</h2>
  ${chipList(comic.teams)}

  <h2 class="section">Locations</h2>
  ${chipList(comic.locations)}
</section>`;

  wireThumbs(viewEl);

  const toggle = document.getElementById("toggle-read");
  if (toggle) {
    toggle.addEventListener("click", async () => {
      const target = !comic.read;
      toggle.disabled = true;
      try {
        await api(`/api/comics/${comic.id}/read`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ read: target }),
        });
        comic.read = target;
        loadStats();
        await renderDetail(comic.id);
      } catch (err) {
        toast(`Could not change the read state: ${err.message}`, "error");
        toggle.disabled = false;
      }
    });
  }
}

/* ------------------------------------------------------------------- reader */

let reader = null;
let pageToken = 0;
let progressTimer = null;
let stageObserver = null;

function setReader(value) {
  if (reader && !value) {
    flushProgress();
    if (stageObserver) {
      stageObserver.disconnect();
      stageObserver = null;
    }
  }
  reader = value;
}

/* While the reader is open the library chrome is hidden and the reader fills
   the viewport (see the `body.reader-open` rules in styles.css). Leaving the
   reader - Escape, the "Issue" or "Library" button, the back button, any other
   address - always puts the chrome back, because every route change runs
   through renderRoute(). */
function setReaderMode(on) {
  document.body.classList.toggle(READER_BODY_CLASS, Boolean(on));
}

function readerBackHash() {
  return reader ? detailUrl(reader.id) : "#/";
}

function postProgress(id, lastPage, keepalive) {
  return fetch(`/api/comics/${id}/progress`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ last_page: lastPage }),
    keepalive: Boolean(keepalive),
  }).then((response) => {
    if (!response.ok) toast("Could not save your place in this issue.", "error");
  }).catch(() => {
    toast("Could not save your place — the server is not answering.", "error");
  });
}

function flushProgress() {
  clearTimeout(progressTimer);
  progressTimer = null;
  if (reader && reader.page > 0 && reader.page !== reader.saved) {
    reader.saved = reader.page;
    postProgress(reader.id, reader.page, true);
  }
}

function saveProgress(page) {
  clearTimeout(progressTimer);
  progressTimer = setTimeout(flushProgress, PROGRESS_DEBOUNCE_MS);
  if (page === reader.total) markRead(true);
}

async function markRead(value) {
  if (!reader || reader.read === value) return;
  reader.read = value;
  try {
    await api(`/api/comics/${reader.id}/read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ read: value }),
    });
    paintReaderBar();
    loadStats();
  } catch (err) {
    toast(`Could not change the read state: ${err.message}`, "error");
  }
}

async function renderReader(id, page) {
  viewEl.innerHTML = '<p class="muted">Opening the reader…</p>';
  let comic;
  try {
    comic = await api(`/api/comics/${id}`);
  } catch (err) {
    setReaderMode(false);
    viewEl.innerHTML = errorPanel("Could not open the reader", err.message) + backLink();
    return;
  }

  const total = comic.page_count || (comic.pages ? comic.pages.length : 0);
  if (comic.status !== "ok" || !total) {
    const reason = comic.error_message ||
      (comic.status === "missing" ? "file no longer present at this path"
                                  : "the archive holds no readable pages");
    viewEl.innerHTML = errorPanel("The reader cannot open this issue", reason) +
      backLink("Back to the issue") +
      `<p><a href="${esc(sessionStorage.getItem("longbox.library") || "#/")}">&larr; Back to library</a></p>`;
    setReaderMode(false);
    return;
  }

  reader = {
    id: comic.id, comic, total, page: 0, saved: comic.last_page || 0,
    read: Boolean(comic.read), dirtyPage: comic.last_page || 0, next: null,
  };
  await showPage(Math.min(Math.max(1, page), total), { first: true });
  if (reader.page === total) findNextIssue();
}

function paintReaderShell() {
  setReaderMode(true);
  viewEl.innerHTML = `<section class="reader">
  <div class="reader-bar" id="reader-bar"></div>
  <div class="stage" id="stage"></div>
  <div class="reader-foot" id="reader-foot"></div>
</section>`;
  watchStage();
  paintReaderBar();
  paintReaderFoot();
}

/* ------------------------------------------------------------- page fitting */
/* A page is shown whole: it is scaled to the stage so that neither the width
   nor the height overflows, which is "fit height" for the usual tall comic
   page and "fit width" for a landscape spread.  Small and low-res pages are
   grown the same way (up to MAX_PAGE_UPSCALE) instead of floating tiny in the
   middle of the window.  The aspect ratio is never touched and nothing is ever
   cropped, because both sides are computed from one single scale factor. */

function fitPage(image) {
  const stage = document.getElementById("stage");
  if (!stage || !image || !image.naturalWidth || !image.naturalHeight) return;
  const boxWidth = Math.max(1, stage.clientWidth - STAGE_PADDING);
  const boxHeight = Math.max(1, stage.clientHeight - STAGE_PADDING);
  const scaleByWidth = boxWidth / image.naturalWidth;
  const scaleByHeight = boxHeight / image.naturalHeight;
  const scale = Math.min(scaleByWidth, scaleByHeight, MAX_PAGE_UPSCALE);
  image.style.width = `${Math.max(1, Math.floor(image.naturalWidth * scale))}px`;
  image.style.height = `${Math.max(1, Math.floor(image.naturalHeight * scale))}px`;
  const mode = scaleByWidth < scaleByHeight ? "width" : "height";
  stage.dataset.fit = mode;
  image.dataset.fit = mode;
  image.dataset.scale = String(Math.round(scale * 100) / 100);
  if (reader) {
    reader.fit = { mode, scale, upscaled: scale > 1.01 };
  }
}

function refitPage() {
  const image = viewEl.querySelector(".page-image");
  if (image && image.complete && image.naturalWidth) fitPage(image);
}

/* The stage changes size when the window is resized, when the browser zoom
   changes, and when the chrome goes away - the page follows it. */
function watchStage() {
  const stage = document.getElementById("stage");
  if (stageObserver) stageObserver.disconnect();
  stageObserver = null;
  if (!stage) return;
  if (typeof ResizeObserver === "function") {
    stageObserver = new ResizeObserver(() => refitPage());
    stageObserver.observe(stage);
  }
  refitPage();
}

function paintReaderBar() {
  const bar = document.getElementById("reader-bar");
  if (!bar || !reader) return;
  const comic = reader.comic;
  const libraryUrl = sessionStorage.getItem("longbox.library") || "#/";
  bar.innerHTML = `
    <a class="btn" href="${esc(detailUrl(comic.id))}" title="Back to the issue (Esc)">Issue</a>
    <a class="btn" href="${esc(libraryUrl)}" title="Back to the library">Library</a>
    <button type="button" class="btn" id="prev"${reader.page <= 1 ? " disabled" : ""}>&larr; Prev</button>
    <button type="button" class="btn" id="next"${reader.page >= reader.total ? " disabled" : ""}>Next &rarr;</button>
    <span class="reader-progress">Page</span>
    <input id="jump" type="number" min="1" max="${reader.total}" value="${reader.page}" aria-label="Go to page">
    <span class="reader-progress">of ${reader.total}</span>
    <span class="spacer"></span>
    <span class="reader-title" title="${esc(comic.series || "")}">${esc(issueLabel(comic))} ${esc(comic.series || "")}</span>
    <button type="button" class="btn" id="toggle-read">${reader.read ? "Mark unread" : "Mark read"}</button>`;

  document.getElementById("prev").addEventListener("click", () => showPage(reader.page - 1));
  document.getElementById("next").addEventListener("click", () => showPage(reader.page + 1));
  const jump = document.getElementById("jump");
  jump.addEventListener("change", () => {
    const wanted = Number(jump.value);
    if (Number.isFinite(wanted)) showPage(wanted);
    jump.value = reader.page;
  });
  jump.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); jump.blur(); }
  });
  document.getElementById("toggle-read").addEventListener("click", () => markRead(!reader.read));
}

function paintReaderFoot() {
  const foot = document.getElementById("reader-foot");
  if (!foot || !reader) return;
  const bits = [`Page ${reader.page} of ${reader.total}`];
  if (reader.page < reader.total) bits.push("Click the page, press Space or → for the next page");
  if (reader.read) bits.push("Read");
  if (reader.next) {
    bits.push(`<a href="#/comics/${reader.next.id}/page/1">Next issue: ` +
      `${esc(reader.next.series || "")} ${esc(issueLabel(reader.next))} &rarr;</a>`);
  } else if (reader.page === reader.total) {
    bits.push('<span class="muted">That is the last issue in this series.</span>');
  }
  foot.innerHTML = bits.map((bit) => `<span>${bit}</span>`).join("");
}

function pageFailure(page, message) {
  return `<div class="stage-message">
  <h2>Page ${page} could not be shown</h2>
  <p>${esc(message)}</p>
  <button type="button" class="btn" id="page-retry">Try page ${page} again</button>
  ${page < reader.total ? `<button type="button" class="btn" id="page-skip">Skip to page ${page + 1}</button>` : ""}
  ${page > 1 ? `<button type="button" class="btn" id="page-back">Back to page ${page - 1}</button>` : ""}
</div>`;
}

function wirePageFailure(page) {
  const retry = document.getElementById("page-retry");
  if (retry) retry.addEventListener("click", () => showPage(page, { force: true }));
  const skip = document.getElementById("page-skip");
  if (skip) skip.addEventListener("click", () => showPage(page + 1, { force: true }));
  const back = document.getElementById("page-back");
  if (back) back.addEventListener("click", () => showPage(page - 1, { force: true }));
}

function preload(page) {
  [page + 1, page - 1].forEach((neighbour) => {
    if (neighbour >= 1 && neighbour <= reader.total) {
      const image = new Image();
      image.src = `/api/comics/${reader.id}/pages/${neighbour}`;
    }
  });
}

function showPage(wanted, options) {
  if (!reader) return;
  options = options || {};
  const page = Math.min(Math.max(1, Math.round(wanted)), reader.total);
  const changed = page !== reader.page;
  reader.page = page;
  if (changed || options.first) {
    history.replaceState(null, "", `#/comics/${reader.id}/page/${page}`);
  }
  if (!changed && !options.force && !options.first) {
    paintReaderBar();
    paintReaderFoot();
    return;
  }
  // Leaving a page never re-posts immediately: showPage() lands on the new page
  // and saveProgress() debounces the write; pagehide/visibilitychange flush it.
  if (document.getElementById("reader-bar") === null) paintReaderShell();
  paintReaderBar();
  paintReaderFoot();

  const stage = document.getElementById("stage");
  const token = ++pageToken;
  stage.innerHTML = `<div class="stage-message"><h2>Loading page ${page} of ${reader.total}…</h2>
    <p class="muted">The first look at a big page can take a moment.</p></div>`;

  const image = new Image();
  image.className = "page-image";
  image.alt = `Page ${page} of ${reader.total}`;
  image.addEventListener("load", () => {
    if (token !== pageToken || !reader) return;
    stage.innerHTML = "";
    stage.appendChild(image);
    fitPage(image);
    image.addEventListener("click", () => showPage(reader.page + 1));
    saveProgress(page);
    preload(page);
    if (page === reader.total) findNextIssue();
  });
  image.addEventListener("error", () => {
    if (token !== pageToken || !reader) return;
    stage.innerHTML = pageFailure(page, "The archive has this page listed, but the " +
      "server could not send it. The file may be damaged on that page.");
    wirePageFailure(page);
  });
  image.src = `/api/comics/${reader.id}/pages/${page}`;
}

async function findNextIssue() {
  if (!reader || reader.next) return;
  const comic = reader.comic;
  const filter = comic.series_key || comic.series || "";
  const previous = reader.next;
  try {
    const params = new URLSearchParams();
    if (filter) params.set("series", filter);
    params.set("sort", "issue");
    params.set("limit", "500");
    const data = await api(`/api/comics?${params.toString()}`);
    const index = data.items.findIndex((item) => item.id === reader.id);
    if (index >= 0 && index + 1 < data.items.length) reader.next = data.items[index + 1];
    if (reader.next !== previous) paintReaderFoot();
  } catch (err) {
    reader.next = null;
  }
}

/* ------------------------------------------------------------- global wiring */

let searchTimer = null;

function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const value = qEl.value.trim();
    if (value !== library.q) applyFilters({ q: value });
  }, 250);
}

qEl.addEventListener("input", debounceSearch);
seriesEl.addEventListener("change", () => applyFilters({ series: seriesEl.value }));
yearEl.addEventListener("change", () => applyFilters({ year: yearEl.value }));
readEl.addEventListener("change", () => applyFilters({ read: readEl.value }));
statusEl.addEventListener("change", () => applyFilters({ status: statusEl.value }));
sortEl.addEventListener("change", () => applyFilters({ sort: sortEl.value }));
clearEl.addEventListener("click", clearFilters);
rescanBtn.addEventListener("click", rescan);

statsEl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-status]");
  if (button) applyFilters({ status: button.dataset.status });
});

document.addEventListener("keydown", (event) => {
  if (!reader) return;
  const target = event.target;
  const typing = target && target.tagName &&
    ["INPUT", "SELECT", "TEXTAREA"].indexOf(target.tagName) !== -1;
  if (event.key === "Escape") {
    location.hash = readerBackHash();
    return;
  }
  if (typing) return;
  switch (event.key) {
    case "ArrowRight":
    case "ArrowDown":
    case "PageDown":
    case " ":
      event.preventDefault();
      showPage(reader.page + 1);
      break;
    case "ArrowLeft":
    case "ArrowUp":
    case "PageUp":
      event.preventDefault();
      showPage(reader.page - 1);
      break;
    case "Home":
      event.preventDefault();
      showPage(1);
      break;
    case "End":
      event.preventDefault();
      showPage(reader.total);
      break;
    default:
      break;
  }
});

/* Browser zoom and a resized window both change the stage; the page follows it
   even where ResizeObserver is not there to notice. */
window.addEventListener("resize", refitPage);

window.addEventListener("pagehide", () => {
  if (reader && reader.page) {
    reader.saved = reader.page;
    postProgress(reader.id, reader.page, true);
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && reader) flushProgress();
});

window.addEventListener("hashchange", () => { renderRoute(); });

/* ------------------------------------------------------------------ startup */

if (!location.hash) location.hash = "#/";
loadFilterOptions();
loadStats();
renderRoute();
