# Phase 3 scoping — baselines → first forecast row → prod

Sequenced breakdown of Phase 3 against the operator's "~1 month to in
prod" timeline. Sized for one model on one gauge (Faukstad, gauge id
12, the six Sjoa sections downstream of it) with the eventual
all-NVE-gauges-scope deferred. Read top-to-bottom to follow the
proposal; jump to §5 "Open decisions" for what needs operator
red-pen.

The doc is a proposal, not a plan-of-record. The Decisions (final)
block at the end is empty until red-pen closes the §5 questions.

Source material:

- `ROADMAP.md` Phase 3 — `ROADMAP.md:124-151`.
- `docs/scoping-genesis.md` Decisions (final) — `:27-72`; baselines
  survey §5 — `:425-541`.
- `docs/forcing-requirements.md` Decisions (final) — `:28-86`;
  five-variable floor §3 — `:88-117`.
- `docs/queries.md` — query-layer reference.
- nokken-web migrations 003 (`forecasts`), 007 (`basins`), 008
  (`weather_*` rekey).
- nokken-data `MIGRATION_PLAN.md`, `docs/operations/historical-backfill.md`.

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
| `forecasts` | nokken-web 003 (`003_forecasts.sql:32-65`) | partial-unique on quantile (`:49-54`) | `valid_time` | the Phase-3 sink — already exists, empty |

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
  query when next reachable; this scoping doc assumes the live
  feeds are populating per-cycle within Phase-3 horizons but does
  not anchor any LOC estimate on it.

The Faukstad weather backfill **does not yet cover the agreed
hindcast window** of 2012-01 → 2024-12 (`scoping-genesis.md:48-53`).
The 18-month window supports a persistence sanity-check (which
needs only flow observations) and an early recession-curve fit; it
does not yet support multi-year hindcast scoring of any
weather-driven baseline. Extending the backfill to MET Nordic v4's
floor (2012-09-01) is operator work in `nokken-data` and runs in
parallel — see §4 PR cadence and §5 open decision A.

### 1.3 Query layer in `src/nokken_forecasting/queries/`

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
| `get_weather_observations` | `queries/weather.py:76-116` | `(time, gauge_id, variable, value, source, basin_version)`; same time / `value_type` filters plus optional `variables`, `source` |
| `get_weather_forecast_latest_as_of` | `queries/weather.py:119-181` | latest forecast cycle per source as of a given time; emits `(issue_time, valid_time, gauge_id, variable, value, source, quantile, basin_version)` |
| `get_weather_forecast_at_lead` | `queries/weather.py:184-257` | same shape, indexed for "what did we predict at lead L for time T" — the hindcast read primitive |

Connection lives at `queries/_connection.py:22-42`; the underlying
read-only pool sets `default_transaction_read_only = on` per
connection in `db/postgres.py:37-62`. There is no write-capable
pool yet — Phase 6 will add one for the forecast-sink path.

What the readers **don't** expose:

- **No upstream-of-X gauge query.** Phase 2b parallel-track work
  (`ROADMAP.md:85-121`); Phase 3 baselines do not require it.
- **No `forecasts` reader.** Phase 3 needs both a writer and a
  reader against `forecasts` (the writer for emitting predictions,
  the reader for hindcast diagnostics). Both are Phase-3 deliverables,
  not Phase-3b backfill.
- **No basin-polygon reader.** Phase 3 baselines consume basin-mean
  series only (`forcing-requirements.md` §6); raw geometries are not
  in scope until Phase 4 if Shyft-os is picked.

### 1.4 What's missing for baselines

For each Phase-3 baseline (§2 below) the data inputs are:

| Baseline | Needs | Have? | Reader |
|---|---|---|---|
| Persistence | flow obs at gauge id 12 | yes (full history) | `get_observations(gauge_id=12, value_type='flow')` |
| Recession curve | flow obs at gauge id 12 | yes (full history) | same |
| Lin regression | flow obs + lagged P/T at gauge id 12 over hindcast window | partial (P/T only 2024-04→2025-10 today; full window when backfill extends) | `get_observations` + `get_weather_observations` |
| GBT | flow obs + 5-variable forcing at gauge id 12 over hindcast window | same as lin reg; needs all five variables | same |

