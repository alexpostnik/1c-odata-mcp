"""Observed export decoration anatomy, with synthetic nonfinancial content."""
import unittest
import modern_adapter as m
import statement_graph as sg
import test_graph_mechanisms as mechanisms
from test_identifiers_modern_math import physical_line
from test_modern_reviewed import parse,op


def export_text(date='01.01.2026'):
    return date+'г. ОАО АСБ "Беларусбанк". ПК "Клиент-банк (WEB)"'


def export_line(text=None,left=15,top=5.128,step=3.6):
    raw=export_text() if text is None else text
    return {'raw_text':raw,'text':raw,'top':top,
            'glyphs':[{'text':char,'x0':left+step*i,'x1':left+step*(i+1),'top':top} for i,char in enumerate(raw)]}


def document(second=None):
    d=mechanisms.ModernEvidenceTests().two_pages(export_line() if second is None else second)
    d['pages'][0]['lines'].insert(0,export_line())
    for page in d['pages']:page.update(width=842.04,height=595.32)
    return d


class ExportHeaderTests(unittest.TestCase):
    def assert_retained(self,d,raw,status='needs_clarification'):
        _,g,q=parse(d);p=op(g)['properties']
        self.assertIn(raw,p['purpose_raw']);self.assertEqual(p['processing']['status'],status)
        self.assertEqual(p['amount'],'2.00');self.assertTrue(sg.validate(g)['valid'])
        self.assertFalse(any(x['page']==2 and x['raw']==raw for x in q['excluded_structural_lines']))
        return q

    def test_repeated_actual_shaped_export_header_is_not_business_purpose(self):
        for date in ('01.01.2026','13.01.2026'):
            d=document(export_line(export_text(date)));d['pages'][0]['lines'][0]=export_line(export_text(date))
            self.assertEqual(m.reviewed_export_header_pages(d),{1,2})
            _,g,q=parse(d);p=op(g)['properties']
            self.assertEqual(p['purpose_raw'],'Complete synthetic purpose\nLater complete business')
            self.assertEqual(p['purpose_status'],'observed');self.assertEqual(p['processing']['status'],'ready')
            self.assertEqual(len([x for x in q['excluded_structural_lines'] if x['raw']==export_text(date)]),2)
            self.assertTrue(sg.validate(g)['valid'])

    def test_header_exclusion_does_not_approve_an_unreviewed_page_join(self):
        d=document();d['reviewed_continuations']=[]
        _,g,q=parse(d);p=op(g)['properties']
        self.assertNotIn(export_text(),p['purpose_raw']);self.assertIn('Later complete business',p['purpose_raw'])
        self.assertEqual(p['purpose_status'],'unknown');self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertEqual(p['amount'],'2.00');self.assertTrue(sg.validate(g)['valid'])
        self.assertIn('continuation boundary unreviewed',q['rows'][0]['gaps'])

    def test_headerlike_business_prefix_or_tail_is_retained_and_held(self):
        for raw in ('Payment for '+export_text(),export_text()+' business invoice',export_text().replace('ПК','PURPOSE')):
            self.assert_retained(document(export_line(raw)),raw)

    def test_header_string_in_supported_business_position_is_retained_ready(self):
        raw=export_text();self.assert_retained(document(physical_line({'65.3':raw},top=22)),raw,'ready')

    def test_displaced_unknown_or_damaged_physical_header_is_retained(self):
        cases=[export_line(left=16),export_line(top=8),export_line(step=3.8),export_line(export_text('31.02.2026'))]
        damaged=export_line();damaged['glyphs'][0]['text']='?';cases.append(damaged)
        for line in cases:
            with self.subTest(line=line['raw_text'],top=line['top']):
                self.assert_retained(document(line),line['raw_text'])

    def test_same_header_requires_source_first_page_and_each_page_first_line(self):
        d=document();d['pages'][0]['lines'].pop(0)
        self.assertEqual(m.reviewed_export_header_pages(d),set());self.assert_retained(d,export_text())
        d=document();d['pages'][1]['lines'].insert(0,physical_line({'65.3':'Leading business continuation'},top=22))
        self.assertEqual(m.reviewed_export_header_pages(d),{1});q=self.assert_retained(d,export_text())
        d=document(export_line(export_text('02.01.2026')))
        self.assertEqual(m.reviewed_export_header_pages(d),{1});self.assert_retained(d,export_text('02.01.2026'))

    def test_missing_or_changed_page_geometry_does_not_prove_decoration(self):
        for field,value in [('width',841),('height',600)]:
            d=document();d['pages'][1][field]=value
            self.assertEqual(m.reviewed_export_header_pages(d),{1});self.assert_retained(d,export_text())
        d=document();d['pages'][1].pop('width');self.assert_retained(d,export_text())
        d=document();d['pages'][1]['lines'][0].pop('glyphs')
        self.assertEqual(m.reviewed_export_header_pages(d),{1})

    def test_unknown_numeric_margin_is_never_restored_as_a_header_rule(self):
        for raw in ('123456789','123 456 789','123456789?','Business reference'):
            self.assert_retained(document(export_line(raw)),raw)


if __name__=='__main__':unittest.main()
