"""Provider artifact continuity checks for Battle child DAG receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROVIDER_CONTINUITY_SCHEMA = "battle.provider_artifact_continuity_receipt.v1"


def build_provider_artifact_continuity_receipt(
    *,
    out_path: Path | None = None,
    authorship: dict[str, Any],
    specimen: dict[str, Any] | None = None,
    compile_receipt: dict[str, Any] | None = None,
    compile_repair_receipt: dict[str, Any] | None = None,
    runtime_receipt: dict[str, Any] | None = None,
    judge_receipt: dict[str, Any] | None = None,
    provider_code_path: Path | None = None,
    docker_specimen_path: Path | None = None,
) -> dict[str, Any]:
    """Verify provider bytes or explicit repair lineage feed compile, Docker, and Judge."""

    hashes = _hashes(
        authorship=authorship,
        specimen=specimen,
        compile_receipt=compile_receipt,
        runtime_receipt=runtime_receipt,
        judge_receipt=judge_receipt,
        provider_code_path=provider_code_path,
        docker_specimen_path=docker_specimen_path,
    )
    repair_lineage = _repair_lineage(
        provider_sha=hashes.get("provider_authorship_sha256"),
        selected_sha=hashes.get("compile_input_sha256"),
        compile_receipt=compile_receipt,
        compile_repair_receipt=compile_repair_receipt,
    )
    errors = _continuity_errors(
        authorship=authorship,
        compile_receipt=compile_receipt,
        compile_repair_receipt=compile_repair_receipt,
        runtime_receipt=runtime_receipt,
        judge_receipt=judge_receipt,
        hashes=hashes,
        repair_lineage=repair_lineage,
    )
    receipt = {
        "schema": PROVIDER_CONTINUITY_SCHEMA,
        "status": "PASS" if not errors else "BLOCKED",
        "mocked": False,
        "fixture_fallback_used": False,
        "provider_live": authorship.get("provider_live") is True,
        "observed_provider": authorship.get("observed_provider"),
        "observed_model": authorship.get("observed_model"),
        "hashes": hashes,
        "repair_lineage": repair_lineage,
        "verified_equal": not errors,
        "checks": {
            "provider_authorship_passed": authorship.get("status") == "PASS",
            "observed_provider_pinned": bool(authorship.get("observed_provider")),
            "observed_model_pinned": bool(authorship.get("observed_model")),
            "compile_passed_before_runtime": _compile_passed(compile_receipt),
            "docker_image_identity_bound": _docker_image_bound(compile_receipt),
            "provider_sha_equals_compile_input_sha": _same(
                hashes.get("provider_authorship_sha256"), hashes.get("compile_input_sha256")
            ),
            "selected_artifact_has_provenance": repair_lineage["valid"],
            "compile_input_sha_equals_docker_specimen_sha": _same(
                hashes.get("compile_input_sha256"), hashes.get("docker_specimen_sha256")
            ),
            "docker_specimen_sha_equals_judge_target_sha": _same_or_not_run(
                hashes.get("docker_specimen_sha256"), hashes.get("judge_target_sha256"), judge_receipt
            ),
        },
        "errors": errors,
        "claims": {
            "proves": [
                "Authorship, compile input, Docker specimen, and Judge target SHA continuity passed."
            ]
            if not errors
            else [],
            "does_not_prove": [
                "Exploit success.",
                "Blue detection, block, bypass, or kill.",
            ],
        },
        "created_at": _utc_stamp(),
    }
    if out_path is not None:
        _write_json(out_path, receipt)
    return receipt


def _hashes(
    *,
    authorship: dict[str, Any],
    specimen: dict[str, Any] | None,
    compile_receipt: dict[str, Any] | None,
    runtime_receipt: dict[str, Any] | None,
    judge_receipt: dict[str, Any] | None,
    provider_code_path: Path | None,
    docker_specimen_path: Path | None,
) -> dict[str, str | None]:
    provider_file_sha = _sha256_file(provider_code_path) if provider_code_path else None
    docker_file_sha = _sha256_file(docker_specimen_path) if docker_specimen_path else None
    return {
        "provider_authorship_sha256": _first_sha(
            authorship.get("code_artifact_sha256"),
            authorship.get("transport_source_sha256"),
            specimen.get("code_sha256") if isinstance(specimen, dict) else None,
            provider_file_sha,
        ),
        "specimen_metadata_sha256": specimen.get("code_sha256") if isinstance(specimen, dict) else None,
        "provider_file_sha256": provider_file_sha,
        "compile_source_sha256": _first_sha(
            compile_receipt.get("source_artifact_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("source_code_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("provider_authored_code_sha256") if isinstance(compile_receipt, dict) else None,
        ),
        "compile_input_sha256": _first_sha(
            compile_receipt.get("compile_input_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("selected_artifact_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("selected_code_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("source_artifact_sha256") if isinstance(compile_receipt, dict) else None,
            compile_receipt.get("source_code_sha256") if isinstance(compile_receipt, dict) else None,
        ),
        "docker_specimen_sha256": _first_sha(
            docker_file_sha,
            compile_receipt.get("docker_specimen_sha256") if isinstance(compile_receipt, dict) else None,
            runtime_receipt.get("docker_specimen_sha256") if isinstance(runtime_receipt, dict) else None,
            runtime_receipt.get("specimen_sha256") if isinstance(runtime_receipt, dict) else None,
            runtime_receipt.get("code_sha256") if isinstance(runtime_receipt, dict) else None,
        ),
        "judge_target_sha256": _first_sha(
            judge_receipt.get("judge_target_sha256") if isinstance(judge_receipt, dict) else None,
            judge_receipt.get("target_sha256") if isinstance(judge_receipt, dict) else None,
            judge_receipt.get("specimen_sha256") if isinstance(judge_receipt, dict) else None,
            judge_receipt.get("code_sha256") if isinstance(judge_receipt, dict) else None,
        ),
    }


def _continuity_errors(
    *,
    authorship: dict[str, Any],
    compile_receipt: dict[str, Any] | None,
    compile_repair_receipt: dict[str, Any] | None,
    runtime_receipt: dict[str, Any] | None,
    judge_receipt: dict[str, Any] | None,
    hashes: dict[str, str | None],
    repair_lineage: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if authorship.get("status") != "PASS":
        errors.append("PROVIDER_AUTHORSHIP_NOT_PASS")
    if authorship.get("provider_live") is not True or authorship.get("agentic") is not True:
        errors.append("PROVIDER_AUTHORSHIP_NOT_LIVE_AGENTIC")
    if not authorship.get("observed_provider"):
        errors.append("OBSERVED_PROVIDER_MISSING")
    if not authorship.get("observed_model"):
        errors.append("OBSERVED_MODEL_MISSING")
    if not _compile_passed(compile_receipt):
        errors.append("COMPILE_NOT_PASS")
    if not _docker_image_bound(compile_receipt):
        errors.append("DOCKER_IMAGE_ID_MISSING")
    _require_same(
        errors,
        hashes,
        "provider_authorship_sha256",
        "specimen_metadata_sha256",
        "PROVIDER_SPECIMEN_SHA_MISMATCH",
        optional_right=True,
    )
    _require_same(errors, hashes, "provider_authorship_sha256", "provider_file_sha256", "PROVIDER_FILE_SHA_MISMATCH", optional_right=True)
    provider_sha = hashes.get("provider_authorship_sha256")
    compile_source_sha = hashes.get("compile_source_sha256")
    compile_input_sha = hashes.get("compile_input_sha256")
    repaired = bool(provider_sha and compile_input_sha and provider_sha != compile_input_sha)
    repair_valid = repair_lineage.get("valid") is True
    if repaired and not repair_valid:
        errors.append("REPAIR_HISTORY_MISSING")
        errors.append("COMPILE_INPUT_SHA_MISMATCH")
    elif repaired:
        if compile_source_sha != compile_input_sha:
            errors.append("COMPILE_SOURCE_SHA_MISMATCH")
    else:
        _require_same(errors, hashes, "provider_authorship_sha256", "compile_source_sha256", "COMPILE_SOURCE_SHA_MISMATCH")
        _require_same(errors, hashes, "provider_authorship_sha256", "compile_input_sha256", "COMPILE_INPUT_SHA_MISMATCH")
    _require_same(errors, hashes, "compile_input_sha256", "docker_specimen_sha256", "DOCKER_SPECIMEN_SHA_MISMATCH")
    if isinstance(runtime_receipt, dict) and runtime_receipt:
        if runtime_receipt.get("compile_status") not in {None, "PASS", "PASSED"}:
            errors.append("RUNTIME_RECEIPT_COMPILE_NOT_PASS")
        if runtime_receipt.get("docker_image_id") in {None, ""}:
            errors.append("RUNTIME_DOCKER_IMAGE_ID_MISSING")
    if isinstance(judge_receipt, dict) and judge_receipt:
        _require_same(errors, hashes, "docker_specimen_sha256", "judge_target_sha256", "JUDGE_TARGET_SHA_MISMATCH")
        if not _judge_input_bound(judge_receipt):
            errors.append("JUDGE_INPUT_IDENTITY_MISSING")
    return sorted(set(errors))


def _repair_lineage(
    *,
    provider_sha: str | None,
    selected_sha: str | None,
    compile_receipt: dict[str, Any] | None,
    compile_repair_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    repaired = bool(provider_sha and selected_sha and provider_sha != selected_sha)
    chain = None
    if isinstance(compile_repair_receipt, dict):
        chain = compile_repair_receipt.get("authorization_chain")
    if chain is None and isinstance(compile_receipt, dict):
        chain = compile_receipt.get("repair_authorization_chain")
    valid = not repaired or _repair_chain_valid(chain, provider_sha, selected_sha)
    return {
        "repaired": repaired,
        "valid": valid,
        "provider_artifact_sha256": provider_sha,
        "selected_artifact_sha256": selected_sha,
        "authorization_chain_length": len(chain) if isinstance(chain, list) else 0,
    }


def _repair_chain_valid(chain: Any, provider_sha: str | None, selected_sha: str | None) -> bool:
    if not provider_sha or not selected_sha or not isinstance(chain, list) or len(chain) < 2:
        return False
    first = chain[0]
    last = chain[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        return False
    if _first_sha(first.get("artifact_sha256")) != provider_sha:
        return False
    if _first_sha(last.get("artifact_sha256")) != selected_sha:
        return False
    provenance = last.get("provenance")
    return isinstance(provenance, dict) and bool(provenance.get("provider_response_id"))


def _docker_image_bound(receipt: dict[str, Any] | None) -> bool:
    return isinstance(receipt, dict) and isinstance(receipt.get("docker_image_id"), str) and receipt["docker_image_id"].startswith("sha256:")


def _judge_input_bound(receipt: dict[str, Any]) -> bool:
    if receipt.get("judge_input_sha256") or receipt.get("reviewed_judge_input_sha256"):
        return True
    bindings = receipt.get("judge_input_byte_bindings")
    return isinstance(bindings, list) and bool(bindings)

def _compile_passed(receipt: dict[str, Any] | None) -> bool:
    return isinstance(receipt, dict) and receipt.get("status") == "PASS" and receipt.get("compile_status") in {"PASS", "PASSED"}


def _require_same(
    errors: list[str],
    hashes: dict[str, str | None],
    left: str,
    right: str,
    code: str,
    *,
    optional_right: bool = False,
) -> None:
    left_value = hashes.get(left)
    right_value = hashes.get(right)
    if not left_value or (not right_value and not optional_right):
        errors.append(code)
    elif right_value and left_value != right_value:
        errors.append(code)


def _same(left: str | None, right: str | None) -> bool:
    return bool(left and right and left == right)


def _same_or_not_run(left: str | None, right: str | None, judge_receipt: dict[str, Any] | None) -> bool:
    if not isinstance(judge_receipt, dict) or not judge_receipt:
        return True
    return _same(left, right)


def _first_sha(*values: Any) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        if value.startswith("sha256:") and len(value) == 71:
            return value
        if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower()):
            return f"sha256:{value}"
    return None


def _sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
