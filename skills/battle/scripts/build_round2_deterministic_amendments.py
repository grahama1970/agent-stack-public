#!/usr/bin/env python3
"""Assemble locally resolvable WebGPT round-2 deterministic amendments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from battle_skill.qualification_bundle import build_package, verify_package  # noqa: E402

ROUND2 = Path("/mnt/storage12tb/skills/battle/complete-review/round-2")
ROUND1 = Path("/mnt/storage12tb/skills/battle/complete-review/round-1")
STORAGE = Path("/mnt/storage12tb/skills/battle")
DEFAULT_OUTPUT = ROUND2 / "deterministic-amendments-implementation.json"
DELIVERY_DIR = ROUND2 / "delivery"
REVIEW_ATTEMPT_ID = "webgpt-r1-authoritative-20260919T230916Z"
OLD_ATTEMPT_DIR = Path("/mnt/storage12tb/skills/battle/complete-review/webgpt-r1-20260919T183646Z")

_DIGEST_KEYS = {"bundle_digest", "artifact_digests", "manifest_digest"}
_SOURCE_CATEGORIES = {"battle_source", "battle_schema", "battle_test", "battle_script", "dirty_or_untracked_battle_file"}


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not a JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_digest(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _scrub_for_digest(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: (None if key in _DIGEST_KEYS else _scrub_for_digest(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_for_digest(item) for item in value]
    return value


def manifest_digest(value: dict[str, Any]) -> str:
    return _json_digest(_scrub_for_digest(value))


def source_tree_digest(entries: list[dict[str, Any]]) -> str:
    source_entries = [
        {"logical_path": e.get("logical_path"), "sha256": e.get("sha256"), "disposition": e.get("disposition")}
        for e in entries
        if e.get("category") in _SOURCE_CATEGORIES
    ]
    return _json_digest(sorted(source_entries, key=lambda e: str(e.get("logical_path") or "")))


def _entries_by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(entry.get("id")): entry for entry in doc.get("entries", []) if entry.get("id")}


def _entry_id(category: str, logical_path: str, source_path: str) -> str:
    return hashlib.sha256(f"{category}\0{logical_path}\0{source_path}".encode()).hexdigest()[:20]


def _file_entry(path: Path, *, category: str, logical_path: str, package_part: str | None = None) -> dict[str, Any]:
    entry = {
        "id": _entry_id(category, logical_path, str(path)),
        "category": category,
        "logical_path": logical_path,
        "source_path": str(path),
        "blob_path": str(path),
        "disposition": "available",
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    if package_part:
        entry["package_part"] = package_part
    return entry


def _resolve_ref(entry_id: str | None, *, outer: dict[str, Any], inner: dict[str, Any], inner_digest: str) -> dict[str, Any] | None:
    if not entry_id:
        return None
    outer_entries = _entries_by_id(outer)
    inner_entries = _entries_by_id(inner)
    if entry_id in outer_entries:
        return {"manifest_digest": None, "entry_id": entry_id, "scope": "round2_outer_bundle_entry"}
    if entry_id in inner_entries:
        return {"manifest_digest": inner_digest, "entry_id": entry_id, "scope": "qualification_package_manifest_entry"}
    return {"manifest_digest": None, "entry_id": entry_id, "scope": "unresolved"}


def build_qualification_artifacts(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    receipt = build_package(
        repo_root=REPO,
        output_dir=output_dir,
        review_attempt_id=REVIEW_ATTEMPT_ID,
        current_response=ROUND1 / "webgpt" / "response.md",
        ticket=ROUND1 / "tickets" / "01-01-qualification-bundle-inspectable.md",
        old_attempt_dir=OLD_ATTEMPT_DIR,
        command=[sys.executable, "skills/battle/scripts/build_qualification_bundle.py", "--repo-root", str(REPO)],
    )
    archive = Path(receipt["archive_path"])
    verification = verify_package(archive)
    verification["command"] = [sys.executable, "skills/battle/scripts/verify_qualification_bundle.py", str(archive)]
    verification["exit_status"] = 0 if verification["passed"] else 1
    verification_path = output_dir / "verification-receipt.json"
    write_json(verification_path, verification)
    if not verification["passed"]:
        raise RuntimeError(f"qualification package did not verify: {verification['problems']}")
    receipt["verification_receipt_path"] = str(verification_path)
    receipt["verification_receipt_sha256"] = sha256_file(verification_path)
    return receipt


def _replace_qualification_entries(bundle: dict[str, Any], q_receipt: dict[str, Any]) -> None:
    part_paths = {
        "archive": Path(q_receipt["archive_path"]),
        "manifest": Path(q_receipt["manifest_path"]),
        "build_receipt": Path(q_receipt["receipt_path"]),
        "verification_receipt": Path(q_receipt["verification_receipt_path"]),
    }
    replacement = [
        _file_entry(path, category="portable_qualification_package", logical_path=f"round-2/qualification-package/{path.name}", package_part=part)
        for part, path in part_paths.items()
    ]
    kept = [entry for entry in bundle.get("entries", []) if entry.get("category") != "portable_qualification_package"]
    bundle["entries"] = sorted([*kept, *replacement], key=lambda e: (str(e.get("category")), str(e.get("logical_path")), str(e.get("id"))))
    package = bundle.setdefault("portable_qualification_package", {})
    package.update({
        "package_bundle_digest": q_receipt["bundle_digest"],
        "archive_sha256": q_receipt["archive_sha256"],
        "archive_path": str(part_paths["archive"]),
        "manifest_path": str(part_paths["manifest"]),
        "verification_receipt_path": str(part_paths["verification_receipt"]),
        "verification_passed": True,
    })


def _dedupe_attempts(bundle: dict[str, Any]) -> None:
    attempts = bundle.get("review_attempts", []) or []
    by_id: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        attempt_id = str(attempt.get("attempt_id"))
        if attempt_id not in by_id:
            by_id[attempt_id] = attempt
            continue
        current = by_id[attempt_id]
        if current.get("disposition") == "current_review_response_bound_to_bundle":
            by_id[attempt_id] = attempt
    bundle["review_attempts"] = list(by_id.values())


def _bind_attempt_refs(bundle: dict[str, Any], qualification: dict[str, Any], inner_digest: str) -> list[dict[str, str]]:
    unresolved: list[dict[str, str]] = []
    inner_attempts = {item.get("attempt_id"): item for item in qualification.get("review_attempts", []) if isinstance(item, dict)}
    for attempt in bundle.get("review_attempts", []) or []:
        attempt.setdefault("reviewed_subject_digest", attempt.get("reviewed_bundle_sha256_in_response"))
        response_ref = _resolve_ref(attempt.get("response_entry_id"), outer=bundle, inner=qualification, inner_digest=inner_digest)
        if response_ref:
            attempt["response_ref"] = response_ref
        for legacy_key, ref_key in (("evidence_entry_ids", "evidence_refs"), ("correction_entry_ids", "correction_refs")):
            raw_ids = [entry_id for entry_id in attempt.get(legacy_key, []) or [] if entry_id]
            refs = [_resolve_ref(entry_id, outer=bundle, inner=qualification, inner_digest=inner_digest) for entry_id in raw_ids]
            if raw_ids and all(ref and ref.get("scope") == "unresolved" for ref in refs):
                inner_attempt = inner_attempts.get(attempt.get("attempt_id")) or {}
                replacement_ids = [entry_id for entry_id in inner_attempt.get(legacy_key, []) or [] if entry_id]
                if replacement_ids:
                    raw_ids = replacement_ids
                    attempt[legacy_key] = raw_ids
                    refs = [_resolve_ref(entry_id, outer=bundle, inner=qualification, inner_digest=inner_digest) for entry_id in raw_ids]
            attempt[ref_key] = [ref for ref in refs if ref]
        for key in ("response_ref", "evidence_refs", "correction_refs"):
            refs = attempt.get(key) if isinstance(attempt.get(key), list) else [attempt.get(key)]
            for ref in refs:
                if isinstance(ref, dict) and ref.get("scope") == "unresolved":
                    unresolved.append({"attempt_id": str(attempt.get("attempt_id")), "entry_id": str(ref.get("entry_id")), "field": key})
    return unresolved


def repair_bundle_refs(bundle_path: Path, qualification_manifest_path: Path, q_receipt: dict[str, Any], out_path: Path) -> dict[str, Any]:
    bundle = read_json(bundle_path)
    qualification = read_json(qualification_manifest_path)
    inner_digest = str(qualification.get("bundle_digest"))
    _replace_qualification_entries(bundle, q_receipt)
    _dedupe_attempts(bundle)
    unresolved = _bind_attempt_refs(bundle, qualification, inner_digest)
    src_digest = source_tree_digest(bundle.get("entries", []))
    bundle["source_tree_digest"] = src_digest
    for run in bundle.get("verification_runs", []) or []:
        if isinstance(run, dict) and "source_tree_digest" in run:
            run["source_tree_digest"] = src_digest
    bundle["digest_scopes"] = {
        "bundle_digest": "round-2 JSON review bundle digest over this repaired bundle with digest fields scrubbed",
        "portable_qualification_package.package_bundle_digest": "inner qualification package manifest digest that resolves qualification-package entry refs",
        "review_attempts.reviewed_subject_digest": "subject attachment digest reviewed by that response; not the containing bundle digest unless explicitly equal",
        "current_skills_battle_tree.tree_digest": "current-tree inventory scope, not packaged source-tree scope",
        "source_tree_digest": "round-2 JSON bundle source-entry scope; empty when source bytes live only in the portable qualification package",
    }
    bundle["receiving_side_verification"] = {
        "schema": "battle.round2.receiving_side_reference_verification.v1",
        "status": "PASS" if not unresolved else "FAIL",
        "inner_qualification_manifest_digest": inner_digest,
        "unresolved_refs": unresolved,
        "rejects": [
            "bare inner-manifest entry ids without manifest_digest",
            "cross-attempt response substitution",
            "refs whose entry_id is absent from the named manifest digest",
        ],
    }
    digest = ""
    for _ in range(3):
        digest = manifest_digest(bundle)
        bundle["bundle_digest"] = digest
        for attempt in bundle.get("review_attempts", []) or []:
            attempt["bundle_digest"] = digest
            attempt.pop("containing_bundle_digest", None)
            for key in ("response_ref",):
                if isinstance(attempt.get(key), dict) and attempt[key].get("scope") == "round2_outer_bundle_entry":
                    attempt[key]["manifest_digest"] = digest
            for key in ("evidence_refs", "correction_refs"):
                for ref in attempt.get(key, []) or []:
                    if ref.get("scope") == "round2_outer_bundle_entry":
                        ref["manifest_digest"] = digest
        for run in bundle.get("verification_runs", []) or []:
            if isinstance(run.get("artifact_digests"), dict):
                run["artifact_digests"]["manifest"] = digest
        if manifest_digest(bundle) == digest:
            break
    write_json(out_path, bundle)
    return bundle["receiving_side_verification"] | {"outer_bundle_digest": digest, "source_tree_digest": src_digest}


def build_delivery_archive(*, repaired_bundle_path: Path, qualification_dir: Path, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [
        (repaired_bundle_path, "round-2/battle-webgpt-review-bundle.repaired.json"),
        (ROUND2 / "battle-webgpt-review-bundle.json", "round-2/battle-webgpt-review-bundle.original.json"),
        (ROUND2 / "webgpt" / "response.md", "round-2/webgpt/response.md"),
        (ROUND2 / "webgpt-review-result.json", "round-2/webgpt-review-result.json"),
        (ROUND2 / "ticket-verdict-ledger.json", "round-2/ticket-verdict-ledger.json"),
        (ROUND2 / "current-skills-battle-inventory.json", "round-2/current-skills-battle-inventory.json"),
    ]
    files.extend((path, f"round-2/qualification-package/{path.name}") for path in sorted(qualification_dir.glob("battle-qualification-*")))
    files.append((qualification_dir / "verification-receipt.json", "round-2/qualification-package/verification-receipt.json"))
    files.extend((path, f"historical-original-scope/{path.name}") for path in sorted(STORAGE.glob("beyond-brief-campaign-20260912*.json")))
    with tempfile.TemporaryDirectory(prefix="battle-round2-delivery-") as tmp_raw:
        tmp = Path(tmp_raw)
        entries = []
        for src, rel in files:
            if not src.exists() or not src.is_file():
                entries.append({"path": rel, "source": str(src), "disposition": "unavailable"})
                continue
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            entries.append({"path": rel, "source": str(src), "disposition": "available", "sha256": sha256_file(src), "bytes": src.stat().st_size})
        root_manifest = {
            "schema": "battle.round2.delivery_manifest.v1",
            "created_at": utc(),
            "entries": entries,
            "non_claims": [
                "delivery archive does not close Tau/provider blockers",
                "delivery archive does not close authorized QEMU blocker",
                "measured learning benefit remains NOT_CLAIMED",
            ],
        }
        write_json(tmp / "manifest.json", root_manifest)
        temp_archive = out_dir / "battle-round2-deterministic-amendments.tar.tmp"
        with tarfile.open(temp_archive, "w") as tar:
            for path in sorted(tmp.rglob("*"), key=lambda p: p.relative_to(tmp).as_posix()):
                info = tar.gettarinfo(str(path), arcname=path.relative_to(tmp).as_posix())
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if path.is_file():
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        digest = sha256_file(temp_archive)
        archive = out_dir / f"battle-round2-deterministic-amendments-{digest.split(':', 1)[1]}.tar"
        os.replace(temp_archive, archive)
    return {"path": str(archive), "sha256": digest, "entries": len([item for item in entries if item["disposition"] == "available"]), "manifest_schema": "battle.round2.delivery_manifest.v1"}


def validate_receiving_side_archive(archive: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="battle-round2-recv-") as tmp_raw:
        recv = Path(tmp_raw)
        with tarfile.open(archive, "r") as tar:
            tar.extractall(recv, filter="data")
        manifest = read_json(recv / "manifest.json")
        problems: list[str] = []
        for entry in manifest.get("entries", []):
            rel = entry.get("path")
            if entry.get("disposition") != "available":
                problems.append(f"unavailable manifest entry: {rel}")
                continue
            path = recv / str(rel)
            if not path.is_file():
                problems.append(f"missing manifest entry: {rel}")
                continue
            if path.stat().st_size != entry.get("bytes"):
                problems.append(f"byte count mismatch: {rel}")
            if sha256_file(path) != entry.get("sha256"):
                problems.append(f"sha256 mismatch: {rel}")
        repaired = read_json(recv / "round-2" / "battle-webgpt-review-bundle.repaired.json")
        if repaired.get("bundle_digest") != manifest_digest(repaired):
            problems.append("repaired bundle_digest mismatch")
        if repaired.get("source_tree_digest") != source_tree_digest(repaired.get("entries", [])):
            problems.append("repaired source_tree_digest mismatch")
        attempt_ids = [a.get("attempt_id") for a in repaired.get("review_attempts", [])]
        if len(attempt_ids) != len(set(attempt_ids)):
            problems.append("duplicate review_attempt attempt_id")
        for attempt in repaired.get("review_attempts", []):
            if attempt.get("completion_status") == "completed" and not attempt.get("reviewed_subject_digest"):
                problems.append(f"completed outer attempt missing reviewed_subject_digest: {attempt.get('attempt_id')}")
        packages = sorted((recv / "round-2" / "qualification-package").glob("battle-qualification-package-*.tar"))
        if not packages:
            problems.append("missing inner qualification archive")
            q_receipt = None
        else:
            q_receipt = verify_package(packages[-1])
            q_receipt["package"] = f"{archive}!/round-2/qualification-package/{packages[-1].name}"
            if not q_receipt["passed"]:
                problems.extend(f"inner qualification: {problem}" for problem in q_receipt["problems"])
    return {
        "schema": "battle.round2.receiving_side_archive_validation.v1",
        "status": "PASS" if not problems else "FAIL",
        "archive": str(archive),
        "archive_sha256": sha256_file(archive),
        "root_manifest_entries": len(manifest.get("entries", [])),
        "problems": problems,
        "inner_qualification_verification": q_receipt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delivery-dir", type=Path, default=DELIVERY_DIR)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="battle-round2-qualification-") as q_tmp:
        q_tmp_dir = Path(q_tmp)
        q_receipt = build_qualification_artifacts(q_tmp_dir)
        q_dir = args.delivery_dir / "qualification-package.round2-repaired"
        if q_dir.exists():
            shutil.rmtree(q_dir)
        shutil.copytree(q_tmp_dir, q_dir)
        for key in ("archive_path", "manifest_path", "receipt_path", "verification_receipt_path"):
            q_receipt[key] = str(q_dir / Path(q_receipt[key]).name)
        q_manifest = Path(q_receipt["manifest_path"])
        repaired_bundle = args.delivery_dir / "battle-webgpt-review-bundle.round2-repaired.json"
        verification = repair_bundle_refs(ROUND2 / "battle-webgpt-review-bundle.json", q_manifest, q_receipt, repaired_bundle)
        delivery = build_delivery_archive(repaired_bundle_path=repaired_bundle, qualification_dir=q_dir, out_dir=args.delivery_dir)
    receiving_validation = validate_receiving_side_archive(Path(delivery["path"]))
    result = {
        "schema": "battle.round2.deterministic_amendments_implementation.v1",
        "status": "PASS" if verification["status"] == "PASS" and receiving_validation["status"] == "PASS" else "FAIL",
        "created_at": utc(),
        "amendments": {
            "A1": {"status": verification["status"], "receiving_side_verification": verification, "receiving_side_archive_validation": receiving_validation, "delivery_archive": delivery},
            "A8": {"status": "BOUND_LOCALLY", "measured_benefit": "NOT_CLAIMED"},
            "A9": {"status": "SUPPORTED_BY_JOINED_AUDIT_COMMAND", "extra_scope": [str(path) for path in sorted(STORAGE.glob("beyond-brief-campaign-20260912*.json"))]},
            "A10": {"status": "SUPPORTED_BY_ACCEPTANCE_RECEIPT", "tau_provider_qemu_release": "BLOCKED"},
        },
        "delivery_archive": delivery,
        "qualification_package": q_receipt,
        "blockers_preserved": [
            "amendments 2/3: authorized live Tau/provider proof remains blocked",
            "amendment 6: authorized QEMU base-image/authorization proof remains blocked",
        ],
        "non_claims": ["release not authorized", "campaign completion not claimed", "measured learning benefit NOT_CLAIMED"],
    }
    write_json(args.out, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
