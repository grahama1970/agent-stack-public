import hashlib
import json
from pathlib import Path

from battle_skill.am07_learning import build_am07_learning_receipt, verify_memory_write_receipt


def _write(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _audit_record(**overrides: object) -> dict:
    record = {
        "record_type": "reviewer_approved_audit_finding",
        "finding": "Patched importer blocks traversal while preserving normal zip imports.",
        "intent": "defensive_audit_memory",
        "reviewer_approval": {"status": "APPROVED", "reviewer": "Judge", "specimen": "selected-child"},
        "contains_executable_strategy": False,
    }
    record.update(overrides)
    return record


def _fixture(
    root: Path,
    *,
    am06_status: str = "PASS",
    run_status: str = "PASS",
    selected_record: dict | None = None,
    selected_role: str = "blue",
    selected_verdict: str = "BLUE_SUCCESS",
    selected_id: str = "selected-child",
) -> tuple[Path, Path]:
    spawn = {
        "schema": "battle.production_scale.spawn_receipt.v1",
        "status": "PASS",
        "children": [
            {
                "child_id": selected_id,
                "learning_role": selected_role,
                "mutation_operator": "defense_aware_crossover",
                "lineage_decision": "promote_to_memory_seed",
                "specimen_sha256": _sha("selected"),
                "preflight": {"process_status": "PASS", "semantic_exit_code": 0},
                "learning_record": selected_record if selected_record is not None else _audit_record(),
            },
            {
                "child_id": "dead-child",
                "learning_role": "blue",
                "mutation_operator": "warm_pond",
                "lineage_decision": "reject_dead_preflight",
                "specimen_sha256": _sha("dead"),
                "preflight": {"process_status": "FAIL", "semantic_exit_code": None},
                "learning_record": _audit_record(),
            },
        ],
    }
    spawn_path = root / "round-01" / "spawns" / "spawn-receipt.json"
    spawn_sha = _write(spawn_path, spawn)
    round1 = {
        "schema": "battle.production_scale.round_receipt.v2",
        "status": "PASS",
        "round_number": 1,
        "vulnerability": "path traversal",
        "red_goal": "prove traversal",
        "blue_plan": "patch traversal",
        "verdict": selected_verdict,
        "artifacts": {"original_app_sha256": _sha("orig"), "patched_app_sha256": _sha("patch"), "judge_sha256": _sha("judge")},
        "spawn_receipt": {**spawn, "path": str(spawn_path), "sha256": spawn_sha},
    }
    round2 = {
        "schema": "battle.production_scale.round_receipt.v2",
        "status": "PASS",
        "round_number": 2,
        "vulnerability": "unsafe eval",
        "red_goal": "later red candidate",
        "blue_plan": "later blue candidate",
        "verdict": "BLUE_SUCCESS",
        "artifacts": {"original_app_sha256": _sha("orig2"), "patched_app_sha256": _sha("patch2"), "judge_sha256": _sha("judge2")},
    }
    r1_path = root / "round-01" / "round-receipt.json"
    r2_path = root / "round-02" / "round-receipt.json"
    r1_sha = _write(r1_path, round1)
    r2_sha = _write(r2_path, round2)
    scorekeeper = {
        "schema": "battle.production_scale.scorekeeper_receipt.v2",
        "status": "PASS" if run_status == "PASS" else "GATES_INCOMPLETE",
        "round_receipts": [{"path": str(r1_path), "sha256": r1_sha}, {"path": str(r2_path), "sha256": r2_sha}],
    }
    score_path = root / "scorekeeper-receipt.json"
    score_sha = _write(score_path, scorekeeper)
    run = {
        "schema": "battle.production_scale.run_receipt.v2",
        "status": run_status,
        "battle_id": "battle-am07-fixture",
        "scorekeeper_receipt": str(score_path),
        "scoreboard": {**scorekeeper, "path": str(score_path), "sha256": score_sha},
        "arena_maker_intent": {"arena_maker_intent_schema": "battle.arena_maker_intent.v1", "arena_maker_intent_path": str(root / "intent.json"), "arena_maker_intent_sha256": _sha("intent")},
        "arena_maker_intent_verification": {"status": "PASS", "problems": []},
    }
    run_path = root / "run-receipt.json"
    _write(run_path, run)
    am06 = {"schema": "battle.am06.reporting.v1", "status": am06_status, "generated_from_receipts_only": True}
    am06_path = root / "am06-reporting" / "am06-reporting.json"
    _write(am06_path, am06)
    return run_path, am06_path


def test_am07_writes_authorized_audit_record_exactly_rereads_and_consumes_it(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "PASS"
    assert len(receipt["promotion_decisions"]) == 1
    assert receipt["promotion_decisions"][0]["learning_role"] == "blue"
    write_decision = receipt["memory_write_decisions"][0]
    assert write_decision["decision"] == "WRITE_PERFORMED"
    assert write_decision["durable_memory_written"] is True
    assert receipt["gates"]["durable_storage"]["status"] == "PASS"
    assert receipt["gates"]["exact_reread"]["status"] == "PASS"
    assert receipt["gates"]["later_consumption"]["status"] == "PASS"
    assert receipt["efficacy"]["status"] == "NOT_CLAIMED"
    assert receipt["memory_write_receipts"][0]["memory_record_sha256"] == receipt["recall_receipts"][0]["memory_record_sha256"]
    assert Path(receipt["external_manifest"]["path"]).exists()
    adaptation = json.loads(Path(receipt["cross_round_adaptation_receipt"]["path"]).read_text())
    assert adaptation["selected_evidence"]["memory_record_sha256"] == receipt["memory_write_receipts"][0]["memory_record_sha256"]
    assert "caused better later fitness" in adaptation["claims"]["does_not_prove"][0]


def test_am07_denies_writes_when_am06_blocks_promotion(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path, am06_status="FAIL")
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["promotion_decisions"] == []
    assert {item["decision"] for item in receipt["memory_write_decisions"]} == {"WRITE_DENIED"}
    assert all(item["memory_learning_allowed"] is False for item in receipt["rejection_decisions"])
    assert any("judge_authorized_evidence_missing" in item["reason_codes"] for item in receipt["rejection_decisions"])


def test_am07_revoked_source_denies_memory_write(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path, selected_record=_audit_record(source_revoked=True))
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["promotion_decisions"]
    assert receipt["memory_write_decisions"][0]["decision"] == "WRITE_DENIED"
    assert "source_revoked" in receipt["memory_write_decisions"][0]["reason_codes"]


def test_am07_duplicate_authorized_write_is_idempotent(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    first = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")
    second = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert first["status"] == "PASS"
    assert second["status"] == "PASS"
    assert second["memory_write_receipts"][0]["idempotent_reuse"] is True
    assert second["memory_write_receipts"][0]["memory_record_sha256"] == first["memory_write_receipts"][0]["memory_record_sha256"]


def test_am07_altered_memory_record_is_detected(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")
    record_path = Path(receipt["memory_write_receipts"][0]["memory_record_path"])
    record_path.write_text('{"tampered": true}\n', encoding="utf-8")

    verification = verify_memory_write_receipt(Path(receipt["memory_write_receipts"][0]["path"]))

    assert verification["status"] == "BLOCKED"
    assert verification["reason_codes"] == ["memory_record_sha256_mismatch"]


def test_am07_rejects_role_inappropriate_blue_promotion(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path, selected_role="blue", selected_verdict="RED_SUCCESS")
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["promotion_decisions"] == []
    assert any("role_inappropriate_promotion" in item["reason_codes"] for item in receipt["rejection_decisions"])


def test_am07_denies_write_without_judge_specimen_binding(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    r1_path = tmp_path / "round-01" / "round-receipt.json"
    round1 = json.loads(r1_path.read_text())
    round1["artifacts"].pop("judge_sha256")
    r1_sha = _write(r1_path, round1)
    score_path = tmp_path / "scorekeeper-receipt.json"
    scorekeeper = json.loads(score_path.read_text())
    scorekeeper["round_receipts"][0]["sha256"] = r1_sha
    score_sha = _write(score_path, scorekeeper)
    run = json.loads(run_path.read_text())
    run["scoreboard"]["sha256"] = score_sha
    _write(run_path, run)

    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["gates"]["durable_storage"]["status"] == "BLOCKED"
    assert receipt["memory_write_receipts"] == []
    assert receipt["memory_write_decisions"][0]["decision"] == "WRITE_DENIED"
    assert "judge_specimen_binding_missing" in receipt["memory_write_decisions"][0]["reason_codes"]


def test_am07_missing_consumption_evidence_blocks_storage_from_being_learning(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    scorekeeper = json.loads((tmp_path / "scorekeeper-receipt.json").read_text())
    scorekeeper["round_receipts"] = scorekeeper["round_receipts"][:1]
    score_sha = _write(tmp_path / "scorekeeper-receipt.json", scorekeeper)
    run = json.loads(run_path.read_text())
    run["scoreboard"]["sha256"] = score_sha
    _write(run_path, run)

    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["gates"]["durable_storage"]["status"] == "PASS"
    assert receipt["gates"]["later_consumption"]["status"] == "BLOCKED"
    assert receipt["gates"]["later_consumption"]["reason_codes"] == ["missing_consumption_evidence"]


def test_am07_blocks_tampered_scorekeeper_chain(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    run = json.loads(run_path.read_text())
    run["scoreboard"]["sha256"] = "0" * 64
    _write(run_path, run)

    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["status"] == "BLOCKED"
    assert receipt["promotion_decisions"] == []
    assert any(problem["reason"] == "sha256_mismatch" for problem in receipt["problems"])


def test_candidate_change_alone_is_not_efficacy(tmp_path: Path) -> None:
    run_path, am06_path = _fixture(tmp_path)
    receipt = build_am07_learning_receipt(run_receipt_path=run_path, am06_report_path=am06_path, out_dir=tmp_path / "am07")

    assert receipt["cross_round_adaptation_receipt"]["status"] == "PASS"
    assert receipt["efficacy"] == {
        "status": "NOT_CLAIMED",
        "reason_codes": ["controlled_comparison_not_supplied"],
        "controlled_comparison_receipt": None,
    }


def test_retained_agentic_eval_fixture_is_wired() -> None:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "agentic_eval_am07_learning.json"
    fixture = json.loads(fixture_path.read_text())
    assert fixture["retained"] is True
    assert fixture["am_gate"] == "AM-07"
    assert "run_am07_learning_eval.py" in fixture["command"]
    assert "Storage is not learning" in fixture["proof_boundary"]["does_not_prove"]
