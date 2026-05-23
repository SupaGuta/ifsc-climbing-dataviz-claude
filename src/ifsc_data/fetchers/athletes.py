"""Hydrate athlete profile fields from /athletes/{id}."""
from __future__ import annotations

import logging
from typing import Optional

from ..api.client import APIClient
from ..db.repository import Repository

log = logging.getLogger(__name__)


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

            repo.update_athlete(
                ath_row_id,
                firstname=data.get("firstname"),
                lastname=data.get("lastname"),
                gender=gender,
                height=data.get("height"),
                arm_span=data.get("arm_span"),
                birthday=data.get("birthday"),
                city=data.get("city"),
                country=data.get("country"),
                photo_url=data.get("photo_url"),
                is_paraclimbing=1 if data.get("paraclimbing_sport_class") is not None else 0,
            )
            repo.mark_fetched("athletes", ath_row_id)
            ok += 1
        except Exception as exc:
            log.exception("Failed to parse /athletes/%s: %s", ath_ifsc, exc)
            fail += 1

    log.info("Athletes: %d hydrated, %d failed.", ok, fail)
    return ok, fail
