"""First-run adopt/match + structure planning (P1).

Matches the Sonto structure (areas, projects, groups) against the existing Todoist projects
and sections by name-within-parent, so we don't duplicate what's already there. Anything
unmatched becomes an ordered create plan (areas -> projects -> groups), with `temp_id`s so a
parent created in the same batch can be referenced by its children.

Mapping: Sonto Area -> Todoist top-level project; Sonto Project -> sub-project (parent =
area's project) or top-level if area-less; Sonto Group -> section in the project's project.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import mapping
from .model import EntityType, canonical_hash


@dataclass
class Plan:
    matched: list = field(default_factory=list)   # (entity_type, sonto_id, todoist_id, hash)
    creates: list = field(default_factory=list)   # dicts (see _create)
    area_map: dict = field(default_factory=dict)  # sonto area id -> todoist project id|temp
    project_map: dict = field(default_factory=dict)
    group_map: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return (s or "").strip().casefold()


def match_structure(sonto: dict, td_projects: list[dict], td_sections: list[dict]) -> Plan:
    plan = Plan()
    counter = [0]

    def temp() -> str:
        counter[0] += 1
        return f"tmp_{counter[0]}"

    # Index Todoist side.
    inbox_ids = {p["id"] for p in td_projects if p.get("is_inbox_project")}
    top_by_name: dict[str, dict] = {}
    sub_by_parent_name: dict[tuple[str, str], dict] = {}
    for p in td_projects:
        if p["id"] in inbox_ids:
            continue
        if p.get("parent_id"):
            sub_by_parent_name[(p["parent_id"], _norm(p["name"]))] = p
        else:
            top_by_name.setdefault(_norm(p["name"]), p)
    sec_by_project_name: dict[tuple[str, str], dict] = {
        (s["project_id"], _norm(s["name"])): s for s in td_sections
    }
    consumed: set[str] = set()

    area_name = {a["id"]: a["name"] for a in sonto["areas"]}
    project_name = {p["id"]: p["name"] for p in sonto["projects"]}

    def _create(entity_type, sonto_id, name, parent, parent_name, canonical):
        tid = temp()
        plan.creates.append({
            "entity_type": entity_type, "sonto_id": sonto_id, "name": name,
            "parent": parent, "parent_name": parent_name, "temp_id": tid,
            "hash": canonical_hash(canonical),
        })
        return tid

    # 1) Areas -> top-level projects.
    for a in sonto["areas"]:
        canon = mapping.area_canonical(a)
        existing = top_by_name.get(_norm(a["name"]))
        if existing and existing["id"] not in consumed:
            consumed.add(existing["id"])
            plan.area_map[a["id"]] = existing["id"]
            plan.matched.append((EntityType.AREA, a["id"], existing["id"], canonical_hash(canon)))
        else:
            plan.area_map[a["id"]] = _create(EntityType.AREA, a["id"], a["name"], None, None, canon)

    # 2) Projects -> sub-projects (under area) or top-level (area-less).
    for p in sonto["projects"]:
        a_name = area_name.get(p["area_id"]) if p["area_id"] else None
        canon = mapping.project_canonical(p, a_name)
        parent_td = plan.area_map.get(p["area_id"]) if p["area_id"] else None
        existing = None
        if parent_td and not parent_td.startswith("tmp_"):
            existing = sub_by_parent_name.get((parent_td, _norm(p["name"])))
        elif not p["area_id"]:
            existing = top_by_name.get(_norm(p["name"]))
        if existing and existing["id"] not in consumed:
            consumed.add(existing["id"])
            plan.project_map[p["id"]] = existing["id"]
            plan.matched.append((EntityType.PROJECT, p["id"], existing["id"], canonical_hash(canon)))
        else:
            plan.project_map[p["id"]] = _create(
                EntityType.PROJECT, p["id"], p["name"], parent_td, a_name, canon)

    # 3) Groups -> sections in the parent project.
    for g in sonto["groups"]:
        if g["project_id"]:
            parent_td = plan.project_map.get(g["project_id"])
            parent_name = project_name.get(g["project_id"])
        else:
            parent_td = plan.area_map.get(g["area_id"])
            parent_name = area_name.get(g["area_id"])
        canon = mapping.group_canonical(g, parent_name)
        existing = None
        if parent_td and not parent_td.startswith("tmp_"):
            existing = sec_by_project_name.get((parent_td, _norm(g["name"])))
        if existing and existing["id"] not in consumed:
            consumed.add(existing["id"])
            plan.group_map[g["id"]] = existing["id"]
            plan.matched.append((EntityType.GROUP, g["id"], existing["id"], canonical_hash(canon)))
        else:
            plan.group_map[g["id"]] = _create(
                EntityType.GROUP, g["id"], g["name"], parent_td, parent_name, canon)

    return plan
