"""PO.BY-oriented bank statement classification rules.

The rules are intentionally data-free: real 7.7 bank statement rows stay in
local work artifacts.  Each returned decision keeps the source identity and
amount intact so a migration batch can audit source-line -> rule -> target.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


POBY_BANK_STATEMENT_OSN_URL = "https://po.by/knowledge-base-by/zagruzka-vypiski-banka-v-1s-osn/"


@dataclass(frozen=True)
class Rule:
    id: str
    priority: int
    decision_required: bool
    confidence: str
    reason: str
    target: dict[str, Any]
    predicate: Callable[["LineView"], bool]
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedAmount:
    name: str
    present: bool
    raw_text: str
    value: Decimal
    malformed: bool = False


@dataclass(frozen=True)
class AmountState:
    direction: str
    amount: Decimal
    amount_text: str
    issue: str | None = None
    issue_detail: str | None = None


class LineView:
    def __init__(self, raw: Mapping[str, Any], source: Mapping[str, Any] | None = None) -> None:
        self.raw = raw
        self.source = dict(source or {})
        self.amount_state = _amount_state(raw)
        self.direction = self.amount_state.direction
        self.amount = self.amount_state.amount
        self.amount_text = self.amount_state.amount_text
        self.amount_issue = self.amount_state.issue
        self.amount_issue_detail = self.amount_state.issue_detail
        self.source_account = _account_code(raw)
        self.purpose = _text(raw.get("purpose"))
        self.sub1 = _subconto_text(raw.get("sub1") or raw.get("counterparty"))
        self.sub2 = _subconto_text(raw.get("sub2") or raw.get("agreement"))
        self.source_id = _source_id(raw, self.source)

    @property
    def searchable(self) -> str:
        return _normalize(" ".join((self.purpose, self.sub1, self.sub2)))

    @property
    def revenue_context(self) -> str:
        raw_row = self.raw.get("bank_row_raw")
        return _normalize(" ".join((self.purpose, self.sub1, self.sub2,
                                    raw_row if isinstance(raw_row, str) else "")))

    @property
    def purpose_n(self) -> str:
        return _normalize(self.purpose)

    @property
    def sub1_n(self) -> str:
        return _normalize(self.sub1)


def classify_line(raw: Mapping[str, Any], source: Mapping[str, Any] | None = None, *, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    line = LineView(raw, source)
    raw_row = raw.get("bank_row_raw")
    if _income_revenue_scope(line) and raw_row is not None and not isinstance(raw_row, str):
        # Supplied malformed context cannot become an invented empty bank row.
        line.amount_issue = "cash_revenue_raw_context_invalid"
        line.amount_issue_detail = "bank_row_raw must preserve the source string"
        rule = _amount_issue_rule(line)
    elif line.amount_issue and not _blank_zero(line):
        rule = _amount_issue_rule(line)
    else:
        rule = next((candidate for candidate in RULES if candidate.predicate(line)), _fallback_rule(line))
        # A newly raw-selected safe income route needs the same bank evidence.
        # Keep the accepted purpose/subconto card rules outside this extension.
        context_without_raw = LineView({key: value for key, value in raw.items()
                                        if key != "bank_row_raw"}, source)
        new_raw_route = (_income_revenue_scope(line) and not rule.decision_required
                         and not rule.predicate(context_without_raw)
                         and not (rule.id == "income_card_erip_intermediary"
                                  and _income_legacy_card_erip(context_without_raw)))
        if (rule.id == "income_cash_revenue_deposit" or new_raw_route) and not _revenue_money_safe(line):
            if line.amount_issue is None:
                line.amount_issue = "cash_revenue_amount_conflict"
                line.amount_issue_detail = "cash receipt needs matching positive printed credit, observed blank or zero debit and resolved amount"
            rule = _amount_issue_rule(line)
    return _decision(line, rule, policy=policy)


def classify_lines(lines: Iterable[Mapping[str, Any]], *, policy: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return [classify_line(line, policy=policy) for line in lines]


def decisions_from_source_compact(document: Mapping[str, Any], *, policy: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for statement in document.get("bank", []):
        source = {
            "documentCursor": statement.get("cursor"),
            "documentNumber": statement.get("number"),
            "date": statement.get("date"),
        }
        for line in statement.get("lines", []):
            decisions.append(classify_line(line, source, policy=policy))
    return decisions


def summarize(decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    total = 0
    safe = 0
    decision_required = 0
    rule_counts: dict[str, int] = {}
    for item in decisions:
        total += 1
        rule_id = str(item["classification"]["ruleId"])
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
        if item["classification"]["decisionRequired"]:
            decision_required += 1
        else:
            safe += 1
    return {
        "totalLines": total,
        "safeClassifiedLines": safe,
        "decisionRequiredLines": decision_required,
        "ruleCounts": dict(sorted(rule_counts.items())),
    }


def _decision(line: LineView, rule: Rule, *, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validation = {"status": "ok"} if not line.amount_issue else {
        "status": "needsDecision",
        "reason": line.amount_issue,
        "detail": line.amount_issue_detail,
    }
    target = json.loads(json.dumps(rule.target, ensure_ascii=False))
    if rule.id == "expense_usn_tax":
        # Semantic classifier output only; classification does not establish OData field names.
        target["taxPeriod"] = _usn_tax_period(line)
    configured = policy.get(rule.id) if isinstance(policy, Mapping) else None
    enabled = (not rule.decision_required and isinstance(configured, Mapping)
               and isinstance(configured.get("review_ref"), str) and bool(configured["review_ref"].strip())
               and isinstance(configured.get("target"), Mapping) and bool(configured["target"]))
    if enabled:
        target.update(json.loads(json.dumps(configured["target"], ensure_ascii=False, allow_nan=False)))
    else:
        target["decision"] = "supply independently reviewed target policy before posting"
    return {
        "source": {
            "sourceId": line.source_id,
            "date": line.source.get("date") or line.raw.get("date"),
            "documentCursor": line.source.get("documentCursor") or line.raw.get("documentCursor"),
            "documentNumber": line.source.get("documentNumber") or line.raw.get("documentNumber"),
            "lineNumber": line.raw.get("lineNumber", line.raw.get("line")),
            "direction": line.direction,
            "amount": line.amount_text,
            "sourceAccount": line.source_account,
            "amountValidation": validation,
        },
        "classification": {
            "ruleId": rule.id,
            "priority": rule.priority,
            "decisionRequired": not enabled,
            "confidence": rule.confidence,
            "reason": rule.reason,
            "evidence": [configured["review_ref"]] if enabled else [],
        },
        "target": target,
        "crossCheck": {
            "sourceAccountIsAuthoritative": False,
            "sourceAccountRole": "cross-check only; purpose, counterparty and amount drive the rule",
        },
    }


def _fallback_rule(line: LineView) -> Rule:
    if line.direction == "expense":
        return Rule(
            id="expense_unclassified_needs_decision",
            priority=998,
            decision_required=True,
            confidence="low",
            reason="No configuration-specific write-off rule matched after unsafe fallbacks were removed.",
            target={
                "document": "BankWriteOff",
                "operationCandidates": ["supplierPayment", "otherWriteOff", "taxPayment"],
                "decision": "confirm target operation and account before posting",
            },
            predicate=lambda _line: True,
            evidence=(),
        )
    return Rule(
        id="income_unclassified_needs_decision",
        priority=999,
        decision_required=True,
        confidence="low",
        reason="No configuration-specific receipt rule matched after unsafe fallbacks were removed.",
        target={
            "document": "BankReceipt",
            "operationCandidates": ["buyerPayment", "otherReceipt"],
            "decision": "confirm target operation and account before posting",
        },
        predicate=lambda _line: True,
        evidence=(),
    )


def _amount_issue_rule(line: LineView) -> Rule:
    issue = line.amount_issue or "amount_or_direction"
    return Rule(
        id=f"{issue}_needs_decision",
        priority=1,
        decision_required=True,
        confidence="high",
        reason="Amount and direction must be resolved before applying bank-statement rules.",
        target={
            "documentCandidates": ["BankReceipt", "BankWriteOff"],
            "decision": "fix or confirm amount, direction and source fields before posting",
        },
        predicate=lambda _line: True,
        evidence=(),
    )


def _amount_state(raw: Mapping[str, Any]) -> AmountState:
    explicit = _normalize(_text(raw.get("direction")))
    explicit_direction = ""
    if explicit in {"income", "приход", "receipt"}:
        explicit_direction = "income"
    elif explicit in {"expense", "расход", "writeoff", "write-off"}:
        explicit_direction = "expense"

    income = _parse_amount_field("income", raw.get("income"))
    expense = _parse_amount_field("expense", raw.get("expense"))
    amount = _parse_amount_field("amount", raw.get("amount"))
    fields = (income, expense, amount)
    malformed = tuple(field for field in fields if field.malformed)
    if malformed:
        return AmountState(
            direction=explicit_direction or "zero_or_mixed",
            amount=Decimal("0"),
            amount_text=_amount_raw_summary(malformed),
            issue="amount_malformed",
            issue_detail="malformed numeric field: " + ", ".join(field.name for field in malformed),
        )

    income_nonzero = income.value > 0
    expense_nonzero = expense.value > 0
    if income_nonzero and expense_nonzero:
        return AmountState(
            direction="zero_or_mixed",
            amount=Decimal("0"),
            amount_text=_amount_raw_summary((income, expense)),
            issue="amount_mixed",
            issue_detail="both income and expense fields are non-zero",
        )

    if explicit_direction == "income" and (expense_nonzero or amount.value < 0):
        return AmountState(
            direction="income",
            amount=_chosen_amount_text_decimal(income, amount),
            amount_text=_amount_raw_summary(tuple(field for field in fields if field.present)),
            issue="amount_direction_conflict",
            issue_detail="explicit income direction conflicts with expense or negative amount",
        )
    if explicit_direction == "expense" and income_nonzero:
        return AmountState(
            direction="expense",
            amount=_chosen_amount_text_decimal(expense, amount),
            amount_text=_amount_raw_summary(tuple(field for field in fields if field.present)),
            issue="amount_direction_conflict",
            issue_detail="explicit expense direction conflicts with income field",
        )

    if explicit_direction:
        direction = explicit_direction
    elif expense_nonzero or amount.value < 0:
        direction = "expense"
    elif income_nonzero or amount.value > 0:
        direction = "income"
    else:
        direction = "zero_or_mixed"

    resolved_amount = Decimal("0")
    if direction == "expense":
        resolved_amount = expense.value if expense_nonzero else abs(amount.value)
    elif direction == "income":
        resolved_amount = income.value if income_nonzero else abs(amount.value)

    amount_text = format(resolved_amount, "f")
    if resolved_amount == 0:
        return AmountState(
            direction=direction,
            amount=resolved_amount,
            amount_text=amount_text,
            issue="amount_zero",
            issue_detail="resolved operation amount is zero",
        )
    return AmountState(direction=direction, amount=resolved_amount, amount_text=amount_text)


def _parse_amount_field(name: str, value: Any) -> ParsedAmount:
    if value is None or value == "":
        return ParsedAmount(name=name, present=False, raw_text="", value=Decimal("0"))
    if isinstance(value, Decimal):
        if value.is_finite():
            return ParsedAmount(name=name, present=True, raw_text=format(value, "f"), value=value)
        return ParsedAmount(name=name, present=True, raw_text=str(value), value=Decimal("0"), malformed=True)
    if isinstance(value, (int, float)):
        text = str(value)
    else:
        text = str(value).strip()
    # Preserve genuinely grouped cells (including narrow NBSP) before the
    # legacy parser. Cash-revenue admission separately rejects malformed grammar.
    printed = _printed_money_value(value)
    if printed is not None:
        return ParsedAmount(name=name, present=True, raw_text=text, value=printed)
    normalized = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return ParsedAmount(name=name, present=True, raw_text=text, value=Decimal("0"), malformed=True)
    if not parsed.is_finite():
        return ParsedAmount(name=name, present=True, raw_text=text, value=Decimal("0"), malformed=True)
    return ParsedAmount(name=name, present=True, raw_text=text, value=parsed)


def _amount_raw_summary(fields: Iterable[ParsedAmount]) -> str:
    present = [field for field in fields if field.present]
    if not present:
        return ""
    if len(present) == 1:
        return present[0].raw_text
    return "; ".join(f"{field.name}={field.raw_text}" for field in present)


def _chosen_amount_text_decimal(primary: ParsedAmount, fallback: ParsedAmount) -> Decimal:
    if primary.present and primary.value != 0:
        return abs(primary.value)
    if fallback.present:
        return abs(fallback.value)
    return Decimal("0")


def _account_code(raw: Mapping[str, Any]) -> str:
    direct = raw.get("sourceAccount") or raw.get("source_account")
    if direct:
        return _text(direct)
    account = raw.get("account")
    if isinstance(account, Mapping):
        return _text(account.get("code"))
    return _text(account)


def _subconto_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_text(value.get(name)) for name in ("code", "name", "description", "fullName"))
    return _text(value)


def _source_id(raw: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    explicit = raw.get("sourceId") or raw.get("source_id")
    if explicit:
        return _text(explicit)
    cursor = source.get("documentCursor") or raw.get("documentCursor")
    line = raw.get("lineNumber", raw.get("line"))
    if cursor is not None and line is not None:
        return f"{cursor}:{line}"
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def _has(line: LineView, pattern: str, *, fields: str = "all") -> bool:
    text = line.searchable
    if fields == "purpose":
        text = line.purpose_n
    elif fields == "sub1":
        text = line.sub1_n
    elif fields == "raw":
        text = line.revenue_context
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _not_has(line: LineView, pattern: str) -> bool:
    return not _has(line, pattern)


def _account_in(line: LineView, *codes: str) -> bool:
    return line.source_account in set(codes)


def _has_sub1(line: LineView) -> bool:
    return bool(line.sub1_n)


def _blank_zero(line: LineView) -> bool:
    return line.amount == 0 and not line.source_account and not line.purpose_n


def _income_erroneous_or_government(line: LineView) -> bool:
    return line.direction == "income" and _has(
        line, r"ошибоч|финансовое управление|мингорисполком|гумф|угк|казнач|райфо", fields="raw"
    )


def _income_charter_or_special_account(line: LineView) -> bool:
    return line.direction == "income" and _account_in(line, "55.5")


def _income_loan_short_term(line: LineView) -> bool:
    return (
        line.direction == "income"
        and _account_in(line, "66.4")
        and _has(line, r"займ|зама|дог\.?\s*вр займа|взнос по дог займа")
    )


def _income_loan_keyword_unresolved(line: LineView) -> bool:
    return (
        line.direction == "income"
        and not _account_in(line, "66.4")
        and _has(line, r"займ|зама|дог\.?\s*вр займа|взнос по дог займа")
    )


def _income_card_erip(line: LineView) -> bool:
    if _income_revenue_scope(line):
        # Within the owner's cash correction scope, generic transfer and
        # electronic-document wording do not prove card/ERIP revenue.
        return _income_explicit_card_erip(line)
    return _income_legacy_card_erip(line)


def _income_legacy_card_erip(line: LineView) -> bool:
    """Accepted purpose/subconto predicate, without the new raw context."""
    return line.direction == "income" and _has(
        line,
        r"пластик|карточ|ерип|easypay|электронн|денежн.*перевод|птс\.?\s*сис\.?\s*easypay|операций:.*комиссия",
    )


_SYMBOL_2004_LINE = re.compile(r"^[ \t\u00a0]*(?:\x1b4|\(cid:27\)4)?символ[ \t\u00a0]+2004(?=[ \t\u00a0:]|$)", re.IGNORECASE)
# Presence is deliberately broader than the supported exact grammar. A label
# with an unknown prefix, code or boundary must not disappear as "absent".
_SYMBOL_COMPONENT_PRESENCE = re.compile(r"символ(?![а-яёa-z])", re.IGNORECASE)
_SYMBOL_COMPONENT_LINE = re.compile(
    r"^[ \t\u00a0]*(?P<marker>\x1b4|\(cid:27\)4)?символ[ \t\u00a0]+"
    r"(?P<code>[0-9]{4})(?=[ \t\u00a0:\r\n]|$)(?P<tail>[^\r\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
_COMPONENT_MONEY = re.compile(
    r"(?P<amount>(?:[0-9]{1,3}(?P<group>[ \u00a0\u202f])[0-9]{3}"
    r"(?:(?P=group)[0-9]{3})*|[0-9]+)(?:[.,][0-9]{1,2})?)"
    r"(?:[ \t\u00a0]+(?P<unit>BYN|BYR))?", re.IGNORECASE,
)


def _component_money(tail: str, marker: str | None = None) -> tuple[Decimal, str | None] | None:
    tail = tail.strip(" \t\u00a0")
    if tail.startswith(":"):
        tail = tail[1:].lstrip(" \t\u00a0")
    if marker:
        # ESC and its literal CID rendering are the same known command pair.
        end = next((value for value in ("\x1b5", "(cid:27)5") if tail.endswith(value)), None)
        if end is None:
            return None
        tail = tail[:-len(end)].rstrip(" \t\u00a0")
    money = _COMPONENT_MONEY.fullmatch(tail)
    if money is None:
        return None
    amount = Decimal(re.sub(r"[ \u00a0\u202f]", "", money.group("amount")).replace(",", "."))
    unit = money.group("unit")
    return amount, unit.upper() if unit is not None else None


def _symbol_component_status(credit: Decimal, *texts: Any, components: Any = ()) -> str:
    """All present printed/typed assertions must resolve; code-only is absent.

    Graph callers preserve full CashSymbol properties in components, including
    unresolved amount_raw/markers_raw. Exact known print pairs may be read, but
    flattened marker-like digits are never removed or rounded.
    """
    assertions = []
    for text in texts:
        if not isinstance(text, str):
            continue
        # splitlines inventories CR/LF/CRLF and Unicode source line boundaries.
        # Full-line parsing also holds inline labels and unsupported print prefixes;
        # never delete guessed digits or silently ignore a later assertion.
        for line in text.splitlines():
            if _SYMBOL_COMPONENT_PRESENCE.search(line) is None:
                continue
            match = _SYMBOL_COMPONENT_LINE.fullmatch(line)
            if match is None:
                return "unknown"
            assertions.append((match.group("code"), _component_money(match.group("tail"), match.group("marker"))))
    if not isinstance(components, (list, tuple)):
        return "unknown"
    for component in components:
        if not isinstance(component, Mapping) or not isinstance(component.get("code_raw"), str):
            return "unknown"
        code = component["code_raw"]
        if not re.fullmatch(r"[0-9]{4}", code):
            return "unknown"
        if set(component) == {"code_raw"}:
            continue  # A code alone does not assert an amount.
        raw = component.get("amount_raw")
        if not isinstance(raw, str):
            return "unknown"
        markers = component.get("markers_raw")
        if markers is not None and not isinstance(markers, str):
            return "unknown"
        if markers:
            match = _SYMBOL_COMPONENT_LINE.fullmatch(markers.strip("\r\n"))
            if (match is None or match.group("code") != code
                    or match.group("tail").lstrip(" \t\u00a0:") != raw.strip(" \t\u00a0")):
                return "unknown"
            value = _component_money(match.group("tail"), match.group("marker"))
        else:
            value = _component_money(raw)
        normalized = component.get("amount")
        if normalized is not None:
            if not isinstance(normalized, str) or _component_money(normalized) != value:
                return "unknown"
        assertions.append((code, value))
    # Explicit source units are retained. The existing input has no supported
    # binding between a component unit and the reviewed bank-credit unit;
    # opaque evidence references cannot prove equivalence or denomination.
    if any(value is None or value[1] is not None for _, value in assertions):
        return "unknown"
    if any(code == "2004" and value[0] != credit or code != "2004" and value[0] > 0
           for code, value in assertions):
        return "conflict"
    return "matched" if assertions else "absent"


def _symbol_projections_agree(facts: Mapping[str, Any]) -> bool:
    """Both supplied fields describe the same links, including empty sets.

    Omission keeps legacy code-only facts valid. Presence of an empty or invalid
    projection is evidence too; it cannot silently corroborate a selecting code.
    """
    if "cash_symbols" in facts:
        codes = facts["cash_symbols"]
        if not isinstance(codes, (list, tuple)) or any(
            not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}", code) for code in codes
        ):
            return False
    if "cash_symbols" not in facts or "cash_symbol_components" not in facts:
        return True
    components = facts["cash_symbol_components"]
    if not isinstance(components, (list, tuple)):
        return False
    if any(not isinstance(item, Mapping) or not isinstance(item.get("code_raw"), str)
           or not re.fullmatch(r"[0-9]{4}", item["code_raw"]) for item in components):
        return False
    return set(codes) == {item["code_raw"] for item in components}


def has_cash_symbol_2004(cash_symbols: Any = None, bank_row_raw: Any = None) -> bool:
    """Exact typed code or printed bank-service label; never account/amount/code15.

    cash_symbols contains projected HAS_CASH_SYMBOL -> CashSymbol.code_raw
    strings. bank_row_raw is the unchanged bank-row observation, not a snippet.
    """
    return (isinstance(cash_symbols, (list, tuple))
            and any(type(code) is str and code == "2004" for code in cash_symbols)
            or isinstance(bank_row_raw, str) and any(
                _SYMBOL_2004_LINE.match(line) is not None for line in bank_row_raw.splitlines()
            ))


def _revenue_money_safe(line: LineView) -> bool:
    # Absent cells are unknown. An observed empty debit cell is a proven blank.
    if "printed_credit" not in line.raw or "printed_debit" not in line.raw:
        return False
    credit = _printed_money_value(line.raw["printed_credit"])
    debit_raw = line.raw["printed_debit"]
    debit = Decimal(0) if debit_raw == "" else _printed_money_value(debit_raw)
    if debit_raw is None:
        # Do not use LineView's display identity, which strips/coerces values.
        # The observation must bind to an actual nonempty, exact input identity.
        identities = [line.raw[key] for key in ("sourceId", "source_id") if key in line.raw]
        if not identities or not all(_proven_blank_debit(
                line.raw.get("printed_debit_observation"), ident) for ident in identities):
            return False
        debit = Decimal(0)
    if credit is None or credit <= 0 or debit != 0:
        return False
    if line.direction != "income" or line.amount != credit:
        return False
    for name in ("income", "amount", "expense"):
        if name not in line.raw:
            continue
        raw = line.raw[name]
        # Legacy missing normalized values are not extra amount assertions.
        if raw is None or raw == "":
            continue
        field = _printed_money_value(raw)
        if field is None or field != (Decimal(0) if name == "expense" else credit):
            return False
    if not _symbol_projections_agree(line.raw):
        line.amount_issue = "component_unknown"
        line.amount_issue_detail = "cash-symbol code projections disagree or are invalid"
        return False
    component_status = _symbol_component_status(credit, line.purpose, line.raw.get("bank_row_raw"),
                                                components=line.raw.get("cash_symbol_components", ()))
    if component_status in {"unknown", "conflict"}:
        line.amount_issue = "component_" + component_status
        line.amount_issue_detail = "printed cash-symbol allocation is " + component_status + "; whole-credit cash revenue needs separate exact evidence"
        return False
    return True


_PRINTED_MONEY = re.compile(
    r"[+-]?(?:[0-9]{1,3}(?P<group>[ \u00a0\u202f])[0-9]{3}"
    r"(?:(?P=group)[0-9]{3})*|[0-9]+)(?:[.,][0-9]{1,2})?"
)


def _printed_money_value(value: Any) -> Decimal | None:
    """Validate the entire cell before removing supported grouping characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip(" \t\u00a0\u202f")
    if _PRINTED_MONEY.fullmatch(text) is None:
        return None
    return Decimal(re.sub(r"[ \u00a0\u202f]", "", text).replace(",", "."))


