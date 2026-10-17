"""Small, bounded 1C 8.3 OData client for MCP tools."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import html as html_lib
import json
import re
import threading
import errno
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID
from xml.etree import ElementTree

from .auth import KeychainCredentialStore
from .config import RuntimeConfig
from .counterparty import require_counterparty_post_permit
from .diagnostics import (TRANSPORT_SUBREASONS, diagnostic_stage,
                          record_odata_attempt, record_request_call)


class ODataClientError(RuntimeError):
    """Raised for sanitized OData transport and response errors."""


class ODataHTTPError(ODataClientError):
    """Raised for sanitized non-2xx OData HTTP responses."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str | None = None,
        message: str | None = None,
        definitive: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message or None
        self.detail = self.message
        self.definitive = definitive
        super().__init__(str(self))

    def __str__(self) -> str:
        text = f"OData HTTP {self.status_code}"
        if self.code:
            text += f" code {self.code}"
        if self.message:
            text += f": {self.message}"
        return text


class ODataTransportError(ODataClientError):
    """Raised when no definite HTTP response was received from OData."""

    _CATEGORIES = {"timeout", "connection", "tls", "proxy", "transport"}
    _SUBREASONS = TRANSPORT_SUBREASONS

    def __init__(self, category: str, *, attempts: int, subreason: str | None = None) -> None:
        self.category = category if category in self._CATEGORIES else "transport"
        self.attempts = attempts
        self.subreason = (
            subreason if isinstance(subreason, str) and subreason in self._SUBREASONS else None
        )
        suffix = "attempt" if attempts == 1 else "attempts"
        super().__init__(f"OData transport {self.category} after {attempts} {suffix}")


class ODataAcceptedResponseError(ODataClientError):
    """Raised when a write received 2xx but its response body was unusable."""

    def __init__(self, status_code: int, *, key: str | None = None) -> None:
        self.accepted = True
        self.status_code = status_code
        self.key = key
        self.location_key = key
        super().__init__(str(self))

    def __str__(self) -> str:
        text = f"OData HTTP {self.status_code} accepted but response could not be parsed"
        if self.key:
            text += f"; key {self.key}"
        return text


@dataclass(frozen=True)
class _ODataErrorInfo:
    code: str | None
    message: str | None
    structured: bool


_SAFE_SEGMENT = re.compile(r"^[^\W\d]\w{0,127}(?:\.[^\W\d]\w{0,127})*$", re.UNICODE)
_MAX_RESPONSE_BYTES = 8_000_000
_MAX_ERROR_BYTES = 64_000
_MAX_ERROR_DETAIL_CHARS = 600
_MAX_ERROR_LINES = 6
_MAX_SKIP = 10_000_000
_MAX_QUERY_CHARS = 4_000
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_NULL_GUID = "00000000-0000-0000-0000-000000000000"
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|authorization|api[-_ ]?key)\b"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_HTML_RE = re.compile(r"(?is)<\s*(?:!doctype\s+html|html|head|body)\b")
_HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<\s*(script|style)\b.*?<\s*/\s*\1\s*>")
_HTML_TAG_RE = re.compile(r"(?is)<[^>]+>")
_HTML_BREAK_RE = re.compile(r"(?is)<\s*(?:br|/\s*(?:p|div|li|tr|td|body|html|h[1-6]))\b[^>]*>")
_SESSION_BLOCK_MARKERS = (
    "Начало сеанса с информационной базой запрещено",
    "информационной базой запрещено",
    "занята для обновления",
    "занята для обновления или архивации",
)
_SESSION_BLOCK_DETAIL_PATTERNS = (
    re.compile(
        r"Начало сеанса с информационной базой запрещено\.?"
        r"(?:\s*В данный момент база занята для обновления(?: или архивации)?"
        r"(?:,\s*дождитесь окончания блокировки)?\.?)?"
    ),
    re.compile(
        r"В данный момент база занята для обновления(?: или архивации)?"
        r"(?:,\s*дождитесь окончания блокировки)?\.?"
    ),
)
_STACKTRACE_LINE_RE = re.compile(
    r"(?i)^(traceback\b|stack\s*trace\b|at\s+\S+|file\s+\".*\"|caused by:|"
    r"\s*[\w.$<>]+\([^)]*\)|\s*line\s+\d+\b)"
)
_HEADER_LINE_RE = re.compile(
    r"(?i)^(authorization|proxy-authorization|cookie|set-cookie|host|user-agent|"
    r"accept|content-type|content-length|x-[a-z0-9-]+)\s*:"
)
_LOCATION_GUID_RE = re.compile(
    r"\bguid'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'"
)


