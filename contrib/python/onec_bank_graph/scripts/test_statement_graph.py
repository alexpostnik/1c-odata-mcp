import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from statement_graph import annotate_rko, build, check_continuity, check_conversion, combine, decimal, normalize_decimal, validate, write_new

FIXTURE=Path(__file__).resolve().parents[1]/'assets/synthetic-reviewed.json'

class BankingGraphTests(unittest.TestCase):
    def setUp(self):
        self.document=json.loads(FIXTURE.read_text())
        self.graph=build(self.document)
    def operation(self,graph=None):
        return next(n for n in (graph or self.graph)['nodes'] if n['type']=='Operation')
    def statement(self,graph=None):
        return next(n for n in (graph or self.graph)['nodes'] if n['type']=='Statement')
    def annotation(self, status='candidate'):
        return {'scope':'cash_revenue_rko','database':'test','status':status,
                'document':{'date':'2011-02-03','number':'SYN-000001','type':'Расходный кассовый ордер'},
                'observed_at':'2026-10-07T08:00:00Z','observed_on':'2026-10-07',
                'observed_precision':'exact_timestamp','snapshot_atomic':False,'posted':True,'deletion_mark':False}
    def test_rko_candidate_does_not_claim_completed_processing(self):
        annotation=self.annotation();saved=copy.deepcopy(self.graph)
        result=annotate_rko(self.graph,{self.operation()['id']:annotation})
        self.assertEqual(self.graph,saved)
        self.assertEqual(self.operation(result)['properties'].pop('one_c_rko')['status'],'candidate')
        self.assertEqual(result,saved)  # all bank nodes/edges/properties/raw money unchanged
    def test_rko_loaded_requires_exact_successful_post_and_readback(self):
        ident=self.operation()['id'];annotation=self.annotation('loaded_verified')
        evidence={ident:{'exact_source_target':True,'post_success':True,'readback_success':True,'same_test_claim':True}}
        for missing in [None,{}, {ident:{**evidence[ident],'readback_success':False}},
                        {ident:{**evidence[ident],'post_success':None}},
                        {ident:{**evidence[ident],'exact_source_target':False}},
                        {ident:{**evidence[ident],'same_test_claim':False}}]:
            with self.subTest(evidence=missing),self.assertRaises(ValueError):
                annotate_rko(self.graph,{ident:annotation},missing)
        result=annotate_rko(self.graph,{ident:annotation},evidence)
        self.assertEqual(self.operation(result)['properties']['one_c_rko']['status'],'loaded_verified')
    def test_rko_unknown_pending_partial_never_promote_observed_posted(self):
        for status in ['unknown','pending','partial']:
            annotation=self.annotation(status)
            result=annotate_rko(self.graph,{self.operation()['id']:annotation})
            self.assertEqual(self.operation(result)['properties']['one_c_rko']['status'],status)
        annotation=self.annotation('unknown');annotation.update(document=None,posted=None,deletion_mark=None)
        self.assertTrue(validate(annotate_rko(self.graph,{self.operation()['id']:annotation}))['valid'])
    def test_rko_annotation_rejects_private_payload_and_impossible_loaded_status(self):
        for change in [{'Ref_Key':'synthetic-key'},{'receipt':{}},{'personnel':'Synthetic person'},
                       {'database':'prod'},{'scope':'whole_bank_migration'},
                       {'posted':False,'status':'loaded_verified'},
                       {'deletion_mark':True,'status':'loaded_verified'},
                       {'document':None,'status':'loaded_verified'},
                       {'observed_at':'yesterday'},
                       {'document':{'date':'2011-02-30','number':'SYN-1','type':'Расходный кассовый ордер'}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                annotate_rko(self.graph,{self.operation()['id']:{**self.annotation(),**change}})
        with self.assertRaises(ValueError):annotate_rko(self.graph,{'missing':self.annotation()})
    def test_rko_conflicting_annotations_never_silently_merge(self):
        first=annotate_rko(self.graph,{self.operation()['id']:self.annotation()})
        second=annotate_rko(self.graph,{self.operation()['id']:self.annotation('pending')})
        with self.assertRaises(ValueError):combine([first,second])
    def test_rko_candidate_unknown_read_time_is_explicit_and_cannot_be_loaded(self):
        ident=self.operation()['id'];annotation=self.annotation()
        annotation.update(observed_at=None,observed_precision='unknown_time')
        result=annotate_rko(self.graph,{ident:annotation})
        self.assertIsNone(self.operation(result)['properties']['one_c_rko']['observed_at'])
        self.assertFalse(self.operation(result)['properties']['one_c_rko']['snapshot_atomic'])
        annotation['status']='loaded_verified'
        evidence={ident:{'exact_source_target':True,'post_success':True,'readback_success':True,'same_test_claim':True}}
        with self.assertRaises(ValueError):annotate_rko(self.graph,{ident:annotation},evidence)
        annotation=self.annotation();annotation['observed_on']='2026-10-06'
        with self.assertRaises(ValueError):annotate_rko(self.graph,{ident:annotation})
    def test_rko_cli_keeps_input_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'bank.json';annotations=root/'annotations.json';output=root/'annotated.json'
            write_new(source,self.graph);before=source.read_bytes()
            write_new(annotations,{self.operation()['id']:self.annotation()})
            command=[sys.executable,str(Path(__file__).with_name('statement_graph.py')),
                     'annotate-rko',str(source),'--annotations',str(annotations),'--output',str(output)]
            self.assertEqual(subprocess.run(command,capture_output=True,timeout=20).returncode,0)
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(self.operation(json.loads(output.read_text()))['properties']['one_c_rko']['status'],'candidate')
            self.assertEqual(subprocess.run(command,capture_output=True,timeout=20).returncode,2)
    def test_pilot_preserves_all_business_relations(self):
        self.assertTrue(validate(self.graph)['valid'])
        self.assertEqual(validate(self.graph)['operation_count'],2)
        self.assertEqual({e['type'] for e in self.graph['edges']},{'FOR_ACCOUNT','IN_CURRENCY','CONTAINS','OWNED_BY','SERVICED_BY','PAYEE','PAYER','MENTIONS','HAS_COMPONENT','HAS_TAX','HAS_CASH_SYMBOL'})
    def test_no_pdf_graph_or_value_links(self):
        payload=json.dumps(self.graph)
        for forbidden in ['SourceFile','SourceSpan','sha256','provenance','span_ids','page_count','synthetic-2010.pdf']:
            self.assertNotIn(forbidden,payload)
    def test_continued_purpose_and_unknown_fields_survive(self):
        row=self.document['statements'][0]['operations'][0]
        raw='Invoice SYN-1\ncontinued purpose\nwith unknown 007 code'
        previous=row['properties']['purpose_raw']
        row['properties']['purpose_raw']=raw
        # This positive represents a different synthetic source, not a property
        # substitution against retained evidence from the original fixture.
        detail=next(o for o in row['observations'] if o['field']=='identity_detail_raw')
        self.assertTrue(detail['raw'].endswith(previous))
        detail['raw']=detail['raw'][:-len(previous)]+raw
        self.document['entities']=[e for e in self.document['entities'] if e['alias']!='fee']
        self.document['relations']=[e for e in self.document['relations'] if e['to']!='fee']
        row['unrecognized_fields']=[{'label':None,'value':'007','status':'unknown'}]
        graph=build(self.document)
        self.assertTrue(validate(graph)['valid'])
        self.assertEqual(self.operation(graph)['properties']['purpose_raw'],raw)
        self.assertEqual(self.operation(graph)['unrecognized_fields'][0]['value'],'007')
    def test_alias_text_is_not_rewritten(self):
        self.document['statements'][0]['operations'][0]['properties']['purpose_raw']='account'
        self.assertEqual(self.operation(build(self.document))['properties']['purpose_raw'],'account')
    def test_exact_decimal_without_float_or_rounding(self):
        self.assertEqual(normalize_decimal('1 234,50'),'1234.50')
        self.assertEqual(decimal('9007199254740993.01'),Decimal('9007199254740993.01'))
        for bad in [1.1,'NaN','1e3','1,234.50','01.1']:
            with self.assertRaises(ValueError):
                decimal(bad) if not isinstance(bad,str) or ',' not in bad else normalize_decimal(bad)
    def test_schema_rejects_numeric_money_and_bad_dates(self):
        self.operation()['properties']['amount']=20.01
        self.assertFalse(validate(self.graph)['valid'])
        self.graph=build(self.document)
        self.operation()['properties']['value_date']='2010-02-30'
        self.assertFalse(validate(self.graph)['valid'])
    def test_blanks_and_unknown_currency_stay_available(self):
        self.operation()['properties']['printed_credit']=None
        next(o for o in self.operation()['observations'] if o['field']=='printed_credit').update(raw='',normalized=None,status='absent')
        currency=next(n for n in self.graph['nodes'] if n['type']=='CurrencyContext')
        currency['properties'].update(printed_currency='BYN',semantics_status='unknown',epoch=None,evidence_refs=[])
        self.assertTrue(validate(self.graph)['valid'])
        self.assertEqual(self.operation()['properties']['amount'],'20.01')
    def test_partial_rows_cannot_erase_proven_movement(self):
        self.statement()['properties'].update(rows_complete=False,expected_operations=99)
        self.operation()['properties'].update(row_status='partial',amount=None,direction='unknown')
        result=validate(self.graph)
        self.assertFalse(result['valid'])
        self.assertEqual(result['partial_statements'],1)
    def test_known_complete_count_mismatch_is_invalid(self):
        self.statement()['properties']['expected_operations']=3
        self.assertFalse(validate(self.graph)['valid'])
    def test_dangling_or_wrong_business_reference_is_invalid(self):
        self.statement()['properties']['account_id']='missing'
        self.assertFalse(validate(self.graph)['valid'])
        self.graph=build(self.document)
        self.graph['edges'][0]['to']=self.operation()['id']
        self.assertFalse(validate(self.graph)['valid'])
    def test_build_repeatability_and_no_auto_same_amount_dedup(self):
        self.assertEqual(build(self.document),build(copy.deepcopy(self.document)))
        rows=self.document['statements'][0]['operations'];rows.append(copy.deepcopy(rows[0]));rows[-1]['alias']='statement:1:row:3'
        self.document['relations'].append({'type':'PAYEE','from':rows[-1]['alias'],'to':rows[-1]['properties']['counterparty']['party_id']})
        self.document['statements'][0]['properties']['expected_operations']=3
        graph=build(self.document)
        self.assertEqual(validate(graph)['operation_count'],3)
        self.assertNotEqual([n for n in graph['nodes'] if n['type']=='Operation'][0]['id'],[n for n in graph['nodes'] if n['type']=='Operation'][2]['id'])
    def test_combine_only_explicit_identifiers(self):
        other=copy.deepcopy(self.document);other['dataset_id']='second-statement'
        merged=combine([self.graph,build(other)])
        self.assertEqual(sum(n['type']=='Account' for n in merged['nodes']),2)
        self.assertTrue(validate(merged)['valid'])
        self.assertEqual(combine([self.graph,self.graph]),combine([self.graph]))
        other_graph=copy.deepcopy(self.graph);self.operation(other_graph)['properties']['purpose_raw']='different'
        with self.assertRaises(ValueError):combine([self.graph,other_graph])
    def test_legacy_codes_and_blank_future_iban(self):
        account=next(n for n in self.graph['nodes'] if n['type']=='Account')
        account['properties']['number_raw']='0000123'
        self.statement()['properties']['account_number_raw']='0000123'
        self.assertTrue(validate(self.graph)['valid'])
        self.assertIsNone(account['properties']['iban_raw'])
        self.assertEqual(self.operation()['properties']['executor_code_raw'],'009')
    def test_empty_statement_omitted_totals_are_not_zero(self):
        d=copy.deepcopy(self.document);st=d['statements'][0];st['operations']=[]
        st['properties'].update(expected_operations=0,printed_debit_total=None,printed_credit_total=None,closing='100.00')
        d['relations']=[e for e in d['relations'] if ':row:' not in e['from']]
        g=build(d, diagnostic=True);self.assertTrue(validate(g)['valid']);self.assertEqual(validate(g)['operation_count'],0)
        self.assertEqual(validate(g)['processing_admission']['status'],'blocked')
        self.assertIsNone(self.statement(g)['properties']['printed_debit_total'])
    def test_reissue_exclusion_needs_business_decision(self):
        self.statement()['properties']['selection']='exclude_reissue'
        self.assertFalse(validate(self.graph)['valid'])
        self.statement()['properties']['selection_evidence']=['reviewed-logical-statement-copy']
        self.assertTrue(validate(self.graph)['valid'])
    def controls(self):
        return json.loads((FIXTURE.parent/'synthetic-controls.json').read_text())
    def test_statement_controls_pass_exactly(self):
        self.assertEqual(check_conversion(self.graph,self.controls())['status'],'pass')
    def test_single_statement_unknown_unit_keeps_draft_and_returns_review(self):
        currency=next(n for n in self.graph['nodes'] if n['type']=='CurrencyContext')
        currency['properties'].update(semantics_status='unknown',epoch=None,evidence_refs=[])
        before=copy.deepcopy(self.graph)
        checked=check_conversion(self.graph,self.controls())
        self.assertEqual(checked['status'],'needs_review')
        self.assertIn('money_unit_unknown',checked['statements'][0]['issues'])
        self.assertEqual(self.graph,before)
        with tempfile.TemporaryDirectory() as tmp:
            draft=Path(tmp)/'draft.json';draft.write_text(json.dumps(self.graph))
            controls=Path(tmp)/'controls.json';controls.write_text(json.dumps(self.controls()))
            output=Path(tmp)/'checks.json'
            run=subprocess.run([sys.executable,str(FIXTURE.parents[1]/'scripts/statement_graph.py'),'check',str(draft),'--controls',str(controls),'--output',str(output)],capture_output=True,text=True)
            self.assertEqual(run.returncode,1)
            self.assertEqual(json.loads(output.read_text())['status'],'needs_review')
            self.assertEqual(json.loads(draft.read_text()),before)
    def precision_graph_and_controls(self):
        graph=copy.deepcopy(self.graph);controls=self.controls()
        debit='0.00000000000000000001';credit='0.00000000000000000002';zero='0.00000000000000000000'
        for op in [n for n in graph['nodes'] if n['type']=='Operation']:
            if op['properties']['direction']=='outflow':op['properties'].update(amount=debit,printed_debit=debit,printed_credit=zero)
            else:op['properties'].update(amount=credit,printed_credit=credit,printed_debit=zero)
            for observation in op['observations']:
                if observation['field'] in ('printed_debit','printed_credit'):
                    value=op['properties'][observation['field']]
                    observation.update(raw=value,normalized=value,status='observed')
        values=dict(opening=zero,closing=debit,printed_debit_total=debit,printed_credit_total=credit)
        self.statement(graph)['properties'].update(values)
        controls['statements'][0].update(values,opening_raw=zero)
        return graph,controls
    def test_high_precision_zero_differences_use_fixed_notation(self):
        graph,controls=self.precision_graph_and_controls()
        checked=check_conversion(graph,controls)
        self.assertEqual(checked['status'],'pass')
        self.assertEqual(checked['statements'][0]['computed_debit'],'0.00000000000000000001')
        for difference in checked['statements'][0]['differences'].values():
            self.assertNotIn('E',difference)
            self.assertEqual(decimal(difference),Decimal(0))
        # Fixed output does not broaden the accepted monetary input syntax.
        with self.assertRaises(ValueError):decimal('0E-20')
    def test_high_precision_small_mismatch_is_failed_without_rounding(self):
        graph,controls=self.precision_graph_and_controls()
        self.statement(graph)['properties']['closing']='0.00000000000000000002'
        checked=check_conversion(graph,controls)
        self.assertEqual(checked['status'],'failed')
        self.assertEqual(checked['statements'][0]['differences']['closing_copy'],'0.00000000000000000001')
    def test_high_precision_continuity_zero_difference_does_not_raise(self):
        graph=self.continuity_graph()
        previous,current=[n for n in graph['nodes'] if n['type']=='Statement']
        previous['properties']['closing']='0.00000000000000000001'
        current['properties']['opening']='0.000000000000000000010'
        checked=check_continuity(graph)
        self.assertEqual(checked['status'],'pass')
        self.assertEqual(decimal(checked['pairs'][0]['opening_minus_previous_closing']),Decimal(0))
    def test_omitted_and_duplicated_row_fail_controls(self):
        omitted=copy.deepcopy(self.graph)
        op=self.operation(omitted);omitted['nodes']=[n for n in omitted['nodes'] if n['id']!=op['id']]
        omitted['edges']=[e for e in omitted['edges'] if e['from']!=op['id'] and e['to']!=op['id']]
        self.assertEqual(check_conversion(omitted,self.controls())['status'],'failed')
        d=copy.deepcopy(self.document);duplicate=copy.deepcopy(d['statements'][0]['operations'][0]);duplicate['alias']='statement:1:row:3'
        d['statements'][0]['operations'].append(duplicate);d['statements'][0]['properties']['expected_operations']=3
        d['relations'].append({'type':'PAYEE','from':duplicate['alias'],'to':duplicate['properties']['counterparty']['party_id']})
        self.assertEqual(check_conversion(build(d),self.controls())['status'],'failed')
    def test_blank_first_opening_zero_is_source_semantics(self):
        d=copy.deepcopy(self.document);st=d['statements'][0];st['properties'].update(opening=None,opening_origin='unknown',first_account_evidence=['independent synthetic account-start evidence'])
        st['observations']=[{'field':'opening','raw':'','normalized':None,'status':'observed'}]+[o for o in st['observations'] if o['field']=='counterparty_field_type']
        graph=build(d);self.assertEqual(self.statement(graph)['properties']['opening'],'0')
        self.assertEqual(self.statement(graph)['observations'][0]['raw'],'')
        c=self.controls();c['statements'][0].update(opening=None,opening_raw='')
        self.assertEqual(check_conversion(graph,c)['status'],'pass')
        d['statements'][0]['properties']['first_statement_marker']=None
        d['statements'][0]['properties']['first_account_evidence']=[]
        graph=build(d);self.assertIsNone(self.statement(graph)['properties']['opening'])
        c['statements'][0].update(first_for_account=False,first_account_basis=None)
        self.assertEqual(check_conversion(graph,c)['status'],'needs_review')
    def test_zero_rule_not_inferred_from_earliest_archive_or_year(self):
        self.statement()['properties']['opening_origin']='first_statement_blank_zero'
        self.statement()['properties']['first_statement_marker']=None
        self.assertFalse(validate(self.graph)['valid'])
    def continuity_graph(self):
        d=copy.deepcopy(self.document);st=d['statements'][0]
        st['properties'].update(period_end='2010-06-30')
        second=copy.deepcopy(st);second['alias']='statement:2';second['properties'].update(period_start='2010-07-01',period_end='2010-12-31',opening='10.010',first_statement_marker=None)

        for i, row in enumerate(second['operations'],1):
            row['alias']='statement:2:row:'+str(i);row['properties']['transaction_date']='2010-08-01'
            d['relations'].append({'type':row['properties']['counterparty']['role'].upper(),'from':row['alias'],'to':row['properties']['counterparty']['party_id']})
        d['statements'].append(second);return build(d)
    def test_continuity_exact_account_boundary(self):
        graph=self.continuity_graph();self.assertEqual(check_continuity(graph)['status'],'pass')
        statements=[n for n in graph['nodes'] if n['type']=='Statement'];statements[1]['properties']['opening']='10.02'
        self.assertEqual(check_continuity(graph)['status'],'failed')
    def test_gaps_overlaps_and_unknown_units_do_not_false_fail_or_pass(self):
        for start in ['2010-08-01','2010-06-01']:
            g=self.continuity_graph();[n for n in g['nodes'] if n['type']=='Statement'][1]['properties']['period_start']=start
            self.assertEqual(check_continuity(g)['status'],'needs_review')
        g=self.continuity_graph();next(n for n in g['nodes'] if n['type']=='CurrencyContext')['properties']['semantics_status']='unknown'
        self.assertEqual(check_continuity(g)['status'],'needs_review')
    def test_copied_control_mismatch_and_unknown_are_distinct(self):
        self.statement()['properties']['closing']='10.02'
        self.assertEqual(check_conversion(self.graph,self.controls())['status'],'failed')
        self.graph=build(self.document);self.statement()['properties']['closing']=None
        self.assertEqual(check_conversion(self.graph,self.controls())['status'],'needs_review')
    def test_partial_graph_kept_as_draft_despite_matching_totals(self):
        self.statement()['properties']['rows_complete']=False
        self.assertTrue(validate(self.graph)['valid'])
        self.assertEqual(check_conversion(self.graph,self.controls())['status'],'needs_review')
    def test_blank_nonmonetary_side_and_included_fee_not_double_counted(self):
        self.operation()['properties']['printed_credit']=None
        next(o for o in self.operation()['observations'] if o['field']=='printed_credit').update(raw='',normalized=None,status='absent')
        checked=check_conversion(self.graph,self.controls())
        self.assertEqual(checked['status'],'pass')
        self.assertEqual(checked['statements'][0]['computed_debit'],'20.01')

    def test_output_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'graph.json';write_new(path,self.graph)
            with self.assertRaises(FileExistsError):write_new(path,self.graph)
            self.assertEqual(json.loads(path.read_text()),self.graph)

if __name__=='__main__':unittest.main()
