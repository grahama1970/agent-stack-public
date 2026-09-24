from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from battle_skill.complete_acceptance import (
    build_complete_acceptance_receipt,
    release_gate_from_complete_receipt,
    validate_complete_acceptance_receipt,
)
from battle_skill.production_adapter import evaluate_complete_release_gate


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _am06(status: str = "FAIL", *, winner=None, replay_tmp: bool = False, timeline: bool = False) -> dict:
    command = "python skills/battle/scripts/run_am06_reporting_eval.py --out artifacts/round-1"
    if replay_tmp:
        command = "python skills/battle/scripts/run_am06_reporting_eval.py --out /tmp/nonportable"
    return {
        "schema": "battle.am06.reporting.v1",
        "status": status,
        "battle_id": "battle-test",
        "fail_closed": True,
        "generated_from_receipts_only": True,
        "winner": winner,
        "red_blue_wins": {"red_round_wins": None, "blue_round_wins": None, "winner": None, "authority": None},
        "am04_timeline": {"status": "PASS"} if timeline else None,
        "attempt_rows": [
            {"row_id": "round-01:original", "round_number": 1, "lane": "red", "attempt_label": "original", "gate": "PASS", "verdict": "BLUE_SUCCESS", "judge_authorized_win": None, "judge_receipt": {"path": "round-01/original/attempt.json", "sha256": "abc"}, "artifact_hashes": {}},
            {"row_id": "round-01:patched", "round_number": 1, "lane": "blue", "attempt_label": "patched", "gate": "PASS", "verdict": "BLUE_SUCCESS", "judge_authorized_win": None, "judge_receipt": {"path": "round-01/patched/attempt.json", "sha256": "def"}, "artifact_hashes": {}},
        ],
        "lane_graph": {"nodes": [{"id": "round-1"}], "edges": []},
        "timing": {"rounds": []},
        "incomplete_gates": [{"gate": "SPAWN_PREFLIGHT"}],
        "false_green_revocations": [],
        "replay_commands": [{"label": "full_am06_local_docker_path", "command": command}],
        "artifact_hashes": [],
        "proof_boundary": {"proves": ["truthful fail-closed report"], "does_not_prove": ["Provider/Tau-generated Red and Blue creativity."]},
        "required_refs": [],
        "problems": [{"gate": "scorekeeper", "reason": "scorekeeper_not_passed"}],
        "human_report": "AM06_REPORT.md",
        "path": "am06-reporting.json",
    }


def _run_receipt(status: str = "GATES_INCOMPLETE") -> dict:
    return {"schema": "battle.production_scale.run_receipt.v2", "status": status, "winner": None, "blocked_rounds": [2]}


def _am05(*, qemu_status: str = "BLOCKED") -> dict:
    return {"schema": "battle.am05.run_receipt.v1", "status": "FAIL", "docker_lifecycle": {"status": "PASS"}, "qemu_lifecycle": {"status": qemu_status, "blocker": "qemu_base_image_not_supplied"}}


def _am07(status: str = "BLOCKED") -> dict:
    return {"schema": "battle.am07.learning.v1", "status": status, "gates": {"durable_storage": {"status": "WRITE_AUTHORIZED_NOT_PERFORMED"}}}


def test_complete_receipt_distinguishes_valid_report_from_completion_and_release(tmp_path: Path) -> None:
    am06 = _write(tmp_path / "am06.json", _am06("FAIL"))
    run = _write(tmp_path / "run.json", _run_receipt())
    am05 = _write(tmp_path / "am05.json", _am05())
    am07 = _write(tmp_path / "am07.json", _am07("BLOCKED"))

    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=am06,
        run_receipt_path=run,
        am05_receipt_path=am05,
        am07_receipt_path=am07,
    )

    validate_complete_acceptance_receipt(receipt)
    assert receipt["report_validity"]["status"] == "PASS"
    assert receipt["campaign_completion"]["status"] == "INCOMPLETE"
    assert receipt["release_eligibility"]["status"] == "BLOCKED"
    assert receipt["release_authorized"] is False
    assert receipt["aggregate_winner"] == {"authorized": False, "winner": None, "reason": receipt["aggregate_winner"]["reason"]}
    assert {row["id"] for row in receipt["requirements_ledger"]} == {f"AM-{i:02d}" for i in range(1, 11)}
    assert {row["legacy_am_id"] for row in receipt["requirement_crosswalk"]} == {f"AM-{i:02d}" for i in range(1, 11)}
    assert {"qemu_lifecycle_not_passed", "learning_incomplete"} <= {item["code"] for item in receipt["blockers"]}
    assert receipt["attempt_rows"]
    assert any("production deployment readiness" in claim for claim in receipt["exact_non_claims"])


