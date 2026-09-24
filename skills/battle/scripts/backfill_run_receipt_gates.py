#!/usr/bin/env python3
"""Hash-bound disposition index for historical Battle receipts.

This backfill never executes preserved specimens. It inspects retained receipt
bytes, records eligibility corrections, and leaves history intact.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import run_small_medium_production_battle as gates  # noqa: E402

AUDIT_SCHEMA = "battle.production_scale.backfill_gate_audit.v1"
INDEX_SCHEMA = "battle.production_scale.backfill_gate_index.v1"
DISPOSITIONS = {
    "valid_under_original_contract",
    "revoked_false_green",
    "unparseable",
    "missing_dependency",
    "unverified",
}
RECEIPT_NAMES = {"run-receipt.json", "round-receipt.json", "scorekeeper-receipt.json"}
DOWNSTREAM_NAMES = {"REPORT.md", "commentary.jsonl"}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str | None:
    return gates.sha256_file(path) if path.exists() and path.is_file() else None


def _kind(path: Path) -> str:
    name = path.name
    if name == "run-receipt.json":
        return "run_receipt"
    if name == "round-receipt.json":
        return "round_receipt"
    if name == "scorekeeper-receipt.json":
        return "scorekeeper_receipt"
    if name == "REPORT.md":
        return "downstream_report"
    if name == "commentary.jsonl":
        return "downstream_commentary"
    if "learning" in name.lower() and name.endswith(".json"):
        return "downstream_learning"
    return "json_artifact"


def _declared_paths(root: Path, extra_paths: list[Path] | None = None) -> list[Path]:
    if root.is_file():
        paths: set[Path] = {root}
    else:
        paths = set()
        for name in RECEIPT_NAMES | DOWNSTREAM_NAMES:
            paths.update(root.rglob(name))
        paths.update(path for path in root.rglob("*.json") if "learning" in path.name.lower())
    for path in extra_paths or []:
        paths.add(path)
    return sorted(paths)
def _resolve_ref(base: Path, ref: dict[str, Any] | str) -> Path | None:
    raw = ref.get("path") if isinstance(ref, dict) else ref
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else base / path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _dependency_problems(receipt_path: Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    refs = receipt.get("round_receipts") or receipt.get("scoreboard", {}).get("round_receipts") or []
    problems: list[dict[str, Any]] = []
    for ref in refs:
        dep_path = _resolve_ref(receipt_path.parent, ref)
        expected_hash = ref.get("sha256") if isinstance(ref, dict) else None
        if dep_path is None or not dep_path.is_file():
            problems.append({"kind": "missing_dependency", "ref": ref, "reason": "referenced_child_receipt_missing"})
            continue
        actual_hash = gates.sha256_file(dep_path)
        if expected_hash and gates.normalize_sha256(str(expected_hash)) != actual_hash:
            problems.append({"kind": "missing_dependency", "path": str(dep_path), "reason": "referenced_child_receipt_hash_mismatch", "expected_sha256": expected_hash, "actual_sha256": actual_hash})
            continue
        if not _inside(dep_path, receipt_path.parent):
            problems.append({"kind": "missing_dependency", "path": str(dep_path), "reason": "referenced_child_receipt_outside_parent_scope", "parent_scope": str(receipt_path.parent)})
    return problems


def _attempt_issue(attempt: dict[str, Any]) -> dict[str, Any] | None:
    stderr = attempt.get("stderr", "") or ""
    code = attempt.get("semantic_exit_code", attempt.get("exit_code"))
    gate = attempt.get("gate")
    label = attempt.get("label")
    signed = code in gates.ALLOWED_SEMANTIC_EXIT_CODES and gates.judge_exit_is_signed(attempt, int(code))
    if code in {2, 10} and signed:
        return None
    if gates.TRACEBACK_RE.search(stderr):
        return {"kind": "revocable_execution_failure", "reason": "compile_or_import_failure", "label": label, "exit_code": attempt.get("exit_code"), "gate": gate}
    if attempt.get("timed_out") or gate == "CANCELED_TIMEOUT" or attempt.get("error"):
        return {"kind": "revocable_execution_failure", "reason": "harness_failure", "label": label, "exit_code": attempt.get("exit_code"), "gate": gate}
    if code in gates.ALLOWED_SEMANTIC_EXIT_CODES:
        return None
    if attempt.get("process_status") == "FAIL" or gate in {"ERROR_COMPILE", "HARNESS_ERROR"}:
        return {"kind": "unverified", "reason": "fail_label_without_raw_revocation_evidence", "label": label, "exit_code": attempt.get("exit_code"), "gate": gate}
    if code is not None:
        return {"kind": "unverified", "reason": "nonsemantic_exit_without_original_contract", "label": label, "exit_code": attempt.get("exit_code"), "gate": gate}
    return None


def _execution_issues(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rounds = receipt.get("rounds") or [receipt]
    for round_item in rounds:
        for attempt in round_item.get("docker_attempts", []) or []:
            issue = _attempt_issue(attempt)
            if issue:
                issues.append({"round": round_item.get("round_number"), **issue})
        spawn = round_item.get("spawn_receipt") or {}
        for child in spawn.get("children", []) or []:
            preflight = child.get("preflight", {})
            issue = _attempt_issue(preflight)
            if issue:
                issues.append({"round": round_item.get("round_number"), "child_id": child.get("child_id"), **issue})
    return issues


def _disposition(original_status: Any, issues: list[dict[str, Any]], dependency_problems: list[dict[str, Any]]) -> str:
    if dependency_problems:
        return "missing_dependency"
    if original_status == "PASS" and any(issue.get("kind") == "revocable_execution_failure" for issue in issues):
        return "revoked_false_green"
    if any(issue.get("kind") == "unverified" for issue in issues):
        return "unverified"
    return "valid_under_original_contract"


def audit_run_receipt(path: Path, *, preserve_original: bool = False) -> dict[str, Any]:
    """Backward-compatible single-run audit entry."""
    entry = audit_artifact(path, preserve_original=preserve_original)
    return {
        "schema": AUDIT_SCHEMA,
        "path": entry["path"],
        "original_sha256": entry.get("original_sha256"),
        "recomputed": True,
        "original_status": entry.get("original_status"),
        "recomputed_status": "FALSE_GREEN_REVOKED" if entry.get("disposition") == "revoked_false_green" else entry.get("original_status", "UNKNOWN"),
        "winner": "NOT_SCORED" if entry.get("disposition") == "revoked_false_green" else entry.get("winner"),
        "false_green_child_failures": entry.get("execution_issues", []),
        "original_preserved_as": entry.get("original_preserved_as"),
        "disposition": entry.get("disposition"),
        "dependency_problems": entry.get("dependency_problems", []),
    }


def audit_artifact(path: Path, *, preserve_original: bool = False) -> dict[str, Any]:
    base = {
        "schema": AUDIT_SCHEMA,
        "path": str(path),
        "kind": _kind(path),
        "original_sha256": _hash(path),
    }
    try:
        receipt = _read(path)
    except Exception as exc:
        disposition = "unparseable" if path.suffix == ".json" and path.exists() else "unverified"
        return {**base, "disposition": disposition, "eligibility": "withheld", "status": "UNPARSEABLE" if disposition == "unparseable" else "UNVERIFIED", "error": f"{type(exc).__name__}: {exc}"}
    if path.name not in RECEIPT_NAMES:
        return {**base, "disposition": "unverified", "eligibility": "withheld", "status": receipt.get("status")}

    dependency_problems = _dependency_problems(path, receipt)
    issues = _execution_issues(receipt)
    disposition = _disposition(receipt.get("status"), issues, dependency_problems)
    original_copy = None
    if disposition == "revoked_false_green" and preserve_original:
        original_copy = path.with_name(f"{path.stem}.v1{path.suffix}")
        if not original_copy.exists():
            shutil.copy2(path, original_copy)
    return {
        **base,
        "original_status": receipt.get("status"),
        "winner": receipt.get("winner"),
        "disposition": disposition,
        "eligibility": "revoked" if disposition == "revoked_false_green" else "eligible" if disposition == "valid_under_original_contract" else "withheld",
        "execution_issues": issues,
        "dependency_problems": dependency_problems,
        "original_preserved_as": str(original_copy) if original_copy else None,
    }


def _apply_downstream_revocations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    revoked_dirs = {Path(entry["path"]).parent for entry in entries if entry.get("kind") == "run_receipt" and entry.get("disposition") == "revoked_false_green"}
    downstream_revocations: list[dict[str, Any]] = []
    for entry in entries:
        if not str(entry.get("kind", "")).startswith("downstream_"):
            continue
        entry_path = Path(entry["path"])
        source = next((run_dir for run_dir in revoked_dirs if run_dir == entry_path.parent or run_dir in entry_path.parents), None)
        if source is None:
            continue
        entry["disposition"] = "revoked_false_green"
        entry["eligibility"] = "revoked"
        entry["source_revocation"] = str(source / "run-receipt.json")
        downstream_revocations.append({"path": entry["path"], "source_revocation": entry["source_revocation"]})
    return downstream_revocations


def build_index(root: Path, *, preserve_originals: bool = False, extra_paths: list[Path] | None = None) -> dict[str, Any]:
    paths = _declared_paths(root, extra_paths)
    entries = [audit_artifact(path, preserve_original=preserve_originals) for path in paths]
    downstream_revocations = _apply_downstream_revocations(entries)
    counts = Counter(entry.get("disposition") for entry in entries)
    return {
        "schema": INDEX_SCHEMA,
        "status": "PASS",
        "recomputed": True,
        "archive_root": str(root),
        "declared_archive_scope": {
            "patterns": sorted(RECEIPT_NAMES | DOWNSTREAM_NAMES | {"*learning*.json"}),
            "extra_paths": [str(path) for path in extra_paths or []],
            "item_count": len(entries),
        },
        "disposition_counts": dict(sorted(counts.items())),
        "revoked_count": counts.get("revoked_false_green", 0),
        "unparseable_count": counts.get("unparseable", 0),
        "missing_dependency_count": counts.get("missing_dependency", 0),
        "unverified_count": counts.get("unverified", 0),
        "downstream_revocation_count": len(downstream_revocations),
        "downstream_revocations": downstream_revocations,
        "entries": entries,
        "created_at": gates.utc(),
        "non_claims": [
            "backfill inspects retained evidence without executing historical specimens",
            "revocation changes eligibility, not history",
            "nonzero or Docker FAIL labels without raw revocation evidence are withheld as unverified rather than revoked",
        ],
    }


def write_index_atomic(path: Path, index: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    gates.write_json(tmp, index)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--include", action="append", type=Path, default=[], help="additional declared historical artifact path to disposition")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--preserve-originals", action="store_true")
    args = parser.parse_args()
    index = build_index(args.root, preserve_originals=args.preserve_originals, extra_paths=args.include)
    write_index_atomic(args.out, index)
    print(json.dumps({"status": index["status"], "declared_items": index["declared_archive_scope"]["item_count"], "revoked_count": index["revoked_count"], "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