def _proven_blank_debit(observation: Any, source_id: str) -> bool:
    return (isinstance(source_id, str) and bool(source_id.strip())
            and isinstance(observation, Mapping)
            and isinstance(observation.get("source_id"), str)
            and bool(observation["source_id"].strip())
            and observation["source_id"] == source_id
            and observation.get("field") == "printed_debit"
            and observation.get("status") == "absent"
            and observation.get("normalized") is None
            and isinstance(observation.get("raw"), str)
            and not observation["raw"].strip())


def _income_revenue_scope(line: LineView) -> bool:
    """Owner bank-income/date/purpose-or-symbol gate, without route selection."""
    raw_date = line.source.get("date") or line.raw.get("date")
    if not isinstance(raw_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?", raw_date
    ):
        return False
    try:
        datetime.fromisoformat(raw_date)
    except ValueError:
        return False
    return (line.direction == "income"
            and (_has(line, r"\b(?:выручк(?:а|и|у|е|ой|ою|ам|ами|ах)|выручек)\b", fields="purpose")
                 or has_cash_symbol_2004(line.raw.get("cash_symbols"), line.raw.get("bank_row_raw"))
                 or has_cash_symbol_2004(None, line.purpose)))


def _income_mixed_revenue_loan(line: LineView) -> bool:
    return _income_revenue_scope(line) and re.search(
        r"\b(?:займ\w*|за[её]м\w*|кредит\w*)\b", line.revenue_context
    ) is not None


