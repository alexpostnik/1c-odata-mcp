"""Local MCP tools for named 1C 8.3 OData targets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache, wraps
import hashlib
import inspect
import json
import os
import re
import secrets
import threading
from time import perf_counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from mcp.server import MCPServer

from .auth import CredentialError
from .client import (ODataAcceptedResponseError, ODataClient,
                     ODataClientError, ODataHTTPError, ODataTransportError)
from .config import (ConfigError, RuntimeConfig, RuntimeTarget,
                     ensure_secret_free, targets_from_json)
from .counterparty import (validate_counterparty_create, guarded_counterparty_create,
                           target_fingerprint as counterparty_target_fingerprint)
from .diagnostics import (PERFORMANCE_LOGGER, diagnostic_row, diagnostic_stage,
                          increment_search_count, instrument_tool_call,
                          set_row_outcome, set_search_profile)
from .ledger import UncertainWrite, WriteLedger, counterparty_pre_post_result, request_digest
from .metadata import MetadataModel
from .person_mapping import preflight_person_names


_MAX_TOOL_QUERY_CHARS = 4_000
_MAX_PREFIX_CHARS = 128
_MAX_SEARCH_SCAN = 100_000
_MAX_SEARCH_SKIP = 10_000_000
_PROD_WRITE_PROBE_DRY_RUN_TTL_SECONDS = 300
_MAX_SEARCH_BATCH_VALUES = 50
_MAX_SEARCH_BATCH_RESULT_ROWS = 1_000
_MAX_REFERENCE_LOOKUP_KEYS_PER_QUERY = 50
_MAX_REFERENCE_LOOKUP_KEYS_PER_WRITE = 200
_MAX_REFERENCE_LOOKUP_QUERIES_PER_WRITE = 20
_MAX_REFERENCE_KEYED_GETS_PER_WRITE = 50
_IF_MATCH_OPERATIONS = {"patch"}
_PREFLIGHT_DATA_VERSION_OPERATIONS = {"Post", "Unpost"}
_NULL_GUID = "00000000-0000-0000-0000-000000000000"
_MAX_MOVEMENT_EXPECTATIONS = 20
_ACCOUNT_IDENTIFIER_RE = re.compile(
    r"(?:[0-9]+(?:\.[0-9]+)*|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z"
)
_MAX_MOVEMENT_ROWS = 200
_MAX_DOCUMENT_QUEUE_ITEMS = 50
_MAX_DOCUMENT_QUEUE_BYTES = 1_000_000
_MAX_DOCUMENT_QUEUE_CONCURRENCY = 4
_DEFAULT_AMOUNT_FIELDS = ("Amount", "Сумма", "СуммаОборот", "СуммаДт", "СуммаКт")
_DEFAULT_DEBIT_ACCOUNT_FIELDS = (
    "AccountDr",
    "AccountDr_Key",
    "AccountDt",
    "AccountDt_Key",
    "СчетДт",
    "СчетДт_Key",
    "СчетДебета",
    "СчетДебета_Key",
)
_DEFAULT_CREDIT_ACCOUNT_FIELDS = (
    "AccountCr",
    "AccountCr_Key",
    "AccountCt",
    "AccountCt_Key",
    "СчетКт",
    "СчетКт_Key",
    "СчетКредита",
    "СчетКредита_Key",
)
_DEFAULT_ACTIVE_FIELDS = ("Active", "Активность")
_COUNTERPARTY_ENTITY_SET = "Catalog_Контрагенты"
_COUNTERPARTY_CLASSIFICATION_FIELD = "ЮридическоеФизическоеЛицо"
_OBSERVED_COUNTERPARTY_CLASSIFICATIONS = frozenset(
    {"ЮридическоеЛицо", "ФизическоеЛицо"}
)
_WRITE_READINESS_ENV_NAMES = (
    "ONEC83_ENABLE_WRITE",
    "ONEC83_TARGET_KIND",
    "ONEC83_WRITE_BASE_URL",
)
_SESSION_BLOCK_MARKERS = (
    "Начало сеанса с информационной базой запрещено",
    "информационной базой запрещено",
    "занята для обновления",
    "занята для обновления или архивации",
)


def _bounded_int(value: int, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be {minimum}..{maximum}")
    return value


def _safe_text(value: str, label: str, *, max_len: int,
               allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or (not value and not allow_empty)
            or len(value) > max_len):
        raise ValueError(f"invalid {label}")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {label}")
    return value


def _safe_optional_query(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _safe_text(value, label, max_len=_MAX_TOOL_QUERY_CHARS)


def _normalized_key(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("key must be a UUID") from exc


def _shape_of(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        field_names = sorted(str(key) for key in value.keys())
        return {"type": "object", "fieldCount": len(value), "fields": field_names}
    if isinstance(value, list):
        result: dict[str, Any] = {"type": "array", "rowCount": len(value)}
        item_fields: set[str] = set()
        for item in value:
            if isinstance(item, dict):
                item_fields.update(str(key) for key in item.keys())
        if item_fields:
            result["itemFields"] = sorted(item_fields)
        return result
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    return {"type": type(value).__name__}


def _payload_summary(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    fields = sorted(str(key) for key in payload.keys())
    return {
        "fieldCount": len(payload),
        "fields": fields,
        "shape": {str(key): _shape_of(value) for key, value in payload.items()},
    }


def _normalized_base_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/") + "/"


def _scalar_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float, Decimal)):
        text = str(value).strip()
        return text or None
    return None


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        text = str(value).strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _field_list(value: Any, default: tuple[str, ...]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        fields = [value]
    elif isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        fields = list(value)
    else:
        raise ValueError("movement field lists must be nonempty strings or string arrays")
    result = []
    for field in fields:
        result.append(_safe_text(field, "movement field", max_len=128))
    return result


def _path_value(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("/"):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _candidate_text_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = []
        for key in ("Code", "Код", "Description", "Наименование", "Ref_Key"):
            text = _scalar_text(value.get(key))
            if text is not None and text not in values:
                values.append(text)
        return values
    text = _scalar_text(value)
    return [] if text is None else [text]


def _field_text_values(row: dict[str, Any], fields: list[str]) -> list[str]:
    values: list[str] = []
    for field in fields:
        for text in _candidate_text_values(_path_value(row, field)):
            if text not in values:
                values.append(text)
    return values


def _movement_account_matches(expected: str, actual_values: list[str]) -> bool:
    if expected in actual_values:
        return True
    if "-" not in expected or not _ACCOUNT_IDENTIFIER_RE.fullmatch(expected):
        return False
    return any("-" in actual and _ACCOUNT_IDENTIFIER_RE.fullmatch(actual)
               and expected.casefold() == actual.casefold() for actual in actual_values)


def _first_decimal_field(row: dict[str, Any], fields: list[str]) -> tuple[str | None, Decimal | None]:
    for field in fields:
        value = _decimal_value(_path_value(row, field))
        if value is not None:
            return field, value
    return None, None


def _recordset_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("accounting register response is not an object")
    rows = payload.get("RecordSet")
    if isinstance(rows, dict):
        rows = rows.get("results", rows.get("value"))
    if rows is None and isinstance(payload.get("value"), list):
        rows = payload.get("value")
    if not isinstance(rows, list):
        raise ValueError("accounting register response has no RecordSet array")
    if len(rows) > _MAX_MOVEMENT_ROWS:
        raise ValueError(f"accounting register response exceeds {_MAX_MOVEMENT_ROWS} rows")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("accounting register rows must be objects")
    return rows


def _active_row(row: dict[str, Any], fields: list[str], expected_value: Any) -> bool:
    found = False
    for field in fields:
        value = _path_value(row, field)
        if value is None:
            continue
        found = True
        if isinstance(expected_value, bool):
            return value is expected_value
        return str(value) == str(expected_value)
    return True if not found else False


def _row_summary(
    row: dict[str, Any],
    *,
    amount_fields: list[str],
    debit_fields: list[str],
    credit_fields: list[str],
) -> dict[str, Any]:
    amount_field, amount = _first_decimal_field(row, amount_fields)
    result: dict[str, Any] = {
        "fields": sorted(str(key) for key in row.keys()),
        "debitValues": _field_text_values(row, debit_fields),
        "creditValues": _field_text_values(row, credit_fields),
    }
    if amount is not None:
        result["amount"] = _decimal_text(amount)
        result["amountField"] = amount_field
    return result


def _normalize_movement_expectation(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("expected_accounting_movements must be an object")
    register = value.get("register") or value.get("entity_set") or value.get("entitySet")
    if not isinstance(register, str) or not register.startswith("AccountingRegister_"):
        raise ValueError("expected_accounting_movements.register must be an AccountingRegister_* entity set")
    rows = value.get("rows", value.get("expectations"))
    if rows is None:
        rows = [{
            "amount": value.get("amount", value.get("total", value.get("expectedAmount"))),
            "debit": value.get("debit", value.get("debitAccount", value.get("debitAccountValue"))),
            "credit": value.get("credit", value.get("creditAccount", value.get("creditAccountValue"))),
        }]
    if "debitAnyOf" in value:
        raise ValueError("debitAnyOf must be inside an expected movement row")
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_MOVEMENT_EXPECTATIONS:
        raise ValueError(f"expected movement rows must contain 1..{_MAX_MOVEMENT_EXPECTATIONS} entries")
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each expected movement row must be an object")
        amount = _decimal_value(row.get("amount", row.get("total", row.get("expectedAmount"))))
        debit_fields = ("debit", "debitAccount", "debitAccountValue")
        has_debit = any(field in row for field in debit_fields)
        has_alternatives = "debitAnyOf" in row
        if has_debit and has_alternatives:
            raise ValueError("debitAnyOf cannot be combined with scalar debit")
        debit = _scalar_text(row.get("debit", row.get("debitAccount", row.get("debitAccountValue"))))
        debit_any_of = row.get("debitAnyOf")
        if has_alternatives:
            if (not isinstance(debit_any_of, list) or not debit_any_of
                    or any(not isinstance(code, str) or not _ACCOUNT_IDENTIFIER_RE.fullmatch(code)
                           for code in debit_any_of)
                    or len({code.casefold() for code in debit_any_of}) != len(debit_any_of)):
                raise ValueError("debitAnyOf must contain unique account codes or GUIDs")
        credit = _scalar_text(row.get("credit", row.get("creditAccount", row.get("creditAccountValue"))))
        if amount is None or (debit is None and not has_alternatives) or credit is None:
            raise ValueError("each expected movement row must include amount, debit or debitAnyOf, and credit")
        normalized_rows.append({
            "amount": _decimal_text(amount),
            **({"debitAnyOf": debit_any_of} if has_alternatives else {"debit": debit}),
            "credit": credit,
        })
    tolerance = _decimal_value(value.get("tolerance", "0"))
    if tolerance is None or tolerance < 0:
        raise ValueError("expected_accounting_movements.tolerance must be a nonnegative number")
    active_value = value.get("activeValue", True)
    if not isinstance(active_value, (bool, str, int)):
        raise ValueError("expected_accounting_movements.activeValue must be a scalar")
    allow_extra_rows = value.get("allowExtraRows", False)
    if not isinstance(allow_extra_rows, bool):
        raise ValueError("expected_accounting_movements.allowExtraRows must be boolean")
    if any("debitAnyOf" in row for row in normalized_rows):
        if len(normalized_rows) != 1:
            raise ValueError("debitAnyOf requires exactly one expected movement row")
        if tolerance != 0:
            raise ValueError("debitAnyOf requires exact amount (tolerance=0)")
        if allow_extra_rows:
            raise ValueError("debitAnyOf does not allow extra active rows")
    result = {
        "register": _safe_text(register, "accounting register", max_len=128),
        "rows": normalized_rows,
        "recorderType": _safe_text(
            value.get("recorderType", value.get("recorder_type")),
            "recorderType",
            max_len=256,
        ) if value.get("recorderType", value.get("recorder_type")) is not None else None,
        "amountFields": _field_list(value.get("amountField", value.get("amountFields")), _DEFAULT_AMOUNT_FIELDS),
        "debitAccountFields": _field_list(
            value.get("debitAccountField", value.get("debitAccountFields")),
            _DEFAULT_DEBIT_ACCOUNT_FIELDS,
        ),
        "creditAccountFields": _field_list(
            value.get("creditAccountField", value.get("creditAccountFields")),
            _DEFAULT_CREDIT_ACCOUNT_FIELDS,
        ),
        "activeFields": _field_list(value.get("activeField", value.get("activeFields")), _DEFAULT_ACTIVE_FIELDS),
        "activeValue": active_value,
        "allowExtraRows": allow_extra_rows,
        "tolerance": _decimal_text(tolerance),
    }
    return result


def _payload_field_names(description: dict) -> list[str]:
    fields: list[str] = []
    for collection_name in ("properties", "navigation_properties"):
        for item in description.get(collection_name, []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                fields.append(item["name"])
    for section in description.get("tabularSections", []):
        if isinstance(section, str):
            fields.append(section)
    return sorted(set(fields))


def _preview_field_list(fields: list[str], *, limit: int = 40) -> str:
    if len(fields) <= limit:
        return ", ".join(fields)
    shown = ", ".join(fields[:limit])
    return f"{shown}, ... (+{len(fields) - limit} more)"


def _validate_payload_field_names(entity_set: str, description: dict,
                                  payload: dict[str, Any]) -> None:
    published_fields = _payload_field_names(description)
    if not published_fields:
        return
    unknown_fields = sorted(set(payload) - set(published_fields))
    if not unknown_fields:
        return
    unknown = ", ".join(unknown_fields)
    published = _preview_field_list(published_fields)
    raise ValueError(
        f"payload field(s) are not published in OData metadata for {entity_set}: {unknown}. "
        f"Remove them from payload or publish them in 1C before writing. "
        f"Published fields include: {published}"
    )


def _has_property(description: dict, field_name: str) -> bool:
    return any(
        item.get("name") == field_name
        for item in description.get("properties", [])
        if isinstance(item, dict)
    )


def _payload_reference_targets(metadata: MetadataModel, entity_set: str,
                               payload: dict[str, Any]) -> tuple[
                                   dict[str, dict[str, list[str]]],
                                   list[dict[str, Any]],
                               ]:
    """Collect only scalar GUIDs paired with a CSDL-declared navigation field.

    1C commonly exposes the scalar link as ``<NavigationName>_Key``. Requiring
    both that GUID property and the matching navigation property avoids
    inferring target entity sets from arbitrary GUID-shaped payload values.
    Published tabular-row sets are traversed only when their parent/section
    mapping is also present in CSDL.
    """
    grouped: dict[str, dict[str, list[str]]] = {}
    metadata_issues: list[dict[str, Any]] = []

    def visit(current_set: str, value: dict[str, Any], path: str,
              ancestors: tuple[str, ...], depth: int) -> None:
        if depth > 8 or current_set in ancestors:
            metadata_issues.append({
                "fieldPath": path,
                "reasonCode": "reference_metadata_depth_limit",
            })
            return
        description = metadata.describe_entity_set(current_set)
        properties = {
            item.get("name"): item
            for item in description.get("properties", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        for navigation in description.get("navigation_properties", []):
            if not isinstance(navigation, dict):
                continue
            navigation_name = navigation.get("name")
            if not isinstance(navigation_name, str):
                continue
            field_name = f"{navigation_name}_Key"
            property_description = properties.get(field_name)
            if not isinstance(property_description, dict):
                continue
            if property_description.get("type") != "Edm.Guid" or field_name not in value:
                continue
            raw_key = value.get(field_name)
            if raw_key is None:
                continue
            field_path = f"{path}.{field_name}"
            target_set = navigation.get("target_entity_set")
            if not isinstance(target_set, str) or not target_set:
                metadata_issues.append({
                    "fieldPath": field_path,
                    "reasonCode": "reference_target_entity_set_unresolved",
                })
                continue
            target_type = navigation.get("target_type") or navigation.get("type")
            try:
                target_description = metadata.describe_entity_set(target_set)
            except ValueError:
                metadata_issues.append({
                    "fieldPath": field_path,
                    "targetEntitySet": target_set,
                    "reasonCode": "reference_target_entity_set_unpublished",
                })
                continue
            resolved_target_type = target_description.get("entity_type")
            if (isinstance(target_type, str) and target_type
                    and isinstance(resolved_target_type, str) and resolved_target_type
                    and target_type != resolved_target_type):
                metadata_issues.append({
                    "fieldPath": field_path,
                    "targetEntitySet": target_set,
                    "reasonCode": "reference_target_type_mismatch",
                })
                continue
            try:
                key = _normalized_key(str(raw_key))
            except ValueError:
                metadata_issues.append({
                    "fieldPath": field_path,
                    "targetEntitySet": target_set,
                    "reasonCode": "reference_key_invalid",
                })
                continue
            if key == _NULL_GUID:
                continue
            grouped.setdefault(target_set, {}).setdefault(key, []).append(field_path)

        for section in description.get("tabularSections", []):
            if not isinstance(section, str) or section not in value:
                continue
            rows = value.get(section)
            if not isinstance(rows, list):
                continue
            row_set = f"{current_set}_{section}"
            try:
                row_description = metadata.describe_entity_set(row_set)
            except ValueError:
                continue
            if (row_description.get("isTabularRowEntity") is not True
                    or row_description.get("parentDocument") != current_set
                    or row_description.get("tabularSection") != section):
                continue
            for index, row in enumerate(rows):
                if isinstance(row, dict):
                    visit(row_set, row, f"{path}.{section}[{index}]",
                          (*ancestors, current_set), depth + 1)

    visit(entity_set, payload, entity_set, (), 0)
    return grouped, metadata_issues


def _property(description: dict, field_name: str) -> dict:
    for item in description.get("properties", []):
        if isinstance(item, dict) and item.get("name") == field_name:
            return item
    raise ValueError(f"field {field_name!r} is not a scalar property of {description.get('name')!r}")


def _select_with_field(select: str | None, field_name: str) -> str:
    if select is None:
        return f"Ref_Key,{field_name}" if field_name != "Ref_Key" else "Ref_Key"
    parts = [part.strip() for part in select.split(",") if part.strip()]
    if "*" in parts or field_name in parts:
        return select
    parts.append(field_name)
    return ",".join(parts)


def _parse_datetime_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an ISO datetime string")
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", normalized):
        raise ValueError(f"{label} must not include a timezone")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO datetime string") from exc
    return parsed.isoformat(timespec="seconds")


def _string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _literal_for_filter(value: str, type_name: str, label: str) -> str:
    if type_name == "Edm.DateTime":
        return f"datetime'{_parse_datetime_text(value, label)}'"
    if type_name == "Edm.String":
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        return _string_literal(value)
    raise ValueError(f"search field type {type_name!r} is not supported")


def _comparable_value(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    if type_name == "Edm.DateTime":
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    if type_name == "Edm.String":
        return str(value)
    return value


def _readback_verification(description: dict[str, Any], key: str,
                           payload: dict[str, Any], readback: Any) -> dict[str, Any]:
    """Strictly compare only submitted metadata-declared scalar values; never expose values."""
    supported_numeric = {
        "Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64",
        "Edm.Single", "Edm.Double", "Edm.Decimal",
    }
    types = {p.get("name"): p.get("type") for p in description.get("properties", [])}
    compared: list[str] = []
    mismatched: list[str] = []
    missing: list[str] = []
    unsupported: list[str] = []
    reasons: set[str] = set()
    normalized_key = _normalized_key(key)
    if not isinstance(readback, dict):
        return {"scope": "submitted_scalar_fields", "status": "unverified",
                "comparedFields": [], "mismatchedFields": [], "missingFields": [],
                "unsupportedFields": list(payload), "reasonCodes": ["readback_not_object"]}
    try:
        if not isinstance(readback.get("Ref_Key"), str):
            raise ValueError("readback key must be a string")
        readback_key = str(UUID(readback["Ref_Key"]))
        expected_key = str(UUID(normalized_key))
        if readback_key != expected_key:
            reasons.add("key_mismatch")
    except (ValueError, TypeError, AttributeError):
        reasons.add("key_missing_or_invalid")
    if not payload:
        reasons.add("empty_payload")
    for field, submitted in payload.items():
        type_name = types.get(field)
        if (field in description.get("tabularSections", []) or
                isinstance(submitted, (dict, list)) or type_name not in {
                    "Edm.String", "Edm.Boolean", "Edm.Guid", "Edm.DateTime", *supported_numeric
                }):
            unsupported.append(field)
            reasons.add("unsupported_field_type")
            continue
        if field not in readback:
            missing.append(field)
            reasons.add("submitted_field_missing")
            continue
        actual = readback[field]
        if submitted is None or actual is None:
            equal = submitted is None and actual is None
        elif type_name == "Edm.String":
            equal = isinstance(submitted, str) and isinstance(actual, str) and submitted == actual
        elif type_name == "Edm.Boolean":
            equal = type(submitted) is bool and type(actual) is bool and submitted is actual
        elif type_name == "Edm.Guid":
            if not isinstance(submitted, str) or not isinstance(actual, str):
                equal = False
            else:
                try:
                    equal = str(UUID(submitted)) == str(UUID(actual))
                except (ValueError, TypeError, AttributeError):
                    equal = False
        elif type_name == "Edm.DateTime":
            def strict_datetime(value: Any) -> tuple[datetime, str] | None:
                if not isinstance(value, str):
                    return None
                match = re.fullmatch(
                    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", value,
                )
                if not match:
                    return None
                try:
                    parsed = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    return None
                return parsed, (match.group(2) or "").rstrip("0")
            left, right = strict_datetime(submitted), strict_datetime(actual)
            equal = left is not None and right is not None and left == right
        else:
            def strict_decimal(value: Any) -> Decimal | None:
                if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
                    return None
                text = str(value)
                if isinstance(value, str) and not re.fullmatch(
                    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text,
                ):
                    return None
                try:
                    number = Decimal(text)
                except (InvalidOperation, ValueError):
                    return None
                return number if number.is_finite() else None
            left, right = strict_decimal(submitted), strict_decimal(actual)
            equal = left is not None and right is not None and left == right
        if equal:
            compared.append(field)
        else:
            mismatched.append(field)
            reasons.add("submitted_field_mismatch")
    verified = (bool(payload) and not mismatched and not missing and not unsupported
                and not reasons)
    return {"scope": "submitted_scalar_fields",
            "status": "verified" if verified else "unverified",
            "comparedFields": compared, "mismatchedFields": mismatched,
            "missingFields": missing, "unsupportedFields": unsupported,
            "reasonCodes": sorted(reasons)}


def _search_target(value: str, type_name: str, label: str) -> Any:
    if type_name == "Edm.DateTime":
        return datetime.fromisoformat(_parse_datetime_text(value, label))
    if type_name == "Edm.String":
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        return value
    raise ValueError(f"search field type {type_name!r} is not supported")


def _value_before(value: Any, target: Any) -> bool:
    if value is None:
        return True
    return value < target


def _where_filter_rejected(error: ODataHTTPError) -> bool:
    text = str(error)
    return (
        error.status_code == 500
        and "Операция не разрешена" in text
        and "ГДЕ" in text
    )


def _known_where_filter_rejection(error: ODataHTTPError) -> bool:
    code = (error.code or "").split(",", 1)[0].strip()
    message = error.message or ""
    return (
        error.status_code == 500
        and code == "-1"
        and 'Операция не разрешена в предложении "ГДЕ"' in message
    )


def _client_error_detail(error: ODataClientError) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "kind": "odataHttp" if isinstance(error, ODataHTTPError) else "odataTransport",
        "message": str(error),
    }
    if isinstance(error, ODataHTTPError):
        detail["httpStatus"] = error.status_code
        detail["definitive"] = error.definitive
        if error.code:
            detail["code"] = error.code
    if isinstance(error, ODataTransportError):
        detail["category"] = error.category
        detail["attempts"] = error.attempts
        if error.subreason:
            detail["subreason"] = error.subreason
    return detail


def _interrupted_scan_result(*, entity_set: str, predicate: dict[str, Any],
                             strategy: str, orderby: str, rows: list[dict[str, Any]],
                             scanned: int, start_skip: int | None,
                             total_count: int | None, resume_skip: int,
                             phase: str, error: ODataTransportError) -> dict[str, Any]:
    detail = _client_error_detail(error)
    return {
        "ok": False,
        "error": detail,
        "scanError": detail,
        "entitySet": entity_set,
        "predicate": predicate,
        "strategy": strategy,
        "orderby": orderby,
        "result": {"value": rows},
        "partialResult": bool(rows),
        "complete": False,
        "resultLimited": False,
        "maxScanReached": False,
        "scanInterrupted": True,
        "scannedRows": scanned,
        "startSkip": start_skip,
        "totalCount": total_count,
        "resumeSkip": resume_skip,
        "scanProgress": {
            "phase": phase,
            "scannedRows": scanned,
            "totalCount": total_count,
            "nextSkip": resume_skip,
        },
        "snapshotConsistent": False,
        "warning": (
            "The fallback scan stopped after a transport failure. Returned rows are partial; "
            "do not use an incomplete result to conclude that no match exists. Repeat the same "
            "search with resume_skip equal to resumeSkip and merge the returned rows. The 1C "
            "collection is not a consistent snapshot, so verify the final complete result."
        ),
    }


def _is_session_block_error(error: ODataClientError) -> bool:
    if not isinstance(error, ODataHTTPError):
        return False
    text = str(error)
    return any(marker in text for marker in _SESSION_BLOCK_MARKERS)


def _normalize_create_payload(description: dict, payload: dict) -> tuple[dict, bool]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a nonempty JSON object")
    data = dict(payload)
    null_ref_key = _has_property(description, "Ref_Key")
    if not null_ref_key:
        return data, False
    ref_key = data.get("Ref_Key")
    if ref_key is None or ref_key == "":
        data["Ref_Key"] = _NULL_GUID
        return data, True
    try:
        normalized = str(UUID(str(ref_key)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("create payload Ref_Key must be a UUID") from exc
    if normalized != _NULL_GUID:
        raise ValueError("create payload Ref_Key must be the null GUID")
    data["Ref_Key"] = _NULL_GUID
    return data, True


def _line_number_identity(value: Any, source: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{source} LineNumber values must be integers or integer strings")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        return int(value)
    raise ValueError(f"{source} LineNumber values must be integers or integer strings")


_TOOL_COVERAGE = {
    "read": [
        "onec83_connection_status",
        "onec83_get_write_receipt",
        "onec83_list_entity_sets",
        "onec83_list_document_types",
        "onec83_describe_entity_set",
        "onec83_resolve_enum_value",
        "onec83_query_entities",
        "onec83_list_entities",
        "onec83_search_entities",
        "onec83_search_entities_batch",
        "onec83_get_entity",
        "onec83_count_entities",
    ],
    "write": [
        "onec83_apply_document_queue",
        "onec83_reconcile_pending_create",
        "onec83_reconcile_pending_post",
        "onec83_create_entity",
        "onec83_update_entity",
        "onec83_patch_entity",
        "onec83_replace_tabular_section",
        "onec83_set_deletion_mark",
        "onec83_post_document",
        "onec83_unpost_document",
        "onec83_document_action",
    ],
    "prodWriteProbe": ["onec83_run_prod_write_probe"],
}


class Bridge:
    def __init__(self, client: ODataClient, ledger: WriteLedger | None = None,
                 metadata: MetadataModel | None = None,
                 target_name: str | None = None) -> None:
        self.client = client
        self.ledger = ledger or WriteLedger()
        self._counterparty_coordination_ledger = self.ledger
        self._counterparty_ledgers = lambda: (self.ledger,)
        canonical_target = counterparty_target_fingerprint(self.client.config.odata_base_url)
        self._counterparty_target_bindings = {
            self.target_fingerprint: canonical_target, canonical_target: canonical_target,
        }
        self._metadata = metadata
        self.target_name = target_name
        self._pending_prod_probe_receipt: tuple[str, str, str, float] | None = None
        self._prod_probe_receipt_lock = threading.Lock()
        self._queue_worker_context = threading.local()
        self._where_filter_rejections: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._where_filter_cache_target = self.target_fingerprint

    @property
    def target_fingerprint(self) -> str:
        normalized = self.client.config.odata_base_url.rstrip("/") + "/"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _prod_write_probe_enabled(self) -> bool:
        config = self.client.config
        return bool(
            self.target_name == "prod"
            and getattr(config, "multi_target", False)
            and getattr(config, "target_kind", None) == "prod"
            and not self.client.write_enabled
            and getattr(config, "prod_write_probe_enabled", False)
            and getattr(config, "prod_write_probe_fingerprint", None)
            == self.target_fingerprint
            and isinstance(getattr(config, "prod_write_probe_id", None), str)
        )

    def _prod_probe_stage_allowed(
        self,
        *,
        entity_set: str,
        operation: str,
        payload: dict | None,
        stage: str | None,
    ) -> bool:
        if not self._prod_write_probe_enabled() or entity_set != "Catalog_Организации":
            return False
        probe_id = self.client.config.prod_write_probe_id
        marker = f"MCP PROD WRITE PROBE {probe_id}"
        if stage == "create" and operation == "create" and isinstance(payload, dict):
            request_payload = dict(payload)
            ref_key = request_payload.pop("Ref_Key", None)
            return (
                ref_key in (None, _NULL_GUID)
                and request_payload == {"Description": marker}
            )
        if stage == "update" and operation == "patch" and payload == {
            "Description": marker + " UPDATED"
        }:
            return True
        if stage == "soft_delete" and operation == "patch" and payload == {
            "DeletionMark": True
        }:
            return True
        return False

    def _prod_probe_marker_rows(self, entity_set: str, marker: str) -> list[dict[str, Any]]:
        marker_search = self.search_entities(
            entity_set,
            field="Description",
            equals=marker,
            top=2,
            select="Ref_Key,Description,DeletionMark",
            max_scan=1000,
            page_size=100,
        )
        page = marker_search.get("result")
        rows = page.get("value") if isinstance(page, dict) else None
        if (
            marker_search.get("complete") is not True
            or marker_search.get("resultLimited") is True
            or marker_search.get("maxScanReached") is True
            or marker_search.get("scanInterrupted") is True
            or not isinstance(rows, list)
            or len(rows) > 1
            or (isinstance(page, dict) and page.get("next_link") is not None)
        ):
            raise ValueError(
                "the generated probe marker search was incomplete or ambiguous"
            )
        return rows

    def run_prod_write_probe(
        self,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        expected_target: str | None = None,
        dry_run_receipt: str | None = None,
    ) -> dict[str, Any]:
        """Run one scoped catalog create/update/readback/soft-delete probe on prod."""
        if type(dry_run) is not bool or type(confirm) is not bool:
            raise ValueError("dry_run and confirm must be booleans")
        if not self._prod_write_probe_enabled():
            raise PermissionError(
                "the one-time PROD write probe is disabled or its configured target fingerprint does not match"
            )
        if expected_target != self.target_fingerprint:
            raise PermissionError("the exact PROD targetFingerprint is required")
        if not dry_run and not confirm:
            raise PermissionError("confirm=true is required to apply the one-time PROD write probe")
        if not dry_run:
            with self._prod_probe_receipt_lock:
                pending = self._pending_prod_probe_receipt
                if (
                    not isinstance(dry_run_receipt, str)
                    or pending is None
                    or pending[1] != self.target_fingerprint
                    or pending[2] != self.client.config.prod_write_probe_id
                    or not secrets.compare_digest(pending[0], dry_run_receipt)
                    or not 0 <= perf_counter() - pending[3] <= _PROD_WRITE_PROBE_DRY_RUN_TTL_SECONDS
                ):
                    raise PermissionError(
                        "run dry_run=true on this MCP server and pass its one-time dry_run_receipt within five minutes before applying"
                    )
                self._pending_prod_probe_receipt = None

        # Check all local gates before metadata, duplicate-marker, or any other OData request.
        entity_set = "Catalog_Организации"
        description = self.metadata().describe_entity_set(entity_set)
        required_fields = ("Ref_Key", "Description", "DeletionMark", "DataVersion")
        missing_fields = [field for field in required_fields if not _has_property(description, field)]
        if missing_fields:
            raise ValueError(
                "Catalog_Организации is missing required probe fields: "
                + ", ".join(missing_fields)
            )
        if self._is_tabular_row_entity(entity_set, description):
            raise ValueError("the PROD probe entity set must be a catalog, not a tabular row")

        probe_id = self.client.config.prod_write_probe_id
        marker = f"MCP PROD WRITE PROBE {probe_id}"
        updated_marker = marker + " UPDATED"
        operation_prefix = f"prod-probe-{probe_id}"
        operation_ids = {
            "create": operation_prefix + "-create",
            "update": operation_prefix + "-update",
            "softDelete": operation_prefix + "-soft-delete",
        }
        plan = {
            "entitySet": entity_set,
            "targetFingerprint": self.target_fingerprint,
            "ordinaryWriteEnabled": self.client.write_enabled,
            "probeWriteEnabled": True,
            "operations": [
                {"operation": "create", "payload": {"Description": marker}},
                {"operation": "update", "payload": {"Description": updated_marker}},
                {"operation": "softDelete", "payload": {"DeletionMark": True}},
            ],
            "verification": "read each stage back; final DeletionMark must be true",
            "scope": (
                "one synthetic Catalog_Организации record only; no documents, posting, "
                "other entities, or ordinary PROD write tools"
            ),
            "operationIds": operation_ids,
        }
        if dry_run:
            dry_run_receipt = secrets.token_urlsafe(24)
            with self._prod_probe_receipt_lock:
                self._pending_prod_probe_receipt = (
                    dry_run_receipt,
                    self.target_fingerprint,
                    self.client.config.prod_write_probe_id,
                    perf_counter(),
                )
            return {
                "dryRun": True,
                "dryRunReceipt": dry_run_receipt,
                "plan": plan,
                "writeEnabled": False,
            }

        create_receipt = self.ledger.inspect(operation_ids["create"])
        if create_receipt is None:
            rows = self._prod_probe_marker_rows(entity_set, marker)
            if rows:
                raise ValueError(
                    "the generated probe marker already exists without a matching local receipt; "
                    "refusing to create or change another record"
                )

        try:
            created = self.create_entity(
                entity_set,
                {"Description": marker},
                dry_run=False,
                confirm=True,
                expected_target=self.target_fingerprint,
                operation_id=operation_ids["create"],
                _prod_write_probe_stage="create",
            )
        except (ODataClientError, UncertainWrite, ValueError, PermissionError) as exc:
            return {
                "ok": False,
                "phase": "create",
                "error": str(exc),
                "needsReconciliation": isinstance(exc, UncertainWrite),
                "writeEnabled": False,
            }

        key = created.get("key") if isinstance(created, dict) else None
        created_object = created.get("created") if isinstance(created, dict) else None
        created_readback = created.get("readback") if isinstance(created, dict) else None
        if not key and isinstance(created_object, dict):
            key = created_object.get("Ref_Key")
        if not key and isinstance(created_readback, dict):
            key = created_readback.get("Ref_Key")
        if not key:
            try:
                recovered_rows = self._prod_probe_marker_rows(entity_set, marker)
            except (ODataClientError, ValueError) as exc:
                return {
                    "ok": False,
                    "phase": "createReconciliation",
                    "create": created,
                    "error": str(exc),
                    "needsReconciliation": True,
                    "cleanupVerified": False,
                    "writeEnabled": False,
                }
            if len(recovered_rows) != 1:
                return {
                    "ok": False,
                    "phase": "createReconciliation",
                    "create": created,
                    "error": "the create response omitted a key and no unique probe record was found",
                    "needsReconciliation": True,
                    "cleanupVerified": False,
                    "writeEnabled": False,
                }
            key = recovered_rows[0].get("Ref_Key")
            if not isinstance(key, str) or not key:
                return {
                    "ok": False,
                    "phase": "createReconciliation",
                    "create": created,
                    "error": "the unique probe match did not contain Ref_Key",
                    "needsReconciliation": True,
                    "cleanupVerified": False,
                    "writeEnabled": False,
                }

        try:
            current = self.get_entity(entity_set, key)
        except ODataClientError as exc:
            current = created_readback if isinstance(created_readback, dict) else None
            if (
                not isinstance(current, dict)
                or current.get("Ref_Key") != key
                or current.get("Description") != marker
                or not isinstance(current.get("DataVersion"), str)
                or not current.get("DataVersion")
                or current.get("DeletionMark") is not False
            ):
                try:
                    recovery_rows = self._prod_probe_marker_rows(entity_set, marker)
                    if (
                        len(recovery_rows) != 1
                        or recovery_rows[0].get("Ref_Key") != key
                    ):
                        raise ValueError("the exact probe record could not be uniquely matched")
                    current = self.get_entity(entity_set, key)
                except (ODataClientError, ValueError) as recovery_error:
                    return {
                        "ok": False,
                        "phase": "createReadback",
                        "key": key,
                        "create": created,
                        "error": str(recovery_error),
                        "needsReconciliation": True,
                        "cleanupVerified": False,
                        "writeEnabled": False,
                    }
            if not isinstance(current, dict):
                return {
                    "ok": False,
                    "phase": "createReadback",
                    "key": key,
                    "create": created,
                    "error": str(exc),
                    "needsReconciliation": True,
                    "cleanupVerified": False,
                    "writeEnabled": False,
                }
        create_verified = (
            isinstance(current, dict)
            and current.get("Ref_Key") == key
            and current.get("Description") in {marker, updated_marker}
            and type(current.get("DeletionMark")) is bool
        )
        if not create_verified:
            return {
                "ok": False,
                "phase": "createReadback",
                "key": key,
                "create": created,
                "readback": current,
                "createVerified": False,
                "needsReconciliation": True,
                "cleanupVerified": False,
                "writeEnabled": False,
            }

        if current.get("DeletionMark") is True:
            update_verified = current.get("Description") == updated_marker
            return {
                "ok": update_verified,
                "key": key,
                "entitySet": entity_set,
                "marker": marker,
                "createVerified": create_verified,
                "updateVerified": update_verified,
                "deletionMarkVerified": True,
                "alreadySoftDeleted": True,
                "writeEnabled": False,
                "create": created,
            }

        update_result: dict[str, Any] | None = None
        update_error: str | None = None
        update_verified = current.get("Description") == updated_marker
        if current.get("Description") == marker:
            data_version = current.get("DataVersion")
            if not isinstance(data_version, str) or not data_version:
                update_error = "create readback omitted DataVersion; update was not sent"
            else:
                try:
                    update_result = self.patch_entity(
                        entity_set,
                        key,
                        {"Description": updated_marker},
                        dry_run=False,
                        confirm=True,
                        expected_target=self.target_fingerprint,
                        operation_id=operation_ids["update"],
                        expected_data_version=data_version,
                        _prod_write_probe_stage="update",
                    )
                except (ODataClientError, UncertainWrite, ValueError, PermissionError) as exc:
                    update_error = str(exc)
                try:
                    current = self.get_entity(entity_set, key)
                    update_verified = (
                        isinstance(current, dict)
                        and current.get("Description") == updated_marker
                    )
                except ODataClientError as exc:
                    update_error = update_error or str(exc)
        elif current.get("Description") != updated_marker:
            update_error = "the created record no longer has the exact probe marker; update refused"

        # Always attempt the explicitly authorized soft-delete after a verified probe record.
        cleanup_result: dict[str, Any] | None = None
        cleanup_error: str | None = None
        deletion_mark_verified = bool(
            isinstance(current, dict) and current.get("DeletionMark") is True
        )
        if not deletion_mark_verified:
            if (
                not isinstance(current, dict)
                or current.get("Ref_Key") != key
                or current.get("Description") not in {marker, updated_marker}
                or current.get("DeletionMark") is not False
            ):
                cleanup_error = "the probe record could not be verified before soft-delete"
            else:
                data_version = current.get("DataVersion")
                if not isinstance(data_version, str) or not data_version:
                    cleanup_error = "the pre-cleanup readback omitted DataVersion; soft-delete was not sent"
                else:
                    try:
                        cleanup_result = self.patch_entity(
                            entity_set,
                            key,
                            {"DeletionMark": True},
                            dry_run=False,
                            confirm=True,
                            expected_target=self.target_fingerprint,
                            operation_id=operation_ids["softDelete"],
                            expected_data_version=data_version,
                            _prod_write_probe_stage="soft_delete",
                        )
                    except (ODataClientError, UncertainWrite, ValueError, PermissionError) as exc:
                        cleanup_error = str(exc)
                    try:
                        current = self.get_entity(entity_set, key)
                        deletion_mark_verified = (
                            isinstance(current, dict)
                            and current.get("DeletionMark") is True
                        )
                    except ODataClientError as exc:
                        cleanup_error = cleanup_error or str(exc)

        result = {
            "ok": bool(create_verified and update_verified and deletion_mark_verified),
            "entitySet": entity_set,
            "key": key,
            "marker": marker,
            "createVerified": create_verified,
            "updateVerified": update_verified,
            "deletionMarkVerified": deletion_mark_verified,
            "writeEnabled": False,
            "create": created,
            "update": update_result,
            "softDelete": cleanup_result,
            "finalReadback": current,
        }
        if update_error:
            result["updateError"] = update_error
        if cleanup_error:
            result["cleanupError"] = cleanup_error
        return result

    def metadata(self) -> MetadataModel:
        if self._metadata is None:
            self._metadata = MetadataModel.from_xml(self.client.get_metadata())
        return self._metadata

    def status(self) -> dict:
        model = self.metadata()
        entity_sets = model.list_entity_sets()
        read_readiness = self._read_readiness(model, entity_sets)
        write_readiness = self._write_readiness(read_readiness)
        return {
            "connected": True,
            "metadataReady": True,
            "ready": bool(read_readiness.get("ready")) and (
                not self.client.write_enabled or bool(write_readiness.get("ready"))
            ),
            "serviceHost": urlsplit(self.client.config.odata_base_url).hostname,
            "targetFingerprint": self.target_fingerprint,
            "entitySetCount": len(entity_sets),
            "writeEnabled": self.client.write_enabled,
            "prodWriteProbeEnabled": self._prod_write_probe_enabled(),
            "readReadiness": read_readiness,
            "writeReadiness": write_readiness,
            "scope": "published OData objects permitted to the configured 1C user",
            "toolCoverage": _TOOL_COVERAGE,
        }

    def _read_probe_entity_set(self, model: MetadataModel, entity_sets: list[dict]) -> tuple[str | None, dict | None]:
        for item in entity_sets:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str):
                continue
            try:
                description = model.describe_entity_set(name)
            except ValueError:
                continue
            if not self._is_tabular_row_entity(name, description):
                return name, description
        return None, None

    def _read_readiness(self, model: MetadataModel, entity_sets: list[dict]) -> dict[str, Any]:
        probe_entity, description = self._read_probe_entity_set(model, entity_sets)
        if probe_entity is None or description is None:
            return {
                "ready": False,
                "reason": "no non-tabular published entity set is available for a read probe",
                "probe": None,
            }
        select = "Ref_Key" if _has_property(description, "Ref_Key") else None
        try:
            self.client.list_entities(probe_entity, top=1, skip=0, select=select)
        except ODataClientError as exc:
            detail = _client_error_detail(exc)
            session_blocked = _is_session_block_error(exc)
            blocker_type = "sessionBlocked" if session_blocked else detail["kind"]
            reason = (
                "1C session is blocked for the information base"
                if session_blocked
                else "OData object read probe failed"
            )
            return {
                "ready": False,
                "reason": reason,
                "blockerType": blocker_type,
                "probe": {"entitySet": probe_entity, "top": 1, "select": select},
                "error": detail,
            }
        return {
            "ready": True,
            "reason": "OData object reads are available",
            "probe": {"entitySet": probe_entity, "top": 1, "select": select},
        }

    def _write_readiness(self, read_readiness: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.client.config
        client_write_enabled = bool(self.client.write_enabled)
        read_base = _normalized_base_url(getattr(config, "odata_base_url", None))
        write_base = _normalized_base_url(
            getattr(
                config,
                "write_base_url",
                read_base if client_write_enabled else None,
            )
        )
        enable_write = bool(getattr(config, "enable_write", client_write_enabled))
        target_kind = getattr(
            config,
            "target_kind",
            "test" if client_write_enabled else None,
        )
        target_kind_is_test = target_kind == "test"
        write_base_configured = write_base is not None
        write_base_matches_read_base = bool(
            read_base and write_base and read_base == write_base
        )
        object_reads_ready = (
            read_readiness is None or read_readiness.get("ready") is True
        )
        config_ready = (
            enable_write
            and target_kind_is_test
            and write_base_matches_read_base
        )
        ready = client_write_enabled and config_ready and object_reads_ready
        blockers = []
        enable_write_label = "target.enable_write" if getattr(config, "multi_target", False) else "ONEC83_ENABLE_WRITE"
        target_kind_label = "target.target_kind" if getattr(config, "multi_target", False) else "ONEC83_TARGET_KIND"
        write_base_label = "target.write_base_url" if getattr(config, "multi_target", False) else "ONEC83_WRITE_BASE_URL"
        if not enable_write:
            blockers.append(f"{enable_write_label} is not true")
        if not target_kind_is_test:
            blockers.append(f"{target_kind_label} is not test")
        if not write_base_configured:
            blockers.append(f"{write_base_label} is not configured")
        elif not write_base_matches_read_base:
            blockers.append(f"{write_base_label} does not match the read target")
        if config_ready and not client_write_enabled:
            blockers.append("OData client write guard reports disabled")
        if not object_reads_ready:
            reason = read_readiness.get("reason") if isinstance(read_readiness, dict) else None
            blockers.append(
                "OData object reads are not ready" + (f": {reason}" if reason else "")
            )
        return {
            "ready": ready,
            "reason": (
                "writes enabled for the configured test OData target"
                if ready
                else "; ".join(blockers)
            ),
            "checks": {
                "enableWriteEnvTrue": enable_write,
                "targetKindIsTest": target_kind_is_test,
                "writeBaseUrlConfigured": write_base_configured,
                "writeBaseMatchesReadBase": write_base_matches_read_base,
                "clientWriteGuardEnabled": client_write_enabled,
                "objectReadsReady": object_reads_ready,
            },
            "requiredEnvNames": (
                ["ONEC83_TARGETS_JSON"]
                if getattr(config, "multi_target", False)
                else list(_WRITE_READINESS_ENV_NAMES)
            ),
        }

    def list_entity_sets(self, prefix: str | None = None) -> dict:
        if prefix == "":
            prefix = None
        if prefix is not None:
            prefix = _safe_text(prefix, "prefix", max_len=_MAX_PREFIX_CHARS)
        sets = self.metadata().list_entity_sets(prefix)
        return {"entitySets": sets, "count": len(sets),
                "targetFingerprint": self.target_fingerprint}

    def list_document_types(self) -> dict:
        model = self.metadata()
        sets = [
            item
            for item in model.list_entity_sets("Document_")
            if not self._is_tabular_row_entity(
                item["name"],
                model.describe_entity_set(item["name"]),
            )
        ]
        return {"entitySets": sets, "count": len(sets),
                "targetFingerprint": self.target_fingerprint}

    def describe_entity_set(self, entity_set: str) -> dict:
        description = self.metadata().describe_entity_set(entity_set)
        description["mcpCapabilities"] = self._capabilities_for(entity_set, description)
        return description

    def refresh_metadata(self) -> dict:
        self._metadata = None
        self._where_filter_rejections.clear()
        self._where_filter_cache_target = self.target_fingerprint
        return self.status()

    def resolve_enum_value(self, enum_name: str, member_name: str,
                           field: str | None = None,
                           account_context: str | None = None) -> dict:
        if account_context is not None:
            self._require_test_enum_context(account_context)
        return self.metadata().resolve_enum_value(
            enum_name, member_name, field=field, account_context=account_context,
        )

    def query_entities(self, entity_set: str, top: int = 20, skip: int = 0,
                       filter: str | None = None, select: str | None = None,
                       orderby: str | None = None, expand: str | None = None) -> dict:
        self.metadata().describe_entity_set(entity_set)
        max_top = getattr(self.client.config, "max_top", 200)
        top = _bounded_int(top, "top", 1, max_top)
        skip = _bounded_int(skip, "skip", 0, 10_000_000)
        filter = _safe_optional_query(filter, "filter")
        select = _safe_optional_query(select, "select")
        orderby = _safe_optional_query(orderby, "orderby")
        expand = _safe_optional_query(expand, "expand")
        rows = self.client.list_entities(entity_set, top=top, skip=skip,
                                         filter=filter, select=select,
                                         orderby=orderby, expand=expand)
        return {"entitySet": entity_set, "top": top, "skip": skip,
                "result": rows, "snapshotConsistent": False}

    def list_entities(self, entity_set: str, top: int = 20, skip: int = 0,
                      filter: str | None = None, select: str | None = None,
                      orderby: str | None = None, expand: str | None = None) -> dict:
        return self.query_entities(entity_set, top, skip, filter, select, orderby, expand)

    def search_entities(self, entity_set: str, field: str, top: int = 20,
                        equals: str | None = None,
                        from_value: str | None = None,
                        to_exclusive: str | None = None,
                        select: str | None = None,
                        max_scan: int = 1000,
                        page_size: int = 100,
                        resume_skip: int | None = None) -> dict:
        description = self.metadata().describe_entity_set(entity_set)
        field = _safe_text(field, "field", max_len=_MAX_PREFIX_CHARS)
        property_model = _property(description, field)
        if property_model.get("collection"):
            raise ValueError("search field must be a scalar property")
        type_name = property_model.get("type")
        if type_name not in {"Edm.DateTime", "Edm.String"}:
            raise ValueError("search supports Edm.DateTime ranges and Edm.String equality")
        max_top = getattr(self.client.config, "max_top", 200)
        top = _bounded_int(top, "top", 1, max_top)
        max_scan = _bounded_int(max_scan, "max_scan", 1, _MAX_SEARCH_SCAN)
        page_size = _bounded_int(page_size, "page_size", 1, max_top)
        if resume_skip is not None:
            resume_skip = _bounded_int(resume_skip, "resume_skip", 0, _MAX_SEARCH_SKIP)
        select = _safe_optional_query(select, "select")
        scan_select = _select_with_field(select, field)

        range_mode = from_value is not None or to_exclusive is not None
        exact_mode = equals is not None
        if exact_mode == range_mode:
            raise ValueError("provide either equals or both from_value and to_exclusive")
        if range_mode:
            if from_value is None or to_exclusive is None:
                raise ValueError("range search requires from_value and to_exclusive")
            if type_name != "Edm.DateTime":
                raise ValueError("range search is supported only for Edm.DateTime")
            lower = _search_target(from_value, type_name, "from_value")
            upper = _search_target(to_exclusive, type_name, "to_exclusive")
            if not lower < upper:
                raise ValueError("from_value must be earlier than to_exclusive")
            server_filter = (
                f"{field} ge {_literal_for_filter(from_value, type_name, 'from_value')} "
                f"and {field} lt {_literal_for_filter(to_exclusive, type_name, 'to_exclusive')}"
            )
            predicate = {"field": field, "operator": "range", "from": from_value,
                         "toExclusive": to_exclusive, "type": type_name}
            search_class = "date_range"
        else:
            assert equals is not None
            lower = _search_target(equals, type_name, "equals")
            upper = lower
            server_filter = f"{field} eq {_literal_for_filter(equals, type_name, 'equals')}"
            predicate = {"field": field, "operator": "eq", "value": equals,
                         "type": type_name}
            search_class = "date_equality" if type_name == "Edm.DateTime" else "string_equality"

        set_search_profile(
            search_class,
            "fallback_resume" if resume_skip is not None else "server_filter",
        )

        orderby = f"{field} asc"
        filter_error = None
        filter_cache_hit = False
        if resume_skip is None:
            target_fingerprint = self.target_fingerprint
            if target_fingerprint != self._where_filter_cache_target:
                self._where_filter_rejections.clear()
                self._where_filter_cache_target = target_fingerprint
            capability_key = (
                target_fingerprint,
                entity_set,
                field,
                predicate["operator"],
                type_name,
            )
            filter_error = self._where_filter_rejections.get(capability_key)
            filter_cache_hit = filter_error is not None
            if filter_cache_hit:
                set_search_profile(search_class, "fallback_scan_cache_hit")
                with diagnostic_stage("server_filter_cache_hit"):
                    pass
            if not filter_cache_hit:
                try:
                    rows = self.client.list_entities(entity_set, top=top, skip=0,
                                                     filter=server_filter,
                                                     select=scan_select,
                                                     orderby=orderby)
                    values = rows.get("value") if isinstance(rows, dict) else None
                    result_limited = isinstance(values, list) and len(values) >= top
                    return {"entitySet": entity_set, "predicate": predicate,
                            "strategy": "server_filter", "filter": server_filter,
                            "orderby": orderby, "result": rows,
                            "complete": not result_limited,
                            "resultLimited": result_limited, "maxScanReached": False,
                            "serverFilterCacheHit": False,
                            "snapshotConsistent": False,
                            "warning": (
                                "Returned rows reached top; use a larger top before relying on uniqueness."
                                if result_limited else None
                            )}
                except ODataHTTPError as exc:
                    if not _where_filter_rejected(exc):
                        set_search_profile(search_class, "server_filter_error")
                        raise
                    set_search_profile(search_class, "fallback_scan")
                    filter_error = {"httpStatus": exc.status_code, "code": exc.code,
                                    "message": str(exc)}
                    if _known_where_filter_rejection(exc):
                        self._where_filter_rejections[capability_key] = dict(filter_error)
                except Exception:
                    set_search_profile(search_class, "server_filter_error")
                    raise

        if type_name == "Edm.String":
            if resume_skip is not None:
                set_search_profile(search_class, "fallback_resume")
            fallback = self._linear_scan_search(
                entity_set=entity_set,
                field=field,
                type_name=type_name,
                predicate=predicate,
                target=lower,
                top=top,
                select=scan_select,
                orderby=orderby,
                max_scan=max_scan,
                page_size=page_size,
                resume_skip=resume_skip,
            )
        else:
            if resume_skip is not None:
                set_search_profile(search_class, "fallback_resume")
            fallback = self._ordered_scan_search(
                entity_set=entity_set,
                field=field,
                type_name=type_name,
                predicate=predicate,
                lower=lower,
                upper=upper,
                exact_mode=exact_mode,
                top=top,
                select=scan_select,
                orderby=orderby,
                max_scan=max_scan,
                page_size=page_size,
                resume_skip=resume_skip,
            )
        fallback.update({
            "filter": server_filter,
            "serverFilterError": filter_error,
            "serverFilterCacheHit": filter_cache_hit,
            "filterSkippedForResume": resume_skip is not None,
            "snapshotConsistent": False,
        })
        return fallback

    def search_entities_batch(self, entity_set: str, field: str,
                              equals_values: list[str], top: int = 20,
                              select: str | None = None,
                              max_scan: int = 1000,
                              page_size: int = 100,
                              resume_skip: int | None = None) -> dict:
        """Resolve several exact values with one bounded fallback traversal."""
        description = self.metadata().describe_entity_set(entity_set)
        field = _safe_text(field, "field", max_len=_MAX_PREFIX_CHARS)
        property_model = _property(description, field)
        if property_model.get("collection"):
            raise ValueError("search field must be a scalar property")
        type_name = property_model.get("type")
        if type_name not in {"Edm.DateTime", "Edm.String"}:
            raise ValueError("batch search supports Edm.String and Edm.DateTime equality")

        max_top = getattr(self.client.config, "max_top", 200)
        top = _bounded_int(top, "top", 1, max_top)
        max_scan = _bounded_int(max_scan, "max_scan", 1, _MAX_SEARCH_SCAN)
        page_size = _bounded_int(page_size, "page_size", 1, max_top)
        if resume_skip is not None:
            resume_skip = _bounded_int(resume_skip, "resume_skip", 0, _MAX_SEARCH_SKIP)
        if (not isinstance(equals_values, list)
                or not 1 <= len(equals_values) <= _MAX_SEARCH_BATCH_VALUES):
            raise ValueError(
                f"equals_values must contain 1..{_MAX_SEARCH_BATCH_VALUES} values"
            )
        if len(equals_values) * top > _MAX_SEARCH_BATCH_RESULT_ROWS:
            raise ValueError("batch result limit exceeds the bounded row budget")

        predicates: list[dict[str, Any]] = []
        targets: list[Any] = []
        seen_targets: set[Any] = set()
        for index, value in enumerate(equals_values):
            value = _safe_text(
                value, f"equals_values[{index}]", max_len=512,
                allow_empty=type_name == "Edm.String",
            )
            target = _search_target(value, type_name, f"equals_values[{index}]")
            if target in seen_targets:
                raise ValueError("equals_values must be unique after normalization")
            seen_targets.add(target)
            targets.append(target)
            predicates.append({
                "field": field,
                "operator": "eq",
                "value": value,
                "type": type_name,
            })

        selected: list[str] = []
        if select is not None:
            select = _safe_optional_query(select, "select")
            selected = [part.strip() for part in (select or "").split(",") if part.strip()]
            if select is not None and (
                not selected or any(part in {"*", "**"} for part in selected)
            ):
                raise ValueError("batch search requires an explicit projected select")
            for selected_field in selected:
                selected_model = _property(description, selected_field)
                if selected_model.get("collection"):
                    raise ValueError("batch search select fields must be scalar properties")
        keys = description.get("keys")
        if isinstance(keys, list):
            for key in keys:
                if isinstance(key, str):
                    key_model = _property(description, key)
                    if key_model.get("collection"):
                        raise ValueError("batch search key fields must be scalar properties")
                    if key not in selected:
                        selected.append(key)
        if _has_property(description, "Ref_Key") and "Ref_Key" not in selected:
            selected.append("Ref_Key")
        for selected_field in (field,):
            if selected_field not in selected:
                selected.append(selected_field)
        scan_select = ",".join(dict.fromkeys(selected))

        search_class = "date_equality" if type_name == "Edm.DateTime" else "string_equality"
        orderby = f"{field} asc"
        filter_error = None
        target_fingerprint = self.target_fingerprint
        if target_fingerprint != self._where_filter_cache_target:
            self._where_filter_rejections.clear()
            self._where_filter_cache_target = target_fingerprint
        capability_key = (target_fingerprint, entity_set, field, "eq", type_name)
        filter_error = self._where_filter_rejections.get(capability_key)
        cache_hit = filter_error is not None
        if resume_skip is None and cache_hit:
            set_search_profile(search_class, "fallback_scan_cache_hit")
            with diagnostic_stage("server_filter_cache_hit"):
                pass
        elif resume_skip is not None:
            cache_hit = False
            set_search_profile(search_class, "fallback_resume")
        else:
            filtered: list[dict[str, Any]] = []
            try:
                for predicate in predicates:
                    server_filter = (
                        f"{field} eq "
                        f"{_literal_for_filter(predicate['value'], type_name, 'equals')}"
                    )
                    filtered.append(self.client.list_entities(
                        entity_set, top=top, skip=0, filter=server_filter,
                        select=scan_select, orderby=orderby,
                    ))
            except ODataHTTPError as exc:
                if not _where_filter_rejected(exc):
                    set_search_profile(search_class, "server_filter_error")
                    raise
                set_search_profile(search_class, "fallback_scan")
                filter_error = {
                    "httpStatus": exc.status_code,
                    "code": exc.code,
                    "message": str(exc),
                }
                if _known_where_filter_rejection(exc):
                    self._where_filter_rejections[capability_key] = dict(filter_error)
                    cache_hit = False
            except Exception:
                set_search_profile(search_class, "server_filter_error")
                raise
            else:
                set_search_profile(search_class, "server_filter")
                results = []
                for predicate, rows in zip(predicates, filtered, strict=True):
                    values = rows.get("value") if isinstance(rows, dict) else None
                    if not isinstance(values, list):
                        raise ValueError("batch search server filter returned an invalid page")
                    result_limited = len(values) >= top
                    results.append({
                        "predicate": predicate,
                        "strategy": "server_filter",
                        "result": rows,
                        "complete": not result_limited,
                        "resultLimited": result_limited,
                        "maxScanReached": False,
                        "warning": (
                            "Returned rows reached top; increase top before relying on uniqueness."
                            if result_limited else None
                        ),
                    })
                result_limited = any(item["resultLimited"] for item in results)
                return {
                    "entitySet": entity_set,
                    "field": field,
                    "strategy": "server_filter_batch",
                    "queryCount": len(predicates),
                    "results": results,
                    "complete": all(item["complete"] for item in results),
                    "resultLimited": result_limited,
                    "serverFilterCacheHit": False,
                    "snapshotConsistent": False,
                }

        initial_skip = resume_skip if resume_skip is not None else 0
        resume_completeness_unknown = resume_skip is not None and resume_skip > 0
        indexed_rows: dict[Any, list[dict[str, Any]]] = {
            target: [] for target in targets
        }
        result_limited: set[Any] = set()
        scanned = 0
        skip = initial_skip
        total_count: int | None = None
        scan_error: ODataTransportError | None = None
        scan_phase: str | None = None
        try:
            total_count = self.client.count_entities(entity_set)
        except ODataTransportError as exc:
            scan_error = exc
            scan_phase = "count"
        if total_count is not None and resume_skip is not None and resume_skip > total_count:
            raise ValueError("resume_skip exceeds the current collection count")

        while (scan_error is None and total_count is not None
               and skip < total_count and scanned < max_scan):
            request_top = min(page_size, max_scan - scanned, total_count - skip)
            try:
                increment_search_count("fallback_page_reads")
                page = self.client.list_entities(
                    entity_set, top=request_top, skip=skip,
                    select=scan_select, orderby=orderby,
                )
            except ODataTransportError as exc:
                scan_error = exc
                scan_phase = "page_read"
                break
            page_rows = page.get("value") if isinstance(page, dict) else None
            if not isinstance(page_rows, list):
                raise ValueError("batch search fallback returned an invalid page")
            if not page_rows:
                break
            for row in page_rows:
                if not isinstance(row, dict):
                    raise ValueError("batch search fallback returned an invalid row")
                scanned += 1
                comparable = _comparable_value(row.get(field), type_name)
                if comparable not in indexed_rows:
                    continue
                matches = indexed_rows[comparable]
                if len(matches) < top:
                    matches.append(row)
                else:
                    result_limited.add(comparable)
            skip += len(page_rows)
            if len(page_rows) < request_top:
                break

        scan_complete = (
            scan_error is None and total_count is not None and skip >= total_count
        )
        max_scan_reached = (
            scan_error is None and total_count is not None
            and scanned >= max_scan and not scan_complete
        )
        next_skip = skip if scan_error is not None or not scan_complete else None
        scan_error_detail = _client_error_detail(scan_error) if scan_error else None
        results = []
        for predicate, target in zip(predicates, targets, strict=True):
            limited = target in result_limited
            complete = scan_complete and not limited and not resume_completeness_unknown
            warning = None
            if limited:
                warning = "Results reached top; increase top before relying on uniqueness."
            elif scan_error is not None:
                warning = (
                    "The shared fallback scan stopped after a transport failure. "
                    "Returned rows are partial; repeat the same query set with resume_skip "
                    "equal to resumeSkip and merge rows."
                )
            elif max_scan_reached:
                warning = (
                    "The shared fallback scan reached max_scan. Increase max_scan or "
                    "narrow the search before using absence to deduplicate."
                )
            elif not scan_complete:
                warning = "The shared fallback scan did not reach the counted end of the collection."
            if resume_completeness_unknown:
                resume_warning = (
                    "Previous per-value matches are not retained for a resumed scan; "
                    "use these rows as candidates only."
                )
                warning = f"{warning} {resume_warning}" if warning else resume_warning
            results.append({
                "predicate": predicate,
                "strategy": "shared_fallback_scan",
                "result": {"value": indexed_rows[target]},
                "complete": complete,
                "resultLimited": limited,
                "maxScanReached": max_scan_reached,
                "scanInterrupted": scan_error is not None,
                "scanPhase": scan_phase,
                "scannedRows": scanned,
                "startSkip": initial_skip,
                "totalCount": total_count,
                "resumeSkip": next_skip,
                "resumedFromSkip": resume_skip,
                "filterSkippedForResume": resume_skip is not None,
                "resumeCompletenessUnknown": resume_completeness_unknown,
                "error": scan_error_detail,
                "warning": warning,
            })
        batch_result_limited = bool(result_limited)
        return {
            "ok": scan_error is None,
            "entitySet": entity_set,
            "field": field,
            "strategy": "shared_fallback_scan",
            "queryCount": len(predicates),
            "results": results,
            "complete": (
                scan_complete and not batch_result_limited
                and not resume_completeness_unknown
            ),
            "resultLimited": batch_result_limited,
            "maxScanReached": max_scan_reached,
            "scanInterrupted": scan_error is not None,
            "scanPhase": scan_phase,
            "scannedRows": scanned,
            "startSkip": initial_skip,
            "totalCount": total_count,
            "resumeSkip": next_skip,
            "resumedFromSkip": resume_skip,
            "serverFilterCacheHit": cache_hit,
            "filterSkippedForResume": resume_skip is not None,
            "resumeCompletenessUnknown": resume_completeness_unknown,
            "serverFilterError": filter_error,
            "warning": (
                "Previous per-value matches are not retained for a resumed scan; "
                "use these rows as candidates only."
                if resume_completeness_unknown else None
            ),
            "error": scan_error_detail,
            "snapshotConsistent": False,
        }

    def _linear_scan_search(self, *, entity_set: str, field: str, type_name: str,
                            predicate: dict[str, Any], target: Any,
                            top: int, select: str, orderby: str,
                            max_scan: int, page_size: int,
                            resume_skip: int | None) -> dict:
        try:
            total_count = self.client.count_entities(entity_set)
        except ODataTransportError as exc:
            initial_skip = resume_skip if resume_skip is not None else 0
            return _interrupted_scan_result(
                entity_set=entity_set, predicate=predicate,
                strategy="linear_scan_after_filter_error", orderby=orderby,
                rows=[], scanned=0, start_skip=initial_skip,
                total_count=None, resume_skip=initial_skip,
                phase="count", error=exc,
            )
        if resume_skip is not None and resume_skip > total_count:
            raise ValueError("resume_skip exceeds the current collection count")
        rows: list[dict[str, Any]] = []
        scanned = 0
        initial_skip = resume_skip if resume_skip is not None else 0
        skip = initial_skip
        result_limited = False

        while skip < total_count and scanned < max_scan:
            request_top = min(page_size, max_scan - scanned, total_count - skip)
            try:
                increment_search_count("fallback_page_reads")
                page = self.client.list_entities(entity_set, top=request_top, skip=skip,
                                                 select=select, orderby=orderby)
            except ODataTransportError as exc:
                return _interrupted_scan_result(
                    entity_set=entity_set, predicate=predicate,
                    strategy="linear_scan_after_filter_error", orderby=orderby,
                    rows=rows, scanned=scanned, start_skip=initial_skip,
                    total_count=total_count, resume_skip=skip,
                    phase="page_read", error=exc,
                )
            page_rows = page.get("value")
            if not isinstance(page_rows, list):
                raise ValueError("scan fallback returned an invalid page")
            if not page_rows:
                break
            for row in page_rows:
                if not isinstance(row, dict):
                    raise ValueError("scan fallback returned an invalid row")
                scanned += 1
                if _comparable_value(row.get(field), type_name) == target:
                    rows.append(row)
                    if len(rows) >= top:
                        result_limited = True
                        break
            skip += len(page_rows)
            if result_limited or len(page_rows) < request_top:
                break
        complete = skip >= total_count and not result_limited
        max_scan_reached = scanned >= max_scan and not complete and not result_limited
        return {
            "entitySet": entity_set,
            "predicate": predicate,
            "strategy": "linear_scan_after_filter_error",
            "orderby": orderby,
            "result": {"value": rows},
            "complete": complete,
            "resultLimited": result_limited,
            "maxScanReached": max_scan_reached,
            "scanInterrupted": False,
            "scannedRows": scanned,
            "startSkip": initial_skip,
            "totalCount": total_count,
            "resumeSkip": skip if max_scan_reached else None,
            "resumedFromSkip": resume_skip,
            "warning": (
                "No matches are definitive only when complete=true. "
                "String fallback scans from the beginning because 1C string collation "
                "may differ from Python ordering."
                if not complete else None
            ),
        }

    def _ordered_scan_search(self, *, entity_set: str, field: str, type_name: str,
                             predicate: dict[str, Any], lower: Any, upper: Any,
                             exact_mode: bool, top: int, select: str, orderby: str,
                             max_scan: int, page_size: int,
                             resume_skip: int | None) -> dict:
        try:
            total_count = self.client.count_entities(entity_set)
        except ODataTransportError as exc:
            initial_skip = resume_skip if resume_skip is not None else 0
            return _interrupted_scan_result(
                entity_set=entity_set, predicate=predicate,
                strategy="ordered_scan_after_filter_error", orderby=orderby,
                rows=[], scanned=0, start_skip=initial_skip,
                total_count=None, resume_skip=initial_skip,
                phase="count", error=exc,
            )
        if resume_skip is not None and resume_skip > total_count:
            raise ValueError("resume_skip exceeds the current collection count")
        if resume_skip is not None:
            start_skip = resume_skip
        else:
            try:
                start_skip = self._ordered_lower_bound(
                    entity_set, field, type_name, lower, total_count, orderby,
                )
            except ODataTransportError as exc:
                return _interrupted_scan_result(
                    entity_set=entity_set, predicate=predicate,
                    strategy="ordered_scan_after_filter_error", orderby=orderby,
                    rows=[], scanned=0, start_skip=None, total_count=total_count,
                    resume_skip=0, phase="start_position", error=exc,
                )
        rows: list[dict[str, Any]] = []
        scanned = 0
        skip = start_skip
        complete = False
        result_limited = False

        while skip < total_count and scanned < max_scan:
            request_top = min(page_size, max_scan - scanned, total_count - skip)
            try:
                increment_search_count("fallback_page_reads")
                page = self.client.list_entities(entity_set, top=request_top, skip=skip,
                                                 select=select, orderby=orderby)
            except ODataTransportError as exc:
                return _interrupted_scan_result(
                    entity_set=entity_set, predicate=predicate,
                    strategy="ordered_scan_after_filter_error", orderby=orderby,
                    rows=rows, scanned=scanned, start_skip=start_skip,
                    total_count=total_count, resume_skip=skip,
                    phase="page_read", error=exc,
                )
            page_rows = page.get("value")
            if not isinstance(page_rows, list):
                raise ValueError("ordered fallback returned an invalid page")
            if not page_rows:
                complete = True
                break
            stop = False
            for row in page_rows:
                if not isinstance(row, dict):
                    raise ValueError("ordered fallback returned an invalid row")
                scanned += 1
                comparable = _comparable_value(row.get(field), type_name)
                if exact_mode:
                    if comparable is not None and comparable > lower:
                        complete = True
                        stop = True
                        break
                    matched = comparable == lower
                else:
                    if comparable is not None and comparable >= upper:
                        complete = True
                        stop = True
                        break
                    matched = comparable is not None and lower <= comparable < upper
                if matched:
                    rows.append(row)
                    if len(rows) >= top:
                        result_limited = True
                        stop = True
                        break
            skip += len(page_rows)
            if stop:
                break
            if len(page_rows) < request_top:
                complete = True
                break
        if skip >= total_count and not result_limited:
            complete = True
        max_scan_reached = scanned >= max_scan and not complete and not result_limited
        return {
            "entitySet": entity_set,
            "predicate": predicate,
            "strategy": "ordered_scan_after_filter_error",
            "orderby": orderby,
            "result": {"value": rows},
            "complete": complete,
            "resultLimited": result_limited,
            "maxScanReached": max_scan_reached,
            "scanInterrupted": False,
            "scannedRows": scanned,
            "startSkip": start_skip,
            "totalCount": total_count,
            "resumeSkip": skip if max_scan_reached else None,
            "resumedFromSkip": resume_skip,
            "warning": (
                "No matches are definitive only when complete=true. "
                "Increase max_scan or narrow the search before using absence for deduplication."
                if not complete else None
            ),
        }

    def _ordered_lower_bound(self, entity_set: str, field: str, type_name: str,
                             target: Any, total_count: int, orderby: str) -> int:
        low = 0
        high = total_count
        select = field
        while low < high:
            midpoint = (low + high) // 2
            increment_search_count("date_lower_bound_probes")
            page = self.client.list_entities(entity_set, top=1, skip=midpoint,
                                             select=select, orderby=orderby)
            rows = page.get("value")
            if not isinstance(rows, list):
                raise ValueError("ordered fallback returned an invalid page")
            if not rows:
                high = midpoint
                continue
            row = rows[0]
            if not isinstance(row, dict):
                raise ValueError("ordered fallback returned an invalid row")
            comparable = _comparable_value(row.get(field), type_name)
            if _value_before(comparable, target):
                low = midpoint + 1
            else:
                high = midpoint
        return low

    def _get_entity_with_keyed_404_fallback(
        self, entity_set: str, key: str,
    ) -> tuple[dict[str, Any], str]:
        """Read one entity, using one exact collection lookup only after keyed 404."""
        key = _normalized_key(key)
        try:
            entity = self.client.get_entity(entity_set, key)
            if not isinstance(entity, dict):
                raise ODataClientError("OData item response is not an object")
            return entity, "keyed_get"
        except ODataHTTPError as exc:
            if exc.status_code != 404:
                raise
            keyed_error = exc

        def inconclusive(reason: str, http_status: int | None = None) -> ODataHTTPError:
            keyed_error.readback_fallback = {
                "status": "inconclusive", "reason": reason,
            }
            if http_status is not None:
                keyed_error.readback_fallback["httpStatus"] = http_status
            original_message = keyed_error.message or "entity instance not found"
            keyed_error.message = f"{original_message}; exact Ref_Key lookup inconclusive ({reason})"
            return keyed_error

        filter_text = f"Ref_Key eq guid'{key}'"
        try:
            with diagnostic_stage("readback_404_ref_key_lookup"):
                page = self.client.list_entities(
                    entity_set, top=2, skip=0, filter=filter_text,
                )
        except ODataClientError as fallback_error:
            http_status = (fallback_error.status_code
                           if isinstance(fallback_error, ODataHTTPError) else None)
            raise inconclusive("filter_request_failed", http_status) from None

        rows = page.get("value") if isinstance(page, dict) else None
        if not isinstance(rows, list) or len(rows) > 2:
            raise inconclusive("invalid_or_oversized_page")
        count = page.get("count")
        if count is not None and (type(count) is not int or count < 0):
            raise inconclusive("invalid_match_count")
        if (type(count) is int and count > 1) or page.get("next_link") is not None:
            raise inconclusive("multiple_or_truncated_matches")

        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_key = _normalized_key(row.get("Ref_Key"))
            except ValueError:
                continue
            if row_key == key:
                matches.append(row)
        if len(rows) == 1 and len(matches) == 1 and count in (None, 1):
            return matches[0], "ref_key_filter_after_404"
        reason = "no_match" if not rows else (
            "multiple_or_truncated_matches" if len(rows) > 1 or len(matches) > 1
            else "mismatched_key"
        )
        raise inconclusive(reason)

    def get_entity(self, entity_set: str, key: str) -> dict:
        self.metadata().describe_entity_set(entity_set)
        entity, _strategy = self._get_entity_with_keyed_404_fallback(entity_set, key)
        return entity

    def get_accounting_register_record_set(
        self,
        entity_set: str,
        recorder_key: str,
        recorder_entity_set: str | None = None,
        recorder_type: str | None = None,
    ) -> dict:
        if not entity_set.startswith("AccountingRegister_"):
            raise ValueError("entity_set must be an AccountingRegister_*")
        self.metadata().describe_entity_set(entity_set)
        if (recorder_entity_set is None) == (recorder_type is None):
            raise ValueError(
                "provide exactly one of recorder_entity_set or recorder_type"
            )
        if recorder_entity_set is not None:
            recorder_entity_set = _safe_text(
                recorder_entity_set, "recorder_entity_set", max_len=_MAX_PREFIX_CHARS,
            )
            recorder_description = self.metadata().describe_entity_set(
                recorder_entity_set
            )
            recorder_type = self._movement_recorder_type(
                recorder_description, {},
            )
        else:
            recorder_type = _safe_text(
                recorder_type, "recorder_type", max_len=_MAX_PREFIX_CHARS,
            )
        recorder_key = _normalized_key(recorder_key)
        try:
            payload = self.client.get_register_record_set(
                entity_set, recorder_key, recorder_type,
            )
        except ODataHTTPError as exc:
            if exc.status_code != 404:
                raise
            return {
                "ok": False,
                "entitySet": entity_set,
                "recorderKey": recorder_key,
                "recorderType": recorder_type,
                "strategy": "recorder_named_key",
                "complete": False,
                "absenceStatus": "unknown",
                "snapshotConsistent": False,
                "error": {"kind": "odataHttp", "httpStatus": 404,
                          "code": exc.code, "message": str(exc)},
                "message": "Named-key 404 does not prove that this recorder has no register rows; use a complete bounded collection scan",
            }
        rows = _recordset_rows(payload)
        return {
            "entitySet": entity_set,
            "recorderKey": recorder_key,
            "recorderType": recorder_type,
            "strategy": "recorder_named_key",
            "result": {"value": rows},
            "count": len(rows),
            "complete": True,
            "snapshotConsistent": False,
        }

    def count_entities(self, entity_set: str) -> dict:
        self.metadata().describe_entity_set(entity_set)
        return {"entitySet": entity_set, "count": self.client.count_entities(entity_set),
                "snapshotConsistent": False}

    def _validate_payload_references(self, entity_set: str,
                                     payload: dict[str, Any]) -> dict[str, Any]:
        grouped, metadata_issues = _payload_reference_targets(
            self.metadata(), entity_set, payload,
        )
        total_keys = sum(len(keys) for keys in grouped.values())
        unverified = list(metadata_issues)
        missing: list[dict[str, str]] = []
        checked_count = 0
        lookup_count = 0
        keyed_lookup_count = 0
        reason_codes = {item["reasonCode"] for item in metadata_issues}
        query_count = sum(
            (len(keys) + _MAX_REFERENCE_LOOKUP_KEYS_PER_QUERY - 1)
            // _MAX_REFERENCE_LOOKUP_KEYS_PER_QUERY
            for keys in grouped.values()
        )

        limit_reason = None
        if total_keys > _MAX_REFERENCE_LOOKUP_KEYS_PER_WRITE:
            limit_reason = "reference_lookup_limit_exceeded"
        elif query_count > _MAX_REFERENCE_LOOKUP_QUERIES_PER_WRITE:
            limit_reason = "reference_lookup_request_limit_exceeded"
        if limit_reason:
            for target_set, keys in grouped.items():
                paths = sorted({
                    path for key_paths in keys.values() for path in key_paths
                })
                unverified.append({
                    "targetEntitySet": target_set,
                    "fieldPaths": paths[:20],
                    "omittedFieldPathCount": max(0, len(paths) - 20),
                    "reasonCode": limit_reason,
                })
            reason_codes.add(limit_reason)
            return {
                "status": "unknown",
                "checkedReferenceCount": 0,
                "lookupCount": 0,
                "targetEntitySets": sorted(grouped),
                "missingReferences": [],
                "unverifiedReferences": unverified,
                "reasonCodes": sorted(reason_codes),
            }

        for target_set, key_paths in grouped.items():
            all_keys = sorted(key_paths)
            target_fingerprint = self.target_fingerprint
            if target_fingerprint != self._where_filter_cache_target:
                self._where_filter_rejections.clear()
                self._where_filter_cache_target = target_fingerprint
            capability_key = (
                target_fingerprint, target_set, "Ref_Key", "eq_or", "Edm.Guid",
            )
            filter_is_known_unsupported = capability_key in self._where_filter_rejections

            def keyed_get_batch(keys: list[str]) -> None:
                nonlocal checked_count, keyed_lookup_count, lookup_count
                reason_codes.add("reference_filter_unsupported_keyed_get_fallback")
                for key in keys:
                    if keyed_lookup_count >= _MAX_REFERENCE_KEYED_GETS_PER_WRITE:
                        reason = "reference_keyed_get_limit_exceeded"
                        reason_codes.add(reason)
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                            })
                        continue
                    keyed_lookup_count += 1
                    lookup_count += 1
                    try:
                        row = self.client.get_entity(target_set, key)
                    except ODataHTTPError as exc:
                        if exc.status_code == 404:
                            reason = "reference_keyed_get_404_inconclusive"
                            reason_codes.add(reason)
                            for path in key_paths[key]:
                                unverified.append({
                                    "fieldPath": path,
                                    "targetEntitySet": target_set,
                                    "reasonCode": reason,
                                    "httpStatus": 404,
                                })
                            continue
                        reason = "reference_keyed_get_http_error"
                        reason_codes.add(reason)
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                                "httpStatus": exc.status_code,
                            })
                        continue
                    except ODataClientError:
                        reason = "reference_keyed_get_transport_error"
                        reason_codes.add(reason)
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                            })
                        continue

                    raw_key = row.get("Ref_Key") if isinstance(row, dict) else None
                    try:
                        returned_key = _normalized_key(str(raw_key))
                    except ValueError:
                        returned_key = None
                    if returned_key != key:
                        reason = "reference_keyed_get_response_incomplete"
                        reason_codes.add(reason)
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                            })
                        continue
                    checked_count += 1

            if filter_is_known_unsupported:
                reason_codes.add("reference_filter_known_unsupported")
                keyed_get_batch(all_keys)
                continue
            for offset in range(0, len(all_keys), _MAX_REFERENCE_LOOKUP_KEYS_PER_QUERY):
                batch = all_keys[offset:offset + _MAX_REFERENCE_LOOKUP_KEYS_PER_QUERY]
                lookup_count += 1
                filter_text = " or ".join(
                    f"Ref_Key eq guid'{key}'" for key in batch
                )
                try:
                    result = self.client.list_entities(
                        target_set,
                        top=len(batch) + 1,
                        skip=0,
                        filter=filter_text,
                        select="Ref_Key",
                    )
                except ODataHTTPError as exc:
                    if _where_filter_rejected(exc):
                        reason = "reference_filter_unsupported"
                        if _known_where_filter_rejection(exc):
                            self._where_filter_rejections[capability_key] = {
                                "httpStatus": exc.status_code,
                                "code": exc.code,
                                "message": str(exc),
                            }
                            reason_codes.add(reason)
                            keyed_get_batch(all_keys[offset:])
                            break
                    else:
                        reason = "reference_lookup_http_error"
                    reason_codes.add(reason)
                    for key in batch:
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                                "httpStatus": exc.status_code,
                            })
                    continue
                except ODataClientError as exc:
                    reason = "reference_lookup_transport_error"
                    reason_codes.add(reason)
                    for key in batch:
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                            })
                    continue

                rows = result.get("value") if isinstance(result, dict) else None
                expected = set(batch)
                returned: set[str] = set()
                response_is_complete = (
                    isinstance(rows, list)
                    and "next_link" not in result
                    and len(rows) <= len(batch)
                )
                if response_is_complete:
                    for row in rows:
                        raw_key = row.get("Ref_Key") if isinstance(row, dict) else None
                        try:
                            returned_key = _normalized_key(str(raw_key))
                        except ValueError:
                            response_is_complete = False
                            break
                        if returned_key not in expected or returned_key in returned:
                            response_is_complete = False
                            break
                        returned.add(returned_key)

                if not response_is_complete:
                    reason = "reference_lookup_response_incomplete"
                    reason_codes.add(reason)
                    for key in batch:
                        for path in key_paths[key]:
                            unverified.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                                "reasonCode": reason,
                            })
                    continue

                checked_count += len(batch)
                for key in batch:
                    if key not in returned:
                        for path in key_paths[key]:
                            missing.append({
                                "fieldPath": path,
                                "targetEntitySet": target_set,
                            })

        status = "verified"
        if unverified:
            status = "unknown"
        elif missing:
            status = "missing"
        elif checked_count == 0:
            status = "not_applicable"
        if missing:
            reason_codes.add("reference_target_missing")
        return {
            "status": status,
            "checkedReferenceCount": checked_count,
            "lookupCount": lookup_count,
            "targetEntitySets": sorted({
                *grouped,
                *[
                    item["targetEntitySet"]
                    for item in metadata_issues
                    if isinstance(item.get("targetEntitySet"), str)
                ],
            }),
            "missingReferences": sorted(
                missing, key=lambda item: (item["targetEntitySet"], item["fieldPath"])
            ),
            "unverifiedReferences": sorted(
                unverified,
                key=lambda item: (
                    str(item.get("targetEntitySet", "")),
                    str(item.get("fieldPath", "")),
                    str(item.get("reasonCode", "")),
                ),
            ),
            "reasonCodes": sorted(reason_codes),
        }

    def _reference_validation_failure(self, *, entity_set: str,
                                      operation: str, payload: dict[str, Any],
                                      key: str | None, operation_id: str,
                                      precondition: str | None,
                                      validation: dict[str, Any]) -> dict[str, Any]:
        status = validation.get("status")
        message = (
            "one or more submitted references are missing from their target catalog; "
            "no OData write was sent"
            if status == "missing"
            else "submitted references could not be fully verified; no OData write was sent"
        )
        receipt = {
            "ok": False,
            "entitySet": entity_set,
            "operation": operation,
            "operationId": operation_id,
            "requestHash": request_digest(
                self.target_fingerprint, operation, entity_set, key, payload, precondition,
            ),
            "targetFingerprint": self.target_fingerprint,
            "writeState": "failed",
            "phase": "reference_validation",
            "error": {
                "kind": "referenceValidation",
                "validationStatus": status,
                "message": message,
            },
            "referenceValidation": validation,
        }
        self.ledger.fail(operation_id, receipt)
        return receipt

    def _preflight_payload_references(self, *, entity_set: str,
                                      operation: str, payload: dict[str, Any],
                                      key: str | None, operation_id: str,
                                      precondition: str | None = None,
                                      reserve_failure: Callable[[], dict | None] | None = None,
                                      ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            validation = self._validate_payload_references(entity_set, payload)
        except Exception:
            validation = {
                "status": "unknown",
                "checkedReferenceCount": 0,
                "lookupCount": 0,
                "targetEntitySets": [],
                "missingReferences": [],
                "unverifiedReferences": [{
                    "fieldPath": entity_set,
                    "reasonCode": "reference_validation_internal_error",
                }],
                "reasonCodes": ["reference_validation_internal_error"],
            }
        if validation["status"] in {"verified", "not_applicable"}:
            return validation, None
        if reserve_failure is not None:
            cached = reserve_failure()
            if cached is not None:
                return validation, cached
        failure = self._reference_validation_failure(
            entity_set=entity_set,
            operation=operation,
            payload=payload,
            key=key,
            operation_id=operation_id,
            precondition=precondition,
            validation=validation,
        )
        return validation, failure

    def _prepare_write(self, *, entity_set: str, operation: str,
                       payload: dict | None, key: str | None, dry_run: bool,
                       confirm: bool, expected_target: str | None,
                       operation_id: str | None,
                       precondition: str | None = None,
                       request_precondition: Any | None = None,
                       prod_write_probe_stage: str | None = None) -> dict | None:
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        if key is not None:
            key = _normalized_key(key)
        if payload is not None:
            if not isinstance(payload, dict) or not payload:
                raise ValueError("payload must be a nonempty JSON object")
            if any(not isinstance(field, str) for field in payload.keys()):
                raise ValueError("payload field names must be strings")
            if operation != "create" and "Ref_Key" in payload:
                raise ValueError("Ref_Key cannot be changed in a payload")
            _validate_payload_field_names(entity_set, description, payload)
            self.metadata().validate_payload(entity_set, payload)
        plan = {
            "operation": operation,
            "entitySet": entity_set,
            "key": key,
            "payloadSummary": _payload_summary(payload),
            "targetFingerprint": self.target_fingerprint,
            "precondition": precondition,
            "wouldWrite": True,
            "confirmationRequired": {
                "dry_run": False,
                "confirm": True,
                "expected_target": self.target_fingerprint,
                "operation_id": "stable unique 8..128 printable ASCII characters",
            },
        }
        if payload is not None:
            compatibility_checker = getattr(
                self.metadata(), "enum_property_compatibility", None
            )
            if callable(compatibility_checker):
                compatibility = compatibility_checker(entity_set, payload)
                if compatibility:
                    plan["propertyCompatibility"] = compatibility
        if operation in _IF_MATCH_OPERATIONS:
            plan["confirmationRequired"]["expected_data_version"] = (
                precondition or "fresh DataVersion from an immediate read; sent via If-Match"
            )
        if operation in _PREFLIGHT_DATA_VERSION_OPERATIONS:
            plan["confirmationRequired"]["expected_data_version"] = (
                precondition or "fresh DataVersion from an immediate read; checked before action"
            )
            plan["preconditionScope"] = "pre-action read only; not sent as If-Match"
        if dry_run:
            return {"dryRun": True, "plan": plan, "writeEnabled": self.client.write_enabled}
        if not self.client.write_enabled and not self._prod_probe_stage_allowed(
            entity_set=entity_set,
            operation=operation,
            payload=payload,
            stage=prod_write_probe_stage,
        ):
            raise PermissionError(
                "OData writes are disabled for this configured target; "
                "call onec83_connection_status and inspect writeReadiness.reason"
            )
        if not confirm or expected_target != self.target_fingerprint:
            raise PermissionError("confirm=true and exact targetFingerprint are required")
        if not operation_id:
            raise ValueError("operation_id is required for a write receipt")
        digest = request_digest(self.target_fingerprint, operation, entity_set,
                                key, payload,
                                precondition if request_precondition is None else request_precondition)
        prepared = (counterparty_pre_post_result(self.target_fingerprint, digest)
                    if operation == "create" and payload is not None
                    and validate_counterparty_create(entity_set, payload) is not None else None)
        cached = (self.ledger.reserve(operation_id, digest, prepared_result=prepared)
                  if prepared is not None else self.ledger.reserve(operation_id, digest))
        if cached is not None:
            return {"replayedReceipt": True, **cached}
        return None

    def _completed_write_receipt(self, *, entity_set: str, operation: str,
                                 payload: dict | None, key: str | None,
                                 confirm: bool, expected_target: str | None,
                                 operation_id: str | None,
                                 precondition: str | None = None,
                                 request_precondition: Any | None = None) -> dict | None:
        if not self.client.write_enabled:
            raise PermissionError(
                "OData writes are disabled for this configured target; "
                "call onec83_connection_status and inspect writeReadiness.reason"
            )
        if not confirm or expected_target != self.target_fingerprint:
            raise PermissionError("confirm=true and exact targetFingerprint are required")
        if not operation_id:
            raise ValueError("operation_id is required for a write receipt")
        digest = request_digest(self.target_fingerprint, operation, entity_set,
                                key, payload,
                                precondition if request_precondition is None else request_precondition)
        prepared = (counterparty_pre_post_result(self.target_fingerprint, digest)
                    if operation == "create" and payload is not None
                    and validate_counterparty_create(entity_set, payload) is not None else None)
        cached = (self.ledger.lookup(operation_id, digest, prepared_result=prepared)
                  if prepared is not None else self.ledger.lookup(operation_id, digest))
        if cached is not None:
            return {"replayedReceipt": True, **cached}
        return None

    def _write_error_result(self, *, entity_set: str, operation: str,
                            payload: dict | None, key: str | None,
                            operation_id: str, precondition: str | None,
                            error: ODataClientError, phase: str,
                            request_precondition: Any | None = None,
                            reference_validation: dict[str, Any] | None = None) -> dict:
        """Finalize only a definite 1C refusal; preserve uncertain writes as pending."""
        definite_refusal = (
            phase == "write"
            and isinstance(error, ODataHTTPError)
            and error.definitive
        )
        state = "failed" if definite_refusal else "pending"
        detail: dict[str, Any] = {
            "kind": "odataHttp" if isinstance(error, ODataHTTPError) else "odataTransport",
            "message": str(error),
        }
        if isinstance(error, ODataHTTPError):
            detail["httpStatus"] = error.status_code
            if error.code:
                detail["code"] = error.code
        elif isinstance(error, ODataTransportError):
            detail["category"] = error.category
            detail["attempts"] = error.attempts
            if error.subreason:
                detail["subreason"] = error.subreason
        receipt = {
            "ok": False,
            "entitySet": entity_set,
            "operation": operation,
            "operationId": operation_id,
            "requestHash": request_digest(
                self.target_fingerprint, operation, entity_set,
                key, payload,
                precondition if request_precondition is None else request_precondition,
            ),
            "targetFingerprint": self.target_fingerprint,
            "writeState": state,
            "phase": phase,
            "error": detail,
        }
        if operation in {"Post", "Unpost"} and precondition is not None:
            receipt["expectedDataVersion"] = precondition
        if reference_validation is not None:
            receipt["referenceValidation"] = reference_validation
        if definite_refusal:
            self.ledger.fail(operation_id, receipt)
        else:
            self.ledger.record_pending(operation_id, receipt["requestHash"], receipt)
        return receipt

    def _accepted_without_readback(self, *, entity_set: str, operation: str,
                                   operation_id: str, key: str | None,
                                   error: ODataClientError,
                                   payload: dict | None = None,
                                   reference_validation: dict[str, Any] | None = None) -> dict:
        """A successful write response is durable evidence even if GET then fails."""
        receipt = {
            "entitySet": entity_set,
            "operation": operation,
            "operationId": operation_id,
            "key": key,
            "writeState": "accepted_unverified" if key else "accepted_unverified_missing_key",
            "readbackFetched": False,
            "readbackVerified": False,
            "readbackError": str(error),
        }
        fallback = getattr(error, "readback_fallback", None)
        if isinstance(fallback, dict):
            receipt["readbackFallback"] = fallback
        if reference_validation is not None:
            receipt["referenceValidation"] = reference_validation
        if operation == "create" and key is None:
            digest = request_digest(self.target_fingerprint, "create", entity_set,
                                    None, payload, None)
            receipt.update({"ok": False, "requestHash": digest,
                            "targetFingerprint": self.target_fingerprint,
                            "message": "1C accepted the create request but returned no target key; reconcile before retry"})
            self.ledger.record_pending(operation_id, digest, receipt)
        else:
            self.ledger.complete(operation_id, receipt)
        return receipt

    def get_write_receipt(self, operation_id: str) -> dict:
        """Inspect a local write receipt without sending a request to 1C."""
        receipt = self.ledger.inspect(operation_id)
        if receipt is None:
            return {"found": False, "operationId": operation_id}
        return {"found": True, "operationId": operation_id, **receipt}

    def _require_test_enum_context(self, value: Any) -> None:
        """Observed enum dispatch evidence is scoped to the TEST target only."""
        if value is None:
            return
        config = self.client.config
        if self.target_name == "prod" or getattr(config, "target_kind", None) != "test":
            raise ValueError(
                "owner-observed enum account_context is available only for a TEST target; "
                "it is not verified for PROD or an unspecified target"
            )

    def _assert_payload_enum_context_target(self, value: Any) -> None:
        if isinstance(value, dict):
            account_context = value.get("accountContext")
            if account_context is not None:
                self._require_test_enum_context(account_context)
            for child in value.values():
                self._assert_payload_enum_context_target(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_payload_enum_context_target(child)

    def _validate_and_normalize_payload(self, entity_set: str, payload: dict) -> dict:
        self._assert_payload_enum_context_target(payload)
        metadata = self.metadata()
        description = metadata.describe_entity_set(entity_set)
        _validate_payload_field_names(entity_set, description, payload)
        normalizer = getattr(metadata, "normalize_payload", None)
        if callable(normalizer):
            normalized = normalizer(entity_set, payload)
            if not isinstance(normalized, dict):
                raise ValueError("metadata payload normalizer returned a non-object")
            return normalized
        metadata.validate_payload(entity_set, payload)
        return dict(payload)

    def _pending_create_reconciliation_context(self, *, operation_id: str,
                                               entity_set: str, payload: dict,
                                               filter: str) -> dict[str, Any]:
        filter = _safe_text(filter, "filter", max_len=_MAX_TOOL_QUERY_CHARS)
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        if not _has_property(description, "Ref_Key"):
            raise ValueError("pending create reconciliation requires a Ref_Key entity")
        payload, _null_ref_key = _normalize_create_payload(description, payload)
        legacy_payload = payload
        payload = self._validate_and_normalize_payload(entity_set, payload)
        filter_fields = re.sub(r"'(?:[^']|'')*'", "''", filter)
        identifying_fields = [name for name in payload
                              if name != "Ref_Key"
                              and isinstance(payload[name], (str, int, float, bool))
                              and re.search(rf"(?<![\w]){re.escape(name)}\s+eq\b", filter_fields)]
        if not identifying_fields:
            raise ValueError("filter must use an identifying field from the original create payload")
        digest = request_digest(self.target_fingerprint, "create", entity_set,
                                None, payload, None)
        legacy_digest = request_digest(
            self.target_fingerprint, "create", entity_set, None, legacy_payload, None
        )
        prior = self.ledger.inspect(operation_id)
        if prior is None or prior["state"] != "pending":
            raise ValueError("operation_id has no pending write to reconcile")
        if prior["request_hash"] not in {digest, legacy_digest}:
            raise ValueError("original create payload does not match the pending request hash")
        page = self.client.list_entities(entity_set, top=2, skip=0,
                                         filter=filter, select="Ref_Key")
        rows = page.get("value")
        if not isinstance(rows, list) or len(rows) > 2:
            raise ValueError("reconciliation query returned an invalid page")
        if len(rows) > 1:
            raise ValueError("reconciliation filter matched multiple objects")
        return {
            "payload": payload,
            "filter": filter,
            "identifying_fields": identifying_fields,
            "digest": digest,
            "rows": rows,
        }

    def reconcile_pending_create(self, *, operation_id: str,
                                 entity_set: str, payload: dict,
                                 filter: str,
                                 expected_target: str,
                                 dry_run: bool = True,
                                 confirm: bool = False,
                                 expected_key: str | None = None) -> dict:
        """Reconcile an uncertain create with a bounded target query and exact request hash."""
        if expected_target != self.target_fingerprint:
            raise PermissionError("exact targetFingerprint is required")
        context = self._pending_create_reconciliation_context(
            operation_id=operation_id,
            entity_set=entity_set,
            payload=payload,
            filter=filter,
        )
        payload = context["payload"]
        filter = context["filter"]
        identifying_fields = context["identifying_fields"]
        digest = context["digest"]
        rows = context["rows"]
        if not rows:
            if not dry_run:
                raise ValueError("no object found; pending receipt cannot be finalized from absence alone")
            return {"dryRun": True, "operationId": operation_id,
                    "observedCount": 0, "writeState": "pending",
                    "reconciled": False,
                    "message": "No match is not proof that the original POST failed; keep pending"}
        key = None
        compared_fields = [name for name, value in payload.items()
                           if name != "Ref_Key" and isinstance(value, (str, int, float, bool))]
        if rows:
            key = rows[0].get("Ref_Key") if isinstance(rows[0], dict) else None
            key = _normalized_key(key)
            candidate, _readback_strategy = self._get_entity_with_keyed_404_fallback(
                entity_set, key,
            )
            if not isinstance(candidate, dict):
                raise ValueError("reconciliation object could not be read back")
            def matches(name: str) -> bool:
                expected = payload[name]
                actual = candidate.get(name)
                if isinstance(expected, bool):
                    return actual is expected
                if isinstance(expected, (int, float)):
                    return str(actual) == str(expected)
                return actual == expected
            if any(not matches(name) for name in compared_fields):
                raise ValueError("reconciliation object does not match original scalar payload fields")
        receipt = {
            "entitySet": entity_set,
            "operation": "create",
            "operationId": operation_id,
            "requestHash": digest,
            "targetFingerprint": self.target_fingerprint,
            "reconciled": True,
            "reconcileFilterHash": request_digest(filter),
            "identifyingFields": sorted(identifying_fields),
            "comparedFields": sorted(compared_fields),
            "observedCount": len(rows),
            "key": key,
            "readbackFetched": bool(rows),
            "readbackVerified": False,
            "writeState": "reconciled_present",
        }
        if dry_run:
            return {"dryRun": True, "plan": receipt,
                    "confirmationRequired": {"dry_run": False, "confirm": True}}
        if not confirm:
            raise PermissionError("confirm=true is required to finalize reconciliation")
        if expected_key is None or _normalized_key(expected_key) != key:
            raise PermissionError("expected_key from independent object review must match the found key")
        self.ledger.reconcile_pending(operation_id, digest, "complete", receipt)
        return receipt

    def reconcile_pending_post(
        self, *, operation_id: str, entity_set: str, key: str,
        expected_data_version: str,
        expected_accounting_movements: dict[str, Any] | None = None,
        accounting_register: str | None = None,
        expected_target: str, dry_run: bool = True, confirm: bool = False,
        max_scan: int = 1000, page_size: int = 100,
    ) -> dict:
        """Close a documented 5xx Post uncertainty from current unposted evidence only.

        This does not infer the historical outcome of the Post or write to 1C.
        """
        if expected_target != self.target_fingerprint:
            raise PermissionError("exact targetFingerprint is required")
        if not entity_set.startswith("Document_"):
            raise ValueError("only a top-level Document_* Post can be reconciled")
        key = _normalized_key(key)
        if not isinstance(expected_data_version, str) or not expected_data_version:
            raise ValueError("original expected_data_version is required")
        max_scan = _bounded_int(max_scan, "max_scan", 1, _MAX_SEARCH_SCAN)
        page_size = _bounded_int(page_size, "page_size", 1, getattr(self.client.config, "max_top", 200))
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        if not self.metadata().has_action(entity_set, "Post"):
            raise ValueError("Post is not published for this document")
        expectation = _normalize_movement_expectation(expected_accounting_movements)
        if expectation is not None:
            register = expectation["register"]
            if accounting_register is not None and accounting_register != register:
                raise ValueError("accounting_register differs from the original movement expectation")
            expectation = {**expectation, "recorderType": self._movement_recorder_type(description, expectation)}
        else:
            register = accounting_register
        if not isinstance(register, str) or not register.startswith("AccountingRegister_"):
            raise ValueError("an AccountingRegister_* is required for complete movement inspection")
        register_description = self.metadata().describe_entity_set(register)
        recorder_type = (expectation or {}).get("recorderType") or self._movement_recorder_type(description, {})
        request_precondition = self._document_action_request_precondition(
            expected_data_version, expectation,
        )
        digest = request_digest(
            self.target_fingerprint, "Post", entity_set, key, None, request_precondition,
        )
        prior = self.ledger.inspect(operation_id)
        if prior is None or prior["state"] != "pending":
            raise ValueError("operation_id has no pending Post to reconcile")
        original = prior.get("result")
        if prior["request_hash"] != digest or not isinstance(original, dict):
            raise ValueError("original Post arguments do not match the pending request hash")
        error = original.get("error")
        if (original.get("operation") != "Post"
                or original.get("entitySet") != entity_set
                or original.get("targetFingerprint") != self.target_fingerprint
                or original.get("requestHash") != digest
                or ("expectedDataVersion" in original
                    and original.get("expectedDataVersion") != expected_data_version)
                or original.get("phase") != "write"
                or not isinstance(error, dict)
                or error.get("kind") != "odataHttp"
                or type(error.get("httpStatus")) is not int
                or not 500 <= error["httpStatus"] < 600):
            raise ValueError("pending receipt is not a saved write-phase HTTP 5xx Post")

        def blocked(reason: str) -> dict:
            return {
                "ok": False, "operationId": operation_id, "dryRun": dry_run,
                "writeState": "pending", "reconciled": False,
                "blocker": reason,
            }

        # Every unfiltered page must be accounted for. A named-key 404 is never
        # used as negative evidence: some 1C publications return it for an
        # existing unposted document with no record set.
        if not all(_has_property(register_description, field)
                   for field in ("Recorder", "Recorder_Type")):
            return blocked("register does not publish a stable Recorder/Recorder_Type scan key")
        try:
            with diagnostic_stage("reconciliation"):
                before, _readback_strategy = self._get_entity_with_keyed_404_fallback(
                    entity_set, key,
                )
                if not isinstance(before, dict):
                    return blocked("document readback is invalid")
                if _normalized_key(before.get("Ref_Key")) != key:
                    return blocked("document key differs from the requested key")
                if before.get("Posted") is not False or before.get("DeletionMark") is not False:
                    return blocked("document is posted, deleted, or its state is unavailable")
                if before.get("DataVersion") != expected_data_version:
                    return blocked("document DataVersion differs from the original Post precondition")

                count_before = self.client.count_entities(register)
                if type(count_before) is not int or count_before < 0 or count_before > max_scan:
                    return blocked("register count is invalid or exceeds max_scan")
                if count_before == 0:
                    empty_page = self.client.list_entities(
                        register, top=1, skip=0,
                        select="Recorder,Recorder_Type",
                        orderby="Recorder,Recorder_Type",
                    )
                    if (not isinstance(empty_page, dict)
                            or empty_page.get("value") != []
                            or empty_page.get("next_link") is not None
                            or ("count" in empty_page and
                                (type(empty_page["count"]) is not int or empty_page["count"] != 0))):
                        return blocked("zero register count disagrees with the first page")
                scanned = 0
                seen: set[tuple[str, str]] = set()
                while scanned < count_before:
                    top = min(page_size, count_before - scanned)
                    page = self.client.list_entities(
                        register, top=top, skip=scanned,
                        select="Recorder,Recorder_Type",
                        orderby="Recorder,Recorder_Type",
                    )
                    rows = page.get("value") if isinstance(page, dict) else None
                    if (not isinstance(rows, list) or len(rows) != top
                            or page.get("next_link") is not None
                            or ("count" in page and (type(page["count"]) is not int
                                                      or page["count"] != count_before))):
                        return blocked("register scan has an incomplete or unsupported page")
                    for row in rows:
                        if not isinstance(row, dict):
                            return blocked("register scan returned an invalid row")
                        row_key = _normalized_key(row.get("Recorder"))
                        row_type = row.get("Recorder_Type")
                        if not isinstance(row_type, str) or not row_type:
                            return blocked("register scan lacks recorder identity")
                        identity = (row_key, row_type)
                        if identity in seen:
                            return blocked("register scan repeated a row identity")
                        seen.add(identity)
                        if row_key == key and row_type == recorder_type:
                            return blocked("register contains a row for this document, active or inactive")
                    scanned += len(rows)
                count_after = self.client.count_entities(register)
                if type(count_after) is not int or count_after != count_before:
                    return blocked("register count changed during the scan")
                after, _readback_strategy = self._get_entity_with_keyed_404_fallback(
                    entity_set, key,
                )
                if not isinstance(after, dict):
                    return blocked("final document readback is invalid")
                if _normalized_key(after.get("Ref_Key")) != key:
                    return blocked("document key changed during the scan")
                if (after.get("DataVersion") != expected_data_version
                        or after.get("Posted") is not False
                        or after.get("DeletionMark") is not False):
                    return blocked("document changed during the scan")
        except (ODataClientError, ValueError) as exc:
            return blocked(f"complete register/document read unavailable: {exc}")

        receipt = {
            "ok": False, "entitySet": entity_set, "operation": "Post",
            "operationId": operation_id, "key": key,
            "targetFingerprint": self.target_fingerprint,
            "requestHash": digest, "expectedDataVersion": expected_data_version,
            "writeState": "reconciled_observed_unposted", "reconciled": True,
            "originalReceipt": original,
            "observation": {
                "observedAtUtc": datetime.now(timezone.utc).isoformat(),
                "posted": False, "deletionMark": False,
                "dataVersion": expected_data_version,
                "register": register, "recorderType": recorder_type,
                "registerRowsScanned": scanned, "registerCountBefore": count_before,
                "registerCountAfter": count_after, "matchingRecorderRows": 0,
                "snapshotConsistent": False,
            },
            "message": "Currently unposted with no register row observed; historical Post outcome is not proven",
        }
        if dry_run:
            preview = {**receipt, "writeState": "pending", "reconciled": False,
                       "proposedWriteState": "reconciled_observed_unposted"}
            if expectation is None:
                return {"dryRun": True, "plan": preview, "canFinalize": False,
                        "blocker": "original Post did not bind a register to its request hash"}
            return {"dryRun": True, "plan": preview, "canFinalize": True,
                    "confirmationRequired": {"dry_run": False, "confirm": True}}
        if not confirm:
            raise PermissionError("confirm=true is required to finalize local Post reconciliation")
        if expectation is None:
            return blocked("original Post did not bind a register to its request hash; observation cannot finalize the receipt")
        self.ledger.reconcile_pending(operation_id, digest, "reconciled", receipt)
        return receipt

    def _assert_current_data_version(
        self,
        entity_set: str,
        key: str,
        expected_data_version: str | None,
    ) -> dict:
        if not expected_data_version:
            raise ValueError("expected_data_version is required for an applied document action")
        current, _readback_strategy = self._get_entity_with_keyed_404_fallback(
            entity_set, key,
        )
        if not isinstance(current, dict):
            raise ValueError("current object was not returned by OData")
        if current.get("DataVersion") != expected_data_version:
            raise ValueError("DataVersion changed; reread the document")
        return current

    def _validate_movement_expectation_metadata(
        self,
        expectation: dict[str, Any] | None,
    ) -> None:
        if expectation is None:
            return
        self.metadata().describe_entity_set(expectation["register"])

    def _movement_recorder_type(
        self,
        description: dict[str, Any],
        expectation: dict[str, Any],
    ) -> str:
        explicit = expectation.get("recorderType")
        if isinstance(explicit, str) and explicit:
            return explicit
        entity_type = description.get("entity_type")
        if isinstance(entity_type, str) and entity_type:
            return entity_type
        entity_type_name = description.get("entity_type_name")
        if isinstance(entity_type_name, str) and entity_type_name:
            return (
                entity_type_name
                if "." in entity_type_name
                else f"StandardODATA.{entity_type_name}"
            )
        raise ValueError("document metadata does not expose entity type for movement recorder")

    def _document_action_request_precondition(
        self,
        expected_data_version: str | None,
        movement_expectation: dict[str, Any] | None,
    ) -> Any:
        if movement_expectation is None:
            return expected_data_version
        return {
            "expectedDataVersion": expected_data_version,
            "expectedAccountingMovements": movement_expectation,
        }

    def _verify_accounting_movements(
        self,
        key: str,
        expectation: dict[str, Any],
    ) -> dict[str, Any]:
        register = expectation["register"]
        recorder_type = expectation["recorderType"]
        try:
            payload = self.client.get_register_record_set(register, key, recorder_type)
            rows = _recordset_rows(payload)
        except (ODataClientError, ValueError) as exc:
            return {
                "requested": True,
                "verified": False,
                "status": "unavailable",
                "register": register,
                "recorderKey": key,
                "recorderType": recorder_type,
                "reason": str(exc),
            }

        amount_fields = expectation["amountFields"]
        debit_fields = expectation["debitAccountFields"]
        credit_fields = expectation["creditAccountFields"]
        active_rows = [
            row for row in rows
            if _active_row(row, expectation["activeFields"], expectation["activeValue"])
        ]
        tolerance = Decimal(expectation["tolerance"])
        checks = []
        diagnostics = []
        matched_row_indexes: set[int] = set()
        all_verified = True
        for expected in expectation["rows"]:
            expected_amount = Decimal(expected["amount"])
            acceptable_debits = expected.get("debitAnyOf", [expected.get("debit")])
            debit_label = " or ".join(acceptable_debits)
            matching_rows = []
            missing_amount_rows = 0
            actual_total = Decimal("0")
            for row_index, row in enumerate(active_rows):
                debit_values = _field_text_values(row, debit_fields)
                credit_values = _field_text_values(row, credit_fields)
                if (not any(_movement_account_matches(debit, debit_values)
                            for debit in acceptable_debits)
                        or not _movement_account_matches(expected["credit"], credit_values)):
                    continue
                matched_row_indexes.add(row_index)
                amount_field, amount = _first_decimal_field(row, amount_fields)
                summary = _row_summary(
                    row,
                    amount_fields=amount_fields,
                    debit_fields=debit_fields,
                    credit_fields=credit_fields,
                )
                summary["amountField"] = amount_field
                matching_rows.append(summary)
                if amount is None:
                    missing_amount_rows += 1
                    continue
                actual_total += amount
            amount_delta = abs(actual_total - expected_amount)
            row_verified = (bool(matching_rows) and missing_amount_rows == 0
                            and amount_delta <= tolerance
                            and ("debitAnyOf" not in expected or len(matching_rows) == 1))
            all_verified = all_verified and row_verified
            if not matching_rows:
                diagnostics.append(
                    f"no active accounting movement for debit {debit_label} "
                    f"and credit {expected['credit']}"
                )
            elif missing_amount_rows:
                diagnostics.append(
                    f"{missing_amount_rows} matching accounting movement row(s) have no readable amount"
                )
            elif "debitAnyOf" in expected and len(matching_rows) != 1:
                diagnostics.append(
                    f"expected one active accounting movement for debit {debit_label}, "
                    f"found {len(matching_rows)}"
                )
            elif amount_delta > tolerance:
                diagnostics.append(
                    f"amount mismatch for debit {debit_label} credit {expected['credit']}: "
                    f"expected {expected['amount']}, actual {_decimal_text(actual_total)}"
                )
            checks.append({
                "expected": expected,
                "actualTotal": _decimal_text(actual_total),
                "matchedRowCount": len(matching_rows),
                "missingAmountRowCount": missing_amount_rows,
                "amountMatches": amount_delta <= tolerance,
                "verified": row_verified,
                "matchedRows": matching_rows[:10],
            })
        extra_rows = [
            row for row_index, row in enumerate(active_rows)
            if row_index not in matched_row_indexes
        ]
        if extra_rows and not expectation["allowExtraRows"]:
            all_verified = False
            diagnostics.append(
                f"{len(extra_rows)} extra active accounting movement row(s) were not expected"
            )
        return {
            "requested": True,
            "verified": all_verified,
            "status": "verified" if all_verified else "mismatch",
            "register": register,
            "recorderKey": key,
            "recorderType": recorder_type,
            "actualRowCount": len(rows),
            "activeRowCount": len(active_rows),
            "expectedRowCount": len(expectation["rows"]),
            "tolerance": expectation["tolerance"],
            "amountFields": amount_fields,
            "debitAccountFields": debit_fields,
            "creditAccountFields": credit_fields,
            "allowExtraRows": expectation["allowExtraRows"],
            "checks": checks,
            "diagnostics": diagnostics,
            "extraRows": [
                _row_summary(
                    row,
                    amount_fields=amount_fields,
                    debit_fields=debit_fields,
                    credit_fields=credit_fields,
                )
                for row in extra_rows[:10]
            ],
            "sampleRows": [
                _row_summary(
                    row,
                    amount_fields=amount_fields,
                    debit_fields=debit_fields,
                    credit_fields=credit_fields,
                )
                for row in active_rows[:10]
            ],
        }

    @staticmethod
    def _accounting_movement_error(verification: dict[str, Any], write_state: str) -> dict[str, Any]:
        status = verification.get("status")
        if status == "unavailable":
            message = (
                "1C accepted Post, but the expected accounting movements could not be "
                "verified through OData. Inspect the write receipt before taking action; "
                "do not repeat Post automatically."
            )
        else:
            message = (
                "1C accepted Post, but the accounting movements do not match the "
                "expected movements. Inspect the write receipt before taking action; "
                "do not repeat Post automatically."
            )
        details: dict[str, Any] = {"status": status}
        reason = verification.get("reason")
        if isinstance(reason, str) and reason:
            details["reason"] = reason
        diagnostics = verification.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics:
            details["diagnostics"] = diagnostics
        return {
            "kind": "accountingMovementVerification",
            "code": write_state,
            "message": message,
            "details": details,
        }

    def _capabilities_for(self, entity_set: str, description: dict) -> dict:
        is_tabular_row = self._is_tabular_row_entity(entity_set, description)
        post_published = self.metadata().has_action(entity_set, "Post")
        unpost_published = self.metadata().has_action(entity_set, "Unpost")
        has_deletion_mark = _has_property(description, "DeletionMark")
        create_tool_available = not is_tabular_row
        update_tool_available = not is_tabular_row
        deletion_mark_tool_available = has_deletion_mark and not is_tabular_row
        post_tool_available = post_published and not is_tabular_row
        unpost_tool_available = unpost_published and not is_tabular_row
        capabilities = {
            "canRead": True,
            "canCreate": None if create_tool_available else False,
            "canUpdate": None if update_tool_available else False,
            "canSetDeletionMark": None if deletion_mark_tool_available else False,
            "canPost": None if post_tool_available else False,
            "canUnpost": None if unpost_tool_available else False,
            "toolAvailable": {
                "create": create_tool_available,
                "update": update_tool_available,
                "setDeletionMark": deletion_mark_tool_available,
                "post": post_tool_available,
                "unpost": unpost_tool_available,
            },
            "publishedActions": {"Post": post_published, "Unpost": unpost_published},
            "rightsKnown": False,
            "tabularSectionsUseFullReplacement": bool(description.get("tabularSections")),
            "isTabularRowEntity": is_tabular_row,
            "requiresWriteConfirmation": True,
        }
        if description.get("parentDocument"):
            capabilities["parentDocument"] = description["parentDocument"]
        if description.get("tabularSection"):
            capabilities["tabularSection"] = description["tabularSection"]
        if entity_set == _COUNTERPARTY_ENTITY_SET:
            capabilities["businessRules"] = {
                "counterpartyClassification": {
                    "field": _COUNTERPARTY_CLASSIFICATION_FIELD,
                    "observedValues": sorted(_OBSERVED_COUNTERPARTY_CLASSIFICATIONS),
                    "observedValuesEvidence": "read-only sample on 2026-09-28",
                    "enforcement": "advisory_only",
                    "individualEntrepreneurStatus": "unknown",
                    "migrationWritePolicy": "hold_until_verified",
                }
            }
        return capabilities

    def _is_tabular_row_entity(self, entity_set: str, description: dict) -> bool:
        entity_type_name = description.get("entity_type_name")
        if not entity_type_name and isinstance(description.get("entity_type"), str):
            entity_type_name = description["entity_type"].rsplit(".", 1)[-1]
        if (
            entity_set.startswith("Document_")
            and isinstance(entity_type_name, str)
            and entity_type_name.endswith("_RowType")
        ):
            return True
        if "isTabularRowEntity" in description:
            return description.get("isTabularRowEntity") is True
        if not entity_set.startswith("Document_"):
            return False
        return False

    def _ensure_direct_write_allowed(self, entity_set: str, description: dict) -> None:
        if not self._is_tabular_row_entity(entity_set, description):
            return
        parent = description.get("parentDocument")
        section = description.get("tabularSection")
        if parent:
            detail = f"write the parent document {parent}"
            if section:
                detail += f" section {section}"
            raise ValueError(
                "standalone document tabular-row entity sets are read-only; "
                f"{detail} with replace_tabular_section"
            )
        raise ValueError(
            "standalone document tabular-row entity sets are read-only; "
            "write the parent document with replace_tabular_section"
        )

    def _validate_tabular_replacement_rows(
        self,
        current_rows: list[dict],
        replacement_rows: list[dict],
    ) -> dict[str, Any]:
        if not replacement_rows:
            return {"replacementClearsSection": True}
        for row in replacement_rows:
            if any(not isinstance(field, str) for field in row.keys()):
                raise ValueError("tabular row field names must be strings")
        replacement_field_sets = {frozenset(row.keys()) for row in replacement_rows}
        if len(replacement_field_sets) != 1:
            raise ValueError("all replacement tabular rows must use identical field coverage")
        replacement_fields = set(next(iter(replacement_field_sets)))
        if "LineNumber" not in replacement_fields:
            raise ValueError("replacement tabular rows must include LineNumber for row identity")
        line_numbers = [
            _line_number_identity(row["LineNumber"], "replacement tabular rows")
            for row in replacement_rows
        ]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("replacement tabular rows must have unique LineNumber values")
        if current_rows:
            current_field_sets = {frozenset(row.keys()) for row in current_rows}
            current_fields = set().union(*(set(row.keys()) for row in current_rows))
            if replacement_fields != current_fields:
                raise ValueError(
                    "replacement tabular rows must include the exact field coverage "
                    "returned by the current OData read"
                )
            if len(current_field_sets) != 1:
                raise ValueError("current tabular rows have inconsistent field coverage; reread with full rows")
            current_line_numbers = [
                _line_number_identity(row.get("LineNumber"), "current tabular rows")
                for row in current_rows
            ]
            if (
                len(current_rows) == len(replacement_rows)
                and len(current_line_numbers) == len(set(current_line_numbers))
                and set(line_numbers) != set(current_line_numbers)
            ):
                raise ValueError("replacement tabular rows must preserve LineNumber identity")
        return {"replacementRowFields": sorted(replacement_fields)}

    def _queue_worker_bridge(self) -> "Bridge":
        worker = getattr(self._queue_worker_context, "bridge", None)
        if worker is not None:
            return worker
        client_factory = getattr(self.client, "for_worker", None)
        if callable(client_factory):
            worker = Bridge(
                client_factory(),
                ledger=self.ledger,
                metadata=self.metadata(),
                target_name=self.target_name,
            )
        else:
            worker = self
        self._queue_worker_context.bridge = worker
        return worker

    @staticmethod
    def _queue_id(value: Any, label: str) -> str:
        if (not isinstance(value, str) or not 8 <= len(value) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError(f"{label} must be a stable 8..128 character printable ASCII identifier")
        return value

    def _process_document_queue_item(self, index: int, item: dict,
                                    expected_target: str) -> dict:
        started = perf_counter()
        phase = "create"
        try:
            bridge = self._queue_worker_bridge()
            created = bridge.create_entity(
                item["entity_set"], item["payload"], dry_run=False,
                confirm=True, expected_target=expected_target,
                operation_id=item["operation_id"],
            )
            state = created.get("writeState", "accepted")
            key = created.get("key")
            if not key and isinstance(created.get("created"), dict):
                key = created["created"].get("Ref_Key")
            if not key and isinstance(created.get("readback"), dict):
                key = created["readback"].get("Ref_Key")

            if created.get("ok") is False:
                uncertain = state in {
                    "pending", "accepted_unverified", "accepted_unverified_missing_key",
                    "accepted_movement_unavailable",
                }
                failed_result = {
                    "index": index,
                    "operationId": item["operation_id"],
                    "postOperationId": item.get("post_operation_id"),
                    "entitySet": item["entity_set"],
                    "key": key,
                    "ok": False,
                    "state": "needs_reconciliation" if uncertain else "failed",
                    "createState": state,
                    "error": created.get("error") or {"message": created.get("message", "create failed")},
                    "elapsedMs": round((perf_counter() - started) * 1000, 1),
                }
                if isinstance(created.get("referenceValidation"), dict):
                    failed_result["referenceValidation"] = created["referenceValidation"]
                return failed_result
            if not key:
                return {
                    "index": index,
                    "operationId": item["operation_id"],
                    "postOperationId": item.get("post_operation_id"),
                    "entitySet": item["entity_set"],
                    "ok": False,
                    "state": "needs_reconciliation",
                    "createState": state,
                    "error": {"message": "create returned no Ref_Key; inspect the write receipt before retrying"},
                    "elapsedMs": round((perf_counter() - started) * 1000, 1),
                }

            result: dict[str, Any] = {
                "index": index,
                "operationId": item["operation_id"],
                "entitySet": item["entity_set"],
                "key": key,
                "createState": state,
                "createReplayed": created.get("replayedReceipt") is True,
                "createReadbackFetched": created.get("readbackFetched") is True,
                "ok": True,
                "state": "created",
            }
            verification = created.get("readbackVerification")
            if isinstance(verification, dict) and (
                verification.get("mismatchedFields")
                or verification.get("missingFields")
                or {"key_mismatch", "key_missing_or_invalid"}.intersection(
                    verification.get("reasonCodes") or []
                )
            ):
                result.update({
                    "ok": False,
                    "state": "needs_reconciliation",
                    "error": {"message": "create readback contradicts submitted fields or key; reconcile the created document before continuing"},
                    "elapsedMs": round((perf_counter() - started) * 1000, 1),
                })
                return result
            if isinstance(created.get("referenceValidation"), dict):
                result["referenceValidation"] = created["referenceValidation"]
            if not item["post"]:
                if not result["createReadbackFetched"]:
                    result.update({
                        "ok": False,
                        "state": "needs_reconciliation",
                        "error": {"message": "create was accepted but readback failed; verify the returned key before considering the row complete"},
                    })
                result["elapsedMs"] = round((perf_counter() - started) * 1000, 1)
                return result

            prior_post = bridge.ledger.inspect(item["post_operation_id"])
            if prior_post is not None:
                prior_result = prior_post.get("result") or {}
                if prior_post.get("state") == "pending":
                    result.update({
                        "postOperationId": item["post_operation_id"],
                        "ok": False,
                        "state": "needs_reconciliation",
                        "postState": prior_result.get("writeState", "pending"),
                        "error": {"message": "post receipt is pending; reconcile it before resuming this row"},
                        "elapsedMs": round((perf_counter() - started) * 1000, 1),
                    })
                    return result
                prior_version = prior_result.get("expectedDataVersion")
                movement_expectation = _normalize_movement_expectation(
                    item.get("expected_accounting_movements")
                )
                if movement_expectation is not None:
                    description = bridge.metadata().describe_entity_set(item["entity_set"])
                    movement_expectation = {
                        **movement_expectation,
                        "recorderType": bridge._movement_recorder_type(
                            description, movement_expectation
                        ),
                    }
                if isinstance(prior_version, str):
                    request_precondition = bridge._document_action_request_precondition(
                        prior_version, movement_expectation
                    )
                    expected_hash = request_digest(
                        bridge.target_fingerprint, "Post", item["entity_set"],
                        key, None, request_precondition,
                    )
                    if prior_post.get("request_hash") != expected_hash:
                        raise ValueError("post_operation_id was already used for a different request")
                    if prior_post.get("state") == "reconciled":
                        result.update({
                            "postOperationId": item["post_operation_id"],
                            "ok": False,
                            "state": "failed",
                            "nextAction": "needs_new_post_operation",
                            "postState": prior_result.get("writeState"),
                            "postReplayed": True,
                            "error": {"message": "original Post was locally reconciled as currently unposted; correct the existing document and use a new Post operation_id with fresh DataVersion"},
                            "elapsedMs": round((perf_counter() - started) * 1000, 1),
                        })
                        return result
                    prior_state = prior_result.get("writeState", "accepted")
                    movement = prior_result.get("accountingMovementVerification")
                    movements_verified = prior_result.get("accountingMovementsVerified") is True
                    if isinstance(movement, dict):
                        movements_verified = movement.get("verified") is True
                    prior_ok = (
                        prior_post.get("state") == "complete"
                        and prior_result.get("ok") is not False
                        and prior_state not in {
                            "pending", "accepted_unverified", "accepted_movement_unavailable",
                            "accepted_movement_mismatch",
                        }
                    )
                    result.update({
                        "postOperationId": item["post_operation_id"],
                        "postState": prior_state,
                        "postReadbackFetched": prior_result.get("readbackFetched") is True,
                        "accountingMovementsVerified": movements_verified,
                        "accountingMovementVerificationRequested": movement_expectation is not None,
                        "postReplayed": True,
                        "ok": prior_ok,
                        "state": "posted" if prior_ok else (
                            "needs_reconciliation" if prior_state in {
                                "pending", "accepted_unverified", "accepted_movement_unavailable",
                            } else "failed"
                        ),
                        "elapsedMs": round((perf_counter() - started) * 1000, 1),
                    })
                    if prior_result.get("error"):
                        result["error"] = prior_result["error"]
                    if isinstance(movement, dict) and movement.get("diagnostics"):
                        result["diagnostics"] = movement["diagnostics"]
                    return result
                if prior_post.get("state") in {"complete", "failed"}:
                    return {
                        **result,
                        "postOperationId": item["post_operation_id"],
                        "ok": False,
                        "state": "needs_reconciliation",
                        "postState": prior_result.get("writeState", prior_post["state"]),
                        "error": {"message": "saved post receipt predates resumable queue metadata; inspect it before retrying"},
                        "elapsedMs": round((perf_counter() - started) * 1000, 1),
                    }

            readback = created.get("readback")
            if not isinstance(readback, dict) or not readback.get("DataVersion"):
                if isinstance(created.get("dataVersion"), str):
                    readback = {"DataVersion": created["dataVersion"]}
            if not isinstance(readback, dict) or not readback.get("DataVersion"):
                phase = "readback"
                readback = bridge.get_entity(item["entity_set"], key)
            if not isinstance(readback, dict) or not isinstance(readback.get("DataVersion"), str):
                raise ValueError("fresh DataVersion was not returned for the created document")

            phase = "post"
            posted = bridge.post_document(
                item["entity_set"], key, dry_run=False, confirm=True,
                expected_target=expected_target,
                operation_id=item["post_operation_id"],
                expected_data_version=readback["DataVersion"],
                expected_accounting_movements=item.get("expected_accounting_movements"),
            )
            post_state = posted.get("writeState", "accepted")
            movements = posted.get("accountingMovementVerification")
            movement_verified = posted.get("accountingMovementsVerified") is True
            if isinstance(movements, dict):
                movement_verified = movements.get("verified") is True
            post_ok = posted.get("ok") is not False and post_state not in {
                "pending", "accepted_unverified", "accepted_unverified_missing_key",
                "accepted_movement_unavailable", "accepted_movement_mismatch",
            }
            result.update({
                "postOperationId": item["post_operation_id"],
                "postState": post_state,
                "postReadbackFetched": posted.get("readbackFetched") is True,
                "accountingMovementsVerified": movement_verified,
                "accountingMovementVerificationRequested": item.get("expected_accounting_movements") is not None,
                "ok": post_ok,
                "state": "posted" if post_ok else (
                    "needs_reconciliation" if post_state in {
                        "pending", "accepted_unverified", "accepted_movement_unavailable",
                    } else "failed"
                ),
            })
            if posted.get("error"):
                result["error"] = posted["error"]
            elif posted.get("readbackError"):
                result["error"] = {"message": posted["readbackError"]}
            if movements and movements.get("diagnostics"):
                result["diagnostics"] = movements["diagnostics"]
            result["elapsedMs"] = round((perf_counter() - started) * 1000, 1)
            return result
        except (ODataClientError, UncertainWrite, ValueError, PermissionError) as exc:
            uncertain = isinstance(exc, (ODataTransportError, UncertainWrite)) or phase in {"readback", "post"}
            return {
                "index": index,
                "operationId": item["operation_id"],
                "postOperationId": item.get("post_operation_id"),
                "entitySet": item["entity_set"],
                "ok": False,
                "state": "needs_reconciliation" if uncertain else "failed",
                "failedPhase": phase,
                "error": {"message": str(exc)},
                "elapsedMs": round((perf_counter() - started) * 1000, 1),
            }
        except Exception:
            return {
                "index": index,
                "operationId": item["operation_id"],
                "postOperationId": item.get("post_operation_id"),
                "entitySet": item["entity_set"],
                "ok": False,
                "state": "needs_reconciliation",
                "failedPhase": phase,
                "error": {"message": "unexpected local error; inspect operation receipts before resuming"},
                "elapsedMs": round((perf_counter() - started) * 1000, 1),
            }

    def _run_document_queue_item_with_diagnostics(
        self, row_token: str, index: int, item: dict, expected_target: str,
    ) -> dict:
        with diagnostic_row(row_token) as counter:
            result = self._process_document_queue_item(index, item, expected_target)
            set_row_outcome(counter, result)
            return result

    def apply_document_queue(self, items: list[dict], *, dry_run: bool = True,
                             confirm: bool = False,
                             expected_target: str | None = None,
                             max_concurrency: int = 1) -> dict:
        """Preflight and process independent document creates/posts with durable row receipts."""
        if type(dry_run) is not bool or type(confirm) is not bool:
            raise ValueError("dry_run and confirm must be booleans")
        max_concurrency = _bounded_int(
            max_concurrency, "max_concurrency", 1, _MAX_DOCUMENT_QUEUE_CONCURRENCY
        )
        if not isinstance(items, list) or not 1 <= len(items) <= _MAX_DOCUMENT_QUEUE_ITEMS:
            raise ValueError(f"items must contain 1..{_MAX_DOCUMENT_QUEUE_ITEMS} documents")
        try:
            encoded_items = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("items must contain JSON-compatible values") from None
        if len(encoded_items.encode("utf-8")) > _MAX_DOCUMENT_QUEUE_BYTES:
            raise ValueError("items payload exceeds the 1 MB queue limit")

        allowed_fields = {
            "entity_set", "payload", "operation_id", "post", "post_operation_id",
            "expected_accounting_movements",
        }
        operation_ids: set[str] = set()
        prepared: list[dict] = []
        self.metadata()
        for index, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                raise ValueError(f"items[{index}] must be an object")
            unknown_fields = sorted(set(raw_item) - allowed_fields)
            if unknown_fields:
                raise ValueError(f"items[{index}] contains unsupported fields: {', '.join(unknown_fields)}")
            entity_set = raw_item.get("entity_set")
            if not isinstance(entity_set, str) or not entity_set.startswith("Document_"):
                raise ValueError(f"items[{index}].entity_set must be a published Document_* set")
            operation_id = self._queue_id(raw_item.get("operation_id"), f"items[{index}].operation_id")
            if operation_id in operation_ids:
                raise ValueError("queue operation ids must be unique")
            operation_ids.add(operation_id)
            payload = raw_item.get("payload")
            if not isinstance(payload, dict):
                raise ValueError(f"items[{index}].payload must be an object")
            post = raw_item.get("post", False)
            if type(post) is not bool:
                raise ValueError(f"items[{index}].post must be a boolean")
            post_operation_id = raw_item.get("post_operation_id")
            movements = raw_item.get("expected_accounting_movements")
            if post:
                post_operation_id = self._queue_id(
                    post_operation_id, f"items[{index}].post_operation_id"
                )
                if post_operation_id in operation_ids:
                    raise ValueError("queue operation ids must be unique")
                operation_ids.add(post_operation_id)
            elif post_operation_id is not None or movements is not None:
                raise ValueError(f"items[{index}] post options require post=true")

            create_plan = self.create_entity(entity_set, payload, dry_run=True)
            item = {
                "entity_set": entity_set,
                "payload": payload,
                "operation_id": operation_id,
                "post": post,
            }
            post_plan = None
            if post:
                description = self.metadata().describe_entity_set(entity_set)
                self._ensure_direct_write_allowed(entity_set, description)
                if not self.metadata().has_action(entity_set, "Post"):
                    raise ValueError(f"Post is not published for items[{index}].entity_set")
                movement_expectation = _normalize_movement_expectation(movements)
                self._validate_movement_expectation_metadata(movement_expectation)
                item.update({
                    "post_operation_id": post_operation_id,
                    "expected_accounting_movements": movements,
                })
                post_plan = {
                    "action": "Post",
                    "operationId": post_operation_id,
                    "expectedAccountingMovements": movement_expectation is not None,
                    "dataVersion": "read after create and checked immediately before Post",
                }
            prepared.append({
                **item,
                "index": index,
                "create_plan": create_plan["plan"],
                "post_plan": post_plan,
            })

        if dry_run:
            return {
                "dryRun": True,
                "total": len(prepared),
                "maximumConcurrency": max_concurrency,
                "writeEnabled": self.client.write_enabled,
                "results": [{
                    "index": item["index"],
                    "operationId": item["operation_id"],
                    "entitySet": item["entity_set"],
                    "createPlan": item["create_plan"],
                    "postPlan": item["post_plan"],
                } for item in prepared],
            }
        if not self.client.write_enabled:
            raise PermissionError("OData writes are disabled for this configured target")
        if not confirm or expected_target != self.target_fingerprint:
            raise PermissionError("confirm=true and exact targetFingerprint are required")

        # Read Keychain once on the caller thread so parallel workers never race a credential prompt.
        password_reader = getattr(self.client, "_password_value", None)
        if callable(password_reader):
            password_reader()
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=min(max_concurrency, len(prepared)),
                                thread_name_prefix="onec83-queue") as executor:
            futures = []
            for item in prepared:
                context = copy_context()
                row_token = uuid4().hex
                futures.append(executor.submit(
                    context.run,
                    self._run_document_queue_item_with_diagnostics,
                    row_token,
                    item["index"],
                    item,
                    expected_target,
                ))
            results = [future.result() for future in futures]
        elapsed_ms = round((perf_counter() - started) * 1000, 1)
        succeeded = sum(result["ok"] is True for result in results)
        uncertain = sum(result["state"] == "needs_reconciliation" for result in results)
        failed = len(results) - succeeded - uncertain
        return {
            "dryRun": False,
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "needsReconciliation": uncertain,
            "maxConcurrency": min(max_concurrency, len(results)),
            "elapsedMs": elapsed_ms,
            "results": results,
            "resumePolicy": "resubmit unchanged rows with the same operation ids; reconcile pending receipts before retrying",
        }

    def create_entity(self, entity_set: str, payload: dict, dry_run: bool = True,
                      confirm: bool = False, expected_target: str | None = None,
                      operation_id: str | None = None,
                      _prod_write_probe_stage: str | None = None) -> dict:
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        payload, null_ref_key = _normalize_create_payload(description, payload)
        preflight_person_names(entity_set, payload)
        payload = self._validate_and_normalize_payload(entity_set, payload)
        unp = validate_counterparty_create(entity_set, payload)
        if unp is not None and not dry_run:
            replay = self._completed_write_receipt(
                entity_set=entity_set, operation="create", payload=payload, key=None,
                confirm=confirm, expected_target=expected_target, operation_id=operation_id,
            )
            if replay is not None:
                return replay
            with guarded_counterparty_create(
                ledger=self.ledger,
                target=counterparty_target_fingerprint(self.client.config.odata_base_url), unp=unp,
                operation_id=operation_id, payload=payload, search=self.search_entities,
                coordination_ledger=self._counterparty_coordination_ledger,
                claim_ledgers=self._counterparty_ledgers,
                receipt_target=self.target_fingerprint,
                target_bindings=self._counterparty_target_bindings,
            ) as check_uniqueness:
                result = self._create_validated_entity(
                    entity_set, payload, description, null_ref_key, dry_run, confirm,
                    expected_target, operation_id, _prod_write_probe_stage, check_uniqueness,
                )
                saved = self.ledger.inspect(operation_id)
                uniqueness = (saved["result"] or {}).get("counterpartyUniqueness")
                return {**result, "counterpartyUniqueness": uniqueness} if uniqueness else result
        return self._create_validated_entity(
            entity_set, payload, description, null_ref_key, dry_run, confirm,
            expected_target, operation_id, _prod_write_probe_stage,
        )

    def _create_validated_entity(self, entity_set: str, payload: dict, description: dict,
                                 null_ref_key: bool, dry_run: bool, confirm: bool,
                                 expected_target: str | None, operation_id: str | None,
                                 _prod_write_probe_stage: str | None,
                                 check_counterparty_uniqueness: Callable[[], dict] | None = None) -> dict:
        def prepare() -> dict | None:
            return self._prepare_write(entity_set=entity_set, operation="create",
                                       payload=payload, key=None, dry_run=dry_run,
                                       confirm=confirm, expected_target=expected_target,
                                       operation_id=operation_id,
                                       prod_write_probe_stage=_prod_write_probe_stage)

        if check_counterparty_uniqueness is not None:
            reference_validation, reference_failure = self._preflight_payload_references(
                entity_set=entity_set, operation="create", payload=payload,
                key=None, operation_id=operation_id, reserve_failure=prepare,
            )
            if reference_failure is not None:
                return reference_failure
            uniqueness = check_counterparty_uniqueness()
        # A rejected uniqueness check must not consume the operation ID.
        # For cards the remaining work before transport is local, with metadata
        # already loaded and all reference network reads completed above.
        preview = prepare()
        if preview is not None:
            if dry_run and validate_counterparty_create(entity_set, payload) is not None:
                preview["plan"]["counterpartyUniqueness"] = {
                    "status": "not_checked", "readsPerformed": False,
                    "checkedBeforeActualPost": True,
                }
            return preview
        if check_counterparty_uniqueness is None:
            reference_validation, reference_failure = self._preflight_payload_references(
                entity_set=entity_set, operation="create", payload=payload,
                key=None, operation_id=operation_id,
            )
            if reference_failure is not None:
                return reference_failure
        if reference_validation.get("status") == "not_applicable":
            reference_validation = None
        if check_counterparty_uniqueness is not None:
            digest = self.ledger.inspect(operation_id)["request_hash"]
            # Commit the transport-possible transition and sanitized proof in
            # one transaction. A death after this point must retain HOLD.
            self.ledger.record_pending(operation_id, digest, {
                "entitySet": entity_set, "targetFingerprint": self.target_fingerprint,
                "counterpartyUniqueness": uniqueness,
            })
        try:
            with diagnostic_stage("create"):
                if _prod_write_probe_stage is None:
                    created = self.client.create_entity(
                        entity_set, payload, null_ref_key=null_ref_key,
                    )
                else:
                    created = self.client.create_entity(
                        entity_set, payload, null_ref_key=null_ref_key,
                        _prod_write_probe_stage=_prod_write_probe_stage,
                    )
        except ODataAcceptedResponseError as exc:
            return self._accepted_without_readback(
                entity_set=entity_set, operation="create",
                operation_id=operation_id, key=exc.key, error=exc,
                payload=payload,
                reference_validation=reference_validation,
            )
        except ODataClientError as exc:
            return self._write_error_result(
                entity_set=entity_set, operation="create", payload=payload,
                key=None, operation_id=operation_id, precondition=None,
                error=exc, phase="write",
                reference_validation=reference_validation,
            )
        result: dict[str, Any] = {"created": created, "readbackFetched": False,
                                  "readbackVerified": False, "entitySet": entity_set}
        if reference_validation is not None:
            result["referenceValidation"] = reference_validation
        if isinstance(created, dict) and created.get("Ref_Key"):
            try:
                with diagnostic_stage("readback"):
                    result["readback"], result["readbackStrategy"] = (
                        self._get_entity_with_keyed_404_fallback(
                            entity_set, created["Ref_Key"],
                        )
                    )
                result["readbackFetched"] = True
                # 1C requires a null GUID in Ref_Key when creating an object.
                # That sentinel is not a business value to compare with the
                # generated key; the verifier checks the returned key itself.
                verification_payload = {
                    field: value for field, value in payload.items()
                    if field != "Ref_Key"
                }
                verification = _readback_verification(
                    description, created["Ref_Key"], verification_payload,
                    result["readback"],
                )
                result["readbackVerified"] = verification["status"] == "verified"
                result["readbackVerification"] = verification
            except ODataClientError as exc:
                return self._accepted_without_readback(
                    entity_set=entity_set, operation="create",
                    operation_id=operation_id, key=created["Ref_Key"], error=exc,
                    reference_validation=reference_validation,
                )
        else:
            result["writeState"] = "accepted_unverified"
        receipt = {
            "entitySet": entity_set,
            "key": created.get("Ref_Key") if isinstance(created, dict) else None,
            "dataVersion": (result.get("readback", {}).get("DataVersion")
                            if isinstance(result.get("readback"), dict) else None),
            "writeState": result.get("writeState", "accepted"),
            "readbackFetched": result["readbackFetched"],
            "readbackVerified": result["readbackVerified"],
            "createdResponseDigest": request_digest(created),
        }
        if result.get("readbackStrategy"):
            receipt["readbackStrategy"] = result["readbackStrategy"]
        if result.get("readbackVerification"):
            receipt["readbackVerification"] = result["readbackVerification"]
        if reference_validation is not None:
            receipt["referenceValidation"] = reference_validation
        self.ledger.complete(operation_id, receipt)
        return result

    def patch_entity(self, entity_set: str, key: str, payload: dict,
                     dry_run: bool = True, confirm: bool = False,
                     expected_target: str | None = None,
                     operation_id: str | None = None,
                     expected_data_version: str | None = None,
                     _allow_tabular_replace: bool = False,
                     _prod_write_probe_stage: str | None = None) -> dict:
        key = _normalized_key(key)
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        sections = set(description.get("tabularSections", []))
        if sections.intersection(payload) and not _allow_tabular_replace:
            raise ValueError("tabular sections require replace_tabular_section with all rows")
        if not dry_run and not expected_data_version:
            raise ValueError("expected_data_version is required for an applied PATCH")
        preflight_person_names(entity_set, payload)
        payload = self._validate_and_normalize_payload(entity_set, payload)
        preview = self._prepare_write(entity_set=entity_set, operation="patch",
                                      payload=payload, key=key, dry_run=dry_run,
                                      confirm=confirm, expected_target=expected_target,
                                      operation_id=operation_id,
                                      precondition=expected_data_version,
                                      prod_write_probe_stage=_prod_write_probe_stage)
        if preview is not None:
            return preview
        reference_validation, reference_failure = self._preflight_payload_references(
            entity_set=entity_set,
            operation="patch",
            payload=payload,
            key=key,
            operation_id=operation_id,
            precondition=expected_data_version,
        )
        if reference_failure is not None:
            return reference_failure
        if reference_validation.get("status") == "not_applicable":
            reference_validation = None
        try:
            if _prod_write_probe_stage is None:
                self.client.patch_entity(
                    entity_set, key, payload, if_match=expected_data_version,
                )
            else:
                self.client.patch_entity(
                    entity_set, key, payload, if_match=expected_data_version,
                    _prod_write_probe_stage=_prod_write_probe_stage,
                )
        except ODataAcceptedResponseError as exc:
            return self._accepted_without_readback(
                entity_set=entity_set, operation="patch",
                operation_id=operation_id, key=key, error=exc,
                reference_validation=reference_validation,
            )
        except ODataClientError as exc:
            return self._write_error_result(
                entity_set=entity_set, operation="patch", payload=payload,
                key=key, operation_id=operation_id,
                precondition=expected_data_version, error=exc, phase="write",
                reference_validation=reference_validation,
            )
        try:
            readback, readback_strategy = self._get_entity_with_keyed_404_fallback(
                entity_set, key,
            )
        except ODataClientError as exc:
            return self._accepted_without_readback(
                entity_set=entity_set, operation="patch",
                operation_id=operation_id, key=key, error=exc,
                reference_validation=reference_validation,
            )
        verification = _readback_verification(description, key, payload, readback)
        result = {"entitySet": entity_set, "key": key,
                  "readback": readback, "readbackFetched": True,
                  "readbackVerified": verification["status"] == "verified",
                  "readbackVerification": verification,
                  "readbackStrategy": readback_strategy}
        receipt = {"entitySet": entity_set, "key": key,
                   "readbackFetched": True,
                   "readbackVerified": verification["status"] == "verified",
                   "readbackVerification": verification,
                   "readbackStrategy": readback_strategy}
        if reference_validation is not None:
            result["referenceValidation"] = reference_validation
            receipt["referenceValidation"] = reference_validation
        self.ledger.complete(operation_id, receipt)
        return result

    def update_entity(self, entity_set: str, key: str, payload: dict,
                      dry_run: bool = True, confirm: bool = False,
                      expected_target: str | None = None,
                      operation_id: str | None = None,
                      expected_data_version: str | None = None) -> dict:
        return self.patch_entity(entity_set, key, payload, dry_run, confirm,
                                 expected_target, operation_id, expected_data_version)

    def replace_tabular_section(self, entity_set: str, key: str,
                                section: str, rows: list[dict],
                                expected_row_count: int,
                                expected_data_version: str | None = None,
                                dry_run: bool = True, confirm: bool = False,
                                expected_target: str | None = None,
                                operation_id: str | None = None) -> dict:
        key = _normalized_key(key)
        description = self.metadata().describe_entity_set(entity_set)
        if section not in description.get("tabularSections", []):
            raise ValueError("section is not a published tabular section of this entity")
        if not isinstance(section, str) or not section:
            raise ValueError("section is required")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows must be a list of JSON objects")
        if type(expected_row_count) is not int or expected_row_count < 0:
            raise ValueError("expected_row_count must be nonnegative")
        current, _readback_strategy = self._get_entity_with_keyed_404_fallback(
            entity_set, key,
        )
        current_rows = current.get(section) if isinstance(current, dict) else None
        if isinstance(current_rows, dict):
            current_rows = current_rows.get("results", current_rows.get("value"))
        if not isinstance(current_rows, list):
            raise ValueError("current tabular rows were not returned by OData")
        if any(not isinstance(row, dict) for row in current_rows):
            raise ValueError("current tabular rows must be JSON objects")
        if len(current_rows) != expected_row_count:
            raise ValueError("tabular row count changed; reread the document")
        normalized_payload = self._validate_and_normalize_payload(entity_set, {section: rows})
        normalized_rows = normalized_payload.get(section)
        if not isinstance(normalized_rows, list) or any(
            not isinstance(row, dict) for row in normalized_rows
        ):
            raise ValueError("normalized tabular rows must be a list of JSON objects")
        row_validation = self._validate_tabular_replacement_rows(current_rows, normalized_rows)
        if not dry_run and current.get("DataVersion") != expected_data_version:
            raise ValueError("DataVersion changed; reread the document")
        result = self.patch_entity(entity_set, key, normalized_payload, dry_run,
                                   confirm, expected_target, operation_id,
                                   expected_data_version,
                                   _allow_tabular_replace=True)
        result["previousRowCount"] = len(current_rows)
        result["replacementRowCount"] = len(rows)
        result["rowValidation"] = row_validation
        return result

    def document_action(self, entity_set: str, key: str, action: str,
                        dry_run: bool = True, confirm: bool = False,
                        expected_target: str | None = None,
                        operation_id: str | None = None,
                        expected_data_version: str | None = None,
                        expected_accounting_movements: dict[str, Any] | None = None) -> dict:
        key = _normalized_key(key)
        if not entity_set.startswith("Document_") or action not in {"Post", "Unpost"}:
            raise ValueError("only Post or Unpost of a published Document_* is supported")
        movement_expectation = _normalize_movement_expectation(expected_accounting_movements)
        if movement_expectation is not None and action != "Post":
            raise ValueError("expected_accounting_movements is only supported for Post")
        description = self.metadata().describe_entity_set(entity_set)
        self._ensure_direct_write_allowed(entity_set, description)
        if not self.metadata().has_action(entity_set, action):
            raise ValueError(f"{action} is not published for this top-level document entity set")
        self._validate_movement_expectation_metadata(movement_expectation)
        if movement_expectation is not None:
            movement_expectation = {
                **movement_expectation,
                "recorderType": self._movement_recorder_type(description, movement_expectation),
            }
        request_precondition = self._document_action_request_precondition(
            expected_data_version,
            movement_expectation,
        )
        if dry_run:
            preview = self._prepare_write(entity_set=entity_set, operation=action,
                                          payload=None, key=key, dry_run=dry_run,
                                          confirm=confirm,
                                          expected_target=expected_target,
                                          operation_id=operation_id,
                                          precondition=expected_data_version,
                                          request_precondition=request_precondition)
            if preview is not None:
                if movement_expectation is not None:
                    preview["plan"]["accountingMovementVerification"] = {
                        "requested": True,
                        "register": movement_expectation["register"],
                        "expectedRows": movement_expectation["rows"],
                        "status": "planned",
                    }
                return preview
        replay = self._completed_write_receipt(entity_set=entity_set,
                                               operation=action,
                                               payload=None, key=key,
                                               confirm=confirm,
                                               expected_target=expected_target,
                                               operation_id=operation_id,
                                               precondition=expected_data_version,
                                               request_precondition=request_precondition)
        if replay is not None:
            return replay
        with diagnostic_stage(
            "post_precondition_read" if action == "Post" else "unpost_precondition_read"
        ):
            self._assert_current_data_version(entity_set, key, expected_data_version)
        reserved = self._prepare_write(entity_set=entity_set, operation=action,
                                       payload=None, key=key, dry_run=dry_run,
                                       confirm=confirm, expected_target=expected_target,
                                       operation_id=operation_id,
                                       precondition=expected_data_version,
                                       request_precondition=request_precondition)
        if reserved is not None:
            return reserved
        try:
            with diagnostic_stage(action.lower()):
                self.client.invoke_action(entity_set, key, action)
        except ODataAcceptedResponseError as exc:
            result = {
                "entitySet": entity_set,
                "key": key,
                "action": action,
                "expectedDataVersion": expected_data_version,
                "writeState": "accepted_unverified",
                "readbackFetched": False,
                "readbackVerified": False,
                "readbackError": str(exc),
            }
            if movement_expectation is not None:
                with diagnostic_stage("movement_verification") as trace:
                    verification = self._verify_accounting_movements(key, movement_expectation)
                    trace.observe_result(verification)
                result["accountingMovementVerification"] = verification
                result["accountingMovementsVerified"] = verification["verified"]
                if verification["verified"]:
                    result["ok"] = True
                    result["writeState"] = "accepted_verified"
                else:
                    result["ok"] = False
                    result["writeState"] = (
                        "accepted_movement_unavailable"
                        if verification["status"] == "unavailable"
                        else "accepted_movement_mismatch"
                    )
                    result["error"] = self._accounting_movement_error(
                        verification, result["writeState"],
                    )
            self.ledger.complete(operation_id, result)
            return result
        except ODataClientError as exc:
            return self._write_error_result(
                entity_set=entity_set, operation=action, payload=None,
                key=key, operation_id=operation_id,
                precondition=expected_data_version, error=exc, phase="write",
                request_precondition=request_precondition,
            )
        try:
            with diagnostic_stage(f"{action.lower()}_readback"):
                readback, readback_strategy = self._get_entity_with_keyed_404_fallback(
                    entity_set, key,
                )
            readback_fetched = True
            readback_error = None
        except ODataClientError as exc:
            readback = None
            readback_fetched = False
            readback_error = str(exc)
        result = {"entitySet": entity_set, "key": key, "action": action,
                  "expectedDataVersion": expected_data_version,
                  "readback": readback, "readbackFetched": readback_fetched,
                  "readbackVerified": False, "accountingMovementsVerified": False}
        if readback_fetched:
            result["readbackStrategy"] = readback_strategy
        if readback_error is not None:
            result["readbackError"] = readback_error
            result["writeState"] = "accepted_unverified"
        else:
            result["writeState"] = "accepted"
        if movement_expectation is not None:
            with diagnostic_stage("movement_verification") as trace:
                verification = self._verify_accounting_movements(key, movement_expectation)
                trace.observe_result(verification)
            result["accountingMovementVerification"] = verification
            result["accountingMovementsVerified"] = verification["verified"]
            if verification["verified"]:
                result["ok"] = True
                result["writeState"] = "accepted_verified"
            else:
                result["ok"] = False
                result["writeState"] = (
                    "accepted_movement_unavailable"
                    if verification["status"] == "unavailable"
                    else "accepted_movement_mismatch"
                )
                result["error"] = self._accounting_movement_error(
                    verification, result["writeState"],
                )
        self.ledger.complete(operation_id, result)
        return result

    def post_document(self, entity_set: str, key: str,
                      dry_run: bool = True, confirm: bool = False,
                      expected_target: str | None = None,
                      operation_id: str | None = None,
                      expected_data_version: str | None = None,
                      expected_accounting_movements: dict[str, Any] | None = None) -> dict:
        return self.document_action(entity_set, key, "Post", dry_run, confirm,
                                    expected_target, operation_id,
                                    expected_data_version,
                                    expected_accounting_movements)

    def unpost_document(self, entity_set: str, key: str,
                        dry_run: bool = True, confirm: bool = False,
                        expected_target: str | None = None,
                        operation_id: str | None = None,
                        expected_data_version: str | None = None) -> dict:
        return self.document_action(entity_set, key, "Unpost", dry_run, confirm,
                                    expected_target, operation_id,
                                    expected_data_version)


_ACTIVE_DATABASE: ContextVar[tuple[str, Bridge] | None] = ContextVar(
    "onec83_active_database", default=None
)


def _ledger_path_for_target(name: str, *, multi_target: bool) -> Path:
    configured = os.environ.get("ONEC83_LEDGER_PATH")
    base = Path(configured) if configured else (
        Path.home() / "Library" / "Application Support" / "onec-odata-mcp" / "ledger.sqlite3"
    )
    # Keep the historical test ledger at its established path. Every other
    # target gets a separate database so operation IDs cannot cross bases.
    if not multi_target or name == "test":
        return base
    suffix = base.suffix or ".sqlite3"
    return base.with_name(f"{base.stem}-{name}{suffix}")


def _configure_counterparty_journals(bridges: dict[str, Bridge]) -> None:
    base = _ledger_path_for_target("test", multi_target=True)
    configured = {bridge.ledger.path for bridge in bridges.values()}

    def historical_journals() -> tuple[WriteLedger, ...]:
        # Keep original paths and receipts. Unknown legacy pending history in
        # any retired alias conservatively holds Create rather than guessing
        # its target. Discovery failure propagates before any POST.
        suffix = base.suffix or ".sqlite3"
        paths = configured | {base}
        if base.parent.exists():
            paths.update(path for path in base.parent.iterdir()
                         if path.name.startswith(base.stem + "-")
                         and path.name.endswith(suffix))
        return tuple(WriteLedger(path) for path in sorted(paths))

    bindings = {}
    for bridge in bridges.values():
        canonical = counterparty_target_fingerprint(bridge.client.config.odata_base_url)
        bindings.update({bridge.target_fingerprint: canonical, canonical: canonical})
    for bridge in bridges.values():
        bridge._counterparty_target_bindings = bindings
        bridge._counterparty_coordination_ledger = WriteLedger(base)
        bridge._counterparty_ledgers = historical_journals


@lru_cache(maxsize=8)
def _bridges_for_targets(raw: str) -> dict[str, Bridge]:
    targets = targets_from_json(raw)
    bridges = {
        target.name: Bridge(
            ODataClient(target.config),
            ledger=WriteLedger(_ledger_path_for_target(target.name, multi_target=True)),
            target_name=target.name,
        )
        for target in targets
    }
    _configure_counterparty_journals(bridges)
    return bridges


@lru_cache(maxsize=1)
def _legacy_bridge() -> Bridge:
    bridge = Bridge(ODataClient(RuntimeConfig.from_env()))
    _configure_counterparty_journals({"test": bridge})
    return bridge


def _target_bridge(database: str | None) -> tuple[str, Bridge] | None:
    raw = os.environ.get("ONEC83_TARGETS_JSON")
    if raw is None:
        if database is not None:
            raise ConfigError("database selection requires ONEC83_TARGETS_JSON")
        return None
    ensure_secret_free()
    bridges = _bridges_for_targets(raw)
    if database is None:
        if len(bridges) != 1:
            raise ValueError(
                "database is required when multiple 1C bases are configured; "
                "call onec83_list_databases, then pass database='test' or database='prod'"
            )
        database = next(iter(bridges))
    if not isinstance(database, str) or not database:
        raise ValueError("database must be a configured target name")
    bridge = bridges.get(database)
    if bridge is None:
        raise ValueError(
            f"unknown database {database!r}; call onec83_list_databases for available names"
        )
    return database, bridge


@contextmanager
def _database_context(database: str | None):
    selected = _target_bridge(database)
    if selected is None:
        yield None
        return
    token = _ACTIVE_DATABASE.set(selected)
    try:
        yield selected[0]
    finally:
        _ACTIVE_DATABASE.reset(token)


def _bridge() -> Bridge:
    selected = _ACTIVE_DATABASE.get()
    if selected is not None:
        return selected[1]
    raw = os.environ.get("ONEC83_TARGETS_JSON")
    if raw is not None:
        selected = _target_bridge(None)
        assert selected is not None
        return selected[1]
    return _legacy_bridge()


mcp = MCPServer("1C 8.3 OData")


def _diagnostic_tool(*, route_database: bool = True):
    """Return known failures as visible structured results for MCP clients."""
    def register(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            database = kwargs.pop("database", None) if route_database else None
            selected_name = None
            with instrument_tool_call(fn.__name__, PERFORMANCE_LOGGER) as perf_call:
                try:
                    database_scope = _database_context(database) if route_database else nullcontext(None)
                    with database_scope as selected_name:
                        result = fn(*args, **kwargs)
                    if selected_name is not None and isinstance(result, dict):
                        result = {**result, "database": selected_name}
                    if perf_call is not None:
                        perf_call.observe_result(result)
                    return result
                except (ODataClientError, CredentialError, ConfigError,
                        UncertainWrite, ValueError, PermissionError) as exc:
                    kind = (
                        "odataHttp" if isinstance(exc, ODataHTTPError) else
                        "odataTransport" if isinstance(exc, ODataTransportError) else
                        "odataClient" if isinstance(exc, ODataClientError) else
                        "uncertainWrite" if isinstance(exc, UncertainWrite) else
                        "credential" if isinstance(exc, CredentialError) else
                        "configuration" if isinstance(exc, ConfigError) else
                        "validation" if isinstance(exc, ValueError) else
                        "permission" if isinstance(exc, PermissionError) else
                        "unknown"
                    )
                    error: dict[str, Any] = {"kind": kind, "message": str(exc)}
                    if isinstance(exc, ODataHTTPError):
                        error["httpStatus"] = exc.status_code
                        error["definitive"] = exc.definitive
                        if exc.code:
                            error["code"] = exc.code
                    elif isinstance(exc, ODataTransportError):
                        error["category"] = exc.category
                        error["attempts"] = exc.attempts
                        if exc.subreason:
                            error["subreason"] = exc.subreason
                    result = {"ok": False, "error": error}
                    if selected_name is not None:
                        result["database"] = selected_name
                    if perf_call is not None:
                        perf_call.observe_result(result)
                    return result
        if route_database:
            signature = inspect.signature(fn)
            parameters = list(signature.parameters.values())
            if "database" not in signature.parameters:
                database_parameter = inspect.Parameter(
                    "database", inspect.Parameter.KEYWORD_ONLY,
                    default=None, annotation=str | None,
                )
                insert_at = next(
                    (index for index, parameter in enumerate(parameters)
                     if parameter.kind is inspect.Parameter.VAR_KEYWORD),
                    len(parameters),
                )
                parameters.insert(insert_at, database_parameter)
                call.__signature__ = signature.replace(parameters=parameters)
            base_doc = fn.__doc__ or ""
            call.__doc__ = (
                base_doc + " Pass database='test' or database='prod' to select the base; "
                "when only one target is configured it may be omitted."
            ).strip()
        return mcp.tool()(call)
    return register


@_diagnostic_tool(route_database=False)
def onec83_list_databases() -> dict:
    """List configured database aliases and their access mode without connecting to 1C."""
    raw = os.environ.get("ONEC83_TARGETS_JSON")
    if raw is None:
        config = RuntimeConfig.from_env()
        name = config.target_kind or "default"
        targets = [RuntimeTarget(name=name, config=config)]
    else:
        ensure_secret_free()
        targets = targets_from_json(raw)
    return {
        "databases": [
            {
                "name": target.name,
                "kind": target.config.target_kind,
                "readOnly": not target.config.write_enabled,
                "writeEnabled": target.config.write_enabled,
                "prodWriteProbeEnabled": target.config.prod_write_probe_enabled,
            }
            for target in targets
        ],
        "selectionRequired": len(targets) > 1,
    }


@_diagnostic_tool()
def onec83_connection_status() -> dict:
    """Authenticate and inspect the configured OData target. Use targetFingerprint as expected_target for writes; writeReadiness explains required write env without exposing URLs or secrets."""
    return _bridge().status()


@_diagnostic_tool()
def onec83_list_entity_sets(prefix: str | None = None) -> dict:
    """List published 1C entity sets, optionally by prefix such as Document_ or Catalog_."""
    return _bridge().list_entity_sets(prefix)


@_diagnostic_tool()
def onec83_list_document_types() -> dict:
    """List top-level published Document_* sets, including non-postable documents."""
    return _bridge().list_document_types()


@_diagnostic_tool()
def onec83_describe_entity_set(entity_set: str) -> dict:
    """Read fields, keys, table parts, and advertised operations of a published entity set."""
    return _bridge().describe_entity_set(entity_set)


@_diagnostic_tool()
def onec83_refresh_metadata() -> dict:
    """Reload current published OData schema after a configuration or rights change."""
    return _bridge().refresh_metadata()


@_diagnostic_tool()
def onec83_resolve_enum_value(enum_name: str, member_name: str,
                              field: str | None = None,
                              account_context: str | None = None) -> dict:
    """Build an EnumRef payload fragment without writing. Owner-observed account_context mappings are TEST-only and apply only to their documented enum/member/field context."""
    return _bridge().resolve_enum_value(enum_name, member_name, field, account_context)


@_diagnostic_tool()
def onec83_query_entities(entity_set: str, top: int = 20, skip: int = 0,
                          filter: str | None = None, select: str | None = None,
                          orderby: str | None = None, expand: str | None = None) -> dict:
    """Read a bounded OData page; pages from a changing 1C base are not a consistent snapshot. If 1C rejects a Date or tax-id filter in WHERE, use onec83_search_entities."""
    return _bridge().query_entities(entity_set, top, skip, filter, select, orderby, expand)


@_diagnostic_tool()
def onec83_list_entities(entity_set: str, top: int = 20, skip: int = 0,
                         filter: str | None = None, select: str | None = None,
                         orderby: str | None = None, expand: str | None = None) -> dict:
    """Alias for bounded entity listing; use describe_entity_set first to choose fields."""
    return _bridge().list_entities(entity_set, top, skip, filter, select, orderby, expand)


@_diagnostic_tool()
def onec83_search_entities(entity_set: str, field: str, top: int = 20,
                           equals: str | None = None,
                           from_value: str | None = None,
                           to_exclusive: str | None = None,
                           select: str | None = None,
                           max_scan: int = 1000,
                           page_size: int = 100,
                           resume_skip: int | None = None) -> dict:
    """Search one scalar Edm.String equality or Edm.DateTime range. It first tries server $filter; if 1C rejects that field in WHERE, it falls back to orderby plus bounded scan and reports completeness. If a fallback transport failure returns resumeSkip, repeat the same search with resume_skip set to that value and merge rows; never infer absence until complete=true."""
    return _bridge().search_entities(
        entity_set=entity_set,
        field=field,
        top=top,
        equals=equals,
        from_value=from_value,
        to_exclusive=to_exclusive,
        select=select,
        max_scan=max_scan,
        page_size=page_size,
        resume_skip=resume_skip,
    )


@_diagnostic_tool()
def onec83_search_entities_batch(entity_set: str, field: str,
                                 equals_values: list[str], top: int = 20,
                                 select: str | None = None,
                                 max_scan: int = 1000,
                                 page_size: int = 100,
                                 resume_skip: int | None = None) -> dict:
    """Search up to 50 exact values for one scalar field (including an empty Edm.String). On a known 1C WHERE rejection, reuse one bounded projected scan for all values. A resumed fallback from a nonzero skip returns candidates only because previous per-value match counts are not retained; complete remains false. Pages are not a stable snapshot."""
    return _bridge().search_entities_batch(
        entity_set=entity_set,
        field=field,
        equals_values=equals_values,
        top=top,
        select=select,
        max_scan=max_scan,
        page_size=page_size,
        resume_skip=resume_skip,
    )


@_diagnostic_tool()
def onec83_get_entity(entity_set: str, key: str) -> dict:
    """Read one published 1C object by its GUID key."""
    return _bridge().get_entity(entity_set, key)


@_diagnostic_tool()
def onec83_get_accounting_register_record_set(
    entity_set: str,
    recorder_key: str,
    recorder_entity_set: str | None = None,
    recorder_type: str | None = None,
) -> dict:
    """Read the complete AccountingRegister_* record set by recorder key using 1C's bounded named-key endpoint. Supply recorder_entity_set to derive Recorder_Type from metadata, or recorder_type explicitly."""
    return _bridge().get_accounting_register_record_set(
        entity_set=entity_set,
        recorder_key=recorder_key,
        recorder_entity_set=recorder_entity_set,
        recorder_type=recorder_type,
    )


