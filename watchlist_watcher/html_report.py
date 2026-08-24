"""Generate a self-contained cinema-style HTML report viewer."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .diff import DiffResult
from .models import CONFIDENCE_PROBABLE, FilmAvailability

logger = logging.getLogger(__name__)

JUSTWATCH_ATTRIBUTION = (
    "Streaming availability data provided by JustWatch via The Movie Database (TMDB). "
    "No aggregator is authoritative; this tool prefers high recall over precision."
)


def _service_label(name: str, tier: str = "subscription") -> str:
    if tier == "library" or "library" in name.lower():
        if "(library" in name:
            return name
        return f"{name} (library, limited)"
    return name


def _split_aligned(raw: str) -> list[str]:
    value = (raw or "").strip()
    if not value:
        return []
    return [p.strip() for p in value.split(";")]


def _confidence_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    confirmed = probable = 0
    for row in rows:
        for svc in row.get("on_my_services") or []:
            if svc.get("confidence") == "confirmed":
                confirmed += 1
            else:
                probable += 1
    return confirmed, probable


def films_to_payload(
    films: list[FilmAvailability],
    diff: Optional[DiffResult] = None,
) -> dict[str, Any]:
    """Serialize availability into JSON embedded in the HTML viewer."""
    rows: list[dict[str, Any]] = []
    for item in films:
        on_my: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in item.on_my_services:
            if hit.canonical_name in seen:
                continue
            seen.add(hit.canonical_name)
            on_my.append(
                {
                    "name": hit.canonical_name,
                    "label": _service_label(hit.canonical_name, hit.tier),
                    "tier": hit.tier,
                    "confidence": hit.confidence or CONFIDENCE_PROBABLE,
                    "sources": hit.sources or "tmdb",
                    "motn_link": hit.motn_link or "",
                    "bucket": hit.bucket,
                }
            )
        rows.append(
            {
                "title": item.film.name,
                "year": item.film.year,
                "tmdb_id": item.tmdb_id,
                "on_my_services": on_my,
                "streaming": sorted({h.canonical_name for h in item.streaming}),
                "rent": sorted({h.canonical_name for h in item.rent}),
                "buy": sorted({h.canonical_name for h in item.buy}),
                "letterboxd_url": item.film.letterboxd_uri,
                "watch_link": item.watch_link,
                "expires_on": item.expires_on,
                "days_left": item.days_left,
                "last_changed": item.last_changed,
                "stale_tmdb_services": list(item.stale_tmdb_services),
                "stale_source": item.stale_source,
                "verification": item.verification_status,
                "motn_checked": item.motn_checked,
            }
        )

    events: list[dict[str, Any]] = []
    if diff and not diff.cold_start:
        for event in diff.leaving_soon + diff.arrivals + diff.departures + diff.new_to_watchlist:
            events.append(
                {
                    "kind": event.kind,
                    "title": event.title,
                    "year": event.year,
                    "provider": event.provider,
                    "detail": event.detail,
                    "days_left": event.days_left,
                    "suspect": bool(event.suspect),
                }
            )

    on_my_count = sum(1 for row in rows if row["on_my_services"])
    confirmed, probable = _confidence_counts(rows)
    return {
        "generated": True,
        "stats": {
            "total": len(rows),
            "on_my_services": on_my_count,
            "confirmed": confirmed,
            "probable": probable,
            "cold_start": bool(diff.cold_start) if diff else False,
        },
        "events": events,
        "films": rows,
        "attribution": JUSTWATCH_ATTRIBUTION,
    }


def payload_from_csv(csv_path: Path) -> dict[str, Any]:
    """Build viewer payload from an existing watchlist_streaming.csv."""
    rows: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            on_labels = _split_aligned(raw.get("on_my_services") or "")
            confidences = _split_aligned(raw.get("confidence") or "")
            sources_list = _split_aligned(raw.get("sources") or "")
            motn_links = _split_aligned(raw.get("motn_links") or "")

            on_my: list[dict[str, Any]] = []
            for idx, label in enumerate(on_labels):
                if not label:
                    continue
                tier = "library" if "library" in label.lower() else "subscription"
                name = label.split(" (library")[0].strip()
                on_my.append(
                    {
                        "name": name,
                        "label": label,
                        "tier": tier,
                        "confidence": (
                            confidences[idx]
                            if idx < len(confidences) and confidences[idx]
                            else CONFIDENCE_PROBABLE
                        ),
                        "sources": (
                            sources_list[idx]
                            if idx < len(sources_list) and sources_list[idx]
                            else "tmdb"
                        ),
                        "motn_link": (
                            motn_links[idx]
                            if idx < len(motn_links) and motn_links[idx]
                            else ""
                        ),
                        "bucket": "",
                    }
                )

            def split_list(key: str) -> list[str]:
                value = (raw.get(key) or "").strip()
                if not value:
                    return []
                return [p.strip() for p in value.split(";") if p.strip()]

            days_raw = (raw.get("days_left") or "").strip()
            days_left: Optional[int]
            if days_raw.isdigit() or (days_raw.startswith("-") and days_raw[1:].isdigit()):
                days_left = int(days_raw)
            else:
                days_left = None

            expires = (raw.get("expires_on") or "").strip()
            if expires.lower() == "unknown":
                expires = ""

            stale_raw = (raw.get("stale_tmdb_services") or "").strip()
            stale = [p.strip() for p in stale_raw.split(";") if p.strip()]

            year_raw = (raw.get("year") or "").strip()
            rows.append(
                {
                    "title": (raw.get("title") or "").strip(),
                    "year": int(year_raw) if year_raw.isdigit() else None,
                    "tmdb_id": int(raw["tmdb_id"]) if (raw.get("tmdb_id") or "").isdigit() else 0,
                    "on_my_services": on_my,
                    "streaming": split_list("streaming"),
                    "rent": split_list("rent"),
                    "buy": split_list("buy"),
                    "letterboxd_url": (raw.get("letterboxd_url") or "").strip(),
                    "watch_link": (raw.get("watch_link") or "").strip(),
                    "expires_on": expires or None,
                    "days_left": days_left,
                    "last_changed": (raw.get("last_changed") or "").strip() or None,
                    "stale_tmdb_services": stale,
                    "stale_source": (raw.get("stale_source") or "").strip(),
                    "verification": (raw.get("verification") or "").strip(),
                    "motn_checked": (raw.get("verification") or "").strip()
                    in {"checked", "verified"},
                }
            )

    confirmed, probable = _confidence_counts(rows)
    return {
        "generated": True,
        "stats": {
            "total": len(rows),
            "on_my_services": sum(1 for row in rows if row["on_my_services"]),
            "confirmed": confirmed,
            "probable": probable,
            "cold_start": False,
        },
        "events": [],
        "films": rows,
        "attribution": JUSTWATCH_ATTRIBUTION,
    }


def write_html_report(path: Path, payload: dict[str, Any]) -> None:
    """Write a self-contained interactive HTML report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data_json = json.dumps(payload, ensure_ascii=False)
    # Escape </script> so embedded JSON cannot break out of the script tag.
    data_json = data_json.replace("<", "\\u003c")
    document = _HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    path.write_text(document, encoding="utf-8")
    logger.info("Wrote HTML report to %s", path)
    # GitHub Pages serves index.html at the site root.
    if path.name != "index.html":
        index_path = path.with_name("index.html")
        index_path.write_text(document, encoding="utf-8")
        logger.info("Wrote Pages index to %s", index_path)


