"""Bounded, offline consumer of the accepted bank archive; no source writes."""
from __future__ import annotations

import argparse
import base64
import builtins
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import importlib.util
from functools import lru_cache
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sys

VERSION = "bank-graph-reader-v1"
MAX_LIMIT = 100
MAX_OUTPUT_BYTES = 262144
MAX_QUERY_BYTES = 8192
# Common source/query/response bound, below MCP 2.x Pydantic serialization
# depth limits even after protocol envelopes. Rejection precedes ID admission.
MAX_JSON_DEPTH = 128
# Read the host's limit without changing process-wide interpreter settings.
MAX_JSON_INTEGER_DIGITS = min(4300, getattr(sys, "get_int_max_str_digits", lambda: 0)() or 4300)
FILTERS = {
    "operation_id", "statement_id", "date_from", "date_to", "direction",
    "document_number", "amount", "purpose_contains", "row_status",
    "duplicate_status", "processing_status", "payment_text_status",
    "settlement_status", "classification_status", "counterparty_name",
    "counterparty_unp", "counterparty_internal_id", "observed_counterparty_name",
    "observed_counterparty_unp", "payer_unp", "payee_unp", "rko_status", "selection",
}
EDGE_ENDPOINTS = {
    "CONTAINS": ({"Statement"}, {"Operation"}),
    "FOR_ACCOUNT": ({"Statement"}, {"Account"}),
    "IN_CURRENCY": ({"Statement"}, {"CurrencyContext"}),
    "OWNED_BY": ({"Account"}, {"Party"}),
    "SERVICED_BY": ({"Account"}, {"Bank"}),
    "PAYER": ({"Operation"}, {"Party"}),
    "PAYEE": ({"Operation"}, {"Party"}),
    "ON_BEHALF_OF": ({"Operation"}, {"Party"}),
    "FROM_ACCOUNT": ({"Operation"}, {"Account"}),
    "TO_ACCOUNT": ({"Operation"}, {"Account"}),
    "MENTIONS": ({"Operation"}, {"DocumentReference"}),
    "HAS_COMPONENT": ({"Operation"}, {"AmountComponent"}),
    "HAS_TAX": ({"Operation"}, {"TaxDetail"}),
    "HAS_CASH_SYMBOL": ({"Operation"}, {"CashSymbol"}),
    "DUPLICATE_MEMBER": ({"DuplicateDecision"}, {"Statement", "Operation"}),
}
# Required enum fields consumed by indexing/filtering, from graph.schema.json.
# Optional downstream objects remain optional; their enum is required when
# the object is supplied. Raw unknown/null values have separate contracts.
SOURCE_ENUMS = {
    "Statement": {"selection": ("include", "exclude_reissue", "hold"),
                  "debit_is": ("outflow", "inflow", "unknown"),
                  "opening_sign_rule": ("signed", "unknown")},
    "Operation": {"direction": ("inflow", "outflow", "unknown"),
                  "row_status": ("complete", "partial", "unreadable"),
                  "duplicate_status": ("unreviewed", "unique", "candidate", "confirmed_duplicate"),
                  "classification_status": ("observed", "verified", "inferred", "unknown", "absent", "unreadable", "not_applicable")},
    "Operation.counterparty": {"role": ("payer", "payee", "unknown"),
                               "status": ("observed", "unknown")},
    "Operation.processing": {"status": ("ready", "needs_clarification", "blocked_format")},
    "Operation.payment1c_text": {"status": ("ready", "held")},
    "Operation.settlement": {"status": ("verified_equation", "verified_withheld_fee", "unknown", "not_applicable")},
    "Operation.one_c_rko": {"status": ("unknown", "candidate", "pending", "partial", "loaded_verified")},
}


class QueryError(ValueError):
    """Invalid query/archive, stale cursor, missing ID or oversized response."""


class QueryArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise QueryError(message)


def source_enums(value, label):
    for field, choices in SOURCE_ENUMS.get(label, {}).items():
        if value.get(field) not in choices:
            raise QueryError(f"{label}.{field} must be one of {'/'.join(choices)}")


def json_resources(value):
    """Validate JSON iteratively, including cycles and opaque extensions.

    Depth counts containers, including the root. No recursive walk or coercion
    can discard an admitted field. Shared acyclic values are allowed.
    """
    stack = [(value, 0, False)]
    active = set()
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if isinstance(item, (dict, list)):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise QueryError(f"JSON nesting exceeds {MAX_JSON_DEPTH} containers; no data truncated")
            if id(item) in active:
                raise QueryError("JSON must not contain a cycle")
            active.add(id(item))
            stack.append((item, depth, True))
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise QueryError("JSON object keys must be strings")
                children = item.values()
            else:
                children = item
            stack.extend((child, depth, False) for child in children)
        elif type(item) is int:
            try:
                if len(str(abs(item))) > MAX_JSON_INTEGER_DIGITS:
                    raise QueryError(f"JSON integers exceed {MAX_JSON_INTEGER_DIGITS} digits")
            except ValueError as exc:
                raise QueryError(f"JSON integers exceed {MAX_JSON_INTEGER_DIGITS} digits") from exc
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise QueryError("JSON numbers must be finite; NaN/Infinity are invalid")
        elif item is not None and not isinstance(item, (str, bool)):
            raise QueryError("response/query must be finite JSON")
    return value


def strict_json(raw):
    """Normalize parser resource errors for every source and query boundary."""
    def reject_constant(value):
        raise QueryError("JSON numbers must be finite; NaN/Infinity are invalid")
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            reject_constant(value)
        # Compare decimal values of JSON tokens, not binary float expansion.
        # Ordinary roundtrippable floats (e.g. 0.1) remain numbers; do not
        # silently round long tokens or underflow a nonzero token to zero.
        if Decimal(value) != Decimal(str(parsed)):
            raise QueryError("JSON number cannot round-trip exactly; no rounding or underflow allowed")
        return parsed
    def limited_integer(value):
        if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
            raise QueryError(f"JSON integers exceed {MAX_JSON_INTEGER_DIGITS} digits")
        return int(value)
    try:
        return json_resources(json.loads(raw, parse_constant=reject_constant,
                                         parse_float=finite_float, parse_int=limited_integer))
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError, MemoryError, InvalidOperation) as exc:
        if isinstance(exc, QueryError):
            raise
        raise QueryError("invalid JSON or unsupported JSON parsing resources; no data truncated") from exc


def encoded(value):
    try:
        json_resources(value)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError, MemoryError, InvalidOperation) as exc:
        if isinstance(exc, QueryError):
            raise
        raise QueryError("response/query must be finite UTF-8 JSON within supported resources; no data truncated") from exc


def detached_json(value):
    """Copy every JSON container iteratively; leaves are immutable scalars."""
    json_resources(value)
    if not isinstance(value, (dict, list)):
        return value
    result = {} if isinstance(value, dict) else []
    stack = [(value, result)]
    while stack:
        source, target = stack.pop()
        children = source.items() if isinstance(source, dict) else enumerate(source)
        for key, child in children:
            if isinstance(child, (dict, list)):
                copied = {} if isinstance(child, dict) else []
                stack.append((child, copied))
            else:
                copied = child
            if isinstance(target, dict):
                target[key] = copied
            else:
                target.append(copied)
    return result


def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise QueryError("money must be a finite fixed decimal string")
    return Decimal(value)


def source_money(value):
    # Canonical normalized-money syntax; historical account/UNP strings never
    # go through this parser. Queries keep their separate exact Decimal syntax.
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)):
        raise QueryError("source money must use canonical fixed decimal string syntax or null")
    return None if value is None else decimal(value)


def bounded(value):
    if len(encoded(value).encode()) > MAX_OUTPUT_BYTES:
        raise QueryError("response exceeds 262144 bytes; use a smaller limit or a narrower query; no data truncated")
    return value


def object_value(value, label):
    if not isinstance(value, dict):
        raise QueryError(f"{label} must be an object")
    return value


def object_array(value, label):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise QueryError(f"{label} must be an array of objects")
    return value


def string_fields(value, fields, label, required=False):
    for field in fields:
        actual = value.get(field)
        if (required and (not isinstance(actual, str) or not actual)
                or actual is not None and not isinstance(actual, str)):
            raise QueryError(f"{label}.{field} must be {'a nonempty string' if required else 'a string or null'}")


def required_fields(value, fields, label):
    if any(field not in value for field in fields):
        raise QueryError(f"{label} requires {', '.join(fields)} (nullable fields may be null)")


def iso_dates(value, fields, label):
    string_fields(value, fields, label)
    for field in fields:
        actual = value.get(field)
        if actual is not None:
            try:
                if date.fromisoformat(actual).isoformat() != actual:
                    raise ValueError()
            except ValueError as exc:
                raise QueryError(f"{label}.{field} must be a real ISO YYYY-MM-DD or null") from exc


def declared_count(value, field, actual, label):
    if field in value and (type(value[field]) is not int or value[field] != actual):
        raise QueryError(f"{label}.{field} disagrees with graph count")


