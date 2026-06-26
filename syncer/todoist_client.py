"""Todoist client — unified API v1 (REST v2 / Sync v9 are dead since Feb 2026).

Change detection and writes go through `POST /sync`:
  - read: send the saved `sync_token` + `resource_types`; get only what changed (+`is_deleted`
    tombstones) and a fresh token.
  - write: send a `commands` array (<=100/req); each command carries a client `uuid`
    (server-side idempotency) and creates use `temp_id` (parent+children in one batch).
Completed instances come from `GET /tasks/completed/by_completion_date` (~92-day window).
"""

from __future__ import annotations

import json
import os
import uuid as _uuid
from typing import Any

from . import config, transport


class TodoistError(Exception):
    pass


class TodoistAuthError(TodoistError):
    pass


def _load_token() -> str:
    env = os.environ.get("TODOIST_API_TOKEN")
    if env:
        return env.strip()
    try:
        with open(config.TODOIST_TOKEN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        tok = data.get("access_token") or data.get("token")
        if tok:
            return tok.strip()
    except FileNotFoundError:
        pass
    raise TodoistAuthError(
        f"No Todoist token. Set TODOIST_API_TOKEN or create {config.TODOIST_TOKEN_PATH} "
        '({"access_token": "..."}). Get one at Todoist -> Settings -> Integrations -> Developer.'
    )


class TodoistClient:
    def __init__(self, token: str | None = None):
        self._token = token or _load_token()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def new_uuid() -> str:
        return str(_uuid.uuid4())

    @staticmethod
    def command(ctype: str, args: dict, *, temp_id: str | None = None,
                uuid: str | None = None) -> dict:
        cmd: dict[str, Any] = {"type": ctype, "uuid": uuid or TodoistClient.new_uuid(), "args": args}
        if temp_id:
            cmd["temp_id"] = temp_id
        return cmd

    def sync(self, *, sync_token: str = "*", resource_types: list[str] | None = None,
             commands: list[dict] | None = None) -> dict:
        form: dict[str, Any] = {"sync_token": sync_token}
        if resource_types is not None:
            form["resource_types"] = json.dumps(resource_types)
        if commands is not None:
            form["commands"] = json.dumps(commands)
        resp = transport.request("POST", config.TODOIST_SYNC_URL,
                                 headers=self._headers, form_body=form)
        if resp.status == 401:
            raise TodoistAuthError("Todoist 401 — bad/expired API token")
        if not resp.ok:
            raise TodoistError(f"/sync HTTP {resp.status}: {resp.text[:400]}")
        return resp.json()

    def read_changes(self, sync_token: str = "*",
                     resource_types: list[str] | None = None) -> dict:
        return self.sync(sync_token=sync_token,
                         resource_types=resource_types or config.TODOIST_RESOURCE_TYPES)

    def apply_commands(self, commands: list[dict], *, sync_token: str = "*") -> dict:
        """Apply <=100 commands in one batch. Returns sync_status + temp_id_mapping."""
        if len(commands) > config.TODOIST_MAX_COMMANDS_PER_BATCH:
            raise TodoistError(
                f"{len(commands)} commands exceeds batch cap "
                f"{config.TODOIST_MAX_COMMANDS_PER_BATCH}; chunk before calling."
            )
        return self.sync(sync_token=sync_token, commands=commands)

    def get_completed_by_completion_date(self, *, since: str, until: str,
                                         cursor: str | None = None, limit: int = 200) -> dict:
        params = [f"since={since}", f"until={until}", f"limit={limit}"]
        if cursor:
            params.append(f"cursor={cursor}")
        url = f"{config.TODOIST_COMPLETED_BY_COMPLETION}?{'&'.join(params)}"
        resp = transport.request("GET", url, headers=self._headers)
        if resp.status == 401:
            raise TodoistAuthError("Todoist 401 — bad/expired API token")
        if not resp.ok:
            raise TodoistError(f"completed HTTP {resp.status}: {resp.text[:400]}")
        return resp.json()
