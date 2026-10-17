"""Source-only acceptance: actual unknowns stay truthful; tampering fails."""
import copy
import unittest
import source_readiness as sr
from test_modern_reviewed import doc, parse, op
import test_modern_reviewed as modern_tests
import archive_adapter as adapter
import bank_identifiers as identifiers
import statement_graph as sg
import modern_adapter as modern
from jsonschema import Draft202012Validator
import json


def fixture(unp='123456789'):
    _, graph, _ = parse(doc(unp=unp))
    st = next(n for n in graph['nodes'] if n['type'] == 'Statement')
    p = st['properties']
    controls = {'statements': [{
        'statement_id': st['id'], 'opening': p['opening'], 'closing': p['closing'],
        'printed_debit_total': p['printed_debit_total'], 'printed_credit_total': p['printed_credit_total'],
        'no_activity': False,
        'opening_raw': p['opening'], 'first_for_account': None,
        'first_account_basis': 'Synthetic explicit opening, no first-account claim',
        'source_review': {'origin': 'independent_original_pdf', 'review_status': 'reviewed', 'reference': 'synthetic-review'},
        'source_unit_review': {'status': 'verified', 'source_unit': 'BYN_2009_ruble', 'source_epoch': 'from_2016_07_01', 'transfer_factor': '1', 'printed_label_anomaly': False, 'basis': 'synthetic fixture printed BYN and January 2026 period'},
        'printed_operation_count': {'status': 'absent', 'value': None, 'raw': None}}]}
    review = {'profile': sr.PROFILE, 'source_review': 'independent_pdf_review',
              'full_field_coverage': True, 'unresolved_source_errors': [], 'operations': 1,
              'node_fingerprints': {n['id']: sr.fact_fingerprint(n) for n in graph['nodes']},
              'edge_fingerprint': sr.edge_fingerprint(graph['edges']),
              'controls_fingerprint': sr.control_fingerprint(controls)}
    return graph, controls, review


