"""One native source, several Catalog facts; no live 1C calls or identities."""
import copy
import json

import pytest

from onec_odata_mcp.queue_checkpoint import (
    _key, _legacy_key, import_evidence, screen_rows, prepare_claim, resolve_claim,
    preview_claim, verify_preview_claim, apply_claim, cancel_claim,
)
from onec_odata_mcp.queue_manifest import QueueManifestError, _digest
from test_catalog_prerequisites import (
    candidate, absence, found, dispatch, server_receipt, created_evidence,
)
from test_queue_checkpoint import (
    candidate as document_candidate, dispatched, successful_resolution,
)


def employee(person, field='Person_Key'):
    row = copy.deepcopy(person)
    row['prepared']['entity_set'] = 'Catalog_Employees'
    row['prepared']['payload'] = {'Description': 'SYNTHETIC_EMPLOYEE', field: 'synthetic-person-key'}
    row['prerequisites'] = [{'candidate': copy.deepcopy(person), 'target_key': 'synthetic-person-key',
                             'payload_field': field}]
    return row


def saved(path):
    return json.loads(path.read_text())


def seal(path, value):
    core = {name: value[name] for name in ('schema_version', 'entries', 'claims') if name in value}
    path.write_text(json.dumps({**core, 'index_sha256': _digest(core)}))


def legacy_v3(path, row):
    """Represent the same dispatched fixture as produced by accepted schema3."""
    value = saved(path)
    old_key = _legacy_key(row)
    entry = value['entries'].pop(_key(row))
    entry.pop('catalog_entity_set')
    value['entries'][old_key] = entry
    for claim in value['claims'].values():
        for binding in claim['rows']:
            if binding['key'] == _key(row):
                binding['key'] = old_key
    value['schema_version'] = 3
    seal(path, value)
    return value


@pytest.mark.parametrize('person_field,employee_field', [
    ('Person_Key', 'Employee_Key'), ('ФизическоеЛицо_Key', 'Сотрудник_Key'),
])
def test_same_native_tuple_person_employee_document_and_handoff(tmp_path, person_field, employee_field):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    person['target_fingerprint'] = 'test-a'
    staff = employee(person, person_field)
    assert staff['source'] == person['source']
    assert staff['prepared']['source_id'] == person['prepared']['source_id']
    assert _key(person) != _key(staff)
    person_manifest, _ = dispatch(path, person, 'person')
    person_result = server_receipt(person_manifest, key='synthetic-person-key')
    resolve_claim(path, person_manifest, [person], person_result,
                  [created_evidence(person, person_manifest, person_result)])
    person_claim = copy.deepcopy(saved(path)['claims']['person'])
    staff_manifest, _ = dispatch(path, staff, 'employee')
    assert staff_manifest['rows'][0]['payload'][person_field] == person_result['result']['key']
    staff_result = server_receipt(staff_manifest, key='synthetic-employee-key')
    staff_proof = created_evidence(staff, staff_manifest, staff_result)
    resolve_claim(path, staff_manifest, [staff], staff_result, [staff_proof])
    assert saved(path)['claims']['person'] == person_claim
    # A new chat reloads the same file; it cannot reuse either completed card.
    assert screen_rows(str(path), [person, staff])['newly_created_verified'] == 2
    for row in (person, staff):
        with pytest.raises(QueueManifestError, match='reserved'):
            prepare_claim(path, [row], batch_id='another-card-batch')
    # Document identity retains its native document tuple, with verified employee link.
    document = document_candidate()
    document['prepared']['payload'][employee_field] = staff_result['result']['key']
    document['prerequisites'] = [{'candidate': staff, 'target_key': staff_result['result']['key'],
                                  'payload_field': employee_field}]
    document_manifest = dispatched(path, [document], batch='document')
    result, proofs = successful_resolution(document_manifest, [document])
    wrong = copy.deepcopy(proofs)
    wrong[0]['readback'][employee_field] = 'unknown-employee'
    with pytest.raises(QueueManifestError, match='not fully verified'):
        resolve_claim(path, document_manifest, [document], result, wrong)
    resolve_claim(path, document_manifest, [document], result, proofs)
    assert screen_rows(path, [person, staff, document])['newly_created_verified'] == 3
    before = path.read_bytes()
    assert resolve_claim(path, staff_manifest, [staff], staff_result, [staff_proof])['status'] == 'resolved'
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['payload', 'template', 'source_id', 'database'])
def test_same_fact_changes_do_not_get_another_key(tmp_path, change):
    path = tmp_path / 'private' / 'checkpoint.json'
    row = candidate()
    import_evidence(path, row, absence(row))
    changed = copy.deepcopy(row)
    if change == 'payload': changed['prepared']['payload']['Description'] = 'CHANGED'
    if change == 'template': changed['template']['version'] = '2'
    if change == 'source_id': changed['prepared']['source_id'] = 'new-display-id'
    if change == 'database': changed['database'] = 'another-alias'
    assert _key(changed) == _key(row)
    assert screen_rows(path, [changed])['held_count_by_reason'] == {'source_or_template_stale': 1}
    with pytest.raises(QueueManifestError, match='missing_verified'):
        prepare_claim(path, [changed], batch_id='changed')


