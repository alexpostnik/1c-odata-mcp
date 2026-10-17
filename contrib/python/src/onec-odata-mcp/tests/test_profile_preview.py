"""Synthetic, offline scenarios for explicitly selected accounting profiles."""
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from onec_odata_mcp import profile_preview
from onec_odata_mcp.profile_preview import ProfilePreviewError, preview_batch as _preview_batch

POLICY_PATH = Path(__file__).resolve().parents[3] / "examples/synthetic-policy.json"
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
REVIEW_PATH = POLICY_PATH.with_name("synthetic-reviewed-contract.json")
REVIEW = json.loads(REVIEW_PATH.read_text())
def preview_batch(*args):
    return _preview_batch(*args, reviewed_contract=REVIEW)


def prepared(source_id, facts):
    return {"source_id": source_id, "facts": facts,
            "source_evidence": {key: f"synthetic-source:{source_id}:{key}" for key in facts}}


def commission(amount="12.50"):
    return {"source_date": "2011-04-05", "in_current_migration": True,
            "operation_class": "bank_commission", "cash_direction": "outgoing",
            "separate_payment": True, "amount": amount, "payment_purpose": "Synthetic bank fee",
            "payer_recipient_checked": True}


def fszn(amount=175):
    return {"source_date": "2011-04-06", "in_current_migration": True,
            "operation_class": "ordinary_fszn_contribution", "is_fine_or_penalty": False,
            "amount_whole_rub": amount, "payment_purpose": "Synthetic ordinary FSZN contribution",
            "payer_recipient_checked": True, "counterparty_verified": True}


def assert_no_write_shape(result):
    raw = json.dumps(result, ensure_ascii=False)
    for token in ('"payload"', '"entity_set"', '"items"', '"writeEnabled"',
                  '"dry_run"', '"confirm"', '"operation_id"'):
        assert token not in raw


def test_commission_batch_reuses_one_profile_but_never_becomes_executable():
    result = preview_batch(POLICY, {"id": "bank-commission", "version": 1},
                           [prepared("synthetic-1", commission()), prepared("synthetic-2", commission("8.25"))])
    assert len(result["rows"]) == 2
    assert all(row["status"] == "review_required" for row in result["rows"])
    assert result["rows"][0]["planned_concept"]["expected_accounts"] == [
        {"debit": "expense", "credit": "bank", "amount": "12.50"}]
    assert result["rows"][0]["source_basis"]["operation_class"]["source"].startswith("synthetic-source:")
    assert_no_write_shape(result)


def test_fszn_whole_ruble_split_and_fines_stop_independent_rows():
    penalty = fszn(35)
    penalty["is_fine_or_penalty"] = True
    result = preview_batch(POLICY, {"id": "contribution-split", "version": 1},
                           [prepared("ordinary", fszn()), prepared("fine", penalty),
                            prepared("missing", {k: v for k, v in fszn().items() if k != "counterparty_verified"})])
    ordinary, fine, missing = result["rows"]
    assert ordinary["status"] == "review_required"
    assert ordinary["planned_concept"]["expected_accounts"] == [
        {"debit": "primary", "credit": "bank", "amount_whole_rub": 150},
        {"debit": "secondary", "credit": "bank", "amount_whole_rub": 25}]
    assert fine["status"] == "incomplete"
    assert any("fines and penalties" in reason for reason in fine["stop_reasons"])
    assert missing["missing_facts"] == ["counterparty_verified", "source_evidence.counterparty_verified"]
    assert_no_write_shape(result)


def test_free_text_does_not_select_profile_and_evidence_is_required():
    facts = commission()
    facts["operation_class"] = "unknown"
    row = prepared("ambiguous", facts)
    row["source_evidence"].pop("payment_purpose")
    result = preview_batch(POLICY, {"id": "bank-commission", "version": 1}, [row])
    assert result["rows"][0]["status"] == "incomplete"
    assert "source_evidence.payment_purpose" in result["rows"][0]["missing_facts"]
    assert any("operation_class" in reason for reason in result["rows"][0]["ambiguous_facts"])
    with pytest.raises(ProfilePreviewError):
        preview_batch(POLICY, {"id": "unknown", "version": 1}, [row])


