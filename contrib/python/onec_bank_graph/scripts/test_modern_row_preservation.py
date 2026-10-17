"""Required-field damage must preserve rows and survive caller status claims."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import modern_adapter as m
import statement_graph as sg
from test_modern_reviewed import doc, line, IBAN, op, parse
from test_modern_source_regressions import two_rows


def bank_line(**changes):
    fields={'document_number_raw':'001','operation_code_raw':'6','bank_code':'SYN0BY00',
            'account':IBAN,'printed_debit':'2.00','printed_credit':'0.00',
            'transaction_date':'05/01/2026'}
    fields.update(changes)
    return line(fields,side='ООО SYN',top=10)


class RequiredFieldRowPreservation(unittest.TestCase):
    def preserved(self,d,gap):
        _,g,q=parse(d);ops=[n for n in g['nodes'] if n['type']=='Operation']
        self.assertEqual(len(ops),2)
        self.assertEqual(sum((Decimal(n['properties']['printed_debit']) for n in ops),Decimal(0)),Decimal('4.00'))
        self.assertEqual([n['properties']['processing']['status'] for n in ops],['needs_clarification','ready'])
        self.assertIn(gap,ops[0]['properties']['processing']['gaps'])
        self.assertTrue(sg.validate(g)['valid'],sg.validate(g)['errors'])
        ops[0]['properties']['row_status']='complete'
        for observation in ops[0]['observations']:observation['status']='observed'
        sg.materialize_processing(g)
        self.assertIn(gap,ops[0]['properties']['processing']['gaps'])
        self.assertEqual(ops[0]['properties']['processing']['status'],'needs_clarification')
        self.assertEqual(ops[1]['properties']['processing']['status'],'ready')
        self.assertTrue(sg.validate(g)['valid'],sg.validate(g)['errors'])
        return g,q

    def test_missing_damaged_and_letter_opcode_preserve_both_rows_and_money(self):
        for raw in ['', '?', '*', '\ufffd', 'X', '12X', '1?', '-1', '\u0661']:
            with self.subTest(raw=raw):
                d=two_rows();d['pages'][0]['lines'][4]=bank_line(operation_code_raw=raw)
                g,q=self.preserved(d,'modern_operation_code_unresolved')
                self.assertEqual(q['rows'][0]['fields']['operation_code_raw'],raw)
                self.assertEqual(next(o['raw'] for o in op(g)['observations'] if o['field']=='operation_code_raw'),raw)

    def test_multiple_missing_required_fields_do_not_erase_operation(self):
        for changes in [dict(account='',transaction_date=''),
                        dict(account='',transaction_date='',operation_code_raw=''),
                        dict(account='',transaction_date='',operation_code_raw='',document_number_raw='',bank_code=''),
                        dict(document_number_raw='?',operation_code_raw='X',account='BY??001',transaction_date='??')]:
            with self.subTest(changes=changes):
                d=two_rows();d['pages'][0]['lines'][4]=bank_line(**changes)
                gap='modern_operation_code_unresolved' if 'operation_code_raw' in changes else 'modern_transaction_date_unresolved'
                g,q=self.preserved(d,gap)
                self.assertEqual(op(g)['properties']['amount'],'2.00')
                for key,value in changes.items():self.assertEqual(q['rows'][0]['fields'][key],value)

    def test_numeric_code_and_blank_optional_bank_code_remain_ready(self):
        for raw in ['0','6','001']:
            d=two_rows();d['pages'][0]['lines'][4]=bank_line(operation_code_raw=raw,bank_code='')
            _,g,_=parse(d)
            self.assertEqual([n['properties']['processing']['status'] for n in g['nodes'] if n['type']=='Operation'],['ready','ready'])
            self.assertTrue(sg.validate(g)['valid'])

    def test_unknown_document_number_does_not_add_a_readiness_prerequisite(self):
        d=two_rows();d['pages'][0]['lines'][4]=bank_line(document_number_raw='')
        _,g,_=parse(d)
        self.assertEqual(op(g)['properties']['document_number_raw'],'')
        self.assertEqual(op(g)['properties']['processing']['status'],'ready')
        self.assertTrue(sg.validate(g)['valid'])

    def test_forged_ready_processing_rejected_from_unchanged_damaged_code(self):
        d=two_rows();d['pages'][0]['lines'][4]=bank_line(operation_code_raw='?')
        _,g,_=parse(d);p=op(g)['properties'];p['row_status']='complete'
        p['processing'].update(status='ready',gaps=[],questions=[])
        p['processing']['effective_counterparty']['origin']='source_observed'
        self.assertFalse(sg.validate(g)['valid'])
        sg.materialize_processing(g)
        self.assertIn('modern_operation_code_unresolved',p['processing']['gaps'])

    def test_missing_or_duplicate_code_source_observation_stays_held(self):
        for duplicate in [False,True]:
            _,g,_=parse(doc());observations=op(g)['observations']
            source=next(o for o in observations if o['field']=='operation_code_raw')
            if duplicate:observations.append(copy.deepcopy(source))
            else:observations.remove(source)
            sg.materialize_processing(g)
            self.assertIn('modern_operation_code_unresolved',op(g)['properties']['processing']['gaps'])
            self.assertFalse(sg.validate(g)['valid'])

    def test_numeric_purpose_lookalike_is_retained_and_blocks_coverage(self):
        d=two_rows();lookalike=bank_line(document_number_raw='777',operation_code_raw='X')
        d['pages'][0]['lines'].insert(len(doc()['pages'][0]['lines'])-2,lookalike)
        s,e,r,q=m.parse_statement(d,1,'synthetic-lookalike',reviewed=True)
        self.assertEqual(len(s['operations']),2)
        self.assertIn(lookalike['raw_text'],s['operations'][0]['properties']['purpose_raw'])
        self.assertEqual(q['residual_business_lines'][0]['raw'],lookalike['raw_text'])
        g=sg.build({'dataset_id':'synthetic-lookalike','statements':[s],'entities':e,'relations':r},diagnostic=True)
        sg.materialize_processing(g)
        self.assertEqual(sg.identity_admission(g)['status'],'blocked')
        self.assertIn('unassigned_original_business_text',sg.identity_admission(g)['statements'][0]['coverage_gaps'])

    def test_unprovable_first_boundary_is_residual_not_admitted_subset(self):
        d=two_rows();d['pages'][0]['lines'][4]=bank_line(operation_code_raw='')
        queue=next(i for i,x in enumerate(d['pages'][0]['lines']) if 'Очередь' in x['raw_text'])
        d['pages'][0]['lines'][queue]=line({},side='Queue anatomy unknown',top=24)
        s,e,r,q=m.parse_statement(d,1,'synthetic-residual',reviewed=True)
        self.assertEqual(len(s['operations']),1);self.assertEqual(len(q['residual_business_lines']),1)
        self.assertIn(d['pages'][0]['lines'][4]['raw_text'],q['header_raw'])
        with self.assertRaises(ValueError):sg.build({'dataset_id':'synthetic-residual','statements':[s],'entities':e,'relations':r})

    def test_cli_preserves_full_partial_output_for_required_field_damage(self):
        for changes in [dict(operation_code_raw=''),dict(operation_code_raw='X'),dict(account='',transaction_date='')]:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);d=two_rows();d['pages'][0]['lines'][4]=bank_line(**changes)
                (root/'source.json').write_text(json.dumps(d))
                run=subprocess.run([sys.executable,'-B',str(Path(m.__file__).with_name('archive_adapter.py')),
                    'modern-assemble',str(root/'source.json'),'--dataset-id','synthetic-required-field',
                    '--output',str(root/'reviewed.json'),'--qa-output',str(root/'qa.json')],capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stderr)
                q=json.loads((root/'qa.json').read_text());self.assertEqual(len(q['rows']),2)
                self.assertEqual([x['document_view']['processing']['status'] for x in q['rows']],['needs_clarification','ready'])
                g=sg.build(json.loads((root/'reviewed.json').read_text()))
                self.assertEqual(sum(Decimal(n['properties']['printed_debit']) for n in g['nodes'] if n['type']=='Operation'),Decimal('4.00'))

    def test_cli_unproven_boundary_saves_private_qa_and_no_accepted_subset(self):
        d=two_rows();lookalike=bank_line(document_number_raw='777')
        d['pages'][0]['lines'].insert(len(doc()['pages'][0]['lines'])-2,lookalike)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'source.json').write_text(json.dumps(d))
            run=subprocess.run([sys.executable,'-B',str(Path(m.__file__).with_name('archive_adapter.py')),
                'modern-assemble',str(root/'source.json'),'--dataset-id','synthetic-coverage',
                '--output',str(root/'reviewed.json'),'--qa-output',str(root/'qa.json')],capture_output=True,text=True)
            self.assertEqual(run.returncode,2,run.stderr);self.assertFalse((root/'reviewed.json').exists())
            q=json.loads((root/'qa.json').read_text());self.assertEqual(q['processing_status'],'blocked')
            self.assertEqual(len(q['residual_business_lines']),1)
            self.assertEqual(q['validation']['processing_admission']['status'],'blocked')


if __name__=='__main__':unittest.main()
