"""Central configuration: paths, endpoints, and mapping-policy flags.

Anything a human might reasonably want to tune lives here. Nothing here is secret —
secrets live in `.secrets/` (gitignored) and in the Sonto app's own token store.
"""

from __future__ import annotations

import datetime as _dt
import locale as _locale
import os
import re
import subprocess
from pathlib import Path

# --- Paths -----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / ".secrets"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "sync.db"
LOG_DIR = REPO_ROOT / "logs"

# Sonto's MCP-bundle proxy stores its OAuth tokens here (shared with Claude Desktop).
SONTO_TOKENS_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "net.map-territory.Space"
    / "mcpb_proxy_tokens.json"
)

# Todoist personal API token (file form mirrors the space-content `.secrets/*.json` pattern).
TODOIST_TOKEN_PATH = SECRETS_DIR / "todoist-token.json"

# --- Sonto MCP -------------------------------------------------------------
SONTO_MCP_HOST = "127.0.0.1"
SONTO_MCP_DEFAULT_PORT = 2402  # overridden by the `port` field in the tokens file
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_CLIENT_NAME = "sonto-todoist-sync"


def sonto_base_url(port: int) -> str:
    return f"http://{SONTO_MCP_HOST}:{port}"


def sonto_mcp_url(port: int) -> str:
    # JSON-RPC endpoint is the root path (verified); `/mcp` is an accepted alias.
    return f"{sonto_base_url(port)}/"


def sonto_oauth_token_url(port: int) -> str:
    return f"{sonto_base_url(port)}/oauth/token"


# Refresh the access token if it expires within this many seconds (proactive).
TOKEN_REFRESH_SKEW_SECONDS = 120

# --- Todoist API v1 --------------------------------------------------------
TODOIST_API_BASE = "https://api.todoist.com/api/v1"
TODOIST_SYNC_URL = f"{TODOIST_API_BASE}/sync"
TODOIST_COMPLETED_BY_COMPLETION = f"{TODOIST_API_BASE}/tasks/completed/by_completion_date"
TODOIST_RESOURCE_TYPES = ["projects", "sections", "items", "labels"]
TODOIST_MAX_COMMANDS_PER_BATCH = 100

# --- Bootstrap ladder ------------------------------------------------------
# Gates how destructive the engine is allowed to be. Stored in the `state` table.
PHASE_READONLY = "readonly"
PHASE_ONEWAY_S2T = "oneway_sonto_to_todoist"
PHASE_ONEWAY_S2T_DELETES = "oneway_with_deletes"
PHASE_TWOWAY = "twoway"
BOOTSTRAP_PHASES = [PHASE_READONLY, PHASE_ONEWAY_S2T, PHASE_ONEWAY_S2T_DELETES, PHASE_TWOWAY]
DEFAULT_PHASE = PHASE_READONLY

# --- Mapping policy (see docs/PLAN.md) ------------------------------------
# Week-scheduled Sonto task -> Todoist due on the FIRST DAY of that week per the system
# locale (Monday in NL, Sunday in US) + a marker label that is the round-trip source of truth.
# The weekday is resolved at runtime by locale_first_weekday(); override with this env var.
WEEK_FIRST_DAY_ENV = "SYNC_WEEK_FIRST_DAY"  # "0".."6" (Mon..Sun) or a name like "sunday"
WEEK_LABEL_PREFIX = "sonto-week-"  # e.g. "sonto-week-2026-W27"

# Priority: Sonto `important` boolean <-> Todoist 1..4 (4 = highest/P1).
TODOIST_PRIORITY_IMPORTANT = 4
TODOIST_PRIORITY_NORMAL = 1

# Conflict resolution: strict last-write-wins by modified timestamp. If Sonto does not
# expose a reliable per-item timestamp (resolved at introspection), fall back to this side.
LWW_FALLBACK_WINNER = "todoist"  # "todoist" | "sonto"

# Safety: deleting from the user's real planner is the highest-blast-radius action.
ALLOW_SONTO_DELETES = False

