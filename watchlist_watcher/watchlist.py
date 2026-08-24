"""Read Letterboxd watchlist data from export files or public pages."""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .http_util import HttpClient
from .models import WatchlistFilm

logger = logging.getLogger(__name__)

LETTERBOXD_BASE = "https://letterboxd.com"
LETTERBOXD_NS = {"lb": "https://letterboxd.com"}


@dataclass(frozen=True)
class WatchedFilm:
    """One recently logged diary entry from Letterboxd RSS."""

    name: str
    year: Optional[int]
    watched_date: Optional[str] = None
    link: Optional[str] = None


def load_watchlist(
    source: Optional[Path] = None,
    *,
    username: Optional[str] = None,
    http: Optional[HttpClient] = None,
) -> list[WatchlistFilm]:
    """Load a watchlist from a CSV/zip export, or scrape a public profile.

    Prefers ``source`` when provided. Falls back to scraping ``username``.
    """
    if source is not None:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Watchlist source not found: {path}")
        if path.suffix.lower() == ".zip":
            return _from_zip(path)
        return _from_csv(path)

    if username:
        if http is None:
            raise ValueError("HttpClient is required for scrape mode.")
        logger.warning(
            "Using fragile scrape mode for Letterboxd user %s. Prefer a CSV export.",
            username,
        )
        return scrape_watchlist(username, http)

    raise ValueError("Provide a watchlist CSV/zip path or LETTERBOXD_USER for scrape mode.")


def _from_zip(path: Path) -> list[WatchlistFilm]:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith("watchlist.csv")]
        if not names:
            raise ValueError(f"No watchlist.csv found inside {path}")
        with archive.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8-sig")
            return _parse_csv_text(text.read())


def _from_csv(path: Path) -> list[WatchlistFilm]:
    # utf-8-sig handles Letterboxd's BOM; also try cp1252 if mojibake slipped in.
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    return _parse_csv_text(text)


def _detect_delimiter(sample: str) -> str:
    """Prefer tabs when Excel re-saved the Letterboxd export as TSV."""
    first_line = sample.splitlines()[0] if sample else ""
    if first_line.count("\t") >= 3 and first_line.count(",") < first_line.count("\t"):
        return "\t"
    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",\t;")
        return dialect.delimiter
    except csv.Error:
        return ","


def _parse_csv_text(text: str) -> list[WatchlistFilm]:
    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return _parse_csv_rows(reader)


def _parse_csv_rows(reader: csv.DictReader) -> list[WatchlistFilm]:
    films: list[WatchlistFilm] = []
    for row in reader:
        # Normalize header whitespace/case quirks from spreadsheet editors.
        normalized = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        name = normalized.get("Name") or ""
        uri = normalized.get("Letterboxd URI") or ""
        if not name or not uri:
            continue
        year_raw = normalized.get("Year") or ""
        year = int(year_raw) if year_raw.isdigit() else None
        films.append(
            WatchlistFilm(
                name=name,
                year=year,
                letterboxd_uri=uri.rstrip("/"),
                date_added=normalized.get("Date") or None,
            )
        )
    logger.info("Loaded %d films from Letterboxd export.", len(films))
    return films


