"""Synthetic wide export: bank cell isolation, wrapped suffix, intact type gates."""
import unittest
import archive_adapter as a
import statement_graph as sg
from test_archive_adapter import document,line


def wide_doc():
    doc=document([],debit_total='12.34',credit_total='0.00',closing='-12.34')
    header=doc['pages'][0]['lines'][:7]+[line(x) for x in [
        'N док |Вид |Код|   Корресподнент                              |                  Обороты                   |',
        '      |опер|исп|----------------------------------------------+---------------------------------------------  Дата',
        '      |    |   |   Код     | Счет                             |       Дебет          |     Кредит          |']]
    cells=[' ']*117
    for start,end,value in [(0,6,'81'),(6,11,'  6  '),(11,15,'56'),(15,27,'123456789'),(27,62,'9876543210001'),(62,85,'12.34'),(85,107,''),(107,117,'01/01/2010')]:
        cells[start:end]=list(value.rjust(end-start))
    body=[''.join(cells),'3050','Наименование корреспондента: SYNTHETIC PAYEE','УНП плательщика: 123456789 УНП получателя: 987654321','Очередность платежа: 22','Full purpose 999999.99 is not movement.']
    footer=['ОБОРОТЫ'.ljust(63)+'12.34'.rjust(22)+'0.00'.rjust(22),'Исходящее сальдо -12.34']
    doc['pages']=[{'page':1,'lines':header+[line(x) for x in body+footer],'business_fonts':['synthetic']}]
    return doc


class WideTests(unittest.TestCase):
    def test_damaged_code_with_bic_iban_keeps_two_rows_and_holds_only_second(self):
        doc=wide_doc()
        lines=doc['pages'][0]['lines']
        row=lines[10]['text']
        iban='BY12SYNB00000000000000000000'
        damaged=row[:6]+' ?*? '+row[11:15]+'SYNBBY2X'.rjust(12)+iban.rjust(35)+row[62:]
        detail=[line(x) for x in ['3050','Наименование корреспондента: SECOND PAYEE',
                'УНП плательщика: 123456789 УНП получателя: 987654321',
                'Очередность платежа: 22','Second complete purpose.']]
        lines[-2:-2]=[line(damaged)]+detail
        stmt,entities,relations,qa=a.parse_statement(doc,1,'synthetic-damaged-wide')
        self.assertEqual(len(stmt['operations']),2)
        first,second=[x['properties'] for x in stmt['operations']]
        self.assertEqual(first['purpose_raw'],'Full purpose 999999.99 is not movement.')
        self.assertEqual(second['purpose_raw'],'Second complete purpose.')
        self.assertIsNone(second['operation_code_raw'])
        raw=next(x['raw'] for x in stmt['operations'][1]['observations'] if x['field']=='bank_row_raw')
        self.assertEqual(raw[6:11],' ?*? ')
        self.assertEqual(second['row_status'],'partial')
        self.assertEqual((second['amount'],second['direction']),('12.34','outflow'))
        graph=sg.build({'dataset_id':'synthetic-damaged-wide','entities':entities,'relations':relations,'statements':[stmt]})
        self.assertTrue(sg.validate(graph)['valid'])
        decisions=[x['properties']['processing'] for x in graph['nodes'] if x['type']=='Operation']
        self.assertEqual([x['status'] for x in decisions],['ready','needs_clarification'])

    def test_damaged_code_purpose_reference_without_full_row_anatomy_is_detail(self):
        for broken in ('bank','account','money','date'):
            with self.subTest(broken=broken):
                doc=wide_doc();lines=doc['pages'][0]['lines'];row=lines[10]['text']
                reference=row[:6]+' ?*? '+row[11:15]+'SYNBBY2X'.rjust(12)+'BY12SYNB00000000000000000000'.rjust(35)+row[62:]
                start,end={'bank':(15,27),'account':(27,62),'money':(62,107),'date':(107,117)}[broken]
                reference=reference[:start]+'reference'.rjust(end-start)+reference[end:]
                lines.insert(-2,line(reference))
                stmt,_,_,_=a.parse_statement(doc,1,'synthetic-reference')
                self.assertEqual(len(stmt['operations']),1)
                self.assertIn('reference',stmt['operations'][0]['properties']['purpose_raw'])

    def test_exact_wide_columns_and_wrapped_suffix(self):
        stmt,_,_,qa=a.parse_statement(wide_doc(),1,'synthetic-wide')
        p=stmt['operations'][0]['properties']
        self.assertTrue(qa['column_contract']['recognized'])
        self.assertEqual(qa['column_contract']['columns']['printed_debit'],(62,85))
        self.assertEqual((p['amount'],p['direction'],p['transaction_date']),('12.34','outflow','2010-01-01'))
        self.assertEqual(p['unlabelled_numeric_raw'],'3050')
        self.assertEqual(p['purpose_raw'],'Full purpose 999999.99 is not movement.')
        self.assertEqual(p['counterparty']['unp_raw'],'987654321')
        self.assertEqual(stmt['properties']['printed_debit_total'],'12.34')
        self.assertEqual(qa['identity_admission']['status'],'admitted')
    def test_labelled_wide_iban_and_bic_are_preserved_strings(self):
        doc=wide_doc()
        row=doc['pages'][0]['lines'][10]['text']
        iban='BY94SYNB00000000000000000000'
        doc['pages'][0]['lines'][10]=line(row[:15]+'SYNBBY2X'.rjust(12)+iban.rjust(35)+row[62:])
        stmt,entities,relations,qa=a.parse_statement(doc,1,'synthetic-wide')
        account=next(x for x in entities if x['alias'].endswith(':correspondent-account'))
        bank=next(x for x in entities if x['alias'].endswith(':correspondent-bank'))
        self.assertEqual(account['properties']['number_raw'],iban)
        self.assertEqual(account['properties']['format'],'iban')
        self.assertEqual(bank['properties']['code_raw'],'SYNBBY2X')
        graph=sg.build({'dataset_id':'synthetic-wide','entities':entities,'relations':relations,'statements':[stmt]})
        self.assertTrue(sg.validate(graph)['valid'])
        banknode=next(n for n in graph['nodes'] if n['type']=='Bank' and n['properties']['code_system_raw']=='Код')
        banknode['properties']['code_raw']='SYN?BY2X'
        self.assertFalse(sg.validate(graph)['valid'])
        self.assertEqual(stmt['operations'][0]['properties']['amount'],'12.34')
        self.assertEqual(qa['identity_admission']['status'],'admitted')

    def test_shifted_wide_header_never_admits_money(self):
        doc=wide_doc();doc['pages'][0]['lines'][9]=line(' '+doc['pages'][0]['lines'][9]['text'])
        stmt,_,_,qa=a.parse_statement(doc,1,'synthetic-wide')
        self.assertFalse(qa['column_contract']['recognized'])
        self.assertIsNone(stmt['operations'][0]['properties']['amount'])
        self.assertEqual(qa['identity_admission']['status'],'blocked')
    def test_wide_without_name_type_remains_blocked(self):
        doc=wide_doc();doc['pages'][0]['lines']=[x for x in doc['pages'][0]['lines'] if not x['text'].startswith('Наименование корреспондента:')]
        _,_,_,qa=a.parse_statement(doc,1,'synthetic-wide')
        self.assertEqual(qa['identity_admission']['status'],'blocked')
        self.assertIn('counterparty_full_name',qa['identity_admission']['field_types']['missing'])

if __name__=='__main__':unittest.main()
