"""Private bearer validation shared by helpers and HTTP transport (stdlib only)."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_MIN_TOKEN_LENGTH = 32


def read_private_bearer_token(path: str | Path) -> str:
    """Read a private per-user token file without following symlinks."""
    selected = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(selected, flags)
    except OSError as exc:
        raise ValueError("MCP bearer token file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("MCP bearer token path must be a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("MCP bearer token file must belong to the current user")
        if info.st_mode & 0o077:
            raise ValueError("MCP bearer token file permissions must be 0600 or stricter")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            token = stream.read().strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("MCP bearer token file cannot be read") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(token) < _MIN_TOKEN_LENGTH or not token.isascii() or re.search(r"\s", token):
        raise ValueError("MCP bearer token must be at least 32 non-whitespace ASCII characters")
    return token
