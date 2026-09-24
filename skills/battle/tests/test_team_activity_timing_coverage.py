from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from battle_skill.team_activity import (
    TEAM_ACTIVITY_SCHEMA,
    activity_record,
    build_production_activity_bundle,
    validate_team_activity_receipt,
)


def _timing(start: int = 1_000, end: int = 2_000, *, deadline: int | None = None) -> dict:
    payload = {
        "clock": "monotonic_ns",
        "start_monotonic_ns": start,
        "end_monotonic_ns": end,
        "duration_breakdown": {
            "queue_seconds": 0.0,
            "provider_seconds": 0.0,
            "build_seconds": 0.0,
            "runtime_seconds": 0.0,
            "replay_seconds": 0.0,
        },
    }
    if deadline is not None:
        payload["deadline_monotonic_ns"] = deadline
    return payload


def _receipt(activity: dict, *, required: list[str] | None = None, window: dict | None = None) -> dict:
    return {
        "schema": TEAM_ACTIVITY_SCHEMA,
        "status": "PASS",
        "battle_id": "battle-test",
        "run_id": "run-test",
        "team": activity["team"],
        "required_activity_ids": required or [activity["activity_id"]],
        "execution_window": window or {"enabled": False},
        "campaign_clock": "campaign-clock.json",
        "activities": [activity],
    }


def _red(activity_id: str = "red-1") -> dict:
    return activity_record(
        activity_id=activity_id,
        lane_id="lane-red",
        team="red",
        activity_kind="scan",
        timing=_timing(),
        inputs=[{"kind": "target"}],
        observations=[{"kind": "scan", "items": ["checked app.py"]}],
        artifact_refs=[{"path": "red.json", "sha256": "abc"}],
    )


def _blue(activity_id: str = "blue-1", *, kind: str = "proactive") -> dict:
    return activity_record(
        activity_id=activity_id,
        lane_id="lane-blue",
        team="blue",
        activity_kind="patch",
        timing=_timing(),
        inputs=[{"kind": "target"}],
        observations=[{"kind": "patch", "items": ["patched app.py"]}],
        artifact_refs=[{"path": "blue.json", "sha256": "def"}],
        blue_input_kind=kind,  # type: ignore[arg-type]
        judge_confirmed_refs=[{"path": "judge.json", "sha256": "ghi"}] if kind == "reactive" else None,
    )


def _problems(receipt: dict) -> list[str]:
    return validate_team_activity_receipt(receipt)["problems"]


def test_valid_activity_receipt_passes() -> None:
    assert validate_team_activity_receipt(_receipt(_red()))["status"] == "PASS"


def test_absent_required_lane_fails_closed() -> None:
    assert "missing_required_activity:red-2" in _problems(_receipt(_red("red-1"), required=["red-1", "red-2"]))


def test_incomplete_iteration_link_fails_closed() -> None:
    activity = _red()
    activity["iteration"]["parent_activity_ids"] = ["missing-parent"]
    assert "missing_iteration_link:red-1:missing-parent" in _problems(_receipt(activity))


def test_deadline_expiry_requires_terminal_disposition() -> None:
    activity = _red()
    activity["timing"] = _timing(start=1_000, end=3_000, deadline=2_000)
    assert "deadline_missed_without_terminal_disposition:red-1" in _problems(_receipt(activity))


def test_cancellation_requires_reason() -> None:
    activity = _red()
    activity["terminal_disposition"] = {"status": "cancelled"}
    assert "missing_cancellation_reason:red-1" in _problems(_receipt(activity))


def test_late_completion_requires_reason_but_is_deadline_aware() -> None:
    activity = _red()
    activity["timing"] = _timing(start=1_000, end=3_000, deadline=2_000)
    activity["terminal_disposition"] = {"status": "late_completed"}
    assert "missing_late_completion_reason:red-1" in _problems(_receipt(activity))


def test_duplicate_activity_ids_fail_closed() -> None:
    activity = _red()
    receipt = _receipt(activity)
    receipt["activities"] = [activity, deepcopy(activity)]
    assert "duplicate_activity_id:red-1" in _problems(receipt)


def test_clock_discontinuity_fails_closed() -> None:
    activity = _red()
    activity["timing"] = _timing(start=3_000, end=2_000)
    assert "clock_discontinuity:red-1" in _problems(_receipt(activity))


