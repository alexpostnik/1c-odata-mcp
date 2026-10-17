"""Synthetic exact-claim regressions; no original bank data or live services."""
import copy
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
import archive_adapter as a
import statement_graph as sg
from test_archive_adapter import document, row
from test_admission_regressions import identity_body, parsed


def operation(g):
    return next(n for n in g['nodes'] if n['type'] == 'Operation')


def parse_doc(doc):
    st, entities, relations, qa = a.parse_statement(doc, 1, 'synthetic')
    return {'dataset_id':'synthetic','statements':[st],'entities':entities,'relations':relations}, qa


class FinalClaimTests(unittest.TestCase):
    def test_SeWZ_invalid_date_status_preserves_movement_and_independent_row(self):
        for direction in ('inflow', 'outflow'):
            for date in ('01/01/2010', '31/02/2010'):
                body = identity_body(direction)
                body[0] = body[0].replace('01/01/2010', date)
                reviewed, qa = parsed(body + identity_body(number='2'))
                g = sg.build(reviewed); ops = [n for n in g['nodes'] if n['type']=='Operation']
                observation = next(o for o in ops[0]['observations'] if o['field']=='transaction_date')
                valid_date = date == '01/01/2010'
                self.assertEqual(observation['status'], 'observed' if valid_date else 'unknown')
                self.assertEqual(observation['normalized'], '2010-01-01' if valid_date else None)
                self.assertIn(date, observation['raw'])
                self.assertEqual(ops[0]['properties']['processing']['status'], 'ready' if valid_date else 'needs_clarification')
                self.assertEqual(ops[0]['properties']['direction'], direction)
                self.assertEqual(ops[0]['properties']['amount'], '3' if direction=='inflow' else '2')
                self.assertEqual(ops[1]['properties']['processing']['status'], 'ready')
                self.assertTrue(sg.validate(g)['valid'])

    def test_SeWb_negative_fee_never_becomes_positive_withholding(self):
        for direction in ('inflow', 'outflow'):
            for label in ('комиссия', 'комиссия банка', 'Удержана комиссия', 'комиссию'):
                for token in ('-15', ': -15', '15', '- 15'):
                    purpose = 'Эквайринг; '+label+' '+token
                    body = identity_body(direction); body[0] = row('1', credit='985' if direction=='inflow' else '', debit='985' if direction=='outflow' else '');body[-1]=purpose
                    reviewed, _ = parsed(body);g=sg.build(reviewed);o=operation(g)['properties']
                    fee = next(n['properties'] for n in g['nodes'] if n['type']=='AmountComponent')
                    negative = token in ('-15', ': -15')
                    self.assertEqual(fee['value'], '-15' if negative else '15')
                    self.assertEqual(fee['inclusion'], 'included' if direction=='inflow' and not negative else 'unknown')
                    self.assertEqual(o['settlement']['status'], 'unknown' if direction=='inflow' and negative else 'verified_withheld_fee' if direction=='inflow' else 'not_applicable')
                    self.assertEqual((o['amount'],o['direction'],o['purpose_raw'],o['payment1c_comment_raw']),('985',direction,purpose,purpose))
                    self.assertTrue(sg.validate(g)['valid'])

    def test_SeWd_combine_rechecks_merged_source_without_mutating_inputs(self):
        reviewed, _ = parsed(identity_body());g1=sg.build(reviewed);g2=copy.deepcopy(g1)
        next(o for o in operation(g2)['observations'] if o['field']=='identity_detail_raw')['raw'] += ' '
        inputs = copy.deepcopy([g1,g2])
        self.assertTrue(all(sg.validate(g)['valid'] for g in inputs))
        with self.assertRaises(ValueError):sg.combine([g1,g2])
        self.assertEqual([g1,g2],inputs)
        self.assertTrue(sg.validate(sg.combine([g1,g1]))['valid'])
        r2=copy.deepcopy(reviewed);r2['dataset_id']='synthetic-other';other=sg.build(r2)
        combined=sg.combine([g1,other])
        self.assertTrue(sg.validate(combined)['valid'])
        self.assertEqual(sum(n['type']=='Operation' for n in combined['nodes']),2)

    def test_SeWh_actual_geometry_deduplicates_only_exact_overlaid_glyphs(self):
        for direction in ('inflow','outflow'):
            for token, overlay in (('5.00', None), ('5.00','5'), ('5.00','6'), ('55.00',None)):
                body=identity_body(direction);body[0]=row('1',credit=token if direction=='inflow' else '',debit=token if direction=='outflow' else '')
                original=document(body,debit_total=token if direction=='outflow' else '0.00',credit_total=token if direction=='inflow' else '0.00',closing=token)
                chars=[]
                for i,item in enumerate([l for p in original['pages'] for l in p['lines']]):
                    for n,ch in enumerate(item['raw_text']):
                        c={'text':ch,'size':7,'width':4.176,'x0':49.68+4.2*n,'x1':49.68+4.2*n+4.176,'top':20+i*10,'fontname':'synthetic'};chars.append(c)
                        if overlay and i==10 and n==item['raw_text'].index(token):
                            chars.append(dict(c,text=overlay))
                reader=mock.MagicMock();reader.__enter__.return_value.pages=[SimpleNamespace(chars=chars,page_number=1)]
                with mock.patch.dict(sys.modules, {'pdfplumber':SimpleNamespace(open=lambda _:reader)}):geom=a.geometry('synthetic-only')
                reviewed, qa=parse_doc(geom);g=sg.build(reviewed);o=operation(g)['properties'];l=geom['pages'][0]['lines'][10]
                if overlay=='6':
                    self.assertIsNone(o['amount']);self.assertEqual(o['direction'],'unknown')
                    self.assertEqual(o['processing']['status'],'needs_clarification')
                else:
                    self.assertEqual((o['amount'],o['direction']),(token,direction))
                    self.assertEqual(o['processing']['status'],'ready')
                    field='printed_credit' if direction=='inflow' else 'printed_debit'
                    start,end=a.COLUMNS[field]
                    self.assertEqual(a.field_slice(l,start,end).strip(),token)
                    if overlay:
                        self.assertEqual(sum(glyph['text']=='5' and start <= glyph['cell'] < end for glyph in l['glyphs']),2)
                        self.assertIn('55.00',l['raw_text'])
                self.assertTrue(sg.validate(g)['valid'])

    def test_Tl8T_service_price_retained_without_settlement_inference(self):
        for direction in ('inflow','outflow'):
            for text, value in [('комиссия банка за ведение счета 15 BYR','15'),('комиссия по договору SYN 15',None),('комиссия по договору 123 15 BYR',None),('комиссия банка 15 BYR','15'),('комиссия за ведение счета',None)]:
                body=identity_body(direction);body[-1]='Эквайринг; '+text
                reviewed,_=parsed(body);g=sg.build(reviewed)
                components=[n['properties'] for n in g['nodes'] if n['type']=='AmountComponent']
                if text=='комиссия за ведение счета':self.assertEqual(components,[])
                else:
                    self.assertEqual(len(components),1)
                    self.assertEqual(components[0]['value'],value)
                    node=next(n for n in g['nodes'] if n['type']=='AmountComponent')
                    self.assertIn(text.split(' BYR')[0],next(o['raw'] for o in node['observations'] if o['field']=='mention_raw'))
                    if value=='15' and text.endswith('BYR'):self.assertEqual(components[0]['currency_raw'],'BYR')
                    if 'за ' in text or 'по ' in text:self.assertEqual(components[0]['inclusion'],'unknown')
                self.assertEqual(operation(g)['properties']['purpose_raw'],body[-1])
                self.assertTrue(sg.validate(g)['valid'])

    def test_Tl8X_excluded_partial_keeps_review_without_reincluding_money(self):
        for name in ('???','SYNTHETIC COUNTERPARTY'):
            reviewed,_=parsed(identity_body(name=name));p=reviewed['statements'][0]['properties']
            p.update(selection='exclude_reissue',selection_evidence=['synthetic-reviewed-copy'])
            g=sg.build(reviewed);result=sg.check_conversion(g,{'statements':[]})
            partial=name=='???'
            self.assertTrue(sg.validate(g)['valid'])
            self.assertEqual(result['status'],'needs_review' if partial else 'pass')
            self.assertEqual(result['statements'][0]['status'],'needs_review' if partial else 'excluded_reviewed_copy')
            self.assertEqual(result['statements'][0]['issues'],['row_clarification_required'] if partial else [])
            self.assertNotIn('computed_credit',result['statements'][0])
            self.assertEqual(operation(g)['properties']['processing']['status'],'needs_clarification' if partial else 'ready')

    def test_Tl8a_bank_code_status_and_validation(self):
        for code,status in [('12?','unknown'),('123456789','observed'),('','absent')]:
            body=identity_body();body[0]=body[0][:15]+f'{code:<10}'+body[0][25:]
            reviewed,_=parsed(body);g=sg.build(reviewed)
            account=next(e['to'] for e in g['edges'] if e['from']==operation(g)['id'] and e['type']=='FROM_ACCOUNT')
            bank_id=next(e['to'] for e in g['edges'] if e['from']==account and e['type']=='SERVICED_BY')
            bank=next(n for n in g['nodes'] if n['id']==bank_id)
            observation=next(o for o in bank['observations'] if o['field']=='code_raw')
            self.assertEqual(observation['status'],status)
            self.assertEqual(observation['raw'].strip(),code)
            self.assertEqual(observation['normalized'],code if status=='observed' else None)
            self.assertEqual(operation(g)['properties']['processing']['status'],'ready')
            self.assertTrue(sg.validate(g)['valid'])
            if status=='unknown':
                observation['status']='observed'
                self.assertFalse(sg.validate(g)['valid'])
                import bank_identifiers
                bank['properties'].update(code_system_raw='BIC',code_raw='SYNTHBIC',identifier=bank_identifiers.recognize('SYNTHBIC','bank'))
                observation.update(raw='SYNTHBIC',normalized='SYNTHBIC',status='observed')
                self.assertTrue(sg.validate(g)['valid'])

    def test_Tl8f_proven_turnover_boundary_survives_unreadable_amounts(self):
        for debit,credit in [('',''),('damaged','BROKEN'),('0.00','3.00')]:
            doc=document(identity_body(),debit_total=debit,credit_total=credit,closing='3.00')
            reviewed,qa=parse_doc(doc);g=sg.build(reviewed);st=reviewed['statements'][0]
            self.assertEqual(len(st['operations']),1)
            self.assertEqual(operation(g)['properties']['purpose_raw'],'Exact purpose')
            self.assertEqual(operation(g)['properties']['payment1c_comment_raw'],'Exact purpose')
            self.assertIsNotNone(a.footer_contract(doc)['turnover'])
            self.assertIn('ОБОРОТЫ',qa['footer_raw'])
            self.assertEqual(st['properties']['printed_credit_total'],'3.00' if credit=='3.00' else None)
            self.assertEqual(operation(g)['properties']['amount'],'3')
            self.assertTrue(sg.validate(g)['valid'])


if __name__=='__main__':unittest.main()
