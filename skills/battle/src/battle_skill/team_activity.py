"""Team activity and campaign clock receipts for Battle.

The receipts are coverage evidence only: they show what each lane did and when
it was observed. They do not score research quality, exploitability, or defense
success.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

TEAM_ACTIVITY_SCHEMA = "battle.team_activity_receipt.v1"
CAMPAIGN_CLOCK_SCHEMA = "battle.campaign_clock.v1"
TIMING_KEYS = ("queue_seconds", "provider_seconds", "build_seconds", "runtime_seconds", "replay_seconds")
TERMINAL_STATUSES = {"completed", "blocked", "cancelled", "deadline_expired", "late_completed", "unlaunched"}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _event_by_phase(events: list[dict[str, Any]], phase: str) -> dict[str, Any] | None:
    return next((event for event in events if event.get("phase") == phase), None)


def _timing_from_events(
    start_event: dict[str, Any] | None,
    end_event: dict[str, Any] | None,
    *,
    runtime_seconds: float | None = None,
) -> dict[str, Any]:
    start_ns = int((start_event or {}).get("monotonic_ns") or 0)
    end_ns = int((end_event or start_event or {}).get("monotonic_ns") or start_ns)
    elapsed = max(0.0, (end_ns - start_ns) / 1_000_000_000)
    runtime = elapsed if runtime_seconds is None else max(0.0, float(runtime_seconds))
    return {
        "clock": "monotonic_ns",
        "start_monotonic_ns": start_ns,
        "end_monotonic_ns": end_ns,
        "duration_breakdown": {
            "queue_seconds": 0.0,
            "provider_seconds": 0.0,
            "build_seconds": 0.0,
            "runtime_seconds": round(runtime, 6),
            "replay_seconds": 0.0,
        },
    }


def _timing_from_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    start_ns = int(attempt.get("start_monotonic_ns") or 0)
    end_ns = int(attempt.get("end_monotonic_ns") or start_ns)
    breakdown = {key: float((attempt.get("duration_breakdown") or {}).get(key, 0.0)) for key in TIMING_KEYS}
    if not any(breakdown.values()) and end_ns >= start_ns:
        breakdown["runtime_seconds"] = round((end_ns - start_ns) / 1_000_000_000, 6)
    return {
        "clock": "monotonic_ns",
        "start_monotonic_ns": start_ns,
        "end_monotonic_ns": end_ns,
        "duration_breakdown": breakdown,
        **({"deadline_monotonic_ns": attempt["deadline_monotonic_ns"]} if "deadline_monotonic_ns" in attempt else {}),
    }


def _instant_timing(anchor_ns: int) -> dict[str, Any]:
    return {
        "clock": "monotonic_ns",
        "start_monotonic_ns": anchor_ns,
        "end_monotonic_ns": anchor_ns,
        "duration_breakdown": {key: 0.0 for key in TIMING_KEYS},
    }


def activity_record(
    *,
    activity_id: str,
    lane_id: str,
    team: Literal["red", "blue"],
    activity_kind: str,
    timing: dict[str, Any],
    terminal_status: str = "completed",
    terminal_reason: str | None = None,
    model: str = "local-deterministic-fixture",
    persona: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    research_refs: list[dict[str, Any]] | None = None,
    iteration: dict[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    blue_input_kind: Literal["proactive", "reactive"] | None = None,
    judge_confirmed_refs: list[dict[str, Any]] | None = None,
    private_red_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    terminal = {"status": terminal_status}
    if terminal_reason:
        terminal["reason"] = terminal_reason
    record = {
        "activity_id": activity_id,
        "lane_id": lane_id,
        "team": team,
        "activity_kind": activity_kind,
        "model_provenance": {"model": model, "source": "configured_or_local_receipt"},
        "persona_provenance": {"persona": persona or f"battle-{team}-fixture", "source": "activity_builder"},
        "inputs": inputs or [],
        "observations": observations or [],
        "research_refs": research_refs or [],
        "iteration": iteration or {"iteration_id": activity_id, "attempt_number": 1, "parent_activity_ids": []},
        "artifact_refs": artifact_refs or [],
        "terminal_disposition": terminal,
        "timing": timing,
    }
    if blue_input_kind:
        record["blue_input_kind"] = blue_input_kind
    if judge_confirmed_refs:
        record["judge_confirmed_refs"] = judge_confirmed_refs
    if private_red_refs:
        record["private_red_refs"] = private_red_refs
    return record


def validate_team_activity_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if receipt.get("schema") != TEAM_ACTIVITY_SCHEMA:
        problems.append("schema_mismatch")
    activities = receipt.get("activities") or []
    if not isinstance(activities, list) or not activities:
        problems.append("missing_activities")
        activities = []
    required = set(receipt.get("required_activity_ids") or [])
    seen: set[str] = set()
    for activity in activities:
        activity_id = activity.get("activity_id")
        if not activity_id:
            problems.append("missing_activity_id")
            continue
        if activity_id in seen:
            problems.append(f"duplicate_activity_id:{activity_id}")
        seen.add(activity_id)
        if activity.get("team") not in {"red", "blue"}:
            problems.append(f"invalid_team:{activity_id}")
        if not activity.get("lane_id"):
            problems.append(f"missing_lane_id:{activity_id}")
        if not activity.get("model_provenance") or not activity.get("persona_provenance"):
            problems.append(f"missing_provenance:{activity_id}")
        if not activity.get("inputs"):
            problems.append(f"missing_inputs:{activity_id}")
        if not activity.get("observations") and not activity.get("research_refs"):
            problems.append(f"missing_observation_or_research:{activity_id}")
        if not activity.get("artifact_refs"):
            problems.append(f"missing_artifact_refs:{activity_id}")
        iteration = activity.get("iteration") or {}
        for parent_id in iteration.get("parent_activity_ids") or []:
            if parent_id not in seen:
                problems.append(f"missing_iteration_link:{activity_id}:{parent_id}")
        terminal = activity.get("terminal_disposition") or {}
        terminal_status = terminal.get("status")
        if terminal_status not in TERMINAL_STATUSES:
            problems.append(f"invalid_terminal_status:{activity_id}")
        if terminal_status in {"cancelled", "deadline_expired", "unlaunched"} and not terminal.get("reason"):
            problems.append(f"missing_cancellation_reason:{activity_id}")
        timing = activity.get("timing") or {}
        start_ns = timing.get("start_monotonic_ns")
        end_ns = timing.get("end_monotonic_ns")
        if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns < start_ns:
            problems.append(f"clock_discontinuity:{activity_id}")
            elapsed = 0.0
        else:
            elapsed = (end_ns - start_ns) / 1_000_000_000
        breakdown = timing.get("duration_breakdown") or {}
        if set(TIMING_KEYS) - set(breakdown):
            problems.append(f"missing_duration_breakdown:{activity_id}")
        total = 0.0
        for key in TIMING_KEYS:
            value = breakdown.get(key, 0.0)
            if not isinstance(value, (int, float)) or value < 0:
                problems.append(f"negative_duration:{activity_id}:{key}")
            total += float(value or 0.0)
        if elapsed and total > elapsed + 0.001:
            problems.append(f"duration_exceeds_elapsed:{activity_id}")
        deadline = timing.get("deadline_monotonic_ns")
        if isinstance(deadline, int) and end_ns and end_ns > deadline and terminal_status not in {"deadline_expired", "late_completed"}:
            problems.append(f"deadline_missed_without_terminal_disposition:{activity_id}")
        if terminal_status == "late_completed" and not terminal.get("reason"):
            problems.append(f"missing_late_completion_reason:{activity_id}")
        if activity.get("team") == "blue":
            if activity.get("blue_input_kind") == "proactive" and (activity.get("judge_confirmed_refs") or activity.get("private_red_refs")):
                problems.append(f"unauthorized_reactive_evidence:{activity_id}")
            if activity.get("blue_input_kind") == "reactive" and not activity.get("judge_confirmed_refs"):
                problems.append(f"missing_judge_confirmed_reactive_input:{activity_id}")
            if activity.get("private_red_refs"):
                problems.append(f"unauthorized_private_red_visibility:{activity_id}")
    missing = required - seen
    for activity_id in sorted(missing):
        problems.append(f"missing_required_activity:{activity_id}")
    window = receipt.get("execution_window") or {"enabled": False}
    if window.get("enabled"):
        start = window.get("start_monotonic_ns")
        end = window.get("end_monotonic_ns")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            problems.append("invalid_execution_window")
        else:
            for activity in activities:
                timing = activity.get("timing") or {}
                if timing.get("start_monotonic_ns", start) < start or timing.get("end_monotonic_ns", end) > end:
                    problems.append(f"execution_window_violation:{activity.get('activity_id')}")
    return {"status": "PASS" if not problems else "FAIL", "problems": problems}


def build_campaign_clock(*, battle_id: str, run_id: str, activities: list[dict[str, Any]]) -> dict[str, Any]:
    timings = [activity.get("timing") or {} for activity in activities]
    starts = [timing.get("start_monotonic_ns") for timing in timings if isinstance(timing.get("start_monotonic_ns"), int)]
    ends = [timing.get("end_monotonic_ns") for timing in timings if isinstance(timing.get("end_monotonic_ns"), int)]
    return {
        "schema": CAMPAIGN_CLOCK_SCHEMA,
        "status": "PASS" if starts and ends else "FAIL",
        "battle_id": battle_id,
        "run_id": run_id,
        "clock": "monotonic_ns",
        "start_monotonic_ns": min(starts) if starts else None,
        "end_monotonic_ns": max(ends) if ends else None,
        "concurrency_basis": "explicit_activity_records_only_not_adjacent_wall_clock",
        "duration_components": list(TIMING_KEYS),
        "activity_count": len(activities),
        "activity_ids": [activity["activity_id"] for activity in activities],
    }


def validate_campaign_clock(clock: dict[str, Any], activities: list[dict[str, Any]]) -> dict[str, Any]:
    problems: list[str] = []
    if clock.get("schema") != CAMPAIGN_CLOCK_SCHEMA:
        problems.append("schema_mismatch")
    if clock.get("concurrency_basis") != "explicit_activity_records_only_not_adjacent_wall_clock":
        problems.append("invalid_concurrency_basis")
    start = clock.get("start_monotonic_ns")
    end = clock.get("end_monotonic_ns")
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        problems.append("clock_discontinuity")
    ids = {activity.get("activity_id") for activity in activities}
    if set(clock.get("activity_ids") or []) != ids:
        problems.append("activity_id_mismatch")
    return {"status": "PASS" if not problems else "FAIL", "problems": problems}


def _receipt_ref(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": _sha256_file(path)} if path.is_file() else {"path": str(path), "missing": True}


def build_reactive_activity_bundle(*, out_dir: Path, battle_id: str, run_id: str, ledger_path: Path) -> dict[str, Any]:
    ledger = _read_json(ledger_path)
    events = ledger.get("events") or []
    red_path = out_dir / "red-hack-observation.json"
    proactive_path = out_dir / "blue" / "proactive-blue-input.json"
    reactive_input_path = out_dir / "blue" / "reactive-blue-input.json"
    patch_path = out_dir / "blue" / "candidate-patch-receipt.json"
    judge1_path = out_dir / "judge-1" / "judge-1-receipt.json"
    red = activity_record(
        activity_id="reactive-round:red:probe",
        lane_id="reactive-round:red",
        team="red",
        activity_kind="scan_and_probe",
        timing=_timing_from_events(_event_by_phase(events, "red_started"), _event_by_phase(events, "red_observation_received")),
        inputs=[_receipt_ref(out_dir / "immutable-baseline-manifest.json")],
        observations=[_receipt_ref(red_path)],
        research_refs=[_receipt_ref(ledger_path), _receipt_ref(red_path)],
        artifact_refs=[_receipt_ref(red_path)],
        persona="battle-red-probe-fixture",
    )
    blue_activities = [
        activity_record(
            activity_id="reactive-round:blue:proactive",
            lane_id="reactive-round:blue-proactive",
            team="blue",
            activity_kind="proactive_defense_input",
            timing=_timing_from_events(_event_by_phase(events, "proactive_blue_started"), _event_by_phase(events, "proactive_blue_terminal")),
            inputs=[_receipt_ref(out_dir / "immutable-baseline-manifest.json")],
            observations=[_receipt_ref(proactive_path)],
            research_refs=[_receipt_ref(ledger_path), _receipt_ref(proactive_path)],
            artifact_refs=[_receipt_ref(proactive_path)],
            blue_input_kind="proactive",
            persona="battle-blue-proactive-fixture",
        )
    ]
    if reactive_input_path.is_file() or patch_path.is_file():
        blue_activities.append(
            activity_record(
                activity_id="reactive-round:blue:reactive",
                lane_id="reactive-round:blue-reactive",
                team="blue",
                activity_kind="judge_confirmed_reactive_patch",
                timing=_timing_from_events(_event_by_phase(events, "reactive_blue_started"), _event_by_phase(events, "candidate_patch_received")),
                inputs=[_receipt_ref(reactive_input_path)],
                observations=[_receipt_ref(patch_path)],
                research_refs=[_receipt_ref(ledger_path), _receipt_ref(judge1_path), _receipt_ref(reactive_input_path)],
                artifact_refs=[_receipt_ref(patch_path)],
                iteration={"iteration_id": "reactive-round:blue:reactive", "attempt_number": 2, "parent_activity_ids": ["reactive-round:blue:proactive"]},
                blue_input_kind="reactive",
                judge_confirmed_refs=[_receipt_ref(judge1_path)],
                persona="battle-blue-reactive-fixture",
            )
        )
    return _write_team_bundle(out_dir=out_dir / "activity-coverage", battle_id=battle_id, run_id=run_id, red_activities=[red], blue_activities=blue_activities)


def build_production_activity_bundle(*, root: Path, run_receipt: dict[str, Any]) -> dict[str, Any]:
    red_activities: list[dict[str, Any]] = []
    blue_activities: list[dict[str, Any]] = []
    for round_receipt in run_receipt.get("rounds") or []:
        round_num = int(round_receipt.get("round_number") or 0)
        original, patched = (round_receipt.get("docker_attempts") or [{}, {}])[:2]
        round_path = Path(str(round_receipt.get("path") or root / f"round-{round_num:02d}" / "round-receipt.json"))
        red_activities.append(
            activity_record(
                activity_id=f"round-{round_num:02d}:red:scan-probe",
                lane_id=f"round-{round_num:02d}:red",
                team="red",
                activity_kind="scan_and_probe",
                timing=_timing_from_attempt(original),
                inputs=[{"kind": "goal", "value": round_receipt.get("red_goal")}, _receipt_ref(root / "arena-contract.json")],
                observations=[{"kind": "scan", "items": round_receipt.get("red_scanned") or []}, {"kind": "attempts", "items": round_receipt.get("red_attempts") or []}],
                research_refs=[_receipt_ref(round_path), _receipt_ref(root / "arena-contract.json")],
                artifact_refs=[_receipt_ref(Path(str(original.get("path") or ""))) if original.get("path") else _receipt_ref(round_path)],
                persona="battle-red-production-fixture",
            )
        )
        blue_activities.append(
            activity_record(
                activity_id=f"round-{round_num:02d}:blue:proactive-patch",
                lane_id=f"round-{round_num:02d}:blue-proactive",
                team="blue",
                activity_kind="proactive_patch_attempt",
                timing=_timing_from_attempt(patched),
                inputs=[{"kind": "plan", "value": round_receipt.get("blue_plan")}, _receipt_ref(root / "arena-contract.json")],
                observations=[{"kind": "scan", "items": round_receipt.get("blue_scanned") or []}, {"kind": "attempts", "items": round_receipt.get("blue_attempts") or []}],
                research_refs=[_receipt_ref(round_path), _receipt_ref(root / "arena-contract.json")],
                artifact_refs=[_receipt_ref(Path(str(patched.get("path") or ""))) if patched.get("path") else _receipt_ref(round_path)],
                blue_input_kind="proactive",
                persona="battle-blue-production-fixture",
            )
        )
        spawn = round_receipt.get("spawn_receipt") or {}
        spawn_path = Path(str(spawn.get("path") or root / f"round-{round_num:02d}" / "spawns" / "spawn-receipt.json"))
        for child in spawn.get("children") or []:
            preflight = child.get("preflight") or {}
            team = "blue" if any(token in str(child.get("child_id")) for token in ("blue", "containment", "counter")) else "red"
            target = blue_activities if team == "blue" else red_activities
            decision = child.get("lineage_decision")
            if preflight.get("timed_out"):
                terminal_status = "deadline_expired"
                terminal_reason = preflight.get("error") or "preflight deadline expired"
            elif decision == "reject_dead_preflight":
                terminal_status = "cancelled"
                terminal_reason = "lineage cancelled after dead preflight"
            else:
                terminal_status = "completed"
                terminal_reason = str(decision) if decision else None
            parent_id = f"round-{round_num:02d}:{team}:" + ("proactive-patch" if team == "blue" else "scan-probe")
            target.append(
                activity_record(
                    activity_id=f"round-{round_num:02d}:{team}:spawn:{child.get('child_id')}",
                    lane_id=f"round-{round_num:02d}:{team}:spawn",
                    team=team,  # type: ignore[arg-type]
                    activity_kind="spawn_preflight",
                    timing=_timing_from_attempt(preflight),
                    terminal_status=terminal_status,
                    terminal_reason=terminal_reason,
                    inputs=[{"kind": "hypothesis", "value": child.get("hypothesis")}],
                    observations=[{"kind": "preflight", "gate": preflight.get("gate"), "exit_code": preflight.get("exit_code")}],
                    research_refs=[_receipt_ref(spawn_path), _receipt_ref(round_path)],
                    artifact_refs=[_receipt_ref(Path(str(preflight.get("path") or ""))) if preflight.get("path") else _receipt_ref(round_path)],
                    iteration={"iteration_id": str(child.get("child_id")), "attempt_number": int(child.get("random_rank") or 1), "parent_activity_ids": [parent_id]},
                    persona=f"battle-{team}-spawn-fixture",
                )
            )
            if decision == "promote_to_rematch_seed":
                anchor = int(preflight.get("end_monotonic_ns") or preflight.get("start_monotonic_ns") or 0)
                target.append(
                    activity_record(
                        activity_id=f"round-{round_num:02d}:{team}:rematch-unlaunched:{child.get('child_id')}",
                        lane_id=f"round-{round_num:02d}:{team}:rematch",
                        team=team,  # type: ignore[arg-type]
                        activity_kind="rematch_seed_not_launched",
                        timing=_instant_timing(anchor),
                        terminal_status="unlaunched",
                        terminal_reason="promoted seed retained; rematch launch is outside the small/medium proof run",
                        inputs=[{"kind": "promoted_seed", "value": child.get("child_id")}],
                        observations=[{"kind": "launch_accounting", "launched": False}],
                        research_refs=[_receipt_ref(spawn_path), _receipt_ref(round_path)],
                        artifact_refs=[_receipt_ref(spawn_path)],
                        iteration={"iteration_id": f"rematch:{child.get('child_id')}", "attempt_number": int(child.get("random_rank") or 1), "parent_activity_ids": [f"round-{round_num:02d}:{team}:spawn:{child.get('child_id')}"]},
                        persona=f"battle-{team}-spawn-fixture",
                    )
                )
    return _write_team_bundle(out_dir=root / "activity-coverage", battle_id=str(run_receipt.get("battle_id") or root.name), run_id=str(run_receipt.get("battle_id") or root.name), red_activities=red_activities, blue_activities=blue_activities)


def _write_team_bundle(*, out_dir: Path, battle_id: str, run_id: str, red_activities: list[dict[str, Any]], blue_activities: list[dict[str, Any]]) -> dict[str, Any]:
    all_activities = red_activities + blue_activities
    clock = build_campaign_clock(battle_id=battle_id, run_id=run_id, activities=all_activities)
    clock_validation = validate_campaign_clock(clock, all_activities)
    clock["validation"] = clock_validation
    clock_path = _write_json(out_dir / "campaign-clock.json", clock)
    receipts: dict[str, Any] = {}
    for team, activities in (("red", red_activities), ("blue", blue_activities)):
        receipt = {
            "schema": TEAM_ACTIVITY_SCHEMA,
            "status": "PASS",
            "battle_id": battle_id,
            "run_id": run_id,
            "team": team,
            "required_activity_ids": [activity["activity_id"] for activity in activities],
            "execution_window": {"enabled": False},
            "campaign_clock": str(clock_path),
            "activities": activities,
            "claims": {
                "proves": ["explicit team activity and monotonic timing coverage was recorded"],
                "does_not_prove": ["research quality", "vulnerability discovery", "defense success", "model improvement", "production throughput"],
            },
        }
        validation = validate_team_activity_receipt(receipt)
        receipt["status"] = validation["status"]
        receipt["validation"] = validation
        path = _write_json(out_dir / f"{team}-team-activity-receipt.json", receipt)
        receipts[team] = {"path": str(path), "sha256": _sha256_file(path), "status": receipt["status"]}
    return {
        "schema": "battle.team_activity_coverage_bundle.v1",
        "status": "PASS" if clock_validation["status"] == "PASS" and all(item["status"] == "PASS" for item in receipts.values()) else "FAIL",
        "campaign_clock": {"path": str(clock_path), "sha256": _sha256_file(clock_path), "status": clock["status"]},
        "team_activity_receipts": receipts,
    }
