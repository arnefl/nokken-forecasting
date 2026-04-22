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

Phase 1 (this PR) is scoping and scaffolding only — no model code, no
data access, no dependencies on the database. The first substantive
modelling work lands in Phase 3.
