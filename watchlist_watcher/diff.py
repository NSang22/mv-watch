"""Compare current availability against prior state."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import DiffEvent, DiffResult, FilmAvailability

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


def build_next_state(
    current: list[FilmAvailability],
    previous: dict[str, Any],
    *,
    leaving_soon_alerts: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Construct the state blob for the next successful run."""
    films: dict[str, Any] = {}
    for item in current:
        films[str(item.tmdb_id)] = {
            "providers": sorted(my_service_names(item)),
            "title": item.film.name,
            "year": item.film.year,
            "letterboxd_uri": item.film.letterboxd_uri,
            "expires_on": item.expires_on,
            "expiry_by_service": item.expiry_by_service,
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
    """
    prev_films: dict[str, Any] = previous.get("films") or {}
    cold_start = not prev_films
    today = today or date.today()
    thresholds = sorted(leaving_soon_thresholds or [], reverse=True)
    alerted: dict[str, str] = dict(previous.get("leaving_soon_alerts") or {})

    result = DiffResult(cold_start=cold_start)
    current_ids = {str(item.tmdb_id) for item in current}

    for item in current:
        key = str(item.tmdb_id)
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
        for provider in sorted(prev_providers - now_providers):
            result.departures.append(
                DiffEvent(
                    kind="departure",
                    title=item.film.name,
                    year=item.film.year,
                    tmdb_id=item.tmdb_id,
                    provider=provider,
                    detail=(
                        f"Departed from {provider} "
                        "(detected after the fact via TMDB snapshot)"
                    ),
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
        on_services = sum(1 for item in current if my_service_names(item))
        result.arrivals = []
        result.departures = []
        result.leaving_soon = []
        result.new_to_watchlist = []
        logger.info(
            "Cold start: recorded %d films (%d on your services). No arrival alerts.",
            len(current),
            on_services,
        )

    # Drop leaving-soon alert keys for films no longer tracked.
    pruned = {
        key: value
        for key, value in alerted.items()
        if key.split(":", 1)[0] in current_ids
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
            item.last_changed = run_ts
        return

    prev_films = previous.get("films") or {}
    for item in current:
        if item.tmdb_id in changed_ids:
            item.last_changed = run_ts
        else:
            prev = prev_films.get(str(item.tmdb_id)) or {}
            item.last_changed = prev.get("last_changed") or previous.get("last_run")
