"""Spin-wheel title extraction and HTML writer tests (no network)."""

from __future__ import annotations

from pathlib import Path

from watchlist_watcher.models import WatchlistFilm
from watchlist_watcher.spin import (
    build_spin_films,
    extract_titles_from_csv_path,
    extract_titles_from_csv_text,
    film_matches,
    write_spin_html,
)


def test_extract_letterboxd_name_column() -> None:
    text = (
        "Date,Name,Year,Letterboxd URI\n"
        "2024-01-01,Fight Club,1999,https://letterboxd.com/film/fight-club/\n"
        "2024-01-02,Inception,2010,https://letterboxd.com/film/inception/\n"
    )
    assert extract_titles_from_csv_text(text) == ["Fight Club", "Inception"]


def test_extract_tab_separated_watchlist() -> None:
    text = (
        "Date\tName\tYear\tLetterboxd URI\n"
        "8/8/2025\tPulp Fiction\t1994\thttps://boxd.it/29Pq\n"
        "8/8/2025\tInception\t2010\thttps://boxd.it/1skk\n"
    )
    assert extract_titles_from_csv_text(text) == ["Pulp Fiction", "Inception"]


def test_extract_title_column_alias() -> None:
    text = "title,notes\nThe Matrix,cool\nHeat,\n"
    assert extract_titles_from_csv_text(text) == ["The Matrix", "Heat"]


def test_extract_bare_title_list() -> None:
    text = "Fight Club\nInception\nHeat\n"
    assert extract_titles_from_csv_text(text) == ["Fight Club", "Inception", "Heat"]


def test_extract_dedupes_casefold() -> None:
    text = "Name\nHeat\nheat\nHEAT\n"
    assert extract_titles_from_csv_text(text) == ["Heat"]


def test_extract_from_path(tmp_path: Path) -> None:
    path = tmp_path / "list.csv"
    path.write_text("movie\nCarol\nPortrait of a Lady on Fire\n", encoding="utf-8")
    assert extract_titles_from_csv_path(path) == [
        "Carol",
        "Portrait of a Lady on Fire",
    ]


def test_write_spin_html_embeds_titles(tmp_path: Path) -> None:
    out = tmp_path / "spin.html"
    write_spin_html(out, ["Heat", "Carol"], source_label="Test list")
    html = out.read_text(encoding="utf-8")
    assert "Heat" in html
    assert "Carol" in html
    assert "Test list" in html
    assert "spin-btn" in html
    assert "runtime-chips" in html
    assert "genre-chips" in html


def test_film_matches_runtime_genre_and_services() -> None:
    film = {
        "title": "Heat",
        "runtime": 170,
        "genres": ["Crime", "Drama"],
        "decade": 1990,
        "services": ["Netflix"],
    }
    assert film_matches(film, runtime_max=180)
    assert not film_matches(film, runtime_max=120)
    assert film_matches(film, genres=["Crime"])
    assert not film_matches(film, genres=["Comedy"])
    assert film_matches(film, decade=1990)
    assert film_matches(film, available_only=True)
    assert film_matches(film, services=["Netflix"])
    assert not film_matches(film, services=["Tubi"])


def test_build_spin_films_joins_streaming_and_meta(tmp_path: Path) -> None:
    streaming = tmp_path / "watchlist_streaming.csv"
    streaming.write_text(
        "title,year,tmdb_id,streaming,on_my_services,rent,buy,letterboxd_url,"
        "watch_link,last_changed,expires_on,days_left\n"
        "Heat,1995,949,Netflix,Netflix,,,https://boxd.it/29Pq,"
        "https://www.themoviedb.org/movie/949/watch?locale=US,,unknown,unknown\n",
        encoding="utf-8",
    )
    meta = tmp_path / "spin_meta.json"
    meta.write_text(
        '{"949": {"runtime": 170, "genres": ["Crime", "Drama"], "decade": 1990, "year": 1995}}\n',
        encoding="utf-8",
    )
    films = build_spin_films(
        [
            WatchlistFilm(
                name="Heat",
                year=1995,
                letterboxd_uri="https://boxd.it/29Pq",
            )
        ],
        streaming_csv=streaming,
        meta_path=meta,
    )
    assert len(films) == 1
    assert films[0]["runtime"] == 170
    assert "Crime" in films[0]["genres"]
    assert films[0]["services"] == ["Netflix"]
    assert films[0]["decade"] == 1990
