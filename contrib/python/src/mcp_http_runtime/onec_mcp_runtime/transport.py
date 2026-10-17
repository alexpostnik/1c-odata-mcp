"""Authenticated, loopback-only Streamable HTTP transport for MCPServer."""
from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .diagnostics import emit_event
from .tokens import _MIN_TOKEN_LENGTH, read_private_bearer_token

_LOOPBACK_HOST = "127.0.0.1"


class BearerTokenMiddleware:
    """Require a fixed local bearer token before forwarding any HTTP request."""

    def __init__(self, app: Any, token: str):
        if len(token) < _MIN_TOKEN_LENGTH:
            raise ValueError("MCP bearer token is too short")
        self.app = app
        self._expected = token.encode("ascii")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "lifespan":
            emit_event("lifespan", "started")

            async def lifecycle_send(message):
                outcomes = {
                    "lifespan.startup.complete": "ready",
                    "lifespan.startup.failed": "failed",
                    "lifespan.shutdown.complete": "stopped",
                    "lifespan.shutdown.failed": "failed",
                }
                outcome = outcomes.get(message.get("type"))
                if outcome:
                    emit_event("lifespan", outcome)
                await send(message)

            await self.app(scope, receive, lifecycle_send)
            return
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        supplied = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                supplied = value
                break
        valid = False
        if supplied is not None:
            try:
                scheme, _, value = supplied.decode("ascii").partition(" ")
                valid = scheme.casefold() == "bearer" and hmac.compare_digest(
                    value.encode("ascii"), self._expected
                )
            except (UnicodeError, ValueError):
                valid = False
        if valid:
            started = time.monotonic()

            async def response_send(message):
                status = message.get("status")
                if message.get("type") == "http.response.start" and type(status) is int and status >= 400:
                    emit_event("http", "response_rejected", started=started, status=status)
                await send(message)

            try:
                await self.app(scope, receive, response_send)
            except Exception:
                emit_event("http", "failed", started=started)
                raise
            return
        emit_event("http", "auth_rejected", status=401)
        body = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def create_authenticated_app(
    server: MCPServer,
    *,
    host: str,
    port: int,
    token_file: str | Path,
    streamable_http_path: str = "/mcp",
    json_response: bool = False,
    stateless_http: bool = True,
):
    """Build the SDK Streamable HTTP ASGI app with local auth and host checks."""
    if host != _LOOPBACK_HOST:
        raise ValueError("MCP Streamable HTTP must bind to 127.0.0.1")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("MCP Streamable HTTP port must be between 1024 and 65535")
    if not streamable_http_path.startswith("/") or streamable_http_path != "/mcp":
        raise ValueError("MCP endpoint path must be /mcp")
    token = read_private_bearer_token(token_file)
    origin = f"http://{host}:{port}"
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{host}:{port}"],
        allowed_origins=[origin],
    )
    app = server.streamable_http_app(
        host=host,
        streamable_http_path=streamable_http_path,
        json_response=json_response,
        stateless_http=stateless_http,
        transport_security=security,
    )
    return BearerTokenMiddleware(app, token)


def run_streamable_http(
    server: MCPServer,
    *,
    host: str,
    port: int,
    token_file: str | Path,
    streamable_http_path: str = "/mcp",
) -> None:
    """Run the official SDK Streamable HTTP app on a private loopback listener."""
    import uvicorn

    app = create_authenticated_app(
        server,
        host=host,
        port=port,
        token_file=token_file,
        streamable_http_path=streamable_http_path,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=server.settings.log_level.lower(),
        access_log=False,
    )
