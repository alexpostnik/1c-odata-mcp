"""Install and operate the per-user macOS LaunchAgents for local 1C MCP servers."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import anyio
import tomlkit

from .headers import token_file_for_server
from .diagnostics import emit_event
from .transport import read_private_bearer_token

_SERVERS = {
    "onec83-odata": {
        "label": "org.onec.community.1c83-odata-mcp",
        "port": 8765,
        "helper": "onec-mcp-http-auth-odata",
        "source_package": "src/onec-odata-mcp",
    },
    "onecLive": {
        "label": "org.onec.community.1c-live-77-mcp",
        "port": 8766,
        "helper": "onec-mcp-http-auth-live77",
        "source_package": "src/agent_bridge",
    },
}
_CONFIG_KEYS_TO_REMOVE = ("command", "args", "cwd", "env", "env_vars", "experimental_environment")


class InstallError(RuntimeError):
    pass


def support_root(home: Path | None = None) -> Path:
    selected = home or Path.home()
    return selected / "Library" / "Application Support" / "onec-mcp-runtime"


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def ensure_token(server_id: str, *, home: Path | None = None) -> Path:
    path = token_file_for_server(server_id, home=home)
    _private_directory(support_root(home))
    _private_directory(path.parent)
    if not path.exists():
        token = secrets.token_urlsafe(48)
        _write_private(path, (token + "\n").encode("ascii"))
    read_private_bearer_token(path)
    return path


def _service_descriptor_path(server_id: str, *, home: Path | None = None) -> Path:
    return support_root(home) / "services" / f"{server_id}.json"


def _load_config(path: Path):
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError) as exc:
        raise InstallError("cannot read Codex MCP configuration") from exc


def _resolve_command(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InstallError("configured MCP command is missing")
    if os.path.isabs(value):
        resolved = Path(value)
    else:
        found = shutil.which(value)
        if not found:
            raise InstallError("configured MCP interpreter was not found")
        resolved = Path(found)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallError("configured MCP command must be an executable Python interpreter")
    return str(resolved)


def _toolkit_root(selected: Path) -> Path:
    """Resolve either a public repository checkout or its Python toolkit root."""
    selected = selected.expanduser().resolve()
    for candidate in (selected / "contrib" / "python", selected):
        manifest = candidate / "pyproject.toml"
        try:
            project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})
        except (OSError, ValueError):
            continue
        if project.get("name") == "onec-community-toolkit":
            return candidate
    raise InstallError("selected checkout does not contain the unified Python toolkit")


def _extract_stdio_descriptor(doc, server_id: str, repo_root: Path) -> dict[str, Any]:
    try:
        table = doc["mcp_servers"][server_id]
        command = _resolve_command(table["command"])
        args = list(table.get("args", []))
        env = dict(table.get("env", {}))
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError(f"Codex MCP configuration for {server_id} must use stdio") from exc
    if not args or any(not isinstance(item, str) for item in args):
        raise InstallError(f"Codex MCP arguments for {server_id} are invalid")
    expected_module = "onec_odata_mcp.server" if server_id == "onec83-odata" else "agent_bridge"
    if args[:2] != ["-m", expected_module]:
        raise InstallError(f"Codex MCP command for {server_id} is not the supported server module")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
        raise InstallError(f"Codex MCP environment for {server_id} must contain strings only")
    working_directory = table.get("cwd")
    if working_directory is not None and not isinstance(working_directory, str):
        raise InstallError(f"Codex MCP working directory for {server_id} is invalid")
    source_root = _toolkit_root(repo_root) / _SERVERS[server_id]["source_package"]
    if server_id == "onecLive":
        previous_pythonpath = env.get("PYTHONPATH")
        paths = [part for part in (previous_pythonpath or "").split(os.pathsep) if part]
        resolved_source = source_root.resolve()
        paths = [
            str(source_root),
            *(
                part
                for part in paths
                if Path(part).expanduser().resolve() != resolved_source
            ),
        ]
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return {
        "server_id": server_id,
        "command": command,
        "args": args,
        "environment": env,
        "working_directory": str((repo_root if not working_directory else Path(working_directory)).resolve()),
        "repo_root": str(repo_root),
    }


def _verify_main_checkout(repo: Path) -> None:
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    if branch.returncode or branch.stdout.strip() != "main":
        raise InstallError("the installed source checkout must be on branch main")
    state = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if state.returncode or state.stdout.strip():
        raise InstallError("the installed source checkout must be clean before installation or update")


def _install_packages(descriptor: dict[str, Any]) -> None:
    python = descriptor["command"]
    toolkit = _toolkit_root(Path(descriptor["repo_root"]))
    local_packages = [toolkit]
    for package in local_packages:
        result = subprocess.run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-e", str(package)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise InstallError(f"package installation failed for {descriptor['server_id']}")


def _server_program_arguments(descriptor: dict[str, Any], token_path: Path) -> list[str]:
    settings = _SERVERS[descriptor["server_id"]]
    args = list(descriptor["args"])
    return [
        descriptor["command"],
        *args,
        "--transport", "streamable-http",
        "--host", "127.0.0.1",
        "--port", str(settings["port"]),
        "--bearer-token-file", str(token_path),
    ]


def build_launch_agent_plist(
    descriptor: dict[str, Any],
    token_path: Path,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """Create a launchd property list without embedding the bearer token."""
    server_id = descriptor["server_id"]
    settings = _SERVERS[server_id]
    root = home or Path.home()
    log_root = root / "Library" / "Logs" / "onec-mcp-runtime"
    environment = dict(descriptor["environment"])
    environment.pop("ONEC_MCP_BEARER_TOKEN", None)
    environment["PYTHONUNBUFFERED"] = "1"
    return {
        "Label": settings["label"],
        "ProgramArguments": _server_program_arguments(descriptor, token_path),
        "WorkingDirectory": descriptor["working_directory"],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(log_root / f"{server_id}.stdout.log"),
        "StandardErrorPath": str(log_root / f"{server_id}.stderr.log"),
    }


def _write_launch_agent(descriptor: dict[str, Any], token_path: Path, *, home: Path | None = None) -> Path:
    root = home or Path.home()
    path = root / "Library" / "LaunchAgents" / f"{_SERVERS[descriptor['server_id']]['label']}.plist"
    _private_directory(root / "Library" / "Logs" / "onec-mcp-runtime")
    data = plistlib.dumps(build_launch_agent_plist(descriptor, token_path, home=root), fmt=plistlib.FMT_XML)
    _write_private(path, data)
    _private_directory(support_root(root) / "services")
    _write_private(
        _service_descriptor_path(descriptor["server_id"], home=root),
        json.dumps(descriptor, sort_keys=True).encode("utf-8"),
    )
    return path


def _read_descriptor(server_id: str, *, home: Path | None = None) -> dict[str, Any]:
    path = _service_descriptor_path(server_id, home=home)
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallError(f"private runtime descriptor for {server_id} must have mode 0600")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"runtime descriptor for {server_id} is unavailable") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if (
        not isinstance(value, dict)
        or value.get("server_id") != server_id
        or not isinstance(value.get("environment"), dict)
    ):
        raise InstallError(f"runtime descriptor for {server_id} is invalid")
    return value


def _launchctl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise InstallError("launchctl operation failed; inspect the local service logs")
    return result


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _probe_failure(exc: Exception, httpx_module) -> tuple[str, int | None]:
    """Classify wrapped client failures without serializing exception details."""
    pending = [exc]
    seen: set[int] = set()
    outcome = "probe_failed"
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, httpx_module.HTTPStatusError):
            status = error.response.status_code
            if status == 401:
                return "auth_rejected", status
            if type(status) is int and 400 <= status <= 599:
                return "response_rejected", status
        if isinstance(error, httpx_module.TimeoutException):
            outcome = "timed_out"
        elif isinstance(error, httpx_module.ConnectError) and outcome != "timed_out":
            outcome = "connection_failed"
        pending.extend(getattr(error, "exceptions", ()))
        for cause in (error.__cause__, error.__context__):
            if cause is not None:
                pending.append(cause)
    return outcome, None


async def _probe(url: str, token: str) -> int:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    phase = "probe_connect"
    response_status: int | None = None

    async def observe_response(response):
        # The SDK can translate HTTP errors into MCP errors without retaining
        # their HTTPStatusError cause. Observe only status, never body/headers.
        nonlocal response_status
        response_status = response.status_code

    started = time.monotonic()
    emit_event(phase, "started")
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"response": [observe_response]},
        ) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    phase = "probe_initialize"
                    emit_event(phase, "started")
                    await session.initialize()
                    emit_event(phase, "completed", started=started)
                    phase = "probe_tools"
                    response_status = None
                    emit_event(phase, "started")
                    result = await session.list_tools()
                    emit_event(phase, "completed", started=started)
                    return len(result.tools)
    except Exception as exc:
        outcome, status = _probe_failure(exc, httpx2)
        if response_status == 401:
            outcome, status = "auth_rejected", response_status
        elif response_status is not None and 400 <= response_status <= 599:
            outcome, status = "response_rejected", response_status
        emit_event(phase, outcome, started=started, status=status)
        raise


def _wait_for_service(server_id: str, token_path: Path, timeout: float = 15) -> int:
    port = _SERVERS[server_id]["port"]
    url = f"http://127.0.0.1:{port}/mcp"
    token = read_private_bearer_token(token_path)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            count = anyio.run(_probe, url, token)
            if count < 1:
                raise InstallError(f"MCP server {server_id} advertised no tools")
            return count
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise InstallError(f"MCP server {server_id} did not become ready") from last_error


def _bootstrap(descriptor: dict[str, Any], plist_path: Path) -> None:
    label = _SERVERS[descriptor["server_id"]]["label"]
    _launchctl(["bootout", _gui_domain(), str(plist_path)], check=False)
    _launchctl(["bootstrap", _gui_domain(), str(plist_path)])
    _launchctl(["kickstart", "-k", f"{_gui_domain()}/{label}"])


def _switch_codex_config(config_path: Path, *, home: Path | None = None) -> None:
    root = home or Path.home()
    doc = _load_config(config_path)
    servers = doc.get("mcp_servers")
    if servers is None:
        raise InstallError("Codex has no MCP server configuration")
    for server_id, settings in _SERVERS.items():
        table = servers.get(server_id)
        if table is None:
            raise InstallError(f"Codex MCP server {server_id} disappeared during installation")
        descriptor = _read_descriptor(server_id, home=root)
        helper = Path(descriptor["command"]).parent / settings["helper"]
        for key in _CONFIG_KEYS_TO_REMOVE:
            table.pop(key, None)
        table["url"] = f"http://127.0.0.1:{settings['port']}/mcp"
        table["http_headers_helper"] = str(helper)
        # Global registrations also load for chats outside the 1C domain.
        # Preserve an explicit operator decision, including required=true.
        table.setdefault("required", False)
    backup = support_root(root) / "backups" / "config.toml.before-streamable-http"
    if not backup.exists():
        _private_directory(support_root(root))
        _private_directory(backup.parent)
        _write_private(backup, config_path.read_bytes())
    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    temp = config_path.with_name(f".{config_path.name}.mcp-runtime.tmp")
    _write_private(temp, tomlkit.dumps(doc).encode("utf-8"))
    temp.chmod(original_mode)
    os.replace(temp, config_path)


def install(config_path: Path, repo_root: Path, *, home: Path | None = None) -> None:
    if sys.platform != "darwin":
        raise InstallError("LaunchAgent installation is supported only on macOS")
    root = home or Path.home()
    repo = repo_root.expanduser().resolve(strict=True)
    _verify_main_checkout(repo)
    doc = _load_config(config_path)
    created: list[tuple[dict[str, Any], Path]] = []
    try:
        for server_id in _SERVERS:
            descriptor = _extract_stdio_descriptor(doc, server_id, repo)
            _install_packages(descriptor)
            token_path = ensure_token(server_id, home=root)
            plist_path = _write_launch_agent(descriptor, token_path, home=root)
            created.append((descriptor, plist_path))
            _bootstrap(descriptor, plist_path)
        for descriptor, _ in created:
            _wait_for_service(descriptor["server_id"], ensure_token(descriptor["server_id"], home=root))
        _switch_codex_config(config_path, home=root)
    except Exception:
        for descriptor, plist_path in created:
            _launchctl(
                ["bootout", _gui_domain(), str(plist_path)],
                check=False,
            )
        raise


def update(*, pull: bool = False, home: Path | None = None) -> None:
    if sys.platform != "darwin":
        raise InstallError("LaunchAgent updates are supported only on macOS")
    root = home or Path.home()
    descriptors = [_read_descriptor(server_id, home=root) for server_id in _SERVERS]
    repo = Path(descriptors[0]["repo_root"])
    if any(Path(item["repo_root"]) != repo for item in descriptors):
        raise InstallError("the local MCP services reference different source checkouts")
    _verify_main_checkout(repo)
    if pull:
        result = subprocess.run(
            ["git", "-C", str(repo), "pull", "--ff-only", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise InstallError("source checkout update failed; inspect its local Git state")
    for descriptor in descriptors:
        _install_packages(descriptor)
        token_path = ensure_token(descriptor["server_id"], home=root)
        plist_path = _write_launch_agent(descriptor, token_path, home=root)
        _bootstrap(descriptor, plist_path)
    for descriptor in descriptors:
        _wait_for_service(
            descriptor["server_id"],
            ensure_token(descriptor["server_id"], home=root),
        )


def status(*, home: Path | None = None) -> dict[str, int]:
    root = home or Path.home()
    result: dict[str, int] = {}
    for server_id in _SERVERS:
        descriptor = _read_descriptor(server_id, home=root)
        plist_path = root / "Library" / "LaunchAgents" / f"{_SERVERS[server_id]['label']}.plist"
        if not plist_path.exists():
            raise InstallError(f"LaunchAgent plist for {server_id} is missing")
        _launchctl(["print", f"{_gui_domain()}/{_SERVERS[server_id]['label']}"])
        result[server_id] = _wait_for_service(
            server_id,
            token_file_for_server(server_id, home=root),
            timeout=3,
        )
    return result


def rollback(config_path: Path, *, home: Path | None = None) -> None:
    root = home or Path.home()
    backup = support_root(root) / "backups" / "config.toml.before-streamable-http"
    if not backup.is_file():
        raise InstallError("Codex configuration backup is unavailable")
    for server_id, settings in _SERVERS.items():
        plist_path = root / "Library" / "LaunchAgents" / f"{settings['label']}.plist"
        _launchctl(["bootout", _gui_domain(), str(plist_path)], check=False)
    _write_private(config_path, backup.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local 1C MCP LaunchAgents on macOS.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--config", type=Path, default=Path.home() / ".codex" / "config.toml")
    install_parser.add_argument("--repo-root", type=Path, required=True)
    update_parser = subparsers.add_parser("update")
    update_parser.add_argument("--pull", action="store_true")
    subparsers.add_parser("status")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--config", type=Path, default=Path.home() / ".codex" / "config.toml")
    args = parser.parse_args()
    try:
        if args.action == "install":
            install(args.config, args.repo_root)
            print("Local MCP LaunchAgents are running; Codex configuration now uses localhost Streamable HTTP.")
        elif args.action == "update":
            update(pull=args.pull)
            print("Local MCP services updated and restarted without restarting Codex.")
        elif args.action == "status":
            counts = status()
            print("Both local MCP services are reachable; advertised tool counts: "
                  + ", ".join(f"{name}={count}" for name, count in counts.items()))
        elif args.action == "rollback":
            rollback(args.config)
            print("LaunchAgents stopped and the previous Codex MCP configuration restored.")
    except (InstallError, ValueError) as exc:
        parser.exit(1, f"mcp-runtime: {exc}\n")


if __name__ == "__main__":
    main()
