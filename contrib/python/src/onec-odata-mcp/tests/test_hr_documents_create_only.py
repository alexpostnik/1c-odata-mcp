"""Synthetic HR source artifacts + actual helper/Bridge/SQLite contracts; no 1C."""
import copy
import hashlib
import json

import pytest

from onec_odata_mcp.hr_source import native_document_line
from onec_odata_mcp.queue_checkpoint import (
    import_evidence, screen_rows, prepare_claim, preview_claim, verify_preview_claim,
    apply_claim, resolve_claim, cancel_claim, _key,
)
from onec_odata_mcp.queue_manifest import (
    QueueManifestError, _digest, compile_manifest, validate_manifest, manifest_tool,
    record_result, resume_request,
)
from test_catalog_prerequisites import (
    SyntheticClient, _CSDL, candidate as catalog_candidate, absence, found,
)
from test_queue_checkpoint import candidate as bank_candidate, evidence as bank_evidence
from test_queue_checkpoint import dispatched as bank_dispatched


ENTITY = "Document_SyntheticHire"
EMPLOYEE_KEY = "00000000-0000-0000-0000-000000000099"
NAMESPACE = "sha256:" + "a" * 64
SCOPE = "same-information-base-only; base continuity and lifetime unverified"
HR = "hr_documents_create_only"


def artifact(directory, name, body):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (name + ".json")
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def source_fixture(directory, *, target="synthetic-test-target", identity="SYNTHETIC-DOC-TOKEN", token="SYNTHETIC-EMPLOYEE-TOKEN", mapping_mode="exact_native"):
    employee = catalog_candidate("saved-employee-alias", payload={"Description": "SYNTHETIC_EMPLOYEE"})
    employee.update(target_fingerprint=target, database="test")
    employee["prepared"]["entity_set"] = "Catalog_Employee"
    request = {"source_alias": "synthetic77", "document_type": "SyntheticHire", "document_metadata_id": "77",
               "native_reference": identity, "source_namespace": NAMESPACE,
               "attribute_names": ["Employee", "HireDate", "CreationDate", "ExternalOrder"], "table_attribute_names": [],
               "line_cursor": "0", "limit": 100}
    relation = {"status": "native-typed-unverified", "metadataKind": "reference",
                "catalogType": "SyntheticEmployees", "observedCatalogType": "SyntheticEmployees",
                "catalogMetadataId": "5", "nativeReference": token, "sourceNamespace": NAMESPACE}
    data = {"kind": "document_by_native", "metadataKind": "document", "documentType": "SyntheticHire",
            "documentMetadataId": "77", "observedDocumentType": "SyntheticHire", "sourceNamespace": NAMESPACE,
            "nativeReference": identity, "roundtripStatus": "matched-typed-selected",
            "stableIdentity": "unverified", "snapshotStatus": "none", "identityScope": SCOPE,
            "document": {"valueKind": "document", "selected": True, "nativeReference": identity,
                         "documentMetadataId": "77", "documentType": "SyntheticHire", "date": "2010-03-03",
                         "number": "SYNTHETIC-ORDER", "posted": True},
            "attributes": {"Employee": {"valueKind": "reference", "selected": True, "empty": False,
                                         "nativeReference": token, "refTypeId": "5", "nativeIdentityStatus": "unverified"},
                           "HireDate": "2010-03-03", "CreationDate": "2010-03-02", "ExternalOrder": "BUSINESS-77"}, "relations": {"Employee": relation},
            "lines": [], "lineNumberSemantics": "ordinal-traversal", "lineCursor": "0", "nextLineCursor": None, "hasMore": False,
            "declaredLineCount": 0, "unsupported": [], "readStatus": "complete-unverified",
            "historyLinkStatus": "unsupported-hold", "historyLinkReason": "periodic-document-native-transition-unimplemented"}
    cat_request = {"source_alias": "synthetic77", "type_name": "SyntheticEmployees", "catalog_metadata_id": "5",
                   "native_reference": token, "as_of": "2010-03-03", "attribute_names": ["Description"], "source_namespace": NAMESPACE}
    cat_data = {"kind": "reference_by_native", "type": "SyntheticEmployees", "observedCatalogType": "SyntheticEmployees",
                "catalogMetadataId": "5", "nativeReference": token, "roundtripStatus": "matched-typed-selected",
                "sourceNamespace": NAMESPACE, "sourceNamespaceProof": {"scheme": "configured-alias-windows-path-sha256-v1", "expected": NAMESPACE,
                    "observed": NAMESPACE, "status": "matched-request"},
                "stableIdentity": "unverified", "snapshotStatus": "none", "identityScope": SCOPE,
                "readContext": {"asOf": cat_request["as_of"], "useDate": "applied", "selection": "FindItem",
                                "periodicStatus": "date-context-applied-unverified"},
                "reference": {"valueKind": "reference", "selected": True, "nativeReference": token,
                              "refTypeId": "5", "nativeIdentityStatus": "unverified"},
                "attributes": {"Description": "SYNTHETIC_EMPLOYEE"}, "relations": {},
                "unsupported": [], "readStatus": "complete-unverified"}
    row = {"workflow": HR, "database": "test", "source": {"namespace": NAMESPACE, "date": "2010-03-03",
             "statement": "77", "line": native_document_line(data)}, "target_fingerprint": target,
           "template": {"id": "synthetic-hr-draft", "version": "1"},
           "prepared": {"source_id": "synthetic-order", "entity_set": ENTITY, "post": False,
                        "payload": {"Date": "2010-03-03T00:00:00", "CreatedAt": "2010-03-02T00:00:00", "ExternalOrder": "BUSINESS-77",
                                    "ДатаПриема": "2010-03-03T00:00:00", "Employee_Key": EMPLOYEE_KEY}},
           "prerequisites": [{"candidate": employee, "target_key": EMPLOYEE_KEY, "payload_field": "Employee_Key"}]}
    row["source_evidence"] = {
        "addressed_request_ref": artifact(directory, "doc-request", request),
        "addressed_response_ref": artifact(directory, "doc-response", {"schemaVersion": "1.0", "status": "ok",
            "operation": "read_document_by_native", "sourceAlias": "synthetic77", "data": data}),
        "scalar_bindings": [{"payload_field": "Date", "source_field": "document.date", "transform": "date_midnight"},
                            {"payload_field": "CreatedAt", "source_field": "attribute:CreationDate", "transform": "date_midnight"},
                            {"payload_field": "ExternalOrder", "source_field": "attribute:ExternalOrder", "transform": "exact"},
                            {"payload_field": "ДатаПриема", "source_field": "attribute:HireDate", "transform": "date_midnight"}],
        "relation_bindings": [{"payload_field": "Employee_Key", "source_field": "Employee", "role": "employee",
                               "catalog_request_ref": artifact(directory, "employee-request", cat_request),
                               "catalog_response_ref": artifact(directory, "employee-response", {"schemaVersion": "1.0", "status": "ok",
                                   "operation": "read_reference_by_native", "sourceAlias": "synthetic77", "data": cat_data})}],
    }
    factual = artifact(directory, "synthetic-facts", {"synthetic": True, "purpose": "offline-only independent factual evidence"})
    date_roles = {"schema_version": 1, "status": "accepted_hr_date_roles", "document_source": row["source"],
                  "target_fingerprint": target, "template": row["template"], "entity_set": ENTITY,
                  "source_roles": {"document.date": "document_date", "attribute:CreationDate": "creation_date", "attribute:HireDate": "business_date"},
                  "target_roles": {"Date": "document_date", "CreatedAt": "creation_date", "ДатаПриема": "business_date"},
                  "evidence_refs": [factual]}
    date_role_ref = artifact(directory, "date-roles", date_roles)
    for i, binding in enumerate(row["source_evidence"]["scalar_bindings"]):
        origin = "business_fact" if binding["payload_field"] == "ExternalOrder" else "source_date"
        role = date_roles["target_roles"].get(binding["payload_field"])
        binding["date_role"] = role
        binding.update(value_origin=origin, origin_evidence_ref=artifact(directory, "origin-" + str(i), {
            "schema_version": 1, "status": "accepted_hr_scalar_origin", "document_source": row["source"],
            "source_field": binding["source_field"], "payload_field": binding["payload_field"], "value_origin": origin,
            "date_role": role, "date_roles_sha256": date_role_ref["sha256"], "evidence_refs": [factual]}))
    row["source_evidence"]["date_inventory_ref"] = artifact(directory, "date-inventory", {
        "schema_version": 1, "status": "accepted_hr_source_date_inventory", "document_source": row["source"],
        "dates": [{"source_field": b["source_field"], "payload_field": b["payload_field"]}
                  for b in row["source_evidence"]["scalar_bindings"] if b["value_origin"] == "source_date"],
        "creation_date_status": "observed", "creation_date_source_field": "attribute:CreationDate", "role_evidence_ref": date_role_ref})
    binding = row["source_evidence"]["relation_bindings"][0]
    lineage = {"schema_version": 1, "kind": "hr_catalog_relation_mapping", "document_source": row["source"],
               "mapping_mode": mapping_mode, "candidate_sha256": _digest(employee), "source": employee["source"],
               "target_key": EMPLOYEE_KEY, "target_fingerprint": target, "database": "test",
               "original_catalog_request_ref": artifact(directory, "original-employee-request", cat_request),
               "original_catalog_response_ref": artifact(directory, "original-employee-response", {
                   "schemaVersion": "1.0", "status": "ok", "operation": "read_reference_by_native", "sourceAlias": "synthetic77", "data": cat_data}),
               "verified_evidence_ref": artifact(directory, "verified-employee-evidence", found(employee, EMPLOYEE_KEY)),
               "prerequisite_acceptance_ref": artifact(directory, "original-employee-acceptance", {
                   "synthetic": True, "purpose": "original accepted canonical prerequisite provenance"})}
    source_relation = {"source_alias": cat_request["source_alias"], "source_namespace": NAMESPACE,
                       "catalog_type": cat_request["type_name"], "catalog_metadata_id": cat_request["catalog_metadata_id"],
                       "native_reference": token, "as_of": cat_request["as_of"]}
    correspondence = {"schema_version": 1, "status": "accepted_hr_factual_correspondence", "source_relation": source_relation,
                      **{k: lineage[k] for k in ("mapping_mode", "document_source", "candidate_sha256", "target_key", "target_fingerprint", "database")},
                      "basis": "independently_reviewed_historical_facts", "explanation": "SYNTHETIC independently checked employment facts; no name matching",
                      "factual_evidence_refs": [factual], "prerequisite_acceptance_sha256": lineage["prerequisite_acceptance_ref"]["sha256"],
                      "verified_evidence_sha256": _digest(found(employee, EMPLOYEE_KEY))}
    lineage["correspondence_ref"] = artifact(directory, "correspondence", correspondence)
    binding["catalog_lineage_ref"] = artifact(directory, "lineage", lineage)
    seal_continuity(row, directory)
    bind_review(row, directory)
    return row, employee


