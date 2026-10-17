"""Owner-allowed source blanks, with retained original evidence against false blanks."""
import copy
import unittest
import archive_adapter as a
import statement_graph as sg
from payment_text import prepare_payment_text, writable_payment_text
from test_admission_regressions import identity_body, parsed
from test_modern_reviewed import doc, parse, op

class SourceEmptyPurposeTests(unittest.TestCase):
    def test_labelled_empty_and_independent_row_both_retained_ready(self):
        body=identity_body();body[-1]=''
        reviewed,_=parsed(body+identity_body(number='2'))
        graph=sg.build(reviewed);ops=[n for n in graph['nodes'] if n['type']=='Operation']
        self.assertEqual(len(ops),2)
        p=ops[0]['properties'];self.assertEqual(p['purpose_raw'],p['payment1c_comment_raw']);self.assertEqual(p['purpose_raw'],'')
        self.assertEqual((p['processing']['status'],p['payment1c_text']['value']),('ready',''))
        self.assertEqual(writable_payment_text(p),'');self.assertEqual(p['amount'],'3');self.assertTrue(sg.validate(graph)['valid'])
        self.assertEqual(ops[1]['properties']['processing']['status'],'ready')
    def test_modern_closed_empty_retained_ready(self):
        _,g,_=parse(doc(purpose=''));p=op(g)['properties']
        self.assertEqual(p['purpose_raw'],'');self.assertEqual(p['payment1c_comment_raw'],'')
        self.assertEqual((p['purpose_status'],p['processing']['status'],p['payment1c_text']['value']),('observed','ready',''))
        self.assertEqual(writable_payment_text(p),'');self.assertTrue(sg.validate(g)['valid'])
    def test_unknown_empty_boundary_does_not_become_ready(self):
        _,g,_=parse(doc(purpose=''));p=op(g)['properties'];p['purpose_status']='unknown';p['payment1c_text']=prepare_payment_text('', 'unknown');sg.materialize_processing(g)
        self.assertEqual(p['processing']['status'],'needs_clarification')
        with self.assertRaises(ValueError):writable_payment_text(p)
    def test_forged_empty_purpose_and_comment_rejected_by_retained_source(self):
        for modern in (False,True):
            if modern:_,g,_=parse(doc(purpose='Printed business purpose'))
            else:
                body=identity_body();body[-1]='Printed business purpose';reviewed,_=parsed(body);g=sg.build(reviewed)
            p=op(g)['properties'];p['purpose_raw']=p['payment1c_comment_raw']='';p['payment1c_text']=prepare_payment_text('');sg.materialize_processing(g)
            self.assertFalse(sg.validate(g)['valid'])
    def test_nonempty_source_never_substituted_by_blank_derived_value(self):
        _,g,_=parse(doc(purpose='Keep full printed text'));p=op(g)['properties'];p['payment1c_text']=prepare_payment_text('')
        self.assertFalse(sg.validate(g)['valid'])
        with self.assertRaises(ValueError):writable_payment_text(p)
