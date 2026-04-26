# Phase 3 scoping — baselines → first forecast row → prod

Sequenced breakdown of Phase 3 against the operator's "~1 month to in
prod" timeline. Sized for one model on one gauge (Faukstad, gauge id
12, the six Sjoa sections downstream of it) with the eventual
all-NVE-gauges-scope deferred. Read top-to-bottom to follow the
proposal; jump to the Decisions (final) block for what subsequent PR
prompts reference.

Source material:

- `ROADMAP.md` Phase 3 — `:124-151`.
- `docs/scoping-genesis.md` Decisions (final) — `:27-72`; baselines
  survey §5 — `:425-541`.
- `docs/forcing-requirements.md` Decisions (final) — `:28-86`;
  five-variable floor §3 — `:88-117`.
- `docs/queries.md` — query-layer reference.
- nokken-web migrations 003 (`forecasts`), 007 (`basins`), 008
  (`weather_*` rekey).
- nokken-data `MIGRATION_PLAN.md`, `docs/operations/historical-backfill.md`.

---

## Decisions (final)

Compact reference for downstream PR prompts. Each bullet closes one
§5 question; longer prose with rationale lives in §5.

- **In-prod definition.** "In prod" = daily rows in `forecasts` for
  Faukstad written by a scheduled forecast job. nokken-web UI
  rendering of those rows is OUT of this 1-month sprint; the rows
  are accessible via SQL or the existing nokken-web read endpoints.
- **Prod milestone.** PR 1 (persistence baseline) + PR 2 (scheduled
  forecast job + operator runbook) = "in prod." Better baselines
  (PRs 3–5) are quality improvements landing *after* the milestone.
- **Hindcasts: DB rows, not parquet.** Hindcast rows land in the
  same `forecasts` table as live forecasts, distinguished by a
  wall-clock `model_run_at` column (live: `model_run_at ≈
  issue_time`; hindcast: `model_run_at ≫ issue_time`). Joinable
  with `observations` for skill scoring; readable by any downstream
  tool without a separate format.
- **Schema gap → PR 0 in nokken-web (BLOCKER for PR 1).** Migration
  003 has `model_version` as the model identifier
  (`003_forecasts.sql:39`); it does NOT carry a wall-clock execution
  timestamp. A new `model_run_at TIMESTAMP NOT NULL` column on
  `forecasts` lands in nokken-web before PR 1. See §3.2 and §4.0.
- **Model-identifier convention.** `model_version TEXT` (existing
  column) carries `<scheme>_v<N>` — `persistence_v1`, `recession_v1`,
  `linear_v1`, `lgb_v1`. Bump the integer when the trained artifact
  changes; the scheme prefix stays stable.
- **Multi-gauge scope.** Faukstad only (gauge id 12, six Sjoa
  sections) for *all* baselines this sprint. Multi-gauge fan-out is
  post-Phase-3; the harness signature `evaluate(model, catchments,
  window)` already takes a list, so expansion is a configuration
  change, not a refactor.
- **Hindcast train/test split.** Train 2012-09-01 → 2019-12-31
  (matches MET Nordic v4 floor); test 2020-01-01 → 2024-12-31. Per
  `forcing-requirements.md` and `scoping-genesis.md:48-53`. Operator
  extends MET v4 backfill to cover the full window in parallel with
  PR 1 — ~21 h wall-clock at 1.5 s/hour for the missing ~50,000 hours.
- **Forecast horizon.** 7 days, hourly. Lead 0–58 h: MET Nordic
  Forecast 1 km (gridded). Lead 58 h–168 h: MET locationforecast
  (point). Per-variable nuance in `forcing-requirements.md` §4.2.
  Lead-time-to-source mapping is a PR 4 concern (first
  forcing-input baseline); persistence and recession don't consume
  forecast forcings.
- **Comparison report.** PR 6 reads hindcast rows out of `forecasts`
  via the existing query layer + a new `get_forecasts` reader,
  joins to observations, computes per-baseline KGE / MAE /
  pinball@0.9 per lead-time, renders to
  `docs/phase3-baselines-comparison.md`.
