"""Synthetic confirmed identities preserve evidence and independent holds."""
import copy
import unittest
import statement_graph as sg
import counterparty_identity as ci
from test_modern_reviewed import doc, parse, op


def answer(g, number=1, entity='private:synthetic-entity-1', name='SYNTHETIC CONFIRMED PERSON'):
    n = op(g)
    return {'operation_id':n['id'], 'role':n['properties']['counterparty']['role'],
            'internal_id':'internal-counterparty:'+str(number), 'confirmed_entity_ref':entity,
            'confirmed_name':name, 'business_relation':'statutory_capital_contribution',
            'origin':'owner_confirmed', 'business_facts_sha256':ci.source_digest(g,n),
            'original_detail_raw':next(o['raw'] for o in n['observations'] if o['field']=='identity_detail_raw'),
            'answered_by':'synthetic-owner','answered_at':'2026-10-13T09:00:00Z',
            'answer_ref':'private:synthetic-answer','confirmation_ref':'private:synthetic-review'}


def resolve(g, a=None, registry=()):
    a = answer(g) if a is None else a
    return ci.annotate(g,{op(g)['id']:a},{op(g)['id']:copy.deepcopy(a)},registry)


def variant(g, label):
    old='dataset:synthetic-modern:'
    def walk(value):
        if isinstance(value, str):return value.replace(old,'dataset:'+label+':')
        if isinstance(value, dict):return {k:walk(v) for k,v in value.items()}
        if isinstance(value, list):return list(map(walk,value))
        return value
    return walk(g)


class CounterpartyIdentityTests(unittest.TestCase):
    def test_zero_source_preserved_effective_unknown_unp_and_additive_owner_link(self):
        _,g,_=parse(doc(name=['SYNTHETIC FOUNDERS'],unp='0'))
        original=copy.deepcopy(g);r=resolve(g);p=op(r)['properties']
        self.assertEqual(p['processing']['status'],'ready')
        effective=p['processing']['effective_counterparty']
        self.assertIsNone(effective['unp_raw']);self.assertEqual(effective['internal_id'],'internal-counterparty:1')
        self.assertEqual(effective['name_raw'],'SYNTHETIC FOUNDERS')
        self.assertEqual(effective['confirmed_name'],'SYNTHETIC CONFIRMED PERSON')
        self.assertEqual(effective['business_relation'],'statutory_capital_contribution')
        self.assertEqual(r['edges'],original['edges'])
        for old,new in zip(original['nodes'],r['nodes']):
            clean=copy.deepcopy(new)
            if new['type']=='Operation':
                clean['properties'].pop(ci.PROPERTY);clean['properties']['processing']=old['properties']['processing']
            self.assertEqual(clean,old)
        self.assertEqual(g,original);self.assertTrue(sg.validate(r)['valid'])
        import source_readiness as sr
        self.assertEqual(sr.fact_fingerprint(op(g)),sr.fact_fingerprint(op(r)))
        self.assertIsNone(op(g)['properties']['processing']['effective_counterparty']['unp_raw'])

    def test_same_confirmed_entity_same_number_across_distinct_rows(self):
        _,g,_=parse(doc(unp='0'));r=resolve(g)
        other=variant(g,'other');r2=resolve(other,answer(other),[op(r)['properties'][ci.PROPERTY]])
        combined=sg.combine([r,r2]);self.assertTrue(sg.validate(combined)['valid'])
        self.assertEqual({n['properties']['processing']['effective_counterparty']['internal_id'] for n in combined['nodes'] if n['type']=='Operation'}, {'internal-counterparty:1'})

    def test_same_name_or_amount_does_not_group_and_registry_conflicts_fail(self):
        _,g,_=parse(doc(unp='0'));r=resolve(g);registry=[op(r)['properties'][ci.PROPERTY]]
        other=variant(g,'other')
        distinct=answer(other,2,'private:synthetic-distinct')
        self.assertTrue(sg.validate(resolve(other,distinct,registry))['valid'])
        for a in (answer(other,1,'private:synthetic-distinct'),answer(other,2),answer(other,name='OTHER SYNTHETIC NAME')):
            with self.assertRaises(ValueError):resolve(other,a,registry)
        r2=resolve(other,answer(other,1,'private:synthetic-distinct'))
        with self.assertRaises(ValueError):sg.combine([r,r2])

    def test_stale_forged_wrong_role_raw_id_origin_zero_and_fake_official_fail(self):
        _,g,_=parse(doc(unp='0'));base=answer(g)
        for key,value in [('operation_id','other-row'),('role','payer'),('original_detail_raw','forged raw'),
                          ('business_facts_sha256','0'*64),('internal_id','0'),('internal_id','123456789'),
                          ('internal_id','internal-counterparty:0'),('answered_at','2026-10-13T09:00:00'),
                          ('confirmation_ref',''),('origin','source_observed')]:
            bad=dict(base);bad[key]=value
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):resolve(g,bad)
        fake=dict(base);fake['unp_raw']='123456789'
        with self.assertRaises(ValueError):resolve(g,fake)
        with self.assertRaises(ValueError):ci.annotate(g,{op(g)['id']:base},{})
        verified=copy.deepcopy(base);verified['confirmed_entity_ref']='private:other'
        with self.assertRaises(ValueError):ci.annotate(g,{op(g)['id']:base},{op(g)['id']:verified})
        r=resolve(g);bad=copy.deepcopy(r)
        op(bad)['properties']['purpose_raw']='forged purpose'
        sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'])
        bad=copy.deepcopy(r);op(bad)['observations'][0]['raw']='forged source'
        sg.materialize_processing(bad);self.assertFalse(sg.validate(bad)['valid'])
        with self.assertRaises(ValueError):resolve(r,answer(r,2))

    def test_real_alpha_unp_damage_and_unknown_names_do_not_get_internal_override(self):
        for value in ('123456789','AB1234567','???'):
            _,g,_=parse(doc(unp=value))
            with self.subTest(value=value), self.assertRaises(ValueError):resolve(g)
        _,g,_=parse(doc(name=['SYNTHETIC [unknown]'],unp='0'))
        with self.assertRaises(ValueError):resolve(g)

    def test_blank_optional_and_all_nonidentity_holds_remain(self):
        _,g,_=parse(doc(unp=''));self.assertTrue(sg.validate(resolve(g))['valid'])
        for field,value,gap in [('duplicate_status','candidate','duplicate_review_unresolved'),
                               ('row_status','partial','row_fields_or_boundary_unresolved'),
                               ('purpose_status','unknown','purpose_boundary_unresolved')]:
            _,g,_=parse(doc(unp='0'));op(g)['properties'][field]=value
            if field=='purpose_status':op(g)['properties']['payment1c_text']=sg.prepare_payment_text(op(g)['properties']['purpose_raw'],'unknown')
            sg.materialize_processing(g);r=resolve(g)
            self.assertEqual(op(r)['properties']['processing']['status'],'needs_clarification')
            self.assertIn(gap,op(r)['properties']['processing']['gaps'])
            bad=copy.deepcopy(r);op(bad)['properties']['processing']['gaps']=[];op(bad)['properties']['processing']['status']='ready'
            self.assertFalse(sg.validate(bad)['valid'])

if __name__=='__main__':unittest.main()
