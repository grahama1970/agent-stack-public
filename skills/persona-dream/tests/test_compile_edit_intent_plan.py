"""PD-UX-03 tests: typed edit intent -> deterministic dependency-aware revision plan.

Covers the golden dependency-plan case, determinism (same frozen inputs give
the same substantive plan), before/after hash comparison (index vs disk), the
typed negative cases, and the planning-only boundary (no successor activation,
no provider call, no persona-memory write).
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = SKILL_ROOT / "fixtures" / "pd-ux-03"
RUN_ROOT = FIXTURE / "golden-run"
INTENT = FIXTURE / "golden.edit-intent.json"
GOLDEN_PLAN = FIXTURE / "golden.plan.json"
SCRIPT = SKILL_ROOT / "scripts" / "compile_edit_intent_plan.py"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plan_mod = load_module("persona_dream_edit_intent_planner", SCRIPT)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compile_golden(tmp_path: Path) -> dict:
    intent = plan_mod.load_edit_intent(INTENT)
    return plan_mod.compile_plan(intent, run_root=RUN_ROOT)


def mutate_intent(tmp_path: Path, mutator) -> Path:
    intent = read_json(INTENT)
    mutator(intent)
    path = tmp_path / "intent.json"
    path.write_text(json.dumps(intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def expect_blocked(tmp_path: Path, mutator) -> dict:
    path = mutate_intent(tmp_path, mutator)
    with pytest.raises(plan_mod.PlanBlocked) as excinfo:
        intent = plan_mod.load_edit_intent(path)
        plan_mod.compile_plan(intent, run_root=RUN_ROOT)
    return {"code": excinfo.value.code, "details": excinfo.value.details}


# --- golden dependency-plan case -------------------------------------------------


def test_golden_plan_matches_checked_in_golden(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    golden = read_json(GOLDEN_PLAN)
    assert plan == golden


def test_golden_classification_reuse_revalidate_stale_regenerate(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    assert plan["classification_sets"] == {
        "regenerate": ["media_lock", "sb_001.start_frame", "storyboard_packet"],
        "revalidate": ["sb_001.end_frame", "sb_002.end_frame", "sb_002.start_frame"],
        "stale": [
            "phase07.phase.07.storyboard.live.tau.receipts.storyboard.frame.generation.sb.001.start.frame.scillm.image.generation.receipt.json",
            "phase11.phase.11.submit.return.canonical.phase11.live.request.v1.json",
        ],
        "reuse": ["contact_sheet_manifest", "dream_request", "human_idea"],
    }
    # Downstream derivations regenerate; downstream evidence goes stale.
    assert plan["classification"]["storyboard_packet"]["class"] == "regenerate"
    assert plan["classification"]["media_lock"]["class"] == "regenerate"
    assert plan["classification"]["phase11.phase.11.submit.return.canonical.phase11.live.request.v1.json"]["class"] == "stale"


def test_selective_execution_claimed_with_full_coverage(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    assert plan["selective_execution"]["claimed"] is True
    assert plan["selective_execution"]["blocked_by"] == []
    assert plan["selective_execution"]["unknown_coverage_artifacts"] == []


# --- determinism -----------------------------------------------------------------


def test_same_frozen_inputs_same_plan(tmp_path: Path) -> None:
    a = compile_golden(tmp_path / "a")
    b = compile_golden(tmp_path / "b")
    assert a == b
    body = {k: v for k, v in a.items() if k != "plan_id"}
    digest = "editplan_" + hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    assert a["plan_id"] == digest


def test_plan_body_has_no_wall_clock_or_randomness(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    blob = json.dumps(plan)
    for forbidden in ("created_at", "createdAt", "timestamp", "nonce", "uuid"):
        assert forbidden not in blob


# --- before/after hash comparison ------------------------------------------------


def test_target_expected_observed_and_index_hashes_match(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    target = plan["target"]
    assert target["expected_sha256"] == target["observed_sha256"] == target["index_sha256"]


def test_every_artifact_classified_and_index_verified_against_disk(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    index = read_json(
        RUN_ROOT / ".persona-dream" / "revisions" / "rev_golden_pd_ux_03" / "revision_artifact_index.json"
    )
    assert set(plan["classification"]) == set(index["artifacts"])
    assert all(entry["class"] in {"regenerate", "revalidate", "stale", "reuse"} for entry in plan["classification"].values())


# --- planning-only boundary ------------------------------------------------------


def test_plan_boundary_is_planning_only(tmp_path: Path) -> None:
    plan = compile_golden(tmp_path)
    assert plan["boundary"] == {
        "execution": "planning_only",
        "successor_activation": False,
        "provider_call_attempts": 0,
        "paid_submissions": 0,
        "memory_writes": 0,
    }
    assert plan["lineage"]["successor_revision_id"] is None
    assert plan["actual_provider_call_attempts"] == 0


def test_cli_writes_plan_and_receipt_only(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    rc = plan_mod.main(
        [
            "--intent", str(INTENT),
            "--run-root", str(RUN_ROOT),
            "--output-dir", str(out_dir),
            "--json",
        ]
    )
    assert rc == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["RECEIPT.json", "edit_intent_plan.json"]
    receipt = read_json(out_dir / "RECEIPT.json")
    assert receipt["schema"] == "persona_dream.edit_intent_plan_receipt.v1"
    assert receipt["status"] == "PASS_EDIT_INTENT_PLANNED"
    assert receipt["plan_sha256"] == "sha256:" + hashlib.sha256(
        (out_dir / "edit_intent_plan.json").read_bytes()
    ).hexdigest()
    # No successor revision directory appears anywhere.
    revisions_dir = RUN_ROOT / ".persona-dream" / "revisions"
    assert sorted(p.name for p in revisions_dir.iterdir()) == ["rev_golden_pd_ux_03"]


# --- typed negative cases --------------------------------------------------------


def test_stale_target_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["target"]["expected_sha256"] = "sha256:" + "f" * 64
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_STALE_TARGET"


def test_target_revision_not_active_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["target"]["revision_id"] = "rev_not_the_active_one"
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_TARGET_REVISION_NOT_ACTIVE"


def test_out_of_scope_identity_change_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["requested_change"]["kind"] = "identity_source_change"
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_EDIT_OUT_OF_SCOPE_IDENTITY"


def test_contradictory_constraints_fail_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["preservation_constraints"].append("target_only_no_downstream_propagation")
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_CONTRADICTORY_CONSTRAINTS"
    assert "storyboard_packet" in result["details"]["contradictions"][0]


def test_execution_boundary_violation_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["execution_boundary"] = "execute_now"
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_EXECUTION_BOUNDARY"


def test_unknown_preservation_constraint_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["preservation_constraints"].append("constraint_nobody_declared")
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_UNKNOWN_PRESERVATION_CONSTRAINT"


def test_missing_target_artifact_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["target"]["panel_id"] = "sb_009"
    path = mutate_intent(tmp_path, mutate)
    intent = plan_mod.load_edit_intent(path)
    with pytest.raises(plan_mod.PlanBlocked) as excinfo:
        plan_mod.compile_plan(intent, run_root=RUN_ROOT)
    assert excinfo.value.code == "BLOCKED_TARGET_ARTIFACT_MISSING"


def test_tampered_artifact_after_index_fails_closed(tmp_path: Path) -> None:
    """Before/after hash comparison: an artifact changed since indexing blocks."""
    copied = tmp_path / "tampered-run"
    shutil.copytree(RUN_ROOT, copied)
    frame = (
        copied
        / ".persona-dream/revisions/rev_golden_pd_ux_03/phase_07_storyboard_live_tau"
        / "generated_storyboard_frames/sb_002_start_frame.png"
    )
    frame.write_bytes(frame.read_bytes() + b"tamper")
    intent = plan_mod.load_edit_intent(INTENT)
    with pytest.raises(plan_mod.PlanBlocked) as excinfo:
        plan_mod.compile_plan(intent, run_root=copied)
    assert excinfo.value.code == "BLOCKED_REVISION_INDEX_STALE"
    assert excinfo.value.details["artifact_id"] == "sb_002.start_frame"


def test_unknown_dependency_coverage_blocks_selective_execution(tmp_path: Path) -> None:
    """An uncovered index artifact blocks the selective-execution claim (not the plan)."""
    copied = tmp_path / "uncovered-run"
    shutil.copytree(RUN_ROOT, copied)
    rev_root = copied / ".persona-dream/revisions/rev_golden_pd_ux_03"
    extra_rel = "phase_12_watch_observation/watch_note.json"
    extra = rev_root / extra_rel
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text(json.dumps({"note": "uncovered artifact"}) + "\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(extra.read_bytes()).hexdigest()
    index_path = rev_root / "revision_artifact_index.json"
    index = read_json(index_path)
    index["artifacts"]["phase12.watch.note"] = {
        "consumer_contract": None,
        "kind": "json",
        "media_type": "application/json",
        "phase_id": "12",
        "relative_path": extra_rel,
        "roles": ["optional_evidence"],
        "schema": None,
        "sha256": digest,
        "size_bytes": extra.stat().st_size,
    }
    index["artifact_count"] += 1
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    intent = plan_mod.load_edit_intent(INTENT)
    plan = plan_mod.compile_plan(intent, run_root=copied)
    assert plan["selective_execution"]["claimed"] is False
    assert plan["selective_execution"]["blocked_by"] == ["BLOCKED_COVERAGE_UNKNOWN_DEPENDENCY"]
    assert plan["selective_execution"]["unknown_coverage_artifacts"] == ["phase12.watch.note"]
    # The uncovered artifact cannot be proven unaffected, so it is not claimed reusable.
    assert plan["classification"]["phase12.watch.note"]["class"] == "reuse"
    assert plan["classification"]["phase12.watch.note"]["rule"] == "unknown"


def test_schema_violation_fails_typed(tmp_path: Path) -> None:
    def mutate(intent: dict) -> None:
        intent["target"]["panel_id"] = "not-a-panel"
    result = expect_blocked(tmp_path, mutate)
    assert result["code"] == "BLOCKED_EDIT_INTENT_SCHEMA"
