"""Write CSV/markdown reports and send ntfy push notifications."""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

import requests

from .diff import DiffResult
from .models import FilmAvailability

logger = logging.getLogger(__name__)

JUSTWATCH_ATTRIBUTION = (
    "Streaming availability data provided by JustWatch via The Movie Database (TMDB)."
)
MOTN_ATTRIBUTION = (
    "Cross-check and expiry dates provided by Movie of the Night "
    "(https://www.movieofthenight.com/about/api/) when enrichment is enabled."
)


def _fmt_providers(names: list[str]) -> str:
    return "; ".join(names)


def _service_label(name: str, tier: str) -> str:
    if tier == "library":
        return f"{name} (library, limited)"
    return name


def write_csv(path: Path, films: list[FilmAvailability]) -> None:
    """Write watchlist_streaming.csv sorted by days_left then title."""

    def sort_key(item: FilmAvailability) -> tuple:
        # Unknown expiry sorts after known dates, never as "no expiry".
        days = item.days_left if item.days_left is not None else 10**9
        return (days, item.film.name.lower())

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "title",
        "year",
        "tmdb_id",
        "streaming",
        "on_my_services",
        "confidence",
        "sources",
        "motn_links",
        "rent",
        "buy",
        "letterboxd_url",
        "watch_link",
        "last_changed",
        "expires_on",
        "days_left",
        "verification",
        "stale_source",
        "stale_tmdb_services",
        "presence_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in sorted(films, key=sort_key):
            # Preserve hit order alignment across parallel columns.
            on_hits = []
            seen: set[str] = set()
            for hit in item.on_my_services:
                if hit.canonical_name in seen:
                    continue
                seen.add(hit.canonical_name)
                on_hits.append(hit)
            writer.writerow(
                {
                    "title": item.film.name,
                    "year": item.film.year if item.film.year is not None else "",
                    "tmdb_id": item.tmdb_id,
                    "streaming": _fmt_providers(
                        sorted({h.canonical_name for h in item.streaming})
                    ),
                    "on_my_services": _fmt_providers(
                        [_service_label(h.canonical_name, h.tier) for h in on_hits]
                    ),
                    "confidence": _fmt_providers([h.confidence for h in on_hits]),
                    "sources": _fmt_providers([h.sources for h in on_hits]),
                    "motn_links": _fmt_providers([h.motn_link or "" for h in on_hits]),
                    "rent": _fmt_providers(sorted({h.canonical_name for h in item.rent})),
                    "buy": _fmt_providers(sorted({h.canonical_name for h in item.buy})),
                    "letterboxd_url": item.film.letterboxd_uri,
                    "watch_link": item.watch_link,
                    "last_changed": item.last_changed or "",
                    "expires_on": item.expires_on or "unknown",
                    "days_left": item.days_left if item.days_left is not None else "unknown",
                    "verification": item.verification_status or "",
                    "stale_source": item.stale_source or "",
                    "stale_tmdb_services": _fmt_providers(item.stale_tmdb_services),
                    "presence_status": item.presence_status,
                }
            )


