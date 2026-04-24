# Schema compatibility

This repo is a downstream consumer of the Postgres schema defined and
migrated in the sibling repo [`arnefl/nokken-web`](https://github.com/arnefl/nokken-web).
nokken-web owns the migrations; nokken-forecasting never writes DDL
and never adds files under `db/postgres/migrations/`. See
`docs/scoping-genesis.md` §2 and `ROADMAP.md` Phase 2 for the full
rationale; the same pattern is in use in
[`arnefl/nokken-data`](https://github.com/arnefl/nokken-data/blob/main/SCHEMA_COMPAT.md).

## Pinned nokken-web commit

```
sha    = 4a886011d4087e0c3588f5a4db57cd7bfa2d64de
pinned = 2026-04-24
```

This is the merge commit of
[nokken-web PR #111](https://github.com/arnefl/nokken-web/pull/111)
— "schema: weather tables re-keyed to gauge (008)" — which re-keys
`weather_observations` and `weather_forecasts` from `section_id`
to `gauge_id` and adds a nullable `basin_version INTEGER` audit
column on both tables. Phase 3b's query layer reads gauge-keyed
weather rows directly; `section_id → gauge_id` resolution moves to
read time via a join through `sections`, and `basin_version`
carries forward the catchment polygon version each row was
aggregated under.

The prior pin (`e9f1cf8…`, PR #107, migration 007) introduced the
`basins` table and `basins_current` view — still in effect, and the
`basin_version` column added by 008 references that versioning
scheme. The pin before that (`c18e41a…`, PR #98) introduced the
three hypertables this repo reads from or (in Phase 6) writes to,
and remains in effect:

- `forecasts` — flow / level forecast outputs owned here; multi-lead,
  multi-quantile, multi-model-version.
- `weather_observations` — hourly basin-mean historical forcing
  written by nokken-data; read here as training / hindcast input.
  Re-keyed to `gauge_id` by 008.
- `weather_forecasts` — hourly basin-mean weather forecasts written
  by nokken-data; read here as live-forecast forcing. Re-keyed to
  `gauge_id` by 008.

Earlier still, `c0f0ff7…` (2026-04-22; tracked only in sibling
`nokken-data/SCHEMA_COMPAT.md`, never landed here) predated
migrations 003/004/005.

## Bump protocol

- **Driven by CI failure.** A migration in nokken-web has changed a
  table nokken-forecasting depends on. The read-layer fix and the
  SHA bump land in the **same PR**: update `sha`, update `pinned`,
  adjust the reader, done.
- **Prophylactic sync.** Bumping to a newer nokken-web SHA with no
  code changes is fine — small, docs-only diff, no behaviour change
  expected.
- **Local verification before merging a bump.** Re-run the sequence
  against a local Postgres:
  1. `git fetch` a copy of `arnefl/nokken-web` at the new SHA
     (sparse checkout of `db/postgres/migrations/` is enough).
  2. Apply the migrations in filename order to a throwaway
     Postgres.
  3. Run the inspection CLI from this repo (`nokken-forecasting
     inspect describe <table>`) against that DB to confirm the
     tables this repo depends on still look right.
  4. `uv run pytest` against that DB.

## CI wiring

No CI job currently validates the pin against nokken-web's head.
Adding one mirroring nokken-data's
`.github/workflows/ci.yml` integration job (sparse-checkout nokken-web
at the pinned SHA, apply migrations to a Postgres service container,
run `tests/integration`) is a natural follow-up once:

- a `NOKKEN_WEB_TOKEN` secret is configured on this repo
  (fine-grained PAT, Contents: Read on `arnefl/nokken-web`), and
- the integration test suite here has grown enough to justify it —
  today it's the single Phase-1 smoke test plus the Phase-2 inspect
  tests.

Until then, the pin is validated by the operator's local runs against
`nessie` (which carries nokken-web's migrations applied in
production).

History lives in git; no changelog file in this repo.
