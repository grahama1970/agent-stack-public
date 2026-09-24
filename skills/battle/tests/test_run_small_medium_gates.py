from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_small_medium_production_battle.py"
sys.path.insert(0, str(ROOT.parent))
spec = importlib.util.spec_from_file_location("run_small_medium_production_battle", SCRIPT)
assert spec and spec.loader
battle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = battle
spec.loader.exec_module(battle)
backfill_spec = importlib.util.spec_from_file_location("backfill_run_receipt_gates", ROOT / "scripts" / "backfill_run_receipt_gates.py")
assert backfill_spec and backfill_spec.loader
backfill = importlib.util.module_from_spec(backfill_spec)
sys.modules[backfill_spec.name] = backfill
backfill_spec.loader.exec_module(backfill)
verify_spec = importlib.util.spec_from_file_location("verify_gate_integrity_audit", ROOT / "scripts" / "verify_gate_integrity_audit.py")
assert verify_spec and verify_spec.loader
verify_gate = importlib.util.module_from_spec(verify_spec)
sys.modules[verify_spec.name] = verify_gate
verify_spec.loader.exec_module(verify_gate)


def _attempt(label: str, exit_code: int, *, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    classified = battle.classify_attempt(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        error=None,
        label=label,
    )
    return {
        "schema": "battle.production_scale.docker_attempt.v1",
        "status": classified["process_status"],
        "process_status": classified["process_status"],
        "label": label,
        "exit_code": exit_code,
        "semantic_exit_code": classified["semantic_exit_code"],
        "infra_error": classified["infra_error"],
        "timed_out": False,
        "gate": classified["gate"],
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": classified["stdout_sha256"],
        "stderr_sha256": classified["stderr_sha256"],
    }


def _signed(code: int) -> str:
    result = "confirmed" if code == 10 else "blocked" if code == 0 else "regression"
    return json.dumps({"judge_exit_signal": code, "probe_result": result}) + "\n"


def _round_spec(round_number: int = 1) -> Any:
    return battle.RoundSpec(
        round_number,
        "fixture vuln",
        "red goal",
        "blue plan",
        "balanced",
        "x=1\n",
        "x=2\n",
        "print('judge')\n",
    )


def _run_round(tmp_path: Path, monkeypatch: Any, attempts: list[dict[str, Any]], round_number: int = 1) -> dict[str, Any]:
    seq = iter(attempts)
    monkeypatch.setattr(battle, "run_docker", lambda work, label: next(seq))
    return battle.judge_round(tmp_path, _round_spec(round_number), tmp_path / "events.jsonl")


def test_compile_error_blocks_round_scorekeeper_and_run_winner(tmp_path: Path, monkeypatch: Any) -> None:
    round_receipt = _run_round(
        tmp_path,
        monkeypatch,
        [
            _attempt("original", 1, stderr="IndentationError: bad indent"),
            _attempt("patched", 0, stdout=_signed(0)),
        ],
    )
    assert round_receipt["status"] != "PASS"
    assert round_receipt["verdict"] == "ERROR_COMPILE"

    scorekeeper = battle.write_scorekeeper(tmp_path, [round_receipt], tmp_path / "events.jsonl")
    assert scorekeeper["status"] != "PASS"
    assert scorekeeper["winner"] is None
    run_receipt = {"schema": "battle.production_scale.run_receipt.v2", "status": scorekeeper["status"], "scoreboard": scorekeeper, "winner": None}
    assert run_receipt["status"] != "PASS"
    assert run_receipt["winner"] is None


def test_patched_traceback_exit10_is_compile_error_not_success(tmp_path: Path, monkeypatch: Any) -> None:
    round_receipt = _run_round(
        tmp_path,
        monkeypatch,
        [
            _attempt("original", 10, stdout=_signed(10)),
            _attempt("patched", 10, stdout=_signed(10), stderr="Traceback (most recent call last): boom"),
        ],
    )
    assert round_receipt["status"] == "BLOCKED"
    assert round_receipt["verdict"] == "ERROR_COMPILE"
    assert round_receipt["blue_score"] == 0


def test_exit10_without_judge_signed_stdout_is_insufficient_evidence(tmp_path: Path, monkeypatch: Any) -> None:
    round_receipt = _run_round(
        tmp_path,
        monkeypatch,
        [
            _attempt("original", 10, stdout=""),
            _attempt("patched", 0, stdout=_signed(0)),
        ],
    )
    assert round_receipt["status"] == "INSUFFICIENT_EVIDENCE"
    assert round_receipt["verdict"] == "HARNESS_ERROR"
    scorekeeper = battle.write_scorekeeper(tmp_path, [round_receipt], tmp_path / "events.jsonl")
    assert scorekeeper["status"] == "GATES_INCOMPLETE"
    assert scorekeeper["incomplete_gates"]


def test_scorekeeper_detects_tampered_round_receipt_hash(tmp_path: Path, monkeypatch: Any) -> None:
    round_receipt = _run_round(
        tmp_path,
        monkeypatch,
        [
            _attempt("original", 10, stdout=_signed(10)),
            _attempt("patched", 0, stdout=_signed(0)),
        ],
    )
    assert battle.write_scorekeeper(tmp_path, [round_receipt], tmp_path / "events.jsonl")["status"] == "PASS"
    path = Path(round_receipt["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["red_score"] = 99
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    scorekeeper = battle.write_scorekeeper(tmp_path, [round_receipt], tmp_path / "events.jsonl")
    assert scorekeeper["status"] == "CHAIN_MISMATCH"
    assert scorekeeper["winner"] is None


def test_report_for_blocked_round_makes_no_red_blue_win_claim(tmp_path: Path) -> None:
    blocked_round = {
        "round_number": 1,
        "vulnerability": "fixture vuln",
        "verdict": "ERROR_COMPILE",
        "status": "BLOCKED",
        "expected_balance": "balanced",
        "red_scanned": ["x"],
        "red_attempts": ["x"],
        "blue_scanned": ["x"],
        "blue_attempts": ["x"],
        "adaptive_lineage_created": ["x"],
        "incomplete_gates": [{"gate": "ERROR_COMPILE"}],
        "docker_attempts": [_attempt("original", 1, stderr="SyntaxError"), _attempt("patched", 0, stdout=_signed(0))],
    }
    receipt = {
        "status": "GATES_INCOMPLETE",
        "winner": None,
        "arena": battle.ARENA_CONTRACT,
        "scoreboard": {"red_total": 0, "blue_total": 0, "red_round_wins": 0, "blue_round_wins": 0},
        "rounds": [blocked_round],
        "incomplete_gates": [{"gate": "ERROR_COMPILE"}],
    }
    battle.write_report(tmp_path, receipt)
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "incomplete_gates" in report
    assert "Red takes the round" not in report
    assert "Blue takes the round" not in report


def test_happy_round_can_pass_and_emit_winner(tmp_path: Path, monkeypatch: Any) -> None:
    round_receipt = _run_round(
        tmp_path,
        monkeypatch,
        [
            _attempt("original", 10, stdout=_signed(10)),
            _attempt("patched", 0, stdout=_signed(0)),
        ],
    )
    assert round_receipt["status"] == "PASS"
    assert round_receipt["verdict"] == "BLUE_SUCCESS"
    scorekeeper = battle.write_scorekeeper(tmp_path, [round_receipt], tmp_path / "events.jsonl")
    assert scorekeeper["status"] == "PASS"
    assert scorekeeper["winner"] == "Blue"


def test_backfill_revokes_legacy_parent_pass_with_failed_child(tmp_path: Path) -> None:
    run_path = tmp_path / "run-receipt.json"
    legacy = {
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "winner": "Red",
        "rounds": [
            {
                "round_number": 1,
                "docker_attempts": [
                    {"label": "original", "exit_code": 1, "stderr": "IndentationError: bad", "stdout": ""},
                    {"label": "patched", "exit_code": 0, "stderr": "", "stdout": ""},
                ],
            }
        ],
    }
    run_path.write_text(json.dumps(legacy), encoding="utf-8")
    audit = backfill.audit_run_receipt(run_path, preserve_original=True)
    assert audit["recomputed_status"] == "FALSE_GREEN_REVOKED"
    assert audit["winner"] == "NOT_SCORED"
    assert audit["disposition"] == "revoked_false_green"
    assert (tmp_path / "run-receipt.v1.json").is_file()


def test_backfill_keeps_valid_nonzero_judge_outcome_eligible(tmp_path: Path) -> None:
    run_path = tmp_path / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "winner": "Red",
        "rounds": [{"round_number": 1, "docker_attempts": [_attempt("original", 10, stdout=_signed(10))]}],
    }), encoding="utf-8")

    audit = backfill.audit_run_receipt(run_path)

    assert audit["recomputed_status"] == "PASS"
    assert audit["disposition"] == "valid_under_original_contract"
    assert audit["winner"] == "Red"


