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
- **Reverse structure**: Todoist-only projects become Sonto areas/projects (a top-level project
  with sub-projects → Area, otherwise → Project; sections → Groups), then their tasks flow in.

Gated / not yet done (see `docs/PLAN.md`, `build-log.md`): deletes **into** Sonto
(`ALLOW_SONTO_DELETES`, off), sub-task flattening, project/area tags reverse, and the scheduled
job (documented below, intentionally not installed).

## ⚠️ Todoist ↔ Sonto: where the mapping is NOT 1:1

Sonto and Todoist model planning differently. The sync does its best, but some things are
**lossy, one-directional, or deliberately dropped**. Know these before trusting it blindly:

- **Containers (Areas vs Projects).** Sonto separates **Areas** (never-ending life categories)
  from **Projects** (bounded). Todoist has one generic, nestable "project". Mapping:
  - Sonto → Todoist: Area → top-level project, Project → sub-project, Group → section.
  - Todoist → Sonto: a top-level project **with** sub-projects → **Area**; otherwise → **Project**;
    sub-projects → Projects; sections → Groups.
  - Consequence: a project's *kind* can change across a round-trip if its hierarchy changes
    (e.g. give a Sonto Project a sub-project and it becomes an Area-shaped thing).
- **Scheduling (ladder vs due date).** Sonto schedules on a granularity ladder
  (unscheduled → week → day → timed event); Todoist has a single **due date** plus a separate
  **deadline**.
  - Day → Todoist due date (clean).
  - **Week → Todoist due date on the first day of that week (per your system locale: Monday in
    NL, Sunday in US) + a `sonto-week-YYYY-WW` label.** The label is the round-trip source of
    truth; the date is just so it's usable in Todoist.
  - Timed calendar events → Todoist due *datetime*; the event's duration / calendar / event-ness
    is Sonto-only and **dropped**.
  - **Todoist task `deadline` has no Sonto equivalent** (Sonto deadlines are project-level only)
    → dropped.
- **Priority (boolean vs 4 levels).** Sonto `important` is on/off; Todoist is P1–P4.
  `important` ↔ **P1**; P2/P3/P4 all collapse to *not important* coming back (lossy 4 → 2).
- **Sub-tasks.** Todoist has real nested sub-tasks; Sonto has none (sub-items are `[ ]`/`[x]`
  checklist lines inside a task's notes). **Todoist sub-tasks are currently NOT synced** (skipped
  to avoid duplication).
- **Tags vs labels.** Sonto tags attach to tasks, projects **and** areas; Todoist labels attach
  to **tasks only**. Task tags ↔ labels works. Project/area tags are **not mirrored**. And a new
  Todoist label can't create a Sonto tag (Sonto's MCP exposes no create-tag tool) — only
  *existing* Sonto tags are settable from Todoist. The `sonto-week-…` label is internal plumbing.
- **Notes formatting.** Sonto rich text ↔ Todoist Markdown, best-effort. Todoist auto-converts
  bare URLs into `[title](url)` links; that's cosmetic and normalized so it doesn't ping-pong.
- **Completion & recurring.** Completion syncs both ways. **Recurring tasks are not modeled in
  Sonto** — only the current instance syncs; Todoist owns the recurrence (we never delete/recreate
  a recurring task).
- **Conflicts.** If the same task is edited on both sides between runs, **Todoist wins** (Sonto
  exposes no per-item modified timestamp, so strict last-write-wins falls back to Todoist).
- **Deletes.** Sonto → Todoist deletes propagate. **Todoist → Sonto deletes are OFF by default**
  (`ALLOW_SONTO_DELETES` in `syncer/config.py`) — deleting from the planner is the highest-risk
  action.
- **Sonto-only states.** The in-progress toggle, the "late" indicator, and manual task/section
  **ordering** are Sonto-only and not represented in Todoist (ordering is intentionally not synced).

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