def _income_explicit_card_erip(line: LineView) -> bool:
    text = line.revenue_context if _income_revenue_scope(line) else line.searchable
    return line.direction == "income" and re.search(r"пластик|карточ|ерип|easypay", text) is not None


_BANK_COMMISSION = r"комис|ком\.?\s*воз|вознаг|возн\."


def _income_revenue_intermediary_unresolved(line: LineView) -> bool:
    if not _income_revenue_scope(line) or _income_explicit_card_erip(line):
        return False
    # A document's electronic format is not an intermediary. Keep other
    # electronic/transfer contexts unresolved rather than guessing 57.1/50.1.
    text = re.sub(r"\bэлектронн\w*\s+документ\w*\b", "", line.revenue_context)
    # The accepted expense vocabulary is contradictory evidence for a cash RKO,
    # not an incoming commission accounting mapping. Scan both complete texts.
    return re.search(_BANK_COMMISSION + r"|электронн|денежн\w*\s+перевод|операций:.*комиссия|возврат\w*\s+комисси\w*", text) is not None


def _income_cash_deposit(line: LineView) -> bool:
    # Higher-priority explicit card/ERIP and conflict rules retain precedence.
    return (_income_revenue_scope(line) and not _income_mixed_revenue_loan(line)
            and not _income_bank_interest(line))


