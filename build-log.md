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

- **P0 — plumbing + introspection: IN PROGRESS.**
- P1 one-way structure, P2 tasks, P3 reverse + conflicts, P4 hard mappings, P5 cron: not started.

## File status

| File | Status | Notes |
|---|---|---|
| `.gitignore`, `README.md` | done | secrets/data/logs ignored |
| `docs/PLAN.md` | done | full design (in-repo copy) |
| `build-log.md` | done | this file; keep updated |
| `syncer/config.py` | done | paths, endpoints, policy flags, `locale_first_weekday()` |
| `syncer/transport.py` | done | stdlib HTTP helper, non-raising on 4xx/5xx |
| `syncer/store.py` | done | SQLite: id_map/state/snapshot/applied_commands/run_lock |
| `syncer/model.py` | done | `NormalizedEntity` + `canonical_hash` (echo guard) |
| `syncer/mcp_client.py` | done | OAuth refresh + Streamable-HTTP JSON-RPC (JSON+SSE), session, re-init |
| `syncer/todoist_client.py` | done | /sync read, completed read, batched writes (uuid/temp_id) |
| `syncer/introspect.py` | done | P0 truth-finder: tools/list dump + read-only sample payloads |
| `syncer/main.py`, `__main__.py`, `run.py` | done | CLI: --introspect/--once/--status/--set-phase, run-lock |
| `syncer/mapping.py` | partial | week + priority helpers done; entity translation = P1+ |
| `syncer/sonto.py` | partial | read wrappers done; write methods = P1+ |
| `syncer/reconcile.py`, `adopt.py` | stub | engine + adopt land P1+ (documented skeletons) |

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
- NEXT (P0 finish): run `python run.py --introspect` live to capture real Sonto tool schemas
  (resolves: per-item timestamp? id format? tag tools? add_task arg shape?) → then P1.
