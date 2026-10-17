"""Synthetic checks for the macOS side of the read-only source bridge."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from agent_bridge import odata, source
from agent_bridge.config import ConfigError, load_config
from agent_bridge.core import Bridge
from agent_bridge.ledger import Ledger, LedgerError, year_period


class MacBridgeTests(unittest.TestCase):
    def test_transport_health_uses_fixed_identity_command(self):
        identity = "/Users/example/.ssh/onec_bridge_windows_ed25519"
        host_key_alias = "win77-bridge.local"
        remote_command = "C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe -NoProfile -File C:\\1c-migration\\repo\\tools\\agent-bridge\\windows\\Invoke-77Bridge.ps1"
        def fake_run(argv, **kwargs):
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("IdentitiesOnly=yes", argv)
            self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
            self.assertEqual(argv[argv.index("-i") + 1], identity)
            self.assertEqual(argv[argv.index("HostKeyAlias=" + host_key_alias) - 1], "-o")
            self.assertEqual(argv[-1], "cmd.exe /d /c whoami")
            self.assertNotIn(remote_command, argv)
            self.assertEqual(kwargs["input"], "")
            return type("Result", (), {"returncode": 0,
                                        "stdout": "DESKTOP-RQ7MHS4\\onecbridge\r\n",
                                        "stderr": "raw host and user details"})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.transport_health({"host": "windows-host", "user": "onecbridge",
                                              "identityFile": identity,
                                              "hostKeyAlias": host_key_alias,
                                              "remoteCommand": remote_command},
                                             source_alias="src77-2010-2012")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "transport_health")
        self.assertTrue(result["checks"]["strictHostKeyChecking"])
        self.assertFalse(result["checks"]["oneCAccess"])
        self.assertTrue(result["data"]["identityMatchesConfiguredUser"])
        encoded = json.dumps(result)
        self.assertNotIn("DESKTOP-RQ7MHS4", encoded)
        self.assertNotIn("onecbridge", encoded)
        self.assertNotIn("raw host", encoded)

    def test_transport_health_rejects_non_windows_identity_output(self):
        def fake_run(_argv, **_kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "onecbridge\n",
                                        "stderr": ""})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.transport_health({"host": "windows-host", "user": "onecbridge",
                                              "remoteCommand": "adapter.ps1"},
                                             source_alias="src")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "SSH_IDENTITY_OUTPUT_INVALID")
        self.assertNotIn("onecbridge", json.dumps(result))

    def test_transport_health_returns_sanitized_ssh_errors(self):
        def fake_run(_argv, **_kwargs):
            return type("Result", (), {"returncode": 255, "stdout": "",
                                        "stderr": "Permission denied for reader@windows-host"})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.transport_health({"host": "windows-host", "user": "reader",
                                              "remoteCommand": "adapter.ps1"},
                                             source_alias="src")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "SSH_AUTH_FAILED")
        encoded = json.dumps(result)
        self.assertNotIn("windows-host", encoded)
        self.assertNotIn("reader", encoded)
        self.assertNotIn("Permission denied", encoded)

    def test_transport_rejects_unsafe_host_key_alias(self):
        with self.assertRaisesRegex(source.SourceError, "host key alias"):
            source.SSHExecTransport()._argv(
                {"host": "windows-host", "user": "reader", "hostKeyAlias": "bad alias",
                 "remoteCommand": "adapter.ps1"},
                "adapter.ps1",
            )

    def test_source_request_uses_configured_default_timeout(self):
        class RecordingTransport:
            def __init__(self):
                self.timeouts = []

            def exchange(self, _source, payload, timeout):
                self.timeouts.append(timeout)
                return {"schemaVersion": "1.0", "operation": payload["operation"],
                        "status": "ok", "requestId": payload["requestId"]}

        transport = RecordingTransport()
        runtime_source = {"host": "host", "user": "reader", "remoteCommand": "read.ps1",
                          "requestTimeoutSeconds": 210}
        source.request(runtime_source, "health", source_alias="src77-2010-2012",
                       transport=transport)
        source.request(runtime_source, "health", source_alias="src77-2010-2012",
                       timeout=9, transport=transport)
        source.request({**runtime_source, "requestTimeoutSeconds": 241}, "health",
                       source_alias="src77-2010-2012", timeout=8, transport=transport)
        self.assertEqual(transport.timeouts, [210, 9, 8])
        with self.assertRaisesRegex(ValueError, "request timeout"):
            source.request({**runtime_source, "requestTimeoutSeconds": 241}, "health",
                           source_alias="src77-2010-2012", transport=transport)

    def test_source_request_transport_allows_240_second_cap(self):
        def fake_run(_argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 240)
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "health", "status": "ok",
                "requestId": request["requestId"]})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.SSHExecTransport().exchange(
                {"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                {"schemaVersion": "1.0", "requestId": "synthetic", "operation": "health",
                 "sourceAlias": "src77-2010-2012"},
                999,
            )
        self.assertEqual(result["status"], "ok")

    def test_source_exchange_uses_local_single_flight_lock(self):
        flock_calls = []

        def fake_flock(_fd, operation):
            flock_calls.append(operation)

        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "health", "status": "ok",
                "requestId": request["requestId"]})})()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TMPDIR": tmp}):
                with patch("agent_bridge.source.fcntl.flock", side_effect=fake_flock):
                    with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
                        result = source.SSHExecTransport().exchange(
                            {"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                            {"schemaVersion": "1.0", "requestId": "synthetic",
                             "operation": "health", "sourceAlias": "src"},
                            30,
                        )
            locks = list((Path(tmp) / "agent_bridge_1c_locks").glob("source-*.lock"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(flock_calls, [source.fcntl.LOCK_EX, source.fcntl.LOCK_UN])
        self.assertEqual(len(locks), 1)

    def test_overlapping_source_page_exchanges_are_serialized(self):
        if source.fcntl is None:
            self.skipTest("flock is available only on the Mac/Linux bridge")

        first_exchange_started = threading.Event()
        release_first_exchange = threading.Event()
        second_lock_attempted = threading.Event()
        second_exchange_started = threading.Event()
        thread_context = threading.local()
        state_lock = threading.Lock()
        active_exchanges = 0
        max_active_exchanges = 0
        exchange_count = 0
        original_flock = source.fcntl.flock

        def tracked_flock(fd, operation):
            if operation == source.fcntl.LOCK_EX and getattr(thread_context, "role", None) == "second":
                second_lock_attempted.set()
            return original_flock(fd, operation)

        def fake_run(_argv, **kwargs):
            nonlocal active_exchanges, max_active_exchanges, exchange_count
            with state_lock:
                exchange_count += 1
                this_exchange = exchange_count
                active_exchanges += 1
                max_active_exchanges = max(max_active_exchanges, active_exchanges)
            try:
                if this_exchange == 1:
                    first_exchange_started.set()
                    if not release_first_exchange.wait(3):
                        raise AssertionError("first synthetic SSH exchange was not released")
                else:
                    second_exchange_started.set()
                request = json.loads(kwargs["input"])
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": json.dumps({
                        "schemaVersion": "1.0", "operation": request["operation"],
                        "status": "ok", "requestId": request["requestId"],
                    }),
                    "stderr": "",
                })()
            finally:
                with state_lock:
                    active_exchanges -= 1

        transport = source.SSHExecTransport()
        runtime_source = {"host": "host", "user": "reader", "remoteCommand": "read.ps1"}

        def exchange(role, operation):
            thread_context.role = role
            return transport.exchange(
                runtime_source,
                {"schemaVersion": "1.0", "requestId": role,
                 "operation": operation, "sourceAlias": "src"},
                30,
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"TMPDIR": tmp}):
                with patch("agent_bridge.source.fcntl.flock", side_effect=tracked_flock):
                    with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            first = pool.submit(exchange, "first", "read_page")
                            self.assertTrue(first_exchange_started.wait(3))
                            second = pool.submit(exchange, "second", "read_documents_page")
                            try:
                                self.assertTrue(second_lock_attempted.wait(3))
                                self.assertFalse(second_exchange_started.wait(0.1))
                            finally:
                                release_first_exchange.set()
                            first_result = first.result(timeout=3)
                            second_result = second.result(timeout=3)

        self.assertEqual(first_result["status"], "ok")
        self.assertEqual(second_result["status"], "ok")
        self.assertEqual(exchange_count, 2)
        self.assertEqual(max_active_exchanges, 1)

    def test_source_exchange_limits_utf8_response_bytes(self):
        response = '{"message":"' + ("я" * 4_000_001) + '"}'
        self.assertLess(len(response), 8_000_000)
        self.assertGreater(len(response.encode("utf-8")), 8_000_000)

        def fake_run(_argv, **_kwargs):
            return type("Result", (), {"returncode": 0, "stdout": response, "stderr": ""})()

        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "exceeds size limit"):
                source.SSHExecTransport().exchange(
                    {"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                    {"schemaVersion": "1.0", "requestId": "synthetic",
                     "operation": "read_page", "sourceAlias": "src"},
                    30,
                )

    def test_source_schema_advertises_complete_reader_operations(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertEqual(request["operation"], "schema")
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "schema", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "datasets": [{"kind": "document", "type": "Invoice",
                                  "status": "configured-unverified"}],
                    "supportedKinds": ["document", "reference", "operation",
                                       "operationEntry"],
                    "provisionalKinds": ["constant"],
                    "unsupportedKinds": ["register", "periodicDocumentSelection",
                                         "customOperationAttributes", "customEntryAttributes"],
                    "readOperations": ["document_presence", "read_page", "read_documents_page",
                                       "read_document_lines_page", "read_document_lines_batch", "read_constants_page",
                                       "read_operations_page", "read_operation_entries_page",
                                       "read_periodic_history_page"],
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                    "schema", source_alias="src77-2010-2012")
        self.assertIn("document_presence", result["data"]["readOperations"])
        self.assertIn("read_constants_page", result["data"]["readOperations"])
        self.assertIn("read_document_lines_batch", result["data"]["readOperations"])
        self.assertIn("read_operations_page", result["data"]["readOperations"])
        self.assertIn("read_periodic_history_page", result["data"]["readOperations"])

    def test_source_schema_accepts_legacy_worker_without_batch_capability(self):
        schema = {"datasets": [], "supportedKinds": ["document", "reference", "operation", "operationEntry"],
                  "provisionalKinds": ["constant"], "unsupportedKinds": ["register"],
                  "readOperations": ["document_presence", "read_page", "read_documents_page",
                                     "read_document_lines_page", "read_constants_page",
                                     "read_operations_page", "read_operation_entries_page",
                                     "read_periodic_history_page"]}
        source._validate_source_schema(schema)

    def test_document_lines_batch_rejects_cross_scope_success_pages(self):
        requested_period = {"from": "2012-01-01", "toExclusive": "2013-01-01"}
        base_page = {"kind": "document_lines", "type": "ПоступлениеТоваров",
                     "document": None, "lines": [], "hasMore": False, "nextCursor": None,
                     "period": requested_period, "snapshotStatus": "none",
                     "stableIdentity": "unverified",
                     "coverage": {"status": "document-not-found", "unsupported": []}}
        inputs = [{"documentCursor": "7", "lineCursor": "0", "limit": 1}]
        for change in ({"type": "РасходТоваров"},
                       {"period": {"from": "2011-01-01", "toExclusive": "2012-01-01"}}):
            with self.subTest(change=change), self.assertRaisesRegex(source.SourceError, "outside the requested scope"):
                page = {**base_page, **change}
                source._validate_line_batch({"items": [{"documentCursor": "7", "lineCursor": "0",
                                                          "limit": 1, "status": "ok", "data": page}],
                                             "adapterElapsedMs": 0}, inputs,
                                            document_type="ПоступлениеТоваров", period=requested_period)

    def test_document_lines_batch_rejects_document_date_outside_period(self):
        period = {"from": "2012-01-01", "toExclusive": "2013-01-01"}
        page = {"kind": "document_lines", "type": "ПоступлениеТоваров",
                "document": {"offset": "7", "date": "2013-01-01"},
                "lines": [], "hasMore": False, "nextCursor": None,
                "period": period, "snapshotStatus": "none", "stableIdentity": "unverified",
                "coverage": {"status": "metadata-driven-unverified", "unsupported": []}}
        with self.assertRaisesRegex(source.SourceError, "document outside the requested period"):
            source._validate_line_batch({"items": [{"documentCursor": "7", "lineCursor": "0",
                                                      "limit": 1, "status": "ok", "data": page}],
                                         "adapterElapsedMs": 0},
                                        [{"documentCursor": "7", "lineCursor": "0", "limit": 1}],
                                        document_type="ПоступлениеТоваров", period=period)

    def test_document_presence_page_contract(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertEqual(request["operation"], "document_presence")
            self.assertEqual(request["period"], {"from": "2010-01-01", "toExclusive": "2011-01-01"})
            self.assertEqual(request["cursor"], "20")
            self.assertEqual(request["limit"], 1000)
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "document_presence", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "kind": "document_presence",
                    "documentsScanned": 7,
                    "groups": [{
                        "documentType": "АвансовыйОтчет",
                        "presentation": "Авансовый отчет",
                        "count": 7,
                        "firstDate": "2010-02-01",
                        "lastDate": "2010-12-31",
                        "postedCount": 6,
                        "unpostedCount": 1,
                        "unknownPostedCount": 0,
                        "metadata": {
                            "known": True,
                            "id": "12",
                            "headerAttributeCount": 17,
                            "tableAttributeCount": 15,
                        },
                    }],
                    "hasMore": True,
                    "nextCursor": "27",
                    "coverage": {"status": "presence-page-unverified", "unsupported": []},
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                    "document_presence", source_alias="src77-2010-2012",
                                    from_date="2010-01-01", to_exclusive="2011-01-01",
                                    cursor="20", limit=1000)
        self.assertEqual(result["data"]["groups"][0]["documentType"], "АвансовыйОтчет")
        with self.assertRaisesRegex(ValueError, "presence limit"):
            source.request({}, "document_presence", source_alias="src77-2010-2012",
                           from_date="2010-01-01", to_exclusive="2011-01-01",
                           limit=5001)

    def test_source_contract_and_bounded_page(self):
        identity = "/Users/example/.ssh/onec_bridge_windows_ed25519"
        def fake_run(argv, **kwargs):
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("IdentitiesOnly=yes", argv)
            self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
            self.assertIn("ForwardAgent=no", argv)
            self.assertIn("PasswordAuthentication=no", argv)
            self.assertIn("UpdateHostKeys=no", argv)
            self.assertEqual(argv[argv.index("-i") + 1], identity)
            self.assertNotIn('"password"', kwargs["input"].lower())
            request = json.loads(kwargs["input"])
            self.assertEqual(request["period"], {"from": "2012-01-01", "toExclusive": "2013-01-01"})
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_documents_page", "status": "ok",
                "requestId": request["requestId"], "data": {"rows": [{"key": "synthetic-1"}],
                "hasMore": False, "nextCursor": None,
                "coverage": {"status": "configured-fields-unverified", "unsupported": []}}})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "identityFile": identity,
                                     "remoteCommand": "read.ps1"},
                                    "read_documents_page", source_alias="source", document_type="Invoice",
                                    from_date="2012-01-01", to_exclusive="2013-01-01")
        self.assertEqual(len(result["data"]["rows"]), 1)
        with self.assertRaises(ValueError):
            source.request({}, "read_documents_page", source_alias="x", document_type="A",
                           from_date="2012-01-01", to_exclusive="2012-12-31", limit=0)

    def test_source_page_separates_adapter_and_bridge_metrics(self):
        wire_responses = []

        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            answer = {
                "schemaVersion": "1.0", "operation": "read_page", "status": "ok",
                "requestId": request["requestId"],
                "data": {"rows": [], "hasMore": False, "nextCursor": None,
                         "adapterElapsedMs": 23,
                         "coverage": {"status": "configured-fields-unverified", "unsupported": []}},
            }
            serialized = json.dumps(answer, ensure_ascii=False)
            wire_responses.append(serialized)
            return type("Result", (), {"returncode": 0, "stdout": serialized})()

        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request(
                {"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                "read_page", source_alias="src77-2010-2012",
                dataset_kind="reference", dataset_type="Контрагенты",
            )
        self.assertEqual(result["data"]["adapterElapsedMs"], 23)
        metrics = result["bridgeMetrics"]
        self.assertEqual(set(metrics), {"roundTripElapsedMs", "responseJsonUtf8Bytes"})
        self.assertIsInstance(metrics["roundTripElapsedMs"], int)
        self.assertGreaterEqual(metrics["roundTripElapsedMs"], 0)
        self.assertEqual(metrics["responseJsonUtf8Bytes"], len(wire_responses[0].encode("utf-8")))
        self.assertNotIn("_bridgeTransportMetrics", result)

    def test_source_pages_reject_invalid_adapter_elapsed(self):
        source_info = {"host": "host", "user": "reader", "remoteCommand": "read.ps1"}
        for invalid in (True, -1, 600_001, 1.5):
            with self.subTest(invalid=invalid):
                def fake_exchange(_source, payload, _timeout):
                    return {
                        "schemaVersion": "1.0", "operation": payload["operation"], "status": "ok",
                        "requestId": payload["requestId"],
                        "data": {"rows": [], "hasMore": False, "nextCursor": None,
                                 "adapterElapsedMs": invalid,
                                 "coverage": {"status": "configured-fields-unverified", "unsupported": []}},
                    }

                transport = type("Transport", (), {"exchange": staticmethod(fake_exchange)})()
                with self.assertRaisesRegex(source.SourceError, "adapterElapsedMs"):
                    source.request(source_info, "read_page", source_alias="src77-2010-2012",
                                   dataset_kind="reference", dataset_type="Контрагенты",
                                   transport=transport)

    def test_metadata_inventory_contract(self):
        other_sections = {
            name: {"present": False, "boundedObjectCount": 0, "countLimit": 100000}
            for name in ("constants", "enumerations", "registers", "journals",
                         "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors")
        }
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertEqual(request["operation"], "metadata_inventory")
            self.assertEqual(set(request), {"schemaVersion", "requestId", "operation", "sourceAlias"})
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "metadata_inventory", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "status": "metadata_catalog",
                    "counts": {"catalogs": 1, "documents": 1, "attributes": 2},
                    "catalogs": [{"id": "1", "name": "SyntheticCatalog", "attributes": [
                        {"id": "2", "name": "Rate", "typeCode": "N", "length": 10,
                         "precision": 2, "refTypeId": ""}
                    ]}],
                    "documents": [{"id": "3", "name": "SyntheticDocument",
                                   "headAttributes": [
                                       {"id": "4", "name": "Total", "typeCode": "N",
                                        "length": 15, "precision": 2, "refTypeId": ""}
                                   ],
                                   "tableAttributes": []}],
                    "otherSections": other_sections,
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                    "metadata_inventory", source_alias="src77-2010-2012")
        self.assertEqual(result["data"]["counts"]["catalogs"], 1)

    def test_metadata_inventory_accepts_extended_surface_contract(self):
        other_sections = {
            name: {"present": name == "constants", "boundedObjectCount": 1 if name == "constants" else 0, "countLimit": 100000}
            for name in ("constants", "enumerations", "registers", "journals",
                         "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors")
        }
        section_coverage = {
            "constants": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                          "countLimit": 100000, "sectionNames": ["Consts", "Const"]},
            "catalogs": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                         "countLimit": 100000, "sectionNames": ["SbCnts"]},
            "documents": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                          "countLimit": 100000, "sectionNames": ["Documents"]},
            "enumerations": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                             "countLimit": 100000, "sectionNames": ["Enums", "Enum"]},
            "registers": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                          "countLimit": 100000, "sectionNames": ["Registers"]},
            "journals": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                         "countLimit": 100000, "sectionNames": ["Journals", "JournalsDef", "DocJournals", "GenJrnlFldDef"]},
            "documentNumerators": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                                   "countLimit": 100000, "sectionNames": ["DocNumRef"]},
            "chartsOfAccounts": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                                 "countLimit": 100000, "sectionNames": ["Accounts", "ChartsOfAccounts"]},
            "reports": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                        "countLimit": 100000, "sectionNames": ["Reports"]},
            "dataProcessors": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                               "countLimit": 100000, "sectionNames": ["DataProcessors", "DataProcessings"]},
            "unmapped": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 3,
                         "countLimit": 100000},
        }
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "metadata_inventory", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "status": "metadata_catalog",
                    "counts": {"constants": 1, "catalogs": 1, "documents": 1, "attributes": 2,
                               "catalogAttributes": 1, "documentHeadAttributes": 1,
                               "documentTableAttributes": 0, "totalMetadataAttributes": 3},
                    "constants": [{"id": "10", "name": "DefaultCurrency", "typeCode": "S",
                                   "length": 10, "precision": 0, "refTypeId": "0"}],
                    "catalogs": [{"id": "1", "name": "SyntheticCatalog", "attributes": [
                        {"id": "2", "name": "Rate", "typeCode": "N", "length": 10,
                         "precision": 2, "refTypeId": ""}
                    ]}],
                    "documents": [{"id": "3", "name": "SyntheticDocument",
                                   "headAttributes": [
                                       {"id": "4", "name": "Total", "typeCode": "N",
                                        "length": 15, "precision": 2, "refTypeId": ""}
                                   ],
                                   "tableAttributes": []}],
                    "otherSections": other_sections,
                    "sectionCoverage": section_coverage,
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                    "metadata_inventory", source_alias="src77-2010-2012")
        self.assertEqual(result["data"]["counts"]["totalMetadataAttributes"], 3)

    def test_metadata_inventory_rejects_unbounded_extended_counts(self):
        other_sections = {
            name: {"present": False, "boundedObjectCount": 0, "countLimit": 100000}
            for name in ("constants", "enumerations", "registers", "journals",
                         "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors")
        }
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "metadata_inventory", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "status": "metadata_catalog",
                    "counts": {"catalogs": 0, "documents": 0, "attributes": 0,
                               "totalMetadataAttributes": 200001},
                    "catalogs": [],
                    "documents": [],
                    "otherSections": other_sections,
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "bounded counts"):
                source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                               "metadata_inventory", source_alias="src77-2010-2012")

    def test_metadata_inventory_rejects_generic_section_names(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "metadata_inventory", "status": "ok",
                "requestId": request["requestId"], "data": {
                    "status": "metadata_catalog",
                    "counts": {"catalogs": 0, "documents": 0, "attributes": 0},
                    "catalogs": [],
                    "documents": [],
                    "topSections": [{"name": "TaskItem", "childCount": 1}],
                    "otherSections": {},
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "invalid metadata inventory"):
                source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                               "metadata_inventory", source_alias="src77-2010-2012")

    def test_source_request_raises_sanitized_adapter_error(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "health", "status": "error",
                "requestId": request["requestId"],
                "error": {"code": "COM_ACCESS_DENIED", "message": "raw local path C:\\secret"},
                "data": {"rows": ["private accounting row"]},
                "localPath": "C:\\secret",
            })})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaises(source.SourceError) as caught:
                source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                               "health", source_alias="src77-2010-2012")
        self.assertEqual(str(caught.exception), "Windows adapter error: COM_ACCESS_DENIED")

    def test_generic_reference_page_unicode_and_hyphen_alias(self):
        def fake_run(argv, **kwargs):
            self.assertNotIn("-i", argv)
            self.assertNotIn("IdentitiesOnly=yes", argv)
            request = json.loads(kwargs["input"])
            self.assertEqual(request["sourceAlias"], "src77-2010-2012")
            self.assertEqual(request["kind"], "reference")
            self.assertEqual(request["type"], "Контрагенты")
            self.assertNotIn("period", request)
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_page", "status": "ok",
                "requestId": request["requestId"], "data": {"rows": [], "hasMore": False,
                "nextCursor": None,
                "coverage": {"status": "metadata-driven-unverified", "unsupported": []}}})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                    "read_page", source_alias="src77-2010-2012",
                                    dataset_kind="reference", dataset_type="Контрагенты")
        self.assertEqual(result["status"], "ok")
        with self.assertRaises(ValueError):
            source.request({}, "read_page", source_alias="src77-2010-2012",
                           dataset_kind="register", dataset_type="Движения")

    def test_extended_reader_pages_have_explicit_coverage_and_bounded_cursors(self):
        responses = []
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            responses.append(request)
            if request["operation"] == "read_document_lines_page":
                self.assertEqual(request["documentCursor"], "12")
                self.assertEqual(request["lineCursor"], "0")
                data = {
                    "lines": [{"lineNumber": 1, "attributes": {}}],
                    "hasMore": True,
                    "nextCursor": {"documentCursor": "12", "lineCursor": "1"},
                    "adapterElapsedMs": 18,
                    "coverage": {"status": "partial-unsupported-values",
                                 "unsupported": [{"reason": "attribute-read-failed"}]},
                }
            elif request["operation"] == "read_constants_page":
                data = {
                    "rows": [{"name": "DefaultCurrency",
                              "value": "USD"}],
                    "hasMore": False,
                    "nextCursor": None,
                    "coverage": {"status": "provisional-constant-context",
                                 "unsupported": []},
                }
            elif request["operation"] == "read_operations_page":
                self.assertEqual(request["period"], {"from": "2012-01-01", "toExclusive": "2013-01-01"})
                data = {
                    "rows": [{"operationCursor": "12", "date": "2012-02-03",
                              "description": "synthetic", "entryCount": 2,
                              "document": {"serializationStatus": "structural-unverified",
                                           "date": "2012-02-03", "number": "5",
                                           "identityStatus": "unverified", "type": "unknown"}}],
                    "hasMore": True,
                    "nextCursor": "13",
                    "coverage": {"status": "standard-fields-only-unverified",
                                 "unsupported": []},
                }
            elif request["operation"] == "read_operation_entries_page":
                self.assertEqual(request["operationCursor"], "12")
                self.assertEqual(request["entryCursor"], "0")
                data = {
                    "operation": {"operationCursor": "12", "date": "2012-02-03"},
                    "entries": [{"entryCursor": "0", "number": 1,
                                 "sum": 10, "debit": {}, "credit": {}}],
                    "hasMore": True,
                    "nextCursor": {"operationCursor": "12", "entryCursor": "1"},
                    "coverage": {"status": "partial-unsupported-values",
                                 "unsupported": [{"reason": "standard-field-unavailable"}]},
                }
            else:
                raise AssertionError(request["operation"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": request["operation"], "status": "ok",
                "requestId": request["requestId"], "data": data})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            line_page = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                       "read_document_lines_page", source_alias="src77-2010-2012",
                                       document_type="ПоступлениеТоваров",
                                       from_date="2012-01-01", to_exclusive="2013-01-01",
                                       document_cursor="12", line_cursor="0", limit=1)
            constants = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                       "read_constants_page", source_alias="src77-2010-2012",
                                       limit=1)
            operations = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                        "read_operations_page", source_alias="src77-2010-2012",
                                        from_date="2012-01-01", to_exclusive="2013-01-01",
                                        limit=1)
            entries = source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                                     "read_operation_entries_page", source_alias="src77-2010-2012",
                                     from_date="2012-01-01", to_exclusive="2013-01-01",
                                     operation_cursor="12", entry_cursor="0", limit=1)
        self.assertEqual(line_page["data"]["nextCursor"]["lineCursor"], "1")
        self.assertEqual(line_page["data"]["adapterElapsedMs"], 18)
        self.assertEqual(constants["data"]["coverage"]["status"], "provisional-constant-context")
        self.assertEqual(operations["data"]["nextCursor"], "13")
        self.assertEqual(entries["data"]["nextCursor"]["entryCursor"], "1")
        self.assertEqual([item["operation"] for item in responses],
                         ["read_document_lines_page", "read_constants_page",
                          "read_operations_page", "read_operation_entries_page"])
        with self.assertRaisesRegex(ValueError, "line cursor"):
            source.request({}, "read_document_lines_page", source_alias="src77-2010-2012",
                           document_type="ПоступлениеТоваров",
                           from_date="2012-01-01", to_exclusive="2013-01-01",
                           document_cursor="12", line_cursor="1000001")
        with self.assertRaisesRegex(ValueError, "entry cursor"):
            source.request({}, "read_operation_entries_page", source_alias="src77-2010-2012",
                           from_date="2012-01-01", to_exclusive="2013-01-01",
                           operation_cursor="12", entry_cursor="1000001")

    def test_document_lines_batch_preserves_cursors_and_isolates_item_errors(self):
        items = [
            {"documentCursor": "12", "lineCursor": "0", "limit": 1},
            {"documentCursor": "13", "lineCursor": "4", "limit": 5},
            {"documentCursor": "14", "lineCursor": "2", "limit": 3},
        ]

        def line_page(document_cursor, line_cursor, limit):
            return {
                "kind": "document_lines", "type": "ПоступлениеТоваров",
                "document": {"offset": document_cursor, "date": "2012-03-04",
                              "number": "7", "posted": None, "attributes": {},
                              "declaredLineCount": 2},
                "lines": [{"lineNumber": int(line_cursor) + 1, "attributes": {}}],
                "hasMore": True,
                "nextCursor": {"documentCursor": document_cursor,
                               "lineCursor": str(int(line_cursor) + 1)},
                "coverage": {"status": "metadata-driven-unverified", "unsupported": []},
                "period": {"from": "2012-01-01", "toExclusive": "2013-01-01"},
            }

        calls = []

        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            calls.append(request)
            self.assertEqual(request["operation"], "read_document_lines_batch")
            self.assertEqual(request["documentType"], "ПоступлениеТоваров")
            self.assertEqual(request["period"], {"from": "2012-01-01", "toExclusive": "2013-01-01"})
            self.assertEqual(request["items"], items)
            results = [
                {"documentCursor": "12", "lineCursor": "0", "limit": 1,
                 "status": "ok", "data": line_page("12", "0", 1)},
                {"documentCursor": "13", "lineCursor": "4", "limit": 5,
                 "status": "ok", "data": {
                     "kind": "document_lines", "type": "ПоступлениеТоваров",
                     "document": None, "lines": [], "hasMore": False,
                     "nextCursor": None,
                     "period": {"from": "2012-01-01", "toExclusive": "2013-01-01"},
                     "snapshotStatus": "none", "stableIdentity": "unverified",
                     "coverage": {"status": "document-not-found", "unsupported": []},
                 }},
                {"documentCursor": "14", "lineCursor": "2", "limit": 3,
                 "status": "error", "error": {"code": "LINE_READ_FAILED"}},
            ]
            answer = {"schemaVersion": "1.0", "operation": request["operation"],
                      "status": "ok", "requestId": request["requestId"],
                      "data": {"items": results, "adapterElapsedMs": 37}}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(answer)})()

        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request(
                {"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                "read_document_lines_batch", source_alias="src77-2010-2012",
                document_type="ПоступлениеТоваров", from_date="2012-01-01",
                to_exclusive="2013-01-01", line_items=items,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["data"]["adapterElapsedMs"], 37)
        self.assertEqual(result["data"]["items"][0]["data"]["nextCursor"],
                         {"documentCursor": "12", "lineCursor": "1"})
        self.assertEqual(result["data"]["items"][1]["data"]["coverage"]["status"],
                         "document-not-found")
        self.assertEqual(result["data"]["items"][2]["error"]["code"], "LINE_READ_FAILED")

    def test_document_lines_batch_enforces_bounds_unique_cursors_and_response_enum(self):
        source_info = {"host": "host", "user": "reader", "remoteCommand": "read.ps1"}
        base = dict(source_alias="src77-2010-2012", document_type="ПоступлениеТоваров",
                    from_date="2012-01-01", to_exclusive="2013-01-01")
        with self.assertRaisesRegex(ValueError, "1..10"):
            source.request({}, "read_document_lines_batch", **base, line_items=[])
        with self.assertRaisesRegex(ValueError, "1..10"):
            source.request({}, "read_document_lines_batch", **base,
                           line_items=[{"documentCursor": str(i), "lineCursor": "0", "limit": 1}
                                       for i in range(11)])
        duplicate = [{"documentCursor": "1", "lineCursor": "0", "limit": 1},
                     {"documentCursor": "01", "lineCursor": "3", "limit": 2}]
        with self.assertRaisesRegex(ValueError, "unique"):
            source.request({}, "read_document_lines_batch", **base, line_items=duplicate)
        invalid_limit = [{"documentCursor": "1", "lineCursor": "0", "limit": 101}]
        with self.assertRaisesRegex(ValueError, "1..100"):
            source.request({}, "read_document_lines_batch", **base, line_items=invalid_limit)
        with self.assertRaisesRegex(ValueError, "line cursor"):
            source.request({}, "read_document_lines_batch", **base,
                           line_items=[{"documentCursor": "1", "lineCursor": "1000001", "limit": 1}])
        with self.assertRaisesRegex(ValueError, "document cursor"):
            source.request({}, "read_document_lines_batch", **base,
                           line_items=[{"documentCursor": "10000001", "lineCursor": "0", "limit": 1}])
        with self.assertRaisesRegex(ValueError, "invalid line batch item"):
            source.request({}, "read_document_lines_batch", **base,
                           line_items=[{"documentCursor": "1", "lineCursor": "0", "limit": 1,
                                        "field": "caller supplied"}])

        items = [{"documentCursor": "1", "lineCursor": "0", "limit": 1}]
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            answer = {"schemaVersion": "1.0", "operation": request["operation"],
                      "status": "ok", "requestId": request["requestId"],
                      "data": {"items": [{"documentCursor": "1", "lineCursor": "0",
                                           "limit": 1, "status": "error",
                                           "error": {"code": "raw COM exception: C:\\private"}}],
                               "adapterElapsedMs": 2}}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(answer)})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "invalid source line batch error"):
                source.request(source_info, "read_document_lines_batch", **base, line_items=items)

        integer_items = [
            {"documentCursor": 7, "lineCursor": 0, "limit": 1},
            {"documentCursor": "008", "lineCursor": "00", "limit": 1},
        ]
        def integer_cursor_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertEqual(request["items"], [
                {"documentCursor": "7", "lineCursor": "0", "limit": 1},
                {"documentCursor": "8", "lineCursor": "0", "limit": 1},
            ])
            page = {
                "kind": "document_lines", "type": "ПоступлениеТоваров",
                "document": None, "lines": [], "hasMore": False, "nextCursor": None,
                "period": {"from": "2012-01-01", "toExclusive": "2013-01-01"},
                "snapshotStatus": "none", "stableIdentity": "unverified",
                "coverage": {"status": "document-not-found", "unsupported": []},
            }
            answer = {"schemaVersion": "1.0", "operation": request["operation"],
                      "status": "ok", "requestId": request["requestId"],
                      "data": {"items": [{"documentCursor": "7", "lineCursor": "0",
                                           "limit": 1, "status": "ok", "data": page},
                                          {"documentCursor": "8", "lineCursor": "0",
                                           "limit": 1, "status": "ok", "data": page}],
                               "adapterElapsedMs": 0}}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(answer)})()
        with patch("agent_bridge.source.subprocess.run", side_effect=integer_cursor_run):
            integer_result = source.request(source_info, "read_document_lines_batch", **base,
                                            line_items=integer_items)
        self.assertEqual(integer_result["data"]["items"][0]["documentCursor"], "7")
        self.assertEqual(integer_result["data"]["items"][1]["documentCursor"], "8")

    def test_document_lines_batch_rejects_malformed_response_through_request(self):
        source_info = {"host": "host", "user": "reader", "remoteCommand": "read.ps1"}
        items = [{"documentCursor": "12", "lineCursor": "3", "limit": 2}]

        def malformed_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            page = {
                "kind": "document_lines", "type": "ПоступлениеТоваров",
                "document": {"offset": "12", "date": "2012-03-04", "number": "7",
                             "posted": None, "attributes": {}, "declaredLineCount": 2},
                "lines": [{"lineNumber": 4, "attributes": {}}],
                "hasMore": False, "nextCursor": None,
                "coverage": {"status": "metadata-driven-unverified", "unsupported": []},
            }
            answer = {
                "schemaVersion": "1.0", "operation": request["operation"],
                "status": "ok", "requestId": request["requestId"],
                "data": {"items": [{
                    # The adapter must echo the requested cursor; a shifted item could
                    # otherwise attach valid lines to the wrong source document.
                    "documentCursor": "13", "lineCursor": "3", "limit": 2,
                    "status": "ok", "data": page,
                }], "adapterElapsedMs": 2},
            }
            return type("Result", (), {
                "returncode": 0, "stdout": json.dumps(answer),
            })()

        with patch("agent_bridge.source.subprocess.run", side_effect=malformed_run):
            with self.assertRaisesRegex(source.SourceError, "changed an input cursor"):
                source.request(
                    source_info, "read_document_lines_batch",
                    source_alias="src77-2010-2012",
                    document_type="ПоступлениеТоваров",
                    from_date="2012-01-01", to_exclusive="2013-01-01",
                    line_items=items,
                )

    def test_periodic_history_pages_require_metadata_link_and_bounded_cursors(self):
        responses = []
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            responses.append(request)
            if request["kind"] == "constant":
                self.assertEqual(request["type"], "ОсновнаяВалюта")
                self.assertEqual(request["historyCursor"], "2")
                self.assertNotIn("ownerCursor", request)
                self.assertNotIn("attributeName", request)
                data = {
                    "target": {"kind": "constant", "constantName": "ОсновнаяВалюта"},
                    "rows": [{"historyCursor": "2", "date": "2012-01-02", "value": "BYR"}],
                    "hasMore": True,
                    "nextCursor": {"historyCursor": "3"},
                    "coverage": {"status": "metadata-linked-complete-unverified",
                                 "metadataLink": {"kind": "constant", "constantName": "ОсновнаяВалюта"},
                                 "unsupported": []},
                }
            elif request["kind"] == "catalog_attribute":
                self.assertEqual(request["type"], "Валюты")
                self.assertEqual(request["attributeName"], "Текущ_курс")
                self.assertEqual(request["ownerCursor"], "5")
                self.assertEqual(request["historyCursor"], "0")
                data = {
                    "target": {"kind": "catalog_attribute", "catalogName": "Валюты",
                               "attributeName": "Текущ_курс"},
                    "rows": [{"ownerCursor": "5", "historyCursor": "0",
                              "date": "2012-01-03", "value": 840}],
                    "hasMore": True,
                    "nextCursor": {"ownerCursor": "5", "historyCursor": "1"},
                    "coverage": {"status": "partial-owner-scan-budget",
                                 "metadataLink": {"kind": "catalog_attribute",
                                                  "catalogName": "Валюты",
                                                  "attributeName": "Текущ_курс"},
                                 "unsupported": []},
                }
            else:
                raise AssertionError(request["kind"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_periodic_history_page",
                "status": "ok", "requestId": request["requestId"], "data": data})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            constant_page = source.request({"host": "host", "user": "reader",
                                            "remoteCommand": "read.ps1"},
                                           "read_periodic_history_page",
                                           source_alias="src77-2010-2012",
                                           periodic_kind="constant",
                                           periodic_type="ОсновнаяВалюта",
                                           from_date="2012-01-01",
                                           to_exclusive="2013-01-01",
                                           history_cursor="2",
                                           limit=1)
            catalog_page = source.request({"host": "host", "user": "reader",
                                           "remoteCommand": "read.ps1"},
                                          "read_periodic_history_page",
                                          source_alias="src77-2010-2012",
                                          periodic_kind="catalog_attribute",
                                          periodic_type="Валюты",
                                          periodic_attribute="Текущ_курс",
                                          from_date="2012-01-01",
                                          to_exclusive="2013-01-01",
                                          owner_cursor="5",
                                          history_cursor="0",
                                          limit=1)
        self.assertEqual(constant_page["data"]["nextCursor"]["historyCursor"], "3")
        self.assertEqual(catalog_page["data"]["nextCursor"]["ownerCursor"], "5")
        self.assertEqual([item["operation"] for item in responses],
                         ["read_periodic_history_page", "read_periodic_history_page"])
        with self.assertRaisesRegex(ValueError, "owner cursor"):
            source.request({}, "read_periodic_history_page", source_alias="src77-2010-2012",
                           periodic_kind="catalog_attribute", periodic_type="Валюты",
                           periodic_attribute="Текущ_курс", from_date="2012-01-01",
                           to_exclusive="2013-01-01", owner_cursor="10000001")

    def test_periodic_history_omits_empty_optional_cursors(self):
        responses = []
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            responses.append(request)
            data = {
                "target": {"kind": "constant", "constantName": "ОсновнаяВалюта"},
                "rows": [],
                "hasMore": False,
                "nextCursor": None,
                "coverage": {"status": "metadata-linked-complete-unverified",
                             "metadataLink": {"kind": "constant", "constantName": "ОсновнаяВалюта"},
                             "unsupported": []},
            }
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_periodic_history_page",
                "status": "ok", "requestId": request["requestId"], "data": data})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                           "read_periodic_history_page", source_alias="src77-2010-2012",
                           periodic_kind="constant", periodic_type="ОсновнаяВалюта",
                           from_date="2012-01-01", to_exclusive="2013-01-01")
        self.assertNotIn("historyCursor", responses[0])
        self.assertNotIn("ownerCursor", responses[0])
        self.assertNotIn("attributeName", responses[0])

    def test_periodic_history_rejects_missing_metadata_link(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_periodic_history_page",
                "status": "ok", "requestId": request["requestId"],
                "data": {"target": {"kind": "constant"}, "rows": [], "hasMore": False,
                         "nextCursor": None,
                         "coverage": {"status": "metadata-linked-complete-unverified",
                                      "unsupported": []}}})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "metadata link"):
                source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                               "read_periodic_history_page", source_alias="src77-2010-2012",
                               periodic_kind="constant", periodic_type="ОсновнаяВалюта",
                               from_date="2012-01-01", to_exclusive="2013-01-01")

    def test_periodic_history_accepts_explicit_unsupported_context_status(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_periodic_history_page",
                "status": "ok", "requestId": request["requestId"],
                "data": {
                    "target": {"kind": "constant", "constantName": "ОсновнаяВалюта"},
                    "rows": [],
                    "hasMore": False,
                    "nextCursor": None,
                    "coverage": {"status": "unsupported-periodic-context",
                                 "metadataLink": {"kind": "constant",
                                                  "constantName": "ОсновнаяВалюта"},
                                 "unsupported": [{"reason": "periodic-context-unavailable"}]},
                }})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            result = source.request({"host": "host", "user": "reader",
                                     "remoteCommand": "read.ps1"},
                                    "read_periodic_history_page",
                                    source_alias="src77-2010-2012",
                                    periodic_kind="constant",
                                    periodic_type="ОсновнаяВалюта",
                                    from_date="2012-01-01",
                                    to_exclusive="2013-01-01")
        self.assertEqual(result["data"]["coverage"]["status"], "unsupported-periodic-context")

    def test_source_page_rejects_silent_coverage_omission(self):
        def fake_run(_argv, **kwargs):
            request = json.loads(kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({
                "schemaVersion": "1.0", "operation": "read_page", "status": "ok",
                "requestId": request["requestId"],
                "data": {"rows": [], "hasMore": False, "nextCursor": None}})})()
        with patch("agent_bridge.source.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(source.SourceError, "coverage"):
                source.request({"host": "host", "user": "reader", "remoteCommand": "read.ps1"},
                               "read_page", source_alias="src77-2010-2012",
                               dataset_kind="reference", dataset_type="Контрагенты")

    def test_odata_metadata_and_entity_validation(self):
        xml = b'''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
          xmlns:edm="http://schemas.microsoft.com/ado/2008/09/edm"><edmx:DataServices>
          <edm:Schema Namespace="Test"><edm:EntityType Name="Doc">
          <edm:Property Name="Ref_Key"/><edm:Property Name="Date"/></edm:EntityType>
          <edm:EntityContainer Name="Default"><edm:EntitySet Name="Document_Test"
          EntityType="Test.Doc"/></edm:EntityContainer></edm:Schema>
          </edmx:DataServices></edmx:Edmx>'''
        profile = {"serviceUrl": "https://example.invalid/odata/standard.odata/"}
        with patch("agent_bridge.odata._get", return_value=xml):
            schema = odata.metadata(profile)
            self.assertIn("Document_Test", schema["entitySets"])
            with self.assertRaises(ValueError):
                odata.read_page(profile, "Document_Unknown")
        calls = []
        def fake_get(_profile, url):
            calls.append(url)
            return xml if url.endswith("$metadata") else b'{"d":{"results":[{"Ref_Key":"synthetic"}]}}'
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            page = odata.read_page(profile, "Document_Test", from_date="2012-01-01",
                                   to_exclusive="2013-01-01", date_property="Date")
        self.assertEqual(len(page["rows"]), 1)
        self.assertIn("%24filter", calls[-1])
        self.assertFalse(page["hasMore"])
        def count_get(_profile, url):
            calls.append(url)
            return xml if url.endswith("$metadata") else b"12"
        with patch("agent_bridge.odata._get", side_effect=count_get):
            count = odata.count(profile, "Document_Test", from_date="2012-01-01",
                                to_exclusive="2013-01-01", date_property="Date")
        self.assertEqual(count["count"], 12)
        self.assertIn("/$count?", calls[-1])

    def test_odata_numeric_cursor_is_bounded(self):
        xml = b'''<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
          <EntityType Name="Doc"><Property Name="Ref_Key"/></EntityType>
          <EntityContainer Name="Default"><EntitySet Name="Document_Test"
          EntityType="Test.Doc"/></EntityContainer></Schema>'''
        profile = {"serviceUrl": "https://example.invalid/odata/standard.odata/"}
        next_url = "https://example.invalid/odata/standard.odata/Document_Test?$top=2&$skip=1"
        pages = [json.dumps({"d": {"results": [{"Ref_Key": "a"}], "__next": next_url}}).encode(),
                 b'{"d":{"results":[{"Ref_Key":"b"}]}}']
        def fake_get(_profile, url):
            return xml if url.endswith("$metadata") else pages.pop(0)
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            first = odata.read_page(profile, "Document_Test", limit=2)
            with self.assertRaises(ValueError):
                odata.read_page(profile, "Document_Test", limit=2,
                                cursor=first["nextCursor"][:-2] + "xx")
            second = odata.read_page(profile, "Document_Test", limit=2,
                                     cursor=first["nextCursor"])
        self.assertEqual(second["rows"][0]["Ref_Key"], "b")

    def test_year_checkpoint_requires_full_year_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(str(Path(directory) / "ledger.sqlite3"))
            try:
                self.assertEqual(year_period(2012), ("2012-01-01", "2013-01-01"))
                ledger.record_run(target_id="target-1", run_id="partial", source_alias="s", dataset_kind="documents",
                                  mapping_version="v1", period_from="2012-01-01",
                                  period_to_exclusive="2012-12-31", stage="reconciled",
                                  source_count=1, target_count=1, source_hash="a", target_hash="b",
                                  mapping_hash="c", idempotency_key="partial")
                for category in ("documents", "movements", "trial_balance"):
                    ledger.add_year_evidence(target_id="target-1", year=2012, category=category,
                                             evidence_ref="synthetic-" + category,
                                             evidence_hash="hash-" + category,
                                             method="independent_reconciliation")
                with self.assertRaises(LedgerError):
                    ledger.close_year(2012, target_id="target-1", first_year=2012, required_coverage=[{"sourceAlias": "s", "datasetKind": "documents"}])
                ledger.record_run(target_id="target-1", run_id="full", source_alias="s", dataset_kind="documents",
                                  mapping_version="v1", period_from="2012-01-01",
                                  period_to_exclusive="2013-01-01", stage="reconciled",
                                  source_count=1, target_count=1, source_hash="a", target_hash="b",
                                  mapping_hash="c", idempotency_key="full")
                with self.assertRaises(LedgerError):
                    ledger.close_year(2012, target_id="target-1", first_year=2012,
                                      required_coverage=[{"sourceAlias": "s", "datasetKind": "documents"}, {"sourceAlias": "s", "datasetKind": "registers"}])
                with self.assertRaises(LedgerError):
                    ledger.close_year(2012, target_id="target-1", first_year=2012,
                                      required_coverage=[{"sourceAlias": "s", "datasetKind": "documents"}])
                for category in ("documents", "movements", "trial_balance"):
                    ledger.add_year_evidence(target_id="target-1", year=2012, category=category,
                                             evidence_ref="fresh-" + category,
                                             evidence_hash="fresh-hash-" + category,
                                             method="independent_reconciliation")
                receipt = ledger.close_year(2012, target_id="target-1", first_year=2012,
                                            required_coverage=[{"sourceAlias": "s", "datasetKind": "documents"}])
                self.assertEqual(receipt["period"]["toExclusive"], "2013-01-01")
                self.assertEqual(ledger.year_status(2012, target_id="target-1")["state"], "closed")
                ledger.record_run(target_id="target-1", run_id="later", source_alias="s", dataset_kind="documents",
                                  mapping_version="v2", period_from="2012-01-01",
                                  period_to_exclusive="2013-01-01", stage="source_page",
                                  source_count=2, target_count=0, source_hash="d", target_hash="e",
                                  mapping_hash="f", idempotency_key="later")
                self.assertEqual(ledger.year_status(2012, target_id="target-1")["state"], "open")
                with self.assertRaises(LedgerError):
                    ledger.close_year(2012, target_id="target-1", first_year=2012,
                                      required_coverage=[{"sourceAlias": "s", "datasetKind": "documents"}])
            finally:
                ledger.close()

    def test_no_target_write_in_core(self):
        bridge = Bridge({"sources": {"s": {}}, "targets": {
            "p": {"environment": "prod"}, "t": {"environment": "test"}}})
        for alias in ("p", "t"):
            self.assertFalse(bridge.target_write_readiness(alias)["writeEnabled"])
        self.assertFalse(hasattr(bridge, "odata_write"))

    def test_runtime_config_must_live_outside_git(self):
        example = Path(__file__).resolve().parents[1] / "src" / "agent_bridge" / "config.example.json"
        with self.assertRaises(ConfigError):
            load_config(str(example))
        with tempfile.TemporaryDirectory() as directory:
            data = json.loads(example.read_text(encoding="utf-8"))
            data["sources"]["src77-2010-2012"] = data["sources"].pop("source_77_example")
            data["ledger"]["path"] = str(Path(directory) / "ledger.sqlite3")
            identity = Path(directory) / "onec_bridge_windows_ed25519"
            identity.write_text("synthetic-key-placeholder", encoding="utf-8")
            for runtime_source in data["sources"].values():
                runtime_source["identityFile"] = str(identity)
            private = Path(directory) / "bridge.json"
            private.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_config(str(private))
            self.assertIn("src77-2010-2012", loaded["sources"])
            self.assertEqual(loaded["sources"]["src77-2010-2012"]["identityFile"],
                             str(identity.resolve()))
            self.assertEqual(loaded["sources"]["src77-2010-2012"]["hostKeyAlias"],
                             "win77-bridge.local")
            self.assertEqual(loaded["sources"]["live77_interactive_broker_example"]["requestTimeoutSeconds"],
                             210)
            data["targets"] = {}
            private.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn("src77-2010-2012", load_config(str(private))["sources"])
            data["sources"] = {}
            data["targets"] = loaded["targets"]
            private.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn("prod_example", load_config(str(private))["targets"])
            valid_sources = copy.deepcopy(loaded["sources"])
            for invalid_timeout in (0, 241, "210", True):
                data["sources"] = copy.deepcopy(valid_sources)
                data["sources"]["src77-2010-2012"]["requestTimeoutSeconds"] = invalid_timeout
                private.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "requestTimeoutSeconds"):
                    load_config(str(private))
            data["sources"] = copy.deepcopy(valid_sources)
            data["sources"]["src77-2010-2012"]["remoteCommand"] += " & whoami"
            private.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(private))
            data["sources"] = copy.deepcopy(valid_sources)
            data["sources"]["src77-2010-2012"]["identityFile"] = "relative/id_ed25519"
            private.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "identityFile"):
                load_config(str(private))
            data["sources"] = copy.deepcopy(valid_sources)
            data["sources"]["src77-2010-2012"]["identityFile"] = str(example)
            private.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "identityFile"):
                load_config(str(private))
            data["sources"] = copy.deepcopy(valid_sources)
            data["sources"]["src77-2010-2012"]["hostKeyAlias"] = "bad alias"
            private.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "hostKeyAlias"):
                load_config(str(private))


if __name__ == "__main__":
    unittest.main()
