"""Tests for grouped departures and the anomaly gate."""

from __future__ import annotations

from watchlist_watcher.anomaly import assess_departure_anomaly
from watchlist_watcher.diff import compute_diff
from watchlist_watcher.models import (
    PRESENCE_VERIFIED_ABSENT,
    PRESENCE_VERIFIED_PRESENT,
    DiffEvent,
    DiffResult,
    FilmAvailability,
    ProviderHit,
    WatchlistFilm,
)


def _film(title: str, tmdb_id: int, providers: list[str]) -> FilmAvailability:
    hits = [ProviderHit(name, name, "flatrate", "subscription") for name in providers]
    return FilmAvailability(
        film=WatchlistFilm(title, 2017, f"https://boxd.it/{tmdb_id}"),
        tmdb_id=tmdb_id,
        watch_link=f"https://www.themoviedb.org/movie/{tmdb_id}/watch?locale=US",
        on_my_services=hits,
        streaming=hits,
        presence_status=(
            PRESENCE_VERIFIED_PRESENT if hits else PRESENCE_VERIFIED_ABSENT
        ),
    )


def test_departures_group_by_film() -> None:
    previous = {
        "films": {
            "391713": {
                "providers": ["Hoopla", "Tubi", "YouTube Free"],
                "title": "Lady Bird",
            }
        },
        "leaving_soon_alerts": {},
    }
    current = [_film("Lady Bird", 391713, [])]
    diff, _ = compute_diff(current, previous)
    assert len(diff.departures) == 1
    event = diff.departures[0]
    assert event.provider == "Hoopla, Tubi, YouTube Free"
    assert event.suspect is True
    assert "SUSPECT" in event.detail


def test_partial_departure_not_suspect() -> None:
    previous = {
        "films": {
            "1": {"providers": ["Netflix", "Tubi"], "title": "Example"},
        },
        "leaving_soon_alerts": {},
    }
    current = [_film("Example", 1, ["Netflix"])]
    diff, _ = compute_diff(current, previous)
    assert len(diff.departures) == 1
    assert diff.departures[0].provider == "Tubi"
    assert diff.departures[0].suspect is False


def test_anomaly_gate_triggers_on_count() -> None:
    diff = DiffResult(
        cold_start=False,
        departures=[
            DiffEvent("departure", f"Film {i}", 2000, i, "Netflix")
            for i in range(11)
        ],
    )
    decision = assess_departure_anomaly(
        diff,
        watchlist_size=200,
        max_departure_films=10,
        max_departure_fraction=0.05,
    )
    assert decision.anomalous is True
    assert "max_departure_films" in decision.message


def test_anomaly_gate_triggers_on_fraction() -> None:
    diff = DiffResult(
        cold_start=False,
        departures=[
            DiffEvent("departure", f"Film {i}", 2000, i, "Netflix")
            for i in range(6)
        ],
    )
    decision = assess_departure_anomaly(
        diff,
        watchlist_size=100,
        max_departure_films=10,
        max_departure_fraction=0.05,
    )
    assert decision.anomalous is True
    assert "max_departure_fraction" in decision.message


def test_anomaly_gate_allows_small_runs() -> None:
    diff = DiffResult(
        cold_start=False,
        departures=[DiffEvent("departure", "Film", 2000, 1, "Netflix")],
    )
    decision = assess_departure_anomaly(
        diff,
        watchlist_size=100,
        max_departure_films=10,
        max_departure_fraction=0.05,
    )
    assert decision.anomalous is False
