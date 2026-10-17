"""Synthetic source-contract regressions; no actual PDF proof is claimed."""
import copy
import json
from pathlib import Path
import unittest
import archive_adapter as aa
import source_readiness as sr
import statement_graph as sg

BASE = Path(__file__).resolve().parents[1]


class SourceContractGuards(unittest.TestCase):
    def test_header_preserves_distinct_names_wraps_unknown_codes_and_absence(self):
        lines = ['SYNTHETIC BANK', 'МФО BAD-CODE', 'Исполнитель 001',
                 'Дата пред. опер. Первая выписка по счету',
                 'Cчет клиента SYNTHETIC-ACCOUNT BYN', 'Наименование Synthetic owner',
                 '  printed continuation', 'Наименование клиента Separate printed spelling',
                 'Входящее сальдо 0.00', 'Наименование корреспондента: transaction name']
        fields = aa.source_header_fields(lines)
        self.assertEqual(fields['own_bank_code_raw'], 'BAD-CODE')
        self.assertEqual(fields['own_bank_code_label'], 'МФО')
        self.assertIsNone(fields['previous_operation_date_raw'])
        self.assertEqual(fields['own_organization_name_assertions'], [
            {'label': 'Наименование', 'raw': 'Synthetic owner\nprinted continuation'},
            {'label': 'Наименование клиента', 'raw': 'Separate printed spelling'}])
        st = {'properties': {'account_id': 'account'}, 'observations': []}
        entities = [aa.entry('account', 'Account', {'number_raw': 'SYNTHETIC-ACCOUNT'})]
        edges = []
        aa.materialize_source_header(st, entities, edges, lines, 1)
        bank = next(n for n in entities if n['type'] == 'Bank')
        self.assertEqual(bank['properties']['identity_status'], 'unknown')
        self.assertEqual(bank['properties']['code_raw'], 'BAD-CODE')
        self.assertIsNone(st['properties']['previous_operation_date'])
        self.assertEqual({e['type'] for e in edges}, {'OWNED_BY', 'SERVICED_BY'})
        self.assertIsNone(next(n for n in entities if n['type'] == 'Party')['properties']['unp_raw'])

    def test_source_projection_evidence_has_exact_unique_operation_parity(self):
        graph = sg.build(json.loads((BASE / 'assets/synthetic-reviewed.json').read_text()))
        for node in graph['nodes']:
            if node['type'] == 'Operation':
                node['properties']['payment1c_comment_raw'] = node['properties']['purpose_raw']
        review = {'profile': sr.PROFILE, 'source_review': 'independent_pdf_review',
                  'node_fingerprints': {n['id']: sr.fact_fingerprint(n) for n in graph['nodes']},
                  'edge_fingerprint': sr.edge_fingerprint(graph['edges']),
                  'operations': 2, 'full_field_coverage': True, 'unresolved_source_errors': [],
                  'service_projections': {}}
        self.assertFalse(sr.review_matches(graph, review))
        for target in ['missing-ID', next(n['id'] for n in graph['nodes'] if n['type'] == 'Statement')]:
            bad = copy.deepcopy(review)
            bad['service_projections'][target] = {'resource_bound': True}
            self.assertTrue(sr.review_matches(graph, bad))
        op = next(n for n in graph['nodes'] if n['type'] == 'Operation')
        for projection in [None, {}, '']:
            bad = copy.deepcopy(graph)
            next(n for n in bad['nodes'] if n['id'] == op['id'])['properties']['source_text'] = projection
            proof = copy.deepcopy(review)
            proof['node_fingerprints'] = {n['id']: sr.fact_fingerprint(n) for n in bad['nodes']}
            self.assertTrue(sr.review_matches(bad, proof))
        duplicate = copy.deepcopy(graph)
        duplicate['nodes'].append(copy.deepcopy(op))
        proof = copy.deepcopy(review)
        proof['operations'] += 1
        self.assertIn('duplicate source node ID', sr.review_matches(duplicate, proof))

    def test_accept_requires_explicit_supported_unit_review(self):
        graph = sg.build(json.loads((BASE / 'assets/synthetic-reviewed.json').read_text()))
        controls = json.loads((BASE / 'assets/synthetic-controls.json').read_text())
        self.assertTrue(sg.check_conversion(graph, controls)['accepted'])
        unit = controls['statements'][0]['source_unit_review']
        changes = [None, {**unit, 'source_epoch': 'pre_2016_07_01'},
                   {**unit, 'source_unit': 'unknown'}, {**unit, 'basis': ' '},
                   {**unit, 'status': 'unknown'}, {**unit, 'transfer_factor': '10000'}]
        for changed in changes:
            bad = copy.deepcopy(controls)
            if changed is None:
                bad['statements'][0].pop('source_unit_review')
            else:
                bad['statements'][0]['source_unit_review'] = changed
            checked = sg.check_conversion(graph, bad)
            self.assertFalse(checked['accepted'])
            self.assertNotEqual(checked['math_status'], 'pass')
        self.assertEqual(graph, sg.build(json.loads((BASE / 'assets/synthetic-reviewed.json').read_text())))


if __name__ == '__main__':
    unittest.main()
