"""Optional injected MCPServer 2.2.0 adapter. No OData/config/runtime imports."""
from pathlib import Path
import importlib.util
from importlib.metadata import version, PackageNotFoundError
from time import perf_counter
from typing import Any, Annotated, Literal
from threading import RLock, Thread, Event

# Load only this checkout's sibling; never bind or publish the host's generic
# top-level `reader`. No sys.path/sys.modules/import-hook changes are needed.
_reader_spec = importlib.util.spec_from_file_location(
    "_bank_graph_adapter.reader", Path(__file__).resolve().with_name("reader.py"))
_reader_module = importlib.util.module_from_spec(_reader_spec)
_reader_spec.loader.exec_module(_reader_module)
Reader, QueryError = _reader_module.Reader, _reader_module.QueryError
VERSION, contract = _reader_module.VERSION, _reader_module.contract


class BankGraphSDKError(ValueError):
    """The adapter's exact supported SDK is unavailable; stop before indexing."""


def require_bank_graph_sdk():
    """Admit the installed distribution before using version-specific MCP APIs."""
    try:
        installed = version("mcp")
    except PackageNotFoundError:
        raise BankGraphSDKError("bank graph requires installed MCP SDK exactly 2.2.0") from None
    if installed != "2.2.0":
        raise BankGraphSDKError("bank graph requires installed MCP SDK exactly 2.2.0")


class BankGraphLifecycle:
    """One private candidate; no query can observe incomplete validation.

    The loader never holds the publication lock while reading/indexing. Closing
    fences publication and drops data immediately; the daemon cannot delay exit.
    Python cannot interrupt a running synchronous Reader; it finishes privately.
    """

    def __init__(self, archive, validator=None, *, background=True):
        self.archive, self.validator = archive, validator
        self.background = background
        self.lock = RLock()
        self.done = Event()
        self.reader = None
        self.state = "initializing"
        self.load_seconds = None
        self.closed = False
        self.thread = None

    def load(self):
        started = perf_counter()
        try:
            candidate = Reader(self.archive)
            if self.validator is not None:
                self.validator(candidate)
        except Exception:
            # No source values, paths or exception details cross this boundary.
            with self.lock:
                self.load_seconds = perf_counter() - started
                self.state = "failed"
            self.done.set()
            return
        with self.lock:
            self.load_seconds = perf_counter() - started
            if not self.closed:
                self.reader = candidate
                self.state = "ready"
        self.done.set()

    def start(self):
        with self.lock:
            if self.thread is not None or self.closed:
                raise ValueError("bank graph loader already started or closed")
            self.thread = Thread(target=self.load, name="bank-graph-initializer", daemon=True)
            self.thread.start()

    def close(self):
        with self.lock:
            self.closed = True
            self.reader = None
            self.state = "failed"

    def status(self):
        with self.lock:
            result = {"state": self.state,
                      "mode": ("one background validation/index; data unavailable until ready" if self.background
                               else "one synchronous offline validation/index"),
                      "archive_load_and_index_seconds": self.load_seconds}
            if self.state == "failed":
                result["error"] = "bank graph initialization failed or server stopped; no data or fallback"
            return result


