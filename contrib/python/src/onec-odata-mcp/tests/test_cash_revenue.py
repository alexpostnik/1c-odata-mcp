"""Synthetic accounting boundary regressions; never calls 1C."""
import copy
import unittest
from datetime import date

from onec_odata_mcp.cash_revenue import CashRevenueError, bank_row_in_rko_scope, build_rko_draft, rko_cleanup_candidates


class CashRevenueTests(unittest.TestCase):
    def setUp(self):
        self.facts = {"source_id": "synthetic-event", "date": "2011-02-03T00:00:00",
                      "amount": 1250, "printed_credit": "1250", "printed_debit": "",
                      "purpose": "  Выручка за услуги; синтетический полный текст\nВторая строка.  ",
                      "bank_direction": "credit",
                      "bank_account_correlation_evidence": "synthetic:source-bank-context",
                      "operation_class": "cash_revenue_bank_deposit",
                      "source_links": {"bank": "synthetic-bank", "rko": "synthetic-rko"},
                      "overlap_evidence": "synthetic:reviewed-union",
                      "currency_unit_evidence": "synthetic:historical-unit"}
        self.config = {"depositor": "Синтетический сдатчик",
                       "depositor_employee_evidence": "synthetic:existing-employee"}
        self.refs = {name: f"00000000-0000-4000-8000-{n:012d}" for n, name in enumerate(
            ["organization", "currency", "cash_account", "bank_account",
             "organization_bank_account"], start=1)}

    def test_debit_bank_credit_cash_and_no_bank_receipt_or_supplier_analytics(self):
        value = build_rko_draft(self.facts, self.refs, self.config)
        self.assertEqual(value["entity_set"], "Document_РасходныйКассовыйОрдер")
        self.assertEqual(value["payload_draft"]["ВидОперации"], "ВзносНаличнымиВБанк")
        self.assertEqual(value["payload_draft"]["СчетКасса_Key"], self.refs["cash_account"])
        self.assertEqual(value["payload_draft"]["СчетУчетаРасчетовСКонтрагентом_Key"], self.refs["bank_account"])
        row = value["movement_expectation_draft"]["rows"][0]
        self.assertEqual(row, {"amount": 1250, "debit": self.refs["bank_account"], "credit": self.refs["cash_account"]})
        self.assertFalse(value["can_apply"])
        self.assertEqual(value["status"], "review_required")
        self.assertNotIn("payload", value)
        self.assertNotIn("post", value)
        for field in ["Number", "Ref_Key", "ДоговорКонтрагента_Key", "Контрагент", "РасшифровкаПлатежа"]:
            self.assertNotIn(field, value["payload_draft"])

    def test_bank_required_rko_is_only_corroboration(self):
        facts = {**self.facts, "source_links": {"bank": "synthetic-source"}}
        self.assertEqual(build_rko_draft(facts, self.refs, self.config)["source_links"], facts["source_links"])
        with self.assertRaises(CashRevenueError):
            build_rko_draft({**facts, "source_links": {"rko": "synthetic-rko"}}, self.refs, self.config)

    def test_generic_transfer_and_electronic_document_keep_cash_draft(self):
        for purpose in ["Внесение денежной выручки. Перевод на расчетный счет",
                        "Выручка за услуги. Электронный документ"]:
            with self.subTest(purpose=purpose):
                draft = build_rko_draft({**self.facts, "purpose": purpose}, self.refs, self.config)
                self.assertEqual(draft["payload_draft"]["Комментарий"], purpose)
                self.assertFalse(draft["can_apply"])

    def test_ambiguous_electronic_or_transfer_context_requires_review(self):
        for purpose in ["Электронное перечисление выручки", "Выручка: денежный перевод",
                        "Выручка, операций: 3, комиссия"]:
            with self.subTest(purpose=purpose), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": purpose}, self.refs, self.config)

    def test_comment_is_exact_full_purpose_and_depositor_is_private_configuration(self):
        value = build_rko_draft({**self.facts, "attachment": "Synthetic receipt"}, self.refs, self.config)
        self.assertEqual(value["payload_draft"]["Комментарий"], self.facts["purpose"])
        self.assertEqual(value["payload_draft"]["Основание"], self.facts["purpose"])
        self.assertEqual(value["payload_draft"]["Выдать"], self.config["depositor"])
        self.assertEqual(value["payload_draft"]["Приложение"], "Synthetic receipt")
        for config in [{}, {"depositor": "X", "depositor_employee_evidence": ""}, {"depositor": ""}, {"depositor": "X", "bank": "Synthetic bank"}]:
            with self.assertRaises(CashRevenueError):
                build_rko_draft(self.facts, self.refs, config)
        for field in ["recipient", "comment", "counterparty", "contract", "subconto"]:
            with self.subTest(field=field), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, field: "obsolete override"}, self.refs, self.config)

    def test_exact_selector_boundaries_word_and_bank_credit(self):
        for form in ["выручка", "выручки", "выручку", "выручкой", "выручкою", "выручке", "выручкам", "выручками", "выручках", "выручек"]:
            self.assertTrue(bank_row_in_rko_scope("2011-01-01", f"Взнос {form} за услуги", "credit"))
        for day in ["2009-12-31", "2010-01-01", "2016-06-30", "2016-07-01", "2026-10-13", "2016-06-30T23:59:59"]:
            self.assertTrue(bank_row_in_rko_scope(day, "Услуги. ВЫРУЧКА\nза день", "credit"))
        for day, purpose, direction in [
            ("2016-02-30", "Выручка", "credit"), ("2011-01-01", "Выручка", "debit"),
            ("2011-01-01", "За платные услуги", "credit"),
            ("2011-01-01", "Сверхвыручка", "credit"), ("2011-01-01", "Выручк", "credit")]:
            self.assertFalse(bank_row_in_rko_scope(day, purpose, direction))
        for day in ["2010-01-01T00:00:00", "2016-06-30T23:59:59"]:
            self.assertFalse(build_rko_draft({**self.facts, "date": day}, self.refs, self.config)["can_apply"])

    def test_labeled_symbol_in_full_purpose_without_typed_link(self):
        purpose = "Символ 2004 1250\nСинтетический полный банковский текст"
        draft = build_rko_draft({**self.facts, "date": "2020-01-01T00:00:00", "purpose": purpose}, self.refs, self.config)
        self.assertEqual(draft["payload_draft"]["Комментарий"], purpose)
        for purpose in ["Упоминание символ 2004", "Символ 20040", "Символ 2004.00"]:
            with self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": purpose}, self.refs, self.config)

    def test_symbol2004_structured_or_raw_and_blank_purpose_preserved(self):
        for extra in [{"cash_symbols": ["2004"]},
                      {"bank_row_raw": "Символ 2004 1250\nСинтетическое описание"},
                      {"bank_row_raw": "row\n   \x1b4символ 2004 : 1250\x1b5"}]:
            for purpose in ["", "  ", "Синтетическое назначение без ключевого слова"]:
                with self.subTest(extra=extra, purpose=purpose):
                    facts = {**self.facts, **extra, "purpose": purpose, "date": "2017-06-22T00:00:00"}
                    saved = copy.deepcopy(facts)
                    draft = build_rko_draft(facts, self.refs, self.config)
                    self.assertEqual(draft["payload_draft"]["Основание"], purpose)
                    self.assertEqual(draft["payload_draft"]["Комментарий"], purpose)
                    self.assertEqual(facts, saved)
                    self.assertFalse(draft["can_apply"])
        for purpose in ["Выручка и заем", "Выручка по карточкам", "Электронное перечисление", "ЕРИП"]:
            with self.subTest(purpose=purpose), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": purpose, "cash_symbols": ["2004"]}, self.refs, self.config)
        for extra in [{}, {"cash_symbols": ["20040", "02004", "15", 2004]},
                      {"bank_row_raw": "Символ 20040"}, {"bank_row_raw": "Символ 2004.00"},
                      {"bank_row_raw": "Счет 2004; сумма 2004; код операции 15"},
                      {"bank_row_raw": "В тексте упомянут символ 2004"}]:
            with self.subTest(extra=extra), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": "", **extra}, self.refs, self.config)
        for bad in [{"amount": 0}, {"amount": -1}, {"bank_direction": "debit"},
                    {"date": "2017-02-30T00:00:00"}, {"purpose": None}]:
            with self.subTest(bad=bad), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": "", "cash_symbols": ["2004"], **bad}, self.refs, self.config)

    def test_symbol_only_draft_requires_printed_money_equality(self):
        base = {**self.facts, "purpose": "", "cash_symbols": ["2004"]}
        for change in [{"printed_credit": None}, {"printed_credit": ""}, {"printed_credit": "999"},
                       {"printed_credit": "-1250"}, {"printed_debit": "1"},
                       {"printed_debit": "bad"}, {"amount": 999}]:
            with self.subTest(change=change), self.assertRaises(CashRevenueError):
                build_rko_draft({**base, **change}, self.refs, self.config)
        without_print = {key: value for key, value in base.items() if key not in {"printed_credit", "printed_debit"}}
        with self.assertRaises(CashRevenueError):
            build_rko_draft(without_print, self.refs, self.config)
        self.assertEqual(build_rko_draft(base, self.refs, self.config)["payload_draft"]["СуммаДокумента"], 1250)

    def test_explicit_symbol_components_must_cover_whole_credit(self):
        with self.assertRaises(CashRevenueError):
            build_rko_draft({**self.facts, "purpose": "Символ 2004: 50"}, self.refs, self.config)
        for purpose, symbols in [("", ["2004"]), ("Выручка за услуги", []),
                                 ("Выручка за услуги", ["2004"])]:
            for raw in ["Символ 2004: 50", "Символ 2004:50\nСимвол 2032: 1200",
                        "Символ 2004: 1250\nСимвол 2032: 50"]:
                with self.subTest(purpose=purpose, symbols=symbols, raw=raw), self.assertRaises(CashRevenueError):
                    build_rko_draft({**self.facts, "purpose": purpose, "cash_symbols": symbols,
                                     "bank_row_raw": raw}, self.refs, self.config)
            for raw in ["Символ 2004: 1250", "\x1b4Символ 2004: 1250\x1b5", "Символ 2004: 1 250.00"]:
                with self.subTest(purpose=purpose, symbols=symbols, raw=raw):
                    self.assertEqual(build_rko_draft({**self.facts, "purpose": purpose,
                        "cash_symbols": symbols, "bank_row_raw": raw}, self.refs, self.config)["status"],
                        "review_required")

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
                    facts = {**self.facts, "purpose": purpose, "cash_symbols": symbols}
                    facts[place] = facts.get(place, "") + "\n" + raw
                    before = copy.deepcopy(facts)
                    with self.subTest(purpose=purpose, place=place, case=name):
                        if accepted:
                            result = build_rko_draft(facts, self.refs, self.config)
                            self.assertEqual(result["payload_draft"]["СуммаДокумента"], 1250)
                            self.assertFalse(result["can_apply"])
                        else:
                            with self.assertRaisesRegex(CashRevenueError, "component_unknown" if issue == "unknown" else "component conflicts"):
                                build_rko_draft(facts, self.refs, self.config)
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
                    facts = {**self.facts, "purpose": purpose, "cash_symbols": symbols,
                             "cash_symbol_components": components}
                    before = copy.deepcopy(facts)
                    if accepted:
                        self.assertFalse(build_rko_draft(facts, self.refs, self.config)["can_apply"])
                    else:
                        with self.assertRaises(CashRevenueError):
                            build_rko_draft(facts, self.refs, self.config)
                    self.assertEqual(facts, before)

    def test_all_revenue_selectors_require_observed_printed_money(self):
        selectors = [{"purpose": "Выручка за услуги"},
                     {"purpose": "", "cash_symbols": ["2004"]},
                     {"purpose": "Выручка за услуги", "cash_symbols": ["2004"]}]
        for selector in selectors:
            with self.subTest(selector=selector):
                base = {**self.facts, **selector}
                self.assertEqual(build_rko_draft(base, self.refs, self.config)["status"], "review_required")
                self.assertEqual(build_rko_draft({**base, "printed_debit": "0"}, self.refs, self.config)["status"], "review_required")
                for bad in [{"printed_credit": ""}, {"printed_credit": "0"},
                            {"printed_credit": "999"}, {"printed_credit": "NaN"},
                            {"printed_credit": "Infinity"}, {"printed_debit": "1"},
                            {"printed_debit": None}, {"printed_debit": "NaN"},
                            {"amount": 999}, {"income": "999"}, {"income": "0"},
                            {"expense": "1"}, {"income": "NaN"}, {"expense": "Infinity"}]:
                    with self.subTest(selector=selector, bad=bad), self.assertRaises(CashRevenueError):
                        build_rko_draft({**base, **bad}, self.refs, self.config)
                for missing in ("printed_credit", "printed_debit"):
                    without = {key: value for key, value in base.items() if key != missing}
                    with self.subTest(selector=selector, missing=missing), self.assertRaises(CashRevenueError):
                        build_rko_draft(without, self.refs, self.config)
                self.assertEqual(build_rko_draft({**base, "income": "1250", "expense": ""},
                                                 self.refs, self.config)["status"], "review_required")
                observed = {"source_id": base["source_id"], "field": "printed_debit",
                            "raw": "   ", "normalized": None, "status": "absent"}
                self.assertEqual(build_rko_draft({**base, "printed_debit": None,
                                                  "printed_debit_observation": observed},
                                                 self.refs, self.config)["status"], "review_required")
                for bad_observation in [{**observed, "source_id": "another-event"},
                                        {**observed, "raw": "1"},
                                        {**observed, "normalized": "0"},
                                        {**observed, "status": "unknown"}]:
                    with self.subTest(selector=selector, bad_observation=bad_observation), self.assertRaises(CashRevenueError):
                        build_rko_draft({**base, "printed_debit": None,
                                         "printed_debit_observation": bad_observation}, self.refs, self.config)

    def test_raw_and_purpose_exclusions_match_cash_classifier(self):
        exclusions = ["Ошибочный платеж", "Финансовое управление", "Казначейство",
                      "Проценты банка", "Плата за остаток", "Возврат налогов",
                      "УСН 01201", "Раздел налога 01201", "Инспекция МНС",
                      "Выручка и займ", "Выручка по карточкам", "ЕРИП",
                      "Денежный перевод", "Операций: 3, комиссия", "Возврат комиссии"]
        for marker in exclusions:
            for location in ("purpose", "bank_row_raw"):
                with self.subTest(marker=marker, location=location), self.assertRaises(CashRevenueError):
                    facts = {**self.facts, "purpose": "" if location == "bank_row_raw" else "Выручка " + marker,
                             "cash_symbols": ["2004"]}
                    if location == "bank_row_raw":
                        facts["bank_row_raw"] = "Символ 2004 1250\n" + marker
                    build_rko_draft(facts, self.refs, self.config)
        for wording in ["Оплата от покупателя: выручка за услуги, в том числе НДС 20%",
                        "Выручка за услуги. Электронный документ"]:
            self.assertEqual(build_rko_draft({**self.facts, "purpose": wording},
                                             self.refs, self.config)["status"], "review_required")

    def test_bounded_textual_cid_symbol_prefix(self):
        raw = "(cid:27)4символ 2004 : 1250(cid:27)5"
        self.assertTrue(bank_row_in_rko_scope("2017-01-01", "", "credit", bank_row_raw=raw))
        self.assertEqual(build_rko_draft({**self.facts, "purpose": "", "bank_row_raw": raw}, self.refs, self.config)["status"], "review_required")
        for bad in ["(cid:27)4символ 20040", "(cid:27)4символ 2004.00", "prefix (cid:27)4символ 2004"]:
            self.assertFalse(bank_row_in_rko_scope("2017-01-01", "", "credit", bank_row_raw=bad))

    def test_symbol_raw_conflicts_and_nonbreaking_spaces(self):
        raw = "\u00a0Символ\u00a02004\u00a0: 1250"
        facts = {**self.facts, "purpose": "", "bank_row_raw": raw}
        self.assertEqual(build_rko_draft(facts, self.refs, self.config)["payload_draft"]["Комментарий"], "")
        for text in ["Карточки", "Займ", "Возврат комиссии"]:
            with self.subTest(text=text), self.assertRaises(CashRevenueError):
                build_rko_draft({**facts, "bank_row_raw": raw + "\n" + text}, self.refs, self.config)
        for label in ["Символ 20040", "Символ 2004.00", "Текст Символ 2004"]:
            with self.subTest(label=label), self.assertRaises(CashRevenueError):
                build_rko_draft({**facts, "bank_row_raw": label}, self.refs, self.config)

    def test_foreign_period_class_unknown_unit_and_unreconciled_source_hold(self):
        for key, bad in [("bank_direction", "debit"), ("purpose", "За платные услуги"), ("date", "2011-02-03"),
                         ("date", "2011-02-30T00:00:00"), ("operation_class", "card_receipt"),
                         ("operation_class", "supplier_payment"), ("currency_unit_evidence", ""),
                         ("overlap_evidence", ""), ("bank_account_correlation_evidence", ""), ("source_links", {}),
                         ("source_links", {"bank": ""})]:
            with self.subTest(key=key, bad=bad), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, key: bad}, self.refs, self.config)

    def test_mixed_loan_revenue_and_explicit_card_cannot_take_whole_row_amount(self):
        for purpose in ["Выручка и займ", "Взнос выручки, возврат займа",
                        "Выручка и заем", "Выручка по карточкам", "Выручка ЕРИП"]:
            with self.subTest(purpose=purpose), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "purpose": purpose}, self.refs, self.config)

    def test_no_guessed_references_or_negative_nan_boolean_amount(self):
        for amount in [0, -1, True, float("nan"), float("inf"), "12.50", 10 ** 1000]:
            with self.subTest(amount=amount), self.assertRaises(CashRevenueError):
                build_rko_draft({**self.facts, "amount": amount}, self.refs, self.config)
        for refs in [{}, {**self.refs, "cash_account": "50.1"},
                     {**self.refs, "bank_account": self.refs["cash_account"]},
                     {**self.refs, "currency": "00000000-0000-0000-0000-000000000000"}]:
            with self.assertRaises(CashRevenueError):
                build_rko_draft(self.facts, refs, self.config)

    def test_same_day_same_amount_events_remain_separate_and_input_unchanged(self):
        saved = copy.deepcopy(self.facts)
        first = build_rko_draft(self.facts, self.refs, self.config)
        second = build_rko_draft({**self.facts, "source_id": "synthetic-other-event",
                                  "source_links": {"bank": "synthetic-other-bank"}}, self.refs, self.config)
        self.assertNotEqual(first["source_id"], second["source_id"])
        self.assertEqual(self.facts, saved)


