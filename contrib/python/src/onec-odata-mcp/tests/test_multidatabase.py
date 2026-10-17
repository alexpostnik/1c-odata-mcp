from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import json
from threading import Barrier

import pytest

from onec_odata_mcp import server as server_module
from onec_odata_mcp.client import ODataClient, ODataTransportError
from onec_odata_mcp.config import ConfigError, targets_from_json
from onec_odata_mcp.metadata import MetadataModel


def _target(base_url: str, kind: str, service: str, *, writable: bool = False) -> dict:
    return {
        "odata_base_url": base_url,
        "odata_user": "2",
        "keychain_service": service,
        "target_kind": kind,
        "enable_write": writable,
        "write_base_url": base_url if writable else None,
    }


def _config() -> str:
    return json.dumps({
        "test": _target("https://test.example/base/odata/standard.odata/", "test",
                        "onec83-test", writable=True),
        "prod": _target("https://prod.example/base/odata/standard.odata/", "prod",
                        "onec83-prod"),
    })


def test_multi_target_configuration_validates_write_guard_and_prod_read_only():
    targets = targets_from_json(_config())

    assert [target.name for target in targets] == ["test", "prod"]
    assert targets[0].config.write_enabled is True
    assert targets[1].config.write_enabled is False

    unsafe = json.loads(_config())
    unsafe["prod"]["enable_write"] = True
    unsafe["prod"]["write_base_url"] = unsafe["prod"]["odata_base_url"]
    with pytest.raises(ConfigError, match="prod targets must set enable_write=false"):
        targets_from_json(json.dumps(unsafe))

    misspelled = json.loads(_config())
    misspelled["test"]["base_url"] = misspelled["test"].pop("odata_base_url")
    with pytest.raises(ConfigError, match="unsupported fields: base_url"):
        targets_from_json(json.dumps(misspelled))


def test_prod_write_probe_requires_exact_prod_fingerprint_without_enabling_ordinary_writes():
    raw = json.loads(_config())
    prod = raw["prod"]
    fingerprint = hashlib.sha256(prod["odata_base_url"].encode("utf-8")).hexdigest()
    prod.update({
        "prod_write_probe_enabled": True,
        "prod_write_probe_fingerprint": fingerprint,
        "prod_write_probe_id": "0123456789abcdef",
    })

    parsed = targets_from_json(json.dumps(raw))[1]
    assert parsed.config.prod_write_probe_enabled is True
    assert parsed.config.write_enabled is False

    wrong_fingerprint = json.loads(json.dumps(raw))
    wrong_fingerprint["prod"]["prod_write_probe_fingerprint"] = "0" * 64
    with pytest.raises(ConfigError, match="must match the configured target fingerprint"):
        targets_from_json(json.dumps(wrong_fingerprint))

    for field in ("prod_write_probe_fingerprint", "prod_write_probe_id"):
        invalid_type = json.loads(json.dumps(raw))
        invalid_type["prod"][field] = []
        with pytest.raises(ConfigError, match=f"{field} must be a string"):
            targets_from_json(json.dumps(invalid_type))

    test_probe = json.loads(json.dumps(raw))
    test_probe["test"].update({
        "prod_write_probe_enabled": True,
        "prod_write_probe_fingerprint": hashlib.sha256(
            test_probe["test"]["odata_base_url"].encode("utf-8")
        ).hexdigest(),
        "prod_write_probe_id": "0123456789abcdef",
    })
    with pytest.raises(ConfigError, match="allowed only for prod targets"):
        targets_from_json(json.dumps(test_probe))

    non_prod_alias = json.loads(json.dumps(raw))
    non_prod_alias["production"] = non_prod_alias.pop("prod")
    with pytest.raises(ConfigError, match="requires the target alias 'prod'"):
        targets_from_json(json.dumps(non_prod_alias))


PROBE_CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices>
    <Schema Namespace="Demo" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
      <EntityType Name="Catalog_Организации">
        <Key><PropertyRef Name="Ref_Key" /></Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false" />
        <Property Name="Description" Type="Edm.String" />
        <Property Name="DeletionMark" Type="Edm.Boolean" Nullable="false" />
        <Property Name="DataVersion" Type="Edm.String" Nullable="false" />
      </EntityType>
      <EntityContainer Name="DefaultContainer">
        <EntitySet Name="Catalog_Организации" EntityType="Demo.Catalog_Организации" />
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


