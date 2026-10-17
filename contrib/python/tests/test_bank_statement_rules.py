"""Synthetic regression tests for bank statement classification the configured policy."""
from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "onec_bank_graph" / "bank_statement_rules.py"

spec = importlib.util.spec_from_file_location("bank_statement_rules", RULES_PATH)
bank_statement_rules = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bank_statement_rules
spec.loader.exec_module(bank_statement_rules)


SYNTHETIC_POLICY = {rule.id: {"target": {"account":"synthetic-account", "debitAccount":"synthetic-bank", "creditAccount":"synthetic-cash", "operationType":"ПеречислениеНалога", "budgetPaymentKind":"Налог"}, "review_ref":"synthetic-reviewed-policy"} for rule in bank_statement_rules.RULES}
def configured_classify_line(raw, source=None, **kwargs):
    return bank_statement_rules.classify_line(raw,source,policy=SYNTHETIC_POLICY)


def classify(**values: Any) -> dict[str, Any]:
    line = {
        "sourceId": values.pop("source_id"),
        "lineNumber": 1,
        "direction": values.pop("direction", "expense"),
        "amount": values.pop("amount", "123.45"),
        "sourceAccount": values.pop("source_account", "60.1"),
        "purpose": values.pop("purpose", ""),
        "sub1": values.pop("sub1", "Синтетический контрагент"),
        "sub2": values.pop("sub2", "Синтетический договор"),
    }
    line.update(values)
    return configured_classify_line(line, policy={rule.id: {"target": {"account": "synthetic-account", "debitAccount":"synthetic-bank", "creditAccount":"synthetic-cash", "operationType":"ПеречислениеНалога", "budgetPaymentKind":"Налог"}, "review_ref":"synthetic-reviewed-policy"} for rule in bank_statement_rules.RULES})


def rule_id(decision: dict[str, Any]) -> str:
    return decision["classification"]["ruleId"]


