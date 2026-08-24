"""Generate a standalone random-movie spin wheel page."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TITLE_HEADERS = {
    "name",
    "title",
    "film",
    "movie",
    "movie title",
    "film title",
}


def extract_titles_from_csv_text(text: str) -> list[str]:
    """Pull movie titles from CSV/TSV text in a forgiving way.

    Prefers Name/title columns (Letterboxd / streaming export). Falls back to
    the first column when the file is a bare title list.
    """
    sample = text.lstrip("\ufeff")
    if not sample.strip():
        return []

    first_line = sample.splitlines()[0]
    delimiter = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(sample), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip() for cell in rows[0]]
    header_lower = [h.casefold() for h in header]
    title_idx: Optional[int] = None
    for idx, name in enumerate(header_lower):
        if name in TITLE_HEADERS:
            title_idx = idx
            break

    titles: list[str] = []
    if title_idx is not None:
        for row in rows[1:]:
            if title_idx >= len(row):
                continue
            title = row[title_idx].strip().strip('"')
            if title:
                titles.append(title)
    else:
        # Bare list: use first column; skip a header-looking first cell.
        start = 0
        if header_lower and header_lower[0] in TITLE_HEADERS | {"titles", "movies", "films"}:
            start = 1
        for row in rows[start:]:
            if not row:
                continue
            title = row[0].strip().strip('"')
            if title:
                titles.append(title)

    # De-dupe while keeping order.
    seen: set[str] = set()
    unique: list[str] = []
    for title in titles:
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(title)
    return unique


def extract_titles_from_csv_path(path: Path) -> list[str]:
    """Load titles from a watchlist or title-only CSV/TSV file."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    return extract_titles_from_csv_text(text)


