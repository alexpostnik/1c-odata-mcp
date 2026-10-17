"""Private offline index of verified source rows for the existing queue helper.

No network access. An import is evidence evaluation, not a 1C verification call.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from .person_mapping import preflight_person_names

from .queue_manifest import (
    QueueManifestError, _canonical_json, _digest, _required_text,
    _validate_movement_expectation, _valid_document_entity_set,
    _valid_catalog_entity_set, _CATALOG_WORKFLOW, _HR_WORKFLOW,
    _catalog_payload,
    read_private_json, write_private_json, record_result, compile_manifest,
    validate_manifest, _validate_manifest_history, _historical_preview_digest,
    _record_historical_result, dry_run_request, verify_dry_run, apply_request,
)


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SCHEMA = 4
_STATUSES = {"held", "missing_verified", "found_existing_verified", "newly_created_verified"}
_STICKY_CONFLICT_REASONS = {"target_conflict", "conflicting_evidence", "target_ambiguous"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _source(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    source = candidate.get("source")
    if not isinstance(source, Mapping) or set(source) != {
        "namespace", "date", "statement", "line"
    }:
        raise QueueManifestError("source needs namespace, date, statement, line")
    namespace = _required_text(source["namespace"], "source.namespace")
    date = source["date"]
    if not isinstance(date, str) or not _DATE.fullmatch(date):
        raise QueueManifestError("source.date must be canonical YYYY-MM-DD")
    from datetime import date as calendar_date
    try:
        calendar_date.fromisoformat(date)
    except ValueError:
        raise QueueManifestError("source.date is invalid") from None
    statement = _required_text(str(source["statement"]), "source.statement")
    line = _required_text(str(source["line"]), "source.line")
    if (statement.isdigit() and str(int(statement)) != statement) or (line.isdigit() and str(int(line)) != line):
        raise QueueManifestError("source statement and line must use canonical numbers")
    return namespace, date, statement, line


def _candidate(candidate: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    if _depth > 8:
        raise QueueManifestError("prerequisite nesting exceeds 8")
    catalog = isinstance(candidate, Mapping) and candidate.get("workflow") == _CATALOG_WORKFLOW
    hr = isinstance(candidate, Mapping) and candidate.get("workflow") == _HR_WORKFLOW
    fields = {"source", "target_fingerprint", "template", "prepared"}
    if isinstance(candidate, Mapping) and "prerequisites" in candidate:
        fields.add("prerequisites")
    if hr:
        fields.add("source_evidence")
        fields.add("prerequisites")
    if catalog or hr:
        fields.add("workflow")
        if "database" in candidate:
            _required_text(candidate["database"], "database")
            fields.add("database")
    if not isinstance(candidate, Mapping) or set(candidate) != fields:
        raise QueueManifestError("candidate needs source, target_fingerprint, template, prepared")
    _source(candidate)
    _required_text(candidate["target_fingerprint"], "target_fingerprint")
    template = candidate["template"]
    if not isinstance(template, Mapping) or set(template) != {"id", "version"}:
        raise QueueManifestError("template needs id and version")
    _required_text(template["id"], "template.id")
    _required_text(template["version"], "template.version")
    prepared = candidate["prepared"]
    row_fields = {"source_id", "entity_set", "payload", "post"}
    if not (catalog or hr):
        row_fields.add("expected_accounting_movements")
    if not isinstance(prepared, Mapping) or set(prepared) != row_fields:
        raise QueueManifestError("prepared row has invalid fields for its workflow")
    _required_text(prepared["source_id"], "prepared.source_id")
    validator = _valid_catalog_entity_set if catalog else _valid_document_entity_set
    if not validator(prepared["entity_set"]):
        raise QueueManifestError("prepared.entity_set is invalid")
    if (not isinstance(prepared["payload"], Mapping) or not prepared["payload"]
            or prepared["post"] is not (False if catalog or hr else True)):
        raise QueueManifestError("prepared row needs a nonempty payload and correct post mode")
    if catalog or hr:
        _catalog_payload(prepared["payload"])
    else:
        if prepared["expected_accounting_movements"] is None:
            raise QueueManifestError("prepared row needs movement expectation")
        _validate_movement_expectation(prepared["expected_accounting_movements"], "prepared movement")
    if "prerequisites" in candidate:
        _prerequisite_shape(candidate["prerequisites"], candidate, _depth=_depth)
    if hr:
        from .hr_source import validate_source
        validate_source(candidate)
    return json.loads(_canonical_json(candidate))


def _prerequisite_shape(prerequisites: Any, candidate: Mapping[str, Any], *, _depth: int = 0) -> None:
    if not isinstance(prerequisites, list) or not 1 <= len(prerequisites) <= 16:
        raise QueueManifestError("prerequisites need 1..16 exact Catalog bindings")
    fields = set()
    for binding in prerequisites:
        if not isinstance(binding, Mapping) or set(binding) != {"candidate", "payload_field", "target_key"}:
            raise QueueManifestError("prerequisite binding shape is invalid")
        previous = _candidate(binding["candidate"], _depth=_depth + 1)
        # OData reference fields can be Unicode; keep the shared ID contract ASCII.
        field = binding["payload_field"]
        if not isinstance(field, str) or not 1 <= len(field) <= 256 or not field.isidentifier():
            raise QueueManifestError("prerequisite.payload_field must be a bounded Unicode identifier")
        key = _required_text(binding["target_key"], "prerequisite.target_key")
        if (previous.get("workflow") != _CATALOG_WORKFLOW or len(key) > 256
                or previous["target_fingerprint"] != candidate["target_fingerprint"]
                or previous.get("database") != candidate.get("database")
                or not field.endswith("_Key") or field in fields
                or candidate["prepared"]["payload"].get(field) != key):
            raise QueueManifestError("prerequisite must bind exact Catalog target and payload reference")
        fields.add(field)


def _prerequisites(index: Mapping[str, Any], candidate: Mapping[str, Any], *, legacy: bool = False) -> None:
    # Pre-existing unqualified claims retain their original contract and IDs.
    # This does not authorize a sibling claim or attach invented dependencies.
    if candidate.get("workflow") == _CATALOG_WORKFLOW and not legacy:
        native_source = _source(candidate)
        siblings = [(entry, _entry_catalog(index, key)) for key, entry in index["entries"].items()
                    if _source(entry) == native_source
                    and entry["target_fingerprint"] == candidate["target_fingerprint"]
                    and _entry_catalog(index, key) not in {None, candidate["prepared"]["entity_set"]}]
        references = any(field.endswith("_Key") and value is not None
                         for field, value in candidate["prepared"]["payload"].items())
        native_binding = any(_source(binding["candidate"]) == native_source
                             for binding in candidate.get("prerequisites", []))
        if siblings and references and not native_binding:
            raise QueueManifestError("derived Catalog reference needs an exact native prerequisite")
        if references:
            for entry, entity_set in siblings:
                fields = {field for field, value in candidate["prepared"]["payload"].items()
                          if field.endswith("_Key") and value is not None and value == entry["target_key"]}
                bindings = [binding for binding in candidate.get("prerequisites", [])
                            if _source(binding["candidate"]) == native_source
                            and binding["candidate"]["prepared"]["entity_set"] == entity_set]
                if ((entry["status"] not in {"found_existing_verified", "newly_created_verified"}
                     and not bindings)
                        or not fields <= {binding["payload_field"] for binding in bindings}):
                    raise QueueManifestError("derived Catalog reference needs an exact native prerequisite")
    for binding in candidate.get("prerequisites", []):
        previous = binding["candidate"]
        key = _checkpoint_key(index, previous)
        entry = index["entries"].get(key)
        if (entry is None or entry["candidate_sha256"] != _digest(previous)
                or entry["target_key"] != binding["target_key"]
                or entry["status"] not in {"found_existing_verified", "newly_created_verified"}
                or any(claim["stage"] != "resolved" and
                       any(row["key"] == key for row in claim["rows"])
                       for claim in index["claims"].values())):
            raise QueueManifestError("prerequisite readback is not verified in this checkpoint")
        if candidate.get("workflow") == _HR_WORKFLOW:
            from .hr_source import validate_checkpoint_lineage
            validate_checkpoint_lineage(candidate, binding["payload_field"], entry)
        _prerequisites(index, previous, legacy=key == _legacy_key(previous))


def _legacy_key(candidate: Mapping[str, Any]) -> str:
    return _canonical_json([_source(candidate), candidate["target_fingerprint"]])


def _hr_native_source(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    namespace, _date, metadata, native = _source(candidate)
    return namespace, metadata, native


def _key(candidate: Mapping[str, Any]) -> str:
    if candidate.get("workflow") == _CATALOG_WORKFLOW:
        return _canonical_json([_source(candidate), candidate["target_fingerprint"],
                                _CATALOG_WORKFLOW, candidate["prepared"]["entity_set"]])
    if candidate.get("workflow") == _HR_WORKFLOW:
        return _canonical_json([_hr_native_source(candidate), candidate["target_fingerprint"],
                                _HR_WORKFLOW, candidate["prepared"]["entity_set"]])
    return _legacy_key(candidate)


def _entry_catalog(index: Mapping[str, Any], key: str) -> str | None:
    entry = index["entries"][key]
    if "catalog_entity_set" in entry:
        return entry["catalog_entity_set"]
    # A legacy entry has no entity discriminator. Only its original bound
    # manifest can identify it; do not infer from a new candidate or a GUID.
    entities = {
        claim["manifest"]["rows"][position]["entity_set"]
        for claim in index["claims"].values()
        if claim["manifest"].get("workflow") == _CATALOG_WORKFLOW
        for position, row in enumerate(claim["rows"])
        if row["key"] == key and row["candidate_sha256"] == entry["candidate_sha256"]
    }
    return next(iter(entities)) if len(entities) == 1 else None


def _checkpoint_key(index: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    key = _key(candidate)
    if candidate.get("workflow") == _HR_WORKFLOW:
        entity = candidate["prepared"]["entity_set"]
        matches = []
        for old_key, entry in index["entries"].items():
            if (entry["target_fingerprint"] != candidate["target_fingerprint"]
                    or _hr_native_source(entry) != _hr_native_source(candidate)
                    or _entry_catalog(index, old_key) is not None):
                continue
            old_entity = entry.get("hr_entity_set")
            if old_entity is None:
                entities = {claim["manifest"]["rows"][position]["entity_set"]
                            for claim in index["claims"].values()
                            if claim["manifest"].get("workflow") == _HR_WORKFLOW
                            for position, row in enumerate(claim["rows"])
                            if row["key"] == old_key and row["candidate_sha256"] == entry["candidate_sha256"]}
                if len(entities) == 1:
                    old_entity = next(iter(entities))
                elif not entities and entry["candidate_sha256"] == _digest(candidate):
                    old_entity = entity  # Exact old candidate, no identity backfill.
                else:
                    raise QueueManifestError("HR native Document legacy entity is unknown; reconcile original entry")
            if old_entity == entity:
                if entry["candidate_sha256"] != _digest(candidate):
                    raise QueueManifestError("HR native Document facts changed; reconcile original entry and operation")
                matches.append(old_key)
        if len(matches) > 1:
            raise QueueManifestError("HR native Document has conflicting entries; reconcile original operations")
        return matches[0] if matches else key
    if candidate.get("workflow") != _CATALOG_WORKFLOW:
        return key
    legacy = _legacy_key(candidate)
    entry = index["entries"].get(legacy)
    if entry is None:
        return key
    # Continue the exact old candidate and its operation IDs on the old key.
    if entry["candidate_sha256"] == _digest(candidate):
        if key in index["entries"]:
            raise QueueManifestError("legacy and derived Catalog bindings conflict")
        return legacy
    entity_set = _entry_catalog(index, legacy)
    if entity_set is None:
        raise QueueManifestError("legacy_source_binding_unknown")
    if entity_set == candidate["prepared"]["entity_set"]:
        if key in index["entries"]:
            raise QueueManifestError("legacy and derived Catalog bindings conflict")
        return legacy
    return key


def _catalog_fields(candidate: Mapping[str, Any], key: str) -> dict[str, str]:
    if candidate.get("workflow") == _HR_WORKFLOW and key == _key(candidate):
        return {"hr_entity_set": candidate["prepared"]["entity_set"]}
    return ({"catalog_entity_set": candidate["prepared"]["entity_set"]}
            if candidate.get("workflow") == _CATALOG_WORKFLOW and key == _key(candidate)
            else {})


def _target_conflicts(index: Mapping[str, Any], candidate: Mapping[str, Any],
                      key: str, target_key: str) -> list[str]:
    catalog = candidate.get("workflow") == _CATALOG_WORKFLOW
    return [other_key for other_key, other in index["entries"].items()
            if other_key != key and other["target_key"] == target_key
            and other["target_fingerprint"] == candidate["target_fingerprint"]
            and (not catalog or _entry_catalog(index, other_key) is None
                 or _entry_catalog(index, other_key) == candidate["prepared"]["entity_set"])]


def _matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _matches(a, b) for a, b in zip(expected, actual)
        )
    return type(expected) is type(actual) and expected == actual


def _movement_matches(expected: Mapping[str, Any], actual: Any, target_key: str,
                      entity_set: str) -> bool:
    # Require an exact, fully observed active set. Do not trust a boolean summary.
    register = expected.get("register", expected.get("entity_set", expected.get("entitySet")))
    recorder_type = expected.get("recorderType", expected.get("recorder_type", entity_set))
    if (not isinstance(actual, Mapping) or actual.get("register") != register
            or actual.get("target_key") != target_key
            or actual.get("recorder_key") != target_key
            or actual.get("recorder_type") != recorder_type
            or actual.get("complete") is not True
            or actual.get("result_limited") is not False
            or actual.get("truncated") is not False
            or actual.get("max_scan_reached", False) is not False):
        return False
    rows = expected.get("rows", expected.get("expectations"))
    if rows is None:
        rows = [{key: expected[key] for key in (
            "amount", "total", "expectedAmount", "debit", "debitAccount",
            "debitAccountValue", "credit", "creditAccount", "creditAccountValue"
        ) if key in expected}]
    if not isinstance(rows, list) or expected.get("allowExtraRows", False) or float(expected.get("tolerance", 0)) != 0:
        return False
    observed = actual.get("rows")
    if (not isinstance(observed, list) or len(observed) != len(rows)
            or type(actual.get("total_count")) is not int
            or actual["total_count"] != len(observed)
            or any(not isinstance(row, Mapping)
                   or row.get("recorder_key") != target_key
                   or row.get("recorder_type") != recorder_type for row in observed)):
        return False
    remaining = list(observed)
    for wanted in rows:
        amount = next((wanted[k] for k in ("amount", "total", "expectedAmount") if k in wanted), None)
        debit = next((wanted[k] for k in ("debit", "debitAccount", "debitAccountValue") if k in wanted), None)
        credit = next((wanted[k] for k in ("credit", "creditAccount", "creditAccountValue") if k in wanted), None)
        possible_debits = wanted.get("debitAnyOf", [debit])
        from decimal import Decimal, InvalidOperation
        def same_amount(value: Any) -> bool:
            if isinstance(value, bool):
                return False
            try:
                return Decimal(str(value)) == Decimal(str(amount))
            except (InvalidOperation, ValueError):
                return False
        matched = [row for row in remaining if isinstance(row, Mapping)
                   and row.get("active") is True
                   and same_amount(row.get("amount"))
                   and row.get("debit") in possible_debits
                   and row.get("credit") == credit]
        if len(matched) != 1:
            return False
        remaining.remove(matched[0])
    return not remaining


def _evaluate(candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[str, str, str | None]:
    if not isinstance(evidence, Mapping):
        return "held", "evidence_missing", None
    if evidence.get("candidate_sha256") != _digest(candidate):
        return "held", "source_or_template_stale", None
    if evidence.get("target_fingerprint") != candidate["target_fingerprint"]:
        return "held", "other_target", None
    if evidence.get("source") != candidate["source"]:
        return "held", "source_mismatch", None
    if evidence.get("kind") == "missing_verified":
        lookup = evidence.get("lookup")
        if (isinstance(lookup, Mapping) and lookup.get("complete") is True
                and lookup.get("keys") == [] and lookup.get("result_limited") is False
                and lookup.get("source") == candidate["source"]
                and lookup.get("target_fingerprint") == candidate["target_fingerprint"]):
            return "missing_verified", "verified_absent", None
        return "held", "absence_unverified", None
    target_key = evidence.get("target_key")
    if not isinstance(target_key, str) or not target_key or len(target_key) > 256:
        return "held", "target_key_missing", None
    readback = evidence.get("readback")
    context = evidence.get("readback_context")
    entity_set = candidate["prepared"]["entity_set"]
    catalog = candidate.get("workflow") == _CATALOG_WORKFLOW
    hr = candidate.get("workflow") == _HR_WORKFLOW
    create_only = catalog or hr
    # A raw document may omit its collection/target; require a separately bound
    # readback context in that case. Any context present on the raw result must agree.
    bound = readback if isinstance(readback, Mapping) and all(
        field in readback for field in ("entity_set", "target_fingerprint")
    ) else context
    if (not isinstance(bound, Mapping) or bound.get("entity_set") != entity_set
            or bound.get("target_fingerprint") != candidate["target_fingerprint"]
            or (context is not None and
                (not isinstance(context, Mapping) or context.get("entity_set") != entity_set
                 or context.get("target_fingerprint") != candidate["target_fingerprint"]))
            or (isinstance(readback, Mapping) and
                (("entity_set" in readback and readback["entity_set"] != entity_set)
                 or ("target_fingerprint" in readback and
                     readback["target_fingerprint"] != candidate["target_fingerprint"])))):
        return "held", "readback_context_unverified", target_key
    if (not isinstance(readback, Mapping) or readback.get("Ref_Key") != target_key
            or (not catalog and readback.get("Posted") is not (False if hr else True))
            or readback.get("DeletionMark") is not False
            or not _matches(candidate["prepared"]["payload"], readback)):
        return "held", "readback_unverified", target_key
    movement = evidence.get("active_movements")
    if hr:
        # This workflow requires a full separately addressed GET, never a Create
        # response or a selected summary masquerading as a complete document.
        if (not isinstance(context, Mapping) or set(context) != {
                "entity_set", "target_fingerprint", "database", "key", "full_get"}
                or context["database"] != candidate.get("database")
                or context["key"] != target_key or context["full_get"] is not True
                or "active_movements" in evidence):
            return "held", "hr_full_get_unverified", target_key
    if not create_only and not _movement_matches(candidate["prepared"]["expected_accounting_movements"],
                                             movement, target_key, entity_set):
        return "held", "movements_unverified", target_key
    kind = evidence.get("kind")
    if kind == "found_existing_verified":
        lookup = evidence.get("lookup")
        if (not isinstance(lookup, Mapping) or lookup.get("complete") is not True
                or lookup.get("result_limited") is not False
                or lookup.get("source") != candidate["source"]
                or lookup.get("target_fingerprint") != candidate["target_fingerprint"]
                or lookup.get("keys") != [target_key]):
            return "held", "target_ambiguous", target_key
    elif kind == "newly_created_verified":
        manifest = evidence.get("manifest")
        receipt = evidence.get("result_receipt")
        if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
            return "held", "queue_receipt_missing", target_key
        try:
            if (manifest.get("workflow", "documents") != candidate.get("workflow", "documents")
                    or manifest.get("database") != candidate.get("database")
                    or manifest.get("target_fingerprint") != candidate["target_fingerprint"]
                    or manifest.get("template") != candidate["template"]):
                return "held", "queue_plan_mismatch", target_key
            rows = manifest.get("rows", [])
            matched = [row for row in rows if row.get("source_id") == candidate["prepared"]["source_id"]]
            if len(matched) != 1:
                return "held", "queue_plan_mismatch", target_key
            row = matched[0]
            if any(row.get(field) != candidate["prepared"].get(field) for field in
                   ("entity_set", "payload", "post", "expected_accounting_movements")):
                return "held", "queue_plan_mismatch", target_key
            result = receipt.get("result")
            if record_result(manifest, result) != receipt:
                return "held", "queue_receipt_invalid", target_key
            if create_only:
                saved = result.get("result")
                if (result.get("state") != "complete" or not isinstance(saved, Mapping)
                        or saved.get("key") != target_key or saved.get("entitySet") != entity_set
                        or saved.get("writeState") not in {"accepted", "accepted_unverified", "reconciled_present"}):
                    return "held", "catalog_outcome_unverified", target_key
            else:
                results = [item for item in result["results"] if item["operationId"] == row["operation_id"]]
                if (len(results) != 1 or results[0].get("postOperationId") != row["post_operation_id"]
                        or results[0].get("key") != target_key or results[0].get("ok") is not True
                        or results[0].get("state") not in {"posted", "accepted_verified"}
                        or sum(item.get("key") == target_key for item in result["results"]) != 1):
                    return "held", "queue_outcome_unverified", target_key
        except (QueueManifestError, TypeError, KeyError, AttributeError, ValueError):
            return "held", "queue_receipt_invalid", target_key
    else:
        return "held", "kind_unknown", target_key
    return kind, "verified", target_key


@contextmanager
def _locked(path: Path):
    directory = path.parent
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise QueueManifestError("checkpoint directory must be owner-only and not a symlink")
    lock = directory / (path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise QueueManifestError("checkpoint lock must be owner-only")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise QueueManifestError("checkpoint cannot be a symlink")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _validate_claim(batch_id: str, claim: Any, entries: Mapping[str, Any]) -> None:
    fields = {
        "manifest", "rows", "stage", "preview_request_sha256", "preview_result_sha256",
        "preview_receipt",
    }
    ordinary = [fields | optional for optional in (
        set(), {"resolution_sha256"}, {"prerequisites"},
        {"resolution_sha256", "prerequisites"})]
    allowed = ordinary + [shape | {"hr_sources"} for shape in ordinary]
    allowed += [fields | {"resolution_sha256", "repair_audit"}]
    if not isinstance(claim, dict) or set(claim) not in allowed:
        raise QueueManifestError("checkpoint claim shape is invalid")
    manifest = _validate_manifest_history(claim["manifest"])
    if batch_id != manifest["batch_id"] or claim["stage"] not in {
        "prepared", "previewed", "verified_preview", "dispatch_uncertain", "resolved",
    }:
        raise QueueManifestError("checkpoint claim identity or stage is invalid")
    rows = claim["rows"]
    if not isinstance(rows, list) or len(rows) != len(manifest["rows"]):
        raise QueueManifestError("checkpoint claim rows differ from manifest")
    keys: set[str] = set()
    for position, row in enumerate(rows):
        if (not isinstance(row, dict) or set(row) != {"key", "candidate_sha256", "evidence_sha256"}
                or not isinstance(row["key"], str) or row["key"] in keys
                or not all(isinstance(row[name], str) and _HEX.fullmatch(row[name])
                           for name in ("candidate_sha256", "evidence_sha256"))):
            raise QueueManifestError("checkpoint claim row binding is invalid")
        keys.add(row["key"])
        if row["key"] not in entries:
            raise QueueManifestError("checkpoint claim source entry is missing")
        entity_set = entries[row["key"]].get("catalog_entity_set")
        if entity_set is not None and (
                manifest.get("workflow") != _CATALOG_WORKFLOW
                or manifest["rows"][position]["entity_set"] != entity_set):
            raise QueueManifestError("checkpoint claim Catalog binding is invalid")
        hr_entity = entries[row["key"]].get("hr_entity_set")
        if hr_entity is not None and (
                manifest.get("workflow") != _HR_WORKFLOW
                or manifest["rows"][position]["entity_set"] != hr_entity):
            raise QueueManifestError("checkpoint claim HR binding is invalid")
    if "prerequisites" in claim:
        dependencies = claim["prerequisites"]
        if not isinstance(dependencies, dict) or not dependencies or not set(dependencies) <= keys:
            raise QueueManifestError("checkpoint claim prerequisites are invalid")
        for position, row in enumerate(rows):
            if row["key"] in dependencies:
                candidate = {"target_fingerprint": manifest["target_fingerprint"],
                             "database": manifest.get("database"),
                             "prepared": manifest["rows"][position]}
                _prerequisite_shape(dependencies[row["key"]], candidate)
    if manifest.get("workflow") == _HR_WORKFLOW:
        sources = claim.get("hr_sources")
        if (not isinstance(sources, dict) or set(sources) != keys
                or not isinstance(claim.get("prerequisites"), dict)
                or set(claim["prerequisites"]) != keys):
            raise QueueManifestError("HR claim source/dependency binding missing")
        from .hr_source import source_shape
        for row in rows:
            source_shape(sources[row["key"]])
    elif "hr_sources" in claim:
        raise QueueManifestError("HR source proof belongs only to HR claims")
    request_digest = claim["preview_request_sha256"]
    result_digest = claim["preview_result_sha256"]
    receipt = claim["preview_receipt"]
    if claim["stage"] == "prepared":
        if any(item is not None for item in (request_digest, result_digest, receipt)):
            raise QueueManifestError("prepared claim has preview data")
    elif claim["stage"] == "previewed":
        if request_digest != _historical_preview_digest(manifest) or result_digest is not None or receipt is not None:
            raise QueueManifestError("previewed claim is invalid")
    else:
        if (request_digest != _historical_preview_digest(manifest)
                or not isinstance(result_digest, str) or not _HEX.fullmatch(result_digest)
                or not isinstance(receipt, dict)
                or receipt != {"schema_version": 1, "plan_sha256": manifest["plan_sha256"],
                               "target_fingerprint": manifest["target_fingerprint"],
                               "template": manifest["template"], "dry_run_verified": True}):
            raise QueueManifestError("verified claim is invalid")
    resolution = claim.get("resolution_sha256")
    if (claim["stage"] == "resolved") != (isinstance(resolution, str) and bool(_HEX.fullmatch(resolution))):
        raise QueueManifestError("resolved claim tombstone is invalid")
    if "repair_audit" in claim:
        audit = claim["repair_audit"]
        from .person_repair import repair_resolution_core
        if (claim["stage"] != "resolved" or not isinstance(audit, dict)
                or audit.get("resolution_sha256") != resolution
                or set(audit) not in (
                    {"resolution_sha256", "plan", "acceptance", "evidence", "original_claim",
                     "original_result_receipt", "original_entry", "source_bundle"},
                    {"resolution_sha256", "plan", "acceptance", "evidence", "original_claim",
                     "original_result_receipt", "original_entry", "source_bundle",
                     "effect_appendix", "effect_acceptance"})
                or _digest(repair_resolution_core(audit["plan"], audit["acceptance"], audit["evidence"],
                    audit["source_bundle"], audit.get("effect_appendix"), audit.get("effect_acceptance"))) != resolution):
            raise QueueManifestError("repair audit tombstone invalid")
        original = audit["original_claim"]
        if not isinstance(original, dict) or original.get("stage") != "dispatch_uncertain":
            raise QueueManifestError("repair original claim audit invalid")
        _validate_claim(batch_id, original, entries)
        if {k: v for k, v in claim.items() if k not in {"stage", "resolution_sha256", "repair_audit"}} != {
                k: v for k, v in original.items() if k != "stage"}:
            raise QueueManifestError("repair original claim history changed")
        # Outer checkpoint sealing alone must not mask lost/rebound history.
        try:
            from .person_repair import PROFILE
            plan = audit["plan"]
            if "effect_appendix" in audit:
                from .person_repair import _effect_structure, _effect_acceptance_structure, _effect_remainder
                _effect_structure(plan, audit["source_bundle"], audit["acceptance"],
                                  audit["effect_appendix"], audit["evidence"][0])
                _effect_acceptance_structure(audit["effect_appendix"], audit["evidence"],
                                             audit["effect_acceptance"])
            from .person_repair import _effect_remainder
            for proof in audit["evidence"]:
                _effect_remainder(proof["before"], proof["after"],
                                  allow_service="effect_appendix" in audit)
            plan_core = {k: v for k, v in plan.items() if k != "plan_sha256"}
            bundle = audit["source_bundle"]
            bundle_core = {k: v for k, v in bundle.items() if k != "source_bundle_sha256"}
            if (set(bundle_core) != {"schema_version", "plan_sha256", "accepted_handoff"}
                    or bundle_core["schema_version"] != 1
                    or bundle_core["plan_sha256"] != plan["plan_sha256"]
                    or bundle["source_bundle_sha256"] != _digest(bundle_core)
                    or audit["acceptance"]["source_bundle_sha256"] != bundle["source_bundle_sha256"]
                    or audit["acceptance"]["accepted_handoff_sha256"] != bundle_core["accepted_handoff"]["sha256"]):
                raise QueueManifestError("repair history source bundle invalid")
            if (set(plan_core) != {"schema_version", "profile", "checkpoint_sha256", "bindings"}
                    or plan_core["schema_version"] != 1 or plan_core["profile"] != PROFILE
                    or plan["plan_sha256"] != _digest(plan_core)
                    or not isinstance(plan["bindings"], list) or not plan["bindings"]
                    or len(rows) != 1):
                raise QueueManifestError("repair history plan invalid")
            bindings = [b for b in plan["bindings"] if b.get("manifest") == manifest]
            if len(bindings) != 1:
                raise QueueManifestError("repair history manifest binding invalid")
            binding = bindings[0]
            candidate = _candidate(binding["candidate"])
            row = rows[0]
            old_entry = audit["original_entry"]
            entry_fields = {"source", "candidate_sha256", "target_fingerprint", "status",
                            "reason", "target_key", "evidence_sha256"}
            if (not isinstance(old_entry, dict)
                    or set(old_entry) not in (entry_fields, entry_fields | {"catalog_entity_set"})
                    or candidate.get("workflow") != _CATALOG_WORKFLOW
                    or old_entry["status"] != "missing_verified" or old_entry["target_key"] is not None
                    or old_entry["source"] != candidate["source"]
                    or old_entry["target_fingerprint"] != candidate["target_fingerprint"]
                    or old_entry["candidate_sha256"] != _digest(candidate)
                    or old_entry["candidate_sha256"] != row["candidate_sha256"]
                    or old_entry["evidence_sha256"] != row["evidence_sha256"]
                    or not isinstance(old_entry["reason"], str) or not old_entry["reason"]
                    or ("catalog_entity_set" in old_entry and
                        old_entry["catalog_entity_set"] != candidate["prepared"]["entity_set"])
                    or row["key"] != (_key(candidate) if "catalog_entity_set" in old_entry
                                      else _legacy_key(candidate))):
                raise QueueManifestError("repair original entry binding invalid")
            original_result = binding["original_result"]
            original_receipt = _record_historical_result(manifest, original_result)
            if (audit["original_result_receipt"] != original_receipt
                    or original_result.get("state") != "complete"
                    or original_result.get("result", {}).get("key") != binding["target_key"]
                    or original_result.get("database") != candidate.get("database")
                    or entries[row["key"]]["target_key"] != binding["target_key"]
                    or entries[row["key"]]["evidence_sha256"] != _digest(audit)):
                raise QueueManifestError("repair original receipt or terminal audit binding invalid")
        except (KeyError, TypeError, AttributeError, ValueError):
            raise QueueManifestError("repair original history shape invalid") from None
    if claim["stage"] == "resolved":
        for row in rows:
            entry = entries[row["key"]]
            if (entry["status"] != "newly_created_verified" or
                    entry["candidate_sha256"] != row["candidate_sha256"] or
                    entry["target_key"] is None):
                raise QueueManifestError("resolved claim entry is inconsistent")


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": _SCHEMA, "entries": {}, "claims": {}}
    if path.stat().st_mode & 0o077:
        raise QueueManifestError("checkpoint file must be owner-only")
    value = read_private_json(path)
    version = value.get("schema_version") if isinstance(value, dict) else None
    fields = {"schema_version", "entries", "index_sha256"} | ({"claims"} if version in {3, _SCHEMA} else set())
    if (version not in {2, 3, _SCHEMA} or set(value) != fields
            or not isinstance(value.get("entries"), dict)
            or (version in {3, _SCHEMA} and not isinstance(value.get("claims"), dict))
            or value.get("index_sha256") != _digest({key: value[key] for key in fields - {"index_sha256"}})):
        raise QueueManifestError("checkpoint schema is invalid")
    for key, entry in value["entries"].items():
        if (not isinstance(key, str) or not isinstance(entry, dict)
                or set(entry) not in (
                    {"source", "candidate_sha256", "target_fingerprint", "status",
                     "reason", "target_key", "evidence_sha256"},
                    {"source", "candidate_sha256", "target_fingerprint", "status",
                     "reason", "target_key", "evidence_sha256", "catalog_entity_set"},
                    {"source", "candidate_sha256", "target_fingerprint", "status",
                     "reason", "target_key", "evidence_sha256", "hr_entity_set"})
                or not isinstance(entry["status"], str) or entry["status"] not in _STATUSES
                or not isinstance(entry["reason"], str) or not entry["reason"]
                or not isinstance(entry["target_fingerprint"], str)
                or not entry["target_fingerprint"]
                or not isinstance(entry["candidate_sha256"], str)
                or not _HEX.fullmatch(entry["candidate_sha256"])
                or not isinstance(entry["evidence_sha256"], str)
                or not _HEX.fullmatch(entry["evidence_sha256"])):
            raise QueueManifestError("checkpoint entry is invalid")
        try:
            expected_key = _canonical_json([_source({"source": entry["source"]}),
                                            entry["target_fingerprint"]])
            if "catalog_entity_set" in entry:
                if version != _SCHEMA or not _valid_catalog_entity_set(entry["catalog_entity_set"]):
                    raise QueueManifestError("checkpoint Catalog entity is invalid")
                expected_key = _canonical_json([_source({"source": entry["source"]}),
                                               entry["target_fingerprint"], _CATALOG_WORKFLOW,
                                               entry["catalog_entity_set"]])
            if "hr_entity_set" in entry:
                if (version != _SCHEMA or not _valid_document_entity_set(entry["hr_entity_set"])
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry["source"]["namespace"])
                        or not re.fullmatch(r"native-document-[0-9a-f]{64}", entry["source"]["line"])):
                    raise QueueManifestError("checkpoint HR native entity is invalid")
                expected_key = _key({**entry, "workflow": _HR_WORKFLOW,
                                     "prepared": {"entity_set": entry["hr_entity_set"]}})
        except (QueueManifestError, TypeError, ValueError):
            raise QueueManifestError("checkpoint entry source is invalid") from None
        target_key = entry["target_key"]
        if (key != expected_key or
                (target_key is not None and
                 (not isinstance(target_key, str) or not target_key or len(target_key) > 256)) or
                (entry["status"] == "missing_verified" and target_key is not None) or
                (entry["status"] in {"found_existing_verified", "newly_created_verified"}
                 and target_key is None)):
            raise QueueManifestError("checkpoint entry key or status is inconsistent")
    if version != _SCHEMA:
        value = {"schema_version": _SCHEMA, "entries": value["entries"],
                 "claims": value.get("claims", {})}
    reserved: set[str] = set()
    for batch_id, claim in value["claims"].items():
        _validate_claim(batch_id, claim, value["entries"])
        if claim["stage"] == "resolved":
            continue
        for row in claim["rows"]:
            if row["key"] in reserved:
                raise QueueManifestError("checkpoint source is reserved by multiple claims")
            reserved.add(row["key"])
    if version != _SCHEMA:
        _write(path, value)
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            core = {"schema_version": value["schema_version"], "entries": value["entries"],
                    "claims": value["claims"]}
            sealed = {**core, "index_sha256": _digest(core)}
            stream.write(_canonical_json(sealed) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_evidence(path: str | os.PathLike[str], candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate(candidate)
    status, reason, target_key = _evaluate(candidate, evidence)
    digest = _digest(candidate)
    with _locked(Path(path)):
        index = _read(Path(path))
        key = _checkpoint_key(index, candidate)
        _prerequisites(index, candidate, legacy=key == _legacy_key(candidate))
        entries = index["entries"]
        if any(claim["stage"] == "dispatch_uncertain" and
               any(row["key"] == key for row in claim["rows"])
               for claim in index["claims"].values()):
            raise QueueManifestError("dispatch uncertain; reconcile same operation IDs before importing evidence")
        if any(claim["stage"] == "resolved" and
               any(row["key"] == key for row in claim["rows"])
               for claim in index["claims"].values()):
            raise QueueManifestError("resolved claim is terminal; source evidence cannot be replaced")
        previous = entries.get(key)
        if previous:
            if previous["reason"] in _STICKY_CONFLICT_REASONS:
                # A later ordinary import cannot resolve a recorded contradiction.
                # Preserve the checkpoint entry until an explicit reconciliation/rebuild.
                return {"source": candidate["source"], **previous}
            elif previous["candidate_sha256"] != digest:
                status, reason = "held", "source_or_template_stale"
            elif previous["status"] == "missing_verified" and status in {
                "newly_created_verified", "found_existing_verified"
            }:
                pass
            elif previous["status"] != "held" and previous["target_key"] != target_key:
                status, reason = "held", "target_conflict"
            elif (previous["status"] != "held" and
                  (status != previous["status"] or previous["evidence_sha256"] != _digest(evidence))):
                status, reason = "held", "conflicting_evidence"
        if status not in {"held", "missing_verified"}:
            conflicts = _target_conflicts(index, candidate, key, target_key)
            if any(claim["stage"] == "resolved" and
                   any(row["key"] in conflicts for row in claim["rows"])
                   for claim in index["claims"].values()):
                raise QueueManifestError("target conflicts with a resolved terminal claim")
            for other_key in conflicts:
                status, reason = "held", "target_ambiguous"
                entries[other_key]["status"] = "held"
                entries[other_key]["reason"] = "target_ambiguous"
        entry = {
            "source": candidate["source"],
            "candidate_sha256": digest,
            "target_fingerprint": candidate["target_fingerprint"],
            "status": status, "reason": reason, "target_key": target_key,
            "evidence_sha256": _digest(evidence),
            **_catalog_fields(candidate, key),
        }
        if previous != entry:
            entries[key] = entry
            _write(Path(path), index)
        return {"source": candidate["source"], **entry}


def screen_rows(path: str | os.PathLike[str], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    prepared = [_candidate(candidate) for candidate in candidates]
    keys = [_key(candidate) for candidate in prepared]
    if len(set(keys)) != len(keys):
        raise QueueManifestError("candidate source keys must be unique")
    with _locked(Path(path)):
        index = _read(Path(path))
        binding_errors = {}
        keys = []
        for position, candidate in enumerate(prepared):
            try:
                keys.append(_checkpoint_key(index, candidate))
                _prerequisites(index, candidate, legacy=keys[-1] == _legacy_key(candidate))
            except QueueManifestError as exc:
                if len(keys) == position:
                    keys.append(_key(candidate))
                binding_errors[position] = str(exc)
        if len(set(keys)) != len(keys):
            raise QueueManifestError("candidate source keys must be unique")
        entries = index["entries"]
        reserved = {row["key"]: claim["stage"] for claim in index["claims"].values()
                    if claim["stage"] != "resolved"
                    for row in claim["rows"]}
    ready_count = 0
    held: list[dict[str, Any]] = []
    counts = {"found_existing_verified": 0, "newly_created_verified": 0}
    reasons: dict[str, int] = {}
    for position, (candidate, key) in enumerate(zip(prepared, keys)):
        entry = entries.get(key)
        if position in binding_errors or entry is None:
            reason = binding_errors.get(position, "checkpoint_missing")
            held.append({"source": candidate["source"], "reason": reason})
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        if entry["target_fingerprint"] != candidate["target_fingerprint"]:
            reason = "other_target"
        elif entry["candidate_sha256"] != _digest(candidate):
            reason = "source_or_template_stale"
        elif key in reserved:
            reason = ("dispatch_uncertain" if reserved[key] == "dispatch_uncertain"
                      else "claim_reserved")
        elif entry["status"] == "missing_verified":
            ready_count += 1
            continue
        elif entry["status"] in counts:
            counts[entry["status"]] += 1
            continue
        else:
            reason = entry["reason"]
        held.append({"source": candidate["source"], "reason": reason})
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "ready_batch_count": (ready_count + 49) // 50,
        "ready_count": ready_count, "held": held, "held_count_by_reason": reasons,
        **counts,
    }


def _eligible(index: Mapping[str, Any], claim: Mapping[str, Any]) -> None:
    for position, row in enumerate(claim["rows"]):
        preflight_person_names(claim["manifest"]["rows"][position]["entity_set"],
                               claim["manifest"]["rows"][position]["payload"])
        entry = index["entries"].get(row["key"])
        if (entry is None or entry["status"] != "missing_verified"
                or entry["candidate_sha256"] != row["candidate_sha256"]
                or entry["evidence_sha256"] != row["evidence_sha256"]):
            raise QueueManifestError("claimed source evidence or status changed; no arguments emitted")
        manifest = claim["manifest"]
        if manifest.get("workflow") == _HR_WORKFLOW:
            original = _hr_claim_candidate(index, claim, position)
            if _digest(_candidate(original)) != row["candidate_sha256"]:
                raise QueueManifestError("HR claim source or candidate binding changed")
            if _checkpoint_key(index, original) != row["key"]:
                raise QueueManifestError("HR native claim binding changed; reconcile original operation")
        _prerequisites(index, {
            "source": entry["source"], "target_fingerprint": manifest["target_fingerprint"],
            "workflow": manifest.get("workflow", "documents"),
            "prepared": manifest["rows"][position],
            "prerequisites": claim.get("prerequisites", {}).get(row["key"], []),
            **({"source_evidence": claim["hr_sources"][row["key"]]}
               if manifest.get("workflow") == _HR_WORKFLOW else {}),
        }, legacy="catalog_entity_set" not in entry)


def _hr_claim_candidate(index: Mapping[str, Any], claim: Mapping[str, Any], position: int) -> dict:
    manifest = claim["manifest"]
    row = claim["rows"][position]
    entry = index["entries"][row["key"]]
    return {"source": entry["source"], "target_fingerprint": manifest["target_fingerprint"],
            "template": manifest["template"], "workflow": _HR_WORKFLOW,
            **({"database": manifest["database"]} if "database" in manifest else {}),
            "prepared": {k: v for k, v in manifest["rows"][position].items()
                         if k not in {"operation_id", "post_operation_id"}},
            "prerequisites": claim["prerequisites"][row["key"]],
            "source_evidence": claim["hr_sources"][row["key"]]}


def _claim(index: Mapping[str, Any], batch_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    claim = index["claims"].get(batch_id)
    if claim is None or claim["manifest"] != manifest:
        raise QueueManifestError("batch claim is missing or manifest changed")
    return claim


def prepare_claim(path: str | os.PathLike[str], candidates: Sequence[Mapping[str, Any]], *,
                  batch_id: str, max_concurrency: int = 1) -> dict[str, Any]:
    prepared = [_candidate(item) for item in candidates]
    if not prepared:
        raise QueueManifestError("claim needs 1..50 candidates")
    for item in prepared:
        preflight_person_names(item["prepared"]["entity_set"], item["prepared"]["payload"])
    first = prepared[0]
    if any(item["target_fingerprint"] != first["target_fingerprint"] or
           item["template"] != first["template"] or
           item.get("workflow", "documents") != first.get("workflow", "documents") or
           item.get("database") != first.get("database") for item in prepared):
        raise QueueManifestError("claim rows must share target and template")
    manifest = compile_manifest(
        [item["prepared"] for item in prepared], batch_id=batch_id,
        target_fingerprint=first["target_fingerprint"],
        template_id=first["template"]["id"], template_version=first["template"]["version"],
        max_concurrency=max_concurrency,
        workflow=first.get("workflow", "documents"),
        database=first.get("database"),
    )
    keys = [_key(item) for item in prepared]
    if len(set(keys)) != len(keys):
        raise QueueManifestError("claim source keys must be unique")
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        keys = [_checkpoint_key(index, item) for item in prepared]
        for key, item in zip(keys, prepared):
            _prerequisites(index, item, legacy=key == _legacy_key(item))
        if len(set(keys)) != len(keys):
            raise QueueManifestError("claim source keys must be unique")
        if batch_id in index["claims"]:
            existing = _claim(index, batch_id, manifest)
            if existing["stage"] == "resolved":
                raise QueueManifestError("batch ID is permanently resolved and cannot be reused")
            if existing["stage"] == "dispatch_uncertain":
                raise QueueManifestError("dispatch uncertain; reconcile same operation IDs before further action")
            if existing["rows"] != [
                {"key": key, "candidate_sha256": _digest(item),
                 "evidence_sha256": index["entries"].get(key, {}).get("evidence_sha256")}
                for item, key in zip(prepared, keys)
            ]:
                raise QueueManifestError("batch claim source, order, or evidence changed")
            _eligible(index, existing)
            return manifest
        occupied = {row["key"] for claim in index["claims"].values() for row in claim["rows"]}
        if occupied.intersection(keys):
            raise QueueManifestError("source and target already reserved by another batch")
        rows = []
        for item, key in zip(prepared, keys):
            entry = index["entries"].get(key)
            if (entry is None or entry["status"] != "missing_verified"
                    or entry["candidate_sha256"] != _digest(item)):
                raise QueueManifestError("claim requires exact current missing_verified evidence")
            rows.append({"key": key, "candidate_sha256": entry["candidate_sha256"],
                         "evidence_sha256": entry["evidence_sha256"]})
        index["claims"][batch_id] = {
            "manifest": manifest, "rows": rows, "stage": "prepared",
            "preview_request_sha256": None, "preview_result_sha256": None,
            "preview_receipt": None,
        }
        dependencies = {key: item["prerequisites"] for key, item in zip(keys, prepared)
                        if "prerequisites" in item}
        if dependencies:
            index["claims"][batch_id]["prerequisites"] = dependencies
        if first.get("workflow") == _HR_WORKFLOW:
            index["claims"][batch_id]["hr_sources"] = {
                key: item["source_evidence"] for key, item in zip(keys, prepared)}
        _write(destination, index)
    return manifest


def preview_claim(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        claim = _claim(index, manifest["batch_id"], manifest)
        if claim["stage"] == "dispatch_uncertain":
            raise QueueManifestError("dispatch uncertain; reconcile same operation IDs")
        _eligible(index, claim)
        request = dry_run_request(manifest)["tool_arguments"]
        if claim["stage"] != "previewed":
            claim["stage"] = "previewed"
            claim["preview_request_sha256"] = _digest(request)
            claim["preview_result_sha256"] = None
            claim["preview_receipt"] = None
            _write(destination, index)
        return request


def verify_preview_claim(path: str | os.PathLike[str], manifest: Mapping[str, Any],
                         request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        claim = _claim(index, manifest["batch_id"], manifest)
        if claim["stage"] not in {"previewed", "verified_preview"}:
            raise QueueManifestError("claim needs a current issued preview")
        _eligible(index, claim)
        if _digest(request) != claim["preview_request_sha256"]:
            raise QueueManifestError("saved preview request differs from issued request")
        receipt = verify_dry_run(manifest, result, request)
        if claim["stage"] == "verified_preview":
            if claim["preview_result_sha256"] != _digest(result) or claim["preview_receipt"] != receipt:
                raise QueueManifestError("verified preview result differs from saved claim")
            return receipt
        claim["stage"] = "verified_preview"
        claim["preview_result_sha256"] = _digest(result)
        claim["preview_receipt"] = receipt
        _write(destination, index)
        return receipt


def apply_claim(path: str | os.PathLike[str], manifest: Mapping[str, Any],
                receipt: Mapping[str, Any], *, target_fingerprint: str,
                template_id: str, template_version: str) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        claim = _claim(index, manifest["batch_id"], manifest)
        if claim["stage"] != "verified_preview" or claim["preview_receipt"] != receipt:
            raise QueueManifestError("current verified preview receipt is required")
        _eligible(index, claim)
        request = apply_request(manifest, receipt, target_fingerprint=target_fingerprint,
                                template_id=template_id, template_version=template_version)
        claim["stage"] = "dispatch_uncertain"
        _write(destination, index)
        return request


def resolve_claim(path: str | os.PathLike[str], manifest: Mapping[str, Any],
                  candidates: Sequence[Mapping[str, Any]], result: Mapping[str, Any],
                  evidences: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Atomically terminalize an exact dispatched batch after full offline verification."""
    manifest = validate_manifest(manifest)
    prepared = [_candidate(item) for item in candidates]
    if not isinstance(evidences, list) or len(prepared) != len(manifest["rows"]) or len(evidences) != len(prepared):
        raise QueueManifestError("resolution needs one candidate and evidence per manifest row")
    receipt = record_result(manifest, result)
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        bindings = [{"key": _checkpoint_key(index, item), "candidate_sha256": _digest(item)}
                    for item in prepared]
        resolution_digest = _digest({"bindings": bindings, "result_receipt": receipt,
                                     "evidence_sha256": [_digest(item) for item in evidences]})
        claim = _claim(index, manifest["batch_id"], manifest)
        if claim["stage"] == "resolved":
            if claim["resolution_sha256"] != resolution_digest:
                raise QueueManifestError("resolved batch evidence differs from terminal tombstone")
            return {"batch_id": manifest["batch_id"], "status": "resolved",
                    "newly_created_verified": len(prepared)}
        if claim["stage"] != "dispatch_uncertain":
            raise QueueManifestError("resolution requires a dispatched uncertain claim")
        for saved, supplied in zip(claim["rows"], bindings):
            if saved["key"] != supplied["key"] or saved["candidate_sha256"] != supplied["candidate_sha256"]:
                raise QueueManifestError("resolution source, candidate, or order differs from claim")
        _eligible(index, claim)
        target_keys: set[str] = set()
        new_entries = {}
        for candidate, binding, evidence in zip(prepared, bindings, evidences):
            if (not isinstance(evidence, Mapping) or evidence.get("manifest") != manifest
                    or evidence.get("result_receipt") != receipt):
                raise QueueManifestError("resolution needs exact saved manifest and result receipt per row")
            status, reason, target_key = _evaluate(candidate, evidence)
            if status != "newly_created_verified" or target_key is None:
                raise QueueManifestError(f"resolution row is not fully verified: {reason}")
            if target_key in target_keys:
                raise QueueManifestError("resolution target key is shared by multiple rows")
            target_keys.add(target_key)
            if _target_conflicts(index, candidate, binding["key"], target_key):
                raise QueueManifestError("resolution target key conflicts with another source")
            new_entries[binding["key"]] = {
                "source": candidate["source"], "candidate_sha256": binding["candidate_sha256"],
                "target_fingerprint": candidate["target_fingerprint"],
                "status": status, "reason": reason, "target_key": target_key,
                "evidence_sha256": _digest(evidence),
                **_catalog_fields(candidate, binding["key"]),
            }
        index["entries"].update(new_entries)
        claim["stage"] = "resolved"
        claim["resolution_sha256"] = resolution_digest
        _write(destination, index)
    return {"batch_id": manifest["batch_id"], "status": "resolved",
            "newly_created_verified": len(prepared)}