class BankStatementRulesTests(unittest.TestCase):
    def assert_decision_queue(self, decision: dict[str, Any], expected_rule: str) -> None:
        self.assertEqual(rule_id(decision), expected_rule)
        self.assertIs(decision["classification"]["decisionRequired"], True)
        self.assertNotIn("account", decision["target"])
        self.assertIn("decision", decision["target"])

    def assert_safe_account(self, decision: dict[str, Any], expected_rule: str, account: str) -> None:
        self.assertEqual(rule_id(decision), expected_rule)
        self.assertIs(decision["classification"]["decisionRequired"], False)
        self.assertEqual(decision["target"]["account"], "synthetic-account")

    def test_usn_annual_and_quarterly_tax_periods(self) -> None:
        samples = (
            ("synthetic-usn-annual", "Единый налог при УСН, раздел налога 01201 за 2020 год", {"kind": "annual", "year": 2020}),
            ("synthetic-usn-annual-po", "Единый налог по УСН за 2021 год", {"kind": "annual", "year": 2021}),
            ("synthetic-usn-quarter", "Уплата единого налога при УСН за 3 квартал 2021 года", {"kind": "quarterly", "year": 2021, "quarter": 3}),
            ("synthetic-usn-quarter-po", "Уплата единого налога по УСН за 3 квартал 2021 года", {"kind": "quarterly", "year": 2021, "quarter": 3}),
            ("synthetic-usn-quarter-roman", "Единый налог по УСН за IV квартал 2021 года", {"kind": "quarterly", "year": 2021, "quarter": 4}),
        )
        for source_id, purpose, period in samples:
            with self.subTest(purpose=purpose):
                decision = classify(source_id=source_id, source_account="60.1", purpose=purpose)
                self.assert_safe_account(decision, "expense_usn_tax", "synthetic-account")
                self.assertEqual(decision["source"]["sourceId"], source_id)
                self.assertEqual(decision["target"]["operationType"], "ПеречислениеНалога")
                self.assertEqual(decision["target"]["budgetPaymentKind"], "Налог")
                self.assertEqual(decision["target"]["taxPeriod"], period)
                self.assertEqual(decision["classification"]["evidence"], ["synthetic-reviewed-policy"])

    def test_usn_uncertain_period_and_mixed_purposes_stay_on_hold(self) -> None:
        purposes = (
            "Единый налог при УСН, раздел налога 01201",
            "Налог по УСН за квартал 2021 года",
            "Налог по УСН за 2021 год и 2022 год",
            "Налог по УСН за 5 квартал 2021 года",
            "Налог по УСН за 1 и 2 квартал 2021 года",
            "Налог по УСН за IV квартал 2021 и 2022 года",
            "Налог по УСН за 2021 год и НДС",
            "Налог по УСН за 2021 год, комиссия банка",
            "Пеня по налогу при УСН за 2021 год",
            "Пеня по налогу при УСН за 2021 год, комиссия банка",
            "Штраф по налогу при УСН за 2021 год",
            "Раздел налога 01201 за 2021 год",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(source_id=f"synthetic-usn-hold-{index}", purpose=purpose)
                self.assertIs(decision["classification"]["decisionRequired"], True)
                self.assertNotIn("account", decision["target"])

    def test_usn_variants_cannot_fall_through_to_bank_commission(self) -> None:
        purposes = (
            "Единый налог УСН за 2021 год, комиссия банка",
            "Налог УСН за 2021 год, комиссия банка",
            "Единый налог (УСН) за 2021 год, комиссия банка",
            "Единый налог, УСН за 2021 год, комиссия банка",
            "Налог по системе УСН за 2021 год, комиссия банка",
            "Единый налог по УСН за 2021 год, комиссия банка",
            "Оплата услуг по УСН, комиссия банка",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(source_id=f"synthetic-usn-bank-fee-{index}", purpose=purpose)
                self.assert_decision_queue(decision, "expense_usn_tax_period_needs_decision")

    def test_other_taxes_never_use_usn_rule(self) -> None:
        for index, purpose in enumerate(("Налог на прибыль за 2021 год", "НДС за 3 квартал 2021 года"), start=1):
            with self.subTest(purpose=purpose):
                decision = classify(source_id=f"synthetic-other-tax-{index}", purpose=purpose)
                self.assert_decision_queue(decision, "expense_explicit_tax_needs_decision")

    def test_dotted_tax_abbreviations_hold_before_bank_fee_fallback(self) -> None:
        purposes = (
            "Н.Д.С. за 3 квартал 2021 года, комиссия банка",
            "Н. Д. С. за 3 квартал 2021 года, комиссия банка",
            "Нал. на прибыль за 2021 год, комиссия банка",
            "Нал.на прибыль за 2021 год, комиссия банка",
            "Нал. на недвижимость, комиссия банка",
            "Нал. за 2021 год, комиссия банка",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(source_id=f"synthetic-dotted-tax-{index}", purpose=purpose)
                self.assert_decision_queue(decision, "expense_explicit_tax_needs_decision")

    def test_explicit_unconfirmed_tax_blocks_expense_fallbacks(self) -> None:
        purposes = (
            "НДС за 3 квартал 2021 года",
            "НДС за 3 квартал 2021 года, комиссия банка",
            "Уплата акциза за 2021 год, комиссия банка",
            "Налог на прибыль за 2021 год и НДС",
            "Подоходный налог и НДС за 2021 год",
            "Взносы ФСЗН и НДС за 2021 год",
            "Подоходный налог за 2021 год, комиссия банка",
            "Взносы ФСЗН за 2021 год, комиссия банка",
            "Подоходный налог и заработная плата",
            "Взносы ФСЗН и зарплата",
            "Перечисление взносов ФСЗН и единый налог",
            "Уплата подоходного налога и земельного налога",
            "Подоходный налог за 2021 год, ком. воз.",
            "Взносы ФСЗН, ком. воз.",
            "Подоходный налог за 2021 год, возн. банка",
            "Налоговый платеж, комиссия банка",
            "Подоходный налог и налоговые платежи",
            "Взносы ФСЗН и налоговые платежи",
            "Подоходный налог, к. воз.",
            "Взносы ФСЗН, к. воз.",
            "Налоговые платежи, комиссия банка",
            "Перечисление подоходного налога за 2021 год, к. воз.",
            "Уплата взносов ФСЗН за 3 квартал 2021 года и прочие платежи",
            "Подоходный налог за 2021 год, к. воз. банка",
            "Взносы ФСЗН за 2021 год, к. воз. банка",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-tax-expense-{index}",
                    source_account="60.1",
                    purpose=purpose,
                    sub1="Синтетический контрагент",
                )
                self.assert_decision_queue(decision, "expense_explicit_tax_needs_decision")

    def test_explicit_unconfirmed_tax_blocks_income_fallbacks(self) -> None:
        purposes = (
            "Возврат НДС за 3 квартал 2021 года",
            "Возврат Н. Д. С. за 3 квартал 2021 года",
            "Возврат НДС, оплата от покупателя",
            "Оплата за проданные сертификаты и возврат НДС",
            "Поступление через ЕРИП, возврат налога на прибыль",
            "Возврат НДС и подоходного налога",
            "Возврат нал.на прибыль, оплата от покупателя за сертификаты",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-tax-income-{index}",
                    direction="income",
                    source_account="62.1",
                    purpose=purpose,
                    sub1="Синтетический контрагент",
                )
                self.assert_decision_queue(decision, "income_explicit_tax_needs_decision")

    def test_tax_authority_in_counterparty_blocks_commercial_fallbacks(self) -> None:
        cases = (
            ("expense", "Оплата услуг по счету №12", "Инспекция МНС по району", "expense_tax_authority_counterparty_needs_decision"),
            ("expense", "Комиссия банка за перевод", "Инспекция МНС по району", "expense_tax_authority_counterparty_needs_decision"),
            ("income", "Оплата от покупателя за услуги", "Налоговая инспекция района", "income_tax_authority_counterparty_needs_decision"),
            ("income", "Возврат оплаты от поставщика", "Налоговая инспекция района", "income_tax_authority_counterparty_needs_decision"),
            ("expense", "Оплата услуг по счету №12", "ИМНС по району", "expense_tax_authority_counterparty_needs_decision"),
            ("expense", "Оплата услуг по счету №12", "Инспекция Министерства по налогам и сборам по району", "expense_tax_authority_counterparty_needs_decision"),
            ("income", "Оплата от покупателя за услуги", "Инспекция Министерства по налогам и сборам по району", "income_tax_authority_counterparty_needs_decision"),
            ("expense", "Оплата услуг по счету №12", "Министерство по налогам и сборам Республики Беларусь", "expense_tax_authority_counterparty_needs_decision"),
        )
        for index, (direction, purpose, sub1, expected_rule) in enumerate(cases, start=1):
            with self.subTest(direction=direction, purpose=purpose, sub1=sub1):
                decision = classify(
                    source_id=f"synthetic-tax-authority-{index}",
                    direction=direction,
                    purpose=purpose,
                    sub1=sub1,
                )
                self.assert_decision_queue(decision, expected_rule)
                self.assertEqual(decision["source"]["sourceId"], f"synthetic-tax-authority-{index}")
                self.assertEqual(decision["target"]["document"], "BankReceipt" if direction == "income" else "BankWriteOff")

    def test_tax_authority_guard_preserves_confirmed_tax_and_purpose_hold(self) -> None:
        confirmed = (
            ("Оплата налога на недвижимость", "expense_property_tax", "68.1.4"),
            ("Единый налог при УСН за 2021 год", "expense_usn_tax", "68.3.2"),
            ("Уплата подоходного налога за 2021 год", "expense_income_tax", "68.4.1"),
            ("Перечисление взносов ФСЗН за 2021 год", "expense_fszn", "69.1"),
        )
        for index, (purpose, expected_rule, account) in enumerate(confirmed, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-authority-confirmed-{index}",
                    purpose=purpose,
                    sub1="Инспекция МНС по району",
                )
                self.assert_safe_account(decision, expected_rule, account)

        self.assert_decision_queue(
            classify(
                source_id="synthetic-authority-explicit-vat",
                purpose="НДС за 3 квартал 2021 года, комиссия банка",
                sub1="Инспекция МНС по району",
            ),
            "expense_explicit_tax_needs_decision",
        )
        self.assert_safe_account(
            classify(
                source_id="synthetic-full-authority-confirmed-tax",
                purpose="Оплата налога на недвижимость",
                sub1="Инспекция Министерства по налогам и сборам по району",
            ),
            "expense_property_tax",
            "68.1.4",
        )
        self.assert_decision_queue(
            classify(
                source_id="synthetic-authority-income-vat",
                direction="income",
                purpose="Возврат НДС, оплата от покупателя",
                sub1="Налоговая инспекция района",
            ),
            "income_explicit_tax_needs_decision",
        )

    def test_tax_word_in_commercial_counterparty_is_not_tax_authority(self) -> None:
        self.assert_safe_account(
            classify(
                source_id="synthetic-normal-supplier",
                purpose="Оплата услуг по счету №12",
                sub1="ООО Обычный поставщик",
            ),
            "expense_supplier_payment",
            "60.1",
        )
        self.assert_safe_account(
            classify(
                source_id="synthetic-normal-bank",
                purpose="Комиссия банка за перевод",
                sub1="Обычный банк",
            ),
            "expense_bank_fee",
            "26",
        )
        self.assert_safe_account(
            classify(
                source_id="synthetic-tax-word-bank-name",
                purpose="Комиссия банка за перевод",
                sub1="Банк Налоговый сервис",
            ),
            "expense_bank_fee",
            "26",
        )
        for sub1 in ("Налоговый консультант", "Налоговая консультация"):
            with self.subTest(sub1=sub1):
                self.assert_safe_account(
                    classify(
                        source_id=f"synthetic-commercial-name-{sub1}",
                        direction="income",
                        purpose="Оплата от покупателя за услуги",
                        sub1=sub1,
                        sub2="Договор на налоговые консультации",
                    ),
                    "income_buyer_payment",
                    "62.1",
                )

    def test_tax_word_in_commercial_supplier_name_or_contract_keeps_supplier_rule(self) -> None:
        cases = (
            ("ООО Налоговый консультант", "Синтетический договор"),
            ("ООО Поставщик", "Договор на налоговые консультации"),
            ("ООО Министерство налогового консалтинга", "Синтетический договор"),
            ("ООО Поставщик", "Договор с Инспекцией Министерства по налогам и сборам"),
        )
        for index, (sub1, sub2) in enumerate(cases, start=1):
            with self.subTest(sub1=sub1, sub2=sub2):
                self.assert_safe_account(
                    classify(
                        source_id=f"synthetic-tax-word-supplier-{index}",
                        purpose="Оплата услуг по счету №12",
                        sub1=sub1,
                        sub2=sub2,
                    ),
                    "expense_supplier_payment",
                    "60.1",
                )

        self.assert_decision_queue(
            classify(
                source_id="synthetic-tax-purpose-commercial-supplier",
                purpose="Оплата налога на прибыль",
                sub1="ООО Налоговый консультант",
                sub2="Договор на налоговые консультации",
            ),
            "expense_explicit_tax_needs_decision",
        )

    def test_tax_guard_preserves_confirmed_tax_and_ordinary_commercial_rules(self) -> None:
        confirmed = (
            ("Оплата налога на недвижимость", "expense_property_tax", "68.1.4"),
            ("Единый налог при УСН за 2021 год", "expense_usn_tax", "68.3.2"),
            ("Уплата подоходного налога за 2021 год", "expense_income_tax", "68.4.1"),
            ("Перечисление взносов ФСЗН за 2021 год", "expense_fszn", "69.1"),
            ("Подоходный налог", "expense_income_tax", "68.4.1"),
            ("Оплата подоходного налога за 3 квартал 2021 года", "expense_income_tax", "68.4.1"),
            ("Взносы ФСЗН", "expense_fszn", "69.1"),
            ("Уплата взносов ФСЗН за 3 квартал 2021 года", "expense_fszn", "69.1"),
        )
        for index, (purpose, rule, account) in enumerate(confirmed, start=1):
            with self.subTest(purpose=purpose):
                self.assert_safe_account(
                    classify(source_id=f"synthetic-confirmed-tax-{index}", purpose=purpose),
                    rule,
                    account,
                )

        supplier = classify(
            source_id="synthetic-supplier-service",
            purpose="Оплата услуг по счету №123, в т.ч. НДС",
        )
        self.assert_safe_account(supplier, "expense_supplier_payment", "synthetic-account")
        supplier_without_vat = classify(
            source_id="synthetic-supplier-service-without-vat",
            purpose="Оплата услуг по счету №124, без НДС",
        )
        self.assert_safe_account(supplier_without_vat, "expense_supplier_payment", "synthetic-account")
        supplier_with_dotted_vat = classify(
            source_id="synthetic-supplier-service-dotted-vat",
            purpose="Оплата услуг по счету №125, в т.ч. Н.Д.С.",
        )
        self.assert_safe_account(supplier_with_dotted_vat, "expense_supplier_payment", "synthetic-account")
        fee = classify(
            source_id="synthetic-pure-bank-fee",
            purpose="Комиссия банка за перевод",
            sub1="Синтетический банк",
        )
        self.assert_safe_account(fee, "expense_bank_fee", "synthetic-account")
        buyer = classify(
            source_id="synthetic-pure-buyer-payment",
            direction="income",
            source_account="62.1",
            purpose="Оплата от покупателя за сертификаты",
        )
        self.assert_safe_account(buyer, "income_buyer_payment", "synthetic-account")
        buyer_with_vat = classify(
            source_id="synthetic-buyer-payment-with-vat",
            direction="income",
            source_account="62.1",
            purpose="Оплата от покупателя за сертификаты, в т.ч. НДС",
        )
        self.assert_safe_account(buyer_with_vat, "income_buyer_payment", "synthetic-account")
        buyer_with_dotted_vat = classify(
            source_id="synthetic-buyer-payment-dotted-vat",
            direction="income",
            source_account="62.1",
            purpose="Оплата от покупателя за сертификаты, в т.ч. Н. Д. С.",
        )
        self.assert_safe_account(buyer_with_dotted_vat, "income_buyer_payment", "synthetic-account")

    def test_incoming_usn_mentions_hold_before_every_income_safe_rule(self) -> None:
        purposes = (
            "Возврат налога по УСН за 2021 год",
            "Возврат налога по УСН и НДС за 2021 год",
            "Проценты банка по УСН за 2021 год",
            "Поступление через ЕРИП, раздел налога 01201",
            "Оплата за проданные сертификаты по УСН",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-income-usn-hold-{index}",
                    direction="income",
                    purpose=purpose,
                    sub1="Синтетическое ООО",
                )
                self.assert_decision_queue(decision, "income_usn_tax_needs_decision")
                self.assertEqual(decision["target"]["document"], "BankReceipt")
                self.assertNotIn("operation", decision["target"])

    def test_income_usn_guard_uses_purpose_not_counterparty(self) -> None:
        decision = classify(
            source_id="synthetic-income-usn-counterparty",
            direction="income",
            purpose="Поступление денежных средств через ЕРИП",
            sub1="Синтетическое ООО УСН",
        )
        self.assert_safe_account(decision, "income_card_erip_intermediary", "synthetic-account")

    def test_property_tax_is_separate_and_profit_tax_is_not_a_company_rule(self) -> None:
        property_tax = classify(
            source_id="synthetic-property-tax:1",
            source_account="68.31",
            purpose="Оплата налога на недвижимость",
            sub1="Синтетическое казначейство",
        )
        self.assert_safe_account(
            property_tax,
            "expense_property_tax",
            "68.1.4",
        )
        self.assertEqual(
            property_tax["classification"]["evidence"],
            [
                "synthetic-reviewed-policy",
            ],
        )

        property_tax_penalty = classify(
            source_id="synthetic-property-tax-penalty:1",
            source_account="68.31",
            purpose="Пеня по налогу на недвижимость",
            sub1="Синтетическое казначейство",
        )
        self.assert_safe_account(
            property_tax_penalty,
            "expense_property_tax",
            "68.1.4",
        )
        self.assertEqual(property_tax_penalty["target"], property_tax["target"])

        profit_tax = classify(
            source_id="synthetic-profit-tax:1",
            source_account="68.31",
            purpose="Оплата налога на прибыль",
            sub1="Синтетическое казначейство",
        )
        self.assert_decision_queue(profit_tax, "expense_explicit_tax_needs_decision")

    def test_tax_account_alone_and_other_tax_penalties_stay_unresolved(self) -> None:
        ambiguous_tax = classify(
            source_id="synthetic-ambiguous-tax:1",
            source_account="68.31",
            purpose="Оплата налога",
            sub1="Синтетическое казначейство",
        )
        self.assert_decision_queue(
            ambiguous_tax,
            "expense_explicit_tax_needs_decision",
        )

        other_tax_penalty = classify(
            source_id="synthetic-profit-tax-penalty:1",
            source_account="68.31",
            purpose="Пеня по налогу на прибыль",
            sub1="Синтетический налоговый орган",
        )
        self.assert_decision_queue(
            other_tax_penalty,
            "expense_legacy_tax_penalty_needs_decision",
        )

    def test_other_tax_penalties_stay_unresolved_before_safe_expense_rules(self) -> None:
        samples = (
            ("68.31", "Пеня по подоходному налогу", "Синтетический налоговый орган"),
            ("68.31", "Пеня по налогу на прибыль, комиссия банка", "Синтетический банк"),
            ("99", "Штраф, комиссия банка", "Синтетический банк"),
            ("60.1", "Пеня по НДС, удержание по исполнительному листу", "Синтетический получатель"),
            ("69", "Пеня ФСЗН и подоходный налог", "Синтетический фонд"),
            ("68.31", "Пеня ФСЗН и подоходный налог", "Синтетический фонд"),
        )
        for index, (source_account, purpose, sub1) in enumerate(samples, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-other-tax-penalty:{index}",
                    source_account=source_account,
                    purpose=purpose,
                    sub1=sub1,
                )
                self.assert_decision_queue(decision, "expense_legacy_tax_penalty_needs_decision")

        fszn_only_samples = (
            ("69", "Пеня ФСЗН", "Синтетический налоговый орган"),
            ("68.31", "Пеня ФСЗН", "Синтетический фонд"),
            ("99", "Штраф ФСЗН", "Синтетический фонд"),
            ("69", "Пеня ФСЗН", "Синтетический налоговый орган по НДС"),
            ("99", "Пеня ФСЗН", "Синтетический налоговый орган по НДС"),
            ("68.31", "Пеня ФСЗН, комиссия банка", "Синтетический банк"),
        )
        for index, (source_account, purpose, sub1) in enumerate(fszn_only_samples, start=1):
            with self.subTest(source_account=source_account, purpose=purpose, sub1=sub1):
                decision = classify(
                    source_id=f"synthetic-fszn-only-penalty:{index}",
                    source_account=source_account,
                    purpose=purpose,
                    sub1=sub1,
                )
                self.assert_decision_queue(decision, "expense_fszn_penalty_needs_decision")

    def test_direct_property_tax_purposes_allow_only_bounded_suffixes(self) -> None:
        purposes = (
            "Оплата налога на недвижимость",
            "Уплата налога на недвижимость в налоговую инспекцию",
            "Перечисление налога на недвижимость за 2026 год",
            "Оплата налога на недвижимость за 3 квартал 2026 года по уведомлению №123 от 01.10.2026",
            "Оплата налога на недвижимость по уведомлению №123/2026",
            "Пеня по налогу на недвижимость",
            "Уплата пени по налогу на недвижимость в бюджет",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-property-tax-positive:{index}",
                    source_account="68.31",
                    purpose=purpose,
                    sub1="Синтетический налоговый орган",
                )
                self.assert_safe_account(decision, "expense_property_tax", "synthetic-account")

    def test_other_property_tax_mentions_stay_unresolved_before_fallbacks(self) -> None:
        purposes = (
            "Пеня по налогу на недвижимость и налогу на прибыль",
            "Оплата налога на недвижимость и налога на прибыль",
            "Пеня по налогу на недвижимость и НДС",
            "Оплата налога на недвижимость и НДС",
            "Оплата налога на недвижимость и акциза",
            "Оплата налога на недвижимость и подоходного налога",
            "Оплата налога на недвижимость и ФСЗН",
            "Штраф по налогу на недвижимость",
            "Оплата услуг по расчету налога на недвижимость",
            "Оплата налога на недвижимость и аренды",
            "Оплата налога на недвижимость и акц.",
            "Оплата налога на недвижимость, комиссия банка",
            "Уплата налога на недвижимость и подоходного налога за услуги",
            "Оплата налога на недвижимость по уведомлению №123/НДС",
            "Пеня по налогу на недвижимость по извещению №123/акц.",
            "Оплата налога на недвижимость по требованию №123подоходный",
        )
        for index, purpose in enumerate(purposes, start=1):
            with self.subTest(purpose=purpose):
                decision = classify(
                    source_id=f"synthetic-property-tax-negative:{index}",
                    source_account="68.31",
                    purpose=purpose,
                    sub1="Синтетический налоговый орган",
                )
                self.assert_decision_queue(decision, "expense_property_tax_purpose_needs_decision")

    def test_client_bank_service_fees_match_before_supplier_fallback(self) -> None:
        samples = {
            "319:1": "Плата за обслуживание системы Клиент-Банк",
            "351:1": "Комиссия за Клиент банк",
            "369:2": "Вознаграждение банка за систему клиентбанк",
            "414:2": "Плата за обслуживание клиент-банк",
        }
        for source_id, purpose in samples.items():
            with self.subTest(source_id=source_id):
                decision = classify(source_id=source_id, purpose=purpose, sub1="Банк")
                self.assert_safe_account(decision, "expense_bank_fee", "synthetic-account")

    def test_card_and_erip_receipts_use_57_1_without_buyer_analytics_from_intermediary(self) -> None:
        decision = classify(
            source_id="card-erip:1",
            direction="income",
            source_account="62.1",
            purpose="Поступление денежных средств от оплат платежной картой и ЕРИП",
            sub1="Синтетический банк / EasyPay",
        )

        self.assert_safe_account(decision, "income_card_erip_intermediary", "synthetic-account")
        self.assertNotIn("counterpartyFrom", decision["target"])
        self.assertNotIn("agreementFrom", decision["target"])

    def test_treasury_penalties_fines_and_tax_coded_withholdings_prevent_salary_or_supplier_fallback(self) -> None:
        samples = {
            "229:2": "Пеня в бюджет",
            "297:3": "Штраф, код платежа 05115",
            "418:2": "Пеня, код платежа 13511",
            "402:6": "Раздел налога, удержано по требованию",
        }
        for source_id, purpose in samples.items():
            with self.subTest(source_id=source_id):
                decision = classify(
                    source_id=source_id,
                    source_account="60.1",
                    purpose=purpose,
                    sub1="Синтетическое казначейство",
                )
                self.assert_decision_queue(decision, "expense_treasury_penalty_fine_withholding_needs_decision")

    def test_salary_withholding_has_own_decision_bucket_before_salary_fallback(self) -> None:
        decision = classify(
            source_id="46:3",
            source_account="76.7",
            purpose="Удержание из заработной платы",
            sub1="Синтетический получатель удержания",
        )

        self.assert_decision_queue(decision, "expense_salary_withholding_needs_decision")

    def test_enforcement_writ_can_use_documented_76_7_bucket(self) -> None:
        decision = classify(
            source_id="enforcement:1",
            source_account="76.7",
            purpose="Удержание по исполнительному листу из заработной платы",
            sub1="Синтетический получатель удержания",
        )

        self.assert_safe_account(decision, "expense_enforcement_withholding", "synthetic-account")

    def test_cash_revenue_deposit_is_not_ordinary_buyer_payment(self) -> None:
        decision = classify(
            source_id="314:3",
            date="2011-02-03",
            direction="income",
            source_account="62.1",
            purpose="Взнос наличными в банк: выручка за платные услуги",
            sub1="Филиал банка",
            printed_credit="123.45", printed_debit="",
        )

        self.assert_safe_account(decision, "income_cash_revenue_deposit", "synthetic-account")
        self.assertEqual(decision["target"]["document"], "CashExpenseOrder")
        self.assertEqual(decision["target"]["debitAccount"], "synthetic-bank")
        self.assertEqual(decision["target"]["creditAccount"], "synthetic-cash")
        self.assertIs(decision["target"]["additionalBankReceipt"], False)
        self.assertNotIn("counterpartyFrom", decision["target"])
        self.assertNotIn("counterparty", decision["target"])
        self.assertNotIn("contract", decision["target"])

    def test_cash_rko_route_does_not_capture_card_or_genuine_supplier_payment(self) -> None:
        card = classify(source_id="synthetic-card", direction="income",
                        purpose="Выручка по карточкам", sub1="Синтетический банк")
        self.assertEqual(card["target"]["document"], "BankReceipt")
        self.assertEqual(card["target"]["account"], "synthetic-account")
        supplier = classify(source_id="synthetic-supplier", purpose="Оплата поставщику за товар")
        self.assertEqual(supplier["target"]["document"], "BankWriteOff")
        self.assertEqual(supplier["target"]["account"], "synthetic-account")

    def test_cash_rko_scope_uses_bank_purpose_period_and_not_bank_identity(self) -> None:
        for form in ["выручка", "выручки", "выручку", "выручкой", "выручке"]:
            result = classify(source_id="synthetic-form", date="2011-01-01", direction="income", purpose=form, sub1="",
                              printed_credit="123.45", printed_debit="")
            self.assertEqual(result["target"]["document"], "CashExpenseOrder")
        for day in ["2009-12-31", "2010-01-01", "2016-06-30", "2016-07-01", "2026-10-13"]:
            result = classify(source_id="synthetic-revenue", date=day, direction="income",
                              purpose="ВЫРУЧКА за услуги", sub1="", source_account="",
                              printed_credit="123.45", printed_debit="")
            self.assertEqual(result["target"]["document"], "CashExpenseOrder")
            self.assertEqual(result["target"]["templateVersion"], "2")
        for day, purpose, direction in [
            ("2011-01-01", "За платные услуги", "income"),
            ("2011-01-01", "Перевод выручк", "income"),
            ("2011-01-01", "Сверхвыручка", "income"),
            ("2011-01-01", "Выручка и заем", "income"),
            ("2011-01-01", "Выручка и займ", "income"),
            ("2011-01-01", "Выручка", "expense"), ("", "Выручка", "income")]:
            with self.subTest(day=day, purpose=purpose, direction=direction):
                result = classify(source_id="synthetic-not-revenue", date=day,
                                  purpose=purpose, direction=direction, sub1="Синтетический банк")
                self.assertNotEqual(result["target"].get("document"), "CashExpenseOrder")

    def test_eligible_word_does_not_override_explicit_card_erip_rule(self) -> None:
        result = classify(source_id="synthetic-card-revenue", date="2016-06-30",
                          purpose="Выручка по карточкам", direction="income", sub1="")
        self.assertEqual(result["target"]["document"], "BankReceipt")
        self.assertEqual(result["target"]["account"], "synthetic-account")

    def test_scoped_mixed_revenue_loan_holds_before_any_safe_route(self) -> None:
        for purpose in ["Выручка и займ", "Выручка и заем", "Выручка и заём",
                        "Взнос выручки, возврат займа", "Выручка по карточкам и кредит"]:
            for account in ["66.4", ""]:
                with self.subTest(purpose=purpose, account=account):
                    decision = classify(source_id="synthetic-mixed-revenue-loan", date="2011-02-03",
                                        direction="income", source_account=account, purpose=purpose)
                    self.assert_decision_queue(decision, "income_mixed_revenue_loan_needs_decision")
                    self.assertNotIn("operation", decision["target"])
                    self.assertEqual(decision["source"]["amount"], "123.45")

    def test_scoped_generic_transfer_and_document_format_do_not_imply_card(self) -> None:
        for purpose in ["Внесение денежной выручки. Перевод на расчетный счет",
                        "Выручка за услуги. Электронный документ",
                        "Оплата от покупателя: выручка за услуги, в том числе НДС 20%"]:
            with self.subTest(purpose=purpose):
                decision = classify(source_id="synthetic-generic-revenue", date="2011-02-03",
                                    direction="income", source_account="", purpose=purpose, sub1="", sub2="",
                                    printed_credit="123.45", printed_debit="")
                self.assert_safe_account(decision, "income_cash_revenue_deposit", "synthetic-account")
                self.assertEqual(decision["target"]["document"], "CashExpenseOrder")

    def test_scoped_ambiguous_intermediary_revenue_holds(self) -> None:
        for purpose, sub1 in [("Выручка: денежный перевод", ""),
                              ("Электронное перечисление выручки", ""),
                              ("Выручка, операций: 3, комиссия", ""),
                              ("Выручка за услуги", "Электронный посредник")]:
            with self.subTest(purpose=purpose, sub1=sub1):
                decision = classify(source_id="synthetic-intermediary-revenue", date="2011-02-03",
                                    direction="income", purpose=purpose, sub1=sub1, sub2="")
                self.assert_decision_queue(decision, "income_revenue_intermediary_needs_decision")

    def test_scoped_explicit_card_erip_revenue_still_uses_57_1(self) -> None:
        for purpose in ["Выручка по карточкам", "Выручка через ЕРИП", "Выручка EasyPay"]:
            with self.subTest(purpose=purpose):
                decision = classify(source_id="synthetic-explicit-card-revenue", date="2011-02-03",
                                    direction="income", purpose=purpose)
                self.assert_safe_account(decision, "income_card_erip_intermediary", "synthetic-account")

    def test_revenue_guards_apply_to_new_dates_and_preserve_unrelated_routes(self) -> None:
        for day in ["2009-12-31", "2016-07-01"]:
            mixed = classify(source_id="synthetic-mixed", date=day, direction="income",
                             source_account="66.4", purpose="Выручка и займ")
            self.assert_decision_queue(mixed, "income_mixed_revenue_loan_needs_decision")
            cash = classify(source_id="synthetic-cash", date=day, direction="income",
                            purpose="Выручка. Электронный документ", printed_credit="123.45", printed_debit="")
            self.assert_safe_account(cash, "income_cash_revenue_deposit", "synthetic-account")
        loan = classify(source_id="synthetic-loan", date="2017-01-01", direction="income",
                        source_account="66.4", purpose="Взнос по договору займа")
        self.assert_safe_account(loan, "income_loan_short_term", "synthetic-account")
        transfer = classify(source_id="synthetic-transfer", date="2017-01-01", direction="income",
                            purpose="Денежный перевод")
        self.assert_safe_account(transfer, "income_card_erip_intermediary", "synthetic-account")

    def test_labeled_symbol_in_full_purpose_without_typed_link(self) -> None:
        purpose = "Символ 2004 123.45\nСинтетический полный банковский текст"
        decision = classify(source_id="synthetic-purpose-label", date="2020-01-01", direction="income", purpose=purpose,
                            printed_credit="123.45", printed_debit="")
        self.assert_safe_account(decision, "income_cash_revenue_deposit", "synthetic-account")
        for purpose in ["Упоминание символ 2004", "Символ 20040", "Символ 2004.00"]:
            decision = classify(source_id="synthetic-purpose-negative", date="2020-01-01", direction="income", purpose=purpose)
            self.assertNotEqual(decision["target"].get("document"), "CashExpenseOrder")

    def test_exact_symbol2004_and_raw_only_label_select_blank_purpose(self) -> None:
        for extra in [{"cash_symbols": ["2004"]},
                      {"bank_row_raw": "Символ 2004 123.45\nСинтетическое описание"},
                      {"bank_row_raw": "row\n   \x1b4символ 2004 : 123.45\x1b5"}]:
            with self.subTest(extra=extra):
                decision = classify(source_id="synthetic-symbol", date="2017-06-22", direction="income", purpose="",
                                    printed_credit="123.45", printed_debit="", **extra)
                self.assert_safe_account(decision, "income_cash_revenue_deposit", "synthetic-account")
                self.assertNotIn("period", decision["target"])
        for extra in [{}, {"cash_symbols": ["15", "20040", "02004", 2004]},
                      {"bank_row_raw": "Символ 20040"}, {"bank_row_raw": "Символ 2004.00"},
                      {"bank_row_raw": "счет 2004; сумма 2004; код операции 15"},
                      {"bank_row_raw": "Назначение: символ 2004"}]:
            with self.subTest(extra=extra):
                decision = classify(source_id="synthetic-not-symbol", date="2017-06-22", direction="income", purpose="", **extra)
                self.assertNotEqual(decision["target"].get("document"), "CashExpenseOrder")
        for bad in [{"direction": "expense"}, {"amount": "0"}, {"amount": "-1"},
                    {"date": "2017-02-30"}, {"income": "123.45", "expense": "1"}]:
            with self.subTest(bad=bad):
                args = {"source_id": "synthetic-bad", "date": "2017-01-01", "direction": "income",
                        "purpose": "", "cash_symbols": ["2004"], **bad}
                self.assertNotEqual(classify(**args)["target"].get("document"), "CashExpenseOrder")
        for purpose, expected in [("Карточки", "income_card_erip_intermediary"),
                                  ("Выручка и займ", "income_mixed_revenue_loan_needs_decision"),
                                  ("Электронное перечисление", "income_revenue_intermediary_needs_decision"),
                                  ("Ошибочно перечисленная сумма", "income_erroneous_or_government_transfer_needs_decision")]:
            decision = classify(source_id="synthetic-conflict", date="2017-01-01", direction="income",
                                purpose=purpose, cash_symbols=["2004"], printed_credit="123.45", printed_debit="")
            self.assertEqual(rule_id(decision), expected)

    def test_symbol_only_raw_conflicts_money_and_nbsp(self) -> None:
        base = {"source_id": "synthetic-symbol-raw", "date": "2017-06-22", "direction": "income",
                "purpose": "", "bank_row_raw": "Символ\u00a02004 123.45", "printed_credit": "123.45",
                "printed_debit": "0"}
        self.assert_safe_account(classify(**base), "income_cash_revenue_deposit", "synthetic-account")
        for raw, rejected in [("Символ\u00a02004 123.45\nЗайм", "income_mixed_revenue_loan_needs_decision"),
                              ("Символ 2004 123.45\nВозврат комиссии", "income_revenue_intermediary_needs_decision")]:
            with self.subTest(raw=raw):
                self.assert_decision_queue(classify(**{**base, "bank_row_raw": raw}), rejected)
        card = classify(**{**base, "bank_row_raw": "Символ 2004 123.45\nКарточки"})
        self.assertNotEqual(card["target"].get("document"), "CashExpenseOrder")
        for bad in [{"printed_credit": ""}, {"printed_credit": "200"}, {"printed_debit": "1"},
                    {"amount": "200"}, {"income": "100"}, {"income": "0"},
                    {"expense": "-1"}, {"printed_credit": "bad"}]:
            with self.subTest(bad=bad):
                self.assert_decision_queue(classify(**{**base, **bad}), "cash_revenue_amount_conflict_needs_decision")

    def test_explicit_symbol_components_hold_whole_credit(self) -> None:
        self.assert_decision_queue(classify(source_id="synthetic-purpose-component",
            date="2020-01-01", direction="income", source_account="", amount="123.45",
            income="123.45", printed_credit="123.45", printed_debit="",
            purpose="Символ 2004: 50"), "component_conflict_needs_decision")
        for purpose, symbols in [("", ["2004"]), ("Выручка за услуги", []),
                                 ("Выручка за услуги", ["2004"])]:
            for raw in ["Символ 2004: 50", "Символ 2004:50\nСимвол 2032: 73.45",
                        "Символ 2004: 123.45\nСимвол 2032: 50"]:
                with self.subTest(purpose=purpose, symbols=symbols, raw=raw):
                    self.assert_decision_queue(classify(source_id="synthetic-component", date="2020-01-01",
                        direction="income", source_account="", amount="123.45", income="123.45",
                        printed_credit="123.45", printed_debit="", purpose=purpose,
                        cash_symbols=symbols, bank_row_raw=raw), "component_conflict_needs_decision")
            for raw in ["Символ 2004: 123.45", "\x1b4Символ 2004: 123.45\x1b5",
                        "Символ 2004: 123,45\r\n"]:
                with self.subTest(purpose=purpose, symbols=symbols, raw=raw):
                    self.assert_safe_account(classify(source_id="synthetic-component", date="2020-01-01",
                        direction="income", source_account="", amount="123.45", income="123.45",
                        printed_credit="123.45", printed_debit="", purpose=purpose,
                        cash_symbols=symbols, bank_row_raw=raw), "income_cash_revenue_deposit", "50.1")

    def test_complete_component_formats_and_unknown_hold(self):
        cases = [
            ('plain_equal', 'Символ 2004: 1250', True, None),
            ('comma_equal', 'Символ 2004: 1250,00', True, None),
            ('ASCII_group', 'Символ 2004: 1 250,00', True, None),
            ('NBSP_group', 'Символ 2004: 1\xa0250.00', True, None),
            ('narrowNBSP_group', 'Символ 2004: 1\u202f250.00', True, None),
            ('CRLF_equal', 'Символ 2004: 1250\r\n', True, None),
            ('CRLF_conflict', 'Символ 2004: 50\r\n', False, 'conflict'),
            ('group_conflict', 'Символ 2004: 2 000,00', False, 'conflict'),
            ('currency_unit_unbound', 'Символ 2004: 1250,00 BYN', False, 'unknown'),
            ('ESC_pair', '\x1b4Символ 2004: 1 250,00\x1b5', True, None),
            ('CID_pair', '(cid:27)4Символ 2004: 1250(cid:27)5', True, None),
            ('ESC_conflict', '\x1b4Символ 2004: 50\x1b5', False, 'conflict'),
            ('CID_conflict', '(cid:27)4Символ 2004: 50(cid:27)5', False, 'conflict'),
            ('absent', '', True, None),
            ('zero_other', 'Символ 2032: 0', True, None),
            ('positive_other_only', 'Символ 2032: 20', False, 'conflict'),
            ('positive_other', 'Символ 2004: 1250\nСимвол 2032: 1 250,00', False, 'conflict'),
            ('repeat_equal', 'Символ 2004: 1250\nСимвол 2004: 1 250,00', True, None),
            ('repeat_conflict', 'Символ 2004: 1250\nСимвол 2004: 50', False, 'conflict'),
            ('flattened', 'Символ 2004: 1250.005', False, 'unknown'),
            ('three_decimals', 'Символ 2004: 1250.001', False, 'unknown'),
            ('negative', 'Символ 2004: -50', False, 'unknown'),
            ('plus', 'Символ 2004: +1250', False, 'unknown'),
            ('nan', 'Символ 2004: NaN', False, 'unknown'),
            ('inf', 'Символ 2004: Infinity', False, 'unknown'),
            ('unknown', 'Символ 2004: неизвестно', False, 'unknown'),
            ('empty', 'Символ 2004: ', False, 'unknown'),
            ('double_space', 'Символ 2004: 1  250,00', False, 'unknown'),
            ('tab_group', 'Символ 2004: 1\t250,00', False, 'unknown'),
            ('bad_group', 'Символ 2004: 12 50,00', False, 'unknown'),
            ('mixed_group', 'Символ 2004: 1 000\xa0250,00', False, 'unknown'),
            ('partial_money', 'Символ 2004: 1250,00:', False, 'unknown'),
            ('dotdot', 'Символ 2004: 1250..00', False, 'unknown'),
            ('decimal_suffix', 'Символ 2004: 1250,00.1', False, 'unknown'),
            ('trailing_number', 'Символ 2004: 1250 50', False, 'unknown'),
            ('trailing_prose', 'Символ 2004: 1250 пояснение', False, 'unknown'),
            ('trailing_marker', 'Символ 2004: 1250\x1b5', False, 'unknown'),
            ('label_only', 'Символ 2004', False, 'unknown'),
            ('missing_ESC_end', '\x1b4Символ 2004: 1250', False, 'unknown'),
            ('mixed_marker_representation', '\x1b4Символ 2004: 1250(cid:27)5', True, None),
            ('unknown_other', 'Символ 2004: 1250\nСимвол 2032: неизвестно', False, 'unknown'),
            ('repeat_unknown', 'Символ 2004: 1250\nСимвол 2004: 1250.005', False, 'unknown'),
            ('unknown_before_equal', 'Символ 2004: ?\nСимвол 2004: 1250', False, 'unknown'),
            ('conflict_then_unknown', 'Символ 2004: 50\nСимвол 2032: ?', False, 'unknown'),
        ]
        for purpose, symbols in [("Выручка", []), ("", ["2004"]), ("Выручка", ["2004"])]:
            for place in ["purpose", "bank_row_raw"]:
                for name, raw, accepted, issue in cases:
                    facts = {"sourceId": "synthetic-component", "date": "2020-01-01",
                             "direction": "income", "amount": 1250, "income": "1250",
                             "printed_credit": "1250", "printed_debit": "",
                             "purpose": purpose, "cash_symbols": symbols}
                    facts[place] = facts.get(place, "") + "\n" + raw
                    before = copy.deepcopy(facts)
                    with self.subTest(purpose=purpose, place=place, case=name):
                        result = configured_classify_line(facts)
                        if accepted:
                            self.assert_safe_account(result, "income_cash_revenue_deposit", "synthetic-account")
                        else:
                            self.assert_decision_queue(result, "component_" + issue + "_needs_decision")
                            self.assertEqual(result["source"]["amountValidation"]["reason"], "component_" + issue)
                        self.assertEqual(facts, before)

    def test_typed_component_evidence_is_not_dropped(self):
        cases = [
            ('code_only', [{'code_raw': '2004'}], True),
            ('empty', [], True),
            ('unknown', [{'code_raw': '2004', 'amount_raw': '1250.005', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '   4символ 2004 : 1250.005'}], False),
            ('unknown_no_markers', [{'code_raw': '2004', 'amount_raw': '1250.005', 'amount': None}], False),
            ('exact', [{'code_raw': '2004', 'amount_raw': '1 250,00', 'amount': '1250.00'}], True),
            ('normalized_conflict', [{'code_raw': '2004', 'amount_raw': '1250', 'amount': '50'}], False),
            ('missing', [{'code_raw': '2004', 'amount': None}], False),
            ('null', None, False),
            ('malformed', {}, False),
            ('other_unknown', [{'code_raw': '2032', 'amount_raw': '?'}], False),
            ('other_positive', [{'code_raw': '2032', 'amount_raw': '50'}], False),
            ('other_zero', [{'code_raw': '2032', 'amount_raw': '0'}], True),
            ('repeated_unknown', [{'code_raw': '2004', 'amount_raw': '1250'}, {'code_raw': '2004', 'amount_raw': '?'}], False),
            ('bad_markers_type', [{'code_raw': '2004', 'amount_raw': '1250', 'markers_raw': []}], False),
            ('bad_normalized', [{'code_raw': '2004', 'amount_raw': '1250', 'amount': 'NaN'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 1250\x1b5'}], True),
            ('paired', [{'code_raw': '2004', 'amount_raw': '50\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 50\x1b5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250.005\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 1250.005\x1b5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 1250(cid:27)5'}], True),
            ('paired', [{'code_raw': '2004', 'amount_raw': '50(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 50(cid:27)5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250.005(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 1250.005(cid:27)5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 1250(cid:27)5'}], True),
            ('paired', [{'code_raw': '2004', 'amount_raw': '50(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 50(cid:27)5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250.005(cid:27)5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '\x1b4Символ 2004: 1250.005(cid:27)5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 1250\x1b5'}], True),
            ('paired', [{'code_raw': '2004', 'amount_raw': '50\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 50\x1b5'}], False),
            ('paired', [{'code_raw': '2004', 'amount_raw': '1250.005\x1b5', 'amount': None, 'semantics_status': 'unknown', 'markers_raw': '(cid:27)4Символ 2004: 1250.005\x1b5'}], False),
        ]
        for purpose, symbols in [("Выручка", []), ("", ["2004"]), ("Выручка", ["2004"])]:
            for name, components, accepted in cases:
                # R9: both supplied projections must agree. These old amount-only
                # fixtures also exercise contradictory empty/other-code lists.
                accepted = accepted and (name == "empty" if not symbols
                                         else name not in {"empty", "other_zero"})
                with self.subTest(case=name, purpose=purpose):
                    facts = {"sourceId": "synthetic-typed", "date": "2020-01-01",
                             "direction": "income", "amount": 1250, "printed_credit": "1250",
                             "printed_debit": "", "purpose": purpose, "cash_symbols": symbols,
                             "cash_symbol_components": components}
                    before = copy.deepcopy(facts)
                    result = configured_classify_line(facts)
                    self.assertEqual(not result["classification"]["decisionRequired"], accepted)
                    self.assertEqual(result["target"].get("document") == "CashExpenseOrder", accepted)
                    self.assertEqual(facts, before)

    def test_all_revenue_selectors_require_observed_printed_money(self) -> None:
        selectors = [
            {"purpose": "Выручка за услуги"},
            {"purpose": "", "cash_symbols": ["2004"]},
            {"purpose": "Выручка за услуги", "cash_symbols": ["2004"]},
        ]
        base = {"source_id": "synthetic-revenue-money", "date": "2020-01-01",
                "direction": "income", "amount": "123.45", "printed_credit": "123.45",
                "printed_debit": ""}
        for selector in selectors:
            with self.subTest(selector=selector):
                self.assert_safe_account(classify(**{**base, **selector}),
                                         "income_cash_revenue_deposit", "50.1")
                self.assert_safe_account(classify(**{**base, **selector, "printed_debit": "0"}),
                                         "income_cash_revenue_deposit", "50.1")
                for bad in [
                    {"printed_credit": ""}, {"printed_credit": "0"},
                    {"printed_credit": "999"}, {"printed_credit": "NaN"},
                    {"printed_credit": "Infinity"}, {"printed_debit": "1"},
                    {"printed_debit": None}, {"printed_debit": "NaN"},
                    {"amount": "999"}, {"income": "999"},
                    {"income": "0"}, {"expense": "1"},
                ]:
                    with self.subTest(selector=selector, bad=bad):
                        self.assertNotEqual(classify(**{**base, **selector, **bad})["target"].get("document"),
                                            "CashExpenseOrder")
                for missing in ("printed_credit", "printed_debit"):
                    row = {**base, **selector}
                    row.pop(missing)
                    self.assert_decision_queue(classify(**row), "cash_revenue_amount_conflict_needs_decision")
                observed = {"source_id": base["source_id"], "field": "printed_debit",
                            "raw": "   ", "normalized": None, "status": "absent"}
                self.assert_safe_account(classify(**{**base, **selector, "printed_debit": None,
                                                     "printed_debit_observation": observed}),
                                         "income_cash_revenue_deposit", "50.1")
                for bad_observation in [{**observed, "source_id": "another-event"},
                                        {**observed, "raw": "1"},
                                        {**observed, "normalized": "0"},
                                        {**observed, "status": "unknown"}]:
                    self.assert_decision_queue(classify(**{**base, **selector, "printed_debit": None,
                                                             "printed_debit_observation": bad_observation}),
                                               "cash_revenue_amount_conflict_needs_decision")
        for label in ["Символ 20040", "Символ 2004.00", "Текст: Символ 2004"]:
            self.assertNotEqual(classify(**{**base, "bank_row_raw": label})["target"].get("document"), "CashExpenseOrder")

    def test_symbol_raw_exclusions_and_cid_prefix(self) -> None:
        base = {"source_id": "synthetic-raw-exclusion", "date": "2017-06-22", "direction": "income",
                "purpose": "", "printed_credit": "123.45", "printed_debit": "0"}
        cid = "(cid:27)4символ 2004 : 123.45(cid:27)5"
        self.assert_safe_account(classify(**{**base, "bank_row_raw": cid}),
                                 "income_cash_revenue_deposit", "50.1")
        for text, rejected in [("Ошибочный платеж", "income_erroneous_or_government_transfer_needs_decision"),
                               ("Проценты банка", "income_bank_interest"),
                               ("Возврат налога", "income_explicit_tax_needs_decision"),
                               ("УСН 01201", "income_usn_tax_needs_decision")]:
            with self.subTest(text=text):
                decision = classify(**{**base, "bank_row_raw": cid + "\n" + text})
                self.assertEqual(rule_id(decision), rejected)
                self.assertNotEqual(decision["target"].get("document"), "CashExpenseOrder")
        for bad in ["(cid:27)4символ 20040", "(cid:27)4символ 2004.00", "prefix (cid:27)4символ 2004"]:
            self.assertNotEqual(classify(**{**base, "bank_row_raw": bad})["target"].get("document"), "CashExpenseOrder")

    def test_erroneous_transfer_is_not_safe_buyer_fallback(self) -> None:
        decision = classify(
            source_id="226:1",
            direction="income",
            source_account="62.1",
            purpose="Ошибочно перечисленная сумма",
            sub1="Синтетическое финансовое управление",
        )

        self.assert_decision_queue(decision, "income_erroneous_or_government_transfer_needs_decision")

    def test_supplier_and_buyer_fallbacks_require_business_words_not_legacy_account_only(self) -> None:
        supplier_decision = classify(
            source_id="supplier:1",
            source_account="60.1",
            purpose="Налоговый платеж без подробностей",
            sub1="Синтетический поставщик",
        )
        self.assert_decision_queue(supplier_decision, "expense_explicit_tax_needs_decision")

        buyer_decision = classify(
            source_id="buyer:1",
            direction="income",
            source_account="62.1",
            purpose="Поступление без назначения",
            sub1="Синтетический покупатель",
        )
        self.assert_decision_queue(buyer_decision, "income_unclassified_needs_decision")

    def test_malformed_amount_never_becomes_safe_bank_fee(self) -> None:
        decision = classify(
            source_id="bad-amount:1",
            direction="expense",
            amount="not-a-number",
            source_account="60.1",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assert_decision_queue(decision, "amount_malformed_needs_decision")
        self.assertEqual(decision["source"]["amount"], "not-a-number")
        self.assertEqual(decision["source"]["amountValidation"]["status"], "needsDecision")

    def test_mixed_income_and_expense_never_uses_purpose_fallback(self) -> None:
        decision = classify(
            source_id="mixed-amount:1",
            direction="income",
            amount="123.45",
            income="123.45",
            expense="123.45",
            source_account="62.1",
            purpose="Поступление денежных средств от оплат платежной картой и ЕРИП",
            sub1="Банк",
        )

        self.assert_decision_queue(decision, "amount_mixed_needs_decision")
        self.assertEqual(decision["source"]["amountValidation"]["reason"], "amount_mixed")

    def test_audit_record_preserves_source_identity_amount_and_cross_check_role(self) -> None:
        decision = classify(
            source_id="audit:1",
            direction="expense",
            amount="9876.54",
            source_account="60.1",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assertEqual(decision["source"]["sourceId"], "audit:1")
        self.assertEqual(decision["source"]["amount"], "9876.54")
        self.assertEqual(decision["source"]["sourceAccount"], "60.1")
        self.assertIs(decision["crossCheck"]["sourceAccountIsAuthoritative"], False)

    def test_malformed_amount_cannot_be_silently_rewritten_to_zero_or_safe_account(self) -> None:
        decision = classify(
            source_id="bad-amount:1",
            direction="expense",
            amount="not-a-number",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assert_decision_queue(decision, "amount_malformed_needs_decision")
        self.assertEqual(decision["source"]["amount"], "not-a-number")
        self.assertEqual(decision["source"]["amountValidation"]["reason"], "amount_malformed")

    def test_zero_amount_with_business_text_cannot_be_safe_account(self) -> None:
        decision = classify(
            source_id="zero-amount:1",
            direction="expense",
            amount="0.00",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assert_decision_queue(decision, "amount_zero_needs_decision")
        self.assertEqual(decision["source"]["amount"], "0.00")

    def test_mixed_income_and_expense_fields_cannot_pick_directional_rule(self) -> None:
        decision = classify(
            source_id="mixed-amount:1",
            direction="expense",
            income="10.00",
            expense="20.00",
            amount="20.00",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assert_decision_queue(decision, "amount_mixed_needs_decision")
        self.assertEqual(decision["source"]["amount"], "income=10.00; expense=20.00")

    def test_explicit_direction_cannot_override_contradictory_amount_fields(self) -> None:
        decision = classify(
            source_id="direction-conflict:1",
            direction="income",
            income="",
            expense="15.00",
            amount="15.00",
            source_account="62.1",
            purpose="Поступление денежных средств от оплат платежной картой и ЕРИП",
            sub1="Синтетический банк / EasyPay",
        )

        self.assert_decision_queue(decision, "amount_direction_conflict_needs_decision")
        self.assertEqual(decision["source"]["amount"], "expense=15.00; amount=15.00")

    def test_valid_comma_amount_is_normalized_without_losing_value(self) -> None:
        decision = classify(
            source_id="comma-amount:1",
            direction="expense",
            amount="9 876,54",
            purpose="Плата за обслуживание Клиент-Банк",
            sub1="Банк",
        )

        self.assert_safe_account(decision, "expense_bank_fee", "synthetic-account")
        self.assertEqual(decision["source"]["amount"], "9876.54")
        self.assertEqual(decision["source"]["amountValidation"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
