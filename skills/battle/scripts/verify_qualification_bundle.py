#!/usr/bin/env python3
"""Verify a portable Battle qualification package without trusting producer claims."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from battle_skill.qualification_bundle import verify_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(verify_main())
