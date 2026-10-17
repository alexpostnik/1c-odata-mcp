"""Official Python MCP SDK server. Never writes to a 1C target."""
from __future__ import annotations

import argparse
from functools import lru_cache

from mcp.server import MCPServer

from .core import Bridge
from .tool_errors import source_tool_errors

mcp = MCPServer("1C live bridge")


@lru_cache(maxsize=1)
def _bridge() -> Bridge:
    return Bridge.from_environment()


@mcp.tool()
@source_tool_errors
def transport_health(source_alias: str) -> dict:
    """Check only the Mac to Windows SSH transport; no 1C command is run."""
    return _bridge().transport_health(source_alias)


@mcp.tool()
@source_tool_errors
def source_health(source_alias: str) -> dict:
    """Check the named read-only Windows 1C 7.7 adapter."""
    return _bridge().source_health(source_alias)


@mcp.tool()
@source_tool_errors
def source_schema(source_alias: str) -> dict:
    """Inspect named 7.7 metadata and explicit adapter capabilities."""
    return _bridge().source_schema(source_alias)


@mcp.tool()
@source_tool_errors
def source_metadata_inventory(source_alias: str) -> dict:
    """Read the named 7.7 1Cv7.MD metadata inventory without opening row data."""
    return _bridge().source_metadata_inventory(source_alias)


@mcp.tool()
@source_tool_errors
def source_document_presence_page(source_alias: str,
                                  from_date: str, to_exclusive: str,
                                  cursor: str | None = None,
                                  limit: int = 1000) -> dict:
    """Read one bounded aggregate page of actual 7.7 document types present in a period."""
    return _bridge().source_document_presence_page(
        source_alias, from_date, to_exclusive, cursor, limit,
    )


@mcp.tool()
@source_tool_errors
def source_read_documents_page(source_alias: str, document_type: str,
                               from_date: str, to_exclusive: str,
                               cursor: str | None = None, limit: int = 100) -> dict:
    """Read one bounded chronological 7.7 document page, with an exclusive end date."""
    return _bridge().source_read_documents_page(source_alias, document_type,
                                                 from_date, to_exclusive, cursor, limit)


@mcp.tool()
@source_tool_errors
def source_probe_reference_identity(source_alias: str, type_name: str, cursor: str) -> dict:
    """Probe one native catalog reference roundtrip; identity/snapshot remain unverified.

    Seed offset 0..100 is provisional. No serialized reference input, scans or writes.
    Requires an adapter advertising probe_reference_identity in fresh source_schema.
    """
    return _bridge().source_probe_reference_identity(source_alias, type_name, cursor)


@mcp.tool()
@source_tool_errors
def source_read_reference_by_native(source_alias: str, type_name: str,
                                     catalog_metadata_id: str, native_reference: str,
                                     as_of: str, attribute_names: list[str],
                                     source_namespace: str | None = None) -> dict:
    """Address one saved native catalog reference on an explicit ISO as-of date.

    Token is private data, never executable input. At most 32 allowed metadata fields;
    empty fields requests identity only. Unsupported fields/relations remain on HOLD.
    Optional source_namespace guards the configured source location; scoped proof
    is required by HR bindings. No cursor/scan/Code lookup; identity stays unverified.
    """
    return _bridge().source_read_reference_by_native(
        source_alias, type_name, catalog_metadata_id, native_reference, as_of, attribute_names, source_namespace)


@mcp.tool()
@source_tool_errors
def source_acquire_document_identity(source_alias: str, document_type: str,
                                     document_metadata_id: str, from_date: str,
                                     to_exclusive: str, document_cursor: str) -> dict:
    """Acquire one native Document token from a bounded 0..100 period seed.

    Save the private typed source tuple; offsets/date/number are not identity.
    Requires fresh schema advertising acquire_document_identity; no snapshot proof.
    """
    return _bridge().source_acquire_document_identity(
        source_alias, document_type, document_metadata_id, from_date, to_exclusive, document_cursor)


