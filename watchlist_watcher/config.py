"""Configuration loading for watchlist-watcher."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class ServiceConfig:
    """One configured streaming service with alias patterns."""

    name: str
    match: list[str]
    exclude: list[str] = field(default_factory=list)
    tier: str = "subscription"  # subscription | library


@dataclass
class StreamingAvailabilityConfig:
    """Optional Movie of the Night enrichment settings."""

    enabled: bool = False
    base_url: str = "https://api.movieofthenight.com/v4"
    leaving_soon_days: list[int] = field(default_factory=lambda: [14, 3])
    catalog_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class PathsConfig:
    """Output and cache file locations."""

    watchlist: Path
    state: Path
    id_cache: Path
    overrides: Path
    unmatched: Path
    csv_report: Path
    markdown_report: Path


@dataclass
class AppConfig:
    """Runtime configuration assembled from YAML and environment variables."""

    region: str
    request_delay_seconds: float
    failure_rate_threshold: float
    paths: PathsConfig
    services: list[ServiceConfig]
    streaming_availability: StreamingAvailabilityConfig
    tmdb_api_key: str
    ntfy_topic: Optional[str]
    letterboxd_user: Optional[str]
    streaming_availability_api_key: Optional[str]


def _as_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path


def load_config(config_path: Path | str = "config.yaml") -> AppConfig:
    """Load config.yaml and required environment variables.

    Raises:
        FileNotFoundError: When the YAML file is missing.
        ValueError: When TMDB_API_KEY is unset.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        example = config_path.with_name("config.example.yaml")
        raise FileNotFoundError(
            f"Missing {config_path}. Copy {example.name} to {config_path.name} and edit it."
        )

    with config_path.open(encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}

    base = config_path.parent.resolve()
    paths_raw = raw.get("paths") or {}
    paths = PathsConfig(
        watchlist=_as_path(paths_raw.get("watchlist", "watchlist.csv"), base),
        state=_as_path(paths_raw.get("state", "state.json"), base),
        id_cache=_as_path(paths_raw.get("id_cache", "cache/id_cache.json"), base),
        overrides=_as_path(paths_raw.get("overrides", "overrides.json"), base),
        unmatched=_as_path(paths_raw.get("unmatched", "unmatched.csv"), base),
        csv_report=_as_path(paths_raw.get("csv_report", "watchlist_streaming.csv"), base),
        markdown_report=_as_path(paths_raw.get("markdown_report", "report.md"), base),
    )

    services: list[ServiceConfig] = []
    for item in raw.get("services") or []:
        services.append(
            ServiceConfig(
                name=item["name"],
                match=list(item.get("match") or []),
                exclude=list(item.get("exclude") or []),
                tier=item.get("tier", "subscription"),
            )
        )

    sa_raw = raw.get("streaming_availability") or {}
    sa = StreamingAvailabilityConfig(
        enabled=bool(sa_raw.get("enabled", False)),
        base_url=sa_raw.get("base_url", "https://api.movieofthenight.com/v4"),
        leaving_soon_days=list(sa_raw.get("leaving_soon_days") or [14, 3]),
        catalog_ids=dict(sa_raw.get("catalog_ids") or {}),
    )

    tmdb_api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not tmdb_api_key:
        raise ValueError("TMDB_API_KEY environment variable is required.")

    return AppConfig(
        region=str(raw.get("region", "US")).upper(),
        request_delay_seconds=float(raw.get("request_delay_seconds", 0.35)),
        failure_rate_threshold=float(raw.get("failure_rate_threshold", 0.2)),
        paths=paths,
        services=services,
        streaming_availability=sa,
        tmdb_api_key=tmdb_api_key,
        ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
        letterboxd_user=os.environ.get("LETTERBOXD_USER") or None,
        streaming_availability_api_key=os.environ.get("STREAMING_AVAILABILITY_API_KEY")
        or None,
    )
