"""Hydrate competitions: fetch per-competition rankings, register athletes + results.

Competitions are indexed by (event_ifsc_id, comp_ifsc_id) so the API path is
/events/{event_ifsc_id}/result/{comp_ifsc_id}.

Each per-competition unit of work runs inside a single SQL transaction via
`repo.transaction()`, so a mid-loop failure rolls back the partial state. This
boundary covers both the legacy `results` writes and the per-round structural
tables (`category_rounds`, `round_stages`, `routes`, `round_results`,
`stage_results`, `ascents`). See ADR 0005 and ADR 0007.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, cast

from ..api.client import APIClient
from ..db.repository import Repository

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

# Canonical bracket ordering for speed-final heat NAMES. Used only as a
# fallback when a heat has no `heat_id` (legacy payloads); modern payloads
# always carry heat_id and seq is set to heat_id directly so each physical
# heat gets its own round_stages row.
SPEED_HEAT_SEQ: dict[str, int] = {
    "1/8": 0,
    "1/4": 1,
    "1/2": 2,
    "Small Final": 3,
    "Final": 4,
}


def _norm_score(value: Any) -> Optional[str]:
    """Polymorphic score normalization: empty string → None; else str(value)."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _to_int_bool(value: Any) -> Optional[int]:
    """JSON bool → SQLite int 0/1. Pass through None."""
    if value is None:
        return None
    return 1 if value else 0


def _speed_seq(name: Optional[str]) -> int:
    """Resolve a speed heat name to a stable seq; unknown names → 999."""
    if name is None:
        return 999
    return SPEED_HEAT_SEQ.get(name, 999)


def _ascent_kwargs(ascent: dict[str, Any]) -> dict[str, Any]:
    """Extract the discipline-spanning set of fields from one ascent dict."""
    return dict(
        rank=ascent.get("rank"),
        score=_norm_score(ascent.get("score")),
        status=ascent.get("status"),
        modified=ascent.get("modified"),
        top=_to_int_bool(ascent.get("top")),
        plus=_to_int_bool(ascent.get("plus")),
        corrective_rank=ascent.get("corrective_rank"),
        top_tries=ascent.get("top_tries"),
        restarted=_to_int_bool(ascent.get("restarted")),
        time_ms=ascent.get("time_ms"),
        dnf=_to_int_bool(ascent.get("dnf")),
        dns=_to_int_bool(ascent.get("dns")),
        zone=_to_int_bool(ascent.get("zone")),
        zone_tries=ascent.get("zone_tries"),
        low_zone=_to_int_bool(ascent.get("low_zone")),
        low_zone_tries=ascent.get("low_zone_tries"),
        points=ascent.get("points"),
    )


def _ingest_ascents(
    repo: Repository,
    *,
    ascents: list[dict[str, Any]],
    competition_id: int,
    round_stage_id: int,
    athlete_id: int,
    route_id_by_ifsc: dict[int, int],
    category_round_id: int,
) -> None:
    """Upsert each ascent; create the route on the fly if it wasn't pre-seen."""
    for ascent in ascents:
        if not isinstance(ascent, dict):
            continue
        rt_ifsc = ascent.get("route_id")
        if rt_ifsc is None:
            continue
        rt_local = route_id_by_ifsc.get(int(rt_ifsc))
        if rt_local is None:
            rt_local = repo.upsert_route(
                int(rt_ifsc),
                category_round_id=category_round_id,
                name=ascent.get("route_name"),
            )
            route_id_by_ifsc[int(rt_ifsc)] = rt_local
        repo.upsert_ascent(
            competition_id=competition_id,
            round_stage_id=round_stage_id,
            route_id=rt_local,
            athlete_id=athlete_id,
            **_ascent_kwargs(ascent),
        )


