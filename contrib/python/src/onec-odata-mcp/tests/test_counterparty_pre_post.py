"""Atomic pre-transport recovery and actual CSDL reference order; synthetic only."""
import subprocess
import sys
from pathlib import Path

import pytest

from onec_odata_mcp.ledger import UncertainWrite
from onec_odata_mcp.metadata import MetadataModel
from test_counterparty_create import CSDL, KEY, PAYLOAD, bridge, create

CRASH_SCRIPT = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from test_counterparty_create import bridge, create
b, client = bridge(Path(sys.argv[1]))
stage = sys.argv[2]
def after(obj, method):
    original = getattr(obj, method)
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(77)
    setattr(obj, method, crash)
if stage == 'reserve':
    after(b.ledger, 'reserve')
elif stage == 'prepare':
    after(b, '_prepare_write')
elif stage == 'reference':
    after(b, '_preflight_payload_references')
elif stage == 'search':
    after(b, 'search_entities')
elif stage == 'before_pending':
    b.ledger.record_pending = lambda *a, **kw: os._exit(77)
elif stage == 'pending':
    after(b.ledger, 'record_pending')
elif stage == 'transport':
    def sent(*a, **kw):
        Path(sys.argv[1], 'post-entered').touch()
        os._exit(77)
    client.create_entity = sent
elif stage == 'accepted':
    after(client, 'create_entity')
elif stage == 'complete':
    b.ledger.complete = lambda *a, **kw: os._exit(77)
