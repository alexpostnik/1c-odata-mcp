"""Synthetic counterexamples for target-bound year checkpoints."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from agent_bridge.core import Bridge
from agent_bridge.ledger import Ledger, LedgerError


class LedgerCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "ledger.sqlite3")
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def run_record(self, year, source="a", target="clean", stage="reconciled", suffix=""):
        key = f"{target}-{source}-{year}-{stage}-{suffix}"
        self.ledger.record_run(
            run_id=key, target_id=target, source_alias=source, dataset_kind="documents",
            mapping_version="v1", period_from=f"{year}-01-01",
            period_to_exclusive=f"{year + 1}-01-01", stage=stage,
            source_count=1, target_count=1, source_hash="source-" + key,
            target_hash="target-" + key, mapping_hash="map-" + key,
            idempotency_key=key)

    def evidence(self, year, target="clean", suffix=""):
        for category in ("documents", "movements", "trial_balance"):
            self.ledger.add_year_evidence(
                target_id=target, year=year, category=category,
                evidence_ref=f"synthetic-{category}-{suffix}",
                evidence_hash=f"hash-{category}-{suffix}",
                method="independent_reconciliation")

    def close_year(self, year, sources=("a",), target="clean"):
        return self.ledger.close_year(
            year, target_id=target, first_year=2012,
            required_coverage=[{"sourceAlias": source, "datasetKind": "documents"}
                               for source in sources])

    def mapping(self, source_key="object-1", target_ref="target-1",
                stage="source_page", idempotency_key="mapping-1"):
        return dict(target_id="clean", source_alias="a", dataset_kind="documents",
                    source_key=source_key, mapping_version="v1", target_ref=target_ref,
                    stage=stage, idempotency_key=idempotency_key)

    def assert_mapping_writer_serializes(self, winner, loser, error):
        entered = threading.Event()
        resume = threading.Event()
        outcome = []

        class PausingLedger(Ledger):
            def _record_mapping_locked(self, *args):
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("timed out waiting for competing mapping")
                return super()._record_mapping_locked(*args)

        def mapper():
            ledger = PausingLedger(self.path)
            try:
                ledger.record_mapping(**winner)
                outcome.append("ok")
            except Exception as exc:
                outcome.append(exc)
            finally:
                ledger.close()

        thread = threading.Thread(target=mapper)
        thread.start()
        try:
            self.assertTrue(entered.wait(5), "mapping did not reach locked validation")
            self.ledger.db.execute("PRAGMA busy_timeout=0")
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.ledger.record_mapping(**loser)
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["ok"])
        with self.assertRaisesRegex(LedgerError, error):
            self.ledger.record_mapping(**loser)
        stored = self.ledger.db.execute(
            "SELECT target_ref,stage,idempotency_key FROM mapping WHERE target_id=? "
            "AND source_alias=? AND dataset_kind=? AND source_key=? AND mapping_version=?",
            (winner["target_id"], winner["source_alias"], winner["dataset_kind"],
             winner["source_key"], winner["mapping_version"])).fetchone()
        self.assertEqual(stored, (winner["target_ref"], winner["stage"],
                                  winner["idempotency_key"]))

    def test_concurrent_mapping_cannot_regress_stage(self):
        self.ledger.record_mapping(**self.mapping())
        self.assert_mapping_writer_serializes(
            self.mapping(stage="reconciled"), self.mapping(stage="source_page"),
            "stage regression")

    def test_identical_mapping_replay_keeps_evidence_timestamp(self):
        values = self.mapping()
        self.ledger.record_mapping(**values)
        with patch("agent_bridge.ledger._utc", side_effect=AssertionError("replay changed time")):
            self.ledger.record_mapping(**values)

    def test_concurrent_mapping_cannot_change_target_ref_or_idempotency(self):
        self.assert_mapping_writer_serializes(
            self.mapping(source_key="ref-conflict", idempotency_key="ref-key"),
            self.mapping(source_key="ref-conflict", target_ref="other-target",
                         idempotency_key="ref-key"),
            "different target reference")
        self.assert_mapping_writer_serializes(
            self.mapping(source_key="idem-conflict", idempotency_key="idem-key"),
            self.mapping(source_key="idem-conflict", idempotency_key="other-idem-key"),
            "idempotency key mismatch")

    def test_unfinished_other_source_cannot_be_masked_by_reconciled_source(self):
        self.run_record(2012, source="a", stage="source_page")
        self.run_record(2012, source="b", stage="reconciled")
        self.evidence(2012)
        with self.assertRaisesRegex(LedgerError, "coverage"):
            self.close_year(2012, sources=("b",))
        with self.assertRaisesRegex(LedgerError, "unfinished"):
            self.close_year(2012, sources=("a", "b"))
        self.run_record(2012, source="a", stage="reconciled")
        self.evidence(2012, suffix="fresh")
        receipt = self.close_year(2012, sources=("a", "b"))
        self.assertEqual({r["sourceAlias"] for r in receipt["runBindings"]}, {"a", "b"})
        self.assertEqual(receipt["assertion"], "local_evidence_only")

    def test_target_specific_runs_evidence_checkpoints_and_mapping_invalidation(self):
        self.run_record(2012, target="clean")
        self.evidence(2012, target="clean")
        self.close_year(2012, target="clean")
        self.assertEqual(self.ledger.year_status(2012, target_id="other")["state"], "open")
        with self.assertRaises(LedgerError):
            self.close_year(2012, target="other")
        self.run_record(2012, target="other")
        self.evidence(2012, target="other")
        self.close_year(2012, target="other")
        self.ledger.record_mapping(target_id="clean", source_alias="a",
                                   dataset_kind="documents", source_key="object-1",
                                   mapping_version="v1", target_ref="target-1",
                                   stage="source_page", idempotency_key="mapping-1")
        self.assertEqual(self.ledger.year_status(2012, target_id="clean")["state"], "open")
        self.assertEqual(self.ledger.year_status(2012, target_id="other")["state"], "closed")
        with self.assertRaises(LedgerError):
            self.ledger.record_mapping(target_id="other", source_alias="a",
                                       dataset_kind="documents", source_key="object-1",
                                       mapping_version="v1", target_ref="target-1",
                                       stage="source_page", idempotency_key="mapping-1")

    def test_later_year_requires_evidence_after_previous_year_reclose(self):
        self.run_record(2012)
        self.evidence(2012)
        self.close_year(2012)
        self.run_record(2013)
        self.evidence(2013)
        receipt = self.close_year(2013)
        self.assertTrue(receipt["previousClosedAt"])
        self.run_record(2012, suffix="changed")
        self.assertEqual(self.ledger.year_status(2013, target_id="clean")["state"], "open")
        self.evidence(2012, suffix="new-2012")
        self.close_year(2012)
        with self.assertRaisesRegex(LedgerError, "previous year close"):
            self.close_year(2013)
        self.evidence(2013, suffix="new-2013")
        self.close_year(2013)
        self.assertEqual(self.ledger.year_status(2013, target_id="clean")["state"], "closed")

    def test_first_year_cannot_change_to_skip_reopened_predecessor(self):
        self.run_record(2012)
        self.evidence(2012)
        self.close_year(2012)
        self.run_record(2013)
        self.evidence(2013)
        self.close_year(2013)
        self.run_record(2012, suffix="changed")
        with self.assertRaisesRegex(LedgerError, "first year is immutable"):
            self.ledger.close_year(
                2013, target_id="clean", first_year=2013,
                required_coverage=[{"sourceAlias": "a", "datasetKind": "documents"}])
        self.assertEqual(self.ledger.year_status(2013, target_id="clean")["state"], "open")

    def test_close_reserves_writer_before_reading_mutable_evidence(self):
        self.run_record(2012)
        self.evidence(2012)
        entered = threading.Event()
        resume = threading.Event()
        outcome = []

        class PausingLedger(Ledger):
            def _close_year_locked(self, *args):
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("timed out waiting for concurrent writer check")
                return super()._close_year_locked(*args)

        def closer():
            ledger = PausingLedger(self.path)
            try:
                outcome.append(ledger.close_year(
                    2012, target_id="clean", first_year=2012,
                    required_coverage=[{"sourceAlias": "a", "datasetKind": "documents"}]))
            except Exception as exc:
                outcome.append(exc)
            finally:
                ledger.close()

        thread = threading.Thread(target=closer)
        thread.start()
        try:
            self.assertTrue(entered.wait(5), "close did not reach locked validation")
            self.ledger.db.execute("PRAGMA busy_timeout=0")
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.run_record(2012, suffix="concurrent")
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        self.assertEqual(self.ledger.year_status(2012, target_id="clean")["state"], "closed")
        self.run_record(2012, suffix="concurrent")
        self.assertEqual(self.ledger.year_status(2012, target_id="clean")["state"], "open")

    def test_bridge_fingerprint_changes_when_target_endpoint_changes(self):
        config = {"ledger": {"path": self.path}, "targets": {
            "clean": {"serviceUrl": "https://example.invalid/one", "environment": "test"}}}
        target_id = Bridge(config).year_status(2012, "clean")["targetId"]
        self.run_record(2012, target=target_id)
        self.evidence(2012, target=target_id)
        self.close_year(2012, target=target_id)
        first = Bridge(config).year_status(2012, "clean")
        self.assertEqual(first["state"], "closed")
        config["targets"]["clean"]["serviceUrl"] = "https://example.invalid/two"
        second = Bridge(config).year_status(2012, "clean")
        self.assertNotEqual(first["targetId"], second["targetId"])
        self.assertEqual(second["state"], "open")

    def test_legacy_unbound_ledger_fails_closed(self):
        old_path = str(Path(self.directory.name) / "old.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("CREATE TABLE run (run_id TEXT PRIMARY KEY, source_alias TEXT)")
        with self.assertRaisesRegex(LedgerError, "legacy ledger"):
            Ledger(old_path)

    def test_existing_checkpoint_without_fixed_start_year_fails_closed(self):
        old_path = str(Path(self.directory.name) / "old-target-bound.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("CREATE TABLE year_checkpoint (target_id TEXT, year INTEGER, state TEXT, "
                       "closed_at TEXT, evidence_json TEXT)")
            db.execute("INSERT INTO year_checkpoint VALUES ('clean', 2012, 'closed', 'time', '{}')")
        with self.assertRaisesRegex(LedgerError, "fixed migration first year"):
            Ledger(old_path)


if __name__ == "__main__":
    unittest.main()