def _process_category_rounds(
    repo: Repository,
    *,
    comp_id: int,
    cr_payload: list[dict[str, Any]],
) -> tuple[
    dict[int, int],
    dict[int, int],
    dict[int, dict[int, int]],
    dict[int, dict[str, int]],
]:
    """Phase A: upsert category_rounds + routes + the structural stages we can
    discover at this level (default stage for simple rounds, one stage per
    combined_stage for combined rounds).

    Returns:
        round_id_by_ifsc: category_round IFSC id -> local id
        route_id_by_ifsc: route IFSC id -> local id
        stage_local_by_round: category_round local id -> {seq -> stage local id}
        combined_stage_by_kind: category_round local id -> {lower(kind) -> stage local id};
            used by phase B to resolve per-athlete combined sub-stages by name/kind
            instead of by position (positions can diverge across athletes).
    """
    round_id_by_ifsc: dict[int, int] = {}
    route_id_by_ifsc: dict[int, int] = {}
    stage_local_by_round: dict[int, dict[int, int]] = {}
    combined_stage_by_kind: dict[int, dict[str, int]] = {}

    for cr in cr_payload:
        if not isinstance(cr, dict):
            continue
        cr_ifsc = cr.get("category_round_id")
        if cr_ifsc is None:
            continue
        cr_local = repo.upsert_category_round(
            int(cr_ifsc),
            competition_id=comp_id,
            kind=cr.get("kind"),
            name=cr.get("name"),
            category=cr.get("category"),
            format=cr.get("format"),
            format_identifier=cr.get("format_identifier"),
            status=cr.get("status"),
            status_as_of=cr.get("status_as_of"),
            league_round_id=(cr.get("round") or {}).get("league_round_id"),
        )
        round_id_by_ifsc[int(cr_ifsc)] = cr_local

        # Collect routes from any of the three possible locations.
        seen_route_ifsc: set[int] = set()

        def _collect_routes(routes: list[dict[str, Any]]) -> None:
            for rt in routes:
                if not isinstance(rt, dict):
                    continue
                rt_ifsc = rt.get("id")
                if rt_ifsc is None or int(rt_ifsc) in seen_route_ifsc:
                    continue
                seen_route_ifsc.add(int(rt_ifsc))
                rt_local = repo.upsert_route(
                    int(rt_ifsc),
                    category_round_id=cr_local,
                    name=rt.get("name"),
                )
                route_id_by_ifsc[int(rt_ifsc)] = rt_local

        _collect_routes(cr.get("routes") or [])
        for sg in cr.get("starting_groups") or []:
            if not isinstance(sg, dict):
                continue
            _collect_routes(sg.get("routes") or [])
        for cs in cr.get("combined_stages") or []:
            if not isinstance(cs, dict):
                continue
            _collect_routes(cs.get("routes") or [])

        # Create round_stages: one per combined_stage if present, else a single
        # default stage. Speed-final heats are discovered lazily in phase B.
        stages_for_round: dict[int, int] = {}
        kind_index: dict[str, int] = {}
        combined_stages = cr.get("combined_stages") or []
        if combined_stages:
            for seq, cs in enumerate(combined_stages):
                if not isinstance(cs, dict):
                    continue
                cs_kind = cs.get("kind")
                stage_local = repo.upsert_round_stage(
                    category_round_id=cr_local,
                    seq=seq,
                    name=(cs_kind.title() if cs_kind else None),
                    kind=cs_kind,
                    combined_stage_ifsc_id=cs.get("id"),
                )
                stages_for_round[seq] = stage_local
                if cs_kind:
                    kind_index[cs_kind.lower()] = stage_local
        else:
            # Default stage seq=0; populated lazily on first use in phase B
            # (we avoid creating it for empty rounds with no athletes).
            pass
        stage_local_by_round[cr_local] = stages_for_round
        if kind_index:
            combined_stage_by_kind[cr_local] = kind_index

    return round_id_by_ifsc, route_id_by_ifsc, stage_local_by_round, combined_stage_by_kind


def _ensure_default_stage(
    repo: Repository,
    *,
    cr_local: int,
    stage_local_by_round: dict[int, dict[int, int]],
) -> int:
    """Get-or-create the default seq=0 stage for a simple round."""
    stages = stage_local_by_round.setdefault(cr_local, {})
    if 0 not in stages:
        stages[0] = repo.upsert_round_stage(category_round_id=cr_local, seq=0)
    return stages[0]


