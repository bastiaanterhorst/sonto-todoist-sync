"""The reconcile engine.

P1 implements the one-way Sonto -> Todoist **structure** mirror (areas -> top projects,
projects -> sub-projects, groups -> sections) with a name-based adopt/match pass and a dry-run
planner. Applying is gated behind the bootstrap phase: nothing is written in `readonly`/dry-run.
Tasks (P2), the reverse direction + conflicts (P3), and the hard mappings (P4) build on this.
"""

from __future__ import annotations

import datetime as _dt
import logging

import datetime as _dt

from . import adopt, config, mapping
from . import sonto as sonto_mod
from . import todoist_client
from .model import EntityType, canonical_hash
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

    # Always record existing Sonto<->Todoist structure matches (internal bookkeeping, not a
    # Todoist write) so the task pass can resolve placement even in dry-run.
    _seed_matched(store, plan)

    # P1 structure creates: ensure the project/section skeleton exists (and is mapped) first,
    # so the task pass can place tasks into real Todoist ids. Apply-only.
    struct_applied = 0
    if will_apply:
        struct_applied = _create_structure(store, todoist, plan)
        _apply_moves(store, todoist, plan)
    elif plan.creates or plan.moves:
        log.info("structure: %d creates, %d moves pending (dry-run/readonly: not applied)",
                 len(plan.creates), len(plan.moves))

    # P2 tasks (one-way Sonto->Todoist, incomplete only).
    tasks_applied = _task_pass(store, sonto, todoist, dry_run=dry_run, will_apply=will_apply)

    return {"phase": phase, "dry_run": dry_run, "matched": len(plan.matched),
            "creates": len(plan.creates), "structure_applied": struct_applied,
            "tasks_applied": tasks_applied}


def _resolve_placement(store: store_mod.Store, task: dict):
    """Resolve a Sonto task's Todoist (project_id, section_id) from the id_map. Container ids in
    the task payload are raw Core-Data tokens -> decode to the stable URI before lookup."""
    td_project = td_section = None
    proj, area, grp = task.get("projectID"), task.get("areaID"), task.get("groupID")
    if proj:
        m = store.map_by_sonto(EntityType.PROJECT, sonto_mod.stable_id(proj))
        td_project = m.todoist_id if m else None
    elif area:
        m = store.map_by_sonto(EntityType.AREA, sonto_mod.stable_id(area))
        td_project = m.todoist_id if m else None
    if grp:
        m = store.map_by_sonto(EntityType.GROUP, sonto_mod.stable_id(grp))
        td_section = m.todoist_id if m else None
    if td_section and not td_project:
        td_section = None  # never place a section without its project
    return td_project, td_section


def _sched_str(task: dict) -> str:
    kind, day_iso, week, year = mapping.task_schedule(task)
    if kind == "day":
        return f" [day {day_iso}]"
    if kind == "week":
        return f" [week {year}-W{week:02d}]"
    return ""


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _task_pass(store, sonto, todoist, *, dry_run, will_apply) -> int:
    tasks = sonto.snapshot_tasks()
    creates = []
    for t in tasks:
        uuid = t.get("todoUUID")
        if not uuid or store.map_by_sonto(EntityType.TASK, uuid):
            continue  # already mapped -> update handled in a later increment
        td_project, td_section = _resolve_placement(store, t)
        canon = mapping.task_canonical(t, td_project, td_section)
        creates.append({"task": t, "uuid": uuid, "project_id": td_project,
                        "section_id": td_section, "hash": canonical_hash(canon)})

    log.info("Tasks: %d incomplete in Sonto; %d new to create in Todoist",
             len(tasks), len(creates))
    for c in creates[:25]:
        where = c["section_id"] or c["project_id"] or "Inbox"
        log.info("  CREATE task %r -> %s%s",
                 (c["task"].get("name") or "")[:50], where, _sched_str(c["task"]))
    if len(creates) > 25:
        log.info("  ... and %d more", len(creates) - 25)

    if not (will_apply and creates):
        if not will_apply:
            log.info("tasks: dry-run/readonly: no changes applied.")
        return 0

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    applied = 0
    for chunk in _chunks(creates, config.TODOIST_MAX_COMMANDS_PER_BATCH):
        cmds = [todoist.command("item_add",
                                mapping.task_item_args(c["task"], c["project_id"], c["section_id"]),
                                temp_id=c["uuid"]) for c in chunk]
        resp = todoist.apply_commands(cmds)
        _check_status(resp)
        temp_map = resp.get("temp_id_mapping", {})
        new_token = resp.get("sync_token")
        with store.transaction():
            for c in chunk:
                real = temp_map.get(c["uuid"])
                if not real:
                    log.warning("no temp_id mapping for task %r — skipping map seed",
                                c["task"].get("name"))
                    continue
                store.upsert_map(entity_type=EntityType.TASK, sonto_id=c["uuid"],
                                 todoist_id=real, last_synced_hash=c["hash"],
                                 sonto_hash=c["hash"], todoist_hash=c["hash"],
                                 last_synced_at=now, deleted=0)
            if new_token:
                store.set_state("todoist_sync_token", new_token)
        applied += len(chunk)
    log.info("applied: %d task creates", applied)
    return applied


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
    for m in plan.moves:
        log.info("  MOVE    project %r -> re-parent under area's project", m["name"])


def _seed_matched(store: store_mod.Store, plan: adopt.Plan) -> None:
    """Record already-existing Sonto<->Todoist structure links in the id_map (idempotent)."""
    if not plan.matched:
        return
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    with store.transaction():
        for et, sonto_id, todoist_id, h in plan.matched:
            store.upsert_map(entity_type=et, sonto_id=sonto_id, todoist_id=todoist_id,
                             last_synced_hash=h, sonto_hash=h, todoist_hash=h,
                             last_synced_at=now, deleted=0)


def _create_structure(store: store_mod.Store, todoist: todoist_client.TodoistClient,
                      plan: adopt.Plan) -> int:
    """Create the missing structure in one batched /sync call, then seed the id_map."""
    if not plan.creates:
        return 0
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

    resp = todoist.apply_commands(commands)
    _check_status(resp)
    temp_map = resp.get("temp_id_mapping", {})
    new_token = resp.get("sync_token")

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    with store.transaction():
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

    log.info("structure applied: %d creates", len(plan.creates))
    return len(plan.creates)


def _apply_moves(store: store_mod.Store, todoist: todoist_client.TodoistClient,
                 plan: adopt.Plan) -> int:
    """Re-parent existing Todoist projects whose Sonto area changed (project_move)."""
    if not plan.moves:
        return 0
    cmds = [todoist.command("project_move", {"id": m["todoist_id"], "parent_id": m["parent"]})
            for m in plan.moves]
    resp = todoist.apply_commands(cmds)
    _check_status(resp)
    if resp.get("sync_token"):
        store.set_state("todoist_sync_token", resp["sync_token"])
        store.conn.commit()
    log.info("structure moved: %d projects re-parented", len(plan.moves))
    return len(plan.moves)


def _check_status(resp: dict) -> None:
    status = resp.get("sync_status", {})
    errors = {u: s for u, s in status.items() if s != "ok" and not (isinstance(s, dict) and s.get("error_code") is None)}
    if errors:
        raise todoist_client.TodoistError(f"Todoist command errors: {errors}")