def test_complete_gate_refuses_partial_score_promotion_and_stale_or_nonportable_inputs(tmp_path: Path) -> None:
    am06_payload = _am06("FAIL", winner="red", replay_tmp=True)
    am06 = _write(tmp_path / "am06.json", am06_payload)
    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=am06,
        run_receipt_path=_write(tmp_path / "run.json", _run_receipt()),
        am05_receipt_path=_write(tmp_path / "am05.json", _am05()),
        am07_receipt_path=_write(tmp_path / "am07.json", _am07()),
    )

    assert receipt["report_validity"]["status"] == "FAIL"
    assert receipt["aggregate_winner_authorized"] is False
    assert receipt["aggregate_winner"]["winner"] is None
    assert receipt["portable_replay"]["status"] == "FAIL"
    assert receipt["portable_replay"]["nonportable_references"]
    with pytest.raises(ValueError, match="release requires"):
        bad = dict(receipt)
        bad["release_authorized"] = True
        validate_complete_acceptance_receipt(bad)


def _forged_release(receipt: dict) -> dict:
    forged = copy.deepcopy(receipt)
    forged["status"] = "PASS"
    forged["blockers"] = []
    forged["report_validity"] = {"status": "PASS", "valid_failed_report_allowed": True, "problems": []}
    forged["campaign_completion"] = {"status": "COMPLETE", "winner": "blue", "blocker_count": 0}
    forged["release_eligibility"] = {"status": "ELIGIBLE", "missing_or_unproven_requirements": [], "blocker_count": 0}
    forged["release_authorized"] = True
    forged["aggregate_winner_authorized"] = True
    forged["aggregate_winner"] = {"authorized": True, "winner": "blue", "reason": "forged complete"}
    forged["portable_replay"] = {"status": "PASS", "commands": [], "readback_required": True, "nonportable_references": []}
    for row in forged["requirements_ledger"]:
        row["status"] = "IMPLEMENTED_AND_PROVEN"
        row["blockers"] = []
        row["evidence"] = row.get("evidence") or ["forged evidence"]
    return forged


def test_raw_release_gate_rejects_forged_public_receipt_claims(tmp_path: Path) -> None:
    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=_write(tmp_path / "am06.json", _am06("FAIL")),
        run_receipt_path=_write(tmp_path / "run.json", _run_receipt()),
        am05_receipt_path=_write(tmp_path / "am05.json", _am05()),
        am07_receipt_path=_write(tmp_path / "am07.json", _am07()),
    )

    cases: dict[str, dict] = {}
    cases["omitted_requirement"] = _forged_release(receipt)
    cases["omitted_requirement"]["requirements_ledger"] = [row for row in cases["omitted_requirement"]["requirements_ledger"] if row["id"] != "AM-02"] + [copy.deepcopy(cases["omitted_requirement"]["requirements_ledger"][-1])]
    cases["missing_attempt"] = _forged_release(receipt)
    cases["missing_attempt"]["attempt_rows"] = []
    cases["partial_score_promoted"] = _forged_release(receipt)
    cases["partial_score_promoted"]["status"] = "FAIL"
    cases["partial_score_promoted"]["aggregate_winner"] = {"authorized": True, "winner": "red", "reason": "forged partial"}
    cases["nonportable_replay_promoted"] = _forged_release(receipt)
    cases["nonportable_replay_promoted"]["portable_replay"] = {"status": "FAIL", "commands": [{"label": "tmp", "command": "python /tmp/x"}], "readback_required": True, "nonportable_references": [{"label": "tmp", "command": "python /tmp/x"}]}
    cases["downstream_failure_ignored"] = _forged_release(receipt)
    cases["downstream_failure_ignored"]["blockers"] = [{"code": "learning_incomplete", "status": "BLOCKED"}]
    cases["borrowed_review_sha"] = _forged_release(receipt)
    cases["borrowed_review_sha"]["source_refs"]["review"] = {"path": "borrowed-review.json", "sha256": "0" * 64, "schema": "borrowed"}
    cases["stale_requirement_ref"] = _forged_release(receipt)
    cases["stale_requirement_ref"]["requirements_ref"]["sha256"] = "0" * 64

    for name, forged in cases.items():
        with pytest.raises(ValueError):
            release_gate_from_complete_receipt(forged)


