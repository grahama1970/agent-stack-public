#!/usr/bin/env python3
"""Variant comparison grid: tile 2-4 clip/image variants with labels.

Deterministic, headless FFmpeg lane using xstack + drawtext. Each cell keeps
its variant's native resolution (no scaling). Mismatched durations are padded
(tpad clone) or trimmed (-t) to the longest input, recorded per input in the
receipt. Emits accepted:false and never touches source media.
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

app = typer.Typer(add_completion=False, help="Tile 2-4 variants into one labelled grid.")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

EXIT_BAD_INPUT_COUNT = 6
EXIT_MISSING_INPUT = 3
EXIT_MIXED_TYPES = 7
EXIT_FFMPEG_FAILED = 5

LAYOUTS = {2: "row", 3: "row", 4: "grid2x2"}

#: xstack is broken on ffmpeg 6.1.1-ubuntu5 (fails to configure its output pad
#: even for equal-size inputs); hstack/vstack are not. Grids are built with
#: hstack/vstack over cells padded (never scaled) to a common cell canvas.
# ponytail: pad-to-canvas, per-cell scaling if mixed-aspect grids ever matter


def _fail(reason: str, code: int, **extra: Any) -> None:
    payload = {"error": "compare_variants_grid_failed", "reason": reason, **extra}
    print(json.dumps(payload), file=sys.stderr)
    raise typer.Exit(code)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _duration(path: Path) -> float | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _dims(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or "x" not in proc.stdout:
        _fail("dimension_probe_failed", EXIT_FFMPEG_FAILED, path=str(path))
    w, h = proc.stdout.strip().split("x")
    return int(w), int(h)


def _escape_drawtext(text: str) -> str:
    # ponytail: minimal escaping for :, ', \, % — enough for short labels
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _find_font() -> str | None:
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(p).is_file():
            return p
    return None


@app.command()
def main(
    inputs: list[Path] = typer.Option(..., "--input", help="Variant clip or image; repeatable (2-4)"),
    labels: list[str] = typer.Option(None, "--label", help="Cell label per input (same order)"),
    output: Path = typer.Option(..., "--output", help="Grid output path (.png or .mp4)"),
) -> None:
    if not 2 <= len(inputs) <= 4:
        _fail("invalid_input_count", EXIT_BAD_INPUT_COUNT, count=len(inputs), allowed="2-4")
    for p in inputs:
        if not p.is_file():
            _fail("input_missing", EXIT_MISSING_INPUT, path=str(p))
    if labels is None:
        labels = [p.stem for p in inputs]
    if len(labels) != len(inputs):
        _fail("label_count_mismatch", EXIT_BAD_INPUT_COUNT, labels=len(labels), inputs=len(inputs))

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        _fail("ffmpeg_not_found", EXIT_FFMPEG_FAILED)

    is_image = [p.suffix.lower() in IMAGE_EXTS for p in inputs]
    all_images = all(is_image)
    if not all_images and any(is_image):
        _fail("mixed_image_and_video_inputs", EXIT_MIXED_TYPES)

    font = _find_font()

    dims = [_dims(p) for p in inputs]
    cell_w = max(w for w, _ in dims)
    cell_h = max(h for _, h in dims)

    durations = [_duration(p) for p in inputs]
    if all_images:
        max_dur = 0.0
        actions = [{"index": i, "duration_seconds": None, "action": "image_passthrough"}
                   for i in range(len(inputs))]
    else:
        if any(d is None for d in durations):
            bad = [str(inputs[i]) for i, d in enumerate(durations) if d is None]
            _fail("duration_probe_failed", EXIT_FFMPEG_FAILED, paths=bad)
        max_dur = max(durations)  # type: ignore[type-var]
        actions = []
        for i, d in enumerate(durations):
            assert d is not None
            if abs(d - max_dur) < 0.02:
                action = "none"
            elif d < max_dur:
                action = "pad_tpad_clone"
            else:
                action = "trim_t"
            actions.append({"index": i, "duration_seconds": round(d, 3), "action": action})

    # Build filter_complex: pad each variant (never scale) to the common cell
    # canvas, burn the label, then hstack/vstack into the grid.
    n = len(inputs)
    chains = []
    for i, ((w, h), label) in enumerate(zip(dims, labels)):
        parts = []
        if not all_images:
            d = durations[i]
            assert d is not None
            if d < max_dur - 0.02:
                parts.append(f"tpad=stop_mode=clone:stop_duration={max_dur - d + 0.05:.3f}")
        if (w, h) != (cell_w, cell_h):
            parts.append(
                f"pad=w={cell_w}:h={cell_h}:x=(ow-iw)/2:y=(oh-ih)/2:color=black"
            )
        dt = (
            f"drawtext=text='{_escape_drawtext(label)}':x=8:y=8:fontsize=20:"
            "fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4"
        )
        if font:
            dt += f":fontfile='{font}'"
        parts.append(dt)
        chains.append(f"[{i}:v]" + ",".join(parts) + f"[v{i}]")
    if n == 4:
        chains.append("[v0][v1]hstack[r0];[v2][v3]hstack[r1];[r0][r1]vstack[out]")
    else:
        chains.append("".join(f"[v{i}]" for i in range(n)) + f"hstack=inputs={n}[out]")
    filter_complex = ";".join(chains)

    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for p in inputs:
        cmd += ["-i", str(p)]
    if not all_images:
        cmd += ["-t", f"{max_dur:.3f}", "-r", "24"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += ["-filter_complex", filter_complex, "-map", "[out]", str(output)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        _fail("ffmpeg_failed", EXIT_FFMPEG_FAILED, stderr=proc.stderr[-2000:])

    receipt = {
        "schema": "persona_dream.compare_variants_grid_receipt.v1",
        "status": "GRID_CANDIDATE_NOT_ACCEPTED",
        "accepted": False,
        "provider_calls": 0,
        "inputs": [
            {"path": str(p), "sha256": _sha256(p), "label": l}
            for p, l in zip(inputs, labels)
        ],
        "labels": labels,
        "duration_handling": {
            "max_duration_seconds": round(max_dur, 3) if not all_images else None,
            "per_input": actions,
        },
        "grid": {
            "layout": LAYOUTS[n],
            "cell_canvas": {"width": cell_w, "height": cell_h},
            "cell_padded": [
                {"index": i, "native": {"width": w, "height": h},
                 "padded": (w, h) != (cell_w, cell_h)}
                for i, (w, h) in enumerate(dims)
            ],
            "note": "native resolution preserved per cell; no scaling; black pad to common cell canvas; hstack/vstack used because xstack is broken on ffmpeg 6.1.1",
        },
        "output": {"path": str(output), "sha256": _sha256(output)},
        "ffmpeg_command": " ".join(cmd),
        "source_media_overwritten": False,
    }
    receipt_path = output.parent / (output.stem + ".receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    app()
