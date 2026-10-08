"""ComicInfo.xml parsing, checked against the owner's real sample file."""

from __future__ import annotations

from pathlib import Path

import pytest

from longbox.comicinfo import (
    ComicInfoError,
    classify_volume,
    extract_comicvine_issue_id,
    parse_comicinfo,
    split_multi,
)

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "comicinfo_xmen_11.xml"


@pytest.fixture(scope="module")
def sample_info() -> dict:
    return parse_comicinfo(SAMPLE.read_text(encoding="utf-8"))


def test_real_sample_scalar_fields(sample_info):
    assert sample_info["series"] == "X-Men"
    assert sample_info["number"] == "11"
    assert sample_info["title"] == "Live Capture"
    assert sample_info["year"] == 2025
    assert sample_info["month"] == 4
    assert sample_info["day"] == 1
    assert sample_info["publisher"] == "Marvel"
    assert sample_info["page_count"] == 27
    assert sample_info["scan_information"] == "Marika-Empire"
    assert sample_info["web"].endswith("/4000-1095610/")


def test_real_sample_summary_keeps_newlines(sample_info):
    assert "\n" in sample_info["summary"]
    assert sample_info["summary"].startswith("PLANETFALL!")
    assert "Alaska!" in sample_info["summary"]


def test_real_sample_multi_value_lists(sample_info):
    assert sample_info["characters"] == [
        "Beast [1462]", "Cyclops [1459]", "Juggernaut [1445]", "Kwannon [9709]",
        "Magik [8303]", "Quentin Quire [40583]", "Temper [73388]",
    ]
    assert sample_info["teams"] == ["Hellions [3199]", "O*N*E [40667]", "X-Men [3173]"]
    assert sample_info["locations"] == [
        "Alaska [55827]", "New York State [56310]", "The Factory [67103]",
        "Westchester [57611]", "Xavier Institute [41138]",
    ]
    assert sample_info["writers"] == ["Jed MacKay"]
    assert len(sample_info["cover_artists"]) == 11
    assert "Frank D'Armata" in sample_info["cover_artists"]
    assert sample_info["editors"] == ["C.B. Cebulski", "Martin Biro", "Tom Brevoort"]


def test_real_sample_volume_is_classified_not_grouped(sample_info):
    assert sample_info["volume"] == "2024"
    assert sample_info["volume_kind"] == "volume_year"


def test_real_sample_comicvine_issue_id(sample_info):
    assert sample_info["comicvine_issue_id"] == "1095610"


def test_real_sample_pages(sample_info):
    pages = sample_info["pages"]
    assert len(pages) == 7  # the real file lists all 27; this sample is trimmed
    assert pages[0] == {"image": 0, "size": 3472085, "width": 1988, "height": 3056,
                        "type": "FrontCover"}
    # the last page is junk: smaller, no Type
    assert pages[-1]["width"] == 1080 and pages[-1]["height"] == 1529
    assert pages[-1]["type"] is None


def test_parses_the_same_from_bytes(sample_info):
    assert parse_comicinfo(SAMPLE.read_bytes()) == sample_info


@pytest.mark.parametrize(
    "raw,kind",
    [("2024", "volume_year"), ("158814", "volume_id"), ("158814 ", "volume_id"),
     ("Vol. 2", "other"), (None, None), ("", None)],
)
def test_classify_volume(raw, kind):
    _, classified = classify_volume(raw)
    assert classified == kind


def test_comicvine_id_comes_from_notes_when_web_is_absent():
    notes = "Scraped metadata from ComicVine [CVDB1095610]."
    assert extract_comicvine_issue_id(None, notes) == "1095610"
    assert extract_comicvine_issue_id("https://example.com/volume/4000-555/", None) == "555"


def test_every_field_is_optional():
    parsed = parse_comicinfo('<?xml version="1.0"?><ComicInfo><Series>X-Men</Series></ComicInfo>')
    assert parsed["series"] == "X-Men"
    assert parsed["writers"] == []          # no creator credits at all
    assert parsed["pencillers"] == []
    assert parsed["year"] is None
    assert parsed["volume"] is None
    assert parsed["pages"] == []
    assert parsed["comicvine_issue_id"] is None
    assert parsed["summary"] is None


def test_xml_with_a_namespace_is_understood():
    xml = ('<ComicInfo xmlns="http://example.com/comicinfo"><Series>Saga</Series>'
           "<Number>12</Number></ComicInfo>")
    parsed = parse_comicinfo(xml)
    assert parsed["series"] == "Saga"
    assert parsed["number"] == "12"


def test_broken_xml_raises_a_clear_error():
    with pytest.raises(ComicInfoError):
        parse_comicinfo("<ComicInfo><Series>Not closed</ComicInfo>")


def test_split_multi_ignores_empty_entries():
    assert split_multi("Cyclops [1459], , Beast [1462],") == ["Cyclops [1459]", "Beast [1462]"]
    assert split_multi(None) == []
    assert split_multi("   ") == []