- **Operator runbook for the prod job.** Folds into PR 2
  (`docs/deploy.md`); not a separate later PR.
- **Live DB inventory.** Local Postgres unreachable at scoping
  time; §1.2 lists the exact inspect-CLI queries the operator runs
  against `nessie` and folds the numbers in pre-merge.

---

## 1. Inventory — what's in the DB right now

### 1.1 Tables modelling code reads

| Table | Migration | Key | Hypertable axis | Phase-3 role |
|---|---|---|---|---|
| `observations` | nokken-web 002 (`002_timeseries_tables.sql:23-32`) | `(time, gauge_id, value_type)` | `time` | flow / level training target |
| `weather_observations` | nokken-web 008 (`008_weather_rekey_to_gauge.sql:59-72`) | `(time, gauge_id, variable, source)` | `time` | five-variable historical forcing |
| `weather_forecasts` | nokken-web 008 (`008_weather_rekey_to_gauge.sql:113-126`) | partial-unique on quantile (`:139-146`) | `valid_time` | live NWP forcing |
| `gauges` | nokken-web 001 (`001_reference_tables.sql:39-49`) | `gauge_id` | — | static metadata |
| `sections` | nokken-web 001 (`001_reference_tables.sql:80-103`) | `section_id` | — | section→gauge linkage and `flowMin`/`flowMax` thresholds |
| `basins` / `basins_current` | nokken-web 007 (`007_basins.sql:42-127`) | `(gauge_id, version)` | — | basin polygon for forcing aggregation; `basin_version` is the audit pin written into weather rows |
| `forecasts` | nokken-web 003 (`003_forecasts.sql:32-65`) | partial-unique on quantile (`:49-54`) | `valid_time` | the Phase-3 sink — exists but missing `model_run_at` column (§3.2) |

### 1.2 Production data state (Faukstad, gauge id 12)

Per the operator's status as of 2026-04-26 and
`nokken-data/docs/operations/historical-backfill.md:10-16`:

- **`observations`** — recovery complete for all 65 active NVE
  gauges. Faukstad is in that set; flow history extends back to
  2003-01-31 (verified against `nessie` in PR #4 and re-cited at
  `scoping-genesis.md:52`). 10-minute live ingestion runs against
  `nessie` via the Phase-1 NVE flow scraper.
- **`weather_observations`** — 18-month backfill window
  2024-04-01 → 2025-10-01 currently in flight at ~1.5 s/hour
  (`historical-backfill.md:11-12`); expected complete tonight. Five
  variables per UTC hour (`forcing-requirements.md:82-86`),
  `source = 'met_nordic_analysis_v4'`, `basin_version = 7` (the
  current Faukstad polygon, 405.4 km²,
  `historical-backfill.md:88-94`).
- **`weather_forecasts`** — locationforecast 2.0 + MET Nordic
  Forecast 1 km fetchers are merged in nokken-data and slated to
  run live. Population state on `nessie` requires an inspect-CLI
  query when next reachable.

The Faukstad weather backfill **does not yet cover the agreed
hindcast window** of 2012-09-01 → 2024-12-31 (Decisions block,
`scoping-genesis.md:48-53`). Extending the backfill to MET Nordic
v4's floor is operator work in `nokken-data` and runs in parallel —
~21 h wall-clock at 1.5 s/hour for the ~50,000 missing hours.

### 1.3 Pre-merge inventory queries the operator runs against `nessie`

CC could not reach a live Postgres at scoping time
(local instance not running; production behind operator-only
network). The exact numbers below get folded into this section
before PR 0 / PR 1 merge. Run via `nokken-forecasting inspect query
"<SQL>"` (or `psql` directly) against `nessie`:

