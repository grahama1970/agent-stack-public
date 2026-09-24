#!/usr/bin/env python3
"""Compile a typed Persona Dream edit intent into a deterministic dependency-aware revision plan.

This command is the PD-UX-03 planning back end. It reads a typed edit intent
(contract ``persona_dream.edit_intent.v1``, see
contracts/persona_dream_edit_intent.v1.schema.json) naming one storyboard panel
frame target in one frozen revision, and compiles it against the revision's
artifact index plus the declared dependency graph
(contracts/persona_dream_revision_dependency_graph.v1.json).

Classification rules:

* ``regenerate`` — the targeted frame plus every downstream non-evidence
  derivation (storyboard packet, media lock) that transitively consumes it.
* ``stale`` — downstream evidence artifacts (frame generation receipts,
  provider packets) that documented the pre-edit state; retained as historical
  evidence, never silently reused.
* ``revalidate`` — upstream inputs of regenerate nodes that the edit does not
  change (sibling frames, untouched panels); they must be hash-reverified
  before any downstream manifest is rebuilt from them.
* ``reuse`` — everything else, reusable only with an index hash match.

Selective-execution claims are blocked when any index artifact has unknown
dependency coverage (matched by no graph rule): the plan cannot prove the edit
does not silently affect uncovered artifacts.

The execution boundary is ``planning_only``. This command writes exactly one
plan artifact and one receipt. It never activates a successor revision, never
performs provider work, and never writes persona memory. It reuses the shared
mechanisms of scripts/create_successor_revision.py (canonical hashing, atomic
writes, active-revision grounding from
scripts/reconstruct_upstream_contract_revision.py) without mutating that
historical qualified path.

Determinism: the plan body contains no wall clock, no randomness, and no
environment-dependent values; the plan_id is the SHA-256 of the canonical plan
body. The same frozen inputs produce byte-identical plan artifacts.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# Shared deterministic mechanisms reused from the qualified revision path
# (the same helpers create_successor_revision.py builds on).
from idea_lineage import canonical_json_bytes, read_object, sha256_file  # noqa: E402
from reconstruct_upstream_contract_revision import (  # noqa: E402
    ReconstructionBlocked,
    active_revision,
    atomic_write_json,
)

EDIT_INTENT_SCHEMA = SKILL_ROOT / "contracts" / "persona_dream_edit_intent.v1.schema.json"
DEFAULT_GRAPH = SKILL_ROOT / "contracts" / "persona_dream_revision_dependency_graph.v1.json"

PLAN_SCHEMA = "persona_dream.edit_intent_plan.v1"
RECEIPT_SCHEMA = "persona_dream.edit_intent_plan_receipt.v1"
PASS_STATUS = "PASS_EDIT_INTENT_PLANNED"

KNOWN_CONSTRAINTS = frozenset(
    {
        "identity_source_unchanged",
        "untouched_panels_reused_by_hash",
        "no_provider_submission",
        "historical_receipts_preserved",
        "target_only_no_downstream_propagation",
    }
)
OUT_OF_SCOPE_KINDS = frozenset({"identity_source_change"})


class PlanBlocked(RuntimeError):
    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None, exit_code: int = 2) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})
        self.exit_code = exit_code


def fail(code: str, **details: Any) -> None:
    raise PlanBlocked(code, details=details or None)


def load_edit_intent(path: Path) -> dict[str, Any]:
    if not path.is_file():
        fail("BLOCKED_EDIT_INTENT_MISSING", path=str(path))
    intent = read_object(path)
    # Typed pre-checks so common refusals carry their own codes instead of a
    # bare schema failure.
    boundary = intent.get("execution_boundary")
    if boundary is not None and boundary != "planning_only":
        fail("BLOCKED_EXECUTION_BOUNDARY", observed=boundary, required="planning_only")
    constraints = intent.get("preservation_constraints")
    if isinstance(constraints, list):
        unknown = sorted(set(constraints) - KNOWN_CONSTRAINTS)
        if unknown:
            fail("BLOCKED_UNKNOWN_PRESERVATION_CONSTRAINT", unknown=unknown)
    kind = (intent.get("requested_change") or {}).get("kind")
    if kind in OUT_OF_SCOPE_KINDS:
        fail(
            "BLOCKED_EDIT_OUT_OF_SCOPE_IDENTITY",
            observed=kind,
            note=(
                "identity truth changes flow through create_successor_revision.py; "
                "panel edit intents cannot target the identity source"
            ),
        )
    schema = json.loads(EDIT_INTENT_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(intent), key=lambda e: list(e.path))
    if errors:
        fail(
            "BLOCKED_EDIT_INTENT_SCHEMA",
            errors=[
                {"path": "/".join(str(p) for p in e.path), "message": e.message}
                for e in errors
            ],
        )
    return intent


def load_graph(path: Path) -> dict[str, Any]:
    graph = read_object(path)
    if graph.get("schema") != "persona_dream.revision_dependency_graph.v1":
        fail("BLOCKED_DEPENDENCY_GRAPH_SCHEMA", observed=graph.get("schema"))
    return graph


def match_rule(artifact_id: str, rules: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for rule in rules:
        if fnmatch.fnmatchcase(artifact_id, rule["match"]):
            return rule
    return None


def depends_closure(
    changed: set[str],
    edges: Mapping[str, list[str]],
) -> set[str]:
    """Artifacts that transitively depend on any node in ``changed``."""
    downstream: set[str] = set()
    frontier = list(changed)
    while frontier:
        node = frontier.pop()
        for artifact_id, deps in edges.items():
            if artifact_id in downstream or artifact_id in changed:
                continue
            if node in deps:
                downstream.add(artifact_id)
                frontier.append(artifact_id)
    return downstream


def verify_index_against_disk(revision_root: Path, index: Mapping[str, Any]) -> dict[str, str]:
    """Recompute every indexed hash from disk; the index is the stale-oracle."""
    observed: dict[str, str] = {}
    for artifact_id, entry in sorted(index["artifacts"].items()):
        rel = entry["relative_path"]
        path = revision_root / rel
        if not path.is_file():
            fail("BLOCKED_INDEX_ARTIFACT_MISSING", artifact_id=artifact_id, relative_path=rel)
        observed[artifact_id] = sha256_file(path)
        if observed[artifact_id] != entry["sha256"]:
            fail(
                "BLOCKED_REVISION_INDEX_STALE",
                artifact_id=artifact_id,
                relative_path=rel,
                index_sha256=entry["sha256"],
                observed_sha256=observed[artifact_id],
            )
    return observed


def compile_plan(
    intent: Mapping[str, Any],
    *,
    run_root: Path,
    graph_path: Path = DEFAULT_GRAPH,
) -> dict[str, Any]:
    graph = load_graph(graph_path)
    rules = graph["rules"]

    target = intent["target"]
    target_artifact_id = f"{target['panel_id']}.{target['artifact_role']}"

    active_id, _pointer = active_revision(run_root)
    if target["revision_id"] != active_id:
        fail(
            "BLOCKED_TARGET_REVISION_NOT_ACTIVE",
            target_revision_id=target["revision_id"],
            active_revision_id=active_id,
        )
    revision_root = run_root / ".persona-dream" / "revisions" / active_id
    if not revision_root.is_dir():
        fail("BLOCKED_REVISION_ROOT_MISSING", revision_root=str(revision_root))

    index_path = revision_root / "revision_artifact_index.json"
    if not index_path.is_file():
        fail("BLOCKED_REVISION_INDEX_MISSING", path=str(index_path))
    index = read_object(index_path)
    if index.get("schema") != "persona_dream.revision_artifact_index.v1":
        fail("BLOCKED_REVISION_INDEX_SCHEMA", observed=index.get("schema"))
    artifacts: dict[str, Any] = index["artifacts"]

    observed = verify_index_against_disk(revision_root, index)

    if target_artifact_id not in artifacts:
        fail("BLOCKED_TARGET_ARTIFACT_MISSING", artifact_id=target_artifact_id)
    expected = target["expected_sha256"]
    if expected != observed[target_artifact_id]:
        fail(
            "BLOCKED_STALE_TARGET",
            artifact_id=target_artifact_id,
            expected_sha256=expected,
            observed_sha256=observed[target_artifact_id],
        )

    # Coverage: every index artifact must be matched by a declared rule,
    # otherwise its dependency relationship to the edit is unknown.
    coverage: dict[str, str] = {}
    rule_for: dict[str, Mapping[str, Any]] = {}
    for artifact_id in artifacts:
        rule = match_rule(artifact_id, rules)
        rule_for[artifact_id] = rule  # type: ignore[assignment]
        coverage[artifact_id] = rule["id"] if rule else "unknown"
    unknown = sorted(a for a, c in coverage.items() if c == "unknown")

    def matches(pattern: str, artifact_id: str) -> bool:
        return fnmatch.fnmatchcase(artifact_id, pattern)

    # Downstream edges: artifact -> the artifacts it transitively depends on.
    edges: dict[str, list[str]] = {}
    for artifact_id, rule in rule_for.items():
        if rule is None:
            edges[artifact_id] = []
            continue
        deps = [d for d in rule.get("depends_on", [])]
        resolved: list[str] = []
        for dep in deps:
            resolved.extend(a for a in artifacts if matches(dep, a))
        edges[artifact_id] = sorted(set(resolved))

    changed = {target_artifact_id}
    downstream = depends_closure(changed, edges)

    # Upstream closure of the regenerate set (revalidate candidates).
    regenerate = set(changed)
    for artifact_id in downstream:
        if rule_for[artifact_id]["class"] == "evidence":
            continue
        regenerate.add(artifact_id)
    upstream_of_regen: set[str] = set()
    seen: set[str] = set(regenerate)
    frontier = list(regenerate)
    while frontier:
        node = frontier.pop()
        for dep in edges.get(node, []):
            if dep not in seen:
                seen.add(dep)
                upstream_of_regen.add(dep)
                frontier.append(dep)

    classification: dict[str, dict[str, str]] = {}
    for artifact_id in sorted(artifacts):
        rule = rule_for[artifact_id]
        if artifact_id in changed:
            classification[artifact_id] = {
                "class": "regenerate",
                "reason": "edit target; replaced in a successor revision by the planned change",
            }
        elif artifact_id in regenerate:
            classification[artifact_id] = {
                "class": "regenerate",
                "reason": f"downstream derivation consuming {sorted(set(edges[artifact_id]) & (changed | regenerate))}",
            }
        elif artifact_id in downstream:
            classification[artifact_id] = {
                "class": "stale",
                "reason": "downstream evidence documenting the pre-edit state; retained as historical evidence, never reused",
            }
        elif (
            artifact_id in upstream_of_regen
            and rule is not None
            and rule["class"] not in {"identity_source"}
        ):
            classification[artifact_id] = {
                "class": "revalidate",
                "reason": "unchanged input of a regenerate node; hash must be reverified before downstream rebuild",
            }
        else:
            classification[artifact_id] = {
                "class": "reuse",
                "reason": "not affected by the edit; reusable only with an index hash match",
            }
        classification[artifact_id]["relative_path"] = artifacts[artifact_id]["relative_path"]
        classification[artifact_id]["rule"] = rule["id"] if rule else "unknown"

    classes = ("regenerate", "revalidate", "stale", "reuse")
    classification_sets = {
        cls: sorted(a for a, c in classification.items() if c["class"] == cls) for cls in classes
    }

    constraints = list(intent["preservation_constraints"])
    contradictions: list[str] = []
    if "target_only_no_downstream_propagation" in constraints and any(
        classification[a]["class"] in {"regenerate", "stale"} and a not in changed
        for a in classification
    ):
        contradictions.append(
            "target_only_no_downstream_propagation contradicts the declared dependency graph: "
            f"{sorted(set(classification_sets['regenerate']) - changed)} propagate from the target"
        )
    if "identity_source_unchanged" in constraints and intent["requested_change"]["kind"] in OUT_OF_SCOPE_KINDS:
        contradictions.append("identity_source_unchanged contradicts the requested identity change")
    if contradictions:
        fail("BLOCKED_CONTRADICTORY_CONSTRAINTS", contradictions=contradictions)

    if "untouched_panels_reused_by_hash" in constraints:
        bad = [
            a
            for a in classification_sets["reuse"]
            if a.startswith("sb_") and observed[a] != artifacts[a]["sha256"]
        ]
        if bad:
            fail("BLOCKED_CONSTRAINT_UNSATISFIABLE", artifacts=bad)
    if "no_provider_submission" not in constraints:
        # Not a contradiction, but planning never submits; record the default.
        constraints = constraints + ["no_provider_submission"]

    selective_execution = {
        "claimed": not unknown,
        "blocked_by": [] if not unknown else ["BLOCKED_COVERAGE_UNKNOWN_DEPENDENCY"],
        "unknown_coverage_artifacts": unknown,
        "note": (
            "selective execution claims the regenerate/stale set is complete: "
            "everything else is reusable. Unknown dependency coverage makes that "
            "claim unprovable, so it stays false."
        ),
    }

    boundary = {
        "execution": "planning_only",
        "successor_activation": False,
        "provider_call_attempts": 0,
        "paid_submissions": 0,
        "memory_writes": 0,
    }

    plan = {
        "schema": PLAN_SCHEMA,
        "intent_id": intent["intent_id"],
        "target": {
            "run_id": target["run_id"],
            "revision_id": target["revision_id"],
            "panel_id": target["panel_id"],
            "artifact_role": target["artifact_role"],
            "artifact_id": target_artifact_id,
            "relative_path": artifacts[target_artifact_id]["relative_path"],
            "expected_sha256": expected,
            "observed_sha256": observed[target_artifact_id],
            "index_sha256": artifacts[target_artifact_id]["sha256"],
        },
        "requested_change": dict(intent["requested_change"]),
        "preservation_constraints": sorted(constraints),
        "classification": classification,
        "classification_sets": classification_sets,
        "classification_counts": {cls: len(ids) for cls, ids in classification_sets.items()},
        "selective_execution": selective_execution,
        "coverage": coverage,
        "dependency_graph": {
            "contract": str(graph_path.relative_to(SKILL_ROOT)) if _is_relative_to(graph_path, SKILL_ROOT) else str(graph_path),
            "sha256": sha256_file(graph_path),
        },
        "revision_index": {
            "path": str((revision_root / "revision_artifact_index.json").relative_to(run_root)),
            "sha256": sha256_file(index_path),
        },
        "lineage": {
            "source_revision_id": active_id,
            "successor_revision_id": None,
            "note": "planning only: no successor revision created or activated",
        },
        "boundary": boundary,
        "claims": {
            "proves": [
                "the edit intent is typed, grounded in the active frozen revision, and its target hash matches the artifact on disk",
                "every revision artifact is classified regenerate/revalidate/stale/reuse from declared dependencies",
                "the plan is deterministic: the same frozen inputs produce the same plan body",
            ],
            "does_not_prove": [
                "any artifact was regenerated, revalidated, or reused",
                "a successor revision exists or was activated",
                "selective execution is sound when dependency coverage is unknown",
                "any provider call, paid submission, or persona memory write occurred",
            ],
        },
        "live": True,
        "mocked": False,
        "actual_provider_call_attempts": 0,
    }
    body = canonical_json_bytes({k: v for k, v in plan.items() if k != "plan_id"})
    plan["plan_id"] = "editplan_" + hashlib.sha256(body).hexdigest()[:12]
    return plan


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def write_receipt(plan: Mapping[str, Any], plan_path: Path) -> dict[str, Any]:
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": PASS_STATUS,
        "plan_id": plan["plan_id"],
        "intent_id": plan["intent_id"],
        "plan_path": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
        "selective_execution_claimed": plan["selective_execution"]["claimed"],
        "classification_counts": plan["classification_counts"],
        "boundary": dict(plan["boundary"]),
        "source_revision_id": plan["lineage"]["source_revision_id"],
        "live": True,
        "mocked": False,
        "actual_provider_call_attempts": 0,
        "claims": plan["claims"],
    }
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", type=Path, required=True, help="edit intent JSON (persona_dream.edit_intent.v1)")
    parser.add_argument("--run-root", type=Path, required=True, help="run root holding .persona-dream state")
    parser.add_argument("--dependency-graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--output-dir", type=Path, default=None, help="plan directory; default .persona-dream/plans/<plan_id>")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        intent = load_edit_intent(args.intent)
        plan = compile_plan(intent, run_root=args.run_root.expanduser().resolve(), graph_path=args.dependency_graph.expanduser().resolve())
    except (PlanBlocked, ReconstructionBlocked) as exc:
        blocked = {
            "schema": "persona_dream.edit_intent_plan_blocked.v1",
            "status": "BLOCKED",
            "code": getattr(exc, "code", type(exc).__name__),
            "details": getattr(exc, "details", {}),
            "boundary": {
                "execution": "planning_only",
                "successor_activation": False,
                "provider_call_attempts": 0,
                "paid_submissions": 0,
                "memory_writes": 0,
            },
            "actual_provider_call_attempts": 0,
        }
        print(json.dumps(blocked, indent=2, sort_keys=True))
        return getattr(exc, "exit_code", 2)

    out_dir = args.output_dir or (args.run_root / ".persona-dream" / "plans" / plan["plan_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "edit_intent_plan.json"
    atomic_write_json(plan_path, plan)
    receipt = write_receipt(plan, plan_path)
    atomic_write_json(out_dir / "RECEIPT.json", receipt)
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"{receipt['status']} plan={plan['plan_id']} written={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
