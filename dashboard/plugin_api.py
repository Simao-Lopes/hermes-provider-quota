"""Read-only current-session account quota. Never submits an inference request.

Mounted at /api/plugins/provider-quota/quota by the local Hermes backend. The
route takes the focused chat's runtime session id, borrows that session's own
provider credentials in-process, and asks the provider what is left. Nothing here
returns, logs or persists a credential; failures become "unavailable", never a
guessed number.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import math
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_CACHE = {}
_LOCK = threading.Lock()
TTL_SECONDS = 60

# Providers whose allowance Hermes already knows how to read.
SUPPORTED = frozenset({"openai-codex", "anthropic", "openrouter", "nous", "opencode-go"})

OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_BASE_PATHS = frozenset({"/zen/go/v1", "/inference/go/openai/v1"})


def gateway_session_registry():
    """The in-process gateway's live session table — REST and desktop chat share a process.

    Missing internals raise on purpose: the caller degrades to an honest
    "unavailable" instead of reading some other account's quota.
    """
    from tui_gateway import server

    missing = [name for name in ("_sessions", "_sessions_lock", "_session_uses_compute_host")
               if not hasattr(server, name)]
    if missing:
        raise RuntimeError(f"Hermes gateway session registry changed; missing {missing}")
    return server._sessions, server._sessions_lock, server._session_uses_compute_host


def session_credentials(session_id):
    """(provider, base_url, api_key) for a live session of THIS profile, else None."""
    from hermes_constants import get_hermes_home

    sessions, lock, uses_compute_host = gateway_session_registry()
    with lock:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Session is no longer active")
        home = session.get("profile_home")
        if home and Path(home).resolve() != Path(get_hermes_home()).resolve():
            raise HTTPException(409, "Session belongs to another profile")
        if uses_compute_host(session):
            return None  # the credential is on the compute host; never borrow a local one
        agent = session.get("agent")
        if agent is None:
            return None
    return (str(getattr(agent, "provider", "") or ""),
            str(getattr(agent, "base_url", "") or ""),
            str(getattr(agent, "api_key", "") or ""))


def fetch_opencode_go(base_url, api_key):
    """OpenCode Go allowance (rolling / weekly / monthly), from OpenCode's own usage API."""
    import httpx
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    url = urlsplit(base_url or "https://opencode.ai/zen/go/v1")
    if (url.scheme != "https" or url.netloc != "opencode.ai"
            or url.path.rstrip("/") not in OPENCODE_BASE_PATHS
            or url.query or url.fragment or not api_key):
        raise ValueError("OpenCode Go account endpoint unavailable")

    with httpx.Client(timeout=15.0) as client:
        response = client.get(OPENCODE_USAGE_URL,
                              headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        response.raise_for_status()
        usage = response.json()["usage"]

    windows = []
    for key, label in (("rolling", "Rolling"), ("weekly", "Weekly"), ("monthly", "Monthly")):
        meter = usage[key]
        percent = meter["percent"]
        if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                or not math.isfinite(percent) or not 0 <= percent <= 100):
            raise ValueError("Invalid OpenCode quota percentage")
        reset_at = datetime.fromisoformat(str(meter["resetsAt"]).replace("Z", "+00:00"))
        if reset_at.tzinfo is None:
            raise ValueError("OpenCode reset time has no timezone")
        windows.append(AccountUsageWindow(label, percent, reset_at))
    return AccountUsageSnapshot("opencode-go", "opencode_usage_api", datetime.now(timezone.utc),
                                plan="Go", windows=tuple(windows))


def account_snapshot(provider, base_url, api_key):
    from agent.account_usage import build_nous_credits_snapshot, fetch_account_usage

    if provider == "opencode-go":
        return fetch_opencode_go(base_url, api_key)
    if provider == "nous":
        from hermes_cli.nous_account import get_nous_portal_account_info
        return build_nous_credits_snapshot(get_nous_portal_account_info(force_fresh=True))
    return fetch_account_usage(provider, base_url=base_url or None, api_key=api_key or None)


def read_quota(credentials):
    if not credentials:
        return {"available": False, "reason": "No local active provider yet", "windows": [], "details": []}
    provider, base_url, api_key = credentials

    identity = hashlib.sha256((provider + "\0" + base_url + "\0" + api_key).encode()).digest()
    with _LOCK:
        cached = _CACHE.get(identity)
        if cached and time.monotonic() - cached[0] < TTL_SECONDS:
            return cached[1]

        supported = provider in SUPPORTED
        try:
            snapshot = account_snapshot(provider, base_url, api_key) if supported else None
        except Exception:
            # Account services fail independently of chat. Never leak tokens or tracebacks.
            snapshot = None

        data = asdict(snapshot) if snapshot else {"windows": [], "details": []}
        data.update(provider=provider, available=bool(snapshot and snapshot.available))
        if not data["available"]:
            data["reason"] = ("Provider quota is temporarily unavailable" if supported
                              else "This provider does not expose quota through Hermes")
        data["checked_at"] = datetime.now(timezone.utc).isoformat()

        _CACHE[identity] = (time.monotonic(), data)
        for key, entry in list(_CACHE.items()):  # bound growth across credential rotation
            if time.monotonic() - entry[0] > 600:
                del _CACHE[key]
        return data


@router.get("/quota")
def quota(session_id: str = Query(min_length=1, max_length=200)):
    return read_quota(session_credentials(session_id))
