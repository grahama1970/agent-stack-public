"""AM-07 evidence-bound learning receipts.

Promotion authorization, durable audit-record storage, exact reread, later use,
and measured benefit are separate assertions. Executable attack strategies are
not durable-write targets here; only reviewer-approved audit findings can be
stored.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AM07_SCHEMA = "battle.am07.learning.v1"
ADAPTATION_SCHEMA = "battle.am07.cross_round_adaptation.v1"
MEMORY_WRITE_SCHEMA = "battle.am07.memory_write_receipt.v1"
RECALL_SCHEMA = "battle.am07.memory_recall_receipt.v1"
CONSUMPTION_SCHEMA = "battle.am07.memory_consumption_receipt.v1"
MANIFEST_SCHEMA = "battle.am07.learning_manifest.v1"
POLICY_ID = "battle-am07-judge-authorized-replay-learning-v2"
PROMOTE_DECISIONS = {"promote_to_rematch_seed", "promote_to_memory_seed"}
AUDIT_RECORD_TYPE = "reviewer_approved_audit_finding"


def build_am07_learning_receipt(*, run_receipt_path: Path, am06_report_path: Path, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[dict[str, Any]] = []
    source_receipts: list[dict[str, Any]] = []

    run = _read_receipt(run_receipt_path, None, "run_receipt", problems)
    am06 = _read_receipt(am06_report_path, None, "AM-06", problems)
    if run is None:
        return _write_receipt(out_dir, _blocked_receipt(out_dir, problems, source_receipts), None, [], [], [])

    source_receipts.append(_source_ref("run_receipt", run_receipt_path, run))
    root = run_receipt_path.parent
    scorekeeper_path = Path(str(run.get("scorekeeper_receipt") or root / "scorekeeper-receipt.json"))
    scorekeeper = _read_receipt(scorekeeper_path, _nested_sha(run.get("scoreboard")), "scorekeeper", problems)
    if scorekeeper:
        source_receipts.append(_source_ref("scorekeeper", scorekeeper_path, scorekeeper))
    if am06:
        source_receipts.append(_source_ref("AM-06", am06_report_path, am06))

    rounds = _verified_rounds(scorekeeper, problems, source_receipts)
    evidence_gate = _evidence_gate(run=run, scorekeeper=scorekeeper, am06=am06, problems=problems)
    decisions = _decisions(run=run, rounds=rounds, evidence_gate=evidence_gate)
    memory_chain = _build_memory_chain(out_dir=out_dir, decisions=decisions)
    adaptation = build_cross_round_adaptation_receipt(
        battle_id=str(run.get("battle_id") or root.name),
        run_id=root.name,
        decisions=decisions,
        rounds=rounds,
        source_receipts=source_receipts,
        recall_receipts=memory_chain["recall_receipts"],
    )
    if adaptation["status"] != "PASS":
        problems.append({"gate": "cross_round_adaptation", "reason": adaptation["status"], "details": adaptation.get("problems", [])})

    promoted = [item for item in decisions if item["decision"] == "PROMOTE"]
    rejected = [item for item in decisions if item["decision"] == "REJECT"]
    storage_gate = _chain_gate(memory_chain["memory_write_receipts"], "authorized_durable_audit_record_written")
    recall_gate = _chain_gate(memory_chain["recall_receipts"], "exact_reread_verified")
    consumption_gate = {"status": adaptation["status"], "reason_codes": [] if adaptation["status"] == "PASS" else ["missing_consumption_evidence"], "fail_closed": True}
    efficacy = {"status": "NOT_CLAIMED", "reason_codes": ["controlled_comparison_not_supplied"], "controlled_comparison_receipt": None}
    status = "PASS" if (
        evidence_gate["status"] == "PASS"
        and promoted
        and storage_gate["status"] == "PASS"
        and recall_gate["status"] == "PASS"
        and consumption_gate["status"] == "PASS"
        and not problems
    ) else "BLOCKED"
    receipt = {
        "schema": AM07_SCHEMA,
        "status": status,
        "battle_id": str(run.get("battle_id") or root.name),
        "run_id": root.name,
        "policy_id": POLICY_ID,
        "policy_sha256": _sha_text(POLICY_ID),
        "gates": {
            "promotion_authorization": evidence_gate,
            "durable_storage": storage_gate,
            "exact_reread": recall_gate,
            "later_consumption": consumption_gate,
            "measured_benefit": efficacy,
        },
        "arena_maker_intent": _arena_intent_ref(run),
        "am06_report_status": None if am06 is None else am06.get("status"),
        "source_receipts": source_receipts,
        "am02_specimen_hashes": _specimen_hashes(rounds),
        "promotion_decisions": promoted,
        "rejection_decisions": rejected,
        "memory_write_decisions": memory_chain["memory_write_decisions"],
        "memory_write_receipts": memory_chain["memory_write_receipts"],
        "recall_receipts": memory_chain["recall_receipts"],
        "cross_round_adaptation_receipt": {"status": adaptation["status"], "path": str(out_dir / "am07-cross-round-adaptation.json"), "sha256": None},
        "efficacy": efficacy,
        "problems": problems,
        "claims": {
            "proves": [
                "Promotion and rejection decisions bind AM-03 intent, AM-02 specimen hashes, AM-06 status, source receipts, and parent/child genome hashes.",
                "Reviewer-approved audit findings can be durably written, exactly reread, and consumed by a later replay candidate.",
            ] if status == "PASS" else [],
            "does_not_prove": [
                "Executable attack strategies were not written to durable memory.",
                "Storage or retrieval caused later success.",
                "Selected evidence caused better later fitness without a controlled comparison receipt.",
            ],
        },
        "created_at": _utc(),
    }
    return _write_receipt(out_dir, receipt, adaptation, memory_chain["memory_write_receipts"], memory_chain["recall_receipts"], memory_chain["consumption_receipts"])


def build_cross_round_adaptation_receipt(*, battle_id: str, run_id: str, decisions: list[dict[str, Any]], rounds: list[dict[str, Any]], source_receipts: list[dict[str, Any]], recall_receipts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Prove selected audit evidence was consumed by a later candidate, not that it worked."""
    ordered_rounds = sorted(rounds, key=lambda item: int(item.get("round_number") or 0))
    recall_by_decision = {item.get("decision_id"): item for item in recall_receipts or [] if item.get("status") == "PASS"}
    for decision in [item for item in decisions if item.get("decision") == "PROMOTE"]:
        recall = recall_by_decision.get(decision["decision_id"])
        if not recall:
            continue
        parent_round = int(decision.get("parent_round") or 0)
        later = next((item for item in ordered_rounds if int(item.get("round_number") or 0) > parent_round), None)
        if not later:
            continue
        base_candidate = {"round_number": later.get("round_number"), "red_goal": later.get("red_goal"), "blue_plan": later.get("blue_plan"), "vulnerability": later.get("vulnerability")}
        selected_evidence = {
            "decision_id": decision["decision_id"],
            "candidate_id": decision["candidate_id"],
            "source_receipt_sha256": decision["source_receipt_sha256"],
            "parent_genome_sha256": decision["parent_genome_sha256"],
            "child_genome_sha256": decision["child_genome_sha256"],
            "memory_record_sha256": recall["memory_record_sha256"],
            "recall_receipt_sha256": recall["receipt_sha256"],
        }
        base_sha = _json_sha(base_candidate)
        adapted_sha = _json_sha({**base_candidate, "am07_consumed_audit_memory": selected_evidence})
        consumption = {
            "schema": CONSUMPTION_SCHEMA,
            "status": "PASS" if base_sha != adapted_sha else "BLOCKED",
            "decision_id": decision["decision_id"],
            "later_round": later.get("round_number"),
            "memory_record_sha256": recall["memory_record_sha256"],
            "recall_receipt_sha256": recall["receipt_sha256"],
            "base_candidate_sha256": base_sha,
            "adapted_candidate_sha256": adapted_sha,
            "created_at": _utc(),
        }
        return {
            "schema": ADAPTATION_SCHEMA,
            "status": consumption["status"],
            "battle_id": battle_id,
            "run_id": run_id,
            "policy_id": POLICY_ID,
            "selected_evidence": selected_evidence,
            "later_candidate": {"round_number": later.get("round_number"), "base_candidate_sha256": base_sha, "adapted_candidate_sha256": adapted_sha, "affected_fields": ["am07_consumed_audit_memory"]},
            "consumption_receipt": consumption,
            "source_receipts": source_receipts,
            "claims": {"proves": ["A later replay candidate input consumed an exactly reread AM-07 audit memory record."], "does_not_prove": ["The selected evidence caused better later fitness or exploit success."]},
            "problems": [],
            "created_at": _utc(),
        }
    return {
        "schema": ADAPTATION_SCHEMA,
        "status": "BLOCKED",
        "battle_id": battle_id,
        "run_id": run_id,
        "policy_id": POLICY_ID,
        "selected_evidence": None,
        "later_candidate": None,
        "consumption_receipt": None,
        "source_receipts": source_receipts,
        "claims": {"proves": [], "does_not_prove": ["Causal efficacy beyond replay."]},
        "problems": ["no promoted durable audit record with a later round candidate"],
        "created_at": _utc(),
    }


