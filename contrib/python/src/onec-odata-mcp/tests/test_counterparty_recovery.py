"""Crash, historical alias and durable evidence regressions; synthetic only."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from onec_odata_mcp import server
from onec_odata_mcp.client import ODataClientError, ODataHTTPError
from onec_odata_mcp.ledger import UncertainWrite, WriteLedger, request_digest
from onec_odata_mcp.metadata import MetadataModel
from test_counterparty_create import CSDL, ENTITY, PAYLOAD, Client, bridge, create
from test_multidatabase import _target


@pytest.mark.parametrize("operation", ["synthetic-create-1", "synthetic-new-attempt"])
@pytest.mark.parametrize("stage", ["search", "prepare"])
def test_pre_receipt_process_crash_can_resume_without_manual_repair(tmp_path, operation, stage):
    script = '''import os, sys
from pathlib import Path
sys.path.insert(0, "tests")
from test_counterparty_create import bridge, create
b, _ = bridge(Path(sys.argv[1]))
def crash(*a, **kw):
    os._exit(73)
if sys.argv[2] == "search":
    b.search_entities = crash
else:
    b._prepare_write = crash
create(b)
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path), stage], timeout=15)
    assert crashed.returncode == 73
    restarted, client = bridge(tmp_path)
    assert create(restarted, operation=operation)["readbackVerified"]
    assert len(client.posts) == 1


def test_active_pre_receipt_scan_cannot_be_recovered_as_orphan(tmp_path):
    first, client = bridge(tmp_path)
    second, _ = bridge(tmp_path, client)
    entered, release = threading.Event(), threading.Event()
    original = first.search_entities
    def search(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    first.search_entities = search
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(create, first)
        assert entered.wait(5)
        try:
            with pytest.raises(UncertainWrite):
                create(second, operation="synthetic-concurrent-scan")
        finally:
            release.set()
        assert running.result()["readbackVerified"]
    assert len(client.posts) == 1


@pytest.mark.parametrize("old_alias", ["test", "retired_alias"])
def test_renamed_alias_discovers_historical_pending_after_cold_restart(tmp_path, monkeypatch, old_alias):
    path = tmp_path / "ledger.sqlite3"
    url = "https://synthetic.test/base/odata/standard.odata/"
    old_raw = json.dumps({old_alias: _target(url, "test", "synthetic", writable=True)})
    script = '''import os, sys
sys.path.insert(0, "tests")
from onec_odata_mcp import server
from onec_odata_mcp.client import ODataClientError
from onec_odata_mcp.metadata import MetadataModel
from test_counterparty_create import CSDL, Client, create
class PendingClient(Client):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.post_failure = ODataClientError("synthetic uncertain POST")
server.ODataClient = PendingClient
b = next(iter(server._bridges_for_targets(sys.argv[1]).values()))
b._metadata = MetadataModel.from_xml(CSDL)
assert create(b)["writeState"] == "pending"
'''
    env = {**os.environ, "ONEC83_LEDGER_PATH": str(path)}
    subprocess.run([sys.executable, "-c", script, old_raw], env=env, check=True, timeout=15)
    monkeypatch.setenv("ONEC83_LEDGER_PATH", str(path))
    def make_client(config):
        client = Client()
        client.config = config
        return client
    monkeypatch.setattr(server, "ODataClient", make_client)
    server._bridges_for_targets.cache_clear()
    try:
        new_raw = json.dumps({"replacement": _target(url, "test", "synthetic", writable=True)})
        b = server._bridges_for_targets(new_raw)["replacement"]
        b._metadata = MetadataModel.from_xml(CSDL)
        with pytest.raises(UncertainWrite):
            create(b, operation="synthetic-renamed-create")
        assert not b.client.posts
    finally:
        server._bridges_for_targets.cache_clear()


@pytest.mark.parametrize("failure", [None, "pending", "accepted_readback"])
def test_uniqueness_evidence_is_durable_before_post_and_returned_on_all_outcomes(tmp_path, failure):
    b, client = bridge(tmp_path)
    original_post = client.create_entity
    checks_at_post = []
    def post(*args, **kwargs):
        checks_at_post.append(b.ledger.inspect("synthetic-create-1")["result"]["counterpartyUniqueness"])
        return original_post(*args, **kwargs)
    client.create_entity = post
    if failure == "pending":
        client.post_failure = ODataClientError("synthetic unknown")
    if failure == "accepted_readback":
        def unavailable(*args, **kwargs):
            raise ODataClientError("synthetic readback unavailable")
        client.get_entity = unavailable
    result = create(b)
    check = result["counterpartyUniqueness"]
    assert checks_at_post == [check]
    assert check["status"] == "verified_absent"
    assert check["targetFingerprint"] == b.target_fingerprint
    assert check["strategy"] == "server_filter"
    assert check["complete"] is True
    assert check["resultLimited"] is False
    assert check["checkedAt"] and check["phase"] == "before_post"
    assert check["evidenceDigest"] == request_digest({k: v for k, v in check.items() if k != "evidenceDigest"})
    encoded = json.dumps(check, ensure_ascii=False)
    assert PAYLOAD["ИНН"] not in encoded and PAYLOAD["Description"] not in encoded
    assert b.ledger.inspect("synthetic-create-1")["result"]["counterpartyUniqueness"] == check
    if failure != "pending":
        reads = len(client.reads)
        replay = create(b)
        assert replay["counterpartyUniqueness"] == check
        assert len(client.reads) == reads and len(client.posts) == 1


def test_fallback_evidence_keeps_only_sanitized_factual_search_fields(tmp_path):
    b, client = bridge(tmp_path)
    original = client.list_entities
    def without_filter(entity_set, **kwargs):
        if kwargs.get("filter"):
            raise ODataHTTPError(500, code="-1", message='Операция не разрешена в предложении "ГДЕ"')
        return original(entity_set, **kwargs)
    client.list_entities = without_filter
    result = create(b)
    check = result["counterpartyUniqueness"]
    assert check["strategy"] in {"linear_scan_after_filter_error", "ordered_scan_after_filter_error"}
    assert check["complete"] is True
    assert "filter" not in check and "predicate" not in check and "result" not in check


def test_live_scan_process_holds_claim_until_death_then_recovers(tmp_path):
    ready = tmp_path / "scan-ready"
    script = '''import sys, time
from pathlib import Path
sys.path.insert(0, "tests")
from test_counterparty_create import bridge, create
b, _ = bridge(Path(sys.argv[1]))
def search(*a, **kw):
    Path(sys.argv[2]).touch()
    while True:
        time.sleep(0.05)
b.search_entities = search
create(b)
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(ready)])
    try:
        import time
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        b, client = bridge(tmp_path)
        with pytest.raises(UncertainWrite):
            create(b, operation="synthetic-live-process")
        assert not client.posts
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert create(b, operation="synthetic-after-process-death")["readbackVerified"]
    assert len(client.posts) == 1


def test_renamed_alias_holds_unknown_legacy_history_without_claim_or_binding(tmp_path, monkeypatch):
    old_path = tmp_path / "ledger-retired.sqlite3"
    old = WriteLedger(old_path)
    old.reserve("synthetic-legacy-pending", "synthetic-legacy-digest")
    before = old.inspect("synthetic-legacy-pending")
    monkeypatch.setenv("ONEC83_LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    def make_client(config):
        client = Client()
        client.config = config
        return client
    monkeypatch.setattr(server, "ODataClient", make_client)
    server._bridges_for_targets.cache_clear()
    try:
        raw = json.dumps({"replacement": _target("https://synthetic.test/base/odata/standard.odata/", "test", "synthetic", writable=True)})
        b = server._bridges_for_targets(raw)["replacement"]
        b._metadata = MetadataModel.from_xml(CSDL)
        with pytest.raises(UncertainWrite):
            create(b)
        assert not b.client.posts
        assert old.inspect("synthetic-legacy-pending") == before
    finally:
        server._bridges_for_targets.cache_clear()


def test_reconciliation_preserves_original_pre_post_evidence_for_replay(tmp_path):
    b, client = bridge(tmp_path)
    client.post_failure = ODataClientError("synthetic unknown")
    pending = create(b)
    saved = b.ledger.inspect("synthetic-create-1")
    b.ledger.reconcile_pending("synthetic-create-1", saved["request_hash"], "reconciled", {
        "entitySet": ENTITY, "writeState": "reconciled",
    })
    replay = create(b)
    assert replay["counterpartyUniqueness"] == pending["counterpartyUniqueness"]
    assert len(client.posts) == 1
