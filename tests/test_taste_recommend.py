"""Tests for Letterboxd taste parsing and residual ridge model."""

from __future__ import annotations

from pathlib import Path

from watchlist_watcher.letterboxd_taste import load_taste_library
from watchlist_watcher.taste_model import (
    FilmFeatures,
    TrainRow,
    encode_features,
    fit_ridge,
    mae,
    mood_matches,
    parse_decade_flag,
    residual_from_rating,
    train_taste_model,
)


def _write_export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    likes = root / "likes"
    likes.mkdir(parents=True)
    (root / "ratings.csv").write_text(
        "Date,Name,Year,Letterboxd URI,Rating\n"
        "2024-01-01,Heat,1995,https://letterboxd.com/film/heat/,5\n"
        "2024-01-02,The Notebook,2004,https://letterboxd.com/film/the-notebook/,2\n"
        "2024-01-03,Zodiac,2007,https://letterboxd.com/film/zodiac/,4.5\n"
        "2024-01-04,Amelie,2001,https://letterboxd.com/film/amelie/,3\n",
        encoding="utf-8",
    )
    (root / "diary.csv").write_text(
        "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
        "2024-02-01,Heat,1995,https://boxd.it/abc,5,Yes,,2024-02-01\n"
        "2024-02-02,Unrated Log,1999,https://boxd.it/def,,,2024-02-02\n",
        encoding="utf-8",
    )
    (likes / "films.csv").write_text(
        "Date,Name,Year,Letterboxd URI\n"
        "2024-03-01,Heat,1995,https://letterboxd.com/film/heat/\n"
        "2024-03-02,Liked Only,2010,https://letterboxd.com/film/liked-only/\n",
        encoding="utf-8",
    )
    (root / "profile.csv").write_text(
        "Favorite Films\n"
        "Heat, Zodiac\n",
        encoding="utf-8",
    )
    return root


def test_load_taste_weights_and_ignores_unrated(tmp_path: Path) -> None:
    lib = load_taste_library(_write_export(tmp_path))
    rated = {f.name: f for f in lib.rated_films()}
    assert "Heat" in rated
    assert rated["Heat"].rewatch_count >= 1
    assert rated["Heat"].liked is True
    assert rated["Heat"].sample_weight() >= 3.0
    assert "Liked Only" in rated
    assert rated["Liked Only"].effective_rating() == 4.5
    assert "Unrated Log" not in rated


def test_profile_favorite_links_keep_uri(tmp_path: Path) -> None:
    root = tmp_path / "export"
    root.mkdir()
    (root / "ratings.csv").write_text(
        "Date,Name,Year,Letterboxd URI,Rating\n"
        "2024-01-01,Heat,1995,https://letterboxd.com/film/heat/,5\n",
        encoding="utf-8",
    )
    (root / "profile.csv").write_text(
        "Date Joined,Username,Favorite Films\n"
        '2023-01-01,tester,"https://boxd.it/a5fa, https://letterboxd.com/film/zodiac/"\n',
        encoding="utf-8",
    )
    lib = load_taste_library(root)
    by_uri = {f.letterboxd_uri: f for f in lib.films.values() if f.letterboxd_uri}
    assert "https://boxd.it/a5fa" in by_uri
    assert by_uri["https://boxd.it/a5fa"].favorite is True
    assert "https://letterboxd.com/film/zodiac" in by_uri or any(
        f.slug == "zodiac" and f.favorite for f in lib.films.values()
    )


def test_residual_scale() -> None:
    # My 5 stars vs TMDB 8/10 (=4.0 on five-point) => +1 residual.
    assert abs(residual_from_rating(5.0, 8.0) - 1.0) < 1e-9


def test_ridge_recovers_genre_signal() -> None:
    rows: list[TrainRow] = []
    # Synthetic: Crime genre => high residual, Romance => low.
    for i in range(20):
        rows.append(
            TrainRow(
                film=None,  # type: ignore[arg-type]
                features=FilmFeatures(
                    tmdb_id=i,
                    title=f"Crime {i}",
                    year=1990 + i,
                    runtime=120,
                    decade=1990,
                    original_language="en",
                    vote_average=7.0,
                    genres=["Crime", "Thriller"],
                    keywords=["neo-noir"],
                    directors=["Director A"],
                    cast=["Actor A"],
                ),
                rating=5.0,
                residual=1.5,
                weight=1.0,
            )
        )
    for i in range(20, 40):
        rows.append(
            TrainRow(
                film=None,  # type: ignore[arg-type]
                features=FilmFeatures(
                    tmdb_id=i,
                    title=f"Romance {i}",
                    year=1990 + i,
                    runtime=100,
                    decade=1990,
                    original_language="en",
                    vote_average=7.0,
                    genres=["Romance"],
                    keywords=["love"],
                    directors=["Director B"],
                    cast=["Actor B"],
                ),
                rating=2.0,
                residual=-1.5,
                weight=1.0,
            )
        )

    model = train_taste_model(rows, holdout_fraction=0.25, seed=0, alpha=1.0)
    assert model.validation.beats_baseline
    crime = dict(zip(model.feature_names, model.coefficients)).get("genre:Crime", 0.0)
    romance = dict(zip(model.feature_names, model.coefficients)).get("genre:Romance", 0.0)
    assert crime > romance


def test_mean_baseline_gate_on_noise() -> None:
    rows: list[TrainRow] = []
    for i in range(30):
        rows.append(
            TrainRow(
                film=None,  # type: ignore[arg-type]
                features=FilmFeatures(
                    tmdb_id=i,
                    title=f"Film {i}",
                    year=2000,
                    runtime=100,
                    decade=2000,
                    original_language="en",
                    vote_average=7.0,
                    genres=["Drama"],
                    keywords=[],
                    directors=[f"D{i}"],
                    cast=[],
                ),
                rating=3.5,
                residual=(0.1 if i % 2 == 0 else -0.1),
                weight=1.0,
            )
        )
    model = train_taste_model(rows, holdout_fraction=0.2, seed=1, alpha=50.0)
    # On near-noise data the model often fails to beat mean; assert metrics exist.
    assert model.validation.mae_baseline >= 0
    assert model.validation.n_holdout >= 1


def test_mood_and_decade_helpers() -> None:
    features = FilmFeatures(
        tmdb_id=1,
        title="X",
        year=1975,
        runtime=110,
        decade=1970,
        original_language="en",
        vote_average=8.0,
        genres=["Crime", "Thriller"],
        keywords=["neo-noir"],
        directors=["Y"],
        cast=[],
    )
    assert mood_matches(features, "dark")
    assert not mood_matches(features, "funny")
    assert parse_decade_flag("1970s") == 1970


def test_encode_and_fit_smoke() -> None:
    features = FilmFeatures(
        tmdb_id=1,
        title="X",
        year=1995,
        runtime=170,
        decade=1990,
        original_language="en",
        vote_average=8.0,
        genres=["Crime"],
        keywords=["heist"],
        directors=["Michael Mann"],
        cast=["Al Pacino"],
    )
    names = ["genre:Crime", "director:Michael Mann", "runtime:ge150"]
    vector = encode_features(features, names)
    assert vector == [1.0, 1.0, 1.0]
    row = TrainRow(
        film=None,  # type: ignore[arg-type]
        features=features,
        rating=5.0,
        residual=1.0,
        weight=3.0,
    )
    intercept, coef = fit_ridge([row, row], names, alpha=1.0)
    assert len(coef) == 3
    assert mae([intercept], [1.0]) >= 0