```sql
-- Faukstad observations span and density
SELECT value_type, MIN(time) AS min_t, MAX(time) AS max_t, COUNT(*) AS n
FROM observations
WHERE gauge_id = 12
GROUP BY value_type;

-- Faukstad weather observations span per variable per source
SELECT variable, source, MIN(time) AS min_t, MAX(time) AS max_t,
       COUNT(*) AS n, MIN(basin_version) AS min_v, MAX(basin_version) AS max_v
FROM weather_observations
WHERE gauge_id = 12
GROUP BY variable, source;

-- Faukstad forecast forcings — live cycle population
SELECT variable, source,
       COUNT(DISTINCT issue_time) AS cycles,
       MIN(issue_time) AS first_cycle, MAX(issue_time) AS last_cycle,
       COUNT(*) AS n
FROM weather_forecasts
WHERE gauge_id = 12
GROUP BY variable, source;

-- forecasts table (should be empty pre-PR-1)
SELECT COUNT(*) FROM forecasts;
```

The first three numbers anchor §2's hindcast-window feasibility
calls and the §4 PR-cadence assumptions. The fourth confirms the
sink starts empty.

### 1.4 Query layer in `src/nokken_forecasting/queries/`

Phase 3b landed six readers (`docs/queries.md`,
`src/nokken_forecasting/queries/__init__.py:46-64`). All take an
injected `asyncpg.Connection`, accept tz-aware UTC `pandas.Timestamp`
inputs, return tz-aware-UTC DataFrames over half-open `[start, end)`
ranges:

| Reader | File:line | Returns |
|---|---|---|
| `get_gauges` | `queries/gauges.py:46-72` | one row per gauge; `gauge_id`, name, `has_flow`, `has_level`, `source`, `sourcing_key`, `drainage_basin` |
| `get_sections` | `queries/sections.py:64-98` | section row including `gauge_id`, `gauge_sub`, `flowMin`/`flowMax` |
| `get_observations` | `queries/observations.py:38-73` | `(time, gauge_id, value_type, value)`; filters by `gauge_id` (required), time range, optional `value_type` |
| `get_weather_observations` | `queries/weather.py:76-116` | `(time, gauge_id, variable, value, source, basin_version)`; same time filters plus optional `variables`, `source` |
| `get_weather_forecast_latest_as_of` | `queries/weather.py:119-181` | latest forecast cycle per source as of a given time; emits `(issue_time, valid_time, gauge_id, variable, value, source, quantile, basin_version)` |
| `get_weather_forecast_at_lead` | `queries/weather.py:184-257` | same shape, indexed for "what did we predict at lead L for time T" — the hindcast read primitive |

Connection lives at `queries/_connection.py:22-42`; the underlying
read-only pool sets `default_transaction_read_only = on` per
connection in `db/postgres.py:37-62`. There is no write-capable
pool yet — PR 1 adds one for the forecast-sink path.

What the readers **don't** expose:

- **No `forecasts` reader.** Phase 3 needs both a writer and a
  reader against `forecasts`. The reader (`get_forecasts`) is a
  Phase-3 deliverable — folded into PR 6 (comparison report) since
  that is the first PR that reads operationally-written rows.
- **No upstream-of-X gauge query.** Phase 2b parallel-track work
  (`ROADMAP.md:85-121`); not required for Phase 3.
- **No basin-polygon reader.** Baselines consume basin-mean series
  only; raw geometries deferred until Phase 4.

### 1.5 What's missing for baselines

| Baseline | Needs | Have today? |
|---|---|---|
| Persistence | flow obs at gauge id 12 | yes (full history) |
| Recession curve | flow obs at gauge id 12 | yes (full history) |
| Lin regression | flow obs + lagged P/T at gauge id 12 over hindcast window | partial — P/T 2024-04→2025-10 today; full window after operator backfill extension |
| GBT | flow obs + 5-variable forcing over hindcast window | same as lin regression |

Persistence and recession have full inputs as of today. Linear
regression and GBT depend on the operator-side backfill extension;
PR cadence in §4 paces those PRs after the backfill is done.

---

## 2. Baseline candidates

Four baselines per `scoping-genesis.md` §5 (`:425-541`); listed
roughly in implementation-cost order.

### 2.1 Persistence

- **Form.** Forecast = current observation, held flat for N hours
  (deterministic). Optional AR(1) drift fit by OLS as a second
  variant under the same module if PR-3 hindcast diagnostics
  motivate it.
