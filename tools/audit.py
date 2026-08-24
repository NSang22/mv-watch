#!/usr/bin/env python3
"""One-off blinded availability audit (Netflix + Amazon Prime Video).

Builds a stratified sample of title-provider pairs, writes self-contained
audit.html for human judgments, and exports audit_results.csv with
precision/recall per source and provider.

Does not modify the main watchlist-watcher pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from watchlist_watcher.config import load_config  # noqa: E402
from watchlist_watcher.http_util import (  # noqa: E402
    HTTPStatusError,
    HttpClient,
    TransientHTTPError,
)
from watchlist_watcher.providers import TMDB_BASE, match_service  # noqa: E402
from watchlist_watcher.resolve import IdResolver  # noqa: E402
from watchlist_watcher.watchlist import load_watchlist  # noqa: E402

logger = logging.getLogger("audit")

AUDIT_PROVIDERS = ("Netflix", "Amazon Prime Video")
STRATA = ("both_available", "disagree", "neither_available")
WATCHABLE_MOTN_TYPES = frozenset({"subscription", "free"})

SEARCH_URLS = {
    "Netflix": "https://www.netflix.com/search?q={query}",
    "Amazon Prime Video": "https://www.amazon.com/s?k={query}&i=instant-video",
}


@dataclass
class AuditRow:
    """One blinded title-provider judgment unit."""

    id: str
    title: str
    year: Optional[int]
    tmdb_id: int
    provider: str
    stratum: str
    tmdb_says: str  # available | unavailable
    motn_says: str  # available | unavailable
    motn_link: str
    check_url: str
    letterboxd_url: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sample watchlist title-provider pairs for a blinded Netflix/Prime "
            "availability audit and write audit.html."
        )
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--per-stratum",
        type=int,
        default=12,
        help="Target rows per stratum (default: 12 → ~36 total)",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for sampling")
    parser.add_argument(
        "--out-html",
        default="audit.html",
        help="Output HTML path (default: ./audit.html)",
    )
    parser.add_argument(
        "--out-sample",
        default="audit_sample.json",
        help="JSON dump of the sampled rows (default: ./audit_sample.json)",
    )
    parser.add_argument(
        "--max-films",
        type=int,
        default=0,
        help="Optional cap on films to probe (0 = all resolved)",
    )
    return parser


def _catalog_id(config: Any, provider: str) -> str:
    return (config.streaming_availability.catalog_ids or {}).get(provider, "")


def _search_url(provider: str, title: str) -> str:
    return SEARCH_URLS[provider].format(query=quote_plus(title))


def _tmdb_available(
    region_payload: dict[str, Any],
    provider: str,
    services: list[Any],
) -> bool:
    for bucket in ("flatrate", "free", "ads"):
        for entry in region_payload.get(bucket) or []:
            raw = (entry.get("provider_name") or "").strip()
            matched = match_service(raw, services)
            if matched is not None and matched.name == provider:
                return True
    return False


def _parse_motn_for_providers(
    data: dict[str, Any],
    *,
    region: str,
    catalog_ids: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Return provider -> {available: bool, link: str} for Netflix/Prime."""
    streaming = data.get("streamingOptions") or {}
    options = streaming.get(region.lower()) or streaming.get(region.upper()) or []
    out: dict[str, dict[str, Any]] = {
        name: {"available": False, "link": ""} for name in AUDIT_PROVIDERS
    }
    id_to_name = {
        cid: name for name, cid in catalog_ids.items() if name in AUDIT_PROVIDERS
    }
    for option in options:
        option_type = (option.get("type") or "").lower()
        if option_type not in WATCHABLE_MOTN_TYPES:
            continue
        # Amazon Channels / similar show up as addon and falsely inflate Prime.
        if option.get("addon"):
            continue
        service_obj = option.get("service") or {}
        service_id = str(service_obj.get("id") or "").strip()
        name = id_to_name.get(service_id)
        if name is None:
            continue
        out[name]["available"] = True
        link = (option.get("link") or option.get("videoLink") or "").strip()
        if link and not out[name]["link"]:
            out[name]["link"] = link
    return out


