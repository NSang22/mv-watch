"""Unit tests for year-tolerance matching, diff engine, and cold start."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from watchlist_watcher.config import ServiceConfig
from watchlist_watcher.diff import compute_diff, my_service_names
from watchlist_watcher.models import (
    PRESENCE_VERIFIED_ABSENT,
    PRESENCE_VERIFIED_PRESENT,
    FilmAvailability,
    ProviderHit,
    WatchlistFilm,
)
from watchlist_watcher.providers import match_service, validate_service_matches
from watchlist_watcher.resolve import year_within_tolerance


FIXTURES = Path(__file__).parent / "fixtures"


def _film(
    title: str,
    tmdb_id: int,
    providers: list[str],
    *,
    year: int = 1999,
    tiers: dict[str, str] | None = None,
    expiry_by_service: dict[str, str] | None = None,
    days_left: int | None = None,
    expires_on: str | None = None,
) -> FilmAvailability:
    tiers = tiers or {}
    hits = [
        ProviderHit(
            canonical_name=name,
            raw_name=name,
            bucket="flatrate",
            tier=tiers.get(name, "subscription"),
        )
        for name in providers
    ]
    return FilmAvailability(
        film=WatchlistFilm(
            name=title,
            year=year,
            letterboxd_uri=f"https://letterboxd.com/film/{title.lower().replace(' ', '-')}/",
        ),
        tmdb_id=tmdb_id,
        watch_link=f"https://www.themoviedb.org/movie/{tmdb_id}/watch?locale=US",
        streaming=hits,
        on_my_services=hits,
        expires_on=expires_on,
        days_left=days_left,
        expiry_by_service=expiry_by_service or {},
        presence_status=(
            PRESENCE_VERIFIED_PRESENT if hits else PRESENCE_VERIFIED_ABSENT
        ),
    )


class TestYearTolerance:
    def test_exact_match(self) -> None:
        assert year_within_tolerance(1999, 1999) is True

    def test_within_one_year(self) -> None:
        assert year_within_tolerance(1999, 2000) is True
        assert year_within_tolerance(2000, 1999) is True

    def test_outside_tolerance(self) -> None:
        assert year_within_tolerance(1999, 2001) is False

    def test_missing_years_rejected(self) -> None:
        assert year_within_tolerance(None, 1999) is False
        assert year_within_tolerance(1999, None) is False
        assert year_within_tolerance(None, None) is False

    def test_fixture_search_results(self) -> None:
        payload = json.loads((FIXTURES / "tmdb_search_fight_club.json").read_text(encoding="utf-8"))
        years = [
            int(item["release_date"][:4])
            for item in payload["results"]
            if item.get("release_date")
        ]
        # Letterboxd year 1999 accepts the TMDB 1999 hit and rejects 1975.
        assert year_within_tolerance(1999, 1999) is True
        assert year_within_tolerance(1999, 1975) is False
        assert 1999 in years and 1975 in years


class TestProviderMatching:
    def test_netflix_ads_tier_folds(self) -> None:
        services = [
            ServiceConfig(
                name="Netflix",
                match=["Netflix", "Netflix basic with Ads", "Netflix Standard with Ads"],
            )
        ]
        assert match_service("Netflix basic with Ads", services).name == "Netflix"

    def test_amazon_video_is_not_prime(self) -> None:
        services = [
            ServiceConfig(
                name="Amazon Prime Video",
                match=["Amazon Prime Video", "Amazon Prime Video with Ads"],
                exclude=["Amazon Video"],
            )
        ]
        assert match_service("Amazon Prime Video with Ads", services).name == "Amazon Prime Video"
        assert match_service("Amazon Video", services) is None

    def test_tubi_and_youtube_free(self) -> None:
        services = [
            ServiceConfig(name="Tubi", match=["Tubi TV", "Tubi"]),
            ServiceConfig(name="YouTube Free", match=["YouTube Free"]),
        ]
        assert match_service("Tubi TV", services).name == "Tubi"
        assert match_service("YouTube Free", services).name == "YouTube Free"
        assert match_service("YouTube", services) is None

    def test_validate_unmatched_service(self) -> None:
        services = [ServiceConfig(name="Made Up Flix", match=["Made Up Flix"])]
        missing = validate_service_matches(["Netflix", "Tubi TV"], services)
        assert missing == ["Made Up Flix"]


class TestDiffEngine:
    def test_cold_start_no_arrival_flood(self) -> None:
        current = [
            _film("Fight Club", 550, ["Netflix"]),
            _film("Heat", 949, ["Amazon Prime Video"]),
        ]
        diff, _ = compute_diff(current, {"films": {}, "leaving_soon_alerts": {}})
        assert diff.cold_start is True
        assert diff.arrivals == []
        assert diff.departures == []
        assert diff.leaving_soon == []
        assert diff.new_to_watchlist == []

    def test_arrival(self) -> None:
        previous = {
            "films": {
                "550": {"providers": [], "title": "Fight Club"},
            },
            "leaving_soon_alerts": {},
        }
        current = [_film("Fight Club", 550, ["Netflix"])]
        diff, _ = compute_diff(current, previous)
        assert diff.cold_start is False
        assert len(diff.arrivals) == 1
        assert diff.arrivals[0].provider == "Netflix"
        assert diff.arrivals[0].kind == "arrival"

    def test_departure_postmortem(self) -> None:
        previous = {
            "films": {
                "550": {"providers": ["Netflix"], "title": "Fight Club"},
            },
            "leaving_soon_alerts": {},
        }
        current = [_film("Fight Club", 550, [])]
        diff, _ = compute_diff(current, previous)
        assert len(diff.departures) == 1
        assert diff.departures[0].kind == "departure"
        assert diff.departures[0].provider == "Netflix"
        assert "Left Netflix" in diff.departures[0].detail
        assert diff.departures[0].suspect is True

    def test_new_to_watchlist_no_arrival(self) -> None:
        previous = {
            "films": {
                "550": {"providers": ["Netflix"], "title": "Fight Club"},
            },
            "leaving_soon_alerts": {},
        }
        current = [
            _film("Fight Club", 550, ["Netflix"]),
            _film("Heat", 949, ["Tubi"]),
        ]
        diff, _ = compute_diff(current, previous)
        assert diff.arrivals == []
        assert len(diff.new_to_watchlist) == 1
        assert diff.new_to_watchlist[0].tmdb_id == 949
        assert "Tubi" in diff.new_to_watchlist[0].detail

    def test_leaving_soon_dedupes_threshold(self) -> None:
        previous = {
            "films": {
                "550": {"providers": ["Netflix"], "title": "Fight Club"},
            },
            "leaving_soon_alerts": {},
        }
        current = [
            _film(
                "Fight Club",
                550,
                ["Netflix"],
                expiry_by_service={"Netflix": "2026-08-18"},
                expires_on="2026-08-18",
                days_left=10,
            )
        ]
        today = date(2026, 8, 8)
        diff, alerted = compute_diff(
            current,
            previous,
            leaving_soon_thresholds=[14, 3],
            today=today,
        )
        assert len(diff.leaving_soon) == 1
        assert diff.leaving_soon[0].threshold == 14
        assert "550:Netflix:14" in alerted

        # Second run should not re-alert the same threshold.
        diff2, alerted2 = compute_diff(
            current,
            {"films": previous["films"], "leaving_soon_alerts": alerted},
            leaving_soon_thresholds=[14, 3],
            today=today,
        )
        assert diff2.leaving_soon == []
        assert alerted2 == alerted

    def test_leaving_soon_fires_tighter_threshold_later(self) -> None:
        previous = {
            "films": {
                "550": {"providers": ["Netflix"], "title": "Fight Club"},
            },
            "leaving_soon_alerts": {"550:Netflix:14": "2026-08-01"},
        }
        current = [
            _film(
                "Fight Club",
                550,
                ["Netflix"],
                expiry_by_service={"Netflix": "2026-08-10"},
                expires_on="2026-08-10",
                days_left=2,
            )
        ]
        diff, alerted = compute_diff(
            current,
            previous,
            leaving_soon_thresholds=[14, 3],
            today=date(2026, 8, 8),
        )
        assert len(diff.leaving_soon) == 1
        assert diff.leaving_soon[0].threshold == 3
        assert "550:Netflix:3" in alerted

    def test_my_service_names(self) -> None:
        film = _film("X", 1, ["Netflix", "Hoopla"], tiers={"Hoopla": "library"})
        assert my_service_names(film) == {"Netflix", "Hoopla"}


class TestProvidersFixture:
    def test_watch_providers_buckets(self) -> None:
        payload = json.loads(
            (FIXTURES / "tmdb_watch_providers_550.json").read_text(encoding="utf-8")
        )
        us = payload["results"]["US"]
        flatrate_names = {p["provider_name"] for p in us.get("flatrate", [])}
        ads_names = {p["provider_name"] for p in us.get("ads", [])}
        assert "Netflix" in flatrate_names or "Amazon Prime Video" in flatrate_names
        assert "Tubi TV" in ads_names or "YouTube Free" in ads_names
