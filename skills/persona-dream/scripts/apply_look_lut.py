#!/usr/bin/env python3
"""LUT look-lock grade: apply a .cube LUT to accepted clips/frames.

Deterministic, headless FFmpeg lane. Produces GRADED_CANDIDATE_NOT_ACCEPTED
outputs plus a hash-bound receipt. Never overwrites source media, never calls
a provider, never writes memory.

LUTs come from the sibling `create-lut` skill (`./run.sh bake --ref ref.png
--source src.png --output match.cube` or `./run.sh generate --preset ...
--size 33`).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(add_completion=False, help="Apply a .cube LUT as a graded candidate.")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# Typed exit codes (fail-closed).
EXIT_INVALID_LUT = 2
EXIT_MISSING_INPUT = 3
EXIT_HASH_MISMATCH = 4
EXIT_FFMPEG_FAILED = 5


def _fail(reason: str, code: int, **extra: Any) -> None:
    payload = {"error": "look_lut_failed", "reason": reason, **extra}
    print(json.dumps(payload), file=sys.stderr)
    raise typer.Exit(code)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _validate_cube(lut: Path) -> int:
    if not lut.is_file():
        _fail("lut_missing", EXIT_INVALID_LUT, lut=str(lut))
    try:
        lines = lut.read_text().splitlines()
    except OSError as exc:
        _fail("lut_unreadable", EXIT_INVALID_LUT, error=str(exc))
    size = None
    numeric = 0
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("LUT_3D_SIZE"):
            parts = s.split()
            if len(parts) != 2 or not parts[1].isdigit():
                _fail("lut_invalid_header", EXIT_INVALID_LUT, line=s)
            size = int(parts[1])
            continue
        parts = s.split()
        if all(_is_float(p) for p in parts) and len(parts) == 3:
            numeric += 1
    if size is None or not (2 <= size <= 129):
        _fail("lut_invalid_missing_lut_3d_size", EXIT_INVALID_LUT)
    assert size is not None
    if numeric != size**3:
        _fail("lut_invalid_table_size", EXIT_INVALID_LUT, expected=size**3, found=numeric)
    return size


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


@app.command()
def main(
    inputs: list[Path] = typer.Option(..., "--input", help="Accepted clip or frame; repeatable"),
    lut: Path = typer.Option(..., "--lut", help=".cube LUT (from skills/create-lut)"),
    output_dir: Path = typer.Option(..., "--output-dir", help="Directory for graded CANDIDATE outputs"),
    expected_manifest: Path = typer.Option(
        None, "--expected-input-manifest",
        help="Optional JSON {path: sha256} to fail closed on input hash mismatch",
    ),
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        _fail("ffmpeg_not_found", EXIT_FFMPEG_FAILED)

    size = _validate_cube(lut)

    for p in inputs:
        if not p.is_file():
            _fail("input_missing", EXIT_MISSING_INPUT, path=str(p))

    input_records = [{"path": str(p), "sha256": _sha256(p)} for p in inputs]

    if expected_manifest is not None:
        expected = json.loads(expected_manifest.read_text())
        for rec in input_records:
            want = expected.get(rec["path"])
            if want is not None and want != rec["sha256"]:
                _fail(
                    "input_hash_mismatch", EXIT_HASH_MISMATCH,
                    path=rec["path"], expected=want, actual=rec["sha256"],
                )

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, str]] = []
    commands: list[str] = []

    for p in inputs:
        out = output_dir / f"{p.stem}.lut_candidate{p.suffix.lower()}"
        if out.resolve() == p.resolve():
            _fail("output_would_overwrite_source", EXIT_MISSING_INPUT, path=str(out))
        cmd = [
            ffmpeg, "-y", "-loglevel", "error", "-i", str(p),
            "-vf", f"lut3d=file='{lut.resolve()}'",
        ]
        if p.suffix.lower() not in IMAGE_EXTS:
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        cmd.append(str(out))
        commands.append(" ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            _fail("ffmpeg_failed", EXIT_FFMPEG_FAILED, stderr=proc.stderr[-2000:], input=str(p))
        if not out.is_file() or out.stat().st_size == 0:
            _fail("ffmpeg_produced_no_output", EXIT_FFMPEG_FAILED, input=str(p))
        outputs.append({"input": str(p), "path": str(out), "sha256": _sha256(out)})

    receipt = {
        "schema": "persona_dream.look_lut_receipt.v1",
        "status": "GRADED_CANDIDATE_NOT_ACCEPTED",
        "accepted": False,
        "provider_calls": 0,
        "lut": {"path": str(lut), "sha256": _sha256(lut), "lut_3d_size": size},
        "inputs": input_records,
        "outputs": outputs,
        "ffmpeg_commands": commands,
        "source_media_overwritten": False,
    }
    receipt_path = output_dir / "look_lut_receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    app()
