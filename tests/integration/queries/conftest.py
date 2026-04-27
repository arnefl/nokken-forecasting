"""Testcontainers Postgres + nokken-web migrations + synthetic seed.

Spins up a one-shot Postgres container per test session, applies the
nokken-web migrations exactly as they sat at the SCHEMA_COMPAT-pinned
SHA (read via ``git show <sha>:db/postgres/migrations/<file>`` against
the sibling checkout at ``../nokken-web``), seeds a Faukstad-centred
fixture covering all five forcing variables across both forecast
sources, and yields per-test asyncpg connections.

Reading migrations at the pinned SHA — not the sibling's ``HEAD`` —
is what the SCHEMA_COMPAT bump protocol prescribes: a future
migration could land in nokken-web that breaks the readers here, and
running tests against ``HEAD`` would surface that as a noisy local
failure unrelated to this repo.

Skip cleanly when:
* ``testcontainers`` import fails;
* Docker is not reachable (testcontainers raises during container
  start);
* the sibling ``nokken-web`` git checkout is missing or does not
  carry the pinned SHA.

This mirrors ``nokken-data``'s integration-test stance: integration
tests are operator-machine contracts, not CI gates today. The
``SCHEMA_COMPAT.md`` "CI wiring" section spells out the future
sparse-checkout job that would let CI run them.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_COMPAT_PATH = REPO_ROOT / "SCHEMA_COMPAT.md"
SIBLING_NOKKEN_WEB = REPO_ROOT.parent / "nokken-web"


def _read_pinned_sha() -> str:
    text = SCHEMA_COMPAT_PATH.read_text()
    match = re.search(r"^sha\s*=\s*([0-9a-f]{40})", text, re.MULTILINE)
    if not match:
        raise RuntimeError(
            f"Could not parse pinned SHA from {SCHEMA_COMPAT_PATH}"
        )
    return match.group(1)


def _migration_blobs(sha: str) -> list[tuple[str, str]]:
    if not (SIBLING_NOKKEN_WEB / ".git").is_dir():
        pytest.skip(
            f"sibling nokken-web git checkout not found at "
            f"{SIBLING_NOKKEN_WEB}; skip integration tests"
        )
    try:
        listing = subprocess.run(
            ["git", "ls-tree", "--name-only", f"{sha}:db/postgres/migrations"],
            cwd=SIBLING_NOKKEN_WEB,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"pinned SHA {sha} not present in {SIBLING_NOKKEN_WEB}: "
            f"{exc.stderr.strip()}"
        )
    names = sorted(line for line in listing.stdout.splitlines() if line.endswith(".sql"))
    blobs: list[tuple[str, str]] = []
    for name in names:
        body = subprocess.run(
            ["git", "show", f"{sha}:db/postgres/migrations/{name}"],
            cwd=SIBLING_NOKKEN_WEB,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        blobs.append((name, body))
    if not blobs:
        pytest.skip(f"pinned SHA {sha} has no migrations on disk; skip")
    return blobs


@pytest.fixture(scope="session")
def _postgres_container() -> Iterator[str]:
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed; skip integration tests")

    sha = _read_pinned_sha()
    blobs = _migration_blobs(sha)

    try:
        container = PostgresContainer("postgres:16-alpine", driver=None)
        container.start()
    except Exception as exc:  # pragma: no cover - infra-shaped skip
        pytest.skip(f"Docker not available for testcontainers: {exc}")

    try:
        dsn = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
        asyncio.run(_apply_migrations(dsn, blobs))
        yield dsn
    finally:
        container.stop()


async def _apply_migrations(dsn: str, blobs: list[tuple[str, str]]) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for _name, body in blobs:
            await conn.execute(body)
    finally:
        await conn.close()


@pytest.fixture
async def seeded_conn(_postgres_container: str) -> AsyncIterator[asyncpg.Connection]:
    """Yield a connection with the synthetic fixture freshly written.

    Each test gets a clean slate: tables are truncated then re-seeded
    so test ordering doesn't matter. The connection is writable —
    test seeding bypasses the read-only inspect pool.
    """
    conn = await asyncpg.connect(_postgres_container)
    try:
        await _truncate_all(conn)
        await _seed_fixture(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def seeded_conn_readonly_default(
    _postgres_container: str,
) -> AsyncIterator[asyncpg.Connection]:
    """Like ``seeded_conn`` but with the production session GUC applied.

    Production connections come from the pool in
    ``nokken_forecasting.db.postgres`` whose ``init`` callback sets
    ``default_transaction_read_only = on`` per session. Tests that
    exercise write paths through the pool's contract (job tick, future
    hindcast harness) need that GUC mirrored on the seeded connection
    so a writer regression — a refactor that lets the
    ``SET TRANSACTION READ WRITE`` opt-out fall away — would surface
    here as SQLSTATE 25006 rather than slipping through.

    PR 1's ``test_writer_overrides_session_read_only_default`` calls
    ``SET default_transaction_read_only = on`` inline rather than using
    this fixture so the GUC application stays visible alongside the
    contract assertion. New tests should prefer this fixture.
    """
    conn = await asyncpg.connect(_postgres_container)
    try:
        await _truncate_all(conn)
        await _seed_fixture(conn)
        await conn.execute("SET default_transaction_read_only = on")
        yield conn
    finally:
        await conn.close()


# ---------------- synthetic fixture ----------------
#
# One gauge (Faukstad, gauge_id=12), two sections referencing it,
# 24 hourly observations of flow, 24 hourly weather_observations rows
# for each of the 5 variables, and a single forecast cycle covering
# all 5 variables across both sources at the same (gauge,
# issue_time, valid_time) tuple — exercising the
# multi-source-per-issue case that
# ``get_weather_forecast_latest_as_of`` documents.

FIXTURE_GAUGE_ID = 12
FIXTURE_SECTION_IDS = (100, 101)
FIXTURE_RIVER_ID = 5

WEATHER_VARIABLES = (
    "temperature",
    "precipitation",
    "shortwave",
    "relative_humidity",
    "wind_speed",
)
LOCATIONFORECAST_VARIABLES = (
    "temperature",
    "precipitation",
    "relative_humidity",
    "wind_speed",
)

FIXTURE_OBS_START = datetime(2025, 4, 1, 0, 0, 0)
FIXTURE_FCST_ISSUE_TIMES = (
    datetime(2025, 4, 1, 0, 0, 0),  # earlier cycle
    datetime(2025, 4, 1, 12, 0, 0),  # latest cycle (used by latest_as_of tests)
)
FIXTURE_FCST_HORIZON_HOURS = 24


async def _truncate_all(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "TRUNCATE weather_forecasts, weather_observations, observations, "
        "sections, gauges, rivers RESTART IDENTITY CASCADE"
    )


async def _seed_fixture(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "INSERT INTO rivers (river_id, river_name, country_code) "
        "VALUES ($1, $2, $3)",
        FIXTURE_RIVER_ID,
        "Sjoa",
        "NO",
    )
    await conn.execute(
        "INSERT INTO gauges (gauge_id, gauge_name, source, sourcing_key, "
        "drainage_basin, location, has_flow, has_level, gauge_active) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        FIXTURE_GAUGE_ID,
        "Faukstad",
        "nve",
        "2.595.0",
        3700.0,
        None,
        1,
        1,
        1,
    )
    section_rows = [
        (
            section_id,
            f"Sjoa section {section_id}",
            FIXTURE_RIVER_ID,
            section_id - FIXTURE_SECTION_IDS[0] + 1,
            FIXTURE_GAUGE_ID,
            0,
            "flow",
            None,
            None,
            None,
            None,
            "0",
            "10",
            "100",
            "300",
        )
        for section_id in FIXTURE_SECTION_IDS
    ]
    await conn.executemany(
        "INSERT INTO sections (section_id, section_name, river_id, "
        "local_section_id, gauge_id, gauge_sub, gauge_default, "
        '"flowAbsoluteMin", "flowMin", "flowMax", "flowAbsoluteMax", '
        '"levelAbsoluteMin", "levelMin", "levelMax", "levelAbsoluteMax") '
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
        section_rows,
    )

    obs_rows = [
        (FIXTURE_OBS_START + timedelta(hours=h), FIXTURE_GAUGE_ID, "flow", 10.0 + h)
        for h in range(24)
    ]
    await conn.executemany(
        "INSERT INTO observations (time, gauge_id, value_type, value) "
        "VALUES ($1, $2, $3, $4)",
        obs_rows,
    )

    wobs_rows = []
    for variable in WEATHER_VARIABLES:
        for h in range(24):
            wobs_rows.append(
                (
                    FIXTURE_OBS_START + timedelta(hours=h),
                    FIXTURE_GAUGE_ID,
                    variable,
                    float(h) + (0.1 * WEATHER_VARIABLES.index(variable)),
                    "met_nordic_analysis_v4",
                    1,  # basin_version
                )
            )
    # One row with NULL basin_version + alternate source so source / NULL
    # filtering tests have something to grip on.
    wobs_rows.append(
        (
            FIXTURE_OBS_START,
            FIXTURE_GAUGE_ID,
            "temperature",
            -99.0,
            "met_nordic_analysis_operational",
            None,
        )
    )
    await conn.executemany(
        "INSERT INTO weather_observations (time, gauge_id, variable, "
        "value, source, basin_version) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        wobs_rows,
    )

    # Forecast cycles: each issue time emits H+1..H+FIXTURE_FCST_HORIZON_HOURS
    # rows for every (variable, source) pair the source covers.
    fcst_rows = []
    for issue_time in FIXTURE_FCST_ISSUE_TIMES:
        for h in range(1, FIXTURE_FCST_HORIZON_HOURS + 1):
            valid_time = issue_time + timedelta(hours=h)
            for variable in LOCATIONFORECAST_VARIABLES:
                fcst_rows.append(
                    (
                        issue_time,
                        valid_time,
                        FIXTURE_GAUGE_ID,
                        variable,
                        float(h),
                        "met_locationforecast_2_complete",
                        None,  # quantile (deterministic)
                        1,
                    )
                )
            # Shortwave only on metpplatest, same issue_time so a
            # latest_as_of read with no source filter returns rows from
            # both sources at the same issue cycle.
            fcst_rows.append(
                (
                    issue_time,
                    valid_time,
                    FIXTURE_GAUGE_ID,
                    "shortwave",
                    float(h) * 100.0,
                    "met_nordic_forecast_1km",
                    None,
                    1,
                )
            )
    # One probabilistic row at the latest cycle to exercise non-NULL
    # quantile handling.
    latest = FIXTURE_FCST_ISSUE_TIMES[-1]
    fcst_rows.append(
        (
            latest,
            latest + timedelta(hours=1),
            FIXTURE_GAUGE_ID,
            "temperature",
            5.5,
            "met_locationforecast_2_complete",
            0.9,
            1,
        )
    )
    await conn.executemany(
        "INSERT INTO weather_forecasts (issue_time, valid_time, gauge_id, "
        "variable, value, source, quantile, basin_version) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        fcst_rows,
    )