def seal_continuity(row, directory):
    """Only synthetic fixtures may manufacture these independently accepted reviews."""
    b = row["source_evidence"]["relation_bindings"][0]
    lineage = json.loads(open(b["catalog_lineage_ref"]["path"]).read())
    correspondence = json.loads(open(lineage["correspondence_ref"]["path"]).read())
    review = {"schema_version": 1, "status": "accepted_hr_bounded_source_continuity",
              "scope": "original-current-addressed-observations; global-lifetime-unverified", "document_source": row["source"],
              "source_relation": correspondence["source_relation"], "original_request_sha256": lineage["original_catalog_request_ref"]["sha256"],
              "original_response_sha256": lineage["original_catalog_response_ref"]["sha256"], "current_request_sha256": b["catalog_request_ref"]["sha256"],
              "current_response_sha256": b["catalog_response_ref"]["sha256"], "correspondence_sha256": lineage["correspondence_ref"]["sha256"],
              "continuity_evidence_refs": [artifact(directory, "synthetic-continuity-evidence", {"synthetic": True,
                  "purpose": "independently checked original/current producer registration and source continuity"})]}
    lineage["continuity_review_ref"] = artifact(directory, "continuity", review)
    b["catalog_lineage_ref"] = artifact(directory, "lineage", lineage)


def bind_review(row, directory):
    # This generated acceptance is SYNTHETIC. Actual provenance is an external
    # independent factual review, never a helper-created acceptance.
    p = row["source_evidence"]
    review = {"schema_version": 1, "status": "accepted_hr_source_binding", "source": row["source"],
              "prepared_sha256": _digest(row["prepared"]), "template": row["template"],
              "target_fingerprint": row["target_fingerprint"], "database": row.get("database"),
              "addressed_request_sha256": p["addressed_request_ref"]["sha256"],
              "addressed_response_sha256": p["addressed_response_ref"]["sha256"],
              "scalar_bindings_sha256": _digest(p["scalar_bindings"]),
              "date_inventory_sha256": p["date_inventory_ref"]["sha256"],
              "relation_bindings_sha256": _digest(p["relation_bindings"]),
              "prerequisites_sha256": _digest(row["prerequisites"])}
    p["independent_review_ref"] = artifact(directory, "review", review)


def seal_date_roles(row, directory, roles):
    """Synthetic independent semantic-role review; never actual metadata proof."""
    p = row["source_evidence"]
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    inventory["role_evidence_ref"] = artifact(directory, "date-roles", roles)
    p["date_inventory_ref"] = artifact(directory, "date-inventory", inventory)
    for i, binding in enumerate(p["scalar_bindings"]):
        origin = json.loads(open(binding["origin_evidence_ref"]["path"]).read())
        origin.update(source_field=binding["source_field"], payload_field=binding["payload_field"], date_role=binding["date_role"],
                      date_roles_sha256=inventory["role_evidence_ref"]["sha256"])
        binding["origin_evidence_ref"] = artifact(directory, "updated-origin-" + str(i), origin)


class HRClient(SyntheticClient):
    def create_entity(self, entity_set, payload, null_ref_key=False):
        try:
            return super().create_entity(entity_set, payload, null_ref_key)
        finally:
            for (collection, _), value in self.objects.items():
                if collection == ENTITY:
                    value.setdefault("Posted", False)
                    value.setdefault("Number", "SYNTHETIC-TARGET-AUTO")

    def list_entities(self, entity_set, **kwargs):
        if entity_set == ENTITY and kwargs.get("filter") == "ExternalOrder eq 'BUSINESS-77'":
            self.calls.append(("list", entity_set))
            return {"value": [{"Ref_Key": key} for (collection, key), obj in self.objects.items()
                              if collection == entity_set and obj.get("ExternalOrder") == "BUSINESS-77"]}
        return super().list_entities(entity_set, **kwargs)


def bridge_fixture(tmp_path, monkeypatch, *, business_target_field="ДатаПриема"):
    from onec_odata_mcp import server
    from onec_odata_mcp.metadata import MetadataModel
    from onec_odata_mcp.ledger import WriteLedger
    document_type = '''<EntityType Name="Document_SyntheticHire"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/><Property Name="Number" Type="Edm.String"/>
<Property Name="Date" Type="Edm.DateTime"/><Property Name="ДатаПриема" Type="Edm.DateTime"/>
<Property Name="CreatedAt" Type="Edm.DateTime"/><Property Name="ExternalOrder" Type="Edm.String"/>
<Property Name="Posted" Type="Edm.Boolean"/><Property Name="DeletionMark" Type="Edm.Boolean"/>
<Property Name="Employee_Key" Type="Edm.Guid"/>
<NavigationProperty Name="Employee" Relationship="StandardODATA.Hire_Employee" FromRole="Hire" ToRole="Employee"/>
</EntityType><Association Name="Hire_Employee"><End Role="Hire" Type="StandardODATA.Document_SyntheticHire" Multiplicity="*"/>
<End Role="Employee" Type="StandardODATA.Catalog_Employee" Multiplicity="1"/></Association>'''
    document_set = '''<EntitySet Name="Document_SyntheticHire" EntityType="StandardODATA.Document_SyntheticHire"/>
<AssociationSet Name="Hire_Employee" Association="StandardODATA.Hire_Employee"><End Role="Hire" EntitySet="Document_SyntheticHire"/>
<End Role="Employee" EntitySet="Catalog_Employee"/></AssociationSet>'''
    document_type = document_type.replace('Name="ДатаПриема"', 'Name="' + business_target_field + '"')
    csdl = _CSDL.replace('<EntityContainer Name="Container">', document_type + '<EntityContainer Name="Container">')
    csdl = csdl.replace('</EntityContainer>', document_set + '</EntityContainer>')
    client = HRClient()
    client.objects[("Catalog_Employee", EMPLOYEE_KEY)] = {"Ref_Key": EMPLOYEE_KEY, "Description": "SYNTHETIC_EMPLOYEE", "DeletionMark": False}
    bridge = server.Bridge(client, WriteLedger(tmp_path / "ledger.sqlite3"), MetadataModel.from_xml(csdl))
    monkeypatch.setattr(server, "_target_bridge", lambda database: (database, bridge))
    return server, bridge, client


def business_mapping_fixture(directory, *, target="synthetic-test-target", target_field="ДатаПриема"):
    row, employee = source_fixture(directory, target=target)
    p = row["source_evidence"]
    request = json.loads(open(p["addressed_request_ref"]["path"]).read())
    response = json.loads(open(p["addressed_response_ref"]["path"]).read())
    request["attribute_names"].remove("HireDate")
    response["data"]["attributes"].pop("HireDate")
    p["addressed_request_ref"] = artifact(directory, "doc-request", request)
    p["addressed_response_ref"] = artifact(directory, "doc-response", response)
    binding = next(b for b in p["scalar_bindings"] if b["payload_field"] == "ДатаПриема")
    row["prepared"]["payload"].pop("ДатаПриема")
    row["prepared"]["payload"][target_field] = row["prepared"]["payload"]["Date"]
    binding.update(source_field="document.date", payload_field=target_field)
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    inventory["dates"] = [{"source_field": b["source_field"], "payload_field": b["payload_field"]}
                          for b in p["scalar_bindings"] if b["value_origin"] == "source_date"]
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    roles["source_roles"].pop("attribute:HireDate")
    roles["target_roles"][target_field] = roles["target_roles"].pop("ДатаПриема")
    p["date_inventory_ref"] = artifact(directory, "date-inventory", inventory)
    seal_date_roles(row, directory, roles)
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    data = response["data"]
    identity = {"metadata_kind": "document", "source_namespace": data["sourceNamespace"], "document_type": data["documentType"],
                "document_metadata_id": data["documentMetadataId"], "native_reference": data["nativeReference"]}
    # These independently accepted semantics are strictly synthetic. They do
    # not establish any real DocDate -> hire-date meaning or actual metadata.
    evidence = artifact(directory, "synthetic-business-config", {"synthetic": True, "purpose": "source and target configuration event-date semantics"})
    meaning = "SYNTHETIC_EVENT_EFFECTIVE_DATE"
    source = {"schema_version": 1, "status": "accepted_hr_document_business_meaning", "document_source": row["source"],
              "document_identity": identity, "source_field": "document.date", "source_role": "document_date",
              "business_meaning": meaning, "evidence_refs": [evidence]}
    target_meaning = {"schema_version": 1, "status": "accepted_hr_target_business_date_semantics", "template": row["template"],
                      "entity_set": ENTITY, "target_fingerprint": target, "database": row.get("database"), "payload_field": target_field,
                      "date_role": "business_date", "date_roles_sha256": inventory["role_evidence_ref"]["sha256"],
                      "business_meaning": meaning, "evidence_refs": [evidence]}
    core = {"schema_version": 1, "status": "accepted_hr_document_business_date_mapping", "document_source": row["source"],
            "document_identity": identity, "source_field": "document.date", "source_role": "document_date", "target_role": "business_date",
            "payload_field": target_field, "transform": binding["transform"], "source_value_sha256": _digest(data["document"]["date"]),
            "prepared_sha256": _digest(row["prepared"]), "template": row["template"], "entity_set": ENTITY,
            "target_fingerprint": target, "database": row.get("database"), "addressed_request_sha256": p["addressed_request_ref"]["sha256"],
            "addressed_response_sha256": p["addressed_response_ref"]["sha256"], "date_roles_sha256": inventory["role_evidence_ref"]["sha256"],
            "origin_evidence_sha256": binding["origin_evidence_ref"]["sha256"], "source_semantics_ref": artifact(directory, "source-business-meaning", source),
            "target_semantics_ref": artifact(directory, "target-business-meaning", target_meaning)}
    core["independent_review_ref"] = artifact(directory, "business-mapping-review", {"schema_version": 1,
        "status": "accepted_hr_document_business_date_mapping_review", "mapping_sha256": _digest(core)})
    binding["business_date_witness_ref"] = artifact(directory, "business-witness", core)
    bind_review(row, directory)
    return row, employee


