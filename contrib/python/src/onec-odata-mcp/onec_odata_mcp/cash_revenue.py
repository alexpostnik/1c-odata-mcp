"""Offline RKO draft for owner-declared bank revenue, with no date-range cutoff.

PROD observed fields are a candidate template, not proof of TEST requiredness
or authority. The output deliberately cannot be passed to a document queue.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal
from datetime import date, datetime
from typing import Any, Mapping
from uuid import UUID


ENTITY_SET = "Document_РасходныйКассовыйОрдер"
OPERATION = "ВзносНаличнымиВБанк"
_REFERENCE_FIELDS = {
    "organization": "Организация_Key",
    "currency": "ВалютаДокумента_Key",
    "cash_account": "СчетКасса_Key",
    "bank_account": "СчетУчетаРасчетовСКонтрагентом_Key",
    "organization_bank_account": "СчетОрганизации_Key",
}
_REQUIRED = {"source_id", "date", "amount", "purpose", "operation_class",
             "source_links", "overlap_evidence", "currency_unit_evidence"}
_REQUIRED.update({"bank_direction", "bank_account_correlation_evidence"})
_OPTIONAL = {"attachment", "cash_symbols", "bank_row_raw", "printed_credit", "printed_debit",
             "printed_debit_observation", "income", "expense", "cash_symbol_components"}
_REVENUE_WORD = re.compile(r"\b(?:выручк(?:а|и|у|е|ой|ою|ам|ами|ах)|выручек)\b", re.IGNORECASE)
# Reuse the classifier's accepted generic bank-fee vocabulary as a hold only.
_BANK_COMMISSION = re.compile(r"комис|ком\.?\s*воз|вознаг|возн\.")
_USN_TAX_MENTION = re.compile(r"\bусн\b|\bраздел\s+налога\s+01201\b")
_TAX_MARKER = re.compile(
    r"\bналог\w*\b|\bндс\b|\bн\s*\.\s*д\s*\.\s*с\.?(?!\w)|"
    r"\bнал\.|\bакц(?:из\w*|\.)|\bподоход\w*|\bподох\b|\bфсзн\b|\bусн\b|"
    r"\bраздел\s+налога\s+01201\b"
)
_INVOICE_VAT_METADATA = re.compile(
    r"(?:\bв\s*т\.?\s*ч\.?|\bв\s+том\s+числе|\bбез)\s+"
    r"(?:ндс\b|н\s*\.\s*д\s*\.\s*с\.?(?!\w))"
)
_COMMERCIAL_VAT_CONTEXT = re.compile(
    r"\b(?:оплата|плата|предоплата)\b.*\b(?:услуг|товар|работ|материал|оборуд)\w*|"
    r"\bоплата\s+от\s+покупател\w*|\b(?:проданн\w*|подарочн\w*)\s+сертификат\w*"
)
_TAX_AUTHORITY = re.compile(
    r"\bналогов\w*\s+инспекци\w*\b|\bинспекци\w*\s+мнс\b|\bимнс\b|"
    r"\b(?:инспекци\w*\s+)?министерств\w*\s+по\s+налогам\s+и\s+сборам\b"
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


def bank_row_in_rko_scope(source_date: str, purpose: str, direction: str,
                          *, cash_symbols: Any = None, bank_row_raw: Any = None) -> bool:
    """Exact owner selector; bank name/account and legacy RKO do not select rows.

    Direction is the bank statement column: credit means a bank receipt.
    This checks eligibility only, never identity, currency or write authority.
    """
    if not isinstance(source_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?", source_date
    ):
        return False
    try:
        datetime.fromisoformat(source_date)
    except ValueError:
        return False
    return (direction == "credit" and isinstance(purpose, str)
            and (_REVENUE_WORD.search(purpose) is not None
                 or has_cash_symbol_2004(cash_symbols, bank_row_raw)
                 or has_cash_symbol_2004(None, purpose)))


class CashRevenueError(ValueError):
    """Missing or conflicting facts hold this event only."""


def rko_cleanup_candidates(
    starting_set: list[Mapping[str, Any]], eligible_source_ids: list[str],
    source_results: list[Mapping[str, Any]], proofs: Mapping[str, Any],
    *, period_start: date, period_end: date,
) -> dict[str, Any]:
    """Offline final-cleanup gate, never OData arguments or write authority.

    The private starting set is frozen before Unpost. Every selected source
    must have a successful exact-link/Post/readback result before any remaining
    starting document can be considered. Unknown claims/holds stop cleanup.
    This is the owner's period correction, separate from duplicate allowance.
    """
    if type(period_start) is not date or type(period_end) is not date or period_start > period_end:
        raise CashRevenueError("cleanup requires an explicit valid inclusive period")
    required_proofs = {'full_starting_set', 'full_source_coverage', 'same_test_ledger',
                       'claim_continuity', 'held_identity_resolved'}
    if any(not isinstance(value, list) for value in (starting_set, eligible_source_ids, source_results)):
        raise CashRevenueError('cleanup sets/results must be lists')
    if not isinstance(proofs, Mapping) or set(proofs) != required_proofs:
        raise CashRevenueError('cleanup requires exact continuity/coverage proofs')
    gaps = [key for key in sorted(required_proofs) if proofs[key] is not True]
    ids = [_text(value, 'eligible source id') for value in eligible_source_ids]
    if len(set(ids)) != len(ids):
        raise CashRevenueError('duplicate eligible source id')
    starts = {}
    for row in starting_set:
        fields = {'target_id', 'date', 'unpost_verified', 'posted',
                  'deletion_mark', 'fresh_version', 'held'}
        if not isinstance(row, Mapping) or set(row) != fields:
            raise CashRevenueError('starting set requires exact fresh outcome fields')
        ident = _text(row['target_id'], 'target id')
        try:
            day = date.fromisoformat(row['date'])
        except (ValueError, TypeError):
            raise CashRevenueError('starting document date invalid') from None
        if not period_start <= day <= period_end or ident in starts:
            raise CashRevenueError('starting set must be unique and within owner period')
        starts[ident] = row
        if row['unpost_verified'] is not True:
            gaps.append('starting_unpost_incomplete')
        if row['held'] is not False:
            gaps.append('held_starting_document')
    results = {}
    used = set()
    for row in source_results:
        fields = {'source_id', 'target_id', 'status', 'exact_source_target', 'post_readback_verified'}
        if not isinstance(row, Mapping) or set(row) != fields:
            raise CashRevenueError('source result requires exact observed outcome fields')
        ident = _text(row['source_id'], 'source id')
        if ident in results or ident not in ids:
            raise CashRevenueError('duplicate or unselected source result')
        results[ident] = row
        if (row['status'] != 'loaded_verified' or row['exact_source_target'] is not True
                or row['post_readback_verified'] is not True):
            gaps.append('source_pass_incomplete')
        else:
            target_id = _text(row['target_id'], 'successful target id')
            if target_id in used:
                raise CashRevenueError('different source rows cannot share one RKO')
            used.add(target_id)
    if set(results) != set(ids):
        gaps.append('source_pass_incomplete')
    candidates = []
    for ident, row in starts.items():
        if ident in used:
            continue
        if row['posted'] is not False or row['deletion_mark'] is not False or row['fresh_version'] is not True:
            gaps.append('remaining_document_fresh_unposted_check')
        candidates.append(ident)
    return {'status': 'blocked' if gaps else 'review_required', 'can_apply': False,
            'candidate_target_ids': [] if gaps else sorted(candidates),
            'gaps': sorted(set(gaps)), 'scope': 'cash_revenue_rko_period_cleanup'}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CashRevenueError(f"{label} requires a nonempty source fact/evidence")
    return value


def _printed_amount(value: Any, label: str, *, blank_zero: bool = False) -> Decimal:
    if blank_zero and value == "":
        return Decimal(0)
    amount = _printed_money_value(value)
    if amount is None:
        raise CashRevenueError(f"{label} requires a complete printed decimal amount")
    return amount


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


def build_rko_draft(
    facts: Mapping[str, Any], references: Mapping[str, str],
    private_config: Mapping[str, str],
) -> dict[str, Any]:
    """Prepare one eligible bank row; no discovery, dedup, claim or write.

    private_config.depositor is the exact owner-approved value, supplied locally.
    Never put that value or real bank account numbers into repository fixtures.

    A factual reviewer must verify the event linkage and target reference
    meanings outside this pure function. Nonempty evidence refs are pointers,
    not proof that an underlying source has been independently accepted.
    """
    if not isinstance(facts, Mapping) or not _REQUIRED <= set(facts) or set(facts) - _REQUIRED - _OPTIONAL:
        raise CashRevenueError("cash event requires the exact supported facts")
    if facts["operation_class"] != "cash_revenue_bank_deposit":
        raise CashRevenueError("only cash revenue bank deposits belong to this template")
    purpose = facts["purpose"]
    if not isinstance(purpose, str):
        raise CashRevenueError("purpose must preserve the source string, including an observed blank")
    raw_row = facts.get("bank_row_raw")
    if raw_row is not None and not isinstance(raw_row, str):
        raise CashRevenueError("bank_row_raw must preserve the source string")
    context = re.sub(r"\s+", " ", (purpose + " " + (raw_row or "")).casefold().replace("ё", "е"))
    if _USN_TAX_MENTION.search(context):
        raise CashRevenueError("incoming USN or tax-section receipt needs separate classification")
    tax_context = _INVOICE_VAT_METADATA.sub("", context) if _COMMERCIAL_VAT_CONTEXT.search(context) else context
    if _TAX_MARKER.search(tax_context) or _TAX_AUTHORITY.search(context):
        raise CashRevenueError("tax-related receipt or tax authority needs separate classification")
    if re.search(r"ошибоч|финансовое управление|мингорисполком|гумф|угк|казнач|райфо|процент|плата за остат", context):
        raise CashRevenueError("erroneous, government or bank-interest receipt is not cash revenue")
    if _BANK_COMMISSION.search(context):
        raise CashRevenueError("bank commission or reward in full source context requires separate evidence")
    intermediary_context = re.sub(r"\bэлектронн\w*\s+документ\w*\b", "", context)
    if re.search(r"\b(?:займ\w*|за[её]м\w*|кредит\w*)\b|пластик|карточ|ерип|easypay|электронн|"
                 r"денежн\w*\s+перевод|операций:.*комиссия|возврат\w*\s+комисси\w*", intermediary_context, flags=re.IGNORECASE):
        raise CashRevenueError("mixed loan, card/ERIP or unresolved intermediary purpose requires separate evidence; never take full row amount")
    source_id = _text(facts["source_id"], "source_id")
    raw_date = _text(facts["date"], "date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw_date):
        raise CashRevenueError("date needs an evidenced OData DateTime; no invented time")
    if not bank_row_in_rko_scope(raw_date, purpose, facts["bank_direction"],
                                 cash_symbols=facts.get("cash_symbols"),
                                 bank_row_raw=facts.get("bank_row_raw")):
        raise CashRevenueError("requires a valid bank credit date and word forms of выручка or exact cash symbol 2004")
    amount = facts["amount"]
    try:
        valid_amount = type(amount) in (int, float) and math.isfinite(amount) and amount > 0
    except OverflowError:
        valid_amount = False
    if not valid_amount:
        raise CashRevenueError("amount needs a positive finite JSON number in the verified currency unit")
    credit = _printed_amount(facts.get("printed_credit"), "printed_credit")
    if "printed_debit" not in facts:
        raise CashRevenueError("printed_debit needs an observed blank or zero cell")
    if facts["printed_debit"] is None and _proven_blank_debit(facts.get("printed_debit_observation"), source_id):
        debit = Decimal(0)
    else:
        debit = _printed_amount(facts["printed_debit"], "printed_debit", blank_zero=True)
    if credit <= 0 or debit != 0 or credit != Decimal(str(amount)):
        raise CashRevenueError("cash receipt needs positive printed credit, blank or zero debit and exact amount")
    if not _symbol_projections_agree(facts):
        raise CashRevenueError("component_unknown: cash-symbol code projections disagree or are invalid")
    component_status = _symbol_component_status(credit, purpose, raw_row,
                                                components=facts.get("cash_symbol_components", ()))
    if component_status == "unknown":
        raise CashRevenueError("component_unknown: printed cash-symbol amount is unresolved; exact allocation needs separate evidence")
    if component_status == "conflict":
        raise CashRevenueError("printed cash-symbol component conflicts with whole receipt; allocation needs separate evidence")
    for name in ("income", "expense"):
        if name in facts:
            asserted = _printed_amount(facts[name], name, blank_zero=name == "expense")
            if asserted != (credit if name == "income" else Decimal(0)):
                raise CashRevenueError(f"{name} conflicts with printed cash receipt amount")
    links = facts["source_links"]
    if not isinstance(links, Mapping) or "bank" not in links or set(links) - {"bank", "rko"}:
        raise CashRevenueError("source_links requires bank provenance; RKO alone cannot select a row")
    for kind, link in links.items():
        _text(link, f"source_links.{kind}")
    for field in ("overlap_evidence", "currency_unit_evidence", "bank_account_correlation_evidence"):
        _text(facts[field], field)
    if not isinstance(references, Mapping) or set(references) != set(_REFERENCE_FIELDS):
        raise CashRevenueError("all five locally verified target references are required")
    for name, value in references.items():
        try:
            valid = isinstance(value, str) and str(UUID(value)) == value.lower() and UUID(value).int != 0
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise CashRevenueError(f"{name} needs a nonzero target GUID; never reuse a PROD key")
    if references["cash_account"].lower() == references["bank_account"].lower():
        raise CashRevenueError("cash and bank must be distinct verified accounts")
    if not isinstance(private_config, Mapping) or set(private_config) != {"depositor", "depositor_employee_evidence"}:
        raise CashRevenueError("private configuration requires owner-approved depositor and existing employee evidence")
    depositor = _text(private_config["depositor"], "private_config.depositor")
    _text(private_config["depositor_employee_evidence"], "private_config.depositor_employee_evidence")
    payload = {"Date": raw_date, "ВидОперации": OPERATION,
               "СуммаДокумента": amount, "Основание": purpose,
               "Комментарий": purpose, "Выдать": depositor,
               **{field: references[name] for name, field in _REFERENCE_FIELDS.items()}}
    for name, field in (("attachment", "Приложение"),):
        if name in facts:
            if not isinstance(facts[name], str):
                raise CashRevenueError(f"{name} must preserve the source string")
            payload[field] = facts[name]
    return {
        "template": {"id": "cash-revenue-rko", "version": 2},
        "source_id": source_id, "source_links": dict(links),
        "status": "review_required", "can_apply": False,
        "entity_set": ENTITY_SET, "payload_draft": payload,
        "movement_expectation_draft": {
            "register": "AccountingRegister_Хозрасчетный",
            "recorderType": "StandardODATA." + ENTITY_SET,
            "amountField": "Сумма", "debitAccountField": "AccountDr_Key",
            "creditAccountField": "AccountCr_Key", "activeField": "Active",
            "activeValue": True, "allowExtraRows": False, "tolerance": 0,
            "rows": [{"amount": amount, "debit": references["bank_account"],
                      "credit": references["cash_account"]}],
        },
        "missing_facts": ["accepted exact TEST operation/reference mapping and business requiredness",
                          "accepted exact v2 TEST payload; existing employee and string field metadata are verified",
                          "verified TEST Create/Post parameters and outcome",
                          "same registration/receipt ledger/claim continuity and assigned writer"],
    }