class RkoCleanupTests(unittest.TestCase):
    def setUp(self):
        self.start=[{'target_id':'syn-used','date':'2011-02-03','unpost_verified':True,
                     'posted':True,'deletion_mark':False,'fresh_version':True,'held':False},
                    {'target_id':'syn-remaining','date':'2011-02-04','unpost_verified':True,
                     'posted':False,'deletion_mark':False,'fresh_version':True,'held':False}]
        self.ids=['syn-source']
        self.results=[{'source_id':'syn-source','target_id':'syn-used','status':'loaded_verified',
                       'exact_source_target':True,'post_readback_verified':True}]
        self.proofs={k:True for k in ['full_starting_set','full_source_coverage','same_test_ledger',
                                     'claim_continuity','held_identity_resolved']}
    def gate(self):
        return rko_cleanup_candidates(self.start,self.ids,self.results,self.proofs,period_start=date(2010,1,1),period_end=date(2016,6,30))
    def test_final_cleanup_only_remaining_starting_set_after_complete_pass(self):
        value=self.gate()
        self.assertEqual(value['candidate_target_ids'],['syn-remaining'])
        self.assertEqual(value['status'],'review_required')
        self.assertFalse(value['can_apply'])
        self.assertNotIn('payload',value)
    def test_no_cleanup_during_unknown_pending_partial_or_missing_source(self):
        for status in ['unknown','candidate','pending','partial']:
            with self.subTest(status=status):
                self.results[0]['status']=status
                self.assertEqual(self.gate()['candidate_target_ids'],[])
        self.results=[]
        self.assertEqual(self.gate()['status'],'blocked')
    def test_any_incomplete_starting_unpost_claim_coverage_hold_blocks_cleanup(self):
        for key in self.proofs:
            with self.subTest(proof=key):
                self.proofs[key]=None
                self.assertEqual(self.gate()['candidate_target_ids'],[])
                self.proofs[key]=True
        for key,value in [('unpost_verified',False),('posted',True),('held',True),
                          ('fresh_version',False),('deletion_mark',True)]:
            with self.subTest(key=key):
                old=self.start[1][key];self.start[1][key]=value
                self.assertEqual(self.gate()['candidate_target_ids'],[])
                self.start[1][key]=old
    def test_receipt_and_exact_link_required_for_every_source(self):
        for key in ['exact_source_target','post_readback_verified']:
            self.results[0][key]=False
            self.assertEqual(self.gate()['status'],'blocked')
            self.results[0][key]=True
    def test_no_duplicate_sources_or_targets_or_foreign_period(self):
        for ids,results,start in [
            (self.ids*2,self.results,self.start),
            (self.ids,self.results*2,self.start),
            (self.ids,self.results,self.start*2),
            (self.ids,self.results,[{**self.start[1],'date':'2016-07-01'}]),
            (['syn-source','syn-source2'],self.results+[{**self.results[0],'source_id':'syn-source2'}],self.start)]:
            with self.assertRaises(CashRevenueError):
                rko_cleanup_candidates(start,ids,results,self.proofs,period_start=date(2010,1,1),period_end=date(2016,6,30))

if __name__ == "__main__":
    unittest.main()
