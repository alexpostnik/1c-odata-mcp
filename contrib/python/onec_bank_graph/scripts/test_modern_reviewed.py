"""Synthetic tests for the reviewed narrow modern and empty archival contracts."""
import copy
import unittest
import json
import tempfile
import subprocess
import sys
from pathlib import Path
import modern_adapter as m
import archive_adapter as a
import statement_graph as sg
from test_identifiers_modern_math import physical_line as line,modern_document,modern_row,IBAN
from test_archive_adapter import document


def confirmation():
    return {'question':m.ROLE_QUESTION,'answer':m.ROLE_ANSWER,'scope':m.REVIEWED_PROFILE,
            'answered_by':'synthetic-owner','evidence_ref':'synthetic-owner-format-evidence'}


def doc(name=None,unp='123456789',codes=None,bank=None,purpose='Complete synthetic purpose',acceptance=False):
    name=name or ['ООО СИНТЕТИКА','Г.МИНСК, РБ']
    bank=bank or ["Г.МИНСК,ОАО 'Синтетический", "банк'"]
    rows=[modern_row()];rows[0]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00','account':IBAN,'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side=name[0],top=10)
    rows.append(line({'document_number_raw':'03/01/2026','operation_code_raw':'Несрочный'},side=name[1] if len(name)>1 else '',top=11))
    rows += [line({},side=x,top=12+i) for i,x in enumerate(name[2:]+bank+['УНП:'+unp]+(codes or []))]
    if acceptance:rows.append(line({'operation_code_raw':'Без акцепта'},top=23))
    rows += [line({},side='Очередь 22',top=24),line({'65.3':purpose},top=25)]
    d=modern_document(rows);d['role_confirmation']=confirmation();return d


def parse(d):
    s,e,r,q=m.parse_statement(d,1,'synthetic-modern',reviewed=True)
    g=sg.build({'dataset_id':'synthetic-modern','statements':[s],'entities':e,'relations':r})
    return s,g,q


def op(g):return next(n for n in g['nodes'] if n['type']=='Operation')