@pytest.mark.parametrize("profile_id", ["bank-commission", "contribution-split"])
def test_only_reviewed_v1_contract_is_supported(profile_id):
    row = prepared("synthetic", commission() if profile_id.startswith("bank-") else fszn())
    with pytest.raises(ProfilePreviewError, match="id/version"):
        preview_batch(POLICY, {"id": profile_id, "version": 2}, [row])
    changed = deepcopy(POLICY)
    selected = next(profile for profile in changed["profiles"] if profile["id"] == profile_id)
    selected["selector"]["allowed_values"].append("unreviewed_class")
    with pytest.raises(ProfilePreviewError, match="contract changed"):
        preview_batch(changed, {"id": profile_id, "version": 1}, [row])


@pytest.mark.parametrize("profile_id,rule_id,change", [
    ("bank-commission", "commission", ("debit_account", "44")),
    ("contribution-split", "contribution", ("ratio", "33:2 for all migration years")),
    ("contribution-split", "contribution", ("status", "unknown")),
])
def test_linked_rule_semantics_and_status_are_pinned(profile_id, rule_id, change):
    changed = deepcopy(POLICY)
    rule = next(rule for rule in changed["rules"] if rule["id"] == rule_id)
    rule[change[0]] = change[1]
    facts = commission() if profile_id.startswith("bank-") else fszn()
    with pytest.raises(ProfilePreviewError, match="linked policy rule changed"):
        preview_batch(changed, {"id": profile_id, "version": 1}, [prepared("synthetic", facts)])


def test_linked_fszn_split_part_cannot_change_silently():
    changed = deepcopy(POLICY)
    rule = next(rule for rule in changed["rules"] if rule["id"] == "contribution")
    rule["parts"][0]["amount_fraction"] = "33/35"
    with pytest.raises(ProfilePreviewError, match="linked policy rule changed"):
        preview_batch(changed, {"id": "contribution-split", "version": 1},
                      [prepared("synthetic", fszn())])


@pytest.mark.parametrize("remove", [False, True])
def test_linked_rule_must_be_unique_and_present(remove):
    changed = deepcopy(POLICY)
    rule = next(rule for rule in changed["rules"] if rule["id"] == "commission")
    if remove:
        changed["rules"].remove(rule)
    else:
        changed["rules"].append(deepcopy(rule))
    with pytest.raises(ProfilePreviewError, match="absent or ambiguous"):
        preview_batch(changed, {"id": "bank-commission", "version": 1},
                      [prepared("synthetic", commission())])


@pytest.mark.parametrize("purpose", [
    "Пени за несвоевременную уплату взносов ФСЗН",
    "Уплата пеней ФСЗН", "Пеня по взносам", "Пен. ФСЗН",
    "Штраф за просрочку", "Штр. по взносам", "Неустойка ФСЗН",
])
def test_fszn_purpose_warning_overrides_false_penalty_flag(purpose):
    facts = fszn()
    facts["payment_purpose"] = purpose
    result = preview_batch(POLICY, {"id": "contribution-split", "version": 1},
                           [prepared("synthetic", facts)])
    row = result["rows"][0]
    assert row["status"] == "incomplete"
    assert any("payment_purpose" in reason for reason in row["ambiguous_facts"])
    assert_no_write_shape(result)


def test_non_marker_pension_word_does_not_trigger_penalty_warning():
    facts = fszn()
    facts["payment_purpose"] = "Пенсионные взносы ФСЗН за апрель"
    row = preview_batch(POLICY, {"id": "contribution-split", "version": 1},
                        [prepared("synthetic", facts)])["rows"][0]
    assert row["status"] == "review_required"


