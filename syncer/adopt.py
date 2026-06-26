"""First-run adopt/match pass.

Before any create, match pre-existing entities across the two systems (by name within parent;
tasks by content+container with a fuzzy fallback) and seed `id_map` with
`last_synced_hash = current projection`, so the first real sync propagates only genuine diffs
rather than recreating everything. Low-confidence matches are logged for human review.

Implemented in P1 alongside the one-way structure mirror.
"""

from __future__ import annotations


def adopt(store, sonto_entities, todoist_entities):  # pragma: no cover - P1
    raise NotImplementedError("Adopt/match lands in P1; see docs/PLAN.md")