def reseal_business_witness(row, directory, witness):
    core = {k: v for k, v in witness.items() if k != "independent_review_ref"}
    witness["independent_review_ref"] = artifact(directory, "business-mapping-review", {"schema_version": 1,
        "status": "accepted_hr_document_business_date_mapping_review", "mapping_sha256": _digest(core)})
    binding = next(b for b in row["source_evidence"]["scalar_bindings"] if "business_date_witness_ref" in b)
    binding["business_date_witness_ref"] = artifact(directory, "business-witness", witness)
    bind_review(row, directory)


def prepare(tmp_path, monkeypatch):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    row, employee = source_fixture(tmp_path / "source", target=bridge.target_fingerprint)
    path = tmp_path / "private" / "checkpoint.json"
    assert import_evidence(path, employee, found(employee, EMPLOYEE_KEY))["status"] == "found_existing_verified"
    assert import_evidence(path, row, absence(row))["status"] == "missing_verified"
    assert screen_rows(path, [row])["ready_count"] == 1
    manifest = prepare_claim(path, [row], batch_id="synthetic-hr")
    request = preview_claim(path, manifest)
    response = server.onec83_create_entity(**request)
    receipt = verify_preview_claim(path, manifest, request, response)
    return server, bridge, client, path, row, manifest, receipt


def dispatch(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, receipt = prepare(tmp_path, monkeypatch)
    args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                       template_id=row["template"]["id"], template_version="1")
    return server, bridge, client, path, row, manifest, args


def full_get_proof(server, row, manifest, result):
    key = result["result"]["key"]
    readback = server.onec83_get_entity(entity_set=ENTITY, key=key, database="test")
    return {"kind": "newly_created_verified", "candidate_sha256": _digest(row),
            "source": row["source"], "target_fingerprint": row["target_fingerprint"], "target_key": key,
            "manifest": manifest, "result_receipt": record_result(manifest, result),
            "readback": readback, "readback_context": {"entity_set": ENTITY,
                "target_fingerprint": row["target_fingerprint"], "database": "test", "key": key, "full_get": True}}


def test_complete_create_only_cycle_actual_bridge_and_same_receipt_ledger(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    assert manifest_tool(manifest) == "onec83_create_entity"
    assert args["database"] == "test" and args["confirm"] is True
    assert "items" not in args and "post" not in args and "post_operation_id" not in args
    assert "Number" not in args["payload"] and "Code" not in args["payload"]
    assert args["payload"]["Date"] == "2010-03-03T00:00:00"
    assert args["payload"]["CreatedAt"] == "2010-03-02T00:00:00"
    assert _key(row) == json.dumps([list((row["source"][k] for k in ("namespace", "statement", "line"))),
                                  row["target_fingerprint"], HR, ENTITY], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert json.loads(path.read_text())["claims"][manifest["batch_id"]]["stage"] == "dispatch_uncertain"
    result = server.onec83_create_entity(**args)
    assert result["readbackVerified"] is True
    saved = server.onec83_get_write_receipt(args["operation_id"], database="test")
    proof = full_get_proof(server, row, manifest, saved)
    assert proof["readback"]["Posted"] is False
    assert proof["readback"]["Number"] == "SYNTHETIC-TARGET-AUTO"
    assert resolve_claim(path, manifest, [row], saved, [proof])["status"] == "resolved"
    before = path.read_bytes()
    assert resolve_claim(path, manifest, [row], saved, [proof])["status"] == "resolved"
    assert path.read_bytes() == before
    assert screen_rows(path, [row])["newly_created_verified"] == 1
    assert sum(c[0] == "create" for c in client.calls) == 1
    assert not any(c[0] == "post" for c in client.calls)


@pytest.mark.parametrize("mutation", ["post", "movement", "posted_payload", "tabular", "no_workflow"])
def test_wrong_mode_or_fake_movements_rejected_before_claim(tmp_path, mutation):
    row, _ = source_fixture(tmp_path / "source")
    if mutation == "post": row["prepared"]["post"] = True
    if mutation == "movement": row["prepared"]["expected_accounting_movements"] = None
    if mutation == "posted_payload": row["prepared"]["payload"]["Posted"] = False
    if mutation == "tabular": row["prepared"]["payload"]["Rows"] = []
    if mutation == "no_workflow": row.pop("workflow")
    path = tmp_path / "private" / "checkpoint.json"
    with pytest.raises(QueueManifestError):
        prepare_claim(path, [row], batch_id="bad")
    assert not path.exists()


@pytest.mark.parametrize("mutation", ["token", "namespace", "kind", "selected", "metadata", "partial", "rows",
                                     "relation", "relation_token", "empty_relation_token", "date", "cursor", "review", "stale_bytes"])
def test_typed_source_rejects_resealed_lies_and_unavailable_proof(tmp_path, mutation):
    row, _ = source_fixture(tmp_path / "source")
    p = row["source_evidence"]
    ref = p["addressed_response_ref"]
    response = json.loads(open(ref["path"]).read())
    data = response["data"]
    if mutation == "token": data["nativeReference"] = "OTHER"
    if mutation == "namespace": data["sourceNamespace"] = "sha256:" + "b" * 64
    if mutation == "kind": data["metadataKind"] = "reference"
    if mutation == "selected": data["document"]["selected"] = False
    if mutation == "metadata": data["document"]["documentMetadataId"] = "99"
    if mutation == "partial": data["readStatus"] = "partial-hold"
    if mutation == "rows": data["declaredLineCount"] = 1
    if mutation == "relation": data["relations"]["Employee"]["status"] = "unsupported-hold"
    if mutation == "relation_token": data["relations"]["Employee"]["nativeReference"] = "OTHER"
    if mutation == "empty_relation_token":
        data["relations"]["Employee"]["nativeReference"] = ""
        data["attributes"]["Employee"]["nativeReference"] = ""
    if mutation == "date": row["source"]["line"] = "2010-03-03-SYNTHETIC-ORDER"
    if mutation == "cursor": row["source"]["line"] = "0"
    if mutation == "stale_bytes":
        open(ref["path"], "w").write("{}")
    else:
        p["addressed_response_ref"] = artifact(tmp_path / "source", "doc-response", response)
        bind_review(row, tmp_path / "source")
    if mutation == "review":
        open(p["independent_review_ref"]["path"], "w").write("{}")
    path = tmp_path / "private" / "checkpoint.json"
    with pytest.raises(QueueManifestError):
        import_evidence(path, row, absence(row))
    assert not path.exists()


def test_employee_prerequisite_not_verified_and_late_loss_hold_only_dependent(tmp_path, monkeypatch):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    row, employee = source_fixture(tmp_path / "source", target=bridge.target_fingerprint)
    path = tmp_path / "private" / "checkpoint.json"
    with pytest.raises(QueueManifestError, match="prerequisite readback"):
        import_evidence(path, row, absence(row))
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="late-change")
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, server.onec83_create_entity(**request))
    contradictory = found(employee, "different-key")
    import_evidence(path, employee, contradictory)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="prerequisite readback"):
        apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                    template_id=row["template"]["id"], template_version="1")
    assert path.read_bytes() == before and not any(c[0] == "create" for c in client.calls)
    independent = catalog_candidate("independent")
    independent.update(target_fingerprint=bridge.target_fingerprint, database="test")
    import_evidence(path, independent, absence(independent))
    assert prepare_claim(path, [independent], batch_id="independent")["batch_id"] == "independent"


@pytest.mark.parametrize("mutation", ["Posted", "DeletionMark", "Employee_Key", "Date", "CreatedAt", "missing_field", "full_get", "database", "movements"])
def test_receipt_summary_never_overrides_full_addressed_get(tmp_path, monkeypatch, mutation):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    proof = full_get_proof(server, row, manifest, result)
    if mutation in {"Posted", "DeletionMark"}: proof["readback"][mutation] = True
    if mutation in {"Employee_Key", "Date", "CreatedAt"}: proof["readback"][mutation] = "OTHER"
    if mutation == "missing_field": proof["readback"].pop("ДатаПриема")
    if mutation == "full_get": proof["readback_context"]["full_get"] = False
    if mutation == "database": proof["readback_context"]["database"] = "prod"
    if mutation == "movements": proof["active_movements"] = {"verified": True}
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="not fully verified"):
        resolve_claim(path, manifest, [row], result, [proof])
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["unknown", "refused", "accepted-unknown"])
def test_unknown_or_refused_no_new_ids_or_cancellation(tmp_path, monkeypatch, failure):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    client.fail = failure
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    assert result["state"] == ("failed" if failure == "refused" else "pending")
    before = path.read_bytes()
    for action in (
        lambda: prepare_claim(path, [row], batch_id="new-id"),
        lambda: cancel_claim(path, batch_id=manifest["batch_id"], plan_sha256=manifest["plan_sha256"]),
        lambda: import_evidence(path, row, absence(row)),
        lambda: apply_claim(path, manifest, {}, target_fingerprint=bridge.target_fingerprint,
                            template_id=row["template"]["id"], template_version="1"),
        lambda: resume_request(manifest, record_result(manifest, result), target_fingerprint=bridge.target_fingerprint,
                               template_id=row["template"]["id"], template_version="1"),
    ):
        with pytest.raises(QueueManifestError): action()
    assert path.read_bytes() == before and sum(c[0] == "create" for c in client.calls) == 1


