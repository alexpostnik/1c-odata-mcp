"""Five source-mechanism regressions, using only synthetic bank evidence."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import archive_adapter as a
import bank_identifiers as bi
import modern_adapter as m
import statement_graph as sg
from test_archive_adapter import line
from test_integrated_wide_identifiers import VALID_IBAN
from test_wide_archive import wide_doc
from test_identifiers_modern_math import physical_line
from test_modern_reviewed import doc, parse, op
import test_modern_reviewed as modern_fixtures


class ContinuationOverlapTests(unittest.TestCase):
    def wrapped(self, conflict=None, glyphs=False, independent=False):
        d=wide_doc();raw=d['pages'][0]['lines'][10]['text']
        d['pages'][0]['lines'][10]=line(raw[:15]+'SYNBBY2X'.rjust(12)+VALID_IBAN[:20].rjust(35)+raw[62:])
        tail=line(' '*15+'ABC'.rjust(12)+VALID_IBAN[20:].rjust(35))
        if conflict:
            cell=55 if conflict=='account' else 25
            tail['overlaps']=[cell]
            if glyphs:
                tail['glyphs']=[{'text':c,'cell':i} for i,c in enumerate(tail['text'])]
                tail['glyphs'].append({'text':'9','cell':cell})
                tail['glyphs'].sort(key=lambda g:g['cell'])
                tail['raw_text']=a.field_slice(tail,0,None)
        d['pages'][0]['lines'].insert(11,tail)
        if independent:
            other=wide_doc()['pages'][0]['lines'][10:-2]
            other[0]=line('82'.rjust(6)+other[0]['text'][6:])
            d['pages'][0]['lines'][-2:-2]=other
        s,e,r,q=a.parse_statement(d,1,'synthetic-279')
        g=sg.build({'dataset_id':'synthetic-279','statements':[s],'entities':e,'relations':r})
        return g,q

    def test_account_overlap_never_passes_checksum_or_processing(self):
        for glyphs in (False,True):
            g,q=self.wrapped('account',glyphs)
            account=next(n for n in g['nodes'] if n['id'].endswith(':correspondent-account'))
            ident=account['properties']['identifier']
            self.assertIsNone(ident['normalized']);self.assertEqual(ident['format_status'],'unknown')
            self.assertFalse(bi.usable(ident));self.assertEqual(ident['value_check'],'not_checked')
            self.assertIn('\n',ident['raw']);self.assertIsNone(account['properties']['number_raw'])
            p=op(g)['properties'];self.assertEqual((p['amount'],p['direction']),('12.34','outflow'))
            self.assertEqual(p['processing']['status'],'needs_clarification')
            self.assertTrue(sg.validate(g)['valid'],sg.validate(g)['errors'])
            self.assertEqual(q['rows'][0]['fields']['account']['status'],'unknown')
            retained=q['rows'][0]['identifier_continuations'][0]
            self.assertEqual(retained['overlaps'],[55])
            if glyphs:self.assertEqual({x['text'] for x in retained['glyphs'] if x['cell']==55},{'0','9'})
            forced=copy.deepcopy(g);op(forced)['properties']['row_status']='complete';sg.materialize_processing(forced)
            self.assertIn('bank_identifier_unusable',op(forced)['properties']['processing']['gaps'])
            self.assertEqual(op(forced)['properties']['processing']['status'],'needs_clarification')

    def test_overlap_preserves_independent_row_and_all_movements(self):
        g,q=self.wrapped('account',True,independent=True)
        rows=[n for n in g['nodes'] if n['type']=='Operation']
        self.assertEqual(len(rows),2);self.assertEqual(len(q['rows']),2)
        self.assertEqual([n['properties']['processing']['status'] for n in rows],['needs_clarification','ready'])
        self.assertEqual([n['properties']['amount'] for n in rows],['12.34','12.34'])
        self.assertEqual(rows[1]['properties']['purpose_raw'],'Full purpose 999999.99 is not movement.')
        self.assertTrue(sg.validate(g)['valid'])

    def test_optional_bank_overlap_stays_field_local(self):
        g,q=self.wrapped('bank',True)
        bank=next(n for n in g['nodes'] if n['id'].endswith(':correspondent-bank'))
        self.assertEqual(bank['properties']['identifier']['format_status'],'unknown')
        self.assertIsNone(bank['properties']['code_raw'])
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertEqual(op(g)['properties']['amount'],'12.34');self.assertTrue(sg.validate(g)['valid'])
        self.assertEqual(q['rows'][0]['fields']['bank_code']['status'],'unknown')

    def test_clean_wrapped_account_and_bic_still_ready(self):
        g,q=self.wrapped();self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertEqual(q['rows'][0]['bank_identifiers']['account']['normalized'],VALID_IBAN)
        self.assertEqual(q['rows'][0]['bank_identifiers']['bank']['normalized'],'SYNBBY2XABC')
        self.assertTrue(sg.validate(g)['valid'])


class SignedEquationTests(unittest.TestCase):
    def fixture(self,unknown=True):
        base=Path(sg.__file__).resolve().parents[1]
        g=sg.build(json.loads((base/'assets/synthetic-reviewed.json').read_text()))
        c=json.loads((base/'assets/synthetic-controls.json').read_text())
        st=next(n for n in g['nodes'] if n['type']=='Statement')
        st['properties']['opening_sign_rule']='unknown' if unknown else 'signed'
        st['properties']['closing']='999.00';c['statements'][0]['closing']='999.00'
        return g,c

    def test_unknown_sign_does_not_fail_hypothetical_equations(self):
        g,c=self.fixture();res=sg.check_conversion(g,c);r=res['statements'][0]
        self.assertEqual((res['math_status'],res['status']),('unknown','needs_review'))
        self.assertFalse(res['accepted']);self.assertIn('balance_equation_unknown',r['issues'])
        self.assertTrue(all(sg.decimal(x)==0 for x in r['differences'].values()))
        for key in ('graph_balance_equation','pdf_balance_equation'):
            self.assertIsNone(r['checks'][key]['graph']);self.assertIsNone(r['checks'][key]['difference'])
            self.assertEqual(r['checks'][key]['status'],'unknown')

    def test_unknown_column_meaning_does_not_run_equations(self):
        g,c=self.fixture(False);next(n for n in g['nodes'] if n['type']=='Statement')['properties']['debit_is']='unknown'
        for n in g['nodes']:
            if n['type']=='Operation':n['properties']['bank_debit_is']='outflow'
        res=sg.check_conversion(g,c)
        self.assertEqual(res['math_status'],'unknown')
        self.assertNotIn('graph_balance_equation',res['statements'][0]['differences'])
        self.assertNotIn('pdf_balance_equation',res['statements'][0]['differences'])

    def test_known_signed_nonzero_equations_still_fail(self):
        g,c=self.fixture(False);res=sg.check_conversion(g,c)
        self.assertEqual(res['math_status'],'failed')
        self.assertEqual(res['statements'][0]['differences']['graph_balance_equation'],'-988.99')
        self.assertEqual(res['statements'][0]['differences']['pdf_balance_equation'],'-988.99')

    def test_direct_copy_and_turnover_errors_fail_despite_unknown_sign(self):
        for key in ('closing','printed_debit_total'):
            g,c=self.fixture();c['statements'][0][key]='998.00'
            res=sg.check_conversion(g,c)
            self.assertEqual(res['math_status'],'failed')
            self.assertNotEqual(res['statements'][0]['differences'][key+'_copy'],'0.00')


class ModernEvidenceTests(unittest.TestCase):
    def test_conflicts_hold_row_and_cannot_be_forced_complete(self):
        for category,assertion in [('urgency','Срочный'),('acceptance','С акцептом')]:
            d=doc(acceptance=True)
            queue=next(i for i,x in enumerate(d['pages'][0]['lines']) if 'Очередь 22' in x['raw_text'])
            d['pages'][0]['lines'].insert(queue,physical_line({'operation_code_raw':assertion},top=23.5))
            _,g,q=parse(d);p=op(g)['properties'];gap='modern_conflicting_'+category
            self.assertIn(assertion,p['modern_fields'][category+'_raw'])
            self.assertIn(gap,p['processing']['gaps']);self.assertEqual(p['processing']['status'],'needs_clarification')
            self.assertEqual(p['amount'],'2.00');self.assertTrue(sg.validate(g)['valid'],sg.validate(g)['errors'])
            self.assertIn(gap,q['rows'][0]['gaps'])
            p['row_status']='complete';sg.materialize_processing(g)
            self.assertIn(gap,p['processing']['gaps']);self.assertEqual(p['processing']['status'],'needs_clarification')

    def test_conflict_holds_only_its_row(self):
        d=doc();second=doc();second['pages'][0]['lines'][4]=physical_line({'document_number_raw':'002','operation_code_raw':'6','bank_code':'SYN0BY00','account':modern_fixtures.IBAN,'printed_debit':'2.00','printed_credit':'0.00','transaction_date':'05/01/2026'},side='ООО СИНТЕТИКА',top=30)
        queue=next(i for i,x in enumerate(d['pages'][0]['lines']) if 'Очередь 22' in x['raw_text'])
        d['pages'][0]['lines'].insert(queue,physical_line({'operation_code_raw':'Срочный'},top=23.5))
        d['pages'][0]['lines'][-2:-2]=second['pages'][0]['lines'][4:-2]
        _,g,_=parse(d);rows=[n for n in g['nodes'] if n['type']=='Operation']
        self.assertEqual([n['properties']['processing']['status'] for n in rows],['needs_clarification','ready'])
        self.assertEqual([n['properties']['amount'] for n in rows],['2.00','2.00']);self.assertTrue(sg.validate(g)['valid'])

    def test_equal_case_variants_are_not_false_conflicts(self):
        d=doc(acceptance=True);queue=next(i for i,x in enumerate(d['pages'][0]['lines']) if 'Очередь 22' in x['raw_text'])
        d['pages'][0]['lines'].insert(queue,physical_line({'operation_code_raw':'НЕСРОЧНЫЙ'},top=23.5))
        _,g,_=parse(d);self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertIn('НЕСРОЧНЫЙ',op(g)['properties']['modern_fields']['urgency_raw'])

    def test_valid_single_categories_and_business_words_stay_ready(self):
        _,g,_=parse(doc(acceptance=True,purpose='Срочный business payment С акцептом'))
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertEqual(op(g)['properties']['purpose_raw'],'Срочный business payment С акцептом')

    def two_pages(self,first):
        d=doc();lines=d['pages'][0]['lines'];d['pages'][0]['lines']=lines[:-2]
        d['pages'].append({'page':2,'lines':[first,physical_line({'65.3':'Later complete business'},top=22)]+lines[-2:]})
        d['reviewed_continuations']=[{'from_page':1,'to_page':2,'operation_ordinal':1,'decision':'same_operation','evidence':'synthetic original join review'}]
        return d

    def test_unknown_top_numeric_line_retained_and_held(self):
        _,g,q=parse(self.two_pages(physical_line({'65.3':'123456789'},top=8)))
        p=op(g)['properties'];self.assertEqual(p['purpose_raw'],'Complete synthetic purpose\n123456789\nLater complete business')
        self.assertEqual(p['purpose_status'],'unknown');self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertTrue(sg.validate(g)['valid']);self.assertFalse(any(x['raw']=='123456789' for x in q['excluded_structural_lines']))
        self.assertEqual(p['amount'],'2.00')

    def test_all_unknown_top_business_spelling_is_retained_and_held(self):
        for text in ('123 456 789','123456789?','Unproven business reference'):
            for x in ('15','65.3'):
                with self.subTest(text=text,x=x):
                    _,g,q=parse(self.two_pages(physical_line({x:text},top=8)))
                    p=op(g)['properties']
                    self.assertEqual(p['purpose_raw'],'Complete synthetic purpose\n'+text+'\nLater complete business')
                    self.assertEqual(p['purpose_status'],'unknown')
                    self.assertEqual(p['processing']['status'],'needs_clarification')
                    self.assertIn('unreviewed margin service anatomy',q['rows'][0]['gaps'])
                    self.assertTrue(sg.validate(g)['valid']);self.assertEqual(p['amount'],'2.00')
                    self.assertFalse(any(x['raw']==text for x in q['excluded_structural_lines']))

    def test_left_prefix_is_not_lost_when_line_also_has_body_glyphs(self):
        first=physical_line({'15':'Prefix ','65.3':'continuation'},top=8)
        _,g,_=parse(self.two_pages(first));p=op(g)['properties']
        self.assertEqual(p['purpose_raw'],'Complete synthetic purpose\nPrefix continuation\nLater complete business')
        self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertTrue(sg.validate(g)['valid'])

    def test_unproven_left_business_geometry_is_held_outside_top_margin_too(self):
        _,g,_=parse(self.two_pages(physical_line({'15':'Left business reference'},top=22)))
        p=op(g)['properties'];self.assertIn('Left business reference',p['purpose_raw'])
        self.assertEqual(p['purpose_status'],'unknown');self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertEqual(p['amount'],'2.00');self.assertTrue(sg.validate(g)['valid'])

    def test_supported_business_and_exact_web_decoration(self):
        for text,top,kept,status in [('123456789',22,True,'ready'),('123 456 789',22,True,'ready'),
                ('123456789?',22,True,'ready'),('Supported business continuation',22,True,'ready'),
                ('Клиент-банк (WEB) 2',4,False,'ready'),('Invoice Клиент-банк (WEB) 2',4,True,'needs_clarification')]:
            _,g,q=parse(self.two_pages(physical_line({'65.3':text},top=top)))
            self.assertEqual(text in op(g)['properties']['purpose_raw'],kept)
            self.assertEqual(op(g)['properties']['processing']['status'],status)
            self.assertTrue(sg.validate(g)['valid'])


class PairedOutputTests(unittest.TestCase):
    def test_cli_both_modes_reject_collisions_without_dangling_file(self):
        for action,data in [('modern-assemble',doc()),('balance-only-archive',modern_fixtures.EmptyArchiveTests().empty())]:
            for collision in ('output','qa','same','canonical_alias','symlink_parent','dangling_symlink'):
                with self.subTest(action=action,collision=collision),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);source=root/'input.json';source.write_text(json.dumps(data))
                    output=root/'output.json';qa=root/'qa.json';existing=None
                    if collision in ('output','qa'):
                        existing=output if collision=='output' else qa;existing.write_text('preserved original')
                    elif collision=='same':qa=output
                    elif collision=='canonical_alias':qa=root/'missing/../output.json'
                    elif collision=='symlink_parent':
                        (root/'alias').symlink_to(root,target_is_directory=True);qa=root/'alias/output.json'
                    else:output.symlink_to(root/'absent-target')
                    result=subprocess.run([sys.executable,a.__file__,action,str(source),'--dataset-id','synthetic-pair','--output',str(output),'--qa-output',str(qa)],capture_output=True,text=True)
                    self.assertNotEqual(result.returncode,0)
                    if existing:self.assertEqual(existing.read_text(),'preserved original')
                    self.assertFalse((root/'qa.json').exists() if collision!='qa' else (root/'output.json').exists())
                    if collision not in ('output','dangling_symlink'):self.assertFalse(output.exists())
                    if collision=='dangling_symlink':
                        self.assertTrue(output.is_symlink());self.assertFalse((root/'absent-target').exists())

    def test_build_controls_collision_preserves_existing_output(self):
        base=Path(sg.__file__).resolve().parents[1]
        for collision in ('graph_exists','same'):
            with self.subTest(collision=collision),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);graph=root/'graph.json';acceptance=root/'acceptance.json'
                if collision=='graph_exists':graph.write_text('preserved graph')
                else:acceptance=graph
                result=subprocess.run([sys.executable,sg.__file__,'build',str(base/'assets/synthetic-reviewed.json'),'--controls',str(base/'assets/synthetic-controls.json'),'--output',str(graph),'--acceptance-output',str(acceptance)],capture_output=True,text=True)
                self.assertEqual(result.returncode,2)
                if collision=='graph_exists':self.assertEqual(graph.read_text(),'preserved graph');self.assertFalse(acceptance.exists())
                else:self.assertFalse(graph.exists())

    def test_handled_second_write_error_cleans_both_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp)/'first.json',Path(tmp)/'second.json'
            dump=sg.json.dump;count=0
            def failing_dump(data,stream,**kwargs):
                nonlocal count
                count+=1
                if count==2:
                    stream.write('partial');raise OSError('synthetic write failure')
                return dump(data,stream,**kwargs)
            with patch.object(sg.json,'dump',side_effect=failing_dump),self.assertRaises(OSError):
                sg.write_new_pair(first,{'first':1},second,{'second':2})
            self.assertFalse(first.exists());self.assertFalse(second.exists())

    def test_handled_second_reservation_race_preserves_other_creator_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp)/'first.json',Path(tmp)/'second.json';real_open=Path.open
            def raced_open(path,*args,**kwargs):
                if path==second.resolve() and args and args[0]=='x':
                    with real_open(second,'w') as stream:stream.write('other creator')
                return real_open(path,*args,**kwargs)
            with patch.object(Path,'open',raced_open),self.assertRaises(FileExistsError):
                sg.write_new_pair(first,{},second,{})
            self.assertFalse(first.exists());self.assertEqual(second.read_text(),'other creator')

    def test_successful_pair_is_exclusive_and_has_correct_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp)/'first.json',Path(tmp)/'second.json'
            sg.write_new_pair(first,{'first':1},second,{'second':2})
            self.assertEqual(json.loads(first.read_text()),{'first':1});self.assertEqual(json.loads(second.read_text()),{'second':2})
            with self.assertRaises(FileExistsError):sg.write_new_pair(first,{},second,{})
            self.assertEqual(json.loads(first.read_text()),{'first':1})


if __name__=='__main__':unittest.main()
