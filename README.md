# sonto-todoist-sync

Two-way sync between [**Sonto**](https://apps.apple.com/us/app/space-goal-life-planner/id6502294348) (a goal & life planner for Mac and iOS) and
**Todoist**, designed to run every 15 minutes and keep both as close to identical as possible.
Zero runtime dependencies — standard library only (Python 3.11+); no venv or pip.

Under the hood: the Sonto side is driven through the app's built-in **MCP server**
(`http://127.0.0.1:2402/`, called directly as a JSON-RPC API — no LLM); the Todoist side uses
the **API v1**; and a local **SQLite** layer maps IDs across the two and detects changes.
Internals are documented in `docs/PLAN.md` and `build-log.md`.

## Setup

1. **Todoist token** — create `.secrets/todoist-token.json`:

   ```json
   {"access_token": "YOUR_TOKEN"}
   ```

   Get the token from Todoist → Settings → Integrations → Developer. (Or export `TODOIST_API_TOKEN`.)

2. **Sonto** — open the Sonto Mac app and enable its MCP server (Settings → AI). The sync reads
   and auto-refreshes the app's own token; nothing else to configure.

That's it — no venv, no pip, no other env vars.

## Usage (run it manually)

```
cd ~/code/sonto-todoist-sync
python3 run.py --status          # phase, last run, token health
python3 run.py --once --dry-run  # preview what it would change (writes nothing)
python3 run.py --once            # sync now — applies changes, both directions
```

Each run needs the **Sonto Mac app open with its MCP server enabled** (the sync talks to
`127.0.0.1:2402`) and a network connection (for Todoist). If Sonto's refresh token ever dies,
the run exits with a `needs_repair` note — re-pair the MCP in Sonto settings.

## Run automatically every 15 minutes (launchd)

1. Copy the template (in `deploy/`) and fill in your paths:

   `deploy/net.map-territory.sonto-todoist-sync.plist.example` →
   `~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist`

   Set the absolute paths to `python3` (e.g. `/opt/homebrew/bin/python3`) and `run.py`.
   `StartInterval` is `900` (15 min); logs go to `logs/sync.out` / `logs/sync.err`.

2. Load it (`logs/` must exist):

   ```
   mkdir -p logs
   launchctl load ~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist
   launchctl start net.map-territory.sonto-todoist-sync   # run once now
   tail -f logs/sync.err                                  # watch
   ```

3. To stop: `launchctl unload ~/Library/LaunchAgents/net.map-territory.sonto-todoist-sync.plist`.

The Sonto app must be running with its MCP server enabled for each scheduled run.

## ⚠️ Warning: Todoist ↔ Sonto mapping is NOT 1:1

Sonto and Todoist model planning differently. The sync does its best, but some things are
**lossy, one-directional, or deliberately dropped**. Know these before trusting it blindly:

- **Containers (Areas vs Projects).** Sonto separates **Areas** (never-ending life categories)
  from **Projects** (bounded). Todoist has one generic, nestable "project". Mapping:
  - Sonto → Todoist: Area → top-level project, Project → sub-project, Group → section.
  - Todoist → Sonto: a top-level project **with** sub-projects → **Area**; otherwise → **Project**;
    sub-projects → Projects; sections → Groups.
  - Consequence: a project's *kind* can change across a round-trip if its hierarchy changes.
- **Scheduling (ladder vs due date).** Sonto schedules on a granularity ladder
  (unscheduled → week → day → timed event); Todoist has a single **due date** plus a separate
  **deadline**.
  - Day → Todoist due date (clean).
  - **Week → Todoist due date on the first day of that week (per your system locale: Monday in
    NL, Sunday in US) + a `sonto-week-YYYY-WW` label.** The label is the round-trip source of
    truth; the date just makes it usable in Todoist.
  - Timed calendar events → Todoist due *datetime*; the event's duration / calendar / event-ness
    is Sonto-only and **dropped**.
  - **Todoist task `deadline` has no Sonto equivalent** (Sonto deadlines are project-level only)
    → dropped.
- **Priority (boolean vs 4 levels).** Sonto `important` is on/off; Todoist is P1–P4.
  `important` ↔ **P1**; P2/P3/P4 collapse to *not important* coming back (lossy 4 → 2).
- **Sub-tasks.** Todoist has real nested sub-tasks; Sonto has none (sub-items are `[ ]`/`[x]`
  checklist lines inside a task's notes). **Todoist sub-tasks are NOT synced.**
- **Tags vs labels.** Sonto tags attach to tasks, projects **and** areas; Todoist labels attach
  to **tasks only**. Task tags ↔ labels works; project/area tags are **not mirrored**. A new
  Todoist label can't create a Sonto tag (no create-tag MCP tool) — only *existing* Sonto tags
  are settable from Todoist. The `sonto-week-…` label is internal plumbing.
- **Notes formatting.** Sonto rich text ↔ Todoist Markdown, best-effort. Todoist auto-links bare
  URLs into `[title](url)`; that's cosmetic and normalized so it doesn't ping-pong.
- **Completion & recurring.** Completion syncs both ways. **Recurring tasks aren't modeled in
  Sonto** — only the current instance syncs; Todoist owns the recurrence.
- **Conflicts.** If the same task is edited on both sides between runs, **Todoist wins** (Sonto
  exposes no per-item modified timestamp).
- **Deletes.** Propagate **both ways** by default — deleting a synced task in either app deletes
  it in the other. (Guard: if the Todoist completed-items read is incomplete, deletes into Sonto
  are skipped that run, so a completed task is never mistaken for a deleted one.)
- **Re-filing.** Moving a synced task between containers propagates Sonto → Todoist; the
  Todoist → Sonto direction updates the task's fields but doesn't yet re-file it (edge case).
- **Sonto-only states.** The in-progress toggle, the "late" indicator, and manual ordering are
  Sonto-only and not represented in Todoist.
