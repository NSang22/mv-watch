"""Departure anomaly gate helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .models import DiffResult


@dataclass(frozen=True)
class AnomalyDecision:
    """Outcome of the departure sanity gate."""

    anomalous: bool
    departure_films: int
    watchlist_size: int
    fraction: float
    message: str = ""


def assess_departure_anomaly(
    diff: DiffResult,
    *,
    watchlist_size: int,
    max_departure_films: int,
    max_departure_fraction: float,
) -> AnomalyDecision:
    """Return whether this run's departures look too large to trust."""
    departure_films = len(diff.departures)
    fraction = (departure_films / watchlist_size) if watchlist_size else 0.0
    over_count = departure_films > max_departure_films
    over_fraction = fraction > max_departure_fraction
    if not (over_count or over_fraction):
        return AnomalyDecision(
            anomalous=False,
            departure_films=departure_films,
            watchlist_size=watchlist_size,
            fraction=fraction,
        )

    reasons: list[str] = []
    if over_count:
        reasons.append(
            f"{departure_films} films exceeded max_departure_films={max_departure_films}"
        )
    if over_fraction:
        reasons.append(
            f"{fraction:.1%} of the watchlist exceeded "
            f"max_departure_fraction={max_departure_fraction:.0%}"
        )
    message = (
        "ANOMALOUS RUN: departure flood detected "
        f"({'; '.join(reasons)}). "
        "A real catalog does not shed that many titles overnight across "
        "unrelated services. Skipping state write and notifications. "
        "Investigate unresolved.csv / TMDB health, then re-run."
    )
    return AnomalyDecision(
        anomalous=True,
        departure_films=departure_films,
        watchlist_size=watchlist_size,
        fraction=fraction,
        message=message,
    )
