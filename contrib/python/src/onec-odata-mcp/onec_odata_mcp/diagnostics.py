"""Opt-in, content-free performance events for local 1C OData runs."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading
from time import perf_counter
from typing import Any, Iterator
from uuid import uuid4


_ALLOWED_TOOLS = frozenset({
    "onec83_apply_document_queue",
    "onec83_connection_status",
    "onec83_count_entities",
    "onec83_create_entity",
    "onec83_describe_entity_set",
    "onec83_document_action",
    "onec83_get_entity",
    "onec83_get_accounting_register_record_set",
    "onec83_get_write_receipt",
    "onec83_list_document_types",
    "onec83_list_entities",
    "onec83_list_entity_sets",
    "onec83_patch_entity",
    "onec83_post_document",
    "onec83_query_entities",
    "onec83_reconcile_pending_create",
    "onec83_reconcile_pending_post",
    "onec83_refresh_metadata",
    "onec83_replace_tabular_section",
    "onec83_resolve_enum_value",
    "onec83_search_entities",
    "onec83_search_entities_batch",
    "onec83_set_deletion_mark",
    "onec83_unpost_document",
    "onec83_update_entity",
})
_ROW_TOKEN_RE = re.compile(r"^[0-9a-f]{16,32}$")
_ALLOWED_STAGES = frozenset({
    "connection_status",
    "metadata_read",
    "entity_lookup",
    "entity_read",
    "queue",
    "queue_preflight",
    "create",
    "readback",
    "post_precondition_read",
    "unpost_precondition_read",
    "post",
    "post_readback",
    "unpost_readback",
    "movement_verification",
    "server_filter_cache_hit",
    "readback_404_ref_key_lookup",
    "reconciliation",
    "patch",
    "unpost",
    "other",
})
_ALLOWED_OUTCOMES = frozenset({
    "success", "partial", "failed", "needs_reconciliation", "dry_run", "unknown",
})
_ALLOWED_ERROR_CATEGORIES = frozenset({
    "odata_http", "odata_client", "timeout", "connection", "tls", "proxy",
    "transport", "credential", "configuration", "validation", "permission",
    "uncertain_write", "http_4xx", "http_5xx", "unexpected",
})
TRANSPORT_SUBREASONS = frozenset({
    "timeout_connect",
    "timeout_read",
    "timeout_unknown",
    "tls_certificate",
    "tls_handshake",
    "tls_protocol",
    "connection_reset",
    "connection_refused",
    "connection_dns",
})
_ALLOWED_SEARCH_CLASSES = frozenset({
    "string_equality", "date_range", "date_equality",
})
_ALLOWED_SEARCH_STRATEGIES = frozenset({
    "server_filter", "server_filter_error", "fallback_scan", "fallback_resume",
    "fallback_scan_cache_hit",
})
_CURRENT_CALL: ContextVar["PerformanceCall | None"] = ContextVar("onec83_perf_call", default=None)
_CURRENT_COUNTER: ContextVar["RequestCounter | None"] = ContextVar("onec83_request_counter", default=None)
_CURRENT_STAGE: ContextVar[str] = ContextVar("onec83_perf_stage", default="other")
_CURRENT_ROW: ContextVar[str | None] = ContextVar("onec83_perf_row", default=None)


def _category(value: str | None) -> str | None:
    return value if isinstance(value, str) and value in _ALLOWED_ERROR_CATEGORIES else None


def _stage(value: str | None) -> str:
    return value if isinstance(value, str) and value in _ALLOWED_STAGES else "other"


def _outcome(value: str) -> str:
    return value if value in _ALLOWED_OUTCOMES else "unknown"


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, perf_counter() - started) * 1000, 1)


def _error_category(error: BaseException) -> str:
    name = type(error).__name__
    if name == "ODataTransportError":
        return _category(getattr(error, "category", None)) or "transport"
    if name == "ODataHTTPError":
        return "odata_http"
    if name == "ODataClientError":
        return "odata_client"
    if name == "CredentialError":
        return "credential"
    if name == "ConfigError":
        return "configuration"
    if name in {"UncertainWrite", "ODataAcceptedResponseError"}:
        return "uncertain_write"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, ValueError):
        return "validation"
    return "unexpected"


class RequestCounter:
    """Thread-safe logical-request and HTTP-attempt counters, optionally nested."""

    def __init__(self, parent: "RequestCounter | None" = None) -> None:
        self.parent = parent
        self._lock = threading.Lock()
        self._calls = 0
        self._attempts = 0

    def request(self) -> None:
        self._increment("_calls")

    def attempt(self) -> None:
        self._increment("_attempts")

    def _increment(self, field_name: str) -> None:
        current: RequestCounter | None = self
        while current is not None:
            with current._lock:
                setattr(current, field_name, getattr(current, field_name) + 1)
            current = current.parent

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self._calls, self._attempts


@dataclass
class PerformanceCall:
    logger: "PerformanceLogger"
    tool_name: str
    call_id: str = field(default_factory=lambda: uuid4().hex)
    counter: RequestCounter = field(default_factory=RequestCounter)
    outcome: str = "success"
    error_category: str | None = None
    summary: dict[str, int] = field(default_factory=dict)
    search_class: str | None = None
    search_strategy: str | None = None
    search_counts: dict[str, int] = field(default_factory=lambda: {
        "fallback_page_reads": 0,
        "date_lower_bound_probes": 0,
    })

    def observe_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        if result.get("dryRun") is True:
            self.outcome = "dry_run"
        elif result.get("state") == "needs_reconciliation" or _nonnegative_int(
            result.get("needsReconciliation")
        ):
            self.outcome = "needs_reconciliation"
        elif result.get("ok") is False:
            self.outcome = "failed"
            error = result.get("error")
            if isinstance(error, dict):
                self.error_category = _category(error.get("category")) or _category(
                    error.get("kind")
                )
                kind_map = {
                    "odataHttp": "odata_http",
                    "odataClient": "odata_client",
                    "uncertainWrite": "uncertain_write",
                    "credential": "credential",
                    "configuration": "configuration",
                    "validation": "validation",
                    "permission": "permission",
                }
                if self.error_category is None:
                    self.error_category = kind_map.get(error.get("kind"))
        elif _nonnegative_int(result.get("failed")):
            self.outcome = (
                "failed"
                if _nonnegative_int(result.get("succeeded")) == 0
                else "partial"
            )

        if isinstance(result.get("total"), int) and not isinstance(result.get("total"), bool):
            self.summary["wave_size"] = max(0, result["total"])
            for source, target in (
                ("maxConcurrency", "concurrency"),
                ("succeeded", "rows_succeeded"),
                ("failed", "rows_failed"),
                ("needsReconciliation", "rows_uncertain"),
            ):
                number = result.get(source)
                if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                    self.summary[target] = number


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class PerformanceLogger:
    """Append fixed-schema JSONL events to a private local file."""

    def __init__(self, *, enabled: bool, path: Path | None = None) -> None:
        self.enabled = enabled
        self.path = path
        self.run_id = uuid4().hex
        self._lock = threading.Lock()
        self._write_failures = 0

    @classmethod
    def from_environment(cls) -> "PerformanceLogger":
        enabled = os.environ.get("ONEC83_PERF_LOGGING", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled:
            return cls(enabled=False)
        raw_path = os.environ.get("ONEC83_PERF_LOG_PATH", "").strip()
        path = (
            Path(raw_path).expanduser()
            if raw_path
            else Path.home() / "Library" / "Logs" / "onec-odata-mcp" / "onec83-performance.jsonl"
        )
        return cls(enabled=True, path=path)

    @property
    def write_failures(self) -> int:
        return self._write_failures

    def _append(self, event: dict[str, Any]) -> None:
        if not self.enabled or self.path is None:
            return
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            descriptor: int | None = None
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("performance log is not a regular file")
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(line)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("short write")
                    remaining = remaining[written:]
            except OSError:
                self._write_failures += 1
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        self._write_failures += 1

    def _base(self, event_type: str, call: PerformanceCall) -> dict[str, Any]:
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event_type,
            "run_id": self.run_id,
            "call_id": call.call_id,
            "tool": call.tool_name if call.tool_name in _ALLOWED_TOOLS else "other",
        }

    def log_tool_call(self, call: PerformanceCall, elapsed_ms: float) -> None:
        odata_calls, odata_attempts = call.counter.snapshot()
        event = self._base("mcp_call", call)
        event.update({
            "stage": _stage(_CURRENT_STAGE.get()),
            "elapsed_ms": elapsed_ms,
            "mcp_calls": 1,
            "odata_calls": odata_calls,
            "odata_attempts": odata_attempts,
            "outcome": _outcome(call.outcome),
        })
        error_category = _category(call.error_category)
        if error_category:
            event["error_category"] = error_category
        event.update(call.summary)
        if call.search_class in _ALLOWED_SEARCH_CLASSES and call.search_strategy in _ALLOWED_SEARCH_STRATEGIES:
            event.update({
                "search_class": call.search_class,
                "search_strategy": call.search_strategy,
                **{name: _nonnegative_int(value) for name, value in call.search_counts.items()
                   if name in {"fallback_page_reads", "date_lower_bound_probes"}},
            })
        self._append(event)

    def log_odata_attempt(
        self,
        call: PerformanceCall,
        *,
        stage: str,
        elapsed_ms: float,
        attempt: int,
        status_code: int | None,
        error_category: str | None,
        transport_subreason: str | None = None,
    ) -> None:
        event = self._base("odata_attempt", call)
        event.update({
            "stage": _stage(stage),
            "elapsed_ms": elapsed_ms,
            "attempt": max(1, attempt),
        })
        row_token = _CURRENT_ROW.get()
        if isinstance(row_token, str) and _ROW_TOKEN_RE.fullmatch(row_token):
            event["row_token"] = row_token
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            event["http_status"] = status_code
            if status_code >= 500:
                event["error_category"] = "http_5xx"
            elif status_code >= 400:
                event["error_category"] = "http_4xx"
        elif _category(error_category):
            event["error_category"] = _category(error_category)
        if status_code is None and isinstance(transport_subreason, str) and transport_subreason in TRANSPORT_SUBREASONS:
            event["transport_subreason"] = transport_subreason
        self._append(event)

    def log_stage(
        self,
        call: PerformanceCall,
        *,
        stage: str,
        elapsed_ms: float,
        counter: RequestCounter,
        outcome: str,
        error_category: str | None,
    ) -> None:
        calls, attempts = counter.snapshot()
        event = self._base("stage", call)
        event.update({
            "stage": _stage(stage),
            "elapsed_ms": elapsed_ms,
            "odata_calls": calls,
            "odata_attempts": attempts,
            "outcome": _outcome(outcome),
        })
        row_token = _CURRENT_ROW.get()
        if isinstance(row_token, str) and _ROW_TOKEN_RE.fullmatch(row_token):
            event["row_token"] = row_token
        if _category(error_category):
            event["error_category"] = _category(error_category)
        self._append(event)

    def log_queue_row(
        self,
        call: PerformanceCall,
        *,
        row_token: str,
        elapsed_ms: float,
        counter: RequestCounter,
        outcome: str,
        error_category: str | None,
    ) -> None:
        if not _ROW_TOKEN_RE.fullmatch(row_token):
            return
        calls, attempts = counter.snapshot()
        event = self._base("queue_row", call)
        event.update({
            "row_token": row_token,
            "elapsed_ms": elapsed_ms,
            "odata_calls": calls,
            "odata_attempts": attempts,
            "outcome": _outcome(outcome),
        })
        if _category(error_category):
            event["error_category"] = _category(error_category)
        self._append(event)


PERFORMANCE_LOGGER = PerformanceLogger.from_environment()


@contextmanager
def instrument_tool_call(tool_name: str, logger: PerformanceLogger = PERFORMANCE_LOGGER) -> Iterator[PerformanceCall | None]:
    if not logger.enabled:
        yield None
        return
    call = PerformanceCall(logger=logger, tool_name=tool_name)
    started = perf_counter()
    call_token: Token[PerformanceCall | None] = _CURRENT_CALL.set(call)
    counter_token: Token[RequestCounter | None] = _CURRENT_COUNTER.set(call.counter)
    stage_token: Token[str] = _CURRENT_STAGE.set(_tool_stage(tool_name))
    try:
        yield call
    except BaseException as exc:
        call.outcome = "failed"
        call.error_category = _error_category(exc)
        raise
    finally:
        logger.log_tool_call(call, _elapsed_ms(started))
        _CURRENT_STAGE.reset(stage_token)
        _CURRENT_COUNTER.reset(counter_token)
        _CURRENT_CALL.reset(call_token)


def _tool_stage(tool_name: str) -> str:
    if tool_name in {"onec83_connection_status"}:
        return "connection_status"
    if tool_name in {"onec83_list_entity_sets", "onec83_list_document_types", "onec83_describe_entity_set", "onec83_refresh_metadata"}:
        return "metadata_read"
    if tool_name in {
        "onec83_search_entities", "onec83_search_entities_batch",
        "onec83_count_entities",
    }:
        return "entity_lookup"
    if tool_name == "onec83_get_accounting_register_record_set":
        return "movement_verification"
    if tool_name in {"onec83_get_entity", "onec83_query_entities", "onec83_list_entities"}:
        return "entity_read"
    if tool_name == "onec83_apply_document_queue":
        return "queue"
    if tool_name in {"onec83_get_write_receipt", "onec83_reconcile_pending_create", "onec83_reconcile_pending_post"}:
        return "reconciliation"
    if tool_name in {"onec83_create_entity"}:
        return "create"
    if tool_name in {"onec83_post_document", "onec83_document_action"}:
        return "post"
    if tool_name == "onec83_unpost_document":
        return "unpost"
    if tool_name in {"onec83_patch_entity", "onec83_update_entity", "onec83_replace_tabular_section", "onec83_set_deletion_mark"}:
        return "patch"
    return "other"


def current_performance_call() -> PerformanceCall | None:
    return _CURRENT_CALL.get()


def set_search_profile(search_class: str, strategy: str) -> None:
    """Set a fixed, content-free search classification for the active MCP call."""
    call = _CURRENT_CALL.get()
    if call is None:
        return
    call.search_class = (
        search_class
        if isinstance(search_class, str) and search_class in _ALLOWED_SEARCH_CLASSES
        else None
    )
    call.search_strategy = (
        strategy
        if isinstance(strategy, str) and strategy in _ALLOWED_SEARCH_STRATEGIES
        else None
    )


def increment_search_count(name: str) -> None:
    """Count one logical fallback read/probe; retries remain OData attempts, not pages."""
    call = _CURRENT_CALL.get()
    if call is None or name not in {"fallback_page_reads", "date_lower_bound_probes"}:
        return
    call.search_counts[name] += 1


def record_request_call() -> None:
    counter = _CURRENT_COUNTER.get()
    if counter is not None:
        counter.request()


def record_odata_attempt(
    *,
    stage: str | None,
    elapsed_ms: float,
    attempt: int,
    status_code: int | None = None,
    error_category: str | None = None,
    transport_subreason: str | None = None,
) -> None:
    counter = _CURRENT_COUNTER.get()
    if counter is not None:
        counter.attempt()
    call = _CURRENT_CALL.get()
    if call is not None:
        call.logger.log_odata_attempt(
            call,
            stage=_stage(stage or _CURRENT_STAGE.get()),
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            status_code=status_code,
            error_category=error_category,
            transport_subreason=transport_subreason,
        )


@contextmanager
def diagnostic_stage(name: str) -> Iterator["StageTrace"]:
    trace = StageTrace()
    call = _CURRENT_CALL.get()
    if call is None:
        yield trace
        return
    stage_name = _stage(name)
    parent = _CURRENT_COUNTER.get() or call.counter
    counter = RequestCounter(parent=parent)
    counter_token: Token[RequestCounter | None] = _CURRENT_COUNTER.set(counter)
    stage_token: Token[str] = _CURRENT_STAGE.set(stage_name)
    started = perf_counter()
    try:
        yield trace
    except BaseException as exc:
        trace.outcome = "failed"
        trace.error_category = _error_category(exc)
        raise
    finally:
        call.logger.log_stage(
            call,
            stage=stage_name,
            elapsed_ms=_elapsed_ms(started),
            counter=counter,
            outcome=trace.outcome,
            error_category=trace.error_category,
        )
        _CURRENT_STAGE.reset(stage_token)
        _CURRENT_COUNTER.reset(counter_token)


@dataclass
class StageTrace:
    outcome: str = "success"
    error_category: str | None = None

    def observe_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        if result.get("verified") is False or result.get("status") in {"unavailable", "mismatch"}:
            self.outcome = "failed"
            self.error_category = "odata_client" if result.get("status") == "unavailable" else None


@contextmanager
def diagnostic_row(row_token: str | None) -> Iterator[RequestCounter | None]:
    call = _CURRENT_CALL.get()
    if call is None or not isinstance(row_token, str) or not _ROW_TOKEN_RE.fullmatch(row_token):
        yield None
        return
    parent = _CURRENT_COUNTER.get() or call.counter
    counter = RequestCounter(parent=parent)
    counter_token: Token[RequestCounter | None] = _CURRENT_COUNTER.set(counter)
    row_token_context: Token[str | None] = _CURRENT_ROW.set(row_token)
    started = perf_counter()
    outcome = "unknown"
    error_category: str | None = None
    try:
        yield counter
    except BaseException as exc:
        outcome = "failed"
        error_category = _error_category(exc)
        raise
    finally:
        if getattr(counter, "_diagnostic_error_category", None):
            error_category = counter._diagnostic_error_category
        outcome = getattr(counter, "_diagnostic_outcome", outcome)
        call.logger.log_queue_row(
            call,
            row_token=row_token,
            elapsed_ms=_elapsed_ms(started),
            counter=counter,
            outcome=outcome,
            error_category=error_category,
        )
        _CURRENT_ROW.reset(row_token_context)
        _CURRENT_COUNTER.reset(counter_token)


def set_row_outcome(counter: RequestCounter | None, result: Any) -> None:
    """Set queue-row outcome on its active diagnostic context without retaining row data."""
    if counter is None:
        return
    # Outcome is stored on the counter only as a private diagnostic attribute.
    state = result.get("state") if isinstance(result, dict) else None
    if state in {"created", "posted"}:
        setattr(counter, "_diagnostic_outcome", "success")
    elif state == "needs_reconciliation":
        setattr(counter, "_diagnostic_outcome", "needs_reconciliation")
    elif state == "failed":
        setattr(counter, "_diagnostic_outcome", "failed")
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        category = _category(error.get("category"))
        if category is None:
            category = {
                "odataHttp": "odata_http",
                "odataClient": "odata_client",
                "uncertainWrite": "uncertain_write",
                "credential": "credential",
                "configuration": "configuration",
                "validation": "validation",
                "permission": "permission",
            }.get(error.get("kind"))
        if category:
            setattr(counter, "_diagnostic_error_category", category)