PasswordProvider = Callable[[str, str], str]


def _safe_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _query_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_QUERY_CHARS:
        raise ValueError(f"invalid {label}")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {label}")
    return value


def _header_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"invalid {label}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"invalid {label}")
    return value


def _odata_string_literal(value: str, label: str) -> str:
    value = _header_text(value, label)
    escaped = value.replace("'", "''")
    return "'" + quote(escaped, safe="._-") + "'"


def _bounded_int(value: int, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be {minimum}..{maximum}")
    return value


def _uuid_key(value: UUID | str) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("key must be a UUID") from exc


def _create_payload(payload: Mapping[str, Any], null_ref_key: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    data = dict(payload)
    if not null_ref_key:
        return data
    ref_key = data.get("Ref_Key")
    if ref_key is None or ref_key == "":
        data["Ref_Key"] = _NULL_GUID
        return data
    try:
        normalized = str(ref_key if isinstance(ref_key, UUID) else UUID(str(ref_key)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("create payload Ref_Key must be a UUID") from exc
    if normalized != _NULL_GUID:
        raise ValueError("create payload Ref_Key must be the null GUID")
    data["Ref_Key"] = _NULL_GUID
    return data


def _safe_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _content_bytes(response: Any, *, limit: int) -> bytes:
    content = getattr(response, "content", b"")
    if content is None:
        return b""
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if not isinstance(content, bytes):
        return b""
    return content[:limit]


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "content-type":
            return str(value).lower()
    return ""


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted and isinstance(value, str):
            return value
    return None


def _location_guid(response: Any) -> str | None:
    location = _response_header(response, "Location")
    if not location or len(location) > 2048:
        return None
    match = _LOCATION_GUID_RE.search(location)
    if not match:
        return None
    try:
        return str(UUID(match.group(1)))
    except (TypeError, ValueError, AttributeError):
        return None


def _accepted_response_error(response: Any, *, key: str | None = None) -> ODataAcceptedResponseError:
    return ODataAcceptedResponseError(_status_code(response), key=key or _location_guid(response))


def _looks_like_html(text: str, content_type: str = "") -> bool:
    return "text/html" in content_type or bool(_HTML_RE.search(text[:1024]))


def _is_stack_or_header_line(line: str) -> bool:
    return bool(_STACKTRACE_LINE_RE.search(line) or _HEADER_LINE_RE.search(line))


def _bounded_detail(text: str) -> str | None:
    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = "".join(char if ord(char) >= 32 or char == "\t" else " " for char in raw_line).strip()
        if not line or _is_stack_or_header_line(line):
            continue
        lines.append(re.sub(r"\s+", " ", line))
        if len(lines) >= _MAX_ERROR_LINES:
            break
    if not lines:
        return None
    detail = " | ".join(lines)
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[: _MAX_ERROR_DETAIL_CHARS - 1].rstrip() + "..."
    return detail or None


def _html_visible_text(text: str) -> str:
    text = _HTML_SCRIPT_STYLE_RE.sub("\n", text)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return html_lib.unescape(text)


def _known_session_block_text(text: str | None) -> bool:
    return bool(text and any(marker in text for marker in _SESSION_BLOCK_MARKERS))


def _extract_session_block_detail(text: str) -> str | None:
    normalized = " ".join(text.split())
    for pattern in _SESSION_BLOCK_DETAIL_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return match.group(0).strip()
    return None


def _html_error_info(text: str, redactions: set[str]) -> _ODataErrorInfo:
    detail = _extract_session_block_detail(_html_visible_text(text))
    if detail is not None:
        return _ODataErrorInfo(None, _sanitize_text(detail, redactions), False)
    return _ODataErrorInfo(None, None, False)


def _sanitize_text(value: Any, redactions: set[str], *, max_chars: int = _MAX_ERROR_DETAIL_CHARS) -> str | None:
    text = _safe_string(value)
    if text is None:
        return None
    text = _URL_RE.sub("[redacted-url]", text)
    text = _BASIC_AUTH_RE.sub("Basic [redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    for item in sorted((item for item in redactions if item), key=len, reverse=True):
        text = text.replace(item, "[redacted]")
    detail = _bounded_detail(text)
    if detail is None:
        return None
    if len(detail) > max_chars:
        detail = detail[: max_chars - 1].rstrip() + "..."
    return detail


def _sanitize_code(value: Any, redactions: set[str]) -> str | None:
    code = _sanitize_text(value, redactions, max_chars=96)
    if code is None:
        return None
    return re.sub(r"\s+", " ", code)


def _message_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        nested = value.get("value")
        if isinstance(nested, str):
            return nested
    return None


def _append_unique(messages: list[str], value: str | None) -> None:
    value = _safe_string(value)
    if value is not None and value not in messages:
        messages.append(value)


def _json_error_info(payload: Any, redactions: set[str]) -> _ODataErrorInfo | None:
    if not isinstance(payload, Mapping):
        return None
    raw_error = payload.get("odata.error")
    if raw_error is None:
        raw_error = payload.get("error")
    if isinstance(raw_error, str):
        return _ODataErrorInfo(None, _sanitize_text(raw_error, redactions), False)
    if not isinstance(raw_error, Mapping):
        return None

    messages: list[str] = []
    _append_unique(messages, _message_value(raw_error.get("message")))
    details = raw_error.get("details")
    if isinstance(details, list):
        for item in details[:4]:
            if isinstance(item, Mapping):
                _append_unique(messages, _message_value(item.get("message")))
    inner = raw_error.get("innererror", raw_error.get("innerError"))
    if isinstance(inner, Mapping):
        _append_unique(messages, _message_value(inner.get("message")))

    detail = _sanitize_text(" | ".join(messages), redactions) if messages else None
    code = _sanitize_code(raw_error.get("code"), redactions)
    structured = bool(code or detail or "message" in raw_error or "details" in raw_error)
    return _ODataErrorInfo(code, detail, structured)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _first_child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in list(element):
        if _local_name(child.tag) == name and child.text:
            return child.text
    return None


def _xml_error_info(text: str, redactions: set[str]) -> _ODataErrorInfo | None:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    if _local_name(root.tag) != "error":
        return None
    messages: list[str] = []
    _append_unique(messages, _first_child_text(root, "message"))
    for child in list(root):
        if _local_name(child.tag) == "innererror":
            _append_unique(messages, _first_child_text(child, "message"))
            break
    detail = _sanitize_text(" | ".join(messages), redactions) if messages else None
    code = _sanitize_code(_first_child_text(root, "code"), redactions)
    structured = bool(code or detail)
    return _ODataErrorInfo(code, detail, structured)


def _response_error_info(response: Any, redactions: set[str]) -> _ODataErrorInfo:
    content = _content_bytes(response, limit=_MAX_ERROR_BYTES)
    if not content:
        return _ODataErrorInfo(None, None, False)
    text = content.decode("utf-8", errors="replace")
    content_type = _content_type(response)

    stripped = text.lstrip()
    if "json" in content_type or stripped.startswith(("{", "[")):
        try:
            info = _json_error_info(json.loads(text), redactions)
        except (TypeError, ValueError):
            info = None
        if info is not None:
            return info

    if "xml" in content_type or stripped.startswith("<"):
        info = _xml_error_info(text, redactions)
        if info is not None:
            return info

    if _looks_like_html(text, content_type):
        return _html_error_info(text, redactions)
    return _ODataErrorInfo(None, _sanitize_text(text, redactions), False)


def _is_definitive_http_error(status: int) -> bool:
    if 400 <= status < 500:
        return status not in {408, 429}
    if 500 <= status < 600:
        # Even a structured 1C exception does not prove that a write was rolled back.
        return False
    return False


def _is_known_where_filter_rejection(status: int, error_info: _ODataErrorInfo) -> bool:
    code = (error_info.code or "").split(",", 1)[0].strip()
    message = error_info.message or ""
    return (
        status == 500
        and code == "-1"
        and 'Операция не разрешена в предложении "ГДЕ"' in message
    )


def _transport_details(exc: BaseException) -> tuple[str, str | None]:
    """Classify from exception structure only; never inspect exception messages."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    seen_containers: set[int] = set()
    names: list[str] = []
    errnos: set[int] = set()
    reasons: set[str] = set()
    while pending and len(seen) < 40:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        cls = type(current)
        # Keep concrete types only: urllib3's NewConnectionError inherits
        # ConnectTimeoutError, but that inheritance does not mean a timeout occurred.
        names.append(cls.__name__.lower())
        names.append(getattr(cls, "__module__", "").lower())
        number = getattr(current, "errno", None)
        if isinstance(number, int):
            errnos.add(number)
        reason = getattr(current, "reason", None)
        if isinstance(reason, str):
            reasons.add(reason.upper())
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        # requests/urllib3 wrappers place nested exception instances inside args.
        # Traverse only exception objects and plain tuple/list containers.
        stack: list[Any] = [current.args]
        inspected = 0
        while stack and inspected < 100 and len(seen) + len(pending) < 40:
            value = stack.pop()
            inspected += 1
            if isinstance(value, BaseException):
                pending.append(value)
            elif isinstance(value, (tuple, list)):
                if id(value) in seen_containers:
                    continue
                seen_containers.add(id(value))
                stack.extend(value[:10])
        wrapped_reason = getattr(current, "reason", None)
        if isinstance(wrapped_reason, BaseException):
            pending.append(wrapped_reason)

    haystack = " ".join(names)
    if errno.ECONNRESET in errnos:
        return "connection", "connection_reset"
    if errno.ECONNREFUSED in errnos:
        return "connection", "connection_refused"
    if "gaierror" in names or "nameresolutionerror" in names:
        return "connection", "connection_dns"
    if "timeout" in haystack:
        if "connecttimeout" in haystack or "connect_timeout" in haystack:
            return "timeout", "timeout_connect"
        if "readtimeout" in haystack or "read_timeout" in haystack:
            return "timeout", "timeout_read"
        return "timeout", "timeout_unknown"
    if "ssl" in haystack or "tls" in haystack or "certificate" in haystack:
        if any("certverification" in name or name == "certificateerror" for name in names):
            return "tls", "tls_certificate"
        if "CERTIFICATE_VERIFY_FAILED" in reasons:
            return "tls", "tls_certificate"
        if any(name in {"sslhandshakeerror", "tls_handshake_error"} for name in names) or reasons & {
            "TLSV1_ALERT_HANDSHAKE_FAILURE", "SSLV3_ALERT_HANDSHAKE_FAILURE",
            "HANDSHAKE_FAILURE",
        }:
            return "tls", "tls_handshake"
        if "sslprotocolerror" in haystack or reasons & {
            "TLSV1_ALERT_PROTOCOL_VERSION", "PROTOCOL_VERSION", "WRONG_VERSION_NUMBER",
            "UNSUPPORTED_PROTOCOL",
        }:
            return "tls", "tls_protocol"
        return "tls", None
    if "proxy" in haystack:
        return "proxy", None
    if any(item in haystack for item in ("connection", "connect", "socket")):
        return "connection", None
    return "transport", None


def _transport_category(exc: BaseException) -> str:
    return _transport_details(exc)[0]


def _payload_redactions(value: Any, redactions: set[str], *, limit: int = 100) -> None:
    if len(redactions) >= limit:
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _payload_redactions(item, redactions, limit=limit)
            if len(redactions) >= limit:
                return
    elif isinstance(value, list):
        for item in value[:20]:
            _payload_redactions(item, redactions, limit=limit)
            if len(redactions) >= limit:
                return
    elif isinstance(value, str):
        if len(value) >= 3:
            redactions.add(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
        if len(text) >= 3:
            redactions.add(text)


def _request_json(response: Any) -> Any:
    content = getattr(response, "content", b"")
    if content is None:
        content = b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ODataClientError("OData response exceeds size limit")
    if getattr(response, "status_code", 0) == 204 or not content:
        return None
    try:
        if hasattr(response, "json"):
            return response.json()
        text = getattr(response, "text", "")
        return json.loads(text)
    except (TypeError, ValueError):
        raise ODataClientError("OData response is not valid JSON") from None


def _metadata_text(response: Any) -> str:
    content = getattr(response, "content", b"")
    if content is None:
        content = b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ODataClientError("OData response exceeds size limit")
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ODataClientError("OData metadata response is not text") from None


def _normalize_collection(payload: Any, limit: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ODataClientError("OData collection response is not an object")
    data = payload.get("d", payload)
    if isinstance(data, dict):
        rows = data.get("results")
        if rows is None:
            rows = data.get("value")
        count = data.get("__count", data.get("@odata.count", data.get("odata.count")))
        next_link = data.get("__next", data.get("@odata.nextLink", data.get("odata.nextLink")))
    else:
        rows = data
        count = None
        next_link = None
    if rows is None and isinstance(payload.get("value"), list):
        rows = payload["value"]
        count = payload.get("@odata.count", payload.get("odata.count"))
        next_link = payload.get("@odata.nextLink", payload.get("odata.nextLink"))
    if not isinstance(rows, list) or len(rows) > limit:
        raise ODataClientError("OData collection response has invalid page size")
    result: dict[str, Any] = {"value": rows}
    if count is not None:
        try:
            result["count"] = int(count)
        except (TypeError, ValueError):
            raise ODataClientError("OData count value is invalid") from None
    if next_link is not None:
        if not isinstance(next_link, str) or len(next_link) > _MAX_QUERY_CHARS:
            raise ODataClientError("OData next link is invalid")
        result["next_link"] = next_link
    return result


def _normalize_item(payload: Any) -> Any:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return payload
    data = payload.get("d", payload)
    if isinstance(data, dict) and "results" in data:
        raise ODataClientError("OData item response unexpectedly contains a collection")
    return data


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code"))
    except (TypeError, ValueError):
        raise ODataClientError("OData transport returned an invalid response") from None


class ODataClient:
    """Read and write 1C 8.3 entities through standard OData."""

    def __init__(
        self,
        config: RuntimeConfig,
        password_provider: PasswordProvider | KeychainCredentialStore | None = None,
        transport: Any | None = None,
    ) -> None:
        self.config = config
        self._password_provider = password_provider or KeychainCredentialStore()
        self._transport = transport if transport is not None else self._default_transport()
        self._password: str | None = None
        self._password_lock = threading.Lock()
        self._injected_transport = transport is not None

    @property
    def write_enabled(self) -> bool:
        return self.config.write_enabled

    def get_metadata(self) -> str:
        with diagnostic_stage("metadata_read"):
            response = self._request("GET", "$metadata", accept="application/xml, application/json")
        return _metadata_text(response)

    def list_entities(
        self,
        entity_set: str,
        top: int,
        skip: int,
        filter: str | None = None,
        select: str | None = None,
        orderby: str | None = None,
        expand: str | None = None,
    ) -> dict[str, Any]:
        top = _bounded_int(top, "top", 1, self.config.max_top)
        skip = _bounded_int(skip, "skip", 0, _MAX_SKIP)
        params: dict[str, Any] = {"$format": "json", "$top": top, "$skip": skip}
        if filter is not None:
            params["$filter"] = _query_text(filter, "filter")
        if select is not None:
            params["$select"] = _query_text(select, "select")
        if orderby is not None:
            params["$orderby"] = _query_text(orderby, "orderby")
        if expand is not None:
            params["$expand"] = _query_text(expand, "expand")
        response = self._request("GET", self._entity_path(entity_set), params=params)
        return _normalize_collection(_request_json(response), top)

    def count_entities(self, entity_set: str) -> int:
        response = self._request(
            "GET",
            self._entity_path(entity_set, "$count"),
            accept="application/json, application/xml, */*",
        )
        content = getattr(response, "text", None)
        if content is None:
            raw = getattr(response, "content", b"")
            if isinstance(raw, str):
                content = raw
            else:
                try:
                    content = raw.decode("ascii")
                except (AttributeError, UnicodeDecodeError):
                    raise ODataClientError("OData count response is invalid") from None
        try:
            value = int(str(content).strip())
        except ValueError:
            raise ODataClientError("OData count response is invalid") from None
        if value < 0:
            raise ODataClientError("OData count response is invalid")
        return value

    def get_entity(self, entity_set: str, key: UUID | str) -> Any:
        response = self._request("GET", self._entity_key_path(entity_set, key))
        return _normalize_item(_request_json(response))

    def get_register_record_set(
        self,
        entity_set: str,
        recorder: UUID | str,
        recorder_type: str,
    ) -> Any:
        if not entity_set.startswith("AccountingRegister_"):
            raise ValueError("entity_set must be an AccountingRegister_*")
        path = (
            f"{self._entity_path(entity_set)}("
            f"Recorder=guid'{_uuid_key(recorder)}',"
            f"Recorder_Type={_odata_string_literal(recorder_type, 'recorder_type')}"
            ")"
        )
        response = self._request("GET", path)
        return _normalize_item(_request_json(response))

    def create_entity(
        self,
        entity_set: str,
        payload: Mapping[str, Any],
        null_ref_key: bool = False,
        *,
        _prod_write_probe_stage: str | None = None,
    ) -> Any:
        self._require_write(
            entity_set=entity_set,
            payload=payload,
            prod_write_probe_stage=_prod_write_probe_stage,
        )
        require_counterparty_post_permit(entity_set, payload, self.config.odata_base_url)
        response = self._request(
            "POST",
            self._entity_path(entity_set),
            json_payload=_create_payload(payload, null_ref_key),
        )
        location_key = _location_guid(response)
        try:
            created = _normalize_item(_request_json(response))
        except ODataClientError as exc:
            if isinstance(exc, ODataAcceptedResponseError):
                raise
            raise _accepted_response_error(response, key=location_key) from None
        if created is None and location_key:
            return {"Ref_Key": location_key}
        if isinstance(created, dict) and location_key and not created.get("Ref_Key"):
            return {**created, "Ref_Key": location_key}
        if null_ref_key and (not isinstance(created, dict) or not created.get("Ref_Key")):
            raise _accepted_response_error(response, key=location_key)
        if not isinstance(created, dict) and location_key:
            raise _accepted_response_error(response, key=location_key)
        return created

    def patch_entity(
        self,
        entity_set: str,
        key: UUID | str,
        payload: Mapping[str, Any],
        if_match: str | None = None,
        *,
        _prod_write_probe_stage: str | None = None,
    ) -> Any:
        self._require_write(
            entity_set=entity_set,
            payload=payload,
            prod_write_probe_stage=_prod_write_probe_stage,
        )
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        key = _uuid_key(key)
        headers = None
        if if_match is not None:
            headers = {"If-Match": _header_text(if_match, "if_match")}
        response = self._request(
            "PATCH",
            self._entity_key_path(entity_set, key),
            json_payload=dict(payload),
            extra_headers=headers,
        )
        try:
            return _normalize_item(_request_json(response))
        except ODataClientError as exc:
            if isinstance(exc, ODataAcceptedResponseError):
                raise
            raise _accepted_response_error(response, key=key) from None

    def invoke_action(
        self,
        entity_set: str,
        key: UUID | str,
        action: str,
        if_match: str | None = None,
    ) -> Any:
        self._require_write()
        key = _uuid_key(key)
        action = _safe_segment(action, "action")
        params = {"PostingModeOperational": "false"} if action == "Post" else None
        headers = None
        if if_match is not None:
            headers = {"If-Match": _header_text(if_match, "if_match")}
        response = self._request(
            "POST",
            f"{self._entity_key_path(entity_set, key)}/{quote(action, safe='.')}",
            params=params,
            extra_headers=headers,
        )
        try:
            return _normalize_item(_request_json(response))
        except ODataClientError as exc:
            if isinstance(exc, ODataAcceptedResponseError):
                raise
            raise _accepted_response_error(response, key=key) from None

    def _entity_path(self, entity_set: str, suffix: str | None = None) -> str:
        entity_set = _safe_segment(entity_set, "entity_set")
        path = quote(entity_set, safe="")
        if suffix is not None:
            suffix = _safe_segment(suffix, "path segment") if suffix != "$count" else "$count"
            path += "/" + quote(suffix, safe="$")
        return path

    def _entity_key_path(self, entity_set: str, key: UUID | str) -> str:
        return f"{self._entity_path(entity_set)}(guid'{_uuid_key(key)}')"

    def _require_write(
        self,
        *,
        entity_set: str | None = None,
        payload: Mapping[str, Any] | None = None,
        prod_write_probe_stage: str | None = None,
    ) -> None:
        if self.write_enabled:
            return
        if self._prod_write_probe_request_is_allowed(
            entity_set=entity_set,
            payload=payload,
            stage=prod_write_probe_stage,
        ):
            return
        raise PermissionError("OData writes are disabled")

    def _prod_write_probe_request_is_allowed(
        self,
        *,
        entity_set: str | None,
        payload: Mapping[str, Any] | None,
        stage: str | None,
    ) -> bool:
        config = self.config
        probe_id = getattr(config, "prod_write_probe_id", None)
        configured_fingerprint = getattr(
            config, "prod_write_probe_fingerprint", None,
        )
        normalized_url = config.odata_base_url.rstrip("/") + "/"
        fingerprint = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        if not (
            getattr(config, "multi_target", False)
            and getattr(config, "target_kind", None) == "prod"
            and getattr(config, "prod_write_probe_enabled", False)
            and configured_fingerprint == fingerprint
            and isinstance(probe_id, str)
            and entity_set == "Catalog_Организации"
            and isinstance(payload, Mapping)
        ):
            return False

        marker = f"MCP PROD WRITE PROBE {probe_id}"
        if stage == "create":
            allowed_payload = dict(payload)
            ref_key = allowed_payload.pop("Ref_Key", None)
            return (
                (ref_key is None or ref_key == _NULL_GUID)
                and allowed_payload == {"Description": marker}
            )
        if stage == "update":
            return dict(payload) == {"Description": marker + " UPDATED"}
        if stage == "soft_delete":
            return dict(payload) == {"DeletionMark": True}
        return False

    def _password_value(self) -> str:
        if self._password is None:
            with self._password_lock:
                if self._password is None:
                    provider = self._password_provider
                    if callable(provider):
                        password = provider(self.config.keychain_service, self.config.odata_user)
                    else:
                        password = provider.password(self.config.keychain_service, self.config.odata_user)
                    if not isinstance(password, str) or password == "":
                        raise ODataClientError("OData credential is unavailable")
                    self._password = password
        return self._password

    def for_worker(self) -> "ODataClient":
        """Create a worker-local HTTP session while reusing this client's cached credential."""
        transport = self._transport if self._injected_transport else None
        return ODataClient(
            self.config,
            password_provider=lambda _service, _user: self._password_value(),
            transport=transport,
        )

    def _redactions(self, password: str, json_payload: Mapping[str, Any] | None) -> set[str]:
        redactions: set[str] = set()
        if len(password) >= 4:
            redactions.add(password)
        base_url = self.config.odata_base_url.rstrip("/")
        redactions.add(base_url)
        redactions.add(base_url + "/")
        parsed = urlsplit(self.config.odata_base_url)
        for segment in (part for part in parsed.path.split("/") if part):
            if segment == "odata" or segment == "standard.odata":
                continue
            if len(segment) >= 8 or any(char.isdigit() for char in segment):
                redactions.add(segment)
        if json_payload is not None:
            _payload_redactions(json_payload, redactions)
        return redactions

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_payload: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        accept: str = "application/json",
    ) -> Any:
        headers = {
            "Accept": accept,
            "Accept-Charset": "UTF-8",
            "DataServiceVersion": "3.0;NetFx",
            "MaxDataServiceVersion": "3.0;NetFx",
        }
        if json_payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if extra_headers is not None:
            headers.update(dict(extra_headers))
        password = self._password_value()
        redactions = self._redactions(password, json_payload)
        kwargs = {
            "headers": headers,
            "params": dict(params or {}),
            "auth": (self.config.odata_user, password),
            "timeout": self.config.timeout_seconds,
            "verify": True,
            "allow_redirects": False,
        }
        if json_payload is not None:
            kwargs["json"] = json_payload

        attempts = self.config.get_retries + 1 if method == "GET" else 1
        record_request_call()
        last_response: Any | None = None
        last_error_info: _ODataErrorInfo | None = None
        for attempt in range(attempts):
            attempt_started = perf_counter()
            try:
                response = self._transport.request(method, self.config.odata_base_url + path, **kwargs)
            except Exception as exc:
                category, subreason = _transport_details(exc)
                record_odata_attempt(
                    stage=None,
                    elapsed_ms=round((perf_counter() - attempt_started) * 1000, 1),
                    attempt=attempt + 1,
                    error_category=category,
                    transport_subreason=subreason,
                )
                if method == "GET" and attempt + 1 < attempts:
                    continue
                raise ODataTransportError(category, attempts=attempt + 1,
                                          subreason=subreason) from None
            try:
                status = _status_code(response)
            except ODataClientError:
                record_odata_attempt(
                    stage=None,
                    elapsed_ms=round((perf_counter() - attempt_started) * 1000, 1),
                    attempt=attempt + 1,
                    error_category="odata_client",
                )
                raise
            record_odata_attempt(
                stage=None,
                elapsed_ms=round((perf_counter() - attempt_started) * 1000, 1),
                attempt=attempt + 1,
                status_code=status,
            )
            if 300 <= status < 400:
                raise ODataHTTPError(status, definitive=False)
            if 200 <= status < 300:
                return response
            last_response = response
            last_error_info = None
            if method == "GET" and status in _TRANSIENT_STATUS and attempt + 1 < attempts:
                if status == 500:
                    error_info = _response_error_info(response, redactions)
                    if _is_known_where_filter_rejection(status, error_info):
                        last_error_info = error_info
                        break
                continue
            break
        status = _status_code(last_response)
        error_info = last_error_info or _response_error_info(last_response, redactions)
        raise ODataHTTPError(
            status,
            code=error_info.code,
            message=error_info.message,
            definitive=_is_definitive_http_error(status),
        )

    @staticmethod
    def _default_transport() -> Any:
        try:
            import requests
        except ImportError:
            raise ODataClientError("requests dependency is unavailable") from None
        return requests.Session()