@pytest.mark.parametrize('state', ['pending', 'failed'])
def test_unknown_fact_keeps_operation_and_independent_catalog_fact_can_continue(tmp_path, state):
    path = tmp_path / 'private' / 'checkpoint.json'
    row = candidate()
    manifest, args = dispatch(path, row, 'held-person')
    result = server_receipt(manifest, state=state)
    snapshot = copy.deepcopy(saved(path)['claims']['held-person'])
    with pytest.raises(QueueManifestError):
        resolve_claim(path, manifest, [row], result, [created_evidence(row, manifest, result)])
    with pytest.raises(QueueManifestError):
        import_evidence(path, row, absence(row))
    with pytest.raises(QueueManifestError):
        prepare_claim(path, [row], batch_id='retry-new-id')
    with pytest.raises(QueueManifestError):
        cancel_claim(path, batch_id='held-person', plan_sha256=manifest['plan_sha256'])
    # This unrelated fact is not a dependent employee with an invented person key.
    other = employee(row)
    other['prepared']['payload'] = {'Description': 'INDEPENDENT_SYNTHETIC_FACT'}
    other.pop('prerequisites')
    import_evidence(path, other, absence(other))
    prepare_claim(path, [other], batch_id='independent-derived')
    assert saved(path)['claims']['held-person'] == snapshot
    assert args['operation_id'] == manifest['rows'][0]['operation_id']
    assert screen_rows(path, [row])['held_count_by_reason'] == {'dispatch_uncertain': 1}


