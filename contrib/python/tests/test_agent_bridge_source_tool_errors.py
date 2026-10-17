"""MCP-facing tests for sanitized failures in 7.7 source tools."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from mcp.server.mcpserver.exceptions import ToolError

from agent_bridge import server, source
from agent_bridge.config import ConfigError, load_config
from agent_bridge.core import Bridge


class SourceToolErrorTests(unittest.TestCase):
    def call_tool(self, name: str, args: dict) -> tuple[ToolError, str]:
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                asyncio.run(server.mcp.call_tool(name, args))
        except ToolError as exc:
            return exc, stderr.getvalue()
        self.fail("expected MCP ToolError")

    @staticmethod
    def diagnostic(error: ToolError) -> dict:
        text = str(error)
        start = text.find("{")
        if start < 0:
            raise AssertionError(f"MCP error did not preserve diagnostic JSON: {text}")
        return json.loads(text[start:])

    def test_unknown_alias_is_actionable_and_stops_before_transport(self):
        bridge = Bridge({"sources": {"live77": {}}})
        with patch.object(server, "_bridge", return_value=bridge), \
                patch.object(source.SSHExecTransport, "exchange", side_effect=AssertionError("must not call transport")):
            error, logged = self.call_tool("source_schema", {"source_alias": "src77-2010-2012"})
        result = self.diagnostic(error)
        self.assertEqual(result["code"], "UNKNOWN_SOURCE_ALIAS")
        self.assertEqual(result["stage"], "source_lookup")
        self.assertEqual(result["availableSourceAliases"], ["live77"])
        self.assertEqual(result["action"], "Use one of the configured source aliases.")
        self.assertNotIn("src77-2010-2012", str(error) + logged)
        self.assertEqual(json.loads(logged)["correlationId"], result["correlationId"])

    def test_transport_and_remote_adapter_errors_are_classified(self):
        bridge = Bridge({"sources": {"live77": {
            "host": "host", "user": "reader", "remoteCommand": "fixed-worker",
        }}})
        with patch.object(server, "_bridge", return_value=bridge), \
                patch("agent_bridge.source.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 3)):
            error, logged = self.call_tool("source_health", {"source_alias": "live77"})
        result = self.diagnostic(error)
        self.assertEqual((result["code"], result["stage"]),
                         ("SOURCE_TRANSPORT_FAILURE", "transport"))
        self.assertEqual(json.loads(logged)["correlationId"], result["correlationId"])

        def remote_failure(_profile, payload, _timeout):
            return {"schemaVersion": "1.0", "status": "error", "operation": "health",
                    "requestId": payload["requestId"], "error": {"code": "COM_INIT_FAILED"}}

        with patch.object(server, "_bridge", return_value=bridge), \
                patch.object(source.SSHExecTransport, "exchange", side_effect=remote_failure):
            error, logged = self.call_tool("source_health", {"source_alias": "live77"})
        result = self.diagnostic(error)
        self.assertEqual((result["code"], result["stage"]),
                         ("SOURCE_ADAPTER_COM_INIT_FAILED", "source_adapter"))
        self.assertEqual(json.loads(logged)["correlationId"], result["correlationId"])

    def test_unexpected_error_is_sanitized_in_client_and_stderr(self):
        secret = "private-path-and-secret-canary"

        class BrokenBridge:
            def source_schema(self, _alias):
                raise RuntimeError(secret)

        with patch.object(server, "_bridge", return_value=BrokenBridge()):
            error, logged = self.call_tool("source_schema", {"source_alias": "live77"})
        result = self.diagnostic(error)
        self.assertEqual(result["code"], "SOURCE_INTERNAL_ERROR")
        self.assertEqual(result["stage"], "internal")
        self.assertTrue(result["correlationId"])
        self.assertNotIn(secret, str(error) + logged)

    def test_missing_runtime_config_is_a_sanitized_configuration_error(self):
        with patch.dict(os.environ, {"ONEC_BRIDGE_CONFIG": "/path/that/does/not/exist"}):
            with self.assertRaisesRegex(ConfigError, "cannot resolve runtime config"):
                load_config()
        with patch.object(server, "_bridge", side_effect=ConfigError("private path canary")):
            error, logged = self.call_tool("source_health", {"source_alias": "live77"})
        result = self.diagnostic(error)
        self.assertEqual((result["code"], result["stage"]), ("CONFIGURATION_ERROR", "configuration"))
        self.assertNotIn("private path canary", str(error) + logged)

    def test_wrapped_tool_signature_and_mcp_schema_are_unchanged(self):
        self.assertEqual(str(inspect.signature(server.source_schema)), "(source_alias: 'str') -> 'dict'")
        schema = next(tool.input_schema for tool in asyncio.run(server.mcp.list_tools())
                      if tool.name == "source_schema")
        self.assertEqual(schema["required"], ["source_alias"])
        self.assertEqual(schema["properties"], {
            "source_alias": {"title": "Source Alias", "type": "string"},
        })

    def test_successful_mcp_result_is_unchanged(self):
        expected = {"status": "ok", "operation": "schema", "sourceAlias": "live77",
                    "data": {"supportedKinds": ["document"]}}

        class HealthyBridge:
            def source_schema(self, _alias):
                return expected

        with patch.object(server, "_bridge", return_value=HealthyBridge()):
            result = asyncio.run(server.mcp.call_tool("source_schema", {"source_alias": "live77"}))
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), expected)


if __name__ == "__main__":
    unittest.main()