def test_backfill_does_not_revoke_on_docker_fail_label_alone(tmp_path: Path) -> None:
    run_path = tmp_path / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "winner": "Blue",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "process_status": "FAIL", "exit_code": 1, "stdout": "", "stderr": ""}]}],
    }), encoding="utf-8")

    audit = backfill.audit_run_receipt(run_path)

    assert audit["disposition"] == "unverified"
    assert audit["recomputed_status"] == "PASS"
    assert audit["winner"] == "Blue"


def test_backfill_records_malformed_and_missing_child_receipts(tmp_path: Path) -> None:
    (tmp_path / "bad" / "round-receipt.json").parent.mkdir()
    (tmp_path / "bad" / "round-receipt.json").write_text("{not json", encoding="utf-8")
    run_path = tmp_path / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v2",
        "status": "PASS",
        "scoreboard": {"round_receipts": [{"path": "missing-round.json", "sha256": "sha256:abc"}]},
    }), encoding="utf-8")

    index = backfill.build_index(tmp_path)
    by_path = {Path(entry["path"]).name: entry for entry in index["entries"]}

    assert by_path["run-receipt.json"]["disposition"] == "missing_dependency"
    assert by_path["round-receipt.json"]["disposition"] == "unparseable"
    assert index["missing_dependency_count"] == 1
    assert index["unparseable_count"] == 1


