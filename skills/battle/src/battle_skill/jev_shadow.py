"""Battle-owned shadow Jev advisory adapter.

Shadow mode ONLY, mirroring the house pattern in skills/triage-error/jev_shadow.py:
Jev may rank Red/Blue evidence candidates and emit hash-bound ``jev.decision.v2``
receipts, but this adapter can never change Battle execution. It returns BOTH the
Jev proposal and the deterministic Battle baseline ordering; in shadow mode the
caller must use the baseline. ``applied`` is always False.

Fail-closed rules (task contract):
- accepted/abstain/blocked/error/no_match distinctions are preserved verbatim;
- any non-accepted, invalid, or unavailable result falls back to the deterministic
  Battle baseline (input order, pinned candidates first is NOT applied -- baseline
  is exactly the caller's order);
- one Jev call per proposal; the Jev client's internal cooldown is the only
  backoff. This adapter adds no retry, fallback cascade, or quota store;
- payloads referencing Battle private artifacts never reach the Jev transport;
- receipts bind battle/run/round identity, question definition, policy/catalog
  hashes, candidate membership, and per-candidate source identity.

Jev is advisory: it must not authorize targets, execute tools, select adaptive
winners, alter the child-DAG route, judge findings or patches, compute scores,
approve spawns, or reinterpret campaign verdicts. Battle's score authority stays
``judge_receipts_only``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PROPOSAL_SCHEMA = "battle.jev_shadow_proposal.v1"
TRIAGE_SCHEMA = "battle.jev_prejudge_triage.v1"
QUALIFICATION_SCHEMA = "battle.jev_prejudge_qualification.v1"
DEFAULT_PREJUDGE_AUDIT_RATE = 0.1
MIN_PREJUDGE_AUDIT_RATE = 0.01
MAX_PREJUDGE_AUDIT_RATE = 1.0
DEFAULT_QUALIFICATION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# Battle private-artifact markers (mirror tau_child_dag.FORBIDDEN_INPUTS intent):
# a payload containing any of these is blocked before any egress attempt.
PRIVATE_MARKERS = (
    "arena/private/actual",
    "hidden-ground-truth.json",
    "hidden-vulnerability-ledger.json",
    "judge/oracle/source.py",
)

ALLOWED_FRESHNESS = {"unknown", "CURRENT", "STALE", "UNINDEXED",
                     "SOURCE_CURRENT_INDEX_INCOMPLETE", "BLOCKED"}


def shadow_enabled() -> bool:
    """Enabled only with JEV_API_KEY and not explicitly disabled (JEV_SHADOW=0)."""
    return bool(os.getenv("JEV_API_KEY")) and os.getenv("JEV_SHADOW", "1") not in {"0", "false", "no"}


def _stable_digest(value: Any) -> str:
    """SHA-256 over canonical JSON. Local binding only; jev.hash.v1 ownership
    stays inside jev_harness for request/receipt hashes."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(value: str) -> float | None:
    try:
        if value.endswith("Z"):
            return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
        return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def configured_prejudge_audit_rate() -> float:
    try:
        rate = float(os.getenv("BATTLE_JEV_PREJUDGE_AUDIT_RATE", str(DEFAULT_PREJUDGE_AUDIT_RATE)))
    except ValueError:
        rate = DEFAULT_PREJUDGE_AUDIT_RATE
    return min(MAX_PREJUDGE_AUDIT_RATE, max(MIN_PREJUDGE_AUDIT_RATE, rate))


@dataclass
class ShadowProposal:
    """Typed proposal receipt. ``applied`` is always False in shadow mode."""
    schema: str
    battle_id: str
    round_number: int
    team: Literal["red", "blue"]
    question_id: str
    status: Literal["accepted", "abstain", "blocked", "error", "no_match"]
    outcome: Literal["ranked", "baseline", "empty"]
    applied: bool
    baseline_order: list[str]
    jev_order: list[str] | None
    reason: str
    battle_binding: dict[str, Any] = field(default_factory=dict)
    request_binding: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "battle_id": self.battle_id,
            "round_number": self.round_number,
            "team": self.team,
            "question_id": self.question_id,
            "status": self.status,
            "outcome": self.outcome,
            "applied": self.applied,
            "baseline_order": self.baseline_order,
            "jev_order": self.jev_order,
            "reason": self.reason,
            "battle_binding": self.battle_binding,
            "request_binding": self.request_binding,
            "receipt": self.receipt,
            "created_at": self.created_at,
        }


