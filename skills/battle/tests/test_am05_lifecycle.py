from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from battle_skill.am05_lifecycle import (
    NetworkPolicy,
    ResourceBudget,
    artifact_manifest,
    attempt_qemu_lifecycle,
    parent_gate_receipt,
    run_am05_lifecycle,
    run_docker_lifecycle,
)
from battle_skill.arena_intent import build_arena_maker_intent, freeze_arena_maker_intent


def _intent(tmp_path: Path) -> dict[str, Any]:
    contract = tmp_path / "arena-contract.json"
    contract.write_text('{"schema":"test"}\n', encoding="utf-8")
    return freeze_arena_maker_intent(
        build_arena_maker_intent(
            battle_id="am05-test",
            target_identity="target@sha256:abc",
            authorization_manifest=None,
            arena_contract=contract,
            runtime_mode="docker_qemu_lifecycle",
            round_count=1,
            objective="Prove AM-05 lifecycle receipts fail closed.",
        ),
        tmp_path / "arena-maker-intent.json",
    )


def _child_gates() -> dict[str, Any]:
    return {
        "AM-01": {"schema": "battle.am01.authorization_gate.v1", "status": "PASS", "receipt": "auth"},
        "AM-02": {"schema": "battle.am02.child_gate.v1", "status": "PASS", "receipt": "child"},
    }


def test_parent_gate_binds_intent_and_fails_closed_on_missing_child_gate(tmp_path: Path) -> None:
    binding = _intent(tmp_path)
    docker = {"status": "PASS", "path": "docker.json", "sha256": "sha256:" + "a" * 64}
    qemu = {"status": "BLOCKED", "blocker": "qemu_host_tools_unavailable", "path": "qemu.json", "sha256": "sha256:" + "b" * 64}

    combined = parent_gate_receipt(
        out_dir=tmp_path,
        arena_intent_binding=binding,
        child_gates=_child_gates(),
        docker_receipt=docker,
        qemu_receipt=qemu,
        backend_applicability={"required_backends": ["docker", "qemu"]},
    )
    assert combined["status"] == "FAIL"
    assert "qemu_lifecycle_not_passed" in combined["blockers"]

    scoped = parent_gate_receipt(
        out_dir=tmp_path / "docker-only",
        arena_intent_binding=binding,
        child_gates=_child_gates(),
        docker_receipt=docker,
        qemu_receipt={"status": "INAPPLICABLE", "path": "qemu.json", "sha256": "sha256:" + "b" * 64},
        backend_applicability={"required_backends": ["docker"]},
    )
    assert scoped["status"] == "PASS"
    assert scoped["qualification_scope"] == "docker_only"
    assert scoped["arena_maker_intent"]["sha256"] == binding["arena_maker_intent_sha256"]

    failed = parent_gate_receipt(
        out_dir=tmp_path / "bad",
        arena_intent_binding=binding,
        child_gates={"AM-01": _child_gates()["AM-01"]},
        docker_receipt=docker,
        qemu_receipt={"status": "INAPPLICABLE", "path": "qemu.json", "sha256": "sha256:" + "b" * 64},
        backend_applicability={"required_backends": ["docker"]},
    )
    assert failed["status"] == "FAIL"
    assert "am01_am02_child_gates_failed" in failed["blockers"]


