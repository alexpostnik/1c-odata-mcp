"""Bounded the reviewed scenario person mapping; no network, checkpoint edits or write grant."""
from datetime import datetime
import re
from typing import Any, Mapping, Sequence

PERSON_ENTITY = "Catalog_ФизическиеЛица"
NAME_FIELDS = ("Description", "ФИО")
PERSON_FIELDS = {*NAME_FIELDS, "ДатаРождения", "ДокументЛичныйНомер", "СтраховойНомерПФР"}
# Observed cutoff in the reviewed scenario, NOT a CSDL facet or a proven configuration limit.
OBSERVED_NAME_CEILING = 50
_NAIVE_SECONDS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.0{1,7})?\Z")


def compact_name(parts: Sequence[str]) -> str:
    """Remove only U+0020 padding from the THREE source fields, preserving words."""
    if not isinstance(parts, (list, tuple)) or len(parts) != 3:
        raise ValueError("person name needs exactly three source parts")
    clean = []
    for part in parts:
        if not isinstance(part, str) or not part.rstrip(" "):
            raise ValueError("person name part is missing")
        if any(char.isspace() and char != " " for char in part) or any(ord(c) < 32 for c in part):
            raise ValueError("unsupported whitespace in person name part")
        clean.append(part.rstrip(" "))
    name = " ".join(clean)
    preflight_person_names(PERSON_ENTITY, {field: name for field in NAME_FIELDS})
    return name


def preflight_person_names(entity_set: str, payload: Mapping[str, Any]) -> None:
    """Conservative guard before helper dispatch; never trim an existing payload."""
    if entity_set != PERSON_ENTITY:
        return
    for field in NAME_FIELDS:
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not value
                                 or len(value) > OBSERVED_NAME_CEILING):
            raise ValueError(f"{field} exceeds conservative the reviewed scenario person-name ceiling or is invalid")


def canonical_birthday(value: Any) -> str:
    """Only valid naive second timestamps with absent/ALL ZERO 1..7 fraction."""
    if not isinstance(value, str) or not _NAIVE_SECONDS.fullmatch(value):
        raise ValueError("birthday is not a supported zero-fraction naive timestamp")
    seconds = value.split(".", 1)[0]
    try:
        datetime.fromisoformat(seconds)
    except ValueError:
        raise ValueError("birthday timestamp is invalid") from None
    return seconds


def mapped_person_payload(original: Mapping[str, Any], parts: Sequence[str]) -> dict:
    if set(original) != PERSON_FIELDS:
        raise ValueError("the reviewed scenario mapping requires exact five-field person payload")
    # Prove these are the very source parts used by the ORIGINAL request; no reparse.
    padded = " ".join(parts)
    if any(original[field] != padded for field in NAME_FIELDS):
        raise ValueError("source parts do not bind original person name")
    name = compact_name(parts)
    return {**original, **{field: name for field in NAME_FIELDS},
            "ДатаРождения": canonical_birthday(original["ДатаРождения"])}