def verify_memory_write_receipt(write_receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(write_receipt_path.read_text(encoding="utf-8"))
    record_path = Path(str(receipt.get("memory_record_path")))
    expected = str(receipt.get("memory_record_sha256") or "")
    actual = _sha_file(record_path) if record_path.exists() else None
    return {
        "schema": "battle.am07.memory_write_verification.v1",
        "status": "PASS" if actual == expected else "BLOCKED",
        "memory_write_receipt": str(write_receipt_path),
        "memory_record_path": str(record_path),
        "expected_sha256": expected,
        "actual_sha256": actual,
        "reason_codes": [] if actual == expected else ["memory_record_sha256_mismatch"],
        "created_at": _utc(),
    }


def _build_memory_chain(*, out_dir: Path, decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    memory_dir = out_dir / "durable-audit-memory"
    receipt_dir = out_dir / "memory-receipts"
    memory_dir.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    write_decisions: list[dict[str, Any]] = []
    write_receipts: list[dict[str, Any]] = []
    recall_receipts: list[dict[str, Any]] = []
    consumption_receipts: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for decision in decisions:
        allowed, reasons = _memory_write_authorization(decision)
        record_id = _record_id(decision)
        if decision.get("decision") != "PROMOTE" or not allowed:
            write_decisions.append({
                "decision_id": decision["decision_id"],
                "candidate_id": decision["candidate_id"],
                "decision": "WRITE_DENIED",
                "durable_memory_written": False,
                "reason_codes": decision.get("reason_codes", []) + reasons,
                "source_receipt_sha256": decision["source_receipt_sha256"],
            })
            continue
        record = _audit_memory_record(decision, record_id)
        record_sha = _json_sha(record)
        expected_file_sha = _json_file_sha(record)
        if record_id in seen and seen[record_id] != record_sha:
            write_decisions.append({
                "decision_id": decision["decision_id"],
                "candidate_id": decision["candidate_id"],
                "decision": "WRITE_DENIED",
                "durable_memory_written": False,
                "reason_codes": ["duplicate_record_conflict"],
                "source_receipt_sha256": decision["source_receipt_sha256"],
            })
            continue
        seen[record_id] = record_sha
        record_path = memory_dir / f"{record_id}.json"
        idempotent = record_path.exists()
        if not idempotent:
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        actual_record_sha = _sha_file(record_path)
        write_receipt = {
            "schema": MEMORY_WRITE_SCHEMA,
            "status": "PASS" if actual_record_sha == expected_file_sha else "BLOCKED",
            "decision_id": decision["decision_id"],
            "candidate_id": decision["candidate_id"],
            "record_id": record_id,
            "idempotent_reuse": idempotent,
            "memory_record_path": str(record_path),
            "memory_record_sha256": actual_record_sha,
            "expected_record_sha256": expected_file_sha,
            "canonical_record_sha256": record_sha,
            "source_receipt_sha256": decision["source_receipt_sha256"],
            "created_at": _utc(),
        }
        write_path = receipt_dir / f"{record_id}.write.json"
        write_path.write_text(json.dumps(write_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_receipt["path"] = str(write_path)
        write_receipt["receipt_sha256"] = _sha_file(write_path)
        recall = _build_recall_receipt(write_receipt, receipt_dir / f"{record_id}.recall.json")
        write_decisions.append({
            "decision_id": decision["decision_id"],
            "candidate_id": decision["candidate_id"],
            "decision": "WRITE_PERFORMED" if write_receipt["status"] == "PASS" else "WRITE_BLOCKED",
            "durable_memory_written": write_receipt["status"] == "PASS",
            "reason_codes": ["reviewer_approved_audit_finding"] if write_receipt["status"] == "PASS" else ["memory_record_write_mismatch"],
            "source_receipt_sha256": decision["source_receipt_sha256"],
            "memory_record_sha256": write_receipt["memory_record_sha256"],
            "memory_write_receipt_sha256": write_receipt["receipt_sha256"],
            "recall_receipt_sha256": recall["receipt_sha256"],
        })
        write_receipts.append(write_receipt)
        recall_receipts.append(recall)
    return {"memory_write_decisions": write_decisions, "memory_write_receipts": write_receipts, "recall_receipts": recall_receipts, "consumption_receipts": consumption_receipts}


def _build_recall_receipt(write_receipt: dict[str, Any], path: Path) -> dict[str, Any]:
    record_path = Path(str(write_receipt["memory_record_path"]))
    actual_sha = _sha_file(record_path)
    exact = actual_sha == write_receipt["memory_record_sha256"]
    recall = {
        "schema": RECALL_SCHEMA,
        "status": "PASS" if exact else "BLOCKED",
        "decision_id": write_receipt["decision_id"],
        "candidate_id": write_receipt["candidate_id"],
        "memory_write_receipt_sha256": write_receipt["receipt_sha256"],
        "memory_record_path": str(record_path),
        "memory_record_sha256": actual_sha,
        "exact_reread": exact,
        "created_at": _utc(),
    }
    path.write_text(json.dumps(recall, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recall["path"] = str(path)
    recall["receipt_sha256"] = _sha_file(path)
    return recall


def _memory_write_authorization(decision: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if decision.get("decision") != "PROMOTE":
        reasons.append("promotion_not_authorized")
    learning_record = decision.get("learning_record") if isinstance(decision.get("learning_record"), dict) else {}
    if learning_record.get("record_type") != AUDIT_RECORD_TYPE:
        reasons.append("not_reviewer_approved_audit_finding")
    approval = learning_record.get("reviewer_approval") if isinstance(learning_record.get("reviewer_approval"), dict) else {}
    if approval.get("status") != "APPROVED" or approval.get("reviewer") != "Judge":
        reasons.append("judge_reviewer_approval_missing")
    if learning_record.get("source_revoked") is True:
        reasons.append("source_revoked")
    if learning_record.get("contains_executable_strategy") is True:
        reasons.append("executable_strategy_not_memory_write_target")
    if not decision.get("arena_maker_intent_sha256"):
        reasons.append("intent_binding_missing")
    if not decision.get("source_receipt_sha256"):
        reasons.append("source_binding_missing")
    if not decision.get("child_genome_sha256") or not decision.get("parent_genome_sha256"):
        reasons.append("parent_child_identity_missing")
    if not decision.get("am02_specimen_hashes"):
        reasons.append("specimen_binding_missing")
    if not _judge_sha256(decision):
        reasons.append("judge_specimen_binding_missing")
    return not reasons, reasons


def _judge_sha256(decision: dict[str, Any]) -> str | None:
    return next((item.get("sha256") for item in decision.get("am02_specimen_hashes", []) if item.get("kind") == "judge_sha256"), None)


def _audit_memory_record(decision: dict[str, Any], record_id: str) -> dict[str, Any]:
    learning_record = decision.get("learning_record") if isinstance(decision.get("learning_record"), dict) else {}
    return {
        "schema": "battle.am07.durable_audit_record.v1",
        "record_id": record_id,
        "record_type": AUDIT_RECORD_TYPE,
        "intent_sha256": decision.get("arena_maker_intent_sha256"),
        "judge_sha256": _judge_sha256(decision),
        "specimen_hashes": decision.get("am02_specimen_hashes", []),
        "source_receipt_sha256": decision.get("source_receipt_sha256"),
        "parent_source_receipt_sha256": decision.get("parent_source_receipt_sha256"),
        "parent_genome_sha256": decision.get("parent_genome_sha256"),
        "child_genome_sha256": decision.get("child_genome_sha256"),
        "candidate_id": decision.get("candidate_id"),
        "learning_role": decision.get("learning_role"),
        "finding": learning_record.get("finding"),
        "intent": learning_record.get("intent"),
        "reviewer_approval": learning_record.get("reviewer_approval"),
    }


def _record_id(decision: dict[str, Any]) -> str:
    return _sha_text(str(decision["decision_id"]))[:24]


def _chain_gate(receipts: list[dict[str, Any]], pass_reason: str) -> dict[str, Any]:
    if not receipts:
        return {"status": "BLOCKED", "reason_codes": ["no_authorized_durable_audit_record"], "fail_closed": True}
    bad = [item for item in receipts if item.get("status") != "PASS"]
    return {"status": "PASS" if not bad else "BLOCKED", "reason_codes": [pass_reason] if not bad else ["receipt_not_pass"], "fail_closed": True}


def _decisions(*, run: dict[str, Any], rounds: list[dict[str, Any]], evidence_gate: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    gate_ok = evidence_gate["status"] == "PASS"
    for round_receipt in rounds:
        spawn = round_receipt.get("spawn_receipt") if isinstance(round_receipt.get("spawn_receipt"), dict) else None
        if not spawn:
            continue
        children = spawn.get("children") if isinstance(spawn.get("children"), list) else []
        for child in children:
            preflight = child.get("preflight") if isinstance(child.get("preflight"), dict) else {}
            role = _learning_role(child)
            role_ok = _role_promotion_ok(role, round_receipt)
            child_ok = gate_ok and round_receipt.get("status") == "PASS" and spawn.get("status") == "PASS" and preflight.get("process_status") == "PASS" and preflight.get("semantic_exit_code") == 0
            promoted = child_ok and role_ok and child.get("lineage_decision") in PROMOTE_DECISIONS
            reason_codes = _reason_codes(gate_ok, role_ok, round_receipt, spawn, child, preflight, promoted)
            decisions.append({
                "decision_id": f"{run.get('battle_id') or 'battle'}-r{round_receipt.get('round_number')}-{child.get('child_id')}",
                "decision": "PROMOTE" if promoted else "REJECT",
                "candidate_id": str(child.get("child_id")),
                "parent_round": round_receipt.get("round_number"),
                "learning_role": role,
                "mutation_operator": child.get("mutation_operator"),
                "lineage_decision": child.get("lineage_decision"),
                "parent_genome_sha256": _json_sha(_round_parent_hashes(round_receipt)),
                "child_genome_sha256": _norm_sha(child.get("specimen_sha256")),
                "source_receipt_sha256": _norm_sha(spawn.get("sha256")),
                "parent_source_receipt_sha256": _norm_sha(round_receipt.get("sha256")),
                "arena_maker_intent_sha256": (_arena_intent_ref(run) or {}).get("sha256"),
                "am06_status_required": "PASS",
                "am02_specimen_hashes": _decision_specimen_hashes(round_receipt, child),
                "learning_record": child.get("learning_record") if isinstance(child.get("learning_record"), dict) else {},
                "reason_codes": reason_codes,
                "memory_learning_allowed": promoted,
                "created_at": _utc(),
            })
    return decisions


def _learning_role(child: dict[str, Any]) -> str:
    return str(child.get("learning_role") or child.get("team") or "red_blue_lineage")


def _role_promotion_ok(role: str, round_receipt: dict[str, Any]) -> bool:
    normalized = role.lower()
    verdict = round_receipt.get("verdict")
    if normalized in {"red", "attacker", "red_blue_lineage"}:
        return verdict == "RED_SUCCESS"
    if normalized in {"blue", "defender"}:
        return verdict == "BLUE_SUCCESS"
    return False


def _reason_codes(gate_ok: bool, role_ok: bool, round_receipt: dict[str, Any], spawn: dict[str, Any], child: dict[str, Any], preflight: dict[str, Any], promoted: bool) -> list[str]:
    if promoted:
        return ["judge_authorized_replay_evidence", "role_appropriate_promotion", "policy_promoted_lineage_decision"]
    reasons = []
    if not gate_ok:
        reasons.append("judge_authorized_evidence_missing")
    if round_receipt.get("status") != "PASS":
        reasons.append("parent_round_not_pass")
    if not role_ok:
        reasons.append("role_inappropriate_promotion")
    if spawn.get("status") != "PASS":
        reasons.append("spawn_receipt_not_pass")
    if preflight.get("process_status") != "PASS" or preflight.get("semantic_exit_code") != 0:
        reasons.append("child_preflight_not_replayable_pass")
    if child.get("lineage_decision") not in PROMOTE_DECISIONS:
        reasons.append("policy_rejected_lineage_decision")
    return reasons or ["policy_rejected_lineage_decision"]


def _evidence_gate(*, run: dict[str, Any], scorekeeper: dict[str, Any] | None, am06: dict[str, Any] | None, problems: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = []
    if run.get("status") != "PASS":
        reasons.append("run_receipt_not_pass")
    if scorekeeper is None or scorekeeper.get("status") != "PASS":
        reasons.append("scorekeeper_not_pass")
    if am06 is None or am06.get("status") != "PASS":
        reasons.append("am06_not_pass")
    intent = run.get("arena_maker_intent_verification") if isinstance(run.get("arena_maker_intent_verification"), dict) else {}
    if intent.get("status") != "PASS":
        reasons.append("am03_intent_not_pass")
    if problems:
        reasons.append("source_receipt_verification_problem")
    return {"status": "PASS" if not reasons else "BLOCKED", "authority": "Judge receipts + scorekeeper + AM-03 + AM-06", "reason_codes": reasons, "fail_closed": True}


def _verified_rounds(scorekeeper: dict[str, Any] | None, problems: list[dict[str, Any]], source_receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not scorekeeper:
        return []
    rounds = []
    refs = scorekeeper.get("round_receipts") if isinstance(scorekeeper.get("round_receipts"), list) else []
    for ref in refs:
        path = Path(str(ref.get("path")))
        receipt = _read_receipt(path, _norm_sha(ref.get("sha256")), "round_receipt", problems)
        if receipt is None:
            continue
        receipt["path"] = str(path)
        receipt["sha256"] = _sha_file(path)
        source_receipts.append(_source_ref("round_receipt", path, receipt))
        spawn = receipt.get("spawn_receipt") if isinstance(receipt.get("spawn_receipt"), dict) else None
        if spawn and spawn.get("path") and spawn.get("sha256"):
            spawn_path = Path(str(spawn["path"]))
            verified_spawn = _read_receipt(spawn_path, _norm_sha(spawn.get("sha256")), "spawn_receipt", problems)
            if verified_spawn is not None:
                verified_spawn["path"] = str(spawn_path)
                verified_spawn["sha256"] = _sha_file(spawn_path)
                receipt["spawn_receipt"] = verified_spawn
                source_receipts.append(_source_ref("spawn_receipt", spawn_path, verified_spawn))
                for child in verified_spawn.get("children", []) if isinstance(verified_spawn.get("children"), list) else []:
                    preflight = child.get("preflight") if isinstance(child.get("preflight"), dict) else None
                    if preflight and preflight.get("path") and preflight.get("sha256"):
                        preflight_path = Path(str(preflight["path"]))
                        verified_preflight = _read_receipt(preflight_path, _norm_sha(preflight.get("sha256")), "child_preflight_receipt", problems)
                        if verified_preflight is not None:
                            source_receipts.append(_source_ref("child_preflight_receipt", preflight_path, verified_preflight))
        rounds.append(receipt)
    return rounds


def _specimen_hashes(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for round_receipt in rounds:
        for name, value in _round_parent_hashes(round_receipt).items():
            if isinstance(value, str) and len(value) == 64:
                out.append({"round": round_receipt.get("round_number"), "kind": name, "sha256": value})
        spawn = round_receipt.get("spawn_receipt") if isinstance(round_receipt.get("spawn_receipt"), dict) else None
        for child in spawn.get("children", []) if spawn else []:
            value = _norm_sha(child.get("specimen_sha256"))
            if value:
                out.append({"round": round_receipt.get("round_number"), "kind": "spawn_child", "candidate_id": child.get("child_id"), "sha256": value})
    return out


def _decision_specimen_hashes(round_receipt: dict[str, Any], child: dict[str, Any]) -> list[dict[str, Any]]:
    hashes = [{"kind": key, "sha256": value} for key, value in _round_parent_hashes(round_receipt).items() if isinstance(value, str) and len(value) == 64]
    child_sha = _norm_sha(child.get("specimen_sha256"))
    if child_sha:
        hashes.append({"kind": "spawn_child", "sha256": child_sha})
    return hashes


def _round_parent_hashes(round_receipt: dict[str, Any]) -> dict[str, Any]:
    artifacts = round_receipt.get("artifacts") if isinstance(round_receipt.get("artifacts"), dict) else {}
    return {"original_app_sha256": _norm_sha(artifacts.get("original_app_sha256")), "patched_app_sha256": _norm_sha(artifacts.get("patched_app_sha256")), "judge_sha256": _norm_sha(artifacts.get("judge_sha256"))}


def _arena_intent_ref(run: dict[str, Any]) -> dict[str, Any] | None:
    intent = run.get("arena_maker_intent") if isinstance(run.get("arena_maker_intent"), dict) else None
    if not intent:
        return None
    verification = run.get("arena_maker_intent_verification") if isinstance(run.get("arena_maker_intent_verification"), dict) else {}
    return {"schema": intent.get("arena_maker_intent_schema"), "path": intent.get("arena_maker_intent_path"), "sha256": intent.get("arena_maker_intent_sha256"), "verification_status": verification.get("status")}


def _blocked_receipt(out_dir: Path, problems: list[dict[str, Any]], source_receipts: list[dict[str, Any]]) -> dict[str, Any]:
    blocked_gate = {"status": "BLOCKED", "reason_codes": ["run_receipt_unavailable"], "fail_closed": True}
    return {"schema": AM07_SCHEMA, "status": "BLOCKED", "battle_id": out_dir.name, "run_id": out_dir.name, "policy_id": POLICY_ID, "policy_sha256": _sha_text(POLICY_ID), "gates": {name: blocked_gate for name in ("promotion_authorization", "durable_storage", "exact_reread", "later_consumption", "measured_benefit")}, "source_receipts": source_receipts, "am02_specimen_hashes": [], "promotion_decisions": [], "rejection_decisions": [], "memory_write_decisions": [], "memory_write_receipts": [], "recall_receipts": [], "cross_round_adaptation_receipt": None, "efficacy": {"status": "NOT_CLAIMED", "reason_codes": ["controlled_comparison_not_supplied"]}, "problems": problems, "claims": {"proves": [], "does_not_prove": ["Learning eligibility."]}, "created_at": _utc()}


def _write_receipt(out_dir: Path, receipt: dict[str, Any], adaptation: dict[str, Any] | None, write_receipts: list[dict[str, Any]], recall_receipts: list[dict[str, Any]], consumption_receipts: list[dict[str, Any]]) -> dict[str, Any]:
    if adaptation is not None:
        adaptation_path = out_dir / "am07-cross-round-adaptation.json"
        adaptation_path.write_text(json.dumps(adaptation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["cross_round_adaptation_receipt"]["sha256"] = _sha_file(adaptation_path)
        if adaptation.get("consumption_receipt"):
            consumption_path = out_dir / "memory-receipts" / f"{_record_id({'decision_id': adaptation['consumption_receipt']['decision_id']})}.consumption.json"
            consumption_path.parent.mkdir(parents=True, exist_ok=True)
            consumption_path.write_text(json.dumps(adaptation["consumption_receipt"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
            adaptation["consumption_receipt"]["path"] = str(consumption_path)
            adaptation["consumption_receipt"]["receipt_sha256"] = _sha_file(consumption_path)
            consumption_receipts.append(adaptation["consumption_receipt"])
            receipt["consumption_receipts"] = consumption_receipts
    path = out_dir / "am07-learning.json"
    receipt["path"] = str(path)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt_sha = _sha_file(path)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "learning_receipt_path": str(path),
        "learning_receipt_sha256": receipt_sha,
        "memory_write_receipts": [{"path": item.get("path"), "sha256": item.get("receipt_sha256")} for item in write_receipts],
        "recall_receipts": [{"path": item.get("path"), "sha256": item.get("receipt_sha256")} for item in recall_receipts],
        "consumption_receipts": [{"path": item.get("path"), "sha256": item.get("receipt_sha256")} for item in consumption_receipts],
        "created_at": _utc(),
    }
    manifest_path = out_dir / "am07-learning-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["sha256"] = receipt_sha
    receipt["external_manifest"] = {"path": str(manifest_path), "sha256": _sha_file(manifest_path)}
    return receipt


def _read_receipt(path: Path, expected_sha256: str | None, label: str, problems: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        actual = _sha_file(path)
        if expected_sha256 and actual != expected_sha256:
            problems.append({"gate": label, "reason": "sha256_mismatch", "path": str(path), "expected_sha256": expected_sha256, "actual_sha256": actual})
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        problems.append({"gate": label, "reason": "unreadable_receipt", "path": str(path), "detail": f"{type(exc).__name__}: {exc}"})
        return None


def _source_ref(label: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "path": str(path), "schema": str(payload.get("schema") or "unknown"), "status": payload.get("status"), "sha256": _sha_file(path)}


def _nested_sha(value: Any) -> str | None:
    return _norm_sha(value.get("sha256")) if isinstance(value, dict) else None


def _norm_sha(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _json_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_file_sha(value: Any) -> str:
    return hashlib.sha256((json.dumps(value, indent=2, sort_keys=True) + "\n").encode()).hexdigest()


def _utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
