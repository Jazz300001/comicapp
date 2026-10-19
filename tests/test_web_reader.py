"""The reader takes the whole window, and the page is fitted to it.

Both halves are CSS/JS only, so what can be checked without a browser is the
served source: the rules a browser needs (the chrome hidden, the reader filling
the viewport, the stage keeping its scrollbar), the wiring that switches those
rules on the reader route and off again on the way out, and the fit arithmetic
the page image is sized with.  These assertions read the files the app actually
serves, so a renamed class, a dropped rule or a rewritten formula fails here.
The aspect-ratio cases below re-run the published formula, so "scaled up but
never distorted and never cropped" is checked as arithmetic, not as a promise.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "longbox" / "static"

# Chrome the reader keeps on screen: the top bar (~50px) and the foot (~38px).
READER_CHROME_H = 88


def rules_for(css: str, selector: str) -> list[str]:
    """Declarations of every rule whose selector list contains `selector`."""
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", strip_comments(css)):
        parts = [part.strip() for part in match.group(1).split(",")]
        if selector in parts:
            found.append(match.group(2))
    return found


def strip_comments(css: str) -> str:
    """Comments sit right above a selector; they are not part of it."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def declarations(css: str, selector: str) -> str:
    """All declarations that apply to `selector`, joined."""
    blocks = rules_for(css, selector)
    assert blocks, f"no CSS rule for {selector!r}"
    return "\n".join(blocks)


