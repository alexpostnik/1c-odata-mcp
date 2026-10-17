"""Synthetic single-card Catalog claims through existing generic Create receipts."""
import copy
import json

import pytest

from onec_odata_mcp.ledger import request_digest
from onec_odata_mcp.queue_checkpoint import (
    import_evidence, screen_rows, prepare_claim, preview_claim, verify_preview_claim,
    apply_claim, cancel_claim, resolve_claim,
)
from onec_odata_mcp.queue_manifest import (
    QueueManifestError, _digest, _catalog_payload_summary, compile_manifest,
    dry_run_request, validate_manifest, record_result, resume_request, manifest_tool,
)


KEY = "00000000-0000-0000-0000-000000000001"


def candidate(identity="1", payload=None):
    return {"workflow": "catalog_prerequisites",
            "source": {"namespace": "synthetic-hr", "date": "2010-03-03",
                       "statement": "Catalog_People", "line": identity},
            "target_fingerprint": "synthetic-test-target",
            "template": {"id": "synthetic-hr-scalar", "version": "1"},
            "prepared": {"source_id": f"person-{identity}", "entity_set": "Catalog_ФизическиеЛица",
                         "payload": payload or {"Description": "SYNTHETIC_PERSON"}, "post": False}}


def absence(row):
    return {"candidate_sha256": _digest(row), "target_fingerprint": row["target_fingerprint"],
            "source": row["source"], "kind": "missing_verified",
            "lookup": {"complete": True, "result_limited": False, "keys": [],
                       "source": row["source"], "target_fingerprint": row["target_fingerprint"]}}


def preview(manifest):
    return {"dryRun": True, "writeEnabled": True,
            "plan": {"operation": "create", "entitySet": manifest["rows"][0]["entity_set"],
                     "key": None, "wouldWrite": True,
                     "targetFingerprint": manifest["target_fingerprint"],
                     "payloadSummary": _catalog_payload_summary(manifest["rows"][0])}}


def server_receipt(manifest, state="complete", key=KEY):
    row = manifest["rows"][0]
    return {"found": True, "operationId": row["operation_id"], "state": state,
            "request_hash": request_digest(manifest["target_fingerprint"], "create", row["entity_set"],
                                           None, {**row["payload"], "Ref_Key": "00000000-0000-0000-0000-000000000000"}, None),
            "result": {"entitySet": row["entity_set"], "key": key, "writeState": "accepted",
                       "readbackFetched": True, "readbackVerified": True}, "created_at": "synthetic"}


def found(row, key=KEY):
    return {**absence(row), "kind": "found_existing_verified", "target_key": key,
            "lookup": {**absence(row)["lookup"], "keys": [key]},
            "readback_context": {"entity_set": row["prepared"]["entity_set"],
                                 "target_fingerprint": row["target_fingerprint"]},
            "readback": {"Ref_Key": key, "DeletionMark": False, **row["prepared"]["payload"]}}


def dispatch(path, row, batch="synthetic-card-1"):
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id=batch)
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, preview(manifest))
    args = apply_claim(path, manifest, receipt, target_fingerprint=row["target_fingerprint"],
                       template_id=row["template"]["id"], template_version=row["template"]["version"])
    return manifest, args


def created_evidence(row, manifest, result):
    return {**found(row, result["result"].get("key", KEY)), "kind": "newly_created_verified",
            "manifest": manifest, "result_receipt": record_result(manifest, result)}