create(b)
'''


def crash_at(tmp_path, stage):
    process = subprocess.run([sys.executable, '-B', '-c', CRASH_SCRIPT,
                              str(tmp_path), stage, str(Path(__file__).resolve().parent)], timeout=15)
    assert process.returncode == 77


@pytest.mark.parametrize('stage', ['reserve', 'prepare', 'reference', 'search', 'before_pending'])
@pytest.mark.parametrize('operation', ['synthetic-create-1', 'synthetic-new-after-crash'])
def test_proven_pre_post_process_death_recovers_with_bound_receipt(tmp_path, stage, operation):
    crash_at(tmp_path, stage)
    b, client = bridge(tmp_path)
    saved = b.ledger.inspect('synthetic-create-1')
    if stage in {'reference', 'search'}:
        assert saved is None  # A claim-only attempt is still before the reservation.
    else:
        assert saved['result'] is not None, 'Initial receipt must atomically retain target/request/pre-POST stage'
        assert saved['result']['targetFingerprint'] == b.target_fingerprint
        assert saved['result']['requestHash'] == saved['request_hash']
        assert saved['result']['counterpartyCreateStage'] == 'pre_post'
    assert not (tmp_path / 'post-entered').exists()
    assert create(b, operation=operation)['readbackVerified']
    assert len(client.posts) == 1
    assert create(b, operation=operation)['replayedReceipt']
    assert len(client.posts) == 1


@pytest.mark.parametrize('stage', ['pending', 'transport', 'accepted', 'complete'])
@pytest.mark.parametrize('operation', ['synthetic-create-1', 'synthetic-new-after-crash'])
def test_transport_possible_process_death_remains_held(tmp_path, stage, operation):
    crash_at(tmp_path, stage)
    b, client = bridge(tmp_path)
    saved = b.ledger.inspect('synthetic-create-1')
    assert saved['state'] == 'pending'
    assert saved['result']['counterpartyUniqueness']['status'] == 'verified_absent'
    with pytest.raises(UncertainWrite):
        create(b, operation=operation)
    assert not client.posts
    assert b.ledger.inspect('synthetic-create-1') == saved


REF_CSDL = CSDL.replace(
    '<Property Name="ИНН" Type="Edm.String"/>',
    '<Property Name="ИНН" Type="Edm.String"/><Property Name="Owner_Key" Type="Edm.Guid"/>'
    '<NavigationProperty Name="Owner" Relationship="Demo.Card_Owner" FromRole="Card" ToRole="Owner"/>',
).replace(
    '<EntityContainer Name="Demo">',
    '<EntityType Name="Owner"><Key><PropertyRef Name="Ref_Key"/></Key><Property Name="Ref_Key" Type="Edm.Guid"/></EntityType>'
    '<Association Name="Card_Owner"><End Role="Card" Type="Demo.Counterparty" Multiplicity="*"/>'
    '<End Role="Owner" Type="Demo.Owner" Multiplicity="0..1"/></Association><EntityContainer Name="Demo">',
).replace(
    '</EntityContainer>',
    '<EntitySet Name="Catalog_Owners" EntityType="Demo.Owner"/>'
    '<AssociationSet Name="Card_Owner" Association="Demo.Card_Owner">'
    '<End Role="Card" EntitySet="Catalog_Контрагенты"/><End Role="Owner" EntitySet="Catalog_Owners"/>'
    '</AssociationSet></EntityContainer>',
)


@pytest.mark.parametrize('insert', [False, True])
def test_fresh_uniqueness_follows_actual_reference_network_read(tmp_path, insert):
    b, client = bridge(tmp_path)
    b._metadata = MetadataModel.from_xml(REF_CSDL)
    events = []
    read, post = client.list_entities, client.create_entity
    def list_entities(entity_set, **kwargs):
        if entity_set == 'Catalog_Owners':
            events.append('reference')
            if insert:
                client.rows.append({'ИНН': PAYLOAD['ИНН'], 'Ref_Key': KEY})
            return {'value': [{'Ref_Key': KEY}]}
        events.append('uniqueness')
        return read(entity_set, **kwargs)
    def send(*args, **kwargs):
        events.append('POST')
        assert b.ledger.inspect('synthetic-create-1')['result']['counterpartyUniqueness']['status'] == 'verified_absent'
        return post(*args, **kwargs)
    client.list_entities, client.create_entity = list_entities, send
    if insert:
        with pytest.raises(ValueError, match='already exists'):
            create(b, {**PAYLOAD, 'Owner_Key': KEY})
        assert not client.posts
        assert events == ['reference', 'uniqueness']
    else:
        result = create(b, {**PAYLOAD, 'Owner_Key': KEY})
        assert result['referenceValidation']['status'] == 'verified'
        assert result['referenceValidation']['checkedReferenceCount'] == 1
        assert events == ['reference', 'uniqueness', 'POST']
        assert len(client.posts) == 1


@pytest.mark.parametrize('corrupt', ['stage', 'request', 'target', 'claim_missing', 'claim_request', 'pending'])
def test_prepared_status_without_exact_binding_is_never_released(tmp_path, corrupt):
    import json
    import sqlite3
    crash_at(tmp_path, 'reserve')
    b, client = bridge(tmp_path)
    saved = b.ledger.inspect('synthetic-create-1')
    result = saved['result']
    with sqlite3.connect(b.ledger.path) as conn:
        if corrupt == 'claim_missing':
            conn.execute('DELETE FROM counterparty_creates')
        elif corrupt == 'claim_request':
            conn.execute("UPDATE counterparty_creates SET request_hash='different'")
        elif corrupt == 'pending':
            conn.execute("UPDATE writes SET state='pending'")
        else:
            field = {'stage': 'counterpartyCreateStage', 'request': 'requestHash', 'target': 'targetFingerprint'}[corrupt]
            result[field] = 'unknown'
            conn.execute('UPDATE writes SET result_json=?', (json.dumps(result),))
    before = b.ledger.inspect('synthetic-create-1')
    for operation in ['synthetic-create-1', 'synthetic-recovery-new']:
        with pytest.raises(UncertainWrite):
            create(b, operation=operation)
    assert not client.posts
    assert b.ledger.inspect('synthetic-create-1') == before


def test_prepared_payload_conflict_and_pending_reconcile_do_not_send(tmp_path):
    crash_at(tmp_path, 'reserve')
    b, client = bridge(tmp_path)
    saved = b.get_write_receipt('synthetic-create-1')
    assert saved['found'] and saved['state'] == 'prepared'
    with pytest.raises(ValueError, match='different request'):
        create(b, {**PAYLOAD, 'Description': 'SYNTHETIC CHANGED'})
    with pytest.raises(ValueError, match='no pending write'):
        b.reconcile_pending_create(operation_id='synthetic-create-1', entity_set='Catalog_Контрагенты',
            payload=PAYLOAD, filter="ИНН eq '001ABC234'", expected_target=b.target_fingerprint,
            dry_run=False, confirm=True)
    assert not client.posts and not client.reads
    assert create(b)['readbackVerified']
    assert len(client.posts) == 1


def test_prepared_request_does_not_block_same_id_in_other_target(tmp_path):
    from onec_odata_mcp.server import Bridge
    from onec_odata_mcp.ledger import WriteLedger
    from onec_odata_mcp.counterparty import target_fingerprint
    from test_counterparty_create import Client
    crash_at(tmp_path, 'reserve')
    b, client = bridge(tmp_path)
    other_client = Client()
    other_client.config.odata_base_url = 'https://other.test/odata/'
    other = Bridge(other_client, WriteLedger(tmp_path / 'other.sqlite3'), MetadataModel.from_xml(CSDL))
    bindings = {x.target_fingerprint: target_fingerprint(x.client.config.odata_base_url) for x in (b, other)}
    bindings.update({v: v for v in list(bindings.values())})
    peers = lambda: (b.ledger, other.ledger)
    for x in (b, other):
        x._counterparty_ledgers = peers
        x._counterparty_coordination_ledger = b.ledger
        x._counterparty_target_bindings = bindings
    assert create(other)['readbackVerified']
    assert b.ledger.inspect('synthetic-create-1')['state'] == 'prepared'
    assert create(b)['readbackVerified']
    assert len(client.posts) == len(other_client.posts) == 1


@pytest.mark.parametrize('outcome', ['missing', 'unknown'])
def test_reference_failure_retains_failed_receipt_without_uniqueness(tmp_path, outcome):
    from onec_odata_mcp.client import ODataClientError
    b, client = bridge(tmp_path)
    b._metadata = MetadataModel.from_xml(REF_CSDL)
    events = []
    def read(entity_set, **kwargs):
        events.append(entity_set)
        assert entity_set == 'Catalog_Owners'
        if outcome == 'unknown':
            raise ODataClientError('synthetic unavailable')
        return {'value': []}
    client.list_entities = read
    result = create(b, {**PAYLOAD, 'Owner_Key': KEY})
    assert result['phase'] == 'reference_validation' and result['writeState'] == 'failed'
    assert 'counterpartyUniqueness' not in result
    assert b.ledger.inspect('synthetic-create-1')['state'] == 'failed'
    assert events == ['Catalog_Owners'] and not client.posts
    assert create(b, {**PAYLOAD, 'Owner_Key': KEY})['replayedReceipt']
    assert events == ['Catalog_Owners']


def test_prepared_receipt_cannot_complete_or_reconcile_without_transition(tmp_path):
    from onec_odata_mcp.ledger import counterparty_pre_post_result
    b, client = bridge(tmp_path)
    digest = 'synthetic-request-digest'
    marker = counterparty_pre_post_result(b.target_fingerprint, digest)
    b.ledger.reserve('synthetic-prepared-only', digest, prepared_result=marker)
    with pytest.raises(UncertainWrite):
        b.ledger.lookup('synthetic-prepared-only', digest)
    with pytest.raises(RuntimeError, match='has not entered transport'):
        b.ledger.complete('synthetic-prepared-only', {'writeState': 'accepted'})
    with pytest.raises(RuntimeError):
        b.ledger.reconcile_pending('synthetic-prepared-only', digest, 'complete', {})
    assert b.ledger.inspect('synthetic-prepared-only')['state'] == 'prepared'
    b.ledger.fail('synthetic-prepared-only', {'writeState': 'failed'})
    assert b.ledger.inspect('synthetic-prepared-only')['state'] == 'failed'
    assert not client.posts
