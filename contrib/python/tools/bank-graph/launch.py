"""Trusted external bootstrap: python -I -S -B /trusted/launch.py [arguments].

Copy this file from the independently verified accepted Git commit, outside
the mutable release. Interpreter, stdlib, dependencies and bootstrap are trusted
operator inputs. Never process package PYTHONPATH, .pth or site customization.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class PackageAdmissionError(ValueError):
    """Release membership or trusted provenance cannot be verified."""


def verify_package(binding_path, revision, expected_digest):
    """Authenticate exact files AND directories, without importing release code."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise PackageAdmissionError("an exact accepted bank graph revision is required")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise PackageAdmissionError("a trusted export receipt binding SHA256 is required")
    path = Path(binding_path).absolute()
    if path.name != "bank-graph-binding.json" or path.is_symlink() or path.parent.is_symlink():
        raise PackageAdmissionError("select a regular binding and package root without symlinks")
    root = path.parent.resolve(strict=True)
    path = root / path.name
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 262144:
        raise PackageAdmissionError("bank graph binding must be a bounded regular file")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_digest:
        raise PackageAdmissionError("bank graph binding digest differs from trusted export receipt")
    binding = json.loads(body)
    if (not isinstance(binding, dict) or set(binding) != {"schema_version", "repository", "revision", "files"}
            or type(binding["schema_version"]) is not int or binding["schema_version"] != 1
            or binding["repository"] != "onec-community-toolkit" or binding["revision"] != revision):
        raise PackageAdmissionError("bank graph binding does not match the requested revision")
    files = binding["files"]
    if not isinstance(files, dict) or not files:
        raise PackageAdmissionError("bank graph binding requires file digests")
    required = {"tools/bank-graph/launch.py", "onec_bank_graph/reader.py", "onec_bank_graph/mcp_adapter.py",
                "src/onec-odata-mcp/onec_odata_mcp/__init__.py",
                "src/onec-odata-mcp/onec_odata_mcp/server.py", "src/onec-odata-mcp/onec_odata_mcp/bank_graph.py",
                "src/mcp_http_runtime/onec_mcp_runtime/__init__.py", "src/mcp_http_runtime/onec_mcp_runtime/transport.py",

                "archive/manifest.json",
                "onec_bank_graph/references/graph.schema.json"}
    required.update("onec_bank_graph/scripts/" + name + ".py" for name in
                    ("payment_text", "statement_graph", "bank_identifiers", "archive_adapter", "modern_adapter", "source_readiness", "counterparty_identity"))
    if not required <= files.keys():
        raise PackageAdmissionError("bank graph package is incomplete")
    expected_dirs = set()
    for relative, digest in files.items():
        name = PurePosixPath(relative)
        if (not isinstance(relative, str) or not relative or name.is_absolute()
                or name.as_posix() != relative or ".." in name.parts or "\\" in relative
                or relative == path.name or "__pycache__" in name.parts
                or name.suffix in {".pyc", ".pyo", ".pth"}
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise PackageAdmissionError("invalid bank graph binding path or digest")
        expected_dirs.update(str(parent) for parent in name.parents if str(parent) != ".")
    actual_files, actual_dirs = set(), set()
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + names:
            selected = Path(directory) / name
            relative = selected.relative_to(root).as_posix()
            mode = selected.lstat().st_mode
            if stat.S_ISDIR(mode):
                actual_dirs.add(relative)
            elif stat.S_ISREG(mode):
                actual_files.add(relative)
            else:
                raise PackageAdmissionError("bank graph package requires regular files and directories without symlinks")
    if actual_files != set(files) | {path.name} or actual_dirs != expected_dirs:
        raise PackageAdmissionError("bank graph package membership differs from trusted binding")
    for relative, digest in files.items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise PackageAdmissionError("bank graph package file digest mismatch")
    return root, binding


def admit_package(binding_path, revision, digest, dependencies):
    """Expose paths only after admission, retaining trusted dependency isolation."""
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise PackageAdmissionError("trusted launcher requires Python -I -S -B")
    root, binding = verify_package(binding_path, revision, digest)
    launcher = Path(__file__).resolve(strict=True)
    if launcher.is_relative_to(root):
        raise PackageAdmissionError("use a trusted launcher outside the selected package")
    if binding["files"].get("tools/bank-graph/launch.py") != hashlib.sha256(launcher.read_bytes()).hexdigest():
        raise PackageAdmissionError("trusted launcher must match the selected committed package")
    dependency_root = Path(dependencies).resolve(strict=True)
    if not dependency_root.is_dir() or dependency_root.is_relative_to(root) or root.is_relative_to(dependency_root):
        raise PackageAdmissionError("trusted dependencies must be a separate directory outside the package")
    # -S omits site-packages even in venvs. Explicitly add operator-selected
    # wheel dependencies; do not call site.addsitedir or execute .pth files.
    sys.path.insert(0, str(dependency_root))
    try:
        installed_sdk = version("mcp")
    except PackageNotFoundError:
        raise PackageAdmissionError("bank graph requires installed MCP SDK exactly 2.2.0") from None
    if installed_sdk != "2.2.0":
        raise PackageAdmissionError("bank graph requires installed MCP SDK exactly 2.2.0")
    sys.path[:0] = [str(root / "src/onec-odata-mcp"), str(root / "src/mcp_http_runtime")]
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", required=True, help="trusted existing wheel site-packages directory; no .pth processing")
    parser.add_argument("--bank-graph-binding", required=True)
    parser.add_argument("--bank-graph-revision", required=True)
    parser.add_argument("--bank-graph-binding-sha256", required=True)
    args, server_args = parser.parse_known_args()
    try:
        admit_package(args.bank_graph_binding, args.bank_graph_revision, args.bank_graph_binding_sha256, args.dependencies)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        parser.error(str(exc) if isinstance(exc, PackageAdmissionError) else "invalid or unavailable bank graph package; startup stopped")
    try:
        from onec_odata_mcp.server import main as server_main
    except ImportError:
        parser.error("trusted connector dependencies are unavailable; startup stopped")
    sys.argv = ["onec_odata_mcp.server", "--bank-graph-binding", args.bank_graph_binding,
                "--bank-graph-revision", args.bank_graph_revision,
                "--bank-graph-binding-sha256", args.bank_graph_binding_sha256, *server_args]
    server_main()


if __name__ == "__main__":
    main()
