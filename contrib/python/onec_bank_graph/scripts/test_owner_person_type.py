"""Owner type permits a source blank, never guessing UNP or borrowing identity."""
import copy
import unittest
import statement_graph as sg
from test_modern_reviewed import doc, parse, op


def answer(g, person_type='physical_person'):
    n=op(g)
    return {'operation_id':n['id'],'role':n['properties']['counterparty']['role'],
            'person_type':person_type,'origin':'owner_confirmed',
            'original_detail_raw':next(x['raw'] for x in n['observations'] if x['field']=='identity_detail_raw'),
            'answered_by':'synthetic-owner','answered_at':'2026-10-13T09:00:00Z',
            'answer_ref':'synthetic-owner-answer','confirmation_ref':'synthetic-owner-confirmation'}


class OwnerPersonTypeTests(unittest.TestCase):
    def test_blank_is_admitted_only_with_explicit_type_and_raw_unchanged(self):
        _,g,_=parse(doc(name=['СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ'],unp=''))
        before=copy.deepcopy(g);resolved=sg.classify_counterparty_types(g,{op(g)['id']:answer(g)})
        p=op(resolved)['properties'];self.assertEqual(p['processing']['status'],'ready')
        self.assertEqual(p['processing']['effective_counterparty']['origin'],'owner_confirmed')
        self.assertIsNone(p['processing']['effective_counterparty']['unp_raw'])
        self.assertEqual(p['counterparty'],op(before)['properties']['counterparty'])
        for old,new in zip(before['nodes'],resolved['nodes']):
            self.assertEqual(old['observations'],new['observations'])
            if old['type']=='Party':self.assertEqual(old,new)
        self.assertEqual(before['edges'],resolved['edges']);self.assertTrue(sg.validate(resolved)['valid'])
        self.assertEqual(op(g)['properties']['processing']['status'],'needs_clarification')

    def test_legal_entity_zero_damage_duplicate_and_unknown_name_stay_held(self):
        for value in ('0','000000000','???','УНП:'):
            _,g,_=parse(doc(unp=value));r=sg.classify_counterparty_types(g,{op(g)['id']:answer(g)})
            self.assertEqual(op(r)['properties']['processing']['status'],'needs_clarification',value)
        _,g,_=parse(doc(unp=''));r=sg.classify_counterparty_types(g,{op(g)['id']:answer(g,'legal_entity')})
        self.assertEqual(op(r)['properties']['processing']['status'],'needs_clarification')
        _,g,_=parse(doc(name=['SYNTHETIC [unknown]'],unp=''));r=sg.classify_counterparty_types(g,{op(g)['id']:answer(g)})
        self.assertIn('missing_or_unreadable_full_name',op(r)['properties']['processing']['gaps'])

    def test_wrong_row_role_raw_timestamp_missing_and_self_claimed_origin_rejected(self):
        _,g,_=parse(doc(unp=''));base=answer(g)
        for key,value in [('operation_id','another-row'),('role','payer' if base['role']=='payee' else 'payee'),('original_detail_raw','different raw'),
                          ('answered_at','2026-10-13T09:00:00'),('answer_ref',''),('confirmation_ref',''),
                          ('origin','source_observed')]:
            bad=dict(base);bad[key]=value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):sg.classify_counterparty_types(g,{op(g)['id']:bad})

    def test_other_identity_money_boundary_and_text_gates_remain(self):
        _,g,_=parse(doc(unp=''));op(g)['properties']['duplicate_status']='candidate';sg.materialize_processing(g)
        r=sg.classify_counterparty_types(g,{op(g)['id']:answer(g)})
        self.assertIn('duplicate_review_unresolved',op(r)['properties']['processing']['gaps'])
        self.assertEqual(op(r)['properties']['processing']['status'],'needs_clarification')
        bad=copy.deepcopy(r);op(bad)['properties']['processing']['status']='ready'
        self.assertFalse(sg.validate(bad)['valid'])