def _income_bank_interest(line: LineView) -> bool:
    return line.direction == "income" and _has(line, r"процент|плата за остат", fields="raw")


def _income_supplier_refund(line: LineView) -> bool:
    return (
        line.direction == "income"
        and _has_sub1(line)
        and _has(line, r"возврат|поставщик|ооо|одо|оао|зао|сзао|чуп|ип\s+")
        and _not_has(line, r"займ|процент|ошибоч|выручк|пластик|карточ|easypay|ерип")
    )


def _income_buyer_payment(line: LineView) -> bool:
    return (
        line.direction == "income"
        and _has_sub1(line)
        and _has(line, r"проданн.*сертифик|подарочн.*сертифик|оплат[аы]?\s+от\s+покуп|за\s+проданн")
        and _not_has(line, r"ошибоч|выручк|платные услуги|пластик|карточ|easypay|ерип|процент|плата за остат|займ")
    )


def _expense_bank_fee(line: LineView) -> bool:
    return line.direction == "expense" and (
        _has(
            line,
            _BANK_COMMISSION + r"|банк.?клиент|клиент.?банк|клиентбанк|"
            r"система\s+клиент.?банк|плата за обслуживание.*клиент.?банк|карт.?счет|"
            r"прием налич|зачислен.*карт|мф\s*biss|платежи\s*мб\s*\"?biss",
        )
        or (_account_in(line, "26", "97") and _has(line, r"услуги банка", fields="sub1"))
    )


