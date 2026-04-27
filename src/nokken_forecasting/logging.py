"""JSON-line logging configuration.

The shape mirrors ``nokken-data``'s ``_JsonFormatter`` so a single
``journalctl`` workflow spans both repos: every record renders as one
JSON object per line with ``ts`` / ``level`` / ``logger`` / ``msg`` plus
any structured fields passed via ``extra=`` on the call site. Operators
``grep`` or ``jq`` over the same shape regardless of which sibling
emitted the line.

Format-only parity. The two repos do not import each other; cross-repo
imports would re-introduce the deploy coupling the three-repo split
deliberately broke.

The Phase-3 forecast job uses the structured-fields lane heavily —
``event="forecast_job.gauge"`` / ``gauge_id=...`` / ``rows_written=...``
— so the operator's runbook can grep one line per gauge tick. The
formatter writes any non-standard ``LogRecord`` attribute through to the
output object verbatim, with ``json.dumps(default=str)`` falling back on
``str()`` for non-trivial values (``Timestamp``, ``datetime``, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any

# LogRecord attributes the standard library always sets. Anything outside
# this set was injected by the caller via ``extra=`` and gets emitted as a
# top-level key on the JSON object.
_STANDARD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install :class:`JsonFormatter` on the root logger.

    Idempotent: the root handler is replaced on every call so a second
    invocation (e.g. tests sharing a process) doesn't stack handlers and
    duplicate every line.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
