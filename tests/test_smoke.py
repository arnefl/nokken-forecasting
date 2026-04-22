"""Trivial smoke test so `uv run pytest` exits 0 on a fresh scaffold.

Replaced with real tests once Phase 3 lands model code.
"""

import nokken_forecasting


def test_package_imports() -> None:
    assert nokken_forecasting.__doc__ is not None