def test_lost_create_response_addressed_reconciliation_and_full_get_no_new_write(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    client.fail = "accepted-unknown"
    server.onec83_create_entity(**args)
    pending = server.onec83_get_write_receipt(args["operation_id"], database="test")
    assert pending["state"] == "pending"
    params = {"operation_id": args["operation_id"], "entity_set": ENTITY, "payload": args["payload"],
              "filter": "ExternalOrder eq 'BUSINESS-77'", "expected_target": bridge.target_fingerprint}
    preview = bridge.reconcile_pending_create(**params)
    key = preview["plan"]["key"]
    bridge.reconcile_pending_create(**params, dry_run=False, confirm=True, expected_key=key)
    recovered = server.onec83_get_write_receipt(args["operation_id"], database="test")
    assert recovered["state"] == "complete" and recovered["result"]["writeState"] == "reconciled_present"
    assert resolve_claim(path, manifest, [row], recovered, [full_get_proof(server, row, manifest, recovered)])["status"] == "resolved"
    assert sum(c[0] == "create" for c in client.calls) == 1


def test_source_artifact_rechecked_before_apply_and_claim_history_retained(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, receipt = prepare(tmp_path, monkeypatch)
    before = path.read_bytes()
    open(row["source_evidence"]["addressed_response_ref"]["path"], "w").write("{}")
    with pytest.raises(QueueManifestError, match="artifact"):
        apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                    template_id=row["template"]["id"], template_version="1")
    assert path.read_bytes() == before
    assert not any(c[0] == "create" for c in client.calls)


def test_posted_and_legacy_claims_remain_exact_after_hr_resolution(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    bank = bank_candidate()
    import_evidence(path, bank, bank_evidence(bank))
    original = json.loads(path.read_text())
    bank_entry = copy.deepcopy(original["entries"][_key(bank)])
    held = bank_candidate("synthetic-held")
    proof = bank_evidence(held)
    proof["readback"]["Posted"] = False
    import_evidence(path, held, proof)
    held_entry = copy.deepcopy(json.loads(path.read_text())["entries"][_key(held)])
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    resolve_claim(path, manifest, [row], result, [full_get_proof(server, row, manifest, result)])
    after = json.loads(path.read_text())
    assert after["schema_version"] == 4 and after["entries"][_key(bank)] == bank_entry
    assert after["entries"][_key(held)] == held_entry and held_entry["status"] == "held"
    assert screen_rows(path, [bank])["found_existing_verified"] == 1


def test_hr_manifest_reseal_does_not_allow_post_bulk_or_false_movement(tmp_path):
    row, _ = source_fixture(tmp_path / "source")
    manifest = compile_manifest([row["prepared"]], batch_id="one", target_fingerprint=row["target_fingerprint"],
                                template_id="synthetic", template_version="1", workflow=HR, database="test")
    for field, value in (("post", True), ("expected_accounting_movements", None), ("post_operation_id", "invented")):
        changed = copy.deepcopy(manifest)
        changed["rows"][0][field] = value
        changed["plan_sha256"] = _digest({k: v for k, v in changed.items() if k != "plan_sha256"})
        with pytest.raises(QueueManifestError): validate_manifest(changed)
    with pytest.raises(QueueManifestError, match="exactly one"):
        compile_manifest([row["prepared"]], batch_id="one", target_fingerprint=row["target_fingerprint"],
                         template_id="synthetic", template_version="1", workflow=HR, max_concurrency=2)


@pytest.mark.parametrize("mutation", ["operationId", "request_hash", "database", "entitySet", "found"])
def test_hr_receipt_exact_bindings_not_interchangeable(tmp_path, monkeypatch, mutation):
    server, bridge, client, path, row, manifest, args = dispatch(tmp_path, monkeypatch)
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    if mutation == "entitySet": result["result"][mutation] = "Document_Other"
    elif mutation == "found": result[mutation] = False
    else: result[mutation] = "OTHER"
    with pytest.raises(QueueManifestError): record_result(manifest, result)


def test_actual_mcp_envelopes_are_byte_bound_not_replaced_by_summary(tmp_path):
    row, employee = source_fixture(tmp_path / "source")
    for ref in (row["source_evidence"]["addressed_response_ref"],
                row["source_evidence"]["relation_bindings"][0]["catalog_response_ref"]):
        raw = open(ref["path"]).read()
        wrapped = artifact(tmp_path / "source", "wrapped-" + str(len(raw)),
                           {"is_error": False, "content": [{"type": "text", "text": raw}]})
        ref.update(wrapped)
    seal_continuity(row, tmp_path / "source")
    bind_review(row, tmp_path / "source")
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    assert import_evidence(path, row, absence(row))["status"] == "missing_verified"
    p = row["source_evidence"]
    bad = {"is_error": True, "content": [{"type": "text", "text": "{}"}]}
    p["addressed_response_ref"] = artifact(tmp_path / "source", "error-response", bad)
    bind_review(row, tmp_path / "source")
    with pytest.raises(QueueManifestError, match="envelope"):
        prepare_claim(path, [row], batch_id="error")


def test_legacy_v3_dispatched_bank_claim_and_ids_unchanged_by_hr(tmp_path, monkeypatch):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    path = tmp_path / "private" / "checkpoint.json"
    bank = bank_candidate()
    bank_manifest = bank_dispatched(path, [bank], batch="legacy-bank-dispatched")
    old = json.loads(path.read_text())
    old["schema_version"] = 3
    old["index_sha256"] = _digest({k: old[k] for k in ("schema_version", "entries", "claims")})
    path.write_text(json.dumps(old))
    original_entries, original_claims = copy.deepcopy(old["entries"]), copy.deepcopy(old["claims"])
    row, employee = source_fixture(tmp_path / "source", target=bridge.target_fingerprint)
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="new-hr")
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, server.onec83_create_entity(**request))
    args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                       template_id=row["template"]["id"], template_version="1")
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    resolve_claim(path, manifest, [row], result, [full_get_proof(server, row, manifest, result)])
    final = json.loads(path.read_text())
    assert final["schema_version"] == 4
    assert all(final["entries"][k] == v for k, v in original_entries.items())
    assert all(final["claims"][k] == v for k, v in original_claims.items())
    assert final["claims"]["legacy-bank-dispatched"]["manifest"] == bank_manifest
    assert screen_rows(path, [bank])["held_count_by_reason"] == {"dispatch_uncertain": 1}


def test_resealed_hr_claim_cannot_remove_saved_source_proof(tmp_path, monkeypatch):
    _, _, _, path, row, manifest, _ = prepare(tmp_path, monkeypatch)
    old = json.loads(path.read_text())
    old["claims"][manifest["batch_id"]].pop("hr_sources")
    old["index_sha256"] = _digest({k: old[k] for k in ("schema_version", "entries", "claims")})
    path.write_text(json.dumps(old))
    with pytest.raises(QueueManifestError, match="source/dependency"):
        screen_rows(path, [row])


@pytest.mark.parametrize("mutation", ["missing_namespace", "missing_producer", "naked_namespace", "unbound",
                                     "cross_base", "repoint", "original_missing_producer", "original_cross_base",
                                     "original_changed_fact", "missing_original", "missing_continuity", "missing_correspondence",
                                     "name_only", "wrong_mapping_target", "missing_factual_evidence", "missing_continuity_evidence"])
def test_relation_scope_and_independent_correspondence_reject_resealed_changes(tmp_path, mutation):
    directory = tmp_path / "source"
    row, employee = source_fixture(directory)
    binding = row["source_evidence"]["relation_bindings"][0]
    lineage = json.loads(open(binding["catalog_lineage_ref"]["path"]).read())
    current = json.loads(open(binding["catalog_response_ref"]["path"]).read())
    request = json.loads(open(binding["catalog_request_ref"]["path"]).read())
    original = json.loads(open(lineage["original_catalog_response_ref"]["path"]).read())
    correspondence = json.loads(open(lineage["correspondence_ref"]["path"]).read())
    other = "sha256:" + "b" * 64
    if mutation == "missing_namespace": current["data"].pop("sourceNamespace")
    if mutation in {"missing_producer", "naked_namespace"}: current["data"].pop("sourceNamespaceProof")
    if mutation == "unbound": current["data"]["sourceNamespaceProof"].update(expected=None, status="observed-unbound")
    if mutation == "cross_base": current["data"]["sourceNamespace"] = other
    if mutation == "repoint":
        request["source_namespace"] = other
        current["data"].update(sourceNamespace=other, sourceNamespaceProof={"scheme": "configured-alias-windows-path-sha256-v1",
                              "expected": other, "observed": other, "status": "matched-request"})
    if mutation == "original_missing_producer": original["data"].pop("sourceNamespaceProof")
    if mutation == "original_cross_base":
        original["data"].update(sourceNamespace=other, sourceNamespaceProof={"scheme": "configured-alias-windows-path-sha256-v1",
                               "expected": other, "observed": other, "status": "matched-request"})
    if mutation == "original_changed_fact": original["data"]["attributes"]["Description"] = "ANOTHER FACT"
    if mutation == "name_only": correspondence["basis"] = "same_display_name"
    if mutation == "wrong_mapping_target": correspondence["target_key"] = "another-target"
    if mutation == "missing_factual_evidence": correspondence["factual_evidence_refs"] = []
    binding["catalog_response_ref"] = artifact(directory, "employee-response", current)
    binding["catalog_request_ref"] = artifact(directory, "employee-request", request)
    lineage["original_catalog_response_ref"] = artifact(directory, "original-employee-response", original)
    lineage["correspondence_ref"] = artifact(directory, "correspondence", correspondence)
    binding["catalog_lineage_ref"] = artifact(directory, "lineage", lineage)
    seal_continuity(row, directory)
    if mutation == "missing_continuity_evidence":
        latest = json.loads(open(binding["catalog_lineage_ref"]["path"]).read())
        continuity = json.loads(open(latest["continuity_review_ref"]["path"]).read())
        continuity["continuity_evidence_refs"] = []
        latest["continuity_review_ref"] = artifact(directory, "continuity", continuity)
        binding["catalog_lineage_ref"] = artifact(directory, "lineage", latest)
    if mutation in {"missing_original", "missing_continuity", "missing_correspondence"}:
        latest = json.loads(open(binding["catalog_lineage_ref"]["path"]).read())
        key = {"missing_original": "original_catalog_response_ref", "missing_continuity": "continuity_review_ref",
               "missing_correspondence": "correspondence_ref"}[mutation]
        from pathlib import Path
        Path(latest[key]["path"]).unlink()
    bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError): import_evidence(path, row, absence(row))
    assert path.read_bytes() == before
    assert screen_rows(path, [employee])["found_existing_verified"] == 1