- **Inputs.** Flow at gauge id 12 only.
- **Reader.** `get_observations(conn, gauge_id=12, …,
  value_type='flow')`.
- **Hindcast metric.** KGE primary + MAE + pinball@0.9
  (`scoping-genesis.md:43-46`, `:533-535`). Skill scored against
  persistence itself; persistence's own KGE is the harness sanity
  check.
- **Hindcast window.** 2020-01 → 2024-12. Persistence does not
  need the historical weather backfill.
- **First PR LOC.** ~150–200 (PR 1; minimal — just the baseline,
  the writer, and a smoke test). Harness/metrics module lands later.
- **Risk.** Trivial. Anchors the framework, the writer, and the
  read-back path against a model so simple it cannot be wrong.

### 2.2 Recession curve

- **Form.** Linear-reservoir Q(t) = Q₀·exp(−t/k) per
  `scoping-genesis.md:451-466`. One-parameter fit per gauge via
  `scipy.optimize.curve_fit`; two-reservoir `k_fast / k_slow`
  variant only if PR-3 residuals justify it.
- **Inputs.** Flow at gauge id 12 + event separation. No weather.
- **Reader.** `get_observations(...)`.
- **Hindcast metric.** Same trio. Score separately on
  recession-limb-only subsets — recession is regime-specific and
  its overall hindcast number masks what it is good at.
- **Hindcast window.** 2020-01 → 2024-12.
- **First PR LOC.** ~250 + the harness/metrics module that PR 3
  introduces (~150) + persistence-hindcast backfill in the same
  PR (~50) ≈ 450 total.
- **Risk.** Low. Pitfall: recession alone ignores precip-driven
  step responses. Honest reporting handles it.

### 2.3 Linear regression on lagged P, T, Q

- **Form.** Per `scoping-genesis.md:467-489`: lagged precipitation,
  rolling sums (3 / 7-day), positive degree-day accumulations,
  lagged observed flow, day-of-year sin/cos, antecedent-wetness
  (30-day rolling P). Ridge by default; Lasso for diagnostic
  feature ranking.
- **Inputs.** Flow + temperature + precipitation at gauge id 12
  over the hindcast window. Two of five variables from
  `weather_observations`.
- **Reader.** `get_observations(...)` + `get_weather_observations(
  variables=['temperature', 'precipitation'])`.
- **Hindcast metric.** KGE / MAE / pinball@0.9 per lead-time on
  the grid `{1, 3, 6, 12, 24, 48, 72, 120, 168}` h
  (`scoping-genesis.md:40-41`).
- **Hindcast window.** 2020-01 → 2024-12, requires the operator
  backfill extension to have reached 2012-09-01 by PR 4 merge time.
- **First PR LOC.** ~350. `scikit-learn` as new dep.
- **Lead-time-to-source mapping.** Live forecasting at lead 0–58 h
  uses `met_nordic_forecast_1km` rows; lead 58 h–168 h uses
  `met_locationforecast_2_complete`. Per-variable nuance (shortwave
  gap past +66 h) per `forcing-requirements.md:402-405`. Documented
  here because PR 4 is the first PR that consumes forecast forcings.

### 2.4 Gradient-boosted trees (LightGBM)

- **Form.** Per `scoping-genesis.md:490-516`: same features as
  §2.3 plus rolling min/max/std (7, 30-day) and static catchment
  attributes from `sections_characteristics`. LightGBM with one
  model per target quantile (`objective='quantile', alpha=τ`) for
  τ ∈ {0.1, 0.5, 0.9}.
- **Inputs.** Flow + all five `forcing-requirements.md` §3
  variables at gauge id 12 over the hindcast window.
- **Reader.** `get_observations(...)` +
  `get_weather_observations(variables=None)` (defaults to all five).
- **Hindcast metric.** KGE / MAE / pinball@0.9 per lead-time.
  Pinball is the headline here — GBT is the only baseline emitting
  native quantiles.
