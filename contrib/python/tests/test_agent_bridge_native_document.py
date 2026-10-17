"""Synthetic Document addressing contracts; no source rows, credentials or COM."""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import unicodedata
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/agent_bridge'))
from agent_bridge import source, server
from mcp.server.mcpserver.exceptions import ToolError

TOKEN = 'private-synthetic-document-caf\u00e9'
NS = 'sha256:' + 'a' * 64
ACQUIRE = dict(source_alias='synthetic77', document_type='SyntheticHire', document_metadata_id='12',
               from_date='2000-01-01', to_exclusive='2001-01-01', document_cursor='0')
READ = dict(source_alias='synthetic77', document_type='SyntheticHire', document_metadata_id='12',
            native_reference=TOKEN, source_namespace=NS, attribute_names=['Employee', 'Category'],
            table_attribute_names=['Hours'], line_cursor='0', limit=100)


def response(p):
    token = p.get('nativeReference', TOKEN)
    row = dict(valueKind='document', selected=True, nativeReference=token,
               documentType=p['documentType'], documentMetadataId=p['documentMetadataId'],
               date='2000-01-02', number='synthetic-001', posted=False)
    d = dict(kind='document_identity' if p['operation'] == 'acquire_document_identity' else 'document_by_native',
             metadataKind='document', documentType=p['documentType'], observedDocumentType=p['documentType'],
             documentMetadataId=p['documentMetadataId'], nativeReference=token,
             sourceNamespace=p.get('sourceNamespace', NS), roundtripStatus='matched-typed-selected',
             stableIdentity='unverified', snapshotStatus='none', identityScope=source._DOCUMENT_IDENTITY_SCOPE,
             historyLinkStatus='unsupported-hold', historyLinkReason='periodic-document-native-transition-unimplemented')
    if p['operation'] == 'acquire_document_identity':
        d.update(seedCursor=p['documentCursor'], period=p['period'], resolvedNativeReference=token,
                 seed=copy.deepcopy(row), resolved=copy.deepcopy(row))
    else:
        value = dict(valueKind='reference', selected=True, empty=False, nativeReference='private-synthetic-employee',
                     refTypeId='13', nativeIdentityStatus='unverified')
        relation = dict(status='native-typed-unverified', metadataKind='reference', catalogType='SyntheticEmployee',
                        observedCatalogType='SyntheticEmployee', catalogMetadataId='13',
                        nativeReference=value['nativeReference'], sourceNamespace=d['sourceNamespace'])
        d.update(document=row, attributes={k: (copy.deepcopy(value) if k == 'Employee' else 1) for k in p['attributeNames']},
                 relations={'Employee': relation} if 'Employee' in p['attributeNames'] else {},
                 lines=[dict(lineNumber=int(p['lineCursor']) + 1, sourceLineNumber=7, sourceLineNumberStatus='observed',
                             attributes={k: 8 for k in p['tableAttributeNames']}, relations={})],
                 lineCursor=p['lineCursor'], lineNumberSemantics='ordinal-traversal', nextLineCursor=None, hasMore=False,
                 declaredLineCount=int(p['lineCursor']) + 1, unsupported=[], readStatus='complete-unverified')
    return dict(schemaVersion='1.0', operation=p['operation'], status='ok', requestId=p['requestId'],
                sourceAlias=p['sourceAlias'], data=d)


class Transport:
    def __init__(self, mutate=None): self.calls = []; self.mutate = mutate
    def exchange(self, c, p, timeout):
        self.calls.append(copy.deepcopy(p)); r = response(p)
        if self.mutate: self.mutate(r)
        return r


