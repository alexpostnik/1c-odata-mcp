"""Application facade. Every network path is read-only."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import threading

from . import odata, source, v77masterdata, v77scan
from .config import load_config
from .ledger import Ledger


class UnknownSourceAliasError(ValueError):
    """A requested source alias is not present in the private configuration."""

    def __init__(self, configured_aliases: list[str]):
        self.configured_aliases = tuple(configured_aliases)
        super().__init__("unknown source alias")


class Bridge:
    def __init__(self, config: dict):
        self.config = config
        self._odata_clients: dict[str, odata.ODataClient] = {}
        self._odata_clients_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "Bridge":
        return cls(load_config())

    def _source(self, alias: str) -> dict:
        try:
            return self.config["sources"][alias]
        except KeyError as exc:
            aliases = sorted(
                alias for alias in self.config.get("sources", {})
                if isinstance(alias, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", alias)
            )
            raise UnknownSourceAliasError(aliases) from exc

    def _target(self, alias: str) -> dict:
        try:
            return self.config["targets"][alias]
        except KeyError as exc:
            raise ValueError("unknown target alias") from exc

    def _odata_client(self, alias: str) -> odata.ODataClient:
        profile = self._target(alias)
        with self._odata_clients_lock:
            client = self._odata_clients.get(alias)
            if client is None:
                client = odata.ODataClient(profile, target_alias=alias)
                self._odata_clients[alias] = client
            return client

    def source_health(self, source_alias: str) -> dict:
        return source.request(self._source(source_alias), "health", source_alias=source_alias)

    def transport_health(self, source_alias: str) -> dict:
        return source.transport_health(self._source(source_alias), source_alias=source_alias)

    def source_schema(self, source_alias: str) -> dict:
        result = source.request(self._source(source_alias), "schema", source_alias=source_alias)
        result["coverageNotice"] = "Only Windows adapter reported capabilities are supported; unlisted dataset kinds are unknown or unsupported."
        return result

    def source_metadata_inventory(self, source_alias: str) -> dict:
        result = source.request(self._source(source_alias), "metadata_inventory", source_alias=source_alias)
        result["coverageNotice"] = "Metadata inventory lists configuration objects and attributes only; it is not a row export and does not prove page-read support."
        return result

    def source_document_presence_page(self, source_alias: str,
                                      from_date: str, to_exclusive: str,
                                      cursor: str | None = None,
                                      limit: int = 1000) -> dict:
        return source.request(self._source(source_alias), "document_presence",
                              source_alias=source_alias,
                              from_date=from_date, to_exclusive=to_exclusive,
                              cursor=cursor, limit=limit)

    def source_read_documents_page(self, source_alias: str, document_type: str,
                                   from_date: str, to_exclusive: str,
                                   cursor: str | None = None, limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_documents_page",
                              source_alias=source_alias, document_type=document_type,
                              from_date=from_date, to_exclusive=to_exclusive,
                              cursor=cursor, limit=limit)

    def source_probe_reference_identity(self, source_alias: str, type_name: str,
                                        cursor: str) -> dict:
        return source.request(self._source(source_alias), "probe_reference_identity",
                              source_alias=source_alias, dataset_type=type_name, cursor=cursor)

    def source_read_reference_by_native(self, source_alias: str, type_name: str,
                                        catalog_metadata_id: str, native_reference: str,
                                        as_of: str, attribute_names: list[str],
                                        source_namespace: str | None = None) -> dict:
        return source.request(self._source(source_alias), "read_reference_by_native",
                              source_alias=source_alias, dataset_type=type_name,
                              catalog_metadata_id=catalog_metadata_id, native_reference=native_reference,
                              as_of=as_of, attribute_names=attribute_names,
                              source_namespace=source_namespace)

    def source_acquire_document_identity(self, source_alias: str, document_type: str,
                                         document_metadata_id: str, from_date: str,
                                         to_exclusive: str, document_cursor: str) -> dict:
        return source.request(self._source(source_alias), "acquire_document_identity",
                              source_alias=source_alias, document_type=document_type,
                              document_metadata_id=document_metadata_id, from_date=from_date,
                              to_exclusive=to_exclusive, document_cursor=document_cursor)

    def source_read_document_by_native(self, source_alias: str, document_type: str,
                                       document_metadata_id: str, native_reference: str,
                                       source_namespace: str, attribute_names: list[str],
                                       table_attribute_names: list[str], line_cursor: str = "0",
                                       limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_document_by_native",
                              source_alias=source_alias, document_type=document_type,
                              document_metadata_id=document_metadata_id, native_reference=native_reference,
                              source_namespace=source_namespace, attribute_names=attribute_names,
                              table_attribute_names=table_attribute_names, line_cursor=line_cursor, limit=limit)

    def source_read_page(self, source_alias: str, kind: str, type_name: str,
                         from_date: str | None = None, to_exclusive: str | None = None,
                         cursor: str | None = None, limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_page", source_alias=source_alias,
                              dataset_kind=kind, dataset_type=type_name, from_date=from_date,
                              to_exclusive=to_exclusive, cursor=cursor, limit=limit)

    def source_read_document_lines_page(self, source_alias: str, document_type: str,
                                        from_date: str, to_exclusive: str,
                                        document_cursor: str, line_cursor: str,
                                        limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_document_lines_page",
                              source_alias=source_alias, document_type=document_type,
                              from_date=from_date, to_exclusive=to_exclusive,
                              document_cursor=document_cursor, line_cursor=line_cursor,
                              limit=limit)

    def source_read_document_lines_batch(self, source_alias: str, document_type: str,
                                         from_date: str, to_exclusive: str,
                                         items: list[dict]) -> dict:
        return source.request(self._source(source_alias), "read_document_lines_batch",
                              source_alias=source_alias, document_type=document_type,
                              from_date=from_date, to_exclusive=to_exclusive,
                              line_items=items)

    def source_read_constants_page(self, source_alias: str,
                                   cursor: str | None = None, limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_constants_page",
                              source_alias=source_alias, cursor=cursor, limit=limit)

    def source_read_operations_page(self, source_alias: str,
                                    from_date: str, to_exclusive: str,
                                    cursor: str | None = None,
                                    limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_operations_page",
                              source_alias=source_alias, from_date=from_date,
                              to_exclusive=to_exclusive, cursor=cursor, limit=limit)

    def source_read_operation_entries_page(self, source_alias: str,
                                           from_date: str, to_exclusive: str,
                                           operation_cursor: str, entry_cursor: str,
                                           limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_operation_entries_page",
                              source_alias=source_alias, from_date=from_date,
                              to_exclusive=to_exclusive, operation_cursor=operation_cursor,
                              entry_cursor=entry_cursor, limit=limit)

    def source_read_periodic_history_page(self, source_alias: str, kind: str, type_name: str,
                                          from_date: str, to_exclusive: str,
                                          attribute_name: str | None = None,
                                          owner_cursor: str | None = None,
                                          history_cursor: str | None = None,
                                          limit: int = 100) -> dict:
        return source.request(self._source(source_alias), "read_periodic_history_page",
                              source_alias=source_alias, periodic_kind=kind,
                              periodic_type=type_name, periodic_attribute=attribute_name,
                              from_date=from_date, to_exclusive=to_exclusive,
                              owner_cursor=owner_cursor, history_cursor=history_cursor,
                              limit=limit)

    def source_scan_start(self, source_alias: str, from_date: str, to_exclusive: str) -> dict:
        self._source(source_alias)
        return v77scan.source_scan_start(self.config, self, source_alias, from_date, to_exclusive)

    def source_scan_page(self, scan_id: str, page_budget: int = 1,
                         limit: int = 100, max_seconds: int = 30) -> dict:
        return v77scan.source_scan_page(self.config, self, scan_id, page_budget, limit, max_seconds)

    def source_scan_status(self, scan_id: str) -> dict:
        return v77scan.source_scan_status(self.config, scan_id)

    def source_scan_receipt(self, scan_id: str) -> dict:
        return v77scan.source_scan_receipt(self.config, scan_id)

    def source_catalog_constants_inventory_start(self, source_alias: str,
                                                 from_date: str,
                                                 to_exclusive: str,
                                                 include_history: bool = False,
                                                 filled_catalog_names: list[str] | None = None,
                                                 filled_catalog_selection_source: str | None = None,
                                                 filled_catalog_selection_receipt_hash: str | None = None) -> dict:
        self._source(source_alias)
        return v77masterdata.source_catalog_constants_inventory_start(
            self.config, self, source_alias, from_date, to_exclusive, include_history,
            filled_catalog_names, filled_catalog_selection_source,
            filled_catalog_selection_receipt_hash)

    def source_catalog_constants_inventory_page(self, inventory_id: str,
                                                page_budget: int = 1,
                                                limit: int = 100,
                                                max_seconds: int = 30) -> dict:
        return v77masterdata.source_catalog_constants_inventory_page(
            self.config, self, inventory_id, page_budget, limit, max_seconds)

    def source_catalog_constants_inventory_status(self, inventory_id: str) -> dict:
        return v77masterdata.source_catalog_constants_inventory_status(self.config, inventory_id)

    def source_catalog_constants_inventory_receipt(self, inventory_id: str) -> dict:
        return v77masterdata.source_catalog_constants_inventory_receipt(self.config, inventory_id)

    def odata_metadata(self, target_alias: str) -> dict:
        profile = self._target(target_alias)
        result = self._odata_client(target_alias).metadata(force_refresh=True)
        return {"targetAlias": target_alias, "environment": profile["environment"], **result}

    def odata_read_page(self, target_alias: str, entity_set: str, limit: int = 100,
                        from_date: str | None = None, to_exclusive: str | None = None,
                        date_property: str | None = None, cursor: str | None = None) -> dict:
        return self._odata_client(target_alias).read_page(
            entity_set, limit=limit, from_date=from_date,
            to_exclusive=to_exclusive, date_property=date_property, cursor=cursor,
        )

    def odata_count(self, target_alias: str, entity_set: str,
                    from_date: str | None = None, to_exclusive: str | None = None,
                    date_property: str | None = None) -> dict:
        return self._odata_client(target_alias).count(
            entity_set, from_date=from_date, to_exclusive=to_exclusive,
            date_property=date_property,
        )

    def migration_preview(self, source_alias: str, target_alias: str,
                          dataset_kind: str, mapping_version: str,
                          from_date: str, to_exclusive: str) -> dict:
        self._source(source_alias)
        profile = self._target(target_alias)
        if not dataset_kind or not mapping_version:
            raise ValueError("dataset kind and mapping version required")
        try:
            start, end = date.fromisoformat(from_date), date.fromisoformat(to_exclusive)
        except ValueError as exc:
            raise ValueError("invalid preview period") from exc
        if start >= end or start.isoformat() != from_date or end.isoformat() != to_exclusive:
            raise ValueError("invalid preview period")
        return {"status": "preview_only", "sourceAlias": source_alias,
                "targetAlias": target_alias, "targetEnvironment": profile["environment"],
                "datasetKind": dataset_kind, "mappingVersion": mapping_version,
                "period": {"from": from_date, "toExclusive": to_exclusive},
                "transformation": "not_evaluated", "writeEnabled": False,
                "reason": "No accounting mapping or target-side identity has been verified."}

    def target_write_readiness(self, target_alias: str) -> dict:
        profile = self._target(target_alias)
        reason = ("Production writes are forbidden by this bridge." if profile["environment"] == "prod"
                  else "Test write requires exact target identity, OData user and rights, mapping, independent validation and owner receipt; no write implementation is installed.")
        return {"targetAlias": target_alias, "environment": profile["environment"],
                "status": "blocked", "writeEnabled": False, "reason": reason,
                "exactlyOnce": "unproven; target-side stable identity and reconciliation required"}

    def year_status(self, year: int, target_alias: str) -> dict:
        profile = self._target(target_alias)
        # Bind local evidence to the configured endpoint as well as its alias.
        # A reused alias pointing at a different base must see an open ledger.
        identity = {"alias": target_alias, "serviceUrl": profile["serviceUrl"],
                    "environment": profile["environment"]}
        target_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        ledger = Ledger(self.config["ledger"]["path"])
        try:
            return {"targetAlias": target_alias, **ledger.year_status(year, target_id=target_id)}
        finally:
            ledger.close()
