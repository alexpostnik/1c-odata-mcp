"""Offline atomic resolution of sealed original batch claims after audited PATCH.

This module sends no writes and emits no write arguments. Saved authority evidence
must be independently authenticated by the appointed migrator before execution.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
from typing import Mapping

from .ledger import request_digest
from .person_mapping import PERSON_ENTITY, NAME_FIELDS, canonical_birthday, mapped_person_payload
from .queue_manifest import QueueManifestError, _digest, _record_historical_result, read_private_json

PROFILE = "person-repair-v1"


def seal_source_bundle(plan, accepted_handoff):
    """Add refs to an existing sealed plan; hashes and sealing are not acceptance."""
    core = {"schema_version": 1, "plan_sha256": plan["plan_sha256"],
            "accepted_handoff": accepted_handoff}
    return {**core, "source_bundle_sha256": _digest(core)}


def _artifact_bytes(ref):
    if (not isinstance(ref, dict) or not isinstance(ref.get("path"), str)
            or not isinstance(ref.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])):
        raise QueueManifestError("repair source artifact reference invalid")
    raw = Path(ref["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise QueueManifestError("repair source artifact bytes differ")
    return raw


def _artifact(ref):
    return json.loads(_artifact_bytes(ref).decode("utf-8-sig"))


def _source_response(ref):
    saved = _artifact(ref)
    if (saved.get("is_error") is not False or len(saved["content"]) != 1
            or saved["content"][0].get("type") != "text"):
        raise QueueManifestError("repair source response envelope invalid")
    return json.loads(saved["content"][0]["text"])


def validate_source_bundle(plan, bundle, acceptance):
    """Check immutable the reviewed scenario artifacts, not a caller's repeated scalar assertions.

    The appointed independent factual reviewer must authenticate the two accepted
    digests and review reference outside this helper. This is offline consistency
    checking; a self-sealed bundle/ref cannot authenticate its own provenance.
    """
    try:
        core = {k: v for k, v in bundle.items() if k != "source_bundle_sha256"}
        if (set(core) != {"schema_version", "plan_sha256", "accepted_handoff"}
                or core["schema_version"] != 1 or core["plan_sha256"] != plan["plan_sha256"]
                or bundle["source_bundle_sha256"] != _digest(core)
                or acceptance.get("source_bundle_sha256") != bundle["source_bundle_sha256"]
                or acceptance.get("accepted_handoff_sha256") != core["accepted_handoff"]["sha256"]
                or not isinstance(acceptance.get("source_independent_review_ref"), str)
                or not acceptance["source_independent_review_ref"].strip()):
            raise QueueManifestError("repair authenticated source bundle acceptance missing")
        accepted = _artifact(core["accepted_handoff"])
        handoff = _artifact(accepted["privateExactHandoff"])
        convention = accepted["acceptedConventionCommit"]
        if (not isinstance(convention, str) or not convention
                or acceptance.get("accepted_convention_commit") != convention
                or handoff["convention"]["acceptedConventionCommit"] != convention
                or len(accepted["originalLineageDigests"]) != len(plan["bindings"]) or len(handoff["rows"]) != len(plan["bindings"])):
            raise QueueManifestError("repair accepted source convention/lineage invalid")
        # Retain the original independently reviewed source/archive/continuity refs.
        for field in ("separateCoordinatorAcceptance", "independentHandoffReview",
                      "independentActualSourceReview"):
            _artifact_bytes(accepted[field])
        seen_native = set()
        for binding in plan["bindings"]:
            candidate = binding["candidate"]
            matches = [r for r in handoff["rows"] if r["originalCandidateExactCopy"] == candidate]
            if len(matches) != 1:
                raise QueueManifestError("repair source original candidate differs")
            row = matches[0]
            lineage = [r for r in accepted["originalLineageDigests"]
                       if r["originalRecordedCandidateSHA256"] == _digest(candidate)]
            if len(lineage) != 1:
                raise QueueManifestError("repair source accepted candidate lineage differs")
            lineage = lineage[0]
            payload, source = candidate["prepared"]["payload"], candidate["source"]
            if (row["acceptedConventionCommit"] != convention
                    or row["originalRecordedCandidateSHA256"] != _digest(candidate)
                    or row["originalSourceExactCopy"] != source
                    or row["originalPreparedPayloadExactCopy"] != payload
                    or row["originalPreparedSourceIdExactCopy"] != candidate["prepared"]["source_id"]
                    or row["originalSourceTupleSHA256"] != _digest(source)
                    or row["originalPreparedPayloadSHA256"] != _digest(payload)
                    or lineage["originalSourceTupleSHA256"] != _digest(source)
                    or lineage["originalPayloadSHA256"] != _digest(payload)
                    or lineage["acquisitionBinding"] != row["existingAcquisitionBinding"]
                    or lineage["addressedBinding"] != row["existingAddressedBinding"]):
                raise QueueManifestError("repair source original tuple/payload lineage differs")
            for field in ("sourceRegistrationEvidenceRef", "sourceDescriptorEvidence",
                          "sourceConfigurationEvidenceRef", "sourceArchiveIntentEvidenceRef",
                          "independentAcceptanceEvidenceRef"):
                _artifact_bytes(row[field])
            acquire_request = _artifact(row["acquisitionRequestRef"])
            addressed_request = _artifact(row["addressedRequestRef"])
            acquisition = _source_response(row["acquisitionResponseRef"])
            addressed = _source_response(row["addressedResponseRef"])
            acquire_binding = _artifact(row["existingAcquisitionBinding"])
            addressed_binding = _artifact(row["existingAddressedBinding"])
            acquisition_hash = row["acquisitionResponseRef"]["sha256"]
            addressed_hash = row["addressedResponseRef"]["sha256"]
            if (binding["source_binding_sha256"] != addressed_hash
                    or row["acquisitionResponseSHA256"] != acquisition_hash
                    or row["addressedResponseSHA256"] != addressed_hash
                    or acquire_binding["actualResponseSHA256"] != acquisition_hash
                    or addressed_binding["acquisitionResponseSHA256"] != acquisition_hash
                    or addressed_binding["addressedResponseSHA256"] != addressed_hash
                    or acquire_request["argumentsSHA256"] != acquire_binding["argumentsSHA256"]):
                raise QueueManifestError("repair source acquisition/addressed digest differs")
            # Runtime dispatch revision precedes convention acceptance; their SHA
            # values need not be equal. Both request records are byte-bound above.
            if not all(isinstance(r.get("argumentsSHA256"), str)
                       and re.fullmatch(r"[0-9a-f]{64}", r["argumentsSHA256"])
                       for r in (acquire_request, addressed_request)):
                raise QueueManifestError("repair source dispatch argument digest invalid")
            native = row["nativeReferencePrivate"]
            catalog = row["catalogMetadataId"]
            # Cross-bind the recorded dispatch to the accepted row, rather than
            # accepting any byte-bound request with a syntactically valid hash.
            addressed_arguments = {
                "source_alias": row["sourceRegistrationNamespace"],
                "type_name": row["catalogType"],
                "catalog_metadata_id": catalog,
                "native_reference": native,
                "as_of": source["date"],
                "attribute_names": ["Фамилия", "Имя", "Отчество", "ЛичныйНомер", "СтраховойНом", "ДатаРожд"],
            }
            if addressed_request["argumentsSHA256"] != _digest(addressed_arguments):
                raise QueueManifestError("repair source addressed dispatch arguments differ")
            native_tuple = (row["sourceRegistrationNamespace"], catalog, native)
            if (not all(isinstance(v, str) and v for v in native_tuple)
                    or row["nativeTupleCanonicalSHA256"] != _digest(list(native_tuple))
                    or row["nativeReferenceSHA256"] != hashlib.sha256(native.encode("utf-8")).hexdigest()
                    or native_tuple in seen_native):
                raise QueueManifestError("repair source native tuples are not injective")
            seen_native.add(native_tuple)
            for provenance in (acquire_binding, addressed_binding):
                if (provenance["catalogMetadataId"] != catalog
                        or provenance["nativeReference"] != native
                        or provenance["originalSourceSHA256"] != _digest(source)
                        or provenance["preparedPayloadSHA256"] != _digest(payload)
                        or provenance["candidateRecordedSHA256"] != _digest(candidate)):
                    raise QueueManifestError("repair source native lineage differs")
            data = addressed["data"]
            acquired_rows = acquisition["data"]["rows"]
            acquired = [r for r in acquired_rows if r["nativeReference"] == native]
            if (addressed["status"] != "ok" or acquisition["status"] != "ok"
                    or data["kind"] != "reference_by_native"
                    or data["roundtripStatus"] != "matched-typed-selected"
                    or data["readStatus"] != "complete-unverified"
                    or data["reference"]["valueKind"] != "reference"
                    or data["reference"]["serializationStatus"] != "structural-unverified"
                    or data["readContext"]["useDate"] != "applied"
                    or data["readContext"]["selection"] != "FindItem"
                    or data["readContext"]["periodicStatus"] != "date-context-applied-unverified"
                    or len(acquired) != 1 or acquired[0]["isGroup"] is not False
                    or data["nativeReference"] != native
                    or data["catalogMetadataId"] != catalog
                    or data["type"] != row["catalogType"]
                    or acquisition["data"]["type"] != row["catalogType"]
                    or data["reference"]["nativeReference"] != native
                    or data["reference"]["refTypeId"] != catalog
                    or data["reference"]["selected"] is not True
                    or data["reference"]["isGroup"] is not False
                    or data["unsupported"] != []
                    or data["readContext"] != row["dateContextEvidence"]["actualReadContext"]
                    or data["readContext"]["asOf"] != source["date"]
                    or row["sourceOriginalAsOf"] != source["date"]
                    or addressed_binding["asOf"] != source["date"]):
                raise QueueManifestError("repair source addressed native/date roundtrip differs")
            attrs = data["attributes"]
            parts = [attrs[f] for f in ("Фамилия", "Имя", "Отчество")]
            if (set(attrs) != {"Фамилия", "Имя", "Отчество", "ЛичныйНомер", "СтраховойНом", "ДатаРожд"}
                    or parts != binding["source_parts"]
                    or attrs["ЛичныйНомер"] != payload["ДокументЛичныйНомер"]
                    or attrs["СтраховойНом"] != payload["СтраховойНомерПФР"]
                    or canonical_birthday(attrs["ДатаРожд"]) != canonical_birthday(payload["ДатаРождения"])
                    or mapped_person_payload(payload, parts) != binding["desired_payload"]):
                raise QueueManifestError("repair source original scalar values differ")
        return {"source_bundle_sha256": bundle["source_bundle_sha256"], "verified_rows": len(plan["bindings"])}
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise QueueManifestError("repair source artifacts missing, invalid or inconsistent") from None



# A case-specific appendix is separate from the immutable original plan. Neither
# its checksum nor the external-reference assertions authenticate authority.
SERVICE_EFFECT_FIELD = "ИдентификаторСлужебный"


def effect_row1_pins(proof):
    return {"evidence_sha256": _digest(proof), "full_before_sha256": _digest(proof["before"]),
            "full_after_sha256": _digest(proof["after"]),
            "receipt_sha256": _digest(proof["patch_receipt"]),
            "request_hash": proof["patch_receipt"]["request_hash"],
            "operation_id": proof["operation_id"]}


def seal_effect_appendix(plan, source_bundle, acceptance, *, owner_decision_ref,
                         observed_review_ref, observed_row1, expires_at):
    """Seal only this exact batch effect scope; never returns PATCH/dispatch arguments.

    Authenticate actual owner decision and independent factual review OUTSIDE
    this helper before use. Sealing, arbitrary refs or hashes are not a grant.
    No source-plan reseal is needed or permitted for this additive appendix.
    """
    core = {"schema_version": 1, "profile": PROFILE, "plan_sha256": plan["plan_sha256"],
            "source_bundle_sha256": source_bundle["source_bundle_sha256"],
            "checkpoint_sha256": plan["checkpoint_sha256"], "target_kind": "test",
            **{f: acceptance[f] for f in ("target_fingerprint", "database",
                "writer_registration_receipt", "ledger_namespace_receipt")},
            "field": SERVICE_EFFECT_FIELD, "owner_decision_ref": owner_decision_ref,
            "observed_review_ref": observed_review_ref,
            "observed_row1": effect_row1_pins(observed_row1), "expires_at": expires_at,
            "formula_and_downstream": "UNKNOWN_ACCEPTED_ONLY_EXACT_BATCH",
            "rows": [{"candidate_sha256": _digest(b["candidate"]), "target_key": b["target_key"],
                      "mode": "observed_no_dispatch" if i == 0 else "remaining_name_patch"}
                     for i, b in enumerate(plan["bindings"])]}
    return {**core, "effect_appendix_sha256": _digest(core)}


def _effect_structure(plan, source_bundle, acceptance, appendix, observed_row1):
    """Pure history validation: no private artifact reads and no expiry gate."""
    try:
        expected = seal_effect_appendix(plan, source_bundle, acceptance,
            owner_decision_ref=appendix["owner_decision_ref"],
            observed_review_ref=appendix["observed_review_ref"],
            observed_row1=observed_row1, expires_at=appendix["expires_at"])
        if _digest(appendix) != _digest(expected) or len(appendix["rows"]) != len(plan["bindings"]):
            raise QueueManifestError("repair effect appendix exact case binding invalid")
        _time(appendix["expires_at"])
        for field in ("owner_decision_ref", "observed_review_ref"):
            ref = appendix[field]
            if (set(ref) != {"path", "sha256"} or not isinstance(ref["path"], str)
                    or not ref["path"].strip() or not isinstance(ref["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])):
                raise QueueManifestError("repair effect external reference invalid")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise QueueManifestError("repair effect appendix shape invalid") from None


def validate_effect_appendix(plan, source_bundle, acceptance, appendix, observed_row1):
    """Check actual saved decision/review bytes; caller authenticates provenance.

    Validates saved row1 only. It does not authorize or construct another row1
    operation. New effects additionally require unexpired external TEST authority.
    """
    try:
        _effect_structure(plan, source_bundle, acceptance, appendix, observed_row1)
        owner = _artifact(appendix["owner_decision_ref"])
        review = _artifact(appendix["observed_review_ref"])
        pins = appendix["observed_row1"]
        if (owner["original_plan_sha256"] != plan["plan_sha256"]
                or owner["source_bundle_sha256"] != source_bundle["source_bundle_sha256"]
                or owner["field"] != SERVICE_EFFECT_FIELD
                or owner["exact_row1_observed_before_after_effect_accepted"] is not True
                or owner["row1_PATCH_repeat_authorized"] is not False
                or owner["remaining_rows_authorized"] != [b["candidate"]["prepared"]["source_id"] for b in plan["bindings"][1:]]
                or owner["owner_accepts_exact_case_unknown_semantics"] is not True
                or owner["formula_and_downstream_unknown_explicitly_disclosed_before_answer"] is not True
                or owner["actual_row1_evidence_hashes"] != {k: pins[k] for k in (
                    "full_before_sha256", "full_after_sha256", "receipt_sha256", "request_hash")}
                or owner["independent_fact_review_sha256"] != appendix["observed_review_ref"]["sha256"]
                or any(review["row1"][k] != pins[k] for k in (
                    "full_before_sha256", "full_after_sha256", "receipt_sha256", "request_hash"))
                or any(review["row1"]["checks"][k] is not True for k in (
                    "independent_beforeafterreceipt_exact", "full_field_set_exact",
                    "only_observed_service_extra", "all_other_fields_exact_recursive_typed"))
                or review["formula_and_downstream_effects"] != "UNKNOWN"):
            raise QueueManifestError("repair effect owner/independent observed acceptance differs")
        _effect_remainder(observed_row1["before"], observed_row1["after"], allow_service=True)
        if observed_row1["before"][SERVICE_EFFECT_FIELD] == observed_row1["after"][SERVICE_EFFECT_FIELD]:
            raise QueueManifestError("repair appendix requires exact observed row1 additional effect")
        return {"effect_appendix_sha256": appendix["effect_appendix_sha256"],
                "observed_row1_no_dispatch": True, "rows": len(plan["bindings"])}
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise QueueManifestError("repair effect authority artifacts missing or inconsistent") from None


def _effect_remainder(before, after, *, allow_service=False):
    # Key sets must match even for the one accepted extra effect. Digest equality
    # preserves JSON scalar types (Python True == 1 must not bypass the guard).
    exclusions = {*NAME_FIELDS, "DataVersion"}
    if allow_service:
        if (set(before) != set(after) or SERVICE_EFFECT_FIELD not in before
                or not isinstance(before[SERVICE_EFFECT_FIELD], str)
                or not isinstance(after[SERVICE_EFFECT_FIELD], str)):
            raise QueueManifestError("repair effect field set/type differs")
        exclusions.add(SERVICE_EFFECT_FIELD)
    unchanged = lambda row: {k: v for k, v in row.items() if k not in exclusions}
    if _digest(unchanged(before)) != _digest(unchanged(after)):
        raise QueueManifestError("repair changed unrelated full readback fields")


def _effect_acceptance_structure(appendix, evidence, effect_acceptance):
    try:
        if (set(effect_acceptance) != {"schema_version", "effect_appendix_sha256",
                "owner_decision_sha256", "observed_review_sha256", "independent_effect_review_ref", "expires_at"}
                or type(effect_acceptance["schema_version"]) is not int or effect_acceptance["schema_version"] != 1
                or effect_acceptance["effect_appendix_sha256"] != appendix["effect_appendix_sha256"]
                or effect_acceptance["owner_decision_sha256"] != appendix["owner_decision_ref"]["sha256"]
                or effect_acceptance["observed_review_sha256"] != appendix["observed_review_ref"]["sha256"]):
            raise QueueManifestError("repair exact all rows effect acceptance invalid")
        _time(effect_acceptance["expires_at"])
        ref = effect_acceptance["independent_effect_review_ref"]
        if (set(ref) != {"path", "sha256"} or not isinstance(ref["path"], str)
                or not ref["path"].strip() or not isinstance(ref["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])):
            raise QueueManifestError("repair all rows independent effect review reference invalid")
        if (len(evidence) != len(appendix["rows"]) or any(p.get("candidate_sha256") != r["candidate_sha256"]
                or p.get("target_key") != r["target_key"] for r, p in zip(appendix["rows"], evidence))):
            raise QueueManifestError("repair effect evidence must be exact ordered batch")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise QueueManifestError("repair effect acceptance shape invalid") from None


def validate_effect_acceptance(appendix, evidences, effect_acceptance):
    """All rows factual acceptance is separate; authenticate reviewer outside helper."""
    try:
        _effect_acceptance_structure(appendix, evidences, effect_acceptance)
        reviewed = _artifact(effect_acceptance["independent_effect_review_ref"])
        expected = {"schema_version": 1, "status": "accepted_exact_batch_effects",
            "effect_appendix_sha256": appendix["effect_appendix_sha256"],
            "evidence_sha256": _digest(evidences), "rows": [
                {"candidate_sha256": r["candidate_sha256"], "target_key": r["target_key"],
                 "evidence_sha256": _digest(p), "field": SERVICE_EFFECT_FIELD,
                 "full_field_set_unchanged": True, "all_other_fields_unchanged": True}
                for r, p in zip(appendix["rows"], evidences)]}
        if _digest(reviewed) != _digest(expected):
            raise QueueManifestError("repair independent all rows effect evidence differs")
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise QueueManifestError("repair all rows effect review artifacts missing or inconsistent") from None


def repair_resolution_core(plan, acceptance, evidences, source_bundle,
                           effect_appendix=None, effect_acceptance=None):
    core = {"plan": plan, "acceptance": acceptance, "evidence": evidences, "source_bundle": source_bundle}
    if effect_appendix is not None or effect_acceptance is not None:
        if effect_appendix is None or effect_acceptance is None:
            raise QueueManifestError("repair effect appendix and acceptance required together")
        core.update(effect_appendix=effect_appendix, effect_acceptance=effect_acceptance)
    return core


def seal_plan(checkpoint, bindings):
    """Seal private, exact original-batch bindings; sealing is NOT acceptance."""
    core = {"schema_version": 1, "profile": PROFILE,
            "checkpoint_sha256": checkpoint["index_sha256"], "bindings": bindings}
    return {**core, "plan_sha256": _digest(core)}


def _time(value):
    if not isinstance(value, str):
        raise QueueManifestError("repair evidence needs UTC observation times")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QueueManifestError("repair observation time invalid") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise QueueManifestError("repair times must be UTC")
    return parsed



def _receipt_time(value):
    # WriteLedger.created_at is SQLite CURRENT_TIMESTAMP: UTC, seconds, no zone.
    # Accept ONLY that exact stored format, not arbitrary naive timestamps.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value):
        return _time(value.replace(" ", "T") + "Z")
    return _time(value)


def _receipt_between(before_value, receipt_value, after_value):
    before, after = _time(before_value), _time(after_value)
    receipt = _receipt_time(receipt_value)
    # SQLite CURRENT_TIMESTAMP represents an interval [second, second + 1s).
    # An addressed before read and PATCH may complete within that same second.
    sqlite_seconds = isinstance(receipt_value, str) and bool(re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", receipt_value))
    lower = before.replace(microsecond=0) if sqlite_seconds else before
    return before <= after <= datetime.now(timezone.utc) and lower <= receipt <= after

def resolve_person_repair(path, plan, acceptance, evidences, *, source_bundle,
                          effect_appendix=None, effect_acceptance=None):
    """Check full provenance then terminalize the batch atomically, retaining audit."""
    # Lazy import avoids a cycle in the checkpoint CLI.
    from .queue_checkpoint import (_locked, _read, _write, _candidate, _checkpoint_key,
                                  _claim, _target_conflicts, _catalog_fields, _matches)
    try:
        core = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if (set(core) != {"schema_version", "profile", "checkpoint_sha256", "bindings"}
                or core["schema_version"] != 1 or core["profile"] != PROFILE
                or plan["plan_sha256"] != _digest(core)
                or not isinstance(core["bindings"], list) or not core["bindings"]
                or not isinstance(evidences, list) or len(evidences) != len(core["bindings"])):
            raise QueueManifestError("repair requires sealed exact original-batch the reviewed scenario plan")
        if (acceptance.get("plan_sha256") != plan["plan_sha256"]
                or acceptance.get("target_kind") != "test"
                or not all(isinstance(acceptance.get(field), str) and acceptance[field].strip()
                           for field in ("correction_authority_ref", "independent_review_ref",
                                         "writer_registration_receipt", "ledger_namespace_receipt"))):
            raise QueueManifestError("exact plan acceptance/TEST authority/continuity missing")
        validate_source_bundle(plan, source_bundle, acceptance)
        expires_at = _time(acceptance["expires_at"])
        resolution_core = repair_resolution_core(plan, acceptance, evidences, source_bundle,
                                                effect_appendix, effect_acceptance)
        if effect_appendix is not None:
            validate_effect_appendix(plan, source_bundle, acceptance, effect_appendix, evidences[0])
            validate_effect_acceptance(effect_appendix, evidences, effect_acceptance)
        resolution = _digest(resolution_core)
        destination = Path(path)
        with _locked(destination):
            if read_private_json(destination).get("schema_version") != 4:
                raise QueueManifestError("repair requires existing schema4; no migration/reset")
            index = _read(destination)
            original_index_digest = index.get("index_sha256")
            new_entries = {}
            audits = []
            seen_keys, seen_targets, seen_operations = set(), set(), set()
            for binding, proof in zip(plan["bindings"], evidences):
                candidate = _candidate(binding["candidate"])
                manifest = binding["manifest"]
                claim = _claim(index, manifest["batch_id"], manifest)
                key = _checkpoint_key(index, candidate)
                target_key = binding["target_key"]
                if (candidate.get("workflow") != "catalog_prerequisites"
                        or candidate["prepared"]["entity_set"] != PERSON_ENTITY
                        or len(manifest["rows"]) != 1 or len(claim["rows"]) != 1
                        or claim["rows"][0]["key"] != key
                        or claim["rows"][0]["candidate_sha256"] != _digest(candidate)
                        or candidate["target_fingerprint"] != acceptance.get("target_fingerprint")
                        or candidate.get("database") != acceptance.get("database")
                        or key in seen_keys or target_key in seen_targets):
                    raise QueueManifestError("repair original source/claim/target binding invalid")
                seen_keys.add(key); seen_targets.add(target_key)
                desired = mapped_person_payload(candidate["prepared"]["payload"], binding["source_parts"])
                if (binding["desired_payload"] != desired
                        or not isinstance(binding.get("source_binding_sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", binding["source_binding_sha256"])
                        or any(len(part) != 28 for part in binding["source_parts"])):
                    raise QueueManifestError("repair source-backed projection differs")
                original_result = binding["original_result"]
                original_receipt = _record_historical_result(manifest, original_result)
                saved = original_result.get("result", {})
                if (original_result.get("state") != "complete" or saved.get("key") != target_key
                        or saved.get("entitySet") != PERSON_ENTITY
                        or saved.get("writeState") not in {"accepted", "accepted_unverified", "reconciled_present"}
                        or manifest["rows"][0]["payload"] != candidate["prepared"]["payload"]
                        or manifest["rows"][0]["source_id"] != candidate["prepared"]["source_id"]
                        or manifest.get("target_fingerprint") != candidate["target_fingerprint"]
                        or manifest.get("template") != candidate["template"]
                        or manifest.get("database") != candidate.get("database")
                        or original_result.get("database") != candidate.get("database")):
                    raise QueueManifestError("repair original Create receipt/manifest invalid")
                before, after = proof["before"], proof["after"]
                failed = binding["failed_readback"]
                if (proof.get("candidate_sha256") != _digest(candidate)
                        or proof.get("target_key") != target_key
                        or proof.get("readback_context") != {
                            "entity_set": PERSON_ENTITY, "target_fingerprint": candidate["target_fingerprint"],
                            "database": candidate.get("database")}
                        or any(row.get("Ref_Key") != target_key or row.get("DeletionMark") is not False
                               for row in (failed, before, after))
                        or any(before.get(field) != failed.get(field)
                               or failed.get(field) != candidate["prepared"]["payload"][field][:50]
                               or len(failed.get(field, "")) != 50 for field in NAME_FIELDS)
                        or _matches(candidate["prepared"]["payload"], failed)):
                    raise QueueManifestError("repair failed/fresh readback context invalid")
                for field in ("ДокументЛичныйНомер", "СтраховойНомерПФР"):
                    if any(row.get(field) != desired[field] for row in (failed, before, after)):
                        raise QueueManifestError("repair identifier mismatch")
                if any(canonical_birthday(row.get("ДатаРождения")) != desired["ДатаРождения"]
                       for row in (failed, before, after)):
                    raise QueueManifestError("repair birthday is not zero-only equivalent")
                if any(after.get(field) != desired[field] for field in NAME_FIELDS):
                    raise QueueManifestError("repair full name mismatch")
                # Full readback mappings: only the two names and version may differ.
                _effect_remainder(before, after, allow_service=effect_appendix is not None)
                version = proof["expected_data_version"]
                if (not isinstance(version, str) or not version or version == "*"
                        or before.get("DataVersion") != version
                        or not isinstance(after.get("DataVersion"), str)
                        or not after["DataVersion"] or after["DataVersion"] in {version, "*"}):
                    raise QueueManifestError("repair needs exact fresh DataVersion/If-Match")
                operation = proof["operation_id"]
                if (not isinstance(operation, str) or len(operation) < 8 or operation in seen_operations
                        or len(operation) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in operation)
                        or any(operation == row["operation_id"] for c in index["claims"].values()
                               for row in c["manifest"]["rows"])):
                    raise QueueManifestError("repair operation ID collides or invalid")
                seen_operations.add(operation)
                patch_payload = {field: desired[field] for field in NAME_FIELDS}
                patch = proof["patch_receipt"]
                result = patch.get("result", {})
                if (patch.get("found") is not True or patch.get("operationId") != operation
                        or patch.get("state") != "complete"
                        or patch.get("database") != candidate.get("database")
                        or patch.get("request_hash") != request_digest(candidate["target_fingerprint"],
                            "patch", PERSON_ENTITY, target_key, patch_payload, version)
                        or result.get("entitySet") != PERSON_ENTITY or result.get("key") != target_key
                        or result.get("readbackFetched") is not True
                        or result.get("readbackVerified") is not True
                        or not _receipt_between(proof["before_observed_at"], patch["created_at"],
                                                proof["after_observed_at"])):
                    raise QueueManifestError("repair PATCH receipt/fresh observation binding invalid")
                if _target_conflicts(index, candidate, key, target_key):
                    raise QueueManifestError("repair target conflicts with another source")
                audit = {"resolution_sha256": resolution, **resolution_core, "original_claim": claim if claim["stage"] != "resolved"
                         else claim["repair_audit"]["original_claim"], "original_result_receipt": original_receipt,
                         "original_entry": index["entries"][key] if claim["stage"] != "resolved"
                         else claim["repair_audit"]["original_entry"]}
                if claim["stage"] == "resolved":
                    if claim.get("repair_audit") != audit:
                        raise QueueManifestError("repair terminal evidence differs")
                elif (claim["stage"] != "dispatch_uncertain"
                        or index["entries"][key]["candidate_sha256"] != _digest(candidate)
                        or index["entries"][key]["evidence_sha256"] != claim["rows"][0]["evidence_sha256"]
                        or index["entries"][key]["status"] != "missing_verified"):
                    raise QueueManifestError("repair requires original dispatched uncertain claim")
                audits.append((claim, audit))
                new_entries[key] = {"source": candidate["source"], "candidate_sha256": _digest(candidate),
                    "target_fingerprint": candidate["target_fingerprint"], "status": "newly_created_verified",
                    "reason": "verified_after_audited_person_repair", "target_key": target_key,
                    "evidence_sha256": _digest(audit), **_catalog_fields(candidate, key)}
            if all(claim["stage"] == "resolved" for claim, _ in audits):
                return {"status": "resolved", "repaired_original_claims": len(plan["bindings"]), "plan_sha256": plan["plan_sha256"]}
            if effect_appendix is not None and any(_time(v["expires_at"]) <= datetime.now(timezone.utc)
                    for v in (effect_appendix, effect_acceptance)):
                raise QueueManifestError("repair effect appendix/acceptance expired")
            if expires_at <= datetime.now(timezone.utc):
                raise QueueManifestError("exact plan acceptance/TEST authority expired")
            if any(claim["stage"] == "resolved" for claim, _ in audits) or original_index_digest != plan["checkpoint_sha256"]:
                raise QueueManifestError("repair checkpoint changed; revalidate and reaccept exact plan")
            # Capture original state without a self-reference before mutating claims.
            import copy
            audits = [(claim, copy.deepcopy(audit)) for claim, audit in audits]
            index["entries"].update(new_entries)
            for claim, audit in audits:
                claim["stage"] = "resolved"
                claim["resolution_sha256"] = resolution
                claim["repair_audit"] = audit
            _write(destination, index)
            return {"status": "resolved", "repaired_original_claims": len(plan["bindings"]), "plan_sha256": plan["plan_sha256"]}
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, QueueManifestError):
            raise
        raise QueueManifestError("repair evidence shape or mapping invalid") from None