@_diagnostic_tool()
def onec83_count_entities(entity_set: str) -> dict:
    """Count a published entity set; count is not a consistent snapshot of later pages."""
    return _bridge().count_entities(entity_set)


@_diagnostic_tool()
def onec83_get_write_receipt(operation_id: str) -> dict:
    """Inspect a local write receipt by stable operation_id. This sends no OData request."""
    return _bridge().get_write_receipt(operation_id)


@_diagnostic_tool()
def onec83_reconcile_pending_create(operation_id: str, entity_set: str,
                                    payload: dict, filter: str,
                                    expected_target: str, dry_run: bool = True,
                                    confirm: bool = False,
                                    expected_key: str | None = None) -> dict:
    """Reconcile a pending create only after a unique matching object is read back. Supply the original payload and identifying filter; inspect dry_run first. To finalize: dry_run=false, confirm=true, expected_key and expected_target. No 1C write is sent."""
    return _bridge().reconcile_pending_create(
        operation_id=operation_id, entity_set=entity_set, payload=payload,
        filter=filter, expected_target=expected_target,
        dry_run=dry_run, confirm=confirm, expected_key=expected_key,
    )


@_diagnostic_tool()
def onec83_reconcile_pending_post(operation_id: str, entity_set: str, key: str,
                                  expected_data_version: str,
                                  expected_target: str,
                                  expected_accounting_movements: dict[str, Any] | None = None,
                                  accounting_register: str | None = None,
                                  dry_run: bool = True, confirm: bool = False,
                                  max_scan: int = 1000, page_size: int = 100) -> dict:
    """Inspect a saved HTTP 5xx Post against the current document and a complete bounded register scan. Finalization updates only the local receipt; it never resends Post. Inspect dry_run first, then supply confirm=true and exact target."""
    return _bridge().reconcile_pending_post(
        operation_id=operation_id, entity_set=entity_set, key=key,
        expected_data_version=expected_data_version, expected_target=expected_target,
        expected_accounting_movements=expected_accounting_movements,
        accounting_register=accounting_register, dry_run=dry_run, confirm=confirm,
        max_scan=max_scan, page_size=page_size,
    )


