"""Shared data models for watchlist-watcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


WATCHABLE_BUCKETS = ("flatrate", "free", "ads")
PAY_BUCKETS = ("rent", "buy")
ADS_FREE_BUCKETS = frozenset({"ads", "free"})

# Provider lookup outcomes used by the diff engine and state writer.
PRESENCE_VERIFIED_PRESENT = "verified-present"
PRESENCE_VERIFIED_ABSENT = "verified-absent"
PRESENCE_UNKNOWN = "unknown"

# Per-service confidence from measured accuracy, not MotN agreement.
# confirmed: Netflix/Prime (TMDB 36/36 on blind audit).
# probable: unaudited free/library catalogs (Tubi / YouTube Free / Hoopla).
# disputed: removed; MotN Prime disagreements were mostly rent/buy/addon noise.
CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_PROBABLE = "probable"

AUDITED_CONFIRMED_SERVICES = frozenset({"Netflix", "Amazon Prime Video"})
UNAULTED_PROBABLE_SERVICES = frozenset({"Tubi", "YouTube Free", "Hoopla"})


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
    # confirmed | probable
    confidence: str = CONFIDENCE_PROBABLE
    # Always tmdb while MotN availability override is off.
    sources: str = "tmdb"
    motn_link: Optional[str] = None


def confidence_for_provider(canonical_name: str) -> str:
    """Assign confidence from audit evidence, not source agreement."""
    if canonical_name in AUDITED_CONFIRMED_SERVICES:
        return CONFIDENCE_CONFIRMED
    return CONFIDENCE_PROBABLE


def is_low_reliability_provider(hit: ProviderHit) -> bool:
    """Unaudited free/library catalogs are treated as least reliable."""
    return (
        hit.canonical_name in UNAULTED_PROBABLE_SERVICES
        or hit.bucket in ADS_FREE_BUCKETS
        or hit.tier == "library"
    )


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
    # verified-present | verified-absent | unknown
    presence_status: str = PRESENCE_UNKNOWN
    unresolved_reason: str = ""
    expires_on: Optional[str] = None  # ISO date for earliest known expiry on my services
    days_left: Optional[int] = None
    expiry_by_service: dict[str, str] = field(default_factory=dict)
    # Legacy field; MotN availability override no longer populates this.
    stale_tmdb_services: list[str] = field(default_factory=list)
    stale_source: str = ""
    # expiry-enriched | checked | unverified | ""
    verification_status: str = ""
    motn_checked: bool = False
    last_changed: Optional[str] = None


@dataclass
class UnresolvedLookup:
    """A film whose provider lookup could not be trusted this run."""

    title: str
    year: Optional[int]
    tmdb_id: Optional[int]
    letterboxd_uri: str
    reason: str


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
    # True when a departure wiped every previously known my-service at once.
    suspect: bool = False


@dataclass
class DiffResult:
    """Outcome of comparing current availability to prior state."""

    cold_start: bool
    arrivals: list[DiffEvent] = field(default_factory=list)
    departures: list[DiffEvent] = field(default_factory=list)
    leaving_soon: list[DiffEvent] = field(default_factory=list)
    new_to_watchlist: list[DiffEvent] = field(default_factory=list)
