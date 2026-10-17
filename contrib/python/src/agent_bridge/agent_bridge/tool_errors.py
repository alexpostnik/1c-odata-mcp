"""Sanitized anticipated errors for the read-only source MCP tools."""
from __future__ import annotations

from functools import wraps
import json
import re
import sys
from typing import Callable, TypeVar
import uuid

from mcp.server.mcpserver.exceptions import ToolError

from .config import ConfigError
from .core import UnknownSourceAliasError
from .source import SourceError, sanitize_probe_diagnostic

_F = TypeVar("_F", bound=Callable)


def _diagnostic(exc: Exception, correlation_id: str) -> dict:
    diagnostic = {
        "code": "SOURCE_INTERNAL_ERROR",
        "stage": "internal",
        "correlationId": correlation_id,
        "message": "The source tool failed unexpectedly; use the correlation ID when reviewing local diagnostics.",
        "action": "Retry once; if it persists, provide the correlation ID to the bridge maintainer.",
    }
    if isinstance(exc, UnknownSourceAliasError):
        diagnostic.update(
            code="UNKNOWN_SOURCE_ALIAS",
            stage="source_lookup",
            message="The requested source alias is not configured.",
            action="Use one of the configured source aliases.",
            availableSourceAliases=list(exc.configured_aliases),
        )
    elif isinstance(exc, ConfigError):
        diagnostic.update(
            code="CONFIGURATION_ERROR",
            stage="configuration",
            message="The source bridge configuration is unavailable or invalid.",
            action="Check the private source bridge configuration and restart the MCP server.",
        )
    elif isinstance(exc, ValueError):
        diagnostic.update(
            code="INVALID_SOURCE_REQUEST",
            stage="input_validation",
            message="The source request arguments are invalid.",
            action="Check the tool argument requirements and try again.",
        )
    elif isinstance(exc, SourceError):
        code = getattr(exc, "code", "SOURCE_RESPONSE_INVALID")
        stage = getattr(exc, "stage", "response_validation")
        if not isinstance(code, str):
            code = "SOURCE_RESPONSE_INVALID"
        if stage not in {"transport", "source_adapter", "response_validation"}:
            stage = "response_validation"
        if stage == "transport":
            diagnostic.update(
                code="SOURCE_TRANSPORT_FAILURE",
                stage=stage,
                message="The Windows source bridge could not be reached, timed out, or completed with an error.",
                action="Check transport_health and the configured SSH connection.",
            )
        elif (stage == "source_adapter"
              and re.fullmatch(r"SOURCE_ADAPTER_[A-Z][A-Z0-9_]{0,79}", code)):
            diagnostic.update(
                code=code,
                stage=stage,
                message="The Windows source adapter rejected or could not complete the request.",
                action="Check source adapter health and retry only after resolving the reported adapter condition.",
            )
            probe = sanitize_probe_diagnostic(getattr(exc, "probe_diagnostic", None))
            if probe is not None:
                diagnostic["probeDiagnostic"] = probe
                diagnostic["action"] = "Keep this probe on hold; resolve the reported stage before authorizing another probe."
        elif stage == "response_validation":
            diagnostic.update(
                code="SOURCE_RESPONSE_INVALID",
                stage="response_validation",
                message="The source bridge returned a response that did not meet the expected contract.",
                action="Check source adapter health and review the correlation ID if the problem persists.",
            )
        else:
            diagnostic.update(
                code="SOURCE_RESPONSE_INVALID",
                stage="response_validation",
                message="The source bridge returned a response that did not meet the expected contract.",
                action="Check source adapter health and review the correlation ID if the problem persists.",
            )
    return diagnostic


def source_tool_errors(function: _F) -> _F:
    """Convert source-tool failures to bounded MCP errors without exposing exception text."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            correlation_id = str(uuid.uuid4())
            diagnostic = _diagnostic(exc, correlation_id)
            print(json.dumps({
                "event": "source_tool_failure",
                "tool": function.__name__,
                **diagnostic,
            }, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)
            raise ToolError(json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))) from None

    return wrapped  # type: ignore[return-value]
