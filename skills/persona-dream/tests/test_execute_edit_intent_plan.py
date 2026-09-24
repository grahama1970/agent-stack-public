"""PD-UX-05 tests: validated edit plan -> sanctioned revision/Tau handoff.

Covers: hash-bound authorization (changed plans never inherit approval),
execution = queueing the existing repair workflow (never running it),
candidate-media-only boundary (frozen source revision and unrelated accepted
assets unchanged), interrupted-execution reconciliation (timeout is not proof
no job exists; ambiguous outcomes reconcile or block; failures block), and the
read-only status surface. Simulated provider outcomes qualify integration and
recovery only — never live-generation proof.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RUN = SKILL_ROOT / "fixtures" / "pd-ux-03" / "golden-run"
INTENT = SKILL_ROOT / "fixtures" / "pd-ux-03" / "golden.edit-intent.json"
SCRIPT = SKILL_ROOT / "scripts" / "execute_edit_intent_plan.py"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


exe_mod = load_module("persona_dream_execute_edit_intent_plan", SCRIPT)
from idea_lineage import canonical_json_bytes, sha256_file  # noqa: E402


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


class Rig:
    """A tmp run root + compiled plan + matching authorization."""

    def __init__(self, tmp_path: Path) -> None:
        self.run_root = tmp_path / "run"
        shutil.copytree(FIXTURE_RUN, self.run_root)
        self.plan_dir = tmp_path / "plan"
        subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "compile_edit_intent_plan.py"),
             "--intent", str(INTENT), "--run-root", str(self.run_root),
             "--output-dir", str(self.plan_dir), "--json"],
            capture_output=True, text=True, check=True,
        )
        self.plan_path = self.plan_dir / "edit_intent_plan.json"
        self.plan = json.loads(self.plan_path.read_text())
        self.auth_path = tmp_path / "authorization.json"
        self.write_authorization()

    def write_authorization(self, **overrides: str) -> None:
        revision_root = self.run_root / ".persona-dream" / "revisions" / self.plan["target"]["revision_id"]
        auth = {
            "schema": exe_mod.AUTH_SCHEMA,
            "operation": "execute_edit_intent_plan",
            "approved_by": "human-test-approver",
            "plan_id": self.plan["plan_id"],
            "plan_sha256": sha256_file(self.plan_path),
            "payload_sha256": "sha256:" + hashlib.sha256(
                canonical_json_bytes({k: v for k, v in self.plan.items() if k != "plan_id"})
            ).hexdigest(),
            "revision_index_sha256": sha256_file(revision_root / "revision_artifact_index.json"),
            "dependency_graph_sha256": sha256_file(SKILL_ROOT / self.plan["dependency_graph"]["contract"]),
            "target_artifact_id": self.plan["target"]["artifact_id"],
            "target_expected_sha256": self.plan["target"]["expected_sha256"],
        }
        auth.update(overrides)
        self.auth_path.write_text(json.dumps(auth, indent=2, sort_keys=True) + "\n")

    def execute(self) -> dict:
        proc = cli("--run-root", str(self.run_root), "--plan", str(self.plan_dir),
                   "--authorization", str(self.auth_path))
        payload = json.loads(proc.stdout)
        payload["_exit"] = proc.returncode
        return payload

    def record(self, outcome: str) -> dict:
        proc = cli("--run-root", str(self.run_root), "--record-outcome", outcome,
                   "--plan-id", self.plan["plan_id"])
        return json.loads(proc.stdout)

    def status(self) -> dict:
        proc = cli("--run-root", str(self.run_root), "--status", "--plan-id", self.plan["plan_id"])
        return json.loads(proc.stdout)

    def revision_digest(self) -> str:
        return exe_mod.digest_tree(
            self.run_root / ".persona-dream" / "revisions" / self.plan["target"]["revision_id"]
        )


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- golden handoff ---------------------------------------------------------------


def test_execute_hands_off_to_sanctioned_repair_queue(rig: Rig) -> None:
    before = rig.revision_digest()
    result = rig.execute()
    assert result["_exit"] == 0
    assert result["status"] == "PASS_EDIT_PLAN_HANDED_OFF"
    assert result["state"] == "HANDED_OFF"
    assert result["actual_provider_call_attempts"] == 0
    assert result["boundary"]["successor_activation"] is False
    assert result["boundary"]["memory_writes"] == 0
    # Frozen source revision (incl. unrelated accepted assets) is unchanged.
    assert rig.revision_digest() == before
    # The existing queue/work-order shapes were written, not executed.
    wo = json.loads((rig.run_root / result["work_order"]).read_text())
    assert wo["schemaVersion"] == "persona_dream.repair_work_order.v1"
    assert wo["status"] == "REQUESTED"
    assert wo["policy"]["candidate_media_only"] is True
    queue = json.loads((rig.run_root / result["queue_item"]).read_text())
    assert queue["schemaVersion"] == "persona_dream.tau_repair_queue_item.v1"
    assert queue["status"] == "QUEUED"
    application = json.loads((rig.run_root / result["application"]).read_text())
    assert application["schemaVersion"] == "persona_dream.edit_plan_application.v1"
    assert application["policy"]["no_successor_activation"] is True
    assert application["stale_artifacts_retained_as_history"], "accepted/stale history retained, not replaced"
    dag = json.loads((rig.run_root / ".persona-dream" / "repair" / "tau-runs" / f"editplan-{rig.plan['plan_id']}" / "dag-contract.json").read_text())
    assert dag["schema"] == "tau.dag_contract.v1"
    assert dag["terminal_nodes"] == ["human"], "human review remains the terminal node"


def test_status_is_read_only_record_projection(rig: Rig) -> None:
    rig.execute()
    status = rig.status()
    assert status["schema"] == "persona_dream.edit_plan_job_status.v1"
    assert status["read_only"] is True
    assert status["state"] == "HANDED_OFF"
    assert status["work_order_id"] == f"editplan-{rig.plan['plan_id']}"
    before = rig.revision_digest()
    rig.status()
    assert rig.revision_digest() == before


# --- idempotence + reconciliation -------------------------------------------------


def test_rerun_is_idempotent_no_second_handoff(rig: Rig) -> None:
    first = rig.execute()
    second = rig.execute()
    assert second["status"] == "PASS_EDIT_PLAN_HANDED_OFF"
    assert second["reconciled"] is True
    # Exactly one work order + queue item exist.
    repair = rig.run_root / ".persona-dream" / "repair"
    assert len(list((repair / "work-orders").glob("*.json"))) == 1
    assert len(list((repair / "tau-queue").glob("*.json"))) == 1


def test_timeout_is_not_proof_no_job_exists(rig: Rig) -> None:
    rig.execute()
    assert rig.record("TIMEOUT")["state"] == "AMBIGUOUS"
    result = rig.execute()
    assert result["status"] == "PASS_EDIT_PLAN_HANDED_OFF"
    assert result["reconciled"] is True
    assert result["note"].startswith("reconciled interrupted")
    assert len(list((rig.run_root / ".persona-dream" / "repair" / "work-orders").glob("*.json"))) == 1


def test_ambiguous_outcome_without_durable_handoff_blocks(rig: Rig) -> None:
    rig.execute()
    rig.record("PROVIDER_AMBIGUOUS")
    # The downstream workflow consumed/moved the handoff artifacts.
    shutil.rmtree(rig.run_root / ".persona-dream" / "repair")
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_AMBIGUOUS_EXECUTION"
    assert result["boundary"]["provider_call_attempts"] == 0


def test_recorded_provider_failure_blocks_retry_and_retains_evidence(rig: Rig) -> None:
    rig.execute()
    recorded = rig.record("PROVIDER_FAILURE")
    assert recorded["simulated"] is True
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_PREVIOUS_EXECUTION_FAILURE"
    events = result["details"]["evidence_events"]
    assert events, "failure evidence retained in the job record"


def test_interrupted_executing_job_without_handoff_blocks(rig: Rig) -> None:
    # Simulate a crash after the job record write but before the handoff became durable.
    job_path = exe_mod.job_path(rig.run_root, rig.plan["plan_id"])
    job_path.parent.mkdir(parents=True, exist_ok=True)
    auth_sha = sha256_file(rig.auth_path)
    job_path.write_text(json.dumps({
        "schema": exe_mod.JOB_SCHEMA, "job_id": f"job_{rig.plan['plan_id']}",
        "plan_id": rig.plan["plan_id"], "state": "EXECUTING",
        "authorization_sha256": auth_sha, "handoff": None, "events": [],
    }))
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_INTERRUPTED_EXECUTION_UNRESOLVED"


def test_new_authorization_cannot_take_over_existing_job(rig: Rig) -> None:
    rig.execute()
    rig.write_authorization(target_expected_sha256="sha256:" + "0" * 64)
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] in {"BLOCKED_JOB_AUTHORIZATION_MISMATCH", "BLOCKED_AUTHORIZATION_PLAN_CHANGED"}


# --- authorization binding --------------------------------------------------------


def test_changed_plan_never_inherits_approval(rig: Rig) -> None:
    rig.plan["requested_change"]["note"] = "silently changed after approval"
    rig.plan_path.write_text(json.dumps(rig.plan, indent=2, sort_keys=True) + "\n")
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] in {"BLOCKED_PLAN_ID_MISMATCH", "BLOCKED_PLAN_SHA256_MISMATCH"}


def test_authorization_hash_mismatch_blocks(rig: Rig) -> None:
    rig.write_authorization(plan_sha256="sha256:" + "1" * 64)
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_AUTHORIZATION_PLAN_CHANGED"


def test_authorization_for_other_operation_blocked(rig: Rig) -> None:
    rig.write_authorization(operation="some_other_operation")
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_AUTHORIZATION_OPERATION"


def test_unsigned_authorization_blocked(rig: Rig) -> None:
    rig.write_authorization(approved_by="")
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_AUTHORIZATION_UNSIGNED"


def test_stale_target_blocks(rig: Rig) -> None:
    target = rig.run_root / ".persona-dream" / "revisions" / rig.plan["target"]["revision_id"] / rig.plan["target"]["relative_path"]
    target.write_bytes(b"mutated after planning")
    result = rig.execute()
    assert result["_exit"] == 2
    assert result["code"] == "BLOCKED_STALE_TARGET"


def test_missing_authorization_blocks(rig: Rig) -> None:
    proc = cli("--run-root", str(rig.run_root), "--plan", str(rig.plan_dir),
               "--authorization", str(rig.run_root / "nope.json"))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["code"] == "BLOCKED_AUTHORIZATION_MISSING"


# --- proof boundary ---------------------------------------------------------------


def test_claims_distinguish_handoff_from_generation(rig: Rig) -> None:
    result = rig.execute()
    claims = result["claims"]
    assert any("live generation" in c for c in claims["does_not_prove"])
    assert result["live"] is False and result["mocked"] is False
    # Simulated outcomes are labelled as qualifying integration only.
    recorded = rig.record("TIMEOUT")
    assert recorded["simulated"] is True