The honest gap: **regression and GBT baselines depend on the
multi-year MET Nordic v4 weather backfill, not yet run.** The doc
sequences PRs to land persistence first (no dependency) and pace the
weather-driven baselines behind the weather backfill (§4).

---

## 2. Baseline candidates

Four baselines per `scoping-genesis.md` §5 (`:425-541`); listed
roughly in implementation-cost order. Each row anchors on what the
first PR landing the baseline contains, not on the full research
arc behind it.

### 2.1 Persistence

- **Form.** Forecast = current observation, held flat for N hours
  (deterministic). Optional AR(1) drift fit by OLS on lag-1
  residuals as a second variant under the same module.
- **Inputs.** Flow at gauge id 12 only.
- **Reader.** `get_observations(conn, gauge_id=12, start=…, end=…,
  value_type='flow')`.
- **Hindcast metric.** KGE primary + MAE + pinball-loss-at-τ=0.9
  per `scoping-genesis.md:43-46` and `:533-535`. Persistence is
  the baseline the other three are scored against (skill against
  persistence, not against zero); reporting persistence's own KGE
  is the "is the harness producing sane numbers" check.
- **Hindcast window.** 2020-01 → 2024-12 per `scoping-genesis.md:48-53`.
  Persistence does not need the historical weather backfill — it
  reads only the flow column.
- **First PR LOC.** ~600 lines including: `baselines/persistence.py`
  (~80), evaluation harness skeleton `evaluate.py` (~150),
  `metrics.py` (KGE/MAE/pinball, ~100), `pipelines/forecast_job.py`
  scaffold + persistence writer (~120), tests (~100), CLI subcommand
  (~50).
- **Risk.** Trivial to implement, anchoring value is high. The risk
  is that the harness shape needs to absorb three more baselines
  later — biased toward over-design on PR-1. Mitigation: only
  shape `evaluate(model, catchments, window)` to the
  scoping-genesis §5.5 contract; don't try to abstract ahead.

### 2.2 Recession curve

- **Form.** Linear-reservoir Q(t) = Q₀·exp(−t/k) per
  `scoping-genesis.md:451-466`. One-parameter fit per gauge using
  `scipy.optimize.curve_fit`; optionally a two-reservoir
  `k_fast / k_slow` split if the residuals justify it (decide from
  PR-1 hindcast diagnostics, not in advance).
- **Inputs.** Flow at gauge id 12 + an event-separation pass
  (peak detection on observed flow). No weather.
- **Reader.** `get_observations(...)`.
- **Hindcast metric.** Same trio (KGE / MAE / pinball@0.9). Score
  separately on recession-limb-only subsets — recession curve is
  a regime-specific model and its overall hindcast number masks
  what it is good at.
- **Hindcast window.** 2020-01 → 2024-12.
- **First PR LOC.** ~250 lines: `baselines/recession.py` (~120),
  event-separation utility (~50), tests (~60), report addition (~20).
- **Risk.** Low. The pitfall is treating the precip-driven step
  response: recession alone gets the dry weeks right and the rain
  weeks wrong. Honest reporting handles it.

### 2.3 Linear regression on lagged P, T, Q

- **Form.** Per `scoping-genesis.md:467-489`: lagged precipitation
  (0–5 days, rolling 3 / 7-day sums), lagged temperature, positive
  degree-day accumulations as a snowmelt proxy, lagged observed
  flow, day-of-year sin/cos, antecedent-wetness (30-day rolling P).
  Ridge regression by default — collinearity is severe between
  lagged P and rolling sums.
- **Inputs.** Flow + temperature + precipitation at gauge id 12
  over the hindcast window. Two of five variables from
  `weather_observations`.
- **Reader.** `get_observations(...)` + `get_weather_observations(
  variables=['temperature', 'precipitation'])`.
- **Hindcast metric.** KGE / MAE / pinball@0.9 per lead-time on
  the lead grid `{1, 3, 6, 12, 24, 48, 72, 120, 168}` h
  (`scoping-genesis.md:40-41`). Score separately on
  melt-vs-rain-vs-recession regimes if the residuals point that way.
