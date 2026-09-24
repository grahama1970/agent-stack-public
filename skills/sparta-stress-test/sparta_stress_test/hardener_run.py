"""Play-task entry for the sparta-stress-hardener workflowScript.

Exact contract of the workflowScript play task:
  python3 -m sparta_stress_test.hardener_run --persona P --seed S --run-dir W --endpoints BASE

Deterministic small bank per (persona, seed): real corpus controls + fabricated
controls; runs each through run_single (REAL /intent -> /answer -> /clarify ->
/deflect via pipeline_client) and writes results.jsonl into the run dir.

MULTI-TURN (wave 1): for CLARIFY results the persona simulates the human
answer to the clarifying question and the follow-up turn goes back through the
REAL pipeline; for held/ungrounded answers the REAL /draft product is invoked
and the persona simulates the human signoff checkpoint (accept/amend/reject)
via /draft/signoff. The persona NEVER grades correctness.

Persona model: cheap/low-effort LLM via the scillm proxy (never a HIGH-effort
frontier model in the measured persona role). The current repaired proof uses
only the live-verified gemini-flash model id.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .runner import run_single

PERSONA_MODELS = [  # verified live in this repair cycle; do not probe fallback IDs.
    ("gemini-flash", "low"),
]
PERSONA_TIMEOUT_S = 300
PERSONA_MAX_ATTEMPTS = 3
PERSONA_RETRY_BACKOFF_S = (1.0, 3.0)
BANK_SIZE = 7


def _scillm_key() -> str:
    # master keys first; the dev-default SCILLM_PROXY_KEY is rejected when the
    # proxy has a real key override configured, so try it last.
    for k in ("SCILLM_MASTER_KEY", "LITELLM_MASTER_KEY"):
        if os.environ.get(k):
            return os.environ[k]
    # the running proxy container's own master key (localhost only)
    try:
        import subprocess
        out = subprocess.run(
            ["docker", "inspect", "docker-scillm-proxy-1", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if line.startswith("SCILLM_MASTER_KEY="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return os.environ.get("SCILLM_PROXY_KEY", "")


def _is_transient_persona_error(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError))


def persona_llm(
    messages: list[dict],
    state: dict,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_attempts: int = PERSONA_MAX_ATTEMPTS,
) -> str:
    """Cheap persona completion using the verified live persona model."""
    key = _scillm_key()
    models = PERSONA_MODELS[state.get("model_idx", 0):]
    last_err = None
    for i, (model, effort) in enumerate(models):
        for attempt in range(1, max_attempts + 1):
            body = json.dumps({"model": model, "reasoning_effort": effort,
                               "messages": messages}).encode()
            req = urllib.request.Request(
                "http://localhost:4001/v1/chat/completions", data=body,
                headers={"Authorization": f"Bearer {key}", "X-Caller-Skill": "scillm",
                         "Content-Type": "application/json"})
            evidence = {"model": model, "effort": effort, "attempt": attempt}
            try:
                with urlopen_fn(req, timeout=PERSONA_TIMEOUT_S) as r:
                    d = json.loads(r.read())
                evidence["outcome"] = "success"
                state.setdefault("persona_attempts", []).append(evidence)
                state["model"], state["effort"] = model, effort
                state["model_idx"] = state.get("model_idx", 0) + i
                return d["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                evidence.update({"outcome": "http_error", "status": e.code})
                state.setdefault("persona_attempts", []).append(evidence)
                raise
            except Exception as e:
                if not _is_transient_persona_error(e):
                    evidence.update({"outcome": "non_transient_error", "error_type": type(e).__name__})
                    state.setdefault("persona_attempts", []).append(evidence)
                    raise
                last_err = f"{model}: {type(e).__name__}: {str(e)[:200]}"
                evidence.update({"outcome": "transient_error", "error_type": type(e).__name__,
                                 "error": str(e)[:200]})
                state.setdefault("persona_attempts", []).append(evidence)
                if attempt >= max_attempts:
                    break
                sleep_fn(PERSONA_RETRY_BACKOFF_S[min(attempt - 1, len(PERSONA_RETRY_BACKOFF_S) - 1)])
    raise RuntimeError(f"persona_llm exhausted models after {max_attempts} attempts: {last_err}")


def _post_local(path: str, payload: dict) -> dict:
    base = os.environ.get("SPARTA_ENDPOINTS", "http://127.0.0.1:8601")
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


PERSONA_BIOS = {
    "jennifer_cheung": (
        "You role-play Jennifer Cheung, a hands-on spacecraft GNC engineer. "
        "Practical, skeptical, wants actionable countermeasures. Answer as the "
        "human user would. NEVER grade or evaluate the system's correctness."),
    "brandon_bailey": (
        "You role-play Brandon Bailey, a SPARTA compliance analyst. Precise, "
        "evidence-oriented, and careful about control identifiers. Answer as the "
        "human user would. NEVER grade or evaluate the system's correctness."),
    "margaret_chen": (
        "You role-play Margaret Chen, a program security manager. You focus on "
        "risk decisions, auditability, and concise next actions. Answer as the "
        "human user would. NEVER grade or evaluate the system's correctness."),
}


def persona_turn(persona: str, history: list[dict], instruction: str, state: dict) -> str:
    if persona not in PERSONA_BIOS:
        raise ValueError(f"unknown persona: {persona}")
    msgs = ([{"role": "system", "content": PERSONA_BIOS[persona]}]
            + history + [{"role": "user", "content": instruction}])
    return persona_llm(msgs, state).strip()


def _draft_signoff_followup(question_text: str, persona: str, state: dict, trace: dict) -> None:
    draft = _post_local("/draft", {"q": question_text, "scope": "sparta",
                                   "created_by": persona})
    trace["checkpoint"] = "draft_signoff"
    trace["turns"].append({"role": "draft", "draft_id": draft.get("draft_id"),
                            "answer_excerpt": (draft.get("draft_answer") or "")[:300],
                            "draft_artifact": draft})
    decision = persona_turn(persona, [
        {"role": "user", "content": question_text},
        {"role": "assistant", "content":
            json.dumps({"draft_answer": (draft.get("draft_answer") or "(empty — no candidate text provided)")[:800],
                        "gaps": str(draft.get("reasoning", {}).get("gaps"))[:300]})},
    ], "You are the human reviewer of this DRAFT packet as a non-authoritative "
       "collaboration artifact. Decide: accept (the packet is plausible and useful "
       "for expert review, even if it names evidence gaps), amend (reply 'amend: "
       "<your one-sentence candidate packet text>'), or reject (fabricated/unusable). "
       "Format: one word then one sentence. Do not grade the earlier system response "
       "or treat this as answer authority; judge only whether this draft packet is "
       "fit to send to an expert reviewer.", state)
    word = decision.split()[0].strip('.,:').lower() if decision.split() else "reject"
    decision_v = word if word in ("accept", "amend", "reject") else "needs_more_evidence"
    payload = {"draft": draft, "decision": decision_v,
               "signed_by": persona, "reviewer_role": "agent",
               "signoff_level": "agent_plausibility_reviewed",
               "authority_scope": "plausibility_only"}
    if decision_v in ("accept", "amend"):
        # contract: accepted/amended sign-off requires candidate answer text
        payload["answer"] = (decision.split(":", 1)[1].strip()
                             if decision_v == "amend" and ":" in decision
                             else (draft.get("draft_answer") or
                                   "Plausible per draft; pending expert evidence."))
    sign = _post_local("/draft/signoff", payload)
    trace["turns"].append({"role": "persona_signoff", "decision": decision_v,
                            "raw": decision[:200],
                            "signoff_keys": list(sign.keys())[:10],
                            "signoff_status": (sign.get("status") or sign.get("admission_state")
                                               or ("ok" if sign.get("ok") else None))})


def multiturn_followup(q: dict, r: dict, state: dict) -> dict:
    """Persona-simulated human checkpoints after run_single. Returns a trace dict."""
    trace = {"question": r["question_text"], "persona": q.get("persona"),
             "seed": q.get("seed"), "target_control": q.get("target_control"),
             "bank_index": q.get("bank_index"),
             "single_turn_action": r["actual_action"], "checkpoint": None,
             "turns": [], "model": None, "effort": None}
    persona = q.get("persona", "jennifer_cheung")
    action = r["actual_action"]
    guidance = ""
    for t in r.get("turns", []):
        if t.get("role") == "assistant":
            guidance = t.get("content", "")
            break

    if action == "CLARIFY":
        try:
            cl = _post_local("/clarify", {"q": r["question_text"], "scope": "sparta"})
            guidance = cl.get("final_response") or cl.get("clarifying_question") or ""
        except Exception as e:
            guidance = ""
            trace["turns"].append({"role": "clarify_error", "error": str(e)[:200]})
    if action == "CLARIFY" and guidance:
        # Human checkpoint: answer the clarifying question in character.
        reply = persona_turn(persona, [
            {"role": "user", "content": r["question_text"]},
            {"role": "assistant", "content": guidance},
        ], "Answer the assistant's clarifying question in character, briefly. "
           "Do not evaluate the question.", state)
        trace["checkpoint"] = "clarify_answer"
        trace["turns"].append({"role": "clarifying_question", "content": guidance[:300]})
        trace["turns"].append({"role": "persona", "content": reply})
        # Follow-up turn back through the REAL pipeline (/intent with context).
        try:
            context = [
                {"role": "user", "content": r["question_text"]},
                {"role": "clarifying_question", "content": guidance},
                {"role": "user", "content": reply},
            ]
            intent2 = _post_local("/intent", {
                "q": r["question_text"], "scope": "sparta", "session_id": "sparta-stress-test",
                "context": context})
            trace["turns"].append({"role": "intent_followup",
                                    "action": intent2.get("action"),
                                    "confidence": intent2.get("confidence")})
            a2 = intent2.get("action")
            if a2 in ("QUERY", "COMPLIANCE"):
                ans2 = _post_local("/answer", {"q": r["question_text"], "scope": "sparta", "k": 10,
                                                "intent_action": a2})
                sources = ans2.get("sources") or []
                answered = ans2.get("can_answer") is True
                trace["turns"].append({
                    "role": "answer_followup",
                    "answered": answered,
                    "answer_status": ans2.get("status"),
                    "text": (ans2.get("final_response") or ans2.get("source_answer") or "")[:500],
                    "qra_count": len(sources)})
                if not answered:
                    _draft_signoff_followup(r["question_text"], persona, state, trace)
            elif a2 == "NO_MATCH":
                d2 = _post_local("/deflect", {"q": reply, "intent_action": "NO_MATCH"})
                trace["turns"].append({"role": "deflect_followup",
                                        "text": (d2.get("final_response") or "")[:300]})
        except Exception as e:
            trace["turns"].append({"role": "pipeline_error", "error": str(e)[:300]})

    elif (
        action in ("NO_MATCH", "COMPLIANCE")
        and r.get("grade") in ("F", "D")
        and not (r.get("had_answer") and r.get("source_keys"))
    ):
        # Ungrounded/failed answer -> REAL /draft, then persona human signoff.
        try:
            _draft_signoff_followup(r["question_text"], persona, state, trace)
        except Exception as e:
            trace["turns"].append({"role": "draft_error", "error": str(e)[:300]})
    else:
        trace["checkpoint"] = "none"
    trace["model"], trace["effort"] = state.get("model"), state.get("effort")
    return trace


REAL_CONTROLS = ["EX-0016.01", "DE-0009.01", "CM0029", "IA-0007.02", "CWE-287"]
FABRICATED = ["SV-ZZ-99", "SV-XX-77"]
TEMPLATES = [
    "What countermeasures address the threats related to {c}?",
    "How should we harden our system against {c}?",
    "What does SPARTA say about {c}?",
]


def build_bank(persona: str, seed: int) -> list[dict]:
    rng = random.Random(seed)
    bank = []
    for c in REAL_CONTROLS + FABRICATED:
        bank.append({"question": rng.choice(TEMPLATES).format(c=c), "target_control": c,
                     "persona": persona, "seed": seed, "difficulty": "single_hop"})
    rng.shuffle(bank)
    for i, q in enumerate(bank):
        q["bank_index"] = i
    return bank


def _provenance(persona: str, seed: int, bank_index: int, q: dict, endpoints: str) -> dict:
    return {
        "persona": persona,
        "seed": seed,
        "bank_index": bank_index,
        "question": q["question"],
        "target_control": q.get("target_control"),
        "endpoints": endpoints,
    }


def continuation_verifier_command(python_invocation: str, log_path: str | Path) -> str:
    """Shell snippet for verifier continuations; tee cannot mask Python failure."""
    return (
        "set -euo pipefail\n"
        f"{python_invocation} 2>&1 | tee {shlex.quote(str(log_path))}\n"
    )


def _replace_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
    except Exception:
        try:
            Path(tmp).unlink()
        finally:
            raise


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_jsonl_checkpoint(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    rows: list[dict] = []
    for i, line in enumerate(lines):
        if not line.strip():
            if i == len(lines) - 1:
                continue
            raise ValueError(f"{path}: blank JSONL line at {i + 1}")
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                raise ValueError(f"{path}: invalid JSON before final line")
            _replace_text(path, "\n".join(lines[:i]) + ("\n" if i else ""))
            return rows
    return rows


def _trim_jsonl(path: Path, keep: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    _replace_text(path, "\n".join(lines[:keep]) + ("\n" if keep else ""))


def _validate_row(row: dict, q: dict, persona: str, seed: int, bank_index: int, endpoints: str) -> None:
    if row.get("question_text") != q["question"][:100]:
        raise ValueError(f"result row {bank_index}: question mismatch")
    if row.get("target_control") != q.get("target_control"):
        raise ValueError(f"result row {bank_index}: control mismatch")
    if row.get("persona") != persona:
        raise ValueError(f"result row {bank_index}: persona mismatch")
    prov = row.get("run_provenance")
    if not isinstance(prov, dict):
        raise ValueError(f"result row {bank_index}: missing run_provenance")
    expected = _provenance(persona, seed, bank_index, q, endpoints)
    for key, value in expected.items():
        if key in prov and prov.get(key) != value:
            raise ValueError(f"result row {bank_index}: provenance {key} mismatch")
    for key in ("persona", "seed", "target_control"):
        if key not in prov:
            raise ValueError(f"result row {bank_index}: provenance {key} missing")


def _validate_trace(trace: dict, row: dict, q: dict, persona: str, seed: int, bank_index: int) -> None:
    if trace.get("question") != q["question"][:100]:
        raise ValueError(f"trace row {bank_index}: question mismatch")
    if trace.get("target_control") != q.get("target_control"):
        raise ValueError(f"trace row {bank_index}: control mismatch")
    if trace.get("persona") != persona:
        raise ValueError(f"trace row {bank_index}: persona mismatch")
    if trace.get("seed") != seed:
        raise ValueError(f"trace row {bank_index}: seed mismatch")
    if "bank_index" in trace and trace.get("bank_index") != bank_index:
        raise ValueError(f"trace row {bank_index}: bank_index mismatch")
    if trace.get("single_turn_action") != row.get("actual_action"):
        raise ValueError(f"trace row {bank_index}: action mismatch")


def _load_checkpoint(run_dir: Path, bank: list[dict], persona: str, seed: int, endpoints: str) -> tuple[list[dict], list[dict]]:
    results_path = run_dir / "results.jsonl"
    traces_path = run_dir / "hardening_trace.jsonl"
    rows = _read_jsonl_checkpoint(results_path)
    traces = _read_jsonl_checkpoint(traces_path)
    if len(rows) > BANK_SIZE or len(traces) > BANK_SIZE:
        raise ValueError("checkpoint has more than 7 rows")
    if len(rows) != len(traces):
        if abs(len(rows) - len(traces)) != 1:
            raise ValueError("checkpoint has multiple unpaired rows")
        keep = min(len(rows), len(traces))
        if len(rows) > keep:
            _trim_jsonl(results_path, keep)
            rows = rows[:keep]
        if len(traces) > keep:
            _trim_jsonl(traces_path, keep)
            traces = traces[:keep]
    for i, (row, trace) in enumerate(zip(rows, traces)):
        _validate_row(row, bank[i], persona, seed, i, endpoints)
        _validate_trace(trace, row, bank[i], persona, seed, i)
    return rows, traces


def _signoff_header(persona: str, seed: int) -> str:
    return f"# persona sign-off: {persona} seed={seed}"


def _valid_signoff(path: Path, persona: str, seed: int) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("persona_signoff.md exists but is empty")
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if first != _signoff_header(persona, seed):
        raise ValueError("persona_signoff.md has wrong persona/seed header")
    return True


def _write_signoff(path: Path, persona: str, seed: int, state: dict) -> None:
    signoff = persona_turn(persona, [],
        f"You just completed a stress-test session with the SPARTA memory system "
        f"as {persona}. In 2-3 sentences, give your in-character sign-off on the "
        f"CONVERSATION EXPERIENCE only (did it feel useful, responsive, honest). "
        f"Do NOT grade factual correctness.", state)
    if not signoff.strip():
        raise ValueError("persona LLM returned empty signoff")
    _replace_text(path,
        f"{_signoff_header(persona, seed)}\n"
        f"model: {state.get('model')} effort: {state.get('effort')}\n"
        f"fallbacks: {json.dumps(state.get('fallbacks', []))}\n\n{signoff}\n")


def _fake_result(q: dict, persona: str, seed: int, bank_index: int, endpoints: str) -> dict:
    return {
        "question_text": q["question"][:100],
        "target_control": q.get("target_control"),
        "persona": persona,
        "actual_action": "COMPLIANCE",
        "run_provenance": _provenance(persona, seed, bank_index, q, endpoints),
    }


def _fake_trace(q: dict, persona: str, seed: int, bank_index: int) -> dict:
    return {
        "question": q["question"][:100],
        "persona": persona,
        "seed": seed,
        "target_control": q.get("target_control"),
        "bank_index": bank_index,
        "single_turn_action": "COMPLIANCE",
        "checkpoint": "none",
        "turns": [],
    }


class _FakePersonaResponse:
    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"content": self._text}}]}).encode()


def persona_retry_self_check() -> None:
    old_key = globals()["_scillm_key"]
    globals()["_scillm_key"] = lambda: "test-key"
    try:
        calls = {"n": 0}

        def timeout_then_success(_req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("simulated timeout")
            return _FakePersonaResponse("ok")

        state: dict[str, Any] = {}
        text = persona_llm([{"role": "user", "content": "x"}], state,
                           urlopen_fn=timeout_then_success, sleep_fn=lambda _s: None,
                           max_attempts=2)
        assert text == "ok"
        assert calls["n"] == 2
        assert state["model"] == "gemini-flash"
        assert [a["outcome"] for a in state["persona_attempts"]] == ["transient_error", "success"]

        exhausted: dict[str, Any] = {}
        try:
            persona_llm([{"role": "user", "content": "x"}], exhausted,
                        urlopen_fn=lambda _req, timeout: (_ for _ in ()).throw(TimeoutError("again")),
                        sleep_fn=lambda _s: None, max_attempts=2)
        except RuntimeError as exc:
            assert "exhausted" in str(exc)
        else:
            raise AssertionError("exhausted transient retries were accepted")
        assert [a["outcome"] for a in exhausted["persona_attempts"]] == ["transient_error", "transient_error"]

        cmd = continuation_verifier_command("timeout 1 python verifier.py", "/tmp/verifier.log")
        assert "pipefail" in cmd and "| tee " in cmd and "|| exit $?" not in cmd
    finally:
        globals()["_scillm_key"] = old_key


def checkpoint_self_check() -> None:
    persona_retry_self_check()
    persona, seed, endpoints = "jennifer_cheung", 101, "http://example.invalid"
    bank = build_bank(persona, seed)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        complete = root / "complete"
        complete.mkdir()
        _replace_text(complete / "results.jsonl", "".join(
            json.dumps(_fake_result(q, persona, seed, i, endpoints)) + "\n"
            for i, q in enumerate(bank)))
        _replace_text(complete / "hardening_trace.jsonl", "".join(
            json.dumps(_fake_trace(q, persona, seed, i)) + "\n"
            for i, q in enumerate(bank)))
        _replace_text(complete / "persona_signoff.md", f"{_signoff_header(persona, seed)}\nmodel: test effort: low\n\nok\n")
        rows, traces = _load_checkpoint(complete, bank, persona, seed, endpoints)
        assert len(rows) == BANK_SIZE and len(traces) == BANK_SIZE
        assert _valid_signoff(complete / "persona_signoff.md", persona, seed)

        bad = root / "bad"
        bad.mkdir()
        wrong = _fake_result(bank[0], "margaret_chen", seed, 0, endpoints)
        _replace_text(bad / "results.jsonl", json.dumps(wrong) + "\n")
        _replace_text(bad / "hardening_trace.jsonl", json.dumps(_fake_trace(bank[0], persona, seed, 0)) + "\n")
        try:
            _load_checkpoint(bad, bank, persona, seed, endpoints)
        except ValueError as exc:
            assert "persona" in str(exc)
        else:
            raise AssertionError("mismatched persona was accepted")

        dangling = root / "dangling"
        dangling.mkdir()
        _replace_text(dangling / "results.jsonl",
            json.dumps(_fake_result(bank[0], persona, seed, 0, endpoints)) + "\n" +
            json.dumps(_fake_result(bank[1], persona, seed, 1, endpoints)) + "\n")
        _replace_text(dangling / "hardening_trace.jsonl", json.dumps(_fake_trace(bank[0], persona, seed, 0)) + "\n")
        rows, traces = _load_checkpoint(dangling, bank, persona, seed, endpoints)
        assert len(rows) == 1 and len(traces) == 1
        assert len((dangling / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    print("checkpoint self-check: OK")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check-checkpoint", action="store_true")
    ap.add_argument("--persona")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--run-dir")
    ap.add_argument("--endpoints", default="http://127.0.0.1:8601")
    a = ap.parse_args()
    if a.self_check_checkpoint:
        checkpoint_self_check()
        return
    if not a.persona or a.seed is None or not a.run_dir:
        ap.error("--persona, --seed, and --run-dir are required unless --self-check-checkpoint is used")
    if a.persona not in PERSONA_BIOS:
        raise ValueError(f"unknown persona: {a.persona}")

    wd = Path(a.run_dir)
    wd.mkdir(parents=True, exist_ok=True)
    os.environ["SPARTA_ENDPOINTS"] = a.endpoints
    out = wd / "results.jsonl"
    trace_f = wd / "hardening_trace.jsonl"
    signoff_f = wd / "persona_signoff.md"
    state: dict = {}
    bank = build_bank(a.persona, a.seed)
    rows, traces = _load_checkpoint(wd, bank, a.persona, a.seed, a.endpoints)
    if signoff_f.exists():
        _valid_signoff(signoff_f, a.persona, a.seed)

    for i in range(len(rows), BANK_SIZE):
        q = bank[i]
        r = run_single(q)
        r["run_provenance"] = _provenance(a.persona, a.seed, i, q, a.endpoints)
        _append_jsonl(out, r)
        trace = multiturn_followup(q, r, state)
        _append_jsonl(trace_f, trace)
        rows.append(r)
        traces.append(trace)

    rows, traces = _load_checkpoint(wd, bank, a.persona, a.seed, a.endpoints)
    if len(rows) != BANK_SIZE or len(traces) != BANK_SIZE:
        raise RuntimeError(f"checkpoint incomplete: results={len(rows)} traces={len(traces)}")
    if not _valid_signoff(signoff_f, a.persona, a.seed):
        _write_signoff(signoff_f, a.persona, a.seed, state)
    if not _valid_signoff(signoff_f, a.persona, a.seed):
        raise RuntimeError("persona signoff validation failed")

    print(f"HARDENER_RUN_DONE persona={a.persona} seed={a.seed} lines={len(rows)} "
          f"traces={len(traces)} model={state.get('model')} effort={state.get('effort')}")


if __name__ == "__main__":
    main()