def function_body(js: str, name: str) -> str:
    """Source of `function name(...) { ... }`, braces matched."""
    start = js.index(f"function {name}(")
    depth = 0
    for index in range(start, len(js)):
        if js[index] == "{":
            depth += 1
        elif js[index] == "}":
            depth -= 1
            if depth == 0:
                return js[start:index + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


def constant(js: str, name: str) -> float:
    match = re.search(rf"const {name} = ([0-9.]+);", js)
    assert match, f"no constant {name}"
    return float(match.group(1))


def fit(natural_w, natural_h, box_w, box_h, max_upscale):
    """The fit formula the UI uses, spelled out so it can be checked."""
    scale_by_width = box_w / natural_w
    scale_by_height = box_h / natural_h
    scale = min(scale_by_width, scale_by_height, max_upscale)
    width = max(1, math.floor(natural_w * scale))
    height = max(1, math.floor(natural_h * scale))
    mode = "width" if scale_by_width < scale_by_height else "height"
    return width, height, mode, scale


@pytest.fixture()
def js(client) -> str:
    return client.get("/static/app.js").text


@pytest.fixture()
def css(client) -> str:
    return client.get("/static/styles.css").text


# --- the reader owns the whole window ----------------------------------------

def test_reading_takes_the_whole_window(css):
    # the topbar and the filter bar are out of the way, and the page padding
    # (18px all round) goes with them
    assert "display: none" in declarations(css, "body.reader-open .topbar")
    assert "display: none" in declarations(css, "body.reader-open .controls")
    assert "padding: 0" in declarations(css, "body.reader-open .view")
    # and the reader itself grows to the full viewport instead of leaving the
    # topbar's 58px as dead space
    reader = declarations(css, "body.reader-open .reader")
    assert "height: 100dvh" in reader
    assert "height: 100vh" in reader, "vh fallback for browsers without dvh"
    # with the chrome shown (any other screen) the old subtraction still holds
    assert "calc(100dvh - var(--topbar-h))" in declarations(css, ".reader")


def test_the_stage_keeps_its_own_scrolling(css):
    stage = declarations(css, ".stage")
    assert "overflow: auto" in stage
    # a flex child refuses to shrink below its content by default, which would
    # push the reader foot off screen in a half-height window
    assert "min-height: 0" in stage
    # nothing in reader mode traps the page in an unscrollable box
    for selector in ("html, body", "body", "html"):
        for block in rules_for(css, selector):
            assert "overflow: hidden" not in block
    for block in rules_for(css, "body.reader-open .reader"):
        assert "overflow: hidden" not in block


def test_reader_shell_still_has_the_containers_the_code_wires(js):
    shell = function_body(js, "paintReaderShell")
    for fragment in ('class="reader"', 'id="reader-bar"', 'id="stage"',
                     'id="reader-foot"'):
        assert fragment in shell


def test_reader_mode_is_switched_by_the_wiring_code(js, css):
    assert 'const READER_BODY_CLASS = "reader-open"' in js
    assert "classList.toggle(READER_BODY_CLASS" in function_body(js, "setReaderMode")
    # on the moment the address says reader, so the chrome never flashes back
    assert 'setReaderMode(route.name === "reader")' in function_body(js, "renderRoute")
    assert "setReaderMode(true)" in function_body(js, "paintReaderShell")
    # the class the code sets is the class the stylesheet keys off
    assert "body.reader-open" in css


def test_leaving_the_reader_gives_the_chrome_back(js):
    reader = function_body(js, "renderReader")
    # both ways out of the reader screen (no such comic, unreadable issue) hand
    # the window back to the library chrome
    assert reader.count("setReaderMode(false)") >= 2
    # leaving the reader disconnects the resize watcher as well
    assert "stageObserver.disconnect()" in function_body(js, "setReader")


def test_escape_and_the_buttons_still_leave_the_reader(js):
    # Escape behaviour is untouched: back to the issue this page came from
    assert "location.hash = readerBackHash()" in js
    bar = function_body(js, "paintReaderBar")
    # a mouse-only way back that needs no keyboard
    assert 'title="Back to the library"' in bar
    assert 'sessionStorage.getItem("longbox.library")' in bar
    assert 'href="${esc(detailUrl(comic.id))}"' in bar
    # next/prev navigation is the same as before
    assert 'id="prev"' in bar and 'id="next"' in bar
    assert "showPage(reader.page - 1)" in bar
    assert "showPage(reader.page + 1)" in bar


# --- small and low-res pages are grown to fit --------------------------------

def test_pages_are_fitted_to_the_stage(css, js):
    body = function_body(js, "fitPage")
    assert "stage.clientWidth" in body and "stage.clientHeight" in body
    assert "Math.min(scaleByWidth, scaleByHeight, MAX_PAGE_UPSCALE)" in body
    # both sides come from the one scale factor: no independent stretching
    assert "image.style.width = " in body and "image.style.height = " in body
    assert "image.naturalWidth * scale" in body
    assert "image.naturalHeight * scale" in body
    # a small page is grown; a thumbnail-sized scan is not blown into mush
    upscale = constant(js, "MAX_PAGE_UPSCALE")
    assert 1 < upscale <= 4
    # the fit leaves the stage's 8px padding alone
    assert "padding: 8px" in declarations(css, ".stage")
    assert constant(js, "STAGE_PADDING") == 8 * 2


def test_the_fit_keeps_the_aspect_ratio_and_never_crops(css, js):
    upscale = constant(js, "MAX_PAGE_UPSCALE")
    padding = constant(js, "STAGE_PADDING")
    # 1920x1080 and 1366x768, and the same window shrunk to half its width
    boxes = [(1920, 1080 - READER_CHROME_H), (1366, 768 - READER_CHROME_H),
             (683, 384 - READER_CHROME_H)]
    pages = [(1988, 3057), (1264, 1920), (300, 450), (100, 150),
             (1700, 800), (800, 1700)]
    for window_w, window_h in boxes:
        box_w = window_w - padding
        box_h = window_h - padding
        for natural_w, natural_h in pages:
            width, height, mode, scale = fit(natural_w, natural_h, box_w, box_h,
                                             upscale)
            ratio = (natural_w / natural_h) / (width / height)
            assert abs(ratio - 1) < 0.01, (natural_w, natural_h, width, height)
            assert width <= box_w and height <= box_h, "page would overflow"
            assert scale <= upscale
            # the fitted axis is the one that fills the stage, unless the
            # upscale cap is what stopped the page growing
            if scale < upscale:
                if mode == "height":
                    assert abs(height - box_h) <= 1, (natural_w, natural_h, height, box_h)
                    assert width <= box_w
                else:
                    assert abs(width - box_w) <= 1, (natural_w, natural_h, width, box_w)
                    assert height <= box_h
            else:
                assert scale == upscale

    # on a normal window the page shape decides the axis: a tall comic page
    # fills the height, a landscape spread fills the width
    box_w, box_h = 1920 - padding, 1080 - READER_CHROME_H - padding
    tall = fit(1988, 3057, box_w, box_h, upscale)
    spread = fit(1700, 800, box_w, box_h, upscale)
    assert tall[2] == "height" and abs(tall[1] - box_h) <= 1
    assert spread[2] == "width" and abs(spread[0] - box_w) <= 1
    # a low-res scan on a big screen is grown rather than left tiny
    small = fit(300, 450, box_w, box_h, upscale)
    assert small[0] > 300 and small[1] > 450
    # and a thumbnail-sized scan stops at the cap instead of turning to mush
    tiny = fit(100, 150, box_w, box_h, upscale)
    assert tiny[3] == upscale


def test_the_bounds_stay_as_a_safety_net(css):
    image = declarations(css, ".page-image")
    # before the fit runs (and if it ever could not run) the page cannot spill
    # out of the stage, and flex is not allowed to squeeze it out of shape
    assert "max-width: 100%" in image
    assert "max-height: 100%" in image
    assert "flex: 0 0 auto" in image
    assert "object-fit: contain" in image
    # cover/fill would crop or stretch the page (the grid's covers are a
    # different element and keep `object-fit: cover` for their thumbnails)
    assert "cover" not in image
    assert "fill" not in image


def test_the_page_follows_a_resized_window(js):
    assert 'window.addEventListener("resize", refitPage)' in js
    assert "new ResizeObserver(" in function_body(js, "watchStage")
    assert "stageObserver.observe(stage)" in function_body(js, "watchStage")
    refit = function_body(js, "refitPage")
    # a page that has not decoded yet has no size to fit
    assert "image.complete" in refit and "image.naturalWidth" in refit
    # the fit is applied to a page that has just loaded
    assert "fitPage(image)" in function_body(js, "showPage")


def test_scaling_stays_client_side_and_read_only(js):
    """The fit is CSS/JS arithmetic: no new request, no new server surface."""
    start = js.index("/* ------------------------------------------------------------- page fitting */")
    end = js.index("function paintReaderBar()")
    fitting = js[start:end]
    for forbidden in ("fetch(", "XMLHttpRequest", "http://", "https://", "POST"):
        assert forbidden not in fitting


def test_reading_position_and_navigation_are_untouched(client):
    """The API the reader leans on still behaves exactly as before."""
    items = client.get("/api/comics", params={"limit": 50}).json()["items"]
    comic = next(item for item in items if item["status"] == "ok" and item["page_count"])
    page = client.get(f"/api/comics/{comic['id']}/pages/1")
    assert page.status_code == 200
    assert len(page.content) > 100
    saved = client.post(f"/api/comics/{comic['id']}/progress", json={"last_page": 2})
    assert saved.json()["last_page"] == 2
    assert client.get(f"/api/comics/{comic['id']}").json()["last_page"] == 2
    # page 2 exists; page 0 and the page after the last are refused
    assert client.get(f"/api/comics/{comic['id']}/pages/2").status_code == 200
    assert client.get(f"/api/comics/{comic['id']}/pages/0").status_code == 404
    assert client.get(
        f"/api/comics/{comic['id']}/pages/{comic['page_count'] + 1}"
    ).status_code == 404
