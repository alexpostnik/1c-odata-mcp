"""Behavioral regressions for fresh extraction and saved-source consistency."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import modern_adapter as m
import statement_graph as sg
from test_modern_reviewed import doc, parse, op, line, IBAN


def two_rows():
    d=doc();first=d['pages'][0]['lines'];second=copy.deepcopy(first[4:-2])
    second[0]=line({'document_number_raw':'002','operation_code_raw':'6','bank_code':'SYN0BY00',
                    'account':IBAN,'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SECOND',top=40)
    for n,l in enumerate(second[1:]):l['top']=41+n
    d['pages'][0]['lines']=first[:-2]+second+[
        line({'65.3':'ОБОРОТЫ','printed_debit':'4.00','printed_credit':'0.00'},top=90),
        line({'65.3':'Исходящее сальдо 6.00'},top=91)]
    return d


def signature(x=15,top=560.448):
    texts=['---Электронно цифровая подпись:---------','ИД эл. кл.: SYNTHETIC 00000001',
           'Владелец ключа: SYNTHETIC OWNER','Подписано: 2026-01-31 17:03:42']
    return [line({str(x):text},top=top+5*i) for i,text in enumerate(texts)]


class ModernSourceRegressions(unittest.TestCase):
    def held_first_ready_second(self,d,gap):
        _,g,q=parse(d);ops=[n for n in g['nodes'] if n['type']=='Operation']
        self.assertEqual(len(ops),2);self.assertTrue(sg.validate(g)['valid'],sg.validate(g)['errors'])
        self.assertEqual(ops[0]['properties']['processing']['status'],'needs_clarification')
        self.assertIn(gap,ops[0]['properties']['processing']['gaps'])
        self.assertEqual(ops[1]['properties']['processing']['status'],'ready')
        ops[0]['properties']['row_status']='complete';sg.materialize_processing(g)
        self.assertEqual(ops[0]['properties']['processing']['status'],'needs_clarification')
        return g,q

    def test_unknown_metadata_retained_and_hold_recomputed(self):
        d=two_rows();d['pages'][0]['lines'].insert(6,line({'document_number_raw':'UNKNOWNMETA'},top=11.5))
        g,q=self.held_first_ready_second(d,'modern_unknown_metadata')
        self.assertIn('UNKNOWNMETA',q['rows'][0]['metadata_raw'])
        self.assertIn('UNKNOWNMETA',next(o['raw'] for o in op(g)['observations'] if o['field']=='modern_metadata_cells_raw'))

    def test_unknown_other_metadata_cells_hold_without_erasure(self):
        for cells in [{'operation_code_raw':'New acceptance?'},{'bank_code':'bad code?'},
                      {'printed_credit':'1.00'},{'transaction_date':'04/01/2026'}]:
            d=two_rows();d['pages'][0]['lines'].insert(6,line(cells,top=11.5))
            with self.subTest(cells=cells):self.held_first_ready_second(d,'modern_unknown_metadata')

    def test_duplicate_document_dates_stay_raw_and_normalized_unknown(self):
        for second in ['04/01/2026','03/01/2026']:
            d=two_rows();d['pages'][0]['lines'].insert(6,line({'document_number_raw':second},top=11.5))
            g,q=self.held_first_ready_second(d,'modern_document_date_unresolved')
            self.assertIsNone(op(g)['properties']['document_date'])
            self.assertIn('03/01/2026',q['rows'][0]['metadata_raw']);self.assertIn(second,q['rows'][0]['metadata_raw'])

    def test_unreadable_document_date_preserves_both_rows(self):
        for raw in ['99/01/2026','??/01/2026','UNREADABLE','']:
            d=two_rows();d['pages'][0]['lines'][5]=line({'document_number_raw':raw,'operation_code_raw':'Несрочный'},side='Г.МИНСК, РБ',top=11)
            with self.subTest(raw=raw):
                g,q=self.held_first_ready_second(d,'modern_document_date_unresolved')
                self.assertIsNone(op(g)['properties']['document_date'])
                self.assertIn(raw,q['rows'][0]['metadata_raw'])

    def test_unreadable_or_duplicate_bank_date_keeps_anchor_and_money(self):
        for raw in ['99/01/2026','??/01/2026','UNREADABLE','05/01/2026 06/01/2026','']:
            d=two_rows();d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00','account':IBAN,
                'printed_debit':'2.00','printed_credit':'0.00','transaction_date':raw},side='ООО SYN',top=10)
            with self.subTest(raw=raw):
                g,q=self.held_first_ready_second(d,'modern_transaction_date_unresolved')
                p=op(g)['properties'];self.assertIsNone(p['transaction_date']);self.assertEqual(p['amount'],'2.00')
                self.assertEqual(next(o['raw'] for o in op(g)['observations'] if o['field']=='transaction_date'),raw)

    def test_partial_cli_writes_full_reviewed_and_qa_outputs(self):
        for raw in [None,'','??/01/2026','05/01/2026 06/01/2026','blank-account']:
            d=two_rows()
            if raw is None:d['pages'][0]['lines'].insert(6,line({'document_number_raw':'04/01/2026'},top=11.5))
            else:d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00','account':'' if raw=='blank-account' else IBAN,
                'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026' if raw=='blank-account' else raw},side='ООО SYN',top=10)
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);src=root/'source.json';src.write_text(json.dumps(d))
                p=subprocess.run([sys.executable,'-B',str(Path(m.__file__).with_name('archive_adapter.py')),'modern-assemble',str(src),
                    '--dataset-id','synthetic-dates','--output',str(root/'reviewed.json'),'--qa-output',str(root/'qa.json')],capture_output=True,text=True)
                self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(json.loads(p.stdout)['accepted'])
                g=sg.build(json.loads((root/'reviewed.json').read_text()));q=json.loads((root/'qa.json').read_text())
                self.assertEqual(len(q['rows']),2);self.assertTrue(sg.validate(g)['valid'])
                self.assertEqual(q['rows'][0]['document_view']['processing']['status'],'needs_clarification')
                self.assertEqual(q['rows'][1]['document_view']['processing']['status'],'ready')
                self.assertEqual(op(g)['properties']['amount'],'2.00')
                if raw is not None and raw!='blank-account':self.assertIsNone(op(g)['properties']['transaction_date'])

    def test_metadata_status_claim_cannot_hide_raw_unknown(self):
        d=two_rows();d['pages'][0]['lines'].insert(6,line({'operation_code_raw':'UNKNOWNMETA'},top=11.5))
        _,g,_=parse(d);p=op(g)['properties'];p['row_status']='complete'
        for o in op(g)['observations']:o['status']='observed'
        sg.materialize_processing(g);self.assertIn('modern_unknown_metadata',p['processing']['gaps'])

    def test_missing_or_malformed_retained_metadata_holds_row(self):
        _,g,_=parse(doc())
        for raw in [None,'bad-json','{}','[]']:
            bad=copy.deepcopy(g);o=next(o for o in op(bad)['observations'] if o['field']=='modern_metadata_cells_raw');o['raw']=raw
            sg.materialize_processing(bad)
            self.assertEqual(op(bad)['properties']['processing']['status'],'needs_clarification')

    def test_unknown_metadata_cannot_be_relabelled_as_optional_bank_fragment(self):
        d=doc();d['pages'][0]['lines'].insert(6,line({'document_number_raw':'UNKNOWNMETA'},top=11.5))
        _,g,_=parse(d);o=next(o for o in op(g)['observations'] if o['field']=='modern_metadata_cells_raw')
        rows=json.loads(o['raw']);rows[1]['cells']['bank_code']=rows[1]['cells']['document_number_raw'];rows[1]['cells']['document_number_raw']=''
        o['raw']=json.dumps(rows);op(g)['properties']['row_status']='complete';sg.materialize_processing(g)
        self.assertIn('modern_metadata_source_unresolved',op(g)['properties']['processing']['gaps'])

    def test_heading_raw_recomputed_despite_profile_status_claims(self):
        _,g,_=parse(doc())
        for raw in ['UNSUPPORTED RAW SOURCE','{}','[]','null']:
            bad=copy.deepcopy(g);st=next(n for n in bad['nodes'] if n['type']=='Statement')
            next(o for o in st['observations'] if o['field']=='modern_column_headings')['raw']=raw
            sg.materialize_processing(bad);self.assertEqual(sg.identity_admission(bad)['status'],'blocked')
            self.assertFalse(sg.validate(bad)['valid'])

    def test_conflicting_heading_cells_or_raw_header_block_format(self):
        _,g,_=parse(doc())
        for mutate in ['cell','raw','duplicate','header']:
            bad=copy.deepcopy(g);st=next(n for n in bad['nodes'] if n['type']=='Statement')
            o=next(o for o in st['observations'] if o['field']=='modern_column_headings');rows=json.loads(o['raw'])
            if mutate=='cell':rows[0]['cells']['printed_debit']='Кредит'
            elif mutate=='raw':rows[0]['raw']='OTHER SOURCE'
            elif mutate=='duplicate':rows.append(copy.deepcopy(rows[0]))
            else:next(o for o in st['observations'] if o['field']=='business_header_raw')['raw']='OTHER HEADER'
            o['raw']=json.dumps(rows);sg.materialize_processing(bad)
            self.assertEqual(sg.identity_admission(bad)['status'],'blocked',mutate)

    def test_optional_unknown_bank_code_field_does_not_hold_valid_row(self):
        for raw in ['12?','', 'bad-code']:
            d=two_rows();d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':raw,
                'account':IBAN,'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SYN',top=10)
            _,g,_=parse(d);p=op(g)['properties'];self.assertTrue(sg.validate(g)['valid'])
            self.assertEqual(p['processing']['status'],'ready');self.assertEqual(p['processing']['effective_counterparty']['origin'],'source_observed')
            account_id=next(e['to'] for e in g['edges'] if e['type']=='TO_ACCOUNT' and e['from']==op(g)['id'])
            bank_id=next(e['to'] for e in g['edges'] if e['type']=='SERVICED_BY' and e['from']==account_id)
            bank=next(n for n in g['nodes'] if n['type']=='Bank' and n['id']==bank_id);self.assertEqual(bank['properties']['identity_status'],'unknown')
            self.assertEqual(bank['properties']['identifier']['raw'],raw)

    def test_optional_bank_code_does_not_clear_required_account_hold(self):
        d=two_rows();d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'',
            'account':'BY??001','printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SYN',top=10)
        self.held_first_ready_second(d,'bank_identifier_unusable')

    def test_blank_required_account_retains_original_bankdate_anchor(self):
        d=two_rows();d['pages'][0]['lines'][4]=line({'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00',
            'account':'','printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО SYN',top=10)
        g,q=self.held_first_ready_second(d,'bank_identifier_unusable')
        self.assertEqual(op(g)['properties']['amount'],'2.00');self.assertEqual(q['rows'][0]['fields']['account'],'')

    def test_complete_export_signature_excluded_on_nonterminal_and_terminal_pages(self):
        d=doc();lines=d['pages'][0]['lines'];footer=lines[-2:]
        d['pages'][0].update(height=595.32,lines=lines[:-2]+signature())
        d['pages'].append({'page':2,'height':595.32,'lines':[line({'65.3':'Continued business'},top=22)]+footer+signature()})
        d['reviewed_continuations']=[{'from_page':1,'to_page':2,'operation_ordinal':1,'decision':'same_operation','evidence':'synthetic original QA'}]
        _,g,q=parse(d);self.assertEqual(op(g)['properties']['purpose_raw'],'Complete synthetic purpose\nContinued business')
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertEqual(sum(x['reason']=='electronic_signature' for x in q['excluded_structural_lines']),8)

    def test_signature_words_at_business_position_are_preserved(self):
        for x,top in [(65.3,560.448),(65.3,300)]:
            d=doc();d['pages'][0]['height']=595.32;d['pages'][0]['lines'][-2:-2]=signature(x,top)
            _,g,q=parse(d);self.assertFalse(any(x['reason']=='electronic_signature' for x in q['excluded_structural_lines']))
            self.assertIn('Подписано:',op(g)['properties']['purpose_raw'])

    def test_unknown_or_incomplete_margin_signature_is_retained_and_held(self):
        for change in ['incomplete','unknown_line','displaced']:
            d=doc();footer=d['pages'][0]['lines'][-2:];d['pages'][0]['lines']=d['pages'][0]['lines'][:-2]
            block=signature()
            if change=='incomplete':block=block[:-1]
            elif change=='unknown_line':block.append(line({'15':'UNKNOWN SERVICE DETAIL'},top=580.448))
            else:block=signature(20)
            d['pages'][0].update(height=595.32,lines=d['pages'][0]['lines']+block)
            d['pages'].append({'page':2,'lines':[line({'65.3':'Continued'},top=22)]+footer})
            d['reviewed_continuations']=[{'from_page':1,'to_page':2,'operation_ordinal':1,'decision':'same_operation','evidence':'synthetic original QA'}]
            _,g,q=parse(d);self.assertEqual(op(g)['properties']['processing']['status'],'needs_clarification')
            self.assertIn('Владелец ключа:',op(g)['properties']['purpose_raw'])
            self.assertFalse(any(x['reason']=='electronic_signature' for x in q['excluded_structural_lines']))


if __name__=='__main__':unittest.main()
