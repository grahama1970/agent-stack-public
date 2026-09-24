#!/usr/bin/env python3
"""Close browser windows Ask owns but no live run is using.

Ask already reaps at provisioning time, which only helps when another Ask run
starts. A machine that finishes its last roundtable at 18:00 and runs nothing
until morning keeps every window open all night -- and if the owning process
died mid-run, nothing ever comes back for them at all. This is the timer-driven
half, meant for cron.

It closes only what the ledger (`ask.browser_windows`) claims, and only when
the owning process is gone AND the mode's TTL has passed. It never inspects or
touches a window Ask did not record, so a human's own tabs are out of scope by
construction rather than by heuristic.

Dry-run by default; pass --apply to actually close.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = SKILL_ROOT.parent
SURF_RUN = SKILLS_DIR / "surf" / "run.sh"

sys.path.insert(0, str(SKILL_ROOT / "src"))

from ask import browser_windows  # noqa: E402


def _close(window_id: str, *, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [str(SURF_RUN), "window.close", str(window_id)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SURF_RUN.parent),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)[:200]
    return proc.returncode, (proc.stderr or "")[:200]


def _owned_ask_process(pid: int) -> bool:
    """Reject PID reuse: terminate only a live process whose command is Ask-owned."""
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
    except OSError:
        return False
    return "ask.tau_dag_cli" in command or str(SKILL_ROOT) in command


def reap(*, apply: bool, timeout: float, now: float) -> dict:
    entries = browser_windows.load()
    terminated: list[dict] = []
    seen_pids: set[int] = set()
    for entry in entries:
        pid = int(entry.get("pid") or 0)
        age = now - float(entry.get("created_at") or 0)
        if not pid or pid in seen_pids or age < browser_windows.ttl_for_mode(str(entry.get("mode") or "")):
            continue
        seen_pids.add(pid)
        if not browser_windows.pid_alive(pid) or not _owned_ask_process(pid):
            continue
        record = {"pid": pid, "age_seconds": int(age), "mode": entry.get("mode")}
        if apply:
            try:
                os.kill(pid, signal.SIGTERM)
                record["signal"] = "SIGTERM"
            except OSError as exc:
                record["error"] = str(exc)[:200]
        terminated.append(record)

    if apply and terminated:
        deadline = time.monotonic() + min(timeout, 5.0)
        while time.monotonic() < deadline and any(browser_windows.pid_alive(r["pid"]) for r in terminated if "error" not in r):
            time.sleep(0.1)

    due = browser_windows.reclaimable(entries, now=now)
    closed: list[dict] = []
    errors: list[dict] = []
    settled: set[str] = set()

    for entry in due:
        window_id = str(entry.get("window_id"))
        if window_id in settled:
            continue
        record = {
            "window_id": window_id,
            "mode": entry.get("mode"),
            "source": entry.get("source"),
            "age_seconds": int(now - float(entry.get("created_at") or 0)),
        }
        if not apply:
            closed.append(record)
            continue
        code, message = _close(window_id, timeout=timeout)
        # A window that is already gone still settles the obligation: the
        # ledger tracks what is outstanding, and a failed close on a closed
        # window is done, not broken.
        if code != 0 and "no window" not in message.lower():
            errors.append({**record, "error": message})
            continue
        settled.add(window_id)
        closed.append({**record, "returncode": code})

    if apply and settled:
        browser_windows.deregister(settled)

    return {
        "schema": "ask.window_reap_receipt.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "applied": apply,
        "registry": str(browser_windows.REGISTRY),
        "tracked_windows": len({str(e.get("window_id")) for e in entries}),
        "closed_count": len(closed),
        "kept_count": len({str(e.get("window_id")) for e in entries}) - len(closed),
        "terminated_owner_count": len(terminated),
        "terminated_owners": terminated,
        "closed": closed,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually close windows (default: dry-run)")
    ap.add_argument("--timeout", type=float, default=60.0, help="per-close timeout in seconds")
    ap.add_argument("--json", action="store_true", help="emit the JSON receipt only")
    args = ap.parse_args(argv)

    if not SURF_RUN.exists():
        print(f"surf run.sh not found at {SURF_RUN}", file=sys.stderr)
        return 2

    receipt = reap(apply=args.apply, timeout=args.timeout, now=time.time())
    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        mode = "APPLIED" if receipt["applied"] else "DRY-RUN"
        print(
            f"[{mode}] {receipt['closed_count']} abandoned Ask window(s) "
            f"{'closed' if receipt['applied'] else 'would close'}; "
            f"{receipt['kept_count']} still owned; {len(receipt['errors'])} error(s)."
        )
        for c in receipt["closed"]:
            print(f"  close  window {c['window_id']}  mode={c['mode']}  age={c['age_seconds']}s")
        for e in receipt["errors"]:
            print(f"  ERROR  window {e['window_id']}: {e['error']}")
    return 1 if receipt["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
