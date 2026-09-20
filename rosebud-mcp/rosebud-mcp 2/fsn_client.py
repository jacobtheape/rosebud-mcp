"""Flower Shop Network relay API client (stdlib only).

Ports the HTTP logic from the flowers skill's bin/fsn.py for reuse by the
Rosebud MCP server. Raises FSNError on any API or transport failure.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.flowershopnetwork.com/api/"
USER_AGENT = "rosebud-mcp/1.0"


class FSNError(Exception):
    pass


def _token(explicit=None):
    token = explicit or os.environ.get("FSN_API_TOKEN")
    if not token:
        raise FSNError(
            "No FSN API token configured. Set the FSN_API_TOKEN environment "
            "variable to a partner token provisioned by Flower Shop Network."
        )
    return token


def call(func, params, token=None):
    """POST to an FSN API function; return the parsed JSON response."""
    params = dict(params)
    params["__token"] = _token(token)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(BASE + func, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "{}"
        try:
            body = json.loads(raw)
            ex = (body.get("exception") or {})
            raise FSNError(f"FSN HTTP {e.code}: {ex.get('message', raw)}")
        except (json.JSONDecodeError, AttributeError):
            raise FSNError(f"FSN HTTP error {e.code}: {raw}")
    except urllib.error.URLError as e:
        raise FSNError(f"FSN network error: {e.reason}")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise FSNError(f"FSN returned non-JSON: {raw[:200]}")
    if isinstance(body, dict) and "exception" in body:
        ex = body["exception"] or {}
        msg = ex.get("message", "API exception")
        errors = ex.get("errors") or []
        detail = f" ({'; '.join(errors)})" if errors else ""
        raise FSNError(f"FSN error: {msg}{detail}")
    return body
