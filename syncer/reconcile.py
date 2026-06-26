"""The reconcile engine.

P1 implements the one-way Sonto -> Todoist **structure** mirror (areas -> top projects,
projects -> sub-projects, groups -> sections) with a name-based adopt/match pass and a dry-run
planner. Applying is gated behind the bootstrap phase: nothing is written in `readonly`/dry-run.
Tasks (P2), the reverse direction + conflicts (P3), and the hard mappings (P4) build on this.
"""

from __future__ import annotations

import datetime as _dt
import logging

from . import adopt, config
from . import sonto as sonto_mod
from . import todoist_client
from .model import EntityType
from . import store as store_mod

log = logging.getLogger(__name__)

_APPLY_PHASES = (config.PHASE_ONEWAY_S2T, config.PHASE_ONEWAY_S2T_DELETES, config.PHASE_TWOWAY)


def run(store: store_mod.Store, *, dry_run: bool = True) -> dict:
    phase = store.bootstrap_phase()
    log.info("reconcile: phase=%s dry_run=%s", phase, dry_run)

    sonto = sonto_mod.Sonto()
    todoist = todoist_client.TodoistClient()

    struct = sonto.snapshot_structure()
    td = todoist.read_changes(sync_token="*")
    plan = adopt.match_structure(struct, td.get("projects", []), td.get("sections", []))

    _report(struct, plan)

    will_apply = (not dry_run) and phase in _APPLY_PHASES
    if not will_apply:
        log.info("dry-run/readonly (phase=%s): no changes applied.", phase)
        return {"phase": phase, "dry_run": dry_run, "matched": len(plan.matched),
                "creates": len(plan.creates), "applied": 0}

    applied = _apply_structure(store, todoist, plan)
    return {"phase": phase, "dry_run": dry_run, "matched": len(plan.matched),
            "creates": len(plan.creates), "applied": applied}


def _report(struct: dict, plan: adopt.Plan) -> None:
    counts: dict[str, int] = {}
    for et, *_ in plan.matched:
        counts[et] = counts.get(et, 0) + 1
    log.info("Sonto structure: %d areas, %d projects, %d groups",
             len(struct["areas"]), len(struct["projects"]), len(struct["groups"]))
    log.info("Matched existing Todoist objects: %s", counts or "{}")
    log.info("Planned creates in Todoist: %d", len(plan.creates))
    for c in plan.creates:
        if c["entity_type"] == EntityType.GROUP:
            where = f"section in '{c['parent_name'] or '?'}'"
        elif c["parent"]:
            where = f"sub-project under '{c['parent_name'] or '?'}'"
        else:
            where = "top-level project"
        log.info("  CREATE %-7s %r -> %s", c["entity_type"], c["name"], where)


def _apply_structure(store: store_mod.Store, todoist: todoist_client.TodoistClient,
                     plan: adopt.Plan) -> int:
    """Create the missing structure in one batched /sync call, then seed the id_map."""
    commands = []
    for c in plan.creates:
        if c["entity_type"] == EntityType.GROUP:
            cmd = todoist.command("section_add",
                                  {"name": c["name"], "project_id": c["parent"]},
                                  temp_id=c["temp_id"])
        else:  # area or area-less/sub project -> Todoist project
            args = {"name": c["name"]}
            if c["parent"]:
                args["parent_id"] = c["parent"]
            cmd = todoist.command("project_add", args, temp_id=c["temp_id"])
        commands.append(cmd)

    if len(commands) > config.TODOIST_MAX_COMMANDS_PER_BATCH:
        # Parents must share a batch with children; topo-chunking is a P4 refinement.
        raise NotImplementedError(
            f"{len(commands)} creates exceeds one batch; topo-chunking not yet implemented")

    temp_map: dict[str, str] = {}
    new_token = None
    if commands:
        resp = todoist.apply_commands(commands)
        temp_map = resp.get("temp_id_mapping", {})
        new_token = resp.get("sync_token")
        _check_status(resp)

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    with store.transaction():
        for et, sonto_id, todoist_id, h in plan.matched:
            store.upsert_map(entity_type=et, sonto_id=sonto_id, todoist_id=todoist_id,
                             last_synced_hash=h, sonto_hash=h, todoist_hash=h,
                             last_synced_at=now, deleted=0)
        for c in plan.creates:
            real = temp_map.get(c["temp_id"])
            if not real:
                log.warning("no temp_id mapping for %s %r — skipping map seed",
                            c["entity_type"], c["name"])
                continue
            store.upsert_map(entity_type=c["entity_type"], sonto_id=c["sonto_id"],
                             todoist_id=real, last_synced_hash=c["hash"],
                             sonto_hash=c["hash"], todoist_hash=c["hash"],
                             last_synced_at=now, deleted=0)
        if new_token:
            store.set_state("todoist_sync_token", new_token)

    log.info("applied: %d creates, %d matches seeded into id_map",
             len(plan.creates), len(plan.matched))
    return len(plan.creates)


def _check_status(resp: dict) -> None:
    status = resp.get("sync_status", {})
    errors = {u: s for u, s in status.items() if s != "ok" and not (isinstance(s, dict) and s.get("error_code") is None)}
    if errors:
        raise todoist_client.TodoistError(f"Todoist command errors: {errors}")