- **Hindcast window.** Same as §2.3.
- **First PR LOC.** ~450. `lightgbm` as new dep.
- **Risk.** Medium. Trees can't extrapolate beyond training;
  mitigations spec'd in `scoping-genesis.md:506-510`.

### 2.5 Cross-cutting harness

A `evaluate(model, catchments, window) → DataFrame(lead, metric,
catchment)` per `scoping-genesis.md:539-541` lives once in PR 3
(introduced with recession), exercised by recession, lin
regression, and GBT in PRs 3–5, then by persistence retroactively
inside PR 3 to write persistence's hindcast rows.

The harness writes hindcast results **as forecast rows in the
`forecasts` table** (one row per
(`issue_time`, `valid_time`, `gauge_id`, `value_type`, `model_version`,
`quantile`)), distinguished from live forecasts only by
`model_run_at` ≫ `issue_time`. PR 6 reads them back via a new
`get_forecasts` reader, joins to `observations`, and renders the
comparison report.

---

## 3. Forecast write contract

### 3.1 Existing schema (migration 003)

`db/postgres/migrations/003_forecasts.sql:32-65`:

```
issue_time     TIMESTAMP NOT NULL
valid_time     TIMESTAMP NOT NULL
gauge_id       INTEGER NOT NULL  -- FK gauges
value_type     VARCHAR(6) NOT NULL  -- 'flow' | 'level'
quantile       REAL                  -- NULL for deterministic
value          REAL NOT NULL
model_version  TEXT NOT NULL
```

Two partial-unique indexes on
(issue_time, valid_time, gauge_id, value_type, model_version) — one
for `quantile IS NULL`, one for `quantile IS NOT NULL` — let a
deterministic and a probabilistic row coexist for the same model
run. Hypertable on `valid_time`.

`model_version` is the model identifier (`:39`); convention
`<scheme>_v<N>` (`persistence_v1`, `recession_v1`, `linear_v1`,
`lgb_v1`).

### 3.2 Schema gap — `model_run_at` column (BLOCKER for PR 1)

The Decisions block resolves hindcasts to land in the `forecasts`
table alongside live forecasts. The existing schema does not
distinguish them: `issue_time` carries the forecast's
"as-of" stamp (used for lead-time arithmetic), but for a hindcast
that stamp is set to a historical date the model is pretending it
ran from. There is no column for the wall-clock execution time.

Fix: nokken-web migration adds

```sql
ALTER TABLE forecasts
  ADD COLUMN model_run_at TIMESTAMP NOT NULL DEFAULT NOW();
```

Semantics:

- **Live forecast.** `model_run_at ≈ issue_time` (within minutes of
  the schedule cycle that produced the row).
- **Hindcast.** `model_run_at ≫ issue_time` (the wall-clock time
  the hindcast loop wrote the row, possibly years after
  `issue_time`).

`DEFAULT NOW()` makes the column non-blocking for live writes that
don't pass it explicitly, and lets any inadvertent legacy row pick
up a sensible value. The forecast-writer in PR 1 sets it
explicitly so the contract is unambiguous on the writer side.

**Open design point inside PR 0:** whether `model_run_at` joins the
unique-index keys (so hindcast reruns coexist) or stays purely
audit (so reruns require a `model_version` bump). Recommendation:
purely audit for v1. Reruns bump `model_version` to e.g.
`persistence_v1_hindcast_2026_05_01`; the wall-clock column is
audit only. Cheap to relax later if iteration cadence demands it.

### 3.3 Other schema gaps (none gating Phase 3)

- **No `source` column.** `weather_observations` /
  `weather_forecasts` carry one (`008_weather_rekey_to_gauge.sql:64,119`);
  `forecasts` does not. nokken-web's `DESIGN_NOTES.md` requires
  attribution on Coming-up cards but those rendering surfaces are
  out of this sprint per the Decisions block. Derive attribution
  from `model_version` for v1; revisit when UI rendering lands.
- **No `models` reference table.** `model_version` is a free-form
  string. Phase 3 doesn't need the table; Phase 5+ may.
