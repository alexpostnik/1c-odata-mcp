"""Target authority, receipt scoping and historical compatibility; synthetic only."""
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest
from onec_odata_mcp import server
from onec_odata_mcp.client import ODataClientError
from onec_odata_mcp.counterparty import COUNTERPARTIES
from onec_odata_mcp.ledger import WriteLedger, UncertainWrite, request_digest
from onec_odata_mcp.metadata import MetadataModel
from test_counterparty_create import Client, CSDL, create, PAYLOAD
from test_multidatabase import _target


def configured(tmp_path, monkeypatch, urls):
    monkeypatch.setenv('ONEC83_LEDGER_PATH', str(tmp_path / 'ledger.sqlite3'))
    def make_client(config):
        c = Client(); c.config = config; return c
    monkeypatch.setattr(server, 'ODataClient', make_client)
    server._bridges_for_targets.cache_clear()
    raw = json.dumps({name: _target(url, 'test', 'synthetic', writable=True) for name,url in urls.items()})
    bridges = server._bridges_for_targets(raw)
    for b in bridges.values(): b._metadata = MetadataModel.from_xml(CSDL)
    return bridges


@pytest.mark.parametrize('url', ['https://SYNTHETIC.test/base/odata/standard.odata/', 'https://synthetic.test:443/base/odata/standard.odata/'])
def test_equivalent_endpoint_pending_must_hold_new_alias(tmp_path, monkeypatch, url):
    bridges = configured(tmp_path, monkeypatch, {'test':url, 'alias':'https://synthetic.test/base/odata/standard.odata/'})
    first, second = bridges['test'], bridges['alias']
    first.client.post_failure = ODataClientError('synthetic unknown accepted POST')
    assert create(first)['writeState'] == 'pending'
    with pytest.raises(UncertainWrite):
        create(second, operation='synthetic-alias-new')
    assert not second.client.posts


def test_known_other_target_legacy_pending_must_not_block(tmp_path, monkeypatch):
    bridges = configured(tmp_path, monkeypatch, {'test':'https://synthetic-a.test/base/odata/standard.odata/', 'other':'https://synthetic-b.test/base/odata/standard.odata/'})
    current, other = bridges['test'], bridges['other']
    other.ledger.reserve('synthetic-legacy-other', 'synthetic-digest')
    other.ledger.record_pending('synthetic-legacy-other', 'synthetic-digest', {'entitySet':COUNTERPARTIES,'targetFingerprint':other.target_fingerprint,'writeState':'pending'})
    assert create(current)['readbackVerified']


def test_other_target_same_operation_receipt_does_not_make_prepost_claim_uncertain(tmp_path, monkeypatch):
    bridges = configured(tmp_path, monkeypatch, {'test':'https://synthetic-a.test/base/odata/standard.odata/', 'other':'https://synthetic-b.test/base/odata/standard.odata/'})
    current, other = bridges['test'], bridges['other']
    assert create(other, operation='synthetic-shared-operation')['readbackVerified']
    claim = request_digest(current.target_fingerprint, COUNTERPARTIES, PAYLOAD['ИНН'])
    for peer in current._counterparty_ledgers():
        peer.reserve_counterparty(claim, 'synthetic-shared-operation')
    # Simulate process death after replicated A claim, before A ordinary write.
    assert current.ledger.inspect('synthetic-shared-operation') is None
    assert create(current, operation='synthetic-after-prepost-crash')['readbackVerified']


