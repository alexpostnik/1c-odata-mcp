"""Runtime configuration for the 1C 8.3 OData MCP connector."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """Raised when runtime configuration is incomplete or unsafe."""


_TARGET_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PROD_WRITE_PROBE_ID_RE = re.compile(r"^[a-f0-9]{16}$")


_PASSWORD_ENV_KEYS = (
    "ONEC83_ODATA_PASSWORD",
    "ONEC83_PASSWORD",
    "ONEC83_BROWSER_PASSWORD",
)


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required")
    return value.strip()


def _secret_free(env: Mapping[str, str]) -> None:
    for key in _PASSWORD_ENV_KEYS:
        if env.get(key):
            raise ConfigError(f"{key} must not be set; use macOS Keychain")


def ensure_secret_free(env: Mapping[str, str] | None = None) -> None:
    """Reject password environment variables for both legacy and target configs."""
    _secret_free(os.environ if env is None else env)


def _bool(value: str | None, key: str) -> bool:
    if value is None or value.strip() == "":
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean")


def _safe_text(value: str, key: str, *, max_len: int = 256) -> str:
    if not value or len(value) > max_len or any(ord(char) < 32 for char in value):
        raise ConfigError(f"{key} has unsafe characters")
    return value


def _https_base_url(value: str, key: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{key} must be an HTTPS base URL without credentials or query")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    if not path.endswith("/odata/standard.odata/"):
        raise ConfigError(f"{key} must end in /odata/standard.odata/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass(frozen=True)
class RuntimeConfig:
    """Public runtime settings consumed by the OData client and MCP server."""

    odata_base_url: str
    odata_user: str
    keychain_service: str
    write_base_url: str | None = None
    enable_write: bool = False
    target_kind: str | None = None
    timeout_seconds: float = 20.0
    max_top: int = 200
    get_retries: int = 2
    multi_target: bool = False
    prod_write_probe_enabled: bool = False
    prod_write_probe_fingerprint: str | None = None
    prod_write_probe_id: str | None = None

    @classmethod
    def from_target_mapping(cls, target: Mapping[str, object]) -> "RuntimeConfig":
        """Build one target from the structured multi-database configuration."""
        required = ("odata_base_url", "odata_user", "keychain_service", "target_kind")
        missing = [
            key for key in required
            if not isinstance(target.get(key), str) or not str(target[key]).strip()
        ]
        if missing:
            raise ConfigError("target is missing required fields: " + ", ".join(missing))
        base_url = _https_base_url(str(target["odata_base_url"]), "odata_base_url")
        user = _safe_text(str(target["odata_user"]).strip(), "odata_user")
        service = _safe_text(str(target["keychain_service"]).strip(), "keychain_service")
        target_kind = _safe_text(str(target["target_kind"]).strip(), "target_kind", max_len=32)
        if target_kind not in {"test", "prod"}:
            raise ConfigError("target_kind must be test or prod")
        enable_write = _target_bool(target.get("enable_write", False), "enable_write")
        raw_write_base = target.get("write_base_url")
        write_base_url = None
        if raw_write_base is not None and str(raw_write_base).strip():
            if not isinstance(raw_write_base, str):
                raise ConfigError("write_base_url must be a string")
            write_base_url = _https_base_url(raw_write_base, "write_base_url")
        if target_kind == "prod" and enable_write:
            raise ConfigError("prod targets must set enable_write=false")
        if enable_write and write_base_url != base_url:
            raise ConfigError("write_base_url must strictly match odata_base_url to enable writes")
        if enable_write and target_kind != "test":
            raise ConfigError("only test targets can enable writes")
        probe_enabled = _target_bool(
            target.get("prod_write_probe_enabled", False),
            "prod_write_probe_enabled",
        )
        probe_fingerprint_value = target.get("prod_write_probe_fingerprint")
        probe_id_value = target.get("prod_write_probe_id")
        if probe_fingerprint_value is not None and not isinstance(probe_fingerprint_value, str):
            raise ConfigError("prod_write_probe_fingerprint must be a string")
        if probe_id_value is not None and not isinstance(probe_id_value, str):
            raise ConfigError("prod_write_probe_id must be a string")
        probe_fingerprint = (
            str(probe_fingerprint_value).strip()
            if isinstance(probe_fingerprint_value, str) and probe_fingerprint_value.strip()
            else None
        )
        probe_id = (
            str(probe_id_value).strip()
            if isinstance(probe_id_value, str) and probe_id_value.strip()
            else None
        )
        if probe_enabled:
            if target_kind != "prod":
                raise ConfigError("prod_write_probe_enabled is allowed only for prod targets")
            if enable_write or write_base_url is not None:
                raise ConfigError("prod write probe cannot enable ordinary writes or set write_base_url")
            expected_fingerprint = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            if probe_fingerprint != expected_fingerprint:
                raise ConfigError(
                    "prod_write_probe_fingerprint must match the configured target fingerprint"
                )
            if not isinstance(probe_id, str) or not _PROD_WRITE_PROBE_ID_RE.fullmatch(probe_id):
                raise ConfigError("prod_write_probe_id must be exactly 16 lowercase hexadecimal characters")
        elif probe_fingerprint is not None or probe_id is not None:
            raise ConfigError(
                "prod_write_probe_fingerprint and prod_write_probe_id require prod_write_probe_enabled=true"
            )
        return cls(
            odata_base_url=base_url,
            odata_user=user,
            keychain_service=service,
            write_base_url=write_base_url,
            enable_write=enable_write,
            target_kind=target_kind,
            multi_target=True,
            prod_write_probe_enabled=probe_enabled,
            prod_write_probe_fingerprint=probe_fingerprint,
            prod_write_probe_id=probe_id,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        source = os.environ if env is None else env
        _secret_free(source)

        base_url = _https_base_url(_required(source, "ONEC83_ODATA_BASE_URL"), "ONEC83_ODATA_BASE_URL")
        user = _safe_text(_required(source, "ONEC83_ODATA_USER"), "ONEC83_ODATA_USER")
        service = _safe_text(_required(source, "ONEC83_KEYCHAIN_SERVICE"), "ONEC83_KEYCHAIN_SERVICE")
        enable_write = _bool(source.get("ONEC83_ENABLE_WRITE"), "ONEC83_ENABLE_WRITE")

        raw_write_base = source.get("ONEC83_WRITE_BASE_URL")
        write_base_url = None
        if raw_write_base and raw_write_base.strip():
            write_base_url = _https_base_url(raw_write_base, "ONEC83_WRITE_BASE_URL")
        target_kind = source.get("ONEC83_TARGET_KIND")
        if target_kind is not None:
            target_kind = _safe_text(target_kind.strip(), "ONEC83_TARGET_KIND", max_len=32)
            if target_kind not in {"test", "prod"}:
                raise ConfigError("ONEC83_TARGET_KIND must be test or prod")
        if enable_write and write_base_url != base_url:
            raise ConfigError("ONEC83_WRITE_BASE_URL must strictly match ONEC83_ODATA_BASE_URL to enable writes")
        if enable_write and target_kind != "test":
            raise ConfigError("ONEC83_TARGET_KIND=test is required to enable writes")

        return cls(
            odata_base_url=base_url,
            odata_user=user,
            keychain_service=service,
            write_base_url=write_base_url,
            enable_write=enable_write,
            target_kind=target_kind,
        )

    @property
    def write_enabled(self) -> bool:
        return self.enable_write and self.write_base_url == self.odata_base_url and self.target_kind == "test"


@dataclass(frozen=True)
class RuntimeTarget:
    """Named OData target in one MCP process."""

    name: str
    config: RuntimeConfig


def targets_from_json(raw: str) -> tuple[RuntimeTarget, ...]:
    """Parse and validate ONEC83_TARGETS_JSON without exposing endpoint details."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise ConfigError("ONEC83_TARGETS_JSON must be valid JSON") from None
    if not isinstance(parsed, dict) or not parsed:
        raise ConfigError("ONEC83_TARGETS_JSON must be a non-empty object keyed by target name")
    if len(parsed) > 16:
        raise ConfigError("ONEC83_TARGETS_JSON may configure at most 16 targets")
    targets: list[RuntimeTarget] = []
    for name, values in parsed.items():
        if not isinstance(name, str) or not _TARGET_ALIAS_RE.fullmatch(name):
            raise ConfigError("target names must match ^[a-z][a-z0-9_-]{0,31}$")
        if not isinstance(values, dict):
            raise ConfigError(f"target {name!r} must be an object")
        allowed_fields = {
            "odata_base_url", "odata_user", "keychain_service", "target_kind",
            "enable_write", "write_base_url", "prod_write_probe_enabled",
            "prod_write_probe_fingerprint", "prod_write_probe_id",
        }
        unknown_fields = sorted(set(values) - allowed_fields)
        if unknown_fields:
            raise ConfigError(
                f"target {name!r} has unsupported fields: {', '.join(unknown_fields)}"
            )
        config = RuntimeConfig.from_target_mapping(values)
        if config.prod_write_probe_enabled and name != "prod":
            raise ConfigError("PROD write probe requires the target alias 'prod'")
        if name == "prod" and config.target_kind != "prod":
            raise ConfigError("target named prod must set target_kind=prod")
        if name == "test" and config.target_kind != "test":
            raise ConfigError("target named test must set target_kind=test")
        targets.append(RuntimeTarget(name=name, config=config))
    return tuple(targets)


def _target_bool(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _bool(value, key)
    if value is None:
        return False
    raise ConfigError(f"{key} must be a boolean")
