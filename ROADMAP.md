# Roadmap — nokken-forecasting

Authoritative phase plan for building this repo. Mirrors the
structure of `nokken-web/MIGRATION_PLAN.md` and
`nokken-data/MIGRATION_PLAN.md`; tick checkboxes here in the same PR
that ships each item. `docs/scoping-genesis.md` stays as archaeology
after this file lands — the same pattern nokken-data follows.

Cross-repo dependencies on `arnefl/nokken-web` and
`arnefl/nokken-data` are marked ⇆.

## Phase 1 — Research & scoping (this PR)

**Goal.** Establish the repo, the cross-repo ownership boundary, and
the backlog of decisions the user must close before modelling can
start. No model code, no data access, no dependencies on the shared
database.

**Entry criteria.** Sibling repos (`nokken-web`, `nokken-data`) are
checked out alongside so inventories in the scoping doc can cite
concrete files.

**Exit criteria.**
- `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`,
  `CLAUDE.md`, this file, `docs/scoping-genesis.md`, and minimal
  test scaffolding landed on `main`.
- `uv run ruff check` and `uv run pytest` pass locally and in CI.
- `docs/scoping-genesis.md` §8 "Open decisions" lists every
  question the user must answer before Phase 3 can start.

**Repos touched.** `nokken-forecasting` only.

**Checklist.**

- [ ] Repo scaffolding — `README.md`, `CLAUDE.md`, `pyproject.toml`,
      `.env.example`, `.gitignore`, `src/nokken_forecasting/`
      package stub, `tests/test_smoke.py`, GitHub Actions CI.
- [ ] `docs/scoping-genesis.md` with §1–§8 filled in and every
      factual claim cited.
- [ ] `ROADMAP.md` (this file).
- [ ] `uv.lock` committed.

---

## Phase 2 — Data readiness

**Goal.** Close the data gaps
(`docs/scoping-genesis.md` §7.1) and schema gaps (§7.2) so that a
modeller has inputs, a forecast-sink table, and a first target
section committed. Landed via coordinated PRs in `nokken-data` and
`nokken-web` — this repo contributes a `SCHEMA_COMPAT.md`-style pin
once the sink schema is defined.

**Entry criteria.**
- Open decisions 1 (first section), 5 (sink shape), 8 (forcing
  aggregation ownership) are closed in
  `docs/scoping-genesis.md` §8.

**Exit criteria.**
- Forecast-sink migration exists in `nokken-web/db/postgres/migrations/`
  and is applied in production. ⇆ nokken-web
- The forcing feeds flagged as "needed" for the first target
  section in `docs/scoping-genesis.md` §7.1 are ingesting into
  Postgres (or written to an agreed alternative store). ⇆
  nokken-data
- This repo pins nokken-web at the SHA that introduces the sink
  migration, via a `SCHEMA_COMPAT.md` mirroring nokken-data's
  pattern.
- A query layer in `src/nokken_forecasting/` can read observations,
  gauges, and sections from the DB against test fixtures. No model
  code yet.

**Repos touched.** `nokken-web` (migration + pydantic model),
`nokken-data` (ingestion), `nokken-forecasting` (read client +
SCHEMA_COMPAT).

---

## Phase 3 — Baseline modelling on the first target section

**Goal.** Stand up the four baselines from
`docs/scoping-genesis.md` §5 (persistence, recession-curve, linear
regression, gradient-boosted trees) on the agreed first target
section, with the shared evaluation harness from §5.5 producing
reproducible hindcast numbers.

**Entry criteria.**
- Phase 2 exit criteria met: forcing data ingesting; forecast-sink
  table exists; SCHEMA_COMPAT pinned.
- Open decisions 2 (horizon grid), 3 (primary metric), 4 (hindcast
  window) are closed.

**Exit criteria.**
- Each baseline has a reproducible training script, a pickled /
  serialised fitted artefact, and a hindcast DataFrame over the
  agreed window.
- `evaluate(model, catchments, window) → DataFrame(lead, metric,
  catchment)` implemented and exercised by all four baselines.
- A short report in `docs/` comparing the four on the agreed
  primary metric. No claims about Shyft-os yet; the report is
  baselines-only.
- Modelling dependencies (scikit-learn, LightGBM, pandas,
  statsmodels, …) committed in `pyproject.toml`.

**Repos touched.** `nokken-forecasting` only.

---

## Phase 4 — Framework evaluation (Shyft-os vs. best baseline)

**Goal.** A genuine side-by-side hindcast of Shyft-os against the
best Phase 3 baseline on the same section, same hindcast window,
same metrics. Produces a one-page recommendation: adopt Shyft-os,
stay on baselines, or keep both.

**Entry criteria.**
- Phase 3 exit criteria met: baselines stood up, harness exists,
  one clear best-baseline picked.
- Open decision 9 (Shyft-os local dev path) closed.

**Exit criteria.**
- Shyft-os installable on CI and on the operator's machine via the
  decided path (conda vs. from-source).
- A cell vector exists for the first target section's catchment
  (built from NVE polygons + DEM + land-cover), stored as a
  reproducible artefact.
- Shyft-os hindcast numbers land in the same
  `evaluate(...)`-produced DataFrame as the baselines.
- Recommendation write-up in `docs/` with the numbers; user
  accepts or rejects it (closes open decision 6).

**Repos touched.** `nokken-forecasting` only (Shyft-os is a
Python dependency, not a sibling repo).

---

## Phase 5 — Calibration & hindcast validation at chosen framework

**Goal.** Whichever framework Phase 4 picks, calibrate it properly
across the full set of first-tier paddling sections, with hindcast
validation that the operator trusts enough to run in production.

**Entry criteria.**
- Open decision 6 (framework choice) closed.
- The set of sections expanding beyond Phase 3's first section is
  agreed with the user (may require closing further open
  decisions not yet in §8).

**Exit criteria.**
- Per-section calibrated model artefacts checked in (or a
  reproducible training pipeline that rebuilds them).
- Hindcast validation passes an agreed skill threshold on
  held-out years across every first-tier section.
- Failure modes documented: which sections / regimes the model
  under-performs on, and what triggers a retrain.

**Repos touched.** `nokken-forecasting` only.

---

## Phase 6 — Production forecast job

**Goal.** A scheduled forecast job that runs in production, reads
latest observations + forcing forecasts from Postgres, runs the
Phase 5 model stack, and writes outputs to the forecast-sink table
via the contract defined in Phase 2. nokken-web's "Coming up"
bucket lights up for the covered sections.

**Entry criteria.**
- Phase 5 exit criteria met.
- Open decision 10 (output cadence) closed.

**Exit criteria.**
- `src/nokken_forecasting/pipelines/forecast_job.py` (or equivalent)
  implemented; runnable via `uv run nokken-forecasting run
  forecast_job` (CLI pattern mirroring nokken-data).
- Systemd service deployed on the same VM as nokken-data (or a
  dedicated host if the operator decides; reopen as an open
  decision if so).
- Outputs are written through the sink contract and visible in
  nokken-web's "Coming up" bucket. ⇆ nokken-web for any shape
  adjustments discovered at integration time.
- Operator runbook in `docs/deploy.md` covering start / stop /
  retrain / incident response.

**Repos touched.** `nokken-forecasting` (code + deploy unit);
`nokken-web` only if the sink contract needs adjustment.
