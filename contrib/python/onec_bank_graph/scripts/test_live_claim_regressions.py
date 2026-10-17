"""Bounded synthetic regressions for independently confirmed current claims."""
import copy
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
import archive_adapter as a
import statement_graph as sg
from test_admission_regressions import identity_body, parsed
from test_archive_adapter import document, row
from test_final_claim_regressions import parse_doc


def operations(graph):
    return [n for n in graph['nodes'] if n['type'] == 'Operation']


def graph_for(purpose, direction='outflow', extra=None):
    body = identity_body(direction); body[-1] = purpose
    if extra: body.insert(-1, extra)
    reviewed, _ = parsed(body + identity_body(number='2'))
    return sg.build(reviewed)


class CountedEdges(list):
    def __init__(self, values):
        super().__init__(values); self.yields = 0; self.passes = 0

    def __iter__(self):
        self.passes += 1
        for edge in super().__iter__():
            self.yields += 1
            yield edge


class LiveClaimTests(unittest.TestCase):
    def test_U7XF_explicit_currency_like_units_never_become_unlabelled(self):
        for unit in ('GBP', 'usd', 'USD', 'BYR', ''):
            purpose = 'Перевод денежных средств; комиссия 15' + (' ' + unit if unit else '')
            body = identity_body('inflow'); body[0] = row('1', credit='985'); body[-1] = purpose
            reviewed, _ = parsed(body); g = sg.build(reviewed); p = operations(g)[0]['properties']
            component = next(n['properties'] for n in g['nodes'] if n['type']=='AmountComponent')
            compatible = unit in ('BYR', '')
            self.assertEqual(component['unit_raw'], unit or None)
            self.assertEqual(component['currency_raw'], unit if unit in ('usd', 'USD', 'BYR') else None)
            self.assertEqual((component['value_raw'].strip(), component['value']), ('15', '15'))
            self.assertEqual(component['inclusion'], 'included' if compatible else 'unknown')
            self.assertEqual(p['settlement']['status'], 'verified_withheld_fee' if compatible else 'unknown')
            self.assertIsNone(p['settlement']['gross'])
            self.assertEqual((p['amount'], p['direction']), ('985', 'inflow'))
            self.assertEqual((p['purpose_raw'], p['payment1c_comment_raw']), (purpose, purpose))
            self.assertEqual(len(operations(g)), 1)
            self.assertTrue(sg.validate(g)['valid'])

    def test_U7XS_equal_subcell_glyphs_preserve_money_and_raw_geometry(self):
        for direction in ('inflow', 'outflow'):
            for token, overlay, shift in (('5.00', None, 0), ('5.00', '5', 0),
                                          ('5.00', '5', 0.01), ('5.00', '6', 0.01),
                                          ('55.00', None, 0)):
                body = identity_body(direction)
                body[0] = row('1', credit=token if direction=='inflow' else '',
                              debit=token if direction=='outflow' else '')
                original = document(body, debit_total=token if direction=='outflow' else '0.00',
                                    credit_total=token if direction=='inflow' else '0.00', closing=token)
                chars = []
                for i, item in enumerate([line for page in original['pages'] for line in page['lines']]):
                    for n, ch in enumerate(item['raw_text']):
                        char = {'text':ch, 'size':7, 'width':4.176, 'x0':49.68+4.2*n,
                                'x1':49.68+4.2*n+4.176, 'top':20+i*10, 'fontname':'synthetic'}
                        chars.append(char)
                        if overlay and i==10 and n==item['raw_text'].index(token):
                            chars.append(dict(char, text=overlay, x0=char['x0']+shift, x1=char['x1']+shift))
                reader = mock.MagicMock()
                reader.__enter__.return_value.pages = [SimpleNamespace(chars=chars, page_number=1)]
                with mock.patch.dict(sys.modules, {'pdfplumber':SimpleNamespace(open=lambda _:reader)}):
                    geom = a.geometry('synthetic-only')
                reviewed, qa = parse_doc(geom); g = sg.build(reviewed); p = operations(g)[0]['properties']
                line = geom['pages'][0]['lines'][10]
                field = 'printed_credit' if direction=='inflow' else 'printed_debit'
                start, end = a.COLUMNS[field]
                if overlay=='6':
                    self.assertTrue(line['overlaps'])
                    self.assertIsNone(p['amount']); self.assertEqual(p['direction'], 'unknown')
                    self.assertEqual(p['processing']['status'], 'needs_clarification')
                else:
                    self.assertEqual(line['overlaps'], [])
                    self.assertEqual(a.field_slice(line, start, end).strip(), token)
                    self.assertEqual((p['amount'], p['direction']), (token, direction))
                    self.assertEqual(p['processing']['status'], 'ready')
                    self.assertEqual(qa['rows'][0]['fields'][field]['raw'].strip(), token)
                    if overlay:
                        raw_digits = [glyph for glyph in line['glyphs'] if glyph['text']=='5' and start <= glyph['cell'] < end]
                        self.assertEqual(len(raw_digits), 2)
                        self.assertEqual(raw_digits[0]['cell'], raw_digits[1]['cell'])
                        self.assertAlmostEqual(raw_digits[1]['x0']-raw_digits[0]['x0'], shift)
                        self.assertIn('55.00', line['raw_text'])
                self.assertEqual(p['purpose_raw'], p['payment1c_comment_raw'])
                self.assertEqual(len(operations(g)), 1)
                self.assertTrue(sg.validate(g)['valid'])

    def test_UWHK_source_classification_and_status_are_reproduced(self):
        for direction in ('inflow', 'outflow'):
            for purpose in ('Оплата синтетической услуги', 'Вознаграждение за прием наличных денег'):
                g = graph_for(purpose, direction); p = operations(g)[0]['properties']
                standalone = direction == 'outflow' and 'Вознаграждение' in purpose
                self.assertEqual(p['classification'], 'bank_fee' if standalone else None)
                self.assertTrue(sg.validate(g)['valid'])
                for classification, status in ((None if standalone else 'bank_fee', 'unknown' if standalone else 'inferred'),
                                               (p['classification'], 'unknown' if standalone else 'inferred')):
                    bad = copy.deepcopy(g); bp = operations(bad)[0]['properties']
                    bp.update(classification=classification, classification_status=status)
                    v = sg.validate(bad)
                    self.assertFalse(v['valid'])
                    self.assertTrue(any('classification differs' in e for e in v['errors']))
                    self.assertEqual((bp['amount'], bp['direction'], bp['purpose_raw']),
                                     (p['amount'], p['direction'], p['purpose_raw']))
        g = graph_for('Вознаграждение за прием наличных денег')
        operations(g)[0]['properties'].update(purpose_status='unknown')
        self.assertTrue(any('fee classification has unresolved' in e for e in sg.validate(g)['errors']))

    def test_UWHR_single_replacement_structural_keywords_hold_only_the_row(self):
        for word, suffix in (('УНП', 'плательщика: 999888777'), ('УНП', 'получателя: 999888777'),
                             ('Наименование', 'корреспондента: COMPETING NAME')):
            for offset in range(len(word)):
                for glyph in ('?', '*', '\ufffd'):
                    keyword = word[:offset] + glyph + word[offset+1:]
                    text = keyword + ' ' + suffix
                    metadata, assertions, purpose, gaps = a.metadata_fields([text], [{}])
                    self.assertEqual(purpose, [])
                    self.assertIn('unsupported_or_damaged_identity_label', gaps)
                    self.assertEqual(assertions[0]['value_raw'], suffix.split(': ', 1)[1])
                    self.assertNotIn(assertions[0]['label'], a.IDENTITY_FIELD_LABELS)
        for direction in ('inflow', 'outflow'):
            role = 'плательщика' if direction == 'inflow' else 'получателя'
            for extra in ('УН? '+role+': 999888777', 'У?П '+role+': 999888777',
                          'На?менование корреспондента: COMPETING NAME',
                          'Наименован?е корреспондента: COMPETING NAME'):
                g = graph_for('Exact purpose', direction, extra); ops = operations(g)
                admission = sg.identity_admission(g)['statements'][0]
                self.assertEqual(admission['field_types']['status'], 'supported')
                self.assertEqual(ops[0]['properties']['processing']['status'], 'needs_clarification')
                self.assertEqual(ops[0]['properties']['purpose_raw'], 'Exact purpose')
                self.assertEqual(ops[0]['properties']['payment1c_comment_raw'], 'Exact purpose')
                detail = next(o['raw'] for o in ops[0]['observations'] if o['field']=='identity_detail_raw')
                self.assertIn(extra, detail)
                self.assertEqual(ops[1]['properties']['processing']['status'], 'ready')
                self.assertTrue(sg.validate(g)['valid'])
        text = 'Деловой текст: другое на?менование товара и обычная строка'
        self.assertEqual(a.metadata_fields([text], [{}])[2], [text])
        self.assertEqual(a.metadata_fields([text], [{}])[3], [])
        body = [s.replace('УНП', 'УН?').replace('Наименование', 'На?менование') for s in identity_body()]
        reviewed, _ = parsed(body)
        self.assertEqual(sg.identity_admission(sg.build(reviewed, diagnostic=True))['status'], 'blocked')

    def test_UWHW_ruble_price_retains_number_without_currency_inference(self):
        for direction in ('inflow', 'outflow'):
            for unit in ('РУБ.', 'рублей', 'Р УБ.', 'BYR'):
                purpose = 'комиссия банка за ведение счета 15 ' + unit
                g = graph_for(purpose, direction); p = operations(g)[0]['properties']
                c = next(n['properties'] for n in g['nodes'] if n['type']=='AmountComponent')
                self.assertEqual((c['value_raw'], c['value'], c['unit_raw']), ('15', '15', unit))
                self.assertEqual(c['currency_raw'], 'BYR' if unit=='BYR' else None)
                self.assertEqual(c['inclusion'], 'unknown')
                self.assertEqual(p['settlement']['status'], 'unknown' if direction=='inflow' else 'not_applicable')
                self.assertEqual((p['purpose_raw'], p['payment1c_comment_raw']), (purpose, purpose))
                self.assertEqual((p['amount'], p['direction']), ('3' if direction=='inflow' else '2', direction))
                self.assertTrue(sg.validate(g)['valid'])
        self.assertEqual(a.amount_components('комиссия банка за ведение счета', 'inflow', '3'), [])
        for unit in ('РУБ.', 'BYR'):
            cs = a.amount_components('комиссия банка за ведение счета -15 '+unit, 'inflow', '3')
            self.assertEqual(a.component_money(cs[0][2]), '-15'); self.assertEqual(cs[0][4], 'unknown')
            cs = a.amount_components('комиссия банка по договору 2020 за услуги 15 '+unit, 'inflow', '3')
            self.assertIsNone(a.component_money(cs[0][2])); self.assertIn('2020', cs[0][2])

    def test_UWHZ_matching_substituted_purpose_and_comment_rejected(self):
        purpose = 'Исходное синтетическое назначение\nПолная вторая строка'
        for extra in (None, 'Деловой префикс УНП за кого: 999888777'):
            g = graph_for(purpose, extra=extra); op = operations(g)[0]; p = op['properties']
            detail = next(o['raw'] for o in op['observations'] if o['field']=='identity_detail_raw')
            lines = detail.split('\n'); reconstructed = '\n'.join(a.metadata_fields(lines, [{} for _ in lines])[2])
            self.assertEqual(p['purpose_raw'], reconstructed); self.assertTrue(sg.validate(g)['valid'])
            bad = copy.deepcopy(g); bp = operations(bad)[0]['properties']
            bp.update(purpose_raw='Подменённое поручение', payment1c_comment_raw='Подменённое поручение')
            self.assertTrue(any('purpose differs from retained source detail' in e for e in sg.validate(bad)['errors']))
            bp['payment1c_comment_raw'] = 'Другой комментарий'
            self.assertTrue(any('payment comment differs' in e for e in sg.validate(bad)['errors']))
        body = identity_body(); index = next(i for i, s in enumerate(body) if s.startswith('Наименование корреспондента'))
        body.insert(index+1, ' '*25+'SECOND NAME LINE')
        reviewed, _ = parsed(body); self.assertTrue(sg.validate(sg.build(reviewed))['valid'])

    def test_UWHg_index_preserves_ambiguity_coverage_and_single_edge_pass(self):
        g = graph_for('Exact purpose'); opid = operations(g)[0]['id']
        for edge_type, gap, target_side in (('CONTAINS', 'original_row_membership_ambiguous', 'to'),
                                           ('PAYEE', 'counterparty_role_link_missing_or_ambiguous', 'from')):
            for mutation in ('duplicate', 'wrong_target', 'missing'):
                bad = copy.deepcopy(g)
                edge = next(e for e in bad['edges'] if e['type']==edge_type and e[target_side]==opid)
                if mutation=='duplicate': bad['edges'].append(dict(edge, id=edge['id']+':duplicate'))
                elif mutation=='wrong_target': edge['from' if edge_type=='CONTAINS' else 'to'] = 'missing:synthetic'
                else: bad['edges'].remove(edge)
                state = sg.identity_admission(bad)['statements'][0]
                self.assertIn(gap, state['coverage_gaps'] if edge_type=='CONTAINS' else state['rows'][0]['gaps'])
        bad = copy.deepcopy(g)
        next(e for e in bad['edges'] if e['from']==opid and e['type']=='PAYEE')['type']='PAYER'
        self.assertIn('counterparty_role_link_missing_or_ambiguous', sg.identity_admission(bad)['statements'][0]['rows'][0]['gaps'])
        reviewed, _ = parsed(identity_body(name='???') + identity_body(number='2'))
        partial = sg.identity_admission(sg.build(reviewed))
        self.assertEqual(partial['status'], 'partial'); self.assertEqual(partial['statements'][0]['ready_count'], 1)
        for count in (8, 16, 32):
            body = []
            for n in range(1, count+1): body += identity_body(number=str(n))
            reviewed, _ = parsed(body); graph = sg.build(reviewed)
            edges = CountedEdges(graph['edges']); graph['edges'] = edges
            self.assertEqual(sg.identity_admission(graph)['status'], 'admitted')
            self.assertEqual(edges.passes, 1); self.assertEqual(edges.yields, len(edges))


if __name__ == '__main__': unittest.main()