class ReviewedModernTests(unittest.TestCase):
    def test_city_inside_name_and_uppercase_bank(self):
        s,g,q=parse(doc());p=op(g)['properties']
        self.assertEqual(p['counterparty']['name_raw'],'ООО СИНТЕТИКА\nГ.МИНСК, РБ')
        self.assertEqual(p['modern_fields']['bank_name_raw'],"Г.МИНСК,ОАО 'Синтетический\nбанк'")
        self.assertEqual(p['processing']['status'],'ready');self.assertTrue(sg.validate(g)['valid'])
        self.assertFalse(any('counterparty_field_type'==x['field'] for x in s['observations']))
        self.assertEqual(next(x for x in s['observations'] if x['field']=='modern_role_confirmation')['status'],'inferred')
    def test_bank_lexeme_wrapped_across_physical_lines(self):
        _,g,_=parse(doc(bank=["Г.МИНСК,'СИНТЕТИЧЕСКИЙБА","НК' ОАО"]))
        self.assertEqual(op(g)['properties']['modern_fields']['bank_name_raw'],"Г.МИНСК,'СИНТЕТИЧЕСКИЙБА\nНК' ОАО")
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
    def test_no_city_only_split(self):
        sf=m.sidecar_fields('ООО SYN\nГ.МИНСК, РБ\nУНП:123456789\nОчередь 22')
        self.assertIsNone(sf['name_raw']);self.assertIn('modern_name_bank_boundary_unresolved',sf['gaps'])
    def test_blank_unp_holds_row_without_fabrication(self):
        _,g,_=parse(doc(unp=''));p=op(g)['properties']
        self.assertEqual(p['modern_fields']['unp_raw'],'');self.assertIsNone(p['counterparty']['unp_raw'])
        self.assertEqual(p['processing']['status'],'needs_clarification');self.assertTrue(sg.validate(g)['valid'])
        self.assertEqual(sg.identity_admission(g)['status'],'partial')
    def test_alphanumeric_unp_preserved_not_digits(self):
        _,g,_=parse(doc(unp='AB 1234567'));p=op(g)['properties']
        self.assertEqual(p['modern_fields']['unp_raw'],'AB 1234567');self.assertEqual(p['counterparty']['unp_raw'],'AB1234567')
        self.assertEqual(p['amount'],'2.00');self.assertEqual(p['processing']['status'],'ready')
    def test_codes_after_unp_are_not_unp_continuation(self):
        _,g,_=parse(doc(codes=['0101','0099']));p=op(g)['properties']
        self.assertEqual(p['modern_fields']['unlabelled_codes_raw'],['0101','0099'])
        self.assertEqual(p['counterparty']['unp_raw'],'123456789');self.assertEqual(p['processing']['status'],'ready')
        self.assertEqual([x['value'] for x in op(g)['unrecognized_fields']],['0101','0099'])
    def test_passport_text_does_not_become_full_name(self):
        _,g,_=parse(doc(name=['Паспорт SYN 000001','выдан SYN']))
        p=op(g)['properties'];self.assertIsNone(p['counterparty']['name_raw'])
        self.assertEqual(p['modern_fields']['name_raw'],'Паспорт SYN 000001\nвыдан SYN')
        self.assertIn('modern_document_text_not_proven_party_name',p['processing']['gaps'])
    def test_series_number_issued_document_with_linewrap_holds_name(self):
        for name in [['SYNTHETIC PERSON','MC 0000007 ВЫДАН','СИНТЕТИЧЕСКИМ ОРГАНОМ'],['SYNTHETIC PERSON MP','0000008 ВЫДАН','СИНТЕТИЧЕСКИМ ОРГАНОМ']]:
            _,g,_=parse(doc(name=name,unp=''))
            self.assertIsNone(op(g)['properties']['counterparty']['name_raw'])
            self.assertIn('modern_document_text_not_proven_party_name',op(g)['properties']['processing']['gaps'])
    def test_dates_urgency_and_extra_acceptance_are_separate(self):
        _,g,_=parse(doc(acceptance=True));p=op(g)['properties']
        self.assertEqual((p['document_date'],p['transaction_date']),('2026-01-03','2026-01-05'))
        self.assertEqual(p['modern_fields']['urgency_raw'],'Несрочный');self.assertEqual(p['modern_fields']['acceptance_raw'],'Без акцепта')
        self.assertNotIn('акцепта',p['purpose_raw']);self.assertTrue(sg.validate(g)['valid'])
    def test_full_page_width_purpose_survives_right_table_edge(self):
        d=doc();d['pages'][0]['lines'][-3]=line({'65.3':'Full purpose ', '700':'far right tail'},top=25)
        _,g,_=parse(d);self.assertEqual(op(g)['properties']['purpose_raw'],'Full purpose far right tail')
    def test_crosspage_purpose_and_structural_web_header(self):
        d=doc();lines=d['pages'][0]['lines'];footer=lines[-2:];d['pages'][0]['lines']=lines[:-2]
        d['pages'].append({'page':2,'lines':[line({'65.3':'Клиент-банк (WEB) 2'},top=4),line({'65.3':'Continuation far ', '700':'right'},top=22)]+footer})
        d['reviewed_continuations']=[{'from_page':1,'to_page':2,'operation_ordinal':1,'decision':'same_operation','evidence':'synthetic second-engine original QA'}]
        _,g,q=parse(d);self.assertEqual(op(g)['properties']['purpose_raw'],'Complete synthetic purpose\nContinuation far right')
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertTrue(any(x['reason']=='page_header' for x in q['excluded_structural_lines']))
    def test_unreviewed_page_join_holds_only_row(self):
        d=doc();lines=d['pages'][0]['lines'];d['pages'][0]['lines']=lines[:-2];d['pages'].append({'page':2,'lines':[line({'65.3':'Continuation'},top=22)]+lines[-2:]})
        _,g,_=parse(d);self.assertEqual(op(g)['properties']['purpose_status'],'unknown');self.assertEqual(op(g)['properties']['processing']['status'],'needs_clarification')
    def test_terminal_seal_is_excluded_structurally(self):
        d=doc();d['pages'][0]['lines'] += [line({'65.3':'ОАО SYN банк'},top=92),line({'65.3':'БИК SYN0BY00 31.01.2026'},top=93)]
        _,g,q=parse(d);self.assertEqual(op(g)['properties']['purpose_raw'],'Complete synthetic purpose')
        self.assertTrue(any(x['reason']=='terminal_control_or_seal' for x in q['excluded_structural_lines']))
    def test_footer_words_in_business_purpose_are_kept(self):
        _,g,_=parse(doc(purpose='ОБОРОТЫ Исходящее сальдо БИК SYN0BY00 purpose'))
        self.assertIn('Исходящее сальдо',op(g)['properties']['purpose_raw'])
    def test_unrecognized_terminal_business_or_extra_row_is_not_dropped(self):
        for extra in [line({'65.3':'Unassigned business payment'},top=95),modern_row(top=95)]:
            d=doc();d['pages'][0]['lines'].append(extra)
            with self.assertRaises(ValueError):parse(d)
    def test_wrap_preserves_literal_newline_and_typed_identifiers(self):
        d=doc();first=d['pages'][0]['lines'][4];first['glyphs']=[x for x in first['glyphs'] if not (184.58<=x['x0']<396.79)]
        # Replace with physical short source fragments; next line supplies exact observed wrap.
        d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00AB','account':IBAN[:20],'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SYN',top=10)
        d['pages'][0]['lines'][5]=line({'document_number_raw':'03/01/2026','operation_code_raw':'Несрочный','bank_code':'C','account':IBAN[20:]},side='Г.МИНСК, РБ',top=11)
        _,g,q=parse(d);self.assertEqual(q['rows'][0]['bank_identifiers']['bank_code']['raw'],'SYN0BY00AB\nC');self.assertEqual(q['rows'][0]['bank_identifiers']['account']['normalized'],IBAN)
        self.assertTrue(sg.validate(g)['valid'])
    def test_damaged_code_and_identifiers_keep_row_money(self):
        d=doc();d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'?','bank_code':'BAD?','account':'BY??001','printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SYN',top=10)
        _,g,q=parse(d);p=op(g)['properties'];self.assertEqual((p['amount'],p['row_status']),('2.00','partial'))
        self.assertEqual(q['rows'][0]['bank_identifiers']['bank_code']['raw'],'BAD?');self.assertEqual(p['processing']['status'],'needs_clarification')
    def test_missing_or_wrong_owner_evidence_blocks_new_path(self):
        for value in [None,{},dict(confirmation(),answer='Нет'),dict(confirmation(),scope='different-format')]:
            d=doc();d['role_confirmation']=value
            with self.assertRaises(ValueError):parse(d)
    def test_invented_source_identity_and_purpose_are_rejected(self):
        _,g,_=parse(doc())
        for mutate in ('purpose','source','bank','date','transaction_date','operation_code','acceptance'):
            bad=copy.deepcopy(g);p=op(bad)['properties']
            if mutate=='purpose':p.update(purpose_raw='Replaced',payment1c_comment_raw='Replaced',payment1c_text=sg.prepare_payment_text('Replaced','observed'))
            elif mutate=='source':p['counterparty']['name_raw']='Fabricated'
            elif mutate=='bank':p['modern_fields']['bank_name_raw']='Fabricated'
            elif mutate=='date':p['document_date']='2026-01-04'
            elif mutate=='transaction_date':p['transaction_date']='2026-01-06'
            elif mutate=='operation_code':p['operation_code_raw']='9'
            else:p['modern_fields']['acceptance_raw']='Invented'
            sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'],mutate)
    def test_misbound_continuation_does_not_release_row(self):
        d=doc();lines=d['pages'][0]['lines'];d['pages'][0]['lines']=lines[:-2]
        d['pages'].append({'page':2,'lines':[line({'65.3':'Continuation'},top=22)]+lines[-2:]})
        d['reviewed_continuations']=[{'from_page':1,'to_page':2,'operation_ordinal':2,'decision':'same_operation','evidence':'wrong row proof'}]
        _,g,_=parse(d);self.assertEqual(op(g)['properties']['purpose_status'],'unknown')
    def test_linked_bank_name_and_code_cannot_be_substituted(self):
        _,g,_=parse(doc())
        for key,value in [('name_raw','Invented bank'),('identifier',__import__('bank_identifiers').recognize('SYN1BY00','bank'))]:
            bad=copy.deepcopy(g);bank=next(n for n in bad['nodes'] if n['type']=='Bank' and n['id'].endswith(':row:1:bank'));bank['properties'][key]=value
            if key=='identifier':bank['properties']['code_raw']='SYN1BY00'
            sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'])
    def test_confirmed_identity_is_separate_from_unchanged_passport_source(self):
        _,g,_=parse(doc(name=['Паспорт SYN 000001','выдан SYN'],unp=''))
        source=next(x['raw'] for x in op(g)['observations'] if x['field']=='identity_detail_raw')
        answer={'role':'payee','name_raw':'Synthetic confirmed person','unp_raw':'123456789','original_detail_raw':source,
                'answered_by':'synthetic-owner','answered_at':'2026-01-06T10:00:00+03:00','answer_ref':'synthetic-answer','confirmation_ref':'synthetic-confirmation'}
        resolved=sg.clarify_counterparties(g,{op(g)['id']:answer});p=op(resolved)['properties']
        self.assertEqual(p['processing']['effective_counterparty']['origin'],'owner_confirmed')
        self.assertEqual(p['processing']['status'],'ready');self.assertIsNone(p['counterparty']['name_raw'])
        self.assertIn('Паспорт',p['modern_fields']['name_raw']);self.assertTrue(sg.validate(resolved)['valid'])
    def test_modern_cli_saves_reviewed_input_with_explicit_nonacceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.json';source.write_text(json.dumps(doc()),encoding='utf-8')
            script=Path(a.__file__)
            result=subprocess.run([sys.executable,str(script),'modern-assemble',str(source),'--dataset-id','synthetic-cli','--output',str(root/'reviewed.json'),'--qa-output',str(root/'qa.json')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(json.loads(result.stdout)['accepted'])
            reviewed=json.loads((root/'reviewed.json').read_text());graph=sg.build(reviewed)
            self.assertEqual(sg.validate(graph)['operation_count'],1);self.assertTrue(sg.validate(graph)['valid'])
            view=json.loads((root/'qa.json').read_text())['rows'][0]['document_view']
            self.assertEqual(view['payment1c_text'],op(graph)['properties']['payment1c_text'])
            self.assertEqual(view['processing']['status'],'ready');self.assertEqual(view['bank_document_kind_candidate'],'Списание с расчетного счета')
    def test_generic_profile_flag_does_not_admit_legacy(self):
        _,g,_=parse(doc());st=next(n for n in g['nodes'] if n['type']=='Statement')
        st['observations']=[x for x in st['observations'] if x['field']!='modern_role_confirmation'];sg.materialize_processing(g)
        self.assertEqual(sg.identity_admission(g)['status'],'blocked');self.assertFalse(sg.validate(g)['valid'])


class EmptyArchiveTests(unittest.TestCase):
    def empty(self):
        d=document([],empty=True);d['pages'][0]['lines']+=d['pages'][1]['lines'];d['pages']=d['pages'][:1]
        # The real empty source has a rule after the header, not an invented row.
        d['pages'][0]['lines'].insert(-1,{'text':'-'*94,'raw_text':'-'*94,'top':1,'overlaps':[]})
        return d
    def test_empty_preserves_own_header_roles_without_transactions_or_turnovers(self):
        g,q=a.balance_only_archive(self.empty(),'synthetic-empty');st=next(n for n in g['nodes'] if n['type']=='Statement')
        self.assertFalse(any(n['type']=='Operation' for n in g['nodes']));self.assertEqual(st['properties']['expected_operations'],0)
        owner=next(n for n in g['nodes'] if n['type']=='Party')
        bank=next(n for n in g['nodes'] if n['type']=='Bank')
        self.assertTrue(any(e['type']=='OWNED_BY' and e['from']==st['properties']['account_id'] and e['to']==owner['id'] for e in g['edges']))
        self.assertTrue(any(e['type']=='SERVICED_BY' and e['from']==st['properties']['account_id'] and e['to']==bank['id'] for e in g['edges']))
        self.assertFalse(any(e['type'] in ('PAYER','PAYEE') for e in g['edges']))
        self.assertEqual(st['properties']['header_fields']['own_organization_name_assertions'][0]['raw'],owner['properties']['name_raw'])
        self.assertIsNone(st['properties']['printed_debit_total']);self.assertIsNone(st['properties']['printed_credit_total']);self.assertFalse(q['accepted'])
        self.assertTrue(sg.validate(g)['valid']);self.assertEqual(sg.identity_admission(g)['status'],'blocked')
        with self.assertRaises(ValueError):sg.require_processing_admission(g)
    def test_empty_cli_archival_output_is_explicitly_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'empty.json';source.write_text(json.dumps(self.empty()),encoding='utf-8')
            result=subprocess.run([sys.executable,str(Path(a.__file__)),'balance-only-archive',str(source),'--dataset-id','synthetic-empty-cli','--output',str(root/'archive.json'),'--qa-output',str(root/'qa.json')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);status=json.loads(result.stdout)
            self.assertFalse(status['accepted']);self.assertTrue(status['archive_only'])
            graph=json.loads((root/'archive.json').read_text());self.assertFalse(any(n['type']=='Operation' for n in graph['nodes']))
            self.assertEqual(sg.check_conversion(graph,{'statements':[]})['accepted'],False)
    def test_archive_rejects_nonempty_or_invented_balances(self):
        d=self.empty();d['pages'][0]['lines'].insert(-2,{'text':'unassigned business payment','raw_text':'unassigned business payment','top':1,'overlaps':[]})
        with self.assertRaises(ValueError):a.balance_only_archive(d,'synthetic-empty')
        d=self.empty();d['pages'][0]['lines']=[l for l in d['pages'][0]['lines'] if 'Входящее сальдо' not in l['raw_text']]
        with self.assertRaises((ValueError,StopIteration)):a.balance_only_archive(d,'synthetic-empty')

if __name__=='__main__':unittest.main()
