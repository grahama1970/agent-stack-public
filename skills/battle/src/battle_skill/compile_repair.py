"""Bounded compile gate and provider repair receipts for Battle artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


TEAM_COMPILE_SCHEMA_V2 = "battle.team_compile_receipt.v2"
PROVIDER_COMPILE_REPAIR_SCHEMA_V1 = "battle.provider_compile_repair_receipt.v1"
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def run_compile_gate(
    *,
    source_path: Path,
    out_dir: Path,
    source_artifact_sha256: str | None = None,
    team: str | None = None,
    generation: int | None = None,
    docker_image: str = DEFAULT_DOCKER_IMAGE,
    timeout_s: int = 120,
    provider_route: dict[str, Any] | None = None,
    prior_source_artifact_sha256: str | None = None,
    use_docker: bool | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Compile one Python artifact and write a v2 compile receipt."""

    out_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_path.resolve()
    source_sha = _sha_path_prefixed(source_path)
    expected_sha = _normalize_sha(source_artifact_sha256) or source_sha
    stdout_path = out_dir / "compile.stdout.txt"
    stderr_path = out_dir / "compile.stderr.txt"
    use_docker = _use_docker() if use_docker is None else use_docker
    docker_image_id = _docker_image_id(docker_image) if use_docker else None
    if use_docker:
        command = _docker_compile_command(
            source_path=source_path, docker_image=docker_image, timeout_s=timeout_s
        )
    else:
        command = [sys.executable, "-m", "py_compile", str(source_path)]

    errors = _provider_route_errors(provider_route)
    if use_docker and not docker_image_id:
        errors.append("DOCKER_IMAGE_ID_UNAVAILABLE")
    if source_sha != expected_sha:
        errors.append("SOURCE_ARTIFACT_SHA_MISMATCH")

    try:
        completed = (command_runner or _default_runner)(command, timeout_s)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = int(completed.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        stdout = ""
        stderr = repr(exc)
        exit_code = 127
        errors.append("COMPILE_TRANSPORT_UNAVAILABLE")

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    if exit_code != 0:
        errors.append("COMPILE_FAILED")
    status = "PASS" if not errors and exit_code == 0 else "BLOCKED"
    receipt = {
        "schema": TEAM_COMPILE_SCHEMA_V2,
        "status": status,
        "team": team,
        "generation": generation,
        "mocked": False,
        "live": "docker_python_compile" if use_docker else "deterministic_local_py_compile",
        "compile_status": "PASS" if status == "PASS" else "FAILED",
        "runtime_status": "NOT_RUN",
        "target_contact": "NOT_RUN",
        "judge_status": "NOT_RUN" if status == "PASS" else "JUDGE_NOT_RUN_COMPILE_FAILED",
        "source_artifact": str(source_path),
        "source_artifact_sha256": expected_sha,
        "source_code_sha256": expected_sha,
        "provider_authored_code_sha256": expected_sha,
        "prior_source_artifact_sha256": _normalize_sha(prior_source_artifact_sha256),
        "original_provider_artifact_sha256": _normalize_sha(prior_source_artifact_sha256) or expected_sha,
        "compile_input_sha256": source_sha,
        "selected_artifact_sha256": source_sha,
        "selected_code_sha256": source_sha,
        "docker_specimen_sha256": source_sha,
        "docker_image": docker_image,
        "docker_image_id": docker_image_id,
        "command": command,
        "docker_command": command if use_docker else None,
        "docker_exit_code": exit_code if use_docker else None,
        "exit_code": exit_code,
        "timeout_s": timeout_s,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": _sha_path_prefixed(stdout_path),
        "stderr_sha256": _sha_path_prefixed(stderr_path),
        "provider_route": provider_route or {},
        "errors": sorted(set(errors)),
        "claims": {
            "proves": [
                "Battle compiled the selected artifact before runtime/Judge entry."
            ]
            if status == "PASS"
            else ["Battle retained a fail-closed compile receipt before Judge entry."],
            "does_not_prove": [
                "The specimen runs.",
                "The specimen contacts the target.",
                "The specimen exploits the target.",
                "Judge verified exploit success.",
            ],
        },
        "created_at": _now(),
    }
    _write_json(out_dir / "compile_receipt.json", receipt)
    return receipt


def bounded_compile_repair(
    *,
    source_path: Path,
    out_dir: Path,
    final_path: Path | None = None,
    max_compile_repairs: int = 3,
    repair_candidates: list[dict[str, Any]] | None = None,
    docker_image: str = DEFAULT_DOCKER_IMAGE,
    timeout_s: int = 120,
    provider_route: dict[str, Any] | None = None,
    use_docker: bool | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Run compile, then at most three provenance-bound repair candidates."""

    out_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    repair_candidates = repair_candidates or []
    current_path = source_path.resolve()
    original_sha = _sha_path_prefixed(current_path)
    authorized_sha = original_sha
    prior_sha: str | None = None
    final_status = "COMPILE_FAILED"
    final_sha: str | None = None
    final_compile: dict[str, Any] | None = None
    auth_chain: list[dict[str, Any]] = [
        {"attempt": 0, "artifact_sha256": original_sha, "provenance": "provider_authorship"}
    ]

    for attempt_no in range(1, max_compile_repairs + 2):
        attempt_dir = out_dir / "repair-attempts" / str(attempt_no)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        retained = attempt_dir / "specimen.py"
        shutil.copyfile(current_path, retained)
        compile_receipt = run_compile_gate(
            source_path=current_path,
            out_dir=attempt_dir,
            source_artifact_sha256=authorized_sha,
            docker_image=docker_image,
            timeout_s=timeout_s,
            provider_route=provider_route,
            prior_source_artifact_sha256=prior_sha,
            use_docker=use_docker,
            command_runner=command_runner,
        )
        attempt = {
            "attempt": attempt_no,
            "status": compile_receipt["status"],
            "compile_status": compile_receipt["compile_status"],
            "specimen_path": str(retained),
            "specimen_sha256": _sha_path_prefixed(retained),
            "stderr_path": compile_receipt["stderr_path"],
            "stderr_sha256": compile_receipt["stderr_sha256"],
            "command": compile_receipt["command"],
            "docker_image": compile_receipt["docker_image"],
            "docker_image_id": compile_receipt["docker_image_id"],
            "timeout_s": timeout_s,
            "compile_receipt_path": str(attempt_dir / "compile_receipt.json"),
            "errors": compile_receipt.get("errors", []),
        }
        attempts.append(attempt)
        if compile_receipt["status"] == "PASS":
            final_status = "PASS"
            final_sha = compile_receipt["selected_artifact_sha256"]
            final_compile = compile_receipt
            if final_path is not None:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(current_path, final_path)
            break
        if attempt_no > max_compile_repairs:
            final_status = "REPAIR_EXHAUSTED"
            break
        candidate = repair_candidates[attempt_no - 1] if attempt_no - 1 < len(repair_candidates) else None
        if not candidate:
            final_status = "COMPILE_FAILED"
            break
        candidate_bytes = _candidate_bytes(candidate)
        candidate_sha = "sha256:" + hashlib.sha256(candidate_bytes).hexdigest()
        provenance = candidate.get("provenance") if isinstance(candidate, dict) else None
        factory = candidate.get("provenance_factory") if isinstance(candidate, dict) else None
        if callable(factory):
            provenance = factory(attempt, candidate_sha)
        if not _repair_provenance_valid(
            provenance=provenance,
            failed_sha=attempt["specimen_sha256"],
            repaired_sha=candidate_sha,
            stderr_sha=attempt["stderr_sha256"],
        ):
            attempt["repair_rejected"] = "REPAIR_PROVENANCE_MISSING"
            final_status = "HASH_CHAIN_MISMATCH"
            break
        next_path = attempt_dir / "repaired_candidate.py"
        next_path.write_bytes(candidate_bytes)
        prior_sha = authorized_sha
        authorized_sha = candidate_sha
        auth_chain.append(
            {
                "attempt": attempt_no,
                "artifact_sha256": candidate_sha,
                "provenance": provenance,
            }
        )
        attempt["provider_response_id"] = provenance.get("provider_response_id")
        attempt["repair_candidate_sha256"] = candidate_sha
        current_path = next_path.resolve()

    if final_compile is not None:
        final_compile["source_specimen_sha256"] = original_sha
        final_compile["selected_specimen_sha256"] = final_sha
        final_compile["repair_history_required"] = final_sha != original_sha
        final_compile["repair_authorization_chain"] = auth_chain
        final_compile["accepted_repaired_artifact_provenance"] = (
            auth_chain[-1]["provenance"] if final_sha != original_sha else None
        )
        final_compile["provider_authored_code_sha256"] = original_sha

    receipt = {
        "schema": PROVIDER_COMPILE_REPAIR_SCHEMA_V1,
        "status": "PASS" if final_status == "PASS" else "BLOCKED",
        "final_status": final_status,
        "max_compile_repairs": max_compile_repairs,
        "attempts": attempts,
        "source_specimen_path": str(source_path),
        "source_specimen_sha256": original_sha,
        "original_specimen_retained": bool(attempts),
        "final_specimen_path": str(final_path) if final_status == "PASS" and final_path else None,
        "final_specimen_sha256": final_sha,
        "selected_specimen_sha256": final_sha,
        "repair_history_required": bool(final_sha and final_sha != original_sha),
        "accepted_repaired_artifact_provenance": (
            auth_chain[-1]["provenance"] if final_sha and final_sha != original_sha else None
        ),
        "authorization_chain": auth_chain,
        "final_compile_receipt": final_compile,
        "runtime_status": "NOT_RUN",
        "target_contact": "NOT_RUN",
        "judge_status": "NOT_RUN" if final_status == "PASS" else "JUDGE_NOT_RUN_COMPILE_FAILED",
        "errors": _repair_errors(final_status),
        "claims": {
            "proves": [
                "Battle bounded compile repair to at most three provenance-bound repair attempts and retained failed specimens."
            ],
            "does_not_prove": [
                "Provider creativity across targets.",
                "Exploit success.",
                "Judge verification.",
            ],
        },
        "created_at": _now(),
    }
    _write_json(out_dir / "provider_compile_repair_receipt.json", receipt)
    if final_compile is not None:
        _write_json(out_dir / "compile_receipt.json", final_compile)
    return receipt


def _use_docker() -> bool:
    return os.environ.get("BATTLE_COMPILE_REPAIR_BACKEND", "docker") != "local"


def _docker_compile_command(*, source_path: Path, docker_image: str, timeout_s: int) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "-e",
        "PYTHONPYCACHEPREFIX=/tmp/pycache",
        "--user",
        "65534:65534",
        "-v",
        f"{source_path.parent}:/work:ro",
        "-w",
        "/work",
        docker_image,
        "python",
        "-m",
        "py_compile",
        source_path.name,
    ]


def _default_runner(command: list[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)


def _docker_image_id(docker_image: str) -> str | None:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", docker_image, "--format", "{{.Id}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return None
    image_id = completed.stdout.strip()
    return image_id if completed.returncode == 0 and image_id.startswith("sha256:") else None


def _provider_route_errors(route: dict[str, Any] | None) -> list[str]:
    if not route:
        return []
    errors: list[str] = []
    requested_provider = route.get("requested_provider") or route.get("provider")
    requested_model = route.get("requested_model") or route.get("model")
    observed_provider = route.get("observed_provider")
    observed_model = route.get("observed_model")
    if not requested_provider:
        errors.append("PROVIDER_PIN_MISSING")
    if not requested_model:
        errors.append("PROVIDER_MODEL_PIN_MISSING")
    if not observed_provider:
        errors.append("OBSERVED_PROVIDER_MISSING")
    if not observed_model:
        errors.append("OBSERVED_MODEL_MISSING")
    if requested_provider and observed_provider and requested_provider != observed_provider:
        errors.append("OBSERVED_PROVIDER_MISMATCH")
    if requested_model and observed_model and requested_model != observed_model:
        errors.append("OBSERVED_MODEL_MISMATCH")
    if route.get("observed_identity_fallback_used") is True:
        errors.append("OBSERVED_IDENTITY_FALLBACK_USED")
    return errors


def _candidate_bytes(candidate: dict[str, Any]) -> bytes:
    if isinstance(candidate.get("bytes"), bytes):
        return candidate["bytes"]
    if isinstance(candidate.get("text"), str):
        return candidate["text"].encode("utf-8")
    path = candidate.get("path")
    if isinstance(path, (str, Path)):
        return Path(path).read_bytes()
    raise ValueError("repair candidate requires bytes, text, or path")


def _repair_provenance_valid(
    *, provenance: Any, failed_sha: str, repaired_sha: str, stderr_sha: str
) -> bool:
    return (
        isinstance(provenance, dict)
        and bool(provenance.get("provider_response_id"))
        and _normalize_sha(provenance.get("failed_specimen_sha256")) == failed_sha
        and _normalize_sha(provenance.get("repaired_specimen_sha256")) == repaired_sha
        and _normalize_sha(provenance.get("stderr_sha256")) == stderr_sha
    )


def _repair_errors(final_status: str) -> list[str]:
    if final_status == "PASS":
        return []
    if final_status == "COMPILE_FAILED":
        return ["COMPILE_FAILED"]
    if final_status == "REPAIR_EXHAUSTED":
        return ["REPAIR_EXHAUSTED"]
    if final_status == "HASH_CHAIN_MISMATCH":
        return ["HASH_CHAIN_MISMATCH", "REPAIR_PROVENANCE_MISSING"]
    return [final_status]


def _normalize_sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("sha256:") and len(value) == 71:
        return value
    if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower()):
        return f"sha256:{value}"
    return None


def _sha_path_prefixed(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
