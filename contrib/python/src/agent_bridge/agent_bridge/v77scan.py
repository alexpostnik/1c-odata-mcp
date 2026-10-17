"""Private resumable source coverage scans for 1C 7.7 metadata objects.

The scanner stores only metadata counts, cursors, row counts and hashes in a
local SQLite file outside Git. Source rows are hashed in memory and discarded.
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


class ScanError(ValueError):
    pass


class ScanBridge(Protocol):
    def source_metadata_inventory(self, source_alias: str) -> dict: ...
    def source_document_presence_page(self, source_alias: str,
                                      from_date: str, to_exclusive: str,
                                      cursor: str | None = None,
                                      limit: int = 1000) -> dict: ...
    def source_read_page(self, source_alias: str, kind: str, type_name: str,
                         from_date: str | None = None,
                         to_exclusive: str | None = None,
                         cursor: str | None = None,
                         limit: int = 100) -> dict: ...


_HASH_SEED = hashlib.sha256(b"agent_bridge.v77scan.dataset.v1").hexdigest()
_SCAN_STATUSES = {"running", "partial", "blocked"}
_DATASET_STATUSES = {"pending", "running", "verified", "blocked"}
_MAX_PAGE_BUDGET = 25
_MAX_LIMIT = 100
_MAX_SECONDS = 240
_DOCUMENT_PRESENCE_LIMIT = 5000
_MAX_DOCUMENT_PRESENCE_PAGES = 1000
_FUTURE_CONSTANT_TOOL = "source_read_constants_page"
_FUTURE_LINE_TOOL = "source_read_document_lines_page"
_OTHER_SECTION_TOOL_PREFIX = "source_read_"
_CURSOR_MAXIMUM = 10_000_000
_ACCEPTED_PAGE_COVERAGE = {
    "configured-fields-unverified",
    "metadata-driven-unverified",
}


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
        raise ScanError(f"{name} must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ScanError(f"{name} must be ISO YYYY-MM-DD")
    return value


def _period(from_date: str, to_exclusive: str) -> tuple[str, str]:
    start = _date(from_date, "from_date")
    end = _date(to_exclusive, "to_exclusive")
    if start >= end:
        raise ScanError("period must be nonempty")
    return start, end


def _positive_int(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ScanError(f"{name} must be 1..{maximum}")
    return value


def _bounded_cursor(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) <= 8
        and int(value) <= _CURSOR_MAXIMUM
    )


def _validate_page_contract(data: dict, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ScanError("source page has invalid data")
    if len(data["rows"]) > limit:
        raise ScanError("source page exceeded requested limit")
    if not isinstance(data.get("hasMore"), bool):
        raise ScanError("source page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not _bounded_cursor(next_cursor):
            raise ScanError("source page lacks bounded next cursor")
    elif next_cursor is not None:
        raise ScanError("source page has unexpected next cursor")
    coverage = data.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("status") not in _ACCEPTED_PAGE_COVERAGE:
        raise ScanError("source page coverage is partial or unsupported")


def _validate_document_presence_contract(data: object, *, limit: int) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ScanError("document presence page has invalid data")
    scanned = data.get("documentsScanned")
    if type(scanned) is not int or not 0 <= scanned <= limit:
        raise ScanError("document presence page lacks bounded scan count")
    if len(data["groups"]) > scanned:
        raise ScanError("document presence page has too many groups")
    if not isinstance(data.get("hasMore"), bool):
        raise ScanError("document presence page lacks hasMore")
    next_cursor = data.get("nextCursor")
    if data["hasMore"]:
        if not _bounded_cursor(next_cursor):
            raise ScanError("document presence page lacks bounded next cursor")
    elif next_cursor is not None:
        raise ScanError("document presence page has unexpected next cursor")
    coverage = data.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("status") != "presence-page-unverified":
        raise ScanError("document presence page coverage is unsupported")
    for group in data["groups"]:
        if not isinstance(group, dict):
            raise ScanError("invalid document presence group")
        name = group.get("documentType")
        if not isinstance(name, str) or not name:
            raise ScanError("invalid document presence group")
        count = group.get("count")
        if type(count) is not int or count < 1:
            raise ScanError("invalid document presence group")
        for key in ("postedCount", "unpostedCount", "unknownPostedCount"):
            if type(group.get(key)) is not int or not 0 <= group[key] <= count:
                raise ScanError("invalid document presence group")
        if group["postedCount"] + group["unpostedCount"] + group["unknownPostedCount"] != count:
            raise ScanError("invalid document presence group")
        first_date = _date(group.get("firstDate"), "firstDate")
        last_date = _date(group.get("lastDate"), "lastDate")
        if first_date > last_date:
            raise ScanError("invalid document presence group")
        metadata = group.get("metadata")
        if not isinstance(metadata, dict):
            raise ScanError("invalid document presence metadata")
        if type(metadata.get("known")) is not bool:
            raise ScanError("invalid document presence metadata")
        if type(metadata.get("headerAttributeCount")) is not int or metadata["headerAttributeCount"] < 0:
            raise ScanError("invalid document presence metadata")
        if type(metadata.get("tableAttributeCount")) is not int or metadata["tableAttributeCount"] < 0:
            raise ScanError("invalid document presence metadata")


def _merge_document_presence_group(existing: dict | None, group: dict) -> dict:
    if existing is None:
        return json.loads(json.dumps(group, ensure_ascii=False))
    metadata = existing["metadata"]
    incoming_metadata = group["metadata"]
    if metadata != incoming_metadata:
        raise ScanError("document presence metadata changed between pages")
    existing["count"] += group["count"]
    existing["postedCount"] += group["postedCount"]
    existing["unpostedCount"] += group["unpostedCount"]
    existing["unknownPostedCount"] += group["unknownPostedCount"]
    existing["firstDate"] = min(existing["firstDate"], group["firstDate"])
    existing["lastDate"] = max(existing["lastDate"], group["lastDate"])
    return existing


def _collect_document_presence(bridge: ScanBridge, source_alias: str,
                               from_date: str, to_exclusive: str) -> tuple[dict[str, dict], dict, str]:
    cursor: str | None = None
    pages = 0
    documents_scanned = 0
    groups_by_type: dict[str, dict] = {}
    while True:
        if pages >= _MAX_DOCUMENT_PRESENCE_PAGES:
            raise ScanError("document presence did not finish within page budget")
        page = bridge.source_document_presence_page(
            source_alias, from_date, to_exclusive, cursor=cursor,
            limit=_DOCUMENT_PRESENCE_LIMIT)
        data = page.get("data")
        _validate_document_presence_contract(data, limit=_DOCUMENT_PRESENCE_LIMIT)
        pages += 1
        documents_scanned += data["documentsScanned"]
        for group in data["groups"]:
            name = group["documentType"]
            groups_by_type[name] = _merge_document_presence_group(groups_by_type.get(name), group)
        if data["hasMore"] is not True:
            break
        next_cursor = data.get("nextCursor")
        if next_cursor == cursor:
            raise ScanError("document presence cursor did not advance")
        cursor = next_cursor
    receipt = {
        "actualDocumentTypeCount": len(groups_by_type),
        "documentsScanned": documents_scanned,
        "pageCount": pages,
        "periodFrom": from_date,
        "periodToExclusive": to_exclusive,
        "groups": [groups_by_type[name] for name in sorted(groups_by_type)],
    }
    return groups_by_type, receipt, _hash(receipt)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def scan_store_path(config: dict) -> Path:
    scan_ledger = config.get("scanLedger")
    if scan_ledger is not None:
        if not isinstance(scan_ledger, dict):
            raise ScanError("scanLedger must be an object")
        raw_path = scan_ledger.get("path")
    else:
        ledger = config.get("ledger")
        if not isinstance(ledger, dict) or not isinstance(ledger.get("path"), str):
            raise ScanError("ledger path required for source scan storage")
        ledger_path = Path(ledger["path"]).expanduser()
        raw_path = str(ledger_path.with_name(f"{ledger_path.stem}.v77-scan{ledger_path.suffix}"))
    if not isinstance(raw_path, str) or not raw_path:
        raise ScanError("scan ledger path required")
    path = Path(raw_path).expanduser().resolve()
    repository = _repository_root()
    if path == repository or repository in path.parents:
        raise ScanError("scan ledger must be outside the Git checkout")
    return path


def _metadata_counts(data: dict) -> dict:
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise ScanError("metadata inventory lacks counts")
    return {key: value for key, value in sorted(counts.items()) if type(value) is int}


def _other_section_unsupported(data: dict) -> list[dict]:
    unsupported = []
    other_sections = data.get("otherSections", {})
    if isinstance(other_sections, dict):
        for name, indicator in sorted(other_sections.items()):
            if isinstance(indicator, dict) and indicator.get("present") is True:
                unsupported.append({
                    "kind": name,
                    "status": "unsupported",
                    "count": indicator.get("boundedObjectCount"),
                    "reason": "metadata section has no Mac scan reader",
                    "requiredTool": f"{_OTHER_SECTION_TOOL_PREFIX}{name}_page",
                })
    coverage = data.get("sectionCoverage")
    if not isinstance(coverage, dict):
        unsupported.append({
            "kind": "metadata_section_coverage",
            "status": "unsupported",
            "reason": "metadata inventory did not prove all top-level sections are mapped",
            "requiredTool": "source_metadata_inventory",
        })
    elif isinstance(coverage.get("unmapped"), dict) and coverage["unmapped"].get("present") is True:
        unsupported.append({
            "kind": "metadata_unmapped_sections",
            "status": "unsupported",
            "count": coverage["unmapped"].get("boundedObjectCount"),
            "reason": "metadata inventory reported unmapped top-level sections",
            "requiredTool": "source_metadata_inventory",
        })
    return unsupported


def _plan_from_inventory(inventory: dict, document_presence: dict[str, dict],
                         document_presence_hash: str) -> tuple[list[dict], list[dict], dict, str]:
    data = inventory.get("data")
    if not isinstance(data, dict) or data.get("status") != "metadata_catalog":
        raise ScanError("metadata inventory is not a 7.7 catalog")
    counts = _metadata_counts(data)
    datasets: list[dict] = []
    unsupported: list[dict] = [{
        "kind": "source_snapshot",
        "status": "unverified",
        "reason": "page exhaustion uses provisional offsets and does not prove a stable source snapshot",
        "requiredTool": "stable source snapshot receipt",
    }]
    if "constants" not in counts:
        unsupported.append({
            "kind": "constants",
            "status": "unsupported",
            "reason": "metadata inventory did not include bounded constants",
            "requiredTool": _FUTURE_CONSTANT_TOOL,
        })
    elif counts["constants"] > 0:
        unsupported.append({
            "kind": "constants",
            "status": "not_scanned",
            "count": counts["constants"],
            "reason": "constant value pages are available separately and are not included in this scan pass",
            "requiredTool": _FUTURE_CONSTANT_TOOL,
        })
    catalogs = data.get("catalogs", [])
    documents = data.get("documents", [])
    if not isinstance(catalogs, list) or not isinstance(documents, list):
        raise ScanError("metadata inventory lacks catalog or document lists")
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            raise ScanError("invalid catalog metadata")
        name = str(catalog.get("name", ""))
        object_id = str(catalog.get("id", ""))
        datasets.append({
            "datasetKey": f"reference:{object_id}:{name}",
            "kind": "reference",
            "typeName": name,
            "metadataHash": _hash({
                "kind": "reference",
                "id": object_id,
                "name": name,
                "attributeCount": len(catalog.get("attributes", [])),
            }),
        })

    documents_by_name: dict[str, dict] = {}
    for document in documents:
        if not isinstance(document, dict):
            raise ScanError("invalid document metadata")
        name = str(document.get("name", ""))
        documents_by_name[name] = document

    for name, group in sorted(document_presence.items()):
        document = documents_by_name.get(name)
        presence_metadata = group["metadata"]
        if document is None:
            object_id = str(presence_metadata.get("id") or _hash({"documentType": name})[:16])
            head_attribute_count = presence_metadata["headerAttributeCount"]
            table_attribute_count = presence_metadata["tableAttributeCount"]
            unsupported.append({
                "kind": "document_metadata",
                "typeName": name,
                "status": "unsupported",
                "count": group["count"],
                "reason": "document appears in live presence scan but is absent from parsed metadata inventory",
                "requiredTool": "source_metadata_inventory",
            })
        else:
            object_id = str(document.get("id", presence_metadata.get("id") or ""))
            table_attributes = document.get("tableAttributes", [])
            if not isinstance(table_attributes, list):
                raise ScanError("invalid document table metadata")
            head_attributes = document.get("headAttributes", [])
            if not isinstance(head_attributes, list):
                raise ScanError("invalid document head metadata")
            head_attribute_count = len(head_attributes)
            table_attribute_count = len(table_attributes)

        datasets.append({
            "datasetKey": f"document:{object_id}:{name}",
            "kind": "document",
            "typeName": name,
            "expectedCount": group["count"],
            "metadataHash": _hash({
                "kind": "document",
                "id": object_id,
                "name": name,
                "headAttributeCount": head_attribute_count,
                "tableAttributeCount": table_attribute_count,
                "presenceCount": group["count"],
                "presenceFirstDate": group["firstDate"],
                "presenceLastDate": group["lastDate"],
            }),
        })
        if table_attribute_count:
            unsupported.append({
                "kind": "document_lines",
                "typeName": name,
                "status": "not_scanned",
                "count": table_attribute_count,
                "reason": "document line pages are available separately and are not included in this scan pass",
                "requiredTool": _FUTURE_LINE_TOOL,
            })
    unsupported.extend(_other_section_unsupported(data))
    metadata_receipt = {
        "counts": counts,
        "catalogCount": len(catalogs),
        "configuredDocumentCount": len(documents),
        "actualDocumentTypeCount": len(document_presence),
        "documentsScanned": sum(group["count"] for group in document_presence.values()),
        "documentPresenceHash": document_presence_hash,
    }
    return datasets, unsupported, metadata_receipt, _hash(metadata_receipt)


class ScanStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS source_scan (
              scan_id TEXT PRIMARY KEY,
              source_alias TEXT NOT NULL,
              period_from TEXT NOT NULL,
              period_to_exclusive TEXT NOT NULL,
              status TEXT NOT NULL,
              reason TEXT NOT NULL,
              metadata_hash TEXT NOT NULL,
              metadata_counts_json TEXT NOT NULL,
              unsupported_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_scan_dataset (
              scan_id TEXT NOT NULL,
              dataset_key TEXT NOT NULL,
              kind TEXT NOT NULL,
              type_name TEXT NOT NULL,
              metadata_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              cursor TEXT,
              row_count INTEGER NOT NULL,
              page_count INTEGER NOT NULL,
              data_hash TEXT NOT NULL,
              total_count INTEGER,
              reason TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(scan_id, dataset_key),
              FOREIGN KEY(scan_id) REFERENCES source_scan(scan_id) ON DELETE CASCADE);
        """)

    def close(self) -> None:
        self.db.close()

    def create_scan(self, *, source_alias: str, period_from: str,
                    period_to_exclusive: str, metadata_hash: str,
                    metadata_counts: dict, unsupported: list[dict],
                    datasets: list[dict]) -> str:
        scan_id = str(uuid.uuid4())
        now = _utc()
        status = "partial" if not datasets else "running"
        reason = "no row pages scheduled; stable source snapshot remains unverified" if status == "partial" else "scan initialized"
        with self.db:
            self.db.execute(
                "INSERT INTO source_scan VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scan_id, source_alias, period_from, period_to_exclusive, status,
                 reason, metadata_hash, json.dumps(metadata_counts, sort_keys=True),
                 json.dumps(unsupported, sort_keys=True), now, now))
            for dataset in datasets:
                self.db.execute(
                    "INSERT INTO source_scan_dataset VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scan_id, dataset["datasetKey"], dataset["kind"],
                     dataset["typeName"], dataset["metadataHash"], "pending",
                     None, 0, 0, _HASH_SEED, dataset.get("expectedCount"), "", now))
        self.refresh_scan_status(scan_id)
        return scan_id

    def scan_row(self, scan_id: str) -> sqlite3.Row:
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT * FROM source_scan WHERE scan_id=?", (scan_id,)).fetchone()
        if not row:
            raise ScanError("unknown scan id")
        return row

    def dataset_rows(self, scan_id: str) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        self.scan_row(scan_id)
        return list(self.db.execute(
            "SELECT * FROM source_scan_dataset WHERE scan_id=? ORDER BY kind,type_name,dataset_key",
            (scan_id,)))

    def next_dataset(self, scan_id: str) -> sqlite3.Row | None:
        self.db.row_factory = sqlite3.Row
        return self.db.execute(
            "SELECT * FROM source_scan_dataset WHERE scan_id=? "
            "AND status IN ('pending','running') ORDER BY kind,type_name,dataset_key LIMIT 1",
            (scan_id,)).fetchone()

    def mark_dataset_blocked(self, scan_id: str, dataset_key: str, reason: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE source_scan_dataset SET status='blocked', reason=?, updated_at=? "
                "WHERE scan_id=? AND dataset_key=?",
                (reason, _utc(), scan_id, dataset_key))
        self.refresh_scan_status(scan_id)

    def record_page(self, scan_id: str, dataset_key: str, *, rows: list[object],
                    has_more: bool, next_cursor: str | None,
                    total_count: int | None) -> None:
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            "SELECT * FROM source_scan_dataset WHERE scan_id=? AND dataset_key=?",
            (scan_id, dataset_key)).fetchone()
        if not row:
            raise ScanError("unknown scan dataset")
        if row["status"] not in ("pending", "running"):
            raise ScanError("scan dataset is not resumable")
        if has_more and not _bounded_cursor(next_cursor):
            raise ScanError("source page lacks bounded next cursor")
        if has_more and next_cursor == row["cursor"]:
            raise ScanError("source page cursor did not advance")
        if not has_more and next_cursor is not None:
            raise ScanError("source page has unexpected next cursor")
        current_total = row["total_count"]
        if total_count is not None:
            if type(total_count) is not int or total_count < 0:
                raise ScanError("source page totalCount is invalid")
            if current_total is not None and current_total != total_count:
                raise ScanError("source page totalCount changed")
        else:
            total_count = current_total
        row_count = row["row_count"] + len(rows)
        if total_count is not None and row_count > total_count:
            raise ScanError("source page row count exceeds totalCount")
        status = "running" if has_more else "verified"
        reason = "cursor saved" if has_more else "page stream exhausted"
        if status == "verified" and total_count is not None and row_count != total_count:
            raise ScanError("source page ended before totalCount")
        with self.db:
            self.db.execute(
                "UPDATE source_scan_dataset SET status=?, cursor=?, row_count=?, "
                "page_count=?, data_hash=?, total_count=?, reason=?, updated_at=? "
                "WHERE scan_id=? AND dataset_key=?",
                (status, next_cursor if has_more else None, row_count,
                 row["page_count"] + 1, _extend_hash(row["data_hash"], rows),
                 total_count, reason, _utc(), scan_id, dataset_key))
        self.refresh_scan_status(scan_id)

    def refresh_scan_status(self, scan_id: str) -> None:
        row = self.scan_row(scan_id)
        unsupported = json.loads(row["unsupported_json"])
        datasets = self.dataset_rows(scan_id)
        if any(dataset["status"] == "blocked" for dataset in datasets):
            status = "blocked"
            reason = "one or more source pages failed closed"
        elif any(dataset["status"] != "verified" for dataset in datasets):
            status = "running"
            reason = "metadata-listed source pages remain"
        elif unsupported:
            status = "partial"
            reason = "unsupported metadata-listed surfaces remain"
        else:
            status = "partial"
            reason = "all scheduled page streams exhausted; stable source snapshot remains unverified"
        if status not in _SCAN_STATUSES:
            raise AssertionError(status)
        with self.db:
            self.db.execute(
                "UPDATE source_scan SET status=?, reason=?, updated_at=? WHERE scan_id=?",
                (status, reason, _utc(), scan_id))

    def status(self, scan_id: str) -> dict:
        self.refresh_scan_status(scan_id)
        row = self.scan_row(scan_id)
        datasets = self.dataset_rows(scan_id)
        unsupported = json.loads(row["unsupported_json"])
        counts = json.loads(row["metadata_counts_json"])
        dataset_counts: dict[str, int] = {}
        for dataset in datasets:
            dataset_counts[dataset["status"]] = dataset_counts.get(dataset["status"], 0) + 1
        return {
            "scanId": row["scan_id"],
            "sourceAlias": row["source_alias"],
            "status": row["status"],
            "complete": row["status"] == "complete",
            "reason": row["reason"],
            "period": {
                "from": row["period_from"],
                "toExclusive": row["period_to_exclusive"],
            },
            "metadataHash": row["metadata_hash"],
            "metadataCounts": counts,
            "datasetCount": len(datasets),
            "datasetStatuses": dataset_counts,
            "unsupportedCount": len(unsupported),
            "updatedAt": row["updated_at"],
        }

    def receipt(self, scan_id: str) -> dict:
        summary = self.status(scan_id)
        row = self.scan_row(scan_id)
        unsupported = json.loads(row["unsupported_json"])
        datasets = self.dataset_rows(scan_id)
        summary["unsupported"] = unsupported
        summary["datasets"] = [{
            "kind": dataset["kind"],
            "typeName": dataset["type_name"],
            "status": dataset["status"],
            "rowCount": dataset["row_count"],
            "pageCount": dataset["page_count"],
            "dataHash": dataset["data_hash"],
            "metadataHash": dataset["metadata_hash"],
            "cursor": dataset["cursor"],
            "totalCount": dataset["total_count"],
            "reason": dataset["reason"],
        } for dataset in datasets]
        summary["assertion"] = "partial_or_unverified_source_coverage"
        summary["privacy"] = "row values are hashed in memory and are not stored in scan receipts"
        return summary