- **Hindcast window.** 2020-01 → 2024-12 *if* the historical
  backfill has reached 2012-01. Without that window the baseline
  PR can still land technically but with a degraded /
  short-window hindcast — flag as caveat in the report.
- **First PR LOC.** ~350 lines: `baselines/linear.py` (~150),
  feature builder `features.py` (~120), tests (~60), report
  addition (~20). `scikit-learn` as new dep.
- **Risk.** Low-medium. The pitfall is mis-specified
  threshold/saturation non-linearities at peak flows. Ridge is
  tractable; lasso for diagnostic feature ranking.

### 2.4 Gradient-boosted trees (LightGBM)

- **Form.** Per `scoping-genesis.md:490-516`: same features as
  §2.3 plus rolling min/max/std (7, 30-day) and static catchment
  attributes from `sections_characteristics`. LightGBM with one
  model per target quantile (`objective='quantile', alpha=τ`) for
  τ ∈ {0.1, 0.5, 0.9} — native quantile output is the hook for
  pinball-loss scoring and the multi-quantile rows the `forecasts`
  table already supports.
- **Inputs.** Flow + all five `forcing-requirements.md` §3
  variables (temperature, precipitation, shortwave, relative
  humidity, wind speed) at gauge id 12 over the hindcast window.
- **Reader.** `get_observations(...)` +
  `get_weather_observations(variables=None)` (defaults to all five).
- **Hindcast metric.** KGE / MAE / pinball@0.9 per lead-time.
  Pinball is the headline metric here — the GBT is the only
  baseline emitting native quantiles.
- **Hindcast window.** 2020-01 → 2024-12, same caveat as §2.3 on
  multi-year forcing availability.
- **First PR LOC.** ~450 lines: `baselines/gbt.py` (~200),
  feature builder reuses §2.3 (~50 of new code), tests (~120),
  report (~50), tuning sweep harness (~30). `lightgbm` as new dep.
- **Risk.** Medium. Trees can't extrapolate beyond training; the
  largest observed flood bounds predictions. Mitigations are spec'd
  in `scoping-genesis.md:506-510` (log-transform target,
  physically-meaningful derived features, separate top-decile
  reporting).

### 2.5 Cross-cutting harness

A `evaluate(model, catchments, window) → DataFrame(lead, metric,
catchment)` per `scoping-genesis.md:539-541` lives once, exercised
by all four. The shape is the
`forecasts`-table-shape-as-DataFrame: `issue_time` × `valid_time`
× `lead_hours` × `quantile` × `value`. The harness produces
parquet hindcast artifacts under
`artifacts/hindcasts/<model>_<window>.parquet`; **hindcasts are
not written to the production `forecasts` table** — the table
holds operationally-issued forecasts only. (This is a proposal —
see §5 open decision F.)

---

## 3. Forecast write contract

The sink schema already exists. nokken-web's `forecasts` table
landed in migration 003 (`db/postgres/migrations/003_forecasts.sql:32-65`)
with the seven columns:

```
issue_time     TIMESTAMP NOT NULL
valid_time     TIMESTAMP NOT NULL
gauge_id       INTEGER NOT NULL  -- FK gauges
value_type     VARCHAR(6) NOT NULL  -- 'flow' | 'level'
quantile       REAL                  -- NULL for deterministic
value          REAL NOT NULL
model_version  TEXT NOT NULL
```

Two partial-unique indexes on the (issue_time, valid_time, gauge_id,
value_type, model_version) tuple — one for `quantile IS NULL`, one
for `quantile IS NOT NULL` — let a deterministic and a probabilistic
row coexist for the same model run. Hypertable on `valid_time` under
the conditional-promotion pattern.

### 3.1 Sufficient for Phase-3 baselines as-is

All four baselines fit the existing schema:

- Persistence and recession emit `quantile = NULL` rows — one per
  (issue_time, valid_time, gauge_id, 'flow', `model_version`).
- Linear regression emits `quantile = NULL` deterministic rows
  (Ridge has no native quantile output without conformal wrapping).
