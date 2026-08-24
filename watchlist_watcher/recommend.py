"""Recommend command: rank available watchlist films by residual taste fit."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import AppConfig, load_config
from .http_util import HttpClient
from .letterboxd_taste import load_taste_library
from .resolve import IdResolver
from .recommend_html import build_recommend_payload, write_recommend_html
from .taste_model import (
    MOOD_MAP,
    FilmFeatures,
    TasteEnricher,
    TasteModel,
    build_training_rows,
    mood_matches,
    parse_decade_flag,
    train_taste_model,
    watched_directors,
)

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    title: str
    year: Optional[int]
    tmdb_id: int
    letterboxd_url: str
    on_my_services: list[str]
    features: FilmFeatures
    score: float
    explanation: str
    is_wildcard: bool = False


def find_default_export(base: Path) -> Optional[Path]:
    """Look for a Letterboxd export zip or unpacked directory near the project."""
    candidates = [
        base / "letterboxd-export",
        base / "export",
        base / "data" / "letterboxd-export",
    ]
    for path in candidates:
        if path.exists():
            return path
    zips = sorted(base.glob("letterboxd-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if zips:
        return zips[0]
    zips = sorted(base.glob("*letterboxd*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if zips:
        return zips[0]
    return None


def load_available_from_csv(csv_path: Path) -> list[dict[str, object]]:
    """Rows from watchlist_streaming.csv that are on at least one my-service."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Missing {csv_path}. Run the main watcher first so availability is known."
        )
    rows: list[dict[str, object]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            on_raw = (raw.get("on_my_services") or "").strip()
            if not on_raw:
                continue
            services = [p.strip() for p in on_raw.split(";") if p.strip()]
            if not services:
                continue
            tmdb_raw = (raw.get("tmdb_id") or "").strip()
            if not tmdb_raw.isdigit():
                continue
            year_raw = (raw.get("year") or "").strip()
            rows.append(
                {
                    "title": (raw.get("title") or "").strip(),
                    "year": int(year_raw) if year_raw.isdigit() else None,
                    "tmdb_id": int(tmdb_raw),
                    "letterboxd_url": (raw.get("letterboxd_url") or "").strip(),
                    "on_my_services": services,
                }
            )
    return rows


def _format_contrib(parts: list[tuple[str, float]]) -> str:
    bits: list[str] = []
    for name, value in parts:
        short = name
        for prefix in ("genre:", "keyword:", "director:", "cast:", "decade:", "lang:", "runtime:"):
            if short.startswith(prefix):
                short = short[len(prefix) :]
                break
        sign = "+" if value >= 0 else ""
        bits.append(f"{sign}{short}")
    return ", ".join(bits[:4])


def passes_filters(
    features: FilmFeatures,
    *,
    runtime_max: Optional[int],
    mood: Optional[str],
    decade: Optional[int],
    unwatched_director: bool,
    known_directors: set[str],
) -> bool:
    if runtime_max is not None:
        if features.runtime is None or features.runtime > runtime_max:
            return False
    if decade is not None:
        if features.decade != decade:
            return False
    if mood:
        if not mood_matches(features, mood):
            return False
    if unwatched_director:
        directors = {d.casefold() for d in features.directors}
        if not directors:
            return False
        if directors & known_directors:
            return False
    return True


def pick_wildcard(
    ranked: list[Candidate],
    pool: list[Candidate],
) -> Optional[Candidate]:
    """Pick one deliberate off-profile title still available after filters."""
    top_ids = {c.tmdb_id for c in ranked[:10]}
    outsiders = [c for c in pool if c.tmdb_id not in top_ids]
    if not outsiders:
        return None

    # Profile = genres among top ranked; wildcard maximizes genres outside that set.
    profile_genres: set[str] = set()
    for cand in ranked[:10]:
        profile_genres.update(cand.features.genres)
    if not profile_genres:
        # Fall back to mid-pack by score distance from the top mean.
        return outsiders[len(outsiders) // 2]

    best: Optional[Candidate] = None
    best_score = -1e9
    for cand in outsiders:
        novel = [g for g in cand.features.genres if g not in profile_genres]
        novelty = len(novel)
        # Prefer somewhat positive fit, not dumpster fire.
        combined = novelty * 1.5 + cand.score
        if combined > best_score:
            best_score = combined
            best = cand
    if best is None:
        return None
    novel = [g for g in best.features.genres if g not in profile_genres]
    why = ", ".join(novel[:3]) if novel else "away from your top-genre cluster"
    best.is_wildcard = True
    best.explanation = (
        f"Wildcard: still available, but leans {why} instead of your usual residual profile"
    )
    return best


def run_recommend_rank(
    *,
    config: AppConfig,
    export_path: Path,
    runtime_max: Optional[int] = None,
    mood: Optional[str] = None,
    decade: Optional[str] = None,
    unwatched_director: bool = False,
    top_n: int = 10,
    seed: int = 42,
) -> int:
    """Mode 1: rank available watchlist titles by residual taste fit."""
    http = HttpClient(delay_seconds=config.request_delay_seconds)
    library = load_taste_library(export_path)
    resolver = IdResolver(config, http)
    enricher = TasteEnricher(
        api_key=config.tmdb_api_key,
        http=http,
        resolver=resolver,
        cache_path=config.paths.id_cache.parent / "taste_enrichment.json",
    )

    train_rows = build_training_rows(library, enricher)
    resolver.save_cache()
    if len(train_rows) < 10:
        logger.error(
            "Not enough rated/liked films with TMDB enrichment (%d). "
            "Export ratings/likes and retry.",
            len(train_rows),
        )
        return 2

    model = train_taste_model(train_rows, holdout_fraction=0.2, seed=seed)
    _print_validation(model)

    if not model.validation.beats_baseline:
        print()
        print(
            "Model does not beat the mean-residual baseline on holdout MAE. "
            "Refusing to ship recommendations built on noise."
        )
        print(
            "Your strongest residual features are still printed above so you can "
            "inspect whether the fit recovered anything real."
        )
        return 3

    decade_i = parse_decade_flag(decade) if decade else None
    if mood and mood.casefold() not in MOOD_MAP:
        logger.error(
            "Unknown mood %r. Choose from: %s",
            mood,
            ", ".join(sorted(MOOD_MAP)),
        )
        return 2

    available = load_available_from_csv(config.paths.csv_report)
    known_directors = watched_directors(train_rows)

    scored: list[Candidate] = []
    for index, row in enumerate(available, start=1):
        tmdb_id = int(row["tmdb_id"])
        title = str(row["title"])
        if index == 1 or index % 20 == 0:
            logger.info("Scoring available film %d/%d: %s", index, len(available), title)
        features = enricher.enrich_tmdb_id(tmdb_id, title_hint=title)
        if features is None:
            continue
        if not passes_filters(
            features,
            runtime_max=runtime_max,
            mood=mood,
            decade=decade_i,
            unwatched_director=unwatched_director,
            known_directors=known_directors,
        ):
            continue
        score = model.predict_residual(features)
        contrib = model.explain(features, top_k=4)
        services = ", ".join(row["on_my_services"])  # type: ignore[arg-type]
        expl = (
            f"predicted residual {score:+.2f} via {_format_contrib(contrib) or 'global intercept'}; "
            f"on {services}"
        )
        scored.append(
            Candidate(
                title=title,
                year=row["year"] if isinstance(row["year"], int) else None,
                tmdb_id=tmdb_id,
                letterboxd_url=str(row.get("letterboxd_url") or ""),
                on_my_services=list(row["on_my_services"]),  # type: ignore[arg-type]
                features=features,
                score=score,
                explanation=expl,
            )
        )

    enricher.save()
    resolver.save_cache()

    if not scored:
        print()
        print("No available watchlist films matched your filters.")
        return 0

    scored.sort(key=lambda c: c.score, reverse=True)
    top = scored[:top_n]
    wildcard = pick_wildcard(top, scored)

    print()
    print(f"Top {len(top)} available watchlist fits")
    if runtime_max is not None:
        print(f"  filter runtime_max={runtime_max}")
    if mood:
        print(f"  filter mood={mood}")
    if decade:
        print(f"  filter decade={decade}")
    if unwatched_director:
        print("  filter unwatched-director")
    print()
    for rank, cand in enumerate(top, start=1):
        year = f" ({cand.year})" if cand.year else ""
        print(f"{rank:>2}. {cand.title}{year}")
        print(f"    {cand.explanation}")

    if wildcard is not None:
        year = f" ({wildcard.year})" if wildcard.year else ""
        print()
        print(f"Wildcard: {wildcard.title}{year}")
        print(f"    {wildcard.explanation}")
        services = ", ".join(wildcard.on_my_services)
        print(f"    on {services}; predicted residual {wildcard.score:+.2f}")

    _write_recommend_page(
        config=config,
        model=model,
        top=top,
        wildcard=wildcard,
        runtime_max=runtime_max,
        mood=mood,
        decade=decade,
        unwatched_director=unwatched_director,
    )
    print()
    print(f"Wrote {config.paths.recommend_html}")

    return 0


def _candidate_payload(cand: Candidate, *, why: Optional[str] = None) -> dict:
    return {
        "title": cand.title,
        "year": cand.year,
        "tmdb_id": cand.tmdb_id,
        "letterboxd_url": cand.letterboxd_url,
        "on_my_services": list(cand.on_my_services),
        "score": round(cand.score, 4),
        "explanation": cand.explanation,
        "why": why or cand.explanation,
    }


def _write_recommend_page(
    *,
    config: AppConfig,
    model: TasteModel,
    top: list[Candidate],
    wildcard: Optional[Candidate],
    runtime_max: Optional[int],
    mood: Optional[str],
    decade: Optional[str],
    unwatched_director: bool,
) -> None:
    v = model.validation
    top_rows = []
    for cand in top:
        contrib = model.explain(cand.features, top_k=4)
        why = (
            f"predicted residual {cand.score:+.2f} via "
            f"{_format_contrib(contrib) or 'global intercept'}"
        )
        top_rows.append(_candidate_payload(cand, why=why))
    wild_row = None
    if wildcard is not None:
        wild_row = _candidate_payload(
            wildcard,
            why=wildcard.explanation,
        )
    payload = build_recommend_payload(
        top=top_rows,
        wildcard=wild_row,
        validation={
            "n_train": v.n_train,
            "n_holdout": v.n_holdout,
            "mae_model": v.mae_model,
            "mae_baseline": v.mae_baseline,
            "mean_residual": v.mean_residual,
            "correlation": v.correlation,
            "beats_baseline": v.beats_baseline,
            "alpha": model.alpha,
        },
        top_positive=model.top_positive,
        top_negative=model.top_negative,
        filters={
            "runtime_max": runtime_max,
            "mood": mood,
            "decade": decade,
            "unwatched_director": unwatched_director,
        },
    )
    write_recommend_html(config.paths.recommend_html, payload)


def _print_validation(model: TasteModel) -> None:
    v = model.validation
    corr = "n/a" if v.correlation is None else f"{v.correlation:.3f}"
    print("Taste model (residual = my rating - TMDB vote/2)")
    print(f"  train={v.n_train}  holdout={v.n_holdout}  ridge_alpha={model.alpha}")
    print(f"  holdout correlation predicted vs actual residual: {corr}")
    print(f"  holdout MAE model:    {v.mae_model:.3f}")
    print(f"  holdout MAE baseline: {v.mae_baseline:.3f}  (always predict mean residual {v.mean_residual:+.3f})")
    if v.beats_baseline:
        print("  verdict: beats mean baseline")
    else:
        print("  verdict: does NOT beat mean baseline")

    print()
    print("Strongest positive residual features (like more than the crowd):")
    if not model.top_positive:
        print("  (none)")
    for name, coef in model.top_positive[:8]:
        print(f"  {coef:+.3f}  {name}")

    print()
    print("Strongest negative residual features (like less than the crowd):")
    if not model.top_negative:
        print("  (none)")
    for name, coef in model.top_negative[:8]:
        print(f"  {coef:+.3f}  {name}")


def run_recommend_cli(argv: list[str], *, config_path: Path) -> int:
    """Parse recommend-specific flags from argv already sliced after 'recommend'."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="watchlist-watcher recommend",
        description=(
            "Rank available watchlist films by residual taste fit "
            "(Mode 1). Mode 2 (off-watchlist discovery) is not built yet."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(config_path),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--export",
        default=None,
        help="Letterboxd export .zip or unpacked directory",
    )
    parser.add_argument("--runtime-max", type=int, default=None)
    parser.add_argument(
        "--mood",
        default=None,
        help=f"One of: {', '.join(sorted(MOOD_MAP))}",
    )
    parser.add_argument("--decade", default=None, help="e.g. 1970 or 1970s")
    parser.add_argument(
        "--unwatched-director",
        action="store_true",
        help="Only films whose director is absent from your watched taste set",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    export = Path(args.export) if args.export else find_default_export(Path(args.config).parent)
    if export is None:
        logger.error(
            "No Letterboxd export found. Pass --export path/to/letterboxd.zip "
            "(or unpack ratings.csv, diary.csv, likes/films.csv, profile.csv)."
        )
        return 2

    return run_recommend_rank(
        config=config,
        export_path=export,
        runtime_max=args.runtime_max,
        mood=args.mood,
        decade=args.decade,
        unwatched_director=args.unwatched_director,
        top_n=args.top,
        seed=args.seed,
    )
