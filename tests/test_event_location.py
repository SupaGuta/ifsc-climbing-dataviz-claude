"""Tests for the event-name → (city, country) parser."""
from __future__ import annotations

import pytest

from wcl_data.parsers.event_location import (
    parse_city_country,
    postprocess_city,
    to_iso3,
)


@pytest.mark.parametrize("name,city,country", [
    # Explicit "(ISO3)" anchor — the common modern format.
    ("IFSC Climbing Worldcup (L,S) - Chamonix (FRA) 2019", "Chamonix", "FRA"),
    ("IFSC Climbing World Championships - Hachioji (JPN) 2019", "Hachioji", "JPN"),
    ("Olympic Games (C) - Tokyo (JPN) 2020", "Tokyo", "JPN"),
    # Country word in parens (still needs a separator before the city)
    ("IFSC World Cup - Innsbruck (Austria) 2023", "Innsbruck", "AUT"),
    # "..., USA <year>" — country-token-before-year branch
    ("IFSC Climbing Worldcup - Vail, USA 1997", "Vail", "USA"),
])
def test_parse_extracts_city_and_country(name, city, country):
    got_city, got_country = parse_city_country(name)
    assert got_country == country
    assert got_city == city


def test_parse_returns_none_for_unparseable():
    city, country = parse_city_country("Just a vague title")
    assert (city, country) == (None, None)


def test_us_state_suffix_stripped_only_when_country_is_usa():
    city, country = parse_city_country("IFSC Climbing Worldcup - Salt Lake City Utah (USA) 2021")
    assert country == "USA"
    assert city == "Salt Lake City"


def test_discipline_block_does_not_confuse_country_extraction():
    city, country = parse_city_country("World Climbing Worldcup (B,L) - Innsbruck (AUT) 2024")
    assert (city, country) == ("Innsbruck", "AUT")


# --- A2: validate (XXX) parens against the known ISO3/IOC set --------------

@pytest.mark.parametrize("name", [
    # `(CMA)` is a known typo for `(CHN)` in event ifsc_id=511; the parser
    # should reject it and let the fetcher fall through to the API field.
    "Promo Event (L+S) Asia Cup - Changsha (CMA)",
    # Fully fabricated unknown code — must not pollute the country column.
    "Climbing Cup - Springfield (XYZ) 2024",
])
def test_unknown_iso3_in_parens_is_rejected(name):
    _, country = parse_city_country(name)
    assert country is None


def test_last_known_iso3_wins_over_earlier_unknown():
    # If a stray unknown code appears earlier and a known code later, the
    # later known code should anchor (and the earlier garbage should not).
    _, country = parse_city_country("Cup (XYZ) - Chamonix (FRA) 2020")
    assert country == "FRA"


@pytest.mark.parametrize("code", [
    # IFSC/IOC federation codes that pycountry doesn't recognize as ISO3 but
    # are nevertheless real anchors in event names. Without these in FALLBACK,
    # the post-A2 validation drops ~200 events worth of country signal.
    "IRI",  # Iran (IFSC)
    "SIN",  # Singapore (IFSC)
    "INA",  # Indonesia (IFSC)
    "GER",  # Germany (IFSC)
    "SUI",  # Switzerland (IFSC)
    "NED",  # Netherlands (IFSC)
    "POR",  # Portugal (IFSC)
    "SLO",  # Slovenia (IFSC)
    "BUL",  # Bulgaria (IFSC)
    "KSA",  # Saudi Arabia (IOC)
    "GUA",  # Guatemala (IOC)
])
def test_ifsc_and_ioc_variants_are_accepted(code):
    _, country = parse_city_country(f"Climbing Cup - City ({code}) 2020")
    assert country == code


# --- to_iso3 normalization (ADR 0008) --------------------------------------

@pytest.mark.parametrize("raw,iso3", [
    # IFSC variants → canonical ISO3
    ("GER", "DEU"), ("SUI", "CHE"), ("NED", "NLD"), ("POR", "PRT"),
    ("SLO", "SVN"), ("BUL", "BGR"), ("CRO", "HRV"), ("GRE", "GRC"),
    ("RSA", "ZAF"), ("PHI", "PHL"),
    ("INA", "IDN"), ("IRI", "IRN"), ("MAS", "MYS"), ("SIN", "SGP"),
    # IOC-only variants
    ("KSA", "SAU"), ("GUA", "GTM"), ("CHI", "CHL"), ("TPE", "TWN"),
    # IFSC-internal oddities folded to the right ISO3
    ("CFR", "RUS"),  # historical RusClim federation code
    ("CMA", "CHN"),  # typo in event ifsc_id=511
    # Already-ISO3 codes pass through unchanged
    ("FRA", "FRA"), ("JPN", "JPN"), ("USA", "USA"), ("CHN", "CHN"),
    ("BRA", "BRA"), ("MAC", "MAC"), ("KOR", "KOR"),
    # Empty / None
    (None, None), ("", None),
])
def test_to_iso3_normalization(raw, iso3):
    assert to_iso3(raw) == iso3