@_diagnostic_tool()
def onec83_apply_document_queue(items: list[dict], dry_run: bool = True,
                                confirm: bool = False,
                                expected_target: str | None = None,
                                max_concurrency: int = 1) -> dict:
    """Preflight up to 50 independent document creates and optional posts. Applied creates check CSDL-declared scalar references before sending POST; missing or unverifiable targets fail that row without sending the write. Apply with dry_run=false, confirm=true, exact expected_target, and stable unique create/post operation ids. Concurrency is bounded to 1..4; resubmit unchanged rows to resume and reconcile pending receipts before retrying."""
    return _bridge().apply_document_queue(
        items, dry_run=dry_run, confirm=confirm,
        expected_target=expected_target, max_concurrency=max_concurrency,
    )


@_diagnostic_tool()
def onec83_run_prod_write_probe(dry_run: bool = True, confirm: bool = False,
                                expected_target: str | None = None,
                                dry_run_receipt: str | None = None) -> dict:
    """Run one explicitly configured PROD smoke test: create one marked Catalog_Организации record, update its Description, verify readback, then set DeletionMark=true. Applying requires the one-use dry_run_receipt from this MCP server. The tool never enables ordinary PROD writes, creates documents, or posts them."""
    return _bridge().run_prod_write_probe(
        dry_run=dry_run,
        confirm=confirm,
        expected_target=expected_target,
        dry_run_receipt=dry_run_receipt,
    )


