"""Normalized internal representation + canonical content hashing.

Every Sonto and Todoist object is projected into a `NormalizedEntity` whose `fields` dict
contains ONLY the mapped, round-trippable values (title, notes, important, schedule,
container refs, sorted tags, completion). The canonical hash over that dict is the engine's
notion of "has this changed?" — deliberately excluding side-specific noise (internal ids,
sort order, server timestamps) so an echo of our own write is a no-op rather than a
ping-pong trigger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class EntityType:
    AREA = "area"
    PROJECT = "project"
    GROUP = "group"
    TASK = "task"
    TAG = "tag"
    INBOX = "inbox"
    ALL = ("area", "project", "group", "task", "tag", "inbox")


class Side:
    SONTO = "sonto"
    TODOIST = "todoist"


@dataclass
class NormalizedEntity:
    entity_type: str
    side: str                       # Side.SONTO | Side.TODOIST
    source_id: str | None           # native id on `side` (None for not-yet-created)
    fields: dict[str, Any]          # canonical, mapped fields only
    updated_at: str | None = None   # modified timestamp on `side`, if available (RFC3339)
    parent_ref: dict[str, str] = field(default_factory=dict)  # e.g. {"project": "<id>"}
    raw: dict[str, Any] = field(default_factory=dict)         # untouched source payload

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.fields)


def _normalize_value(v: Any) -> Any:
    """Make values stable for hashing: sort lists of scalars, trim strings."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        norm = [_normalize_value(x) for x in v]
        # sort lists of scalars (e.g. tags/labels) for order-independence
        if all(isinstance(x, (str, int, float, bool)) for x in norm):
            return sorted(norm, key=lambda x: str(x))
        return norm
    if isinstance(v, dict):
        return {k: _normalize_value(v[k]) for k in sorted(v)}
    return v


def canonical_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop None/empty values and normalize, so absent vs empty don't differ."""
    out: dict[str, Any] = {}
    for k in sorted(fields):
        v = _normalize_value(fields[k])
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out


def canonical_hash(fields: dict[str, Any]) -> str:
    canonical = canonical_fields(fields)
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