class FakeProdProbeClient:
    def __init__(self, config):
        self.config = config
        self.write_enabled = False
        self.calls = []
        self.record = None
        self.next_key = "11111111-2222-4333-8444-000000000003"
        self.fail_update = False
        self.omit_create_key = False
        self.get_errors_remaining = 0
        self.mismatch_readback = False

    def list_entities(self, entity_set, *, top, skip, filter=None, select=None,
                      orderby=None, expand=None):
        self.calls.append(("list", entity_set, filter, select))
        if filter is not None:
            raise server_module.ODataHTTPError(
                500,
                code="-1",
                message='Операция не разрешена в предложении "ГДЕ"',
            )
        values = [dict(self.record)] if self.record is not None else []
        return {"value": values[skip:skip + top], "count": len(values), "next_link": None}

    def count_entities(self, entity_set):
        self.calls.append(("count", entity_set))
        return 1 if self.record is not None else 0

    def create_entity(self, entity_set, payload, null_ref_key=False,
                      *, _prod_write_probe_stage=None):
        self.calls.append(("create", entity_set, dict(payload), _prod_write_probe_stage))
        self.record = {
            "Ref_Key": self.next_key,
            "Description": payload["Description"],
            "DeletionMark": False,
            "DataVersion": "version-1",
        }
        if self.omit_create_key:
            return {"Description": self.record["Description"]}
        return {"Ref_Key": self.next_key, "Description": self.record["Description"]}

    def get_entity(self, entity_set, key):
        self.calls.append(("get", entity_set, key))
        if self.get_errors_remaining:
            self.get_errors_remaining -= 1
            raise server_module.ODataHTTPError(503, code="unavailable", message="synthetic readback failure")
        if self.record is None or key != self.record["Ref_Key"]:
            raise AssertionError("unexpected read target")
        result = dict(self.record)
        if self.mismatch_readback:
            result["Description"] = "not the generated probe marker"
        return result

    def patch_entity(self, entity_set, key, payload, if_match=None,
                     *, _prod_write_probe_stage=None):
        self.calls.append(("patch", entity_set, dict(payload), if_match,
                           _prod_write_probe_stage))
        if _prod_write_probe_stage == "update" and self.fail_update:
            raise server_module.ODataHTTPError(
                400, code="validation", message="synthetic update refusal",
                definitive=True,
            )
        if self.record is None or key != self.record["Ref_Key"]:
            raise AssertionError("unexpected update target")
        if if_match != self.record["DataVersion"]:
            raise AssertionError("probe write must use the immediately read DataVersion")
        self.record.update(payload)
        self.record["DataVersion"] = f"version-{len([x for x in self.calls if x[0] == 'patch']) + 1}"
        return {}


def _configured_prod_probe():
    raw = json.loads(_config())
    prod = raw["prod"]
    prod["prod_write_probe_enabled"] = True
    prod["prod_write_probe_fingerprint"] = hashlib.sha256(
        prod["odata_base_url"].encode("utf-8")
    ).hexdigest()
    prod["prod_write_probe_id"] = "0123456789abcdef"
    return targets_from_json(json.dumps(raw))[1]


