"""Battle child exploit DAG node adapter invoked by Tau command specs."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .child_dag_code_author import run_code_author
from .child_dag_method_combiner import run_method_combiner
from .child_dag_research import run_research_scout
from .compile_repair import bounded_compile_repair
from .provider_continuity import build_provider_artifact_continuity_receipt


NODE_RECEIPT_SCHEMA = "battle.child_dag_node_receipt.v1"

NEXT_NODE = {
    "lineage-summarizer": "research-scout",
    "research-scout": "method-combiner",
    "method-combiner": "exploit-code-author",
    "exploit-code-author": "compile-repair",
    "compile-repair": "artifact-reviewer",
    "artifact-reviewer": "battle-handoff-writer",
    "battle-handoff-writer": "human",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: python -m battle_skill.child_dag_node_adapter <node-id>",
            file=sys.stderr,
        )
        return 2
    node_id = args[0]
    try:
        start_payload = _read_stdin_handoff()
        artifact_dir = _artifact_dir()
        response, exit_code = run_node(
            node_id=node_id, start_payload=start_payload, artifact_dir=artifact_dir
        )
        if response is not None:
            print(json.dumps(response, sort_keys=True))
        return exit_code
    except Exception as exc:  # pragma: no cover - fail-closed command boundary.
        print(f"battle child DAG node adapter failed: {exc}", file=sys.stderr)
        return 1


def run_node(
    *, node_id: str, start_payload: dict[str, Any], artifact_dir: Path
) -> tuple[dict[str, Any] | None, int]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    selected = os.environ.get("TAU_HANDOFF_SELECTED_AGENT") or node_id
    if selected != node_id:
        receipt = _node_receipt(
            node_id=node_id,
            status="BLOCKED",
            verdict="SELECTED_AGENT_MISMATCH",
            evidence=[],
        )
        receipt["errors"] = [
            f"selected agent {selected!r} did not match adapter node {node_id!r}"
        ]
        _write_json(artifact_dir / f"{node_id}-node-receipt.json", receipt)
        return None, 1

    if node_id == "lineage-summarizer":
        return _run_lineage_summarizer(
            start_payload=start_payload, artifact_dir=artifact_dir
        ), 0
    if node_id == "research-scout":
        response = _run_research_scout(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0 if response is not None else 1
    if node_id == "method-combiner":
        response = _run_method_combiner(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0 if response is not None else 1
    if node_id == "exploit-code-author":
        response = _run_exploit_code_author(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0 if response is not None and response.get("result", {}).get(
            "status"
        ) == "PASS" else 1
    if node_id == "compile-repair":
        response = _run_compile_repair(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0
    if node_id == "artifact-reviewer":
        response = _run_artifact_reviewer(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0
    if node_id == "battle-handoff-writer":
        response = _run_battle_handoff_writer(
            start_payload=start_payload, artifact_dir=artifact_dir
        )
        return response, 0

    return _blocked_missing_adapter(
        node_id=node_id, start_payload=start_payload, artifact_dir=artifact_dir
    ), 1


def _run_lineage_summarizer(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any]:
    dag_path = _dag_contract_path(start_payload)
    spawn_root = dag_path.parent if dag_path is not None else None
    child_packet_path = (
        spawn_root / "child-knowledge-packet.json" if spawn_root is not None else None
    )
    spawn_policy_path = (
        spawn_root / "spawn-policy-decision.json" if spawn_root is not None else None
    )
    child_packet = (
        _read_json(child_packet_path)
        if child_packet_path is not None and child_packet_path.exists()
        else {}
    )
    spawn_policy = (
        _read_json(spawn_policy_path)
        if spawn_policy_path is not None and spawn_policy_path.exists()
        else {}
    )

    lineage_summary = {
        "schema": "battle.child_lineage_summary.v1",
        "status": "PASS",
        "mocked": False,
        "live": "tau_command_spec_node",
        "agentic": False,
        "battle_id": child_packet.get("battle_id")
        or _target_value(start_payload, "battle_id"),
        "parent_lane_id": child_packet.get("parent_lane_id"),
        "child_lane_id": child_packet.get("child_lane_id"),
        "parent_specimen_count": len(child_packet.get("parent_specimens", []))
        if isinstance(child_packet.get("parent_specimens"), list)
        else 0,
        "inherited_methods": child_packet.get("inherited_methods", []),
        "hypotheses": child_packet.get("hypotheses", []),
        "blocked_ideas": child_packet.get("blocked_ideas", []),
        "next_research_questions": child_packet.get("next_research_questions", []),
        "spawn_policy_decision": spawn_policy.get("decision"),
        "claims": {
            "proves": [
                "Battle child DAG lineage-summarizer read the parent-approved child knowledge packet."
            ],
            "does_not_prove": [
                "Tau researched new sources.",
                "Tau generated exploit code.",
                "Exploit success.",
            ],
        },
    }
    lineage_path = artifact_dir / "lineage_summary.json"
    _write_json(lineage_path, lineage_summary)

    evidence = [_evidence("lineage_summary.json", lineage_path)]
    if child_packet_path is not None and child_packet_path.exists():
        evidence.append(_evidence("child_knowledge_packet.json", child_packet_path))
    receipt = _node_receipt(
        node_id="lineage-summarizer", status="PASS", verdict="PASS", evidence=evidence
    )
    receipt["source_artifacts"] = {
        "dag_contract": str(dag_path) if dag_path is not None else None,
        "child_knowledge_packet": str(child_packet_path)
        if child_packet_path is not None
        else None,
        "spawn_policy_decision": str(spawn_policy_path)
        if spawn_policy_path is not None
        else None,
    }
    receipt_path = artifact_dir / "lineage-summarizer-node-receipt.json"
    _write_json(receipt_path, receipt)
    evidence.append(_evidence("lineage-summarizer-node-receipt.json", receipt_path))
    return _handoff(
        start_payload=start_payload,
        previous_subagent="lineage-summarizer",
        status="PASS",
        summary="Lineage summary created from parent-approved child knowledge packet.",
        evidence=evidence,
        next_agent="research-scout",
        artifacts=[str(lineage_path), str(receipt_path)],
    )


def _blocked_missing_adapter(
    *, node_id: str, start_payload: dict[str, Any], artifact_dir: Path
) -> None:
    verdict = _blocked_verdict(node_id)
    receipt = _node_receipt(
        node_id=node_id, status="BLOCKED", verdict=verdict, evidence=[]
    )
    receipt["reason"] = _blocked_reason(node_id)
    receipt["claims"] = {
        "proves": [f"Battle reached the {node_id} command-spec boundary."],
        "does_not_prove": [
            "A real adapter produced this node's required artifacts.",
            "Exploit success.",
        ],
    }
    _write_json(artifact_dir / f"{node_id}-node-receipt.json", receipt)
    return None


def _run_exploit_code_author(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any] | None:
    lineage_path = _find_named_artifact(start_payload, "lineage_summary.json")
    research_path = _find_named_artifact(start_payload, "research_receipts.json")
    candidates_path = _find_named_artifact(start_payload, "candidate_methods.json")
    genome_path = _find_named_artifact(start_payload, "exploit_genome.json")
    child_packet_path = _find_named_artifact(
        start_payload, "child-knowledge-packet.json"
    )
    missing = [
        name
        for name, path in {
            "lineage_summary.json": lineage_path,
            "research_receipts.json": research_path,
            "candidate_methods.json": candidates_path,
            "exploit_genome.json": genome_path,
        }.items()
        if path is None
    ]
    if missing:
        receipt = _node_receipt(
            node_id="exploit-code-author",
            status="BLOCKED",
            verdict="UPSTREAM_CODE_AUTHOR_INPUT_MISSING",
            evidence=[],
        )
        receipt["reason"] = (
            f"exploit-code-author missing upstream artifacts: {', '.join(missing)}"
        )
        receipt["claims"] = {
            "proves": ["Battle reached the exploit-code-author command-spec boundary."],
            "does_not_prove": ["A provider authored exploit code.", "Exploit success."],
        }
        _write_json(artifact_dir / "exploit-code-author-node-receipt.json", receipt)
        return None

    result = run_code_author(
        artifact_dir=artifact_dir,
        lineage_summary_path=lineage_path,
        research_receipts_path=research_path,
        candidate_methods_path=candidates_path,
        exploit_genome_path=genome_path,
        child_knowledge_packet_path=child_packet_path,
        dag_id=_target_value(start_payload, "dag_id"),
        goal_hash=_goal_hash(start_payload),
    )
    evidence = [
        _evidence("lineage_summary.json", lineage_path),
        _evidence("research_receipts.json", research_path),
        _evidence("candidate_methods.json", candidates_path),
        _evidence("exploit_genome.json", genome_path),
        _evidence(
            "exploit-code-author-work-order.json", result["battle_work_order_path"]
        ),
        _evidence("tau-scillm-worker-work-order.json", result["tau_work_order_path"]),
        _evidence(
            "tau-scillm-worker-launch-receipt.json", result["launch_receipt_path"]
        ),
        _evidence(
            "provider-artifact-validation.json", result["artifact_validation_path"]
        ),
        _evidence(
            "provider-authorship-receipt.json", result["provider_authorship_path"]
        ),
        _evidence(
            "provider-code-author-boundary-receipt.json",
            result["boundary_receipt_path"],
        ),
    ]
    if result.get("worker_result_path") is not None:
        evidence.append(
            _evidence("provider-worker-result.json", result["worker_result_path"])
        )
    if result.get("validation_receipt_path") is not None:
        evidence.append(
            _evidence(
                "tau-worker-validation-receipt.json", result["validation_receipt_path"]
            )
        )
    if result.get("code_path") is not None:
        evidence.append(_evidence("exploit_specimen.py", result["code_path"]))
    if result.get("specimen_path") is not None:
        evidence.append(_evidence("specimen.json", result["specimen_path"]))

    status = result["status"]
    receipt = _node_receipt(
        node_id="exploit-code-author",
        status=status,
        verdict=result["verdict"],
        evidence=evidence,
    )
    receipt["provider_live"] = result["authorship"].get("provider_live")
    receipt["agentic"] = bool(result["authorship"].get("agentic"))
    receipt["execution_mode"] = result["authorship"].get("execution_mode")
    receipt["authored_by"] = result["authorship"].get("authored_by")
    receipt["errors"] = result["authorship"].get("errors", [])
    receipt["claims"] = result["claims"]
    receipt_path = artifact_dir / "exploit-code-author-node-receipt.json"
    _write_json(receipt_path, receipt)

    if status != "PASS":
        return _handoff(
            start_payload=start_payload,
            previous_subagent="exploit-code-author",
            status="BLOCKED",
            summary=f"Exploit Code Author blocked at {result['verdict']}.",
            evidence=[
                *evidence,
                _evidence("exploit-code-author-node-receipt.json", receipt_path),
            ],
            next_agent="compile-repair",
            artifacts=[str(item["path"]) for item in evidence if item.get("path")],
        )

    return _handoff(
        start_payload=start_payload,
        previous_subagent="exploit-code-author",
        status="PASS",
        summary="Exploit Code Author materialized provider-authored exploit specimen artifact.",
        evidence=[
            *evidence,
            _evidence("exploit-code-author-node-receipt.json", receipt_path),
        ],
        next_agent="compile-repair",
        artifacts=[
            str(result["code_path"]),
            str(result["specimen_path"]),
            str(result["provider_authorship_path"]),
            str(result["boundary_receipt_path"]),
            str(receipt_path),
        ],
    )


def _run_research_scout(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any] | None:
    lineage_path = _find_named_artifact(start_payload, "lineage_summary.json")
    if lineage_path is None:
        receipt = _node_receipt(
            node_id="research-scout",
            status="BLOCKED",
            verdict="RESEARCH_SOURCE_INPUT_MISSING",
            evidence=[],
        )
        receipt["reason"] = (
            "research-scout requires lineage_summary.json from the live Tau command loop."
        )
        receipt["claims"] = {
            "proves": ["Battle reached the research-scout command-spec boundary."],
            "does_not_prove": [
                "Research Scout produced source-bearing receipts.",
                "Exploit success.",
            ],
        }
        _write_json(artifact_dir / "research-scout-node-receipt.json", receipt)
        return None

    result = run_research_scout(
        artifact_dir=artifact_dir, lineage_summary_path=lineage_path
    )
    evidence = [
        _evidence("lineage_summary.json", lineage_path),
        _evidence("research-source-packet.json", result["source_packet_path"]),
        _evidence("research-source-receipt.json", result["source_receipt_path"]),
        _evidence("research_receipts.json", result["research_receipts_path"]),
        _evidence("candidate_methods.json", result["candidate_methods_path"]),
    ]
    receipt = _node_receipt(
        node_id="research-scout",
        status=result["status"],
        verdict=result["status"],
        evidence=evidence,
    )
    receipt["tau_research_source_receipt"] = str(result["source_receipt_path"])
    receipt["claims"] = result["research_receipts"]["claims"]
    receipt_path = artifact_dir / "research-scout-node-receipt.json"
    _write_json(receipt_path, receipt)
    if result["status"] != "PASS":
        return None
    evidence.append(_evidence("research-scout-node-receipt.json", receipt_path))
    return _handoff(
        start_payload=start_payload,
        previous_subagent="research-scout",
        status="PASS",
        summary="Research Scout produced Tau-validated source-bearing design-input receipts.",
        evidence=evidence,
        next_agent="method-combiner",
        artifacts=[
            str(result["research_receipts_path"]),
            str(result["candidate_methods_path"]),
            str(receipt_path),
        ],
    )


def _run_method_combiner(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any] | None:
    research_path = _find_named_artifact(start_payload, "research_receipts.json")
    candidates_path = _find_named_artifact(start_payload, "candidate_methods.json")
    missing = [
        name
        for name, path in {
            "research_receipts.json": research_path,
            "candidate_methods.json": candidates_path,
        }.items()
        if path is None
    ]
    if missing:
        receipt = _node_receipt(
            node_id="method-combiner",
            status="BLOCKED",
            verdict="UPSTREAM_RESEARCH_ARTIFACT_MISSING",
            evidence=[],
        )
        receipt["reason"] = (
            f"method-combiner missing upstream artifacts: {', '.join(missing)}"
        )
        receipt["claims"] = {
            "proves": ["Battle reached the method-combiner command-spec boundary."],
            "does_not_prove": [
                "A source-backed exploit genome was produced.",
                "Exploit success.",
            ],
        }
        _write_json(artifact_dir / "method-combiner-node-receipt.json", receipt)
        return None

    result = run_method_combiner(
        artifact_dir=artifact_dir,
        research_receipts_path=research_path,
        candidate_methods_path=candidates_path,
    )
    evidence = [
        _evidence("research_receipts.json", research_path),
        _evidence("candidate_methods.json", candidates_path),
        _evidence("exploit_genome.json", result["genome_path"]),
        _evidence("combination_rationale.md", result["rationale_path"]),
    ]
    receipt = _node_receipt(
        node_id="method-combiner",
        status=result["status"],
        verdict=result["status"],
        evidence=evidence,
    )
    receipt["claims"] = result["genome"]["claims"]
    receipt["errors"] = result["errors"]
    receipt_path = artifact_dir / "method-combiner-node-receipt.json"
    _write_json(receipt_path, receipt)
    if result["status"] != "PASS":
        return None
    evidence.append(_evidence("method-combiner-node-receipt.json", receipt_path))
    return _handoff(
        start_payload=start_payload,
        previous_subagent="method-combiner",
        status="PASS",
        summary="Method Combiner produced a deterministic source-backed exploit genome candidate.",
        evidence=evidence,
        next_agent="exploit-code-author",
        artifacts=[
            str(result["genome_path"]),
            str(result["rationale_path"]),
            str(receipt_path),
        ],
    )


def _run_compile_repair(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any]:
    code_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "exploit_specimen.py"
    )
    if code_path is None:
        receipt = _node_receipt(
            node_id="compile-repair",
            status="BLOCKED",
            verdict="UPSTREAM_CODE_ARTIFACT_MISSING",
            evidence=[],
        )
        receipt["reason"] = (
            "compile-repair requires exploit_specimen.py from the prior provider-authored node receipt evidence."
        )
        receipt["claims"] = {
            "proves": ["Battle reached the compile-repair command-spec boundary."],
            "does_not_prove": [
                "A provider-authored exploit specimen was found.",
                "Code compiled.",
                "Exploit success.",
            ],
        }
        receipt_path = artifact_dir / "compile-repair-node-receipt.json"
        _write_json(receipt_path, receipt)
        return _handoff(
            start_payload=start_payload,
            previous_subagent="compile-repair",
            status="BLOCKED",
            summary="Compile Repair blocked because exploit_specimen.py was not present in upstream evidence.",
            evidence=[_evidence("compile-repair-node-receipt.json", receipt_path)],
            next_agent="blocked",
            artifacts=[str(receipt_path)],
        )

    authorship_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "provider-authorship-receipt.json"
    )
    specimen_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "specimen.json"
    )
    authorship = _read_json(authorship_path) if authorship_path else {}
    specimen = _read_json(specimen_path) if specimen_path else {}
    final_path = artifact_dir / "repaired_exploit_specimen.py"
    docker_image = os.environ.get("BATTLE_CHILD_COMPILE_IMAGE", "python:3.12-slim")
    provider_route = {
        "requested_provider": authorship.get("requested_provider"),
        "requested_model": authorship.get("requested_model"),
        "observed_provider": authorship.get("observed_provider"),
        "observed_model": authorship.get("observed_model"),
        "observed_identity_fallback_used": authorship.get("observed_identity_fallback_used"),
        "fallback_history": authorship.get("observed_identity_fallbacks", []),
    }
    repair_receipt = bounded_compile_repair(
        source_path=code_path,
        out_dir=artifact_dir,
        final_path=final_path,
        max_compile_repairs=int(os.environ.get("BATTLE_MAX_COMPILE_REPAIRS", "3")),
        docker_image=docker_image,
        provider_route=provider_route if any(provider_route.values()) else None,
    )
    compile_receipt = repair_receipt.get("final_compile_receipt") or {}
    compile_receipt_path = artifact_dir / "compile_receipt.json"
    repair_receipt_path = artifact_dir / "provider_compile_repair_receipt.json"
    errors = [str(item) for item in repair_receipt.get("errors", [])]
    continuity_errors: list[str] = []
    source_sha = _sha256_file(code_path)
    selected_sha = _sha256_file(final_path) if final_path.exists() else None
    if authorship and not _same_sha(authorship.get("code_artifact_sha256"), source_sha):
        continuity_errors.append("AUTHORSHIP_SOURCE_SHA_MISMATCH")
    if specimen and not _same_sha(specimen.get("code_sha256"), source_sha):
        continuity_errors.append("SPECIMEN_SOURCE_SHA_MISMATCH")
    final_status = str(repair_receipt.get("final_status"))
    status = "PASS" if final_status == "PASS" and not continuity_errors else "BLOCKED"
    verdict = "PASS" if status == "PASS" else final_status if final_status != "PASS" else "PROVIDER_CONTINUITY_FAILED"
    errors.extend(continuity_errors)

    evidence = [
        _evidence("exploit_specimen.py", code_path),
        _evidence("provider_compile_repair_receipt.json", repair_receipt_path),
    ]
    if final_path.exists():
        evidence.append(_evidence("repaired_exploit_specimen.py", final_path))
    if compile_receipt_path.exists():
        evidence.append(_evidence("compile_receipt.json", compile_receipt_path))
    for name in ("compile.stdout.txt", "compile.stderr.txt"):
        path = artifact_dir / "repair-attempts" / "1" / name
        if path.exists():
            evidence.append(_evidence(name, path))

    claims = {
        "proves": [
            "Battle bounded provider compile repair, retained failed specimens, and selected only a compile-PASS artifact."
        ]
        if status == "PASS"
        else [
            "Battle retained a fail-closed provider compile-repair receipt and did not advance to Judge."
        ],
        "does_not_prove": [
            "The specimen runs.",
            "The specimen contacts the target.",
            "The specimen exploits the target.",
            "Any Blue detection, kill, or block occurred.",
            "Judge verified exploit success.",
        ],
    }
    receipt = _node_receipt(
        node_id="compile-repair", status=status, verdict=verdict, evidence=evidence
    )
    receipt["compiler"] = compile_receipt.get("live") or "docker_python_compile"
    receipt["errors"] = errors
    receipt["provider_compile_repair_receipt"] = str(repair_receipt_path)
    receipt["claims"] = claims
    receipt_path = artifact_dir / "compile-repair-node-receipt.json"
    _write_json(receipt_path, receipt)

    evidence.append(_evidence("compile-repair-node-receipt.json", receipt_path))
    if status != "PASS":
        return _handoff(
            start_payload=start_payload,
            previous_subagent="compile-repair",
            status="BLOCKED",
            summary="Compile Repair blocked before runtime/Judge with retained failed specimens.",
            evidence=evidence,
            next_agent="blocked",
            artifacts=[str(repair_receipt_path), str(receipt_path)],
        )

    return _handoff(
        start_payload=start_payload,
        previous_subagent="compile-repair",
        status="PASS",
        summary="Compile Repair produced a provenance-bound Docker-compiled child specimen candidate.",
        evidence=evidence,
        next_agent="artifact-reviewer",
        artifacts=[str(compile_receipt_path), str(repair_receipt_path), str(final_path), str(receipt_path)],
    )

def _run_artifact_reviewer(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any]:
    required = {
        "provider-authorship-receipt.json": _find_upstream_named_artifact(
            start_payload, artifact_dir, "provider-authorship-receipt.json"
        ),
        "specimen.json": _find_upstream_named_artifact(
            start_payload, artifact_dir, "specimen.json"
        ),
        "exploit_genome.json": _find_upstream_named_artifact(
            start_payload, artifact_dir, "exploit_genome.json"
        ),
        "research_receipts.json": _find_upstream_named_artifact(
            start_payload, artifact_dir, "research_receipts.json"
        ),
        "compile_receipt.json": _find_upstream_named_artifact(
            start_payload, artifact_dir, "compile_receipt.json"
        ),
        "exploit_specimen.py": _find_upstream_named_artifact(
            start_payload, artifact_dir, "exploit_specimen.py"
        ),
        "repaired_exploit_specimen.py": _find_upstream_named_artifact(
            start_payload, artifact_dir, "repaired_exploit_specimen.py"
        ),
    }
    missing = [name for name, path in required.items() if path is None]
    errors: list[str] = []
    compile_receipt = (
        _read_json(required["compile_receipt.json"])
        if required["compile_receipt.json"]
        else {}
    )
    repair_receipt_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "provider_compile_repair_receipt.json"
    )
    repair_receipt = _read_json(repair_receipt_path) if repair_receipt_path else {}
    authorship = (
        _read_json(required["provider-authorship-receipt.json"])
        if required["provider-authorship-receipt.json"]
        else {}
    )
    specimen = _read_json(required["specimen.json"]) if required["specimen.json"] else {}
    provider_code_path = required["exploit_specimen.py"]
    code_path = required["repaired_exploit_specimen.py"]
    if compile_receipt.get("status") != "PASS":
        errors.append("compile receipt status is not PASS")
    if (
        authorship.get("provider_live") is not True
        or authorship.get("agentic") is not True
    ):
        errors.append("provider authorship is not live and agentic")
    selected_sha = _sha256_file(code_path) if code_path else None
    if selected_sha and not _same_sha(
        compile_receipt.get("selected_artifact_sha256") or compile_receipt.get("selected_code_sha256"),
        selected_sha,
    ):
        errors.append("selected code hash does not match compile receipt")
    strict_continuity = (
        compile_receipt.get("schema") == "battle.team_compile_receipt.v2"
        or bool(authorship.get("code_artifact_sha256"))
    )
    if not strict_continuity:
        missing = [name for name in missing if name not in {"specimen.json", "exploit_specimen.py"}]
    if missing:
        errors.append(f"missing required artifacts: {', '.join(missing)}")
    continuity = build_provider_artifact_continuity_receipt(
        out_path=artifact_dir / "provider-continuity-receipt.json",
        authorship=authorship,
        specimen=specimen,
        compile_receipt=compile_receipt,
        compile_repair_receipt=repair_receipt,
        provider_code_path=provider_code_path,
        docker_specimen_path=code_path,
    )
    if strict_continuity and continuity.get("status") != "PASS":
        errors.append("provider artifact continuity status is not PASS")
        errors.extend(str(item) for item in continuity.get("errors", []))
    if code_path:
        text = code_path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in (
            "arena/private/",
            "hidden-ground-truth.json",
            "hidden-vulnerability-ledger.json",
            "judge/oracle/",
        ):
            if marker in text:
                errors.append(f"private or oracle reference found: {marker}")
    status = "PASS" if not errors else "BLOCKED"
    verdict = "PASS" if not errors else "ARTIFACT_REVIEW_FAILED"
    review = {
        "schema": "battle.child_artifact_review_receipt.v1",
        "status": status,
        "verdict": verdict,
        "mocked": False,
        "live": "deterministic_artifact_review",
        "fixture_fallback_used": False,
        "selected_code_sha256": selected_sha,
        "provider_continuity_receipt": "provider-continuity-receipt.json",
        "checks": {
            "provider_authorship_live": authorship.get("provider_live") is True,
            "provider_authorship_agentic": authorship.get("agentic") is True,
            "compile_passed_in_docker": compile_receipt.get("live")
            == "docker_python_compile"
            and compile_receipt.get("status") == "PASS",
            "selected_hash_bound": bool(
                selected_sha
                and _same_sha(
                    compile_receipt.get("selected_artifact_sha256")
                    or compile_receipt.get("selected_code_sha256"),
                    selected_sha,
                )
            ),
            "provider_continuity_passed": (not strict_continuity) or continuity.get("status") == "PASS",
            "private_reference_scan_passed": not any(
                "private or oracle reference" in item for item in errors
            ),
        },
        "errors": errors,
        "claims": {
            "proves": [
                "Provider authorship, Docker compile evidence, selected code hash, and private-boundary checks passed."
            ]
            if not errors
            else [],
            "does_not_prove": [
                "The specimen runs.",
                "The specimen contacts the target.",
                "The specimen exploits the target.",
                "Judge verified an outcome.",
            ],
        },
        "created_at": _utc_stamp(),
    }
    review_path = artifact_dir / "review_receipt.json"
    _write_json(review_path, review)
    evidence = [
        _evidence(name, path) for name, path in required.items() if path is not None
    ]
    continuity_path = artifact_dir / "provider-continuity-receipt.json"
    if continuity_path.exists():
        evidence.append(_evidence("provider-continuity-receipt.json", continuity_path))
    evidence.append(_evidence("review_receipt.json", review_path))
    node_receipt = _node_receipt(
        node_id="artifact-reviewer", status=status, verdict=verdict, evidence=evidence
    )
    node_receipt["errors"] = errors
    node_receipt["claims"] = review["claims"]
    node_path = artifact_dir / "artifact-reviewer-node-receipt.json"
    _write_json(node_path, node_receipt)
    return _handoff(
        start_payload=start_payload,
        previous_subagent="artifact-reviewer",
        status=status,
        summary="Artifact review passed."
        if status == "PASS"
        else "Artifact review blocked.",
        evidence=[
            *evidence,
            _evidence("artifact-reviewer-node-receipt.json", node_path),
        ],
        next_agent="battle-handoff-writer" if status == "PASS" else "blocked",
        artifacts=[str(review_path), str(node_path)],
    )


def _run_battle_handoff_writer(
    *, start_payload: dict[str, Any], artifact_dir: Path
) -> dict[str, Any]:
    review_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "review_receipt.json"
    )
    compile_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "compile_receipt.json"
    )
    code_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "repaired_exploit_specimen.py"
    )
    authorship_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "provider-authorship-receipt.json"
    )
    genome_path = _find_upstream_named_artifact(
        start_payload, artifact_dir, "exploit_genome.json"
    )
    missing = [
        name
        for name, path in {
            "review_receipt.json": review_path,
            "compile_receipt.json": compile_path,
            "repaired_exploit_specimen.py": code_path,
            "provider-authorship-receipt.json": authorship_path,
            "exploit_genome.json": genome_path,
        }.items()
        if path is None
    ]
    errors = [f"missing required artifacts: {', '.join(missing)}"] if missing else []
    review = _read_json(review_path) if review_path else {}
    compile_receipt = _read_json(compile_path) if compile_path else {}
    selected_sha = _sha256_file(code_path) if code_path else None
    if review.get("status") != "PASS":
        errors.append("artifact review status is not PASS")
    if review.get("checks", {}).get("provider_continuity_passed") is not True:
        errors.append("provider continuity was not independently verified")
    if selected_sha and not _same_sha(
        compile_receipt.get("selected_artifact_sha256") or compile_receipt.get("selected_code_sha256"),
        selected_sha,
    ):
        errors.append("handoff code hash does not match compile receipt")
    status = "PASS" if not errors else "BLOCKED"
    handoff = {
        "schema": "battle.exploit_runner_handoff.v1",
        "status": status,
        "battle_id": _target_value(start_payload, "battle_id"),
        "dag_id": _target_value(start_payload, "dag_id"),
        "code_artifact": str(code_path) if code_path else None,
        "selected_code_sha256": selected_sha,
        "compile_receipt_sha256": _sha256_file(compile_path) if compile_path else None,
        "review_receipt_sha256": _sha256_file(review_path) if review_path else None,
        "provider_authorship_receipt_sha256": _sha256_file(authorship_path)
        if authorship_path
        else None,
        "provider_continuity_receipt_sha256": _sha256_file(_find_upstream_named_artifact(start_payload, artifact_dir, "provider-continuity-receipt.json"))
        if _find_upstream_named_artifact(start_payload, artifact_dir, "provider-continuity-receipt.json")
        else None,
        "exploit_genome_sha256": _sha256_file(genome_path) if genome_path else None,
        "allowed_runner": "battle_docker_exploit_runner",
        "errors": errors,
        "claims": {
            "proves": [
                "Battle selected one reviewed Docker-compiled provider-authored artifact for Docker execution."
            ]
            if not errors
            else [],
            "does_not_prove": [
                "The specimen runs.",
                "The specimen exploits the target.",
                "Judge verified an outcome.",
            ],
        },
        "created_at": _utc_stamp(),
    }
    handoff_path = artifact_dir / "battle_exploit_runner_handoff.json"
    _write_json(handoff_path, handoff)
    evidence = [_evidence("battle_exploit_runner_handoff.json", handoff_path)]
    node_receipt = _node_receipt(
        node_id="battle-handoff-writer",
        status=status,
        verdict="PASS" if status == "PASS" else "HANDOFF_FAILED",
        evidence=evidence,
    )
    node_receipt["errors"] = errors
    node_receipt["claims"] = handoff["claims"]
    node_path = artifact_dir / "battle-handoff-writer-node-receipt.json"
    _write_json(node_path, node_receipt)
    return _handoff(
        start_payload=start_payload,
        previous_subagent="battle-handoff-writer",
        status=status,
        summary="Battle Docker runner handoff written."
        if status == "PASS"
        else "Battle runner handoff blocked.",
        evidence=[
            *evidence,
            _evidence("battle-handoff-writer-node-receipt.json", node_path),
        ],
        next_agent="human" if status == "PASS" else "blocked",
        artifacts=[str(handoff_path), str(node_path)],
    )


def _blocked_verdict(node_id: str) -> str:
    if node_id == "research-scout":
        return "RESEARCH_ADAPTER_MISSING"
    if node_id == "exploit-code-author":
        return "PROVIDER_OR_TAU_CODE_AUTHOR_ADAPTER_MISSING"
    if node_id == "compile-repair":
        return "UPSTREAM_CODE_ARTIFACT_MISSING"
    return "UPSTREAM_ARTIFACT_MISSING"


def _blocked_reason(node_id: str) -> str:
    if node_id == "research-scout":
        return "No real source-bearing research adapter is configured for PR3a; fixture research fallback is forbidden."
    if node_id == "exploit-code-author":
        return "No real Tau/provider code-authoring adapter is configured; fixture child code fallback is forbidden."
    return "Required upstream artifacts are missing; this PR3a adapter does not synthesize fixture outputs."


def _handoff(
    *,
    start_payload: dict[str, Any],
    previous_subagent: str,
    status: str,
    summary: str,
    evidence: list[dict[str, Any]],
    next_agent: str,
    artifacts: list[str],
) -> dict[str, Any]:
    carried_artifacts = _dedupe_artifacts(
        [*_context_artifacts(start_payload), *artifacts]
    )
    return {
        "schema": "tau.agent_handoff.v1",
        "github": start_payload.get("github")
        if isinstance(start_payload.get("github"), dict)
        else {"repo": "grahama1970/agent-skills", "target": "skills/battle"},
        "goal": start_payload.get("goal")
        if isinstance(start_payload.get("goal"), dict)
        else {},
        "previous_subagent": previous_subagent,
        "context": {
            "summary": summary,
            "artifacts": carried_artifacts,
        },
        "result": {
            "status": status,
            "summary": summary,
            "evidence": evidence,
        },
        "rationale": "The Tau DAG contract controls the next Battle child exploit node.",
        "next_agent": {
            "name": next_agent,
            "executor": "human" if next_agent == "human" else "local",
            "reason": "Continue along the Battle child exploit DAG route.",
        },
        "required_evidence": start_payload.get("required_evidence")
        if isinstance(start_payload.get("required_evidence"), list)
        else [],
        "stop_condition": "Stop at human or any fail-closed Battle/Tau invariant.",
    }


def _dedupe_artifacts(artifacts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for artifact in artifacts:
        if artifact in seen:
            continue
        seen.add(artifact)
        result.append(artifact)
    return result


def _node_receipt(
    *, node_id: str, status: str, verdict: str, evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": NODE_RECEIPT_SCHEMA,
        "node_id": node_id,
        "status": status,
        "verdict": verdict,
        "mocked": False,
        "live": "tau_command_spec_node",
        "agentic": False,
        "fixture_fallback_used": False,
        "evidence": evidence,
        "created_at": _utc_stamp(),
    }


def _dag_contract_path(start_payload: dict[str, Any]) -> Path | None:
    for item in _context_artifacts(start_payload):
        path = Path(item)
        if path.name == "child-exploit-dag.yaml" and path.exists():
            return path.resolve()
    for item in _result_evidence(start_payload):
        if isinstance(item, dict) and item.get("kind") == "dag_contract":
            path_value = item.get("path")
            if isinstance(path_value, str):
                path = Path(path_value)
                if path.exists():
                    return path.resolve()
    return None


def _context_artifacts(payload: dict[str, Any]) -> list[str]:
    context = payload.get("context")
    if not isinstance(context, dict):
        return []
    artifacts = context.get("artifacts")
    return (
        [item for item in artifacts if isinstance(item, str)]
        if isinstance(artifacts, list)
        else []
    )


def _result_evidence(payload: dict[str, Any]) -> list[Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    evidence = result.get("evidence")
    return evidence if isinstance(evidence, list) else []


def _find_named_artifact(payload: dict[str, Any], name: str) -> Path | None:
    candidates: list[str] = []
    candidates.extend(_context_artifacts(payload))
    for item in _result_evidence(payload):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            candidates.append(item["path"])
    for candidate in candidates:
        path = Path(candidate)
        if path.name == name and path.exists():
            return path.resolve()
    return None


def _find_upstream_named_artifact(
    payload: dict[str, Any], artifact_dir: Path, name: str
) -> Path | None:
    if path := _find_named_artifact(payload, name):
        return path
    return _find_named_artifact_from_node_receipts(artifact_dir.parent, name)


def _find_named_artifact_from_node_receipts(root: Path, name: str) -> Path | None:
    if not root.exists():
        return None
    for receipt_path in sorted(root.rglob("*node-receipt.json")):
        try:
            receipt = _read_json(receipt_path)
        except (OSError, json.JSONDecodeError, RuntimeError):
            continue
        for item in receipt.get("evidence", []):
            if not isinstance(item, dict) or item.get("kind") != name:
                continue
            path_value = item.get("path")
            if not isinstance(path_value, str) or not path_value:
                continue
            path = Path(path_value)
            if path.exists():
                return path.resolve()
    return None


def _target_value(payload: dict[str, Any], key: str) -> Any:
    context = payload.get("context")
    tau_node = context.get("tau_dag_node") if isinstance(context, dict) else None
    target = tau_node.get("target") if isinstance(tau_node, dict) else None
    return target.get(key) if isinstance(target, dict) else None


def _goal_hash(payload: dict[str, Any]) -> str | None:
    goal = payload.get("goal")
    value = goal.get("goal_hash") if isinstance(goal, dict) else None
    return value if isinstance(value, str) and value else None


def _evidence(kind: str, path: Path) -> dict[str, Any]:
    return {"kind": kind, "path": str(path)}


def _read_stdin_handoff() -> dict[str, Any]:
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        raise RuntimeError("stdin handoff root must be a JSON object")
    return payload


def _artifact_dir() -> Path:
    value = os.environ.get("TAU_HANDOFF_COMMAND_ARTIFACT_DIR")
    if not value:
        raise RuntimeError("TAU_HANDOFF_COMMAND_ARTIFACT_DIR is required")
    return Path(value).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _utc_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _same_sha(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.removeprefix("sha256:") == right.removeprefix("sha256:")


if __name__ == "__main__":  # pragma: no cover - command entrypoint.
    raise SystemExit(main())
