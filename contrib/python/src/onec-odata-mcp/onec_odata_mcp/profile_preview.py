"""Deterministic, offline accounting-profile classification preview.

This module does not import the OData client or queue compiler. Its output is
conceptual bookkeeping only and can never serve as a write request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


class ProfilePreviewError(ValueError):
    """The selected policy profile or preview input has an invalid shape."""


# A positive match is only a reason for review. This is intentionally a short
# warning list, not a general Russian-language payment classifier.
_FINE_OR_PENALTY = re.compile(
    r"(?<!\w)(?:пен(?:я|и|ей|ю|ям|ями|ях|\.)|штраф\w*|штр\.|неустойк\w*|penalt(?:y|ies)|fine)(?!\w)",
    re.IGNORECASE,
)
_COMMISSION_PHRASE = r"(?:комисси\w*|вознаграждени\w*)(?:\s+банк\w*)?"
_PAYROLL_PHRASE = r"(?:(?:выплат\w*|перечислени\w*)\s+)?(?:зарплат\w*|заработн\w*\s+плат\w*)"
_MIXED_PAYROLL = re.compile(
    rf"(?<!\w)(?:{_COMMISSION_PHRASE}\s+и\s+{_PAYROLL_PHRASE}"
    rf"|{_PAYROLL_PHRASE}\s+и\s+{_COMMISSION_PHRASE})(?!\w)",
    re.IGNORECASE,
)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_decimal(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _canonical_sha256(record: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfilePreviewError("selected policy record cannot be validated") from exc
    return hashlib.sha256(canonical).hexdigest()


def _selected_profile(policy: Mapping[str, Any], selection: Mapping[str, Any], reviewed_contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """Check independently retained profile/rule hashes; hashes are not authority."""
    if not isinstance(selection, Mapping) or set(selection) != {"id", "version"}:
        raise ProfilePreviewError("select a profile using exact id and integer version")
    if not _text(selection.get("id")) or type(selection.get("version")) is not int:
        raise ProfilePreviewError("profile id/version are invalid")
    if not isinstance(policy, Mapping) or not isinstance(policy.get("profiles"), list) or not isinstance(policy.get("rules"), list):
        raise ProfilePreviewError("policy profiles or rules are unavailable")
    profiles = [p for p in policy["profiles"] if p.get("id") == selection["id"] and p.get("version") == selection["version"]]
    if len(profiles) != 1:
        raise ProfilePreviewError("selected profile id/version is absent or ambiguous")
    profile = profiles[0]
    pin_key = str(selection["id"]) + ":" + str(selection["version"])
    if _canonical_sha256(profile) != reviewed_contract.get("profiles", {}).get(pin_key):
        raise ProfilePreviewError("selected policy contract changed; independent review required")
    if profile.get("policy_status") != "declared" or profile.get("odata_mapping", {}).get("status") != "unknown":
        raise ProfilePreviewError("profile status or OData boundary differs from this preview contract")
    rules = [rule for rule in policy["rules"] if rule.get("id") == profile.get("policy_rule_id")]
    if len(rules) != 1:
        raise ProfilePreviewError("linked policy rule is absent or ambiguous")
    rule = rules[0]
    if rule.get("status") != "declared" or _canonical_sha256(rule) != reviewed_contract.get("rules", {}).get(rule["id"]):
        raise ProfilePreviewError("linked policy rule changed; independent review required")
    if profile.get("kind") not in {"bank_commission", "contribution_split"}:
        raise ProfilePreviewError("unsupported profile kind")
    accounts = profile.get("expected_accounts")
    count = 1 if profile["kind"] == "bank_commission" else 2
    if not isinstance(accounts, list) or len(accounts) != count or any(set(a) != {"debit", "credit"} or any(not _text(v) for v in a.values()) for a in accounts):
        raise ProfilePreviewError("explicit account mapping required")
    if profile["kind"] == "contribution_split":
        ratio = profile.get("secondary_share")
        if not isinstance(ratio, list) or len(ratio) != 2 or any(type(v) is not int or v <= 0 for v in ratio) or ratio[0] >= ratio[1]:
            raise ProfilePreviewError("secondary_share requires positive numerator smaller than denominator")
    if not isinstance(profile.get("required_source_facts"), list) or not profile["required_source_facts"]:
        raise ProfilePreviewError("required source facts must be explicit")
    return profile


def _row_preview(profile: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping) or not _text(row.get("source_id")):
        raise ProfilePreviewError("each row needs a nonempty source_id")
    facts = row.get("facts")
    evidence = row.get("source_evidence")
    if not isinstance(facts, Mapping) or not isinstance(evidence, Mapping):
        raise ProfilePreviewError("each row needs facts and source_evidence objects")
    required = profile["required_source_facts"]
    missing = [key for key in required if key not in facts or facts[key] is None or (isinstance(facts[key], str) and not facts[key].strip())]
    missing += [f"source_evidence.{key}" for key in required if not _text(evidence.get(key))]
    ambiguous: list[str] = []
    basis = {key: {"value": facts[key], "source": evidence[key]} for key in required
             if key in facts and facts[key] is not None and _text(evidence.get(key))}
    if "source_date" in facts and "source_date" not in missing:
        try:
            if not isinstance(facts["source_date"], str) or date.fromisoformat(facts["source_date"]).isoformat() != facts["source_date"]:
                raise ValueError
        except ValueError:
            ambiguous.append("source_date must be an ISO calendar date")
    if facts.get("in_current_migration") is not True and "in_current_migration" not in missing:
        ambiguous.append("source period is outside or not verified for the current migration")
    if "payment_purpose" not in missing and not _text(facts.get("payment_purpose")):
        ambiguous.append("payment_purpose must contain source text")
    if facts.get("payer_recipient_checked") is not True and "payer_recipient_checked" not in missing:
        ambiguous.append("payer/recipient context is not verified")
    selector = profile["selector"]
    if "operation_class" not in missing and facts.get("operation_class") not in selector["allowed_values"]:
        ambiguous.append("operation_class does not match the explicitly selected profile")
    planned: dict[str, Any] = {"document_kind": profile["conceptual_document"]["kind"],
                               "fields": {}, "expected_accounts": []}
    if profile["kind"] == "bank_commission":
        if facts.get("cash_direction") != "outgoing" and "cash_direction" not in missing:
            ambiguous.append("commission must be an outgoing bank payment")
        if facts.get("separate_payment") is not True and "separate_payment" not in missing:
            ambiguous.append("commission must be separate from an adjacent payroll payment")
        if _text(facts.get("payment_purpose")) and _MIXED_PAYROLL.search(facts["payment_purpose"]):
            ambiguous.append("payment_purpose explicitly combines commission and payroll payout")
        amount = _positive_decimal(facts.get("amount"))
        if "amount" not in missing and amount is None:
            ambiguous.append("amount must be a positive decimal string")
        planned["fields"] = {"operation": "Прочее списание", "cost_item": "Комиссия банку",
                             "source_date": facts.get("source_date"), "amount": facts.get("amount")}
        if amount is not None:
            planned["expected_accounts"] = [{**profile["expected_accounts"][0], "amount": str(amount)}]
    else:
        if facts.get("is_fine_or_penalty") is not False and "is_fine_or_penalty" not in missing:
            ambiguous.append("fines and penalties are excluded from the ordinary FSZN profile")
        if _text(facts.get("payment_purpose")) and _FINE_OR_PENALTY.search(facts["payment_purpose"]):
            ambiguous.append("payment_purpose mentions a possible fine or penalty; reconcile source classification")
        if facts.get("counterparty_verified") is not True and "counterparty_verified" not in missing:
            ambiguous.append("target counterparty is not verified")
        amount = facts.get("amount_whole_rub")
        if type(amount) is not int or amount <= 0:
            if "amount_whole_rub" not in missing:
                ambiguous.append("amount_whole_rub must be a positive integer")
            amount = None
        planned["fields"] = {"operation": "Перечисление налога", "payment_type": "Налог",
                             "source_date": facts.get("source_date"), "total_whole_rub": amount}
        if amount is not None:
            numerator, denominator = profile["secondary_share"]
            pension, remainder = divmod(amount * numerator, denominator)
            if 2 * remainder >= denominator:
                pension += 1
            social = amount - pension
            planned["fields"].update({"primary_whole_rub": social, "secondary_whole_rub": pension})
            planned["expected_accounts"] = [
                {**profile["expected_accounts"][0], "amount_whole_rub": social},
                {**profile["expected_accounts"][1], "amount_whole_rub": pension},
            ]
    technical_stop = "exact OData fields, literals, references and Post behavior are unknown"
    return {"source_id": row["source_id"], "selected_profile": {"id": profile["id"], "version": profile["version"]},
            "source_basis": basis, "planned_concept": planned,
            "missing_facts": missing, "ambiguous_facts": ambiguous,
            "stop_reasons": missing + ambiguous + [technical_stop],
            "status": "incomplete" if missing or ambiguous else "review_required"}


def preview_batch(policy: Mapping[str, Any], selection: Mapping[str, Any],
                  rows: Sequence[Mapping[str, Any]], *, reviewed_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Preview one explicitly selected profile across independent prepared rows."""
    profile = _selected_profile(policy, selection, reviewed_contract)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ProfilePreviewError("rows must contain 1..50 prepared items")
    ids = [row.get("source_id") if isinstance(row, Mapping) else None for row in rows]
    if any(not _text(source_id) for source_id in ids):
        raise ProfilePreviewError("each row needs a nonempty source_id")
    if len(ids) != len(set(ids)):
        raise ProfilePreviewError("source_id values must be unique")
    return {"selected_profile": {"id": profile["id"], "version": profile["version"]},
            "policy_rule_id": profile["policy_rule_id"], "policy_status": profile["policy_status"],
            "technical_mapping_status": "unknown", "rows": [_row_preview(profile, row) for row in rows]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline classification-only profile preview; never prepares write arguments")
    parser.add_argument("--policy", required=True, help="operator-selected accounting policy JSON")
    parser.add_argument("--reviewed-contract", required=True, help="independently retained profile/rule hashes")
    parser.add_argument("--input", required=True, help="private JSON with profile {id,version} and rows")
    parser.add_argument("--output", required=True, help="new private result JSON path (must not exist)")
    args = parser.parse_args(argv)
    try:
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        prepared = json.loads(Path(args.input).read_text(encoding="utf-8"))
        reviewed_contract = json.loads(Path(args.reviewed_contract).read_text(encoding="utf-8"))
        result = preview_batch(policy, prepared["profile"], prepared["rows"], reviewed_contract=reviewed_contract)
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(2, "profile preview error: invalid policy or input\n")
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(2, "profile preview error: cannot create exclusive private output\n")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, TypeError):
        try:
            os.unlink(args.output)
        except OSError:
            pass
        parser.exit(2, "profile preview error: cannot write private output\n")
    counts = Counter(row["status"] for row in result["rows"])
    print(f"preview saved: rows={len(result['rows'])} review_required={counts['review_required']} incomplete={counts['incomplete']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