- **No `basin_version` audit column.** `weather_*` tables stamp
  it; `forecasts` doesn't. Acceptable for Phase 3 — one model run
  uses one polygon — flag as future work.

### 3.4 Lane split

| Concern | Lane |
|---|---|
| `forecasts` table DDL (incl. PR 0 `model_run_at` column) | nokken-web |
| `models` reference table (future) | nokken-web |
| `get_forecasts()` query in `api/src/nokken/db/queries.py` (UI side) | nokken-web (out of sprint) |
| Coming-up rendering, chart data assembly | nokken-web (out of sprint) |
| Forecast generation (training, hindcast, scheduled run) | nokken-forecasting |
| `get_forecasts` reader for the comparison report | nokken-forecasting (PR 6) |
| Writer pool against `forecasts` | nokken-forecasting (PR 1) |
| Systemd unit + operator runbook | nokken-forecasting (PR 2) |
| MET Nordic v4 historical backfill extension (2012-09 → 2024-04) | nokken-data (operator action) |

---

## 4. Sequenced PR breakdown — ~3-4 weeks, 6 PRs (+1 in nokken-web)

Per-repo, one task per PR. Cross-repo coordination flows through
this doc.

### 4.0 PR 0 — nokken-web: add `model_run_at` to `forecasts` (BLOCKER)

- **Repo.** `nokken-web`.
- **Scope.** New migration (next sequence number after 010) that
  `ALTER TABLE forecasts ADD COLUMN model_run_at TIMESTAMP NOT NULL
  DEFAULT NOW()`. Bump pydantic `Forecast` model in
  `api/src/nokken/models/forecasts.py` to add the field. No index
  changes (the column is audit-only per §3.2).
- **Estimated LOC.** ~50.
- **Lands.** Column on the production schema; pydantic model bump.
  `nokken-forecasting` bumps `SCHEMA_COMPAT.md` to the post-PR-0
  SHA in PR 1.
- **Dependencies.** None.
- **Gates.** PR 1.

### 4.1 PR 1 — Phase 3a: persistence baseline + writer + first row

- **Repo.** `nokken-forecasting`.
- **Scope.** Persistence baseline (`baselines/persistence.py`)
  emitting deterministic flat-line forecasts over the 7-day
  horizon at hourly resolution. CLI subcommand
  `nokken-forecasting forecast persistence --gauge-id 12
  --issue-time <iso>` that writes
  `model_version='persistence_v1'` rows to `nessie`'s `forecasts`
  table with `model_run_at = NOW()`. Adds writer-capable pool
  variant in `db/postgres.py` (no read-only init; runs under
  `nokken_forecast_writer` role, see §5). Bumps
  `SCHEMA_COMPAT.md` to the post-PR-0 SHA. Smoke test against the
  integration-fixture Postgres.
- **Deps.** `scipy` (downstream baselines need it; cheap to add now).
- **Estimated LOC.** ~150–200.
- **Lands in DB.** First `forecasts` rows for Faukstad at
  `model_version = 'persistence_v1'`.
- **Dependencies.** PR 0.
- **Out of scope.** Hindcast harness, metrics module, comparison
  report — all land in PR 3 alongside recession.

### 4.2 PR 2 — Phase 3b: scheduled forecast job + operator runbook (PROD MILESTONE)

- **Repo.** `nokken-forecasting`.
- **Scope.** `pipelines/forecast_job.py` runs PR 1's persistence
  baseline on a schedule against `nessie`. CLI:
  `nokken-forecasting run forecast_job` (mirroring nokken-data's
  pattern, `ROADMAP.md:219-220`). Systemd unit
  `deploy/nokken-forecasting-job.service` + timer aligned to
  daily cadence (operator can tighten to per-NWP-cycle later — see
  §5 C). Operator runbook `docs/deploy.md` covering install /
  start / stop / log inspection / incident response. Config
  plumbing for `POSTGRES_DSN` write-role and the schedule.
- **Estimated LOC.** ~300–400.
- **Lands in DB.** Daily Faukstad forecast rows in `forecasts`.
  **This is the "in prod" milestone.**