def write_html_from_films(
    path: Path,
    films: list[FilmAvailability],
    diff: Optional[DiffResult] = None,
) -> None:
    """Serialize live run results into report.html."""
    write_html_report(path, films_to_payload(films, diff))


# Cinema / repertory program viewer. Self-contained: no build step, no CDN auth.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Watchlist Watcher</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Syne:wght@600;700;800&display=swap" rel="stylesheet" />
  <style>
    :root {
      --paper: #e7eaf0;
      --ink: #12141a;
      --ink-soft: #3a4250;
      --mute: #6b7382;
      --line: rgba(18, 20, 26, 0.12);
      --ticket: #c81e2c;
      --ticket-soft: rgba(200, 30, 44, 0.1);
      --good: #0f6b4c;
      --shadow: 0 18px 50px rgba(18, 20, 26, 0.08);
      --radius: 2px;
      --max: 1080px;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Fraunces", Georgia, serif;
      font-optical-sizing: auto;
      background:
        radial-gradient(1200px 500px at 10% -10%, rgba(200, 30, 44, 0.08), transparent 55%),
        radial-gradient(900px 420px at 100% 0%, rgba(18, 20, 26, 0.06), transparent 50%),
        linear-gradient(180deg, #f2f4f8 0%, var(--paper) 40%, #dde2ea 100%);
      min-height: 100vh;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.045;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
      z-index: 0;
    }

    .wrap {
      position: relative;
      z-index: 1;
      width: min(100% - 2rem, var(--max));
      margin: 0 auto;
      padding: 2.25rem 0 4rem;
    }

    .mast {
      display: grid;
      gap: 1rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--line);
      animation: rise 0.7s ease both;
    }

    .brand-row {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }

    .brand {
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 800;
      font-size: clamp(2.4rem, 7vw, 4.4rem);
      letter-spacing: -0.04em;
      line-height: 0.92;
      margin: 0;
    }

    .brand span {
      color: var(--ticket);
    }

    .stub-row {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      flex-wrap: wrap;
    }

    .stub {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--ticket);
      border: 1px solid var(--ticket);
      padding: 0.45rem 0.7rem;
      background: var(--ticket-soft);
    }

    .spin-link {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--ink);
      text-decoration: none;
      border: 1px solid var(--line);
      padding: 0.45rem 0.7rem;
      background: rgba(255, 255, 255, 0.45);
      transition: border-color 0.15s ease, color 0.15s ease;
    }

    .spin-link:hover {
      color: var(--ticket);
      border-color: var(--ticket);
    }

    .lede {
      margin: 0;
      max-width: 36rem;
      font-size: 1.1rem;
      color: var(--ink-soft);
      line-height: 1.45;
    }

    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem 1.25rem;
      margin-top: 0.35rem;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--mute);
      animation: rise 0.8s 0.08s ease both;
    }

    .stats strong {
      color: var(--ink);
      font-size: 1.05rem;
      letter-spacing: 0;
      margin-right: 0.25rem;
    }

    .controls {
      position: sticky;
      top: 0;
      z-index: 5;
      display: grid;
      gap: 0.85rem;
      padding: 1rem 0 1.1rem;
      margin: 0.5rem 0 0.25rem;
      background: linear-gradient(180deg, rgba(231, 234, 240, 0.96), rgba(231, 234, 240, 0.88));
      backdrop-filter: blur(8px);
      border-bottom: 1px solid var(--line);
      animation: rise 0.85s 0.12s ease both;
    }

    .search {
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.65);
      color: var(--ink);
      font: inherit;
      font-size: 1.05rem;
      padding: 0.85rem 1rem;
      outline: none;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }

    .search:focus {
      border-color: rgba(200, 30, 44, 0.55);
      box-shadow: 0 0 0 3px rgba(200, 30, 44, 0.12);
    }

    .filters {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }

    .chip {
      appearance: none;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.45);
      color: var(--ink-soft);
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      padding: 0.5rem 0.75rem;
      cursor: pointer;
      transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease, transform 0.15s ease;
    }

    .chip:hover { transform: translateY(-1px); }
    .chip[aria-pressed="true"] {
      background: var(--ink);
      border-color: var(--ink);
      color: #f4f6fa;
    }
    .chip.ticket[aria-pressed="true"] {
      background: var(--ticket);
      border-color: var(--ticket);
    }

    .sort-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
      color: var(--mute);
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    select.sort {
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.65);
      color: var(--ink);
      font: inherit;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 0.45rem 0.6rem;
    }

    .events {
      display: grid;
      gap: 0.5rem;
      margin: 1.25rem 0 0.5rem;
    }

    .event {
      border-left: 3px solid var(--ticket);
      padding: 0.65rem 0.85rem;
      background: rgba(255,255,255,0.45);
      font-size: 0.98rem;
    }

    .event .k {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--ticket);
      margin-right: 0.5rem;
    }

    .section {
      margin-top: 1.75rem;
      animation: rise 0.75s 0.16s ease both;
    }

    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 0.75rem;
      padding-bottom: 0.45rem;
      border-bottom: 2px solid var(--ink);
    }

    .section-head h2 {
      margin: 0;
      font-family: "Syne", system-ui, sans-serif;
      font-size: clamp(1.2rem, 3vw, 1.6rem);
      font-weight: 800;
      letter-spacing: -0.03em;
    }

    .count {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--mute);
    }

    .list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 0;
    }

    .film {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 0.75rem 1rem;
      align-items: start;
      padding: 0.95rem 0.15rem;
      border-bottom: 1px solid var(--line);
      transition: background 0.2s ease, padding-left 0.2s ease;
    }

    .film:hover {
      background: rgba(255,255,255,0.4);
      padding-left: 0.35rem;
    }

    .title {
      margin: 0;
      font-size: 1.18rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.2;
    }

    .title a {
      color: inherit;
      text-decoration: none;
      background-image: linear-gradient(var(--ticket), var(--ticket));
      background-position: 0 100%;
      background-repeat: no-repeat;
      background-size: 0 2px;
      transition: background-size 0.25s ease;
    }

    .title a:hover { background-size: 100% 2px; }

    .meta {
      margin: 0.3rem 0 0;
      color: var(--mute);
      font-size: 0.92rem;
    }

    .tags {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      justify-content: flex-end;
      max-width: 20rem;
    }

    .tag {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.65rem;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 0.35rem 0.5rem;
      border: 1px solid var(--ink);
      background: #fff;
    }

    .tag.library {
      border-style: dashed;
    }

    .tag.muted {
      border-color: var(--line);
      color: var(--mute);
      font-weight: 700;
    }

    .tag.confirmed {
      color: var(--good);
      border-color: rgba(15, 107, 76, 0.45);
      background: rgba(15, 107, 76, 0.08);
    }

    .tag.probable {
      color: #7a5a12;
      border-color: rgba(122, 90, 18, 0.4);
      background: rgba(214, 168, 58, 0.14);
    }

    .confidence-key {
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      margin-top: 0.2rem;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--mute);
    }

    .actions {
      grid-column: 1 / -1;
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem 1rem;
      align-items: center;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .actions a, .actions button {
      color: var(--ink-soft);
      text-decoration: none;
      border: 0;
      background: none;
      padding: 0;
      font: inherit;
      letter-spacing: inherit;
      text-transform: inherit;
      cursor: pointer;
      border-bottom: 1px solid transparent;
      transition: color 0.2s ease, border-color 0.2s ease;
    }

    .actions a:hover, .actions button:hover {
      color: var(--ticket);
      border-color: var(--ticket);
    }

    .actions button.wrong {
      color: var(--ticket);
    }

    .toast {
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      z-index: 5;
      background: var(--ink);
      color: #f4f6fa;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 0.7rem 0.9rem;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }

    .toast.show { opacity: 1; }

    .empty {
      padding: 2rem 0.25rem;
      color: var(--mute);
      font-size: 1.05rem;
    }

    footer {
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid var(--line);
      color: var(--mute);
      font-size: 0.88rem;
      line-height: 1.5;
    }

    @keyframes rise {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (max-width: 640px) {
      .film { grid-template-columns: 1fr; }
      .tags { justify-content: flex-start; max-width: none; }
      .wrap { width: min(100% - 1.25rem, var(--max)); padding-top: 1.4rem; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="mast">
      <div class="brand-row">
        <h1 class="brand">Watchlist<br /><span>Watcher</span></h1>
        <div class="stub-row">
          <div class="stub">Now showing</div>
          <a class="spin-link" href="recommend.html">Taste picks</a>
          <a class="spin-link" href="spin.html">Tonight's spin</a>
        </div>
      </div>
      <p class="lede">Films from your Letterboxd watchlist that look available on services you care about. Confirmed = Netflix/Prime (audited). Probable = unaudited free/library. High recall on purpose.</p>
      <div class="stats" id="stats"></div>
      <div class="confidence-key" id="confidence-key"></div>
    </header>

    <div class="controls">
      <input class="search" id="search" type="search" placeholder="Search titles..." autocomplete="off" />
      <div class="filters" id="filters" role="toolbar" aria-label="Service filters"></div>
      <div class="sort-row">
        <div id="visible-count"></div>
        <div style="display:flex; gap:0.75rem; align-items:center; flex-wrap:wrap;">
          <button type="button" class="chip" id="export-feedback">Export feedback.csv</button>
          <label>
            Sort
            <select class="sort" id="sort">
              <option value="title">Title</option>
              <option value="year-desc">Year (new)</option>
              <option value="year-asc">Year (old)</option>
              <option value="days">Leaving soonest</option>
              <option value="confidence">Confidence</option>
            </select>
          </label>
        </div>
      </div>
    </div>

    <div class="events" id="events" hidden></div>
    <main id="main"></main>

    <footer id="footer"></footer>
  </div>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script id="report-data" type="application/json">__DATA_JSON__</script>
  <script>
    const data = JSON.parse(document.getElementById("report-data").textContent);

    const state = {
      query: "",
      service: "available",
      sort: "title",
    };

    const serviceSet = new Map();
    for (const film of data.films) {
      for (const svc of film.on_my_services || []) {
        serviceSet.set(svc.name, svc.label);
      }
    }

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    const FEEDBACK_KEY = "watchlist-watcher-feedback";

    function loadFeedback() {
      try {
        const raw = localStorage.getItem(FEEDBACK_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        return [];
      }
    }

    function saveFeedback(rows) {
      localStorage.setItem(FEEDBACK_KEY, JSON.stringify(rows));
    }

    function csvEscape(value) {
      const text = String(value ?? "");
      if (/[",\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
      return text;
    }

    function feedbackCsv(rows) {
      const header = "title,tmdb_id,provider,source,date";
      const body = rows.map((row) => [
        csvEscape(row.title),
        csvEscape(row.tmdb_id),
        csvEscape(row.provider),
        csvEscape(row.source),
        csvEscape(row.date),
      ].join(","));
      return [header, ...body].join("\n") + "\n";
    }

    function downloadFeedback(rows) {
      const blob = new Blob([feedbackCsv(rows)], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "feedback.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }

    function showToast(message) {
      const el = document.getElementById("toast");
      el.textContent = message;
      el.classList.add("show");
      clearTimeout(showToast._timer);
      showToast._timer = setTimeout(() => el.classList.remove("show"), 1800);
    }

    function markWrong(film, providerName, source) {
      const rows = loadFeedback();
      rows.push({
        title: film.title || "",
        tmdb_id: film.tmdb_id || "",
        provider: providerName || "",
        source: source || "tmdb",
        date: new Date().toISOString().slice(0, 10),
      });
      saveFeedback(rows);
      downloadFeedback(rows);
      showToast("Appended to feedback.csv");
    }

    function confidenceRank(film, serviceName) {
      const order = { probable: 0, confirmed: 1 };
      const hits = film.on_my_services || [];
      const hit = serviceName
        ? hits.find((s) => s.name === serviceName)
        : hits.slice().sort((a, b) => (order[a.confidence] ?? 0) - (order[b.confidence] ?? 0))[0];
      return order[hit?.confidence] ?? 0;
    }

    function renderStats() {
      const el = document.getElementById("stats");
      el.innerHTML = `
        <div><strong>${data.stats.total}</strong> on watchlist</div>
        <div><strong>${data.stats.on_my_services}</strong> on your services</div>
        <div><strong>${data.stats.confirmed || 0}</strong> confirmed</div>
        <div><strong>${data.stats.probable || 0}</strong> probable</div>
      `;
      const key = document.getElementById("confidence-key");
      key.innerHTML = `
        <span class="tag confirmed">confirmed · Netflix/Prime audited</span>
        <span class="tag probable">probable · unaudited free/library</span>
      `;
    }

    function renderFilters() {
      const root = document.getElementById("filters");
      const chips = [
        ["available", "On my services", true],
        ["confirmed", "Confirmed", true],
        ["probable", "Probable", true],
        ["all", "Entire list", false],
        ...[...serviceSet.entries()]
          .sort((a, b) => a[1].localeCompare(b[1]))
          .map(([name, label]) => [name, label, true]),
      ];
      root.innerHTML = chips.map(([id, label, ticket]) => `
        <button type="button" class="chip ${ticket && id !== "all" ? "ticket" : ""}"
          data-service="${esc(id)}" aria-pressed="${id === state.service}">
          ${esc(label)}
        </button>
      `).join("");
      root.querySelectorAll(".chip").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.service = btn.dataset.service;
          root.querySelectorAll(".chip").forEach((b) => {
            b.setAttribute("aria-pressed", String(b.dataset.service === state.service));
          });
          render();
        });
      });
    }

    function renderEvents() {
      const root = document.getElementById("events");
      if (!data.events || !data.events.length) {
        root.hidden = true;
        return;
      }
      root.hidden = false;
      const labels = {
        arrival: "Arrived",
        departure: "Left",
        leaving_soon: "Leaving soon",
        new_to_watchlist: "New",
      };
      root.innerHTML = data.events.slice(0, 12).map((ev) => `
        <div class="event">
          <span class="k">${esc(labels[ev.kind] || ev.kind)}${ev.suspect ? " · SUSPECT" : ""}</span>
          <strong>${esc(ev.title)}${ev.year ? ` (${esc(ev.year)})` : ""}</strong>
          ${ev.provider ? ` · ${esc(ev.provider)}` : ""}
          ${ev.detail ? ` · ${esc(ev.detail)}` : ""}
        </div>
      `).join("");
    }

    function matches(film) {
      const q = state.query.trim().toLowerCase();
      if (q && !(`${film.title} ${film.year || ""}`.toLowerCase().includes(q))) {
        return false;
      }
      const mine = film.on_my_services || [];
      if (state.service === "available") return mine.length > 0;
      if (state.service === "all") return true;
      if (state.service === "confirmed" || state.service === "probable") {
        return mine.some((s) => (s.confidence || "probable") === state.service);
      }
      return mine.some((s) => s.name === state.service);
    }

    function sortFilms(films) {
      const copy = [...films];
      const serviceName = (
        state.service !== "available"
        && state.service !== "all"
        && state.service !== "confirmed"
        && state.service !== "probable"
      ) ? state.service : null;
      if (state.sort === "year-desc") {
        copy.sort((a, b) => (b.year || 0) - (a.year || 0) || a.title.localeCompare(b.title));
      } else if (state.sort === "year-asc") {
        copy.sort((a, b) => (a.year || 9999) - (b.year || 9999) || a.title.localeCompare(b.title));
      } else if (state.sort === "days") {
        copy.sort((a, b) => {
          const da = a.days_left == null ? 1e9 : a.days_left;
          const db = b.days_left == null ? 1e9 : b.days_left;
          return da - db || a.title.localeCompare(b.title);
        });
      } else if (state.sort === "confidence") {
        copy.sort((a, b) => confidenceRank(a, serviceName) - confidenceRank(b, serviceName)
          || a.title.localeCompare(b.title));
      } else {
        copy.sort((a, b) => a.title.localeCompare(b.title));
      }
      return copy;
    }

    function groupFilms(films) {
      if (
        state.service !== "available"
        && state.service !== "all"
        && state.service !== "confirmed"
        && state.service !== "probable"
      ) {
        return [[serviceSet.get(state.service) || state.service, films]];
      }
      if (state.service === "all") {
        const available = films.filter((f) => (f.on_my_services || []).length);
        const rest = films.filter((f) => !(f.on_my_services || []).length);
        const groups = [];
        if (available.length) groups.push(["On your services", available]);
        if (rest.length) groups.push(["Not on your services", rest]);
        return groups;
      }
      if (state.service === "confirmed" || state.service === "probable") {
        return [[state.service[0].toUpperCase() + state.service.slice(1), films]];
      }
      const buckets = new Map();
      for (const film of films) {
        for (const svc of film.on_my_services || []) {
          if (!buckets.has(svc.label)) buckets.set(svc.label, []);
          buckets.get(svc.label).push(film);
        }
      }
      return [...buckets.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
    }

    function filmItem(film, groupLabel) {
      const year = film.year ? ` (${esc(film.year)})` : "";
      const expiry = film.expires_on
        ? `Expires ${esc(film.expires_on)}${film.days_left != null ? ` · ${esc(film.days_left)}d left` : ""}`
        : "Expiry unknown";
      let contextHit = null;
      if (groupLabel && serviceSet.size) {
        contextHit = (film.on_my_services || []).find((s) => s.label === groupLabel || s.name === groupLabel);
      }
      if (!contextHit && (state.service === "confirmed" || state.service === "probable")) {
        contextHit = (film.on_my_services || []).find((s) => (s.confidence || "probable") === state.service);
      }
      if (!contextHit) contextHit = (film.on_my_services || [])[0] || null;

      const tags = (film.on_my_services || []).map((s) => {
        const conf = s.confidence || "probable";
        const lib = s.tier === "library" ? " library" : "";
        return `<span class="tag ${esc(conf)}${lib}">${esc(s.label)} · ${esc(conf)}</span>`;
      }).join("");
      const streamingHint = !(film.on_my_services || []).length && film.streaming?.length
        ? `<span class="tag muted">${esc(film.streaming.slice(0, 2).join(" · "))}${film.streaming.length > 2 ? " +" : ""}</span>`
        : "";
      const motn = contextHit?.motn_link
        ? `<a href="${esc(contextHit.motn_link)}" target="_blank" rel="noopener">MotN link</a>`
        : "";
      const provider = contextHit?.name || "";
      const source = contextHit?.sources || "tmdb";
      return `
        <li class="film">
          <div>
            <h3 class="title">
              <a href="${esc(film.letterboxd_url)}" target="_blank" rel="noopener">${esc(film.title)}${year}</a>
            </h3>
            <p class="meta">${expiry}</p>
          </div>
          <div class="tags">${tags || streamingHint || `<span class="tag muted">Rent / buy only</span>`}</div>
          <div class="actions">
            <a href="${esc(film.letterboxd_url)}" target="_blank" rel="noopener">Letterboxd</a>
            <a href="${esc(film.watch_link)}" target="_blank" rel="noopener">TMDB watch page</a>
            ${motn}
            ${provider ? `<button type="button" class="wrong" data-provider="${esc(provider)}" data-source="${esc(source)}">Wrong</button>` : ""}
          </div>
        </li>
      `;
    }

    function bindWrongButtons(root, filmsByTitle) {
      root.querySelectorAll("button.wrong").forEach((btn) => {
        btn.addEventListener("click", () => {
          const item = btn.closest(".film");
          const titleLink = item?.querySelector(".title a");
          const title = titleLink ? titleLink.textContent.replace(/\s*\(\d{4}\)\s*$/, "").trim() : "";
          const film = filmsByTitle.get(title) || data.films.find((f) => f.title === title);
          if (!film) return;
          markWrong(film, btn.dataset.provider, btn.dataset.source);
        });
      });
    }

    function render() {
      const filtered = sortFilms(data.films.filter(matches));
      document.getElementById("visible-count").textContent = `${filtered.length} titles`;
      const main = document.getElementById("main");
      if (!filtered.length) {
        main.innerHTML = `<p class="empty">No films match this filter.</p>`;
        return;
      }
      const groups = groupFilms(filtered);
      const filmsByTitle = new Map(filtered.map((f) => [f.title, f]));
      main.innerHTML = groups.map(([label, films]) => `
        <section class="section">
          <div class="section-head">
            <h2>${esc(label)}</h2>
            <div class="count">${films.length} films</div>
          </div>
          <ul class="list">${films.map((film) => filmItem(film, label)).join("")}</ul>
        </section>
      `).join("");
      bindWrongButtons(main, filmsByTitle);
    }

    document.getElementById("search").addEventListener("input", (e) => {
      state.query = e.target.value;
      render();
    });
    document.getElementById("sort").addEventListener("change", (e) => {
      state.sort = e.target.value;
      render();
    });
    document.getElementById("export-feedback").addEventListener("click", () => {
      const rows = loadFeedback();
      downloadFeedback(rows);
      showToast(rows.length ? `Exported ${rows.length} rows` : "feedback.csv is empty");
    });
    document.getElementById("footer").textContent =
      `${data.attribution} MotN deep links appear when MotN returns a watchable option. ` +
      `Wrong only appends to feedback.csv (via download); it does not change overrides.`;

    renderStats();
    renderFilters();
    renderEvents();
    render();
  </script>
</body>
</html>
"""
