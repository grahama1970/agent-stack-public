import json
from pathlib import Path
from battle_skill.action_catalog_consumer import load_catalog, eligible_projection, consume

ROOT = Path(__file__).parents[1]

def good(c):
    return {"state":"accepted", "action_id":"battle.red.research", "authorization":"security.target_authorization.v1", "arguments":{"target_id":"t"}}

def ctx(c):
    base = {"catalog_bytes": (ROOT/"fixtures/action_catalog.json").read_bytes(), "authorization_schema":"security.target_authorization.v1", "target_state":"authorized", "dependencies":[], "budget":9, "capacity":9, "cooldown":False, "isolation_profile":"docker"}
    base["revalidate"] = {**base, "freshness": True}
    return base

def test_projection_excludes_structural_and_is_sorted():
    c=load_catalog(ROOT/"fixtures/action_catalog.json")
    ids=eligible_projection(c)
    assert ids == tuple(sorted(ids)) and all(c.raw["nodes"][[n["id"] for n in c.raw["nodes"]].index(i)]["kind"] == "action" for i in ids)

def test_unknown_malformed_authorization_and_hash_fail_closed():
    c=load_catalog(ROOT/"fixtures/action_catalog.json"); x=ctx(c)
    assert consume(c,{**good(c),"action_id":"battle.red"},x).status == "error"
    assert consume(c,{**good(c),"arguments":{}},x).status == "blocked"
    x["catalog_bytes"] = b"stale"
    assert consume(c,good(c),x).reason == "stale_catalog_hash"

def test_each_jev_state_and_tau_only_dispatch():
    c=load_catalog(ROOT/"fixtures/action_catalog.json"); x=ctx(c)
    for state in ("abstain","blocked","error","no_match"):
        assert consume(c,{"state":state},x).status == state
    class E:
        def dispatch(self,*a): return {"tau":True}
    assert consume(c,good(c),x,E()).receipt == {"tau":True}
    assert consume(c,good(c),x).reason == "tau_executor_required"

def test_missing_and_invalid_freshness_fail_closed():
    c=load_catalog(ROOT/"fixtures/action_catalog.json"); x=ctx(c)
    x.pop("revalidate")
    assert consume(c,good(c),x).reason == "pre_dispatch_revalidation_required"
    x=ctx(c); x["revalidate"]["freshness"] = False
    assert consume(c,good(c),x).reason == "pre_dispatch_stale_freshness"
    x=ctx(c); x["state_identity"] = "new"; x["revalidate"]["state_identity"] = "old"
    assert consume(c,good(c),x).reason == "pre_dispatch_state_mismatch"

def test_fallback_is_revalidated():
    c=load_catalog(ROOT/"fixtures/action_catalog.json"); x=ctx(c)
    fb=good(c); fb["arguments"]={}
    assert consume(c,{"state":"no_match"},x,fallback=fb).status == "blocked"