def test_original_verified_evidence_must_match_existing_checkpoint_not_fresh_assertion(tmp_path):
    directory = tmp_path / "source"
    row, employee = source_fixture(directory)
    binding = row["source_evidence"]["relation_bindings"][0]
    lineage = json.loads(open(binding["catalog_lineage_ref"]["path"]).read())
    altered = found(employee, EMPLOYEE_KEY)
    altered["synthetic_new_assertion"] = True
    lineage["verified_evidence_ref"] = artifact(directory, "verified-employee-evidence", altered)
    correspondence = json.loads(open(lineage["correspondence_ref"]["path"]).read())
    correspondence["verified_evidence_sha256"] = _digest(altered)
    lineage["correspondence_ref"] = artifact(directory, "correspondence", correspondence)
    binding["catalog_lineage_ref"] = artifact(directory, "lineage", lineage)
    seal_continuity(row, directory)
    bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match="accepted checkpoint"):
        import_evidence(path, row, absence(row))
    assert path.read_bytes() == before


def test_two_distinct_source_cards_map_to_one_verified_employee_without_recreating_catalog(tmp_path, monkeypatch):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    path = tmp_path / "private" / "checkpoint.json"
    rows = [source_fixture(tmp_path / str(i), target=bridge.target_fingerprint, identity="DOCUMENT-" + str(i),
                           token="DUPLICATE-CARD-" + str(i), mapping_mode="corrective_many_to_one")[0] for i in range(2)]
    employee = rows[0]["prerequisites"][0]["candidate"]
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    original = copy.deepcopy(json.loads(path.read_text())["entries"][_key(employee)])
    for i, row in enumerate(rows):
        assert row["source"] != employee["source"]
        import_evidence(path, row, absence(row))
        manifest = prepare_claim(path, [row], batch_id="many-one-" + str(i))
        request = preview_claim(path, manifest)
        receipt = verify_preview_claim(path, manifest, request, server.onec83_create_entity(**request))
        args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                           template_id=row["template"]["id"], template_version="1")
        assert args["payload"]["Employee_Key"] == EMPLOYEE_KEY
        server.onec83_create_entity(**args)
        result = server.onec83_get_write_receipt(args["operation_id"], database="test")
        resolve_claim(path, manifest, [row], result, [full_get_proof(server, row, manifest, result)])
    assert rows[0]["source"]["line"] != rows[1]["source"]["line"]
    final = json.loads(path.read_text())
    assert final["entries"][_key(employee)] == original
    assert screen_rows(path, rows)["newly_created_verified"] == 2
    assert sum(c[0] == "create" for c in client.calls) == 2
    assert not any(c[0] == "create" and c[1] == "Catalog_Employee" for c in client.calls)


@pytest.mark.parametrize("field", ["Number", "Code", "Код", "ТабельныйНомер", "ЛичныйНомер"])
def test_source_generated_numbering_never_copied_into_target_even_with_resealed_review(tmp_path, field):
    row, _ = source_fixture(tmp_path / "source")
    row["prepared"]["payload"][field] = "SYNTHETIC-ORDER"
    bind_review(row, tmp_path / "source")
    with pytest.raises(QueueManifestError, match="numbering"):
        import_evidence(tmp_path / "private" / "checkpoint.json", row, absence(row))


@pytest.mark.parametrize("mutation", ["document_number", "generated_origin", "creation_replaced", "creation_omitted"])
def test_source_identifier_origin_and_original_creation_date_cannot_be_silently_changed(tmp_path, mutation):
    directory = tmp_path / "source"
    row, _ = source_fixture(directory)
    scalar = row["source_evidence"]["scalar_bindings"]
    if mutation == "document_number": scalar[2]["source_field"] = "document.number"
    if mutation == "generated_origin": scalar[2]["value_origin"] = "source_assigned_identifier"
    if mutation == "creation_replaced": row["prepared"]["payload"]["CreatedAt"] = "2026-10-03T00:00:00"
    if mutation == "creation_omitted":
        row["prepared"]["payload"].pop("CreatedAt")
        scalar.pop(1)
    bind_review(row, directory)
    with pytest.raises(QueueManifestError): import_evidence(tmp_path / "private" / "checkpoint.json", row, absence(row))


def test_unavailable_creation_date_is_explicit_gap_without_fabricated_target_value(tmp_path):
    directory = tmp_path / "source"
    row, employee = source_fixture(directory)
    row["prepared"]["payload"].pop("CreatedAt")
    row["source_evidence"]["scalar_bindings"].pop(1)
    request = json.loads(open(row["source_evidence"]["addressed_request_ref"]["path"]).read())
    response = json.loads(open(row["source_evidence"]["addressed_response_ref"]["path"]).read())
    request["attribute_names"].remove("CreationDate")
    response["data"]["attributes"].pop("CreationDate")
    row["source_evidence"]["addressed_request_ref"] = artifact(directory, "doc-request", request)
    row["source_evidence"]["addressed_response_ref"] = artifact(directory, "doc-response", response)
    inventory = json.loads(open(row["source_evidence"]["date_inventory_ref"]["path"]).read())
    inventory.update(creation_date_status="unavailable", creation_date_source_field=None)
    inventory["dates"] = [item for item in inventory["dates"] if item["payload_field"] != "CreatedAt"]
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    roles["source_roles"].pop("attribute:CreationDate")
    row["source_evidence"]["date_inventory_ref"] = artifact(directory, "date-inventory", inventory)
    seal_date_roles(row, directory, roles)
    bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    assert screen_rows(path, [row])["ready_count"] == 1
    manifest = prepare_claim(path, [row], batch_id="explicit-gap")
    assert "CreatedAt" not in preview_claim(path, manifest)["payload"]


def test_late_relation_lineage_loss_prevents_apply_without_changing_claim_or_catalog(tmp_path, monkeypatch):
    server, bridge, client, path, row, manifest, receipt = prepare(tmp_path, monkeypatch)
    b = row["source_evidence"]["relation_bindings"][0]
    lineage = json.loads(open(b["catalog_lineage_ref"]["path"]).read())
    open(lineage["continuity_review_ref"]["path"], "w").write("{}")
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                    template_id=row["template"]["id"], template_version="1")
    assert path.read_bytes() == before and not any(c[0] == "create" for c in client.calls)


@pytest.mark.parametrize("status", ["unavailable", "not_present", "observed"])
@pytest.mark.parametrize("target_field", ["CreatedAt", "ИсходныйМоментРегистрации"])
def test_creation_date_substitution_rejected_with_resealed_semantic_role_proof(tmp_path, monkeypatch, status, target_field):
    directory = tmp_path / "source"
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    row, employee = source_fixture(directory, target=bridge.target_fingerprint)
    p = row["source_evidence"]
    request = json.loads(open(p["addressed_request_ref"]["path"]).read())
    response = json.loads(open(p["addressed_response_ref"]["path"]).read())
    request["attribute_names"].remove("CreationDate")
    response["data"]["attributes"].pop("CreationDate")
    p["addressed_request_ref"] = artifact(directory, "doc-request", request)
    p["addressed_response_ref"] = artifact(directory, "doc-response", response)
    b = next(b for b in p["scalar_bindings"] if b["payload_field"] == "CreatedAt")
    row["prepared"]["payload"].pop("CreatedAt")
    row["prepared"]["payload"][target_field] = row["prepared"]["payload"]["Date"]
    b.update(payload_field=target_field, source_field="document.date")
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    inventory.update(creation_date_status=status, creation_date_source_field="document.date" if status == "observed" else None)
    inventory["dates"] = [{"source_field": x["source_field"], "payload_field": x["payload_field"]}
                          for x in p["scalar_bindings"] if x["value_origin"] == "source_date"]
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    roles["source_roles"].pop("attribute:CreationDate")
    roles["target_roles"].pop("CreatedAt")
    roles["target_roles"][target_field] = "creation_date"
    # Even a resealed assertion that the header is creation data contradicts
    # the fixed header semantics. This must fail before any HR entry/claim.
    if status == "observed": roles["source_roles"]["document.date"] = "creation_date"
    p["date_inventory_ref"] = artifact(directory, "date-inventory", inventory)
    seal_date_roles(row, directory, roles)
    bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError): import_evidence(path, row, absence(row))
    with pytest.raises(QueueManifestError): prepare_claim(path, [row], batch_id="no-substitution")
    assert path.read_bytes() == before and not any(c[0] == "create" for c in client.calls)


def test_separately_observed_creation_with_same_value_and_arbitrary_target_name_is_preserved(tmp_path):
    directory = tmp_path / "source"
    row, employee = source_fixture(directory)
    p = row["source_evidence"]
    response = json.loads(open(p["addressed_response_ref"]["path"]).read())
    response["data"]["attributes"]["CreationDate"] = "2010-03-03"
    p["addressed_response_ref"] = artifact(directory, "doc-response", response)
    b = next(b for b in p["scalar_bindings"] if b["payload_field"] == "CreatedAt")
    target_field = "ИсходныйМоментРегистрации"
    row["prepared"]["payload"].pop("CreatedAt")
    row["prepared"]["payload"][target_field] = "2010-03-03T00:00:00"
    b["payload_field"] = target_field
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    next(d for d in inventory["dates"] if d["payload_field"] == "CreatedAt")["payload_field"] = target_field
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    roles["target_roles"][target_field] = roles["target_roles"].pop("CreatedAt")
    p["date_inventory_ref"] = artifact(directory, "date-inventory", inventory)
    seal_date_roles(row, directory, roles)
    bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="equal-distinct-date-facts")
    payload = preview_claim(path, manifest)["payload"]
    assert payload[target_field] == payload["Date"] == "2010-03-03T00:00:00"


@pytest.mark.parametrize("mutation", ["missing_role_evidence", "creation_as_business_date", "header_on_business_field", "missing_target_role"])
def test_date_role_evidence_cannot_be_omitted_or_relabelled_to_hide_substitution(tmp_path, mutation):
    directory = tmp_path / "source"
    row, _ = source_fixture(directory)
    p = row["source_evidence"]
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    b = next(b for b in p["scalar_bindings"] if b["payload_field"] == "CreatedAt")
    if mutation == "missing_role_evidence": roles["evidence_refs"] = []
    if mutation == "creation_as_business_date": b["date_role"] = "business_date"
    if mutation == "header_on_business_field":
        b.update(source_field="document.date", date_role="document_date")
        roles["target_roles"]["CreatedAt"] = "document_date"
    if mutation == "missing_target_role": roles["target_roles"].pop("CreatedAt")
    seal_date_roles(row, directory, roles)
    bind_review(row, directory)
    with pytest.raises(QueueManifestError): import_evidence(tmp_path / "private" / "checkpoint.json", row, absence(row))


