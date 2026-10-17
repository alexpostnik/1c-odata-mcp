"""Export a single committed connector/reader/source package to a private directory.

No installation, configuration, process restart, network or 1C access. Operator
must separately prove that the selected revision is accepted main before use.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


PATHS = (
    "src/onec-odata-mcp/onec_odata_mcp", "src/mcp_http_runtime/onec_mcp_runtime",
    "onec_bank_graph", "tools/bank-graph/launch.py",
)
PREFIX = "contrib/python/"



def export_package(repository, revision, destination, *, archive):
    repository, destination = Path(repository).resolve(), Path(destination).absolute()
    repository = Path(subprocess.check_output(["git", "-C", str(repository), "rev-parse", "--show-toplevel"], text=True).strip()).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("select a full committed revision; acceptance must be proved separately")
    if destination.exists():
        raise ValueError("destination must not exist; never overwrite an installed release")
    actual = subprocess.check_output(["git", "-C", str(repository), "rev-parse", revision + "^{commit}"], text=True).strip()
    if actual != revision:
        raise ValueError("revision must identify the exact commit")
    source_archive = Path(archive).absolute()
    if source_archive.is_symlink() or not source_archive.is_dir() or not (source_archive / "manifest.json").is_file():
        raise ValueError("select an explicit regular private archive with manifest.json")
    data = subprocess.check_output(["git", "-C", str(repository), "archive", "--format=tar", revision, *[PREFIX + p for p in PATHS]])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".bank-graph-package-", dir=destination.parent))
    try:
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                        or "__pycache__" in name.parts or name.suffix in {".pyc", ".pyo", ".pth"}
                        or not (member.isdir() or member.isfile())):
                    raise ValueError("package requires regular committed files without symlinks")
                if member.isdir():
                    continue
                if member.name in files:
                    raise ValueError("duplicate package path")
                body = archive.extractfile(member).read()
                if not member.name.startswith(PREFIX):
                    raise ValueError("unexpected toolkit prefix")
                member.name = member.name[len(PREFIX):]
                path = temporary / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                files[member.name] = hashlib.sha256(body).hexdigest()
        for selected in sorted(source_archive.rglob("*")):
            if selected.is_symlink() or not (selected.is_file() or selected.is_dir()):
                raise ValueError("source archive requires regular files/directories without symlinks")
            if selected.is_dir():
                continue
            relative = "archive/" + selected.relative_to(source_archive).as_posix()
            body = selected.read_bytes()
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            files[relative] = hashlib.sha256(body).hexdigest()
        binding = {"schema_version": 1, "repository": "onec-community-toolkit", "revision": revision, "files": dict(sorted(files.items()))}
        body = json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if len(body.encode()) > 262144:
            raise ValueError("package binding exceeds supported size")
        (temporary / "bank-graph-binding.json").write_text(body, encoding="utf-8")
        temporary.rename(destination)
        return {"revision": revision, "file_count": len(files), "binding_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "launcher_sha256": files["tools/bank-graph/launch.py"],
                "acceptance": "not checked; operator must verify accepted main", "installation": "not performed"}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--archive", required=True, type=Path, help="operator-selected private archive; never bundled by default")
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export_package(args.repository, args.revision, args.destination, archive=args.archive), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
