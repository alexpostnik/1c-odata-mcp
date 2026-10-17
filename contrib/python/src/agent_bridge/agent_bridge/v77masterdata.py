"""Bounded catalog and constants inventory for 1C 7.7.

The inventory stores metadata names, cursors, row counts and aggregate hashes in
a local SQLite file outside Git. Source rows and values are hashed in memory and
discarded before receipts are produced.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Protocol


class MasterdataInventoryError(ValueError):
    pass


class MasterdataBridge(Protocol):
    def source_metadata_inventory(self, source_alias: str) -> dict: ...
    def source_read_page(self, source_alias: str, kind: str, type_name: str,
                         from_date: str | None = None,
                         to_exclusive: str | None = None,
                         cursor: str | None = None,
                         limit: int = 100) -> dict: ...
    def source_read_constants_page(self, source_alias: str,
                                   cursor: str | None = None,
                                   limit: int = 100) -> dict: ...
    def source_read_periodic_history_page(self, source_alias: str, kind: str,
                                          type_name: str, from_date: str,
                                          to_exclusive: str,
                                          attribute_name: str | None = None,
                                          owner_cursor: str | None = None,
                                          history_cursor: str | None = None,
                                          limit: int = 100) -> dict: ...


_HASH_SEED = hashlib.sha256(b"agent_bridge.v77masterdata.dataset.v1").hexdigest()
_INVENTORY_STATUSES = {"running", "partial", "blocked"}
_TASK_STATUSES = {"pending", "running", "waiting_catalog", "verified", "unsupported", "skipped", "blocked"}
_ACTIVE_TASK_STATUSES = {"pending", "running"}
_MAX_PAGE_BUDGET = 25
_MAX_LIMIT = 100
_MAX_SECONDS = 240
_CURSOR_MAXIMUM = 10_000_000
_HISTORY_CURSOR_MAXIMUM = 1_000_000
_CATALOG_PAGE_COVERAGE = {
    "configured-fields-unverified",
    "metadata-driven-unverified",
    "partial-unsupported-values",
}
_CONSTANT_PAGE_COVERAGE = {
    "provisional-constant-context",
    "partial-unsupported-values",
    "unsupported-constant-context",
}
_FILLED_CATALOG_SELECTION_SOURCES = {
    "caller-provided-filled-catalog-list",
    "live-catalog-presence-scan",
}
_HISTORY_PAGE_COVERAGE = {
    "metadata-linked-complete-unverified",
    "partial-unsupported-values",
    "unsupported-periodic-context",
    "partial-owner-scan-budget",
}
_TASK_ORDER_SQL = (
    "CASE task_kind "
    "WHEN 'catalog' THEN 1 "
    "WHEN 'constants' THEN 2 "
    "WHEN 'constant_history' THEN 3 "
    "WHEN 'catalog_attribute_history' THEN 4 "
    "ELSE 9 END"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _extend_hash(current: str, rows: list[object]) -> str:
    digest = bytes.fromhex(current)
    for row in rows:
        row_digest = hashlib.sha256(_canonical(row)).digest()
        digest = hashlib.sha256(digest + row_digest).digest()
    return digest.hex()


def _date(value: str, name: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise MasterdataInventoryError(f"{name} must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise MasterdataInventoryError(f"{name} must be ISO YYYY-MM-DD")
    return value


def _period(from_date: str, to_exclusive: str) -> tuple[str, str]:
    start = _date(from_date, "from_date")
    end = _date(to_exclusive, "to_exclusive")
    if start >= end:
        raise MasterdataInventoryError("period must be nonempty")
    return start, end


def _positive_int(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise MasterdataInventoryError(f"{name} must be 1..{maximum}")
    return value


def _bounded_cursor(value: object, maximum: int = _CURSOR_MAXIMUM) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) <= 8
        and int(value) <= maximum
    )


def _cursor_json(value: object | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_cursor(value: str | None) -> object | None:
    if value is None:
        return None
    return json.loads(value)


def _coverage_status(data: dict, allowed: set[str], label: str) -> str:
    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        raise MasterdataInventoryError(f"{label} lacks coverage status")
    status = coverage.get("status")
    if not isinstance(status, str) or status not in allowed:
        raise MasterdataInventoryError(f"{label} coverage is unsupported")
    unsupported = coverage.get("unsupported")
    if not isinstance(unsupported, list):
        raise MasterdataInventoryError(f"{label} lacks unsupported status")
    return status


def _validate_row_page(data: dict, *, limit: int, allowed: set[str], label: str) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise MasterdataInventoryError(f"{label} has invalid data")
    if len(data["rows"]) > limit:
        raise MasterdataInventoryError(f"{label} exceeded requested limit")
    if not isinstance(data.get("hasMore"), bool):
        raise MasterdataInventoryError(f"{label} lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not _bounded_cursor(next_cursor):
            raise MasterdataInventoryError(f"{label} lacks bounded next cursor")
    elif next_cursor is not None:
        raise MasterdataInventoryError(f"{label} has unexpected next cursor")
    return _coverage_status(data, allowed, label)


def _validate_history_page(data: dict, *, limit: int) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise MasterdataInventoryError("history page has invalid data")
    if len(data["rows"]) > limit:
        raise MasterdataInventoryError("history page exceeded requested limit")
    if not isinstance(data.get("target"), dict):
        raise MasterdataInventoryError("history page lacks target")
    if not isinstance(data.get("hasMore"), bool):
        raise MasterdataInventoryError("history page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not isinstance(next_cursor, dict) or set(next_cursor) not in (
            {"historyCursor"}, {"ownerCursor", "historyCursor"},
        ):
            raise MasterdataInventoryError("history page lacks bounded next cursor")
        if not _bounded_cursor(next_cursor.get("historyCursor"), _HISTORY_CURSOR_MAXIMUM):
            raise MasterdataInventoryError("history page lacks bounded next cursor")
        if "ownerCursor" in next_cursor and not _bounded_cursor(next_cursor.get("ownerCursor")):
            raise MasterdataInventoryError("history page lacks bounded next cursor")
    elif next_cursor is not None:
        raise MasterdataInventoryError("history page has unexpected next cursor")
    status = _coverage_status(data, _HISTORY_PAGE_COVERAGE, "history page")
    coverage = data["coverage"]
    if not isinstance(coverage.get("metadataLink"), dict):
        raise MasterdataInventoryError("history page lacks metadata link")
    return status


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def inventory_store_path(config: dict) -> Path:
    inventory_ledger = config.get("catalogConstantsInventoryLedger")
    if inventory_ledger is not None:
        if not isinstance(inventory_ledger, dict):
            raise MasterdataInventoryError("catalogConstantsInventoryLedger must be an object")
        raw_path = inventory_ledger.get("path")
    else:
        ledger = config.get("ledger")
        if not isinstance(ledger, dict) or not isinstance(ledger.get("path"), str):
            raise MasterdataInventoryError("ledger path required for catalog/constants inventory storage")
        ledger_path = Path(ledger["path"]).expanduser()
        raw_path = str(ledger_path.with_name(f"{ledger_path.stem}.v77-catalog-constants{ledger_path.suffix}"))
    if not isinstance(raw_path, str) or not raw_path:
        raise MasterdataInventoryError("catalog/constants inventory ledger path required")
    path = Path(raw_path).expanduser().resolve()
    repository = _repository_root()
    if path == repository or repository in path.parents:
        raise MasterdataInventoryError("catalog/constants inventory ledger must be outside the Git checkout")
    return path


def _metadata_counts(data: dict) -> dict:
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise MasterdataInventoryError("metadata inventory lacks counts")
    return {key: value for key, value in sorted(counts.items()) if type(value) is int}


def _validated_catalog_name_selection(catalogs: list, filled_catalog_names: list[str] | None) -> set[str] | None:
    if filled_catalog_names is None:
        return None
    if not isinstance(filled_catalog_names, list):
        raise MasterdataInventoryError("filled_catalog_names must be a list")
    names: list[str] = []
    seen: set[str] = set()
    for value in filled_catalog_names:
        if not isinstance(value, str) or not value:
            raise MasterdataInventoryError("filled_catalog_names must contain nonempty strings")
        if value in seen:
            raise MasterdataInventoryError("filled_catalog_names must not contain duplicates")
        seen.add(value)
        names.append(value)
    metadata_names = {
        str(catalog.get("name", ""))
        for catalog in catalogs
        if isinstance(catalog, dict)
    }
    unknown = sorted(set(names) - metadata_names)
    if unknown:
        raise MasterdataInventoryError("filled_catalog_names contains unknown catalog")
    return set(names)


def _validated_selection_source(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _FILLED_CATALOG_SELECTION_SOURCES:
        raise MasterdataInventoryError("filled_catalog_selection_source is unsupported")
    return value


def _validated_receipt_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MasterdataInventoryError("filled_catalog_selection_receipt_hash must be a SHA-256 hex digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise MasterdataInventoryError("filled_catalog_selection_receipt_hash must be a SHA-256 hex digest")
    return value


def _plan_from_inventory(inventory: dict, *, include_history: bool = False,
                         filled_catalog_names: list[str] | None = None,
                         filled_catalog_selection_source: str | None = None,
                         filled_catalog_selection_receipt_hash: str | None = None) -> tuple[list[dict], dict, str]:
    data = inventory.get("data")
    if not isinstance(data, dict) or data.get("status") != "metadata_catalog":
        raise MasterdataInventoryError("metadata inventory is not a 7.7 catalog")
    counts = _metadata_counts(data)
    catalogs = data.get("catalogs", [])
    constants = data.get("constants", [])
    if not isinstance(catalogs, list) or not isinstance(constants, list):
        raise MasterdataInventoryError("metadata inventory lacks catalog or constant lists")
    selected_catalog_names = _validated_catalog_name_selection(catalogs, filled_catalog_names)
    selection_source = _validated_selection_source(filled_catalog_selection_source)
    selection_receipt_hash = _validated_receipt_hash(filled_catalog_selection_receipt_hash)
    if selected_catalog_names is None:
        if selection_source is not None or selection_receipt_hash is not None:
            raise MasterdataInventoryError(
                "filled catalog selection provenance requires filled_catalog_names")
    else:
        selection_source = selection_source or "caller-provided-filled-catalog-list"
    tasks: list[dict] = []
    selected_catalog_count = 0
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            raise MasterdataInventoryError("invalid catalog metadata")
        name = str(catalog.get("name", ""))
        object_id = str(catalog.get("id", ""))
        attributes = catalog.get("attributes", [])
        if not isinstance(attributes, list):
            raise MasterdataInventoryError("invalid catalog attribute metadata")
        catalog_selected = selected_catalog_names is None or name in selected_catalog_names
        if catalog_selected:
            selected_catalog_count += 1
        tasks.append({
            "taskKey": f"catalog:{object_id}:{name}",
            "taskKind": "catalog",
            "catalogName": name,
            "attributeName": None,
            "constantName": None,
            "status": "pending" if catalog_selected else "skipped",
            "reason": "" if catalog_selected else "catalog omitted by filled-catalog selection",
            "metadataHash": _hash({
                "kind": "catalog",
                "id": object_id,
                "name": name,
                "attributeCount": len(attributes),
            }),
        })
        if include_history and catalog_selected:
            for attribute in attributes:
                if not isinstance(attribute, dict):
                    raise MasterdataInventoryError("invalid catalog attribute metadata")
                attr_name = str(attribute.get("name", ""))
                attr_id = str(attribute.get("id", ""))
                tasks.append({
                    "taskKey": f"catalog_attribute_history:{object_id}:{name}:{attr_id}:{attr_name}",
                    "taskKind": "catalog_attribute_history",
                    "catalogName": name,
                    "attributeName": attr_name,
                    "constantName": None,
                    "status": "waiting_catalog",
                    "metadataHash": _hash({
                        "kind": "catalog_attribute_history",
                        "catalogId": object_id,
                        "catalogName": name,
                        "attributeId": attr_id,
                        "attributeName": attr_name,
                        "typeCode": attribute.get("typeCode"),
                        "refTypeId": attribute.get("refTypeId"),
                    }),
                })
    if constants:
        tasks.append({
            "taskKey": "constants",
            "taskKind": "constants",
            "catalogName": None,
            "attributeName": None,
            "constantName": None,
            "status": "pending",
            "metadataHash": _hash({"kind": "constants", "count": len(constants)}),
        })
    if include_history:
        for constant in constants:
            if not isinstance(constant, dict):
                raise MasterdataInventoryError("invalid constant metadata")
            name = str(constant.get("name", ""))
            object_id = str(constant.get("id", ""))
            tasks.append({
                "taskKey": f"constant_history:{object_id}:{name}",
                "taskKind": "constant_history",
                "catalogName": None,
                "attributeName": None,
                "constantName": name,
                "status": "pending",
                "metadataHash": _hash({
                    "kind": "constant_history",
                    "id": object_id,
                    "name": name,
                    "typeCode": constant.get("typeCode"),
                    "refTypeId": constant.get("refTypeId"),
                }),
            })
    metadata_receipt = {
        "counts": counts,
        "catalogCount": len(catalogs),
        "constantCount": len(constants),
        "catalogAttributeHistoryTargets": sum(
            1 for task in tasks if task["taskKind"] == "catalog_attribute_history"
        ),
        "constantHistoryTargets": len(constants) if include_history else 0,
        "includeHistory": include_history,
        "catalogSelection": {
            "source": (
                "all-metadata-catalogs"
                if selected_catalog_names is None
                else selection_source
            ),
            "receiptHash": selection_receipt_hash,
            "metadataCatalogCount": len(catalogs),
            "selectedCatalogCount": selected_catalog_count,
            "skippedCatalogCount": len(catalogs) - selected_catalog_count,
            "skippedCompletenessClaim": "not-claimed-by-metadata",
            "selectionCompletenessClaim": (
                None if selected_catalog_names is None
                else "caller-evidence-only; metadata validation only proves names exist"
            ),
            "selectedCatalogNamesHash": _hash(sorted(selected_catalog_names))
            if selected_catalog_names is not None else None,
        },
    }
    return tasks, metadata_receipt, _hash(metadata_receipt)


class InventoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS catalog_constants_inventory (
              inventory_id TEXT PRIMARY KEY,
              source_alias TEXT NOT NULL,
              period_from TEXT NOT NULL,
              period_to_exclusive TEXT NOT NULL,
              status TEXT NOT NULL,
              reason TEXT NOT NULL,
              metadata_hash TEXT NOT NULL,
              metadata_counts_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS catalog_constants_task (
              inventory_id TEXT NOT NULL,
              task_key TEXT NOT NULL,
              task_kind TEXT NOT NULL,
              catalog_name TEXT,
              attribute_name TEXT,
              constant_name TEXT,
              status TEXT NOT NULL,
              cursor_json TEXT,
              row_count INTEGER NOT NULL,
              page_count INTEGER NOT NULL,
              data_hash TEXT NOT NULL,
              coverage_status TEXT,
              metadata_hash TEXT NOT NULL,
              reason TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(inventory_id, task_key),
              FOREIGN KEY(inventory_id) REFERENCES catalog_constants_inventory(inventory_id) ON DELETE CASCADE);
        """)

    def close(self) -> None:
        self.db.close()

    def create_inventory(self, *, source_alias: str, period_from: str,
                         period_to_exclusive: str, metadata_hash: str,
                         metadata_counts: dict, tasks: list[dict]) -> str:
        inventory_id = str(uuid.uuid4())
        now = _utc()
        status = "running" if tasks else "partial"
        reason = "inventory initialized" if tasks else "no catalog or constant inventory tasks were scheduled"
        with self.db:
            self.db.execute(
                "INSERT INTO catalog_constants_inventory VALUES (?,?,?,?,?,?,?,?,?,?)",
                (inventory_id, source_alias, period_from, period_to_exclusive,
                 status, reason, metadata_hash,
                 json.dumps(metadata_counts, sort_keys=True), now, now))
            for task in tasks:
                self.db.execute(
                    "INSERT INTO catalog_constants_task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (inventory_id, task["taskKey"], task["taskKind"],
                     task["catalogName"], task["attributeName"], task["constantName"],
                     task["status"], None, 0, 0, _HASH_SEED, None,
                     task["metadataHash"], task.get("reason", ""), now))
        self.refresh_inventory_status(inventory_id)
        return inventory_id

    def inventory_row(self, inventory_id: str) -> sqlite3.Row:
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            "SELECT * FROM catalog_constants_inventory WHERE inventory_id=?",
            (inventory_id,)).fetchone()
        if not row:
            raise MasterdataInventoryError("unknown catalog/constants inventory id")
        return row

    def task_rows(self, inventory_id: str) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        self.inventory_row(inventory_id)
        return list(self.db.execute(
            "SELECT * FROM catalog_constants_task WHERE inventory_id=? "
            f"ORDER BY {_TASK_ORDER_SQL}, catalog_name, constant_name, attribute_name, task_key",
            (inventory_id,)))

    def next_task(self, inventory_id: str) -> sqlite3.Row | None:
        self.db.row_factory = sqlite3.Row
        return self.db.execute(
            "SELECT * FROM catalog_constants_task WHERE inventory_id=? "
            "AND status IN ('pending','running') "
            f"ORDER BY {_TASK_ORDER_SQL}, catalog_name, constant_name, attribute_name, task_key LIMIT 1",
            (inventory_id,)).fetchone()

    def mark_task_blocked(self, inventory_id: str, task_key: str, reason: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE catalog_constants_task SET status='blocked', reason=?, updated_at=? "
                "WHERE inventory_id=? AND task_key=?",
                (reason, _utc(), inventory_id, task_key))
        self.refresh_inventory_status(inventory_id)

    def _activate_catalog_history_if_ready(self, inventory_id: str, catalog_name: str) -> None:
        row = self.db.execute(
            "SELECT row_count FROM catalog_constants_task WHERE inventory_id=? "
            "AND task_kind='catalog' AND catalog_name=? AND status='verified'",
            (inventory_id, catalog_name)).fetchone()
        if row is None:
            return
        now = _utc()
        if int(row[0]) > 0:
            self.db.execute(
                "UPDATE catalog_constants_task SET status='pending', reason='', updated_at=? "
                "WHERE inventory_id=? AND task_kind='catalog_attribute_history' "
                "AND catalog_name=? AND status='waiting_catalog'",
                (now, inventory_id, catalog_name))
        else:
            self.db.execute(
                "UPDATE catalog_constants_task SET status='skipped', "
                "reason='catalog has no rows; history scan limited to filled catalogs', updated_at=? "
                "WHERE inventory_id=? AND task_kind='catalog_attribute_history' "
                "AND catalog_name=? AND status='waiting_catalog'",
                (now, inventory_id, catalog_name))

    def record_page(self, inventory_id: str, task_key: str, *, rows: list[object],
                    has_more: bool, next_cursor: object | None,
                    coverage_status: str, final_status: str | None = None) -> None:
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            "SELECT * FROM catalog_constants_task WHERE inventory_id=? AND task_key=?",
            (inventory_id, task_key)).fetchone()
        if not row:
            raise MasterdataInventoryError("unknown catalog/constants inventory task")
        if row["status"] not in _ACTIVE_TASK_STATUSES:
            raise MasterdataInventoryError("catalog/constants inventory task is not resumable")
        cursor_json = _cursor_json(next_cursor)
        if has_more and cursor_json is None:
            raise MasterdataInventoryError("source page lacks bounded next cursor")
        if has_more and cursor_json == row["cursor_json"]:
            raise MasterdataInventoryError("source page cursor did not advance")
        if not has_more and cursor_json is not None:
            raise MasterdataInventoryError("source page has unexpected next cursor")
        row_count = row["row_count"] + len(rows)
        if has_more:
            status = "running"
            reason = "cursor saved"
        else:
            status = final_status or "verified"
            reason = coverage_status if status == "unsupported" else "page stream exhausted"
        if status not in _TASK_STATUSES:
            raise AssertionError(status)
        with self.db:
            self.db.execute(
                "UPDATE catalog_constants_task SET status=?, cursor_json=?, row_count=?, "
                "page_count=?, data_hash=?, coverage_status=?, reason=?, updated_at=? "
                "WHERE inventory_id=? AND task_key=?",
                (status, cursor_json if has_more else None, row_count,
                 row["page_count"] + 1, _extend_hash(row["data_hash"], rows),
                 coverage_status, reason, _utc(), inventory_id, task_key))
            if row["task_kind"] == "catalog" and not has_more:
                self._activate_catalog_history_if_ready(inventory_id, row["catalog_name"])
        self.refresh_inventory_status(inventory_id)

    def refresh_inventory_status(self, inventory_id: str) -> None:
        row = self.inventory_row(inventory_id)
        tasks = self.task_rows(inventory_id)
        if any(task["status"] == "blocked" for task in tasks):
            status = "blocked"
            reason = "one or more catalog/constants inventory pages failed closed"
        elif any(task["status"] in ("pending", "running", "waiting_catalog") for task in tasks):
            status = "running"
            reason = "catalog or constants inventory pages remain"
        else:
            status = "partial"
            reason = "scheduled streams ended; source snapshot remains unverified"
        if status not in _INVENTORY_STATUSES:
            raise AssertionError(status)
        with self.db:
            self.db.execute(
                "UPDATE catalog_constants_inventory SET status=?, reason=?, updated_at=? "
                "WHERE inventory_id=?",
                (status, reason, _utc(), row["inventory_id"]))

    def status(self, inventory_id: str) -> dict:
        self.refresh_inventory_status(inventory_id)
        row = self.inventory_row(inventory_id)
        tasks = self.task_rows(inventory_id)
        counts = json.loads(row["metadata_counts_json"])
        task_statuses: dict[str, int] = {}
        task_kinds: dict[str, int] = {}
        non_empty_catalog_count = 0
        selected_catalog_count = 0
        skipped_catalog_count = 0
        history_row_count = 0
        for task in tasks:
            task_statuses[task["status"]] = task_statuses.get(task["status"], 0) + 1
            task_kinds[task["task_kind"]] = task_kinds.get(task["task_kind"], 0) + 1
            if task["task_kind"] == "catalog":
                if task["status"] == "skipped":
                    skipped_catalog_count += 1
                else:
                    selected_catalog_count += 1
                if task["row_count"] > 0:
                    non_empty_catalog_count += 1
            if task["task_kind"] in ("constant_history", "catalog_attribute_history"):
                history_row_count += task["row_count"]
        return {
            "inventoryId": row["inventory_id"],
            "sourceAlias": row["source_alias"],
            "status": row["status"],
            "complete": False,
            "reason": row["reason"],
            "period": {
                "from": row["period_from"],
                "toExclusive": row["period_to_exclusive"],
            },
            "metadataHash": row["metadata_hash"],
            "metadataCounts": counts,
            "taskCount": len(tasks),
            "taskKinds": task_kinds,
            "taskStatuses": task_statuses,
            "selectedCatalogCount": selected_catalog_count,
            "skippedCatalogCount": skipped_catalog_count,
            "nonEmptyCatalogCount": non_empty_catalog_count,
            "historyRowCount": history_row_count,
            "updatedAt": row["updated_at"],
        }

    def receipt(self, inventory_id: str) -> dict:
        summary = self.status(inventory_id)
        tasks = self.task_rows(inventory_id)
        summary["nonEmptyCatalogs"] = [
            _task_receipt(task) for task in tasks
            if task["task_kind"] == "catalog" and task["row_count"] > 0
        ]
        summary["emptyCatalogCount"] = sum(
            1 for task in tasks if task["task_kind"] == "catalog" and task["status"] == "verified" and task["row_count"] == 0
        )
        summary["catalogsSkippedBySelectionCount"] = summary["skippedCatalogCount"]
        summary["constants"] = next(
            (_task_receipt(task) for task in tasks if task["task_kind"] == "constants"),
            None,
        )
        summary["history"] = {
            "constantTargets": [
                _task_receipt(task) for task in tasks if task["task_kind"] == "constant_history"
            ],
            "catalogAttributeTargets": [
                _task_receipt(task) for task in tasks if task["task_kind"] == "catalog_attribute_history"
            ],
        }
        summary["assertion"] = "partial_or_unverified_catalog_constants_inventory"
        summary["privacy"] = "catalog rows, constant values and optional history values are hashed in memory and are not stored in inventory receipts"
        summary["limitations"] = [
            "live-read pages may observe a changing source without a stable snapshot",
        ]
        if any(task["task_kind"] in ("constant_history", "catalog_attribute_history")
               for task in tasks):
            summary["limitations"].extend([
                "metadata proves object and attribute names, not that an attribute is periodic",
                "catalog history targets are attempted only after the catalog is observed nonempty",
            ])
        if summary["skippedCatalogCount"]:
            summary["limitations"].append(
                "skipped catalogs come from the validated filled-catalog selection and are not claimed complete by metadata alone"
            )
        return summary