def test_catalog_claim_full_cycle_uses_existing_create_and_receipt(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest, args = dispatch(path, row)
    assert manifest_tool(manifest) == "onec83_create_entity"
    assert "items" not in args and args["operation_id"] == manifest["rows"][0]["operation_id"]
    assert args["confirm"] is True and "post" not in args
    assert json.loads(path.read_text())["claims"][manifest["batch_id"]]["stage"] == "dispatch_uncertain"
    result = server_receipt(manifest)
    proof = created_evidence(row, manifest, result)
    assert resolve_claim(path, manifest, [row], result, [proof])["status"] == "resolved"
    before = path.read_bytes()
    assert resolve_claim(path, manifest, [row], result, [proof])["status"] == "resolved"
    assert path.read_bytes() == before
    assert screen_rows(path, [row])["newly_created_verified"] == 1
    with pytest.raises(QueueManifestError, match="resolved|reserved"):
        prepare_claim(path, [row], batch_id="replacement-batch")


@pytest.mark.parametrize("entity", ["Catalog_", "Catalog_Example/Rows", "Catalog_Example?x", "https://Catalog_Example", "Catalog_" + "x" * 256, "Document_Example", "InformationRegister_Example"])
def test_catalog_rejects_wrong_and_path_entity_sets(entity):
    row = candidate()["prepared"]
    row["entity_set"] = entity
    with pytest.raises(QueueManifestError):
        compile_manifest([row], batch_id="b", target_fingerprint="t", template_id="tpl",
                         template_version="1", workflow="catalog_prerequisites")


def test_catalog_is_explicit_and_never_weakens_document_compiler():
    with pytest.raises(QueueManifestError, match="Document"):
        compile_manifest([candidate()["prepared"]], batch_id="b", target_fingerprint="t",
                         template_id="tpl", template_version="1")


@pytest.mark.parametrize("change", [
    {"post": True}, {"expected_accounting_movements": None},
    {"payload": {"Description": {"enum": "value"}}}, {"payload": {"Rows": []}},
    {"payload": {"Ref_Key": KEY}}, {"payload": {"Posted": False}},
    {"payload": {"DeletionMark": True}}, {"payload": {"Description": float("nan")}},
])
def test_unsupported_catalog_options_rejected_before_claim(tmp_path, change):
    row = candidate()
    row["prepared"].update(change)
    path = tmp_path / "private" / "checkpoint.json"
    with pytest.raises(QueueManifestError):
        import_evidence(path, row, absence(row))
    assert not path.exists()


def test_catalog_claims_are_single_card_and_document_candidates_cannot_opt_in(tmp_path):
    row = candidate()
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, row, absence(row))
    import_evidence(path, candidate("2"), absence(candidate("2")))
    for rows, concurrency in (([row, candidate("2")], 1), ([row], 2)):
        with pytest.raises(QueueManifestError, match="exactly one"):
            prepare_claim(path, rows, batch_id="b", max_concurrency=concurrency)
    malformed = copy.deepcopy(row)
    del malformed["workflow"]
    with pytest.raises(QueueManifestError):
        import_evidence(path, malformed, absence(malformed))


def test_duplicate_claim_and_other_batch_collision_preserve_evidence(tmp_path):
    row = candidate()
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="first")
    assert prepare_claim(path, [row], batch_id="first") == manifest
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="reserved"):
        prepare_claim(path, [row], batch_id="second")
    assert path.read_bytes() == before
    # Another independently mapped card can still be claimed.
    second = candidate("2")
    import_evidence(path, second, absence(second))
    assert prepare_claim(path, [second], batch_id="independent")["batch_id"] == "independent"


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_uncertain_or_refused_receipt_never_resolves_or_releases_claim(tmp_path, state):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest, args = dispatch(path, row)
    result = server_receipt(manifest, state=state)
    proof = created_evidence(row, manifest, result)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="not fully verified"):
        resolve_claim(path, manifest, [row], result, [proof])
    with pytest.raises(QueueManifestError, match="not complete"):
        resume_request(manifest, record_result(manifest, result), target_fingerprint=row["target_fingerprint"],
                       template_id=row["template"]["id"], template_version="1")
    with pytest.raises(QueueManifestError):
        cancel_claim(path, batch_id=manifest["batch_id"], plan_sha256=manifest["plan_sha256"])
    with pytest.raises(QueueManifestError):
        import_evidence(path, row, absence(row))
    assert path.read_bytes() == before


