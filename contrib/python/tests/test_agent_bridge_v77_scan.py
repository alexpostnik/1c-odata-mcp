"""Synthetic checks for private 7.7 source coverage scans."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from agent_bridge import source, v77scan

ROOT = Path(__file__).resolve().parents[1]


def _empty_sections() -> dict:
    return {
        name: {"present": False, "boundedObjectCount": 0, "countLimit": 100000}
        for name in ("constants", "enumerations", "registers", "journals",
                     "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors")
    }


def _section_coverage(*, constants: bool = False, unmapped: bool = False) -> dict:
    return {
        "constants": {"present": constants, "rootSectionCount": 1 if constants else 0,
                      "boundedObjectCount": 1 if constants else 0,
                      "countLimit": 100000, "sectionNames": ["Consts", "Const"]},
        "catalogs": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                     "countLimit": 100000, "sectionNames": ["SbCnts"]},
        "documents": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                      "countLimit": 100000, "sectionNames": ["Documents"]},
        "enumerations": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                         "countLimit": 100000, "sectionNames": ["Enums", "Enum"]},
        "registers": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                      "countLimit": 100000, "sectionNames": ["Registers"]},
        "journals": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                     "countLimit": 100000, "sectionNames": ["Journals"]},
        "documentNumerators": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                               "countLimit": 100000, "sectionNames": ["DocNumRef"]},
        "chartsOfAccounts": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                             "countLimit": 100000, "sectionNames": ["Accounts"]},
        "reports": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                    "countLimit": 100000, "sectionNames": ["Reports"]},
        "dataProcessors": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
                           "countLimit": 100000, "sectionNames": ["DataProcessors"]},
        "unmapped": {"present": unmapped, "rootSectionCount": 1 if unmapped else 0,
                     "boundedObjectCount": 1 if unmapped else 0, "countLimit": 100000},
    }




def _presence(groups: list[dict]) -> dict:
    return {"data": {
        "kind": "document_presence",
        "snapshot": {"status": "live-read-unverified", "identity": None},
        "period": {"from": "2012-01-01", "toExclusive": "2013-01-01"},
        "groups": groups,
        "documentsScanned": sum(group["count"] for group in groups),
        "hasMore": False,
        "nextCursor": None,
        "coverage": {"status": "presence-page-unverified", "unsupported": []},
    }}


def _presence_from_inventory(inventory: dict) -> dict:
    groups = []
    for document in inventory["data"].get("documents", []):
        groups.append({
            "documentType": document["name"],
            "presentation": document["name"],
            "count": 1,
            "firstDate": "2012-01-01",
            "lastDate": "2012-01-01",
            "postedCount": 0,
            "unpostedCount": 1,
            "unknownPostedCount": 0,
            "metadata": {
                "known": True,
                "id": document.get("id"),
                "headerAttributeCount": len(document.get("headAttributes", [])),
                "tableAttributeCount": len(document.get("tableAttributes", [])),
            },
        })
    return _presence(groups)

def _inventory(*, catalogs: list[dict] | None = None,
               documents: list[dict] | None = None,
               constants: list[dict] | None = None,
               sections: dict | None = None,
               coverage: dict | None = None) -> dict:
    catalogs = catalogs if catalogs is not None else [{
        "id": "1", "name": "SyntheticCatalog", "attributes": []
    }]
    documents = documents if documents is not None else [{
        "id": "2", "name": "SyntheticDocument",
        "headAttributes": [], "tableAttributes": []
    }]
    constants = constants if constants is not None else []
    counts = {
        "catalogs": len(catalogs),
        "documents": len(documents),
        "constants": len(constants),
        "attributes": sum(len(c.get("attributes", [])) for c in catalogs)
        + sum(len(d.get("headAttributes", [])) + len(d.get("tableAttributes", []))
              for d in documents),
        "totalMetadataAttributes": len(constants),
    }
    return {"data": {
        "status": "metadata_catalog",
        "counts": counts,
        "catalogs": catalogs,
        "documents": documents,
        "constants": constants,
        "otherSections": sections if sections is not None else _empty_sections(),
        "sectionCoverage": coverage if coverage is not None else _section_coverage(),
    }}


class FakeBridge:
    def __init__(self, inventory: dict, pages: list[dict] | None = None,
                 errors: list[Exception] | None = None,
                 presence_pages: list[dict] | None = None):
        self.inventory = inventory
        self.pages = pages or []
        self.errors = errors or []
        self.presence_pages = presence_pages
        self.calls: list[dict] = []

    def source_metadata_inventory(self, source_alias: str) -> dict:
        self.calls.append({"operation": "source_metadata_inventory",
                           "sourceAlias": source_alias})
        return self.inventory

    def source_document_presence_page(self, source_alias: str,
                                      from_date: str, to_exclusive: str,
                                      cursor: str | None = None,
                                      limit: int = 1000) -> dict:
        self.calls.append({"operation": "source_document_presence_page",
                           "sourceAlias": source_alias, "fromDate": from_date,
                           "toExclusive": to_exclusive, "cursor": cursor,
                           "limit": limit})
        if self.presence_pages is None:
            return _presence_from_inventory(self.inventory)
        return self.presence_pages.pop(0)

    def source_read_page(self, source_alias: str, kind: str, type_name: str,
                         from_date: str | None = None,
                         to_exclusive: str | None = None,
                         cursor: str | None = None,
                         limit: int = 100) -> dict:
        self.calls.append({"operation": "source_read_page", "sourceAlias": source_alias,
                           "kind": kind, "typeName": type_name, "fromDate": from_date,
                           "toExclusive": to_exclusive, "cursor": cursor, "limit": limit})
        if self.errors:
            raise self.errors.pop(0)
        return self.pages.pop(0)


class V77ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.scan_path = Path(self.directory.name) / "source-scan.sqlite3"
        self.config = {"ledger": {"path": str(Path(self.directory.name) / "ledger.sqlite3")},
                       "scanLedger": {"path": str(self.scan_path)}}

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_scan_start_uses_actual_document_presence_before_planning_documents(self):
        document_a = {"id": "2", "name": "PresentDocument",
                      "headAttributes": [{"id": "10", "name": "Amount"}],
                      "tableAttributes": []}
        document_b = {"id": "3", "name": "AbsentDocument",
                      "headAttributes": [], "tableAttributes": []}
        inventory = _inventory(catalogs=[], documents=[document_a, document_b])
        bridge = FakeBridge(
            inventory,
            pages=[{"data": {"rows": [], "hasMore": False, "nextCursor": None,
                              "coverage": {"status": "metadata-driven-unverified",
                                           "unsupported": []}}}],
            presence_pages=[_presence([{
                "documentType": "PresentDocument",
                "presentation": "PresentDocument",
                "count": 7,
                "firstDate": "2012-02-01",
                "lastDate": "2012-12-31",
                "postedCount": 0,
                "unpostedCount": 7,
                "unknownPostedCount": 0,
                "metadata": {
                    "known": True,
                    "id": "2",
                    "headerAttributeCount": 1,
                    "tableAttributeCount": 0,
                },
            }])])
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        self.assertEqual(start["datasetCount"], 1)
        self.assertEqual(start["metadataCounts"]["configuredDocumentCount"], 2)
        self.assertEqual(start["metadataCounts"]["actualDocumentTypeCount"], 1)
        self.assertEqual(start["metadataCounts"]["documentsScanned"], 7)
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        self.assertEqual([dataset["typeName"] for dataset in receipt["datasets"]],
                         ["PresentDocument"])
        self.assertEqual(receipt["datasets"][0]["totalCount"], 7)
        self.assertNotIn("AbsentDocument", json.dumps(receipt, ensure_ascii=False))
        advanced = v77scan.source_scan_page(self.config, bridge, start["scanId"],
                                            page_budget=1, limit=10)
        self.assertEqual(advanced["status"], "blocked")
        self.assertEqual(bridge.calls[0]["operation"], "source_metadata_inventory")
        self.assertEqual(bridge.calls[1]["operation"], "source_document_presence_page")
        self.assertEqual(bridge.calls[-1]["typeName"], "PresentDocument")

    def test_scan_fails_closed_when_lines_constants_and_sections_are_unsupported(self):
        document = {"id": "2", "name": "SyntheticDocument", "headAttributes": [],
                    "tableAttributes": [{"id": "3", "name": "LineTotal",
                                         "typeCode": "N", "length": 10,
                                         "precision": 2, "refTypeId": ""}]}
        constants = [{"id": "4", "name": "DefaultCurrency", "typeCode": "B",
                      "length": 0, "precision": 0, "refTypeId": "1"}]
        sections = _empty_sections()
        sections["registers"] = {"present": True, "boundedObjectCount": 1,
                                 "countLimit": 100000}
        bridge = FakeBridge(_inventory(documents=[document], constants=constants,
                                       sections=sections,
                                       coverage=_section_coverage(constants=True, unmapped=True)),
                            pages=[
                                {"data": {"rows": [{"Ref": "synthetic-document"}],
                                          "hasMore": False, "nextCursor": None,
                                          "coverage": {"status": "configured-fields-unverified",
                                                       "unsupported": []}}},
                                {"data": {"rows": [], "hasMore": False, "nextCursor": None,
                                          "coverage": {"status": "configured-fields-unverified",
                                                       "unsupported": []}}},
                            ])
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        self.assertEqual(start["status"], "running")
        advanced = v77scan.source_scan_page(self.config, bridge, start["scanId"], page_budget=2, limit=10)
        self.assertEqual(advanced["status"], "partial")
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        self.assertFalse(receipt["complete"])
        unsupported = {(item["kind"], item.get("requiredTool")) for item in receipt["unsupported"]}
        self.assertIn(("constants", "source_read_constants_page"), unsupported)
        self.assertIn(("document_lines", "source_read_document_lines_page"), unsupported)
        self.assertTrue(any(item["kind"] == "registers" for item in receipt["unsupported"]))
        self.assertTrue(any(item["kind"] == "metadata_unmapped_sections"
                            for item in receipt["unsupported"]))

    def test_scan_hashes_rows_without_persisting_raw_values(self):
        raw_value = "SECRET-ACCOUNTING-VALUE-DO-NOT-PERSIST"
        bridge = FakeBridge(
            _inventory(documents=[]),
            pages=[{"data": {"rows": [{"Ref": "first", "Amount": raw_value}],
                              "hasMore": True, "nextCursor": "1",
                              "coverage": {"status": "metadata-driven-unverified",
                                           "unsupported": []}}},
                   {"data": {"rows": [{"Ref": "second", "Amount": raw_value}],
                              "hasMore": False, "nextCursor": None,
                              "coverage": {"status": "metadata-driven-unverified",
                                           "unsupported": []}}}])
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        first = v77scan.source_scan_page(self.config, bridge, start["scanId"], page_budget=1, limit=1)
        self.assertEqual(first["status"], "running")
        self.assertEqual(bridge.calls[-1]["cursor"], None)
        second = v77scan.source_scan_page(self.config, bridge, start["scanId"], page_budget=1, limit=1)
        self.assertEqual(second["status"], "partial")
        self.assertEqual(bridge.calls[-1]["cursor"], "1")
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        encoded_receipt = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(raw_value, encoded_receipt)
        self.assertNotIn(raw_value.encode("utf-8"), self.scan_path.read_bytes())
        self.assertEqual(receipt["datasets"][0]["rowCount"], 2)
        self.assertEqual(receipt["datasets"][0]["pageCount"], 2)
        self.assertEqual(receipt["assertion"], "partial_or_unverified_source_coverage")
        self.assertTrue(any(item["kind"] == "source_snapshot" for item in receipt["unsupported"]))

    def test_scan_rejects_unbounded_cursor_before_persisting(self):
        bridge = FakeBridge(
            _inventory(documents=[]),
            pages=[{"data": {"rows": [{"Ref": "first"}],
                              "hasMore": True,
                              "nextCursor": "../../../private",
                              "coverage": {"status": "metadata-driven-unverified",
                                           "unsupported": []}}}])
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        status = v77scan.source_scan_page(self.config, bridge, start["scanId"], page_budget=1, limit=1)
        self.assertEqual(status["status"], "blocked")
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        self.assertEqual(receipt["datasets"][0]["rowCount"], 0)
        self.assertEqual(receipt["datasets"][0]["cursor"], None)

    def test_scan_page_blocks_on_source_error(self):
        bridge = FakeBridge(_inventory(documents=[]),
                            errors=[source.SourceError("Windows adapter error: COM_ACCESS_DENIED")])
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        status = v77scan.source_scan_page(self.config, bridge, start["scanId"], page_budget=1, limit=1)
        self.assertEqual(status["status"], "blocked")
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["datasets"][0]["status"], "blocked")
        self.assertEqual(receipt["datasets"][0]["reason"], "SourceError")

    def test_status_and_receipt_read_private_store_without_source_call(self):
        bridge = FakeBridge(_inventory(catalogs=[], documents=[]))
        start = v77scan.source_scan_start(self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        self.assertEqual(v77scan.source_scan_status(self.config, start["scanId"])["status"],
                         "partial")
        receipt = v77scan.source_scan_receipt(self.config, start["scanId"])
        self.assertEqual(receipt["datasetCount"], 0)
        self.assertEqual(receipt["assertion"], "partial_or_unverified_source_coverage")

    def test_mcp_server_exposes_scan_control_tools(self):
        server = (ROOT / "src/agent_bridge/agent_bridge/server.py").read_text(encoding="utf-8")
        for name in ("source_scan_start", "source_scan_page",
                     "source_scan_status", "source_scan_receipt"):
            self.assertIn(f"def {name}", server)
            self.assertIn(f"return _bridge().{name}", server)


if __name__ == "__main__":
    unittest.main()