class DocumentTests(unittest.TestCase):
    def acquire(self, t, **changes):
        return source.request({}, 'acquire_document_identity', transport=t, **(ACQUIRE | changes))
    def read(self, t, **changes):
        return source.request({}, 'read_document_by_native', transport=t, **(READ | changes))

    def test_bounded_acquisition_then_saved_token_repeated_addressing(self):
        t = Transport(); a = self.acquire(t)['data']
        for _ in range(2):
            d = self.read(t, native_reference=a['nativeReference'], source_namespace=a['sourceNamespace'])['data']
            self.assertEqual(d['document'], a['resolved'])
            self.assertEqual(d['readStatus'], 'complete-unverified')
            self.assertEqual(d['historyLinkStatus'], 'unsupported-hold')
        self.assertEqual(len(t.calls), 3)
        self.assertNotIn('nativeReference', t.calls[0]); self.assertEqual(t.calls[0]['metadataKind'], 'document')
        for p in t.calls[1:]:
            self.assertNotIn('period', p); self.assertNotIn('documentCursor', p)
            self.assertEqual(p['nativeReference'], TOKEN)

    def test_invalid_inputs_reject_before_transport(self):
        changes = [dict(native_reference=x) for x in (None,'',' ', 'a\0b','a\u2028b','a\ud800b','x'*4097)]
        changes += [dict(source_namespace=x) for x in (None,'', 'sha256:A'+'a'*63, 'synthetic77')]
        changes += [dict(document_metadata_id=x) for x in (None,0,'0','12x','9'*11)]
        changes += [dict(attribute_names=x) for x in (None,'Employee',['Invoke()'],['x.y'],['Employee','Employee'],['x']*33)]
        changes += [dict(table_attribute_names=None), dict(table_attribute_names=['x','x']),
                    dict(document_type='Catalog.X'),dict(dataset_kind='reference'),dict(dataset_type='SyntheticEmployee'),
                    dict(cursor='0'),dict(document_cursor='0'),dict(from_date='2000-01-01'),
                    dict(line_cursor=None),dict(line_cursor='10001'),dict(line_cursor='0;Write()'),dict(limit=True),dict(limit=101)]
        for change in changes:
            with self.subTest(change=change):
                t=Transport()
                with self.assertRaises(ValueError): self.read(t, **change)
                self.assertEqual(t.calls,[])
        for change in (dict(document_cursor=None),dict(document_cursor='101'),dict(native_reference=TOKEN),
                       dict(source_namespace=NS),dict(from_date='2000-1-01'),dict(to_exclusive='1999-01-01'),
                       dict(line_cursor='0'),dict(attribute_names=[])):
            with self.subTest(change=change):
                t=Transport()
                with self.assertRaises(ValueError):self.acquire(t,**change)
                self.assertEqual(t.calls,[])

    def test_expression_looking_token_is_opaque_data(self):
        token='\" ); ExecuteBatch(\"private-canary\"); //\n'
        d=self.read(Transport(),native_reference=token)['data']
        self.assertEqual(d['nativeReference'],token)

    def test_kind_scope_metadata_selected_and_ordinal_proof_fail_closed(self):
        mutations=[lambda r:r.update(sourceAlias='foreign77'),
                   *[lambda r,k=k,v=v:r['data'].update({k:v}) for k,v in (
                       ('metadataKind','reference'),('kind','reference_by_native'),('documentType','Other'),
                       ('observedDocumentType','Other'),('documentMetadataId','13'),('sourceNamespace','sha256:'+'b'*64),
                       ('nativeReference',TOKEN+'\0'),('nativeReference',unicodedata.normalize('NFD', TOKEN)),
                       ('roundtripStatus','scalar-only'),('stableIdentity','verified'),('snapshotStatus','frozen'),
                       ('historyLinkStatus','verified'),('historyLinkReason',''))],
                   *[lambda r,k=k,v=v:r['data']['document'].update({k:v}) for k,v in (
                       ('selected',False),('selected',1),('valueKind','reference'),('documentMetadataId','13'),
                       ('documentType','Other'),('nativeReference','different'),('date','2000-02-30'))]]
        for mutate in mutations:
            with self.subTest(mutate=mutate),self.assertRaises(source.SourceError):self.read(Transport(mutate))
        for mutate in (lambda r:r['data']['seed'].update(number='different'),
                       lambda r:r['data']['resolved'].update(selected=False),lambda r:r['data'].update(seedCursor='1'),
                       lambda r:r['data'].update(resolvedNativeReference='different')):
            with self.assertRaises(source.SourceError):self.acquire(Transport(mutate))

    def test_relation_proof_must_match_value_and_source(self):
        mutations=[lambda r:r['data'].update(relations={}),
                   *[lambda r,k=k,v=v:r['data']['relations']['Employee'].update({k:v}) for k,v in (
                       ('nativeReference','wrong'),('metadataKind','document'),('sourceNamespace','sha256:'+'b'*64),
                       ('catalogMetadataId','14'),('observedCatalogType','Other'),('catalogType','Invoke()'))],
                   lambda r:r['data']['attributes']['Employee'].update(empty=True),
                   lambda r:r['data']['attributes']['Employee'].update(selected=1),
                   lambda r:r['data']['relations'].update(Category={'status':'native-typed-unverified'})]
        for mutate in mutations:
            with self.assertRaises(source.SourceError):self.read(Transport(mutate))

    def test_unsupported_enum_relation_and_unknown_header_are_explicit_hold(self):
        def unsupported(r):
            d=r['data'];d['attributes']['Category']={'valueKind':'unsupported','serializationStatus':'enum-unverified'}
            d['readStatus']='partial-hold'
        self.assertEqual(self.read(Transport(unsupported))['data']['readStatus'],'partial-hold')
        def relation(r):
            d=r['data'];d['relations']['Employee']={'status':'unsupported-hold','reason':'document-or-ambiguous-relation-unverified'}
            d['readStatus']='partial-hold'
        self.assertEqual(self.read(Transport(relation))['data']['readStatus'],'partial-hold')
        for field in ('posted','declaredLineCount'):
            def unknown(r):
                d=r['data'];(d['document'] if field=='posted' else d)[field]=None;d['readStatus']='partial-hold'
            self.assertEqual(self.read(Transport(unknown))['data']['readStatus'],'partial-hold')
        def false_complete(r):unsupported(r);r['data']['readStatus']='complete-unverified'
        with self.assertRaises(source.SourceError):self.read(Transport(false_complete))

    def test_lines_have_bound_continuation_and_requested_field_coverage(self):
        def page(r):
            d=r['data'];d.update(hasMore=True,nextLineCursor='1',declaredLineCount=2,readStatus='page-complete-unverified')
        self.assertEqual(self.read(Transport(page),limit=1)['data']['nextLineCursor'],'1')
        mutations=[lambda r:r['data'].update(nextLineCursor='1'),lambda r:r['data'].update(declaredLineCount=2),
                   lambda r:r['data'].update(hasMore=1),lambda r:r['data'].update(lines=r['data']['lines']*101),
                   lambda r:r['data']['lines'][0]['attributes'].update(Extra=8),
                   lambda r:r['data']['lines'][0].update(lineNumber=0),
                   lambda r:r['data']['attributes'].update(Extra=8)]
        for mutate in mutations:
            with self.assertRaises(source.SourceError):self.read(Transport(mutate))
        def count_changed(r):r['data'].update(declaredLineCount=2,readStatus='partial-hold',unsupported=[{'name':'lineCount','reason':'line-count-changed'}])
        self.assertEqual(self.read(Transport(count_changed))['data']['readStatus'],'partial-hold')

    def test_wrong_duplicate_or_out_of_page_ordinal_line_identity_rejects(self):
        for wrong in (0, 2, 99, True):
            def mutate(r): r['data']['lines'][0]['lineNumber'] = wrong
            with self.assertRaises(source.SourceError): self.read(Transport(mutate))
        def duplicated(r):
            d=r['data']; d['lines'] *= 2; d['declaredLineCount']=2
        with self.assertRaises(source.SourceError):self.read(Transport(duplicated))
        def out_of_page(r):r['data']['lines'][0]['lineNumber']=1
        with self.assertRaises(source.SourceError):self.read(Transport(out_of_page),line_cursor='1')
        def no_semantics(r):r['data'].pop('lineNumberSemantics')
        with self.assertRaises(source.SourceError):self.read(Transport(no_semantics))

    def test_stored_number_gaps_and_unavailable_standard_property_preserve_ordinal(self):
        self.assertEqual(self.read(Transport())['data']['lines'][0]['sourceLineNumber'],7)
        def unavailable(r):r['data']['lines'][0].update(sourceLineNumber=None,sourceLineNumberStatus='unavailable')
        self.assertEqual(self.read(Transport(unavailable))['data']['readStatus'],'complete-unverified')
        def gap(r):
            d=r['data'];first=d['lines'][0];second=copy.deepcopy(first)
            second.update(lineNumber=2,sourceLineNumber=99);d['lines'].append(second);d['declaredLineCount']=2
        self.assertEqual(self.read(Transport(gap))['data']['readStatus'],'complete-unverified')
        def duplicate_stored(r):gap(r);r['data']['lines'][1]['sourceLineNumber']=7
        with self.assertRaises(source.SourceError):self.read(Transport(duplicate_stored))
        def false_fallback(r):r['data']['lines'][0].update(sourceLineNumber=7,sourceLineNumberStatus='unavailable')
        with self.assertRaises(source.SourceError):self.read(Transport(false_fallback))

    def test_acquisition_seed_must_be_within_exclusive_requested_period(self):
        for wrong in ('1999-12-31','2001-01-01','2010-01-02'):
            def mutate(r):
                for key in ('seed','resolved'):r['data'][key]['date']=wrong
            with self.assertRaises(source.SourceError):self.acquire(Transport(mutate))

    def test_recovered_native_broker_error_remains_fixed_sanitized_code(self):
        class Recovered:
            def exchange(self,c,p,t):return dict(schemaVersion='1.0',operation=p['operation'],status='error',
                error=dict(code='BROKER_RECOVERED_STALE_PROCESSING',message='PRIVATE_CANARY'))
        with self.assertRaises(source.SourceError) as caught:self.read(Recovered())
        self.assertEqual(caught.exception.code,'SOURCE_ADAPTER_BROKER_RECOVERED_STALE_PROCESSING')
        self.assertNotIn('PRIVATE_CANARY',str(caught.exception))

    def test_private_native_failure_is_sanitized_at_mcp_boundary(self):
        canary='PRIVATE_DOCUMENT_TOKEN_HEADER_PATH'
        class Errors:
            def exchange(self,c,p,t):return dict(schemaVersion='1.0',operation=p['operation'],status='error',
                error=dict(code='NATIVE_DOCUMENT_NOT_FOUND',message=canary,
                           probeDiagnostic=dict(stage='find-document',exceptionType='System.Exception',token=canary,message=canary)))
        class Bridge:
            def source_read_document_by_native(self,*a):return source.request({},'read_document_by_native',transport=Errors(),**READ)
        output=io.StringIO()
        with patch.object(server,'_bridge',return_value=Bridge()),contextlib.redirect_stderr(output):
            with self.assertRaises(ToolError) as caught:server.source_read_document_by_native('synthetic77','SyntheticHire','12',TOKEN,NS,['Employee','Category'],['Hours'])
        for text in (str(caught.exception),output.getvalue()):
            self.assertNotIn(canary,text);self.assertNotIn(TOKEN,text)
        self.assertEqual(json.loads(str(caught.exception))['probeDiagnostic']['stage'],'find-document')

    def test_mcp_inputs_expose_no_expression_or_locator_as_identity(self):
        tools=asyncio.run(server.mcp.list_tools())
        schema=next(t.input_schema for t in tools if t.name=='source_read_document_by_native')
        self.assertEqual(set(schema['required']),set(READ)-{'line_cursor','limit'})
        for key in ('expression','document_cursor','date','number','from_date'):self.assertNotIn(key,schema['properties'])
        acquired=next(t.input_schema for t in tools if t.name=='source_acquire_document_identity')
        self.assertEqual(set(acquired['required']),set(ACQUIRE))

    def test_new_schema_advertisement_requires_complete_typed_contract(self):
        d=dict(datasets=[],supportedKinds=['document','reference','operation','operationEntry'],provisionalKinds=['constant'],
               unsupportedKinds=['enumeration'],readOperations=['document_presence','read_page','read_documents_page',
                   'read_document_lines_page','read_constants_page','read_operations_page','read_operation_entries_page','read_periodic_history_page',
                   'acquire_document_identity','read_document_by_native'],sourceNamespace=NS,
               nativeDocument=dict(metadataKind='document',seedCursorMax=100,lineCursorMax=10000,lineLimitMax=100,fieldLimitPerSection=32,
                   identityScope=source._DOCUMENT_IDENTITY_SCOPE,historyLinkStatus='unsupported-hold',historyLinkReason='periodic-document-native-transition-unimplemented'))
        source._validate_source_schema(d)
        for mutate in (lambda x:x.pop('nativeDocument'),lambda x:x.update(sourceNamespace='foreign'),
                       lambda x:x['readOperations'].remove('read_document_by_native'),lambda x:x['nativeDocument'].update(lineLimitMax=101)):
            broken=copy.deepcopy(d);mutate(broken)
            with self.assertRaises(source.SourceError):source._validate_source_schema(broken)
        # Older installed schemas remain valid and simply do not claim the new reader.
        d.pop('nativeDocument');d.pop('sourceNamespace');d['readOperations']=d['readOperations'][:-2]
        source._validate_source_schema(d)



if __name__=='__main__':unittest.main()
