"""Tests for the CSV exporter."""
from __future__ import annotations

import csv
import re

import pytest

from wcl_data.db.repository import Repository
from wcl_data.exporter import DEFAULT_EXPORT_VIEWS, VIEW_NAMES, export_all, export_view


def _seed(memory_db) -> None:
    """Seed a minimal season → league → event → competition → athlete → result chain.

    Uses Germany (IFSC code GER, ISO3 DEU) for both event and athlete so the
    country / country_iso3 split is observable in the export tests.
    """
    repo = Repository(memory_db)
    season_id = repo.upsert_season(2024, year=2024)
    league_id = repo.upsert_league("World Cup")
    event_id = repo.upsert_event_skeleton(100, season_id=season_id, league_id=league_id)
    repo.update_event(
        event_id,
        name="IFSC World Cup - Munich (GER) 2024",
        city="Munich",
        country="GER",
        country_iso3="DEU",
        date_start="2024-06-01",
        date_end="2024-06-03",
        is_paraclimbing=0,
    )
    discipline_id = repo.upsert_discipline("lead")
    category_id = repo.upsert_category("Men", 0)
    comp_id = repo.upsert_competition(
        event_id=event_id, ifsc_id=5,
        discipline_id=discipline_id, category_id=category_id,
    )
    athlete_id = repo.upsert_athlete_skeleton(1364)
    repo.update_athlete(
        athlete_id,
        firstname="Adam",
        lastname="ONDRA",
        gender=0,
        country="GER",
        country_iso3="DEU",
        height=186,
        birthday="1993-02-05",
    )
    repo.upsert_result(competition_id=comp_id, athlete_id=athlete_id, rank=1)


def test_export_all_writes_default_views_only(memory_db, tmp_path):
    """`export_all` writes every default view; `ascents` is registered but opt-in."""
    _seed(memory_db)
    paths = export_all(memory_db, output_dir=tmp_path)

    assert set(paths.keys()) == set(DEFAULT_EXPORT_VIEWS)
    assert "ascents" in VIEW_NAMES
    assert "ascents" not in DEFAULT_EXPORT_VIEWS
    for path in paths.values():
        assert path.exists()
        assert path.suffix == ".csv"
        assert path.parent == tmp_path


def test_export_ascents_works_on_demand(memory_db, tmp_path):
    """`ascents` is excluded from export_all but can still be exported explicitly."""
    _seed(memory_db)
    path = export_view(memory_db, "ascents", output_dir=tmp_path)
    assert path.exists()


def test_export_round_results_joins_through_to_athletes(memory_db, tmp_path):
    """The 'round_results' view should pre-join down to round_name, athlete, etc."""
    _seed(memory_db)
    # Add a category_round + round_result on top of the seed data.
    repo = Repository(memory_db)
    comp = memory_db.execute("SELECT id FROM competitions").fetchone()[0]
    athlete = memory_db.execute("SELECT id FROM athletes").fetchone()[0]
    cr = repo.upsert_category_round(
        9999, competition_id=comp, kind="lead", name="Qualification",
        format="World Climbing: 2 routes", league_round_id=1,
    )
    repo.upsert_round_result(
        competition_id=comp, category_round_id=cr, athlete_id=athlete,
        rank=1, score="7.75", starting_group=None,
    )

    path = export_view(memory_db, "round_results", output_dir=tmp_path)
    with path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["round_name"] == "Qualification"
    assert row["round_kind"] == "lead"
    assert row["round_rank"] == "1"
    assert row["round_score"] == "7.75"
    assert row["athlete_lastname"] == "ONDRA"


def test_export_results_joins_everything(memory_db, tmp_path):
    """The 'results' view should pre-join down to athlete-name + discipline + category."""
    _seed(memory_db)
    path = export_view(memory_db, "results", output_dir=tmp_path)

    with path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["athlete_firstname"] == "Adam"
    assert row["athlete_lastname"] == "ONDRA"
    assert row["athlete_country"] == "GER"
    assert row["athlete_country_iso3"] == "DEU"
    assert row["event_country"] == "GER"
    assert row["event_country_iso3"] == "DEU"
    assert row["event_name"].startswith("IFSC World Cup")
    assert row["season_year"] == "2024"
    assert row["league_name"] == "World Cup"
    assert row["discipline"] == "lead"
    assert row["category"] == "Men"
    assert row["gender"] == "male"
    assert row["rank"] == "1"


def test_export_athletes_translates_gender_to_string(memory_db, tmp_path):
    _seed(memory_db)
    path = export_view(memory_db, "athletes", output_dir=tmp_path)
    with path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["gender"] == "male"  # not "0"


def test_export_events_and_athletes_carry_country_iso3(memory_db, tmp_path):
    """ADR 0008: both the events and athletes default views surface the
    raw federation `country` and the normalized `country_iso3` side by side."""
    _seed(memory_db)

    e_path = export_view(memory_db, "events", output_dir=tmp_path)
    with e_path.open("r", encoding="utf-8") as f:
        e_rows = list(csv.DictReader(f))
    assert e_rows[0]["country"] == "GER"
    assert e_rows[0]["country_iso3"] == "DEU"

    a_path = export_view(memory_db, "athletes", output_dir=tmp_path)
    with a_path.open("r", encoding="utf-8") as f:
        a_rows = list(csv.DictReader(f))
    assert a_rows[0]["country"] == "GER"
    assert a_rows[0]["country_iso3"] == "DEU"


def test_export_filename_carries_utc_timestamp(memory_db, tmp_path):
    _seed(memory_db)
    path = export_view(memory_db, "athletes", output_dir=tmp_path)
    # Expected pattern: athletes_2026-05-22T185030Z.csv
    assert re.fullmatch(r"athletes_\d{4}-\d{2}-\d{2}T\d{6}Z\.csv", path.name), (
        f"unexpected filename: {path.name}"
    )


def test_export_unknown_view_raises(memory_db, tmp_path):
    with pytest.raises(ValueError, match="Unknown view"):
        export_view(memory_db, "not_a_view", output_dir=tmp_path)


def test_export_creates_output_dir_if_missing(memory_db, tmp_path):
    target = tmp_path / "nested" / "exports"
    assert not target.exists()
    _seed(memory_db)
    export_view(memory_db, "seasons", output_dir=target)
    assert target.is_dir()
