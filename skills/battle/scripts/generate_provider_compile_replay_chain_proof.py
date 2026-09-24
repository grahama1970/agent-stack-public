#!/usr/bin/env python3
"""Generate the ticket-03 local compile, execution, Judge, and exact-replay proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from battle_skill.adaptive_red_blue_lineage_canary import (
    _docker_image_id,
    _preserve_selected_artifact,
    _run_exact_judge_replay,
)
from battle_skill.arena_live_battle_proof import _judge_tau_artifacts
from battle_skill.compile_repair import bounded_compile_repair, run_compile_gate


TARGET_APP = '''from pathlib import Path
from zipfile import ZipFile


def import_zip(zip_path: str, destination: str) -> list[str]:
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with ZipFile(zip_path) as archive:
        for name in archive.namelist():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            written.append(str(target))
    return written
'''

RED_EXPLOIT = '''import argparse
import tempfile
import zipfile
from pathlib import Path

from app import import_zip

MARKER = "RED_EXPLOIT_CONFIRMED"


def run_exploit() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive_path = root / "payload.zip"
        destination = root / "imported"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../arena_escape.txt", MARKER)
        import_zip(str(archive_path), str(destination))
        escaped = root / "arena_escape.txt"
        return escaped.is_file() and escaped.read_text(encoding="utf-8") == MARKER


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-vulnerable", action="store_true")
    args = parser.parse_args()
    vulnerable = run_exploit()
    if vulnerable:
        print(MARKER)
        return 0
    if args.expect_vulnerable:
        return 1
    print("EXPLOIT_BLOCKED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

BLUE_PATCH = '''from pathlib import Path
from zipfile import ZipFile


def import_zip(zip_path: str, destination: str) -> list[str]:
    dest = Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with ZipFile(zip_path) as archive:
        for name in archive.namelist():
            target = (dest / name).resolve()
            if not target.is_relative_to(dest):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            written.append(str(target))
    return written
'''

BROKEN_BLUE_PATCH = "def import_zip(zip_path: str, destination: str) -> list[str]:\n    return [\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("proofs/ticket-03-provider-chain-local"),
    )
    parser.add_argument("--docker-image", default="python:3.12-slim")
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    image_id = _docker_image_id(args.docker_image)
    fixtures_dir = out_dir / "fixtures"
    fixtures_dir.mkdir()
    direct_chain = _build_chain(
        out_dir=out_dir,
        fixtures_dir=fixtures_dir,
        fixture_id="direct-owned-fixture",
        generation=1,
        docker_image=args.docker_image,
        docker_image_id=image_id,
        repaired_blue=False,
    )
    repaired_chain = _build_chain(
        out_dir=out_dir,
        fixtures_dir=fixtures_dir,
        fixture_id="repaired-owned-fixture",
        generation=2,
        docker_image=args.docker_image,
        docker_image_id=image_id,
        repaired_blue=True,
    )
    chains = [direct_chain, repaired_chain]
    status = "PASS" if all(chain["status"] == "PASS" for chain in chains) else "BLOCKED"
    summary = {
        "schema": "battle.provider_compile_execution_replay_chain.local_proof.v1",
        "status": status,
        "docker_image": args.docker_image,
        "docker_image_id": image_id,
        "owned_fixture_count": len(chains),
        "chains": chains,
        "proof_boundary": {
            "proves": [
                "Two owned local fixtures compiled in Docker, executed in isolated Docker Judge workspaces, and produced PASS exact-pair replay receipts.",
                "The repaired fixture selected artifact differs from the original broken provider bytes only through an explicit provider_response_id repair authorization chain.",
            ] if status == "PASS" else [],
            "does_not_prove": [
                "A live Tau/provider authored these local owned fixtures.",
                "QEMU execution succeeded.",
                "Judge correctness beyond this retained local fixture pair.",
            ],
        },
    }
    _write_json(out_dir / "local-docker-summary.json", summary)
    print(json.dumps({"status": status, "out_dir": str(out_dir), "chains": len(chains)}, sort_keys=True))
    return 0 if status == "PASS" else 1


def _build_chain(
    *,
    out_dir: Path,
    fixtures_dir: Path,
    fixture_id: str,
    generation: int,
    docker_image: str,
    docker_image_id: str,
    repaired_blue: bool,
) -> dict[str, Any]:
    chain_dir = out_dir / "execution-replay" / fixture_id
    generation_dir = chain_dir / f"generation-{generation}"
    target_dir = generation_dir / "arena" / "team-public" / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "app.py").write_text(TARGET_APP, encoding="utf-8")
    target_sha = _sha(target_dir / "app.py")

    red_source = fixtures_dir / f"{fixture_id}-red.py"
    red_source.write_text(RED_EXPLOIT, encoding="utf-8")
    red_compile = run_compile_gate(
        source_path=red_source,
        out_dir=chain_dir / "red-compile",
        source_artifact_sha256=_sha_prefixed(red_source),
        team="red",
        generation=generation,
        docker_image=docker_image,
        provider_route=_local_route(fixture_id, "red"),
        use_docker=True,
    )

    if repaired_blue:
        blue_source = fixtures_dir / f"{fixture_id}-blue-broken.py"
        blue_source.write_text(BROKEN_BLUE_PATCH, encoding="utf-8")
        blue_receipt = bounded_compile_repair(
            source_path=blue_source,
            out_dir=chain_dir / "blue-repaired-compile",
            final_path=chain_dir / "blue-repaired-compile" / "selected.py",
            max_compile_repairs=1,
            repair_candidates=[
                {
                    "text": BLUE_PATCH,
                    "provenance_factory": lambda attempt, candidate_sha: {
                        "provider_response_id": f"{fixture_id}-blue-repair-response",
                        "failed_specimen_sha256": attempt["specimen_sha256"],
                        "repaired_specimen_sha256": candidate_sha,
                        "stderr_sha256": attempt["stderr_sha256"],
                    },
                }
            ],
            docker_image=docker_image,
            provider_route=_local_route(fixture_id, "blue"),
            use_docker=True,
        )
        blue_compile = blue_receipt["final_compile_receipt"]
        blue_selected = Path(str(blue_receipt["final_specimen_path"]))
        blue_compile_path = chain_dir / "blue-repaired-compile" / "compile_receipt.json"
    else:
        blue_source = fixtures_dir / f"{fixture_id}-blue.py"
        blue_source.write_text(BLUE_PATCH, encoding="utf-8")
        blue_compile = run_compile_gate(
            source_path=blue_source,
            out_dir=chain_dir / "blue-compile",
            source_artifact_sha256=_sha_prefixed(blue_source),
            team="blue",
            generation=generation,
            docker_image=docker_image,
            provider_route=_local_route(fixture_id, "blue"),
            use_docker=True,
        )
        blue_receipt = None
        blue_selected = blue_source
        blue_compile_path = chain_dir / "blue-compile" / "compile_receipt.json"

    red_slot = _preserve_selected_artifact(
        generation_dir=generation_dir,
        generation=generation,
        team="red",
        source=red_source,
        expected_sha256=_sha(red_source),
    )
    blue_slot = _preserve_selected_artifact(
        generation_dir=generation_dir,
        generation=generation,
        team="blue",
        source=blue_selected,
        expected_sha256=_sha(blue_selected),
    )
    pipelines = {
        "red": _pipeline(red_slot, red_source, chain_dir / "red-compile" / "compile_receipt.json"),
        "blue": _pipeline(blue_slot, blue_selected, blue_compile_path),
    }
    manifest = {
        "schema": "battle.local_owned_fixture_manifest.v1",
        "status": "PASS",
        "teams": [
            _team_manifest("red", pipelines["red"]),
            _team_manifest("blue", pipelines["blue"]),
        ],
    }
    scenario = {
        "schema": "battle.arena_scenario.v1",
        "scenario_id": f"{fixture_id}-zip-slip",
        "scenario_kind": "zip-slip",
        "public_entrypoint": "import_zip(zip_path, destination)",
    }
    original_judge = _judge_tau_artifacts(
        out_dir=generation_dir,
        scenario=scenario,
        docker_image=docker_image,
        tau_manifest=manifest,
    )
    original_judge_path = _write_json(generation_dir / "judge" / "judge-receipt.json", original_judge)
    exact_replay = _run_exact_judge_replay(
        generation_dir=generation_dir,
        scenario=scenario,
        docker_image=docker_image,
        reviewed_manifest=manifest,
        original_judge=original_judge,
        pipelines=pipelines,
        target_identity_sha256=target_sha,
        docker_image_id=docker_image_id,
    )
    status = "PASS" if (
        red_compile["status"] == "PASS"
        and blue_compile["status"] == "PASS"
        and original_judge["status"] == "PASS"
        and exact_replay["status"] == "PASS"
    ) else "BLOCKED"
    return {
        "fixture_id": fixture_id,
        "status": status,
        "source_kind": "local_owned_fixture",
        "provider_live": False,
        "generation": generation,
        "target_identity_sha256": target_sha,
        "red_compile_receipt": str(chain_dir / "red-compile" / "compile_receipt.json"),
        "blue_compile_receipt": str(blue_compile_path),
        "blue_provider_compile_repair_receipt": str(chain_dir / "blue-repaired-compile" / "provider_compile_repair_receipt.json") if repaired_blue else None,
        "repaired_blue": repaired_blue,
        "repair_history_required": bool(blue_receipt and blue_receipt.get("repair_history_required")),
        "judge_receipt": str(original_judge_path),
        "judge_status": original_judge["status"],
        "judge_verdict": original_judge["verdict"],
        "judged_pair_count": original_judge["judged_pair_count"],
        "isolated_execution_command_count": len(original_judge.get("commands_run", [])),
        "exact_replay_receipt": exact_replay["path"],
        "exact_replay_sha256": exact_replay["sha256"],
        "exact_replay_status": exact_replay["status"],
        "exact_replay_matched": exact_replay["matched"],
    }


def _pipeline(slot: dict[str, Any], source: Path, compile_receipt: Path) -> dict[str, Any]:
    return {
        "selected_artifact_sha256": slot["preserved_sha256"],
        "selected_artifact_path": slot["path"],
        "selected_artifact_source_path": str(source),
        "compile_receipt_path": str(compile_receipt),
        "handoff_sha256": _sha(compile_receipt),
        "immutable_slot": slot,
    }


def _team_manifest(team: str, pipeline: dict[str, Any]) -> dict[str, Any]:
    return {
        "team": team,
        "worker_id": f"{team}-0",
        "lane_id": f"local-{team}-lane",
        "action_id": f"local-{team}-action",
        "materialized_artifact": {"status": "PASS", "path": pipeline["selected_artifact_path"]},
    }


def _local_route(fixture_id: str, team: str) -> dict[str, str]:
    return {
        "requested_provider": "local-owned-fixture",
        "requested_model": f"{fixture_id}-{team}",
        "observed_provider": "local-owned-fixture",
        "observed_model": f"{fixture_id}-{team}",
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_prefixed(path: Path) -> str:
    return f"sha256:{_sha(path)}"


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
