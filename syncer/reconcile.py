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
    inbox_id = next((p["id"] for p in td.get("projects", [])
                     if p.get("inbox_project") or p.get("is_inbox_project")), None)
    plan = adopt.match_structure(struct, td.get("projects", []), td.get("sections", []))

    _report(struct, plan)

    will_apply = (not dry_run) and phase in _APPLY_PHASES

    # Always record existing Sonto<->Todoist structure matches (internal bookkeeping, not a
    # Todoist write) so the task pass can resolve placement even in dry-run.
    _seed_matched(store, plan)

    # Structure (Sonto -> Todoist): skeleton first, so tasks place into real Todoist ids.
    struct_applied = 0
    if will_apply:
        struct_applied = _create_structure(store, todoist, plan)
        _apply_moves(store, todoist, plan)
    elif plan.creates or plan.moves:
        log.info("structure: %d creates, %d moves pending (dry-run/readonly: not applied)",
                 len(plan.creates), len(plan.moves))

    # Tasks (two-way).
    task_result = _reconcile_tasks(store, sonto, todoist, struct, td, inbox_id,
                                   dry_run=dry_run, phase=phase)

    return {"phase": phase, "dry_run": dry_run, "matched": len(plan.matched),
            "structure_creates": len(plan.creates), "structure_applied": struct_applied,
            "tasks": task_result}


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


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


_BATCH = config.TODOIST_MAX_COMMANDS_PER_BATCH


def _reconcile_tasks(store, sonto, todoist, struct, td, inbox_id, *, dry_run, phase) -> dict:
    will_forward = (not dry_run) and phase in _APPLY_PHASES
    will_reverse = (not dry_run) and phase == config.PHASE_TWOWAY and config.ALLOW_SONTO_WRITES
    will_sonto_delete = will_reverse and config.ALLOW_SONTO_DELETES

    # --- Sonto current state (incl. completed, to tell "done" from "deleted") ---
    sonto_tasks = sonto.snapshot_tasks(include_completed=True)
    s_by_uuid = {}
    for t in sonto_tasks:
        u = t.get("todoUUID")
        if not u:
            continue
        p, sec = _resolve_placement(store, t)
        canon = mapping.task_canonical(t, p, sec)
        s_by_uuid[u] = {"uuid": u, "task": t, "project": p, "section": sec,
                        "completed": bool(t.get("completed")), "canon": canon,
                        "hash": canonical_hash(canon)}

    # --- Todoist current state: active items + recently-completed items ---
    t_by_id = {}
    for i in td.get("items", []):
        if i.get("is_deleted"):
            continue
        canon = mapping.todoist_task_canonical(i, inbox_id)
        t_by_id[i["id"]] = {"item": i, "completed": False, "canon": canon,
                            "hash": canonical_hash(canon)}
    for ci in _fetch_completed(todoist):
        cid = ci.get("id") or ci.get("task_id") or ci.get("v2_task_id")
        if cid and cid not in t_by_id:
            t_by_id[cid] = {"item": ci, "completed": True, "canon": None, "hash": None}

    # --- classify each mapped pair + unmapped on either side ---
    B = {k: [] for k in ("fwd_create", "fwd_update", "fwd_complete", "fwd_delete",
                         "rev_create", "rev_update", "rev_complete", "rev_delete", "conflict")}
    mapped_s, mapped_t = set(), set()
    for row in store.maps(EntityType.TASK):
        if row.deleted:
            continue
        mapped_s.add(row.sonto_id)
        mapped_t.add(row.todoist_id)
        _classify_pair(row, s_by_uuid.get(row.sonto_id), t_by_id.get(row.todoist_id), B)
    for u, s in s_by_uuid.items():
        if u not in mapped_s and not s["completed"]:
            B["fwd_create"].append(s)
    for i, t in t_by_id.items():
        if (i not in mapped_t and not t["completed"] and _td_task_syncable(t["item"])
                and _td_task_has_sonto_home(store, t["item"], inbox_id)):
            B["rev_create"].append(t)

    _report_tasks(B, len(s_by_uuid), len(t_by_id), will_forward, will_reverse, will_sonto_delete)

    applied = {}
    if will_forward:
        applied["fwd_create"] = _forward_create_tasks(store, todoist, B["fwd_create"])
        applied["fwd_update"] = _forward_update_tasks(store, todoist, B["fwd_update"])
        applied["fwd_complete"] = _forward_complete_tasks(store, todoist, B["fwd_complete"])
        applied["fwd_delete"] = _forward_delete_tasks(store, todoist, B["fwd_delete"], len(s_by_uuid))
    if will_reverse:
        applied["rev_update"] = _reverse_update_tasks(store, sonto, struct, B["rev_update"] + B["conflict"])
        applied["rev_create"] = _reverse_create_tasks(store, sonto, struct, B["rev_create"], inbox_id)
        applied["rev_complete"] = _reverse_complete_tasks(store, sonto, B["rev_complete"])
    if will_sonto_delete:
        applied["rev_delete"] = _reverse_delete_tasks(store, sonto, B["rev_delete"], len(t_by_id))

    return {"counts": {k: len(v) for k, v in B.items()}, "applied": applied}


