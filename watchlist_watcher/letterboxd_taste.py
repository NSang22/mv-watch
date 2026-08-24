"""Parse Letterboxd export CSVs into a weighted taste signal."""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Sample weights: rewatches > likes > plain ratings.
WEIGHT_RATING = 1.0
WEIGHT_LIKE = 2.0
WEIGHT_REWATCH = 3.0
WEIGHT_FAVORITE = 2.5

# Synthetic ratings when a film is liked/favorited but never star-rated.
IMPLIED_LIKE_RATING = 4.5
IMPLIED_FAVORITE_RATING = 5.0

FILM_URI_RE = re.compile(r"letterboxd\.com/film/([^/\s]+)", re.I)
DIARY_URI_RE = re.compile(r"letterboxd\.com/[^/]+/film/([^/\s]+)", re.I)


@dataclass
class TasteFilm:
    """One film after merging ratings / likes / diary / favorites."""

    name: str
    year: Optional[int]
    letterboxd_uri: str = ""
    slug: str = ""
    rating: Optional[float] = None  # Letterboxd 0.5-5 when known
    liked: bool = False
    favorite: bool = False
    rewatch_count: int = 0
    diary_logs: int = 0

    @property
    def key(self) -> str:
        if self.slug:
            return self.slug.casefold()
        year = self.year if self.year is not None else ""
        return f"{self.name.casefold()}|{year}"

    def effective_rating(self) -> Optional[float]:
        """Rating used for residual training. None means exclude."""
        if self.rating is not None:
            return float(self.rating)
        if self.favorite:
            return IMPLIED_FAVORITE_RATING
        if self.liked:
            return IMPLIED_LIKE_RATING
        return None

    def sample_weight(self) -> float:
        """Higher weight for rewatches, then likes/favorites, then ratings."""
        if self.rewatch_count > 0:
            return WEIGHT_REWATCH * (1.0 + 0.25 * (self.rewatch_count - 1))
        if self.favorite:
            return WEIGHT_FAVORITE
        if self.liked:
            return WEIGHT_LIKE
        return WEIGHT_RATING


@dataclass
class TasteLibrary:
    """Merged Letterboxd taste corpus."""

    films: dict[str, TasteFilm] = field(default_factory=dict)
    source: str = ""

    def rated_films(self) -> list[TasteFilm]:
        """Films with an effective rating (star, like, or favorite)."""
        out = []
        for film in self.films.values():
            if film.effective_rating() is not None:
                out.append(film)
        return out

    def watched_keys(self) -> set[str]:
        return set(self.films.keys())


def load_taste_library(source: Path) -> TasteLibrary:
    """Load ratings/diary/likes/profile from a Letterboxd zip or unpacked dir."""
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"Letterboxd export not found: {source}")

    if source.is_file() and source.suffix.lower() == ".zip":
        return _from_zip(source)
    if source.is_dir():
        return _from_dir(source)
    raise ValueError(f"Taste export must be a .zip or directory: {source}")


def _from_zip(path: Path) -> TasteLibrary:
    lib = TasteLibrary(source=str(path))
    with zipfile.ZipFile(path) as archive:
        names = {n.replace("\\", "/"): n for n in archive.namelist()}

        def read_member(suffixes: tuple[str, ...]) -> Optional[str]:
            for logical, real in names.items():
                lower = logical.lower()
                if not lower.endswith(suffixes):
                    continue
                if "deleted/" in lower or "orphaned/" in lower:
                    continue
                with archive.open(real) as handle:
                    return io.TextIOWrapper(handle, encoding="utf-8-sig").read()
            # Fallback including deleted/orphaned paths.
            for logical, real in names.items():
                lower = logical.lower()
                if lower.endswith(suffixes):
                    with archive.open(real) as handle:
                        return io.TextIOWrapper(handle, encoding="utf-8-sig").read()
            return None

        ratings = read_member(("ratings.csv",))
        diary = read_member(("diary.csv",))
        likes = read_member(("likes/films.csv",))
        if likes is None:
            for logical, real in names.items():
                if logical.lower().endswith("likes/films.csv"):
                    with archive.open(real) as handle:
                        likes = io.TextIOWrapper(handle, encoding="utf-8-sig").read()
                    break
        profile = read_member(("profile.csv",))

    _ingest(lib, ratings=ratings, diary=diary, likes=likes, profile=profile)
    return lib


