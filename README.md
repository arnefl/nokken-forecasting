# nokken-forecasting

Short-horizon river flow / water-level forecasting for
[nokken.net](https://nokken.net) paddling sections. Third repo in a
three-repo split:

- [`arnefl/nokken-web`](https://github.com/arnefl/nokken-web) — user-facing
  FastAPI app. **Owns the Postgres schema.**
- [`arnefl/nokken-data`](https://github.com/arnefl/nokken-data) — ingestion
  pipelines. Writes observations (and, later, forcing data) into the
  shared Postgres database.
- `nokken-forecasting` (this repo) — modelling. Reads observations and
  forcing data from the shared Postgres, writes forecast outputs back
  via a forecast-sink contract owned by nokken-web.

No web UI here. No fetchers here. See the
[scoping doc](./docs/scoping-genesis.md) for the cross-repo
ownership boundary and the current data / schema gaps; see
[`ROADMAP.md`](./ROADMAP.md) for the phased plan.

- **Scope + decisions:** [`docs/scoping-genesis.md`](./docs/scoping-genesis.md).
- **Phases + progress:** [`ROADMAP.md`](./ROADMAP.md).
- **Rules for Claude Code:** [`CLAUDE.md`](./CLAUDE.md).

## Quickstart

```
uv sync
uv run pytest
uv run ruff check
```

### Local DB credentials

This repo reads from the same Postgres `nessie` database as nokken-web
and nokken-data. The canonical source for local-dev DB credentials is
the sibling `nokken-web/.env` (read-only `nokken_ro` role). To exercise
DB-backed code locally, populate `nokken-forecasting/.env` (gitignored)
with the `POSTGRES_DSN` value from `nokken-web/.env`. Variable names
match across the three repos. The integration smoke test
(`tests/integration/test_db_smoke.py`) skips cleanly when the env var
is not set, so `uv run pytest` works without a DB on hand.

Phase 1 onward is incremental — scoping landed first, read-only DB
access lands here, and the first substantive modelling work lands in
Phase 3 (see [`ROADMAP.md`](./ROADMAP.md)).
