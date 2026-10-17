"""Synthetic text-boundary regressions; no original bank data or 1C writes."""
import copy
import unittest
import json
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import archive_adapter as adapter
import statement_graph as sg
from payment_text import prepare_payment_text, writable_payment_text, xml10_character, SOURCE_BOUND_PROFILE
from test_archive_adapter import document, row


def reviewed(purposes):
    body = []
    for ordinal, purpose in enumerate(purposes, 1):
        body.extend([row(str(ordinal), credit='3.00'),
                     'Наименование корреспондента: SYNTHETIC CUSTOMER',
                     'УНП плательщика: 123456789', 'Очередность: 22', *purpose.split('\n')])
    st, en, rel, qa = adapter.parse_statement(document(body), 1, 'text-synthetic')
    return {'dataset_id': 'text-synthetic', 'statements': [st], 'entities': en, 'relations': rel}, qa


class PaymentTextTests(unittest.TestCase):
    def projection(self, value):
        return {'profile': 'source-bound-winansi-1b-cash-service-v1',
                'value': value, 'raw_preserved': True}

    def test_source_bound_all_endpoint_encodings_preserve_business_and_default_profile(self):
        for opening in ['\x1b4', '(cid:27)4']:
            for closing in ['\x1b5', '(cid:27)5']:
                raw = 'Синтетическое ё <>& 45\r\n\t'+opening+'символ 2004 : 455.00'+closing+'\r\nДоговор 54'
                projection = self.projection('Синтетическое ё <>& 45\r\n\tсимвол 2004 : 455.00\r\nДоговор 54')
                original = {'purpose_raw': raw, 'purpose_status': 'observed', 'payment1c_comment_raw': raw,
                            'source_text': projection, 'amount': '3.00',
                            'processing': {'status': 'needs_clarification', 'gaps': ['payment_text_held']}}
                original['payment1c_text'] = prepare_payment_text(raw)
                saved = copy.deepcopy(original)
                prepared = copy.deepcopy(original)
                prepared['payment1c_text'] = prepare_payment_text(raw, source_text=projection)
                expected = 'Синтетическое ё <>& 45 символ 2004 : 455.00 Договор 54'
                self.assertEqual(prepared['payment1c_text']['profile'], SOURCE_BOUND_PROFILE)
                self.assertEqual(writable_payment_text(prepared, source_text=projection), expected)
                self.assertEqual(original, saved)
                self.assertEqual(prepared['amount'], original['amount'])
                self.assertEqual(prepared['processing'], original['processing'])
                with self.assertRaises(ValueError):
                    writable_payment_text(prepared)  # explicit opt-in required
                self.assertEqual(original['payment1c_text']['status'], 'ready')
                self.assertEqual([d['index'] for d in prepared['payment1c_text']['diagnostics'][:2]],
                                 [raw.index(opening), raw.index(closing)])

    def test_source_bound_projection_and_guard_reject_substitution(self):
        raw = '\x1b4символ 2004: 455.00(cid:27)5'
        projection = self.projection('символ 2004: 455.00')
        for bad in [{**projection, 'value': 'символ 2004: 454.00'},
                    {**projection, 'raw_preserved': False}, {**projection, 'profile': 'unknown'},
                    {**projection, 'extra': 'forged'}, {}, 'short']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prepare_payment_text(raw, source_text=bad)
        properties = {'purpose_raw': raw, 'purpose_status': 'observed', 'source_text': projection,
                      'payment1c_text': prepare_payment_text(raw, source_text=projection)}
        for field, value in [('value', 'short'), ('diagnostics', []), ('status', 'held'), ('profile', 'invented')]:
            bad = copy.deepcopy(properties); bad['payment1c_text'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                writable_payment_text(bad, source_text=projection)
        bad = copy.deepcopy(properties); bad['source_text']['value'] = 'forged'
        with self.assertRaises(ValueError):
            writable_payment_text(bad, source_text=projection)

    def test_source_bound_incomplete_nested_or_non_service_context_rejected(self):
        for raw in ['(cid:27)4символ 2004: 5', 'символ 2004: 4(cid:27)5',
                    '(cid:27)4договор 45(cid:27)5', 'A (cid:27)4символ 2004: 5(cid:27)5',
                    '(cid:27)4символ 2004: 5(cid:27)54', '(cid:27)4символ 2004: 5\n(cid:27)5',
                    '(cid:27)4символ 2004: 5(cid:27)4(cid:27)5']:
            with self.subTest(raw=raw):
                try:
                    text = prepare_payment_text(raw, source_text=self.projection(raw))
                except ValueError:
                    continue
                self.assertEqual(text['status'], 'held')
                self.assertIsNone(text['value'])

    def test_source_bound_other_controls_and_unresolved_boundary_still_hold(self):
        for tail, status in [('\x00', 'observed'), ('\x1b6', 'observed'), ('\x85', 'observed'),
                             ('(cid:27)6', 'observed'), ('', 'unknown')]:
            raw = '(cid:27)4символ 2004: 455.00\x1b5\n'+tail
            result = prepare_payment_text(raw, status, source_text=self.projection('символ 2004: 455.00\n'+tail))
            self.assertEqual(result['status'], 'held')
            self.assertIsNone(result['value'])
    def test_all_raw_cid_and_mixed_pairs_preserve_business_digits_and_offsets(self):
        for start in ('\x1b4', '(cid:27)4'):
            for end in ('\x1b5', '(cid:27)5'):
                with self.subTest(start=repr(start), end=repr(end)):
                    business = 'символ 2004 : 455.0045 (договор 45) <>& ё'
                    raw = 'Договор 45\r\n\t\u00a0' + start + business + end + ' \t\r\nНДС 54.00'
                    saved = raw
                    result = prepare_payment_text(raw)
                    expected = 'Договор 45 ' + business + ' НДС 54.00'
                    self.assertEqual(result['status'], 'ready')
                    self.assertEqual(result['value'], expected)
                    self.assertEqual(raw, saved)
                    removed = [d for d in result['diagnostics'] if d['code'].startswith('removed_')]
                    self.assertEqual(removed, [
                        {'code': 'removed_escp_italic_4', 'index': raw.index(start),
                         'codepoint': 'U+001B' if start[0] == '\x1b' else 'U+0028'},
                        {'code': 'removed_escp_italic_5', 'index': raw.index(end),
                         'codepoint': 'U+001B' if end[0] == '\x1b' else 'U+0028'}])
                    self.assertEqual(prepare_payment_text(result['value'])['value'], expected)
                    self.assertEqual(prepare_payment_text(result['value'])['diagnostics'], [])
                    self.assertEqual(ET.fromstring('<text>'+escape(expected)+'</text>').text, expected)

    def test_repeated_differently_encoded_pairs_use_original_offsets(self):
        raw = '(cid:27)4символ 45: 45.00\x1b5\n\x1b4символ 54: 54.00(cid:27)5'
        result = prepare_payment_text(raw)
        self.assertEqual(result['value'], 'символ 45: 45.00 символ 54: 54.00')
        removed = [d for d in result['diagnostics'] if d['code'].startswith('removed_')]
        self.assertEqual([d['index'] for d in removed],
                         [raw.index('(cid:27)4'), raw.index('\x1b5'), raw.index('\x1b4'), raw.index('(cid:27)5')])

    def test_closing_token_cannot_hide_a_damaged_cid_business_suffix(self):
        for start in ('\x1b4', '(cid:27)4'):
            for end in ('\x1b5', '(cid:27)5'):
                for suffix in ('', ' \t', '\r\nДоговор 54'):
                    with self.subTest(start=repr(start), end=repr(end), suffix=repr(suffix)):
                        raw = 'Бизнес 50045\n\t'+start+'символ 45: (cid'+end+suffix
                        result = prepare_payment_text(raw)
                        self.assertEqual(result['status'], 'held')
                        self.assertIsNone(result['value'])
                        self.assertIn({'code': 'unknown_escape_sequence',
                                       'index': raw.index('(cid', raw.index('символ')), 'codepoint': 'U+0028'},
                                      result['diagnostics'])
                        props = {'purpose_raw': raw, 'purpose_status': 'observed',
                                 'payment1c_comment_raw': raw, 'payment1c_text': result}
                        with self.assertRaises(ValueError):
                            writable_payment_text(props)
        # A complete ordinary word with a similar prefix stays business text.
        raw = '(cid:27)4символ 45: (cider) 50045.00\x1b5'
        result = prepare_payment_text(raw)
        self.assertEqual(result['value'], 'символ 45: (cider) 50045.00')
        self.assertEqual(prepare_payment_text(result['value'])['status'], 'ready')

    def test_exposed_cid_suffix_holds_only_its_graph_row_without_source_changes(self):
        purposes = ['\x1b4символ 45: (cid\x1b5', 'Independent business 50045.00']
        source, qa = reviewed(purposes)
        saved = copy.deepcopy(source)
        graph = sg.build(source)
        self.assertEqual(source, saved)
        self.assertTrue(sg.validate(graph)['valid'], sg.validate(graph)['errors'])
        ops = [n['properties'] for n in graph['nodes'] if n['type'] == 'Operation']
        self.assertEqual([p['processing']['status'] for p in ops], ['needs_clarification', 'ready'])
        for props, purpose, evidence in zip(ops, purposes, qa['rows']):
            self.assertEqual(props['purpose_raw'], purpose)
            self.assertEqual(props['payment1c_comment_raw'], purpose)
            self.assertEqual(props['amount'], '3.00')
            self.assertEqual(props['payment1c_text'], evidence['document_view']['payment1c_text'])
        stale = copy.deepcopy(graph)
        props = next(n['properties'] for n in stale['nodes'] if n['type'] == 'Operation')
        props['payment1c_text']['status'] = 'ready'
        props['payment1c_text']['value'] = 'символ 45: (cid'
        self.assertFalse(sg.validate(stale)['valid'])
        with self.assertRaises(ValueError):
            writable_payment_text(props)

    def test_cid_near_miss_nested_damaged_and_unsupported_forms_stay_held(self):
        variants = [
            '(cid:27)4символ 45: 45.00', 'символ 45: 45.00(cid:27)5',
            '(cid:27)4договор 45(cid:27)5', '(cid:27)412345(cid:27)5',
            '(cid:27)4символ 45: 45.00(cid:27)54',
            '(cid:27)4символ 45: 45.00\x1b54',
            '\x1b4символ 45: (cid:27)4 45.00(cid:27)5',
            '(cid:27)4символ 45: \x1b4 45.00\x1b5',
            '(cid:27)4символ 45: (cid:42) 45.00(cid:27)5',
            '(cid:27)4символ 45: 45.00(cid:27)5(cid:27)5',
            '(cid:27) 4символ 45: 45.00(cid:27)5',
            '(cid:027)4символ 45: 45.00(cid:27)5',
            '(CID:27)4символ 45: 45.00(cid:27)5',
            '(cid:27)6символ 45: 45.00(cid:27)5',
            '(cid:27)4символ 45: 45.00(cid:27',
            '(cid:27)4символ 45: 45.00\n(cid:27)5',
            'prefix (cid:27)4символ 45: 45.00(cid:27)5',
            '(cid:27)4символ 45: 45.00(cid:27)5 suffix',
            '(cid:27)4символ 45: (cid 42) 45.00(cid:27)5',
            '(cid:27)4символ 45: (cid)text 45.00(cid:27)5',
        ]
        for raw in variants:
            with self.subTest(raw=repr(raw)):
                result = prepare_payment_text(raw)
                self.assertEqual(result['status'], 'held')
                self.assertIsNone(result['value'])
                self.assertTrue(any(d['code'] == 'unknown_escape_sequence' for d in result['diagnostics']))
                self.assertFalse(any(d['code'].startswith('removed_') for d in result['diagnostics']))

    def test_arbitrary_cid_markers_are_not_silently_deleted_or_accepted(self):
        for marker in ('(cid:42)', '(cid:27)', '(cid:27', '(CID:27)5', '(cid)text', '(cid 27)'):
            raw = 'Бизнес 45 ' + marker + ' 50045.00'
            result = prepare_payment_text(raw)
            self.assertEqual(result['status'], 'held')
            self.assertIn({'code': 'unknown_escape_sequence', 'index': raw.index(marker),
                           'codepoint': 'U+0028'}, result['diagnostics'])
            self.assertFalse(any(d['code'].startswith('removed_') for d in result['diagnostics']))
        self.assertEqual(prepare_payment_text('Бизнес 45 (cider) 50045.00')['value'], 'Бизнес 45 (cider) 50045.00')

    def test_unmatched_textual_cid_has_one_actual_codepoint_diagnostic(self):
        raw = 'Бизнес 45\n(cid:27)4символ 2004: 45.00(cid:27)5\n(cid:27)6'
        projection = self.projection('Бизнес 45\nсимвол 2004: 45.00\n(cid:27)6')
        result = prepare_payment_text(raw, source_text=projection)
        unmatched = [item for item in result['diagnostics']
                     if item['code'] == 'unknown_escape_sequence' and item['index'] == raw.index('(cid:27)6')]
        self.assertEqual(unmatched, [{'code': 'unknown_escape_sequence',
                                      'index': raw.index('(cid:27)6'), 'codepoint': 'U+0028'}])
        self.assertEqual(result['status'], 'held')

    def test_mixed_pair_does_not_clear_unsafe_character_or_unknown_boundary(self):
        line = '(cid:27)4символ 45: 45.00\x1b5'
        for raw, status, code in [(line, 'unknown', 'purpose_boundary_unresolved'),
                                  ('\x00\n'+line, 'observed', 'xml10_forbidden_character'),
                                  (line+'\n(cid:42)', 'observed', 'unknown_escape_sequence')]:
            result = prepare_payment_text(raw, status)
            self.assertEqual(result['status'], 'held')
            self.assertIsNone(result['value'])
            self.assertTrue(any(d['code'] == code for d in result['diagnostics']))

    def test_cid_mixed_adapter_and_graph_preserve_source_money_and_reject_stale_value(self):
        purposes = ['Бизнес 45\n'+start+'символ 2004: 455.0045'+end
                    for start in ('\x1b4', '(cid:27)4') for end in ('\x1b5', '(cid:27)5')]
        source, qa = reviewed(purposes)
        saved = copy.deepcopy(source)
        graph = sg.build(source)
        self.assertEqual(source, saved)
        self.assertTrue(sg.validate(graph)['valid'], sg.validate(graph)['errors'])
        ops = [n['properties'] for n in graph['nodes'] if n['type'] == 'Operation']
        for p, purpose, evidence in zip(ops, purposes, qa['rows']):
            self.assertEqual(p['purpose_raw'], purpose)
            self.assertEqual(p['payment1c_comment_raw'], purpose)
            self.assertEqual(p['amount'], '3.00')
            self.assertEqual(p['direction'], 'inflow')
            self.assertEqual(p['payment1c_text'], evidence['document_view']['payment1c_text'])
            self.assertEqual(writable_payment_text(p), 'Бизнес 45 символ 2004: 455.0045')
            self.assertEqual(p['processing']['status'], 'ready')
            stale = copy.deepcopy(p)
            stale['payment1c_text']['value'] = purpose
            with self.assertRaises(ValueError):
                writable_payment_text(stale)
            stale = copy.deepcopy(p)
            stale['purpose_status'] = 'unknown'
            with self.assertRaises(ValueError):
                writable_payment_text(stale)
            stale = copy.deepcopy(p)
            stale['payment1c_comment_raw'] += 'changed'
            with self.assertRaises(ValueError):
                writable_payment_text(stale)

    def test_bounded_escp_pair_preserves_full_cash_business_line_and_real_digits(self):
        raw = 'Синтетическая выручка 45\n   \x1b4символ 2004 :      455.00\x1b5\nДоговор 45'
        text = prepare_payment_text(raw)
        self.assertEqual(text['value'], 'Синтетическая выручка 45 символ 2004 : 455.00 Договор 45')
        self.assertEqual(text['status'], 'ready')
        removed = [d for d in text['diagnostics'] if d['code'].startswith('removed_')]
        self.assertEqual([d['index'] for d in removed], [raw.index('\x1b4'), raw.index('\x1b5')])
        self.assertNotIn('\x1b', text['value'])
        self.assertNotIn('<ESC>', text['value'])

    def test_multiple_pairs_crlf_and_no_parameter_digit_consumption(self):
        raw = '\x1b4символ 45: 45.00\x1b5\r\n\t\x1b4символ 54: 54.00\x1b5'
        self.assertEqual(prepare_payment_text(raw)['value'], 'символ 45: 45.00 символ 54: 54.00')

    def test_unknown_sequences_and_damaged_pair_hold_instead_of_guessing(self):
        for raw in ['A\x1b6B', 'A\x1b', '\x1b4символ 2004: 5', 'символ 2004: 4\x1b5',
                    '\x1b4договор 45\x1b5', '\x1b412345\x1b5', '\x1b4символ 2004: 5\x1b54',
                    '\x1b4символ 2004: 5\x1b4\x1b5']:
            with self.subTest(raw=repr(raw)):
                result = prepare_payment_text(raw)
                self.assertEqual(result['status'], 'held')
                self.assertIsNone(result['value'])
                self.assertTrue(any(d['code'] == 'unknown_escape_sequence' for d in result['diagnostics']))

    def test_forbidden_codepoints_diagnose_original_offset_without_writable_value(self):
        for n in list(range(32)) + [0xD800, 0xDFFF, 0xFFFE, 0xFFFF]:
            if n in [9, 10, 13]:
                continue
            result = prepare_payment_text('ё'+chr(n)+' tail')
            self.assertIsNone(result['value'])
            code = 'unknown_escape_sequence' if n == 27 else 'xml10_forbidden_character'
            self.assertIn({'code': code, 'index': 1, 'codepoint': 'U+'+format(n, '04X')}, result['diagnostics'])
        for n in [0x7F, 0x80, 0x85, 0x9F]:
            self.assertTrue(xml10_character(chr(n)))
            result = prepare_payment_text('a'+chr(n)+'b')
            self.assertIsNone(result['value'])
            self.assertEqual(result['diagnostics'][0]['code'], 'unsupported_control_character')

    def test_unicode_markup_quotes_whitespace_and_idempotence(self):
        raw = '  Ёлка\tё\r\nA\u00a0\u00a0B\u2003C <>& "quote" \'text\' e\u0301 😀  '
        result = prepare_payment_text(raw)
        expected = 'Ёлка ё A B C <>& "quote" \'text\' e\u0301 😀'
        self.assertEqual(result['value'], expected)
        again = prepare_payment_text(expected)
        self.assertEqual(again['value'], expected)
        self.assertEqual(again['diagnostics'], [])
        # Encoding/escaping belongs to the transport, preserving actual text.
        self.assertEqual(ET.fromstring('<text>'+escape(expected)+'</text>').text, expected)

    def test_source_empty_is_preserved_but_unknown_boundary_still_holds(self):
        for raw in ('', ' \n\t\u00a0'):
            result = prepare_payment_text(raw, 'observed')
            self.assertEqual((result['status'], result['value']), ('ready', ''))
            props = {'purpose_raw': raw, 'purpose_status': 'observed',
                     'payment1c_comment_raw': raw, 'payment1c_text': result}
            self.assertEqual(writable_payment_text(props), '')
        for raw in ('', ' \n\t\u00a0', 'Readable'):
            result = prepare_payment_text(raw, 'unknown')
            self.assertEqual((result['status'], result['value']), ('held', None))
        for raw in ('\x00', '\x1b4\x1b5', '(cid:27)', '(cid)'):
            self.assertEqual(prepare_payment_text(raw)['status'], 'held')

    def test_xml_range_edges_and_held_surrogate_json_roundtrip(self):
        for n in [9, 10, 13, 0x20, 0xD7FF, 0xE000, 0xFFFD, 0x10000, 0x10FFFF]:
            self.assertTrue(xml10_character(chr(n)))
        raw = 'Raw\ud800 full'
        data = {'purpose_raw': raw, 'payment1c_text': prepare_payment_text(raw)}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'held.json'
            sg.write_new(p, data)
            self.assertEqual(json.loads(p.read_text(encoding='utf-8')), data)

    def test_long_full_text_never_truncated_or_changed_twice(self):
        raw = ('Договор 45\tё <>&\n' * 10000) + 'КОНЕЦ 50045'
        expected = ('Договор 45 ё <>& ' * 10000) + 'КОНЕЦ 50045'
        self.assertEqual(prepare_payment_text(raw)['value'], expected)
        self.assertEqual(prepare_payment_text(expected)['value'], expected)

    def test_adapter_graph_raw_money_coverage_and_independent_rows(self):
        purposes = ['Синтетическая выручка\n \x1b4символ 2004: 455.00\x1b5',
                    'Unknown\x00 unsafe', 'Independent\tё <>&']
        source, qa = reviewed(purposes)
        saved = copy.deepcopy(source)
        graph = sg.build(source)
        self.assertEqual(source, saved)
        self.assertTrue(sg.validate(graph)['valid'], sg.validate(graph)['errors'])
        ops = [n for n in graph['nodes'] if n['type'] == 'Operation']
        self.assertEqual(len(ops), 3)
        self.assertEqual([n['properties']['processing']['status'] for n in ops], ['ready', 'needs_clarification', 'ready'])
        for op, purpose, evidence in zip(ops, purposes, qa['rows']):
            p = op['properties']
            self.assertEqual(p['purpose_raw'], purpose)
            self.assertEqual(p['payment1c_comment_raw'], purpose)
            self.assertEqual(p['amount'], '3.00')
            self.assertEqual(p['direction'], 'inflow')
            self.assertEqual(p['payment1c_text'], evidence['document_view']['payment1c_text'])
        self.assertEqual(qa['rows'][1]['document_view']['row_processing_allowed'], False)
        self.assertEqual(writable_payment_text(ops[0]['properties']), 'Синтетическая выручка символ 2004: 455.00')
        with self.assertRaises(ValueError):
            writable_payment_text(ops[1]['properties'])
        self.assertEqual(sg.identity_admission(graph)['status'], 'partial')
        self.assertEqual(sum(sg.decimal(n['properties']['amount']) for n in ops), sg.decimal('9.00'))

    def test_forged_stale_missing_value_status_diagnostics_and_ready_claims_rejected(self):
        source, _ = reviewed(['Safe\n full', 'Unsafe\x00'])
        graph = sg.build(source)
        for field, value in [('value', 'short'), ('status', 'held'), ('diagnostics', []), ('profile', 'invented')]:
            candidate = copy.deepcopy(graph)
            p = next(n for n in candidate['nodes'] if n['type'] == 'Operation')['properties']
            p['payment1c_text'][field] = value
            self.assertFalse(sg.validate(candidate)['valid'], field)
            with self.assertRaises(ValueError):
                writable_payment_text(p)
        candidate = copy.deepcopy(graph)
        p = [n for n in candidate['nodes'] if n['type'] == 'Operation'][1]['properties']
        p['processing']['status'] = 'ready'
        p['processing']['gaps'] = []
        self.assertFalse(sg.validate(candidate)['valid'])
        candidate = copy.deepcopy(graph)
        p = next(n for n in candidate['nodes'] if n['type'] == 'Operation')['properties']
        del p['payment1c_text']
        with self.assertRaises(ValueError):
            writable_payment_text(p)
        with self.assertRaises(ValueError):
            sg.require_processing_admission(candidate)
        self.assertNotEqual(sg.identity_admission(candidate)['statements'][0]['rows'][0]['status'], 'ready')


if __name__ == '__main__':
    unittest.main()
