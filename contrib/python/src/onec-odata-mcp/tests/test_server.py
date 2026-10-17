import json
from pathlib import Path
import threading
import time

import pytest
from requests.exceptions import Timeout

from onec_odata_mcp import server as server_module
from onec_odata_mcp.client import (ODataAcceptedResponseError, ODataClientError,
                                   ODataClient, ODataHTTPError, ODataTransportError)
from onec_odata_mcp.config import RuntimeConfig
from onec_odata_mcp.diagnostics import (PerformanceCall, PerformanceLogger,
                                        instrument_tool_call, record_odata_attempt,
                                        record_request_call)
from onec_odata_mcp.ledger import UncertainWrite, WriteLedger, request_digest
from onec_odata_mcp.log_summary import summarize
from onec_odata_mcp.server import Bridge


class FakeMetadata:
    def list_entity_sets(self, prefix=None):
        sets = [
            {"name": "Document_Example"},
            {"name": "Document_Nonpostable"},
            {"name": "Document_Example_Lines"},
            {"name": "Document_BadMarker_Rows"},
            {"name": "Catalog_Контрагенты"},
            {"name": "Catalog_БанковскиеСчета"},
            {"name": "Catalog_Example"},
            {"name": "InformationRegister_Example"},
            {"name": "AccountingRegister_Example"},
        ]
        if prefix is not None:
            sets = [item for item in sets if item["name"].startswith(prefix)]
        return sets

    def describe_entity_set(self, name):
        if name == "Document_Example":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"},
                                   {"name": "Number", "type": "Edm.String"},
                                   {"name": "Date", "type": "Edm.DateTime"},
                                   {"name": "DataVersion"},
                                   {"name": "DeletionMark", "type": "Edm.Boolean"},
                                   {"name": "Amount", "type": "Edm.Decimal"},
                                   {"name": "GuidValue", "type": "Edm.Guid"},
                                   {"name": "ComplexValue", "type": "Example.Complex"}],
                    "tabularSections": ["Lines"],
                    "isTabularRowEntity": False,
                    "entity_type": "StandardODATA.Document_Example",
                    "entity_type_name": "Document_Example"}
        if name == "Document_Nonpostable":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"}, {"name": "Number"},
                                   {"name": "DataVersion"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "Document_Nonpostable"}
        if name == "Document_Example_Lines":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"}, {"name": "LineNumber"}],
                    "tabularSections": [],
                    "isTabularRowEntity": True,
                    "entity_type_name": "Document_Example_Lines_RowType",
                    "parentDocument": "Document_Example",
                    "tabularSection": "Lines"}
        if name == "Document_BadMarker_Rows":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"}, {"name": "LineNumber"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "Document_BadMarker_Rows_RowType",
                    "parentDocument": "Document_Example",
                    "tabularSection": "Lines"}
        if name == "Catalog_Example":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"},
                                   {"name": "ИНН", "type": "Edm.String"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "Catalog_Example"}
        if name == "InformationRegister_Example":
            return {"name": name,
                    "properties": [{"name": "Period"}, {"name": "Value"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "InformationRegister_Example"}
        if name == "AccountingRegister_Example":
            return {"name": name,
                    "properties": [{"name": "Recorder_Key"}, {"name": "RecordSet"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "AccountingRegister_Example"}
        if name == "Catalog_Контрагенты":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"}, {"name": "Description"},
                                   {"name": "ИНН", "type": "Edm.String"},
                                   {"name": "НаименованиеПолное"},
                                   {"name": "ЮридическоеФизическоеЛицо"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "Catalog_Контрагенты"}
        if name == "Catalog_БанковскиеСчета":
            return {"name": name,
                    "properties": [{"name": "Ref_Key"}, {"name": "НомерСчета"},
                                   {"name": "Owner"}, {"name": "Owner_Type"}],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": "Catalog_БанковскиеСчета"}
        if name == "Catalog_CreateVerification":
            return {
                "name": name,
                "properties": [
                    {"name": "Ref_Key", "type": "Edm.Guid"},
                    {"name": "Description", "type": "Edm.String"},
                    {"name": "Owner", "type": "Edm.Guid"},
                    {"name": "Owner_Type", "type": "Edm.String"},
                    {"name": "Bank", "type": "Edm.Guid"},
                    {"name": "Bank_Type", "type": "Edm.String"},
                    {"name": "Currency", "type": "Edm.Guid"},
                    {"name": "Currency_Type", "type": "Edm.String"},
                    {"name": "AccountNumber", "type": "Edm.String"},
                    {"name": "AccountKind", "type": "Edm.String"},
                    {"name": "AccountKind_Type", "type": "Edm.String"},
                    {"name": "CurrencyFlag", "type": "Edm.Boolean"},
                    {"name": "ComplexValue", "type": "Example.Complex"},
                ],
                "tabularSections": [],
                "isTabularRowEntity": False,
                "entity_type_name": "Catalog_CreateVerification",
            }
        raise ValueError("entity set not published")

    def has_action(self, name, action):
        return name == "Document_Example" and action in {"Post", "Unpost"}

    def resolve_enum_value(self, enum_name, member_name, field=None, account_context=None):
        enum_name = enum_name.removeprefix("EnumRef.")
        result = {
            "enumName": enum_name,
            "memberName": member_name,
            "xmlType": f"EnumRef.{enum_name}",
            "odataType": (
                f"StandardODATA.{enum_name}"
                if account_context is not None
                else f"StandardODATA.EnumRef_{enum_name}"
            ),
            "metadataVerified": True,
        }
        if field is not None:
            result["payloadFragment"] = {
                field: member_name,
                f"{field}_Type": result["odataType"],
            }
        if account_context is not None:
            result["accountContext"] = account_context
            result["propertyCompatibility"] = "observed_for_context"
        return result

    def validate_payload(self, name, payload):
        self.describe_entity_set(name)
        if name == "InformationRegister_Example":
            if set(payload) - {"Period", "Value"}:
                raise ValueError("unknown field")
            return
        if name == "Catalog_Example":
            if set(payload) - {"Ref_Key", "ИНН"}:
                raise ValueError("unknown field")
            return
        if name == "Catalog_Контрагенты":
            if set(payload) - {"Ref_Key", "Description", "ИНН", "НаименованиеПолное",
                               "ЮридическоеФизическоеЛицо"}:
                raise ValueError("unknown field")
            return
        if name == "Catalog_БанковскиеСчета":
            if set(payload) - {"Ref_Key", "НомерСчета", "Owner", "Owner_Type"}:
                raise ValueError("unknown field")
            return
        if name == "Catalog_CreateVerification":
            allowed = {
                "Ref_Key", "Description", "Owner", "Owner_Type", "Bank",
                "Bank_Type", "Currency", "Currency_Type", "AccountNumber",
                "AccountKind", "AccountKind_Type", "CurrencyFlag", "ComplexValue",
            }
            if set(payload) - allowed:
                raise ValueError("unknown field")
            return
        if set(payload) - {"Ref_Key", "Number", "Date", "DeletionMark", "Amount",
                           "GuidValue", "ComplexValue", "Lines"}:
            raise ValueError("unknown field")

    def normalize_payload(self, name, payload):
        self.validate_payload(name, payload)
        if name != "Document_Example" or "Lines" not in payload:
            return dict(payload)
        data = dict(payload)
        data["Lines"] = [
            self._normalize_line_row(row)
            for row in payload["Lines"]
        ]
        return data

    def _normalize_line_row(self, row):
        normalized = {}
        for field, value in row.items():
            if field in {"LineNumber", "Multiplicity"} and isinstance(value, int):
                normalized[field] = str(value)
            elif (
                isinstance(value, dict)
                and (value.get("kind") or value.get("onec83ValueType")) in {"EnumRef", "enumRef", "enum"}
            ):
                normalized[field] = str(value.get("member") or value.get("memberName") or value.get("value"))
                enum_name = str(value.get("enum") or value.get("enumName")).removeprefix("EnumRef.")
                normalized[f"{field}_Type"] = f"StandardODATA.EnumRef_{enum_name}"
            else:
                normalized[field] = value
        return normalized


class FakeReferenceMetadata(FakeMetadata):
    """Synthetic CSDL descriptions for document/header/tabular reference tests."""

    def describe_entity_set(self, name):
        if name == "Document_ReferenceCheck":
            return {
                "name": name,
                "entity_type": "StandardODATA.Document_ReferenceCheck",
                "properties": [
                    {"name": "Ref_Key", "type": "Edm.Guid"},
                    {"name": "Number", "type": "Edm.String"},
                    {"name": "ДоговорКонтрагента_Key", "type": "Edm.Guid"},
                ],
                "navigation_properties": [{
                    "name": "ДоговорКонтрагента",
                    "type": "StandardODATA.Catalog_ReferenceContracts",
                    "target_type": "StandardODATA.Catalog_ReferenceContracts",
                    "target_entity_set": "Catalog_ReferenceContracts",
                }],
                "tabularSections": ["РасшифровкаПлатежа"],
                "isTabularRowEntity": False,
                "entity_type_name": "Document_ReferenceCheck",
            }
        if name == "Document_ReferenceCheck_РасшифровкаПлатежа":
            return {
                "name": name,
                "entity_type": "StandardODATA.Document_ReferenceCheck_РасшифровкаПлатежа",
                "properties": [
                    {"name": "Ref_Key", "type": "Edm.Guid"},
                    {"name": "LineNumber", "type": "Edm.Int64"},
                    {"name": "ДоговорКонтрагента_Key", "type": "Edm.Guid"},
                ],
                "navigation_properties": [{
                    "name": "ДоговорКонтрагента",
                    "type": "StandardODATA.Catalog_ReferenceContracts",
                    "target_type": "StandardODATA.Catalog_ReferenceContracts",
                    "target_entity_set": "Catalog_ReferenceContracts",
                }],
                "tabularSections": [],
                "isTabularRowEntity": True,
                "parentDocument": "Document_ReferenceCheck",
                "tabularSection": "РасшифровкаПлатежа",
                "entity_type_name": "Document_ReferenceCheck_РасшифровкаПлатежа_RowType",
            }
        if name == "Catalog_ReferenceContracts":
            return {
                "name": name,
                "entity_type": "StandardODATA.Catalog_ReferenceContracts",
                "properties": [{"name": "Ref_Key", "type": "Edm.Guid"}],
                "navigation_properties": [],
                "tabularSections": [],
                "isTabularRowEntity": False,
                "entity_type_name": "Catalog_ReferenceContracts",
            }
        return super().describe_entity_set(name)

    def validate_payload(self, name, payload):
        if name == "Document_ReferenceCheck":
            allowed = {
                "Ref_Key", "Number", "ДоговорКонтрагента_Key",
                "РасшифровкаПлатежа",
            }
            if set(payload) - allowed:
                raise ValueError("unknown field")
            return
        if name == "Document_ReferenceCheck_РасшифровкаПлатежа":
            if set(payload) - {"Ref_Key", "LineNumber", "ДоговорКонтрагента_Key"}:
                raise ValueError("unknown field")
            return
        super().validate_payload(name, payload)


class FakeClient:
    def __init__(self, write_enabled=True, **config_attrs):
        attrs = {"odata_base_url": "https://example.test/test/odata/standard.odata/"}
        attrs.update(config_attrs)
        self.config = type("Config", (), attrs)()
        self.write_enabled = write_enabled
        self.calls = []
        self.data_version = "v1"
        self.current_lines = [{"LineNumber": 1, "Item_Key": "item-1",
                               "Quantity": "1.000"}]
        self.datasets = {
            "Document_Example": [
                {"Ref_Key": "00000000-0000-0000-0000-000000000010",
                 "Date": "2010-03-22T12:01:00", "Number": "1"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000011",
                 "Date": "2010-04-07T09:00:00", "Number": "2"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000012",
                 "Date": "2010-04-07T18:00:00", "Number": "3"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000013",
                 "Date": "2010-04-08T09:00:00", "Number": "4"},
            ],
            "Catalog_Example": [
                {"Ref_Key": "00000000-0000-0000-0000-000000000020",
                 "ИНН": ""},
                {"Ref_Key": "00000000-0000-0000-0000-000000000021",
                 "ИНН": "100"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000022",
                 "ИНН": "200"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000023",
                 "ИНН": "200"},
                {"Ref_Key": "00000000-0000-0000-0000-000000000024",
                 "ИНН": "300"},
            ],
        }

    def create_entity(self, entity_set, payload, null_ref_key=False):
        self.calls.append(("create", entity_set, payload, null_ref_key))
        return {"Ref_Key": "00000000-0000-0000-0000-000000000001"}

    def list_entities(self, entity_set, top, skip, filter=None, select=None,
                      orderby=None, expand=None):
        self.calls.append(("list", entity_set, top, skip, filter, select, orderby, expand))
        if filter and ("Date" in filter or "ИНН" in filter):
            raise ODataHTTPError(
                500,
                code="-1",
                message='Операция не разрешена в предложении "ГДЕ"',
                definitive=False,
            )
        if orderby and entity_set in self.datasets:
            field = orderby.split()[0]
            rows = sorted(self.datasets[entity_set], key=lambda item: item.get(field) or "")
            rows = rows[skip:skip + top]
            if select and select != "*":
                fields = [field.strip() for field in select.split(",") if field.strip()]
                rows = [{field: row[field] for field in fields if field in row} for row in rows]
            return {"value": rows}
        return {"value": [{"Ref_Key": "00000000-0000-0000-0000-000000000001"}]}

    def count_entities(self, entity_set):
        self.calls.append(("count", entity_set))
        if entity_set in self.datasets:
            return len(self.datasets[entity_set])
        return 1

    def get_entity(self, entity_set, key):
        self.calls.append(("get", entity_set, key))
        return {"Ref_Key": key, "Number": "1", "DataVersion": self.data_version,
                "Lines": [dict(row) for row in self.current_lines]}

    def get_register_record_set(self, entity_set, recorder, recorder_type):
        self.calls.append(("get-register", entity_set, recorder, recorder_type))
        if entity_set == "AccountingRegister_Example":
            return {
                "Recorder": recorder,
                "Recorder_Type": recorder_type,
                "RecordSet": [
                    {
                        "Active": True,
                        "AccountDr_Key": "10000000-0000-0000-0000-000000000051",
                        "AccountCr_Key": "10000000-0000-0000-0000-000000000911",
                        "Сумма": 90.0,
                    }
                ],
            }
        raise ODataHTTPError(404, code="9", message="register record set not found", definitive=True)

    def patch_entity(self, entity_set, key, payload, if_match=None):
        self.calls.append(("patch", entity_set, key, payload, if_match))

    def invoke_action(self, entity_set, key, action, if_match=None):
        self.calls.append(("action", entity_set, key, action, if_match))


def make_bridge(tmp_path: Path, write_enabled=True, **config_attrs):
    client = FakeClient(write_enabled, **config_attrs)
    bridge = Bridge(client, WriteLedger(tmp_path / "ledger.sqlite3"), FakeMetadata())
    return bridge, client


def make_reference_bridge(tmp_path: Path):
    client = FakeClient()
    bridge = Bridge(
        client,
        WriteLedger(tmp_path / "reference-ledger.sqlite3"),
        FakeReferenceMetadata(),
    )
    return bridge, client


def test_document_queue_telemetry_tracks_real_workers_and_uncertain_resume(
    tmp_path: Path, monkeypatch,
):
    log_path = tmp_path / "onec83-performance.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    company_name = "PRIVATE-CANARY-Company-North"
    document_date = "2010-01-02T03:04:05"
    amount = "12345.67"
    operation_id = "PRIVATE-CANARY-operation-GUID-20100102"
    key = "c0ffee00-0000-0000-0000-000000000001"
    response_error = "PRIVATE-CANARY-response-error"
    definite_value = "synthetic-definite-refusal"
    uncertain_value = "synthetic-uncertain-row"

    class MockResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.text = json.dumps(payload)
            self.content = self.text.encode("utf-8")
            self.headers = {}

        def json(self):
            return json.loads(self.text)

    class MockTransport:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.peak_active = 0
            self.create_requests: dict[str, int] = {}
            self.documents: dict[str, dict] = {}

        def request(self, method, url, **kwargs):
            with self.lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            try:
                time.sleep(0.01)
                if method == "POST":
                    payload = kwargs["json"]
                    number = payload["Number"]
                    with self.lock:
                        self.create_requests[number] = self.create_requests.get(number, 0) + 1
                    if number == definite_value:
                        return MockResponse(400, {
                            "error": {
                                "code": "validation",
                                "message": {"value": response_error},
                            },
                        })
                    if number == uncertain_value:
                        raise Timeout("synthetic transport timeout")
                    with self.lock:
                        self.documents[key] = {
                            "Ref_Key": key,
                            "Number": number,
                            "Date": payload["Date"],
                            "Amount": payload["Amount"],
                            "DataVersion": "synthetic-version",
                        }
                    return MockResponse(201, {"d": {"Ref_Key": key}})
                if method == "GET":
                    requested_key = url.split("guid'", 1)[1].split("'", 1)[0]
                    with self.lock:
                        document = dict(self.documents[requested_key])
                    return MockResponse(200, {"d": document})
                raise AssertionError(f"unexpected HTTP method: {method}")
            finally:
                with self.lock:
                    self.active -= 1

    class QueueTelemetryMetadata(FakeMetadata):
        def describe_entity_set(self, name):
            description = super().describe_entity_set(name)
            if name == "Document_Example":
                description["properties"].append({"name": "Amount", "type": "Edm.Decimal"})
            return description

        def validate_payload(self, name, payload):
            if name == "Document_Example":
                if set(payload) - {"Ref_Key", "Number", "Date", "Amount", "DeletionMark", "Lines"}:
                    raise ValueError("unknown field")
                return
            super().validate_payload(name, payload)

    transport = MockTransport()
    base_url = "https://example.test/base/odata/standard.odata/"
    config = RuntimeConfig(
        odata_base_url=base_url,
        odata_user="synthetic-user",
        keychain_service="synthetic-test",
        write_base_url=base_url,
        enable_write=True,
        target_kind="test",
        get_retries=0,
    )
    client = ODataClient(
        config,
        password_provider=lambda _service, _user: "synthetic-password",
        transport=transport,
    )
    bridge = Bridge(client, WriteLedger(tmp_path / "ledger.sqlite3"), QueueTelemetryMetadata())
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)
    monkeypatch.setattr(server_module, "PERFORMANCE_LOGGER", logger)

    items = [
        {"entity_set": "Document_Example",
         "payload": {"Number": company_name, "Date": document_date, "Amount": amount},
         "operation_id": operation_id},
        {"entity_set": "Document_Example",
         "payload": {"Number": definite_value, "Date": document_date, "Amount": amount},
         "operation_id": "synthetic-definite-operation"},
        {"entity_set": "Document_Example",
         "payload": {"Number": uncertain_value, "Date": document_date, "Amount": amount},
         "operation_id": "synthetic-uncertain-operation"},
    ]
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
        "max_concurrency": 3,
    }

    first = server_module.onec83_apply_document_queue(items, **args)

    assert first["succeeded"] == 1
    assert first["failed"] == 1
    assert first["needsReconciliation"] == 1
    assert transport.peak_active >= 2
    assert transport.create_requests == {
        company_name: 1,
        definite_value: 1,
        uncertain_value: 1,
    }
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    first_rows = [event for event in events if event["event"] == "queue_row"]
    assert len(first_rows) == 3
    assert len({event["row_token"] for event in first_rows}) == 3
    assert all(len(event["row_token"]) == 32 for event in first_rows)
    assert sorted(
        (event["outcome"], event["odata_calls"], event["odata_attempts"])
        for event in first_rows
    ) == sorted([
        ("success", 2, 2),
        ("failed", 1, 1),
        ("needs_reconciliation", 1, 1),
    ])
    assert any(event["event"] == "stage" and event["stage"] == "create"
               and "row_token" in event for event in events)

    second = server_module.onec83_apply_document_queue([items[2]], **args)

    assert second["needsReconciliation"] == 1
    assert transport.create_requests[uncertain_value] == 1
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    resumed_rows = [event for event in events if event["event"] == "queue_row"]
    assert len(resumed_rows) == 4
    assert resumed_rows[-1]["outcome"] == "needs_reconciliation"
    assert resumed_rows[-1]["odata_calls"] == 0
    assert resumed_rows[-1]["odata_attempts"] == 0

    summary = summarize(log_path)
    assert summary["queue"]["rows_submitted"] == 4
    assert summary["queue"]["row_outcomes"]["success"] == 1
    assert summary["queue"]["row_outcomes"]["failed"] == 1
    assert summary["queue"]["row_outcomes"]["needs_reconciliation"] == 2
    assert summary["tools"]["onec83_apply_document_queue"]["calls"] == 2
    assert summary["odata_attempts"]["count"] == 4
    assert summary["stages"]["create"]["elapsed_ms"]["count"] == 3
    assert summary["stages"]["readback"]["elapsed_ms"]["count"] == 1
    serialized_log = log_path.read_text(encoding="utf-8")
    serialized_summary = json.dumps(summary, ensure_ascii=False)
    for canary in (company_name, document_date, amount, operation_id, key, response_error):
        assert canary not in serialized_log
        assert canary not in serialized_summary


def test_dry_run_does_not_write_and_rejects_unknown_schema(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    result = bridge.create_entity("Document_Example", {"Number": "1"})
    assert result["dryRun"] is True
    assert "payload" not in result["plan"]
    assert result["plan"]["payloadSummary"]["fields"] == ["Number", "Ref_Key"]
    assert result["plan"]["payloadSummary"]["shape"]["Number"] == {"type": "string"}
    assert result["plan"]["payloadSummary"]["shape"]["Ref_Key"] == {"type": "string"}
    assert client.calls == []
    with pytest.raises(ValueError, match="not published.*Surprise"):
        bridge.create_entity("Document_Example", {"Surprise": "x"})
    with pytest.raises(ValueError, match="nonempty"):
        bridge.create_entity("Document_Example", {})
    with pytest.raises(ValueError, match="not published"):
        bridge.create_entity("Document_Absent", {"Number": "1"})


def test_status_describe_and_document_list_report_generic_coverage(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    status = bridge.status()
    assert "onec83_update_entity" in status["toolCoverage"]["write"]
    assert "onec83_post_document" in status["toolCoverage"]["write"]
    assert "onec83_apply_document_queue" in status["toolCoverage"]["write"]
    assert status["ready"] is True
    assert status["readReadiness"]["ready"] is True
    assert status["writeReadiness"]["ready"] is True
    assert status["writeReadiness"]["checks"] == {
        "enableWriteEnvTrue": True,
        "targetKindIsTest": True,
        "writeBaseUrlConfigured": True,
        "writeBaseMatchesReadBase": True,
        "clientWriteGuardEnabled": True,
        "objectReadsReady": True,
    }
    assert status["writeReadiness"]["requiredEnvNames"] == [
        "ONEC83_ENABLE_WRITE",
        "ONEC83_TARGET_KIND",
        "ONEC83_WRITE_BASE_URL",
    ]

    documents = bridge.list_document_types()
    assert documents["entitySets"] == [
        {"name": "Document_Example"},
        {"name": "Document_Nonpostable"},
    ]

    description = bridge.describe_entity_set("Document_Example")
    assert description["mcpCapabilities"]["canCreate"] is None
    assert description["mcpCapabilities"]["canUpdate"] is None
    assert description["mcpCapabilities"]["canPost"] is None
    assert description["mcpCapabilities"]["canSetDeletionMark"] is None
    assert description["mcpCapabilities"]["toolAvailable"]["create"] is True
    assert description["mcpCapabilities"]["publishedActions"] == {
        "Post": True,
        "Unpost": True,
    }
    assert description["mcpCapabilities"]["rightsKnown"] is False

    row_description = bridge.describe_entity_set("Document_Example_Lines")
    assert row_description["mcpCapabilities"]["canCreate"] is False
    assert row_description["mcpCapabilities"]["canUpdate"] is False
    assert row_description["mcpCapabilities"]["canPost"] is False
    assert row_description["mcpCapabilities"]["parentDocument"] == "Document_Example"
    assert row_description["mcpCapabilities"]["tabularSection"] == "Lines"

    nonpostable = bridge.describe_entity_set("Document_Nonpostable")
    assert nonpostable["mcpCapabilities"]["canCreate"] is None
    assert nonpostable["mcpCapabilities"]["toolAvailable"]["create"] is True
    assert nonpostable["mcpCapabilities"]["canPost"] is False
    assert nonpostable["mcpCapabilities"]["toolAvailable"]["post"] is False

    bad_marker_row = bridge.describe_entity_set("Document_BadMarker_Rows")
    assert bad_marker_row["mcpCapabilities"]["isTabularRowEntity"] is True
    assert bad_marker_row["mcpCapabilities"]["canCreate"] is False

    counterparty = bridge.describe_entity_set("Catalog_Контрагенты")
    assert counterparty["mcpCapabilities"]["businessRules"]["counterpartyClassification"] == {
        "field": "ЮридическоеФизическоеЛицо",
        "observedValues": ["ФизическоеЛицо", "ЮридическоеЛицо"],
        "observedValuesEvidence": "read-only sample on 2026-09-28",
        "enforcement": "advisory_only",
        "individualEntrepreneurStatus": "unknown",
        "migrationWritePolicy": "hold_until_verified",
    }


def test_status_write_readiness_explains_disabled_config_without_urls(tmp_path: Path):
    bridge, _client = make_bridge(
        tmp_path,
        write_enabled=False,
        enable_write=False,
        target_kind=None,
        write_base_url=None,
    )

    readiness = bridge.status()["writeReadiness"]

    assert readiness["ready"] is False
    assert readiness["checks"] == {
        "enableWriteEnvTrue": False,
        "targetKindIsTest": False,
        "writeBaseUrlConfigured": False,
        "writeBaseMatchesReadBase": False,
        "clientWriteGuardEnabled": False,
        "objectReadsReady": True,
    }
    assert "ONEC83_ENABLE_WRITE is not true" in readiness["reason"]
    assert "https://example.test" not in repr(readiness)


def test_status_write_readiness_reports_exact_gate_mismatch_without_urls(tmp_path: Path):
    bridge, _client = make_bridge(
        tmp_path,
        write_enabled=False,
        enable_write=True,
        target_kind="test",
        write_base_url="https://other.test/test/odata/standard.odata/",
    )

    readiness = bridge.status()["writeReadiness"]

    assert readiness["ready"] is False
    assert readiness["checks"]["enableWriteEnvTrue"] is True
    assert readiness["checks"]["targetKindIsTest"] is True
    assert readiness["checks"]["writeBaseUrlConfigured"] is True
    assert readiness["checks"]["writeBaseMatchesReadBase"] is False
    assert "does not match" in readiness["reason"]
    assert "https://example.test" not in repr(readiness)
    assert "https://other.test" not in repr(readiness)


def test_status_reports_session_block_and_disables_write_readiness(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def blocked_read(*args, **kwargs):
        client.calls.append(("blocked-read", args, kwargs))
        raise ODataHTTPError(
            403,
            message=(
                "Начало сеанса с информационной базой запрещено. "
                "В данный момент база занята для обновления или архивации"
            ),
            definitive=True,
        )

    client.list_entities = blocked_read

    status = bridge.status()

    assert status["connected"] is True
    assert status["metadataReady"] is True
    assert status["ready"] is False
    assert status["readReadiness"]["ready"] is False
    assert status["readReadiness"]["blockerType"] == "sessionBlocked"
    assert status["readReadiness"]["error"]["httpStatus"] == 403
    assert "Начало сеанса" in status["readReadiness"]["error"]["message"]
    assert status["writeReadiness"]["ready"] is False
    assert status["writeReadiness"]["checks"]["objectReadsReady"] is False
    assert "OData object reads are not ready" in status["writeReadiness"]["reason"]
    assert "https://example.test" not in repr(status)


def test_resolve_enum_value_returns_composite_payload_fragment(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.resolve_enum_value(
        "EnumRef.ВидыПлатежейВГосБюджет",
        "Налог",
        "СубконтоДт1",
    )

    assert result["odataType"] == "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет"
    assert result["payloadFragment"] == {
        "СубконтоДт1": "Налог",
        "СубконтоДт1_Type": "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
    }
    assert "onec83_resolve_enum_value" in bridge.status()["toolCoverage"]["read"]


def test_observed_enum_context_requires_test_target_and_reaches_metadata(tmp_path: Path):
    token = "bank_tax_payment_subkonto1_same_dimension"
    test_client = FakeClient(False, target_kind="test")
    test_bridge = Bridge(
        test_client,
        WriteLedger(tmp_path / "test-ledger.sqlite3"),
        FakeMetadata(),
        target_name="test",
    )
    resolved = test_bridge.resolve_enum_value(
        "ВидыПлатежейВГосБюджет",
        "Налог",
        "СубконтоДт1",
        account_context=token,
    )
    assert resolved["odataType"] == "StandardODATA.ВидыПлатежейВГосБюджет"
    assert resolved["accountContext"] == token

    prod_client = FakeClient(False, target_kind="prod")
    prod_bridge = Bridge(
        prod_client,
        WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        FakeMetadata(),
        target_name="prod",
    )
    with pytest.raises(ValueError, match="only for a TEST target"):
        prod_bridge.resolve_enum_value(
            "ВидыПлатежейВГосБюджет",
            "Налог",
            "СубконтоДт1",
            account_context=token,
        )
    with pytest.raises(ValueError, match="only for a TEST target"):
        prod_bridge._validate_and_normalize_payload(
            "Catalog_Example",
            {"ИНН": {"accountContext": token}},
        )

    unknown_client = FakeClient(False)
    unknown_bridge = Bridge(
        unknown_client,
        WriteLedger(tmp_path / "unknown-target-ledger.sqlite3"),
        FakeMetadata(),
    )
    with pytest.raises(ValueError, match="only for a TEST target"):
        unknown_bridge.resolve_enum_value(
            "ВидыПлатежейВГосБюджет",
            "Налог",
            "СубконтоДт1",
            account_context=token,
        )
    with pytest.raises(ValueError, match="only for a TEST target"):
        unknown_bridge._validate_and_normalize_payload(
            "Catalog_Example",
            {"ИНН": {"accountContext": token}},
        )


def test_create_dry_run_surfaces_unknown_enum_property_compatibility(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    bridge._metadata.enum_property_compatibility = lambda entity_set, payload: [{
        "path": f"{entity_set}.Lines[0].СубконтоДт1",
        "propertyCompatibility": "unknown",
        "reason": "synthetic CSDL does not provide a field-specific allowed type set",
    }]

    result = bridge.create_entity(
        "Document_Example",
        {
            "Lines": [{
                "LineNumber": 1,
                "СубконтоДт1": {
                    "onec83ValueType": "EnumRef",
                    "enum": "ВидыПлатежейВГосБюджет",
                    "member": "Налог",
                },
            }],
        },
    )

    assert result["dryRun"] is True
    assert result["plan"]["propertyCompatibility"][0]["propertyCompatibility"] == "unknown"
    assert client.calls == []


def test_write_tool_docstrings_show_apply_parameters():
    write_tools = [
        server_module.onec83_create_entity,
        server_module.onec83_patch_entity,
        server_module.onec83_update_entity,
        server_module.onec83_replace_tabular_section,
        server_module.onec83_set_deletion_mark,
        server_module.onec83_document_action,
        server_module.onec83_post_document,
        server_module.onec83_unpost_document,
    ]
    for tool in write_tools:
        doc = tool.__doc__ or ""
        assert "dry_run=false" in doc
        assert "confirm=true" in doc
        assert "expected_target" in doc
        assert "operation_id" in doc

    versioned_tools = [
        server_module.onec83_patch_entity,
        server_module.onec83_update_entity,
        server_module.onec83_replace_tabular_section,
        server_module.onec83_set_deletion_mark,
        server_module.onec83_document_action,
        server_module.onec83_post_document,
        server_module.onec83_unpost_document,
    ]
    for tool in versioned_tools:
        assert "expected_data_version" in (tool.__doc__ or "")


def test_query_entities_is_bounded_and_has_list_alias(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    result = bridge.list_entities(
        "Document_Example",
        top=1,
        skip=0,
        filter="DeletionMark eq false",
        select="Ref_Key,Number",
    )
    assert result["result"]["value"][0]["Ref_Key"].endswith("1")
    assert client.calls == [
        ("list", "Document_Example", 1, 0, "DeletionMark eq false",
         "Ref_Key,Number", None, None)
    ]

    with pytest.raises(ValueError, match="top"):
        bridge.query_entities("Document_Example", top=0)
    with pytest.raises(ValueError, match="filter"):
        bridge.query_entities("Document_Example", filter="bad\nfilter")


def test_accounting_register_read_uses_named_recorder_key(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    recorder_key = "00000000-0000-0000-0000-000000000001"

    result = bridge.get_accounting_register_record_set(
        "AccountingRegister_Example",
        recorder_key,
        recorder_entity_set="Document_Example",
    )

    assert result["strategy"] == "recorder_named_key"
    assert result["recorderType"] == "StandardODATA.Document_Example"
    assert result["complete"] is True
    assert result["count"] == 1
    assert result["result"]["value"][0]["Сумма"] == 90.0
    assert client.calls == [(
        "get-register",
        "AccountingRegister_Example",
        recorder_key,
        "StandardODATA.Document_Example",
    )]


def test_accounting_register_read_requires_one_recorder_type_source(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    recorder_key = "00000000-0000-0000-0000-000000000001"

    with pytest.raises(ValueError, match="exactly one"):
        bridge.get_accounting_register_record_set(
            "AccountingRegister_Example", recorder_key,
        )
    with pytest.raises(ValueError, match="exactly one"):
        bridge.get_accounting_register_record_set(
            "AccountingRegister_Example",
            recorder_key,
            recorder_entity_set="Document_Example",
            recorder_type="StandardODATA.Document_Example",
        )
    assert client.calls == []


def test_accounting_register_recorder_search_resumes_after_timeout(tmp_path: Path):
    """AccountingRegister Recorder is Edm.String per platform appendix 12.11."""
    class RegisterMetadata(FakeMetadata):
        def describe_entity_set(self, name):
            if name == "AccountingRegister_Example":
                return {
                    "name": name,
                    "properties": [
                        {"name": "Recorder", "type": "Edm.String"},
                        {"name": "Recorder_Type", "type": "Edm.String"},
                        {"name": "RecordSet"},
                    ],
                    "tabularSections": [],
                    "isTabularRowEntity": False,
                    "entity_type_name": name,
                }
            return super().describe_entity_set(name)

    bridge, client = make_bridge(tmp_path)
    bridge._metadata = RegisterMetadata()
    recorder = "00000000-0000-0000-0000-000000000001"
    other = "00000000-0000-0000-0000-000000000002"
    recorder_type = "StandardODATA.Document_Example"
    client.datasets["AccountingRegister_Example"] = [
        {"Recorder": "00000000-0000-0000-0000-000000000000",
         "Recorder_Type": recorder_type, "LineNumber": "0"},
        {"Recorder": recorder, "Recorder_Type": recorder_type,
         "LineNumber": "1"},
        {"Recorder": recorder, "Recorder_Type": recorder_type,
         "LineNumber": "2"},
        {"Recorder": other, "Recorder_Type": recorder_type,
         "LineNumber": "3"},
    ]
    original_list_entities = client.list_entities
    failed_once = False

    def reject_where_then_timeout_page(entity_set, top, skip, filter=None,
                                       select=None, orderby=None, expand=None):
        nonlocal failed_once
        if filter:
            assert filter == f"Recorder eq '{recorder}'"
            client.calls.append((
                "list", entity_set, top, skip, filter, select, orderby, expand,
            ))
            raise ODataHTTPError(
                500, code="-1",
                message='Операция не разрешена в предложении "ГДЕ"',
                definitive=False,
            )
        if skip == 2 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=3)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = reject_where_then_timeout_page
    arguments = {
        "entity_set": "AccountingRegister_Example",
        "field": "Recorder",
        "equals": recorder,
        "select": "Recorder,Recorder_Type,LineNumber",
        "top": 10,
        "page_size": 1,
        "max_scan": 10,
    }

    partial = bridge.search_entities(**arguments)

    assert partial["ok"] is False
    assert partial["error"]["kind"] == "odataTransport"
    assert partial["error"]["category"] == "timeout"
    assert partial["error"]["attempts"] == 3
    assert partial["strategy"] == "linear_scan_after_filter_error"
    assert partial["complete"] is False
    assert partial["partialResult"] is True
    assert partial["scannedRows"] == 2
    assert partial["resumeSkip"] == 2
    assert partial["result"]["value"] == [{
        "Recorder": recorder,
        "Recorder_Type": recorder_type,
        "LineNumber": "1",
    }]

    resumed = bridge.search_entities(
        **arguments, resume_skip=partial["resumeSkip"],
    )

    assert resumed["filterSkippedForResume"] is True
    assert resumed["resumedFromSkip"] == 2
    assert resumed["complete"] is True
    assert resumed["result"]["value"] == [{
        "Recorder": recorder,
        "Recorder_Type": recorder_type,
        "LineNumber": "2",
    }]
    merged = partial["result"]["value"] + resumed["result"]["value"]
    assert [row["LineNumber"] for row in merged] == ["1", "2"]
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1


def test_accounting_register_named_reads_are_bounded_per_recorder(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    recorder_type = "StandardODATA.Document_Example"
    recorder_keys = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]

    results = [
        bridge.get_accounting_register_record_set(
            "AccountingRegister_Example", key,
            recorder_entity_set="Document_Example",
        )
        for key in recorder_keys
    ]

    assert [result["complete"] for result in results] == [True, True]
    assert [call for call in client.calls if call[0] == "get-register"] == [
        ("get-register", "AccountingRegister_Example", key, recorder_type)
        for key in recorder_keys
    ]
    assert not [call for call in client.calls if call[0] in {"list", "count", "create", "patch", "action"}]


def test_accounting_register_direct_reader_timeout_is_structured_and_incomplete(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_bridge(tmp_path)

    def timeout_register(*_args):
        raise ODataTransportError("timeout", attempts=3)

    client.get_register_record_set = timeout_register
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)

    result = server_module.onec83_get_accounting_register_record_set(
        "AccountingRegister_Example",
        "00000000-0000-0000-0000-000000000001",
        recorder_entity_set="Document_Example",
    )

    assert result == {
        "ok": False,
        "error": {
            "kind": "odataTransport",
            "message": "OData transport timeout after 3 attempts",
            "category": "timeout",
            "attempts": 3,
        },
    }
    assert "complete" not in result
    assert client.calls == []


def test_search_entities_falls_back_to_ordered_date_scan(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Document_Example",
        field="Date",
        from_value="2010-04-07T00:00:00",
        to_exclusive="2010-04-08T00:00:00",
        select="Ref_Key,Date",
        top=10,
        page_size=2,
        max_scan=10,
    )

    assert result["strategy"] == "ordered_scan_after_filter_error"
    assert result["serverFilterError"]["httpStatus"] == 500
    assert result["complete"] is True
    assert result["maxScanReached"] is False
    assert result["scannedRows"] == 3
    assert result["startSkip"] == 1
    assert [row["Ref_Key"] for row in result["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    ]
    assert client.calls[0][4].startswith("Date ge datetime")
    assert any(call[0] == "count" for call in client.calls)


def test_search_profile_captures_server_filter_without_fallback_reads(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)
    log_path = tmp_path / "search-profile.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)

    with instrument_tool_call("onec83_search_entities", logger) as call:
        result = bridge.search_entities(
            "Document_Example", field="Number", equals="2",
            select="Ref_Key,Number", top=5,
        )
        call.observe_result(result)

    assert result["strategy"] == "server_filter"
    call = next(json.loads(line) for line in log_path.read_text().splitlines()
                if json.loads(line)["event"] == "mcp_call")
    assert call["search_class"] == "string_equality"
    assert call["search_strategy"] == "server_filter"
    assert call["fallback_page_reads"] == 0
    assert call["date_lower_bound_probes"] == 0


def test_search_profile_marks_transient_server_filter_failure_without_fallback(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    log_path = tmp_path / "search-profile.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    original_list_entities = client.list_entities

    def transient_filter_error(entity_set, top, skip, filter=None, select=None,
                               orderby=None, expand=None):
        if filter:
            record_request_call()
            record_odata_attempt(
                stage=None, elapsed_ms=8, attempt=1, status_code=503,
            )
            raise ODataHTTPError(
                503, code="temporary", message="temporary service failure",
                definitive=False,
            )
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = transient_filter_error
    with pytest.raises(ODataHTTPError):
        with instrument_tool_call("onec83_search_entities", logger):
            bridge.search_entities(
                "Document_Example", field="Number", equals="2",
                select="Ref_Key,Number", top=5,
            )

    event = next(json.loads(line) for line in log_path.read_text().splitlines()
                 if json.loads(line)["event"] == "mcp_call")
    assert event["search_class"] == "string_equality"
    assert event["search_strategy"] == "server_filter_error"
    assert event["fallback_page_reads"] == 0
    assert event["date_lower_bound_probes"] == 0
    assert event["odata_calls"] == 1
    assert event["odata_attempts"] == 1


def test_search_profile_counts_string_fallback_collection_reads(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)
    log_path = tmp_path / "search-profile.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)

    with instrument_tool_call("onec83_search_entities", logger) as call:
        result = bridge.search_entities(
            "Catalog_Example", field="ИНН", equals="200",
            select="Ref_Key,ИНН", top=3, page_size=2, max_scan=10,
        )
        call.observe_result(result)

    assert result["strategy"] == "linear_scan_after_filter_error"
    event = next(json.loads(line) for line in log_path.read_text().splitlines()
                 if json.loads(line)["event"] == "mcp_call")
    assert event["search_class"] == "string_equality"
    assert event["search_strategy"] == "fallback_scan"
    assert event["fallback_page_reads"] == 3
    assert event["date_lower_bound_probes"] == 0
    assert "ИНН" not in log_path.read_text(encoding="utf-8")


def test_search_profile_counts_date_probes_and_resume_reads_including_timeout(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    log_path = tmp_path / "search-profile.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    original_list_entities = client.list_entities
    failed_once = False

    def timeout_one_page(entity_set, top, skip, filter=None, select=None,
                         orderby=None, expand=None):
        nonlocal failed_once
        if not filter and select == "Ref_Key,Date" and skip == 2 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=3)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = timeout_one_page
    arguments = {
        "entity_set": "Document_Example", "field": "Date",
        "from_value": "2010-04-07T00:00:00",
        "to_exclusive": "2010-04-08T00:00:00",
        "select": "Ref_Key,Date", "top": 10, "page_size": 1, "max_scan": 10,
    }
    with instrument_tool_call("onec83_search_entities", logger) as call:
        partial = bridge.search_entities(**arguments)
        call.observe_result(partial)
    with instrument_tool_call("onec83_search_entities", logger) as call:
        resumed = bridge.search_entities(**arguments, resume_skip=partial["resumeSkip"])
        call.observe_result(resumed)

    assert partial["scanInterrupted"] is True
    assert resumed["complete"] is True
    calls = [json.loads(line) for line in log_path.read_text().splitlines()
             if json.loads(line)["event"] == "mcp_call"]
    assert [(call["search_class"], call["search_strategy"]) for call in calls] == [
        ("date_range", "fallback_scan"),
        ("date_range", "fallback_resume"),
    ]
    assert calls[0]["date_lower_bound_probes"] > 0
    assert calls[0]["fallback_page_reads"] == 2  # Includes failed logical page read.
    assert calls[1]["date_lower_bound_probes"] == 0
    assert calls[1]["fallback_page_reads"] == 2
    summary = summarize(log_path)["search_profiles"]
    assert summary["date_range.fallback_scan"]["calls"] == 1
    assert summary["date_range.fallback_resume"]["calls"] == 1


def test_search_entities_returns_partial_date_rows_and_resumes_after_timeout(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)
    bridge._ordered_lower_bound = lambda *_args: 1
    original_list_entities = client.list_entities
    failed_once = False

    def timeout_one_page(entity_set, top, skip, filter=None, select=None,
                         orderby=None, expand=None):
        nonlocal failed_once
        if not filter and skip == 2 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=3)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = timeout_one_page
    arguments = {
        "entity_set": "Document_Example",
        "field": "Date",
        "from_value": "2010-04-07T00:00:00",
        "to_exclusive": "2010-04-08T00:00:00",
        "select": "Ref_Key,Date",
        "top": 10,
        "page_size": 1,
        "max_scan": 10,
    }

    partial = bridge.search_entities(**arguments)

    assert partial["ok"] is False
    assert partial["error"]["kind"] == "odataTransport"
    assert partial["error"]["category"] == "timeout"
    assert partial["error"]["attempts"] == 3
    assert partial["complete"] is False
    assert partial["scanInterrupted"] is True
    assert partial["partialResult"] is True
    assert partial["scannedRows"] == 1
    assert partial["resumeSkip"] == 2
    assert [row["Ref_Key"] for row in partial["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000011",
    ]

    resumed = bridge.search_entities(**arguments, resume_skip=partial["resumeSkip"])

    assert resumed["filterSkippedForResume"] is True
    assert resumed["resumedFromSkip"] == 2
    assert resumed["complete"] is True
    assert [row["Ref_Key"] for row in resumed["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000012",
    ]
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1


def test_search_entities_returns_restart_cursor_if_date_start_lookup_times_out(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)

    def timeout_lower_bound(*_args):
        raise ODataTransportError("timeout", attempts=3)

    bridge._ordered_lower_bound = timeout_lower_bound
    arguments = {
        "entity_set": "Document_Example",
        "field": "Date",
        "from_value": "2010-04-07T00:00:00",
        "to_exclusive": "2010-04-08T00:00:00",
        "select": "Ref_Key,Date",
        "top": 10,
        "page_size": 2,
        "max_scan": 10,
    }

    partial = bridge.search_entities(**arguments)

    assert partial["complete"] is False
    assert partial["partialResult"] is False
    assert partial["scanProgress"]["phase"] == "start_position"
    assert partial["resumeSkip"] == 0

    resumed = bridge.search_entities(**arguments, resume_skip=partial["resumeSkip"])

    assert resumed["complete"] is True
    assert resumed["filterSkippedForResume"] is True
    assert [row["Ref_Key"] for row in resumed["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    ]
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1


def test_search_entities_returns_incomplete_empty_result_and_resumes_string_scan(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)
    original_list_entities = client.list_entities
    failed_once = False

    def timeout_one_page(entity_set, top, skip, filter=None, select=None,
                         orderby=None, expand=None):
        nonlocal failed_once
        if not filter and skip == 2 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=3)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = timeout_one_page
    arguments = {
        "entity_set": "Catalog_Example",
        "field": "ИНН",
        "equals": "200",
        "select": "Ref_Key,ИНН",
        "top": 3,
        "page_size": 1,
        "max_scan": 10,
    }

    partial = bridge.search_entities(**arguments)

    assert partial["ok"] is False
    assert partial["partialResult"] is False
    assert partial["result"]["value"] == []
    assert partial["complete"] is False
    assert partial["scannedRows"] == 2
    assert partial["resumeSkip"] == 2

    resumed = bridge.search_entities(**arguments, resume_skip=partial["resumeSkip"])

    assert resumed["complete"] is True
    assert [row["Ref_Key"] for row in resumed["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000022",
        "00000000-0000-0000-0000-000000000023",
    ]
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1


def test_search_entities_caches_exact_where_rejection_until_refresh_or_target_change(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_bridge(tmp_path)
    metadata = bridge._metadata
    log_path = tmp_path / "onec83-performance.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    arguments = {
        "entity_set": "Document_Example",
        "field": "Date",
        "from_value": "2010-04-07T00:00:00",
        "to_exclusive": "2010-04-08T00:00:00",
        "select": "Ref_Key,Date",
        "top": 10,
        "page_size": 2,
        "max_scan": 10,
    }

    with instrument_tool_call("onec83_search_entities", logger):
        first = bridge.search_entities(**arguments)
    with instrument_tool_call("onec83_search_entities", logger):
        second = bridge.search_entities(**arguments)
    filter_calls = [call for call in client.calls if call[0] == "list" and call[4]]

    assert len(filter_calls) == 1
    assert first["serverFilterCacheHit"] is False
    assert second["serverFilterCacheHit"] is True
    assert second["serverFilterError"] == first["serverFilterError"]
    assert second["result"] == first["result"]
    assert second["complete"] is first["complete"] is True
    summary = summarize(log_path)
    assert summary["stages"]["server_filter_cache_hit"]["elapsed_ms"]["count"] == 1
    assert summary["search_profiles"]["date_range.fallback_scan"]["calls"] == 1
    cached_profile = summary["search_profiles"]["date_range.fallback_scan_cache_hit"]
    assert cached_profile["calls"] == 1
    assert cached_profile["fallback_page_reads"] == 2
    assert cached_profile["date_lower_bound_probes"] == 3
    assert "2010-04-07" not in log_path.read_text(encoding="utf-8")

    monkeypatch.setattr(bridge, "status", lambda: {"connected": True})
    bridge.refresh_metadata()
    bridge._metadata = metadata
    after_refresh = bridge.search_entities(**arguments)
    filter_calls = [call for call in client.calls if call[0] == "list" and call[4]]

    assert after_refresh["serverFilterCacheHit"] is False
    assert len(filter_calls) == 2

    client.config.odata_base_url = "https://other.example.test/base/odata/standard.odata/"
    after_target_change = bridge.search_entities(**arguments)
    filter_calls = [call for call in client.calls if call[0] == "list" and call[4]]

    assert after_target_change["serverFilterCacheHit"] is False
    assert len(filter_calls) == 3


def test_search_entities_falls_back_to_ordered_string_equality(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Catalog_Example",
        field="ИНН",
        equals="200",
        select="Ref_Key,ИНН",
        top=3,
        page_size=2,
        max_scan=10,
    )

    assert result["strategy"] == "linear_scan_after_filter_error"
    assert result["complete"] is True
    assert result["resultLimited"] is False
    assert result["startSkip"] == 0
    assert [row["Ref_Key"] for row in result["result"]["value"]] == [
        "00000000-0000-0000-0000-000000000022",
        "00000000-0000-0000-0000-000000000023",
    ]


@pytest.mark.parametrize("error_code", [None, "OTHER"])
def test_search_entities_fallback_does_not_cache_without_exact_where_signature(
    tmp_path: Path, error_code: str | None,
):
    bridge, client = make_bridge(tmp_path)
    original_list_entities = client.list_entities
    filter_attempts = 0

    def list_without_error_code(entity_set, top, skip, filter=None, select=None,
                                orderby=None, expand=None):
        nonlocal filter_attempts
        if filter:
            filter_attempts += 1
            raise ODataHTTPError(
                500,
                code=error_code,
                message='Операция не разрешена в предложении "ГДЕ"',
                definitive=False,
            )
        return original_list_entities(entity_set, top, skip, filter, select,
                                      orderby, expand)

    client.list_entities = list_without_error_code

    arguments = {
        "entity_set": "Catalog_Example",
        "field": "ИНН",
        "equals": "200",
        "select": "Ref_Key,ИНН",
        "top": 3,
        "page_size": 2,
        "max_scan": 10,
    }
    result = bridge.search_entities(**arguments)
    second = bridge.search_entities(**arguments)

    assert result["strategy"] == "linear_scan_after_filter_error"
    assert second["strategy"] == "linear_scan_after_filter_error"
    assert result["serverFilterError"]["code"] == error_code
    assert result["complete"] is True
    assert len(result["result"]["value"]) == 2
    assert second["serverFilterCacheHit"] is False
    assert filter_attempts == 2


def test_search_entities_server_filter_result_limit_is_not_complete(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Document_Example",
        field="Number",
        equals="1",
        select="Ref_Key,Number",
        top=1,
    )

    assert result["strategy"] == "server_filter"
    assert result["complete"] is False
    assert result["resultLimited"] is True
    assert result["warning"]


def test_search_entities_never_turns_scan_limit_into_definitive_zero(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Document_Example",
        field="Date",
        from_value="2010-03-01T00:00:00",
        to_exclusive="2010-12-31T00:00:00",
        select="Ref_Key,Date",
        top=10,
        page_size=1,
        max_scan=1,
    )

    assert result["strategy"] == "ordered_scan_after_filter_error"
    assert result["complete"] is False
    assert result["maxScanReached"] is True
    assert result["resumeSkip"] == 1
    assert result["warning"]
    assert result["result"]["value"]


def test_search_entities_reports_result_limit_before_full_predicate_exhaustion(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Catalog_Example",
        field="ИНН",
        equals="200",
        select="Ref_Key,ИНН",
        top=1,
        page_size=2,
        max_scan=10,
    )

    assert result["resultLimited"] is True
    assert result["complete"] is False
    assert len(result["result"]["value"]) == 1


def test_search_entities_string_scan_limit_is_not_definitive_zero(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities(
        "Catalog_Example",
        field="ИНН",
        equals="300",
        select="Ref_Key,ИНН",
        top=2,
        page_size=1,
        max_scan=2,
    )

    assert result["strategy"] == "linear_scan_after_filter_error"
    assert result["complete"] is False
    assert result["maxScanReached"] is True
    assert result["result"]["value"] == []
    assert result["warning"]


def test_search_entities_batch_reuses_one_scan_after_known_where_rejection(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Catalog_Example",
        field="ИНН",
        equals_values=["100", "200", "missing"],
        select="Ref_Key",
        top=2,
        page_size=2,
        max_scan=10,
    )

    assert result["strategy"] == "shared_fallback_scan"
    assert result["queryCount"] == 3
    assert result["complete"] is True
    assert result["serverFilterCacheHit"] is False
    assert result["serverFilterError"]["code"] == "-1"
    assert [item["predicate"]["value"] for item in result["results"]] == [
        "100", "200", "missing",
    ]
    assert [len(item["result"]["value"]) for item in result["results"]] == [1, 2, 0]
    assert all(item["complete"] for item in result["results"])
    assert all(set(row) <= {"Ref_Key", "ИНН"}
               for item in result["results"] for row in item["result"]["value"])
    assert result["scannedRows"] == 5
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1
    assert len([call for call in client.calls if call[0] == "count"]) == 1
    fallback_pages = [call for call in client.calls if call[0] == "list" and not call[4]]
    assert len(fallback_pages) == 3
    assert all(call[5] == "Ref_Key,ИНН" for call in fallback_pages)


def test_search_entities_batch_allows_empty_string_exact_match(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Catalog_Example", field="ИНН", equals_values=[""],
        select="Ref_Key", top=2, page_size=2, max_scan=10,
    )

    assert result["complete"] is True
    assert result["results"][0]["predicate"]["value"] == ""
    assert result["results"][0]["complete"] is True
    assert result["results"][0]["result"]["value"] == [
        {"Ref_Key": "00000000-0000-0000-0000-000000000020", "ИНН": ""},
    ]


def test_search_entities_batch_overall_completeness_reflects_limited_value(
    tmp_path: Path,
):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Catalog_Example", field="ИНН", equals_values=["200", "100"],
        select="Ref_Key", top=1, page_size=2, max_scan=10,
    )

    assert result["scannedRows"] == 5
    assert result["complete"] is False
    assert result["resultLimited"] is True
    assert result["results"][0]["complete"] is False
    assert result["results"][0]["resultLimited"] is True
    assert result["results"][1]["complete"] is True
    assert result["results"][1]["resultLimited"] is False


def test_search_entities_batch_reduces_synthetic_fallback_reads_by_more_than_tenfold(
    tmp_path: Path,
):
    values = [f"{number:04d}" for number in range(990, 1010)]

    def warm_rejection(bridge, client):
        bridge.search_entities(
            "Catalog_Example", field="ИНН", equals="warmup",
            select="Ref_Key,ИНН", top=1, page_size=100, max_scan=1000,
        )
        client.calls.clear()

    repeated_bridge, repeated_client = make_bridge(tmp_path / "repeated")
    repeated_client.datasets["Catalog_Example"] = [
        {"Ref_Key": f"key-{number:04d}", "ИНН": f"{number:04d}"}
        for number in range(1000)
    ]
    warm_rejection(repeated_bridge, repeated_client)
    repeated_results = [
        repeated_bridge.search_entities(
            "Catalog_Example", field="ИНН", equals=value,
            select="Ref_Key,ИНН", top=1, page_size=100, max_scan=1000,
        )
        for value in values
    ]

    batch_bridge, batch_client = make_bridge(tmp_path / "batch")
    batch_client.datasets["Catalog_Example"] = [
        {"Ref_Key": f"key-{number:04d}", "ИНН": f"{number:04d}"}
        for number in range(1000)
    ]
    warm_rejection(batch_bridge, batch_client)
    batch_result = batch_bridge.search_entities_batch(
        "Catalog_Example", field="ИНН", equals_values=values,
        select="Ref_Key,ИНН", top=1, page_size=100, max_scan=1000,
    )

    def read_count(client):
        return sum(call[0] in {"count", "list"} for call in client.calls)

    def returned_rows(client):
        return sum(
            min(call[2], max(0, 1000 - call[3]))
            for call in client.calls if call[0] == "list"
        )

    assert all(not item["complete"] for item in repeated_results[:10])
    assert all(item["complete"] for item in repeated_results[10:])
    assert batch_result["complete"] is True
    assert read_count(repeated_client) == 220
    assert read_count(batch_client) == 11
    assert read_count(repeated_client) / read_count(batch_client) == 20
    assert returned_rows(repeated_client) == 20_000
    assert returned_rows(batch_client) == 1_000


def test_search_entities_batch_resume_keeps_absence_incomplete_until_finish(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)
    client.datasets["Catalog_Example"] = [
        {"Ref_Key": f"resume-{index}", "ИНН": value}
        for index, value in enumerate(["300", "300", "300", "400"])
    ]
    bridge.search_entities(
        "Catalog_Example", field="ИНН", equals="warmup",
        select="Ref_Key,ИНН", top=1, page_size=2, max_scan=10,
    )
    client.calls.clear()
    original_list_entities = client.list_entities
    failed_once = False

    def timeout_after_first_page(entity_set, top, skip, filter=None,
                                 select=None, orderby=None, expand=None):
        nonlocal failed_once
        if not filter and skip == 2 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=3)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = timeout_after_first_page
    arguments = {
        "entity_set": "Catalog_Example",
        "field": "ИНН",
        "equals_values": ["300", "missing"],
        "select": "Ref_Key,ИНН",
        "top": 2,
        "page_size": 2,
        "max_scan": 10,
    }

    partial = bridge.search_entities_batch(**arguments)

    assert partial["ok"] is False
    assert partial["complete"] is False
    assert partial["scanInterrupted"] is True
    assert partial["resumeSkip"] == 2
    assert partial["resumeCompletenessUnknown"] is False
    assert all(not item["complete"] for item in partial["results"])

    resumed = bridge.search_entities_batch(
        **arguments, resume_skip=partial["resumeSkip"],
    )

    assert resumed["ok"] is True
    assert resumed["complete"] is False
    assert resumed["filterSkippedForResume"] is True
    assert resumed["resumeCompletenessUnknown"] is True
    assert "candidates only" in resumed["warning"]
    assert [row["ИНН"] for row in resumed["results"][0]["result"]["value"]] == ["300"]
    assert resumed["results"][0]["complete"] is False
    assert resumed["results"][0]["resumeCompletenessUnknown"] is True
    assert resumed["results"][1]["complete"] is False


def test_search_entities_batch_resume_from_zero_can_complete_full_scan(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)
    original_list_entities = client.list_entities
    failed_once = False

    def timeout_before_first_page(entity_set, top, skip, filter=None,
                                  select=None, orderby=None, expand=None):
        nonlocal failed_once
        if not filter and skip == 0 and not failed_once:
            failed_once = True
            raise ODataTransportError("timeout", attempts=1)
        return original_list_entities(
            entity_set, top, skip, filter, select, orderby, expand,
        )

    client.list_entities = timeout_before_first_page
    arguments = {
        "entity_set": "Catalog_Example",
        "field": "ИНН",
        "equals_values": ["100"],
        "select": "Ref_Key,ИНН",
        "top": 2,
        "page_size": 2,
        "max_scan": 10,
    }

    partial = bridge.search_entities_batch(**arguments)
    assert partial["scanInterrupted"] is True
    assert partial["resumeSkip"] == 0

    resumed = bridge.search_entities_batch(
        **arguments, resume_skip=partial["resumeSkip"],
    )

    assert resumed["complete"] is True
    assert resumed["resumeCompletenessUnknown"] is False
    assert resumed["results"][0]["complete"] is True
    assert [row["ИНН"] for row in resumed["results"][0]["result"]["value"]] == ["100"]


def test_search_entities_batch_validates_values_and_explicit_projection(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    with pytest.raises(ValueError, match="1..50"):
        bridge.search_entities_batch(
            "Catalog_Example", field="ИНН", equals_values=[],
        )
    with pytest.raises(ValueError, match="unique"):
        bridge.search_entities_batch(
            "Catalog_Example", field="ИНН", equals_values=["100", "100"],
        )
    with pytest.raises(ValueError, match="explicit projected select"):
        bridge.search_entities_batch(
            "Catalog_Example", field="ИНН", equals_values=["100"], select="**",
        )
    with pytest.raises(ValueError, match="row budget"):
        bridge.search_entities_batch(
            "Catalog_Example", field="ИНН",
            equals_values=["100", "101", "102", "103", "104", "105"],
            top=200,
        )
    assert client.calls == []


def test_search_entities_batch_keeps_bounded_scan_absence_incomplete(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Catalog_Example", field="ИНН", equals_values=["not-present"],
        select="Ref_Key,ИНН", top=2, page_size=1, max_scan=1,
    )

    assert result["complete"] is False
    assert result["maxScanReached"] is True
    assert result["resumeSkip"] == 1
    assert result["snapshotConsistent"] is False
    assert result["results"][0]["complete"] is False
    assert result["results"][0]["result"]["value"] == []
    assert result["results"][0]["maxScanReached"] is True


def test_search_entities_batch_supports_date_equality_fallback(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Document_Example", field="Date",
        equals_values=["2010-04-07T09:00:00", "2010-04-08T09:00:00"],
        select="Ref_Key,Date", top=2, page_size=2, max_scan=10,
    )

    assert result["complete"] is True
    assert [item["predicate"]["type"] for item in result["results"]] == [
        "Edm.DateTime", "Edm.DateTime",
    ]
    assert [item["result"]["value"][0]["Date"] for item in result["results"]] == [
        "2010-04-07T09:00:00", "2010-04-08T09:00:00",
    ]
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 1


def test_search_entities_batch_server_filter_path_preserves_per_value_results(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)

    result = bridge.search_entities_batch(
        "Document_Example", field="Number", equals_values=["1", "2"],
        select="Ref_Key,Number", top=1,
    )

    assert result["strategy"] == "server_filter_batch"
    assert result["queryCount"] == 2
    assert result["complete"] is False
    assert result["resultLimited"] is True
    assert all(item["strategy"] == "server_filter" for item in result["results"])
    assert len([call for call in client.calls if call[0] == "list" and call[4]]) == 2
    assert not [call for call in client.calls if call[0] == "count"]


def test_counterparty_classification_advisory_does_not_block_generic_dry_run(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    missing_classification = bridge.create_entity(
        "Catalog_Контрагенты", {"Description": "Synthetic", "ИНН": "001ABC234"},
    )
    assert missing_classification["dryRun"] is True

    unobserved_classification = bridge.create_entity(
        "Catalog_Контрагенты",
        {"Description": "Synthetic", "ИНН": "001ABC234", "ЮридическоеФизическоеЛицо": "SyntheticNewValue"},
    )
    assert unobserved_classification["dryRun"] is True

    patch = bridge.patch_entity(
        "Catalog_Контрагенты",
        "00000000-0000-0000-0000-000000000001",
        {"ЮридическоеФизическоеЛицо": "SyntheticNewValue"},
    )
    assert patch["dryRun"] is True
    assert client.calls == []


def test_unpublished_payload_field_reports_field_name_before_write(tmp_path: Path, monkeypatch):
    bridge, client = make_bridge(tmp_path)
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)

    result = server_module.onec83_create_entity(
        "Catalog_БанковскиеСчета",
        {
            "Owner": "00000000-0000-0000-0000-000000000001",
            "Owner_Type": "StandardODATA.Catalog_Контрагенты",
            "НомерСчета": "3012000000011",
            "Комментарий": "raw migration note",
        },
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    message = result["error"]["message"]
    assert "Catalog_БанковскиеСчета" in message
    assert "Комментарий" in message
    assert "not published in OData metadata" in message
    assert "НомерСчета" in message
    assert client.calls == []


def test_applied_create_needs_exact_target_and_idempotency_receipt(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    with pytest.raises(PermissionError):
        bridge.create_entity("Document_Example", {"Number": "1"}, dry_run=False,
                             confirm=True, expected_target="wrong", operation_id="pilot-0001")
    result = bridge.create_entity("Document_Example", {"Number": "1"}, dry_run=False,
                                  confirm=True, expected_target=bridge.target_fingerprint,
                                  operation_id="pilot-0001")
    assert result["readbackFetched"] is True
    assert result["readbackVerified"] is True
    assert result["readbackVerification"]["comparedFields"] == ["Number"]
    assert result["readbackVerification"]["status"] == "verified"
    assert [call[0] for call in client.calls] == ["create", "get"]
    assert client.calls[0][2]["Ref_Key"] == "00000000-0000-0000-0000-000000000000"
    assert client.calls[0][3] is True
    replay = bridge.create_entity("Document_Example", {"Number": "1"}, dry_run=False,
                                  confirm=True, expected_target=bridge.target_fingerprint,
                                  operation_id="pilot-0001")
    assert replay["replayedReceipt"] is True
    assert [call[0] for call in client.calls] == ["create", "get"]


def _catalog_create_verification_payload():
    return {
        "Description": "Synthetic account",
        "Owner": "00000000-0000-0000-0000-000000000101",
        "Owner_Type": "StandardODATA.Catalog_Organizations",
        "Bank": "00000000-0000-0000-0000-000000000102",
        "Bank_Type": "StandardODATA.Catalog_Banks",
        "Currency": "00000000-0000-0000-0000-000000000103",
        "Currency_Type": "StandardODATA.Catalog_Currencies",
        "AccountNumber": "synthetic-account-001",
        "AccountKind": "Current",
        "AccountKind_Type": "StandardODATA.EnumRef_AccountKinds",
        "CurrencyFlag": True,
    }


def test_catalog_create_verifies_matching_typed_scalar_fields_and_persists_report(
    tmp_path: Path,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    payload = _catalog_create_verification_payload()

    def readback(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        assert requested_key == key
        return {"Ref_Key": key, **payload, "DataVersion": "v1"}

    client.get_entity = readback
    result = bridge.create_entity(
        "Catalog_CreateVerification", payload, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-catalog-create-readback-verified",
    )

    assert result["readbackFetched"] is True
    assert result["readbackVerified"] is True
    assert result["readbackVerification"]["status"] == "verified"
    assert result["readbackVerification"]["comparedFields"] == list(payload)
    assert "Ref_Key" not in result["readbackVerification"]["comparedFields"]
    receipt = bridge.ledger.inspect("pilot-catalog-create-readback-verified")["result"]
    assert receipt["readbackVerified"] is True
    assert receipt["readbackVerification"] == result["readbackVerification"]
    assert [call[0] for call in client.calls] == ["create", "get"]


@pytest.mark.parametrize(
    ("extra_payload", "readback_overrides", "field_list", "reason_code"),
    [
        ({}, {"CurrencyFlag": False}, "mismatchedFields", "submitted_field_mismatch"),
        (
            {"ComplexValue": {"secret": "synthetic-sensitive-value"}},
            {"ComplexValue": {"secret": "synthetic-sensitive-value"}},
            "unsupportedFields", "unsupported_field_type",
        ),
    ],
)
def test_catalog_create_reports_mismatch_and_unsupported_readback_fields_without_values(
    tmp_path: Path, extra_payload, readback_overrides, field_list, reason_code,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    payload = _catalog_create_verification_payload() | extra_payload
    actual = _catalog_create_verification_payload() | readback_overrides

    def readback(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        return {"Ref_Key": key, **actual, "DataVersion": "v1"}

    client.get_entity = readback
    result = bridge.create_entity(
        "Catalog_CreateVerification", payload, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-catalog-create-readback-diagnostic",
    )

    report = result["readbackVerification"]
    assert result["readbackVerified"] is False
    assert report["status"] == "unverified"
    assert report[field_list] == ["CurrencyFlag" if field_list == "mismatchedFields" else "ComplexValue"]
    assert reason_code in report["reasonCodes"]
    assert "synthetic-sensitive-value" not in json.dumps(report)
    receipt = bridge.ledger.inspect("pilot-catalog-create-readback-diagnostic")["result"]
    assert receipt["readbackVerified"] is False
    assert receipt["readbackVerification"] == report


def _reference_check_payload(header_key, second_row_key):
    return {
        "Number": "synthetic-reference-check",
        "ДоговорКонтрагента_Key": header_key,
        "РасшифровкаПлатежа": [
            {"LineNumber": 1, "ДоговорКонтрагента_Key": header_key},
            {"LineNumber": 2, "ДоговорКонтрагента_Key": second_row_key},
        ],
    }


def _install_reference_lookup(client, available_keys, monkeypatch):
    def list_reference_entities(entity_set, top, skip, filter=None, select=None,
                                orderby=None, expand=None):
        client.calls.append(("reference-list", entity_set, top, skip, filter, select))
        requested = {
            value.split("guid'", 1)[1].split("'", 1)[0]
            for value in filter.split(" or ")
        }
        assert entity_set == "Catalog_ReferenceContracts"
        assert select == "Ref_Key"
        return {"value": [
            {"Ref_Key": key}
            for key in sorted(available_keys.intersection(requested))
        ]}

    monkeypatch.setattr(client, "list_entities", list_reference_entities)


def test_create_batches_and_deduplicates_csdl_reference_checks_in_header_and_rows(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    header_key = "00000000-0000-0000-0000-000000000101"
    second_row_key = "00000000-0000-0000-0000-000000000102"
    _install_reference_lookup(client, {header_key, second_row_key}, monkeypatch)

    result = bridge.create_entity(
        "Document_ReferenceCheck",
        _reference_check_payload(header_key, second_row_key),
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-create-ok",
    )

    assert result["referenceValidation"]["status"] == "verified"
    assert result["referenceValidation"]["checkedReferenceCount"] == 2
    lookups = [call for call in client.calls if call[0] == "reference-list"]
    assert len(lookups) == 1
    assert lookups[0][2] == 3
    assert lookups[0][4] == (
        f"Ref_Key eq guid'{header_key}' or Ref_Key eq guid'{second_row_key}'"
    )
    assert [call[0] for call in client.calls] == [
        "reference-list", "create", "get",
    ]
    receipt = bridge.ledger.inspect("reference-check-create-ok")["result"]
    assert receipt["referenceValidation"] == result["referenceValidation"]


def test_create_missing_csdl_reference_fails_before_write_without_guid_leak(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    header_key = "00000000-0000-0000-0000-000000000111"
    missing_key = "00000000-0000-0000-0000-000000000112"
    _install_reference_lookup(client, {header_key}, monkeypatch)

    result = bridge.create_entity(
        "Document_ReferenceCheck",
        _reference_check_payload(header_key, missing_key),
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-create-missing",
    )

    assert result["ok"] is False
    assert result["writeState"] == "failed"
    assert result["phase"] == "reference_validation"
    assert result["referenceValidation"]["status"] == "missing"
    assert result["referenceValidation"]["missingReferences"] == [{
        "fieldPath": "Document_ReferenceCheck.РасшифровкаПлатежа[1].ДоговорКонтрагента_Key",
        "targetEntitySet": "Catalog_ReferenceContracts",
    }]
    assert [call[0] for call in client.calls] == ["reference-list"]
    serialized = json.dumps({
        "result": result,
        "receipt": bridge.ledger.inspect("reference-check-create-missing"),
    })
    assert header_key not in serialized
    assert missing_key not in serialized


def test_patch_missing_csdl_reference_fails_before_write(tmp_path: Path, monkeypatch):
    bridge, client = make_reference_bridge(tmp_path)
    missing_key = "00000000-0000-0000-0000-000000000118"
    _install_reference_lookup(client, set(), monkeypatch)

    result = bridge.patch_entity(
        "Document_ReferenceCheck",
        "00000000-0000-0000-0000-000000000119",
        {"ДоговорКонтрагента_Key": missing_key},
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-patch-missing",
        expected_data_version="synthetic-version-1",
    )

    assert result["ok"] is False
    assert result["phase"] == "reference_validation"
    assert result["referenceValidation"]["status"] == "missing"
    assert [call[0] for call in client.calls] == ["reference-list"]


def test_create_reference_lookup_error_is_unknown_and_blocks_write_without_echo(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    reference_key = "00000000-0000-0000-0000-000000000121"

    def failed_reference_lookup(entity_set, top, skip, filter=None, select=None,
                                orderby=None, expand=None):
        client.calls.append(("reference-list", entity_set, filter, select))
        raise ODataHTTPError(403, code="9", message=f"denied {reference_key}")

    monkeypatch.setattr(client, "list_entities", failed_reference_lookup)
    result = bridge.create_entity(
        "Document_ReferenceCheck",
        _reference_check_payload(reference_key, reference_key),
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-create-unknown",
    )

    assert result["ok"] is False
    assert result["referenceValidation"]["status"] == "unknown"
    assert "reference_lookup_http_error" in result["referenceValidation"]["reasonCodes"]
    assert result["referenceValidation"]["unverifiedReferences"][0]["httpStatus"] == 403
    assert [call[0] for call in client.calls] == ["reference-list"]
    serialized = json.dumps({
        "result": result,
        "receipt": bridge.ledger.inspect("reference-check-create-unknown"),
    })
    assert reference_key not in serialized
    assert "denied" not in serialized


def test_known_reference_filter_rejection_uses_keyed_get_and_caches(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    reference_key = "00000000-0000-0000-0000-000000000141"
    second_key = "00000000-0000-0000-0000-000000000142"
    calls = []

    def rejected_reference_lookup(entity_set, top, skip, filter=None, select=None,
                                  orderby=None, expand=None):
        calls.append(filter)
        raise ODataHTTPError(
            500,
            code="-1",
            message='Операция не разрешена в предложении "ГДЕ"',
        )

    monkeypatch.setattr(client, "list_entities", rejected_reference_lookup)
    payload = _reference_check_payload(reference_key, second_key)
    first = bridge.create_entity(
        "Document_ReferenceCheck", payload,
        dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-filter-1",
    )
    second = bridge.create_entity(
        "Document_ReferenceCheck", payload,
        dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-filter-2",
    )

    assert first["referenceValidation"]["status"] == "verified"
    assert "reference_filter_unsupported" in first["referenceValidation"]["reasonCodes"]
    assert "reference_filter_unsupported_keyed_get_fallback" in first["referenceValidation"]["reasonCodes"]
    assert second["referenceValidation"]["status"] == "verified"
    assert "reference_filter_known_unsupported" in second["referenceValidation"]["reasonCodes"]
    assert first["referenceValidation"]["lookupCount"] == 3
    assert second["referenceValidation"]["lookupCount"] == 2
    assert len(calls) == 1
    reference_gets = [
        call for call in client.calls
        if call[0] == "get" and call[1] == "Catalog_ReferenceContracts"
    ]
    assert [call[2] for call in reference_gets] == [
        reference_key, second_key, reference_key, second_key,
    ]
    assert len([call for call in client.calls if call[0] == "create"]) == 2


def test_known_filter_rejection_classifies_keyed_404_as_unknown_before_write(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    existing_key = "00000000-0000-0000-0000-000000000151"
    inconclusive_key = "00000000-0000-0000-0000-000000000152"
    monkeypatch.setattr(
        client,
        "list_entities",
        lambda *args, **kwargs: (_ for _ in ()).throw(ODataHTTPError(
            500, code="-1",
            message='Операция не разрешена в предложении "ГДЕ"',
        )),
    )
    original_get = client.get_entity

    def get_with_inconclusive_404(entity_set, key):
        if entity_set == "Catalog_ReferenceContracts" and key == inconclusive_key:
            client.calls.append(("get", entity_set, key))
            raise ODataHTTPError(404, code="9", message=f"not found {inconclusive_key}")
        return original_get(entity_set, key)

    monkeypatch.setattr(client, "get_entity", get_with_inconclusive_404)
    result = bridge.create_entity(
        "Document_ReferenceCheck",
        _reference_check_payload(existing_key, inconclusive_key),
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-keyed-404",
    )

    assert result["ok"] is False
    assert result["phase"] == "reference_validation"
    validation = result["referenceValidation"]
    assert validation["status"] == "unknown"
    assert validation["checkedReferenceCount"] == 1
    assert validation["missingReferences"] == []
    assert validation["unverifiedReferences"] == [{
        "fieldPath": "Document_ReferenceCheck.РасшифровкаПлатежа[1].ДоговорКонтрагента_Key",
        "targetEntitySet": "Catalog_ReferenceContracts",
        "reasonCode": "reference_keyed_get_404_inconclusive",
        "httpStatus": 404,
    }]
    assert "reference_target_missing" not in validation["reasonCodes"]
    assert bridge.ledger.inspect("reference-check-keyed-404")["result"] == result
    assert not any(call[0] == "create" for call in client.calls)
    serialized = json.dumps(result)
    assert inconclusive_key not in serialized
    assert "not found" not in serialized


@pytest.mark.parametrize("response_mode", ["http_error", "wrong_key"])
def test_keyed_get_fallback_unknown_response_blocks_write_without_echo(
    tmp_path: Path, monkeypatch, response_mode: str,
):
    bridge, client = make_reference_bridge(tmp_path)
    reference_key = "00000000-0000-0000-0000-000000000161"
    monkeypatch.setattr(
        client,
        "list_entities",
        lambda *args, **kwargs: (_ for _ in ()).throw(ODataHTTPError(
            500, code="-1",
            message='Операция не разрешена в предложении "ГДЕ"',
        )),
    )

    def failed_or_wrong_get(entity_set, key):
        client.calls.append(("get", entity_set, key))
        if response_mode == "http_error":
            raise ODataHTTPError(403, code="9", message=f"denied {reference_key}")
        return {"Ref_Key": "00000000-0000-0000-0000-000000000162"}

    monkeypatch.setattr(client, "get_entity", failed_or_wrong_get)
    result = bridge.create_entity(
        "Document_ReferenceCheck",
        _reference_check_payload(reference_key, reference_key),
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id=f"reference-check-keyed-unknown-{response_mode}",
    )

    assert result["ok"] is False
    assert result["phase"] == "reference_validation"
    assert result["referenceValidation"]["status"] == "unknown"
    assert not any(call[0] == "create" for call in client.calls)
    serialized = json.dumps(result)
    assert reference_key not in serialized
    assert "denied" not in serialized


def test_keyed_get_fallback_is_bounded_per_write(tmp_path: Path, monkeypatch):
    bridge, client = make_reference_bridge(tmp_path)
    keys = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 52)]
    monkeypatch.setattr(
        client,
        "list_entities",
        lambda *args, **kwargs: (_ for _ in ()).throw(ODataHTTPError(
            500, code="-1",
            message='Операция не разрешена в предложении "ГДЕ"',
        )),
    )
    result = bridge.create_entity(
        "Document_ReferenceCheck",
        {
            "Number": "synthetic-reference-limit",
            "ДоговорКонтрагента_Key": keys[0],
            "РасшифровкаПлатежа": [
                {"LineNumber": index + 1, "ДоговорКонтрагента_Key": key}
                for index, key in enumerate(keys)
            ],
        },
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="reference-check-keyed-limit",
    )

    assert result["ok"] is False
    assert result["referenceValidation"]["status"] == "unknown"
    assert "reference_keyed_get_limit_exceeded" in result["referenceValidation"]["reasonCodes"]
    reference_gets = [
        call for call in client.calls
        if call[0] == "get" and call[1] == "Catalog_ReferenceContracts"
    ]
    assert len(reference_gets) == 50
    assert result["referenceValidation"]["lookupCount"] == 51
    assert not any(call[0] == "create" for call in client.calls)


def test_document_queue_exposes_reference_validation_failure(tmp_path: Path, monkeypatch):
    bridge, client = make_reference_bridge(tmp_path)
    header_key = "00000000-0000-0000-0000-000000000131"
    missing_key = "00000000-0000-0000-0000-000000000132"
    _install_reference_lookup(client, {header_key}, monkeypatch)

    result = bridge.apply_document_queue(
        [{
            "entity_set": "Document_ReferenceCheck",
            "payload": _reference_check_payload(header_key, missing_key),
            "operation_id": "reference-queue-create-missing",
        }],
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
    )

    row = result["results"][0]
    assert row["ok"] is False
    assert row["state"] == "failed"
    assert row["referenceValidation"]["status"] == "missing"
    assert [call[0] for call in client.calls] == ["reference-list"]


def test_document_queue_keyed_404_holds_only_affected_row_without_guid_leak(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_reference_bridge(tmp_path)
    inconclusive_key = "00000000-0000-0000-0000-000000000133"
    existing_key = "00000000-0000-0000-0000-000000000134"

    def rejected_reference_lookup(*args, **kwargs):
        client.calls.append(("reference-list",))
        raise ODataHTTPError(
            500, code="-1", message='Операция не разрешена в предложении "ГДЕ"',
        )

    def keyed_get_with_404(entity_set, key):
        client.calls.append(("get", entity_set, key))
        if entity_set == "Catalog_ReferenceContracts" and key == inconclusive_key:
            raise ODataHTTPError(404, code="9", message=f"missing {inconclusive_key}")
        if entity_set == "Document_ReferenceCheck":
            return {
                "Ref_Key": key,
                **_reference_check_payload(existing_key, existing_key),
                "DataVersion": "synthetic-version-1",
            }
        return {"Ref_Key": key, "DataVersion": "synthetic-version-1"}

    monkeypatch.setattr(client, "list_entities", rejected_reference_lookup)
    monkeypatch.setattr(client, "get_entity", keyed_get_with_404)
    result = bridge.apply_document_queue(
        [
            {
                "entity_set": "Document_ReferenceCheck",
                "payload": _reference_check_payload(inconclusive_key, inconclusive_key),
                "operation_id": "reference-queue-keyed-404",
            },
            {
                "entity_set": "Document_ReferenceCheck",
                "payload": _reference_check_payload(existing_key, existing_key),
                "operation_id": "reference-queue-keyed-existing",
            },
        ],
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        max_concurrency=1,
    )

    held, applied = result["results"]
    assert held["ok"] is False
    assert held["state"] == "failed"
    assert held["referenceValidation"]["status"] == "unknown"
    assert held["referenceValidation"]["missingReferences"] == []
    assert all(
        item["reasonCode"] == "reference_keyed_get_404_inconclusive"
        for item in held["referenceValidation"]["unverifiedReferences"]
    )
    assert applied["ok"] is True
    assert applied["referenceValidation"]["status"] == "verified"
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    assert len([call for call in client.calls if call[0] == "reference-list"]) == 1
    serialized = json.dumps({
        "row": held,
        "receipt": bridge.ledger.inspect("reference-queue-keyed-404"),
    })
    assert inconclusive_key not in serialized
    assert f"missing {inconclusive_key}" not in serialized


def test_document_queue_dry_run_preflights_every_row_without_writes(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": str(i)},
         "operation_id": f"batch-dry-{i:04d}"}
        for i in range(2)
    ]

    result = bridge.apply_document_queue(items, max_concurrency=2)

    assert result["dryRun"] is True
    assert result["total"] == 2
    assert len(result["results"]) == 2
    assert all(row["createPlan"]["operation"] == "create" for row in result["results"])
    assert client.calls == []


def test_document_queue_resumes_completed_rows_without_duplicate_creates(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": "1"},
         "operation_id": f"batch-create-{i:04d}"}
        for i in range(2)
    ]
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
        "max_concurrency": 2,
    }

    first = bridge.apply_document_queue(items, **args)
    call_count = len(client.calls)
    second = bridge.apply_document_queue(items, **args)

    assert first["succeeded"] == 2
    assert second["succeeded"] == 2
    assert [row["createReplayed"] for row in second["results"]] == [True, True]
    assert len(client.calls) == call_count
    assert sum(call[0] == "create" for call in client.calls) == 2


def test_document_queue_keeps_successful_rows_when_one_create_fails(tmp_path: Path, monkeypatch):
    bridge, client = make_bridge(tmp_path)
    original_create = client.create_entity

    def create_with_one_refusal(entity_set, payload, null_ref_key=False):
        if payload.get("Number") == "refused":
            raise ODataHTTPError(400, code="validation", message="synthetic refusal", definitive=True)
        return original_create(entity_set, payload, null_ref_key)

    monkeypatch.setattr(client, "create_entity", create_with_one_refusal)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": "1"},
         "operation_id": "batch-good-0001"},
        {"entity_set": "Document_Example", "payload": {"Number": "refused"},
         "operation_id": "batch-refused-001"},
    ]

    result = bridge.apply_document_queue(
        items, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, max_concurrency=2,
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["needsReconciliation"] == 0
    assert [row["state"] for row in result["results"]] == ["created", "failed"]
    assert sum(call[0] == "create" for call in client.calls) == 1


def test_document_queue_marks_accepted_create_without_readback_for_reconciliation(
    tmp_path: Path, monkeypatch
):
    bridge, client = make_bridge(tmp_path)
    operation_id = "batch-readback-failed-01"
    item = {
        "entity_set": "Document_Example",
        "payload": {"Number": "readback-failed"},
        "operation_id": operation_id,
    }

    def unavailable_readback(_entity_set, _key):
        raise ODataClientError("synthetic readback failure")

    monkeypatch.setattr(client, "get_entity", unavailable_readback)
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
    }

    first = bridge.apply_document_queue([item], **args)
    second = bridge.apply_document_queue([item], **args)

    assert first["succeeded"] == 0
    assert first["needsReconciliation"] == 1
    assert first["results"][0]["state"] == "needs_reconciliation"
    assert first["results"][0]["createReadbackFetched"] is False
    assert second["needsReconciliation"] == 1
    assert second["results"][0]["createReplayed"] is True
    assert sum(call[0] == "create" for call in client.calls) == 1


@pytest.mark.parametrize(
    ("readback_fields", "readback_key", "expected_reason"),
    [
        ({"Number": "different"}, None, "submitted_field_mismatch"),
        ({}, None, "submitted_field_missing"),
        ({"Number": "submitted"}, "00000000-0000-0000-0000-000000000002", "key_mismatch"),
    ],
)
def test_document_queue_holds_contradicted_create_before_post_and_on_replay(
    tmp_path: Path, monkeypatch, readback_fields, readback_key, expected_reason,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def readback(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        return {"Ref_Key": readback_key or key, "DataVersion": "v1", **readback_fields}

    monkeypatch.setattr(client, "get_entity", readback)
    item = {
        "entity_set": "Document_Example", "payload": {"Number": "submitted"},
        "operation_id": "batch-create-contradiction-01", "post": True,
        "post_operation_id": "batch-post-contradiction-01",
    }
    args = {"dry_run": False, "confirm": True,
            "expected_target": bridge.target_fingerprint}

    first = bridge.apply_document_queue([item], **args)
    second = bridge.apply_document_queue([item], **args)

    assert first["needsReconciliation"] == second["needsReconciliation"] == 1
    assert first["results"][0]["state"] == second["results"][0]["state"] == "needs_reconciliation"
    assert second["results"][0]["createReplayed"] is True
    receipt = bridge.ledger.inspect(item["operation_id"])["result"]
    assert expected_reason in receipt["readbackVerification"]["reasonCodes"]
    assert [call[0] for call in client.calls] == ["create", "get"]


def test_document_queue_posts_when_only_tabular_readback_is_unsupported(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def readback(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        return {"Ref_Key": key, "Number": "submitted", "DataVersion": "v1"}

    monkeypatch.setattr(client, "get_entity", readback)
    item = {
        "entity_set": "Document_Example",
        "payload": {"Number": "submitted", "Lines": [{"LineNumber": 1}]},
        "operation_id": "batch-create-unsupported-01", "post": True,
        "post_operation_id": "batch-post-unsupported-01",
    }

    result = bridge.apply_document_queue(
        [item], dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
    )

    assert result["succeeded"] == 1
    assert result["results"][0]["state"] == "posted"
    receipt = bridge.ledger.inspect(item["operation_id"])["result"]
    assert receipt["readbackVerified"] is False
    assert receipt["readbackVerification"]["unsupportedFields"] == ["Lines"]
    assert [call[0] for call in client.calls].count("action") == 1


def test_document_queue_does_not_retry_uncertain_transport_create(tmp_path: Path, monkeypatch):
    bridge, client = make_bridge(tmp_path)

    def interrupted_create(_entity_set, _payload, null_ref_key=False):
        raise ODataTransportError("tls", attempts=1)

    monkeypatch.setattr(client, "create_entity", interrupted_create)
    items = [{
        "entity_set": "Document_Example",
        "payload": {"Number": "unknown-outcome"},
        "operation_id": "batch-unknown-001",
    }]
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
    }

    first = bridge.apply_document_queue(items, **args)
    second = bridge.apply_document_queue(items, **args)

    assert first["needsReconciliation"] == 1
    assert first["results"][0]["state"] == "needs_reconciliation"
    assert second["needsReconciliation"] == 1
    assert client.calls == []


def test_document_queue_replays_post_receipt_without_reposting(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    items = [{
        "entity_set": "Document_Example",
        "payload": {"Number": "1"},
        "operation_id": "batch-post-create-01",
        "post": True,
        "post_operation_id": "batch-post-action-01",
    }]
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
        "max_concurrency": 1,
    }

    first = bridge.apply_document_queue(items, **args)
    call_count = len(client.calls)
    second = bridge.apply_document_queue(items, **args)

    assert first["succeeded"] == 1
    assert first["results"][0]["postState"] == "accepted"
    assert second["succeeded"] == 1
    assert second["results"][0]["postReplayed"] is True
    assert len(client.calls) == call_count
    assert sum(call[0] == "action" for call in client.calls) == 1


def test_document_queue_bounds_concurrent_rows(tmp_path: Path, monkeypatch):
    bridge, client = make_bridge(tmp_path)
    original_create = client.create_entity
    lock = threading.Lock()
    active = 0
    peak = 0

    def slow_create(entity_set, payload, null_ref_key=False):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return original_create(entity_set, payload, null_ref_key)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(client, "create_entity", slow_create)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": "1"},
         "operation_id": f"batch-parallel-{i:04d}"}
        for i in range(8)
    ]

    result = bridge.apply_document_queue(
        items, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, max_concurrency=4,
    )

    assert result["succeeded"] == 8
    assert result["maxConcurrency"] == 4
    assert 2 <= peak <= 4


def test_document_queue_rejects_duplicate_operation_ids_before_writing(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": "1"},
         "operation_id": "batch-duplicate-01"},
        {"entity_set": "Document_Example", "payload": {"Number": "2"},
         "operation_id": "batch-duplicate-01"},
    ]

    with pytest.raises(ValueError, match="unique"):
        bridge.apply_document_queue(
            items, dry_run=False, confirm=True,
            expected_target=bridge.target_fingerprint,
        )

    assert client.calls == []


def test_document_queue_preflights_later_rows_before_applying_earlier_rows(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    items = [
        {"entity_set": "Document_Example", "payload": {"Number": "valid"},
         "operation_id": "batch-preflight-ok"},
        {"entity_set": "Document_Example", "payload": {"Unexpected": "invalid"},
         "operation_id": "batch-preflight-bad"},
    ]

    with pytest.raises(ValueError, match="not published"):
        bridge.apply_document_queue(
            items, dry_run=False, confirm=True,
            expected_target=bridge.target_fingerprint, max_concurrency=2,
        )

    assert client.calls == []


def test_create_does_not_inject_ref_key_when_metadata_does_not_declare_it(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    bridge.create_entity("InformationRegister_Example",
                         {"Period": "2026-09-28T00:00:00", "Value": 1},
                         dry_run=False, confirm=True,
                         expected_target=bridge.target_fingerprint,
                         operation_id="pilot-register-0001")

    assert client.calls[0] == (
        "create",
        "InformationRegister_Example",
        {"Period": "2026-09-28T00:00:00", "Value": 1},
        False,
    )


def test_patch_requires_dataversion_and_passes_if_match(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    with pytest.raises(ValueError, match="expected_data_version"):
        bridge.patch_entity("Document_Example", "00000000-0000-0000-0000-000000000001",
                            {"Number": "2"}, dry_run=False)
    bridge.patch_entity("Document_Example", "00000000-0000-0000-0000-000000000001",
                        {"Number": "2"}, dry_run=False, confirm=True,
                        expected_target=bridge.target_fingerprint,
                        operation_id="pilot-patch-0001", expected_data_version="v1")
    assert client.calls[0][-1] == "v1"


def test_update_alias_uses_patch_safety(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    bridge.update_entity("Document_Example", "00000000-0000-0000-0000-000000000001",
                         {"Number": "2"}, dry_run=False, confirm=True,
                         expected_target=bridge.target_fingerprint,
                         operation_id="pilot-update-0001", expected_data_version="v1")

    assert [call[0] for call in client.calls] == ["patch", "get"]


def test_document_row_entity_sets_are_read_only_for_writes(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    with pytest.raises(ValueError, match="tabular-row entity sets are read-only"):
        bridge.create_entity("Document_Example_Lines", {"LineNumber": 1})
    with pytest.raises(ValueError, match="tabular-row entity sets are read-only"):
        bridge.patch_entity("Document_Example_Lines", key, {"LineNumber": 2})
    with pytest.raises(ValueError, match="tabular-row entity sets are read-only"):
        bridge.post_document("Document_Example_Lines", key)
    with pytest.raises(ValueError, match="tabular-row entity sets are read-only"):
        bridge._prepare_write(entity_set="Document_Example_Lines", operation="create",
                              payload={"LineNumber": 1}, key=None, dry_run=True,
                              confirm=False, expected_target=None,
                              operation_id=None)
    with pytest.raises(ValueError, match="tabular-row entity sets are read-only"):
        bridge.create_entity("Document_BadMarker_Rows", {"LineNumber": 1})

    assert client.calls == []


def test_write_disabled_even_when_caller_confirms(tmp_path: Path):
    bridge, client = make_bridge(tmp_path, write_enabled=False)
    with pytest.raises(PermissionError, match="writeReadiness.reason"):
        bridge.create_entity("Document_Example", {"Number": "1"}, dry_run=False,
                             confirm=True, expected_target=bridge.target_fingerprint,
                             operation_id="pilot-0001")
    assert client.calls == []


def test_tabular_patch_needs_full_replacement_tool_and_checks_prior_rows(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError, match="replace_tabular_section"):
        bridge.patch_entity("Document_Example", key, {"Lines": [{"LineNumber": 1}]})
    with pytest.raises(ValueError, match="row count changed"):
        bridge.replace_tabular_section("Document_Example", key, "Lines", [],
                                       expected_row_count=0)
    with pytest.raises(ValueError, match="exact field coverage"):
        bridge.replace_tabular_section("Document_Example", key, "Lines",
                                       [{"LineNumber": 1}], expected_row_count=1)
    with pytest.raises(ValueError, match="unique LineNumber"):
        bridge.replace_tabular_section(
            "Document_Example",
            key,
            "Lines",
            [
                {"LineNumber": 1, "Item_Key": "item-1", "Quantity": "1.000"},
                {"LineNumber": 1, "Item_Key": "item-2", "Quantity": "2.000"},
            ],
            expected_row_count=1,
        )
    preview = bridge.replace_tabular_section("Document_Example", key, "Lines", [],
                                              expected_row_count=1)
    assert preview["dryRun"] is True
    assert preview["previousRowCount"] == 1
    assert preview["replacementRowCount"] == 0
    assert preview["rowValidation"] == {"replacementClearsSection": True}
    exact_preview = bridge.replace_tabular_section(
        "Document_Example",
        key,
        "Lines",
        [{"LineNumber": 1, "Item_Key": "item-1", "Quantity": "2.000"}],
        expected_row_count=1,
    )
    assert exact_preview["rowValidation"] == {
        "replacementRowFields": ["Item_Key", "LineNumber", "Quantity"]
    }
    assert all(call[0] == "get" for call in client.calls)


def test_replace_tabular_section_uses_keyed_404_fallback_for_current_rows(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    _install_keyed_404_with_collection_result(
        client, {"value": [{
            "Ref_Key": key, "DataVersion": "v1",
            "Lines": [dict(row) for row in client.current_lines],
        }]},
    )

    result = bridge.replace_tabular_section(
        "Document_Example", key, "Lines", [], expected_row_count=1,
    )

    assert result["dryRun"] is True
    assert result["previousRowCount"] == 1
    assert result["replacementRowCount"] == 0
    assert len([call for call in client.calls if call[0] == "list"]) == 1
    assert not any(call[0] == "count" for call in client.calls)
    assert not any(call[0] == "patch" for call in client.calls)


def test_replace_tabular_section_accepts_odata_int64_strings_and_sends_strings(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    client.current_lines = [
        {"LineNumber": "1", "Item_Key": "item-1", "Quantity": "1.000",
         "Multiplicity": "0"}
    ]

    preview = bridge.replace_tabular_section(
        "Document_Example",
        key,
        "Lines",
        [{"LineNumber": "1", "Item_Key": "item-1", "Quantity": "2.000",
          "Multiplicity": "0"}],
        expected_row_count=1,
    )
    assert preview["dryRun"] is True
    assert preview["rowValidation"] == {
        "replacementRowFields": ["Item_Key", "LineNumber", "Multiplicity", "Quantity"]
    }

    result = bridge.replace_tabular_section(
        "Document_Example",
        key,
        "Lines",
        [{"LineNumber": 1, "Item_Key": "item-1", "Quantity": "2.000",
          "Multiplicity": 0}],
        expected_row_count=1,
        expected_data_version=client.data_version,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="tabular-int64-0001",
    )

    patch_calls = [call for call in client.calls if call[0] == "patch"]
    assert patch_calls[-1][3] == {
        "Lines": [{"LineNumber": "1", "Item_Key": "item-1",
                   "Quantity": "2.000", "Multiplicity": "0"}]
    }
    assert result["previousRowCount"] == 1
    assert result["replacementRowCount"] == 1


def test_replace_tabular_section_normalizes_enum_shorthand_before_field_coverage(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    client.current_lines = [
        {
            "LineNumber": "1",
            "Item_Key": "item-1",
            "Quantity": "1.000",
            "СубконтоДт1": "Налог",
            "СубконтоДт1_Type": "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
        }
    ]

    result = bridge.replace_tabular_section(
        "Document_Example",
        key,
        "Lines",
        [
            {
                "LineNumber": 1,
                "Item_Key": "item-1",
                "Quantity": "2.000",
                "СубконтоДт1": {
                    "kind": "EnumRef",
                    "enum": "ВидыПлатежейВГосБюджет",
                    "member": "Налог",
                },
            }
        ],
        expected_row_count=1,
        expected_data_version=client.data_version,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="tabular-enum-0001",
    )

    assert result["rowValidation"] == {
        "replacementRowFields": [
            "Item_Key",
            "LineNumber",
            "Quantity",
            "СубконтоДт1",
            "СубконтоДт1_Type",
        ]
    }
    patch_calls = [call for call in client.calls if call[0] == "patch"]
    assert patch_calls[-1][3] == {
        "Lines": [
            {
                "LineNumber": "1",
                "Item_Key": "item-1",
                "Quantity": "2.000",
                "СубконтоДт1": "Налог",
                "СубконтоДт1_Type": "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
            }
        ]
    }


def test_post_and_unpost_are_explicit_and_check_dataversion(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    preview = bridge.unpost_document("Document_Example", key)
    assert preview["dryRun"] is True
    assert preview["plan"]["operation"] == "Unpost"
    assert client.calls == []

    with pytest.raises(ValueError, match="expected_data_version"):
        bridge.post_document("Document_Example", key, dry_run=False, confirm=True,
                             expected_target=bridge.target_fingerprint,
                             operation_id="pilot-post-0001")
    assert client.calls == []

    with pytest.raises(ValueError, match="DataVersion changed"):
        bridge.post_document("Document_Example", key, dry_run=False, confirm=True,
                             expected_target=bridge.target_fingerprint,
                             operation_id="pilot-post-0001",
                             expected_data_version="stale")
    assert client.calls == [("get", "Document_Example", key)]
    client.calls.clear()

    result = bridge.post_document("Document_Example", key, dry_run=False,
                                  confirm=True,
                                  expected_target=bridge.target_fingerprint,
                                  operation_id="pilot-post-0001",
                                  expected_data_version="v1")
    assert result["action"] == "Post"
    assert "error" not in result
    assert [call[0] for call in client.calls] == ["get", "action", "get"]
    assert client.calls[1] == ("action", "Document_Example", key, "Post", None)

    client.calls.clear()
    client.data_version = "v2"
    replay = bridge.post_document("Document_Example", key, dry_run=False,
                                  confirm=True,
                                  expected_target=bridge.target_fingerprint,
                                  operation_id="pilot-post-0001",
                                  expected_data_version="v1")
    assert replay["replayedReceipt"] is True
    assert client.calls == []


def test_post_can_verify_expected_accounting_movements(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    expected_movements = {
        "register": "AccountingRegister_Example",
        "amount": "90",
        "debit": "10000000-0000-0000-0000-000000000051",
        "credit": "10000000-0000-0000-0000-000000000911",
    }

    preview = bridge.post_document(
        "Document_Example",
        key,
        expected_accounting_movements=expected_movements,
    )
    assert preview["dryRun"] is True
    assert preview["plan"]["accountingMovementVerification"] == {
        "requested": True,
        "register": "AccountingRegister_Example",
        "expectedRows": [{
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000911",
        }],
        "status": "planned",
    }
    assert client.calls == []

    result = bridge.post_document(
        "Document_Example",
        key,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-post-move-ok-0001",
        expected_data_version="v1",
        expected_accounting_movements=expected_movements,
    )

    assert result["ok"] is True
    assert result["writeState"] == "accepted_verified"
    assert result["accountingMovementsVerified"] is True
    assert result["accountingMovementVerification"]["status"] == "verified"
    assert result["accountingMovementVerification"]["recorderType"] == "StandardODATA.Document_Example"
    assert [call[0] for call in client.calls] == ["get", "action", "get", "get-register"]
    assert client.calls[-1] == (
        "get-register",
        "AccountingRegister_Example",
        key,
        "StandardODATA.Document_Example",
    )


@pytest.mark.parametrize(
    ("movement_rows", "verified"),
    [
        ([{"Active": True, "AccountDr_Key": "60.1", "AccountCr_Key": "51", "Сумма": 90}], True),
        ([{"Active": True, "AccountDr_Key": "60.2", "AccountCr_Key": "51", "Сумма": 90}], True),
        ([{"Active": True, "AccountDr_Key": "60.2", "AccountCr_Key": "51", "Сумма": 89}], False),
        ([{"Active": True, "AccountDr_Key": "60.2", "AccountCr_Key": "52", "Сумма": 90}], False),
        ([{"Active": False, "AccountDr_Key": "60.2", "AccountCr_Key": "51", "Сумма": 90}], False),
        ([], False),
        ([{"Active": True, "AccountDr_Key": "60.1", "AccountCr_Key": "51", "Сумма": 45},
          {"Active": True, "AccountDr_Key": "60.2", "AccountCr_Key": "51", "Сумма": 45}], False),
        ([{"Active": True, "AccountDr_Key": "60.2", "AccountCr_Key": "51", "Сумма": 90},
          {"Active": True, "AccountDr_Key": "99", "AccountCr_Key": "51", "Сумма": 1}], False),
    ],
)
def test_supplier_debit_alternative_requires_one_exact_active_movement(
    tmp_path: Path, movement_rows, verified,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def record_set(entity_set, recorder, recorder_type):
        client.calls.append(("get-register", entity_set, recorder, recorder_type))
        return {"RecordSet": movement_rows}

    client.get_register_record_set = record_set
    expectation = {
        "register": "AccountingRegister_Example",
        "rows": [{"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
    }
    result = bridge.post_document(
        "Document_Example", key, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="synthetic-supplier-alternative",
        expected_data_version="v1", expected_accounting_movements=expectation,
    )
    assert result["ok"] is verified
    assert result["accountingMovementVerification"]["status"] == (
        "verified" if verified else "mismatch"
    )
    assert result["accountingMovementVerification"]["checks"][0]["expected"] == {
        "amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51",
    }


def test_supplier_debit_alternative_matches_guid_readback_with_different_case(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    debit = "abcdefab-0000-0000-0000-000000000001"
    credit = "abcdefab-0000-0000-0000-000000000051"
    client.get_register_record_set = lambda *_: {"RecordSet": [{
        "Active": True, "AccountDr_Key": debit.upper(),
        "AccountCr_Key": credit.upper(), "Сумма": 90,
    }]}
    result = bridge.post_document(
        "Document_Example", key, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="synthetic-supplier-guid-case",
        expected_data_version="v1", expected_accounting_movements={
            "register": "AccountingRegister_Example",
            "rows": [{"amount": "90", "debitAnyOf": [
                "abcdefab-0000-0000-0000-000000000002", debit,
            ], "credit": credit}],
        },
    )
    assert result["accountingMovementVerification"]["status"] == "verified"


@pytest.mark.parametrize("alternatives", [
    [], ["60.1", "60.1"], [""], ["60.1", " "], ["60.1", 60.2],
    ["60.1", True], ["60.1", {"code": "60.2"}], "60.1", None,
    ["abcdefab-0000-0000-0000-000000000001",
     "ABCDEFAB-0000-0000-0000-000000000001"],
])
def test_supplier_debit_alternative_rejects_malformed_values(alternatives):
    with pytest.raises(ValueError, match="debitAnyOf"):
        server_module._normalize_movement_expectation({
            "register": "AccountingRegister_Example",
            "rows": [{"amount": "90", "debitAnyOf": alternatives, "credit": "51"}],
        })


def test_supplier_debit_alternative_rejects_scalar_debit_and_top_level():
    with pytest.raises(ValueError, match="debitAnyOf"):
        server_module._normalize_movement_expectation({
            "register": "AccountingRegister_Example",
            "rows": [{"amount": "90", "debit": "60.1",
                      "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
        })
    with pytest.raises(ValueError, match="debitAnyOf"):
        server_module._normalize_movement_expectation({
            "register": "AccountingRegister_Example", "amount": "90",
            "debitAnyOf": ["60.1", "60.2"], "credit": "51",
        })


@pytest.mark.parametrize("alternative_first", [True, False])
def test_supplier_debit_alternative_rejects_multirow_expectation(alternative_first):
    alternative = {"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}
    overlapping_scalar = {"amount": "90", "debit": "60.1", "credit": "51"}
    rows = [alternative, overlapping_scalar] if alternative_first else [overlapping_scalar, alternative]
    with pytest.raises(ValueError, match="debitAnyOf.*one expected movement row"):
        server_module._normalize_movement_expectation({
            "register": "AccountingRegister_Example", "rows": rows,
        })


def test_scalar_multirow_movement_expectation_remains_valid():
    normalized = server_module._normalize_movement_expectation({
        "register": "AccountingRegister_Example",
        "rows": [
            {"amount": "40", "debit": "60.1", "credit": "51"},
            {"amount": "50", "debit": "60.2", "credit": "51"},
        ],
    })
    assert len(normalized["rows"]) == 2


@pytest.mark.parametrize("relaxation", [
    {"allowExtraRows": True}, {"tolerance": "0.01"},
])
def test_supplier_debit_alternative_rejects_relaxed_check(relaxation):
    with pytest.raises(ValueError, match="debitAnyOf"):
        server_module._normalize_movement_expectation({
            "register": "AccountingRegister_Example",
            "rows": [{"amount": "90", "debitAnyOf": ["60.1", "60.2"], "credit": "51"}],
            **relaxation,
        })


def test_supplier_debit_alternative_is_bound_to_post_replay_hash(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    expectation = {
        "register": "AccountingRegister_Example",
        "rows": [{"amount": "90", "debitAnyOf": [
            "10000000-0000-0000-0000-000000000051",
            "10000000-0000-0000-0000-000000000052",
        ], "credit": "10000000-0000-0000-0000-000000000911"}],
    }
    args = {
        "dry_run": False, "confirm": True,
        "expected_target": bridge.target_fingerprint,
        "operation_id": "synthetic-supplier-replay",
        "expected_data_version": "v1",
    }
    first = bridge.post_document("Document_Example", key, **args,
                                 expected_accounting_movements=expectation)
    calls = list(client.calls)
    replay = bridge.post_document("Document_Example", key, **args,
                                  expected_accounting_movements=expectation)
    assert first["ok"] is True
    assert replay["replayedReceipt"] is True
    assert client.calls == calls
    changed = {**expectation, "rows": [{
        **expectation["rows"][0],
        "debitAnyOf": ["10000000-0000-0000-0000-000000000052"],
    }]}
    with pytest.raises(ValueError, match="different request"):
        bridge.post_document("Document_Example", key, **args,
                             expected_accounting_movements=changed)
    assert client.calls == calls


def test_post_movement_mismatch_is_visible_structured_result_after_post(tmp_path: Path,
                                                                        monkeypatch):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)
    get_entity = client.get_entity

    def posted_readback(entity_set, entity_key):
        entity = get_entity(entity_set, entity_key)
        entity["Posted"] = True
        return entity

    client.get_entity = posted_readback

    result = server_module.onec83_post_document(
        entity_set="Document_Example",
        key=key,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-post-move-mismatch-0001",
        expected_data_version="v1",
        expected_accounting_movements={
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000912",
        },
    )

    assert result["ok"] is False
    assert result["readback"]["Posted"] is True
    assert result["writeState"] == "accepted_movement_mismatch"
    assert result["error"]["kind"] == "accountingMovementVerification"
    assert result["error"]["code"] == "accepted_movement_mismatch"
    assert "1C accepted Post" in result["error"]["message"]
    assert "do not repeat Post automatically" in result["error"]["message"]
    assert result["accountingMovementVerification"]["status"] == "mismatch"
    assert "no active accounting movement" in repr(result)
    assert [call[0] for call in client.calls] == ["get", "action", "get", "get-register"]
    assert bridge.ledger.inspect("pilot-post-move-mismatch-0001")["state"] == "complete"


def test_create_dry_run_unknown_field_is_visible_without_sending_write(tmp_path: Path,
                                                                        monkeypatch):
    bridge, client = make_bridge(tmp_path)
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)

    result = server_module.onec83_create_entity(
        entity_set="Catalog_Example",
        payload={"Комментарий": "PASSWORD-SENTINEL"},
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    assert "not published" in result["error"]["message"]
    assert "Комментарий" in result["error"]["message"]
    assert "PASSWORD-SENTINEL" not in repr(result)
    assert client.calls == []


def test_post_movement_verification_rejects_extra_active_rows_by_default(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def get_with_extra_row(entity_set, recorder, recorder_type):
        client.calls.append(("get-register", entity_set, recorder, recorder_type))
        return {
            "Recorder": recorder,
            "Recorder_Type": recorder_type,
            "RecordSet": [
                {
                    "Active": True,
                    "AccountDr_Key": "10000000-0000-0000-0000-000000000051",
                    "AccountCr_Key": "10000000-0000-0000-0000-000000000911",
                    "Сумма": 90.0,
                },
                {
                    "Active": True,
                    "AccountDr_Key": "20000000-0000-0000-0000-000000000001",
                    "AccountCr_Key": "20000000-0000-0000-0000-000000000002",
                    "Сумма": 1.0,
                },
            ],
        }

    client.get_register_record_set = get_with_extra_row

    result = bridge.post_document(
        "Document_Example",
        key,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-post-move-extra-0001",
        expected_data_version="v1",
        expected_accounting_movements={
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000911",
        },
    )

    assert result["ok"] is False
    assert result["writeState"] == "accepted_movement_mismatch"
    assert result["accountingMovementVerification"]["status"] == "mismatch"
    assert result["accountingMovementVerification"]["extraRows"][0]["amount"] == "1"
    assert "extra active" in result["accountingMovementVerification"]["diagnostics"][0]


def test_post_movement_allow_extra_rows_must_be_boolean(tmp_path: Path):
    bridge, _client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    with pytest.raises(ValueError, match="allowExtraRows"):
        bridge.post_document(
            "Document_Example",
            key,
            expected_accounting_movements={
                "register": "AccountingRegister_Example",
                "amount": "90",
                "debit": "10000000-0000-0000-0000-000000000051",
                "credit": "10000000-0000-0000-0000-000000000911",
                "allowExtraRows": "false",
            },
        )


def test_post_movement_verification_unavailable_does_not_claim_success(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def register_unavailable(entity_set, recorder, recorder_type):
        client.calls.append(("get-register", entity_set, recorder, recorder_type))
        raise ODataHTTPError(500, code="filter", message="register unavailable")

    client.get_register_record_set = register_unavailable

    operation_id = "pilot-post-move-unavailable-0001"
    result = bridge.post_document(
        "Document_Example",
        key,
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id=operation_id,
        expected_data_version="v1",
        expected_accounting_movements={
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000911",
        },
    )

    assert result["ok"] is False
    assert result["writeState"] == "accepted_movement_unavailable"
    assert result["error"]["kind"] == "accountingMovementVerification"
    assert result["error"]["code"] == "accepted_movement_unavailable"
    assert "1C accepted Post" in result["error"]["message"]
    assert result["error"]["details"]["reason"] == "OData HTTP 500 code filter: register unavailable"
    assert result["accountingMovementsVerified"] is False
    assert result["accountingMovementVerification"]["status"] == "unavailable"
    assert "register unavailable" in result["accountingMovementVerification"]["reason"]
    receipt = bridge.get_write_receipt(operation_id)
    assert receipt["result"]["error"] == result["error"]
    calls_after_post = list(client.calls)
    replay = bridge.post_document(
        "Document_Example", key, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, operation_id=operation_id,
        expected_data_version="v1",
        expected_accounting_movements={
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000911",
        },
    )
    assert replay["replayedReceipt"] is True
    assert replay["error"] == result["error"]
    assert client.calls == calls_after_post


def test_document_queue_surfaces_movement_failure_and_replays_saved_error(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_bridge(tmp_path)
    original_get = client.get_entity

    def get_created_document(entity_set, key):
        return {**original_get(entity_set, key), "Number": "movement-check"}

    monkeypatch.setattr(client, "get_entity", get_created_document)

    def register_unavailable(entity_set, recorder, recorder_type):
        client.calls.append(("get-register", entity_set, recorder, recorder_type))
        raise ODataHTTPError(500, code="filter", message="register unavailable")

    monkeypatch.setattr(client, "get_register_record_set", register_unavailable)
    item = {
        "entity_set": "Document_Example",
        "payload": {"Number": "movement-check"},
        "operation_id": "batch-movement-create-01",
        "post": True,
        "post_operation_id": "batch-movement-post-01",
        "expected_accounting_movements": {
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": "10000000-0000-0000-0000-000000000911",
        },
    }
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
    }

    first = bridge.apply_document_queue([item], **args)
    row = first["results"][0]
    assert row["ok"] is False
    assert row["state"] == "needs_reconciliation"
    assert row["postState"] == "accepted_movement_unavailable"
    assert row["error"]["code"] == "accepted_movement_unavailable"
    assert first["succeeded"] == 0

    calls_after_post = list(client.calls)
    second = bridge.apply_document_queue([item], **args)
    replayed = second["results"][0]
    assert replayed["ok"] is False
    assert replayed["state"] == "needs_reconciliation"
    assert replayed["postReplayed"] is True
    assert replayed["error"] == row["error"]
    assert client.calls == calls_after_post


@pytest.mark.parametrize("movement_status", ["unavailable", "mismatch"])
def test_document_queue_keeps_movement_error_when_post_readback_fails(
    tmp_path: Path, monkeypatch, movement_status: str,
):
    bridge, client = make_bridge(tmp_path)
    original_get = client.get_entity
    original_action = client.invoke_action
    post_accepted = False

    def action_then_readback_failure(entity_set, key, action, if_match=None):
        nonlocal post_accepted
        original_action(entity_set, key, action, if_match)
        post_accepted = True

    def get_with_failed_post_readback(entity_set, key):
        if post_accepted:
            client.calls.append(("get", entity_set, key))
            raise ODataClientError("synthetic post readback failure")
        return {**original_get(entity_set, key),
                "Number": "post-readback-and-movement-failure"}

    monkeypatch.setattr(client, "invoke_action", action_then_readback_failure)
    monkeypatch.setattr(client, "get_entity", get_with_failed_post_readback)
    if movement_status == "unavailable":
        def register_unavailable(entity_set, recorder, recorder_type):
            client.calls.append(("get-register", entity_set, recorder, recorder_type))
            raise ODataHTTPError(500, code="filter", message="synthetic register failure")

        monkeypatch.setattr(client, "get_register_record_set", register_unavailable)

    item = {
        "entity_set": "Document_Example",
        "payload": {"Number": "post-readback-and-movement-failure"},
        "operation_id": "batch-dual-failure-create-01",
        "post": True,
        "post_operation_id": "batch-dual-failure-post-01",
        "expected_accounting_movements": {
            "register": "AccountingRegister_Example",
            "amount": "90",
            "debit": "10000000-0000-0000-0000-000000000051",
            "credit": (
                "10000000-0000-0000-0000-000000000912"
                if movement_status == "mismatch"
                else "10000000-0000-0000-0000-000000000911"
            ),
        },
    }
    args = {"dry_run": False, "confirm": True,
            "expected_target": bridge.target_fingerprint}

    first = bridge.apply_document_queue([item], **args)
    row = first["results"][0]
    receipt = bridge.get_write_receipt(item["post_operation_id"])["result"]
    expected_code = f"accepted_movement_{movement_status}"
    assert receipt["readbackError"] == "synthetic post readback failure"
    assert receipt["error"]["code"] == expected_code
    assert row["ok"] is False
    assert row["state"] != "posted"
    assert row["postReadbackFetched"] is False
    assert row["error"] == receipt["error"]
    assert row["postState"] == expected_code

    calls_after_post = list(client.calls)
    replay = bridge.apply_document_queue([item], **args)["results"][0]
    assert replay["postReplayed"] is True
    assert replay["error"] == row["error"]
    assert replay["postState"] == expected_code
    assert client.calls == calls_after_post


def test_stale_action_version_does_not_reserve_operation_id(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    args = {
        "dry_run": False,
        "confirm": True,
        "expected_target": bridge.target_fingerprint,
        "operation_id": "pilot-post-stale-0001",
    }
    with pytest.raises(ValueError, match="DataVersion changed"):
        bridge.post_document("Document_Example", key,
                             expected_data_version="old", **args)
    assert [call[0] for call in client.calls] == ["get"]
    client.calls.clear()
    result = bridge.post_document("Document_Example", key,
                                  expected_data_version="v1", **args)
    assert result["action"] == "Post"
    assert [call[0] for call in client.calls] == ["get", "action", "get"]


def test_uncertain_create_is_not_retried(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def interrupted(*args, **kwargs):
        client.calls.append(("create",))
        raise TimeoutError("response lost")

    client.create_entity = interrupted
    kwargs = dict(dry_run=False, confirm=True,
                  expected_target=bridge.target_fingerprint,
                  operation_id="pilot-uncertain-0001")
    with pytest.raises(TimeoutError):
        bridge.create_entity("Document_Example", {"Number": "1"}, **kwargs)
    with pytest.raises(UncertainWrite):
        bridge.create_entity("Document_Example", {"Number": "1"}, **kwargs)
    assert client.calls == [("create",)]


def test_definite_create_failure_surfaces_1c_message_and_replays_receipt(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    calls = []

    def rejected(*args, **kwargs):
        calls.append("create")
        raise ODataHTTPError(400, code="5", message="Не заполнена статья ДДС", definitive=True)

    client.create_entity = rejected
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-error-0001")
    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)

    assert result["ok"] is False
    assert result["writeState"] == "failed"
    assert result["error"]["httpStatus"] == 400
    assert result["error"]["code"] == "5"
    assert "Не заполнена статья ДДС" in result["error"]["message"]
    assert bridge.ledger.inspect(args["operation_id"])["state"] == "failed"
    assert bridge.create_entity("Document_Example", {"Number": "1"}, **args)["replayedReceipt"] is True
    assert calls == ["create"]


def test_uncertain_transport_create_preserves_pending_and_explains_it(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def timed_out(*args, **kwargs):
        raise ODataTransportError("timeout", attempts=1, subreason="timeout_read")

    client.create_entity = timed_out
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-timeout-0001")
    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)

    assert result["ok"] is False
    assert result["writeState"] == "pending"
    assert "timeout" in result["error"]["message"]
    assert result["error"]["category"] == "timeout"
    assert result["error"]["subreason"] == "timeout_read"
    assert bridge.ledger.inspect(args["operation_id"])["state"] == "pending"
    assert bridge.get_write_receipt(args["operation_id"])["result"] == result
    with pytest.raises(UncertainWrite):
        bridge.create_entity("Document_Example", {"Number": "1"}, **args)


def test_http_500_create_is_pending_result_and_receipt_is_inspectable(tmp_path: Path,
                                                                     monkeypatch):
    bridge, client = make_bridge(tmp_path)

    def rejected(*args, **kwargs):
        client.calls.append(("create",))
        raise ODataHTTPError(500, code="Internal",
                             message="Общая ошибка создания",
                             definitive=False)

    client.create_entity = rejected
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)
    args = dict(entity_set="Document_Example", payload={"Number": "1"},
                dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-http500-0001")

    result = server_module.onec83_create_entity(**args)
    assert result["ok"] is False
    assert result["writeState"] == "pending"
    assert result["error"]["httpStatus"] == 500
    assert "Общая ошибка создания" in result["error"]["message"]

    receipt = server_module.onec83_get_write_receipt(args["operation_id"])
    assert receipt["state"] == "pending"
    assert receipt["result"]["error"]["httpStatus"] == 500
    assert receipt["result"]["error"]["code"] == "Internal"
    repeat = server_module.onec83_create_entity(**args)
    assert repeat["ok"] is False
    assert repeat["error"]["kind"] == "uncertainWrite"
    assert "previous write" in repeat["error"]["message"]
    assert "Saved pending diagnostic" in repeat["error"]["message"]
    assert "writeState=pending" in repeat["error"]["message"]
    assert "httpStatus=500" in repeat["error"]["message"]
    assert "Общая ошибка создания" in repeat["error"]["message"]
    assert client.calls == [("create",)]


def _pending_post_reconciliation_fixture(tmp_path: Path):
    class RegisterMetadata(FakeMetadata):
        def describe_entity_set(self, name):
            description = super().describe_entity_set(name)
            if name == "AccountingRegister_Example":
                description["properties"] = [
                    {"name": "Recorder"}, {"name": "Recorder_Type"},
                ]
            return description

    bridge, client = make_bridge(tmp_path)
    bridge._metadata = RegisterMetadata()
    key = "00000000-0000-0000-0000-000000000001"
    register_rows = [
        {"Recorder": "00000000-0000-0000-0000-000000000002",
         "Recorder_Type": "StandardODATA.Document_Example"},
        {"Recorder": "00000000-0000-0000-0000-000000000003",
         "Recorder_Type": "StandardODATA.Document_Example"},
    ]
    doc = {"Ref_Key": key, "DataVersion": "v1", "Posted": False,
           "DeletionMark": False}

    def read(entity_set, read_key):
        client.calls.append(("get", entity_set, read_key))
        return dict(doc)

    def count(entity_set):
        client.calls.append(("count", entity_set))
        return len(register_rows)

    def list_rows(entity_set, top, skip, **kwargs):
        client.calls.append(("list", entity_set, top, skip, kwargs))
        return {"value": [dict(row) for row in register_rows[skip:skip + top]]}

    def http_500(*args, **kwargs):
        client.calls.append(("action", args))
        raise ODataHTTPError(500, code="-1", message="synthetic handler refusal",
                             definitive=False)

    client.get_entity = read
    client.count_entities = count
    client.list_entities = list_rows
    client.invoke_action = http_500
    expectation = {
        "register": "AccountingRegister_Example", "amount": "10",
        "debit": "synthetic-debit", "credit": "synthetic-credit",
    }
    operation_id = "synthetic-pending-post-0001"
    pending = bridge.post_document(
        "Document_Example", key, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, operation_id=operation_id,
        expected_data_version="v1", expected_accounting_movements=expectation,
    )
    assert pending["writeState"] == "pending"
    assert pending["error"]["httpStatus"] == 500
    args = dict(
        operation_id=operation_id, entity_set="Document_Example", key=key,
        expected_data_version="v1", expected_target=bridge.target_fingerprint,
        expected_accounting_movements=expectation, page_size=1,
    )
    return bridge, client, doc, register_rows, args


def test_reconcile_pending_post_closes_only_local_receipt_and_preserves_error(tmp_path: Path):
    bridge, client, doc, rows, args = _pending_post_reconciliation_fixture(tmp_path)
    calls_before = len(client.calls)
    preview = bridge.reconcile_pending_post(**args)
    assert preview["plan"]["writeState"] == "pending"
    assert preview["plan"]["reconciled"] is False
    assert preview["plan"]["proposedWriteState"] == "reconciled_observed_unposted"
    assert bridge.get_write_receipt(args["operation_id"])["state"] == "pending"
    result = bridge.reconcile_pending_post(**args, dry_run=False, confirm=True)
    assert result["ok"] is False
    assert "synthetic handler refusal" in result["originalReceipt"]["error"]["message"]
    assert result["observation"]["registerRowsScanned"] == 2
    assert bridge.get_write_receipt(args["operation_id"])["state"] == "reconciled"
    replay = bridge.post_document(
        args["entity_set"], args["key"], dry_run=False, confirm=True,
        expected_target=args["expected_target"], operation_id=args["operation_id"],
        expected_data_version=args["expected_data_version"],
        expected_accounting_movements=args["expected_accounting_movements"],
    )
    assert replay["replayedReceipt"] is True
    assert replay["writeState"] == "reconciled_observed_unposted"
    assert all(call[0] not in {"action", "patch", "create"} for call in client.calls[calls_before:])


def test_reconcile_pending_post_uses_exact_fallback_for_both_document_reads(
    tmp_path: Path,
):
    bridge, client, doc, _rows, args = _pending_post_reconciliation_fixture(tmp_path)
    calls_before = len(client.calls)
    original_list = client.list_entities
    fallback_calls = []

    def missing_get(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        raise ODataHTTPError(404, code="9", message="entity instance not found")

    def exact_doc_or_register_page(entity_set, top, skip, filter=None, select=None,
                                   orderby=None, expand=None):
        if filter is not None:
            assert filter == f"Ref_Key eq guid'{args['key']}'"
            assert top == 2 and skip == 0
            fallback_calls.append(filter)
            client.calls.append(("list", entity_set, top, skip, filter,
                                 select, orderby, expand))
            return {"value": [dict(doc)]}
        return original_list(entity_set, top, skip, select=select, orderby=orderby)

    client.get_entity = missing_get
    client.list_entities = exact_doc_or_register_page

    result = bridge.reconcile_pending_post(**args, dry_run=False, confirm=True)

    assert result["writeState"] == "reconciled_observed_unposted"
    assert result["observation"]["registerRowsScanned"] == 2
    assert len(fallback_calls) == 2
    assert not any(call[0] == "count" and call[1] == "Document_Example"
                   for call in client.calls[calls_before:])
    assert all(call[0] not in {"action", "patch", "create"}
               for call in client.calls[calls_before:])


@pytest.mark.parametrize("change", [
    "posted", "deleted", "version", "missing_document_key",
    "posted_during_scan", "version_during_scan", "missing_final_document_key",
    "matching_row", "duplicate_recorder", "short_page", "next_link",
    "count_change", "page_count_bool", "missing_key", "read_error",
])
def test_reconcile_pending_post_ambiguous_evidence_remains_pending(tmp_path: Path, change: str):
    bridge, client, doc, rows, args = _pending_post_reconciliation_fixture(tmp_path)
    if change == "posted":
        doc["Posted"] = True
    elif change == "deleted":
        doc["DeletionMark"] = True
    elif change == "version":
        doc["DataVersion"] = "v2"
    elif change == "missing_document_key":
        doc.pop("Ref_Key")
    elif change in {"posted_during_scan", "version_during_scan", "missing_final_document_key"}:
        reads = 0
        def changing_document(entity_set, key):
            nonlocal reads
            reads += 1
            client.calls.append(("get", entity_set, key))
            observed = dict(doc)
            if reads == 2:
                if change == "posted_during_scan":
                    observed["Posted"] = True
                elif change == "version_during_scan":
                    observed["DataVersion"] = "v2"
                else:
                    observed.pop("Ref_Key")
            return observed
        client.get_entity = changing_document
    elif change == "matching_row":
        rows[0]["Recorder"] = args["key"]
    elif change == "duplicate_recorder":
        rows[1] = dict(rows[0])
    elif change == "short_page":
        client.list_entities = lambda *a, **kw: {"value": []}
    elif change == "next_link":
        client.list_entities = lambda *a, **kw: {"value": [rows[0]], "next_link": "skiptoken"}
    elif change == "count_change":
        counts = iter([2, 3])
        client.count_entities = lambda *_a: next(counts)
    elif change == "page_count_bool":
        rows.pop()
        client.list_entities = lambda *a, **kw: {"value": [rows[0]], "count": True}
    elif change == "missing_key":
        rows[0].pop("Recorder_Type")
    elif change == "read_error":
        client.list_entities = lambda *a, **kw: (_ for _ in ()).throw(ODataHTTPError(404))
    result = bridge.reconcile_pending_post(**args, dry_run=False, confirm=True)
    assert result["writeState"] == "pending"
    assert result["reconciled"] is False
    assert bridge.get_write_receipt(args["operation_id"])["state"] == "pending"


def test_reconcile_pending_post_requires_exact_original_request_and_http_response(tmp_path: Path):
    bridge, client, doc, rows, args = _pending_post_reconciliation_fixture(tmp_path)
    with pytest.raises(PermissionError):
        bridge.reconcile_pending_post(**{**args, "expected_target": "wrong-target"})
    with pytest.raises(ValueError, match="request hash"):
        bridge.reconcile_pending_post(**{**args, "key": "00000000-0000-0000-0000-000000000099"})
    with pytest.raises(ValueError, match="request hash"):
        bridge.reconcile_pending_post(**{**args, "expected_data_version": "v2"})
    with pytest.raises(ValueError, match="request hash"):
        bridge.reconcile_pending_post(**{**args, "expected_accounting_movements": {
            "register": "AccountingRegister_Example", "amount": "11",
            "debit": "synthetic-debit", "credit": "synthetic-credit",
        }})
    receipt = bridge.ledger.inspect(args["operation_id"])
    altered = dict(receipt["result"])
    altered["error"] = {"kind": "odataTransport", "message": "timeout"}
    bridge.ledger.record_pending(args["operation_id"], receipt["request_hash"], altered)
    with pytest.raises(ValueError, match="HTTP 5xx"):
        bridge.reconcile_pending_post(**args)


def test_reconcile_pending_post_accepts_legacy_receipt_without_redundant_version(
    tmp_path: Path,
):
    bridge, _client, _doc, _rows, args = _pending_post_reconciliation_fixture(tmp_path)
    receipt = bridge.ledger.inspect(args["operation_id"])
    legacy_result = dict(receipt["result"])
    legacy_result.pop("expectedDataVersion")
    bridge.ledger.record_pending(
        args["operation_id"], receipt["request_hash"], legacy_result,
    )

    with pytest.raises(ValueError, match="request hash"):
        bridge.reconcile_pending_post(
            **{**args, "expected_data_version": "v2"},
        )

    preview = bridge.reconcile_pending_post(**args)
    assert preview["canFinalize"] is True
    result = bridge.reconcile_pending_post(**args, dry_run=False, confirm=True)
    assert result["writeState"] == "reconciled_observed_unposted"
    assert "expectedDataVersion" not in result["originalReceipt"]
    assert bridge.get_write_receipt(args["operation_id"])["state"] == "reconciled"


@pytest.mark.parametrize("stored_version", ["v2", None])
def test_reconcile_pending_post_rejects_conflicting_saved_redundant_version(
    tmp_path: Path, stored_version: str | None,
):
    bridge, _client, _doc, _rows, args = _pending_post_reconciliation_fixture(tmp_path)
    receipt = bridge.ledger.inspect(args["operation_id"])
    altered_result = dict(receipt["result"])
    altered_result["expectedDataVersion"] = stored_version
    bridge.ledger.record_pending(
        args["operation_id"], receipt["request_hash"], altered_result,
    )

    with pytest.raises(ValueError, match="HTTP 5xx Post"):
        bridge.reconcile_pending_post(**args)


def test_reconcile_pending_post_without_hash_bound_register_cannot_finalize(tmp_path: Path):
    bridge, client, doc, rows, args = _pending_post_reconciliation_fixture(tmp_path)
    original_id = "synthetic-post-without-expectation-0002"
    bridge.post_document(
        args["entity_set"], args["key"], dry_run=False, confirm=True,
        expected_target=args["expected_target"], operation_id=original_id,
        expected_data_version="v1",
    )
    unbound = {**args, "operation_id": original_id,
               "expected_accounting_movements": None,
               "accounting_register": "AccountingRegister_Example"}
    preview = bridge.reconcile_pending_post(**unbound)
    assert preview["canFinalize"] is False
    result = bridge.reconcile_pending_post(**unbound, dry_run=False, confirm=True)
    assert result["writeState"] == "pending"
    assert bridge.get_write_receipt(original_id)["state"] == "pending"


def test_named_key_register_404_is_unknown_for_known_and_unknown_recorder(
    tmp_path: Path, monkeypatch,
):
    bridge, client = make_bridge(tmp_path)
    def missing(_entity_set, _key, _type):
        client.calls.append(("get-register",))
        raise ODataHTTPError(404, code="9", message="synthetic record set not found",
                             definitive=True)
    client.get_register_record_set = missing
    keys = ["00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000099"]
    results = [bridge.get_accounting_register_record_set(
        "AccountingRegister_Example", key, recorder_entity_set="Document_Example"
    ) for key in keys]
    assert all(result["complete"] is False for result in results)
    assert all(result["ok"] is False for result in results)
    assert all(result["absenceStatus"] == "unknown" for result in results)
    assert all(result["error"]["httpStatus"] == 404 for result in results)
    assert all("count" not in result for result in results)
    assert client.calls == [("get-register",), ("get-register",)]
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)
    wrapped = server_module.onec83_get_accounting_register_record_set(
        "AccountingRegister_Example", keys[0], recorder_entity_set="Document_Example",
    )
    assert wrapped["ok"] is False
    call = PerformanceCall(PerformanceLogger(enabled=False),
                           "onec83_get_accounting_register_record_set")
    call.observe_result(wrapped)
    assert call.outcome == "failed"


def test_queue_does_not_treat_reconciled_post_as_posted(tmp_path: Path):
    bridge, client, doc, rows, args = _pending_post_reconciliation_fixture(tmp_path)
    doc["Number"] = "1"
    create_id = "synthetic-create-for-reconcile-0001"
    bridge.create_entity(
        "Document_Example", {"Number": "1"}, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, operation_id=create_id,
    )
    bridge.reconcile_pending_post(**args, dry_run=False, confirm=True)
    calls_before = len(client.calls)
    queued = bridge.apply_document_queue([{
        "entity_set": "Document_Example", "payload": {"Number": "1"},
        "operation_id": create_id, "post": True,
        "post_operation_id": args["operation_id"],
        "expected_accounting_movements": args["expected_accounting_movements"],
    }], dry_run=False, confirm=True, expected_target=bridge.target_fingerprint)
    row = queued["results"][0]
    assert row["ok"] is False
    assert row["state"] == "failed"
    assert row["nextAction"] == "needs_new_post_operation"
    assert row["postState"] == "reconciled_observed_unposted"
    assert queued["succeeded"] == 0
    assert all(call[0] not in {"action", "create", "patch"} for call in client.calls[calls_before:])
    wrong = bridge.apply_document_queue([{
        "entity_set": "Document_Example", "payload": {"Number": "1"},
        "operation_id": create_id, "post": True,
        "post_operation_id": args["operation_id"],
        "expected_accounting_movements": {
            "register": "AccountingRegister_Example", "amount": "11",
            "debit": "synthetic-debit", "credit": "synthetic-credit",
        },
    }], dry_run=False, confirm=True, expected_target=bridge.target_fingerprint)
    assert wrong["results"][0]["ok"] is False
    assert wrong["results"][0].get("nextAction") is None
    assert "different request" in wrong["results"][0]["error"]["message"]


def test_successful_create_with_failed_readback_keeps_created_key(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def failed_readback(*args, **kwargs):
        raise ODataTransportError("connection", attempts=3)

    client.get_entity = failed_readback
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-readback-0001")
    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)

    assert result["writeState"] == "accepted_unverified"
    assert result["key"] == "00000000-0000-0000-0000-000000000001"
    assert result["readbackFetched"] is False
    assert bridge.ledger.inspect(args["operation_id"])["state"] == "complete"
    assert bridge.create_entity("Document_Example", {"Number": "1"}, **args)["replayedReceipt"] is True
    assert [call[0] for call in client.calls] == ["create"]


def _install_keyed_404_with_collection_result(client, result):
    key = "00000000-0000-0000-0000-000000000001"

    def missing_keyed_get(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        raise ODataHTTPError(404, code="9", message="entity instance not found")

    def exact_filter(entity_set, top, skip, filter=None, select=None,
                     orderby=None, expand=None):
        client.calls.append(("list", entity_set, top, skip, filter, select, orderby, expand))
        assert top == 2
        assert skip == 0
        assert filter == f"Ref_Key eq guid'{key}'"
        return result

    client.get_entity = missing_keyed_get
    client.list_entities = exact_filter
    return key


def test_create_recovers_keyed_404_from_one_exact_collection_lookup(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    _install_keyed_404_with_collection_result(
        client, {"value": [{"Ref_Key": key, "Number": "1", "DataVersion": "v1"}]},
    )
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-keyed-404-0001")

    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)

    assert result["readback"]["Ref_Key"] == key
    assert result["readbackFetched"] is True
    assert result["readbackVerified"] is True
    assert result["readbackVerification"]["status"] == "verified"
    assert result["readbackStrategy"] == "ref_key_filter_after_404"
    assert len([call for call in client.calls if call[0] == "list"]) == 1
    assert not any(call[0] == "count" for call in client.calls)
    receipt = bridge.ledger.inspect(args["operation_id"])["result"]
    assert receipt["readbackFetched"] is True
    assert receipt["readbackVerified"] is True
    assert receipt["readbackVerification"] == result["readbackVerification"]
    assert receipt["readbackStrategy"] == "ref_key_filter_after_404"


def test_direct_get_recovers_keyed_404_with_exact_filter(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = _install_keyed_404_with_collection_result(
        client, {"value": [{"Ref_Key": "00000000-0000-0000-0000-000000000001"}]},
    )

    result = bridge.get_entity("Document_Example", key)

    assert result["Ref_Key"] == key
    assert len([call for call in client.calls if call[0] == "list"]) == 1
    assert not any(call[0] == "count" for call in client.calls)


def test_direct_get_does_not_fallback_for_non_404_errors(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def failed_get(entity_set, key):
        client.calls.append(("get", entity_set, key))
        raise ODataHTTPError(500, code="Internal", message="synthetic failure")

    client.get_entity = failed_get
    with pytest.raises(ODataHTTPError) as raised:
        bridge.get_entity("Document_Example", "00000000-0000-0000-0000-000000000001")

    assert raised.value.status_code == 500
    assert not any(call[0] == "list" for call in client.calls)


@pytest.mark.parametrize(
    "fallback_result, expected_reason",
    [
        ({"value": []}, "no_match"),
        (ODataTransportError("synthetic timeout", attempts=1), "filter_request_failed"),
        (ODataHTTPError(500, code="-1", message="filter rejected"), "filter_request_failed"),
        ({"value": [{"Ref_Key": "00000000-0000-0000-0000-000000000001"},
                     {"Ref_Key": "00000000-0000-0000-0000-000000000001"}]},
         "multiple_or_truncated_matches"),
        ({"value": [{"Ref_Key": "00000000-0000-0000-0000-000000000001"}],
          "next_link": "https://example.test/next"}, "multiple_or_truncated_matches"),
        ({"value": [{"Ref_Key": "00000000-0000-0000-0000-000000000002"}]},
         "mismatched_key"),
    ],
)
def test_create_keyed_404_fallback_inconclusive_stays_unverified_without_scan(
    tmp_path: Path, fallback_result, expected_reason,
):
    bridge, client = make_bridge(tmp_path)
    key = _install_keyed_404_with_collection_result(client, {"value": []})

    def exact_filter(entity_set, top, skip, filter=None, select=None,
                     orderby=None, expand=None):
        client.calls.append(("list", entity_set, top, skip, filter, select, orderby, expand))
        if isinstance(fallback_result, Exception):
            raise fallback_result
        return fallback_result

    client.list_entities = exact_filter
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-create-keyed-404-0002")

    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)

    assert result["writeState"] == "accepted_unverified"
    assert result["key"] == key
    assert result["readbackFetched"] is False
    assert result["readbackVerified"] is False
    receipt = bridge.ledger.inspect(args["operation_id"])["result"]
    assert receipt["readbackFallback"]["status"] == "inconclusive"
    assert receipt["readbackFallback"]["reason"] == expected_reason
    assert len([call for call in client.calls if call[0] == "list"]) == 1
    assert not any(call[0] == "count" for call in client.calls)
    assert len([call for call in client.calls if call[0] == "get"]) == 1


def test_patch_and_post_readbacks_share_keyed_404_recovery(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    row = {"Ref_Key": key, "Number": "1", "DataVersion": "v1"}
    original_get = client.get_entity
    post_read_count = 0

    def read_with_post_404(entity_set, requested_key):
        nonlocal post_read_count
        post_read_count += 1
        if post_read_count in {1, 2, 3}:
            raise ODataHTTPError(404, code="9", message="entity instance not found")
        return original_get(entity_set, requested_key)

    client.get_entity = read_with_post_404
    client.list_entities = lambda entity_set, top, skip, filter=None, select=None, orderby=None, expand=None: (
        client.calls.append(("list", entity_set, top, skip, filter, select, orderby, expand))
        or {"value": [row]}
    )
    patch = bridge.patch_entity(
        "Document_Example", key, {"Number": "1"}, dry_run=False,
        confirm=True, expected_target=bridge.target_fingerprint,
        operation_id="pilot-patch-keyed-404-0001", expected_data_version="v1",
    )
    assert patch["readbackFetched"] is True
    assert patch["readbackVerified"] is True
    assert patch["readbackStrategy"] == "ref_key_filter_after_404"
    patch_receipt = bridge.ledger.inspect("pilot-patch-keyed-404-0001")["result"]
    assert patch_receipt["readbackVerified"] is True
    assert patch_receipt["readbackVerification"] == patch["readbackVerification"]

    post = bridge.post_document(
        "Document_Example", key, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="pilot-post-keyed-404-0001", expected_data_version="v1",
    )
    assert post["readbackFetched"] is True
    assert post["readbackVerified"] is False
    assert post["readbackStrategy"] == "ref_key_filter_after_404"
    assert len([call for call in client.calls if call[0] == "list"]) == 3
    assert not any(call[0] == "count" for call in client.calls)


@pytest.mark.parametrize(("payload", "row", "verified", "field_list"), [
    ({"Amount": 1}, {"Amount": "1.000"}, True, "comparedFields"),
    ({"Amount": 1}, {"Amount": "1.01"}, False, "mismatchedFields"),
    ({"Amount": 1}, {"Amount": "NaN"}, False, "mismatchedFields"),
    ({"Amount": None}, {}, False, "missingFields"),
    ({"Amount": None}, {"Amount": None}, True, "comparedFields"),
    ({"DeletionMark": True}, {"DeletionMark": 1}, False, "mismatchedFields"),
    ({"GuidValue": "00000000-0000-0000-0000-000000000001"},
     {"GuidValue": "00000000-0000-0000-0000-000000000001"}, True, "comparedFields"),
    ({"Date": "2010-03-22T12:01:00.1200000001"},
     {"Date": "2010-03-22T12:01:00.1200000001"}, True, "comparedFields"),
    ({"Date": "2010-03-22T12:01:00"},
     {"Date": "2010-03-22T12:01:00Z"}, False, "mismatchedFields"),
    ({"ComplexValue": {"x": 1}}, {"ComplexValue": {"x": 1}}, False,
     "unsupportedFields"),
])
def test_patch_readback_verification_is_strict_and_metadata_aware(
    tmp_path: Path, payload, row, verified, field_list,
):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    def readback(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        return {"Ref_Key": key, **row, "DataVersion": "v2"}
    client.get_entity = readback
    result = bridge.patch_entity(
        "Document_Example", key, payload, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="verify-" + field_list + str(len(client.calls)),
        expected_data_version="v1",
    )
    assert result["readbackVerified"] is verified
    assert result["readbackVerification"][field_list]
    assert len([call for call in client.calls if call[0] == "get"]) == 1
    assert len([call for call in client.calls if call[0] == "patch"]) == 1
    call_count = len(client.calls)
    replay = bridge.patch_entity(
        "Document_Example", key, payload, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint,
        operation_id="verify-" + field_list + "0", expected_data_version="v1",
    )
    assert replay["replayedReceipt"] is True
    assert len(client.calls) == call_count


def test_patch_readback_key_mismatch_keeps_fields_unverified(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    def readback(entity_set, key):
        client.calls.append(("get", entity_set, key))
        return {"Ref_Key": "00000000-0000-0000-0000-000000000002", "Number": "1"}
    client.get_entity = readback
    result = bridge.patch_entity(
        "Document_Example", "00000000-0000-0000-0000-000000000001",
        {"Number": "1"}, dry_run=False, confirm=True,
        expected_target=bridge.target_fingerprint, operation_id="verify-key-mismatch",
        expected_data_version="v1",
    )
    assert result["readbackVerified"] is False
    assert "key_mismatch" in result["readbackVerification"]["reasonCodes"]


def test_tabular_readback_verification_reports_unsupported_without_values():
    report = server_module._readback_verification(
        {"properties": [{"name": "Lines", "type": "Collection(Example.Line)"}],
         "tabularSections": ["Lines"]},
        "00000000-0000-0000-0000-000000000001",
        {"Lines": [{"Amount": "sensitive-value"}]},
        {"Ref_Key": "00000000-0000-0000-0000-000000000001",
         "Lines": [{"Amount": "sensitive-value"}]},
    )
    assert report["status"] == "unverified"
    assert report["unsupportedFields"] == ["Lines"]
    assert report["reasonCodes"] == ["unsupported_field_type"]
    assert "sensitive-value" not in repr(report)


def test_readback_verification_rejects_numeric_identity_even_if_uuid_text_matches():
    numeric_key = 11111111111111111111111111111111
    report = server_module._readback_verification(
        {"properties": [{"name": "Number", "type": "Edm.String"}]},
        "11111111-1111-1111-1111-111111111111", {"Number": "1"},
        {"Ref_Key": numeric_key, "Number": "1"},
    )
    assert report["status"] == "unverified"
    assert "key_missing_or_invalid" in report["reasonCodes"]


@pytest.mark.parametrize(("type_name", "submitted", "actual"), [
    ("Edm.Guid", "00000000-0000-0000-0000-000000000001", 1),
    ("Edm.Decimal", 10, "1_0"),
    ("Edm.Decimal", 10, " 10 "),
])
def test_readback_verification_rejects_coercive_guid_and_numeric_values(
    type_name, submitted, actual,
):
    field = "GuidValue" if type_name == "Edm.Guid" else "Amount"
    report = server_module._readback_verification(
        {"properties": [{"name": field, "type": type_name}], "tabularSections": []},
        "00000000-0000-0000-0000-000000000001", {field: submitted},
        {"Ref_Key": "00000000-0000-0000-0000-000000000001", field: actual},
    )
    assert report["status"] == "unverified"
    assert report["mismatchedFields"] == [field]


def test_accepted_unparseable_create_response_keeps_location_key(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000002"

    def accepted(*args, **kwargs):
        raise ODataAcceptedResponseError(201, key=key)

    client.create_entity = accepted
    result = bridge.create_entity("Document_Example", {"Number": "1"},
                                  dry_run=False, confirm=True,
                                  expected_target=bridge.target_fingerprint,
                                  operation_id="pilot-create-accepted-0001")

    assert result["writeState"] == "accepted_unverified"
    assert result["key"] == key
    assert bridge.ledger.inspect("pilot-create-accepted-0001")["state"] == "complete"


def test_accepted_create_without_key_stays_pending_until_matched(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)

    def accepted(*args, **kwargs):
        raise ODataAcceptedResponseError(201)

    client.create_entity = accepted
    operation_id = "pilot-create-no-key-0001"
    args = dict(dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id=operation_id)
    result = bridge.create_entity("Document_Example", {"Number": "1"}, **args)
    assert result["ok"] is False
    assert result["writeState"] == "accepted_unverified_missing_key"
    assert bridge.get_write_receipt(operation_id)["state"] == "pending"
    assert bridge.get_write_receipt(operation_id)["result"] == result
    with pytest.raises(UncertainWrite):
        bridge.create_entity("Document_Example", {"Number": "1"}, **args)


def test_post_business_failure_is_failed_receipt_and_visible_result(tmp_path: Path,
                                                                    monkeypatch):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"

    def rejected(*args, **kwargs):
        raise ODataHTTPError(400, code="4",
                             message="На период документа не заполнена учетная политика",
                             definitive=True)

    client.invoke_action = rejected
    monkeypatch.setattr(server_module, "_bridge", lambda: bridge)
    args = dict(entity_set="Document_Example", key=key,
                dry_run=False, confirm=True,
                expected_target=bridge.target_fingerprint,
                operation_id="pilot-post-error-0001", expected_data_version="v1")

    result = server_module.onec83_post_document(**args)
    assert result["ok"] is False
    assert result["writeState"] == "failed"
    assert "не заполнена учетная политика" in result["error"]["message"]
    assert bridge.ledger.inspect(args["operation_id"])["state"] == "failed"
    assert server_module.onec83_post_document.__name__ == "onec83_post_document"


def test_reconcile_legacy_pending_create_only_after_bounded_target_search(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    payload = {
        "Number": "1",
        "Ref_Key": "00000000-0000-0000-0000-000000000000",
        "Lines": [{"LineNumber": 1, "Item_Key": "item-1",
                   "Quantity": "1.000", "Multiplicity": 0}],
    }
    operation_id = "pilot-reconcile-create-0001"
    digest = request_digest(bridge.target_fingerprint, "create", "Document_Example",
                            None, payload, None)
    bridge.ledger.reserve(operation_id, digest)
    client.list_entities = lambda *args, **kwargs: {"value": []}
    args = dict(operation_id=operation_id, entity_set="Document_Example",
                payload={"Number": "1",
                         "Lines": [{"LineNumber": 1, "Item_Key": "item-1",
                                    "Quantity": "1.000", "Multiplicity": 0}]},
                filter="Number eq '1'",
                expected_target=bridge.target_fingerprint)

    preview = bridge.reconcile_pending_create(**args)
    assert preview["dryRun"] is True
    assert preview["observedCount"] == 0
    assert preview["writeState"] == "pending"
    assert bridge.get_write_receipt(operation_id)["state"] == "pending"
    with pytest.raises(ValueError, match="absence alone"):
        bridge.reconcile_pending_create(**args, dry_run=False)
    assert bridge.get_write_receipt(operation_id)["state"] == "pending"


def test_reconcile_found_create_checks_key_and_original_identifying_value(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    operation_id = "pilot-reconcile-found-0001"
    payload = {"Number": "1", "Ref_Key": "00000000-0000-0000-0000-000000000000"}
    digest = request_digest(bridge.target_fingerprint, "create", "Document_Example",
                            None, payload, None)
    bridge.ledger.reserve(operation_id, digest)
    client.write_enabled = False  # Recovery reads remain available while 1C writes are disabled.
    args = dict(operation_id=operation_id, entity_set="Document_Example",
                payload={"Number": "1"}, filter="Number eq '1'",
                expected_target=bridge.target_fingerprint, expected_key=key,
                dry_run=False, confirm=True)

    client.get_entity = lambda *args, **kwargs: {"Ref_Key": key, "Number": "other"}
    with pytest.raises(ValueError, match="does not match"):
        bridge.reconcile_pending_create(**args)
    assert bridge.ledger.inspect(operation_id)["state"] == "pending"

    client.get_entity = lambda *args, **kwargs: {"Ref_Key": key, "Number": "1"}
    result = bridge.reconcile_pending_create(**args)
    assert result["writeState"] == "reconciled_present"
    assert result["key"] == key
    assert bridge.ledger.inspect(operation_id)["state"] == "complete"


def test_reconcile_found_create_uses_exact_fallback_after_keyed_404(tmp_path: Path):
    bridge, client = make_bridge(tmp_path)
    key = "00000000-0000-0000-0000-000000000001"
    operation_id = "pilot-reconcile-found-404-0001"
    payload = {
        "Number": "1",
        "Ref_Key": "00000000-0000-0000-0000-000000000000",
    }
    digest = request_digest(bridge.target_fingerprint, "create", "Document_Example",
                            None, payload, None)
    bridge.ledger.reserve(operation_id, digest)
    client.write_enabled = False

    def missing_get(entity_set, requested_key):
        client.calls.append(("get", entity_set, requested_key))
        raise ODataHTTPError(404, code="9", message="entity instance not found")

    def matching_queries(entity_set, top, skip, filter=None, select=None,
                         orderby=None, expand=None):
        client.calls.append(("list", entity_set, top, skip, filter, select, orderby, expand))
        if filter == "Number eq '1'":
            return {"value": [{"Ref_Key": key}]}
        assert filter == f"Ref_Key eq guid'{key}'"
        assert top == 2
        return {"value": [{"Ref_Key": key, "Number": "1"}]}

    client.get_entity = missing_get
    client.list_entities = matching_queries
    args = dict(
        operation_id=operation_id, entity_set="Document_Example",
        payload={"Number": "1"}, filter="Number eq '1'",
        expected_target=bridge.target_fingerprint, expected_key=key,
        dry_run=False, confirm=True,
    )

    result = bridge.reconcile_pending_create(**args)

    assert result["writeState"] == "reconciled_present"
    assert result["readbackFetched"] is True
    assert result["readbackVerified"] is False
    assert len([call for call in client.calls if call[0] == "list"]) == 2
    assert not any(call[0] == "count" for call in client.calls)
    assert not any(call[0] in {"create", "patch", "action"} for call in client.calls)
    assert bridge.ledger.inspect(operation_id)["state"] == "complete"


@pytest.mark.parametrize("dry_run", [True, False])
def test_person_name_observed_ceiling_rejects_before_post_without_value_leak(tmp_path, monkeypatch, dry_run):
    bridge, client = make_bridge(tmp_path)
    original = bridge.metadata().describe_entity_set
    monkeypatch.setattr(bridge.metadata(), "describe_entity_set", lambda name:
        {"name": name, "properties": [{"name": "Ref_Key"}, {"name": "Description"}, {"name": "ФИО"}],
         "isTabularRowEntity": False, "isRegister": False, "supportsPost": False}
        if name == "Catalog_ФизическиеЛица" else original(name))
    private_value = "SYNTHETIC-PERSON-SECRET-" * 4
    with pytest.raises(ValueError, match="ceiling") as error:
        bridge.create_entity("Catalog_ФизическиеЛица", {"Description": private_value}, dry_run=dry_run,
                             confirm=not dry_run, expected_target=bridge.target_fingerprint,
                             operation_id="synthetic-width-reject")
    assert private_value not in str(error.value)
    assert client.calls == []
    assert bridge.ledger.inspect("synthetic-width-reject") is None