def test_resume_after_addressed_reconciliation_is_read_only_then_resolve(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    manifest, _ = dispatch(path, row)
    result = server_receipt(manifest)
    result["result"].update({"writeState": "reconciled_present", "readbackVerified": False})
    request = resume_request(manifest, record_result(manifest, result), target_fingerprint=row["target_fingerprint"],
                             template_id=row["template"]["id"], template_version="1")
    assert request["tool_arguments"]["dry_run"] is True
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, {}, target_fingerprint=row["target_fingerprint"],
                    template_id=row["template"]["id"], template_version="1")
    assert resolve_claim(path, manifest, [row], result, [created_evidence(row, manifest, result)])["status"] == "resolved"


@pytest.mark.parametrize("field,value", [("operationId", "wrong-operation"), ("request_hash", "0" * 64), ("found", False)])
def test_catalog_receipt_binds_operation_and_exact_normalized_payload(tmp_path, field, value):
    row = candidate(payload={"Description": "SYNTHETIC", "BirthDate": "2010-03-03T00:00:00",
                             "Count": "7", "Amount": "12.50", "Optional": None, "IsFolder": False})
    manifest, _ = dispatch(tmp_path / "private" / "checkpoint.json", row)
    result = server_receipt(manifest)
    result[field] = value
    with pytest.raises(QueueManifestError, match="exact saved"):
        record_result(manifest, result)


@pytest.mark.parametrize("mutation", ["payload", "entity", "target", "deletion", "key"])
def test_bad_readback_never_accepts_completed_receipt(tmp_path, mutation):
    row = candidate()
    path = tmp_path / "private" / "checkpoint.json"
    manifest, _ = dispatch(path, row)
    result = server_receipt(manifest)
    proof = created_evidence(row, manifest, result)
    if mutation == "payload": proof["readback"]["Description"] = "WRONG"
    if mutation == "entity": proof["readback_context"]["entity_set"] = "Catalog_Employees"
    if mutation == "target": proof["readback_context"]["target_fingerprint"] = "other-test"
    if mutation == "deletion": proof["readback"]["DeletionMark"] = True
    if mutation == "key": proof["readback"]["Ref_Key"] = "another-key"
    with pytest.raises(QueueManifestError, match="not fully verified"):
        resolve_claim(path, manifest, [row], result, [proof])


def test_existing_catalog_identity_collision_and_stale_candidate_are_held(tmp_path):
    path = tmp_path / "private" / "checkpoint.json"
    row = candidate()
    assert import_evidence(path, row, found(row))["status"] == "found_existing_verified"
    second = candidate("2")
    assert import_evidence(path, second, found(second))["reason"] == "target_ambiguous"
    assert screen_rows(path, [row, second])["held_count_by_reason"] == {"target_ambiguous": 2}
    changed = candidate(payload={"Description": "CHANGED"})
    assert screen_rows(path, [changed])["held_count_by_reason"] == {"source_or_template_stale": 1}


def test_preview_normalization_change_is_rejected_before_dispatch(tmp_path):
    row = candidate(payload={"Description": "SYNTHETIC", "Counter": 7})
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="normalized")
    request = preview_claim(path, manifest)
    response = preview(manifest)
    response["plan"]["payloadSummary"]["shape"]["Counter"] = {"type": "string"}
    with pytest.raises(QueueManifestError, match="dry-run"):
        verify_preview_claim(path, manifest, request, response)
    assert json.loads(path.read_text())["claims"]["normalized"]["stage"] == "previewed"


