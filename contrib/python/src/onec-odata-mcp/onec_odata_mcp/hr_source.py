"""Byte/semantic binding for one HR draft; factual provenance is checked outside.

All artifacts are private. A checksum or an assertion authored by the writer is
not source acceptance, a TEST grant, or a guarantee of native token lifetime.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .queue_manifest import QueueManifestError, _digest


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = "same-information-base-only; base continuity and lifetime unverified"
_NAMESPACE_SCHEME = "configured-alias-windows-path-sha256-v1"
_ASSIGNED_FIELDS = {"number", "code", "номер", "код", "табельныйномер", "личныйномер"}
_DATE_ROLES = {"document_date", "creation_date", "business_date"}


def _artifact(ref: Any) -> dict:
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
            or not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute()
            or not isinstance(ref["sha256"], str) or not _HEX.fullmatch(ref["sha256"])):
        raise QueueManifestError("HR source needs exact private artifact refs")
    try:
        path = Path(ref["path"])
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 1_000_000:
            raise ValueError
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        # Never expose artifact paths, native tokens, values, or JSON errors.
        raise QueueManifestError("HR source artifact unavailable or inconsistent") from None


def source_shape(proof: Any) -> None:
    if (not isinstance(proof, dict) or set(proof) != {
            "addressed_request_ref", "addressed_response_ref", "independent_review_ref",
            "scalar_bindings", "relation_bindings", "date_inventory_ref"}
            or not isinstance(proof["scalar_bindings"], list)
            or not 1 <= len(proof["scalar_bindings"]) <= 64
            or not isinstance(proof["relation_bindings"], list)
            or not 1 <= len(proof["relation_bindings"]) <= 16):
        raise QueueManifestError("HR source needs addressed proof and native Catalog relations")
    for field in ("addressed_request_ref", "addressed_response_ref", "independent_review_ref", "date_inventory_ref"):
        ref = proof[field]
        if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
                or not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute()
                or not isinstance(ref["sha256"], str) or not _HEX.fullmatch(ref["sha256"])):
            raise QueueManifestError("HR source ref shape invalid")


def _source_answer(ref: Any, operation: str) -> dict:
    saved = _artifact(ref)
    if "data" not in saved:
        if (saved.get("is_error", saved.get("isError")) is not False
                or not isinstance(saved.get("content"), list) or len(saved["content"]) != 1
                or saved["content"][0].get("type") != "text"):
            raise QueueManifestError("HR source response envelope invalid")
        saved = json.loads(saved["content"][0]["text"])
    if (not isinstance(saved, dict) or saved.get("schemaVersion") != "1.0"
            or saved.get("operation") != operation or saved.get("status") != "ok"):
        raise QueueManifestError("HR source response contract invalid")
    return saved


def _catalog_scope(request: dict, response: dict, namespace: str) -> None:
    """Require the guarded producer observation; old answers cannot supply it."""
    data = response.get("data", {})
    if (request.get("source_namespace") != namespace
            or data.get("sourceNamespace") != namespace
            or data.get("sourceNamespaceProof") != {
                "scheme": _NAMESPACE_SCHEME, "expected": namespace,
                "observed": namespace, "status": "matched-request"}):
        raise QueueManifestError("HR Catalog producer source scope is missing or changed")


def _catalog_lineage(candidate: Mapping[str, Any], binding: dict, dependency: dict, namespace: str,
                     request: dict, response: dict) -> dict:
    lineage = _artifact(binding["catalog_lineage_ref"])
    previous = dependency["candidate"]
    if (set(lineage) != {"schema_version", "kind", "candidate_sha256", "source", "target_key",
                        "target_fingerprint", "database", "original_catalog_request_ref",
                        "original_catalog_response_ref", "verified_evidence_ref", "document_source",
                        "mapping_mode", "prerequisite_acceptance_ref", "correspondence_ref", "continuity_review_ref"}
            or lineage.get("schema_version") != 1 or lineage.get("kind") != "hr_catalog_relation_mapping"
            or lineage.get("mapping_mode") not in {"exact_native", "corrective_many_to_one"}
            or lineage.get("document_source") != candidate["source"]
            or lineage.get("candidate_sha256") != _digest(previous)
            or lineage.get("source") != previous["source"]
            or lineage.get("target_key") != dependency["target_key"]
            or lineage.get("target_fingerprint") != previous["target_fingerprint"]
            or lineage.get("database") != previous.get("database")):
        raise QueueManifestError("HR Catalog original prerequisite lineage is inconsistent")
    original_request = _artifact(lineage["original_catalog_request_ref"])
    original_response = _source_answer(lineage["original_catalog_response_ref"], "read_reference_by_native")
    _catalog_scope(original_request, original_response, namespace)
    # This is the original observation of the DOCUMENT'S source card, not a
    # requirement to replicate the canonical target prerequisite's source card.
    # Source cards can differ while an independently accepted target is shared.
    original = original_response.get("data", {})
    current = response["data"]
    if (original_request != request
            or original_response.get("sourceAlias") != response.get("sourceAlias")
            or any(original.get(field) != current.get(field) for field in (
                "kind", "type", "observedCatalogType", "catalogMetadataId", "nativeReference",
                "roundtripStatus", "stableIdentity", "snapshotStatus", "identityScope",
                "readContext", "reference", "attributes", "relations", "unsupported", "readStatus"))):
        raise QueueManifestError("HR Catalog original/current native source proof differs")
    verified = _artifact(lineage["verified_evidence_ref"])
    _artifact(lineage["prerequisite_acceptance_ref"])
    source_relation = {"source_alias": request["source_alias"], "source_namespace": namespace,
                       "catalog_type": request["type_name"], "catalog_metadata_id": request["catalog_metadata_id"],
                       "native_reference": request["native_reference"], "as_of": request["as_of"]}
    correspondence = _artifact(lineage["correspondence_ref"])
    if (set(correspondence) != {"schema_version", "status", "mapping_mode", "document_source", "source_relation",
                               "candidate_sha256", "target_key", "target_fingerprint", "database", "basis",
                               "explanation", "factual_evidence_refs", "prerequisite_acceptance_sha256", "verified_evidence_sha256"}
            or correspondence.get("schema_version") != 1
            or correspondence.get("status") != "accepted_hr_factual_correspondence"
            or correspondence.get("basis") not in {"independently_reviewed_historical_facts", "owner_correction_with_factual_review"}
            or not isinstance(correspondence.get("explanation"), str)
            or not 1 <= len(correspondence["explanation"].strip()) <= 4096
            or not isinstance(correspondence.get("factual_evidence_refs"), list)
            or not 1 <= len(correspondence["factual_evidence_refs"]) <= 16
            or correspondence.get("source_relation") != source_relation
            or any(correspondence.get(k) != lineage[k] for k in
                   ("mapping_mode", "document_source", "candidate_sha256", "target_key", "target_fingerprint", "database"))
            or correspondence.get("prerequisite_acceptance_sha256") != lineage["prerequisite_acceptance_ref"]["sha256"]
            or correspondence.get("verified_evidence_sha256") != _digest(verified)):
        raise QueueManifestError("HR factual source-to-target correspondence is missing or inconsistent")
    for ref in correspondence["factual_evidence_refs"]:
        _artifact(ref)
    continuity = _artifact(lineage["continuity_review_ref"])
    expected_continuity = {"schema_version": 1, "status": "accepted_hr_bounded_source_continuity",
                          "scope": "original-current-addressed-observations; global-lifetime-unverified",
                          "document_source": candidate["source"], "source_relation": source_relation,
                          "original_request_sha256": lineage["original_catalog_request_ref"]["sha256"],
                          "original_response_sha256": lineage["original_catalog_response_ref"]["sha256"],
                          "current_request_sha256": binding["catalog_request_ref"]["sha256"],
                          "current_response_sha256": binding["catalog_response_ref"]["sha256"],
                          "correspondence_sha256": lineage["correspondence_ref"]["sha256"],
                          "continuity_evidence_refs": continuity.get("continuity_evidence_refs")}
    refs = continuity.get("continuity_evidence_refs")
    if (continuity != expected_continuity or not isinstance(refs, list) or not 1 <= len(refs) <= 16):
        raise QueueManifestError("HR original/current source continuity review is missing or inconsistent")
    for ref in refs:
        _artifact(ref)
    return lineage


def validate_checkpoint_lineage(candidate: Mapping[str, Any], payload_field: str,
                                entry: Mapping[str, Any]) -> None:
    """Cross-check the original accepted evidence; never modify a Catalog entry."""
    try:
        matches = [b for b in candidate["source_evidence"]["relation_bindings"]
                   if b["payload_field"] == payload_field]
        if len(matches) != 1:
            raise QueueManifestError("HR Catalog prerequisite lineage missing")
        lineage = _artifact(matches[0]["catalog_lineage_ref"])
        verified = _artifact(lineage["verified_evidence_ref"])
        if (lineage["candidate_sha256"] != entry["candidate_sha256"]
                or lineage["source"] != entry["source"]
                or lineage["target_fingerprint"] != entry["target_fingerprint"]
                or lineage["target_key"] != entry["target_key"]
                or _digest(verified) != entry["evidence_sha256"]):
            raise QueueManifestError("HR Catalog evidence is not the accepted checkpoint prerequisite")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, QueueManifestError):
            raise
        raise QueueManifestError("HR Catalog checkpoint lineage shape invalid") from None


def native_document_line(data: Mapping[str, Any]) -> str:
    """New HR tuples use typed native identity, never header/cursor/employee ID."""
    return "native-document-" + _digest([
        data["sourceNamespace"], "document", data["documentMetadataId"],
        data["documentType"], data["nativeReference"],
    ])


def _scalar(data: dict, source_field: str, transform: str) -> Any:
    if source_field == "document.date":
        value = data["document"][source_field.split(".")[1]]
    elif source_field.startswith("attribute:"):
        value = data["attributes"].get(source_field[len("attribute:"):])
        if source_field[len("attribute:"):] not in data["attributes"]:
            raise QueueManifestError("HR scalar source field absent")
    else:
        raise QueueManifestError("HR scalar source field unsupported")
    if type(value) not in (str, int, float, bool, type(None)):
        raise QueueManifestError("HR scalar source value unsupported")
    if transform == "date_midnight":
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise QueueManifestError("HR date source needs an exact calendar day") from None
        return value + "T00:00:00"
    if transform != "exact":
        raise QueueManifestError("HR scalar transformation unsupported")
    return value


def _business_date_mapping(candidate: Mapping[str, Any], data: dict, binding: dict,
                           proof: dict, dates: dict) -> None:
    """One independently accepted header-to-business conversion; never creation."""
    witness = _artifact(binding["business_date_witness_ref"])
    identity = {"metadata_kind": "document", "source_namespace": data["sourceNamespace"],
                "document_type": data["documentType"], "document_metadata_id": data["documentMetadataId"],
                "native_reference": data["nativeReference"]}
    expected = {"schema_version": 1, "status": "accepted_hr_document_business_date_mapping",
                "document_source": candidate["source"], "document_identity": identity,
                "source_field": "document.date", "source_role": "document_date",
                "payload_field": binding["payload_field"], "target_role": "business_date",
                "transform": binding["transform"], "source_value_sha256": _digest(data["document"]["date"]),
                "prepared_sha256": _digest(candidate["prepared"]), "template": candidate["template"],
                "entity_set": candidate["prepared"]["entity_set"], "target_fingerprint": candidate["target_fingerprint"],
                "database": candidate.get("database"), "addressed_request_sha256": proof["addressed_request_ref"]["sha256"],
                "addressed_response_sha256": proof["addressed_response_ref"]["sha256"],
                "date_roles_sha256": dates["role_evidence_ref"]["sha256"],
                "origin_evidence_sha256": binding["origin_evidence_ref"]["sha256"]}
    refs = {"source_semantics_ref", "target_semantics_ref", "independent_review_ref"}
    if set(witness) != set(expected) | refs or any(witness.get(k) != v for k, v in expected.items()):
        raise QueueManifestError("HR Document business-date witness is missing or foreign")
    source = _artifact(witness["source_semantics_ref"])
    target = _artifact(witness["target_semantics_ref"])
    source_expected = {"schema_version": 1, "status": "accepted_hr_document_business_meaning",
                       "document_source": candidate["source"], "document_identity": identity,
                       "source_field": "document.date", "source_role": "document_date"}
    target_expected = {"schema_version": 1, "status": "accepted_hr_target_business_date_semantics",
                       "template": candidate["template"], "entity_set": candidate["prepared"]["entity_set"],
                       "target_fingerprint": candidate["target_fingerprint"], "database": candidate.get("database"),
                       "payload_field": binding["payload_field"], "date_role": "business_date",
                       "date_roles_sha256": dates["role_evidence_ref"]["sha256"]}
    for evidence, pinned in ((source, source_expected), (target, target_expected)):
        if (set(evidence) != set(pinned) | {"business_meaning", "evidence_refs"}
                or any(evidence.get(k) != v for k, v in pinned.items())
                or not isinstance(evidence.get("business_meaning"), str)
                or not 1 <= len(evidence["business_meaning"].strip()) <= 4096
                or not isinstance(evidence.get("evidence_refs"), list) or not 1 <= len(evidence["evidence_refs"]) <= 16):
            raise QueueManifestError("HR source/configuration business-date semantics are unproven")
        for ref in evidence["evidence_refs"]:
            _artifact(ref)
    if source["business_meaning"] != target["business_meaning"]:
        raise QueueManifestError("HR Document and target business-date meanings are ambiguous")
    review = _artifact(witness["independent_review_ref"])
    core = {k: v for k, v in witness.items() if k != "independent_review_ref"}
    if review != {"schema_version": 1, "status": "accepted_hr_document_business_date_mapping_review",
                  "mapping_sha256": _digest(core)}:
        raise QueueManifestError("HR Document business-date mapping review is missing or stale")


def _observed_dates(candidate: Mapping[str, Any], data: dict, proof: dict,
                    dates: dict, roles: dict, source_alias: str) -> dict[str, tuple[Any, Any]]:
    """Explicit metadata Date types or known semantic roles; never ISO/name guesses."""
    fields = set(roles["source_roles"])
    if "source_date_types_ref" in dates:
        typed = _artifact(dates["source_date_types_ref"])
        expected = {"schema_version": 1, "status": "accepted_hr_source_date_types",
                    "document_source": candidate["source"],
                    "addressed_request_sha256": proof["addressed_request_ref"]["sha256"],
                    "addressed_response_sha256": proof["addressed_response_ref"]["sha256"]}
        if (set(typed) != set(expected) | {"metadata_response_ref", "evidence_refs", "independent_review_ref"}
                or any(typed.get(k) != v for k, v in expected.items())
                or not isinstance(typed.get("evidence_refs"), list) or not 1 <= len(typed["evidence_refs"]) <= 16):
            raise QueueManifestError("HR source date type provenance is unverified")
        metadata = _source_answer(typed["metadata_response_ref"], "metadata_inventory")
        inventory = metadata.get("data", {})
        documents = inventory.get("documents")
        if (metadata.get("sourceAlias") != source_alias or inventory.get("status") != "metadata_catalog"
                or not isinstance(documents, list) or len(documents) > 10000):
            raise QueueManifestError("HR date metadata source context is unverified")
        matches = [d for d in documents if isinstance(d, dict)
                   and d.get("id") == data["documentMetadataId"] and d.get("name") == data["documentType"]]
        if len(matches) != 1 or not isinstance(matches[0].get("headAttributes"), list):
            raise QueueManifestError("HR date metadata must bind the exact typed Document")
        seen = set()
        for attr in matches[0]["headAttributes"]:
            if (not isinstance(attr, dict) or set(attr) != {"id", "name", "typeCode", "length", "precision", "refTypeId"}
                    or not isinstance(attr["name"], str) or not attr["name"].isidentifier() or attr["name"] in seen):
                raise QueueManifestError("HR source date metadata is ambiguous")
            seen.add(attr["name"])
            if attr["typeCode"] == "D" and attr["refTypeId"] == "0" and attr["name"] in data["attributes"]:
                fields.add("attribute:" + attr["name"])
        if not set(data["attributes"]) <= seen:
            raise QueueManifestError("HR date metadata omits addressed Document fields")
        for ref in typed["evidence_refs"]:
            _artifact(ref)  # Actual configuration/source-scope provenance, independently checked outside.
        review = _artifact(typed["independent_review_ref"])
        core = {k: v for k, v in typed.items() if k != "independent_review_ref"}
        if review != {"schema_version": 1, "status": "accepted_hr_source_date_types_review", "types_sha256": _digest(core)}:
            raise QueueManifestError("HR source date type scope needs independent review")
    observed = {}
    for field in fields:
        value = data["document"]["date"] if field == "document.date" else data["attributes"][field[10:]]
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise QueueManifestError("HR observed source date is not a supported scalar")
        observed[field] = (roles["source_roles"].get(field), value)
    return observed


def _date_gaps(candidate: Mapping[str, Any], data: dict, proof: dict,
               dates: dict, roles: dict, observed: dict) -> set[str]:
    refs = dates.get("unmapped_date_gaps", [])
    status = dates.get("inventory_status", "complete")
    if (not isinstance(refs, list) or len(refs) > 64
            or status != ("partial" if refs else "complete")):
        raise QueueManifestError("HR source date inventory must disclose partial unmapped facts")
    fields = set()
    for ref in refs:
        gap = _artifact(ref)
        field = gap.get("source_field")
        if not isinstance(field, str) or field == "document.date" or field not in observed or field in fields:
            raise QueueManifestError("HR unmapped date gap is missing or duplicates an observed fact")
        role, value = observed[field]
        expected = {"schema_version": 1, "status": "accepted_hr_unmapped_date_gap",
                    "document_source": candidate["source"], "source_field": field,
                    "date_role": role, "observed_value": value,
                    "addressed_request_sha256": proof["addressed_request_ref"]["sha256"],
                    "addressed_response_sha256": proof["addressed_response_ref"]["sha256"],
                    "date_roles_sha256": dates["role_evidence_ref"]["sha256"],
                    "prepared_sha256": _digest(candidate["prepared"]), "template": candidate["template"],
                    "entity_set": candidate["prepared"]["entity_set"], "target_fingerprint": candidate["target_fingerprint"],
                    "database": candidate.get("database"),
                    "partial_semantics": "retained-source-fact-not-written; mapping-and-phase-acceptance-pending"}
        if (set(gap) != set(expected) | {"reason", "explanation", "evidence_refs", "independent_review_ref"}
                or any(gap.get(k) != v for k, v in expected.items())
                or gap.get("reason") not in {"mapping_unavailable", "mapping_unresolved", "source_semantics_unknown"}
                or not isinstance(gap.get("explanation"), str) or not gap["explanation"].strip()
                or len(gap["explanation"]) > 4096
                or not isinstance(gap.get("evidence_refs"), list) or not 1 <= len(gap["evidence_refs"]) <= 16):
            raise QueueManifestError("HR unmapped date fact/context or partial disposition is unverified")
        for evidence in gap["evidence_refs"]:
            _artifact(evidence)
        review = _artifact(gap["independent_review_ref"])
        core = {k: v for k, v in gap.items() if k != "independent_review_ref"}
        if review != {"schema_version": 1, "status": "accepted_hr_unmapped_date_gap_review",
                      "gap_sha256": _digest(core)}:
            raise QueueManifestError("HR unmapped date gap needs independent factual review")
        fields.add(field)
    return fields


def validate_source(candidate: Mapping[str, Any]) -> None:
    try:
        _validate_source(candidate)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, QueueManifestError):
            raise
        raise QueueManifestError("HR source proof shape invalid") from None


def _validate_source(candidate: Mapping[str, Any]) -> None:
    proof = candidate.get("source_evidence")
    source_shape(proof)
    request = _artifact(proof["addressed_request_ref"])
    response = _source_answer(proof["addressed_response_ref"], "read_document_by_native")
    data = response.get("data")
    args = {"source_alias", "document_type", "document_metadata_id", "native_reference",
            "source_namespace", "attribute_names", "table_attribute_names", "line_cursor", "limit"}
    if (set(request) != args or not isinstance(data, dict)
            or not isinstance(request["attribute_names"], list)
            or not 1 <= len(request["attribute_names"]) <= 32
            or len(set(request["attribute_names"])) != len(request["attribute_names"])
            or not all(isinstance(f, str) and f.isidentifier() for f in request["attribute_names"])
            or type(request["limit"]) is not int or not 1 <= request["limit"] <= 100
            or response.get("sourceAlias") != request["source_alias"]
            or data.get("kind") != "document_by_native"
            or data.get("metadataKind") != "document"
            or data.get("documentType") != request["document_type"]
            or data.get("observedDocumentType") != request["document_type"]
            or data.get("documentMetadataId") != request["document_metadata_id"]
            or not isinstance(data.get("documentMetadataId"), str)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", data["documentMetadataId"])
            or data.get("sourceNamespace") != request["source_namespace"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(data.get("sourceNamespace")))
            or not isinstance(data.get("nativeReference"), str) or not data["nativeReference"].strip()
            or len(data["nativeReference"]) > 4096
            or data["nativeReference"] != request["native_reference"]
            or data.get("roundtripStatus") != "matched-typed-selected"
            or data.get("stableIdentity") != "unverified" or data.get("snapshotStatus") != "none"
            or data.get("identityScope") != _SCOPE
            or data.get("readStatus") != "complete-unverified"
            or data.get("unsupported") != [] or data.get("hasMore") is not False
            or data.get("lineCursor") != "0" or request["line_cursor"] != "0"
            or data.get("lineNumberSemantics") != "ordinal-traversal"
            or data.get("nextLineCursor") is not None
            or not isinstance(data.get("lines"), list)
            or not isinstance(data.get("attributes"), dict)
            or not isinstance(data.get("relations"), dict)
            or set(data["attributes"]) != set(request["attribute_names"])):
        raise QueueManifestError("HR addressed Document proof is incomplete or mismatched")
    document = data.get("document")
    if (not isinstance(document, dict) or document.get("valueKind") != "document"
            or document.get("selected") is not True or type(document.get("posted")) is not bool
            or any(document.get(k) != data[k] for k in
                   ("nativeReference", "documentMetadataId", "documentType"))
            or candidate["source"] != {"namespace": data["sourceNamespace"],
                    "statement": data["documentMetadataId"], "line": native_document_line(data),
                    "date": document.get("date")}):
        raise QueueManifestError("HR source tuple must retain its own typed native Document")
    # A scalar draft cannot silently discard source tabular history.
    if data["lines"] or data.get("declaredLineCount") != 0 or request["table_attribute_names"]:
        raise QueueManifestError("HR scalar draft requires a proven empty source table")
    payload = candidate["prepared"]["payload"]
    if any(field.casefold().replace("_", "") in _ASSIGNED_FIELDS for field in payload):
        raise QueueManifestError("HR target numbering and personnel codes must be assigned by the target")
    dates = _artifact(proof["date_inventory_ref"])
    date_keys = {"schema_version", "status", "document_source", "dates", "creation_date_status", "creation_date_source_field", "role_evidence_ref"}
    if (set(dates) not in tuple(date_keys | optional for optional in (
            set(), {"source_date_types_ref"}, {"unmapped_date_gaps", "inventory_status"},
            {"unmapped_date_gaps", "inventory_status", "source_date_types_ref"}))
            or dates.get("schema_version") != 1 or dates.get("status") != "accepted_hr_source_date_inventory"
            or dates.get("document_source") != candidate["source"]
            or dates.get("creation_date_status") not in {"observed", "unavailable", "not_present"}
            or not isinstance(dates.get("dates"), list) or not 1 <= len(dates["dates"]) <= 64):
        raise QueueManifestError("HR source date inventory is missing or inconsistent")
    date_pairs = []
    for item in dates["dates"]:
        if (not isinstance(item, dict) or set(item) != {"source_field", "payload_field"}
                or not all(isinstance(v, str) and v for v in item.values())):
            raise QueueManifestError("HR source date inventory shape invalid")
        date_pairs.append((item["source_field"], item["payload_field"]))
    roles = _artifact(dates["role_evidence_ref"])
    if (set(roles) != {"schema_version", "status", "document_source", "target_fingerprint", "template", "entity_set",
                      "source_roles", "target_roles", "evidence_refs"}
            or roles.get("schema_version") != 1 or roles.get("status") != "accepted_hr_date_roles"
            or any(roles.get(k) != candidate[k] for k in ("target_fingerprint", "template"))
            or roles.get("document_source") != candidate["source"]
            or roles.get("entity_set") != candidate["prepared"]["entity_set"]
            or not isinstance(roles.get("source_roles"), dict) or not 1 <= len(roles["source_roles"]) <= 64
            or not isinstance(roles.get("target_roles"), dict) or not 1 <= len(roles["target_roles"]) <= 64
            or not isinstance(roles.get("evidence_refs"), list) or not 1 <= len(roles["evidence_refs"]) <= 16
            or roles["source_roles"].get("document.date") != "document_date"
            or roles["target_roles"].get("Date") != "document_date"):
        raise QueueManifestError("HR source/target date roles need exact independent evidence")
    for name, role in roles["source_roles"].items():
        if (not isinstance(name, str) or role not in _DATE_ROLES
                or (name != "document.date" and (not name.startswith("attribute:") or name[10:] not in data["attributes"]))
                or (role == "document_date" and name != "document.date")):
            raise QueueManifestError("HR source date roles contradict addressed facts")
    for name, role in roles["target_roles"].items():
        if (not isinstance(name, str) or not name.isidentifier() or role not in _DATE_ROLES
                or (role == "document_date" and name != "Date")):
            raise QueueManifestError("HR target date roles contradict the document header")
    for ref in roles["evidence_refs"]:
        _artifact(ref)
    observed = _observed_dates(candidate, data, proof, dates, roles, request["source_alias"])
    gaps = _date_gaps(candidate, data, proof, dates, roles, observed)
    creation = dates["creation_date_source_field"]
    creation_sources = {name for name, role in roles["source_roles"].items() if role == "creation_date"}
    creation_targets = {name for name, role in roles["target_roles"].items() if role == "creation_date"}
    if (len(set(date_pairs)) != len(date_pairs) or ("document.date", "Date") not in date_pairs
            or (dates["creation_date_status"] == "observed" and
                (not isinstance(creation, str) or not creation.startswith("attribute:")
                 or creation_sources != {creation}
                 or (creation not in gaps and not any(s == creation and t in creation_targets for s, t in date_pairs))))
            or (dates["creation_date_status"] != "observed" and
                (creation is not None or creation_sources or creation_targets.intersection(payload)))):
        raise QueueManifestError("HR source creation date must be preserved or explicitly unavailable")
    date_binding = [b for b in proof["scalar_bindings"]
                    if isinstance(b, dict) and b.get("payload_field") == "Date"]
    if len(date_binding) != 1 or date_binding[0].get("source_field") != "document.date":
        raise QueueManifestError("HR Date must bind the addressed document header date")
    fields = set()
    for binding in proof["scalar_bindings"]:
        keys = {"payload_field", "source_field", "transform", "value_origin", "origin_evidence_ref", "date_role"}
        if (not isinstance(binding, dict) or set(binding) not in (keys, keys | {"business_date_witness_ref"})
                or not all(isinstance(binding[k], str) for k in ("payload_field", "source_field", "transform", "value_origin"))
                or binding.get("value_origin") not in {"business_fact", "source_date"}
                or binding["source_field"] == "document.number"
                or binding["source_field"].removeprefix("attribute:").casefold().replace("_", "") in _ASSIGNED_FIELDS):
            raise QueueManifestError("HR scalar binding shape invalid")
        origin = _artifact(binding["origin_evidence_ref"])
        if (set(origin) != {"schema_version", "status", "document_source", "source_field", "payload_field", "value_origin", "date_role", "date_roles_sha256", "evidence_refs"}
                or origin.get("schema_version") != 1 or origin.get("status") != "accepted_hr_scalar_origin"
                or origin.get("document_source") != candidate["source"]
                or origin.get("source_field") != binding["source_field"]
                or origin.get("payload_field") != binding["payload_field"]
                or origin.get("value_origin") != binding["value_origin"]
                or origin.get("date_role") != binding["date_role"]
                or origin.get("date_roles_sha256") != dates["role_evidence_ref"]["sha256"]
                or not isinstance(origin.get("evidence_refs"), list) or not 1 <= len(origin["evidence_refs"]) <= 16):
            raise QueueManifestError("HR scalar factual origin is missing or inconsistent")
        field, source_field = binding["payload_field"], binding["source_field"]
        role = binding["date_role"]
        business_conversion = (source_field == "document.date" and field != "Date" and role == "business_date"
                               and binding["value_origin"] == "source_date"
                               and roles["target_roles"].get(field) == "business_date")
        if business_conversion:
            _business_date_mapping(candidate, data, binding, proof, dates)
        elif "business_date_witness_ref" in binding:
            raise QueueManifestError("HR business-date witness cannot override another date role")
        if ((binding["value_origin"] == "source_date" and
             (role not in _DATE_ROLES or (roles["source_roles"].get(source_field) != role and not business_conversion)
              or roles["target_roles"].get(field) != role))
                or (binding["value_origin"] != "source_date" and
                    (role is not None or field in roles["target_roles"] or source_field in roles["source_roles"]))
                or (source_field == "document.date" and field != "Date" and not business_conversion)):
            raise QueueManifestError("HR source and target date roles must agree without substitution")
        for ref in origin["evidence_refs"]:
            _artifact(ref)
        value = _scalar(data, binding["source_field"], binding["transform"])
        if (field not in payload or field in fields or type(payload[field]) is not type(value)
                or payload[field] != value):
            raise QueueManifestError("HR payload does not match addressed source scalars")
        fields.add(field)
    if set(date_pairs) != {(b["source_field"], b["payload_field"]) for b in proof["scalar_bindings"] if b["value_origin"] == "source_date"}:
        raise QueueManifestError("HR payload must preserve the accepted source dates")
    mapped_dates = {s for s, _t in date_pairs}
    bound_sources = {b["source_field"] for b in proof["scalar_bindings"]}
    if bound_sources.intersection(gaps) or not set(observed) <= mapped_dates | gaps:
        raise QueueManifestError("HR observed filled dates need preserved bindings or reviewed partial gaps")
    employee = False
    prerequisites = {b["payload_field"]: b for b in candidate.get("prerequisites", [])}
    for binding in proof["relation_bindings"]:
        if (not isinstance(binding, dict) or set(binding) != {"payload_field", "source_field", "role",
                                                           "catalog_request_ref", "catalog_response_ref",
                                                           "catalog_lineage_ref"}
                or binding.get("role") not in {"employee", "reference"}):
            raise QueueManifestError("HR Catalog relation binding shape invalid")
        field, name = binding["payload_field"], binding["source_field"]
        dependency = prerequisites.get(field)
        relation = data["relations"].get(name)
        value = data["attributes"].get(name)
        if (field in fields or dependency is None or not field.endswith("_Key")
                or not isinstance(relation, dict) or not isinstance(value, dict)
                or relation.get("status") != "native-typed-unverified"
                or relation.get("metadataKind") != "reference"
                or relation.get("sourceNamespace") != data["sourceNamespace"]
                or relation.get("catalogType") != relation.get("observedCatalogType")
                or not isinstance(relation.get("nativeReference"), str)
                or not relation["nativeReference"].strip() or len(relation["nativeReference"]) > 4096
                or not isinstance(relation.get("catalogMetadataId"), str)
                or not re.fullmatch(r"[1-9][0-9]{0,9}", relation["catalogMetadataId"])
                or not isinstance(relation.get("catalogType"), str) or not relation["catalogType"].isidentifier()
                or value.get("valueKind") != "reference" or value.get("selected") is not True
                or value.get("empty") is not False
                or value.get("nativeIdentityStatus") != "unverified"
                or value.get("refTypeId") != relation.get("catalogMetadataId")
                or value.get("nativeReference") != relation.get("nativeReference")):
            raise QueueManifestError("HR source employee/reference relation is not proven")
        catalog_request = _artifact(binding["catalog_request_ref"])
        catalog_response = _source_answer(binding["catalog_response_ref"], "read_reference_by_native")
        cat = catalog_response.get("data", {})
        _catalog_scope(catalog_request, catalog_response, data["sourceNamespace"])
        if (catalog_request.get("source_alias") != request["source_alias"]
                or set(catalog_request) != {"source_alias", "type_name", "catalog_metadata_id",
                                            "native_reference", "as_of", "attribute_names", "source_namespace"}
                or catalog_request.get("type_name") != relation["catalogType"]
                or catalog_request.get("catalog_metadata_id") != relation["catalogMetadataId"]
                or catalog_request.get("native_reference") != relation["nativeReference"]
                or not isinstance(catalog_request.get("attribute_names"), list)
                or len(catalog_request["attribute_names"]) > 32
                or not all(isinstance(f, str) and f.isidentifier() for f in catalog_request["attribute_names"])
                or len(set(catalog_request["attribute_names"])) != len(catalog_request["attribute_names"])
                or catalog_request.get("as_of") != candidate["source"]["date"]
                or catalog_response.get("sourceAlias") != request["source_alias"]
                or cat.get("kind") != "reference_by_native"
                or cat.get("type") != relation["catalogType"]
                or cat.get("observedCatalogType") != relation["catalogType"]
                or cat.get("catalogMetadataId") != relation["catalogMetadataId"]
                or cat.get("nativeReference") != relation["nativeReference"]
                or cat.get("roundtripStatus") != "matched-typed-selected"
                or cat.get("readStatus") != "complete-unverified" or cat.get("unsupported") != []
                or cat.get("stableIdentity") != "unverified" or cat.get("snapshotStatus") != "none"
                or cat.get("identityScope") != _SCOPE
                or cat.get("readContext") != {"asOf": catalog_request["as_of"], "useDate": "applied",
                                              "selection": "FindItem", "periodicStatus": "date-context-applied-unverified"}
                or set(cat.get("attributes", {})) != set(catalog_request["attribute_names"])
                or cat.get("reference", {}).get("selected") is not True
                or cat["reference"].get("valueKind") != "reference"
                or cat["reference"].get("refTypeId") != relation["catalogMetadataId"]
                or cat["reference"].get("nativeIdentityStatus") != "unverified"
                or cat["reference"].get("nativeReference") != relation["nativeReference"]):
            raise QueueManifestError("HR Catalog native binding is inconsistent")
        _catalog_lineage(candidate, binding, dependency, data["sourceNamespace"], catalog_request, catalog_response)
        employee |= binding["role"] == "employee"
        fields.add(field)
    expected_fields = set(payload) - {"DeletionMark"}
    reference_fields = {f for f in payload if f.endswith("_Key")}
    if (not employee or fields != expected_fields
            or reference_fields != set(prerequisites)
            or reference_fields != {b["payload_field"] for b in proof["relation_bindings"]}):
        raise QueueManifestError("HR draft needs exact source fields and verified employee prerequisites")
    review = _artifact(proof["independent_review_ref"])
    expected = {"schema_version": 1, "status": "accepted_hr_source_binding",
                "source": candidate["source"], "prepared_sha256": _digest(candidate["prepared"]),
                "template": candidate["template"], "target_fingerprint": candidate["target_fingerprint"],
                "database": candidate.get("database"),
                "addressed_request_sha256": proof["addressed_request_ref"]["sha256"],
                "addressed_response_sha256": proof["addressed_response_ref"]["sha256"],
                "scalar_bindings_sha256": _digest(proof["scalar_bindings"]),
                "date_inventory_sha256": proof["date_inventory_ref"]["sha256"],
                "relation_bindings_sha256": _digest(proof["relation_bindings"]),
                "prerequisites_sha256": _digest(candidate["prerequisites"])}
    if review != expected:
        raise QueueManifestError("HR independent source review does not bind this draft")
