# sonto-todoist-sync

Two-way sync between **Sonto** (formerly "Space" — a goal & life planner, Mac app) and
**Todoist**, designed to run every 15 minutes and keep both as close to identical as possible.

- **Sonto side** is driven through the app's built-in **MCP server**, hit directly as a
  JSON-RPC-over-HTTP API at `http://127.0.0.1:2402/` (no LLM in the loop).
- **Todoist side** uses the **unified API v1** (`https://api.todoist.com/api/v1`) directly —
  the `/sync` incremental token for change detection, the completed-items endpoint for
  instances, and batched idempotent command writes.
- A **SQLite mapping layer** matches IDs across the two systems, detects what changed, and
  suppresses echo (ping-pong) via a canonical content hash.

Zero runtime dependencies — standard library only (Python 3.11+). No venv or pip required.

## Scope

Syncs **tasks, areas, projects, groups ("headings"), and tags**. Recurring tasks are out of
scope — only **instances** are synced; Todoist owns the recurrence definition.

## Status

Early build — **P0 (plumbing + introspection)**. Not yet wired to apply changes. See
`docs/PLAN.md` for the full design and phased build order.

## Layout

```
run.py                 # convenience entry point
syncer/
  config.py            # paths, endpoints, mapping policy flags
  transport.py         # tiny stdlib HTTP helper
  store.py             # SQLite: id_map, state, snapshot, applied_commands, run-lock
  model.py             # normalized entities + canonical field hashing (echo guard)
  mcp_client.py        # Sonto MCP: OAuth refresh + Streamable-HTTP JSON-RPC (initialize/tools)
  todoist_client.py    # Todoist API v1: /sync read, completed read, batched writes
  sonto.py             # semantic Sonto ops over mcp_client (schema-adaptive)
  mapping.py           # Sonto <-> Todoist entity/field translation + impedance policy
  reconcile.py         # the engine (classify -> resolve -> order -> apply -> write-back)
  adopt.py             # first-run match-existing pass (no duplicate creation)
  introspect.py        # P0 truth-finder: dump real tool schemas + sample payloads
  main.py              # CLI: --once / --dry-run / --status / --introspect
```

## Usage (current)

```
python run.py --introspect        # P0: connect to Sonto MCP, list tools + schemas
python run.py --once --dry-run    # snapshot both sides, print would-be changes (no writes)
python run.py --status            # last run, token health, pending conflicts
```

## Secrets

Following a project-scoped, gitignored `.secrets/` convention:

- `.secrets/todoist-token.json` — `{"access_token": "..."}` (Todoist → Settings → Integrations → Developer).
- Sonto tokens are read from the app's own store at
  `~/Library/Application Support/net.map-territory.Space/mcpb_proxy_tokens.json` and refreshed
  in place via the OAuth `refresh_token` grant.
