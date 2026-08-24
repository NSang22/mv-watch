"""Tests: unknown lookups must not produce false departures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from watchlist_watcher.config import AppConfig, PathsConfig, ServiceConfig, StreamingAvailabilityConfig
from watchlist_watcher.diff import build_next_state, compute_diff
from watchlist_watcher.http_util import TransientHTTPError
from watchlist_watcher.models import (
    PRESENCE_UNKNOWN,
    PRESENCE_VERIFIED_ABSENT,
    PRESENCE_VERIFIED_PRESENT,
    FilmAvailability,
    ProviderHit,
    ResolvedFilm,
    WatchlistFilm,
)
from watchlist_watcher.providers import ProviderClient


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


def _config() -> AppConfig:
    return AppConfig(
        region="US",
        request_delay_seconds=0.0,
        failure_rate_threshold=0.2,
        max_departure_films=10,
        max_departure_fraction=0.05,
        paths=_paths(),
        services=[ServiceConfig(name="Netflix", match=["Netflix"])],
        streaming_availability=StreamingAvailabilityConfig(),
        tmdb_api_key="test",
        ntfy_topic=None,
        letterboxd_user=None,
        streaming_availability_api_key=None,
    )


def _resolved(title: str = "Ex Machina", tmdb_id: int = 264660) -> ResolvedFilm:
    return ResolvedFilm(
        film=WatchlistFilm(title, 2015, "https://boxd.it/7T2k"),
        tmdb_id=tmdb_id,
        resolve_source="test",
    )


def _present(title: str = "Ex Machina", tmdb_id: int = 264660) -> FilmAvailability:
    return FilmAvailability(
        film=WatchlistFilm(title, 2015, "https://boxd.it/7T2k"),
        tmdb_id=tmdb_id,
        watch_link="https://www.themoviedb.org/movie/264660/watch?locale=US",
        on_my_services=[ProviderHit("Netflix", "Netflix", "flatrate", "subscription")],
        presence_status=PRESENCE_VERIFIED_PRESENT,
    )


def test_http_error_is_unknown_not_absent() -> None:
    http = MagicMock()
    http.get_json.side_effect = TransientHTTPError(429, "https://api.themoviedb.org/3/x")
    client = ProviderClient(_config(), http)
    result = client.fetch_one(_resolved())
    assert result.presence_status == PRESENCE_UNKNOWN
    assert result.on_my_services == []
    assert client.unresolved[0].reason.startswith("http_error")


def test_timeout_after_retries_is_unknown() -> None:
    http = MagicMock()
    http.get_json.side_effect = requests.Timeout("timed out")
    client = ProviderClient(_config(), http)
    result = client.fetch_one(_resolved())
    assert result.presence_status == PRESENCE_UNKNOWN
    assert result.unresolved_reason == "network_error"


def test_missing_region_key_is_unknown() -> None:
    http = MagicMock()
    http.get_json.return_value = {"results": {"GB": {"flatrate": []}}}
    client = ProviderClient(_config(), http)
    result = client.fetch_one(_resolved())
    assert result.presence_status == PRESENCE_UNKNOWN
    assert result.unresolved_reason == "missing_region"


def test_empty_region_is_verified_absent() -> None:
    http = MagicMock()
    http.get_json.return_value = {"results": {"US": {"link": "https://example.test"}}}
    client = ProviderClient(_config(), http)
    result = client.fetch_one(_resolved())
    assert result.presence_status == PRESENCE_VERIFIED_ABSENT
    assert result.on_my_services == []


def test_unknown_does_not_fire_departure() -> None:
    previous = {
        "films": {"264660": {"providers": ["Netflix"], "title": "Ex Machina"}},
        "leaving_soon_alerts": {},
    }
    unknown = _present()
    unknown.presence_status = PRESENCE_UNKNOWN
    unknown.on_my_services = []
    diff, _ = compute_diff([unknown], previous)
    assert diff.departures == []
    assert diff.arrivals == []


def test_verified_absent_does_fire_departure() -> None:
    previous = {
        "films": {"264660": {"providers": ["Netflix"], "title": "Ex Machina"}},
        "leaving_soon_alerts": {},
    }
    absent = _present()
    absent.presence_status = PRESENCE_VERIFIED_ABSENT
    absent.on_my_services = []
    diff, _ = compute_diff([absent], previous)
    assert len(diff.departures) == 1
    assert diff.departures[0].provider == "Netflix"


def test_build_next_state_carries_forward_unknown() -> None:
    previous = {
        "films": {
            "264660": {
                "providers": ["Netflix"],
                "title": "Ex Machina",
                "year": 2015,
                "letterboxd_uri": "https://boxd.it/7T2k",
            }
        },
        "leaving_soon_alerts": {},
    }
    unknown = _present()
    unknown.presence_status = PRESENCE_UNKNOWN
    unknown.on_my_services = []
    next_state = build_next_state([unknown], previous)
    assert next_state["films"]["264660"]["providers"] == ["Netflix"]


def test_build_next_state_writes_verified_absent() -> None:
    previous = {
        "films": {"264660": {"providers": ["Netflix"], "title": "Ex Machina"}},
        "leaving_soon_alerts": {},
    }
    absent = _present()
    absent.presence_status = PRESENCE_VERIFIED_ABSENT
    absent.on_my_services = []
    next_state = build_next_state([absent], previous)
    assert next_state["films"]["264660"]["providers"] == []
    assert next_state["films"]["264660"]["presence_status"] == PRESENCE_VERIFIED_ABSENT


def test_skipped_no_tmdb_id_goes_to_unresolved() -> None:
    client = ProviderClient(_config(), MagicMock())
    skipped = ResolvedFilm(
        film=WatchlistFilm("Mystery", None, "https://boxd.it/x"),
        tmdb_id=None,
        resolve_source="unmatched",
    )
    results = client.fetch_all([skipped])
    assert results == []
    assert len(client.unresolved) == 1
    assert client.unresolved[0].reason == "skipped_no_tmdb_id"