@_diagnostic_tool()
def onec83_create_entity(entity_set: str, payload: dict, dry_run: bool = True,
                         confirm: bool = False, expected_target: str | None = None,
                         operation_id: str | None = None) -> dict:
    """Preview or create a published object.

    Applied writes check CSDL-declared scalar GUID links in the header and
    published tabular rows before POST. Missing or unknown references fail
    before writing; inspect referenceValidation and use a new operation_id
    after correction. Dry-run does not query reference targets.

    To apply, use dry_run=false, confirm=true, expected_target from status, and
    a stable operation_id. After keyed readback, readbackVerification reports
    field names and safe reason codes. A true readbackVerified requires all
    supported submitted scalar fields and Ref_Key to match; false alone does
    not show Create failed or authorize retry.
    """
    return _bridge().create_entity(entity_set, payload, dry_run, confirm,
                                   expected_target, operation_id)


@_diagnostic_tool()
def onec83_patch_entity(entity_set: str, key: str, payload: dict,
                        dry_run: bool = True, confirm: bool = False,
                        expected_target: str | None = None,
                        operation_id: str | None = None,
                        expected_data_version: str | None = None) -> dict:
    """Preview or PATCH an object. Applied writes batch-check CSDL-declared scalar GUID links in changed fields and tabular rows before PATCH; missing or unknown references fail without sending the write. To apply: dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version from a fresh read for If-Match."""
    return _bridge().patch_entity(entity_set, key, payload, dry_run, confirm,
                                  expected_target, operation_id,
                                  expected_data_version)