def write_report(
    path: Path,
    films: list[FilmAvailability],
    diff: DiffResult,
    *,
    enrichment_used: bool = False,
) -> None:
    """Write report.md with changes first, then films grouped by service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# Watchlist Streaming Report", ""]

    if diff.cold_start:
        on_my = sum(1 for f in films if f.on_my_services)
        lines.extend(
            [
                "## First run summary",
                "",
                f"Recorded **{len(films)}** resolved films. "
                f"**{on_my}** are on your services today.",
                "",
                "No per-film arrival alerts were sent on this cold start.",
                "",
            ]
        )
    else:
        lines.extend(["## Changes", ""])
        if (
            not diff.arrivals
            and not diff.departures
            and not diff.leaving_soon
            and not diff.new_to_watchlist
        ):
            lines.append("No arrivals, departures, or leaving-soon warnings this run.")
            lines.append("")
        else:
            if diff.leaving_soon:
                lines.append("### Leaving soon (advance warning)")
                lines.append("")
                for event in sorted(diff.leaving_soon, key=lambda e: (e.days_left or 0, e.title)):
                    year = f" ({event.year})" if event.year else ""
                    lines.append(
                        f"- **{event.title}{year}** on {event.provider}: {event.detail}"
                    )
                lines.append("")
            if diff.arrivals:
                lines.append("### Arrivals")
                lines.append("")
                for event in diff.arrivals:
                    year = f" ({event.year})" if event.year else ""
                    lines.append(f"- **{event.title}{year}** arrived on **{event.provider}**")
                lines.append("")
            if diff.departures:
                lines.append("### Departures (detected after the fact)")
                lines.append("")
                lines.append(
                    "Grouped by film. These disappeared from a verified TMDB snapshot "
                    "after the previous run. Lines flagged SUSPECT lost every prior "
                    "my-service at once and usually mean a bad lookup, not a removal."
                )
                lines.append("")
                for event in sorted(diff.departures, key=lambda e: e.title.lower()):
                    year = f" ({event.year})" if event.year else ""
                    flag = " **[SUSPECT]**" if event.suspect else ""
                    lines.append(
                        f"- **{event.title}{year}** left **{event.provider}**"
                        f"{flag} (postmortem)"
                    )
                lines.append("")
            if diff.new_to_watchlist:
                lines.append("### New to watchlist")
                lines.append("")
                for event in diff.new_to_watchlist:
                    year = f" ({event.year})" if event.year else ""
                    lines.append(f"- **{event.title}{year}**: {event.detail}")
                lines.append("")

    disputed_films = [f for f in films if f.stale_tmdb_services]
    if disputed_films:
        # Legacy rows only; MotN availability override no longer writes these.
        lines.extend(
            [
                "## Legacy MotN disagreements (informational)",
                "",
                "Availability override is off; these are not used for ranking.",
                "",
            ]
        )
        for film in sorted(disputed_films, key=lambda item: item.film.name.lower()):
            year = f" ({film.film.year})" if film.film.year else ""
            services = ", ".join(film.stale_tmdb_services)
            lines.append(f"- **{film.film.name}{year}**: {services}")
        lines.append("")

    # Group by my services, sorted by film count descending.
    by_service: dict[str, list[tuple[FilmAvailability, str, Optional[str]]]] = defaultdict(
        list
    )
    tiers: dict[str, str] = {}
    for film in films:
        seen_for_film: set[str] = set()
        for hit in film.on_my_services:
            if hit.canonical_name in seen_for_film:
                continue
            seen_for_film.add(hit.canonical_name)
            by_service[hit.canonical_name].append(
                (film, hit.confidence, hit.motn_link)
            )
            tiers[hit.canonical_name] = hit.tier

    lines.append("## Available on your services")
    lines.append("")
    lines.append(
        "Confidence: **confirmed** (Netflix/Prime; TMDB measured n=36) · "
        "**probable** (Tubi / YouTube Free / Hoopla until audited). "
        "Nothing is dropped for low confidence."
    )
    lines.append("")
    if not by_service:
        lines.append("Nothing on your configured services right now.")
        lines.append("")
    else:
        for service, group in sorted(
            by_service.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())
        ):
            label = _service_label(service, tiers.get(service, "subscription"))
            lines.append(f"### {label} ({len(group)})")
            lines.append("")

            def film_sort(
                row: tuple[FilmAvailability, str, Optional[str]],
            ) -> tuple:
                item, confidence, _link = row
                days = item.days_left if item.days_left is not None else 10**9
                rank = {"probable": 0, "confirmed": 1}.get(confidence, 0)
                return (rank, days, item.film.name.lower())

            for film, confidence, motn_link in sorted(group, key=film_sort):
                year = f" ({film.film.year})" if film.film.year else ""
                if film.expires_on:
                    expiry = f" - expires {film.expires_on} ({film.days_left}d)"
                else:
                    expiry = " - expiry unknown"
                rent_note = ""
                has_library_only = all(h.tier == "library" for h in film.on_my_services)
                if has_library_only and (film.rent or film.buy):
                    rent_note = " (also rent/buy elsewhere; Hoopla is limited)"
                motn_note = f" ([MotN]({motn_link}))" if motn_link else ""
                lines.append(
                    f"- [{film.film.name}{year}]({film.film.letterboxd_uri})"
                    f" `{confidence}`{expiry}{rent_note} "
                    f"([TMDB watch]({film.watch_link})){motn_note}"
                )
            lines.append("")

    lines.append("## Attribution")
    lines.append("")
    lines.append(JUSTWATCH_ATTRIBUTION)
    if enrichment_used:
        lines.append("")
        lines.append(MOTN_ATTRIBUTION)
    lines.append("")
    lines.append(
        "No aggregator is authoritative. Free ad-supported catalogs are the least "
        "reliable. This tool prefers high recall over precision."
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def should_notify(diff: DiffResult) -> bool:
    """Return True when a push notification should be sent."""
    if diff.cold_start:
        return True
    return bool(diff.arrivals or diff.departures or diff.leaving_soon)


def build_notification_body(diff: DiffResult, film_count: int) -> tuple[str, str]:
    """Build ntfy title and short body. Keep the body under a few lines."""
    if diff.cold_start:
        title = "Watchlist watcher: first run"
        body = (
            f"Tracked {film_count} films.\n"
            "State saved. Future runs will alert on arrivals and departures.\n"
            f"{JUSTWATCH_ATTRIBUTION}"
        )
        return title, body

    counts = (
        f"{len(diff.arrivals)} arrived, "
        f"{len(diff.departures)} departed, "
        f"{len(diff.leaving_soon)} leaving soon"
    )
    title = f"Watchlist watcher: {counts}"

    lines = [counts]
    for event in diff.leaving_soon[:5]:
        lines.append(f"Leaving: {event.title} ({event.provider})")
    for event in diff.arrivals[:5]:
        lines.append(f"Arrived: {event.title} on {event.provider}")
    for event in diff.departures[:5]:
        flag = " [SUSPECT]" if event.suspect else ""
        lines.append(f"Left: {event.title} from {event.provider}{flag}")
    remaining = (
        max(0, len(diff.leaving_soon) - 5)
        + max(0, len(diff.arrivals) - 5)
        + max(0, len(diff.departures) - 5)
    )
    if remaining:
        lines.append(f"...and {remaining} more. See report.md.")
    lines.append(JUSTWATCH_ATTRIBUTION)
    return title, "\n".join(lines)


def send_ntfy(topic: str, title: str, body: str) -> None:
    """Publish a push notification to an ntfy topic."""
    url = f"https://ntfy.sh/{topic}" if "://" not in topic else topic
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": "movie_camera",
            "Content-Type": "text/plain; charset=utf-8",
        },
        timeout=30,
    )
    response.raise_for_status()
    logger.info("Sent ntfy notification to %s", topic)


def notify(
    topic: Optional[str],
    diff: DiffResult,
    film_count: int,
) -> None:
    """Send a push when there is something worth noticing."""
    if not topic:
        logger.info("NTFY_TOPIC unset; skipping push notification.")
        return
    if not should_notify(diff):
        if not diff.cold_start:
            logger.info("Diff empty; skipping push notification.")
        return
    # On cold start we still notify with a summary (per requirements).
    title, body = build_notification_body(diff, film_count)
    send_ntfy(topic, title, body)
