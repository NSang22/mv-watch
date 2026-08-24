"""Compare current availability against prior state."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    PRESENCE_UNKNOWN,
    DiffEvent,
    DiffResult,
    FilmAvailability,
)

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 form."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    """Load state.json, or return an empty cold-start structure."""
    if not path.exists():
        return {"last_run": None, "films": {}, "leaving_soon_alerts": {}}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("films", {})
    data.setdefault("leaving_soon_alerts", {})
    data.setdefault("last_run", None)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write state.json atomically enough for daily CI use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def my_service_names(availability: FilmAvailability) -> set[str]:
    """Canonical names of configured services currently offering the film."""
    return {hit.canonical_name for hit in availability.on_my_services}


def is_unknown(item: FilmAvailability) -> bool:
    """Return True when this run could not verify providers for the film."""
    return item.presence_status == PRESENCE_UNKNOWN


def build_next_state(
    current: list[FilmAvailability],
    previous: dict[str, Any],
    *,
    leaving_soon_alerts: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Construct the state blob for the next successful run.

    Unknown lookups never overwrite state. Prior provider sets are carried
    forward until a verified present/absent snapshot arrives.
    """
    prev_films: dict[str, Any] = previous.get("films") or {}
    films: dict[str, Any] = {}

    for item in current:
        key = str(item.tmdb_id)
        if is_unknown(item):
            prior = prev_films.get(key)
            if prior is not None:
                films[key] = dict(prior)
                logger.info(
                    "Carrying forward prior state for %s (tmdb_id=%s); lookup was unknown.",
                    item.film.name,
                    item.tmdb_id,
                )
            continue

        films[key] = {
            "providers": sorted(my_service_names(item)),
            "title": item.film.name,
            "year": item.film.year,
            "letterboxd_uri": item.film.letterboxd_uri,
            "expires_on": item.expires_on,
            "expiry_by_service": item.expiry_by_service,
            "presence_status": item.presence_status,
        }

    return {
        "last_run": utc_now_iso(),
        "films": films,
        "leaving_soon_alerts": leaving_soon_alerts
        if leaving_soon_alerts is not None
        else previous.get("leaving_soon_alerts", {}),
    }


def compute_diff(
    current: list[FilmAvailability],
    previous: dict[str, Any],
    *,
    leaving_soon_thresholds: Optional[list[int]] = None,
    today: Optional[date] = None,
) -> tuple[DiffResult, dict[str, str]]:
    """Compare current availability to prior state.

    Distinguishes arrivals, departures, new-to-watchlist titles, leaving-soon
    warnings, and the cold-start path (no per-film arrival flood).

    Unknown lookups never produce departures or arrivals.
    """
    prev_films: dict[str, Any] = previous.get("films") or {}
    cold_start = not prev_films
    today = today or date.today()
    thresholds = sorted(leaving_soon_thresholds or [], reverse=True)
    alerted: dict[str, str] = dict(previous.get("leaving_soon_alerts") or {})

    result = DiffResult(cold_start=cold_start)
    # Track IDs that remain represented in next state (verified + carried unknown).
    tracked_ids: set[str] = set()

    for item in current:
        key = str(item.tmdb_id)
        if is_unknown(item):
            if key in prev_films:
                tracked_ids.add(key)
            continue

        tracked_ids.add(key)
        now_providers = my_service_names(item)
        prev_entry = prev_films.get(key)

        if cold_start:
            continue

        if prev_entry is None:
            # New to watchlist: report availability without firing arrival alerts.
            services = ", ".join(sorted(now_providers)) or "none of my services"
            result.new_to_watchlist.append(
                DiffEvent(
                    kind="new_to_watchlist",
                    title=item.film.name,
                    year=item.film.year,
                    tmdb_id=item.tmdb_id,
                    provider=services,
                    detail=f"Now on watchlist. Available on: {services}",
                )
            )
            continue

        prev_providers = set(prev_entry.get("providers") or [])
        for provider in sorted(now_providers - prev_providers):
            result.arrivals.append(
                DiffEvent(
                    kind="arrival",
                    title=item.film.name,
                    year=item.film.year,
                    tmdb_id=item.tmdb_id,
                    provider=provider,
                    detail=f"Arrived on {provider}",
                )
            )
        lost = sorted(prev_providers - now_providers)
        if lost:
            # One line per film. Losing every prior provider at once is usually a
            # bad lookup, not a coordinated multi-catalog removal.
            suspect = set(lost) == prev_providers and len(prev_providers) > 0
            services = ", ".join(lost)
            detail = f"Left {services}"
            if suspect:
                detail += " [SUSPECT: lost all prior providers at once]"
            result.departures.append(
                DiffEvent(
                    kind="departure",
                    title=item.film.name,
                    year=item.film.year,
                    tmdb_id=item.tmdb_id,
                    provider=services,
                    detail=detail,
                    suspect=suspect,
                )
            )

        # Leaving-soon warnings from enrichment expiry dates.
        for provider, expires_on in (item.expiry_by_service or {}).items():
            if provider not in now_providers:
                continue
            try:
                expiry_date = date.fromisoformat(expires_on)
            except ValueError:
                continue
            days_left = (expiry_date - today).days
            if days_left < 0:
                continue
            applicable = [
                threshold
                for threshold in thresholds
                if days_left <= threshold
                and f"{item.tmdb_id}:{provider}:{threshold}" not in alerted
            ]
            if not applicable:
                continue
            # Fire the most urgent new threshold once. Mark all currently
            # applicable unfired thresholds so a first sighting at day 2 does
            # not later emit a stale 14-day warning.
            threshold = min(applicable)
            result.leaving_soon.append(
                DiffEvent(
                    kind="leaving_soon",
                    title=item.film.name,
                    year=item.film.year,
                    tmdb_id=item.tmdb_id,
                    provider=provider,
                    detail=f"Leaving {provider} in {days_left} day(s) ({expires_on})",
                    days_left=days_left,
                    threshold=threshold,
                )
            )
            for marked in applicable:
                alerted[f"{item.tmdb_id}:{provider}:{marked}"] = today.isoformat()

    if cold_start:
        on_services = sum(
            1
            for item in current
            if not is_unknown(item) and my_service_names(item)
        )
        result.arrivals = []
        result.departures = []
        result.leaving_soon = []
        result.new_to_watchlist = []
        logger.info(
            "Cold start: recorded %d films (%d on your services). No arrival alerts.",
            sum(1 for item in current if not is_unknown(item)),
            on_services,
        )

    # Drop leaving-soon alert keys for films no longer tracked.
    pruned = {
        key: value
        for key, value in alerted.items()
        if key.split(":", 1)[0] in tracked_ids
    }
    return result, pruned


def apply_last_changed(
    current: list[FilmAvailability],
    previous: dict[str, Any],
    diff: DiffResult,
    run_ts: str,
) -> None:
    """Stamp last_changed on films that gained or lost a my-service provider."""
    changed_ids: set[int] = set()
    for event in diff.arrivals + diff.departures + diff.new_to_watchlist:
        changed_ids.add(event.tmdb_id)
    if diff.cold_start:
        for item in current:
            if not is_unknown(item):
                item.last_changed = run_ts
        return

    prev_films = previous.get("films") or {}
    for item in current:
        if is_unknown(item):
            prev = prev_films.get(str(item.tmdb_id)) or {}
            item.last_changed = prev.get("last_changed") or previous.get("last_run")
            continue
        if item.tmdb_id in changed_ids:
            item.last_changed = run_ts
        else:
            prev = prev_films.get(str(item.tmdb_id)) or {}
            item.last_changed = prev.get("last_changed") or previous.get("last_run")
