import json
import tempfile
import unittest
from pathlib import Path

from jev_semantic import jev_required_failed, run_jev_semantic_review


class JevSemanticReviewTests(unittest.TestCase):
    def _surf(self, body: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "run.sh"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def test_disabled_never_calls_surf(self):
        receipt = run_jev_semantic_review(mode="disabled", goal="find failures", surf=Path("/missing"))
        self.assertEqual("disabled", receipt["status"])
        self.assertEqual([], receipt["operations"])
        self.assertFalse(receipt["deterministic_results_changed"])

    def test_optional_unavailable_does_not_fail_required_gate(self):
        surf = self._surf("echo unavailable >&2; exit 1")
        receipt = run_jev_semantic_review(mode="optional", goal="find failures", surf=surf)
        self.assertEqual("unavailable", receipt["status"])
        self.assertFalse(jev_required_failed(receipt))

    def test_required_unavailable_fails_closed(self):
        surf = self._surf("exit 1")
        receipt = run_jev_semantic_review(mode="required", goal="find failures", surf=surf)
        self.assertTrue(jev_required_failed(receipt))

    def test_filter_and_verify_use_shared_surf_commands(self):
        surf = self._surf("printf '%s' '{\"status\":\"accepted\"}'")
        receipt = run_jev_semantic_review(
            mode="required",
            goal="find failed controls",
            outcome="settings were saved",
            tab_id="42",
            surf=surf,
        )
        self.assertEqual("accepted", receipt["status"])
        self.assertEqual(["semantic.filter", "semantic.verify"], [item["command"] for item in receipt["operations"]])
        self.assertFalse(jev_required_failed(receipt))
        json.dumps(receipt)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            run_jev_semantic_review(mode="sometimes")


if __name__ == "__main__":
    unittest.main()
