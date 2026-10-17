"""Synthetic offline evidence and persistence tests for the configured policy."""
import json
import multiprocessing
import copy

import pytest

from onec_odata_mcp.queue_checkpoint import (
    import_evidence, screen_rows, prepare_claim, preview_claim, verify_preview_claim,
    apply_claim, cancel_claim, resolve_claim, main,
)
from onec_odata_mcp.queue_manifest import QueueManifestError, _digest, compile_manifest, record_result


def candidate(line="1", target="test-a", amount="42"):
    return {
        "source": {"namespace": "legacy-bank-a", "date": "2011-03-21", "statement": "7", "line": line},
        "target_fingerprint": target,
        "template": {"id": "synthetic-bank", "version": "1"},
        "prepared": {
            "source_id": f"source-{line}", "entity_set": "Document_Example",
            "payload": {"Number": f"SYN-{line}", "Amount": amount}, "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example",
                "rows": [{"amount": amount, "debit": "51", "credit": "91.1"}],
            },
        },
    }


def evidence(row, *, kind="found_existing_verified", key="key-1"):
    result = {
        "candidate_sha256": _digest(row), "target_fingerprint": row["target_fingerprint"],
        "source": row["source"], "kind": kind, "target_key": key,
        "lookup": {"complete": True, "result_limited": False, "keys": [key],
                   "source": row["source"], "target_fingerprint": row["target_fingerprint"]},
        "readback": {"Ref_Key": key, "Posted": True, "DeletionMark": False,
                     "entity_set": row["prepared"]["entity_set"],
                     "target_fingerprint": row["target_fingerprint"],
                     **row["prepared"]["payload"]},
        "active_movements": {
            "target_key": key, "register": "AccountingRegister_Example",
            "recorder_key": key, "recorder_type": row["prepared"]["entity_set"],
            "complete": True, "result_limited": False, "truncated": False, "total_count": 1,
            "rows": [{"active": True, "recorder_key": key,
                      "recorder_type": row["prepared"]["entity_set"],
                      "amount": row["prepared"]["payload"]["Amount"],
                      "debit": "51", "credit": "91.1"}],
        },
    }
    if kind == "newly_created_verified":
        item = row["prepared"]
        manifest = compile_manifest([item], batch_id="synthetic-batch",
                                    target_fingerprint=row["target_fingerprint"],
                                    template_id=row["template"]["id"],
                                    template_version=row["template"]["version"])
        planned = manifest["rows"][0]
        queue_result = {
            "dryRun": False, "total": 1, "succeeded": 1, "failed": 0,
            "needsReconciliation": 0,
            "results": [{"operationId": planned["operation_id"],
                         "postOperationId": planned["post_operation_id"],
                         "key": key, "ok": True, "state": "posted"}],
        }
        result["manifest"] = manifest
        result["result_receipt"] = record_result(manifest, queue_result)
    return result


