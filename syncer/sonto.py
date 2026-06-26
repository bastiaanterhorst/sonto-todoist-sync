"""Semantic Sonto operations over the raw MCP client.

Thin wrappers around `mcp_client.call_tool` that adapt to the tool `inputSchema` discovered at
runtime (no Sonto param schemas are documented). Read methods are usable now; write methods are
fleshed out per phase once `introspect` confirms the real argument shapes.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from . import mcp_client


def stable_id(token: str) -> str:
    """Sonto area/project/group IDs are base64 Core-Data object tokens whose RAW string varies
    every read (non-deterministic JSON encoding) but which decode to a stable
    `uriRepresentation` (e.g. `x-coredata://STORE/Area/p8`). Use the decoded URI as the map
    key. Task ids (`todoUUID`) are already stable and must NOT be passed here."""
    if not isinstance(token, str):
        return token
    try:
        obj = json.loads(base64.b64decode(token))
        impl = obj.get("implementation", obj)
        return (impl.get("uriRepresentation")
                or f"{impl.get('storeIdentifier')}/{impl.get('entityName')}/{impl.get('primaryKey')}")
    except Exception:
        return token


class Sonto:
    def __init__(self, client: mcp_client.McpClient | None = None):
        self.client = client or mcp_client.McpClient()
        self._tools: dict[str, dict] | None = None

    def tools(self) -> dict[str, dict]:
        if self._tools is None:
            self._tools = {t["name"]: t for t in self.client.list_tools()}
        return self._tools

    def has_tool(self, name: str) -> bool:
        return name in self.tools()

    def _call(self, name: str, args: dict | None = None) -> Any:
        if not self.has_tool(name):
            raise mcp_client.McpError(f"Sonto MCP has no tool '{name}' in this version")
        return self.client.call_tool(name, args or {})

    # --- reads (P0/P1) -----------------------------------------------------
    def list_areas(self) -> Any:
        return self._call("list_areas")

    def list_projects(self) -> Any:
        return self._call("list_projects")

    def list_groups(self) -> Any:
        return self._call("list_groups")

    def get_inbox(self) -> Any:
        return self._call("get_inbox")

    def search_todos(self, **args) -> Any:
        return self._call("search_todos", args)

    def get_day(self, **args) -> Any:
        return self._call("get_day", args)

    def get_week(self, **args) -> Any:
        return self._call("get_week", args)

    # --- parsed reads + structure snapshot (P1) ----------------------------
    @staticmethod
    def parse(result: Any) -> Any:
        """Unwrap an MCP tool result -> inner `data` dict (raises on ok=false)."""
        try:
            text = result["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return result
        obj = json.loads(text)
        if obj.get("ok") is False:
            raise mcp_client.McpError(f"Sonto tool error: {obj.get('error')}")
        return obj.get("data", obj)

    def areas(self) -> list[dict]:
        return self.parse(self.list_areas()).get("areas", [])

    def area_detail(self, area_id: str) -> dict:
        return self.parse(self._call("get_area", {"area_id": area_id}))

    def all_projects(self, include_completed: bool = False) -> list[dict]:
        data = self.parse(self._call("list_projects", {"include_completed": include_completed}))
        return data.get("projects", [])

    def groups_in_project(self, project_id: str) -> list[dict]:
        data = self.parse(self._call("list_groups",
                                     {"context_type": "project", "project_id": project_id}))
        return data.get("groups", [])

    def groups_in_area(self, area_id: str) -> list[dict]:
        data = self.parse(self._call("list_groups",
                                     {"context_type": "area", "area_id": area_id}))
        return data.get("groups", [])

    def tags(self) -> list[dict]:
        return self.parse(self._call("list_tags", {})).get("tags", [])

    def snapshot_structure(self) -> dict:
        """Areas, (non-plan) projects with area linkage, and groups. IDs are the STABLE decoded
        URIs (see stable_id); the raw per-read token is kept only for same-run MCP reads."""
        areas = self.areas()
        proj_area: dict[str, str] = {}  # stable project id -> stable area id
        for a in areas:
            sa = stable_id(a["areaID"])
            for p in self.area_detail(a["areaID"]).get("projects", []):
                proj_area[stable_id(p["projectID"])] = sa

        projects = []
        for p in self.all_projects(include_completed=False):
            if p.get("isPlan"):
                continue  # plans (yearly/quarterly) are not task projects -> excluded
            sp = stable_id(p["projectID"])
            projects.append({
                "id": sp, "raw": p["projectID"], "name": p["name"], "notes": p.get("notes", ""),
                "area_id": proj_area.get(sp), "tags": p.get("tags", []),
            })

        groups = []
        for a in areas:
            sa = stable_id(a["areaID"])
            for g in self.groups_in_area(a["areaID"]):
                groups.append({"id": stable_id(g["groupID"]), "name": g["name"],
                               "area_id": sa, "project_id": None})
        for p in projects:
            for g in self.groups_in_project(p["raw"]):
                groups.append({"id": stable_id(g["groupID"]), "name": g["name"],
                               "project_id": p["id"], "area_id": None})

        return {
            "areas": [{"id": stable_id(a["areaID"]), "name": a["name"], "emoji": a.get("emoji", ""),
                       "notes": a.get("notes", ""), "tags": a.get("tags", [])} for a in areas],
            "projects": projects,
            "groups": groups,
        }

    def snapshot_tasks(self, today=None, *, include_completed: bool = False) -> list[dict]:
        """All tasks, merged from container reads (inbox/area/project) and the scheduled-task
        horizon (get_day/get_week). Each task dict carries its container ids (projectID/areaID/
        groupID) and schedule (scheduledDayISO or scheduledWeek/Year). Completed tasks are
        excluded unless include_completed."""
        import datetime as _dt

        from . import config

        today = today or _dt.date.today()
        records: dict[str, dict] = {}

        def merge(todos):
            for t in todos:
                u = t.get("todoUUID")
                if not u:
                    continue
                rec = records.setdefault(u, {})
                for k, v in t.items():
                    if rec.get(k) in (None, "", [], {}) or k not in rec:
                        rec[k] = v

        # Container reads — complete coverage of filed tasks (+ inbox).
        merge(self.parse(self.get_inbox()).get("todos", []))
        for a in self.areas():
            merge(self.area_detail(a["areaID"]).get("todos", []))
        for p in self.all_projects(include_completed=False):
            if p.get("isPlan"):
                continue
            merge(self.parse(self._call("get_project", {"project_id": p["projectID"]}))
                  .get("todos", []))

        # Scheduled-task horizon — catches purely-scheduled (container-less) tasks.
        for i in range(0, config.DAY_HORIZON_FUTURE_DAYS + 1):
            d = (today + _dt.timedelta(days=i)).isoformat()
            merge(self.parse(self._call("get_day", {"day_iso": d, "include_late": i == 0}))
                  .get("todos", []))
        for i in range(0, config.WEEK_HORIZON_FUTURE + 1):
            ref = (today + _dt.timedelta(weeks=i)).isocalendar()
            merge(self.parse(self._call("get_week", {"week": ref.week, "year": ref.year}))
                  .get("todos", []))

        tasks = list(records.values())
        if not include_completed:
            tasks = [t for t in tasks if not t.get("completed")]
        return tasks

    # --- writes (built out P1+; argument shapes confirmed via introspect) ---
    def add_task(self, **args) -> Any:  # pragma: no cover - P2+
        return self._call("add_task", args)

    def edit_task(self, **args) -> Any:  # pragma: no cover - P2+
        return self._call("edit_task", args)

    def add_project(self, **args) -> Any:  # pragma: no cover - P1+
        return self._call("add_project", args)

    def add_area(self, **args) -> Any:  # pragma: no cover - P1+
        return self._call("add_area", args)

    def add_group(self, **args) -> Any:  # pragma: no cover - P1+
        return self._call("add_group", args)