def _from_dir(path: Path) -> TasteLibrary:
    lib = TasteLibrary(source=str(path))

    def maybe(rel: str) -> Optional[str]:
        candidate = path / rel
        if candidate.exists():
            return candidate.read_text(encoding="utf-8-sig")
        return None

    likes = maybe("likes/films.csv")
    _ingest(
        lib,
        ratings=maybe("ratings.csv"),
        diary=maybe("diary.csv"),
        likes=likes,
        profile=maybe("profile.csv"),
    )
    return lib


def _ingest(
    lib: TasteLibrary,
    *,
    ratings: Optional[str],
    diary: Optional[str],
    likes: Optional[str],
    profile: Optional[str],
) -> None:
    if ratings:
        _parse_ratings(lib, ratings)
    else:
        logger.warning("No ratings.csv found in export.")
    if diary:
        _parse_diary(lib, diary)
    else:
        logger.warning("No diary.csv found in export.")
    if likes:
        _parse_likes(lib, likes)
    else:
        logger.warning("No likes/films.csv found in export.")
    if profile:
        _parse_profile_favorites(lib, profile)
    else:
        logger.warning("No profile.csv found in export.")

    rated = len(lib.rated_films())
    logger.info(
        "Taste library: %d films merged, %d with usable ratings/likes/favorites.",
        len(lib.films),
        rated,
    )


def _get_or_create(
    lib: TasteLibrary,
    *,
    name: str,
    year: Optional[int],
    uri: str,
    slug: str,
) -> TasteFilm:
    key = slug.casefold() if slug else f"{name.casefold()}|{year if year is not None else ''}"
    film = lib.films.get(key)
    if film is None and slug:
        # Merge diary short-links onto an existing name/year ratings row.
        alt = f"{name.casefold()}|{year if year is not None else ''}"
        film = lib.films.get(alt)
        if film is not None:
            lib.films[key] = film
            if not film.slug:
                film.slug = slug
    if film is None and not slug:
        # Merge name/year onto an existing slug row with same title/year.
        for existing in lib.films.values():
            if existing.name.casefold() != name.casefold():
                continue
            if year is not None and existing.year is not None and existing.year != year:
                continue
            film = existing
            lib.films[key] = film
            break
    if film is None:
        film = TasteFilm(name=name, year=year, letterboxd_uri=uri, slug=slug)
        lib.films[key] = film
    else:
        if not film.letterboxd_uri and uri:
            film.letterboxd_uri = uri
        if not film.slug and slug:
            film.slug = slug
        if film.year is None and year is not None:
            film.year = year
    return film


def _parse_year(raw: str) -> Optional[int]:
    text = (raw or "").strip()
    if text.isdigit():
        return int(text)
    return None


def _parse_rating(raw: str) -> Optional[float]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _slug_from_uri(uri: str) -> str:
    text = (uri or "").strip()
    match = FILM_URI_RE.search(text)
    if match:
        return match.group(1)
    match = DIARY_URI_RE.search(text)
    if match:
        slug = match.group(1)
        # Diary entries may append /2 for rewatches already stripped by group.
        return slug
    return ""


def _detect_delimiter(sample: str) -> str:
    first = sample.splitlines()[0] if sample else ""
    if first.count("\t") > first.count(","):
        return "\t"
    return ","


def _rows(text: str) -> list[dict[str, str]]:
    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw in reader:
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            key_s = (key or "").strip()
            if isinstance(value, list):
                value_s = ", ".join(str(v).strip() for v in value if v is not None)
            elif value is None:
                value_s = ""
            else:
                value_s = str(value).strip()
            cleaned[key_s] = value_s
        rows.append(cleaned)
    return rows


def _field(row: dict[str, str], *names: str) -> str:
    lower = {k.casefold(): v for k, v in row.items()}
    for name in names:
        if name.casefold() in lower:
            return lower[name.casefold()]
    return ""