- **Dependencies.** PR 1.

### 4.3 Operator-driven (parallel with PRs 1-2): MET v4 backfill extension

- **Not a code PR.** Operator runs `nokken-data metno historical
  --gauge-id 12 --start 2012-09-01T00Z --until 2024-04-01T00Z`
  per `historical-backfill.md` in tmux/caffeinate sessions.
- **Wall-clock.** ~21 h serial; resumable; can be split across
  multiple sessions.
- **Lands in DB.** Faukstad `weather_observations` covering
  2012-09 → 2024-04 (stitched onto the existing 2024-04 → 2025-10).
- **Gates.** PR 4 (linear regression) — the regression baseline
  needs the full hindcast window to score over.

### 4.4 PR 3 — Phase 3c: recession baseline + harness + persistence hindcast

- **Repo.** `nokken-forecasting`.
- **Scope.** `baselines/recession.py` with linear-reservoir fit per
  gauge (`scipy.optimize.curve_fit`). Event-separation utility for
  recession-limb scoring. **Introduces the harness:** `evaluate.py`
  with `evaluate(model, catchments, window) → DataFrame(lead,
  metric, catchment)`. Metrics module (`metrics.py`) covering KGE,
  MAE, pinball@0.9. Harness writes hindcast rows directly to
  `forecasts` (`model_run_at = NOW()`, `issue_time` = the as-of
  timestamp the model is pretending it ran from). Backfills
  persistence hindcast rows over the test window in the same PR
  using the new harness.
- **Estimated LOC.** ~450 (recession ~150, harness ~150, metrics
  ~100, persistence-hindcast wiring ~50).
- **Lands in DB.** Hindcast rows for `model_version =
  'persistence_v1_hindcast_<date>'` and
  `'recession_v1_hindcast_<date>'` over 2020-01 → 2024-12.
- **Dependencies.** PR 2.

### 4.5 PR 4 — Phase 3d: linear-regression baseline

- **Repo.** `nokken-forecasting`.
- **Scope.** `features.py` producing the `scoping-genesis.md` §5.3
  feature set from `get_observations` + `get_weather_observations`.
  `baselines/linear.py` with Ridge by default; Lasso as
  diagnostic. Lead-time-to-source mapping for live forcings (0–58 h
  → `met_nordic_forecast_1km`; 58 h–168 h →
  `met_locationforecast_2_complete`) wired into the feature
  builder for the live path; hindcast uses
  `met_nordic_analysis_v4` exclusively. `scikit-learn` as new dep.
- **Estimated LOC.** ~350.
- **Lands in DB.** `linear_v1_hindcast_<date>` rows.
- **Dependencies.** PR 3 (harness); operator backfill extension
  reaching 2012-09-01.

### 4.6 PR 5 — Phase 3e: GBT baseline (LightGBM)

- **Repo.** `nokken-forecasting`.
- **Scope.** `baselines/gbt.py` with three quantile models per
  `scoping-genesis.md:500-503`; reuses PR 4's feature builder;
  native quantile output threaded through the harness so pinball
  scoring scores against three real τ rows. `lightgbm` as new dep.
- **Estimated LOC.** ~450.
- **Lands in DB.** `lgb_v1_hindcast_<date>` rows including the
  three τ ∈ {0.1, 0.5, 0.9} quantile rows per (issue, valid)
  tuple.
- **Dependencies.** PR 4.

### 4.7 PR 6 — Phase 3f: comparison report

- **Repo.** `nokken-forecasting`.
- **Scope.** New `get_forecasts` reader in `queries/forecasts.py`
  matching the §1.4 reader conventions. Comparison script joins
  hindcast rows to `observations`, computes per-baseline KGE / MAE
  / pinball@0.9 per lead-time on the
  `{1, 3, 6, 12, 24, 48, 72, 120, 168}` h grid. Renders
  `docs/phase3-baselines-comparison.md` with one table per metric.
  No new schema; no new DB writes.
- **Estimated LOC.** ~300 (reader ~80, scoring ~120, report
  generation + tests ~100).