@pytest.mark.parametrize("old_url", [
    "https://SYNTHETIC.test/base/odata/standard.odata/",
    "https://synthetic.test:443/base/odata/standard.odata/",
])
@pytest.mark.parametrize("state", ["claim_only", "pending", "accepted", "complete", "reconciled"])
def test_preupgrade_raw_claim_survives_retired_authority_spelling(tmp_path, monkeypatch, old_url, state):
    old_target = hashlib.sha256(old_url.encode()).hexdigest()
    old_path = tmp_path / "ledger-retired.sqlite3"
    # A separate old-process fixture writes the exact pre-upgrade two-column
    # claim schema. The new Bridge starts only after that process exits.
    script = """import hashlib, json, sqlite3, sys
path, target, state, payload = sys.argv[1:]
payload = json.loads(payload)
def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()
with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE counterparty_creates (claim_hash TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")
    conn.execute("CREATE TABLE writes (operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO counterparty_creates VALUES (?, ?)",
                 (digest(target, "Catalog_Контрагенты", payload["ИНН"]), "synthetic-old-authority"))
    if state != "claim_only":
        receipt = {"entitySet": "Catalog_Контрагенты", "targetFingerprint": target, "writeState": state}
        conn.execute("INSERT INTO writes(operation_id, request_hash, state, result_json) VALUES (?, ?, ?, ?)",
                     ("synthetic-old-authority", digest(target, "create", "Catalog_Контрагенты", None, payload, None),
                      state if state in {"complete", "reconciled"} else "pending", json.dumps(receipt)))
"""
    subprocess.run([sys.executable, "-c", script, str(old_path), old_target, state,
                    json.dumps(PAYLOAD, ensure_ascii=False)], check=True, timeout=15)
    old = WriteLedger(old_path)
    before = old.inspect("synthetic-old-authority")
    current = configured(tmp_path, monkeypatch, {"replacement": "https://synthetic.test/base/odata/standard.odata/"})["replacement"]
    with pytest.raises(UncertainWrite):
        create(current, operation="synthetic-after-upgrade")
    assert not current.client.posts
    assert old.inspect("synthetic-old-authority") == before


@pytest.mark.parametrize("other", [
    "https://synthetic.test:444/base/odata/standard.odata/",
    "http://synthetic.test/base/odata/standard.odata/",
    "https://synthetic.test/Base/odata/standard.odata/",
    "https://different.test/base/odata/standard.odata/",
])
def test_canonical_identity_preserves_distinct_endpoints(other):
    from onec_odata_mcp.counterparty import target_fingerprint
    assert target_fingerprint(other) != target_fingerprint("https://synthetic.test/base/odata/standard.odata/")


@pytest.mark.parametrize("binding", [None, "", 42, "malformed"])
def test_legacy_pending_unknown_binding_stays_held(tmp_path, monkeypatch, binding):
    current = configured(tmp_path, monkeypatch, {"test": "https://synthetic.test/base/odata/standard.odata/"})["test"]
    current.ledger.reserve("synthetic-old-unknown", "synthetic-digest")
    current.ledger.record_pending("synthetic-old-unknown", "synthetic-digest", {
        "entitySet": COUNTERPARTIES, "targetFingerprint": binding,
    })
    with pytest.raises(UncertainWrite):
        create(current)
    assert not current.client.posts


def test_raw_authority_completed_receipt_replays_without_hash_rewrite(tmp_path, monkeypatch):
    url = "https://SYNTHETIC.test:443/base/odata/standard.odata/"
    current = configured(tmp_path, monkeypatch, {"test": url})["test"]
    raw_target = hashlib.sha256(url.encode()).hexdigest()
    assert current.target_fingerprint == raw_target
    digest = request_digest(raw_target, "create", COUNTERPARTIES, None, {**PAYLOAD, "Ref_Key": "00000000-0000-0000-0000-000000000000"}, None)
    current.ledger.reserve("synthetic-create-1", digest)
    current.ledger.complete("synthetic-create-1", {"entitySet": COUNTERPARTIES, "targetFingerprint": raw_target, "writeState": "complete"})
    before = current.ledger.inspect("synthetic-create-1")
    assert create(current)["replayedReceipt"]
    assert not current.client.posts and not current.client.reads
    assert current.ledger.inspect("synthetic-create-1") == before


