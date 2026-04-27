# nokken-forecasting — operator deploy runbook

Linear script for installing the daily forecast tick on the same VM
that runs `nokken-data-scheduler`. Read top-to-bottom the first time;
reach for the Debug loop section on follow-up visits. Mirrors the
shape of [`nokken-data/deploy/README.md`](https://github.com/arnefl/nokken-data/blob/main/deploy/README.md)
so the operator's muscle memory transfers between the two repos.

The unit shipped here is a **systemd `oneshot` service + timer** — not
an APScheduler-in-service like nokken-data uses. See "Why systemd
timer here, APScheduler in nokken-data?" at the bottom for the
rationale; the asymmetry is intentional.

§§ 1–8 cover the **live** forecast pipeline — the daily tick the
systemd timer drives. § 9 covers the **hindcast** workflow — the
operator-driven replay path that lands historical-issue-time rows
into the same `forecasts` sink. Hindcasts are not scheduled; they
run on demand from the operator's shell.

## Prerequisites

- Same Ubuntu VM that runs `nokken-data-scheduler`. Network reachability
  to nokken-web's `nessie` Postgres is the only external dependency
  here — no upstream APIs are called from this repo.
- `uv` installed **system-wide at `/usr/local/bin/uv`**. The systemd
  unit hardcodes that path, so a per-user install in `~/.local/bin`
  will fail under the `nokken` service user with
  `status=203/EXEC: Failed to locate executable`. Verify:
  `/usr/local/bin/uv --version`. If the nokken-data deploy already
  satisfied this prerequisite, skip the install. Otherwise:

  ```
  curl -LsSf https://astral.sh/uv/install.sh \
    | sudo env UV_INSTALL_DIR=/usr/local UV_NO_MODIFY_PATH=1 sh
  ```

  (The installer writes to `$UV_INSTALL_DIR/bin/uv`, so `/usr/local`
  — not `/usr/local/bin` — is the right value. `UV_NO_MODIFY_PATH=1`
  stops the installer from editing root's shell profile.)
- `gh` (GitHub CLI) installed and on `$PATH`. Verify: `gh --version`.
  If absent, install via the apt incantation in
  [`nokken-data/deploy/README.md` §1](https://github.com/arnefl/nokken-data/blob/main/deploy/README.md#1-install-the-application).
  Needed to clone this repo in §1.
- The shared `nokken` system user already exists from the nokken-data
  install. **Do not re-create it.** If for some reason this VM does
  not yet have it (clean host):
  ```
  sudo useradd --system --home /srv/nokken-data \
      --shell /usr/sbin/nologin nokken
  ```
  We deliberately give `nokken` a single home (`/srv/nokken-data`) and
  not a per-repo home, so uv's per-user interpreter cache lands in one
  place across all sibling repos.
- Postgres role on `POSTGRES_DSN` granted `INSERT` on `forecasts`.
  Locally this role is `nokken_ro` (read-only); on the production VM
  the operator grants `INSERT` on `forecasts` to the same role —
  there is no separate writer role. The session-level
  `default_transaction_read_only = on` invariant defends every read
  path either way (see `CLAUDE.md` "Inspection CLI").

## 1. Install the application

If `gh` is not already authenticated as the operator from the
nokken-data deploy, run:

```
gh auth login
```

Clone the repo as yourself, then move it into place. Cloning as the
operator picks up the credentials from `gh auth login`; `sudo mv`
seats the tree at `/srv/nokken-forecasting`; the `chown` after it
ensures the operator owns the tree so `uv sync` runs as yourself —
§3 hands ownership to `nokken` later.

```
sudo mkdir -p /srv
gh repo clone arnefl/nokken-forecasting /tmp/nokken-forecasting-clone
sudo mv /tmp/nokken-forecasting-clone /srv/nokken-forecasting
sudo chown -R "$USER:$USER" /srv/nokken-forecasting
```

Sync Python deps (no rasterio / GDAL build prerequisites here — this
repo's deps are pure-Python):

```
cd /srv/nokken-forecasting
uv sync --frozen
```

Running `uv sync` as yourself (not `sudo uv sync`) matters: uv's
managed Python interpreter lands under `$HOME/.local/share/uv/python`,
and if root creates the venv its `python` symlink targets
`/root/.local/share/uv/...` — which `nokken` can't traverse, and the
service then crashes with `Failed to query Python interpreter:
failed to canonicalize path`. §3 deletes and re-creates the venv under
`nokken` to put the interpreter somewhere the service user can reach.

Create the `.env` file at `/srv/nokken-forecasting/.env`, mode 600,
owned by **you (the operator)** for now so §2 can source it without
`sudo`. §3 hands ownership to the service user once the unit is
installed. Copy the placeholder structure from
`/srv/nokken-forecasting/.env.example`:

| Variable | Meaning |
| --- | --- |
| `POSTGRES_DSN` | asyncpg DSN for nokken-web's `nessie` Postgres. Role on production must carry `INSERT` on `forecasts`. |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default `INFO`. |

```
sudo chown "$USER:$USER" /srv/nokken-forecasting/.env
chmod 600 /srv/nokken-forecasting/.env
# §3 re-chowns this file to the service user once the unit is installed.
```

## 2. First run — manual verification

Before installing the systemd unit, run the forecast tick by hand to
confirm DNS, credentials, and DB write reachability from this VM. The
tick is idempotent on the writer's deterministic uniqueness key, so
re-running inside the same hour writes zero rows.

`nokken-forecasting` reads configuration from process environment
variables only — it does **not** auto-load `.env`. In production the
systemd unit supplies env via
`EnvironmentFile=/srv/nokken-forecasting/.env` (see §3); for this
interactive run we source the same file into the shell first. Run as
yourself — §1 left `.env` operator-owned precisely so you can do this
without `sudo`:

```
cd /srv/nokken-forecasting
set -a; . /srv/nokken-forecasting/.env; set +a
uv run nokken-forecasting forecast run
```

(`set -a` auto-exports each assignment the sourced file makes; `set +a`
restores normal behaviour before `uv run` launches.)

**What success looks like.** The CLI prints structured JSON log lines
to stdout. A healthy run starts with an `event=forecast_job.start`
line listing the gauges, emits one `event=forecast_job.gauge`
`status=success` line per gauge with `rows_written` set, and ends with
`event=forecast_job.done` carrying `succeeded` / `failed` /
`rows_written` / `exit_code=0`. Today's `FORECAST_GAUGES = (12,)`
yields one gauge line and `rows_written=168` on the first tick.

**If the run fails with `ConnectionRefusedError: ... 5432`** and the
host in your `POSTGRES_DSN` is correct, the most common cause is that
the env file was not loaded — `asyncpg` then falls back to the
placeholder `localhost:5432` default in `src/nokken_forecasting/config.py`.
Re-run the `set -a; . /srv/nokken-forecasting/.env; set +a` block
first, in the same shell. (Same gotcha as nokken-data; same fix.)

**If the run fails with `cannot execute INSERT in a read-only
transaction`** (SQLSTATE 25006), the role on `POSTGRES_DSN` lacks
`INSERT` on `forecasts`, or the `SET TRANSACTION READ WRITE` opt-out
in `writers/forecasts.py` was lost in a refactor. The latter is
covered by
`tests/integration/writers/test_forecasts.py::test_writer_overrides_session_read_only_default`
— a regression there reproduces this failure mode.

Spot-check the rows landed:

```
uv run nokken-forecasting inspect query \
    "SELECT gauge_id, model_version, COUNT(*) AS rows, \
            MIN(model_run_at) AS run_at \
     FROM forecasts WHERE model_run_at > NOW() - INTERVAL '1 hour' \
     GROUP BY gauge_id, model_version"
```

Expect one row per `(gauge_id, model_version)` with `rows = 168` and
a single `run_at` matching the wall-clock instant you ran the tick.

## 3. Install the systemd service + timer

The service runs under `User=nokken`/`Group=nokken` (shared with
`nokken-data-scheduler`); do not run the timer as root. The
`chown -R` below hands the deploy root — including the `.env` file
§1 left operator-owned — to `nokken`, so `EnvironmentFile=` in the
unit can read it.

```
sudo chown -R nokken:nokken /srv/nokken-forecasting
```

The venv §1 created points its `python` symlink at an interpreter
under the operator's `$HOME/.local/share/uv/python/...`, which `nokken`
cannot traverse. Delete and re-sync as `nokken` so the interpreter
lands under the service user's home (`/srv/nokken-data`) instead:

```
sudo rm -rf /srv/nokken-forecasting/.venv
sudo -u nokken -H bash -c \
    'cd /srv/nokken-forecasting && /usr/local/bin/uv sync --frozen'
```

Skipping this step produces `error: Failed to query Python interpreter
… failed to canonicalize path` the first time the unit fires.

Install the service + timer units:

```
sudo cp /srv/nokken-forecasting/deploy/nokken-forecasting-forecast.service \
    /etc/systemd/system/nokken-forecasting-forecast.service
sudo cp /srv/nokken-forecasting/deploy/nokken-forecasting-forecast.timer \
    /etc/systemd/system/nokken-forecasting-forecast.timer
sudo systemctl daemon-reload
```

The unit is now installed but the timer is intentionally **not**
enabled and not started — the cutover happens in §4.

**Operator-verify before enabling.** Confirm `/usr/local/bin/uv`
exists and is executable for the service user:

```
ls -l /usr/local/bin/uv
sudo -u nokken /usr/local/bin/uv --version
```

A missing or non-executable `uv` is what `status=203/EXEC: Failed to
locate executable` in the journal means.

## 4. Cutover: enable the timer

Run the service once manually under the unit's identity to verify the
real-deploy ownership / env / role grants are wired correctly before
arming the schedule:

```
sudo systemctl start nokken-forecasting-forecast.service
sudo systemctl status nokken-forecasting-forecast.service
sudo journalctl -u nokken-forecasting-forecast.service -n 50 -o cat
```

Expect `Active: inactive (dead)` with `status=0/SUCCESS` (oneshot
exits when its work is done) and the JSON-line forecast_job.* events
in the journal. Re-run the SQL spot-check from §2 to confirm rows
landed.

If that's clean, arm the timer:

```
sudo systemctl enable --now nokken-forecasting-forecast.timer
systemctl list-timers nokken-forecasting-forecast.timer
```

`list-timers` shows a `NEXT` time at the upcoming `00:00 UTC` boundary
and a `LEFT` countdown.

## 5. Verify

After the next `00:00 UTC` tick:

```
sudo systemctl list-timers nokken-forecasting-forecast.timer
sudo systemctl status nokken-forecasting-forecast.service
sudo journalctl -u nokken-forecasting-forecast.service \
    --since "1 hour ago" -o cat
```

A clean tick produces three event types in the journal:

```
{"event": "forecast_job.start", "issue_time": "2026-04-28T00:00:00+00:00", "gauges": [12], ...}
{"event": "forecast_job.gauge", "gauge_id": 12, "status": "success", "rows_written": 168, ...}
{"event": "forecast_job.done", "total": 1, "succeeded": 1, "failed": 0, "rows_written": 168, "exit_code": 0, ...}
```

SQL health check after N ≥ 2 ticks:

```
uv run nokken-forecasting inspect query \
    "SELECT gauge_id, model_version, \
            COUNT(DISTINCT model_run_at) AS ticks, \
            COUNT(*) AS rows, \
            MIN(model_run_at) AS first_run_at, \
            MAX(model_run_at) AS last_run_at \
     FROM forecasts \
     WHERE model_run_at > NOW() - INTERVAL '7 days' \
     GROUP BY gauge_id, model_version"
```

Healthy:

- `ticks` increments by 1 per day per gauge.
- `rows = ticks * 168` (one full 7-day horizon per tick per gauge).
- `last_run_at - first_run_at ≈ 24h * (ticks - 1)`.
- A second `systemctl start` inside the same hour writes 0 new rows
  (idempotency on the deterministic
  `(issue_time, valid_time, gauge_id, value_type, model_version)`
  uniqueness key); `model_run_at` of the original rows is preserved.

## 6. Apply an update

§1 cloned the repo with the operator's `gh` auth and §3 handed the
tree to `nokken:nokken`. After that hand-off, the operator can no
longer `git pull` (ownership denies writes) and `sudo -u nokken git
pull` fails because `nokken` has no shell and no git credentials.

If you already did the group-membership setup for nokken-data, the
`nokken` group is in your shell's group list and the steps below "Just
Work". If this is the first sibling repo deployed to this VM, the
nokken-data §6 one-time setup applies here unchanged — the tree at
`/srv/nokken-forecasting` needs the same `chown -R`/`chmod -R g+rwX`/
`g+s` setgid bit/`git config --global --add safe.directory` treatment.
See [`nokken-data/deploy/README.md` §6](https://github.com/arnefl/nokken-data/blob/main/deploy/README.md#6-apply-an-update)
for the verbatim commands; substitute `/srv/nokken-forecasting` for
`/srv/nokken-data`.

Every subsequent update:

```
cd /srv/nokken-forecasting
git pull --ff-only
sudo -u nokken -H bash -c \
    'cd /srv/nokken-forecasting && /usr/local/bin/uv sync --frozen'
sudo systemctl daemon-reload   # picks up unit-file edits, harmless otherwise
```

`uv sync` stays sudo'd as `nokken` for the same reason §3 did: uv's
interpreter cache must land under `nokken`'s `$HOME`, not the
operator's, or the service crashes with `Failed to query Python
interpreter`. No `systemctl restart` is needed — the unit is a
oneshot, and the next timer tick picks up the new code automatically.
If you want to validate the update immediately:

```
sudo systemctl start nokken-forecasting-forecast.service
sudo journalctl -u nokken-forecasting-forecast.service -n 50 -o cat
```

## 7. Pause / resume / rollback

Pause (e.g. during a DB migration or upstream-feed outage):

```
sudo systemctl disable --now nokken-forecasting-forecast.timer
```

Resume:

```
sudo systemctl enable --now nokken-forecasting-forecast.timer
```

`Persistent=true` does **not** retroactively fire ticks for the
disabled period — only timer-down windows that systemd considers
"missed" while the timer was *enabled* are caught up.

Rollback to the previous repo state:

```
cd /srv/nokken-forecasting
git log --oneline -5
git reset --hard <previous-sha>
sudo -u nokken -H bash -c \
    'cd /srv/nokken-forecasting && /usr/local/bin/uv sync --frozen'
```

The next timer tick uses the rolled-back code. If the rollback also
needs to disarm the schedule, run the pause command above.

## 8. Debug loop with Claude Code

When the timer or service misbehaves, paste the output of the
following commands into a fresh Claude Code session. Copy-paste rather
than improvise under stress.

```
sudo systemctl list-timers nokken-forecasting-forecast.timer
sudo systemctl status nokken-forecasting-forecast.service
sudo journalctl -u nokken-forecasting-forecast.service --since "2 days ago" -o cat
sudo journalctl -u nokken-forecasting-forecast.service -p warning --since "7 days ago" -o cat
```

Greppable structured-log slices. Note: the JSON formatter emits
`"event": "..."` with a space after the colon, so a pattern that
assumes no space silently matches nothing — use `?` to tolerate
either variant (same gotcha and the same `grep -E` workaround as
nokken-data §8):

```
sudo journalctl -u nokken-forecasting-forecast.service --since "7 days ago" -o cat \
    | grep -E '"event": ?"forecast_job.done"'
sudo journalctl -u nokken-forecasting-forecast.service --since "7 days ago" -o cat \
    | grep -E '"status": ?"error"'
```

To isolate a single failing tick from the timer, run it by hand
against the same environment:

```
cd /srv/nokken-forecasting
sudo -u nokken env $(grep -v '^#' /srv/nokken-forecasting/.env | xargs) \
    /usr/local/bin/uv run nokken-forecasting forecast run
```

If `uv` or the service-user setup needs a sanity check:

```
ls -l /usr/local/bin/uv
sudo -u nokken /usr/local/bin/uv --version
ls -l /srv/nokken-forecasting/.env
```

(A missing or non-executable `/usr/local/bin/uv` is what
`status=203/EXEC: Failed to locate executable` in the journal means.
Re-run the Prerequisites install one-liner to fix.)

### Common failure modes

| Symptom | Diagnosis | Fix |
|---|---|---|
| `Failed to query Python interpreter … failed to canonicalize path` | The venv's `python` symlink targets a path under the operator's `$HOME`, which `nokken` cannot traverse. | Re-do the §3 venv-as-`nokken` step: `sudo rm -rf /srv/nokken-forecasting/.venv` then `sudo -u nokken -H bash -c '... uv sync --frozen'`. |
| `status=203/EXEC: Failed to locate executable` | `/usr/local/bin/uv` is missing or non-executable. | Re-run the Prerequisites `uv` install one-liner. |
| `ConnectionRefusedError: ... 5432` (manual run) | `.env` not sourced; asyncpg fell back to the placeholder default in `config.py`. | `set -a; . /srv/nokken-forecasting/.env; set +a` in the same shell, then re-run. |
| `cannot execute INSERT in a read-only transaction` (SQLSTATE 25006) | Either the role on `POSTGRES_DSN` lacks `INSERT` on `forecasts`, or the writer's `SET TRANSACTION READ WRITE` opt-out fell away in a refactor. | Verify the grant: `GRANT INSERT ON forecasts TO <role>` on `nessie`. If grants are correct, the regression test (`tests/integration/writers/test_forecasts.py::test_writer_overrides_session_read_only_default`) reproduces this — fix in `writers/forecasts.py`. |
| `permission denied for table forecasts` | Same as above — role lacks `INSERT`. | `GRANT INSERT ON forecasts TO <role>` on `nessie`. |
| `no observations available for gauge N at or before <ts>` | The gauge has no `value_type=flow` rows in the 7-day lookback ending at the tick's `issue_time`. | Verify nokken-data's NVE flow scraper is running (`journalctl -u nokken-data-scheduler --since "1h ago"`). If only this gauge is affected, drop it from `FORECAST_GAUGES` until the upstream feed recovers. |
| `gauge_id … violates foreign key constraint` | `FORECAST_GAUGES` lists a `gauge_id` not present in `gauges`. | Reconcile against `nokken-forecasting query gauges`. |
| Timer fires but the unit exits non-zero | Per-gauge errors logged as `event=forecast_job.gauge status=error`; if every gauge errored the job exits 1. | Grep the per-gauge log lines for `error` field; fix and either wait for the next tick or `systemctl start` the unit manually to retry. |
| Catch-up tick after host downtime writes "wrong" `issue_time` | `Persistent=true` fires the missed tick as soon as the timer is back. The job's `issue_time` defaults to wall-clock now floored to the hour, so the catch-up rows reflect the catch-up hour, not the missed midnight. | Working as intended — the audit trail (`model_run_at` stamp) makes the gap visible. To fill the gap deliberately, replay manually with `nokken-forecasting forecast run --issue-time <iso>`. |

## 9. Hindcasts

The scheduled timer covers **live** forecasts only — the `forecast run`
entrypoint above. **Hindcasts** (replays at historical issue-times,
producing rows PR 6's comparison report scores against) are an
operator-driven manual workflow. They share the same `forecasts` sink
and the same Postgres role; they're distinguished from live rows by
`model_run_at ≫ issue_time` (live: `model_run_at ≈ issue_time`). Per
`docs/phase3-scoping.md` Decisions (final).

### When to run a hindcast

- After PR 3 lands, to backfill `persistence_v1` reference rows over
  the test window (2020-01-01 → 2024-12-31). PR 3's test plan lists
  the exact invocation; this is also the smoke-test that the harness
  end-to-end works against `nessie`.
- After a new baseline lands (PR 4 = `linear_v1`, PR 5 = `lgb_v1`),
  to populate hindcast rows over the same test window so PR 6's
  comparison report has scores per baseline.
- After a baseline's `model_version` is bumped (e.g. retrained
  artefact), to re-score the new version against the same window.
  The deterministic uniqueness key is
  `(issue_time, valid_time, gauge_id, value_type, model_version)` —
  bump the version (`persistence_v1` → `persistence_v1_<date>` if
  needed) so the new rows coexist with the prior run rather than
  conflicting.

### Run a hindcast

`nokken-forecasting hindcast run` builds an inclusive issue-time list
from `--start` / `--end` / `--cadence` and dispatches the named
baseline through the harness in `nokken_forecasting.hindcast`. Every
row from one invocation shares one `model_run_at`; rerunning over the
same window is a no-op on the writer's uniqueness key.

The reference invocation — persistence baseline over the agreed test
window, weekly cadence (~261 issue-times × 168 h ≈ 43,800 rows):

```
cd /srv/nokken-forecasting
set -a; . /srv/nokken-forecasting/.env; set +a
uv run nokken-forecasting hindcast run \
    --baseline persistence --gauge-id 12 \
    --start 2020-01-01T00:00:00Z --end 2024-12-31T00:00:00Z \
    --cadence weekly
```

Recession at the same window:

```
uv run nokken-forecasting hindcast run \
    --baseline recession --gauge-id 12 \
    --start 2020-01-01T00:00:00Z --end 2024-12-31T00:00:00Z \
    --cadence weekly
```

Hourly / daily cadences are available for tighter windows (e.g.
post-incident replay over a one-week window) — pick the lightest
cadence that fills the test set. Weekly is the standing default.

### What success looks like

A healthy run starts with one `event=hindcast.start` line listing
`baseline`, `gauge_id`, `cadence`, `start`, `end`, `issue_times` count
and `model_run_at`; emits one `event=hindcast.issue_time
status=success` line per issue-time with `rows_inserted` set; and
ends with `event=hindcast.done` carrying `succeeded` / `failed` /
`rows_attempted` / `rows_inserted` / `model_run_at`. Per-issue-time
errors land as `status=error` in the per-line stream — a single bad
issue-time (e.g. a gauge outage covering the lookback window) does
not abort the run.

### Verify the rows landed

`model_run_at` is the discriminator. Replace the literal below with
the timestamp from the `hindcast.start` log line of the run you want
to inspect:

```
uv run nokken-forecasting inspect query \
    "SELECT model_version, COUNT(DISTINCT issue_time) AS issue_times, \
            COUNT(*) AS rows, \
            MIN(issue_time) AS first_issue, MAX(issue_time) AS last_issue \
     FROM forecasts \
     WHERE gauge_id = 12 \
       AND model_run_at = '<run_at-iso>' \
     GROUP BY model_version"
```

Healthy: one row per `model_version`, `rows = issue_times * 168`,
`first_issue` and `last_issue` matching the `--start` / `--end` you
passed.

### Distinguish hindcast rows from live rows

The two share the table; the live cron writes one tick a day with
`model_run_at ≈ issue_time + minutes`, hindcasts write a whole
window's worth at one wall-clock instant. Either filter:

```
-- Live rows only (model_run_at within 1 hour of issue_time):
SELECT * FROM forecasts
WHERE ABS(EXTRACT(EPOCH FROM (model_run_at - issue_time))) < 3600;

-- Hindcast rows only (model_run_at much later than issue_time):
SELECT * FROM forecasts
WHERE model_run_at > issue_time + INTERVAL '1 day';
```

The 1-hour / 1-day thresholds are coarse but reliable — the live
job's `Persistent=true` catch-up could push `model_run_at` an hour
or two past `issue_time`, but never a full day.

### When *not* to run a hindcast

- Before PR 3 merges. The harness only exists from PR 3 onwards;
  earlier hindcasts have no driver here.
- Inside the same hour as a live tick at the same `issue_time`. The
  uniqueness key collides — ON CONFLICT DO NOTHING preserves the
  live row, so the hindcast batch records zero `rows_inserted`. To
  replay a live tick deliberately, bump `model_version` to a
  `_hindcast_<date>`-suffixed variant (per `docs/phase3-scoping.md`
  §3.2) before the rerun.
- Against a window outside the observation history. Persistence /
  recession both raise `ValueError` per issue-time when the lookback
  is empty; the harness logs and continues, but a window of
  pre-2000 issue-times is just noise.

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
matching the repo name, JSON-line logs, the same `gh`-then-`uv`
install dance — so the operator's muscle memory transfers; the
scheduling primitive is what differs.

Reopen this if a second forecast cadence lands (e.g. per-NWP-cycle
4×/day) — at two cadences the choice is still a coin-flip; at three
or more, switch to APScheduler-in-service to recoup the shared
misfire / coalesce policy.
