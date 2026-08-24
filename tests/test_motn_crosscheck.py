"""Tests for MotN expiry enrichment (availability override disabled)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from watchlist_watcher.config import (
    AppConfig,
    PathsConfig,
    ServiceConfig,
    StreamingAvailabilityConfig,
)
from watchlist_watcher.enrich import ExpiryEnricher, mark_all_unverified, write_conflicts_csv
from watchlist_watcher.http_util import HTTPStatusError
from watchlist_watcher.models import (
    PRESENCE_VERIFIED_ABSENT,
    PRESENCE_VERIFIED_PRESENT,
    FilmAvailability,
    ProviderHit,
    WatchlistFilm,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _paths() -> PathsConfig:
    return PathsConfig(
        watchlist=Path("watchlist.csv"),
        state=Path("state.json"),
        id_cache=Path("cache/id_cache.json"),
        overrides=Path("overrides.json"),
        unmatched=Path("unmatched.csv"),
        csv_report=Path("watchlist_streaming.csv"),
        markdown_report=Path("report.md"),
        html_report=Path("report.html"),
        spin_html=Path("spin.html"),
        recommend_html=Path("recommend.html"),
        conflicts=Path("conflicts.csv"),
        unresolved=Path("unresolved.csv"),
        feedback=Path("feedback.csv"),
    )


def _config(*, enabled: bool = True, api_key: str | None = "test-key") -> AppConfig:
    return AppConfig(
        region="US",
        request_delay_seconds=0.0,
        failure_rate_threshold=0.2,
        max_departure_films=10,
        max_departure_fraction=0.05,
        paths=_paths(),
        services=[
            ServiceConfig(name="Netflix", match=["Netflix"]),
            ServiceConfig(name="Amazon Prime Video", match=["Amazon Prime Video"]),
            ServiceConfig(name="YouTube Free", match=["YouTube Free"]),
            ServiceConfig(name="Tubi", match=["Tubi TV", "Tubi"]),
            ServiceConfig(name="Hoopla", match=["Hoopla"], tier="library"),
        ],
        streaming_availability=StreamingAvailabilityConfig(
            enabled=enabled,
            catalog_ids={
                "Netflix": "netflix",
                "Amazon Prime Video": "prime",
                "YouTube Free": "youtube",
                "Tubi": "tubi",
                "Hoopla": "hoopla",
            },
        ),
        tmdb_api_key="test",
        ntfy_topic=None,
        letterboxd_user=None,
        streaming_availability_api_key=api_key,
    )


def _film_on(*services: str) -> FilmAvailability:
    hits = []
    for name in services:
        if name == "Hoopla":
            hits.append(ProviderHit(name, name, "free", "library"))
        elif name in {"Tubi", "YouTube Free"}:
            hits.append(ProviderHit(name, name, "ads", "subscription"))
        else:
            hits.append(ProviderHit(name, name, "flatrate", "subscription"))
    return FilmAvailability(
        film=WatchlistFilm(
            name="Ex Machina",
            year=2015,
            letterboxd_uri="https://boxd.it/7T2k",
        ),
        tmdb_id=264660,
        watch_link="https://www.themoviedb.org/movie/264660/watch?locale=US",
        on_my_services=hits,
        streaming=hits,
        presence_status=(
            PRESENCE_VERIFIED_PRESENT if hits else PRESENCE_VERIFIED_ABSENT
        ),
    )


def _enricher(http: MagicMock) -> ExpiryEnricher:
    enricher = ExpiryEnricher(_config(), http)
    enricher.covered_catalog_ids = {"netflix", "prime", "tubi", "youtube"}
    enricher._coverage_loaded = True
    return enricher


def test_motn_does_not_drop_tmdb_netflix_hit() -> None:
    payload = {
        "streamingOptions": {
            "us": [
                {
                    "service": {"id": "youtube", "name": "YouTube Free"},
                    "type": "free",
                    "link": "https://www.youtube.com/watch?v=example",
                }
            ]
        }
    }
    http = MagicMock()
    http.get_json.return_value = payload
    film = _film_on("Netflix", "YouTube Free")
    conflicts = _enricher(http).enrich_all([film], today=date(2026, 8, 8))

    by_name = {hit.canonical_name: hit for hit in film.on_my_services}
    assert set(by_name) == {"Netflix", "YouTube Free"}
    assert by_name["Netflix"].confidence == "confirmed"
    assert by_name["YouTube Free"].confidence == "probable"
    assert by_name["YouTube Free"].motn_link.startswith("https://www.youtube.com/")
    assert film.stale_source == ""
    assert film.stale_tmdb_services == []
    assert conflicts == []


def test_addon_and_rent_do_not_count_as_subscription_expiry() -> None:
    payload = {
        "streamingOptions": {
            "us": [
                {"service": {"id": "prime", "name": "Prime Video"}, "type": "rent"},
                {"service": {"id": "prime", "name": "Prime Video"}, "type": "buy"},
                {
                    "service": {"id": "prime", "name": "Prime Video"},
                    "type": "addon",
                    "addon": {"id": "hbomax"},
                },
            ]
        }
    }
    http = MagicMock()
    http.get_json.return_value = payload
    film = _film_on("Amazon Prime Video")
    _enricher(http).enrich_one(film, today=date(2026, 8, 8))
    assert {h.canonical_name for h in film.on_my_services} == {"Amazon Prime Video"}
    assert film.expires_on is None
    assert film.on_my_services[0].motn_link in (None, "")


def test_tubi_stays_probable_when_motn_silent() -> None:
    payload = {"streamingOptions": {"us": []}}
    http = MagicMock()
    http.get_json.return_value = payload
    film = _film_on("Tubi")
    film.film = WatchlistFilm("Paris, Texas", 1984, "https://boxd.it/29Ts")
    film.tmdb_id = 655
    _enricher(http).enrich_one(film, today=date(2026, 8, 8))
    assert {h.canonical_name for h in film.on_my_services} == {"Tubi"}
    assert film.on_my_services[0].confidence == "probable"


def test_sa_keeps_services_and_attaches_subscription_expiry() -> None:
    payload = json.loads((FIXTURES / "motn_show_550.json").read_text(encoding="utf-8"))
    http = MagicMock()
    http.get_json.return_value = payload
    film = _film_on("Netflix", "Amazon Prime Video")
    film.film = WatchlistFilm("Fight Club", 1999, "https://letterboxd.com/film/fight-club/")
    film.tmdb_id = 550
    _enricher(http).enrich_one(film, today=date(2026, 3, 1))

    names = {hit.canonical_name for hit in film.on_my_services}
    assert names == {"Netflix", "Amazon Prime Video"}
    assert film.stale_source == ""
    assert film.expires_on is not None
    by_name = {hit.canonical_name: hit for hit in film.on_my_services}
    assert by_name["Netflix"].confidence == "confirmed"
    assert by_name["Amazon Prime Video"].confidence == "confirmed"


def test_load_region_coverage_logs_providers() -> None:
    http = MagicMock()
    http.get_json.return_value = {
        "countryCode": "us",
        "services": [
            {"id": "netflix", "name": "Netflix"},
            {"id": "tubi", "name": "Tubi"},
            {"id": "prime", "name": "Prime Video"},
        ],
    }
    enricher = ExpiryEnricher(_config(), http)
    enricher.load_region_coverage()
    assert enricher.covered_catalog_ids == {"netflix", "tubi", "prime"}


def test_unconfigured_falls_back_unverified() -> None:
    film = _film_on("Netflix")
    http = MagicMock()
    conflicts = ExpiryEnricher(_config(enabled=False, api_key=None), http).enrich_all([film])
    http.get_json.assert_not_called()
    assert conflicts == []
    assert film.verification_status == "unverified"


def test_api_failure_keeps_tmdb_as_unverified() -> None:
    http = MagicMock()
    http.get_json.side_effect = HTTPStatusError(503, "https://example.test")
    film = _film_on("Netflix")
    _enricher(http).enrich_one(film, today=date(2026, 8, 8))
    assert film.verification_status == "unverified"
    assert {h.canonical_name for h in film.on_my_services} == {"Netflix"}


def test_conflicts_csv_noop_when_empty(tmp_path: Path) -> None:
    path = tmp_path / "conflicts.csv"
    write_conflicts_csv(path, [])
    assert not path.exists()


def test_mark_all_unverified() -> None:
    film = _film_on("Netflix")
    mark_all_unverified([film])
    assert film.verification_status == "unverified"
    assert film.on_my_services[0].confidence == "confirmed"