def test_cross_turn_existing_and_newly_created_are_distinct(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    first, second, third = candidate(), candidate("2"), candidate("3")
    assert import_evidence(path, first, evidence(first))["status"] == "found_existing_verified"
    assert import_evidence(path, second, evidence(second, kind="newly_created_verified", key="key-2"))["status"] == "newly_created_verified"
    missing = {"candidate_sha256": _digest(third), "target_fingerprint": third["target_fingerprint"],
               "source": third["source"], "kind": "missing_verified",
               "lookup": {"complete": True, "result_limited": False, "keys": [],
                          "source": third["source"], "target_fingerprint": third["target_fingerprint"]}}
    assert import_evidence(path, third, missing)["status"] == "missing_verified"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    result = screen_rows(path, [first, second, third])
    assert result["found_existing_verified"] == 1
    assert result["newly_created_verified"] == 1
    assert "rows" not in result
    assert result["ready_count"] == 1
    before = path.read_bytes()
    assert import_evidence(path, first, evidence(first))["status"] == "found_existing_verified"
    assert path.read_bytes() == before


def test_mismatch_stale_uncertain_and_conflict_hold(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    original = candidate()
    wrong = evidence(original)
    wrong["target_fingerprint"] = "other-target"
    assert import_evidence(path, original, wrong)["reason"] == "other_target"
    assert screen_rows(path, [original])["held_count_by_reason"] == {"other_target": 1}
    assert import_evidence(path, original, evidence(original))["status"] == "found_existing_verified"
    changed = candidate(amount="43")
    assert screen_rows(path, [changed])["held_count_by_reason"] == {"source_or_template_stale": 1}
    assert screen_rows(path, [candidate(target="other-target")])["held_count_by_reason"] == {"checkpoint_missing": 1}
    ambiguous = candidate("2")
    assert import_evidence(path, ambiguous, evidence(ambiguous, key="key-1"))["reason"] == "target_ambiguous"
    assert screen_rows(path, [original, ambiguous])["held_count_by_reason"] == {"target_ambiguous": 2}


def test_target_conflict_stays_held_after_reimporting_old_evidence(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    assert import_evidence(path, row, evidence(row, key="key-a"))["status"] == "found_existing_verified"

    conflict = import_evidence(path, row, evidence(row, key="key-b"))
    assert (conflict["status"], conflict["reason"]) == ("held", "target_conflict")

    replay = import_evidence(path, row, evidence(row, key="key-a"))
    assert (replay["status"], replay["reason"], replay["target_key"]) == (
        "held", "target_conflict", "key-b"
    )
    assert screen_rows(path, [row])["held_count_by_reason"] == {"target_conflict": 1}


def test_boolean_claims_and_missing_bindings_never_complete(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    proof = evidence(row)
    proof["readback"]["Amount"] = "99"
    assert import_evidence(path, row, proof)["reason"] == "readback_unverified"
    proof = evidence(row)
    proof["active_movements"] = {"verified": True, "target_key": "key-1"}
    assert import_evidence(path, row, proof)["reason"] == "movements_unverified"
    proof = evidence(row, kind="newly_created_verified")
    proof["result_receipt"]["result"]["results"][0]["state"] = "needs_reconciliation"
    assert import_evidence(path, row, proof)["reason"] == "queue_receipt_invalid"
    proof = evidence(row, kind="newly_created_verified")
    del proof["result_receipt"]
    assert import_evidence(path, row, proof)["reason"] == "queue_receipt_missing"
    assert screen_rows(path, [row])["found_existing_verified"] == 0


def test_absence_needs_complete_lookup_then_can_transition_to_created(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    assert screen_rows(path, [row])["held_count_by_reason"] == {"checkpoint_missing": 1}
    absent = {"candidate_sha256": _digest(row), "target_fingerprint": row["target_fingerprint"],
              "source": row["source"], "kind": "missing_verified",
              "lookup": {"complete": False, "result_limited": False, "keys": [],
                         "source": row["source"], "target_fingerprint": row["target_fingerprint"]}}
    assert import_evidence(path, row, absent)["reason"] == "absence_unverified"
    absent["lookup"]["complete"] = True
    assert import_evidence(path, row, absent)["status"] == "missing_verified"
    assert screen_rows(path, [row])["ready_count"] == 1
    assert import_evidence(path, row, evidence(row, kind="newly_created_verified"))["status"] == "newly_created_verified"
    assert screen_rows(path, [row])["newly_created_verified"] == 1


def test_scalar_movement_expectation_from_existing_queue_format(tmp_path):
    row = candidate()
    row["prepared"]["expected_accounting_movements"] = {
        "register": "AccountingRegister_Example", "amount": "42.00",
        "debit": "51", "credit": "91.1", "tolerance": "0",
    }
    proof = evidence(row)
    assert import_evidence(tmp_path / "private" / "checkpoint.json", row, proof)["status"] == "found_existing_verified"


def test_input_order_and_atomic_file_under_parallel_import(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate(str(i)) for i in range(1, 9)]
    with multiprocessing.Pool(4) as pool:
        pool.starmap(import_evidence, [(path, row, evidence(row, key=f"key-{i}"))
                                       for i, row in enumerate(rows, 1)])
    assert len(json.loads(path.read_text())["entries"]) == 8
    assert screen_rows(path, list(reversed(rows)))["found_existing_verified"] == 8


def test_same_source_different_targets_coexist_and_missing_can_be_found(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    first, second = candidate(target="test-a"), candidate(target="test-b")
    absent = {"candidate_sha256": _digest(first), "target_fingerprint": first["target_fingerprint"],
              "source": first["source"], "kind": "missing_verified",
              "lookup": {"complete": True, "result_limited": False, "keys": [],
                         "source": first["source"], "target_fingerprint": first["target_fingerprint"]}}
    assert import_evidence(path, first, absent)["status"] == "missing_verified"
    assert import_evidence(path, first, evidence(first))["status"] == "found_existing_verified"
    assert import_evidence(path, second, evidence(second, key="key-b"))["status"] == "found_existing_verified"
    assert len(json.loads(path.read_text())["entries"]) == 2
    assert screen_rows(path, [first, second])["found_existing_verified"] == 2


def test_old_screen_report_cannot_supply_apply_rows(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    absent = {"candidate_sha256": _digest(row), "target_fingerprint": row["target_fingerprint"],
              "source": row["source"], "kind": "missing_verified",
              "lookup": {"complete": True, "result_limited": False, "keys": [],
                         "source": row["source"], "target_fingerprint": row["target_fingerprint"]}}
    import_evidence(path, row, absent)
    old_report = screen_rows(path, [row])
    assert old_report["ready_count"] == 1
    assert "rows" not in old_report and "tool_arguments" not in old_report
    import_evidence(path, row, evidence(row))
    current_report = screen_rows(path, [row])
    assert current_report["ready_count"] == 0
    assert current_report["found_existing_verified"] == 1


@pytest.mark.parametrize("change", [
    lambda proof: proof["readback"].update(entity_set="Document_Other"),
    lambda proof: proof["readback"].update(target_fingerprint="other-target"),
    lambda proof: proof["readback"].pop("entity_set"),
    lambda proof: proof["readback"].pop("target_fingerprint"),
])
def test_readback_requires_bound_entity_and_target(tmp_path, change):
    row = candidate()
    proof = evidence(row)
    change(proof)
    assert import_evidence(tmp_path / "private" / "checkpoint.json", row, proof)["status"] == "held"


def test_explicit_readback_context_can_bind_raw_document(tmp_path):
    row = candidate()
    proof = evidence(row)
    proof["readback"].pop("entity_set")
    proof["readback"].pop("target_fingerprint")
    proof["readback_context"] = {"entity_set": row["prepared"]["entity_set"],
                                  "target_fingerprint": row["target_fingerprint"]}
    assert import_evidence(tmp_path / "private" / "checkpoint.json", row, proof)["status"] == "found_existing_verified"


@pytest.mark.parametrize("change", [
    lambda movement: movement.pop("complete"),
    lambda movement: movement.update(complete=False),
    lambda movement: movement.update(result_limited=True),
    lambda movement: movement.pop("truncated"),
    lambda movement: movement.update(truncated=True),
    lambda movement: movement.pop("total_count"),
    lambda movement: movement.update(total_count=2),
    lambda movement: movement.update(recorder_key="foreign"),
    lambda movement: movement.update(recorder_type="Document_Foreign"),
    lambda movement: movement["rows"][0].update(recorder_key="foreign"),
    lambda movement: movement["rows"][0].update(recorder_type="Document_Foreign"),
])
def test_movements_require_complete_exact_recorder_set(tmp_path, change):
    row = candidate()
    proof = evidence(row)
    change(proof["active_movements"])
    assert import_evidence(tmp_path / "private" / "checkpoint.json", row, proof)["reason"] == "movements_unverified"


def test_checkpoint_rejects_corrupt_digest_entry_and_key(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    import_evidence(path, row, evidence(row))
    original = json.loads(path.read_text())
    damaged = copy.deepcopy(original)
    entry = next(iter(damaged["entries"].values()))
    entry["status"] = "missing_verified"
    path.write_text(json.dumps(damaged))
    with pytest.raises(QueueManifestError):
        screen_rows(path, [row])
    damaged["index_sha256"] = _digest({"schema_version": damaged["schema_version"],
                                       "entries": damaged["entries"], "claims": damaged["claims"]})
    path.write_text(json.dumps(damaged))
    with pytest.raises(QueueManifestError):
        screen_rows(path, [row])
    damaged = copy.deepcopy(original)
    damaged["entries"]["wrong-key"] = damaged["entries"].pop(next(iter(damaged["entries"])))
    damaged["index_sha256"] = _digest({"schema_version": damaged["schema_version"],
                                       "entries": damaged["entries"], "claims": damaged["claims"]})
    path.write_text(json.dumps(damaged))
    with pytest.raises(QueueManifestError):
        screen_rows(path, [row])


def missing(row):
    return {"candidate_sha256": _digest(row), "target_fingerprint": row["target_fingerprint"],
            "source": row["source"], "kind": "missing_verified",
            "lookup": {"complete": True, "result_limited": False, "keys": [],
                       "source": row["source"], "target_fingerprint": row["target_fingerprint"]}}


def dry_result(manifest):
    return {"dryRun": True, "total": len(manifest["rows"]), "writeEnabled": True,
            "results": [{"operationId": row["operation_id"], "entitySet": row["entity_set"],
                         "postPlan": {"operationId": row["post_operation_id"]}}
                        for row in manifest["rows"]]}


def claim_ready(path, rows, batch="batch-one"):
    for row in rows:
        import_evidence(path, row, missing(row))
    return prepare_claim(path, rows, batch_id=batch)


def _race_apply(path, manifest, receipt):
    try:
        apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                    template_id="synthetic-bank", template_version="1")
        return "applied"
    except QueueManifestError:
        return "blocked"


def _race_import(path, row):
    try:
        import_evidence(path, row, evidence(row))
        return "imported"
    except QueueManifestError:
        return "blocked"


def test_import_apply_race_serializes_exclusively(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    with multiprocessing.Pool(2) as pool:
        applying = pool.apply_async(_race_apply, (path, manifest, receipt))
        importing = pool.apply_async(_race_import, (path, row))
        outcomes = {applying.get(timeout=10), importing.get(timeout=10)}
    assert outcomes in ({"applied", "blocked"}, {"imported", "blocked"})
    saved = json.loads(path.read_text())
    if saved["claims"]["batch-one"]["stage"] == "dispatch_uncertain":
        assert saved["entries"][next(iter(saved["entries"]))]["status"] == "missing_verified"
    else:
        assert saved["entries"][next(iter(saved["entries"]))]["status"] == "found_existing_verified"


def test_competing_batches_and_dispatch_uncertain_are_durable(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    assert screen_rows(path, [row])["held_count_by_reason"] == {"claim_reserved": 1}
    with pytest.raises(QueueManifestError, match="reserved"):
        prepare_claim(path, [row], batch_id="batch-two")
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    args = apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                       template_id="synthetic-bank", template_version="1")
    assert args["items"][0]["operation_id"] == manifest["rows"][0]["operation_id"]
    assert json.loads(path.read_text())["claims"]["batch-one"]["stage"] == "dispatch_uncertain"
    assert screen_rows(path, [row])["held_count_by_reason"] == {"dispatch_uncertain": 1}
    with pytest.raises(QueueManifestError, match="reserved"):
        prepare_claim(path, [row], batch_id="batch-two")
    with pytest.raises(QueueManifestError, match="dispatch uncertain"):
        prepare_claim(path, [row], batch_id="batch-one")
    with pytest.raises(QueueManifestError, match="dispatch uncertain"):
        cancel_claim(path, batch_id="batch-one", plan_sha256=manifest["plan_sha256"])
    with pytest.raises(QueueManifestError, match="dispatch uncertain"):
        import_evidence(path, row, evidence(row))
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                    template_id="synthetic-bank", template_version="1")


def test_stale_screen_manifest_preview_and_import_apply_race(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    assert screen_rows(path, [row])["ready_count"] == 0
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    assert import_evidence(path, row, evidence(row))["status"] == "found_existing_verified"
    with pytest.raises(QueueManifestError, match="changed"):
        apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                    template_id="synthetic-bank", template_version="1")
    with pytest.raises(QueueManifestError, match="changed"):
        preview_claim(path, manifest)
    with pytest.raises(QueueManifestError, match="changed"):
        prepare_claim(path, [row], batch_id="batch-one")
    with pytest.raises(QueueManifestError, match="reserved"):
        prepare_claim(path, [row], batch_id="new-batch")
    assert json.loads(path.read_text())["claims"]["batch-one"]["stage"] == "verified_preview"


def test_unrelated_entry_update_does_not_block_claim(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    first, second = candidate(), candidate("2")
    manifest = claim_ready(path, [first])
    import_evidence(path, second, evidence(second, key="key-2"))
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    assert apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                       template_id="synthetic-bank", template_version="1")["dry_run"] is False


def test_claim_rejects_changed_manifest_receipt_target_and_preview(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    with pytest.raises(QueueManifestError):
        verify_preview_claim(path, manifest, {"items": []}, dry_result(manifest))
    request = preview_claim(path, manifest)
    bad = copy.deepcopy(request)
    bad["items"][0]["payload"]["Amount"] = "99"
    with pytest.raises(QueueManifestError):
        verify_preview_claim(path, manifest, bad, dry_result(manifest))
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, receipt, target_fingerprint="other",
                    template_id="synthetic-bank", template_version="1")
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, {**receipt, "plan_sha256": "0" * 64},
                    target_fingerprint="test-a", template_id="synthetic-bank", template_version="1")
    altered = copy.deepcopy(manifest)
    altered["plan_sha256"] = "0" * 64
    with pytest.raises(QueueManifestError):
        preview_claim(path, altered)
    assert json.loads(path.read_text())["claims"]["batch-one"]["stage"] == "verified_preview"


def test_cancel_before_dispatch_requires_exact_owner(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    with pytest.raises(QueueManifestError):
        cancel_claim(path, batch_id="batch-one", plan_sha256="0" * 64)
    cancel_claim(path, batch_id="batch-one", plan_sha256=manifest["plan_sha256"])
    assert prepare_claim(path, [row], batch_id="batch-two")["batch_id"] == "batch-two"


def test_v2_migrates_under_lock_and_preserves_verified_entry(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    import_evidence(path, row, evidence(row))
    old = json.loads(path.read_text())
    core = {"schema_version": 2, "entries": old["entries"]}
    path.write_text(json.dumps({**core, "index_sha256": _digest(core)}))
    assert screen_rows(path, [row])["found_existing_verified"] == 1
    migrated = json.loads(path.read_text())
    assert migrated["schema_version"] == 4 and migrated["claims"] == {}
    assert migrated["entries"] == old["entries"]


def test_claim_limits_and_integrity(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate(str(i)) for i in range(1, 52)]
    for row in rows:
        import_evidence(path, row, missing(row))
    assert len(prepare_claim(path, rows[:50], batch_id="fifty")["rows"]) == 50
    with pytest.raises(QueueManifestError, match="1..50"):
        prepare_claim(path, rows, batch_id="fifty-one")
    huge = candidate("99")
    huge["prepared"]["payload"]["Blob"] = "x" * 1_000_000
    import_evidence(path, huge, missing(huge))
    with pytest.raises(QueueManifestError, match="1 MB"):
        prepare_claim(path, [huge], batch_id="huge")
    saved = json.loads(path.read_text())
    saved["claims"]["fifty"]["rows"][0]["evidence_sha256"] = "0" * 64
    path.write_text(json.dumps(saved))
    with pytest.raises(QueueManifestError, match="schema"):
        screen_rows(path, rows[:1])


def successful_resolution(manifest, rows):
    result_rows = []
    for i, planned in enumerate(manifest["rows"]):
        result_rows.append({"operationId": planned["operation_id"],
                            "postOperationId": planned["post_operation_id"],
                            "key": f"key-{i + 1}", "ok": True, "state": "posted"})
    result = {"dryRun": False, "total": len(rows), "succeeded": len(rows),
              "failed": 0, "needsReconciliation": 0, "results": result_rows}
    receipt = record_result(manifest, result)
    proofs = []
    for i, row in enumerate(rows):
        proof = evidence(row, kind="newly_created_verified", key=f"key-{i + 1}")
        proof["manifest"] = manifest
        proof["result_receipt"] = receipt
        proofs.append(proof)
    return result, proofs


def dispatched(path, rows, batch="batch-one"):
    manifest = claim_ready(path, rows, batch=batch)
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, dry_result(manifest))
    apply_claim(path, manifest, receipt, target_fingerprint="test-a",
                template_id="synthetic-bank", template_version="1")
    return manifest


def test_reprepare_binds_source_candidate_and_order(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate("1"), candidate("2")]
    manifest = claim_ready(path, rows)
    assert prepare_claim(path, rows, batch_id="batch-one") == manifest
    changed_source = copy.deepcopy(rows)
    changed_source[0]["source"]["line"] = "10"
    with pytest.raises(QueueManifestError):
        prepare_claim(path, changed_source, batch_id="batch-one")
    changed_payload = copy.deepcopy(rows)
    changed_payload[0]["source"]["statement"] = "8"
    changed_payload[0]["prepared"]["payload"]["Amount"] = "99"
    with pytest.raises(QueueManifestError):
        prepare_claim(path, changed_payload, batch_id="batch-one")
    with pytest.raises(QueueManifestError):
        prepare_claim(path, list(reversed(rows)), batch_id="batch-one")


def test_verified_preview_reissues_after_cli_output_failure(tmp_path, monkeypatch):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = claim_ready(path, [row])
    request = preview_claim(path, manifest)
    result = dry_result(manifest)
    from onec_odata_mcp import queue_checkpoint as module
    original = module.write_private_json
    monkeypatch.setattr(module, "write_private_json", lambda *args: (_ for _ in ()).throw(OSError("output failed")))
    from onec_odata_mcp.queue_manifest import write_manifest, write_private_json
    write_manifest(tmp_path / "manifest.json", manifest)
    write_private_json(tmp_path / "request.json", request)
    write_private_json(tmp_path / "result.json", result)
    with pytest.raises(SystemExit):
        main(["verify-preview", "--checkpoint", str(path), "--manifest", str(tmp_path / "manifest.json"),
              "--request", str(tmp_path / "request.json"), "--result", str(tmp_path / "result.json"),
              "--output", str(tmp_path / "receipt.json")])
    monkeypatch.setattr(module, "write_private_json", original)
    before = path.read_bytes()
    receipt = verify_preview_claim(path, manifest, request, result)
    assert path.read_bytes() == before
    assert receipt["dry_run_verified"] is True
    with pytest.raises(QueueManifestError, match="differs"):
        verify_preview_claim(path, manifest, request, {**result, "elapsedMs": 1})


def test_resolve_all_rows_atomically_and_keep_tombstone(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate("1"), candidate("2")]
    manifest = dispatched(path, rows)
    result, proofs = successful_resolution(manifest, rows)
    assert resolve_claim(path, manifest, rows, result, proofs)["newly_created_verified"] == 2
    before = path.read_bytes()
    assert resolve_claim(path, manifest, rows, result, proofs)["status"] == "resolved"
    assert path.read_bytes() == before
    assert screen_rows(path, rows)["newly_created_verified"] == 2
    with pytest.raises(QueueManifestError):
        prepare_claim(path, rows, batch_id="batch-one")
    with pytest.raises(QueueManifestError):
        import_evidence(path, rows[0], proofs[0])
    with pytest.raises(QueueManifestError):
        cancel_claim(path, batch_id="batch-one", plan_sha256=manifest["plan_sha256"])
    with pytest.raises(QueueManifestError):
        resolve_claim(path, manifest, rows, result, list(reversed(proofs)))


@pytest.mark.parametrize("damage", [
    lambda result, proofs: result["results"][0].update(ok=False, state="needs_reconciliation"),
    lambda result, proofs: proofs[0]["readback"].update(Amount="99"),
    lambda result, proofs: proofs[0]["active_movements"].update(complete=False),
    lambda result, proofs: proofs[0].update(target_key="key-2"),
    lambda result, proofs: proofs[0].update(result_receipt={}),
])
def test_resolution_holds_entire_batch_on_uncertain_or_conflicting_evidence(tmp_path, damage):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate("1"), candidate("2")]
    manifest = dispatched(path, rows)
    result, proofs = successful_resolution(manifest, rows)
    damage(result, proofs)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        resolve_claim(path, manifest, rows, result, proofs)
    assert path.read_bytes() == before
    assert screen_rows(path, rows)["held_count_by_reason"] == {"dispatch_uncertain": 2}


def test_resolution_rejects_other_batch_and_target_collision(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest = dispatched(path, [row])
    result, proofs = successful_resolution(manifest, [row])
    foreign = copy.deepcopy(manifest)
    foreign["batch_id"] = "different"
    with pytest.raises(QueueManifestError):
        resolve_claim(path, foreign, [row], result, proofs)
    other = candidate("2")
    import_evidence(path, other, evidence(other, key="key-1"))
    with pytest.raises(QueueManifestError, match="conflicts"):
        resolve_claim(path, manifest, [row], result, proofs)


def _race_resolve(path, manifest, rows, result, proofs):
    try:
        return resolve_claim(path, manifest, rows, result, proofs)["status"]
    except QueueManifestError:
        return "blocked"


def test_parallel_resolution_same_evidence_is_idempotent(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate()]
    manifest = dispatched(path, rows)
    result, proofs = successful_resolution(manifest, rows)
    with multiprocessing.Pool(2) as pool:
        outcomes = pool.starmap(_race_resolve, [(path, manifest, rows, result, proofs)] * 2)
    assert outcomes == ["resolved", "resolved"]
    assert screen_rows(path, rows)["newly_created_verified"] == 1


def test_cli_resolve_uses_saved_artifacts_and_different_batch_cannot_reclaim(tmp_path, capsys):
    from onec_odata_mcp.queue_manifest import write_manifest, write_private_json
    path = tmp_path / "private" / "checkpoint.json"
    rows = [candidate()]
    manifest = dispatched(path, rows)
    result, proofs = successful_resolution(manifest, rows)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    write_manifest(artifacts / "manifest.json", manifest)
    write_private_json(artifacts / "candidates.json", {"candidates": rows})
    write_private_json(artifacts / "result.json", result)
    write_private_json(artifacts / "evidence.json", {"evidence": proofs})
    argv = ["resolve", "--checkpoint", str(path), "--manifest", str(artifacts / "manifest.json"),
            "--candidates", str(artifacts / "candidates.json"),
            "--result", str(artifacts / "result.json"),
            "--evidence", str(artifacts / "evidence.json")]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["newly_created_verified"] == 1
    assert main(argv) == 0
    with pytest.raises(QueueManifestError):
        prepare_claim(path, rows, batch_id="batch-two")
