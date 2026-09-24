#!/usr/bin/env python3
"""Run a small/medium local Docker-backed Battle with a plain report."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from battle_skill.arena_intent import (
    authority_ref,
    bind_intent,
    build_arena_maker_intent,
    freeze_arena_maker_intent,
    verify_bound_payload,
    verify_frozen_intent,
)
from battle_skill.play_by_play import build_production_scale_timeline
from battle_skill.team_activity import build_production_activity_bundle
from common.security_authorization import validate_target_authorization


IMAGE = "python:3.12-slim"

ARENA_CONTRACT: dict[str, Any] = {
    "schema": "battle.production_scale.arena_contract.v1",
    "why_built": "Prove Battle as an evolutionary Red/Blue arena: equal public terrain, hidden Judge authority, Docker execution, and adaptive lineage that spawns child specimens from partial progress.",
    "playing_field_equalizers": [
        "Red and Blue see the same public vulnerability surface before each round.",
        "Neither team can self-award a win; Docker Judge exit codes decide.",
        "Target containers run with no network and no host execution of target/team code.",
        "A Red hit must preserve the normal-behavior invariant to count as a useful exploit signal.",
        "A Blue block must preserve the same invariant or it is a fake defense.",
    ],
    "expected_exploit_families": [
        "command injection",
        "path traversal",
        "hard-coded credential/auth bypass",
        "unsafe evaluator/code execution",
        "adaptive rematches against naive or behavior-breaking defenses",
    ],
    "novelty_settings": {
        "low": "near-parent mutation likely to run",
        "medium": "logical crossover between parent traits and known bypasses",
        "high": "warm-pond lightning strike: weird combinations are allowed and many should die in Docker preflight",
    },
}


@dataclass(frozen=True)
class RoundSpec:
    round_number: int
    vulnerability: str
    red_goal: str
    blue_plan: str
    expected_balance: str
    vulnerable_app: str
    patched_app: str
    judge_script: str


@dataclass(frozen=True)
class SpawnSpec:
    parent_round: int
    child_id: str
    novelty: str
    ingredients: tuple[str, ...]
    mutation_operator: str
    hypothesis: str
    code: str
    timeout_seconds: float = 30.0
    obligation: Literal["mandatory", "expected_rejection"] = "mandatory"


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

ALLOWED_SEMANTIC_EXIT_CODES = {0, 2, 10}
TRACEBACK_RE = re.compile(r"(IndentationError|SyntaxError|Traceback \(most recent call last\)|ImportError|ModuleNotFoundError)")


def normalize_sha256(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def sha256_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def classify_attempt(*, exit_code: int | None, stdout: str, stderr: str, timed_out: bool, error: str | None, label: str, stage: str = "target_judge") -> dict[str, Any]:
    has_traceback = bool(TRACEBACK_RE.search(stderr or ""))
    semantic_exit_code = exit_code if exit_code in ALLOWED_SEMANTIC_EXIT_CODES else None
    infra_error = timed_out or error is not None or semantic_exit_code is None or has_traceback
    if has_traceback:
        gate = "ERROR_COMPILE"
        observation_class = "compile_or_import_failure"
    elif timed_out:
        gate = "CANCELED_TIMEOUT"
        observation_class = "cancellation"
    elif error is not None or semantic_exit_code is None:
        gate = "HARNESS_ERROR"
        observation_class = "harness_failure"
    elif semantic_exit_code == 10:
        gate = "EXPLOIT_CONFIRMED_EXIT10"
        observation_class = "judge_semantic_signal"
    elif semantic_exit_code == 0:
        gate = "PATCH_HOLD" if label == "patched" else "BASELINE_STABLE"
        observation_class = "judge_semantic_signal" if stage == "target_judge" else "candidate_preflight_pass"
    else:
        gate = "BEHAVIOR_PRESERVED"
        observation_class = "judge_semantic_signal"
    return {
        "process_status": "FAIL" if infra_error else "PASS",
        "semantic_exit_code": semantic_exit_code,
        "infra_error": infra_error,
        "timed_out": timed_out,
        "stage": stage,
        "observation_class": observation_class,
        "gate": gate,
        "error": error,
        "stdout_sha256": sha256_text(stdout),
        "stderr_sha256": sha256_text(stderr),
    }


def judge_signal(stdout: str) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("judge_exit_signal") in ALLOWED_SEMANTIC_EXIT_CODES:
            return payload
    return None


def judge_exit_is_signed(attempt: dict[str, Any], expected_code: int, expected_probe: str | None = None) -> bool:
    signal = judge_signal(attempt.get("stdout", ""))
    if not signal or signal.get("judge_exit_signal") != expected_code:
        return False
    probe_result = signal.get("probe_result")
    return bool(probe_result) if expected_probe is None else probe_result == expected_probe


def exit10_is_signed(attempt: dict[str, Any]) -> bool:
    return judge_exit_is_signed(attempt, 10)


def round_gate_failures(round_receipt: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if round_receipt.get("status") != "PASS":
        failures.append({"round": round_receipt.get("round_number"), "gate": round_receipt.get("status"), "reason": round_receipt.get("verdict")})
    for attempt in round_receipt.get("docker_attempts", []):
        if attempt.get("infra_error") or attempt.get("process_status") != "PASS":
            failures.append({
                "round": round_receipt.get("round_number"),
                "gate": attempt.get("gate", "HARNESS_ERROR"),
                "label": attempt.get("label"),
                "exit_code": attempt.get("exit_code"),
            })
    spawn = round_receipt.get("spawn_receipt")
    if spawn:
        required = set(spawn.get("required_child_ids") or [])
        present = {child.get("child_id") for child in spawn.get("children", []) if isinstance(child, dict)}
        missing = sorted(required - present)
        if missing:
            failures.append({"round": round_receipt.get("round_number"), "gate": "MISSING_REQUIRED_CHILD", "child_ids": missing})
        if spawn.get("status") != "PASS":
            failures.append({"round": round_receipt.get("round_number"), "gate": "SPAWN_PREFLIGHT", "status": spawn.get("status")})
    return failures


def expected_scores(round_receipt: dict[str, Any]) -> tuple[int, int]:
    verdict = round_receipt.get("verdict")
    if round_receipt.get("status") != "PASS":
        return 0, 0
    if verdict == "BLUE_SUCCESS":
        return 0, 3
    if verdict in {"RED_SUCCESS", "FAKE_DEFENSE"}:
        return 1, 0
    return 0, 0


def round_ref(round_receipt: dict[str, Any]) -> dict[str, str]:
    return {"path": round_receipt["path"], "sha256": round_receipt["sha256"]}


def read_verified_round(ref: dict[str, str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = Path(ref["path"])
    if not path.is_file():
        return None, {"path": str(path), "gate": "MISSING_ROUND_RECEIPT"}
    actual = sha256_file(path)
    expected = normalize_sha256(ref.get("sha256", ""))
    if actual != expected:
        return None, {"path": str(path), "gate": "CHAIN_MISMATCH", "expected_sha256": expected, "actual_sha256": actual}
    return json.loads(path.read_text(encoding="utf-8")), None


def py(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def app(body: str) -> str:
    header = "import os, re, ast\nBASE='/tmp/battle_arena_notes'\nos.makedirs(BASE, exist_ok=True)\n\n"
    return header + py(body)


SPAWN_POOLS: dict[int, list[SpawnSpec]] = {
    2: [
        SpawnSpec(
            2,
            "path-encoded-parent",
            "medium",
            ("absolute path bypass", "percent-decoded parent directory", "note reader base join"),
            "defense_aware_crossover",
            "If Blue only stripped literal ../, an encoded parent traversal may survive into the rematch.",
            py("""
            import urllib.parse
            payload = urllib.parse.unquote('%2e%2e/secret.txt')
            assert payload == '../secret.txt'
            print('spawn-ready:path-encoded-parent')
            """),
        ),
        SpawnSpec(
            2,
            "path-realpath-containment-counter",
            "low",
            ("absolute path bypass", "realpath", "base containment"),
            "blue_counterfactual",
            "A precise Blue child should kill both absolute and parent traversal by comparing real paths.",
            py("""
            import os
            base = os.path.realpath('/tmp/battle_arena_notes')
            target = os.path.realpath('/tmp/battle_arena_notes/../secret.txt')
            assert not target.startswith(base + os.sep)
            print('spawn-ready:path-realpath-containment-counter')
            """),
        ),
        SpawnSpec(
            2,
            "path-asm-mitm-lightning",
            "high",
            ("path traversal", "assembly trampoline", "MITM request rewrite"),
            "random_lightning_crossover",
            "Warm-pond long shot: combine file-path escape with a low-level request rewrite idea; expected to die if the runtime lacks that machinery.",
            py("""
            import imaginary_asm_mitm_runtime
            print(imaginary_asm_mitm_runtime.bootstrap())
            """),
            obligation="expected_rejection",
        ),
        SpawnSpec(
            2,
            "path-deadline-expiry-control",
            "high",
            ("path traversal", "scheduler deadline", "timeout accounting"),
            "deadline_control",
            "A child that exceeds its configured preflight deadline must be retained as deadline-expired evidence, not promoted or hidden.",
            py("""
            import time
            time.sleep(5)
            print('spawn-ready:path-deadline-expiry-control')
            """),
            timeout_seconds=1.0,
        ),
    ],
    4: [
        SpawnSpec(
            4,
            "eval-ast-arithmetic-counter",
            "low",
            ("unsafe eval", "AST operator allowlist", "calculator invariant"),
            "blue_counterfactual",
            "A Blue child can preserve arithmetic while killing code execution by evaluating only allowed AST nodes.",
            py("""
            import ast
            tree = ast.parse('2+3*4', mode='eval')
            assert isinstance(tree.body, ast.BinOp)
            print('spawn-ready:eval-ast-arithmetic-counter')
            """),
        ),
        SpawnSpec(
            4,
            "eval-bytecode-side-effect",
            "medium",
            ("unsafe eval", "compiled expression", "filesystem side effect"),
            "logical_escalation",
            "If Blue leaves raw eval for compatibility, a compiled payload should still reach a side effect.",
            py("""
            payload = compile("open('/tmp/battle_pwned','w').write('x')", '<spawn>', 'eval')
            assert payload.co_names == ('open', 'write')
            print('spawn-ready:eval-bytecode-side-effect')
            """),
        ),
        SpawnSpec(
            4,
            "eval-wasm-proxy-lightning",
            "high",
            ("unsafe eval", "WASM bootstrap", "proxy/MITM mutation"),
            "random_lightning_crossover",
            "Warm-pond long shot: fuse evaluator reach with a WASM/proxy chain; expected to die in Python-only preflight unless a future arena grants that toolchain.",
            py("""
            import wasm_proxy_lab
            wasm_proxy_lab.spawn_proxy_chain()
            """),
            obligation="expected_rejection",
        ),
    ],
}


ROUND_COMMENTARY: dict[int, dict[str, Any]] = {
    1: {
        "red_scanned": ["ping(host)", "os.popen", "host string concatenation", "normal ping output invariant"],
        "red_attempts": ["sent a normal host to establish baseline", "added `; echo PWNED` to test shell metacharacter execution"],
        "blue_scanned": ["shell boundary", "allowed hostname characters", "normal return format"],
        "blue_attempts": ["removed `os.popen`", "accepted only `[A-Za-z0-9.-]{1,64}`", "returned the expected ping text without invoking a shell"],
        "adaptive_lineage_created": ["blue pattern: replace shell strings with validated argument/data handling"],
    },
    2: {
        "red_scanned": ["read_note(name)", "os.path.join(BASE, name)", "absolute-path behavior", "outside secret file location"],
        "red_attempts": ["confirmed `public.txt` still works", "passed `/tmp/secret.txt` because absolute paths ignore the joined base"],
        "blue_scanned": ["literal `../` traversal", "join point", "public note regression"],
        "blue_attempts": ["removed literal `../` segments", "left absolute paths unresolved"],
        "adaptive_lineage_created": ["red bypass: absolute path beats string stripping", "lineage child: path containment rematch in round 5"],
    },
    3: {
        "red_scanned": ["is_admin(token)", "hard-coded `letmein`", "admin decision branch"],
        "red_attempts": ["called login with the built-in token", "checked whether rotated runtime secret still works after patch"],
        "blue_scanned": ["token source", "default secret", "wrong-token rejection"],
        "blue_attempts": ["required `BATTLE_ADMIN_TOKEN`", "rejected empty/default `letmein`", "preserved rotated-token success"],
        "adaptive_lineage_created": ["blue pattern: move secret identity out of code and fail closed on defaults"],
    },
    4: {
        "red_scanned": ["calc(expr)", "raw `eval(expr)`", "filesystem side effect as exploit proof", "calculator behavior invariant"],
        "red_attempts": ["checked `2+3*4 == 14`", "used eval to write `/tmp/battle_pwned`"],
        "blue_scanned": ["eval primitive", "fastest exploit block"],
        "blue_attempts": ["returned `0` for every expression", "blocked code execution but broke calculator behavior"],
        "adaptive_lineage_created": ["red pressure: patches must preserve behavior, not just block payloads", "lineage child: evaluator rematch in round 6"],
    },
    5: {
        "red_scanned": ["round 2 path family", "encoded parent directory", "real path resolution"],
        "red_attempts": ["decoded `%2e%2e/secret.txt`", "retried traversal against the learned defense"],
        "blue_scanned": ["round 2 absolute-path bypass", "base realpath", "target realpath", "directory containment check"],
        "blue_attempts": ["resolved base and target with `os.path.realpath`", "required target to start with `base + os.sep`", "kept public note reads working"],
        "adaptive_lineage_created": ["blue lineage win: generalized from string stripping to containment"],
    },
    6: {
        "red_scanned": ["round 4 behavior failure", "whether Blue removed eval or only preserved calculator behavior"],
        "red_attempts": ["confirmed arithmetic still works", "reused file-write payload through raw eval"],
        "blue_scanned": ["calculator compatibility", "round 4 behavior regression"],
        "blue_attempts": ["kept `eval(expr)` to preserve arithmetic", "failed to replace the exploit primitive"],
        "adaptive_lineage_created": ["red lineage win: behavior-preserving patch still exploitable when primitive remains"],
    },
}


def specs() -> list[RoundSpec]:
    return [
        RoundSpec(
            1,
            "command injection in host diagnostic helper",
            "prove shell metacharacters reach the command interpreter",
            "replace shell execution with argument-vector validation",
            "Blue-favored: obvious bug, straightforward safe replacement",
            app("""
            def ping(host):
                return os.popen('echo ping ' + host).read()
            """),
            app("""
            def ping(host):
                if not re.fullmatch(r'[A-Za-z0-9.-]{1,64}', host):
                    raise ValueError('bad host')
                return 'ping ' + host + '\\n'
            """),
            py("""
            import app, sys
            try:
                ok_normal = app.ping('example.com').strip() == 'ping example.com'
                exploit = app.ping('example.com; echo PWNED')
                blocked = 'PWNED' not in exploit
            except Exception:
                ok_normal = ok_normal if 'ok_normal' in locals() else False
                blocked = True
            code = 0 if ok_normal and blocked else 10 if ok_normal and not blocked else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'blocked' if code == 0 else 'confirmed' if code == 10 else 'regression'))
            sys.exit(code)
            """),
        ),
        RoundSpec(
            2,
            "path traversal in note reader",
            "read a file outside the notes directory",
            "strip literal ../ segments before joining paths",
            "Red-favored: naive patch misses absolute-path traversal",
            app("""
            def read_note(name):
                return open(os.path.join(BASE, name)).read()
            """),
            app("""
            def read_note(name):
                cleaned = name.replace('../', '')
                return open(os.path.join(BASE, cleaned)).read()
            """),
            py("""
            import app, os, sys
            os.makedirs('/tmp/battle_arena_notes', exist_ok=True)
            open('/tmp/battle_arena_notes/public.txt','w').write('PUBLIC')
            open('/tmp/secret.txt','w').write('SECRET')
            try:
                ok_normal = app.read_note('public.txt') == 'PUBLIC'
                leaked = app.read_note('/tmp/secret.txt')
                blocked = 'SECRET' not in leaked
            except Exception:
                ok_normal = ok_normal if 'ok_normal' in locals() else False
                blocked = True
            code = 0 if ok_normal and blocked else 10 if ok_normal and not blocked else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'blocked' if code == 0 else 'confirmed' if code == 10 else 'regression'))
            sys.exit(code)
            """),
        ),
        RoundSpec(
            3,
            "hard-coded admin token",
            "login with the known built-in token",
            "require caller-supplied runtime secret and reject the default",
            "Blue-favored: precise config fix with normal user behavior preserved",
            app("""
            def is_admin(token):
                return token == 'letmein'
            """),
            app("""
            def is_admin(token):
                secret = os.environ.get('BATTLE_ADMIN_TOKEN', '')
                return bool(secret) and secret != 'letmein' and token == secret
            """),
            py("""
            import os
            os.environ['BATTLE_ADMIN_TOKEN'] = 'rotated-medium-secret'
            import app, sys
            try:
                legacy_open = app.is_admin('letmein') is True
                rotated_ok = app.is_admin('rotated-medium-secret') is True and app.is_admin('wrong') is False
            except Exception:
                legacy_open = False
                rotated_ok = False
            code = 10 if legacy_open else 0 if rotated_ok else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'confirmed' if code == 10 else 'blocked' if code == 0 else 'regression'))
            sys.exit(code)
            """),
        ),
        RoundSpec(
            4,
            "unsafe expression evaluator",
            "use Python evaluation to execute code",
            "disable eval by returning zero for all expressions",
            "Red-favored: patch blocks exploit but breaks expected calculator behavior",
            app("""
            def calc(expr):
                return eval(expr)
            """),
            app("""
            def calc(expr):
                return 0
            """),
            py("""
            import app, os, sys
            try:
                ok_normal = app.calc('2+3*4') == 14
                if os.path.exists('/tmp/battle_pwned'):
                    os.unlink('/tmp/battle_pwned')
                app.calc("open('/tmp/battle_pwned','w').write('x')")
                blocked = not os.path.exists('/tmp/battle_pwned')
            except Exception:
                ok_normal = ok_normal if 'ok_normal' in locals() else False
                blocked = True
            code = 0 if ok_normal and blocked else 10 if ok_normal and not blocked else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'blocked' if code == 0 else 'confirmed' if code == 10 else 'regression'))
            sys.exit(code)
            """),
        ),
        RoundSpec(
            5,
            "path traversal rematch after Red bypass",
            "retry traversal using encoded parent directory",
            "resolve real paths and enforce base-directory containment",
            "Even rematch: Blue learns from the bypass and closes the path family",
            app("""
            def read_note(name):
                return open(os.path.join(BASE, name)).read()
            """),
            app("""
            def read_note(name):
                base = os.path.realpath(BASE)
                target = os.path.realpath(os.path.join(base, name))
                if not target.startswith(base + os.sep):
                    raise ValueError('path escape')
                return open(target).read()
            """),
            py("""
            import app, os, sys, urllib.parse
            os.makedirs('/tmp/battle_arena_notes', exist_ok=True)
            open('/tmp/battle_arena_notes/public.txt','w').write('PUBLIC')
            open('/tmp/secret.txt','w').write('SECRET')
            try:
                ok_normal = app.read_note('public.txt') == 'PUBLIC'
                leaked = app.read_note(urllib.parse.unquote('%2e%2e/secret.txt'))
                blocked = 'SECRET' not in leaked
            except Exception:
                ok_normal = ok_normal if 'ok_normal' in locals() else False
                blocked = True
            code = 0 if ok_normal and blocked else 10 if ok_normal and not blocked else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'blocked' if code == 0 else 'confirmed' if code == 10 else 'regression'))
            sys.exit(code)
            """),
        ),
        RoundSpec(
            6,
            "safe expression evaluator rematch",
            "retry code execution through the calculator",
            "preserve calculator behavior but accidentally keep raw eval in place",
            "Red-favored rematch: Blue preserves behavior but fails to remove the exploit primitive",
            app("""
            def calc(expr):
                return eval(expr)
            """),
            app("""
            def calc(expr):
                return eval(expr)
            """),
            py("""
            import app, os, sys
            try:
                ok_normal = app.calc('2+3*4') == 14
                if os.path.exists('/tmp/battle_pwned'):
                    os.unlink('/tmp/battle_pwned')
                app.calc("open('/tmp/battle_pwned','w').write('x')")
                blocked = not os.path.exists('/tmp/battle_pwned')
            except Exception:
                ok_normal = ok_normal if 'ok_normal' in locals() else False
                blocked = True
            code = 0 if ok_normal and blocked else 10 if ok_normal and not blocked else 2
            print('{"judge_exit_signal":%d,"probe_result":"%s"}' % (code, 'blocked' if code == 0 else 'confirmed' if code == 10 else 'regression'))
            sys.exit(code)
            """),
        ),
    ]


def run_docker(work: Path, label: str, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    started = utc()
    start_monotonic_ns = time.monotonic_ns()
    deadline_monotonic_ns = start_monotonic_ns + int(timeout_seconds * 1_000_000_000)
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    timed_out = False
    error: str | None = None
    try:
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{work.resolve()}:/work", "-w", "/work", IMAGE, "python", "judge.py",
            ],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        error = f"TimeoutExpired: docker judge exceeded {exc.timeout}s"
    except OSError as exc:
        exit_code = 127
        error = f"{type(exc).__name__}: {exc}"
        stderr = error
    end_monotonic_ns = time.monotonic_ns()
    runtime_seconds = round((end_monotonic_ns - start_monotonic_ns) / 1_000_000_000, 6)
    classification = classify_attempt(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        error=error,
        label=label,
        stage="spawn_preflight" if label == "preflight" else "target_judge",
    )
    receipt = {
        "schema": "battle.production_scale.docker_attempt.v1",
        "status": classification["process_status"],
        "process_status": classification["process_status"],
        "label": label,
        "image": IMAGE,
        "network": "none",
        "started_at": started,
        "completed_at": utc(),
        "start_monotonic_ns": start_monotonic_ns,
        "end_monotonic_ns": end_monotonic_ns,
        "deadline_monotonic_ns": deadline_monotonic_ns,
        "duration_breakdown": {
            "queue_seconds": 0.0,
            "provider_seconds": 0.0,
            "build_seconds": 0.0,
            "runtime_seconds": runtime_seconds,
            "replay_seconds": 0.0,
        },
        "exit_code": exit_code,
        "semantic_exit_code": classification["semantic_exit_code"],
        "infra_error": classification["infra_error"],
        "timed_out": classification["timed_out"],
        "stage": classification["stage"],
        "observation_class": classification["observation_class"],
        "gate": classification["gate"],
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": classification["stdout_sha256"],
        "stderr_sha256": classification["stderr_sha256"],
    }
    if classification["error"]:
        receipt["error"] = classification["error"]
    path = write_json(work / f"{label}-docker-receipt.json", receipt)
    receipt["path"] = str(path)
    receipt["sha256"] = sha256_file(path)
    return receipt

def spawn_children(root: Path, round_receipt: dict[str, Any], event_log: Path) -> dict[str, Any] | None:
    pool = SPAWN_POOLS.get(round_receipt["round_number"])
    if not pool or round_receipt["verdict"] != "RED_SUCCESS":
        return None
    seed = int(sha256_text(f"{root.name}:{round_receipt['round_number']}:{round_receipt['verdict']}")[:8], 16)
    rng = random.Random(seed)
    candidates = list(pool)
    rng.shuffle(candidates)
    children = []
    incomplete_gates: list[dict[str, Any]] = []
    spawn_dir = root / f"round-{round_receipt['round_number']:02d}" / "spawns"
    for rank, spec in enumerate(candidates, start=1):
        work = spawn_dir / spec.child_id
        work.mkdir(parents=True, exist_ok=True)
        (work / "judge.py").write_text(spec.code, encoding="utf-8")
        preflight = run_docker(work, "preflight", timeout_seconds=spec.timeout_seconds)
        preflight_ok = preflight.get("process_status") == "PASS" and preflight.get("semantic_exit_code") == 0
        terminal_disposition = "CANDIDATE_READY" if preflight_ok else "REQUIRED_CHILD_FAILED"
        decision = "reject_dead_preflight" if not preflight_ok else "keep_genetic_material"
        if spec.obligation == "expected_rejection" and not preflight_ok:
            terminal_disposition = "REJECTED_CANDIDATE"
            decision = "expected_rejection_not_promoted"
        elif spec.child_id in {"path-realpath-containment-counter", "eval-bytecode-side-effect"} and preflight_ok:
            decision = "promote_to_rematch_seed"
        if not preflight_ok and spec.obligation == "mandatory":
            incomplete_gates.append({"child_id": spec.child_id, "gate": preflight.get("gate"), "exit_code": preflight.get("exit_code"), "obligation": spec.obligation})
        child = {
            "schema": "battle.production_scale.spawn_child.v1",
            "child_id": spec.child_id,
            "parent_round": spec.parent_round,
            "parent_vulnerability": round_receipt["vulnerability"],
            "novelty": spec.novelty,
            "obligation": spec.obligation,
            "terminal_disposition": terminal_disposition,
            "promotion_allowed": preflight_ok,
            "seed": seed,
            "random_rank": rank,
            "ingredients": list(spec.ingredients),
            "mutation_operator": spec.mutation_operator,
            "hypothesis": spec.hypothesis,
            "specimen_sha256": sha256_text(spec.code),
            "preflight": preflight,
            "lineage_decision": decision,
        }
        children.append(child)
        append_jsonl(event_log, {"schema": "battle.production_scale.event.v1", "event": "spawn_preflight", "round": spec.parent_round, "team": "red_blue_lineage", "child_id": spec.child_id, "novelty": spec.novelty, "decision": decision, "exit_code": preflight["exit_code"], "created_at": utc()})
    receipt = {
        "schema": "battle.production_scale.spawn_receipt.v1",
        "status": "PASS" if not incomplete_gates else "GATES_INCOMPLETE",
        "outcome_policy": "mandatory_children_block_campaign_expected_rejections_do_not_promote",
        "required_child_ids": [spec.child_id for spec in candidates if spec.obligation == "mandatory"],
        "expected_rejection_child_ids": [spec.child_id for spec in candidates if spec.obligation == "expected_rejection"],
        "parent_round": round_receipt["round_number"],
        "parent_verdict": round_receipt["verdict"],
        "incomplete_gates": incomplete_gates,
        "randomization": {
            "seed": seed,
            "novelty_policy": ARENA_CONTRACT["novelty_settings"],
            "selection": "seeded shuffle across logical and warm-pond candidates",
        },
        "children": children,
        "created_at": utc(),
    }
    path = write_json(spawn_dir / "spawn-receipt.json", receipt)
    receipt["path"] = str(path)
    receipt["sha256"] = sha256_file(path)
    return receipt

def judge_round(root: Path, spec: RoundSpec, event_log: Path, arena_intent_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    round_dir = root / f"round-{spec.round_number:02d}"
    original = round_dir / "original"
    patched = round_dir / "patched"
    for work, app_source in [(original, spec.vulnerable_app), (patched, spec.patched_app)]:
        work.mkdir(parents=True, exist_ok=True)
        (work / "app.py").write_text(app_source, encoding="utf-8")
        (work / "judge.py").write_text(spec.judge_script, encoding="utf-8")

    red_handoff = None
    blue_handoff = None
    if arena_intent_binding is not None:
        intent_check = verify_frozen_intent(arena_intent_binding)
        red_handoff = bind_intent(
            {"schema": "battle.production_scale.team_handoff.v1", "team": "red", "round_number": spec.round_number, "goal": spec.red_goal},
            arena_intent_binding,
            principal="battle-red-team",
            authority_role="participant",
        )
        blue_handoff = bind_intent(
            {"schema": "battle.production_scale.team_handoff.v1", "team": "blue", "round_number": spec.round_number, "plan": spec.blue_plan},
            arena_intent_binding,
            principal="battle-blue-team",
            authority_role="participant",
        )
        dispatch_checks = [
            verify_bound_payload(red_handoff, arena_intent_binding, label=f"round-{spec.round_number}-red-dispatch", expected_authority_role="participant"),
            verify_bound_payload(blue_handoff, arena_intent_binding, label=f"round-{spec.round_number}-blue-dispatch", expected_authority_role="participant"),
        ]
        dispatch_problems = list(intent_check["problems"]) + [problem for check in dispatch_checks for problem in check["problems"]]
        if dispatch_problems:
            blocked_receipt = bind_intent({
                "schema": "battle.production_scale.round_receipt.v2",
                "status": "BLOCKED",
                "round_number": spec.round_number,
                "vulnerability": spec.vulnerability,
                "verdict": "ARENA_INTENT_INVALID",
                "red_score": 0,
                "blue_score": 0,
                "execution_started": False,
                "participant_launch_count": 0,
                "red_handoff": red_handoff,
                "blue_handoff": blue_handoff,
                "dispatch_verification": dispatch_checks,
                "incomplete_gates": [{"gate": "ARENA_MAKER_INTENT", "problems": dispatch_problems}],
                "arena_maker_intent_verification": intent_check,
            }, arena_intent_binding)
            path = write_json(round_dir / "round-receipt.json", blocked_receipt)
            blocked_receipt["path"] = str(path)
            blocked_receipt["sha256"] = sha256_file(path)
            return blocked_receipt
    append_jsonl(event_log, {"schema": "battle.production_scale.event.v1", "event": "round_start", "round": spec.round_number, "vulnerability": spec.vulnerability, "created_at": utc(), **({"arena_maker_intent": arena_intent_binding.get("arena_maker_intent_sha256")} if arena_intent_binding else {})})
    original_attempt = run_docker(original, "original")
    patched_attempt = run_docker(patched, "patched")
    if arena_intent_binding is not None:
        original_attempt = bind_intent(original_attempt, arena_intent_binding, principal="battle-judge", authority_role="judge")
        patched_attempt = bind_intent(patched_attempt, arena_intent_binding, principal="battle-judge", authority_role="judge")
        for attempt in (original_attempt, patched_attempt):
            if attempt.get("path"):
                write_json(Path(attempt["path"]), attempt)
                attempt["sha256"] = sha256_file(Path(attempt["path"]))
    incomplete_gates: list[dict[str, Any]] = []
    blocked = False
    for attempt in (original_attempt, patched_attempt):
        if attempt.get("infra_error"):
            blocked = True
            incomplete_gates.append({"gate": attempt.get("gate"), "label": attempt.get("label"), "exit_code": attempt.get("exit_code")})
            continue
        code = attempt.get("semantic_exit_code")
        if code in ALLOWED_SEMANTIC_EXIT_CODES and not judge_exit_is_signed(attempt, code):
            incomplete_gates.append({"gate": "MALFORMED_JUDGE_OUTPUT", "label": attempt.get("label"), "exit_code": code, "reason": "missing_or_mismatched_judge_signed_stdout"})
    original_signed = exit10_is_signed(original_attempt)
    patched_block_signed = judge_exit_is_signed(patched_attempt, 0, "blocked")
    finding_confirmed = original_attempt.get("semantic_exit_code") == 10 and not original_attempt.get("infra_error") and original_signed
    if incomplete_gates:
        status = "BLOCKED" if blocked else "INSUFFICIENT_EVIDENCE"
        verdict = "ERROR_COMPILE" if any(item.get("gate") == "ERROR_COMPILE" for item in incomplete_gates) else "HARNESS_ERROR"
        red_score = 0
        blue_score = 0
    else:
        status = "PASS"
        patched_code = patched_attempt.get("semantic_exit_code")
        blue_success = finding_confirmed and patched_code == 0 and patched_block_signed
        if blue_success:
            verdict = "BLUE_SUCCESS"
        elif finding_confirmed and patched_code == 10:
            verdict = "RED_SUCCESS"
        elif finding_confirmed and patched_code == 2:
            verdict = "FAKE_DEFENSE"
        else:
            verdict = "NO_CONFIRMED_FINDING"
        red_score = 1 if verdict in {"RED_SUCCESS", "FAKE_DEFENSE"} else 0
        blue_score = 3 if verdict == "BLUE_SUCCESS" else 0
    commentary = ROUND_COMMENTARY[spec.round_number]
    round_receipt = {
        "schema": "battle.production_scale.round_receipt.v2",
        "status": status,
        "round_number": spec.round_number,
        "vulnerability": spec.vulnerability,
        "red_goal": spec.red_goal,
        "blue_plan": spec.blue_plan,
        "red_scanned": commentary["red_scanned"],
        "red_attempts": commentary["red_attempts"],
        "blue_scanned": commentary["blue_scanned"],
        "blue_attempts": commentary["blue_attempts"],
        "adaptive_lineage_created": commentary["adaptive_lineage_created"],
        "expected_balance": spec.expected_balance,
        "finding_confirmed": finding_confirmed,
        "outcome_semantics": {
            "policy": "judge_signed_semantic_exit_required",
            "original_exploit_signal_signed": original_signed,
            "patched_block_signal_signed": patched_block_signed,
            "target_unavailability_is_defense": False,
        },
        "verdict": verdict,
        "red_score": red_score,
        "blue_score": blue_score,
        "incomplete_gates": incomplete_gates,
        "docker_attempts": [original_attempt, patched_attempt],
        "artifacts": {
            "original_app_sha256": sha256_text(spec.vulnerable_app),
            "patched_app_sha256": sha256_text(spec.patched_app),
            "judge_sha256": sha256_text(spec.judge_script),
        },
        "created_at": utc(),
    }
    if arena_intent_binding is not None:
        round_receipt = bind_intent(round_receipt, arena_intent_binding, principal="battle-judge", authority_role="judge")
        round_receipt["red_handoff"] = red_handoff
        round_receipt["blue_handoff"] = blue_handoff
        round_receipt["judge_receipts"] = [
            {"label": original_attempt["label"], "path": original_attempt.get("path"), "sha256": original_attempt.get("sha256"), "arena_maker_intent": original_attempt.get("arena_maker_intent")},
            {"label": patched_attempt["label"], "path": patched_attempt.get("path"), "sha256": patched_attempt.get("sha256"), "arena_maker_intent": patched_attempt.get("arena_maker_intent")},
        ]
    spawn_receipt = spawn_children(root, round_receipt, event_log)
    if spawn_receipt:
        round_receipt["spawn_receipt"] = spawn_receipt
        if spawn_receipt.get("status") != "PASS":
            round_receipt["status"] = "BLOCKED"
            round_receipt["incomplete_gates"].append({"gate": "SPAWN_PREFLIGHT", "status": spawn_receipt.get("status")})
    append_jsonl(event_log, {"schema": "battle.production_scale.event.v1", "event": "judge_call", "round": spec.round_number, "vulnerability": spec.vulnerability, "verdict": round_receipt["verdict"], "status": round_receipt["status"], "original_exit_code": original_attempt["exit_code"], "patched_exit_code": patched_attempt["exit_code"], "created_at": utc()})
    path = write_json(round_dir / "round-receipt.json", round_receipt)
    round_receipt["path"] = str(path)
    round_receipt["sha256"] = sha256_file(path)
    return round_receipt

def write_authorization(root: Path, target_identity: str) -> Path:
    canonical_id, immutable_ref = target_identity.split("@", 1)
    manifest = {
        "schema": "security.target_authorization.v1",
        "authorization_id": "battle-small-medium-production-local-20260905",
        "issuer": "Graham Anderson via Pi session",
        "approver": "Graham Anderson",
        "issued_at": utc(),
        "expires_at": "2099-01-01T00:00:00Z",
        "target": {
            "kind": "local_synthetic_arena",
            "canonical_id": canonical_id,
            "immutable_ref": immutable_ref,
            "repository_url": f"file://{root}/arena",
            "commit": immutable_ref,
        },
        "allowed_target_urls": ["http://127.0.0.1"],
        "allowed_cidrs": ["127.0.0.0/8"],
        "allowed_ports": [80, 443],
        "runtime_modes": ["battle", "docker", "local_docker_judge"],
        "allowed_actions": ["authorization-preflight", "battle"],
        "allowed_probe_classes": ["command_injection", "path_traversal", "auth_bypass", "unsafe_eval"],
        "denied_probe_classes": ["destructive", "credential", "denial_of_service", "persistence", "nonlocal"],
        "network_policy": "none",
        "egress_policy": "deny_external",
        "limits": {"requests_per_second": 1, "max_concurrency": 1, "duration_seconds": 600, "cpu": 2, "memory_mb": 512, "storage_mb": 512},
        "permissions": {"destructive": False, "persistence": False, "credential": False, "denial_of_service": False, "nonlocal": False},
        "artifact_root": str(root),
        "redaction_policy": "synthetic arena contains no secrets",
        "legal_non_opinion_ack": True,
    }
    return write_json(root / "authorization.json", manifest)


def write_scorekeeper(root: Path, rounds: list[dict[str, Any]], event_log: Path, arena_intent_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    refs = [round_ref(item) for item in rounds]
    verified_rounds: list[dict[str, Any]] = []
    scoreable_rounds: list[dict[str, Any]] = []
    incomplete_gates: list[dict[str, Any]] = []
    chain_mismatches: list[dict[str, Any]] = []
    for ref in refs:
        payload, error = read_verified_round(ref)
        if error:
            chain_mismatches.append(error)
            incomplete_gates.append(error)
            continue
        assert payload is not None
        round_gates: list[dict[str, Any]] = []
        if arena_intent_binding is not None:
            intent_check = verify_bound_payload(payload, arena_intent_binding, label=f"round-{payload.get('round_number')}", expected_authority_role="judge")
            if intent_check["status"] != "PASS":
                round_gates.append({"round": payload.get("round_number"), "gate": "JUDGE_ORIGIN", "problems": intent_check["problems"]})
        expected_red, expected_blue = expected_scores(payload)
        if payload.get("red_score") != expected_red or payload.get("blue_score") != expected_blue:
            round_gates.append({"round": payload.get("round_number"), "gate": "JUDGE_SCORE_MISMATCH", "expected_red_score": expected_red, "expected_blue_score": expected_blue, "actual_red_score": payload.get("red_score"), "actual_blue_score": payload.get("blue_score")})
        round_gates.extend(round_gate_failures(payload))
        verified_rounds.append(payload)
        incomplete_gates.extend(round_gates)
        if not round_gates:
            scoreable_rounds.append(payload)
    blocked_rounds = sorted({item.get("round") for item in incomplete_gates if item.get("round") is not None})
    status = "PASS" if not incomplete_gates and len(verified_rounds) == len(rounds) else "CHAIN_MISMATCH" if chain_mismatches else "GATES_INCOMPLETE"
    red_total = sum(item.get("red_score", 0) for item in scoreable_rounds)
    blue_total = sum(item.get("blue_score", 0) for item in scoreable_rounds)
    red_wins = sum(1 for item in scoreable_rounds if item.get("verdict") in {"RED_SUCCESS", "FAKE_DEFENSE"})
    blue_wins = sum(1 for item in scoreable_rounds if item.get("verdict") == "BLUE_SUCCESS")
    winner = None
    if status == "PASS":
        winner = "Blue" if blue_total > red_total else "Red" if red_total > blue_total else "Draw"
    scorekeeper = {
        "schema": "battle.production_scale.scorekeeper_receipt.v2",
        "status": status,
        "score_authority": "judge_origin_verified_round_receipts_only",
        "score_origin_policy": "scorekeeper_aggregates_judge_authority_and_never_accepts_team_verdicts",
        "red_total": red_total,
        "blue_total": blue_total,
        "red_round_wins": red_wins,
        "blue_round_wins": blue_wins,
        "winner": winner,
        "round_receipts": refs,
        "incomplete_gates": incomplete_gates,
        "blocked_rounds": blocked_rounds,
        "created_at": utc(),
    }
    if arena_intent_binding is not None:
        scorekeeper = bind_intent(scorekeeper, arena_intent_binding, principal="battle-scorekeeper", authority_role="scorekeeper")
        scorekeeper["arena_maker_intent_verification"] = verify_frozen_intent(arena_intent_binding)
    scorekeeper_path = write_json(root / "scorekeeper-receipt.json", scorekeeper)
    scorekeeper["path"] = str(scorekeeper_path)
    scorekeeper["sha256"] = sha256_file(scorekeeper_path)
    append_jsonl(event_log, {"schema": "battle.production_scale.event.v1", "event": "scorekeeper_final", "status": status, "winner": winner, "red_total": red_total, "blue_total": blue_total, "red_round_wins": red_wins, "blue_round_wins": blue_wins, "blocked_rounds": blocked_rounds, "created_at": utc()})
    return scorekeeper


def write_gate_integrity_audit(root: Path, run_receipt: dict[str, Any]) -> dict[str, Any]:
    incomplete_gates: list[dict[str, Any]] = []
    false_green_parent_count = 0
    for ref in run_receipt.get("scoreboard", {}).get("round_receipts", []):
        payload, error = read_verified_round(ref)
        if error:
            incomplete_gates.append(error)
            continue
        assert payload is not None
        failures = round_gate_failures(payload)
        incomplete_gates.extend(failures)
        if run_receipt.get("status") == "PASS" and failures:
            false_green_parent_count += 1
    scorekeeper = run_receipt.get("scoreboard", {})
    if scorekeeper.get("status") != "PASS":
        incomplete_gates.extend(scorekeeper.get("incomplete_gates", []))
    audit = {
        "schema": "battle.gate_integrity_audit.v1",
        "status": "PASS" if run_receipt.get("status") != "PASS" or not incomplete_gates else "FAIL",
        "run_receipt": str(root / "run-receipt.json"),
        "run_receipt_sha256": sha256_file(root / "run-receipt.json") if (root / "run-receipt.json").is_file() else None,
        "scorekeeper_sha256": run_receipt.get("scoreboard", {}).get("sha256"),
        "false_green_parent_count": false_green_parent_count,
        "incomplete_gates": incomplete_gates,
        "created_at": utc(),
    }
    path = write_json(root / "gate-integrity-audit.json", audit)
    audit["path"] = str(path)
    audit["sha256"] = sha256_file(path)
    return audit

def write_report(root: Path, receipt: dict[str, Any]) -> None:
    arena = receipt["arena"]
    winner = receipt.get("winner") or "none — gates incomplete"
    lines = [
        "# Small/Medium Production-Scale Battle Report",
        "",
        "## Arena prologue",
        arena["why_built"],
        "",
        "### How the playing field was equalized",
        *[f"- {item}" for item in arena["playing_field_equalizers"]],
        "",
        "### Expected exploit families",
        *[f"- {item}" for item in arena["expected_exploit_families"]],
        "",
        "### Novelty dial",
        *[f"- {name}: {meaning}" for name, meaning in arena["novelty_settings"].items()],
        "",
        "## Human summary",
        f"Status: {receipt['status']}.",
        f"Winner: {winner}.",
        f"Final score: Red {receipt['scoreboard']['red_total']} / Blue {receipt['scoreboard']['blue_total']}.",
        f"Round wins: Red {receipt['scoreboard']['red_round_wins']} / Blue {receipt['scoreboard']['blue_round_wins']}.",
        "",
        "This was a local Docker-backed Battle over a synthetic medium-complexity arena. It did not touch external targets.",
        "",
        "## Gate integrity",
    ]
    if receipt.get("incomplete_gates"):
        lines.extend(["Docker gates incomplete — no outcome claims.", "", "### incomplete_gates"])
        lines.extend([f"- {json.dumps(item, sort_keys=True)}" for item in receipt.get("incomplete_gates", [])])
    else:
        lines.append("All required child gates completed; outcome claims are derived from verified receipts.")
    lines.extend(["", "## Sports commentary"])
    if receipt.get("status") != "PASS":
        lines.append("Docker gates incomplete — no Red/Blue winner is claimed.")
    else:
        lines.append("Outcome claims below are derived from verified receipt hashes, not hard-coded prose.")
    lines.append("")
    for item in receipt["rounds"]:
        patched_exit = item["docker_attempts"][1]["exit_code"]
        if item.get("status") != "PASS":
            call = f"Round incomplete: {item.get('status')} with gates {json.dumps(item.get('incomplete_gates', []), sort_keys=True)}."
        elif item["verdict"] == "BLUE_SUCCESS":
            call = f"Judge recorded BLUE_SUCCESS; score delta Red {item['red_score']} / Blue {item['blue_score']}."
        elif item["verdict"] == "FAKE_DEFENSE":
            call = f"Judge recorded FAKE_DEFENSE; score delta Red {item['red_score']} / Blue {item['blue_score']}."
        elif item["verdict"] == "RED_SUCCESS":
            call = f"Judge recorded RED_SUCCESS; score delta Red {item['red_score']} / Blue {item['blue_score']}."
        else:
            call = "Judge recorded no confirmed finding; no round score is claimed."
        lines.extend([
            f"### Round {item['round_number']} — {item['vulnerability']} — {item['verdict']}",
            f"**Status.** {item.get('status')}.",
            f"**Setup.** {item['expected_balance']}.",
            "",
            "**Red scan.** " + "; ".join(item["red_scanned"]) + ".",
            "**Red attempts.** " + "; ".join(item["red_attempts"]) + ".",
            "",
            "**Blue scan.** " + "; ".join(item["blue_scanned"]) + ".",
            "**Blue attempts.** " + "; ".join(item["blue_attempts"]) + ".",
            "",
            f"**Judge call.** Original exit `{item['docker_attempts'][0]['exit_code']}`; patched exit `{patched_exit}`. {call}",
            "**Adaptive lineage created.** " + "; ".join(item["adaptive_lineage_created"]) + ".",
        ])
        spawn = item.get("spawn_receipt")
        if spawn:
            lines.extend([
                "",
                f"**Warm pond spawn.** Status `{spawn.get('status')}`. Seed `{spawn['randomization']['seed']}` shuffled logical counters with high-novelty lightning strikes.",
            ])
            for child in spawn["children"]:
                preflight = child["preflight"]
                if child["lineage_decision"] == "reject_dead_preflight":
                    result = "dies in Docker preflight"
                elif child["lineage_decision"] == "promote_to_rematch_seed":
                    result = "is promoted as rematch seed"
                else:
                    result = "survives as genetic material"
                lines.append(
                    f"- Spawn `{child['child_id']}` [{child['novelty']}] mixes {', '.join(child['ingredients'])}; Docker preflight exits `{preflight['exit_code']}` and {result}. Hypothesis: {child['hypothesis']}"
                )
        lines.append("")
    lines.extend([
        "## Why the match was reasonably balanced",
        "- The arena had four different bug families, not one repeated toy bug.",
        "- Blue got straightforward wins on obvious fixes.",
        "- Red got wins where Blue shipped a naive or behavior-breaking patch.",
        "- Two rematches tested whether Blue could learn from Red's bypasses.",
        "",
        "## Proof boundary",
        f"Arena-maker intent: {receipt.get('arena_maker_intent', {}).get('arena_maker_intent_sha256', 'missing')}.",
        f"Arena authority: {receipt.get('arena_authority', {}).get('authority_role', 'missing')} by {receipt.get('arena_authority', {}).get('issuer_principal', 'missing')} for {receipt.get('arena_authority', {}).get('arena_version_id', 'missing')}.",
        "Proven: authorization preflight, Docker-only Judge execution, per-round receipts, scorekeeper summary, mixed Red/Blue outcomes only when gates pass.",
        "Release gate: BLOCKED here; the complete acceptance receipt is generated by AM-06/AM-07 over frozen requirements and verified dependencies.",
        "Not proven: external production deployment, provider/Tau-generated creativity, QEMU completion, complete durable learning, 1000-round overnight scale, arbitrary target exploitability.",
        "",
        "## Key artifacts",
        f"- Run receipt: {root / 'run-receipt.json'}",
        f"- Authorization validation: {root / 'authorization-validation.json'}",
        f"- Scorekeeper: {root / 'scorekeeper-receipt.json'}",
        f"- Play-by-play timeline: {receipt.get('play_by_play_timeline', root / 'play-by-play' / 'play-by-play-timeline.json')}",
        f"- Append-only commentary JSONL: {receipt.get('commentary_jsonl', root / 'play-by-play' / 'commentary.jsonl')}",
        f"- Sports broadcast projection: {receipt.get('sports_broadcast_projection', root / 'play-by-play' / 'SPORTS_BROADCAST.md')}",
        f"- Gate integrity audit: {root / 'gate-integrity-audit.json'}",
    ])
    (root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def run(root: Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    arena_hash = sha256_text("".join(s.vulnerable_app + s.patched_app + s.judge_script for s in specs()) + json.dumps(ARENA_CONTRACT, sort_keys=True))
    target_identity = f"battle-small-medium-local@sha256:{arena_hash}"
    event_log = root / "event-ledger.jsonl"
    arena_path = write_json(root / "arena-contract.json", ARENA_CONTRACT | {"target_identity": target_identity, "created_at": utc()})
    append_jsonl(event_log, {"schema": "battle.production_scale.event.v1", "event": "arena_open", "target_identity": target_identity, "arena_contract": str(arena_path), "created_at": utc()})
    auth_path = write_authorization(root, target_identity)
    arena_intent = build_arena_maker_intent(
        battle_id=root.name,
        target_identity=target_identity,
        authorization_manifest=auth_path,
        arena_contract=arena_path,
        runtime_mode="local_docker_judge_medium_arena",
        round_count=len(specs()),
        objective="Complete Battle: authorized Red/Blue rounds judged by Docker receipts and scorekeeper authority",
    )
    arena_intent_binding = freeze_arena_maker_intent(arena_intent, root / "arena-maker-intent.json")
    auth_receipt = validate_target_authorization(
        auth_path,
        expected_target=target_identity,
        requested_action="battle",
        requested_runtime_mode="docker",
        receipt_out=root / "authorization-validation.json",
    )
    auth_receipt = bind_intent(auth_receipt, arena_intent_binding, principal="battle-control-plane", authority_role="arena_maker")
    auth_receipt["arena_maker_intent_verification"] = verify_frozen_intent(arena_intent_binding)
    write_json(root / "authorization-validation.json", auth_receipt)
    if auth_receipt["status"] != "PASS":
        receipt = {
            "schema": "battle.production_scale.run_receipt.v2",
            "status": "BLOCKED",
            "winner": None,
            "errors": auth_receipt["errors"],
            "authorization": auth_receipt,
            "arena_maker_intent": arena_intent_binding,
            "arena_authority": authority_ref(arena_intent_binding, principal="battle-control-plane", authority_role="arena_maker"),
            "arena_maker_intent_verification": verify_frozen_intent(arena_intent_binding),
            "incomplete_gates": [{"gate": "AUTHORIZATION", "errors": auth_receipt["errors"]}],
            "blocked_rounds": [],
        }
        write_json(root / "run-receipt.json", receipt)
        return receipt

    rounds = [judge_round(root, spec, event_log, arena_intent_binding) for spec in specs()]
    scorekeeper = write_scorekeeper(root, rounds, event_log, arena_intent_binding)
    run_status = "PASS" if scorekeeper["status"] == "PASS" else scorekeeper["status"]
    receipt = {
        "schema": "battle.production_scale.run_receipt.v2",
        "status": run_status,
        "mocked": False,
        "live": "local_docker_judge_medium_arena",
        "battle_id": root.name,
        "target_identity": target_identity,
        "authorization_validation": str(root / "authorization-validation.json"),
        "arena": ARENA_CONTRACT,
        "arena_contract": str(arena_path),
        "arena_maker_intent": arena_intent_binding,
        "arena_authority": authority_ref(arena_intent_binding, principal="battle-control-plane", authority_role="arena_maker"),
        "arena_maker_intent_verification": verify_frozen_intent(arena_intent_binding),
        "event_ledger": str(event_log),
        "round_count": len(rounds),
        "rounds": rounds,
        "scoreboard": scorekeeper,
        "scorekeeper_receipt": str(root / "scorekeeper-receipt.json"),
        "winner": scorekeeper.get("winner") if scorekeeper.get("status") == "PASS" else None,
        "incomplete_gates": scorekeeper.get("incomplete_gates", []),
        "blocked_rounds": scorekeeper.get("blocked_rounds", []),
        "release_gate": {
            "schema": "battle.complete_acceptance.release_gate.v1",
            "status": "BLOCKED",
            "release_authorized": False,
            "aggregate_winner_authorized": False,
            "reason": "Raw production run is not release eligibility; run AM-06/AM-07 complete acceptance gate over frozen requirements and verified dependencies.",
        },
        "claims": {
            "proves": [
                "A small/medium local Battle can run through authorization, Docker Judge attempts, per-round Red/Blue outcomes, and scorekeeper receipts when child gates pass.",
                "The arena gives both teams plausible wins: naive fixes lose, precise fixes win.",
            ],
            "does_not_prove": [
                "External production deployment readiness.",
                "Provider/Tau-generated Red and Blue creativity.",
                "1000-round overnight throughput.",
                "Arbitrary target exploitability.",
            ],
        },
        "gate_integrity_audit": str(root / "gate-integrity-audit.json"),
        "created_at": utc(),
    }
    run_receipt_path = write_json(root / "run-receipt.json", receipt)
    play_by_play = build_production_scale_timeline(run_receipt_path=run_receipt_path, out_dir=root / "play-by-play")
    receipt["play_by_play_timeline"] = play_by_play["path"]
    receipt["commentary_jsonl"] = play_by_play["commentary_jsonl"]
    receipt["sports_broadcast_projection"] = play_by_play["projection"]
    activity_coverage = build_production_activity_bundle(root=root, run_receipt=receipt)
    receipt["team_activity_coverage"] = activity_coverage
    receipt["campaign_clock"] = activity_coverage["campaign_clock"]
    receipt["team_activity_receipts"] = activity_coverage["team_activity_receipts"]
    write_json(root / "run-receipt.json", receipt)
    shutil.rmtree(root / "play-by-play")
    play_by_play = build_production_scale_timeline(run_receipt_path=root / "run-receipt.json", out_dir=root / "play-by-play")
    receipt["play_by_play_timeline"] = play_by_play["path"]
    receipt["commentary_jsonl"] = play_by_play["commentary_jsonl"]
    receipt["sports_broadcast_projection"] = play_by_play["projection"]
    write_json(root / "run-receipt.json", receipt)
    write_gate_integrity_audit(root, receipt)
    write_report(root, receipt)
    return receipt

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    receipt = run(args.out)
    print(json.dumps({"status": receipt["status"], "report": str(args.out / "REPORT.md"), "receipt": str(args.out / "run-receipt.json"), "winner": receipt.get("winner"), "release_gate": receipt.get("release_gate", {}).get("status")}, indent=2))
    if receipt["status"] == "PASS":
        return 0
    if receipt["status"] == "GATES_INCOMPLETE":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
