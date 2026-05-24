"""Hydrate athlete profile fields from /athletes/{id}."""
from __future__ import annotations

import logging
from typing import Optional

from ..api.client import APIClient
from ..db.repository import Repository
from ..parsers.event_location import to_iso3

log = logging.getLogger(__name__)

# Keys on a `cup_rankings[]` entry that are NOT a discipline sub-object.
_CUP_META_KEYS = {"name", "id", "season"}


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: int,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    stale = repo.find_stale("athletes", stale_days=stale_days)
    if limit is not None:
        stale = stale[:limit]
    if not stale:
        return 0, 0

    ifsc_to_id = {row["ifsc_id"]: row["id"] for row in stale}
    log.info("Hydrating %d athlete(s).", len(stale))

    ok = fail = 0
    for fetched in client.stream("athletes", ifsc_to_id.keys()):
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
                    for disc_key, disc_obj in cup.items():
                        if disc_key in _CUP_META_KEYS or not isinstance(disc_obj, dict):
                            continue
                        repo.upsert_cup_ranking(
                            athlete_id=ath_row_id,
                            cup_ifsc_id=cup_ifsc_id,
                            cup_name=cup.get("name"),
                            season=cup.get("season"),
                            discipline=disc_key,
                            d_cat_id=disc_obj.get("d_cat_id"),
                            rank=disc_obj.get("rank"),
                        )

                repo.mark_fetched("athletes", ath_row_id)
            ok += 1
        except Exception as exc:
            log.exception("Failed to parse /athletes/%s: %s", ath_ifsc, exc)
            fail += 1

    log.info("Athletes: %d hydrated, %d failed.", ok, fail)
    return ok, fail