def test_proactive_blue_cannot_see_reactive_or_private_red_evidence() -> None:
    activity = _blue(kind="proactive")
    activity["judge_confirmed_refs"] = [{"path": "judge.json"}]
    activity["private_red_refs"] = [{"path": "red-private.json"}]
    problems = _problems(_receipt(activity))
    assert "unauthorized_reactive_evidence:blue-1" in problems
    assert "unauthorized_private_red_visibility:blue-1" in problems


def test_execution_window_enforced_only_when_enabled() -> None:
    activity = _red()
    disabled = _receipt(activity, window={"enabled": False, "start_monotonic_ns": 1_500, "end_monotonic_ns": 1_700})
    assert not any(problem.startswith("execution_window_violation") for problem in _problems(disabled))
    enabled = _receipt(activity, window={"enabled": True, "start_monotonic_ns": 1_500, "end_monotonic_ns": 1_700})
    assert "execution_window_violation:red-1" in _problems(enabled)


def _write(path: Path, payload: dict | str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def test_production_activity_bundle_records_terminal_and_source_coverage(tmp_path: Path) -> None:
    _write(tmp_path / "arena-contract.json")
    round_path = _write(tmp_path / "round-02" / "round-receipt.json")
    spawn_path = _write(tmp_path / "round-02" / "spawns" / "spawn-receipt.json")
    original_path = _write(tmp_path / "round-02" / "original" / "original-docker-receipt.json")
    patched_path = _write(tmp_path / "round-02" / "patched" / "patched-docker-receipt.json")
    deadline_path = _write(tmp_path / "round-02" / "spawns" / "deadline" / "preflight-docker-receipt.json")
    cancelled_path = _write(tmp_path / "round-02" / "spawns" / "cancelled" / "preflight-docker-receipt.json")
    promoted_path = _write(tmp_path / "round-02" / "spawns" / "promoted" / "preflight-docker-receipt.json")

    def attempt(path: Path, start: int, end: int, **extra: object) -> dict:
        return {
            "path": str(path),
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "duration_breakdown": {key: 0.0 for key in ("queue_seconds", "provider_seconds", "build_seconds", "runtime_seconds", "replay_seconds")},
            **extra,
        }

    run_receipt = {
        "battle_id": "battle-test",
        "rounds": [
            {
                "round_number": 2,
                "path": str(round_path),
                "red_goal": "probe",
                "blue_plan": "patch",
                "red_scanned": ["source"],
                "red_attempts": ["payload"],
                "blue_scanned": ["source"],
                "blue_attempts": ["patch"],
                "docker_attempts": [attempt(original_path, 10, 20), attempt(patched_path, 20, 30)],
                "spawn_receipt": {
                    "path": str(spawn_path),
                    "children": [
                        {"child_id": "deadline", "random_rank": 1, "hypothesis": "slow", "lineage_decision": "reject_dead_preflight", "preflight": attempt(deadline_path, 30, 50, deadline_monotonic_ns=40, timed_out=True, error="TimeoutExpired")},
                        {"child_id": "cancelled", "random_rank": 2, "hypothesis": "dead", "lineage_decision": "reject_dead_preflight", "preflight": attempt(cancelled_path, 50, 60)},
                        {"child_id": "path-realpath-containment-counter", "random_rank": 3, "hypothesis": "seed", "lineage_decision": "promote_to_rematch_seed", "preflight": attempt(promoted_path, 60, 70)},
                    ],
                },
            }
        ],
    }

    bundle = build_production_activity_bundle(root=tmp_path, run_receipt=run_receipt)
    assert bundle["status"] == "PASS"
    activities = []
    for ref in (bundle["team_activity_receipts"]["red"], bundle["team_activity_receipts"]["blue"]):
        activities.extend(json.loads(Path(ref["path"]).read_text(encoding="utf-8"))["activities"])
    statuses = {(activity["terminal_disposition"] or {}).get("status") for activity in activities}
    assert {"deadline_expired", "cancelled", "unlaunched"} <= statuses
    assert all(activity["research_refs"] for activity in activities)
    assert any(activity["timing"].get("deadline_monotonic_ns") for activity in activities)
