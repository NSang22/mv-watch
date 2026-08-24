# Watchlist Watcher

Cross-reference a Letterboxd watchlist with streaming availability (via TMDB),
write a daily report, and push a short notification when a film you want
arrives on a service you already pay for.

No aggregator is authoritative. Free ad-supported tiers are the least reliable.
The tool favors high recall over precision and surfaces confidence
(confirmed / probable / disputed) instead of silently dropping weak hits.

Letterboxd Pro surfaces the same underlying JustWatch data behind a paywall.
This tool keeps that data in files you own, plus a daily delta so arrivals
find you instead of the other way around.

## Website

Live site: **https://nsang22.github.io/mv-watch/**

GitHub Pages serves `index.html` (same cinema viewer as `report.html`), plus
`spin.html` and `recommend.html`. The daily Action:

- syncs watched titles off the site via your public Letterboxd **diary RSS**
  (HTML watchlist scraping is blocked by Letterboxd; RSS still works)
- refreshes TMDB availability
- rebuilds Tonight's Spin with length / genre / decade / service filters
- redeploys Pages

For a full watchlist reset (manual removals, not just watches), drop a fresh
Letterboxd export as `watchlist.csv` and push. Taste picks still update when
you run `recommend` locally and push.

The published site is public. Keep the Letterboxd profile/diary public for
automatic sync.

## Quick start

