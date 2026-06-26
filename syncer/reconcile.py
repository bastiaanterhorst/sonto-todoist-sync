"""The reconcile engine.

Built out across P1-P4. The skeleton documents the loop so the wiring is clear; the apply
steps are gated behind the bootstrap phase (`store.bootstrap_phase()`) and `dry_run`.

Loop (see docs/PLAN.md for detail):
  lock -> ensure Sonto token -> pull Todoist (/sync token + completed) -> pull Sonto snapshot
  -> normalize+hash -> classify create/update/delete (empty-read sanity floor) -> resolve
  (strict LWW, delete-wins) -> topo-order -> apply -> write-back ids + last_synced_hash +
  sync_token in ONE transaction -> release lock.
"""

from __future__ import annotations

import logging

from . import store as store_mod

log = logging.getLogger(__name__)


def run(store: store_mod.Store, *, dry_run: bool = True) -> dict:
    """Run one reconcile pass. Currently a P0 placeholder: it reports the phase and does not
    yet apply changes. Wiring lands in P1+."""
    phase = store.bootstrap_phase()
    log.info("reconcile: phase=%s dry_run=%s", phase, dry_run)
    log.warning("reconcile engine not yet implemented beyond P0 — no changes made.")
    return {"phase": phase, "dry_run": dry_run, "applied": 0, "status": "not_implemented"}
