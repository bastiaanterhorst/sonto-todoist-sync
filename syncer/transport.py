"""Tiny stdlib HTTP helper.

Returns (status, headers, body_bytes) and does NOT raise on 4xx/5xx, so callers can
inspect 401s (token refresh), 429s (backoff), etc. Used for plain JSON/form requests
(Todoist API, the Sonto OAuth token endpoint). The MCP Streamable-HTTP transport needs
SSE-aware streaming and lives in `mcp_client.py`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form_body: dict[str, Any] | None = None,
    timeout: float = config.HTTP_TIMEOUT_SECONDS,
) -> HttpResponse:
    headers = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body, doseq=True).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as e:
        # Non-2xx: surface it instead of raising, so callers can branch on status.
        return HttpResponse(e.code, dict(e.headers.items()) if e.headers else {}, e.read())
