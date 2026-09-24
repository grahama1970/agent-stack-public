#!/usr/bin/env python3
"""Annotate Surf tab-list JSON with persistent first-seen ages."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

LEDGER = Path(os.environ.get("SURF_TAB_AGE_LEDGER", Path.home() / ".surf" / "tab-first-seen.json"))


def _load_tabs() -> list[dict]:
    data = json.load(sys.stdin)
    tabs = data if isinstance(data, list) else data.get("tabs", data.get("result", []))
    if not isinstance(tabs, list):
        raise ValueError("tab.list JSON must contain a tab list")
    return [dict(tab) for tab in tabs if isinstance(tab, dict) and "id" in tab]


def _load_ledger() -> dict:
    try:
        data = json.loads(LEDGER.read_text())
        return data if data.get("schema") == "surf.tab_age_ledger.v1" else {"schema": "surf.tab_age_ledger.v1", "tabs": {}}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {"schema": "surf.tab_age_ledger.v1", "tabs": {}}


def _save_ledger(data: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tab-age-", dir=LEDGER.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.chmod(name, 0o600)
        os.replace(name, LEDGER)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def annotate(tabs: list[dict], now: float) -> list[dict]:
    ledger = _load_ledger()
    entries = ledger.setdefault("tabs", {})
    had_history = bool(entries)
    current = {str(tab["id"]) for tab in tabs}
    for key in list(entries):
        if key not in current:
            del entries[key]
    for tab in tabs:
        key = str(tab["id"])
        if key not in entries:
            entries[key] = {"first_seen": now, "precise": had_history}
        entry = entries[key]
        age = max(0.0, now - float(entry.get("first_seen", now)))
        tab["first_seen"] = float(entry.get("first_seen", now))
        tab["age_seconds"] = round(age, 3)
        tab["age_human"] = _human(age)
        tab["age_source"] = "observed" if entry.get("precise") else "at_least"
    _save_ledger(ledger)
    return sorted(tabs, key=lambda tab: tab["age_seconds"], reverse=True)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    tabs = annotate(_load_tabs(), time.time())
    if mode == "annotate-tabs":
        json.dump(tabs, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if mode != "report":
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    for tab in tabs:
        prefix = ">=" if tab["age_source"] == "at_least" else ""
        print(f"{tab['id']}  {prefix}{tab['age_human']:>7}  {tab.get('title', '')}  {tab.get('url', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
