#!/usr/bin/env python3
"""Agentic-eval driver for the live oai-trial adaptive invariant Battle."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
TARGET = Path("~/workspace/experiments/oai-trial")
IDENTITY = "oai-trial@TRIAL_BRIEF.md:sha256:83b1199fa7f0da86b6e0836b6634657a5ff92c6d9f8bd3014f115a3de4942bfc"
RED_RECEIPT = TARGET / "security/battle/runs/adaptive-contextual-graph-c34-live-final/adaptive-battle-receipt.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["live", "denied"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    shutil.rmtree(args.out, ignore_errors=True)
    target_identity = IDENTITY if args.mode == "live" else "wrong-target"
    command = [
        "uv", "run", "--project", str(SKILL), "python", "-m", "battle_skill.adaptive_invariant_battle",
        "--adapter", str(TARGET / "security/battle/contextual_graph_generator.py"),
        "--target-run-cmd", "docker run --rm -v {input}:/trial/input:ro -v {output}:/trial/output anonymization-trial run",
        "--judge", str(SKILL / "fixtures/reference-judges/no_data_leak_judge.py"),
        "--functional-judge", str(SKILL / "fixtures/reference-judges/functional_anonymize_judge.py"),
        "--authorization-manifest", str(TARGET / "security/battle/authorization.json"),
        "--target-identity", target_identity, "--execution-target", "anonymization-trial",
        "--out", str(args.out), "--parent-run-id", f"agentic-eval-{args.mode}",
        "--generations", "2", "--population", "2",
    ]
    if args.mode == "live":
        command.extend(["--blue-replay-from", str(RED_RECEIPT)])
    env = os.environ.copy()
    python_path = [str(SKILL.parent), str(SKILL / "src")]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    completed = subprocess.run(
        command, text=True, capture_output=True, check=False, timeout=300, env=env
    )
    if not (args.out / "adaptive-battle-receipt.json").is_file():
        raise AssertionError(
            "adaptive Battle did not emit a receipt\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    receipt = json.loads((args.out / "adaptive-battle-receipt.json").read_text())
    if args.mode == "live":
        assert completed.returncode == 0, completed.stderr
        assert receipt["status"] == "PASS"
        assert receipt["release_blocked"] is False
        assert receipt["counts"]["generations"] == 2
        assert receipt["counts"]["executed_mutations"] == 4
        assert receipt["counts"]["judge_confirmed_red_wins"] == 0
        assert receipt["counts"]["promoted_deterministic_cases"] == 0
        assert receipt["counts"]["replayed_blue_fixes"] == 3
        assert receipt["counts"]["functional_control_failures"] == 0
        assert receipt["blue_fix_status"] == "PASS"
        assert receipt["proof_boundary"]["blue_fix_claimed"] is True
        assert receipt["release_gate"]["decision"] == "ALLOW"
        for generation in receipt["generations"]:
            selection = json.loads(Path(generation["selection_receipt"]).read_text())
            assert selection["schema"] == "battle.universal_adaptive_lineage_generation.v2"
            assert selection["oracle"]["oracle_id"] == "battle-contextual-invariant-judge-oracle-v1"
            assert selection["oracle"]["evidence"]["source"] == "independent_invariant_judge_case_receipts"
            assert selection["reproduction"]["status"] == "PASS"
        replay = receipt["blue_fix_replays"][0]
        assert replay["schema"] == "battle.adaptive_blue_replay.v1"
        assert replay["status"] == "PASS"
        assert replay["source_unresolved_red_wins"] == ["g01-m02", "g02-m02", "g03-m02"]
        assert Path(replay["campaign_receipt"]).is_file()
    else:
        assert completed.returncode == 1
        assert receipt["status"] == "BLOCKED" and receipt["execution_started"] is False
        assert not (args.out / "campaigns").exists()
    print(f"ADAPTIVE_CONTEXTUAL_EVAL_PASS mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
