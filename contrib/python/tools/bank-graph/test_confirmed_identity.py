"""Reader uses additive confirmed identities, retaining original lookup paths."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from reader import Reader, QueryError, _load_source_modules, CANONICAL_MODULES

SCRIPTS=Path(__file__).resolve().parents[2]/'onec_bank_graph/scripts'
MODULES=_load_source_modules(SCRIPTS,CANONICAL_MODULES+('test_archive_adapter','test_identifiers_modern_math','test_modern_reviewed','test_counterparty_identity','test_admission_regressions','test_owner_role_unps','test_modern_identity_account'))
fixture=MODULES['test_counterparty_identity'];role_fixture=MODULES['test_owner_role_unps']


def write(root,graphs):
    inputs=[]
    for index,g in enumerate(graphs):
        name='g'+str(index)+'.json';(root/name).write_text(json.dumps(g,ensure_ascii=False))
        inputs.append({'graph':name,'statement_id':next(n['id'] for n in g['nodes'] if n['type']=='Statement')})
    (root/'manifest.json').write_text(json.dumps({'inputs':inputs}))


class ConfirmedIdentityReaderTests(unittest.TestCase):
    def test_effective_internal_name_original_zero_lookup_get_and_aggregation(self):
        _,g,_=fixture.parse(fixture.doc(name=['SYNTHETIC FOUNDERS'],unp='0'))
        r=fixture.resolve(g)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r]);reader=Reader(root)
            effective=reader.search({'counterparty_internal_id':'internal-counterparty:1'})['items']
            self.assertEqual(len(effective),1);self.assertIsNone(effective[0]['effective_counterparty']['unp_raw'])
            self.assertEqual(len(reader.search({'counterparty_name':'SYNTHETIC CONFIRMED PERSON'})['items']),1)
            self.assertEqual(len(reader.search({'observed_counterparty_name':'SYNTHETIC FOUNDERS'})['items']),1)
            self.assertEqual(reader.search({'counterparty_unp':'0'})['items'],[])
            self.assertEqual(len(reader.search({"observed_counterparty_unp":"0"})["items"]),1)
            # Exact source zero is available even if normalized counterparty UNP is null.
            node=reader.get(fixture.op(r)['id'])['node'];self.assertEqual(node,fixture.op(r))
            self.assertEqual(node['properties']['modern_fields']['unp_raw'],'0')
            self.assertEqual(reader.aggregate(group_by='counterparty')['items'][0]['group'],'internal-counterparty:1')

    def test_archive_rejects_conflicting_number_and_stale_raw_even_if_processing_recomputed(self):
        _,g,_=fixture.parse(fixture.doc(unp='0'));r=fixture.resolve(g)
        other=fixture.variant(g,'other');conflict=fixture.resolve(other,fixture.answer(other,1,'private:distinct'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r,conflict])
            with self.assertRaises(QueryError):Reader(root)
            bad=copy.deepcopy(r);fixture.op(bad)['properties']['owner_counterparty_identity']['business_facts_sha256']='0'*64
            MODULES['statement_graph'].materialize_processing(bad);write(root,[bad])
            with self.assertRaises(QueryError):Reader(root)

    def test_own_payer_official_unp_search_and_get_are_separate_from_payee(self):
        g=role_fixture.graph();r=role_fixture.resolve(g);ident=fixture.op(r)['id']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r]);reader=Reader(root)
            self.assertEqual(len(reader.search({'payer_unp':'123456789'})['items']),1)
            self.assertEqual(reader.search({'payee_unp':'123456789'})['items'],[])
            got=reader.get(ident)
            self.assertEqual(got['effective_parties'][0]['official_unp'],'123456789')
            self.assertEqual(got['node'],fixture.op(r))
            self.assertEqual(reader.search({'counterparty_unp':'444555666'})['items'][0]['id'],ident)


    def test_corrected_working_unp_and_original_wrong_unp_have_distinct_search_paths(self):
        g=role_fixture.graph();r=role_fixture.resolve(g,role_fixture.correction_answer(g));ident=fixture.op(r)['id']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r]);reader=Reader(root)
            self.assertEqual(reader.search({'counterparty_unp':'123456789'})['items'][0]['id'],ident)
            self.assertEqual(reader.search({'counterparty_unp':'444555666'})['items'],[])
            self.assertEqual(reader.search({'observed_counterparty_unp':'444555666'})['items'][0]['id'],ident)
            self.assertEqual(reader.get(ident)['node']['properties']['counterparty']['unp_raw'],'444555666')
            self.assertEqual(reader.search({'payee_unp':'123456789'})['items'][0]['id'],ident)


    def test_pending_dispute_never_exposes_original_wrong_unp_as_working_ready(self):
        g=role_fixture.graph();r=role_fixture.resolve(g,role_fixture.pending_answer(g))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r]);reader=Reader(root)
            self.assertEqual(reader.search({'counterparty_unp':'444555666'})['items'],[])
            self.assertEqual(reader.search({'payee_unp':'444555666'})['items'],[])
            held=reader.search({'processing_status':'needs_clarification'})['items']
            self.assertEqual(len(held),1);self.assertIsNone(held[0]['effective_counterparty']['unp_raw'])
            self.assertEqual(held[0]['effective_counterparty']['resolution_status'],'pending')
            self.assertEqual(len(reader.search({'observed_counterparty_unp':'444555666'})['items']),1)


    def test_canonical_modern_account_without_observations_accepts_bound_correction(self):
        modern_fixture=MODULES['test_modern_identity_account'];g=modern_fixture.graph();r=modern_fixture.apply(g)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write(root,[r]);reader=Reader(root)
            self.assertEqual(len(reader.search({'counterparty_unp':'987654321'})['items']),1)
            self.assertEqual(len(reader.search({'observed_counterparty_unp':'123456789'})['items']),1)
            bad=copy.deepcopy(r);n=fixture.op(bad);account_id=n['properties']['owner_role_unps'][0]['correction']['account_id']
            next(x for x in bad['nodes'] if x['id']==account_id)['properties']['identifier']['raw']='999999999'
            MODULES['statement_graph'].materialize_processing(bad);write(root,[bad])
            with self.assertRaises(QueryError):Reader(root)

if __name__=='__main__':unittest.main()
