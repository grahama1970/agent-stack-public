from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from battle_skill.play_by_play import (
    PLAY_BY_PLAY_EVENT_SCHEMA,
    REQUIRED_EVENT_TYPES,
    PlayByPlayError,
    build_production_scale_timeline,
    event,
    evidence_ref,
    fact_from,
    read_receipt,
    rebuild_outputs,
    sha256_file,
    validate_timeline,
    write_commentary_jsonl,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _attempt(path: Path, *, label: str, exit_code: int, gate: str, when: str) -> Path:
    return _write(path, {
        "schema": "battle.production_scale.docker_attempt.v1",
        "label": label,
        "exit_code": exit_code,
        "semantic_exit_code": exit_code,
        "gate": gate,
        "process_status": "PASS",
        "infra_error": False,
        "created_at": when,
    })


def _receipt_root(tmp_path: Path) -> Path:
    root = tmp_path / "battle-run"
    arena = _write(root / "arena-contract.json", {"schema": "battle.production_scale.arena_contract.v1", "target_identity": "local@sha256:abc", "created_at": "2026-01-01T00:00:00Z"})
    auth = _write(root / "authorization-validation.json", {"schema": "security.target_authorization.receipt.v1", "status": "PASS", "created_at": "2026-01-01T00:00:01Z"})
    original = _attempt(root / "round-01" / "original" / "original-docker-receipt.json", label="original", exit_code=10, gate="EXPLOIT_CONFIRMED_EXIT10", when="2026-01-01T00:00:02Z")
    patched = _attempt(root / "round-01" / "patched" / "patched-docker-receipt.json", label="patched", exit_code=0, gate="PATCH_HOLD", when="2026-01-01T00:00:03Z")
    round_payload = {
        "schema": "battle.production_scale.round_receipt.v2",
        "status": "PASS",
        "round_number": 1,
        "vulnerability": "path traversal",
        "red_goal": "test traversal",
        "blue_plan": "contain paths",
        "red_scanned": ["path join", "absolute path"],
        "red_attempts": ["read public note", "try /tmp/secret.txt"],
        "blue_scanned": ["join point", "base directory"],
        "blue_attempts": ["realpath containment"],
        "verdict": "BLUE_SUCCESS",
        "finding_confirmed": True,
        "red_score": 0,
        "blue_score": 3,
        "incomplete_gates": [],
        "outcome_semantics": {
            "patched_block_signal_signed": True,
            "target_unavailability_is_defense": False,
        },
        "docker_attempts": [
            {"path": str(original), "sha256": sha256_file(original), "label": "original", "exit_code": 10, "gate": "EXPLOIT_CONFIRMED_EXIT10"},
            {"path": str(patched), "sha256": sha256_file(patched), "label": "patched", "exit_code": 0, "gate": "PATCH_HOLD"},
        ],
        "artifacts": {
            "original_app_sha256": "a" * 64,
            "patched_app_sha256": "b" * 64,
            "judge_sha256": "c" * 64,
        },
        "created_at": "2026-01-01T00:00:04Z",
    }
    round_path = _write(root / "round-01" / "round-receipt.json", round_payload)
    scorekeeper = _write(root / "scorekeeper-receipt.json", {"schema": "battle.production_scale.scorekeeper_receipt.v2", "status": "PASS", "red_total": 0, "blue_total": 3, "winner": "Blue", "created_at": "2026-01-01T00:00:05Z"})
    _write(root / "run-receipt.json", {
        "schema": "battle.production_scale.run_receipt.v2",
        "status": "PASS",
        "battle_id": "battle-run",
        "target_identity": "local@sha256:abc",
        "arena_contract": str(arena),
        "authorization_validation": str(auth),
        "rounds": [{"path": str(round_path), "sha256": sha256_file(round_path)}],
        "scorekeeper_receipt": str(scorekeeper),
        "created_at": "2026-01-01T00:00:06Z",
    })
    return root


def test_play_by_play_builds_semantic_outputs_and_restarts_from_prefix(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")

    assert timeline["schema"] == "battle.play_by_play_timeline.v1"
    assert timeline["status"] == "PASS"
    assert set(REQUIRED_EVENT_TYPES) <= {item["event_type"] for item in timeline["battle_events"]}
    assert timeline["round_events"][0]["round_number"] == 1
    assert timeline["battle_events"][-1]["event_type"] == "battle_closed"
    assert timeline["outputs_rebuilt_from_events"] == ["commentary_jsonl", "round_events", "battle_events", "projection"]
    assert Path(timeline["commentary_jsonl"]).read_text(encoding="utf-8").count("\n") == len(timeline["battle_events"])
    assert "does not infer motive, causality" in Path(timeline["projection"]).read_text(encoding="utf-8")

    commentary = Path(timeline["commentary_jsonl"])
    prefix = commentary.read_text(encoding="utf-8").splitlines()[:5]
    commentary.write_text("\n".join(prefix) + "\n", encoding="utf-8")
    restarted = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    assert restarted["commentary_jsonl_sha256"] == timeline["commentary_jsonl_sha256"]


def test_play_by_play_uses_scorekeeper_totals_as_final_score(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    scorekeeper_path = root / "scorekeeper-receipt.json"
    scorekeeper = json.loads(scorekeeper_path.read_text(encoding="utf-8"))
    scorekeeper["blue_total"] = 2
    scorekeeper["winner"] = "Blue"
    _write(scorekeeper_path, scorekeeper)

    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    scorekeeper_event = next(item for item in timeline["battle_events"] if item["event_type"] == "scorekeeper_final")

    assert scorekeeper_event["score_delta"] == {"red": 0, "blue": -1, "authority": "scorekeeper_receipt"}
    assert scorekeeper_event["score_after"] == {"red": 0, "blue": 2}
    assert timeline["battle_events"][-1]["score_after"] == {"red": 0, "blue": 2}
    assert "Score Red 0 / Blue 2" in Path(timeline["projection"]).read_text(encoding="utf-8")


def test_play_by_play_schema_has_required_fields_and_allowed_event_types(tmp_path: Path) -> None:
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "battle.play_by_play_event.v1.schema.json").read_text(encoding="utf-8"))
    assert len(schema["required"]) == 19
    root = _receipt_root(tmp_path)
    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    validator = Draft202012Validator(schema)
    for item in timeline["battle_events"]:
        validator.validate(item)
        assert item["schema"] == PLAY_BY_PLAY_EVENT_SCHEMA
        assert item["evidence_refs"]
        assert item["facts"]
        assert item["causal_refs"] or item["sequence"] == 1
        assert item["non_claims"]


def test_play_by_play_rejects_false_fact_attached_to_valid_receipt(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    event_payload = timeline["battle_events"][0]
    event_payload["facts"][0]["value"] = "wrong target"
    event_payload["facts"][0]["statement"] = '/target_identity = "wrong target"'

    with pytest.raises(PlayByPlayError, match="value mismatch"):
        validate_timeline([event_payload])


def test_play_by_play_rejects_broken_unsupported_and_reordered_events(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    source = read_receipt(root / "run-receipt.json")
    base_ref = evidence_ref(source, "/status")
    first = event(
        battle_id="b",
        sequence=1,
        event_type="battle_closed",
        actor="system",
        summary="Battle receipt recorded status PASS.",
        occurred_at="2026-01-01T00:00:01Z",
        evidence_refs=[base_ref],
    )
    with pytest.raises(PlayByPlayError, match="win_recorded requires"):
        event(
            battle_id="b",
            sequence=2,
            event_type="win_recorded",
            actor="red",
            summary="Red self-claim tries to claim a win.",
            occurred_at="2026-01-01T00:00:02Z",
            evidence_refs=[base_ref],
            causal_refs=[first["event_id"]],
            statement_kind="team_claim",
        )

    first["evidence_refs"][0]["sha256"] = "0" * 64
    with pytest.raises(PlayByPlayError, match="broken evidence ref"):
        validate_timeline([first])


def test_play_by_play_rejects_missing_terminal_truncated_write_and_bad_score_authority(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    events = [dict(item, causal_refs=[]) for item in timeline["battle_events"] if item["event_type"] != "terminal_event_recorded"]
    for sequence, item in enumerate(events, start=1):
        item["sequence"] = sequence
        item["event_id"] = f"{item['battle_id']}:{sequence:04d}:{item['event_type']}"
    with pytest.raises(PlayByPlayError, match="missing terminal events"):
        validate_timeline(events)

    truncated = root / "truncated.jsonl"
    truncated.write_text(json.dumps(timeline["battle_events"][0]) + "\n{" , encoding="utf-8")
    with pytest.raises(PlayByPlayError, match="truncated or invalid"):
        write_commentary_jsonl(truncated, timeline["battle_events"])

    source = read_receipt(root / "run-receipt.json")
    with pytest.raises(PlayByPlayError, match="nonzero score_delta"):
        event(
            battle_id="b",
            sequence=1,
            event_type="score_delta_recorded",
            actor="judge",
            summary="Score moves without authority.",
            occurred_at="2026-01-01T00:00:01Z",
            evidence_refs=[evidence_ref(source, "/status")],
            score_delta={"red": 1, "blue": 0, "authority": "none"},
        )


def test_play_by_play_rebuilds_outputs_from_retained_events(tmp_path: Path) -> None:
    root = _receipt_root(tmp_path)
    timeline = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    rebuilt = rebuild_outputs(events=timeline["battle_events"], out_dir=root / "rebuilt-play-by-play")

    assert rebuilt["semantic_coverage"]["missing_event_types"] == []
    assert Path(rebuilt["commentary_jsonl"]).read_text(encoding="utf-8")
    assert Path(rebuilt["projection"]).read_text(encoding="utf-8").startswith("# Receipt-backed Battle play-by-play")
