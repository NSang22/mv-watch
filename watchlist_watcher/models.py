"""Shared data models for watchlist-watcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


WATCHABLE_BUCKETS = ("flatrate", "free", "ads")
PAY_BUCKETS = ("rent", "buy")


@dataclass(frozen=True)
class WatchlistFilm:
    """One row from a Letterboxd watchlist export or scrape."""

    name: str
    year: Optional[int]
    letterboxd_uri: str
    date_added: Optional[str] = None


@dataclass
class ResolvedFilm:
    """Watchlist film after TMDB ID resolution."""

    film: WatchlistFilm
    tmdb_id: Optional[int]
    resolve_source: Optional[str] = None  # override | search | letterboxd | unmatched


@dataclass
class ProviderHit:
    """A single streaming provider entry after alias folding."""

    canonical_name: str
    raw_name: str
    bucket: str  # flatrate | free | ads | rent | buy
    tier: str  # subscription | library | pay


@dataclass
class FilmAvailability:
    """Normalized TMDB watch-provider result for one film."""

    film: WatchlistFilm
    tmdb_id: int
    watch_link: str
    streaming: list[ProviderHit] = field(default_factory=list)
    on_my_services: list[ProviderHit] = field(default_factory=list)
    rent: list[ProviderHit] = field(default_factory=list)
    buy: list[ProviderHit] = field(default_factory=list)
    expires_on: Optional[str] = None  # ISO date for earliest known expiry on my services
    days_left: Optional[int] = None
    expiry_by_service: dict[str, str] = field(default_factory=dict)
    last_changed: Optional[str] = None


@dataclass
class DiffEvent:
    """One availability change worth reporting."""

    kind: str  # arrival | departure | leaving_soon | new_to_watchlist | summary
    title: str
    year: Optional[int]
    tmdb_id: int
    provider: str
    detail: str = ""
    days_left: Optional[int] = None
    threshold: Optional[int] = None


@dataclass
class DiffResult:
    """Outcome of comparing current availability to prior state."""

    cold_start: bool
    arrivals: list[DiffEvent] = field(default_factory=list)
    departures: list[DiffEvent] = field(default_factory=list)
    leaving_soon: list[DiffEvent] = field(default_factory=list)
    new_to_watchlist: list[DiffEvent] = field(default_factory=list)
