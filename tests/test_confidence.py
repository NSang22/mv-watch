"""Confidence tier assignment unit tests."""

from __future__ import annotations

from watchlist_watcher.models import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    confidence_for_provider,
)


def test_netflix_and_prime_are_confirmed() -> None:
    assert confidence_for_provider("Netflix") == CONFIDENCE_CONFIRMED
    assert confidence_for_provider("Amazon Prime Video") == CONFIDENCE_CONFIRMED


def test_free_catalogs_are_probable() -> None:
    assert confidence_for_provider("Tubi") == CONFIDENCE_PROBABLE
    assert confidence_for_provider("YouTube Free") == CONFIDENCE_PROBABLE
    assert confidence_for_provider("Hoopla") == CONFIDENCE_PROBABLE
