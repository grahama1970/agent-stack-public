from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from battle_skill.qualification_bundle import build_package, verify_package  # noqa: E402


def _old_attempt(root: Path) -> Path:
    old = root / "old-attempt"
    old.mkdir()
    (old / "webgpt_transport_summary.json").write_text(json.dumps({"submitted_to_chatgpt": True}), encoding="utf-8")
    (old / "response.meta.json").write_text(json.dumps({"submitted_to_chatgpt": False, "response_chars": 0}), encoding="utf-8")
    (old / "webgpt_inflight.json").write_text(json.dumps({"submitted_to_chatgpt": False}), encoding="utf-8")
    return old


def _package(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    src = repo / "skills" / "battle" / "src" / "battle_skill"
    schemas = repo / "skills" / "battle" / "schemas"
    tests = repo / "skills" / "battle" / "tests"
    scripts = repo / "skills" / "battle" / "scripts"
    for path in [src, schemas, tests, scripts]:
        path.mkdir(parents=True)
    (src / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (schemas / "demo.schema.json").write_text("{}\n", encoding="utf-8")
    (tests / "test_demo.py").write_text("def test_demo(): assert True\n", encoding="utf-8")
    (scripts / "demo.py").write_text("print('demo')\n", encoding="utf-8")

    review = tmp_path / "review"
    review.mkdir()
    response = review / "response.md"
    response.write_text("COMPLETE_BATTLE_WEBGPT_R1_SENTINEL\n", encoding="utf-8")
    (review / "response.meta.json").write_text(json.dumps({"response_chars": 36}), encoding="utf-8")
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    receipt = build_package(
        repo_root=repo,
        output_dir=tmp_path / "out",
        review_attempt_id="current-attempt",
        current_response=response,
        ticket=ticket,
        old_attempt_dir=_old_attempt(tmp_path),
        source_roots=[src, schemas, tests, scripts],
        proof_roots=[],
        command=["build-test"],
    )
    return Path(receipt["archive_path"])


def _extract(archive: Path, dest: Path) -> Path:
    root = dest / "pkg"
    root.mkdir(parents=True)
    with tarfile.open(archive, "r") as tar:
        tar.extractall(root, filter="data")
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _save_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_verifier_accepts_intact_qualification_package(tmp_path: Path) -> None:
    archive = _package(tmp_path)

    receipt = verify_package(archive)

    root = _extract(archive, tmp_path / "extract-intact")
    manifest = _manifest(root)
    current = next(item for item in manifest["review_attempts"] if item["attempt_id"] == "current-attempt")
    assert current["response_ref"] == {"manifest_digest": manifest["bundle_digest"], "entry_id": current["response_entry_id"], "scope": "containing_manifest_entry"}
    assert current["reviewed_subject_digest"].startswith("sha256:")
    assert "review_attempts.reviewed_subject_digest" in manifest["digest_scopes"]
    assert receipt["passed"] is True
    assert receipt["entry_count"] >= 8


def test_verifier_rejects_missing_blob(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    blob_path = next(entry["blob_path"] for entry in manifest["entries"] if entry["disposition"] == "available")
    (root / blob_path).unlink()

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("missing blob" in problem for problem in receipt["problems"])


def test_verifier_rejects_altered_source_or_schema_bytes(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    source_entry = next(entry for entry in manifest["entries"] if entry["category"] == "battle_source")
    (root / source_entry["blob_path"]).write_text("tampered\n", encoding="utf-8")

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("sha256 mismatch" in problem for problem in receipt["problems"])


def test_builder_records_unavailable_explicit_dependency_without_prose_false_positive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "skills" / "battle" / "src" / "battle_skill"
    schemas = repo / "skills" / "battle" / "schemas"
    tests = repo / "skills" / "battle" / "tests"
    scripts = repo / "skills" / "battle" / "scripts"
    proofs = repo / "skills" / "battle" / "proofs"
    for path in [src, schemas, tests, scripts, proofs]:
        path.mkdir(parents=True)
    (src / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (schemas / "demo.schema.json").write_text("{}\n", encoding="utf-8")
    (tests / "test_demo.py").write_text("def test_demo(): assert True\n", encoding="utf-8")
    (scripts / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    (proofs / "receipt.json").write_text(
        json.dumps({"proof_path": "missing-proof.json", "notes": "this prose mentions not/a/required/path.md"}),
        encoding="utf-8",
    )
    review = tmp_path / "review"
    review.mkdir()
    response = review / "response.md"
    response.write_text("COMPLETE_BATTLE_WEBGPT_R1_SENTINEL\n", encoding="utf-8")
    (review / "response.meta.json").write_text(json.dumps({"response_chars": 36}), encoding="utf-8")
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")

    archive = Path(build_package(
        repo_root=repo,
        output_dir=tmp_path / "out",
        review_attempt_id="current-attempt",
        current_response=response,
        ticket=ticket,
        old_attempt_dir=_old_attempt(tmp_path),
        source_roots=[src, schemas, tests, scripts],
        proof_roots=[proofs],
        command=["build-test"],
    )["archive_path"])
    root = _extract(archive, tmp_path / "extract")
    manifest = _manifest(root)

    unavailable = [entry for entry in manifest["entries"] if entry["disposition"] == "unavailable"]
    edge_paths = {edge.get("path") for edge in manifest["dependency_edges"]}
    receipt = verify_package(root)

    assert any(entry["logical_path"] == "missing-proof.json" for entry in unavailable)
    assert "missing-proof.json" in edge_paths
    assert "not/a/required/path.md" not in edge_paths
    assert receipt["passed"] is False
    assert any("required dependency unavailable" in problem for problem in receipt["problems"])


def test_verifier_rejects_unresolved_transitive_dependency(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    manifest["dependency_edges"].append({"from_entry_id": manifest["entries"][0]["id"], "to_entry_id": "missing", "kind": "referenced_path"})
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("unresolved dependency edge" in problem for problem in receipt["problems"])


def test_verifier_rejects_dependency_edge_to_unavailable_entry(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    manifest["entries"].append({
        "id": "unavailable-dependency",
        "category": "transitive_receipt_dependency",
        "logical_path": "missing.json",
        "source_path": "missing.json",
        "disposition": "unavailable",
    })
    manifest["dependency_edges"].append({
        "from_entry_id": manifest["entries"][0]["id"],
        "to_entry_id": "unavailable-dependency",
        "kind": "referenced_path",
        "path": "missing.json",
    })
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("required dependency unavailable" in problem for problem in receipt["problems"])


def test_verifier_rejects_cross_attempt_response_substitution(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    manifest["review_attempts"][1]["response_attempt_id"] = "other-attempt"
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("cross-attempt response substitution" in problem for problem in receipt["problems"])


def test_verifier_rejects_ambiguous_or_cross_manifest_review_refs(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    current = manifest["review_attempts"][1]
    current["response_ref"] = {"manifest_digest": "sha256:" + "0" * 64, "entry_id": current["response_entry_id"], "scope": "inner_manifest_entry"}
    current["evidence_entry_ids"] = ["inner-only-id"]
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("cross-manifest review ref" in problem for problem in receipt["problems"])
    assert any("ambiguous inner-manifest refs" in problem for problem in receipt["problems"])


def test_verifier_rejects_unbound_verification_run(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    run = manifest["verification_runs"][0]
    run["command"] = []
    run.pop("exit_status")
    run["environment"] = {}
    run["source_tree_digest"] = "sha256:" + "0" * 64
    run["artifact_digests"] = {}
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("missing command" in problem for problem in receipt["problems"])
    assert any("missing exit_status" in problem for problem in receipt["problems"])
    assert any("missing environment identity" in problem for problem in receipt["problems"])
    assert any("source_tree_digest is not bound" in problem for problem in receipt["problems"])
    assert any("manifest digest is not bound" in problem for problem in receipt["problems"])


def test_verifier_rejects_completed_review_without_reviewed_subject_digest(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    manifest["review_attempts"][1]["reviewed_subject_digest"] = None
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("completed review missing reviewed_subject_digest" in problem for problem in receipt["problems"])


def test_verifier_rejects_completed_review_without_response_or_sentinel(tmp_path: Path) -> None:
    root = _extract(_package(tmp_path), tmp_path / "extract")
    manifest = _manifest(root)
    manifest["review_attempts"][1]["completion_status"] = "completed"
    manifest["review_attempts"][1]["disposition"] = "not_suffix_fragile"
    manifest["review_attempts"][1]["states"]["response_received"] = False
    manifest["review_attempts"][1]["states"]["sentinel_validated"] = False
    _save_manifest(root, manifest)

    receipt = verify_package(root)

    assert receipt["passed"] is False
    assert any("completed review without response and valid sentinel" in problem for problem in receipt["problems"])
