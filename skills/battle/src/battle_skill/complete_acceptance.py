"""Integrated Complete Battle acceptance and release gate."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COMPLETE_ACCEPTANCE_SCHEMA = "battle.complete_acceptance_receipt.v1"
REQUIREMENTS_SCHEMA = "battle.complete_acceptance_requirements.v1"
STATUSES = {"IMPLEMENTED_AND_PROVEN", "IMPLEMENTED_UNPROVEN", "PARTIAL", "MISSING"}


def _expected_requirements() -> dict[str, dict[str, Any]]:
    requirements = _read_json(default_requirements_path()) or {}
    return {str(row.get("id")): row for row in _as_list(requirements.get("requirements"))}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_requirements_path() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "complete_acceptance_requirements.round1.json"


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON receipt is not an object: {path}")
    return data


def _ref(path: Path | None, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if path is None or payload is None or not Path(path).is_file():
        return None
    return {"path": str(path), "sha256": sha256_file(Path(path)), "schema": payload.get("schema"), "status": payload.get("status")}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def build_complete_acceptance_receipt(
    *,
    requirements_path: Path | None = None,
    out_dir: Path,
    am06_report_path: Path,
    run_receipt_path: Path | None = None,
    am05_receipt_path: Path | None = None,
    am07_receipt_path: Path | None = None,
    dependency_receipts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Build one source-bound acceptance receipt and fail-closed release gate."""

    requirements_path = requirements_path or default_requirements_path()
    requirements = _read_json(requirements_path)
    if requirements is None or requirements.get("schema") != REQUIREMENTS_SCHEMA:
        raise ValueError(f"requirements must be {REQUIREMENTS_SCHEMA}: {requirements_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    am06 = _read_json(am06_report_path)
    run = _read_json(run_receipt_path) if run_receipt_path else None
    am05 = _read_json(am05_receipt_path) if am05_receipt_path else None
    am07 = _read_json(am07_receipt_path) if am07_receipt_path else None
    deps = {name: _read_json(path) for name, path in (dependency_receipts or {}).items()}

    refs = {
        "requirements": _ref(requirements_path, requirements),
        "am06_report": _ref(am06_report_path, am06),
        "run_receipt": _ref(run_receipt_path, run),
        "am05_lifecycle": _ref(am05_receipt_path, am05),
        "am07_learning": _ref(am07_receipt_path, am07),
    }
    refs.update({name: _ref(path, deps.get(name)) for name, path in (dependency_receipts or {}).items()})

    requirement_rows = _requirement_rows(requirements, am06, run, am05, am07, deps)
    blockers = _blockers(am06, run, am05, am07, requirement_rows)
    report_validity = _report_validity(am06)
    campaign_completion = _campaign_completion(am06, run, blockers)
    release_eligibility = _release_eligibility(requirement_rows, blockers)
    aggregate_winner = {
        "authorized": campaign_completion["status"] == "COMPLETE" and release_eligibility["status"] == "ELIGIBLE",
        "winner": (am06 or {}).get("winner") if campaign_completion["status"] == "COMPLETE" and release_eligibility["status"] == "ELIGIBLE" else None,
        "reason": "withheld unless report, campaign, all requirements, provider/Tau, QEMU, replay, cleanup, and learning gates pass",
    }
    portable_replay = _portable_replay(am06, out_dir)
    non_claims = list(dict.fromkeys(requirements.get("exact_non_claims", []) + _as_list(((am06 or {}).get("proof_boundary") or {}).get("does_not_prove"))))

    receipt = {
        "schema": COMPLETE_ACCEPTANCE_SCHEMA,
        "status": "PASS" if report_validity["status"] == "PASS" else "FAIL",
        "release_authorized": release_eligibility["status"] == "ELIGIBLE",
        "aggregate_winner_authorized": aggregate_winner["authorized"],
        "requirement_set_id": requirements.get("requirement_set_id"),
        "requirements_ref": refs["requirements"],
        "source_refs": {key: value for key, value in refs.items() if value is not None},
        "requirement_crosswalk": _requirement_crosswalk(requirements),
        "report_validity": report_validity,
        "campaign_completion": campaign_completion,
        "release_eligibility": release_eligibility,
        "aggregate_winner": aggregate_winner,
        "requirements_ledger": requirement_rows,
        "attempt_rows": _as_list((am06 or {}).get("attempt_rows")),
        "lane_graph": (am06 or {}).get("lane_graph") or {"nodes": [], "edges": []},
        "timing": (am06 or {}).get("timing") or {},
        "blockers": blockers,
        "portable_replay": portable_replay,
        "exact_non_claims": non_claims,
        "created_at": _utc(),
    }
    path = out_dir / "complete-acceptance-receipt.json"
    receipt["path"] = str(path)
    atomic_write_json(path, receipt)
    receipt["sha256"] = sha256_file(path)
    atomic_write_json(path, receipt)
    return receipt


def validate_complete_acceptance_receipt(receipt: dict[str, Any]) -> None:
    problems: list[str] = []
    if receipt.get("schema") != COMPLETE_ACCEPTANCE_SCHEMA:
        problems.append(f"schema must be {COMPLETE_ACCEPTANCE_SCHEMA}")
    if receipt.get("release_authorized") and receipt.get("release_eligibility", {}).get("status") != "ELIGIBLE":
        problems.append("release_authorized requires release_eligibility ELIGIBLE")
    if receipt.get("aggregate_winner_authorized") and not receipt.get("release_authorized"):
        problems.append("aggregate_winner_authorized requires release_authorized")
    rows = receipt.get("requirements_ledger")
    row_list = rows if isinstance(rows, list) else []
    expected = _expected_requirements()
    row_ids = [row.get("id") for row in row_list if isinstance(row, dict)]
    if set(row_ids) != set(expected) or len(row_ids) != len(expected):
        problems.append("requirements_ledger must cover each frozen round-1 requirement exactly once")
    for row in row_list:
        rid = row.get("id")
        if row.get("status") not in STATUSES:
            problems.append(f"invalid requirement status for {rid}")
        expected_row = expected.get(str(rid))
        if expected_row and (
            row.get("title") != expected_row.get("title")
            or row.get("profiles") != expected_row.get("profiles")
            or row.get("release_required") != bool(expected_row.get("release_required", True))
        ):
            problems.append(f"requirement row does not match frozen ledger for {rid}")
    if receipt.get("release_authorized"):
        _validate_release_claim(receipt, row_list, problems)
    if problems:
        raise ValueError("; ".join(problems))


def _validate_release_claim(receipt: dict[str, Any], rows: list[dict[str, Any]], problems: list[str]) -> None:
    if receipt.get("status") != "PASS":
        problems.append("release requires complete acceptance receipt status PASS")
    if receipt.get("report_validity", {}).get("status") != "PASS":
        problems.append("release requires report_validity PASS")
    if receipt.get("campaign_completion", {}).get("status") != "COMPLETE":
        problems.append("release requires campaign_completion COMPLETE")
    eligibility = receipt.get("release_eligibility", {})
    if eligibility.get("status") != "ELIGIBLE" or eligibility.get("missing_or_unproven_requirements"):
        problems.append("release requires release_eligibility ELIGIBLE with no missing requirements")
    if receipt.get("blockers"):
        problems.append("release requires no blockers")
    if any(row.get("status") != "IMPLEMENTED_AND_PROVEN" for row in rows):
        problems.append("release requires every requirement IMPLEMENTED_AND_PROVEN")
    if not _as_list(receipt.get("attempt_rows")):
        problems.append("release requires retained attempt_rows")
    portable = receipt.get("portable_replay") or {}
    if portable.get("status") != "PASS" or _as_list(portable.get("nonportable_references")):
        problems.append("release requires portable_replay PASS with no nonportable references")
    aggregate = receipt.get("aggregate_winner") or {}
    if receipt.get("aggregate_winner_authorized") is not True or aggregate.get("authorized") is not True or aggregate.get("winner") is None:
        problems.append("release requires an authorized aggregate winner")
    _validate_release_source_refs(receipt, problems)


def _validate_release_source_refs(receipt: dict[str, Any], problems: list[str]) -> None:
    refs = {"requirements_ref": receipt.get("requirements_ref")}
    refs.update((receipt.get("source_refs") or {}) if isinstance(receipt.get("source_refs"), dict) else {})
    for name, ref in refs.items():
        if not isinstance(ref, dict):
            problems.append(f"release source ref missing for {name}")
            continue
        path = ref.get("path")
        expected_sha = ref.get("sha256")
        if not path or not expected_sha:
            problems.append(f"release source ref incomplete for {name}")
            continue
        source = Path(path)
        if not source.is_file():
            problems.append(f"release source ref not readable for {name}")
            continue
        if sha256_file(source) != expected_sha:
            problems.append(f"release source ref sha mismatch for {name}")


def release_gate_from_complete_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    validate_complete_acceptance_receipt(receipt)
    return {
        "schema": "battle.complete_acceptance.release_gate.v1",
        "status": "PASS" if receipt.get("release_authorized") else "BLOCKED",
        "release_authorized": bool(receipt.get("release_authorized")),
        "aggregate_winner_authorized": bool(receipt.get("aggregate_winner_authorized")),
        "blockers": receipt.get("blockers", []),
        "receipt_sha256": receipt.get("sha256"),
    }


def _requirement_crosswalk(requirements: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for req in _as_list(requirements.get("requirements")):
        rows.append({
            "requirement_id": req.get("id"),
            "round1_ticket_id": str(req.get("round1_ticket_id") or req.get("ticket_id") or req.get("id", "")).replace("AM-", "").zfill(2),
            "legacy_am_id": req.get("legacy_am_id") or req.get("id"),
            "title": req.get("title"),
        })
    return rows


def _requirement_rows(requirements: dict[str, Any], am06: dict[str, Any] | None, run: dict[str, Any] | None, am05: dict[str, Any] | None, am07: dict[str, Any] | None, deps: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for req in _as_list(requirements.get("requirements")):
        rid = req.get("id")
        status = "MISSING"
        evidence: list[str] = []
        blockers: list[str] = []
        if rid == "AM-01":
            q = deps.get("qualification_bundle")
            status, evidence, blockers = _status_from(q, "qualification_bundle")
        elif rid == "AM-02":
            intent_ok = ((am06 or {}).get("arena_maker_intent") or {}).get("status") == "PASS"
            tau = deps.get("tau_dispatch")
            status = "IMPLEMENTED_AND_PROVEN" if intent_ok and _is_pass(tau) else "PARTIAL" if intent_ok else "MISSING"
            evidence = ["AM-06 frozen intent PASS"] if intent_ok else []
            blockers = [] if _is_pass(tau) else ["tau_dispatch_not_proven"]
        elif rid == "AM-03":
            p = deps.get("provider_compile_replay")
            status, evidence, blockers = _status_from(p, "provider_compile_replay")
            if status == "MISSING":
                blockers.append("provider_compile_replay_not_proven")
        elif rid in {"AM-04", "AM-07"}:
            timeline_ok = bool((am06 or {}).get("am04_timeline"))
            rows_ok = bool(_as_list((am06 or {}).get("attempt_rows")))
            status = "IMPLEMENTED_AND_PROVEN" if timeline_ok and rows_ok else "PARTIAL" if timeline_ok or rows_ok else "MISSING"
            evidence = ["AM-06 timeline and attempt rows present"] if timeline_ok and rows_ok else []
            blockers = [] if status == "IMPLEMENTED_AND_PROVEN" else ["play_by_play_or_attempt_rows_incomplete"]
        elif rid == "AM-05":
            score_ok = ((am06 or {}).get("score_authority") == "scorekeeper_receipt_and_judge_round_receipts_only") and bool((am06 or {}).get("attempt_rows"))
            status = "IMPLEMENTED_AND_PROVEN" if score_ok else "MISSING"
            evidence = ["AM-06 score authority is scorekeeper/Judge only"] if score_ok else []
            blockers = [] if score_ok else ["judge_origin_scoring_not_proven"]
        elif rid == "AM-06":
            docker_ok = (am05 or {}).get("status") == "PASS" or (((am05 or {}).get("docker_lifecycle") or {}).get("status") == "PASS")
            qemu_ok = (((am05 or {}).get("qemu_lifecycle") or {}).get("status") == "PASS")
            status = "IMPLEMENTED_AND_PROVEN" if docker_ok and qemu_ok else "PARTIAL" if docker_ok else "MISSING"
            evidence = ["Docker lifecycle PASS"] if docker_ok else []
            blockers = [] if qemu_ok else ["qemu_lifecycle_not_passed"]
        elif rid == "AM-08":
            status = "IMPLEMENTED_AND_PROVEN" if _is_pass(am07) else "PARTIAL" if am07 else "MISSING"
            evidence = ["AM-08 learning receipt PASS"] if _is_pass(am07) else []
            blockers = [] if _is_pass(am07) else ["learning_readback_or_consumption_not_passed"]
        elif rid == "AM-09":
            b = deps.get("historical_backfill") or deps.get("gate_integrity_audit")
            status, evidence, blockers = _status_from(b, "historical_backfill")
        elif rid == "AM-10":
            replay_ok = bool(am06 and _as_list((am06 or {}).get("attempt_rows")))
            status = "IMPLEMENTED_AND_PROVEN" if replay_ok else "PARTIAL"
            evidence = ["complete acceptance receipt generated from AM-06 attempt rows and requirement crosswalk"] if status == "IMPLEMENTED_AND_PROVEN" else []
            blockers = [] if status == "IMPLEMENTED_AND_PROVEN" else ["integrated_report_missing_attempt_rows"]
        rows.append({
            "id": rid,
            "title": req.get("title"),
            "profiles": req.get("profiles", []),
            "release_required": bool(req.get("release_required", True)),
            "status": status,
            "evidence": evidence,
            "blockers": blockers,
        })
    return rows


def _status_from(payload: dict[str, Any] | None, label: str) -> tuple[str, list[str], list[str]]:
    if payload is None:
        return "MISSING", [], [f"{label}_missing"]
    if _is_pass(payload):
        return "IMPLEMENTED_AND_PROVEN", [f"{label} status PASS"], []
    return "PARTIAL", [f"{label} status {payload.get('status')}"], [f"{label}_not_pass"]


def _blockers(am06: dict[str, Any] | None, run: dict[str, Any] | None, am05: dict[str, Any] | None, am07: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if (am06 or {}).get("status") != "PASS":
        blockers.append({"code": "am06_report_status_not_pass", "status": (am06 or {}).get("status")})
    if (run or {}).get("status") not in {"PASS", None}:
        blockers.append({"code": "campaign_gates_incomplete", "status": (run or {}).get("status"), "blocked_rounds": (run or {}).get("blocked_rounds", [])})
    qemu = ((am05 or {}).get("qemu_lifecycle") or {}) if isinstance((am05 or {}).get("qemu_lifecycle"), dict) else {}
    if qemu.get("status") != "PASS":
        blockers.append({"code": "qemu_lifecycle_not_passed", "status": qemu.get("status"), "blocker": qemu.get("blocker")})
    if not _is_pass(am07):
        blockers.append({"code": "learning_incomplete", "status": (am07 or {}).get("status")})
    for row in rows:
        if row.get("release_required") and row.get("status") != "IMPLEMENTED_AND_PROVEN":
            blockers.append({"code": "requirement_not_proven", "requirement_id": row.get("id"), "status": row.get("status"), "blockers": row.get("blockers", [])})
    return blockers


def _report_validity(am06: dict[str, Any] | None) -> dict[str, Any]:
    problems = []
    if am06 is None:
        problems.append("am06_report_missing")
    elif am06.get("schema") != "battle.am06.reporting.v1":
        problems.append("am06_schema_invalid")
    elif am06.get("generated_from_receipts_only") is not True or am06.get("fail_closed") is not True:
        problems.append("am06_not_receipt_only_fail_closed")
    elif am06.get("status") == "FAIL" and am06.get("winner") is not None:
        problems.append("failed_report_promoted_winner")
    elif not _as_list(am06.get("attempt_rows")):
        problems.append("attempt_rows_missing")
    return {"status": "PASS" if not problems else "FAIL", "valid_failed_report_allowed": True, "problems": problems}


def _campaign_completion(am06: dict[str, Any] | None, run: dict[str, Any] | None, blockers: list[dict[str, Any]]) -> dict[str, Any]:
    complete = (am06 or {}).get("status") == "PASS" and (run or {}).get("status") == "PASS" and not (am06 or {}).get("incomplete_gates")
    return {"status": "COMPLETE" if complete else "INCOMPLETE", "winner": (am06 or {}).get("winner") if complete else None, "blocker_count": len(blockers)}


def _release_eligibility(rows: list[dict[str, Any]], blockers: list[dict[str, Any]]) -> dict[str, Any]:
    missing = [row["id"] for row in rows if row.get("release_required") and row.get("status") != "IMPLEMENTED_AND_PROVEN"]
    return {"status": "ELIGIBLE" if not missing and not blockers else "BLOCKED", "missing_or_unproven_requirements": missing, "blocker_count": len(blockers)}


def _portable_replay(am06: dict[str, Any] | None, out_dir: Path) -> dict[str, Any]:
    source_commands = _as_list((am06 or {}).get("replay_commands"))
    commands = list(source_commands) + [
        {"label": "complete_acceptance_readback", "command": "python -m json.tool package/complete-acceptance/complete-acceptance-receipt.json >/dev/null", "provenance_path": str(out_dir / "complete-acceptance-receipt.json")}
    ]
    nonportable = [item for item in commands if "/tmp/" in str(item.get("command")) or str(item.get("command", "")).startswith("/")]
    return {
        "status": "PASS" if source_commands and not nonportable else "FAIL",
        "commands": commands,
        "nonportable_references": nonportable,
        "readback_required": True,
        "path_policy": "command fields must be package-relative; provenance_path may preserve original absolute labels",
    }


def _is_pass(payload: dict[str, Any] | None) -> bool:
    return bool(payload) and (payload.get("status") == "PASS" or payload.get("passed") is True)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
