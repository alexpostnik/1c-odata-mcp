"""Synthetic source full-name prefix before document details, never name exceptions."""
import copy
import unittest
import modern_adapter as m
import statement_graph as sg
from test_modern_reviewed import doc,parse,op

class SourceNamePrefixTests(unittest.TestCase):
    def test_three_printed_name_words_before_passport_preserve_full_block(self):
        for names,expected in [(['СИНТЕТИЧЕСКИЙ','АЛЕКСАНДР','ИВАНОВИЧ','MC 0000001 ВЫДАН','СИНТЕТИЧЕСКИМ ОРГАНОМ','010101'],'СИНТЕТИЧЕСКИЙ\nАЛЕКСАНДР\nИВАНОВИЧ'),(['СИНТЕТИЧЕСКАЯ АННА','ИВАНОВНА            MP','0000002 ВЫДАН','СИНТЕТИЧЕСКИМ ОРГАНОМ'],'СИНТЕТИЧЕСКАЯ АННА\nИВАНОВНА')]:
            _,g,_=parse(doc(name=names));p=op(g)['properties'];full='\n'.join(names)
            self.assertEqual(p['modern_fields']['name_raw'],full);self.assertEqual(p['counterparty']['name_raw'],expected)
            self.assertEqual(p['processing']['status'],'ready');self.assertTrue(sg.validate(g)['valid'])
            party=next(n for n in g['nodes'] if n['id']==p['counterparty']['party_id']);ob=next(x for x in party['observations'] if x['field']=='name_raw')
            self.assertEqual((ob['raw'],ob['normalized'],ob['status']),(full,expected,'observed'))
    def test_blank_unp_still_holds_only_unp_after_name_extraction(self):
        _,g,_=parse(doc(name=['СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ','MC 0000001 ВЫДАН','ОРГАН'],unp=''));p=op(g)['properties']
        self.assertEqual(p['counterparty']['name_raw'],'СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ');self.assertIsNone(p['counterparty']['unp_raw']);self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertIn('missing_or_unreadable_unp',p['processing']['gaps']);self.assertNotIn('missing_or_unreadable_full_name',p['processing']['gaps']);self.assertTrue(sg.validate(g)['valid'])
    def test_absent_partial_damaged_or_repeated_name_boundary_stays_unknown(self):
        for raw in ('MC 0000001 ВЫДАН\nОРГАН','СИНТЕТИЧЕСКИЙ АЛЕКСАНДР\nMC 0000001 ВЫДАН','СИНТЕТИЧЕСКИЙ ??? ИВАНОВИЧ\nMC 0000001 ВЫДАН','СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ\nMC 0000001 ВЫДАН\nMP 0000002 ВЫДАН','СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ\nMC 000000? ВЫДАН','СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ\nMC 000000 ВЫДАН','СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ\nmc 0000001 ВЫДАН','Паспорт SYN 000001\nвыдан SYN'):
            self.assertIsNone(m.identity_name_from_block(raw),raw)
    def test_cannot_substitute_prefix_or_normalize_whole_document_as_name(self):
        _,g,_=parse(doc(name=['СИНТЕТИЧЕСКИЙ АЛЕКСАНДР ИВАНОВИЧ','MC 0000001 ВЫДАН','ОРГАН']));p=op(g)['properties'];party=next(n for n in g['nodes'] if n['id']==p['counterparty']['party_id'])
        for fake in ('ДРУГОЙ АЛЕКСАНДР ИВАНОВИЧ',p['modern_fields']['name_raw']):
            bad=copy.deepcopy(g);bp=op(bad)['properties'];pn=next(n for n in bad['nodes'] if n['id']==party['id']);bp['counterparty']['name_raw']=fake;pn['properties']['name_raw']=fake
            next(x for x in pn['observations'] if x['field']=='name_raw')['normalized']=fake;sg.materialize_processing(bad)
            self.assertFalse(sg.validate(bad)['valid'])
