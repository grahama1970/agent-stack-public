#!/usr/bin/env python3
"""Apply a validated Persona Dream edit-intent plan by handing it to the sanctioned revision/Tau workflow.

This command is the PD-UX-05 application back end. It is the *only*
validation/execution path for consuming a PD-UX-03 edit-intent plan: the CLI
and the UI server route both run this script; the server never re-implements
the rules.

Boundary contract (fail-closed):

* Input is a plan produced by ``compile_edit_intent_plan.py`` plus its receipt.
  The plan is re-verified: ``plan_id`` is recomputed from the canonical body,
  ``plan_sha256`` must match the receipt, the planning boundary must be
  ``planning_only``, and the plan's target revision must still be the active
  frozen revision with an unchanged artifact index and dependency graph.
* A separate, explicitly approved authorization record
  (``persona_dream.edit_plan_authorization.v1``) binds the approval to the
  exact operation, plan id, plan/receipt/index/graph hashes, and target
  artifact + expected hash. Any changed plan invalidates the authorization
  (changed plans never inherit approval).
* Execution = handing the plan to the *existing* sanctioned revision/Tau
  repair workflow: a ``persona_dream.repair_work_order.v1`` work order, lease,
  Tau DAG contract, and queue item are written in the exact shapes the
  existing queue (server/src/repair.ts) and executor
  (``execute_local_pipeline_repair.py``) consume. No new orchestration engine
  is introduced and no repair step is run here.
* Interrupted execution is reconciled against the durable job record and the
  on-disk handoff artifacts before any retry: a timeout is NOT proof that no
  job exists, and ambiguous provider outcomes reconcile or block — never a
  blind retry. A recorded provider failure retains its evidence and blocks
  until a new authorization is supplied.
* Generated media are candidates only; no successor activation, no provider
  call, no paid submission, no public upload, and no persona-memory write ever
  happens here. Accepted history (the frozen source revision and unrelated
  accepted assets) is never mutated.

Simulated provider outcomes (``--record-outcome``) qualify integration and
recovery behavior only; they are never evidence of live generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from idea_lineage import canonical_json_bytes, read_object, sha256_file  # noqa: E402
from reconstruct_upstream_contract_revision import (  # noqa: E402
    ReconstructionBlocked,
    active_revision,
    atomic_write_json,
)

JOB_SCHEMA = "persona_dream.edit_plan_execution_job.v1"
AUTH_SCHEMA = "persona_dream.edit_plan_authorization.v1"
APPLICATION_SCHEMA = "persona_dream.edit_plan_application.v1"
BLOCKED_SCHEMA = "persona_dream.edit_plan_execution_blocked.v1"
PASS_STATUS = "PASS_EDIT_PLAN_HANDED_OFF"
OPERATION = "execute_edit_intent_plan"

RECORDABLE_OUTCOMES = ("HANDED_OFF_CONFIRMED", "PROVIDER_FAILURE", "PROVIDER_AMBIGUOUS", "TIMEOUT")
TERMINAL_STATES = ("HANDED_OFF", "FAILED", "AMBIGUOUS")

DEFAULT_GRAPH = SKILL_ROOT / "contracts" / "persona_dream_revision_dependency_graph.v1.json"


class ExecutionBlocked(RuntimeError):
    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None, exit_code: int = 2) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})
        self.exit_code = exit_code


def fail(code: str, **details: Any) -> None:
    raise ExecutionBlocked(code, details=details or None)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest_tree(root: Path) -> str:
    """Deterministic digest of a directory tree (relative paths + file hashes)."""
    sha = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        sha.update(str(path.relative_to(root)).encode())
        sha.update(sha256_file(path).encode())
    return "sha256:" + sha.hexdigest()


def job_path(run_root: Path, plan_id: str) -> Path:
    return run_root / ".persona-dream" / "jobs" / "edit_plans" / f"{plan_id}.json"


def handoff_paths(run_root: Path, plan_id: str, work_order_id: str) -> dict[str, Path]:
    repair = run_root / ".persona-dream" / "repair"
    tau_run = repair / "tau-runs" / work_order_id
    return {
        "work_order": repair / "work-orders" / f"{work_order_id}.json",
        "lease": repair / "leases" / f"editplan-{plan_id}.lease",
        "queue_item": repair / "tau-queue" / f"{work_order_id}.json",
        "dag_contract": tau_run / "dag-contract.json",
        "application": run_root / ".persona-dream" / "apply" / plan_id / "application_work_order.json",
    }


def read_job(run_root: Path, plan_id: str) -> dict[str, Any] | None:
    path = job_path(run_root, plan_id)
    if not path.is_file():
        return None
    return read_object(path)


def append_event(job: dict[str, Any], run_root: Path, kind: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    job = dict(job)
    job["events"] = [*job.get("events", []), {"at": utc_now(), "kind": kind, "detail": dict(detail)}]
    atomic_write_json(job_path(run_root, job["plan_id"]), job)
    return job


def load_plan(plan_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if plan_path.is_dir():
        plan_path = plan_path / "edit_intent_plan.json"
    if not plan_path.is_file():
        fail("BLOCKED_PLAN_MISSING", path=str(plan_path))
    plan = read_object(plan_path)
    if plan.get("schema") != "persona_dream.edit_intent_plan.v1":
        fail("BLOCKED_PLAN_SCHEMA", observed=plan.get("schema"))
    body = canonical_json_bytes({k: v for k, v in plan.items() if k != "plan_id"})
    recomputed = "editplan_" + hashlib.sha256(body).hexdigest()[:12]
    if plan.get("plan_id") != recomputed:
        fail("BLOCKED_PLAN_ID_MISMATCH", declared=plan.get("plan_id"), recomputed=recomputed)
    receipt_path = plan_path.parent / "RECEIPT.json"
    if not receipt_path.is_file():
        fail("BLOCKED_PLAN_RECEIPT_MISSING", path=str(receipt_path))
    receipt = read_object(receipt_path)
    if receipt.get("status") != "PASS_EDIT_INTENT_PLANNED":
        fail("BLOCKED_PLAN_NOT_VALIDATED", observed=receipt.get("status"))
    if receipt.get("plan_id") != plan["plan_id"]:
        fail("BLOCKED_PLAN_RECEIPT_MISMATCH", receipt_plan_id=receipt.get("plan_id"))
    if receipt.get("plan_sha256") != sha256_file(plan_path):
        fail(
            "BLOCKED_PLAN_SHA256_MISMATCH",
            receipt_sha256=receipt.get("plan_sha256"),
            observed_sha256=sha256_file(plan_path),
        )
    if plan["boundary"].get("execution") != "planning_only":
        fail("BLOCKED_PLAN_BOUNDARY", observed=plan["boundary"].get("execution"))
    return plan, receipt


def load_authorization(path: Path, plan: dict[str, Any], run_root: Path) -> dict[str, Any]:
    if not path.is_file():
        fail("BLOCKED_AUTHORIZATION_MISSING", path=str(path))
    auth = read_object(path)
    if auth.get("schema") != AUTH_SCHEMA:
        fail("BLOCKED_AUTHORIZATION_SCHEMA", observed=auth.get("schema"))
    if auth.get("operation") != OPERATION:
        fail("BLOCKED_AUTHORIZATION_OPERATION", observed=auth.get("operation"), required=OPERATION)
    if not auth.get("approved_by"):
        fail("BLOCKED_AUTHORIZATION_UNSIGNED")
    target = plan["target"]

    active_id, _pointer = active_revision(run_root)
    if target["revision_id"] != active_id:
        fail("BLOCKED_TARGET_REVISION_NOT_ACTIVE", target_revision_id=target["revision_id"], active_revision_id=active_id)
    revision_root = run_root / ".persona-dream" / "revisions" / active_id
    index_path = revision_root / "revision_artifact_index.json"
    graph = Path(plan["dependency_graph"]["contract"])
    if not graph.is_absolute():
        graph = SKILL_ROOT / graph
    observed = {
        "plan_id": plan["plan_id"],
        "plan_sha256": sha256_file(plan_path_for(plan)),
        "payload_sha256": "sha256:" + hashlib.sha256(
            canonical_json_bytes({k: v for k, v in plan.items() if k != "plan_id"})
        ).hexdigest(),
        "revision_index_sha256": sha256_file(index_path),
        "dependency_graph_sha256": sha256_file(graph),
        "target_artifact_id": target["artifact_id"],
        "target_expected_sha256": target["expected_sha256"],
    }
    mismatches = {key: {"authorized": auth.get(key), "observed": value} for key, value in observed.items() if auth.get(key) != value}
    if mismatches:
        fail("BLOCKED_AUTHORIZATION_PLAN_CHANGED", mismatches=mismatches)
    return auth


_plan_path_cache: dict[str, Path] = {}


def plan_path_for(plan: Mapping[str, Any]) -> Path:
    return _plan_path_cache[plan["plan_id"]]


def verify_plan_grounding(plan: dict[str, Any], run_root: Path) -> None:
    """The revision the plan was compiled against must still be active and unchanged."""
    active_id, _pointer = active_revision(run_root)
    if plan["target"]["revision_id"] != active_id:
        fail("BLOCKED_TARGET_REVISION_NOT_ACTIVE", target_revision_id=plan["target"]["revision_id"], active_revision_id=active_id)
    revision_root = run_root / ".persona-dream" / "revisions" / active_id
    index_path = revision_root / "revision_artifact_index.json"
    if sha256_file(index_path) != plan["revision_index"]["sha256"]:
        fail("BLOCKED_REVISION_INDEX_CHANGED", plan_sha256=plan["revision_index"]["sha256"], observed=sha256_file(index_path))
    index = read_object(index_path)
    artifact = index["artifacts"].get(plan["target"]["artifact_id"])
    if not artifact:
        fail("BLOCKED_TARGET_ARTIFACT_MISSING", artifact_id=plan["target"]["artifact_id"])
    observed_hash = sha256_file(revision_root / artifact["relative_path"])
    if observed_hash != plan["target"]["expected_sha256"]:
        fail("BLOCKED_STALE_TARGET", expected_sha256=plan["target"]["expected_sha256"], observed_sha256=observed_hash)


def handoff_intact(run_root: Path, plan_id: str, work_order_id: str, application_sha256: str) -> bool:
    paths = handoff_paths(run_root, plan_id, work_order_id)
    try:
        return (
            paths["work_order"].is_file()
            and paths["queue_item"].is_file()
            and paths["application"].is_file()
            and sha256_file(paths["application"]) == application_sha256
        )
    except OSError:
        return False


def phase_of(relative_path: str) -> str:
    # phase_07_storyboard_live_tau/... -> "07"
    head = relative_path.split("/", 1)[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    return digits.zfill(2) if digits else "00"


def write_handoff(run_root: Path, plan: dict[str, Any], auth: dict[str, Any], job: dict[str, Any]) -> dict[str, Path]:
    """Delegate to the existing sanctioned revision/Tau repair workflow.

    Writes the same shapes server/src/repair.ts enqueues: a
    persona_dream.repair_work_order.v1 work order, lease, Tau DAG contract
    (pointing at execute_local_pipeline_repair.py), queue item — plus the
    application record binding the plan. Nothing here executes a repair step.
    """
    plan_id = plan["plan_id"]
    work_order_id = f"editplan-{plan_id}"
    paths = handoff_paths(run_root, plan_id, work_order_id)
    if paths["work_order"].exists() or paths["lease"].exists():
        fail("BLOCKED_HANDOFF_ALREADY_PRESENT", work_order=str(paths["work_order"]))

    stale = plan["classification_sets"]["stale"]
    regenerate = plan["classification_sets"]["regenerate"]
    affected = sorted(set(stale) | set(regenerate))
    phases = sorted({phase_of(plan["classification"][a]["relative_path"]) for a in affected})

    source_digest = digest_tree(run_root / ".persona-dream" / "revisions" / plan["target"]["revision_id"])

    application = {
        "schemaVersion": APPLICATION_SCHEMA,
        "plan_id": plan_id,
        "plan_sha256": sha256_file(plan_path_for(plan)),
        "authorization_sha256": sha256_file(Path(auth["_path"])),
        "work_order_id": work_order_id,
        "run_id": plan["target"]["run_id"],
        "source_revision_id": plan["target"]["revision_id"],
        "target": dict(plan["target"]),
        "requested_change": dict(plan["requested_change"]),
        "preservation_constraints": list(plan["preservation_constraints"]),
        "classification_counts": dict(plan["classification_counts"]),
        "regenerate_artifacts": [
            {"artifact_id": a, "relative_path": plan["classification"][a]["relative_path"]} for a in regenerate
        ],
        "stale_artifacts_retained_as_history": [
            {"artifact_id": a, "relative_path": plan["classification"][a]["relative_path"]} for a in stale
        ],
        "policy": {
            "candidate_media_only": True,
            "no_successor_activation": True,
            "accepted_history_preserved": True,
            "replacement_only_via_existing_review_and_promotion_path": True,
            "forbidden_side_effects": [
                "external_network",
                "public_media_publication",
                "paid_provider_call",
                "provider_submission",
                "persona_memory_write",
                "human_acceptance_fabrication",
            ],
            "stop_before_phase": "11",
            "max_attempts_per_phase": 3,
        },
        "executor": {
            "workflow": "existing sanctioned revision/Tau repair workflow",
            "script": "scripts/execute_local_pipeline_repair.py",
            "note": "this application record only queues the work; no repair step runs here",
        },
        "source_revision_digest": source_digest,
        "created_at": utc_now(),
        "status": "REQUESTED",
    }
    atomic_write_json(paths["application"], application)

    work_order = {
        "schemaVersion": "persona_dream.repair_work_order.v1",
        "workOrderId": work_order_id,
        "dedupKey": f"editplan-{plan_id}",
        "runId": plan["target"]["run_id"],
        "runRoot": str(run_root),
        "sourceRevisionId": plan["target"]["revision_id"],
        "targetRevisionId": f"rev_editplan_{plan_id.removeprefix('editplan_')}",
        "createdAt": utc_now(),
        "detection": {
            "earliestStalePhase": phases[0] if phases else "07",
            "selectedThroughPhase": "10",
            "phaseRange": phases or ["07"],
            "reasons": [
                {"phaseId": phase_of(plan["classification"][a]["relative_path"]), "code": f"edit_plan_{plan['classification'][a]['class']}"}
                for a in affected
            ],
        },
        "policy": dict(application["policy"]),
        "application_record": str(paths["application"].relative_to(run_root)),
        "status": "REQUESTED",
    }
    atomic_write_json(paths["work_order"], work_order)

    paths["lease"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["lease"] / "lease.json", {
        "schemaVersion": "persona_dream.repair_lease.v1",
        "dedupKey": work_order["dedupKey"],
        "workOrderId": work_order_id,
        "ownerId": f"edit-plan-executor-{os.getpid()}",
        "acquiredAt": work_order["createdAt"],
        "sourceRevisionId": work_order["sourceRevisionId"],
    })

    # Tau DAG contract in the existing sanctioned shape; commands point at the
    # existing executor script, this command only writes the contract.
    command_specs = paths["dag_contract"].parent / "command-specs"
    skill_root = SKILL_ROOT
    repair_script = skill_root / "scripts" / "execute_local_pipeline_repair.py"
    source_manifest = run_root / "repair_sources.v1.json"
    uv_command = ["uv", "run", "--project", str(skill_root), "python", str(repair_script)]
    for role in ("installer", "reviewer"):
        atomic_write_json(command_specs / f"repair-{role}" / "tau-dispatch-command.json", {
            "command": [*uv_command, "install" if role == "installer" else "review", "--work-order", str(paths["work_order"]), "--sources", str(source_manifest), "--tau-handoff"],
            "timeout_s": 300,
        })
    atomic_write_json(paths["dag_contract"], {
        "schema": "tau.dag_contract.v1",
        "dag_id": f"persona-dream-{work_order_id}",
        "goal": {
            "goal_id": f"persona-dream-{work_order['runId']}-edit-plan",
            "goal_version": 1,
            "goal_hash": f"sha256:{hashlib.sha256(canonical_json_bytes({'plan_id': plan_id, 'plan_sha256': application['plan_sha256']})).hexdigest()}",
        },
        "target": {
            "repo": "grahama1970/agent-skills",
            "target": str(run_root),
            "allowed_paths": [
                f"{run_root}/.persona-dream/revisions/**",
                f"{run_root}/.persona-dream/repair/**",
                f"{run_root}/.persona-dream/state/**",
            ],
        },
        "entry_node": "repair-installer",
        "terminal_nodes": ["human"],
        "limits": {"resume": True, "default_timeout_seconds": 300, "max_total_attempts": 3},
        "nodes": [
            {"id": "repair-installer", "agent": "coder", "executor": "local", "max_attempts": 1,
             "command_spec": str(command_specs / "repair-installer" / "tau-dispatch-command.json"),
             "required_evidence": ["repair_revision_manifest"]},
            {"id": "repair-reviewer", "agent": "reviewer", "executor": "local", "max_attempts": 1,
             "command_spec": str(command_specs / "repair-reviewer" / "tau-dispatch-command.json"),
             "required_evidence": ["revision_promotion_receipt"]},
            {"id": "human", "agent": "human", "executor": "human"},
        ],
        "edges": [
            {"from": "repair-installer", "to": "repair-reviewer"},
            {"from": "repair-reviewer", "to": "human"},
        ],
        "required_evidence": ["repair_revision_manifest", "revision_promotion_receipt"],
        "fail_closed_on": [
            "goal_hash_mismatch", "target_changed", "unexpected_node", "unexpected_edge",
            "missing_required_evidence", "max_attempts_exceeded", "malformed_handoff",
            "branch_target_divergence", "branch_goal_hash_divergence",
        ],
    })
    atomic_write_json(paths["queue_item"], {
        "schemaVersion": "persona_dream.tau_repair_queue_item.v1",
        "status": "QUEUED",
        "queuedAt": utc_now(),
        "workOrderPath": str(paths["work_order"]),
        "dagContractPath": str(paths["dag_contract"]),
        "workOrder": work_order,
    })
    return paths


def reconcile(run_root: Path, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a non-terminal job from durable records before any retry.

    Returns a terminal result dict when reconciliation completes the job, or
    None when execution may proceed (no prior job / fully reconciled fresh).
    Raises ExecutionBlocked when the prior state must not be retried blind.
    """
    plan_id = plan["plan_id"]
    state = job.get("state")
    work_order_id = job.get("work_order_id") or f"editplan-{plan_id}"
    application_sha256 = (job.get("handoff") or {}).get("application_sha256", "")
    intact = handoff_intact(run_root, plan_id, work_order_id, application_sha256) if application_sha256 else False

    if state == "HANDED_OFF":
        return {"status": PASS_STATUS, "plan_id": plan_id, "state": "HANDED_OFF", "reconciled": True,
                "note": "prior handoff intact; idempotent no-op", "job": job}

    if state in {"EXECUTING", "AMBIGUOUS"}:
        # A timeout or crash is NOT proof that no job exists: reconcile from
        # the on-disk handoff artifacts first.
        if intact:
            resolved = dict(job, state="HANDED_OFF")
            resolved = append_event(resolved, run_root, "reconciled", {
                "from_state": state, "basis": "handoff artifacts present with matching hashes",
            })
            return {"status": PASS_STATUS, "plan_id": plan_id, "state": "HANDED_OFF", "reconciled": True,
                    "note": f"reconciled interrupted {state} job from durable handoff artifacts", "job": resolved}
        outcome = next((e["detail"].get("outcome") for e in reversed(job.get("events", [])) if e["kind"] == "outcome"), None)
        if outcome == "PROVIDER_AMBIGUOUS" or outcome == "TIMEOUT" or state == "AMBIGUOUS":
            fail("BLOCKED_AMBIGUOUS_EXECUTION", plan_id=plan_id, prior_state=state,
                 recorded_outcome=outcome,
                 note="provider outcome ambiguous: reconcile or obtain a human disposition; never blind-retry")
        if outcome == "PROVIDER_FAILURE":
            fail("BLOCKED_PREVIOUS_EXECUTION_FAILURE", plan_id=plan_id, evidence_events=[
                e for e in job.get("events", []) if e["kind"] == "outcome"])
        # EXECUTING with no handoff artifacts and no outcome: the process died
        # mid-write; the handoff never became durable. Safe to redo? No — the
        # queued work order may exist partially; block pending inspection.
        fail("BLOCKED_INTERRUPTED_EXECUTION_UNRESOLVED", plan_id=plan_id, prior_state=state,
             note="job record claims execution but no durable handoff; inspect before retry")

    if state == "FAILED":
        fail("BLOCKED_PREVIOUS_EXECUTION_FAILURE", plan_id=plan_id,
             evidence_events=[e for e in job.get("events", []) if e["kind"] == "outcome"])

    fail("BLOCKED_UNKNOWN_JOB_STATE", plan_id=plan_id, observed_state=state)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    run_root: Path = args.run_root.expanduser().resolve()
    plan_path: Path = args.plan.expanduser().resolve()
    if plan_path.is_dir():
        plan_path = plan_path / "edit_intent_plan.json"
    plan, receipt = load_plan(plan_path)
    _plan_path_cache[plan["plan_id"]] = plan_path
    verify_plan_grounding(plan, run_root)
    auth = load_authorization(args.authorization.expanduser().resolve(), plan, run_root)
    auth["_path"] = str(args.authorization.expanduser().resolve())

    existing = read_job(run_root, plan["plan_id"])
    if existing is not None:
        if sha256_file(Path(auth["_path"])) != existing.get("authorization_sha256"):
            fail("BLOCKED_JOB_AUTHORIZATION_MISMATCH",
                 note="a new authorization cannot silently take over an existing job; clear the job record explicitly")
        result = reconcile(run_root, plan, existing)
        if result is not None:
            return result

    job = {
        "schema": JOB_SCHEMA,
        "job_id": f"job_{plan['plan_id']}",
        "plan_id": plan["plan_id"],
        "run_root": str(run_root),
        "source_revision_id": plan["target"]["revision_id"],
        "authorization_sha256": sha256_file(Path(auth["_path"])),
        "state": "EXECUTING",
        "work_order_id": None,
        "handoff": None,
        "boundary": {
            "provider_call_attempts": 0,
            "paid_submissions": 0,
            "public_uploads": 0,
            "memory_writes": 0,
            "successor_activation": False,
            "candidate_media_only": True,
        },
        "events": [{"at": utc_now(), "kind": "authorized", "detail": {
            "operation": OPERATION, "approved_by": auth.get("approved_by"),
            "plan_sha256": sha256_file(plan_path),
        }}],
    }
    atomic_write_json(job_path(run_root, plan["plan_id"]), job)

    paths = write_handoff(run_root, plan, auth, job)
    application_sha256 = sha256_file(paths["application"])
    job = append_event(job, run_root, "handoff", {
        "work_order": str(paths["work_order"].relative_to(run_root)),
        "queue_item": str(paths["queue_item"].relative_to(run_root)),
        "dag_contract": str(paths["dag_contract"].relative_to(run_root)),
        "application": str(paths["application"].relative_to(run_root)),
        "application_sha256": application_sha256,
        "queue_item_sha256": sha256_file(paths["queue_item"]),
        "work_order_sha256": sha256_file(paths["work_order"]),
    })
    job = dict(job)
    job["state"] = "HANDED_OFF"
    job["work_order_id"] = f"editplan-{plan['plan_id']}"
    job["handoff"] = {"application_sha256": application_sha256}
    atomic_write_json(job_path(run_root, plan["plan_id"]), job)

    return {
        "status": PASS_STATUS,
        "plan_id": plan["plan_id"],
        "job_id": job["job_id"],
        "state": "HANDED_OFF",
        "reconciled": False,
        "work_order": str(paths["work_order"].relative_to(run_root)),
        "queue_item": str(paths["queue_item"].relative_to(run_root)),
        "application": str(paths["application"].relative_to(run_root)),
        "classification_counts": plan["classification_counts"],
        "boundary": job["boundary"],
        "claims": {
            "proves": [
                "a hash-bound authorization for the exact plan/inputs was consumed and the plan was handed to the existing sanctioned revision/Tau repair queue",
                "the frozen source revision and unrelated accepted assets are unchanged by the handoff",
                "interrupted or ambiguous prior executions reconcile from durable records or block, never blind-retry",
            ],
            "does_not_prove": [
                "that any artifact was regenerated, accepted, or activated",
                "that any provider call or paid submission occurred",
                "live generation: simulated provider failures qualify integration and recovery only",
            ],
        },
        "live": False,
        "mocked": False,
        "actual_provider_call_attempts": 0,
        "job": job,
    }


