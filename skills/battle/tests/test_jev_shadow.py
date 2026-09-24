"""Fail-closed contract tests for the Battle shadow Jev advisory adapter.

Shadow authority boundary under test: the adapter may rank evidence candidates
advisorially, but a non-accepted/invalid/unavailable result must fall back to
the deterministic Battle baseline, no retry may occur, and Battle's
judge-only score authority is untouched.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from battle_skill import jev_shadow
from battle_skill.jev_shadow import ShadowProposal, propose_evidence_ranking
from battle_skill.orchestrator import BattleOrchestrator
from battle_skill.orchestrator_judge import wrap_blue_candidates
from battle_skill.state import AttackType, DefenseType, Finding, Patch


def _candidates() -> list[dict]:
    return [
        {"id": "fam-1", "text": "prior exploit family: command injection via CLI arg",
         "kind": "evidence", "source": {"lesson": "cmd-inj-lesson-1"}, "pinned": False,
         "freshness": "CURRENT"},
        {"id": "fam-2", "text": "patch hardening evidence: input validation patch",
         "kind": "evidence", "source": {"lesson": "defense-lesson-2"}, "pinned": False,
         "freshness": "unknown"},
        {"id": "fam-3", "text": "dormant research pointer, stale index",
         "kind": "evidence", "source": {}, "pinned": False, "freshness": "STALE"},
    ]


class _NoulTransport(httpx.AsyncBaseTransport):
    """Accepted-response transport: one noul answer per question."""

    def __init__(self, noul: float = 0.9, model: str = "jev-latest"):
        self.noul = noul
        self.model = model
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        answers = {q: {"type": "noul", "noul": self.noul} for q in body["questions"]}
        return httpx.Response(200, json={"model": self.model, "answers": answers, "usage": {}})


class _StatusTransport(httpx.AsyncBaseTransport):
    """Fixed-status transport for provider-failure paths."""

    def __init__(self, status: int, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, headers=self.headers)


def _factory(transport: httpx.AsyncBaseTransport):
    from jev_harness import Jev

    def factory(policy):
        return Jev(api_key="test-key", policy=policy,
                   http=httpx.AsyncClient(transport=transport))
    return factory


def _run(factory=None, candidates=None, monkey_env: dict | None = None, **kw) -> ShadowProposal:
    merged = dict(battle_id="battle-test-001", round_number=3, team="red",
                  question_id="red_evidence_ranking", task={"goal": "rank attack evidence"},
                  candidates=candidates if candidates is not None else _candidates())
    merged.update(kw)
    if monkey_env is not None:
        import os
        saved = {k: os.environ.get(k) for k in monkey_env}
        os.environ.update(monkey_env)
        try:
            return asyncio.run(propose_evidence_ranking(**merged))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return asyncio.run(propose_evidence_ranking(**merged))


def test_accepted_ranking_preserves_distinctions_and_binds_hashes() -> None:
    transport = _NoulTransport()
    proposal = _run(jev_factory=_factory(transport))
    assert transport.calls == 1
    assert proposal.status == "accepted" and proposal.outcome == "ranked"
    assert proposal.applied is False  # shadow: never applied, by construction
    assert set(proposal.jev_order) <= set(proposal.baseline_order)  # membership gate
    r = proposal.receipt
    assert r["schema_version"] == "jev.decision.v2"
    for key in ("request_hash", "state_hash", "questions_hash", "policy_hash"):
        assert r[key], key
    assert proposal.request_binding["candidate_hash"]
    assert proposal.request_binding["source_identity"]["fam-1"]
    # catalog_hash binds the exact normalized catalog sent to Jev; deterministic
    # over packet content and stable across two calls with identical candidates.
    catalog_hash = proposal.request_binding["catalog_hash"]
    assert isinstance(catalog_hash, str) and len(catalog_hash) == 64
    again = _run(jev_factory=_factory(_NoulTransport()))
    assert again.request_binding["catalog_hash"] == catalog_hash
    # catalog_hash changes when the catalog changes (freshness differs).
    changed = _candidates()
    changed[0]["freshness"] = "STALE"
    different = _run(jev_factory=_factory(_NoulTransport()), candidates=changed)
    assert different.request_binding["catalog_hash"] != catalog_hash
    assert proposal.battle_binding == {
        "battle_id": "battle-test-001", "round_number": 3,
        "team": "red", "question_id": "red_evidence_ranking",
    }
    assert proposal.to_dict()["schema"] == "battle.jev_shadow_proposal.v1"


def test_baseline_fallback_on_provider_429_without_retry() -> None:
    transport = _StatusTransport(429, headers={"Retry-After": "30"})
    proposal = _run(jev_factory=_factory(transport))
    assert transport.calls == 1  # exactly one call; no retry cascade
    assert proposal.status == "abstain" and proposal.outcome == "baseline"
    assert proposal.jev_order is None
    assert proposal.applied is False
    assert proposal.receipt["reason"] == "rate_limited"
    assert proposal.receipt["retry_after_ms"] > 0
    assert proposal.baseline_order == ["fam-1", "fam-2", "fam-3"]


def test_blocked_on_private_marker_never_reaches_transport() -> None:
    transport = _NoulTransport()
    cands = _candidates()
    cands[0]["text"] = "probe arena/private/actual leakage"
    proposal = _run(jev_factory=_factory(transport), candidates=cands)
    assert transport.calls == 0  # never transmitted
    assert proposal.status == "blocked" and proposal.outcome == "baseline"
    assert proposal.reason.startswith("private_marker:")


def test_malformed_and_nonmember_and_disabled_fallbacks() -> None:
    # candidate MISSING id entirely: typed error receipt, baseline-safe empty order
    p_missing = _run(candidates=[{"text": "x", "freshness": "CURRENT"}])
    assert p_missing.status == "error" and p_missing.outcome == "baseline"
    assert p_missing.reason == "invalid_candidate_id"
    assert p_missing.baseline_order == [] and p_missing.jev_order is None
    assert p_missing.applied is False
    # invalid (empty) id
    p1 = _run(candidates=[{"text": "x", "id": "", "freshness": "CURRENT"}])
    assert p1.status == "error" and p1.outcome == "baseline"
    assert p1.reason == "invalid_candidate_id"
    # duplicate ids
    dup = _candidates()
    dup[1]["id"] = "fam-1"
    p2 = _run(candidates=dup)
    assert p2.status == "error" and p2.reason == "duplicate_candidate_ids"
    # nonmember freshness value
    bad = _candidates()
    bad[0]["freshness"] = "MADE_UP"
    p3 = _run(candidates=bad)
    assert p3.status == "error" and p3.reason == "nonmember_freshness"
    # disabled via JEV_SHADOW=0 -> blocked, baseline preserved
    p4 = _run(monkey_env={"JEV_SHADOW": "0"})
    assert p4.status == "blocked" and p4.outcome == "baseline"
    assert p4.reason == "jev_disabled_or_no_api_key"
    assert p4.jev_order is None
    # exact-empty candidates -> no_match, no Jev call, empty outcome
    p5 = _run(candidates=[])
    assert p5.status == "no_match" and p5.outcome == "empty"
    assert p5.receipt is None and p5.baseline_order == []


def test_error_receipt_falls_back_to_baseline() -> None:
    transport = _StatusTransport(500)
    proposal = _run(jev_factory=_factory(transport))
    assert proposal.status == "error" and proposal.outcome == "baseline"
    assert proposal.jev_order is None
    assert proposal.applied is False
    assert proposal.receipt["reason"] == "provider_http_500"


def test_low_confidence_kept_and_cooldown_abstains_to_baseline() -> None:
    # noul 0.1 is a legitimate low-confidence ACCEPTED ranking; Jev keeps
    # mid/low-band candidates by design (retrieval order preserved on ties).
    transport = _NoulTransport(noul=0.1)
    proposal = _run(jev_factory=_factory(transport))
    assert transport.calls == 1
    assert proposal.status == "accepted"
    assert proposal.jev_order == ["fam-1", "fam-2", "fam-3"]
    # A second proposal reusing the SAME client instance lands inside the
    # provider cooldown set by a prior 429 and abstains WITHOUT a new transport hit.
    calls = {"n": 0}

    class OneCall429(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "60"})

    from jev_harness import Jev
    state = {"client": None}

    def reuse_factory(policy):
        if state["client"] is None:
            state["client"] = Jev(api_key="test-key", policy=policy,
                                  http=httpx.AsyncClient(transport=OneCall429()))
        return state["client"]

    first = _run(jev_factory=reuse_factory)
    second = _run(jev_factory=reuse_factory)
    assert calls["n"] == 1 and first.status == "abstain" and first.receipt["reason"] == "rate_limited"
    assert second.status == "abstain" and second.receipt["reason"] == "provider_cooldown"
    assert calls["n"] == 1  # cooldown abstain did not touch the transport again
    assert second.outcome == "baseline" and second.applied is False


def test_injected_factory_authorized_without_key_explicit_disable_still_blocks() -> None:
    # Repair pin: an explicitly injected bounded client is its own authorization;
    # it must not require JEV_API_KEY. Progressive: production callers (no
    # factory) without a key stay blocked, and JEV_SHADOW=0 blocks every path.
    transport = _NoulTransport()
    proposal = _run(jev_factory=_factory(transport), monkey_env={"JEV_API_KEY": ""})
    assert transport.calls == 1 and proposal.status == "accepted"
    disabled = _run(monkey_env={"JEV_SHADOW": "0"})
    assert disabled.status == "blocked" and disabled.reason == "jev_disabled_or_no_api_key"
    disabled_factory = _run(jev_factory=_factory(_NoulTransport()), monkey_env={"JEV_SHADOW": "0"})
    assert disabled_factory.status == "blocked"


def test_orchestrator_persists_red_and_blue_shadow_receipts(tmp_path: Path) -> None:
    orchestrator = BattleOrchestrator.__new__(BattleOrchestrator)
    orchestrator.battle_id = "battle-live-path"
    orchestrator.control_dir = tmp_path
    finding = Finding(
        id="finding-1", type=AttackType.AUDIT, severity="medium",
        description="command injection evidence", file_path="src/cli.py",
    )
    patch = Patch(
        id="patch-1", finding_id=finding.id, type=DefenseType.PATCH,
        diff="validate the command argument",
    )
    candidates = wrap_blue_candidates([patch], phase="reactive")
    red_path, blue_path = orchestrator._write_jev_shadow_receipts(
        round_num=1,
        findings=[finding],
        candidates=candidates,
        patches=[patch],
        jev_factory=_factory(_NoulTransport()),
    )
    red = json.loads(red_path.read_text())
    blue = json.loads(blue_path.read_text())
    assert red["status"] == blue["status"] == "accepted"
    assert red["applied"] is blue["applied"] is False
    assert red["baseline_order"] == [finding.id]
    assert blue["baseline_order"] == [candidates[0].candidate_id]
    assert red_path.parent.name == "round-0001"


def test_score_authority_untouched() -> None:
    # The shadow adapter exposes no scoring/selection/authorization functions.
    module_attrs = set(dir(jev_shadow))
    forbidden = {"score", "judge", "approve_spawn", "select_adaptive_winner", "authorize_target"}
    assert module_attrs & forbidden == set()
    # Battle's scorer still imports and derives from judge receipts.
    from battle_skill.scoring import Scorer  # noqa: F401
