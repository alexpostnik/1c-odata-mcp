import hashlib
import json

import pytest

from onec_odata_mcp.queue_manifest import (
    QueueManifestError,
    apply_request,
    compile_manifest,
    dry_run_request,
    main,
    read_manifest,
    record_result,
    resume_request,
    summarize_result,
    validate_manifest,
    verify_dry_run,
    write_manifest,
)


def plan(rows=None, **overrides):
    return compile_manifest(
        rows or [{
            "source_id": "source-001",
            "entity_set": "Document_Example",
            "payload": {"Number": "SYN-001", "Amount": "12.50"},
            "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example", "amount": "12.50",
                "debit": "synthetic-debit", "credit": "synthetic-credit",
            },
        }],
        batch_id="synthetic-batch-1",
        target_fingerprint="fingerprint-test-target",
        template_id="synthetic-template",
        template_version="1",
        **overrides,
    )


def dry_result(manifest):
    return {
        "dryRun": True,
        "writeEnabled": True,
        "total": len(manifest["rows"]),
        "results": [{
            "operationId": row["operation_id"],
            "postPlan": ({"operationId": row["post_operation_id"]}
                         if row.get("post") else None),
            "entitySet": row["entity_set"],
        } for row in manifest["rows"]],
    }


def apply_result(manifest, state="posted"):
    return {
        "dryRun": False,
        "total": len(manifest["rows"]),
        "succeeded": len(manifest["rows"]) if state == "posted" else 0,
        "failed": 0,
        "needsReconciliation": 0 if state == "posted" else len(manifest["rows"]),
        "elapsedMs": 123.4,
        "results": [{
            "operationId": row["operation_id"],
            "postOperationId": row.get("post_operation_id"),
            "ok": state == "posted",
            "state": state,
        } for row in manifest["rows"]],
    }


def test_compile_is_deterministic_and_queue_ids_stay_stable_for_same_batch_rows():
    first = plan()
    second = plan()
    assert first == second
    assert first["rows"][0]["operation_id"] == second["rows"][0]["operation_id"]
    assert len(first["rows"][0]["operation_id"]) in range(8, 129)


