"""Filename parsing: the messy-name cases, and the two traps in the owner's data."""

from __future__ import annotations

import pytest

from longbox.parser import normalise_series, parse_filename


@pytest.mark.parametrize(
    "filename,series,volume,issue,year,issue_sort",
    [
        ("Ultimate Spider-Man 001 (2000).cbz", "Ultimate Spider-Man", None, "1", 2000, 1.0),
        ("X-Men_Vol_2_#1.cbz", "X-Men", "2", "1", None, 1.0),
        ("Batman #404 (1987) (digital).cbr", "Batman", None, "404", 1987, 404.0),
        ("Saga 12 (2014) (Digital) (Zone-Empire).cbz", "Saga", None, "12", 2014, 12.0),
        ("X-Men Annual 3 (1990).cbz", "X-Men Annual", None, "3", 1990, 3.0),
    ],
)
def test_required_cases(filename, series, volume, issue, year, issue_sort):
    parsed = parse_filename(filename)
    assert parsed["series"] == series
    assert parsed["volume"] == volume
    assert parsed["issue_number"] == issue
    assert parsed["issue_sort"] == issue_sort
    assert parsed["year"] == year


def test_annual_is_flagged_and_kept_out_of_the_main_series():
    parsed = parse_filename("X-Men Annual 3 (1990).cbz")
    assert parsed["annual"] is True
    # judgement call: the annual keeps its own series name so #3 never collides
    # with X-Men #3 when sorting one series
    assert parsed["series"] == "X-Men Annual"
    assert parse_filename("X-Men 3 (1990).cbz")["series_key"] != parsed["series_key"]


def test_underscores_and_windows_paths():
    parsed = parse_filename(r"C:\Users\jasro\Desktop\comics\X-Men_Vol_2_#1.cbz")
    assert parsed["series"] == "X-Men"
    assert parsed["volume"] == "2"


def test_unicode_and_spaces_in_path():
    parsed = parse_filename("/tmp/Bêtes du Nord Töme 3 (2019) (French) [c2c].cbz")
    assert parsed["series"] == "Bêtes du Nord Töme"
    assert parsed["issue_number"] == "3"
    assert parsed["year"] == 2019
    assert parsed["series_key"] == "betesdunordtome"


def test_series_name_containing_a_year_like_number():
    parsed = parse_filename("Spider-Man 2099 5 (1992).cbz")
    assert parsed["series"] == "Spider-Man 2099"
    assert parsed["issue_number"] == "5"
    assert parsed["year"] == 1992


def test_number_is_a_string_letter_suffix_and_decimal():
    letter = parse_filename("Spider-Man 2099 #1.MU (1992).cbz")
    assert letter["issue_number"] == "1.MU"
    assert letter["issue_sort"] == 1.0
    decimal = parse_filename("Saga #12.5 (2014).cbz")
    assert decimal["issue_number"] == "12.5"
    assert decimal["issue_sort"] == 12.5


def test_no_issue_number_is_not_an_error():
    parsed = parse_filename("Locke & Key One Shot (2011).cbz")
    assert parsed["issue_number"] is None
    assert parsed["issue_sort"] is None
    assert parsed["year"] == 2011
    assert parsed["series"] == "Locke & Key One Shot"


def test_trailing_release_tag_is_stripped_when_there_is_no_issue_number():
    assert parse_filename("Locke & Key One Shot (2011) digital.cbz")["series"] == "Locke & Key One Shot"


def test_noise_word_inside_a_real_series_name_survives():
    # "Broken Scan" is a series name, not a release tag: never strip it blindly
    parsed = parse_filename("Broken Scan 04 (2015).cbz")
    assert parsed["series"] == "Broken Scan"
    assert parsed["issue_number"] == "4"


def test_year_without_brackets():
    parsed = parse_filename("Saga 12 2014.cbz")
    assert parsed["series"] == "Saga"
    assert parsed["issue_number"] == "12"
    assert parsed["year"] == 2014


def test_leading_zeros_normalised_but_sorted_numerically():
    assert parse_filename("Ultimate Spider-Man 009 (2000).cbz")["issue_number"] == "9"
    issues = [parse_filename(f"Saga {n:03d}.cbz")["issue_sort"] for n in (2, 10, 1)]
    assert sorted(issues) == [1.0, 2.0, 10.0]


def test_empty_input_is_handled():
    parsed = parse_filename("")
    assert parsed["series"] is None
    assert parsed["issue_number"] is None


@pytest.mark.parametrize("name", ["X-Men", "x-men", "X Men", "X-MEN", "X: Men"])
def test_series_key_groups_name_variants(name):
    assert normalise_series(name) == "xmen"


def test_series_key_ignores_volume_so_one_series_stays_one_series():
    # the trap from the owner's files: <Volume> is 2024 in some issues and a
    # ComicVine id in others; grouping must not depend on it
    first = parse_filename("X-Men 011 (2025).cbz")
    second = parse_filename("X-Men 012 (2025).cbz")
    assert first["series_key"] == second["series_key"] == "xmen"


def test_series_key_none_for_empty():
    assert normalise_series(None) is None
    assert normalise_series("   ") is None