_USN_TAX_MENTION = r"\bусн\b|\bраздел\s+налога\s+01201\b"
_USN_PERIOD = r"за\s+(?:(?P<quarter>[1-4]|iv|iii|ii|i)\s*(?:квартал|кв\.?)\s+)?(?P<year>20\d{2})(?:\s*(?:г\.?|год(?:а)?))?"
_USN_DIRECT_PURPOSE = re.compile(
    rf"(?:(?:уплата|оплата|перечисление)\s+)?(?:(?:единый\s+налог|единого\s+налога)\s+(?:при|по)\s+усн|налог\s+по\s+усн)"
    rf"(?:,?\s+раздел\s+налога\s+01201)?(?:,?\s+{_USN_PERIOD})\.?"
)


def _usn_tax_period(line: LineView) -> dict[str, Any] | None:
    match = _USN_DIRECT_PURPOSE.fullmatch(line.purpose_n)
    if match is None:
        return None
    period: dict[str, Any] = {"year": int(match.group("year"))}
    quarter = match.group("quarter")
    if quarter is None:
        period["kind"] = "annual"
    else:
        quarter_number = int(quarter) if quarter.isdigit() else {"i": 1, "ii": 2, "iii": 3, "iv": 4}[quarter]
        period.update({"kind": "quarterly", "quarter": quarter_number})
    return period


def _expense_usn_tax(line: LineView) -> bool:
    return line.direction == "expense" and _usn_tax_period(line) is not None


def _income_usn_tax_unresolved(line: LineView) -> bool:
    fields = "raw" if _income_revenue_scope(line) else "purpose"
    return line.direction == "income" and _has(line, _USN_TAX_MENTION, fields=fields)


def _expense_usn_tax_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _has(line, _USN_TAX_MENTION, fields="purpose")


def _expense_treasury_penalty_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _has(
        line, r"гумф|угк|райфо|казнач|мингорисполком|финансовое управление"
    ) and _has(line, r"пен[яи]|штраф|05115|13511|раздел налога.*удержан")


def _expense_fszn_penalty_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and (
        _has(line, r"фсзн") or _account_in(line, "69")
    ) and _has(line, r"пен[яи]|штраф") and not _other_tax_marker_in_purpose(line)


def _expense_fszn(line: LineView) -> bool:
    return line.direction == "expense" and _has(line, r"фсзн") and _not_has(line, r"пен[яи]|штраф")


def _expense_income_tax(line: LineView) -> bool:
    return line.direction == "expense" and _has(line, r"подоход|подох")


def _expense_belgosstrakh_penalty_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _has(
        line, r"белгосстрах|бегосстрах|страх"
    ) and _has(line, r"пен[яи]|штраф")


def _expense_belgosstrakh(line: LineView) -> bool:
    return line.direction == "expense" and _has(
        line, r"белгосстрах|бегосстрах|страховые взносы|отчисления"
    ) and _not_has(line, r"пен[яи]|штраф")


