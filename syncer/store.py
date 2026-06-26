"""SQLite persistence: the ID-mapping layer, run state, Sonto snapshot, idempotency
ledger, and a single-row run-lock.

The mapping table is keyed on (entity_type, sonto_id, todoist_id). `last_synced_hash` is
the canonical content hash agreed at the last successful sync; comparing each side's
current hash against it is what classifies create/update/delete and suppresses echo
(ping-pong). See docs/PLAN.md.
"""

from __future__ import annotations

import contextlib
import socket
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterator

from . import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS id_map (
  entity_type      TEXT NOT NULL,
  sonto_id         TEXT,
  todoist_id       TEXT,
  sonto_updated    TEXT,
  todoist_updated  TEXT,
  last_synced_hash TEXT,
  sonto_hash       TEXT,
  todoist_hash     TEXT,
  last_synced_at   TEXT,
  deleted          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (entity_type, sonto_id, todoist_id)
);
CREATE INDEX IF NOT EXISTS idx_map_sonto   ON id_map(entity_type, sonto_id);
CREATE INDEX IF NOT EXISTS idx_map_todoist ON id_map(entity_type, todoist_id);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sonto_snapshot (
  entity_type TEXT NOT NULL,
  sonto_id    TEXT NOT NULL,
  hash        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  seen_at     TEXT NOT NULL,
  PRIMARY KEY (entity_type, sonto_id)
);

CREATE TABLE IF NOT EXISTS applied_commands (
  uuid       TEXT PRIMARY KEY,
  command    TEXT NOT NULL,
  status     TEXT,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_lock (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  pid          INTEGER,
  host         TEXT,
  acquired_at  REAL,
  heartbeat_at REAL
);
"""


@dataclass
class MapRow:
    entity_type: str
    sonto_id: str | None
    todoist_id: str | None
    sonto_updated: str | None
    todoist_updated: str | None
    last_synced_hash: str | None
    sonto_hash: str | None
    todoist_hash: str | None
    last_synced_at: str | None
    deleted: int


class Store:
    def __init__(self, db_path=config.DB_PATH):
        self.db_path = db_path
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        cur = self.get_state("schema_version")
        if cur is None:
            self.set_state("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # --- transactions ------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN IMMEDIATE;")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- state -------------------------------------------------------------
    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def bootstrap_phase(self) -> str:
        return self.get_state("bootstrap_phase", config.DEFAULT_PHASE)

    # --- id_map ------------------------------------------------------------
    def upsert_map(self, **fields) -> None:
        cols = [
            "entity_type", "sonto_id", "todoist_id", "sonto_updated", "todoist_updated",
            "last_synced_hash", "sonto_hash", "todoist_hash", "last_synced_at", "deleted",
        ]
        vals = [fields.get(c) for c in cols]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in
                            ("entity_type", "sonto_id", "todoist_id"))
        self.conn.execute(
            f"INSERT INTO id_map ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(entity_type, sonto_id, todoist_id) DO UPDATE SET {updates}",
            vals,
        )

    def maps(self, entity_type: str | None = None) -> list[MapRow]:
        if entity_type:
            rows = self.conn.execute(
                "SELECT * FROM id_map WHERE entity_type=?", (entity_type,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM id_map").fetchall()
        return [MapRow(**dict(r)) for r in rows]

    def map_by_sonto(self, entity_type: str, sonto_id: str) -> MapRow | None:
        r = self.conn.execute(
            "SELECT * FROM id_map WHERE entity_type=? AND sonto_id=?",
            (entity_type, sonto_id),
        ).fetchone()
        return MapRow(**dict(r)) if r else None

    def map_by_todoist(self, entity_type: str, todoist_id: str) -> MapRow | None:
        r = self.conn.execute(
            "SELECT * FROM id_map WHERE entity_type=? AND todoist_id=?",
            (entity_type, todoist_id),
        ).fetchone()
        return MapRow(**dict(r)) if r else None

    # --- sonto snapshot ----------------------------------------------------
    def snapshot(self, entity_type: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM sonto_snapshot WHERE entity_type=?", (entity_type,)
        ).fetchall()
        return {r["sonto_id"]: r for r in rows}

    def upsert_snapshot(self, entity_type: str, sonto_id: str, hash_: str,
                        payload: str, seen_at: str) -> None:
        self.conn.execute(
            "INSERT INTO sonto_snapshot(entity_type, sonto_id, hash, payload, seen_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(entity_type, sonto_id) DO UPDATE SET "
            "hash=excluded.hash, payload=excluded.payload, seen_at=excluded.seen_at",
            (entity_type, sonto_id, hash_, payload, seen_at),
        )

    def delete_snapshot(self, entity_type: str, sonto_id: str) -> None:
        self.conn.execute(
            "DELETE FROM sonto_snapshot WHERE entity_type=? AND sonto_id=?",
            (entity_type, sonto_id),
        )

    # --- idempotency ledger ------------------------------------------------
    def command_seen(self, uuid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM applied_commands WHERE uuid=?", (uuid,)
        ).fetchone() is not None

    def record_command(self, uuid: str, command: str, status: str, applied_at: str) -> None:
        self.conn.execute(
            "INSERT INTO applied_commands(uuid, command, status, applied_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uuid) DO UPDATE SET status=excluded.status",
            (uuid, command, status, applied_at),
        )

    # --- run lock ----------------------------------------------------------
    def acquire_lock(self) -> bool:
        """Single-row advisory lock. Steals a stale lock (dead prior run)."""
        now = time.time()
        host = socket.gethostname()
        pid = _getpid()
        with self.transaction() as c:
            row = c.execute("SELECT pid, heartbeat_at FROM run_lock WHERE id=1").fetchone()
            if row is not None:
                age = now - (row["heartbeat_at"] or 0)
                if age < config.RUN_LOCK_STALE_SECONDS:
                    return False  # held and fresh
            c.execute(
                "INSERT INTO run_lock(id, pid, host, acquired_at, heartbeat_at) "
                "VALUES(1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "pid=excluded.pid, host=excluded.host, acquired_at=excluded.acquired_at, "
                "heartbeat_at=excluded.heartbeat_at",
                (pid, host, now, now),
            )
        return True

    def heartbeat(self) -> None:
        self.conn.execute("UPDATE run_lock SET heartbeat_at=? WHERE id=1", (time.time(),))
        self.conn.commit()

    def release_lock(self) -> None:
        self.conn.execute("DELETE FROM run_lock WHERE id=1")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _getpid() -> int:
    import os
    return os.getpid()