def financial_controls(statement, operations, control, accepted=False):
    """Compare declared controls with retained rows, without inventing facts.

    Mirrors the applicable exact comparisons in source_readiness.monetary_check
    and statement_graph.check_conversion. Missing/partial source facts remain
    readable; a passing source-check cannot use them as complete evidence.
    """
    fields = ("opening", "closing", "printed_debit_total", "printed_credit_total")
    for field in fields:
        if field in control:
            actual, printed = statement.get(field), control[field]
            if printed is not None:
                source_money(printed)
            if (actual is None) != (printed is None) or (
                    actual is not None and decimal(actual) != decimal(printed)):
                raise QueryError(f"control {field} disagrees with Statement")
    count = control.get("printed_operation_count")
    if count is not None:
        object_value(count, "printed_operation_count")
        required_fields(count, ("status", "value", "raw"), "printed_operation_count")
        if count["status"] not in ("observed", "absent", "unknown"):
            raise QueryError("invalid printed_operation_count status")
        raw, value = count["raw"], count["value"]
        if value is not None and (type(value) is not int or value < 0):
            raise QueryError("printed_operation_count value must be nonnegative integer or null")
        string_fields(count, ("raw",), "printed_operation_count")
        if count["status"] == "absent" and (raw is not None or value is not None):
            raise QueryError("contradictory absent printed_operation_count")
        if count["status"] == "observed":
            match = re.fullmatch(r"\s*(?:[^0-9\n]*:\s*)?([0-9]+)\s*", raw) if isinstance(raw, str) else None
            if match is None or value is None or (match[1].lstrip("0") or "0") != str(value) or value != len(operations):
                raise QueryError("observed printed_operation_count disagrees with raw/count")
    if "no_activity" in control and type(control["no_activity"]) is not bool:
        raise QueryError("control no_activity must be boolean")
    if control.get("no_activity"):
        if operations:
            raise QueryError("no_activity control has operations")
        if accepted and (not statement.get("archive_only") or any(
                control.get(f) is not None or statement.get(f) is not None for f in fields[2:])
                or statement.get("opening") is None or statement.get("closing") is None
                or decimal(statement["opening"]) != decimal(statement["closing"])):
            raise QueryError("accepted empty source must retain equal balances and unprinted turnovers")
        return
    complete = statement.get("rows_complete") is True
    money_known = all(o.get("amount") is not None and o.get("direction") != "unknown"
                      and ((decimal(o.get("printed_debit") or "0") > 0)
                           != (decimal(o.get("printed_credit") or "0") > 0)) for o in operations)
    if accepted and (not complete or not operations or not money_known or
                     statement.get("debit_is") == "unknown" or statement.get("opening_sign_rule") != "signed"
                     or any(control.get(f) is None for f in fields)
                     or any(o["row_status"] != "complete" for o in operations)):
        raise QueryError("accepted source financial controls require complete known rows/signs")
    if not complete or not money_known or not operations:
        return
    # Enough precision for all source coefficients, scale alignment and carry.
    with localcontext() as context:
        context.prec = 32 + sum(len(o.get(f) or "") for o in operations
                                for f in ("amount", "printed_debit", "printed_credit")) + sum(
                                    len(control.get(f) or "") for f in fields)
        debit = sum((decimal(o.get("printed_debit") or "0") for o in operations), Decimal(0))
        credit = sum((decimal(o.get("printed_credit") or "0") for o in operations), Decimal(0))
        income = sum((decimal(o["amount"]) for o in operations if o["direction"] == "inflow"), Decimal(0))
        expense = sum((decimal(o["amount"]) for o in operations if o["direction"] == "outflow"), Decimal(0))
        for field, total in (("printed_debit_total", debit), ("printed_credit_total", credit)):
            if control.get(field) is not None and total != decimal(control[field]):
                raise QueryError(f"control {field} disagrees with complete rows")
        if any(field in control for field in fields):
            for o in operations:
                d, c = decimal(o.get("printed_debit") or "0"), decimal(o.get("printed_credit") or "0")
                if decimal(o["amount"]) != d + c:
                    raise QueryError("controlled row amount disagrees with bank cells")
                if statement["debit_is"] != "unknown":
                    direction = ("outflow" if d > 0 else "inflow")
                    if statement["debit_is"] == "inflow":
                        direction = "inflow" if d > 0 else "outflow"
                    if o["direction"] != direction:
                        raise QueryError("controlled row direction disagrees with bank cells")
            if statement["debit_is"] != "unknown" and statement.get("opening_sign_rule") == "signed":
                if control.get("opening") is not None and control.get("closing") is not None:
                    if decimal(control["opening"]) + income - expense != decimal(control["closing"]):
                        raise QueryError("control balance equation disagrees with complete rows")
                expense_field, income_field = (("printed_debit_total", "printed_credit_total")
                                               if statement["debit_is"] == "outflow" else
                                               ("printed_credit_total", "printed_debit_total"))
                for field, total in ((expense_field, expense), (income_field, income)):
                    if control.get(field) is not None and total != decimal(control[field]):
                        raise QueryError("control direction totals disagree with complete rows")


def rko_annotation(value):
    """Validate the supplied OneCRko schema/conditional claim, not authenticity."""
    fields = ("scope", "database", "status", "document", "observed_at", "posted", "deletion_mark",
              "observed_on", "observed_precision", "snapshot_atomic")
    required_fields(value, fields, "Operation.one_c_rko")
    if value["scope"] != "cash_revenue_rko" or value["database"] != "test":
        raise QueryError("OneCRko scope/database must be cash_revenue_rko/test")
    iso_dates(value, ("observed_on",), "OneCRko")
    if value["observed_on"] is None:
        raise QueryError("OneCRko.observed_on must be a real ISO date")
    for field in ("posted", "deletion_mark"):
        if value[field] is not None and type(value[field]) is not bool:
            raise QueryError(f"OneCRko.{field} must be boolean or null")
    if type(value["snapshot_atomic"]) is not bool or value["observed_precision"] not in ("unknown_time", "exact_timestamp"):
        raise QueryError("OneCRko snapshot/observation precision is invalid")
    document = value["document"]
    if document is not None:
        object_value(document, "OneCRko.document")
        required_fields(document, ("date", "number", "type"), "OneCRko.document")
        iso_dates(document, ("date",), "OneCRko.document")
        if (document["date"] is None or document["type"] != "Расходный кассовый ордер"
                or not isinstance(document["number"], str)
                or not re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё-]{1,32}", document["number"])):
            raise QueryError("OneCRko.document must satisfy the canonical document contract")
    stamp = value["observed_at"]
    if value["observed_precision"] == "unknown_time":
        if stamp is not None:
            raise QueryError("OneCRko unknown_time requires null observed_at")
    else:
        try:
            if not isinstance(stamp, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})", stamp):
                raise ValueError()
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QueryError("OneCRko exact_timestamp requires a real zoned observed_at") from exc
    if value["status"] == "loaded_verified" and (document is None or value["posted"] is not True
            or value["deletion_mark"] is not False or value["observed_precision"] != "exact_timestamp"):
        raise QueryError("OneCRko loaded_verified requires document/posted/nondeleted/exact_timestamp")


def payment_text_claim(value):
    # The status drives a search filter; enforce its schema's conditional value.
    required_fields(value, ("profile", "status", "value", "diagnostics"), "Operation.payment1c_text")
    if value["profile"] != "xml10-single-line-escp-cash-v1":
        raise QueryError("payment1c_text profile is unsupported")
    if value["status"] == "held":
        if value["value"] is not None:
            raise QueryError("held payment1c_text requires null value")
    elif not isinstance(value["value"], str):
        raise QueryError("ready payment1c_text requires a string value; source-proven empty is allowed")
    diagnostics = object_array(value["diagnostics"], "payment1c_text diagnostics")
    codes = ("removed_escp_italic_4", "removed_escp_italic_5", "unknown_escape_sequence",
             "xml10_forbidden_character", "unsupported_control_character", "whitespace_collapsed",
             "purpose_boundary_unresolved", "empty_business_text")
    for entry in diagnostics:
        required_fields(entry, ("code", "index", "codepoint"), "payment1c_text diagnostic")
        if entry["code"] not in codes or type(entry["index"]) is not int or entry["index"] < 0:
            raise QueryError("payment1c_text diagnostic code/index is invalid")
        string_fields(entry, ("codepoint",), "payment1c_text diagnostic")