def _task_receipt(task: sqlite3.Row) -> dict:
    return {
        "kind": task["task_kind"],
        "catalogName": task["catalog_name"],
        "attributeName": task["attribute_name"],
        "constantName": task["constant_name"],
        "status": task["status"],
        "rowCount": task["row_count"],
        "pageCount": task["page_count"],
        "dataHash": task["data_hash"],
        "metadataHash": task["metadata_hash"],
        "coverageStatus": task["coverage_status"],
        "cursor": _load_cursor(task["cursor_json"]),
        "reason": task["reason"],
    }


def source_catalog_constants_inventory_start(config: dict, bridge: MasterdataBridge,
                                             source_alias: str, from_date: str,
                                             to_exclusive: str,
                                             include_history: bool = False,
                                             filled_catalog_names: list[str] | None = None,
                                             filled_catalog_selection_source: str | None = None,
                                             filled_catalog_selection_receipt_hash: str | None = None) -> dict:
    if type(include_history) is not bool:
        raise MasterdataInventoryError("include_history must be a boolean")
    start, end = _period(from_date, to_exclusive)
    inventory = bridge.source_metadata_inventory(source_alias)
    tasks, metadata_counts, metadata_hash = _plan_from_inventory(
        inventory, include_history=include_history,
        filled_catalog_names=filled_catalog_names,
        filled_catalog_selection_source=filled_catalog_selection_source,
        filled_catalog_selection_receipt_hash=filled_catalog_selection_receipt_hash)
    store = InventoryStore(inventory_store_path(config))
    try:
        inventory_id = store.create_inventory(
            source_alias=source_alias, period_from=start, period_to_exclusive=end,
            metadata_hash=metadata_hash, metadata_counts=metadata_counts, tasks=tasks)
        return store.status(inventory_id)
    finally:
        store.close()


