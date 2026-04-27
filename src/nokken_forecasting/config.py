"""Environment-driven application settings.

Mirrors `nokken-data/src/nokken_data/config.py` so a single credential
set spans the three repos. Every variable is listed in `.env.example`.
The eventual systemd forecast unit will load `/srv/nokken-forecasting/.env`
via `EnvironmentFile=`; tests monkeypatch the environment directly.
`get_settings()` is `lru_cache`d so the first call snapshots the
environment for the process lifetime.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    # Placeholder default lets `Settings()` construct under unit tests
    # without env setup. It intentionally points at nothing real — any
    # caller that tries to open a connection will fail fast.
    postgres_dsn: str = Field(default="postgresql://nokken:@localhost:5432/nessie")

    # Write-capable DSN for the forecast-sink path (Phase 3 PR 1+).
    # Carries the `nokken_forecast_writer` role on production deploy
    # units; unset locally and in unit tests, in which case opening
    # the write pool fails fast. Never reuse `postgres_dsn` for writes
    # — that pool sets `default_transaction_read_only = on`.
    postgres_write_dsn: str | None = Field(default=None)

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
