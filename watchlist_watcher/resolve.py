"""Resolve Letterboxd films to TMDB IDs with a permanent disk cache."""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from .config import AppConfig
from .http_util import HttpClient
from .models import ResolvedFilm, WatchlistFilm

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_LINK_RE = re.compile(
    r"https?://(?:www\.)?themoviedb\.org/movie/(\d+)",
    re.IGNORECASE,
)


def year_within_tolerance(
    letterboxd_year: Optional[int],
    tmdb_year: Optional[int],
    tolerance: int = 1,
) -> bool:
    """Return True when release years agree within ``tolerance`` years.

    Missing years on either side fail the check so remakes are not accepted
    from title-only search hits.
    """
    if letterboxd_year is None or tmdb_year is None:
        return False
    return abs(letterboxd_year - tmdb_year) <= tolerance


def _tmdb_release_year(result: dict[str, Any]) -> Optional[int]:
    date = (result.get("release_date") or "").strip()
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


class IdResolver:
    """Map Letterboxd URIs to TMDB movie IDs using overrides, cache, and two tiers."""

    def __init__(self, config: AppConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.cache_path = config.paths.id_cache
        self.overrides_path = config.paths.overrides
        self.unmatched_path = config.paths.unmatched
        self.cache: dict[str, Any] = self._load_json(self.cache_path)
        self.overrides: dict[str, int] = self._load_overrides()
        self.unmatched: list[WatchlistFilm] = []
        self._dirty = False

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    def _load_overrides(self) -> dict[str, int]:
        if not self.overrides_path.exists():
            return {}
        with self.overrides_path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        out: dict[str, int] = {}
        if isinstance(raw, dict):
            for uri, tmdb_id in raw.items():
                out[str(uri).rstrip("/")] = int(tmdb_id)
        return out

    def save_cache(self) -> None:
        """Persist the ID cache and unmatched CSV to disk."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as handle:
            json.dump(self.cache, handle, indent=2, sort_keys=True)
            handle.write("\n")
        self._write_unmatched()
        self._dirty = False

    def _write_unmatched(self) -> None:
        self.unmatched_path.parent.mkdir(parents=True, exist_ok=True)
        with self.unmatched_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["Name", "Year", "Letterboxd URI"],
            )
            writer.writeheader()
            for film in self.unmatched:
                writer.writerow(
                    {
                        "Name": film.name,
                        "Year": film.year if film.year is not None else "",
                        "Letterboxd URI": film.letterboxd_uri,
                    }
                )

    def resolve_all(self, films: list[WatchlistFilm]) -> list[ResolvedFilm]:
        """Resolve every film, flushing cache periodically and on interrupt."""
        resolved: list[ResolvedFilm] = []
        try:
            for index, film in enumerate(films, start=1):
                resolved.append(self.resolve_one(film))
                if self._dirty and index % 10 == 0:
                    self.save_cache()
        except KeyboardInterrupt:
            logger.warning("Interrupted during ID resolution. Saving cache.")
            self.save_cache()
            raise
        self.save_cache()
        return resolved

    def resolve_one(self, film: WatchlistFilm) -> ResolvedFilm:
        """Resolve a single film through overrides, cache, search, then scrape."""
        uri = (film.letterboxd_uri or "").rstrip("/")
        has_uri = bool(uri)

        if has_uri and uri in self.overrides:
            tmdb_id = self.overrides[uri]
            self._remember(uri, tmdb_id, "override", film)
            return ResolvedFilm(film=film, tmdb_id=tmdb_id, resolve_source="override")

        if has_uri:
            cached = self.cache.get(uri)
            if isinstance(cached, dict) and cached.get("tmdb_id"):
                return ResolvedFilm(
                    film=film,
                    tmdb_id=int(cached["tmdb_id"]),
                    resolve_source=str(cached.get("source") or "cache"),
                )
            if cached is False or (
                isinstance(cached, dict) and cached.get("tmdb_id") is None
            ):
                self.unmatched.append(film)
                return ResolvedFilm(film=film, tmdb_id=None, resolve_source="unmatched")

        tmdb_id = self._search_tmdb(film)
        if tmdb_id is not None:
            if has_uri:
                self._remember(uri, tmdb_id, "search", film)
            return ResolvedFilm(film=film, tmdb_id=tmdb_id, resolve_source="search")

        # Favorites / partial export rows may lack a Letterboxd URL. Never hit
        # HTTP with an empty string; fall through to unmatched after search.
        if has_uri:
            tmdb_id = self._scrape_letterboxd_tmdb_id(uri)
            if tmdb_id is not None:
                self._remember(uri, tmdb_id, "letterboxd", film)
                return ResolvedFilm(
                    film=film, tmdb_id=tmdb_id, resolve_source="letterboxd"
                )
            self._remember(uri, None, "unmatched", film)

        self.unmatched.append(film)
        logger.warning("Unmatched: %s (%s) %s", film.name, film.year, uri or "(no uri)")
        return ResolvedFilm(film=film, tmdb_id=None, resolve_source="unmatched")

    def _remember(
        self,
        uri: str,
        tmdb_id: Optional[int],
        source: str,
        film: WatchlistFilm,
    ) -> None:
        self.cache[uri] = {
            "tmdb_id": tmdb_id,
            "source": source,
            "name": film.name,
            "year": film.year,
        }
        self._dirty = True

    def _search_tmdb(self, film: WatchlistFilm) -> Optional[int]:
        params: dict[str, Any] = {
            "api_key": self.config.tmdb_api_key,
            "query": film.name,
            "include_adult": "false",
        }
        if film.year is not None:
            params["year"] = film.year

        data = self.http.get_json(f"{TMDB_BASE}/search/movie", params=params)
        results = data.get("results") or []

        for result in results:
            tmdb_year = _tmdb_release_year(result)
            if year_within_tolerance(film.year, tmdb_year):
                return int(result["id"])

        # Retry without the year filter when Letterboxd year is present but
        # TMDB search-by-year returned nothing useful.
        if film.year is not None and "year" in params:
            params.pop("year")
            data = self.http.get_json(f"{TMDB_BASE}/search/movie", params=params)
            for result in data.get("results") or []:
                tmdb_year = _tmdb_release_year(result)
                if year_within_tolerance(film.year, tmdb_year):
                    return int(result["id"])

        return None

    def _scrape_letterboxd_tmdb_id(self, letterboxd_uri: str) -> Optional[int]:
        if not (letterboxd_uri or "").strip():
            return None
        html = self.http.get_text(letterboxd_uri)
        soup = BeautifulSoup(html, "lxml")

        for anchor in soup.find_all("a", href=True):
            match = TMDB_LINK_RE.search(anchor["href"])
            if match:
                return int(match.group(1))

        match = TMDB_LINK_RE.search(html)
        if match:
            return int(match.group(1))
        return None
