#!/usr/bin/env python3
"""Deterministic production-facing probe for the Battle catalog consumer."""
import argparse, json
from pathlib import Path
from battle_skill.action_catalog_consumer import load_catalog, consume

ROOT = Path(__file__).parents[1]

def main():
    p=argparse.ArgumentParser(); p.add_argument("scenario"); p.add_argument("--receipt",required=True); a=p.parse_args()
    c=load_catalog(ROOT/"fixtures/action_catalog.json")
    base={"catalog_bytes":(ROOT/"fixtures/action_catalog.json").read_bytes(),"authorization_schema":"security.target_authorization.v1","target_state":"authorized","dependencies":[],"budget":9,"capacity":9,"cooldown":False,"isolation_profile":"docker"}
    proposal={"state":"accepted","action_id":"battle.red.research","authorization":"security.target_authorization.v1","arguments":{"target_id":"t"}}
    class Tau:
        def dispatch(self, action_id, arguments, profile): return {"called":True,"action_id":action_id,"profile":profile}
    executor=Tau()
    if a.scenario=="accepted": base["revalidate"]={**base,"freshness":True}
    elif a.scenario=="missing_revalidate": pass
    elif a.scenario=="freshness_false": base["revalidate"]={**base,"freshness":False}
    elif a.scenario=="freshness_mismatch": base["state_identity"]="new"; base["revalidate"]={**base,"freshness":True,"state_identity":"old"}
    elif a.scenario=="structural": proposal["action_id"]="battle.red"
    elif a.scenario=="malformed": proposal["arguments"]={}; base["revalidate"]={**base,"freshness":True}
    elif a.scenario=="jev": proposal={"state":"blocked"}
    elif a.scenario=="fallback": proposal={"state":"no_match"}; base["revalidate"]={**base,"freshness":True}; executor=None
    elif a.scenario=="tau_boundary": base["revalidate"]={**base,"freshness":True}; executor=None
    else: raise SystemExit(2)
    d=consume(c,proposal,base,executor,fallback={"state":"accepted"} if a.scenario=="fallback" else None)
    receipt={"scenario":a.scenario,"status":d.status,"reason":d.reason,"action_id":d.action_id,"dispatched":d.receipt is not None}
    Path(a.receipt).write_text(json.dumps(receipt,sort_keys=True)+"\n")
    return 0 if d.status in {"accepted","blocked","error"} else 1
if __name__=="__main__": raise SystemExit(main())
