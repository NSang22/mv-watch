"""CLI entrypoint for watchlist-watcher."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from .anomaly import assess_departure_anomaly
from .config import load_config
from .diff import (
    apply_last_changed,
    build_next_state,
    compute_diff,
    load_state,
    save_state,
    utc_now_iso,
)
from .enrich import ExpiryEnricher, enrichment_enabled, write_conflicts_csv
from .http_util import HttpClient
from .html_report import payload_from_csv, write_html_from_films, write_html_report
from .models import PRESENCE_UNKNOWN
from .notify import notify, write_csv, write_report
from .providers import ProviderClient, write_unresolved_csv
from .resolve import IdResolver
from .spin import build_spin_films, write_spin_html
from .watchlist import (
    fetch_diary_rss,
    load_watchlist,
    prune_watched_films,
    scrape_looks_usable,
    write_watchlist_csv,
)

logger = logging.getLogger(__name__)

# Load .env from the working directory when present. Real env vars still win.
load_dotenv(override=False)


def build_parser() -> argparse.ArgumentParser:
    """Create the argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="watchlist-watcher",
        description=(
            "Cross-reference a Letterboxd watchlist with TMDB streaming "
            "availability and notify when films arrive on your services. "
            "Also: `recommend` to rank available watchlist titles by taste."
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--watchlist",
        default=None,
        help="Path to watchlist.csv or Letterboxd export .zip (overrides config)",
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Force Letterboxd scrape mode using LETTERBOXD_USER",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="Print TMDB provider names for the configured region and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute reports without writing state.json or sending notifications",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write state even when the departure anomaly gate would abort",
    )
    parser.add_argument(
        "--render-html",
        action="store_true",
        help="Rebuild report.html from watchlist_streaming.csv (no TMDB calls)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def dispatch(argv: list[str] | None = None) -> int:
    """CLI entry. Supports `recommend` as a first-word subcommand."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "recommend":
        from .recommend import run_recommend_cli

        config = "config.yaml"
        rest = raw[1:]
        cleaned: list[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "--config" and i + 1 < len(rest):
                config = rest[i + 1]
                i += 2
                continue
            cleaned.append(rest[i])
            i += 1
        return run_recommend_cli(cleaned, config_path=Path(config))
    return run(raw)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _ensure_config(config_path: Path) -> None:
    if config_path.exists():
        return
    example = config_path.with_name("config.example.yaml")
    if example.exists():
        shutil.copy(example, config_path)
        logger.info("Created %s from %s", config_path, example.name)
        return
    raise FileNotFoundError(f"Missing {config_path} and no config.example.yaml to copy.")


def run_list_providers(config_path: Path) -> int:
    """Dump TMDB provider names for the configured region."""
    config = load_config(config_path)
    http = HttpClient(delay_seconds=config.request_delay_seconds)
    client = ProviderClient(config, http)
    providers = client.list_region_providers()
    print(f"TMDB movie providers for region {config.region}:")
    for item in providers:
        name = item.get("provider_name", "")
        provider_id = item.get("provider_id", "")
        print(f"  {provider_id:>5}  {name}")
    missing = []
    from .providers import validate_service_matches

    missing = validate_service_matches(
        [p.get("provider_name", "") for p in providers],
        config.services,
    )
    if missing:
        print("\nWARNING: these configured services matched zero providers:")
        for name in missing:
            print(f"  - {name}")
        return 2
    return 0


def run_render_html(config_path: Path) -> int:
    """Rebuild the HTML viewer and spin wheel from local CSV files."""
    import yaml

    raw = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    paths = raw.get("paths") or {}
    base = config_path.parent.resolve()
    csv_report = base / paths.get("csv_report", "watchlist_streaming.csv")
    html_report = base / paths.get("html_report", "report.html")
    spin_html = base / paths.get("spin_html", "spin.html")
    watchlist = base / paths.get("watchlist", "watchlist.csv")
    id_cache = base / paths.get("id_cache", "cache/id_cache.json")
    spin_meta = base / "cache" / "spin_meta.json"
    taste_cache = base / "cache" / "taste_enrichment.json"
    if csv_report.exists():
        payload = payload_from_csv(csv_report)
        write_html_report(html_report, payload)
        logger.info("Wrote %s", html_report.resolve())
    else:
        logger.warning("Missing %s; skipped report.html", csv_report)

    if watchlist.exists():
        films = load_watchlist(watchlist)
        api_key = None
        http = None
        try:
            from .config import load_config

            cfg = load_config(config_path)
            api_key = cfg.tmdb_api_key
            http = HttpClient(delay_seconds=cfg.request_delay_seconds)
        except (FileNotFoundError, ValueError):
            # Offline rebuild: use whatever metadata is already cached.
            pass
        spin_films = build_spin_films(
            films,
            streaming_csv=csv_report if csv_report.exists() else None,
            id_cache_path=id_cache if id_cache.exists() else None,
            meta_path=spin_meta,
            taste_cache_path=taste_cache if taste_cache.exists() else None,
            overrides_path=base / paths.get("overrides", "overrides.json"),
            tmdb_api_key=api_key,
            http=http,
        )
        write_spin_html(spin_html, spin_films, source_label="Default watchlist")
        logger.info("Wrote %s", spin_html.resolve())
    else:
        logger.warning("Missing %s; skipped spin.html", watchlist)
    return 0


def _load_watchlist_films(config, args, http) -> list:
    """Prefer a live Letterboxd scrape; else CSV + diary RSS pruning."""
    watchlist_path = Path(args.watchlist) if args.watchlist else config.paths.watchlist
    csv_films = []
    if watchlist_path.exists():
        csv_films = load_watchlist(watchlist_path)

    username = (config.letterboxd_user or "").strip()
    should_scrape = bool(args.scrape or username)
    if should_scrape and username:
        try:
            scraped = load_watchlist(None, username=username, http=http)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Letterboxd scrape failed; using committed CSV: %s", exc)
            scraped = []
        if scrape_looks_usable(scraped, len(csv_films)):
            write_watchlist_csv(watchlist_path, scraped)
            return scraped
        if scraped:
            logger.warning(
                "Scrape returned %d films vs %d in CSV; keeping the committed export.",
                len(scraped),
                len(csv_films),
            )

    films = list(csv_films)
    if username and films:
        try:
            watched = fetch_diary_rss(username, http)
            films, removed = prune_watched_films(films, watched)
            if removed:
                write_watchlist_csv(watchlist_path, films)
                logger.info(
                    "Pruned %d watched title(s) via diary RSS: %s",
                    len(removed),
                    ", ".join(f.name for f in removed[:12])
                    + ("…" if len(removed) > 12 else ""),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Diary RSS sync failed; keeping committed CSV: %s", exc)

    if films:
        return films
    raise ValueError(
        "No watchlist available. Commit watchlist.csv or set LETTERBOXD_USER."
    )


def _state_is_stale(previous: dict, *, max_age_days: int = 7) -> bool:
    """True when the last successful state write is older than max_age_days."""
    from datetime import datetime, timezone

    raw = previous.get("last_run")
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - stamp
    return age.total_seconds() > max_age_days * 86400


def run(argv: list[str] | None = None) -> int:
    """Run the full watchlist check. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config_path = Path(args.config)
    try:
        _ensure_config(config_path)
        if args.list_providers:
            return run_list_providers(config_path)
        if args.render_html:
            return run_render_html(config_path)
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    http = HttpClient(delay_seconds=config.request_delay_seconds)
    providers = ProviderClient(config, http)

    try:
        providers.warn_unmatched_services()
    except Exception as exc:  # noqa: BLE001 - startup warning should not hard-fail
        logger.warning("Could not validate provider names against TMDB: %s", exc)

    try:
        films = _load_watchlist_films(config, args, http)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load watchlist: %s", exc)
        return 2

    resolver = IdResolver(config, http)
    try:
        resolved = resolver.resolve_all(films)
    except KeyboardInterrupt:
        logger.error("Interrupted. ID cache was saved.")
        return 130

    try:
        availability = providers.fetch_all(resolved)
    except KeyboardInterrupt:
        logger.error("Interrupted during provider fetch.")
        return 130

    enricher = ExpiryEnricher(config, http)
    used_enrichment = enrichment_enabled(config)
    try:
        conflicts = enricher.enrich_all(availability)
    except KeyboardInterrupt:
        logger.error("Interrupted during enrichment.")
        return 130

    if providers.attempts and providers.failure_rate > config.failure_rate_threshold:
        logger.error(
            "TMDB failure rate %.0f%% (%d/%d) exceeded threshold %.0f%%.",
            providers.failure_rate * 100,
            providers.failures,
            providers.attempts,
            config.failure_rate_threshold * 100,
        )
        return 1

    previous = load_state(config.paths.state)
    diff, alerted = compute_diff(
        availability,
        previous,
        leaving_soon_thresholds=config.streaming_availability.leaving_soon_days
        if used_enrichment
        else [],
    )

    anomaly = assess_departure_anomaly(
        diff,
        watchlist_size=len(films),
        max_departure_films=config.max_departure_films,
        max_departure_fraction=config.max_departure_fraction,
    )
    stale_state = _state_is_stale(previous)
    if anomaly.anomalous and not (args.force or stale_state):
        write_unresolved_csv(config.paths.unresolved, providers.unresolved)
        if conflicts:
            write_conflicts_csv(config.paths.conflicts, conflicts)
        logger.error("%s", anomaly.message)
        print(anomaly.message, file=sys.stderr)
        return 3
    if anomaly.anomalous and (args.force or stale_state):
        reason = "--force" if args.force else "stale state (>7d)"
        logger.warning(
            "Departure anomaly bypassed (%s): %d films / %.1f%%. Continuing.",
            reason,
            anomaly.departure_films,
            anomaly.fraction * 100,
        )

    run_ts = utc_now_iso()
    apply_last_changed(availability, previous, diff, run_ts)

    write_csv(config.paths.csv_report, availability)
    if conflicts:
        write_conflicts_csv(config.paths.conflicts, conflicts)
    write_unresolved_csv(config.paths.unresolved, providers.unresolved)
    write_report(
        config.paths.markdown_report,
        availability,
        diff,
        enrichment_used=used_enrichment,
    )
    write_html_from_films(config.paths.html_report, availability, diff)
    try:
        spin_meta = config.paths.id_cache.parent / "spin_meta.json"
        taste_cache = config.paths.id_cache.parent / "taste_enrichment.json"
        spin_films = build_spin_films(
            films,
            streaming_csv=config.paths.csv_report,
            id_cache_path=config.paths.id_cache,
            meta_path=spin_meta,
            taste_cache_path=taste_cache if taste_cache.exists() else None,
            overrides_path=config.paths.overrides,
            tmdb_api_key=config.tmdb_api_key,
            http=http,
        )
        write_spin_html(
            config.paths.spin_html,
            spin_films,
            source_label="Default watchlist",
        )
    except Exception as exc:  # noqa: BLE001 - spin page is optional UX
        logger.warning("Could not write spin.html: %s", exc)

    next_state = build_next_state(
        availability,
        previous,
        leaving_soon_alerts=alerted,
    )
    # Preserve last_changed inside state for stable CSV stamps.
    for item in availability:
        entry = next_state["films"].get(str(item.tmdb_id))
        if entry is not None and item.presence_status != PRESENCE_UNKNOWN:
            entry["last_changed"] = item.last_changed

    if args.dry_run:
        logger.info("Dry run: skipped state write and notifications.")
    else:
        save_state(config.paths.state, next_state)
        try:
            notify(config.ntfy_topic, diff, film_count=len(availability))
        except Exception as exc:  # noqa: BLE001
            logger.error("Notification failed: %s", exc)

    unmatched = sum(1 for item in resolved if item.tmdb_id is None)
    logger.info(
        "Done. %d watchlist films, %d resolved, %d unmatched, "
        "%d arrivals, %d departures, %d leaving soon.",
        len(films),
        len(availability),
        unmatched,
        len(diff.arrivals),
        len(diff.departures),
        len(diff.leaving_soon),
    )
    return 0


def main() -> None:
    """Console script entrypoint."""
    sys.exit(dispatch())


if __name__ == "__main__":
    main()
