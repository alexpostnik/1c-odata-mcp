"""Invented per-role official answers never rewrite observed zeros."""
import copy
import unittest
import statement_graph as sg
import counterparty_identity as ci
from test_admission_regressions import parsed, diagnostic, identity_body
from test_counterparty_identity import op


def graph():
    body=identity_body('outflow');body[2]='УНП плательщика: 0 УНП получателя: 444555666 УНП за кого: '
    reviewed,_=parsed(body);g=diagnostic(reviewed);sg.materialize_processing(g);return g


def answer(g,role='payer',value='123456789'):
    n=op(g);party=next(e['to'] for e in g['edges'] if e['from']==n['id'] and e['type']==role.upper())
    return {'operation_id':n['id'],'role':role,'party_id':party,'official_unp':value,
            'original_unp_raw':'0' if role=='payer' else '444555666',
            'original_detail_raw':next(o['raw'] for o in n['observations'] if o['field']=='identity_detail_raw'),
            'business_facts_sha256':ci.source_digest(g,n),'origin':'owner_confirmed',
            'answered_by':'synthetic-owner','answered_at':'2026-10-13T09:00:00Z',
            'answer_ref':'private:synthetic-answer','confirmation_ref':'private:synthetic-review'}


def resolve(g,a=None):
    a=answer(g) if a is None else a
    return ci.annotate_role_unps(g,{op(g)['id']:[a]},{op(g)['id']:[copy.deepcopy(a)]})


def correction_answer(g):
    a=answer(g,'payee','123456789');n=op(g)
    account_id=next(e['to'] for e in g['edges'] if e['from']==n['id'] and e['type']=='TO_ACCOUNT')
    account=next(n for n in g['nodes'] if n['id']==account_id)
    name=n['properties']['counterparty']['name_raw'];number=account['properties']['number_raw']
    a['correction']={'kind':'owner_disputed_source','status':'confirmed','dispute_ref':'private:synthetic-owner-dispute',
                     'replacement_evidence_ref':'private:synthetic-independent-account-name-proof',
                     'account_id':account_id,'account_number_raw':number,'name_raw':name,
                     'replacement_evidence':{'official_unp':a['official_unp'],'account_number_raw':number,'name_raw':name}}
    return a


def pending_answer(g):
    a=correction_answer(g);a['official_unp']=None;a['correction']['status']='pending'
    a['correction']['replacement_evidence_ref']=None;a['correction']['replacement_evidence']=None
    return a


