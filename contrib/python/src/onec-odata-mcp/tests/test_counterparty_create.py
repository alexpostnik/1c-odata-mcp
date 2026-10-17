"""Synthetic counterparty create contract; no connection to a real database."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from onec_odata_mcp import server
from onec_odata_mcp.client import ODataClient, ODataClientError, ODataHTTPError
from onec_odata_mcp.config import RuntimeConfig
from onec_odata_mcp.ledger import UncertainWrite, WriteLedger
from onec_odata_mcp.metadata import MetadataModel
from onec_odata_mcp.server import Bridge

ENTITY = "Catalog_Контрагенты"
KEY = "00000000-0000-0000-0000-000000000001"
PAYLOAD = {"Description": "SYNTHETIC CARD", "ИНН": "001ABC234"}
CSDL = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
<edmx:DataServices><Schema Namespace="Demo" xmlns="http://schemas.microsoft.com/ado/2009/11/edm">
<EntityType Name="Counterparty"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Description" Type="Edm.String"/>
<Property Name="ИНН" Type="Edm.String"/><Property Name="IsFolder" Type="Edm.Boolean"/>
<Property Name="DeletionMark" Type="Edm.Boolean"/>
<Property Name="ЮридическоеФизическоеЛицо" Type="Edm.String"/>
</EntityType><EntityContainer Name="Demo"><EntitySet Name="Catalog_Контрагенты" EntityType="Demo.Counterparty"/>
<EntitySet Name="Catalog_Other" EntityType="Demo.Counterparty"/>
</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'''


class Client:
    write_enabled = True

    def __init__(self):
        self.config = type("Config", (), {"odata_base_url": "https://synthetic.test/odata/", "max_top": 200})()
        self.rows = []
        self.posts = []
        self.reads = []
        self.failure = None
        self.post_failure = None
        self.entered = None
        self.release = None
        self.next_link = None

    def list_entities(self, entity_set, **kwargs):
        self.reads.append(kwargs)
        if self.failure:
            raise self.failure
        rows = self.rows
        if kwargs.get("filter"):
            rows = [row for row in rows if row.get("ИНН") == PAYLOAD["ИНН"]]
        page = rows[kwargs["skip"]:kwargs["skip"] + kwargs["top"]]
        return {"value": page, **({"next_link": self.next_link} if self.next_link else {})}

    def count_entities(self, entity_set):
        return len(self.rows)

    def create_entity(self, entity_set, payload, null_ref_key=False):
        self.posts.append(dict(payload))
        if self.entered:
            self.entered.set()
            assert self.release.wait(5)
        if self.post_failure:
            raise self.post_failure
        self.rows.append({**payload, "Ref_Key": KEY})
        return {"Ref_Key": KEY}

    def get_entity(self, entity_set, key):
        return {**self.posts[-1], "Ref_Key": key}


def bridge(tmp_path, client=None):
    client = client or Client()
    return Bridge(client, WriteLedger(tmp_path / "receipts.sqlite3"), MetadataModel.from_xml(CSDL)), client


def create(b, payload=None, operation="synthetic-create-1"):
    return b.create_entity(ENTITY, PAYLOAD if payload is None else payload, dry_run=False,
                           confirm=True, expected_target=b.target_fingerprint, operation_id=operation)


@pytest.mark.parametrize("value", [None, "", "  ", "0", "000000000", 123, True, {}, "UNKNOWN", "N/A"])
@pytest.mark.parametrize("dry", [True, False])
def test_required_unp_rejected_before_receipt_or_post(tmp_path, value, dry):
    b, client = bridge(tmp_path)
    payload = {**PAYLOAD, "ИНН": value}
    with pytest.raises(ValueError):
        if dry:
            b.create_entity(ENTITY, payload)
        else:
            create(b, payload)
    assert client.posts == []
    assert b.ledger.inspect("synthetic-create-1") is None


@pytest.mark.parametrize("field", ["Description", "ИНН"])
def test_missing_required_field_rejected(tmp_path, field):
    b, client = bridge(tmp_path)
    with pytest.raises(ValueError):
        create(b, {k: v for k, v in PAYLOAD.items() if k != field})
    assert not client.posts


@pytest.mark.parametrize("name", [None, "", "  ", 4])
def test_blank_name_rejected(tmp_path, name):
    b, client = bridge(tmp_path)
    with pytest.raises(ValueError):
        create(b, {**PAYLOAD, "Description": name})
    assert not client.posts


@pytest.mark.parametrize("unp", ["001234567", "001ABC234", "АВ1234567"])
def test_unique_supported_unp_preserves_string_and_leading_zeroes(tmp_path, unp):
    b, client = bridge(tmp_path)
    result = create(b, {**PAYLOAD, "ИНН": unp})
    assert result["readbackVerified"] is True
    assert client.posts[0]["ИНН"] == unp
    assert client.reads
    assert "DeletionMark eq false" not in str(client.reads)


def test_preview_validates_fields_but_does_not_claim_uniqueness(tmp_path):
    b, client = bridge(tmp_path)
    preview = b.create_entity(ENTITY, PAYLOAD)
    assert preview["plan"]["counterpartyUniqueness"]["status"] == "not_checked"
    assert not client.reads and not client.posts
    assert not b.ledger.path.exists()


@pytest.mark.parametrize("marked", [False, True])
def test_duplicate_including_deletion_mark_is_rejected_without_leaking_values(tmp_path, marked):
    b, client = bridge(tmp_path)
    client.rows = [{"Ref_Key": KEY, "ИНН": PAYLOAD["ИНН"], "DeletionMark": marked}]
    with pytest.raises(ValueError) as exc:
        create(b)
    assert PAYLOAD["ИНН"] not in str(exc.value) and KEY not in str(exc.value)
    assert not client.posts
    assert b.ledger.inspect("synthetic-create-1") is None


@pytest.mark.parametrize("outcome", [
    {"complete": False}, {"complete": True, "resultLimited": True},
    {"complete": True, "maxScanReached": True}, {"complete": True, "scanInterrupted": True},
    {"complete": True, "result": {"value": [], "next_link": "hidden-page"}},
    {"complete": True, "result": {}}, {"complete": None},
])
def test_incomplete_or_unknown_absence_fails_closed_without_operation_claim(tmp_path, monkeypatch, outcome):
    b, client = bridge(tmp_path)
    response = {"complete": True, "resultLimited": False, "maxScanReached": False,
                "result": {"value": []}, **outcome}
    monkeypatch.setattr(b, "search_entities", lambda *a, **kw: response)
    with pytest.raises(ValueError):
        create(b)
    assert not client.posts
    assert b.ledger.inspect("synthetic-create-1") is None


def test_duplicate_wins_even_if_search_incomplete(tmp_path, monkeypatch):
    b, client = bridge(tmp_path)
    monkeypatch.setattr(b, "search_entities", lambda *a, **kw: {
        "complete": False, "result": {"value": [{"ИНН": PAYLOAD["ИНН"]}]}})
    with pytest.raises(ValueError, match="already exists"):
        create(b)
    assert not client.posts


def test_search_exception_sanitized_and_retry_after_definite_no_post_is_possible(tmp_path):
    b, client = bridge(tmp_path)
    client.failure = ODataClientError("SENSITIVE IDENTIFIER " + PAYLOAD["ИНН"])
    with pytest.raises(ValueError) as exc:
        create(b)
    assert "SENSITIVE" not in str(exc.value) and PAYLOAD["ИНН"] not in str(exc.value)
    assert b.ledger.inspect("synthetic-create-1") is None
    client.failure = None
    assert create(b)["readbackVerified"] is True


def test_actual_server_next_page_cannot_be_mistaken_for_unique(tmp_path):
    b, client = bridge(tmp_path)
    client.next_link = "next-page"
    with pytest.raises(ValueError):
        create(b)
    assert not client.posts


def test_filter_rejection_reuses_bounded_scan_and_checks_all_rows(tmp_path):
    b, client = bridge(tmp_path)
    original = client.list_entities
    client.rows = [{"ИНН": "111"}, {"ИНН": PAYLOAD["ИНН"], "DeletionMark": True}]
    def list_rows(entity_set, **kwargs):
        if kwargs.get("filter"):
            raise ODataHTTPError(500, code="-1", message='Операция не разрешена в предложении "ГДЕ"')
        return original(entity_set, **kwargs)
    client.list_entities = list_rows
    with pytest.raises(ValueError, match="already exists"):
        create(b)
    assert not client.posts


@pytest.mark.parametrize("value", ["true", 1, [], {}, None])
def test_non_boolean_folder_cannot_bypass_requiredness(tmp_path, value):
    b, client = bridge(tmp_path)
    with pytest.raises(ValueError):
        create(b, {"Description": "SYNTHETIC FOLDER", "IsFolder": value})
    assert not client.posts


def test_true_folder_needs_no_unp_and_other_catalog_semantics_stay_unchanged(tmp_path):
    b, client = bridge(tmp_path)
    assert create(b, {"Description": "SYNTHETIC FOLDER", "IsFolder": True})["readbackVerified"]
    assert not client.reads
    assert b.create_entity("Catalog_Other", {"Description": "Other"})["dryRun"]


def test_completed_replay_precedes_duplicate_read(tmp_path):
    b, client = bridge(tmp_path)
    create(b)
    client.failure = ODataClientError("must not search")
    assert create(b)["replayedReceipt"] is True
    assert len(client.posts) == 1


def test_uncertain_post_holds_same_unp_new_operation_after_new_bridge(tmp_path):
    b, client = bridge(tmp_path)
    client.post_failure = ODataClientError("uncertain synthetic POST")
    result = create(b)
    assert result["writeState"] == "pending"
    restarted, _ = bridge(tmp_path, client)
    with pytest.raises(UncertainWrite):
        create(restarted, operation="synthetic-create-2")
    with pytest.raises(UncertainWrite):
        create(restarted)
    assert len(client.posts) == 1


def test_concurrent_bridges_same_unp_issue_at_most_one_post(tmp_path):
    client = Client()
    client.entered, client.release = threading.Event(), threading.Event()
    first, _ = bridge(tmp_path, client)
    second, _ = bridge(tmp_path, client)
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(create, first)
        assert client.entered.wait(5)
        try:
            with pytest.raises((UncertainWrite, ValueError)):
                create(second, operation="synthetic-create-2")
        finally:
            client.release.set()
        assert future.result()["readbackVerified"] is True
    assert len(client.posts) == 1


def test_different_target_can_use_same_unp(tmp_path):
    b, _ = bridge(tmp_path)
    create(b)
    client = Client()
    client.config.odata_base_url = "https://other-synthetic.test/odata/"
    second, _ = bridge(tmp_path, client)
    assert create(second, operation="synthetic-create-2")["readbackVerified"]


def test_public_tool_routes_through_same_guard(tmp_path, monkeypatch):
    b, client = bridge(tmp_path)
    monkeypatch.setattr(server, "_bridge", lambda: b)
    result = server.onec83_create_entity(ENTITY, {"Description": "Synthetic"})
    assert result["ok"] is False
    assert not client.posts


def test_raw_client_card_create_cannot_bypass_bridge_guard():
    class Transport:
        calls = []
        def request(self, *args, **kwargs):
            self.calls.append(args)
            raise AssertionError("unguarded POST")
    config = RuntimeConfig(odata_base_url="https://synthetic.test/odata/", odata_user="synthetic",
                           keychain_service="synthetic", enable_write=True,
                           write_base_url="https://synthetic.test/odata/", target_kind="test")
    transport = Transport()
    client = ODataClient(config, password_provider=lambda *a: "synthetic", transport=transport)
    with pytest.raises((ValueError, PermissionError), match="Bridge|guard"):
        client.create_entity(ENTITY, PAYLOAD)
    assert not transport.calls


def test_definite_post_refusal_releases_unp_for_new_operation(tmp_path):
    b, client = bridge(tmp_path)
    client.post_failure = ODataHTTPError(400, code="synthetic", message="synthetic refusal", definitive=True)
    assert create(b)["writeState"] == "failed"
    client.post_failure = None
    restarted, _ = bridge(tmp_path, client)
    assert create(restarted, operation="synthetic-create-2")["readbackVerified"]
    assert len(client.posts) == 2


def test_rejected_duplicate_does_not_leave_uncertain_claim(tmp_path):
    b, client = bridge(tmp_path)
    client.rows = [{"ИНН": PAYLOAD["ИНН"]}]
    with pytest.raises(ValueError, match="already exists"):
        create(b)
    client.rows = []
    assert create(b)["readbackVerified"]


def test_unclassified_legacy_pending_receipt_holds_new_counterparty(tmp_path):
    b, client = bridge(tmp_path)
    b.ledger.reserve("synthetic-old-attempt", "unknown-original-request")
    with pytest.raises(UncertainWrite):
        create(b)
    assert not client.posts


def test_persistent_unp_claim_survives_separate_process(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "receipts.sqlite3"
    script = '''from pathlib import Path
from onec_odata_mcp.ledger import WriteLedger, request_digest
from onec_odata_mcp.counterparty import COUNTERPARTIES
import sys
ledger = WriteLedger(Path(sys.argv[1]))
ledger.reserve_counterparty(request_digest(sys.argv[2], COUNTERPARTIES, sys.argv[3]), "synthetic-process-attempt")
ledger.reserve("synthetic-process-attempt", "synthetic-request-digest")
'''
    b, client = bridge(tmp_path)
    subprocess.run([sys.executable, "-c", script, str(path), b.target_fingerprint, PAYLOAD["ИНН"]], check=True)
    restarted, _ = bridge(tmp_path, client)
    with pytest.raises(UncertainWrite):
        create(restarted)
    assert not client.posts


def test_guarded_real_client_path_sends_one_post_and_replay_does_not_repeat(tmp_path):
    from test_client import FakeResponse, FakeTransport, config
    transport = FakeTransport([
        FakeResponse(200, {"value": []}),
        FakeResponse(201, {"Ref_Key": KEY}),
        FakeResponse(200, {**PAYLOAD, "Ref_Key": KEY}),
    ])
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/", target_kind="test")
    client = ODataClient(cfg, password_provider=lambda *a: "synthetic", transport=transport)
    b = Bridge(client, WriteLedger(tmp_path / "receipts.sqlite3"), MetadataModel.from_xml(CSDL))
    assert create(b)["readbackVerified"]
    assert create(b)["replayedReceipt"]
    assert [call[0] for call in transport.calls] == ["GET", "POST", "GET"]


def test_database_aliases_same_target_preserve_separate_receipts_and_share_unp_hold(tmp_path, monkeypatch):
    import json
    from test_multidatabase import _target
    url = "https://synthetic.test/base/odata/standard.odata/"
    raw = json.dumps({name: _target(url, "test", "synthetic", writable=True)
                      for name in ("test", "test_alias")})
    def make_client(config):
        client = Client()
        client.config = config
        return client
    monkeypatch.setattr(server, "ODataClient", make_client)
    monkeypatch.setattr(server, "_ledger_path_for_target", lambda name, **kw: tmp_path / (name + ".sqlite3"))
    server._bridges_for_targets.cache_clear()
    try:
        bridges = server._bridges_for_targets(raw)
        for b in bridges.values():
            b._metadata = MetadataModel.from_xml(CSDL)
        first, second = bridges["test"], bridges["test_alias"]
        assert first.ledger.path != second.ledger.path
        first.client.post_failure = ODataClientError("uncertain synthetic POST")
        assert create(first)["writeState"] == "pending"
        with pytest.raises(UncertainWrite):
            create(second, operation="synthetic-alias-create-2")
        assert not second.client.posts
    finally:
        server._bridges_for_targets.cache_clear()