def _ensure_speed_stage(
    repo: Repository,
    *,
    cr_local: int,
    heat: dict[str, Any],
    stage_local_by_round: dict[int, dict[int, int]],
) -> int:
    """Get-or-create a speed-final heat stage.

    One round_stages row per physical heat: keyed by `heat_id` when present,
    so multiple heats sharing a bracket name (e.g. eight 1/8 heats) each get
    their own row. `seq` is set to `heat_id` directly — heat ids are
    monotonically allocated by the World Climbing, so ORDER BY seq preserves chronology.

    If `heat_id` is absent (very old payloads), fall back to the bracket name
    via SPEED_HEAT_SEQ — accepting that multiple unnamed heats would collapse,
    but logging a warning so the gap is visible.
    """
    heat_id = heat.get("heat_id")
    if heat_id is not None:
        seq = int(heat_id)
    else:
        seq = _speed_seq(heat.get("name"))
        log.warning(
            "Speed heat without heat_id (cr_local=%s, name=%r) — falling back "
            "to name-based seq; co-located heats with the same name will collapse",
            cr_local, heat.get("name"),
        )
    stages = stage_local_by_round.setdefault(cr_local, {})
    if seq not in stages:
        stages[seq] = repo.upsert_round_stage(
            category_round_id=cr_local,
            seq=seq,
            name=heat.get("name"),
            heat_id=heat_id,
        )
    return stages[seq]


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Pass either `stale_days` (default) or `rows` (used by `pull_new`).

    Rows shape: `(comp_id, comp_ifsc, event_ifsc)` — the inline JOIN provides this
    when `stale_days` is used; callers passing `rows=` must use the same shape
    (e.g. `repo.find_ongoing_competitions()`).
    """
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        cutoff = repo.stale_cutoff(stale_days)
        rows = list(repo.conn.execute(
            "SELECT c.id AS comp_id, c.ifsc_id AS comp_ifsc, e.ifsc_id AS event_ifsc "
            "FROM competitions c JOIN events e ON c.event_id = e.id "
            "WHERE c.last_fetched_at IS NULL OR c.last_fetched_at < ? "
            "ORDER BY c.id ASC",
            (cutoff,),
        ))
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    log.info("Hydrating %d competition(s).", len(rows))

    items: list[tuple[int, str]] = [
        (cast(int, r["comp_id"]), f"/events/{r['event_ifsc']}/result/{r['comp_ifsc']}")
        for r in rows
    ]

    ok = fail = 0
    for fetched in client.stream_paths(items):
        comp_id = int(fetched.key)
        data = fetched.data
        try:
            with repo.transaction():
                # Wipe athlete-keyed per-round data; structural rows are upserted.
                repo.delete_round_data_for_competition(comp_id)
                repo.delete_results_for_competition(comp_id)

                # Phase A: top-level structure (rounds, routes, stages).
                (
                    round_id_by_ifsc,
                    route_id_by_ifsc,
                    stage_local_by_round,
                    combined_stage_by_kind,
                ) = _process_category_rounds(
                    repo,
                    comp_id=comp_id,
                    cr_payload=data.get("category_rounds") or [],
                )

                # Phase B: per-athlete results.
                for entry in data.get("ranking") or []:
                    if not isinstance(entry, dict):
                        continue
                    athlete_ifsc = entry.get("athlete_id")
                    if athlete_ifsc is None:
                        continue
                    athlete_id = repo.upsert_athlete_skeleton(int(athlete_ifsc))
                    repo.upsert_result(
                        competition_id=comp_id,
                        athlete_id=athlete_id,
                        rank=entry.get("rank"),
                    )

                    for rnd in entry.get("rounds") or []:
                        if not isinstance(rnd, dict):
                            continue
                        cr_ifsc = rnd.get("category_round_id")
                        if cr_ifsc is None:
                            continue
                        cr_local = round_id_by_ifsc.get(int(cr_ifsc))
                        if cr_local is None:
                            # Older payloads sometimes reference a round in the
                            # ranking that isn't in the top-level array.
                            log.debug(
                                "Round %s in ranking but not in category_rounds (comp %d) — materializing skeleton",
                                cr_ifsc, comp_id,
                            )
                            cr_local = repo.upsert_category_round(
                                int(cr_ifsc),
                                competition_id=comp_id,
                                name=rnd.get("round_name"),
                            )
                            round_id_by_ifsc[int(cr_ifsc)] = cr_local

                        repo.upsert_round_result(
                            competition_id=comp_id,
                            category_round_id=cr_local,
                            athlete_id=athlete_id,
                            rank=rnd.get("rank"),
                            score=_norm_score(rnd.get("score")),
                            starting_group=rnd.get("starting_group"),
                        )

                        # Dispatch on the per-round structure.
                        elim_stages = rnd.get("speed_elimination_stages")
                        combined_stages = rnd.get("combined_stages")
                        ascents = rnd.get("ascents")

                        # Old API payloads (pre-2018 events ~500-1100) use a
                        # dict instead of a list for `speed_elimination_stages`,
                        # holding the per-athlete ascents under `ascents[]` plus
                        # some legacy metadata (group_name, route_ranks, …).
                        # Unwrap to feed the default ascents branch.
                        if isinstance(elim_stages, dict):
                            if ascents is None:
                                ascents = elim_stages.get("ascents")
                            elim_stages = None
                        # Same defensive normalization for combined_stages,
                        # in case the API ever returns a dict there too.
                        if isinstance(combined_stages, dict):
                            if ascents is None:
                                ascents = combined_stages.get("ascents")
                            combined_stages = None

                        if elim_stages:
                            for heat in elim_stages:
                                if not isinstance(heat, dict):
                                    continue
                                stage_local = _ensure_speed_stage(
                                    repo,
                                    cr_local=cr_local,
                                    heat=heat,
                                    stage_local_by_round=stage_local_by_round,
                                )
                                repo.upsert_stage_result(
                                    competition_id=comp_id,
                                    round_stage_id=stage_local,
                                    athlete_id=athlete_id,
                                    rank=None,
                                    score=_norm_score(heat.get("score")),
                                    time_ms=heat.get("time"),
                                    winner=_to_int_bool(heat.get("winner")),
                                )
                                _ingest_ascents(
                                    repo,
                                    ascents=heat.get("ascents") or [],
                                    competition_id=comp_id,
                                    round_stage_id=stage_local,
                                    athlete_id=athlete_id,
                                    route_id_by_ifsc=route_id_by_ifsc,
                                    category_round_id=cr_local,
                                )
                        elif combined_stages:
                            for stage in combined_stages:
                                if not isinstance(stage, dict):
                                    continue
                                # Match per-athlete sub-stages to the structural
                                # stages by `stage_name` ↔ `kind` (case-insensitive)
                                # rather than by enumerate position — positions can
                                # diverge if an athlete only competed in some of
                                # the sub-stages.
                                stage_name = stage.get("stage_name")
                                kind_key = stage_name.lower() if stage_name else None
                                kind_index = combined_stage_by_kind.get(cr_local) or {}
                                stages = stage_local_by_round.setdefault(cr_local, {})
                                stage_local = kind_index.get(kind_key) if kind_key else None
                                if stage_local is None:
                                    # True fallback: structural stage missing
                                    # (e.g. round was lazy-created from ranking).
                                    # Allocate a fresh seq and write kind too.
                                    next_seq = max(stages, default=-1) + 1
                                    stage_local = repo.upsert_round_stage(
                                        category_round_id=cr_local,
                                        seq=next_seq,
                                        name=stage_name,
                                        kind=kind_key,
                                    )
                                    stages[next_seq] = stage_local
                                    if kind_key:
                                        combined_stage_by_kind.setdefault(cr_local, {})[kind_key] = stage_local
                                repo.upsert_stage_result(
                                    competition_id=comp_id,
                                    round_stage_id=stage_local,
                                    athlete_id=athlete_id,
                                    rank=stage.get("stage_rank"),
                                    score=_norm_score(stage.get("stage_score")),
                                )
                                _ingest_ascents(
                                    repo,
                                    ascents=stage.get("ascents") or [],
                                    competition_id=comp_id,
                                    round_stage_id=stage_local,
                                    athlete_id=athlete_id,
                                    route_id_by_ifsc=route_id_by_ifsc,
                                    category_round_id=cr_local,
                                )
                        else:
                            # Simple round: ascents directly under the round.
                            stage_local = _ensure_default_stage(
                                repo,
                                cr_local=cr_local,
                                stage_local_by_round=stage_local_by_round,
                            )
                            repo.upsert_stage_result(
                                competition_id=comp_id,
                                round_stage_id=stage_local,
                                athlete_id=athlete_id,
                                rank=rnd.get("rank"),
                                score=_norm_score(rnd.get("score")),
                            )
                            _ingest_ascents(
                                repo,
                                ascents=ascents or [],
                                competition_id=comp_id,
                                round_stage_id=stage_local,
                                athlete_id=athlete_id,
                                route_id_by_ifsc=route_id_by_ifsc,
                                category_round_id=cr_local,
                            )

                repo.mark_fetched("competitions", comp_id)
            ok += 1
        except Exception as exc:
            log.exception("Failed to parse %s: %s", fetched.path, exc)
            fail += 1

    log.info("Competitions: %d hydrated, %d failed.", ok, fail)
    return ok, fail