def write_watchlist_csv(path: Path, films: list[WatchlistFilm]) -> None:
    """Persist a watchlist in Letterboxd export shape so later runs stay in sync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Name", "Year", "Letterboxd URI"])
        writer.writeheader()
        for film in films:
            writer.writerow(
                {
                    "Date": film.date_added or "",
                    "Name": film.name,
                    "Year": film.year if film.year is not None else "",
                    "Letterboxd URI": film.letterboxd_uri,
                }
            )
    logger.info("Wrote %d films to %s", len(films), path)


def scrape_looks_usable(scraped: list[WatchlistFilm], previous_count: int) -> bool:
    """Reject an empty or obviously truncated public-watchlist scrape."""
    if not scraped:
        return False
    if previous_count <= 0:
        return True
    return len(scraped) >= max(5, int(previous_count * 0.2))


def fetch_diary_rss(username: str, http: HttpClient) -> list[WatchedFilm]:
    """Load recent diary entries from the public Letterboxd RSS feed.

    Letterboxd blocks HTML scrapes from many hosts, but the diary RSS feed still
    works and is enough to drop titles you just watched.
    """
    url = f"{LETTERBOXD_BASE}/{username.strip().strip('/')}/rss/"
    xml_text = http.get_text(url)
    root = ET.fromstring(xml_text)
    watched: list[WatchedFilm] = []
    for item in root.findall("./channel/item"):
        name = item.findtext("lb:filmTitle", default="", namespaces=LETTERBOXD_NS).strip()
        year_raw = item.findtext("lb:filmYear", default="", namespaces=LETTERBOXD_NS)
        if not name:
            # Fallback for non-diary activity items.
            title = (item.findtext("title") or "").strip()
            if " - " in title:
                title = title.rsplit(" - ", 1)[0].strip()
            if "," in title:
                maybe_name, maybe_year = title.rsplit(",", 1)
                maybe_year = maybe_year.strip()
                if maybe_year.isdigit():
                    name = maybe_name.strip()
                    year_raw = maybe_year
        if not name:
            continue
        year = int(year_raw) if year_raw and str(year_raw).isdigit() else None
        watched.append(
            WatchedFilm(
                name=name,
                year=year,
                watched_date=item.findtext(
                    "lb:watchedDate", default=None, namespaces=LETTERBOXD_NS
                ),
                link=item.findtext("link"),
            )
        )
    logger.info("Loaded %d recent diary entries from RSS for %s.", len(watched), username)
    return watched


def film_was_watched(film: WatchlistFilm, watched: list[WatchedFilm]) -> bool:
    """Match watchlist rows to diary entries by title, with optional year."""
    name = film.name.casefold()
    for entry in watched:
        if entry.name.casefold() != name:
            continue
        if film.year is None or entry.year is None or film.year == entry.year:
            return True
    return False


def prune_watched_films(
    films: list[WatchlistFilm],
    watched: list[WatchedFilm],
) -> tuple[list[WatchlistFilm], list[WatchlistFilm]]:
    """Drop watchlist rows that appear in recent diary RSS."""
    remaining: list[WatchlistFilm] = []
    removed: list[WatchlistFilm] = []
    for film in films:
        if film_was_watched(film, watched):
            removed.append(film)
        else:
            remaining.append(film)
    return remaining, removed


def scrape_watchlist(username: str, http: HttpClient) -> list[WatchlistFilm]:
    """Page through a public Letterboxd watchlist until a page has no posters.

    Fragile: Letterboxd markup changes can break this without warning.
    """
    films: list[WatchlistFilm] = []
    seen: set[str] = set()
    page = 1

    while True:
        url = f"{LETTERBOXD_BASE}/{username}/watchlist/page/{page}/"
        html = http.get_text(url)
        page_films = _parse_watchlist_page(html)
        if not page_films:
            break
        for film in page_films:
            if film.letterboxd_uri in seen:
                continue
            seen.add(film.letterboxd_uri)
            films.append(film)
        logger.info("Scraped watchlist page %d (%d films so far).", page, len(films))
        page += 1

    logger.info("Scraped %d films for user %s.", len(films), username)
    return films


def _parse_watchlist_page(html: str) -> list[WatchlistFilm]:
    soup = BeautifulSoup(html, "lxml")
    films: list[WatchlistFilm] = []

    for poster in soup.select("li.poster-container div.film-poster, div.film-poster"):
        slug = poster.get("data-film-slug") or poster.get("data-target-link")
        name = poster.get("data-film-name") or poster.get("data-item-name")
        year_raw = poster.get("data-film-release-year") or poster.get("data-item-full-display-name")

        if not slug:
            link = poster.find("a")
            if link and link.get("href"):
                slug = link["href"]
        if not name:
            img = poster.find("img")
            if img and img.get("alt"):
                name = img["alt"]

        if not slug or not name:
            continue

        href = str(slug)
        if not href.startswith("http"):
            href = urljoin(LETTERBOXD_BASE, href)
        href = href.rstrip("/")

        year: Optional[int] = None
        if year_raw and str(year_raw).isdigit():
            year = int(year_raw)
        else:
            # data-item-full-display-name looks like "Title (2019)"
            display = poster.get("data-item-full-display-name") or ""
            if display.endswith(")") and "(" in display:
                maybe = display.rsplit("(", 1)[-1].rstrip(")")
                if maybe.isdigit():
                    year = int(maybe)

        films.append(
            WatchlistFilm(
                name=str(name).strip(),
                year=year,
                letterboxd_uri=href,
            )
        )

    return films
