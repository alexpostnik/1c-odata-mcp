"""Synthetic regression cases for the observed archive layout, without bank data."""
import unittest
import archive_adapter as adapter
import statement_graph as sg


def diagnostic_build(document):
    return sg.build(document, diagnostic=True)



def line(text):
    return {'text': text, 'raw_text': text, 'top': 1, 'overlaps': []}


def row(number, debit='', credit='', suffix='1234'):
    return (f'{number:>6}' + '  6  ' + f'{56:>4}' + '123456789 ' + '9876543210001  ' +
            f'{debit:>22}{credit:>22}' + '01/01/2010  ' + suffix)


def document(body, empty=False, debit_total='2.00', credit_total='3.00', closing='1.00'):
    header = [
        ' Филиал SYNTHETIC BANK БИК 000000000',
        'ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД С 01/01/2010 ПО 31/12/2010 ПО BYR',
        'Ответственный исполнитель 56', 'Дата пред. опер. 1-я выписка',
        'Cчет клиента SYNTHETIC-ACCOUNT BYR', 'Наименование SYNTHETIC HOLDER',
        'Входящее сальдо 0.00',
        'N док |Вид |Код|   Корресподнент       |                  Обороты                   |',
        '      |опер|исп|-----------------------+---------------------------------------------  Дата',
        '      |    |   |   Код   | Счет        |       Дебет          |     Кредит          |']
    footer = [] if empty else ['ОБОРОТЫ'.ljust(40) + f'{debit_total:>22}{credit_total:>22}']
    footer.append('Исходящее сальдо ' + ('0.00' if empty else closing))
    return {'reviewed_continuations': [{'from_page': 1, 'to_page': 2, 'decision': 'same_operation', 'evidence': 'synthetic boundary review'}],
            'pages': [{'page': 1, 'lines': [line(t) for t in header + body[:3]], 'business_fonts': ['synthetic']},
                      {'page': 2, 'lines': [line(t) for t in body[3:] + footer], 'business_fonts': ['synthetic']}]}