def test_backfill_includes_original_beyond_brief_malformed_scope(tmp_path: Path) -> None:
    extra = tmp_path / "beyond-brief-campaign-20260912.json"
    extra.write_text('{"schema":"legacy"}\ntrailing-bytes\n', encoding="utf-8")

    index = backfill.build_index(tmp_path / "empty-root", extra_paths=[extra])
    entry = index["entries"][0]

    assert index["declared_archive_scope"]["extra_paths"] == [str(extra)]
    assert entry["original_sha256"] == battle.sha256_file(extra)
    assert entry["disposition"] == "unparseable"
    assert entry["eligibility"] == "withheld"


def test_backfill_traces_downstream_revocation_and_is_repeat_safe(tmp_path: Path) -> None:
    run_path = tmp_path / "case" / "run-receipt.json"
    run_path.parent.mkdir()
    legacy = {
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "winner": "Red",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "exit_code": 1, "stderr": "Traceback (most recent call last): boom", "stdout": ""}]}],
    }
    run_path.write_text(json.dumps(legacy), encoding="utf-8")
    (run_path.parent / "REPORT.md").write_text("# stale winner\n", encoding="utf-8")

    first = backfill.build_index(tmp_path, preserve_originals=True)
    second = backfill.build_index(tmp_path, preserve_originals=True)

    assert first["revoked_count"] == second["revoked_count"] == 2
    assert first["downstream_revocation_count"] == second["downstream_revocation_count"] == 1
    assert (run_path.parent / "run-receipt.v1.json").is_file()
    assert len(list(run_path.parent.glob("run-receipt.v1*.json"))) == 1


