"""Local write receipt store. An uncertain POST is never retried automatically."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping


class UncertainWrite(RuntimeError):
    """An earlier attempt may have reached 1C; reconcile before trying again."""


_TERMINAL_STATES = {"complete", "failed", "reconciled"}
_UNCERTAIN_WRITE_MESSAGE = (
    "A previous write with this operation_id may have reached 1C; "
    "check 1C and use onec83_get_write_receipt / reconcile before "
    "starting a new logical attempt."
)
_MAX_PENDING_DETAIL_CHARS = 600


def request_digest(*parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def counterparty_pre_post_result(target: str, digest: str) -> dict:
    """Exact initial marker, committed before any possible counterparty POST."""
    return {"entitySet": "Catalog_Контрагенты", "targetFingerprint": target,
            "requestHash": digest, "counterpartyCreateStage": "pre_post"}


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise ValueError("operation_id must be a stable 8..128 character identifier")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in operation_id):
        raise ValueError("operation_id must contain printable ASCII without spaces")


def _decode_result(result_json: str | None) -> dict | None:
    if result_json is None:
        return None
    result = json.loads(result_json)
    if result is not None and not isinstance(result, dict):
        raise RuntimeError("write receipt result must be a JSON object")
    return result


def _pending_message(result: dict | None) -> str:
    if not result:
        return _UNCERTAIN_WRITE_MESSAGE + " No saved diagnostic is available for this pending receipt."
    details = []
    write_state = result.get("writeState")
    if isinstance(write_state, str) and write_state:
        details.append(f"saved writeState={write_state}")
    error = result.get("error")
    if isinstance(error, dict):
        status = error.get("httpStatus")
        if isinstance(status, int):
            details.append(f"httpStatus={status}")
        code = error.get("code")
        if isinstance(code, str) and code:
            details.append(f"code={code}")
        message = error.get("message")
        if isinstance(message, str) and message:
            details.append(f"message={message}")
    elif isinstance(result.get("message"), str) and result["message"]:
        details.append(f"message={result['message']}")
    if not details:
        return _UNCERTAIN_WRITE_MESSAGE + " Saved pending receipt has no error message."
    detail = "; ".join(details)
    if len(detail) > _MAX_PENDING_DETAIL_CHARS:
        detail = detail[: _MAX_PENDING_DETAIL_CHARS - 1].rstrip() + "..."
    return f"{_UNCERTAIN_WRITE_MESSAGE} Saved pending diagnostic: {detail}"


class WriteLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get(
            "ONEC83_LEDGER_PATH",
            Path.home() / "Library" / "Application Support" / "onec-odata-mcp" / "ledger.sqlite3",
        ))
        self._schema_lock = threading.Lock()
        self._schema_initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        if not self._schema_initialized:
            try:
                with self._schema_lock:
                    if not self._schema_initialized:
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            "CREATE TABLE IF NOT EXISTS writes ("
                            "operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                            "state TEXT NOT NULL, result_json TEXT, "
                            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                        )
                        conn.execute(
                            "CREATE TABLE IF NOT EXISTS counterparty_creates ("
                            "claim_hash TEXT PRIMARY KEY, operation_id TEXT NOT NULL)"
                        )
                        columns = {row[1] for row in conn.execute("PRAGMA table_info(counterparty_creates)")}
                        for column in ("target_fingerprint", "request_hash"):
                            if column not in columns:
                                conn.execute(f"ALTER TABLE counterparty_creates ADD COLUMN {column} TEXT")
                        conn.commit()
                        if self.path.exists():
                            self.path.chmod(0o600)
                        self._schema_initialized = True
            except Exception:
                conn.close()
                raise
        return conn

    def lookup(self, operation_id: str, digest: str, *,
               prepared_result: dict | None = None) -> dict | None:
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_hash, state, result_json FROM writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        if row[0] != digest:
            raise ValueError("operation_id was already used for a different request")
        if row[1] in _TERMINAL_STATES:
            return _decode_result(row[2])
        if (row[1] == "prepared" and prepared_result is not None
                and _decode_result(row[2]) == prepared_result):
            # This is only a replay precheck. Claim proof and lifetime locks
            # are still required before reserve can resume the operation.
            return None
        raise UncertainWrite(_pending_message(_decode_result(row[2])))

    def reserve(self, operation_id: str, digest: str, *,
                prepared_result: dict | None = None) -> dict | None:
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_hash, state, result_json FROM writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("operation_id was already used for a different request")
                if row[1] in _TERMINAL_STATES:
                    return _decode_result(row[2])
                if (row[1] == "prepared" and prepared_result is not None
                        and _decode_result(row[2]) == prepared_result):
                    return None
                raise UncertainWrite(_pending_message(_decode_result(row[2])))
            conn.execute(
                "INSERT INTO writes(operation_id, request_hash, state, result_json) VALUES (?, ?, ?, ?)",
                (operation_id, digest, "prepared" if prepared_result is not None else "pending",
                 json.dumps(prepared_result, ensure_ascii=False, sort_keys=True)
                 if prepared_result is not None else None),
            )
        return None

    def recover_counterparty_pre_post(self, *, target: str,
                                      target_bindings: Mapping[str, str],
                                      operation_id: str) -> None:
        """Retire exact abandoned preparations under all peer lifetime locks.

        The same operation stays resumable. A different operation retains its
        receipt as a definite pre-POST failure; never erase history to recover.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT operation_id, request_hash, result_json FROM writes WHERE state='prepared'"
            ).fetchall()
            for prior_op, digest, encoded in rows:
                result = _decode_result(encoded)
                raw = result.get("targetFingerprint") if result else None
                if not isinstance(raw, str) or target_bindings.get(raw) != target:
                    continue
                if result != counterparty_pre_post_result(raw, digest):
                    continue
                claim = conn.execute(
                    "SELECT 1 FROM counterparty_creates WHERE operation_id=? "
                    "AND target_fingerprint=? AND request_hash=?",
                    (prior_op, target, digest),
                ).fetchone()
                if claim and prior_op != operation_id:
                    conn.execute(
                        "UPDATE writes SET state='failed', result_json=? WHERE operation_id=? AND state='prepared'",
                        (json.dumps({**result, "writeState": "failed", "phase": "recovered_pre_post"},
                                    ensure_ascii=False, sort_keys=True), prior_op),
                    )

    @contextmanager
    def counterparty_lock(self) -> Iterator[None]:
        """Process-lifetime exclusion; the empty sidecar stores no receipts.

        Do not lock SQLite's own file: flock and SQLite byte-range locks can
        interact on some systems. The OS releases this lock on process death.
        """
        try:
            import fcntl
        except ImportError:
            raise PermissionError("Counterparty Create requires a supported process lock") from None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(str(self.path) + ".counterparty.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "rb") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UncertainWrite("Another counterparty Create is active in this journal") from None
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def reserve_counterparty(self, claim: str, operation_id: str, *,
                             locked_peers: tuple[WriteLedger, ...] = (),
                             target: str | None = None, request_hash: str | None = None,
                             target_bindings: Mapping[str, str] | None = None,
                             claim_aliases: frozenset[str] = frozenset(),
                             claim_targets: Mapping[str, str] | None = None) -> None:
        """Reserve one target/UNP; old opaque identities remain conservative holds.

        Binding columns are additive. Ordinary receipts and their hashes are
        never rewritten; unknown historical authorities cannot be reverse-hashed.
        """
        _validate_operation_id(operation_id)
        bindings = target_bindings or {}
        aliases = claim_aliases | {claim}

        def receipt_target(receipt: dict | None) -> str | None:
            result = (receipt or {}).get("result") or {}
            if not isinstance(result, dict):
                return None
            raw = result.get("targetFingerprint")
            evidence = result.get("counterpartyUniqueness")
            saved = evidence.get("targetFingerprint") if isinstance(evidence, dict) else None
            known = [bindings.get(value) if isinstance(value, str) else None
                     for value in (raw, saved) if value is not None]
            if not known or any(value is None or value != known[0] for value in known):
                return None
            return known[0]

        def no_possible_post(peer: WriteLedger, prior_operation: str,
                             prior_request: str | None) -> bool:
            receipt = peer.inspect(prior_operation)
            if receipt is None:
                return True
            bound_target = receipt_target(receipt)
            if bound_target is not None and bound_target != target:
                return True
            # A different request or missing target cannot prove a pre-POST orphan.
            exact_request = prior_request is not None and receipt["request_hash"] == prior_request
            result = receipt.get("result") or {}
            prepared = (receipt["state"] == "prepared" and result == counterparty_pre_post_result(
                result.get("targetFingerprint"), receipt["request_hash"]))
            return (bound_target == target and exact_request
                    and (receipt["state"] == "failed" or prepared))

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claims = conn.execute(
                "SELECT claim_hash, operation_id, target_fingerprint, request_hash "
                "FROM counterparty_creates"
            ).fetchall()
            pending = conn.execute(
                "SELECT operation_id, request_hash, result_json FROM writes WHERE state IN ('pending','prepared')"
            ).fetchall()
            for pending_op, pending_hash, encoded in pending:
                result = _decode_result(encoded)
                if result and result.get("entitySet") not in {None, "Catalog_Контрагенты"}:
                    continue
                bound = receipt_target({"result": result})
                if target is not None and bound is not None and bound != target:
                    continue
                indexed = any(op == pending_op and (
                    (saved_hash == pending_hash and saved_target == bound and bound is not None)
                    or (saved_hash is None and saved_target is None)
                ) for _, op, saved_target, saved_hash in claims)
                if not indexed:
                    raise UncertainWrite(
                        "An earlier unindexed Create may concern this counterparty; "
                        "reconcile the original pending receipt before a new Create"
                    )
            for prior_claim, prior_op, prior_target, prior_request in claims:
                if prior_target is not None and (
                    not isinstance(prior_target, str) or len(prior_target) != 64
                    or any(ch not in "0123456789abcdef" for ch in prior_target)
                ):
                    raise UncertainWrite("A historical counterparty claim has malformed target binding")
                if target is None:
                    if prior_claim != claim:
                        continue
                else:
                    # A peer receipt sharing only operation_id cannot bind an
                    # old opaque claim: that receipt may belong to another base.
                    bound = prior_target or (claim_targets or {}).get(prior_claim)
                    if bound is None and prior_claim in aliases:
                        bound = target
                    if bound is not None and bound != target:
                        continue
                    if bound == target and prior_claim not in aliases:
                        continue
                    if bound is None and prior_claim not in aliases:
                        raise UncertainWrite("A historical counterparty claim has unknown target binding")
                # Every possibly matching receipt must prove absence or a
                # definite failure while all peer lifetime locks are held.
                orphan = bool(locked_peers) and all(
                    no_possible_post(peer, prior_op, prior_request) for peer in locked_peers
                )
                local = self.inspect(prior_op)
                failed = (target is None and local is not None and local["state"] == "failed")
                if orphan or failed:
                    conn.execute("DELETE FROM counterparty_creates WHERE claim_hash=?", (prior_claim,))
                else:
                    raise UncertainWrite(
                        "Counterparty UNP already has a reserved or accepted Create in this target; "
                        "reconcile the original operation before another logical attempt"
                    )
            conn.execute(
                "INSERT INTO counterparty_creates"
                "(claim_hash, operation_id, target_fingerprint, request_hash) VALUES (?, ?, ?, ?)",
                (claim, operation_id, target, request_hash),
            )

    def release_counterparty(self, claim: str, operation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM counterparty_creates WHERE claim_hash=? AND operation_id=?",
                (claim, operation_id),
            )

    def complete(self, operation_id: str, result: dict) -> None:
        self._finalize_pending(operation_id, "complete", result)

    def fail(self, operation_id: str, result: dict) -> None:
        self._finalize_pending(operation_id, "failed", result)

    def record_pending(self, operation_id: str, digest: str, result: dict) -> None:
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_hash, state, result_json FROM writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("write receipt is missing or already finalized")
            if row[0] != digest:
                raise ValueError("operation_id was already used for a different request")
            if row[1] not in {"pending", "prepared"}:
                raise RuntimeError("write receipt is missing or already finalized")
            saved = _decode_result(row[2]) or {}
            if "counterpartyUniqueness" in saved:
                result = {**result, "counterpartyUniqueness": saved["counterpartyUniqueness"]}
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
            cur = conn.execute(
                "UPDATE writes SET state='pending', result_json=? "
                "WHERE operation_id=? AND state IN ('pending','prepared') AND request_hash=?",
                (encoded, operation_id, digest),
            )
            if cur.rowcount != 1:
                raise RuntimeError("write receipt is missing or already finalized")

    def inspect(self, operation_id: str) -> dict | None:
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_hash, state, result_json, created_at FROM writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "state": row[1],
            "request_hash": row[0],
            "result": _decode_result(row[2]),
            "created_at": row[3],
        }

    def reconcile_pending(self, operation_id: str, digest: str, state: str, result: dict) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("state must be 'complete', 'failed', or 'reconciled'")
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_hash, state, result_json FROM writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("write receipt is missing or already finalized")
            if row[0] != digest:
                raise ValueError("operation_id was already used for a different request")
            if row[1] != "pending":
                raise RuntimeError("write receipt is missing or already finalized")
            saved = _decode_result(row[2]) or {}
            if "counterpartyUniqueness" in saved:
                result = {**result, "counterpartyUniqueness": saved["counterpartyUniqueness"]}
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
            cur = conn.execute(
                "UPDATE writes SET state=?, result_json=? "
                "WHERE operation_id=? AND state='pending' AND request_hash=?",
                (state, encoded, operation_id, digest),
            )
            if cur.rowcount != 1:
                raise RuntimeError("write receipt is missing or already finalized")

    def _finalize_pending(self, operation_id: str, state: str, result: dict) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("state must be 'complete' or 'failed'")
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, result_json FROM writes WHERE operation_id=? AND state IN ('pending','prepared')",
                (operation_id,),
            ).fetchone()
            if row and row[0] == "prepared" and state != "failed":
                raise RuntimeError("a prepared counterparty Create has not entered transport")
            saved = (_decode_result(row[1]) or {}) if row else {}
            if "counterpartyUniqueness" in saved:
                result = {**result, "counterpartyUniqueness": saved["counterpartyUniqueness"]}
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
            cur = conn.execute(
                "UPDATE writes SET state=?, result_json=? "
                "WHERE operation_id=? AND state IN ('pending','prepared')",
                (state, encoded, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("write receipt is missing or already finalized")