@_diagnostic_tool()
def onec83_update_entity(entity_set: str, key: str, payload: dict,
                         dry_run: bool = True, confirm: bool = False,
                         expected_target: str | None = None,
                         operation_id: str | None = None,
                         expected_data_version: str | None = None) -> dict:
    """Preview or update an object. Alias for PATCH and applies the same pre-write CSDL reference checks; missing or unknown references block the write. To apply use dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version."""
    return _bridge().update_entity(entity_set, key, payload, dry_run, confirm,
                                   expected_target, operation_id,
                                   expected_data_version)


@_diagnostic_tool()
def onec83_replace_tabular_section(entity_set: str, key: str, section: str,
                                    rows: list[dict], expected_row_count: int,
                                    expected_data_version: str | None = None,
                                    dry_run: bool = True, confirm: bool = False,
                                    expected_target: str | None = None,
                                    operation_id: str | None = None) -> dict:
    """Replace a full tabular section on its parent object. To apply: reread rows, pass expected_row_count and expected_data_version, then dry_run=false, confirm=true, expected_target from status, operation_id."""
    return _bridge().replace_tabular_section(
        entity_set, key, section, rows, expected_row_count,
        expected_data_version, dry_run, confirm, expected_target, operation_id,
    )


@_diagnostic_tool()
def onec83_set_deletion_mark(entity_set: str, key: str, mark: bool,
                             expected_data_version: str | None = None,
                             dry_run: bool = True, confirm: bool = False,
                             expected_target: str | None = None,
                             operation_id: str | None = None) -> dict:
    """Set 1C DeletionMark via PATCH. To apply: dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version; this does not physically delete."""
    return _bridge().patch_entity(
        entity_set, key, {"DeletionMark": mark}, dry_run, confirm,
        expected_target, operation_id, expected_data_version,
    )


