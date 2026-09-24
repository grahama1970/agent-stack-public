"""AM-05 arena lifecycle receipts.

Small, typed control-plane helpers for proving a Battle arena runtime was
started with bounded resources, no network, warm-state readback, cleanup, and a
fail-closed parent gate. Docker can be exercised locally; QEMU is represented by
a typed PASS/BLOCKED receipt and is never claimed when host tools or a base image
are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .arena_intent import bind_intent, verify_frozen_intent

RESOURCE_BUDGET_SCHEMA = "battle.am05.resource_budget.v1"
NETWORK_POLICY_SCHEMA = "battle.am05.network_policy_proof.v1"
LIFECYCLE_SCHEMA = "battle.am05.lifecycle_receipt.v1"
WARM_STATE_SCHEMA = "battle.am05.warm_state_receipt.v1"
RESET_CLEANUP_SCHEMA = "battle.am05.reset_cleanup_receipt.v1"
ARTIFACT_MANIFEST_SCHEMA = "battle.am05.artifact_manifest.v1"
PARENT_GATE_SCHEMA = "battle.am05.parent_gate_receipt.v1"
QEMU_SCHEMA = "battle.am05.qemu_lifecycle_receipt.v1"
RUN_SCHEMA = "battle.am05.run_receipt.v1"
BACKEND_APPLICABILITY_SCHEMA = "battle.am05.backend_applicability.v1"
SUPPORTED_BACKENDS = frozenset({"docker", "qemu"})


@dataclass(frozen=True)
class ResourceBudget:
    """The minimum useful budget contract Docker/QEMU receipts can enforce."""

    schema: str = RESOURCE_BUDGET_SCHEMA
    cpus: float = 0.5
    memory_mb: int = 128
    pids_limit: int = 64
    tmpfs_mb: int = 16
    timeout_seconds: int = 30

    def validate(self) -> None:
        problems: list[str] = []
        if not (0 < self.cpus <= 8):
            problems.append("cpus must be >0 and <=8")
        if not (16 <= self.memory_mb <= 8192):
            problems.append("memory_mb must be between 16 and 8192")
        if not (8 <= self.pids_limit <= 4096):
            problems.append("pids_limit must be between 8 and 4096")
        if not (1 <= self.tmpfs_mb <= 1024):
            problems.append("tmpfs_mb must be between 1 and 1024")
        if not (1 <= self.timeout_seconds <= 3600):
            problems.append("timeout_seconds must be between 1 and 3600")
        if problems:
            raise ValueError("invalid AM-05 resource budget: " + "; ".join(problems))


@dataclass(frozen=True)
class NetworkPolicy:
    schema: str = "battle.am05.network_policy.v1"
    docker_network: str = "none"
    egress: str = "deny_external"
    allowed_loopback_only: bool = True


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def sha256_json(payload: Any) -> str:
    return _sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return subprocess.CompletedProcess(command, 124, stdout, stderr + f"\ntimeout after {exc.timeout}s")


def _receipt_ref(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def _child_gate_status(child_gates: dict[str, Any]) -> dict[str, Any]:
    required = ("AM-01", "AM-02")
    missing = [gate for gate in required if gate not in child_gates]
    failed = [gate for gate in required if isinstance(child_gates.get(gate), dict) and child_gates[gate].get("status") != "PASS"]
    malformed = [gate for gate in required if gate in child_gates and not isinstance(child_gates.get(gate), dict)]
    refs = {
        gate: {
            "status": child_gates[gate].get("status"),
            "sha256": sha256_json(child_gates[gate]),
        }
        for gate in required
        if isinstance(child_gates.get(gate), dict)
    }
    problems = [*(f"missing_child_gate:{gate}" for gate in missing), *(f"failed_child_gate:{gate}" for gate in failed), *(f"malformed_child_gate:{gate}" for gate in malformed)]
    return {
        "schema": "battle.am05.child_gate_binding.v1",
        "status": "PASS" if not problems else "FAIL",
        "required": list(required),
        "refs": refs,
        "problems": problems,
    }


def _intent_status(arena_intent_binding: dict[str, Any] | None) -> dict[str, Any]:
    if not arena_intent_binding:
        return {"schema": "battle.am05.intent_gate.v1", "status": "FAIL", "problems": ["missing_arena_maker_intent_binding"]}
    receipt = verify_frozen_intent(arena_intent_binding)
    return {"schema": "battle.am05.intent_gate.v1", **receipt}


def _docker_blocked(out_dir: Path, *, reason: str, detail: str = "") -> dict[str, Any]:
    receipt = {
        "schema": LIFECYCLE_SCHEMA,
        "runtime": "docker",
        "status": "BLOCKED",
        "mocked": False,
        "live": False,
        "reason": reason,
        "detail": detail,
        "created_at": utc(),
    }
    path = write_json(out_dir / "docker-lifecycle-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def _docker_inapplicable(out_dir: Path, *, reason: str, arena_intent_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    receipt = {
        "schema": LIFECYCLE_SCHEMA,
        "runtime": "docker",
        "status": "INAPPLICABLE",
        "mocked": False,
        "live": False,
        "reason": reason,
        "created_at": utc(),
    }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(out_dir / "docker-lifecycle-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def _qemu_inapplicable(out_dir: Path, *, reason: str, arena_intent_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    receipt = {
        "schema": QEMU_SCHEMA,
        "runtime": "qemu",
        "status": "INAPPLICABLE",
        "mocked": False,
        "live": False,
        "reason": reason,
        "started_at": utc(),
        "finished_at": utc(),
        "claims": {"proves": ["QEMU was explicitly out of scope for this lifecycle gate"], "does_not_prove": ["QEMU boot or VM isolation"]},
    }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(out_dir / "qemu-lifecycle-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def _inspect_container(name: str, timeout: int) -> dict[str, Any]:
    proc = _run(["docker", "inspect", name], timeout=timeout)
    if proc.returncode != 0:
        return {"status": "FAIL", "stderr": proc.stderr, "exit_code": proc.returncode}
    try:
        payload = json.loads(proc.stdout)[0]
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        return {"status": "FAIL", "stderr": repr(exc), "exit_code": proc.returncode}
    return {"status": "PASS", "payload": payload}


def _budget_proof(inspect_payload: dict[str, Any], budget: ResourceBudget) -> dict[str, Any]:
    host = inspect_payload.get("HostConfig") or {}
    expected_memory = budget.memory_mb * 1024 * 1024
    expected_nano = int(budget.cpus * 1_000_000_000)
    checks = {
        "memory_bytes": host.get("Memory") == expected_memory,
        "nano_cpus": host.get("NanoCpus") == expected_nano,
        "pids_limit": host.get("PidsLimit") == budget.pids_limit,
        "readonly_rootfs": host.get("ReadonlyRootfs") is True,
    }
    return {
        "schema": "battle.am05.resource_budget_proof.v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "requested": asdict(budget),
        "observed": {
            "memory_bytes": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
            "pids_limit": host.get("PidsLimit"),
            "readonly_rootfs": host.get("ReadonlyRootfs"),
        },
        "checks": checks,
    }


def _network_proof(inspect_payload: dict[str, Any], policy: NetworkPolicy) -> dict[str, Any]:
    host = inspect_payload.get("HostConfig") or {}
    mode = str(host.get("NetworkMode") or "")
    settings = inspect_payload.get("NetworkSettings") or {}
    networks = settings.get("Networks") or {}
    network_names = set(networks) if isinstance(networks, dict) else set()
    none_only = not network_names or network_names == {"none"}
    checks = {
        "network_mode_none": mode == policy.docker_network == "none",
        "no_external_networks": none_only,
        "egress_denied": policy.egress == "deny_external",
    }
    return {
        "schema": NETWORK_POLICY_SCHEMA,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "policy": asdict(policy),
        "observed": {"network_mode": mode, "networks": sorted(networks)},
        "checks": checks,
    }


def normalize_required_backends(required_backends: tuple[str, ...] | list[str] | set[str] | None) -> tuple[str, ...]:
    backends = tuple(required_backends or ("docker", "qemu"))
    unknown = sorted(set(backends) - SUPPORTED_BACKENDS)
    if unknown:
        raise ValueError(f"unsupported AM-05 backend(s): {unknown}")
    if not backends:
        raise ValueError("at least one AM-05 backend must be required or explicitly marked inapplicable")
    return tuple(backend for backend in ("docker", "qemu") if backend in set(backends))


def backend_applicability_receipt(
    *,
    out_dir: Path,
    required_backends: tuple[str, ...] | list[str] | set[str] | None,
    docker_image: str,
    qemu_base_image: Path | None,
    arena_intent_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = normalize_required_backends(required_backends)
    qemu_image_path = Path(qemu_base_image) if qemu_base_image is not None else None
    receipt = {
        "schema": BACKEND_APPLICABILITY_SCHEMA,
        "status": "PASS",
        "required_backends": list(required),
        "inapplicable_backends": [backend for backend in ("docker", "qemu") if backend not in required],
        "docker": {"required": "docker" in required, "image": docker_image},
        "qemu": {
            "required": "qemu" in required,
            "base_image": None if qemu_image_path is None else str(qemu_image_path),
            "base_image_sha256": sha256_file(qemu_image_path) if qemu_image_path is not None and qemu_image_path.is_file() else None,
        },
        "created_at": utc(),
    }
    if "qemu" in required and receipt["qemu"]["base_image_sha256"] is None:
        receipt["qemu"]["pre_execution_blocker"] = "qemu_base_image_not_supplied"
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(Path(out_dir) / "backend-applicability-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def _docker_inventory(name: str, state_dir: Path, timeout: int) -> dict[str, Any]:
    ps = _run(["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"], timeout=timeout)
    names = [line for line in ps.stdout.splitlines() if line]
    residual = []
    if name in names:
        residual.append({"kind": "docker_container", "id": name})
    if state_dir.exists():
        residual.append({"kind": "host_state_dir", "id": str(state_dir)})
    return {
        "schema": "battle.am05.cleanup_inventory.v1",
        "docker_ps_exit_code": ps.returncode,
        "container_names": names,
        "host_state_dir_exists": state_dir.exists(),
        "residual_resources": residual,
        "checked_at": utc(),
    }


def run_docker_lifecycle(
    *,
    out_dir: Path,
    image: str = "python:3.12-slim",
    budget: ResourceBudget | None = None,
    network_policy: NetworkPolicy | None = None,
    arena_intent_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a real Docker start/readback/cleanup cycle and write typed receipts."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = budget or ResourceBudget()
    network_policy = network_policy or NetworkPolicy()
    budget.validate()
    state_dir = out_dir / "docker-state"
    shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True)
    name = f"battle-am05-{int(time.time() * 1000)}"
    events: list[dict[str, Any]] = []

    def mark(phase: str) -> None:
        events.append({"phase": phase, "at": utc()})

    mark("docker_preflight_started")
    if shutil.which("docker") is None:
        return _docker_blocked(out_dir, reason="docker_cli_unavailable")
    info = _run(["docker", "info"], timeout=budget.timeout_seconds)
    if info.returncode != 0:
        return _docker_blocked(out_dir, reason="docker_daemon_unavailable", detail=info.stderr.strip())

    start_cmd = [
        "docker", "run", "--detach", "--name", name,
        "--network", network_policy.docker_network,
        "--cpus", str(budget.cpus),
        "--memory", f"{budget.memory_mb}m",
        "--pids-limit", str(budget.pids_limit),
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={budget.tmpfs_mb}m",
        "--mount", f"type=bind,src={state_dir},dst=/battle-state",
        image, "sh", "-c", "printf 'warm-state-ok' > /battle-state/warm.txt; sleep 300",
    ]
    cleanup_steps: list[dict[str, Any]] = []
    status = "PASS"
    problems: list[str] = []
    inspect: dict[str, Any] = {"status": "FAIL", "payload": {}}
    readback = ""
    container_id = ""
    start_attempted = False
    interruption: str | None = None
    try:
        mark("docker_start_requested")
        start_attempted = True
        start = _run(start_cmd, timeout=budget.timeout_seconds)
        container_id = start.stdout.strip()
        if start.returncode != 0 or not container_id:
            status = "BLOCKED"
            problems.append("docker_start_timeout" if start.returncode == 124 else "docker_start_failed")
        else:
            mark("docker_started")
            inspect = _inspect_container(name, budget.timeout_seconds)
            if inspect.get("status") != "PASS":
                status = "FAIL"
                problems.append("docker_inspect_failed")
            read = _run(["docker", "exec", name, "cat", "/battle-state/warm.txt"], timeout=budget.timeout_seconds)
            readback = read.stdout.strip()
            mark("warm_state_readback")
            if read.returncode != 0 or readback != "warm-state-ok":
                status = "FAIL"
                problems.append("warm_state_readback_failed")
    except KeyboardInterrupt:
        interruption = "supervisor_interruption"
        status = "FAIL"
        problems.append(interruption)
    except Exception as exc:
        interruption = f"lifecycle_exception:{type(exc).__name__}"
        status = "FAIL"
        problems.append(interruption)
    finally:
        if container_id:
            stop = _run(["docker", "stop", name], timeout=budget.timeout_seconds)
            cleanup_steps.append({"command": "docker stop", "exit_code": stop.returncode, "stderr_tail": stop.stderr[-300:]})
            mark("docker_stopped")
            rm = _run(["docker", "rm", name], timeout=budget.timeout_seconds)
            cleanup_steps.append({"command": "docker rm", "exit_code": rm.returncode, "stderr_tail": rm.stderr[-300:]})
            mark("docker_removed")
        elif start_attempted:
            rm = _run(["docker", "rm", "-f", name], timeout=budget.timeout_seconds)
            cleanup_steps.append({"command": "docker rm -f partial-start", "exit_code": rm.returncode, "stderr_tail": rm.stderr[-300:]})
            mark("docker_partial_start_cleanup_attempted")

    host_warm_path = state_dir / "warm.txt"
    host_readback = host_warm_path.read_text(encoding="utf-8") if host_warm_path.is_file() else ""
    warm_receipt = {
        "schema": WARM_STATE_SCHEMA,
        "status": "PASS" if readback == "warm-state-ok" and host_readback == "warm-state-ok" else "FAIL",
        "container_readback": readback,
        "host_readback_sha256": _sha256_bytes(host_readback.encode("utf-8")) if host_readback else None,
        "state_file_existed_before_cleanup": host_warm_path.is_file(),
        "writable_state": {"host_state_dir": str(state_dir), "container_mount": "/battle-state"},
        "created_at": utc(),
    }
    write_json(out_dir / "warm-state-receipt.json", bind_intent(warm_receipt, arena_intent_binding) if arena_intent_binding else warm_receipt)
    shutil.rmtree(state_dir, ignore_errors=True)
    inventory = _docker_inventory(name, state_dir, budget.timeout_seconds)
    cleanup_receipt = {
        "schema": RESET_CLEANUP_SCHEMA,
        "status": "PASS" if not inventory["residual_resources"] and inventory["docker_ps_exit_code"] == 0 else "FAIL",
        "container_removed": not any(item["kind"] == "docker_container" for item in inventory["residual_resources"]),
        "host_state_dir_removed": not state_dir.exists(),
        "steps": cleanup_steps,
        "post_cleanup_inventory": inventory,
        "interruption": interruption,
        "checked_at": utc(),
    }
    write_json(out_dir / "reset-cleanup-receipt.json", bind_intent(cleanup_receipt, arena_intent_binding) if arena_intent_binding else cleanup_receipt)

    inspect_payload = inspect.get("payload") if inspect.get("status") == "PASS" else {}
    budget_proof = _budget_proof(inspect_payload, budget) if inspect_payload else {"schema": "battle.am05.resource_budget_proof.v1", "status": "FAIL", "problems": ["missing_docker_inspect"]}
    net_proof = _network_proof(inspect_payload, network_policy) if inspect_payload else {"schema": NETWORK_POLICY_SCHEMA, "status": "FAIL", "problems": ["missing_docker_inspect"]}
    if budget_proof["status"] != "PASS":
        status = "FAIL" if status == "PASS" else status
        problems.append("resource_budget_not_enforced")
    if net_proof["status"] != "PASS":
        status = "FAIL" if status == "PASS" else status
        problems.append("network_policy_not_proven")
    if warm_receipt["status"] != "PASS":
        status = "FAIL" if status == "PASS" else status
    if cleanup_receipt["status"] != "PASS":
        status = "FAIL" if status == "PASS" else status
        problems.append("cleanup_not_confirmed")

    receipt = {
        "schema": LIFECYCLE_SCHEMA,
        "runtime": "docker",
        "status": status,
        "mocked": False,
        "live": True,
        "image": image,
        "resource_identity": {"container_name": name, "container_id": container_id, "image": image},
        "container_name": name,
        "container_id": container_id,
        "resource_budget_proof": budget_proof,
        "network_policy_proof": net_proof,
        "warm_state_receipt": _receipt_ref(out_dir / "warm-state-receipt.json"),
        "reset_cleanup_receipt": _receipt_ref(out_dir / "reset-cleanup-receipt.json"),
        "execution_artifacts": {"warm_state": str(host_warm_path), "cleanup_inventory": inventory},
        "lifecycle_timestamps": events,
        "problems": problems,
        "created_at": utc(),
    }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(out_dir / "docker-lifecycle-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def _qemu_inventory(out_dir: Path) -> dict[str, Any]:
    round_dirs = sorted(str(path) for path in Path(out_dir).glob("vm-round-*") if path.exists())
    return {
        "schema": "battle.am05.qemu_cleanup_inventory.v1",
        "residual_resources": [{"kind": "qemu_round_dir", "id": path} for path in round_dirs],
        "checked_at": utc(),
    }


def attempt_qemu_lifecycle(
    *,
    out_dir: Path,
    base_image: Path | None = None,
    authorization: dict[str, Any] | None = None,
    arena_intent_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attempt the QEMU path only when host tools and an authorized base image exist."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = utc()
    image_path = Path(base_image) if base_image is not None else None
    try:
        from .qemu_vm import preflight_tools, vm_round

        preflight_tools()
    except Exception as exc:
        receipt = {
            "schema": QEMU_SCHEMA,
            "runtime": "qemu",
            "status": "BLOCKED",
            "mocked": False,
            "live": False,
            "blocker": "qemu_host_tools_unavailable",
            "detail": str(exc),
            "started_at": started,
            "finished_at": utc(),
            "claims": {"proves": [], "does_not_prove": ["QEMU boot or VM isolation"]},
        }
    else:
        if image_path is None or not image_path.is_file():
            receipt = {
                "schema": QEMU_SCHEMA,
                "runtime": "qemu",
                "status": "BLOCKED",
                "mocked": False,
                "live": False,
                "blocker": "qemu_base_image_not_supplied",
                "started_at": started,
                "finished_at": utc(),
                "claims": {"proves": ["QEMU host tools are installed"], "does_not_prove": ["QEMU boot or VM isolation"]},
            }
        elif not authorization or authorization.get("status") != "PASS":
            receipt = {
                "schema": QEMU_SCHEMA,
                "runtime": "qemu",
                "status": "BLOCKED",
                "mocked": False,
                "live": False,
                "blocker": "qemu_authorization_missing_or_failed",
                "base_image": str(image_path),
                "base_image_sha256": sha256_file(image_path),
                "started_at": started,
                "finished_at": utc(),
                "claims": {"proves": ["QEMU host tools are installed", "QEMU base image exists"], "does_not_prove": ["authorized QEMU boot"]},
            }
        else:
            try:
                vm_receipt = vm_round(
                    image_path,
                    "printf 'qemu-warm-state-ok\n' > /tmp/battle-am05-warm.txt; cat /tmp/battle-am05-warm.txt",
                    authorization=authorization,
                    out_dir=out_dir,
                    boot_timeout=int(authorization.get("boot_timeout_seconds", 300)),
                )
                vm_ref = _receipt_ref(out_dir / "vm-round-receipt.json")
                passed = vm_receipt.booted and vm_receipt.exit_code == 0 and "qemu-warm-state-ok" in vm_receipt.stdout
                cleanup_status = vm_receipt.cleanup_receipt.get("status") if isinstance(vm_receipt.cleanup_receipt, dict) else None
                receipt = {
                    "schema": QEMU_SCHEMA,
                    "runtime": "qemu",
                    "status": "PASS" if passed and cleanup_status == "PASS" else "FAIL",
                    "mocked": False,
                    "live": True,
                    "base_image": str(image_path),
                    "base_image_sha256": sha256_file(image_path),
                    "authorization_id": authorization.get("authorization_id"),
                    "vm_round_receipt": vm_ref,
                    "cleanup_status": cleanup_status,
                    "post_cleanup_inventory": _qemu_inventory(out_dir),
                    "started_at": started,
                    "finished_at": utc(),
                    "claims": {"proves": ["authorized QEMU boot, payload execution, and teardown were attempted"], "does_not_prove": ["complete VM isolation or arbitrary-workload safety"]},
                }
            except BaseException as exc:
                receipt = {
                    "schema": QEMU_SCHEMA,
                    "runtime": "qemu",
                    "status": "FAIL",
                    "mocked": False,
                    "live": False,
                    "base_image": str(image_path),
                    "base_image_sha256": sha256_file(image_path),
                    "authorization_id": authorization.get("authorization_id"),
                    "failure": f"{type(exc).__name__}: {exc}",
                    "interruption": "supervisor_interruption" if isinstance(exc, KeyboardInterrupt) else None,
                    "post_cleanup_inventory": _qemu_inventory(out_dir),
                    "started_at": started,
                    "finished_at": utc(),
                    "claims": {"proves": ["QEMU lifecycle failed closed"], "does_not_prove": ["QEMU boot or VM isolation"]},
                }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(out_dir / "qemu-lifecycle-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def artifact_manifest(root: Path, *, arena_intent_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "artifact-manifest.json":
            continue
        entries.append({"path": str(path.relative_to(root)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    receipt = {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "status": "PASS",
        "root": str(root),
        "artifacts": entries,
        "artifact_count": len(entries),
        "created_at": utc(),
    }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(root / "artifact-manifest.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def parent_gate_receipt(
    *,
    out_dir: Path,
    arena_intent_binding: dict[str, Any] | None,
    child_gates: dict[str, Any],
    docker_receipt: dict[str, Any],
    qemu_receipt: dict[str, Any],
    backend_applicability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent_gate = _intent_status(arena_intent_binding)
    child_gate = _child_gate_status(child_gates)
    required = normalize_required_backends((backend_applicability or {}).get("required_backends"))
    blockers: list[str] = []
    if intent_gate.get("status") != "PASS":
        blockers.append("am03_intent_gate_failed")
    if child_gate.get("status") != "PASS":
        blockers.append("am01_am02_child_gates_failed")
    if "docker" in required and docker_receipt.get("status") != "PASS":
        blockers.append("docker_lifecycle_not_passed")
    if "qemu" in required and qemu_receipt.get("status") != "PASS":
        blockers.append("qemu_lifecycle_not_passed")
    if "docker" not in required and docker_receipt.get("status") != "INAPPLICABLE":
        blockers.append("docker_marked_inapplicable_but_executed_or_failed")
    if "qemu" not in required and qemu_receipt.get("status") != "INAPPLICABLE":
        blockers.append("qemu_marked_inapplicable_but_executed_or_failed")
    status = "PASS" if not blockers else "FAIL"
    scope = "combined_docker_qemu" if set(required) == {"docker", "qemu"} else "docker_only" if required == ("docker",) else "qemu_only"
    receipt = {
        "schema": PARENT_GATE_SCHEMA,
        "status": status,
        "qualification_scope": scope,
        "fail_closed": True,
        "intent_gate": intent_gate,
        "child_gate_binding": child_gate,
        "backend_applicability": None if backend_applicability is None else {"path": backend_applicability.get("path"), "sha256": backend_applicability.get("sha256"), "required_backends": backend_applicability.get("required_backends")},
        "docker_lifecycle": {"status": docker_receipt.get("status"), "path": docker_receipt.get("path"), "sha256": docker_receipt.get("sha256")},
        "qemu_lifecycle": {"status": qemu_receipt.get("status"), "blocker": qemu_receipt.get("blocker"), "path": qemu_receipt.get("path"), "sha256": qemu_receipt.get("sha256")},
        "blockers": blockers,
        "gated_at": utc(),
    }
    if arena_intent_binding:
        receipt = bind_intent(receipt, arena_intent_binding)
    path = write_json(Path(out_dir) / "parent-gate-receipt.json", receipt)
    receipt.update(_receipt_ref(path))
    return receipt


def run_am05_lifecycle(
    *,
    out_dir: Path,
    arena_intent_binding: dict[str, Any] | None,
    child_gates: dict[str, Any],
    docker_image: str = "python:3.12-slim",
    budget: ResourceBudget | None = None,
    network_policy: NetworkPolicy | None = None,
    qemu_base_image: Path | None = None,
    qemu_authorization: dict[str, Any] | None = None,
    required_backends: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Run AM-05 lifecycle receipts with explicit backend applicability."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    required = normalize_required_backends(required_backends)
    applicability = backend_applicability_receipt(
        out_dir=out_dir,
        required_backends=required,
        docker_image=docker_image,
        qemu_base_image=qemu_base_image,
        arena_intent_binding=arena_intent_binding,
    )
    docker_receipt = run_docker_lifecycle(
        out_dir=out_dir,
        image=docker_image,
        budget=budget,
        network_policy=network_policy,
        arena_intent_binding=arena_intent_binding,
    ) if "docker" in required else _docker_inapplicable(out_dir, reason="backend_not_required", arena_intent_binding=arena_intent_binding)
    qemu_receipt = attempt_qemu_lifecycle(
        out_dir=out_dir,
        base_image=qemu_base_image,
        authorization=qemu_authorization,
        arena_intent_binding=arena_intent_binding,
    ) if "qemu" in required else _qemu_inapplicable(out_dir, reason="backend_not_required", arena_intent_binding=arena_intent_binding)
    gate = parent_gate_receipt(
        out_dir=out_dir,
        arena_intent_binding=arena_intent_binding,
        child_gates=child_gates,
        docker_receipt=docker_receipt,
        qemu_receipt=qemu_receipt,
        backend_applicability=applicability,
    )
    manifest = artifact_manifest(out_dir, arena_intent_binding=arena_intent_binding)
    run_receipt = {
        "schema": RUN_SCHEMA,
        "status": "PASS" if gate.get("status") == "PASS" else "FAIL",
        "mocked": False,
        "live": docker_receipt.get("live") is True or qemu_receipt.get("live") is True,
        "qualification_scope": gate.get("qualification_scope"),
        "backend_applicability": {"path": applicability.get("path"), "sha256": applicability.get("sha256"), "required_backends": applicability.get("required_backends")},
        "docker_lifecycle": {"path": docker_receipt.get("path"), "sha256": docker_receipt.get("sha256"), "status": docker_receipt.get("status")},
        "qemu_lifecycle": {"path": qemu_receipt.get("path"), "sha256": qemu_receipt.get("sha256"), "status": qemu_receipt.get("status"), "blocker": qemu_receipt.get("blocker")},
        "parent_gate": {"path": gate.get("path"), "sha256": gate.get("sha256"), "status": gate.get("status")},
        "artifact_manifest": {"path": manifest.get("path"), "sha256": manifest.get("sha256"), "status": manifest.get("status")},
        "created_at": utc(),
    }
    if arena_intent_binding:
        run_receipt = bind_intent(run_receipt, arena_intent_binding)
    path = write_json(out_dir / "am05-run-receipt.json", run_receipt)
    run_receipt.update(_receipt_ref(path))
    return run_receipt
