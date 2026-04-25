# Query layer — Phase 3b

Read-only library that backs the Phase 3 baselines and (in Phase 6)
the production forecast job. Lives in
[`src/nokken_forecasting/queries/`](../src/nokken_forecasting/queries/).
Exposes typed `pandas.DataFrame` readers over the five tables
modelling code consumes; details on the per-reader column schema sit
in the per-module docstring.

## Two-layer plan

The query layer is intentionally split in two, with the upper layer
deferred to Phase 4.

**Lower layer — this PR.** SQL-natural shape, one row per DB row.
Column names match the source table (`time` / `valid_time`,
`gauge_id`, `variable`, `value`, `source`, `quantile`,
`basin_version`). Strictly typed: `time` columns are `datetime64[ns,
UTC]`, `value` is `float64`, nullable integers are `Int64`, nullable
floats stay `float64` with `NaN`. Empty results still carry the
documented columns.

**Upper layer — Phase 4.** Shyft-shaped per-variable per-point /
per-basin time series for `geo_point_source`. Building this transform
before exercising Shyft's installation and runtime constraints risks
fixing subtle CRS / tz / dtype / NaN choices wrong; nothing in the
lower layer here forecloses that work, and Phase 4 owns the design
once Shyft's actual API surface is in front of us.

## Connection handling

Readers take an injected `asyncpg.Connection` and never construct
their own — testable by passing a mock; production callers reach the
shared read-only pool through `connect()` (a thin `async with`
wrapper over `nokken_forecasting.db.postgres.get_pool`). The
read-only role used by the `inspect` CLI works for every read here.

## Time semantics

The Postgres tables use `TIMESTAMP WITHOUT TIME ZONE` for every time
column; the convention across all three sibling repos is to interpret
stored values as naive UTC. Readers accept tz-aware UTC
`pandas.Timestamp` inputs (naive inputs raise a `ValueError`) and
emit tz-aware UTC `Timestamp` columns. Time windows are half-open
`[start, end)`.

## Forecast latest-as-of vs at-lead

Both readers handle the "multiple sources at the same gauge" shape
the schema permits:

- `get_weather_forecast_latest_as_of` returns `MAX(issue_time) ≤ as_of`
  **per source**, so two sources with different latest issue cycles
  appear together in the result, distinguishable by `issue_time` /
  `source`.
- `get_weather_forecast_at_lead` picks `MAX(issue_time) ≤ target_time
  − lead_hours` per source, then keeps rows whose `valid_time` falls
  within `±tolerance_hours` of `target_time`. Used for hindcast skill
  evaluation: "what did we predict at lead L for time T?"

## CLI

A `query` sub-app on `nokken-forecasting` exposes the readers for
operator spot-checks against `nessie` after a backfill. Output
mirrors `inspect`'s aligned-text default with `--json` for NDJSON.

```
nokken-forecasting query gauges
nokken-forecasting query observations --gauge-id 12 \
    --start 2020-01-01 --end 2020-02-01
nokken-forecasting query weather-observations --gauge-id 12 \
    --start 2025-04-01 --end 2025-05-01
nokken-forecasting query weather-forecast-latest --gauge-id 12 \
    --as-of now --horizon 168
nokken-forecasting query weather-forecast-at-lead --gauge-id 12 \
    --target-time 2025-04-02T12:00 --lead-hours 24
```

The CLI is a spot-check tool; modelling code calls the readers
directly and works on the returned DataFrames.
