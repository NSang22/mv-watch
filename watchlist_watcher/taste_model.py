"""Residual taste model: Ridge over TMDB one-hot features."""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .http_util import HTTPStatusError, HttpClient, TransientHTTPError
from .letterboxd_taste import TasteFilm, TasteLibrary
from .resolve import IdResolver

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"


@dataclass
class FilmFeatures:
    """TMDB enrichment used by the taste model."""

    tmdb_id: int
    title: str
    year: Optional[int]
    runtime: Optional[int]
    decade: Optional[int]
    original_language: str
    vote_average: float
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)


@dataclass
class TrainRow:
    film: TasteFilm
    features: FilmFeatures
    rating: float
    residual: float
    weight: float


@dataclass
class ValidationReport:
    n_train: int
    n_holdout: int
    correlation: Optional[float]
    mae_model: float
    mae_baseline: float
    beats_baseline: bool
    mean_residual: float


@dataclass
class TasteModel:
    """Fitted ridge model predicting rating residual."""

    feature_names: list[str]
    coefficients: list[float]
    intercept: float
    alpha: float
    validation: ValidationReport
    top_positive: list[tuple[str, float]]
    top_negative: list[tuple[str, float]]

    def predict_residual(self, features: FilmFeatures) -> float:
        vector = encode_features(features, self.feature_names)
        total = self.intercept
        for coef, value in zip(self.coefficients, vector):
            total += coef * value
        return total

    def explain(self, features: FilmFeatures, *, top_k: int = 4) -> list[tuple[str, float]]:
        """Return top contributing (feature, coef*value) terms for this film."""
        vector = encode_features(features, self.feature_names)
        parts: list[tuple[str, float]] = []
        for name, coef, value in zip(self.feature_names, self.coefficients, vector):
            if value == 0:
                continue
            parts.append((name, coef * value))
        parts.sort(key=lambda item: abs(item[1]), reverse=True)
        return parts[:top_k]


def residual_from_rating(my_rating: float, vote_average: float) -> float:
    """my Letterboxd stars (0.5-5) minus TMDB vote on the same 0-5 scale."""
    tmdb_on_five = float(vote_average) / 2.0
    return float(my_rating) - tmdb_on_five


def load_enrichment_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_enrichment_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def features_from_cache_entry(entry: dict[str, Any]) -> FilmFeatures:
    return FilmFeatures(
        tmdb_id=int(entry["tmdb_id"]),
        title=str(entry.get("title") or ""),
        year=entry.get("year"),
        runtime=entry.get("runtime"),
        decade=entry.get("decade"),
        original_language=str(entry.get("original_language") or ""),
        vote_average=float(entry.get("vote_average") or 0.0),
        genres=list(entry.get("genres") or []),
        keywords=list(entry.get("keywords") or []),
        directors=list(entry.get("directors") or []),
        cast=list(entry.get("cast") or []),
    )


def features_to_cache_entry(features: FilmFeatures) -> dict[str, Any]:
    return {
        "tmdb_id": features.tmdb_id,
        "title": features.title,
        "year": features.year,
        "runtime": features.runtime,
        "decade": features.decade,
        "original_language": features.original_language,
        "vote_average": features.vote_average,
        "genres": features.genres,
        "keywords": features.keywords,
        "directors": features.directors,
        "cast": features.cast,
    }