_OTHER_TAX_MARKER = (
    r"\bналог\w*\b|\bндс\b|\bн\s*\.\s*д\s*\.\s*с\.?(?!\w)|"
    r"\bнал\.|\bакц(?:из\w*|\.)|\bподоход\w*|\bподох\b"
)
_EXPLICIT_TAX_PURPOSE_MARKER = rf"{_OTHER_TAX_MARKER}|\bфсзн\b|\bусн\b|\bраздел\s+налога\s+01201\b"
_INVOICE_VAT_METADATA = re.compile(
    r"(?:\bв\s*т\.?\s*ч\.?|\bв\s+том\s+числе|\bбез)\s+(?:ндс\b|н\s*\.\s*д\s*\.\s*с\.?(?!\w))"
)
_COMMERCIAL_VAT_CONTEXT = re.compile(
    r"\b(?:оплата|плата|предоплата)\b.*\b(?:услуг|товар|работ|материал|оборуд)\w*|"
    r"\bоплата\s+от\s+покупател\w*|\b(?:проданн\w*|подарочн\w*)\s+сертификат\w*"
)
_CONFIRMED_TAX_PERIOD = r"за\s+(?:[1-4]\s*(?:квартал|кв\.?)\s+)?20\d{2}(?:\s*(?:г\.?|год(?:а)?))?"
_CONFIRMED_TAX_SUFFIX = rf"(?:,?\s+{_CONFIRMED_TAX_PERIOD})?\.?"
_CONFIRMED_FSZN_PURPOSE = re.compile(
    rf"(?:(?:уплата|оплата|перечисление)\s+)?(?:взносы|взносов)\s+фсзн{_CONFIRMED_TAX_SUFFIX}"
)
_CONFIRMED_INCOME_TAX_PURPOSE = re.compile(
    rf"(?:(?:уплата|оплата|перечисление)\s+)?(?:подоходный\s+налог|подоходного\s+налога){_CONFIRMED_TAX_SUFFIX}"
)


def _explicit_tax_purpose(line: LineView, *, full_bank_row: bool = False) -> bool:
    purpose = line.revenue_context if full_bank_row else line.purpose_n
    # VAT stated as a price component in a commercial payment is not itself a
    # tax remittance. Other tax markers in the same purpose still hold.
    if _COMMERCIAL_VAT_CONTEXT.search(purpose):
        purpose = _INVOICE_VAT_METADATA.sub("", purpose)
    return re.search(_EXPLICIT_TAX_PURPOSE_MARKER, purpose) is not None


def _confirmed_tax_purpose_without_other_subject(line: LineView) -> bool:
    purpose = line.purpose_n
    # Only a complete, single-purpose wording can bypass the generic tax
    # guard. Unknown suffixes remain unresolved even without a known marker.
    return (
        _CONFIRMED_FSZN_PURPOSE.fullmatch(purpose) is not None
        or _CONFIRMED_INCOME_TAX_PURPOSE.fullmatch(purpose) is not None
    )


def _income_explicit_tax_unresolved(line: LineView) -> bool:
    return line.direction == "income" and _explicit_tax_purpose(
        line, full_bank_row=_income_revenue_scope(line))


def _expense_explicit_tax_unresolved(line: LineView) -> bool:
    return (
        line.direction == "expense"
        and _explicit_tax_purpose(line)
        and not _confirmed_tax_purpose_without_other_subject(line)
    )


_TAX_AUTHORITY_COUNTERPARTY = re.compile(
    r"\bналогов\w*\s+инспекци\w*\b|\bинспекци\w*\s+мнс\b|\bимнс\b|"
    r"\b(?:инспекци\w*\s+)?министерств\w*\s+по\s+налогам\s+и\s+сборам\b"
)


def _tax_authority_counterparty(line: LineView) -> bool:
    return _TAX_AUTHORITY_COUNTERPARTY.search(line.sub1_n) is not None


def _income_tax_authority_unresolved(line: LineView) -> bool:
    return line.direction == "income" and (
        _tax_authority_counterparty(line) or _income_revenue_scope(line)
        and _TAX_AUTHORITY_COUNTERPARTY.search(line.revenue_context) is not None)


def _expense_tax_authority_unresolved(line: LineView) -> bool:
    return (
        line.direction == "expense"
        and _tax_authority_counterparty(line)
        and not _confirmed_tax_purpose_without_other_subject(line)
    )


def _other_tax_marker_in_purpose(line: LineView) -> bool:
    return _has(line, _OTHER_TAX_MARKER, fields="purpose")


def _explicit_fszn_only_penalty_purpose(line: LineView) -> bool:
    return (
        _has(line, r"фсзн", fields="purpose")
        and _has(line, r"пен[яи]|штраф", fields="purpose")
        and not _other_tax_marker_in_purpose(line)
    )


def _expense_legacy_tax_penalty_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and (
        _account_in(line, "68.31", "99")
        or _has(line, _OTHER_TAX_MARKER)
    ) and _has(line, r"пен[яи]|штраф") and not _explicit_fszn_only_penalty_purpose(line)


_PROPERTY_TAX_MENTION = re.compile(r"\bналог\w*\s+на\s+недвижимост\w*\b")
_PROPERTY_TAX_NAME = r"налога\s+на\s+недвижимость"
_PROPERTY_TAX_DIRECT = rf"(?:оплата|уплата|перечисление)\s+{_PROPERTY_TAX_NAME}"
_PROPERTY_TAX_PENALTY = (
    rf"(?:(?:оплата|уплата|перечисление)\s+пени|пеня)\s+по\s+налогу\s+на\s+недвижимость"
)
# Only a period, a numbered tax notice, or the tax recipient may follow the
# direct payment wording. A free-text remainder would silently accept services,
# rent, or a second tax in the same payment purpose. Notice numbers accept
# numeric segments only, so another tax cannot hide after a slash or hyphen.
_PROPERTY_TAX_PERIOD = r"за\s+(?:(?:[1-4])\s*(?:квартал|кв\.?)\s+)?20\d{2}(?:\s*(?:г\.?|год(?:а)?))?"
# _normalize applies NFKC, which folds the numero sign (№) to "no".
_PROPERTY_TAX_DOCUMENT = r"по\s+(?:уведомлению|извещению|требованию)\s+no\s*\d+(?:[-/]\d+)*(?:\s+от\s+\d{2}\.\d{2}\.\d{4})?"
_PROPERTY_TAX_RECIPIENT = r"в\s+(?:налоговую\s+инспекцию|инспекцию\s+мнс|бюджет)"
_PROPERTY_TAX_PURPOSE = re.compile(
    rf"(?:{_PROPERTY_TAX_DIRECT}|{_PROPERTY_TAX_PENALTY})"
    rf"(?:(?:,\s*|\s+)(?:{_PROPERTY_TAX_PERIOD}|{_PROPERTY_TAX_DOCUMENT}|{_PROPERTY_TAX_RECIPIENT}))*\.?"
)


