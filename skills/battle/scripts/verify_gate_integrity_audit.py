#!/usr/bin/env python3
"""Recompute fail-closed gate integrity for production or backfill receipts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import backfill_run_receipt_gates as backfill  # noqa: E402
import run_small_medium_production_battle as gates  # noqa: E402

BACKFILL_INDEX_SCHEMA = "battle.production_scale.backfill_gate_index.v1"
ALLOWED_BACKFILL_DISPOSITIONS = {
    "valid_under_original_contract",
    "revoked_false_green",
    "unparseable",
    "missing_dependency",
    "unverified",
}
PRIMARY_RECEIPT_KINDS = {"run_receipt", "round_receipt", "scorekeeper_receipt"}


def _entry_path(index_path: Path, index: dict[str, Any], raw: Any) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    archive_root = Path(str(index.get("archive_root") or ""))
    candidates = [path]
    if archive_root:
        candidates.append(archive_root / path)
        if not archive_root.is_absolute():
            candidates.append(index_path.parent / archive_root / path)
    candidates.append(index_path.parent / path)
    return next((candidate for candidate in candidates if candidate.exists()), path)


def _archive_root(index_path: Path, index: dict[str, Any]) -> Path | None:
    raw = index.get("archive_root")
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else index_path.parent / path


def _entry_map(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(entry.get("path")): entry for entry in entries if entry.get("path")}


def _downstream_edges(items: Any) -> list[tuple[str, str]]:
    return sorted((str(item.get("path")), str(item.get("source_revocation"))) for item in (items or []) if item.get("path") and item.get("source_revocation"))


def _compare_recomputed_backfill(index_path: Path, index: dict[str, Any], entries: list[dict[str, Any]], problems: list[dict[str, Any]]) -> dict[str, Any] | None:
    root = _archive_root(index_path, index)
    if root is None or not root.exists():
        problems.append({"gate": "BACKFILL_ARCHIVE_ROOT_MISSING", "archive_root": index.get("archive_root")})
        return None
    expected = backfill.build_index(root, extra_paths=[Path(str(path)) for path in ((index.get("declared_archive_scope") or {}).get("extra_paths") or [])])
    observed_by_path = _entry_map(entries)
    expected_by_path = _entry_map(expected["entries"])
    observed_paths = set(observed_by_path)
    expected_paths = set(expected_by_path)
    if observed_paths != expected_paths:
        problems.append({
            "gate": "BACKFILL_RECOMPUTED_SCOPE_MISMATCH",
            "missing_paths": sorted(expected_paths - observed_paths),
            "unexpected_paths": sorted(observed_paths - expected_paths),
        })
    expected_counts = dict(sorted(Counter(entry.get("disposition") for entry in expected["entries"]).items()))
    if index.get("disposition_counts") != expected_counts:
        problems.append({"gate": "BACKFILL_RECOMPUTED_COUNT_MISMATCH", "expected": expected_counts, "observed": index.get("disposition_counts")})
    if index.get("downstream_revocation_count") != expected.get("downstream_revocation_count"):
        problems.append({"gate": "BACKFILL_RECOMPUTED_DOWNSTREAM_COUNT_MISMATCH", "expected": expected.get("downstream_revocation_count"), "observed": index.get("downstream_revocation_count")})
    if _downstream_edges(index.get("downstream_revocations")) != _downstream_edges(expected.get("downstream_revocations")):
        problems.append({"gate": "BACKFILL_RECOMPUTED_DOWNSTREAM_REVOCATIONS_MISMATCH", "expected": expected.get("downstream_revocations"), "observed": index.get("downstream_revocations")})
    for path, expected_entry in expected_by_path.items():
        observed_entry = observed_by_path.get(path)
        if observed_entry is None:
            continue
        for field in ("kind", "original_sha256", "disposition", "eligibility", "source_revocation"):
            if observed_entry.get(field) != expected_entry.get(field):
                problems.append({"gate": "BACKFILL_RECOMPUTED_ENTRY_MISMATCH", "path": path, "field": field, "expected": expected_entry.get(field), "observed": observed_entry.get(field)})
    return expected


def verify_backfill_index(index_path: Path) -> dict[str, Any]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    problems: list[dict[str, Any]] = []
    entries = index.get("entries") or []
    if index.get("schema") != BACKFILL_INDEX_SCHEMA:
        problems.append({"gate": "BACKFILL_SCHEMA", "reason": "not_a_backfill_index"})
    recomputed_index = _compare_recomputed_backfill(index_path, index, entries, problems)
    paths = [entry.get("path") for entry in entries]
    duplicates = sorted(path for path, count in Counter(paths).items() if path and count > 1)
    if duplicates:
        problems.append({"gate": "BACKFILL_DUPLICATE_ENTRY", "paths": duplicates})
    counts = Counter(entry.get("disposition") for entry in entries)
    if dict(sorted(counts.items())) != index.get("disposition_counts"):
        problems.append({"gate": "BACKFILL_COUNT_MISMATCH", "expected": dict(sorted(counts.items())), "observed": index.get("disposition_counts")})
    declared = index.get("declared_archive_scope", {}).get("item_count")
    if declared != len(entries):
        problems.append({"gate": "BACKFILL_SCOPE_COUNT_MISMATCH", "declared": declared, "observed": len(entries)})
    if recomputed_index and declared != recomputed_index.get("declared_archive_scope", {}).get("item_count"):
        problems.append({"gate": "BACKFILL_RECOMPUTED_DECLARED_SCOPE_MISMATCH", "expected": recomputed_index.get("declared_archive_scope", {}).get("item_count"), "observed": declared})
    for entry in entries:
        disposition = entry.get("disposition")
        raw_path = entry.get("path")
        entry_path = _entry_path(index_path, index, raw_path)
        if entry_path is None or not entry_path.is_file():
            problems.append({"gate": "BACKFILL_PATH_MISSING", "path": raw_path})
        else:
            actual_hash = gates.sha256_file(entry_path)
            expected_hash = entry.get("original_sha256")
            if not expected_hash:
                problems.append({"gate": "BACKFILL_MISSING_HASH", "path": raw_path})
            elif gates.normalize_sha256(str(expected_hash)) != actual_hash:
                problems.append({"gate": "BACKFILL_HASH_MISMATCH", "path": raw_path, "expected_sha256": expected_hash, "actual_sha256": actual_hash})
            if entry.get("kind") in PRIMARY_RECEIPT_KINDS:
                recomputed = backfill.audit_artifact(entry_path)
                if recomputed.get("disposition") != disposition:
                    problems.append({"gate": "BACKFILL_RECOMPUTED_DISPOSITION_MISMATCH", "path": raw_path, "expected": recomputed.get("disposition"), "observed": disposition})
        if disposition not in ALLOWED_BACKFILL_DISPOSITIONS:
            problems.append({"gate": "BACKFILL_UNKNOWN_DISPOSITION", "path": raw_path, "disposition": disposition})
        if disposition == "revoked_false_green" and entry.get("eligibility") != "revoked":
            problems.append({"gate": "BACKFILL_REVOCATION_NOT_WITHHELD", "path": raw_path})
        if disposition == "missing_dependency" and not entry.get("dependency_problems"):
            problems.append({"gate": "BACKFILL_MISSING_DEPENDENCY_UNEXPLAINED", "path": raw_path})
    downstream = index.get("downstream_revocations") or []
    for item in downstream:
        if not item.get("path") or not item.get("source_revocation"):
            problems.append({"gate": "BACKFILL_DOWNSTREAM_REVOCATION_UNBOUND", "item": item})
    audit = {
        "schema": "battle.gate_integrity_audit.v1",
        "status": "PASS" if not problems else "FAIL",
        "run_receipt": str(index_path),
        "run_receipt_sha256": gates.sha256_file(index_path),
        "scorekeeper_sha256": None,
        "false_green_parent_count": sum(1 for entry in entries if entry.get("kind") == "run_receipt" and entry.get("disposition") == "revoked_false_green"),
        "incomplete_gates": problems,
        "backfill_index": str(index_path),
        "backfill_index_sha256": gates.sha256_file(index_path),
        "declared_archive_scope_count": declared,
        "disposition_counts": dict(sorted(counts.items())),
        "downstream_revocation_count": len(downstream),
        "recomputed_archive_scope_count": recomputed_index.get("declared_archive_scope", {}).get("item_count") if recomputed_index else None,
        "recomputed_downstream_revocation_count": recomputed_index.get("downstream_revocation_count") if recomputed_index else None,
        "created_at": gates.utc(),
    }
    return audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_receipt", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.run_receipt.read_text(encoding="utf-8"))
    if payload.get("schema") == BACKFILL_INDEX_SCHEMA:
        audit = verify_backfill_index(args.run_receipt)
        if args.out:
            gates.write_json(args.out, audit)
    else:
        audit = gates.write_gate_integrity_audit(args.run_receipt.parent, payload)
        if args.out and args.out != args.run_receipt.parent / "gate-integrity-audit.json":
            gates.write_json(args.out, audit)
    print(json.dumps({"status": audit["status"], "false_green_parent_count": audit["false_green_parent_count"], "audit": str(args.out or audit.get("path") or args.run_receipt.parent / "gate-integrity-audit.json")}, indent=2))
    return 0 if audit["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
