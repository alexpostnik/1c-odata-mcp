"""Synthetic checks for catalog/constants inventory receipts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from agent_bridge import v77masterdata

ROOT = Path(__file__).resolve().parents[1]


def _empty_sections() -> dict:
    return {
        name: {"present": False, "boundedObjectCount": 0, "countLimit": 100000}
        for name in ("constants", "enumerations", "registers", "journals",
                     "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors")
    }


def _section_coverage(*, constants: bool = True) -> dict:
    return {
        "constants": {"present": constants, "rootSectionCount": 1 if constants else 0,
                      "boundedObjectCount": 1 if constants else 0,
                      "countLimit": 100000, "sectionNames": ["Consts", "Const"]},
        "catalogs": {"present": True, "rootSectionCount": 1, "boundedObjectCount": 1,
                     "countLimit": 100000, "sectionNames": ["SbCnts"]},
        "documents": {"present": False, "rootSectionCount": 0, "boundedObjectCount": 0,
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
        "unmapped": {"present": False, "rootSectionCount": 0,
                     "boundedObjectCount": 0, "countLimit": 100000},
    }


def _inventory(*, catalog_rows: bool = True, constants: bool = True,
               extra_catalogs: list[dict] | None = None) -> dict:
    catalog_attributes = [{
        "id": "2", "name": "Rate", "typeCode": "N", "length": 10,
        "precision": 2, "refTypeId": "",
    }]
    catalogs = [{"id": "1", "name": "Currencies", "attributes": catalog_attributes}]
    catalogs.extend(extra_catalogs or [])
    constant_items = ([{
        "id": "3", "name": "DefaultCurrency", "typeCode": "S",
        "length": 10, "precision": 0, "refTypeId": "",
    }] if constants else [])
    counts = {
        "catalogs": len(catalogs),
        "documents": 0,
        "constants": len(constant_items),
        "attributes": len(catalog_attributes),
        "catalogAttributes": len(catalog_attributes),
        "documentHeadAttributes": 0,
        "documentTableAttributes": 0,
        "totalMetadataAttributes": len(catalog_attributes) + len(constant_items),
    }
    sections = _empty_sections()
    sections["constants"] = {"present": constants, "boundedObjectCount": len(constant_items),
                             "countLimit": 100000}
    return {"data": {
        "status": "metadata_catalog",
        "counts": counts,
        "catalogs": catalogs if catalog_rows else catalogs,
        "documents": [],
        "constants": constant_items,
        "otherSections": sections,
        "sectionCoverage": _section_coverage(constants=constants),
    }}


class FakeBridge:
    def __init__(self, inventory: dict, pages: list[dict] | None = None):
        self.inventory = inventory
        self.pages = pages or []
        self.calls: list[dict] = []

    def source_metadata_inventory(self, source_alias: str) -> dict:
        self.calls.append({"operation": "source_metadata_inventory", "sourceAlias": source_alias})
        return self.inventory

    def source_read_page(self, source_alias: str, kind: str, type_name: str,
                         from_date: str | None = None,
                         to_exclusive: str | None = None,
                         cursor: str | None = None,
                         limit: int = 100) -> dict:
        self.calls.append({"operation": "source_read_page", "kind": kind,
                           "typeName": type_name, "cursor": cursor, "limit": limit})
        return self.pages.pop(0)

    def source_read_constants_page(self, source_alias: str,
                                   cursor: str | None = None,
                                   limit: int = 100) -> dict:
        self.calls.append({"operation": "source_read_constants_page",
                           "cursor": cursor, "limit": limit})
        return self.pages.pop(0)

    def source_read_periodic_history_page(self, source_alias: str, kind: str,
                                          type_name: str, from_date: str,
                                          to_exclusive: str,
                                          attribute_name: str | None = None,
                                          owner_cursor: str | None = None,
                                          history_cursor: str | None = None,
                                          limit: int = 100) -> dict:
        self.calls.append({"operation": "source_read_periodic_history_page",
                           "kind": kind, "typeName": type_name,
                           "attributeName": attribute_name,
                           "ownerCursor": owner_cursor,
                           "historyCursor": history_cursor,
                           "limit": limit})
        return self.pages.pop(0)


class V77MasterdataInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.inventory_path = Path(self.directory.name) / "catalog-constants.sqlite3"
        self.config = {
            "ledger": {"path": str(Path(self.directory.name) / "ledger.sqlite3")},
            "catalogConstantsInventoryLedger": {"path": str(self.inventory_path)},
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_inventory_default_plan_reads_catalogs_and_current_constants_only(self):
        raw_value = "SECRET-CURRENT-VALUE"
        bridge = FakeBridge(_inventory(), pages=[
            {"data": {"rows": [{"code": "USD", "description": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-driven-unverified",
                                   "unsupported": []}}},
            {"data": {"rows": [{"name": "DefaultCurrency", "value": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "provisional-constant-context",
                                   "unsupported": []}}},
        ])
        start = v77masterdata.source_catalog_constants_inventory_start(
            self.config, bridge, "src77", "2012-01-01", "2013-01-01")
        self.assertEqual(start["taskKinds"], {"catalog": 1, "constants": 1})
        advanced = v77masterdata.source_catalog_constants_inventory_page(
            self.config, bridge, start["inventoryId"], page_budget=5, limit=10)
        self.assertEqual(advanced["status"], "partial")
        receipt = v77masterdata.source_catalog_constants_inventory_receipt(
            self.config, start["inventoryId"])
        encoded_receipt = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(raw_value, encoded_receipt)
        self.assertEqual(receipt["historyRowCount"], 0)
        self.assertEqual(receipt["history"]["constantTargets"], [])
        self.assertEqual(receipt["history"]["catalogAttributeTargets"], [])
        self.assertNotIn("constant_history", receipt["taskKinds"])
        self.assertNotIn("catalog_attribute_history", receipt["taskKinds"])
        self.assertTrue(all("history" not in limitation for limitation in receipt["limitations"]))
        self.assertEqual([call["operation"] for call in bridge.calls], [
            "source_metadata_inventory",
            "source_read_page",
            "source_read_constants_page",
        ])

    def test_inventory_hashes_catalog_constants_and_history_without_persisting_values(self):
        raw_value = "SECRET-CATALOG-CONSTANT-HISTORY-VALUE"
        bridge = FakeBridge(_inventory(), pages=[
            {"data": {"rows": [{"code": "USD", "description": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-driven-unverified",
                                   "unsupported": []}}},
            {"data": {"rows": [{"name": "DefaultCurrency", "value": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "provisional-constant-context",
                                   "unsupported": []}}},
            {"data": {"target": {"kind": "constant", "constantName": "DefaultCurrency"},
                      "rows": [{"date": "2012-02-03", "value": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-linked-complete-unverified",
                                   "metadataLink": {"kind": "constant",
                                                    "constantName": "DefaultCurrency"},
                                   "unsupported": []}}},
            {"data": {"target": {"kind": "catalog_attribute", "catalogName": "Currencies",
                                 "attributeName": "Rate"},
                      "rows": [{"date": "2012-02-04", "value": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-linked-complete-unverified",
                                   "metadataLink": {"kind": "catalog_attribute",
                                                    "catalogName": "Currencies",
                                                    "attributeName": "Rate"},
                                   "unsupported": []}}},
        ])
        start = v77masterdata.source_catalog_constants_inventory_start(
            self.config, bridge, "src77", "2012-01-01", "2013-01-01",
            include_history=True)
        self.assertEqual(start["status"], "running")
        advanced = v77masterdata.source_catalog_constants_inventory_page(
            self.config, bridge, start["inventoryId"], page_budget=4, limit=10)
        self.assertEqual(advanced["status"], "partial")
        receipt = v77masterdata.source_catalog_constants_inventory_receipt(
            self.config, start["inventoryId"])
        encoded_receipt = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(raw_value, encoded_receipt)
        self.assertNotIn(raw_value.encode("utf-8"), self.inventory_path.read_bytes())
        self.assertEqual(receipt["nonEmptyCatalogCount"], 1)
        self.assertEqual(receipt["nonEmptyCatalogs"][0]["rowCount"], 1)
        self.assertEqual(receipt["constants"]["rowCount"], 1)
        self.assertEqual(receipt["historyRowCount"], 2)
        self.assertFalse(receipt["complete"])
        self.assertEqual([call["operation"] for call in bridge.calls], [
            "source_metadata_inventory",
            "source_read_page",
            "source_read_constants_page",
            "source_read_periodic_history_page",
            "source_read_periodic_history_page",
        ])

    def test_empty_catalog_skips_catalog_attribute_history(self):
        bridge = FakeBridge(_inventory(constants=False), pages=[
            {"data": {"rows": [], "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-driven-unverified",
                                   "unsupported": []}}},
        ])
        start = v77masterdata.source_catalog_constants_inventory_start(
            self.config, bridge, "src77", "2012-01-01", "2013-01-01",
            include_history=True)
        advanced = v77masterdata.source_catalog_constants_inventory_page(
            self.config, bridge, start["inventoryId"], page_budget=5, limit=10)
        self.assertEqual(advanced["status"], "partial")
        receipt = v77masterdata.source_catalog_constants_inventory_receipt(
            self.config, start["inventoryId"])
        self.assertEqual(receipt["nonEmptyCatalogCount"], 0)
        self.assertEqual(receipt["emptyCatalogCount"], 1)
        self.assertEqual(receipt["history"]["catalogAttributeTargets"][0]["status"], "skipped")
        self.assertEqual([call["operation"] for call in bridge.calls], [
            "source_metadata_inventory",
            "source_read_page",
        ])

    def test_filled_catalog_selection_skips_unselected_catalog_pages(self):
        raw_value = "SECRET-SELECTED-CATALOG-VALUE"
        bridge = FakeBridge(_inventory(extra_catalogs=[
            {"id": "4", "name": "EmptyCatalog", "attributes": []},
        ]), pages=[
            {"data": {"rows": [{"code": "USD", "description": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "metadata-driven-unverified",
                                   "unsupported": []}}},
            {"data": {"rows": [{"name": "DefaultCurrency", "value": raw_value}],
                      "hasMore": False, "nextCursor": None,
                      "coverage": {"status": "provisional-constant-context",
                                   "unsupported": []}}},
        ])
        start = v77masterdata.source_catalog_constants_inventory_start(
            self.config, bridge, "src77", "2012-01-01", "2013-01-01",
            filled_catalog_names=["Currencies"],
            filled_catalog_selection_source="live-catalog-presence-scan",
            filled_catalog_selection_receipt_hash=(
                "0123456789abcdef0123456789abcdef"
                "0123456789abcdef0123456789abcdef"
            ))
        self.assertEqual(start["metadataCounts"]["counts"]["catalogs"], 2)
        self.assertEqual(start["metadataCounts"]["catalogSelection"]["metadataCatalogCount"], 2)
        self.assertEqual(start["metadataCounts"]["catalogSelection"]["selectedCatalogCount"], 1)
        self.assertEqual(start["metadataCounts"]["catalogSelection"]["skippedCatalogCount"], 1)
        self.assertEqual(start["metadataCounts"]["catalogSelection"]["source"],
                         "live-catalog-presence-scan")
        self.assertEqual(start["selectedCatalogCount"], 1)
        self.assertEqual(start["skippedCatalogCount"], 1)
        advanced = v77masterdata.source_catalog_constants_inventory_page(
            self.config, bridge, start["inventoryId"], page_budget=5, limit=10)
        self.assertEqual(advanced["status"], "partial")
        receipt = v77masterdata.source_catalog_constants_inventory_receipt(
            self.config, start["inventoryId"])
        encoded_receipt = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(raw_value, encoded_receipt)
        self.assertEqual(receipt["nonEmptyCatalogCount"], 1)
        self.assertEqual(receipt["emptyCatalogCount"], 0)
        self.assertEqual(receipt["catalogsSkippedBySelectionCount"], 1)
        self.assertEqual(receipt["metadataCounts"]["catalogSelection"]["skippedCompletenessClaim"],
                         "not-claimed-by-metadata")
        self.assertEqual(receipt["metadataCounts"]["catalogSelection"]["selectionCompletenessClaim"],
                         "caller-evidence-only; metadata validation only proves names exist")
        self.assertEqual([call for call in bridge.calls if call["operation"] == "source_read_page"], [
            {"operation": "source_read_page", "kind": "reference",
             "typeName": "Currencies", "cursor": None, "limit": 10},
        ])

    def test_filled_catalog_selection_rejects_unknown_catalog_name(self):
        bridge = FakeBridge(_inventory())
        with self.assertRaisesRegex(v77masterdata.MasterdataInventoryError,
                                    "unknown catalog"):
            v77masterdata.source_catalog_constants_inventory_start(
                self.config, bridge, "src77", "2012-01-01", "2013-01-01",
                filled_catalog_names=["MissingCatalog"])

    def test_filled_catalog_selection_rejects_unbounded_provenance(self):
        bridge = FakeBridge(_inventory())
        with self.assertRaisesRegex(v77masterdata.MasterdataInventoryError,
                                    "selection_source"):
            v77masterdata.source_catalog_constants_inventory_start(
                self.config, bridge, "src77", "2012-01-01", "2013-01-01",
                filled_catalog_names=["Currencies"],
                filled_catalog_selection_source="operator pasted full receipt text")
        with self.assertRaisesRegex(v77masterdata.MasterdataInventoryError,
                                    "selection_receipt_hash"):
            v77masterdata.source_catalog_constants_inventory_start(
                self.config, bridge, "src77", "2012-01-01", "2013-01-01",
                filled_catalog_names=["Currencies"],
                filled_catalog_selection_source="live-catalog-presence-scan",
                filled_catalog_selection_receipt_hash="sha256:presence-receipt")

    def test_mcp_server_exposes_catalog_constants_inventory_tools(self):
        server = (ROOT / "src/agent_bridge/agent_bridge/server.py").read_text(encoding="utf-8")
        for name in ("source_catalog_constants_inventory_start",
                     "source_catalog_constants_inventory_page",
                     "source_catalog_constants_inventory_status",
                     "source_catalog_constants_inventory_receipt"):
            self.assertIn(f"def {name}", server)
            self.assertIn(f"return _bridge().{name}", server)


if __name__ == "__main__":
    unittest.main()
