#!/usr/bin/env python3
"""Build the Complete Battle integrated acceptance/release-gate receipt."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "skills"))
sys.path.insert(0, str(PROJECT_ROOT))

from battle_skill.complete_acceptance import build_complete_acceptance_receipt, release_gate_from_complete_receipt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=ROOT / "fixtures" / "complete_acceptance_requirements.round1.json")
    parser.add_argument("--am06-report", type=Path, required=True)
    parser.add_argument("--run-receipt", type=Path)
    parser.add_argument("--am05-receipt", type=Path)
    parser.add_argument("--am07-receipt", type=Path)
    parser.add_argument("--dependency", action="append", default=[], help="name=path extra dependency receipt, e.g. qualification_bundle=receipt.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    dependencies: dict[str, Path] = {}
    for item in args.dependency:
        if "=" not in item:
            raise SystemExit(f"--dependency must be name=path: {item}")
        name, raw_path = item.split("=", 1)
        dependencies[name] = Path(raw_path)
    receipt = build_complete_acceptance_receipt(
        requirements_path=args.requirements,
        out_dir=args.out,
        am06_report_path=args.am06_report,
        run_receipt_path=args.run_receipt,
        am05_receipt_path=args.am05_receipt,
        am07_receipt_path=args.am07_receipt,
        dependency_receipts=dependencies,
    )
    gate = release_gate_from_complete_receipt(receipt)
    print(json.dumps({"status": receipt["status"], "release_gate": gate["status"], "release_authorized": gate["release_authorized"], "receipt": receipt["path"], "sha256": receipt["sha256"]}, indent=2, sort_keys=True))
    return 0 if gate["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
