"""The JSON API, exercised through fastapi's TestClient."""

from __future__ import annotations

import os

from longbox import archive as archive_mod


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["comics"] == 14


def test_list_comics_returns_pagination_envelope(client):
    body = client.get("/api/comics").json()
    assert set(body) >= {"items", "total", "limit", "offset"}
    assert body["total"] == 14
    assert body["limit"] == 50 and body["offset"] == 0
    assert len(body["items"]) == 14
    first = body["items"][0]
    assert first["thumb_url"].endswith("/thumbnail")
    assert first["read"] is False and first["last_page"] == 0


def test_list_is_sorted_by_series_and_issue_numerically(client):
    items = client.get("/api/comics", params={"limit": 100}).json()["items"]
    keys = [item["series_key"] for item in items]
    assert keys[0] == "batman"
    xmen = [item["issue_number"] for item in items if item["series_key"] == "xmen"]
    assert xmen == ["11", "12", "13", "14"]


def test_pagination_limits_and_offsets(client):
    page = client.get("/api/comics", params={"limit": 5, "offset": 5}).json()
    assert page["total"] == 14 and len(page["items"]) == 5
    whole = client.get("/api/comics", params={"limit": 100}).json()["items"]
    assert [item["id"] for item in page["items"]] == [item["id"] for item in whole[5:10]]


def test_search_matches_series_title_writer_and_character(client):
    assert client.get("/api/comics", params={"q": "saga"}).json()["total"] == 1
    assert client.get("/api/comics", params={"q": "Powerless"}).json()["total"] == 1
    assert client.get("/api/comics", params={"q": "Jed MacKay"}).json()["total"] == 1
    characters = client.get("/api/comics", params={"q": "Cyclops"}).json()
    assert characters["total"] == 1
    assert characters["items"][0]["series"] == "X-Men"
    assert client.get("/api/comics", params={"q": "nothing-matches"}).json()["total"] == 0


def test_series_and_year_filters(client):
    by_name = client.get("/api/comics", params={"series": "X-Men"}).json()
    by_variant = client.get("/api/comics", params={"series": "x-men"}).json()
    assert by_name["total"] == by_variant["total"] == 4
    assert by_name["total"] == 4
    assert client.get("/api/comics", params={"year": 2000}).json()["total"] == 2
    assert client.get("/api/comics", params={"year": 1234}).json()["total"] == 0


def test_read_filter_and_progress_round_trip(client):
    comic_id = client.get("/api/comics", params={"q": "X-Men Annual"}).json()["items"][0]["id"]
    assert client.post(f"/api/comics/{comic_id}/progress",
                       json={"last_page": 3}).json()["last_page"] == 3
    detail = client.get(f"/api/comics/{comic_id}").json()
    assert detail["last_page"] == 3 and detail["read"] is False
    assert client.get("/api/comics", params={"read": False}).json()["total"] == 14
    assert client.post(f"/api/comics/{comic_id}/read", json={"read": True}).json()["read"] is True
    assert client.get("/api/comics", params={"read": True}).json()["total"] == 1
    assert client.get("/api/comics", params={"read": False}).json()["total"] == 13
    # marking read must not lose the reading position
    assert client.get(f"/api/comics/{comic_id}").json()["last_page"] == 3
    assert client.post(f"/api/comics/{comic_id}/read",
                       json={"read": False}).json()["read"] is False
    assert client.get("/api/comics", params={"read": True}).json()["total"] == 0


def test_detail_has_the_parsed_metadata(client):
    comic_id = client.get("/api/comics", params={"q": "Live Capture"}).json()["items"][0]["id"]
    detail = client.get(f"/api/comics/{comic_id}").json()
    assert detail["series"] == "X-Men" and detail["issue_number"] == "11"
    assert detail["comicvine_issue_id"] == "1095610"
    assert "Cyclops [1459]" in detail["characters"]
    assert "X-Men [3173]" in detail["teams"]
    assert "Alaska [55827]" in detail["locations"]
    assert detail["writers"] == ["Jed MacKay"]
    assert "\n" in detail["summary"]                     # newlines survive
    assert detail["comicinfo_raw"].startswith("<?xml")
    assert detail["volume_raw"] == "2024" and detail["volume_kind"] == "volume_year"
    assert len(detail["pages"]) == 27
    assert detail["pages"][0]["name"] == "page1.jpg"
    assert os.path.isabs(detail["path"])


