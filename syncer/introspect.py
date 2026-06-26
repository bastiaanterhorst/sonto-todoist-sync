"""P0 truth-finder.

The Sonto MCP tool *names* are known but their parameter schemas are not documented anywhere.
This connects to the live MCP, dumps every tool's `inputSchema`, and (read-only) samples a few
`list_*`/`get_*` calls to learn payload shapes — crucially whether entities carry stable IDs
and a modified timestamp (which decides strict-LWW vs the Todoist-wins fallback) and whether
any tag tooling exists. Output is printed and written to `data/sonto-tools.json`.

Strictly read-only: only `tools/list` and read-style tools are called.
"""

from __future__ import annotations

import json

from . import config, mcp_client

# Read-only tools that are safe to sample for payload shape.
SAFE_SAMPLE_TOOLS = ["list_areas", "list_projects", "list_groups", "get_inbox"]


def run_introspection(*, write: bool = True) -> dict:
    client = mcp_client.McpClient()
    print(f"Connecting to Sonto MCP at {config.sonto_mcp_url(client.port)} ...")
    init = client.connect()
    server = (init or {}).get("serverInfo", {})
    print(f"  serverInfo: {server}\n")

    tools = client.list_tools()
    print(f"tools/list -> {len(tools)} tools:\n")
    by_name = {}
    for t in sorted(tools, key=lambda x: x.get("name", "")):
        name = t.get("name", "?")
        by_name[name] = t
        desc = (t.get("description") or "").strip().splitlines()[0:1]
        print(f"  - {name}: {desc[0] if desc else ''}")

    # Flag the unknowns the design depends on.
    tag_tools = [n for n in by_name if "tag" in n.lower()]
    print("\nCapability checks:")
    print(f"  tag-related tools: {tag_tools or 'NONE (task-tag sync degrades to ignored)'}")

    samples = {}
    print("\nSampling read-only tools for payload shape (ids? timestamps?):")
    for name in SAFE_SAMPLE_TOOLS:
        if name not in by_name:
            continue
        try:
            result = client.call_tool(name, {})
            samples[name] = result
            print(f"\n  {name} ->")
            print(_preview(result))
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"  {name} -> ERROR: {e}")

    report = {
        "serverInfo": server,
        "tools": by_name,
        "tag_tools": tag_tools,
        "samples": samples,
    }
    if write:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = config.DATA_DIR / "sonto-tools.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote full schemas + samples to {out}")
    return report


def _preview(obj, limit: int = 1200) -> str:
    blob = json.dumps(obj, indent=2, ensure_ascii=False)
    return blob if len(blob) <= limit else blob[:limit] + "\n    ... (truncated)"