def source_catalog_constants_inventory_page(config: dict, bridge: MasterdataBridge,
                                            inventory_id: str, page_budget: int = 1,
                                            limit: int = 100, max_seconds: int = 30) -> dict:
    page_budget = _positive_int(page_budget, "page_budget", _MAX_PAGE_BUDGET)
    limit = _positive_int(limit, "limit", _MAX_LIMIT)
    max_seconds = _positive_int(max_seconds, "max_seconds", _MAX_SECONDS)
    store = InventoryStore(inventory_store_path(config))
    pages_read = 0
    deadline = time.monotonic() + max_seconds
    try:
        while pages_read < page_budget and time.monotonic() < deadline:
            status = store.status(inventory_id)
            if status["status"] in ("partial", "blocked"):
                break
            task = store.next_task(inventory_id)
            if task is None:
                break
            try:
                rows, has_more, next_cursor, coverage_status, final_status = _read_task_page(
                    bridge, status, task, limit)
                store.record_page(
                    inventory_id, task["task_key"], rows=rows, has_more=has_more,
                    next_cursor=next_cursor, coverage_status=coverage_status,
                    final_status=final_status)
                pages_read += 1
            except Exception as exc:
                store.mark_task_blocked(inventory_id, task["task_key"], _sanitized_error(exc))
                break
        result = store.status(inventory_id)
        result["pagesRead"] = pages_read
        result["pageBudget"] = page_budget
        return result
    finally:
        store.close()


