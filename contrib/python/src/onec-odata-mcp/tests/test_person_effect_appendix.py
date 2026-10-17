"""Synthetic exact3 additional-effect appendix; no real identity/receipt data."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from onec_odata_mcp.person_repair import (
    SERVICE_EFFECT_FIELD, effect_row1_pins, seal_effect_appendix,
    validate_effect_appendix, resolve_person_repair,
)
from onec_odata_mcp.queue_checkpoint import _read, _write, _locked, _key, screen_rows
from onec_odata_mcp.queue_manifest import QueueManifestError, _digest
from test_person_repair import make_case


def save(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def accept_effects(tmp_path, appendix, proofs):
    ref = save(tmp_path, "synthetic-all_rows-review.json", {
        "schema_version": 1, "status": "accepted_exact_batch_effects",
        "effect_appendix_sha256": appendix["effect_appendix_sha256"],
        "evidence_sha256": _digest(proofs), "rows": [
            {"candidate_sha256": row["candidate_sha256"], "target_key": row["target_key"],
             "evidence_sha256": _digest(proof), "field": SERVICE_EFFECT_FIELD,
             "full_field_set_unchanged": True, "all_other_fields_unchanged": True}
            for row, proof in zip(appendix["rows"], proofs)]})
    return {"schema_version": 1, "effect_appendix_sha256": appendix["effect_appendix_sha256"],
        "owner_decision_sha256": appendix["owner_decision_ref"]["sha256"],
        "observed_review_sha256": appendix["observed_review_ref"]["sha256"],
        "independent_effect_review_ref": ref, "expires_at": appendix["expires_at"]}


def make_effect_case(tmp_path):
    path, plan, acceptance, proofs, bundle = make_case(tmp_path)
    for i, proof in enumerate(proofs):
        proof["before"][SERVICE_EFFECT_FIELD] = f"synthetic-service-before-{i}"
        proof["after"][SERVICE_EFFECT_FIELD] = f"synthetic-service-after-{i}"
        proof["before"]["Nested"] = {"value": 1, "annotation": [True, None]}
        proof["after"]["Nested"] = copy.deepcopy(proof["before"]["Nested"])
    pins = effect_row1_pins(proofs[0])
    selected = {k: pins[k] for k in ("full_before_sha256", "full_after_sha256", "receipt_sha256", "request_hash")}
    review = save(tmp_path, "synthetic-observed-review.json", {"row1": {**selected,
        "checks": {k: True for k in ("independent_beforeafterreceipt_exact", "full_field_set_exact",
            "only_observed_service_extra", "all_other_fields_exact_recursive_typed")}},
        "formula_and_downstream_effects": "UNKNOWN"})
    owner = save(tmp_path, "synthetic-owner.json", {
        "original_plan_sha256": plan["plan_sha256"], "source_bundle_sha256": bundle["source_bundle_sha256"],
        "field": SERVICE_EFFECT_FIELD, "exact_row1_observed_before_after_effect_accepted": True,
        "row1_PATCH_repeat_authorized": False,
        "remaining_rows_authorized": [b["candidate"]["prepared"]["source_id"] for b in plan["bindings"][1:]],
        "owner_accepts_exact_case_unknown_semantics": True,
        "formula_and_downstream_unknown_explicitly_disclosed_before_answer": True,
        "actual_row1_evidence_hashes": selected, "independent_fact_review_sha256": review["sha256"]})
    appendix = seal_effect_appendix(plan, bundle, acceptance, owner_decision_ref=owner,
        observed_review_ref=review, observed_row1=proofs[0], expires_at=acceptance["expires_at"])
    effect_acceptance = accept_effects(tmp_path, appendix, proofs)
    return path, plan, acceptance, proofs, bundle, appendix, effect_acceptance


def resolve(case):
    path, plan, acceptance, proofs, bundle, appendix, effects = case
    return resolve_person_repair(path, plan, acceptance, proofs, source_bundle=bundle,
        effect_appendix=appendix, effect_acceptance=effects)


def test_effect_appendix_resolves_original_batch_without_replacing_original_history(tmp_path):
    case = make_effect_case(tmp_path)
    path, plan, acceptance, proofs, bundle, appendix, effects = case
    originals = copy.deepcopy((plan, acceptance, bundle))
    checkpoint = json.loads(path.read_text())
    assert validate_effect_appendix(plan, bundle, acceptance, appendix, proofs[0])["observed_row1_no_dispatch"]
    assert resolve(case)["repaired_original_claims"] == 3
    repaired = _read(path)
    for b in plan["bindings"]:
        batch = b["manifest"]["batch_id"]
        audit = repaired["claims"][batch]["repair_audit"]
        assert audit["original_claim"] == checkpoint["claims"][batch]
        assert audit["original_entry"] == checkpoint["entries"][_key(b["candidate"])]
        assert repaired["claims"][batch]["manifest"] == b["manifest"]
        assert audit["effect_appendix"] == appendix
        assert audit["effect_acceptance"] == effects
    independent = set(checkpoint["claims"]) - {b["manifest"]["batch_id"] for b in plan["bindings"]}
    assert all(checkpoint["claims"][k] == repaired["claims"][k] for k in independent)
    assert (plan, acceptance, bundle) == originals
    terminal = path.read_bytes()
    assert resolve(case)["repaired_original_claims"] == 3
    assert path.read_bytes() == terminal
    assert screen_rows(path, [b["candidate"] for b in plan["bindings"]])["newly_created_verified"] == 3


@pytest.mark.parametrize("change", ["no-appendix", "no-all_rows-acceptance", "wrong-field", "wrong-plan", "wrong-bundle",
    "wrong-target", "wrong-ledger", "wrong-registration", "wrong-order", "row1-new-operation", "row1-new-after",
    "row1-new-before", "row1-new-receipt", "expired-appendix", "expired-effects", "owner-byte-change", "review-byte-change",
    "owner-reject", "owner-other-plan", "owner-other-scope", "review-not-independent", "review-unknown-erased",
    "missing-third-proof", "proof-order", "stale-all_rows-review", "all_rows-fieldset-false", "other-effect", "nested-type",
    "service-added", "service-removed", "service-type", "annotation"])
def test_effect_failures_hold_all_rows_with_no_checkpoint_mutation(tmp_path, change):
    case = list(make_effect_case(tmp_path))
    path, plan, acceptance, proofs, bundle, appendix, effects = case
    if change == "no-appendix": case[5] = case[6] = None
    if change == "no-all_rows-acceptance": case[6] = None
    if change == "wrong-field": appendix["field"] = "AnotherServiceField"
    if change == "wrong-plan": appendix["plan_sha256"] = "0" * 64
    if change == "wrong-bundle": appendix["source_bundle_sha256"] = "0" * 64
    if change == "wrong-target": appendix["target_fingerprint"] = "synthetic-foreign"
    if change == "wrong-ledger": appendix["ledger_namespace_receipt"] = "foreign"
    if change == "wrong-registration": appendix["writer_registration_receipt"] = "foreign"
    if change == "wrong-order": appendix["rows"].reverse()
    if change == "row1-new-operation": proofs[0]["operation_id"] = "synthetic-new-row1-operation"
    if change == "row1-new-after": proofs[0]["after"][SERVICE_EFFECT_FIELD] = "different"
    if change == "row1-new-before": proofs[0]["before"][SERVICE_EFFECT_FIELD] = "different"
    if change == "row1-new-receipt": proofs[0]["patch_receipt"]["created_at"] = proofs[0]["after_observed_at"]
    if change == "expired-appendix": appendix["expires_at"] = "2000-01-01T00:00:00Z"
    if change == "expired-effects": effects["expires_at"] = "2000-01-01T00:00:00Z"
    if change in {"owner-byte-change", "review-byte-change"}:
        field = "owner_decision_ref" if change == "owner-byte-change" else "observed_review_ref"
        from pathlib import Path
        p = Path(appendix[field]["path"]); p.write_bytes(p.read_bytes() + b" ")
    if change.startswith("owner-") and change not in {"owner-byte-change"}:
        from pathlib import Path
        p = Path(appendix["owner_decision_ref"]["path"]); v = json.loads(p.read_text())
        if change == "owner-reject": v["exact_row1_observed_before_after_effect_accepted"] = False
        if change == "owner-other-plan": v["original_plan_sha256"] = "0" * 64
        if change == "owner-other-scope": v["remaining_rows_authorized"].append("other-row")
        appendix["owner_decision_ref"] = save(tmp_path, "synthetic-owner-rebound.json", v)
    if change in {"review-not-independent", "review-unknown-erased"}:
        from pathlib import Path
        v = json.loads(Path(appendix["observed_review_ref"]["path"]).read_text())
        if change == "review-not-independent": v["row1"]["checks"]["independent_beforeafterreceipt_exact"] = False
        if change == "review-unknown-erased": v["formula_and_downstream_effects"] = "KNOWN_SAFE"
        appendix["observed_review_ref"] = save(tmp_path, "synthetic-review-rebound.json", v)
        owner = json.loads(Path(appendix["owner_decision_ref"]["path"]).read_text())
        owner["independent_fact_review_sha256"] = appendix["observed_review_ref"]["sha256"]
        appendix["owner_decision_ref"] = save(tmp_path, "synthetic-owner-rebound.json", owner)
    if change == "missing-third-proof": proofs.pop()
    if change == "proof-order": proofs.reverse()
    if change == "stale-all_rows-review": proofs[2]["after_observed_at"] = datetime.now(timezone.utc).isoformat()
    if change == "all_rows-fieldset-false":
        from pathlib import Path
        v = json.loads(Path(effects["independent_effect_review_ref"]["path"]).read_text())
        v["rows"][2]["full_field_set_unchanged"] = False
        effects["independent_effect_review_ref"] = save(tmp_path, "synthetic-review-rejected.json", v)
    if change == "other-effect": proofs[2]["after"]["Code"] = "new"
    if change == "nested-type": proofs[2]["after"]["Nested"]["value"] = True
    if change == "service-added": del proofs[2]["before"][SERVICE_EFFECT_FIELD]
    if change == "service-removed": del proofs[2]["after"][SERVICE_EFFECT_FIELD]
    if change == "service-type": proofs[2]["after"][SERVICE_EFFECT_FIELD] = 123
    if change == "annotation": proofs[2]["after"]["@odata.etag"] = "new"
    # Re-seal shape changes and refresh all_rows reviewer assertions for unauthorized
    # full-row effects: actual comparator/binding must reject them nonetheless.
    if change not in {"no-appendix", "no-all_rows-acceptance"}:
        appendix["effect_appendix_sha256"] = _digest({k:v for k,v in appendix.items() if k != "effect_appendix_sha256"})
    if change in {"expired-appendix", "other-effect", "nested-type", "service-added", "service-removed", "service-type", "annotation"}:
        case[6] = accept_effects(tmp_path, appendix, proofs)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError): resolve(case)
    assert path.read_bytes() == before


def test_effect_terminal_replay_after_both_expiries_is_exact_and_idempotent(tmp_path, monkeypatch):
    case = make_effect_case(tmp_path)
    resolve(case)
    final = case[0].read_bytes()
    import onec_odata_mcp.person_repair as module
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return datetime.now(tz) + timedelta(days=2)
    monkeypatch.setattr(module, "datetime", Later)
    assert resolve(case)["repaired_original_claims"] == 3
    assert case[0].read_bytes() == final
    changed = list(copy.deepcopy(case))
    changed[3][2]["after_observed_at"] = datetime.now(timezone.utc).isoformat()
    changed[6] = accept_effects(tmp_path, changed[5], changed[3])
    with pytest.raises(QueueManifestError, match="terminal evidence"): resolve(changed)
    assert case[0].read_bytes() == final


@pytest.mark.parametrize("change", ["remove-appendix", "remove-both", "row1-mode", "field", "ledger", "proof", "partial-acceptance"])
def test_effect_history_rejects_rehashed_structurally_invalid_audits(tmp_path, change):
    case = make_effect_case(tmp_path)
    resolve(case)
    path, plan = case[:2]
    with _locked(path):
        index = _read(path)
        claim = index["claims"][plan["bindings"][0]["manifest"]["batch_id"]]
        audit = claim["repair_audit"]
        if change == "remove-appendix": del audit["effect_appendix"]
        if change == "remove-both":
            del audit["effect_appendix"]
            del audit["effect_acceptance"]
        if change == "row1-mode": audit["effect_appendix"]["rows"][0]["mode"] = "remaining_name_patch"
        if change == "field": audit["effect_appendix"]["field"] = "OtherField"
        if change == "ledger": audit["effect_appendix"]["ledger_namespace_receipt"] = "foreign"
        if change == "proof": audit["evidence"][0]["after"][SERVICE_EFFECT_FIELD] = "changed"
        if change == "partial-acceptance": del audit["effect_acceptance"]["independent_effect_review_ref"]
        if "effect_appendix" in audit:
            audit["effect_appendix"]["effect_appendix_sha256"] = _digest({k:v for k,v in audit["effect_appendix"].items() if k != "effect_appendix_sha256"})
        core = {k:v for k,v in audit.items() if k in {"plan", "acceptance", "evidence", "source_bundle", "effect_appendix", "effect_acceptance"}}
        audit["resolution_sha256"] = claim["resolution_sha256"] = _digest(core)
        index["entries"][claim["rows"][0]["key"]]["evidence_sha256"] = _digest(audit)
        _write(path, index)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError): screen_rows(path, [b["candidate"] for b in plan["bindings"]])
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["acceptance-schema-bool", "review-check-number"])
def test_effect_metadata_requires_exact_json_types(tmp_path, change):
    case = make_effect_case(tmp_path)
    effects = case[6]
    if change == "acceptance-schema-bool": effects["schema_version"] = True
    else:
        from pathlib import Path
        reviewed = json.loads(Path(effects["independent_effect_review_ref"]["path"]).read_text())
        reviewed["rows"][2]["all_other_fields_unchanged"] = 1
        effects["independent_effect_review_ref"] = save(tmp_path, "synthetic-typed-review.json", reviewed)
    before = case[0].read_bytes()
    with pytest.raises(QueueManifestError): resolve(case)
    assert case[0].read_bytes() == before
