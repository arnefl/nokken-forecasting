# nokken-forecasting deploy runbook

Operator-facing reference for installing, verifying, and operating the
scheduled forecast job on the production host. Mirrors the location and
shape of [`nokken-data/deploy/README.md`](https://github.com/arnefl/nokken-data/blob/main/deploy/README.md).

The unit deployed here is a **systemd `oneshot` service + timer** —
not an APScheduler-in-service like nokken-data uses. See "Why systemd
timer here, APScheduler in nokken-data?" at the bottom for the
rationale; the asymmetry is intentional.

## Prerequisites

- Linux host with systemd (the `nessie` VM today). Postgres reachable
  from the host either locally or over the private network.
- `uv` installed at `/usr/local/bin/uv` (matches the `ExecStart=`
  path in `nokken-forecasting-forecast.service`). Adjust the unit if
  yours differs.
- The shared `nokken_ro` role on `nessie` granted `INSERT` on
  `forecasts` so the writer can land rows. Locally this role stays
  read-only — production gets the grant; the session-level
  `default_transaction_read_only = on` invariant defends every read
  path either way (see `CLAUDE.md` "Inspection CLI").
- A `nokken` system user (shared with nokken-data; create only if the
  host doesn't already have it):
  ```
  sudo useradd --system --home /srv/nokken-forecasting \
      --shell /usr/sbin/nologin nokken
  ```

## Install

1. **Clone or update the repo at the deploy root.**
   ```
   sudo install -d -o nokken -g nokken /srv/nokken-forecasting
   sudo -u nokken git clone https://github.com/arnefl/nokken-forecasting.git \
       /srv/nokken-forecasting
   sudo -u nokken bash -c 'cd /srv/nokken-forecasting && uv sync --frozen'
   ```

2. **Create the `.env` file.**
   ```
   sudo install -m 600 -o nokken -g nokken /dev/null /srv/nokken-forecasting/.env
   sudo -u nokken vi /srv/nokken-forecasting/.env
   ```
   Populate the variable names listed in `.env.example` — currently
   `POSTGRES_DSN` and `LOG_LEVEL`. The DSN's role must carry `INSERT`
   on `forecasts`.

3. **Install the systemd units.**
   ```
   sudo install -m 644 deploy/nokken-forecasting-forecast.service \
       /etc/systemd/system/
   sudo install -m 644 deploy/nokken-forecasting-forecast.timer \
       /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now nokken-forecasting-forecast.timer
   ```

4. **Confirm the timer is armed.**
   ```
   systemctl list-timers nokken-forecasting-forecast.timer
   ```
   Expect a `NEXT` time at the upcoming `00:00 UTC` boundary and a
   `LEFT` countdown.

## Verify

After at least one tick has fired (or run one immediately with
`sudo systemctl start nokken-forecasting-forecast.service`):

```sql
-- Latest tick's rows: 168 hourly leads for each gauge in FORECAST_GAUGES.
SELECT gauge_id,
       model_version,
       MIN(model_run_at) AS first_written,
       MAX(model_run_at) AS last_written,
       COUNT(*)          AS rows
FROM   forecasts
WHERE  model_run_at > NOW() - INTERVAL '2 hours'
GROUP  BY gauge_id, model_version
ORDER  BY gauge_id;
```

Healthy after N ticks (N ≥ 2):

- One distinct `model_run_at` per tick per gauge — every gauge in a
  tick shares the same `model_run_at` because the job stamps it once
  at start.
- 168 rows per `(gauge_id, model_version, model_run_at)` group.
- `issue_time` advances by 24 h between ticks; `valid_time` covers
  `issue_time + 1h … issue_time + 168h`.
- A second tick run inside the same hour writes 0 new rows
  (idempotency on the deterministic `(issue_time, valid_time, gauge_id,
  value_type, model_version)` uniqueness key) — `model_run_at` of the
  original rows is preserved.

## Logs

Log destination: systemd-journald via `SyslogIdentifier=nokken-forecasting`.
Format: one JSON object per line (see
`src/nokken_forecasting/logging.py`).

Tail live:
```
journalctl -t nokken-forecasting -f -o cat | jq .
```
(`-o cat` strips the journald prefix so each line is parseable JSON.)

Greppable events:

- `event=forecast_job.start` — one per tick. Carries `issue_time`,
  `gauges`, `value_type`, `horizon_hours`.
- `event=forecast_job.gauge` — one per gauge per tick.
  `status=success|error`. On success carries `rows_written`. On
  error carries `error` (truncated `repr` of the exception).
- `event=forecast_job.done` — the summary line. Greppable as
  `journalctl -t nokken-forecasting | grep forecast_job.done`. Carries
  `total`, `succeeded`, `failed`, `rows_written`, `exit_code`.

The summary line is the operator's fast-path health check.

## Common failure modes

| Symptom | Diagnosis | Fix |
|---|---|---|
| `cannot execute INSERT in a read-only transaction` (SQLSTATE 25006) | Writer lost its `SET TRANSACTION READ WRITE` opt-out — likely a refactor that replaced the explicit SQL with `conn.transaction(readonly=False)`. asyncpg only emits `READ ONLY`, never `READ WRITE`. | Restore the explicit `SET TRANSACTION READ WRITE` in `writers/forecasts.py`. Covered by `tests/integration/writers/test_forecasts.py::test_writer_overrides_session_read_only_default`. |
| `permission denied for table forecasts` | The role on `POSTGRES_DSN` lacks `INSERT` on `forecasts`. | `GRANT INSERT ON forecasts TO <role>` on `nessie`. The session-level read-only invariant is still in force; the role grant is the second layer. |
| `no observations available for gauge N at or before <ts>` | The gauge has no `value_type=flow` rows in the 7-day lookback ending at the tick's `issue_time`. | Verify nokken-data's NVE flow scraper is running. If only this gauge is affected, drop it from `FORECAST_GAUGES` until the upstream feed comes back. |
| `gauge_id … violates foreign key constraint` | `FORECAST_GAUGES` lists a `gauge_id` not present in the `gauges` table. | Reconcile `FORECAST_GAUGES` against `SELECT gauge_id FROM gauges` on the deploy DB. |
| Timer fires but the unit immediately exits non-zero | Per-gauge errors logged as `event=forecast_job.gauge status=error`; if every gauge errored the job exits 1. | Check the per-gauge log lines for the underlying `error` field; fix and either wait for the next tick or `systemctl start` the unit manually to retry. |
| Catch-up tick after host downtime writes "wrong" `issue_time` | `Persistent=true` fires the missed tick as soon as the timer is back. The job's `issue_time` defaults to wall-clock now floored to the hour, so the catch-up rows reflect the catch-up hour, not the missed midnight. | Working as intended — the audit trail (`model_run_at` stamp) makes the gap visible. If the gap matters, replay manually with `nokken-forecasting forecast run --issue-time <iso>`. |

## Pause / resume

Disable the timer (e.g. during a DB migration or upstream-feed
outage):

```
sudo systemctl disable --now nokken-forecasting-forecast.timer
```

Resume:

```
sudo systemctl enable --now nokken-forecasting-forecast.timer
```

Disabling the timer leaves the service unit installed; the next
`enable --now` immediately arms the next-`00:00 UTC` schedule, and
`Persistent=true` does **not** retroactively fire a tick for the
disabled period (only timer-down windows that systemd considers
"missed" while the timer was *enabled* are caught up).

## Why systemd timer here, APScheduler in nokken-data?

nokken-data runs six pipelines on five interleaved cadences (every 10
minutes, with second-offsets) inside a single long-running service —
`max_instances=1` plus `coalesce=True` per-job is the right shape for
that workload, and the scheduler module is the natural home for it.

nokken-forecasting runs **one** job on **one** cadence (daily). A
systemd timer is the lighter fit: one-shot per tick, no leaked process
state, no APScheduler dependency, and `Persistent=true` already
delivers the catch-up behaviour APScheduler's `misfire_grace_time`
provides on the other side.

The two repos therefore deploy as different runtime models on
purpose. They share the operationally-visible style — `User=nokken`,
`EnvironmentFile=/srv/<repo>/.env`, journald with a `SyslogIdentifier`
matching the repo name, JSON-line logs — so the operator's muscle
memory transfers; the scheduling primitive is what differs.

Reopening this if a second forecast cadence lands (e.g. per-NWP-cycle
4×/day) is fine — at two cadences the choice is still a coin-flip; at
three or more, switch to APScheduler-in-service to recoup the shared
misfire / coalesce policy.
