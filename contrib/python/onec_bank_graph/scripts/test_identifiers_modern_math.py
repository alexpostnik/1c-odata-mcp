"""Synthetic source fixtures: identifier loss, physical rows and independent math."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import bank_identifiers as bi
import archive_adapter as a
import modern_adapter as m
import statement_graph as sg
from test_archive_adapter import document, row

BASE=Path(__file__).resolve().parents[1]

def synthetic_iban():
    bban='SYN03012'+'0'*16
    text=bban+'BY00'
    n=int(''.join(str(ord(c)-55) if c.isalpha() else c for c in text))
    return 'BY'+str(98-n%97).zfill(2)+bban

IBAN=synthetic_iban()

def physical_line(fields=None,side='',top=1):
    glyphs=[]
    for key,text in (fields or {}).items():
        x=m.CELLS[key][0] if key in m.CELLS else float(key)
        glyphs += [{'text':c,'x0':x+i*1.2,'x1':x+(i+1)*1.2,'top':top} for i,c in enumerate(text)]
    glyphs += [{'text':c,'x0':693+i*1.2,'x1':694+i*1.2,'top':top} for i,c in enumerate(side)]
    glyphs.sort(key=lambda x:x['x0'])
    raw=''.join(g['text'] for g in glyphs)
    return {'raw_text':raw,'text':raw,'top':top,'glyphs':glyphs}

def modern_document(body):
    header=[physical_line({'65.3':'ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД С 01/01/2026 ПО 31/01/2026'},top=1),
            physical_line({'65.3':'Cчет клиента 000123 BYN'},top=2),physical_line({'65.3':'Входящее сальдо 10.00'},top=3),
            physical_line({'document_number_raw':'Дата','operation_code_raw':'Опер','bank_code':'Код','account':'Счет','printed_debit':'Дебет','printed_credit':'Кредит','transaction_date':'Дата'},top=4)]
    footer=[physical_line({'65.3':'ОБОРОТЫ','printed_debit':'2.00','printed_credit':'0.00'},top=90),physical_line({'65.3':'Исходящее сальдо 8.00'},top=91)]
    return {'pages':[{'page':1,'lines':header+body+footer}]}

def modern_row(code='6',bank='SYN0BY00',account=IBAN,top=10):
    return physical_line({'document_number_raw':'001','operation_code_raw':code,'bank_code':bank,'account':account,'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='Unlabelled name',top=top)

class IdentifierTests(unittest.TestCase):
    def test_historical_strings_keep_zeros_and_no_checksum_claim(self):
        for kind in ('bank','account'):
            p=bi.recognize('00 012\n3',kind)
            self.assertEqual((p['raw'],p['normalized'],p['format'],p['value_check']),('00 012\n3','000123','historical_numeric','not_applicable'))
            self.assertEqual(p['existence'],'unknown')
        with self.assertRaises(ValueError):bi.recognize(123,'account')
    def test_bic8_bic11_wrapped_and_alphanumeric_prefix(self):
        for raw,expected in [('SYN0BY00','SYN0BY00'),('SYN0 BY00\nABC','SYN0BY00ABC')]:
            p=bi.recognize(raw,'bank')
            self.assertEqual((p['format_status'],p['format'],p['normalized'],p['value_check']),('recognized','bic',expected,'not_checked'))
        self.assertEqual(bi.recognize('SYN0BY00AB','bank')['format_status'],'unknown')
    def test_iban_shape_checksum_and_raw_are_independent(self):
        raw=IBAN[:4]+' '+IBAN[4:12]+'\n'+IBAN[12:]
        p=bi.recognize(raw,'account')
        self.assertEqual(p['raw'],raw);self.assertEqual(p['normalized'],IBAN)
        self.assertEqual((p['format'],p['format_status'],p['value_check']),('iban','recognized','passed'))
        bad=bi.recognize(IBAN[:-1]+'1','account')
        self.assertEqual((bad['format_status'],bad['value_check']),('recognized','failed'))
        for v in [IBAN[:-1],IBAN[:8]+'?'+IBAN[9:],IBAN.replace('S','С'),IBAN.lower()]:
            self.assertFalse(bi.usable(bi.recognize(v,'account')))
        self.assertEqual(bi.recognize('DE1234567890','account')['value_check'],'not_checked')
    def test_wide_normal_and_damaged_code_keep_all_rows_and_purpose(self):
        bars=a.WIDE_BARS
        def wide(number,op='  6  ',bank='SYN0BY00ABC',account=IBAN):
            return f'{number:>6}'+op+f'{56:>4}'+bank.ljust(12)+account.ljust(35)+f'{"2.00":>23}{"0.00":>22}'+'01/01/2010  1234'
        doc=document([])
        first=doc['pages'][0]['lines']
        first[-2]= {'text':'      |опер|исп|'+'-'*46+'+'+'-'*45+'  Дата','raw_text':'','overlaps':[]}
        bottom=[' ']*108
        for x in bars:bottom[x]='|'
        for x,label in [(16,'Код'),(28,'Счет'),(63,'Дебет'),(86,'Кредит')]:bottom[x:x+len(label)]=label
        first[-1]={'text':''.join(bottom),'raw_text':''.join(bottom),'overlaps':[]}
        body=[wide('1'),'Original purpose IBAN '+IBAN,wide('2',' ? ? ',bank='SYN0BY00AB',account=IBAN[:7]+'?'+IBAN[8:]),'Held purpose',wide('3'),'Independent purpose']
        doc['pages'][1]['lines']=[{'text':x,'raw_text':x,'top':1,'overlaps':[]} for x in body]+[
            {'text':'ОБОРОТЫ'.ljust(63)+f'{"6.00":>22}{"0.00":>22}','raw_text':'ОБОРОТЫ'.ljust(63)+f'{"6.00":>22}{"0.00":>22}','overlaps':[]},
            {'text':'Исходящее сальдо -6.00','raw_text':'Исходящее сальдо -6.00','overlaps':[]}]
        s,e,r,q=a.parse_statement(doc,1,'synthetic')
        self.assertEqual(len(s['operations']),3)
        self.assertEqual(s['operations'][0]['properties']['purpose_raw'],'Original purpose IBAN '+IBAN)
        self.assertEqual(s['operations'][1]['properties']['purpose_raw'],'Held purpose')
        self.assertEqual(q['rows'][0]['bank_identifiers']['account']['normalized'],IBAN)
        self.assertEqual(s['operations'][1]['properties']['row_status'],'partial')
        self.assertEqual(s['operations'][2]['properties']['amount'],'2.00')

class ModernTests(unittest.TestCase):
    def test_same_cell_bic_and_iban_wrap_without_role_invention(self):
        first=modern_row(bank='SYN0BY00AB',account=IBAN[:20])
        cont=physical_line({'bank_code':'C','account':IBAN[20:],'document_number_raw':'03/01/2026','operation_code_raw':'Несрочный'},side='Bank label',top=19)
        qline=physical_line({},side='Очередь 22',top=28)
        purpose=physical_line({'65.3':'Кол-во операций: 2; exact purpose'},top=40)
        s,e,r,q=m.parse_statement(modern_document([first,cont,qline,purpose]),1,'synthetic')
        self.assertEqual(q['operations'],1);self.assertEqual(q['rows'][0]['bank_identifiers']['bank_code']['raw'],'SYN0BY00AB\nC')
        self.assertEqual(q['rows'][0]['bank_identifiers']['bank_code']['normalized'],'SYN0BY00ABC')
        self.assertEqual(q['rows'][0]['bank_identifiers']['account']['normalized'],IBAN)
        p=s['operations'][0]['properties'];self.assertEqual((p['document_date'],p['transaction_date']),('2026-01-03','2026-01-05'))
        self.assertEqual(p['purpose_raw'],'Кол-во операций: 2; exact purpose')
        self.assertFalse(any(o['field']=='counterparty_field_type' for o in s['observations']))
        g=sg.build({'dataset_id':'synthetic','statements':[s],'entities':e,'relations':r},diagnostic=True)
        sg.materialize_processing(g);self.assertTrue(sg.validate(g)['valid']);self.assertEqual(sg.identity_admission(g)['status'],'blocked')
    def test_damaged_identifiers_and_operation_code_preserve_boundary(self):
        rows=[modern_row(),physical_line({},side='Очередь 22',top=20),physical_line({'65.3':'First purpose '+IBAN},top=30),
              modern_row(code='?',bank='BAD?',account='BY??UNKNOWN',top=40),physical_line({},side='Очередь 22',top=50),physical_line({'65.3':'Second purpose'},top=60)]
        s,e,r,q=m.parse_statement(modern_document(rows),1,'synthetic')
        self.assertEqual(q['operations'],2)
        self.assertEqual(s['operations'][0]['properties']['purpose_raw'],'First purpose '+IBAN)
        self.assertEqual(s['operations'][1]['properties']['purpose_raw'],'Second purpose')
        self.assertEqual(s['operations'][1]['properties']['amount'],'2.00')
    def test_purpose_number_references_are_not_rows(self):
        fields={'document_number_raw':'123','operation_code_raw':'6','bank_code':'SYN0BY00','account':IBAN}
        self.assertFalse(m.boundary({**fields,'printed_debit':'','printed_credit':'','transaction_date':''}))

class MathTests(unittest.TestCase):
    def setUp(self):
        self.graph=sg.build(json.loads((BASE/'assets/synthetic-reviewed.json').read_text()))
        self.controls=json.loads((BASE/'assets/synthetic-controls.json').read_text())
    def test_independent_controls_count_and_math_are_separate(self):
        c=self.controls['statements'][0];c['printed_operation_count']={'status':'observed','value':2,'raw':'Всего документов: 2'}
        result=sg.check_conversion(self.graph,self.controls)
        self.assertEqual(result['math_status'],'pass');self.assertTrue(result['accepted'])
        c['printed_operation_count']['value']=3
        result=sg.check_conversion(self.graph,self.controls)
        self.assertEqual(result['math_status'],'failed');self.assertFalse(result['accepted'])
        self.assertEqual(result['statements'][0]['differences']['printed_operation_count_rows'],'-1')
    def test_missing_controls_or_origin_never_ready(self):
        for key in ['source_review','printed_operation_count','opening','closing','printed_debit_total','printed_credit_total']:
            c=copy.deepcopy(self.controls)
            if key in ['source_review','printed_operation_count']:c['statements'][0].pop(key)
            else:c['statements'][0][key]=None
            result=sg.check_conversion(self.graph,c)
            self.assertEqual(result['math_status'],'unknown',key);self.assertFalse(result['accepted'])
    def test_all_rows_are_included_even_when_processing_held(self):
        g=copy.deepcopy(self.graph)
        for n in g['nodes']:
            if n['type']=='CurrencyContext':n['properties'].update(semantics_status='unknown',epoch=None,evidence_refs=[])
        result=sg.check_conversion(g,self.controls)
        self.assertEqual(result['math_status'],'pass');self.assertFalse(result['accepted'])
        self.assertEqual(result['statements'][0]['graph_operation_count'],2)
        self.assertEqual(result['statements'][0]['computed_expense'],'20.01')
        self.assertEqual(result['statements'][0]['computed_income'],'30.02')
    def test_mismatched_expense_or_income_controls_fail_even_if_equation_agrees(self):
        c=copy.deepcopy(self.controls)
        c['statements'][0].update(printed_debit_total='21.01',printed_credit_total='31.02')
        result=sg.check_conversion(self.graph,c)
        self.assertEqual(result['math_status'],'failed');self.assertFalse(result['accepted'])
    def test_signed_balances_and_each_statement_not_global_netting(self):
        d=json.loads((BASE/'assets/synthetic-reviewed.json').read_text())
        d['statements'][0]['properties'].update(opening='-50.00',closing='-39.99')
        g=sg.build(d);c=copy.deepcopy(self.controls)
        c['statements'][0].update(opening='-50.00',opening_raw='-50.00',closing='-39.99')
        self.assertEqual(sg.check_conversion(g,c)['math_status'],'pass')
        other=copy.deepcopy(d);other['dataset_id']='second-synthetic'
        combined=sg.combine([g,sg.build(other)])
        cc=copy.deepcopy(c['statements'][0]);cc['statement_id']='dataset:second-synthetic:statement:1'
        c['statements'].append(cc)
        c['statements'][0]['printed_debit_total']='21.01'
        c['statements'][1]['printed_debit_total']='19.01'
        result=sg.check_conversion(combined,c)
        self.assertEqual(result['math_status'],'failed')
        self.assertTrue(all(x['math_result']['status']=='failed' for x in result['statements']))
        self.assertEqual(sorted(x['differences']['expense_rows'] for x in result['statements']),['-1.00','1.00'])

    def test_build_cli_runs_required_postbuild_gate_when_controls_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);c=root/'controls.json';c.write_text(json.dumps(self.controls))
            cmd=[sys.executable,str(BASE/'scripts/statement_graph.py'),'build',str(BASE/'assets/synthetic-reviewed.json'),'--output',str(root/'graph.json'),'--controls',str(c),'--acceptance-output',str(root/'acceptance.json')]
            p=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertTrue(json.loads((root/'acceptance.json').read_text())['accepted'])
            c.write_text(json.dumps({'statements':[]}))
            cmd[cmd.index(str(root/'graph.json'))]=str(root/'held.json');cmd[-1]=str(root/'held-acceptance.json')
            p=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(p.returncode,1);self.assertFalse(json.loads((root/'held-acceptance.json').read_text())['accepted'])

if __name__=='__main__':unittest.main()
