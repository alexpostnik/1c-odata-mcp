"""Bounded Unicode bindings, using only synthetic offline candidates/evidence."""
import copy

import pytest

from onec_odata_mcp.queue_checkpoint import (
    _candidate, apply_claim, cancel_claim, import_evidence, prepare_claim, screen_rows,
)
from onec_odata_mcp.queue_manifest import QueueManifestError, _required_text
from test_catalog_derived_claims import employee, saved
from test_catalog_prerequisites import candidate, absence, found, dispatch
from test_queue_checkpoint import candidate as document_candidate


@pytest.mark.parametrize('field', ['ФизическоеЛицо_Key', 'Сотрудник_Key',
                                  'Ж' * 252 + '_Key', 'И\u0306мя_Key'])
def test_reference_identifier_is_bounded_and_preserves_exact_spelling(field):
    person = candidate()
    staff = employee(person, field)
    original = copy.deepcopy(staff)
    assert _candidate(staff) == original
    assert staff == original
    # This exception belongs only to payload_field, never shared identifiers.
    with pytest.raises(QueueManifestError, match='ASCII'):
        _required_text(field, 'synthetic-id')


@pytest.mark.parametrize('field', [
    None, 7, [], '', '1Сотрудник_Key', 'ФизическоеЛицо', 'ФизическоеЛицо_key',
    'Ж' * 253 + '_Key', ' Person_Key', 'Person_Key ', 'Person Key',
    'Person\t_Key', 'Person\n_Key', 'Person\r_Key', 'Person\x00_Key',
    'Person\x7f_Key', 'Person\u200b_Key', 'Person\u202e_Key', 'Person\u00a0_Key',
    'Person/Other_Key', 'Person\\Other_Key', '../Person_Key', 'Person.Other_Key',
    'Person?x_Key', 'Person#x_Key', 'Person%2f_Key', 'https://Person_Key',
])
def test_invalid_reference_fields_fail_before_checkpoint_mutation(tmp_path, field):
    # Document payloads do not have the Catalog name guard, so this exercises
    # the prerequisite-field validator itself, including path/control syntax.
    person = candidate()
    row = document_candidate()
    row['target_fingerprint'] = person['target_fingerprint']
    row['prerequisites'] = [{'candidate': person, 'target_key': 'synthetic-person-key',
                             'payload_field': field}]
    if isinstance(field, str):
        row['prepared']['payload'][field] = 'synthetic-person-key'
    path = tmp_path / 'private' / 'checkpoint.json'
    with pytest.raises(QueueManifestError):
        _candidate(row)
    with pytest.raises(QueueManifestError):
        import_evidence(path, row, absence(row))
    assert not path.exists()


@pytest.mark.parametrize('change', ['missing', 'mismatch', 'duplicate', 'target',
                                    'database', 'database_absent', 'candidate'])
def test_unicode_binding_retains_exact_shape_and_target_guards(tmp_path, change):
    person = candidate()
    person['database'] = 'test'
    staff = employee(person, 'ФизическоеЛицо_Key')
    binding = staff['prerequisites'][0]
    if change == 'missing':
        del staff['prepared']['payload']['ФизическоеЛицо_Key']
    elif change == 'mismatch':
        staff['prepared']['payload']['ФизическоеЛицо_Key'] = 'different-person-key'
    elif change == 'duplicate':
        staff['prerequisites'].append(copy.deepcopy(binding))
    elif change == 'target':
        binding['candidate']['target_fingerprint'] = 'other-test'
    elif change == 'database':
        binding['candidate']['database'] = 'other-test-alias'
    elif change == 'database_absent':
        del binding['candidate']['database']
    elif change == 'candidate':
        binding['candidate'] = document_candidate()
    path = tmp_path / 'private' / 'checkpoint.json'
    with pytest.raises(QueueManifestError):
        import_evidence(path, staff, absence(staff))
    assert not path.exists()


@pytest.mark.parametrize('field', ['ФизическоеЛицо_Key', 'Сотрудник_Key'])
def test_unicode_dependency_requires_original_exact_candidate(tmp_path, field):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    import_evidence(path, person, found(person, key='synthetic-person-key'))
    staff = employee(person, field)
    staff['prerequisites'][0]['candidate']['template']['version'] = '2'
    before = path.read_bytes()
    for call in (lambda: import_evidence(path, staff, absence(staff)),
                 lambda: prepare_claim(path, [staff], batch_id='stale-dependent')):
        with pytest.raises(QueueManifestError, match='prerequisite readback'):
            call()
    assert path.read_bytes() == before


def test_unresolved_unicode_employee_claim_cannot_reset_cancel_or_dispatch_again(tmp_path):
    path = tmp_path / 'private' / 'checkpoint.json'
    person = candidate()
    import_evidence(path, person, found(person, key='synthetic-person-key'))
    staff = employee(person, 'ФизическоеЛицо_Key')
    manifest, args = dispatch(path, staff, 'held-unicode-employee')
    original_claim = copy.deepcopy(saved(path)['claims']['held-unicode-employee'])
    before = path.read_bytes()
    for call in (
        lambda: import_evidence(path, staff, absence(staff)),
        lambda: prepare_claim(path, [staff], batch_id='replacement-employee'),
        lambda: cancel_claim(path, batch_id=manifest['batch_id'], plan_sha256=manifest['plan_sha256']),
        lambda: apply_claim(path, manifest, {}, target_fingerprint=staff['target_fingerprint'],
                            template_id=staff['template']['id'], template_version='1'),
    ):
        with pytest.raises(QueueManifestError):
            call()
    assert path.read_bytes() == before
    assert saved(path)['claims']['held-unicode-employee'] == original_claim
    assert args['operation_id'] == manifest['rows'][0]['operation_id']
    assert screen_rows(path, [staff])['held_count_by_reason'] == {'dispatch_uncertain': 1}


@pytest.mark.parametrize('location', ['namespace', 'target', 'template', 'source_id', 'target_key'])
def test_shared_candidate_identifiers_still_reject_unicode(location):
    person = candidate()
    staff = employee(person, 'ФизическоеЛицо_Key')
    if location == 'namespace':
        staff['source']['namespace'] = 'Источник'
    elif location == 'target':
        staff['target_fingerprint'] = 'Цель'
    elif location == 'template':
        staff['template']['id'] = 'Шаблон'
    elif location == 'source_id':
        staff['prepared']['source_id'] = 'Строка'
    else:
        staff['prerequisites'][0]['target_key'] = 'Ключ'
        staff['prepared']['payload']['ФизическоеЛицо_Key'] = 'Ключ'
    with pytest.raises(QueueManifestError, match='ASCII'):
        _candidate(staff)