def source_scan_start(config: dict, bridge: ScanBridge, source_alias: str,
                      from_date: str, to_exclusive: str) -> dict:
    start, end = _period(from_date, to_exclusive)
    inventory = bridge.source_metadata_inventory(source_alias)
    document_presence, document_presence_receipt, document_presence_hash = _collect_document_presence(
        bridge, source_alias, start, end)
    datasets, unsupported, metadata_counts, metadata_hash = _plan_from_inventory(
        inventory, document_presence, document_presence_hash)
    metadata_counts["documentPresence"] = document_presence_receipt
    store = ScanStore(scan_store_path(config))
    try:
        scan_id = store.create_scan(
            source_alias=source_alias, period_from=start, period_to_exclusive=end,
            metadata_hash=metadata_hash, metadata_counts=metadata_counts,
            unsupported=unsupported, datasets=datasets)
        return store.status(scan_id)
    finally:
        store.close()


def source_scan_page(config: dict, bridge: ScanBridge, scan_id: str,
                     page_budget: int = 1, limit: int = 100,
                     max_seconds: int = 30) -> dict:
    page_budget = _positive_int(page_budget, "page_budget", _MAX_PAGE_BUDGET)
    limit = _positive_int(limit, "limit", _MAX_LIMIT)
    max_seconds = _positive_int(max_seconds, "max_seconds", _MAX_SECONDS)
    store = ScanStore(scan_store_path(config))
    pages_read = 0
    deadline = time.monotonic() + max_seconds
    try:
        while pages_read < page_budget and time.monotonic() < deadline:
            status = store.status(scan_id)
            if status["status"] in ("complete", "partial", "blocked"):
                break
            dataset = store.next_dataset(scan_id)
            if dataset is None:
                break
            try:
                page = bridge.source_read_page(
                    status["sourceAlias"], dataset["kind"], dataset["type_name"],
                    from_date=status["period"]["from"] if dataset["kind"] == "document" else None,
                    to_exclusive=status["period"]["toExclusive"] if dataset["kind"] == "document" else None,
                    cursor=dataset["cursor"], limit=limit)
                data = page.get("data")
                _validate_page_contract(data, limit=limit)
                rows = data["rows"]
                store.record_page(
                    scan_id, dataset["dataset_key"], rows=rows,
                    has_more=data.get("hasMore") is True,
                    next_cursor=data.get("nextCursor"),
                    total_count=data.get("totalCount"))
                pages_read += 1
            except Exception as exc:
                store.mark_dataset_blocked(scan_id, dataset["dataset_key"],
                                           _sanitized_error(exc))
                break
        result = store.status(scan_id)
        result["pagesRead"] = pages_read
        result["pageBudget"] = page_budget
        return result
    finally:
        store.close()


def _sanitized_error(exc: Exception) -> str:
    if isinstance(exc, ScanError):
        return str(exc)
    return exc.__class__.__name__


def source_scan_status(config: dict, scan_id: str) -> dict:
    store = ScanStore(scan_store_path(config))
    try:
        return store.status(scan_id)
    finally:
        store.close()


def source_scan_receipt(config: dict, scan_id: str) -> dict:
    store = ScanStore(scan_store_path(config))
    try:
        return store.receipt(scan_id)
    finally:
        store.close()
