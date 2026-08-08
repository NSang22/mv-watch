"""TMDB watch-provider fetching and service alias matching."""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Iterable, Optional

from .config import AppConfig, ServiceConfig
from .http_util import HttpClient, HTTPStatusError, TransientHTTPError
from .models import (
    PAY_BUCKETS,
    WATCHABLE_BUCKETS,
    FilmAvailability,
    ProviderHit,
    ResolvedFilm,
)

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"


def match_service(
    provider_name: str,
    services: Iterable[ServiceConfig],
) -> Optional[ServiceConfig]:
    """Fold a raw TMDB provider name onto a configured service, if any.

    Matching uses case-insensitive fnmatch globs from config. Exclude patterns
    always win, so Amazon Video never folds into Amazon Prime Video.
    """
    name = provider_name.strip()
    lowered = name.casefold()

    for service in services:
        excluded = False
        for pattern in service.exclude:
            if fnmatch.fnmatchcase(lowered, pattern.casefold()):
                excluded = True
                break
        if excluded:
            continue

        for pattern in service.match:
            if fnmatch.fnmatchcase(lowered, pattern.casefold()):
                return service
            # Allow exact-prefix style by also testing a trailing wildcard form
            # when the config entry has no glob metacharacters.
            if not any(ch in pattern for ch in "*?[]"):
                if lowered == pattern.casefold() or lowered.startswith(
                    pattern.casefold() + " "
                ):
                    return service
    return None


def validate_service_matches(
    region_providers: list[str],
    services: list[ServiceConfig],
) -> list[str]:
    """Return canonical service names that match zero providers in the region."""
    unmatched: list[str] = []
    for service in services:
        hits = [p for p in region_providers if match_service(p, [service]) is not None]
        if not hits:
            unmatched.append(service.name)
    return unmatched


class ProviderClient:
    """Fetch and normalize TMDB watch providers for resolved films."""

    def __init__(self, config: AppConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.failures = 0
        self.attempts = 0

    def list_region_providers(self) -> list[dict[str, Any]]:
        """Return TMDB movie providers for the configured region."""
        data = self.http.get_json(
            f"{TMDB_BASE}/watch/providers/movie",
            params={
                "api_key": self.config.tmdb_api_key,
                "watch_region": self.config.region,
            },
        )
        results = data.get("results") or []
        results.sort(key=lambda item: (item.get("provider_name") or "").lower())
        return results

    def warn_unmatched_services(self) -> None:
        """Log loud warnings when configured services match nothing in-region."""
        providers = [p.get("provider_name", "") for p in self.list_region_providers()]
        missing = validate_service_matches(providers, self.config.services)
        for name in missing:
            logger.error(
                "Configured service %r matches ZERO providers in region %s. "
                "Run --list-providers and fix the match patterns in config.yaml.",
                name,
                self.config.region,
            )

    def fetch_all(self, resolved: list[ResolvedFilm]) -> list[FilmAvailability]:
        """Fetch providers for every resolved film, caching progress on interrupt."""
        results: list[FilmAvailability] = []
        try:
            for item in resolved:
                if item.tmdb_id is None:
                    continue
                availability = self.fetch_one(item)
                if availability is not None:
                    results.append(availability)
        except KeyboardInterrupt:
            logger.warning(
                "Interrupted during provider fetch after %d films. Progress is kept in memory for this run.",
                len(results),
            )
            raise
        return results

    def fetch_one(self, item: ResolvedFilm) -> Optional[FilmAvailability]:
        """Fetch and normalize providers for one resolved film."""
        assert item.tmdb_id is not None
        self.attempts += 1
        try:
            data = self.http.get_json(
                f"{TMDB_BASE}/movie/{item.tmdb_id}/watch/providers",
                params={"api_key": self.config.tmdb_api_key},
            )
        except (TransientHTTPError, HTTPStatusError) as exc:
            self.failures += 1
            logger.error(
                "TMDB providers failed for %s (tmdb_id=%s): %s",
                item.film.name,
                item.tmdb_id,
                exc,
            )
            return None

        region = (data.get("results") or {}).get(self.config.region) or {}
        watch_link = region.get("link") or (
            f"https://www.themoviedb.org/movie/{item.tmdb_id}/watch"
            f"?locale={self.config.region}"
        )

        streaming: list[ProviderHit] = []
        on_my: list[ProviderHit] = []
        rent: list[ProviderHit] = []
        buy: list[ProviderHit] = []

        for bucket in WATCHABLE_BUCKETS:
            for entry in region.get(bucket) or []:
                hit = self._to_hit(entry, bucket)
                if hit is None:
                    continue
                streaming.append(hit)
                matched = match_service(hit.raw_name, self.config.services)
                if matched is not None:
                    on_my.append(
                        ProviderHit(
                            canonical_name=matched.name,
                            raw_name=hit.raw_name,
                            bucket=bucket,
                            tier=matched.tier,
                        )
                    )

        for bucket in PAY_BUCKETS:
            target = rent if bucket == "rent" else buy
            for entry in region.get(bucket) or []:
                hit = self._to_hit(entry, bucket, default_tier="pay")
                if hit is not None:
                    target.append(hit)

        return FilmAvailability(
            film=item.film,
            tmdb_id=item.tmdb_id,
            watch_link=watch_link,
            streaming=self._dedupe_hits(streaming),
            on_my_services=self._dedupe_hits(on_my),
            rent=self._dedupe_hits(rent),
            buy=self._dedupe_hits(buy),
        )

    def _to_hit(
        self,
        entry: dict[str, Any],
        bucket: str,
        default_tier: str = "subscription",
    ) -> Optional[ProviderHit]:
        raw = (entry.get("provider_name") or "").strip()
        if not raw:
            return None
        matched = match_service(raw, self.config.services)
        if matched is not None:
            return ProviderHit(
                canonical_name=matched.name,
                raw_name=raw,
                bucket=bucket,
                tier=matched.tier,
            )
        return ProviderHit(
            canonical_name=raw,
            raw_name=raw,
            bucket=bucket,
            tier=default_tier,
        )

    @staticmethod
    def _dedupe_hits(hits: list[ProviderHit]) -> list[ProviderHit]:
        seen: set[tuple[str, str]] = set()
        out: list[ProviderHit] = []
        for hit in hits:
            key = (hit.canonical_name, hit.bucket)
            if key in seen:
                continue
            seen.add(key)
            out.append(hit)
        return out

    @property
    def failure_rate(self) -> float:
        """Fraction of provider lookups that failed."""
        if self.attempts == 0:
            return 0.0
        return self.failures / self.attempts
