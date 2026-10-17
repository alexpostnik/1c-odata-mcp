"""Additive confirmed identity, never a tax identifier or a source repair.

The private reviewer verifies actual evidence; these checks only establish
closed shape, exact source binding, and internally consistent assignments.
"""
import copy
import hashlib
import json
import re
from datetime import datetime

PROPERTY = 'owner_counterparty_identity'
PREFIX = r'internal-counterparty:[1-9][0-9]*'
# These are interpretation fields. Everything else participates in source binding.
ADDITIVE = {'processing', PROPERTY, 'counterparty_clarification', 'owner_counterparty_type', 'one_c_rko'}
RESOLVABLE = {'missing_or_unreadable_unp', 'unp_source_assertion_missing_or_conflicting',
              'counterparty_unp_raw_source_observation_missing_or_ambiguous',
              'counterparty_not_source_observed', 'counterparty_party_disagreement'}


def source_digest(graph, operation):
    nodes = {n['id']: n for n in graph['nodes']}
    links = [e for e in graph['edges'] if e['from'] == operation['id'] or e['to'] == operation['id']]
    related = {operation['id']} | {e['from'] for e in links} | {e['to'] for e in links}
    originals = []
    for ident in sorted(related):
        n = copy.deepcopy(nodes[ident])
        if n['type'] == 'Operation':
            n['properties'] = {k:v for k,v in n['properties'].items() if k not in ADDITIVE}
        for field in ('observations', 'unrecognized_fields'):
            n[field] = sorted(n.get(field, []), key=lambda v:json.dumps(v, ensure_ascii=False, sort_keys=True))
        originals.append(n)
    payload = {'nodes': originals, 'edges': sorted(links, key=lambda e:e['id'])}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def shape_valid(answer):
    required = {'operation_id', 'role', 'internal_id', 'confirmed_entity_ref', 'confirmed_name',
                'original_detail_raw', 'business_facts_sha256', 'answered_by', 'answered_at',
                'answer_ref', 'confirmation_ref', 'origin', 'business_relation'}
    if not isinstance(answer, dict) or set(answer) != required:
        return False
    if (answer['origin'] != 'owner_confirmed' or answer['role'] not in ('payer', 'payee') or
            not isinstance(answer['internal_id'], str) or not re.fullmatch(PREFIX, answer['internal_id']) or
            not isinstance(answer['business_facts_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', answer['business_facts_sha256'])):
        return False
    for key in required - {'business_relation'}:
        if not isinstance(answer[key], str) or not answer[key].strip():
            return False
    if answer['business_relation'] is not None and (not isinstance(answer['business_relation'], str) or not answer['business_relation'].strip()):
        return False
    try:
        return datetime.fromisoformat(answer['answered_at'].replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def collisions(answers):
    """Explicit entity reference is required; names/accounts/amounts never group."""
    ids, entities = {}, {}
    conflicts = set()
    for answer in answers:
        if not shape_valid(answer):
            conflicts.add(answer.get('operation_id') if isinstance(answer, dict) else None)
            continue
        ident, entity = answer['internal_id'], answer['confirmed_entity_ref']
        signature = (entity, answer['confirmed_name'])
        if ident in ids and ids[ident][0] != signature:
            conflicts.update(ids[ident][1]); conflicts.add(answer['operation_id'])
        if entity in entities and entities[entity][0] != (ident, answer['confirmed_name']):
            conflicts.update(entities[entity][1]); conflicts.add(answer['operation_id'])
        ids.setdefault(ident, (signature, []))[1].append(answer['operation_id'])
        entities.setdefault(entity, ((ident, answer['confirmed_name']), []))[1].append(answer['operation_id'])
    return conflicts


def valid(graph, op, role, source, metadata, nodes, role_links, holders, modern_profile, sf):
    import archive_adapter as adapter
    a = op['properties'].get(PROPERTY)
    cp = op['properties'].get('counterparty') or {}
    if not shape_valid(a) or not source or source.get('status') != 'observed':
        return False
    if (a['operation_id'] != op['id'] or a['role'] != role or cp.get('role') != role or
            a['original_detail_raw'] != source['raw'] or a['business_facts_sha256'] != source_digest(graph, op) or
            not adapter.readable_name(a['confirmed_name']) or not adapter.readable_name(cp.get('name_raw'))):
        return False
    # Only an explicitly absent/zero source value: damaged or repeated values
    # and an already confirmed real UNP require the existing clarification path.
    values = metadata.get('УНП '+('плательщика:' if role == 'payer' else 'получателя:'), [])
    if (len(values) != 1 or not isinstance(values[0], str) or
            values[0].strip() not in ('', '0', '000000000') or cp.get('unp_raw') not in (None, '0', '000000000') or
            op['properties'].get('counterparty_clarification') is not None):
        return False
    if metadata.get('Наименование корреспондента:', []) != [cp['name_raw']]:
        return False
    if len(role_links) != 1 or role_links[0]['to'] != cp.get('party_id'):
        return False
    party = nodes.get(cp.get('party_id'))
    if not party or party['type'] != 'Party' or party['id'] in holders:
        return False
    pp = party['properties']
    if pp.get('role_raw') != role or pp.get('name_raw') != cp['name_raw'] or pp.get('unp_raw') != cp.get('unp_raw'):
        return False
    for key in ('name_raw', 'unp_raw'):
        obs = [o for o in party.get('observations', []) if o['field'] == key]
        if len(obs) != 1 or not isinstance(obs[0]['raw'], str) or obs[0]['normalized'] != cp[key]:
            return False
        if key == 'unp_raw':
            if obs[0]['raw'].strip() != values[0].strip() or obs[0]['status'] not in ('observed', 'unknown', 'absent'):
                return False
        elif obs[0]['status'] != 'observed':
            return False
        elif modern_profile:
            import modern_adapter as modern
            if obs[0]['raw'] != sf.get('name_raw') or not modern.name_observation_matches(obs[0]['raw'], cp[key]):
                return False
        elif obs[0]['raw'] != cp[key]:
            return False
    return True


def annotate(graph, answers, verified_confirmations, existing_assignments=()):
    """Caller-supplied independently reviewed evidence is compared exactly.

    This is not authentication. Never create both mappings from an unchecked
    claim; review the real private owner evidence first. Registry spans all
    previously confirmed entities, including those in other graphs.
    """
    import statement_graph as sg
    sg.require_processing_admission(graph)
    if not sg.validate(graph)['valid'] or not isinstance(answers, dict) or not isinstance(verified_confirmations, dict):
        raise ValueError('current consistent graph and independently verified confirmations required')
    result = copy.deepcopy(graph)
    operations = {n['id']: n for n in result['nodes'] if n['type'] == 'Operation'}
    for ident, a in answers.items():
        if ident not in operations or not shape_valid(a) or verified_confirmations.get(ident) != a:
            raise ValueError('existing row and exact independently reviewed owner confirmation required')
        previous = operations[ident]['properties'].get(PROPERTY)
        if previous is not None and previous != a:
            raise ValueError('existing confirmed identity cannot be silently replaced')
        operations[ident]['properties'][PROPERTY] = copy.deepcopy(a)
    assignments = list(existing_assignments) + [n['properties'][PROPERTY] for n in operations.values() if PROPERTY in n['properties']]
    if collisions(assignments):
        raise ValueError('internal number/entity/name collision or conflict')
    sg.materialize_processing(result)
    if not sg.validate(result)['valid']:
        raise ValueError('internal identity conflicts with exact original source/provenance')
    sg.require_processing_admission(result)
    return result


ROLE_PROPERTY = 'owner_role_unps'
ADDITIVE.add(ROLE_PROPERTY)


def role_shape_valid(a):
    required = {'operation_id', 'role', 'party_id', 'official_unp', 'original_unp_raw',
                'original_detail_raw', 'business_facts_sha256', 'answered_by', 'answered_at',
                'answer_ref', 'confirmation_ref', 'origin'}
    if not isinstance(a, dict) or set(a) not in (required, required | {'correction'}):
        return False
    if 'correction' in a and not correction_shape_valid(a['correction']):
        return False
    if a['role'] not in ('payer', 'payee') or a['origin'] != 'owner_confirmed':
        return False
    for k in required:
        if k == 'official_unp' and a[k] is None:
            continue
        if not isinstance(a[k], str) or (k != 'original_unp_raw' and not a[k].strip()):
            return False
    import archive_adapter as adapter
    pending = a.get('correction', {}).get('status') == 'pending'
    if (pending and a['official_unp'] is not None) or (not pending and not adapter.readable_unp(a['official_unp'])) or not re.fullmatch('[0-9a-f]{64}', a['business_facts_sha256']):
        return False
    try:
        return datetime.fromisoformat(a['answered_at'].replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def role_valid(graph, op, a, source, metadata, nodes, role_edges):
    if not role_shape_valid(a) or not source or source.get('status') != 'observed':
        return False
    if (a['operation_id'] != op['id'] or a['original_detail_raw'] != source['raw'] or
            a['business_facts_sha256'] != source_digest(graph, op)):
        return False
    links = role_edges.get((op['id'], a['role'].upper()), [])
    if len(links) != 1 or links[0]['to'] != a['party_id']:
        return False
    party = nodes.get(a['party_id'])
    if not party or party['type'] != 'Party' or party['properties'].get('role_raw') != a['role']:
        return False
    label = 'УНП '+('плательщика:' if a['role'] == 'payer' else 'получателя:')
    if metadata.get(label, []) != [a['original_unp_raw']]:
        return False
    raw = a['original_unp_raw']
    import archive_adapter as adapter
    replacement = raw.replace(' ', '') != a['official_unp'] and raw.strip() not in ('', '0', '000000000')
    if replacement:
        if (not adapter.readable_unp(raw.replace(' ', '')) or
                not correction_valid(graph, op, a, party, nodes)):
            return False
    elif 'correction' in a:
        # A correction cannot invent a dispute over a blank/zero or unchanged value.
        return False
    obs = [o for o in party.get('observations', []) if o['field'] == 'unp_raw']
    if (len(obs) != 1 or obs[0]['raw'] != raw or obs[0]['normalized'] != party['properties'].get('unp_raw') or
            obs[0]['status'] not in ('unknown', 'absent', 'observed')):
        return False
    cp = op['properties'].get('counterparty') or {}
    if cp.get('role') == a['role'] and cp.get('name_raw') != party['properties'].get('name_raw'):
        return False
    clarification = op['properties'].get('counterparty_clarification')
    if cp.get('role') == a['role'] and clarification and clarification.get('unp_raw') != a['official_unp']:
        return False
    if cp.get('role') == a['role'] and PROPERTY in op['properties']:
        return False
    return True


def role_answers_valid(graph, op, source, metadata, nodes, role_edges):
    answers = op['properties'].get(ROLE_PROPERTY)
    return (isinstance(answers, list) and bool(answers) and
            all(role_valid(graph, op, a, source, metadata, nodes, role_edges) for a in answers) and
            len({a['role'] for a in answers}) == len(answers))


def annotate_role_unps(graph, answers, verified_confirmations, existing_assignments=()):
    """Add exact official per-role owner facts; preserve source and other holds.

    Counterparty readiness still uses the existing clarification path. Own-side
    role identity is an independent annotation, never a header substitution.
    """
    import statement_graph as sg
    sg.require_processing_admission(graph)
    if not sg.validate(graph)['valid'] or not isinstance(answers, dict) or not isinstance(verified_confirmations, dict):
        raise ValueError('current graph and independently verified role confirmations required')
    result = copy.deepcopy(graph)
    ops = {n['id']:n for n in result['nodes'] if n['type'] == 'Operation'}
    for ident, values in answers.items():
        if (ident not in ops or not isinstance(values, list) or not values or
                not all(role_shape_valid(a) for a in values) or verified_confirmations.get(ident) != values):
            raise ValueError('existing row and exact reviewed official role confirmations required')
        previous = ops[ident]['properties'].get(ROLE_PROPERTY)
        if previous is not None and previous != values and not pending_resolution(previous, values):
            raise ValueError('existing official role confirmation cannot be silently replaced')
        ops[ident]['properties'][ROLE_PROPERTY] = copy.deepcopy(values)
    assignments = list(existing_assignments) + [a for n in ops.values() for a in n['properties'].get(ROLE_PROPERTY, [])]
    if role_collisions(assignments):
        raise ValueError('same Party/role has conflicting official UNP assignments')
    sg.materialize_processing(result)
    if not sg.validate(result)['valid']:
        raise ValueError('official role identity conflicts with exact source/role/Party/provenance')
    sg.require_processing_admission(result)
    return result


def effective_parties(op):
    """Only validated annotations may supply official own-side identities.

    Consumer must validate full graph before calling; no raw zero fallback.
    """
    values = op['properties'].get(ROLE_PROPERTY, [])
    if not isinstance(values, list) or not all(role_shape_valid(a) for a in values):
        return []
    return [{'role':a['role'], 'party_id':a['party_id'], 'official_unp':a['official_unp'],
             'origin':a['origin'], **({'correction_kind':a['correction']['kind'], 'resolution_status':a['correction']['status']} if 'correction' in a else {})} for a in values]


def role_collisions(answers):
    assigned = {}
    conflicts = set()
    for a in answers:
        if not role_shape_valid(a):
            conflicts.add(a.get('operation_id') if isinstance(a, dict) else None)
            continue
        if a['official_unp'] is None:
            continue
        key = (a['party_id'], a['role'])
        if key in assigned and assigned[key][0] != a['official_unp']:
            conflicts.update(assigned[key][1]); conflicts.add(a['operation_id'])
        assigned.setdefault(key, (a['official_unp'], []))[1].append(a['operation_id'])
    return conflicts


CORRECTION_KEYS = {'kind', 'dispute_ref', 'replacement_evidence_ref', 'account_id',
                   'account_number_raw', 'name_raw', 'replacement_evidence', 'status'}


def correction_shape_valid(value):
    if not isinstance(value, dict) or set(value) != CORRECTION_KEYS or value['kind'] != 'owner_disputed_source':
        return False
    if value['status'] not in ('pending', 'confirmed'):
        return False
    if any(not isinstance(value[k], str) or not value[k].strip() for k in CORRECTION_KEYS - {'replacement_evidence', 'replacement_evidence_ref'}):
        return False
    if value['status'] == 'pending':
        return value['replacement_evidence_ref'] is None and value['replacement_evidence'] is None
    if not isinstance(value['replacement_evidence_ref'], str) or not value['replacement_evidence_ref'].strip():
        return False
    evidence = value['replacement_evidence']
    return (isinstance(evidence, dict) and set(evidence) == {'official_unp', 'account_number_raw', 'name_raw'} and
            all(isinstance(v, str) and v.strip() for v in evidence.values()))


def correction_valid(graph, op, a, party, nodes):
    """Owner-disputed original remains observed; confirmed working correction is separate.

    The caller verifies the actual dispute and independent replacement evidence.
    Exact name/account evidence cannot be substituted by an amount/UNP match.
    """
    c = a.get('correction')
    if not correction_shape_valid(c):
        return False
    links = [e for e in graph['edges'] if e['from'] == op['id'] and
             e['type'] == ('TO_ACCOUNT' if a['role'] == 'payee' else 'FROM_ACCOUNT')]
    if len(links) != 1 or links[0]['to'] != c['account_id']:
        return False
    account = nodes.get(c['account_id'])
    if not account or account['type'] != 'Account':
        return False
    if (account['properties'].get('number_raw') != c['account_number_raw'] or
            party['properties'].get('name_raw') != c['name_raw']):
        return False
    import archive_adapter as adapter
    if not adapter.readable_name(c['name_raw']):
        return False
    if c['status'] == 'confirmed' and c['replacement_evidence'] != {'official_unp':a['official_unp'],
                                    'account_number_raw':c['account_number_raw'], 'name_raw':c['name_raw']}:
        return False
    if not account_source_matches(graph, op, a['role'], account, c['account_number_raw']):
        return False
    name_obs = [o for o in party.get('observations', []) if o['field'] == 'name_raw']
    if len(name_obs) != 1 or name_obs[0]['status'] != 'observed' or name_obs[0]['normalized'] != c['name_raw']:
        return False
    return True


def pending_resolution(previous, values):
    """Only a bound pending dispute can advance to a confirmed replacement."""
    if not isinstance(previous, list) or not isinstance(values, list) or len(previous) != len(values):
        return False
    old_by_role = {a.get('role'):a for a in previous if isinstance(a, dict)}
    if len(old_by_role) != len(previous):
        return False
    for a in values:
        old = old_by_role.get(a.get('role'))
        if old == a:
            continue
        if (not role_shape_valid(old) or old.get('correction', {}).get('status') != 'pending' or
                a.get('correction', {}).get('status') != 'confirmed'):
            return False
        bindings = ('operation_id', 'role', 'party_id', 'original_unp_raw', 'original_detail_raw', 'business_facts_sha256')
        if any(old[k] != a[k] for k in bindings):
            return False
        if any(old['correction'][k] != a['correction'][k] for k in ('kind','dispute_ref','account_id','account_number_raw','name_raw')):
            return False
    return True


def account_source_matches(graph, op, role, account, number):
    """Canonical legacy observation OR reviewed modern typed/row raw evidence.

    Empty Account observations alone prove nothing. Modern evidence must bind
    the actual role account to the supported source profile and unique retained
    raw account/bank cells; every typed account field must reproduce that source.
    """
    import bank_identifiers as bi
    p = account['properties']
    observations = account.get('observations', [])
    values = [o for o in observations if o['field'] == 'number_raw']
    if values:
        if len(values) != 1 or values[0]['status'] != 'observed' or values[0]['normalized'] != number:
            return False
        identifier = bi.recognize(values[0]['raw'], 'account')
        return (identifier['normalized'] == number and bi.usable(identifier) and
                ('identifier' not in p or p['identifier'] == identifier))
    if observations != [] or {'inflow':'payer', 'outflow':'payee'}.get(op['properties'].get('direction')) != role:
        return False
    import modern_adapter as modern
    statements = [n for n in graph['nodes'] if n['type'] == 'Statement' and n['id'] == op['properties']['statement_id']]
    operations = [n for n in graph['nodes'] if n['type'] == 'Operation' and n['properties']['statement_id'] == op['properties']['statement_id']]
    if len(statements) != 1 or not modern.format_supported(statements[0], operations):
        return False
    raw_accounts = [o for o in op.get('observations', []) if o['field'] == 'modern_account_raw']
    raw_banks = [o for o in op.get('observations', []) if o['field'] == 'modern_bank_code_raw']
    if (len(raw_accounts) != 1 or raw_accounts[0]['status'] != 'observed' or
            len(raw_banks) != 1 or raw_banks[0]['status'] != 'observed'):
        return False
    identifier = bi.recognize(raw_accounts[0]['raw'], 'account')
    bank_identifier = bi.recognize(raw_banks[0]['raw'], 'bank')
    expected_format = 'iban' if identifier['format'] == 'iban' else 'historical' if identifier['format'] == 'historical_numeric' else 'unknown'
    expected = {'identifier':identifier, 'number_raw':number, 'format':expected_format,
                'identity_status':'observed',
                'iban_raw':identifier['raw'] if identifier['format'] == 'iban' else None,
                'bic_raw':bank_identifier['raw'] if bank_identifier['format'] == 'bic' else None}
    return (bi.usable(identifier) and identifier['normalized'] == number and
            all(k in p and p[k] == v for k,v in expected.items()))
