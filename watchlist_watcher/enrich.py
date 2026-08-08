"""Optional Streaming Availability API enrichment for expiry dates.

TMDB remains the source of truth for where a film can be watched. This module
only attaches expires_on / days_left when Movie of the Night knows them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from .config import AppConfig
from .http_util import HttpClient, HTTPStatusError, TransientHTTPError
from .models import FilmAvailability
from .providers import match_service

logger = logging.getLogger(__name__)

JUSTWATCH_NOTE = (
    "Streaming availability data provided by JustWatch via TMDB. "
    "Expiry dates provided by Movie of the Night "
    "(https://www.movieofthenight.com/about/api/)."
)


def enrichment_enabled(config: AppConfig) -> bool:
    """Return True when expiry enrichment should run."""
    sa = config.streaming_availability
    return bool(sa.enabled and config.streaming_availability_api_key)


class ExpiryEnricher:
    """Attach known expiry dates without overriding TMDB availability."""

    def __init__(self, config: AppConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.failures = 0
        self.attempts = 0

    def enrich_all(self, films: list[FilmAvailability], *, today: Optional[date] = None) -> None:
        """Enrich films that sit on at least one of the configured services."""
        if not enrichment_enabled(self.config):
            logger.info("Streaming Availability enrichment disabled or key missing.")
            return

        today = today or date.today()
        try:
            for film in films:
                if not film.on_my_services:
                    continue
                self.enrich_one(film, today=today)
        except KeyboardInterrupt:
            logger.warning("Interrupted during expiry enrichment.")
            raise

    def enrich_one(self, film: FilmAvailability, *, today: date) -> None:
        """Query MotN for one film and attach per-service expiry when present."""
        self.attempts += 1
        url = (
            f"{self.config.streaming_availability.base_url.rstrip('/')}"
            f"/shows/movie/{film.tmdb_id}"
        )
        headers = {"X-API-Key": self.config.streaming_availability_api_key or ""}
        try:
            data = self.http.get_json(
                url,
                params={"country": self.config.region.lower()},
                headers=headers,
            )
        except (TransientHTTPError, HTTPStatusError) as exc:
            self.failures += 1
            logger.warning(
                "Streaming Availability lookup failed for tmdb_id=%s: %s",
                film.tmdb_id,
                exc,
            )
            return

        options = self._options_for_country(data, self.config.region.lower())
        expiry_by_service: dict[str, str] = {}

        my_names = {hit.canonical_name for hit in film.on_my_services}
        for option in options:
            service_obj = option.get("service") or {}
            service_id = service_obj.get("id") or ""
            service_name = service_obj.get("name") or service_id
            canonical = self._canonical_for_motn(service_id, service_name)
            if canonical is None or canonical not in my_names:
                continue
            expires_on = self._parse_expires_on(option.get("expiresOn"))
            if expires_on is None:
                continue
            # Keep the earliest expiry when multiple options exist for one service.
            prior = expiry_by_service.get(canonical)
            if prior is None or expires_on < prior:
                expiry_by_service[canonical] = expires_on

        film.expiry_by_service = expiry_by_service
        if expiry_by_service:
            earliest = min(expiry_by_service.values())
            film.expires_on = earliest
            film.days_left = (date.fromisoformat(earliest) - today).days
        else:
            film.expires_on = None
            film.days_left = None

    def _canonical_for_motn(self, service_id: str, service_name: str) -> Optional[str]:
        catalog_ids = self.config.streaming_availability.catalog_ids
        for canonical, catalog_id in catalog_ids.items():
            if service_id == catalog_id:
                return canonical

        matched = match_service(service_name, self.config.services)
        return matched.name if matched else None

    @staticmethod
    def _options_for_country(data: dict[str, Any], country: str) -> list[dict[str, Any]]:
        streaming = data.get("streamingOptions") or {}
        options = streaming.get(country) or streaming.get(country.upper()) or []
        return list(options) if isinstance(options, list) else []

    @staticmethod
    def _parse_expires_on(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        if isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        if isinstance(value, str):
            # Accept already-ISO dates.
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                return None
        return None
