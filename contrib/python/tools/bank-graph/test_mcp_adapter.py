"""Optional actual SDK integration, using only in-memory transport."""
import json
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch, Mock
from importlib.metadata import PackageNotFoundError, version

from test_reader import make_archive

SDK_AVAILABLE = importlib.util.find_spec("mcp") is not None
if SDK_AVAILABLE:
    import anyio
    from mcp import ClientSession
    from mcp.server import MCPServer
    from mcp.shared.memory import create_client_server_memory_streams


class SDKAdmissionTests(unittest.TestCase):
    def test_registration_rejects_wrong_or_missing_sdk_before_import_index_or_registration(self):
        import builtins
        import mcp_adapter as adapter
        for reported in ("2.0.0", "2.2.1", "2.3.0", "3.0.0", None):
            with self.subTest(reported=reported):
                server = Mock()
                original_import = builtins.__import__
                def guarded_import(name, *args, **kwargs):
                    if name == "pydantic" or name == "mcp" or name.startswith(("mcp.", "mcp_types")):
                        self.fail("SDK API imported before admission")
                    return original_import(name, *args, **kwargs)
                with patch.object(adapter, "version", return_value=reported,
                                  side_effect=PackageNotFoundError("mcp") if reported is None else None) as query, \
                        patch.object(adapter, "Reader") as reader, \
                        patch("builtins.__import__", side_effect=guarded_import):
                    with self.assertRaisesRegex(adapter.BankGraphSDKError, "exactly 2.2.0"):
                        adapter.register_bank_graph_tools(server, "/missing/archive")
                    query.assert_called_once_with("mcp")
                    reader.assert_not_called()
                    self.assertEqual(server.mock_calls, [])

    def test_standalone_rejects_wrong_or_missing_sdk_without_sdk_import_or_transport(self):
        # Fresh processes prove that even absent distribution metadata reaches a
        # clear CLI error before MCPServer import, indexing or transport startup.
        code = textwrap.dedent("""
            import builtins, importlib.metadata, importlib.util, sys
            original_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name == 'pydantic' or name == 'mcp' or name.startswith(('mcp.', 'mcp_types')):
                    raise AssertionError('SDK imported before admission')
                return original_import(name, *args, **kwargs)
            builtins.__import__ = guarded_import
            def reported(name):
                assert name == 'mcp'
                if sys.argv[2] == 'missing':
                    raise importlib.metadata.PackageNotFoundError(name)
                return sys.argv[2]
            importlib.metadata.version = reported
            spec = importlib.util.spec_from_file_location('standalone_adapter', sys.argv[1])
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            def forbidden_reader(*args, **kwargs):
                raise AssertionError('archive indexed before admission')
            adapter.Reader = forbidden_reader
            sys.argv = [sys.argv[1], '--archive', '/missing/archive']
            adapter.main()
        """)
        for reported in ("2.0.0", "2.2.1", "2.3.0", "3.0.0", "missing"):
            with self.subTest(reported=reported):
                result = subprocess.run([sys.executable, "-B", "-c", code,
                                         str((Path(__file__).resolve().parents[2] / "onec_bank_graph/mcp_adapter.py").resolve()), reported],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("bank graph requires installed MCP SDK exactly 2.2.0", result.stderr)
                self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(SDK_AVAILABLE, "optional mcp2.x SDK unavailable; core remains stdlib")
class AdapterTests(unittest.TestCase):
    def test_authenticated_http_discovery_is_responsive_during_initialization_both_protocols(self):
        import mcp_adapter as adapter
        from threading import Event
        from time import perf_counter
        import httpx2
        from mcp.client.streamable_http import streamable_http_client
        runtime = Path(__file__).resolve().parents[2] / "src/mcp_http_runtime"
        with patch.object(sys, "path", [str(runtime), *sys.path]):
            from onec_mcp_runtime.transport import create_authenticated_app
        for modern in (False, True):
            with self.subTest(modern=modern), tempfile.TemporaryDirectory() as directory:
                archive=Path(directory)/"archive"; archive.mkdir(); make_archive(archive)
                token=Path(directory)/"synthetic.token"; token.write_text("a"*64); token.chmod(0o600)
                entered, release=Event(), Event(); real_reader=adapter.Reader
                def loader(path):
                    entered.set()
                    if not release.wait(5): raise RuntimeError("synthetic timeout")
                    return real_reader(path)
                server=MCPServer("authenticated-slow-source")
                with patch.object(adapter, "Reader", side_effect=loader):
                    lifecycle=adapter.register_bank_graph_tools(server, archive, background=True)
                    try:
                        self.assertTrue(entered.wait(2))
                        app=create_authenticated_app(server, host="127.0.0.1", port=18990,
                                                     token_file=token, json_response=True)
                        async def exercise():
                            async with app.app.router.lifespan_context(app.app):
                                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                                    # No native listener or working-service request; actual auth layer.
                                    response=await http.post("http://127.0.0.1:18990/mcp", json={})
                                    self.assertEqual(response.status_code,401)
                                    http.headers["Authorization"]="Bearer "+"a"*64
                                    async with streamable_http_client("http://127.0.0.1:18990/mcp", http_client=http) as streams:
                                        async with ClientSession(*streams) as client:
                                            with anyio.fail_after(2):
                                                started=perf_counter()
                                                await (client.discover() if modern else client.initialize())
                                                self.assertLess(perf_counter()-started,2)
                                                self.assertEqual(len((await client.list_tools()).tools),6)
                                                self.assertEqual(len((await client.list_resources()).resources),2)
                                                result=await client.call_tool("bank_graph_contract",{})
                                                self.assertEqual(result.structured_content["startup"]["state"],"initializing")
                                                self.assertTrue((await client.call_tool("bank_graph_search",{})).is_error)
                                            release.set()
                                            self.assertTrue(await anyio.to_thread.run_sync(lifecycle.done.wait,2))
                                            self.assertEqual((await client.call_tool("bank_graph_contract",{})).structured_content["startup"]["state"],"ready")
                                            self.assertFalse((await client.call_tool("bank_graph_search",{})).is_error)
                        anyio.run(exercise)
                    finally: release.set(); lifecycle.close(); lifecycle.thread.join(2)

    def test_background_initializing_discovery_data_refusal_then_atomic_ready_both_protocols(self):
        import mcp_adapter as adapter
        from threading import Event
        from time import perf_counter
        for modern in (False, True):
            with self.subTest(modern=modern), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory); make_archive(archive)
                entered, release, validated, publish = (Event() for _ in range(4))
                loads = []
                real_reader = adapter.Reader
                def slow_reader(path):
                    loads.append(path); entered.set()
                    if not release.wait(5): raise RuntimeError("synthetic gate timeout")
                    return real_reader(path)
                def validator(reader):
                    validated.set()
                    if not publish.wait(5): raise RuntimeError("synthetic binding gate timeout")
                server = MCPServer("slow-reader", instructions="Preserve existing OData gates.")
                @server.tool()
                def existing_sentinel() -> dict:
                    """Existing discovery remains present."""
                    return {"unchanged": True}
                with patch.object(adapter, "Reader", side_effect=slow_reader):
                    lifecycle = adapter.register_bank_graph_tools(server, archive, background=True,
                                                                 reader_validator=validator)
                    try:
                        self.assertTrue(entered.wait(2))
                        with self.assertRaisesRegex(ValueError, "already selected"):
                            adapter.register_bank_graph_tools(server, archive, background=True)
                        with self.assertRaisesRegex(ValueError, "already started"):
                            lifecycle.start()
                        async def exercise():
                            async with create_client_server_memory_streams() as (cs, ss):
                                async with anyio.create_task_group() as group:
                                    low = server._lowlevel_server
                                    group.start_soon(low.run, *ss, low.create_initialization_options())
                                    async with ClientSession(*cs) as client:
                                        # No timeout override: modern uses actual SDK10s default.
                                        with anyio.fail_after(2):
                                            started = perf_counter()
                                            initial = await (client.discover() if modern else client.initialize())
                                            self.assertIn("initializing/ready/failed", initial.instructions)
                                            self.assertLess(perf_counter() - started, 2)
                                            self.assertEqual(len((await client.list_tools()).tools), 7)
                                            self.assertEqual(len((await client.list_resources()).resources), 2)
                                            state = (await client.call_tool("bank_graph_contract", {})).structured_content
                                            self.assertEqual(state["startup"]["state"], "initializing")
                                            self.assertIsNone(state["startup"]["archive_load_and_index_seconds"])
                                            resource = await client.read_resource("bank-graph://contract")
                                            self.assertEqual(json.loads(resource.contents[0].text)["startup"]["state"], "initializing")
                                            self.assertIn("startup.state=initializing", (await client.read_resource("bank-graph://manual")).contents[0].text)
                                            for name, args in [("bank_graph_overview", {}), ("bank_graph_search", {}),
                                                               ("bank_graph_get", {"node_id":"o11"}), ("bank_graph_links", {"node_id":"o11"}),
                                                               ("bank_graph_aggregate", {})]:
                                                refused = await client.call_tool(name, args)
                                                self.assertTrue(refused.is_error)
                                                self.assertIn("initializing", refused.content[0].text)
                                            self.assertFalse((await client.call_tool("existing_sentinel", {})).is_error)
                                        release.set()
                                        self.assertTrue(await anyio.to_thread.run_sync(validated.wait, 2))
                                        # Canonical reader exists privately; binding validation incomplete.
                                        self.assertIsNone(lifecycle.reader)
                                        self.assertEqual((await client.call_tool("bank_graph_contract", {})).structured_content["startup"]["state"], "initializing")
                                        publish.set()
                                        self.assertTrue(await anyio.to_thread.run_sync(lifecycle.done.wait, 2))
                                        ready = (await client.call_tool("bank_graph_contract", {})).structured_content["startup"]
                                        self.assertEqual(ready["state"], "ready")
                                        self.assertGreaterEqual(ready["archive_load_and_index_seconds"], 0)
                                        selected = lifecycle.reader
                                        for name, args in [("bank_graph_overview", {}), ("bank_graph_search", {}),
                                                           ("bank_graph_get", {"node_id":"o11"}), ("bank_graph_links", {"node_id":"o11"}),
                                                           ("bank_graph_aggregate", {})]:
                                            self.assertFalse((await client.call_tool(name, args)).is_error)
                                        self.assertIs(lifecycle.reader, selected)
                                    group.cancel_scope.cancel()
                        anyio.run(exercise)
                        self.assertEqual(loads, [archive])
                    finally:
                        release.set(); publish.set(); lifecycle.close(); lifecycle.thread.join(2)
                    self.assertFalse(lifecycle.thread.is_alive())
                    self.assertIsNone(lifecycle.reader)

    def test_background_missing_corrupt_and_binding_failure_sanitized_no_partial_data_both_protocols(self):
        import mcp_adapter as adapter
        for case in ("missing", "corrupt", "binding"):
            for modern in (False, True):
                with self.subTest(case=case, modern=modern), tempfile.TemporaryDirectory() as directory:
                    archive = Path(directory) / "archive"
                    if case != "missing":
                        archive.mkdir(); make_archive(archive)
                    if case == "corrupt": (archive / "manifest.json").write_text("{private-secret-source-path")
                    def validator(reader):
                        raise ValueError("private-secret-source-path")
                    server = MCPServer("failed-source")
                    lifecycle = adapter.register_bank_graph_tools(server, archive, background=True,
                                                 reader_validator=validator if case == "binding" else None)
                    try:
                        self.assertTrue(lifecycle.done.wait(2)); self.assertIsNone(lifecycle.reader)
                        async def exercise():
                            async with create_client_server_memory_streams() as (cs, ss):
                                async with anyio.create_task_group() as group:
                                    low = server._lowlevel_server
                                    group.start_soon(low.run, *ss, low.create_initialization_options())
                                    async with ClientSession(*cs) as client:
                                        with anyio.fail_after(2):
                                            await (client.discover() if modern else client.initialize())
                                            self.assertEqual(len((await client.list_tools()).tools), 6)
                                            state = (await client.call_tool("bank_graph_contract", {})).structured_content["startup"]
                                            self.assertEqual(state["state"], "failed")
                                            self.assertNotIn("private-secret", json.dumps(state))
                                            for name, args in [("bank_graph_overview", {}), ("bank_graph_search", {}),
                                                               ("bank_graph_get", {"node_id":"o11"}), ("bank_graph_links", {"node_id":"o11"}),
                                                               ("bank_graph_aggregate", {})]:
                                                refused = await client.call_tool(name, args)
                                                self.assertTrue(refused.is_error)
                                                self.assertIn("failed", refused.content[0].text)
                                                self.assertNotIn("private-secret", refused.content[0].text)
                                            self.assertIn("startup.state=failed", (await client.read_resource("bank-graph://manual")).contents[0].text)
                                    group.cancel_scope.cancel()
                        anyio.run(exercise)
                    finally: lifecycle.close(); lifecycle.thread.join(2)

    def test_close_during_background_initialization_fences_late_publication_without_waiting(self):
        import mcp_adapter as adapter
        from threading import Event
        from time import perf_counter
        entered, release = Event(), Event()
        def loader(path):
            entered.set(); release.wait(3); return object()
        with patch.object(adapter, "Reader", side_effect=loader) as load:
            lifecycle = adapter.register_bank_graph_tools(MCPServer("shutdown"), "/synthetic", background=True)
            try:
                self.assertTrue(entered.wait(2))
                started = perf_counter(); lifecycle.close()
                self.assertLess(perf_counter() - started, 0.1)
                self.assertIsNone(lifecycle.reader); self.assertEqual(lifecycle.status()["state"], "failed")
                release.set(); lifecycle.thread.join(2)
                self.assertFalse(lifecycle.thread.is_alive()); self.assertTrue(lifecycle.done.is_set())
                self.assertIsNone(lifecycle.reader); self.assertEqual(lifecycle.status()["state"], "failed")
                load.assert_called_once()
            finally: release.set(); lifecycle.close()

    def test_standalone_actual_exact_sdk_registers_before_selected_transport(self):
        import mcp_adapter as adapter
        self.assertEqual(version("mcp"), "2.2.0")
        with tempfile.TemporaryDirectory() as directory:
            make_archive(Path(directory))
            server = MCPServer("standalone-positive")
            middleware_before = len(server.middleware)
            with patch("sys.argv", ["mcp_adapter.py", "--archive", directory]), \
                    patch("mcp.server.MCPServer", return_value=server) as factory, \
                    patch.object(server, "run") as run, \
                    patch.object(adapter, "version", wraps=version) as query:
                adapter.main()
                self.assertEqual(query.call_count, 2)  # main and direct registration
                factory.assert_called_once()
                run.assert_called_once_with(transport="stdio")
                self.assertEqual(len(server.middleware), middleware_before + 1)

    def test_handshake_and_modern_discovery_validation_error_envelopes_and_all_methods(self):
        from mcp_adapter import register_bank_graph_tools
        from mcp_types import SERVER_INFO_META_KEY
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)
            make_archive(archive)
            for modern in (False, True):
                with self.subTest(modern=modern):
                    server = MCPServer("protocol-test", instructions="Keep OData target selection.")
                    @server.tool()
                    def existing_sentinel() -> dict:
                        """Existing unrelated registration."""
                        return {"unchanged": True}
                    # Observe the actual wire dict returned by the entire chain.
                    wire_results = []
                    async def observe(ctx, call_next):
                        result = await call_next(ctx)
                        if ctx.method == "tools/call":
                            wire_results.append(result)
                        return result
                    server.middleware.append(observe)
                    register_bank_graph_tools(server, archive)
                    async def exercise():
                        async with create_client_server_memory_streams() as (cs, ss):
                            async with anyio.create_task_group() as group:
                                low = server._lowlevel_server
                                group.start_soon(low.run, *ss, low.create_initialization_options())
                                async with ClientSession(*cs) as client:
                                    initial = await (client.discover() if modern else client.initialize())
                                    self.assertIn("bank_graph_contract", initial.instructions)
                                    self.assertIn("Keep OData target selection.", initial.instructions)
                                    if modern:
                                        self.assertEqual(client.protocol_version, "2026-07-28")
                                    self.assertEqual(len((await client.list_tools()).tools), 7)
                                    self.assertEqual(len((await client.list_resources()).resources), 2)
                                    for uri in ("bank-graph://contract", "bank-graph://manual"):
                                        self.assertTrue((await client.read_resource(uri)).contents)
                                    for name, args in (
                                        ("bank_graph_contract", {}), ("bank_graph_overview", {}),
                                        ("bank_graph_search", {"limit": 1}), ("bank_graph_get", {"node_id": "o11"}),
                                        ("bank_graph_links", {"node_id": "o11"}), ("bank_graph_aggregate", {}),
                                        ("existing_sentinel", {}),
                                    ):
                                        self.assertFalse((await client.call_tool(name, args)).is_error, name)
                                    for name, args in (
                                        ("bank_graph_contract", {"unknown": True}),
                                        ("bank_graph_search", {"filters": '{"direction":"inflow"}'}),
                                        ("bank_graph_links", {"node_id": "o11", "edge_types": '["CONTAINS"]'}),
                                        ("bank_graph_search", {"cursor": "x" * 8193}),
                                    ):
                                        self.assertTrue((await client.call_tool(name, args)).is_error, name)
                                        wire = wire_results[-1]
                                        self.assertIs(wire["isError"], True)
                                        self.assertTrue(wire["content"][0]["text"])
                                        if modern:
                                            self.assertEqual(wire["resultType"], "complete")
                                            self.assertEqual(wire["_meta"][SERVER_INFO_META_KEY], low.server_info_stamp)
                                        else:
                                            self.assertNotIn("resultType", wire)
                                            self.assertNotIn("_meta", wire)
                                    self.assertFalse((await client.call_tool("bank_graph_contract", {})).is_error)
                                group.cancel_scope.cancel()
                    anyio.run(exercise)

    def test_adapter_sibling_import_preserves_host_reader_both_orders_and_retries(self):
        # Each process begins with genuine host/adapter import order. Fixture
        # generation happens in this parent and cannot preload the child reader.
        code = textwrap.dedent("""
            import importlib.util, pathlib, sys, types
            from mcp.server import MCPServer
            root, archive = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[3])
            host = types.ModuleType('reader')
            host.marker = object()
            initial_path, initial_meta = sys.path[:], sys.meta_path[:]
            if sys.argv[2] == 'host-first':
                sys.modules['reader'] = host
            spec = importlib.util.spec_from_file_location('synthetic_host_adapter', root/'mcp_adapter.py')
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            if sys.argv[2] == 'adapter-first':
                assert 'reader' not in sys.modules
                sys.modules['reader'] = host
            import reader
            assert reader is host
            for i in range(8):
                bank = adapter.register_bank_graph_tools(MCPServer('isolated-'+str(i)), archive)
                assert bank.get('o11')['node']['id'] == 'o11'
                assert sys.modules['reader'] is host
            graph = archive/'g1.json'
            original = graph.read_bytes()
            try:
                graph.write_text('{bad')
                try:
                    adapter.register_bank_graph_tools(MCPServer('bad-source'), archive)
                    raise AssertionError('bad archive accepted')
                except adapter.QueryError:
                    pass
            finally:
                graph.write_bytes(original)
            bank = adapter.register_bank_graph_tools(MCPServer('retry-source'), archive)
            assert bank.get('o11')['node']['id'] == 'o11'
            assert sys.path == initial_path and sys.meta_path == initial_meta
            assert sys.modules['reader'] is host
            assert '_bank_graph_adapter.reader' not in sys.modules
        """)
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            make_archive(archive)
            for order in ('host-first', 'adapter-first'):
                with self.subTest(order=order):
                    result = subprocess.run([sys.executable, '-B', '-c', code,
                                             str((Path(__file__).resolve().parents[2] / "onec_bank_graph").resolve()), order, str(archive)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_sdk_supported_deep_values_exact_numbers_and_source_rejection(self):
        import mcp_adapter as adapter
        from reader import encoded, MAX_JSON_DEPTH, MAX_JSON_INTEGER_DIGITS
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            make_archive(archive)
            nested = json.loads('[' * (MAX_JSON_DEPTH - 5) +
                                '{"finite":0.1,"small":1e-300,"raw":"000123 1e-10000","unknown":null}' +
                                ']' * (MAX_JSON_DEPTH - 5))
            leaf = nested
            while isinstance(leaf, list):
                leaf = leaf[0]
            leaf['integer_at_limit'] = int('9' * MAX_JSON_INTEGER_DIGITS)
            leaf['integer_above_binary64'] = 9007199254740993
            leaf['subnormal'] = 5e-324
            leaf['numeric_string'] = '0.123456789012345678901234567890'
            graph = json.loads((archive/'g1.json').read_text())
            for node in graph['nodes']:
                node['properties']['opaque'] = nested
            graph['edges'][0]['opaque'] = nested
            (archive/'g1.json').write_text(json.dumps(graph))
            controls = json.loads((archive/'u1.json').read_text())
            controls['statements'][0]['source_unit_review']['opaque'] = nested
            (archive/'u1.json').write_text(json.dumps(controls))
            manifest = json.loads((archive/'manifest.json').read_text())
            manifest['status'] = nested
            manifest['inputs'][0]['source_ready'] = nested
            (archive/'manifest.json').write_text(json.dumps(manifest))
            before = {p.name: p.read_bytes() for p in archive.iterdir()}
            server = MCPServer('deep-exact-synthetic')
            reader = adapter.register_bank_graph_tools(server, archive)
            async def exercise():
                async with create_client_server_memory_streams() as (cs, ss):
                    async with anyio.create_task_group() as group:
                        low = server._lowlevel_server
                        group.start_soon(low.run, *ss, low.create_initialization_options())
                        async with ClientSession(*cs) as client:
                            await client.initialize()
                            calls = [('bank_graph_contract', {}, adapter.contract),
                                     ('bank_graph_overview', {}, reader.overview),
                                     ('bank_graph_search', {'limit':1}, lambda:reader.search(limit=1)),
                                     ('bank_graph_get', {'node_id':'o11'}, lambda:reader.get('o11')),
                                     ('bank_graph_links', {'node_id':'s1','limit':100}, lambda:reader.links('s1',limit=100)),
                                     ('bank_graph_aggregate', {}, reader.aggregate)]
                            for name, args, oracle in calls:
                                result = await client.call_tool(name, args)
                                self.assertFalse(result.is_error, name)
                                self.assertEqual(encoded(result.structured_content), encoded(oracle()), name)
                                self.assertEqual(encoded(json.loads(result.content[0].text)), encoded(oracle()), name)
                            search = await client.call_tool('bank_graph_search', {'filters':{'operation_id':'o11'}})
                            exact_id = search.structured_content['items'][0]['id']
                            result = await client.call_tool('bank_graph_get', {'node_id':exact_id})
                            self.assertFalse(result.is_error)
                            self.assertEqual(encoded(result.structured_content['node']), encoded(graph['nodes'][3]))
                            result.structured_content['node']['properties']['opaque'] = 'caller'
                            self.assertEqual(encoded(reader.get(exact_id)['node']), encoded(graph['nodes'][3]))
                        group.cancel_scope.cancel()
            anyio.run(exercise)
            self.assertEqual(before, {p.name: p.read_bytes() for p in archive.iterdir()})
            for token in ('['*500+'null'+']'*500, '0.123456789012345678901234567890', '1e-10000'):
                make_archive(archive)
                path = archive/'g1.json'
                original = path.read_text().rstrip()
                path.write_text(original[:-1] + ',"opaque":' + token + '}')
                before = {p.name: p.read_bytes() for p in archive.iterdir()}
                with self.assertRaises(adapter.QueryError):
                    adapter.register_bank_graph_tools(MCPServer('unsupported-source'), archive)
                self.assertEqual(before, {p.name: p.read_bytes() for p in archive.iterdir()})

    def test_initialize_discovery_resources_queries_and_errors(self):
        from mcp_adapter import register_bank_graph_tools
        from reader import contract
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_archive(root)
            server = MCPServer("isolated-test", instructions=contract()["instructions"])
            register_bank_graph_tools(server, root)

            async def exercise():
                async with create_client_server_memory_streams() as (client_streams, server_streams):
                    async with anyio.create_task_group() as group:
                        low = server._lowlevel_server
                        group.start_soon(low.run, *server_streams, low.create_initialization_options())
                        async with ClientSession(*client_streams) as client:
                            init = await client.initialize()
                            self.assertIn("inflow", init.instructions)
                            listed = await client.list_tools()
                            self.assertEqual(len(listed.tools), 6)
                            for tool in listed.tools:
                                self.assertTrue(tool.annotations.read_only_hint)
                                self.assertIsNotNone(tool.input_schema)
                                self.assertIsNotNone(tool.output_schema)
                            resources = await client.list_resources()
                            self.assertEqual(len(resources.resources), 2)
                            resource = await client.read_resource("bank-graph://contract")
                            self.assertTrue(json.loads(resource.contents[0].text)["read_only"])
                            overview = await client.call_tool("bank_graph_overview", {})
                            self.assertFalse(overview.is_error)
                            self.assertEqual(overview.structured_content["node_counts"]["Operation"], 9)
                            search = await client.call_tool("bank_graph_search", {"filters": {"direction": "inflow"}, "limit": 1})
                            self.assertFalse(search.is_error)
                            cursor = search.structured_content["next_cursor"]
                            page = await client.call_tool("bank_graph_search", {"filters": {"direction": "inflow"}, "limit": 1, "cursor": cursor})
                            self.assertNotEqual(search.structured_content["items"][0]["id"], page.structured_content["items"][0]["id"])
                            get = await client.call_tool("bank_graph_get", {"node_id": "o11"})
                            self.assertIn("\n", get.structured_content["node"]["properties"]["purpose_raw"])
                            links = await client.call_tool("bank_graph_links", {"node_id": "o12", "edge_types": ["HAS_COMPONENT"]})
                            self.assertEqual(links.structured_content["items"][0]["neighbor"]["type"], "AmountComponent")
                            aggregate = await client.call_tool("bank_graph_aggregate", {"group_by": "none", "limit": 100})
                            self.assertEqual(aggregate.structured_content["total"], 9)
                            for invalid in (True, 0, 101, 1.5):
                                rejected = await client.call_tool("bank_graph_search", {"limit": invalid})
                                self.assertTrue(rejected.is_error)
                            error = await client.call_tool("bank_graph_search", {"filters": {"arbitrary": "value"}})
                            self.assertTrue(error.is_error)
                            for name, args in (
                                ("bank_graph_contract", {"archive": "/arbitrary"}),
                                ("bank_graph_overview", {"unknown": True}),
                                ("bank_graph_search", {"filters": '{"direction":"inflow"}'}),
                                ("bank_graph_links", {"node_id": "o11", "edge_types": '["CONTAINS"]'}),
                                ("bank_graph_get", {"node_id": "o11", "unknown": 1}),
                                ("bank_graph_aggregate", {"unknown": 1}),
                                ("bank_graph_search", {"cursor": "x" * 8193}),
                            ):
                                rejected = await client.call_tool(name, args)
                                self.assertTrue(rejected.is_error, (name, args))
                            for tool in listed.tools:
                                self.assertIs(tool.input_schema["additionalProperties"], False)
                        group.cancel_scope.cancel()
            anyio.run(exercise)


if __name__ == "__main__":
    unittest.main()