@pytest.mark.parametrize("target_field", ["ДатаПриема", "УчебнаяДатаСобытия"])
def test_independently_witnessed_document_business_date_full_lifecycle(tmp_path, monkeypatch, target_field):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch, business_target_field=target_field)
    row, employee = business_mapping_fixture(tmp_path / "source", target=bridge.target_fingerprint, target_field=target_field)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    assert screen_rows(path, [row])["ready_count"] == 1
    manifest = prepare_claim(path, [row], batch_id="proven-business-date")
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, server.onec83_create_entity(**request))
    args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                       template_id=row["template"]["id"], template_version="1")
    assert args["payload"][target_field] == args["payload"]["Date"] == "2010-03-03T00:00:00"
    server.onec83_create_entity(**args)
    result = server.onec83_get_write_receipt(args["operation_id"], database="test")
    assert resolve_claim(path, manifest, [row], result, [full_get_proof(server, row, manifest, result)])["status"] == "resolved"
    assert sum(c[0] == "create" for c in client.calls) == 1 and not any(c[0] == "post" for c in client.calls)


@pytest.mark.parametrize("mutation", ["missing_witness", "foreign_native", "foreign_kind", "foreign_source", "foreign_field",
                                     "foreign_template", "foreign_entity", "foreign_target", "foreign_prepared", "foreign_read",
                                     "foreign_role_digest", "foreign_origin", "missing_source_semantics", "empty_source_evidence",
                                     "ambiguous_meaning", "target_creation_semantics", "missing_target_evidence", "stale_review"])
def test_business_date_witness_missing_foreign_ambiguous_or_creation_semantics_hold(tmp_path, mutation):
    directory = tmp_path / "source"
    row, employee = business_mapping_fixture(directory, target_field="УчебнаяДатаСобытия")
    b = next(b for b in row["source_evidence"]["scalar_bindings"] if "business_date_witness_ref" in b)
    witness = json.loads(open(b["business_date_witness_ref"]["path"]).read())
    if mutation == "missing_witness": b.pop("business_date_witness_ref")
    if mutation == "foreign_native": witness["document_identity"]["native_reference"] = "OTHER-DOCUMENT"
    if mutation == "foreign_kind": witness["document_identity"]["metadata_kind"] = "reference"
    if mutation == "foreign_source": witness["document_source"]["line"] = "another-document"
    if mutation == "foreign_field": witness["payload_field"] = "ДатаПриема"
    if mutation == "foreign_template": witness["template"] = {"id": "another", "version": "1"}
    if mutation == "foreign_entity": witness["entity_set"] = "Document_Other"
    if mutation == "foreign_target": witness["target_fingerprint"] = "other-target"
    if mutation == "foreign_prepared": witness["prepared_sha256"] = "0" * 64
    if mutation == "foreign_read": witness["addressed_response_sha256"] = "0" * 64
    if mutation == "foreign_role_digest": witness["date_roles_sha256"] = "0" * 64
    if mutation == "foreign_origin": witness["origin_evidence_sha256"] = "0" * 64
    if mutation in {"missing_source_semantics", "empty_source_evidence", "ambiguous_meaning", "target_creation_semantics", "missing_target_evidence"}:
        key = "source_semantics_ref" if mutation in {"missing_source_semantics", "empty_source_evidence"} else "target_semantics_ref"
        value = json.loads(open(witness[key]["path"]).read())
        if mutation == "missing_source_semantics": value["status"] = "unverified"
        if mutation in {"empty_source_evidence", "missing_target_evidence"}: value["evidence_refs"] = []
        if mutation == "ambiguous_meaning": value["business_meaning"] = "ANOTHER_EVENT"
        if mutation == "target_creation_semantics": value["date_role"] = "creation_date"
        witness[key] = artifact(directory, "changed-semantics", value)
    if mutation != "missing_witness": reseal_business_witness(row, directory, witness)
    else: bind_review(row, directory)
    if mutation == "stale_review":
        review = json.loads(open(witness["independent_review_ref"]["path"]).read())
        review["mapping_sha256"] = "0" * 64
        witness["independent_review_ref"] = artifact(directory, "business-mapping-review", review)
        b["business_date_witness_ref"] = artifact(directory, "business-witness", witness)
        bind_review(row, directory)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError): import_evidence(path, row, absence(row))
    assert path.read_bytes() == before


def test_business_witness_cannot_override_known_target_creation_role(tmp_path):
    directory = tmp_path / "source"
    row, employee = business_mapping_fixture(directory, target_field="УчебнаяДатаСобытия")
    p = row["source_evidence"]
    b = next(b for b in p["scalar_bindings"] if "business_date_witness_ref" in b)
    witness = json.loads(open(b["business_date_witness_ref"]["path"]).read())
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    roles = json.loads(open(inventory["role_evidence_ref"]["path"]).read())
    roles["target_roles"][b["payload_field"]] = "creation_date"
    seal_date_roles(row, directory, roles)
    inventory = json.loads(open(p["date_inventory_ref"]["path"]).read())
    witness.update(date_roles_sha256=inventory["role_evidence_ref"]["sha256"], origin_evidence_sha256=b["origin_evidence_ref"]["sha256"])
    target = json.loads(open(witness["target_semantics_ref"]["path"]).read())
    target["date_roles_sha256"] = witness["date_roles_sha256"]
    witness["target_semantics_ref"] = artifact(directory, "target-business-meaning", target)
    reseal_business_witness(row, directory, witness)
    with pytest.raises(QueueManifestError): import_evidence(tmp_path / "private" / "checkpoint.json", row, absence(row))


def test_late_business_witness_loss_holds_apply_with_same_claim(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    row, employee = business_mapping_fixture(directory, target=bridge.target_fingerprint)
    path = tmp_path / "private" / "checkpoint.json"
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY))
    import_evidence(path, row, absence(row))
    manifest = prepare_claim(path, [row], batch_id="late-business-proof-loss")
    req = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, req, server.onec83_create_entity(**req))
    b = next(b for b in row["source_evidence"]["scalar_bindings"] if "business_date_witness_ref" in b)
    witness = json.loads(open(b["business_date_witness_ref"]["path"]).read())
    from pathlib import Path
    Path(witness["source_semantics_ref"]["path"]).unlink()
    before = path.read_bytes()
    with pytest.raises(QueueManifestError):
        apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                    template_id=row["template"]["id"], template_version="1")
    assert path.read_bytes() == before and not any(c[0] == "create" for c in client.calls)


def loaded(ref):
    return json.loads(open(ref['path']).read())


def corrected_header_fixture(directory, *, target, identity='SYNTHETIC-DOC-TOKEN'):
    """Fresh independently resealed synthetic observation; not native lifetime proof."""
    row, employee = source_fixture(directory, target=target, identity=identity)
    row['source']['date'] = '2010-03-04'
    row['prepared']['payload']['Date'] = '2010-03-04T00:00:00'
    p = row['source_evidence']
    response = loaded(p['addressed_response_ref'])
    response['data']['document']['date'] = row['source']['date']
    p['addressed_response_ref'] = artifact(directory, 'corrected-header', response)
    inv = loaded(p['date_inventory_ref']); inv['document_source'] = row['source']
    roles = loaded(inv['role_evidence_ref']); roles['document_source'] = row['source']
    p['date_inventory_ref'] = artifact(directory, 'corrected-inventory', inv)
    for i, b in enumerate(p['scalar_bindings']):
        origin = loaded(b['origin_evidence_ref']); origin['document_source'] = row['source']
        b['origin_evidence_ref'] = artifact(directory, 'corrected-origin-' + str(i), origin)
    seal_date_roles(row, directory, roles)
    for b in p['relation_bindings']:
        request = loaded(b['catalog_request_ref']); request['as_of'] = row['source']['date']
        answer = loaded(b['catalog_response_ref']); answer['data']['readContext']['asOf'] = row['source']['date']
        b['catalog_request_ref'] = artifact(directory, 'corrected-catalog-request', request)
        b['catalog_response_ref'] = artifact(directory, 'corrected-catalog-response', answer)
        lineage = loaded(b['catalog_lineage_ref']); lineage['document_source'] = row['source']
        old_request = loaded(lineage['original_catalog_request_ref']); old_request['as_of'] = row['source']['date']
        old_answer = loaded(lineage['original_catalog_response_ref']); old_answer['data']['readContext']['asOf'] = row['source']['date']
        lineage['original_catalog_request_ref'] = artifact(directory, 'corrected-original-request', old_request)
        lineage['original_catalog_response_ref'] = artifact(directory, 'corrected-original-response', old_answer)
        corr = loaded(lineage['correspondence_ref']); corr['document_source'] = row['source']
        corr['source_relation']['as_of'] = row['source']['date']
        lineage['correspondence_ref'] = artifact(directory, 'corrected-correspondence', corr)
        b['catalog_lineage_ref'] = artifact(directory, 'corrected-lineage', lineage)
    seal_continuity(row, directory); bind_review(row, directory)
    return row, employee