def _expense_property_tax(line: LineView) -> bool:
    return line.direction == "expense" and _PROPERTY_TAX_PURPOSE.fullmatch(line.purpose_n) is not None


def _expense_property_tax_purpose_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _PROPERTY_TAX_MENTION.search(line.purpose_n) is not None


def _expense_enforcement_withholding(line: LineView) -> bool:
    return line.direction == "expense" and _has(line, r"исполнит|исп\.?\s*лист|исполн\.?\s*лист|исполнительн")


def _expense_salary_withholding_unresolved(line: LineView) -> bool:
    return (
        line.direction == "expense"
        and _has(line, r"удержан|удержание")
        and not _expense_enforcement_withholding(line)
    )


def _expense_salary(line: LineView) -> bool:
    return (
        line.direction == "expense"
        and _has(line, r"\bзп\b|зарплат|заработн")
        and _not_has(line, r"удержан|налог|взносы|страховые|исполнит|исп\.?\s*лист")
    )


def _expense_supplier_payment(line: LineView) -> bool:
    return (
        line.direction == "expense"
        and _has_sub1(line)
        and _has(
            line,
            r"за\b|оплат|предоплат|аренд|возмещ|коммун|комун|сч[её]т|с/ф|услуг|товар|"
            r"издел|оборуд|мебел|интернет|связ|охран|реклам|листов|материал|сертификат|"
            r"хостинг|домен|подписк|кассов|аппарат|дисконт|журнал|ип\s+|ооо|одо|оао|зао|сзао|чуп|сп\s+ооо",
        )
        and _not_has(
            line,
            r"пен[яи]|штраф|удержан|фсзн|подоход|белгосстрах|"
            r"бегосстрах|страховые\s+взносы|недвиж|клиент.?банк|банк.?клиент|"
            r"комис|вознаг|\bзп\b|зарплат|заработн|займ",
        )
    )


def _expense_76_1_other_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _account_in(line, "76.1")


def _expense_service_tax_unresolved(line: LineView) -> bool:
    return line.direction == "expense" and _account_in(line, "42.6")


