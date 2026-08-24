"""Watchlist CSV/zip loading tests (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from watchlist_watcher.watchlist import load_watchlist


def test_load_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "watchlist.csv"
    csv_path.write_text(
        "Date,Name,Year,Letterboxd URI\n"
        "2024-01-01,Fight Club,1999,https://letterboxd.com/film/fight-club/\n",
        encoding="utf-8",
    )
    films = load_watchlist(csv_path)
    assert len(films) == 1
    assert films[0].name == "Fight Club"
    assert films[0].year == 1999


def test_load_tab_separated(tmp_path: Path) -> None:
    tsv_path = tmp_path / "watchlist.csv"
    tsv_path.write_text(
        "Date\tName\tYear\tLetterboxd URI\n"
        "8/8/2025\tPulp Fiction\t1994\thttps://boxd.it/29Pq\n"
        "8/8/2025\tInception\t2010\thttps://boxd.it/1skk\n",
        encoding="utf-8",
    )
    films = load_watchlist(tsv_path)
    assert len(films) == 2
    assert films[0].name == "Pulp Fiction"
    assert films[0].letterboxd_uri.endswith("29Pq")
