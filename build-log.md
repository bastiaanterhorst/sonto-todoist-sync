# Build Log

A running, detailed record of progress so any agent (or future me) can reconstruct the state
of this project, understand why things are the way they are, and pick up where it was left.
Newest entries at the bottom of each section. See `docs/PLAN.md` for the full design and
`README.md` for the quick orientation.

## What this project is

A zero-dependency Python tool that two-way-syncs **Sonto** (planner app, formerly "Space") and
**Todoist**, intended to run every 15 minutes via launchd. Sonto is reached through its
built-in MCP server, called directly as a JSON-RPC HTTP API at `http://127.0.0.1:2402/`
(no LLM). Todoist uses the unified API v1. A SQLite database maps IDs across the two systems
and detects/reconciles changes. Scope: tasks, areas, projects, groups ("headings"), tags.
Recurring tasks: instances only (Todoist owns recurrence).

## Key decisions (and why)

- **New standalone repo** `~/code/sonto-todoist-sync/`, git on `main`, commit incrementally.
  (Originally planned inside `space-content`; moved out at the user's request.)
- **Zero runtime dependencies — stdlib only.** A cron job should "just run" without a venv or
  pip. HTTP via `urllib`/`http.client`, DB via `sqlite3`, hashing via `hashlib`.
- **Todoist via direct API v1**, not a CLI. The official `Doist/todoist-cli` and third-party
  `sachaos/todoist` lack JSON output, `updated_at`, the `/sync` incremental token, and
  completed-task access — all required for robust two-way sync.
- **Conflict resolution: strict last-write-wins** by modified timestamp; fall back to
  Todoist-wins if Sonto exposes no reliable per-item timestamp (TBD at introspection).
- **Sub-tasks: flatten one-way** (Todoist sub-tasks → `- [ ]` lines in parent Sonto notes).
- **Week-scheduled tasks: Todoist due = first day of that week per system locale** (Monday in
  NL, Sunday in US, via `config.locale_first_weekday()`) **+ `sonto-week-YYYY-WW` marker label**
  as the round-trip source of truth.
- **Safety-first bootstrap ladder** (`readonly → oneway_sonto_to_todoist →
  oneway_with_deletes → twoway`); deletes into Sonto stay gated behind `ALLOW_SONTO_DELETES`.

## Environment facts (verified live during planning)

- Sonto MCP listens on `127.0.0.1:2402`; JSON-RPC at `/`. 401 without bearer.
- Sonto tokens: `~/Library/Application Support/net.map-territory.Space/mcpb_proxy_tokens.json`
  (`client_id, access_token, refresh_token, expires_at, port`). **On-disk token is expired** →
  refresh is the normal path. OAuth discovery at `/.well-known/oauth-authorization-server`;
  `token_endpoint = http://127.0.0.1:2402/oauth/token`; `refresh_token` grant works.
- Todoist API v1 base `https://api.todoist.com/api/v1`; `/sync` (token), completed endpoint,
  batched commands with `uuid`/`temp_id`. Priority 1–4 inverted (4 = highest). Labels on tasks
  only.
- Tool param schemas for Sonto are NOT documented anywhere → must `tools/list` at runtime.
  Tag tools and project-deadline params are unverified (predate Sonto 1.6).

## Phase status

- **P0 — plumbing + introspection: COMPLETE.** Live introspection run; Todoist verified.
- **P1 — one-way Sonto→Todoist structure: APPLIED to real Todoist & idempotent.**
  Phase set to `oneway_sonto_to_todoist`; `python run.py --once` created 4 projects + 11
  sections. **Idempotency verified**: an immediate re-run = 15 matched / 0 creates / 0 applied.
  Live Todoist now mirrors Sonto: M×T (4 sections), Personal (1), Sonto (6), Plans (empty);
  pre-existing Inbox/Privé/Stevinstraat left untouched (one-way, no deletes). `--dry-run`
  still previews; matching is name-within-parent so it re-discovers applied objects.
- **P2 — tasks (one-way Sonto→Todoist): APPLIED to real Todoist & idempotent.**
  Created 47 incomplete tasks (25 sections / 2 project-root / 20 Inbox). Re-run = 0 creates.
  Verified live: 60 tasks total (13 pre-existing untouched + 47 new), 22 with due dates, the 4
  week tasks carry `sonto-week-2026-W26` + due Mon 2026-06-22. important→P1, notes→description,
  tags→labels all correct. Completed tasks excluded (P4). **Current capability: a one-way,
  CREATE-only Sonto→Todoist mirror of structure + incomplete tasks, idempotent & safe to repeat.**
  Not yet: task UPDATES (P2b), reverse direction (P3), completion/deletes/recurring (P4).
- **P2b/P3/P4 — two-way tasks: DONE & live & idempotent.** Unified reconcile state machine.
  Forward (Sonto→Todoist): create/update/complete/delete. Reverse (Todoist→Sonto):
  create/update/complete for tasks with a Sonto home (Todoist Inbox or mapped project/section).
  Conflict = strict-LWW → Todoist-wins. Completion both ways (Todoist completed-items endpoint).
  Verified live: forward+reverse settle to zero; a Todoist priority edit propagated to Sonto
  `isImportant` and reverted. Phase = `twoway`, `ALLOW_SONTO_WRITES=True`.
  - **Gated / deferred (documented):** deletes INTO Sonto (`ALLOW_SONTO_DELETES=False`);
    reverse structure creation (Todoist-only projects → Sonto areas — those tasks are left in
    Todoist, not dumped into Sonto Inbox); sub-task flattening; tags-on-projects/areas reverse
    (Sonto has no create-tag MCP tool, so only existing tags are settable).
- **P5 — launchd: documented, NOT installed** (per instruction). See README "Scheduling" +
  `deploy/net.map-territory.sonto-todoist-sync.plist.example`.

## GOTCHA: Sonto entity IDs are per-read-unstable (must decode)

Sonto **area/project/group** IDs (`areaID`/`projectID`/`groupID`) are base64 Core-Data object
tokens whose RAW STRING CHANGES ON EVERY READ (non-deterministic JSON encoding) but which
decode to a stable `uriRepresentation` (e.g. `x-coredata://STORE/Area/p8`). **Always key the
id_map on `sonto.stable_id(token)`, never the raw token.** Task IDs (`todoUUID`) ARE stable
UUIDs — do not decode those. This bug first showed as duplicated id_map rows (2×) + tasks
falling to Inbox; fixed in `sonto.stable_id` + used in `snapshot_structure` and
`_resolve_placement`. Also: a Sonto project's area membership is only correct once stable ids
are used — which revealed the `Sonto` project actually lives in the `M×T` area (P1 had created
it top-level), handled by a `project_move` (see `adopt.Plan.moves` / `reconcile._apply_moves`).

## Introspection findings (P0, 2026-06-26) — verified live

Server `Space - Life Planner` v1.6.1; **36 tools**. Full schemas committed at
`docs/sonto-mcp-tools.md`; full dump incl. real sample data is in gitignored
`data/sonto-tools.json`. Key facts the engine relies on:

- **IDs**: tasks → clean `todoUUID` (e.g. `41F182E3-...`); areas/projects → opaque base64
  Core-Data tokens (`areaID`/`projectID`); tags → tag UUIDs. All stable; use as map keys.
- **Scheduling (on `add_task`/`edit_task`)**: day → `scheduled_day_iso`/`day_iso` (YYYY-MM-DD);
  week → `scheduled_week` + `scheduled_week_year` (ISO week); `unschedule` clears. Confirms the
  granularity ladder and how to set it. `add_task` needs `context_type`
  (inbox/day/week/project/area) + `name`.
- **Tags are first-class**: `list_tags`, `get_tag_todos`, and `set_tags`/`add_tags`/`remove_tags`
  (tag UUIDs) on task/project/area tools. So task↔label sync is fully supported; project/area
  tags exist too (still no Todoist home → Sonto-only).
- **Sub-tasks**: `notes` is markdown where `[]`/`[x]` are (completed) sub-task checklist items —
  exactly the flatten target for Todoist sub-tasks. `*italic*`/`**bold**` supported.
- **NO per-item modified timestamp** on Sonto tasks (fields: completed, hasLinkedEvent,
  inProgress, isImportant, isLate, name, notes, projectIsPlan, section, tags, todoUUID).
  → strict-LWW uses the **Todoist-wins fallback** on both-changed conflicts (`config
  .LWW_FALLBACK_WINNER="todoist"`). Sonto-side change detection = hash-diff vs snapshot.
- **`list_groups`/`get_project`/`get_area` need a parent id** (not global). `list_projects`
  takes optional `area_id`/`include_completed`. Projects carry **`isPlan`** (yearly/quarterly
  plans) → exclude plans from sync.
- **Todoist** verified: token valid; full sync returns sync_token + items with `updated_at`,
  `due`, `deadline`, `priority`, `labels`, `parent_id`, `project_id`, `section_id`, `checked`,
  `completed_at`, `is_deleted`. (Todoist `deadline` is task-level; Sonto has no per-task
  deadline → drop on the Sonto side.)

## File status

| File | Status | Notes |
|---|---|---|
| `.gitignore`, `README.md` | done | secrets/data/logs ignored |
| `docs/PLAN.md` | done | full design (in-repo copy) |
| `docs/sonto-mcp-tools.md` | done | real MCP tool schemas (from live introspection; no personal data) |
| `build-log.md` | done | this file; keep updated |
| `syncer/config.py` | done | paths, endpoints, policy flags, `locale_first_weekday()` |
| `syncer/transport.py` | done | stdlib HTTP helper, non-raising on 4xx/5xx |
| `syncer/store.py` | done | SQLite: id_map/state/snapshot/applied_commands/run_lock |
| `syncer/model.py` | done | `NormalizedEntity` + `canonical_hash` (echo guard) |
| `syncer/mcp_client.py` | done | OAuth refresh + Streamable-HTTP JSON-RPC (JSON+SSE), session, re-init |
| `syncer/todoist_client.py` | done | /sync read, completed read, batched writes (uuid/temp_id) |
| `syncer/introspect.py` | done | P0 truth-finder: tools/list dump + read-only sample payloads |
| `syncer/main.py`, `__main__.py`, `run.py` | done | CLI: --introspect/--once/--status/--set-phase, run-lock |
| `syncer/mapping.py` | done (P1/P2) | week/priority/structure + task helpers (`task_item_args`, due/labels) |
| `syncer/sonto.py` | done (P1/P2) | reads, `stable_id`, `snapshot_structure`, `snapshot_tasks` |
| `syncer/adopt.py` | done (P1) | structure matcher + ordered create plan + `moves` (re-parent) |
| `syncer/reconcile.py` | partial | structure pass (create+move) + P2 task create pass; updates/deletes = P3/P4 |

## How to run (target)

```
python run.py --introspect      # connect to Sonto MCP, dump real tools + schemas
python run.py --once --dry-run  # snapshot both sides, print would-be changes
python run.py --status          # last run, token health, pending conflicts
```

## Changelog

### 2026-06-26
- Repo created, `git init -b main`. Python 3.14.4, stdlib-only design chosen.
- Wrote scaffold: `.gitignore`, `README.md`, `docs/PLAN.md`, `build-log.md`.
- Wrote `config.py` (incl. locale-aware first-day-of-week: macOS `AppleFirstWeekday` →
  glibc `FIRST_WEEKDAY` → region heuristic → Monday; env override `SYNC_WEEK_FIRST_DAY`),
  `transport.py`, `store.py` (full schema), `model.py` (canonical hashing).
- Wrote `mcp_client.py` (OAuth `refresh_token` grant + atomic locked token rewrite; MCP
  Streamable-HTTP: initialize → notifications/initialized → tools/list → tools/call; handles
  `application/json` and `text/event-stream`; 401 → refresh+retry; `invalid_grant` →
  `NeedsRepair`), `todoist_client.py` (API v1 `/sync` read + batched command writes with
  `uuid`/`temp_id` + completed-by-completion-date), `introspect.py` (read-only tool-schema
  dump + sample payloads → `data/sonto-tools.json`), `mapping.py` (week label/due-date +
  priority helpers), `sonto.py` (read wrappers), `main.py`/`run.py` CLI, and `reconcile.py`/
  `adopt.py` documented skeletons.
- Smoke tests pass: compile + imports clean; week_due_date(Fri 2026-06-26)=Mon 2026-06-22;
  `--status` reads token health (confirms **Sonto token expired → will refresh**, **Todoist
  token missing**). No live network calls or token mutation performed yet.
- **BLOCKED on two human-gated inputs before live `--introspect`/`--once`:**
  1. A **Todoist API token** (`.secrets/todoist-token.json` or `TODOIST_API_TOKEN`).
  2. OK to **refresh the Sonto MCP token live** — the access token is expired, so the first
     real call performs an OAuth `refresh_token` against `127.0.0.1:2402/oauth/token` and
     rewrites the shared `mcpb_proxy_tokens.json` (also used by Claude Desktop). Behaviour is
     standard OAuth refresh, but it mutates a shared file — confirm before running.
- Ran `python run.py --introspect` live (token refreshed OK). Captured 36 real tools +
  schemas → `docs/sonto-mcp-tools.md`; samples → gitignored `data/sonto-tools.json`. See
  "Introspection findings" above. Verified Todoist token (real account: 3 projects, 5 sections,
  13 tasks, 1 label) and confirmed `updated_at`/`due`/`deadline`/`labels` item fields.
- **P0 complete.** All design unknowns resolved.
- Decision: write target = **dry-run on real account, then careful apply via the bootstrap
  ladder** (no scratch account).
- Built **P1 structure mirror**: `sonto.snapshot_structure()` (areas, non-plan projects with
  area linkage, groups), `mapping` structure helpers, `adopt.match_structure()` (name-based
  match within parent + ordered create plan with temp_ids), `reconcile` structure pass
  (dry-run report + gated batched apply via Todoist `project_add`/`section_add` with temp_ids,
  then id_map seeding + sync_token persisted in one transaction).
- Verified live dry-run, then **APPLIED** (user chose "apply now"): 15 creates succeeded;
  re-run proved idempotent (0 creates). Todoist structure confirmed via live read (7 projects /
  16 sections incl. pre-existing). id_map seeded (in gitignored `data/sync.db`).
### 2026-06-26 (cont.) — P2

- Built P2 task sync: `sonto.snapshot_tasks()` (merges container reads + get_day/get_week
  horizon; incomplete only), `mapping.task_item_args`/`task_due_and_labels`/`week_to_due_date`,
  `reconcile._task_pass` (create unmapped tasks, batched `item_add` w/ todoUUID temp_ids,
  id_map seed). Integrated into `run()` after the structure pass.
- **Hit + fixed the unstable-id bug** (see GOTCHA above): added `sonto.stable_id`, re-keyed the
  map, added `project_move` handling for the Sonto-under-M×T correction. Refactored structure
  apply into `_seed_matched` (always; non-destructive) + `_create_structure` + `_apply_moves`.
- Applied the structure correction to Todoist (Sonto is now a sub-project of M×T; 15 mapped).
- **P2 task creates APPLIED** (user approved): 47 tasks created, idempotent re-run verified,
  Todoist state confirmed (60 total, week labels + Monday due correct, originals untouched).
### 2026-06-26 (cont.) — P2b/P3/P4 two-way + P5 docs

- Replaced the one-way task pass with a **unified two-way reconcile** (`_reconcile_tasks`):
  classifies every id_map task pair + unmapped tasks into fwd/rev create/update/complete/delete
  + conflict; applies forward live, reverse in `twoway` (gated by `ALLOW_SONTO_WRITES`).
- **Echo-guard fixes** (were false-diffing every run): Todoist Inbox detected via `inbox_project`
  (not `is_inbox_project`) and normalized to `"inbox"` on both sides; markdown links
  `[title](url)` collapsed to `url` when hashing (Todoist auto-links bare URLs in descriptions).
  After these, forward settles to 0 changes on re-run.
- Completion: pull Todoist completed-items (`by_completion_date`) so a done task reads as
  completed, not deleted. Empty-read sanity floor guards mass deletes both directions.
- Reverse primitives validated on a throwaway task: `add_task` returns `{todo}` (uuid via
  `extract_todo_uuid`), `edit_task`, `complete_task`, `delete_task` (needs
  `confirm_destructive=true`, else the SSE read hangs).
- **Reverse home filter**: only reverse-sync Todoist tasks in the Todoist Inbox or a mapped
  project/section. The 13 pre-existing Privé/Stevinstraat tasks (unmapped projects) are left
  in Todoist — avoids Inbox-dumping + a placement ping-pong.
- Enabled `twoway` + `ALLOW_SONTO_WRITES`; reverse-created the 1 Todoist-Inbox task into Sonto
  (correctly day-scheduled from its due date); verified a live Todoist→Sonto priority round-trip
  and full idempotency.
- **P5**: documented launchd setup in README + added `deploy/*.plist.example`; **did not install
  the job** (per instruction).
- Open/next (not blocking a working two-way sync): reverse structure creation; sub-task
  flatten; tags on projects/areas reverse; enabling `ALLOW_SONTO_DELETES` after the
  tombstone/sanity-floor paths get more real-world soak; switching reads to the incremental
  Todoist `sync_token` (currently full-sync each run — fine at this scale).

### (superseded) earlier P2 note
- NEXT: **P2 — tasks (one-way Sonto→Todoist).** For each Sonto task: content=name,
  notes (flatten any Todoist-only []/[x] later; for S→T just pass notes), important→priority,
  day-schedule→`due.date`, week-schedule→`due` on locale-first-day + `sonto-week-YYYY-WW`
  label, Inbox→Inbox, placement into the mirrored project/section using the todo's
  areaID/projectID/groupID/section from Sonto reads (get_inbox/get_project/get_area/get_day/
  get_week/search_todos). Then P3 reverse + conflicts, P4 tags/completion/deletes, P5 cron.
  Note: completion + recurring-instance handling and tag→label come with P2/P4.
