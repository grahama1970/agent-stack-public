"""Focused COMPLETE amendment 5 checks: outcome semantics and Judge-origin scoring."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from battle_skill.arena_intent import bind_intent, build_arena_maker_intent, freeze_arena_maker_intent

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_small_medium_production_battle.py"
spec = importlib.util.spec_from_file_location("run_small_medium_production_battle", SCRIPT)
assert spec and spec.loader
battle_run = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = battle_run
spec.loader.exec_module(battle_run)


def _arena_binding(tmp_path: Path, battle_id: str = "campaign-a") -> dict:
    auth = battle_run.write_json(tmp_path / battle_id / "authorization.json", {"schema": "auth", "battle_id": battle_id})
    contract = battle_run.write_json(tmp_path / battle_id / "arena.json", {"schema": "arena", "battle_id": battle_id})
    intent = build_arena_maker_intent(
        battle_id=battle_id,
        target_identity=f"{battle_id}@sha256:{'a' * 64}",
        authorization_manifest=auth,
        arena_contract=contract,
        runtime_mode="local_docker_judge_medium_arena",
        round_count=1,
        objective="test Judge-origin scoring",
    )
    return freeze_arena_maker_intent(intent, tmp_path / battle_id / "arena-maker-intent.json")


def _round(tmp_path: Path, payload: dict) -> dict:
    path = battle_run.write_json(tmp_path / payload.get("name", "round") / "round-receipt.json", payload)
    payload["path"] = str(path)
    payload["sha256"] = battle_run.sha256_file(path)
    battle_run.write_json(path, payload)
    payload["sha256"] = battle_run.sha256_file(path)
    return payload


def test_classify_attempt_uses_stage_specific_observations() -> None:
    timeout = battle_run.classify_attempt(exit_code=124, stdout="", stderr="", timed_out=True, error="TimeoutExpired", label="preflight", stage="spawn_preflight")
    traceback = battle_run.classify_attempt(exit_code=1, stdout="", stderr="Traceback (most recent call last)", timed_out=False, error=None, label="patched")
    benign = battle_run.classify_attempt(exit_code=1, stdout="", stderr="", timed_out=False, error=None, label="patched")

    assert timeout["gate"] == "CANCELED_TIMEOUT"
    assert timeout["observation_class"] == "cancellation"
    assert traceback["gate"] == "ERROR_COMPILE"
    assert benign["gate"] == "HARNESS_ERROR"


def test_expected_rejection_is_not_missing_required_child(monkeypatch, tmp_path: Path) -> None:
    def fake_run_docker(work: Path, label: str, *, timeout_seconds: float = 30.0) -> dict:
        if "lightning" in str(work):
            return {"process_status": "FAIL", "semantic_exit_code": None, "infra_error": True, "timed_out": False, "gate": "ERROR_COMPILE", "exit_code": 1}
        if "deadline" in str(work):
            return {"process_status": "FAIL", "semantic_exit_code": None, "infra_error": True, "timed_out": True, "gate": "CANCELED_TIMEOUT", "exit_code": 124}
        return {"process_status": "PASS", "semantic_exit_code": 0, "infra_error": False, "timed_out": False, "gate": "BASELINE_STABLE", "exit_code": 0}

    monkeypatch.setattr(battle_run, "run_docker", fake_run_docker)
    receipt = battle_run.spawn_children(tmp_path, {"round_number": 2, "verdict": "RED_SUCCESS", "vulnerability": "path traversal"}, tmp_path / "events.jsonl")

    assert receipt["status"] == "GATES_INCOMPLETE"
    rejected = next(child for child in receipt["children"] if child["child_id"] == "path-asm-mitm-lightning")
    assert rejected["terminal_disposition"] == "REJECTED_CANDIDATE"
    assert rejected["promotion_allowed"] is False
    assert {item["child_id"] for item in receipt["incomplete_gates"]} == {"path-deadline-expiry-control"}


def test_scorekeeper_rejects_participant_and_cross_campaign_judge_receipts(tmp_path: Path) -> None:
    binding_a = _arena_binding(tmp_path, "campaign-a")
    binding_b = _arena_binding(tmp_path, "campaign-b")
    base = {
        "schema": "battle.production_scale.round_receipt.v2",
        "status": "PASS",
        "round_number": 1,
        "vulnerability": "fixture",
        "verdict": "BLUE_SUCCESS",
        "finding_confirmed": True,
        "red_score": 0,
        "blue_score": 3,
        "incomplete_gates": [],
        "docker_attempts": [],
        "artifacts": {"judge_sha256": "j" * 64},
    }
    forged = _round(tmp_path, bind_intent(base | {"name": "forged"}, binding_a, principal="battle-blue-team", authority_role="participant"))
    cross = _round(tmp_path, bind_intent(base | {"name": "cross", "round_number": 2}, binding_b, principal="battle-judge", authority_role="judge"))

    tampered = _round(tmp_path, bind_intent(base | {"name": "tampered", "blue_score": 99}, binding_a, principal="battle-judge", authority_role="judge"))

    forged_score = battle_run.write_scorekeeper(tmp_path / "forged-score", [forged], tmp_path / "forged-score" / "events.jsonl", binding_a)
    cross_score = battle_run.write_scorekeeper(tmp_path / "cross-score", [cross], tmp_path / "cross-score" / "events.jsonl", binding_a)
    tampered_score = battle_run.write_scorekeeper(tmp_path / "tampered-score", [tampered], tmp_path / "tampered-score" / "events.jsonl", binding_a)

    assert forged_score["status"] == "GATES_INCOMPLETE"
    assert forged_score["red_total"] == forged_score["blue_total"] == 0
    assert any(item["gate"] == "JUDGE_ORIGIN" for item in forged_score["incomplete_gates"])
    assert cross_score["status"] == "GATES_INCOMPLETE"
    assert cross_score["red_total"] == cross_score["blue_total"] == 0
    assert any(item["gate"] == "JUDGE_ORIGIN" for item in cross_score["incomplete_gates"])
    assert tampered_score["status"] == "GATES_INCOMPLETE"
    assert tampered_score["red_total"] == tampered_score["blue_total"] == 0
    assert any(item["gate"] == "JUDGE_SCORE_MISMATCH" for item in tampered_score["incomplete_gates"])


def test_malformed_judge_output_and_target_unavailability_do_not_score(monkeypatch, tmp_path: Path) -> None:
    attempts = {
        "original": {"process_status": "PASS", "semantic_exit_code": 10, "infra_error": False, "timed_out": False, "gate": "EXPLOIT_CONFIRMED_EXIT10", "exit_code": 10, "stdout": '{"judge_exit_signal":10,"probe_result":"confirmed"}', "stderr": "", "path": str(tmp_path / "original.json")},
        "patched": {"process_status": "PASS", "semantic_exit_code": 0, "infra_error": False, "timed_out": False, "gate": "PATCH_HOLD", "exit_code": 0, "stdout": "", "stderr": "", "path": str(tmp_path / "patched.json")},
    }
    for attempt in attempts.values():
        battle_run.write_json(Path(attempt["path"]), attempt)
        attempt["sha256"] = battle_run.sha256_file(Path(attempt["path"]))

    monkeypatch.setattr(battle_run, "run_docker", lambda work, label, timeout_seconds=30.0: dict(attempts[label]))
    monkeypatch.setattr(battle_run, "spawn_children", lambda *args, **kwargs: None)
    receipt = battle_run.judge_round(tmp_path, battle_run.RoundSpec(1, "fixture", "red", "blue", "balance", "", "", ""), tmp_path / "events.jsonl")

    assert receipt["status"] == "INSUFFICIENT_EVIDENCE"
    assert receipt["blue_score"] == 0
    assert any(item["gate"] == "MALFORMED_JUDGE_OUTPUT" for item in receipt["incomplete_gates"])
