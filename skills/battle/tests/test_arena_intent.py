from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))

from battle_skill.arena_intent import (  # noqa: E402
    REQUIRED_INTENT_FIELDS,
    arena_intent_sha256,
    bind_intent,
    build_arena_maker_intent,
    freeze_arena_maker_intent,
    validate_arena_intent,
    verify_bound_payload,
    verify_frozen_intent,
)

SCRIPT = ROOT / "scripts" / "run_small_medium_production_battle.py"
spec = importlib.util.spec_from_file_location("run_small_medium_production_battle_am03", SCRIPT)
assert spec and spec.loader
battle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = battle
spec.loader.exec_module(battle)


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_arena_intent_freeze_fails_closed_on_missing_mismatch_and_mutation(tmp_path: Path) -> None:
    auth = _write(tmp_path / "authorization.json", {"target": "x"})
    arena = _write(tmp_path / "arena.json", {"arena": "x"})
    intent = build_arena_maker_intent(
        battle_id="battle-1",
        target_identity="target@sha256:abc",
        authorization_manifest=auth,
        arena_contract=arena,
        runtime_mode="local_docker_judge_medium_arena",
        round_count=1,
        objective="Complete Battle test",
    )
    binding = freeze_arena_maker_intent(intent, tmp_path / "arena-maker-intent.json")
    bound = bind_intent({"schema": "example"}, binding)

    assert verify_frozen_intent(binding)["status"] == "PASS"
    assert verify_bound_payload(bound, binding, label="receipt")["status"] == "PASS"
    assert verify_bound_payload({"schema": "example"}, binding, label="receipt")["status"] == "FAIL"
    bad = dict(bound)
    bad["arena_maker_intent"] = {"schema": "battle.arena_maker_intent.v1", "sha256": "0" * 64}
    assert verify_bound_payload(bad, binding, label="receipt")["status"] == "FAIL"

    frozen_path = Path(binding["arena_maker_intent_path"])
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    assert set(REQUIRED_INTENT_FIELDS) <= set(frozen)
    assert "complete_battle_fields" not in frozen
    assert validate_arena_intent(frozen) == []
    assert arena_intent_sha256(dict(reversed(list(frozen.items())))) == binding["arena_maker_intent_sha256"]

    for field in REQUIRED_INTENT_FIELDS:
        broken = dict(frozen)
        broken.pop(field)
        assert validate_arena_intent(broken), field

    mutated = dict(frozen)
    mutated["arena_goal"] = "mutated after freeze"
    _write(frozen_path, mutated)
    drift = verify_frozen_intent(binding)
    assert drift["status"] == "FAIL"
    assert any("ARENA_INTENT_DRIFT" in problem for problem in drift["problems"])


def _attempt(label: str) -> dict[str, Any]:
    code = 10 if label == "original" else 0
    result = "confirmed" if code == 10 else "blocked"
    stdout = json.dumps({"judge_exit_signal": code, "probe_result": result}) + "\n"
    classified = battle.classify_attempt(exit_code=code, stdout=stdout, stderr="", timed_out=False, error=None, label=label)
    return {
        "schema": "battle.production_scale.docker_attempt.v1",
        "status": classified["process_status"],
        "process_status": classified["process_status"],
        "label": label,
        "exit_code": code,
        "semantic_exit_code": classified["semantic_exit_code"],
        "infra_error": classified["infra_error"],
        "timed_out": False,
        "gate": classified["gate"],
        "stdout": stdout,
        "stderr": "",
        "stdout_sha256": classified["stdout_sha256"],
        "stderr_sha256": classified["stderr_sha256"],
    }