def test_backfill_atomic_write_leaves_previous_index_on_replace_failure(monkeypatch: Any, tmp_path: Path) -> None:
    out = tmp_path / "index.json"
    out.write_text('{"old": true}\n', encoding="utf-8")
    original_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> None:
        if self.name == "index.json.tmp":
            raise RuntimeError("interrupted")
        original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    try:
        backfill.write_index_atomic(out, {"schema": "x"})
    except RuntimeError:
        pass

    assert json.loads(out.read_text(encoding="utf-8")) == {"old": True}


def test_verify_backfill_index_audits_counts_and_revocations(tmp_path: Path) -> None:
    run_path = tmp_path / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "exit_code": 1, "stderr": "IndentationError: bad"}]}],
    }), encoding="utf-8")
    index = backfill.build_index(tmp_path)
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    assert audit["status"] == "PASS"
    assert audit["false_green_parent_count"] == 1
    assert audit["declared_archive_scope_count"] == 1


def test_verify_backfill_index_rejects_forged_entry_hash(tmp_path: Path) -> None:
    run_path = tmp_path / "run-receipt.json"
    run_path.write_text(json.dumps({"schema": "battle.production_scale.run_receipt.v1", "status": "PASS"}), encoding="utf-8")
    index = backfill.build_index(tmp_path)
    index["entries"][0]["original_sha256"] = "0" * 64
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    assert audit["status"] == "FAIL"
    assert any(item["gate"] == "BACKFILL_HASH_MISMATCH" for item in audit["incomplete_gates"])


def test_backfill_marks_cross_campaign_child_receipt_as_missing_dependency(tmp_path: Path) -> None:
    campaign_a = tmp_path / "campaign-A"
    campaign_b = tmp_path / "campaign-B"
    campaign_a.mkdir()
    campaign_b.mkdir()
    round_path = campaign_b / "round-receipt.json"
    round_path.write_text(json.dumps({"schema": "battle.production_scale.round_receipt.v1", "status": "PASS"}), encoding="utf-8")
    run_path = campaign_a / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "scoreboard": {"round_receipts": [{"path": str(round_path), "sha256": battle.sha256_file(round_path)}]},
    }), encoding="utf-8")

    index = backfill.build_index(tmp_path)
    run_entry = next(entry for entry in index["entries"] if entry["kind"] == "run_receipt")

    assert run_entry["disposition"] == "missing_dependency"
    assert run_entry["dependency_problems"][0]["reason"] == "referenced_child_receipt_outside_parent_scope"


def _refresh_backfill_summaries(index: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for entry in index["entries"]:
        counts[entry["disposition"]] = counts.get(entry["disposition"], 0) + 1
    index["disposition_counts"] = dict(sorted(counts.items()))
    index["revoked_count"] = counts.get("revoked_false_green", 0)
    index["unparseable_count"] = counts.get("unparseable", 0)
    index["missing_dependency_count"] = counts.get("missing_dependency", 0)
    index["unverified_count"] = counts.get("unverified", 0)
    index["declared_archive_scope"]["item_count"] = len(index["entries"])
    index["downstream_revocation_count"] = len(index.get("downstream_revocations", []))


def test_verify_backfill_index_recomputes_cross_campaign_disposition(tmp_path: Path) -> None:
    campaign_a = tmp_path / "campaign-A"
    campaign_b = tmp_path / "campaign-B"
    campaign_a.mkdir()
    campaign_b.mkdir()
    round_path = campaign_b / "round-receipt.json"
    round_path.write_text(json.dumps({"schema": "battle.production_scale.round_receipt.v1", "status": "PASS"}), encoding="utf-8")
    run_path = campaign_a / "run-receipt.json"
    run_path.write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "scoreboard": {"round_receipts": [{"path": str(round_path), "sha256": battle.sha256_file(round_path)}]},
    }), encoding="utf-8")
    index = backfill.build_index(tmp_path)
    for entry in index["entries"]:
        if entry["kind"] == "run_receipt":
            entry["disposition"] = "valid_under_original_contract"
            entry["eligibility"] = "eligible"
            entry["dependency_problems"] = []
    counts: dict[str, int] = {}
    for entry in index["entries"]:
        counts[entry["disposition"]] = counts.get(entry["disposition"], 0) + 1
    index["disposition_counts"] = dict(sorted(counts.items()))
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    assert audit["status"] == "FAIL"
    assert any(item["gate"] == "BACKFILL_RECOMPUTED_DISPOSITION_MISMATCH" for item in audit["incomplete_gates"])