def write_spin_html(
    path: Path,
    titles: list[str],
    *,
    source_label: str = "Default watchlist",
) -> None:
    """Write a self-contained spin wheel page with embedded default titles."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {"source": source_label, "titles": titles},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    path.write_text(_SPIN_TEMPLATE.replace("__DATA_JSON__", data), encoding="utf-8")
    logger.info("Wrote spin wheel (%d titles) to %s", len(titles), path)


_SPIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Watchlist Spin</title>
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
      max-width: 34rem;
      color: var(--ink-soft);
      font-size: 1.05rem;
    }
    .panel {
      display: grid;
      gap: 1.25rem;
      justify-items: center;
    }
    .stage {
      position: relative;
      width: min(100%, 420px);
      aspect-ratio: 1;
    }
    .pointer {
      position: absolute;
      top: -0.15rem;
      left: 50%;
      transform: translateX(-50%);
      z-index: 3;
      width: 0;
      height: 0;
      border-left: 14px solid transparent;
      border-right: 14px solid transparent;
      border-top: 28px solid var(--ticket);
      filter: drop-shadow(0 2px 0 rgba(18,20,26,0.15));
    }
    .wheel {
      width: 100%;
      height: 100%;
      border-radius: 50%;
      border: 8px solid var(--ink);
      box-shadow: 0 18px 50px rgba(18, 20, 26, 0.12);
      transition: transform 4.2s cubic-bezier(0.12, 0.75, 0.12, 1);
      background: conic-gradient(#ddd, #eee);
    }
    .hub {
      position: absolute;
      inset: 50%;
      width: 72px;
      height: 72px;
      margin: -36px 0 0 -36px;
      border-radius: 50%;
      background: var(--ink);
      color: #f4f6fa;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      display: grid;
      place-items: center;
      z-index: 2;
      border: 3px solid #fff;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      justify-content: center;
      width: 100%;
    }
    button, .file-btn {
      appearance: none;
      border: 1px solid var(--ink);
      background: var(--ink);
      color: #f4f6fa;
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.78rem;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      padding: 0.85rem 1.2rem;
      cursor: pointer;
      transition: transform 0.15s ease, background 0.2s ease;
    }
    button.secondary, .file-btn {
      background: transparent;
      color: var(--ink);
    }
    button:hover, .file-btn:hover { transform: translateY(-1px); }
    button:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
    .file-btn { display: inline-grid; place-items: center; }
    .file-btn input { display: none; }
    .meta {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--mute);
      text-align: center;
    }
    .result {
      min-height: 5.5rem;
      width: min(100%, 520px);
      text-align: center;
      padding: 1rem 1.1rem;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.55);
    }
    .result .label {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--ticket);
      margin-bottom: 0.35rem;
    }
    .result .title {
      font-family: "Syne", system-ui, sans-serif;
      font-size: clamp(1.4rem, 4vw, 2rem);
      font-weight: 800;
      letter-spacing: -0.03em;
      line-height: 1.15;
    }
    .result.flash {
      animation: pop 0.45s ease;
      border-color: rgba(200, 30, 44, 0.45);
      background: var(--ticket-soft);
    }
    @keyframes pop {
      from { transform: scale(0.97); opacity: 0.4; }
      to { transform: scale(1); opacity: 1; }
    }
    .hint {
      margin: 0;
      color: var(--mute);
      font-size: 0.95rem;
      text-align: center;
      max-width: 34rem;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>Tonight's<br /><span>Spin</span></h1>
      <div class="nav"><a href="index.html">Back to report</a> · <a href="recommend.html">Taste picks</a></div>
    </div>
    <p class="lede">Spin the wheel for a random title. Defaults to your Letterboxd watchlist. Import any CSV if you want a different pile.</p>

    <div class="panel">
      <div class="stage">
        <div class="pointer" aria-hidden="true"></div>
        <div class="wheel" id="wheel" aria-hidden="true"></div>
        <div class="hub">Spin</div>
      </div>

      <div class="result" id="result">
        <div class="label">Ready</div>
        <div class="title" id="result-title">Hit spin when you are.</div>
      </div>

      <div class="actions">
        <button type="button" id="spin-btn">Spin</button>
        <button type="button" class="secondary" id="reset-btn">Use default list</button>
        <label class="file-btn">
          Import CSV
          <input id="import" type="file" accept=".csv,.tsv,.txt,text/csv" />
        </label>
      </div>

      <div class="meta" id="source-meta"></div>
      <p class="hint">Accepts Letterboxd exports (Name column), streaming CSVs (title column), or a plain list of movie names.</p>
    </div>
  </div>

  <script id="spin-data" type="application/json">__DATA_JSON__</script>
  <script>
    const embedded = JSON.parse(document.getElementById("spin-data").textContent);
    const state = {
      titles: [...embedded.titles],
      source: embedded.source || "Default watchlist",
      rotation: 0,
      spinning: false,
      segments: [],
    };

    const wheel = document.getElementById("wheel");
    const spinBtn = document.getElementById("spin-btn");
    const resetBtn = document.getElementById("reset-btn");
    const importInput = document.getElementById("import");
    const sourceMeta = document.getElementById("source-meta");
    const result = document.getElementById("result");
    const resultTitle = document.getElementById("result-title");

    const COLORS = ["#12141a", "#c81e2c", "#2d3340", "#8b1520", "#3a4250", "#a81a26"];

    function setSource(label, count) {
      sourceMeta.textContent = `${label} · ${count} titles`;
    }

    function pickWinner(list) {
      return list[Math.floor(Math.random() * list.length)];
    }

    function buildSegments(titles, winner) {
      const maxSeg = 12;
      if (titles.length <= maxSeg) {
        return [...titles];
      }
      const others = titles.filter((t) => t !== winner);
      for (let i = others.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [others[i], others[j]] = [others[j], others[i]];
      }
      const sample = others.slice(0, maxSeg - 1);
      sample.splice(Math.floor(Math.random() * (sample.length + 1)), 0, winner);
      return sample;
    }

    function paintWheel(segments) {
      state.segments = segments;
      if (!segments.length) {
        wheel.style.background = "#ccc";
        return;
      }
      const step = 360 / segments.length;
      const stops = segments.map((_, i) => {
        const color = COLORS[i % COLORS.length];
        return `${color} ${i * step}deg ${(i + 1) * step}deg`;
      });
      wheel.style.background = `conic-gradient(from -90deg, ${stops.join(", ")})`;
    }

    function parseCsvText(text) {
      const sample = text.replace(/^\uFEFF/, "");
      if (!sample.trim()) return [];
      const first = sample.split(/\r?\n/)[0] || "";
      const delim = (first.match(/\t/g) || []).length >= (first.match(/,/g) || []).length ? "\t" : ",";
      const rows = [];
      for (const line of sample.split(/\r?\n/)) {
        if (!line.trim()) continue;
        // Lightweight CSV split that respects quotes.
        const cells = [];
        let cur = "";
        let inQ = false;
        for (let i = 0; i < line.length; i++) {
          const ch = line[i];
          if (ch === '"') {
            if (inQ && line[i + 1] === '"') { cur += '"'; i++; }
            else inQ = !inQ;
          } else if (ch === delim && !inQ) {
            cells.push(cur); cur = "";
          } else cur += ch;
        }
        cells.push(cur);
        rows.push(cells.map((c) => c.trim()));
      }
      if (!rows.length) return [];
      const headers = rows[0].map((h) => h.toLowerCase());
      const titleKeys = new Set(["name", "title", "film", "movie", "movie title", "film title"]);
      let idx = headers.findIndex((h) => titleKeys.has(h));
      const titles = [];
      if (idx >= 0) {
        for (const row of rows.slice(1)) {
          const t = (row[idx] || "").trim();
          if (t) titles.push(t);
        }
      } else {
        let start = 0;
        if (titleKeys.has(headers[0]) || ["titles", "movies", "films"].includes(headers[0])) start = 1;
        for (const row of rows.slice(start)) {
          const t = (row[0] || "").trim();
          if (t) titles.push(t);
        }
      }
      const seen = new Set();
      return titles.filter((t) => {
        const key = t.toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }

    function refreshIdleWheel() {
      if (!state.titles.length) {
        paintWheel([]);
        resultTitle.textContent = "No titles loaded.";
        spinBtn.disabled = true;
        setSource(state.source, 0);
        return;
      }
      spinBtn.disabled = false;
      const previewWinner = pickWinner(state.titles);
      paintWheel(buildSegments(state.titles, previewWinner));
      setSource(state.source, state.titles.length);
    }

    function spin() {
      if (state.spinning || !state.titles.length) return;
      state.spinning = true;
      spinBtn.disabled = true;
      result.classList.remove("flash");
      document.querySelector(".result .label").textContent = "Spinning";
      resultTitle.textContent = "…";

      const winner = pickWinner(state.titles);
      const segments = buildSegments(state.titles, winner);
      paintWheel(segments);
      const index = segments.indexOf(winner);
      const step = 360 / segments.length;
      // Pointer is at top (-90deg origin). Land segment center under pointer.
      const segmentCenter = index * step + step / 2;
      const extraTurns = 5 + Math.floor(Math.random() * 3);
      const target = extraTurns * 360 + (360 - segmentCenter);
      state.rotation += target;
      wheel.style.transform = `rotate(${state.rotation}deg)`;

      window.setTimeout(() => {
        document.querySelector(".result .label").textContent = "Tonight";
        resultTitle.textContent = winner;
        result.classList.add("flash");
        state.spinning = false;
        spinBtn.disabled = false;
      }, 4300);
    }

    spinBtn.addEventListener("click", spin);
    resetBtn.addEventListener("click", () => {
      state.titles = [...embedded.titles];
      state.source = embedded.source || "Default watchlist";
      document.querySelector(".result .label").textContent = "Ready";
      resultTitle.textContent = "Back on your default watchlist.";
      refreshIdleWheel();
    });
    importInput.addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const text = await file.text();
      const titles = parseCsvText(text);
      if (!titles.length) {
        document.querySelector(".result .label").textContent = "Import failed";
        resultTitle.textContent = "Could not find movie titles in that file.";
        return;
      }
      state.titles = titles;
      state.source = `Imported · ${file.name}`;
      document.querySelector(".result .label").textContent = "Imported";
      resultTitle.textContent = `${titles.length} titles ready. Spin when you want.`;
      refreshIdleWheel();
      importInput.value = "";
    });

    refreshIdleWheel();
  </script>
</body>
</html>
"""
