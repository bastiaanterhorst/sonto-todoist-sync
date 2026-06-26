"""Semantic Sonto operations over the raw MCP client.

Thin wrappers around `mcp_client.call_tool` that adapt to the tool `inputSchema` discovered at
runtime (no Sonto param schemas are documented). Read methods are usable now; write methods are
fleshed out per phase once `introspect` confirms the real argument shapes.
"""

from __future__ import annotations

import json
from typing import Any

from . import mcp_client


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
        """Areas, (non-plan) projects with area linkage, and groups. The shape the P1
        structure mirror consumes."""
        areas = self.areas()
        proj_area: dict[str, str] = {}
        for a in areas:
            for p in self.area_detail(a["areaID"]).get("projects", []):
                proj_area[p["projectID"]] = a["areaID"]

        projects = []
        for p in self.all_projects(include_completed=False):
            if p.get("isPlan"):
                continue  # plans (yearly/quarterly) are not task projects -> excluded
            projects.append({
                "id": p["projectID"], "name": p["name"], "notes": p.get("notes", ""),
                "area_id": proj_area.get(p["projectID"]), "tags": p.get("tags", []),
            })

        groups = []
        for a in areas:
            for g in self.groups_in_area(a["areaID"]):
                groups.append({"id": g["groupID"], "name": g["name"],
                               "area_id": a["areaID"], "project_id": None})
        for p in projects:
            for g in self.groups_in_project(p["id"]):
                groups.append({"id": g["groupID"], "name": g["name"],
                               "project_id": p["id"], "area_id": None})

        return {
            "areas": [{"id": a["areaID"], "name": a["name"], "emoji": a.get("emoji", ""),
                       "notes": a.get("notes", ""), "tags": a.get("tags", [])} for a in areas],
            "projects": projects,
            "groups": groups,
        }

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