def test_supplier_alternative_manifest_roundtrip_and_digest(tmp_path):
    row = {
        "source_id": "synthetic-supplier-001", "entity_set": "Document_Example",
        "payload": {"Number": "SYN-001"}, "post": True,
        "expected_accounting_movements": {
            "register": "AccountingRegister_Example",
            "rows": [{"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
        },
    }
    manifest = plan([row])
    path = tmp_path / "supplier-manifest.json"
    write_manifest(path, manifest)
    loaded = read_manifest(path)
    assert loaded == manifest
    assert dry_run_request(loaded)["tool_arguments"]["items"][0][
        "expected_accounting_movements"
    ] == row["expected_accounting_movements"]
    changed = plan([{**row, "expected_accounting_movements": {
        **row["expected_accounting_movements"],
        "rows": [{"amount": "90", "debitAnyOf": ["60.1"], "credit": "51"}],
    }}])
    assert changed["rows"][0]["post_operation_id"] == manifest["rows"][0]["post_operation_id"]
    assert changed["plan_sha256"] != manifest["plan_sha256"]


@pytest.mark.parametrize("alternatives", [
    [], ["60.1", "60.1"], [""], ["60.1", " "], ["60.1", 60.2],
    ["60.1", True], ["60.1", {"code": "60.2"}], "60.1", None,
    ["abcdefab-0000-0000-0000-000000000001",
     "ABCDEFAB-0000-0000-0000-000000000001"],
])
def test_manifest_rejects_bad_debit_alternatives(alternatives):
    with pytest.raises(QueueManifestError, match="debitAnyOf"):
        plan([{
            "source_id": "s", "entity_set": "Document_Example",
            "payload": {"Number": "SYN"}, "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example",
                "rows": [{"amount": "90", "debitAnyOf": alternatives, "credit": "51"}],
            },
        }])


def test_manifest_rejects_combined_scalar_and_alternative_debit():
    with pytest.raises(QueueManifestError, match="debitAnyOf"):
        plan([{
            "source_id": "s", "entity_set": "Document_Example",
            "payload": {"Number": "SYN"}, "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example",
                "rows": [{"amount": "90", "debit": "60.1",
                          "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
            },
        }])


@pytest.mark.parametrize("alternative_first", [True, False])
def test_manifest_rejects_multirow_debit_alternative(alternative_first):
    alternative = {"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}
    overlapping_scalar = {"amount": "90", "debit": "60.1", "credit": "51"}
    rows = [alternative, overlapping_scalar] if alternative_first else [overlapping_scalar, alternative]
    with pytest.raises(QueueManifestError, match="debitAnyOf.*one movement row"):
        plan([{
            "source_id": "s", "entity_set": "Document_Example",
            "payload": {"Number": "SYN"}, "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example", "rows": rows,
            },
        }])


def test_manifest_keeps_scalar_multirow_expectation():
    rows = [
        {"amount": "40", "debit": "60.1", "credit": "51"},
        {"amount": "50", "debit": "60.2", "credit": "51"},
    ]
    manifest = plan([{
        "source_id": "s", "entity_set": "Document_Example",
        "payload": {"Number": "SYN"}, "post": True,
        "expected_accounting_movements": {
            "register": "AccountingRegister_Example", "rows": rows,
        },
    }])
    assert manifest["rows"][0]["expected_accounting_movements"]["rows"] == rows


@pytest.mark.parametrize("relaxation", [
    {"allowExtraRows": True}, {"tolerance": "0.01"},
])
def test_manifest_rejects_relaxed_debit_alternative(relaxation):
    with pytest.raises(QueueManifestError, match="debitAnyOf"):
        plan([{
            "source_id": "s", "entity_set": "Document_Example",
            "payload": {"Number": "SYN"}, "post": True,
            "expected_accounting_movements": {
                "register": "AccountingRegister_Example",
                "rows": [{"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
                **relaxation,
            },
        }])


def test_changed_plan_keeps_ids_but_changes_digest_and_is_not_silently_applied():
    original = plan()
    changed = plan([{
        "source_id": "source-001", "entity_set": "Document_Example",
        "payload": {"Number": "SYN-002"}, "post": True,
    }])
    assert original["rows"][0]["operation_id"] == changed["rows"][0]["operation_id"]
    assert original["plan_sha256"] != changed["plan_sha256"]
    receipt = verify_dry_run(
        original, dry_result(original), dry_run_request(original)["tool_arguments"]
    )
    with pytest.raises(QueueManifestError, match="verified dry-run receipt"):
        apply_request(
            changed, receipt,
            target_fingerprint="fingerprint-test-target",
            template_id="synthetic-template", template_version="1",
        )


@pytest.mark.parametrize(
    ("target", "template_id", "version", "message"),
    [
        ("different-target", "synthetic-template", "1", "target fingerprint changed"),
        ("fingerprint-test-target", "other-template", "1", "template changed"),
        ("fingerprint-test-target", "synthetic-template", "2", "template changed"),
    ],
)
def test_apply_rejects_changed_target_or_template(target, template_id, version, message):
    manifest = plan()
    with pytest.raises(QueueManifestError, match=message):
        apply_request(
            manifest, {}, target_fingerprint=target,
            template_id=template_id, template_version=version,
        )


def test_dry_run_and_apply_are_separate_and_apply_reuses_exact_ids():
    manifest = plan()
    preview_envelope = dry_run_request(manifest)
    preview = preview_envelope["tool_arguments"]
    assert preview["dry_run"] is True
    assert "confirm" not in preview
    receipt = verify_dry_run(manifest, dry_result(manifest), preview)
    applied = apply_request(
        manifest, receipt,
        target_fingerprint="fingerprint-test-target",
        template_id="synthetic-template", template_version="1",
    )
    assert applied["dry_run"] is False
    assert applied["confirm"] is True
    assert applied["expected_target"] == "fingerprint-test-target"
    assert applied["items"] == preview["items"]


def test_resume_keeps_ids_and_rejects_uncertain_rows():
    manifest = plan()
    result_receipt = record_result(manifest, apply_result(manifest))
    resumed = resume_request(
        manifest, result_receipt,
        target_fingerprint="fingerprint-test-target",
        template_id="synthetic-template", template_version="1",
    )
    assert resumed == dry_run_request(manifest)
    uncertain = apply_result(manifest, state="needs_reconciliation")
    uncertain["needsReconciliation"] = 1
    uncertain_receipt = record_result(manifest, uncertain)
    with pytest.raises(QueueManifestError, match="reconciliation"):
        resume_request(
            manifest, uncertain_receipt, target_fingerprint="fingerprint-test-target",
            template_id="synthetic-template", template_version="1",
        )


def test_summary_reports_outcomes_without_changing_queue_result():
    manifest = plan()
    result = apply_result(manifest)
    summary = summarize_result(manifest, result)
    assert summary == {
        "planSha256": manifest["plan_sha256"], "total": 1,
        "succeeded": 1, "failed": 0, "needsReconciliation": 0,
        "elapsedMs": 123.4,
    }
    assert result["results"][0]["state"] == "posted"


def test_manifest_file_is_private_validated_and_never_overwritten(tmp_path):
    manifest = plan()
    path = tmp_path / "prepared.json"
    write_manifest(path, manifest)
    assert read_manifest(path) == manifest
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        write_manifest(path, manifest)
    modified = json.loads(path.read_text())
    modified["target_fingerprint"] = "other-target"
    path.write_text(json.dumps(modified))
    with pytest.raises(QueueManifestError, match="digest"):
        read_manifest(path)


def test_cyrillic_entity_set_survives_compile_validate_preview_and_apply_preparation():
    entity_set = "Document_ПлатежноеПоручение"
    manifest = plan([{
        "source_id": "source-cyrillic-001",
        "entity_set": entity_set,
        "payload": {"Number": "SYN-CYR-001"},
        "post": True,
    }])
    assert validate_manifest(manifest) == manifest
    assert manifest["rows"][0]["entity_set"] == entity_set

    preview = dry_run_request(manifest)["tool_arguments"]
    assert preview["items"][0]["entity_set"] == entity_set
    receipt = verify_dry_run(manifest, dry_result(manifest), preview)
    prepared_apply = apply_request(
        manifest, receipt,
        target_fingerprint="fingerprint-test-target",
        template_id="synthetic-template", template_version="1",
    )
    assert prepared_apply["items"][0]["entity_set"] == entity_set
    assert prepared_apply["items"][0]["operation_id"] == manifest["rows"][0]["operation_id"]


@pytest.mark.parametrize(
    "entity_set",
    [
        "Document_",
        "Document_Платежное Поручение",
        "Document_Платежное/Поручение",
        "Document_Платежное?Поручение",
        "Document_Платежное#Поручение",
        "Document_Платежное\nПоручение",
        "Document_Платежное\x01Поручение",
        "Document_" + "А" * 249,
    ],
)
def test_compile_rejects_invalid_unicode_entity_set(entity_set):
    with pytest.raises(QueueManifestError, match="Document_.*identifier"):
        plan([{"source_id": "s", "entity_set": entity_set, "payload": {"Number": "SYN"}}])


def test_validate_rejects_invalid_entity_set_even_with_recomputed_digest():
    manifest = plan()
    manifest["rows"][0]["entity_set"] = "Document_Платежное/Поручение"
    body = {key: value for key, value in manifest.items() if key != "plan_sha256"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with pytest.raises(QueueManifestError, match="entity_set is invalid"):
        validate_manifest(manifest)


def test_compile_enforces_queue_item_count_size_and_concurrency():
    many = [{
        "source_id": f"s-{index:03d}", "entity_set": "Document_Example",
        "payload": {"Number": f"SYN-{index:03d}"},
    } for index in range(51)]
    with pytest.raises(QueueManifestError, match="1..50"):
        plan(many)
    with pytest.raises(QueueManifestError, match="1 to 4"):
        plan(max_concurrency=5)
    oversized = [{
        "source_id": "source-001", "entity_set": "Document_Example",
        "payload": {"Text": "x" * 1_000_001},
    }]
    with pytest.raises(QueueManifestError, match="1 MB"):
        plan(oversized)


def test_compile_rejects_duplicate_source_ids_and_unclassified_shapes():
    duplicate = [
        {"source_id": "same", "entity_set": "Document_A", "payload": {"Number": "SYN"}},
        {"source_id": "same", "entity_set": "Document_A", "payload": {"Number": "SYN"}},
    ]
    with pytest.raises(QueueManifestError, match="unique"):
        plan(duplicate)
    with pytest.raises(QueueManifestError, match=r"Document_\*"):
        plan([{"source_id": "s", "entity_set": "Catalog_Example", "payload": {}}])


def test_compile_rejects_empty_payload_and_unsupported_source_fields():
    with pytest.raises(QueueManifestError, match="nonempty"):
        plan([{"source_id": "s", "entity_set": "Document_Example", "payload": {}}])
    with pytest.raises(QueueManifestError, match="unsupported source fields"):
        plan([{
            "source_id": "s", "entity_set": "Document_Example",
            "payload": {"Number": "SYN"}, "unexpected": True,
        }])


def test_cli_runs_offline_compile_preview_apply_summary_and_same_id_resume(tmp_path, capsys):
    input_path = tmp_path / "rows.json"
    manifest_path = tmp_path / "manifest.json"
    request_path = tmp_path / "preview-args.json"
    response_path = tmp_path / "preview-result.json"
    preview_receipt_path = tmp_path / "preview-receipt.json"
    apply_response_path = tmp_path / "apply-result.json"
    result_receipt_path = tmp_path / "result-receipt.json"
    input_path.write_text(json.dumps({"rows": [{
        "source_id": "source-cli-001", "entity_set": "Document_Example",
        "payload": {"Number": "SYN-CLI-001"},
    }]}))

    assert main([
        "compile", "--input", str(input_path), "--output", str(manifest_path),
        "--batch-id", "batch-cli-001", "--target-fingerprint", "fingerprint-test",
        "--template-id", "template-test", "--template-version", "1",
    ]) == 0
    capsys.readouterr()
    assert main(["dry-run", "--manifest", str(manifest_path), "--save-request", str(request_path)]) == 0
    preview_args = json.loads(capsys.readouterr().out)
    assert json.loads(request_path.read_text()) == preview_args
    operation_id = preview_args["items"][0]["operation_id"]
    response_path.write_text(json.dumps({
        "dryRun": True, "writeEnabled": True, "total": 1,
        "results": [{"operationId": operation_id, "entitySet": "Document_Example",
                     "postPlan": None}],
    }))
    assert main([
        "verify-dry-run", "--manifest", str(manifest_path), "--request", str(request_path),
        "--result", str(response_path), "--output", str(preview_receipt_path),
    ]) == 0
    capsys.readouterr()
    apply_args = [
        "apply", "--manifest", str(manifest_path), "--preview-receipt", str(preview_receipt_path),
        "--target-fingerprint", "fingerprint-test", "--template-id", "template-test",
        "--template-version", "1",
    ]
    assert main(apply_args) == 0
    emitted_apply = json.loads(capsys.readouterr().out)
    assert emitted_apply["items"][0]["operation_id"] == operation_id
    assert emitted_apply["dry_run"] is False and emitted_apply["confirm"] is True

    apply_response_path.write_text(json.dumps({
        "dryRun": False, "total": 1, "succeeded": 1, "failed": 0,
        "needsReconciliation": 0, "elapsedMs": 20,
        "results": [{"operationId": operation_id, "ok": True,
                     "state": "created", "elapsedMs": 20}],
    }))
    assert main([
        "record-result", "--manifest", str(manifest_path), "--result", str(apply_response_path),
        "--output", str(result_receipt_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["succeeded"] == 1
    assert main([
        "resume", "--manifest", str(manifest_path), "--result-receipt", str(result_receipt_path),
        "--target-fingerprint", "fingerprint-test", "--template-id", "template-test",
        "--template-version", "1",
    ]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["items"][0]["operation_id"] == operation_id
    assert resumed["dry_run"] is True


def test_preview_and_result_receipts_reject_mismatched_rows_and_preview_as_apply():
    manifest = plan()
    bad_preview = dry_result(manifest)
    bad_preview["results"][0]["postPlan"]["operationId"] = "different-post-id"
    with pytest.raises(QueueManifestError, match="Post operation ID"):
        verify_dry_run(manifest, bad_preview, dry_run_request(manifest)["tool_arguments"])
    preview_as_apply = dry_result(manifest)
    with pytest.raises(QueueManifestError, match="completed apply"):
        record_result(manifest, preview_as_apply)
    mismatched_counts = apply_result(manifest)
    mismatched_counts["failed"] = 1
    with pytest.raises(QueueManifestError, match="counters"):
        record_result(manifest, mismatched_counts)
    with pytest.raises(QueueManifestError, match="row must be an object"):
        verify_dry_run(
            manifest, {**dry_result(manifest), "results": [None]},
            dry_run_request(manifest)["tool_arguments"],
        )
    disabled = dry_result(manifest)
    disabled["writeEnabled"] = False
    with pytest.raises(QueueManifestError, match="disabled"):
        verify_dry_run(manifest, disabled, dry_run_request(manifest)["tool_arguments"])