@_diagnostic_tool()
def onec83_document_action(entity_set: str, key: str, action: str,
                           dry_run: bool = True, confirm: bool = False,
                           expected_target: str | None = None,
                           operation_id: str | None = None,
                           expected_data_version: str | None = None,
                           expected_accounting_movements: dict[str, Any] | None = None) -> dict:
    """Preview or run Post/Unpost. To apply: dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version; version check is pre-action only, not atomic. For Post, optional expected_accounting_movements verifies AccountingRegister_* rows by amount and debit/credit."""
    return _bridge().document_action(entity_set, key, action, dry_run, confirm,
                                     expected_target, operation_id,
                                     expected_data_version,
                                     expected_accounting_movements)


@_diagnostic_tool()
def onec83_post_document(entity_set: str, key: str,
                         dry_run: bool = True, confirm: bool = False,
                         expected_target: str | None = None,
                         operation_id: str | None = None,
                         expected_data_version: str | None = None,
                         expected_accounting_movements: dict[str, Any] | None = None) -> dict:
    """Preview or post a Document_*. To apply: dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version; version check is pre-action only, not atomic. Optional expected_accounting_movements verifies AccountingRegister_* rows by amount and debit/credit; Posted=true alone is not accounting verification."""
    return _bridge().post_document(entity_set, key, dry_run, confirm,
                                   expected_target, operation_id,
                                   expected_data_version,
                                   expected_accounting_movements)