@pytest.mark.parametrize("state", ["pending", "complete", "failed"])
def test_bound_claim_with_unknown_or_mismatched_request_cannot_be_orphaned(tmp_path, monkeypatch, state):
    from onec_odata_mcp.counterparty import target_fingerprint
    b = configured(tmp_path, monkeypatch, {"test": "https://synthetic.test/base/odata/standard.odata/"})["test"]
    target = target_fingerprint(b.client.config.odata_base_url)
    claim = request_digest(target, COUNTERPARTIES, PAYLOAD["ИНН"])
    for peer in b._counterparty_ledgers():
        peer.reserve_counterparty(claim, "synthetic-prior-bound", target=target,
                                  request_hash="synthetic-original-hash")
    b.ledger.reserve("synthetic-prior-bound", "synthetic-different-hash")
    receipt = {"entitySet": COUNTERPARTIES, "targetFingerprint": b.target_fingerprint}
    if state == "pending":
        b.ledger.record_pending("synthetic-prior-bound", "synthetic-different-hash", receipt)
    elif state == "complete":
        b.ledger.complete("synthetic-prior-bound", receipt)
    else:
        b.ledger.fail("synthetic-prior-bound", receipt)
    with pytest.raises(UncertainWrite):
        create(b)
    assert not b.client.posts


def test_same_target_pending_other_unp_allows_independent_card(tmp_path, monkeypatch):
    b = configured(tmp_path, monkeypatch, {"test": "https://SYNTHETIC.test:443/base/odata/standard.odata/"})["test"]
    b.client.post_failure = ODataClientError("synthetic pending")
    assert create(b)["writeState"] == "pending"
    b.client.post_failure = None
    assert create(b, payload={**PAYLOAD, "ИНН": "002ABC234"}, operation="synthetic-independent-unp")["readbackVerified"]


@pytest.mark.parametrize("bad", ["malformed", "", 42])
def test_malformed_claim_target_cannot_be_skipped_as_other_target(tmp_path, monkeypatch, bad):
    b = configured(tmp_path, monkeypatch, {"test": "https://synthetic.test/base/odata/standard.odata/"})["test"]
    b.ledger.reserve_counterparty("synthetic-opaque-claim", "synthetic-old-claim")
    with sqlite3.connect(b.ledger.path) as conn:
        conn.execute("UPDATE counterparty_creates SET target_fingerprint=?", (bad,))
    with pytest.raises(UncertainWrite):
        create(b)
    assert not b.client.posts



def test_unknown_raw_claim_is_not_bound_by_unrelated_same_id_receipt(tmp_path, monkeypatch):
    bridges = configured(tmp_path, monkeypatch, {
        "test": "https://synthetic.test/base/odata/standard.odata/",
        "other": "https://different.test/base/odata/standard.odata/",
    })
    b, other = bridges["test"], bridges["other"]
    old_target = hashlib.sha256(b"https://SYNTHETIC.test:443/base/odata/standard.odata/").hexdigest()
    claim = request_digest(old_target, COUNTERPARTIES, PAYLOAD["ИНН"])
    other.ledger.reserve_counterparty(claim, "synthetic-shared-old-id")
    other.ledger.reserve("synthetic-shared-old-id", "synthetic-unrelated-digest")
    other.ledger.complete("synthetic-shared-old-id", {
        "entitySet": COUNTERPARTIES, "targetFingerprint": other.target_fingerprint,
    })
    with pytest.raises(UncertainWrite):
        create(b)
    assert not b.client.posts


@pytest.mark.parametrize("state", ["pending", "complete"])
def test_historical_claim_hash_proves_different_target_without_id_guess(tmp_path, monkeypatch, state):
    bridges = configured(tmp_path, monkeypatch, {
        "test": "https://synthetic.test/base/odata/standard.odata/",
        "other": "https://different.test/base/odata/standard.odata/",
    })
    b, other = bridges["test"], bridges["other"]
    claim = request_digest(other.target_fingerprint, COUNTERPARTIES, PAYLOAD["ИНН"])
    other.ledger.reserve_counterparty(claim, "synthetic-historical-other")
    other.ledger.reserve("synthetic-historical-other", "synthetic-other-digest")
    receipt = {"entitySet": COUNTERPARTIES, "targetFingerprint": other.target_fingerprint}
    if state == "pending":
        other.ledger.record_pending("synthetic-historical-other", "synthetic-other-digest", receipt)
    else:
        other.ledger.complete("synthetic-historical-other", receipt)
    assert create(b)["readbackVerified"]
