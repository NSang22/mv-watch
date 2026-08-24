"""Movie of the Night expiry enrichment (availability override disabled).

MotN is consulted only for expiry dates and deep links on films TMDB already
places on your services. Availability override is intentionally off: a blind
audit found MotN Prime precision near chance when ``addon`` options were
treated as included-with-Prime. Do not re-enable override without filtering
MotN option ``type`` to subscription/free only and re-running tools/audit.py.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig
from .http_util import HttpClient, HTTPStatusError, TransientHTTPError
from .models import PRESENCE_UNKNOWN, FilmAvailability, confidence_for_provider
from .providers import match_service


logger = logging.getLogger(__name__)

# Included-with-service / free-with-ads only. Never treat rent, buy, or addon
# (Prime channels, etc.) as "on my subscription."
EXPIRY_MOTN_TYPES = frozenset({"subscription", "free"})


@dataclass
class AvailabilityConflict:
    """Legacy disagreement row shape retained for CSV append compatibility."""

    run_date: str
    title: str
    year: Optional[int]
    tmdb_id: int
    letterboxd_url: str
    service: str
    tmdb_says: str
    streaming_availability_says: str
    resolution: str


def enrichment_enabled(config: AppConfig) -> bool:
    """Return True when MotN expiry enrichment should run."""
    sa = config.streaming_availability
    return bool(sa.enabled and config.streaming_availability_api_key)


def mark_all_unverified(films: list[FilmAvailability]) -> None:
    """Fallback when MotN is off: keep TMDB hits with provider-policy confidence."""
    for film in films:
        if film.presence_status == PRESENCE_UNKNOWN:
            continue
        if film.on_my_services:
            film.verification_status = "unverified"
            film.stale_source = ""
            film.stale_tmdb_services = []
            film.motn_checked = False
            for hit in film.on_my_services:
                hit.confidence = confidence_for_provider(hit.canonical_name)
                hit.sources = "tmdb"
                hit.motn_link = None


class ExpiryEnricher:
    """Attach MotN expiry dates; never override TMDB availability."""

    def __init__(self, config: AppConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.failures = 0
        self.attempts = 0
        self.conflicts: list[AvailabilityConflict] = []
        self.covered_catalog_ids: set[str] = set()
        self.covered_services: list[tuple[str, str]] = []
        self._coverage_loaded = False

    def load_region_coverage(self) -> None:
        """Fetch MotN provider coverage for logging only (no override gate)."""
        if self._coverage_loaded:
            return
        self._coverage_loaded = True
        if not enrichment_enabled(self.config):
            return

        url = (
            f"{self.config.streaming_availability.base_url.rstrip('/')}"
            f"/countries/{self.config.region.lower()}"
        )
        headers = {"X-API-Key": self.config.streaming_availability_api_key or ""}
        try:
            data = self.http.get_json(url, headers=headers)
        except (TransientHTTPError, HTTPStatusError) as exc:
            logger.warning(
                "Could not load MotN country coverage for %s: %s.",
                self.config.region,
                exc,
            )
            return

        services = data.get("services") or []
        covered: list[tuple[str, str]] = []
        for service in services:
            service_id = str(service.get("id") or "").strip()
            service_name = str(service.get("name") or service_id).strip()
            if not service_id:
                continue
            self.covered_catalog_ids.add(service_id)
            covered.append((service_id, service_name))
        covered.sort(key=lambda item: item[1].casefold())
        self.covered_services = covered
        logger.info(
            "MotN covers %d providers in %s (expiry only; availability override OFF): %s",
            len(covered),
            self.config.region.upper(),
            ", ".join(f"{name} ({sid})" for sid, name in covered),
        )

    def enrich_all(
        self,
        films: list[FilmAvailability],
        *,
        today: Optional[date] = None,
    ) -> list[AvailabilityConflict]:
        """Attach expiry for films on my services. Returns no availability conflicts."""
        today = today or date.today()
        self.conflicts = []

        if not enrichment_enabled(self.config):
            logger.info(
                "Streaming Availability API unconfigured or disabled. "
                "Keeping TMDB availability; no MotN expiry."
            )
            mark_all_unverified(films)
            return []

        self.load_region_coverage()
        logger.info(
            "MotN availability override is OFF. Using MotN for expiry dates only."
        )

        try:
            for film in films:
                if film.presence_status == PRESENCE_UNKNOWN:
                    continue
                # Always stamp provider-policy confidence on TMDB hits.
                for hit in film.on_my_services:
                    hit.confidence = confidence_for_provider(hit.canonical_name)
                    hit.sources = "tmdb"
                if not film.on_my_services:
                    film.verification_status = ""
                    continue
                self.enrich_one(film, today=today)
        except KeyboardInterrupt:
            logger.warning("Interrupted during Streaming Availability enrichment.")
            raise
        return []

    def enrich_one(self, film: FilmAvailability, *, today: date) -> None:
        """Fetch MotN expiry for one film. Never mutates on_my_services membership."""
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
            film.verification_status = "unverified"
            film.stale_source = ""
            film.stale_tmdb_services = []
            film.motn_checked = False
            for hit in film.on_my_services:
                hit.confidence = confidence_for_provider(hit.canonical_name)
                hit.sources = "tmdb"
                hit.motn_link = None
            logger.warning(
                "Streaming Availability lookup failed for %s (tmdb_id=%s): %s. "
                "Keeping TMDB availability without MotN expiry.",
                film.film.name,
                film.tmdb_id,
                exc,
            )
            return

        film.motn_checked = True
        options = self._options_for_country(data, self.config.region.lower())
        expiry_by_service, motn_links = self._parse_motn_expiry(options)

        for hit in film.on_my_services:
            hit.confidence = confidence_for_provider(hit.canonical_name)
            hit.sources = "tmdb"
            hit.motn_link = motn_links.get(hit.canonical_name)

        kept = {hit.canonical_name for hit in film.on_my_services}
        film.expiry_by_service = {
            name: day for name, day in expiry_by_service.items() if name in kept
        }
        if film.expiry_by_service:
            earliest = min(film.expiry_by_service.values())
            film.expires_on = earliest
            film.days_left = (date.fromisoformat(earliest) - today).days
        else:
            film.expires_on = None
            film.days_left = None

        film.verification_status = "expiry-enriched" if film.expiry_by_service else "checked"
        film.stale_source = ""
        film.stale_tmdb_services = []

    def _parse_motn_expiry(
        self,
        options: list[dict[str, Any]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Extract expiry and deep links from subscription/free options only."""
        expiry_by_service: dict[str, str] = {}
        links_by_service: dict[str, str] = {}

        for option in options:
            option_type = (option.get("type") or "").lower()
            if option_type not in EXPIRY_MOTN_TYPES:
                continue
            # Guard: addon payloads sometimes misuse type; require no addon object
            # when we want plain subscription inclusion.
            if option.get("addon"):
                continue
            service_obj = option.get("service") or {}
            service_id = service_obj.get("id") or ""
            service_name = service_obj.get("name") or service_id
            canonical = self._canonical_for_motn(service_id, service_name)
            if canonical is None:
                continue
            link = (option.get("link") or option.get("videoLink") or "").strip()
            if link and canonical not in links_by_service:
                links_by_service[canonical] = link
            expires_on = self._parse_expires_on(option.get("expiresOn"))
            if expires_on is None:
                continue
            prior = expiry_by_service.get(canonical)
            if prior is None or expires_on < prior:
                expiry_by_service[canonical] = expires_on

        return expiry_by_service, links_by_service

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
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                return None
        return None


def write_conflicts_csv(path: Path, conflicts: list[AvailabilityConflict]) -> None:
    """Append disagreements to conflicts.csv (no-op when conflicts is empty)."""
    if not conflicts:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_date",
        "title",
        "year",
        "tmdb_id",
        "service",
        "tmdb_says",
        "streaming_availability_says",
        "resolution",
        "letterboxd_url",
    ]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for item in conflicts:
            writer.writerow(
                {
                    "run_date": item.run_date,
                    "title": item.title,
                    "year": item.year if item.year is not None else "",
                    "tmdb_id": item.tmdb_id,
                    "service": item.service,
                    "tmdb_says": item.tmdb_says,
                    "streaming_availability_says": item.streaming_availability_says,
                    "resolution": item.resolution,
                    "letterboxd_url": item.letterboxd_url,
                }
            )
    logger.info("Appended %d disagreements to %s", len(conflicts), path)
