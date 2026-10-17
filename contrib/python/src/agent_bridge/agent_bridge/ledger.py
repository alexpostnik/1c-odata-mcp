"""Local, target-bound migration evidence ledger; never an accounting verdict.

Only opaque references, hashes and locally asserted stages are stored. A closed
checkpoint means that the declared coverage and evidence passed these local
checks; it does not verify the books or authorize a target write.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


class LedgerError(ValueError):
    pass


_STAGES = ("source_inventory", "source_page", "mapped_preview", "target_readback", "reconciled")
_EVIDENCE = ("documents", "movements", "trial_balance")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{name} required")
    return value


def year_period(year: int) -> tuple[str, str]:
    if type(year) is not int or not 1900 <= year < 9999:
        raise LedgerError("invalid year")
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


class Ledger:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        # Old ledgers have no target identity. There is no safe way to attribute
        # their records to a target, so require a fresh ledger instead of guessing.
        for table in ("mapping", "run", "year_evidence", "year_checkpoint"):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if columns and "target_id" not in columns:
                self.db.close()
                raise LedgerError("legacy ledger has unbound target evidence; use a new ledger path")
        boundary_columns = {row[1] for row in self.db.execute("PRAGMA table_info(target_migration)")}
        if not boundary_columns and self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='year_checkpoint'").fetchone():
            if self.db.execute("SELECT 1 FROM year_checkpoint LIMIT 1").fetchone():
                self.db.close()
                raise LedgerError("legacy ledger lacks fixed migration first year; use a new ledger path")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS mapping (
              target_id TEXT NOT NULL, source_alias TEXT NOT NULL,
              dataset_kind TEXT NOT NULL, source_key TEXT NOT NULL,
              mapping_version TEXT NOT NULL, target_ref TEXT NOT NULL,
              stage TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(target_id, source_alias, dataset_kind, source_key, mapping_version));
            CREATE TABLE IF NOT EXISTS run (
              run_id TEXT PRIMARY KEY, target_id TEXT NOT NULL,
              source_alias TEXT NOT NULL, dataset_kind TEXT NOT NULL,
              mapping_version TEXT NOT NULL, period_from TEXT NOT NULL,
              period_to_exclusive TEXT NOT NULL, stage TEXT NOT NULL,
              source_count INTEGER NOT NULL, target_count INTEGER NOT NULL,
              source_hash TEXT NOT NULL, target_hash TEXT NOT NULL,
              mapping_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
              recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS year_evidence (
              target_id TEXT NOT NULL, year INTEGER NOT NULL,
              category TEXT NOT NULL, evidence_ref TEXT NOT NULL,
              evidence_hash TEXT NOT NULL, method TEXT NOT NULL,
              verified_at TEXT NOT NULL,
              PRIMARY KEY(target_id, year, category));
            CREATE TABLE IF NOT EXISTS year_checkpoint (
              target_id TEXT NOT NULL, year INTEGER NOT NULL,
              state TEXT NOT NULL, closed_at TEXT, evidence_json TEXT,
              PRIMARY KEY(target_id, year));
            CREATE TABLE IF NOT EXISTS target_migration (
              target_id TEXT PRIMARY KEY, first_year INTEGER NOT NULL);
        """)

    def close(self) -> None:
        self.db.close()

    def record_mapping(self, *, target_id: str, source_alias: str, dataset_kind: str,
                       source_key: str, mapping_version: str, target_ref: str,
                       stage: str, idempotency_key: str) -> None:
        if stage not in _STAGES:
            raise LedgerError("unknown stage")
        for name, value in (("target_id", target_id), ("source_alias", source_alias),
                            ("dataset_kind", dataset_kind), ("source_key", source_key),
                            ("mapping_version", mapping_version), ("target_ref", target_ref),
                            ("idempotency_key", idempotency_key)):
            _required(value, name)
        # Lock before reading current stage/ref or idempotency binding. An
        # unlocked read followed by ON CONFLICT can overwrite a newer stage.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._record_mapping_locked(target_id, source_alias, dataset_kind,
                                        source_key, mapping_version, target_ref,
                                        stage, idempotency_key)

    def _record_mapping_locked(self, target_id: str, source_alias: str,
                               dataset_kind: str, source_key: str,
                               mapping_version: str, target_ref: str,
                               stage: str, idempotency_key: str) -> None:
        current = self.db.execute(
            "SELECT target_ref,stage,idempotency_key FROM mapping WHERE "
            "target_id=? AND source_alias=? AND dataset_kind=? AND source_key=? AND mapping_version=?",
            (target_id, source_alias, dataset_kind, source_key, mapping_version)).fetchone()
        if current and current[0] != target_ref:
            raise LedgerError("same source key maps to a different target reference")
        if current and _STAGES.index(stage) < _STAGES.index(current[1]):
            raise LedgerError("stage regression")
        if current and current[2] != idempotency_key:
            raise LedgerError("idempotency key mismatch")
        bound = self.db.execute(
            "SELECT target_id,source_alias,dataset_kind,source_key,mapping_version FROM mapping "
            "WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if bound and bound != (target_id, source_alias, dataset_kind, source_key, mapping_version):
            raise LedgerError("idempotency key already bound to a different mapping")
        if current and current[1] == stage:
            return
        self.db.execute(
            "INSERT INTO mapping VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT("
            "target_id,source_alias,dataset_kind,source_key,mapping_version) DO UPDATE SET "
            "stage=excluded.stage,updated_at=excluded.updated_at",
            (target_id, source_alias, dataset_kind, source_key, mapping_version,
             target_ref, stage, idempotency_key, _utc()))
        if not current or current[1] != stage:
            self.db.execute("UPDATE year_checkpoint SET state='open',closed_at=NULL,evidence_json=NULL "
                            "WHERE target_id=? AND state='closed'", (target_id,))

    def record_run(self, *, run_id: str, target_id: str, source_alias: str,
                   dataset_kind: str, mapping_version: str, period_from: str,
                   period_to_exclusive: str, stage: str, source_count: int,
                   target_count: int, source_hash: str, target_hash: str,
                   mapping_hash: str, idempotency_key: str) -> None:
        if stage not in _STAGES:
            raise LedgerError("unknown stage")
        for name, value in (("run_id", run_id), ("target_id", target_id),
                            ("source_alias", source_alias), ("dataset_kind", dataset_kind),
                            ("mapping_version", mapping_version), ("source_hash", source_hash),
                            ("target_hash", target_hash), ("mapping_hash", mapping_hash),
                            ("idempotency_key", idempotency_key)):
            _required(value, name)
        try:
            start, end = date.fromisoformat(period_from), date.fromisoformat(period_to_exclusive)
        except (TypeError, ValueError) as exc:
            raise LedgerError("invalid run period") from exc
        if (start >= end or start.isoformat() != period_from or end.isoformat() != period_to_exclusive
                or type(source_count) is not int or type(target_count) is not int
                or min(source_count, target_count) < 0):
            raise LedgerError("invalid run counts or period")
        values = (run_id, target_id, source_alias, dataset_kind, mapping_version,
                  period_from, period_to_exclusive, stage, source_count, target_count,
                  source_hash, target_hash, mapping_hash, idempotency_key)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._record_run_locked(values)

    def _record_run_locked(self, values: tuple) -> None:
        target_id, period_from, idempotency_key = values[1], values[5], values[13]
        existing = self.db.execute(
            "SELECT run_id,target_id,source_alias,dataset_kind,mapping_version,period_from,"
            "period_to_exclusive,stage,source_count,target_count,source_hash,target_hash,"
            "mapping_hash,idempotency_key FROM run WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
        if existing:
            if existing != values:
                raise LedgerError("idempotency key already has different run data")
            return
        self.db.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values + (_utc(),))
        self._invalidate_from_date(target_id, period_from)

    def _invalidate_from_date(self, target_id: str, from_date: str) -> None:
        year = date.fromisoformat(from_date).year
        self.db.execute("UPDATE year_checkpoint SET state='open',closed_at=NULL,evidence_json=NULL "
                        "WHERE target_id=? AND year>=? AND state='closed'", (target_id, year))

    def add_year_evidence(self, *, target_id: str, year: int, category: str,
                          evidence_ref: str, evidence_hash: str, method: str) -> None:
        _required(target_id, "target_id")
        year_period(year)
        if category not in _EVIDENCE:
            raise LedgerError("invalid year evidence category")
        _required(evidence_ref, "evidence_ref")
        _required(evidence_hash, "evidence_hash")
        if method not in ("independent_reconciliation", "manual_verification"):
            raise LedgerError("independent verification required")
        with self.db:
            self.db.execute(
                "INSERT INTO year_evidence VALUES (?,?,?,?,?,?,?) ON CONFLICT(target_id,year,category) "
                "DO UPDATE SET evidence_ref=excluded.evidence_ref,evidence_hash=excluded.evidence_hash,"
                "method=excluded.method,verified_at=excluded.verified_at",
                (target_id, year, category, evidence_ref, evidence_hash, method, _utc()))
            self.db.execute("UPDATE year_checkpoint SET state='open',closed_at=NULL,evidence_json=NULL "
                            "WHERE target_id=? AND year>=? AND state='closed'", (target_id, year))

    def close_year(self, year: int, *, target_id: str, first_year: int,
                   required_coverage: list[dict[str, str]]) -> dict:
        """Close only an exact, target-specific source/dataset coverage matrix.

        Each coverage entry is {"sourceAlias": ..., "datasetKind": ...}. The
        caller asserts its completeness; the ledger rejects unlisted year runs
        but cannot independently discover absent sources or accounting objects.
        """
        _required(target_id, "target_id")
        start, end = year_period(year)
        year_period(first_year)
        if year < first_year:
            raise LedgerError("year precedes migration start")
        if not isinstance(required_coverage, list) or not required_coverage:
            raise LedgerError("explicit source/dataset coverage matrix required")
        try:
            required = [(_required(item["sourceAlias"], "sourceAlias"),
                         _required(item["datasetKind"], "datasetKind"))
                        for item in required_coverage if isinstance(item, dict)]
        except KeyError as exc:
            raise LedgerError("invalid source/dataset coverage matrix") from exc
        if len(required) != len(required_coverage) or len(required) != len(set(required)):
            raise LedgerError("invalid or duplicate source/dataset coverage")
        # Reserve the SQLite writer slot before inspecting any mutable evidence.
        # A concurrent run/evidence insert will serialize after the checkpoint
        # and invalidate it, rather than slip between validation and the write.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self._close_year_locked(year, target_id, first_year, start, end, required)

    def _close_year_locked(self, year: int, target_id: str, first_year: int,
                           start: str, end: str, required: list[tuple[str, str]]) -> dict:
        boundary = self.db.execute(
            "SELECT first_year FROM target_migration WHERE target_id=?", (target_id,)).fetchone()
        if boundary and boundary[0] != first_year:
            raise LedgerError("migration first year is immutable for this target")
        if not boundary:
            existing = self.db.execute(
                "SELECT 1 FROM year_checkpoint WHERE target_id=? LIMIT 1", (target_id,)).fetchone()
            if existing:
                raise LedgerError("target checkpoints lack a fixed migration first year; use a new ledger")
        previous_closed_at = None
        if year > first_year:
            previous = self.db.execute(
                "SELECT state,closed_at FROM year_checkpoint WHERE target_id=? AND year=?",
                (target_id, year - 1)).fetchone()
            if not previous or previous[0] != "closed" or not previous[1]:
                raise LedgerError("previous year is not closed for this target")
            previous_closed_at = previous[1]
        evidence = self.db.execute(
            "SELECT category,evidence_ref,evidence_hash,method,verified_at FROM year_evidence "
            "WHERE target_id=? AND year=?", (target_id, year)).fetchall()
        if {row[0] for row in evidence} != set(_EVIDENCE):
            raise LedgerError("documents, movements and trial balance reconciliation are required")
        # Any assertion that precedes the previous year's latest close is stale
        # even if no current-year run has changed.
        if previous_closed_at and any(row[4] <= previous_closed_at for row in evidence):
            raise LedgerError("year evidence predates previous year close")
        newest_run = self.db.execute(
            "SELECT MAX(recorded_at) FROM run WHERE target_id=? AND period_from<? "
            "AND period_to_exclusive>?", (target_id, end, start)).fetchone()[0]
        newest_mapping = self.db.execute(
            "SELECT MAX(updated_at) FROM mapping WHERE target_id=?", (target_id,)).fetchone()[0]
        newest_input = max(filter(None, (newest_run, newest_mapping)), default=None)
        if newest_input and any(row[4] <= newest_input for row in evidence):
            raise LedgerError("reconciliation evidence predates a source or mapping change")
        runs = self.db.execute(
            "SELECT source_alias,dataset_kind,stage,source_hash,target_hash,mapping_hash,"
            "mapping_version,run_id,period_from,period_to_exclusive FROM run "
            "WHERE target_id=? AND period_from<? AND period_to_exclusive>? ORDER BY rowid",
            (target_id, end, start)).fetchall()
        latest = {(r[0], r[1]): r for r in runs}
        if set(latest) != set(required):
            raise LedgerError("observed source/dataset runs differ from required coverage")
        if any(row[2] != "reconciled" or row[8:10] != (start, end)
               for row in latest.values()):
            raise LedgerError("full calendar year has unfinished source/dataset runs")
        receipt = {
            "year": year, "targetId": target_id,
            "period": {"from": start, "toExclusive": end},
            "evidence": [{"category": a, "reference": b, "hash": c, "method": d}
                         for a, b, c, d, _ in evidence],
            "requiredCoverage": [{"sourceAlias": a, "datasetKind": b}
                                 for a, b in sorted(required)],
            "previousClosedAt": previous_closed_at,
            "runBindings": [dict(sourceAlias=r[0], datasetKind=r[1], sourceHash=r[3],
                                 targetHash=r[4], mappingHash=r[5], mappingVersion=r[6],
                                 runId=r[7]) for _, r in sorted(latest.items())],
            "assertion": "local_evidence_only",
        }
        if not boundary:
            self.db.execute("INSERT INTO target_migration VALUES (?,?)", (target_id, first_year))
        self.db.execute(
            "INSERT INTO year_checkpoint VALUES (?,?,?,?,?) ON CONFLICT(target_id,year) DO UPDATE SET "
            "state=excluded.state,closed_at=excluded.closed_at,evidence_json=excluded.evidence_json",
            (target_id, year, "closed", _utc(), json.dumps(receipt, sort_keys=True)))
        self.db.execute("UPDATE year_checkpoint SET state='open',closed_at=NULL,evidence_json=NULL "
                        "WHERE target_id=? AND year>? AND state='closed'", (target_id, year))
        return receipt

    def year_status(self, year: int, *, target_id: str) -> dict:
        _required(target_id, "target_id")
        start, end = year_period(year)
        checkpoint = self.db.execute(
            "SELECT state,evidence_json FROM year_checkpoint WHERE target_id=? AND year=?",
            (target_id, year)).fetchone()
        if checkpoint and checkpoint[0] == "closed" and not self.db.execute(
                "SELECT 1 FROM target_migration WHERE target_id=?", (target_id,)).fetchone():
            raise LedgerError("checkpoint lacks fixed migration first year")
        categories = [r[0] for r in self.db.execute(
            "SELECT category FROM year_evidence WHERE target_id=? AND year=?",
            (target_id, year))]
        return {"year": year, "targetId": target_id,
                "period": {"from": start, "toExclusive": end},
                "state": checkpoint[0] if checkpoint else "open",
                "evidenceCategories": sorted(categories),
                "receipt": json.loads(checkpoint[1]) if checkpoint and checkpoint[1] else None}