class SourceReadinessTests(unittest.TestCase):
    def test_ready_source_with_processing_unknown(self):
        graph, controls, review = fixture(unp='')
        self.assertEqual(op(graph)['properties']['processing']['status'], 'needs_clarification')
        result = sr.check(graph, controls, review)
        self.assertTrue(result['source_ready'], result['errors'])
        self.assertFalse(result['future_processing_ready'])
        self.assertEqual(op(graph)['properties']['modern_fields']['unp_raw'], '')
        self.assertIsNone(op(graph)['properties']['counterparty']['unp_raw'])

    def test_cent_text_direction_and_row_loss_rejected(self):
        graph, controls, review = fixture()
        changes = [('amount', '2.01'), ('direction', 'inflow'), ('purpose_raw', 'Incomplete'),
                   ('document_date', '2026-01-04')]
        for field, value in changes:
            changed = copy.deepcopy(graph)
            op(changed)['properties'][field] = value
            self.assertFalse(sr.check(changed, controls, review)['source_ready'], field)
        changed = copy.deepcopy(graph)
        changed['nodes'] = [n for n in changed['nodes'] if n['type'] != 'Operation']
        self.assertFalse(sr.check(changed, controls, review)['source_ready'])

    def test_coordinated_full_text_rewrite_fails_source_review(self):
        graph, controls, review = fixture()
        changed = copy.deepcopy(graph)
        p = op(changed)['properties']
        p['purpose_raw'] = p['payment1c_comment_raw'] = 'Altered synthetic full text'
        # Even substituting all derived text cannot change the source receipt.
        self.assertIn('source field/node reconciliation mismatch', sr.review_matches(changed, review))

    def test_party_bank_role_reference_tamper(self):
        graph, controls, review = fixture()
        for kind, key in [('Bank', 'code_raw'), ('Account', 'number_raw'), ('Party', 'unp_raw')]:
            changed = copy.deepcopy(graph)
            n = next(n for n in changed['nodes'] if n['type'] == kind)
            n['properties'][key] = 'SYNTHETIC-WRONG'
            self.assertIn('source field/node reconciliation mismatch', sr.review_matches(changed, review))
        changed = copy.deepcopy(graph)
        edge = next(e for e in changed['edges'] if e['type'] == 'PAYEE')
        edge['type'] = 'PAYER'
        self.assertIn('source role/reference reconciliation mismatch', sr.review_matches(changed, review))

    def test_changed_independent_controls_fail(self):
        graph, controls, review = fixture()
        controls['statements'][0]['closing'] = '88.98'
        self.assertFalse(sr.check(graph, controls, review)['source_ready'])

    def test_independent_controls_reference_required_after_reconciliation(self):
        graph, controls, review = fixture()
        for case, value in [('absent', None), ('null', None), ('empty', ''),
                            ('whitespace', ' \t\n '), ('number', 1), ('boolean', True),
                            ('list', ['synthetic-review']), ('object', {'ref': 'synthetic-review'})]:
            changed = copy.deepcopy(controls)
            source_review = changed['statements'][0]['source_review']
            if case == 'absent':
                del source_review['reference']
            else:
                source_review['reference'] = value
            reconciled = copy.deepcopy(review)
            reconciled['controls_fingerprint'] = sr.control_fingerprint(changed)
            money_errors, _ = sr.monetary_check(graph, changed)
            self.assertTrue(any('independent controls/unit review missing' in error
                                for error in money_errors), case)
            result = sr.check(graph, changed, reconciled)
            self.assertFalse(result['source_ready'], case)
        self.assertTrue(sr.check(graph, controls, review)['source_ready'])

    def test_unknown_printed_bank_raw_is_retained_without_repair(self):
        graph, controls, _ = fixture()
        st = next(n for n in graph['nodes'] if n['type'] == 'Statement')
        bank = {'id': 'dataset:synthetic-modern:own-bank', 'type': 'Bank',
                'properties': {'name_raw': 'Synthetic own bank', 'code_raw': 'SYN0BY0000',
                               'identifier': identifiers.recognize('SYN0BY0000', 'bank'),
                               'code_system_raw': 'БИК', 'valid_from': None, 'valid_to': None,
                               'identity_status': 'unknown', 'branch_raw': None,
                               'city_raw': None, 'country_raw': None},
                'observations': [{'field': 'code_raw', 'raw': 'SYN0BY0000',
                                  'normalized': None, 'status': 'unknown'}],
                'unrecognized_fields': []}
        graph['nodes'].append(bank)
        graph['edges'].append({'id': 'synthetic-ownbank-edge', 'type': 'SERVICED_BY',
                               'from': st['properties']['account_id'], 'to': bank['id']})
        review = {'profile': sr.PROFILE, 'source_review': 'independent_pdf_review',
                  'full_field_coverage': True, 'unresolved_source_errors': [], 'operations': 1,
                  'node_fingerprints': {n['id']: sr.fact_fingerprint(n) for n in graph['nodes']},
                  'edge_fingerprint': sr.edge_fingerprint(graph['edges']),
                  'controls_fingerprint': sr.control_fingerprint(controls)}
        self.assertTrue(sr.check(graph, controls, review)['source_ready'])
        self.assertEqual(bank['properties']['code_raw'], 'SYN0BY0000')
        self.assertIsNone(bank['properties']['identifier']['normalized'])
        bank['properties']['identity_status'] = 'observed'
        self.assertFalse(sr.check(graph, controls, review)['source_ready'])

    def test_empty_missing_turnovers_are_na_invented_zero_fails(self):
        graph, _ = adapter.balance_only_archive(modern_tests.EmptyArchiveTests().empty(), 'synthetic-empty-source')
        st = next(n for n in graph['nodes'] if n['type'] == 'Statement')
        p = st['properties']
        controls = {'statements': [{'statement_id': st['id'], 'opening': p['opening'],
                    'closing': p['closing'], 'printed_debit_total': None, 'printed_credit_total': None,
                    'opening_raw': p['opening'], 'first_for_account': None, 'first_account_basis': None,
                    'no_activity': True,
                    'source_review': {'origin': 'independent_original_pdf', 'review_status': 'reviewed', 'reference': 'synthetic-empty-review'},
                    'printed_operation_count': {'status': 'absent', 'value': None, 'raw': None},
                    'source_unit_review': {'status': 'verified', 'source_unit': 'BYR_2000_historical_ruble',
                                          'source_epoch': 'pre_2016_07_01', 'transfer_factor': '1',
                                          'printed_label_anomaly': False, 'basis': 'synthetic fixture printed BYR and 2010 period'}}]}
        review = {'profile': sr.PROFILE, 'source_review': 'independent_pdf_review',
                  'full_field_coverage': True, 'unresolved_source_errors': [], 'operations': 0,
                  'node_fingerprints': {n['id']: sr.fact_fingerprint(n) for n in graph['nodes']},
                  'edge_fingerprint': sr.edge_fingerprint(graph['edges']),
                  'controls_fingerprint': sr.control_fingerprint(controls)}
        result = sr.check(graph, controls, review)
        self.assertTrue(result['source_ready'], result['errors'])
        self.assertEqual(result['statements'][0]['math_status'], 'not_applicable')
        p['printed_debit_total'] = '0.00'
        self.assertFalse(sr.check(graph, controls, review)['source_ready'])

    def test_bounded_service_projection_retains_all_business_digits(self):
        raw = 'Complete synthetic purpose\n   \x1b4символ 2004 :          654.81(cid:27)5'
        self.assertEqual(sr.semantic_cash_projection(raw), 'Complete synthetic purpose\n   символ 2004 :          654.81')
        raw2 = raw.replace('\x1b4', '(cid:27)4').replace('(cid:27)5', '\x1b5')
        self.assertEqual(sr.semantic_cash_projection(raw2), sr.semantic_cash_projection(raw))
        for bad in ['Party (cid:27)5', 'символ 2004 : 654.81(cid:27)5', '\x1b4символ 2004 : 654.81(cid:99)5']:
            with self.assertRaises(ValueError): sr.semantic_cash_projection(bad)

    def test_multiple_mixed_cash_pairs_and_unproved_controls(self):
        raw='Complete synthetic purpose\n   \x1b4символ 2004 : 750.00(cid:27)5\n   \x1b4символ 2032 : 2950.00\x1b5'
        expected='Complete synthetic purpose\n   символ 2004 : 750.00\n   символ 2032 : 2950.00'
        self.assertEqual(sr.semantic_cash_projection(raw),expected)
        self.assertEqual(sr.semantic_cash_projection(raw.replace('(cid:27)','\x1b')),expected)
        for bad in [raw+'\n\x1bXother business',raw.replace('\x1b5',''),raw+'\n(cid:99)']:
            with self.assertRaises(ValueError):sr.semantic_cash_projection(bad)

    def test_unknown_date_shape_stays_held_and_source_not_ready(self):
        graph,controls,review=fixture()
        transaction=next(o for o in op(graph)['observations'] if o['field']=='transaction_date')
        transaction['status']='unknown'
        self.assertIn('modern_transaction_date_unresolved',modern.source_fields(op(graph))['gaps'])
        self.assertFalse(sg.validate(graph)['valid'])  # stale ready claim
        sg.materialize_processing(graph)
        self.assertEqual(op(graph)['properties']['processing']['status'],'needs_clarification')
        self.assertEqual(transaction['status'],'unknown')
        self.assertTrue(sg.validate(graph)['valid'])
        review['node_fingerprints']={n['id']:sr.fact_fingerprint(n) for n in graph['nodes']}
        self.assertFalse(sr.check(graph,controls,review)['source_ready'])
        self.assertTrue(any('unresolved source transaction date' in x for x in sr.check(graph,controls,review)['errors']))

    def test_printed_count_tuple_contract_and_runtime(self):
        graph,controls,review=fixture()
        validator=Draft202012Validator(json.loads((sg.PACKAGE/'references/controls.schema.json').read_text()))
        cases=[{'status':'absent','value':1,'raw':'1'},
               {'status':'observed','value':1,'raw':None},
               {'status':'observed','value':1,'raw':''},
               {'status':'observed','value':1,'raw':'  '},
               {'status':'observed','value':True,'raw':'1'},
               {'status':'observed','value':1,'raw':'2'}]
        for count in cases:
            controls['statements'][0]['printed_operation_count']=count
            errors,_=sr.monetary_check(graph,controls)
            self.assertTrue(errors,count)
            if count['raw']!='2':self.assertFalse(validator.is_valid(controls),count)
            review['controls_fingerprint']=sr.control_fingerprint(controls)
            self.assertFalse(sr.check(graph,controls,review)['source_ready'],count)
        for count in [{'status':'absent','value':None,'raw':None},{'status':'observed','value':1,'raw':' 1 '},{'status':'observed','value':1,'raw':'Всего документов: 1'}]:
            controls['statements'][0]['printed_operation_count']=count
            review['controls_fingerprint']=sr.control_fingerprint(controls)
            self.assertTrue(validator.is_valid(controls))
            self.assertTrue(sr.check(graph,controls,review)['source_ready'])

    def test_units_bound_after_coordinated_controls_and_source_fingerprints(self):
        graph, controls, review = fixture()
        st = next(n for n in graph['nodes'] if n['type'] == 'Statement')
        currency = next(n for n in graph['nodes'] if n['type'] == 'CurrencyContext')
        unit = controls['statements'][0]['source_unit_review']
        cases = {
            'synthetic_tuple': lambda g, c: c['statements'][0]['source_unit_review'].update(
                source_unit='synthetic-unit', source_epoch='synthetic-epoch'),
            'unsupported_unit': lambda g, c: c['statements'][0]['source_unit_review'].update(source_unit='unproved-unit'),
            'wrong_epoch': lambda g, c: c['statements'][0]['source_unit_review'].update(source_epoch='pre_2016_07_01'),
            'old_unit_for_modern_period': lambda g, c: c['statements'][0]['source_unit_review'].update(
                source_unit='BYR_2000_historical_ruble', source_epoch='pre_2016_07_01'),
            'unproved_anomaly': lambda g, c: c['statements'][0]['source_unit_review'].update(printed_label_anomaly=True),
            'conversion': lambda g, c: c['statements'][0]['source_unit_review'].update(transfer_factor='10000'),
            'empty_basis': lambda g, c: c['statements'][0]['source_unit_review'].update(basis=' '),
            'context_reference': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties'].update(currency_context_id='missing'),
            'context_edge': lambda g, c: next(e for e in g['edges'] if e['type'] == 'IN_CURRENCY').update(to='missing'),
            'context_type': lambda g, c: next(n for n in g['nodes'] if n['id'] == currency['id']).update(type='Account'),
            'period_copy': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties'].update(period_start='2010-01-01'),
            'period_literal': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties'].update(heading_raw=st['properties']['heading_raw'].replace('2026', '2010')),
            'invalid_date': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties'].update(period_end='2026-02-30'),
            'currency_code': lambda g, c: next(n for n in g['nodes'] if n['id'] == currency['id'])['properties'].update(printed_currency='USD'),
            'currency_epoch': lambda g, c: next(n for n in g['nodes'] if n['id'] == currency['id'])['properties'].update(epoch='pre_2016_07_01'),
            'currency_observation': lambda g, c: next(n for n in g['nodes'] if n['id'] == currency['id'])['observations'].append(
                {'field': 'printed_currency', 'raw': 'BYR', 'normalized': 'BYN', 'status': 'observed'}),
            'currency_factor': lambda g, c: next(n for n in g['nodes'] if n['id'] == currency['id'])['properties'].update(conversion_factor='10000'),
            'account_literal': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties']['header_fields'].update(own_account_line_raw='Cчет клиента 000123 USD'),
            'heading_currency': lambda g, c: next(n for n in g['nodes'] if n['id'] == st['id'])['properties'].update(heading_raw=st['properties']['heading_raw']+' ПО BYR'),
            'statement_control_reference': lambda g, c: c['statements'][0].update(statement_id='missing'),
        }
        self.assertTrue(sr.source_unit_matches_statement(graph, st, unit))
        for case, mutate in cases.items():
            with self.subTest(case=case):
                changed, checked = copy.deepcopy(graph), copy.deepcopy(controls)
                mutate(changed, checked)
                reconciled = copy.deepcopy(review)
                reconciled.update(node_fingerprints={n['id']: sr.fact_fingerprint(n) for n in changed['nodes']},
                                  edge_fingerprint=sr.edge_fingerprint(changed['edges']),
                                  controls_fingerprint=sr.control_fingerprint(checked))
                # Isolate the canonical monetary guard even with replaced receipts.
                errors, _ = sr.monetary_check(changed, checked)
                self.assertTrue(errors)
                self.assertFalse(sr.check(changed, checked, reconciled, semantic_validation=False)['source_ready'])

    def test_historical_printed_byn_requires_reviewed_old_unit_not_relabelling(self):
        graph, _ = adapter.balance_only_archive(modern_tests.EmptyArchiveTests().empty(), 'synthetic-old-unit')
        st = next(n for n in graph['nodes'] if n['type'] == 'Statement')
        currency = next(n for n in graph['nodes'] if n['type'] == 'CurrencyContext')
        unit = {'status': 'verified', 'source_unit': 'BYR_2000_historical_ruble',
                'source_epoch': 'pre_2016_07_01', 'transfer_factor': '1',
                'printed_label_anomaly': False, 'basis': 'Synthetic 2010 printed BYR fixture'}
        self.assertTrue(sr.source_unit_matches_statement(graph, st, unit))
        currency['properties']['printed_currency'] = 'BYN'
        for o in currency['observations']:
            if o['field'] == 'printed_currency': o.update(raw='BYN', normalized='BYN')
        st['properties']['heading_raw'] = st['properties']['heading_raw'].replace('BYR', 'BYN')
        st['properties']['header_fields']['own_account_line_raw'] = st['properties']['header_fields']['own_account_line_raw'].replace('BYR', 'BYN')
        for o in st['observations']:
            if o['field'] == 'header_own_account_line_raw': o.update(raw=o['raw'].replace('BYR', 'BYN'), normalized=o['normalized'].replace('BYR', 'BYN'))
        self.assertFalse(sr.source_unit_matches_statement(graph, st, unit))
        unit.update(printed_label_anomaly=True, basis='Synthetic independently reviewed literal historical anomaly')
        before = copy.deepcopy(graph)
        self.assertTrue(sr.source_unit_matches_statement(graph, st, unit))
        for changes in [dict(source_unit='synthetic-unit', source_epoch='synthetic-epoch'),
                        dict(source_unit='BYN_2009_ruble', source_epoch='from_2016_07_01'),
                        dict(source_unit='unsupported', source_epoch='unsupported')]:
            self.assertFalse(sr.source_unit_matches_statement(graph, st, {**unit, **changes}))
        currency['observations'][0]['raw'] = 'BYR'
        self.assertFalse(sr.source_unit_matches_statement(graph, st, unit))
        currency['observations'][0]['raw'] = 'BYN'
        self.assertEqual(graph, before)  # No source relabelling or amount conversion.
        st['properties'].update(period_start='2016-01-01', period_end='2016-12-31',
                                heading_raw='ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД С 01/01/2016 ПО 31/12/2016 ПО BYN')
        self.assertFalse(sr.source_unit_matches_statement(graph, st, unit))


if __name__ == '__main__': unittest.main()
