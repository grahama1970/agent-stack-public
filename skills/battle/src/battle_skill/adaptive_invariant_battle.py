"""Judge-directed adaptive invariant Battle controller.

The target adapter owns only domain fixture materialization and mutation
operators.  Battle owns generation order, Judge execution, parent selection,
lineage, promotion, release blocking, and receipts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from common.security_authorization import validate_target_authorization

from .adaptive_lineage_engine import (
    AdaptiveLineageEngine,
    GenerationRequest,
    JudgedPopulation,
    OracleSelection,
    PopulationStageResult,
    RecallResult,
    candidate_id,
)
from .adaptive_lineage_memory import MemoryBackend
from .invariant_campaign import run_campaign

SCHEMA = "battle.adaptive_invariant_battle.v1"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("battle_adaptive_target_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load adaptive adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "adaptive_seeds", "adaptive_mutations", "adaptive_safe_control",
        "materialize_adaptive_case", "adaptive_validate_materialized_case",
    ):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"adaptive adapter missing callable {name}")
    return module


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _score(receipt: dict[str, Any], mutation: dict[str, Any]) -> list[int]:
    red_win = _is_red_win(receipt)
    execution = receipt.get("execution") or {}
    # Only objective case/Judge fields lead the ordering. Mutation complexity
    # breaks ties among candidates with the same observed outcome.
    return [
        int(red_win),
        int(execution.get("kind") == "ACCEPT"),
        int((receipt.get("security_judge") or {}).get("status") == "FAIL"),
        int(mutation.get("hop_count", 0)),
        len(mutation.get("clues", [])),
        int(mutation.get("ambiguous", False)),
        len(set(mutation.get("carriers", []))),
    ]


def _is_red_win(receipt: Mapping[str, Any]) -> bool:
    """A Red win requires product execution, not harness or launch failure."""

    execution = receipt.get("execution") or {}
    return execution.get("kind") == "ACCEPT" and receipt.get("verdict") != "PASS"


def _functional_control_passed(receipt: Mapping[str, Any]) -> bool:
    execution = receipt.get("execution") or {}
    functional_judge = receipt.get("functional_judge") or {}
    return (
        receipt.get("verdict") == "PASS"
        and execution.get("kind") == "ACCEPT"
        and functional_judge.get("status") == "PASS"
    )


def _replay_promoted_regressions(
    *, source_receipt_path: Path, target_run_cmd: str, judge: Path,
    functional_judge: Path, out_dir: Path, authorization: Mapping[str, Any],
    execution_target: str,
) -> dict[str, Any]:
    source_receipt_path = source_receipt_path.resolve()
    source = json.loads(source_receipt_path.read_text(encoding="utf-8"))
    if source.get("schema") != SCHEMA:
        raise ValueError("Blue replay source must be an adaptive Battle receipt")
    unresolved = set(source.get("unresolved_red_wins") or [])
    promotions = source.get("promotions")
    if not isinstance(promotions, list) or not promotions:
        raise ValueError("Blue replay source has no promoted Red regressions")
    mutation_ids = [str(item.get("mutation_id")) for item in promotions]
    if set(mutation_ids) != unresolved:
        raise ValueError("Blue replay promotions must exactly match unresolved Red wins")

    replay_root = out_dir / "blue-replay"
    frozen_cases = []
    retained_copies = []
    for promotion in promotions:
        fixture = Path(str(promotion.get("retained_fixture", ""))).resolve()
        promotion_receipt = fixture / "promotion-receipt.json"
        if not fixture.is_dir() or not promotion_receipt.is_file():
            raise ValueError(f"retained regression is unavailable: {fixture}")
        replay_fixture = replay_root / "inputs" / str(promotion["mutation_id"])
        shutil.copytree(fixture, replay_fixture)
        retained_copies.append(
            {
                "mutation_id": str(promotion["mutation_id"]),
                "source_fixture": str(fixture),
                "source_promotion_receipt_sha256": _sha(promotion_receipt),
                "replay_fixture": str(replay_fixture),
            }
        )
        frozen_cases.append(
            {
                "id": str(promotion["mutation_id"]),
                "input_dir": str(replay_fixture),
                "declared_expectation": "MUST_REJECT",
                "effective_expectation": "MUST_REJECT",
                "expectation_source": "promoted_red_regression",
            }
        )

    result = run_campaign(
        "", target_run_cmd, str(judge),
        judge_params={"output_subdir": "corpus"},
        functional_judge=str(functional_judge), work_root=replay_root,
        frozen_cases=frozen_cases,
        attestation_context={
            "authorization_manifest_sha256": authorization.get("manifest_sha256"),
            "resolved_image": execution_target,
        },
    )
    campaign = result.to_dict()
    campaign_path = _write(replay_root / "campaign-result.json", campaign)
    cases = campaign.get("case_receipts") or []
    passed_ids = [
        str(item["case_id"])
        for item in cases
        if item.get("verdict") == "PASS"
        and (item.get("execution") or {}).get("kind") == "CONTRACT_REJECT"
    ]
    replay = {
        "schema": "battle.adaptive_blue_replay.v1",
        "status": "PASS" if len(passed_ids) == len(frozen_cases) else "FAIL",
        "source_red_receipt": str(source_receipt_path),
        "source_red_receipt_sha256": _sha(source_receipt_path),
        "source_unresolved_red_wins": sorted(unresolved),
        "requested_cases": mutation_ids,
        "retained_fixture_copies": retained_copies,
        "replayed_cases": [str(item.get("case_id")) for item in cases],
        "passed_cases": passed_ids,
        "failed_cases": [
            str(item.get("case_id")) for item in cases if str(item.get("case_id")) not in passed_ids
        ],
        "campaign_receipt": str(campaign_path),
        "judge": {"path": str(judge.resolve()), "sha256": _sha(judge)},
        "functional_judge": {
            "path": str(functional_judge.resolve()), "sha256": _sha(functional_judge)
        },
    }
    replay_path = _write(replay_root / "blue-replay-receipt.json", replay)
    replay["receipt"] = str(replay_path)
    return replay


class _ContextualOracle:
    """Select a survivor from objective Judge receipts, including defended probes."""

    oracle_id = "battle-contextual-invariant-judge-oracle-v1"

    def select(
        self, judged_population: JudgedPopulation, *, request: GenerationRequest,
        recall: RecallResult,
    ) -> OracleSelection:
        ranked = sorted(
            judged_population.judged.items,
            key=lambda item: (tuple(-int(value) for value in item["score"]), candidate_id(item)),
        )
        survivor = ranked[0]
        survivor_id = candidate_id(survivor)
        scores = [
            {
                "candidate_id": candidate_id(item),
                "score": list(item["score"]),
                "judge_verdict": item["judge_verdict"],
                "case_receipt_sha256": item["case_receipt_sha256"],
            }
            for item in judged_population.judged.items
        ]
        return OracleSelection(
            survivor=survivor,
            bad_genetic_material={
                "schema": "battle.contextual_invariant_bad_material.v1",
                "specimens": [
                    {
                        "candidate_id": candidate_id(item),
                        "source": self.oracle_id,
                        "reason_codes": ["not_selected_by_judge_score"],
                        "evidence": {"score": list(item["score"])},
                    }
                    for item in judged_population.judged.items
                    if candidate_id(item) != survivor_id
                ],
            },
            evidence={
                "source": "independent_invariant_judge_case_receipts",
                "candidate_scores": scores,
                "selected_candidate_id": survivor_id,
                "memory_recall_receipt": recall.receipt,
            },
            oracle_id=self.oracle_id,
        )


class _ContextualHooks:
    def __init__(
        self, *, candidates: list[dict[str, Any]], adapter: Any, out_dir: Path,
        target_run_cmd: str, judge: Path, functional_judge: Path,
        authorization: Mapping[str, Any], execution_target: str,
    ) -> None:
        self.candidates = candidates
        self.adapter = adapter
        self.out_dir = out_dir
        self.target_run_cmd = target_run_cmd
        self.judge_path = judge
        self.functional_judge = functional_judge
        self.authorization = authorization
        self.execution_target = execution_target
        self.evaluated: list[dict[str, Any]] = []

    def synthesize(self, *, request: GenerationRequest, recall: RecallResult) -> PopulationStageResult:
        return PopulationStageResult(items=tuple(self.candidates))

    def review(
        self, *, request: GenerationRequest, recall: RecallResult,
        generated: PopulationStageResult,
    ) -> PopulationStageResult:
        reviewed = []
        for item in generated.items:
            mutation = item.get("mutation")
            if not isinstance(mutation, Mapping):
                raise ValueError("adaptive candidate mutation must be an object")
            reviewed.append({**dict(item), "review_status": "PASS"})
        return PopulationStageResult(items=tuple(reviewed))

    def judge(
        self, *, request: GenerationRequest, recall: RecallResult,
        generated: PopulationStageResult, reviewed: PopulationStageResult,
    ) -> PopulationStageResult:
        judged = []
        for candidate in reviewed.items:
            item = self._execute_candidate(dict(candidate), request.generation)
            self.evaluated.append(item)
            judged.append(item)
        return PopulationStageResult(items=tuple(judged))

    def _execute_candidate(self, candidate: dict[str, Any], generation: int) -> dict[str, Any]:
        mutation_id = str(candidate["candidate_id"])
        mutation = dict(candidate["mutation"])
        campaign_root = self.out_dir / "campaigns" / mutation_id
        case_root = campaign_root / "inputs" / mutation_id
        case = self.adapter.materialize_adaptive_case(case_root, mutation, mutation_id)
        materialization = self.adapter.adaptive_validate_materialized_case(case_root, mutation)
        if materialization.get("status") != "PASS":
            raise ValueError(f"adaptive materialization invalid: {materialization}")
        materialization_path = _write(
            campaign_root / "materialization-receipt.json", materialization
        )
        safe_root = campaign_root / "inputs" / f"{mutation_id}-safe-control"
        safe_mutation = self.adapter.adaptive_safe_control(mutation)
        safe = self.adapter.materialize_adaptive_case(
            safe_root, safe_mutation, f"{mutation_id}-safe-control"
        )
        safe_validation = self.adapter.adaptive_validate_materialized_case(
            safe_root, safe_mutation
        )
        if safe_validation.get("status") != "PASS":
            raise ValueError(f"adaptive safe control invalid: {safe_validation}")
        result = run_campaign(
            "", self.target_run_cmd, str(self.judge_path),
            judge_params={"output_subdir": "corpus"},
            functional_judge=str(self.functional_judge), work_root=campaign_root,
            frozen_cases=[
                {"id": case[0], "input_dir": case[1], "declared_expectation": case[2], "effective_expectation": case[2], "expectation_source": "adaptive_adapter"},
                {"id": safe[0], "input_dir": safe[1], "declared_expectation": safe[2], "effective_expectation": safe[2], "expectation_source": "adaptive_adapter"},
            ],
            attestation_context={
                "authorization_manifest_sha256": self.authorization.get("manifest_sha256"),
                "resolved_image": self.execution_target,
            },
        )
        campaign = result.to_dict()
        campaign_path = _write(campaign_root / "campaign-result.json", campaign)
        case_receipt = next(
            item for item in campaign["case_receipts"] if item["case_id"] == case[0]
        )
        safe_receipt = next(
            item for item in campaign["case_receipts"] if item["case_id"] == safe[0]
        )
        score = _score(case_receipt, mutation)
        execution_errors = [
            {
                "case_id": receipt.get("case_id"),
                "execution_kind": (receipt.get("execution") or {}).get("kind"),
                "exit_code": (receipt.get("execution") or {}).get("exit_code"),
            }
            for receipt in campaign["case_receipts"]
            if (receipt.get("execution") or {}).get("kind") not in {"ACCEPT", "CONTRACT_REJECT"}
        ]
        red_win = _is_red_win(case_receipt)
        return {
            **candidate,
            "generation": generation,
            "score": score,
            "red_win": red_win,
            "judge_verdict": "RED_SUCCESS" if red_win else "DEFENDED",
            "case_receipt": case_receipt,
            "functional_control_receipt": safe_receipt,
            "functional_control_passed": _functional_control_passed(safe_receipt),
            "case_receipt_sha256": "sha256:" + hashlib.sha256(
                json.dumps(case_receipt, sort_keys=True).encode()
            ).hexdigest(),
            "campaign_receipt": str(campaign_path),
            "input_dir": str(case_root),
            "materialization_receipt": str(materialization_path),
            "execution_errors": execution_errors,
        }


def run_adaptive_invariant_battle(
    *, adapter_path: Path, target_run_cmd: str, judge: Path,
    functional_judge: Path, authorization_manifest: Path, target_identity: str,
    execution_target: str, out_dir: Path, parent_run_id: str,
    generations: int = 3, population: int = 2,
    params: dict[str, Any] | None = None,
    blue_replay_from: Path | None = None,
) -> dict[str, Any]:
    if generations < 2 or population < 1:
        raise ValueError("adaptive Battle requires generations >= 2 and population >= 1")
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    authorization = validate_target_authorization(
        authorization_manifest,
        expected_target=target_identity,
        expected_execution_target=execution_target,
        requested_action="battle",
        requested_runtime_mode="docker",
        requested_probe_class="adaptive-invariant",
        receipt_out=out_dir / "authorization-validation.json",
    )
    if authorization.get("status") != "PASS":
        receipt = {"schema": SCHEMA, "status": "BLOCKED", "release_blocked": True,
                   "reason": "target_authorization_failed", "authorization": authorization,
                   "execution_started": False}
        _write(out_dir / "adaptive-battle-receipt.json", receipt)
        return receipt

    adapter = _load(adapter_path)
    battle_id = f"adaptive-{parent_run_id}"
    parameters = dict(params or {})
    candidates = list(adapter.adaptive_seeds(parameters))[:population]
    if not candidates:
        raise ValueError("adaptive adapter returned no seeds")
    generation_receipts: list[dict[str, Any]] = []
    all_red_wins: list[dict[str, Any]] = []
    parent: dict[str, Any] | None = None
    started = time.time()
    memory = MemoryBackend(
        base_url=str(parameters.get("memory_base_url", "http://127.0.0.1:8601")),
        collection=str(parameters.get("memory_collection", "battle_lineage_graph")),
    )

    for generation in range(1, generations + 1):
        if generation > 1:
            candidates = list(adapter.adaptive_mutations(parent["mutation"], parent["case_receipt"], parameters))[:population]
            if not candidates:
                raise ValueError(f"adaptive adapter returned no generation-{generation} mutations")
        candidate_records = [
            {
                "candidate_id": f"g{generation:02d}-m{index:02d}",
                "mutation_id": f"g{generation:02d}-m{index:02d}",
                "parent_mutation_id": parent["mutation_id"] if parent else None,
                "mutation": mutation,
            }
            for index, mutation in enumerate(candidates, 1)
        ]
        hooks = _ContextualHooks(
            candidates=candidate_records, adapter=adapter, out_dir=out_dir,
            target_run_cmd=target_run_cmd, judge=judge,
            functional_judge=functional_judge, authorization=authorization,
            execution_target=execution_target,
        )
        engine = AdaptiveLineageEngine(
            population_hooks=hooks, oracle=_ContextualOracle(), memory_hooks=memory
        )
        engine_receipt = engine.run_generation(
            GenerationRequest(
                battle_id=battle_id, lineage_id=f"{battle_id}-contextual-graph",
                run_id=parent_run_id, role="red", generation=generation,
                population_size=len(candidate_records), max_generations=generations,
                max_recall_hops=max(2, max(int(item["mutation"].get("hop_count", 2)) for item in candidate_records)),
                out_dir=out_dir / "lineage", subgraph_scope={"role": "red"},
                context={
                    "recall_query": f"contextual graph generation {generation} Judge outcomes",
                    "parent_mutation_id": parent["mutation_id"] if parent else None,
                },
            )
        )
        evaluated = hooks.evaluated
        all_red_wins.extend(item for item in evaluated if item["red_win"])
        selected_id = (engine_receipt.get("oracle") or {}).get("survivor_id")
        selected = next(item for item in evaluated if item["mutation_id"] == selected_id)
        parent = selected
        generation_receipts.append(
            {
                "generation": generation,
                "parent_mutation_id": candidate_records[0]["parent_mutation_id"],
                "mutations": evaluated,
                "selection_receipt": engine_receipt["receipt_path"],
                "selection_schema": engine_receipt["schema"],
                "memory_writeback": engine_receipt.get("reproduction"),
            }
        )

    promotions = []
    for win in all_red_wins:
        destination = out_dir / "promoted-regressions" / win["mutation_id"]
        shutil.copytree(Path(win["input_dir"]), destination)
        promotion = {"schema": "battle.deterministic_regression_promotion.v1", "status": "PROMOTED",
                     "mutation_id": win["mutation_id"], "source_generation": win["generation"],
                     "source_case_receipt": win["campaign_receipt"], "retained_fixture": str(destination)}
        promotion_path = _write(destination / "promotion-receipt.json", promotion)
        promotion["receipt"] = str(promotion_path)
        promotions.append(promotion)

    unresolved_red_wins = [item["mutation_id"] for item in all_red_wins]
    execution_errors = [
        {"mutation_id": item["mutation_id"], **error}
        for generation in generation_receipts
        for item in generation["mutations"]
        for error in item["execution_errors"]
    ]
    functional_failures = [
        item["mutation_id"]
        for generation in generation_receipts
        for item in generation["mutations"]
        if not item["functional_control_passed"]
    ]
    blue_replay = (
        _replay_promoted_regressions(
            source_receipt_path=blue_replay_from, target_run_cmd=target_run_cmd,
            judge=judge, functional_judge=functional_judge, out_dir=out_dir,
            authorization=authorization, execution_target=execution_target,
        )
        if blue_replay_from is not None
        else None
    )
    replay_failed = blue_replay is not None and blue_replay["status"] != "PASS"
    replayed_blue_fixes = len(blue_replay["passed_cases"]) if blue_replay else 0
    status = (
        "ERROR" if execution_errors
        else "FAIL" if all_red_wins or functional_failures or replay_failed
        else "PASS"
    )
    release_blocked = status != "PASS"
    blue_fix_claimed = bool(
        blue_replay is not None
        and blue_replay["status"] == "PASS"
        and not all_red_wins
        and not functional_failures
        and not execution_errors
    )
    receipt = {
        "schema": SCHEMA, "status": status,
        "release_blocked": release_blocked, "battle_id": battle_id,
        "parent_run_id": parent_run_id, "target_binding": target_identity,
        "runtime_binding": {"mode": "docker", "execution_target": execution_target,
                            "target_run_cmd_sha256": "sha256:" + hashlib.sha256(target_run_cmd.encode()).hexdigest()},
        "authorization_receipt": str(out_dir / "authorization-validation.json"),
        "adapter": {"path": str(adapter_path.resolve()), "sha256": _sha(adapter_path)},
        "judge": {"path": str(judge.resolve()), "sha256": _sha(judge)},
        "functional_judge": {"path": str(functional_judge.resolve()), "sha256": _sha(functional_judge)},
        "generations": generation_receipts,
        "counts": {"generations": generations, "executed_mutations": sum(len(g["mutations"]) for g in generation_receipts),
                   "judge_confirmed_red_wins": len(all_red_wins), "promoted_deterministic_cases": len(promotions),
                   "replayed_blue_fixes": replayed_blue_fixes,
                   "functional_controls": sum(len(g["mutations"]) for g in generation_receipts),
                   "functional_control_failures": len(functional_failures)},
        "promotions": promotions, "blue_fix_replays": [blue_replay] if blue_replay else [],
        "blue_fix_status": blue_replay["status"] if blue_replay else ("NOT_ATTEMPTED" if all_red_wins else "NOT_REQUIRED"),
        "unresolved_red_wins": unresolved_red_wins,
        "execution_errors": execution_errors,
        "functional_control_failures": functional_failures,
        "release_gate": {
            "triggered_by_red_wins": bool(all_red_wins),
            "unresolved_red_win_count": len(unresolved_red_wins),
            "triggered_by_execution_errors": bool(execution_errors),
            "triggered_by_functional_failures": bool(functional_failures),
            "triggered_by_blue_replay_failure": replay_failed,
            "decision": "BLOCK" if release_blocked else "ALLOW",
        },
        "proof_boundary": {"mocked": False, "live": not execution_errors, "target_path": "canonical Docker publication command",
                           "blue_fix_claimed": blue_fix_claimed,
                           "adaptive_contract": "battle.universal_adaptive_lineage_generation.v2",
                           "memory_boundary": "live_http_daemon"},
        "started_at_epoch": started, "finished_at_epoch": time.time(),
    }
    _write(out_dir / "adaptive-battle-receipt.json", receipt)
    _write(out_dir / "adaptive-battle-report.json", {**receipt, "schema": "battle.adaptive_invariant_report.v1"})
    return receipt


def _cli(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run a Judge-directed adaptive invariant Battle")
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--target-run-cmd", required=True)
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--functional-judge", type=Path, required=True)
    parser.add_argument("--authorization-manifest", type=Path, required=True)
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--execution-target", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parent-run-id", required=True)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--population", type=int, default=2)
    parser.add_argument("--params", default="{}")
    parser.add_argument("--blue-replay-from", type=Path)
    args = parser.parse_args(argv)
    receipt = run_adaptive_invariant_battle(
        adapter_path=args.adapter, target_run_cmd=args.target_run_cmd,
        judge=args.judge, functional_judge=args.functional_judge,
        authorization_manifest=args.authorization_manifest,
        target_identity=args.target_identity, execution_target=args.execution_target,
        out_dir=args.out, parent_run_id=args.parent_run_id,
        generations=args.generations, population=args.population,
        params=json.loads(args.params),
        blue_replay_from=args.blue_replay_from,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