def register_bank_graph_tools(server, archive, *, source_binding=None, background=False, reader_validator=None):
    """Register six read-only tools and discovery resources on a caller's server.

    Caller chooses the archive once, outside tool arguments. Tests use a fresh
    isolated server; this function neither starts nor updates a running connector.
    """
    require_bank_graph_sdk()
    from mcp.types import ToolAnnotations
    from pydantic import Field

    PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
    from mcp.server.mcpserver.exceptions import ToolError

    if getattr(server, "_bank_graph_lifecycle", None) is not None:
        raise ValueError("bank graph already selected for this server")
    lifecycle = BankGraphLifecycle(archive, reader_validator, background=background)
    reader = None
    if not background:
        # Existing direct/offline callers retain exceptions and a ready Reader.
        started = perf_counter()
        reader = Reader(archive)
        if reader_validator is not None:
            reader_validator(reader)
        lifecycle.reader, lifecycle.state = reader, "ready"
        lifecycle.load_seconds = perf_counter() - started
        lifecycle.done.set()
    # Freeze selected metadata/manual; only explicit lifecycle state changes.
    # No graph is read into the agent context.
    manual = (Path(__file__).resolve().parent / "references/reader-manual.md").read_text(encoding="utf-8")
    binding = _reader_module.detached_json(source_binding) if source_binding is not None else None
    lock = RLock()

    def discovered_contract():
        result = contract()
        if binding is not None:
            result["source_binding"] = _reader_module.detached_json(binding)
        if background or binding is not None:
            result["startup"] = lifecycle.status()
        return _reader_module.bounded(result)

    def invoke(method, *args):
        with lifecycle.lock:
            selected = lifecycle.reader
            state = lifecycle.state
        if selected is None:
            raise ToolError("bank graph " + state + "; read bank_graph_contract.startup; data unavailable, no fallback")
        with lock:
            try:
                return getattr(selected, method)(*args)
            except QueryError as exc:
                raise ToolError(str(exc)) from exc
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=False)

    @server.tool(name="bank_graph_contract", annotations=annotations, structured_output=True)
    def bank_graph_contract() -> dict[str, Any]:
        """Read inputs, limits, typed edges and startup.state before queries; data requires ready."""
        return discovered_contract()

    @server.tool(name="bank_graph_overview", annotations=annotations, structured_output=True)
    def bank_graph_overview() -> dict[str, Any]:
        """Read content snapshot, observed counts, statement units and declared source acceptance."""
        result = invoke("overview")
        if binding is not None:
            result["source_binding"] = _reader_module.detached_json(binding)
        return _reader_module.bounded(result)

    @server.tool(name="bank_graph_search", annotations=annotations, structured_output=True)
    def bank_graph_search(filters: dict[str, str] | None = None, limit: PageLimit = 20, cursor: str | None = None) -> dict[str, Any]:
        """AND filters from contract; bounded candidates. Substring is not revenue classification.

        Dates are inclusive transaction dates. Default selection=include. Follow
        next_cursor with identical filters/limit; get exact ID for full purpose.
        """
        return invoke("search", filters, limit, cursor)

    @server.tool(name="bank_graph_get", annotations=annotations, structured_output=True)
    def bank_graph_get(node_id: str) -> dict[str, Any]:
        """Read exact opaque node ID with complete properties/observations/raw fields. No truncation."""
        return invoke("get", node_id)

    @server.tool(name="bank_graph_links", annotations=annotations, structured_output=True)
    def bank_graph_links(node_id: str, edge_types: list[str] | None = None,
                         direction: Literal["in", "out", "both"] = "both", limit: PageLimit = 20, cursor: str | None = None) -> dict[str, Any]:
        """Read one-hop typed edges and neighbor IDs; direction=in/out/both. Get neighbors separately."""
        return invoke("links", node_id, edge_types, direction, limit, cursor)

    @server.tool(name="bank_graph_aggregate", annotations=annotations, structured_output=True)
    def bank_graph_aggregate(filters: dict[str, str] | None = None, group_by: Literal["statement", "year", "counterparty", "none"] = "statement",
                             limit: PageLimit = 20, cursor: str | None = None) -> dict[str, Any]:
        """Exact Decimal amounts, grouped by statement/year/counterparty/none and always unit/epoch/currency/direction.

        Pages contain complete groups. Held rows remain in sums; unknown amounts
        are counted separately. Never add fee/basis components or convert money.
        """
        return invoke("aggregate", filters, group_by, limit, cursor)

    @server.resource("bank-graph://contract", mime_type="application/json")
    def bank_graph_contract_resource() -> str:
        return _reader_module.encoded(discovered_contract())

    @server.resource("bank-graph://manual", mime_type="text/markdown")
    def bank_graph_manual_resource() -> str:
        if background:
            return "Bank graph startup.state=" + lifecycle.status()["state"] + ". Read bank_graph_contract for current readiness; data requires ready.\n\n" + manual
        return manual

    # MCP 2.2's default argument model ignores extra keys and may parse JSON
    # strings as objects. Validate the original JSON through the same reader
    # boundary before that model, without changing unrelated OData tools.
    from mcp.types import CallToolResult, TextContent
    from mcp_types import SERVER_INFO_META_KEY
    from mcp_types.methods import serialize_server_result
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS
    allowed = {
        "bank_graph_contract": set(), "bank_graph_overview": set(),
        "bank_graph_search": {"filters", "limit", "cursor"},
        "bank_graph_get": {"node_id"},
        "bank_graph_links": {"node_id", "edge_types", "direction", "limit", "cursor"},
        "bank_graph_aggregate": {"filters", "group_by", "limit", "cursor"},
    }

    async def bank_graph_boundary(ctx, call_next):
        params = ctx.params or {}
        name = params.get("name")
        if ctx.method == "tools/call" and isinstance(name, str) and name in allowed:
            try:
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict) or set(arguments) - allowed[name]:
                    raise QueryError("unknown bank graph parameters; read bank_graph_contract")
                raw = _reader_module.encoded(arguments)
                if len(raw.encode("utf-8")) > _reader_module.MAX_QUERY_BYTES:
                    raise QueryError("bank graph parameters exceed 8192 UTF-8 bytes")
                _reader_module.strict_json(raw)
                if "filters" in arguments and arguments["filters"] is not None and not isinstance(arguments["filters"], dict):
                    raise QueryError("filters must be a JSON object or null")
                if "edge_types" in arguments and arguments["edge_types"] is not None and not isinstance(arguments["edge_types"], list):
                    raise QueryError("edge_types must be a JSON array or null")
            except QueryError as exc:
                # Middleware short-circuits bypass the SDK serializer. Own the
                # complete envelope for the pinned SDK's legacy/modern eras.
                rejected = CallToolResult(is_error=True, content=[TextContent(type="text", text=str(exc))]).model_dump(
                    by_alias=True, mode="json", exclude_none=True)
                rejected = serialize_server_result(ctx.method, ctx.protocol_version, rejected)
                if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
                    rejected["resultType"] = "complete"
                    rejected["_meta"] = {SERVER_INFO_META_KEY: server._lowlevel_server.server_info_stamp}
                return rejected
        result = await call_next(ctx)
        if ctx.method in {"initialize", "server/discover"}:
            result["instructions"] = "\n\n".join(filter(None, (result.get("instructions"),
                "Bank graph is available through six read-only bank_graph_* tools and bank-graph://contract/manual. "
                "Start with bank_graph_contract; check startup.state (initializing/ready/failed). Only when ready call bank_graph_overview and bounded search; use its opaque ID for get and typed links. If initializing, check contract later; failed requires operator action and never falls back. "
                + contract()["instructions"])))
        if ctx.method == "tools/list":
            # Schema and enforcement agree; keep every existing tool intact.
            for tool in result["tools"]:
                if tool["name"] in allowed:
                    tool["inputSchema"] = {**tool["inputSchema"], "additionalProperties": False}
        return result

    server.middleware.append(bank_graph_boundary)

    server._bank_graph_lifecycle = lifecycle
    if background:
        lifecycle.start()
        return lifecycle
    return reader


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help="operator-selected private archive directory")
    args = parser.parse_args()
    try:
        require_bank_graph_sdk()
    except BankGraphSDKError as exc:
        parser.error(str(exc))
    from mcp.server import MCPServer
    server = MCPServer("bank-graph-reader", version=VERSION, instructions=contract()["instructions"])
    lifecycle = register_bank_graph_tools(server, args.archive, background=True)
    try:
        server.run(transport="stdio")
    finally:
        lifecycle.close()


if __name__ == "__main__":
    main()