class TasteEnricher:
    """Resolve + fetch TMDB details/credits/keywords with a disk cache."""

    def __init__(
        self,
        *,
        api_key: str,
        http: HttpClient,
        resolver: IdResolver,
        cache_path: Path,
    ) -> None:
        self.api_key = api_key
        self.http = http
        self.resolver = resolver
        self.cache_path = cache_path
        self.cache = load_enrichment_cache(cache_path)

    def save(self) -> None:
        save_enrichment_cache(self.cache_path, self.cache)

    def enrich_taste_film(self, film: TasteFilm) -> Optional[FilmFeatures]:
        cache_key = f"taste:{film.key}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("tmdb_id"):
            return features_from_cache_entry(cached)

        from .models import WatchlistFilm

        uri = (film.letterboxd_uri or "").strip()
        if not uri and film.slug:
            uri = f"https://letterboxd.com/film/{film.slug}/"

        resolved = self.resolver.resolve_one(
            WatchlistFilm(
                name=film.name,
                year=film.year,
                letterboxd_uri=uri,
            )
        )
        if not resolved.tmdb_id:
            return None
        features = self.enrich_tmdb_id(int(resolved.tmdb_id), title_hint=film.name)
        if features is not None:
            self.cache[cache_key] = features_to_cache_entry(features)
        return features

    def enrich_tmdb_id(
        self,
        tmdb_id: int,
        *,
        title_hint: str = "",
    ) -> Optional[FilmFeatures]:
        cache_key = f"tmdb:{tmdb_id}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("tmdb_id"):
            return features_from_cache_entry(cached)

        params = {"api_key": self.api_key}
        try:
            detail = self.http.get_json(f"{TMDB_BASE}/movie/{tmdb_id}", params=params)
            credits = self.http.get_json(
                f"{TMDB_BASE}/movie/{tmdb_id}/credits",
                params=params,
            )
            keywords_payload = self.http.get_json(
                f"{TMDB_BASE}/movie/{tmdb_id}/keywords",
                params=params,
            )
        except (TransientHTTPError, HTTPStatusError) as exc:
            logger.warning("TMDB enrich failed for %s (%s): %s", title_hint, tmdb_id, exc)
            return None

        year = None
        release = (detail.get("release_date") or "").strip()
        if len(release) >= 4 and release[:4].isdigit():
            year = int(release[:4])
        decade = (year // 10) * 10 if year is not None else None
        runtime = detail.get("runtime")
        runtime_i = int(runtime) if isinstance(runtime, int) and runtime > 0 else None

        genres = [
            str(g.get("name"))
            for g in (detail.get("genres") or [])
            if g.get("name")
        ]
        keywords = [
            str(k.get("name"))
            for k in (keywords_payload.get("keywords") or [])
            if k.get("name")
        ]
        directors = [
            str(person.get("name"))
            for person in (credits.get("crew") or [])
            if person.get("job") == "Director" and person.get("name")
        ]
        cast = [
            str(person.get("name"))
            for person in (credits.get("cast") or [])[:5]
            if person.get("name")
        ]

        features = FilmFeatures(
            tmdb_id=tmdb_id,
            title=str(detail.get("title") or title_hint or ""),
            year=year,
            runtime=runtime_i,
            decade=decade,
            original_language=str(detail.get("original_language") or ""),
            vote_average=float(detail.get("vote_average") or 0.0),
            genres=genres,
            keywords=keywords,
            directors=directors,
            cast=cast,
        )
        self.cache[cache_key] = features_to_cache_entry(features)
        return features


def build_training_rows(
    library: TasteLibrary,
    enricher: TasteEnricher,
) -> list[TrainRow]:
    rows: list[TrainRow] = []
    rated = library.rated_films()
    for index, film in enumerate(rated, start=1):
        rating = film.effective_rating()
        if rating is None:
            continue
        if index == 1 or index % 25 == 0:
            logger.info("Enriching taste film %d/%d: %s", index, len(rated), film.name)
        features = enricher.enrich_taste_film(film)
        if features is None:
            continue
        if features.vote_average <= 0:
            continue
        residual = residual_from_rating(rating, features.vote_average)
        rows.append(
            TrainRow(
                film=film,
                features=features,
                rating=rating,
                residual=residual,
                weight=film.sample_weight(),
            )
        )
    enricher.save()
    logger.info("Built %d training rows with TMDB enrichment.", len(rows))
    return rows


def build_vocabulary(rows: list[TrainRow]) -> list[str]:
    """One-hot feature names with frequency floors for sparse slots."""
    genre_counts: dict[str, int] = {}
    keyword_counts: dict[str, int] = {}
    director_counts: dict[str, int] = {}
    cast_counts: dict[str, int] = {}
    decade_counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}

    for row in rows:
        for genre in row.features.genres:
            genre_counts[f"genre:{genre}"] = genre_counts.get(f"genre:{genre}", 0) + 1
        for keyword in row.features.keywords:
            key = f"keyword:{keyword.casefold()}"
            keyword_counts[key] = keyword_counts.get(key, 0) + 1
        for director in row.features.directors:
            key = f"director:{director}"
            director_counts[key] = director_counts.get(key, 0) + 1
        for actor in row.features.cast:
            key = f"cast:{actor}"
            cast_counts[key] = cast_counts.get(key, 0) + 1
        if row.features.decade is not None:
            key = f"decade:{row.features.decade}s"
            decade_counts[key] = decade_counts.get(key, 0) + 1
        if row.features.original_language:
            key = f"lang:{row.features.original_language}"
            lang_counts[key] = lang_counts.get(key, 0) + 1

    names: list[str] = []
    names.extend(sorted(genre_counts))
    names.extend(sorted(k for k, n in keyword_counts.items() if n >= 3))
    names.extend(sorted(k for k, n in director_counts.items() if n >= 2))
    names.extend(sorted(k for k, n in cast_counts.items() if n >= 3))
    names.extend(sorted(decade_counts))
    names.extend(sorted(k for k, n in lang_counts.items() if n >= 3))
    # Continuous-ish runtime bucket markers.
    names.extend(["runtime:lt90", "runtime:90_119", "runtime:120_149", "runtime:ge150"])
    return names