def test_prod_write_probe_runs_only_create_update_and_soft_delete_with_readback(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    result = bridge.run_prod_write_probe(
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        dry_run_receipt=dry_run["dryRunReceipt"],
    )

    assert result["ok"] is True
    assert result["createVerified"] is True
    assert result["updateVerified"] is True
    assert result["deletionMarkVerified"] is True
    assert result["finalReadback"]["Description"] == "MCP PROD WRITE PROBE 0123456789abcdef UPDATED"
    assert result["finalReadback"]["DeletionMark"] is True
    assert client.write_enabled is False
    writes = [call for call in client.calls if call[0] in {"create", "patch"}]
    assert [call[0] for call in writes] == ["create", "patch", "patch"]
    assert writes[0][1] == "Catalog_Организации"
    assert writes[0][2]["Description"] == "MCP PROD WRITE PROBE 0123456789abcdef"
    assert writes[1][2] == {"Description": "MCP PROD WRITE PROBE 0123456789abcdef UPDATED"}
    assert writes[2][2] == {"DeletionMark": True}
    assert all(call[1] == "Catalog_Организации" for call in writes)
    assert any(call[0] == "count" for call in client.calls)


def test_prod_write_probe_soft_deletes_verified_record_when_description_update_fails(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    client.fail_update = True
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    result = bridge.run_prod_write_probe(
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        dry_run_receipt=dry_run["dryRunReceipt"],
    )

    assert result["ok"] is False
    assert result["updateVerified"] is False
    assert result["deletionMarkVerified"] is True
    assert result["finalReadback"]["Description"] == "MCP PROD WRITE PROBE 0123456789abcdef"
    assert result["finalReadback"]["DeletionMark"] is True
    assert "cleanupError" not in result


@pytest.mark.parametrize("recovery_mode", ["missing_create_key", "readback_failure"])
def test_prod_write_probe_recovers_exact_marker_and_soft_deletes_record(tmp_path, recovery_mode):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    if recovery_mode == "missing_create_key":
        client.omit_create_key = True
    else:
        # Fail both the create helper's immediate readback and the probe's first readback.
        client.get_errors_remaining = 2
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / f"prod-ledger-{recovery_mode}.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    result = bridge.run_prod_write_probe(
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        dry_run_receipt=dry_run["dryRunReceipt"],
    )

    assert result["ok"] is True
    assert result["createVerified"] is True
    assert result["updateVerified"] is True
    assert result["deletionMarkVerified"] is True
    assert result["finalReadback"]["DeletionMark"] is True


def test_prod_write_probe_never_soft_deletes_mismatched_readback(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    client.mismatch_readback = True
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger-mismatch.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    result = bridge.run_prod_write_probe(
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        dry_run_receipt=dry_run["dryRunReceipt"],
    )

    assert result["ok"] is False
    assert result["needsReconciliation"] is True
    assert result["cleanupVerified"] is False
    assert not [call for call in client.calls if call[0] == "patch"]


def test_prod_write_probe_rejects_bad_fingerprint_before_any_odata_request(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        target_name="prod",
    )

    with pytest.raises(PermissionError, match="exact PROD targetFingerprint"):
        bridge.run_prod_write_probe(
            dry_run=False,
            confirm=True,
            expected_target="wrong-target",
        )
    assert client.calls == []


def test_prod_write_probe_requires_single_use_dry_run_receipt_before_apply(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    with pytest.raises(PermissionError, match="run dry_run=true"):
        bridge.run_prod_write_probe(
            dry_run=False,
            confirm=True,
            expected_target=bridge.target_fingerprint,
        )
    assert client.calls == []

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    with pytest.raises(PermissionError, match="run dry_run=true"):
        bridge.run_prod_write_probe(
            dry_run=False,
            confirm=True,
            expected_target=bridge.target_fingerprint,
            dry_run_receipt="not-the-receipt",
        )
    result = bridge.run_prod_write_probe(
        dry_run=False,
        confirm=True,
        expected_target=bridge.target_fingerprint,
        dry_run_receipt=dry_run["dryRunReceipt"],
    )
    assert result["ok"] is True

    with pytest.raises(PermissionError, match="run dry_run=true"):
        bridge.run_prod_write_probe(
            dry_run=False,
            confirm=True,
            expected_target=bridge.target_fingerprint,
            dry_run_receipt=dry_run["dryRunReceipt"],
        )


def test_prod_write_probe_expires_dry_run_receipt_after_five_minutes(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger-expired.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    receipt, fingerprint, probe_id, issued_at = bridge._pending_prod_probe_receipt
    bridge._pending_prod_probe_receipt = (
        receipt, fingerprint, probe_id,
        issued_at - server_module._PROD_WRITE_PROBE_DRY_RUN_TTL_SECONDS - 1,
    )

    with pytest.raises(PermissionError, match="within five minutes"):
        bridge.run_prod_write_probe(
            dry_run=False,
            confirm=True,
            expected_target=bridge.target_fingerprint,
            dry_run_receipt=dry_run["dryRunReceipt"],
        )
    assert client.calls == []


def test_prod_write_probe_dry_run_receipt_is_consumed_atomically(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger-concurrent.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )
    dry_run = bridge.run_prod_write_probe(
        dry_run=True,
        expected_target=bridge.target_fingerprint,
    )
    start = Barrier(2)

    def apply_once():
        start.wait()
        try:
            return bridge.run_prod_write_probe(
                dry_run=False,
                confirm=True,
                expected_target=bridge.target_fingerprint,
                dry_run_receipt=dry_run["dryRunReceipt"],
            )
        except PermissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: apply_once(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert len([call for call in client.calls if call[0] in {"create", "patch"}]) == 3


def test_prod_regular_write_stays_blocked_when_probe_is_enabled(tmp_path):
    target = _configured_prod_probe()
    client = FakeProdProbeClient(target.config)
    bridge = server_module.Bridge(
        client,
        ledger=server_module.WriteLedger(tmp_path / "prod-ledger.sqlite3"),
        metadata=MetadataModel.from_xml(PROBE_CSDL),
        target_name="prod",
    )

    with pytest.raises(PermissionError, match="writes are disabled"):
        bridge.create_entity(
            "Catalog_Организации",
            {"Description": "ordinary prod write"},
            dry_run=False,
            confirm=True,
            expected_target=bridge.target_fingerprint,
            operation_id="regular-prod-create-1",
        )
    assert not [call for call in client.calls if call[0] == "create"]


def test_prod_odata_client_probe_capability_does_not_unlock_regular_writes():
    target = _configured_prod_probe()

    class RecordingTransport:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise RuntimeError("synthetic transport stop")

    transport = RecordingTransport()
    client = ODataClient(
        target.config,
        password_provider=lambda _service, _user: "unused-password",
        transport=transport,
    )
    marker = "MCP PROD WRITE PROBE 0123456789abcdef"

    with pytest.raises(PermissionError, match="OData writes are disabled"):
        client.create_entity("Catalog_Организации", {"Description": "ordinary prod write"})
    with pytest.raises(PermissionError, match="OData writes are disabled"):
        client.create_entity(
            "Document_ПлатежноеПоручение",
            {"Description": marker},
            _prod_write_probe_stage="create",
        )
    with pytest.raises(PermissionError, match="OData writes are disabled"):
        client.create_entity(
            "Catalog_Организации",
            {"Description": marker + " UPDATED"},
            _prod_write_probe_stage="create",
        )
    assert transport.calls == []

    with pytest.raises(ODataTransportError):
        client.create_entity(
            "Catalog_Организации",
            {"Description": marker},
            _prod_write_probe_stage="create",
        )
    assert len(transport.calls) == 1


def test_prod_odata_client_blocks_create_before_transport():
    prod = targets_from_json(_config())[1]

    class RecordingTransport:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("a read-only prod target must not send a write")

    transport = RecordingTransport()
    client = ODataClient(
        prod.config,
        password_provider=lambda _service, _user: "unused-password",
        transport=transport,
    )
    with pytest.raises(PermissionError, match="OData writes are disabled"):
        client.create_entity("Catalog_Test", {"Description": "must not be written"})
    assert transport.calls == []


def test_multi_target_write_readiness_names_target_json_configuration():
    prod = targets_from_json(_config())[1]
    client = ODataClient(
        prod.config,
        password_provider=lambda _service, _user: "unused-password",
        transport=object(),
    )
    bridge = server_module.Bridge(client)

    readiness = bridge._write_readiness({"ready": False, "reason": "no readable entity sets"})

    assert readiness["requiredEnvNames"] == ["ONEC83_TARGETS_JSON"]
    assert "target.enable_write is not true" in readiness["reason"]
    assert "target.target_kind is not test" in readiness["reason"]

@pytest.mark.parametrize("raw", [
    "not-json",
    "[]",
    json.dumps({"Bad Alias": _target("https://a.example/odata/standard.odata/", "test", "x")}),
    json.dumps({"prod": _target("https://a.example/odata/standard.odata/", "test", "x")}),
    json.dumps({"test": _target("http://a.example/odata/standard.odata/", "test", "x")}),
])
def test_multi_target_configuration_rejects_invalid_registry(raw):
    with pytest.raises(ConfigError):
        targets_from_json(raw)


class FakeBridge:
    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def status(self):
        self.calls += 1
        return {
            "connected": True,
            "targetFingerprint": self.name,
            "writeEnabled": self.name == "test",
        }


def test_tools_route_each_request_to_selected_target_and_tag_response(monkeypatch):
    raw = _config()
    monkeypatch.setenv("ONEC83_TARGETS_JSON", raw)
    bridges = {"test": FakeBridge("test"), "prod": FakeBridge("prod")}
    monkeypatch.setattr(server_module, "_bridges_for_targets", lambda _raw: bridges)

    with ThreadPoolExecutor(max_workers=2) as pool:
        test_future = pool.submit(server_module.onec83_connection_status, database="test")
        prod_future = pool.submit(server_module.onec83_connection_status, database="prod")
        test_result = test_future.result()
        prod_result = prod_future.result()

    assert test_result["database"] == "test"
    assert test_result["targetFingerprint"] == "test"
    assert test_result["writeEnabled"] is True
    assert prod_result["database"] == "prod"
    assert prod_result["targetFingerprint"] == "prod"
    assert prod_result["writeEnabled"] is False
    assert bridges["test"].calls == bridges["prod"].calls == 1


def test_multi_target_call_without_database_fails_closed(monkeypatch):
    monkeypatch.setenv("ONEC83_TARGETS_JSON", _config())
    bridges = {"test": FakeBridge("test"), "prod": FakeBridge("prod")}
    monkeypatch.setattr(server_module, "_bridges_for_targets", lambda _raw: bridges)

    result = server_module.onec83_connection_status()

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    assert "database is required" in result["error"]["message"]
    assert bridges["test"].calls == bridges["prod"].calls == 0


def test_unknown_database_fails_without_falling_back_to_another_target(monkeypatch):
    monkeypatch.setenv("ONEC83_TARGETS_JSON", _config())
    bridges = {"test": FakeBridge("test"), "prod": FakeBridge("prod")}
    monkeypatch.setattr(server_module, "_bridges_for_targets", lambda _raw: bridges)

    result = server_module.onec83_connection_status(database="production")

    assert result["ok"] is False
    assert result["error"]["kind"] == "validation"
    assert "unknown database" in result["error"]["message"]
    assert bridges["test"].calls == bridges["prod"].calls == 0


def test_multi_target_mode_still_rejects_password_environment_variables(monkeypatch):
    monkeypatch.setenv("ONEC83_TARGETS_JSON", _config())
    monkeypatch.setenv("ONEC83_ODATA_PASSWORD", "do-not-use")

    result = server_module.onec83_connection_status(database="test")

    assert result["ok"] is False
    assert result["error"]["kind"] == "configuration"
    assert "must not be set" in result["error"]["message"]


def test_database_discovery_does_not_expose_urls_or_credentials(monkeypatch):
    monkeypatch.setenv("ONEC83_TARGETS_JSON", _config())

    result = server_module.onec83_list_databases()

    assert result["selectionRequired"] is True
    assert result["databases"] == [
        {"name": "test", "kind": "test", "readOnly": False, "writeEnabled": True,
         "prodWriteProbeEnabled": False},
        {"name": "prod", "kind": "prod", "readOnly": True, "writeEnabled": False,
         "prodWriteProbeEnabled": False},
    ]
    assert "https://" not in json.dumps(result)
    assert "onec83-prod" not in json.dumps(result)


def test_multi_target_ledger_is_isolated_while_test_keeps_legacy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ONEC83_LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))

    assert server_module._ledger_path_for_target("test", multi_target=True) == tmp_path / "ledger.sqlite3"
    assert server_module._ledger_path_for_target("prod", multi_target=True) == tmp_path / "ledger-prod.sqlite3"


def test_every_target_tool_exposes_database_in_mcp_input_schema():
    async def read_tool_schemas():
        return await server_module.mcp.list_tools()

    schemas = asyncio.run(read_tool_schemas())
    for tool in schemas:
        if tool.name == "onec83_list_databases":
            assert "database" not in tool.input_schema["properties"]
        else:
            assert "database" in tool.input_schema["properties"], tool.name
