"""Optional external captcha solver hook (2captcha / CapSolver / anti-captcha, etc.).

Wire via HybridClient(captcha_solver=...) or env CAPTCHA_SOLVER_URL.
This module does not ship vendor SDKs — keep secrets out of the repo.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping


def env_http_solver(url: str, meta: Mapping[str, Any]) -> str | None:
    """
    POST JSON {"url", "status", "body_preview"} to CAPTCHA_SOLVER_URL.
    Expect {"token": "..."} on success.
    """
    endpoint = os.getenv("CAPTCHA_SOLVER_URL")
    if not endpoint:
        return None
    payload = json.dumps(
        {
            "url": url,
            "status": meta.get("status"),
            "body_preview": (meta.get("body") or "")[:1500],
            "api_key": os.getenv("CAPTCHA_API_KEY"),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = data.get("token") or data.get("captcha_token")
        return str(token) if token else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None