class ArchiveAdapterTests(unittest.TestCase):
    def graph(self, body):
        statement, entities, relations, qa = adapter.parse_statement(document(body), 1, 'synthetic')
        graph = diagnostic_build({'dataset_id': 'synthetic', 'entities': entities,
                          'statements': [statement], 'relations': relations})
        self.assertTrue(sg.validate(graph)['valid'])
        return statement, entities, graph, qa

    def test_standalone_fee_base_and_rate_do_not_create_movements(self):
        # Entirely synthetic values/text, not a reconstruction of the owner's row.
        purpose = 'Комис.вознагр.за прием налич. денег с суммы оборотов 760000.00\nпо ставке 0.30 % согласно договору SYN-8.'
        body = [row('1', debit='2280.00'), 'Очередность: 22', *purpose.split('\n'),
                row('2', credit='760000.00'), 'Выручка по синтетическому документу SYN-9']
        statement, entities, graph, _ = self.graph(body)
        ops = statement['operations']
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0]['properties']['amount'], '2280.00')
        self.assertEqual(ops[0]['properties']['direction'], 'outflow')
        self.assertEqual(ops[0]['properties']['purpose_raw'], purpose)
        self.assertEqual(ops[1]['properties']['amount'], '760000.00')
        self.assertEqual(ops[1]['properties']['transaction_date'], ops[0]['properties']['transaction_date'])
        self.assertEqual([(n['properties']['kind'], n['properties']['value']) for n in entities
                          if n['type'] == 'AmountComponent'], [('basis', '760000.00')])
        self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 2)

    def test_net_acquiring_is_one_credit_with_explicit_components(self):
        purpose = 'Эквайринг. Общая сумма: 1000.00; удержанная комиссия: 15.00; net: 985.00.\nСсылка SYN-20; произвольное число 500000.00.'
        statement, entities, graph, _ = self.graph([row('1', credit='985.00'), *purpose.split('\n')])
        op = statement['operations'][0]['properties']
        self.assertEqual((op['amount'], op['direction'], op['purpose_raw']), ('985.00', 'inflow', purpose))
        components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
        self.assertEqual([(n['kind'], n['value']) for n in components], [('gross', '1000.00'), ('fee', '15.00'), ('net', '985.00')])
        self.assertEqual(components[1]['inclusion'], 'included')
        self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_fee_does_not_invent_unprinted_gross_or_classify_revenue(self):
        statement, entities, _, _ = self.graph([row('1', credit='985.00'), 'Комиссия банка - 15.00. За услуги SYN-1.'])
        components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
        self.assertEqual([n['kind'] for n in components], ['fee'])
        self.assertEqual(components[0]['inclusion'], 'unknown')
        self.assertIsNone(statement['operations'][0]['properties']['classification'])

    def test_full_purpose_includes_cash_symbol_tax_and_unknown_lines(self):
        purpose = ['Original  two spaces 999.00.', 'символ 01: 3.00', 'По разделу налога: 123', 'Unknown label: SYN-0']
        statement, entities, _, _ = self.graph([row('1', credit='3.00'), *purpose])
        self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], '\n'.join(purpose))
        self.assertEqual(sum(n['type'] == 'CashSymbol' for n in entities), 1)
        self.assertEqual(sum(n['type'] == 'TaxDetail' for n in entities), 1)

    def test_overlap_holds_only_affected_row(self):
        doc = document([row('1', debit='2.00'), 'Overlapped purpose', row('2', credit='3.00'), 'Intact purpose'])
        doc['pages'][0]['lines'][-3]['overlaps'] = [44]
        statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
        self.assertEqual([r['properties']['row_status'] for r in statement['operations']], ['partial', 'complete'])
        self.assertFalse(statement['properties']['rows_complete'])
        self.assertEqual(len(qa['partial_rows']), 1)

    def test_shifted_or_missing_heading_holds_money_and_acceptance(self):
        for changed in ('', ' ' + '      |    |   |   Код   | Счет        |       Дебет          |     Кредит          |'):
            doc = document([row('1', debit='2.00'), 'Purpose preserved'])
            doc['pages'][0]['lines'][9] = line(changed)
            statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
            op = statement['operations'][0]['properties']
            self.assertIsNone(op['amount'])
            self.assertEqual(op['direction'], 'unknown')
            self.assertEqual(op['purpose_raw'], 'Purpose preserved')
            self.assertFalse(statement['properties']['rows_complete'])
            self.assertFalse(qa['column_contract']['recognized'])

    def test_integer_acquiring_labels_without_punctuation(self):
        purpose = 'ПЕРЕЧИСЛЕНИЕ ПРИ РАСЧЕТАХ ПО КАРТОЧКАМ.\nНА ОБЩУЮ СУММУ 250000, КОМИССИЯ 10000'
        statement, entities, _, _ = self.graph([row('1', credit='240000.00'), *purpose.split('\n')])
        self.assertEqual(statement['operations'][0]['properties']['amount'], '240000.00')
        details = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
        self.assertEqual([(n['kind'], n['value'], n['inclusion']) for n in details],
                         [('gross', '250000', 'unknown'), ('fee', '10000', 'included')])

    def test_component_preserves_interior_spaces_and_never_reads_numeric_prefix(self):
        for text, expected in [('Комиссия банка - 123 45.00', '12345.00'),
                               ('Комиссия: 12.345', '12.345')]:
            details = adapter.amount_components(text, 'inflow', '900.00')
            self.assertEqual(len(details), 1)
            self.assertEqual(adapter.money(details[0][2]), expected)
        self.assertEqual(adapter.amount_components('Комиссия: 0.20 % от оборотов 999999.00', 'outflow', '10.00'), [])

    def test_spelled_percent_fee_is_rate_text_not_money_component(self):
        for unit in ('%', 'процент', 'процента', 'процентов', 'percent', 'percents'):
            purpose = 'Комиссия 2 ' + unit + ' от суммы оборотов 1000'
            statement, entities, graph, _ = self.graph([row('1', debit='20.00'), purpose])
            self.assertEqual([n for n in entities if n['type'] == 'AmountComponent'], [])
            self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
            self.assertEqual(statement['operations'][0]['properties']['amount'], '20.00')
            self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_abbreviated_percent_details_remain_only_in_full_purpose(self):
        for unit in ('проц', 'проц.', 'ПРОЦ.'):
            for rate in ('2', '2.50', '2,50', '12'):
                with self.subTest(unit=unit, rate=rate):
                    purpose = 'Комиссия ' + rate + ' ' + unit + ' от оборота 1000; Комиссия: 15.00'
                    statement, entities, graph, _ = self.graph([row('1', debit='20.00'), purpose])
                    components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
                    self.assertEqual([(c['kind'], c['value']) for c in components], [('fee', '15.00')])
                    self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
                    self.assertEqual(statement['operations'][0]['properties']['amount'], '20.00')
                    self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_grouped_rates_cannot_backtrack_into_money_prefixes(self):
        values = ('1 234', '1 234,50', '1 234.50', '1  234,50',
                  '1\u00a0234.50', '1\u00a0234\u00a0567,50')
        for unit in ('проц', 'проц.', 'ПРОЦ.', '%', 'процент', 'percent'):
            for rate in values:
                with self.subTest(unit=unit, rate=rate):
                    purpose = 'Комиссия ' + rate + ' ' + unit + ' от оборота; Комиссия: 19.25'
                    statement, entities, graph, _ = self.graph([row('1', debit='19.25'), purpose])
                    components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
                    self.assertEqual([(c['kind'], c['value']) for c in components], [('fee', '19.25')])
                    self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
                    self.assertEqual(statement['operations'][0]['properties']['amount'], '19.25')
                    self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)
        for value in values:
            with self.subTest(monetary_value=value):
                purpose = 'Комиссия: ' + value + '; За услуги SYN-1'
                statement, entities, _, _ = self.graph([row('1', debit='19.25'), purpose])
                components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
                self.assertEqual([(c['kind'], c['value']) for c in components], [('fee', adapter.money(value))])
                self.assertEqual(components[0]['value_raw'].strip(), value)
                self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)

    def test_single_printed_zero_preserves_observation_without_direction(self):
        for side in ('debit', 'credit'):
            for spelling in ('0', '0.00', '0,00'):
                with self.subTest(side=side, spelling=spelling):
                    statement, _, graph, qa = self.graph([row('1', **{side: spelling}), 'Zero purpose',
                                                         row('2', credit='3.00'), 'Independent purpose'])
                    held, intact = statement['operations']
                    props = held['properties']
                    self.assertEqual(props['printed_' + side], adapter.money(spelling))
                    other = 'credit' if side == 'debit' else 'debit'
                    self.assertIsNone(props['printed_' + other])
                    self.assertEqual((props['direction'], props['amount'], props['row_status']),
                                     ('unknown', None, 'partial'))
                    observed = next(o for o in held['observations'] if o['field'] == 'printed_' + side)
                    self.assertEqual(observed['raw'].strip(), spelling)
                    self.assertEqual((observed['normalized'], observed['status']), (adapter.money(spelling), 'observed'))
                    blank = next(o for o in held['observations'] if o['field'] == 'printed_' + other)
                    self.assertEqual((blank['normalized'], blank['status']), (None, 'absent'))
                    self.assertFalse(any(e['type'] in ('FROM_ACCOUNT', 'TO_ACCOUNT') and
                                         e['from'].endswith(':row:1') for e in graph['edges']))
                    self.assertEqual((intact['properties']['direction'], intact['properties']['amount'],
                                      intact['properties']['row_status']), ('inflow', '3.00', 'complete'))
                    self.assertEqual(intact['properties']['purpose_raw'], 'Independent purpose')
                    self.assertFalse(statement['properties']['rows_complete'])
                    self.assertEqual([p['ordinal'] for p in qa['partial_rows']], [1])
                    controls = {'statements': [{'statement_id': 'dataset:synthetic:statement:1',
                                 'opening': '0.00', 'closing': '1.00', 'printed_debit_total': '2.00',
                                 'printed_credit_total': '3.00', 'no_activity': False, 'opening_raw': '0.00',
                                 'first_for_account': True, 'first_account_basis': 'synthetic marker'}]}
                    acceptance = sg.check_conversion(graph, controls)
                    self.assertEqual(acceptance['status'], 'failed')
                    self.assertIn('ambiguous_bank_cells', acceptance['statements'][0]['issues'])

    def test_acquiring_components_do_not_enter_printed_cash_controls(self):
        doc = document([row('1', credit='985.00'), 'Эквайринг. Общая сумма: 1000.00; удержанная комиссия: 15.00'],
                       debit_total='0.00', credit_total='985.00', closing='985.00')
        statement, entities, relations, _ = adapter.parse_statement(doc, 1, 'synthetic')
        currency = next(n['properties'] for n in entities if n['type'] == 'CurrencyContext')
        currency.update(semantics_status='verified', unit_raw='synthetic units', epoch='synthetic', evidence_refs=['synthetic review'])
        graph = diagnostic_build({'dataset_id': 'synthetic', 'entities': entities, 'statements': [statement], 'relations': relations})
        controls = {'statements': [{'statement_id': 'dataset:synthetic:statement:1', 'opening': '0.00',
                     'closing': '985.00', 'printed_debit_total': '0.00', 'printed_credit_total': '985.00',
                     'no_activity': False, 'opening_raw': '0.00', 'first_for_account': True,
                     'first_account_basis': 'synthetic printed first-statement marker'}]}
        result = sg.check_conversion(graph, controls)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['processing_admission']['status'], 'blocked')
        self.assertEqual(result['statements'][0]['computed_debit'], '0')
        self.assertEqual(result['statements'][0]['computed_credit'], '985.00')

    def test_date_drift_is_resolved_only_within_tail_columns(self):
        doc = document([row('1', debit='2.00'), 'Purpose'])
        header = doc['pages'][0]['lines'][10]
        header['text'] = header['text'].replace('01/01/2010', '0 1/01/2010')
        statement, _, _, _ = adapter.parse_statement(doc, 1, 'synthetic')
        self.assertEqual(statement['operations'][0]['properties']['row_status'], 'complete')
        header['raw_text'] = header['raw_text'].replace('01/01/2010', '02/01/2010')
        statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
        self.assertEqual(statement['operations'][0]['properties']['row_status'], 'partial')
        self.assertIn('date/suffix boundary unresolved', qa['partial_rows'][0]['gaps'])

    def test_unreviewed_continuation_is_partial_but_preserved(self):
        doc = document([row('1', debit='2.00'), 'First purpose line', 'Second purpose line',
                        'Continuation line', row('2', credit='3.00'), 'Independent purpose'])
        doc.pop('reviewed_continuations')
        statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
        first, second = [r['properties'] for r in statement['operations']]
        self.assertEqual(first['purpose_raw'], 'First purpose line\nSecond purpose line\nContinuation line')
        self.assertEqual(first['row_status'], 'partial')
        self.assertEqual(second['row_status'], 'complete')
        self.assertIn('continuation boundary unreviewed: 1 -> 2', qa['partial_rows'][0]['gaps'])

    def test_numeric_reference_list_is_purpose_and_continues_across_pages(self):
        body = [row('1', debit='2.00'), 'Очередность: 22', '1234 5678 9012 ОТ 01.01.2010. КОМИССИЯ БАНКА - 0.01',
                'Continuation, exact  two spaces.', row('2', credit='3.00'), 'Очередность: 0', 'Synthetic receipt']
        statement, _, _, qa = adapter.parse_statement(document(body), 1, 'synthetic')
        self.assertEqual(len(statement['operations']), 2)
        first = statement['operations'][0]['properties']
        self.assertEqual(first['purpose_raw'], body[2] + '\n' + body[3])
        self.assertEqual(first['printed_debit'], '2.00')
        self.assertIsNone(first['printed_credit'])
        self.assertEqual(first['unlabelled_numeric_raw'], '1234')
        self.assertEqual(first['transaction_date'], '2010-01-01')
        self.assertEqual(first['row_status'], 'complete')
        self.assertEqual(qa['partial_rows'], [])

    def test_empty_statement_does_not_invent_turnovers(self):
        statement, _, _, _ = adapter.parse_statement(document([], empty=True), 1, 'synthetic')
        self.assertEqual(statement['operations'], [])
        self.assertIsNone(statement['properties']['printed_debit_total'])
        self.assertIsNone(statement['properties']['printed_credit_total'])
        self.assertTrue(statement['properties']['rows_complete'])

    def test_ambiguous_money_keeps_original_row_and_is_partial(self):
        body = [row('1', debit='2.00', credit='3.00'), 'Очередность: 22', 'Original meaningful purpose']
        statement, _, _, qa = adapter.parse_statement(document(body), 1, 'synthetic')
        self.assertEqual(len(statement['operations']), 1)
        self.assertEqual(statement['operations'][0]['properties']['row_status'], 'partial')
        self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], body[2])
        self.assertFalse(statement['properties']['rows_complete'])
        self.assertEqual(len(qa['partial_rows']), 1)

    def test_damaged_second_row_tail_keeps_separate_partial_operation(self):
        for damaged in (row('2', credit='3.00', suffix='').rstrip(),
                        row('2', credit='3.00').replace('01/01/2010', '??/01/2010')):
            doc = document([row('1', debit='2.00'), 'First purpose', damaged, 'Second purpose'])
            statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
            self.assertEqual(len(statement['operations']), 2)
            first, second = [r['properties'] for r in statement['operations']]
            self.assertEqual((first['purpose_raw'], first['row_status']), ('First purpose', 'complete'))
            self.assertEqual((second['purpose_raw'], second['row_status']), ('Second purpose', 'partial'))
            self.assertEqual(second['document_number_raw'], '2')
            self.assertEqual(second['printed_credit'], '3.00')
            self.assertIsNone(second['transaction_date'])
            self.assertFalse(statement['properties']['rows_complete'])
            self.assertEqual([g['ordinal'] for g in qa['partial_rows']], [2])

    def test_debit_credit_words_in_purpose_do_not_change_header_contract(self):
        purpose = 'Сверка Дебет и Кредит; произвольные слова'
        statement, _, _, qa = adapter.parse_statement(document([row('1', debit='2.00'), purpose,
                                                               row('2', credit='3.00'), 'Second']), 1, 'synthetic')
        self.assertTrue(qa['column_contract']['recognized'])
        self.assertEqual(len(qa['column_contract']['evidence']), 1)
        self.assertEqual([r['properties']['row_status'] for r in statement['operations']], ['complete', 'complete'])
        self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)

    def test_blank_or_unreadable_account_does_not_erase_row_boundary(self):
        for account in (' ' * 14, '98765?3210001'.ljust(14)):
            second_raw = row('2', credit='3.00')
            second_raw = second_raw[:25] + account + second_raw[39:]
            statement, entities, _, qa = adapter.parse_statement(document([row('1', debit='2.00'), 'First',
                                                                            second_raw, 'Second']), 1, 'synthetic')
            self.assertEqual(len(statement['operations']), 2)
            first, second = [r['properties'] for r in statement['operations']]
            self.assertEqual((first['purpose_raw'], first['row_status']), ('First', 'complete'))
            self.assertEqual((second['purpose_raw'], second['row_status']), ('Second', 'partial'))
            self.assertEqual(second['printed_credit'], '3.00')
            self.assertFalse(statement['properties']['rows_complete'])
            self.assertEqual([g['ordinal'] for g in qa['partial_rows']], [2])
            self.assertIn('correspondent account unreadable or absent', qa['partial_rows'][0]['gaps'])
            corr = next(n for n in entities if n['alias'] == 'statement:1:row:2:correspondent-account')
            self.assertEqual(corr['properties']['identity_status'], 'unknown')

    def test_blank_money_cells_do_not_erase_row_with_known_tail(self):
        statement, _, _, qa = adapter.parse_statement(document([row('1', debit='2.00'), 'First',
                                                                row('2'), 'Second']), 1, 'synthetic')
        self.assertEqual(len(statement['operations']), 2)
        first, second = [r['properties'] for r in statement['operations']]
        self.assertEqual((first['purpose_raw'], first['row_status']), ('First', 'complete'))
        self.assertEqual((second['purpose_raw'], second['row_status']), ('Second', 'partial'))
        self.assertIsNone(second['amount'])
        self.assertIsNone(second['printed_debit'])
        self.assertIsNone(second['printed_credit'])
        self.assertEqual(second['direction'], 'unknown')
        self.assertFalse(statement['properties']['rows_complete'])
        self.assertEqual([g['ordinal'] for g in qa['partial_rows']], [2])

    def test_gross_net_labels_without_colon_preserve_all_components(self):
        for purpose in ('Эквайринг gross 1000; net 985; комиссия 15',
                        'Эквайринг gross1000; net985; комиссия15',
                        'Эквайринг общая сумма 1000; сумма нетто 985; комиссия 15'):
            statement, entities, graph, _ = self.graph([row('1', credit='985.00'), purpose])
            details = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
            self.assertEqual([(n['kind'], n['value']) for n in details], [('gross', '1000'), ('net', '985'), ('fee', '15')])
            self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
            self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_account_shaped_numeric_reference_does_not_split_purpose(self):
        for reference in ('123456123451234' + ' ' * 10 + '12345678901234',
                          '123456123451234' + '123456789012345678901234'):
            purpose = 'Reference follows\n' + reference + '\nContinuation'
            statement, _, graph, qa = self.graph([row('1', debit='2.00'), *purpose.split('\n'),
                                                  row('2', credit='3.00'), 'Independent'])
            self.assertEqual(len(statement['operations']), 2)
            self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
            self.assertEqual([x['properties']['amount'] for x in statement['operations']], ['2.00', '3.00'])
            self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)
            self.assertEqual(qa['partial_rows'], [])

    def test_long_numeric_reference_cannot_supply_its_own_money_evidence(self):
        for length in (38, 39, 40, 50, 61, 62, 63, 83, 84, 85, 100):
            with self.subTest(length=length):
                reference = ('1234567890' * 10)[:length]
                purpose = 'Reference follows\n' + reference + '\nContinuation'
                statement, _, graph, qa = self.graph([row('1', debit='2.00'), *purpose.split('\n'),
                                                      row('2', credit='3.00'), 'Independent'])
                ops = [x['properties'] for x in statement['operations']]
                self.assertEqual(len(ops), 2)
                self.assertEqual(ops[0]['purpose_raw'], purpose)
                self.assertEqual([x['amount'] for x in ops], ['2.00', '3.00'])
                self.assertEqual([x['row_status'] for x in ops], ['complete', 'complete'])
                self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)
                self.assertEqual(qa['partial_rows'], [])

    def test_damaged_tail_retains_padded_rows_with_full_document_and_executor(self):
        second = row('123456', credit='3.00').replace('01/01/2010', '??/01/2010')
        second = second[:11] + '1234' + second[15:]
        statement, _, _, qa = self.graph([row('1', debit='2.00'), 'First', second, 'Second'])
        ops = [x['properties'] for x in statement['operations']]
        self.assertEqual(len(ops), 2)
        self.assertEqual([x['purpose_raw'] for x in ops], ['First', 'Second'])
        self.assertEqual([x['amount'] for x in ops], ['2.00', '3.00'])
        self.assertEqual([x['row_status'] for x in ops], ['complete', 'partial'])
        self.assertEqual(ops[1]['document_number_raw'], '123456')
        self.assertEqual(ops[1]['executor_code_raw'], '1234')
        self.assertEqual([x['ordinal'] for x in qa['partial_rows']], [2])

    def test_grouped_numeric_reference_is_not_observed_code_padding(self):
        reference = '123456' + ' 123 ' + '1234' + '1234567890' * 7
        statement, _, graph, qa = self.graph([row('1', debit='2.00'), reference,
                                              row('2', credit='3.00'), 'Independent'])
        self.assertEqual(len(statement['operations']), 2)
        self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], reference)
        self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)
        self.assertEqual(qa['partial_rows'], [])

    def test_heading_prefix_prose_preserves_purpose_and_independent_money(self):
        for prefix in ('N документа 123 по договору', 'N док 123 по договору',
                       '|опер prose reference', '|    | purpose reference'):
            with self.subTest(prefix=prefix):
                purpose = prefix + '\nOrdinary continuation\nAnother purpose line'
                statement, _, graph, qa = self.graph([row('1', debit='2.00'), *purpose.split('\n'),
                                                      row('2', credit='3.00'), 'Independent'])
                ops = [x['properties'] for x in statement['operations']]
                self.assertEqual(len(ops), 2)
                self.assertEqual(ops[0]['purpose_raw'], purpose)
                self.assertEqual([x['amount'] for x in ops], ['2.00', '3.00'])
                self.assertEqual([x['row_status'] for x in ops], ['complete', 'complete'])
                self.assertEqual(len(qa['column_contract']['evidence']), 1)
                self.assertEqual(qa['partial_rows'], [])
                self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)

    def test_dated_numeric_purpose_needs_independent_row_padding(self):
        for start in ('123456789012345', '123456' + ' 123 ' + '1234'):
            for middle in ('', '1234567890' * 6):
                with self.subTest(start=start, middle=middle):
                    reference = (start + middle).ljust(84) + '01/01/2010  1234'
                    purpose = 'Reference follows\n' + reference + '\nContinuation'
                    statement, _, graph, qa = self.graph([row('1', debit='2.00'), *purpose.split('\n'),
                                                          row('2', credit='3.00'), 'Independent'])
                    ops = [x['properties'] for x in statement['operations']]
                    self.assertEqual(len(ops), 2)
                    self.assertEqual(ops[0]['purpose_raw'], purpose)
                    self.assertEqual([x['amount'] for x in ops], ['2.00', '3.00'])
                    self.assertEqual([x['row_status'] for x in ops], ['complete', 'complete'])
                    self.assertEqual(qa['partial_rows'], [])
                    self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)

    def test_bad_repeated_heading_holds_only_its_segment(self):
        for damage in ('shifted', 'top_overlap', 'middle_overlap', 'bottom_overlap',
                       'missing_bottom', 'missing_middle', 'removed_bottom', 'removed_middle',
                       'both_blank', 'both_removed', 'missing_top'):
            for same_page in (False, True):
                with self.subTest(damage=damage, same_page=same_page):
                    doc = document([])
                    header = doc['pages'][0]['lines'][:10]
                    bad = [dict(x) for x in header[7:]]
                    if damage == 'shifted':
                        bad[2] = line(' ' + bad[2]['text'])
                    elif damage == 'missing_bottom':
                        bad[2] = line('')
                    elif damage == 'missing_middle':
                        bad[1] = line('')
                    elif damage == 'both_blank':
                        bad[1:] = [line(''), line('')]
                    elif damage == 'both_removed':
                        bad[1:] = []
                    elif damage == 'missing_top':
                        bad = bad[1:]
                    elif damage.startswith('removed_'):
                        del bad[2 if damage == 'removed_bottom' else 1]
                    else:
                        bad[{'top_overlap': 0, 'middle_overlap': 1, 'bottom_overlap': 2}[damage]]['overlaps'] = [39]
                    segments = [header + [line(row('1', debit='2.00')), line('First')],
                                bad + [line(row('2', credit='3.00')), line('Held')],
                                [dict(x) for x in header[7:]] + [line(row('3', credit='4.00')), line('Third'),
                                                              line('-'*90), line('Исходящее сальдо 5.00')]]
                    doc['pages'] = ([{'page': 1, 'lines': sum(segments, [])}] if same_page else
                                    [{'page': i, 'lines': ls} for i, ls in enumerate(segments, 1)])
                    statement, entities, relations, qa = adapter.parse_statement(doc, 1, 'synthetic')
                    ops = [x['properties'] for x in statement['operations']]
                    self.assertEqual([x['amount'] for x in ops], ['2.00', None, '4.00'])
                    self.assertEqual([x['direction'] for x in ops], ['outflow', 'unknown', 'inflow'])
                    self.assertEqual([x['row_status'] for x in ops], ['complete', 'partial', 'complete'])
                    self.assertEqual([x['purpose_raw'] for x in ops], ['First', 'Held', 'Third'])
                    self.assertEqual([x['ordinal'] for x in qa['partial_rows']], [2])
                    self.assertEqual([x['recognized'] for x in qa['column_contract']['evidence']], [True, False, True])
                    self.assertFalse(statement['properties']['rows_complete'])
                    graph = diagnostic_build({'dataset_id': 'synthetic', 'entities': entities,
                                      'statements': [statement], 'relations': relations})
                    self.assertTrue(sg.validate(graph)['valid'])

    def test_all_observed_heading_presence_combinations_and_prose_controls(self):
        for mask in range(8):
            for absent in ('blank', 'removed'):
                for same_page in (False, True):
                    with self.subTest(mask=mask, absent=absent, same_page=same_page):
                        doc = document([])
                        header = doc['pages'][0]['lines'][:10]
                        fragments = [dict(header[7 + i]) if mask & (1 << i) else line('') for i in range(3)]
                        if absent == 'removed':
                            fragments = [x for x in fragments if x['text']]
                        segments = [header + [line(row('1', debit='2.00')), line('First')],
                                    fragments + [line(row('2', credit='3.00')), line('Held')],
                                    header[7:] + [line(row('3', credit='4.00')), line('Third'),
                                                  line('-'*90), line('Исходящее сальдо 5.00')]]
                        doc['pages'] = ([{'page': 1, 'lines': sum(segments, [])}] if same_page else
                                        [{'page': i, 'lines': ls} for i, ls in enumerate(segments, 1)])
                        statement, _, _, qa = adapter.parse_statement(doc, 1, 'synthetic')
                        ops = [x['properties'] for x in statement['operations']]
                        self.assertEqual([x['purpose_raw'] for x in ops], ['First', 'Held', 'Third'])
                        # No visible fragment cannot prove an invisible heading.
                        held = mask not in (0, 7)
                        self.assertEqual([x['amount'] for x in ops], ['2.00', None if held else '3.00', '4.00'])
                        self.assertEqual([x['row_status'] for x in ops], ['complete', 'partial' if held else 'complete', 'complete'])
                        self.assertEqual([x['ordinal'] for x in qa['partial_rows']], [2] if held else [])
        prose_cases = ['N документа 123 по договору', 'N док 123 по договору', '|опер prose reference',
                       '|    | purpose reference', 'Reference |опер|исп| with a prose note',
                       '|    | Код Счет Дебет Кредит are ordinary prose',
                       'Reference |опер|исп| with a prose note\n|    | Код Счет Дебет Кредит are ordinary prose']
        for prose in prose_cases:
            statement, _, _, qa = self.graph([row('1', debit='2.00'), *prose.split('\n'),
                                             row('2', credit='3.00'), 'Independent'])
            self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], prose)
            self.assertEqual([x['properties']['amount'] for x in statement['operations']], ['2.00', '3.00'])
            self.assertEqual(qa['partial_rows'], [])

    def test_observed_mixed_script_and_spaced_commission_labels_preserve_mentions(self):
        for label in ('УДЕРЖАННАЯ КОМИCСИЯ', 'К ОМИССИЯ', 'УДЕРЖАННАЯ К ОМИССИЯ'):
            with self.subTest(label=label):
                purpose = 'Эквайринг gross 1000; ' + label + ' 15'
                statement, entities, graph, _ = self.graph([row('1', credit='985.00'), purpose])
                components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
                self.assertEqual([(c['kind'], c['value'], c['inclusion']) for c in components],
                                 [('gross', '1000', 'included'), ('fee', '15', 'included')])
                fee = next(n for n in entities if n['type'] == 'AmountComponent' and n['properties']['kind'] == 'fee')
                mention = next(o['raw'] for o in fee['observations'] if o['field'] == 'mention_raw')
                self.assertEqual(mention, label + ' 15')
                self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
                self.assertEqual(statement['operations'][0]['properties']['amount'], '985.00')
                self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)

    def test_spaced_fractional_components_are_whole_tokens_and_rates_stay_text(self):
        for value, expected in (('6000. 00', '6000.00'), ('12. 50', '12.50'),
                                ('12,\u00a050', '12.50'), ('1 234, 50', '1234.50')):
            with self.subTest(value=value):
                purpose = 'Комиссия банка - ' + value + '; За услуги SYN-1'
                statement, entities, _, _ = self.graph([row('1', debit='19.25'), purpose])
                node = next(n for n in entities if n['type'] == 'AmountComponent')
                component = node['properties']
                self.assertEqual(component['value'], expected)
                self.assertEqual(component['value_raw'].strip(), value)
                mention = next(o['raw'] for o in node['observations'] if o['field'] == 'mention_raw')
                self.assertEqual(mention.strip(), 'Комиссия банка - ' + value)
                self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
                for unit in ('проц', 'проц.', '%', 'процент'):
                    rate_purpose = 'Комиссия ' + value + ' ' + unit + ' от оборота; Комиссия 19.25'
                    _, entities, _, _ = self.graph([row('1', debit='19.25'), rate_purpose])
                    self.assertEqual([n['properties']['value'] for n in entities if n['type'] == 'AmountComponent'], ['19.25'])

    def test_explicit_na_summu_gross_preserves_value_without_invented_inclusion(self):
        for label in ('НА СУММУ', 'на сумму:', 'На сумму -'):
            for value in ('1 000', '1 000. 00', '1\u00a0000,00'):
                with self.subTest(label=label, value=value):
                    purpose = 'Расчёт по синтетическому реестру; ' + label + ' ' + value + '; КОМИССИЯ 15'
                    statement, entities, graph, _ = self.graph([row('1', credit='985.00'), purpose])
                    components = [n['properties'] for n in entities if n['type'] == 'AmountComponent']
                    self.assertEqual([(c['kind'], c['value'], c['inclusion']) for c in components],
                                     [('gross', adapter.money(value), 'unknown'), ('fee', '15', 'unknown')])
                    gross = next(n for n in entities if n['type'] == 'AmountComponent' and n['properties']['kind'] == 'gross')
                    mention = next(o['raw'] for o in gross['observations'] if o['field'] == 'mention_raw')
                    self.assertEqual(mention.strip(), label + ' ' + value)
                    self.assertEqual(statement['operations'][0]['properties']['purpose_raw'], purpose)
                    self.assertEqual(statement['operations'][0]['properties']['amount'], '985.00')
                    self.assertEqual(sum(n['type'] == 'Operation' for n in graph['nodes']), 1)
                    acquiring = adapter.amount_components('Эквайринг ' + purpose, 'inflow', '985.00')
                    self.assertEqual([c[4] for c in acquiring], ['unknown', 'included'])
        for prose in ('цена сумму 1000', 'на суммуX 1000', 'на сумму итог 1000',
                      'НА СУММУ 1 234,50 проц от оборота'):
            self.assertEqual(adapter.amount_components(prose, 'inflow', '985.00'), [])
        self.assertEqual([c[1] for c in adapter.amount_components('КОМИССИЯ 15', 'inflow', '985.00')], ['fee'])

    def test_unnormalizable_component_is_unknown_and_independent_rows_survive(self):
        for purpose in ('Комиссия 0015.00', 'Эквайринг gross 1000; комиссия 0015.00',
                        'Эквайринг gross 01000; удержанная комиссия 15',
                        'Удержанная комиссия 0015.00', 'net 000985.00'):
            with self.subTest(purpose=purpose):
                statement, entities, graph, qa = self.graph([row('1', credit='985.00'), purpose,
                                                            row('2', debit='2.00'), 'Independent'])
                ops = [x['properties'] for x in statement['operations']]
                self.assertEqual([x['amount'] for x in ops], ['985.00', '2.00'])
                self.assertEqual(ops[0]['purpose_raw'], purpose)
                self.assertEqual([x['row_status'] for x in ops], ['complete', 'complete'])
                components = [x for x in entities if x['type'] == 'AmountComponent']
                bad = [x for x in components if x['properties']['value_raw'].startswith('0')]
                self.assertEqual(len(bad), 1)
                self.assertIsNone(bad[0]['properties']['value'])
                self.assertEqual(bad[0]['properties']['inclusion'], 'unknown')
                value_obs = next(x for x in bad[0]['observations'] if x['field'] == 'value')
                self.assertEqual(value_obs['status'], 'unknown')
                self.assertEqual(value_obs['raw'], bad[0]['properties']['value_raw'])
                self.assertEqual(sum(x['type'] == 'Operation' for x in graph['nodes']), 2)
                self.assertEqual(qa['partial_rows'], [])


if __name__ == '__main__':
    unittest.main()
