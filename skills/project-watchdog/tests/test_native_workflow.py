"""Native Pi workflow handoff and fail-closed result checks."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "watchdog_v2.py"
SPEC = importlib.util.spec_from_file_location("watchdog_v2", SOURCE)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)


def event_stream(fixer_status: str = "COMPLETE", reviewer_verdict: str = "PASS", *, exact_call: bool = True) -> str:
    path = "/tmp/watchdog-workflow/ticket.js"
    cwd = "/tmp/watchdog-target"
    args = {"workflowScriptPath": path, "cwd": cwd, "async": False}
    if not exact_call:
        args["workflowScriptPath"] = "/tmp/other.js"
    call = {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "subagent", "arguments": args}]}}
    fixer = {"ok": True, "runId": "fixer-run", "output": json.dumps({"status": fixer_status, "summary": "done", "checks": []})}
    review_1 = {"ok": True, "runId": "review-1-run", "output": "concrete findings: none"}
    reviewer = {"ok": True, "runId": "reviewer-run", "output": json.dumps({"verdict": reviewer_verdict, "findings": []})}
    result = {
        "type": "message_end",
        "message": {
            "role": "toolResult",
            "toolName": "subagent",
            "toolCallId": "call-1",
            "isError": False,
            "details": {
                "mode": "workflow",
                "runId": "workflow-run",
                "workflow": {"value": {"schema": "project_watchdog.v2.workflow", "results": {"fixer": fixer, "review_1": review_1, "reviewer": reviewer}, "fixer": fixer, "reviewer": reviewer}},
                "workflowChildren": {
                    "inventoryComplete": True,
                    "workflowState": "completed",
                    "children": [
                        {"childId": "fixer", "state": "completed"},
                        {"childId": "review_1", "state": "completed"},
                        {"childId": "reviewer", "state": "completed"},
                    ],
                },
            },
        },
    }
    return "\n".join(json.dumps(event) for event in (call, result))


class NativeWorkflowTests(unittest.TestCase):
    def test_maintainer_active_ticket_is_not_eligible(self) -> None:
        project = watchdog.Project(
            project_id="sample", repo="owner/repo", cwd="/tmp",
            ready_label="agent-work", active_label="agent-active", done_label="agent-done",
            target_prefixes=(), target_excludes=(), default_state="active", proof_command=("true",),
        )
        issue = {"title": "work", "body": "", "labels": [{"name": "agent-work"}, {"name": "maintainer-active"}]}
        self.assertFalse(watchdog.eligible(issue, project))

    def test_runner_command_is_not_trusted_as_proof_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.json"
            path.write_text(json.dumps({
                "projects": [{
                    "project_id": "sample",
                    "repo": "owner/repo",
                    "runner": {"cwd": directory, "command": "bash sanity.sh"},
                }],
            }), encoding="utf-8")
            project = watchdog.load_projects(path)[0]
            self.assertIsNone(project.proof_command)
            self.assertEqual(project.default_state, "paused")

    def test_blocked_upstream_is_not_eligible(self) -> None:
        project = watchdog.Project(
            project_id="sample", repo="owner/repo", cwd="/tmp",
            ready_label="agent-work", active_label="agent-active", done_label="agent-done",
            target_prefixes=(), target_excludes=(), default_state="active", proof_command=("true",),
        )
        issue = {"title": "work", "body": "blocked-by: owner/upstream#1", "labels": [{"name": "blocked:upstream"}]}
        self.assertFalse(watchdog.eligible(issue, project))
        self.assertEqual(watchdog.dependency_refs(issue["body"]), ("owner/upstream#1",))

    def test_project_without_proof_profile_is_fail_closed_inactive(self) -> None:
        project = watchdog.Project(
            project_id="sample", repo="owner/repo", cwd="/tmp",
            ready_label="agent-work", active_label="agent-active", done_label="agent-done",
            target_prefixes=(), target_excludes=(), default_state="active", proof_command=None,
        )
        self.assertEqual(watchdog.project_state({"global": {"state": "active"}}, project), "paused")

    def test_resolved_dependency_hold_only_clears_and_rescans_later(self) -> None:
        project = watchdog.Project(
            project_id="sample", repo="owner/repo", cwd="/tmp",
            ready_label="agent-work", active_label="agent-active", done_label="agent-done",
            target_prefixes=(), target_excludes=(), default_state="active", proof_command=("true",),
        )

        class FakeGh:
            def __init__(self) -> None:
                self.cleared: list[int] = []

            def list_open(self, _project):
                return [
                    {"number": 1, "title": "unblocked", "body": "blocked-by: owner/upstream#2", "labels": [{"name": "agent-work"}, {"name": "blocked:upstream"}]},
                    {"number": 3, "title": "other", "body": "", "labels": [{"name": "agent-work"}]},
                ]

            def dependency_status(self, ticket):
                return {"status": "resolved" if ticket.number == 1 else "none"}

            def clear_dependency_hold(self, ticket):
                self.cleared.append(ticket.number)

        gh = FakeGh()
        self.assertIsNone(watchdog.select_ticket(gh, project, {}, 0, apply=True))
        self.assertEqual(gh.cleared, [1])

    def test_out_of_scope_dependency_hold_cannot_block_target_scan(self) -> None:
        project = watchdog.Project(
            project_id="sample", repo="owner/repo", cwd="/tmp",
            ready_label="agent-work", active_label="agent-active", done_label="agent-done",
            target_prefixes=("wanted-target",), target_excludes=(), default_state="active", proof_command=("true",),
        )

        class FakeGh:
            def list_open(self, _project):
                return [
                    {"number": 1, "title": "unrelated", "body": "", "labels": [{"name": "agent-work"}, {"name": "blocked:upstream"}]},
                    {"number": 2, "title": "wanted-target", "body": "", "labels": [{"name": "agent-work"}]},
                ]

            def dependency_status(self, ticket):
                if ticket.number == 1:
                    raise AssertionError("out-of-scope dependency was inspected")
                return {"status": "none", "refs": []}

        selected = watchdog.select_ticket(FakeGh(), project, {}, 0)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.number, 2)

    def parse(self, stream: str) -> dict:
        return watchdog.parse_workflow_events(stream, Path("/tmp/watchdog-workflow/ticket.js"), "/tmp/watchdog-target")

    def test_global_pi_is_not_shadowed_by_workspace_binary(self) -> None:
        with patch.dict(os.environ, {"PATH": "/workspace/node_modules/.bin:/global/bin", "PROJECT_WATCHDOG_PI_BIN": ""}, clear=False):
            with patch.object(watchdog.shutil, "which", return_value="/global/bin/pi") as lookup:
                self.assertEqual(watchdog.pi_executable(), "/global/bin/pi")
        lookup.assert_called_once_with("pi", path="/global/bin")

    def test_invalid_pi_override_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"PROJECT_WATCHDOG_PI_BIN": str(Path(root) / "missing")}, clear=False):
                with self.assertRaisesRegex(OSError, "executable absolute path"):
                    watchdog.pi_executable()

    def test_nested_pi_is_pinned_to_selected_binary(self) -> None:
        with patch.dict(os.environ, {"PATH": "/workspace/node_modules/.bin:/global/bin", "PROJECT_WATCHDOG_PI_BIN": ""}, clear=False):
            with patch.object(watchdog.shutil, "which", return_value="/global/bin/pi"):
                child = watchdog.pi_environment()
        self.assertEqual(child["PI_SUBAGENT_PI_BINARY"], "/global/bin/pi")
        self.assertEqual(child["PATH"], "/global/bin")

    def test_cron_line_pins_runtime_path_and_pi_binary(self) -> None:
        line = watchdog.cron_line()
        self.assertIn("flock -n", line)
        self.assertIn("PATH=", line)
        self.assertIn(str(Path.home() / ".local" / "share" / "pi-node" / "node-v22.23.2-linux-x64" / "bin"), line)
        self.assertIn(str(Path.home() / ".local" / "bin"), line)
        self.assertIn(f"PROJECT_WATCHDOG_PI_BIN={Path.home() / '.pi' / 'agent' / 'bin' / 'pi'}", line)
        self.assertIn("tick --apply --project all --lock-held", line)

    def test_monitor_projects_daily_claim_is_atomic_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(watchdog, "MAINTENANCE_ROOT", root), \
                 patch.object(watchdog, "MONITOR_PROJECTS_STATE", root / "state.json"), \
                 patch.object(watchdog, "MONITOR_PROJECTS_LOCK", root / "lock"):
                before = watchdog._claim_monitor_projects_day(datetime(2026, 9, 20, 2, 29))
                first = watchdog._claim_monitor_projects_day(datetime(2026, 9, 20, 2, 30))
                second = watchdog._claim_monitor_projects_day(datetime(2026, 9, 20, 4, 0))
        self.assertFalse(before["due"])
        self.assertTrue(first["due"])
        self.assertFalse(second["due"])
        self.assertEqual(second["reason"], "already_claimed")

    def test_complete_native_workflow_passes(self) -> None:
        result = self.parse(event_stream())
        self.assertTrue(result["ok"])
        self.assertTrue(watchdog.reviewer_passed(result))

    def test_blocked_fixer_cannot_pass(self) -> None:
        result = self.parse(event_stream(fixer_status="BLOCKED"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_role"], "fixer")
        self.assertFalse(watchdog.reviewer_passed(result))

    def test_failed_reviewer_cannot_pass(self) -> None:
        result = self.parse(event_stream(reviewer_verdict="FAIL"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_role"], "reviewer")

    def test_wrong_tool_call_cannot_pass(self) -> None:
        result = self.parse(event_stream(exact_call=False))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_role"], "workflow")

    def test_parent_prose_cannot_replace_tool_result(self) -> None:
        text = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": '{"verdict":"PASS"}'}]}})
        self.assertFalse(self.parse(text)["ok"])

    def test_failed_extra_node_cannot_be_ignored(self) -> None:
        events = [json.loads(line) for line in event_stream().splitlines()]
        result = events[1]["message"]["details"]
        value = result["workflow"]["value"]
        value["results"] = {
            "scout": {"ok": False, "output": "blocked"},
            "fixer": value["fixer"],
            "review_1": value["results"]["review_1"],
            "reviewer": value["reviewer"],
        }
        result["workflowChildren"]["children"].append({"childId": "scout", "state": "failed"})
        parsed = self.parse("\n".join(json.dumps(event) for event in events))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["failed_role"], "workflow")

    def test_missing_registered_node_cannot_pass(self) -> None:
        events = [json.loads(line) for line in event_stream().splitlines()]
        result = events[1]["message"]["details"]
        value = result["workflow"]["value"]
        del value["results"]["review_1"]
        result["workflowChildren"]["children"] = [
            child for child in result["workflowChildren"]["children"] if child["childId"] != "review_1"
        ]
        parsed = self.parse("\n".join(json.dumps(event) for event in events))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["failed_role"], "workflow")

    def test_workflow_does_not_receive_trusted_proof_command(self) -> None:
        ticket = watchdog.Ticket(
            project=watchdog.Project("sample", "owner/repo", "/tmp", "agent-work", "agent-active", "agent-done", (), (), "active", ("bash", "-lc", "echo SECRET")),
            number=1, title="title", url="", labels=(), body="Target: x",
        )
        script = watchdog.workflow_script(ticket)
        self.assertNotIn("echo SECRET", script)


if __name__ == "__main__":
    unittest.main()
