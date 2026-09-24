"""Optional Jev review through Surf's shared semantic browser commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

VALID_MODES = {"required", "optional", "disabled"}


def find_surf() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent.parent / "surf" / "run.sh",
        Path.home() / ".agents" / "skills" / "surf" / "run.sh",
        Path.home() / ".pi" / "skills" / "surf" / "run.sh",
    ]
    return next((path for path in candidates if path.exists()), None)


def run_jev_semantic_review(
    *,
    mode: str,
    goal: str = "",
    outcome: str = "",
    tab_id: str = "",
    surf: Path | None = None,
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Run requested Surf semantic reads and return a typed, non-authoritative receipt."""
    if mode not in VALID_MODES:
        raise ValueError(f"--jev must be one of: {', '.join(sorted(VALID_MODES))}")

    receipt: dict[str, Any] = {
        "schema": "test-interactions.jev-review.v1",
        "mode": mode,
        "proof_grade": "advisory",
        "deterministic_results_changed": False,
        "status": "disabled" if mode == "disabled" else "not_requested",
        "operations": [],
    }
    if mode == "disabled" or not (goal.strip() or outcome.strip()):
        return receipt

    surf = surf or find_surf()
    if not surf:
        receipt.update(status="unavailable", error="surf_not_found")
        return receipt

    operations = [("semantic.filter", goal.strip()), ("semantic.verify", outcome.strip())]
    for command, question in operations:
        if not question:
            continue
        argv = [str(surf), command, question]
        if tab_id:
            argv.extend(["--tab-id", tab_id])
        argv.append("--json")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            receipt["operations"].append({"command": command, "status": "unavailable", "error": "timeout"})
            receipt["status"] = "unavailable"
            continue

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "surf_semantic_failed").strip()[:1000]
            receipt["operations"].append({"command": command, "status": "unavailable", "error": error})
            receipt["status"] = "unavailable"
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            receipt["operations"].append({"command": command, "status": "error", "error": "invalid_json"})
            receipt["status"] = "error"
            continue
        receipt["operations"].append({"command": command, "status": "accepted", "result": payload})
        if receipt["status"] in {"not_requested", "accepted"}:
            receipt["status"] = "accepted"

    return receipt


def jev_required_failed(receipt: dict[str, Any]) -> bool:
    return receipt.get("mode") == "required" and receipt.get("status") != "accepted"
