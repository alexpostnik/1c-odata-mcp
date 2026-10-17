"""Synthetic behavioral coverage; no published real banking fixtures."""
import ast
import base64
import copy
from functools import lru_cache
import json
import importlib.util
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reader import Reader, QueryError, SOURCE_ENUMS, contract, incoming_candidate, source_unit_review_valid, canonical_sources, semantic_source, source_enums, CANONICAL_MODULES, _load_source_modules, source_schema, supplied_source_fields, strict_json, encoded, bounded, detached_json, MAX_JSON_DEPTH, MAX_JSON_INTEGER_DIGITS


@lru_cache(maxsize=1)
def reviewed_source_fixture():
    # Reuse the accepted synthetic fixture without making its source imports
    # visible to the test runner or relying on canonical_sources changing paths.
    scripts = Path(__file__).resolve().parents[2] / "onec_bank_graph/scripts"
    modules = _load_source_modules(scripts, CANONICAL_MODULES +
        ("test_archive_adapter", "test_identifiers_modern_math", "test_modern_reviewed"))
    return modules


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def refresh_processing(graph):
    with canonical_sources() as (facts, _, _, _):
        facts.materialize_processing(graph)


def make_archive(root):
    inputs = []
    for index, (unit, epoch) in enumerate((("BYR_2000_historical_ruble", "pre_2016_07_01"),
                                         ("BYN_2009_ruble", "from_2016_07_01"), (None, None)), 1):
        sid, cid = f"s{index}", f"c{index}"
        nodes = [{"id": sid, "type": "Statement", "properties": {
            "currency_context_id": cid, "account_id": f"a{index}", "period_start": "2016-01-01" if index == 1 else "2016-07-01",
            "period_end": "2016-06-30" if index == 1 else "2016-12-31", "year": 2016, "debit_is": "outflow", "opening_sign_rule": "signed", "selection": "include",
            "expected_operations": 3, "rows_complete": True, "opening": None, "closing": None,
            "printed_debit_total": None, "printed_credit_total": None}, "observations": [], "unrecognized_fields": []},
            {"id": cid, "type": "CurrencyContext", "properties": {"printed_currency": "BYR" if index == 1 else "BYN", "semantics_status": "unknown"}, "observations": [], "unrecognized_fields": []},
            {"id": f"a{index}", "type": "Account", "properties": {"number_raw": "000123"}, "observations": [], "unrecognized_fields": []}]
        edges = [{"id": f"currency{index}", "type": "IN_CURRENCY", "from": sid, "to": cid},
                 {"id": f"account{index}", "type": "FOR_ACCOUNT", "from": sid, "to": f"a{index}"}]
        for ordinal in range(1, 4):
            amount = ["100000000000000000000000000000.000000000000000000001", "2.00", None][ordinal - 1]
            direction = "outflow" if ordinal == 2 else "inflow" if ordinal == 1 else "unknown"
            purpose = "Комиссия за прием выручки; база 999999" if ordinal == 2 else "Внесение выручки\nполное назначение"
            props = {"statement_id": sid, "ordinal": ordinal, "transaction_date": f"2016-0{1 if index == 1 else 7}-0{ordinal}",
                     "document_number_raw": "001", "direction": direction, "amount": amount,
                     "printed_credit": amount if direction == "inflow" else None,
                     "printed_debit": amount if direction == "outflow" else None,
                     "purpose_raw": purpose, "payment1c_comment_raw": purpose,
                     "row_status": "partial" if ordinal == 3 else "complete", "duplicate_status": "unique", "classification_status": "unknown",
                     "processing": None,
                     "payment1c_text": None,
                     "source_text": {"value": "source projection"}}
            nodes.append({"id": f"o{index}{ordinal}", "type": "Operation", "properties": props,
                          "observations": [{"field": "purpose", "raw": purpose, "normalized": None, "status": "observed"}], "unrecognized_fields": []})
            edges.append({"id": f"contains{index}{ordinal}", "type": "CONTAINS", "from": sid, "to": f"o{index}{ordinal}"})
        nodes.append({"id": f"fee{index}", "type": "AmountComponent", "properties": {"kind": "basis", "value": "999999"}, "observations": [], "unrecognized_fields": []})
        edges.append({"id": f"component{index}", "type": "HAS_COMPONENT", "from": f"o{index}2", "to": f"fee{index}"})
        with canonical_sources() as (facts, _, _, _):
            for node in nodes:
                if node["type"] == "Operation":
                    p = node["properties"]
                    p["payment1c_text"] = facts.prepare_payment_text(p["purpose_raw"], "unknown")
            facts.materialize_processing({"nodes": nodes, "edges": edges})

        sp = nodes[0]["properties"]
        code = nodes[1]["properties"]["printed_currency"]
        sp["heading_raw"] = "ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД С " + ("01/01/2016 ПО 30/06/2016" if index == 1 else "01/07/2016 ПО 31/12/2016")
        line = "000123 " + code
        sp["header_fields"] = {"own_account_line_raw": line}
        nodes[0]["observations"].append({"field": "header_own_account_line_raw", "raw": line, "normalized": line, "status": "observed"})
        graph, controls = f"g{index}.json", f"u{index}.json"
        write(root / graph, {"schema_version": 1, "nodes": nodes, "edges": edges})
        write(root / controls, {"statements": [{"statement_id": sid, "source_unit_review": {
            "status": "verified" if epoch else "unknown", "source_unit": unit, "source_epoch": epoch,
            "transfer_factor": "1", "printed_label_anomaly": False, "basis": "Synthetic reviewed source"},
            "source_review": {"origin": "independent_original_pdf", "review_status": "reviewed", "reference": "synthetic-reference"}}]})
        inputs.append({"graph": graph, "controls": controls, "statement_id": sid, "source_ready": True})
    write(root / "manifest.json", {"status": "source_ready", "inputs": inputs})


class CanonicalHostIsolationMixin:
    def test_host_import_orders_preserve_modules_and_valid_archive(self):
        # Each name/order uses a fresh process, so neither this test runner nor
        # the fixture generator can hide import-order or cache contamination.
        program = r"""
import builtins
import importlib
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

from reader import Reader, QueryError, CANONICAL_MODULES, canonical_sources

archive, host, mode, selected = sys.argv[1:]
names = CANONICAL_MODULES if selected == "all" else (selected,)
sys.path.insert(0, host)
if mode == "host_first":
    expected = {name: importlib.import_module(name) for name in names}
else:
    expected = {}
prior_path = sys.path[:]
prior_path_object = sys.path
prior_import = builtins.__import__
prior_modules = {name: sys.modules.get(name) for name in CANONICAL_MODULES}

# Cache and several concurrent constructors must keep local lazy imports local.
with ThreadPoolExecutor(max_workers=3) as pool:
    readers = list(pool.map(lambda _: Reader(archive), range(3)))
for reader in readers:
    assert reader.search()["total"] == 9
    assert reader.get("o11")["node"]["properties"]["purpose_raw"].startswith("Внесение")
    assert reader.links("o12", ["HAS_COMPONENT"])["items"][0]["neighbor"]["type"] == "AmountComponent"
    assert reader.aggregate(group_by="none")["matched_operations"] == 9
    assert reader.overview()["node_counts"]["Operation"] == 9
with canonical_sources() as (facts, adapter, modern, readiness):
    assert facts.__name__.startswith("_bank_graph_sources_")
    assert facts.__name__.endswith(".statement_graph")
    assert adapter.sg is facts and readiness.sg is facts
    assert modern.bi is adapter.identifiers
    assert facts.prepare_payment_text.__module__.endswith(".payment_text")
    assert facts.prepare_payment_text("Full purpose", "observed")["status"] == "ready"

# Semantic rejection occurs inside the same source context, after loading.
graph_path = Path(archive) / "g1.json"
before = graph_path.read_bytes()
graph = json.loads(before)
operation = next(n for n in graph["nodes"] if n["type"] == "Operation")
operation["properties"]["payment1c_text"]["value"] = "foreign replacement"
graph_path.write_text(json.dumps(graph), encoding="utf-8")
try:
    try:
        Reader(archive)
    except QueryError:
        pass
    else:
        raise AssertionError("contradictory payment text was admitted")
finally:
    graph_path.write_bytes(before)
assert sys.path is prior_path_object and sys.path == prior_path
assert builtins.__import__ is prior_import
assert all(sys.modules.get(name) is prior_modules[name] for name in CANONICAL_MODULES)
assert not any(name.startswith("_bank_graph_sources_") for name in sys.modules)
if mode == "reader_first":
    expected = {name: importlib.import_module(name) for name in names}
for name, module in expected.items():
    assert sys.modules[name] is module
    assert module.MARKER == "host:" + name
    assert Path(module.__file__).parent == Path(host)
print(json.dumps({"names": list(names), "order": mode, "readers": 3, "status": "PASS"}))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, host = root / "archive", root / "host"
            archive.mkdir()
            host.mkdir()
            make_archive(archive)
            for name in CANONICAL_MODULES:
                (host / (name + ".py")).write_text(
                    "MARKER = " + repr("host:" + name) + "\ndef __getattr__(name):\n"
                    "    raise AssertionError('foreign module must not supply canonical predicates')\n",
                    encoding="utf-8")
            for mode in ("host_first", "reader_first"):
                for name in (*CANONICAL_MODULES, "all"):
                    with self.subTest(order=mode, name=name):
                        proc = subprocess.run([sys.executable, "-B", "-c", program,
                            str(archive), str(host), mode, name],
                            cwd=Path(__file__).parent, capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(json.loads(proc.stdout)["status"], "PASS")

    def test_failed_source_load_preserves_host_state_and_retry(self):
        program = r"""
import builtins
import importlib.machinery
import json
import sys
import types
from unittest.mock import patch
from reader import canonical_sources, CANONICAL_MODULES, QueryError

host = {name: types.ModuleType(name) for name in CANONICAL_MODULES}
sys.modules.update(host)
prior_path = sys.path[:]
prior_import = builtins.__import__
ordinary_source = importlib.machinery.SourceFileLoader.get_source

def failing_source(loader, fullname):
    if fullname.endswith(".source_readiness"):
        raise OSError("synthetic late source read failure")
    return ordinary_source(loader, fullname)
with patch.object(importlib.machinery.SourceFileLoader, "get_source", failing_source):
    try:
        with canonical_sources():
            pass
    except QueryError as error:
        assert "unavailable canonical source contract" in str(error)
    else:
        raise AssertionError("source read failure was suppressed")
assert sys.path == prior_path and builtins.__import__ is prior_import
assert all(sys.modules[name] is module for name, module in host.items())
assert not any(name.startswith("_bank_graph_sources_") for name in sys.modules)
# Failed construction must not leave a partially initialized cached group.
with canonical_sources() as (facts, adapter, modern, readiness):
    assert readiness.sg is facts and adapter.sg is facts
    assert facts.prepare_payment_text("Full purpose", "observed")["status"] == "ready"
assert all(sys.modules[name] is module for name, module in host.items())
print(json.dumps({"failure": "QueryError", "retry": "PASS", "host_state": "preserved"}))
"""
        proc = subprocess.run([sys.executable, "-B", "-c", program],
            cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["retry"], "PASS")

    @unittest.skipUnless(importlib.util.find_spec("mcp"),
                         "optional mcp2.x SDK unavailable; core remains stdlib")
    def test_actual_mcp_host_namespace_discovery_and_queries(self):
        # Reuse the actual SDK in-memory discovery/query test while every
        # generic source name is occupied by a foreign host module, and then
        # exercise the inverse order in a separate process.
        program = r"""
import json
import sys
import types
import unittest
from reader import CANONICAL_MODULES
mode = sys.argv[1]
host = {name: types.ModuleType(name) for name in CANONICAL_MODULES}
prior_path = sys.path[:]
if mode == "host_first":
    sys.modules.update(host)
from test_mcp_adapter import AdapterTests
suite = unittest.defaultTestLoader.loadTestsFromName(
    "test_initialize_discovery_resources_queries_and_errors", AdapterTests)
result = unittest.TextTestRunner().run(suite)
assert result.wasSuccessful() and result.testsRun == 1 and not result.skipped
assert sys.path == prior_path
if mode == "reader_first":
    assert all(name not in sys.modules for name in CANONICAL_MODULES)
    sys.modules.update(host)
assert all(sys.modules[name] is module for name, module in host.items())
# Canonical modules were cached before host imports in this order; another
# complete SDK registration/query cycle must still retain these host objects.
result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName(
    "test_initialize_discovery_resources_queries_and_errors", AdapterTests))
