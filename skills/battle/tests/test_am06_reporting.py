from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from battle_skill.am06_reporting import AM06_SCHEMA, build_am06_report, validate_am06_report
from battle_skill.arena_intent import bind_intent, build_arena_maker_intent, freeze_arena_maker_intent
from battle_skill.play_by_play import build_production_scale_timeline, sha256_file


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _receipt_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "battle-am06"
    root.mkdir()
    arena = _write(root / "arena-contract.json", {"schema": "battle.production_scale.arena_contract.v1", "created_at": "2026-01-01T00:00:00Z"})
    auth = _write(root / "authorization.json", {"schema": "security.target_authorization.v1", "target": "local@sha256:abc"})
    binding = freeze_arena_maker_intent(
        build_arena_maker_intent(
            battle_id="battle-am06",
            target_identity="local@sha256:abc",
            authorization_manifest=auth,
            arena_contract=arena,
            runtime_mode="local_docker_judge_medium_arena",
            round_count=1,
            objective="Prove AM-06 receipt-derived reporting.",
        ),
        root / "arena-maker-intent.json",
    )
    _write(root / "authorization-validation.json", bind_intent({"schema": "security.target_authorization.receipt.v1", "status": "PASS", "created_at": "2026-01-01T00:00:01Z"}, binding))

    original_payload = bind_intent({
        "schema": "battle.production_scale.docker_attempt.v1",
        "status": "PASS",
        "process_status": "PASS",
        "label": "original",
        "image": "python:3.12-slim",
        "network": "none",
        "started_at": "2026-01-01T00:00:02Z",
        "completed_at": "2026-01-01T00:00:03Z",
        "exit_code": 10,
        "semantic_exit_code": 10,
        "infra_error": False,
        "timed_out": False,
        "gate": "EXPLOIT_CONFIRMED_EXIT10",
        "stdout": '{"judge_exit_signal":10,"probe_result":"confirmed"}\n',
        "stderr": "",
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }, binding)
    patched_payload = bind_intent({
        "schema": "battle.production_scale.docker_attempt.v1",
        "status": "PASS",
        "process_status": "PASS",
        "label": "patched",
        "image": "python:3.12-slim",
        "network": "none",
        "started_at": "2026-01-01T00:00:04Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "exit_code": 0,
        "semantic_exit_code": 0,
        "infra_error": False,
        "timed_out": False,
        "gate": "PATCH_HOLD",
        "stdout": '{"judge_exit_signal":0,"probe_result":"blocked"}\n',
        "stderr": "",
        "stdout_sha256": "c" * 64,
        "stderr_sha256": "d" * 64,
    }, binding)
    original = _write(root / "round-01" / "original" / "original-docker-receipt.json", original_payload)
    patched = _write(root / "round-01" / "patched" / "patched-docker-receipt.json", patched_payload)
    round_payload = bind_intent({
        "schema": "battle.production_scale.round_receipt.v2",
        "status": "PASS",
        "round_number": 1,
        "vulnerability": "path traversal",
        "red_goal": "read outside base",
        "blue_plan": "contain paths",
        "expected_balance": "Blue-favored",
        "red_scanned": ["checked path join"],
        "red_attempts": ["sent traversal payload"],
        "blue_scanned": ["reviewed path containment"],
        "blue_attempts": ["normalized requested path"],
        "adaptive_lineage_created": ["no promoted child"],
        "finding_confirmed": True,
        "outcome_semantics": {"patched_block_signal_signed": True, "target_unavailability_is_defense": False},
        "verdict": "BLUE_SUCCESS",
        "red_score": 0,
        "blue_score": 3,
        "incomplete_gates": [],
        "docker_attempts": [
            {"path": str(original), "sha256": sha256_file(original), "label": "original", "exit_code": 10, "gate": "EXPLOIT_CONFIRMED_EXIT10"},
            {"path": str(patched), "sha256": sha256_file(patched), "label": "patched", "exit_code": 0, "gate": "PATCH_HOLD"},
        ],
        "artifacts": {"original_app_sha256": "e" * 64, "patched_app_sha256": "f" * 64, "judge_sha256": "1" * 64},
        "created_at": "2026-01-01T00:00:06Z",
    }, binding)
    round_path = _write(root / "round-01" / "round-receipt.json", round_payload)
    scorekeeper_payload = bind_intent({
        "schema": "battle.production_scale.scorekeeper_receipt.v2",
        "status": "PASS",
        "red_total": 0,
        "blue_total": 3,
        "red_round_wins": 0,
        "blue_round_wins": 1,
        "winner": "Blue",
        "round_receipts": [{"path": str(round_path), "sha256": sha256_file(round_path)}],
        "incomplete_gates": [],
        "created_at": "2026-01-01T00:00:07Z",
    }, binding)
    scorekeeper = _write(root / "scorekeeper-receipt.json", scorekeeper_payload)
    run_payload = {
        "schema": "battle.production_scale.run_receipt.v2",
        "status": "PASS",
        "mocked": False,
        "live": "local_docker_judge_medium_arena",
        "battle_id": "battle-am06",
        "target_identity": "local@sha256:abc",
        "authorization_validation": str(root / "authorization-validation.json"),
        "arena_contract": str(arena),
        "arena_maker_intent": binding,
        "rounds": [{"path": str(round_path), "sha256": sha256_file(round_path)}],
        "scoreboard": {**scorekeeper_payload, "sha256": sha256_file(scorekeeper)},
        "scorekeeper_receipt": str(scorekeeper),
        "winner": "Blue",
        "incomplete_gates": [],
        "claims": {"proves": ["receipt-backed local Battle"], "does_not_prove": ["external deployment"]},
        "created_at": "2026-01-01T00:00:08Z",
    }
    run_path = _write(root / "run-receipt.json", run_payload)
    timeline = build_production_scale_timeline(run_receipt_path=run_path, out_dir=root / "play-by-play")
    _write(root / "gate-integrity-audit.json", {
        "schema": "battle.gate_integrity_audit.v1",
        "status": "PASS",
        "run_receipt": str(run_path),
        "run_receipt_sha256": sha256_file(run_path),
        "scorekeeper_sha256": sha256_file(scorekeeper),
        "false_green_parent_count": 0,
        "incomplete_gates": [],
        "created_at": "2026-01-01T00:00:09Z",
    })
    am05_payload = bind_intent({
        "schema": "battle.am05.run_receipt.v1",
        "status": "PASS",
        "mocked": False,
        "live": True,
        "docker_lifecycle": {"path": str(root / "am05" / "docker-lifecycle-receipt.json"), "sha256": "sha256:" + "2" * 64, "status": "PASS"},
        "qemu_lifecycle": {"path": str(root / "am05" / "qemu-lifecycle-receipt.json"), "sha256": "sha256:" + "3" * 64, "status": "BLOCKED", "blocker": "qemu_base_image_not_supplied"},
        "parent_gate": {"path": str(root / "am05" / "parent-gate-receipt.json"), "sha256": "sha256:" + "4" * 64, "status": "PASS"},
        "artifact_manifest": {"path": str(root / "am05" / "artifact-manifest.json"), "sha256": "sha256:" + "5" * 64, "status": "PASS"},
        "created_at": "2026-01-01T00:00:10Z",
    }, binding)
    am05 = _write(root / "am05" / "am05-run-receipt.json", am05_payload)
    assert Path(timeline["path"]).is_file()
    return run_path, am05


