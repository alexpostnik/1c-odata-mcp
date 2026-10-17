"""Synthetic the reviewed scenario width preflight and original-three audited recovery regression."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from onec_odata_mcp.person_mapping import (
    compact_name, canonical_birthday, mapped_person_payload, preflight_person_names,
)
from onec_odata_mcp.person_repair import seal_plan, seal_source_bundle, resolve_person_repair, validate_source_bundle
from onec_odata_mcp.ledger import request_digest
from onec_odata_mcp.queue_checkpoint import (
    _read, _write, _locked, _key, prepare_claim, preview_claim, resolve_claim, screen_rows,
)
from onec_odata_mcp.queue_manifest import QueueManifestError, _digest
from test_catalog_prerequisites import candidate, dispatch, server_receipt, found


def make_case(tmp_path, count=3):
    path = tmp_path / "private" / "checkpoint.json"
    parts = [p.ljust(28) for p in ("Синтетический", "Пример", "Тестович")]
    raw = " ".join(parts)
    payload = {"Description": raw, "ФИО": raw, "ДатаРождения": "1980-01-02T00:00:00.0000000",
               "ДокументЛичныйНомер": "SYNTHETIC-ID", "СтраховойНомерПФР": "SYNTHETIC-INSURANCE"}
    bindings, evidences = [], []
    now = datetime.now(timezone.utc)
    # Reproduce the ORIGINAL accepted helper behavior, before this preflight fix.
    # Dispatch compact stand-in then rebind synthetic original with sealed manifests.
    import onec_odata_mcp.queue_checkpoint as checkpoint_module
    import onec_odata_mcp.person_mapping as mapping_module
    original_guard = checkpoint_module.preflight_person_names
    original_mapping_guard = mapping_module.preflight_person_names
    checkpoint_module.preflight_person_names = lambda *args: None
    mapping_module.preflight_person_names = lambda *args: None
    try:
        for i in range(count):
            row = candidate(str(i + 1), payload=copy.deepcopy(payload))
            manifest, _ = dispatch(path, row, batch=f"original-{i}")
            key = f"00000000-0000-0000-0000-{i+1:012d}"
            receipt = server_receipt(manifest, key=key)
            receipt["result"]["readbackVerified"] = False
            failed = {**payload, "Description": raw[:50], "ФИО": raw[:50],
                      "ДатаРождения": "1980-01-02T00:00:00", "Ref_Key": key,
                      "DeletionMark": False, "DataVersion": "historical-version"}
            desired = mapped_person_payload(payload, parts)
            binding = {"candidate": row, "manifest": manifest, "original_result": receipt,
                       "failed_readback": failed, "source_parts": parts,
                       "source_binding_sha256": _digest({"synthetic": i}),
                       "target_key": key, "desired_payload": desired}
            before = {**failed, "DataVersion": f"fresh-{i}"}
            after = {**failed, **desired, "DataVersion": f"after-{i}"}
            operation = f"repair-operation-{i}"
            patch_receipt = {"found": True, "state": "complete", "operationId": operation,
                "request_hash": request_digest(row["target_fingerprint"], "patch",
                    row["prepared"]["entity_set"], key,
                    {field: desired[field] for field in ("Description", "ФИО")}, before["DataVersion"]),
                "created_at": (now - timedelta(minutes=2)).isoformat(),
                "result": {"entitySet": row["prepared"]["entity_set"], "key": key,
                           "readbackFetched": True, "readbackVerified": True}}
            evidences.append({"candidate_sha256": _digest(row), "target_key": key,
                "before": before, "after": after, "patch_receipt": patch_receipt,
                "operation_id": operation, "expected_data_version": before["DataVersion"],
                "before_observed_at": (now - timedelta(minutes=3)).isoformat(),
                "after_observed_at": (now - timedelta(minutes=1)).isoformat(),
                "readback_context": {"entity_set": row["prepared"]["entity_set"],
                                     "target_fingerprint": row["target_fingerprint"], "database": None}})
            bindings.append(binding)
        bank = candidate("bank")
        bank["source"]["namespace"] = "synthetic-unrelated-held"
        dispatch(path, bank, batch="bank-hold")
    finally:
        checkpoint_module.preflight_person_names = original_guard
        mapping_module.preflight_person_names = original_mapping_guard
    plan = seal_plan(json.loads(path.read_text()), bindings)
    acceptance = {"plan_sha256": plan["plan_sha256"], "target_kind": "test",
        "target_fingerprint": bindings[0]["candidate"]["target_fingerprint"], "database": None,
        "correction_authority_ref": "synthetic-explicit-TEST-grant",
        "independent_review_ref": "synthetic-review", "writer_registration_receipt": "synthetic-registration",
        "ledger_namespace_receipt": "synthetic-ledger",
        "expires_at": (now + timedelta(hours=1)).isoformat()}
    bundle = make_source_bundle(tmp_path, plan)
    acceptance.update({"plan_sha256": plan["plan_sha256"],
                       "source_bundle_sha256": bundle["source_bundle_sha256"],
                       "accepted_handoff_sha256": bundle["accepted_handoff"]["sha256"],
                       "accepted_convention_commit": "synthetic-convention-commit",
                       "source_independent_review_ref": "synthetic-independent-factual-acceptance"})
    return path, plan, acceptance, evidences, bundle



def make_source_bundle(tmp_path, plan):
    """Entirely synthetic immutable the reviewed scenario chain, with real raw-byte digests."""
    import hashlib
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    def save(name, value):
        path = root / (name + ".json")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    def response(data):
        return {"is_error": False, "content": [{"type": "text", "text": json.dumps({"status": "ok", "data": data})}]}
    common = save("independent-factual-acceptance", {"synthetic": True})
    rows, lineage = [], []
    for i, binding in enumerate(plan["bindings"]):
        candidate = binding["candidate"]
        payload, source = candidate["prepared"]["payload"], candidate["source"]
        native, catalog = f"SYNTHETIC-OPAQUE-TOKEN-{i}", "SYNTHETIC-CATALOG-ID"
        acquisition = save(f"acquisition-{i}", response({"type": "synthetic-catalog-type", "rows": [
            {"nativeReference": native, "isGroup": False}]}))
        attrs = dict(zip(("Фамилия", "Имя", "Отчество"), binding["source_parts"]))
        attrs.update({"ЛичныйНомер": payload["ДокументЛичныйНомер"],
                      "СтраховойНом": payload["СтраховойНомерПФР"],
                      "ДатаРожд": payload["ДатаРождения"]})
        read_context = {"asOf": source["date"], "useDate": "applied", "selection": "FindItem",
                        "periodicStatus": "date-context-applied-unverified"}
        addressed = save(f"addressed-{i}", response({"nativeReference": native, "catalogMetadataId": catalog,
            "type": "synthetic-catalog-type", "kind": "reference_by_native", "roundtripStatus": "matched-typed-selected",
            "readStatus": "complete-unverified", "reference": {"nativeReference": native, "refTypeId": catalog,
                "selected": True, "isGroup": False, "valueKind": "reference", "serializationStatus": "structural-unverified"},
            "unsupported": [], "readContext": read_context, "attributes": attrs}))
        binding["source_binding_sha256"] = addressed["sha256"]
        args_hash = _digest({"synthetic-arguments": i})
        request = save(f"request-{i}", {"argumentsSHA256": args_hash, "acceptedCommit": "synthetic-older-runtime-revision"})
        addressed_args = {"source_alias": "synthetic-registration", "type_name": "synthetic-catalog-type",
                          "catalog_metadata_id": catalog, "native_reference": native, "as_of": source["date"],
                          "attribute_names": ["Фамилия", "Имя", "Отчество", "ЛичныйНомер", "СтраховойНом", "ДатаРожд"]}
        addressed_request = save(f"addressed-request-{i}", {
            "argumentsSHA256": _digest(addressed_args), "acceptedCommit": "synthetic-older-runtime-revision"})
        shared = {"catalogMetadataId": catalog, "nativeReference": native,
                  "originalSourceSHA256": _digest(source), "preparedPayloadSHA256": _digest(payload),
                  "candidateRecordedSHA256": _digest(candidate)}
        acq_binding = save(f"acquisition-binding-{i}", {**shared, "actualResponseSHA256": acquisition["sha256"],
                                                      "argumentsSHA256": args_hash})
        addr_binding = save(f"addressed-binding-{i}", {**shared, "acquisitionResponseSHA256": acquisition["sha256"],
            "addressedResponseSHA256": addressed["sha256"], "asOf": source["date"]})
        rows.append({"originalCandidateExactCopy": copy.deepcopy(candidate),
            "acceptedConventionCommit": "synthetic-convention-commit", "originalRecordedCandidateSHA256": _digest(candidate),
            "originalSourceExactCopy": copy.deepcopy(source), "originalPreparedPayloadExactCopy": copy.deepcopy(payload),
            "originalPreparedSourceIdExactCopy": candidate["prepared"]["source_id"],
            "originalSourceTupleSHA256": _digest(source), "originalPreparedPayloadSHA256": _digest(payload),
            "existingAcquisitionBinding": acq_binding, "existingAddressedBinding": addr_binding,
            **{f: common for f in ("sourceRegistrationEvidenceRef", "sourceDescriptorEvidence", "sourceConfigurationEvidenceRef",
                                  "sourceArchiveIntentEvidenceRef", "independentAcceptanceEvidenceRef")},
            "acquisitionRequestRef": request, "addressedRequestRef": addressed_request,
            "acquisitionResponseRef": acquisition, "addressedResponseRef": addressed,
            "acquisitionResponseSHA256": acquisition["sha256"], "addressedResponseSHA256": addressed["sha256"],
            "nativeReferencePrivate": native, "catalogMetadataId": catalog, "sourceRegistrationNamespace": "synthetic-registration",
            "nativeReferenceSHA256": hashlib.sha256(native.encode()).hexdigest(),
            "nativeTupleCanonicalSHA256": _digest(["synthetic-registration", catalog, native]),
            "catalogType": "synthetic-catalog-type", "dateContextEvidence": {"actualReadContext": read_context},
            "sourceOriginalAsOf": source["date"]})
        lineage.append({"originalRecordedCandidateSHA256": _digest(candidate), "originalSourceTupleSHA256": _digest(source),
            "originalPayloadSHA256": _digest(payload), "acquisitionBinding": acq_binding, "addressedBinding": addr_binding})
    plan["plan_sha256"] = _digest({k: v for k, v in plan.items() if k != "plan_sha256"})
    handoff = save("handoff", {"convention": {"acceptedConventionCommit": "synthetic-convention-commit"}, "rows": rows})
    accepted = save("accepted", {"acceptedConventionCommit": "synthetic-convention-commit", "privateExactHandoff": handoff,
        "originalLineageDigests": lineage, **{f: common for f in (
            "separateCoordinatorAcceptance", "independentHandoffReview", "independentActualSourceReview")}})
    return seal_source_bundle(plan, accepted)

def test_mapping_preserves_complete_parts_and_zero_fraction_only():
    parts = [v.ljust(28) for v in ("Синтетический", "Пример", "Тестович")]
    assert len(" ".join(parts)) == 86
    assert compact_name(parts) == "Синтетический Пример Тестович"
    assert compact_name(["A-B  ", "C D  ", "E  "]) == "A-B C D E"
    assert compact_name([" A  ", "B  ", "C  "]).startswith(" A")
    assert canonical_birthday("1980-01-02T00:00:00.0000000") == "1980-01-02T00:00:00"
    for value in ("1980-01-02T00:00:00.0000001", "1980-01-02T00:00:00Z",
                  "1980-01-02T00:00:00+00:00", "1980-02-30T00:00:00", "1980-01-02",
                  "1980-01-02T00:00:00.00000000"):
        with pytest.raises(ValueError):
            canonical_birthday(value)
    with pytest.raises(ValueError):
        compact_name(["x" * 49, "A", "B"])
    preflight_person_names("Catalog_ФизическиеЛица", {"ФИО": "x" * 50})
    with pytest.raises(ValueError):
        preflight_person_names("Catalog_ФизическиеЛица", {"ФИО": "x" * 51})


def test_catalog_width_guard_before_claim_and_saved_preview(tmp_path):
    path, plan, _, _, _ = make_case(tmp_path)
    with _locked(path):
        index = _read(path)
        index["claims"][plan["bindings"][0]["manifest"]["batch_id"]]["stage"] = "verified_preview"
        _write(path, index)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ceiling"):
        preview_claim(path, plan["bindings"][0]["manifest"])
    with pytest.raises(ValueError, match="ceiling"):
        prepare_claim(path, [candidate("new", {"Description": "x" * 51})], batch_id="new")
    assert path.read_bytes() == before


def test_repair_is_atomic_idempotent_preserves_original_and_bank_holds(tmp_path):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    original = json.loads(path.read_text())
    binding = plan["bindings"][0]
    from onec_odata_mcp.queue_manifest import _record_historical_result
    proof = {**found(binding["candidate"], binding["target_key"]), "kind": "newly_created_verified",
             "manifest": binding["manifest"],
             "result_receipt": _record_historical_result(binding["manifest"], binding["original_result"])}
    proof["readback"] = evidences[0]["after"]
    with pytest.raises((QueueManifestError, ValueError)):
        resolve_claim(path, binding["manifest"], [binding["candidate"]], binding["original_result"], [proof])
    assert resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)["repaired_original_claims"] == 3
    repaired = json.loads(path.read_text())
    assert repaired["claims"]["bank-hold"] == original["claims"]["bank-hold"]
    bank_key = original["claims"]["bank-hold"]["rows"][0]["key"]
    assert repaired["entries"][bank_key] == original["entries"][bank_key]
    for item in plan["bindings"]:
        batch = item["manifest"]["batch_id"]
        assert repaired["claims"][batch]["repair_audit"]["original_claim"] == original["claims"][batch]
        assert repaired["claims"][batch]["manifest"] == item["manifest"]
        assert repaired["entries"][_key(item["candidate"])]["candidate_sha256"] == _digest(item["candidate"])
    final = path.read_bytes()
    resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
    assert path.read_bytes() == final
    assert screen_rows(path, [b["candidate"] for b in plan["bindings"]])["newly_created_verified"] == 3


@pytest.mark.parametrize("change", ["wrong-name", "nonzero-date", "wrong-ID", "receipt-hash", "other-target",
                                    "stale-version", "wildcard", "pending", "expired", "unapproved", "duplicate-key",
                                    "new-candidate", "original-operation", "changed-checkpoint"])
def test_repair_refuses_bad_evidence_without_any_checkpoint_mutation(tmp_path, change):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    if change == "wrong-name": evidences[2]["after"]["ФИО"] = "Truncated"
    if change == "nonzero-date": evidences[2]["after"]["ДатаРождения"] += ".0000001"
    if change == "wrong-ID": evidences[2]["after"]["СтраховойНомерПФР"] = "different"
    if change == "receipt-hash": evidences[2]["patch_receipt"]["request_hash"] = "0" * 64
    if change == "other-target": acceptance["target_fingerprint"] = "other"
    if change == "stale-version": evidences[2]["expected_data_version"] = "historical-version"
    if change == "wildcard": evidences[2]["expected_data_version"] = "*"
    if change == "pending": evidences[2]["patch_receipt"]["state"] = "pending"
    if change == "expired": acceptance["expires_at"] = "2000-01-01T00:00:00Z"
    if change == "unapproved": acceptance["correction_authority_ref"] = ""
    if change == "duplicate-key": plan["bindings"][2]["target_key"] = plan["bindings"][0]["target_key"]
    if change == "new-candidate": plan["bindings"][2]["candidate"]["source"]["line"] = "99"
    if change == "original-operation": evidences[2]["operation_id"] = plan["bindings"][0]["manifest"]["rows"][0]["operation_id"]
    if change == "changed-checkpoint":
        with _locked(path):
            index = _read(path)
            index["entries"][next(iter(index["entries"]))]["reason"] = "changed"
            _write(path, index)
    if change in {"duplicate-key", "new-candidate"}:
        plan["plan_sha256"] = _digest({k:v for k,v in plan.items() if k != "plan_sha256"})
        acceptance["plan_sha256"] = plan["plan_sha256"]
        bundle = seal_source_bundle(plan, bundle["accepted_handoff"])
        acceptance["source_bundle_sha256"] = bundle["source_bundle_sha256"]
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
    assert path.read_bytes() == before


def test_repair_accepts_exact_sqlite_utc_receipt_time_and_preserves_original_entry(tmp_path):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    original = json.loads(path.read_text())
    for proof in evidences:
        proof["patch_receipt"]["created_at"] = datetime.fromisoformat(proof["patch_receipt"]["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
    resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
    repaired = json.loads(path.read_text())
    for binding in plan["bindings"]:
        claim = repaired["claims"][binding["manifest"]["batch_id"]]
        assert claim["repair_audit"]["original_entry"] == original["entries"][_key(binding["candidate"])]
    # Tampered audit fails checkpoint validation even with a resealed outer checksum.
    claim["repair_audit"]["evidence"][0]["after"]["ФИО"] = "tampered"
    with _locked(path):
        _write(path, repaired)
    with pytest.raises(QueueManifestError, match="audit"):
        screen_rows(path, [binding["candidate"]])


@pytest.mark.parametrize("receipt_kind", ["sqlite_same_second", "sqlite_previous_second", "precise_before_read", "reversed_reads"])
def test_repair_chronology_uses_only_declared_sqlite_second_precision(tmp_path, receipt_kind):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    second = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0)
    for proof in evidences:
        proof["before_observed_at"] = (second + timedelta(microseconds=100000)).isoformat()
        proof["after_observed_at"] = (second + timedelta(microseconds=800000)).isoformat()
        proof["patch_receipt"]["created_at"] = second.strftime("%Y-%m-%d %H:%M:%S")
        if receipt_kind == "sqlite_previous_second":
            proof["patch_receipt"]["created_at"] = (second - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        if receipt_kind == "precise_before_read":
            proof["patch_receipt"]["created_at"] = second.isoformat()
        if receipt_kind == "reversed_reads":
            proof["before_observed_at"], proof["after_observed_at"] = proof["after_observed_at"], proof["before_observed_at"]
    before = path.read_bytes()
    if receipt_kind == "sqlite_same_second":
        assert resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)["repaired_original_claims"] == 3
    else:
        with pytest.raises(QueueManifestError, match="observation"):
            resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
        assert path.read_bytes() == before


@pytest.mark.parametrize("history_change", ["missing-entry", "entry-source", "entry-evidence", "entry-identity",
                                           "missing-receipt", "receipt-operation", "entry-audit-digest"])
def test_repaired_checkpoint_checks_history_shape_and_exact_bindings(tmp_path, history_change):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
    with _locked(path):
        index = _read(path)
        claim = index["claims"][plan["bindings"][0]["manifest"]["batch_id"]]
        audit = claim["repair_audit"]
        if history_change == "missing-entry": audit["original_entry"] = None
        if history_change == "entry-source": audit["original_entry"]["source"]["line"] = "99"
        if history_change == "entry-evidence": audit["original_entry"]["evidence_sha256"] = "0" * 64
        if history_change == "entry-identity": audit["original_entry"]["candidate_sha256"] = "0" * 64
        if history_change == "missing-receipt": audit["original_result_receipt"] = {"tampered": True}
        if history_change == "receipt-operation": audit["original_result_receipt"]["result"]["operationId"] = "different-operation"
        entry = index["entries"][claim["rows"][0]["key"]]
        if history_change == "entry-audit-digest": entry["evidence_sha256"] = "0" * 64
        else:
            # Even recalculating the per-entry digest cannot validate a structurally
            # invalid or rebound original history, beyond resealing the outer file.
            entry["evidence_sha256"] = _digest(audit)
        _write(path, index)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        screen_rows(path, [b["candidate"] for b in plan["bindings"]])
    assert path.read_bytes() == before


def test_terminal_repair_exact_replay_survives_expiry_but_new_evidence_does_not(tmp_path, monkeypatch):
    path, plan, acceptance, evidences, bundle = make_case(tmp_path)
    acceptance["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)
    before = path.read_bytes()
    import onec_odata_mcp.person_repair as module
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=1)
    monkeypatch.setattr(module, "datetime", Later)
    assert resolve_person_repair(path, plan, acceptance, evidences, source_bundle=bundle)["repaired_original_claims"] == 3
    assert path.read_bytes() == before
    changed = copy.deepcopy(evidences)
    changed[0]["after_observed_at"] = datetime.now(timezone.utc).isoformat()
    with pytest.raises(QueueManifestError, match="terminal evidence"):
        resolve_person_repair(path, plan, acceptance, changed, source_bundle=bundle)
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["added", "removed", "nested", "annotation"])
def test_repair_refuses_every_unrelated_full_readback_change_atomically(tmp_path, change):
    path, plan, acceptance, proofs, bundle = make_case(tmp_path)
    proof = proofs[2]
    proof["before"]["nested"] = {"rows": [{"amount": 1}]}
    proof["after"]["nested"] = {"rows": [{"amount": 1}]}
    proof["before"]["odata.metadata"] = "synthetic-annotation"
    proof["after"]["odata.metadata"] = "synthetic-annotation"
    if change == "added": proof["after"]["new"] = False
    if change == "removed": del proof["after"]["nested"]
    if change == "nested": proof["after"]["nested"]["rows"][0]["amount"] = 2
    if change == "annotation": proof["after"]["odata.metadata"] = "different"
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="unrelated"):
        resolve_person_repair(path, plan, acceptance, proofs, source_bundle=bundle)
    assert path.read_bytes() == before


def test_repair_remainder_is_structural_not_key_order(tmp_path):
    path, plan, acceptance, proofs, bundle = make_case(tmp_path)
    for proof in proofs:
        proof["before"]["nested"] = {"a": 1, "b": [2, {"c": None}]}
        proof["after"]["nested"] = {"b": [2, {"c": None}], "a": 1}
        proof["after"] = dict(reversed(list(proof["after"].items())))
    assert resolve_person_repair(path, plan, acceptance, proofs, source_bundle=bundle)["repaired_original_claims"] == 3


@pytest.mark.parametrize("field", ["Description", "ФИО"])
def test_manifest_guard_covers_compiler_and_saved_resealed_apply(tmp_path, field):
    from onec_odata_mcp.queue_manifest import compile_manifest, validate_manifest, dry_run_request, apply_request
    row = candidate("bounded", payload={field: "x" * 50})
    kwargs = {"batch_id": "synthetic-direct", "target_fingerprint": row["target_fingerprint"],
              "template_id": row["template"]["id"], "template_version": row["template"]["version"],
              "workflow": "catalog_prerequisites"}
    safe = compile_manifest([row["prepared"]], **kwargs)
    assert dry_run_request(safe)["tool_arguments"]["payload"][field] == "x" * 50
    row["prepared"]["payload"][field] += "x"
    with pytest.raises(ValueError, match="ceiling"):
        compile_manifest([row["prepared"]], **kwargs)
    safe["rows"][0]["payload"][field] += "x"
    safe["plan_sha256"] = _digest({k: v for k, v in safe.items() if k != "plan_sha256"})
    for check in (validate_manifest, dry_run_request):
        with pytest.raises(ValueError, match="ceiling"):
            check(safe)
    preview_receipt = {"schema_version": 1, "plan_sha256": safe["plan_sha256"],
                       "target_fingerprint": safe["target_fingerprint"], "template": safe["template"],
                       "dry_run_verified": True}
    with pytest.raises(ValueError, match="ceiling"):
        apply_request(safe, preview_receipt, target_fingerprint=row["target_fingerprint"],
                      template_id=row["template"]["id"], template_version=row["template"]["version"])
    row["prepared"]["entity_set"] = "Catalog_Синтетический"
    assert compile_manifest([row["prepared"]], **kwargs)["rows"][0]["payload"][field] == "x" * 51


@pytest.mark.parametrize("change", ["raw-byte", "stale-acceptance", "foreign-candidate", "native", "asof", "scalar", "digest", "missing-review"])
def test_source_bundle_refuses_tampering_without_checkpoint_changes(tmp_path, change):
    import hashlib
    path, plan, acceptance, proofs, bundle = make_case(tmp_path)
    root = tmp_path / "source"
    if change == "raw-byte":
        p = root / "addressed-2.json"
        p.write_bytes(p.read_bytes() + b" ")
    elif change == "stale-acceptance": acceptance["accepted_handoff_sha256"] = "0" * 64
    elif change == "missing-review": acceptance["source_independent_review_ref"] = ""
    elif change == "digest":
        plan["bindings"][2]["source_binding_sha256"] = "0" * 64
        plan["plan_sha256"] = _digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        acceptance["plan_sha256"] = plan["plan_sha256"]
        bundle = seal_source_bundle(plan, bundle["accepted_handoff"])
        acceptance["source_bundle_sha256"] = bundle["source_bundle_sha256"]
    else:
        # Rehash the whole attacker-supplied chain and even rebind acceptance. The
        # actual native/scalar/date/candidate checks must still reject it.
        handoff = json.loads((root / "handoff.json").read_text())
        if change == "foreign-candidate": handoff["rows"][2]["originalCandidateExactCopy"]["source"]["line"] = "99"
        else:
            p = root / "addressed-2.json"
            saved = json.loads(p.read_text()); response = json.loads(saved["content"][0]["text"])
            if change == "native": response["data"]["reference"]["nativeReference"] = "FOREIGN-TOKEN"
            if change == "asof": response["data"]["readContext"]["asOf"] = "2000-01-01"
            if change == "scalar": response["data"]["attributes"]["ЛичныйНомер"] = "FOREIGN-ID"
            saved["content"][0]["text"] = json.dumps(response); p.write_text(json.dumps(saved))
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            handoff["rows"][2]["addressedResponseRef"]["sha256"] = digest
            handoff["rows"][2]["addressedResponseSHA256"] = digest
            plan["bindings"][2]["source_binding_sha256"] = digest
            p = root / "addressed-binding-2.json"; v = json.loads(p.read_text())
            v["addressedResponseSHA256"] = digest; p.write_text(json.dumps(v))
            handoff["rows"][2]["existingAddressedBinding"]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        p = root / "handoff.json"; p.write_text(json.dumps(handoff))
        accepted = json.loads((root / "accepted.json").read_text())
        accepted["privateExactHandoff"]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        accepted["originalLineageDigests"][2]["addressedBinding"] = handoff["rows"][2]["existingAddressedBinding"]
        p = root / "accepted.json"; p.write_text(json.dumps(accepted))
        plan["plan_sha256"] = _digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        bundle = seal_source_bundle(plan, {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        acceptance.update({"plan_sha256": plan["plan_sha256"], "source_bundle_sha256": bundle["source_bundle_sha256"],
                           "accepted_handoff_sha256": bundle["accepted_handoff"]["sha256"]})
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        resolve_person_repair(path, plan, acceptance, proofs, source_bundle=bundle)
    assert path.read_bytes() == before


def test_additive_source_bundle_keeps_original_sealed_plan_byte_identical(tmp_path):
    _, plan, acceptance, _, bundle = make_case(tmp_path)
    before = json.dumps(plan, sort_keys=True)
    resealed_bundle = seal_source_bundle(plan, bundle["accepted_handoff"])
    assert resealed_bundle == bundle
    assert validate_source_bundle(plan, bundle, acceptance)["verified_rows"] == 3
    assert json.dumps(plan, sort_keys=True) == before


@pytest.mark.parametrize("change", ["native", "date", "type", "namespace", "catalog", "field-list",
                                    "field-order", "unrelated", "malformed", "missing", "wrong-shape"])
def test_addressed_request_semantics_reject_rehashed_bundle_atomically(tmp_path, change):
    import hashlib
    path, plan, acceptance, proofs, bundle = make_case(tmp_path)
    checkpoint_before = path.read_bytes()
    plan_before = json.dumps(plan, sort_keys=True)
    root = tmp_path / "source"
    handoff = json.loads((root / "handoff.json").read_text())
    row = handoff["rows"][2]
    args = {"source_alias": row["sourceRegistrationNamespace"], "type_name": row["catalogType"],
            "catalog_metadata_id": row["catalogMetadataId"], "native_reference": row["nativeReferencePrivate"],
            "as_of": row["originalSourceExactCopy"]["date"],
            "attribute_names": ["Фамилия", "Имя", "Отчество", "ЛичныйНомер", "СтраховойНом", "ДатаРожд"]}
    if change == "native": args["native_reference"] = "SYNTHETIC-OTHER-NATIVE"
    if change == "date": args["as_of"] = "2000-01-01"
    if change == "type": args["type_name"] = "synthetic-other-type"
    if change == "namespace": args["source_alias"] = "synthetic-other-registration"
    if change == "catalog": args["catalog_metadata_id"] = "SYNTHETIC-OTHER-CATALOG"
    if change == "field-list": args["attribute_names"].pop()
    if change == "field-order": args["attribute_names"].reverse()
    if change == "unrelated": args = {"synthetic-unrelated-arguments": True}
    request = {"argumentsSHA256": _digest(args), "acceptedCommit": "synthetic-older-runtime-revision"}
    if change == "malformed": request["argumentsSHA256"] = "invalid"
    if change == "missing": del request["argumentsSHA256"]
    if change == "wrong-shape": request = []
    def save(name, value):
        p = root / name
        p.write_text(json.dumps(value))
        return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    # All enclosing artifact hashes and acceptance refs are fresh, so the sole
    # refusal is the request semantics/shape, not a stale raw-byte digest.
    row["addressedRequestRef"] = save("foreign-addressed-request.json", request)
    accepted = json.loads((root / "accepted.json").read_text())
    accepted["privateExactHandoff"] = save("handoff.json", handoff)
    bundle = seal_source_bundle(plan, save("accepted.json", accepted))
    acceptance.update(source_bundle_sha256=bundle["source_bundle_sha256"],
                      accepted_handoff_sha256=bundle["accepted_handoff"]["sha256"])
    with pytest.raises(QueueManifestError):
        validate_source_bundle(plan, bundle, acceptance)
    with pytest.raises(QueueManifestError):
        resolve_person_repair(path, plan, acceptance, proofs, source_bundle=bundle)
    assert path.read_bytes() == checkpoint_before
    assert json.dumps(plan, sort_keys=True) == plan_before


@pytest.mark.parametrize("count", [1, 4])
def test_repair_resolves_arbitrary_sealed_batch_size(tmp_path, count):
    path, plan, acceptance, evidence, bundle = make_case(tmp_path, count=count)
    result = resolve_person_repair(path, plan, acceptance, evidence, source_bundle=bundle)
    assert result["repaired_original_claims"] == count
    assert resolve_person_repair(path, plan, acceptance, evidence, source_bundle=bundle)["repaired_original_claims"] == count