def graph_declarations(graph, item, artifacts):
    """Bind manifest/sidecar identity and counts before exposing declarations."""
    statements = {n["id"]: n["properties"] for n in graph["nodes"] if n["type"] == "Statement"}
    operations = [n for n in graph["nodes"] if n["type"] == "Operation"]
    contains = defaultdict(list)
    for edge in graph["edges"]:
        if edge["type"] == "CONTAINS":
            contains[edge["to"]].append(edge["from"])
    # Detect ownership contradictions before attaching any manifest provenance.
    for node in operations:
        owner = node["properties"]["statement_id"]
        if owner in statements:
            incoming = contains[node["id"]]
            if incoming != [owner]:
                raise QueryError("operation statement_id must agree with exactly one incoming CONTAINS edge")
    string_fields(item, ("statement_id",), "manifest input", required=True)
    sid = item["statement_id"]
    if set(statements) != {sid}:
        raise QueryError("manifest statement_id must name the graph's single Statement")
    p, ops = statements[sid], [n["properties"] for n in operations]
    ready = sum((o.get("processing") or {}).get("status") == "ready" for o in ops)
    for field, actual in (("operations", len(ops)), ("ready", ready), ("held", len(ops) - ready)):
        declared_count(item, field, actual, "manifest input")
    for field in ("period_start", "period_end"):
        if field in item:
            iso_dates(item, (field,), "manifest input")
            if item[field] != p.get(field):
                raise QueryError(f"manifest input {field} disagrees with Statement")
    check = artifacts.get("source_check", {})
    accepted = check.get("source_ready") is True or check.get("accepted") is True
    if type(check.get("source_ready")) is bool and type(check.get("accepted")) is bool and check["source_ready"] != check["accepted"]:
        raise QueryError("source-check accepted/source_ready flags contradict")
    controls = artifacts.get("controls", {}).get("statements", [])
    if accepted and {c["statement_id"] for c in controls} != {sid}:
        raise QueryError("accepted source-check requires matching controls")
    for control in controls:
        if accepted and not source_unit_review_valid(control.get("source_unit_review")):
            raise QueryError("accepted source-check requires valid no-conversion unit review")
        financial_controls(p, ops, control, accepted)
    if accepted:
        if check.get("errors") != []:
            raise QueryError("accepted source-check declares unresolved errors")
        records = object_array(check.get("statements"), "source_check statements")
        if len(records) != 1 or records[0].get("statement_id") != sid:
            raise QueryError("accepted source-check must cover its Statement exactly once")
        expected_math = "not_applicable" if not ops and p.get("archive_only") else "pass"
        if records[0].get("math_status") != expected_math:
            raise QueryError("accepted source-check math_status contradicts source rows")
    for label, data in artifacts.items():
        if type(data.get("source_ready")) is bool:
            for declared in (item.get("source_ready"), check.get("source_ready")):
                if type(declared) is bool and declared != data["source_ready"]:
                    raise QueryError(f"{label} source_ready declaration contradicts manifest/source-check")
        admission = data.get("processing_admission")
        status = admission.get("status") if isinstance(admission, dict) else admission
        if isinstance(item.get("processing_admission"), str) and isinstance(status, str) and status != item["processing_admission"]:
            raise QueryError(f"{label} processing_admission disagrees with manifest")
        if "source_acceptance_ref" in data and data["source_acceptance_ref"] != item.get("source_check"):
            raise QueryError(f"{label} source_acceptance_ref disagrees with manifest")
        if "statement_id" in data and data["statement_id"] != sid:
            raise QueryError(f"{label} statement_id disagrees with manifest/graph")
        for field, actual in (("operations", len(ops)), ("operation_count", len(ops)),
                              ("node_count", len(graph["nodes"])), ("edge_count", len(graph["edges"])),
                              ("partial_statements", int(not p.get("rows_complete")))):
            declared_count(data, field, actual, label)
        containers = [data]
        admission = data.get("processing_admission")
        if isinstance(admission, dict):
            containers.append(admission)
        for container in containers:
            if "statements" not in container:
                continue
            records = object_array(container["statements"], f"{label} statements")
            seen = set()
            for record in records:
                string_fields(record, ("statement_id",), label, required=True)
                if record["statement_id"] != sid or record["statement_id"] in seen:
                    raise QueryError(f"{label} statements must have unique graph-local identities")
                seen.add(record["statement_id"])
                for field, actual in (("operations", len(ops)), ("operation_count", len(ops)),
                                      ("graph_operation_count", len(ops)), ("ready_count", ready)):
                    declared_count(record, field, actual, label)
                for field, column, direction in (("computed_debit", "printed_debit", None),
                                                 ("computed_credit", "printed_credit", None),
                                                 ("computed_expense", "amount", "outflow"),
                                                 ("computed_income", "amount", "inflow")):
                    if record.get(field) is not None:
                        declared = decimal(record[field])
                        if direction is not None and any(o.get("amount") is None or o["direction"] == "unknown" for o in ops):
                            raise QueryError(f"{label} {field} claims a sum over unknown money")
                        with localcontext() as context:
                            context.prec = 32 + len(record[field]) + sum(len(o.get(column) or "") for o in ops)
                            total = sum((decimal(o.get(column) or "0") for o in ops
                                         if direction is None or o["direction"] == direction), Decimal(0))
                            if declared != total:
                                raise QueryError(f"{label} {field} disagrees with graph rows")
                if accepted and label == "source_check":
                    differences = object_value(record.get("differences"), "source-check differences")
                    if any(decimal(v) != 0 for v in differences.values()):
                        raise QueryError("accepted source-check declares nonzero financial differences")
                if "rows" in record:
                    rows = object_array(record["rows"], f"{label} rows")
                    ids = [row.get("operation_id") for row in rows]
                    if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) != {n["id"] for n in operations}:
                        raise QueryError(f"{label} rows must cover graph Operations exactly once")

CANONICAL_MODULES = ("payment_text", "statement_graph", "bank_identifiers",
                     "archive_adapter", "modern_adapter", "source_readiness", "counterparty_identity")


def _load_source_modules(scripts, names):
    """Execute exact local files with imports confined to these module objects.

    Accepted sources use absolute imports, including imports inside predicates.
    A module-local builtins mapping redirects only the named source modules;
    ordinary Python/optional dependency imports keep their normal behavior.
    No module is published in sys.modules and no global importer/path is changed.
    """
    modules = {}
    namespace = "_bank_graph_sources_" + hashlib.sha256(str(scripts).encode()).hexdigest()
    ordinary_import = builtins.__import__

    def source_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in names:
            return load(name)
        return ordinary_import(name, globals, locals, fromlist, level)

    def load(name):
        if name in modules:
            return modules[name]
        path = scripts / (name + ".py")
        spec = importlib.util.spec_from_file_location(namespace + "." + name, path)
        if spec is None or spec.loader is None:
            raise ImportError("canonical source module is unavailable")
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__builtins__"] = dict(vars(builtins), __import__=source_import)
        modules[name] = module
        try:
            # get_source/compile execute unchanged predicates without writing
            # bytecode into the read-only canonical source directory.
            source = spec.loader.get_source(spec.name)
            if source is None:
                raise ImportError("canonical source module is unavailable")
            exec(compile(source, str(path), "exec"), module.__dict__)
        except BaseException:
            modules.pop(name, None)
            raise
        return module

    for name in names:
        load(name)
    return modules


@lru_cache(maxsize=1)
def _canonical_modules():
    scripts = Path(__file__).resolve().parent / "scripts"
    # Publish the cache only after the entire local module group loaded.
    return _load_source_modules(scripts, CANONICAL_MODULES)


@contextmanager
def canonical_sources():
    """Use this checkout's pinned pure source predicates, never host modules.

    Private qualified module objects retain isolated imports for lazy predicates
    and repeated Reader instances. Schema/PDF/CLI entry points are not called by
    the reader; pure predicates need no jsonschema or extraction dependency.
    """
    try:
        modules = _canonical_modules()
        yield tuple(modules[name] for name in
                    ("statement_graph", "archive_adapter", "modern_adapter", "source_readiness"))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ImportError,
            OSError, ArithmeticError, RecursionError) as exc:
        if isinstance(exc, QueryError):
            raise
        raise QueryError("invalid or unavailable canonical source contract") from exc


def source_unit_review_valid(unit):
    with canonical_sources() as (facts, _, _, _):
        return facts.source_unit_review_valid(unit)


@lru_cache(maxsize=1)
def source_schema():
    with canonical_sources() as (facts, _, _, _):
        return strict_json((facts.PACKAGE / "references/graph.schema.json").read_text())