def test_am06_report_is_machine_json_and_human_markdown_from_verified_receipts(tmp_path: Path) -> None:
    run_path, am05 = _receipt_fixture(tmp_path)
    report = build_am06_report(run_receipt_path=run_path, out_dir=tmp_path / "out", am05_receipt_path=am05)

    assert report["schema"] == AM06_SCHEMA
    assert report["status"] == "PASS"
    assert report["winner"] == "Blue"
    assert len(report["attempt_rows"]) == 2
    assert report["attempt_rows"][1]["judge_authorized_win"] == "blue"
    assert report["am04_timeline"]["path"].endswith("play-by-play-timeline.json")
    assert report["am05_lifecycle"]["path"].endswith("am05-run-receipt.json")
    assert report["replay_commands"]
    assert "external deployment" in report["proof_boundary"]["does_not_prove"]
    assert Path(report["human_report"]).read_text(encoding="utf-8").startswith("# AM-06")
    validate_am06_report(report)

    schema = json.loads(Path("schemas/battle.am06.reporting.v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(json.loads(Path(report["path"]).read_text(encoding="utf-8")))


def test_am06_fails_closed_on_tampered_attempt_ref(tmp_path: Path) -> None:
    run_path, am05 = _receipt_fixture(tmp_path)
    attempt = run_path.parent / "round-01" / "patched" / "patched-docker-receipt.json"
    payload = json.loads(attempt.read_text(encoding="utf-8"))
    payload["gate"] = "TAMPERED_GREEN"
    _write(attempt, payload)

    report = build_am06_report(run_receipt_path=run_path, out_dir=tmp_path / "out", am05_receipt_path=am05)

    assert report["status"] == "FAIL"
    assert report["winner"] is None
    assert report["red_blue_wins"]["winner"] is None
    assert any(problem["reason"] == "tampered_ref" for problem in report["problems"])


def test_am06_fails_closed_without_am05_lifecycle(tmp_path: Path) -> None:
    run_path, _am05 = _receipt_fixture(tmp_path)

    report = build_am06_report(run_receipt_path=run_path, out_dir=tmp_path / "out", am05_receipt_path=tmp_path / "missing-am05.json")

    assert report["status"] == "FAIL"
    assert report["winner"] is None
    assert any(problem["gate"] == "AM-05" for problem in report["problems"])
