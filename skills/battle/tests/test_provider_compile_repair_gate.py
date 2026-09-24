from __future__ import annotations

import json
from pathlib import Path

from battle_skill.compile_repair import bounded_compile_repair
from battle_skill.provider_continuity import build_provider_artifact_continuity_receipt


def test_non_ascii_bytes_literal_fails_closed_and_retains_failed_specimen(tmp_path: Path) -> None:
    specimen = tmp_path / "exploit_specimen.py"
    specimen.write_text("payload = b'☃'\n", encoding="utf-8")

    receipt = bounded_compile_repair(
        source_path=specimen,
        out_dir=tmp_path / "compile",
        use_docker=False,
    )

    assert receipt["status"] == "BLOCKED"
    assert receipt["final_status"] == "COMPILE_FAILED"
    assert receipt["judge_status"] == "JUDGE_NOT_RUN_COMPILE_FAILED"
    retained = Path(receipt["attempts"][0]["specimen_path"])
    assert retained.read_text(encoding="utf-8") == specimen.read_text(encoding="utf-8")
    assert not (tmp_path / "compile" / "specimen.json").exists()


def test_repair_loop_accepts_attempt_two_with_explicit_provenance(tmp_path: Path) -> None:
    specimen = tmp_path / "exploit_specimen.py"
    specimen.write_text("payload = b'☃'\n", encoding="utf-8")
    repaired_text = "payload = '☃'\n"

    def provenance(attempt: dict, repaired_sha: str) -> dict:
        return {
            "provider_response_id": "repair-response-1",
            "failed_specimen_sha256": attempt["specimen_sha256"],
            "repaired_specimen_sha256": repaired_sha,
            "stderr_sha256": attempt["stderr_sha256"],
        }

    receipt = bounded_compile_repair(
        source_path=specimen,
        out_dir=tmp_path / "compile",
        final_path=tmp_path / "compile" / "repaired_exploit_specimen.py",
        repair_candidates=[{"text": repaired_text, "provenance_factory": provenance}],
        use_docker=False,
    )

    assert receipt["status"] == "PASS"
    assert receipt["final_status"] == "PASS"
    assert len(receipt["attempts"]) == 2
    assert receipt["attempts"][0]["compile_status"] == "FAILED"
    assert Path(receipt["attempts"][0]["specimen_path"]).read_text(encoding="utf-8") == "payload = b'☃'\n"
    assert Path(receipt["final_specimen_path"]).read_text(encoding="utf-8") == repaired_text


def test_three_failing_repairs_exhaust_without_judge_path(tmp_path: Path) -> None:
    specimen = tmp_path / "exploit_specimen.py"
    specimen.write_text("payload = b'☃'\n", encoding="utf-8")

    def bad_candidate(text: str):
        def provenance(attempt: dict, repaired_sha: str) -> dict:
            return {
                "provider_response_id": f"repair-{attempt['attempt']}",
                "failed_specimen_sha256": attempt["specimen_sha256"],
                "repaired_specimen_sha256": repaired_sha,
                "stderr_sha256": attempt["stderr_sha256"],
            }
        return {"text": text, "provenance_factory": provenance}

    receipt = bounded_compile_repair(
        source_path=specimen,
        out_dir=tmp_path / "compile",
        repair_candidates=[bad_candidate("x = (\n"), bad_candidate("y = (\n"), bad_candidate("z = (\n")],
        use_docker=False,
    )

    assert receipt["status"] == "BLOCKED"
    assert receipt["final_status"] == "REPAIR_EXHAUSTED"
    assert len(receipt["attempts"]) == 4
    assert receipt["final_specimen_sha256"] is None
    assert receipt["judge_status"] == "JUDGE_NOT_RUN_COMPILE_FAILED"


def test_hash_chain_break_blocks_continuity_with_v2_compile_receipt(tmp_path: Path) -> None:
    provider = tmp_path / "provider.py"
    repaired = tmp_path / "repaired.py"
    provider.write_text("print('provider')\n", encoding="utf-8")
    repaired.write_text("print('repaired')\n", encoding="utf-8")
    provider_sha = _sha(provider)
    repaired_sha = _sha(repaired)

    receipt = build_provider_artifact_continuity_receipt(
        authorship={
            "status": "PASS",
            "provider_live": True,
            "agentic": True,
            "observed_provider": "tau",
            "observed_model": "model",
            "code_artifact_sha256": provider_sha,
        },
        specimen={"code_sha256": provider_sha},
        compile_receipt={
            "schema": "battle.team_compile_receipt.v2",
            "status": "PASS",
            "compile_status": "PASS",
            "source_artifact_sha256": provider_sha,
            "compile_input_sha256": repaired_sha,
            "selected_artifact_sha256": repaired_sha,
            "docker_specimen_sha256": repaired_sha,
            "docker_image_id": "sha256:" + "d" * 64,
        },
        provider_code_path=provider,
        docker_specimen_path=repaired,
    )

    assert receipt["status"] == "BLOCKED"
    assert "COMPILE_INPUT_SHA_MISMATCH" in receipt["errors"]


def _sha(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_provider_continuity_accepts_repaired_artifact_with_explicit_history(tmp_path: Path) -> None:
    provider = tmp_path / "provider.py"
    repaired = tmp_path / "repaired.py"
    provider.write_text("payload = b'☃'\n", encoding="utf-8")
    repaired.write_text("payload = '☃'\n", encoding="utf-8")
    provider_sha = _sha(provider)
    repaired_sha = _sha(repaired)
    provenance = {
        "provider_response_id": "repair-response-1",
        "failed_specimen_sha256": provider_sha,
        "repaired_specimen_sha256": repaired_sha,
        "stderr_sha256": "sha256:" + "e" * 64,
    }

    receipt = build_provider_artifact_continuity_receipt(
        authorship={
            "status": "PASS",
            "provider_live": True,
            "agentic": True,
            "observed_provider": "tau",
            "observed_model": "model",
            "code_artifact_sha256": provider_sha,
        },
        specimen={"code_sha256": provider_sha},
        compile_receipt={
            "schema": "battle.team_compile_receipt.v2",
            "status": "PASS",
            "compile_status": "PASS",
            "source_artifact_sha256": repaired_sha,
            "compile_input_sha256": repaired_sha,
            "selected_artifact_sha256": repaired_sha,
            "docker_specimen_sha256": repaired_sha,
            "docker_image_id": "sha256:" + "d" * 64,
        },
        compile_repair_receipt={
            "authorization_chain": [
                {"attempt": 0, "artifact_sha256": provider_sha, "provenance": "provider_authorship"},
                {"attempt": 1, "artifact_sha256": repaired_sha, "provenance": provenance},
            ]
        },
        provider_code_path=provider,
        docker_specimen_path=repaired,
    )

    assert receipt["status"] == "PASS"
    assert receipt["repair_lineage"]["repaired"] is True
    assert receipt["checks"]["selected_artifact_has_provenance"] is True
