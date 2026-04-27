# CLAUDE.md — rules for Claude Code in this repo

This file is the Claude-specific counterpart to `ROADMAP.md`. The
roadmap is the source of truth for *what* is being built and in what
order; this file is the source of truth for *how* Claude works inside
this repository. When the two conflict, the roadmap wins and this file
is updated in the same PR that resolves the conflict.

Always read `ROADMAP.md` first, then `docs/scoping-genesis.md` for the
cross-repo inventory, Shyft-os vs. baseline survey, and the open
decisions the user has not yet closed. Do not guess at decisions that
the scoping doc leaves open — stop and ask.

## What this repo is

`nokken-forecasting` produces short-horizon (1–7 day) flow / water-level
forecasts for paddling river sections on [nokken.net](https://nokken.net).
It is one of three sibling repos:

- [`arnefl/nokken-web`](https://github.com/arnefl/nokken-web) — the user-facing
  FastAPI app and the single **owner of the Postgres schema**.
- [`arnefl/nokken-data`](https://github.com/arnefl/nokken-data) — the
  ingestion pipelines that scrape upstream APIs (NVE HydAPI, GLB, and
  in future MET Norway) and write observations / forcing data into the
  shared Postgres database.
- This repo — modelling only. Reads observations + forcing data from
  Postgres; writes forecast outputs back through a contract table
  whose DDL lives in nokken-web.

## Cross-repo ownership boundary (hard rule)

- **Schema changes → `nokken-web` only.** Never write SQL migrations
  in this repo. Never add files under a `db/` or `migrations/`
  directory. If a forecast write path needs a new column or a new
  table, the PR that adds it lands in nokken-web first; the PR here
  bumps any pinned SHA and uses the new column.
- **Fetchers → `nokken-data` only.** Never add scrapers, cron
  pipelines, or APScheduler jobs that talk to upstream providers
  (NVE, GLB, MET, Kartverket, etc.). If a forecast needs a feed
  that is not yet ingested, the PR that lands the fetcher goes in
  nokken-data first; a gap entry lands in `docs/scoping-genesis.md`
  §7 meanwhile.
- **Modelling → here only.** Training code, hindcast harnesses,
  feature pipelines, calibration scripts, forecast-generation
  services, and the systemd unit that runs the forecast job on a
  schedule.
- **No web UI here.** No FastAPI app, no Jinja templates, no static
  assets. The user surface is nokken-web.

When the boundary is ambiguous (e.g., a forcing-data feature store
that could arguably live in nokken-data or here), default to
nokken-data and call it out in the PR.

## Stack summary

- Python 3.12, `uv`-managed. Lockfile committed.
- **Postgres client:** `asyncpg` planned (matches nokken-web and
  nokken-data). No client dep lands until Phase 3 actually reads
  from the DB.
- **Testing:** `pytest` (+ `pytest-asyncio` when async code arrives).
  `ruff` for lint.
- **CI:** GitHub Actions — `uv sync --frozen`, `uv run ruff check`,
  `uv run pytest` on every PR.

No modelling dependencies (scikit-learn, LightGBM, Shyft-os, pandas,
etc.) are committed yet. They land alongside the phase that first
needs them — see `ROADMAP.md`.

## Hard rules

Guardrails that bind every run.

### Forbidden reads

The sibling repos are references, not sources. Read only the minimum
needed for the current task.

- **Read-ok in `nokken-web`:** `CLAUDE.md`, `MIGRATION_PLAN.md`,
  `README.md`, `INVENTORY.md`, `DESIGN_NOTES.md`, `docs/`,
  `db/postgres/migrations/`, and `api/src/nokken/models/` (for row
  shapes when writing forecast-sink adapters).
- **Read-ok in `nokken-data`:** `CLAUDE.md`, `MIGRATION_PLAN.md`,
  `README.md`, `SCHEMA_COMPAT.md`, `docs/scoping.md`,
  `src/nokken_data/sources/` (for upstream-API conventions when
  proposing a new feed), and `src/nokken_data/pipelines/` headers.
- **Never read:** the private legacy repo `arnefl/nokken` under
  `www_old/`, and the legacy script archive on the operator's disk
  (`/Users/arne/Desktop/cron_nokken/`). Both contain inlined
  credentials in their git history.
- **Do not edit sibling repos from this repo's PRs.** If a change is
  needed there, flag it and stop. Coordination happens across two
  PRs, not one.

### Roadmap wins

`ROADMAP.md` is authoritative for what to build and in what order.
`CLAUDE.md` governs only how Claude works inside the repo. When they
disagree, the roadmap wins and `CLAUDE.md` is updated in the same PR
that resolves the conflict.

### Secret hygiene

- All secrets live in environment variables, loaded from a `.env`
  file at the deploy root (e.g. `/srv/nokken-forecasting/.env`,
  chmod 600, outside the repo). `.env.example` in the repo holds
  variable **names** and placeholder values only.
- Variable names mirror nokken-web and nokken-data so a single
  credential set spans all three.
- Never commit real values. Never log env contents, DB DSNs, or
  anything that can carry a secret. Never echo a `.env` value into
  a tool result, sub-agent prompt, commit message, or PR body.
- Legacy `cron_nokken` scripts contain inlined credentials. They
  are never to be read or imported — the transcription in
  `nokken-data/docs/scoping.md` §1 redacts them to role/name only.

#### Local-dev `.env` populated from `nokken-web/.env`

The three repos share one Postgres database, so they share one
credential set. The canonical source for local-dev DB creds is
`nokken-web/.env` (the read-only `nokken_ro` role lives there).
When this repo's local `.env` needs to be populated:

- Copy only the variables this repo's `.env.example` declares —
  today that means `POSTGRES_DSN` and nothing else. Do not copy
  unrelated keys (app secrets, push provider tokens, etc.).
- Refuse to copy a read-write role into this repo while it remains
  a read-only consumer. If `nokken-web/.env`'s `POSTGRES_DSN` is
  not the `nokken_ro` role, stop and flag it rather than narrow
  the role yourself.
- If `../nokken-web/.env` is missing, stop and flag it. Do not
  prompt the operator for credentials interactively and do not
  invent placeholder values.

This convention is new — neither sibling needs it, since they are
the originating sources.

### No commits to `main`

All work lands via PR from a `claude/phase<N>-<short-kebab-desc>`
branch. One task per PR. If a task is trending beyond ~8 files or
~500 added lines, split it and open a follow-up issue.

### When blocked

If a task requires a decision not closed in `ROADMAP.md` or
`docs/scoping-genesis.md` §8, open a **draft** PR whose description
is just the question, and stop. Do not guess. The scoping doc's
"Open decisions" list is the canonical backlog of unresolved
questions — checking new ones off without the user is out of scope.

## Routine workflow

When starting a task:

1. Read `ROADMAP.md` and this file.
2. Check for open PRs on `claude/phase*` branches. If one is open,
   stop and wait — do not start a new task while review is pending.
3. Pick the first unchecked task under the lowest-numbered
   incomplete phase.
4. Branch from a fresh `origin/main` — never from a stale local
   `main`:
   ```
   git fetch origin
   git switch -c claude/phase<N>-<short-kebab-desc> origin/main
   ```
5. Implement the smallest reasonable diff. One task per PR.
6. Tick the task's checkbox in `ROADMAP.md` in the same PR.
7. Run `uv sync`, `uv run ruff check`, `uv run pytest` at the repo
   root. All three must pass. Fix failures yourself; do not leave
   them for review.
8. Push with `-u origin <branch>` and open a PR titled
   `phase <N>: <task summary>` with a brief body: what, why, any
   open questions.

## Where things live

- **Phases, tasks, progress:** `ROADMAP.md`.
- **Scope, inventories, open decisions:** `docs/scoping-genesis.md`.
- **Schema pin + bump protocol:** `SCHEMA_COMPAT.md`.
- **Env-var names:** `.env.example`.
- **Sibling repo pointers:** `README.md`.

## Running tests and linters locally

```
uv sync
uv run ruff check
uv run pytest
```

`pyproject.toml` sets `pythonpath = ["src"]` so imports resolve
without installing the package.

## Inspection CLI

`nokken-forecasting inspect` is a read-only tour of the shared
Postgres schema. It rides the pool from
`nokken_forecasting.db.postgres`, which sets
`default_transaction_read_only = on` at session init — any write
attempt (including CREATE / INSERT / UPDATE / DELETE / TRUNCATE)
raises a Postgres read-only-transaction error regardless of the
role's table privileges. The local-dev DSN copied from
`nokken-web/.env` carries the `nokken_ro` role (per Secret hygiene
above), so writes are blocked twice over; that is deliberate and
must not be softened.

One pool, one DSN. The forecast-sink write path lands in Phase 3
PR 1 on the **same pool** the readers use; the writer
(`src/nokken_forecasting/writers/forecasts.py`) opts into a
read-write transaction by opening `conn.transaction()` and then
issuing `SET TRANSACTION READ WRITE` as the first statement inside
the block. asyncpg's `Transaction` builder only emits `READ ONLY`,
never `READ WRITE`, so the explicit `SET TRANSACTION` is what
actually overrides the session-level read-only default — and only
for the current transaction. The next `BEGIN` on the same
connection inherits the session default again, so adjacent reads
stay defended.

The operator's standing policy is one Postgres role per repo: this
repo's `POSTGRES_DSN` role is `nokken_ro` locally and a single
production role with `INSERT` on `forecasts` granted on prod. There
is no separate writer DSN and no separate writer role. The
`inspect` and `query` subcommands inherit the session-level
read-only invariant unchanged; only the `forecast` group's writer
opens a read-write transaction.

Subcommands — run any of them with `--json` to emit machine-readable
output instead of the default aligned text:

- `nokken-forecasting inspect tables`
  — list public-schema tables and flag Timescale hypertables.
- `nokken-forecasting inspect describe <table>`
  — columns, types, nullability, PK, indexes, hypertable dimensions.
- `nokken-forecasting inspect count <table>`
  — row count plus MIN/MAX of the time column where applicable.
- `nokken-forecasting inspect sample <table> [--limit N] [--where SQL]`
  — sample rows; `--where` is a parameterised WHERE fragment. No
  trailing `;` and no SQL comments — the read-only session is the
  real safety net, `assert_where_safe` is only a cheap early guard.
- `nokken-forecasting inspect query "<SELECT …>"`
  — arbitrary read-only query; the dispatcher rejects anything
  whose first non-comment token is not `SELECT` (case-insensitive).

The `forecast` group runs a baseline and writes its rows into
`forecasts` on the same single pool described above (one Postgres
role per repo; `INSERT` on `forecasts` granted on prod):

- `nokken-forecasting forecast persistence --gauge-id <id>
  [--issue-time <iso>] [--value-type flow|level] [--horizon-hours N]`
  — manual operator path. Last observation held flat for
  `--horizon-hours` (default 168 = 7 days), `model_version =
  'persistence_v1'`, `model_run_at` stamped at write time.
  `--issue-time` defaults to current wall-clock UTC.
- `nokken-forecasting forecast run [--issue-time <iso>]
  [--value-type flow|level] [--horizon-hours N]`
  — unattended scheduled-job path. Iterates every gauge in
  `jobs.forecast_job.FORECAST_GAUGES` (today: just Faukstad,
  `gauge_id = 12`) and writes one tick's rows. Default
  `--issue-time` is now floored to top-of-hour so reruns within an
  hour are no-ops on the writer's deterministic uniqueness key.
  This is the entry point the systemd timer at
  `deploy/nokken-forecasting-forecast.timer` invokes; the operator
  runbook lives at `deploy/README.md`.

Examples:

```
uv run nokken-forecasting inspect tables
uv run nokken-forecasting inspect describe forecasts
uv run nokken-forecasting inspect count observations
uv run nokken-forecasting inspect sample observations --limit 3 \
    --where "gauge_id = 12"
uv run nokken-forecasting inspect query \
    "SELECT value_type, MIN(time), MAX(time), COUNT(*) \
     FROM observations WHERE gauge_id = 12 GROUP BY value_type"
```

Dogfood the CLI rather than dropping into `psql` for one-off reads,
so the read-only guarantee is the single path consulted.
