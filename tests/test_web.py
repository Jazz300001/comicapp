"""The web UI surface: the HTML shell, the static files, and the small API
additions the screens need (year list, file-status filter).

The UI is plain hash-routed HTML/CSS/JS served by the same FastAPI app, so most
of what can be checked without a browser is: the shell is served, the assets are
the real files, nothing points at the internet, and the endpoints the screens
call answer with the shape they expect.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "longbox" / "static"


def test_root_serves_the_html_shell(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Longbox</title>" in response.text
    assert "/static/app.js" in response.text
    assert "/static/styles.css" in response.text


def test_shell_contains_the_library_controls(client):
    html = client.get("/").text
    for element in ('id="q"', 'id="series"', 'id="year"', 'id="read"',
                    'id="status"', 'id="sort"', 'id="rescan"', 'id="view"'):
        assert element in html


def test_ui_loads_nothing_from_the_internet(client):
    # The owner's machine may be offline: no CDN, no webfont, no analytics.
    for path in ("/", "/static/app.js", "/static/styles.css"):
        text = client.get(path).text
        assert "https://" not in text
        assert "cdn." not in text
        assert "fonts.googleapis" not in text
        assert "google-analytics" not in text
    # the only http:// left is the SVG namespace inside the inline favicon
    assert client.get("/").text.count("http://") <= 1


def test_static_assets_are_served(client):
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "--bg:" in css.text

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "loadLibrary" in js.text


def test_static_assets_are_the_files_on_disk(client):
    assert client.get("/static/app.js").text == (STATIC / "app.js").read_text(encoding="utf-8")
    assert client.get("/static/styles.css").text == (STATIC / "styles.css").read_text(encoding="utf-8")


def test_missing_static_file_is_a_404(client):
    assert client.get("/static/no-such-file.js").status_code == 404


def test_the_ui_does_not_shadow_the_api(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/comics").json()["total"] == 14


def test_years_endpoint_fills_the_year_filter(client):
    years = client.get("/api/years").json()
    values = [item["year"] for item in years["items"]]
    assert values == sorted(values, reverse=True)
    assert years["total"] == len(values) > 0
    assert 2000 in values
    assert all(item["count"] > 0 for item in years["items"])


def test_status_filter_lists_unreadable_files_with_their_reason(client):
    broken = client.get("/api/comics", params={"status": "error"}).json()
    assert broken["total"] == 2
    assert {item["status"] for item in broken["items"]} == {"error"}
    assert all(item["error_message"] for item in broken["items"])
    assert all(item["filename"] for item in broken["items"])


def test_status_filter_keeps_the_rest_of_the_library(client):
    readable = client.get("/api/comics", params={"status": "ok", "limit": 100}).json()
    assert readable["total"] == 12
    assert {item["status"] for item in readable["items"]} == {"ok"}
    assert client.get("/api/comics", params={"status": "nothing-like-this"}).json()["total"] == 0


def test_status_filter_combines_with_series_and_read(client):
    filtered = client.get("/api/comics", params={"status": "ok", "series": "X-Men"}).json()
    assert filtered["total"] == 4
    unread = client.get("/api/comics", params={"status": "ok", "read": "false",
                                               "limit": 100}).json()
    assert unread["total"] == 12


def test_reader_can_save_a_place_and_mark_an_issue_read(client):
    item = client.get("/api/comics", params={"limit": 1}).json()["items"][0]
    assert client.post(f"/api/comics/{item['id']}/progress",
                       json={"last_page": 3}).json()["last_page"] == 3
    assert client.post(f"/api/comics/{item['id']}/read", json={"read": True}).json()["read"] is True
    detail = client.get(f"/api/comics/{item['id']}").json()
    assert detail["last_page"] == 3 and detail["read"] is True


def test_next_issue_lookup_uses_the_series_filter_and_issue_sort(client):
    listing = client.get("/api/comics", params={"series": "x-men", "sort": "issue",
                                                "limit": 500}).json()
    issues = [item["issue_number"] for item in listing["items"]]
    assert issues == ["11", "12", "13", "14"]


def test_rescan_button_has_an_endpoint_to_call(client):
    summary = client.post("/api/scan").json()
    assert summary["found"] == 14
    assert summary["errors"] == 2
    assert len(summary["error_files"]) == 2
    assert "elapsed" in summary and "total_in_db" in summary
