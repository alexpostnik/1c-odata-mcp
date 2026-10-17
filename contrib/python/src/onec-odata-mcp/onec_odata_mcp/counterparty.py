"""Counterparty Create rules and a scoped permit for the actual HTTP POST.

Source identity/official registration remains caller evidence. This module does
not infer legal status or introduce prerequisites for other catalogs.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

from .ledger import WriteLedger, counterparty_pre_post_result, request_digest


COUNTERPARTIES = "Catalog_Контрагенты"
_UNP_PLACEHOLDERS = frozenset({"unknown", "undefined", "null", "none", "n/a", "na", "test", "нет", "неизвестно"})
_POST_PERMIT: ContextVar[tuple[str, str] | None] = ContextVar("counterparty_post_permit", default=None)


def validate_counterparty_create(entity_set: str, payload: Mapping[str, Any]) -> str | None:
    """Return exact UNP for cards, None for other sets/structural folders."""
    if entity_set != COUNTERPARTIES:
        return None
    folder = payload.get("IsFolder", False)
    if type(folder) is not bool:
        raise ValueError("Counterparty IsFolder must be a boolean")
    name = payload.get("Description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Counterparty Description must be a nonblank string")
    if folder:
        return None
    unp = payload.get("ИНН")
    if (not isinstance(unp, str) or not unp.strip()
            or unp != unp.strip() or set(unp) == {"0"}
            or unp.casefold() in _UNP_PLACEHOLDERS
            or not unp.isalnum()):
        raise ValueError("Counterparty ИНН must contain the confirmed nonblank UNP; zero and placeholder values are forbidden")
    return unp


def target_fingerprint(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = parts.hostname
    if not host or parts.username is not None or parts.password is not None:
        raise ValueError("Counterparty target requires an endpoint without user information")
    authority = f"[{host}]" if ":" in host else host
    port = parts.port
    if port is not None and (parts.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        authority += f":{port}"
    normalized = urlunsplit((parts.scheme.lower(), authority, parts.path.rstrip("/") + "/",
                             parts.query, parts.fragment))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require_counterparty_post_permit(entity_set: str, payload: Mapping[str, Any], base_url: str) -> None:
    unp = validate_counterparty_create(entity_set, payload)
    if unp is not None and _POST_PERMIT.get() != (target_fingerprint(base_url), request_digest(payload)):
        raise PermissionError("Counterparty card POST requires the guarded Bridge.create_entity path")


def _prove_absence(search: Callable[..., dict], unp: str, target: str) -> dict:
    try:
        result = search(COUNTERPARTIES, field="ИНН", equals=unp, top=2,
                        select="ИНН", max_scan=100_000, page_size=100)
    except Exception:
        # Search exceptions/receipts can contain identifiers. Keep only a fixed
        # public diagnostic; the existing privacy-safe search telemetry remains.
        raise ValueError("Counterparty UNP uniqueness could not be verified; no POST sent") from None
    rows = result.get("result", {}).get("value") if isinstance(result, dict) and isinstance(result.get("result"), dict) else None
    if isinstance(rows, list) and any(isinstance(row, dict) and row.get("ИНН") == unp for row in rows):
        raise ValueError("Counterparty UNP already exists in this target; no POST sent")
    collection = result.get("result", {}) if isinstance(result, dict) else {}
    if (not isinstance(rows, list) or rows
            or result.get("complete") is not True
            or result.get("resultLimited") is not False
            or result.get("maxScanReached") is not False
            or result.get("scanInterrupted", False) is not False
            or result.get("error") is not None
            or any(collection.get(key) for key in ("next_link", "__next", "@odata.nextLink", "odata.nextLink"))):
        raise ValueError("Counterparty UNP uniqueness is incomplete or unknown; no POST sent")
    strategy = result.get("strategy")
    safe_strategy = strategy if strategy in {
        "server_filter", "linear_scan_after_filter_error", "ordered_scan_after_filter_error",
    } else "unknown"
    # Hash only sanitized facts, not a low-entropy UNP or the raw filter/rows.
    # The ordinary write request hash separately binds this to the exact payload.
    summary = {
        "status": "verified_absent", "targetFingerprint": target,
        "entitySet": COUNTERPARTIES, "field": "ИНН", "operator": "eq",
        "checkedAt": datetime.now(timezone.utc).isoformat(), "phase": "before_post",
        "strategy": safe_strategy, "complete": True, "resultLimited": False,
        "maxScanReached": False, "scanInterrupted": False, "matchCount": 0,
        "includesDeletionMarked": True,
        "snapshotConsistent": result.get("snapshotConsistent") is True,
    }
    return {**summary, "evidenceDigest": request_digest(summary)}


@contextmanager
def guarded_counterparty_create(*, ledger: WriteLedger, target: str, unp: str,
                                operation_id: str, payload: dict,
                                search: Callable[..., dict],
                                coordination_ledger: WriteLedger,
                                claim_ledgers: Callable[[], tuple[WriteLedger, ...]],
                                receipt_target: str,
                                target_bindings: Mapping[str, str]) -> Iterator[Callable[[], dict]]:
    claim = request_digest(target, COUNTERPARTIES, unp)
    claim_targets = {request_digest(raw, COUNTERPARTIES, unp): canonical
                     for raw, canonical in target_bindings.items()}
    aliases = {key for key, canonical in claim_targets.items() if canonical == target}
    aliases.add(claim)
    digest = request_digest(receipt_target, "create", COUNTERPARTIES, None, payload, None)
    # Discover historical aliases while holding the stable base-journal lock.
    # A long-lived Bridge therefore sees journals created by a newer alias too.
    with ExitStack() as locks:
        locks.enter_context(coordination_ledger.counterparty_lock())
        peers = claim_ledgers()
        for peer in peers:
            if peer.path != coordination_ledger.path:
                locks.enter_context(peer.counterparty_lock())
        reserved = []
        token = None
        try:
            for peer in peers:
                peer.recover_counterparty_pre_post(
                    target=target, target_bindings=target_bindings, operation_id=operation_id,
                )
            for peer in peers:
                peer.reserve_counterparty(
                    claim, operation_id, locked_peers=peers, target=target,
                    request_hash=digest, target_bindings=target_bindings,
                    claim_aliases=frozenset(aliases), claim_targets=claim_targets,
                )
                reserved.append(peer)
            def check_before_post() -> dict:
                nonlocal token
                summary = _prove_absence(search, unp, receipt_target)
                token = _POST_PERMIT.set((target, request_digest(payload)))
                return summary
            # Bridge completes reference I/O before invoking this check. Only
            # local receipt transactions remain before the permitted transport.
            yield check_before_post
        finally:
            if token is not None:
                _POST_PERMIT.reset(token)
            receipt = ledger.inspect(operation_id)
            if (len(reserved) == len(peers) and receipt is not None
                    and receipt["state"] == "prepared"
                    and receipt["request_hash"] == digest
                    and receipt["result"] == counterparty_pre_post_result(receipt_target, digest)):
                ledger.fail(operation_id, {
                    **receipt["result"], "writeState": "failed", "phase": "before_post",
                })
                receipt = ledger.inspect(operation_id)
            if receipt is None or receipt["state"] == "failed":
                for peer in reversed(reserved):
                    peer.release_counterparty(claim, operation_id)
