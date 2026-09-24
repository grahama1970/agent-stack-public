#!/usr/bin/env python3
"""Build the portable Battle qualification package for Complete Review AM-01."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from battle_skill.qualification_bundle import build_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(build_main())
