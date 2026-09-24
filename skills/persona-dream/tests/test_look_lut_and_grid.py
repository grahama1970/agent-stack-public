"""Focused tests for apply_look_lut.py and compare_variants_grid.py.

Builds real tiny media with ffmpeg lavfi testsrc and a real identity .cube.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def media(tmp_path_factory: Path) -> Path:
    d = tmp_path_factory.mktemp("media")
    # real tiny clip
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x96:rate=12:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "clip.mp4"),
    ])
    assert r.returncode == 0, r.stderr
    # short variant (different duration)
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=160x96:rate=12:duration=0.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "clip_short.mp4"),
    ])
    assert r.returncode == 0, r.stderr
    # frame
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x96", "-frames:v", "1",
        str(d / "frame.png"),
    ])
    assert r.returncode == 0, r.stderr
    # real small .cube: size-2 identity LUT
    lines = ["TITLE tiny_identity", "LUT_3D_SIZE 2"]
    for r_ in range(2):
        for g in range(2):
            for b in range(2):
                lines.append(f"{r_/1:.6f} {g/1:.6f} {b/1:.6f}")
    (d / "tiny.cube").write_text("\n".join(lines) + "\n")
    return d


def test_apply_lut_grades_clip_and_frame(media: Path, tmp_path: Path) -> None:
    src_clip = media / "clip.mp4"
    src_frame = media / "frame.png"
    pre = {p: sha256(p) for p in (src_clip, src_frame, media / "tiny.cube")}
    out_dir = tmp_path / "graded"
    r = _run([
        sys.executable, str(SCRIPTS / "apply_look_lut.py"),
        "--input", str(src_clip), "--input", str(src_frame),
        "--lut", str(media / "tiny.cube"),
        "--output-dir", str(out_dir),
    ])
    assert r.returncode == 0, r.stderr
    receipt = json.loads((out_dir / "look_lut_receipt.json").read_text())
    assert receipt["status"] == "GRADED_CANDIDATE_NOT_ACCEPTED"
    assert receipt["accepted"] is False
    assert receipt["provider_calls"] == 0
    assert len(receipt["outputs"]) == 2
    for rec in receipt["outputs"]:
        assert Path(rec["path"]).is_file()
        assert rec["sha256"] == sha256(Path(rec["path"]))
    for rec in receipt["inputs"]:
        assert rec["sha256"] == pre[Path(rec["path"])]
    assert receipt["lut"]["sha256"] == pre[media / "tiny.cube"]
    assert receipt["lut"]["lut_3d_size"] == 2
    # source bytes unchanged
    for p, h in pre.items():
        assert sha256(p) == h


def test_apply_lut_rejects_invalid_lut(media: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.cube"
    bad.write_text("not a lut\n")
    r = _run([
        sys.executable, str(SCRIPTS / "apply_look_lut.py"),
        "--input", str(media / "frame.png"), "--lut", str(bad),
        "--output-dir", str(tmp_path / "o"),
    ])
    assert r.returncode == 2
    assert "lut_invalid" in r.stderr

    r = _run([
        sys.executable, str(SCRIPTS / "apply_look_lut.py"),
        "--input", str(media / "frame.png"), "--lut", str(tmp_path / "missing.cube"),
        "--output-dir", str(tmp_path / "o"),
    ])
    assert r.returncode == 2
    assert "lut_missing" in r.stderr


def test_apply_lut_hash_mismatch_fails_closed(media: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({str(media / "frame.png"): "sha256:" + "0" * 64}))
    r = _run([
        sys.executable, str(SCRIPTS / "apply_look_lut.py"),
        "--input", str(media / "frame.png"), "--lut", str(media / "tiny.cube"),
        "--output-dir", str(tmp_path / "o"), "--expected-input-manifest", str(manifest),
    ])
    assert r.returncode == 4
    assert "input_hash_mismatch" in r.stderr


def test_grid_tiles_mismatched_durations(media: Path, tmp_path: Path) -> None:
    a, b = media / "clip.mp4", media / "clip_short.mp4"
    pre = {p: sha256(p) for p in (a, b)}
    out = tmp_path / "grid.mp4"
    r = _run([
        sys.executable, str(SCRIPTS / "compare_variants_grid.py"),
        "--input", str(a), "--input", str(b),
        "--label", "A", "--label", "B",
        "--output", str(out),
    ])
    assert r.returncode == 0, r.stderr
    assert out.is_file() and out.stat().st_size > 0
    receipt = json.loads((out.parent / "grid.receipt.json").read_text())
    assert receipt["accepted"] is False
    assert receipt["provider_calls"] == 0
    assert receipt["output"]["sha256"] == sha256(out)
    assert receipt["labels"] == ["A", "B"]
    per = receipt["duration_handling"]["per_input"]
    assert per[0]["action"] in ("none", "trim_t")
    assert per[1]["action"] == "pad_tpad_clone"
    for rec in receipt["inputs"]:
        assert rec["sha256"] == pre[Path(rec["path"])]
    for p, h in pre.items():
        assert sha256(p) == h


def test_grid_four_images(media: Path, tmp_path: Path) -> None:
    frame = media / "frame.png"
    out = tmp_path / "grid4.png"
    r = _run([
        sys.executable, str(SCRIPTS / "compare_variants_grid.py"),
        "--input", str(frame), "--input", str(frame),
        "--input", str(frame), "--input", str(frame),
        "--output", str(out),
    ])
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    receipt = json.loads((out.parent / "grid4.receipt.json").read_text())
    assert len(receipt["inputs"]) == 4
    assert all(rec["sha256"] == sha256(frame) for rec in receipt["inputs"])


def test_grid_rejects_bad_counts_and_missing(media: Path, tmp_path: Path) -> None:
    frame = media / "frame.png"
    # 1 input
    r = _run([
        sys.executable, str(SCRIPTS / "compare_variants_grid.py"),
        "--input", str(frame), "--output", str(tmp_path / "g.png"),
    ])
    assert r.returncode == 6
    assert "invalid_input_count" in r.stderr
    # 5 inputs
    args = [sys.executable, str(SCRIPTS / "compare_variants_grid.py")]
    for _ in range(5):
        args += ["--input", str(frame)]
    args += ["--output", str(tmp_path / "g.png")]
    r = _run(args)
    assert r.returncode == 6
    # missing file
    r = _run([
        sys.executable, str(SCRIPTS / "compare_variants_grid.py"),
        "--input", str(frame), "--input", str(tmp_path / "nope.png"),
        "--output", str(tmp_path / "g.png"),
    ])
    assert r.returncode == 3
    assert "input_missing" in r.stderr
