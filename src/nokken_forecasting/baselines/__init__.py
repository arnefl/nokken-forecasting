"""Forecast baselines.

Each baseline is a pure module: takes input data, returns
``ForecastRow`` objects ready for the writer in
``nokken_forecasting.writers.forecasts``. No DB I/O lives in this
package; the CLI (or the scheduled forecast job and the hindcast
harness) wires the readers, the baseline, and the writer together.

Per ``docs/phase3-scoping.md`` Decisions (final), this sprint scopes
to Faukstad (gauge id 12) only and ``model_version`` follows the
``<scheme>_v<N>`` convention.
"""

from __future__ import annotations

from nokken_forecasting.baselines.persistence import (
    ForecastRow,
    persistence_forecast,
)
from nokken_forecasting.baselines.recession import recession_forecast

__all__ = [
    "ForecastRow",
    "persistence_forecast",
    "recession_forecast",
]