def _stratum_for(tmdb_yes: bool, motn_yes: bool) -> str:
    if tmdb_yes and motn_yes:
        return "both_available"
    if tmdb_yes != motn_yes:
        return "disagree"
    return "neither_available"


def collect_candidates(
    *,
    config: Any,
    http: HttpClient,
    resolved: list[Any],
    rng: random.Random,
    max_films: int,
) -> dict[str, list[AuditRow]]:
    """Probe TMDB + MotN and bucket title-provider pairs by stratum."""
    if not config.streaming_availability_api_key:
        raise SystemExit(
            "STREAMING_AVAILABILITY_API_KEY (or MOTN_API_KEY) is required for the audit."
        )

    films = [item for item in resolved if item.tmdb_id]
    rng.shuffle(films)
    if max_films > 0:
        films = films[:max_films]

    catalog_ids = {
        name: _catalog_id(config, name)
        for name in AUDIT_PROVIDERS
        if _catalog_id(config, name)
    }
    if len(catalog_ids) != len(AUDIT_PROVIDERS):
        raise SystemExit(
            "config streaming_availability.catalog_ids must include Netflix and "
            "Amazon Prime Video."
        )

    pools: dict[str, list[AuditRow]] = {name: [] for name in STRATA}
    motn_headers = {"X-API-Key": config.streaming_availability_api_key}
    motn_base = config.streaming_availability.base_url.rstrip("/")

    for index, item in enumerate(films, start=1):
        tmdb_id = int(item.tmdb_id)
        title = item.film.name
        year = item.film.year
        logger.info("[%d/%d] %s (%s)", index, len(films), title, year or "?")

        try:
            data = http.get_json(
                f"{TMDB_BASE}/movie/{tmdb_id}/watch/providers",
                params={"api_key": config.tmdb_api_key},
            )
        except (TransientHTTPError, HTTPStatusError) as exc:
            logger.warning("TMDB providers failed for %s: %s", title, exc)
            continue

        results = data.get("results") or {}
        region_payload = results.get(config.region) or {}
        if not isinstance(region_payload, dict):
            continue

        try:
            motn = http.get_json(
                f"{motn_base}/shows/movie/{tmdb_id}",
                params={"country": config.region.lower()},
                headers=motn_headers,
            )
        except (TransientHTTPError, HTTPStatusError) as exc:
            logger.warning("MotN lookup failed for %s: %s", title, exc)
            continue

        motn_by = _parse_motn_for_providers(
            motn,
            region=config.region,
            catalog_ids=catalog_ids,
        )

        for provider in AUDIT_PROVIDERS:
            tmdb_yes = _tmdb_available(region_payload, provider, config.services)
            motn_info = motn_by[provider]
            motn_yes = bool(motn_info["available"])
            stratum = _stratum_for(tmdb_yes, motn_yes)
            motn_link = motn_info["link"] if motn_yes else ""
            check_url = motn_link or _search_url(provider, title)
            pools[stratum].append(
                AuditRow(
                    id=f"{tmdb_id}:{provider}",
                    title=title,
                    year=year,
                    tmdb_id=tmdb_id,
                    provider=provider,
                    stratum=stratum,
                    tmdb_says="available" if tmdb_yes else "unavailable",
                    motn_says="available" if motn_yes else "unavailable",
                    motn_link=motn_link,
                    check_url=check_url,
                    letterboxd_url=item.film.letterboxd_uri,
                )
            )

    return pools