def legacy_hr_entry(path, row):
    """Synthetic old date-qualified checkpoint; preserve every original claim/ID."""
    from onec_odata_mcp.queue_checkpoint import _legacy_key, _write
    index = json.loads(path.read_text()); old = _key(row); legacy = _legacy_key(row)
    entry = index['entries'].pop(old); entry.pop('hr_entity_set')
    index['entries'][legacy] = entry
    assert not index['claims']  # Build old fixture before reserving its original operation.
    _write(path, index)
    return legacy


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('stage', ['prepared', 'pending', 'resolved'])
def test_same_native_corrected_header_never_emits_second_operation(tmp_path, monkeypatch, legacy, stage):
    server, bridge, client = bridge_fixture(tmp_path, monkeypatch)
    row, employee = source_fixture(tmp_path/'first', target=bridge.target_fingerprint)
    path = tmp_path/'private'/'checkpoint.json'
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY)); import_evidence(path, row, absence(row))
    key = legacy_hr_entry(path, row) if legacy else _key(row)
    manifest = prepare_claim(path, [row], batch_id='original-native-operation')
    args = manifest['rows'][0]
    if stage != 'prepared':
        request = preview_claim(path, manifest)
        receipt = verify_preview_claim(path, manifest, request, server.onec83_create_entity(**request))
        args = apply_claim(path, manifest, receipt, target_fingerprint=bridge.target_fingerprint,
                           template_id=row['template']['id'], template_version='1')
        server.onec83_create_entity(**args)
        saved = server.onec83_get_write_receipt(args['operation_id'], database='test')
        if stage == 'resolved':
            resolve_claim(path, manifest, [row], saved, [full_get_proof(server, row, manifest, saved)])
    before = path.read_bytes()
    changed, _ = corrected_header_fixture(tmp_path/'corrected', target=bridge.target_fingerprint)
    assert _key(changed) == _key(row)  # Date-independent current identity; old key is retained separately.
    with pytest.raises(QueueManifestError, match='HR native Document facts changed'):
        import_evidence(path, changed, absence(changed))
    screen = screen_rows(path, [changed]); assert screen['ready_count'] == 0 and len(screen['held']) == 1
    with pytest.raises(QueueManifestError, match='HR native Document facts changed'):
        prepare_claim(path, [changed], batch_id='forbidden-second-operation')
    assert path.read_bytes() == before
    original = json.loads(before)
    assert original['claims']['original-native-operation']['rows'][0]['key'] == key
    assert original['claims']['original-native-operation']['manifest']['rows'][0]['operation_id'] == args['operation_id']
    assert sum(c[0] == 'create' for c in client.calls) == (0 if stage == 'prepared' else 1)
    assert not any(c[0] == 'post' for c in client.calls)


def test_same_native_changed_payload_holds_without_replacing_entry(tmp_path):
    row, employee = source_fixture(tmp_path/'source')
    path = tmp_path/'private'/'checkpoint.json'
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY)); import_evidence(path, row, absence(row))
    before = path.read_bytes()
    row['prepared']['payload']['ExternalOrder'] = 'SYNTHETIC-CORRECTED-FACT'
    p = row['source_evidence']; answer = loaded(p['addressed_response_ref'])
    answer['data']['attributes']['ExternalOrder'] = row['prepared']['payload']['ExternalOrder']
    p['addressed_response_ref'] = artifact(tmp_path/'source', 'corrected-fact', answer); bind_review(row, tmp_path/'source')
    with pytest.raises(QueueManifestError, match='HR native Document facts changed'):
        import_evidence(path, row, absence(row))
    assert path.read_bytes() == before


def test_same_native_distinct_target_entity_claims_are_separate(tmp_path):
    first, employee = source_fixture(tmp_path/'first')
    second, _ = source_fixture(tmp_path/'second')
    second['prepared']['entity_set'] = 'Document_SyntheticEmploymentEpisode'
    p = second['source_evidence']; inventory = loaded(p['date_inventory_ref'])
    roles = loaded(inventory['role_evidence_ref']); roles['entity_set'] = second['prepared']['entity_set']
    seal_date_roles(second, tmp_path/'second', roles); bind_review(second, tmp_path/'second')
    path = tmp_path/'private'/'checkpoint.json'
    import_evidence(path, employee, found(employee, EMPLOYEE_KEY)); import_evidence(path, first, absence(first))
    original = prepare_claim(path, [first], batch_id='first-target-entity')
    import_evidence(path, second, absence(second))
    assert screen_rows(path, [second])['ready_count'] == 1
    other = prepare_claim(path, [second], batch_id='other-target-entity')
    assert _key(first) != _key(second)
    assert original['rows'][0]['entity_set'] != other['rows'][0]['entity_set']
    assert original['rows'][0]['operation_id'] != other['rows'][0]['operation_id']


def source_date_type_proof(row, directory, *, field='HireDate', type_code='D'):
    """Exact accepted metadata wire shape; no invented Date valueKind or runtime proof."""
    p = row['source_evidence']; data = loaded(p['addressed_response_ref'])['data']
    kinds = {'Employee':('U','5'), 'HireDate':('D','0'), 'CreationDate':('D','0'), 'ExternalOrder':('S','0')}
    attrs = [{'id':str(i+1), 'name':name, 'typeCode':type_code if name==field else kinds[name][0],
              'length':None, 'precision':None, 'refTypeId':kinds[name][1]} for i,name in enumerate(data['attributes'])]
    metadata = {'schemaVersion':'1.0', 'status':'ok', 'operation':'metadata_inventory', 'sourceAlias':'synthetic77',
                'data':{'status':'metadata_catalog', 'counts':{'catalogs':0,'documents':1,'attributes':len(attrs)},
                        'catalogs':[], 'documents':[{'id':'77','name':'SyntheticHire','headAttributes':attrs,'tableAttributes':[]}]}}
    core = {'schema_version':1,'status':'accepted_hr_source_date_types','document_source':row['source'],
            'addressed_request_sha256':p['addressed_request_ref']['sha256'],
            'addressed_response_sha256':p['addressed_response_ref']['sha256'],
            'metadata_response_ref':artifact(directory,'synthetic-date-metadata',metadata),
            'evidence_refs':[artifact(directory,'synthetic-metadata-scope',{'synthetic':True,'purpose':'same configured source metadata/document provenance'})]}
    core['independent_review_ref'] = artifact(directory,'synthetic-type-review',
        {'schema_version':1,'status':'accepted_hr_source_date_types_review','types_sha256':_digest(core)})
    inv = loaded(p['date_inventory_ref']); inv['source_date_types_ref'] = artifact(directory,'synthetic-types',core)
    p['date_inventory_ref'] = artifact(directory,'typed-inventory',inv)
    bind_review(row,directory)


def omit_date_binding(row, directory, *, field='HireDate', target_field='ДатаПриема', unknown_role=False):
    p = row['source_evidence']; row['prepared']['payload'].pop(target_field)
    p['scalar_bindings'] = [b for b in p['scalar_bindings'] if b['payload_field']!=target_field]
    inv = loaded(p['date_inventory_ref'])
    inv['dates'] = [item for item in inv['dates'] if item['payload_field']!=target_field]
    p['date_inventory_ref'] = artifact(directory,'omitted-inventory',inv)
    if unknown_role:
        roles = loaded(inv['role_evidence_ref']); roles['source_roles'].pop('attribute:'+field)
        roles['target_roles'].pop(target_field); seal_date_roles(row,directory,roles)
    bind_review(row,directory)


def retain_date_gap(row, directory, *, field='HireDate'):
    p = row['source_evidence']; inv = loaded(p['date_inventory_ref']); roles = loaded(inv['role_evidence_ref'])
    field = 'attribute:'+field
    core = {'schema_version':1,'status':'accepted_hr_unmapped_date_gap','document_source':row['source'],
            'source_field':field,'date_role':roles['source_roles'].get(field),
            'observed_value':loaded(p['addressed_response_ref'])['data']['attributes'][field[10:]],
            'addressed_request_sha256':p['addressed_request_ref']['sha256'],
            'addressed_response_sha256':p['addressed_response_ref']['sha256'],
            'date_roles_sha256':inv['role_evidence_ref']['sha256'],'prepared_sha256':_digest(row['prepared']),
            'template':row['template'],'entity_set':row['prepared']['entity_set'],
            'target_fingerprint':row['target_fingerprint'],'database':row.get('database'),
            'partial_semantics':'retained-source-fact-not-written; mapping-and-phase-acceptance-pending',
            'reason':'mapping_unresolved','explanation':'SYNTHETIC reviewed historical field correspondence remains unknown; no target field invented',
            'evidence_refs':[artifact(directory,'synthetic-gap-facts',{'synthetic':True,'purpose':'retained observed date and independently reviewed partial mapping disposition'})]}
    core['independent_review_ref'] = artifact(directory,'synthetic-gap-review',
        {'schema_version':1,'status':'accepted_hr_unmapped_date_gap_review','gap_sha256':_digest(core)})
    inv.update(inventory_status='partial',unmapped_date_gaps=[artifact(directory,'synthetic-gap',core)])
    p['date_inventory_ref'] = artifact(directory,'partial-inventory',inv); bind_review(row,directory)


@pytest.mark.parametrize('typed_unknown', [False, True])
def test_observed_filled_date_cannot_silently_disappear(tmp_path, typed_unknown):
    directory = tmp_path/'source'; row, employee = source_fixture(directory)
    omit_date_binding(row,directory,unknown_role=typed_unknown)
    if typed_unknown:
        source_date_type_proof(row,directory)
    path = tmp_path/'private'/'checkpoint.json'
    import_evidence(path,employee,found(employee,EMPLOYEE_KEY)); before=path.read_bytes()
    with pytest.raises(QueueManifestError, match='observed filled dates'):
        import_evidence(path,row,absence(row))
    assert path.read_bytes()==before


@pytest.mark.parametrize('kind', ['known_business','typed_unknown','known_creation'])
def test_reviewed_unmapped_date_partial_actual_lifecycle_retains_fact(tmp_path, monkeypatch, kind):
    server,bridge,client=bridge_fixture(tmp_path,monkeypatch)
    directory=tmp_path/'source'; row,employee=source_fixture(directory,target=bridge.target_fingerprint)
    field,target_field = ('CreationDate','CreatedAt') if kind=='known_creation' else ('HireDate','ДатаПриема')
    omit_date_binding(row,directory,field=field,target_field=target_field,unknown_role=kind=='typed_unknown')
    if kind=='typed_unknown':
        response=loaded(row['source_evidence']['addressed_response_ref'])
        response['data']['attributes'][field]='2010-03-03T13:14:15.0000000'
        row['source_evidence']['addressed_response_ref']=artifact(directory,'typed-ISO-source',response)
        source_date_type_proof(row,directory,field=field)
    retain_date_gap(row,directory,field=field)
    inv=loaded(row['source_evidence']['date_inventory_ref']); gap=loaded(inv['unmapped_date_gaps'][0])
    assert inv['inventory_status']=='partial' and gap['observed_value']==loaded(row['source_evidence']['addressed_response_ref'])['data']['attributes'][field]
    path=tmp_path/'private'/'checkpoint.json'
    import_evidence(path,employee,found(employee,EMPLOYEE_KEY)); import_evidence(path,row,absence(row))
    assert screen_rows(path,[row])['ready_count']==1
    manifest=prepare_claim(path,[row],batch_id='honest-partial-date-gap')
    req=preview_claim(path,manifest); receipt=verify_preview_claim(path,manifest,req,server.onec83_create_entity(**req))
    args=apply_claim(path,manifest,receipt,target_fingerprint=bridge.target_fingerprint,template_id=row['template']['id'],template_version='1')
    assert target_field not in args['payload']
    server.onec83_create_entity(**args); result=server.onec83_get_write_receipt(args['operation_id'],database='test')
    assert resolve_claim(path,manifest,[row],result,[full_get_proof(server,row,manifest,result)])['status']=='resolved'
    assert sum(c[0]=='create' for c in client.calls)==1 and not any(c[0]=='post' for c in client.calls)
    assert loaded(row['source_evidence']['date_inventory_ref'])['inventory_status']=='partial'