def encode_features(features: FilmFeatures, feature_names: list[str]) -> list[float]:
    active: set[str] = set()
    for genre in features.genres:
        active.add(f"genre:{genre}")
    for keyword in features.keywords:
        active.add(f"keyword:{keyword.casefold()}")
    for director in features.directors:
        active.add(f"director:{director}")
    for actor in features.cast:
        active.add(f"cast:{actor}")
    if features.decade is not None:
        active.add(f"decade:{features.decade}s")
    if features.original_language:
        active.add(f"lang:{features.original_language}")
    runtime = features.runtime
    if runtime is not None:
        if runtime < 90:
            active.add("runtime:lt90")
        elif runtime < 120:
            active.add("runtime:90_119")
        elif runtime < 150:
            active.add("runtime:120_149")
        else:
            active.add("runtime:ge150")
    return [1.0 if name in active else 0.0 for name in feature_names]


def _matmul_ata_atb(
    matrix: list[list[float]],
    targets: list[float],
    weights: list[float],
) -> tuple[list[list[float]], list[float]]:
    n_features = len(matrix[0]) if matrix else 0
    ata = [[0.0 for _ in range(n_features)] for _ in range(n_features)]
    atb = [0.0 for _ in range(n_features)]
    for row, y, w in zip(matrix, targets, weights):
        for i in range(n_features):
            if row[i] == 0:
                continue
            atb[i] += w * row[i] * y
            for j in range(i, n_features):
                if row[j] == 0:
                    continue
                ata[i][j] += w * row[i] * row[j]
    for i in range(n_features):
        for j in range(i + 1, n_features):
            ata[j][i] = ata[i][j]
    return ata, atb


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            continue
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        piv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= piv
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if factor == 0:
                continue
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def fit_ridge(
    rows: list[TrainRow],
    feature_names: list[str],
    *,
    alpha: float = 5.0,
) -> tuple[float, list[float]]:
    """Weighted ridge with intercept. Returns (intercept, coefficients)."""
    if not rows or not feature_names:
        return 0.0, [0.0 for _ in feature_names]

    weights = [r.weight for r in rows]
    y = [r.residual for r in rows]
    # Center target by weighted mean; intercept is that mean after fit of centered y.
    w_sum = sum(weights) or 1.0
    y_mean = sum(w * val for w, val in zip(weights, y)) / w_sum
    y_centered = [val - y_mean for val in y]
    x = [encode_features(r.features, feature_names) for r in rows]

    ata, atb = _matmul_ata_atb(x, y_centered, weights)
    for i in range(len(feature_names)):
        ata[i][i] += alpha

    coef = _solve_linear_system(ata, atb)
    return y_mean, coef


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x < 1e-12 or den_y < 1e-12:
        return None
    return num / (den_x * den_y)


def mae(pred: list[float], actual: list[float]) -> float:
    if not actual:
        return 0.0
    return sum(abs(p - a) for p, a in zip(pred, actual)) / len(actual)


