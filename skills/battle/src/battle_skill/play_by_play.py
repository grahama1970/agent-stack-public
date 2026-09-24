"""Receipt-backed play-by-play projection for Battle.

AM-04 rule: commentary is a projection of cited receipts, not a narrator with a
license to infer motive, causality, success, or impact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PLAY_BY_PLAY_EVENT_SCHEMA = "battle.play_by_play_event.v1"
TIMELINE_SCHEMA = "battle.play_by_play_timeline.v1"

ALLOWED_EVENT_TYPES = {
    "arena_open",
    "authorization_checked",
    "round_started",
    "scan_started",
    "scan_result_recorded",
    "research_recorded",
    "specimen_materialized",
    "patch_materialized",
    "contact_observed",
    "kill_observed",
    "crash_observed",
    "containment_observed",
    "regression_observed",
    "judge_called",
    "judge_verdict_recorded",
    "score_delta_recorded",
    "win_recorded",
    "spawn_preflight_recorded",
    "terminal_event_recorded",
    "round_closed",
    "scorekeeper_final",
    "battle_closed",
}
REQUIRED_EVENT_TYPES = tuple(sorted(ALLOWED_EVENT_TYPES))
EVENT_SOURCE_MAPPING = {
    "arena_open": "arena contract or run receipt target identity",
    "authorization_checked": "authorization validation status",
    "round_started": "round receipt number and vulnerability",
    "scan_started": "team handoff or round goal/plan field",
    "scan_result_recorded": "round receipt scan array",
    "research_recorded": "round receipt attempt/research array",
    "specimen_materialized": "round artifact hash for original specimen",
    "patch_materialized": "round artifact hash for patched specimen",
    "contact_observed": "Docker/Judge attempt receipt exit and gate",
    "kill_observed": "process status or explicit controlled observation",
    "crash_observed": "infra error/process status or explicit controlled observation",
    "containment_observed": "patched Judge receipt plus outcome semantics",
    "regression_observed": "round verdict and outcome semantics",
    "judge_called": "Judge attempt receipt",
    "judge_verdict_recorded": "round receipt verdict and finding fields",
    "score_delta_recorded": "Judge-controlled round score fields",
    "win_recorded": "Judge or scorekeeper winner/verdict fields",
    "spawn_preflight_recorded": "spawn receipt or round verdict not-applicable evidence",
    "terminal_event_recorded": "round/run status and incomplete gates",
    "round_closed": "round receipt status and verdict",
    "scorekeeper_final": "scorekeeper totals and winner fields",
    "battle_closed": "run receipt terminal status",
}
ALLOWED_ACTORS = {"arena", "authorization", "red", "blue", "judge", "scorekeeper", "lineage", "system"}
ALLOWED_CERTAINTY = {"observed_receipt", "verified_hash", "score_authority", "non_claim"}
ALLOWED_STATEMENT_KINDS = {"observation", "team_claim", "judge_confirmation", "scorekeeper_confirmation", "inference", "non_claim"}
ALLOWED_SCORE_AUTHORITIES = {"none", "judge_receipt", "scorekeeper_receipt"}
NON_CLAIMS = [
    "No motive is inferred.",
    "No causality beyond receipt order is inferred.",
    "No success or impact is claimed without Judge/scorekeeper authority.",
]


class PlayByPlayError(ValueError):
    """Raised when a play-by-play event is not receipt-grounded."""


@dataclass(frozen=True)
class ReceiptSource:
    path: Path
    payload: dict[str, Any]
    sha256: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_jsonl(path: Path) -> str:
    return sha256_file(path)


def read_receipt(path: Path, *, expected_sha256: str | None = None) -> ReceiptSource:
    if not path.is_file():
        raise PlayByPlayError(f"missing evidence receipt: {path}")
    actual = sha256_file(path)
    if expected_sha256 and _normal_sha(expected_sha256) != actual:
        raise PlayByPlayError(f"broken evidence ref: {path} expected {expected_sha256} actual {actual}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlayByPlayError(f"receipt is not a JSON object: {path}")
    return ReceiptSource(path=path, payload=payload, sha256=actual)


def evidence_ref(source: ReceiptSource, pointer: str = "") -> dict[str, str]:
    return {
        "path": str(source.path),
        "sha256": source.sha256,
        "schema": str(source.payload.get("schema") or "unknown"),
        "json_pointer": pointer,
    }


def fact_from(source: ReceiptSource, pointer: str, *, source_ref: int = 0) -> dict[str, Any]:
    value = _resolve_pointer(source.payload, pointer)
    return {
        "source_ref": source_ref,
        "field": pointer,
        "value": value,
        "statement": f"{pointer or '/'} = {json.dumps(value, sort_keys=True, separators=(',', ':'))}",
    }


def event(
    *,
    battle_id: str,
    sequence: int,
    event_type: str,
    actor: str,
    summary: str,
    occurred_at: str,
    evidence_refs: list[dict[str, str]],
    round_number: int | None = None,
    period: str | None = None,
    score_delta: dict[str, Any] | None = None,
    score_after: dict[str, int] | None = None,
    certainty: str = "verified_hash",
    statement_kind: str = "observation",
    source_fields: list[str] | None = None,
    facts: list[dict[str, Any]] | None = None,
    causal_refs: list[str] | None = None,
    claims: list[str] | None = None,
    non_claims: list[str] | None = None,
) -> dict[str, Any]:
    if facts is None:
        facts = _facts_from_refs(evidence_refs)
    if source_fields is None:
        source_fields = [str(item.get("field")) for item in facts]
    payload = {
        "schema": PLAY_BY_PLAY_EVENT_SCHEMA,
        "event_id": f"{battle_id}:{sequence:04d}:{event_type}",
        "battle_id": battle_id,
        "round_number": round_number,
        "sequence": sequence,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "actor": actor,
        "period": period or (f"round-{round_number}" if round_number is not None else "battle"),
        "summary": summary,
        "certainty": certainty,
        "statement_kind": statement_kind,
        "evidence_refs": evidence_refs,
        "source_fields": source_fields,
        "facts": facts,
        "causal_refs": causal_refs or [],
        "score_delta": score_delta or {"red": 0, "blue": 0, "authority": "none"},
        "score_after": score_after or {"red": 0, "blue": 0},
        "claims": claims if claims is not None else [str(item.get("statement")) for item in facts],
        "non_claims": non_claims or list(NON_CLAIMS),
    }
    validate_event(payload)
    return payload


def validate_event(payload: dict[str, Any]) -> None:
    problems: list[str] = []
    required = {
        "schema",
        "event_id",
        "battle_id",
        "round_number",
        "sequence",
        "occurred_at",
        "event_type",
        "actor",
        "period",
        "summary",
        "certainty",
        "statement_kind",
        "evidence_refs",
        "source_fields",
        "facts",
        "causal_refs",
        "score_delta",
        "score_after",
        "non_claims",
    }
    missing = sorted(required - set(payload))
    if missing:
        problems.append(f"missing required fields: {missing}")
    if payload.get("schema") != PLAY_BY_PLAY_EVENT_SCHEMA:
        problems.append(f"schema must be {PLAY_BY_PLAY_EVENT_SCHEMA}")
    if payload.get("event_type") not in ALLOWED_EVENT_TYPES:
        problems.append(f"event_type not allowed: {payload.get('event_type')!r}")
    if payload.get("actor") not in ALLOWED_ACTORS:
        problems.append(f"actor not allowed: {payload.get('actor')!r}")
    if payload.get("certainty") not in ALLOWED_CERTAINTY:
        problems.append(f"certainty not allowed: {payload.get('certainty')!r}")
    if payload.get("statement_kind") not in ALLOWED_STATEMENT_KINDS:
        problems.append(f"statement_kind not allowed: {payload.get('statement_kind')!r}")
    if not isinstance(payload.get("sequence"), int) or isinstance(payload.get("sequence"), bool) or payload.get("sequence", 0) < 1:
        problems.append("sequence must be a positive integer")
    expected_event_id = f"{payload.get('battle_id')}:{payload.get('sequence', 0):04d}:{payload.get('event_type')}" if isinstance(payload.get("sequence"), int) else None
    if expected_event_id and payload.get("event_id") != expected_event_id:
        problems.append("event_id must be stable battle_id:sequence:event_type")
    if not _parse_time(str(payload.get("occurred_at") or "")):
        problems.append("occurred_at must be an ISO timestamp")
    if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
        problems.append("summary must be a non-empty string")

    sources: list[ReceiptSource] = []
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        problems.append("evidence_refs must be nonempty")
    else:
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                problems.append(f"evidence_refs[{index}] must be an object")
                continue
            path = Path(str(ref.get("path") or ""))
            expected = str(ref.get("sha256") or "")
            if not expected:
                problems.append(f"evidence_refs[{index}] missing sha256")
                continue
            try:
                source = read_receipt(path, expected_sha256=expected)
            except Exception as exc:
                problems.append(f"evidence_refs[{index}] {exc}")
                continue
            if ref.get("schema") != source.payload.get("schema"):
                problems.append(f"evidence_refs[{index}] schema mismatch")
            sources.append(source)

    source_fields = payload.get("source_fields")
    if not isinstance(source_fields, list) or not all(isinstance(item, str) for item in source_fields):
        problems.append("source_fields must be a string array")
        source_fields = []
    facts = payload.get("facts")
    if not isinstance(facts, list) or not facts:
        problems.append("facts must be nonempty")
    else:
        for index, item in enumerate(facts):
            if not isinstance(item, dict):
                problems.append(f"facts[{index}] must be an object")
                continue
            ref_index = item.get("source_ref")
            field = item.get("field")
            if not _int_not_bool(ref_index) or ref_index < 0 or ref_index >= len(sources):
                problems.append(f"facts[{index}].source_ref invalid")
                continue
            if not isinstance(field, str):
                problems.append(f"facts[{index}].field must be a string")
                continue
            try:
                actual = _resolve_pointer(sources[ref_index].payload, field)
            except PlayByPlayError as exc:
                problems.append(f"facts[{index}] {exc}")
                continue
            if actual != item.get("value"):
                problems.append(f"facts[{index}] value mismatch at {field}")
            if field not in source_fields:
                problems.append(f"facts[{index}] field not listed in source_fields: {field}")
            statement = item.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                problems.append(f"facts[{index}].statement must be nonempty")
            else:
                encoded = json.dumps(item.get("value"), sort_keys=True, separators=(",", ":"))
                if encoded not in statement:
                    problems.append(f"facts[{index}].statement does not include bound value")

    causal_refs = payload.get("causal_refs")
    if not isinstance(causal_refs, list) or not all(isinstance(item, str) for item in causal_refs):
        problems.append("causal_refs must be a string array")

    event_type = payload.get("event_type")
    actor = payload.get("actor")
    statement_kind = payload.get("statement_kind")
    if event_type in {"judge_called", "judge_verdict_recorded", "score_delta_recorded"} and actor not in {"judge", "scorekeeper"}:
        problems.append(f"{event_type} must come from Judge or scorekeeper authority")
    if event_type == "win_recorded" and (actor not in {"judge", "scorekeeper"} or statement_kind not in {"judge_confirmation", "scorekeeper_confirmation"}):
        problems.append("win_recorded requires Judge or scorekeeper confirmation")
    if event_type == "kill_observed" and (actor not in {"system", "judge"} or statement_kind not in {"observation", "judge_confirmation"}):
        problems.append("kill_observed cannot be a team-only claim")

    delta = payload.get("score_delta")
    if not isinstance(delta, dict):
        problems.append("score_delta must be an object")
    else:
        authority = delta.get("authority")
        if authority not in ALLOWED_SCORE_AUTHORITIES:
            problems.append(f"score_delta.authority not allowed: {authority!r}")
        red = delta.get("red")
        blue = delta.get("blue")
        if not _int_not_bool(red) or not _int_not_bool(blue):
            problems.append("score_delta red/blue must be integers")
        elif (red or blue) and authority == "none":
            problems.append("nonzero score_delta requires Judge or scorekeeper authority")
    after = payload.get("score_after")
    if not isinstance(after, dict) or not _int_not_bool(after.get("red")) or not _int_not_bool(after.get("blue")):
        problems.append("score_after red/blue must be integers")
    if not isinstance(payload.get("non_claims"), list) or not payload.get("non_claims"):
        problems.append("non_claims must be nonempty")
    if problems:
        raise PlayByPlayError("; ".join(problems))


def validate_timeline(events: list[dict[str, Any]], *, require_terminal: bool = True) -> None:
    previous_sequence = 0
    previous_time: datetime | None = None
    red = blue = 0
    seen_ids: set[str] = set()
    round_started: set[int] = set()
    round_closed: set[int] = set()
    round_terminal: set[int] = set()
    for item in events:
        validate_event(item)
        event_id = item["event_id"]
        if event_id in seen_ids:
            raise PlayByPlayError(f"duplicate event_id: {event_id}")
        for causal_ref in item["causal_refs"]:
            if causal_ref not in seen_ids:
                raise PlayByPlayError(f"causal ref does not point to an earlier event: {causal_ref}")
        sequence = item["sequence"]
        if sequence != previous_sequence + 1:
            raise PlayByPlayError(f"sequence ordering broken at {sequence}; expected {previous_sequence + 1}")
        current_time = _parse_time(item["occurred_at"])
        if previous_time is not None and current_time is not None and current_time < previous_time:
            raise PlayByPlayError(f"time ordering broken at sequence {sequence}")
        delta = item["score_delta"]
        red += int(delta["red"])
        blue += int(delta["blue"])
        if item["score_after"] != {"red": red, "blue": blue}:
            raise PlayByPlayError(f"score_after mismatch at sequence {sequence}")
        round_number = item.get("round_number")
        if isinstance(round_number, int):
            if item["event_type"] == "round_started":
                round_started.add(round_number)
            if item["event_type"] == "terminal_event_recorded":
                round_terminal.add(round_number)
            if item["event_type"] == "round_closed":
                round_closed.add(round_number)
        previous_sequence = sequence
        previous_time = current_time
        seen_ids.add(event_id)
    if require_terminal and round_started - round_terminal:
        raise PlayByPlayError(f"missing terminal events for rounds: {sorted(round_started - round_terminal)}")
    if require_terminal and round_started - round_closed:
        raise PlayByPlayError(f"missing round_closed events for rounds: {sorted(round_started - round_closed)}")
    if require_terminal and events and events[-1]["event_type"] != "battle_closed":
        raise PlayByPlayError("missing terminal battle_closed event")


def write_commentary_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    validate_timeline(events)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists() and path.stat().st_size:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                existing.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise PlayByPlayError(f"truncated or invalid commentary jsonl at line {line_number}: {exc}") from exc
        validate_timeline(existing, require_terminal=False)
        expected_prefix = events[: len(existing)]
        if existing != expected_prefix:
            raise PlayByPlayError(f"commentary jsonl existing prefix mismatch; refusing rewrite: {path}")
        if len(existing) == len(events):
            return
    with path.open("a", encoding="utf-8") as handle:
        for item in events[len(existing):]:
            handle.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")


def build_production_scale_timeline(*, run_receipt_path: Path, out_dir: Path) -> dict[str, Any]:
    run = read_receipt(run_receipt_path)
    receipt = run.payload
    battle_id = str(receipt.get("battle_id") or run_receipt_path.parent.name)
    events: list[dict[str, Any]] = []
    seq = 1
    score = {"red": 0, "blue": 0}
    last_event_id: str | None = None

    def add(*, sources: list[tuple[ReceiptSource, str]], causal_refs: list[str] | None = None, **kwargs: Any) -> str:
        nonlocal seq, score, last_event_id
        refs = [evidence_ref(source, pointer) for source, pointer in sources]
        facts = [fact_from(source, pointer, source_ref=index) for index, (source, pointer) in enumerate(sources)]
        delta = kwargs.get("score_delta") or {"red": 0, "blue": 0, "authority": "none"}
        score = {"red": score["red"] + int(delta["red"]), "blue": score["blue"] + int(delta["blue"])}
        kwargs["score_after"] = dict(score)
        event_payload = event(
            battle_id=battle_id,
            sequence=seq,
            evidence_refs=refs,
            facts=facts,
            source_fields=[pointer for _, pointer in sources],
            causal_refs=causal_refs if causal_refs is not None else ([last_event_id] if last_event_id else []),
            **kwargs,
        )
        events.append(event_payload)
        seq += 1
        last_event_id = event_payload["event_id"]
        return last_event_id

    arena_path = Path(str(receipt.get("arena_contract") or ""))
    arena = read_receipt(arena_path) if arena_path.is_file() else run
    add(
        event_type="arena_open",
        actor="arena",
        summary=f"Arena opened for target {receipt.get('target_identity', 'unknown target')}.",
        occurred_at=_time_from(arena.payload, receipt),
        sources=[(arena, "/target_identity" if "target_identity" in arena.payload else "/schema")],
        statement_kind="observation",
        certainty="observed_receipt",
    )
    auth_path = Path(str(receipt.get("authorization_validation") or ""))
    auth = read_receipt(auth_path) if auth_path.is_file() else run
    add(
        event_type="authorization_checked",
        actor="authorization",
        summary=f"Authorization receipt recorded status {auth.payload.get('status', 'unknown')}.",
        occurred_at=_time_from(auth.payload, arena.payload, receipt),
        sources=[(auth, "/status")],
        statement_kind="observation",
        certainty="verified_hash",
    )

    for raw_round in receipt.get("rounds") or []:
        if not isinstance(raw_round, dict):
            continue
        round_path = Path(str(raw_round.get("path") or ""))
        round_source = read_receipt(round_path, expected_sha256=raw_round.get("sha256")) if round_path.is_file() else run
        round_payload = round_source.payload if round_source is not run else raw_round
        n = int(round_payload.get("round_number") or 0)
        created = _time_from(round_payload, receipt)
        round_started_at = _round_start_time(round_payload, receipt)
        round_start_id = add(
            event_type="round_started",
            actor="system",
            round_number=n,
            summary=f"Round {n} opened on {round_payload.get('vulnerability', 'unknown vulnerability')}.",
            occurred_at=round_started_at,
            sources=[(round_source, "/round_number"), (round_source, "/vulnerability")],
            statement_kind="observation",
            certainty="observed_receipt",
        )
        add(
            event_type="scan_started",
            actor="red",
            round_number=n,
            summary=f"Red scan started from goal: {round_payload.get('red_goal', 'unspecified')}.",
            occurred_at=round_started_at,
            sources=[(round_source, "/red_goal")],
            statement_kind="team_claim",
            causal_refs=[round_start_id],
        )
        add(
            event_type="scan_result_recorded",
            actor="red",
            round_number=n,
            summary=f"Red scan results recorded {len(round_payload.get('red_scanned') or [])} items.",
            occurred_at=round_started_at,
            sources=[(round_source, "/red_scanned")],
            statement_kind="team_claim",
        )
        add(
            event_type="research_recorded",
            actor="red",
            round_number=n,
            summary=f"Red research/attempt notes recorded {len(round_payload.get('red_attempts') or [])} items.",
            occurred_at=round_started_at,
            sources=[(round_source, "/red_attempts")],
            statement_kind="team_claim",
        )
        add(
            event_type="scan_started",
            actor="blue",
            round_number=n,
            summary=f"Blue scan started from plan: {round_payload.get('blue_plan', 'unspecified')}.",
            occurred_at=round_started_at,
            sources=[(round_source, "/blue_plan")],
            statement_kind="team_claim",
            causal_refs=[round_start_id],
        )
        add(
            event_type="scan_result_recorded",
            actor="blue",
            round_number=n,
            summary=f"Blue scan results recorded {len(round_payload.get('blue_scanned') or [])} items.",
            occurred_at=round_started_at,
            sources=[(round_source, "/blue_scanned")],
            statement_kind="team_claim",
        )
        add(
            event_type="specimen_materialized",
            actor="system",
            round_number=n,
            summary="Original specimen materialized with retained artifact hash.",
            occurred_at=round_started_at,
            sources=[(round_source, "/artifacts/original_app_sha256")],
            statement_kind="observation",
        )
        add(
            event_type="patch_materialized",
            actor="blue",
            round_number=n,
            summary="Patched specimen materialized with retained artifact hash.",
            occurred_at=round_started_at,
            sources=[(round_source, "/artifacts/patched_app_sha256")],
            statement_kind="team_claim",
        )
        attempt_sources: list[tuple[dict[str, Any], ReceiptSource]] = []
        for attempt in round_payload.get("docker_attempts") or []:
            if not isinstance(attempt, dict):
                continue
            attempt_path = Path(str(attempt.get("path") or ""))
            attempt_source = read_receipt(attempt_path, expected_sha256=attempt.get("sha256")) if attempt_path.is_file() else round_source
            attempt_sources.append((attempt, attempt_source))
            add(
                event_type="contact_observed",
                actor="judge",
                round_number=n,
                summary=f"Judge observed {attempt.get('label', 'attempt')} contact with exit {attempt.get('exit_code')} and gate {attempt.get('gate')}.",
                occurred_at=_time_from(attempt, round_payload),
                sources=[(attempt_source, "/exit_code"), (attempt_source, "/gate")],
                statement_kind="judge_confirmation",
                certainty="verified_hash",
            )
            add(
                event_type="judge_called",
                actor="judge",
                round_number=n,
                summary=f"Judge receipt for round {n} {attempt.get('label', 'attempt')} was called.",
                occurred_at=_time_from(attempt, round_payload),
                sources=[(attempt_source, "/label"), (attempt_source, "/exit_code")],
                statement_kind="judge_confirmation",
                certainty="verified_hash",
            )
        original_source = attempt_sources[0][1] if attempt_sources else round_source
        patched_source = attempt_sources[1][1] if len(attempt_sources) > 1 else original_source
        add(
            event_type="kill_observed",
            actor="system",
            round_number=n,
            summary="No kill attribution is made unless the cited process status records it.",
            occurred_at=created,
            sources=[(original_source, "/process_status" if "process_status" in original_source.payload else "/schema")],
            statement_kind="observation",
            certainty="verified_hash",
        )
        add(
            event_type="crash_observed",
            actor="system",
            round_number=n,
            summary="Crash status is copied from the cited infra/process fields, not inferred from team prose.",
            occurred_at=created,
            sources=[(original_source, "/infra_error" if "infra_error" in original_source.payload else "/schema")],
            statement_kind="observation",
            certainty="verified_hash",
        )
        add(
            event_type="containment_observed",
            actor="judge",
            round_number=n,
            summary="Containment observation is bound to patched Judge and outcome-semantics fields.",
            occurred_at=created,
            sources=[(patched_source, "/semantic_exit_code" if "semantic_exit_code" in patched_source.payload else "/schema"), (round_source, "/outcome_semantics/patched_block_signal_signed")],
            statement_kind="judge_confirmation",
            certainty="verified_hash",
        )
        add(
            event_type="regression_observed",
            actor="judge",
            round_number=n,
            summary="Regression observation is bound to the Judge verdict and target-unavailability policy.",
            occurred_at=created,
            sources=[(round_source, "/verdict"), (round_source, "/outcome_semantics/target_unavailability_is_defense")],
            statement_kind="judge_confirmation",
            certainty="verified_hash",
        )
        delta = {"red": int(round_payload.get("red_score") or 0), "blue": int(round_payload.get("blue_score") or 0), "authority": "judge_receipt"}
        add(
            event_type="judge_verdict_recorded",
            actor="judge",
            round_number=n,
            summary=f"Round {n} Judge verdict recorded {round_payload.get('verdict', 'unknown')}.",
            occurred_at=created,
            sources=[(round_source, "/verdict"), (round_source, "/finding_confirmed")],
            statement_kind="judge_confirmation",
            certainty="score_authority",
            claims=["Judge verdict is copied from the cited round receipt."],
        )
        add(
            event_type="score_delta_recorded",
            actor="judge",
            round_number=n,
            summary=f"Round {n} Judge-derived score delta recorded Red {delta['red']} / Blue {delta['blue']}.",
            occurred_at=created,
            sources=[(round_source, "/red_score"), (round_source, "/blue_score"), (round_source, "/verdict")],
            score_delta=delta,
            statement_kind="judge_confirmation",
            certainty="score_authority",
            claims=["Score delta is copied from the cited round receipt."],
        )
        add(
            event_type="win_recorded",
            actor="judge",
            round_number=n,
            summary=f"Round {n} winner semantics recorded by Judge verdict {round_payload.get('verdict', 'unknown')}.",
            occurred_at=created,
            sources=[(round_source, "/verdict")],
            statement_kind="judge_confirmation",
            certainty="score_authority",
        )
        close_time = created
        spawn = round_payload.get("spawn_receipt")
        if isinstance(spawn, dict) and spawn.get("path"):
            spawn_source = read_receipt(Path(str(spawn["path"])), expected_sha256=spawn.get("sha256"))
            spawn_time = _time_from(spawn_source.payload, round_payload)
            close_time = _max_time(created, spawn_time)
            add(
                event_type="spawn_preflight_recorded",
                actor="lineage",
                round_number=n,
                summary=f"Round {n} spawn receipt recorded status {spawn_source.payload.get('status')} for {len(spawn_source.payload.get('children') or [])} children.",
                occurred_at=spawn_time,
                sources=[(spawn_source, "/status"), (spawn_source, "/children")],
                statement_kind="observation",
                certainty="verified_hash",
            )
        else:
            add(
                event_type="spawn_preflight_recorded",
                actor="lineage",
                round_number=n,
                summary="Spawn preflight was not applicable because no spawn receipt is cited for this round.",
                occurred_at=created,
                sources=[(round_source, "/verdict")],
                statement_kind="non_claim",
                certainty="non_claim",
            )
        add(
            event_type="terminal_event_recorded",
            actor="system",
            round_number=n,
            summary=f"Round {n} terminal status {round_payload.get('status')} recorded with {len(round_payload.get('incomplete_gates') or [])} incomplete gates.",
            occurred_at=close_time,
            sources=[(round_source, "/status"), (round_source, "/incomplete_gates")],
            statement_kind="observation",
            certainty="verified_hash",
        )
        add(
            event_type="round_closed",
            actor="system",
            round_number=n,
            summary=f"Round {n} closed with status {round_payload.get('status')} and verdict {round_payload.get('verdict')}.",
            occurred_at=close_time,
            sources=[(round_source, "/status"), (round_source, "/verdict")],
            statement_kind="observation",
            certainty="verified_hash",
        )

    scorekeeper_path = Path(str(receipt.get("scorekeeper_receipt") or ""))
    scorekeeper = read_receipt(scorekeeper_path) if scorekeeper_path.is_file() else run
    final_red = int(scorekeeper.payload.get("red_total") or 0)
    final_blue = int(scorekeeper.payload.get("blue_total") or 0)
    add(
        event_type="scorekeeper_final",
        actor="scorekeeper",
        summary=f"Scorekeeper recorded final Red {scorekeeper.payload.get('red_total')} / Blue {scorekeeper.payload.get('blue_total')} and winner {scorekeeper.payload.get('winner')}.",
        occurred_at=_time_from(scorekeeper.payload, receipt),
        sources=[(scorekeeper, "/red_total"), (scorekeeper, "/blue_total"), (scorekeeper, "/winner")],
        score_delta={"red": final_red - score["red"], "blue": final_blue - score["blue"], "authority": "scorekeeper_receipt"},
        statement_kind="scorekeeper_confirmation",
        certainty="score_authority",
        claims=["Final score and winner are copied from the cited scorekeeper receipt."],
    )
    add(
        event_type="battle_closed",
        actor="system",
        summary=f"Battle receipt recorded status {receipt.get('status')}.",
        occurred_at=_time_from(receipt, scorekeeper.payload),
        sources=[(run, "/status")],
        statement_kind="observation",
        certainty="verified_hash",
    )

    return rebuild_outputs(events=events, out_dir=out_dir, battle_id=battle_id)


def rebuild_outputs(*, events: list[dict[str, Any]], out_dir: Path, battle_id: str | None = None) -> dict[str, Any]:
    validate_timeline(events)
    if battle_id is None:
        battle_id = str(events[0].get("battle_id") if events else "unknown")
    commentary_path = out_dir / "commentary.jsonl"
    write_commentary_jsonl(commentary_path, events)
    projection_path = out_dir / "SPORTS_BROADCAST.md"
    projection_path.write_text(render_sports_projection(events), encoding="utf-8")
    round_events = [
        {"round_number": n, "events": [item for item in events if item.get("round_number") == n]}
        for n in sorted({item.get("round_number") for item in events if item.get("round_number") is not None})
    ]
    observed_types = sorted({item["event_type"] for item in events})
    missing_types = sorted(set(REQUIRED_EVENT_TYPES) - set(observed_types))
    timeline = {
        "schema": TIMELINE_SCHEMA,
        "status": "PASS" if not missing_types else "INCOMPLETE",
        "battle_id": battle_id,
        "commentary_jsonl": str(commentary_path),
        "commentary_jsonl_sha256": sha256_jsonl(commentary_path),
        "projection": str(projection_path),
        "projection_sha256": sha256_file(projection_path),
        "battle_events": events,
        "round_events": round_events,
        "event_type_source_mapping": EVENT_SOURCE_MAPPING,
        "semantic_coverage": {
            "required_event_types": list(REQUIRED_EVENT_TYPES),
            "observed_event_types": observed_types,
            "missing_event_types": missing_types,
            "required_field_count": 19,
            "required_fields": [
                "schema",
                "event_id",
                "battle_id",
                "round_number",
                "sequence",
                "occurred_at",
                "event_type",
                "actor",
                "period",
                "summary",
                "certainty",
                "statement_kind",
                "evidence_refs",
                "source_fields",
                "facts",
                "causal_refs",
                "score_delta",
                "score_after",
                "non_claims",
            ],
            "proof_boundary": "Primary run emits observed/not-applicable receipt-backed events; rare positive kill/crash cases require controlled fixture receipts.",
        },
        "outputs_rebuilt_from_events": ["commentary_jsonl", "round_events", "battle_events", "projection"],
        "checks": [
            "sequence_order_verified",
            "time_order_verified",
            "stable_event_ids_verified",
            "causal_refs_point_backward",
            "evidence_hashes_verified",
            "facts_match_json_pointer_values",
            "terminal_events_present",
            "score_deltas_have_judge_or_scorekeeper_authority",
            "projection_uses_event_facts_and_summaries_only",
        ],
    }
    timeline_path = out_dir / "play-by-play-timeline.json"
    timeline_path.write_text(json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    timeline["path"] = str(timeline_path)
    timeline["sha256"] = sha256_file(timeline_path)
    return timeline


def render_sports_projection(events: list[dict[str, Any]]) -> str:
    validate_timeline(events)
    lines = [
        "# Receipt-backed Battle play-by-play",
        "",
        "This projection is generated only from cited play-by-play events. It does not infer motive, causality, uncited success, or impact.",
        "",
    ]
    for item in events:
        refs = ", ".join(f"{Path(ref['path']).name}@{ref['sha256'][:12]}{ref['json_pointer']}" for ref in item["evidence_refs"])
        score = item["score_after"]
        fact_text = "; ".join(str(fact["statement"]) for fact in item["facts"])
        lines.append(
            f"- [{item['sequence']:02d}] {item['period']} / {item['event_type']} / {item['statement_kind']}: {item['summary']} "
            f"Facts: {fact_text}. Score Red {score['red']} / Blue {score['blue']}. Certainty: {item['certainty']}. Evidence: {refs}."
        )
    return "\n".join(lines) + "\n"


def _facts_from_refs(evidence_refs: list[dict[str, str]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for index, ref in enumerate(evidence_refs):
        pointer = str(ref.get("json_pointer") or "")
        if not pointer:
            pointer = "/schema"
        source = read_receipt(Path(str(ref.get("path"))), expected_sha256=str(ref.get("sha256")))
        facts.append(fact_from(source, pointer, source_ref=index))
    return facts


def _resolve_pointer(payload: dict[str, Any], pointer: str) -> Any:
    if pointer == "":
        return payload
    if not pointer.startswith("/"):
        raise PlayByPlayError(f"json pointer must start with / or be empty: {pointer}")
    current: Any = payload
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                raise PlayByPlayError(f"missing json pointer field: {pointer}")
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise PlayByPlayError(f"missing json pointer field: {pointer}") from exc
        else:
            raise PlayByPlayError(f"missing json pointer field: {pointer}")
    return current


def _int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normal_sha(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_from(*payloads: dict[str, Any]) -> str:
    for payload in payloads:
        for key in ("created_at", "completed_at", "generated_at", "started_at"):
            value = payload.get(key)
            if isinstance(value, str) and _parse_time(value):
                return value
    return "1970-01-01T00:00:00Z"


def _round_start_time(round_payload: dict[str, Any], fallback: dict[str, Any]) -> str:
    attempts = round_payload.get("docker_attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict):
                value = _time_from({"created_at": attempt.get("started_at") or attempt.get("created_at")})
                if value != "1970-01-01T00:00:00Z":
                    return value
    return _time_from(round_payload, fallback)


def _max_time(*values: str) -> str:
    parsed = [(value, _parse_time(value)) for value in values]
    valid = [(value, dt) for value, dt in parsed if dt is not None]
    if not valid:
        return "1970-01-01T00:00:00Z"
    return max(valid, key=lambda item: item[1])[0]
