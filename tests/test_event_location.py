"""Tests for the event-name → (city, country) parser."""
from __future__ import annotations

import pytest

from ifsc_data.parsers.event_location import parse_city_country


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
    city, country = parse_city_country("IFSC Worldcup (B,L) - Innsbruck (AUT) 2024")
    assert (city, country) == ("Innsbruck", "AUT")