def test_verify_backfill_index_rejects_forged_restored_downstream_report(tmp_path: Path) -> None:
    case = tmp_path / "run-20260905T204015Z"
    case.mkdir()
    (case / "run-receipt.json").write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "winner": "Red",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "exit_code": 1, "stderr": "IndentationError: bad"}]}],
    }), encoding="utf-8")
    (case / "REPORT.md").write_text("# stale false-green report\n", encoding="utf-8")
    index = backfill.build_index(tmp_path)
    report_entry = next(entry for entry in index["entries"] if entry["path"].endswith("REPORT.md"))
    report_entry["disposition"] = "valid_under_original_contract"
    report_entry["eligibility"] = "eligible"
    report_entry.pop("source_revocation", None)
    index["downstream_revocations"] = [item for item in index["downstream_revocations"] if not item["path"].endswith("REPORT.md")]
    _refresh_backfill_summaries(index)
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    gates_seen = {item["gate"] for item in audit["incomplete_gates"]}
    assert audit["status"] == "FAIL"
    assert "BACKFILL_RECOMPUTED_ENTRY_MISMATCH" in gates_seen
    assert "BACKFILL_RECOMPUTED_DOWNSTREAM_REVOCATIONS_MISMATCH" in gates_seen


def test_verify_backfill_index_rejects_omitted_and_restored_downstream_dependents(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    (case / "run-receipt.json").write_text(json.dumps({
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "exit_code": 1, "stderr": "Traceback (most recent call last): boom"}]}],
    }), encoding="utf-8")
    (case / "commentary.jsonl").write_text('{"stale": true}\n', encoding="utf-8")
    (case / "learning-summary.json").write_text('{"stale": true}\n', encoding="utf-8")
    index = backfill.build_index(tmp_path)
    omitted = next(entry for entry in index["entries"] if entry["path"].endswith("commentary.jsonl"))
    restored = next(entry for entry in index["entries"] if entry["path"].endswith("learning-summary.json"))
    restored["disposition"] = "valid_under_original_contract"
    restored["eligibility"] = "eligible"
    restored.pop("source_revocation", None)
    index["entries"].remove(omitted)
    index["downstream_revocations"] = [
        item for item in index["downstream_revocations"]
        if not (item["path"].endswith("commentary.jsonl") or item["path"].endswith("learning-summary.json"))
    ]
    _refresh_backfill_summaries(index)
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    gates_seen = {item["gate"] for item in audit["incomplete_gates"]}
    assert audit["status"] == "FAIL"
    assert "BACKFILL_RECOMPUTED_SCOPE_MISMATCH" in gates_seen
    assert "BACKFILL_RECOMPUTED_ENTRY_MISMATCH" in gates_seen
    assert "BACKFILL_RECOMPUTED_DOWNSTREAM_REVOCATIONS_MISMATCH" in gates_seen


def test_verify_false_green_parent_count_excludes_rounds_and_downstream(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    legacy = {
        "schema": "battle.production_scale.run_receipt.v1",
        "status": "PASS",
        "rounds": [{"round_number": 1, "docker_attempts": [{"label": "original", "exit_code": 1, "stderr": "IndentationError: bad"}]}],
    }
    (case / "run-receipt.json").write_text(json.dumps(legacy), encoding="utf-8")
    (case / "round-receipt.json").write_text(json.dumps(legacy), encoding="utf-8")
    (case / "REPORT.md").write_text("# stale winner\n", encoding="utf-8")
    index = backfill.build_index(tmp_path)
    index_path = tmp_path / "backfill-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    audit = verify_gate.verify_backfill_index(index_path)

    assert index["disposition_counts"]["revoked_false_green"] == 3
    assert audit["false_green_parent_count"] == 1
