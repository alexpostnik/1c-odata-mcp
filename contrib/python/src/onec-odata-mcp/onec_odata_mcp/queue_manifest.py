"""Offline compiler and resume guards for ``onec83_apply_document_queue``.

This module never connects to MCP, OData, or 1C. Its manifest contains the
prepared payloads and must be handled as business data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


_SCHEMA_VERSION = 1
_CATALOG_WORKFLOW = "catalog_prerequisites"
_HR_WORKFLOW = "hr_documents_create_only"
_CREATE_ONLY_WORKFLOWS = {_CATALOG_WORKFLOW, _HR_WORKFLOW}
_MAX_ITEMS = 50
_MAX_BYTES = 1_000_000
_MAX_CONCURRENCY = 4
_MAX_ENTITY_SET_LENGTH = 256
_ACCOUNT_IDENTIFIER_RE = re.compile(
    r"(?:[0-9]+(?:\.[0-9]+)*|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z"
)
_UNCERTAIN_STATES = {
    "needs_reconciliation", "pending", "accepted_unverified",
    "accepted_unverified_missing_key", "accepted_movement_unavailable",
    "accepted_movement_mismatch",
}


class QueueManifestError(ValueError):
    """A plan or queue result is unsafe or inconsistent with its manifest."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise QueueManifestError("manifest values must be finite JSON data") from None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _required_text(value: Any, label: str, *, maximum: int = 256) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise QueueManifestError(f"{label} must be printable ASCII text")
    return value


def _valid_document_entity_set(value: Any) -> bool:
    """Accept bounded Unicode identifiers while excluding URL/path syntax."""
    return (
        isinstance(value, str)
        and value.startswith("Document_")
        and len(value) <= _MAX_ENTITY_SET_LENGTH
        and len(value) > len("Document_")
        and value.isidentifier()
    )


def _valid_catalog_entity_set(value: Any) -> bool:
    """The explicit create-only workflow accepts bounded Catalog identifiers."""
    return (isinstance(value, str) and value.startswith("Catalog_")
            and len("Catalog_") < len(value) <= _MAX_ENTITY_SET_LENGTH
            and value.isidentifier())


def manifest_tool(manifest: Mapping[str, Any]) -> str:
    """Select the supported tool; a catalog plan cannot use the document tool."""
    value = validate_manifest(manifest)
    return ("onec83_create_entity" if value.get("workflow") in _CREATE_ONLY_WORKFLOWS
            else "onec83_apply_document_queue")


def _catalog_payload(payload: Any) -> None:
    # Keep this adapter deliberately scalar and pre-normalized. Structured enums,
    # tabular rows and implicit payload normalization need another reviewed contract.
    if (not isinstance(payload, Mapping) or not payload
            or any(not isinstance(key, str) or not key.isidentifier() for key in payload)
            or any(type(value) not in (str, int, float, bool, type(None))
                   for value in payload.values())
            or "Ref_Key" in payload or "Posted" in payload
            or ("DeletionMark" in payload and payload["DeletionMark"] is not False)):
        raise QueueManifestError("catalog payload needs pre-normalized scalar fields; no Ref_Key/Posted/deletion")
    _canonical_json(payload)


def _catalog_request(manifest: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
    row = manifest["rows"][0]
    return {"entity_set": row["entity_set"], "payload": _json_copy(row["payload"]),
            "operation_id": row["operation_id"], "dry_run": dry_run,
            **({"database": manifest["database"]} if "database" in manifest else {}),
            **({"confirm": True, "expected_target": manifest["target_fingerprint"]}
               if not dry_run else {})}


def _catalog_normalized_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**row["payload"], "Ref_Key": "00000000-0000-0000-0000-000000000000"}


def _catalog_payload_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _catalog_normalized_payload(row)
    kinds = {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}
    return {"fieldCount": len(payload), "fields": sorted(payload),
            "shape": {key: {"type": kinds[type(value)]} for key, value in payload.items()}}


