"""Invented canonical modern account evidence; never fabricate observations."""
import copy
import unittest
import counterparty_identity as ci
import statement_graph as sg
from test_modern_reviewed import doc,parse,op


def graph():return parse(doc())[1]


def answer(g):
    n=op(g);party=n['properties']['counterparty']['party_id'];cp=n['properties']['counterparty']
    account_id=next(e['to'] for e in g['edges'] if e['from']==n['id'] and e['type']=='TO_ACCOUNT')
    account=next(x for x in g['nodes'] if x['id']==account_id);number=account['properties']['number_raw']
    return {'operation_id':n['id'],'role':'payee','party_id':party,'official_unp':'987654321','original_unp_raw':'123456789',
            'original_detail_raw':next(o['raw'] for o in n['observations'] if o['field']=='identity_detail_raw'),
            'business_facts_sha256':ci.source_digest(g,n),'answered_by':'synthetic-reviewer','answered_at':'2026-10-13T10:00:00Z',
            'answer_ref':'private:synthetic-owner-dispute','confirmation_ref':'private:synthetic-review','origin':'owner_confirmed',
            'correction':{'kind':'owner_disputed_source','status':'confirmed','dispute_ref':'private:synthetic-owner-dispute',
                          'replacement_evidence_ref':'private:synthetic-account-name-proof','account_id':account_id,
                          'account_number_raw':number,'name_raw':cp['name_raw'],
                          'replacement_evidence':{'official_unp':'987654321','account_number_raw':number,'name_raw':cp['name_raw']}}}


def apply(g,a=None):
    a=answer(g) if a is None else a
    return ci.annotate_role_unps(g,{op(g)['id']:[a]},{op(g)['id']:[copy.deepcopy(a)]})


class ModernIdentityAccountTests(unittest.TestCase):
    def test_canonical_modern_empty_observations_use_typed_and_exact_row_raw(self):
        g=graph();before=copy.deepcopy(g);a=answer(g);account=next(n for n in g['nodes'] if n['id']==a['correction']['account_id'])
        self.assertEqual(account['observations'],[])
        self.assertTrue(ci.account_source_matches(g,op(g),'payee',account,a['correction']['account_number_raw']))
        r=apply(g,a);self.assertTrue(sg.validate(r)['valid'])
        self.assertEqual(op(r)['properties']['processing']['effective_counterparty']['unp_raw'],'987654321')
        clean=copy.deepcopy(r);op(clean)['properties'].pop(ci.ROLE_PROPERTY);op(clean)['properties']['processing']=op(g)['properties']['processing']
        self.assertEqual(clean,before);self.assertEqual(g,before)

    def test_typed_properties_identifier_and_observation_conflicts_fail(self):
        for field,value in [('number_raw','999999999'),('format','historical'),('identity_status','unknown'),
                            ('iban_raw','BY00FAKE'),('bic_raw','OTHERBY00'),('identifier',None)]:
            g=graph();a=answer(g);account=next(n for n in g['nodes'] if n['id']==a['correction']['account_id'])
            account['properties'][field]=value
            self.assertFalse(ci.account_source_matches(g,op(g),'payee',account,a['correction']['account_number_raw']),field)
        for field in ('number_raw','format','identity_status','iban_raw','bic_raw','identifier'):
            g=graph();a=answer(g);account=next(n for n in g['nodes'] if n['id']==a['correction']['account_id']);del account['properties'][field]
            self.assertFalse(ci.account_source_matches(g,op(g),'payee',account,a['correction']['account_number_raw']),field)
        for key,value in [('normalized','999999999'),('raw','BY00FAKE'),('value_check','failed')]:
            g=graph();a=answer(g);account=next(n for n in g['nodes'] if n['id']==a['correction']['account_id']);account['properties']['identifier'][key]=value
            self.assertFalse(ci.account_source_matches(g,op(g),'payee',account,a['correction']['account_number_raw']),key)

    def test_missing_duplicate_conflicting_raw_and_unproved_profile_fail(self):
        for mode in ('missing','duplicate','wrong_raw','unknown','unproven_profile','conflicting_observation'):
            g=graph();a=answer(g);n=op(g);account=next(x for x in g['nodes'] if x['id']==a['correction']['account_id'])
            source=next(o for o in n['observations'] if o['field']=='modern_account_raw')
            if mode=='missing':n['observations'].remove(source)
            if mode=='duplicate':n['observations'].append(copy.deepcopy(source))
            if mode=='wrong_raw':source['raw']='999999999'
            if mode=='unknown':source['status']='unknown'
            if mode=='unproven_profile':next(x for x in g['nodes'] if x['type']=='Statement')['observations']=[]
            if mode=='conflicting_observation':account['observations']=[{'field':'number_raw','raw':'999999999','normalized':'999999999','status':'observed'}]
            self.assertFalse(ci.account_source_matches(g,n,'payee',account,a['correction']['account_number_raw']),mode)
        g=graph();a=answer(g);account=next(x for x in g['nodes'] if x['id']==a['correction']['account_id'])
        self.assertFalse(ci.account_source_matches(g,op(g),'payer',account,a['correction']['account_number_raw']))

    def test_fake_source_or_account_role_annotation_and_nonidentity_holds_rejected(self):
        g=graph();base=answer(g)
        for field,value in [('role','payer'),('business_facts_sha256','0'*64),('original_detail_raw','forged')]:
            bad=copy.deepcopy(base);bad[field]=value
            with self.assertRaises(ValueError):apply(g,bad)
        bad=copy.deepcopy(base);bad['correction']['account_id']=next(n['id'] for n in g['nodes'] if n['type']=='Account' and n['id']!=base['correction']['account_id'])
        with self.assertRaises(ValueError):apply(g,bad)
        op(g)['properties']['duplicate_status']='candidate';sg.materialize_processing(g);r=apply(g)
        self.assertIn('duplicate_review_unresolved',op(r)['properties']['processing']['gaps'])

if __name__=='__main__':unittest.main()