@mcp.tool()
@source_tool_errors
def source_read_document_by_native(source_alias: str, document_type: str,
                                   document_metadata_id: str, native_reference: str,
                                   source_namespace: str, attribute_names: list[str],
                                   table_attribute_names: list[str], line_cursor: str = "0",
                                   limit: int = 100) -> dict:
    """Read one saved typed Document in its source namespace, with bounded lines.

    Requested header/table metadata fields only. Unsupported history/enum relations
    remain HOLD; no current-master substitution, writer or migration claim is granted.
    """
    return _bridge().source_read_document_by_native(
        source_alias, document_type, document_metadata_id, native_reference, source_namespace,
        attribute_names, table_attribute_names, line_cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_page(source_alias: str, kind: str, type_name: str,
                     from_date: str | None = None, to_exclusive: str | None = None,
                     cursor: str | None = None, limit: int = 100) -> dict:
    """Read one 7.7 document or reference page; supported kinds come from source_schema."""
    return _bridge().source_read_page(source_alias, kind, type_name,
                                      from_date, to_exclusive, cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_document_lines_page(source_alias: str, document_type: str,
                                    from_date: str, to_exclusive: str,
                                    document_cursor: str, line_cursor: str,
                                    limit: int = 100) -> dict:
    """Read one bounded 7.7 document table-line page for a document offset."""
    return _bridge().source_read_document_lines_page(source_alias, document_type,
                                                     from_date, to_exclusive,
                                                     document_cursor, line_cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_document_lines_batch(source_alias: str, document_type: str,
                                     from_date: str, to_exclusive: str,
                                     items: list[dict[str, str | int]]) -> dict:
    """Read up to ten bounded line pages for one document type and period."""
    return _bridge().source_read_document_lines_batch(source_alias, document_type,
                                                       from_date, to_exclusive, items)


@mcp.tool()
@source_tool_errors
def source_read_constants_page(source_alias: str,
                               cursor: str | None = None, limit: int = 100) -> dict:
    """Read one bounded metadata-listed 7.7 constants page."""
    return _bridge().source_read_constants_page(source_alias, cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_operations_page(source_alias: str, from_date: str, to_exclusive: str,
                                cursor: str | None = None, limit: int = 100) -> dict:
    """Read one bounded 7.7 accounting operation page, with an exclusive end date."""
    return _bridge().source_read_operations_page(source_alias, from_date,
                                                 to_exclusive, cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_operation_entries_page(source_alias: str,
                                       from_date: str, to_exclusive: str,
                                       operation_cursor: str, entry_cursor: str,
                                       limit: int = 100) -> dict:
    """Read one bounded posting page for a 7.7 operation offset."""
    return _bridge().source_read_operation_entries_page(source_alias, from_date,
                                                        to_exclusive, operation_cursor,
                                                        entry_cursor, limit)


@mcp.tool()
@source_tool_errors
def source_read_periodic_history_page(source_alias: str, kind: str, type_name: str,
                                      from_date: str, to_exclusive: str,
                                      attribute_name: str | None = None,
                                      owner_cursor: str | None = None,
                                      history_cursor: str | None = None,
                                      limit: int = 100) -> dict:
    """Read one bounded 7.7 Periodic history page for a constant or catalog attribute."""
    return _bridge().source_read_periodic_history_page(
        source_alias, kind, type_name, from_date, to_exclusive,
        attribute_name, owner_cursor, history_cursor, limit,
    )


@mcp.tool()
@source_tool_errors
def source_scan_start(source_alias: str, from_date: str, to_exclusive: str) -> dict:
    """Start a private, resumable 7.7 document/catalog coverage scan."""
    return _bridge().source_scan_start(source_alias, from_date, to_exclusive)


@mcp.tool()
@source_tool_errors
def source_scan_page(scan_id: str, page_budget: int = 1,
                     limit: int = 100, max_seconds: int = 30) -> dict:
    """Advance a source coverage scan by bounded pages and store only counts/hashes."""
    return _bridge().source_scan_page(scan_id, page_budget, limit, max_seconds)


@mcp.tool()
@source_tool_errors
def source_scan_status(scan_id: str) -> dict:
    """Read local source coverage scan progress without reading 1C data."""
    return _bridge().source_scan_status(scan_id)


@mcp.tool()
@source_tool_errors
def source_scan_receipt(scan_id: str) -> dict:
    """Return a sanitized source scan receipt with counts, cursors and hashes only."""
    return _bridge().source_scan_receipt(scan_id)


@mcp.tool()
@source_tool_errors
def source_catalog_constants_inventory_start(source_alias: str,
                                             from_date: str,
                                             to_exclusive: str,
                                             include_history: bool = False,
                                             filled_catalog_names: list[str] | None = None,
                                             filled_catalog_selection_source: str | None = None,
                                             filled_catalog_selection_receipt_hash: str | None = None) -> dict:
    """Start a private inventory of selected filled catalogs and current constants."""
    return _bridge().source_catalog_constants_inventory_start(
        source_alias, from_date, to_exclusive, include_history,
        filled_catalog_names, filled_catalog_selection_source,
        filled_catalog_selection_receipt_hash)


@mcp.tool()
@source_tool_errors
def source_catalog_constants_inventory_page(inventory_id: str,
                                            page_budget: int = 1,
                                            limit: int = 100,
                                            max_seconds: int = 30) -> dict:
    """Advance a catalog/constants inventory and store only counts and hashes."""
    return _bridge().source_catalog_constants_inventory_page(
        inventory_id, page_budget, limit, max_seconds)


@mcp.tool()
@source_tool_errors
def source_catalog_constants_inventory_status(inventory_id: str) -> dict:
    """Read local catalog/constants inventory progress without reading 1C data."""
    return _bridge().source_catalog_constants_inventory_status(inventory_id)


@mcp.tool()
@source_tool_errors
def source_catalog_constants_inventory_receipt(inventory_id: str) -> dict:
    """Return catalog/constants coverage counts without row values."""
    return _bridge().source_catalog_constants_inventory_receipt(inventory_id)


@mcp.tool()
def odata_metadata(target_alias: str) -> dict:
    """GET the current 8.3 OData v3 metadata from a configured target."""
    return _bridge().odata_metadata(target_alias)


@mcp.tool()
def odata_read_page(target_alias: str, entity_set: str, limit: int = 100,
                    from_date: str | None = None, to_exclusive: str | None = None,
                    date_property: str | None = None, cursor: str | None = None) -> dict:
    """GET one bounded 8.3 OData entity page after metadata validation."""
    return _bridge().odata_read_page(target_alias, entity_set, limit,
                                     from_date, to_exclusive, date_property, cursor)


@mcp.tool()
def odata_count(target_alias: str, entity_set: str,
                from_date: str | None = None, to_exclusive: str | None = None,
                date_property: str | None = None) -> dict:
    """GET a metadata-listed OData v3 entity count, optionally over a date interval."""
    return _bridge().odata_count(target_alias, entity_set, from_date,
                                 to_exclusive, date_property)


@mcp.tool()
def migration_preview(source_alias: str, target_alias: str, dataset_kind: str,
                      mapping_version: str, from_date: str, to_exclusive: str) -> dict:
    """Describe an intended migration slice without transforming or writing rows."""
    return _bridge().migration_preview(source_alias, target_alias, dataset_kind,
                                       mapping_version, from_date, to_exclusive)


@mcp.tool()
def target_write_readiness(target_alias: str) -> dict:
    """Report why target write is blocked; this server has no POST/PATCH/DELETE path."""
    return _bridge().target_write_readiness(target_alias)


@mcp.tool()
def year_status(year: int, target_alias: str) -> dict:
    """Read local evidence status for a configured target; no accounting verdict."""
    return _bridge().year_status(year, target_alias)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only 1C live bridge.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--bearer-token-file")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    if not args.bearer_token_file:
        parser.error("--bearer-token-file is required for streamable-http")
    from onec_mcp_runtime.transport import run_streamable_http

    run_streamable_http(
        mcp,
        host=args.host,
        port=args.port,
        token_file=args.bearer_token_file,
    )


if __name__ == "__main__":
    main()
