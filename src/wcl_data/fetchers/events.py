"""Hydrate events → competitions, with city/country backfill."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ..api.client import APIClient
from ..db.repository import Repository
from ..parsers import event_location
from ._logging import ProgressLogger, RateLimitedExceptionLogger

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

# One-off API quirk patched in the original codebase.
CATEGORY_NAME_FIXES = {
    "AL1": "Men AL1",  # event 1462
}


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Pass either `stale_days` (default) or `rows` (used by `pull_new`)."""
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        rows = repo.find_stale("events", stale_days=stale_days)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    ifsc_to_id = {row["ifsc_id"]: row["id"] for row in rows}
    log.info("Hydrating %d event(s).", len(rows))

    cities_missing_country: dict[int, str] = {}  # event_row_id -> city
    city_to_country: dict[str, str] = {}

    ok = fail = 0
    exc_log = RateLimitedExceptionLogger(log)
    progress = ProgressLogger(log, len(rows), "events")
    try:
        for fetched in client.stream("events", ifsc_to_id.keys()):
            progress.tick()
            ev_ifsc = int(fetched.key)
            ev_row_id = ifsc_to_id[ev_ifsc]
            data = fetched.data
            try:
                name = data.get("name")
                city, country = event_location.parse_city_country(name or "")
                if not city:
                    loc = data.get("location")
                    if loc:
                        city = loc.strip().split(",")[0]
                if not country:
                    country = data.get("country")
                if not country and city:
                    # Final fallback: known-unambiguous-city dictionary for the
                    # handful of historical UIAA rows where the API has only a
                    # city and the name carries no country anchor.
                    country = event_location.city_to_iso3(city)

                if city and not country:
                    cities_missing_country[ev_row_id] = city
                if city and country:
                    city_to_country[city] = country

                repo.update_event(
                    ev_row_id,
                    name=name,
                    city=city,
                    country=country,
                    country_iso3=event_location.to_iso3(country),
                    date_start=data.get("local_start_date"),
                    date_end=data.get("local_end_date"),
                    is_paraclimbing=1 if data.get("is_paraclimbing_event") else 0,
                )

                for d_cat in data.get("d_cats") or []:
                    discipline_name = (d_cat.get("discipline_kind") or "").lower()
                    category_name = d_cat.get("category_name") or ""
                    category_name = CATEGORY_NAME_FIXES.get(category_name, category_name)
                    comp_ifsc = d_cat.get("dcat_id")
                    if not (discipline_name and category_name and comp_ifsc is not None):
                        continue

                    discipline_id = repo.upsert_discipline(discipline_name)
                    # Categories are normally pre-seeded by season_leagues, but make
                    # sure we have one even if a category appears here first.
                    cat_row = repo.conn.execute(
                        "SELECT id FROM categories WHERE name = ?", (category_name,)
                    ).fetchone()
                    category_id = cat_row[0] if cat_row else repo.upsert_category(category_name, None)

                    repo.upsert_competition(
                        event_id=ev_row_id,
                        ifsc_id=int(comp_ifsc),
                        discipline_id=discipline_id,
                        category_id=category_id,
                    )

                repo.mark_fetched("events", ev_row_id)
                ok += 1
            except Exception as exc:
                exc_log.log("Failed to parse /events/%s: %s", ev_ifsc, exc)
                fail += 1
    finally:
        # Run the city/country backfills even when the for-loop is unwound by
        # AuthFailureAbort — otherwise events processed before the abort would
        # be left with `city` populated but `country=NULL` until the next
        # successful run, an inconsistent intermediate state.
        for ev_row_id, city in cities_missing_country.items():
            country = city_to_country.get(city)
            if country:
                repo.backfill_event_country_for_row(
                    ev_row_id, country, country_iso3=event_location.to_iso3(country)
                )

        # Cross-batch backfill: any remaining country-NULL event whose city appears
        # on a sibling row in the DB (from a prior run) inherits that country.
        cross_batch = repo.backfill_event_country_from_siblings()
        if cross_batch:
            log.info("Backfilled country on %d event(s) from sibling rows.", cross_batch)

        log.info("Events: %d hydrated, %d failed.", ok, fail)
    return ok, fail
