"""Extra tests for enrichment parsing and notifications."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from watchlist_watcher.config import AppConfig, PathsConfig, ServiceConfig, StreamingAvailabilityConfig
from watchlist_watcher.diff import DiffResult
from watchlist_watcher.enrich import ExpiryEnricher
from watchlist_watcher.models import (
    PRESENCE_VERIFIED_PRESENT,
    FilmAvailability,
    ProviderHit,
    WatchlistFilm,
)
from watchlist_watcher.notify import build_notification_body, should_notify


FIXTURES = Path(__file__).parent / "fixtures"


def _minimal_config() -> AppConfig:
    return AppConfig(
        region="US",
        request_delay_seconds=0.0,
        failure_rate_threshold=0.2,
        max_departure_films=10,
        max_departure_fraction=0.05,
        paths=PathsConfig(
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
        ),
        services=[
            ServiceConfig(name="Netflix", match=["Netflix"]),
            ServiceConfig(name="Amazon Prime Video", match=["Amazon Prime Video"]),
        ],
        streaming_availability=StreamingAvailabilityConfig(
            enabled=True,
            catalog_ids={"Netflix": "netflix", "Amazon Prime Video": "prime"},
        ),
        tmdb_api_key="test",
        ntfy_topic=None,
        letterboxd_user=None,
        streaming_availability_api_key="test-key",
    )


def test_enrich_attaches_expiry_without_overriding_providers() -> None:
    payload = json.loads((FIXTURES / "motn_show_550.json").read_text(encoding="utf-8"))
    http = MagicMock()
    http.get_json.return_value = payload
    enricher = ExpiryEnricher(_minimal_config(), http)

    film = FilmAvailability(
        film=WatchlistFilm(
            name="Fight Club",
            year=1999,
            letterboxd_uri="https://letterboxd.com/film/fight-club/",
        ),
        tmdb_id=550,
        watch_link="https://www.themoviedb.org/movie/550/watch?locale=US",
        on_my_services=[
            ProviderHit("Netflix", "Netflix", "flatrate", "subscription"),
            ProviderHit("Amazon Prime Video", "Amazon Prime Video", "flatrate", "subscription"),
        ],
        presence_status=PRESENCE_VERIFIED_PRESENT,
    )
    enricher.enrich_one(film, today=date(2026, 3, 1))

    assert film.on_my_services[0].canonical_name == "Netflix"
    assert "Netflix" in film.expiry_by_service
    assert film.expires_on is not None
    assert film.days_left is not None
    # Prime option in the fixture has no expiresOn.
    assert "Amazon Prime Video" not in film.expiry_by_service


def test_should_notify_rules() -> None:
    cold = DiffResult(cold_start=True)
    assert should_notify(cold) is True

    empty = DiffResult(cold_start=False)
    assert should_notify(empty) is False

    from watchlist_watcher.models import DiffEvent

    with_arrival = DiffResult(
        cold_start=False,
        arrivals=[
            DiffEvent("arrival", "Fight Club", 1999, 550, "Netflix"),
        ],
    )
    assert should_notify(with_arrival) is True


def test_notification_body_is_short() -> None:
    from watchlist_watcher.models import DiffEvent

    diff = DiffResult(
        cold_start=False,
        arrivals=[DiffEvent("arrival", "Fight Club", 1999, 550, "Netflix")],
    )
    title, body = build_notification_body(diff, film_count=10)
    assert "arrived" in title.lower() or "1 arrived" in title
    assert body.count("\n") < 12
    assert "JustWatch" in body