def record_outcome(args: argparse.Namespace) -> dict[str, Any]:
    run_root: Path = args.run_root.expanduser().resolve()
    plan_id = args.plan_id
    job = read_job(run_root, plan_id)
    if job is None:
        fail("BLOCKED_JOB_RECORD_MISSING", plan_id=plan_id)
    if job.get("state") in TERMINAL_STATES and args.record_outcome == "HANDED_OFF_CONFIRMED":
        return {"status": "NOOP_ALREADY_HANDOFF", "plan_id": plan_id, "state": job["state"]}
    simulated = args.record_outcome in {"PROVIDER_FAILURE", "PROVIDER_AMBIGUOUS", "TIMEOUT"}
    new_state = {
        "HANDED_OFF_CONFIRMED": "HANDED_OFF",
        "PROVIDER_FAILURE": "FAILED",
        "PROVIDER_AMBIGUOUS": "AMBIGUOUS",
        "TIMEOUT": "AMBIGUOUS",
    }[args.record_outcome]
    job = append_event(dict(job, state=new_state), run_root, "outcome", {
        "outcome": args.record_outcome,
        "simulated": simulated,
        "note": "simulated provider outcome: qualifies integration/recovery only; never live-generation proof" if simulated else "downstream workflow confirmed the handoff",
    })
    return {
        "status": "RECORDED", "plan_id": plan_id, "outcome": args.record_outcome,
        "state": new_state, "simulated": simulated,
        "job_path": str(job_path(run_root, plan_id).relative_to(run_root)),
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    run_root: Path = args.run_root.expanduser().resolve()
    job = read_job(run_root, args.plan_id)
    if job is None:
        fail("BLOCKED_JOB_RECORD_MISSING", plan_id=args.plan_id)
    return {
        "schema": "persona_dream.edit_plan_job_status.v1",
        "plan_id": args.plan_id,
        "state": job.get("state"),
        "work_order_id": job.get("work_order_id"),
        "handoff": job.get("handoff"),
        "events": job.get("events", []),
        "boundary": job.get("boundary"),
        "read_only": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="run root holding .persona-dream state")
    sub = parser.add_mutually_exclusive_group()
    sub.add_argument("--execute", action="store_true", default=True, help="validate + reconcile + hand off (default)")
    sub.add_argument("--record-outcome", choices=RECORDABLE_OUTCOMES, help="record a downstream (possibly simulated) outcome into the job record")
    sub.add_argument("--status", action="store_true", help="read-only job status")
    parser.add_argument("--plan", type=Path, help="plan dir or edit_intent_plan.json path (execute mode)")
    parser.add_argument("--plan-id", help="plan id (status / record-outcome modes)")
    parser.add_argument("--authorization", type=Path, help="authorization record (execute mode)")
    args = parser.parse_args(argv)

    try:
        if args.status:
            if not args.plan_id:
                fail("BLOCKED_PLAN_ID_REQUIRED")
            result = status(args)
        elif args.record_outcome:
            if not args.plan_id:
                fail("BLOCKED_PLAN_ID_REQUIRED")
            result = record_outcome(args)
        else:
            if not args.plan or not args.authorization:
                fail("BLOCKED_ARGUMENTS_REQUIRED", required=["--plan", "--authorization"])
            result = execute(args)
    except (ExecutionBlocked, ReconstructionBlocked) as exc:
        blocked = {
            "schema": BLOCKED_SCHEMA,
            "status": "BLOCKED",
            "code": getattr(exc, "code", type(exc).__name__),
            "details": getattr(exc, "details", {}) or {},
            "boundary": {
                "provider_call_attempts": 0,
                "paid_submissions": 0,
                "public_uploads": 0,
                "memory_writes": 0,
                "successor_activation": False,
            },
            "actual_provider_call_attempts": 0,
        }
        print(json.dumps(blocked, indent=2, sort_keys=True))
        return getattr(exc, "exit_code", 2)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
