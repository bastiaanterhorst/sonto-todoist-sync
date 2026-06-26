"""CLI entrypoint.

  python run.py --introspect      # P0: connect to Sonto MCP, dump real tools + schemas
  python run.py --once [--dry-run]# run one reconcile pass (dry-run prints, applies nothing)
  python run.py --status          # last run, bootstrap phase, token health, lock
  python run.py --set-phase PHASE # advance/retreat the bootstrap ladder
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time

from . import config, store as store_mod


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _sonto_token_health() -> str:
    try:
        with open(config.SONTO_TOKENS_PATH, encoding="utf-8") as f:
            tok = json.load(f)
    except FileNotFoundError:
        return f"MISSING ({config.SONTO_TOKENS_PATH})"
    exp = tok.get("expires_at")
    if not exp:
        return "present, no expiry recorded"
    remaining = float(exp) - time.time()
    when = _dt.datetime.fromtimestamp(float(exp)).isoformat(timespec="seconds")
    state = "valid" if remaining > 0 else "EXPIRED (will refresh)"
    return f"{state}; expires {when} ({int(remaining)}s)"


def _todoist_token_health() -> str:
    if os.environ.get("TODOIST_API_TOKEN"):
        return "present (env TODOIST_API_TOKEN)"
    if config.TODOIST_TOKEN_PATH.exists():
        return f"present ({config.TODOIST_TOKEN_PATH})"
    return f"MISSING — create {config.TODOIST_TOKEN_PATH} or set TODOIST_API_TOKEN"


def cmd_status() -> int:
    s = store_mod.Store()
    print("sonto-todoist-sync status")
    print(f"  db:             {config.DB_PATH}")
    print(f"  bootstrap phase: {s.bootstrap_phase()}")
    print(f"  last run:       {s.get_state('last_run_at', 'never')}")
    print(f"  needs repair:   {s.get_state('needs_repair', 'no')}")
    print(f"  todoist token:  {'set' if s.get_state('todoist_sync_token') else 'unset (full sync next)'}")
    print(f"  sonto MCP token: {_sonto_token_health()}")
    print(f"  todoist token:  {_todoist_token_health()}")
    lock = s.conn.execute("SELECT pid, host, heartbeat_at FROM run_lock WHERE id=1").fetchone()
    print(f"  run lock:       {'held by pid %s@%s' % (lock['pid'], lock['host']) if lock else 'free'}")
    s.close()
    return 0


def cmd_introspect() -> int:
    from . import introspect  # imported lazily so --status works without a live MCP
    try:
        introspect.run_introspection()
        return 0
    except Exception as e:  # noqa: BLE001
        logging.error("introspection failed: %s", e)
        return 1


def cmd_once(dry_run: bool) -> int:
    from . import reconcile
    s = store_mod.Store()
    if not s.acquire_lock():
        logging.info("another run holds the lock; exiting.")
        s.close()
        return 0
    try:
        result = reconcile.run(s, dry_run=dry_run)
        s.set_state("last_run_at", _dt.datetime.now().isoformat(timespec="seconds"))
        s.conn.commit()
        logging.info("done: %s", result)
        return 0
    finally:
        s.release_lock()
        s.close()


def cmd_set_phase(phase: str) -> int:
    if phase not in config.BOOTSTRAP_PHASES:
        logging.error("unknown phase %r; valid: %s", phase, config.BOOTSTRAP_PHASES)
        return 2
    s = store_mod.Store()
    s.set_state("bootstrap_phase", phase)
    s.conn.commit()
    s.close()
    print(f"bootstrap phase set to {phase}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sonto-todoist-sync", description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--introspect", action="store_true", help="dump Sonto MCP tools + schemas")
    g.add_argument("--once", action="store_true", help="run one reconcile pass")
    g.add_argument("--status", action="store_true", help="show run + token status")
    g.add_argument("--set-phase", metavar="PHASE", help=f"one of {config.BOOTSTRAP_PHASES}")
    p.add_argument("--dry-run", action="store_true", help="with --once: apply nothing")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    if args.introspect:
        return cmd_introspect()
    if args.status:
        return cmd_status()
    if args.set_phase:
        return cmd_set_phase(args.set_phase)
    if args.once:
        return cmd_once(args.dry_run)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