1. Get a free TMDB API key: [https://www.themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
2. Export your Letterboxd watchlist: Settings > Data > Export Your Data. Put
   `watchlist.csv` (or the whole zip) in this directory.
3. Copy config and install:

```bash
cp config.example.yaml config.yaml
python -m pip install -r requirements.txt
export TMDB_API_KEY=your_key_here
# optional:
export NTFY_TOPIC=your-private-topic
export STREAMING_AVAILABILITY_API_KEY=your_motn_key
```

4. Run:

```bash
python -m watchlist_watcher
```

Outputs:

- `watchlist_streaming.csv`
- `report.md`
- `report.html` interactive cinema-style viewer (open in a browser)
- `spin.html` random-title spin wheel (defaults to your watchlist; can import other CSVs)
- `recommend.html` taste-ranked picks (written by the `recommend` command)
- `conflicts.csv` (appended) when TMDB and MotN disagree on a scored service
- `feedback.csv` (written from the HTML "Wrong" button; never applied automatically)
- `unresolved.csv` for failed/skipped provider lookups this run
- updated `state.json` (committed so CI diffs survive across runs)
- `unmatched.csv` for films that failed ID resolution
- optional ntfy push when something changed

Rebuild the HTML viewer and spin wheel from local CSV files without calling TMDB:

```bash
python -m watchlist_watcher --render-html
```

## Recommend (Mode 1: rank the watchlist)

Ranks films that are already on your services by how well they fit *your*
taste better than the crowd's, using a residual model:

`residual = my Letterboxd rating - (TMDB vote_average / 2)`

Weights when building the training signal: **rewatches > likes > ratings**.
Unrated watched diary rows are ignored. Favorites and likes without stars get
implied ratings (5.0 / 4.5) so they still count.

1. Drop a full Letterboxd data export zip (or unpacked folder with
   `ratings.csv`, `diary.csv`, `likes/films.csv`, `profile.csv`) next to the
   project, or pass `--export`.
2. Make sure `watchlist_streaming.csv` exists (run the main watcher first).
3. Rank:

```bash
python -m watchlist_watcher recommend --export letterboxd-export.zip
python -m watchlist_watcher recommend --runtime-max 120 --mood dark --decade 1970s
python -m watchlist_watcher recommend --unwatched-director
```

Validation is non-negotiable: 20% holdout, Pearson correlation of predicted vs
actual residual, and MAE against the baseline of always predicting your mean
residual. If the model does not beat that baseline, recommendations are refused
and the feature report is printed anyway so you can see whether it only
recovered "likes prestige drama."

When validation passes, you get the top 10 with one-line explanations plus one
deliberate wildcard outside your usual genre cluster. The same list is written to
`recommend.html` (linked from `report.html` as **Taste picks**).

Mode 2 (suggest films not on your watchlist) is not built yet.

## How it works

| Module | Role |
| --- | --- |
| `watchlist.py` | Read export CSV/zip, or scrape a public watchlist |
| `resolve.py` | Letterboxd URI to TMDB ID (cache + overrides + two tiers) |
| `providers.py` | TMDB watch providers; alias folding; bucket rules |
| `enrich.py` | Optional Movie of the Night expiry enrichment |
| `diff.py` | Arrivals, departures, leaving-soon, new-to-watchlist, cold start |
| `notify.py` | CSV + markdown report + ntfy |
| `main.py` | CLI wiring |

### Film ID resolution

Letterboxd exports do not include TMDB IDs. Resolution is permanent-cached by
Letterboxd URI:

1. `overrides.json` wins when present (`{"https://letterboxd.com/film/...": 123}`).
2. TMDB `/search/movie` with title and year. A hit is accepted only when the
   release year is within one year of the Letterboxd year.
3. If search fails, fetch the Letterboxd film page and scrape the
   `themoviedb.org/movie/{id}` link. That mapping is treated as authoritative.

Failures land in `unmatched.csv` for hand fixes via `overrides.json`.

### Availability buckets

TMDB returns `flatrate`, `free`, `ads`, `rent`, and `buy`. This tool treats
`flatrate` + `free` + `ads` as "I can watch this now." Rent and buy are
recorded separately and never counted as subscription arrivals.

That matters for Tubi and free YouTube, which land in `ads`, not `flatrate`.

### Your services and aliases

Services live in `config.yaml` as a canonical name plus match patterns.
Matches fold back to the canonical name in every output.

Confirmed TMDB US provider strings (from
`GET /watch/providers/movie?watch_region=US` at build time) are baked into
`config.example.yaml`. Important rules:

- `Netflix` also matches `Netflix basic with Ads` (and `Netflix Standard with Ads` if TMDB ever uses that label).
- `Amazon Prime Video` also matches `Amazon Prime Video with Ads`.
- **`Amazon Video` is not Prime.** It is the rent/buy storefront and is explicitly excluded.
- `YouTube Free` is the free-with-ads catalog. Plain `YouTube` is typically rent/buy.
- `Hoopla` is labeled **library, limited** in reports. A Hoopla-only hit never suppresses rent/buy context.

Dump the live region list any time:

```bash
python -m watchlist_watcher --list-providers
```

On startup the tool warns loudly when a configured service matches zero
providers in your region, so a typo does not fail silently.

### Diff events

| Event | Meaning | Push? |
| --- | --- | --- |
| Arrival | Film is now on a my-service provider it was not on last run | Yes |
| Departure (postmortem) | Film left one or more my-services since last verified run (one line per film) | Yes |
| Leaving soon | MotN expiry crosses a threshold (default 14d / 3d) | Yes |
| New to watchlist | Newly added film; availability is reported only | No arrival push |
| First run (cold start) | Writes state + summary notification | Summary only |

Departures that wipe every prior service on a title are flagged **SUSPECT**. If a run
would depart more than `max_departure_films` (default 10) or more than
`max_departure_fraction` (default 5%) of the watchlist, the job aborts before
writing state or sending notifications.

### Advance warning and confidence tiers (optional)

No aggregator is authoritative. Free ad-supported catalogs are the least
reliable. This tool targets **high recall over precision**: every my-service
hit stays visible.

Confidence is derived from measured accuracy, not MotN agreement:

- **confirmed**: Netflix and Amazon Prime Video. TMDB scored 36/36 on a blind
  audit of these two providers.
- **probable**: Tubi, YouTube Free, Hoopla (and any other unaudited catalog).
  Re-running `tools/audit.py` on those providers is what would promote them.

The `disputed` tier is gone. MotN disagreements on Prime were wrong (mostly
Amazon rent/buy/`addon` options treated as included-with-Prime), so MotN
disagreement carried negative information.

Optional MotN enrichment uses the
[Streaming Availability API](https://www.movieofthenight.com/about/api/) for
**expiry dates only**:

- Gate: `streaming_availability.enabled: true` **and** `STREAMING_AVAILABILITY_API_KEY`.
- Only films TMDB places on your services are queried.
- MotN availability override is **off**. MotN never adds or drops a my-service hit.
- MotN option `type` used for expiry/links: `subscription` and `free` only
  (never `rent` / `buy` / `addon`).
- MotN deep links appear when MotN returns a subscription/free option.
- The HTML report has a **Wrong** button per row that appends feedback.csv.
- If MotN is missing or down, TMDB hits stay; expiry is unknown.

Do not re-enable MotN availability override without filtering option types as
above and re-running the audit.

## Configuration

Environment variables only for secrets (a gitignored `.env` file is also loaded):

| Variable | Required | Purpose |
| --- | --- | --- |
| `TMDB_API_KEY` | Yes | TMDB v3 API key |
| `NTFY_TOPIC` | No | ntfy.sh topic (or full URL) for pushes |
| `LETTERBOXD_USER` | No | Public username for scrape fallback |
| `STREAMING_AVAILABILITY_API_KEY` or `MOTN_API_KEY` | No | MotN key for verification + expiry |

Everything else is in `config.yaml` (see `config.example.yaml`).

## CLI

```bash
python -m watchlist_watcher --help
python -m watchlist_watcher --watchlist path/to/export.zip
python -m watchlist_watcher --scrape
python -m watchlist_watcher --list-providers
python -m watchlist_watcher --dry-run
```

## GitHub Actions

Workflow: `.github/workflows/daily.yml`

- Daily cron + `workflow_dispatch` (full TMDB refresh)
- Push to `main` redeploys the current HTML without calling TMDB
- Runs against committed `watchlist.csv`
- Commits updated `state.json`, CSV, report, and ID cache back to the repo
- Deploys `index.html`, `spin.html`, and `recommend.html` to GitHub Pages
- Transient TMDB errors are retried with backoff; the job exits nonzero only
  when the failure rate exceeds `failure_rate_threshold` (default 20%)

Repo secrets to set:

- `TMDB_API_KEY` (required)
- `LETTERBOXD_USER` (recommended; public username so CI refreshes the watchlist)
- `NTFY_TOPIC` (optional)
- `STREAMING_AVAILABILITY_API_KEY` (optional)

## Tests

```bash
python -m pytest
```

Tests use recorded fixture JSON only. No live network calls.

## Attribution

Streaming availability data is provided by **JustWatch** via
[The Movie Database (TMDB)](https://www.themoviedb.org/) watch-provider
endpoints. This project is not endorsed or certified by TMDB or JustWatch.

When expiry enrichment is enabled, expiry dates are provided by
**Movie of the Night**
([Streaming Availability API](https://www.movieofthenight.com/about/api/)).

## Design choices

- **High recall over precision.** Prefer a false positive you can dismiss over a
  missed watch-now title. Confidence tiers make the uncertainty visible.
- No Letterboxd official API (`api.letterboxd.com`): access requires approval.
- No JustWatch GraphQL or unofficial wrappers: they break and violate expectations.
- No automatic override policy from the Wrong button: collect `feedback.csv` first.
- Daily cadence: TMDB's JustWatch feed lags at least ~24 hours, plus caching.
- State is committed: diffs remain meaningful across ephemeral CI runners.
- Scrape mode is a fallback only and is labeled fragile on purpose.

## Known limitations

- **No source is ground truth:** TMDB/JustWatch and MotN both lag and disagree.
  Free ad-supported catalogs (Tubi, YouTube Free, library tiers) are the least reliable.
- **Scrape mode is fragile:** Letterboxd HTML changes will break it. Prefer CSV export.
- **Expiry coverage is incomplete:** Netflix and Prime often publish dates; Tubi,
  YouTube Free, and Hoopla often do not. Unknown means unknown.
- **Wrong feedback is manual:** the HTML button downloads/appends `feedback.csv`;
  the watcher does not auto-apply those marks.
- **Letterboxd Pro already does the read-only version of this** for about $20/year,
  with a nicer UI inside Letterboxd. This tool exists for local files, diffs,
  and push alerts you control.

## License

Use freely for personal watchlist automation.
