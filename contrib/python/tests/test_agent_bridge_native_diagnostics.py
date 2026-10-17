"""Synthetic adapter faults through the MCP boundary; no Windows/OLE execution."""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/agent_bridge'))
from agent_bridge import server, source
from agent_bridge.core import Bridge
from mcp.server.mcpserver.exceptions import ToolError

CANARY = 'PRIVATE_PERSON_TOKEN_PATH_PASSWORD_CANARY'


class ProbeDiagnosticTests(unittest.TestCase):
    def fault(self, stage, diagnostic=None, operation='probe_reference_identity', code='NATIVE_PROBE_STAGE_FAILED'):
        remote = diagnostic if diagnostic is not None else {
            'stage': stage, 'exceptionType': 'System.Reflection.TargetInvocationException',
            'hresult': '0x80131604', 'message': CANARY, 'path': CANARY,
            'nativeReference': CANARY, 'sourceValues': {'name': CANARY},
            'innerExceptions': [{'exceptionType': 'System.Runtime.InteropServices.COMException',
                                 'hresult': '0x80020006', 'message': CANARY}],
        }
        calls = []

        def exchange(_config, payload, _timeout):
            calls.append(payload)
            return {'schemaVersion': '1.0', 'requestId': payload['requestId'],
                    'operation': operation, 'status': 'error',
                    'error': {'code': code, 'message': CANARY, 'detail': CANARY,
                              'probeDiagnostic': copy.deepcopy(remote)}}

        bridge = Bridge({'sources': {'synthetic77': {}}})
        name = 'source_probe_reference_identity' if operation == 'probe_reference_identity' else 'source_health'
        args = {'source_alias': 'synthetic77'}
        if operation == 'probe_reference_identity':
            args.update(type_name='SyntheticCatalog', cursor='0')
        stderr = io.StringIO()
        with patch.object(server, '_bridge', return_value=bridge), \
                patch.object(source.SSHExecTransport, 'exchange', side_effect=exchange), \
                contextlib.redirect_stderr(stderr):
            try:
                asyncio.run(server.mcp.call_tool(name, args))
            except ToolError as error:
                text = str(error)
            else:
                self.fail('fault must reach client as ToolError')
        self.assertEqual(len(calls), 1)  # no retry or alternate operation
        self.assertNotIn(CANARY, text + stderr.getvalue())
        result = json.loads(text[text.index('{'):])
        logged = json.loads(stderr.getvalue())
        self.assertEqual(result['correlationId'], logged['correlationId'])
        return result, logged

    def test_fault_at_each_stage_survives_transport_and_mcp_safely(self):
        for stage in sorted(source._PROBE_STAGES):
            with self.subTest(stage=stage):
                result, logged = self.fault(stage)
                self.assertEqual(result['code'], 'SOURCE_ADAPTER_NATIVE_PROBE_STAGE_FAILED')
                self.assertEqual(result['stage'], 'source_adapter')
                self.assertEqual(result['probeDiagnostic'], {
                    'stage': stage, 'exceptionType': 'System.Reflection.TargetInvocationException',
                    'hresult': '0x80131604', 'innerExceptions': [{
                        'exceptionType': 'System.Runtime.InteropServices.COMException', 'hresult': '0x80020006'}]})
                self.assertEqual(logged['probeDiagnostic'], result['probeDiagnostic'])
                self.assertIn('hold', result['action'])

    def test_malformed_or_private_fields_fail_closed(self):
        cases = [None, [], CANARY, {'stage': CANARY}, {'stage': []},
                 {'stage': 'current-item', 'exceptionType': CANARY, 'hresult': CANARY},
                 {'stage': 'current-item', 'exceptionType': {}, 'hresult': []},
                 {'stage': 'current-item', 'innerExceptions': CANARY},
                 {'stage': 'current-item', 'innerExceptions': [None, {}, {'exceptionType': CANARY}]},
                 {'stage': 'current-item', 'hresult': '0x800200060'},
                 {'stage': 'current-item', 'hresult': '0x80020006\n' }]
        for raw in cases:
            with self.subTest(raw=raw):
                result, _ = self.fault(None, diagnostic=raw if raw is not None else {'stage': None})
                self.assertNotIn(CANARY, json.dumps(result))
                safe = result.get('probeDiagnostic', {})
                self.assertLessEqual(set(safe), {'stage', 'exceptionType', 'hresult', 'innerExceptions'})
                self.assertNotIn('exceptionType', safe)
                self.assertNotIn('hresult', safe)

    def test_inner_chain_is_bounded_and_unknown_machine_code_not_echoed(self):
        raw = {'stage': 'resolve-seed', 'innerExceptions': [
            {'exceptionType': 'other', 'hresult': '0x80020006', 'message': CANARY} for _ in range(9)]}
        result, _ = self.fault(None, diagnostic=raw, code=CANARY)
        self.assertEqual(result['code'], 'SOURCE_ADAPTER_REMOTE_ERROR')
        self.assertEqual(len(result['probeDiagnostic']['innerExceptions']), 3)

    def test_nonprobe_remote_error_ignores_probe_diagnostic(self):
        result, logged = self.fault('current-item', operation='health', code='COM_INIT_FAILED')
        self.assertEqual(result['code'], 'SOURCE_ADAPTER_COM_INIT_FAILED')
        self.assertNotIn('probeDiagnostic', result)
        self.assertNotIn('probeDiagnostic', logged)
        self.assertEqual(result['action'], 'Check source adapter health and retry only after resolving the reported adapter condition.')



if __name__ == '__main__':
    unittest.main()
