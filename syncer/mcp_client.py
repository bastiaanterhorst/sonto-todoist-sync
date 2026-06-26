"""Sonto MCP client — drives the app's MCP server directly as a JSON-RPC HTTP API.

Two responsibilities:
  1. OAuth: load the bearer token from the app's own token store, refresh it via the
     `refresh_token` grant when it expires, and write the rotated tokens back atomically.
  2. MCP Streamable-HTTP transport: initialize -> notifications/initialized -> tools/list ->
     tools/call, handling both `application/json` and `text/event-stream` responses.

For a 15-minute cron we re-initialize each run (sessions may be GC'd server-side) and never
cache tool schemas across runs (the app updates).
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import time
from typing import Any

from . import config, transport


class McpError(Exception):
    """A JSON-RPC or tool-level error from the Sonto MCP."""


class McpAuthError(McpError):
    """A 401 from the MCP endpoint (bad/missing bearer)."""


class NeedsRepair(McpError):
    """The refresh token is dead — a human must re-pair the MCP in Sonto settings."""


def _now() -> float:
    return time.time()


class TokenStore:
    """Reads/refreshes/persists the Sonto MCP OAuth tokens (shared with Claude Desktop)."""

    def __init__(self, path=config.SONTO_TOKENS_PATH):
        self.path = path

    def load(self) -> dict[str, Any]:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    @property
    def port(self) -> int:
        try:
            return int(self.load().get("port") or config.SONTO_MCP_DEFAULT_PORT)
        except Exception:
            return config.SONTO_MCP_DEFAULT_PORT

    def _expired(self, tokens: dict) -> bool:
        exp = tokens.get("expires_at")
        if not exp:
            return True
        return _now() >= float(exp) - config.TOKEN_REFRESH_SKEW_SECONDS

    def access_token(self, *, force_refresh: bool = False) -> str:
        tokens = self.load()
        if force_refresh or self._expired(tokens):
            tokens = self._refresh(tokens)
        return tokens["access_token"]

    def _refresh(self, tokens: dict) -> dict:
        port = int(tokens.get("port") or config.SONTO_MCP_DEFAULT_PORT)
        resp = transport.request(
            "POST",
            config.sonto_oauth_token_url(port),
            form_body={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens.get("client_id", ""),
            },
        )
        if resp.status == 400 and b"invalid_grant" in resp.body:
            raise NeedsRepair(
                "Sonto refresh token is invalid — re-pair the MCP server in Sonto "
                "Settings -> AI, then re-run setup."
            )
        if not resp.ok:
            raise McpAuthError(f"Token refresh failed: HTTP {resp.status}: {resp.text[:300]}")
        data = resp.json()
        new = dict(tokens)
        new["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            new["refresh_token"] = data["refresh_token"]
        expires_in = data.get("expires_in")
        new["expires_at"] = _now() + float(expires_in) if expires_in else _now() + 3600
        self._write_atomic(new)
        return new

    def _write_atomic(self, tokens: dict) -> None:
        # Lock a sidecar so two overlapping crons can't clobber a rotated refresh token.
        lock_path = str(self.path) + ".lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                tmp = str(self.path) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(tokens, f)
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class McpClient:
    def __init__(self, tokens: TokenStore | None = None):
        self.tokens = tokens or TokenStore()
        self.port = self.tokens.port
        self.session_id: str | None = None
        self._id = 0
        self._initialized = False

    # --- transport ---------------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _headers(self, post_init: bool) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.tokens.access_token()}",
        }
        if post_init:
            h["MCP-Protocol-Version"] = config.MCP_PROTOCOL_VERSION
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _send(self, payload: dict, *, post_init: bool, expect_response: bool,
              _retry: bool = True) -> Any:
        conn = http.client.HTTPConnection(
            config.SONTO_MCP_HOST, self.port, timeout=config.HTTP_TIMEOUT_SECONDS
        )
        try:
            conn.request("POST", "/", body=json.dumps(payload), headers=self._headers(post_init))
            resp = conn.getresponse()
            sid = resp.getheader("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if resp.status == 401:
                resp.read()
                if _retry:
                    self.tokens.access_token(force_refresh=True)  # rotate + persist
                    return self._send(payload, post_init=post_init,
                                      expect_response=expect_response, _retry=False)
                raise McpAuthError("401 from Sonto MCP after token refresh")
            if resp.status >= 400:
                raise McpError(f"MCP HTTP {resp.status}: {resp.read()[:300]!r}")
            if not expect_response:
                resp.read()
                return None
            return self._read_rpc(resp, payload.get("id"))
        finally:
            conn.close()

    def _read_rpc(self, resp, expect_id) -> Any:
        ctype = (resp.getheader("Content-Type") or "").lower()
        if "text/event-stream" not in ctype:
            body = resp.read()
            return json.loads(body) if body else None
        # SSE: events separated by blank lines; the JSON-RPC reply rides in `data:` lines.
        deadline = _now() + config.HTTP_TIMEOUT_SECONDS
        data: list[str] = []
        while _now() < deadline:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data:
                    try:
                        msg = json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        data = []
                        continue
                    data = []
                    if expect_id is None or msg.get("id") == expect_id:
                        return msg
                continue
            if line.startswith(":"):
                continue  # keep-alive comment
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        return None

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._send(payload, post_init=self._initialized, expect_response=True)
        if msg is None:
            raise McpError(f"No JSON-RPC response for {method}")
        if "error" in msg:
            raise McpError(f"{method} error: {msg['error']}")
        return msg.get("result")

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload, post_init=self._initialized, expect_response=False)

    # --- MCP lifecycle -----------------------------------------------------
    def connect(self) -> dict:
        result = self._rpc("initialize", {
            "protocolVersion": config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": config.MCP_CLIENT_NAME, "version": "0.0.1"},
        })
        self._initialized = True
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.connect()
        tools: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        if not self._initialized:
            self.connect()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if isinstance(result, dict) and result.get("isError"):
            raise McpError(f"Tool {name} returned error: {result.get('content')}")
        return result
