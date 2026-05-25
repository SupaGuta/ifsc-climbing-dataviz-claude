"""Hydrate athlete profile fields from /athletes/{id}."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from ..api.client import APIClient
from ..db.repository import Repository
from ..parsers.event_location import to_iso3
from ._common import resolve_rows
from ._logging import ProgressLogger, RateLimitedExceptionLogger

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

# Keys on a `cup_rankings[]` entry that are NOT a discipline sub-object.
_CUP_META_KEYS = {"name", "id", "season"}

# European Cup payloads use an empty-string discipline key and put the
# discipline in the cup name itself, in one of two layouts:
#   "IFSC-Europe Climbing European Cup 2022 - Lead"   (suffix)
#   "IFSC-Europe Climbing European Cup Lead 2024"     (inline, year-suffix)
# Verified to recover 21/21 distinct names and 5540/5540 rows in prod.
_CUP_NAME_DISCIPLINE_RE = re.compile(
    r" (Lead|Boulder|Speed|Combined|B&L)(?: \d{4})?$",
    re.IGNORECASE,
)
_DISCIPLINE_LABEL = {
    "lead": "lead", "boulder": "boulder", "speed": "speed",
    "combined": "combined", "b&l": "boulder&lead",
}


def _discipline_from_cup_name(name: Optional[str]) -> Optional[str]:
    """Recover the cup-rankings discipline from `name` when the API key is empty.

    Returns the canonical lowercase discipline (matching the IFSC labels used
    by World Cup payloads) or None if `name` doesn't carry a discipline suffix.
    """
    if not name:
        return None
    m = _CUP_NAME_DISCIPLINE_RE.search(name)
    if not m:
        return None
    return _DISCIPLINE_LABEL[m.group(1).lower()]


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Pass either `stale_days` (default) or `rows` (uniform with peers).

    The `rows=` form is accepted for signature symmetry with the other four
    hydrators (seasons/season_leagues/events/competitions); pull_new currently
    drives this fetcher with `stale_days=365_000` to scope to NULL-fetched
    skeletons rather than a pre-computed list.
    """
    rows = resolve_rows(repo, "athletes", rows=rows, stale_days=stale_days, limit=limit)
    if not rows:
        return 0, 0

    ifsc_to_id = {row["ifsc_id"]: row["id"] for row in rows}
    log.info("Hydrating %d athlete(s).", len(rows))

    ok = fail = 0
    exc_log = RateLimitedExceptionLogger(log)
    progress = ProgressLogger(log, len(rows), "athletes")
    for fetched in client.stream("athletes", ifsc_to_id.keys()):
        progress.tick()
        ath_ifsc = int(fetched.key)
        ath_row_id = ifsc_to_id[ath_ifsc]
        data = fetched.data
        try:
            gender_str = (data.get("gender") or "").lower()
            gender = 0 if gender_str == "male" else (1 if gender_str == "female" else None)

            country = data.get("country")
            federation = data.get("federation") or {}
            speed_pb = data.get("speed_personal_best") or {}

            with repo.transaction():
                repo.update_athlete(
                    ath_row_id,
                    firstname=data.get("firstname"),
                    lastname=data.get("lastname"),
                    gender=gender,
                    height=data.get("height"),
                    arm_span=data.get("arm_span"),
                    birthday=data.get("birthday"),
                    city=data.get("city"),
                    country=country,
                    country_iso3=to_iso3(country),
                    photo_url=data.get("photo_url"),
                    federation_id=federation.get("id"),
                    federation_name=federation.get("name"),
                    federation_abbreviation=federation.get("abbreviation"),
                    federation_url=federation.get("url"),
                    paraclimbing_sport_class=data.get("paraclimbing_sport_class"),
                    sport_class_status=data.get("sport_class_status"),
                    sport_class_review_date=data.get("sport_class_review_date"),
                    speed_pb_time=speed_pb.get("time"),
                    speed_pb_date=speed_pb.get("date"),
                    speed_pb_event_name=speed_pb.get("event_name"),
                    speed_pb_round_name=speed_pb.get("round_name"),
                )

                repo.delete_cup_rankings_for_athlete(ath_row_id)
                for cup in data.get("cup_rankings") or []:
                    cup_ifsc_id = cup.get("id")
                    if cup_ifsc_id is None:
                        continue
                    cup_name = cup.get("name")
                    for disc_key, disc_obj in cup.items():
                        if disc_key in _CUP_META_KEYS or not isinstance(disc_obj, dict):
                            continue
                        # European Cup payloads ship an empty discipline
                        # key — recover the label from the cup name.
                        discipline = disc_key or _discipline_from_cup_name(cup_name) or disc_key
                        repo.upsert_cup_ranking(
                            athlete_id=ath_row_id,
                            cup_ifsc_id=cup_ifsc_id,
                            cup_name=cup_name,
                            season=cup.get("season"),
                            discipline=discipline,
                            d_cat_id=disc_obj.get("d_cat_id"),
                            rank=disc_obj.get("rank"),
                        )

                repo.mark_fetched("athletes", ath_row_id)
            ok += 1
        except Exception as exc:
            exc_log.log("Failed to parse /athletes/%s: %s", ath_ifsc, exc)
            fail += 1

    log.info("Athletes: %d hydrated, %d failed.", ok, fail)
    return ok, fail
