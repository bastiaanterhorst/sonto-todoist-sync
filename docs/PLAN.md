# Design & Implementation Plan — Sonto ↔ Todoist Two-Way Sync

## Goal

A script run every 15 minutes that keeps **Sonto** (planner app, formerly "Space") and
**Todoist** as close to identical as possible — genuine two-way sync. Sonto is driven via its
built-in **MCP server hit directly as a JSON-RPC HTTP API** (no LLM). A **SQLite mapping
layer** matches IDs and detects change. Scope: **tasks, areas, projects, groups ("headings"),
tags**. Recurring tasks are out of scope — sync **instances only**; Todoist owns recurrence.

## Locked decisions

1. **Todoist access → direct API v1.** The official `Doist/todoist-cli` and third-party
   `sachaos/todoist` lack JSON output, `updated_at`, the incremental `sync_token`, and
   completed-task access. The CLI may be used for token bootstrap / manual debugging only.
2. **Conflict resolution → strict last-write-wins** by modified timestamp. Requires Sonto to
   expose a reliable per-item timestamp (resolved at introspection); else fall back to
   Todoist-wins (`config.LWW_FALLBACK_WINNER`). Deletes beat edits.
3. **Sub-tasks → flatten one-way (Todoist → Sonto notes).** Todoist sub-tasks become `- [ ]`
   checklist lines in the parent Sonto task's notes; never recreated as Todoist tasks.
4. **Week-scheduled tasks → Todoist due date on the FIRST DAY OF THE WEEK per system locale**
   (Monday in NL, Sunday in US — `config.locale_first_weekday()`) **+ a `sonto-week-YYYY-WW`
   marker label** which is the round-trip source of truth (returns to a Sonto *week*, not a day).

## Confirmed environment (verified live)

- **Sonto MCP**: `http://127.0.0.1:2402/` (JSON-RPC at root; `/mcp` alias). Localhost only,
  built into the Mac app `net.map-territory.Space`.
- **Auth**: `Authorization: Bearer <access_token>`; tokens at
  `~/Library/Application Support/net.map-territory.Space/mcpb_proxy_tokens.json`
  (`client_id, access_token, refresh_token, expires_at, port`). **On-disk token already
  expires** → refresh is the normal path. Non-interactive refresh works: OAuth discovery at
  `/.well-known/oauth-authorization-server`, `token_endpoint = /oauth/token`, `refresh_token`
  grant (PKCE). Clean errors: 401 `invalid_token` → refresh; token endpoint 400 `invalid_grant`
  → refresh token dead → flag `needs_repair`, alert, exit (human re-pairs).
- **Sonto tool surface** (names known; **NO param schemas documented — `tools/list` at
  runtime**): `list_areas/add_area/edit_area/delete_area/get_area`,
  `list_projects/add_project/edit_project/delete_project/get_project`,
  `list_groups/add_group/edit_group/delete_group/sort_groups`,
  `add_task/edit_task/delete_task/bulk_edit_tasks/bulk_delete_tasks/delete_completed_todos/
  sort_tasks/search_todos/get_inbox`, `get_day/edit_day/get_day_events/schedule_task_event`,
  `get_week/edit_week`, `add_long_term_plan/…`. **Tag tools and project-deadline params are
  UNVERIFIED** (doc predates Sonto 1.6 Tags) — discover via `tools/list`, degrade gracefully.
- **Sonto model**: Area → Project → Group → Task, plus a global Inbox. "Group" = the
  heading/section analogue (no separate heading entity). Tasks: title, rich-text notes
  (sub-tasks are checklist lines in notes), an **`important` boolean** (not numeric priority),
  backdateable completion, multiple tags. **Scheduling is a granularity ladder** (unscheduled
  → ISO-week → day → timed event), *not* due/deadline. Per-task deadlines don't exist
  (deadline is a Project-level field).
- **Todoist** = unified **API v1** (`https://api.todoist.com/api/v1`; REST v2 & Sync v9 dead
  since Feb 2026). String IDs. `POST /sync` with `sync_token` for incremental change detection
  (+`is_deleted` tombstones); `GET /tasks/completed/by_completion_date` for instances (~92-day
  window); batched `/sync` `commands` (≤100/req) with client `uuid` (idempotency) + `temp_id`
  (parent+children in one batch). `priority` **1–4 inverted** (4=highest). `labels` on **tasks
  only**. Rate limit ~1000/15 min.
- **Runtime**: Python 3.14, **stdlib only** (no deps), `sqlite3` for the DB. macOS / launchd.

## Entity & field mapping

