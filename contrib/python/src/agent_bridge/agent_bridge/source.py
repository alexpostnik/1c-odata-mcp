"""Bounded SSH transport to a fixed Windows JSON program."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from datetime import date
import unicodedata
from typing import Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Mac/Linux runtime has fcntl.
    fcntl = None  # type: ignore[assignment]


class SourceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SOURCE_RESPONSE_INVALID",
                 stage: str = "response_validation", probe_diagnostic: dict | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.probe_diagnostic = sanitize_probe_diagnostic(probe_diagnostic)


_PROBE_STAGES = frozenset({
    "request-validation", "config-read", "source-lookup", "dataset-lookup",
    "metadata-catalog", "open-application", "create-catalog", "select-items",
    "get-item", "current-item", "serialize-seed", "resolve-seed",
    "resolve-native-scalar", "resolve-native-reference",
    "serialize-resolved", "check-selected", "metadata-attributes",
    "serialize-rows", "serialize-attributes", "use-date", "find-item",
    "check-catalog-kind", "serialize-relations",
    "source-namespace", "metadata-document", "create-document", "select-documents",
    "get-document", "current-document", "check-document-kind", "find-document",
    "select-lines", "get-line", "serialize-header",
})
_PROBE_EXCEPTION_TYPES = frozenset({
    "other", "System.Exception", "System.InvalidOperationException",
    "System.ArgumentException", "System.NullReferenceException",
    "System.InvalidCastException", "System.MissingMemberException",
    "System.MissingMethodException", "System.UnauthorizedAccessException",
    "System.IO.IOException", "System.Runtime.InteropServices.COMException",
    "System.Reflection.TargetInvocationException",
    "System.Management.Automation.RuntimeException",
    "System.Management.Automation.MethodInvocationException",
    "System.Management.Automation.PropertyNotFoundException",
    "System.Management.Automation.CommandNotFoundException",
})
_PROBE_ERROR_CODES = frozenset({
    "INTERNAL_ERROR", "NATIVE_PROBE_STAGE_FAILED", "INVALID_REQUEST", "INVALID_CONFIG",
    "CONFIG_NOT_FOUND", "SOURCE_DENIED", "SOURCE_UNAVAILABLE", "DATASET_DENIED",
    "METADATA_READER_UNAVAILABLE", "METADATA_READ_FAILED", "METADATA_CATALOG_UNAVAILABLE",
    "WRONG_BITNESS", "COM_NOT_REGISTERED", "COM_ACCESS_DENIED", "COM_CREATE_FAILED",
    "COM_INIT_FAILED", "CREDENTIAL_UNAVAILABLE", "INITIALIZE_FAILED", "COM_MEMBER_UNAVAILABLE", "RUN_MODE_UNAVAILABLE",
    "IDENTITY_SEED_NOT_FOUND", "NATIVE_IDENTITY_UNAVAILABLE", "NATIVE_IDENTITY_ROUNDTRIP_FAILED",
    "BROKER_FAILED", "BROKER_OPERATION_DENIED", "REMOTE_ERROR",
    "NATIVE_CATALOG_MISMATCH", "NATIVE_REFERENCE_NOT_FOUND", "ASOF_CONTEXT_UNAVAILABLE",
    "NATIVE_DOCUMENT_MISMATCH", "NATIVE_DOCUMENT_NOT_FOUND", "NATIVE_SOURCE_MISMATCH",
    "METADATA_DOCUMENT_UNAVAILABLE", "NATIVE_LINE_IDENTITY_INVALID", "BROKER_RECOVERED_STALE_PROCESSING", "BROKER_INVALID_ENVELOPE",
})


def sanitize_probe_diagnostic(value: object) -> dict | None:
    """Copy only fixed stage/class names and bounded HRESULTs; never raw remote fields."""
    if (not isinstance(value, dict) or not isinstance(value.get("stage"), str)
            or value["stage"] not in _PROBE_STAGES):
        return None

    def node(raw: object) -> dict:
        if not isinstance(raw, dict):
            return {}
        result = {}
        if isinstance(raw.get("exceptionType"), str) and raw["exceptionType"] in _PROBE_EXCEPTION_TYPES:
            result["exceptionType"] = raw["exceptionType"]
        hr = raw.get("hresult")
        if isinstance(hr, str) and re.fullmatch(r"0x[0-9A-Fa-f]{8}", hr):
            result["hresult"] = hr.upper().replace("0X", "0x")
        return result

    safe = {"stage": value["stage"], **node(value)}
    inner = value.get("innerExceptions")
    if isinstance(inner, list):
        children = [n for item in inner[:3] if (n := node(item))]
        if children:
            safe["innerExceptions"] = children
    return safe


_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_SSH_HOST_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,253}$")
_TRANSPORT_IDENTITY_COMMAND = "cmd.exe /d /c whoami"
_WINDOWS_WHOAMI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}\\([A-Za-z0-9_][A-Za-z0-9_.-]{0,79})$")
_METADATA_TOP_REQUIRED_KEYS = {"status", "catalogs", "documents", "counts", "otherSections"}
_METADATA_TOP_OPTIONAL_KEYS = {"constants", "sectionCoverage"}
_METADATA_COUNT_REQUIRED_KEYS = {"catalogs", "documents", "attributes"}
_METADATA_COUNT_OPTIONAL_KEYS = {
    "constants", "catalogAttributes", "documentHeadAttributes",
    "documentTableAttributes", "totalMetadataAttributes",
}
_METADATA_SECTION_KEYS = {
    "constants", "enumerations", "registers", "journals", "documentNumerators",
    "chartsOfAccounts", "reports", "dataProcessors",
}
_METADATA_SECTION_INDICATOR_KEYS = {"present", "boundedObjectCount", "countLimit"}
_METADATA_SECTION_COVERAGE_KEYS = _METADATA_SECTION_KEYS | {"catalogs", "documents", "unmapped"}
_METADATA_SECTION_COVERAGE_KEYS_WITH_NAMES = _METADATA_SECTION_COVERAGE_KEYS - {"unmapped"}
_METADATA_SECTION_COVERAGE_INDICATOR_KEYS = {
    "present", "rootSectionCount", "boundedObjectCount", "countLimit", "sectionNames",
}
_METADATA_UNMAPPED_SECTION_COVERAGE_KEYS = {
    "present", "rootSectionCount", "boundedObjectCount", "countLimit",
}
_METADATA_OBJECT_LIMIT = 10_000
_METADATA_ATTRIBUTE_LIMIT = 200_000
_METADATA_SECTION_COUNT_LIMIT = 100_000
_REQUEST_TIMEOUT_DEFAULT = 30
_REQUEST_TIMEOUT_MAX = 240
_DOCUMENT_PRESENCE_LIMIT_MAX = 5_000
_TIMED_READ_OPERATIONS = {
    "read_page", "read_documents_page", "read_document_lines_page",
    "read_document_lines_batch",
}


@contextmanager
def _source_exchange_lock(source: dict):
    if fcntl is None:
        yield
        return
    identity = {
        "host": source.get("host"),
        "user": source.get("user"),
        "hostKeyAlias": source.get("hostKeyAlias"),
        "remoteCommand": source.get("remoteCommand"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    root = Path(os.environ.get("TMPDIR") or "/tmp") / "agent_bridge_1c_locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / f"source-{digest}.lock"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _identifier(value: str, max_length: int = 80) -> bool:
    """Match Windows Assert-Identifier: Unicode letters, ASCII digits, underscore."""
    return bool(value) and len(value) <= max_length and (
        value[0] == "_" or unicodedata.category(value[0]).startswith("L")) and all(
        ch == "_" or ch in "0123456789" or unicodedata.category(ch).startswith("L")
        for ch in value[1:])


def _date(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("period dates must be ISO YYYY-MM-DD") from exc
    return value


def _request_timeout(source: dict, timeout: int | None) -> int:
    if timeout is not None:
        return timeout
    value = source.get("requestTimeoutSeconds", _REQUEST_TIMEOUT_DEFAULT)
    if type(value) is not int or not 1 <= value <= _REQUEST_TIMEOUT_MAX:
        raise ValueError(f"request timeout must be 1..{_REQUEST_TIMEOUT_MAX} seconds")
    return value


def _metadata_nonnegative_int(value: object, limit: int) -> bool:
    return type(value) is int and 0 <= value <= limit


def _metadata_attr(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != {"id", "name", "typeCode", "length", "precision", "refTypeId"}:
        return False
    if not isinstance(value["id"], str) or not value["id"].isascii() or not value["id"].isdecimal():
        return False
    if not isinstance(value["name"], str) or not _identifier(value["name"], 128):
        return False
    if not isinstance(value["typeCode"], str) or not re.fullmatch(r"[A-Z]", value["typeCode"]):
        return False
    for key in ("length", "precision"):
        if value[key] is not None and not _metadata_nonnegative_int(value[key], 10_000_000):
            return False
    ref_type = value["refTypeId"]
    return isinstance(ref_type, str) and (ref_type == "" or (ref_type.isascii() and ref_type.isdecimal()))


def _metadata_object(value: object, *, document: bool) -> tuple[bool, int]:
    if not isinstance(value, dict):
        return False, 0
    if document:
        if set(value) != {"id", "name", "headAttributes", "tableAttributes"}:
            return False, 0
        attr_lists = (value["headAttributes"], value["tableAttributes"])
    else:
        if set(value) != {"id", "name", "attributes"}:
            return False, 0
        attr_lists = (value["attributes"],)
    if not isinstance(value["id"], str) or not value["id"].isascii() or not value["id"].isdecimal():
        return False, 0
    if not isinstance(value["name"], str) or not _identifier(value["name"], 128):
        return False, 0
    count = 0
    for attrs in attr_lists:
        if not isinstance(attrs, list):
            return False, 0
        count += len(attrs)
        if count > _METADATA_ATTRIBUTE_LIMIT:
            return False, 0
        if any(not _metadata_attr(attr) for attr in attrs):
            return False, 0
    return True, count


def _metadata_keys(value: dict, required: set[str], optional: set[str]) -> bool:
    keys = set(value)
    return required <= keys and keys <= (required | optional)


def _metadata_section_indicator(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _METADATA_SECTION_INDICATOR_KEYS:
        return False
    if type(value["present"]) is not bool:
        return False
    limit = value["countLimit"]
    count = value["boundedObjectCount"]
    if limit != _METADATA_SECTION_COUNT_LIMIT or not _metadata_nonnegative_int(count, limit):
        return False
    return value["present"] == (count > 0)


def _metadata_section_coverage_indicator(value: object, *, section_names: bool) -> bool:
    expected = (
        _METADATA_SECTION_COVERAGE_INDICATOR_KEYS
        if section_names
        else _METADATA_UNMAPPED_SECTION_COVERAGE_KEYS
    )
    if not isinstance(value, dict) or set(value) != expected:
        return False
    if type(value["present"]) is not bool:
        return False
    limit = value["countLimit"]
    root_count = value["rootSectionCount"]
    child_count = value["boundedObjectCount"]
    if limit != _METADATA_SECTION_COUNT_LIMIT:
        return False
    if not _metadata_nonnegative_int(root_count, limit):
        return False
    if not _metadata_nonnegative_int(child_count, limit):
        return False
    if value["present"] != (root_count > 0):
        return False
    if section_names:
        names = value["sectionNames"]
        if not isinstance(names, list) or not 1 <= len(names) <= 20:
            return False
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) for name in names):
            return False
    return True


def _validate_metadata_inventory(data: object) -> None:
    if not isinstance(data, dict) or not _metadata_keys(
        data, _METADATA_TOP_REQUIRED_KEYS, _METADATA_TOP_OPTIONAL_KEYS,
    ):
        raise SourceError("invalid metadata inventory")
    if data.get("status") != "metadata_catalog":
        raise SourceError("invalid metadata inventory")
    counts = data.get("counts")
    if not isinstance(counts, dict) or not _metadata_keys(
        counts, _METADATA_COUNT_REQUIRED_KEYS, _METADATA_COUNT_OPTIONAL_KEYS,
    ):
        raise SourceError("metadata inventory lacks bounded counts")
    if not _metadata_nonnegative_int(counts["catalogs"], _METADATA_OBJECT_LIMIT):
        raise SourceError("metadata inventory lacks bounded counts")
    if not _metadata_nonnegative_int(counts["documents"], _METADATA_OBJECT_LIMIT):
        raise SourceError("metadata inventory lacks bounded counts")
    if not _metadata_nonnegative_int(counts["attributes"], _METADATA_ATTRIBUTE_LIMIT):
        raise SourceError("metadata inventory lacks bounded counts")
    for key in (
        "constants", "catalogAttributes", "documentHeadAttributes",
        "documentTableAttributes", "totalMetadataAttributes",
    ):
        if key in counts and not _metadata_nonnegative_int(counts[key], _METADATA_ATTRIBUTE_LIMIT):
            raise SourceError("metadata inventory lacks bounded counts")
    catalogs, documents = data.get("catalogs"), data.get("documents")
    constants = data.get("constants", [])
    if not isinstance(catalogs, list) or not isinstance(documents, list) or not isinstance(constants, list):
        raise SourceError("invalid metadata inventory")
    if len(constants) > _METADATA_ATTRIBUTE_LIMIT:
        raise SourceError("metadata inventory lacks bounded counts")
    if len(catalogs) != counts["catalogs"] or len(documents) != counts["documents"]:
        raise SourceError("metadata inventory counts mismatch")
    if "constants" in counts and len(constants) != counts["constants"]:
        raise SourceError("metadata inventory counts mismatch")
    attribute_count = 0
    constant_count = 0
    for constant in constants:
        if not _metadata_attr(constant):
            raise SourceError("invalid metadata inventory")
        constant_count += 1
    for catalog in catalogs:
        ok, count = _metadata_object(catalog, document=False)
        if not ok:
            raise SourceError("invalid metadata inventory")
        attribute_count += count
    for document in documents:
        ok, count = _metadata_object(document, document=True)
        if not ok:
            raise SourceError("invalid metadata inventory")
        attribute_count += count
    if attribute_count != counts["attributes"]:
        raise SourceError("metadata inventory counts mismatch")
    if "catalogAttributes" in counts:
        actual = sum(len(catalog["attributes"]) for catalog in catalogs)
        if counts["catalogAttributes"] != actual:
            raise SourceError("metadata inventory counts mismatch")
    if "documentHeadAttributes" in counts:
        actual = sum(len(document["headAttributes"]) for document in documents)
        if counts["documentHeadAttributes"] != actual:
            raise SourceError("metadata inventory counts mismatch")
    if "documentTableAttributes" in counts:
        actual = sum(len(document["tableAttributes"]) for document in documents)
        if counts["documentTableAttributes"] != actual:
            raise SourceError("metadata inventory counts mismatch")
    if "totalMetadataAttributes" in counts and counts["totalMetadataAttributes"] != attribute_count + constant_count:
        raise SourceError("metadata inventory counts mismatch")
    other_sections = data.get("otherSections")
    if not isinstance(other_sections, dict) or set(other_sections) != _METADATA_SECTION_KEYS:
        raise SourceError("invalid metadata inventory")
    for indicator in other_sections.values():
        if not _metadata_section_indicator(indicator):
            raise SourceError("invalid metadata inventory")
    if "sectionCoverage" in data:
        coverage = data["sectionCoverage"]
        if not isinstance(coverage, dict) or set(coverage) != _METADATA_SECTION_COVERAGE_KEYS:
            raise SourceError("invalid metadata inventory")
        for key, indicator in coverage.items():
            if not _metadata_section_coverage_indicator(
                indicator, section_names=key in _METADATA_SECTION_COVERAGE_KEYS_WITH_NAMES,
            ):
                raise SourceError("invalid metadata inventory")


class SourceTransport(Protocol):
    """The fixed JSON contract can be carried by SSH or a later local tunnel."""

    def exchange(self, source: dict, payload: dict, timeout: int) -> dict: ...


class SSHExecTransport:
    """Current proof transport; no COM/headless success is implied."""

    def _argv(self, source: dict, remote_command: str) -> list[str]:
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if source.get("identityFile"):
            argv.extend(["-F", "/dev/null", "-o", "IdentitiesOnly=yes",
                         "-o", "ForwardAgent=no", "-o", "PasswordAuthentication=no",
                         "-o", "KbdInteractiveAuthentication=no",
                         "-o", "PreferredAuthentications=publickey", "-o", "UpdateHostKeys=no",
                         "-i", source["identityFile"]])
        if source.get("hostKeyAlias"):
            if not isinstance(source["hostKeyAlias"], str) or not _SSH_HOST_TOKEN.fullmatch(source["hostKeyAlias"]):
                raise SourceError("invalid SSH host key alias")
            argv.extend(["-o", f"HostKeyAlias={source['hostKeyAlias']}"])
        argv.extend(["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o",
                     "ServerAliveCountMax=1", "-l", source["user"], source["host"],
                     remote_command])
        return argv

    def exchange(self, source: dict, payload: dict, timeout: int) -> dict:
        argv = self._argv(source, source["remoteCommand"])
        started_ns = time.perf_counter_ns()
        with _source_exchange_lock(source):
            try:
                completed = subprocess.run(
                    argv,
                    input=json.dumps(payload, ensure_ascii=False) + "\n", text=True,
                    capture_output=True, timeout=min(max(timeout, 1), _REQUEST_TIMEOUT_MAX), check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SourceError("SSH transport unavailable or timed out",
                                  code="SOURCE_TRANSPORT_FAILURE", stage="transport") from exc
        if completed.returncode != 0:
            raise SourceError("Windows adapter or SSH returned an error",
                              code="SOURCE_TRANSPORT_FAILURE", stage="transport")
        response_bytes = len(completed.stdout.encode("utf-8"))
        if response_bytes > 8_000_000:
            raise SourceError("Windows response exceeds size limit")
        try:
            answer = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError("Windows response is not one JSON object") from exc
        if not isinstance(answer, dict):
            raise SourceError("Windows response is not one JSON object")
        answer["_bridgeTransportMetrics"] = {
            "roundTripElapsedMs": min(600_000, max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)),
            "responseJsonUtf8Bytes": response_bytes,
        }
        return answer

    def health(self, source: dict, timeout: int) -> dict:
        argv = self._argv(source, _TRANSPORT_IDENTITY_COMMAND)
        try:
            completed = subprocess.run(
                argv, input="", text=True, capture_output=True,
                timeout=min(max(timeout, 1), 60), check=False,
            )
        except subprocess.TimeoutExpired:
            return _transport_error("SSH_TIMEOUT", "SSH transport check timed out")
        except OSError:
            return _transport_error("SSH_UNAVAILABLE", "SSH client is unavailable")
        if completed.returncode != 0:
            return _transport_error(_classify_ssh_failure(completed.stderr),
                                    "SSH transport check failed before any 1C command ran",
                                    exit_code=completed.returncode)
        return _identity_health_result(source, completed.stdout)


def _transport_error(code: str, message: str, *, exit_code: int | None = None) -> dict:
    error = {"code": code, "message": message}
    if exit_code is not None:
        error["exitCode"] = exit_code
    return {"schemaVersion": "1.0", "operation": "transport_health",
            "status": "error", "transport": "ssh", "error": error}


def _classify_ssh_failure(stderr: str) -> str:
    text = (stderr or "").casefold()
    if "host key verification failed" in text or "known_hosts" in text:
        return "SSH_HOST_KEY_UNVERIFIED"
    if "permission denied" in text or "publickey" in text:
        return "SSH_AUTH_FAILED"
    if "connection refused" in text:
        return "SSH_PORT_CLOSED"
    if "timed out" in text or "timeout" in text:
        return "SSH_CONNECT_TIMEOUT"
    return "SSH_COMMAND_FAILED"


def _identity_health_result(source: dict, stdout: str) -> dict:
    identity = stdout.strip()
    if not identity or "\n" in identity or "\r" in identity or len(identity) > 512:
        return _transport_error("SSH_IDENTITY_OUTPUT_INVALID",
                                "SSH identity command returned an unexpected response")
    match = _WINDOWS_WHOAMI.fullmatch(identity)
    if match is None:
        return _transport_error("SSH_IDENTITY_OUTPUT_INVALID",
                                "SSH identity command returned an unexpected response")
    observed_user = match.group(1).casefold()
    configured_user = str(source.get("user", "")).casefold()
    matches = bool(configured_user) and observed_user == configured_user
    if not matches:
        result = _transport_error("UNEXPECTED_WINDOWS_IDENTITY",
                                  "SSH authenticated as an unexpected Windows identity")
    else:
        result = {"schemaVersion": "1.0", "operation": "transport_health",
                  "status": "ok", "transport": "ssh"}
    result["checks"] = {
        "batchMode": True,
        "strictHostKeyChecking": True,
        "identityFileConfigured": bool(source.get("identityFile")),
        "remoteCommand": "fixed-windows-identity",
        "oneCAccess": False,
    }
    result["data"] = {
        "authenticated": True,
        "identityMatchesConfiguredUser": matches,
    }
    return result


def _bounded_offset(value: str | None, maximum: int) -> bool:
    return (
        value is None
        or (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and len(value) <= 8
            and int(value) <= maximum
        )
    )


def _validate_page_coverage(data: dict, *, expected_statuses: set[str] | None = None) -> None:
    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        raise SourceError("source page lacks coverage status")
    status = coverage.get("status")
    allowed = expected_statuses or {
        "configured-fields-unverified",
        "metadata-driven-unverified",
        "partial-lines-required",
        "partial-unsupported-values",
        "document-not-found",
        "provisional-constant-context",
        "standard-fields-only-unverified",
        "operation-not-found",
        "unsupported-constant-context",
        "metadata-linked-complete-unverified",
        "unsupported-periodic-context",
        "partial-owner-scan-budget",
        "presence-page-unverified",
    }
    if not isinstance(status, str) or status not in allowed:
        raise SourceError("source page lacks coverage status")
    unsupported = coverage.get("unsupported")
    if not isinstance(unsupported, list) or len(unsupported) > _METADATA_ATTRIBUTE_LIMIT:
        raise SourceError("source page lacks unsupported status")
    for item in unsupported:
        if not isinstance(item, dict):
            raise SourceError("source page lacks unsupported status")
        if not isinstance(item.get("reason"), str) or not item["reason"]:
            raise SourceError("source page lacks unsupported status")


def _validate_row_page(data: object, *, limit: int, cursor_maximum: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list) or len(data["rows"]) > limit:
        raise SourceError("invalid source page")
    if not isinstance(data.get("hasMore"), bool):
        raise SourceError("source page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not _bounded_offset(next_cursor, cursor_maximum) or next_cursor is None:
            raise SourceError("source page lacks nextCursor")
    elif next_cursor is not None:
        raise SourceError("source page has unexpected nextCursor")
    _validate_optional_adapter_elapsed(data)
    _validate_page_coverage(data)


def _validate_optional_adapter_elapsed(data: dict) -> None:
    if ("adapterElapsedMs" in data
            and not _metadata_nonnegative_int(data["adapterElapsedMs"], 600_000)):
        raise SourceError("source page has invalid adapterElapsedMs")


def _validate_document_presence_page(data: object, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise SourceError("invalid document presence page")
    scanned = data.get("documentsScanned")
    if type(scanned) is not int or not 0 <= scanned <= limit:
        raise SourceError("document presence page lacks bounded scan count")
    if len(data["groups"]) > scanned:
        raise SourceError("document presence page has too many groups")
    if not isinstance(data.get("hasMore"), bool):
        raise SourceError("document presence page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not _bounded_offset(next_cursor, 10_000_000) or next_cursor is None:
            raise SourceError("document presence page lacks nextCursor")
    elif next_cursor is not None:
        raise SourceError("document presence page has unexpected nextCursor")
    for group in data["groups"]:
        if not isinstance(group, dict):
            raise SourceError("invalid document presence group")
        if not isinstance(group.get("documentType"), str) or not _identifier(group["documentType"]):
            raise SourceError("invalid document presence group")
        if type(group.get("count")) is not int or not 1 <= group["count"] <= limit:
            raise SourceError("invalid document presence group")
        for key in ("postedCount", "unpostedCount", "unknownPostedCount"):
            if type(group.get(key)) is not int or not 0 <= group[key] <= group["count"]:
                raise SourceError("invalid document presence group")
        if group["postedCount"] + group["unpostedCount"] + group["unknownPostedCount"] != group["count"]:
            raise SourceError("invalid document presence group")
        for key in ("firstDate", "lastDate"):
            try:
                valid_date = date.fromisoformat(_date(group.get(key))).isoformat()
            except (TypeError, ValueError) as exc:
                raise SourceError("invalid document presence group") from exc
            if valid_date != group[key]:
                raise SourceError("invalid document presence group")
        presentation = group.get("presentation")
        if presentation is not None and not isinstance(presentation, str):
            raise SourceError("invalid document presence group")
        metadata = group.get("metadata")
        if not isinstance(metadata, dict) or set(metadata) != {
            "known", "id", "headerAttributeCount", "tableAttributeCount",
        }:
            raise SourceError("invalid document presence group")
        if type(metadata["known"]) is not bool:
            raise SourceError("invalid document presence group")
        if metadata["id"] is not None and (
            not isinstance(metadata["id"], str)
            or not metadata["id"].isascii()
            or not metadata["id"].isdecimal()
        ):
            raise SourceError("invalid document presence group")
        for key in ("headerAttributeCount", "tableAttributeCount"):
            if not _metadata_nonnegative_int(metadata[key], _METADATA_ATTRIBUTE_LIMIT):
                raise SourceError("invalid document presence group")
    _validate_page_coverage(data, expected_statuses={"presence-page-unverified"})


def _validate_line_page(data: object, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or len(data["lines"]) > limit:
        raise SourceError("invalid source line page")
    if not isinstance(data.get("hasMore"), bool):
        raise SourceError("source line page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not isinstance(next_cursor, dict) or set(next_cursor) != {"documentCursor", "lineCursor"}:
            raise SourceError("source line page lacks nextCursor")
        if (next_cursor["documentCursor"] is None
                or next_cursor["lineCursor"] is None
                or not _bounded_offset(next_cursor["documentCursor"], 10_000_000)
                or not _bounded_offset(next_cursor["lineCursor"], 1_000_000)):
            raise SourceError("source line page lacks nextCursor")
    elif next_cursor is not None:
        raise SourceError("source line page has unexpected nextCursor")
    _validate_optional_adapter_elapsed(data)
    _validate_page_coverage(data)


def _validate_line_batch(data: object, inputs: list[dict], *, document_type: str,
                         period: dict[str, str]) -> None:
    if (not isinstance(data, dict) or set(data) != {"items", "adapterElapsedMs"}
            or not _metadata_nonnegative_int(data["adapterElapsedMs"], 600_000)):
        raise SourceError("invalid source line batch")
    results = data["items"]
    if not isinstance(results, list) or len(results) != len(inputs):
        raise SourceError("invalid source line batch")
    error_codes = {
        "DOCUMENT_READ_FAILED", "LINE_READ_FAILED", "UNSUPPORTED_VALUE",
        "ITEM_READ_FAILED",
    }
    for result, requested in zip(results, inputs, strict=True):
        if not isinstance(result, dict):
            raise SourceError("invalid source line batch item")
        common = {"documentCursor", "lineCursor", "limit", "status"}
        if (not isinstance(result.get("documentCursor"), str)
                or not isinstance(result.get("lineCursor"), str)
                or type(result.get("limit")) is not int
                or result.get("documentCursor") != requested["documentCursor"]
                or result.get("lineCursor") != requested["lineCursor"]
                or result.get("limit") != requested["limit"]):
            raise SourceError("source line batch changed an input cursor")
        if result.get("status") == "ok":
            if set(result) != (common | {"data"}):
                raise SourceError("invalid successful source line batch item")
            _validate_line_page(result["data"], limit=requested["limit"])
            page = result["data"]
            if (page.get("kind") != "document_lines" or page.get("type") != document_type
                    or page.get("period") != period):
                raise SourceError("source line batch returned a page outside the requested scope")
            document = page.get("document")
            if document is None:
                coverage = page.get("coverage")
                not_found_keys = {
                    "kind", "type", "document", "lines", "nextCursor", "hasMore",
                    "period", "snapshotStatus", "stableIdentity", "coverage",
                }
                if (set(page) != not_found_keys or not isinstance(coverage, dict)
                        or coverage.get("status") != "document-not-found"
                        or page.get("lines") != [] or page.get("hasMore")
                        or page.get("nextCursor") is not None):
                    raise SourceError("invalid not-found source line batch item")
                continue
            if not isinstance(document, dict) or document.get("offset") != requested["documentCursor"]:
                raise SourceError("source line batch changed the document cursor")
            document_date = document.get("date")
            try:
                parsed_document_date = date.fromisoformat(document_date) if isinstance(document_date, str) else None
            except ValueError:
                parsed_document_date = None
            if (parsed_document_date is None or parsed_document_date.isoformat() != document_date
                    or not date.fromisoformat(period["from"]) <= parsed_document_date
                    < date.fromisoformat(period["toExclusive"])):
                raise SourceError("source line batch returned a document outside the requested period")
            next_cursor = page.get("nextCursor")
            if page.get("hasMore") and (
                next_cursor["documentCursor"] != requested["documentCursor"]
                or next_cursor["lineCursor"] != str(
                    int(requested["lineCursor"]) + len(page["lines"])
                )
            ):
                raise SourceError("source line batch returned an invalid next cursor")
        elif result.get("status") == "error":
            if set(result) != (common | {"error"}):
                raise SourceError("invalid failed source line batch item")
            error = result["error"]
            if (not isinstance(error, dict) or set(error) != {"code"}
                    or error["code"] not in error_codes):
                raise SourceError("invalid source line batch error")
        else:
            raise SourceError("invalid source line batch status")


def _validate_entry_page(data: object, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or len(data["entries"]) > limit:
        raise SourceError("invalid source entry page")
    if not isinstance(data.get("hasMore"), bool):
        raise SourceError("source entry page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not isinstance(next_cursor, dict) or set(next_cursor) != {"operationCursor", "entryCursor"}:
            raise SourceError("source entry page lacks nextCursor")
        if (next_cursor["operationCursor"] is None
                or next_cursor["entryCursor"] is None
                or not _bounded_offset(next_cursor["operationCursor"], 10_000_000)
                or not _bounded_offset(next_cursor["entryCursor"], 1_000_000)):
            raise SourceError("source entry page lacks nextCursor")
    elif next_cursor is not None:
        raise SourceError("source entry page has unexpected nextCursor")
    _validate_page_coverage(data)


def _validate_periodic_history_page(data: object, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list) or len(data["rows"]) > limit:
        raise SourceError("invalid periodic history page")
    if not isinstance(data.get("target"), dict):
        raise SourceError("periodic history page lacks metadata target")
    if not isinstance(data.get("hasMore"), bool):
        raise SourceError("periodic history page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not isinstance(next_cursor, dict) or set(next_cursor) not in (
            {"historyCursor"}, {"ownerCursor", "historyCursor"},
        ):
            raise SourceError("periodic history page lacks nextCursor")
        if (not _bounded_offset(next_cursor.get("historyCursor"), 1_000_000)
                or next_cursor.get("historyCursor") is None):
            raise SourceError("periodic history page lacks nextCursor")
        if "ownerCursor" in next_cursor and (
            not _bounded_offset(next_cursor.get("ownerCursor"), 10_000_000)
            or next_cursor.get("ownerCursor") is None
        ):
            raise SourceError("periodic history page lacks nextCursor")
    elif next_cursor is not None:
        raise SourceError("periodic history page has unexpected nextCursor")
    _validate_page_coverage(data, expected_statuses={
        "metadata-linked-complete-unverified",
        "partial-unsupported-values",
        "unsupported-periodic-context",
        "partial-owner-scan-budget",
    })
    coverage = data["coverage"]
    if not isinstance(coverage.get("metadataLink"), dict):
        raise SourceError("periodic history page lacks metadata link")


def _validate_source_schema(data: object) -> None:
    if not isinstance(data, dict):
        raise SourceError("invalid source schema")
    for key in ("datasets", "supportedKinds", "unsupportedKinds", "readOperations"):
        if not isinstance(data.get(key), list):
            raise SourceError("invalid source schema")
    provisional = data.get("provisionalKinds", [])
    if not isinstance(provisional, list):
        raise SourceError("invalid source schema")
    if "constant" not in data["supportedKinds"] and "constant" not in provisional:
        raise SourceError("source schema lacks explicit constant support")
    for kind in ("operation", "operationEntry"):
        if kind not in data["supportedKinds"]:
            raise SourceError("source schema lacks explicit operation support")
    for operation in (
        "document_presence", "read_page", "read_documents_page", "read_document_lines_page",
        "read_constants_page", "read_operations_page", "read_operation_entries_page",
        "read_periodic_history_page",
    ):
        if operation not in data["readOperations"]:
            raise SourceError("source schema lacks explicit read operations")
    native_ops = {"acquire_document_identity", "read_document_by_native"}
    if native_ops & set(data["readOperations"]):
        if (not native_ops <= set(data["readOperations"]) or not _source_namespace(data.get("sourceNamespace"))
                or data.get("nativeDocument") != {"metadataKind": "document", "seedCursorMax": 100,
                    "lineCursorMax": 10_000, "lineLimitMax": 100, "fieldLimitPerSection": 32,
                    "identityScope": _DOCUMENT_IDENTITY_SCOPE, "historyLinkStatus": "unsupported-hold",
                    "historyLinkReason": "periodic-document-native-transition-unimplemented"}):
            raise SourceError("source schema native document contract mismatch")
    if not data["unsupportedKinds"]:
        raise SourceError("source schema lacks unsupported kinds")


def _validate_reference_identity_probe(answer: dict, payload: dict) -> None:
    data = answer.get("data")
    if not isinstance(data, dict) or answer.get("sourceAlias") != payload["sourceAlias"]:
        raise SourceError("identity probe source mismatch")
    if (data.get("kind") != "reference_identity_probe" or data.get("type") != payload["type"]
            or data.get("seedCursor") != payload["cursor"]
            or data.get("stableIdentity") != "unverified" or data.get("snapshotStatus") != "none"
            or data.get("roundtripStatus") != "matched-selected"
            or data.get("identityScope") != "same-information-base-only; base continuity and lifetime unverified"):
        raise SourceError("identity probe scope or proof mismatch")
    metadata_id = data.get("catalogMetadataId")
    if not isinstance(metadata_id, str) or not re.fullmatch(r"[0-9]+", metadata_id):
        raise SourceError("identity probe catalog metadata missing")
    token = data.get("nativeReference")
    if (not isinstance(token, str) or not token.strip() or len(token) > 4096
            or token != data.get("resolvedNativeReference")):
        raise SourceError("identity probe roundtrip mismatch")
    for key in ("seed", "resolved"):
        row = data.get(key)
        if (not isinstance(row, dict) or row.get("valueKind") != "reference"
                or row.get("nativeReference") != token or row.get("selected") is not True
                or row.get("nativeIdentityStatus") != "unverified"
                or row.get("refTypeId") != metadata_id):
            raise SourceError("identity probe reference mismatch")
    # A token match alone does not justify returning another card's scalars.
    for field in ("code", "description", "fullCode", "fullDescription", "isGroup", "empty"):
        if data["seed"].get(field) != data["resolved"].get(field):
            raise SourceError("identity probe scalar mismatch")
    if not isinstance(data.get("attributes"), dict) or not isinstance(data.get("unsupported"), list):
        raise SourceError("identity probe result incomplete")


def _native_token(value: object) -> bool:
    # Same closed-literal input domain as the Windows encoder; never normalize.
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 4096
            and all(ord(c) != 0 and ord(c) not in (0x2028, 0x2029)
                    and ord(c) <= 0xFFFF and not 0xD800 <= ord(c) <= 0xDFFF
                    and not (unicodedata.category(c) == "Cc" and ord(c) > 127)
                    for c in value))


def _validate_addressed_reference(answer: dict, payload: dict) -> None:
    data = answer.get("data")
    if (not isinstance(data, dict) or answer.get("sourceAlias") != payload["sourceAlias"]
            or data.get("kind") != "reference_by_native" or data.get("type") != payload["type"]
            or data.get("catalogMetadataId") != payload["catalogMetadataId"]
            or data.get("observedCatalogType") != payload["type"]
            or data.get("nativeReference") != payload["nativeReference"]
            or data.get("roundtripStatus") != "matched-typed-selected"
            or data.get("stableIdentity") != "unverified" or data.get("snapshotStatus") != "none"
            or data.get("identityScope") != "same-information-base-only; base continuity and lifetime unverified"
            or data.get("readContext") != {"asOf": payload["asOf"], "useDate": "applied",
                                           "selection": "FindItem", "periodicStatus": "date-context-applied-unverified"}):
        raise SourceError("addressed reference scope or context mismatch")
    # Old Catalog responses remain readable, but cannot satisfy a scoped request.
    if "sourceNamespace" in payload or "sourceNamespace" in data or "sourceNamespaceProof" in data:
        namespace = data.get("sourceNamespace")
        expected = payload.get("sourceNamespace")
        if (not _source_namespace(namespace) or (expected is not None and namespace != expected)
                or data.get("sourceNamespaceProof") != {
                    "scheme": "configured-alias-windows-path-sha256-v1",
                    "expected": expected, "observed": namespace,
                    "status": "matched-request" if expected is not None else "observed-unbound"}):
            raise SourceError("addressed reference producer source scope mismatch")
    row = data.get("reference")
    if (not isinstance(row, dict) or row.get("valueKind") != "reference"
            or row.get("nativeReference") != payload["nativeReference"]
            or row.get("selected") is not True or row.get("refTypeId") != payload["catalogMetadataId"]
            or row.get("nativeIdentityStatus") != "unverified"):
        raise SourceError("addressed reference lacks selected native proof")
    attrs, relations, unsupported = data.get("attributes"), data.get("relations"), data.get("unsupported")
    if (not isinstance(attrs, dict) or set(attrs) != set(payload["attributeNames"])
            or not isinstance(relations, dict) or not set(relations) <= set(attrs)
            or not isinstance(unsupported, list)):
        raise SourceError("addressed attribute contract mismatch")
    incomplete = bool(unsupported)
    for name, relation in relations.items():
        value = attrs[name]
        if (not isinstance(value, dict) or value.get("valueKind") not in ("reference", "unsupported")
                or not isinstance(relation, dict)
                or relation.get("status") not in ("native-typed-unverified", "unsupported-hold")
                or (value.get("valueKind") != "reference" and relation.get("status") != "unsupported-hold")):
            raise SourceError("addressed relation scope mismatch")
        incomplete |= relation["status"] == "unsupported-hold"
    for name, value in attrs.items():
        if isinstance(value, dict):
            if value.get("valueKind") == "reference":
                relation = relations.get(name)
                if not isinstance(relation, dict):
                    raise SourceError("addressed reference relation missing")
                if relation.get("status") == "native-typed-unverified":
                    if (not _native_token(value.get("nativeReference")) or value.get("selected") is not True
                            or value.get("empty") is not False or value.get("nativeIdentityStatus") != "unverified"
                            or relation.get("nativeReference") != value["nativeReference"]
                            or relation.get("catalogMetadataId") != value.get("refTypeId")
                            or not isinstance(relation.get("catalogMetadataId"), str)
                            or not re.fullmatch(r"[1-9][0-9]{0,9}", relation["catalogMetadataId"])
                            or not isinstance(relation.get("catalogType"), str)
                            or not _identifier(relation["catalogType"])
                            or relation.get("observedCatalogType") != relation["catalogType"]):
                        raise SourceError("addressed relation native type proof missing")
                elif relation.get("status") == "unsupported-hold":
                    incomplete = True
                else:
                    raise SourceError("addressed relation status invalid")
            else:
                # A complex/presentation-only value is never a complete required scalar.
                incomplete = True
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise SourceError("addressed attribute value invalid")
    if data.get("readStatus") != ("partial-hold" if incomplete else "complete-unverified"):
        raise SourceError("addressed read completeness mismatch")


_DOCUMENT_IDENTITY_SCOPE = "same-information-base-only; base continuity and lifetime unverified"
_NATIVE_DOCUMENT_OPERATIONS = ("acquire_document_identity", "read_document_by_native")


def _source_namespace(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def _document_row(row: object, payload: dict, token: str) -> None:
    if (not isinstance(row, dict) or row.get("valueKind") != "document"
            or row.get("selected") is not True or row.get("nativeReference") != token
            or row.get("documentType") != payload["documentType"]
            or row.get("documentMetadataId") != payload["documentMetadataId"]
            or not isinstance(row.get("number"), str)
            or (row.get("posted") is not None and type(row["posted"]) is not bool)):
        raise SourceError("native document selected header mismatch")
    try:
        _date(row.get("date"))
    except (ValueError, TypeError):
        raise SourceError("native document date mismatch") from None


def _document_fields(attrs: object, relations: object, names: list[str], namespace: str) -> bool:
    if (not isinstance(attrs, dict) or set(attrs) != set(names) or not isinstance(relations, dict)
            or not set(relations) <= set(attrs)):
        raise SourceError("native document requested metadata fields mismatch")
    partial = False
    for name, value in attrs.items():
        relation = relations.get(name)
        if isinstance(value, dict):
            partial |= value.get("valueKind") != "reference"
            if value.get("valueKind") == "reference" and relation is None:
                raise SourceError("native document reference relation missing")
        elif value is not None and type(value) not in (str, int, float, bool):
            raise SourceError("native document scalar invalid")
        if relation is not None:
            if (not isinstance(relation, dict) or not isinstance(value, dict)
                    or relation.get("status") not in ("native-typed-unverified", "unsupported-hold")):
                raise SourceError("native document relation invalid")
            if relation["status"] == "unsupported-hold":
                if not isinstance(relation.get("reason"), str) or not relation["reason"]:
                    raise SourceError("native document relation HOLD reason missing")
                partial = True
            elif (value.get("valueKind") != "reference" or value.get("selected") is not True
                    or value.get("empty") is not False or not _native_token(value.get("nativeReference"))
                    or relation.get("nativeReference") != value["nativeReference"]
                    or relation.get("metadataKind") != "reference"
                    or relation.get("sourceNamespace") != namespace
                    or relation.get("catalogMetadataId") != value.get("refTypeId")
                    or not isinstance(relation.get("catalogMetadataId"), str)
                    or not re.fullmatch(r"[1-9][0-9]{0,9}", relation["catalogMetadataId"])
                    or not isinstance(relation.get("catalogType"), str)
                    or not _identifier(relation["catalogType"])
                    or relation.get("observedCatalogType") != relation["catalogType"]
                    or value.get("nativeIdentityStatus") != "unverified"):
                raise SourceError("native document relation proof mismatch")
    return partial


def _validate_native_document(answer: dict, payload: dict) -> None:
    data = answer.get("data")
    acquisition = payload["operation"] == "acquire_document_identity"
    if (not isinstance(data, dict) or answer.get("sourceAlias") != payload["sourceAlias"]
            or data.get("kind") != ("document_identity" if acquisition else "document_by_native")
            or data.get("metadataKind") != "document"
            or data.get("documentType") != payload["documentType"]
            or data.get("observedDocumentType") != payload["documentType"]
            or data.get("documentMetadataId") != payload["documentMetadataId"]
            or not _source_namespace(data.get("sourceNamespace"))
            or data.get("roundtripStatus") != "matched-typed-selected"
            or data.get("stableIdentity") != "unverified" or data.get("snapshotStatus") != "none"
            or data.get("identityScope") != _DOCUMENT_IDENTITY_SCOPE
            or data.get("historyLinkStatus") != "unsupported-hold"
            or data.get("historyLinkReason") != "periodic-document-native-transition-unimplemented"):
        raise SourceError("native document scope or typed proof mismatch")
    token = data.get("nativeReference")
    if not _native_token(token):
        raise SourceError("native document token invalid")
    if acquisition:
        if (data.get("seedCursor") != payload["documentCursor"] or data.get("period") != payload["period"]
                or data.get("resolvedNativeReference") != token):
            raise SourceError("native document acquisition roundtrip mismatch")
        for key in ("seed", "resolved"):
            _document_row(data.get(key), payload, token)
        if data["seed"] != data["resolved"]:
            raise SourceError("native document acquisition header changed")
        if not payload["period"]["from"] <= data["seed"]["date"] < payload["period"]["toExclusive"]:
            raise SourceError("native document acquisition date outside requested period")
        return
    if data.get("sourceNamespace") != payload["sourceNamespace"] or token != payload["nativeReference"]:
        raise SourceError("native document saved source tuple mismatch")
    _document_row(data.get("document"), payload, token)
    if (data.get("lineCursor") != payload["lineCursor"] or type(data.get("hasMore")) is not bool
            or not isinstance(data.get("lines"), list) or len(data["lines"]) > payload["limit"]
            or not isinstance(data.get("unsupported"), list)
            or data.get("lineNumberSemantics") != "ordinal-traversal"):
        raise SourceError("native document bounded lines invalid")
    partial = bool(data["unsupported"]) or data["document"]["posted"] is None
    partial |= _document_fields(data.get("attributes"), data.get("relations"), payload["attributeNames"], data["sourceNamespace"])
    stored_numbers = set()
    for index, line in enumerate(data["lines"]):
        if (not isinstance(line, dict) or type(line.get("lineNumber")) is not int
                or line["lineNumber"] != int(payload["lineCursor"]) + index + 1):
            raise SourceError("native document ordinal line identity mismatch")
        stored = line.get("sourceLineNumber")
        if line.get("sourceLineNumberStatus") == "observed":
            if type(stored) is not int or stored < 1 or stored in stored_numbers:
                raise SourceError("native document stored line identity invalid")
            stored_numbers.add(stored)
        elif line.get("sourceLineNumberStatus") != "unavailable" or stored is not None:
            raise SourceError("native document stored line identity evidence mismatch")
        partial |= _document_fields(line.get("attributes"), line.get("relations"), payload["tableAttributeNames"], data["sourceNamespace"])
    declared = data.get("declaredLineCount")
    if declared is not None and (type(declared) is not int or not 0 <= declared <= 1_000_000):
        raise SourceError("native document declared line count invalid")
    if declared is None:
        partial = True
    elif ((not data["hasMore"] and int(payload["lineCursor"]) + len(data["lines"]) != declared)
          or (data["hasMore"] and int(payload["lineCursor"]) + len(data["lines"]) >= declared)):
        partial = True
    expected_next = str(int(payload["lineCursor"]) + len(data["lines"])) if data["hasMore"] else None
    if (data.get("nextLineCursor") != expected_next
            or (data["hasMore"] and (len(data["lines"]) != payload["limit"] or int(expected_next) > 10_000))):
        raise SourceError("native document line continuation mismatch")
    status = "partial-hold" if partial else ("page-complete-unverified" if data["hasMore"] else "complete-unverified")
    if data.get("readStatus") != status:
        raise SourceError("native document requested-field completeness mismatch")

def request(source: dict, operation: str, *, source_alias: str,
            document_type: str | None = None, from_date: str | None = None,
            to_exclusive: str | None = None, cursor: str | None = None,
            limit: int = 100, timeout: int | None = None,
            dataset_kind: str | None = None, dataset_type: str | None = None,
            catalog_metadata_id: str | None = None, native_reference: str | None = None,
            document_metadata_id: str | None = None, source_namespace: str | None = None,
            table_attribute_names: list[str] | None = None,
            as_of: str | None = None, attribute_names: list[str] | None = None,
            document_cursor: str | None = None, line_cursor: str | None = None,
            operation_cursor: str | None = None, entry_cursor: str | None = None,
            periodic_kind: str | None = None, periodic_type: str | None = None,
            periodic_attribute: str | None = None, owner_cursor: str | None = None,
            history_cursor: str | None = None,
            line_items: list[dict] | None = None,
            transport: SourceTransport | None = None) -> dict:
    if operation not in (
        "health", "schema", "metadata_inventory", "document_presence", "probe_reference_identity", "read_reference_by_native",
        "acquire_document_identity", "read_document_by_native",
        "read_documents_page", "read_page",
        "read_document_lines_page", "read_document_lines_batch",
        "read_constants_page", "read_operations_page",
        "read_operation_entries_page", "read_periodic_history_page",
    ):
        raise ValueError("unsupported source operation")
    if not _ALIAS.fullmatch(source_alias):
        raise ValueError("invalid source alias")
    payload = {"schemaVersion": "1.0", "requestId": str(uuid.uuid4()),
               "operation": operation, "sourceAlias": source_alias}
    validated_line_items: list[dict] | None = None
    validated_line_scope: dict[str, str] | None = None
    if operation in _NATIVE_DOCUMENT_OPERATIONS:
        if (dataset_kind is not None or dataset_type is not None or cursor is not None
                or catalog_metadata_id is not None or as_of is not None or line_items is not None
                or operation_cursor is not None or entry_cursor is not None or periodic_kind is not None
                or periodic_type is not None or periodic_attribute is not None
                or owner_cursor is not None or history_cursor is not None):
            raise ValueError("native document requires its typed request only")
        if not isinstance(document_type, str) or not _identifier(document_type):
            raise ValueError("invalid document type")
        if not isinstance(document_metadata_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", document_metadata_id):
            raise ValueError("invalid document metadata ID")
        payload.update(metadataKind="document", documentType=document_type, documentMetadataId=document_metadata_id)
        if operation == "acquire_document_identity":
            if (native_reference is not None or source_namespace is not None or attribute_names is not None
                    or table_attribute_names is not None or line_cursor is not None
                    or document_cursor is None or not _bounded_offset(document_cursor, 100)):
                raise ValueError("document acquisition accepts only a bounded seed")
            start, end = _date(from_date), _date(to_exclusive)
            if start >= end:
                raise ValueError("invalid acquisition period")
            payload.update(period={"from": start, "toExclusive": end}, documentCursor=document_cursor)
        else:
            if from_date is not None or to_exclusive is not None or document_cursor is not None:
                raise ValueError("addressed document does not accept date/number/cursor identity")
            if not _native_token(native_reference) or not _source_namespace(source_namespace):
                raise ValueError("addressed document requires native token and source namespace")
            for names in (attribute_names, table_attribute_names):
                if (not isinstance(names, list) or len(names) > 32
                        or any(not isinstance(n, str) or not _identifier(n) for n in names)
                        or len(set(names)) != len(names)):
                    raise ValueError("document fields must be unique metadata identifiers, at most 32 per section")
            if type(limit) is not int or not 1 <= limit <= 100 or line_cursor is None or not _bounded_offset(line_cursor, 10_000):
                raise ValueError("document lines must be bounded")
            payload.update(nativeReference=native_reference, sourceNamespace=source_namespace,
                           attributeNames=attribute_names, tableAttributeNames=table_attribute_names,
                           lineCursor=line_cursor, limit=limit)
    if operation == "read_reference_by_native":
        if cursor is not None or dataset_kind is not None or from_date is not None or to_exclusive is not None:
            raise ValueError("addressed reference does not accept cursors or page periods")
        if not isinstance(dataset_type, str) or not _identifier(dataset_type):
            raise ValueError("invalid catalog type")
        if not isinstance(catalog_metadata_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", catalog_metadata_id):
            raise ValueError("invalid catalog metadata ID")
        if not _native_token(native_reference):
            raise ValueError("invalid native reference")
        if (not isinstance(attribute_names, list) or len(attribute_names) > 32
                or any(not isinstance(n, str) or not _identifier(n) for n in attribute_names)
                or len(set(attribute_names)) != len(attribute_names)):
            raise ValueError("attribute names must be unique metadata identifiers, at most 32")
        payload.update(type=dataset_type, catalogMetadataId=catalog_metadata_id,
                       nativeReference=native_reference, asOf=_date(as_of), attributeNames=attribute_names)
        if source_namespace is not None:
            if not _source_namespace(source_namespace):
                raise ValueError("invalid source namespace")
            payload["sourceNamespace"] = source_namespace
    if operation == "probe_reference_identity":
        if not isinstance(dataset_type, str) or not _identifier(dataset_type):
            raise ValueError("invalid catalog type")
        if cursor is None or not _bounded_offset(cursor, 100):
            raise ValueError("identity seed cursor must be an offset from 0 to 100")
        payload.update(type=dataset_type, cursor=cursor)
    if operation in (
        "read_documents_page", "read_page", "read_constants_page",
        "read_operations_page", "read_operation_entries_page",
        "read_periodic_history_page",
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
    if operation in ("read_documents_page", "read_page"):
        if not _bounded_offset(cursor, 10_000_000):
            raise ValueError("cursor must be a bounded nonnegative offset")
        if operation == "read_documents_page":
            dataset_kind, dataset_type = "document", document_type
        if dataset_kind not in ("document", "reference"):
            raise ValueError("unsupported dataset kind")
        if not isinstance(dataset_type, str) or not _identifier(dataset_type):
            raise ValueError("invalid dataset type")
        payload.update(cursor=cursor, limit=limit)
        if operation == "read_documents_page":
            payload["documentType"] = dataset_type
        else:
            payload.update(kind=dataset_kind, type=dataset_type)
        if dataset_kind == "document":
            start, end = _date(from_date), _date(to_exclusive)
            if start >= end:
                raise ValueError("period must be nonempty")
            payload["period"] = {"from": start, "toExclusive": end}
    elif operation == "read_document_lines_page":
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if not isinstance(document_type, str) or not _identifier(document_type):
            raise ValueError("invalid document type")
        if not _bounded_offset(document_cursor, 10_000_000) or document_cursor is None:
            raise ValueError("document cursor must be a bounded nonnegative offset")
        if not _bounded_offset(line_cursor, 1_000_000) or line_cursor is None:
            raise ValueError("line cursor must be a bounded nonnegative offset")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        payload.update(documentType=document_type, documentCursor=document_cursor,
                       lineCursor=line_cursor, limit=limit,
                       period={"from": start, "toExclusive": end})
    elif operation == "read_document_lines_batch":
        if not isinstance(document_type, str) or not _identifier(document_type):
            raise ValueError("invalid document type")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        if not isinstance(line_items, list) or not 1 <= len(line_items) <= 10:
            raise ValueError("line batch must contain 1..10 items")
        normalized_items = []
        seen_document_cursors: set[int] = set()
        for item in line_items:
            if not isinstance(item, dict) or set(item) != {"documentCursor", "lineCursor", "limit"}:
                raise ValueError("invalid line batch item")
            item_document_cursor = item["documentCursor"]
            item_line_cursor = item["lineCursor"]
            item_limit = item["limit"]
            if type(item_document_cursor) is int:
                if not 0 <= item_document_cursor <= 10_000_000:
                    raise ValueError("document cursor must be a bounded nonnegative offset")
                item_document_cursor = str(item_document_cursor)
            elif isinstance(item_document_cursor, str) and _bounded_offset(item_document_cursor, 10_000_000):
                item_document_cursor = str(int(item_document_cursor))
            if type(item_line_cursor) is int:
                if not 0 <= item_line_cursor <= 1_000_000:
                    raise ValueError("line cursor must be a bounded nonnegative offset")
                item_line_cursor = str(item_line_cursor)
            elif isinstance(item_line_cursor, str) and _bounded_offset(item_line_cursor, 1_000_000):
                item_line_cursor = str(int(item_line_cursor))
            if (not _bounded_offset(item_document_cursor, 10_000_000)
                    or item_document_cursor is None):
                raise ValueError("document cursor must be a bounded nonnegative offset")
            if not _bounded_offset(item_line_cursor, 1_000_000) or item_line_cursor is None:
                raise ValueError("line cursor must be a bounded nonnegative offset")
            if type(item_limit) is not int or not 1 <= item_limit <= 100:
                raise ValueError("line limit must be 1..100")
            document_offset = int(item_document_cursor)
            if document_offset in seen_document_cursors:
                raise ValueError("document cursors must be unique")
            seen_document_cursors.add(document_offset)
            normalized_items.append({
                "documentCursor": item_document_cursor,
                "lineCursor": item_line_cursor,
                "limit": item_limit,
            })
        validated_line_items = normalized_items
        validated_line_scope = {"from": start, "toExclusive": end}
        payload.update(documentType=document_type, period={"from": start, "toExclusive": end},
                       items=normalized_items)
    elif operation == "read_constants_page":
        if not _bounded_offset(cursor, 10_000):
            raise ValueError("cursor must be a bounded nonnegative offset")
        payload.update(cursor=cursor, limit=limit)
    elif operation == "document_presence":
        if type(limit) is not int or not 1 <= limit <= _DOCUMENT_PRESENCE_LIMIT_MAX:
            raise ValueError("presence limit must be 1..5000")
        if not _bounded_offset(cursor, 10_000_000):
            raise ValueError("cursor must be a bounded nonnegative offset")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        payload.update(cursor=cursor, limit=limit,
                       period={"from": start, "toExclusive": end})
    elif operation == "read_operations_page":
        if not _bounded_offset(cursor, 10_000_000):
            raise ValueError("cursor must be a bounded nonnegative offset")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        payload.update(cursor=cursor, limit=limit,
                       period={"from": start, "toExclusive": end})
    elif operation == "read_operation_entries_page":
        if not _bounded_offset(operation_cursor, 10_000_000) or operation_cursor is None:
            raise ValueError("operation cursor must be a bounded nonnegative offset")
        if not _bounded_offset(entry_cursor, 1_000_000) or entry_cursor is None:
            raise ValueError("entry cursor must be a bounded nonnegative offset")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        payload.update(operationCursor=operation_cursor, entryCursor=entry_cursor,
                       limit=limit, period={"from": start, "toExclusive": end})
    elif operation == "read_periodic_history_page":
        if periodic_kind not in ("constant", "catalog_attribute"):
            raise ValueError("unsupported periodic target kind")
        if not isinstance(periodic_type, str) or not _identifier(periodic_type):
            raise ValueError("invalid periodic target type")
        start, end = _date(from_date), _date(to_exclusive)
        if start >= end:
            raise ValueError("period must be nonempty")
        if not _bounded_offset(history_cursor, 1_000_000):
            raise ValueError("history cursor must be a bounded nonnegative offset")
        payload.update(kind=periodic_kind, type=periodic_type, limit=limit,
                       period={"from": start, "toExclusive": end})
        if history_cursor is not None:
            payload["historyCursor"] = history_cursor
        if periodic_kind == "catalog_attribute":
            if not isinstance(periodic_attribute, str) or not _identifier(periodic_attribute):
                raise ValueError("invalid periodic attribute name")
            if not _bounded_offset(owner_cursor, 10_000_000):
                raise ValueError("owner cursor must be a bounded nonnegative offset")
            payload["attributeName"] = periodic_attribute
            if owner_cursor is not None:
                payload["ownerCursor"] = owner_cursor
        elif owner_cursor is not None or periodic_attribute is not None:
            raise ValueError("constant history does not use owner cursor or attribute name")
    answer = (transport or SSHExecTransport()).exchange(source, payload, _request_timeout(source, timeout))
    transport_metrics = answer.pop("_bridgeTransportMetrics", None)
    if answer.get("schemaVersion") != "1.0" or answer.get("operation") != operation:
        raise SourceError("Windows response contract mismatch")
    if answer.get("requestId") not in (None, payload["requestId"]):
        raise SourceError("Windows response requestId mismatch")
    if answer.get("status") not in ("ok", "error"):
        raise SourceError("Windows response lacks status")
    if answer["status"] == "error":
        remote_error = answer.get("error")
        if not isinstance(remote_error, dict):
            remote_error = {}
        code = str(remote_error.get("code", "REMOTE_ERROR"))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            code = "REMOTE_ERROR"
        if operation in ("probe_reference_identity", "read_reference_by_native", *_NATIVE_DOCUMENT_OPERATIONS) and code not in _PROBE_ERROR_CODES:
            code = "REMOTE_ERROR"
        raise SourceError(f"Windows adapter error: {code}",
                          code=f"SOURCE_ADAPTER_{code}", stage="source_adapter",
                          probe_diagnostic=(remote_error.get("probeDiagnostic")
                                            if operation in ("probe_reference_identity", "read_reference_by_native", *_NATIVE_DOCUMENT_OPERATIONS) else None))
    if operation in _NATIVE_DOCUMENT_OPERATIONS:
        _validate_native_document(answer, payload)
    elif operation == "read_reference_by_native":
        _validate_addressed_reference(answer, payload)
    elif operation == "probe_reference_identity":
        _validate_reference_identity_probe(answer, payload)
    elif operation in ("read_documents_page", "read_page", "read_operations_page"):
        _validate_row_page(answer.get("data"), limit=limit, cursor_maximum=10_000_000)
    elif operation == "document_presence":
        _validate_document_presence_page(answer.get("data"), limit=limit)
    elif operation == "read_constants_page":
        _validate_row_page(answer.get("data"), limit=limit, cursor_maximum=10_000)
    elif operation == "read_document_lines_page":
        _validate_line_page(answer.get("data"), limit=limit)
    elif operation == "read_document_lines_batch":
        _validate_line_batch(answer.get("data"), validated_line_items or [],
                             document_type=document_type or "",
                             period=validated_line_scope or {})
    elif operation == "read_operation_entries_page":
        _validate_entry_page(answer.get("data"), limit=limit)
    elif operation == "read_periodic_history_page":
        _validate_periodic_history_page(answer.get("data"), limit=limit)
    elif operation == "metadata_inventory":
        _validate_metadata_inventory(answer.get("data"))
    elif operation == "schema":
        _validate_source_schema(answer.get("data"))
    if operation in _TIMED_READ_OPERATIONS and isinstance(transport_metrics, dict):
        elapsed = transport_metrics.get("roundTripElapsedMs")
        response_bytes = transport_metrics.get("responseJsonUtf8Bytes")
        if (_metadata_nonnegative_int(elapsed, 600_000)
                and _metadata_nonnegative_int(response_bytes, 8_000_000)):
            answer["bridgeMetrics"] = {
                "roundTripElapsedMs": elapsed,
                "responseJsonUtf8Bytes": response_bytes,
            }
    return answer


def transport_health(source: dict, *, source_alias: str, timeout: int = 15,
                     transport: SSHExecTransport | None = None) -> dict:
    if not _ALIAS.fullmatch(source_alias):
        raise ValueError("invalid source alias")
    result = (transport or SSHExecTransport()).health(source, timeout)
    result["sourceAlias"] = source_alias
    return result