class OwnerRoleUnpTests(unittest.TestCase):
    def test_official_own_role_is_separate_and_all_originals_preserved(self):
        g=graph();before=copy.deepcopy(g);r=resolve(g);self.assertTrue(sg.validate(r)['valid'])
        self.assertEqual(ci.effective_parties(op(r)),[{'role':'payer','party_id':answer(g)['party_id'],'official_unp':'123456789','origin':'owner_confirmed'}])
        clean=copy.deepcopy(r);op(clean)['properties'].pop(ci.ROLE_PROPERTY)
        self.assertEqual(clean,before);self.assertEqual(g,before)
        self.assertEqual(op(r)['properties']['processing']['effective_counterparty']['unp_raw'],'444555666')
        import source_readiness as sr
        self.assertEqual(sr.fact_fingerprint(op(g)),sr.fact_fingerprint(op(r)))

    def test_exact_role_party_source_provenance_and_real_official_value_required(self):
        g=graph();base=answer(g)
        for key,value in [('role','payee'),('party_id',op(g)['properties']['counterparty']['party_id']),
                          ('operation_id','unknown-row'),('original_unp_raw',''),('original_detail_raw','forged'),
                          ('official_unp','0'),('official_unp','000000000'),('official_unp','internal-counterparty:1'),
                          ('business_facts_sha256','0'*64),('origin','source_observed'),
                          ('answered_at','2026-10-13T09:00:00'),('answer_ref','')]:
            a=dict(base);a[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):resolve(g,a)
        with self.assertRaises(ValueError):ci.annotate_role_unps(g,{op(g)['id']:[base]}, {})
        with self.assertRaises(ValueError):resolve(g,answer(g,'payee','123456789'))
        r=resolve(g);bad=copy.deepcopy(r);op(bad)['properties']['amount']='99'
        sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'])
        with self.assertRaises(ValueError):resolve(r,answer(r,value='987654321'))
        changed=answer(g,value='987654321')
        with self.assertRaises(ValueError):ci.annotate_role_unps(g,{op(g)['id']:[changed]},{op(g)['id']:[changed]},[base])
        self.assertTrue(sg.validate(resolve(g,answer(g,value='AB1234567')))['valid'])

    def test_duplicate_role_and_other_holds_cannot_be_suppressed(self):
        g=graph();op(g)['properties']['duplicate_status']='candidate';sg.materialize_processing(g);a=answer(g)
        r=resolve(g,a);self.assertIn('duplicate_review_unresolved',op(r)['properties']['processing']['gaps'])
        with self.assertRaises(ValueError):ci.annotate_role_unps(g,{op(g)['id']:[a,a]},{op(g)['id']:[a,a]})


    def test_owner_disputed_recognized_unp_corrected_separately_with_exact_account_name_proof(self):
        g=graph();before=copy.deepcopy(g);r=resolve(g,correction_answer(g));p=op(r)['properties']
        self.assertEqual(p['counterparty']['unp_raw'],'444555666')
        self.assertEqual(p['processing']['effective_counterparty']['unp_raw'],'123456789')
        self.assertEqual(p['processing']['effective_counterparty']['origin'],'owner_confirmed')
        self.assertEqual(p['processing']['effective_counterparty']['correction_kind'],'owner_disputed_source')
        self.assertEqual(ci.effective_parties(op(r))[0]['correction_kind'],'owner_disputed_source')
        self.assertEqual(ci.effective_parties(op(r))[0]['resolution_status'],'confirmed')
        clean=copy.deepcopy(r);op(clean)['properties'].pop(ci.ROLE_PROPERTY);op(clean)['properties']['processing']=op(g)['properties']['processing']
        self.assertEqual(clean,before);self.assertEqual(g,before)
        import source_readiness as sr
        self.assertEqual(sr.fact_fingerprint(op(g)),sr.fact_fingerprint(op(r)))

    def test_disputed_correction_requires_both_evidence_refs_and_exact_role_account_name_and_value(self):
        g=graph();base=correction_answer(g)
        for key,value in [('kind','ordinary_clarification'),('dispute_ref',''),('replacement_evidence_ref',''),
                          ('account_id','unknown-account'),('account_number_raw','999999999'),('name_raw','OTHER SYNTHETIC NAME')]:
            a=copy.deepcopy(base);a['correction'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):resolve(g,a)
        for key,value in [('official_unp','987654321'),('account_number_raw','999999999'),('name_raw','OTHER SYNTHETIC NAME')]:
            a=copy.deepcopy(base);a['correction']['replacement_evidence'][key]=value
            with self.subTest(evidence_key=key),self.assertRaises(ValueError):resolve(g,a)
        a=copy.deepcopy(base);a.pop('correction')
        with self.assertRaises(ValueError):resolve(g,a)
        with self.assertRaises(ValueError):ci.annotate_role_unps(g,{op(g)['id']:[base]}, {})
        a=copy.deepcopy(base);a['official_unp']='444555666';a['correction']['replacement_evidence']['official_unp']='444555666'
        with self.assertRaises(ValueError):resolve(g,a)
        a=correction_answer(g);a['role']='payer'
        with self.assertRaises(ValueError):resolve(g,a)

    def test_owner_dispute_preserves_other_holds_and_later_account_mutation_is_stale(self):
        g=graph();op(g)['properties']['duplicate_status']='candidate';sg.materialize_processing(g);r=resolve(g,correction_answer(g))
        self.assertIn('duplicate_review_unresolved',op(r)['properties']['processing']['gaps'])
        bad=copy.deepcopy(r);account_id=correction_answer(g)['correction']['account_id']
        next(n for n in bad['nodes'] if n['id']==account_id)['properties']['number_raw']='999999999'
        sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'])


    def test_pending_owner_dispute_holds_only_row_and_can_advance_with_full_proof(self):
        g=graph();r=resolve(g,pending_answer(g));p=op(r)['properties']
        self.assertTrue(sg.validate(r)['valid'])
        self.assertEqual(p['counterparty']['unp_raw'],'444555666')
        self.assertIsNone(p['processing']['effective_counterparty']['unp_raw'])
        self.assertEqual(p['processing']['effective_counterparty']['resolution_status'],'pending')
        self.assertIn('owner_disputed_official_unp_unresolved',p['processing']['gaps'])
        self.assertEqual(p['processing']['status'],'needs_clarification')
        self.assertIsNone(ci.effective_parties(op(r))[0]['official_unp'])
        confirmed=resolve(r,correction_answer(r))
        self.assertEqual(op(confirmed)['properties']['processing']['status'],'ready')
        self.assertEqual(op(confirmed)['properties']['processing']['effective_counterparty']['unp_raw'],'123456789')
        bad=copy.deepcopy(r);op(bad)['properties']['processing']['gaps']=[];op(bad)['properties']['processing']['status']='ready'
        self.assertFalse(sg.validate(bad)['valid'])

    def test_pending_cannot_claim_a_replacement_or_advance_under_another_dispute(self):
        g=graph();base=pending_answer(g)
        for key,value in [('official_unp','0'),('official_unp','123456789')]:
            a=copy.deepcopy(base);a[key]=value
            with self.assertRaises(ValueError):resolve(g,a)
        for key,value in [('replacement_evidence_ref','private:claimed-proof'),('status','confirmed'),('dispute_ref','')]:
            a=copy.deepcopy(base);a['correction'][key]=value
            with self.assertRaises(ValueError):resolve(g,a)
        r=resolve(g,base);wrong=correction_answer(r);wrong['correction']['dispute_ref']='private:other-dispute'
        with self.assertRaises(ValueError):resolve(r,wrong)

if __name__=='__main__':unittest.main()