@_diagnostic_tool()
def onec83_unpost_document(entity_set: str, key: str,
                           dry_run: bool = True, confirm: bool = False,
                           expected_target: str | None = None,
                           operation_id: str | None = None,
                           expected_data_version: str | None = None) -> dict:
    """Preview or unpost a Document_*. To apply: dry_run=false, confirm=true, expected_target from status, operation_id, and expected_data_version; version check is pre-action only, not atomic."""
    return _bridge().unpost_document(entity_set, key, dry_run, confirm,
                                     expected_target, operation_id,
                                     expected_data_version)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local 1C 8.3 OData MCP server.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bearer-token-file")
    parser.add_argument("--bank-graph-binding", help="bank-graph-binding.json from a single committed source package")
    parser.add_argument("--bank-graph-revision", help="exact accepted source revision; required with binding")
    parser.add_argument("--bank-graph-binding-sha256", help="binding digest from separately retained trusted export receipt")
    parser.add_argument("--check-bank-graph", action="store_true", help="validate selected package offline and exit without starting any transport")
    args = parser.parse_args()
    if any((args.bank_graph_binding, args.bank_graph_revision, args.bank_graph_binding_sha256)) and not all((args.bank_graph_binding, args.bank_graph_revision, args.bank_graph_binding_sha256)):
        parser.error("--bank-graph-binding, --bank-graph-revision and --bank-graph-binding-sha256 must be selected together")
    if args.check_bank_graph and not args.bank_graph_binding:
        parser.error("--check-bank-graph requires an explicit bank graph package")
    if args.transport == "streamable-http" and not args.bearer_token_file:
        parser.error("--bearer-token-file is required for streamable-http")
    if args.bank_graph_binding:
        from .bank_graph import BankGraphStartupError, register_bound_bank_graph
        try:
            # Trust admission precedes imports. Live discovery is registered
            # before listening; one background Reader publishes data atomically.
            # Offline check stays synchronous and never opens a transport.
            reader = register_bound_bank_graph(mcp, args.bank_graph_binding, args.bank_graph_revision,
                                               args.bank_graph_binding_sha256, background=not args.check_bank_graph)
        except BankGraphStartupError as exc:
            parser.error(str(exc))
        if args.check_bank_graph:
            print(json.dumps({"state": "ready", "revision": args.bank_graph_revision,
                              "snapshot": reader.snapshot, "graphs": len(reader.inputs),
                              "operations": len(reader.operations), "transport_started": False,
                              "odata_calls": 0}))
            return
    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
            return
        from onec_mcp_runtime.transport import run_streamable_http

        run_streamable_http(
            mcp,
            host=args.host,
            port=args.port,
            token_file=args.bearer_token_file,
        )
    finally:
        if args.bank_graph_binding:
            reader.close()



if __name__ == "__main__":
    main()
