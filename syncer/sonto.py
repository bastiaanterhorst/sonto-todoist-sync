"""Semantic Sonto operations over the raw MCP client.

Thin wrappers around `mcp_client.call_tool` that adapt to the tool `inputSchema` discovered at
runtime (no Sonto param schemas are documented). Read methods are usable now; write methods are
fleshed out per phase once `introspect` confirms the real argument shapes.
"""

from __future__ import annotations

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