# Reverse writes (Todoist -> Sonto: create/edit/complete tasks in the planner) — required for
# genuine two-way sync, so ON in the `twoway` phase. Only tasks with a real Sonto home are
# reverse-synced (Todoist Inbox + mapped projects/sections); Todoist-only-project tasks are not.
# Deletes into Sonto remain separately gated (ALLOW_SONTO_DELETES).
ALLOW_SONTO_WRITES = True

# Only learn/sync date-ladder placement within a rolling horizon (cheap + low-churn).
# Filed tasks are read fully from their container; purely-scheduled tasks (no project/area)
# are discovered from get_day/get_week over this horizon. Overdue day tasks are caught via
# include_late on `today`, so we don't scan far into the past.
CALENDAR_HORIZON_DAYS = 14
DAY_HORIZON_FUTURE_DAYS = 45   # get_day(today..+N)
WEEK_HORIZON_FUTURE = 6        # get_week(current..+N)

# P2 (one-way Sonto->Todoist) mirrors INCOMPLETE tasks only. Completed/recurring handling is P4.
SYNC_COMPLETED_TASKS = False

# If a Sonto list_* read comes back empty/errored, never interpret it as a mass delete.
EMPTY_READ_SANITY_FLOOR = True

# --- Misc ------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS = 30
RUN_LOCK_STALE_SECONDS = 1800  # steal the lock if a prior run's heartbeat is older than this


def env_override(name: str, default):
    """Allow any uppercase config constant to be overridden by an env var (string)."""
    return os.environ.get(name, default)


# --- Locale: first day of the week ----------------------------------------
# Sonto week-scheduled tasks get a Todoist due date on the first day of the relevant week.
# "First day" follows the system locale: Monday in NL, Sunday in the US, etc.
_SUNDAY_FIRST_REGIONS = {
    "US", "CA", "JP", "CN", "KR", "IN", "IL", "ZA", "BR", "MX",
    "PH", "TW", "HK", "CO", "AR", "VE", "PE", "DO", "EG", "SA",
}
_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _apple_first_weekday() -> int | None:
    """macOS AppleFirstWeekday (Apple numbering Sun=1..Sat=7) -> Python weekday (Mon=0..Sun=6)."""
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleFirstWeekday"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    m = re.search(r"(\d+)", out or "")
    if not m:
        return None
    apple = int(m.group(1))
    return (apple + 5) % 7 if 1 <= apple <= 7 else None  # Sun=1->6, Mon=2->0


def _region_from_locale() -> str | None:
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleLocale"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if "_" in out:
            return out.split("_")[1].split("@")[0].upper()
    except Exception:
        pass
    try:
        loc = _locale.getlocale()[0] or (_locale.getdefaultlocale()[0] if hasattr(_locale, "getdefaultlocale") else None)
        if loc and "_" in loc:
            return loc.split("_")[1].split(".")[0].upper()
    except Exception:
        pass
    return None


def locale_first_weekday() -> int:
    """First day of the week as a Python weekday (Mon=0 .. Sun=6), from the system locale.

    Resolution order: SYNC_WEEK_FIRST_DAY env override -> macOS AppleFirstWeekday ->
    glibc FIRST_WEEKDAY -> region heuristic -> Monday (ISO default).
    """
    override = os.environ.get(WEEK_FIRST_DAY_ENV)
    if override:
        o = override.strip().lower()
        if o.isdigit():
            return int(o) % 7
        for i, name in enumerate(_DAY_NAMES):
            if name.startswith(o):
                return i

    apple = _apple_first_weekday()
    if apple is not None:
        return apple

    if hasattr(_locale, "FIRST_WEEKDAY"):  # glibc/Linux extension; Sun=1..Sat=7
        try:
            val = _locale.nl_langinfo(_locale.FIRST_WEEKDAY)
            n = ord(val[0]) if isinstance(val, str) and val else int(val)
            if 1 <= n <= 7:
                return (n + 5) % 7
        except Exception:
            pass

    region = _region_from_locale()
    if region and region in _SUNDAY_FIRST_REGIONS:
        return 6  # Sunday
    return 0  # Monday


def first_day_of_week(d: _dt.date, first_weekday: int | None = None) -> _dt.date:
    """Date of the first day of the locale week that contains date `d`."""
    fw = locale_first_weekday() if first_weekday is None else first_weekday
    return d - _dt.timedelta(days=(d.weekday() - fw) % 7)