- GBT emits one `NULL` row plus three quantile rows per
  (issue_time, valid_time, gauge_id, 'flow', `model_version`) for
  τ ∈ {0.1, 0.5, 0.9}.

`model_version` carries identity. Proposed convention: `<scheme>_v<N>`
— `persistence_v1`, `recession_v1`, `linear_v1`, `lgb_v1`. Bump
the integer when the trained artifact changes; keep the scheme
prefix stable so a single string identifies the model family.

### 3.2 Schema gaps — none gating Phase 3

Three nice-to-haves the table does not carry. None block Phase 3;
each is flagged as candidate future work for nokken-web.

- **No `source` column.** `weather_observations` /
  `weather_forecasts` carry one (`008_weather_rekey_to_gauge.sql:64,119`);
  `forecasts` does not. nokken-web's `DESIGN_NOTES.md` requires
  attribution on Coming-up cards ("forecast · MET / NVE · updated
  14:00", per the sub-agent inventory of the design doc); the
  attribution can be derived from `model_version` for v1, or
  encoded by adding a `source` column under a future migration.
  v1 derivation is the cheaper option.
- **No `basin_version` audit column.** `weather_*` tables stamp
  it; forecasts don't. If a baseline is sensitive to the polygon
  used to aggregate forcings, reproducibility is currently
  asymmetric. Acceptable for Phase 3 (the polygon doesn't change
  per-row — one model run uses one polygon version); flag as
  future work.
- **No `models` reference table.** `model_version` is a free-form
  string. Phase 3 doesn't need a reference table — but Phase 5+
  (per-section calibrated artifacts, A/B evaluation) will probably
  want one. Out of scope here.
- **No hindcast / forecast distinction in the schema.** Resolved
  by **not writing hindcasts to the production table**. Hindcasts
  stay as parquet artifacts under `artifacts/hindcasts/` in this
  repo. The production table holds only operational forecasts.
  See §5 decision F.

### 3.3 Lane split for the forecast write path

| Concern | Lane |
|---|---|
| `forecasts` table DDL | nokken-web |
| `models` reference table (future) | nokken-web |
| `get_forecasts()` query in `api/src/nokken/db/queries.py` | nokken-web |
| Coming-up rendering, chart data assembly | nokken-web |
| Forecast generation (training, hindcast, scheduled run) | nokken-forecasting |
| Writer pool against `forecasts` (separate from the read-only pool in `db/postgres.py`) | nokken-forecasting |
| Systemd unit for the forecast job | nokken-forecasting |
| MET Nordic v4 historical weather backfill extension (2012-09 → 2024-04) | nokken-data (operator action) |

The nokken-web read-side rendering of forecasts is **flagged as
open**: it is technically the path that takes the first
production-written forecast row and lights up the user-facing
"Coming up" bucket, but the nokken-web sub-agent inventory shows
the surface is currently a stub (`api/src/nokken/services/whats_up.py:95`
returns `coming_up=[]` hardcoded; `routes/section.py:297-302`
fills the forecast chart layer with `[None]`). Whether that
work happens within this 1-month sprint is §5 decision E.

---

## 4. Sequenced PR breakdown — ~3-4 weeks

Each PR is per-repo and one-task-per-PR per the cross-repo
convention in `CLAUDE.md`. Cross-repo coordination flows through
this doc; nothing in the sequence bundles repos. LOC estimates
include tests; "added lines" not "diff lines".

### 4.1 Week 1 — vertical slice to first row

#### PR 1 — Phase 3a-i: persistence baseline + harness skeleton

- **Repo.** `nokken-forecasting`.
- **Scope.** Module skeleton (`baselines/`, `evaluate.py`,
  `metrics.py`, `pipelines/forecast_job.py`); persistence baseline
  (`baselines/persistence.py`) emitting deterministic flat-line
  forecasts; metrics (KGE, MAE, pinball@0.9); harness signature
  `evaluate(model, catchments, window) → DataFrame`; CLI subcommand
  `nokken-forecasting forecast persistence --gauge-id 12
  --issue-time <iso>` that writes one `model_version='persistence_v1'`
  forecast row to `nessie`'s `forecasts` table over the agreed
  7-day × hourly horizon. Adds writer-capable pool variant in
  `db/postgres.py` (no read-only init; runs under a write-scoped
  role distinct from `nokken_ro`).
- **Deps.** `scipy` (for KGE / fitting utilities later), no
  ML deps yet.
- **Estimated LOC.** ~600.
- **Lands in DB.** First `forecasts` rows for Faukstad at
  `model_version = 'persistence_v1'`.
- **Dependencies.** None on prior Phase-3 PRs. Requires nokken-web
  migration 003 already applied (it is) and a write-scoped DB role
  (open decision G).
- **Phase 3 anchor.** This is Phase 3a-i per the user prompt — the
  smallest PR that proves the framework, the writer, and the read
  path against a model so trivial we cannot get the model wrong.

#### PR 2 — Phase 3a-ii: hindcast harness + persistence hindcast report

- **Repo.** `nokken-forecasting`.
- **Scope.** Decouple `evaluate` from PR-1's writer path; accept
  any callable `model(history, forcing) → forecast`. Skill-score
  helper `skill_against_persistence(...)`. Per-lead-time scoring on
  the `{1, 3, 6, 12, 24, 48, 72, 120, 168}` h grid. First report
  `docs/phase3-baselines-comparison.md` containing only persistence
  numbers (the table will grow as PRs 4–6 add baselines). Hindcast
  artifacts written to `artifacts/hindcasts/persistence_v1.parquet`;
  not written to the `forecasts` table.
- **Deps.** none new.
- **Estimated LOC.** ~300.
- **Lands.** Parquet artifact + report skeleton.
- **Dependencies.** PR 1.

### 4.2 Week 2 — cover one weather-driven baseline

#### PR 3 — Phase 3b: recession-curve baseline

- **Repo.** `nokken-forecasting`.
- **Scope.** `baselines/recession.py` with linear-reservoir fit
  per gauge via `scipy.optimize.curve_fit`; event-separation
  utility for recession-limb scoring; harness extension to score
  on regime-conditional subsets; report addition.
- **Deps.** none new (`scipy` from PR 1).
- **Estimated LOC.** ~250.
- **Lands.** Hindcast artifact `recession_v1.parquet`; updated
  comparison report; **no production `forecasts` rows yet for this
  model** — the operator decides when (if) to schedule it for
  production via the §5 decision D.
- **Dependencies.** PR 2 for the harness.

**Parallel operator work, not a PR in this repo.** Extend the MET
Nordic v4 weather backfill from 2024-04 back toward 2012-09. The
`nokken-data metno historical` CLI is resumable
(`historical-backfill.md:79-86`); operator runs it in tmux/caffeinate
sessions. ~47 hours of fetch time at 1.5 s/hour for the full
13-year range; can be split. Lin regression and GBT (PRs 4 and 5)
need this window to score over.

### 4.3 Week 3 — weather-driven baselines

#### PR 4 — Phase 3c: linear-regression baseline + feature builder

- **Repo.** `nokken-forecasting`.
- **Scope.** Feature-engineering module (`features.py`) producing
  the `scoping-genesis.md` §5.3 lag / rolling / DOY feature set
  from `get_weather_observations` + `get_observations` outputs;
  `baselines/linear.py` with Ridge by default and Lasso as
  diagnostic. `scikit-learn` as new dep.
- **Deps.** `scikit-learn`.
- **Estimated LOC.** ~350.
- **Lands.** Hindcast artifact `linear_v1.parquet`; updated
  comparison report.
- **Dependencies.** PR 2 (harness), PR 3 (regime utilities are
  reusable). Hindcast skill numbers depend on how far back the
  weather backfill has reached at PR-merge time — flag in the
  report.

#### PR 5 — Phase 3d: GBT baseline (LightGBM)

- **Repo.** `nokken-forecasting`.
- **Scope.** `baselines/gbt.py` with three quantile models per
  `scoping-genesis.md:500-503`; reuses PR-4 feature builder; native
  quantile output threaded through the harness so the
  pinball-at-τ=0.9 metric scores against three real τ rows;
  log-transform target option; top-decile-events separate scoring.
  `lightgbm` as new dep.
- **Deps.** `lightgbm`.
- **Estimated LOC.** ~450.
- **Lands.** Hindcast artifact `lgb_v1.parquet`; updated comparison
  report; **first quantile rows in `forecasts`** when run via the
  `forecast` CLI at `model_version = 'lgb_v1'`.
- **Dependencies.** PR 4.

### 4.4 Week 4 — comparison + production scheduling

#### PR 6 — Phase 3e: comparison report + ship-to-prod recommendation

- **Repo.** `nokken-forecasting`.
- **Scope.** Final `docs/phase3-baselines-comparison.md` with all
  four baselines on KGE / MAE / pinball@0.9 per lead-time; one
  recommendation for which baseline goes to scheduled prod (§5
  decision D); explicit list of failure regimes per baseline.
  No code changes — a docs PR. Picks a single
  `model_version` to schedule.
- **Estimated LOC.** ~150 (mostly markdown).
- **Lands.** Comparison report. The operator's pick from the §5
  decision D closes the choice for PR 7.
- **Dependencies.** PRs 2–5.

#### PR 7 — Phase 3f: scheduled forecast job (nokken-forecasting)

- **Repo.** `nokken-forecasting`.
- **Scope.** `pipelines/forecast_job.py` runs the chosen baseline
  on a schedule against `nessie`. CLI: `nokken-forecasting run
  forecast_job` (mirroring nokken-data's pattern per
  `ROADMAP.md:219-220`). Systemd unit
  `deploy/nokken-forecasting-job.service` + a operator runbook
  `docs/deploy.md` covering start / stop / rotate / incident response.
  Issue-time aligned with MET forecast cycles (open decision §5
  decision C).
- **Estimated LOC.** ~400.
- **Lands.** Daily / per-cycle forecast rows in `forecasts` for
  Faukstad. The "1 month to in prod" goal hits here.
- **Dependencies.** PR 6.

### 4.5 Cross-repo PRs flagged but not pre-committed

- **nokken-web: `get_forecasts()` query + Coming-up rendering.**
  Implementation only valuable once PR 1 has written rows. Could
  land in week 2 or 3 depending on §5 decision E. ~250 LOC. No
  schema change required. **Out of scope of this 1-month sprint
  by default; flag for operator's call.**
- **nokken-data: 13-year weather backfill extension.** Operator
  action, not a code PR.

### 4.6 Total scope

| Repo | PRs | Estimated LOC |
|---|---|---|
| `nokken-forecasting` | 7 | ~2,500 |
| `nokken-web` (optional read path) | 1 | ~250 |
| `nokken-data` | 0 (operator action) | — |

Per-PR ceiling is well under the 500-LOC / 8-file CLAUDE.md
guideline once tests are accounted for; PR 1 (~600 LOC) is the
single PR that flirts with it and is sized that way because the
module skeleton is one-time cost.

---

## 5. Open decisions for operator red-pen

### A. Multi-gauge baselines now or later?

Faukstad first (gauge id 12, six Sjoa sections). The new MET
historical fetcher writes all-gauges-per-hour, so the per-gauge
marginal cost of operationalising more gauges is lower than at
Phase-2-spec time. Still, baselines are validated against
hindcast skill at one gauge before the same model class is rolled
out to others.

- **Proposal.** Phase 3 stays scoped to Faukstad. Multi-gauge
  rollout is a Phase-5-equivalent concern that opens after Phase
  4 framework choice. The Phase 3 evaluation harness signature
  `evaluate(model, catchments, window)` already takes `catchments`
  as a list, so going multi-gauge is a configuration change, not
  a refactor.

### B. Hindcast train/test split — 2012-2019 / 2020-2024 unchanged?

`scoping-genesis.md:48-53` carries the agreed 2012-01-2019-12 /
2020-01-2024-12 split, with training start bounded by MET Nordic
v4's hourly availability floor (2012-09-01).

- **Proposal.** Keep as-is. The recent-window-as-test rationale
  ("that is the climatology production will face") is correct;
  the only honest reason to revisit would be if the multi-year
  weather backfill discovers a precipitation step-change worse
  than the 2016-11-08 known caveat in `forcing-requirements.md:194-198`.
  Re-litigate on data, not in advance.

### C. Forecast horizon and cycle alignment

7-day × hourly horizon with hindcast scoring at lead-times
{1, 3, 6, 12, 24, 48, 72, 120, 168} h is closed
(`scoping-genesis.md:40-41`). Cycle alignment is open decision 10
in `scoping-genesis.md:705-707` (deferred to end of Phase 3).

- **Proposal.** Align the scheduled forecast job (PR 7) to MET's
  Nordic-area NWP cycles at 00 / 06 / 12 / 18 UTC plus a 30-minute
  lag for forcing fetcher catch-up. Four runs/day. Re-litigate to
  once-daily if four runs/day is operational overkill — a
  configuration change in the systemd unit, not a redesign.

### D. Which baseline ships to production?

Phase 3 produces four baselines. Phase 6 (the production job)
runs one. The §4 sequence proposes the operator chooses at PR 6
after seeing all four hindcast numbers.

- **Proposal.** Default to the highest-KGE baseline at lead 24 h
  on the agreed test window, breaking ties on MAE. If the GBT's
  pinball-at-τ=0.9 advantage is material (>20% improvement over
  the next baseline) it wins outright on the high-flow-tail-skill
  argument from `scoping-genesis.md:43-46`. Persistence is a valid
  shipped baseline if no other model beats it — in which case the
  Phase-4 framework evaluation has more leverage than it does
  today.

### E. Does nokken-web's read-side rendering of forecasts ship in the same sprint?

The chart and Coming-up surfaces in nokken-web are stubs
(`services/whats_up.py:95`, `routes/section.py:297-302`). Once
PR 1 writes rows, the data exists; the UI does not yet render
it. Lighting up that surface is one nokken-web PR (~250 LOC) but
not a forecasting concern.

- **Proposal.** **Out of scope for this 1-month sprint.** The
  forecasting-side goal is "rows in the table"; the rendering is
  follow-on work and gates on operator priority. Operator can
  override and add it in week 3 or 4 if it fits.

### F. Hindcasts in `forecasts` or as parquet artifacts?

A hindcast is a forecast issued "as if" at a past time t for
t+lead, scored against observed. The `forecasts` table can
technically hold them — `issue_time` doesn't constrain to "now".
The cost is conflating production forecasts and research
artifacts in one table that nokken-web reads from.

- **Proposal.** **Hindcasts stay as parquet artifacts in
  `nokken-forecasting/artifacts/hindcasts/`.** Production
  `forecasts` table holds only operationally-issued forecasts.
  This avoids a `is_hindcast` schema bump and keeps the read
  path semantically clean.

### G. Write-scoped DB role for forecast writes?

The query layer's read-only pool runs under `nokken_ro`
(per `CLAUDE.md` Secret hygiene). The forecast writer needs a
distinct role with INSERT (and possibly UPDATE for restart
semantics) on `forecasts` only. Adding the role is a
nokken-web operator action against `nessie`.

- **Proposal.** Operator creates a `nokken_forecast_writer` role
  with `INSERT, UPDATE, DELETE ON forecasts` and `SELECT` on the
  read tables, scoped to the public schema. The role lives only
  on production deploy units; local dev keeps using `nokken_ro`
  (no writes locally). Local integration tests already use a
  writable connection per `tests/integration/queries/conftest.py`.

### H. CLAUDE.md edit at PR 1 — write-pool addition

The current `db/postgres.py:1-62` is read-only-only. PR 1
introduces a write-capable pool variant. The addition needs a
matching note in `CLAUDE.md` Inspection-CLI section and Secret
hygiene block — the read-only pool guarantee narrows from
"the only pool" to "the only pool the inspect / query CLIs
use". The forecast-job pool is governed separately.

- **Proposal.** Land the CLAUDE.md edits in PR 1 alongside the
  new pool. No new file; just a paragraph clarifying the two
  pools' lanes.

---

## Decisions (final)

*Empty until operator red-pen closes the §5 questions. Same
convention as `scoping-genesis.md:27-72` and
`forcing-requirements.md:28-76`.*