def test_release_gate_entry_points_block_until_all_requirements_proven(tmp_path: Path) -> None:
    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=_write(tmp_path / "am06.json", _am06("FAIL")),
        run_receipt_path=_write(tmp_path / "run.json", _run_receipt()),
        am05_receipt_path=_write(tmp_path / "am05.json", _am05()),
        am07_receipt_path=_write(tmp_path / "am07.json", _am07()),
    )

    gate = release_gate_from_complete_receipt(receipt)
    adapter_gate = evaluate_complete_release_gate(receipt)
    assert gate["status"] == "BLOCKED"
    assert gate == adapter_gate
    assert gate["aggregate_winner_authorized"] is False


def test_crosswalk_keeps_ticket_07_play_by_play_and_08_learning(tmp_path: Path) -> None:
    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=_write(tmp_path / "am06.json", _am06("FAIL", timeline=True)),
        run_receipt_path=_write(tmp_path / "run.json", _run_receipt()),
        am05_receipt_path=_write(tmp_path / "am05.json", _am05()),
        am07_receipt_path=_write(tmp_path / "am07.json", {"schema": "battle.am07.learning.v1", "status": "PASS"}),
    )

    crosswalk = {row["round1_ticket_id"]: row for row in receipt["requirement_crosswalk"]}
    rows = {row["id"]: row for row in receipt["requirements_ledger"]}
    assert crosswalk["07"]["requirement_id"] == "AM-07"
    assert "play-by-play" in crosswalk["07"]["title"]
    assert crosswalk["08"]["requirement_id"] == "AM-08"
    assert "learning" in crosswalk["08"]["title"]
    assert rows["AM-07"]["status"] == "IMPLEMENTED_AND_PROVEN"
    assert rows["AM-08"]["status"] == "IMPLEMENTED_AND_PROVEN"


def test_complete_acceptance_binds_local_learning_historical_and_qualification_deps(tmp_path: Path) -> None:
    deps = {
        "qualification_bundle": _write(tmp_path / "qualification.json", {"schema": "battle.qualification_bundle_verification_receipt.v1", "status": "PASS"}),
        "historical_backfill": _write(tmp_path / "historical.json", {"schema": "battle.gate_integrity_audit.v1", "status": "PASS"}),
        "provider_compile_replay": _write(tmp_path / "provider.json", {"schema": "battle.provider_artifact_continuity_receipt.v1", "status": "BLOCKED"}),
    }
    am07 = _write(tmp_path / "am07.json", {"schema": "battle.am07.learning.v1", "status": "PASS", "efficacy": {"status": "NOT_CLAIMED"}})

    receipt = build_complete_acceptance_receipt(
        out_dir=tmp_path / "complete",
        am06_report_path=_write(tmp_path / "am06.json", _am06("FAIL")),
        run_receipt_path=_write(tmp_path / "run.json", _run_receipt()),
        am05_receipt_path=_write(tmp_path / "am05.json", _am05()),
        am07_receipt_path=am07,
        dependency_receipts=deps,
    )

    rows = {row["id"]: row for row in receipt["requirements_ledger"]}
    assert rows["AM-01"]["status"] == "IMPLEMENTED_AND_PROVEN"
    assert rows["AM-08"]["status"] == "IMPLEMENTED_AND_PROVEN"
    assert rows["AM-09"]["status"] == "IMPLEMENTED_AND_PROVEN"
    assert rows["AM-03"]["status"] == "PARTIAL"
    assert receipt["source_refs"]["qualification_bundle"]["sha256"]
    assert receipt["release_authorized"] is False


def test_complete_receipt_schema_and_cli_readback(tmp_path: Path) -> None:
    am06 = _write(tmp_path / "am06.json", _am06("FAIL"))
    run = _write(tmp_path / "run.json", _run_receipt())
    am05 = _write(tmp_path / "am05.json", _am05())
    am07 = _write(tmp_path / "am07.json", _am07())
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/build_complete_acceptance_receipt.py",
            "--am06-report",
            str(am06),
            "--run-receipt",
            str(run),
            "--am05-receipt",
            str(am05),
            "--am07-receipt",
            str(am07),
            "--out",
            str(tmp_path / "complete"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 3
    receipt_path = json.loads(proc.stdout)["receipt"]
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "battle.complete_acceptance_receipt.v1.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(receipt))
    assert receipt["release_eligibility"]["status"] == "BLOCKED"
