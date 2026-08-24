"""Generate a standalone random-movie spin wheel page."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .http_util import HTTPStatusError, HttpClient, TransientHTTPError
from .models import WatchlistFilm

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"

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


def as_spin_films(items: list[Any]) -> list[dict[str, Any]]:
    """Normalize title strings or film dicts into the spin payload shape."""
    films: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            title = item.strip()
            film = {
                "title": title,
                "year": None,
                "runtime": None,
                "genres": [],
                "decade": None,
                "services": [],
                "letterboxd_url": "",
                "tmdb_id": None,
            }
        elif isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            year = item.get("year")
            runtime = item.get("runtime")
            decade = item.get("decade")
            film = {
                "title": title,
                "year": int(year) if isinstance(year, int) else None,
                "runtime": int(runtime) if isinstance(runtime, int) else None,
                "genres": [str(g) for g in (item.get("genres") or []) if g],
                "decade": int(decade) if isinstance(decade, int) else None,
                "services": [str(s) for s in (item.get("services") or []) if s],
                "letterboxd_url": str(item.get("letterboxd_url") or ""),
                "tmdb_id": item.get("tmdb_id"),
            }
        else:
            continue
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        films.append(film)
    return films


def film_matches(
    film: dict[str, Any],
    *,
    runtime_max: Optional[int] = None,
    genres: Optional[list[str]] = None,
    decade: Optional[int] = None,
    services: Optional[list[str]] = None,
    available_only: bool = False,
) -> bool:
    """Return True when a spin film passes the active filters."""
    if runtime_max is not None:
        runtime = film.get("runtime")
        if not isinstance(runtime, int) or runtime > runtime_max:
            return False
    if genres:
        have = {str(name) for name in (film.get("genres") or [])}
        if not have.intersection(genres):
            return False
    if decade is not None and film.get("decade") != decade:
        return False
    have_services = [str(name) for name in (film.get("services") or []) if name]
    if available_only and not have_services:
        return False
    if services and not set(have_services).intersection(services):
        return False
    return True


def _norm_uri(uri: str) -> str:
    return uri.rstrip("/").casefold()


def _service_names(raw: str) -> list[str]:
    names: list[str] = []
    for part in raw.split(";"):
        label = part.strip()
        if not label:
            continue
        names.append(label.split(" (library")[0].strip())
    return names


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _streaming_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return index
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            title = (raw.get("title") or "").strip()
            year_raw = (raw.get("year") or "").strip()
            tmdb_raw = (raw.get("tmdb_id") or "").strip()
            uri = (raw.get("letterboxd_url") or "").strip()
            row = {
                "title": title,
                "year": int(year_raw) if year_raw.isdigit() else None,
                "tmdb_id": int(tmdb_raw) if tmdb_raw.isdigit() else None,
                "services": _service_names(raw.get("on_my_services") or ""),
                "letterboxd_url": uri,
            }
            if uri:
                index[_norm_uri(uri)] = row
            if title:
                year_key = year_raw or "?"
                index[f"title:{title.casefold()}|{year_key}"] = row
    return index


def _meta_from_caches(
    tmdb_id: int,
    *,
    spin_meta: dict[str, Any],
    taste_cache: dict[str, Any],
) -> dict[str, Any]:
    entry = spin_meta.get(str(tmdb_id)) or spin_meta.get(tmdb_id)
    if not isinstance(entry, dict):
        taste = taste_cache.get(f"tmdb:{tmdb_id}")
        if isinstance(taste, dict):
            entry = taste
    if not isinstance(entry, dict):
        return {}
    runtime = entry.get("runtime")
    year = entry.get("year")
    decade = entry.get("decade")
    if decade is None and isinstance(year, int):
        decade = (year // 10) * 10
    return {
        "runtime": int(runtime) if isinstance(runtime, int) and runtime > 0 else None,
        "genres": [str(g) for g in (entry.get("genres") or []) if g],
        "decade": int(decade) if isinstance(decade, int) else None,
        "year": int(year) if isinstance(year, int) else None,
    }


def _fetch_tmdb_spin_meta(
    tmdb_id: int,
    http: HttpClient,
    api_key: str,
) -> dict[str, Any]:
    detail = http.get_json(f"{TMDB_BASE}/movie/{tmdb_id}", params={"api_key": api_key})
    year = None
    release = (detail.get("release_date") or "").strip()
    if len(release) >= 4 and release[:4].isdigit():
        year = int(release[:4])
    runtime = detail.get("runtime")
    runtime_i = int(runtime) if isinstance(runtime, int) and runtime > 0 else None
    genres = [str(g.get("name")) for g in (detail.get("genres") or []) if g.get("name")]
    decade = (year // 10) * 10 if year is not None else None
    return {
        "runtime": runtime_i,
        "genres": genres,
        "decade": decade,
        "year": year,
    }


def build_spin_films(
    watchlist: list[WatchlistFilm],
    *,
    streaming_csv: Optional[Path] = None,
    id_cache_path: Optional[Path] = None,
    meta_path: Optional[Path] = None,
    taste_cache_path: Optional[Path] = None,
    tmdb_api_key: Optional[str] = None,
    http: Optional[HttpClient] = None,
) -> list[dict[str, Any]]:
    """Join watchlist rows with availability and TMDB runtime/genre metadata."""
    streaming = _streaming_index(streaming_csv) if streaming_csv else {}
    id_cache = _load_json_dict(id_cache_path) if id_cache_path else {}
    spin_meta = _load_json_dict(meta_path) if meta_path else {}
    taste_cache = _load_json_dict(taste_cache_path) if taste_cache_path else {}
    dirty = False
    films: list[dict[str, Any]] = []

    for item in watchlist:
        uri = item.letterboxd_uri.rstrip("/")
        stream = streaming.get(_norm_uri(uri))
        if stream is None and item.name:
            year_key = str(item.year) if item.year is not None else "?"
            stream = streaming.get(f"title:{item.name.casefold()}|{year_key}")
        stream = stream or {}
        tmdb_id = stream.get("tmdb_id")
        if tmdb_id is None:
            cached = id_cache.get(uri) or id_cache.get(item.letterboxd_uri)
            if isinstance(cached, dict) and cached.get("tmdb_id"):
                tmdb_id = int(cached["tmdb_id"])
        meta: dict[str, Any] = {}
        if isinstance(tmdb_id, int):
            meta = _meta_from_caches(tmdb_id, spin_meta=spin_meta, taste_cache=taste_cache)
            if not meta.get("genres") and tmdb_api_key and http is not None:
                try:
                    meta = _fetch_tmdb_spin_meta(tmdb_id, http, tmdb_api_key)
                    spin_meta[str(tmdb_id)] = meta
                    dirty = True
                except (TransientHTTPError, HTTPStatusError) as exc:
                    logger.warning("Spin meta fetch failed for %s (%s): %s", item.name, tmdb_id, exc)
        year = item.year if item.year is not None else meta.get("year") or stream.get("year")
        decade = meta.get("decade")
        if decade is None and isinstance(year, int):
            decade = (year // 10) * 10
        films.append(
            {
                "title": item.name,
                "year": year,
                "runtime": meta.get("runtime"),
                "genres": list(meta.get("genres") or []),
                "decade": decade,
                "services": list(stream.get("services") or []),
                "letterboxd_url": uri,
                "tmdb_id": tmdb_id,
            }
        )

    if dirty and meta_path is not None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(spin_meta, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info("Wrote spin metadata cache to %s", meta_path)
    return films


def write_spin_html(
    path: Path,
    titles: list[Any],
    *,
    source_label: str = "Default watchlist",
) -> None:
    """Write a self-contained spin wheel page with embedded films and filters."""
    films = as_spin_films(titles)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {
            "source": source_label,
            "titles": [film["title"] for film in films],
            "films": films,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    path.write_text(_SPIN_TEMPLATE.replace("__DATA_JSON__", data), encoding="utf-8")
    logger.info("Wrote spin wheel (%d titles) to %s", len(films), path)


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
      margin: 0 0 1.25rem;
      max-width: 34rem;
      color: var(--ink-soft);
      font-size: 1.05rem;
    }
    .filters {
      width: 100%;
      display: grid;
      gap: 0.85rem;
      margin: 0 0 1.5rem;
      padding: 1rem 1.05rem 1.1rem;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.5);
    }
    .filter-row { display: grid; gap: 0.45rem; }
    .filter-label {
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--mute);
    }
    .chips { display: flex; flex-wrap: wrap; gap: 0.45rem; }
    .chip {
      appearance: none;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.55);
      color: var(--ink-soft);
      font-family: "Syne", system-ui, sans-serif;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      padding: 0.45rem 0.7rem;
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
    .result .sub {
      margin-top: 0.4rem;
      color: var(--ink-soft);
      font-size: 0.95rem;
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
    <p class="lede">Spin a random watchlist title. Narrow by length, genre, decade, or what’s on your services tonight.</p>

    <div class="filters" id="filters">
      <div class="filter-row">
        <div class="filter-label">Length</div>
        <div class="chips" id="runtime-chips"></div>
      </div>
      <div class="filter-row">
        <div class="filter-label">Genre</div>
        <div class="chips" id="genre-chips"></div>
      </div>
      <div class="filter-row">
        <div class="filter-label">Decade</div>
        <div class="chips" id="decade-chips"></div>
      </div>
      <div class="filter-row">
        <div class="filter-label">Available now</div>
        <div class="chips" id="service-chips"></div>
      </div>
    </div>

    <div class="panel">
      <div class="stage">
        <div class="pointer" aria-hidden="true"></div>
        <div class="wheel" id="wheel" aria-hidden="true"></div>
        <div class="hub">Spin</div>
      </div>

      <div class="result" id="result">
        <div class="label">Ready</div>
        <div class="title" id="result-title">Hit spin when you are.</div>
        <div class="sub" id="result-sub"></div>
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

    function asFilm(item) {
      if (typeof item === "string") {
        return { title: item, year: null, runtime: null, genres: [], decade: null, services: [], letterboxd_url: "" };
      }
      return {
        title: item.title || "",
        year: item.year ?? null,
        runtime: item.runtime ?? null,
        genres: item.genres || [],
        decade: item.decade ?? null,
        services: item.services || [],
        letterboxd_url: item.letterboxd_url || "",
      };
    }

    function defaultFilms() {
      const raw = (embedded.films && embedded.films.length) ? embedded.films : (embedded.titles || []);
      return raw.map(asFilm).filter((f) => f.title);
    }

    const state = {
      allFilms: defaultFilms(),
      films: [],
      source: embedded.source || "Default watchlist",
      rotation: 0,
      spinning: false,
      runtimeMax: null,
      genres: new Set(),
      decade: null,
      availableOnly: false,
      services: new Set(),
    };

    const wheel = document.getElementById("wheel");
    const spinBtn = document.getElementById("spin-btn");
    const resetBtn = document.getElementById("reset-btn");
    const importInput = document.getElementById("import");
    const sourceMeta = document.getElementById("source-meta");
    const result = document.getElementById("result");
    const resultTitle = document.getElementById("result-title");
    const resultSub = document.getElementById("result-sub");

    const COLORS = ["#12141a", "#c81e2c", "#2d3340", "#8b1520", "#3a4250", "#a81a26"];
    const RUNTIME_OPTS = [
      { label: "Any", value: null },
      { label: "≤ 90m", value: 90 },
      { label: "≤ 2 hrs", value: 120 },
      { label: "≤ 2.5 hrs", value: 150 },
    ];

    function filmMatches(film) {
      if (state.runtimeMax != null) {
        if (!(typeof film.runtime === "number") || film.runtime > state.runtimeMax) return false;
      }
      if (state.genres.size) {
        if (![...state.genres].some((g) => film.genres.includes(g))) return false;
      }
      if (state.decade != null && film.decade !== state.decade) return false;
      if (state.availableOnly && !film.services.length) return false;
      if (state.services.size) {
        if (![...state.services].some((s) => film.services.includes(s))) return false;
      }
      return true;
    }

    function applyFilters() {
      state.films = state.allFilms.filter(filmMatches);
    }

    function uniqueSorted(values) {
      return [...new Set(values.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)));
    }

    function setSource(label, shown, total) {
      sourceMeta.textContent = shown === total
        ? `${label} · ${shown} titles`
        : `${label} · ${shown} of ${total} titles`;
    }

    function pickWinner(list) {
      return list[Math.floor(Math.random() * list.length)];
    }

    function buildSegments(films, winner) {
      const titles = films.map((f) => f.title);
      const maxSeg = 12;
      if (titles.length <= maxSeg) return [...titles];
      const others = titles.filter((t) => t !== winner.title);
      for (let i = others.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [others[i], others[j]] = [others[j], others[i]];
      }
      const sample = others.slice(0, maxSeg - 1);
      sample.splice(Math.floor(Math.random() * (sample.length + 1)), 0, winner.title);
      return sample;
    }

    function paintWheel(segments) {
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

    function filmSub(film) {
      const bits = [];
      if (film.year) bits.push(String(film.year));
      if (typeof film.runtime === "number") bits.push(`${film.runtime}m`);
      if (film.genres.length) bits.push(film.genres.slice(0, 3).join(", "));
      if (film.services.length) bits.push(film.services.join(" · "));
      return bits.join(" · ");
    }

    function parseCsvText(text) {
      const sample = text.replace(/^\uFEFF/, "");
      if (!sample.trim()) return [];
      const first = sample.split(/\r?\n/)[0] || "";
      const delim = (first.match(/\t/g) || []).length >= (first.match(/,/g) || []).length ? "\t" : ",";
      const rows = [];
      for (const line of sample.split(/\r?\n/)) {
        if (!line.trim()) continue;
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
      }).map(asFilm);
    }

    function makeChip(label, pressed, onClick, ticket) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = ticket ? "chip ticket" : "chip";
      btn.textContent = label;
      btn.setAttribute("aria-pressed", pressed ? "true" : "false");
      btn.addEventListener("click", onClick);
      return btn;
    }

    function renderFilterChips() {
      const runtimeHost = document.getElementById("runtime-chips");
      runtimeHost.replaceChildren(...RUNTIME_OPTS.map((opt) =>
        makeChip(opt.label, state.runtimeMax === opt.value, () => {
          state.runtimeMax = opt.value;
          refreshIdleWheel();
        })
      ));

      const genres = uniqueSorted(state.allFilms.flatMap((f) => f.genres));
      const genreHost = document.getElementById("genre-chips");
      if (!genres.length) {
        genreHost.replaceChildren();
        genreHost.parentElement.hidden = true;
      } else {
        genreHost.parentElement.hidden = false;
        genreHost.replaceChildren(...genres.map((name) =>
          makeChip(name, state.genres.has(name), () => {
            if (state.genres.has(name)) state.genres.delete(name);
            else state.genres.add(name);
            refreshIdleWheel();
          })
        ));
      }

      const decades = uniqueSorted(state.allFilms.map((f) => f.decade)).sort((a, b) => b - a);
      const decadeHost = document.getElementById("decade-chips");
      if (!decades.length) {
        decadeHost.replaceChildren();
        decadeHost.parentElement.hidden = true;
      } else {
        decadeHost.parentElement.hidden = false;
        const chips = [makeChip("Any", state.decade == null, () => {
          state.decade = null;
          refreshIdleWheel();
        })];
        decades.forEach((year) => {
          chips.push(makeChip(`${year}s`, state.decade === year, () => {
            state.decade = state.decade === year ? null : year;
            refreshIdleWheel();
          }));
        });
        decadeHost.replaceChildren(...chips);
      }

      const services = uniqueSorted(state.allFilms.flatMap((f) => f.services));
      const serviceHost = document.getElementById("service-chips");
      const serviceChips = [
        makeChip("Any service", !state.availableOnly && state.services.size === 0, () => {
          state.availableOnly = false;
          state.services.clear();
          refreshIdleWheel();
        }),
        makeChip("On my services", state.availableOnly && state.services.size === 0, () => {
          state.availableOnly = true;
          state.services.clear();
          refreshIdleWheel();
        }, true),
      ];
      services.forEach((name) => {
        serviceChips.push(makeChip(name, state.services.has(name), () => {
          if (state.services.has(name)) state.services.delete(name);
          else state.services.add(name);
          state.availableOnly = state.services.size > 0;
          refreshIdleWheel();
        }));
      });
      serviceHost.replaceChildren(...serviceChips);
    }

    function refreshIdleWheel() {
      applyFilters();
      renderFilterChips();
      resultSub.textContent = "";
      if (!state.films.length) {
        paintWheel([]);
        resultTitle.textContent = state.allFilms.length
          ? "No titles match those filters."
          : "No titles loaded.";
        spinBtn.disabled = true;
        setSource(state.source, 0, state.allFilms.length);
        return;
      }
      spinBtn.disabled = false;
      const previewWinner = pickWinner(state.films);
      paintWheel(buildSegments(state.films, previewWinner));
      setSource(state.source, state.films.length, state.allFilms.length);
    }

    function spin() {
      if (state.spinning || !state.films.length) return;
      state.spinning = true;
      spinBtn.disabled = true;
      result.classList.remove("flash");
      document.querySelector(".result .label").textContent = "Spinning";
      resultTitle.textContent = "…";
      resultSub.textContent = "";

      const winner = pickWinner(state.films);
      const segments = buildSegments(state.films, winner);
      paintWheel(segments);
      const index = segments.indexOf(winner.title);
      const step = 360 / segments.length;
      const segmentCenter = index * step + step / 2;
      const extraTurns = 5 + Math.floor(Math.random() * 3);
      const target = extraTurns * 360 + (360 - segmentCenter);
      state.rotation += target;
      wheel.style.transform = `rotate(${state.rotation}deg)`;

      window.setTimeout(() => {
        document.querySelector(".result .label").textContent = "Tonight";
        resultTitle.textContent = winner.title;
        resultSub.textContent = filmSub(winner);
        result.classList.add("flash");
        state.spinning = false;
        spinBtn.disabled = false;
      }, 4300);
    }

    spinBtn.addEventListener("click", spin);
    resetBtn.addEventListener("click", () => {
      state.allFilms = defaultFilms();
      state.source = embedded.source || "Default watchlist";
      state.runtimeMax = null;
      state.genres.clear();
      state.decade = null;
      state.availableOnly = false;
      state.services.clear();
      document.querySelector(".result .label").textContent = "Ready";
      resultTitle.textContent = "Back on your default watchlist.";
      refreshIdleWheel();
    });
    importInput.addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const films = parseCsvText(await file.text());
      if (!films.length) {
        document.querySelector(".result .label").textContent = "Import failed";
        resultTitle.textContent = "Could not find movie titles in that file.";
        return;
      }
      state.allFilms = films;
      state.source = `Imported · ${file.name}`;
      document.querySelector(".result .label").textContent = "Imported";
      resultTitle.textContent = `${films.length} titles ready. Spin when you want.`;
      refreshIdleWheel();
      importInput.value = "";
    });

    refreshIdleWheel();
  </script>
</body>
</html>
"""
