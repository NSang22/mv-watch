"""CLI entrypoint for watchlist-watcher."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .config import load_config
from .diff import (
    apply_last_changed,
    build_next_state,
    compute_diff,
    load_state,
    save_state,
    utc_now_iso,
)
from .enrich import ExpiryEnricher, enrichment_enabled
from .http_util import HttpClient
from .notify import notify, write_csv, write_report
from .providers import ProviderClient
from .resolve import IdResolver
from .watchlist import load_watchlist

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Create the argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="watchlist-watcher",
        description=(
            "Cross-reference a Letterboxd watchlist with TMDB streaming "
            "availability and notify when films arrive on your services."
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
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


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

    watchlist_path = Path(args.watchlist) if args.watchlist else config.paths.watchlist
    username = config.letterboxd_user if args.scrape or not watchlist_path.exists() else None

    try:
        if args.scrape:
            films = load_watchlist(None, username=config.letterboxd_user, http=http)
        elif watchlist_path.exists():
            films = load_watchlist(watchlist_path, http=http)
        else:
            films = load_watchlist(None, username=username, http=http)
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
        enricher.enrich_all(availability)
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
    run_ts = utc_now_iso()
    apply_last_changed(availability, previous, diff, run_ts)

    write_csv(config.paths.csv_report, availability)
    write_report(
        config.paths.markdown_report,
        availability,
        diff,
        enrichment_used=used_enrichment,
    )

    next_state = build_next_state(
        availability,
        previous,
        leaving_soon_alerts=alerted,
    )
    # Preserve last_changed inside state for stable CSV stamps.
    for item in availability:
        entry = next_state["films"].get(str(item.tmdb_id))
        if entry is not None:
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
    sys.exit(run())


if __name__ == "__main__":
    main()