def test_small_medium_binds_same_intent_hash_through_authorization_teams_judges_scorekeeper_and_report(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(battle, "run_docker", lambda work, label: _attempt(label))
    receipt = battle.run(tmp_path)
    digest = receipt["arena_maker_intent"]["arena_maker_intent_sha256"]

    assert receipt["status"] == "PASS"
    assert receipt["arena_maker_intent_verification"]["status"] == "PASS"
    assert verify_bound_payload(receipt, receipt["arena_maker_intent"], label="run_receipt", expected_authority_role="arena_maker")["status"] == "PASS"
    auth = json.loads((tmp_path / "authorization-validation.json").read_text(encoding="utf-8"))
    scorekeeper = json.loads((tmp_path / "scorekeeper-receipt.json").read_text(encoding="utf-8"))
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert auth["arena_maker_intent"]["sha256"] == digest
    assert scorekeeper["arena_maker_intent"]["sha256"] == digest
    assert f"Arena-maker intent: {digest}." in report
    assert "Arena authority: arena_maker by battle-control-plane" in report
    for round_receipt in receipt["rounds"]:
        on_disk = json.loads(Path(round_receipt["path"]).read_text(encoding="utf-8"))
        assert on_disk["arena_maker_intent"]["sha256"] == digest
        assert on_disk["red_handoff"]["arena_maker_intent"]["sha256"] == digest
        assert on_disk["blue_handoff"]["arena_maker_intent"]["sha256"] == digest
        assert all(item["arena_maker_intent"]["sha256"] == digest for item in on_disk["judge_receipts"])


def test_scorekeeper_fails_closed_when_round_intent_binding_is_removed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(battle, "run_docker", lambda work, label: _attempt(label))
    receipt = battle.run(tmp_path)
    first_round = Path(receipt["rounds"][0]["path"])
    payload = json.loads(first_round.read_text(encoding="utf-8"))
    payload.pop("arena_maker_intent")
    _write(first_round, payload)

    scorekeeper = battle.write_scorekeeper(tmp_path, receipt["rounds"], tmp_path / "event-ledger.jsonl", receipt["arena_maker_intent"])
    assert scorekeeper["status"] in {"CHAIN_MISMATCH", "GATES_INCOMPLETE"}
    assert any(item.get("gate") in {"ARENA_MAKER_INTENT", "CHAIN_MISMATCH"} for item in scorekeeper["incomplete_gates"])


def _binding(tmp_path: Path, battle_id: str = "battle-1") -> dict[str, Any]:
    auth = _write(tmp_path / battle_id / "authorization.json", {"target": battle_id})
    arena = _write(tmp_path / battle_id / "arena.json", {"arena": battle_id})
    intent = build_arena_maker_intent(
        battle_id=battle_id,
        target_identity=f"target-{battle_id}@sha256:abc",
        authorization_manifest=auth,
        arena_contract=arena,
        runtime_mode="local_docker_judge_medium_arena",
        round_count=1,
        objective="Complete Battle authority test",
    )
    return freeze_arena_maker_intent(intent, tmp_path / battle_id / "arena-maker-intent.json")


def test_authority_rejects_posthoc_cross_campaign_and_participant_forged_judge(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "campaign-a")
    other = _binding(tmp_path, "campaign-b")

    posthoc = {"schema": "battle.production_scale.round_receipt.v2", "arena_maker_intent": {"schema": "battle.arena_maker_intent.v1", "sha256": binding["arena_maker_intent_sha256"]}}
    assert verify_bound_payload(posthoc, binding, label="posthoc")["status"] == "FAIL"

    bound = bind_intent({"schema": "battle.production_scale.round_receipt.v2"}, binding, principal="battle-judge", authority_role="judge")
    assert verify_bound_payload(bound, other, label="cross-campaign")["status"] == "FAIL"

    forged = bind_intent({"schema": "battle.production_scale.judge_receipt.v1"}, binding, principal="red", authority_role="judge")
    forged_check = verify_bound_payload(forged, binding, label="forged-judge", expected_authority_role="judge")
    assert forged_check["status"] == "FAIL"
    assert any("participant principal" in problem or "unauthorized Judge" in problem for problem in forged_check["problems"])

    candidate = bind_intent({"schema": "battle.production_scale.blue_candidate.v1"}, binding, principal="battle-blue-team", authority_role="participant")
    candidate["arena_authority"]["baseline_identity"] = "mutated-baseline"
    assert verify_bound_payload(candidate, binding, label="blue-candidate", expected_authority_role="participant")["status"] == "FAIL"


def test_failed_predispatch_arena_validation_launches_zero_participants(tmp_path: Path, monkeypatch: Any) -> None:
    binding = _binding(tmp_path, "campaign-dispatch")
    frozen_path = Path(binding["arena_maker_intent_path"])
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    frozen["arena_goal"] = "mutated after freeze"
    _write(frozen_path, frozen)

    launches = 0

    def counted_run_docker(work: Path, label: str) -> dict[str, Any]:
        nonlocal launches
        launches += 1
        return _attempt(label)

    monkeypatch.setattr(battle, "run_docker", counted_run_docker)
    receipt = battle.judge_round(tmp_path / "run", battle.specs()[0], tmp_path / "events.jsonl", binding)
    assert receipt["status"] == "BLOCKED"
    assert receipt["participant_launch_count"] == 0
    assert launches == 0