assert result.wasSuccessful() and result.testsRun == 1 and not result.skipped
assert sys.path == prior_path
assert all(sys.modules[name] is module for name, module in host.items())
print(json.dumps({"order": mode, "SDK_cycles": 2, "status": "PASS"}))
"""
        for mode in ("host_first", "reader_first"):
            with self.subTest(order=mode):
                proc = subprocess.run([sys.executable, "-B", "-c", program, mode],
                    cwd=Path(__file__).parent, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["status"], "PASS")


class SemanticSourceMixin:
    """Admission parity on synthetic source-bound profiles and legacy diagnostics."""
    def fixture(self, unp="123456789", purpose="Complete synthetic purpose", accepted=False):
        with canonical_sources() as (facts, _, _, readiness):
            # Accepted repository synthetic source anatomy; no PDF/extractor use.
            fixture = reviewed_source_fixture()["test_modern_reviewed"]
            _, graph, _ = fixture.parse(fixture.doc(unp=unp, purpose=purpose))
            st = next(n for n in graph["nodes"] if n["type"] == "Statement")
            p = st["properties"]
            op = next(n for n in graph["nodes"] if n["type"] == "Operation")
            controls = {"statements": [{"statement_id": st["id"], **{k: p[k] for k in
                ("opening", "closing", "printed_debit_total", "printed_credit_total")},
                "no_activity": False,
                "source_review": {"origin": "independent_original_pdf", "review_status": "reviewed", "reference": "synthetic-private-review"},
                "source_unit_review": {"status": "verified", "source_unit": "BYN_2009_ruble", "source_epoch": "from_2016_07_01", "transfer_factor": "1", "printed_label_anomaly": False, "basis": "Synthetic printed BYN/January2026 source"},
                "printed_operation_count": {"status": "absent", "value": None, "raw": None}}]}
            review = {"profile": readiness.PROFILE, "source_review": "independent_pdf_review", "full_field_coverage": True,
                "unresolved_source_errors": [], "operations": 1,
                "node_fingerprints": {n["id"]: readiness.fact_fingerprint(n) for n in graph["nodes"]},
                "edge_fingerprint": readiness.edge_fingerprint(graph["edges"]),
                "controls_fingerprint": readiness.control_fingerprint(controls)}
        item = {"statement_id": st["id"], "graph": "g.json", "controls": "c.json"}
        if accepted:
            item.update(source_check="k.json", source_review="r.json")
        write(self.root / "manifest.json", {"inputs": [item]})
        write(self.root / "r.json", review)
        write(self.root / "k.json", {"source_ready": True, "accepted": True, "errors": [],
             "statements": [{"statement_id": st["id"], "operations": 1, "math_status": "pass", "differences": {}}]})
        self.save(graph, controls)
        return graph, controls, st, op

    def save(self, graph, controls):
        write(self.root / "g.json", graph)
        write(self.root / "c.json", controls)

    def canonical_rejects(self, graph, substring):
        try:
            import jsonschema
        except ModuleNotFoundError:
            return  # Stdlib behavior still exercised; SDK suite runs full parity.
        with canonical_sources() as (facts, _, _, _):
            result = facts.validate(graph)
        self.assertFalse(result["valid"])
        self.assertTrue(any(substring in e for e in result["errors"]), result["errors"])

    def rejected(self, graph, controls, canonical_error, cli=False):
        self.save(graph, controls)
        self.canonical_rejects(graph, canonical_error)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(QueryError):
            Reader(self.root)
        if cli:
            proc = subprocess.run([sys.executable, "-B", str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                "search", "--archive", str(self.root)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stderr)["contract_version"], contract()["contract_version"])
            self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_all_account_bank_identifiers_match_accepted_recognizer(self):
        for kind in ("Account", "Bank"):
            for defect in ("normalized", "recognition", "raw", "existence", "format_or_value", "unusable_observed", "missing_member"):
                with self.subTest(kind=kind, defect=defect):
                    g, c, st, op = self.fixture()
                    route_account = next(e["to"] for e in g["edges"] if e["from"] == op["id"] and e["type"] in ("FROM_ACCOUNT", "TO_ACCOUNT"))
                    route_bank = next(e["to"] for e in g["edges"] if e["from"] == route_account and e["type"] == "SERVICED_BY")
                    node = next(n for n in g["nodes"] if n["id"] == (route_account if kind == "Account" else route_bank))
                    p, ident = node["properties"], node["properties"]["identifier"]
                    expected = "bank identifier recognition mismatch"
                    if defect == "normalized": ident["normalized"] = "000000"
                    elif defect == "recognition": ident["format_status"] = "unknown"
                    elif defect == "raw": ident["raw"] = "000001"
                    elif defect == "existence": ident["existence"] = "verified"
                    elif defect == "missing_member": del ident["diagnostics"]
                    elif defect == "format_or_value":
                        if kind == "Account": p["format"] = "historical"; expected = "bank account format mismatch"
                        else: p["code_raw"] = "000001"; expected = "bank identifier value mismatch"
                    else:
                        with canonical_sources() as (_, _, modern, _):
                            p["identifier"] = modern.bi.recognize("lowercase-invalid", "account" if kind == "Account" else "bank")
                        p["number_raw" if kind == "Account" else "code_raw"] = None
                        if kind == "Account": p["format"] = "unknown"
                        p["identity_status"] = "observed"
                        expected = "unusable bank identifier claimed observed"
                    self.rejected(g, c, "" if defect in ("existence", "missing_member") else expected)

    def test_identifier_unknown_historical_and_bank_observation_compatibility(self):
        # No current-bank lookup, case repair or leading-zero loss. No new
        # requirement for an identifier when the legacy node never supplied one.
        for raw, kind, observed in (("000123", "Account", True), ("000007", "Bank", True),
                                    ("lowercase-invalid", "Bank", False), (None, "Account", False)):
            with self.subTest(raw=raw, kind=kind):
                make_archive(self.root)
                g = json.loads((self.root / "g1.json").read_text())
                with canonical_sources() as (_, _, modern, _):
                    ident = modern.bi.recognize(raw, "account" if kind == "Account" else "bank")
                node = {"id": "supplemental", "type": kind, "properties": {
                    "identifier": ident, "identity_status": "observed" if observed else "unknown"},
                    "observations": [], "unrecognized_fields": []}
                if kind == "Account": node["properties"].update(number_raw=ident["normalized"], format="historical" if observed else "unknown")
                else:
                    node["properties"].update(code_raw=raw, code_system_raw="Код")
                    node["observations"].append({"field": "code_raw", "raw": raw, "normalized": ident["normalized"], "status": "observed" if observed else "unknown"})
                g["nodes"].append(node)
                write(self.root / "g1.json", g)
                self.assertEqual(Reader(self.root).get(node["id"])["node"], node)
                if kind == "Bank":
                    node["observations"][0].update(status="observed", normalized="wrong")
                    write(self.root / "g1.json", g)
                    with self.assertRaisesRegex(QueryError, "unproven observed bank code"):
                        Reader(self.root)

    def test_duplicate_decision_confirmed_evidence_and_member_parity(self):
        for status in ("candidate", "confirmed", "rejected"):
            for defect in (None, "canonical", "evidence", "member"):
                with self.subTest(status=status, defect=defect):
                    g, c, st, op = self.fixture()
                    node = {"id": "duplicate-demo", "type": "DuplicateDecision", "properties": {
                        "status": status, "basis": "Synthetic source comparison", "canonical_id": op["id"],
                        "member_ids": [op["id"]], "evidence_refs": ["synthetic-private-evidence"]},
                        "observations": [], "unrecognized_fields": []}
                    p = node["properties"]
                    if defect == "canonical": p["canonical_id"] = None
                    elif defect == "evidence": p["evidence_refs"] = []
                    elif defect == "member": p["member_ids"] = ["unresolved-member"]
                    g["nodes"].append(node)
                    g["edges"].append({"id": "duplicate-member", "type": "DUPLICATE_MEMBER", "from": node["id"], "to": op["id"]})
                    if defect == "member" or status == "confirmed" and defect is not None:
                        self.rejected(g, c, "missing or mistyped reference" if defect == "member" else "confirmed duplicate needs canonical/evidence")
                    else:
                        self.save(g, c)
                        reader = Reader(self.root)
                        self.assertEqual(reader.get(node["id"])["node"], node)
                        self.assertEqual(reader.links(node["id"])["items"][0]["neighbor"]["id"], op["id"])

    def test_supplied_field_admission_covers_every_canonical_node_type(self):
        cases = {"Statement": ("opening_origin", "unknown", "invented"),
                 "Operation": ("operation_code_raw", "0001", 1),
                 "Party": ("identity_status", "unknown", "ready"),
                 "Account": ("identity_status", "unknown", "confirmed"),
                 "Bank": ("valid_from", None, "2016-02-30"),
                 "CurrencyContext": ("semantics_status", "unknown", "observed"),
                 "DocumentReference": ("availability", "mentioned_only", "verified"),
                 "AmountComponent": ("inclusion", "unknown", "confirmed"),
                 "TaxDetail": ("budget_code_raw", "00001", 1),
                 "CashSymbol": ("semantics_status", "unknown", "confirmed"),
                 "DuplicateDecision": ("status", "candidate", "verified")}
        schema = source_schema()
        canonical_types = {x["$ref"].split("/")[-1] for x in schema["$defs"]["node"]["oneOf"]}
        self.assertEqual(set(cases), canonical_types)
        for kind, (field, valid, invalid) in cases.items():
            with self.subTest(kind=kind):
                make_archive(self.root)
                g = json.loads((self.root / "g1.json").read_text())
                node = next((n for n in g["nodes"] if n["type"] == kind), None)
                if node is None:
                    node = {"id": "supplemental", "type": kind, "properties": {}, "observations": [], "unrecognized_fields": []}
                    g["nodes"].append(node)
                node["properties"][field] = valid
                write(self.root / "g1.json", g)
                self.assertEqual(Reader(self.root).get(node["id"])["node"], node)
                node["properties"][field] = invalid
                write(self.root / "g1.json", g)
                with self.assertRaises(QueryError): Reader(self.root)

    def test_supplied_scalar_fields_match_independent_schema_oracle(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ModuleNotFoundError:
            self.skipTest("Optional SDK runtime supplies independent full-schema oracle")
        schema = source_schema()
        types = [x["$ref"].split("/")[-1] for x in schema["$defs"]["node"]["oneOf"]]
        values = (None, "", "unknown", "observed", "verified", "candidate", "confirmed",
                  "000123", "2016-01-01", "2016-02-30", "0.00", "01", "NaN", False, True, 0, 1)
        checked = 0
        for kind in types:
            for field, declaration in schema["$defs"][kind]["properties"]["properties"]["properties"].items():
                definition = schema["$defs"][declaration["$ref"].split("/")[-1]] if "$ref" in declaration else declaration
                if definition.get("type") in ("array", "object"): continue
                validator = Draft202012Validator({"$defs": schema["$defs"], **declaration}, format_checker=FormatChecker())
                for value in values:
                    with self.subTest(kind=kind, field=field, value=value):
                        try:
                            supplied_source_fields(value, declaration, kind + "." + field)
                            admitted = True
                        except QueryError:
                            admitted = False
                        self.assertEqual(admitted, validator.is_valid(value))
                        checked += 1
        self.assertGreater(checked, 1500)

    def test_operation_local_debit_semantics_precede_statement_fallback(self):
        for statement_semantics, local_semantics, expected in (
                ("unknown", "outflow", True), ("inflow", "outflow", True),
                ("outflow", "unknown", False), ("outflow", "inflow", False),
                ("outflow", None, True), ("unknown", None, False)):
            with self.subTest(statement=statement_semantics, local=local_semantics):
                p = {"direction": "inflow", "amount": "2.00", "printed_credit": "2.0", "printed_debit": None}
                if local_semantics is not None: p["bank_debit_is"] = local_semantics
                self.assertEqual(incoming_candidate(p, statement_semantics), expected)
        make_archive(self.root)
        g = json.loads((self.root / "g1.json").read_text())
        g["nodes"][0]["properties"]["debit_is"] = "unknown"
        for n in g["nodes"]:
            if n["type"] == "Operation": n["properties"]["bank_debit_is"] = "outflow"
        refresh_processing(g)
        write(self.root / "g1.json", g)
        reader = Reader(self.root)
        self.assertTrue(reader.search({"operation_id": "o11"})["items"][0]["bank_inflow_candidate"])
        self.assertFalse(reader.search({"operation_id": "o12"})["items"][0]["bank_inflow_candidate"])
        self.assertEqual(reader.get("o11")["node"], g["nodes"][3])

    def test_source_bound_dates_account_reissue_and_observation_calendar(self):
        for defect in ("date", "account", "reissue", "rko"):
            with self.subTest(defect=defect):
                g, c, st, op = self.fixture()
                if defect == "date":
                    op["properties"]["transaction_date"] = "2099-01-01"
                    error = "operation date outside period"
                elif defect == "account":
                    st["properties"]["account_number_raw"] = "000012345"
                    error = "statement/account number disagreement"
                elif defect == "reissue":
                    st["properties"].update(selection="exclude_reissue", selection_evidence=[])
                    error = "reissue exclusion requires evidence"
                else:
                    op["properties"]["one_c_rko"] = {"scope": "cash_revenue_rko", "database": "test", "status": "loaded_verified",
                        "document": {"date": "2026-01-05", "number": "001", "type": "Расходный кассовый ордер"},
                        "observed_at": "2026-10-10T12:00:00+03:00", "observed_on": "2026-10-09",
                        "posted": True, "deletion_mark": False, "observed_precision": "exact_timestamp", "snapshot_atomic": False}
                    error = "RKO observation requires exact timezone timestamp"
                self.rejected(g, c, error, cli=True)
        g, c, st, op = self.fixture()
        st["properties"].update(selection="exclude_reissue", selection_evidence=["synthetic-owner-copy-decision"])
        self.save(g, c)
        self.assertEqual(Reader(self.root).search()["total"], 0)
        self.assertEqual(Reader(self.root).search({"selection": "all"})["total"], 1)

    def test_bank_knownness_and_complete_movement_match_canonical(self):
        for defect in ("unreadable", "unknown_semantics", "null_amount", "complete_unknown"):
            with self.subTest(defect=defect):
                g, c, st, op = self.fixture()
                p = op["properties"]
                if defect == "unreadable":
                    next(o for o in op["observations"] if o["field"] == "printed_credit").update(raw="???", status="unknown", normalized=None)
                elif defect == "unknown_semantics":
                    p["bank_debit_is"] = "unknown"
                elif defect == "null_amount":
                    p.update(amount=None, row_status="partial")
                else:
                    p.update(amount=None, printed_debit=None, printed_credit=None, direction="unknown", row_status="complete", bank_debit_is="unknown")
                self.rejected(g, c, "complete operation has unknown bank movement" if defect == "complete_unknown" else "movement differs from same-operation bank cells")
        make_archive(self.root)
        g = json.loads((self.root / "g1.json").read_text())
        # Partial unknown legacy diagnostics and proven legacy blank cells coexist.
        self.assertIsNone(Reader(self.root).get("o13")["node"]["properties"]["amount"])
        self.assertTrue(Reader(self.root).search({"operation_id": "o11"})["items"][0]["bank_inflow_candidate"])

    def test_source_text_settlement_components_and_classification_parity(self):
        for defect in ("text", "purpose", "settlement", "component", "classification"):
            with self.subTest(defect=defect):
                g, c, st, op = self.fixture()
                p = op["properties"]
                if defect == "text":
                    p["payment1c_text"].update(value="Fabricated ready text", diagnostics=[])
                    error = "payment text differs from deterministic"
                elif defect == "purpose":
                    p.update(purpose_raw="Fabricated purpose", payment1c_comment_raw="Fabricated purpose")
                    with canonical_sources() as (facts, _, _, _):
                        p["payment1c_text"] = facts.prepare_payment_text(p["purpose_raw"], p["purpose_status"])
                    error = "modern fields differ from retained source"
                elif defect == "settlement":
                    p["settlement"] = {"status": "verified_equation", "gross": "3.00", "commission": "1.00", "net": "1.00"}
                    error = "unproven source settlement fields"
                elif defect == "component":
                    g["nodes"].append({"id": "component", "type": "AmountComponent", "properties": {"kind": "fee", "value_raw": "1.00", "value": "1.00", "unit_raw": None, "currency_raw": None, "inclusion": "included"}, "observations": [], "unrecognized_fields": []})
                    g["edges"].append({"id": "component-link", "type": "HAS_COMPONENT", "from": op["id"], "to": "component"})
                    error = "amount components differ from full source purpose"
                else:
                    p.update(classification="bank_fee", classification_status="inferred")
                    error = "classification differs from source purpose"
                self.rejected(g, c, error)
        # Canonical legacy source builder supplies verified equation/withheld-fee
        # and standalone basis components; each remains one bank movement.
        with canonical_sources() as (facts, adapter, _, _):
            fixture = reviewed_source_fixture()["test_archive_adapter"]
            document, row = fixture.document, fixture.row
            for purpose, direction, amount, expected_status in (("Эквайринг. gross: 1000.00; удержанная комиссия: 15.00; net: 985.00.", "inflow", "985.00", "verified_equation"),
                 ("Удержана комиссия 15.00", "inflow", "985.00", "verified_withheld_fee"),
                 ("Комис.вознагр.за прием налич. денег с суммы оборотов 760000.00 по ставке 0.30 %", "outflow", "2280.00", "not_applicable")):
                st, entities, relations, _ = adapter.parse_statement(document([row("1", credit=amount if direction == "inflow" else "", debit=amount if direction == "outflow" else ""), purpose]), 1, "synthetic-semantic")
                g = facts.build({"dataset_id": "synthetic-semantic", "statements": [st], "entities": entities, "relations": relations}, diagnostic=True)
                facts.materialize_processing(g)
                self.assertEqual(semantic_source(g, {}), set())
                write(self.root / "manifest.json", {"inputs": [{"statement_id": next(n["id"] for n in g["nodes"] if n["type"] == "Statement"), "graph": "g.json"}]})
                write(self.root / "g.json", g)
                self.assertEqual(Reader(self.root).aggregate()["matched_operations"], 1)
                op = next(n for n in g["nodes"] if n["type"] == "Operation")
                self.assertEqual(op['properties']['settlement']['status'], expected_status)
                self.assertEqual(Reader(self.root).get(op["id"])["node"], op)
                damaged = copy.deepcopy(g)
                next(n for n in damaged["nodes"] if n["type"] == "Operation")["properties"]["purpose_raw"] = "Changed source purpose"
                with self.assertRaisesRegex(QueryError, "purpose differs from retained source detail"):
                    semantic_source(damaged, {})

    def test_verified_settlement_requires_observed_purpose_preserving_legacy_unknowns(self):
        for purpose_status in (None, "unknown"):
            make_archive(self.root)
            g = json.loads((self.root / "g1.json").read_text())
            p = g['nodes'][3]['properties']
            p.update(purpose_raw="Удержана комиссия 15.00", payment1c_comment_raw="Удержана комиссия 15.00")
            if purpose_status is not None:
                p['purpose_status'] = purpose_status
            with canonical_sources() as (facts, adapter, _, _):
                p['payment1c_text'] = facts.prepare_payment_text(p['purpose_raw'], "unknown")
                p['settlement'] = adapter.settlement_view(adapter.amount_components(p['purpose_raw'], p['direction'], p['amount'], "BYR"), p['direction'], p['amount'], False)
            self.assertEqual(p['settlement']['status'], 'verified_withheld_fee')
            write(self.root / 'g1.json', g)
            with self.assertRaisesRegex(QueryError, 'verified settlement requires observed full source purpose') as caught:
                Reader(self.root)
            self.assert_archive_rejected(str(caught.exception))
        for settlement in (None, {'status': 'unknown', 'gross': None, 'commission': None, 'net': None}):
            make_archive(self.root)
            g = json.loads((self.root / 'g1.json').read_text())
            p = g['nodes'][3]['properties']
            if settlement is not None:
                p.update(purpose_raw='Комиссия банка за услуги 15.00', payment1c_comment_raw='Комиссия банка за услуги 15.00')
                with canonical_sources() as (facts, adapter, _, _):
                    p['payment1c_text'] = facts.prepare_payment_text(p['purpose_raw'], 'unknown')
                    settlement = adapter.settlement_view(adapter.amount_components(p['purpose_raw'], p['direction'], p['amount'], 'BYR'), p['direction'], p['amount'], False)
                self.assertEqual(settlement['status'], 'unknown')
            p['settlement'] = settlement
            write(self.root / 'g1.json', g)
            self.assertEqual(Reader(self.root).get('o11')['node'], g['nodes'][3])

    def test_observed_identity_source_party_holder_and_processing_parity(self):
        for defect in ("party", "source", "holder", "processing", "clarification"):
            with self.subTest(defect=defect):
                g, c, st, op = self.fixture()
                cp = op["properties"]["counterparty"]
                party = next(n for n in g["nodes"] if n["id"] == cp["party_id"])
                if defect == "party":
                    party["properties"]["name_raw"] = "Fabricated Party"
                elif defect == "source":
                    cp["name_raw"] = party["properties"]["name_raw"] = "Fabricated source identity"
                    for o in party["observations"]:
                        if o["field"] == "name_raw":
                            o.update(raw=cp["name_raw"], normalized=cp["name_raw"])
                elif defect == "holder":
                    holder = next(e["to"] for e in g["edges"] if e["type"] == "OWNED_BY")
                    for e in g["edges"]:
                        if e["from"] == op["id"] and e["type"] == "PAYEE": e["to"] = holder
                    cp["party_id"] = holder
                elif defect == "processing":
                    op["properties"]["processing"]["gaps"] = ["fabricated"]
                else:
                    op["properties"]["counterparty_clarification"] = {"role": "payee", "name_raw": "False", "unp_raw": "123456789", "original_detail_raw": "False", "answered_by": "owner", "answered_at": "2026-10-10T12:00:00Z", "answer_ref": "answer", "confirmation_ref": "confirm"}
                self.rejected(g, c, "invalid owner clarification" if defect == "clarification" else "stale or false row processing admission" if defect == "processing" else "unproven counterparty identity")
        for unp, status in (("123456789", "ready"), ("AB0012345", "ready"),
                            ("AB 0012345", "ready"), ("АB0012345", "needs_clarification"),
                            ("ab0012345", "needs_clarification"), ("AB001234?", "needs_clarification"),
                            ("000000000", "needs_clarification"), ("", "needs_clarification")):
            g, c, st, op = self.fixture(unp=unp)
            r = Reader(self.root)
            self.assertEqual(r.search({"processing_status": status})["total"], 1)
            self.assertEqual(r.get(op["id"])["node"], op)

    def test_source_proven_blank_text_and_false_blank_are_distinct(self):
        g, c, st, op = self.fixture(purpose="", accepted=True)
        reader = Reader(self.root)
        self.assertEqual(reader.search({"processing_status": "ready"})["total"], 1)
        p = reader.get(op["id"])["node"]["properties"]
        self.assertEqual((p["purpose_raw"], p["payment1c_comment_raw"], p["payment1c_text"]["value"]), ("", "", ""))
        g, c, st, op = self.fixture(purpose="Printed business text")
        op["properties"]["purpose_raw"] = op["properties"]["payment1c_comment_raw"] = ""
        with canonical_sources() as (facts, _, _, _):
            op["properties"]["payment1c_text"] = facts.prepare_payment_text("")
            facts.materialize_processing(g)
        self.save(g, c)
        with self.assertRaises(QueryError):
            Reader(self.root)

    def test_owner_physical_type_blank_unp_and_provenance_are_distinct(self):
        for unp, person_type, status in (("", "physical_person", "ready"),
                                       ("0", "physical_person", "needs_clarification"),
                                       ("", "legal_entity", "needs_clarification")):
            g, c, st, op = self.fixture(unp=unp)
            answer = {"operation_id": op["id"], "role": op["properties"]["counterparty"]["role"],
                      "person_type": person_type, "origin": "owner_confirmed",
                      "original_detail_raw": next(x["raw"] for x in op["observations"] if x["field"] == "identity_detail_raw"),
                      "answered_by": "synthetic-owner", "answered_at": "2026-10-13T09:00:00Z",
                      "answer_ref": "synthetic-type-answer", "confirmation_ref": "synthetic-type-confirmation"}
            with canonical_sources() as (facts, _, _, _):
                resolved = facts.classify_counterparty_types(g, {op["id"]: answer})
            self.save(resolved, c)
            reader = Reader(self.root)
            self.assertEqual(reader.search({"processing_status": status})["total"], 1)
            value = reader.get(op["id"])["node"]
            self.assertEqual(value["properties"]["counterparty"], op["properties"]["counterparty"])
            self.assertEqual(value["observations"], op["observations"])
            next(n for n in resolved["nodes"] if n["id"] == op["id"])["properties"]["owner_counterparty_type"]["operation_id"] = "other-row"
            with canonical_sources() as (facts, _, _, _):
                facts.materialize_processing(resolved)
            self.save(resolved, c)
            with self.assertRaises(QueryError):
                Reader(self.root)

    def test_supplied_processing_summaries_bind_to_recomputed_admission(self):
        for defect in ("status", "ready_count", "row", "future_ready"):
            g, c, st, op = self.fixture(unp="")
            with canonical_sources() as (facts, _, _, _):
                admission = facts.identity_admission(g)
            self.assertEqual(admission["status"], "partial")
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest["inputs"][0].update(structure_check="a.json", processing_admission="partial")
            data = {"processing_admission": copy.deepcopy(admission)}
            if defect == "status":
                data["processing_admission"]["status"] = "admitted"
                manifest["inputs"][0]["processing_admission"] = "admitted"
            elif defect == "ready_count": data["processing_admission"]["statements"][0]["ready_count"] = 1
            elif defect == "row": data["processing_admission"]["statements"][0]["rows"][0]["status"] = "ready"
            else:
                with canonical_sources() as (_, _, _, readiness):
                    data.update(profile=readiness.PROFILE, future_processing_ready=True)
            write(self.root / "manifest.json", manifest)
            write(self.root / "a.json", data)
            with self.subTest(defect=defect), self.assertRaises(QueryError): Reader(self.root)
            data = {"processing_admission": admission, "future_processing_ready": False}
            manifest["inputs"][0]["processing_admission"] = "partial"
            write(self.root / "manifest.json", manifest);write(self.root / "a.json", data)
            self.assertEqual(Reader(self.root).search({"processing_status": "needs_clarification"})["total"], 1)
        # Legacy unknown declaration remains diagnostic; it is not a ready claim.
        for status in ("unknown", "not_evaluated"):
            make_archive(self.root)
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest["inputs"][0].update(structure_check="a.json", processing_admission=status)
            write(self.root / "manifest.json", manifest)
            write(self.root / "a.json", {"processing_admission": {"status": status}})
            self.assertEqual(Reader(self.root).search({"processing_status": "blocked_format"})["total"], 9)
            # Unknown status never excuses a contradictory known count.
            write(self.root / "a.json", {"processing_admission": {"status": status,
                "statements": [{"statement_id": "s1", "ready_count": 1}]}})
            with self.assertRaisesRegex(QueryError, 'ready_count disagrees'):
                Reader(self.root)
        # The accepted source checker deliberately emits not_evaluated when
        # semantic_validation=False. Test the exact generated diagnostic too
        # wherever the optional canonical schema runtime is available.
        try:
            import jsonschema
        except ModuleNotFoundError:
            return
        g, c, st, op = self.fixture()
        with canonical_sources() as (_, _, _, readiness):
            diagnostic = readiness.check(g, c, json.loads((self.root / 'r.json').read_text()), semantic_validation=False)
        self.assertEqual(diagnostic['processing_admission'], 'not_evaluated')
        self.assertFalse(diagnostic['future_processing_ready'])
        manifest = json.loads((self.root / 'manifest.json').read_text())
        manifest['inputs'][0].update(source_check='a.json', processing_admission='not_evaluated')
        write(self.root / 'manifest.json', manifest)
        write(self.root / 'a.json', diagnostic)
        self.assertEqual(Reader(self.root).search({'processing_status': 'ready'})['total'], 1)

    def test_unit_context_and_accepted_controls_parity(self):
        for defect in ("epoch", "anomaly", "header", "currency", "unknown_count", "control_review", "receipt_errors", "missing_errors"):
            with self.subTest(defect=defect):
                g, c, st, op = self.fixture(accepted=True)
                control = c["statements"][0]
                if defect == "epoch":
                    control["source_unit_review"].update(source_unit="BYR_2000_historical_ruble", source_epoch="pre_2016_07_01")
                elif defect == "anomaly": control["source_unit_review"]["printed_label_anomaly"] = True
                elif defect == "header": st["properties"]["heading_raw"] = "Wrong source heading"
                elif defect == "currency": next(n for n in g["nodes"] if n["type"] == "CurrencyContext")["properties"]["printed_currency"] = "BYR"
                elif defect == "unknown_count": control["printed_operation_count"] = {"status": "unknown", "value": None, "raw": None}
                elif defect == "control_review": control["source_review"].update(review_status="unknown", reference=None)
                else:
                    receipt = json.loads((self.root / "k.json").read_text()); receipt.pop("errors") if defect == "missing_errors" else receipt.update(errors=["unresolved"])
                    write(self.root / "k.json", receipt)
                self.save(g, c)
                with self.assertRaises(QueryError): Reader(self.root)
                if defect == "epoch":
                    # Same unsupported declaration remains readable diagnostics,
                    # isolated as unknown rather than an incorrect epoch group.
                    manifest = json.loads((self.root / "manifest.json").read_text())
                    manifest["inputs"][0].pop("source_check");manifest["inputs"][0].pop("source_review")
                    write(self.root / "manifest.json", manifest)
                    self.assertEqual(Reader(self.root).aggregate()["items"][0]["unit_status"], "unknown")
        g, c, st, op = self.fixture(accepted=True)
        r = Reader(self.root)
        self.assertEqual(r.aggregate()["items"][0]["unit_status"], "verified")
        self.assertEqual(r.get(op["id"])["node"], op)


class ReaderTests(SemanticSourceMixin, CanonicalHostIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_archive(self.root)
        self.reader = Reader(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_routes_and_confirmed_canonical_lookup(self):
        for status in ("confirmed", "candidate", "rejected", None):
            for defect in (None, "mismatch", "missing_edge", "extra_edge", "wrong_type", "foreign", "canonical_missing", "canonical_nonmember", "canonical_wrongtype"):
                with self.subTest(status=status, defect=defect):
                    make_archive(self.root)
                    g = json.loads((self.root / "g1.json").read_text())
                    p = {"basis": "Synthetic comparison", "canonical_id": "o11", "member_ids": ["o11"], "evidence_refs": ["synthetic"]}
                    if status is not None: p["status"] = status
                    n = {"id": "duplicate-demo", "type": "DuplicateDecision", "properties": p, "observations": [], "unrecognized_fields": []}
                    e = {"id": "duplicate-route", "type": "DUPLICATE_MEMBER", "from": n["id"], "to": "o11"}
                    if defect == "mismatch": e["to"] = "o12"
                    elif defect == "wrong_type": p["member_ids"] = ["a1"]
                    elif defect == "foreign": p["member_ids"] = ["o21"]
                    elif defect == "canonical_missing": p["canonical_id"] = "missing"
                    elif defect == "canonical_nonmember": p["canonical_id"] = "o12"
                    elif defect == "canonical_wrongtype": p["canonical_id"] = "a1"
                    g["nodes"].append(n)
                    if defect != "missing_edge": g["edges"].append(e)
                    if defect == "extra_edge": g["edges"].append({**e, "id": "duplicate-extra", "to": "o12"})
                    write(self.root / "g1.json", g)
                    invalid = defect in {"mismatch", "missing_edge", "extra_edge", "wrong_type", "foreign"} or status == "confirmed" and defect in {"canonical_missing", "canonical_nonmember", "canonical_wrongtype"}
                    if invalid:
                        with self.assertRaises(QueryError): Reader(self.root)
                        proc = subprocess.run([sys.executable, "-B", str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")), "get", "--archive", str(self.root), "--id", n["id"]], capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 2)
                        self.assertNotIn("Traceback", proc.stderr)
                    else:
                        r = Reader(self.root)
                        self.assertEqual(r.get(n["id"])["node"], n)
                        self.assertEqual([v["neighbor"]["id"] for v in r.links(n["id"], ["DUPLICATE_MEMBER"], "out")["items"]], p["member_ids"])
                        if status == "confirmed": self.assertEqual(r.get(p["canonical_id"])["node"]["id"], "o11")
        # Legacy empty diagnostic decision and Statement member both remain usable.
        for member in (None, "s1"):
            make_archive(self.root)
            g = json.loads((self.root / "g1.json").read_text())
            n = {"id": "dup", "type": "DuplicateDecision", "properties": {} if member is None else {"status": "confirmed", "member_ids": [member], "canonical_id": member, "evidence_refs": ["synthetic"]}}
            g["nodes"].append(n)
            if member: g["edges"].append({"id": "dup-edge", "type": "DUPLICATE_MEMBER", "from": "dup", "to": member})
            write(self.root / "g1.json", g)
            self.assertEqual(Reader(self.root).get("dup")["node"], n)

    def test_strict_json_source_parameter_cursor_and_output_boundaries(self):
        from reader import encoded, strict_json, bounded
        import hashlib
        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            for name in ("manifest.json", "g1.json", "u1.json", "structure_check", "conversion_check", "source_review", "source_check"):
                with self.subTest(token=token, source=name):
                    make_archive(self.root)
                    if name.endswith(".json"):
                        path = self.root / name
                        raw = path.read_text().rstrip(); path.write_text(raw[:-1] + ',"opaque":{"v":' + token + '}}')
                    else:
                        m = json.loads((self.root / "manifest.json").read_text()); m["inputs"][0][name] = "bad.json"
                        write(self.root / "manifest.json", m); (self.root / "bad.json").write_text('{"opaque":' + token + '}')
                    before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                    with self.assertRaises(QueryError): Reader(self.root)
                    proc = subprocess.run([sys.executable, "-B", str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")), "overview", "--archive", str(self.root)], capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 2); self.assertNotIn("Traceback", proc.stderr)
                    json.loads(proc.stderr, parse_constant=lambda v: self.fail(v))
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
            make_archive(self.root)
            for command, args in (("search", ["--filters", '{"purpose_contains":' + token + '}']), ("aggregate", ["--filters", '{"amount":' + token + '}']), ("links", ["--id", "o11", "--edge-types", '[' + token + ']'])):
                proc = subprocess.run([sys.executable, "-B", str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")), command, "--archive", str(self.root), *args], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2); self.assertNotIn("Traceback", proc.stderr)
            r = Reader(self.root)
            cursor = r.search(limit=1)["next_cursor"]
            payload = json.loads(base64.urlsafe_b64decode(cursor)); payload.pop("check"); payload["opaque"] = float(token) if token != "1e9999" else float("inf")
            payload["check"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            with self.assertRaises(QueryError): r.search(limit=1, cursor=forged)
            for method in (lambda: r.search({"amount": payload["opaque"]}),
                           lambda: r.aggregate({"amount": payload["opaque"]}),
                           lambda: r.links("o11", [payload["opaque"]]),
                           lambda: r.get(payload["opaque"])):
                with self.assertRaises(QueryError): method()
            for boundary in (encoded, bounded):
                with self.assertRaises(QueryError): boundary({"nested": [payload["opaque"]]})
            with self.assertRaises(QueryError): strict_json('{"nested":[' + token + ']}')
        make_archive(self.root)
        g = json.loads((self.root / "g1.json").read_text()); extension = {"value": 1.25, "raw": "NaN Infinity -Infinity 1e9999", "unknown": None}
        g["nodes"][0]["properties"]["opaque"] = extension; write(self.root / "g1.json", g)
        response = Reader(self.root).get("s1")
        self.assertEqual(response["node"]["properties"]["opaque"], extension)
        self.assertEqual(strict_json(encoded(response)), response)

    def test_manifest_identity_period_and_counts_bind_to_single_graph_statement(self):
        for field, value in (("statement_id", None), ("statement_id", "s2"),
                             ("statement_id", "a1"), ("operations", 2), ("operations", True),
                             ("ready", 3), ("held", 0), ("period_start", "2015-01-01"),
                             ("period_end", "2016-2-01")):
            with self.subTest(field=field, value=value):
                make_archive(self.root)
                manifest = json.loads((self.root / "manifest.json").read_text())
                manifest["inputs"][0][field] = value
                write(self.root / "manifest.json", manifest)
                with self.assertRaises(QueryError):
                    Reader(self.root)
        for defect in ("missing", "unexpected"):
            make_archive(self.root)
            manifest = json.loads((self.root / "manifest.json").read_text())
            if defect == "missing":
                del manifest["inputs"][0]["statement_id"]
                write(self.root / "manifest.json", manifest)
            else:
                graph = json.loads((self.root / "g1.json").read_text())
                other = copy.deepcopy(graph["nodes"][0])
                other["id"] = "unexpected_statement"
                graph["nodes"].append(other)
                write(self.root / "g1.json", graph)
            self.assert_archive_rejected("manifest statement_id must name the graph's single Statement" if defect == "unexpected" else "manifest input.statement_id must be a nonempty string")
        make_archive(self.root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["inputs"][0].update(operations=3, ready=0, held=3,
                                     period_start="2016-01-01", period_end="2016-06-30")
        manifest["coverage"] = {"statements": 3, "operations": 9, "operation_bearing_statements": 3}
        write(self.root / "manifest.json", manifest)
        self.assertEqual(Reader(self.root).search()["total"], 9)
        manifest["coverage"]["operations"] = 8
        write(self.root / "manifest.json", manifest)
        self.assert_archive_rejected("manifest coverage.operations disagrees with graph count")

    def test_consumed_required_nullable_fields_and_real_source_dates(self):
        schema = json.loads((Path(__file__).resolve().parents[2] /
                             "onec_bank_graph/references/graph.schema.json").read_text())
        required = ("transaction_date", "document_number_raw", "amount", "printed_debit", "printed_credit", "purpose_raw")
        canonical = schema["$defs"]["Operation"]["properties"]["properties"]["required"]
        self.assertTrue(set(required) <= set(canonical))
        for field in required:
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            del graph["nodes"][3]["properties"][field]
            write(self.root / "g1.json", graph)
            with self.assertRaisesRegex(QueryError, "Operation requires"):
                Reader(self.root)
        for kind, fields in (("Operation", ("transaction_date", "booking_date", "value_date", "document_date")),
                             ("Statement", ("period_start", "period_end", "statement_date", "previous_operation_date"))):
            for field in fields:
                for value in ("2016-2-01", "2016-02-30", "20160201", "2016-02-01T00:00:00", "", 42, True, []):
                    with self.subTest(kind=kind, field=field, value=value):
                        make_archive(self.root)
                        graph = json.loads((self.root / "g1.json").read_text())
                        graph["nodes"][3 if kind == "Operation" else 0]["properties"][field] = value
                        write(self.root / "g1.json", graph)
                        with self.assertRaises(QueryError):
                            Reader(self.root)
        self.assert_archive_rejected("Statement.previous_operation_date must be a string or null")
        for value in (None, "2016-02-29"):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            op = graph["nodes"][3]
            op["properties"]["transaction_date"] = value
            op["properties"]["document_number_raw"] = None
            write(self.root / "g1.json", graph)
            reader = Reader(self.root)
            self.assertEqual(reader.get("o11")["node"], op)
            self.assertEqual(reader.search({"date_from": "2016-01-01", "date_to": "2016-12-31", "operation_id": "o11"})["total"], int(value is not None))
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][3]["properties"]["purpose_raw"] = None
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("Operation.purpose_raw must be a string")

    def test_statement_completeness_year_and_ordinal_source_contract(self):
        for field, value in (("rows_complete", None), ("rows_complete", 1),
                             ("expected_operations", True), ("expected_operations", -1),
                             ("expected_operations", "3"), ("year", True), ("year", 1899),
                             ("year", "2016"), ("year", 2015), ("period_start", "2016-12-31")):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][0]["properties"][field] = value
            if field == "period_start":
                graph["nodes"][0]["properties"]["period_end"] = "2016-01-01"
            write(self.root / "g1.json", graph)
            with self.subTest(field=field, value=value), self.assertRaises(QueryError):
                Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "o13"]
        graph["edges"] = [e for e in graph["edges"] if e["to"] != "o13"]
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("rows_complete Statement expected_operations disagrees with indexed rows")
        graph["nodes"][0]["properties"]["rows_complete"] = False
        write(self.root / "g1.json", graph)
        reader = Reader(self.root)
        self.assertEqual(reader.search({"statement_id": "s1"})["total"], 2)
        self.assertEqual(reader.get("s1")["node"]["properties"]["expected_operations"], 3)
        graph["nodes"][4]["properties"]["ordinal"] = 1
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("Statement operation ordinal sequence is inconsistent")
        graph["nodes"][0]["properties"].update(expected_operations=None, year=None)
        write(self.root / "g1.json", graph)
        self.assertEqual(Reader(self.root).search({"statement_id": "s1"})["total"], 2)

    def test_all_selected_sidecar_identity_and_count_bindings(self):
        for label in ("structure_check", "conversion_check", "source_review", "source_check"):
            for defect in ("statement_id", "operations", "operation_count", "node_count", "edge_count",
                           "foreign_record", "duplicate_record", "record_count", "ready_count", "rows", "acceptance_ref"):
                with self.subTest(label=label, defect=defect):
                    make_archive(self.root)
                    manifest = json.loads((self.root / "manifest.json").read_text())
                    manifest["inputs"][0][label] = "sidecar.json"
                    write(self.root / "manifest.json", manifest)
                    data = {"statement_id": "s1", "operation_count": 3,
                            "statements": [{"statement_id": "s1", "operations": 3}]}
                    if defect in ("statement_id", "operations", "operation_count", "node_count", "edge_count"):
                        data[defect] = "s2" if defect == "statement_id" else 42
                    elif defect == "foreign_record":
                        data["statements"][0]["statement_id"] = "s2"
                    elif defect == "duplicate_record":
                        data["statements"].append(copy.deepcopy(data["statements"][0]))
                    elif defect == "record_count":
                        data["statements"][0]["operations"] = 2
                    elif defect == "ready_count":
                        data["processing_admission"] = {"statements": [{"statement_id": "s1", "ready_count": 3}]}
                    elif defect == "rows":
                        data["statements"][0]["rows"] = [{"operation_id": "o11"}]
                    else:
                        data["source_acceptance_ref"] = "foreign.json"
                    write(self.root / "sidecar.json", data)
                    with self.assertRaises(QueryError):
                        Reader(self.root)
        make_archive(self.root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["inputs"][0]["structure_check"] = "sidecar.json"
        write(self.root / "manifest.json", manifest)
        write(self.root / "sidecar.json", {"valid": True, "operation_count": 3,
            "processing_admission": {"status": "blocked", "statements": [{"statement_id": "s1",
                "operation_count": 3, "ready_count": 0, "rows": [{"operation_id": f"o1{i}"} for i in range(1, 4)]}]}})
        self.assertEqual(Reader(self.root).search()["total"], 9)

    def financial_fixture(self, accepted=False):
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][0]["properties"].update(opening="-5.00", closing="-5.00",
            printed_debit_total="2.00", printed_credit_total="2.00", opening_sign_rule="signed")
        for op, amount in zip(graph["nodes"][3:6], ("1.25", "2.00", "0.75")):
            p = op["properties"]
            p.update(direction="outflow" if p["ordinal"] == 2 else "inflow", row_status="complete")
            p.update(amount=amount, printed_debit=amount if p["direction"] == "outflow" else None,
                     printed_credit=amount if p["direction"] == "inflow" else None)
        refresh_processing(graph)
        write(self.root / "g1.json", graph)
        controls = json.loads((self.root / "u1.json").read_text())
        controls["statements"][0].update(opening="-5.00", closing="-5.00", printed_debit_total="2.00",
            printed_credit_total="2.00", no_activity=False,
            printed_operation_count={"status": "observed", "value": 3, "raw": "Количество: 3"})
        write(self.root / "u1.json", controls)
        if accepted:
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest["inputs"][0]["source_check"] = "sidecar.json"
            write(self.root / "manifest.json", manifest)
            write(self.root / "sidecar.json", {"source_ready": True, "accepted": True, "errors": [], "operation_count": 3,
                "statements": [{"statement_id": "s1", "operations": 3, "math_status": "pass", "differences": {"debit_rows": "0.00"}}]})
        return graph, controls

    def test_exact_financial_controls_include_held_rows_and_signed_balances(self):
        for accepted in (False, True):
            graph, controls = self.financial_fixture(accepted)
            reader = Reader(self.root)
            self.assertEqual(reader.aggregate({"statement_id": "s1"})["matched_operations"], 3)
            self.assertEqual(reader.search({"statement_id": "s1", "processing_status": "blocked_format"})["total"], 3)
            for defect in ("opening", "closing", "printed_debit_total", "printed_credit_total", "row_amount",
                           "row_cells", "direction", "no_activity", "count_value", "count_raw", "absent_count"):
                with self.subTest(accepted=accepted, defect=defect):
                    graph, controls = self.financial_fixture(accepted)
                    control = controls["statements"][0]
                    if defect in ("opening", "closing", "printed_debit_total", "printed_credit_total"):
                        control[defect] = "1.00"
                    elif defect == "row_amount":
                        graph["nodes"][3]["properties"]["amount"] = "1.26"
                    elif defect == "row_cells":
                        graph["nodes"][3]["properties"].update(amount="1.26", printed_credit="1.26")
                    elif defect == "direction":
                        graph["nodes"][3]["properties"]["direction"] = "outflow"
                    elif defect == "no_activity":
                        control["no_activity"] = True
                    elif defect == "count_value":
                        control["printed_operation_count"]["value"] = 2
                    elif defect == "count_raw":
                        control["printed_operation_count"]["raw"] = "4"
                    else:
                        control["printed_operation_count"]["status"] = "absent"
                    write(self.root / "g1.json", graph)
                    write(self.root / "u1.json", controls)
                    with self.assertRaises(QueryError):
                        Reader(self.root)
        graph, controls = self.financial_fixture(True)
        graph["nodes"][3]["properties"]["amount"] = "1.26"
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("controlled row amount disagrees with bank cells")

    def test_partial_financial_unknowns_and_empty_na_are_preserved(self):
        graph, controls = self.financial_fixture()
        graph["nodes"][0]["properties"]["rows_complete"] = False
        graph["nodes"][3]["properties"].update(amount=None, printed_credit=None, direction="unknown", row_status="partial")
        controls["statements"][0]["printed_operation_count"] = {"status": "unknown", "value": None, "raw": None}
        refresh_processing(graph)
        write(self.root / "g1.json", graph)
        write(self.root / "u1.json", controls)
        reader = Reader(self.root)
        self.assertEqual(reader.get("o11")["node"], graph["nodes"][3])
        self.assertEqual(reader.aggregate({"statement_id": "s1", "direction": "unknown"})["items"][0]["unknown_amount_count"], 1)
        graph, controls = self.financial_fixture(True)
        graph["nodes"] = [n for n in graph["nodes"] if n["type"] not in ("Operation", "AmountComponent")]
        graph["edges"] = [e for e in graph["edges"] if e["type"] not in ("CONTAINS", "HAS_COMPONENT")]
        graph["nodes"][0]["properties"].update(expected_operations=0, archive_only=True,
                                               printed_debit_total=None, printed_credit_total=None)
        controls["statements"][0].update(no_activity=True, printed_debit_total=None, printed_credit_total=None,
                                        printed_operation_count={"status": "absent", "value": None, "raw": None})
        refresh_processing(graph)
        write(self.root / "g1.json", graph)
        write(self.root / "u1.json", controls)
        write(self.root / "sidecar.json", {"accepted": True, "source_ready": True, "errors": [], "operation_count": 0,
            "statements": [{"statement_id": "s1", "operations": 0, "math_status": "not_applicable", "differences": {}}]})
        self.assertEqual(Reader(self.root).aggregate({"statement_id": "s1"})["matched_operations"], 0)
        controls["statements"][0]["printed_credit_total"] = "0.00"
        graph["nodes"][0]["properties"]["printed_credit_total"] = "0.00"
        refresh_processing(graph)
        write(self.root / "g1.json", graph)
        write(self.root / "u1.json", controls)
        self.assert_archive_rejected("accepted empty source must retain equal balances and unprinted turnovers")

    def test_financial_sidecars_cannot_claim_stale_sums_or_contradictory_acceptance(self):
        for field, value in (("computed_debit", "2.01"), ("computed_credit", "2.01"),
                             ("computed_expense", "2.01"), ("computed_income", "2.01")):
            self.financial_fixture()
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest["inputs"][0]["conversion_check"] = "sidecar.json"
            write(self.root / "manifest.json", manifest)
            write(self.root / "sidecar.json", {"accepted": False, "status": "needs_review", "statements": [{"statement_id": "s1", field: value}]})
            with self.subTest(field=field), self.assertRaises(QueryError):
                Reader(self.root)
        for defect in ("missing_records", "math_status", "differences", "unknown_money", "incomplete", "missing_controls"):
            graph, controls = self.financial_fixture(True)
            data = json.loads((self.root / "sidecar.json").read_text())
            if defect == "missing_records":
                data["statements"] = []
            elif defect == "math_status":
                data["statements"][0]["math_status"] = "failed"
            elif defect == "differences":
                data["statements"][0]["differences"]["debit_rows"] = "0.01"
            elif defect == "unknown_money":
                graph["nodes"][3]["properties"]["amount"] = None
            elif defect == "incomplete":
                graph["nodes"][0]["properties"]["rows_complete"] = False
            else:
                manifest = json.loads((self.root / "manifest.json").read_text())
                del manifest["inputs"][0]["controls"]
                write(self.root / "manifest.json", manifest)
            write(self.root / "sidecar.json", data)
            write(self.root / "g1.json", graph)
            with self.subTest(defect=defect), self.assertRaises(QueryError):
                Reader(self.root)

    def test_exact_currency_route_matches_accepted_source_requirement(self):
        source = (Path(__file__).resolve().parents[2] /
                  "onec_bank_graph/scripts/source_readiness.py").read_text()
        function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                        and n.name == "source_unit_matches_statement")
        # The canonical source contract explicitly compares a list to [id], so
        # duplicate routes cannot establish a reviewed unit even if IDs match.
        comparisons = [n for n in ast.walk(function) if isinstance(n, ast.Compare)]
        self.assertTrue(any(isinstance(n.left, ast.Name) and n.left.id == "links"
                            and isinstance(n.comparators[0], ast.List) for n in comparisons))
        graph = json.loads((self.root / "g1.json").read_text())
        graph["edges"].append({**graph["edges"][0], "id": "duplicate_currency"})
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("statement currency_context_id must agree with its IN_CURRENCY edge")

    def test_source_money_syntax_and_nullable_reference_compatibility(self):
        for field, value in (("amount", "01.25"), ("printed_credit", "00"),
                             ("opening", "01.25"), ("closing", "-00.00")):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][0 if field in ("opening", "closing") else 3]["properties"][field] = value
            write(self.root / "g1.json", graph)
            with self.subTest(field=field), self.assertRaises(QueryError):
                Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "Operation"]
        graph["edges"] = [e for e in graph["edges"] if e["type"] not in ("CONTAINS", "HAS_COMPONENT")]
        graph["nodes"][0]["properties"].update(expected_operations=0, currency_context_id=None)
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("null currency reference disagrees with IN_CURRENCY edge")
        graph["edges"] = [e for e in graph["edges"] if e["type"] != "IN_CURRENCY"]
        write(self.root / "g1.json", graph)
        self.assertEqual(Reader(self.root).aggregate({"statement_id": "s1"})["matched_operations"], 0)

    def test_selected_source_and_processing_declarations_cannot_contradict(self):
        for label in ("structure_check", "conversion_check", "source_check"):
            for defect in ("source_ready", "processing_admission"):
                make_archive(self.root)
                manifest = json.loads((self.root / "manifest.json").read_text())
                manifest["inputs"][0].update({label: "sidecar.json", "processing_admission": "partial"})
                data = {"source_ready": False} if defect == "source_ready" else {"processing_admission": {"status": "ready"}}
                write(self.root / "manifest.json", manifest)
                write(self.root / "sidecar.json", data)
                with self.subTest(label=label, defect=defect), self.assertRaises(QueryError):
                    Reader(self.root)
        make_archive(self.root)
        write(self.root / "g1.json", {"schema_version": 1, "nodes": [{"id": "x", "type": "INVALID", "properties": {}}], "edges": []})
        self.assert_archive_rejected("unknown graph node type")

    def test_normalized_bank_movement_is_checked_without_controls(self):
        for defect in ("amount", "direction", "both_columns", "zero_columns", "missing_columns", "invalid_override"):
            make_archive(self.root)
            manifest = json.loads((self.root / "manifest.json").read_text())
            del manifest["inputs"][0]["controls"]
            write(self.root / "manifest.json", manifest)
            graph = json.loads((self.root / "g1.json").read_text())
            p = graph["nodes"][3]["properties"]
            if defect == "amount":
                p["amount"] = "999.00"
            elif defect == "direction":
                p["direction"] = "outflow"
            elif defect == "both_columns":
                p["printed_debit"] = p["printed_credit"]
            elif defect == "zero_columns":
                p.update(amount="0.00", printed_debit="0.00", printed_credit="0.00")
            elif defect == "missing_columns":
                p.update(printed_debit=None, printed_credit=None)
            else:
                p["bank_debit_is"] = "INVALID"
            write(self.root / "g1.json", graph)
            with self.subTest(defect=defect), self.assertRaises(QueryError):
                Reader(self.root)
        self.assert_archive_rejected("Operation.bank_debit_is must be outflow/inflow/unknown")
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        p = graph["nodes"][3]["properties"]
        p.update(bank_debit_is="inflow", printed_debit=p["amount"], printed_credit=None)
        write(self.root / "g1.json", graph)
        reader = Reader(self.root)
        self.assertEqual(reader.get("o11")["node"], graph["nodes"][3])
        self.assertFalse(reader.search({"operation_id": "o11"})["items"][0]["bank_inflow_candidate"])
        self.assertEqual(reader.aggregate({"operation_id": "o11"})["items"][0]["sum_known_amount"], p["amount"])

    def test_supplied_rko_conditional_shape_before_loaded_verified_filter(self):
        valid = {"scope": "cash_revenue_rko", "database": "test", "status": "loaded_verified",
                 "document": {"date": "2016-01-01", "number": "001", "type": "Расходный кассовый ордер"},
                 "observed_at": "2016-01-02T12:34:56.123456+03:00", "posted": True,
                 "deletion_mark": False, "observed_on": "2016-01-02",
                 "observed_precision": "exact_timestamp", "snapshot_atomic": False}
        schema = json.loads((Path(__file__).resolve().parents[2] /
                             "onec_bank_graph/references/graph.schema.json").read_text())
        canonical = schema["$defs"]["OneCRko"]
        self.assertEqual(set(valid), set(canonical["required"]))
        variants = [{k: v for k, v in valid.items() if k != field} for field in valid]
        variants += [{**valid, field: value} for field, values in {
            "scope": (None, "other"), "database": (None, "prod"),
            "document": (None, [], {}, {"date": "2016-02-30", "number": "001", "type": "Расходный кассовый ордер"},
                         {"date": "2016-01-01", "number": "1/2", "type": "Расходный кассовый ордер"}),
            "posted": (False, None, 1), "deletion_mark": (True, None, 0),
            "observed_at": (None, "2016-02-30T12:00:00Z", "2016-01-01T12:00:00", "2016-01-01T12:00:00+99:00"),
            "observed_on": (None, "2016-1-01"), "observed_precision": (None, "unknown_time"),
            "snapshot_atomic": (None, 1)}.items() for value in values]
        for number, annotation in enumerate(variants):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][3]["properties"]["one_c_rko"] = annotation
            write(self.root / "g1.json", graph)
            with self.subTest(case=number), self.assertRaises(QueryError):
                Reader(self.root)
        self.assert_archive_rejected("OneCRko snapshot/observation precision is invalid")
        for status in ("unknown", "candidate", "pending", "partial", "loaded_verified"):
            make_archive(self.root)
            annotation = {**valid, "status": status}
            if status != "loaded_verified":
                annotation.update(document=None, posted=None, deletion_mark=None,
                                  observed_at=None, observed_precision="unknown_time")
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][3]["properties"]["one_c_rko"] = annotation
            write(self.root / "g1.json", graph)
            reader = Reader(self.root)
            self.assertEqual(reader.search({"rko_status": status})["total"], 9 if status == "unknown" else 1)
            self.assertEqual(reader.get("o11")["node"]["properties"]["one_c_rko"], annotation)
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            return
        validator = Draft202012Validator(canonical, format_checker=FormatChecker())
        self.assertTrue(validator.is_valid(valid))
        # jsonschema's RFC3339 formatter is optional. The installed SDK runtime
        # may have no date-time checker; the reader still verifies real calendar
        # timestamps itself. Do not mistake a missing formatter for validation.
        syntactic = [v for v in variants if v.get("observed_at") not in
                     ("2016-02-30T12:00:00Z", "2016-01-01T12:00:00+99:00")]
        self.assertTrue(all(not validator.is_valid(v) for v in syntactic))
        if "date-time" in FormatChecker().checkers:
            self.assertTrue(all(not validator.is_valid(v) for v in variants))

    def test_supplied_payment_text_ready_held_conditional_value(self):
        valid = {"profile": "xml10-single-line-escp-cash-v1", "status": "ready",
                 "value": "Synthetic purpose", "diagnostics": []}
        for field, value in (("profile", "other"), ("value", None), ("value", ""),
                             ("diagnostics", None), ("diagnostics", [None])):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][3]["properties"]["payment1c_text"] = {**valid, field: value}
            write(self.root / "g1.json", graph)
            with self.subTest(field=field, value=value), self.assertRaises(QueryError):
                Reader(self.root)
        for status, value in (("ready", "Synthetic purpose"), ("held", None)):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            node = graph["nodes"][3]
            if status == "ready":
                node["properties"]["purpose_raw"] = value
                node["properties"]["payment1c_comment_raw"] = value
                # Ready text requires a source-bound purpose. Supply a legacy
                # source detail plus proven bank observations for that claim.
                node["properties"]["purpose_status"] = "observed"
                node["properties"]["classification"] = None
                node["observations"].append({"field": "identity_detail_raw", "raw": value, "normalized": value, "status": "observed"})
                for field in ("printed_debit", "printed_credit"):
                    cell = node["properties"][field]
                    node["observations"].append({"field": field, "raw": cell or "", "normalized": cell, "status": "observed" if cell is not None else "absent"})
            with canonical_sources() as (facts, _, _, _):
                annotation = facts.prepare_payment_text(node["properties"]["purpose_raw"], node["properties"].get("purpose_status", "unknown"))
            graph["nodes"][3]["properties"]["payment1c_text"] = annotation
            refresh_processing(graph)
            write(self.root / "g1.json", graph)
            reader = Reader(self.root)
            self.assertEqual(reader.get("o11")["node"]["properties"]["payment1c_text"], annotation)
            self.assertEqual(reader.search({"operation_id": "o11", "payment_text_status": status})["total"], 1)
        graph["nodes"][3]["properties"]["payment1c_text"]["value"] = "Fake held value"
        write(self.root / "g1.json", graph)
        self.assert_archive_rejected("held payment1c_text requires null value")

    def test_required_nullable_denomination_and_control_copy_fields(self):
        for kind, fields in (("CurrencyContext", ("printed_currency",)),
                             ("Statement", ("opening", "closing", "printed_debit_total", "printed_credit_total"))):
            for field in fields:
                make_archive(self.root)
                graph = json.loads((self.root / "g1.json").read_text())
                del graph["nodes"][1 if kind == "CurrencyContext" else 0]["properties"][field]
                write(self.root / "g1.json", graph)
                with self.subTest(kind=kind, field=field), self.assertRaises(QueryError):
                    Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][1]["properties"]["printed_currency"] = None
        write(self.root / "g1.json", graph)
        reader = Reader(self.root)
        self.assertIsNone(reader.aggregate({"statement_id": "s1"})["items"][0]["printed_currency"])
        self.assertEqual(reader.get("s1")["node"]["properties"]["opening"], None)

    def test_counterparty_identity_is_local_and_agrees_with_relevant_route(self):
        for direction, role, edge_type in (("inflow", "payer", "PAYER"),
                                           ("outflow", "payee", "PAYEE"),
                                           ("unknown", "payer", "PAYER"),
                                           ("unknown", "unknown", "PAYEE")):
            for defect in ("valid", "missing", "foreign", "wrong_type", "missing_edge",
                           "mismatched", "duplicate", "conflicting", "wrong_role"):
                with self.subTest(direction=direction, role=role, defect=defect):
                    make_archive(self.root)
                    graph = json.loads((self.root / "g1.json").read_text())
                    graph["nodes"].extend([{"id": name, "type": "Party", "properties": {}}
                                           for name in ("party", "other")])
                    op = graph["nodes"][3]
                    op["properties"]["direction"] = direction
                    if direction == "outflow":
                        op["properties"]["printed_debit"] = op["properties"]["amount"]
                        op["properties"]["printed_credit"] = None
                    if direction == "unknown":
                        op["properties"].update(amount=None, printed_debit=None, printed_credit=None, bank_debit_is="unknown", row_status="partial")
                    cp = {"role": role, "party_id": "party", "status": "unknown",
                          "name_raw": None, "unp_raw": "000123"}
                    op["properties"]["counterparty"] = cp
                    edge = {"id": "identity", "type": edge_type, "from": "o11", "to": "party"}
                    graph["edges"].append(edge)
                    expected = "counterparty party_id must agree with exactly one relevant PAYER/PAYEE edge"
                    if defect in ("missing", "foreign", "wrong_type"):
                        cp["party_id"] = {"missing": "missing", "foreign": "foreign", "wrong_type": "a1"}[defect]
                        expected = "counterparty party_id must name a Party in its graph"
                        if defect == "foreign":
                            foreign = json.loads((self.root / "g2.json").read_text())
                            foreign["nodes"].append({"id": "foreign", "type": "Party", "properties": {}})
                            write(self.root / "g2.json", foreign)
                    elif defect == "missing_edge":
                        graph["edges"].remove(edge)
                    elif defect == "mismatched":
                        edge["to"] = "other"
                    elif defect in ("duplicate", "conflicting"):
                        graph["edges"].append({**edge, "id": "extra_identity",
                                               "to": "other" if defect == "conflicting" else "party"})
                    elif defect == "wrong_role":
                        edge["type"] = "PAYEE" if edge_type == "PAYER" else "PAYER"
                        if direction == "unknown" and role == "unknown":
                            cp["role"] = "payer" if edge_type == "PAYER" else "payee"
                    refresh_processing(graph)
                    write(self.root / "g1.json", graph)
                    if defect == "valid":
                        reader = Reader(self.root)
                        self.assertEqual(reader.get("o11")["node"], op)
                        self.assertEqual(reader.aggregate({"operation_id": "o11"}, "counterparty")["items"][0]["group"], "party")
                        self.assertEqual(reader.links("o11", [edge_type], "out")["items"][0]["neighbor"]["id"], "party")
                    else:
                        self.assert_archive_rejected(expected)
        # Unknown identity does not require inventing a party or a role edge.
        for value in (None, {"role": "unknown", "party_id": None, "status": "unknown",
                                "name_raw": None, "unp_raw": None}):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][3]["properties"]["counterparty"] = value
            refresh_processing(graph)
            write(self.root / "g1.json", graph)
            reader = Reader(self.root)
            self.assertEqual(reader.get("o11")["node"], graph["nodes"][3])
            self.assertIsNone(reader.aggregate({"operation_id": "o11"}, "counterparty")["items"][0]["group"])

    def test_supplied_counterparty_observed_contract_and_nullable_unknown_values(self):
        valid = {"role": "payer", "status": "observed", "party_id": "party",
                 "name_raw": "Synthetic\nName", "unp_raw": "123456789"}
        cases = [(field, None) for field in ("party_id", "name_raw", "unp_raw")]
        cases += [("role", "unknown"), ("name_raw", ""), ("party_id", ""),
                  ("unp_raw", "000000000"), ("unp_raw", "12345678"), ("unp_raw", "unknown")]
        for field, value in cases:
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            graph["nodes"][3]["properties"]["counterparty"] = {**valid, field: value}
            write(self.root / "g1.json", graph)
            self.assert_archive_rejected("observed counterparty must satisfy the canonical identity contract")
        for field in ("party_id", "name_raw", "unp_raw"):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            cp = {**valid, "status": "unknown"}
            del cp[field]
            graph["nodes"][3]["properties"]["counterparty"] = cp
            write(self.root / "g1.json", graph)
            self.assert_archive_rejected("counterparty party_id/name_raw/unp_raw fields are required (null allowed)")

    def test_review_contract_matches_canonical_and_invalid_reviews_stay_unknown(self):
        source = (Path(__file__).resolve().parents[2] /
                  "onec_bank_graph/scripts/statement_graph.py").read_text()
        # Extract only the pure canonical predicate, without loading converter
        # dependencies or changing its module search path.
        function = next(n for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name == "source_unit_review_valid")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "canonical-review", "exec"), namespace)
        canonical = namespace["source_unit_review_valid"]
        valid = {"status": "verified", "source_unit": "synthetic-unit", "source_epoch": "synthetic-epoch",
                 "transfer_factor": "1", "printed_label_anomaly": False, "basis": "Synthetic evidence"}
        reviews = [None, {}, {"status": "unknown"}]
        for field, values in {"status": (None, "unknown", "VERIFIED"),
                              "source_unit": (None, "unsupported"),
                              "source_epoch": (None, "unsupported"),
                              "transfer_factor": (None, "10000", "1.0", 1, True),
                              "printed_label_anomaly": (None, 0, 1, "false", [], {}),
                              "basis": (None, "", "  ", 1, [], {})}.items():
            reviews.append({k: v for k, v in valid.items() if k != field})
            reviews.extend({**valid, field: value} for value in values)
        for unit, epoch in (("synthetic-unit", "synthetic-epoch"),
                            ("BYR_2000_historical_ruble", "pre_2016_07_01"),
                            ("BYN_2009_ruble", "from_2016_07_01")):
            for anomaly in (False, True):
                reviews.append({**valid, "source_unit": unit, "source_epoch": epoch,
                                "printed_label_anomaly": anomaly})
        for review in reviews:
            with self.subTest(review=review):
                self.assertEqual(source_unit_review_valid(review), canonical(review))
                make_archive(self.root)
                controls = json.loads((self.root / "u1.json").read_text())
                controls["statements"][0]["source_unit_review"] = review
                write(self.root / "u1.json", controls)
                reader = Reader(self.root)
                row = reader.aggregate({"operation_id": "o11"}, "none")["items"][0]
                with canonical_sources() as (_, _, _, readiness):
                    graph = json.loads((self.root / "g1.json").read_text())
                    verified = readiness.source_unit_matches_statement(graph, graph["nodes"][0], review)
                self.assertEqual(row["unit_status"], "verified" if verified else "unknown")
                self.assertEqual(row["source_unit"], review["source_unit"] if verified else None)
                self.assertEqual(row["source_epoch"], review["source_epoch"] if verified else None)
                self.assertEqual(row["unknown_unit_statement"], None if verified else "s1")
                self.assertEqual(reader.get("o11")["node"]["properties"]["purpose_raw"],
                                 "Внесение выручки\nполное назначение")
        # Equal invalid claims from separate statements must remain isolated.
        make_archive(self.root)
        for index in (1, 2):
            controls = json.loads((self.root / f"u{index}.json").read_text())
            controls["statements"][0]["source_unit_review"] = {**valid, "transfer_factor": "10000"}
            write(self.root / f"u{index}.json", controls)
        rows = Reader(self.root).aggregate(group_by="none", limit=100)["items"]
        self.assertEqual({r["unknown_unit_statement"] for r in rows}, {"s1", "s2", "s3"})
        self.assertTrue(all(r["unit_status"] == "unknown" for r in rows))

    def test_opaque_ids_remain_retrievable_with_output_bound_and_json_errors(self):
        for length in (1001, 8193, 30000):
            make_archive(self.root)
            graph = json.loads((self.root / "g1.json").read_text())
            renamed = {"o11": "operation:" + "я" * length, "s1": "statement:" + "я" * length}
            for node in graph["nodes"]:
                node["id"] = renamed.get(node["id"], node["id"])
                if node["type"] == "Operation":
                    node["properties"]["statement_id"] = renamed["s1"]
            for edge in graph["edges"]:
                for key in ("from", "to"):
                    edge[key] = renamed.get(edge[key], edge[key])
            controls = json.loads((self.root / "u1.json").read_text())
            controls["statements"][0]["statement_id"] = renamed["s1"]
            write(self.root / "u1.json", controls)
            manifest = json.loads((self.root / "manifest.json").read_text())
            manifest["inputs"][0]["statement_id"] = renamed["s1"]
            write(self.root / "manifest.json", manifest)
            write(self.root / "g1.json", graph)
            graph["nodes"][3]["properties"]["document_number_raw"] = "long-id-row"
            write(self.root / "g1.json", graph)
            reader = Reader(self.root)
            row = reader.search({"document_number": "long-id-row"}, limit=1)["items"][0]
            self.assertEqual(row["id"], renamed["o11"])
            self.assertEqual(reader.get(row["id"])["node"], graph["nodes"][3])
            linked = reader.links(row["id"], ["CONTAINS"], "in", limit=1)
            self.assertEqual(linked["items"][0]["neighbor"]["id"], row["statement_id"])
            self.assertEqual(reader.get(row["statement_id"])["node"], graph["nodes"][0])
            result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                     "get", "--archive", str(self.root), "--id", row["id"]],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["node"], graph["nodes"][3])
        for method in (self.reader.get, self.reader.links):
            for value in (None, [], {}, 1, True):
                with self.subTest(method=method.__name__, value=value):
                    with self.assertRaisesRegex(QueryError, "node_id must be a string"):
                        method(value)

    def test_gap_free_pages_and_query_bound_cursor(self):
        result = self.reader.search(limit=2)
        ids = []
        while True:
            ids.extend(row["id"] for row in result["items"])
            if not result["next_cursor"]:
                break
            result = self.reader.search(limit=2, cursor=result["next_cursor"])
        self.assertEqual(ids, [p["id"] for p in self.reader.operations])
        cursor = self.reader.search(limit=2)["next_cursor"]
        with self.assertRaises(QueryError):
            self.reader.search({"direction": "inflow"}, 2, cursor)
        with self.assertRaises(QueryError):
            self.reader.search(limit=3, cursor=cursor)
        with self.assertRaises(QueryError):
            self.reader.search(cursor="%%%%")
        with self.assertRaises(QueryError):
            self.reader.search(cursor=base64.urlsafe_b64encode(b"[]").decode())
        with self.assertRaises(QueryError):
            self.reader.search(cursor=base64.urlsafe_b64encode(b'"scalar"').decode())
        data = json.loads((self.root / "u1.json").read_text())
        data["changed"] = True
        write(self.root / "u1.json", data)
        with self.assertRaises(QueryError):
            Reader(self.root).search(limit=2, cursor=cursor)

    def test_exact_raw_and_typed_neighbors(self):
        p = self.reader.get("o11")["node"]["properties"]
        self.assertEqual(p["purpose_raw"], "Внесение выручки\nполное назначение")
        self.assertEqual(p["source_text"]["value"], "source projection")
        self.assertIsNone(p["payment1c_text"]["value"])
        self.assertEqual(self.reader.links("o11", direction="in")["items"][0]["neighbor"], {"id": "s1", "type": "Statement"})
        self.assertEqual(self.reader.links("o12", ["HAS_COMPONENT"], "out")["items"][0]["neighbor"]["type"], "AmountComponent")
        with self.assertRaises(QueryError):
            self.reader.get("missing")
        with self.assertRaises(QueryError):
            self.reader.links("o11", ["arbitrary"])

    def test_raw_counterparty_filters_do_not_select_absent_values(self):
        values = [None, "unknown", "", "000123", "Known\nName"]
        for index in range(1, 4):
            graph = json.loads((self.root / f"g{index}.json").read_text())
            for ordinal, node in enumerate(n for n in graph["nodes"] if n["type"] == "Operation"):
                position = (index - 1) * 3 + ordinal
                if position < len(values):
                    value = values[position]
                    node["properties"]["counterparty"] = {"role": "unknown", "status": "unknown", "party_id": None,
                                                              "name_raw": value, "unp_raw": value}
                elif position == len(values):
                    node["properties"]["counterparty"] = {"role": "unknown", "status": "unknown", "party_id": None,
                                                              "name_raw": None, "unp_raw": None}
            refresh_processing(graph)
            write(self.root / f"g{index}.json", graph)
        reader = Reader(self.root)
        for field in ("counterparty_name", "counterparty_unp"):
            for value, expected in (("unknown", "o12"), ("000123", "o21"), ("Known\nName", "o22")):
                with self.subTest(field=field, value=value):
                    result = reader.search({field: value})
                    self.assertEqual([row["id"] for row in result["items"]], [expected])
                    self.assertEqual(reader.aggregate({field: value})["matched_operations"], 1)
        self.assertEqual(reader.search({"rko_status": "unknown"})["total"], 9)

    def assert_archive_rejected(self, message):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaisesRegex(QueryError, re.escape(message)):
            Reader(self.root)
        for command in ("overview", "search", "aggregate"):
            with self.subTest(command=command):
                result = subprocess.run([
                    sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                    command, "--archive", str(self.root)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(json.loads(result.stderr), {
                    "contract_version": "bank-graph-reader-v1", "error": message})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_operation_reference_agrees_with_exactly_one_contains(self):
        for defect in ("absent", "wrong_owner", "conflicting", "duplicate",
                       "foreign_reference", "unresolved_reference", "wrong_type_reference"):
            with self.subTest(defect=defect):
                make_archive(self.root)
                graph = json.loads((self.root / "g1.json").read_text())
                edge = next(e for e in graph["edges"] if e["to"] == "o11")
                expected = "operation statement_id must agree with exactly one incoming CONTAINS edge"
                if defect.endswith("reference"):
                    graph["nodes"][3]["properties"]["statement_id"] = {
                        "foreign_reference": "s2", "unresolved_reference": "missing",
                        "wrong_type_reference": "a1"}[defect]
                    expected = ("operation belongs to another graph" if defect == "foreign_reference"
                                else "operation statement_id is unresolved")
                elif defect == "absent":
                    graph["edges"].remove(edge)
                else:
                    other = copy.deepcopy(graph["nodes"][0])
                    other["id"] = "other_statement"
                    graph["nodes"].append(other)
                    if defect == "wrong_owner":
                        edge["from"] = other["id"]
                    else:
                        extra = {**edge, "id": "extra_contains"}
                        if defect == "conflicting":
                            extra["from"] = other["id"]
                        graph["edges"].append(extra)
                write(self.root / "g1.json", graph)
                self.assert_archive_rejected(expected)

    def test_statement_account_has_one_agreeing_local_route_even_when_empty(self):
        for empty in (False, True):
            for defect in ("absent", "null", "nonstring", "blank", "foreign", "unresolved",
                           "wrong_type", "missing_edge", "mismatched", "conflicting", "duplicate"):
                with self.subTest(empty=empty, defect=defect):
                    make_archive(self.root)
                    graph = json.loads((self.root / "g1.json").read_text())
                    if empty:
                        graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "Operation"]
                        graph["edges"] = [e for e in graph["edges"]
                                          if e["type"] not in ("CONTAINS", "HAS_COMPONENT")]
                    p = graph["nodes"][0]["properties"]
                    expected = "statement account_id must name an Account in its graph"
                    if defect == "absent":
                        del p["account_id"]
                    elif defect in ("null", "nonstring", "blank", "foreign", "unresolved", "wrong_type"):
                        p["account_id"] = {"null": None, "nonstring": [], "blank": "",
                                           "foreign": "a2", "unresolved": "missing", "wrong_type": "c1"}[defect]
                    else:
                        expected = "statement account_id must agree with exactly one FOR_ACCOUNT edge"
                        edge = next(e for e in graph["edges"] if e["type"] == "FOR_ACCOUNT")
                        if defect == "missing_edge":
                            graph["edges"].remove(edge)
                        elif defect == "duplicate":
                            graph["edges"].append({**edge, "id": "extra_account_edge"})
                        else:
                            graph["nodes"].append({"id": "other_account", "type": "Account", "properties": {}})
                            if defect == "mismatched":
                                edge["to"] = "other_account"
                            else:
                                graph["edges"].append({**edge, "id": "extra_account_edge", "to": "other_account"})
                    if defect in ("absent", "null", "nonstring", "blank"):
                        expected = "Statement.account_id must be a nonempty string"
                    write(self.root / "g1.json", graph)
                    self.assert_archive_rejected(expected)

    def test_consumed_required_source_enums_reject_invalid_values(self):
        for label, fields in SOURCE_ENUMS.items():
            for field, choices in fields.items():
                for value in ("missing", None, "", "INVALID", [], {}):
                    with self.subTest(label=label, field=field, value=value):
                        make_archive(self.root)
                        graph = json.loads((self.root / "g1.json").read_text())
                        node = graph["nodes"][0 if label == "Statement" else 3]
                        p = node["properties"]
                        if "." in label:
                            nested = label.split(".")[1]
                            p = p.setdefault(nested, {"role": "unknown", "status": "unknown", "party_id": None, "name_raw": None, "unp_raw": None} if nested == "counterparty" else {})
                        if value == "missing":
                            p.pop(field, None)
                        else:
                            p[field] = value
                        write(self.root / "g1.json", graph)
                        self.assert_archive_rejected(f"{label}.{field} must be one of {'/'.join(choices)}")

    def test_source_enums_match_canonical_schema_and_valid_values_preserve_behavior(self):
        schema = json.loads((Path(__file__).resolve().parents[2] /
                             "onec_bank_graph/references/graph.schema.json").read_text())
        defs = schema["$defs"]
        nested_defs = {"counterparty": "CounterpartyIdentity", "processing": "RowProcessing", "payment1c_text": "Payment1cText", "one_c_rko": "OneCRko"}
        for label, fields in SOURCE_ENUMS.items():
            kind, _, nested = label.partition(".")
            properties = defs[kind]["properties"]["properties"]
            if nested:
                properties = (defs[nested_defs[nested]] if nested in nested_defs else
                              properties["properties"][nested])
            for field, choices in fields.items():
                self.assertIn(field, properties["required"])
                definition = properties["properties"][field]
                if "$ref" in definition:
                    definition = defs[definition["$ref"].rsplit("/", 1)[1]]
                self.assertEqual(tuple(definition["enum"]), choices)
                for value in choices:
                    source_enums({k: v[0] for k, v in fields.items()} | {field: value}, label)
        # Source-bound status behavior is covered by SemanticSourceTests on
        # canonically proven ready/held/observed/unknown/settlement fixtures.
        # Optional downstream statuses must not invent unknown source objects.
        for obj in nested_defs.keys() | {"settlement"}:
            for absent in (False, True):
                make_archive(self.root)
                graph = json.loads((self.root / "g1.json").read_text())
                p = graph["nodes"][3]["properties"]
                p.pop(obj, None)
                if not absent:
                    p[obj] = None
                if obj != "processing":
                    refresh_processing(graph)
                write(self.root / "g1.json", graph)
                self.assertEqual(Reader(self.root).get("o11")["node"], graph["nodes"][3])

    def test_cli_parser_errors_use_json_and_help_remains_successful(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        cases = [["search", "--limit", value] for value in ("1.5", "abc", "true")]
        cases += [["unknown"], ["search", "--unknown"], [], ["search", "--limit"],
                  ["search", "--archive"], ["get"], ["links"], ["search", "--lim", "2"]]
        for args in cases:
            with self.subTest(args=args):
                # Parsing and required-id checks must run before archive I/O.
                result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                         "--archive", str(self.root / "unavailable"), *args],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("usage:", result.stderr)
                error = json.loads(result.stderr)
                self.assertEqual(error["contract_version"], "bank-graph-reader-v1")
                self.assertIsInstance(error["error"], str)
                self.assertNotEqual(error["error"], "archive file unavailable or invalid JSON")
        for args in (["--help"], ["search", "--help"]):
            result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("usage:", result.stdout)
            self.assertEqual(result.stderr, "")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_controls_belong_to_statements_in_their_graph(self):
        for sid in ("s2", "missing", "c1", "o11"):
            with self.subTest(statement_id=sid):
                make_archive(self.root)
                controls = json.loads((self.root / "u1.json").read_text())
                controls["statements"][0]["statement_id"] = sid
                write(self.root / "u1.json", controls)
                if sid == "s2":
                    # Both IDs still exist and remain globally unique.
                    other = json.loads((self.root / "u2.json").read_text())
                    other["statements"][0]["statement_id"] = "s1"
                    write(self.root / "u2.json", other)
                self.assert_archive_rejected(
                    "control statement_id must name a Statement in its graph")
        make_archive(self.root)
        controls = json.loads((self.root / "u1.json").read_text())
        controls["statements"].append(copy.deepcopy(controls["statements"][0]))
        write(self.root / "u1.json", controls)
        self.assert_archive_rejected("duplicate statement controls")

    def test_valid_and_missing_controls_preserve_unit_epochs(self):
        for index, unit, epoch in ((1, "BYR_2000_historical_ruble", "pre_2016_07_01"),
                                  (2, "BYN_2009_ruble", "from_2016_07_01")):
            row = self.reader.aggregate({"operation_id": f"o{index}1"})["items"][0]
            self.assertEqual((row["source_unit"], row["source_epoch"], row["unit_status"]),
                             (unit, epoch, "verified"))
            self.assertIsNone(row["unknown_unit_statement"])
        for mode in ("missing_file_reference", "missing_review", "null_review", "unknown_review"):
            with self.subTest(mode=mode):
                make_archive(self.root)
                if mode == "missing_file_reference":
                    manifest = json.loads((self.root / "manifest.json").read_text())
                    del manifest["inputs"][0]["controls"]
                    write(self.root / "manifest.json", manifest)
                else:
                    control = {"statement_id": "s1"}
                    if mode != "missing_review":
                        control["source_unit_review"] = (None if mode == "null_review" else
                                                        {"status": "unknown", "source_unit": None,
                                                         "source_epoch": None})
                    write(self.root / "u1.json", {"statements": [control]})
                row = Reader(self.root).aggregate({"operation_id": "o11"})["items"][0]
                self.assertEqual(row["unit_status"], "unknown")
                self.assertEqual(row["unknown_unit_statement"], "s1")
                self.assertIsNone(row["source_unit"])
                self.assertIsNone(row["source_epoch"])

    def test_currency_reference_and_edge_belong_to_same_graph(self):
        for empty in (False, True):
            for defect in ("foreign", "missing_node", "wrong_type", "mismatched_edge",
                           "missing_edge", "extra_conflicting_edge"):
                with self.subTest(empty=empty, defect=defect):
                    make_archive(self.root)
                    graph = json.loads((self.root / "g1.json").read_text())
                    if empty:
                        graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "Operation"]
                        graph["edges"] = [e for e in graph["edges"]
                                          if e["type"] not in ("CONTAINS", "HAS_COMPONENT")]
                        graph["nodes"][0]["properties"]["expected_operations"] = 0
                    if defect in ("foreign", "missing_node", "wrong_type"):
                        graph["nodes"][0]["properties"]["currency_context_id"] = {
                            "foreign": "c2", "missing_node": "missing", "wrong_type": "a1"}[defect]
                        expected = "statement currency_context_id must name a CurrencyContext in its graph"
                    else:
                        expected = "statement currency_context_id must agree with its IN_CURRENCY edge"
                        if defect == "missing_edge":
                            graph["edges"] = [e for e in graph["edges"] if e["type"] != "IN_CURRENCY"]
                        else:
                            graph["nodes"].append({"id": "other_currency", "type": "CurrencyContext",
                                                   "properties": {"printed_currency": "USD"}})
                            if defect == "mismatched_edge":
                                graph["edges"][0]["to"] = "other_currency"
                            else:
                                graph["edges"].append({"id": "other_currency_edge", "type": "IN_CURRENCY",
                                                       "from": "s1", "to": "other_currency"})
                    write(self.root / "g1.json", graph)
                    self.assert_archive_rejected(expected)

    def test_currency_null_and_unknown_source_values_preserved(self):
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][1]["properties"]["printed_currency"] = None
        write(self.root / "g1.json", graph)
        reader = Reader(self.root)
        self.assertIsNone(reader.aggregate({"operation_id": "o11"})["items"][0]["printed_currency"])
        self.assertEqual(reader.get("c1")["node"], graph["nodes"][1])
        self.assertEqual(reader.get("c1")["node"]["properties"]["semantics_status"], "unknown")
        self.assertEqual(reader.links("s1", ["IN_CURRENCY"], "out")["items"][0]["neighbor"]["id"], "c1")
        # Empty statements previously allowed an absent/null reference; there
        # is no operation to aggregate and no currency is inferred from links.
        for absent in (False, True):
            with self.subTest(absent=absent):
                make_archive(self.root)
                graph = json.loads((self.root / "g1.json").read_text())
                graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "Operation"]
                graph["edges"] = [e for e in graph["edges"]
                                  if e["type"] not in ("CONTAINS", "HAS_COMPONENT", "IN_CURRENCY")]
                graph["nodes"][0]["properties"]["expected_operations"] = 0
                if absent:
                    del graph["nodes"][0]["properties"]["currency_context_id"]
                else:
                    graph["nodes"][0]["properties"]["currency_context_id"] = None
                write(self.root / "g1.json", graph)
                reader = Reader(self.root)
                self.assertEqual(reader.aggregate({"statement_id": "s1"})["matched_operations"], 0)
                self.assertEqual(reader.get("s1")["node"], graph["nodes"][0])

    def test_get_nested_mutations_cannot_change_snapshot_or_responses(self):
        graph = json.loads((self.root / "g1.json").read_text())
        original = graph["nodes"][3]
        original["properties"]["source_text"]["nested"] = [{"values": ["original"]}]
        original["observations"][0]["extra"] = {"values": ["observed"]}
        original["unrecognized_fields"] = [{"raw": {"values": ["unrecognized"]}}]
        original["extra_top_level"] = {"values": [{"raw": "extra"}]}
        write(self.root / "g1.json", graph)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        reader = Reader(self.root)
        snapshot = reader.snapshot
        search = reader.search(limit=100)
        aggregate = reader.aggregate(limit=100)
        links = reader.links("o11")
        for evict in (False, True, False):
            with self.subTest(evict=evict):
                returned = reader.get("o11")
                node = returned["node"]
                node["properties"]["amount"] = "999.00"
                node["properties"]["purpose_raw"] = "changed"
                node["properties"]["processing"]["status"] = "changed"
                node["properties"]["source_text"]["nested"][0]["values"].append("changed")
                node["observations"][0]["extra"]["values"].clear()
                node["observations"].append({"raw": "changed"})
                node["unrecognized_fields"][0]["raw"]["values"][0] = "changed"
                node["extra_top_level"]["values"][0]["raw"] = "changed"
                if evict:
                    reader.get("o21")
                self.assertEqual(reader.get("o11")["node"], original)
                self.assertEqual(reader.search(limit=100), search)
                self.assertEqual(reader.aggregate(limit=100), aggregate)
                self.assertEqual(reader.links("o11"), links)
                self.assertEqual(reader.snapshot, snapshot)
        for node_id in ("s1", "c1"):
            node = reader.get(node_id)["node"]
            node["properties"].clear()
            node["observations"].append({"raw": "changed"})
            expected = next(n for n in graph["nodes"] if n["id"] == node_id)
            self.assertEqual(reader.get(node_id)["node"], expected)
            self.assertEqual(reader.search(limit=100), search)
            self.assertEqual(reader.aggregate(limit=100), aggregate)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_all_public_responses_own_every_nested_mutable_value(self):
        # Include source extensions, not just the currently projected fields.
        # Returned JSON must remain editable while the reader keeps its snapshot.
        nested = {"evidence": [{"raw": ["original\n000123", None]}]}
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["status"] = copy.deepcopy(nested)
        for item in manifest["inputs"]:
            item["source_ready"] = copy.deepcopy(nested)
            item["processing_admission"] = copy.deepcopy(nested)
        write(self.root / "manifest.json", manifest)
        for index in range(1, 4):
            controls = json.loads((self.root / f"u{index}.json").read_text())
            controls["statements"][0]["source_unit_review"]["evidence_refs"] = copy.deepcopy(nested)
            write(self.root / f"u{index}.json", controls)
            graph = json.loads((self.root / f"g{index}.json").read_text())
            for node in graph["nodes"]:
                node["properties"]["source_extension"] = copy.deepcopy(nested)
                node["observations"].append({"field": "extension", "raw": None, "normalized": None, "status": "unknown", "extra": copy.deepcopy(nested)})
                node["unrecognized_fields"].append(copy.deepcopy(nested))
                node["extra"] = copy.deepcopy(nested)
            for edge in graph["edges"]:
                edge["extra"] = copy.deepcopy(nested)
            write(self.root / f"g{index}.json", graph)
        reader = Reader(self.root)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        snapshot = reader.snapshot

        calls = {"contract": contract, "overview": reader.overview,
                 "search": lambda: reader.search(limit=100),
                 "get": lambda: reader.get("o11"),
                 "links": lambda: reader.links("s1", limit=100),
                 "aggregate": lambda: reader.aggregate(group_by="none", limit=100)}
        # First and later pages, every currency edge endpoint/direction, and
        # non-Operation nodes are observable parts of the ownership contract.
        for method in ("search", "aggregate"):
            fn = getattr(reader, method)
            calls[method + "_first_page"] = lambda fn=fn: fn(limit=1)
            cursor = fn(limit=1)["next_cursor"]
            calls[method + "_next_page"] = lambda fn=fn, cursor=cursor: fn(limit=1, cursor=cursor)
        cursor = reader.links("s1", limit=1)["next_cursor"]
        calls["links_first_page"] = lambda: reader.links("s1", limit=1)
        calls["links_next_page"] = lambda: reader.links("s1", limit=1, cursor=cursor)
        for node_id, direction in (("s1", "out"), ("s1", "both"), ("c1", "in"), ("c1", "both")):
            calls[f"currency_links_{node_id}_{direction}"] = (
                lambda node_id=node_id, direction=direction:
                reader.links(node_id, ["IN_CURRENCY"], direction))
        for node_id in ("s1", "c1", "a1", "fee1"):
            calls["get_" + node_id] = lambda node_id=node_id: reader.get(node_id)
        expected = {name: copy.deepcopy(fn()) for name, fn in calls.items()}
        units = expected["aggregate"]["items"]
        self.assertEqual({row["source_epoch"] for row in units}, {"pre_2016_07_01", "from_2016_07_01", None})
        self.assertEqual({row["unit_status"] for row in units}, {"verified", "unknown"})

        def mutate(value):
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    if isinstance(child, (dict, list)):
                        mutate(child)
                    else:
                        value[key] = "caller replacement"
                value["caller_added"] = [{"changed": True}]
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, (dict, list)):
                        mutate(child)
                    else:
                        value[index] = "caller replacement"
                value.append({"caller_added": [True]})

        def assert_unchanged():
            for name, fn in calls.items():
                self.assertEqual(fn(), expected[name], name)
            self.assertEqual(reader.snapshot, snapshot)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

        for name, fn in calls.items():
            with self.subTest(returned_response=name):
                returned = fn()
                mutate(returned)
                for cached_node in ("o11", "o11", "o21", "o11"):
                    reader.get(cached_node)
                    assert_unchanged()

        # Clearing or replacing containers/edge identity is allowed on a caller's
        # copy too; later pagination and both endpoints must remain consistent.
        for name in ("node_counts", "edge_counts", "source_files"):
            reader.overview()[name].clear()
            assert_unchanged()
        for field in ("status", "source_unit", "source_epoch"):
            reader.overview()["statements"][0]["source_unit_review"][field] = "changed"
            assert_unchanged()
        reader.overview()["statements"][0]["source_unit_review"].clear()
        assert_unchanged()
        for field, changed in (("to", "c2"), ("from", "s2"), ("type", "FOR_ACCOUNT"), ("id", "changed")):
            reader.links("s1", ["IN_CURRENCY"], "out")["items"][0]["edge"][field] = changed
            assert_unchanged()
        reader.links("s1", ["IN_CURRENCY"], "out")["items"][0]["edge"].clear()
        assert_unchanged()

    def test_json_resource_boundaries_preserve_supported_finite_values(self):
        current_limits = (sys.getrecursionlimit(), getattr(sys, "get_int_max_str_digits", lambda: 0)())
        for raw in ('[' * 10000 + 'null' + ']' * 10000, '9' * 5000,
                    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e9999}', '{bad'):
            with self.subTest(kind=raw[:12]):
                with self.assertRaises(QueryError):
                    strict_json(raw)
        supported = strict_json('[' * MAX_JSON_DEPTH + 'null' + ']' * MAX_JSON_DEPTH)
        self.assertEqual(encoded(supported), '[' * MAX_JSON_DEPTH + 'null' + ']' * MAX_JSON_DEPTH)
        self.assertEqual(encoded(detached_json(supported)), encoded(supported))
        with self.assertRaises(QueryError):
            strict_json('[' * (MAX_JSON_DEPTH + 1) + 'null' + ']' * (MAX_JSON_DEPTH + 1))
        huge = 10 ** 5000
        cycle = []; cycle.append(cycle)
        deep = None
        for _ in range(10000):
            deep = [deep]
        for value in (huge, cycle, deep):
            for fn in (encoded, bounded, detached_json):
                with self.subTest(function=fn.__name__, kind=type(value).__name__):
                    with self.assertRaises(QueryError):
                        fn(value)
        ordinary = {"finite": 1.25, "smallest": -1e-300, "unknown": None,
                    "raw": "NaN Infinity -Infinity 1e9999 000123\nисходное",
                    "long_raw": "9" * 5000, "bool": True}
        self.assertEqual(strict_json(encoded(ordinary)), ordinary)
        at_limit = "9" * MAX_JSON_INTEGER_DIGITS
        self.assertEqual(encoded(strict_json(at_limit)), at_limit)
        with self.assertRaises(QueryError):
            strict_json(at_limit + "9")
        shared = {"x": ["raw", None]}
        clone = detached_json([shared, shared])
        clone[0]["x"][0] = "caller"
        self.assertEqual(clone[1], shared)
        self.assertEqual((sys.getrecursionlimit(), getattr(sys, "get_int_max_str_digits", lambda: 0)()), current_limits)
        self.assertEqual(contract()["limits"]["json_container_depth"], MAX_JSON_DEPTH)
        self.assertEqual(contract()["limits"]["json_integer_digits"], MAX_JSON_INTEGER_DIGITS)

    def test_all_selected_source_resource_errors_precede_admitted_ids(self):
        sources = ("manifest.json", "g1.json", "u1.json", "structure_check",
                   "conversion_check", "source_review", "source_check")
        for source in sources:
            for token in ('[' * 10000 + 'null' + ']' * 10000, '9' * 5000):
                with self.subTest(source=source, token_prefix=token[:12]):
                    make_archive(self.root)
                    if source.endswith(".json"):
                        path = self.root / source
                        original = path.read_text().rstrip()
                        path.write_text(original[:-1] + ',"opaque":' + token + '}')
                    else:
                        manifest = json.loads((self.root / "manifest.json").read_text())
                        manifest["inputs"][0][source] = "receipt.json"
                        write(self.root / "manifest.json", manifest)
                        (self.root / "receipt.json").write_text('{"opaque":' + token + '}')
                    before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                    with self.assertRaises(QueryError):
                        Reader(self.root)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        # A source beyond the depth limit is never admitted.
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][3]["properties"]["opaque"] = json.loads('[' * (MAX_JSON_DEPTH - 3) + 'null' + ']' * (MAX_JSON_DEPTH - 3))
        write(self.root / "g1.json", graph)
        with self.assertRaises(QueryError):
            Reader(self.root)
        # Valid parsed JSON can still exceed the full get byte limit. Refuse it
        # during construction, before a successful search can offer its ID.
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][3]["properties"]["opaque"] = "x" * 262144
        write(self.root / "g1.json", graph)
        with self.assertRaisesRegex(QueryError, "no data truncated"):
            Reader(self.root)

    def test_supported_deep_all_public_responses_are_readable_and_detached(self):
        nested = strict_json('[' * (MAX_JSON_DEPTH - 8) + '{"raw":"000123 NaN", "unknown":null,"finite":1.25}' + ']' * (MAX_JSON_DEPTH - 8))
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["status"] = nested
        manifest["inputs"][0]["source_ready"] = nested
        write(self.root / "manifest.json", manifest)
        controls = json.loads((self.root / "u1.json").read_text())
        controls["statements"][0]["source_unit_review"]["opaque"] = nested
        write(self.root / "u1.json", controls)
        graph = json.loads((self.root / "g1.json").read_text())
        for node in graph["nodes"]:
            node["properties"]["opaque"] = nested
        graph["edges"][0]["opaque"] = nested
        write(self.root / "g1.json", graph)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        reader = Reader(self.root)
        calls = {"contract": contract, "overview": reader.overview,
                 "search": lambda: reader.search(limit=1), "get": lambda: reader.get("o11"),
                 "links": lambda: reader.links("s1", limit=100), "aggregate": reader.aggregate}
        expected = {name: encoded(fn()) for name, fn in calls.items()}
        row = reader.search({"operation_id": "o11"}, limit=1)["items"][0]
        self.assertEqual(encoded(reader.get(row["id"])["node"]["properties"]["opaque"]), encoded(nested))
        self.assertEqual(encoded(reader.get("s1")["node"]["properties"]["opaque"]), encoded(nested))
        for name, fn in calls.items():
            returned = fn()
            stack = [returned]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
                    item["caller_added"] = "changed"
                elif isinstance(item, list):
                    stack.extend(v for v in item if isinstance(v, (dict, list)))
                    item.append("caller_added")
            for observed, again in calls.items():
                self.assertEqual(encoded(again()), expected[observed], (name, observed))
            # Switch the one-graph cache and return to the deep node.
            reader.get("o21")
            self.assertEqual(encoded(reader.get("o11")), expected["get"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_numeric_tokens_roundtrip_exactly_or_reject_before_admission(self):
        from decimal import Decimal
        supported = ("0.1", "1.25", "-1.25", "1e-300", "1e100", "0.000", "-0.0", "5e-324",
                     "9007199254740993", "0.100000000000000000000000")
        rejected = ("0.123456789012345678901234567890", "1e-10000", "-1e-10000",
                    "1.00000000000000001", "9007199254740993.0", "1e-324")
        for token in supported:
            with self.subTest(token=token):
                value = strict_json(token)
                self.assertEqual(Decimal(encoded(value)), Decimal(token))
        for token in rejected:
            with self.subTest(token=token):
                with self.assertRaisesRegex(QueryError, "round-trip"):
                    strict_json(token)
        for source in ("manifest.json", "g1.json", "u1.json", "structure_check",
                       "conversion_check", "source_review", "source_check"):
            for token in rejected:
                with self.subTest(source=source, token=token):
                    make_archive(self.root)
                    if source.endswith(".json"):
                        path = self.root / source
                        original = path.read_text().rstrip()
                        path.write_text(original[:-1] + ',"opaque":' + token + '}')
                    else:
                        manifest = json.loads((self.root / "manifest.json").read_text())
                        manifest["inputs"][0][source] = "receipt.json"
                        write(self.root / "manifest.json", manifest)
                        (self.root / "receipt.json").write_text('{"opaque":' + token + '}')
                    before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                    with self.assertRaises(QueryError):
                        Reader(self.root)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"][3]["properties"]["opaque"] = {"finite": [0.1, 1.25, -1e-300],
                                                       "raw": "000123 1e-10000", "unknown": None}
        write(self.root / "g1.json", graph)
        reader = Reader(self.root)
        self.assertEqual(reader.get("o11")["node"], graph["nodes"][3])

    def test_cli_core_filter_and_cursor_resource_errors_use_versioned_json(self):
        for token in ('[' * 10000 + 'null' + ']' * 10000, '9' * 5000):
            for command, args in (("search", ["--filters", '{"amount":' + token + '}']),
                                  ("aggregate", ["--filters", '{"amount":' + token + '}']),
                                  ("links", ["--id", "o11", "--edge-types", '[' + token + ']'])):
                result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                         command, "--archive", str(self.root), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(json.loads(result.stderr)["contract_version"], contract()["contract_version"])
                self.assertFalse(result.stdout)
            cursor = base64.urlsafe_b64encode(token.encode()).decode()
            for call in (lambda: self.reader.search(limit=1, cursor=cursor),
                         lambda: self.reader.links("o11", limit=1, cursor=cursor),
                         lambda: self.reader.aggregate(limit=1, cursor=cursor)):
                with self.assertRaises(QueryError):
                    call()
        deep = None
        for _ in range(10000):
            deep = [deep]
        for fn in (self.reader.search, self.reader.aggregate):
            with self.assertRaises(QueryError):
                fn({"amount": deep})
        short_nested_cursor = base64.urlsafe_b64encode(('{"x":' + '[' * 500 + 'null' + ']' * 500 + '}').encode()).decode()
        for call in (lambda: self.reader.search(limit=1, cursor=short_nested_cursor),
                     lambda: self.reader.links("o11", limit=1, cursor=short_nested_cursor),
                     lambda: self.reader.aggregate(limit=1, cursor=short_nested_cursor)):
            with self.assertRaises(QueryError):
                call()

    def test_malformed_archive_constructor_and_cli_errors(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        graph = json.loads((self.root / "g1.json").read_text())
        controls = json.loads((self.root / "u1.json").read_text())
        cases = [("manifest.json", []), ("manifest.json", {"inputs": [None]}),
                 ("manifest.json", {"inputs": [{"graph": []}]}), ("g1.json", []),
                 ("g1.json", {"schema_version": 1, "edges": []})]
        for field, value in (("nodes", None), ("nodes", {}), ("nodes", [None]),
                             ("edges", None), ("edges", {}), ("edges", [None])):
            broken = copy.deepcopy(graph)
            broken[field] = value
            cases.append(("g1.json", broken))
        for record, field, value in (("nodes", "id", []), ("nodes", "type", {}),
                                     ("nodes", "properties", []), ("edges", "id", []),
                                     ("edges", "from", {}), ("edges", "type", None)):
            broken = copy.deepcopy(graph)
            broken[record][0][field] = value
            cases.append(("g1.json", broken))
        for field, value in (("counterparty", []), ("processing", "ready"),
                             ("ordinal", None), ("transaction_date", []), ("purpose_raw", 123)):
            broken = copy.deepcopy(graph)
            broken["nodes"][3]["properties"][field] = value
            cases.append(("g1.json", broken))
        broken = copy.deepcopy(graph)
        broken["nodes"][0]["properties"]["selection"] = []
        cases.append(("g1.json", broken))
        for value in (None, {}, [None], [{"statement_id": []}],
                      [{"statement_id": "s1", "source_unit_review": "verified"}],
                      [{"statement_id": "s1", "source_unit_review": {"source_unit": []}}]):
            broken = copy.deepcopy(controls)
            broken["statements"] = value
            cases.append(("u1.json", broken))
        for number, (filename, value) in enumerate(cases):
            with self.subTest(case=number, filename=filename):
                make_archive(self.root)
                write(self.root / filename, value)
                with self.assertRaises(QueryError):
                    Reader(self.root)
                result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                         "overview", "--archive", str(self.root)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                error = json.loads(result.stderr)
                self.assertEqual(error["contract_version"], "bank-graph-reader-v1")
                self.assertIsInstance(error["error"], str)

    def test_manifest_artifact_path_constructor_and_cli_errors(self):
        # Every referenced artifact uses the same path boundary, including the
        # optional controls/checks. JSON can encode NUL even though files cannot.
        (self.root / "loop.json").symlink_to("loop.json")
        (self.root / "outside.json").symlink_to(self.root.parent / "outside.json")
        for key in ("graph", "controls", "structure_check", "conversion_check",
                    "source_review", "source_check"):
            for value in ("invalid\0graph.json", [], {}, 0, False, "",
                          "loop.json", "outside.json", "../outside.json",
                          str(self.root / "g1.json"), "g1.json/child.json"):
                with self.subTest(artifact=key, value=value):
                    make_archive(self.root)
                    manifest = json.loads((self.root / "manifest.json").read_text())
                    manifest["inputs"][0][key] = value
                    write(self.root / "manifest.json", manifest)
                    with self.assertRaises(QueryError):
                        Reader(self.root)
                    result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                             "overview", "--archive", str(self.root)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)
                    error = json.loads(result.stderr)
                    self.assertEqual(error["contract_version"], "bank-graph-reader-v1")
                    self.assertIsInstance(error["error"], str)
        make_archive(self.root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["inputs"][0].update({key: None for key in
                                    ("structure_check", "conversion_check", "source_review", "source_check")})
        write(self.root / "manifest.json", manifest)
        self.assertEqual(len(Reader(self.root).operations), 9)

    def test_negative_operation_money_constructor_and_cli_errors(self):
        for field in ("amount", "printed_debit", "printed_credit"):
            for value in ("-1.00", "-0.000000000000000000001"):
                with self.subTest(field=field, value=value):
                    make_archive(self.root)
                    graph = json.loads((self.root / "g1.json").read_text())
                    graph["nodes"][3]["properties"][field] = value
                    write(self.root / "g1.json", graph)
                    expected = f"Operation.{field} must be nonnegative"
                    with self.assertRaisesRegex(QueryError, expected):
                        Reader(self.root)
                    for command in ("overview", "search", "aggregate"):
                        with self.subTest(command=command):
                            result = subprocess.run([
                                sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                                command, "--archive", str(self.root)],
                                capture_output=True, text=True)
                            self.assertEqual(result.returncode, 2)
                            self.assertEqual(result.stdout, "")
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertEqual(json.loads(result.stderr), {
                                "contract_version": "bank-graph-reader-v1", "error": expected})

    def test_operation_money_compatibility_and_signed_balances(self):
        positive = "100000000000000000000000000000.000000000000000000001"
        for value in (None, "0.00", "-0.00", positive):
            with self.subTest(value=value):
                make_archive(self.root)
                graph = json.loads((self.root / "g1.json").read_text())
                balances = {"opening": "-12.3400", "closing": "-0.000000000000000000001"}
                graph["nodes"][0]["properties"].update(balances)
                operation = graph["nodes"][3]
                if value is None:
                    operation["properties"].update(amount=None, printed_credit=None, printed_debit=None,
                                                    direction="unknown", row_status="partial")
                elif value in ("0.00", "-0.00"):
                    operation["properties"]["printed_debit"] = value
                else:
                    operation["properties"].update(amount=value, printed_credit=value, printed_debit=None)
                refresh_processing(graph)
                write(self.root / "g1.json", graph)
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                reader = Reader(self.root)
                self.assertEqual(reader.get("o11")["node"], operation)
                self.assertEqual({field: reader.get("s1")["node"]["properties"][field] for field in balances}, balances)
                found = reader.search({"operation_id": "o11"})["items"][0]
                self.assertEqual({field: found[field] for field in ("amount", "printed_debit", "printed_credit")},
                                 {field: operation["properties"][field] for field in ("amount", "printed_debit", "printed_credit")})
                group = reader.aggregate({"operation_id": "o11"})["items"][0]
                self.assertEqual(group["unknown_amount_count"], int(value is None))
                self.assertEqual(group["sum_known_amount"], None if value is None else positive)
                self.assertEqual(reader.search({"amount": "-1.00"})["total"], 0)
                result = subprocess.run([sys.executable, str((Path(__file__).resolve().parents[2] / "onec_bank_graph/reader.py")),
                    "aggregate", "--archive", str(self.root), "--filters", json.dumps({"operation_id": "o11"})],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["items"][0]["sum_known_amount"], None if value is None else positive)
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_optional_sdk_absence_skips_but_installed_incompatibility_fails(self):
        # -S excludes the interpreter's installed SDK; fake packages represent
        # dependency/API regressions without installing or altering anything.
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(self.root), str(Path(__file__).resolve().parents[2] / "onec_bank_graph")))}
        command = [sys.executable, "-S", "-m", "unittest",
                   "test_mcp_adapter.AdapterTests.test_initialize_discovery_resources_queries_and_errors"]
        cwd = Path(__file__).parent
        absent = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True)
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn("skipped=1", absent.stderr)
        (self.root / "anyio.py").write_text("")
        package = self.root / "mcp"
        package.mkdir()
        (package / "server.py").write_text("MCPServer = object\n")
        (package / "shared").mkdir()
        (package / "shared/__init__.py").write_text("")
        for missing in ("ClientSession", "MCPServer", "memory"):
            with self.subTest(missing=missing):
                (package / "__init__.py").write_text("" if missing == "ClientSession" else "ClientSession = object\n")
                (package / "server.py").write_text("" if missing == "MCPServer" else "MCPServer = object\n")
                result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("FAILED (errors=1)", result.stderr)
                self.assertNotIn("skipped=", result.stderr)
                self.assertIn(missing, result.stderr)

    def test_unit_separated_precise_sums_no_components_and_null(self):
        result = self.reader.aggregate(group_by="none", limit=100)
        self.assertEqual(result["total"], 9)
        self.assertEqual(result["matched_operations"], 9)
        for row in result["items"]:
            if row["direction"] == "outflow":
                self.assertEqual(row["sum_known_amount"], "2.00")
            elif row["direction"] == "inflow":
                self.assertEqual(row["sum_known_amount"], "100000000000000000000000000000.000000000000000000001")
                self.assertEqual(row["unknown_amount_count"], 0)
                self.assertTrue(row["complete_amount_sum"])
            else:
                self.assertIsNone(row["sum_known_amount"])
                self.assertEqual(row["unknown_amount_count"], 1)
                self.assertFalse(row["complete_amount_sum"])
        self.assertEqual(len({(r["source_epoch"], r["unknown_unit_statement"]) for r in result["items"]}), 3)
        unknown = self.reader.aggregate({"operation_id": "o13"})["items"][0]
        self.assertIsNone(unknown["sum_known_amount"])
        self.assertEqual(unknown["known_amount_count"], 0)
        page = self.reader.aggregate(limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(self.reader.aggregate(limit=1, cursor=page["next_cursor"])["total"], 9)

    def test_inflow_gate_before_semantics_and_exact_filters(self):
        candidates = self.reader.search({"purpose_contains": "выручк"}, limit=100)["items"]
        self.assertEqual(sum(r["bank_inflow_candidate"] for r in candidates), 3)
        self.assertTrue(all(not r["bank_inflow_candidate"] for r in candidates if r["direction"] == "outflow"))
        self.assertEqual(self.reader.search({"amount": "2.0"}, limit=100)["total"], 3)
        self.assertEqual(self.reader.search({"document_number": "001", "date_to": "2016-01-01"})["total"], 1)
        self.assertEqual(self.reader.search({"document_number": "1"})["total"], 0)
        props = {"direction": "inflow", "printed_credit": "2", "printed_debit": "1", "amount": "2"}
        self.assertFalse(incoming_candidate(props, "outflow"))
        props["printed_debit"] = "0"
        self.assertTrue(incoming_candidate(props, "outflow"))

    def test_strict_bounds_validation_and_source_preservation(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for filters in ({"unknown": "x"}, {"amount": "NaN"}, {"date_from": "2016-02-31"}, {"date_from": "2017-01-01", "date_to": "2016-01-01"}):
            with self.assertRaises(QueryError):
                self.reader.search(filters)
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(QueryError):
                self.reader.search(limit=limit)
        self.reader.overview()
        self.reader.get("o11")
        self.reader.links("s1", limit=1)
        self.reader.aggregate()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_path_escape_collision_typed_edges_oversize(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["inputs"][0]["graph"] = "../outside.json"
        write(self.root / "manifest.json", manifest)
        with self.assertRaises(QueryError):
            Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["nodes"].append(graph["nodes"][0])
        write(self.root / "g1.json", graph)
        with self.assertRaises(QueryError):
            Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        graph["edges"][0]["to"] = "o11"
        write(self.root / "g1.json", graph)
        with self.assertRaises(QueryError):
            Reader(self.root)
        make_archive(self.root)
        graph = json.loads((self.root / "g1.json").read_text())
        p = graph["nodes"][3]["properties"]
        p["purpose_raw"] = p["payment1c_comment_raw"] = "x" * 270000
        with canonical_sources() as (facts, _, _, _):
            p["payment1c_text"] = facts.prepare_payment_text(p["purpose_raw"], "unknown")
        refresh_processing(graph)
        write(self.root / "g1.json", graph)
        with self.assertRaisesRegex(QueryError, "no data truncated"):
            Reader(self.root).get("o11")




if __name__ == "__main__":
    unittest.main()
