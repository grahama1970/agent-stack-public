"""Fail-closed structural validator for Battle's canonical action catalog."""
from __future__ import annotations
import json, sys
from pathlib import Path

REQUIRED_ACTION_FIELDS = ("arguments", "evidence", "outcome_codes", "timing", "eligibility", "runtime", "judge")
MIN_VERSION = 2

def _classification_codes(path: Path) -> set[str]:
    data = json.loads((path.parent / "classification_codes.json").read_text())
    codes = {e.get("code") for e in data.get("codes", [])}
    assert None not in codes and len(codes) == len(data.get("codes", []))
    return codes

def _bounded_schema(s: object) -> bool:
    if not isinstance(s, dict) or s.get("type") not in {"string", "number", "integer", "boolean", "array", "object"}: return False
    t = s["type"]
    if t == "string": return isinstance(s.get("maxLength"), int) and s["maxLength"] >= 0
    if t == "array": return isinstance(s.get("maxItems"), int) and s["maxItems"] >= 0 and isinstance(s.get("items"), dict) and _bounded_schema(s["items"])
    if t == "object": return isinstance(s.get("maxProperties"), int) and s["maxProperties"] >= 0 and s.get("additionalProperties") is False and isinstance(s.get("properties"), dict) and bool(s["properties"]) and all(_bounded_schema(v) for v in s["properties"].values())
    return True

def eligible_action_ids(data: dict) -> list[str]:
    """Deterministic projection for catalog consumers; runtime dispatch remains unclaimed."""
    return sorted(n["id"] for n in data["nodes"] if n["kind"] == "action")

def validate(path: Path) -> dict:
    data=json.loads(path.read_text())
    assert data["schema"] == "battle.action_catalog.v2"
    nodes=data["nodes"]; ids={n["id"] for n in nodes}; by_id={n["id"]:n for n in nodes}
    assert "actions" not in data and len(ids)==len(nodes) and data["root_id"] in ids
    assert data["version"] >= MIN_VERSION and nodes[0]["id"] == data["root_id"]
    assert sum(n["kind"] == "root" for n in nodes) == 1 and sum(n["kind"] == "category" for n in nodes) >= 1
    assert len({(n["team"], n["name"]) for n in nodes}) == len(nodes)
    codes=_classification_codes(path)
    for n in nodes:
        p=n["parent_id"]; assert p is None or p in ids
        # The synthetic root is addressable but is not part of category paths.
        expected_path = ([n["id"].split(".")[-1]] if p == data["root_id"] else by_id[p]["family_path"] + [n["id"].split(".")[-1]] if p else [n["id"].split(".")[-1]])
        assert n["family_path"] == expected_path
        if n["kind"] == "action":
            assert all(f in n for f in REQUIRED_ACTION_FIELDS)
            a=n["arguments"]; assert isinstance(a,dict) and a.get("type")=="object" and a.get("maxProperties", -1) >= 0 and a.get("additionalProperties") is False and a.get("properties") and all(_bounded_schema(v) for v in a["properties"].values())
            assert set(a.get("required",[])) <= set(a["properties"])
            assert n["evidence"]["required"] is True and n["runtime"]["executor"] == "tau" and n["judge"] == {"replay_required":True,"authority":"judge"}
            assert set(n["outcome_codes"]) <= codes
        elif n["arguments"] != {}: assert False, n["id"]
        cur=n["id"]; seen=set()
        while cur is not None:
            assert cur not in seen, n["id"]; seen.add(cur); cur=by_id[cur]["parent_id"]
    names={n["name"] for n in nodes if n["kind"]=="action"}
    required={"research","scan","probe","exploit","fuzz","mutate","crossover","evade","spawn","abandon","monitor","detect","triage","harden","block","contain","isolate","revoke","kill","patch","rebuild","rollback","restore","regression","replay","wait","evidence","escalate","stop"}
    assert required <= names and not names & {"score","scoring","victory","release","promotion"}
    for n in nodes:
        for dep in n["eligibility"]["dependencies"]: assert dep in ids
    def deps(i, stack):
        assert i not in stack, i
        for d in by_id[i]["eligibility"]["dependencies"]: deps(d, stack|{i})
    for i in ids: deps(i,set())
    return {"status":"PASS","nodes":len(nodes),"actions":len(names),"categories":sum(n["kind"]=="category" for n in nodes)}
if __name__ == "__main__": print(json.dumps(validate(Path(sys.argv[1] if len(sys.argv)>1 else Path(__file__).parents[1]/"fixtures/action_catalog.json")),sort_keys=True))
