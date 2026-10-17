from __future__ import annotations

import json
import io
import os
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "mcp_http_runtime"))

from mcp.server import MCPServer
from starlette.testclient import TestClient
import tomlkit

from onec_mcp_runtime.headers import _emit, headers_json, token_file_for_server
from onec_mcp_runtime.diagnostics import emit_event
import onec_mcp_runtime.macos as runtime_macos
from onec_mcp_runtime.macos import (
    InstallError,
    _extract_stdio_descriptor,
    _service_descriptor_path,
    _switch_codex_config,
    _write_private,
    _write_launch_agent,
    build_launch_agent_plist,
    support_root,
    _probe,
    _probe_failure,
)
from onec_mcp_runtime.transport import (
    BearerTokenMiddleware,
    create_authenticated_app,
    read_private_bearer_token,
)


class LocalMcpHttpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "server.token"
        self.token = "synthetic-runtime-test-token-0123456789"
        self.token_file.write_text(self.token + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)

    def make_server(self):
        server = MCPServer("synthetic runtime test")

        @server.tool()
        def ping() -> dict:
            """Synthetic, side-effect-free test tool."""
            return {"status": "ok"}

        return server

    def make_stdio_config(self, home: Path) -> tuple[Path, Path]:
        config = home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        repo = home / "source"
        repo.mkdir()
        toolkit = repo / "contrib" / "python"
        (toolkit / "src" / "agent_bridge").mkdir(parents=True)
        (toolkit / "pyproject.toml").write_text('[project]\nname="onec-community-toolkit"\n')
        config.write_text(
            f'''[mcp_servers.onec83-odata]
command = {json.dumps(sys.executable)}
args = ["-m", "onec_odata_mcp.server"]
cwd = {json.dumps(str(home))}
env = {{ ONEC83_TARGETS_JSON = {json.dumps('{"test":{}}')} }}

[mcp_servers.onecLive]
command = {json.dumps(sys.executable)}
args = ["-m", "agent_bridge"]
cwd = {json.dumps(str(home))}
env = {{ ONEC_BRIDGE_CONFIG = {json.dumps(str(home / 'bridge.json'))}, PYTHONPATH = "/private/custom/bridge-support" }}
''',
            encoding="utf-8",
        )
        return config, repo

    def test_reads_private_token_and_emits_codex_header(self):
        self.assertEqual(read_private_bearer_token(self.token_file), self.token)
        self.assertEqual(
            headers_json(self.token_file),
            '{"Authorization":"Bearer synthetic-runtime-test-token-0123456789"}',
        )
        self.assertEqual(
            token_file_for_server("onec83-odata", home=Path(self.temp.name)).name,
            "onec83-odata.token",
        )
        self.assertEqual(
            token_file_for_server("onecLive", home=Path(self.temp.name)).name,
            "onecLive.token",
        )

    def test_helper_entrypoints_do_not_import_sdk_even_when_sdk_is_unavailable(self):
        # A fresh process is essential: this test module has already imported MCP.
        home = Path(self.temp.name)
        tokens = home / "Library" / "Application Support" / "onec-mcp-runtime" / "tokens"
        tokens.mkdir(parents=True)
        for name in ("onecLive", "onec83-odata"):
            (tokens / f"{name}.token").write_text(self.token)
            (tokens / f"{name}.token").chmod(0o600)
        code = r'''
import sys, importlib.abc
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class DenySdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"mcp", "uvicorn", "httpx2", "tomlkit", "anyio"}:
            raise AssertionError("helper imported a server dependency")
sys.meta_path.insert(0, DenySdk())
Path.home = classmethod(lambda cls: Path(sys.argv[2]))
from onec_mcp_runtime import headers
getattr(headers, sys.argv[3])()
'''
        for entrypoint in ("live77_main", "odata_main"):
            result = subprocess.run(
                [sys.executable, "-B", "-I", "-c", code,
                 str(ROOT / "src" / "mcp_http_runtime"), str(home), entrypoint],
                capture_output=True, text=True, timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"Authorization": f"Bearer {self.token}"})
            events = [json.loads(line) for line in result.stderr.splitlines()]
            self.assertEqual([e["outcome"] for e in events], ["started", "token_ready", "completed"])
            self.assertNotIn(self.token, result.stderr)
            self.assertNotIn(str(home), result.stderr)

    def test_helper_failure_has_no_stdout_or_sensitive_diagnostics(self):
        for error in (ValueError(f"secret {self.token} {self.token_file}"),):
            out, err = io.StringIO(), io.StringIO()
            with patch("onec_mcp_runtime.headers.headers_json", side_effect=error), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(_emit("onecLive"), 1)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual([json.loads(line)["outcome"] for line in err.getvalue().splitlines()],
                             ["started", "token_rejected"])
            self.assertNotIn(self.token, err.getvalue())
            self.assertNotIn(str(self.token_file), err.getvalue())

    def test_helper_flush_failure_is_reported_without_secret(self):
        class BrokenOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("synthetic output failure")
        err = io.StringIO()
        with patch("onec_mcp_runtime.headers.token_file_for_server", return_value=self.token_file), redirect_stdout(BrokenOutput()), redirect_stderr(err):
            self.assertEqual(_emit("onecLive"), 1)
        self.assertEqual(json.loads(err.getvalue().splitlines()[-1])["outcome"], "output_failed")
        self.assertNotIn(self.token, err.getvalue())

    def test_diagnostic_sink_failure_does_not_block_header_output(self):
        with patch("onec_mcp_runtime.headers.token_file_for_server", return_value=self.token_file), patch("sys.stderr.write", side_effect=OSError("synthetic sink failure")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(_emit("onecLive"), 0)
        self.assertEqual(json.loads(output.getvalue()), {"Authorization": f"Bearer {self.token}"})

    def test_diagnostics_are_timestamped_and_allowlisted(self):
        err = io.StringIO()
        with redirect_stderr(err):
            emit_event("helper", "completed", started=time.monotonic(), status=200)
            emit_event(self.token, "completed")
            emit_event("helper", self.token)
        events = err.getvalue().splitlines()
        self.assertEqual(len(events), 1)
        event = json.loads(events[0])
        self.assertRegex(event["at"], r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
        self.assertGreaterEqual(event["elapsed_ms"], 0)
        self.assertEqual(set(event), {"event", "at", "phase", "outcome", "elapsed_ms", "status"})

    def test_token_validation_rejects_missing_malformed_and_foreign_owner(self):
        from types import SimpleNamespace
        with self.assertRaisesRegex(ValueError, "unavailable"):
            read_private_bearer_token(self.token_file.with_name("missing"))
        for value in ("short", "x" * 32 + " internal space", "é" * 32, "x" * 32 + "\nmore"):
            self.token_file.write_text(value)
            with self.assertRaisesRegex(ValueError, "ASCII"):
                read_private_bearer_token(self.token_file)
        self.token_file.write_bytes(b"\xff" * 32)
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            read_private_bearer_token(self.token_file)
        info = self.token_file.stat()
        with patch("onec_mcp_runtime.tokens.os.fstat", return_value=SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)):
            with self.assertRaisesRegex(ValueError, "current user"):
                read_private_bearer_token(self.token_file)

    def test_non_regular_token_fifo_fails_without_waiting_for_writer(self):
        fifo = self.token_file.with_name("synthetic-fifo")
        os.mkfifo(fifo, 0o600)
        code = "import sys;sys.path.insert(0,sys.argv[1]);from onec_mcp_runtime.tokens import read_private_bearer_token;read_private_bearer_token(sys.argv[2])"
        result = subprocess.run([sys.executable, "-B", "-I", "-c", code,
                                 str(ROOT / "src" / "mcp_http_runtime"), str(fifo)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a regular file", result.stderr)

    def test_probe_classifies_wrapped_client_failures_without_exception_text(self):
        import httpx2
        request = httpx2.Request("POST", "http://127.0.0.1:1/mcp")
        for status in (401, 403, 500):
            response = httpx2.Response(status, request=request)
            error = httpx2.HTTPStatusError(self.token, request=request, response=response)
            result = _probe_failure(ExceptionGroup("synthetic group", [error]), httpx2)
            self.assertEqual(result, ("auth_rejected" if status == 401 else "response_rejected", status))
        self.assertEqual(_probe_failure(httpx2.ConnectError(self.token), httpx2), ("connection_failed", None))
        self.assertEqual(_probe_failure(httpx2.ReadTimeout(self.token), httpx2), ("timed_out", None))
        self.assertEqual(_probe_failure(RuntimeError(self.token), httpx2), ("probe_failed", None))

    def test_probe_protocol_failure_preserves_exception_and_records_phase(self):
        import anyio
        @asynccontextmanager
        async def streams(*args, **kwargs):
            yield (None, None)
        class FailedSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def initialize(self): raise RuntimeError(self_token)
        self_token = self.token
        err = io.StringIO()
        with patch("mcp.client.streamable_http.streamable_http_client", streams), patch("mcp.ClientSession", return_value=FailedSession()), redirect_stderr(err):
            with self.assertRaisesRegex(RuntimeError, "synthetic-runtime"):
                anyio.run(_probe, "http://127.0.0.1:1/mcp", self.token)
        event = json.loads(err.getvalue().splitlines()[-1])
        self.assertEqual((event["phase"], event["outcome"]), ("probe_initialize", "probe_failed"))
        self.assertNotIn(self.token, err.getvalue())

    def test_probe_unavailable_in_isolated_scenario(self):
        import anyio
        # Keep the ephemeral socket bound, but never listen: no live server is addressed.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            port = unavailable.getsockname()[1]
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(Exception):
                anyio.run(_probe, f"http://127.0.0.1:{port}/mcp", self.token)
        self.assertIn(json.loads(err.getvalue().splitlines()[-1])["outcome"], {"connection_failed", "timed_out"})
        self.assertNotIn(self.token, err.getvalue())

    def test_editable_install_metadata_is_ignored_in_runtime_source(self):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT / "src" / "mcp_http_runtime"),
                "check-ignore",
                "-q",
                "onec_mcp_runtime.egg-info/PKG-INFO",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_rejects_loose_permissions_and_symlinks(self):
        self.token_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions"):
            read_private_bearer_token(self.token_file)
        self.token_file.chmod(0o600)
        link = Path(self.temp.name) / "token-link"
        link.symlink_to(self.token_file)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            read_private_bearer_token(link)

    def test_private_file_write_does_not_change_parent_directory_permissions(self):
        parent = Path(self.temp.name) / "shared-directory"
        parent.mkdir()
        parent.chmod(0o755)
        path = parent / "private-file"
        _write_private(path, b"private")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    def test_bridge_descriptor_preserves_existing_pythonpath(self):
        repo = Path(self.temp.name) / "repo"
        (repo / "contrib" / "python" / "src" / "agent_bridge").mkdir(parents=True)
        (repo / "contrib" / "python" / "pyproject.toml").write_text('[project]\nname="onec-community-toolkit"\n')
        import tomlkit

        doc = tomlkit.parse(
            f'[mcp_servers.onecLive]\ncommand = {json.dumps(sys.executable)}\n'
            'args = ["-m", "agent_bridge"]\n'
            'env = { PYTHONPATH = "/private/custom/bridge-support" }\n'
        )
        descriptor = _extract_stdio_descriptor(doc, "onecLive", repo)
        paths = descriptor["environment"]["PYTHONPATH"].split(":")
        self.assertEqual(paths[0], str(repo / "contrib" / "python" / "src" / "agent_bridge"))
        self.assertIn("/private/custom/bridge-support", paths)

    def test_authenticated_session_lists_tools_and_calls_synthetic_tool(self):
        app = create_authenticated_app(
            self.make_server(),
            host="127.0.0.1",
            port=8765,
            token_file=self.token_file,
            json_response=True,
            stateless_http=True,
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            initialized = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-test", "version": "1"},
                    },
                },
            )
            self.assertEqual(initialized.status_code, 200, initialized.text)
            session_id = initialized.headers.get("mcp-session-id")
            self.assertIsNone(session_id)
            server_info = initialized.json()["result"]["serverInfo"]
            self.assertEqual(server_info["name"], "synthetic runtime test")
            session_headers = {
                **headers,
                "Mcp-Protocol-Version": "2025-03-26",
            }
            listed = client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual([tool["name"] for tool in listed.json()["result"]["tools"]], ["ping"])
            called = client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "ping", "arguments": {}},
                },
            )
            self.assertEqual(called.status_code, 200, called.text)
            self.assertIn("status", called.text)

    def test_launch_readiness_probe_uses_real_stateless_http_client(self):
        import anyio
        import uvicorn

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        app = create_authenticated_app(
            self.make_server(),
            host="127.0.0.1",
            port=port,
            token_file=self.token_file,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        )
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started, "synthetic Streamable HTTP server did not start")
            err = io.StringIO()
            with redirect_stderr(err):
                count = anyio.run(_probe, f"http://127.0.0.1:{port}/mcp", self.token)
            self.assertEqual(count, 1)
            events = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith('{"event":')]
            self.assertIn(("probe_initialize", "completed"), [(e["phase"], e["outcome"]) for e in events])
            self.assertIn(("probe_tools", "completed"), [(e["phase"], e["outcome"]) for e in events])
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(Exception):
                anyio.run(_probe, f"http://127.0.0.1:{port}/mcp", "synthetic-wrong-token-0123456789abcdef")
            events = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith('{"event":')]
            self.assertEqual((events[-1]["outcome"], events[-1]["status"]), ("auth_rejected", 401))
            self.assertNotIn(self.token, err.getvalue())
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "synthetic Streamable HTTP server did not stop")

    def test_rejects_missing_wrong_host_and_wrong_origin(self):
        app = create_authenticated_app(
            self.make_server(),
            host="127.0.0.1",
            port=8765,
            token_file=self.token_file,
            json_response=True,
        )
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            missing = client.post("/mcp", json=body)
            self.assertEqual(missing.status_code, 401)
            wrong = client.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"})
            self.assertEqual(wrong.status_code, 401)
            wrong_host = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {self.token}", "Host": "attacker.invalid"},
                json=body,
            )
            self.assertEqual(wrong_host.status_code, 421)
            wrong_origin = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {self.token}", "Origin": "https://attacker.invalid"},
                json=body,
            )
            self.assertEqual(wrong_origin.status_code, 403)

    def test_http_diagnostics_observe_status_without_recording_request_data(self):
        import anyio
        async def rejected_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": self.token.encode()})
        middleware = BearerTokenMiddleware(rejected_app, self.token)
        async def exercise():
            async def receive(): return {"type": "http.request", "body": self.token.encode()}
            async def send(message): pass
            for headers in ([], [(b"authorization", f"Bearer {self.token}".encode())]):
                await middleware({"type": "http", "path": f"/{self.token}", "headers": headers}, receive, send)
        err = io.StringIO()
        with redirect_stderr(err):
            anyio.run(exercise)
        events = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual([(e["outcome"], e["status"]) for e in events],
                         [("auth_rejected", 401), ("response_rejected", 403)])
        self.assertNotIn(self.token, err.getvalue())

    def test_lifespan_diagnostics_observe_readiness_and_shutdown_without_failure_text(self):
        import anyio
        forwarded = []
        async def app(scope, receive, send):
            for kind in ("lifespan.startup.complete", "lifespan.shutdown.failed"):
                await send({"type": kind, "message": self.token})
        async def exercise():
            async def receive(): return {"type": "lifespan.startup"}
            async def send(message): forwarded.append(message)
            await BearerTokenMiddleware(app, self.token)({"type": "lifespan"}, receive, send)
        err = io.StringIO()
        with redirect_stderr(err):
            anyio.run(exercise)
        self.assertEqual(len(forwarded), 2)
        self.assertEqual([json.loads(line)["outcome"] for line in err.getvalue().splitlines()],
                         ["started", "ready", "failed"])
        self.assertNotIn(self.token, err.getvalue())

    def test_launch_agent_is_user_scoped_and_does_not_embed_token(self):
        token_path = Path(self.temp.name) / "private.token"
        plist = build_launch_agent_plist(
            {
                "server_id": "onec83-odata",
                "command": "/private/tmp/venv/bin/python",
                "args": ["-m", "onec_odata_mcp.server"],
                "environment": {
                    "ONEC83_TARGETS_JSON": '{"test":{}}',
                    "ONEC_MCP_BEARER_TOKEN": self.token,
                },
                "working_directory": "/private/tmp/source",
            },
            token_path,
            home=Path(self.temp.name),
        )
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ProgramArguments"][0], "/private/tmp/venv/bin/python")
        self.assertIn("127.0.0.1", plist["ProgramArguments"])
        self.assertIn("8765", plist["ProgramArguments"])
        self.assertEqual(plist["ProgramArguments"][-1], str(token_path))
        self.assertNotIn("ONEC_MCP_BEARER_TOKEN", plist["EnvironmentVariables"])
        self.assertNotIn(self.token, repr(plist))

    def test_launch_agent_writer_prepares_private_log_directory(self):
        home = Path(self.temp.name)
        descriptor = {
            "server_id": "onec83-odata",
            "command": "/private/tmp/venv/bin/python",
            "args": ["-m", "onec_odata_mcp.server"],
            "environment": {},
            "working_directory": "/private/tmp/source",
            "repo_root": "/private/tmp/source",
        }
        token_path = home / "Library" / "Application Support" / "onec-mcp-runtime" / "tokens" / "server.token"
        plist_path = _write_launch_agent(descriptor, token_path, home=home)
        logs = home / "Library" / "Logs" / "onec-mcp-runtime"
        self.assertTrue(logs.is_dir())
        self.assertEqual(logs.stat().st_mode & 0o777, 0o700)
        self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)

    def test_codex_switch_preserves_other_options_and_keeps_backup(self):
        home = Path(self.temp.name)
        config = home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            """[mcp_servers.onec83-odata]
command = "/private/tmp/odata-venv/bin/python"
args = ["-m", "onec_odata_mcp.server"]
env = { ONEC83_TARGETS_JSON = "private-targets" }
required = true
default_tools_approval_mode = "prompt"

[mcp_servers.onecLive]
command = "/private/tmp/live-venv/bin/python"
args = ["-m", "agent_bridge"]
env = { ONEC_BRIDGE_CONFIG = "/private/config.json" }
""",
            encoding="utf-8",
        )
        (support_root(home) / "services").mkdir(parents=True)
        for server_id, command in (
            ("onec83-odata", "/private/tmp/odata-venv/bin/python"),
            ("onecLive", "/private/tmp/live-venv/bin/python"),
        ):
            descriptor = {
                "server_id": server_id,
                "command": command,
                "args": [],
                "environment": {},
                "working_directory": str(home),
                "repo_root": "/private/tmp/source",
            }
            _write_private(
                _service_descriptor_path(server_id, home=home),
                json.dumps(descriptor).encode("utf-8"),
            )
        _switch_codex_config(config, home=home)
        result = tomlkit.parse(config.read_text(encoding="utf-8"))
        odata = result["mcp_servers"]["onec83-odata"]
        bridge = result["mcp_servers"]["onecLive"]
        self.assertEqual(odata["url"], "http://127.0.0.1:8765/mcp")
        self.assertEqual(bridge["url"], "http://127.0.0.1:8766/mcp")
        self.assertEqual(odata["default_tools_approval_mode"], "prompt")
        self.assertIs(odata["required"], True)
        self.assertIs(bridge["required"], False)
        self.assertNotIn("command", odata)
        self.assertNotIn("env", odata)
        self.assertTrue(odata["http_headers_helper"].endswith("onec-mcp-http-auth-odata"))
        self.assertTrue(bridge["http_headers_helper"].endswith("onec-mcp-http-auth-live77"))
        backup = support_root(home) / "backups" / "config.toml.before-streamable-http"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.stat().st_mode & 0o077, 0)

    def test_install_switches_config_only_after_both_services_pass_mcp_probe(self):
        home = Path(self.temp.name)
        config, repo = self.make_stdio_config(home)
        original = config.read_text(encoding="utf-8")
        with (
            patch.object(runtime_macos.sys, "platform", "darwin"),
            patch.object(runtime_macos, "_verify_main_checkout"),
            patch.object(runtime_macos, "_install_packages"),
            patch.object(runtime_macos, "_bootstrap") as bootstrap,
            patch.object(runtime_macos, "_wait_for_service", return_value=5) as probe,
        ):
            runtime_macos.install(config, repo, home=home)
        self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(probe.call_count, 2)
        migrated = tomlkit.parse(config.read_text(encoding="utf-8"))["mcp_servers"]
        self.assertEqual(migrated["onec83-odata"]["url"], "http://127.0.0.1:8765/mcp")
        self.assertEqual(migrated["onecLive"]["url"], "http://127.0.0.1:8766/mcp")
        self.assertNotIn("command", migrated["onec83-odata"])
        self.assertNotEqual(config.read_text(encoding="utf-8"), original)

    def test_install_failure_keeps_stdio_config_and_stops_started_agents(self):
        home = Path(self.temp.name)
        config, repo = self.make_stdio_config(home)
        original = config.read_bytes()
        with (
            patch.object(runtime_macos.sys, "platform", "darwin"),
            patch.object(runtime_macos, "_verify_main_checkout"),
            patch.object(runtime_macos, "_install_packages"),
            patch.object(runtime_macos, "_bootstrap"),
            patch.object(runtime_macos, "_wait_for_service", side_effect=InstallError("synthetic failure")),
            patch.object(runtime_macos, "_launchctl") as launchctl,
        ):
            with self.assertRaisesRegex(InstallError, "synthetic failure"):
                runtime_macos.install(config, repo, home=home)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(launchctl.call_count, 2)

    def test_routine_update_refreshes_launch_agents_without_codex_config(self):
        home = Path(self.temp.name)
        repo = home / "source"
        repo.mkdir()
        for server_id, module in (
            ("onec83-odata", "onec_odata_mcp.server"),
            ("onecLive", "agent_bridge"),
        ):
            descriptor = {
                "server_id": server_id,
                "command": sys.executable,
                "args": ["-m", module],
                "environment": {"SAFE_TEST_VALUE": "synthetic"},
                "working_directory": str(home),
                "repo_root": str(repo),
            }
            _write_launch_agent(
                descriptor,
                runtime_macos.ensure_token(server_id, home=home),
                home=home,
            )
        with (
            patch.object(runtime_macos.sys, "platform", "darwin"),
            patch.object(runtime_macos, "_verify_main_checkout"),
            patch.object(runtime_macos, "_install_packages") as install_packages,
            patch.object(runtime_macos, "_bootstrap") as bootstrap,
            patch.object(runtime_macos, "_wait_for_service", return_value=5) as probe,
        ):
            runtime_macos.update(home=home)
        self.assertEqual(install_packages.call_count, 2)
        self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(probe.call_count, 2)

    def test_rejects_non_loopback_and_non_mcp_paths(self):
        server = self.make_server()
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_authenticated_app(server, host="0.0.0.0", port=8765, token_file=self.token_file)
        with self.assertRaisesRegex(ValueError, "/mcp"):
            create_authenticated_app(
                server,
                host="127.0.0.1",
                port=8765,
                token_file=self.token_file,
                streamable_http_path="/",
            )


if __name__ == "__main__":
    unittest.main()