def sample_stratified(
    pools: dict[str, list[AuditRow]],
    *,
    per_stratum: int,
    rng: random.Random,
) -> list[AuditRow]:
    """Draw roughly equal strata, balancing providers when possible."""
    sampled: list[AuditRow] = []
    for stratum in STRATA:
        pool = list(pools.get(stratum) or [])
        rng.shuffle(pool)
        if len(pool) < per_stratum:
            logger.warning(
                "Stratum %s has only %d candidates (wanted %d).",
                stratum,
                len(pool),
                per_stratum,
            )

        by_provider: dict[str, list[AuditRow]] = {p: [] for p in AUDIT_PROVIDERS}
        leftover: list[AuditRow] = []
        for row in pool:
            if row.provider in by_provider:
                by_provider[row.provider].append(row)
            else:
                leftover.append(row)

        pick: list[AuditRow] = []
        providers = list(AUDIT_PROVIDERS)
        while len(pick) < per_stratum and any(by_provider[p] for p in providers):
            for provider in providers:
                if len(pick) >= per_stratum:
                    break
                if by_provider[provider]:
                    pick.append(by_provider[provider].pop())
        while len(pick) < per_stratum and leftover:
            pick.append(leftover.pop())
        sampled.extend(pick[:per_stratum])

    rng.shuffle(sampled)
    return sampled


