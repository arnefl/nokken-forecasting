# nokken-forecasting — genesis scoping

Scoping document for the `nokken-forecasting` repo. Establishes the
forecasting mission, surveys what nokken-web and nokken-data already
provide, compares Shyft-os against a baseline modelling stack, and
lists the open decisions the user must close before the next PR.

This file stays as archaeology after `ROADMAP.md` lands — the same
pattern nokken-data follows (`nokken-data/CLAUDE.md:9`).

Source material traced for this doc:
- Sibling repo `nokken-web` at `/Users/arne/code/nokken-web/` —
  migrations under `db/postgres/migrations/`, pydantic row models
  under `api/src/nokken/models/`, and `INVENTORY.md`.
- Sibling repo `nokken-data` at `/Users/arne/code/nokken-data/` —
  `MIGRATION_PLAN.md`, `docs/scoping.md`, and `src/nokken_data/`.
- Shyft-os public documentation, source tree, and the 2021 GMD paper
  (Burkhart et al., v4.8). URLs inline in §4.
- Widely-cited hydrology-methods references for §5; URLs inline.

Nothing from `/Users/arne/code/nokken-web/www_old/` (legacy PHP, in a
separate private repo) was read — explicitly forbidden by both
siblings' `CLAUDE.md`.

---

## Decisions (final)

Compact reference for phase PRs — read this block alone if you don't
need the archaeology. Each bullet points at the section where the
decision is elaborated; §8 tags each item closed / deferred / open.

- **First target section** — Sjoa, modelled on gauge id 12 (NVE
  "Faukstad", sourcing key `2.595.0`). Six `sections` rows (ids 54,
  55, 56, 57, 128, 142) link to this gauge, all with `gauge_sub=0`
  (primary linkage). One model serves the whole Sjoa stretch;
  per-section paddleability resolves via the existing `flowMin` /
  `flowMax` thresholds in nokken-web. (§6)
- **Forecast horizon** — 7-day forecast, hourly resolution.
  Hindcast scored at lead-times {1, 3, 6, 12, 24, 48, 72, 120, 168}
  hours, each independently. (§5.5)
- **Primary metric** — KGE as the primary "beats baseline" number;
  MAE (m³/s) as operator-readable secondary; pinball loss at τ=0.9
  for high-flow-tail skill. All three reported per-lead-time as
  skill scores against a persistence baseline (not against zero).
  (§5.5)
- **Hindcast window** — train 2003-01 → 2019-12 (~17 years);
  hindcast 2020-01 → 2024-12 (5 years). Recent test window
  deliberately — that is the climatology production will face.
  Unconstrained on the Faukstad observation side; revisit if
  shorter-history sections are added later. (§5.5)
- **Forecast-sink + weather table families** — three table families
  owned by nokken-web: (a) forecasts written by
  nokken-forecasting; (b) historical weather / forcing written by
  nokken-data; (c) weather forecasts written by nokken-data. All
  hourly, basin-mean per section (or per catchment — nokken-web's
  call). nokken-forecasting proposes shapes for (a) and (c) in the
  Phase 2 nokken-web PR. (§2.4, §7.2)
- **Forcing aggregation ownership** — nokken-data clips gridded
  forcings to basin polygons and writes hourly basin-mean series to
  Postgres; nokken-forecasting reads tidy time-series and never
  touches a raster; nokken-web reads the same tables for UI. Raw
  grids (MET, seNorge, NORA3) are refetchable upstream, not
  persisted. If Phase 4 picks Shyft-os and needs per-cell forcings,
  that is a new path built then, not now. (§7.1)

Still open (carried in §8): framework-choice timing (6), Shyft-os
local dev path (9), output cadence (10), REGINE / vassdrag
upstream-gauge auto-discovery (11, parallel track to Phase 2).

---

## 1. Purpose & scope

