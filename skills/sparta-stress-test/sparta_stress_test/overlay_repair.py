"""Content-addressed replacement overlay for selected hardener baseline rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import grading
from .hardener_run import PERSONA_MODELS, multiturn_followup
from .runner import run_single

QUESTION = "How should we harden our system against CWE-287?"
CONTROL = "CWE-287"
TARGETS = [
    {"run_id": "w1-jennifer_cheung-s101", "persona": "jennifer_cheung", "seed": 101, "bank_index": 6},
    {"run_id": "w1-brandon_bailey-s101", "persona": "brandon_bailey", "seed": 101, "bank_index": 6},
    {"run_id": "w1-margaret_chen-s101", "persona": "margaret_chen", "seed": 101, "bank_index": 6},
]
GUARDS = [
    "guard_grounding_cm_cwe.sh.out",
    "guard_fabricated_deflect.sh.out",
    "guard_s87_clarify_convergence.sh.out",
    "guard_s87_substantive_draft.sh.out",
    "guard_s87_cross_corpus_isolation.sh.out",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _jsonl_lines(path: Path) -> list[bytes]:
    data = path.read_bytes()
    return data.splitlines(keepends=True)


def _load_json_line(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


def _source_hashes(root: Path) -> dict[str, str | None]:
    skill = Path(__file__).resolve().parents[1]
    paths = [
        Path(__file__),
        skill / "sparta_stress_test" / "hardener_run.py",
        skill / "sparta_stress_test" / "grading.py",
        skill / "fixtures" / "agentic_eval.json",
        skill / "fixtures" / "guard_cwe287_hardener_draft_terminal.sh",
        root / "verification_summary.json",
    ]
    return {str(path): (_file_sha(path) if path.exists() else None) for path in paths}


def _target_question() -> dict[str, Any]:
    return {
        "question": QUESTION,
        "target_control": CONTROL,
        "difficulty": "single_hop",
        "category": "sparta",
    }


def _turns(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [turn for turn in trace.get("turns") or [] if isinstance(turn, dict)]


def _substantive_draft(turn: dict[str, Any]) -> bool:
    draft = turn.get("draft_artifact") if isinstance(turn.get("draft_artifact"), dict) else {}
    body = str(draft.get("draft_answer") or "")
    reasoning = draft.get("reasoning") if isinstance(draft.get("reasoning"), dict) else {}
    gaps = "\n".join(str(item) for item in reasoning.get("gaps") or [])
    non_claims = "\n".join(str(item) for item in draft.get("non_claims") or [])
    text = "\n".join([body, gaps, non_claims]).casefold()
    required = [
        "cwe-287",
        "improper authentication",
        "evidence gap",
        "no admitted sparta relationship",
        "expert",
        "sign off",
        "not claim",
    ]
    return len(body.strip()) >= 240 and all(term in text for term in required)


def _validate_replacement(trace: dict[str, Any]) -> None:
    turns = _turns(trace)
    answer_i = next((i for i, turn in enumerate(turns) if turn.get("role") == "answer_followup"), None)
    draft_i = next((i for i, turn in enumerate(turns) if turn.get("role") == "draft"), None)
    signoff_i = next((i for i, turn in enumerate(turns) if turn.get("role") == "persona_signoff"), None)
    if answer_i is None or draft_i is None or signoff_i is None or not (answer_i < draft_i < signoff_i):
        raise RuntimeError(f"trace did not reach answer->draft->signoff: {trace}")
    answer = turns[answer_i]
    if answer.get("answered") is not False or answer.get("answer_status") != "insufficient_evidence":
        raise RuntimeError(f"answer_followup not status-aware insufficient evidence: {answer}")
    if not _substantive_draft(turns[draft_i]):
        raise RuntimeError(f"draft artifact is not substantive: {turns[draft_i]}")
    signoff = turns[signoff_i]
    if signoff.get("decision") not in {"accept", "amend"} or not str(signoff.get("signoff_status") or "").strip():
        raise RuntimeError(f"persona signoff did not reach accepted terminal state: {signoff}")


def _baseline_entry(root: Path, target: dict[str, Any]) -> dict[str, Any]:
    run = root / target["run_id"]
    result_path = run / "results.jsonl"
    trace_path = run / "hardening_trace.jsonl"
    result_lines = _jsonl_lines(result_path)
    trace_lines = _jsonl_lines(trace_path)
    idx = int(target["bank_index"])
    result = _load_json_line(result_lines[idx])
    trace = _load_json_line(trace_lines[idx])
    if result.get("question_text") != QUESTION or result.get("target_control") != CONTROL:
        raise RuntimeError(f"baseline result identity mismatch: {target}")
    if trace.get("question") != QUESTION or trace.get("target_control") != CONTROL:
        raise RuntimeError(f"baseline trace identity mismatch: {target}")
    return {
        "run_id": target["run_id"],
        "persona": target["persona"],
        "seed": target["seed"],
        "bank_index": idx,
        "question": QUESTION,
        "control": CONTROL,
        "result_path": str(result_path),
        "trace_path": str(trace_path),
        "result_file_sha256": _file_sha(result_path),
        "trace_file_sha256": _file_sha(trace_path),
        "result_line_sha256": _sha(result_lines[idx]),
        "trace_line_sha256": _sha(trace_lines[idx]),
        "result_canonical_sha256": _sha(_json_bytes(result)),
        "trace_canonical_sha256": _sha(_json_bytes(trace)),
    }


def _rerun_target(target: dict[str, Any], endpoints: str) -> tuple[dict[str, Any], dict[str, Any]]:
    q = {**_target_question(), **target}
    row = run_single(q)
    row["run_provenance"] = {
        "persona": target["persona"],
        "seed": target["seed"],
        "bank_index": target["bank_index"],
        "question": QUESTION,
        "target_control": CONTROL,
        "endpoints": endpoints,
        "model": PERSONA_MODELS[0][0],
        "effort": PERSONA_MODELS[0][1],
    }
    state: dict[str, Any] = {}
    trace = multiturn_followup(q, row, state)
    _validate_replacement(trace)
    return row, trace


def _copy_effective(root: Path, out: Path, replacements: dict[tuple[str, int], tuple[bytes, bytes]], records: list[dict[str, Any]]) -> dict[str, Any]:
    effective = out / "effective"
    if effective.exists():
        raise RuntimeError(f"effective directory already exists: {effective}")
    manifest_rows: list[dict[str, Any]] = []
    replacement_keys = {(r["run_id"], r["bank_index"]): r for r in records}
    for run_dir in sorted(path for path in root.glob("w1-*-s*") if path.is_dir()):
        dst = effective / run_dir.name
        dst.mkdir(parents=True)
        for name in ("persona_signoff.md", "stdout.txt"):
            src = run_dir / name
            if src.exists():
                shutil.copy2(src, dst / name)
        result_lines = _jsonl_lines(run_dir / "results.jsonl")
        trace_lines = _jsonl_lines(run_dir / "hardening_trace.jsonl")
        new_results, new_traces = [], []
        for idx, (result_line, trace_line) in enumerate(zip(result_lines, trace_lines)):
            key = (run_dir.name, idx)
            replacement = replacements.get(key)
            if replacement:
                result_eff, trace_eff = replacement
                rec = replacement_keys[key]
                if _file_sha(Path(rec["baseline"]["result_path"])) != rec["baseline"]["result_file_sha256"]:
                    raise RuntimeError(f"baseline result file hash changed for {key}")
                if _file_sha(Path(rec["baseline"]["trace_path"])) != rec["baseline"]["trace_file_sha256"]:
                    raise RuntimeError(f"baseline trace file hash changed for {key}")
            else:
                result_eff, trace_eff = result_line, trace_line
            new_results.append(result_eff)
            new_traces.append(trace_eff)
            manifest_rows.append({
                "run_id": run_dir.name,
                "bank_index": idx,
                "substituted": bool(replacement),
                "baseline_result_line_sha256": _sha(result_line),
                "effective_result_line_sha256": _sha(result_eff),
                "baseline_trace_line_sha256": _sha(trace_line),
                "effective_trace_line_sha256": _sha(trace_eff),
                "byte_identical": result_line == result_eff and trace_line == trace_eff,
            })
        (dst / "results.jsonl").write_bytes(b"".join(new_results))
        (dst / "hardening_trace.jsonl").write_bytes(b"".join(new_traces))
    return {
        "schema": "sparta.hardener.effective_manifest.v1",
        "generated_at": _now(),
        "baseline_root": str(root),
        "effective_root": str(effective),
        "total_rows": len(manifest_rows),
        "substituted_rows": sum(1 for row in manifest_rows if row["substituted"]),
        "byte_identical_rows": sum(1 for row in manifest_rows if row["byte_identical"]),
        "rows": manifest_rows,
    }


def _guard_summary(root: Path) -> dict[str, Any]:
    rows = []
    for name in GUARDS:
        path = root / name
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        rows.append({"path": str(path), "sha256": _file_sha(path) if path.exists() else None, "passed": "PASS" in text})
    return {"passed": sum(1 for row in rows if row["passed"]), "total": len(rows), "guards": rows}


def execute(root: Path, out_parent: Path, endpoints: str) -> Path:
    baselines = [_baseline_entry(root, target) for target in TARGETS]
    replacements: dict[tuple[str, int], tuple[bytes, bytes]] = {}
    records: list[dict[str, Any]] = []
    started = _now()
    for target, baseline in zip(TARGETS, baselines):
        row, trace = _rerun_target(target, endpoints)
        result_bytes = _json_bytes(row)
        trace_bytes = _json_bytes(trace)
        key_payload = {
            "baseline_result_line_sha256": baseline["result_line_sha256"],
            "baseline_trace_line_sha256": baseline["trace_line_sha256"],
            "replacement_result_sha256": _sha(result_bytes),
            "replacement_trace_sha256": _sha(trace_bytes),
            "provenance": {k: target[k] for k in ("run_id", "persona", "seed", "bank_index")},
            "question": QUESTION,
            "control": CONTROL,
        }
        overlay_key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = {
            "schema": "sparta.hardener.overlay_record.v1",
            "overlay_key": overlay_key,
            "run_id": target["run_id"],
            "timestamp": _now(),
            "model": PERSONA_MODELS[0][0],
            "effort": PERSONA_MODELS[0][1],
            "persona": target["persona"],
            "seed": target["seed"],
            "bank_index": target["bank_index"],
            "question": QUESTION,
            "control": CONTROL,
            "baseline": baseline,
            "replacement": {
                "result_sha256": _sha(result_bytes),
                "trace_sha256": _sha(trace_bytes),
                "result": row,
                "trace": trace,
            },
            "fixture_source_hashes": _source_hashes(root),
        }
        records.append(record)
        replacements[(target["run_id"], target["bank_index"])] = (result_bytes, trace_bytes)

    run_key = hashlib.sha256("".join(record["overlay_key"] for record in records).encode()).hexdigest()[:24]
    final = out_parent / f"overlay_{run_key}"
    if final.exists():
        raise RuntimeError(f"overlay already exists (append-only): {final}")
    tmp = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=str(out_parent)))
    try:
        (tmp / "overlay_records.jsonl").write_bytes(b"".join(_json_bytes(record) for record in records))
        effective_manifest = _copy_effective(root, tmp, replacements, records)
        effective_manifest["effective_root"] = str(final / "effective")
        (tmp / "effective_manifest.json").write_text(json.dumps(effective_manifest, indent=2), encoding="utf-8")
        grade_rows = []
        for run_dir in sorted((tmp / "effective").glob("w1-*-s*")):
            traces = [json.loads(line) for line in (run_dir / "hardening_trace.jsonl").read_text().splitlines() if line.strip()]
            for idx, line in enumerate((run_dir / "results.jsonl").read_text().splitlines()):
                if not line.strip():
                    continue
                row = json.loads(line)
                row["hardening_trace"] = traces[idx]
                row["run_id"] = run_dir.name
                grade_rows.append(row)
        grade = grading.grade_results(grade_rows, tmp / "grading", wave=1)
        grade["review_path"] = str(final / "grading" / "wave_1_review.md")
        (tmp / "grading" / "routing_grade.json").write_text(json.dumps(grade, indent=2), encoding="utf-8")
        guards = _guard_summary(root)
        metrics = {
            "routing_correct_rate": grade["routing_correct_rate"],
            "context_failures": grade["context_failures"],
            "expert_queue": grade["expert_queue"],
            "persuasive_but_wrong": grade["persuasive_but_wrong"],
            "retained_guards_passed": guards["passed"],
            "retained_guards_total": guards["total"],
        }
        if not (metrics["routing_correct_rate"] >= 0.90 and metrics["context_failures"] == 0 and metrics["expert_queue"] == 0 and metrics["persuasive_but_wrong"] == 0 and metrics["retained_guards_passed"] == 5):
            raise RuntimeError(f"effective metrics failed: {metrics}")
        manifest = {
            "schema": "sparta.hardener.overlay_manifest.v1",
            "run_id": f"overlay_{run_key}",
            "started_at": started,
            "completed_at": _now(),
            "baseline_root": str(root),
            "overlay_dir": str(final),
            "records": records,
            "effective_manifest_path": str(final / "effective_manifest.json"),
            "grade_path": str(final / "grading" / "routing_grade.json"),
            "review_path": str(final / "grading" / "wave_1_review.md"),
            "guard_summary": guards,
            "metrics": metrics,
        }
        (tmp / "overlay_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.rename(final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/mnt/storage12tb/spacetrail/sparta-hardener/verification-20260921T230602Z")
    ap.add_argument("--out-parent", default=None)
    ap.add_argument("--endpoints", default="http://127.0.0.1:8601")
    args = ap.parse_args()
    root = Path(args.root)
    out_parent = Path(args.out_parent) if args.out_parent else root / "overlays"
    out_parent.mkdir(parents=True, exist_ok=True)
    final = execute(root, out_parent, args.endpoints)
    print(json.dumps({"overlay_dir": str(final), "manifest": str(final / "overlay_manifest.json")}, indent=2))


if __name__ == "__main__":
    main()
