"""Bounded runtime diagnostics: no payloads, paths, headers or exception text."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

_PHASES = {"helper", "http", "lifespan", "probe_connect", "probe_initialize", "probe_tools"}
_OUTCOMES = {
    "started", "token_rejected", "token_ready", "output_failed", "completed",
    "auth_rejected", "response_rejected", "failed", "ready", "stopped",
    "connection_failed", "timed_out", "probe_failed",
}


def emit_event(phase: str, outcome: str, *, started: float | None = None,
               status: int | None = None) -> None:
    """Emit only allowlisted fields to stderr; sink failure cannot alter auth."""
    if phase not in _PHASES or outcome not in _OUTCOMES:
        return
    event = {
        "event": "onec_mcp_runtime",
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "phase": phase,
        "outcome": outcome,
    }
    if started is not None:
        event["elapsed_ms"] = round(max(0, time.monotonic() - started) * 1000, 3)
    if type(status) is int and 100 <= status <= 599:
        event["status"] = status
    try:
        sys.stderr.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