# --- C: "Event - Country" fallback (no parens, no year) --------------------

@pytest.mark.parametrize("name,country", [
    ("Oceanian Championship - New Zealand", "NZL"),
    ("Asian Indoor Games - Macau", "MAC"),
    ("UIAA Worldcup - Russia", "RUS"),
])
def test_trailing_country_name_after_separator(name, country):
    city, got_country = parse_city_country(name)
    assert got_country == country
    # These names don't carry a city in a recoverable position; do not
    # invent one.
    assert city is None


@pytest.mark.parametrize("name", [
    # Trailing non-country word — must not match.
    "Centre and South American Continental Championship Sport Climbing - Senior",
    # No separator at all — must not match.
    "Asian Continental Championship",
])
def test_trailing_non_country_does_not_match(name):
    city, country = parse_city_country(name)
    assert (city, country) == (None, None)


# --- postprocess_city heuristics (C12) -------------------------------------
#
# Each case targets a specific branch in `postprocess_city`. Names are drawn
# from real event titles where a naive city extraction would have left
# meaningful noise — the goal of the heuristics is to scrub it without
# inventing data.

@pytest.mark.parametrize("raw_city,country,event_name,expected", [
    # `None` city stays None.
    (None, None, "anything", None),
    # Blank / whitespace-only collapses to None.
    ("   ", None, "anything", None),

    # "rock junior" events that produce 'Rock' / 'Rock Junior' as city → drop.
    ("Rock Junior", None, "Rock Junior Bouldering 2018", None),
    ("rock", None, "Rock Junior Bouldering 2018", None),

    # "of " prefix gets stripped.
    ("of Innsbruck", "AUT", "anything", "Innsbruck"),

    # Promotional / event-naming prefixes get stripped.
    ("Speed Rock Barcelona", "ESP", "Speed Rock Festival", "Barcelona"),
    ("Blocmaster Praha", "CZE", "Blocmaster Series", "Praha"),
    ("Bouldertag Salzburg", "AUT", "Bouldertag Open", "Salzburg"),
    ("Master Innsbruck", "AUT", "Master Series", "Innsbruck"),

    # "boulder " prefix gets stripped UNLESS the only word is "boulder".
    ("Boulder Vail", "USA", "Boulder Worldcup", "Vail"),
    ("Boulder", "USA", "Boulder", "Boulder"),
    # Quirk: "Boulder Colorado" with country=USA first strips "Boulder " then
    # leaves "Colorado" — the US-state strip only fires when there's still a
    # leading word, so a single-word state name survives. Documenting this so
    # a future change to either branch becomes a visible test failure.
    ("Boulder Colorado", "USA", "anything", "Colorado"),

    # Namba Hips suffix scrub.
    ("Osaka Hips", "JPN", "Namba Hips Bouldering 2018", "Osaka"),

    # China-specific "Province" suffix.
    ("Sichuan Province", "CHN", "anything", "Sichuan"),
    ("Sichuan province", "CHN", "anything", "Sichuan"),
    # NOT stripped when country isn't CHN.
    ("Sichuan Province", "USA", "anything", "Sichuan Province"),

    # US state suffix stripping (country must be USA).
    ("Salt Lake City Utah", "USA", "anything", "Salt Lake City"),
    ("Vail Colorado", "USA", "anything", "Vail"),
    # Two-word state (New York, New Mexico, North Carolina, etc.)
    ("Albany New York", "USA", "anything", "Albany"),
    # NOT stripped when country isn't USA.
    ("Salt Lake City Utah", "AUT", "anything", "Salt Lake City Utah"),

    # tidy_case: ALL-CAPS gets Title-Cased.
    ("CHAMONIX", "FRA", "anything", "Chamonix"),
    ("HONG KONG", "HKG", "anything", "Hong Kong"),
    # Mixed-case is left alone (not >90% uppercase).
    ("Chamonix", "FRA", "anything", "Chamonix"),
])
def test_postprocess_city_heuristics(raw_city, country, event_name, expected):
    assert postprocess_city(raw_city, country, event_name) == expected