def write_sample_json(path: Path, rows: list[AuditRow]) -> None:
    path.write_text(
        json.dumps([asdict(row) for row in rows], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_audit_html(path: Path, rows: list[AuditRow]) -> None:
    payload = {
        "generated": True,
        "providers": list(AUDIT_PROVIDERS),
        "strata": list(STRATA),
        "rows": [asdict(row) for row in rows],
    }
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    path.write_text(_AUDIT_HTML.replace("__DATA_JSON__", data), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(override=False)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    http = HttpClient(delay_seconds=config.request_delay_seconds)
    rng = random.Random(args.seed)

    films = load_watchlist(config.paths.watchlist)
    resolver = IdResolver(config, http)
    resolved = resolver.resolve_all(films)
    resolver.save_cache()

    logger.info("Probing TMDB + MotN for stratified audit sample...")
    pools = collect_candidates(
        config=config,
        http=http,
        resolved=resolved,
        rng=rng,
        max_films=args.max_films,
    )
    for stratum in STRATA:
        logger.info("Pool %s: %d", stratum, len(pools[stratum]))

    rows = sample_stratified(pools, per_stratum=args.per_stratum, rng=rng)
    if not rows:
        logger.error("No audit rows sampled.")
        return 2

    out_html = Path(args.out_html)
    out_sample = Path(args.out_sample)
    write_sample_json(out_sample, rows)
    write_audit_html(out_html, rows)

    counts = {s: sum(1 for r in rows if r.stratum == s) for s in STRATA}
    providers = {p: sum(1 for r in rows if r.provider == p) for p in AUDIT_PROVIDERS}
    logger.info("Wrote %s (%d rows)", out_html.resolve(), len(rows))
    logger.info("Wrote %s", out_sample.resolve())
    logger.info("Stratum counts: %s", counts)
    logger.info("Provider counts: %s", providers)
    logger.info("Open %s, judge each row, then Export audit_results.csv.", out_html.name)
    return 0


_AUDIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Availability Audit</title>
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
      --max: 920px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Fraunces", Georgia, serif;
      background:
        radial-gradient(1000px 420px at 8% -10%, rgba(200, 30, 44, 0.07), transparent 55%),
        linear-gradient(180deg, #f2f4f8 0%, var(--paper) 45%, #dde2ea 100%);
      min-height: 100vh;
    }
    .wrap {
      width: min(100% - 2rem, var(--max));
      margin: 0 auto;
      padding: 2rem 0 4rem;
    }
    h1 {
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 800;
      font-size: clamp(2rem, 6vw, 3.2rem);
      letter-spacing: -0.04em;
      line-height: 0.95;
      margin: 0 0 0.6rem;
    }
    h1 span { color: var(--ticket); }
    .lede {
      margin: 0 0 1.25rem;
      color: var(--ink-soft);
      max-width: 38rem;
      line-height: 1.45;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1.25rem;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--mute);
    }
    .toolbar button {
      appearance: none;
      border: 1px solid var(--ticket);
      background: var(--ticket-soft);
      color: var(--ticket);
      font: inherit;
      letter-spacing: inherit;
      text-transform: inherit;
      padding: 0.55rem 0.8rem;
      cursor: pointer;
    }
    .toolbar button:hover { background: var(--ticket); color: #fff; }
    .row {
      border-top: 1px solid var(--line);
      padding: 1rem 0;
      display: grid;
      gap: 0.75rem;
    }
    .title {
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 800;
      font-size: 1.15rem;
      letter-spacing: -0.02em;
      margin: 0;
    }
    .meta {
      margin: 0.2rem 0 0;
      color: var(--mute);
      font-size: 0.95rem;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      align-items: center;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .actions a {
      color: var(--ink-soft);
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }
    .actions a:hover { color: var(--ticket); border-color: var(--ticket); }
    .vote button {
      appearance: none;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.65);
      color: var(--ink-soft);
      font: inherit;
      letter-spacing: inherit;
      text-transform: inherit;
      padding: 0.5rem 0.75rem;
      cursor: pointer;
    }
    .vote button[aria-pressed="true"] {
      background: var(--ink);
      border-color: var(--ink);
      color: #f4f6fa;
    }
    .reveal {
      display: none;
      margin-top: 0.35rem;
      padding: 0.65rem 0.75rem;
      background: rgba(255,255,255,0.55);
      border-left: 3px solid var(--ink);
      color: var(--ink-soft);
      font-size: 0.95rem;
    }
    .reveal.show { display: block; }
    .reveal strong {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.7rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--mute);
    }
    .metrics {
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid var(--line);
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.85rem;
      line-height: 1.5;
      color: var(--ink-soft);
    }
    .done { color: var(--good); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Availability<br /><span>Audit</span></h1>
    <p class="lede">
      One title and one service per row. Open the service link, then mark whether
      it is actually available. Sources stay hidden until you vote. Rows look the
      same on purpose.
    </p>
    <div class="toolbar">
      <div id="progress">0 / 0 judged</div>
      <button type="button" id="export">Export audit_results.csv</button>
    </div>
    <main id="main"></main>
    <pre class="metrics" id="metrics" hidden></pre>
  </div>

  <script id="audit-data" type="application/json">__DATA_JSON__</script>
  <script>
    const data = JSON.parse(document.getElementById("audit-data").textContent);
    const STORAGE_KEY = "watchlist-audit-verdicts-v1";
    const state = loadState();

    function loadState() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch (_) {
        return {};
      }
    }

    function saveState() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    function esc(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function csvEscape(value) {
      const text = String(value ?? "");
      if (/[",\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
      return text;
    }

    function render() {
      const main = document.getElementById("main");
      main.innerHTML = data.rows.map((row) => {
        const verdict = state[row.id]?.verdict || "";
        const revealed = Boolean(verdict);
        const year = row.year ? ` (${esc(row.year)})` : "";
        return `
          <article class="row" data-id="${esc(row.id)}">
            <div>
              <h2 class="title">${esc(row.title)}${year}</h2>
              <p class="meta">${esc(row.provider)}</p>
            </div>
            <div class="actions">
              <a href="${esc(row.check_url)}" target="_blank" rel="noopener">Open ${esc(row.provider)}</a>
              <a href="${esc(row.letterboxd_url)}" target="_blank" rel="noopener">Letterboxd</a>
            </div>
            <div class="vote" role="group" aria-label="Availability verdict">
              ${["yes", "no", "unsure"].map((v) => `
                <button type="button" data-verdict="${v}" aria-pressed="${verdict === v}">${v}</button>
              `).join("")}
            </div>
            <div class="reveal ${revealed ? "show" : ""}">
              <div><strong>After your vote</strong></div>
              <div>TMDB: ${esc(row.tmdb_says)}</div>
              <div>MotN: ${esc(row.motn_says)}</div>
              <div>Stratum: ${esc(row.stratum)}</div>
            </div>
          </article>
        `;
      }).join("");

      main.querySelectorAll(".vote button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const row = btn.closest(".row");
          const id = row.dataset.id;
          state[id] = {
            verdict: btn.dataset.verdict,
            voted_at: new Date().toISOString(),
          };
          saveState();
          render();
          updateProgress();
        });
      });
    }

    function updateProgress() {
      const total = data.rows.length;
      const judged = data.rows.filter((row) => state[row.id]?.verdict).length;
      const el = document.getElementById("progress");
      el.textContent = `${judged} / ${total} judged`;
      el.classList.toggle("done", judged === total && total > 0);
    }

    function truth(verdict) {
      if (verdict === "yes") return true;
      if (verdict === "no") return false;
      return null;
    }

    function metrics() {
      const lines = [];
      const combos = [];
      for (const source of ["tmdb", "motn"]) {
        for (const provider of data.providers) {
          combos.push({ source, provider });
        }
      }

      function score(filterFn, claimFn) {
        let tp = 0, fp = 0, fn = 0, n = 0;
        for (const row of data.rows) {
          if (!filterFn(row)) continue;
          const verdict = state[row.id]?.verdict;
          const actual = truth(verdict);
          if (actual === null) continue;
          const predicted = claimFn(row) === "available";
          n += 1;
          if (predicted && actual) tp += 1;
          if (predicted && !actual) fp += 1;
          if (!predicted && actual) fn += 1;
        }
        const precision = (tp + fp) ? tp / (tp + fp) : null;
        const recall = (tp + fn) ? tp / (tp + fn) : null;
        return { precision, recall, n };
      }

      function fmt(value) {
        return value == null ? "n/a" : value.toFixed(3);
      }

      lines.push("Per source x provider (unsure excluded)");
      for (const { source, provider } of combos) {
        const m = score(
          (row) => row.provider === provider,
          (row) => source === "tmdb" ? row.tmdb_says : row.motn_says,
        );
        lines.push(
          `${source.toUpperCase()} / ${provider}: precision=${fmt(m.precision)} recall=${fmt(m.recall)} n=${m.n}`
        );
      }

      lines.push("");
      lines.push("Per source (all providers)");
      for (const source of ["tmdb", "motn"]) {
        const m = score(
          () => true,
          (row) => source === "tmdb" ? row.tmdb_says : row.motn_says,
        );
        lines.push(
          `${source.toUpperCase()}: precision=${fmt(m.precision)} recall=${fmt(m.recall)} n=${m.n}`
        );
      }

      lines.push("");
      lines.push("Per provider (row counts judged)");
      for (const provider of data.providers) {
        const n = data.rows.filter((row) => (
          row.provider === provider && truth(state[row.id]?.verdict) !== null
        )).length;
        lines.push(`${provider}: judged n=${n}`);
      }
      return lines.join("\n");
    }

    function exportCsv() {
      const header = [
        "title", "year", "tmdb_id", "provider", "stratum",
        "tmdb_says", "motn_says", "check_url", "verdict", "voted_at",
      ];
      const body = data.rows.map((row) => {
        const verdict = state[row.id] || {};
        return [
          row.title,
          row.year ?? "",
          row.tmdb_id,
          row.provider,
          row.stratum,
          row.tmdb_says,
          row.motn_says,
          row.check_url,
          verdict.verdict || "",
          verdict.voted_at || "",
        ].map(csvEscape).join(",");
      });
      const csv = [header.join(","), ...body].join("\n") + "\n";
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit_results.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      const text = metrics();
      const metricsEl = document.getElementById("metrics");
      metricsEl.hidden = false;
      metricsEl.textContent = text;
      console.log(text);
    }

    document.getElementById("export").addEventListener("click", exportCsv);
    render();
    updateProgress();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