def supplied_source_fields(value, schema, label):
    """Check supplied facts against the accepted schema, without filling gaps.

    This is a supplied-value check, not full JSON Schema validation: legacy
    missing fields and opaque extensions remain readable diagnostics. Required
    fields and conditional semantic claims consumed by the reader are checked
    separately. No schema runtime dependency or source mutation is introduced.
    """
    if "$ref" in schema:
        schema = source_schema()["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    def invalid():
        raise QueryError(f"{label} contradicts the supplied-value source schema")
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        matches = {"null": value is None, "string": isinstance(value, str),
                   "boolean": type(value) is bool, "integer": type(value) is int,
                   "object": isinstance(value, dict), "array": isinstance(value, list)}
        if not any(matches.get(t, False) for t in types):
            invalid()
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        invalid()
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        invalid()
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or "pattern" in schema and not re.search(schema["pattern"], value):
            invalid()
        if schema.get("format") == "date":
            iso_dates({"value": value}, ("value",), label)
    if type(value) is int and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        invalid()
    for choice in ("anyOf", "oneOf"):
        if choice in schema:
            successes = 0
            for branch in schema[choice]:
                try:
                    supplied_source_fields(value, branch, label)
                    successes += 1
                except QueryError:
                    pass
            if not successes or choice == "oneOf" and successes != 1:
                invalid()
    for branch in schema.get("allOf", []):
        supplied_source_fields(value, branch, label)
    if isinstance(value, dict):
        for field, field_schema in schema.get("properties", {}).items():
            if field in value:
                supplied_source_fields(value[field], field_schema, label + "." + field)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            supplied_source_fields(item, schema["items"], label + "[]")


def supplied_node_fields(node):
    definition = source_schema()["$defs"][node["type"]]["properties"]
    # Existing explicit legacy null diagnostics do not claim a known identity,
    # ready text, settlement or target state. A null currency reference is already
    # checked against the absence of IN_CURRENCY before semantic admission.
    nullable_diagnostics = {"counterparty", "processing", "payment1c_text", "settlement",
                            "one_c_rko", "counterparty_clarification", "owner_counterparty_identity", "owner_role_unps", "payment1c_comment_raw", "source_text"}
    for field in ("observations", "unrecognized_fields"):
        if field in node:
            supplied_source_fields(node[field], definition[field], node["type"] + "." + field)
    properties = definition["properties"]["properties"]
    for field, value in node["properties"].items():
        legacy_null = value is None and (node["type"] == "Operation" and field in nullable_diagnostics
            or node["type"] == "Statement" and field == "currency_context_id")
        if field in properties and not legacy_null:
            supplied_source_fields(value, properties[field], node["type"] + "." + field)


def semantic_source(graph, artifacts, item=None):
    """Admit supplied source claims once, before any public index/envelope.

    Pure canonical predicates own text, movement knownness, admission and units.
    Inline conditions mirror validate's consumed source assertions; missing/null
    legacy downstream objects remain diagnostic unknowns. This does not call
    the full schema/PDF validator or authenticate historical external receipts.
    """
    with canonical_sources() as (facts, adapter, modern, readiness):
        nodes = {n["id"]: n for n in graph["nodes"]}
        outgoing = defaultdict(list)
        for edge in graph["edges"]:
            outgoing[edge["from"]].append(edge)
        errors = []
        def reject(message, node):
            errors.append(message + ": " + node["id"])
        modern_support = {st["id"]: modern.format_supported(st, [o for o in graph["nodes"]
            if o["type"] == "Operation" and o["properties"]["statement_id"] == st["id"]])
            for st in graph["nodes"] if st["type"] == "Statement"
            and any(o["field"] == "modern_anatomy_profile" for o in st.get("observations", []))}
        for n in graph["nodes"]:
            p = n["properties"]
            if n["type"] in ("Account", "Bank") and "identifier" in p:
                # Reuse the accepted recognizer for every own/counterparty route.
                # Recognition/checksum never supplies existence or registry proof.
                bi = modern.bi
                ident = object_value(p["identifier"], n["type"] + ".identifier")
                kind = "account" if n["type"] == "Account" else "bank"
                if ident != bi.recognize(ident.get("raw"), kind):
                    reject("bank identifier recognition mismatch", n)
                if kind == "account" and p.get("format") != (
                        "iban" if ident.get("format") == "iban" else
                        "historical" if ident.get("format") == "historical_numeric" else "unknown"):
                    reject("bank account format mismatch", n)
                value = p.get("number_raw" if kind == "account" else "code_raw")
                retained_unknown_bank_raw = (kind == "bank" and not bi.usable(ident)
                    and p.get("identity_status") == "unknown" and value == ident["raw"]
                    and any(o["field"] == "code_raw" and o["raw"] == value
                            and o["normalized"] is None and o["status"] == "unknown"
                            for o in n.get("observations", [])))
                if value is not None and not retained_unknown_bank_raw and (
                        ident["format_status"] != "recognized" or value != ident["normalized"]):
                    reject("bank identifier value mismatch", n)
                if not bi.usable(ident) and p.get("identity_status") == "observed":
                    reject("unusable bank identifier claimed observed", n)
            if n["type"] == "Bank" and p.get("code_system_raw") == "Код":
                for observation in n.get("observations", []):
                    if observation["field"] == "code_raw" and observation["status"] == "observed":
                        recognized = modern.bi.recognize(observation["raw"], "bank")
                        if (not modern.bi.usable(recognized) or observation["normalized"] != recognized["normalized"]
                                or p.get("code_raw") != recognized["normalized"]):
                            reject("unproven observed bank code", n)
            if n["type"] == "DuplicateDecision":
                members = p.get("member_ids", [])
                eligible = {"Statement", "Operation"}
                if any(nodes.get(member, {}).get("type") not in eligible for member in members):
                    reject("missing or mistyped duplicate member reference", n)
                linked = [edge["to"] for edge in outgoing[n["id"]] if edge["type"] == "DUPLICATE_MEMBER"]
                if Counter(members) != Counter(linked):
                    reject("duplicate member_ids disagree with DUPLICATE_MEMBER targets", n)
                if p.get("status") == "confirmed":
                    canonical = p.get("canonical_id")
                    if not canonical or not p.get("evidence_refs"):
                        reject("confirmed duplicate needs canonical/evidence", n)
                    elif canonical not in members or nodes.get(canonical, {}).get("type") not in eligible:
                        reject("confirmed duplicate canonical_id must resolve to a local member", n)
            if n["type"] == "Statement":
                account = nodes.get(p["account_id"])
                if "account_number_raw" in p and account and p["account_number_raw"] != account["properties"].get("number_raw"):
                    reject("statement/account number disagreement", n)
                if p["selection"] == "exclude_reissue" and not p.get("selection_evidence"):
                    reject("reissue exclusion requires evidence", n)
                if p.get("opening_origin") == "first_statement_blank_zero":
                    blank = any(o["field"] == "opening" and isinstance(o["raw"], str) and not o["raw"].strip() for o in n.get("observations", []))
                    if not blank or not p.get("first_account_evidence") or p["opening"] is None or decimal(p["opening"]) != 0:
                        reject("unproven first-statement blank opening rule", n)
            if n["type"] == "CurrencyContext" and p.get("semantics_status") == "verified" and (not p.get("epoch") or not p.get("evidence_refs") or not p.get("printed_currency") or not p.get("unit_raw")):
                reject("verified currency context requires epoch/evidence", n)
            if n["type"] != "Operation":
                continue
            st = nodes.get(p["statement_id"])
            if st is None or st["type"] != "Statement":
                raise QueryError("operation statement_id is unresolved")
            if p.get("payment1c_text") is not None and p["payment1c_text"] != facts.prepare_payment_text(p["purpose_raw"], p.get("purpose_status", "unknown")):
                reject("payment text differs from deterministic full source transformation", n)
            if p.get("payment1c_comment_raw") is not None and p["payment1c_comment_raw"] != p["purpose_raw"]:
                reject("payment comment differs from full source purpose", n)

            modern_diagnostic=any(o['field']=='diagnostic_anatomy_profile' and o['normalized']=='belarusbank-landscape-diagnostic-v1' for o in n.get('observations',[]))
            if modern_diagnostic:
                diagnostic_purpose=[o for o in n['observations'] if o['field']=='diagnostic_purpose_raw']
                details=[o for o in n['observations'] if o['field']=='identity_detail_raw']
                if (p.get('purpose_status')!='unknown' or p['row_status']!='partial' or p.get('classification') is not None or p.get('classification_status')!='unknown' or p.get('settlement') is not None or p.get('counterparty',{}).get('status')!='unknown' or
                    len(diagnostic_purpose)!=1 or diagnostic_purpose[0]['raw']!=p['purpose_raw'] or len(details)!=1 or
                    not isinstance(details[0]['raw'],str) or
                    not re.sub(r'\s','',details[0]['raw']).endswith(re.sub(r'\s','',p['purpose_raw'])) or
                    any(e['type']=='HAS_COMPONENT' for e in outgoing[n['id']]) or
                    not st or not any(o['field']=='diagnostic_anatomy_profile' and o['normalized']=='belarusbank-landscape-diagnostic-v1' for o in st['observations'])):
                    errors.append('modern diagnostic scope/purpose mismatch: '+n['id'])
            if 'purpose_status' in p and not modern_diagnostic and 'modern_fields' not in p:
                # repository-pinned adapter
                details = [o for o in n['observations'] if o['field'] == 'identity_detail_raw']
                if len(details) == 1 and isinstance(details[0]['raw'], str):
                    lines = details[0]['raw'].split('\n')
                    _, _, purpose, _ = adapter.metadata_fields(lines, [{} for _ in lines])
                    if p['purpose_raw'] != '\n'.join(purpose):
                        errors.append('purpose differs from retained source detail: '+n['id'])
                else:
                    errors.append('unique retained source detail required for purpose: '+n['id'])
            if 'modern_fields' in p:
                # repository-pinned modern adapter
                sf=modern.source_fields(n);mf=p['modern_fields']
                if not st or not modern_support.get(st['id'],False):errors.append('modern source profile proof missing: '+n['id'])
                side=[o for o in n['observations'] if o['field']=='modern_sidecar_raw']
                if (len(side)!=1 or mf['sidecar_raw']!=side[0]['raw'] or
                    any(mf[k]!=sf.get(k) for k in ('name_raw','bank_name_raw','unp_raw','unlabelled_codes_raw')) or
                    p['purpose_raw']!=sf.get('purpose_raw') or p['priority_raw']!=sf.get('priority_raw')):
                    errors.append('modern fields differ from retained source: '+n['id'])
                for key in ('transaction_date','document_number_raw','operation_code_raw'):
                    os=[o for o in n['observations'] if o['field']==key]
                    # repository-pinned adapter
                    expected_date=modern.date_value(os[0]['raw']) if len(os)==1 and key=='transaction_date' else None
                    if len(os)!=1 or os[0]['normalized']!=p[key] or (p[key]!=expected_date if key=='transaction_date' else os[0]['raw']!=p[key]):
                        errors.append('modern bank row field source mismatch: '+n['id'])
                account_edges=[e for e in outgoing[n['id']] if e['type'] in ('FROM_ACCOUNT','TO_ACCOUNT')]
                if len(account_edges)==1:
                    account=nodes.get(account_edges[0]['to'],{})
                    bank_edges=[e for e in outgoing[account.get('id')] if e['type']=='SERVICED_BY']
                    bank=nodes.get(bank_edges[0]['to'],{}) if len(bank_edges)==1 else {}
                    for field,node in [('modern_account_raw',account),('modern_bank_code_raw',bank)]:
                        os=[o for o in n['observations'] if o['field']==field]
                        if len(os)!=1 or node.get('properties',{}).get('identifier',{}).get('raw')!=os[0]['raw']:
                            errors.append('modern linked identifier differs from source field: '+n['id'])
                    if bank.get('properties',{}).get('name_raw')!=sf.get('bank_name_raw'):
                        errors.append('modern bank name differs from source field: '+n['id'])
                elif p['direction']!='unknown':errors.append('modern bank account link missing or ambiguous: '+n['id'])
                md=sf.get('metadata',{})
                if p['document_date']!=md.get('document_date') or mf['urgency_raw']!=md.get('urgency_raw') or mf['acceptance_raw']!=md.get('acceptance_raw'):
                    errors.append('modern date urgency acceptance source mismatch: '+n['id'])
            d = decimal(p['printed_debit'] or '0')
            cr = decimal(p['printed_credit'] or '0')
            cells_known = all(facts.bank_cell_known(n, f) for f in ('printed_debit', 'printed_credit'))
            bank_known = cells_known and ((d > 0 and cr == 0) or (cr > 0 and d == 0))
            semantics = p.get('bank_debit_is', st['properties']['debit_is'] if st else 'unknown')
            if semantics not in ("outflow", "inflow", "unknown"):
                raise QueryError("Operation.bank_debit_is must be outflow/inflow/unknown")
            if bank_known and semantics != 'unknown' or p['amount'] is not None or p['direction'] != 'unknown':
                expected = ('outflow' if d > 0 else 'inflow')
                if semantics == 'inflow':
                    expected = 'inflow' if d > 0 else 'outflow'
                bank_amount = d if d > 0 else cr
                if (not bank_known or semantics == 'unknown' or p['amount'] is None or
                        decimal(p['amount']) != bank_amount or p['direction'] != expected):
                    errors.append('Operation.amount disagrees with same-row printed bank cells; Operation.direction disagrees with same-row printed bank cells or source knownness: '+n['id'])
            elif p['row_status'] == 'complete':
                errors.append('complete operation has unknown bank movement: '+n['id'])
            currency_node = nodes.get(st['properties']['currency_context_id']) if st else None
            currency = currency_node['properties']['printed_currency'] if currency_node else None
            settlement = p.get('settlement')
            if settlement and settlement['status'] in ('verified_equation', 'verified_withheld_fee') and p.get('purpose_status') != 'observed':
                errors.append('verified settlement requires observed full source purpose: '+n['id'])
            if settlement or ('purpose_status' in p and not modern_diagnostic):
                # Reproduce the supported fields from the full source purpose
                # and independently validated bank movement. Reviewed JSON
                # cannot substitute a description amount or assert a fee.
                # repository-pinned adapter
                standalone = p.get('purpose_status') == 'observed' and adapter.cash_acceptance_fee(p['purpose_raw'], p['direction'])
                components = (adapter.fee_basis(p['purpose_raw']) if standalone else
                              adapter.amount_components(p['purpose_raw'], p['direction'], p['amount'], currency))
                if p.get('purpose_status') == 'unknown':
                    components = [(*c[:4], 'unknown') for c in components]
            if settlement:
                expected_settlement = adapter.settlement_view(components, p['direction'], p['amount'], standalone)
                if settlement != expected_settlement:
                    errors.append('unproven source settlement fields: '+n['id'])
            if 'purpose_status' in p and not modern_diagnostic:
                # repository-pinned adapter
                if (p.get('classification') != ('bank_fee' if standalone else None) or
                        p.get('classification_status') != ('inferred' if standalone else 'unknown')):
                    errors.append('classification differs from source purpose: '+n['id'])
                expected_components = components
                expected_props = []
                for offset, kind, raw, mention, inclusion in expected_components:
                    unit, currency = adapter.component_unit(p['purpose_raw'], offset, mention)
                    expected_props.append({'kind':kind, 'value_raw':raw, 'value':adapter.component_money(raw),
                                           'unit_raw':unit, 'currency_raw':currency, 'inclusion':inclusion})
                component_links = [e for e in outgoing[n['id']] if e['type']=='HAS_COMPONENT']
                actual = [nodes.get(e['to'], {}).get('properties') for e in component_links]
                # Compare whole source assertions, not a selected settlement only.
                import collections
                key = encoded
                if collections.Counter(map(key, actual)) != collections.Counter(map(key, expected_props)):
                    errors.append('amount components differ from full source purpose: '+n['id'])
            if p.get('purpose_status') == 'unknown' and p.get('classification') == 'bank_fee':
                errors.append('fee classification has unresolved purpose boundary: '+n['id'])
            if p.get('one_c_rko') is not None and p['one_c_rko']['observed_at'] is not None:
                try:
                    timestamp = datetime.fromisoformat(p['one_c_rko']['observed_at'].replace('Z', '+00:00'))
                    if timestamp.tzinfo is None or timestamp.date().isoformat() != p['one_c_rko']['observed_on']:raise ValueError('matching observation date/timezone required')
                except ValueError:
                    errors.append('RKO observation requires exact timezone timestamp: '+n['id'])
            if st and p['transaction_date'] and st['properties']['period_start'] and st['properties']['period_end']:
                if not st['properties']['period_start'] <= p['transaction_date'] <= st['properties']['period_end']:
                    errors.append('operation date outside period: '+n['id'])
        admission = facts.identity_admission(graph)
        for state in admission["statements"]:
            for row in state["rows"]:
                p = nodes[row["operation_id"]]["properties"]
                cp = p.get("counterparty")
                if cp and cp["status"] == "observed" and any(g not in facts.NON_IDENTITY_GAPS for g in row["original_identity_gaps"]):
                    errors.append("unproven counterparty identity: " + row["operation_id"])
                if "owner_clarification_missing_or_conflicting_provenance" in row["gaps"]:
                    errors.append("invalid owner clarification: " + row["operation_id"])
                if "owner_official_role_identity_missing_or_conflicting_provenance" in row["gaps"]:
                    errors.append("invalid owner official role identity: " + row["operation_id"])
                if "owner_internal_identity_missing_or_conflicting_provenance" in row["gaps"]:
                    errors.append("invalid owner internal identity: " + row["operation_id"])
                if "owner_type_missing_or_conflicting_provenance" in row["gaps"]:
                    errors.append("invalid owner person type: " + row["operation_id"])
                processing = p.get("processing")
                if processing is not None and processing != {k: row[k] for k in ("status", "gaps", "questions", "effective_counterparty")}:
                    errors.append("stale or false row processing admission: " + row["operation_id"])
                if processing is None and state["status"] != "blocked":
                    errors.append("missing explicit per-row processing admission: " + row["operation_id"])
        def admission_claim(claim, expected):
            # Only retained known assertions are compared; absent/null/unknown
            # diagnostics and opaque extension fields do not invent readiness.
            if claim is None or claim in ("unknown", "not_evaluated"):
                return
            if isinstance(claim, dict) and isinstance(expected, dict):
                for key in claim.keys() & expected.keys():
                    admission_claim(claim[key], expected[key])
            elif isinstance(claim, list) and isinstance(expected, list):
                if all(isinstance(v, dict) for v in claim + expected) and expected:
                    key = "statement_id" if "statement_id" in expected[0] else "operation_id"
                    actual = {v.get(key): v for v in claim}
                    wanted = {v.get(key): v for v in expected}
                    if len(actual) != len(claim) or actual.keys() != wanted.keys():
                        raise QueryError("supplied processing admission identities disagree with graph")
                    for ident in actual:
                        admission_claim(actual[ident], wanted[ident])
                elif claim != expected:
                    raise QueryError("supplied processing admission differs from canonical graph admission")
            elif type(claim) is not type(expected) or claim != expected:
                raise QueryError("supplied processing admission differs from canonical graph admission")
        if item and isinstance(item.get("processing_admission"), str):
            admission_claim(item["processing_admission"], admission["status"])
        for data in artifacts.values():
            claim = data.get("processing_admission")
            if isinstance(claim, (str, dict)):
                admission_claim(claim, admission if isinstance(claim, dict) else admission["status"])
            if data.get("profile") == readiness.PROFILE and data.get("future_processing_ready") is True:
                raise QueryError("source-only review cannot grant future processing readiness")
        accepted = any(artifacts.get("source_check", {}).get(k) is True for k in ("accepted", "source_ready"))
        controls = artifacts.get("controls", {})
        verified_units = set()
        for control in controls.get("statements", []):
            unit = control.get("source_unit_review")
            statement = nodes[control["statement_id"]]
            if readiness.source_unit_matches_statement(graph, statement, unit):
                verified_units.add(statement["id"])
            elif accepted:
                errors.append("source unit/period/currency context mismatch: " + statement["id"])
        if accepted:
            money_errors, _ = readiness.monetary_check(graph, controls)
            errors.extend(money_errors)
            review = artifacts.get("source_review")
            if review is not None:
                errors.extend(readiness.review_matches(graph, review))
                if readiness.control_fingerprint(controls) != review.get("controls_fingerprint"):
                    errors.append("independent source controls reconciliation mismatch")
        if errors:
            raise QueryError("; ".join(errors))
        for node in graph["nodes"]:
            supplied_node_fields(node)
        return verified_units



def incoming_candidate(p, debit_is):
    """Only a bank-cell gate. This does not classify revenue or grant writes."""
    if p.get("direction") != "inflow" or p.get("bank_debit_is", debit_is) != "outflow":
        return False
    credit, debit, amount = p.get("printed_credit"), p.get("printed_debit"), p.get("amount")
    return (credit is not None and amount is not None and decimal(credit) > 0
            and (debit is None or decimal(debit) == 0) and decimal(amount) == decimal(credit))


class Reader:
    """Immutable content snapshot with compact search index and one-graph cache.

    The caller supplies an archive directory at construction. Tool queries cannot
    supply files, URLs or arbitrary projections. Recreate to observe new content.
    """

    def __init__(self, archive):
        self.archive = Path(archive).resolve()
        self.files = {}
        self.node_files = {}
        self.node_types = {}
        self.statements = {}
        self.currencies = {}
        self.operations = []
        self.adjacency = defaultdict(list)
        self.units = {}
        self.verified_units = set()
        self.inputs = {}
        self._cache = None
        self._cache_path = None
        self.manifest = self._read("manifest.json")
        inputs = self.manifest.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise QueryError("manifest inputs must be a nonempty array")
        node_counts, edge_counts = Counter(), Counter()
        edge_ids = set()
        pending_semantics = []
        internal_assignments = []
        official_role_assignments = []
        for item in inputs:
            object_value(item, "manifest input")
            graph_name = item.get("graph")
            self._path(graph_name)
            if graph_name in self.inputs:
                raise QueryError("duplicate graph in manifest")
            self.inputs[graph_name] = item
            graph = self._read(graph_name)
            if type(graph.get("schema_version")) is not int or graph["schema_version"] != 1:
                raise QueryError("unsupported graph schema_version")
            nodes = object_array(graph.get("nodes"), "graph nodes")
            edges = object_array(graph.get("edges"), "graph edges")
            # Bind every manifest-selected acceptance/control artifact, without
            # interpreting a historical check as new source authentication.
            controls = None
            artifacts = {}
            for key in ("controls", "structure_check", "conversion_check", "source_review", "source_check"):
                if key in item and item[key] is not None:
                    data = self._read(item[key])
                    artifacts[key] = data
                    if key == "controls":
                        controls = data
            local_types = {}
            local_nodes = {n["id"]:n for n in nodes if isinstance(n.get("id"), str)}
            related_counts = Counter()
            for edge in edges:
                for endpoint in (edge.get("from"), edge.get("to")):
                    if not isinstance(endpoint, str):
                        raise QueryError("edge endpoints must be strings")
                related_counts.update(set((edge["from"], edge["to"])))
            for node in nodes:
                string_fields(node, ("id", "type"), "node", required=True)
                p = object_value(node.get("properties"), "node properties")
                nid, kind = node["id"], node["type"]
                known_types = set().union(*(ends[0] | ends[1] for ends in EDGE_ENDPOINTS.values()))
                if kind not in known_types:
                    raise QueryError("unknown graph node type")
                if nid in self.node_files:
                    raise QueryError("colliding node ID; no implicit merge")
                # Check the exact get envelope before admitting a usable ID.
                # The snapshot is always a 64-character hexadecimal digest.
                bounded(self._get_payload(node, graph_name, related_counts[nid], "0" * 64))
                self.node_files[nid] = graph_name
                self.node_types[nid] = local_types[nid] = kind
                node_counts[kind] += 1
                if kind == "Statement":
                    string_fields(p, ("account_id",), "Statement", required=True)
                    string_fields(p, ("currency_context_id",), "Statement")
                    source_enums(p, "Statement")
                    required_fields(p, ("period_start", "period_end", "year", "rows_complete", "expected_operations", "opening", "closing", "printed_debit_total", "printed_credit_total"), "Statement")
                    iso_dates(p, ("period_start", "period_end", "statement_date", "previous_operation_date"), "Statement")
                    if p["period_start"] and p["period_end"] and p["period_start"] > p["period_end"]:
                        raise QueryError("reversed Statement period")
                    if p["year"] is not None and (type(p["year"]) is not int or not 1900 <= p["year"] <= 9999):
                        raise QueryError("Statement.year must be an integer 1900..9999 or null")
                    if p["year"] is not None and p["period_start"] and p["year"] != int(p["period_start"][:4]):
                        raise QueryError("Statement year/period mismatch")
                    if type(p["rows_complete"]) is not bool:
                        raise QueryError("Statement.rows_complete must be boolean")
                    if p["expected_operations"] is not None and (type(p["expected_operations"]) is not int or p["expected_operations"] < 0):
                        raise QueryError("Statement.expected_operations must be nonnegative integer or null")
                    for field in ("opening", "closing", "printed_debit_total", "printed_credit_total"):
                        if p.get(field) is not None:
                            source_money(p[field])
                    self.statements[nid] = node["properties"]
                elif kind == "CurrencyContext":
                    required_fields(p, ("printed_currency",), "CurrencyContext")
                    string_fields(p, ("printed_currency",), "CurrencyContext")
                    self.currencies[nid] = node["properties"]
                elif kind == "Operation":
                    p = node["properties"]
                    source_enums(p, "Operation")
                    required_fields(p, ("transaction_date", "document_number_raw", "amount", "printed_debit", "printed_credit", "purpose_raw"), "Operation")
                    if not isinstance(p["purpose_raw"], str):
                        raise QueryError("Operation.purpose_raw must be a string")
                    iso_dates(p, ("transaction_date", "booking_date", "value_date", "document_date"), "Operation")
                    for field in ("amount", "printed_debit", "printed_credit"):
                        if p.get(field) is not None and source_money(p[field]) < 0:
                            raise QueryError(f"Operation.{field} must be nonnegative")
                    fields = ("statement_id", "ordinal", "transaction_date", "document_number_raw",
                              "direction", "amount", "printed_debit", "printed_credit", "purpose_raw",
                              "row_status", "duplicate_status", "classification_status", "bank_debit_is")
                    string_fields(p, ("statement_id",), "Operation", required=True)
                    string_fields(p, tuple(k for k in fields if k != "ordinal"), "Operation")
                    if type(p.get("ordinal")) is not int or p["ordinal"] < 1:
                        raise QueryError("Operation.ordinal must be a positive integer")
                    indexed = {"id": nid, **{k: p.get(k) for k in fields if k != "bank_debit_is" or k in p}}
                    for obj, fields in {"processing": ("status",), "payment1c_text": ("status",),
                                        "settlement": ("status",), "one_c_rko": ("status",),
                                        "counterparty": ("party_id", "name_raw", "unp_raw", "role")}.items():
                        nested = p.get(obj)
                        if nested is not None:
                            source_enums(object_value(nested, f"Operation.{obj}"), f"Operation.{obj}")
                            if obj == "one_c_rko":
                                rko_annotation(nested)
                            elif obj == "payment1c_text":
                                payment_text_claim(nested)
                        nested = {} if nested is None else object_value(nested, f"Operation.{obj}")
                        string_fields(nested, fields, f"Operation.{obj}")
                        if obj == "counterparty" and p.get(obj) is not None:
                            if any(field not in nested for field in ("party_id", "name_raw", "unp_raw")):
                                raise QueryError("counterparty party_id/name_raw/unp_raw fields are required (null allowed)")
                            if nested["status"] == "observed" and (
                                    nested["role"] not in {"payer", "payee"}
                                    or not nested["party_id"] or not nested["name_raw"]
                                    or not re.fullmatch(r"(?!000000000$)(?:[0-9]{9}|[A-Z]{2}[0-9]{7})", nested["unp_raw"] or "")):
                                raise QueryError("observed counterparty must satisfy the canonical identity contract")
                        indexed[obj] = {k: nested.get(k) for k in fields}
                    effective = (p.get("processing") or {}).get("effective_counterparty") or {}
                    indexed["effective_counterparty"] = detached_json(effective)
                    party = local_nodes.get((p.get("counterparty") or {}).get("party_id"))
                    indexed["observed_counterparty"] = {}
                    if party is not None:
                        for field in ("name_raw", "unp_raw"):
                            observations = [o for o in party.get("observations", []) if o.get("field") == field]
                            indexed["observed_counterparty"][field] = observations[0].get("raw") if len(observations) == 1 else None
                    indexed["effective_parties"] = _canonical_modules()["counterparty_identity"].effective_parties(node)
                    values = p.get("owner_role_unps", [])
                    if not isinstance(values, list):
                        raise QueryError("official role annotations must be an array")
                    official_role_assignments.extend(values)
                    annotation = p.get("owner_counterparty_identity")
                    if annotation is not None:
                        internal_assignments.append(annotation)
                    self.operations.append(indexed)
            for control in object_array((controls or {}).get("statements", []), "control statements"):
                string_fields(control, ("statement_id",), "control", required=True)
                sid = control["statement_id"]
                if local_types.get(sid) != "Statement":
                    raise QueryError("control statement_id must name a Statement in its graph")
                if sid in self.units:
                    raise QueryError("duplicate statement controls")
                review = control.get("source_unit_review")
                self.units[sid] = {} if review is None else object_value(review, "source_unit_review")
                object_value(self.units[sid], "source_unit_review")
                string_fields(self.units[sid], ("status", "source_unit", "source_epoch"), "source_unit_review")
            for edge in edges:
                string_fields(edge, ("id", "type", "from", "to"), "edge", required=True)
                if edge["id"] in edge_ids:
                    raise QueryError("colliding edge ID")
                edge_ids.add(edge["id"])
                ends = EDGE_ENDPOINTS.get(edge["type"])
                if (not ends or local_types.get(edge["from"]) not in ends[0]
                        or local_types.get(edge["to"]) not in ends[1]):
                    raise QueryError("invalid typed edge endpoint")
                edge_counts[edge["type"]] += 1
                self.adjacency[edge["from"]].append(edge)
                if edge["to"] != edge["from"]:
                    self.adjacency[edge["to"]].append(edge)
            graph_declarations(graph, item, artifacts)
            pending_semantics.append((graph_name, artifacts, item))
        with canonical_sources():
            if _canonical_modules()["counterparty_identity"].role_collisions(official_role_assignments):
                raise QueryError("conflicting official Party/role assignments across archive")
            if _canonical_modules()["counterparty_identity"].collisions(internal_assignments):
                raise QueryError("conflicting internal counterparty assignments across archive")
        for op in self.operations:
            if op.get("statement_id") not in self.statements:
                raise QueryError("operation statement_id is unresolved")
            sid = op["statement_id"]
            if self.node_files[sid] != self.node_files[op["id"]]:
                raise QueryError("operation belongs to another graph")
            linked = [edge["from"] for edge in self.adjacency[op["id"]]
                      if edge["type"] == "CONTAINS" and edge["to"] == op["id"]]
            if linked != [sid]:
                raise QueryError("operation statement_id must agree with exactly one incoming CONTAINS edge")
            if self.statements[sid].get("currency_context_id") is None:
                raise QueryError("statement currency_context_id is unresolved")
            counterparty = op["counterparty"]
            party_id = counterparty.get("party_id")
            if party_id is not None:
                if (self.node_types.get(party_id) != "Party"
                        or self.node_files[party_id] != self.node_files[op["id"]]):
                    raise QueryError("counterparty party_id must name a Party in its graph")
                # Bank direction selects the counterparty role. An unknown
                # direction can retain a declared role; when both are unknown,
                # either single identity route is usable without guessing one.
                role = {"inflow": "payer", "outflow": "payee"}.get(op["direction"])
                declared = counterparty.get("role")
                if declared in {"payer", "payee"}:
                    if role is not None and declared != role:
                        raise QueryError("counterparty role disagrees with operation direction")
                    role = declared
                relevant = {role.upper()} if role is not None else {"PAYER", "PAYEE"}
                parties = [edge["to"] for edge in self.adjacency[op["id"]]
                           if edge["type"] in relevant and edge["from"] == op["id"]]
                if parties != [party_id]:
                    raise QueryError("counterparty party_id must agree with exactly one relevant PAYER/PAYEE edge")
        # Validate even empty statements; a currency label cannot be borrowed
        # from another graph or disagree with the statement's typed links.
        for sid, statement in self.statements.items():
            account_id = statement["account_id"]
            if (self.node_types.get(account_id) != "Account"
                    or self.node_files[account_id] != self.node_files[sid]):
                raise QueryError("statement account_id must name an Account in its graph")
            linked_accounts = [edge["to"] for edge in self.adjacency[sid]
                               if edge["type"] == "FOR_ACCOUNT" and edge["from"] == sid]
            if linked_accounts != [account_id]:
                raise QueryError("statement account_id must agree with exactly one FOR_ACCOUNT edge")
            currency_id = statement.get("currency_context_id")
            if currency_id is None:
                if any(edge["type"] == "IN_CURRENCY" and edge["from"] == sid for edge in self.adjacency[sid]):
                    raise QueryError("null currency reference disagrees with IN_CURRENCY edge")
                continue
            if (self.node_types.get(currency_id) != "CurrencyContext"
                    or self.node_files[currency_id] != self.node_files[sid]):
                raise QueryError("statement currency_context_id must name a CurrencyContext in its graph")
            linked = [edge["to"] for edge in self.adjacency[sid]
                      if edge["type"] == "IN_CURRENCY" and edge["from"] == sid]
            if linked != [currency_id]:
                raise QueryError("statement currency_context_id must agree with its IN_CURRENCY edge")
        by_statement = defaultdict(list)
        for op in self.operations:
            by_statement[op["statement_id"]].append(op)
        for sid, statement in self.statements.items():
            ops = by_statement[sid]
            if "archive_only" in statement and (statement["archive_only"] is not True or ops
                    or statement["expected_operations"] != 0 or any(statement.get(f) is not None
                        for f in ("printed_debit_total", "printed_credit_total"))):
                raise QueryError("archive_only Statement has operations/invented turnovers or invalid declaration")
            expected = statement["expected_operations"]
            if expected is not None:
                if statement["rows_complete"] and expected != len(ops):
                    raise QueryError("rows_complete Statement expected_operations disagrees with indexed rows")
                if sorted(o["ordinal"] for o in ops) != list(range(1, len(ops) + 1)):
                    raise QueryError("Statement operation ordinal sequence is inconsistent")
        for graph_name, artifacts, item in pending_semantics:
            self.verified_units.update(semantic_source(self._read(graph_name), artifacts, item))
        coverage = self.manifest.get("coverage")
        if isinstance(coverage, dict):
            for field, actual in (("statements", len(self.statements)), ("operations", len(self.operations)),
                                  ("operation_bearing_statements", sum(bool(by_statement[sid]) for sid in self.statements)),
                                  ("archive_only_statements", sum(bool(p.get("archive_only")) for p in self.statements.values()))):
                declared_count(coverage, field, actual, "manifest coverage")
        self.operations.sort(key=lambda p: (p.get("transaction_date") or "", p["statement_id"], p["ordinal"], p["id"]))
        self.node_counts, self.edge_counts = dict(node_counts), dict(edge_counts)
        self.snapshot = hashlib.sha256(encoded({"version": VERSION, "files": self.files}).encode()).hexdigest()
        # Source-derived overview extensions must also fit a readable response.
        self.overview()

    def _path(self, name):
        if not isinstance(name, str) or not name or "\0" in name or Path(name).is_absolute():
            raise QueryError("invalid manifest path")
        try:
            path = (self.archive / name).resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise QueryError("invalid or unavailable manifest path") from exc
        if not path.is_relative_to(self.archive) or ".." in Path(name).parts:
            raise QueryError("manifest path escapes archive")
        return path

    def _read(self, name):
        try:
            raw = self._path(name).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if name in self.files and self.files[name] != digest:
                raise QueryError("archive content changed; construct a fresh reader")
            self.files[name] = digest
            try:
                data = strict_json(raw)
            except ValueError as exc:
                raise QueryError("archive file unavailable or invalid JSON") from exc
            return object_value(data, "archive file root")
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise QueryError("archive file unavailable or invalid JSON") from exc

    def _node(self, node_id):
        name = self.node_files.get(node_id)
        if name is None:
            raise QueryError("node ID not found")
        if self._cache_path != name:
            self._cache = {n["id"]: n for n in self._read(name)["nodes"]}
            self._cache_path = name
        return self._cache[node_id]

    def _filters(self, filters):
        if filters is None:
            filters = {}
        if not isinstance(filters, dict) or set(filters) - FILTERS:
            raise QueryError("filters must use the documented allowlist")
        if len(encoded(filters).encode()) > MAX_QUERY_BYTES:
            raise QueryError("query exceeds 8192 bytes")
        for key, value in filters.items():
            if not isinstance(value, str) or not value or len(value) > 1000:
                raise QueryError("filter values must be nonempty strings of at most 1000 characters")
            if key in {"date_from", "date_to"}:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
                except ValueError as exc:
                    raise QueryError("dates must be real ISO YYYY-MM-DD") from exc
        if filters.get("date_from", "") > filters.get("date_to", "9999-12-31"):
            raise QueryError("date range reversed")
        if "amount" in filters:
            decimal(filters["amount"])
        if "direction" in filters and filters["direction"] not in {"inflow", "outflow", "unknown"}:
            raise QueryError("direction must be inflow/outflow/unknown")
        if "selection" in filters and filters["selection"] not in {"include", "exclude_reissue", "hold", "all"}:
            raise QueryError("selection must be include/exclude_reissue/hold/all")
        return {"selection": "include", **filters}

    def _matches(self, p, filters):
        nested = {"processing_status": ("processing", "status"),
                  "payment_text_status": ("payment1c_text", "status"),
                  "settlement_status": ("settlement", "status"),
                  "counterparty_name": ("effective_counterparty", "name_raw"),
                  "counterparty_unp": ("effective_counterparty", "unp_raw"),
                  "counterparty_internal_id": ("effective_counterparty", "internal_id"),
                  "observed_counterparty_name": ("observed_counterparty", "name_raw"),
                  "observed_counterparty_unp": ("observed_counterparty", "unp_raw"),
                  "rko_status": ("one_c_rko", "status")}
        for key, value in filters.items():
            if key == "selection":
                actual = self.statements[p["statement_id"]].get("selection")
                if value != "all" and actual != value:
                    return False
            elif key in {"date_from", "date_to"}:
                actual = p.get("transaction_date")
                if not actual or (key == "date_from" and actual < value) or (key == "date_to" and actual > value):
                    return False
            elif key == "purpose_contains":
                if value.casefold() not in (p.get("purpose_raw") or "").casefold():
                    return False
            elif key == "amount":
                if p.get("amount") is None or decimal(p["amount"]) != decimal(value):
                    return False
            elif key in {"payer_unp", "payee_unp"}:
                role = key.removesuffix("_unp")
                values = [a["official_unp"] for a in p.get("effective_parties", []) if a["role"] == role]
                if values != [value]:
                    return False
            elif key in nested:
                obj, field = nested[key]
                actual = (p.get(obj) or {}).get(field)
                if key == "counterparty_name":
                    actual = (p.get(obj) or {}).get("confirmed_name") or actual
                if field == "status":
                    actual = actual or "unknown"
                if actual != value:
                    return False
            else:
                actual = p.get({"operation_id": "id", "document_number": "document_number_raw"}.get(key, key))
                if actual != value:
                    return False
        return True

    def _envelope(self, **data):
        # Public JSON belongs to the caller, including nested source extensions.
        # Detach at the common boundary so no response aliases snapshot/index,
        # controls or adjacency state retained by this long-lived reader.
        return detached_json(bounded({"contract_version": VERSION, "snapshot": self.snapshot, **data}))

    def _page(self, rows, query, limit, cursor):
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise QueryError("limit must be an integer in 1..100")
        qhash = hashlib.sha256(encoded({"query": query, "limit": limit}).encode()).hexdigest()
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                raw = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
                payload = strict_json(raw)
                if not isinstance(payload, dict):
                    raise ValueError()
                check = payload.pop("check")
                if check != hashlib.sha256(encoded(payload).encode()).hexdigest():
                    raise ValueError()
                if payload.get("snapshot") != self.snapshot or payload.get("query") != qhash or payload.get("version") != VERSION:
                    raise ValueError()
                offset = payload["offset"]
                if type(offset) is not int or offset <= 0 or offset >= len(rows) or offset % limit:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise QueryError("invalid/stale/query-mismatched cursor; restart from first page") from exc
        end = min(offset + limit, len(rows))
        next_cursor = None
        if end < len(rows):
            payload = {"version": VERSION, "snapshot": self.snapshot, "query": qhash, "offset": end}
            payload["check"] = hashlib.sha256(encoded(payload).encode()).hexdigest()
            next_cursor = base64.urlsafe_b64encode(encoded(payload).encode()).decode()
        return self._envelope(items=rows[offset:end], total=len(rows), next_cursor=next_cursor)

    def overview(self):
        summaries = []
        for sid, p in sorted(self.statements.items()):
            item = self.inputs[self.node_files[sid]]
            summaries.append({"id": sid, "period_start": p.get("period_start"), "period_end": p.get("period_end"),
                              "selection": p.get("selection"), "expected_operations": p.get("expected_operations"),
                              "source_ready_declared": item.get("source_ready"), "processing_admission_declared": item.get("processing_admission"),
                              "source_unit_review": self.units.get(sid, {})})
        return self._envelope(archive_status_declared=self.manifest.get("status"), graphs=len(self.inputs),
                              node_counts=self.node_counts, edge_counts=self.edge_counts,
                              selection_counts=dict(Counter(p.get("selection", "unknown") for p in self.statements.values())),
                              processing_counts=dict(Counter((p.get("processing") or {}).get("status") or "unknown" for p in self.operations)),
                              statements=summaries, source_files=self.files,
                              authenticity="content snapshot only; original PDF authenticity not reaudited")

    def search(self, filters=None, limit=20, cursor=None):
        filters = self._filters(filters)
        rows = []
        for p in self.operations:
            if not self._matches(p, filters):
                continue
            row = {k: p.get(k) for k in ("id", "statement_id", "ordinal", "transaction_date", "document_number_raw", "direction", "amount", "printed_debit", "printed_credit", "row_status", "duplicate_status")}
            purpose = p.get("purpose_raw") or ""
            row["effective_counterparty"] = detached_json(p.get("effective_counterparty") or {})
            row["effective_parties"] = detached_json(p.get("effective_parties") or [])
            row.update(purpose_snippet=purpose[:160], purpose_chars=len(purpose), purpose_snippet_only=True,
                       processing_status=(p.get("processing") or {}).get("status") or "unknown",
                       payment_text_status=(p.get("payment1c_text") or {}).get("status") or "unknown",
                       bank_inflow_candidate=incoming_candidate(p, self.statements[p["statement_id"]].get("debit_is")))
            rows.append(row)
        return self._page(rows, {"method": "search", "filters": filters}, limit, cursor)

    @staticmethod
    def _get_payload(node, graph, related_count, snapshot):
        return {"contract_version": VERSION, "snapshot": snapshot,
                "effective_parties": _canonical_modules()["counterparty_identity"].effective_parties(node) if node["type"] == "Operation" else [],
                "node": node, "graph": graph, "related_count": related_count,
                "next_step": "links(node_id) for bounded typed neighbors"}

    def get(self, node_id):
        if not isinstance(node_id, str):
            raise QueryError("node_id must be a string")
        return detached_json(bounded(self._get_payload(self._node(node_id), self.node_files[node_id],
                                                      len(self.adjacency[node_id]), self.snapshot)))

    def links(self, node_id, edge_types=None, direction="both", limit=20, cursor=None):
        if not isinstance(node_id, str):
            raise QueryError("node_id must be a string")
        if node_id not in self.node_files:
            raise QueryError("node ID not found")
        if direction not in {"in", "out", "both"}:
            raise QueryError("link direction must be in/out/both")
        if edge_types is None:
            edge_types = []
        if not isinstance(edge_types, list) or any(not isinstance(e, str) or e not in EDGE_ENDPOINTS for e in edge_types):
            raise QueryError("unknown edge type")
        edge_types = sorted(set(edge_types))
        rows = []
        for edge in sorted(self.adjacency[node_id], key=lambda e: (e["type"], e["id"])):
            if edge_types and edge["type"] not in edge_types:
                continue
            if direction == "in" and edge["to"] != node_id or direction == "out" and edge["from"] != node_id:
                continue
            other = edge["to"] if edge["from"] == node_id else edge["from"]
            rows.append({"edge": edge, "neighbor": {"id": other, "type": self.node_types[other]},
                         "relative_direction": "out" if edge["from"] == node_id else "in"})
        return self._page(rows, {"method": "links", "node_id": node_id, "edge_types": edge_types, "direction": direction}, limit, cursor)

    def aggregate(self, filters=None, group_by="statement", limit=20, cursor=None):
        filters = self._filters(filters)
        if group_by not in {"statement", "year", "counterparty", "none"}:
            raise QueryError("group_by must be statement/year/counterparty/none")
        groups = {}
        matched = [p for p in self.operations if self._matches(p, filters)]
        # Sufficient for every input coefficient, fractional alignment and carry.
        precision = 32 + sum(len(p.get("amount") or "") for p in matched)
        with localcontext() as context:
            context.prec = precision
            for p in matched:
                sid = p["statement_id"]
                statement = self.statements[sid]
                unit = self.units.get(sid, {})
                verified = sid in self.verified_units
                currency = self.currencies[statement["currency_context_id"]].get("printed_currency")
                unit_key = (unit["source_unit"], unit["source_epoch"], None) if verified else (None, None, sid)
                extra = {"statement": sid, "year": statement.get("year"),
                         "counterparty": (p.get("effective_counterparty") or {}).get("internal_id") or (p.get("counterparty") or {}).get("party_id"), "none": None}[group_by]
                # Never combine directions or printed currencies, even if units match.
                key = encoded([*unit_key, currency, p.get("direction", "unknown"), extra])
                if key not in groups:
                    groups[key] = {"source_unit": unit_key[0], "source_epoch": unit_key[1], "unit_status": "verified" if verified else "unknown",
                                   "unknown_unit_statement": unit_key[2], "printed_currency": currency, "direction": p.get("direction", "unknown"),
                                   "group_by": group_by, "group": extra, "count": 0, "known_amount_count": 0, "unknown_amount_count": 0,
                                   "sum_known_amount": Decimal(0), "processing_counts": Counter(), "payment_text_counts": Counter()}
                row = groups[key]
                row["count"] += 1
                row["processing_counts"][(p.get("processing") or {}).get("status") or "unknown"] += 1
                row["payment_text_counts"][(p.get("payment1c_text") or {}).get("status") or "unknown"] += 1
                if p.get("amount") is None:
                    row["unknown_amount_count"] += 1
                else:
                    row["known_amount_count"] += 1
                    row["sum_known_amount"] += decimal(p["amount"])
        rows = []
        for key in sorted(groups):
            row = groups[key]
            row["sum_known_amount"] = format(row["sum_known_amount"], "f") if row["known_amount_count"] else None
            row["complete_amount_sum"] = row["unknown_amount_count"] == 0
            row["processing_counts"], row["payment_text_counts"] = dict(row["processing_counts"]), dict(row["payment_text_counts"])
            rows.append(row)
        result = self._page(rows, {"method": "aggregate", "filters": filters, "group_by": group_by}, limit, cursor)
        result["matched_operations"] = len(matched)
        result["selection_counts_all_statements"] = dict(Counter(p.get("selection", "unknown") for p in self.statements.values()))
        result["arithmetic"] = "Operation.amount only; no components, currency conversion or readiness exclusion"
        return bounded(result)


def contract():
    return {"contract_version": VERSION, "read_only": True, "filters": sorted(FILTERS),
            "limits": {"page_items": MAX_LIMIT, "output_bytes": MAX_OUTPUT_BYTES, "query_bytes": MAX_QUERY_BYTES,
                       "json_container_depth": MAX_JSON_DEPTH, "json_integer_digits": MAX_JSON_INTEGER_DIGITS,
                       "json_number_roundtrip": "exact decimal value of finite serialized token; otherwise reject before admission"},
            "methods": ["contract", "overview", "search", "get", "links", "aggregate"],
            "ordering": {"search": ["transaction_date null first", "statement_id", "ordinal", "id"],
                         "links": ["edge.type", "edge.id"], "aggregate": ["canonical group key"]},
            "response_fields": {"all_data": ["contract_version", "snapshot"],
                                "pages": ["items", "total", "next_cursor"],
                                "get": ["node", "graph", "related_count", "next_step"],
                                "aggregate": ["matched_operations", "selection_counts_all_statements", "arithmetic"]},
            "filter_semantics": {"combination": "AND", "dates": "inclusive transaction_date ISO YYYY-MM-DD",
                                 "amount": "exact Decimal string equality", "purpose_contains": "casefold substring candidate only",
                                 "default_selection": "include", "other_fields": "exact string equality"},
            "edge_endpoints": {k: [sorted(v[0]), sorted(v[1])] for k, v in EDGE_ENDPOINTS.items()},
            "manual": "README.md", "schema": "onec_bank_graph/references/graph.schema.json",
            "instructions": "Start overview, narrow search, follow next_cursor unchanged, get exact ID and links. Search purpose is a candidate only. For revenue examine full purpose after bank inflow gate. Aggregate units/epochs separately; null is unknown. No 1C authority."}


def main():
    parser = QueryArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=["contract", "overview", "search", "get", "links", "aggregate"])
    parser.add_argument("--archive", help="operator-selected private archive directory; required for data commands")
    parser.add_argument("--filters", default="{}", help="allowlisted JSON object; dates inclusive")
    parser.add_argument("--id", dest="node_id")
    parser.add_argument("--edge-types", default="[]", help="JSON list")
    parser.add_argument("--direction", default="both", help="links: in/out/both")
    parser.add_argument("--group-by", default="statement")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cursor")
    try:
        args = parser.parse_args()
        if args.command in {"get", "links"} and args.node_id is None:
            raise QueryError(f"{args.command} requires --id")
        if args.command == "contract":
            result = contract()
        else:
            if not args.archive:
                raise QueryError("--archive is required for data commands")
            reader = Reader(args.archive)
            if args.command == "overview":
                result = reader.overview()
            elif args.command == "get":
                result = reader.get(args.node_id)
            elif args.command == "links":
                result = reader.links(args.node_id, strict_json(args.edge_types), args.direction, args.limit, args.cursor)
            elif args.command == "search":
                result = reader.search(strict_json(args.filters), args.limit, args.cursor)
            else:
                result = reader.aggregate(strict_json(args.filters), args.group_by, args.limit, args.cursor)
        print(encoded(result))
        return 0
    except (QueryError, json.JSONDecodeError) as exc:
        print(encoded({"error": str(exc), "contract_version": VERSION}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
