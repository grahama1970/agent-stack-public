"""Fail-closed Battle action catalog consumer; Tau is the only dispatch boundary."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

class TauExecutor(Protocol):
    def dispatch(self, action_id: str, arguments: dict[str, Any], profile: str) -> Any: ...

@dataclass(frozen=True)
class Catalog:
    raw: dict[str, Any]
    digest: str
    actions: dict[str, dict[str, Any]]

@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    action_id: str | None = None
    receipt: Any = None

NON_ACCEPTED = {"abstain", "blocked", "error", "no_match"}

def load_catalog(path: str | Path) -> Catalog:
    data = Path(path).read_bytes()
    raw = json.loads(data)
    actions = {n["id"]: n for n in raw["nodes"] if n.get("kind") == "action"}
    return Catalog(raw, hashlib.sha256(data).hexdigest(), actions)

def eligible_projection(catalog: Catalog, is_eligible: Callable[[dict[str, Any]], bool] | None = None) -> tuple[str, ...]:
    check = is_eligible or (lambda n: True)
    return tuple(sorted(i for i, n in catalog.actions.items() if check(n)))

def _schema(value: Any, schema: dict[str, Any]) -> bool:
    typ = schema.get("type")
    if typ == "object":
        return isinstance(value, dict) and all(k in value and k in schema.get("properties", {}) and _schema(value[k], schema["properties"][k]) for k in schema.get("required", [])) and not (set(value) - set(schema.get("properties", {}))) and len(value) <= schema.get("maxProperties", 10**9) and all(_schema(v, schema["properties"][k]) for k,v in value.items())
    if typ == "array": return isinstance(value, list) and len(value) <= schema.get("maxItems", 10**9) and all(_schema(v, schema.get("items", {})) for v in value)
    if typ == "string": return isinstance(value, str) and len(value) <= schema.get("maxLength", 10**9)
    if typ == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number": return isinstance(value, (int,float)) and not isinstance(value, bool)
    if typ == "boolean": return isinstance(value, bool)
    return True

def _checks(node: dict[str, Any], args: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    if node["eligibility"].get("authorization") != ctx.get("authorization_schema"): return "authorization_schema"
    if ctx.get("target_state") not in node["eligibility"].get("target_state", []): return "target_state"
    if not set(node["eligibility"].get("dependencies", [])) <= set(ctx.get("dependencies", [])): return "dependencies"
    if ctx.get("budget", 0) < node["cost"]["budget_units"]: return "budget"
    if ctx.get("capacity", 0) < node["cost"]["capacity_units"]: return "capacity"
    if ctx.get("cooldown", False): return "cooldown"
    if ctx.get("isolation_profile") not in node["runtime"]["profiles"]: return "isolation_profile"
    if not _schema(args, node["arguments"]): return "arguments"
    return None

def consume(catalog: Catalog, proposal: dict[str, Any], ctx: dict[str, Any], executor: TauExecutor | None = None, fallback: dict[str, Any] | None = None) -> Decision:
    state = proposal.get("state")
    if state != "accepted":
        if fallback is not None and state == "no_match":
            proposal = fallback
            state = proposal.get("state")
        else:
            if state in NON_ACCEPTED: return Decision(state, "jev_state")
            return Decision("error", "invalid_jev_state")
    if state != "accepted": return Decision(state if state in NON_ACCEPTED else "error", "fallback_not_accepted")
    selected = proposal.get("action_id")
    if selected not in catalog.actions: return Decision("error", "unknown_or_structural_action")
    node = catalog.actions[selected]
    if not proposal.get("authorization") == node["eligibility"]["authorization"]: return Decision("error", "authorization_mismatch", selected)
    if hashlib.sha256(ctx.get("catalog_bytes", b"")).hexdigest() != catalog.digest: return Decision("blocked", "stale_catalog_hash", selected)
    reason = _checks(node, proposal.get("arguments", {}), ctx)
    if reason: return Decision("blocked", reason, selected)
    # A separate, fresh snapshot is mandatory immediately before the sole dispatch call.
    snapshot = ctx.get("revalidate")
    if not isinstance(snapshot, dict): return Decision("blocked", "pre_dispatch_revalidation_required", selected)
    if snapshot.get("freshness") is not True: return Decision("blocked", "pre_dispatch_stale_freshness", selected)
    expected_identity = ctx.get("state_identity", proposal.get("state_identity"))
    actual_identity = snapshot.get("state_identity")
    expected_hash = ctx.get("state_hash", proposal.get("state_hash"))
    if expected_identity is not None and actual_identity != expected_identity and not (expected_hash is not None and snapshot.get("state_hash") == expected_hash):
        return Decision("blocked", "pre_dispatch_state_mismatch", selected)
    if (reason := _checks(node, proposal.get("arguments", {}), snapshot)): return Decision("blocked", "pre_dispatch_" + reason, selected)
    if executor is None: return Decision("blocked", "tau_executor_required", selected)
    receipt = executor.dispatch(selected, proposal["arguments"], snapshot["isolation_profile"])
    return Decision("accepted", "dispatched_to_tau", selected, receipt)
