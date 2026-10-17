"""Synthetic protocol negatives; no 7.7 runtime or stable-ID proof."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'agent_bridge'))
from agent_bridge import source


def response(payload, token='private-synthetic-native-A', code='001', name='Synthetic A'):
    row = dict(valueKind='reference', nativeReference=token, selected=True,
               nativeIdentityStatus='unverified', refTypeId='12', code=code,
               description=name, fullCode=code, empty=False, isGroup=False)
    return dict(schemaVersion='1.0', operation=payload['operation'], status='ok',
                requestId=payload['requestId'], sourceAlias=payload['sourceAlias'], data=dict(
                    kind='reference_identity_probe', type=payload['type'],
                    seedCursor=payload['cursor'], catalogMetadataId='12',
                    nativeReference=token, resolvedNativeReference=token,
                    roundtripStatus='matched-selected', stableIdentity='unverified', snapshotStatus='none',
                    identityScope='same-information-base-only; base continuity and lifetime unverified',
                    seed=copy.deepcopy(row), resolved=copy.deepcopy(row), attributes={}, unsupported=[]))


class Transport:
    def __init__(self, mutate=None, **fields):
        self.calls = []
        self.mutate = mutate
        self.fields = fields

    def exchange(self, config, payload, timeout):
        self.calls.append(copy.deepcopy(payload))
        value = response(payload, **self.fields)
        if self.mutate:
            self.mutate(value)
        return value


class NativeIdentityTests(unittest.TestCase):
    def probe(self, transport, **kwargs):
        return source.request({}, 'probe_reference_identity', source_alias='synthetic77',
                              dataset_type=kwargs.get('type_name', 'SyntheticCatalog'),
                              cursor=kwargs.get('cursor', '0'), transport=transport)

    def test_request_contains_only_bounded_seed_no_serialized_input(self):
        t = Transport()
        result = self.probe(t, cursor='100')
        self.assertEqual(set(t.calls[0]), {'schemaVersion', 'requestId', 'operation', 'sourceAlias', 'type', 'cursor'})
        self.assertEqual(result['data']['stableIdentity'], 'unverified')
        self.assertEqual(result['data']['snapshotStatus'], 'none')

    def test_invalid_seed_rejected_before_transport(self):
        for cursor in (None, '', '-1', '101', '0;write', 0, '9' * 30):
            with self.subTest(cursor=cursor):
                t = Transport()
                with self.assertRaises(ValueError):
                    self.probe(t, cursor=cursor)
                self.assertFalse(t.calls)

    def test_invalid_type_rejected_before_transport(self):
        for name in ('Catalog.Other', 'Invoke()', '', None):
            t = Transport()
            with self.assertRaises(ValueError):
                self.probe(t, type_name=name)
            self.assertFalse(t.calls)

    def test_changed_scope_proof_or_identity_fails_closed(self):
        mutations = [
            lambda x: x.update(sourceAlias='another77'),
            *[lambda x, k=k, v=v: x['data'].update({k: v}) for k, v in (
                ('type', 'OtherCatalog'), ('seedCursor', '1'), ('catalogMetadataId', 'bad'),
                ('resolvedNativeReference', 'other-native'), ('nativeReference', ''),
                ('nativeReference', 'x' * 4097), ('stableIdentity', 'verified'),
                ('snapshotStatus', 'frozen'), ('roundtripStatus', 'unknown'),
                ('identityScope', 'global-uid'), ('attributes', []), ('unsupported', None))],
            *[lambda x, k=k, v=v: x['data']['resolved'].update({k: v}) for k, v in (
                ('nativeReference', 'other'), ('selected', False), ('selected', 1),
                ('refTypeId', '13'), ('code', 'other'), ('description', 'other'),
                ('nativeIdentityStatus', 'verified'))],
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(source.SourceError):
                    self.probe(Transport(mutation))

    def test_opaque_token_equality_rejects_nul_and_canonical_equivalence(self):
        token = 'private-synthetic-caf\u00e9'
        for changed in (token[:1] + '\0' + token[1:], 'private-synthetic-cafe\u0301'):
            def mutation(value):
                value['data']['resolvedNativeReference'] = changed
                value['data']['resolved']['nativeReference'] = changed
            transport = Transport(mutation, token=token)
            with self.subTest(changed=changed), self.assertRaises(source.SourceError):
                self.probe(transport)
            self.assertEqual(len(transport.calls), 1)

    def test_retry_rename_code_change_does_not_derive_identity_from_scalars(self):
        first = self.probe(Transport())['data']
        retry = self.probe(Transport())['data']
        renamed = self.probe(Transport(code='999', name='Renamed synthetic A'))['data']
        same_code_other_row = self.probe(Transport(token='private-synthetic-native-B'))['data']
        self.assertEqual(first['nativeReference'], retry['nativeReference'])
        self.assertEqual(first['nativeReference'], renamed['nativeReference'])
        self.assertNotEqual(first['nativeReference'], same_code_other_row['nativeReference'])



if __name__ == '__main__':
    unittest.main()
