"""Portable Battle qualification bundle builder and verifier."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

SCHEMA = "battle.comprehensive_web_review_bundle.v2"
REPO_REL = "skills/battle"
DEFAULT_SENTINEL = "COMPLETE_BATTLE_WEBGPT_R1_SENTINEL"
OLD_ATTEMPT_ID = "webgpt-r1-20260919T183646Z"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    except FileNotFoundError as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout


def _safe_rel(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _entry_id(category: str, logical_path: str, source_path: str) -> str:
    return hashlib.sha256(f"{category}\0{logical_path}\0{source_path}".encode()).hexdigest()[:20]


def _add_file(entries: list[dict[str, Any]], blobs: Path, path: Path, *, category: str, logical_path: str | None = None) -> str:
    source_path = str(path)
    logical = logical_path or source_path
    entry_id = _entry_id(category, logical, source_path)
    if any(entry["id"] == entry_id for entry in entries):
        return entry_id
    if not path.exists() or not path.is_file():
        entries.append({
            "id": entry_id,
            "category": category,
            "logical_path": logical,
            "source_path": source_path,
            "disposition": "unavailable",
        })
        return entry_id
    data = path.read_bytes()
    digest = sha256_bytes(data)
    blob = blobs / digest.split(":", 1)[1]
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        blob.write_bytes(data)
    entries.append({
        "id": entry_id,
        "category": category,
        "logical_path": logical,
        "source_path": source_path,
        "disposition": "available",
        "sha256": digest,
        "bytes": len(data),
        "blob_path": ("blobs/" + blob.relative_to(blobs.parent).as_posix()),
    })
    return entry_id


def _write_generated(entries: list[dict[str, Any]], blobs: Path, rel_path: str, payload: Any, *, category: str) -> str:
    generated = blobs.parent.parent / rel_path
    generated.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    generated.write_bytes(data)
    return _add_file(entries, blobs, generated, category=category, logical_path=rel_path)


def _files_under(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(child for child in path.rglob("*") if child.is_file())
    return sorted({p.resolve() for p in files}, key=lambda p: p.as_posix())


def _git_list(repo_root: Path, args: list[str]) -> list[Path]:
    code, out = _run(["git", *args], repo_root)
    if code != 0:
        return []
    return [repo_root / line for line in out.splitlines() if line.strip()]


def _battle_status_paths(repo_root: Path) -> list[Path]:
    source_prefixes = tuple(f"{REPO_REL}/{part}/" for part in ("src", "schemas", "tests", "scripts"))

    def in_source_set(path: Path) -> bool:
        rel = _safe_rel(path, repo_root)
        return bool(rel and rel.startswith(source_prefixes))

    modified: list[Path] = []
    code, out = _run(["git", "status", "--porcelain", "--", *source_prefixes], repo_root)
    if code == 0:
        for line in out.splitlines():
            if not line or line.startswith("?? "):
                continue
            raw = line[3:].strip()
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            path = repo_root / raw
            if in_source_set(path):
                modified.append(path)
    untracked = [path for path in _git_list(repo_root, ["ls-files", "--others", "--exclude-standard", *source_prefixes]) if in_source_set(path)]
    return _files_under([*modified, *untracked])


def _source_tree_digest(entries: list[dict[str, Any]]) -> str:
    source_entries = [
        {"logical_path": e["logical_path"], "sha256": e.get("sha256"), "disposition": e["disposition"]}
        for e in entries
        if e["category"] in {"battle_source", "battle_schema", "battle_test", "battle_script", "dirty_or_untracked_battle_file"}
    ]
    return sha256_bytes(_json_bytes(sorted(source_entries, key=lambda e: e["logical_path"])))


REFERENCE_KEYS = {
    "$ref",
    "archive_path",
    "artifact_path",
    "artifact_paths",
    "dependency_path",
    "dependency_paths",
    "file_path",
    "file_paths",
    "manifest_path",
    "proof_path",
    "proof_paths",
    "receipt_path",
    "receipt_paths",
    "referenced_path",
    "referenced_paths",
    "response_path",
    "schema_path",
    "source_file",
    "source_files",
}
LOCAL_REFERENCE_SUFFIXES = (".json", ".jsonl", ".md", ".txt", ".png", ".tar")


def _looks_like_local_reference(value: str) -> bool:
    if not value or any(ch.isspace() for ch in value):
        return False
    if value.startswith(("http://", "https://", "urn:", "#")):
        return False
    return "/" in value or value.endswith(LOCAL_REFERENCE_SUFFIXES)


def _extract_referenced_paths(value: Any, key: str | None = None) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child_key, item in value.items():
            found.update(_extract_referenced_paths(item, str(child_key)))
    elif isinstance(value, list):
        for item in value:
            found.update(_extract_referenced_paths(item, key))
    elif isinstance(value, str):
        if key in REFERENCE_KEYS and _looks_like_local_reference(value):
            found.add(value)
    return found


def _dependency_entry_id(raw: str, source_path: str) -> str:
    return _entry_id("transitive_receipt_dependency", raw, source_path)


def _add_unavailable_dependency(entries: list[dict[str, Any]], raw: str, *, reason: str) -> str:
    child_id = _dependency_entry_id(raw, raw)
    if not any(entry["id"] == child_id for entry in entries):
        entries.append({
            "id": child_id,
            "category": "transitive_receipt_dependency",
            "logical_path": raw,
            "source_path": raw,
            "disposition": "unavailable",
            "availability_reason": reason,
            "reference_classification": "explicit_json_reference_field",
        })
    return child_id


def _close_json_dependencies(entries: list[dict[str, Any]], blobs: Path, repo_root: Path, allowed_roots: list[Path]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    seen = {entry["id"] for entry in entries}
    changed = True
    while changed:
        changed = False
        for entry in list(entries):
            if entry.get("disposition") != "available" or not entry.get("source_path", "").endswith(".json"):
                continue
            try:
                data = _read_json(Path(entry["source_path"]))
            except Exception:
                continue
            for raw in sorted(_extract_referenced_paths(data)):
                candidates = []
                raw_path = Path(raw)
                if raw_path.is_absolute():
                    candidates.append(raw_path)
                else:
                    candidates.extend([repo_root / raw, repo_root / REPO_REL / raw, Path(entry["source_path"]).parent / raw])
                resolved_dependency: Path | None = None
                unavailable_reason = "missing"
                for candidate in candidates:
                    try:
                        resolved = candidate.resolve()
                    except OSError:
                        unavailable_reason = "unresolvable"
                        continue
                    if not resolved.exists() or not resolved.is_file():
                        continue
                    if not any(_safe_rel(resolved, root) is not None for root in allowed_roots):
                        unavailable_reason = "outside_allowed_roots"
                        continue
                    resolved_dependency = resolved
                    break
                if resolved_dependency is None:
                    child_id = _add_unavailable_dependency(entries, raw, reason=unavailable_reason)
                    seen.add(child_id)
                else:
                    child_id = _dependency_entry_id(str(resolved_dependency), str(resolved_dependency))
                    if child_id not in seen:
                        _add_file(entries, blobs, resolved_dependency, category="transitive_receipt_dependency")
                        seen.add(child_id)
                        changed = True
                edges.append({"from_entry_id": entry["id"], "to_entry_id": child_id, "kind": "referenced_path", "path": raw})
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for edge in edges:
        unique[(edge["from_entry_id"], edge["to_entry_id"], edge["kind"])] = edge
    return list(unique.values())


def _old_attempt_correction(attempt_dir: Path, attempt_id: str) -> tuple[dict[str, Any], dict[str, bool]]:
    transport = attempt_dir / "webgpt_transport_summary.json"
    meta = attempt_dir / "response.meta.json"
    inflight = attempt_dir / "webgpt_inflight.json"
    docs: dict[str, Any] = {}
    for name, path in {"transport": transport, "meta": meta, "inflight": inflight}.items():
        try:
            docs[name] = _read_json(path)
        except Exception as exc:
            docs[name] = {"unavailable": str(exc)}

    transport_submitted = bool(docs.get("transport", {}).get("submitted_to_chatgpt"))
    meta_submitted = bool(docs.get("meta", {}).get("submitted_to_chatgpt"))
    inflight_submitted = bool(docs.get("inflight", {}).get("submitted_to_chatgpt"))
    response_chars = int(docs.get("meta", {}).get("response_chars") or docs.get("meta", {}).get("response_character_count") or 0)
    sentinel = docs.get("meta", {}).get("sentinel") or docs.get("transport", {}).get("sentinel")
    states = {
        "submission_attempted": transport_submitted or meta_submitted or inflight_submitted,
        "submission_acknowledged": transport_submitted,
        "response_received": response_chars > 0,
        "sentinel_validated": bool(sentinel) and response_chars > 0,
    }
    correction = {
        "schema": "battle.web_review_attempt_correction.v1",
        "attempt_id": attempt_id,
        "mode": "append_only_correction_record",
        "source_files": [str(transport), str(meta), str(inflight)],
        "observed_contradictions": [
            "transport submitted_to_chatgpt differs from response.meta/webgpt_inflight" if transport_submitted != (meta_submitted and inflight_submitted) else None,
            "response metadata reports no response characters" if response_chars == 0 else None,
            "no validated sentinel-bearing response was available" if not states["sentinel_validated"] else None,
        ],
        "corrected_states": states,
        "disposition": "cannot_establish_completed_review" if not states["sentinel_validated"] else "sentinel_validated",
    }
    correction["observed_contradictions"] = [item for item in correction["observed_contradictions"] if item]
    return correction, states


def _manifest_digest(manifest: dict[str, Any]) -> str:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: (None if key in {"bundle_digest", "artifact_digests", "manifest_digest"} else scrub(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return sha256_bytes(_json_bytes(scrub(manifest)))


def _entry_ref(manifest_digest: str | None, entry_id: str | None, *, scope: str) -> dict[str, Any] | None:
    if entry_id is None:
        return None
    return {"manifest_digest": manifest_digest, "entry_id": entry_id, "scope": scope}


def _entry_refs(manifest_digest: str | None, entry_ids: list[str], *, scope: str) -> list[dict[str, Any]]:
    return [ref for entry_id in entry_ids if (ref := _entry_ref(manifest_digest, entry_id, scope=scope))]


def _environment(repo_root: Path) -> dict[str, Any]:
    head_code, head = _run(["git", "rev-parse", "HEAD"], repo_root)
    status_code, status = _run(["git", "status", "--porcelain", "--", REPO_REL], repo_root)
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "repo_root": str(repo_root),
        "git_head": head.strip() if head_code == 0 and head.strip() else "unavailable",
        "battle_status_sha256": sha256_bytes(status.encode("utf-8") if status_code == 0 else b""),
    }


def build_package(
    *,
    repo_root: Path,
    output_dir: Path,
    review_attempt_id: str,
    current_response: Path,
    ticket: Path,
    old_attempt_dir: Path | None = None,
    sentinel: str = DEFAULT_SENTINEL,
    command: list[str] | None = None,
    source_roots: list[Path] | None = None,
    proof_roots: list[Path] | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    battle_root = repo_root / REPO_REL
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="battle-qualification-package-") as tmp_raw:
        package_dir = Path(tmp_raw) / "package"
        blobs = package_dir / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        entries: list[dict[str, Any]] = []

        roots = source_roots or [battle_root / "src", battle_root / "schemas", battle_root / "tests", battle_root / "scripts"]
        for path in _files_under(roots):
            rel = path.relative_to(battle_root).as_posix() if _safe_rel(path, battle_root) else path.as_posix()
            category = "battle_source"
            if rel.startswith("schemas/"):
                category = "battle_schema"
            elif rel.startswith("tests/"):
                category = "battle_test"
            elif rel.startswith("scripts/"):
                category = "battle_script"
            _add_file(entries, blobs, path, category=category, logical_path=rel)

        for path in _battle_status_paths(repo_root):
            rel = path.relative_to(battle_root).as_posix() if _safe_rel(path, battle_root) else path.as_posix()
            _add_file(entries, blobs, path, category="dirty_or_untracked_battle_file", logical_path=rel)

        for path in _files_under(proof_roots or [battle_root / "proofs" / "qualification-bundles"]):
            rel = path.relative_to(battle_root).as_posix() if _safe_rel(path, battle_root) else path.as_posix()
            _add_file(entries, blobs, path, category="proof_transcript", logical_path=rel)

        ticket_id = _add_file(entries, blobs, ticket, category="review_ticket", logical_path="round-1/tickets/01-01-qualification-bundle-inspectable.md")
        response_id = _add_file(entries, blobs, current_response, category="current_review_response", logical_path="round-1/webgpt/response.md")
        current_meta = current_response.with_name("response.meta.json")
        _add_file(entries, blobs, current_meta, category="current_review_metadata", logical_path="round-1/webgpt/response.meta.json")

        old_states = {"submission_attempted": False, "submission_acknowledged": False, "response_received": False, "sentinel_validated": False}
        correction_id = None
        if old_attempt_dir is not None:
            for name in ["webgpt_transport_summary.json", "response.meta.json", "webgpt_inflight.json"]:
                _add_file(entries, blobs, old_attempt_dir / name, category="historical_review_attempt", logical_path=f"historical/{old_attempt_dir.name}/{name}")
            correction, old_states = _old_attempt_correction(old_attempt_dir, old_attempt_dir.name)
            correction_id = _write_generated(entries, blobs, f"corrections/{old_attempt_dir.name}.json", correction, category="append_only_correction_record")

        allowed_roots = [battle_root]
        if old_attempt_dir is not None:
            allowed_roots.append(old_attempt_dir.parent)
        allowed_roots.extend([ticket.parent.parent, current_response.parent.parent])
        dependency_edges = _close_json_dependencies(entries, blobs, repo_root, allowed_roots)

        response_text = current_response.read_text(encoding="utf-8", errors="replace") if current_response.exists() else ""
        current_states = {
            "submission_attempted": True,
            "submission_acknowledged": current_response.exists(),
            "response_received": bool(response_text.strip()),
            "sentinel_validated": sentinel in response_text,
        }
        packaged_source_tree_digest = _source_tree_digest(entries)
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "bundle_digest": None,
            "generated_at_unix": int(time.time()),
            "repository": _environment(repo_root),
            "source_tree_digest": packaged_source_tree_digest,
            "digest_scopes": {
                "bundle_digest": "canonical digest of this manifest and its containing qualification bundle metadata; it is not the digest of any reviewed subject",
                "source_tree_digest": "packaged source/schema/test/script/proof entries in this qualification package",
                "repository.battle_status_sha256": "current checkout git-status scope at package build time, not packaged bytes",
                "review_attempts.reviewed_subject_digest": "attachment or subject digest reviewed by that WebGPT response",
            },
            "entries": sorted(entries, key=lambda entry: (entry["category"], entry["logical_path"], entry["id"])),
            "dependency_edges": sorted(dependency_edges, key=lambda edge: (edge["from_entry_id"], edge["to_entry_id"], edge["kind"])),
            "verification_runs": [
                {
                    "attempt_id": "bundle-build",
                    "command": command or sys.argv,
                    "exit_status": 0,
                    "environment": _environment(repo_root),
                    "source_tree_digest": packaged_source_tree_digest,
                    "artifact_digests": {},
                }
            ],
            "review_attempts": [
                {
                    "attempt_id": old_attempt_dir.name if old_attempt_dir is not None else OLD_ATTEMPT_ID,
                    "bundle_digest": None,
                    "reviewed_subject_digest": None,
                    "states": old_states,
                    "completion_status": "not_completed",
                    "response_attempt_id": old_attempt_dir.name if old_attempt_dir is not None else OLD_ATTEMPT_ID,
                    "response_ref": None,
                    "response_entry_id": None,
                    "sentinel": sentinel,
                    "evidence_refs": [],
                    "evidence_entry_ids": [],
                    "correction_refs": _entry_refs(None, [correction_id] if correction_id else [], scope="containing_manifest_entry"),
                    "correction_entry_ids": [correction_id] if correction_id else [],
                    "disposition": "historical_metadata_corrected_append_only",
                },
                {
                    "attempt_id": review_attempt_id,
                    "bundle_digest": None,
                    "reviewed_subject_digest": digest if (digest := sha256_bytes(current_response.read_bytes())) else None,
                    "states": current_states,
                    "completion_status": "completed" if current_states["sentinel_validated"] else "not_completed",
                    "response_attempt_id": review_attempt_id,
                    "response_ref": _entry_ref(None, response_id, scope="containing_manifest_entry"),
                    "response_entry_id": response_id,
                    "sentinel": sentinel,
                    "evidence_refs": _entry_refs(None, [ticket_id, response_id], scope="containing_manifest_entry"),
                    "evidence_entry_ids": [ticket_id, response_id],
                    "correction_refs": [],
                    "correction_entry_ids": [],
                    "disposition": "current_review_response_bound_to_bundle" if current_states["sentinel_validated"] else "current_review_response_without_valid_sentinel",
                },
            ],
            "non_claims": [
                "bundle inspectability and byte continuity do not prove Battle correctness",
                "bundle verification does not prove provider success or Complete Battle acceptance",
                "historical correction records do not rewrite prior artifacts",
            ],
        }
        digest = _manifest_digest(manifest)
        manifest["bundle_digest"] = digest
        for attempt in manifest["review_attempts"]:
            attempt["bundle_digest"] = digest
            for key in ("response_ref",):
                if isinstance(attempt.get(key), dict) and attempt[key].get("manifest_digest") is None:
                    attempt[key]["manifest_digest"] = digest
            for key in ("evidence_refs", "correction_refs"):
                for ref in attempt.get(key, []) or []:
                    if ref.get("manifest_digest") is None:
                        ref["manifest_digest"] = digest
        manifest["verification_runs"][0]["artifact_digests"] = {"manifest": digest}
        manifest_path = package_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        temp_archive = output_dir / f"battle-qualification-package-{digest.split(':', 1)[1]}.tar.tmp"
        with tarfile.open(temp_archive, "w") as tar:
            for path in sorted(package_dir.rglob("*"), key=lambda p: p.relative_to(package_dir).as_posix()):
                info = tar.gettarinfo(str(path), arcname=path.relative_to(package_dir).as_posix())
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if path.is_file():
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        archive_digest = sha256_bytes(temp_archive.read_bytes())
        archive_path = output_dir / f"battle-qualification-package-{archive_digest.split(':', 1)[1]}.tar"
        os.replace(temp_archive, archive_path)
        manifest_copy = output_dir / f"battle-qualification-manifest-{digest.split(':', 1)[1]}.json"
        shutil.copy2(manifest_path, manifest_copy)
        build_receipt = {
            "schema": "battle.qualification_bundle_build_receipt.v1",
            "bundle_digest": digest,
            "archive_sha256": archive_digest,
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_copy),
            "manifest_sha256": sha256_bytes(manifest_copy.read_bytes()),
            "command": command or sys.argv,
            "exit_status": 0,
            "review_attempt_id": review_attempt_id,
        }
        receipt_path = output_dir / f"battle-qualification-build-receipt-{archive_digest.split(':', 1)[1]}.json"
        receipt_path.write_text(json.dumps(build_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        build_receipt["receipt_path"] = str(receipt_path)
        return build_receipt


def _load_manifest_from_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    blobs: dict[str, bytes] = {}
    with tarfile.open(path, "r") as tar:
        manifest_member = tar.getmember("manifest.json")
        manifest = json.loads(tar.extractfile(manifest_member).read().decode("utf-8"))  # type: ignore[union-attr]
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                if extracted is not None:
                    blobs[member.name] = extracted.read()
    return manifest, blobs


def _load_manifest_from_dir(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest = _read_json(path / "manifest.json")
    blobs = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return manifest, blobs


def _manifest_schema() -> dict[str, Any]:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "battle.comprehensive_web_review_bundle.v2.schema.json"
    return _read_json(schema_path)


def _environment_identity_present(environment: Any) -> bool:
    if not isinstance(environment, dict):
        return False
    required = ["python", "platform", "repo_root", "git_head", "battle_status_sha256"]
    return all(environment.get(key) for key in required)


def _verify_attempt_ref(ref: Any, bundle_digest: str | None, entry_ids: set[str], attempt_id: str, field: str, problems: list[str]) -> str | None:
    if ref is None:
        return None
    if not isinstance(ref, dict):
        problems.append(f"ambiguous inner-manifest refs for {attempt_id}: {field} must be manifest_digest+entry_id")
        return None
    if ref.get("manifest_digest") != bundle_digest:
        problems.append(f"cross-manifest review ref for {attempt_id}: {field}")
    entry_id = ref.get("entry_id")
    if not entry_id or entry_id not in entry_ids:
        problems.append(f"unresolved review ref for {attempt_id}: {field}:{entry_id}")
        return None
    return str(entry_id)


def verify_package(package: Path) -> dict[str, Any]:
    if package.is_dir():
        manifest, blobs = _load_manifest_from_dir(package)
    else:
        manifest, blobs = _load_manifest_from_archive(package)
    problems: list[str] = []
    try:
        validator = Draft202012Validator(_manifest_schema())
        for error in sorted(validator.iter_errors(manifest), key=lambda item: list(item.path)):
            location = "/".join(str(part) for part in error.path) or "manifest"
            problems.append(f"schema validation failed at {location}: {error.message}")
    except Exception as exc:
        problems.append(f"schema validation unavailable: {exc}")
    if manifest.get("schema") != SCHEMA:
        problems.append("schema mismatch")
    bundle_digest = manifest.get("bundle_digest")
    recalculated = _manifest_digest(manifest)
    if bundle_digest != recalculated:
        problems.append(f"bundle_digest mismatch expected {bundle_digest} got {recalculated}")

    entry_ids: set[str] = set()
    entries_by_id: dict[str, dict[str, Any]] = {}
    verified_entries: set[str] = set()
    for entry in manifest.get("entries", []):
        entry_id = entry.get("id")
        if not entry_id or entry_id in entry_ids:
            problems.append(f"duplicate or missing entry id: {entry_id}")
            continue
        entry_ids.add(entry_id)
        entries_by_id[entry_id] = entry
        if entry.get("disposition") == "available":
            blob_path = entry.get("blob_path")
            data = blobs.get(blob_path)
            if data is None:
                problems.append(f"missing blob for {entry_id}: {blob_path}")
                continue
            digest = sha256_bytes(data)
            if digest != entry.get("sha256"):
                problems.append(f"sha256 mismatch for {entry_id}: expected {entry.get('sha256')} got {digest}")
                continue
            if len(data) != entry.get("bytes"):
                problems.append(f"byte count mismatch for {entry_id}")
                continue
            verified_entries.add(entry_id)

    for edge in manifest.get("dependency_edges", []):
        from_id = edge.get("from_entry_id")
        to_id = edge.get("to_entry_id")
        if from_id not in entry_ids or to_id not in entry_ids:
            problems.append(f"unresolved dependency edge: {edge}")
            continue
        target = entries_by_id[to_id]
        if target.get("disposition") != "available":
            problems.append(f"required dependency unavailable for edge: {edge}")
        elif to_id not in verified_entries:
            problems.append(f"required dependency bytes unverified for edge: {edge}")

    for run in manifest.get("verification_runs", []):
        attempt_id = run.get("attempt_id")
        if not run.get("command"):
            problems.append(f"verification run {attempt_id} missing command")
        if not isinstance(run.get("exit_status"), int):
            problems.append(f"verification run {attempt_id} missing exit_status")
        if not _environment_identity_present(run.get("environment")):
            problems.append(f"verification run {attempt_id} missing environment identity")
        if run.get("source_tree_digest") != manifest.get("source_tree_digest"):
            problems.append(f"verification run {attempt_id} source_tree_digest is not bound to manifest")
        artifact_digests = run.get("artifact_digests")
        if not isinstance(artifact_digests, dict) or artifact_digests.get("manifest") != bundle_digest:
            problems.append(f"verification run {attempt_id} manifest digest is not bound to bundle")

    attempts = manifest.get("review_attempts", [])
    seen_attempts: set[str] = set()
    entries_by_id = {entry.get("id"): entry for entry in manifest.get("entries", [])}
    for attempt in attempts:
        attempt_id = attempt.get("attempt_id")
        if not attempt_id or attempt_id in seen_attempts:
            problems.append(f"duplicate or missing review attempt id: {attempt_id}")
            continue
        seen_attempts.add(attempt_id)
        if attempt.get("bundle_digest") != bundle_digest:
            problems.append(f"review attempt {attempt_id} is not bound to bundle digest")
        if attempt.get("response_attempt_id") != attempt_id:
            problems.append(f"cross-attempt response substitution for {attempt_id}")
        states = attempt.get("states", {})
        if states.get("sentinel_validated") and not states.get("response_received"):
            problems.append(f"sentinel without response for {attempt_id}")
        response_entry_id = _verify_attempt_ref(attempt.get("response_ref"), bundle_digest, entry_ids, attempt_id, "response_ref", problems)
        if attempt.get("response_entry_id") and response_entry_id and attempt.get("response_entry_id") != response_entry_id:
            problems.append(f"ambiguous response entry id for {attempt_id}")
        for key in ("evidence_refs", "correction_refs"):
            for ref in attempt.get(key, []) or []:
                _verify_attempt_ref(ref, bundle_digest, entry_ids, attempt_id, key, problems)
        for legacy_key, ref_key in (("evidence_entry_ids", "evidence_refs"), ("correction_entry_ids", "correction_refs")):
            legacy_ids = [item for item in attempt.get(legacy_key, []) or [] if item]
            explicit_ids = [item.get("entry_id") for item in attempt.get(ref_key, []) or [] if isinstance(item, dict)]
            if legacy_ids and legacy_ids != explicit_ids:
                problems.append(f"ambiguous inner-manifest refs for {attempt_id}: {legacy_key} must match explicit {ref_key}")
        if attempt.get("completion_status") == "completed" and not attempt.get("reviewed_subject_digest"):
            problems.append(f"completed review missing reviewed_subject_digest for {attempt_id}")
        if states.get("response_received"):
            response_entry = entries_by_id.get(response_entry_id)
            if not response_entry or response_entry.get("disposition") != "available":
                problems.append(f"missing response entry for {attempt_id}")
            elif states.get("sentinel_validated"):
                data = blobs.get(response_entry.get("blob_path"), b"").decode("utf-8", errors="replace")
                if attempt.get("sentinel") not in data:
                    problems.append(f"sentinel not present in response for {attempt_id}")
        if attempt.get("completion_status") == "completed" and not (states.get("response_received") and states.get("sentinel_validated")):
            problems.append(f"completed review without response and valid sentinel for {attempt_id}")

    return {
        "schema": "battle.qualification_bundle_verification_receipt.v1",
        "package": str(package),
        "bundle_digest": bundle_digest,
        "archive_sha256": sha256_bytes(package.read_bytes()) if package.is_file() else None,
        "passed": not problems,
        "problems": problems,
        "entry_count": len(manifest.get("entries", [])),
        "dependency_edge_count": len(manifest.get("dependency_edges", [])),
    }


def build_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a portable Battle qualification package")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--review-attempt-id", required=True)
    parser.add_argument("--current-response", type=Path, required=True)
    parser.add_argument("--ticket", type=Path, required=True)
    parser.add_argument("--old-attempt-dir", type=Path)
    args = parser.parse_args(argv)
    receipt = build_package(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        review_attempt_id=args.review_attempt_id,
        current_response=args.current_response,
        ticket=args.ticket,
        old_attempt_dir=args.old_attempt_dir,
        command=[sys.executable, *sys.argv],
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a portable Battle qualification package")
    parser.add_argument("package", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    receipt = verify_package(args.package)
    receipt["command"] = [sys.executable, *sys.argv]
    receipt["exit_status"] = 0 if receipt["passed"] else 1
    text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if receipt["passed"] else 1