def cancel_claim(path: str | os.PathLike[str], *, batch_id: str, plan_sha256: str) -> None:
    destination = Path(path)
    with _locked(destination):
        index = _read(destination)
        claim = index["claims"].get(batch_id)
        if claim is None or claim["manifest"]["plan_sha256"] != plan_sha256:
            raise QueueManifestError("exact owner claim is required for cancellation")
        if claim["stage"] in {"dispatch_uncertain", "resolved"}:
            raise QueueManifestError("dispatch uncertain or resolved; cancellation is forbidden")
        del index["claims"][batch_id]
        _write(destination, index)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline verified-row checkpoint; no MCP/OData calls")
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="evaluate saved evidence for one prepared source row")
    imp.add_argument("--checkpoint", required=True)
    imp.add_argument("--candidate", required=True)
    imp.add_argument("--evidence", required=True)
    screen = commands.add_parser("screen", help="read-only counts; no apply authorization")
    screen.add_argument("--checkpoint", required=True)
    screen.add_argument("--candidates", required=True, help="JSON object with candidates array")
    screen.add_argument("--output", required=True, help="new private JSON result file")
    prepare = commands.add_parser("prepare", help="atomically claim missing rows and save immutable manifest")
    prepare.add_argument("--checkpoint", required=True)
    prepare.add_argument("--candidates", required=True)
    prepare.add_argument("--batch-id", required=True)
    prepare.add_argument("--max-concurrency", type=int, default=1)
    prepare.add_argument("--output", required=True, help="new private manifest file")
    preview = commands.add_parser("preview", help="issue exact dry-run arguments for a current claim")
    preview.add_argument("--checkpoint", required=True)
    preview.add_argument("--manifest", required=True)
    preview.add_argument("--save-request", required=True, help="new private copy of emitted arguments")
    verify = commands.add_parser("verify-preview", help="bind saved request and result to current claim")
    verify.add_argument("--checkpoint", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--request", required=True)
    verify.add_argument("--result", required=True)
    verify.add_argument("--output", required=True, help="new private preview receipt")
    apply = commands.add_parser("apply", help="mark dispatch uncertain before emitting apply arguments")
    apply.add_argument("--checkpoint", required=True)
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--preview-receipt", required=True)
    apply.add_argument("--target-fingerprint", required=True)
    apply.add_argument("--template-id", required=True)
    apply.add_argument("--template-version", required=True)
    resolve = commands.add_parser("resolve", help="terminalize a fully verified dispatched batch offline")
    resolve.add_argument("--checkpoint", required=True)
    resolve.add_argument("--manifest", required=True)
    resolve.add_argument("--candidates", required=True, help="original ordered candidates JSON")
    resolve.add_argument("--result", required=True, help="saved actual queue apply result JSON")
    resolve.add_argument("--evidence", required=True, help="JSON object with ordered evidence array")
    repair = commands.add_parser("resolve-person-repair", help="offline audited original batch terminalization; no 1C writes")
    repair.add_argument("--checkpoint", required=True)
    repair.add_argument("--plan", required=True)
    repair.add_argument("--acceptance", required=True)
    repair.add_argument("--evidence", required=True)
    repair.add_argument("--source-bundle", required=True)
    repair.add_argument("--effect-appendix", help="private separately accepted exact3 service-effect appendix")
    repair.add_argument("--effect-acceptance", help="private independent all_rows effect acceptance")
    cancel = commands.add_parser("cancel", help="cancel an exact claim before dispatch")
    cancel.add_argument("--checkpoint", required=True)
    cancel.add_argument("--batch-id", required=True)
    cancel.add_argument("--plan-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_evidence(args.checkpoint, read_private_json(args.candidate), read_private_json(args.evidence))
        elif args.command == "screen":
            result = screen_rows(args.checkpoint, read_private_json(args.candidates)["candidates"])
            write_private_json(args.output, result)
        elif args.command == "prepare":
            from .queue_manifest import write_manifest
            manifest = prepare_claim(args.checkpoint, read_private_json(args.candidates)["candidates"],
                                     batch_id=args.batch_id, max_concurrency=args.max_concurrency)
            write_manifest(args.output, manifest)
            result = {"manifest": str(Path(args.output)), "plan_sha256": manifest["plan_sha256"],
                      "rows": len(manifest["rows"])}
        elif args.command == "preview":
            from .queue_manifest import read_manifest
            result = preview_claim(args.checkpoint, read_manifest(args.manifest))
            write_private_json(args.save_request, result)
        elif args.command == "verify-preview":
            from .queue_manifest import read_manifest
            result = verify_preview_claim(args.checkpoint, read_manifest(args.manifest),
                                          read_private_json(args.request), read_private_json(args.result))
            write_private_json(args.output, result)
        elif args.command == "apply":
            from .queue_manifest import read_manifest
            result = apply_claim(args.checkpoint, read_manifest(args.manifest),
                                 read_private_json(args.preview_receipt),
                                 target_fingerprint=args.target_fingerprint,
                                 template_id=args.template_id, template_version=args.template_version)
        elif args.command == "resolve":
            from .queue_manifest import read_manifest
            result = resolve_claim(args.checkpoint, read_manifest(args.manifest),
                                   read_private_json(args.candidates)["candidates"],
                                   read_private_json(args.result),
                                   read_private_json(args.evidence)["evidence"])
        elif args.command == "resolve-person-repair":
            from .person_repair import resolve_person_repair
            result = resolve_person_repair(args.checkpoint, read_private_json(args.plan),
                                           read_private_json(args.acceptance),
                                           read_private_json(args.evidence)["evidence"],
                                           source_bundle=read_private_json(args.source_bundle),
                                           effect_appendix=read_private_json(args.effect_appendix) if args.effect_appendix else None,
                                           effect_acceptance=read_private_json(args.effect_acceptance) if args.effect_acceptance else None)
        else:
            cancel_claim(args.checkpoint, batch_id=args.batch_id, plan_sha256=args.plan_sha256)
            result = {"cancelled": args.batch_id}
        displayed = ({key: result[key] for key in ("candidate_sha256", "status", "reason")}
                     if args.command == "import" else
                     {key: value for key, value in result.items() if key not in {"rows", "held"}}
                     if args.command == "screen" else result)
        print(json.dumps(displayed, ensure_ascii=False, indent=2))
        return 0
    except (QueueManifestError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"queue checkpoint error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
