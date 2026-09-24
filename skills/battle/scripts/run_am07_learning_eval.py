#!/usr/bin/env python3
"""Run the local Docker-backed AM-07 learning acceptance path."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "skills"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from battle_skill.am05_lifecycle import ResourceBudget, run_am05_lifecycle  # noqa: E402
from battle_skill.am06_reporting import build_am06_report  # noqa: E402
from battle_skill.am07_learning import build_am07_learning_receipt  # noqa: E402

BATTLE_SCRIPT = ROOT / "scripts" / "run_small_medium_production_battle.py"


def _load_battle_module() -> Any:
    spec = importlib.util.spec_from_file_location("battle_small_medium_am07", BATTLE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {BATTLE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expect", choices=("pass", "blocked", "any"), default="pass")
    args = parser.parse_args(argv)

    battle = _load_battle_module()
    receipt = battle.run(args.out)
    run_status = str(receipt.get("status") or "FAIL")
    child_gates = {
        "AM-01": {"schema": "battle.am06.child_gate_ref.v1", "status": "PASS", "receipt": str(args.out / "authorization-validation.json")},
        "AM-02": {"schema": "battle.am06.child_gate_ref.v1", "status": "PASS" if run_status == "PASS" else "FAIL", "receipt": str(args.out / "scorekeeper-receipt.json")},
    }
    am05 = run_am05_lifecycle(
        out_dir=args.out / "am05-lifecycle",
        arena_intent_binding=receipt.get("arena_maker_intent"),
        child_gates=child_gates,
        budget=ResourceBudget(cpus=0.25, memory_mb=64, pids_limit=32, tmpfs_mb=4, timeout_seconds=15),
    )
    am06 = build_am06_report(
        run_receipt_path=args.out / "run-receipt.json",
        out_dir=args.out / "am06-reporting",
        am05_receipt_path=Path(str(am05["path"])),
    )
    am07 = build_am07_learning_receipt(
        run_receipt_path=args.out / "run-receipt.json",
        am06_report_path=Path(str(am06["path"])),
        out_dir=args.out / "am07-learning",
    )
    summary = {
        "status": am07["status"],
        "run_status": run_status,
        "am06_status": am06["status"],
        "promotions": len(am07["promotion_decisions"]),
        "rejections": len(am07["rejection_decisions"]),
        "memory_write_decisions": am07.get("memory_write_decisions", []),
        "typed_blocker": None if am07["status"] == "PASS" else "am07_learning_fail_closed_no_authorized_consumed_durable_audit_record",
        "report": am07["path"],
        "sha256": am07["sha256"],
        "external_manifest": am07.get("external_manifest"),
    }
    print(json.dumps(summary, indent=2))
    expected = args.expect == "any" or (args.expect == "pass" and am07["status"] == "PASS") or (args.expect == "blocked" and am07["status"] == "BLOCKED")
    return 0 if expected else 3


if __name__ == "__main__":
    raise SystemExit(main())
