"""Self-contained HTML page for taste recommendations."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write_recommend_html(path: Path, payload: dict[str, Any]) -> None:
    """Write recommend.html with embedded JSON payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    path.write_text(_TEMPLATE.replace("__DATA_JSON__", data), encoding="utf-8")
    logger.info("Wrote recommendations HTML to %s", path)


def build_recommend_payload(
    *,
    top: list[dict[str, Any]],
    wildcard: Optional[dict[str, Any]],
    validation: dict[str, Any],
    top_positive: list[tuple[str, float]],
    top_negative: list[tuple[str, float]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated": True,
        "filters": filters,
        "validation": validation,
        "top_positive": [{"name": n, "coef": c} for n, c in top_positive[:8]],
        "top_negative": [{"name": n, "coef": c} for n, c in top_negative[:8]],
        "top": top,
        "wildcard": wildcard,
    }


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Taste picks · Watchlist Watcher</title>
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
      --ticket-soft: rgba(200, 30, 44, 0.12);
      --ok: #0f6b4c;
      --max: 920px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Fraunces", Georgia, serif;
      background:
        radial-gradient(900px 420px at 12% -8%, rgba(200, 30, 44, 0.1), transparent 55%),
        radial-gradient(700px 360px at 100% 0%, rgba(18, 20, 26, 0.07), transparent 50%),
        linear-gradient(180deg, #f2f4f8 0%, var(--paper) 45%, #dde2ea 100%);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.04;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    }
    .wrap {
      position: relative;
      width: min(100% - 2rem, var(--max));
      margin: 0 auto;
      padding: 2rem 0 3.5rem;
    }
    .top {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: end;
      flex-wrap: wrap;
      margin-bottom: 1.25rem;
    }
    h1 {
      margin: 0;
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 800;
      font-size: clamp(2rem, 6vw, 3.4rem);
      letter-spacing: -0.04em;
      line-height: 0.95;
    }
    h1 span { color: var(--ticket); }
    .nav {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .nav a {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--ink-soft);
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }
    .nav a:hover { color: var(--ticket); border-color: var(--ticket); }
    .lede {
      margin: 0 0 1.5rem;
      max-width: 36rem;
      color: var(--ink-soft);
      font-size: 1.05rem;
    }
    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem 1.25rem;
      margin: 0 0 1.75rem;
      padding: 0.9rem 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--mute);
    }
    .stats strong {
      display: block;
      margin-bottom: 0.15rem;
      font-size: 1.15rem;
      letter-spacing: -0.02em;
      text-transform: none;
      color: var(--ink);
    }
    .stats .ok strong { color: var(--ok); }
    h2 {
      margin: 2rem 0 0.85rem;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.78rem;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--mute);
    }
    ol.picks {
      list-style: none;
      margin: 0;
      padding: 0;
      counter-reset: pick;
    }
    .pick {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 0.85rem 1.1rem;
      padding: 1.05rem 0;
      border-bottom: 1px solid var(--line);
      animation: rise 0.55s ease both;
    }
    .pick:nth-child(1) { animation-delay: 0.05s; }
    .pick:nth-child(2) { animation-delay: 0.1s; }
    .pick:nth-child(3) { animation-delay: 0.15s; }
    .pick:nth-child(4) { animation-delay: 0.2s; }
    .pick:nth-child(5) { animation-delay: 0.25s; }
    .rank {
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 800;
      font-size: 1.6rem;
      letter-spacing: -0.04em;
      color: var(--ticket);
      line-height: 1;
      min-width: 2rem;
    }
    .title {
      margin: 0;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      line-height: 1.15;
    }
    .title a {
      color: inherit;
      text-decoration: none;
    }
    .title a:hover { color: var(--ticket); }
    .meta {
      margin: 0.35rem 0 0;
      color: var(--ink-soft);
      font-size: 0.98rem;
    }
    .tags {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin-top: 0.55rem;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      padding: 0.28rem 0.55rem;
      border: 1px solid var(--line);
      border-radius: 2px;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ink-soft);
      background: rgba(255,255,255,0.45);
    }
    .tag.score {
      color: var(--ok);
      border-color: rgba(15, 107, 76, 0.35);
      background: rgba(15, 107, 76, 0.08);
    }
    .wildcard {
      margin-top: 0.5rem;
      padding: 1.15rem 1.2rem;
      border: 1px dashed rgba(200, 30, 44, 0.45);
      background: var(--ticket-soft);
      animation: rise 0.65s ease 0.3s both;
    }
    .wildcard .eyebrow {
      margin: 0 0 0.45rem;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--ticket);
    }
    .features {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem 1.5rem;
    }
    @media (max-width: 640px) {
      .features { grid-template-columns: 1fr; }
    }
    .features ul {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .features li {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.35rem 0;
      border-bottom: 1px solid var(--line);
      font-size: 0.95rem;
    }
    .features .coef {
      font-family: "Syne", system-ui, sans-serif;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .features .pos { color: var(--ok); }
    .features .neg { color: var(--ticket); }
    .empty {
      color: var(--mute);
      font-style: italic;
    }
    @keyframes rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: none; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>Taste<br /><span>picks</span></h1>
      <div class="nav">
        <a href="index.html">Report</a>
        <a href="spin.html">Spin</a>
      </div>
    </div>
    <p class="lede" id="lede">Available watchlist titles ranked by how much more than the crowd you tend to like them.</p>
    <div class="stats" id="stats"></div>
    <h2>Top fits</h2>
    <ol class="picks" id="picks"></ol>
    <div id="wildcard"></div>
    <h2>What the model leaned on</h2>
    <div class="features" id="features"></div>
  </div>
  <script id="report-data" type="application/json">__DATA_JSON__</script>
  <script>
    const data = JSON.parse(document.getElementById("report-data").textContent);

    function esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      })[c]);
    }

    function render() {
      const v = data.validation || {};
      const filters = data.filters || {};
      const filterBits = [];
      if (filters.runtime_max != null) filterBits.push(`runtime ≤ ${filters.runtime_max}m`);
      if (filters.mood) filterBits.push(`mood ${filters.mood}`);
      if (filters.decade) filterBits.push(`decade ${filters.decade}`);
      if (filters.unwatched_director) filterBits.push("unwatched directors");
      if (filterBits.length) {
        document.getElementById("lede").textContent +=
          ` Filtered to: ${filterBits.join(", ")}.`;
      }

      document.getElementById("stats").innerHTML = `
        <div><strong>${(data.top || []).length}</strong> top picks</div>
        <div><strong>${v.n_train ?? "–"}</strong> train / ${v.n_holdout ?? "–"} holdout</div>
        <div><strong>${v.mae_model != null ? Number(v.mae_model).toFixed(3) : "–"}</strong> model MAE</div>
        <div class="ok"><strong>${v.beats_baseline ? "yes" : "no"}</strong> beats baseline</div>
      `;

      const picks = document.getElementById("picks");
      const rows = data.top || [];
      if (!rows.length) {
        picks.innerHTML = `<li class="empty">No recommendations yet. Run recommend.</li>`;
      } else {
        picks.innerHTML = rows.map((row, i) => {
          const year = row.year ? ` (${esc(row.year)})` : "";
          const href = row.letterboxd_url || "#";
          const services = (row.on_my_services || []).map((s) =>
            `<span class="tag">${esc(s)}</span>`
          ).join("");
          return `
            <li class="pick">
              <div class="rank">${String(i + 1).padStart(2, "0")}</div>
              <div>
                <h3 class="title"><a href="${esc(href)}" target="_blank" rel="noopener">${esc(row.title)}${year}</a></h3>
                <p class="meta">${esc(row.why || row.explanation || "")}</p>
                <div class="tags">
                  <span class="tag score">${row.score >= 0 ? "+" : ""}${Number(row.score).toFixed(2)} residual</span>
                  ${services}
                </div>
              </div>
            </li>`;
        }).join("");
      }

      const wild = data.wildcard;
      const wildEl = document.getElementById("wildcard");
      if (wild) {
        const year = wild.year ? ` (${esc(wild.year)})` : "";
        const href = wild.letterboxd_url || "#";
        wildEl.innerHTML = `
          <h2>Wildcard</h2>
          <div class="wildcard">
            <p class="eyebrow">Off your usual beat</p>
            <h3 class="title"><a href="${esc(href)}" target="_blank" rel="noopener">${esc(wild.title)}${year}</a></h3>
            <p class="meta">${esc(wild.why || wild.explanation || "")}</p>
            <div class="tags">
              <span class="tag score">${wild.score >= 0 ? "+" : ""}${Number(wild.score).toFixed(2)} residual</span>
              ${(wild.on_my_services || []).map((s) => `<span class="tag">${esc(s)}</span>`).join("")}
            </div>
          </div>`;
      }

      const pos = (data.top_positive || []).map((f) =>
        `<li><span>${esc(f.name)}</span><span class="coef pos">+${Number(f.coef).toFixed(3)}</span></li>`
      ).join("") || "<li class='empty'>none</li>";
      const neg = (data.top_negative || []).map((f) =>
        `<li><span>${esc(f.name)}</span><span class="coef neg">${Number(f.coef).toFixed(3)}</span></li>`
      ).join("") || "<li class='empty'>none</li>";
      document.getElementById("features").innerHTML = `
        <div>
          <h2 style="margin-top:0">Like more than the crowd</h2>
          <ul>${pos}</ul>
        </div>
        <div>
          <h2 style="margin-top:0">Like less than the crowd</h2>
          <ul>${neg}</ul>
        </div>`;
    }
    render();
  </script>
</body>
</html>
"""