def test_detail_and_pages_for_unknown_id_is_404(client):
    assert client.get("/api/comics/999999").status_code == 404
    assert client.get("/api/comics/999999/pages/1").status_code == 404
    assert client.get("/api/comics/999999/thumbnail").status_code == 404
    assert client.post("/api/comics/999999/progress", json={"last_page": 1}).status_code == 404
    assert client.post("/api/comics/999999/read", json={"read": True}).status_code == 404


def test_page_bytes_are_served_with_the_right_content_type(client):
    comic_id = client.get("/api/comics", params={"q": "Saga"}).json()["items"][0]["id"]
    response = client.get(f"/api/comics/{comic_id}/pages/1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert len(response.content) > 0
    assert response.content[:2] == b"\xff\xd8"           # real JPEG bytes
    # 10.jpg is page 10 - natural ordering, proven through the API
    assert client.get(f"/api/comics/{comic_id}/pages/10").headers["x-page-name"] == "10.jpg"


def test_page_number_out_of_range_and_error_comics(client):
    comic_id = client.get("/api/comics", params={"q": "Saga"}).json()["items"][0]["id"]
    assert client.get(f"/api/comics/{comic_id}/pages/99").status_code == 404
    assert client.get(f"/api/comics/{comic_id}/pages/0").status_code == 404
    broken = client.get("/api/comics", params={"q": "Broken Scan"}).json()["items"][0]
    assert broken["status"] == "error"
    assert client.get(f"/api/comics/{broken['id']}/pages/1").status_code == 404
    assert client.get(f"/api/comics/{broken['id']}/thumbnail").status_code == 404


def test_thumbnail_is_generated_once_and_cached_outside_the_comics_folder(client):
    comic_id = client.get("/api/comics", params={"q": "Saga"}).json()["items"][0]["id"]
    response = client.get(f"/api/comics/{comic_id}/thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content[:2] == b"\xff\xd8"
    cached = os.path.join(client.thumb_dir, f"{comic_id}.jpg")
    assert os.path.exists(cached)
    assert not os.path.exists(os.path.join(client.get("/api/health").json()["comics_folder"],
                                          "thumbs"))
    # second call is served from the cache and is byte-identical
    assert client.get(f"/api/comics/{comic_id}/thumbnail").content == response.content


def test_series_endpoint_groups_by_series_key(client):
    body = client.get("/api/series").json()
    by_key = {item["series_key"]: item for item in body["items"]}
    assert by_key["xmen"]["count"] == 4            # #11-#14 stay in ONE series
    assert by_key["xmen"]["series"] == "X-Men"
    assert by_key["ultimatespiderman"]["count"] == 2
    assert by_key["xmen"]["first_year"] == 2025
    assert body["total"] == len(body["items"])


def test_stats_endpoint(client):
    body = client.get("/api/stats").json()
    assert body["total"] == 14
    assert body["errors"] == 2
    assert body["missing"] == 0
    assert body["with_comicinfo"] == 11
    assert body["without_comicinfo"] == 3
    assert body["read"] == 0 and body["unread"] == 14
    assert body["series"] == 9
    assert body["pages"] > 0


def test_rescan_endpoint_uses_the_configured_folder(client):
    body = client.post("/api/scan").json()
    assert body["found"] == 14
    # 12 readable archives are re-indexed in place; the 2 unreadable ones are errors
    assert body["indexed"] == 0 and body["updated"] == 12
    assert body["errors"] == 2
    assert len(body["error_files"]) == 2
    assert body["db"].endswith("longbox.db")
    assert client.get("/api/health").json()["comics"] == 14


def test_api_never_accepts_a_client_supplied_path(client):
    # ids only: an obvious path-injection attempt never reaches file handling
    guess = client.get("/api/comics/..%2F..%2Fetc%2Fpasswd")
    assert guess.status_code in (404, 422)
    assert client.get("/api/comics/0").status_code == 404
    assert client.get("/api/comics/not-an-id").status_code == 422


def test_cbr_error_is_reported_through_the_api(client):
    items = client.get("/api/comics", params={"series": "Batman", "limit": 10}).json()["items"]
    item = next(entry for entry in items if entry["archive_type"] == "cbr")
    assert item["status"] == "error"
    if not archive_mod.rar_tool_available():
        assert "unrar not installed" in item["error_message"]
