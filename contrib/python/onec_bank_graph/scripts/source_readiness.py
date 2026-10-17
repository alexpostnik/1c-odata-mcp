#!/usr/bin/env python3
"""Read a reviewed source graph, independently of future processing readiness.

The reconciliation file is a review receipt, not self-authenticating PDF proof.
It must be supplied by an independently checked source review and kept unchanged.
All original geometry, PDF bindings and field-review evidence remain private.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
from decimal import Decimal, localcontext
from datetime import date, datetime
from pathlib import Path
import statement_graph as sg

PROFILE = 'bank-source-readiness-v1'


def fact_fingerprint(node):
    value = copy.deepcopy(node)
    # Later processing decisions cannot change the authenticated bank facts.
    for key in ('processing', 'counterparty_clarification', 'one_c_rko',
                'owner_counterparty_type', 'owner_counterparty_identity', 'owner_role_unps'):
        value['properties'].pop(key, None)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()


def edge_fingerprint(edges):
    return hashlib.sha256(json.dumps(sorted(edges, key=lambda e: e['id']),
                          ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode()).hexdigest()


def control_fingerprint(controls):
    return hashlib.sha256(json.dumps(controls, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()


def semantic_cash_projection(raw):
    """Only the reviewed bounded WinAnsi ESC4/5 cash service pair.

    This function is used only after a row-bound original-resource review.
    Other CID tokens, incomplete pairs and arbitrary control removal are refused.
    """
    if not isinstance(raw, str) or not any(token in raw for token in ('(cid:27)', '\x1b')):
        raise ValueError('missing reviewed source service token')
    lines = raw.split('\n')
    found = 0
    for index, line in enumerate(lines):
        if '(cid:27)' not in line and '\x1b' not in line:
            continue
        matched_line = line.replace('(cid:27)4', '\x1b4').replace('(cid:27)5', '\x1b5')
        match = re.fullmatch(r'(\s*)\x1b4(символ\s+\d+\s*:\s*\d+(?:[.,]\d+)?)\x1b5(\s*)', matched_line)
        if not match:
            raise ValueError('service token outside proven complete cash subline')
        lines[index] = ''.join(match.groups())
        found += line.count('(cid:27)') + line.count('\x1b')
    if found != raw.count('(cid:27)') + raw.count('\x1b'):
        raise ValueError('unreviewed service token')
    value = '\n'.join(lines)
    if '(cid:' in value or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
        raise ValueError('unreviewed source service glyph/control')
    return value


def review_matches(graph, review):
    errors = []
    if review.get('profile') != PROFILE or review.get('source_review') != 'independent_pdf_review':
        errors.append('missing independent source review')
    actual = {n['id']: fact_fingerprint(n) for n in graph['nodes']}
    expected = review.get('node_fingerprints')
    if not isinstance(expected, dict) or actual != expected:
        errors.append('source field/node reconciliation mismatch')
    if edge_fingerprint(graph['edges']) != review.get('edge_fingerprint'):
        errors.append('source role/reference reconciliation mismatch')
    operations = [n for n in graph['nodes'] if n['type'] == 'Operation']
    node_ids = [n['id'] for n in graph['nodes']]
    if len(node_ids) != len(set(node_ids)):
        errors.append('duplicate source node ID')
    projections = review.get('service_projections', {})
    projection_ids = {n['id'] for n in operations if 'source_text' in n['properties']}
    if not isinstance(projections, dict) or set(projections) != projection_ids:
        errors.append('source service projection evidence parity mismatch')

    if len(operations) != review.get('operations'):
        errors.append('independent source row count mismatch')
    if review.get('full_field_coverage') is not True or review.get('unresolved_source_errors') != []:
        errors.append('source field coverage unproven')
    for op in operations:
        p = op['properties']
        if p.get('purpose_status') != 'observed' or p.get('row_status') != 'complete':
            errors.append('unresolved source row/boundary: ' + op['id'])
        if p.get('purpose_raw') != p.get('payment1c_comment_raw'):
            errors.append('raw purpose/comment mismatch: ' + op['id'])
        if 'modern_fields' in p:
            import modern_adapter
            if 'modern_transaction_date_unresolved' in modern_adapter.source_fields(op)['gaps']:
                errors.append('unresolved source transaction date: ' + op['id'])
        projection = p.get('source_text')
        if 'source_text' in p:
            evidence = projections.get(op['id']) if isinstance(projections, dict) else None
            if not isinstance(evidence, dict) or evidence.get('resource_bound') is not True:
                errors.append('source service projection unproven: ' + op['id'])
            try:
                value = semantic_cash_projection(p['purpose_raw'])
                if projection != {'profile': 'source-bound-winansi-1b-cash-service-v1',
                                  'value': value, 'raw_preserved': True}:
                    errors.append('non-lossless source projection: ' + op['id'])
            except ValueError as exc:
                errors.append(str(exc) + ': ' + op['id'])
    return errors


def source_unit_matches_statement(graph, statement, unit):
    """Bind reviewed units to the retained source, without inferring/converting.

    Only the two reviewed Belarusian epochs are supported by this source reader.
    A fixture tuple or a fingerprint is not evidence for an actual printed unit.
    Historical BYN labels require the explicit independently reviewed anomaly.
    """
    if not sg.source_unit_review_valid(unit):
        return False
    p = statement['properties']
    contexts = [n for n in graph['nodes'] if n['id'] == p.get('currency_context_id')]
    links = [e['to'] for e in graph['edges']
             if e['type'] == 'IN_CURRENCY' and e['from'] == statement['id']]
    if (len(contexts) != 1 or contexts[0]['type'] != 'CurrencyContext' or
            links != [contexts[0]['id']]):
        return False
    currency = contexts[0]
    q = currency['properties']
    heading = p.get('heading_raw')
    match = re.fullmatch(
        r'\s*ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД С\s+(\d{2}/\d{2}/\d{4})'
        r'\s+ПО\s+(\d{2}/\d{2}/\d{4})(?:\s+ПО\s+([A-Z]{3}))?\s*',
        heading) if isinstance(heading, str) else None
    try:
        start, end = (date.fromisoformat(p[k]) for k in ('period_start', 'period_end'))
        if (match is None or start > end or
                [datetime.strptime(match[i], '%d/%m/%Y').date() for i in (1, 2)] != [start, end]):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    code = q.get('printed_currency')
    line = p.get('header_fields', {}).get('own_account_line_raw')
    account_code = re.search(r'\s([A-Z]{3})\s*$', line) if isinstance(line, str) else None
    account_observations = [o for o in statement['observations']
                            if o['field'] == 'header_own_account_line_raw']
    if (code not in ('BYR', 'BYN') or account_code is None or account_code[1] != code or
            (match[3] is not None and match[3] != code) or
            len(account_observations) != 1 or
            account_observations[0] != {'field': 'header_own_account_line_raw',
                                       'raw': line, 'normalized': line, 'status': 'observed'}):
        return False
    printed = [o for o in currency['observations'] if o['field'] == 'printed_currency']
    if printed and printed != [{'field': 'printed_currency', 'raw': code,
                                'normalized': code, 'status': 'observed'}]:
        return False
    boundary = date(2016, 7, 1)
    if end < boundary:
        expected = ('BYR_2000_historical_ruble', 'pre_2016_07_01')
        anomaly = code == 'BYN'
    elif start >= boundary and code == 'BYN':
        expected = ('BYN_2009_ruble', 'from_2016_07_01')
        anomaly = False
    else:
        return False  # Unreviewed cross-epoch period or unsupported post-epoch label.
    return ((unit['source_unit'], unit['source_epoch']) == expected and
            unit['printed_label_anomaly'] is anomaly and
            q.get('epoch') in (None, expected[1]) and
            q.get('unit_raw') in (None, code) and
            q.get('conversion_factor') in (None, '1'))


def monetary_check(graph, controls):
    """Exact within-original sums. Held rows count; absent empty totals are NA."""
    errors, results = [], []
    statements = [n for n in graph['nodes'] if n['type'] == 'Statement']
    supplied = controls.get('statements', [])
    if len(supplied) != len(statements) or len({c.get('statement_id') for c in supplied}) != len(supplied):
        return ['missing/duplicate independent controls'], []
    by_id = {c['statement_id']: c for c in supplied}
    with localcontext() as context:
        context.prec = max(100, sum(len(str(n['properties'])) for n in graph['nodes']) * 2)
        for st in statements:
            p, c = st['properties'], by_id.get(st['id'])
            if c is None:
                errors.append('missing controls: ' + st['id']); continue
            unit = c.get('source_unit_review', {})
            source_review = c.get('source_review', {})
            reference = source_review.get('reference')
            if (source_review.get('review_status') != 'reviewed' or
                    source_review.get('origin') != 'independent_original_pdf' or
                    not isinstance(reference, str) or not reference.strip() or
                    unit.get('status') != 'verified' or unit.get('transfer_factor') != '1'):
                errors.append('independent controls/unit review missing: ' + st['id'])
            if not source_unit_matches_statement(graph, st, unit):
                errors.append('source unit/period/currency context mismatch: ' + st['id'])
            count = c.get('printed_operation_count', {})
            if count.get('status') not in ('observed', 'absent'):
                errors.append('printed source count status unknown: ' + st['id'])
            elif count['status'] == 'absent':
                if count.get('value') is not None or count.get('raw') is not None:
                    errors.append('contradictory absent printed count: ' + st['id'])
            else:
                value, raw = count.get('value'), count.get('raw')
                printed = re.fullmatch(r'\s*(?:[^0-9\n]*:\s*)?([0-9]+)\s*', raw) if isinstance(raw, str) else None
                if (type(value) is not int or value < 0 or not isinstance(raw, str) or
                        printed is None or int(printed[1]) != value):
                    errors.append('unproven observed printed count: ' + st['id'])
            ops = [n for n in graph['nodes'] if n['type'] == 'Operation' and n['properties']['statement_id'] == st['id']]
            if count.get('status') == 'observed' and count.get('value') != len(ops):
                errors.append('printed count differs: ' + st['id'])
            differences = {}
            for field in ('opening', 'closing', 'printed_debit_total', 'printed_credit_total'):
                left, right = p.get(field), c.get(field)
                if left is None or right is None:
                    if left != right:
                        errors.append('source control absence mismatch: ' + field)
                else:
                    differences[field + '_copy'] = str(sg.decimal(left) - sg.decimal(right))
            if c.get('no_activity'):
                if ops or not p.get('archive_only') or any(c.get(f) is not None or p.get(f) is not None for f in ('printed_debit_total', 'printed_credit_total')):
                    errors.append('empty source has invented operations/turnovers: ' + st['id'])
                if p.get('opening') is None or p.get('closing') is None or sg.decimal(p['opening']) != sg.decimal(p['closing']):
                    errors.append('empty source balances differ: ' + st['id'])
                status, equation = 'not_applicable', 'not_applicable_unprinted_turnovers'
            else:
                if not ops or p.get('archive_only') or p.get('debit_is') != 'outflow' or p.get('opening_sign_rule') != 'signed':
                    errors.append('unproven source movement/sign semantics: ' + st['id'])
                debit = credit = expense = income = Decimal(0)
                for op in ops:
                    q = op['properties']
                    for field in ('printed_debit', 'printed_credit'):
                        if not sg.bank_cell_known(op, field):
                            errors.append('unknown source bank cell: ' + op['id'])
                    debit += sg.decimal(q['printed_debit']) if q['printed_debit'] is not None else Decimal(0)
                    credit += sg.decimal(q['printed_credit']) if q['printed_credit'] is not None else Decimal(0)
                    if q['direction'] == 'outflow': expense += sg.decimal(q['amount'])
                    elif q['direction'] == 'inflow': income += sg.decimal(q['amount'])
                    else: errors.append('unknown source movement: ' + op['id'])
                if any(c.get(f) is None for f in ('opening', 'closing', 'printed_debit_total', 'printed_credit_total')):
                    errors.append('nonempty source controls missing: ' + st['id'])
                else:
                    differences.update(debit_rows=str(debit - sg.decimal(c['printed_debit_total'])),
                                       credit_rows=str(credit - sg.decimal(c['printed_credit_total'])),
                                       expense_rows=str(expense - sg.decimal(c['printed_debit_total'])),
                                       income_rows=str(income - sg.decimal(c['printed_credit_total'])),
                                       balance_equation=str(sg.decimal(c['opening']) - expense + income - sg.decimal(c['closing'])))
                status, equation = 'pass', 'checked_all_rows'
            if any(Decimal(d) != 0 for d in differences.values()):
                errors.append('nonzero monetary difference: ' + st['id']); status = 'failed'
            results.append({'statement_id': st['id'], 'operations': len(ops), 'math_status': status,
                            'equation': equation, 'differences': differences,
                            'printed_count_status': count.get('status'), 'held_rows_included': True})
    return errors, results


def check(graph, controls, review, *, semantic_validation=True):
    structural = sg.validate(graph) if semantic_validation else {'valid': True, 'errors': []}
    errors = list(structural['errors'])
    if not structural['valid']:
        return {'profile': PROFILE, 'scope': 'source_graph_only', 'source_ready': False,
                'accepted': False, 'errors': errors, 'statements': [],
                'future_processing_ready': False}
    errors += review_matches(graph, review)
    if control_fingerprint(controls) != review.get('controls_fingerprint'):
        errors.append('independent source controls reconciliation mismatch')
    from jsonschema import Draft202012Validator
    schema = json.loads((sg.PACKAGE / 'references/controls.schema.json').read_text())
    if not Draft202012Validator(schema).is_valid(controls):
        money_errors, results = ['invalid independent controls contract'], []
    else:
        try:
            money_errors, results = monetary_check(graph, controls)
        except (ValueError, TypeError, KeyError) as exc:
            money_errors, results = ['unreadable required source monetary field: ' + str(exc)], []
    errors += money_errors
    return {'profile': PROFILE, 'scope': 'source_graph_only', 'source_ready': not errors,
            'accepted': not errors, 'errors': errors, 'statements': results,
            'operation_count': sum(n['type'] == 'Operation' for n in graph['nodes']),
            'processing_admission': structural.get('processing_admission', {}).get('status', 'not_evaluated'),
            'future_processing_ready': False,
            'source_review_limit': 'Fingerprints authenticate reviewed saved facts only. Independent original review is required to establish them.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graph', type=Path)
    parser.add_argument('--controls', required=True, type=Path)
    parser.add_argument('--source-review', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = check(*(json.loads(p.read_text()) for p in (args.graph, args.controls, args.source_review)))
    if args.output: sg.write_new(args.output, result)
    else: print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['source_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
