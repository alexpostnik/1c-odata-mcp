from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from onec_odata_mcp.ledger import UncertainWrite, WriteLedger, request_digest


def assert_inspection(
    inspection: dict | None,
    *,
    state: str,
    request_hash: str,
    result: dict | None,
) -> None:
    assert inspection is not None
    assert isinstance(inspection["created_at"], str)
    assert inspection["created_at"]
    assert {
        "state": inspection["state"],
        "request_hash": inspection["request_hash"],
        "result": inspection["result"],
    } == {
        "state": state,
        "request_hash": request_hash,
        "result": result,
    }


def test_receipt_blocks_uncertain_retry_and_reuses_completed_result(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    digest = request_digest("test-target", "Document_Example", {"Number": "1"})
    assert ledger.lookup("pilot-2026-0001", digest) is None
    assert ledger.inspect("pilot-2026-0001") is None
    assert ledger.reserve("pilot-2026-0001", digest) is None
    assert_inspection(
        ledger.inspect("pilot-2026-0001"),
        state="pending",
        request_hash=digest,
        result=None,
    )
    with pytest.raises(UncertainWrite, match="onec83_get_write_receipt / reconcile"):
        ledger.lookup("pilot-2026-0001", digest)
    with pytest.raises(UncertainWrite, match="onec83_get_write_receipt / reconcile"):
        ledger.reserve("pilot-2026-0001", digest)
    ledger.complete("pilot-2026-0001", {"Ref_Key": "example"})
    assert ledger.lookup("pilot-2026-0001", digest) == {"Ref_Key": "example"}
    assert ledger.reserve("pilot-2026-0001", digest) == {"Ref_Key": "example"}
    assert_inspection(
        ledger.inspect("pilot-2026-0001"),
        state="complete",
        request_hash=digest,
        result={"Ref_Key": "example"},
    )
    with pytest.raises(ValueError, match="different request"):
        ledger.reserve("pilot-2026-0001", request_digest("different"))
    with pytest.raises(ValueError, match="different request"):
        ledger.lookup("pilot-2026-0001", request_digest("different"))


def test_concurrent_first_reservations_initialize_sqlite_schema_once(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    entries = [
        (f"parallel-reserve-{index:04d}", request_digest("row", index))
        for index in range(12)
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda entry: ledger.reserve(*entry), entries))

    assert results == [None] * len(entries)
    assert [ledger.inspect(operation_id)["state"] for operation_id, _ in entries] == [
        "pending"
    ] * len(entries)


def test_failed_receipt_replays_like_completed_receipt(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    digest = request_digest("test-target", "Document_Example", {"Number": "2"})
    receipt = {
        "entitySet": "Document_Example",
        "operationId": "pilot-2026-0002",
        "status": 400,
        "message": "1C rejected the write",
    }

    assert ledger.reserve("pilot-2026-0002", digest) is None
    ledger.fail("pilot-2026-0002", receipt)

    assert ledger.lookup("pilot-2026-0002", digest) == receipt
    assert ledger.reserve("pilot-2026-0002", digest) == receipt
    assert_inspection(
        ledger.inspect("pilot-2026-0002"),
        state="failed",
        request_hash=digest,
        result=receipt,
    )
    with pytest.raises(ValueError, match="different request"):
        ledger.lookup("pilot-2026-0002", request_digest("different"))
    with pytest.raises(RuntimeError, match="missing or already finalized"):
        ledger.fail("pilot-2026-0002", receipt)


def test_reconcile_pending_requires_matching_digest(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    digest = request_digest("test-target", "Document_Example", {"Number": "3"})
    assert ledger.reserve("pilot-2026-0003", digest) is None

    with pytest.raises(ValueError, match="different request"):
        ledger.reconcile_pending(
            "pilot-2026-0003",
            request_digest("different"),
            "failed",
            {"message": "not created"},
        )

    assert_inspection(
        ledger.inspect("pilot-2026-0003"),
        state="pending",
        request_hash=digest,
        result=None,
    )


def test_record_pending_stores_diagnostic_without_finalizing(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    digest = request_digest("test-target", "Document_Example", {"Number": "7"})
    diagnostic = {
        "outcome": "unknown",
        "message": "transport timed out before confirmation",
        "operationId": "pilot-2026-0007",
    }

    assert ledger.reserve("pilot-2026-0007", digest) is None
    ledger.record_pending("pilot-2026-0007", digest, diagnostic)

    assert_inspection(
        ledger.inspect("pilot-2026-0007"),
        state="pending",
        request_hash=digest,
        result=diagnostic,
    )
    with pytest.raises(UncertainWrite, match="onec83_get_write_receipt / reconcile"):
        ledger.lookup("pilot-2026-0007", digest)
    with pytest.raises(UncertainWrite, match="onec83_get_write_receipt / reconcile"):
        ledger.reserve("pilot-2026-0007", digest)
    with pytest.raises(UncertainWrite) as repeat:
        ledger.lookup("pilot-2026-0007", digest)
    assert "Saved pending diagnostic" in str(repeat.value)
    assert "message=transport timed out before confirmation" in str(repeat.value)


def test_record_pending_requires_matching_digest_and_pending_state(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    pending_digest = request_digest("test-target", "Document_Example", {"Number": "8"})
    complete_digest = request_digest("test-target", "Document_Example", {"Number": "9"})
    diagnostic = {"message": "still unknown"}

    assert ledger.reserve("pilot-2026-0008", pending_digest) is None
    with pytest.raises(ValueError, match="different request"):
        ledger.record_pending("pilot-2026-0008", request_digest("different"), diagnostic)
    assert_inspection(
        ledger.inspect("pilot-2026-0008"),
        state="pending",
        request_hash=pending_digest,
        result=None,
    )

    assert ledger.reserve("pilot-2026-0009", complete_digest) is None
    ledger.complete("pilot-2026-0009", {"Ref_Key": "example"})
    with pytest.raises(RuntimeError, match="missing or already finalized"):
        ledger.record_pending("pilot-2026-0009", complete_digest, diagnostic)

    with pytest.raises(RuntimeError, match="missing or already finalized"):
        ledger.record_pending("pilot-2026-0010", pending_digest, diagnostic)


def test_reconcile_pending_marks_complete_or_failed(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    complete_digest = request_digest("test-target", "Document_Example", {"Number": "4"})
    failed_digest = request_digest("test-target", "Document_Example", {"Number": "5"})

    assert ledger.reserve("pilot-2026-0004", complete_digest) is None
    ledger.reconcile_pending(
        "pilot-2026-0004",
        complete_digest,
        "complete",
        {"Ref_Key": "created-after-manual-check"},
    )
    assert ledger.lookup("pilot-2026-0004", complete_digest) == {
        "Ref_Key": "created-after-manual-check",
    }

    assert ledger.reserve("pilot-2026-0005", failed_digest) is None
    ledger.reconcile_pending(
        "pilot-2026-0005",
        failed_digest,
        "failed",
        {"message": "definite 1C refusal was received"},
    )
    assert ledger.reserve("pilot-2026-0005", failed_digest) == {
        "message": "definite 1C refusal was received",
    }

    with pytest.raises(RuntimeError, match="missing or already finalized"):
        ledger.reconcile_pending(
            "pilot-2026-0005",
            failed_digest,
            "failed",
            {"message": "second reconcile must not rewrite receipts"},
        )


def test_reconcile_pending_rejects_invalid_state(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "receipts.sqlite3")
    digest = request_digest("test-target", "Document_Example", {"Number": "6"})
    assert ledger.reserve("pilot-2026-0006", digest) is None

    with pytest.raises(ValueError, match="state must be 'complete', 'failed', or 'reconciled'"):
        ledger.reconcile_pending("pilot-2026-0006", digest, "pending", {"message": "nope"})

    with pytest.raises(RuntimeError, match="missing or already finalized"):
        ledger.fail("pilot-2026-0007", {"message": "not reserved"})