def _validate_candidates(candidates: list[dict[str, Any]]) -> str | None:
    # Type-guard before attribute access: a non-dict candidate must become a
    # typed error receipt, never an AttributeError/KeyError escape hatch.
    if any(not isinstance(c, dict) for c in candidates):
        return "invalid_candidate_type"
    ids = [c.get("id") for c in candidates]
    if any(not isinstance(i, str) or not i for i in ids):
        return "invalid_candidate_id"
    if len(set(ids)) != len(ids):
        return "duplicate_candidate_ids"
    for c in candidates:
        if not isinstance(c.get("text"), str) or not c["text"]:
            return "invalid_candidate_text"
        if c.get("freshness", "unknown") not in ALLOWED_FRESHNESS:
            return "nonmember_freshness"
    return None


def _has_private_marker(payload: dict[str, Any]) -> str | None:
    text = json.dumps(payload, ensure_ascii=False).lower()
    for marker in PRIVATE_MARKERS:
        if marker.lower() in text:
            return marker
    return None


def _catalog_packet(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalized candidate catalog exactly as it is sent to Jev.

    ``catalog_hash`` binds THIS packet, so the receipt proves which
    qualified-offer catalog was judged. Keep field-for-field identical to the
    ``Candidate(...)`` construction below.
    """
    return [{
        "id": str(c["id"]), "text": c["text"], "kind": c.get("kind", "evidence"),
        "source": c.get("source") or {}, "pinned": bool(c.get("pinned", False)),
        "freshness": c.get("freshness", "unknown"),
    } for c in candidates]


def write_evidence_ranking_receipts(requests: list[dict[str, Any]]) -> list[ShadowProposal]:
    """Run a round's shadow proposals in one event loop and persist each receipt."""
    async def _run() -> list[ShadowProposal]:
        proposals = []
        for request in requests:
            try:
                proposal = await propose_evidence_ranking(**{
                    key: value for key, value in request.items() if key != "output_path"
                })
            except Exception as exc:  # noqa: BLE001 - shadow advice must never break Battle
                candidates = request["candidates"]
                proposal = ShadowProposal(
                    schema=PROPOSAL_SCHEMA,
                    battle_id=request["battle_id"],
                    round_number=request["round_number"],
                    team=request["team"],
                    question_id=request["question_id"],
                    status="error",
                    outcome="baseline",
                    applied=False,
                    baseline_order=[str(c.get("id")) for c in candidates if isinstance(c, dict) and c.get("id")],
                    jev_order=None,
                    reason=f"shadow_runtime:{type(exc).__name__}",
                )
            proposals.append(proposal)
        return proposals

    proposals = asyncio.run(_run())
    for request, proposal in zip(requests, proposals, strict=True):
        output_path = Path(request["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(proposal.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return proposals


def write_evidence_ranking_receipt(**request: Any) -> ShadowProposal:
    """Run and persist one shadow proposal without changing caller order."""
    return write_evidence_ranking_receipts([request])[0]


async def propose_evidence_ranking(
    *,
    battle_id: str,
    round_number: int,
    team: Literal["red", "blue"],
    question_id: str,
    task: dict[str, Any],
    candidates: list[dict[str, Any]],
    jev_factory: Any | None = None,
) -> ShadowProposal:
    """Rank evidence candidates through Jev in shadow mode.

    ``jev_factory`` is an optional async context-manager factory returning a
    ``jev_harness.Jev`` (or compatible) client. It is an explicit authorization
    path for an already-bounded local client (tests, host-injected harnesses);
    it needs no environment key. Production callers omit it and the real client
    is built from the environment, so an absent ``JEV_API_KEY`` stays fail-closed
    (blocked) there. Exactly one Jev ``ask`` happens per proposal.
    """
    base_binding = {
        "battle_id": battle_id,
        "round_number": round_number,
        "team": team,
        "question_id": question_id,
    }

    def _proposal(**kw: Any) -> ShadowProposal:
        kw.setdefault("battle_binding", dict(base_binding))
        kw.setdefault("applied", False)  # shadow mode: never applied, by construction
        merged = {**base_binding, **kw}
        return ShadowProposal(schema=PROPOSAL_SCHEMA, **merged)

    # Validate BEFORE any indexed access: a candidate missing ``id`` must yield
    # a typed error receipt with a baseline-safe (empty) order, never a KeyError.
    malformed = _validate_candidates(candidates) if candidates else None
    if malformed:
        return _proposal(status="error", outcome="baseline", baseline_order=[],
                         jev_order=None, reason=malformed,
                         request_binding={"candidate_hash": _stable_digest(candidates)})
    baseline_ids = [c["id"] for c in candidates]
    if not candidates:
        # Exact-empty evidence needs no hosted judgment (runtime rule 5).
        return _proposal(status="no_match", outcome="empty", baseline_order=[],
                         jev_order=None, reason="no_candidates",
                         request_binding={"candidate_hash": _stable_digest([]),
                                          "catalog_hash": _stable_digest([])})

    marker = _has_private_marker({"state": {"task": task, "team": team,
                                            "round_number": round_number},
                                  "candidates": candidates})
    if marker:
        return _proposal(status="blocked", outcome="baseline", baseline_order=baseline_ids,
                         jev_order=None, reason=f"private_marker:{marker}",
                         request_binding={"candidate_hash": _stable_digest(candidates),
                                          "catalog_hash": _stable_digest(_catalog_packet(candidates))})

    candidate_hash = _stable_digest(candidates)
    source_identity = {
        c["id"]: _stable_digest(c.get("source") or {}) for c in candidates
    }
    packet_dicts = _catalog_packet(candidates)
    request_binding = {
        "task_hash": _stable_digest(task),
        "candidate_hash": candidate_hash,
        "catalog_hash": _stable_digest(packet_dicts),
        "source_identity": source_identity,
        "question_id": question_id,
    }

    if (os.getenv("JEV_SHADOW", "1") in {"0", "false", "no"}) or (
        jev_factory is None and not shadow_enabled()
    ):
        # Explicit disable blocks every path. Production callers (no factory)
        # stay fail-closed without JEV_API_KEY. An explicitly injected local
        # client is its own authorization and does not need an env key.
        return _proposal(status="blocked", outcome="baseline", baseline_order=baseline_ids,
                         jev_order=None, reason="jev_disabled_or_no_api_key",
                         request_binding=request_binding)

    try:
        from jev_harness import Candidate, Policy, rank_candidates
    except ImportError as exc:  # jev not on PYTHONPATH: deterministic baseline
        return _proposal(status="error", outcome="baseline", baseline_order=baseline_ids,
                         jev_order=None, reason=f"jev_unavailable:{type(exc).__name__}",
                         request_binding=request_binding)

    packet = [Candidate(**p) for p in packet_dicts]

    async def _run() -> Any:
        policy = Policy(allow_egress=True, data_class="public")
        if jev_factory is not None:
            # Explicitly authorized bounded client; no environment key required.
            async with jev_factory(policy) as jev:
                return await rank_candidates(jev, {"task": task, "team": team,
                                                   "round_number": round_number}, packet)
        from jev_harness import Jev
        api_key = os.getenv("JEV_API_KEY", "")
        async with Jev(api_key=api_key, policy=policy) as jev:
            return await rank_candidates(jev, {"task": task, "team": team,
                                               "round_number": round_number}, packet)

    try:
        ranking = await _run()
    except Exception as exc:  # noqa: BLE001 - fail closed to baseline, keep type
        return _proposal(status="error", outcome="baseline", baseline_order=baseline_ids,
                         jev_order=None, reason=f"transport:{type(exc).__name__}",
                         request_binding=request_binding)

    receipt = ranking.receipt.model_dump(mode="json") if ranking.receipt is not None else None
    binding = dict(request_binding)
    if receipt is not None:
        binding["policy_hash"] = receipt.get("policy_hash")
        binding["request_hash"] = receipt.get("request_hash")
        binding["state_hash"] = receipt.get("state_hash")
        binding["questions_hash"] = receipt.get("questions_hash")

    if ranking.status == "ranked" and receipt is not None and receipt.get("status") == "accepted":
        jev_order = [c.id for c in ranking.kept]
        # Membership gate: the proposal may never introduce an id the caller did not supply.
        if not set(jev_order).issubset(set(baseline_ids)):
            return _proposal(status="error", outcome="baseline", baseline_order=baseline_ids,
                             jev_order=None, reason="nonmember_candidate_in_ranking",
                             request_binding=binding, receipt=receipt)
        return _proposal(status="accepted", outcome="ranked", baseline_order=baseline_ids,
                         jev_order=jev_order, reason=receipt.get("reason", ""),
                         request_binding=binding, receipt=receipt)

    if ranking.status == "empty":
        return _proposal(status="no_match", outcome="empty", baseline_order=[],
                         jev_order=None, reason="no_candidates",
                         request_binding=binding, receipt=receipt)

    receipt_status = (receipt or {}).get("status", "error")
    receipt_reason = (receipt or {}).get("reason", ranking.status)
    return _proposal(status=receipt_status, outcome="baseline", baseline_order=baseline_ids,
                     jev_order=None, reason=str(receipt_reason),
                     request_binding=binding, receipt=receipt)


def _qualification_context(
    *,
    proposal: ShadowProposal,
    audit_rate: float,
    model: str | None = None,
) -> dict[str, Any]:
    receipt_model = (proposal.receipt or {}).get("model") if isinstance(proposal.receipt, dict) else None
    model_id = model or receipt_model or "unknown"
    policy = {
        "schema": "battle.jev_prejudge_policy.v1",
        "audit_rate": audit_rate,
        "score_authority": "judge_receipts_only",
        "false_negative_circuit_breaker": True,
    }
    question = {
        "question_id": proposal.question_id,
        "team": proposal.team,
    }
    catalog_hash = proposal.request_binding.get("catalog_hash") or _stable_digest(proposal.baseline_order)
    model_binding = {"model": model_id}
    return {
        "policy": policy,
        "question": question,
        "catalog_hash": catalog_hash,
        "model": model_binding,
        "policy_hash": _stable_digest(policy),
        "question_hash": _stable_digest(question),
        "catalog_hash_hash": _stable_digest(catalog_hash),
        "model_hash": _stable_digest(model_binding),
    }


def build_prejudge_qualification_receipt(
    *,
    proposal: ShadowProposal,
    audit_rate: float | None = None,
    model: str | None = None,
    max_false_negative_rate: float = 0.0,
    minimum_savings: float = 0.01,
    sample_size: int = 1,
) -> dict[str, Any]:
    """Build the exact activation receipt shape expected by the live gate."""
    rate = configured_prejudge_audit_rate() if audit_rate is None else audit_rate
    context = _qualification_context(proposal=proposal, audit_rate=rate, model=model)
    return {
        "schema": QUALIFICATION_SCHEMA,
        "status": "PASS",
        "created_at": _utc_now(),
        "policy_hash": context["policy_hash"],
        "question_hash": context["question_hash"],
        "catalog_hash": context["catalog_hash"],
        "catalog_hash_hash": context["catalog_hash_hash"],
        "model_hash": context["model_hash"],
        "max_false_negative_rate": max_false_negative_rate,
        "minimum_savings": minimum_savings,
        "sample_size": sample_size,
    }


def _qualification_status(
    *,
    qualification_path: Path | None,
    proposal: ShadowProposal,
    audit_rate: float,
    model: str | None,
) -> dict[str, Any]:
    if os.getenv("BATTLE_JEV_PREJUDGE_ACTIVE", "0") not in {"1", "true", "yes"}:
        return {"active": False, "reason": "shadow_only"}
    if qualification_path is None:
        configured = os.getenv("BATTLE_JEV_PREJUDGE_QUALIFICATION")
        qualification_path = Path(configured) if configured else None
    if qualification_path is None:
        return {"active": False, "reason": "qualification_missing"}
    try:
        qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"active": False, "reason": f"qualification_unreadable:{type(exc).__name__}"}
    if qualification.get("schema") != QUALIFICATION_SCHEMA or qualification.get("status") != "PASS":
        return {"active": False, "reason": "qualification_not_pass", "qualification": qualification}
    created = _parse_utc(str(qualification.get("created_at", "")))
    max_age = int(os.getenv("BATTLE_JEV_PREJUDGE_QUALIFICATION_MAX_AGE_SECONDS", str(DEFAULT_QUALIFICATION_MAX_AGE_SECONDS)))
    if created is None or time.time() - created > max_age:
        return {"active": False, "reason": "qualification_stale", "qualification": qualification}
    context = _qualification_context(proposal=proposal, audit_rate=audit_rate, model=model)
    expected = {
        "policy_hash": context["policy_hash"],
        "question_hash": context["question_hash"],
        "catalog_hash": context["catalog_hash"],
        "catalog_hash_hash": context["catalog_hash_hash"],
        "model_hash": context["model_hash"],
    }
    for key, value in expected.items():
        if qualification.get(key) != value:
            return {"active": False, "reason": f"qualification_mismatch:{key}", "qualification": qualification}
    max_fn = float(qualification.get("max_false_negative_rate", 1.0))
    min_savings = float(qualification.get("minimum_savings", 0.0))
    sample_size = int(qualification.get("sample_size", 0))
    if max_fn > float(os.getenv("BATTLE_JEV_PREJUDGE_MAX_FALSE_NEGATIVE_RATE", "0.0")):
        return {"active": False, "reason": "qualification_false_negative_bound"}
    if min_savings < float(os.getenv("BATTLE_JEV_PREJUDGE_MINIMUM_SAVINGS", "0.01")):
        return {"active": False, "reason": "qualification_savings_bound"}
    if sample_size < int(os.getenv("BATTLE_JEV_PREJUDGE_MIN_SAMPLE_SIZE", "1")):
        return {"active": False, "reason": "qualification_sample_size_bound"}
    return {
        "active": True,
        "reason": "qualification_pass",
        "qualification": qualification,
        "qualification_path": str(qualification_path),
        "qualification_sha256": _file_digest(qualification_path),
        **expected,
    }


def _sample_for_audit(*, seed: str, candidate_id: str, audit_rate: float) -> bool:
    value = int(hashlib.sha256(f"{seed}:{candidate_id}".encode("utf-8")).hexdigest()[:16], 16)
    return value / float(0xFFFFFFFFFFFFFFFF) < audit_rate


def _candidate_bypass_reason(candidate: dict[str, Any]) -> str | None:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    if candidate.get("pinned") is True or source.get("pinned") is True:
        return "pinned_candidate"
    if candidate.get("severity") == "critical" or source.get("severity") == "critical":
        return "critical_candidate"
    markers = {
        str(candidate.get("prior_evidence", "")).lower(),
        str(candidate.get("evidence_status", "")).lower(),
        str(source.get("prior_evidence", "")).lower(),
        str(source.get("evidence_status", "")).lower(),
        str(source.get("functional_evidence_status", "")).lower(),
    }
    if markers & {"failed", "fail", "contradictory", "conflict", "blocked"}:
        return "contradictory_or_failed_prior_evidence"
    return None


def route_prejudge_candidates(
    *,
    proposal: ShadowProposal,
    candidates: list[dict[str, Any]],
    output_path: Path,
    qualification_path: Path | None = None,
    audit_rate: float | None = None,
    model: str | None = None,
    fail_closed_reason: str | None = None,
) -> dict[str, Any]:
    """Write a hash-bound pre-Judge routing receipt.

    The receipt may defer candidates only when Jev is accepted AND the explicit
    qualification gate passes. Every other path routes FULL_JUDGE.
    """
    rate = configured_prejudge_audit_rate() if audit_rate is None else min(MAX_PREJUDGE_AUDIT_RATE, max(MIN_PREJUDGE_AUDIT_RATE, audit_rate))
    candidate_by_id = {str(c.get("id")): c for c in candidates if isinstance(c, dict) and c.get("id")}
    gate = _qualification_status(qualification_path=qualification_path, proposal=proposal, audit_rate=rate, model=model)
    active = bool(gate.get("active")) and proposal.status == "accepted" and fail_closed_reason is None
    if proposal.status != "accepted":
        inactive_reason = f"jev_{proposal.status}"
    elif fail_closed_reason:
        inactive_reason = fail_closed_reason
    else:
        inactive_reason = str(gate.get("reason", "qualification_missing"))
    full_ids = set(proposal.jev_order or []) if active else set()
    seed = _stable_digest({
        "battle_id": proposal.battle_id,
        "round_number": proposal.round_number,
        "team": proposal.team,
        "question_id": proposal.question_id,
        "candidate_hash": proposal.request_binding.get("candidate_hash"),
        "catalog_hash": proposal.request_binding.get("catalog_hash"),
    })
    decisions: list[dict[str, Any]] = []
    for candidate_id in proposal.baseline_order:
        candidate = candidate_by_id.get(candidate_id, {"id": candidate_id})
        bypass = _candidate_bypass_reason(candidate)
        sampled = _sample_for_audit(seed=seed, candidate_id=candidate_id, audit_rate=rate)
        if not active:
            route = "FULL_JUDGE"
            reason = inactive_reason
            audited = False
        elif bypass:
            route = "FULL_JUDGE"
            reason = bypass
            audited = False
        elif candidate_id in full_ids:
            route = "FULL_JUDGE"
            reason = "jev_full_judge"
            audited = False
        elif sampled:
            route = "FULL_JUDGE"
            reason = "deterministic_audit_sample"
            audited = True
        else:
            route = "DEFERRED_AUDIT"
            reason = "jev_deferred"
            audited = False
        decisions.append({
            "candidate_id": candidate_id,
            "route": route,
            "reason": reason,
            "audited_deferred": audited,
            "audit_false_negative": False,
            "source": candidate.get("source") or {},
            "candidate_hash": _stable_digest(candidate),
        })
    judge_requested = sum(1 for decision in decisions if decision["route"] == "FULL_JUDGE")
    receipt = {
        "schema": TRIAGE_SCHEMA,
        "status": "PASS",
        "battle_id": proposal.battle_id,
        "run_id": proposal.battle_id,
        "round_number": proposal.round_number,
        "team": proposal.team,
        "question_id": proposal.question_id,
        "active": active,
        "activation": gate,
        "shadow_status": proposal.status,
        "shadow_reason": proposal.reason,
        "proposal_hash": _stable_digest(proposal.to_dict()),
        "candidate_hash": proposal.request_binding.get("candidate_hash") or _stable_digest(candidates),
        "catalog_hash": proposal.request_binding.get("catalog_hash"),
        "audit_rate": rate,
        "audit_seed": seed,
        "decisions": decisions,
        "metrics": {
            "jev_latency_ms": (proposal.receipt or {}).get("latency_ms") if isinstance(proposal.receipt, dict) else None,
            "jev_cost": (proposal.receipt or {}).get("cost") if isinstance(proposal.receipt, dict) else None,
            "judge_calls_requested": judge_requested,
            "judge_calls_avoided": len(decisions) - judge_requested,
            "audit_sample_count": sum(1 for decision in decisions if decision["audited_deferred"]),
            "false_negatives": 0,
            "routing_outcome": "active" if active else "full_judge",
            "routing_reason": "qualification_pass" if active else inactive_reason,
        },
        "created_at": _utc_now(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def record_prejudge_audit_results(
    receipt_path: Path,
    confirmed_ids: set[str],
) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    false_negative_ids: list[str] = []
    for decision in receipt.get("decisions", []):
        if decision.get("audited_deferred") and decision.get("candidate_id") in confirmed_ids:
            decision["audit_false_negative"] = True
            false_negative_ids.append(decision["candidate_id"])
    receipt.setdefault("metrics", {})["false_negatives"] = len(false_negative_ids)
    receipt["audit_false_negative_candidate_ids"] = false_negative_ids
    if false_negative_ids:
        receipt["status"] = "FAIL_CLOSED"
        receipt["active_pruning_disabled_reason"] = "audit_false_negative"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt
