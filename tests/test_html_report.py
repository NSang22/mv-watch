"""Tests for HTML report generation from CSV."""

from __future__ import annotations

from pathlib import Path

from watchlist_watcher.html_report import payload_from_csv, write_html_report


def test_render_html_from_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "watchlist_streaming.csv"
    csv_path.write_text(
        "title,year,tmdb_id,streaming,on_my_services,rent,buy,letterboxd_url,"
        "watch_link,last_changed,expires_on,days_left\n"
        "1917,2019,530915,Netflix,Netflix,,,https://boxd.it/jj4y,"
        "https://www.themoviedb.org/movie/530915/watch?locale=US,,unknown,unknown\n"
        "Pulp Fiction,1994,680,Amazon Prime Video,Amazon Prime Video,,,"
        "https://boxd.it/29Pq,https://www.themoviedb.org/movie/680/watch?locale=US,,"
        "unknown,unknown\n",
        encoding="utf-8",
    )
    payload = payload_from_csv(csv_path)
    assert payload["stats"]["total"] == 2
    assert payload["stats"]["on_my_services"] == 2
    out = tmp_path / "report.html"
    write_html_report(out, payload)
    text = out.read_text(encoding="utf-8")
    index = tmp_path / "index.html"
    assert index.exists()
    assert index.read_text(encoding="utf-8") == text
    assert "Watchlist" in text
    assert "1917" in text
    assert "JustWatch" in text
    assert "Wrong" in text
    assert "confirmed" in text
    assert "feedback.csv" in text
