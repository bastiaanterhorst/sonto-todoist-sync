"""Sonto <-> Todoist entity/field translation and the impedance-mismatch policy.

The full entity translation is built out in P1-P4. The well-defined, decision-locked helpers
(week scheduling, priority) are implemented now since they're independent of the runtime tool
schemas. See docs/PLAN.md for the mapping tables.
"""

from __future__ import annotations

import datetime as _dt

from . import config

# --- Priority: Sonto `important` boolean <-> Todoist 1..4 (4 = P1/highest) ---

def important_to_priority(important: bool) -> int:
    return config.TODOIST_PRIORITY_IMPORTANT if important else config.TODOIST_PRIORITY_NORMAL


def priority_to_important(priority: int) -> bool:
    # Lossy 4->2: Todoist P1 (4) -> important; everything else -> not important.
    return int(priority or 1) >= config.TODOIST_PRIORITY_IMPORTANT


# --- Week scheduling --------------------------------------------------------
# A Sonto week-scheduled task maps to a Todoist due date on the first day of that week per the
# system locale (Monday in NL, Sunday in US) PLUS a `sonto-week-YYYY-WW` marker label which is
# the round-trip source of truth (so it returns to a Sonto *week*, not a specific day).

def week_label(year: int, iso_week: int) -> str:
    return f"{config.WEEK_LABEL_PREFIX}{year}-W{iso_week:02d}"


def parse_week_label(label: str) -> tuple[int, int] | None:
    if not label.startswith(config.WEEK_LABEL_PREFIX):
        return None
    rest = label[len(config.WEEK_LABEL_PREFIX):]  # "2026-W27"
    try:
        y, w = rest.split("-W")
        return int(y), int(w)
    except (ValueError, IndexError):
        return None


def week_due_date(ref_date: _dt.date) -> _dt.date:
    """Todoist due date for a Sonto task scheduled to the week containing `ref_date`:
    the first day of that week per the system locale."""
    return config.first_day_of_week(ref_date)


def week_label_for_date(ref_date: _dt.date) -> str:
    iso = ref_date.isocalendar()
    return week_label(iso.year, iso.week)


# --- Structure: canonical projections + Todoist create args (P1) ------------
# Sonto Area  -> Todoist top-level project
# Sonto Project (in area) -> Todoist sub-project (parent = area's project)
# Sonto Project (area-less) -> Todoist top-level project
# Sonto Group -> Todoist section (in the project's Todoist project)

def area_canonical(area: dict) -> dict:
    return {"name": (area.get("name") or "").strip()}


def project_canonical(project: dict, area_name: str | None) -> dict:
    return {"name": (project.get("name") or "").strip(), "area": (area_name or "").strip()}


def group_canonical(group: dict, parent_name: str | None) -> dict:
    return {"name": (group.get("name") or "").strip(), "parent": (parent_name or "").strip()}


def todoist_project_args(name: str, parent: str | None = None) -> dict:
    """`parent` is a Todoist project id or a temp_id (for a parent created same batch)."""
    args = {"name": name}
    if parent:
        args["parent_id"] = parent
    return args


def todoist_section_args(name: str, project: str) -> dict:
    return {"name": name, "project_id": project}


# --- Full entity translation (tasks etc. land in P2+) -----------------------

def sonto_to_todoist(entity):  # pragma: no cover - P2+
    raise NotImplementedError("Task translation lands in P2+; see docs/PLAN.md")


def todoist_to_sonto(entity):  # pragma: no cover - P1+
    raise NotImplementedError("Entity translation lands in P1-P4; see docs/PLAN.md")