def _classify_pair(row, s, t, B) -> None:
    if s is None and t is None:
        return  # both gone -> nothing to do (tombstone bookkeeping deferred)
    if s is None:                       # gone from Sonto -> delete in Todoist
        B["fwd_delete"].append((row, t)); return
    if t is None:                       # gone from Todoist -> delete in Sonto (gated)
        B["rev_delete"].append((row, s)); return
    if s["completed"] and not t["completed"]:
        B["fwd_complete"].append((row, s, t)); return
    if t["completed"] and not s["completed"]:
        B["rev_complete"].append((row, s, t)); return
    if s["completed"] and t["completed"]:
        return
    s_changed = s["hash"] != row.last_synced_hash
    t_changed = t["hash"] != row.last_synced_hash
    if s_changed and t_changed:
        B["conflict"].append((row, s, t))      # strict LWW -> Todoist wins -> reverse update
    elif s_changed:
        B["fwd_update"].append((row, s, t))
    elif t_changed:
        B["rev_update"].append((row, s, t))


def _td_task_syncable(item: dict) -> bool:
    """Which Todoist tasks may reverse-create into Sonto. Excludes recurring (Todoist owns the
    recurrence) and sub-tasks (flattening into parent notes is a later refinement)."""
    if item.get("is_deleted") or item.get("parent_id"):
        return False
    if (item.get("due") or {}).get("is_recurring"):
        return False
    return True


def _fetch_completed(todoist) -> list:
    """Recently-completed Todoist tasks (so mapped completions read as 'done', not 'deleted').
    Defensive: any failure degrades gracefully to no completion info."""
    import datetime as dt
    today = dt.date.today()
    since = (today - dt.timedelta(days=89)).isoformat() + "T00:00:00Z"
    until = (today + dt.timedelta(days=1)).isoformat() + "T00:00:00Z"
    out, cursor = [], None
    try:
        for _ in range(10):  # page cap
            resp = todoist.get_completed_by_completion_date(since=since, until=until, cursor=cursor)
            out.extend(resp.get("items", []))
            cursor = resp.get("next_cursor")
            if not cursor:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("completed-tasks fetch failed (%s); completion detection degraded", e)
    return out


def _report_tasks(B, n_sonto, n_todoist, will_forward, will_reverse, will_sonto_delete) -> None:
    log.info("Tasks: sonto=%d todoist=%d | %s", n_sonto, n_todoist,
             " ".join(f"{k}={len(v)}" for k, v in B.items() if v))
    for s in B["fwd_create"][:15]:
        where = s["section"] or s["project"] or "Inbox"
        log.info("  +Todoist  %r -> %s%s", (s["task"].get("name") or "")[:48], where,
                 _sched_str(s["task"]))
    for t in B["rev_create"][:15]:
        log.info("  +Sonto    %r (from Todoist)", (t["item"].get("content") or "")[:48])
    if not will_forward:
        log.info("forward (Sonto->Todoist): dry-run/readonly — not applied")
    rev_pending = len(B["rev_create"]) + len(B["rev_update"]) + len(B["rev_complete"]) + len(B["conflict"])
    if rev_pending and not will_reverse:
        log.info("reverse (Todoist->Sonto): %d pending — needs phase=twoway + ALLOW_SONTO_WRITES "
                 "(not applied)", rev_pending)
    if B["rev_delete"] and not will_sonto_delete:
        log.info("reverse deletes into Sonto: %d pending — needs ALLOW_SONTO_DELETES (not applied)",
                 len(B["rev_delete"]))


# --- forward apply (Sonto -> Todoist) --------------------------------------

def _forward_create_tasks(store, todoist, creates) -> int:
    if not creates:
        return 0
    now, applied = _now_iso(), 0
    for chunk in _chunks(creates, _BATCH):
        cmds = [todoist.command("item_add",
                                mapping.task_item_args(c["task"], c["project"], c["section"]),
                                temp_id=c["uuid"]) for c in chunk]
        resp = todoist.apply_commands(cmds)
        _check_status(resp)
        temp_map = resp.get("temp_id_mapping", {})
        with store.transaction():
            for c in chunk:
                real = temp_map.get(c["uuid"])
                if not real:
                    log.warning("no temp_id for task %r", c["task"].get("name")); continue
                store.upsert_map(entity_type=EntityType.TASK, sonto_id=c["uuid"], todoist_id=real,
                                 last_synced_hash=c["hash"], sonto_hash=c["hash"],
                                 todoist_hash=c["hash"], last_synced_at=now, deleted=0)
            _save_token(store, resp)
        applied += len(chunk)
    return applied