@pytest.mark.parametrize("purpose", [
    "Комиссия и выплата зарплаты",
    "Комиссия банка и выплата зарплаты",
    "Комиссия и зарплата",
    "Выплата зарплаты и комиссия банка",
    "Зарплата и комиссия",
])
def test_commission_flags_explicit_mixed_payroll_but_not_fee_for_payroll_transfer(purpose):
    mixed = commission()
    mixed["payment_purpose"] = purpose
    fee = commission()
    fee["payment_purpose"] = "Комиссия банка за перечисление зарплаты"
    rows = preview_batch(POLICY, {"id": "bank-commission", "version": 1},
                         [prepared("mixed", mixed), prepared("fee", fee)])["rows"]
    assert rows[0]["status"] == "incomplete"
    assert any("payment_purpose" in reason for reason in rows[0]["ambiguous_facts"])
    assert rows[1]["status"] == "review_required"


def test_cli_writes_exclusive_private_file_without_disclosing_row_data(tmp_path):
    private_id = "private-source-id"
    facts = fszn()
    facts["payment_purpose"] = "private-purpose-text"
    prepared_row = prepared(private_id, facts)
    prepared_row["source_evidence"]["source_date"] = "private-evidence-text"
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"profile": {"id": "contribution-split", "version": 1},
                                  "rows": [prepared_row]}), encoding="utf-8")
    output = tmp_path / "result.json"
    command = [sys.executable, "-m", "onec_odata_mcp.profile_preview", "--policy", str(POLICY_PATH), "--reviewed-contract", str(REVIEW_PATH),
               "--input", str(source), "--output", str(output)]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    completed = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert "rows=1" in completed.stdout
    assert not any(value in completed.stdout for value in (private_id, "private-purpose-text", "private-evidence-text"))
    assert output.stat().st_mode & 0o777 == 0o600
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["rows"][0]["source_id"] == private_id
    assert_no_write_shape(result)
    second = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    assert second.returncode == 2
    assert second.stdout == ""
    assert "cannot create exclusive private output" in second.stderr
    assert output.read_text(encoding="utf-8") == json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def test_cli_removes_partial_private_output_after_write_failure(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"profile": {"id": "bank-commission", "version": 1},
                                  "rows": [prepared("private-id", commission())]}), encoding="utf-8")
    output = tmp_path / "result.json"

    def fail_write(*_args, **_kwargs):
        raise OSError("private-write-detail")

    monkeypatch.setattr(profile_preview.json, "dump", fail_write)
    with pytest.raises(SystemExit) as stopped:
        profile_preview.main(["--policy", str(POLICY_PATH), "--reviewed-contract", str(REVIEW_PATH), "--input", str(source), "--output", str(output)])
    captured = capsys.readouterr()
    assert stopped.value.code == 2
    assert captured.out == ""
    assert "private-id" not in captured.err
    assert "private-write-detail" not in captured.err
    assert not output.exists()


def test_split_is_independent_of_callers_decimal_context():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 6
        result = preview_batch(POLICY, {"id": "contribution-split", "version": 1}, [prepared("synthetic-large", fszn(10_000_000))])
    fields = result["rows"][0]["planned_concept"]["fields"]
    assert fields["secondary_whole_rub"] == 1_428_571
    assert fields["primary_whole_rub"] == 8_571_429


@pytest.mark.parametrize("amount,secondary", [(1,1),(2,1),(3,2),(10_000_001,5_000_001)])
def test_split_half_up_is_exact_for_explicit_operator_ratio(amount, secondary):
    from decimal import localcontext
    policy = deepcopy(POLICY)
    profile = next(p for p in policy["profiles"] if p["id"] == "contribution-split")
    profile["secondary_share"] = [1, 2]
    reviewed = deepcopy(REVIEW)
    reviewed["profiles"]["contribution-split:1"] = profile_preview._canonical_sha256(profile)
    with localcontext() as context:
        context.prec = 2
        result = _preview_batch(policy, {"id":"contribution-split","version":1}, [prepared("synthetic-half", fszn(amount))], reviewed_contract=reviewed)
    fields = result["rows"][0]["planned_concept"]["fields"]
    assert fields["secondary_whole_rub"] == secondary
    assert fields["primary_whole_rub"] + secondary == amount
