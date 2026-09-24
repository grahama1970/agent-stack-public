"""
Client for querying and controlling a RUNNING scheduler daemon.

The on-disk registry (jobs.json) is not proof that a job is scheduled:
the daemon loads jobs into APScheduler only at startup or on explicit
/reload. This module is how CLI commands verify the daemon's real
in-memory schedule instead of trusting the disk.

All calls use httpx with explicit short timeouts and fail soft (None),
because callers must distinguish "daemon unreachable" from "job absent".
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from config import PORT_FILE, DEFAULT_METRICS_PORT

_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)


def _base_url() -> str:
    try:
        port = int(PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        port = int(os.getenv("SCHEDULER_METRICS_PORT", DEFAULT_METRICS_PORT))
    return f"http://127.0.0.1:{port}"


def daemon_reachable() -> bool:
    """True when a scheduler daemon answers its metrics server."""
    try:
        resp = httpx.get(f"{_base_url()}/status", timeout=_TIMEOUT)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def scheduled_names() -> Optional[set[str]]:
    """Names the daemon actually has scheduled in APScheduler.

    Returns None when the daemon is unreachable — callers must treat
    None as "cannot confirm", never as "not scheduled".
    """
    try:
        resp = httpx.get(f"{_base_url()}/scheduled", timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError:
        return None
    except ValueError:
        return None
    names = payload.get("scheduled") if isinstance(payload, dict) else None
    if not isinstance(names, list):
        return None
    return {str(item) for item in names}


def reload_jobs() -> Optional[dict[str, Any]]:
    """Ask the daemon to reload jobs.json and reschedule. None if unreachable."""
    try:
        resp = httpx.post(f"{_base_url()}/reload", timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError:
        return None
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