def _forward_update_tasks(store, todoist, pairs) -> int:
    if not pairs:
        return 0
    now, applied = _now_iso(), 0
    for chunk in _chunks(pairs, _BATCH // 2):  # update may emit 2 commands (update + move)
        cmds, meta = [], []
        for row, s, t in chunk:
            item = t["item"]
            args = mapping.task_update_args(s["task"]); args["id"] = item["id"]
            cmds.append(todoist.command("item_update", args))
            want_proj, want_sec = s["project"], s["section"]
            cur_proj, cur_sec = item.get("project_id"), item.get("section_id")
            if (want_sec and want_sec != cur_sec) or (not want_sec and want_proj and want_proj != cur_proj):
                mv = {"id": item["id"]}
                if want_sec:
                    mv["section_id"] = want_sec
                elif want_proj:
                    mv["project_id"] = want_proj
                cmds.append(todoist.command("item_move", mv))
            meta.append((row, s))
        resp = todoist.apply_commands(cmds)
        _check_status(resp)
        with store.transaction():
            for row, s in meta:
                store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                                 todoist_id=row.todoist_id, last_synced_hash=s["hash"],
                                 sonto_hash=s["hash"], todoist_hash=s["hash"],
                                 last_synced_at=now, deleted=0)
            _save_token(store, resp)
        applied += len(meta)
    return applied


def _forward_complete_tasks(store, todoist, pairs) -> int:
    if not pairs:
        return 0
    now, applied = _now_iso(), 0
    for chunk in _chunks(pairs, _BATCH):
        cmds = [todoist.command("item_close", {"id": t["item"]["id"]}) for _, _, t in chunk]
        resp = todoist.apply_commands(cmds)
        _check_status(resp)
        with store.transaction():
            for row, s, t in chunk:
                store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                                 todoist_id=row.todoist_id, last_synced_hash=row.last_synced_hash,
                                 last_synced_at=now, deleted=0)
            _save_token(store, resp)
        applied += len(chunk)
    return applied


def _forward_delete_tasks(store, todoist, pairs, sonto_total) -> int:
    if not pairs:
        return 0
    if config.EMPTY_READ_SANITY_FLOOR and sonto_total == 0:
        log.warning("sanity floor: Sonto returned 0 tasks; skipping %d Todoist deletes", len(pairs))
        return 0
    now, applied = _now_iso(), 0
    for chunk in _chunks(pairs, _BATCH):
        cmds = [todoist.command("item_delete", {"id": t["item"]["id"]}) for _, t in chunk]
        resp = todoist.apply_commands(cmds)
        _check_status(resp)
        with store.transaction():
            for row, t in chunk:
                store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                                 todoist_id=row.todoist_id, last_synced_hash=row.last_synced_hash,
                                 last_synced_at=now, deleted=1)
            _save_token(store, resp)
        applied += len(chunk)
    return applied


# --- reverse apply (Todoist -> Sonto), gated behind ALLOW_SONTO_WRITES ------

def _group_parent_index(struct: dict) -> dict:
    out = {}
    for g in struct.get("groups", []):
        out[g["id"]] = ("project", g["project_id"]) if g.get("project_id") else ("area", g["area_id"])
    return out


def _td_task_has_sonto_home(store, item, inbox_id) -> bool:
    """True if a Todoist task maps to a Sonto location (mapped section/project/area, or the
    Todoist Inbox -> Sonto Inbox). Tasks in unmapped Todoist-only projects have no home and are
    NOT reverse-synced (avoids dumping them into Sonto Inbox + a placement ping-pong)."""
    sec = item.get("section_id")
    if sec and store.map_by_todoist(EntityType.GROUP, sec):
        return True
    proj = item.get("project_id")
    if not proj or proj == inbox_id:
        return True
    return bool(store.map_by_todoist(EntityType.PROJECT, proj)
                or store.map_by_todoist(EntityType.AREA, proj))


def _sonto_placement_for_todoist(store, raw, group_parent, item, inbox_id) -> dict | None:
    """add_task placement kwargs (context_type + raw Sonto tokens) for a Todoist item, or None
    if it has no Sonto home (caller skips)."""
    sec = item.get("section_id")
    if sec:
        gm = store.map_by_todoist(EntityType.GROUP, sec)
        if gm and raw.get(gm.sonto_id) and gm.sonto_id in group_parent:
            kind, pstable = group_parent[gm.sonto_id]
            if raw.get(pstable):
                return {"context_type": kind, f"{kind}_id": raw[pstable], "group_id": raw[gm.sonto_id]}
    proj = item.get("project_id")
    if proj and proj != inbox_id:
        pm = store.map_by_todoist(EntityType.PROJECT, proj)
        if pm and raw.get(pm.sonto_id):
            return {"context_type": "project", "project_id": raw[pm.sonto_id]}
        am = store.map_by_todoist(EntityType.AREA, proj)
        if am and raw.get(am.sonto_id):
            return {"context_type": "area", "area_id": raw[am.sonto_id]}
        return None  # unmapped non-inbox project -> no Sonto home
    return {"context_type": "inbox"}