def test_equal_guids_in_different_catalogs_are_distinct_but_same_catalog_collides(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    staff = employee(person)
    staff.pop('prerequisites')
    staff['prepared']['payload'] = {'Description': 'INDEPENDENT_SYNTHETIC_FACT'}
    assert import_evidence(path, person, found(person, key='synthetic-person-key'))['status'] == 'found_existing_verified'
    assert import_evidence(path, staff, found(staff, key='synthetic-person-key'))['status'] == 'found_existing_verified'
    assert screen_rows(path, [person, staff])['found_existing_verified'] == 2
    duplicate = candidate('2')
    assert import_evidence(path, duplicate, found(duplicate, key='synthetic-person-key'))['reason'] == 'target_ambiguous'
    assert screen_rows(path, [person, duplicate])['held_count_by_reason'] == {'target_ambiguous': 2}
    assert screen_rows(path, [staff])['found_existing_verified'] == 1


def test_collision_cannot_mutate_resolved_tombstone(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    row = candidate()
    manifest, _ = dispatch(path, row)
    result = server_receipt(manifest)
    resolve_claim(path, manifest, [row], result, [created_evidence(row, manifest, result)])
    before = path.read_bytes()
    duplicate = candidate('2')
    with pytest.raises(QueueManifestError, match='resolved terminal'):
        import_evidence(path, duplicate, found(duplicate))
    assert path.read_bytes() == before
    assert screen_rows(path, [row])['newly_created_verified'] == 1


def test_legacy_dispatched_claim_resolves_on_old_key_and_sibling_uses_new_key(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    manifest, args = dispatch(path, person, 'legacy-person')
    legacy = legacy_v3(path, person)
    assert screen_rows(path, [person])['held_count_by_reason'] == {'dispatch_uncertain': 1}
    upgraded = saved(path)
    assert upgraded['schema_version'] == 4
    assert upgraded['entries'] == legacy['entries']
    assert upgraded['claims'] == legacy['claims']
    with pytest.raises(QueueManifestError):
        prepare_claim(path, [person], batch_id='legacy-retry')
    result = server_receipt(manifest, key='synthetic-person-key')
    proof = created_evidence(person, manifest, result)
    resolve_claim(path, manifest, [person], result, [proof])
    resolved_claim = copy.deepcopy(saved(path)['claims']['legacy-person'])
    staff = employee(person)
    import_evidence(path, staff, absence(staff))
    prepare_claim(path, [staff], batch_id='derived-employee')
    assert _legacy_key(person) in saved(path)['entries']
    assert _key(staff) in saved(path)['entries']
    assert saved(path)['claims']['legacy-person'] == resolved_claim
    assert args['operation_id'] == resolved_claim['manifest']['rows'][0]['operation_id']
    assert resolve_claim(path, manifest, [person], result, [proof])['status'] == 'resolved'
    changed_person = candidate(payload={'Description': 'CHANGED'})
    assert screen_rows(path, [changed_person])['held_count_by_reason'] == {'source_or_template_stale': 1}


@pytest.mark.parametrize('version', [2, 3])
def test_opaque_legacy_binding_blocks_sibling_without_reset(tmp_path, version):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    import_evidence(path, person, found(person))
    legacy = legacy_v3(path, person)
    if version == 2:
        legacy.pop('claims')
        legacy['schema_version'] = 2
        seal(path, legacy)
    assert screen_rows(path, [person])['found_existing_verified'] == 1
    before = path.read_bytes()
    staff = employee(person)
    assert screen_rows(path, [staff])['held_count_by_reason'] == {'legacy_source_binding_unknown': 1}
    for call in (lambda: import_evidence(path, staff, absence(staff)),
                 lambda: prepare_claim(path, [staff], batch_id='sibling')):
        with pytest.raises(QueueManifestError, match='legacy_source_binding_unknown'):
            call()
    assert path.read_bytes() == before
    assert saved(path)['entries'] == legacy['entries']


def test_document_claim_keys_ids_and_held_stage_survive_catalog_schema_upgrade(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    rows = [document_candidate('held-a'), document_candidate('held-b')]
    for number, row in enumerate(rows):
        dispatched(path, [row], batch=f'exhausted-synthetic-hold-{number}')
    old = saved(path)
    old['schema_version'] = 3
    seal(path, old)
    assert all(_key(row) == _legacy_key(row) for row in rows)
    person = candidate()
    import_evidence(path, person, absence(person))
    current = saved(path)
    assert current['claims'] == old['claims']
    assert {key: current['entries'][key] for key in old['entries']} == old['entries']
    for row in rows:
        with pytest.raises(QueueManifestError):
            prepare_claim(path, [row], batch_id='forbidden-retry')
    assert screen_rows(path, rows)['held_count_by_reason'] == {'dispatch_uncertain': 2}


def test_catalog_key_annotation_tampering_fails_closed(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    row = candidate()
    import_evidence(path, row, absence(row))
    value = saved(path)
    value['entries'][_key(row)]['catalog_entity_set'] = 'Catalog_Other'
    seal(path, value)
    with pytest.raises(QueueManifestError, match='inconsistent'):
        screen_rows(path, [row])


@pytest.mark.parametrize('person_state', ['absent', 'missing_verified', 'dispatch_uncertain', 'bad_readback'])
@pytest.mark.parametrize('field', ['Person_Key', 'ФизическоеЛицо_Key'])
def test_dependent_employee_needs_verified_person_before_import_or_claim(tmp_path, person_state, field):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    staff = employee(person, field)
    if person_state == 'dispatch_uncertain':
        dispatch(path, person)
    elif person_state != 'absent':
        proof = absence(person) if person_state == 'missing_verified' else found(person, key='synthetic-person-key')
        if person_state == 'bad_readback': proof['readback']['Description'] = 'WRONG'
        import_evidence(path, person, proof)
    # Create the private empty index without importing a missing employee.
    screen_rows(path, [])
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(QueueManifestError, match='prerequisite readback'):
        import_evidence(path, staff, absence(staff))
    with pytest.raises(QueueManifestError, match='prerequisite readback'):
        prepare_claim(path, [staff], batch_id='dependent-employee')
    assert (path.read_bytes() if path.exists() else None) == before


def test_derived_reference_cannot_omit_native_prerequisite_and_checks_before_apply(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    staff = employee(person)
    staff.pop('prerequisites')
    # A standalone old-style card can be prepared before this source has siblings.
    import_evidence(path, staff, absence(staff))
    manifest = prepare_claim(path, [staff], batch_id='early-employee')
    import_evidence(path, person, absence(person))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match='native prerequisite'):
        preview_claim(path, manifest)
    with pytest.raises(QueueManifestError, match='native prerequisite'):
        import_evidence(path, staff, absence(staff))
    assert path.read_bytes() == before


def test_prerequisite_change_after_preview_blocks_apply(tmp_path):
    from test_catalog_prerequisites import preview
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    import_evidence(path, person, found(person, key='synthetic-person-key'))
    staff = employee(person)
    import_evidence(path, staff, absence(staff))
    manifest = prepare_claim(path, [staff], batch_id='employee')
    request = preview_claim(path, manifest)
    receipt = verify_preview_claim(path, manifest, request, preview(manifest))
    # Conflicting person evidence is sticky, even though an employee preview exists.
    import_evidence(path, person, found(person, key='another-person-key'))
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match='prerequisite readback'):
        apply_claim(path, manifest, receipt, target_fingerprint=staff['target_fingerprint'],
                    template_id=staff['template']['id'], template_version=staff['template']['version'])
    assert path.read_bytes() == before
    assert saved(path)['claims']['employee']['stage'] == 'verified_preview'


def test_another_native_fact_cannot_hide_unknown_person_dependency(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    import_evidence(path, person, absence(person))
    other = copy.deepcopy(person)
    other['prepared']['entity_set'] = 'Catalog_AnotherFact'
    other['prepared']['payload'] = {'Description': 'SYNTHETIC_OTHER'}
    import_evidence(path, other, found(other, key='synthetic-other-key'))
    staff = employee(person)
    staff['prepared']['payload']['Other_Key'] = 'synthetic-other-key'
    staff['prerequisites'] = [{'candidate': other, 'target_key': 'synthetic-other-key',
                              'payload_field': 'Other_Key'}]
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match='native prerequisite'):
        import_evidence(path, staff, absence(staff))
    assert path.read_bytes() == before


@pytest.mark.parametrize('field', ['statement', 'line'])
@pytest.mark.parametrize('representations', [(1, '1'), ('1', 1)])
@pytest.mark.parametrize('person_state', ['missing_verified', 'held', 'dispatch_uncertain'])
@pytest.mark.parametrize('stage', ['import', 'screen', 'prepare', 'preview', 'verify-preview', 'apply', 'resolve'])
def test_equivalent_source_representations_cannot_bypass_unknown_native_prerequisite(
        tmp_path, field, representations, person_state, stage):
    from onec_odata_mcp.queue_checkpoint import _source
    from test_catalog_prerequisites import preview
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    person['source'][field] = representations[0]
    staff = employee(person)
    staff.pop('prerequisites')
    staff['source'][field] = representations[1]
    assert person['source'] != staff['source']
    assert _source(person) == _source(staff)
    # A standalone card issued before a native sibling arrived must recheck
    # that sibling before each subsequent stage, including final resolution.
    import_evidence(path, staff, absence(staff))
    manifest = prepare_claim(path, [staff], batch_id='early-employee')
    request = receipt = None
    if stage in {'verify-preview', 'apply', 'resolve'}:
        request = preview_claim(path, manifest)
    if stage in {'apply', 'resolve'}:
        receipt = verify_preview_claim(path, manifest, request, preview(manifest))
    if stage == 'resolve':
        apply_claim(path, manifest, receipt, target_fingerprint=staff['target_fingerprint'],
                    template_id=staff['template']['id'], template_version=staff['template']['version'])
    if person_state == 'dispatch_uncertain':
        dispatch(path, person, 'unknown-person')
    else:
        proof = absence(person) if person_state == 'missing_verified' else found(person)
        if person_state == 'held':
            proof['readback']['Description'] = 'SYNTHETIC_WRONG'
        import_evidence(path, person, proof)
    before = path.read_bytes()
    before_entries = copy.deepcopy(saved(path)['entries'])
    before_claims = copy.deepcopy(saved(path)['claims'])
    if stage == 'screen':
        assert screen_rows(path, [staff])['held_count_by_reason'] == {
            'derived Catalog reference needs an exact native prerequisite': 1}
    else:
        calls = {
            'import': lambda: import_evidence(path, staff, absence(staff)),
            'prepare': lambda: prepare_claim(path, [staff], batch_id='another-employee'),
            'preview': lambda: preview_claim(path, manifest),
            'verify-preview': lambda: verify_preview_claim(path, manifest, request, preview(manifest)),
            'apply': lambda: apply_claim(path, manifest, receipt,
                target_fingerprint=staff['target_fingerprint'], template_id=staff['template']['id'],
                template_version=staff['template']['version']),
            'resolve': lambda: resolve_claim(path, manifest, [staff], server_receipt(manifest),
                [created_evidence(staff, manifest, server_receipt(manifest))]),
        }
        with pytest.raises(QueueManifestError, match='native prerequisite'):
            calls[stage]()
    assert path.read_bytes() == before
    assert saved(path)['entries'] == before_entries
    assert saved(path)['claims'] == before_claims


@pytest.mark.parametrize('field', ['statement', 'line'])
@pytest.mark.parametrize('representations', [(1, '1'), ('1', 1)])
def test_equivalent_native_source_accepts_exact_verified_binding_without_rehash(tmp_path, field, representations):
    from onec_odata_mcp.queue_checkpoint import _source
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    person['source'][field] = representations[0]
    import_evidence(path, person, found(person, key='synthetic-person-key'))
    prior = copy.deepcopy(saved(path)['entries'][_key(person)])
    staff = employee(person)
    staff['source'][field] = representations[1]
    assert _source(staff) == _source(person)
    assert staff['prerequisites'][0]['candidate'] == person
    manifest, args = dispatch(path, staff, 'employee-with-exact-binding')
    result = server_receipt(manifest, key='synthetic-employee-key')
    proof = created_evidence(staff, manifest, result)
    assert resolve_claim(path, manifest, [staff], result, [proof])['status'] == 'resolved'
    assert screen_rows(path, [person, staff])['found_existing_verified'] == 1
    assert screen_rows(path, [person, staff])['newly_created_verified'] == 1
    assert saved(path)['entries'][_key(person)] == prior
    assert prior['source'][field] == representations[0]
    assert prior['candidate_sha256'] == _digest(person)
    assert args['operation_id'] == manifest['rows'][0]['operation_id']
    # Canonical identity equality does not authorize rewriting a proof's raw
    # candidate or digest, even when the resulting checkpoint key is identical.
    changed_binding = copy.deepcopy(staff)
    changed_binding['prerequisites'][0]['candidate']['source'][field] = representations[1]
    assert _key(changed_binding['prerequisites'][0]['candidate']) == _key(person)
    assert _digest(changed_binding['prerequisites'][0]['candidate']) != _digest(person)
    before = path.read_bytes()
    with pytest.raises(QueueManifestError, match='prerequisite readback'):
        import_evidence(path, changed_binding, absence(changed_binding))
    assert path.read_bytes() == before
