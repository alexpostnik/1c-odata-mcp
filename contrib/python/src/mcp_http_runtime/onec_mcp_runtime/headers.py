"""Codex local HTTP header helpers; they never print tokens except as JSON stdout."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .diagnostics import emit_event
from .tokens import read_private_bearer_token

_TOKEN_NAMES = {
    "onec83-odata": "onec83-odata.token",
    "onecLive": "onecLive.token",
}


def token_file_for_server(server_id: str, *, home: Path | None = None) -> Path:
    try:
        filename = _TOKEN_NAMES[server_id]
    except KeyError as exc:
        raise ValueError("unknown local MCP server id") from exc
    root = home or Path.home()
    return root / "Library" / "Application Support" / "onec-mcp-runtime" / "tokens" / filename


def headers_json(token_file: str | Path) -> str:
    token = read_private_bearer_token(token_file)
    return json.dumps({"Authorization": f"Bearer {token}"}, separators=(",", ":"))


def _emit(server_id: str) -> int:
    started = time.monotonic()
    emit_event("helper", "started")
    try:
        output = headers_json(token_file_for_server(server_id))
    except ValueError:
        emit_event("helper", "token_rejected", started=started)
        return 1
    emit_event("helper", "token_ready", started=started)
    try:
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
    except OSError:
        emit_event("helper", "output_failed", started=started)
        return 1
    emit_event("helper", "completed", started=started)
    return 0


def odata_main() -> None:
    raise SystemExit(_emit("onec83-odata"))


def live77_main() -> None:
    raise SystemExit(_emit("onecLive"))
