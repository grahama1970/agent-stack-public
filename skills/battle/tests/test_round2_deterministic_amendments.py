from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_round2_deterministic_amendments.py"
spec = importlib.util.spec_from_file_location("build_round2_deterministic_amendments", SCRIPT)
assert spec and spec.loader
round2 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = round2
spec.loader.exec_module(round2)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_repaired_round2_bundle_dedupes_attempts_and_rebinds_digest(tmp_path: Path) -> None:
    response = tmp_path / "response.md"
    response.write_text("COMPLETE_BATTLE_WEBGPT_R1_SENTINEL\n", encoding="utf-8")
    response_entry = {
        "id": "response-entry",
        "category": "current_review_response",
        "logical_path": "round-1/webgpt/response.md",
        "source_path": str(response),
        "disposition": "available",
        "sha256": round2.sha256_file(response),
        "bytes": response.stat().st_size,
        "blob_path": str(response),
    }
    correction_entry = dict(response_entry, id="correction-entry", category="append_only_correction_record", logical_path="corrections/old.json")
    inner = {
        "schema": "battle.comprehensive_web_review_bundle.v2",
        "bundle_digest": "sha256:" + "1" * 64,
        "entries": [response_entry, correction_entry],
        "review_attempts": [],
    }
    inner_path = _write_json(tmp_path / "qualification-manifest.json", inner)
    archive = tmp_path / "battle-qualification-package-abc.tar"
    archive.write_text("archive", encoding="utf-8")
    build_receipt = tmp_path / "battle-qualification-build-receipt-abc.json"
    build_receipt.write_text("{}", encoding="utf-8")
    verification = tmp_path / "verification-receipt.json"
    verification.write_text('{"passed": true}\n', encoding="utf-8")
    q_receipt = {
        "bundle_digest": inner["bundle_digest"],
        "archive_path": str(archive),
        "archive_sha256": round2.sha256_file(archive),
        "manifest_path": str(inner_path),
        "receipt_path": str(build_receipt),
        "verification_receipt_path": str(verification),
    }
    outer = {
        "schema": "battle.comprehensive_web_review_bundle.v2",
        "bundle_digest": "sha256:" + "0" * 64,
        "source_tree_digest": "sha256:" + "0" * 64,
        "entries": [response_entry | {"id": "outer-response", "category": "current_review_response"}],
        "verification_runs": [{"source_tree_digest": "sha256:" + "0" * 64, "artifact_digests": {"manifest": "sha256:" + "0" * 64}}],
        "review_attempts": [
            {
                "attempt_id": "webgpt-r1-authoritative-20260919T230916Z",
                "completion_status": "completed",
                "disposition": "authoritative_round1_webgpt_response_preserved",
                "response_attempt_id": "webgpt-r1-authoritative-20260919T230916Z",
                "response_entry_id": "outer-response",
                "evidence_entry_ids": ["outer-response"],
                "correction_entry_ids": [],
                "reviewed_bundle_sha256_in_response": "sha256:" + "2" * 64,
            },
            {
                "attempt_id": "webgpt-r1-authoritative-20260919T230916Z",
                "completion_status": "completed",
                "disposition": "current_review_response_bound_to_bundle",
                "response_attempt_id": "webgpt-r1-authoritative-20260919T230916Z",
                "response_entry_id": "response-entry",
                "evidence_entry_ids": ["response-entry"],
                "correction_entry_ids": [],
            },
        ],
    }
    outer_path = _write_json(tmp_path / "outer.json", outer)
    out_path = tmp_path / "repaired.json"

    receipt = round2.repair_bundle_refs(outer_path, inner_path, q_receipt, out_path)
    repaired = json.loads(out_path.read_text(encoding="utf-8"))

    assert receipt["status"] == "PASS"
    assert repaired["bundle_digest"] == round2.manifest_digest(repaired)
    assert repaired["source_tree_digest"] == round2.source_tree_digest(repaired["entries"])
    assert [item["attempt_id"] for item in repaired["review_attempts"]] == ["webgpt-r1-authoritative-20260919T230916Z"]
    attempt = repaired["review_attempts"][0]
    assert attempt["reviewed_subject_digest"] == "sha256:" + "2" * 64
    assert attempt["response_ref"]["manifest_digest"] == repaired["bundle_digest"]
    assert "outer_bundle_digest" not in repaired["receiving_side_verification"]