- **Lands.** Markdown report; the operator decides which baseline
  becomes the prod default after PR 6 (initially `persistence_v1`
  per PR 2).
- **Dependencies.** PRs 3–5.

### 4.8 Total scope

| Repo | PRs | Estimated LOC |
|---|---|---|
| `nokken-web` | 1 (PR 0) | ~50 |
| `nokken-forecasting` | 6 (PRs 1–6) | ~2,000 |
| `nokken-data` | 0 (operator action) | — |

PR 0 unblocks PR 1; the prod milestone hits at end of week 2 with
PR 1 + PR 2 merged. Quality-improvement PRs 3–5 land through weeks
3–4. PR 6 closes the sprint with the comparison report.

---

## 5. §5 questions — resolved

Each question below is the operator's red-pen call. Numbering kept
for traceability with the prior version of this doc.

### A. Multi-gauge baselines now or later?

**Resolved: Faukstad-only this sprint.** All baselines this sprint
operate on gauge id 12 (six Sjoa sections downstream). The MET
historical fetcher already writes all-gauges-per-hour, so the
future expansion is a query/loop change, not new infrastructure.
Multi-gauge fan-out is post-Phase-3.

### B. Hindcast train/test split

**Resolved: train 2012-09-01 → 2019-12-31; test 2020-01-01 →
2024-12-31.** Train start aligned to MET Nordic v4's archive floor
per `forcing-requirements.md`. Operator extends the MET v4 backfill
to cover the full window in parallel with PR 1 (~21 h wall-clock).

### C. Forecast horizon and cycle alignment

**Resolved: 7 days hourly. Live-forcing source by lead: 0–58 h →
`met_nordic_forecast_1km`; 58 h–168 h →
`met_locationforecast_2_complete`.** Documented in PR 4's design;
not bundled into PRs 1–3 (those baselines don't consume forecast
forcings). Cycle alignment for the scheduled job lands in PR 2 as
a single daily cadence; per-NWP-cycle (4×/day) cadence is a
configuration tightening that can ship later.

### D. Which baseline ships to production?

**Resolved: persistence ships first (PR 2 = "in prod"); better
baselines replace it after PR 6.** The original "all baselines
before prod" framing is rebalanced: persistence gets to prod in
week 2 because the framework, the writer, the schedule, and the
runbook are the load-bearing pieces; the model behind them is
swappable. The operator picks the prod default at end-of-PR-6
based on hindcast metrics in the comparison report.

### E. Does nokken-web's read-side rendering of forecasts ship in this sprint?

**Resolved: out of scope.** "In prod" = daily rows in `forecasts`
written by the scheduled job (Decisions block). UI rendering of
those rows in nokken-web's "Coming up" bucket and section chart is
follow-up work, not a Phase-3 deliverable.

### F. Hindcasts in `forecasts` or as parquet?

**Resolved: in `forecasts`.** Distinguished from live forecasts
by `model_run_at` (introduced by PR 0). Joinable with
`observations` for skill scoring; readable by any downstream
tool. Parquet is rejected as premature optimisation — a separate
artifact format adds operational surface without buying anything
the DB doesn't already give.

### G. Write-scoped DB role for forecast writes?

**Resolved: operator creates `nokken_forecast_writer` role with
INSERT, UPDATE, DELETE on `forecasts` + SELECT on read tables.**
Lives only on production deploy units; local dev keeps using
`nokken_ro` (the harness writes hindcasts only against the
integration-test Postgres or against `nessie` post-deploy). PR 1
takes the role name from `POSTGRES_WRITE_DSN` env var.

### H. CLAUDE.md edit at PR 1 — write-pool addition

**Resolved: yes, fold into PR 1.** The current `db/postgres.py`
read-only-only invariant narrows from "the only pool" to "the
only pool the inspect / query CLIs use." A short paragraph in
the Inspection-CLI section of `CLAUDE.md` clarifies the two
pools' lanes. No new file; small inline edit.

---

## Decisions (final)

*See the block at the top of this document.*
