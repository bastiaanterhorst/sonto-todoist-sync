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

**Working two-way sync** (applied to a live Sonto + Todoist, idempotent):

- **Structure** Sonto → Todoist: areas → projects, projects → sub-projects, groups → sections
  (with `project_move` when a project's area changes).
- **Tasks, both directions**: create / update / complete, plus Sonto → Todoist delete. Day &
  week scheduling (week → first-day-of-week-per-locale + a `sonto-week-YYYY-WW` round-trip
  label), `important` ↔ priority, notes, tags ↔ labels, Inbox.
- Conflict resolution: strict last-write-wins, falling back to **Todoist-wins** (Sonto exposes
  no per-item modified timestamp).
- Reverse writes only touch tasks with a real Sonto home (Todoist Inbox or a mapped
  project/section); Todoist-only-project tasks are left alone.

Gated / not yet done (see `docs/PLAN.md`, `build-log.md`): deletes **into** Sonto
(`ALLOW_SONTO_DELETES`, off), reverse structure creation (Todoist project → Sonto area),
sub-task flattening, and the scheduled job (documented below, intentionally not installed).

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

## Usage

```
python run.py --status            # last run, bootstrap phase, token health
python run.py --introspect        # connect to Sonto MCP, dump tools + schemas (read-only)
python run.py --once --dry-run    # reconcile both sides, print would-be changes (no writes)
python run.py --once              # reconcile and APPLY (per the current bootstrap phase)
python run.py --set-phase PHASE   # readonly | oneway_sonto_to_todoist | oneway_with_deletes | twoway
```

**Bootstrap ladder** (`--set-phase`, stored in the DB): `readonly` (no writes) →
`oneway_sonto_to_todoist` (forward only) → `oneway_with_deletes` → `twoway` (full two-way).
Reverse writes into Sonto also require `ALLOW_SONTO_WRITES` (on); deletes into Sonto require
`ALLOW_SONTO_DELETES` (off) — both in `syncer/config.py`. Always `--dry-run` first after changes.

## Scheduling (run every 15 minutes via launchd) — NOT installed; set up manually

The job is intentionally **not** created by this repo. To run the sync every 15 minutes on
macOS, install a LaunchAgent yourself:

1. Copy the template and edit the paths (a ready template is in `deploy/`):

   `deploy/net.map-territory.sonto-todoist-sync.plist.example` →
   `~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist`

   Set the absolute path to `python3` (e.g. `/opt/homebrew/bin/python3`) and to `run.py`
   (`/Users/<you>/code/sonto-todoist-sync/run.py`). `StartInterval` is `900` (15 min);
   `RunAtLoad` runs once on load. Logs go to `logs/sync.out` / `logs/sync.err`.

2. Load it (`logs/` must exist):

   ```
   mkdir -p logs
   launchctl load ~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist
   launchctl start net.map-territory.sonto-todoist-sync     # run once now
   tail -f logs/sync.err                                    # watch
   ```

3. Manage it:

   ```
   launchctl list | grep sonto-todoist          # is it loaded?
   launchctl unload ~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist  # stop
   ```

**Preconditions for each run:** the Sonto Mac app must be running with its MCP server enabled
(Settings → AI), since the sync talks to `127.0.0.1:2402`; and the machine needs network for
the Todoist API. The Sonto access token is auto-refreshed; if its *refresh* token ever dies the
run exits with a `needs_repair` note and you re-pair the MCP in Sonto settings. No env vars are
needed — secrets are read from `.secrets/` and the Sonto app's token store.

## Secrets

Following a project-scoped, gitignored `.secrets/` convention:

- `.secrets/todoist-token.json` — `{"access_token": "..."}` (Todoist → Settings → Integrations → Developer).
- Sonto tokens are read from the app's own store at
  `~/Library/Application Support/net.map-territory.Space/mcpb_proxy_tokens.json` and refreshed
  in place via the OAuth `refresh_token` grant.
