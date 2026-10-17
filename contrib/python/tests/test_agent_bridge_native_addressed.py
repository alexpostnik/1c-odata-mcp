"""Private synthetic addressed-reference protocol; no runtime/identity acceptance."""
import asyncio
import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/agent_bridge'))
from agent_bridge import source, server
from mcp.server.mcpserver.exceptions import ToolError

TOKEN = 'private-synthetic-caf\u00e9'
NS = 'sha256:' + 'a' * 64
ARGS = dict(source_alias='synthetic77', dataset_type='SyntheticCatalog',
            catalog_metadata_id='12', native_reference=TOKEN, as_of='2026-10-02',
            attribute_names=['Status', 'Master'])


def response(p):
    row = dict(valueKind='reference', nativeReference=p['nativeReference'], selected=True,
               refTypeId='12', nativeIdentityStatus='unverified', code='001', description='Synthetic')
    relation = dict(valueKind='reference', nativeReference='private-synthetic-master', selected=True,
                    empty=False, refTypeId='13', nativeIdentityStatus='unverified')
    answer = dict(schemaVersion='1.0', operation=p['operation'], status='ok',
                requestId=p['requestId'], sourceAlias=p['sourceAlias'], data=dict(
                    kind='reference_by_native', type=p['type'], catalogMetadataId='12',
                    observedCatalogType=p['type'], nativeReference=p['nativeReference'],
                    roundtripStatus='matched-typed-selected', stableIdentity='unverified', snapshotStatus='none',
                    identityScope='same-information-base-only; base continuity and lifetime unverified',
                    readContext=dict(asOf=p['asOf'], useDate='applied', selection='FindItem',
                                     periodicStatus='date-context-applied-unverified'),
                    readStatus='complete-unverified', reference=row,
                    attributes={'Status': 'synthetic-current', 'Master': relation},
                    relations={'Master': dict(status='native-typed-unverified', catalogType='SyntheticMaster',
                        observedCatalogType='SyntheticMaster', catalogMetadataId='13', nativeReference=relation['nativeReference'])},
                    unsupported=[]))
    if 'sourceNamespace' in p:
        answer['data'].update(sourceNamespace=p['sourceNamespace'], sourceNamespaceProof=dict(
            scheme='configured-alias-windows-path-sha256-v1', expected=p['sourceNamespace'],
            observed=p['sourceNamespace'], status='matched-request'))
    return answer


class Transport:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def exchange(self, config, payload, timeout):
        self.calls.append(copy.deepcopy(payload))
        result = response(payload)
        if self.mutate:
            self.mutate(result)
        return result