# Exercise the unchanged production Bridge, real CSDL parser and SQLite ledger.
_CSDL = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
<edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">
<EntityType Name="Catalog_Person"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String"/><Property Name="DeletionMark" Type="Edm.Boolean"/>
<Property Name="BirthDate" Type="Edm.DateTime"/><Property Name="Counter" Type="Edm.Int64"/>
<Property Name="Amount" Type="Edm.Decimal"/><Property Name="Optional" Type="Edm.String"/>
</EntityType>
<EntityType Name="Catalog_Employee"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String"/><Property Name="DeletionMark" Type="Edm.Boolean"/>
<Property Name="Person_Key" Type="Edm.Guid"/>
<NavigationProperty Name="Person" Relationship="StandardODATA.Employee_Person" FromRole="Employee" ToRole="Person"/>
</EntityType>
<Association Name="Employee_Person"><End Role="Employee" Type="StandardODATA.Catalog_Employee" Multiplicity="*"/>
<End Role="Person" Type="StandardODATA.Catalog_Person" Multiplicity="1"/></Association>
<EntityContainer Name="Container"><EntitySet Name="Catalog_Person" EntityType="StandardODATA.Catalog_Person"/>
<EntitySet Name="Catalog_Employee" EntityType="StandardODATA.Catalog_Employee"/>
<AssociationSet Name="Employee_Person" Association="StandardODATA.Employee_Person">
<End Role="Employee" EntitySet="Catalog_Employee"/><End Role="Person" EntitySet="Catalog_Person"/>
</AssociationSet></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'''


class SyntheticClient:
    def __init__(self):
        from types import SimpleNamespace
        self.write_enabled = True
        self.config = SimpleNamespace(odata_base_url="https://synthetic.invalid/odata", alias="test", environment="test", username="synthetic")
        self.calls = []
        self.objects = {}
        self.fail = None
        self.readback_fail_once = False

    def create_entity(self, entity_set, payload, null_ref_key=False):
        from onec_odata_mcp.client import ODataTransportError, ODataHTTPError
        self.calls.append(("create", entity_set))
        if self.fail == "unknown":
            raise ODataTransportError("tls", attempts=1)
        if self.fail == "refused":
            raise ODataHTTPError(400, code="synthetic", message="synthetic refusal", definitive=True)
        key = f"00000000-0000-0000-0000-{len(self.objects) + 1:012d}"
        self.objects[(entity_set, key)] = {**payload, "Ref_Key": key, "DeletionMark": False}
        if self.fail == "accepted-unknown":
            raise ODataTransportError("tls", attempts=1)
        return {"Ref_Key": key}

    def get_entity(self, entity_set, key):
        from onec_odata_mcp.client import ODataHTTPError
        self.calls.append(("read", entity_set, key))
        if self.readback_fail_once:
            self.readback_fail_once = False
            raise ODataHTTPError(500, code="synthetic", message="unavailable", definitive=False)
        if (entity_set, key) not in self.objects:
            raise ODataHTTPError(404, code="synthetic", message="absent", definitive=True)
        return dict(self.objects[(entity_set, key)])

    def list_entities(self, entity_set, **kwargs):
        # Missing-reference fallback is bounded and empty; no real HTTP calls.
        self.calls.append(("list", entity_set))
        import re
        if "Description eq" in kwargs.get("filter", ""):
            return {"value": [{"Ref_Key": key} for (collection, key), obj in self.objects.items()
                              if collection == entity_set and obj.get("Description") == "SYNTHETIC_PERSON"]}
        keys = set(re.findall(r"guid'([^']+)'", kwargs.get("filter", "")))
        return {"value": [{"Ref_Key": key} for (collection, key) in self.objects
                          if collection == entity_set and key in keys]}


def bridge_fixture(tmp_path):
    from onec_odata_mcp.server import Bridge
    from onec_odata_mcp.metadata import MetadataModel
    from onec_odata_mcp.ledger import WriteLedger
    client = SyntheticClient()
    return Bridge(client, WriteLedger(tmp_path / "ledger.sqlite3"), MetadataModel.from_xml(_CSDL)), client


def applied_card(path, bridge, row, batch):
    row["target_fingerprint"] = bridge.target_fingerprint
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id=batch)
    request = preview_claim(path, manifest)
    response = bridge.create_entity(**request)
    receipt = verify_preview_claim(path, manifest, request, response)
    args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                       template_id=row["template"]["id"], template_version="1")
    response = bridge.create_entity(**args)
    result = bridge.get_write_receipt(args["operation_id"])
    return manifest, args, response, result


def test_real_bridge_catalog_cycle_dependencies_and_same_ledger_resume(tmp_path):
    bridge, client = bridge_fixture(tmp_path)
    checkpoint = tmp_path / "private" / "checkpoint.json"
    person = candidate(payload={"Description": "SYNTHETIC_PERSON", "Counter": "7", "Amount": "12.50",
                                "BirthDate": "2010-03-03T00:00:00", "Optional": None})
    person["prepared"]["entity_set"] = "Catalog_Person"
    manifest, args, response, result = applied_card(checkpoint, bridge, person, "person-batch")
    assert response["readbackVerified"] is True
    proof = created_evidence(person, manifest, result)
    proof["readback"] = response["readback"]
    assert resolve_claim(checkpoint, manifest, [person], result, [proof])["status"] == "resolved"
    call_count = len(client.calls)
    assert bridge.create_entity(**args)["replayedReceipt"] is True
    assert len(client.calls) == call_count
    employee = candidate(payload={"Description": "SYNTHETIC_EMPLOYEE", "Person_Key": result["result"]["key"]})
    employee["prepared"]["entity_set"] = "Catalog_Employee"
    assert employee["source"] == person["source"]
    employee["prerequisites"] = [{"candidate": person, "target_key": result["result"]["key"],
                                  "payload_field": "Person_Key"}]
    manifest2, _, response2, result2 = applied_card(checkpoint, bridge, employee, "employee-batch")
    assert response2["referenceValidation"]["status"] == "verified"
    assert response2["readback"]["Person_Key"] == result["result"]["key"]
    proof2 = created_evidence(employee, manifest2, result2)
    proof2["readback"] = response2["readback"]
    assert resolve_claim(checkpoint, manifest2, [employee], result2, [proof2])["status"] == "resolved"
    assert sum(call[0] == "create" for call in client.calls) == 2
    assert screen_rows(checkpoint, [person, employee])["newly_created_verified"] == 2


def test_real_bridge_missing_dependency_fails_before_create_and_holds_only_card(tmp_path):
    bridge, client = bridge_fixture(tmp_path)
    checkpoint = tmp_path / "private" / "checkpoint.json"
    employee = candidate("employee", payload={"Description": "SYNTHETIC_EMPLOYEE", "Person_Key": KEY})
    employee["prepared"]["entity_set"] = "Catalog_Employee"
    manifest, _, response, result = applied_card(checkpoint, bridge, employee, "missing-person")
    assert response["ok"] is False and result["state"] == "failed"
    assert not any(call[0] == "create" for call in client.calls)
    with pytest.raises(QueueManifestError):
        resolve_claim(checkpoint, manifest, [employee], result, [created_evidence(employee, manifest, result)])
    independent = candidate("position")
    independent["prepared"]["entity_set"] = "Catalog_Person"
    _, _, good_response, _ = applied_card(checkpoint, bridge, independent, "independent-card")
    assert good_response["readbackVerified"] is True
    assert screen_rows(checkpoint, [employee])["held_count_by_reason"] == {"dispatch_uncertain": 1}


def test_real_bridge_unknown_write_cannot_repeat_or_be_claimed_again(tmp_path):
    from onec_odata_mcp.ledger import UncertainWrite
    bridge, client = bridge_fixture(tmp_path)
    client.fail = "unknown"
    row = candidate()
    row["prepared"]["entity_set"] = "Catalog_Person"
    path = tmp_path / "private" / "checkpoint.json"
    manifest, args, response, result = applied_card(path, bridge, row, "unknown-write")
    assert response["ok"] is False and result["state"] == "pending"
    with pytest.raises(UncertainWrite):
        bridge.create_entity(**args)
    with pytest.raises(QueueManifestError):
        prepare_claim(path, [row], batch_id="new-id-for-same-card")
    assert sum(call[0] == "create" for call in client.calls) == 1
    assert record_result(manifest, result)["result"]["state"] == "pending"


def test_real_metadata_int64_normalization_is_caught_before_apply(tmp_path):
    bridge, client = bridge_fixture(tmp_path)
    row = candidate(payload={"Description": "SYNTHETIC", "Counter": 7})
    row["prepared"]["entity_set"] = "Catalog_Person"
    row["target_fingerprint"] = bridge.target_fingerprint
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="int64-normalization")
    request = preview_claim(path, manifest)
    response = bridge.create_entity(**request)
    with pytest.raises(QueueManifestError):
        verify_preview_claim(path, manifest, request, response)
    assert client.calls == []


def test_known_accepted_key_with_lost_readback_resolves_after_full_read_without_create(tmp_path):
    bridge, client = bridge_fixture(tmp_path)
    client.readback_fail_once = True
    row = candidate()
    row["prepared"]["entity_set"] = "Catalog_Person"
    path = tmp_path / "private" / "checkpoint.json"
    manifest, args, response, result = applied_card(path, bridge, row, "known-key-lost-readback")
    assert result["state"] == "complete" and result["result"]["writeState"] == "accepted_unverified"
    key = result["result"]["key"]
    proof = created_evidence(row, manifest, result)
    proof["readback"] = bridge.get_entity("Catalog_Person", key)
    assert resolve_claim(path, manifest, [row], result, [proof])["status"] == "resolved"
    assert sum(call[0] == "create" for call in client.calls) == 1


def test_real_addressed_pending_reconciliation_resolves_without_another_create(tmp_path):
    bridge, client = bridge_fixture(tmp_path)
    client.fail = "accepted-unknown"
    row = candidate()
    row["prepared"]["entity_set"] = "Catalog_Person"
    path = tmp_path / "private" / "checkpoint.json"
    manifest, args, response, result = applied_card(path, bridge, row, "unknown-accepted")
    assert result["state"] == "pending"
    kwargs = {"operation_id": args["operation_id"], "entity_set": "Catalog_Person", "payload": args["payload"],
              "filter": "Description eq 'SYNTHETIC_PERSON'", "expected_target": bridge.target_fingerprint}
    preview = bridge.reconcile_pending_create(**kwargs)
    assert preview["plan"]["key"] == KEY
    bridge.reconcile_pending_create(**kwargs, dry_run=False, confirm=True, expected_key=KEY)
    recovered = bridge.get_write_receipt(args["operation_id"])
    proof = created_evidence(row, manifest, recovered)
    proof["readback"] = bridge.get_entity("Catalog_Person", KEY)
    assert resolve_claim(path, manifest, [row], recovered, [proof])["status"] == "resolved"
    assert sum(call[0] == "create" for call in client.calls) == 1


def test_explicit_database_binding_uses_actual_registered_create_wrapper(tmp_path, monkeypatch):
    from onec_odata_mcp import server
    bridge, client = bridge_fixture(tmp_path)
    monkeypatch.setattr(server, "_target_bridge", lambda database: (database, bridge))
    row = candidate()
    row["target_fingerprint"] = bridge.target_fingerprint
    row["database"] = "test"
    row["prepared"]["entity_set"] = "Catalog_Person"
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="registered-wrapper")
    request = preview_claim(path, manifest)
    assert request["database"] == "test"
    preview = server.onec83_create_entity(**request)
    assert preview["database"] == "test"
    receipt = verify_preview_claim(path, manifest, request, preview)
    apply = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                        template_id=row["template"]["id"], template_version="1")
    created = server.onec83_create_entity(**apply)
    result = server.onec83_get_write_receipt(apply["operation_id"], database="test")
    proof = created_evidence(row, manifest, result)
    proof["readback"] = created["readback"]
    wrong = {**result, "database": "prod"}
    with pytest.raises(QueueManifestError, match="exact saved"):
        record_result(manifest, wrong)
    assert resolve_claim(path, manifest, [row], result, [proof])["status"] == "resolved"
    assert sum(call[0] == "create" for call in client.calls) == 1