def _read_task_page(bridge: MasterdataBridge, status: dict, task: sqlite3.Row,
                    limit: int) -> tuple[list[object], bool, object | None, str, str | None]:
    source_alias = status["sourceAlias"]
    kind = task["task_kind"]
    cursor = _load_cursor(task["cursor_json"])
    if kind == "catalog":
        page = bridge.source_read_page(
            source_alias, "reference", task["catalog_name"],
            cursor=cursor if isinstance(cursor, str) else None, limit=limit)
        data = page.get("data")
        coverage_status = _validate_row_page(
            data, limit=limit, allowed=_CATALOG_PAGE_COVERAGE, label="catalog page")
        return data["rows"], data["hasMore"], data.get("nextCursor"), coverage_status, None
    if kind == "constants":
        page = bridge.source_read_constants_page(
            source_alias, cursor=cursor if isinstance(cursor, str) else None, limit=limit)
        data = page.get("data")
        coverage_status = _validate_row_page(
            data, limit=limit, allowed=_CONSTANT_PAGE_COVERAGE, label="constants page")
        return data["rows"], data["hasMore"], data.get("nextCursor"), coverage_status, None
    if kind == "constant_history":
        history_cursor = cursor.get("historyCursor") if isinstance(cursor, dict) else None
        page = bridge.source_read_periodic_history_page(
            source_alias, "constant", task["constant_name"],
            status["period"]["from"], status["period"]["toExclusive"],
            history_cursor=history_cursor, limit=limit)
        data = page.get("data")
        coverage_status = _validate_history_page(data, limit=limit)
        final_status = "unsupported" if coverage_status == "unsupported-periodic-context" and not data["hasMore"] else None
        return data["rows"], data["hasMore"], data.get("nextCursor"), coverage_status, final_status
    if kind == "catalog_attribute_history":
        if task["status"] == "waiting_catalog":
            raise MasterdataInventoryError("catalog attribute history is waiting for catalog count")
        owner_cursor = cursor.get("ownerCursor") if isinstance(cursor, dict) else None
        history_cursor = cursor.get("historyCursor") if isinstance(cursor, dict) else None
        page = bridge.source_read_periodic_history_page(
            source_alias, "catalog_attribute", task["catalog_name"],
            status["period"]["from"], status["period"]["toExclusive"],
            attribute_name=task["attribute_name"], owner_cursor=owner_cursor,
            history_cursor=history_cursor, limit=limit)
        data = page.get("data")
        coverage_status = _validate_history_page(data, limit=limit)
        final_status = "unsupported" if coverage_status == "unsupported-periodic-context" and not data["hasMore"] else None
        return data["rows"], data["hasMore"], data.get("nextCursor"), coverage_status, final_status
    raise MasterdataInventoryError("unsupported catalog/constants inventory task")


def _sanitized_error(exc: Exception) -> str:
    if isinstance(exc, MasterdataInventoryError):
        return str(exc)
    return exc.__class__.__name__


def source_catalog_constants_inventory_status(config: dict, inventory_id: str) -> dict:
    store = InventoryStore(inventory_store_path(config))
    try:
        return store.status(inventory_id)
    finally:
        store.close()


def source_catalog_constants_inventory_receipt(config: dict, inventory_id: str) -> dict:
    store = InventoryStore(inventory_store_path(config))
    try:
        return store.receipt(inventory_id)
    finally:
        store.close()