def train_taste_model(
    rows: list[TrainRow],
    *,
    holdout_fraction: float = 0.2,
    seed: int = 42,
    alpha: float = 5.0,
) -> TasteModel:
    if len(rows) < 10:
        raise ValueError(
            f"Need at least 10 enriched rated films to train; got {len(rows)}."
        )

    rng = random.Random(seed)
    indices = list(range(len(rows)))
    rng.shuffle(indices)
    holdout_n = max(1, int(round(len(rows) * holdout_fraction)))
    holdout_idx = set(indices[:holdout_n])
    train_rows = [rows[i] for i in indices if i not in holdout_idx]
    test_rows = [rows[i] for i in indices if i in holdout_idx]
    if not train_rows or not test_rows:
        raise ValueError("Holdout split left an empty train or test set.")

    feature_names = build_vocabulary(train_rows)
    intercept, coef = fit_ridge(train_rows, feature_names, alpha=alpha)

    def predict_row(row: TrainRow) -> float:
        vector = encode_features(row.features, feature_names)
        return intercept + sum(c * v for c, v in zip(coef, vector))

    preds = [predict_row(row) for row in test_rows]
    actual = [row.residual for row in test_rows]
    baseline = sum(r.residual * r.weight for r in train_rows) / (
        sum(r.weight for r in train_rows) or 1.0
    )
    baseline_preds = [baseline for _ in test_rows]
    mae_model = mae(preds, actual)
    mae_base = mae(baseline_preds, actual)
    corr = pearson(preds, actual)
    beats = mae_model < mae_base - 1e-9

    named = list(zip(feature_names, coef))
    named_sorted = sorted(named, key=lambda item: item[1], reverse=True)
    top_positive = [(n, c) for n, c in named_sorted if c > 0][:12]
    top_negative = [(n, c) for n, c in sorted(named, key=lambda item: item[1]) if c < 0][:12]

    # Refit on all rows for deployment coefficients when validation passes or fails;
    # caller decides whether to recommend.
    full_names = build_vocabulary(rows)
    full_intercept, full_coef = fit_ridge(rows, full_names, alpha=alpha)
    named_full = list(zip(full_names, full_coef))
    top_positive = [(n, c) for n, c in sorted(named_full, key=lambda i: i[1], reverse=True) if c > 0][:12]
    top_negative = [(n, c) for n, c in sorted(named_full, key=lambda i: i[1]) if c < 0][:12]

    validation = ValidationReport(
        n_train=len(train_rows),
        n_holdout=len(test_rows),
        correlation=corr,
        mae_model=mae_model,
        mae_baseline=mae_base,
        beats_baseline=beats,
        mean_residual=baseline,
    )
    return TasteModel(
        feature_names=full_names,
        coefficients=full_coef,
        intercept=full_intercept,
        alpha=alpha,
        validation=validation,
        top_positive=top_positive,
        top_negative=top_negative,
    )


def watched_directors(rows: list[TrainRow]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        for director in row.features.directors:
            names.add(director.casefold())
    return names


def parse_decade_flag(value: str) -> int:
    text = value.strip().lower().replace("s", "")
    if not text.isdigit():
        raise ValueError(f"Invalid --decade value: {value}")
    year = int(text)
    if year < 100:
        year *= 100  # unlikely
    return (year // 10) * 10


MOOD_MAP: dict[str, dict[str, set[str]]] = {
    "dark": {
        "genres": {"Crime", "Thriller", "Horror", "War", "Mystery"},
        "keywords": {"neo-noir", "violence", "murder", "revenge", "serial killer", "noir"},
    },
    "funny": {
        "genres": {"Comedy"},
        "keywords": {"satire", "parody", "buddy comedy", "slapstick"},
    },
    "feel-good": {
        "genres": {"Comedy", "Family", "Music", "Romance", "Animation"},
        "keywords": {"friendship", "heartwarming", "coming of age"},
    },
    "tense": {
        "genres": {"Thriller", "Mystery", "Crime", "Horror", "Action"},
        "keywords": {"suspense", "kidnapping", "heist", "conspiracy"},
    },
    "weird": {
        "genres": {"Science Fiction", "Fantasy", "Horror", "Mystery"},
        "keywords": {"surreal", "psychedelic", "body horror", "dream", "avant-garde"},
    },
    "romantic": {
        "genres": {"Romance"},
        "keywords": {"love", "relationship", "wedding"},
    },
    "action": {
        "genres": {"Action", "Adventure", "Western"},
        "keywords": {"chase", "martial arts", "explosion"},
    },
    "quiet": {
        "genres": {"Drama"},
        "keywords": {"melodrama", "meditation", "rural", "family"},
    },
}


def mood_matches(features: FilmFeatures, mood: str) -> bool:
    spec = MOOD_MAP.get(mood.casefold())
    if spec is None:
        raise ValueError(
            f"Unknown --mood {mood!r}. Choose from: {', '.join(sorted(MOOD_MAP))}"
        )
    genres = set(features.genres)
    keywords = {k.casefold() for k in features.keywords}
    if genres & spec["genres"]:
        return True
    if keywords & {k.casefold() for k in spec["keywords"]}:
        return True
    return False
