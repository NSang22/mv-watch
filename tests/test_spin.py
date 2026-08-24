"""Spin-wheel title extraction and HTML writer tests (no network)."""

from __future__ import annotations

from pathlib import Path

from watchlist_watcher.spin import (
    extract_titles_from_csv_path,
    extract_titles_from_csv_text,
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