def _parse_ratings(lib: TasteLibrary, text: str) -> None:
    count = 0
    for row in _rows(text):
        name = _field(row, "Name", "Title")
        if not name:
            continue
        year = _parse_year(_field(row, "Year"))
        uri = _field(row, "Letterboxd URI", "URI", "URL")
        rating = _parse_rating(_field(row, "Rating"))
        if rating is None:
            continue
        slug = _slug_from_uri(uri)
        film = _get_or_create(lib, name=name, year=year, uri=uri, slug=slug)
        # Keep the latest / existing max? Prefer explicit ratings.csv value.
        film.rating = rating
        if uri.startswith("http") and "/film/" in uri:
            film.letterboxd_uri = uri
        count += 1
    logger.info("Parsed %d ratings.", count)


def _parse_diary(lib: TasteLibrary, text: str) -> None:
    logs = 0
    rewatches = 0
    for row in _rows(text):
        name = _field(row, "Name", "Title")
        if not name:
            continue
        year = _parse_year(_field(row, "Year"))
        uri = _field(row, "Letterboxd URI", "URI", "URL")
        rating = _parse_rating(_field(row, "Rating"))
        rewatch_raw = _field(row, "Rewatch").casefold()
        is_rewatch = rewatch_raw in {"yes", "true", "y", "1"}
        slug = _slug_from_uri(uri)
        film = _get_or_create(lib, name=name, year=year, uri=uri, slug=slug)
        film.diary_logs += 1
        logs += 1
        if is_rewatch:
            film.rewatch_count += 1
            rewatches += 1
        if rating is not None:
            # Diary rating fills gaps; do not overwrite a ratings.csv value with empty.
            if film.rating is None:
                film.rating = rating
    logger.info("Parsed %d diary logs (%d marked rewatch).", logs, rewatches)


def _parse_likes(lib: TasteLibrary, text: str) -> None:
    count = 0
    for row in _rows(text):
        name = _field(row, "Name", "Title")
        if not name:
            continue
        year = _parse_year(_field(row, "Year"))
        uri = _field(row, "Letterboxd URI", "URI", "URL")
        slug = _slug_from_uri(uri)
        film = _get_or_create(lib, name=name, year=year, uri=uri, slug=slug)
        film.liked = True
        count += 1
    logger.info("Parsed %d liked films.", count)


def _parse_profile_favorites(lib: TasteLibrary, text: str) -> None:
    """Best-effort parse of profile.csv favorite films."""
    favorites: list[str] = []
    rows = _rows(text)
    if rows:
        for row in rows:
            # Common shapes: column Favorite Films / Favorites with comma titles,
            # or Name column listing favorites.
            for key, value in row.items():
                key_l = key.casefold()
                if "favorite" in key_l and value:
                    parts = re.split(r"[,\n;]+", value)
                    favorites.extend(p.strip() for p in parts if p.strip())
            name = _field(row, "Name", "Title", "Film")
            if name and any("favorite" in k.casefold() for k in row):
                favorites.append(name)

    # Fallback: non-dict-ish profile dumps sometimes use "Favorite Films" then lines.
    if not favorites:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        capture = False
        for line in lines:
            if "favorite" in line.casefold():
                capture = True
                # Same line may contain titles after a colon.
                if ":" in line:
                    tail = line.split(":", 1)[1].strip()
                    if tail:
                        favorites.extend(
                            p.strip() for p in re.split(r"[,\n;]+", tail) if p.strip()
                        )
                continue
            if capture:
                if "," in line and not line.lower().startswith("date"):
                    favorites.extend(p.strip() for p in line.split(",") if p.strip())
                elif line and not line.lower().endswith(".csv"):
                    favorites.append(line)

    count = 0
    for title in favorites:
        # Titles may include a year in parentheses. Favorites may also be
        # Letterboxd / boxd.it links (common in profile.csv exports).
        year = None
        name = title
        uri = ""
        slug = ""
        if re.match(r"^https?://", title):
            uri = title.rstrip("/")
            slug = _slug_from_uri(uri)
            name = slug.replace("-", " ").title() if slug else uri
        else:
            match = re.match(r"^(.*)\((\d{4})\)\s*$", title)
            if match:
                name = match.group(1).strip()
                year = int(match.group(2))
        if not name:
            continue
        film = _get_or_create(lib, name=name, year=year, uri=uri, slug=slug)
        film.favorite = True
        count += 1
    logger.info("Parsed %d favorite films from profile.", count)