def test_docker_lifecycle_receipts_enforce_budget_network_warm_state_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _intent(tmp_path)
    budget = ResourceBudget(cpus=0.25, memory_mb=64, pids_limit=32, tmpfs_mb=4, timeout_seconds=5)
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: list[str], *, timeout: int = 30) -> Result:
        calls.append(command)
        if command[:2] == ["docker", "info"]:
            return Result()
        if command[:2] == ["docker", "run"]:
            mount = next(item for item in command if item.startswith("type=bind,"))
            src = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            Path(src).mkdir(parents=True, exist_ok=True)
            (Path(src) / "warm.txt").write_text("warm-state-ok", encoding="utf-8")
            return Result(stdout="container123\n")
        if command[:2] == ["docker", "inspect"]:
            payload = [{
                "HostConfig": {
                    "Memory": budget.memory_mb * 1024 * 1024,
                    "NanoCpus": int(budget.cpus * 1_000_000_000),
                    "PidsLimit": budget.pids_limit,
                    "ReadonlyRootfs": True,
                    "NetworkMode": "none",
                },
                "NetworkSettings": {"Networks": {}},
            }]
            return Result(stdout=json.dumps(payload))
        if command[:3] == ["docker", "exec", command[2]]:
            return Result(stdout="warm-state-ok")
        if command[:2] in (["docker", "stop"], ["docker", "rm"]):
            return Result()
        if command[:2] == ["docker", "ps"]:
            return Result(stdout="")
        raise AssertionError(command)

    monkeypatch.setattr("battle_skill.am05_lifecycle.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr("battle_skill.am05_lifecycle._run", fake_run)

    receipt = run_docker_lifecycle(
        out_dir=tmp_path / "run",
        image="python:3.12-slim",
        budget=budget,
        network_policy=NetworkPolicy(),
        arena_intent_binding=binding,
    )

    assert receipt["status"] == "PASS"
    assert receipt["mocked"] is False
    assert receipt["resource_budget_proof"]["status"] == "PASS"
    assert receipt["network_policy_proof"]["status"] == "PASS"
    assert json.loads(Path(receipt["warm_state_receipt"]["path"]).read_text())["status"] == "PASS"
    assert json.loads(Path(receipt["reset_cleanup_receipt"]["path"]).read_text())["host_state_dir_removed"] is True
    assert any(call[:2] == ["docker", "rm"] for call in calls)


def test_qemu_unavailable_emits_typed_blocker_without_claiming_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import battle_skill.qemu_vm as qemu_vm

    monkeypatch.setattr(qemu_vm, "preflight_tools", lambda: (_ for _ in ()).throw(RuntimeError("missing qemu")))
    receipt = attempt_qemu_lifecycle(out_dir=tmp_path)

    assert receipt["schema"] == "battle.am05.qemu_lifecycle_receipt.v1"
    assert receipt["status"] == "BLOCKED"
    assert receipt["blocker"] == "qemu_host_tools_unavailable"
    assert "QEMU boot or VM isolation" in receipt["claims"]["does_not_prove"]


def test_artifact_manifest_hashes_receipts(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.txt").write_text("hello", encoding="utf-8")

    receipt = artifact_manifest(tmp_path)

    paths = {item["path"] for item in receipt["artifacts"]}
    assert paths == {"a.json", "nested/b.txt"}
    assert all(item["sha256"].startswith("sha256:") for item in receipt["artifacts"])
    assert Path(receipt["path"]).is_file()


def test_run_am05_defaults_to_combined_and_blocks_without_qemu_base_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _intent(tmp_path)
    docker = {"status": "PASS", "path": str(tmp_path / "docker.json"), "sha256": "sha256:" + "a" * 64, "live": True}
    monkeypatch.setattr("battle_skill.am05_lifecycle.run_docker_lifecycle", lambda **_: docker)
    monkeypatch.setattr("battle_skill.am05_lifecycle.attempt_qemu_lifecycle", lambda **_: {"status": "BLOCKED", "blocker": "qemu_base_image_not_supplied", "path": str(tmp_path / "qemu.json"), "sha256": "sha256:" + "b" * 64, "live": False})

    receipt = run_am05_lifecycle(out_dir=tmp_path / "run", arena_intent_binding=binding, child_gates=_child_gates())
    gate = json.loads(Path(receipt["parent_gate"]["path"]).read_text())
    applicability = json.loads(Path(receipt["backend_applicability"]["path"]).read_text())

    assert receipt["status"] == "FAIL"
    assert receipt["qualification_scope"] == "combined_docker_qemu"
    assert applicability["required_backends"] == ["docker", "qemu"]
    assert applicability["qemu"]["pre_execution_blocker"] == "qemu_base_image_not_supplied"
    assert "qemu_lifecycle_not_passed" in gate["blockers"]


def test_run_am05_docker_only_marks_qemu_inapplicable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _intent(tmp_path)
    docker = {"status": "PASS", "path": str(tmp_path / "docker.json"), "sha256": "sha256:" + "a" * 64, "live": True}
    monkeypatch.setattr("battle_skill.am05_lifecycle.run_docker_lifecycle", lambda **_: docker)

    receipt = run_am05_lifecycle(
        out_dir=tmp_path / "run",
        arena_intent_binding=binding,
        child_gates=_child_gates(),
        required_backends=("docker",),
    )
    qemu = json.loads(Path(receipt["qemu_lifecycle"]["path"]).read_text())

    assert receipt["status"] == "PASS"
    assert receipt["qualification_scope"] == "docker_only"
    assert qemu["status"] == "INAPPLICABLE"
    assert "QEMU boot or VM isolation" in qemu["claims"]["does_not_prove"]


def test_docker_cleanup_failure_preserves_residual_resource_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    budget = ResourceBudget(timeout_seconds=5)

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: list[str], *, timeout: int = 30) -> Result:
        if command[:2] == ["docker", "info"]:
            return Result()
        if command[:2] == ["docker", "run"]:
            mount = next(item for item in command if item.startswith("type=bind,"))
            src = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            Path(src).mkdir(parents=True, exist_ok=True)
            (Path(src) / "warm.txt").write_text("warm-state-ok", encoding="utf-8")
            return Result(stdout="container123\n")
        if command[:2] == ["docker", "inspect"]:
            return Result(stdout=json.dumps([{
                "HostConfig": {"Memory": budget.memory_mb * 1024 * 1024, "NanoCpus": int(budget.cpus * 1_000_000_000), "PidsLimit": budget.pids_limit, "ReadonlyRootfs": True, "NetworkMode": "none"},
                "NetworkSettings": {"Networks": {}},
            }]))
        if command[:2] == ["docker", "exec"]:
            return Result(stdout="warm-state-ok")
        if command[:2] in (["docker", "stop"], ["docker", "rm"]):
            return Result(returncode=1, stderr="teardown failed")
        if command[:2] == ["docker", "ps"]:
            return Result(stdout=command[4].removeprefix("name=") + "\n")
        raise AssertionError(command)

    monkeypatch.setattr("battle_skill.am05_lifecycle.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr("battle_skill.am05_lifecycle._run", fake_run)

    receipt = run_docker_lifecycle(out_dir=tmp_path / "run", budget=budget)
    cleanup = json.loads(Path(receipt["reset_cleanup_receipt"]["path"]).read_text())

    assert receipt["status"] == "FAIL"
    assert "cleanup_not_confirmed" in receipt["problems"]
    assert cleanup["status"] == "FAIL"
    assert cleanup["post_cleanup_inventory"]["residual_resources"] == [{"kind": "docker_container", "id": receipt["container_name"]}]