**Goal.** Produce short-horizon (1–7 day) forecasts of river flow
and/or water level at a set of paddling river sections on
[nokken.net](https://nokken.net), writing the outputs back into the
shared Postgres database for nokken-web to render (currently as the
"Coming up" bucket on the Home page; see
`nokken-web/DESIGN_NOTES.md:119` for the planned consumption).

**In scope.**
- Reading observations and, eventually, forcing data from the shared
  Postgres DB owned by nokken-web.
- Training and calibrating models on historical observations.
- Running scheduled forecast jobs and writing outputs through a
  forecast-sink table whose DDL is owned by nokken-web (see §7).
- Hindcast harnesses and evaluation metrics.

**Out of scope.**
- Any web or mobile UI — that is nokken-web's surface.
- Any upstream-API ingestion (NVE HydAPI, GLB, MET, Kartverket) —
  that is nokken-data's surface. If a forcing feed is missing, the PR
  that lands it goes in nokken-data first.
- Any SQL migration — nokken-web owns the schema (see both
  siblings' `CLAUDE.md` under "Schema ownership").
- Arbitrary basins and non-Norwegian rivers. The target set is the
  paddling sections in the `sections` table today; initial modelling
  narrows to one well-instrumented section (see §6).
- Real-time sub-hourly forecasting. Paddlers plan days ahead; 1–7 day
  daily resolution is the working target horizon.

---

## 2. nokken-web inventory

The schema is authoritative in `db/postgres/migrations/` and mirrored
in pydantic row models under `api/src/nokken/models/`. Only three
migrations exist today (`000_schema_migrations.sql`,
`001_reference_tables.sql`, `002_timeseries_tables.sql`) — the
"16 reference tables plus time-series" consolidation.

### 2.1 Tables relevant to forecasting

All columns below are exact from the migrations unless flagged
otherwise. Citations point at the migration file and line range.

- **`gauges`** (`db/postgres/migrations/001_reference_tables.sql:39-49`)
  — measurement-station metadata. Columns a modeller cares about:
  `gauge_id`, `gauge_name`, `has_flow` (SMALLINT 0/1), `has_level`
  (SMALLINT 0/1), `source` (TEXT — e.g. `'nve'`, `'glb'`, `'manual'`;
  enumerated in `api/src/nokken/models/gauges.py`), `sourcing_key`
  (provider's station id), `drainage_basin` (NUMERIC(6,2)),
  `location` (INTEGER — likely a FK to `geo_spots` by observation of
  the column type, but flagged as an open check), `gauge_active`.

- **`sections`** (`001_reference_tables.sql:80-103`) — the paddling
  sections themselves. Linkage columns: `gauge_id` (nullable FK to
  `gauges`), `gauge_sub` (SMALLINT NOT NULL — flagged per
  `nokken-web/INVENTORY.md` as "triggers warning"; exact semantics
  are an open decision, see §8), and `gauge_default` (`VARCHAR(5)`
  default `'flow'` — which indicator is canonical for this section).
  Threshold bands: `flowAbsoluteMin`/`flowMin`/`flowMax`/
  `flowAbsoluteMax` as `REAL`, and four `level*` columns as
  `VARCHAR(10)` (the string typing is a legacy MySQL translation
  convention; see the comment at lines 1–27 of the migration). No
  many-to-many section-gauge join table exists; each section has at
  most one gauge.

- **`rivers`** (`001_reference_tables.sql:32-36`) — parent
  aggregation: `river_id`, `river_name`, `country_code`.

- **`sections_characteristics`** (`001_reference_tables.sql:114-124`)
  — `distance`, `elevation_drop`, `elevation_slope`, `elevation_min`,
  `elevation_max`. Usable as static features per section.

- **`geo_spots`** (`001_reference_tables.sql:71-77`) — put-in /
  take-out coordinates: `spot_id`, `latitude`, `longitude`,
  `description`. Joined to `sections` via `sections_features`
  (`001_reference_tables.sql:155-172`).

- **`sections_status`** (`001_reference_tables.sql:201-213`) —
  cached latest state per section: `flow`, `level`, `flow_status`
  (INTEGER 1..5), `flow_time`. Written by the
  `update_flow_status` pipeline (not yet ported — see §3). Not a
  training target; derived from observations.

- **`observations`** (`db/postgres/migrations/002_timeseries_tables.sql:23-32`)
  — one row per gauge reading. Columns: `time` (TIMESTAMP without
  time zone), `gauge_id`, `value_type` (`VARCHAR(6)`, observed in
  code as `'flow'` or `'level'`), `value` (REAL). Unique on
  `(time, gauge_id, value_type)`. Run as a TimescaleDB hypertable in
  production (conditional promotion at lines 48–57 of the migration
  — plain table in CI / local stacks without the extension).

- **`statistics`** (`002_timeseries_tables.sql:34-46`) — day-of-year
  climatology: `day`, `month`, `gauge_id`, `value_type`, `average`,
  `minimum`, `maximum`, `q1`, `q2`, `q3`. Sparsely populated in
  production per `api/src/nokken/db/queries.py` query comments; not
  a training target but a useful baseline / anomaly reference.

- **No weather / precipitation / temperature tables exist.**
  Verified by grep across
  `db/postgres/migrations/001_reference_tables.sql` and
  `002_timeseries_tables.sql`. Forcing data is a gap — see §7.

### 2.2 Which sections have an upstream gauge linked?

The linkage is a single nullable FK (`sections.gauge_id`), so the
answer at any point in time is:

```sql
SELECT COUNT(*) FROM sections WHERE gauge_id IS NOT NULL;
```

Without live DB access this scoping doc cannot give a count; the user
should run the query above (or the equivalent against a production
replica) when choosing the first target section (§6). The
`gauge_sub` flag distinguishes "primary" from "proxy/substitute"
linkages but its exact semantics are not documented in the migration
itself — `INVENTORY.md:B` describes it as "triggers warning" and
`api/src/nokken/models/whatsup.py` (read model) comments mention it
in passing. **Open decision (§8).**

### 2.3 Which gauges have observations landed?

Again, a live-DB query:

```sql
SELECT gauge_id, MIN(time), MAX(time), COUNT(*)
FROM observations
WHERE value_type = 'flow'
GROUP BY gauge_id;
```

The canonical read path in application code is
`get_observations()` at `api/src/nokken/db/queries.py`. In production
the `observations` table is a TimescaleDB hypertable with ~1500 child
chunks (per the migration comment at `002_timeseries_tables.sql:9-15`),
so the table is genuinely large and has years of history — but the
exact per-gauge history span is not knowable from the repo alone.

### 2.4 Forecast output table — not yet defined

The legacy MySQL schema had a `gauge_estimates` table
(`nokken-web/INVENTORY.md:161` — `gauge_id`, `date`, `flow`,
`precipitation`). **No Postgres migration for it exists in the
current schema.** The pydantic model file
`api/src/nokken/models/timeseries.py:6-11` claims the migration is
"tracked at `db/postgres/migrations/001_gauge_estimates.sql`", but
that file does not exist on disk (verified by listing
`db/postgres/migrations/` — only `000_*.sql`, `001_reference_tables.sql`,
`002_timeseries_tables.sql`). The comment is a stale TODO marker.

nokken-web's consumption is already sketched:
`DESIGN_NOTES.md:119,142,183,209` reference
`gauge_estimates` as the "Coming up" forecast source;
`services/whats_up.py:11-12` short-circuits the "Coming up" bucket to
`[]` until forecasts are populated; `MIGRATION_PLAN.md:52-57` lists
the "Auto-forecasting service" as post-v1 future scope writing into
`gauge_estimates` "or a forthcoming forecast table". The **shape and
name of the forecast-sink table is an open decision** that a
future nokken-web PR must close (§7, §8).

---

## 3. nokken-data inventory

### 3.1 What ships today

`nokken-data/MIGRATION_PLAN.md` tracks phase progress via checkboxes.
Verified against the source tree in `src/nokken_data/`:

| Phase | Status | Pipeline / module | Writes |
|-------|--------|-------------------|--------|
| 1a | ✓ | `pipelines/scrape_glb.py` + `sources/glb.py` — pulls the public GLB JSON feed and appends observations | `observations` (Postgres) |
| 1b | ✓ | `pipelines/scrape_nve_flow.py` + `sources/nve_hydapi.py` — NVE HydAPI parameter `1001` for instantaneous flow at active gauges with `has_flow=1` | `observations` |
| 1c | ☐ | `scrape_nve_level` (NVE HydAPI parameter `1000`) — not yet ported | — |
| 1d | ☐ | `update_manual` — derived series for gauge ids 51 (Rauma Lower) and 52 (Lora), hardcoded per `nokken-data/CLAUDE.md:89-96` | — |
| 1e | ☐ | `scheduler.py` (APScheduler) + systemd service wiring — not yet installed; `deploy/nokken-data-scheduler.service` is a stub | — |
| 2 | ☐ | `update_flow_status` — classifies `sections_status.flow_status` into 1..5 | `sections_status` |
| 3 | ☐ | `alerts` — push-alert pipeline, gated on nokken-web Phase 8 per `nokken-data/CLAUDE.md:82-88` | `alerts` (MySQL today, Postgres post-cutover) |

Citations: `nokken-data/MIGRATION_PLAN.md:10-116` (the full phase
list with checkboxes), `nokken-data/docs/scoping.md:94-95` (NVE
parameter numbers), `nokken-data/CLAUDE.md:89-96` (hardcoded derived
gauges).

### 3.2 Upstream APIs wired today

- **GLB** — public JSON feed for daily discharge and water level at
  GLB-operated gauges. Handled in `src/nokken_data/sources/glb.py`.
- **NVE HydAPI** — `hydapi.nve.no/api/v1/Observations` with
  `X-API-Key` header; parameters `1001` (flow) and `1000` (level).
  Client at `src/nokken_data/sources/nve_hydapi.py`, per the legacy
  `pyHydAPI.py` behaviour transcribed at
  `nokken-data/docs/scoping.md:108` (retries once on HTTP 429).
- **Prowl** and **Pushover** — push channels for operator alerts on
  scraper errors, at `src/nokken_data/push/`. Not data sources.

### 3.3 What is NOT wired (forecasting-relevant)

- **MET Norway** — no frontend API (locationforecast), no thredds
  reanalysis (`met_forcing_v2`), no radar QPE. Grep across
  `nokken-data/` finds zero mentions of `met.no` or
  `api.met.no`.
- **Kartverket** — no fetchers today. The forecasting-relevant
  Kartverket product is the national detailed height model (DTM
  at 1, 10, and 50 m resolution), distributed via
  [høydedata.no](https://hoydedata.no/) under Kartverket's open
  terms of use ([Kartverket terrengdata page](https://www.kartverket.no/api-og-data/terrengdata)).
  A DEM is not needed for the §5 baselines; it is conditionally
  needed for Phase 4 Shyft-os cell-vector construction per §4.1.
  (The N5 reference in `nokken-web/MIGRATION_PLAN.md:62-72` is a
  separate UI-side elevation-profile concern, not a forecasting
  input.)
- **ECMWF open data** — not wired.
- **MET reanalysis / NORA3 / seNorge** (the gridded
  temperature+precipitation products that would be the natural
  historical forcing for a Norwegian hydrological model) — not
  wired.

Legacy `cron_nokken` did not have a weather fetcher either. The
legacy "forecaster" job in `status_file.csv` is explicitly flagged as
a dead row (`nokken-data/docs/scoping.md:106,110,482`) and dropped
from scope.

### 3.4 Scheduler / cron config

Scheduling is moving from the legacy VM crontab to a single
APScheduler process under the `nokken-data-scheduler` systemd unit
(`nokken-data/CLAUDE.md:29-32`, `nokken-data/MIGRATION_PLAN.md:73-76`).
The service is not yet installed; cadence today comes from the
legacy crontab's 10-minute tick
(`nokken-data/docs/scoping.md` §1.1). Once Phase 1e ships, cadences
live in `src/nokken_data/scheduler.py` as Python config. The forecast
job proposed here will get its own schedule entry — see §7.

### 3.5 Schema compat pin

nokken-data pins a specific nokken-web commit SHA in
`SCHEMA_COMPAT.md:12-14` (`c0f0ff705c138ab596884dd3c00f00858dcaa063`,
pinned 2026-04-22). CI applies nokken-web's migrations at that SHA
to a throwaway Postgres and runs integration tests. This repo
adopts the same pattern in Phase 2 — see `ROADMAP.md`.

---

## 4. Shyft-os survey — adoption-decision depth

[Shyft-os](https://gitlab.com/shyft-os) is an open-source hydrologic
and energy-market modelling framework with a modern C++ core and a
Python API. Built at Statkraft in cooperation with the University of
Oslo; used in Statkraft's 24×7 operational environment to forecast
inflow to Norwegian hydropower reservoirs
([project overview](https://shyft.readthedocs.io/en/latest/content/project/what_is_shyft.html),
[Burkhart et al., *GMD* 14, 821 (2021)](https://gmd.copernicus.org/articles/14/821/2021/)).
Its idiomatic use case is **distributed snowmelt-driven inflow
forecasting for reservoirs**, which partially overlaps our problem
but is not the same problem.

### 4.1 Inputs it expects

- **Forcing variables.** Temperature, precipitation, and — for the
  full PTGSK stack — relative humidity, wind speed, and radiation.
  Typically point time-series from stations, or gridded NWP output
  (AROME-MetCoOp, ECMWF deterministic/ensemble). Shyft interpolates
  station to cell via IDW or Bayesian kriging internally
  ([GMD paper](https://gmd.copernicus.org/articles/14/821/2021/),
  [advanced_simulation notebook](https://notebook.community/statkraft/shyft-doc/notebooks/nea-example/advanced_simulation)).
- **Time resolution.** Hourly is the canonical example (Nea-Nidelva
  runs 8760 steps/year); daily is also supported via the
  `TimeAxis(start, delta, n)` abstraction.
- **Format.** NetCDF (one file per variable, `stations × time`)
  configured through a YAML repository layout pointing at a
  `shyft-data` directory is the shipped-example path.
- **Catchment description.** A pre-built *cell vector* (not a raw
  DEM). Each cell carries location, area, elevation, land-cover
  fractions (glacier/lake/reservoir/forest/unspecified), and a
  catchment id. You must construct this cell vector from outside
  inputs — typically NVE basin polygons plus a DEM, plus a
  land-cover raster — before Shyft sees the catchment.
- **Calibration data.** Observed discharge at the catchment outlet
  (optionally snow water equivalent or snow-covered area). Multi-gauge
  weighted calibration is supported.

### 4.2 Install / dependency footprint

- [Current PyPI release `shyft 26.0.0.post1`](https://pypi.org/project/shyft/)
  (Feb 2025) ships a **Windows CPython 3.11 wheel only** — no sdist,
  no Linux wheel, no macOS wheel.
- On Linux the documented paths are (a) the `sigbjorn` conda
  channel, or (b) a from-source build against gcc ≥7, CMake ≥3.13,
  Boost ≥1.70, dlib ≥19.6, Armadillo ≥9.3, BLAS/LAPACK, NumPy,
  netCDF4, Python 3.6+
  ([libraries.io `shyft`](https://libraries.io/pypi/shyft)).
- **No published macOS wheel.** Local dev on the operator's Mac
  requires a from-source build (open decision — §8).
- CI implication: either a conda-based image or a custom build
  pipeline; neither is free.

### 4.3 Where it shines for our use case

- Distributed snow-dominated hydrology across multiple interchangeable
  model stacks (PTGSK, PTSSK, PTHSK, HBV); directly relevant for
  Norwegian snowmelt-driven spring floods, which are exactly the
  "flashy high-flow windows" paddlers care about.
- Native ingestion of AROME/ECMWF ensembles for probabilistic
  hindcasts.
- A built-in Distributed Time-Series System (DTSS) for persistent
  time-series storage — not something we need since we have Postgres,
  but indicative of operational pedigree.

### 4.4 Where it is overkill (for our use case)

- Our horizon is 1–7 days for roughly ~180 gauges / sections, not
  multi-week reservoir inflow ensembles across a fleet of
  hydropower plants.
- The DTSS, energy-market modules, Bayesian-kriging interpolators,
  and yaml-configured region orchestrations are not needed to answer
  "is the Sjoa paddleable on Saturday?".
- The framework locks the data pipeline into Shyft's NetCDF +
  YAML-repository conventions; substituting or composing with
  modern-Python ML (LightGBM, torch, sklearn) means bypassing most
  of the framework.

### 4.5 What adopting it commits us to

- **License:** LGPL-3.0 — linkable from our code without copyleft
  propagation; modifications to Shyft itself must be shared.
- **Operator knowledge:** debugging reaches into modern C++ + Boost
  / dlib / Armadillo when the Python layer fails.
- **Upstream maintenance:** alive but small; Statkraft's core team
  is effectively the bus factor. Release cadence is rolling (292
  PyPI releases since 2018 per libraries.io).
- **Data conventions:** NetCDF per variable + YAML repository
  config, or we write our own repository adapters.

### 4.6 Minimal reality check

A single-catchment head-to-head hindcast would need: a Linux/conda
env with `shyft` from the `sigbjorn` channel; a cell vector built
from DEM + NVE polygons + land-cover; NetCDF temperature &
precipitation for a calibration window; a YAML
repository/interpolation config; a `TimeAxis` over the hindcast
period; observed outlet discharge; and a driver using the
region-model → `interpolate()` → stack-run → calibrate pattern from
[the Nea-Nidelva tutorial](https://shyft.readthedocs.io/en/latest/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.html).
Estimated 2–5 days of setup before the first hindcast number.

---

## 5. Baseline stack survey — fair-comparison depth

Four baselines below, in implementation-cost order. Each is a
candidate for the head-to-head hindcast in Phase 4.

### 5.1 Persistence

- **Inputs.** Observed discharge at the target gauge only.
- **Training signal.** None for the naive form; a one-parameter
  AR(1)/drift form fits by OLS on lag-1 residuals.
- **Evaluation.** MAE, NSE, KGE at 1–7-day leads on a held-out
  window.
- **Python.** `pandas` shifts; `statsmodels` for AR/ARIMA;
  `sktime` for seasonal naive.
- **Strengths.** Zero data cost. Often the correct benchmark any
  sophisticated model must beat at lead-1 on recession limbs
  ([Kratzert et al., *HESS* 23, 5089 (2019)](https://hess.copernicus.org/articles/23/5089/2019/)).
- **Weaknesses.** Fails on rising limbs — the exact events paddlers
  care about. A non-trivial persistence benchmark is "seasonal
  naive with AR(1) drift" or "persistence-of-anomaly vs. day-of-year
  climatology"; the `statistics` table gives us DOY climatology for
  free.

### 5.2 Recession-curve fits

- **Form.** Linear reservoir, Q(t) = Q₀·exp(−t/k). Canonical
  baseflow model in Chow / Maidment / Mays' *Applied Hydrology* and
  [HEC-HMS recession docs](https://www.hec.usace.army.mil/confluence/hmsdocs/hmstrm/baseflow/recession-model).
- **Inputs.** Discharge history plus event separation (peak
  detection).
- **Parameters per gauge.** Recession constant `k` (sometimes
  seasonal); a threshold Q for "in recession"; optionally a
  two-reservoir k_fast / k_slow split.
- **Evaluation.** MAE on recession-limb-only subsets over several
  years; full-hindcast NSE to expose where it fails.
- **Python.** `scipy.optimize.curve_fit`; optionally
  `hydrofunctions` or `baseflow` for event separation.
- **Strengths.** Trivially interpretable; near-optimal during the
  dry-spell windows that dominate Norwegian summer paddling.
- **Weaknesses.** Undefined behaviour during precipitation or melt
  onset; no forcing mechanism.

### 5.3 Linear regression on P, T, upstream gauge

- **Features.** Lagged basin-mean precipitation (0–5 days and
  rolling 3/7-day sums), lagged air temperature and positive
  degree-day accumulations, upstream gauge lags where a section has
  one, day-of-year sin/cos, antecedent-wetness proxy (30-day rolling
  P). Degree-day features are a standard cheap snowmelt proxy
  ([Engeland et al., *HESS* 23, 723 (2019)](https://hess.copernicus.org/articles/23/723/2019/)).
- **Regularisation.** Ridge by default — severe collinearity between
  lagged P and rolling sums. Lasso for feature selection. OLS as a
  diagnostic only.
- **Regime handling.** A regime indicator (snowpack present/absent
  from a temperature-based flag) interacted with melt features, or
  two separately fit models combined by a gate.
- **Evaluation.** MAE, KGE at 1–7-day leads; separate scoring on
  melt vs. rain regimes.
- **Python.** `scikit-learn` (`Ridge`, `Lasso`, `Pipeline`); `pandas`
  for lag features.
- **Strengths.** Fast, auditable, near-linear in low-flow /
  recession regimes.
- **Weaknesses.** Mis-specifies threshold and saturation
  non-linearities; poor at peak magnitudes.

### 5.4 Gradient-boosted trees on the same features

- **Libraries.** `LightGBM`, `XGBoost`, or
  `sklearn.HistGradientBoostingRegressor`.
- **Feature engineering.** Same features as §5.3 plus rolling
  min/max/std (7, 30-day), and static catchment attributes (area,
  mean elevation, glacier fraction from the
  `sections_characteristics` table and any gauges-level metadata)
  as constant columns — the CAMELS-style setup
  ([Kratzert 2019, HESS](https://hess.copernicus.org/articles/23/5089/2019/)).
- **Probabilistic wiring.** One model per target quantile using the
  pinball / quantile objective (LightGBM
  `objective="quantile", alpha=τ` for τ ∈ {0.1, 0.5, 0.9}), or
  NGBoost, or a conformal wrapper (`MAPIE`).
- **Pitfalls on snowmelt floods.** Trees cannot extrapolate beyond
  the training envelope — the largest observed flood effectively
  bounds predictions. Mitigations: log-transform target, add
  physically meaningful derived features (cumulative melt
  potential, antecedent SWE proxy), report skill separately on the
  top-decile events
  ([Tyralis et al., *JoH* 617, 2023](https://www.sciencedirect.com/science/article/abs/pii/S0022169423000240)).
- **Strengths.** Best single-model skill of the classical baselines;
  handles mixed rain/snow regimes without hand-coded gating; native
  quantiles.
- **Weaknesses.** Extrapolation; feature engineering still
  required; retraining per catchment unless pooled.

### 5.5 What a fair side-by-side against Shyft-os looks like

Five things must be locked before any comparison is credible:

1. **Same catchments.** The paddling-relevant gauges, agreed
   up-front; no post-hoc filtering.
2. **Same hindcast window.** Rolling-origin re-forecasts over ≥5
   years covering both snowmelt springs and rain-driven autumns,
   issued *as if* only data up to t₀ were known, using the same
   NWP forcing feed Shyft-os consumes operationally.
3. **Same train/test split.** Klemeš-style split-sample
   ([Klemeš, *Hydrol. Sci. J.* 1986](https://www.tandfonline.com/doi/pdf/10.1080/02626668609491024));
   ideally a differential split-sample separating wet/dry or
   warm/cold years.
4. **Same metrics.** Report KGE and NSE
   ([Gupta et al., *J. Hydrol.* 377, 80 (2009)](https://agupubs.onlinelibrary.wiley.com/doi/10.1029/2008WR007313)),
   MAE for operational readability, pinball loss at τ=0.9 for the
   high-flow tail, and a skill score *against persistence* rather
   than against zero.
5. **Same lead-time grid.** 1, 2, 3, 5, 7-day leads scored
   independently.

Minimum shared harness: one function
`evaluate(model, catchments, window) → DataFrame(lead, metric,
catchment)`, one Parquet of forcings + obs, deterministic seeds.

---

## 6. Candidate first target section

**Selection constraints.** A good first target has (a) an upstream
`sections.gauge_id` that is not NULL, (b) years of observations in
the `observations` hypertable, (c) a paddling user base large enough
that getting the forecast right matters, and (d) a hydrological
regime varied enough to stress-test models (not a slow-response
glacial river with trivial persistence).

**The honest constraint on this list.** Without live DB access, the
exact row counts in `observations` per gauge_id are unknown from the
repo alone. The candidates below are drawn from well-known Norwegian
paddling rivers known to have NVE gauges; the user should confirm
the per-gauge observation span by running the §2.3 query before
committing.

**Candidates.**

1. **Sjoa — Åmot to Nedre Heidal (or similar Sjoa section).** Well-
   instrumented through the NVE-operated Sjoa gauge system. Major
   Norwegian paddling destination; flashy behaviour from snowmelt +
   glacial influence. Exercises melt-regime features.

2. **Rauma Lower.** The nokken-data codebase already singles this
   out: gauge id 51 is hardcoded as a derived series
   (sum/ratio of upstream gauges) in the `update_manual` pipeline
   (`nokken-data/CLAUDE.md:89-96`). Its existence signals a heavily
   trafficked section where a synthesised gauge was worth building.
   Being a derived series is a meaningful feature (not a bug) — a
   forecast model inherits the derivation arithmetic for free.

3. **Driva (or another Trøndelag-region section).** Large paddling
   profile; NVE gauges available; mixed rainfall-runoff and melt
   regime distinct from the Sjoa/Rauma systems.

**Recommendation (for the user to accept, reject, or replace).**
Start with **Sjoa**. It has a large paddling user base, a
well-studied NVE gauge, and a regime (snowmelt + glacial +
orographic precipitation) that forces the model to do real
hydrological work. Rauma Lower is appealing but the derived-series
complication is a confound on a first model. Driva is a strong
alternative if Sjoa observation coverage turns out to be thin.

**This is a proposal, not a decision.** The user closes §6 in §8.

---

## 7. Gap list — split by target repo

Gaps are the backlog items that must be resolved before
nokken-forecasting can produce a production forecast. Split by the
repo that owns each gap.

### 7.1 Data gaps (future nokken-data PRs)

- **MET Norway forecast API.** Daily / 6-hourly weather forecasts
  for 1–7-day horizons. Needed as model input at forecast time.
  Likely landing: a new `sources/met_locationforecast.py` + a
  `forcing_forecast` (or similar) table row per (gauge, issue_time,
  valid_time).
- **Gridded historical forcing (seNorge / NORA3 or equivalent).**
  Historical temperature + precipitation for training and hindcast.
  This is the biggest data-gap item by volume; may land as bulk
  ingestion rather than a cron-cadence fetcher.
- **MET ensemble forcing (AROME-MetCoOp ensemble or ECMWF open
  data).** Needed for probabilistic / quantile forecasts and for
  any Shyft-os comparison that runs ensembles.
- **Basin-aggregated forcing per section.** Interpolation from
  station or grid to basin mean is either a nokken-data
  responsibility (pre-aggregate and write basin-mean series) or a
  nokken-forecasting one (read raw grids, aggregate in-process).
  Decision in §8.

### 7.2 Schema gaps (future nokken-web PRs)

- **Forecast-sink table(s).** No `gauge_estimates` or equivalent
  migration exists in Postgres today (§2.4). Sketch only — the
  final shape is nokken-web's call:
  - Row grain: `(gauge_id, issue_time, valid_time, value_type,
    value, quantile)` or equivalent. Must support multi-lead and
    multi-quantile outputs.
  - Consumer contract: fields nokken-web needs for
    `DESIGN_NOTES.md:119` ("Coming up" bucket).
  - Hypertable or plain — probably hypertable given the volume.
- **Forcing-data tables** if §7.1 writes go to Postgres and not
  to object storage. Schema ownership stays with nokken-web even
  when the writer is nokken-data.
- **A `gauges.location` FK clarification.** The column is INTEGER
  with no declared FK; whether it points at `geo_spots.spot_id` is
  currently ambiguous. Minor cleanup.

### 7.3 Internal gaps (future nokken-forecasting PRs — high level)

- A Postgres read client (asyncpg, matching siblings) and a query
  layer for observations / sections / gauges.
- A baselines module covering §5.1–§5.4 and a hindcast-evaluation
  harness exposing the §5.5 `evaluate(...)` surface.
- A forecast-generation service that runs on a schedule and writes
  through the forecast-sink contract table.
- Operator runbook for the forecast job (systemd service, cron vs.
  APScheduler, observability).
- Phase-4 framework evaluation: Shyft-os install path, cell-vector
  construction, head-to-head hindcast.

---

## 8. Open decisions

Numbered list. Each carries a status tag. Closed decisions point at
the "Decisions (final)" block at the top of this file for the
answer; deferred decisions record why the answer can wait; open
decisions still need the user.

1. **First target section (§6).** Sjoa, Rauma Lower, Driva, or
   something else? Blocks Phase 3. **[closed — see Decisions
   (final).]**
2. **Forecast horizon grid.** 1, 2, 3, 5, 7 days as proposed in
   §5.5, or a different grid (e.g. hourly sub-daily for the first
   24h)? Blocks Phase 3. **[closed — see Decisions (final).]**
3. **Primary error metric.** KGE and NSE, or MAE, or pinball loss
   at a specific τ? What counts as "win"? Blocks Phase 3.
   **[closed — see Decisions (final).]**
4. **Hindcast window length.** ≥5 years as proposed, or longer /
   shorter depending on per-gauge observation span. Blocks Phase 3.
   **[closed — see Decisions (final).]**
5. **Forecast-sink table shape (§2.4, §7.2).** Single table with
   quantile column vs. separate deterministic / probabilistic
   tables; hypertable vs. plain. This is nokken-web's call, but
   nokken-forecasting should propose a shape. Blocks Phase 2 (data
   readiness) and Phase 6 (production job). **[closed — scope
   expanded to three table families (forecasts, historical weather,
   weather forecasts); see Decisions (final).]**
6. **Framework-choice timing.** Commit to either Shyft-os or
   baselines after Phase 4, or run both in parallel production-like
   for a calibration window? Blocks Phase 5. **[deferred — revisit
   at end of Phase 3.]**
7. **`sections.gauge_sub` semantics (§2.2).** "Proxy vs. primary",
   "triggers warning" (per `INVENTORY.md:B`), or something else?
   Whether a `gauge_sub=1` section is a valid forecasting target
   depends on the answer. **[closed for the first target — all six
   Sjoa sections on gauge id 12 carry `gauge_sub=0` (confirmed via
   the PR #2 read-only DB access); the primary linkage reading is
   the working interpretation. Semantics for non-zero values stay
   deferred until a second target section surfaces one.]**
8. **Forcing aggregation ownership (§7.1).** Basin-mean
   interpolation in nokken-data (pre-aggregated tables) or in
   nokken-forecasting (read grids, aggregate in-process)? Blocks
   Phase 2 data-gap PR shape. **[closed — see Decisions (final).]**
9. **Shyft-os local dev path.** Conda channel or from-source build
   on the operator's Mac (no macOS wheels on PyPI per §4.2)? Blocks
   Phase 4 start. **[deferred — revisit at end of Phase 3.]**
10. **Output cadence.** Once daily at a fixed UTC hour, or aligned
    with MET forecast issue times (typically 00Z/06Z/12Z/18Z)?
    Blocks Phase 6. **[deferred — revisit at end of Phase 3.]**
11. **Upstream-gauge auto-discovery via REGINE / vassdrag code.**
    Add a vassdrag / REGINE identifier column to
    `nokken-web.gauges`; sync the NVE station catalogue from HydAPI
    `/Stations` so ghost gauges (not linked to a `section`) are
    present in the table; enumerate upstream-of-Faukstad gauges
    topologically; use as features in Phase 3.5+. Cross-repo:
    nokken-web column + relaxation of the implicit "gauge must link
    to section" assumption; nokken-data new station-catalogue sync
    pipeline; nokken-forecasting consumes. Runs as a parallel track
    to Phase 2, does not gate it. Blocks Phase 3.5 (upstream-gauge
    feature layering); does not block Phase 3 baselines on forcings
    alone. **[open — parallel track to Phase 2.]**