def _check_catalog_receipt(manifest: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """Bind an actual get_write_receipt result to the normalized Create request."""
    from .ledger import request_digest
    row = manifest["rows"][0]
    payload = _catalog_normalized_payload(row)
    expected_hash = request_digest(manifest["target_fingerprint"], "create", row["entity_set"],
                                   None, payload, None)
    if (not isinstance(result, Mapping) or result.get("found") is not True
            or result.get("operationId") != row["operation_id"]
            or ("database" in manifest and result.get("database") != manifest["database"])
            or result.get("request_hash") != expected_hash
            or result.get("state") not in {"pending", "complete", "failed"}
            or not isinstance(result.get("result"), (Mapping, type(None)))):
        raise QueueManifestError("create-only result must be the exact saved get_write_receipt response")
    saved = result.get("result")
    if saved is not None and saved.get("entitySet") != row["entity_set"]:
        raise QueueManifestError("create-only receipt entity set differs from the plan")
    if result["state"] == "complete" and (not isinstance(saved, Mapping)
                                         or not isinstance(saved.get("key"), str)
                                         or not saved["key"]):
        raise QueueManifestError("completed create-only receipt needs a target key")


def _operation_id(batch_id: str, source_id: str, phase: str) -> str:
    raw = f"{batch_id}\0{source_id}\0{phase}".encode("utf-8")
    return "queue-" + hashlib.sha256(raw).hexdigest()[:32] + "-" + phase


def _validate_movement_expectation(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise QueueManifestError(f"{label} must be an object")
    register = value.get("register", value.get("entity_set", value.get("entitySet")))
    if not isinstance(register, str) or not register.startswith("AccountingRegister_"):
        raise QueueManifestError(f"{label}.register must name an AccountingRegister_*")
    rows = value.get("rows", value.get("expectations"))
    allowed = {
        "register", "entity_set", "entitySet", "rows", "expectations",
        "amount", "total", "expectedAmount", "debit", "debitAccount",
        "debitAccountValue", "credit", "creditAccount", "creditAccountValue",
        "tolerance", "activeValue", "allowExtraRows", "recorderType",
        "recorder_type", "amountField", "amountFields", "debitAccountField",
        "debitAccountFields", "creditAccountField", "creditAccountFields",
        "activeField", "activeFields",
    }
    if set(value) - allowed:
        raise QueueManifestError(f"{label} contains unsupported fields")
    if "debitAnyOf" in value:
        raise QueueManifestError(f"{label}.debitAnyOf must be inside a movement row")
    if rows is None:
        row_fields = {
            "amount", "total", "expectedAmount", "debit", "debitAccount",
            "debitAccountValue", "credit", "creditAccount", "creditAccountValue",
        }
        rows = [{key: value[key] for key in row_fields if key in value}]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise QueueManifestError(f"{label} must contain 1..20 movement rows")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise QueueManifestError(f"{label} row {index} must be an object")
        if set(row) - {
            "amount", "total", "expectedAmount", "debit", "debitAccount",
            "debitAccountValue", "debitAnyOf", "credit", "creditAccount", "creditAccountValue",
        }:
            raise QueueManifestError(f"{label} row {index} contains unsupported fields")
        has_debit = any(key in row for key in ("debit", "debitAccount", "debitAccountValue"))
        has_alternatives = "debitAnyOf" in row
        if has_debit and has_alternatives:
            raise QueueManifestError(f"{label} row {index} combines debit and debitAnyOf")
        if has_alternatives:
            alternatives = row["debitAnyOf"]
            if (not isinstance(alternatives, list) or not alternatives
                    or any(not isinstance(code, str) or not _ACCOUNT_IDENTIFIER_RE.fullmatch(code)
                           for code in alternatives)
                    or len({code.casefold() for code in alternatives}) != len(alternatives)):
                raise QueueManifestError(
                    f"{label} row {index}.debitAnyOf needs unique account codes or GUIDs"
                )
        amount = next((row.get(key) for key in ("amount", "total", "expectedAmount")
                       if row.get(key) is not None), None)
        debit = next((row.get(key) for key in ("debit", "debitAccount", "debitAccountValue")
                      if row.get(key) is not None), None)
        credit = next((row.get(key) for key in ("credit", "creditAccount", "creditAccountValue")
                       if row.get(key) is not None), None)
        if amount is None or (debit is None and not has_alternatives) or credit is None:
            raise QueueManifestError(f"{label} row {index} needs amount, debit or debitAnyOf, and credit")
    if "tolerance" in value:
        try:
            if float(value["tolerance"]) < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise QueueManifestError(f"{label}.tolerance must be a nonnegative number") from None
    if "allowExtraRows" in value and type(value["allowExtraRows"]) is not bool:
        raise QueueManifestError(f"{label}.allowExtraRows must be boolean")
    if any("debitAnyOf" in row for row in rows):
        if len(rows) != 1:
            raise QueueManifestError(f"{label}.debitAnyOf requires exactly one movement row")
        if value.get("allowExtraRows", False):
            raise QueueManifestError(f"{label}.debitAnyOf does not allow extra rows")
        if "tolerance" in value and float(value["tolerance"]) != 0:
            raise QueueManifestError(f"{label}.debitAnyOf requires exact amount")


def compile_manifest(
    rows: Sequence[Mapping[str, Any]],
    *,
    batch_id: str,
    target_fingerprint: str,
    template_id: str,
    template_version: str,
    max_concurrency: int = 1,
    workflow: str = "documents",
    database: str | None = None,
) -> dict[str, Any]:
    """Compile already-classified rows; this does not infer accounting rules."""
    if workflow not in {"documents", *_CREATE_ONLY_WORKFLOWS}:
        raise QueueManifestError("unsupported manifest workflow")
    catalog = workflow == _CATALOG_WORKFLOW
    create_only = workflow in _CREATE_ONLY_WORKFLOWS
    if database is not None:
        if not create_only:
            raise QueueManifestError("database binding belongs only to explicit create-only workflow")
        _required_text(database, "database")
    batch_id = _required_text(batch_id, "batch_id")
    target_fingerprint = _required_text(target_fingerprint, "target_fingerprint")
    template_id = _required_text(template_id, "template_id")
    template_version = _required_text(template_version, "template_version")
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= _MAX_CONCURRENCY:
        raise QueueManifestError("max_concurrency must be an integer from 1 to 4")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not 1 <= len(rows) <= _MAX_ITEMS:
        raise QueueManifestError("rows must contain 1..50 prepared documents")

    if create_only and (len(rows) != 1 or max_concurrency != 1):
        raise QueueManifestError("create-only claims require exactly one row and concurrency=1")

    seen: set[str] = set()
    compiled_rows: list[dict[str, Any]] = []
    allowed_source_fields = {
        "source_id", "entity_set", "payload", "post", "expected_accounting_movements",
    }
    for index, source in enumerate(rows):
        if not isinstance(source, Mapping):
            raise QueueManifestError(f"rows[{index}] must be an object")
        if set(source) - allowed_source_fields:
            raise QueueManifestError(f"rows[{index}] contains unsupported source fields")
        source_id = _required_text(source.get("source_id"), f"rows[{index}].source_id")
        if source_id in seen:
            raise QueueManifestError("source_id values must be unique within a batch")
        seen.add(source_id)
        entity_set = source.get("entity_set")
        validator = _valid_catalog_entity_set if catalog else _valid_document_entity_set
        if not validator(entity_set):
            prefix = "Catalog" if catalog else "Document"
            raise QueueManifestError(
                f"rows[{index}].entity_set must be a bounded {prefix}_* identifier"
            )
        if create_only and (source.get("post", False) is not False
                        or "expected_accounting_movements" in source):
            raise QueueManifestError("create-only rows forbid Post/movement fields")
        payload = source.get("payload")
        if not isinstance(payload, Mapping) or not payload:
            raise QueueManifestError(f"rows[{index}].payload must be a nonempty object")
        if create_only:
            _catalog_payload(payload)
        if catalog:
            from .person_mapping import preflight_person_names
            preflight_person_names(entity_set, payload)
        post = source.get("post", False)
        if type(post) is not bool:
            raise QueueManifestError(f"rows[{index}].post must be a boolean")
        expected_movements = source.get("expected_accounting_movements")
        if not post and expected_movements is not None:
            raise QueueManifestError(f"rows[{index}] movement expectations require post=true")
        _validate_movement_expectation(expected_movements,
                                       f"rows[{index}].expected_accounting_movements")
        item: dict[str, Any] = {
            "source_id": source_id,
            "entity_set": entity_set,
            "payload": _json_copy(payload),
            "operation_id": _operation_id(batch_id, source_id, "create"),
            "post": post,
        }
        if post:
            item["post_operation_id"] = _operation_id(batch_id, source_id, "post")
            if expected_movements is not None:
                if not isinstance(expected_movements, Mapping):
                    raise QueueManifestError(
                        f"rows[{index}].expected_accounting_movements must be an object"
                    )
                item["expected_accounting_movements"] = _json_copy(expected_movements)
        compiled_rows.append(item)

    # Validate serializability and the exact byte limit enforced by the MCP queue.
    queue_items = [_queue_item(row) for row in compiled_rows]
    if len(_canonical_json(queue_items).encode("utf-8")) > _MAX_BYTES:
        raise QueueManifestError("compiled queue items exceed the 1 MB queue limit")
    body = {
        "schema_version": _SCHEMA_VERSION,
        "batch_id": batch_id,
        "target_fingerprint": target_fingerprint,
        "template": {"id": template_id, "version": template_version},
        "max_concurrency": max_concurrency,
        "rows": compiled_rows,
    }
    if create_only:
        body["workflow"] = workflow
        if database is not None:
            body["database"] = database
    return {**body, "plan_sha256": _digest(body)}


def _queue_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "source_id"}


def _validate_manifest_history(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise QueueManifestError("manifest must be an object")
    value = dict(manifest)
    claimed = value.pop("plan_sha256", None)
    allowed_fields = {
        "schema_version", "batch_id", "target_fingerprint", "template",
        "max_concurrency", "rows",
    }
    catalog = value.get("workflow") == _CATALOG_WORKFLOW
    create_only = value.get("workflow") in _CREATE_ONLY_WORKFLOWS
    if create_only:
        allowed_fields.add("workflow")
        if "database" in value:
            _required_text(value["database"], "database")
            allowed_fields.add("database")
    if set(value) != allowed_fields:
        raise QueueManifestError("manifest contains unknown or missing top-level fields")
    if value.get("schema_version") != _SCHEMA_VERSION or claimed != _digest(value):
        raise QueueManifestError("manifest schema or plan digest is invalid")
    rows = value.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_ITEMS:
        raise QueueManifestError("manifest rows must contain 1..50 documents")
    batch_id = _required_text(value.get("batch_id"), "batch_id")
    _required_text(value.get("target_fingerprint"), "target_fingerprint")
    template = value.get("template")
    if not isinstance(template, Mapping) or set(template) != {"id", "version"}:
        raise QueueManifestError("manifest template must be an object")
    _required_text(template.get("id"), "template.id")
    _required_text(template.get("version"), "template.version")
    concurrency = value.get("max_concurrency")
    if type(concurrency) is not int or not 1 <= concurrency <= _MAX_CONCURRENCY:
        raise QueueManifestError("manifest max_concurrency must be an integer from 1 to 4")
    if create_only and (len(rows) != 1 or concurrency != 1):
        raise QueueManifestError("create-only claims require exactly one row and concurrency=1")
    source_ids: set[str] = set()
    operation_ids: set[str] = set()
    allowed_row_fields = {
        "source_id", "entity_set", "payload", "operation_id", "post",
        "post_operation_id", "expected_accounting_movements",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) - allowed_row_fields:
            raise QueueManifestError(f"manifest rows[{index}] has an invalid shape")
        source_id = _required_text(row.get("source_id"), f"rows[{index}].source_id")
        if source_id in source_ids:
            raise QueueManifestError("manifest source_id values must be unique")
        source_ids.add(source_id)
        if row.get("operation_id") != _operation_id(batch_id, source_id, "create"):
            raise QueueManifestError("manifest create operation ID is not deterministic")
        if row["operation_id"] in operation_ids:
            raise QueueManifestError("manifest operation IDs must be unique")
        operation_ids.add(row["operation_id"])
        entity_set = row.get("entity_set")
        validator = _valid_catalog_entity_set if catalog else _valid_document_entity_set
        if not validator(entity_set):
            raise QueueManifestError(f"manifest rows[{index}].entity_set is invalid")
        if create_only and (row.get("post") is not False or "post_operation_id" in row
                        or "expected_accounting_movements" in row):
            raise QueueManifestError("explicit manifest is create-only")
        if (not isinstance(row.get("payload"), Mapping) or not row.get("payload")
                or type(row.get("post")) is not bool):
            raise QueueManifestError(f"manifest rows[{index}] payload/post is invalid")
        if create_only:
            _catalog_payload(row.get("payload"))
        _validate_movement_expectation(
            row.get("expected_accounting_movements"),
            f"manifest rows[{index}].expected_accounting_movements",
        )
        post_id = row.get("post_operation_id")
        if row["post"]:
            if post_id != _operation_id(batch_id, source_id, "post") or post_id in operation_ids:
                raise QueueManifestError("manifest Post operation ID is not deterministic and unique")
            operation_ids.add(post_id)
        elif post_id is not None or row.get("expected_accounting_movements") is not None:
            raise QueueManifestError("manifest Post fields require post=true")
        elif "post_operation_id" in row or "expected_accounting_movements" in row:
            raise QueueManifestError("manifest must omit unused Post fields")
    items = [_queue_item(row) for row in rows]
    if len(_canonical_json(items).encode("utf-8")) > _MAX_BYTES:
        raise QueueManifestError("manifest queue items exceed the 1 MB queue limit")
    return {**value, "plan_sha256": claimed}


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a usable plan, including guards on saved/resealed Catalog rows."""
    value = _validate_manifest_history(manifest)
    if value.get("workflow") == _CATALOG_WORKFLOW:
        from .person_mapping import preflight_person_names
        for row in value["rows"]:
            preflight_person_names(row["entity_set"], row["payload"])
    return value


def _historical_preview_digest(manifest: Mapping[str, Any]) -> str:
    """Verify old checkpoint history only; never return dispatch arguments."""
    value = _validate_manifest_history(manifest)
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        arguments = _catalog_request(value, dry_run=True)
    else:
        arguments = {"items": [_queue_item(row) for row in value["rows"]],
                     "dry_run": True, "max_concurrency": value["max_concurrency"]}
    return _digest(arguments)


def _check_context(
    manifest: Mapping[str, Any], target_fingerprint: str,
    template_id: str, template_version: str,
) -> dict[str, Any]:
    value = validate_manifest(manifest)
    if target_fingerprint != value["target_fingerprint"]:
        raise QueueManifestError("target fingerprint changed; compile and review a new plan")
    if value["template"] != {"id": template_id, "version": template_version}:
        raise QueueManifestError("template changed; compile and review a new plan")
    return value


def dry_run_request(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_manifest(manifest)
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        return {"plan_sha256": value["plan_sha256"], "tool_name": manifest_tool(value),
                "tool_arguments": _catalog_request(value, dry_run=True)}
    return {
        "plan_sha256": value["plan_sha256"],
        "tool_arguments": {
            "items": [_queue_item(row) for row in value["rows"]],
            "dry_run": True,
            "max_concurrency": value["max_concurrency"],
        },
    }


def verify_dry_run(
    manifest: Mapping[str, Any], dry_run_result: Mapping[str, Any],
    submitted_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a successful preview to the exact locally compiled request."""
    value = validate_manifest(manifest)
    expected = dry_run_request(value)["tool_arguments"]
    if _canonical_json(submitted_request) != _canonical_json(expected):
        raise QueueManifestError("submitted dry-run arguments differ from the manifest")
    _check_dry_run(value, dry_run_result)
    return {
        "schema_version": _SCHEMA_VERSION,
        "plan_sha256": value["plan_sha256"],
        "target_fingerprint": value["target_fingerprint"],
        "template": value["template"],
        "dry_run_verified": True,
    }


def apply_request(
    manifest: Mapping[str, Any], preview_receipt: Mapping[str, Any], *,
    target_fingerprint: str, template_id: str, template_version: str,
) -> dict[str, Any]:
    value = _check_context(manifest, target_fingerprint, template_id, template_version)
    expected_preview = {
        "schema_version": _SCHEMA_VERSION,
        "plan_sha256": value["plan_sha256"],
        "target_fingerprint": value["target_fingerprint"],
        "template": value["template"],
        "dry_run_verified": True,
    }
    if _canonical_json(preview_receipt) != _canonical_json(expected_preview):
        raise QueueManifestError("apply requires a matching verified dry-run receipt")
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        return _catalog_request(value, dry_run=False)
    return {
        "items": [_queue_item(row) for row in value["rows"]],
        "dry_run": False,
        "confirm": True,
        "expected_target": value["target_fingerprint"],
        "max_concurrency": value["max_concurrency"],
    }


def resume_request(
    manifest: Mapping[str, Any], result_receipt: Mapping[str, Any], *,
    target_fingerprint: str, template_id: str, template_version: str,
) -> dict[str, Any]:
    """Prepare a fresh dry-run of the same plan after safe prior outcomes."""
    value = _check_context(manifest, target_fingerprint, template_id, template_version)
    expected_envelope = {
        "schema_version": _SCHEMA_VERSION,
        "plan_sha256": value["plan_sha256"],
        "target_fingerprint": value["target_fingerprint"],
        "template": value["template"],
    }
    if (not isinstance(result_receipt, Mapping)
            or any(result_receipt.get(key) != expected for key, expected in expected_envelope.items())
            or not isinstance(result_receipt.get("result"), Mapping)
            or result_receipt.get("result_sha256") != _digest(result_receipt.get("result"))):
        raise QueueManifestError("prior result receipt does not match this plan, target, and template")
    previous_result = result_receipt["result"]
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        _check_catalog_receipt(value, previous_result)
        if previous_result["state"] != "complete":
            raise QueueManifestError("create-only receipt is not complete; reconcile original operation, no new IDs")
        # This is a read-only preview; checkpoint apply remains dispatch_uncertain
        # until resolve, even if the server ledger already has a completed receipt.
        return dry_run_request(value)
    _check_apply_counters(value, previous_result)
    results = previous_result.get("results", [])
    if (previous_result.get("needsReconciliation", 0) or any(
            row.get("state") in _UNCERTAIN_STATES for row in results
    )):
        raise QueueManifestError(
            "uncertain rows require reconciliation; no new operation IDs may be generated"
        )
    return dry_run_request(value)


def record_result(
    manifest: Mapping[str, Any], result: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind returned queue rows to the immutable local plan for later resume."""
    return _record_historical_result(validate_manifest(manifest), result)


def _record_historical_result(manifest, result):
    """Receipt evidence for an immutable prior dispatch, never a new request."""
    value = _validate_manifest_history(manifest)
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        _check_catalog_receipt(value, result)
    else:
        _check_apply_counters(value, result)
    copied_result = _json_copy(result)
    return {
        "schema_version": _SCHEMA_VERSION,
        "plan_sha256": value["plan_sha256"],
        "target_fingerprint": value["target_fingerprint"],
        "template": value["template"],
        "result": copied_result,
        "result_sha256": _digest(copied_result),
    }


def summarize_result(
    manifest: Mapping[str, Any], result: Mapping[str, Any],
) -> dict[str, Any]:
    value = validate_manifest(manifest)
    if value.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        _check_catalog_receipt(value, result)
        return {"planSha256": value["plan_sha256"], "total": 1,
                "receiptState": result["state"], "verifiedRows": "unknown"}
    _check_apply_counters(value, result)
    rows = _check_result_rows(value, result)
    succeeded = sum(row.get("ok") is True for row in rows)
    uncertain = sum(row.get("state") in _UNCERTAIN_STATES for row in rows)
    failed = len(rows) - succeeded - uncertain
    return {
        "planSha256": value["plan_sha256"],
        "total": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "needsReconciliation": uncertain,
        "elapsedMs": result.get("elapsedMs"),
    }


def _check_dry_run(manifest: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if not isinstance(result, Mapping):
        raise QueueManifestError("dry-run response must be an object")
    if manifest.get("workflow") in _CREATE_ONLY_WORKFLOWS:
        row = manifest["rows"][0]
        plan = result.get("plan")
        if (result.get("dryRun") is not True or result.get("writeEnabled") is not True
                or ("database" in manifest and result.get("database") != manifest["database"])
                or not isinstance(plan, Mapping) or plan.get("operation") != "create"
                or plan.get("entitySet") != row["entity_set"] or plan.get("key") is not None
                or plan.get("targetFingerprint") != manifest["target_fingerprint"]
                or plan.get("wouldWrite") is not True
                or plan.get("payloadSummary") != _catalog_payload_summary(row)):
            raise QueueManifestError("create-only apply requires a successful exact-target Create dry-run")
        return
    if result.get("dryRun") is not True or result.get("total") != len(manifest["rows"]):
        raise QueueManifestError("apply requires a matching successful queue dry-run")
    if result.get("writeEnabled") is not True:
        raise QueueManifestError("queue write configuration is disabled")
    returned = result.get("results")
    if not isinstance(returned, list) or len(returned) != len(manifest["rows"]):
        raise QueueManifestError("dry-run result rows do not match the plan")
    for expected, actual in zip(manifest["rows"], returned):
        if not isinstance(actual, Mapping):
            raise QueueManifestError("dry-run result row must be an object")
        if (actual.get("operationId") != expected["operation_id"]
                or actual.get("entitySet") != expected["entity_set"]):
            raise QueueManifestError("dry-run result does not match the compiled plan")
        if expected.get("post"):
            post_plan = actual.get("postPlan")
            if not isinstance(post_plan, Mapping) or post_plan.get("operationId") != expected["post_operation_id"]:
                raise QueueManifestError("dry-run Post operation ID does not match the compiled plan")


def _check_result_rows(
    manifest: Mapping[str, Any], result: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    rows = result.get("results")
    if not isinstance(rows, list):
        raise QueueManifestError("queue result must contain per-row results")
    expected = manifest["rows"]
    if len(rows) != len(expected):
        raise QueueManifestError("queue result rows do not match the manifest")
    for planned, actual in zip(expected, rows):
        if not isinstance(actual, Mapping) or actual.get("operationId") != planned["operation_id"]:
            raise QueueManifestError("queue result operation IDs do not match the manifest")
        if type(actual.get("ok")) is not bool or not isinstance(actual.get("state"), str):
            raise QueueManifestError("queue result row is missing its outcome state")
        if planned.get("post") and actual.get("postOperationId") != planned["post_operation_id"]:
            raise QueueManifestError("queue result Post IDs do not match the manifest")
    return rows


def _check_apply_counters(manifest: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if result.get("dryRun") is not False:
        raise QueueManifestError("result receipt requires a completed apply response, not a preview")
    rows = _check_result_rows(manifest, result)
    succeeded = sum(row.get("ok") is True for row in rows)
    uncertain = sum(row.get("state") in _UNCERTAIN_STATES for row in rows)
    failed = len(rows) - succeeded - uncertain
    if (result.get("total") != len(rows) or result.get("succeeded") != succeeded
            or result.get("failed") != failed or result.get("needsReconciliation") != uncertain):
        raise QueueManifestError("apply result counters do not match its per-row outcomes")


def write_manifest(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> None:
    """Create a private local manifest; existing files are never overwritten."""
    value = validate_manifest(manifest)
    destination = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def read_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise QueueManifestError("manifest file could not be read as JSON") from None
    return validate_manifest(value)


def write_private_json(path: str | os.PathLike[str], value: Mapping[str, Any]) -> None:
    """Create a local JSON artifact with owner-only permissions and no overwrite."""
    content = _canonical_json(value) + "\n"
    destination = Path(path)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def read_private_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise QueueManifestError("local artifact could not be read as JSON") from None
    if not isinstance(value, dict):
        raise QueueManifestError("local artifact must contain a JSON object")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-fingerprint", required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--template-version", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile and validate local arguments for onec83_apply_document_queue; no network access."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    compile_parser = commands.add_parser("compile", help="compile prepared rows into a private manifest")
    compile_parser.add_argument("--input", required=True, help="private JSON file with {\"rows\": [...]} prepared rows")
    compile_parser.add_argument("--output", required=True, help="new manifest path outside the repository checkout")
    compile_parser.add_argument("--batch-id", required=True)
    compile_parser.add_argument("--target-fingerprint", required=True)
    compile_parser.add_argument("--template-id", required=True)
    compile_parser.add_argument("--template-version", required=True)
    compile_parser.add_argument("--max-concurrency", type=int, default=1)

    preview_parser = commands.add_parser("dry-run", help="print queue dry-run arguments")
    preview_parser.add_argument("--manifest", required=True)
    preview_parser.add_argument("--save-request", help="optional new private copy of the printed tool arguments")

    verify_parser = commands.add_parser("verify-dry-run", help="bind a returned preview to the manifest")
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--request", required=True, help="exact JSON arguments submitted to MCP")
    verify_parser.add_argument("--result", required=True, help="saved MCP dry-run response JSON")
    verify_parser.add_argument("--output", required=True, help="new private preview-receipt path")

    apply_parser = commands.add_parser("apply", help="print confirmed apply arguments after dry-run verification")
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--preview-receipt", required=True)
    _context_arguments(apply_parser)

    result_parser = commands.add_parser("record-result", help="save queue result privately and print its summary")
    result_parser.add_argument("--manifest", required=True)
    result_parser.add_argument("--result", required=True, help="saved MCP queue response JSON")
    result_parser.add_argument("--output", required=True, help="new private result-receipt path")

    summary_parser = commands.add_parser("summarize", help="print compact row outcome counts")
    summary_parser.add_argument("--manifest", required=True)
    summary_parser.add_argument("--result", required=True)

    resume_parser = commands.add_parser("resume", help="print same-ID dry-run arguments after safe prior results")
    resume_parser.add_argument("--manifest", required=True)
    resume_parser.add_argument("--result-receipt", required=True)
    _context_arguments(resume_parser)

    args = parser.parse_args(argv)
    try:
        if args.command == "compile":
            input_value = read_private_json(args.input)
            manifest = compile_manifest(
                input_value.get("rows"), batch_id=args.batch_id,
                target_fingerprint=args.target_fingerprint, template_id=args.template_id,
                template_version=args.template_version, max_concurrency=args.max_concurrency,
            )
            write_manifest(args.output, manifest)
            _print_json({"manifest": str(Path(args.output)), "planSha256": manifest["plan_sha256"],
                         "rows": len(manifest["rows"])})
        elif args.command == "dry-run":
            request = dry_run_request(read_manifest(args.manifest))["tool_arguments"]
            if args.save_request:
                write_private_json(args.save_request, request)
            _print_json(request)
        elif args.command == "verify-dry-run":
            manifest = read_manifest(args.manifest)
            request = read_private_json(args.request)
            response = read_private_json(args.result)
            receipt = verify_dry_run(manifest, response, request)
            write_private_json(args.output, receipt)
            _print_json({"previewReceipt": str(Path(args.output)), "planSha256": manifest["plan_sha256"],
                         "verified": True})
        elif args.command == "apply":
            _print_json(apply_request(
                read_manifest(args.manifest), read_private_json(args.preview_receipt),
                target_fingerprint=args.target_fingerprint, template_id=args.template_id,
                template_version=args.template_version,
            ))
        elif args.command in {"record-result", "summarize"}:
            manifest = read_manifest(args.manifest)
            result = read_private_json(args.result)
            summary = summarize_result(manifest, result)
            if args.command == "record-result":
                write_private_json(args.output, record_result(manifest, result))
                summary["resultReceipt"] = str(Path(args.output))
            _print_json(summary)
        elif args.command == "resume":
            _print_json(resume_request(
                read_manifest(args.manifest), read_private_json(args.result_receipt),
                target_fingerprint=args.target_fingerprint, template_id=args.template_id,
                template_version=args.template_version,
            )["tool_arguments"])
        return 0
    except (QueueManifestError, OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"queue manifest error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
