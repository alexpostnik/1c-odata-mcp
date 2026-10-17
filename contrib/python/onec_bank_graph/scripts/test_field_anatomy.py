"""Invented rows exercise source-field decisions, never real bank fixtures."""
import unittest
from unittest import mock
from types import SimpleNamespace
import sys
import archive_adapter as adapter
import statement_graph as sg


def diagnostic_build(document):
    return sg.build(document, diagnostic=True)

from test_archive_adapter import document, line, row


class FieldAnatomyTests(unittest.TestCase):
    def parse(self, body, edit=None):
        doc = document(body)
        if edit:
            edit(doc)
        return adapter.parse_statement(doc, 1, 'synthetic')

    def test_blank_or_damaged_executor_keeps_independent_operation(self):
        for executor in ('    ', ' ?  '):
            damaged = row('2', credit='3.00')
            damaged = damaged[:11] + executor + damaged[15:]
            st, _, _, qa = self.parse([row('1', debit='2.00'), 'First', damaged, 'Second'])
            ops = [r['properties'] for r in st['operations']]
            self.assertEqual([o['amount'] for o in ops], ['2.00', '3.00'])
            self.assertEqual([o['purpose_raw'] for o in ops], ['First', 'Second'])
            self.assertEqual([o['row_status'] for o in ops], ['complete', 'partial'])
            self.assertFalse(st['properties']['rows_complete'])
            self.assertEqual([g['ordinal'] for g in qa['partial_rows']], [2])

    def test_positive_cell_opposite_printed_zero(self):
        for zero in ('0', '0.00'):
            st, _, _, _ = self.parse([row('1', debit='2.00', credit=zero), 'Debit',
                                      row('2', debit=zero, credit='3.00'), 'Credit'])
            a, b = [r['properties'] for r in st['operations']]
            self.assertEqual((a['amount'], a['direction'], a['printed_credit']), ('2.00', 'outflow', zero))
            self.assertEqual((b['amount'], b['direction'], b['printed_debit']), ('3.00', 'inflow', zero))
            self.assertEqual([a['row_status'], b['row_status']], ['complete', 'complete'])

    def test_explicit_component_unit_and_currency_are_raw(self):
        st, entities, _, _ = self.parse([row('1', credit='985.00'),
                                        'Эквайринг на сумму 1000 Р УБ.; комиссия 15 BYR'])
        components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
        self.assertEqual([c['unit_raw'] for c in components], ['Р УБ.', 'BYR'])
        self.assertEqual([c['currency_raw'] for c in components], [None, 'BYR'])

    def test_unknown_header_holds_unlabelled_footer_totals(self):
        def damage(doc):
            doc['pages'][0]['lines'][9] = line(' ' + doc['pages'][0]['lines'][9]['text'])
        st, _, _, qa = self.parse([row('1', debit='2.00'), 'Full purpose'], damage)
        self.assertIsNone(st['properties']['printed_debit_total'])
        self.assertIsNone(st['properties']['printed_credit_total'])
        self.assertFalse(qa['column_contract']['recognized'])

    def test_withheld_inclusion_requires_exact_equation_and_nonconflicting_net(self):
        for purpose in ('gross 1001; удержана комиссия 15',
                        'Эквайринг gross 1000; комиссия 15; net 900',
                        'Эквайринг gross 1000; комиссия 15; net 985; net 985'):
            comps = adapter.amount_components(purpose, 'inflow', '985')
            self.assertTrue(comps)
            self.assertEqual({c[4] for c in comps}, {'unknown'}, purpose)
        for purpose in ('gross 1000; удержана комиссия 15; net 985',
                        'Эквайринг gross 1000; комиссия 15; net 985'):
            self.assertEqual({c[4] for c in adapter.amount_components(purpose, 'inflow', '985')}, {'included'})

    def test_overlap_only_holds_affected_account_field(self):
        def damage(doc):
            doc['pages'][0]['lines'][10]['overlaps'] = [29]
        st, entities, _, qa = self.parse([row('1', debit='2.00'), 'Purpose'], damage)
        account = next(n for n in entities if n['alias'].endswith(':correspondent-account'))
        self.assertIsNone(account['properties']['number_raw'])
        self.assertEqual(account['properties']['identity_status'], 'unknown')
        self.assertEqual(st['operations'][0]['properties']['amount'], '2.00')
        self.assertEqual(st['operations'][0]['properties']['row_status'], 'partial')

    def test_standalone_cash_fee_basis_is_not_revenue_or_withheld_component(self):
        purpose = 'Комис.вознагр.за прием налич. денег с суммы оборотов 760000.00\nпо ставке 0.30 % согласно договору SYN-8.'
        st, entities, relations, qa = self.parse([row('1', debit='2280.00'), *purpose.split('\n')])
        op = st['operations'][0]['properties']
        self.assertEqual((op['direction'], op['amount'], op['classification']), ('outflow', '2280.00', 'bank_fee'))
        self.assertEqual(op['payment1c_comment_raw'], purpose)
        comps = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
        self.assertEqual([(c['kind'], c['value'], c['inclusion']) for c in comps], [('basis', '760000.00', 'unknown')])
        graph = diagnostic_build({'dataset_id':'synthetic','statements':[st], 'entities':entities, 'relations':relations})
        self.assertTrue(sg.validate(graph)['valid'])
        self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_private_document_view_has_fields_and_exact_comment(self):
        purpose = 'Original  two spaces\nПо разделу налога 00123\nсимвол 01: 2.00'
        st, _, _, qa = self.parse([row('1', debit='2.00'), 'Очередность платежа: 22', *purpose.split('\n')])
        op = st['operations'][0]['properties']
        self.assertEqual(op['purpose_raw'], purpose)
        self.assertEqual(op['payment1c_comment_raw'], purpose)
        self.assertEqual(qa['rows'][0]['document_view']['payment1c_comment_raw'], purpose)
        self.assertEqual(qa['rows'][0]['fields']['printed_debit']['normalized'], '2.00')
        self.assertEqual(qa['rows'][0]['fields']['printed_debit']['column_cells'], [39, 62])
        self.assertEqual(qa['rows'][0]['document_view']['amount_origin'], 'same_operation_bank_cells')

    def test_glyph_membership_preserves_duplicate_digit_and_date_after_grid_drift(self):
        original = row('1', credit='420.00')
        doc = document([original, 'Full purpose'])
        source = doc['pages'][0]['lines'][10]
        source['glyphs'] = [{'cell': i, 'text': c, 'x0': i, 'x1': i + 1} for i, c in enumerate(original)]
        source['text'] = source['text'].replace('420.00', '420.0 0')
        st, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
        op = st['operations'][0]['properties']
        self.assertEqual((op['amount'], op['transaction_date'], op['unlabelled_numeric_raw']),
                         ('420.00', '2010-01-01', '1234'))
        self.assertEqual(qa['rows'][0]['fields']['printed_credit']['raw'].strip(), '420.00')

    def test_name_continuation_ends_at_next_metadata_label(self):
        body = [row('1', credit='3.00'), 'Наименование корреспондента: SYNTHETIC NAME',
                '                             SERVICE UNIT', 'Наименование банка: SYNTHETIC BANK',
                'Очередность платежа: 22', 'Full purpose\nUnknown: 700']
        st, entities, _, qa = self.parse(body)
        op = st['operations'][0]['properties']
        self.assertEqual(op['purpose_raw'], 'Full purpose\nUnknown: 700')
        party = next(n for n in entities if n['alias'].endswith(':payer'))
        self.assertEqual(party['properties']['name_raw'], 'SYNTHETIC NAME\n                             SERVICE UNIT')
        self.assertEqual(qa['rows'][0]['document_view']['payment1c_comment_raw'], op['purpose_raw'])

    def test_comment_substitution_fails_validation_but_legacy_input_remains_valid(self):
        st, entities, rels, _ = self.parse([row('1', credit='3.00'), 'Original full purpose'])
        graph = diagnostic_build({'dataset_id':'synthetic','statements':[st], 'entities':entities, 'relations':rels})
        op = next(n for n in graph['nodes'] if n['type']=='Operation')['properties']
        op['payment1c_comment_raw'] = 'Short generated description'
        self.assertFalse(sg.validate(graph)['valid'])
        del op['payment1c_comment_raw']
        del op['purpose_status']
        del op['settlement']
        del op['payment1c_text']  # Legacy source has no safe text representation.
        del op['processing']  # Legacy source has no current row-decision contract.
        self.assertTrue(sg.validate(graph)['valid'])

    def test_unassigned_business_text_holds_coverage(self):
        st, _, _, qa = self.parse(['Unresolved text before first operation', row('1', credit='3.00'), 'Purpose'])
        self.assertFalse(st['properties']['rows_complete'])
        self.assertEqual(len(qa['residual_business_lines']), 1)

    def test_geometry_uses_printed_advance_instead_of_nominal_font_width(self):
        original = document([row('1', credit='420.00'), 'Full purpose'])
        chars = []
        for i, item in enumerate([l for p in original['pages'] for l in p['lines']]):
            for n, char in enumerate(item['raw_text']):
                chars.append({'text':char, 'size':7, 'width':4.176, 'x0':49.68+4.2*n,
                              'x1':49.68+4.2*n+4.176, 'top':20+i*10, 'fontname':'synthetic'})
        reader = mock.MagicMock()
        reader.__enter__.return_value.pages = [SimpleNamespace(chars=chars, page_number=1)]
        with mock.patch.dict(sys.modules, {'pdfplumber':SimpleNamespace(open=lambda _:reader)}):
            geometry = adapter.geometry('synthetic-only')
        st, _, _, qa = adapter.parse_statement(geometry, 1, 'synthetic')
        self.assertEqual(geometry['pages'][0]['step_source'], 'observed_adjacent_glyph_advances')
        self.assertAlmostEqual(geometry['pages'][0]['step'], 4.2)
        self.assertEqual(st['operations'][0]['properties']['amount'], '420.00')
        self.assertEqual(st['operations'][0]['properties']['transaction_date'], '2010-01-01')
        self.assertEqual(qa['partial_rows'], [])

    def test_explicit_withheld_fee_does_not_require_printed_gross(self):
        st, _, _, qa = self.parse([row('1', credit='985.00'), 'Удержана комиссия 15'])
        self.assertEqual(st['operations'][0]['properties']['settlement'],
                         {'status':'verified_withheld_fee','gross':None,'commission':'15','net':'985.00'})
        self.assertEqual(qa['rows'][0]['unverified_components'], [])
        self.assertEqual(qa['rows'][0]['component_gaps'], [])

    def test_manual_reviewed_movement_cannot_replace_bank_cell_from_description(self):
        for field, value in (('amount','999999'), ('direction','outflow'), ('printed_credit',None)):
            st, entities, rels, _ = self.parse([row('1', credit='985.00'), 'Description number 999999'])
            st['operations'][0]['properties'][field] = value
            graph = diagnostic_build({'dataset_id':'synthetic','statements':[st], 'entities':entities, 'relations':rels})
            self.assertFalse(sg.validate(graph)['valid'], field)

    def test_optional_reference_total_does_not_control_receipt_or_fee(self):
        for reference in ('', '; общая сумма 99999', '; Сумма операций: 12345'):
            purpose = 'Перечисление по платежным карточкам; комиссия 15' + reference
            st, entities, rels, qa = self.parse([row('1', credit='985.00'), purpose])
            self.assertEqual(st['operations'][0]['properties']['settlement'],
                             {'status':'verified_withheld_fee','gross':None,'commission':'15','net':'985.00'})
            self.assertEqual(qa['rows'][0]['component_gaps'], [])
            graph = diagnostic_build({'dataset_id':'synthetic','statements':[st], 'entities':entities, 'relations':rels})
            self.assertTrue(sg.validate(graph)['valid'])
            self.assertEqual(sum(n['type']=='Operation' for n in graph['nodes']), 1)
            next(n for n in graph['nodes'] if n['type']=='Operation')['properties']['settlement']['commission']='16'
            self.assertFalse(sg.validate(graph)['valid'])

    def test_debit_fee_with_description_total_is_always_outgoing(self):
        purpose = 'Удержана комиссия 15; общая сумма 1000; выручка'
        st, _, _, qa = self.parse([row('1', debit='15.00'), purpose])
        op = st['operations'][0]['properties']
        self.assertEqual((op['direction'], op['amount']), ('outflow','15.00'))
        self.assertEqual(op['settlement'], {'status':'not_applicable','gross':None,'commission':None,'net':None})
        self.assertIn('not_an_incoming_net_settlement', qa['rows'][0]['component_gaps'])

    def test_incoming_transfer_fee_with_optional_reference_total(self):
        for purpose in ('Перевод денежных средств в пользу SYNTHETIC; комиссия 5',
                        'Перевод денежных средств по отчету; на общую сумму 3000, комиссия 5',
                        'Перечисление денежных средств E-POS; Сумма операций: 3000; комиссия: 5'):
            st, _, _, qa = self.parse([row('1', credit='995.00'), purpose])
            self.assertEqual(st['operations'][0]['properties']['settlement'],
                             {'status':'verified_withheld_fee','gross':None,'commission':'5','net':'995.00'})
            self.assertEqual(qa['rows'][0]['component_gaps'], [])

    def test_commission_refund_relationship_stays_unknown(self):
        st, _, _, qa = self.parse([row('1', credit='15.00'),
                                   'Перевод денежных средств: возврат удержанной комиссии 15'])
        op = st['operations'][0]['properties']
        self.assertEqual((op['amount'],op['direction']), ('15.00','inflow'))
        self.assertEqual(op['settlement']['status'], 'unknown')
        self.assertTrue(qa['rows'][0]['unverified_components'])

    def test_unresolved_purpose_holds_fee_but_keeps_bank_credit(self):
        def overlap(doc):
            doc['pages'][0]['lines'][11]['overlaps']=[20]
        st, entities, rels, qa = self.parse([row('1', credit='985.00'), 'Удержана комиссия 15'], overlap)
        op=st['operations'][0]['properties']
        self.assertEqual((op['amount'],op['direction']), ('985.00','inflow'))
        self.assertEqual(op['purpose_status'], 'unknown')
        self.assertEqual(op['settlement']['status'], 'unknown')
        graph=diagnostic_build({'dataset_id':'synthetic','statements':[st],'entities':entities,'relations':rels})
        self.assertTrue(sg.validate(graph)['valid'])

    def test_verified_settlement_is_checked_again_at_graph_output(self):
        st, entities, rels, _ = self.parse([row('1', credit='985.00'), 'gross 1000; удержана комиссия 15'])
        self.assertEqual(st['operations'][0]['properties']['settlement'],
                         {'status':'verified_equation','gross':'1000','commission':'15','net':'985.00'})
        graph = diagnostic_build({'dataset_id':'synthetic','statements':[st], 'entities':entities, 'relations':rels})
        self.assertTrue(sg.validate(graph)['valid'])
        next(n for n in graph['nodes'] if n['type']=='Operation')['properties']['settlement']['commission']='16'
        self.assertFalse(sg.validate(graph)['valid'])

    def test_business_keywords_are_not_footer_or_header_boundaries(self):
        for purpose in ('ОБОРОТЫ ПО ЭКВАЙРИНГУ ЗА ПЕРИОД; общая сумма 1000; комиссия 15',
                        'Назначение: ВЫПИСКА ИЗ ДОГОВОРА N-SYNTHETIC'):
            st, _, _, qa = self.parse([row('1', credit='985.00'), 'Очередность: 22', purpose])
            op = st['operations'][0]['properties']
            self.assertEqual(op['purpose_raw'], purpose)
            self.assertEqual(op['payment1c_comment_raw'], purpose)
            self.assertEqual(op['row_status'], 'complete')
            self.assertTrue(st['properties']['rows_complete'])
            self.assertEqual(qa['rows'][0]['document_view']['purpose_raw'], purpose)

    def test_unreadable_explicit_net_does_not_disappear_or_yield_numeric_prefix(self):
        for ending in ('net ???', 'net 985; net ???', 'net 985.??', 'net 985.00??', 'net'):
            purpose = 'Эквайринг gross 1000; комиссия 15; ' + ending
            st, entities, _, qa = self.parse([row('1', credit='985.00'), purpose])
            op = st['operations'][0]['properties']
            self.assertEqual(op['settlement'], {'status':'unknown','gross':None,'commission':None,'net':None})
            self.assertEqual(op['purpose_raw'], purpose)
            nets = [n for n in entities if n['type']=='AmountComponent' and n['properties']['kind']=='net']
            self.assertTrue(any(n['properties']['value'] is None for n in nets))
            self.assertTrue(qa['rows'][0]['component_gaps'])

    def test_unreadable_explicit_component_keeps_entire_source_token(self):
        for label in ('gross', 'комиссия', 'net'):
            purpose = 'Эквайринг gross 1000; комиссия 15; net 985'
            token = {'gross':'1000','комиссия':'15','net':'985'}[label]
            purpose = purpose.replace(label + ' ' + token, label + ' ' + token + '.??')
            st, entities, _, qa = self.parse([row('1', credit='985.00'), purpose])
            component = next(n for n in entities if n['type']=='AmountComponent' and n['properties']['value_raw'].strip()==token+'.??')
            self.assertIsNone(component['properties']['value'])
            self.assertEqual(st['operations'][0]['properties']['settlement']['status'], 'unknown')

    def test_every_explicit_money_label_retains_empty_or_unreadable_assertions(self):
        labels = [('gross','gross','1000'), ('сумма брутто','gross','1000'),
                  ('комиссия','fee','15'), ('удержана комиссия','fee','15'),
                  ('net','net','985'), ('сумма нетто','net','985')]
        for label, kind, value in labels:
            for unreadable in ('', '[неразборчиво]', 'неразборчиво', 'N/A', '???'):
                for duplicate in (False, True):
                    with self.subTest(label=label, unreadable=unreadable, duplicate=duplicate):
                        others = ['Эквайринг']
                        if kind != 'fee': others.append('комиссия 15')
                        if duplicate: others.append(label + ' ' + value)
                        others.append(label + (' ' + unreadable if unreadable else ''))
                        purpose='; '.join(others)
                        st, en, _, qa=self.parse([row('1',credit='985.00'),purpose])
                        props=st['operations'][0]['properties']
                        self.assertEqual(props['settlement']['status'],'unknown')
                        components=[n['properties'] for n in en if n['type']=='AmountComponent' and n['properties']['kind']==kind]
                        self.assertEqual(len(components),2 if duplicate else 1)
                        self.assertEqual(components[-1]['value_raw'].strip(),unreadable)
                        self.assertIsNone(components[-1]['value'])
                        self.assertTrue(qa['rows'][0]['component_gaps'])
                        self.assertEqual((props['amount'],props['direction']),('985.00','inflow'))
                        self.assertEqual(props['purpose_raw'],purpose)

    def test_every_money_label_rejects_whole_multi_decimal_or_attached_bad_token(self):
        for label,kind,value in [('gross','gross','1000'),('комиссия','fee','15'),('net','net','985')]:
            for suffix in ('.00.??','.00.55',',00,??','.00..??','.00[неразборчиво]'):
                with self.subTest(label=label,suffix=suffix):
                    purpose='Эквайринг; '+ ('комиссия 15; ' if kind!='fee' else '')+label+' '+value+suffix
                    st,en,_,qa=self.parse([row('1',credit='985.00'),purpose])
                    component=next(n['properties'] for n in en if n['type']=='AmountComponent' and n['properties']['kind']==kind)
                    self.assertEqual(component['value_raw'].strip(),value+suffix)
                    self.assertIsNone(component['value'])
                    self.assertEqual(st['operations'][0]['properties']['settlement']['status'],'unknown')
                    self.assertTrue(qa['rows'][0]['component_gaps'])

    def test_explicit_separate_service_fee_is_not_supported_withholding(self):
        for purpose in ('Перевод денежных средств; комиссия 15 за отдельную услугу',
                        'Удержана комиссия 15 за отдельные услуги; gross 1000',
                        'Перевод денежных средств; комиссия 15 за услуги',
                        'Перевод денежных средств; комиссия 15 за банковскую услугу',
                        'Эквайринг; отдельная банковская комиссия 15',
                        'Эквайринг; отдельная комиссия 15'):
            st,en,rels,qa=self.parse([row('1',credit='985.00'),purpose])
            op=st['operations'][0]['properties']
            self.assertEqual(op['settlement']['status'],'unknown')
            self.assertEqual(op['payment1c_comment_raw'],purpose)
            self.assertTrue(qa['rows'][0]['component_gaps'])
            graph=diagnostic_build({'dataset_id':'synthetic','statements':[st],'entities':en,'relations':rels})
            self.assertTrue(sg.validate(graph)['valid'])

    def test_document_kind_is_gated_by_bank_direction_before_purpose(self):
        for debit,credit,kind in [('15.00','','Списание с расчетного счета'),
                                  ('','985.00','Поступление на расчетный счет'),
                                  ('15.00','985.00',None),('??','985.00',None)]:
            st,_,_,qa=self.parse([row('1',debit=debit,credit=credit),
                                  'Выручка; перевод денежных средств; удержана комиссия 15'])
            self.assertIsNone(qa['rows'][0]['document_view']['bank_document_kind_candidate'])
            self.assertFalse(qa['rows'][0]['document_view']['statement_processing_allowed'])

    def test_classification_fee_prose_does_not_invent_unreadable_money_field(self):
        for purpose in ('Комиссия банка за ведение счета', 'Комиссия за услуги'):
            st,en,_,qa=self.parse([row('1',debit='15.00'),purpose])
            self.assertEqual([n for n in en if n['type']=='AmountComponent'],[])
            self.assertEqual(st['operations'][0]['properties']['settlement']['status'],'not_applicable')
            self.assertEqual(qa['rows'][0]['component_gaps'],[])
            self.assertEqual(st['operations'][0]['properties']['purpose_raw'],purpose)

    def test_attached_label_numeric_token_is_still_an_explicit_assertion(self):
        for label,value in [('gross','1000'),('сумма брутто','1000'),('комиссия','15'),('net','985')]:
            purpose='Эквайринг; '+('комиссия 15; ' if label!='комиссия' else '')+label+value+'.00.??'
            st,en,_,qa=self.parse([row('1',credit='985.00'),purpose])
            self.assertEqual(st['operations'][0]['properties']['settlement']['status'],'unknown')
            self.assertTrue(qa['rows'][0]['component_gaps'])


if __name__ == '__main__':
    unittest.main()