def _reverse_create_tasks(store, sonto, struct, metas, inbox_id) -> int:
    if not metas:
        return 0
    raw = sonto_mod.Sonto.raw_index(struct)
    gp = _group_parent_index(struct)
    tags = sonto.tag_index()
    now, applied = _now_iso(), 0
    for m in metas:
        item = m["item"]
        placement = _sonto_placement_for_todoist(store, raw, gp, item, inbox_id)
        if placement is None:
            continue  # no Sonto home -> skip
        add_args = {"name": item.get("content") or "", **placement}
        if item.get("description"):
            add_args["notes"] = item["description"]
        try:
            res = sonto.add_task(**add_args)
            uuid = sonto.extract_todo_uuid(res)
            if not uuid:
                log.warning("reverse create: no todoUUID for %r", item.get("content")); continue
            edit = {"todo_uuid": uuid, **mapping.todoist_schedule_to_sonto(item)}
            if mapping.priority_to_important(item.get("priority", 1)):
                edit["is_important"] = True
            tag_uuids = mapping.todoist_labels_to_tag_uuids(item.get("labels"), tags)
            if tag_uuids:
                edit["set_tags"] = tag_uuids
            if len(edit) > 1:
                sonto.edit_task(**edit)
        except Exception as e:  # noqa: BLE001
            log.warning("reverse create failed for %r: %s", item.get("content"), e); continue
        h = m["hash"]
        with store.transaction():
            store.upsert_map(entity_type=EntityType.TASK, sonto_id=uuid, todoist_id=item["id"],
                             last_synced_hash=h, sonto_hash=h, todoist_hash=h,
                             last_synced_at=now, deleted=0)
        applied += 1
    return applied


def _reverse_update_tasks(store, sonto, struct, pairs) -> int:
    if not pairs:
        return 0
    tags = sonto.tag_index()
    now, applied = _now_iso(), 0
    for row, s, t in pairs:
        item = t["item"]
        edit = {"todo_uuid": row.sonto_id, "name": item.get("content") or "",
                "notes": item.get("description") or "",
                "is_important": mapping.priority_to_important(item.get("priority", 1)),
                "set_tags": mapping.todoist_labels_to_tag_uuids(item.get("labels"), tags),
                **mapping.todoist_schedule_to_sonto(item)}
        try:
            sonto.edit_task(**edit)
        except Exception as e:  # noqa: BLE001
            log.warning("reverse update failed for %r: %s", item.get("content"), e); continue
        h = t["hash"]
        with store.transaction():
            store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                             todoist_id=row.todoist_id, last_synced_hash=h, sonto_hash=h,
                             todoist_hash=h, last_synced_at=now, deleted=0)
        applied += 1
    return applied


def _reverse_complete_tasks(store, sonto, pairs) -> int:
    if not pairs:
        return 0
    now, applied = _now_iso(), 0
    for row, s, t in pairs:
        try:
            sonto.complete_task(row.sonto_id)
        except Exception as e:  # noqa: BLE001
            log.warning("reverse complete failed: %s", e); continue
        with store.transaction():
            store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                             todoist_id=row.todoist_id, last_synced_hash=row.last_synced_hash,
                             last_synced_at=now, deleted=0)
        applied += 1
    return applied


def _reverse_delete_tasks(store, sonto, pairs, todoist_total) -> int:
    if not pairs:
        return 0
    if config.EMPTY_READ_SANITY_FLOOR and todoist_total == 0:
        log.warning("sanity floor: Todoist returned 0 tasks; skipping %d Sonto deletes", len(pairs))
        return 0
    now, applied = _now_iso(), 0
    for row, s in pairs:
        try:
            sonto.delete_task(row.sonto_id)
        except Exception as e:  # noqa: BLE001
            log.warning("reverse delete failed: %s", e); continue
        with store.transaction():
            store.upsert_map(entity_type=EntityType.TASK, sonto_id=row.sonto_id,
                             todoist_id=row.todoist_id, last_synced_hash=row.last_synced_hash,
                             last_synced_at=now, deleted=1)
        applied += 1
    return applied


def _save_token(store, resp) -> None:
    tok = resp.get("sync_token")
    if tok:
        store.set_state("todoist_sync_token", tok)


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
