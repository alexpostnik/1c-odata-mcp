"""Optional source-bound bank graph startup; never touches OData or its gates."""
from __future__ import annotations

import hashlib
import importlib.util
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path, PurePosixPath
import re
from time import perf_counter


class BankGraphStartupError(ValueError):
    """Requested bank graph package cannot safely start; no archive fallback."""


def register_bound_bank_graph(server, binding_path, expected_revision, expected_binding_sha256, *, background=False):
    """Verify one committed package and load its own adapter/reader exactly once.

    Trust admission completes before imports/registration and either transport.
    Background mode publishes ready only after canonical and source binding checks.
    Missing/changed packages prevent startup, without an earlier-source fallback. The commit's accepted
    status is an operator precondition, not something a local digest proves.
    """
    started = perf_counter()
    try:
        if getattr(server, "_bank_graph_lifecycle", None) is not None:
            raise BankGraphStartupError("bank graph already selected for this server; start a new process to change revision")
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
            raise BankGraphStartupError("an exact accepted bank graph revision is required")
        if not isinstance(expected_binding_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_binding_sha256):
            raise BankGraphStartupError("a trusted export receipt binding SHA256 is required")
        try:
            installed_sdk = version("mcp")
        except PackageNotFoundError:
            raise BankGraphStartupError("bank graph requires installed MCP SDK exactly 2.2.0") from None
        if installed_sdk != "2.2.0":
            raise BankGraphStartupError("bank graph requires installed MCP SDK exactly 2.2.0")
        original_path = Path(binding_path).absolute()
        if original_path.is_symlink() or original_path.parent.is_symlink():
            raise BankGraphStartupError("select a regular binding and package root without symlinks")
        path = original_path.resolve(strict=True)
        root = path.parent
        if path.name != "bank-graph-binding.json":
            raise BankGraphStartupError("select the package's bank-graph-binding.json")
        if path.stat().st_size > 262144:
            raise BankGraphStartupError("bank graph binding exceeds supported size")
        binding_bytes = path.read_bytes()
        binding_digest = hashlib.sha256(binding_bytes).hexdigest()
        # The expected digest comes from the separately retained trusted export
        # receipt, never from this mutable package or its revision claim.
        if binding_digest != expected_binding_sha256:
            raise BankGraphStartupError("bank graph binding digest differs from trusted export receipt")
        binding = json.loads(binding_bytes)
        if (not isinstance(binding, dict) or set(binding) != {"schema_version", "repository", "revision", "files"}
                or type(binding["schema_version"]) is not int or binding["schema_version"] != 1
                or binding["repository"] != "onec-community-toolkit" or binding["revision"] != expected_revision):
            raise BankGraphStartupError("bank graph binding does not match the requested revision")
        files = binding["files"]
        if not isinstance(files, dict) or not files:
            raise BankGraphStartupError("bank graph binding requires file digests")
        required = {
            "onec_bank_graph/reader.py", "onec_bank_graph/mcp_adapter.py", "tools/bank-graph/launch.py",

            "archive/manifest.json",
            "onec_bank_graph/references/graph.schema.json",
            "src/onec-odata-mcp/onec_odata_mcp/server.py",
            "src/onec-odata-mcp/onec_odata_mcp/bank_graph.py",
        }
        required.update("onec_bank_graph/scripts/" + name + ".py" for name in
                        ("payment_text", "statement_graph", "bank_identifiers", "archive_adapter", "modern_adapter", "source_readiness", "counterparty_identity"))
        if not required <= files.keys():
            raise BankGraphStartupError("bank graph package is incomplete")
        # The connector must run from that same release directory, not an older
        # installed wheel with a separately copied skill or replacement reader.
        if Path(__file__).resolve() != root / "src/onec-odata-mcp/onec_odata_mcp/bank_graph.py":
            raise BankGraphStartupError("run connector and reader from the same revision package")
        for relative, digest in files.items():
            if (not isinstance(relative, str) or PurePosixPath(relative).is_absolute()
                    or ".." in PurePosixPath(relative).parts or "\\" in relative
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise BankGraphStartupError("invalid bank graph binding path or digest")
            selected = root / relative
            if selected.is_symlink() or not selected.resolve(strict=True).is_relative_to(root):
                raise BankGraphStartupError("bank graph binding must stay inside its package")
            if hashlib.sha256(selected.read_bytes()).hexdigest() != digest:
                raise BankGraphStartupError("bank graph package file digest mismatch")
        # The external isolated launcher performs this before connector import.
        # Repeat the same complete-tree check for direct API callers before the
        # adapter is executed; this copy is already authenticated above.
        bootstrap_spec = importlib.util.spec_from_file_location("_onec_bound_bank_graph_bootstrap", root / "tools/bank-graph/launch.py")
        bootstrap = importlib.util.module_from_spec(bootstrap_spec)
        bootstrap_spec.loader.exec_module(bootstrap)
        bootstrap.verify_package(path, expected_revision, expected_binding_sha256)
        adapter_path = root / "onec_bank_graph/mcp_adapter.py"
        spec = importlib.util.spec_from_file_location("_onec_bound_bank_graph_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = {"repository": binding["repository"], "revision": expected_revision,
                    "binding_sha256": binding_digest,
                    "package_validation_seconds": perf_counter() - started,
                    "acceptance": "operator-selected revision; trusted export binding and local content hashes verified"}
        def validate_reader(reader):
            # This final check is inside initialization, before ready publication.
            for name, digest in reader.files.items():
                if files.get("archive/" + name) != digest:
                    raise BankGraphStartupError("bank graph source is outside the revision binding")
        selected = module.register_bank_graph_tools(server, root / "archive", source_binding=metadata,
                                                    background=background, reader_validator=validate_reader)
        if not background:
            server._bank_graph_reader = selected
        return selected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, RecursionError, OverflowError) as exc:
        if isinstance(exc, BankGraphStartupError):
            raise
        raise BankGraphStartupError("invalid or unavailable bank graph package; startup stopped") from exc