RULES: tuple[Rule, ...] = (
    Rule(
        'blank_zero_line',
        10,
        True,
        "high",
        'blank zero line; semantic classification only, explicit target policy required.',
        {'action': 'skipCandidate'},
        _blank_zero,
        evidence=(),
    ),
    Rule(
        'income_usn_tax_needs_decision',
        15,
        True,
        "high",
        'income usn tax needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'decision': 'confirm the incoming operation and account separately before posting'},
        _income_usn_tax_unresolved,
        evidence=(),
    ),
    Rule(
        'income_explicit_tax_needs_decision',
        18,
        True,
        "high",
        'income explicit tax needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'decision': 'confirm the incoming tax-related operation and target account before posting'},
        _income_explicit_tax_unresolved,
        evidence=(),
    ),
    Rule(
        'income_tax_authority_counterparty_needs_decision',
        19,
        True,
        "high",
        'income tax authority counterparty needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'decision': 'confirm the actual receipt or refund operation and target account before posting'},
        _income_tax_authority_unresolved,
        evidence=(),
    ),
    Rule(
        'income_erroneous_or_government_transfer_needs_decision',
        20,
        True,
        "high",
        'income erroneous or government transfer needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operationCandidates': ['otherReceipt', 'buyerPayment', 'refundOrCorrection'], 'decision': 'confirm actual operation and account before posting'},
        _income_erroneous_or_government,
    ),
    Rule(
        'income_charter_fund_special_account_needs_decision',
        22,
        True,
        "medium",
        'income charter fund special account needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operationCandidates': ['otherReceipt', 'internalTransfer'], 'decision': 'confirm source of funds, target account and operation before posting'},
        _income_charter_or_special_account,
        evidence=(),
    ),
    Rule(
        'income_mixed_revenue_loan_needs_decision',
        23,
        True,
        "high",
        'income mixed revenue loan needs decision; semantic classification only, explicit target policy required.',
        {'documentCandidates': ['CashExpenseOrder', 'BankReceipt'], 'decision': 'prove revenue and loan components before selecting amounts and target operations'},
        _income_mixed_revenue_loan,
        evidence=(),
    ),
    Rule(
        'income_loan_short_term',
        24,
        False,
        "high",
        'income loan short term; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operation': 'loanReceived'},
        _income_loan_short_term,
        evidence=(),
    ),
    Rule(
        'income_loan_keyword_needs_decision',
        26,
        True,
        "high",
        'income loan keyword needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operationCandidates': ['loanReceived'], 'decision': 'confirm lender, agreement and target account before posting'},
        _income_loan_keyword_unresolved,
        evidence=(),
    ),
    Rule(
        'income_revenue_intermediary_needs_decision',
        28,
        True,
        "medium",
        'income revenue intermediary needs decision; semantic classification only, explicit target policy required.',
        {'documentCandidates': ['CashExpenseOrder', 'BankReceipt'], 'decision': 'confirm cash versus intermediary context before selecting the target operation and account'},
        _income_revenue_intermediary_unresolved,
        evidence=(),
    ),
    Rule(
        'income_card_erip_intermediary',
        30,
        False,
        "high",
        'income card erip intermediary; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operation': 'cardEripReceipt'},
        _income_card_erip,
    ),
    Rule(
        'income_cash_revenue_deposit',
        40,
        False,
        "high",
        'income cash revenue deposit; semantic classification only, explicit target policy required.',
        {'document': 'CashExpenseOrder', 'operation': 'cashRevenueDeposit', 'templateId': 'cash-revenue-rko', 'templateVersion': '2', 'additionalBankReceipt': False},
        _income_cash_deposit,
        evidence=(),
    ),
    Rule(
        'income_bank_interest',
        50,
        False,
        "high",
        'income bank interest; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operation': 'otherReceipt'},
        _income_bank_interest,
    ),
    Rule(
        'income_supplier_refund',
        60,
        False,
        "medium",
        'income supplier refund; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operation': 'supplierRefund'},
        _income_supplier_refund,
    ),
    Rule(
        'income_buyer_payment',
        70,
        False,
        "high",
        'income buyer payment; semantic classification only, explicit target policy required.',
        {'document': 'BankReceipt', 'operation': 'buyerPayment'},
        _income_buyer_payment,
    ),
    Rule(
        'expense_property_tax',
        90,
        False,
        "medium",
        'expense property tax; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'taxPayment'},
        _expense_property_tax,
        evidence=(),
    ),
    Rule(
        'expense_property_tax_purpose_needs_decision',
        95,
        True,
        "high",
        'expense property tax purpose needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['taxPayment', 'taxPenaltyPayment'], 'decision': 'resolve the complete purpose and target account before posting'},
        _expense_property_tax_purpose_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_legacy_tax_penalty_needs_decision',
        97,
        True,
        "medium",
        'expense legacy tax penalty needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['taxPenaltyPayment', 'otherWriteOff'], 'decision': 'confirm actual target account and operation before posting'},
        _expense_legacy_tax_penalty_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_fszn_penalty_needs_decision',
        98,
        True,
        "medium",
        'expense fszn penalty needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['taxPenaltyPayment', 'taxContributionPayment', 'otherWriteOff'], 'decision': 'confirm actual target account and operation before posting'},
        _expense_fszn_penalty_unresolved,
    ),
    Rule(
        'expense_usn_tax',
        99,
        False,
        "high",
        'expense usn tax; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'taxPayment'},
        _expense_usn_tax,
        evidence=(),
    ),
    Rule(
        'expense_usn_tax_period_needs_decision',
        99,
        True,
        "high",
        'expense usn tax period needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'decision': 'confirm one annual or quarterly tax period and the complete USN-only payment purpose before posting'},
        _expense_usn_tax_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_treasury_penalty_fine_withholding_needs_decision',
        99,
        True,
        "high",
        'expense treasury penalty fine withholding needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['taxPenaltyPayment', 'otherWriteOff', 'settlementWithOtherDebtorsCreditors'], 'decision': 'confirm actual target account and operation before posting'},
        _expense_treasury_penalty_unresolved,
    ),
    Rule(
        'expense_explicit_tax_needs_decision',
        99,
        True,
        "high",
        'expense explicit tax needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'decision': 'confirm the complete tax purpose, operation, account and analytics before posting'},
        _expense_explicit_tax_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_tax_authority_counterparty_needs_decision',
        99,
        True,
        "high",
        'expense tax authority counterparty needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'decision': 'confirm the complete payment purpose, operation and target account before posting'},
        _expense_tax_authority_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_bank_fee',
        100,
        False,
        "high",
        'expense bank fee; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'bankCommission'},
        _expense_bank_fee,
    ),
    Rule(
        'expense_fszn',
        140,
        False,
        "high",
        'expense fszn; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'taxContributionPayment'},
        _expense_fszn,
    ),
    Rule(
        'expense_income_tax',
        150,
        False,
        "high",
        'expense income tax; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'taxPayment'},
        _expense_income_tax,
    ),
    Rule(
        'expense_belgosstrakh_penalty_needs_decision',
        160,
        True,
        "medium",
        'expense belgosstrakh penalty needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['insuranceContributionPayment', 'taxPenaltyPayment', 'otherWriteOff'], 'decision': 'confirm actual target account and operation before posting'},
        _expense_belgosstrakh_penalty_unresolved,
    ),
    Rule(
        'expense_belgosstrakh',
        170,
        False,
        "high",
        'expense belgosstrakh; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'insuranceContributionPayment'},
        _expense_belgosstrakh,
    ),
    Rule(
        'expense_enforcement_withholding',
        200,
        False,
        "high",
        'expense enforcement withholding; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'enforcementWithholding'},
        _expense_enforcement_withholding,
    ),
    Rule(
        'expense_salary_withholding_needs_decision',
        205,
        True,
        "medium",
        'expense salary withholding needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['enforcementWithholding', 'otherWriteOff', 'settlementWithOtherDebtorsCreditors'], 'decision': 'confirm recipient, legal basis, target account and operation before posting'},
        _expense_salary_withholding_unresolved,
    ),
    Rule(
        'expense_salary_to_70_requires_payroll_statement',
        210,
        False,
        "high",
        'expense salary to 70 requires payroll statement; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'salaryByStatement'},
        _expense_salary,
    ),
    Rule(
        'expense_supplier_payment',
        220,
        False,
        "medium",
        'expense supplier payment; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operation': 'supplierPayment'},
        _expense_supplier_payment,
    ),
    Rule(
        'expense_76_1_other_needs_decision',
        230,
        True,
        "medium",
        'expense 76 1 other needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['otherWriteOff', 'settlementWithOtherDebtorsCreditors', 'supplierPayment'], 'decision': 'confirm target account and operation before posting'},
        _expense_76_1_other_unresolved,
        evidence=(),
    ),
    Rule(
        'expense_service_tax_needs_decision',
        240,
        True,
        "medium",
        'expense service tax needs decision; semantic classification only, explicit target policy required.',
        {'document': 'BankWriteOff', 'operationCandidates': ['taxPayment', 'otherWriteOff'], 'decision': 'confirm target account and operation before posting'},
        _expense_service_tax_unresolved,
        evidence=(),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify 1C 7.7 bank statement rows without live 1C writes.")
    parser.add_argument("input", type=Path, help="JSON file: source-compact shape or a list of normalized rows")
    parser.add_argument("--policy", type=Path, help="optional explicit per-rule target mapping and independent review references")
    parser.add_argument("--output", type=Path, help="Write full decisions to this JSON file")
    parser.add_argument("--summary", action="store_true", help="Print summary instead of full decisions")
    args = parser.parse_args(argv)

    document = json.loads(args.input.read_text(encoding="utf-8"))
    policy = json.loads(args.policy.read_text(encoding="utf-8")) if args.policy else None
    decisions = decisions_from_source_compact(document, policy=policy) if isinstance(document, Mapping) and "bank" in document else classify_lines(document, policy=policy)
    payload: Any = summarize(decisions) if args.summary else {"decisions": decisions, "summary": summarize(decisions)}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
