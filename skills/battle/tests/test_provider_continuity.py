from __future__ import annotations

import hashlib
import json
from pathlib import Path

from battle_skill import child_dag_node_adapter as adapter
from battle_skill.provider_authorship import build_provider_authorship_receipt
from battle_skill.provider_continuity import build_provider_artifact_continuity_receipt


def test_provider_continuity_requires_equal_authorship_compile_docker_judge_sha(tmp_path: Path) -> None:
    code = tmp_path / "exploit_specimen.py"
    code.write_text("print('provider')\n", encoding="utf-8")
    sha = _sha256_prefixed(code)

    receipt = build_provider_artifact_continuity_receipt(
        authorship={
            "status": "PASS",
            "provider_live": True,
            "agentic": True,
            "observed_provider": "opencode-go",
            "observed_model": "opencode-go/kimi-k2.6",
            "code_artifact_sha256": sha,
        },
        specimen={"code_sha256": sha},
        compile_receipt={
            "status": "PASS",
            "compile_status": "PASS",
            "source_code_sha256": sha,
            "compile_input_sha256": sha,
            "docker_specimen_sha256": sha,
            "selected_code_sha256": sha,
            "docker_image_id": "sha256:" + "d" * 64,
        },
        runtime_receipt={"docker_specimen_sha256": sha, "docker_image_id": "sha256:" + "d" * 64},
        judge_receipt={"judge_target_sha256": sha, "judge_input_sha256": "sha256:" + "e" * 64},
        provider_code_path=code,
        docker_specimen_path=code,
    )

    assert receipt["status"] == "PASS"
    assert receipt["verified_equal"] is True
    assert receipt["checks"]["docker_specimen_sha_equals_judge_target_sha"] is True


def test_artifact_reviewer_blocks_parent_handoff_when_repair_changes_provider_bytes(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "step" / "artifact-reviewer"
    artifact_dir.mkdir(parents=True)
    provider = artifact_dir / "exploit_specimen.py"
    repaired = artifact_dir / "repaired_exploit_specimen.py"
    provider.write_text("print('provider')\n", encoding="utf-8")
    repaired.write_text("print('repaired')\n", encoding="utf-8")
    provider_sha = _sha256_prefixed(provider)
    repaired_sha = _sha256_prefixed(repaired)

    authorship = artifact_dir / "provider-authorship-receipt.json"
    specimen = artifact_dir / "specimen.json"
    compile_receipt = artifact_dir / "compile_receipt.json"
    genome = artifact_dir / "exploit_genome.json"
    research = artifact_dir / "research_receipts.json"
    _write_json(
        authorship,
        {
            "status": "PASS",
            "provider_live": True,
            "agentic": True,
            "observed_provider": "opencode-go",
            "observed_model": "opencode-go/kimi-k2.6",
            "code_artifact_sha256": provider_sha,
        },
    )
    _write_json(specimen, {"code_sha256": provider_sha})
    _write_json(
        compile_receipt,
        {
            "status": "PASS",
            "live": "docker_python_compile",
            "compile_status": "PASS",
            "source_code_sha256": provider_sha,
            "compile_input_sha256": repaired_sha,
            "docker_specimen_sha256": repaired_sha,
            "selected_code_sha256": repaired_sha,
            "docker_image_id": "sha256:" + "d" * 64,
        },
    )
    _write_json(genome, {"status": "PASS"})
    _write_json(research, {"status": "PASS"})

    start_payload = {
        "context": {
            "artifacts": [
                str(authorship),
                str(specimen),
                str(genome),
                str(research),
                str(compile_receipt),
                str(provider),
                str(repaired),
            ]
        }
    }

    handoff = adapter._run_artifact_reviewer(start_payload=start_payload, artifact_dir=artifact_dir)
    review = json.loads((artifact_dir / "review_receipt.json").read_text(encoding="utf-8"))
    continuity = json.loads((artifact_dir / "provider-continuity-receipt.json").read_text(encoding="utf-8"))

    assert handoff["result"]["status"] == "BLOCKED"
    assert review["status"] == "BLOCKED"
    assert review["checks"]["provider_continuity_passed"] is False
    assert continuity["status"] == "BLOCKED"
    assert "COMPILE_INPUT_SHA_MISMATCH" in continuity["errors"]


def test_provider_authorship_blocks_requested_route_identity_fallback(tmp_path: Path) -> None:
    code_sha = "sha256:" + "1" * 64
    receipt = build_provider_authorship_receipt(
        out_path=tmp_path / "provider-authorship.json",
        battle_id="battle-004",
        dag_id="dag-1",
        node_id="exploit-code-author",
        child_lane_id="child-1",
        goal_hash="sha256:goal",
        battle_work_order_path=tmp_path / "battle-work-order.json",
        tau_work_order_path=tmp_path / "tau-work-order.json",
        launch_receipt_path=tmp_path / "launch.json",
        worker_result_path=tmp_path / "result.json",
        worker_validation_path=tmp_path / "validation.json",
        artifact_validation={
            "status": "PASS",
            "code_artifact_sha256": code_sha,
            "code_artifact_bytes": 24,
            "errors": [],
        },
        launch_receipt={
            "status": "PASS",
            "live": True,
            "provider_live": True,
            "provider_request_id": "request-test",
            "provider_response_id": "response-test",
            "provider_call_count": 1,
            "model_provider_route": {"provider": "requested-provider", "model": "requested-model"},
        },
        worker_result={"artifacts": ["outputs/exploit_specimen.py"]},
        worker_validation={"status": "PASS", "provider_live": True, "source_sha256": code_sha},
    )

    assert receipt["status"] == "BLOCKED"
    assert receipt["observed_provider"] == "requested-provider"
    assert receipt["observed_model"] == "requested-model"
    assert receipt["observed_identity_fallback_used"] is True
    assert "OBSERVED_PROVIDER_FALLBACK_USED" in receipt["errors"]
    assert "OBSERVED_MODEL_FALLBACK_USED" in receipt["errors"]


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256_prefixed(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
