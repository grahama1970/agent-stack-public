"""AM-06 receipt-derived reporting.

Acceptance report builder for Battle runs. It reads only receipts, verifies every
referenced hash it uses, and fails closed: no Red/Blue winner is emitted when a
required AM-03/AM-04/AM-05 or Judge/scorekeeper reference is missing or tampered.
"""
from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .arena_intent import verify_bound_payload, verify_frozen_intent
from .play_by_play import validate_timeline

AM06_SCHEMA = "battle.am06.reporting.v1"
AM06_MARKDOWN_SCHEMA = "battle.am06.human_report.v1"


@dataclass(frozen=True)
class JsonReceipt:
    path: Path
    payload: dict[str, Any]
    sha256: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_sha256(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def receipt_ref(source: JsonReceipt, pointer: str = "") -> dict[str, str]:
    return {
        "path": str(source.path),
        "sha256": source.sha256,
        "schema": str(source.payload.get("schema") or "unknown"),
        "json_pointer": pointer,
    }


def build_am06_report(
    *,
    run_receipt_path: Path,
    out_dir: Path,
    am05_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Build machine JSON and Markdown report from receipts only."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[dict[str, Any]] = []
    required_refs: list[dict[str, Any]] = []

    run = _read_json_receipt(Path(run_receipt_path), None, problems, "run_receipt")
    if run is None:
        report = _failure_report(out_dir, problems, required_refs)
        _write_outputs(report, out_dir)
        return report

    root = run.path.parent
    run_payload = run.payload
    battle_id = str(run_payload.get("battle_id") or root.name)
    intent_binding = run_payload.get("arena_maker_intent") if isinstance(run_payload.get("arena_maker_intent"), dict) else None
    intent_status = _intent_status(intent_binding)
    if intent_status.get("status") != "PASS":
        problems.append({"gate": "AM-03", "reason": "arena_maker_intent_failed", "details": intent_status.get("problems", [])})
    required_refs.append({"gate": "AM-03", "status": intent_status.get("status"), "ref": intent_binding or {}})

    scorekeeper = _read_json_receipt(
        Path(str(run_payload.get("scorekeeper_receipt") or root / "scorekeeper-receipt.json")),
        _nested_sha(run_payload, "scoreboard"),
        problems,
        "scorekeeper_receipt",
    )
    gate_audit = _read_json_receipt(root / "gate-integrity-audit.json", None, problems, "gate_integrity_audit")
    timeline = _read_json_receipt(
        Path(str(run_payload.get("play_by_play_timeline") or root / "play-by-play" / "play-by-play-timeline.json")),
        normalize_sha256(run_payload.get("play_by_play_timeline_sha256")),
        problems,
        "am04_timeline",
    )
    am05_path = am05_receipt_path or _path_from_ref(run_payload.get("am05_lifecycle")) or (root / "am05-lifecycle" / "am05-run-receipt.json")
    am05 = _read_json_receipt(Path(am05_path), _sha_from_ref(run_payload.get("am05_lifecycle")), problems, "am05_lifecycle")

    if scorekeeper is not None and intent_binding:
        check = verify_bound_payload(scorekeeper.payload, intent_binding, label="scorekeeper")
        if check.get("status") != "PASS":
            problems.append({"gate": "AM-03", "reason": "scorekeeper_intent_binding_failed", "details": check.get("problems", [])})
    if scorekeeper is None or scorekeeper.payload.get("status") != "PASS":
        problems.append({"gate": "scorekeeper", "reason": "scorekeeper_not_passed", "status": None if scorekeeper is None else scorekeeper.payload.get("status")})
    if timeline is not None:
        try:
            validate_timeline(_as_list(timeline.payload.get("battle_events")))
        except Exception as exc:  # noqa: BLE001 - report must fail closed, not crash.
            problems.append({"gate": "AM-04", "reason": "timeline_validation_failed", "detail": f"{type(exc).__name__}: {exc}"})
    if am05 is not None and intent_binding:
        check = verify_bound_payload(am05.payload, intent_binding, label="am05_lifecycle")
        if check.get("status") != "PASS":
            problems.append({"gate": "AM-03", "reason": "am05_intent_binding_failed", "details": check.get("problems", [])})
    if am05 is None or am05.payload.get("status") != "PASS" or am05.payload.get("live") is not True:
        problems.append({"gate": "AM-05", "reason": "lifecycle_not_passed", "status": None if am05 is None else am05.payload.get("status")})

    rounds, round_refs = _read_rounds(run_payload, problems)
    required_refs.extend([
        {"gate": "run_receipt", "status": "PASS", "ref": receipt_ref(run)},
        {"gate": "scorekeeper", "status": None if scorekeeper is None else scorekeeper.payload.get("status"), "ref": receipt_ref(scorekeeper) if scorekeeper else {}},
        {"gate": "AM-04", "status": None if timeline is None else timeline.payload.get("status"), "ref": receipt_ref(timeline) if timeline else {}},
        {"gate": "AM-05", "status": None if am05 is None else am05.payload.get("status"), "ref": receipt_ref(am05) if am05 else {}},
        *round_refs,
    ])

    attempt_rows = _attempt_rows(rounds, intent_binding, problems)
    incomplete_gates = _collect_incomplete_gates(
        run_payload,
        *(round_source.payload for round_source in rounds),
        scorekeeper.payload if scorekeeper else {},
        gate_audit.payload if gate_audit else {},
        am05.payload if am05 else {},
    )
    false_green_revocations = _false_green_revocations(run_payload, gate_audit.payload if gate_audit else {})
    proof_boundary = _proof_boundary(run_payload, timeline.payload if timeline else {}, am05.payload if am05 else {})
    artifact_hashes = _artifact_hashes(run, scorekeeper, timeline, am05, gate_audit, rounds, attempt_rows)
    lane_graph = _lane_graph(battle_id, rounds, bool(timeline), bool(am05))
    timing = _timing(run_payload, rounds, timeline.payload if timeline else {}, am05.payload if am05 else {})
    scorekeeper_status = scorekeeper.payload.get("status") if scorekeeper else None
    report_status = "PASS" if not problems and scorekeeper_status == "PASS" and (timeline and timeline.payload.get("status") == "PASS") else "FAIL"
    winner = scorekeeper.payload.get("winner") if report_status == "PASS" and scorekeeper else None

    report = {
        "schema": AM06_SCHEMA,
        "status": report_status,
        "battle_id": battle_id,
        "fail_closed": True,
        "generated_from_receipts_only": True,
        "arena_maker_intent": {
            "status": intent_status.get("status"),
            "sha256": None if intent_binding is None else intent_binding.get("arena_maker_intent_sha256"),
            "verification": intent_status,
        },
        "am04_timeline": None if timeline is None else receipt_ref(timeline),
        "am05_lifecycle": None if am05 is None else receipt_ref(am05),
        "score_authority": "scorekeeper_receipt_and_judge_round_receipts_only",
        "winner": winner,
        "red_blue_wins": _authorized_wins(scorekeeper.payload if scorekeeper else {}, report_status),
        "attempt_rows": attempt_rows,
        "lane_graph": lane_graph,
        "timing": timing,
        "incomplete_gates": incomplete_gates,
        "false_green_revocations": false_green_revocations,
        "replay_commands": _replay_commands(run, attempt_rows, am05),
        "artifact_hashes": artifact_hashes,
        "proof_boundary": proof_boundary,
        "required_refs": required_refs,
        "problems": problems,
        "human_report": str(out_dir / "AM06_REPORT.md"),
        "created_at": _utc(),
    }
    _write_outputs(report, out_dir)
    return report


def validate_am06_report(report: dict[str, Any]) -> None:
    problems: list[str] = []
    if report.get("schema") != AM06_SCHEMA:
        problems.append(f"schema must be {AM06_SCHEMA}")
    if report.get("status") not in {"PASS", "FAIL"}:
        problems.append("status must be PASS or FAIL")
    if report.get("winner") is not None and report.get("status") != "PASS":
        problems.append("winner must be null unless AM-06 status is PASS")
    rows = report.get("attempt_rows")
    if not isinstance(rows, list) or not rows:
        problems.append("attempt_rows must be a non-empty list")
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if row.get("judge_authorized_win") not in {"red", "blue", None}:
            problems.append(f"attempt_rows[{index}].judge_authorized_win is invalid")
        if not row.get("judge_receipt"):
            problems.append(f"attempt_rows[{index}] missing judge_receipt")
    if not isinstance(report.get("proof_boundary"), dict):
        problems.append("proof_boundary must be an object")
    if not report.get("fail_closed"):
        problems.append("fail_closed must be true")
    if problems:
        raise ValueError("; ".join(problems))


def _write_outputs(report: dict[str, Any], out_dir: Path) -> None:
    validate_am06_report(report) if report.get("attempt_rows") else None
    machine_path = out_dir / "am06-reporting.json"
    report["path"] = str(machine_path)
    markdown_path = out_dir / "AM06_REPORT.md"
    markdown = render_am06_markdown(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    report["human_report_sha256"] = sha256_file(markdown_path)
    machine_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["sha256"] = sha256_file(machine_path)


def render_am06_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AM-06 Receipt-derived Battle report",
        "",
        f"Schema: `{AM06_MARKDOWN_SCHEMA}`.",
        f"Status: **{report.get('status')}**.",
        f"Winner: **{report.get('winner') or 'none — fail closed or draw'}**.",
        "",
        "All rows below are copied from verified receipt references. Red/Blue wins are named only when Judge/scorekeeper authority is present.",
        "",
        "## Attempt rows",
        "| round | lane | attempt | gate | exit | verdict | authorized win | receipt |",
        "|---:|---|---|---|---:|---|---|---|",
    ]
    for row in report.get("attempt_rows", []):
        ref = row.get("judge_receipt", {})
        lines.append(
            f"| {row.get('round_number')} | {row.get('lane')} | {row.get('attempt_label')} | "
            f"{row.get('gate')} | {row.get('exit_code')} | {row.get('verdict')} | "
            f"{row.get('judge_authorized_win') or ''} | `{Path(str(ref.get('path'))).name}@{str(ref.get('sha256'))[:12]}` |"
        )
    lines.extend(["", "## Incomplete gates"])
    gates = report.get("incomplete_gates") or []
    lines.extend([f"- `{json.dumps(item, sort_keys=True)}`" for item in gates] or ["- none"])
    lines.extend(["", "## False-green revocations"])
    revocations = report.get("false_green_revocations") or []
    lines.extend([f"- `{json.dumps(item, sort_keys=True)}`" for item in revocations] or ["- none"])
    lines.extend(["", "## Replay commands"])
    for item in report.get("replay_commands", []):
        lines.append(f"- `{item.get('command')}`")
    lines.extend(["", "## Proof boundary"])
    boundary = report.get("proof_boundary", {})
    lines.append("Proves:")
    lines.extend([f"- {item}" for item in boundary.get("proves", [])])
    lines.append("Does not prove / non-claims:")
    lines.extend([f"- {item}" for item in boundary.get("does_not_prove", [])])
    lines.extend(["", "## Problems"])
    lines.extend([f"- `{json.dumps(item, sort_keys=True)}`" for item in report.get("problems", [])] or ["- none"])
    return "\n".join(lines) + "\n"


def _read_json_receipt(path: Path, expected_sha256: str | None, problems: list[dict[str, Any]], label: str) -> JsonReceipt | None:
    if not path.is_file():
        problems.append({"gate": label, "reason": "missing_ref", "path": str(path)})
        return None
    actual = sha256_file(path)
    expected = normalize_sha256(expected_sha256)
    if expected and actual != expected:
        problems.append({"gate": label, "reason": "tampered_ref", "path": str(path), "expected_sha256": expected, "actual_sha256": actual})
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append({"gate": label, "reason": "invalid_json", "path": str(path), "detail": str(exc)})
        return None
    if not isinstance(payload, dict):
        problems.append({"gate": label, "reason": "json_not_object", "path": str(path)})
        return None
    return JsonReceipt(path=path, payload=payload, sha256=actual)


def _read_rounds(run_payload: dict[str, Any], problems: list[dict[str, Any]]) -> tuple[list[JsonReceipt], list[dict[str, Any]]]:
    rounds: list[JsonReceipt] = []
    refs: list[dict[str, Any]] = []
    for index, raw in enumerate(_as_list(run_payload.get("rounds"))):
        if not isinstance(raw, dict):
            problems.append({"gate": "round_receipts", "reason": "malformed_round_ref", "index": index})
            continue
        path = Path(str(raw.get("path") or ""))
        source = _read_json_receipt(path, normalize_sha256(raw.get("sha256")), problems, f"round_{index + 1}")
        if source:
            rounds.append(source)
            refs.append({"gate": f"round_{source.payload.get('round_number')}", "status": source.payload.get("status"), "ref": receipt_ref(source)})
    return rounds, refs


def _attempt_rows(rounds: list[JsonReceipt], intent_binding: dict[str, Any] | None, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for round_source in rounds:
        round_payload = round_source.payload
        if intent_binding:
            check = verify_bound_payload(round_payload, intent_binding, label=f"round-{round_payload.get('round_number')}")
            if check.get("status") != "PASS":
                problems.append({"gate": "AM-03", "reason": "round_intent_binding_failed", "round": round_payload.get("round_number"), "details": check.get("problems", [])})
        for index, attempt_ref in enumerate(_as_list(round_payload.get("docker_attempts"))):
            if not isinstance(attempt_ref, dict):
                continue
            attempt_path = Path(str(attempt_ref.get("path") or ""))
            attempt_source = _read_json_receipt(attempt_path, normalize_sha256(attempt_ref.get("sha256")), problems, f"round_{round_payload.get('round_number')}_attempt_{index}")
            attempt = attempt_source.payload if attempt_source else attempt_ref
            if intent_binding and attempt_source:
                check = verify_bound_payload(attempt, intent_binding, label=f"attempt-{attempt.get('label')}")
                if check.get("status") != "PASS":
                    problems.append({"gate": "AM-03", "reason": "attempt_intent_binding_failed", "round": round_payload.get("round_number"), "label": attempt.get("label"), "details": check.get("problems", [])})
            label = str(attempt.get("label") or f"attempt-{index}")
            lane = "blue" if label == "patched" else "red" if label == "original" else "lineage"
            rows.append({
                "row_id": f"round-{int(round_payload.get('round_number') or 0):02d}:{label}",
                "round_number": round_payload.get("round_number"),
                "lane": lane,
                "vulnerability": round_payload.get("vulnerability"),
                "attempt_label": label,
                "objective": round_payload.get("blue_plan") if lane == "blue" else round_payload.get("red_goal"),
                "status": attempt.get("status") or attempt.get("process_status"),
                "gate": attempt.get("gate"),
                "exit_code": attempt.get("exit_code"),
                "semantic_exit_code": attempt.get("semantic_exit_code"),
                "verdict": round_payload.get("verdict"),
                "judge_authorized_win": _row_win(lane, round_payload),
                "started_at": attempt.get("started_at"),
                "completed_at": attempt.get("completed_at"),
                "judge_receipt": receipt_ref(attempt_source) if attempt_source else {"path": str(attempt_path), "sha256": attempt_ref.get("sha256")},
                "artifact_hashes": {
                    "stdout_sha256": attempt.get("stdout_sha256"),
                    "stderr_sha256": attempt.get("stderr_sha256"),
                    **(round_payload.get("artifacts") if isinstance(round_payload.get("artifacts"), dict) else {}),
                },
            })
    return rows


def _row_win(lane: str, round_payload: dict[str, Any]) -> str | None:
    if round_payload.get("status") != "PASS":
        return None
    if lane == "red" and round_payload.get("verdict") in {"RED_SUCCESS", "FAKE_DEFENSE"} and int(round_payload.get("red_score") or 0) > 0:
        return "red"
    if lane == "blue" and round_payload.get("verdict") == "BLUE_SUCCESS" and int(round_payload.get("blue_score") or 0) > 0:
        return "blue"
    return None


def _collect_incomplete_gates(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    for payload in payloads:
        raw = payload.get("incomplete_gates")
        if isinstance(raw, list):
            gates.extend(item for item in raw if isinstance(item, dict))
        if payload.get("status") in {"FAIL", "BLOCKED", "GATES_INCOMPLETE", "CHAIN_MISMATCH"}:
            gates.append({"gate": str(payload.get("schema") or "unknown"), "status": payload.get("status"), "problems": payload.get("problems") or payload.get("blockers") or []})
    return gates


def _false_green_revocations(run_payload: dict[str, Any], gate_audit: dict[str, Any]) -> list[dict[str, Any]]:
    revocations: list[dict[str, Any]] = []
    count = int(gate_audit.get("false_green_parent_count") or 0)
    if count:
        revocations.append({"source": "gate_integrity_audit", "false_green_parent_count": count, "action": "parent PASS revoked"})
    if run_payload.get("status") == "PASS" and gate_audit.get("status") == "FAIL":
        revocations.append({"source": "gate_integrity_audit", "action": "run reported PASS but audit failed"})
    return revocations


def _proof_boundary(run_payload: dict[str, Any], timeline: dict[str, Any], am05: dict[str, Any]) -> dict[str, list[str]]:
    claims = run_payload.get("claims") if isinstance(run_payload.get("claims"), dict) else {}
    proves = list(claims.get("proves") or [])
    does_not = list(claims.get("does_not_prove") or [])
    if timeline:
        proves.append("AM-04 play-by-play ordering and evidence hashes validate for cited events.")
        for event in _as_list(timeline.get("battle_events")):
            if isinstance(event, dict):
                does_not.extend(str(item) for item in event.get("non_claims") or [])
    if am05:
        proves.append("AM-05 lifecycle receipt is included with live Docker status when PASS.")
        qemu = am05.get("qemu_lifecycle") if isinstance(am05.get("qemu_lifecycle"), dict) else {}
        if qemu.get("status") == "BLOCKED":
            does_not.append("QEMU lifecycle is not claimed when host tools or base image are blocked.")
    return {"proves": sorted(set(proves)), "does_not_prove": sorted(set(does_not))}


def _artifact_hashes(*items: Any) -> list[dict[str, Any]]:
    hashes: list[dict[str, Any]] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, JsonReceipt):
            hashes.append({"path": str(item.path), "sha256": item.sha256, "schema": item.payload.get("schema")})
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, JsonReceipt):
                    hashes.append({"path": str(sub.path), "sha256": sub.sha256, "schema": sub.payload.get("schema")})
                elif isinstance(sub, dict) and isinstance(sub.get("artifact_hashes"), dict):
                    hashes.append({"row_id": sub.get("row_id"), **sub["artifact_hashes"]})
    seen = set()
    unique = []
    for entry in hashes:
        key = json.dumps(entry, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _lane_graph(battle_id: str, rounds: list[JsonReceipt], has_timeline: bool, has_am05: bool) -> dict[str, Any]:
    nodes = [
        {"id": "am03_intent", "kind": "intent"},
        {"id": "authorization", "kind": "gate"},
        {"id": "scorekeeper", "kind": "score_authority"},
    ]
    edges = [{"from": "am03_intent", "to": "authorization", "kind": "binds"}]
    for round_source in rounds:
        number = round_source.payload.get("round_number")
        rid = f"round-{number}"
        nodes.append({"id": rid, "kind": "round", "status": round_source.payload.get("status")})
        edges.append({"from": "authorization", "to": rid, "kind": "opens"})
        for attempt in _as_list(round_source.payload.get("docker_attempts")):
            if isinstance(attempt, dict):
                aid = f"{rid}:{attempt.get('label')}"
                nodes.append({"id": aid, "kind": "attempt", "gate": attempt.get("gate")})
                edges.append({"from": rid, "to": aid, "kind": "judge_attempt"})
                edges.append({"from": aid, "to": "scorekeeper", "kind": "score_input"})
    if has_timeline:
        nodes.append({"id": "am04_timeline", "kind": "timeline"})
        edges.append({"from": "scorekeeper", "to": "am04_timeline", "kind": "projects"})
    if has_am05:
        nodes.append({"id": "am05_lifecycle", "kind": "lifecycle"})
        edges.append({"from": "am03_intent", "to": "am05_lifecycle", "kind": "binds"})
    return {"battle_id": battle_id, "nodes": nodes, "edges": edges}


def _timing(run_payload: dict[str, Any], rounds: list[JsonReceipt], timeline: dict[str, Any], am05: dict[str, Any]) -> dict[str, Any]:
    events = timeline.get("battle_events") if isinstance(timeline.get("battle_events"), list) else []
    timestamps = [event.get("occurred_at") for event in events if isinstance(event, dict) and isinstance(event.get("occurred_at"), str)]
    return {
        "created_at": run_payload.get("created_at"),
        "first_event_at": min(timestamps) if timestamps else None,
        "last_event_at": max(timestamps) if timestamps else None,
        "rounds": [
            {"round_number": r.payload.get("round_number"), "created_at": r.payload.get("created_at"), "attempts": [
                {"label": a.get("label"), "started_at": a.get("started_at"), "completed_at": a.get("completed_at")}
                for a in _as_list(r.payload.get("docker_attempts")) if isinstance(a, dict)
            ]}
            for r in rounds
        ],
        "am05_created_at": am05.get("created_at"),
    }


def _replay_commands(run: JsonReceipt, rows: list[dict[str, Any]], am05: JsonReceipt | None) -> list[dict[str, Any]]:
    commands = [{
        "label": "full_am06_local_docker_path",
        "command": "python skills/battle/scripts/run_am06_reporting_eval.py --out package/run",
        "provenance_path": str(run.path.parent),
    }]
    for row in rows:
        work_dir = Path(str(row.get("judge_receipt", {}).get("path", ""))).parent
        if work_dir.name in {"original", "patched", "preflight"} or (work_dir / "judge.py").is_file():
            commands.append({
                "label": str(row.get("row_id")),
                "command": f"docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v package/judge-work/{shlex.quote(work_dir.name)}:/work -w /work python:3.12-slim python judge.py",
                "provenance_path": str(work_dir),
            })
    if am05 is not None:
        commands.append({"label": "am05_lifecycle_receipt", "command": "python skills/battle/scripts/run_am06_reporting_eval.py --out package/run --report-out package/run/am06-reporting", "provenance_path": str(am05.path.parent.parent / "am06-reporting")})
    return commands


def _authorized_wins(scorekeeper: dict[str, Any], report_status: str) -> dict[str, Any]:
    if report_status != "PASS" or scorekeeper.get("status") != "PASS":
        return {"red_round_wins": None, "blue_round_wins": None, "winner": None, "authority": None}
    return {
        "red_round_wins": scorekeeper.get("red_round_wins"),
        "blue_round_wins": scorekeeper.get("blue_round_wins"),
        "winner": scorekeeper.get("winner"),
        "authority": "scorekeeper_receipt",
    }


def _intent_status(intent_binding: dict[str, Any] | None) -> dict[str, Any]:
    if not intent_binding:
        return {"schema": "battle.am06.intent_check.v1", "status": "FAIL", "problems": ["missing_arena_maker_intent_binding"]}
    return verify_frozen_intent(intent_binding)


def _failure_report(out_dir: Path, problems: list[dict[str, Any]], required_refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": AM06_SCHEMA,
        "status": "FAIL",
        "battle_id": out_dir.parent.name,
        "fail_closed": True,
        "generated_from_receipts_only": True,
        "winner": None,
        "red_blue_wins": {"red_round_wins": None, "blue_round_wins": None, "winner": None, "authority": None},
        "attempt_rows": [],
        "lane_graph": {"nodes": [], "edges": []},
        "timing": {},
        "incomplete_gates": [],
        "false_green_revocations": [],
        "replay_commands": [],
        "artifact_hashes": [],
        "proof_boundary": {"proves": [], "does_not_prove": ["No Red/Blue outcome is claimed because required receipts did not verify."]},
        "required_refs": required_refs,
        "problems": problems,
        "human_report": str(out_dir / "AM06_REPORT.md"),
        "created_at": _utc(),
    }


def _nested_sha(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return normalize_sha256(value.get("sha256")) if isinstance(value, dict) else None


def _path_from_ref(value: Any) -> Path | None:
    return Path(str(value.get("path"))) if isinstance(value, dict) and value.get("path") else None


def _sha_from_ref(value: Any) -> str | None:
    return normalize_sha256(value.get("sha256")) if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
