import json
import subprocess
import sys
from pathlib import Path


BATTLE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BATTLE_DIR.parents[1]
PROBE = BATTLE_DIR / "scripts" / "agentic_eval_probe.py"


def test_am05_probe_creates_missing_summary_parent(tmp_path: Path) -> None:
    summary_path = tmp_path / "missing" / "nested" / "summary.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "am05-lifecycle-contracts",
            "--summary",
            str(summary_path),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert (summary_path.parent / "am05-lifecycle-contracts.stdout.txt").is_file()
    assert (summary_path.parent / "am05-lifecycle-contracts.stderr.txt").is_file()