| Sonto | Todoist | Direction |
|---|---|---|
| Area | top-level project | two-way |
| Project (in area) | sub-project (`parent_id`=area's project) | two-way; area change = `project_move` |
| Project (area-less) | top-level project | two-way |
| Group ("heading") | section | two-way |
| Task | task | two-way |
| Inbox | Inbox project | two-way |
| Tag (on task) | label | two-way if MCP exposes tag params; else ignored (logged) |
| Tag (on project/area) | — (labels are task-only) | not mirrored; kept in Sonto |

### Impedance handling

| Concept | Behavior |
|---|---|
| Day-schedule | ↔ Todoist `due.date` |
| Week-schedule | → due `= first day of week per locale` + `sonto-week-YYYY-WW` label (label = round-trip truth) |
| Unscheduled | ↔ no `due` |
| Timed event | → `due.datetime`; event richness (duration/calendar) is Sonto-only, dropped on Todoist |
| Priority | `important=true` ↔ P1 (api 4); `false` ↔ P4 (api 1). Lossy 4→2 |
| Sub-tasks | Todoist sub-tasks → flattened `- [ ]` in parent Sonto notes, one-way |
| Project deadline | DB-noted only; no Todoist home; not synced unless opted in |
| Completion | two-way; pull Todoist completions via `by_completion_date`; recurring → instance only |
| Notes rich text | best-effort plain/Markdown two-way; hash on normalized text |
| In-progress/late, sort order | Sonto-only; dropped + excluded from hash |

## Architecture

- `config.py` — paths, endpoints, mapping-policy flags, locale first-weekday.
- `transport.py` — stdlib HTTP helper (plain JSON/form; non-raising on 4xx/5xx).
- `store.py` — SQLite: `id_map`, `state`, `sonto_snapshot`, `applied_commands`, `run_lock`.
- `model.py` — `NormalizedEntity` + `canonical_hash` (echo guard).
- `mcp_client.py` — Sonto MCP: OAuth refresh + Streamable-HTTP JSON-RPC (initialize →
  notifications/initialized → tools/list → tools/call), JSON + SSE handling, re-init each run.
- `todoist_client.py` — `/sync` read, completed read, batched command writes (uuid/temp_id).
- `sonto.py` — semantic Sonto ops over `mcp_client`, adapting to runtime `inputSchema`.
- `mapping.py` — entity/field translation + impedance policy.
- `reconcile.py` — engine: classify → resolve (LWW, delete-wins) → topo-order → apply →
  write-back; all DB state committed in one transaction with `sync_token`.
- `adopt.py` — first-run match-existing (no duplicate creation).
- `introspect.py` — P0 truth-finder: real Sonto tool schemas + sample payloads; Todoist probe.
- `main.py` — CLI: `--once`, `--dry-run`, `--status`, `--introspect`, run-lock, logging.

### SQLite schema
`id_map(entity_type, sonto_id, todoist_id TEXT, sonto_updated, todoist_updated,
last_synced_hash, sonto_hash, todoist_hash, last_synced_at, deleted)` +
`state(key,value)` + `sonto_snapshot(entity_type, sonto_id, hash, payload, seen_at)` +
`applied_commands(uuid PK, command, status, applied_at)` + `run_lock(single row)`.

### Reconcile loop
1. Lock; ensure Sonto token (refresh; `invalid_grant` → `needs_repair`, exit).
2. Pull Todoist (`/sync` token + completed window). Pull Sonto (full snapshot via `list_*` /
   `get_inbox` / `search_todos` + bounded `get_day`/`get_week` for date placement).
3. Normalize + hash; join to `id_map`.
4. Classify create/update/delete per side. **Empty-read sanity floor**: a failed/empty Sonto
   read is NOT a mass delete.
5. Resolve: one side changed → propagate; both → strict LWW (Sonto ts if available, else
   Todoist-wins); delete-wins.
6. Topo-order (areas→projects→groups→tasks; reverse for deletes).
7. Apply (Todoist batched `/sync`, record `uuid` before send; Sonto per-call).
8. Write-back ids + `last_synced_hash` + snapshot + new `sync_token` in ONE transaction.

## Safety
`--dry-run` default for first runs; run-lock; atomic locked token rewrite; empty-read sanity
floor; first-run adopt/match; idempotent `uuid`s; bootstrap ladder
(`readonly → oneway_sonto_to_todoist → oneway_with_deletes → twoway`); deletes into Sonto
gated behind `ALLOW_SONTO_DELETES` even in twoway.

## Phased build order
- **P0** — clients + OAuth refresh + `tools/list` introspection + read-only snapshot (current).
- **P1** — one-way Sonto→Todoist structure (areas/projects/groups) + adopt.
- **P2** — tasks (one-way): content, notes, important→P1, day-schedule→due, Inbox.
- **P3** — reverse direction + echo-suppression + strict-LWW conflicts.
- **P4** — tags, completion (incl. `by_completion_date`), week-schedule + marker label,
  sub-task flatten, deletes (with sanity floor), recurring-as-instance.
- **P5** — launchd cron, log rotation, `needs_repair` notification, `--status`.

## Verification
Scratch Todoist account for P1–P4; introspect before assuming schemas; **idempotency gate**
(reconcile twice → second run emits zero commands); round-trip tests per lossy mapping;
failure injection (empty read → no deletes; `invalid_grant` → clean exit; kill mid-apply →
clean replay).

## Open items resolved at P0 (not blockers)
Exact Sonto tool param schemas; whether Sonto exposes a modified timestamp (decides strict-LWW
vs fallback); ID format/stability; tag + project-deadline param support; Sonto refresh-token
rotation policy; official `td` CLI real capability surface.