class AddressedTests(unittest.TestCase):
    def read(self, t, **changed):
        return source.request({}, 'read_reference_by_native', transport=t, **(ARGS | changed))

    def test_saved_token_and_original_asof_are_data_not_cursor(self):
        t = Transport()
        r = self.read(t)['data']
        self.assertEqual(set(t.calls[0]), {'schemaVersion', 'operation', 'requestId', 'sourceAlias',
                                        'type', 'catalogMetadataId', 'nativeReference', 'asOf', 'attributeNames'})
        self.assertEqual(r['nativeReference'], TOKEN)
        self.assertEqual(r['readContext']['asOf'], '2026-10-02')
        self.assertEqual(r['stableIdentity'], 'unverified')
        self.assertEqual(r['snapshotStatus'], 'none')

    def test_optional_scoped_catalog_request_requires_actual_matching_producer_contract(self):
        t = Transport()
        r = self.read(t, source_namespace=NS)['data']
        self.assertEqual(t.calls[0]['sourceNamespace'], NS)
        self.assertEqual(r['sourceNamespaceProof'], dict(scheme='configured-alias-windows-path-sha256-v1',
            expected=NS, observed=NS, status='matched-request'))
        mutations = [
            lambda r: r['data'].pop('sourceNamespace'),
            lambda r: r['data'].pop('sourceNamespaceProof'),
            lambda r: r['data'].update(sourceNamespace='sha256:'+'b'*64),
            *[lambda r, k=k, v=v: r['data']['sourceNamespaceProof'].update({k:v}) for k,v in (
                ('scheme','unknown'), ('expected',None), ('observed','sha256:'+'b'*64),
                ('status','observed-unbound'))],
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(source.SourceError):
                self.read(Transport(mutate), source_namespace=NS)
        for namespace in ('', 'foreign77', 'sha256:'+'A'*64, 1):
            t = Transport()
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                self.read(t, source_namespace=namespace)
            self.assertEqual(t.calls, [])

    def test_legacy_omitted_scope_works_but_naked_appended_namespace_is_rejected(self):
        self.assertNotIn('sourceNamespace', self.read(Transport())['data'])
        def observed(r):
            r['data'].update(sourceNamespace=NS, sourceNamespaceProof=dict(
                scheme='configured-alias-windows-path-sha256-v1', expected=None,
                observed=NS, status='observed-unbound'))
        self.assertEqual(self.read(Transport(observed))['data']['sourceNamespaceProof']['status'], 'observed-unbound')
        with self.assertRaises(source.SourceError):
            self.read(Transport(lambda r: r['data'].update(sourceNamespace=NS)))

    def test_unsafe_or_incomplete_request_rejected_before_transport(self):
        changes = [dict(native_reference=t) for t in (None, '', ' ', 'x' * 4097, 'a\0b', 'a\u0085b', 'a\u2028b', 'a\ud800b', 'a\U0001f600b')]
        changes += [dict(as_of=d) for d in (None, '', '2026-2-02', '2026-02-30')]
        changes += [dict(attribute_names=n) for n in (None, 'Status', ['Status', 'Status'], ['x.y'], ['Invoke()'], ['x'] * 33)]
        changes += [dict(cursor='0'), dict(from_date='2026-10-02'), dict(catalog_metadata_id='9' * 11), dict(catalog_metadata_id='0'), dict(catalog_metadata_id=12), dict(dataset_type='Invoke()')]
        for changed in changes:
            with self.subTest(changed=changed):
                t = Transport()
                with self.assertRaises(ValueError): self.read(t, **changed)
                self.assertEqual(t.calls, [])

    def test_expression_looking_data_is_preserved_without_execution(self):
        for token in ('" ); Write(); //', 'prefix;ExecuteBatch("private-canary");', '  preserve spaces  ', 'a\n\tb'):
            t = Transport()
            self.assertEqual(self.read(t, native_reference=token)['data']['nativeReference'], token)
            self.assertEqual(len(t.calls), 1)

    def test_foreign_scope_date_scalar_only_or_ordinal_mismatch_rejected(self):
        mutations = [lambda r: r.update(sourceAlias='foreign77'),
            *[lambda r, k=k, v=v: r['data'].update({k: v}) for k, v in (
                ('type','Foreign'), ('catalogMetadataId','13'), ('observedCatalogType','Foreign'),
                ('nativeReference',TOKEN + '\0'),
                ('nativeReference','private-synthetic-cafe\u0301'), ('roundtripStatus','scalar-only'),
                ('stableIdentity','verified'), ('snapshotStatus','frozen'))],
            lambda r: r['data']['readContext'].update(asOf='2026-10-03'),
            lambda r: r['data']['readContext'].update(useDate='not-exposed'),
            lambda r: r['data']['reference'].update(selected=1),
            lambda r: r['data']['reference'].update(nativeReference='foreign'),
            lambda r: r['data']['reference'].update(refTypeId='13'),
            lambda r: r['data']['attributes'].update(Extra='canary'),
            lambda r: r['data']['relations']['Master'].update(observedCatalogType='Foreign'),
            lambda r: r['data']['relations']['Master'].update(nativeReference='foreign'),
            lambda r: r['data']['relations']['Master'].update(catalogMetadataId=13),
            lambda r: r['data']['attributes']['Master'].update(empty=True),
            lambda r: r['data'].update(relations={}),
            lambda r: r['data']['relations'].update(Status={'status':'native-typed-unverified'}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(source.SourceError): self.read(Transport(mutate))

    def test_unsupported_relation_or_scalar_retains_partial_hold(self):
        def unsupported(r):
            d = r['data']; d['relations']['Master'] = {'status':'unsupported-hold'}
            d['unsupported'] = [{'name':'Master', 'reason':'native-relation-unavailable'}]
            d['readStatus'] = 'partial-hold'
        self.assertEqual(self.read(Transport(unsupported))['data']['readStatus'], 'partial-hold')
        def scalar(r):
            r['data']['attributes']['Status'] = {'valueKind':'unsupported','serializationStatus':'attribute-read-failed'}
            r['data']['readStatus'] = 'partial-hold'
        self.assertEqual(self.read(Transport(scalar))['data']['readStatus'], 'partial-hold')
        def false_complete(r):
            unsupported(r); r['data']['readStatus'] = 'complete-unverified'
        with self.assertRaises(source.SourceError): self.read(Transport(false_complete))

    def test_remote_private_canary_never_reaches_mcp_or_diagnostics(self):
        canary = 'PRIVATE_TOKEN_EXCEPTION_PATH_CONFIG_CANARY'
        def error(p):
            return dict(schemaVersion='1.0',operation=p['operation'],requestId=p['requestId'],status='error',
                error=dict(code='ASOF_CONTEXT_UNAVAILABLE', message=canary,
                    probeDiagnostic=dict(stage='use-date', exceptionType='System.Exception', message=canary, token=canary)))
        class ErrorTransport:
            def exchange(self, c, p, t): return error(p)
        class FakeBridge:
            def source_read_reference_by_native(self, *args):
                return source.request({}, 'read_reference_by_native', transport=ErrorTransport(), **ARGS)
        output = io.StringIO()
        with patch.object(server, '_bridge', return_value=FakeBridge()), contextlib.redirect_stderr(output):
            with self.assertRaises(ToolError) as caught:
                server.source_read_reference_by_native('synthetic77', 'SyntheticCatalog','12',TOKEN,'2026-10-02',['Status','Master'])
        combined = output.getvalue() + str(caught.exception)
        self.assertNotIn(canary, combined)
        self.assertNotIn(TOKEN, combined)
        self.assertEqual(json.loads(str(caught.exception))['probeDiagnostic']['stage'], 'use-date')

    def test_mcp_contract_has_required_typed_inputs_no_expression_cursor(self):
        tools = asyncio.run(server.mcp.list_tools())
        schema = next(t.input_schema for t in tools if t.name == 'source_read_reference_by_native')
        self.assertEqual(set(schema['required']), {'source_alias','type_name','catalog_metadata_id','native_reference','as_of','attribute_names'})
        self.assertNotIn('cursor', schema['properties'])
        self.assertNotIn('expression', schema['properties'])
        self.assertIn('source_namespace', schema['properties'])




if __name__ == '__main__': unittest.main()