@pytest.mark.parametrize('mutation',['value','source','field','role','response','prepared','target','entity','roles','reason','review','facts','complete'])
def test_resealed_partial_gap_rejects_foreign_missing_unreviewed_fact(tmp_path, mutation):
    directory=tmp_path/'source';row,employee=source_fixture(directory)
    omit_date_binding(row,directory);retain_date_gap(row,directory)
    p=row['source_evidence'];inv=loaded(p['date_inventory_ref']);gap=loaded(inv['unmapped_date_gaps'][0])
    if mutation=='value':gap['observed_value']='2000-01-01'
    if mutation=='source':gap['document_source']['line']='native-document-'+'0'*64
    if mutation=='field':gap['source_field']='attribute:UnreadDate'
    if mutation=='role':gap['date_role']='creation_date'
    if mutation=='response':gap['addressed_response_sha256']='0'*64
    if mutation=='prepared':gap['prepared_sha256']='0'*64
    if mutation=='target':gap['target_fingerprint']='another-target'
    if mutation=='entity':gap['entity_set']='Document_Foreign'
    if mutation=='roles':gap['date_roles_sha256']='0'*64
    if mutation=='reason':gap['reason']='silently-omit'
    if mutation=='facts':gap['evidence_refs']=[]
    if mutation=='complete':inv['inventory_status']='complete'
    if mutation!='review':
        core={k:v for k,v in gap.items() if k!='independent_review_ref'}
        gap['independent_review_ref']=artifact(directory,'resealed-gap-review',{'schema_version':1,'status':'accepted_hr_unmapped_date_gap_review','gap_sha256':_digest(core)})
    else:
        gap['explanation']='Changed after independent acceptance'
    inv['unmapped_date_gaps']=[artifact(directory,'resealed-gap',gap)]
    p['date_inventory_ref']=artifact(directory,'resealed-inventory',inv);bind_review(row,directory)
    path=tmp_path/'private'/'checkpoint.json';import_evidence(path,employee,found(employee,EMPLOYEE_KEY));before=path.read_bytes()
    with pytest.raises(QueueManifestError):import_evidence(path,row,absence(row))
    assert path.read_bytes()==before


def test_ISO_string_without_known_date_type_or_role_is_not_guessed(tmp_path):
    directory=tmp_path/'source';row,employee=source_fixture(directory)
    omit_date_binding(row,directory,unknown_role=True);source_date_type_proof(row,directory,type_code='S')
    path=tmp_path/'private'/'checkpoint.json';import_evidence(path,employee,found(employee,EMPLOYEE_KEY))
    assert import_evidence(path,row,absence(row))['status']=='missing_verified'


@pytest.mark.parametrize('mutation',['document','alias','response','evidence','review','ambiguous','missing_field'])
def test_date_type_proof_cannot_hide_unknown_semantic_date_under_foreign_metadata(tmp_path,mutation):
    directory=tmp_path/'source';row,employee=source_fixture(directory)
    omit_date_binding(row,directory,unknown_role=True);source_date_type_proof(row,directory);retain_date_gap(row,directory)
    p=row['source_evidence'];inv=loaded(p['date_inventory_ref']);types=loaded(inv['source_date_types_ref'])
    metadata=loaded(types['metadata_response_ref'])
    if mutation=='document':metadata['data']['documents'][0]['id']='99'
    if mutation=='alias':metadata['sourceAlias']='foreign77'
    if mutation=='response':types['addressed_response_sha256']='0'*64
    if mutation=='evidence':types['evidence_refs']=[]
    if mutation=='ambiguous':metadata['data']['documents'].append(copy.deepcopy(metadata['data']['documents'][0]))
    if mutation=='missing_field':metadata['data']['documents'][0]['headAttributes']=[a for a in metadata['data']['documents'][0]['headAttributes'] if a['name']!='HireDate']
    if mutation=='review':types['independent_review_ref']=artifact(directory,'foreign-type-review',{'schema_version':1,'status':'accepted_hr_source_date_types_review','types_sha256':'0'*64})
    types['metadata_response_ref']=artifact(directory,'resealed-metadata',metadata)
    if mutation!='review':
        core={k:v for k,v in types.items() if k!='independent_review_ref'}
        types['independent_review_ref']=artifact(directory,'resealed-type-review',{'schema_version':1,'status':'accepted_hr_source_date_types_review','types_sha256':_digest(core)})
    inv['source_date_types_ref']=artifact(directory,'resealed-types',types)
    p['date_inventory_ref']=artifact(directory,'resealed-typed-inventory',inv);bind_review(row,directory)
    path=tmp_path/'private'/'checkpoint.json';import_evidence(path,employee,found(employee,EMPLOYEE_KEY));before=path.read_bytes()
    with pytest.raises(QueueManifestError):import_evidence(path,row,absence(row))
    assert path.read_bytes()==before


def test_unknown_legacy_native_entity_holds_without_backfill(tmp_path):
    row,employee=source_fixture(tmp_path/'first')
    path=tmp_path/'private'/'checkpoint.json'
    import_evidence(path,employee,found(employee,EMPLOYEE_KEY));import_evidence(path,row,absence(row))
    legacy_hr_entry(path,row);before=path.read_bytes()
    changed,_=corrected_header_fixture(tmp_path/'corrected',target=row['target_fingerprint'])
    with pytest.raises(QueueManifestError,match='legacy entity is unknown'):
        import_evidence(path,changed,absence(changed))
    assert path.read_bytes()==before and screen_rows(path,[changed])['ready_count']==0


def test_conflicting_legacy_native_entry_blocks_claim_eligibility_before_apply(tmp_path,monkeypatch):
    from onec_odata_mcp.queue_checkpoint import _legacy_key,_write
    server,bridge,client,path,row,manifest,receipt=prepare(tmp_path,monkeypatch)
    changed,_=corrected_header_fixture(tmp_path/'second',target=bridge.target_fingerprint)
    index=json.loads(path.read_text())
    original=index['entries'][_key(row)]
    extra={**original,'source':changed['source'],'candidate_sha256':_digest(changed)}
    extra.pop('hr_entity_set')
    index['entries'][_legacy_key(changed)]=extra
    _write(path,index);before=path.read_bytes()
    with pytest.raises(QueueManifestError,match='HR native Document'):
        apply_claim(path,manifest,receipt,target_fingerprint=bridge.target_fingerprint,template_id=row['template']['id'],template_version='1')
    assert path.read_bytes()==before and not any(c[0]=='create' for c in client.calls)


def test_missing_retained_gap_fact_blocks_apply_and_preserves_claim(tmp_path,monkeypatch):
    server,bridge,client=bridge_fixture(tmp_path,monkeypatch)
    directory=tmp_path/'source';row,employee=source_fixture(directory,target=bridge.target_fingerprint)
    omit_date_binding(row,directory);retain_date_gap(row,directory)
    path=tmp_path/'private'/'checkpoint.json'
    import_evidence(path,employee,found(employee,EMPLOYEE_KEY));import_evidence(path,row,absence(row))
    manifest=prepare_claim(path,[row],batch_id='retained-gap-byte-check')
    req=preview_claim(path,manifest);receipt=verify_preview_claim(path,manifest,req,server.onec83_create_entity(**req))
    before=path.read_bytes()
    gap=loaded(loaded(row['source_evidence']['date_inventory_ref'])['unmapped_date_gaps'][0])
    from pathlib import Path
    Path(gap['evidence_refs'][0]['path']).unlink()
    with pytest.raises(QueueManifestError,match='artifact unavailable'):
        apply_claim(path,manifest,receipt,target_fingerprint=bridge.target_fingerprint,template_id=row['template']['id'],template_version='1')
    assert path.read_bytes()==before and not any(c[0]=='create' for c in client.calls)


def test_retained_gap_cannot_also_write_unknown_Date_as_business_fact(tmp_path):
    directory=tmp_path/'source';row,employee=source_fixture(directory)
    omit_date_binding(row,directory,unknown_role=True);source_date_type_proof(row,directory)
    value=loaded(row['source_evidence']['addressed_response_ref'])['data']['attributes']['HireDate']
    row['prepared']['payload']['UnclassifiedString']=value
    inv=loaded(row['source_evidence']['date_inventory_ref'])
    binding={'payload_field':'UnclassifiedString','source_field':'attribute:HireDate','transform':'exact',
             'value_origin':'business_fact','date_role':None,'origin_evidence_ref':artifact(directory,'unclassified-origin',
              {'schema_version':1,'status':'accepted_hr_scalar_origin','document_source':row['source'],
               'source_field':'attribute:HireDate','payload_field':'UnclassifiedString','value_origin':'business_fact',
               'date_role':None,'date_roles_sha256':inv['role_evidence_ref']['sha256'],
               'evidence_refs':[artifact(directory,'unclassified-facts',{'synthetic':True})]})}
    row['source_evidence']['scalar_bindings'].append(binding)
    retain_date_gap(row,directory)
    path=tmp_path/'private'/'checkpoint.json';import_evidence(path,employee,found(employee,EMPLOYEE_KEY));before=path.read_bytes()
    with pytest.raises(QueueManifestError):import_evidence(path,row,absence(row))
    assert path.read_bytes()==before
