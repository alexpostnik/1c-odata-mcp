"""Invented physical-person tax identifiers; no real identities or registry claim."""
import copy
import unittest
import archive_adapter as a
import statement_graph as sg
from test_admission_regressions import identity_body, parsed
from test_modern_reviewed import doc, parse, op

class PhysicalUnpTests(unittest.TestCase):
    def test_full_ascii_grammar_and_zero_policy(self):
        for value in ('AB1234567','ZZ0000000','AA0000001','001234567','123456789'):
            self.assertTrue(a.readable_unp(value),value)
        for value in (None,'','000000000','ab1234567','Ab1234567','АB1234567','AВ1234567','AB１２３４５６７','AB123456','AB12345678','AB1234567x','AB123456?',' AB1234567','AB1234567\n'):
            self.assertFalse(a.readable_unp(value),value)
        self.assertTrue(a.observed_unp_matches('A B 001 2345','AB0012345'))
        for raw in ('АB0012345','ab0012345','AB0012345\t','AB0012345\x00','AB0012345?','AB0012345\n'):
            self.assertFalse(a.observed_unp_matches(raw,'AB0012345'),raw)
    def test_legacy_same_role_and_raw_spaces(self):
        for direction in ('inflow','outflow'):
            kwargs={'payer':'AB 0012345'} if direction=='inflow' else {'payee':'AB 0012345'}
            reviewed,_=parsed(identity_body(direction,**kwargs));g=sg.build(reviewed)
            p=op(g)['properties'];self.assertEqual(p['counterparty']['unp_raw'],'AB0012345')
            self.assertEqual(p['processing']['status'],'ready');self.assertTrue(sg.validate(g)['valid'])
            party=next(n for n in g['nodes'] if n['id']==p['counterparty']['party_id'])
            self.assertEqual(party['properties']['unp_raw'],'AB0012345')
            self.assertEqual(next(x for x in party['observations'] if x['field']=='unp_raw')['status'],'observed')
    def test_duplicate_conflict_and_wrong_role_stay_held(self):
        for direction in ('inflow','outflow'):
            for defect in ('equal_duplicate','conflict','opposite_only','behalf_only'):
                b=identity_body(direction,payer='AB1234567' if direction=='inflow' else '',payee='AB1234567' if direction=='outflow' else '')
                if defect in ('equal_duplicate','conflict'):b.insert(3,b[2] if defect=='equal_duplicate' else b[2].replace('AB1234567','CD1234567'))
                elif defect=='opposite_only':b[2]='УНП плательщика: '+('' if direction=='inflow' else 'AB1234567')+' УНП получателя: '+('AB1234567' if direction=='inflow' else '')+' УНП за кого: '
                else:b[2]='УНП плательщика:  УНП получателя:  УНП за кого: AB1234567'
                r,_=parsed(b);g=sg.build(r,diagnostic=True)
                self.assertEqual(op(g)['properties']['processing']['status'],'needs_clarification',(direction,defect))
                self.assertTrue(sg.validate(g)['valid'])
    def test_modern_keeps_raw_and_rejects_confusables(self):
        for value in ('AB1234567','AB 0012345'):
            _,g,_=parse(doc(unp=value));p=op(g)['properties']
            self.assertEqual(p['modern_fields']['unp_raw'],value);self.assertEqual(p['processing']['status'],'ready');self.assertTrue(sg.validate(g)['valid'])
            p['counterparty']['unp_raw']='CD1234567';self.assertFalse(sg.validate(g)['valid'])
        for value in ('АB1234567','ab1234567','AB123456?','AB\t1234567',''):
            _,g,_=parse(doc(unp=value));self.assertEqual(op(g)['properties']['processing']['status'],'needs_clarification')
