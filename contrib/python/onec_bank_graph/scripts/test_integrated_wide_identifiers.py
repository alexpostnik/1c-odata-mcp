"""Integration invariants between accepted wide geometry and typed identifiers."""
import unittest
import archive_adapter as a
import statement_graph as sg
import bank_identifiers as bi
from test_archive_adapter import line
from test_wide_archive import wide_doc

VALID_IBAN = 'BY94SYNB00000000000000000000'
INVALID_IBAN = 'BY12SYNB00000000000000000000'


def parsed_iban(iban):
    doc = wide_doc()
    row = doc['pages'][0]['lines'][10]['text']
    doc['pages'][0]['lines'][10] = line(row[:15] + 'SYNBBY2X'.rjust(12) + iban.rjust(35) + row[62:])
    return a.parse_statement(doc, 1, 'synthetic-integrated-wide')


class IntegratedWideTests(unittest.TestCase):
    def test_valid_iban_preserves_wide_suffix_purpose_and_ready_row(self):
        statement, entities, relations, qa = parsed_iban(VALID_IBAN)
        props = statement['operations'][0]['properties']
        self.assertEqual(qa['identity_admission']['status'], 'admitted')
        self.assertEqual((props['unlabelled_numeric_raw'], props['amount']), ('3050', '12.34'))
        self.assertEqual(props['purpose_raw'], 'Full purpose 999999.99 is not movement.')
        graph = sg.build({'dataset_id': 'synthetic-integrated-wide', 'statements': [statement], 'entities': entities, 'relations': relations})
        self.assertTrue(sg.validate(graph)['valid'])
        self.assertEqual(next(n for n in graph['nodes'] if n['type'] == 'Operation')['properties']['processing']['status'], 'ready')

    def test_invalid_checksum_retains_raw_and_money_but_holds_only_own_row(self):
        statement, entities, relations, qa = parsed_iban(INVALID_IBAN)
        account = next(e for e in entities if e['alias'].endswith(':correspondent-account'))['properties']
        self.assertEqual(account['number_raw'], INVALID_IBAN)
        self.assertEqual(account['identifier']['raw'].strip(), INVALID_IBAN)
        self.assertEqual((account['identifier']['format_status'], account['identifier']['value_check']), ('recognized', 'failed'))
        self.assertFalse(bi.usable(account['identifier']))
        self.assertEqual(account['identity_status'], 'unknown')
        props = statement['operations'][0]['properties']
        self.assertEqual((props['amount'], props['direction']), ('12.34', 'outflow'))
        self.assertEqual(props['row_status'], 'partial')
        self.assertEqual(qa['identity_admission']['status'], 'partial')
        graph = sg.build({'dataset_id': 'synthetic-integrated-wide', 'statements': [statement], 'entities': entities, 'relations': relations})
        self.assertTrue(sg.validate(graph)['valid'])
        operation = next(n for n in graph['nodes'] if n['type'] == 'Operation')
        self.assertIn('bank_identifier_unusable', operation['properties']['processing']['gaps'])
        operation['properties']['row_status'] = 'complete'
        sg.materialize_processing(graph)
        self.assertEqual(operation['properties']['processing']['status'], 'needs_clarification')
        self.assertIn('bank_identifier_unusable', operation['properties']['processing']['gaps'])


    def test_readable_bank_name_does_not_claim_unknown_code_identity(self):
        for code in ('QA?0BY2X', 'QA10BY2XAB', '', 'synbby2x', 'СYNBBY2X'):
            with self.subTest(code=code):
                doc = wide_doc()
                lines = doc['pages'][0]['lines']
                raw = lines[10]['text']
                lines[10] = line(raw[:15] + code.rjust(12) + VALID_IBAN.rjust(35) + raw[62:])
                lines.insert(14, line('Наименование банка: SYNTHETIC BANK'))
                statement, entities, relations, qa = a.parse_statement(doc, 1, 'synthetic-bank-name')
                bank = next(e for e in entities if e['alias'].endswith(':correspondent-bank'))
                bp = bank['properties']
                self.assertEqual(bp['name_raw'], 'SYNTHETIC BANK')
                self.assertEqual(bp['identity_status'], 'unknown')
                self.assertIsNone(bp['code_raw'])
                self.assertEqual(bp['identifier']['raw'].strip(), code)
                self.assertFalse(bi.usable(bp['identifier']))
                self.assertIsNone(bp['identifier']['normalized'])
                props = statement['operations'][0]['properties']
                self.assertEqual((props['amount'], props['direction']), ('12.34', 'outflow'))
                self.assertEqual(props['purpose_raw'], 'Full purpose 999999.99 is not movement.')
                graph = sg.build({'dataset_id': 'synthetic-bank-name', 'statements': [statement], 'entities': entities, 'relations': relations})
                self.assertTrue(sg.validate(graph)['valid'])
                # A forged observed identity must still fail recomputation.
                next(n for n in graph['nodes'] if n['type'] == 'Bank' and n['properties'].get('code_system_raw') == 'Код')['properties']['identity_status'] = 'observed'
                self.assertFalse(sg.validate(graph)['valid'])

    def test_unknown_identifier_spelling_retains_damaged_code_boundary(self):
        for code, account in [('synbby2x', VALID_IBAN), ('СYNBBY2X', VALID_IBAN),
                              ('synbbyxx', VALID_IBAN), ('SYNBBY2X', VALID_IBAN.lower()),
                              ('SYNBBY2X', 'В' + VALID_IBAN[1:])]:
            with self.subTest(code=code, account=account):
                doc = wide_doc()
                lines = doc['pages'][0]['lines']
                raw = lines[10]['text']
                damaged = raw[:6] + ' ?*? ' + raw[11:15] + code.rjust(12) + account.rjust(35) + raw[62:]
                detail = ['3050', 'Наименование корреспондента: SECOND PAYEE',
                          'УНП плательщика: 123456789 УНП получателя: 987654321',
                          'Очередность платежа: 22', 'Second complete purpose.']
                lines[-2:-2] = [line(damaged)] + [line(x) for x in detail]
                statement, entities, relations, qa = a.parse_statement(doc, 1, 'synthetic-unknown-boundary')
                self.assertEqual(len(statement['operations']), 2)
                first, second = [x['properties'] for x in statement['operations']]
                self.assertEqual(first['purpose_raw'], 'Full purpose 999999.99 is not movement.')
                self.assertEqual(second['purpose_raw'], 'Second complete purpose.')
                self.assertEqual((second['amount'], second['direction'], second['transaction_date']), ('12.34', 'outflow', '2010-01-01'))
                self.assertIsNone(second['operation_code_raw'])
                self.assertEqual(second['row_status'], 'partial')
                for kind, suffix, raw_value in [('bank', ':correspondent-bank', code), ('account', ':correspondent-account', account)]:
                    prop = next(e['properties'] for e in entities if e['alias'].endswith('row:2' + suffix))
                    self.assertEqual(prop['identifier']['raw'].strip(), raw_value)
                    if not bi.usable(bi.recognize(raw_value, kind)):
                        self.assertIsNone(prop['identifier']['normalized'])
                        self.assertEqual(prop['identity_status'], 'unknown')
                graph = sg.build({'dataset_id': 'synthetic-unknown-boundary', 'statements': [statement], 'entities': entities, 'relations': relations})
                self.assertTrue(sg.validate(graph)['valid'])
                decisions = [n['properties']['processing']['status'] for n in graph['nodes'] if n['type'] == 'Operation']
                self.assertEqual(decisions, ['ready', 'needs_clarification'])


if __name__ == '__main__':
    unittest.main()
