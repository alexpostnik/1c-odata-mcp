"""Private runtime configuration. The repository contains placeholders only."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


_SSH_HOST_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}")
_REQUEST_TIMEOUT_MAX = 240


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _private_path(value: Any, name: str, repository: Path) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} path required")
    path = Path(value).expanduser().resolve()
    if path == repository or repository in path.parents:
        raise ConfigError(f"{name} must be outside the Git checkout")
    return str(path)


def load_config(path: str | None = None) -> dict:
    selected = path or os.environ.get("ONEC_BRIDGE_CONFIG")
    if not selected:
        raise ConfigError("ONEC_BRIDGE_CONFIG is required")
    repository = Path(__file__).resolve().parents[3]
    try:
        file = Path(selected).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ConfigError("cannot resolve runtime config") from exc
    if file == repository or repository in file.parents:
        raise ConfigError("runtime config must be outside the Git checkout")
    try:
        config = _mapping(json.loads(file.read_text(encoding="utf-8")), "config")
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("cannot read runtime config") from exc
    if config.get("schemaVersion") != "1.0":
        raise ConfigError("unsupported config schemaVersion")
    sources = _mapping(config.get("sources", {}), "sources")
    targets = _mapping(config.get("targets", {}), "targets")
    if not sources and not targets:
        raise ConfigError("at least one source or target is required")
    for alias, source in sources.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", alias):
            raise ConfigError("invalid source alias")
        source = _mapping(source, f"source {alias}")
        if source.get("kind") != "ssh-windows-json":
            raise ConfigError("unsupported source kind")
        for key in ("host", "user", "remoteCommand"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ConfigError(f"source {alias} missing {key}")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source["host"])
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", source["user"])):
            raise ConfigError("invalid SSH host or user")
        host_key_alias = source.get("hostKeyAlias")
        if host_key_alias is not None:
            if not isinstance(host_key_alias, str) or not _SSH_HOST_TOKEN.fullmatch(host_key_alias):
                raise ConfigError(f"source {alias} hostKeyAlias must be an ASCII host token")
        if any(c in source["remoteCommand"] for c in "\r\n;&|`$<>"):
            raise ConfigError("remoteCommand must be a fixed command without shell operators")
        request_timeout = source.get("requestTimeoutSeconds")
        if request_timeout is not None and (type(request_timeout) is not int
                                            or not 1 <= request_timeout <= _REQUEST_TIMEOUT_MAX):
            raise ConfigError(f"source {alias} requestTimeoutSeconds must be 1..{_REQUEST_TIMEOUT_MAX}")
        identity_file = source.get("identityFile")
        if identity_file is not None:
            if not isinstance(identity_file, str) or not identity_file.strip():
                raise ConfigError(f"source {alias} identityFile must be a nonempty string")
            identity_path = Path(identity_file).expanduser()
            if not identity_path.is_absolute():
                raise ConfigError(f"source {alias} identityFile must be absolute")
            try:
                identity_path = identity_path.resolve(strict=True)
            except OSError as exc:
                raise ConfigError(f"source {alias} identityFile cannot be resolved") from exc
            if identity_path == repository or repository in identity_path.parents:
                raise ConfigError("source identityFile must be outside the Git checkout")
            if not identity_path.is_file():
                raise ConfigError(f"source {alias} identityFile must be a file")
            source["identityFile"] = str(identity_path)
    for alias, target in targets.items():
        if not alias.isidentifier():
            raise ConfigError("invalid target alias")
        target = _mapping(target, f"target {alias}")
        url = urlsplit(str(target.get("serviceUrl", "")))
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ConfigError("OData serviceUrl must be an HTTPS base URL")
        if target.get("environment") not in ("prod", "test"):
            raise ConfigError("target environment must be prod or test")
        if not target.get("keychainService") or not target.get("username"):
            raise ConfigError("target needs keychainService and username")
    ledger = _mapping(config.get("ledger"), "ledger")
    _private_path(ledger.get("path"), "ledger", repository)
    scan_ledger = config.get("scanLedger")
    if scan_ledger is not None:
        scan_ledger = _mapping(scan_ledger, "scanLedger")
        _private_path(scan_ledger.get("path"), "scanLedger", repository)
    return config
