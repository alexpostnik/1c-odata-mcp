"""Constrained, read-only 1C OData v3 client."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.parse
from datetime import date
from typing import Any
import xml.etree.ElementTree as ET

import requests


class ODataError(RuntimeError):
    pass


_NAME = re.compile(r"^[^\W\d]\w{0,127}$", re.UNICODE)
_MAX_BODY = 8_000_000
_MAX_SKIP = 10_000_000


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if (not isinstance(cursor, str) or not cursor or len(cursor) > 8
            or not cursor.isascii() or not cursor.isdecimal()):
        raise ValueError("cursor must be a bounded numeric offset")
    value = int(cursor)
    if value > _MAX_SKIP:
        raise ValueError("cursor exceeds maximum offset")
    return value


def _continuation_offset(next_link: str, current_url: str, entity_url: str,
                         expected_filter: str | None, expected_order: str | None,
                         limit: int, current_offset: int, row_count: int) -> int:
    if not isinstance(next_link, str) or len(next_link) > 4096:
        raise ODataError("invalid OData continuation")
    url = urllib.parse.urljoin(current_url, next_link)
    actual = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(entity_url)
    if (actual.scheme != "https" or actual.netloc != expected.netloc
            or urllib.parse.unquote(actual.path) != urllib.parse.unquote(expected.path)
            or actual.fragment):
        raise ODataError("OData next link outside requested entitySet")
    try:
        pairs = urllib.parse.parse_qsl(actual.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ODataError("invalid OData continuation query") from exc
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - {"$format", "$top", "$filter", "$orderby", "$skip", "$skiptoken"}:
        raise ODataError("OData continuation changed query bounds")
    if (query.get("$format", "json") != "json" or query.get("$filter", expected_filter) != expected_filter
            or query.get("$orderby", expected_order) != expected_order):
        raise ODataError("OData continuation changed filter or order")
    if "$top" in query and (not query["$top"].isascii() or not query["$top"].isdecimal()
                             or int(query["$top"]) != limit):
        raise ODataError("OData continuation changed page limit")
    if "$skiptoken" in query:
        raise ODataError("OData skiptoken continuation is unsupported")
    next_skip = query.get("$skip")
    if (row_count == 0 or next_skip is None or not next_skip.isascii() or not next_skip.isdecimal()
            or int(next_skip) != current_offset + row_count or int(next_skip) > _MAX_SKIP):
        raise ODataError("OData continuation does not advance one page")
    return int(next_skip)


def _base(profile: dict) -> str:
    return profile["serviceUrl"].rstrip("/") + "/"


def _password(profile: dict) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", profile["keychainService"],
             "-a", profile["username"], "-w"], text=True, capture_output=True,
            check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ODataError("macOS Keychain unavailable") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise ODataError("OData credential unavailable in macOS Keychain")
    return result.stdout.rstrip("\n")


class ODataClient:
    """A profile-scoped read-only client with cached metadata and pooled HTTP connections."""

    def __init__(self, profile: dict, *, target_alias: str | None = None,
                 session_factory: Any | None = None) -> None:
        self.profile = dict(profile)
        self.target_alias = target_alias
        self._session_factory = session_factory or requests.Session
        self._thread_local = threading.local()
        self._credential_lock = threading.Lock()
        self._cached_password: str | None = None
        self._metadata_lock = threading.RLock()
        self._metadata: dict[str, Any] | None = None

    def _password(self) -> str:
        if self._cached_password is None:
            with self._credential_lock:
                if self._cached_password is None:
                    self._cached_password = _password(self.profile)
        return self._cached_password

    def _session(self) -> Any:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._session_factory()
            session.auth = (self.profile["username"], self._password())
            self._thread_local.session = session
        return session

    def _request_bytes(self, url: str, *, timeout: int = 20) -> bytes:
        base = urllib.parse.urlsplit(_base(self.profile))
        target = urllib.parse.urlsplit(url)
        if target.scheme != "https" or target.netloc != base.netloc or not target.path.startswith(base.path):
            raise ODataError("OData URL outside configured service")
        try:
            with self._session().get(
                url,
                headers={"Accept": "application/json, application/xml"},
                timeout=min(max(timeout, 1), 60),
                allow_redirects=False,
                verify=True,
                stream=True,
            ) as response:
                try:
                    status = int(response.status_code)
                except (TypeError, ValueError, AttributeError):
                    raise ODataError("invalid OData response") from None
                if 300 <= status < 400:
                    raise ODataError("OData redirect refused")
                if not 200 <= status < 300:
                    raise ODataError(f"OData HTTP {status}")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    content.extend(chunk)
                    if len(content) > _MAX_BODY:
                        raise ODataError("OData response exceeds size limit")
                return bytes(content)
        except ODataError:
            raise
        except (requests.RequestException, TimeoutError, OSError):
            raise ODataError("OData connection failed or timed out") from None

    def metadata(self, *, force_refresh: bool = False) -> dict:
        with self._metadata_lock:
            if self._metadata is not None and not force_refresh:
                return self._metadata
            body = _get(self, _base(self.profile) + "$metadata")
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise ODataError("invalid OData metadata XML") from None
            types: dict[str, list[str]] = {}
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "EntityType":
                    types[node.attrib.get("Name", "")] = [
                        p.attrib["Name"] for p in node
                        if p.tag.rsplit("}", 1)[-1] == "Property" and "Name" in p.attrib
                        and _NAME.fullmatch(p.attrib["Name"])
                    ]
            sets = {}
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "EntitySet":
                    name = node.attrib.get("Name", "")
                    typ = node.attrib.get("EntityType", "").rsplit(".", 1)[-1]
                    if _NAME.fullmatch(name):
                        sets[name] = sorted(types.get(typ, []))
            parsed = {"entitySets": sets, "status": "verified_from_metadata",
                      "accountingCompleteness": "unknown"}
            self._metadata = parsed
            return parsed

    def read_page(self, entity_set: str, *, limit: int = 100,
                  from_date: str | None = None, to_exclusive: str | None = None,
                  date_property: str | None = None, cursor: str | None = None) -> dict:
        if not _NAME.fullmatch(entity_set):
            raise ValueError("invalid entitySet")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        schema = self.metadata()["entitySets"]
        if entity_set not in schema:
            raise ValueError("entitySet absent from current OData metadata")
        base = _base(self.profile)
        entity_url = base + urllib.parse.quote(entity_set)
        offset = _offset(cursor)
        params = {"$format": "json", "$top": str(limit)}
        requested_filter = None
        if from_date is not None or to_exclusive is not None:
            if not from_date or not to_exclusive or not date_property or date_property not in schema[entity_set]:
                raise ValueError("period requires an existing date property")
            start, end = _iso(from_date), _iso(to_exclusive)
            if start >= end:
                raise ValueError("period must be nonempty")
            requested_filter = (f"{date_property} ge datetime'{start}T00:00:00' and "
                                f"{date_property} lt datetime'{end}T00:00:00'")
            params["$filter"] = requested_filter
        expected_order = "Ref_Key asc" if "Ref_Key" in schema[entity_set] else None
        if expected_order:
            params["$orderby"] = expected_order
        if cursor is not None:
            params["$skip"] = str(offset)
        url = entity_url + "?" + urllib.parse.urlencode(params)
        try:
            document = json.loads(_get(self, url))
            data = document.get("d", document)
            rows = data.get("results", data.get("value"))
            next_url = (data.get("__next") or data.get("odata.nextLink")
                        or data.get("@odata.nextLink") or document.get("odata.nextLink")
                        or document.get("@odata.nextLink"))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ODataError("invalid OData JSON page") from exc
        if not isinstance(rows, list) or len(rows) > limit:
            raise ODataError("invalid OData page size")
        next_cursor = None
        if next_url:
            if expected_order is None:
                raise ODataError("stable Ref_Key ordering required for pagination")
            next_cursor = str(_continuation_offset(next_url, url, entity_url, requested_filter,
                                                    expected_order, limit, offset, len(rows)))
        elif len(rows) == limit:
            if expected_order is None:
                raise ODataError("stable Ref_Key ordering required for pagination")
            if offset + len(rows) > _MAX_SKIP:
                raise ODataError("OData page offset exceeds limit")
            next_cursor = str(offset + len(rows))
        return {"rows": rows, "nextCursor": next_cursor, "hasMore": next_cursor is not None,
                "entitySet": entity_set, "accountingCompleteness": "unknown"}

    def count(self, entity_set: str, *, from_date: str | None = None,
              to_exclusive: str | None = None, date_property: str | None = None) -> dict:
        if not _NAME.fullmatch(entity_set):
            raise ValueError("invalid entitySet")
        schema = self.metadata()["entitySets"]
        if entity_set not in schema:
            raise ValueError("entitySet absent from current OData metadata")
        url = _base(self.profile) + urllib.parse.quote(entity_set) + "/$count"
        if from_date is not None or to_exclusive is not None:
            if not from_date or not to_exclusive or not date_property or date_property not in schema[entity_set]:
                raise ValueError("period requires an existing date property")
            start, end = _iso(from_date), _iso(to_exclusive)
            if start >= end:
                raise ValueError("period must be nonempty")
            value = (f"{date_property} ge datetime'{start}T00:00:00' and "
                     f"{date_property} lt datetime'{end}T00:00:00'")
            url += "?" + urllib.parse.urlencode({"$filter": value})
        body = _get(self, url)
        try:
            number = int(body.decode("ascii").strip())
        except (ValueError, UnicodeError) as exc:
            raise ODataError("invalid OData count response") from exc
        if number < 0:
            raise ODataError("negative OData count response")
        return {"entitySet": entity_set, "count": number, "snapshotConsistency": "unknown"}


def _client(profile: dict | ODataClient) -> ODataClient:
    return profile if isinstance(profile, ODataClient) else ODataClient(profile)


def _get(profile: dict | ODataClient, url: str, *, timeout: int = 20) -> bytes:
    """Bounded HTTPS GET; accepting a client preserves its profile-scoped Session."""
    return _client(profile)._request_bytes(url, timeout=timeout)


def _iso(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("period date must be ISO YYYY-MM-DD") from exc
    return value


def metadata(profile: dict | ODataClient) -> dict:
    """Fetch current metadata. Long-lived Bridge clients reuse it between reads."""
    return _client(profile).metadata(force_refresh=True)


def read_page(profile: dict | ODataClient, entity_set: str, *, limit: int = 100,
              from_date: str | None = None, to_exclusive: str | None = None,
              date_property: str | None = None, cursor: str | None = None) -> dict:
    """Read a bounded page; a long-lived client validates against cached metadata."""
    return _client(profile).read_page(entity_set, limit=limit,
                                      from_date=from_date, to_exclusive=to_exclusive,
                                      date_property=date_property, cursor=cursor)


def count(profile: dict | ODataClient, entity_set: str, *, from_date: str | None = None,
          to_exclusive: str | None = None, date_property: str | None = None) -> dict:
    """GET OData v3 $count; a count is not a consistent snapshot guarantee."""
    return _client(profile).count(entity_set, from_date=from_date,
                                  to_exclusive=to_exclusive, date_property=date_property)
