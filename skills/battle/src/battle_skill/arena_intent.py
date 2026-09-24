"""Arena-maker intent hashing for Battle receipts.

The arena-maker intent is the immutable semantic question a Battle run answers.
Operational details may be carried alongside it, but the thirteen top-level AM-03
fields below are the contract every child receipt binds by hash.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "battle.arena_maker_intent.v1"
BINDING_SCHEMA = "battle.arena_maker_intent_binding.v1"
AUTHORITY_SCHEMA = "battle.arena_maker_authority.v1"
VERIFY_SCHEMA = "battle.arena_maker_intent_verification.v1"

ARENA_MAKER_PRINCIPALS = ("battle-control-plane", "battle-arena-maker")
JUDGE_PRINCIPALS = ("battle-judge", "battle-scorekeeper")
PARTICIPANT_PRINCIPALS = ("battle-red-team", "battle-blue-team", "red", "blue")
AUTHORITY_ROLES = frozenset({"arena_maker", "judge", "scorekeeper", "participant", "bound_consumer"})

REQUIRED_INTENT_FIELDS = (
    "arena_goal",
    "why_this_arena",
    "threat_hypothesis",
    "red_mission",
    "blue_mission",
    "expected_red_behaviors",
    "expected_blue_behaviors",
    "protected_invariants",
    "success_signals",
    "failure_signals",
    "out_of_scope",
    "selection_rationale",
    "difficulty_rationale",
)
LIST_INTENT_FIELDS = {
    "expected_red_behaviors",
    "expected_blue_behaviors",
    "protected_invariants",
    "success_signals",
    "failure_signals",
    "out_of_scope",
}
ALLOWED_TOP_LEVEL_FIELDS = {"schema", *REQUIRED_INTENT_FIELDS, "operational_bindings", "synthesized_from_legacy"}


class ArenaIntentError(ValueError):
    """Raised when a Battle arena intent is missing, mismatched, or mutated."""


def canonical_bytes(payload: Any) -> bytes:
    """Canonical AM-03 serialization: sorted object keys, preserved list order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_json(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def file_sha256(path: Path | None) -> str:
    if path is None:
        return "not_applicable:no_file"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_arena_maker_intent(
    *,
    battle_id: str,
    target_identity: str,
    authorization_manifest: Path | None,
    arena_contract: Path | None,
    runtime_mode: str,
    round_count: int,
    objective: str,
    proof_boundary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the AM-03 semantic intent plus separate operational bindings."""
    return {
        "schema": SCHEMA,
        "arena_goal": objective,
        "why_this_arena": (
            f"Run {battle_id} against {target_identity} so Red and Blue answer one fixed "
            "arena question under receipt-backed Judge and scorekeeper authority."
        ),
        "threat_hypothesis": (
            "Red may exploit the public target surface while Blue may ship incomplete or "
            "behavior-breaking mitigations; the arena tests both claims with executable receipts."
        ),
        "red_mission": "Find and demonstrate authorized exploit evidence without crossing the proof boundary.",
        "blue_mission": "Patch or harden the target while preserving the protected behavior invariants.",
        "expected_red_behaviors": [
            "Inspect only the authorized public target surface.",
            "Submit replayable exploit evidence for Judge verification.",
            "Preserve normal-behavior evidence when claiming a useful exploit.",
        ],
        "expected_blue_behaviors": [
            "Patch the demonstrated weakness without hiding the public contract.",
            "Preserve normal behavior while blocking Red replay.",
            "Emit receipts that bind the same arena-maker intent hash.",
        ],
        "protected_invariants": [
            "Authorization must pass before Red, Blue, Judge, or scorekeeper dispatch.",
            "Judge and scorekeeper receipts, not team prose, decide outcome claims.",
            "The frozen arena-maker intent hash must remain identical across bound receipts.",
        ],
        "success_signals": [
            "Authorization receipt, team handoffs, Judge receipts, scorekeeper, and report carry one intent hash.",
            "Executable Judge evidence supports any Red or Blue success verdict.",
            "Post-freeze intent mutation or missing binding fails closed.",
        ],
        "failure_signals": [
            "Any required intent field is missing or empty.",
            "Any bound receipt omits or changes the arena-maker intent hash.",
            "Scorekeeper cannot recompute the frozen intent hash from the on-disk artifact.",
        ],
        "out_of_scope": [
            "External production deployment readiness.",
            "Unbounded target authorization beyond the supplied manifest.",
            "Claims that Red or Blue succeeded without executable Judge evidence.",
        ],
        "selection_rationale": (
            "This arena is selected because it exercises authorization, dispatch, Judge, "
            "scorekeeper, and report bindings in one verifiable local Battle flow."
        ),
        "difficulty_rationale": (
            f"Difficulty is calibrated for {runtime_mode} with {round_count} receipt-backed round(s), "
            "enough to catch drift without requiring overnight scale."
        ),
        "operational_bindings": {
            "schema": "battle.arena_maker_intent.operational_bindings.v1",
            "battle_id": battle_id,
            "target_identity": target_identity,
            "authorization_manifest_sha256": file_sha256(authorization_manifest),
            "arena_contract_sha256": file_sha256(arena_contract),
            "runtime_mode": runtime_mode,
            "score_authority": "judge_and_scorekeeper_receipts_only",
            "judge_authority": "independent_replay_receipts_required",
            "dispatch_policy": "freeze_intent_before_red_blue_or_judge_dispatch",
            "proof_boundary": proof_boundary
            or {
                "proves": ["intent hash bound before team dispatch"],
                "does_not_prove": ["external production deployment readiness"],
            },
            "round_count": round_count,
        },
    }


def validate_arena_intent(intent: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(intent, dict):
        return ["intent must be an object"]
    extra = sorted(set(intent) - ALLOWED_TOP_LEVEL_FIELDS)
    if extra:
        problems.append(f"unknown top-level intent fields: {extra}")
    if intent.get("schema") != SCHEMA:
        problems.append(f"schema must equal {SCHEMA}")
    for field in REQUIRED_INTENT_FIELDS:
        value = intent.get(field)
        if field in LIST_INTENT_FIELDS:
            if not isinstance(value, list) or not value:
                problems.append(f"{field} must be a non-empty list")
            else:
                for index, item in enumerate(value):
                    if not isinstance(item, str) or not item.strip():
                        problems.append(f"{field}[{index}] must be a non-empty string")
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"{field} must be a non-empty string")
    if "synthesized_from_legacy" in intent and intent.get("synthesized_from_legacy") is not True:
        problems.append("synthesized_from_legacy must be true when present")
    bindings = intent.get("operational_bindings")
    if bindings is not None:
        if not isinstance(bindings, dict):
            problems.append("operational_bindings must be an object")
        elif bindings.get("schema") != "battle.arena_maker_intent.operational_bindings.v1":
            problems.append("operational_bindings.schema is invalid")
    return problems


def canonical_arena_intent(payload: dict[str, Any]) -> dict[str, Any]:
    problems = validate_arena_intent(payload)
    if problems:
        raise ArenaIntentError("; ".join(problems))
    return json.loads(canonical_bytes(payload).decode("ascii"))


def arena_intent_sha256(payload: dict[str, Any]) -> str:
    return sha256_json(canonical_arena_intent(payload))


def _operational_binding_value(intent: dict[str, Any], key: str) -> Any:
    bindings = intent.get("operational_bindings")
    return bindings.get(key) if isinstance(bindings, dict) else None


def frozen_intent_binding(intent: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return the authority binding for an already-frozen arena intent."""
    canonical = canonical_arena_intent(intent)
    digest = arena_intent_sha256(canonical)
    battle_id = _operational_binding_value(canonical, "battle_id") or "unknown_battle"
    target_identity = _operational_binding_value(canonical, "target_identity") or "unknown_target"
    authorization_sha256 = _operational_binding_value(canonical, "authorization_manifest_sha256") or "not_applicable:no_file"
    arena_contract_sha256 = _operational_binding_value(canonical, "arena_contract_sha256") or "not_applicable:no_file"
    return {
        "schema": BINDING_SCHEMA,
        "arena_maker_intent_schema": SCHEMA,
        "arena_maker_intent_path": str(path),
        "arena_maker_intent_sha256": digest,
        "freeze_status": "FROZEN",
        "campaign_identity": str(battle_id),
        "baseline_identity": str(target_identity),
        "authorization_manifest_sha256": str(authorization_sha256),
        "arena_contract_sha256": str(arena_contract_sha256),
        "arena_version_id": f"{battle_id}:{digest}",
        "allowed_arena_maker_principals": list(ARENA_MAKER_PRINCIPALS),
        "allowed_judge_principals": list(JUDGE_PRINCIPALS),
        "participant_principals": list(PARTICIPANT_PRINCIPALS),
    }


def freeze_arena_maker_intent(intent: dict[str, Any], path: Path) -> dict[str, Any]:
    """Write intent before team dispatch and return the immutable authority binding."""
    canonical = canonical_arena_intent(intent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(canonical, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return frozen_intent_binding(canonical, path)


def intent_ref(binding: dict[str, Any]) -> dict[str, str]:
    digest = binding.get("arena_maker_intent_sha256")
    if not isinstance(digest, str) or not digest:
        raise ArenaIntentError("arena maker intent binding is missing sha256")
    return {"schema": SCHEMA, "sha256": digest}


def authority_ref(
    binding: dict[str, Any],
    *,
    principal: str = "battle-control-plane",
    authority_role: str = "bound_consumer",
) -> dict[str, Any]:
    if authority_role not in AUTHORITY_ROLES:
        raise ArenaIntentError(f"unknown arena authority role: {authority_role}")
    return {
        "schema": AUTHORITY_SCHEMA,
        "issuer_principal": principal,
        "authority_role": authority_role,
        "campaign_identity": binding.get("campaign_identity"),
        "baseline_identity": binding.get("baseline_identity"),
        "authorization_manifest_sha256": binding.get("authorization_manifest_sha256"),
        "arena_contract_sha256": binding.get("arena_contract_sha256"),
        "arena_version_id": binding.get("arena_version_id"),
        "arena_maker_intent_sha256": binding.get("arena_maker_intent_sha256"),
    }


def bind_intent(
    payload: dict[str, Any],
    binding: dict[str, Any],
    *,
    principal: str = "battle-control-plane",
    authority_role: str = "bound_consumer",
) -> dict[str, Any]:
    payload = dict(payload)
    payload["arena_maker_intent"] = intent_ref(binding)
    payload["arena_authority"] = authority_ref(binding, principal=principal, authority_role=authority_role)
    return payload


def verify_frozen_intent(binding: dict[str, Any]) -> dict[str, Any]:
    path = binding.get("arena_maker_intent_path")
    expected = binding.get("arena_maker_intent_sha256")
    problems: list[str] = []
    actual = None
    if not isinstance(path, str) or not path:
        problems.append("arena_maker_intent_path missing")
    elif not Path(path).is_file():
        problems.append("arena_maker_intent_path missing")
    else:
        try:
            intent = json.loads(Path(path).read_text(encoding="utf-8"))
            problems.extend(validate_arena_intent(intent))
            actual = arena_intent_sha256(intent) if not problems else sha256_json(intent)
        except Exception as exc:
            problems.append(f"arena_maker_intent unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(expected, str) or not expected:
        problems.append("arena_maker_intent_sha256 missing")
    elif actual is not None and actual != expected:
        problems.append("ARENA_INTENT_DRIFT: arena_maker_intent post-freeze mutation or hash mismatch")
    return {
        "schema": VERIFY_SCHEMA,
        "status": "FAIL" if problems else "PASS",
        "arena_maker_intent_sha256": expected,
        "actual_arena_maker_intent_sha256": actual,
        "problems": problems,
    }


def _verify_authority(
    payload: dict[str, Any],
    binding: dict[str, Any],
    *,
    label: str,
    expected_authority_role: str | None,
) -> list[str]:
    problems: list[str] = []
    authority = payload.get("arena_authority") if isinstance(payload, dict) else None
    if not isinstance(authority, dict):
        return [f"{label} missing arena_authority"]
    if authority.get("schema") != AUTHORITY_SCHEMA:
        problems.append(f"{label} arena_authority schema mismatch")
    for key in (
        "campaign_identity",
        "baseline_identity",
        "authorization_manifest_sha256",
        "arena_contract_sha256",
        "arena_version_id",
        "arena_maker_intent_sha256",
    ):
        if authority.get(key) != binding.get(key):
            problems.append(f"{label} arena_authority {key} mismatch")
    role = authority.get("authority_role")
    principal = authority.get("issuer_principal")
    if role not in AUTHORITY_ROLES:
        problems.append(f"{label} arena_authority role invalid")
    if expected_authority_role is not None and role != expected_authority_role:
        problems.append(f"{label} arena_authority role {role!r} != {expected_authority_role!r}")
    arena_principals = set(binding.get("allowed_arena_maker_principals") or ARENA_MAKER_PRINCIPALS)
    judge_principals = set(binding.get("allowed_judge_principals") or JUDGE_PRINCIPALS)
    participant_principals = set(binding.get("participant_principals") or PARTICIPANT_PRINCIPALS)
    if role == "arena_maker" and principal not in arena_principals:
        problems.append(f"{label} unauthorized arena-maker principal {principal!r}")
    if role in {"judge", "scorekeeper"} and principal not in judge_principals:
        problems.append(f"{label} unauthorized Judge principal {principal!r}")
    if role in {"arena_maker", "judge", "scorekeeper"} and principal in participant_principals:
        problems.append(f"{label} participant principal cannot issue {role} authority")
    return problems


def verify_bound_payload(
    payload: dict[str, Any],
    binding: dict[str, Any],
    *,
    label: str,
    expected_authority_role: str | None = None,
) -> dict[str, Any]:
    frozen = verify_frozen_intent(binding)
    expected = binding.get("arena_maker_intent_sha256")
    ref = payload.get("arena_maker_intent") if isinstance(payload, dict) else None
    problems = list(frozen.get("problems", []))
    if not isinstance(ref, dict):
        problems.append(f"{label} missing arena_maker_intent")
    elif ref.get("schema") == SCHEMA and ref.get("sha256") == expected:
        pass
    elif ref.get("schema") == BINDING_SCHEMA and ref.get("arena_maker_intent_sha256") == expected:
        pass
    else:
        problems.append(f"{label} arena_maker_intent hash mismatch")
    problems.extend(_verify_authority(payload, binding, label=label, expected_authority_role=expected_authority_role))
    return {
        "schema": VERIFY_SCHEMA,
        "status": "FAIL" if problems else "PASS",
        "label": label,
        "arena_maker_intent_sha256": expected,
        "campaign_identity": binding.get("campaign_identity"),
        "baseline_identity": binding.get("baseline_identity"),
        "problems": problems,
    }


def require_bound_payload(
    payload: dict[str, Any],
    binding: dict[str, Any],
    *,
    label: str,
    expected_authority_role: str | None = None,
) -> None:
    receipt = verify_bound_payload(payload, binding, label=label, expected_authority_role=expected_authority_role)
    if receipt["status"] != "PASS":
        raise ArenaIntentError("; ".join(receipt["problems"]))
